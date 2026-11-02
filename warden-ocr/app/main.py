"""warden-ocr: reads text from an invoice image, inside a sandbox.

The container itself has no route to the internet (an internal Docker
network), a read-only root filesystem, no Linux capabilities and a non-root
user. Each image is then read by a separate Tesseract process that the kernel
restricts with Landlock to its own job folder and the system libraries, with
no network and hard limits on CPU time and memory. If Landlock is not
available, the service refuses to read images rather than reading them
unsandboxed."""
import base64
import os
import shutil
import subprocess
import sys
import tempfile
import time

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import landlock

WORK_DIR = os.environ.get("OCR_WORK_DIR", "/work")
MAX_BYTES = int(os.environ.get("OCR_MAX_BYTES", str(5 * 2**20)))
TIMEOUT_SECONDS = 30
SIGNATURES = {b"\x89PNG\r\n\x1a\n": "png", b"\xff\xd8\xff": "jpg", b"II*\x00": "tif", b"MM\x00*": "tif"}

app = FastAPI(title="warden-ocr")


class OcrRequest(BaseModel):
    image_base64: str = Field(min_length=1, max_length=8 * 2**20)


def image_kind(data: bytes) -> str | None:
    return next((kind for sig, kind in SIGNATURES.items() if data.startswith(sig)), None)


@app.get("/health")
def health():
    abi = landlock.abi_version()
    return {"status": "ok" if abi else "unsandboxed", "landlock_abi": abi, "uid": os.getuid()}


@app.post("/ocr")
def ocr(req: OcrRequest):
    abi = landlock.abi_version()
    if not abi:
        raise HTTPException(status_code=503, detail="Landlock is not available on this kernel; "
                                                    "refusing to read images unsandboxed")
    try:
        data = base64.b64decode(req.image_base64, validate=True)
    except ValueError as e:
        raise HTTPException(status_code=422, detail="image_base64 is not valid base64") from e
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"image larger than {MAX_BYTES // 2**20} MB")
    kind = image_kind(data)
    if not kind:
        raise HTTPException(status_code=415, detail="only PNG, JPEG and TIFF images are accepted")

    job = tempfile.mkdtemp(prefix="job-", dir=WORK_DIR)
    try:
        path = os.path.join(job, f"invoice.{kind}")
        with open(path, "wb") as f:
            f.write(data)
        started = time.monotonic()
        try:
            proc = subprocess.run([sys.executable, "-m", "app.jail", job, "--", "tesseract", path, "stdout"],
                                  capture_output=True, timeout=TIMEOUT_SECONDS, cwd=os.path.dirname(__file__) + "/..")
        except subprocess.TimeoutExpired as e:
            raise HTTPException(status_code=422, detail=f"OCR took longer than {TIMEOUT_SECONDS} s") from e
        if proc.returncode != 0:
            err = proc.stderr.decode(errors="replace").strip().splitlines()[-1:] or ["no output"]
            raise HTTPException(status_code=422, detail=f"OCR failed (exit {proc.returncode}): {err[0][:300]}")
        text = proc.stdout.decode(errors="replace").strip()
        return {"text": text, "chars": len(text), "image_type": kind, "bytes": len(data),
                "seconds": round(time.monotonic() - started, 2),
                "sandbox": {"landlock_abi": abi, "network": "none", "writes": "none", "uid": os.getuid()}}
    finally:
        shutil.rmtree(job, ignore_errors=True)
