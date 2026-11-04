"""Phase 11: wakes the services a run needs before it starts.

On Azure every service scales to zero when idle, and the first request to a
sleeping one waits while it starts (10 to 40 seconds). The service clients'
timeouts are seconds, sized for a stack that is already running, so a run
that met a sleeping control plane or ledger would fail on its first call.
Instead, when WAKE_DEPENDENCIES is set, a run first asks every dependency's
health endpoint, all at once, with a long timeout. Once one run has done so,
runs in the next couple of minutes skip it: the services are awake and stay
up for five minutes after their last request."""
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import config

log = logging.getLogger("warden")

ENABLED = os.environ.get("WAKE_DEPENDENCIES", "").lower() in ("1", "true", "yes")
TIMEOUT = float(os.environ.get("WAKE_TIMEOUT_SECONDS", "120"))
FRESH_FOR = 120.0
_last: float | None = None   # never: the monotonic clock starts near zero at boot
_lock = threading.Lock()


def targets() -> dict[str, str]:
    return {"ledger": f"{config.LEDGER_URL}/health", "policy": f"{config.POLICY_URL}/health",
            "control": f"{config.CONTROL_URL}/health", "pii": f"{config.PII_URL}/health",
            "ocr": f"{config.OCR_URL}/health",
            "gateway": f"{config.LITELLM_URL}/health/liveliness"}


def wake(get=None) -> dict[str, str]:
    """Health-checks every dependency in parallel; returns name -> 'up' or the error. Never raises:
    a dependency that is really down fails the run at its own step, with its own message."""
    global _last
    if not ENABLED and get is None:
        return {}
    with _lock:
        if _last is not None and time.monotonic() - _last < FRESH_FOR:
            return {}
        get = get or (lambda url: httpx.get(url, timeout=TIMEOUT).raise_for_status())
        started = time.monotonic()

        def ask(item):
            name, url = item
            try:
                get(url)
                return name, "up"
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                return name, f"{type(exc).__name__}: {exc}"

        with ThreadPoolExecutor(max_workers=len(targets())) as pool:
            result = dict(pool.map(ask, targets().items()))
        if all(v == "up" for v in result.values()):
            _last = time.monotonic()
        log.info("woke dependencies in %.1fs: %s", time.monotonic() - started, result)
        return result
