#!/usr/bin/env python3
"""A stand-in for the spire-server CLI, for the bootstrap test: the subcommands bootstrap.py uses,
with entries kept in a JSON file in the shape `entry show -output json` prints (SPIRE 1.15)."""
import json
import os
import sys
import uuid

state_path = os.environ["FAKE_SPIRE_STATE"]
state = json.load(open(state_path)) if os.path.exists(state_path) else {"entries": [], "healthy_after": 0, "calls": 0}
args = sys.argv[1:]
state["calls"] += 1


def flag(name, many=False):
    values = [args[i + 1] for i, a in enumerate(args) if a == name]
    return values if many else (values[0] if values else None)


def sid(text):
    td, path = text.removeprefix("spiffe://").split("/", 1)
    return {"trust_domain": td, "path": "/" + path}


def entry_from_args(entry_id):
    return {"id": entry_id, "spiffe_id": sid(flag("-spiffeID")), "parent_id": sid(flag("-parentID")),
            "selectors": [dict(zip(("type", "value"), s.split(":", 1))) for s in flag("-selector", True)]}


code = 0
cmd = " ".join(args[:2])
if args[0] == "healthcheck":
    code = 0 if state["calls"] > state["healthy_after"] else 1
elif cmd == "bundle show":
    print("-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----")
elif cmd == "token generate":
    print(f"Token: {uuid.uuid4()}")
elif cmd == "entry show":
    print(json.dumps({"entries": state["entries"], "next_page_token": ""}))
elif cmd == "entry create":
    if any(e["id"] == flag("-entryID") for e in state["entries"]):
        print("Error: entry already exists", file=sys.stderr)
        code = 1
    else:
        state["entries"].append(entry_from_args(flag("-entryID")))
elif cmd == "entry update":
    state["entries"] = [entry_from_args(e["id"]) if e["id"] == flag("-entryID") else e for e in state["entries"]]
elif cmd == "entry delete":
    state["entries"] = [e for e in state["entries"] if e["id"] != flag("-entryID")]
else:
    print(f"unknown command {args}", file=sys.stderr)
    code = 2
json.dump(state, open(state_path, "w"))
sys.exit(code)
