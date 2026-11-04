"""Runs one command inside the sandbox:  python -m app.jail <job_dir> -- <command...>

Order matters: resource limits first, then Landlock, then the network filter,
then exec. After exec the command can read only the system directories it
needs and its own job folder, can write nowhere, and can open no network
connection: Landlock refuses TCP on ABI 4 and later, and the seccomp filter in
netdeny.py refuses every non-local socket on any kernel (phase 11b: Azure's
offered ABI 3). If either can't be applied, the command doesn't run."""
import os
import resource
import sys

from . import landlock, netdeny

SYSTEM_READ_EXEC = ["/usr", "/lib", "/lib64", "/bin", "/sbin"]
SYSTEM_READ = ["/etc", "/proc/self"]  # dynamic linker cache, locale; /proc/self for the runtime


def limits():
    resource.setrlimit(resource.RLIMIT_CPU, (20, 20))                      # seconds of CPU
    resource.setrlimit(resource.RLIMIT_AS, (1024 * 2**20, 1024 * 2**20))   # 1 GiB of memory
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))                      # may not create file data
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def main(argv: list[str]) -> None:
    if "--" not in argv or argv.index("--") != 2:
        sys.exit("usage: python -m app.jail <job_dir> -- <command...>")
    job_dir, command = argv[1], argv[3:]
    limits()
    landlock.restrict(read=SYSTEM_READ + [job_dir], read_exec=SYSTEM_READ_EXEC, write=[])
    netdeny.apply()
    os.execvpe(command[0], command, {"PATH": "/usr/local/bin:/usr/bin:/bin", "OMP_THREAD_LIMIT": "1",
                                     "LANG": "C.UTF-8"})


if __name__ == "__main__":
    main(sys.argv)
