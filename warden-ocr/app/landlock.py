"""A minimal Landlock binding (Linux 5.13+), with no third-party package.

Landlock lets a process give up access to the filesystem and network for
itself and every program it then runs. The kernel enforces it, so it holds
even if the program it runs (here, Tesseract decoding an untrusted image) is
compromised. Only paths granted below stay reachable; everything else gets
"Permission denied". TCP connect and bind are denied too on ABI 4 and later.

Reference: https://docs.kernel.org/userspace-api/landlock.html"""
import ctypes
import os

SYS_CREATE_RULESET, SYS_ADD_RULE, SYS_RESTRICT_SELF = 444, 445, 446  # same on x86-64 and arm64
CREATE_RULESET_VERSION = 1
RULE_PATH_BENEATH = 1
PR_SET_NO_NEW_PRIVS = 38

EXECUTE, WRITE_FILE, READ_FILE, READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
ALL_V1 = (1 << 13) - 1                      # execute .. make_sym
REFER, TRUNCATE, IOCTL_DEV = 1 << 13, 1 << 14, 1 << 15
NET_BIND_TCP, NET_CONNECT_TCP = 1 << 0, 1 << 1
SCOPE_ABSTRACT_UNIX_SOCKET, SCOPE_SIGNAL = 1 << 0, 1 << 1

READ = READ_FILE | READ_DIR
READ_EXEC = READ | EXECUTE

_libc = ctypes.CDLL(None, use_errno=True)


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64),
                ("scoped", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class LandlockUnavailable(OSError):
    pass


def abi_version() -> int:
    """0 when the kernel has no Landlock (or a seccomp profile blocks it)."""
    v = _libc.syscall(SYS_CREATE_RULESET, None, ctypes.c_size_t(0), ctypes.c_uint32(CREATE_RULESET_VERSION))
    return max(v, 0)


def _check(result: int, what: str) -> int:
    if result < 0:
        err = ctypes.get_errno()
        raise LandlockUnavailable(err, f"landlock {what}: {os.strerror(err)}")
    return result


def restrict(read: list[str], read_exec: list[str], write: list[str]) -> int:
    """Restricts this process (and what it execs) to the given directories.
    `write` directories get read and write. Returns the ABI version applied."""
    abi = abi_version()
    if abi < 1:
        raise LandlockUnavailable(0, "Landlock is not available on this kernel")
    fs = ALL_V1 | (REFER if abi >= 2 else 0) | (TRUNCATE if abi >= 3 else 0) | (IOCTL_DEV if abi >= 5 else 0)
    attr = _RulesetAttr(handled_access_fs=fs,
                        handled_access_net=(NET_BIND_TCP | NET_CONNECT_TCP) if abi >= 4 else 0,
                        scoped=(SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL) if abi >= 6 else 0)
    ruleset = _check(_libc.syscall(SYS_CREATE_RULESET, ctypes.byref(attr), ctypes.c_size_t(ctypes.sizeof(attr)),
                                   ctypes.c_uint32(0)), "create_ruleset")
    try:
        grants = [(p, READ) for p in read] + [(p, READ_EXEC) for p in read_exec]
        grants += [(p, fs & ~EXECUTE & ~(IOCTL_DEV if abi >= 5 else 0)) for p in write]
        for path, access in grants:
            if not os.path.exists(path):
                continue
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = _PathBeneathAttr(allowed_access=access & fs, parent_fd=fd)
                _check(_libc.syscall(SYS_ADD_RULE, ruleset, RULE_PATH_BENEATH, ctypes.byref(rule), ctypes.c_uint32(0)),
                       f"add_rule {path}")
            finally:
                os.close(fd)
        _check(_libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0), "no_new_privs")
        _check(_libc.syscall(SYS_RESTRICT_SELF, ruleset, ctypes.c_uint32(0)), "restrict_self")
    finally:
        os.close(ruleset)
    return abi
