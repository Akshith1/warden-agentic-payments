"""No network for the jailed process, whatever the kernel's Landlock version.

Landlock refuses TCP connections only from ABI 4 (Linux 6.7). Locally that
mattered little: Compose gives the OCR container no route out. On Azure
Container Apps the kernel offered ABI 3 and there is no network-less
container, so a compromised Tesseract could have connected to the internet
(phase 11b live run). This adds a seccomp filter, applied after Landlock and
before exec: creating any socket other than a local (Unix) one fails with
"Permission denied", as does io_uring (which can create sockets on its own).
Seccomp filters are inherited and can't be removed, and need no privileges
beyond no_new_privs, which Landlock has already set.

Plain ctypes and a hand-written BPF program, like landlock.py: no package."""
import ctypes
import errno
import os
import platform
import struct

PR_SET_NO_NEW_PRIVS = 38
PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2

# Classic BPF over struct seccomp_data { u32 nr; u32 arch; u64 ip; u64 args[6]; }.
LD_W_ABS = 0x20         # BPF_LD | BPF_W | BPF_ABS
JEQ_K = 0x15            # BPF_JMP | BPF_JEQ | BPF_K
JGE_K = 0x35            # BPF_JMP | BPF_JGE | BPF_K
RET_K = 0x06            # BPF_RET | BPF_K
ALLOW = 0x7FFF0000      # SECCOMP_RET_ALLOW
DENY = 0x00050000 | errno.EACCES   # SECCOMP_RET_ERRNO: the call fails with "Permission denied"
AF_UNIX = 1

# Per architecture: (AUDIT_ARCH value, socket, socketpair, io_uring_setup, x32 bit or 0).
ARCHES = {
    "x86_64": (0xC000003E, 41, 53, 425, 0x40000000),
    "aarch64": (0xC00000B7, 198, 199, 425, 0),
}


class NetDenyUnavailable(OSError):
    pass


def _ins(code: int, jt: int, jf: int, k: int) -> bytes:
    return struct.pack("HBBI", code, jt, jf, k)


def program(machine: str | None = None) -> bytes:
    machine = machine or platform.machine()
    if machine not in ARCHES:
        raise NetDenyUnavailable(0, f"no network filter for {machine}")
    arch, socket_nr, socketpair_nr, uring_nr, x32 = ARCHES[machine]
    p = [
        _ins(LD_W_ABS, 0, 0, 4),                 # 0: A = arch
        _ins(JEQ_K, 1, 0, arch),                 # 1: our architecture? skip the deny
        _ins(RET_K, 0, 0, DENY),                 # 2: a foreign ABI: deny everything
        _ins(LD_W_ABS, 0, 0, 0),                 # 3: A = syscall number
    ]
    if x32:
        p += [_ins(JGE_K, 0, 1, x32),            # 4: an x32 call (same arch, other numbers)
              _ins(RET_K, 0, 0, DENY)]           # 5:   denied
    rest = [
        _ins(JEQ_K, 3, 0, socket_nr),            # socket       -> check the domain
        _ins(JEQ_K, 2, 0, socketpair_nr),        # socketpair   -> check the domain
        _ins(JEQ_K, 4, 0, uring_nr),             # io_uring     -> deny
        _ins(RET_K, 0, 0, ALLOW),                # anything else
        _ins(LD_W_ABS, 0, 0, 16),                # A = args[0] (low 32 bits): the address family
        _ins(JEQ_K, 0, 1, AF_UNIX),              # Unix sockets stay allowed (local only)
        _ins(RET_K, 0, 0, ALLOW),
        _ins(RET_K, 0, 0, DENY),                 # every other family: inet, inet6, packet, netlink...
    ]
    return b"".join(p + rest)


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]


def apply() -> None:
    """Installs the filter on this process; everything it execs inherits it. Raises if it can't."""
    code = program()
    buf = ctypes.create_string_buffer(code, len(code))
    prog = _SockFprog(len(code) // 8, ctypes.cast(buf, ctypes.c_void_p))
    libc = ctypes.CDLL(None, use_errno=True)
    for args, what in (((PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0), "no_new_privs"),
                       ((PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(prog), 0, 0), "seccomp filter")):
        if libc.prctl(*args) != 0:
            err = ctypes.get_errno()
            raise NetDenyUnavailable(err, f"{what}: {os.strerror(err)}")
