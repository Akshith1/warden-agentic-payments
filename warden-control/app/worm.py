"""Phase 11b: a write-once copy of the audit log, in Azure Blob storage.

The hash chain makes an edit to the audit log evident, but only until someone
with the database rewrites the entries and computes the chain again from the
edit onwards: then the chain verifies, and nothing shows. So every entry is
also copied, a few seconds after it is written, to an append blob in a
container with a time-based immutability policy: blobs there can be added to
but not changed, overwritten or deleted, by anyone, the control plane
included, until the retention period ends.

compare() then reads the copy back and checks the database against it entry
by entry: a rewritten history disagrees with the copy at the first changed
entry, however carefully the chain was recomputed.

Blobs are named audit-YYYYMMDD.jsonl (the day of copying), one JSON line per
entry. The control plane authenticates with its own managed identity; it has
no storage key. Where the copy got to is read from the copy itself, never from
the database being checked.

Off unless AUDIT_WORM_URL names the container
(https://<account>.blob.core.windows.net/<container>)."""
import json
import logging
import os
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import httpx

log = logging.getLogger("warden.worm")

URL = os.environ.get("AUDIT_WORM_URL", "").rstrip("/")
INTERVAL = float(os.environ.get("AUDIT_WORM_INTERVAL_SECONDS", "5"))
BATCH = 500
API_VERSION = "2023-11-03"
FIELDS = ("seq", "at", "type", "run_id", "actor", "payload", "prev_hash", "hash")


class ManagedIdentityToken:
    """A token for Azure Storage from Container Apps' managed identity endpoint, reused until
    a minute before it expires."""

    def __init__(self, client_id: str | None = None, http=None):
        self.client_id = client_id if client_id is not None else os.environ.get("AZURE_CLIENT_ID", "")
        self.http = http or httpx
        self._token, self._expiry = None, 0.0

    def __call__(self) -> str:
        if self._token is None or time.time() > self._expiry - 60:
            params = {"resource": "https://storage.azure.com/", "api-version": "2019-08-01"}
            if self.client_id:
                params["client_id"] = self.client_id
            r = self.http.get(os.environ["IDENTITY_ENDPOINT"], params=params,
                              headers={"X-IDENTITY-HEADER": os.environ["IDENTITY_HEADER"]}, timeout=10)
            r.raise_for_status()
            body = r.json()
            self._token, self._expiry = body["access_token"], float(body["expires_on"])
        return self._token


class Container:
    """The few Blob REST calls the copy needs: list, read, create an append blob, append."""

    def __init__(self, url: str, token, http: httpx.Client | None = None):
        self.url, self.token = url.rstrip("/"), token
        self.http = http or httpx.Client(timeout=30)

    def _headers(self, extra: dict | None = None) -> dict:
        return {"Authorization": f"Bearer {self.token()}", "x-ms-version": API_VERSION,
                "x-ms-date": datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")} | (extra or {})

    def names(self, prefix: str = "audit-") -> list[str]:
        r = self.http.get(self.url, params={"restype": "container", "comp": "list", "prefix": prefix},
                          headers=self._headers())
        r.raise_for_status()
        return sorted(n.text for n in ET.fromstring(r.content).iter("Name"))

    def read(self, name: str) -> bytes:
        r = self.http.get(f"{self.url}/{name}", headers=self._headers())
        r.raise_for_status()
        return r.content

    def create(self, name: str) -> None:
        """A new, empty append blob; one that already exists is left alone (it can't be replaced)."""
        r = self.http.put(f"{self.url}/{name}", headers=self._headers(
            {"x-ms-blob-type": "AppendBlob", "If-None-Match": "*", "Content-Length": "0"}))
        if r.status_code not in (201, 409, 412):
            r.raise_for_status()

    def append(self, name: str, data: bytes, at: int) -> None:
        """Adds data at offset `at`; refused if the blob has grown since, so a retry never doubles a line."""
        r = self.http.put(f"{self.url}/{name}", params={"comp": "appendblock"}, content=data,
                          headers=self._headers({"x-ms-blob-condition-appendpos": str(at)}))
        r.raise_for_status()


def _lines(data: bytes) -> list[dict]:
    return [json.loads(line) for line in data.decode().splitlines() if line.strip()]


class Copier:
    def __init__(self, store, container: Container, clock=lambda: datetime.now(timezone.utc)):
        self.store, self.container, self.clock = store, container, clock
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.last_error: str | None = None
        self._state: tuple[int, str | None, int] | None = None

    def _last_copied(self) -> tuple[int, str | None, int]:
        """(last seq in the copy, the newest blob's name, its size), read from the copy itself, never
        from the database being checked. Remembered after each append; read again after any failure."""
        if self._state is not None:
            return self._state
        names = self.container.names()
        if not names:
            return 0, None, 0
        newest = self.container.read(names[-1])
        for name in reversed(names):
            entries = _lines(newest if name == names[-1] else self.container.read(name))
            if entries:
                return entries[-1]["seq"], names[-1], len(newest)
        return 0, names[-1], len(newest)

    def copy_pending(self) -> int:
        """Copies every entry after the last one in the copy (up to BATCH); returns how many."""
        with self.lock:
            try:
                last, newest, size = self._last_copied()
                rows = self.store.db.execute("SELECT * FROM audit WHERE seq > ? ORDER BY seq LIMIT ?",
                                             (last, BATCH)).fetchall()
                if not rows:
                    self._state = (last, newest, size)
                    return 0
                name = self.clock().strftime("audit-%Y%m%d.jsonl")
                if name != newest:
                    self.container.create(name)
                    size = 0
                data = "".join(json.dumps({k: row[k] for k in FIELDS}, separators=(",", ":")) + "\n"
                               for row in rows).encode()
                self.container.append(name, data, size)
                self._state = (rows[-1]["seq"], name, size + len(data))
                return len(rows)
            except Exception:
                self._state = None
                raise

    def compare(self) -> dict:
        """The database checked against the write-once copy, entry by entry."""
        copied: list[dict] = []
        for name in self.container.names():
            copied += _lines(self.container.read(name))
        db = {r["seq"]: {k: r[k] for k in FIELDS} for r in self.store.db.execute("SELECT * FROM audit ORDER BY seq")}
        result = {"copied_through": copied[-1]["seq"] if copied else 0, "database_through": max(db, default=0),
                  "copies": len(copied), "matches": True, "first_difference": None, "problem": None}
        for entry in copied:
            mine = db.get(entry["seq"])
            if mine is None:
                problem = f"entry {entry['seq']} is in the write-once copy but no longer in the database"
            elif mine != entry:
                changed = [k for k in FIELDS if mine[k] != entry[k]]
                problem = f"entry {entry['seq']} differs from the write-once copy ({', '.join(changed)})"
            else:
                continue
            return result | {"matches": False, "first_difference": entry["seq"], "problem": problem}
        return result

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                n = self.copy_pending()
                self.last_error = None
                if n == BATCH:
                    continue                      # more waiting: carry on at once
            except Exception as exc:  # noqa: BLE001 - logged and retried; the database stays the record
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("write-once copy failed, will retry: %s", self.last_error)
            self.wake.wait(INTERVAL)
            self.wake.clear()


def start(store) -> tuple["Copier | None", threading.Event]:
    stop = threading.Event()
    if not URL:
        return None, stop
    copier = Copier(store, Container(URL, ManagedIdentityToken()))
    threading.Thread(target=copier.run, args=(stop,), name="audit-worm", daemon=True).start()
    log.info("copying the audit log to %s", URL)
    return copier, stop
