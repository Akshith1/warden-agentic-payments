"""Scenario 11: shows what the sandbox refuses.   python -m app.selftest

Runs a small probe through the same jail Tesseract runs in and prints what it
could and could not do. Run it inside the container to see the container's
own limits too (non-root, no internet, read-only root)."""
import os
import subprocess
import sys
import tempfile

from . import landlock

PROBE = r"""
import os, socket
def attempt(label, fn):
    try:
        fn(); print(f"  {label:<42} ALLOWED")
    except Exception as e:
        print(f"  {label:<42} REFUSED ({type(e).__name__}: {e})")
attempt("read its own job folder", lambda: os.listdir(JOB))
attempt("write a file in its job folder", lambda: open(os.path.join(JOB, "x"), "w"))
attempt("write a file in /tmp", lambda: open("/tmp/pwned.txt", "w"))
attempt("read the OCR service's code", lambda: open(APP).read())
attempt("connect to 1.1.1.1:80", lambda: socket.create_connection(("1.1.1.1", 80), timeout=3))
attempt("connect to warden-ledger:8000", lambda: socket.create_connection(("warden-ledger", 8000), timeout=3))
"""


def main() -> int:
    abi = landlock.abi_version()
    print(f"Landlock ABI on this kernel: {abi or 'not available'}")
    print(f"Running as uid {os.getuid()} ({'root' if os.getuid() == 0 else 'not root'})")
    try:
        open("/srv/root-fs-check", "w")
        print("Container root filesystem: writable")
    except OSError as e:
        print(f"Container root filesystem: not writable by this user ({e.strerror})")
    if not abi:
        print("The jail can't be applied here, so the service refuses to read images.")
        return 1
    job = tempfile.mkdtemp(prefix="selftest-", dir=os.environ.get("OCR_WORK_DIR", "/work"))
    app_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
    print("Inside the jail (what Tesseract can do):")
    probe = f"JOB = {job!r}\nAPP = {app_file!r}\n" + PROBE
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = subprocess.run([sys.executable, "-m", "app.jail", job, "--", sys.executable, "-c", probe],
                         capture_output=True, text=True, cwd=root)
    print(out.stdout.rstrip() or out.stderr.rstrip())
    os.rmdir(job)
    return 0


if __name__ == "__main__":
    sys.exit(main())
