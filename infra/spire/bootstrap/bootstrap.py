"""Sets up the SPIRE server for this machine, every time the stack starts (phase 6b).

1. Waits for the server's admin socket.
2. Writes the server's CA bundle, so the agent can trust the server.
3. Writes a fresh one-time join token. The agent keeps its identity in its
   data volume and uses that while it is valid; a join-token identity can't
   renew itself once it has expired (say the stack was off overnight), and
   then the agent joins again with this token. (Checked in SPIRE 1.15.5's
   pkg/agent/attestor/node: a cached SVID is loaded first.)
4. Makes the server's identity entries match identities.json: creates
   missing ones, updates changed ones, deletes ones no longer listed.

The SPIRE images have no shell, so this runs in a small Python image with the
spire-server binary copied in, and talks to the server only through its admin
socket on a shared volume."""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SOCKET = os.environ.get("SPIRE_SERVER_SOCKET", "/run/spire/server/api.sock")
OUT = Path(os.environ.get("SPIRE_BOOTSTRAP_DIR", "/run/spire/bootstrap"))
IDENTITIES = Path(os.environ.get("SPIRE_IDENTITIES", "/etc/spire/identities.json"))
SERVER = os.environ.get("SPIRE_SERVER_BIN", "spire-server")


def spire(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run([SERVER, *args, "-socketPath", SOCKET], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise SystemExit(f"spire-server {args[0]} {args[1] if len(args) > 1 else ''} failed: "
                         f"{(result.stderr or result.stdout).strip()}")
    return result


def wait_for_server(seconds: float = 120) -> None:
    deadline = time.monotonic() + seconds
    while spire("healthcheck", check=False).returncode != 0:
        if time.monotonic() > deadline:
            raise SystemExit("the SPIRE server did not become healthy")
        time.sleep(2)


def entries() -> dict[str, dict]:
    shown = json.loads(spire("entry", "show", "-output", "json").stdout or "{}")
    return {e["id"]: e for e in shown.get("entries") or []}


def selectors(entry: dict) -> set[str]:
    return {f"{s['type']}:{s['value']}" for s in entry.get("selectors") or []}


def spiffe_id(value) -> str:
    if isinstance(value, str):
        return value
    return f"spiffe://{value['trust_domain']}{value['path']}"


def sync_entries(config: dict) -> list[str]:
    done, current = [], entries()
    wanted = {i["entry_id"]: i for i in config["identities"]}
    for entry_id, want in wanted.items():
        args = ["-entryID", entry_id, "-spiffeID", want["spiffe_id"], "-parentID", config["agent_id"]]
        for s in want["selectors"]:
            args += ["-selector", s]
        have = current.get(entry_id)
        if have is None:
            spire("entry", "create", *args)
            done.append(f"created {want['spiffe_id']}")
        elif (spiffe_id(have["spiffe_id"]) != want["spiffe_id"] or selectors(have) != set(want["selectors"])
              or spiffe_id(have["parent_id"]) != config["agent_id"]):
            spire("entry", "update", *args)
            done.append(f"updated {want['spiffe_id']}")
    for entry_id, have in current.items():
        # Leave the server's own node entries alone; only workload entries this file manages go.
        if entry_id not in wanted and spiffe_id(have["parent_id"]) == config["agent_id"]:
            spire("entry", "delete", "-entryID", entry_id)
            done.append(f"deleted {spiffe_id(have['spiffe_id'])}")
    return done


def main() -> None:
    config = json.loads(IDENTITIES.read_text())
    wait_for_server()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "bundle.pem").write_text(spire("bundle", "show", "-format", "pem").stdout)
    out = spire("token", "generate", "-spiffeID", config["agent_id"], "-ttl", "3600").stdout
    match = re.search(r"Token:\s*(\S+)", out)
    if not match:
        raise SystemExit(f"no token in: {out!r}")
    (OUT / "join_token").write_text(match.group(1))
    print("Wrote a one-time join token for the agent (valid for an hour; used only if it must join again).")
    for line in sync_entries(config) or ["identities already match identities.json"]:
        print(line)


if __name__ == "__main__":
    sys.exit(main())
