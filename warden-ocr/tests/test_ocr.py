"""Real Tesseract, real Landlock (skipped where the kernel has none)."""
import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import landlock, main

SAMPLES = Path(__file__).resolve().parents[2] / "docs" / "samples"
needs_landlock = pytest.mark.skipif(not landlock.abi_version(), reason="kernel without Landlock")


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "WORK_DIR", str(tmp_path))
    with TestClient(main.app) as client:
        yield client


def send(api, data: bytes):
    return api.post("/ocr", json={"image_base64": base64.b64encode(data).decode()})


@needs_landlock
def test_reads_a_receipt_inside_the_jail(api, tmp_path):
    r = send(api, (SAMPLES / "receipt-clean.png").read_bytes())
    assert r.status_code == 200, r.text
    body = r.json()
    assert "HARBOUR LANE STATIONERS" in body["text"] and "69.80" in body["text"]
    assert body["sandbox"]["landlock_abi"] >= 1 and body["sandbox"]["network"] == "none"
    assert list(tmp_path.iterdir()) == []  # the job folder is removed afterwards


@needs_landlock
def test_hidden_instructions_come_through_as_text(api):
    # OCR doesn't judge content; the guardrail in the backend does.
    text = send(api, (SAMPLES / "receipt-injected.png").read_bytes()).json()["text"]
    assert "ignore all previous" in text


def test_only_images_are_accepted(api):
    assert send(api, b"%PDF-1.7 not an image").status_code == 415
    assert api.post("/ocr", json={"image_base64": "***"}).status_code == 422


def test_size_limit(api, monkeypatch):
    monkeypatch.setattr(main, "MAX_BYTES", 100)
    assert send(api, b"\x89PNG\r\n\x1a\n" + b"0" * 200).status_code == 413


@needs_landlock
def test_a_broken_image_fails_cleanly(api):
    r = send(api, b"\x89PNG\r\n\x1a\n" + b"garbage" * 100)
    assert r.status_code == 422 and "OCR failed" in r.json()["detail"]


def test_without_landlock_it_refuses(api, monkeypatch):
    monkeypatch.setattr(landlock, "abi_version", lambda: 0)
    r = send(api, (SAMPLES / "receipt-clean.png").read_bytes())
    assert r.status_code == 503 and "unsandboxed" in r.json()["detail"]
    assert api.get("/health").json()["status"] == "unsandboxed"


@needs_landlock
def test_the_jail_refuses_writes_reads_and_network(tmp_path):
    import subprocess
    import sys
    probe = (f"import os, socket\n"
             f"for fn in [lambda: open('{tmp_path}/x', 'w'), lambda: open('{__file__}').read(),\n"
             f"           lambda: socket.create_connection(('1.1.1.1', 80), timeout=3)]:\n"
             f"    try:\n        fn(); print('ALLOWED')\n"
             f"    except OSError:\n        print('REFUSED')\n"
             f"print(sorted(os.listdir('{tmp_path}')))\n")
    root = Path(__file__).resolve().parents[1]
    out = subprocess.run([sys.executable, "-m", "app.jail", str(tmp_path), "--", sys.executable, "-c", probe],
                         capture_output=True, text=True, cwd=root)
    assert out.stdout.split() == ["REFUSED", "REFUSED", "REFUSED", "[]"], out.stderr
