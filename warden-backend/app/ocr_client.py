"""Client for warden-ocr, the sandboxed OCR service."""
import httpx

from . import config


class Unreadable(Exception):
    """The image itself can't be read (not an image, too large, OCR failed). Retrying won't help."""


class OcrClient:
    def __init__(self, base_url: str | None = None, timeout: float = 60.0):
        self._http = httpx.Client(base_url=base_url or config.OCR_URL, timeout=timeout)

    def read(self, image_base64: str) -> dict:
        r = self._http.post("/ocr", json={"image_base64": image_base64})
        if r.status_code in (413, 415, 422):
            raise Unreadable(r.json().get("detail", r.text))
        r.raise_for_status()  # 503 (no sandbox) or connection errors: the run fails and can be retried
        return r.json()
