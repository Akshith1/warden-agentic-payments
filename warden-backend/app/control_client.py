"""Client for warden-control: the kill switch and the audit log (phase 9)."""
import httpx

from . import config


class KillSwitchOn(RuntimeError):
    """Raised where a payment would otherwise move. The run fails with the reason and
    can be retried after a controller resets the switch; finished steps aren't repeated."""


class ControlClient:
    def __init__(self, base_url: str | None = None, timeout: float = 5.0):
        self._http = httpx.Client(base_url=base_url or config.CONTROL_URL, timeout=timeout)

    def status(self) -> dict:
        r = self._http.get("/status")
        r.raise_for_status()
        return r.json()

    def ensure_running(self, agent: str) -> None:
        """Fails closed: if the control plane can't answer, nothing moves."""
        try:
            s = self.status()
        except httpx.HTTPError as e:
            raise KillSwitchOn("kill switch status unknown (control plane unreachable)") from e
        if s["global_stop"]:
            raise KillSwitchOn(f"kill switch is on: {s['global']['reason']} (by {s['global']['by']})")
        if agent in s["paused_agents"]:
            raise KillSwitchOn(f"the {agent} agent is paused by the kill switch")

    def record(self, type_: str, payload: dict, run_id: str | None = None, actor: str | None = None) -> dict:
        r = self._http.post("/events", json={"type": type_, "payload": payload, "run_id": run_id, "actor": actor})
        r.raise_for_status()
        return r.json()
