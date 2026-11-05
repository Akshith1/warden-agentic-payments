#!/bin/sh
# Phase 11: runs deploy.ps1 and teardown.ps1 against stand-ins for az and docker (fakebin/),
# so a mistake in the scripts shows up here rather than half-way through a real deploy.
#   sh infra/azure/tests/dry-run.sh        (needs pwsh; uses a throwaway copy of the repository)
set -eu
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../../.." && pwd)
work=$(mktemp -d)
export FAKE_AZ_DIR="$work/fake"
mkdir -p "$FAKE_AZ_DIR" "$work/repo/infra"
cp -r "$repo/infra/azure" "$work/repo/infra/"
printf 'OPENAI_API_KEY=sk-test\nGROQ_API_KEY=gsk-test\nPGPASS_WARDEN_LEDGER=l\nPGPASS_WARDEN_BACKEND=b\nVENDOR_LOOKUP_KEY=v\nLITELLM_MASTER_KEY=m\nAPPROVAL_SAMPLES=3\n' > "$work/repo/.env"
export PATH="$here/fakebin:$PATH"
cd "$work/repo"

echo "--- first deploy"
pwsh -NoProfile -File infra/azure/deploy.ps1 -Yes
grep -q '"signInClientSecret"' .azure-warden.json
grep -q '"intakeClientSecret"' .azure-warden.json
grep -q '"entryKeyGateway"' .azure-warden.json
test "$(grep -c '^docker push' "$FAKE_AZ_DIR/docker.log")" -eq 11         # 13 builds, the 2 bases stay local
grep -q '^containerapp job start -n warden-data-loader' "$FAKE_AZ_DIR/calls.log"     # the data, loaded once
python3 - "$FAKE_AZ_DIR" <<'PY'
import json, sys
d = sys.argv[1]
state = json.load(open(f"{d}/state.json"))
warden = next(a for a in state["apps"] if a["displayName"] == "Warden")
assert sorted(r["value"] for r in warden["appRoles"]) == ["approver", "controller", "submitter"]
assert warden["web"]["redirectUris"][0].endswith("/api/auth/callback"), warden["web"]
assert len(state["assign"]) == 7, state["assign"]          # me x3, amira x2, clerk x1, the intake app x1
submitter = next(r for r in warden["appRoles"] if r["value"] == "submitter")
assert submitter["allowedMemberTypes"] == ["User", "Application"], submitter
assert all(r["allowedMemberTypes"] == ["User"] for r in warden["appRoles"] if r["value"] != "submitter")
intake = next(a for a in state["apps"] if a["displayName"] == "Warden intake")
assert any(a["principalId"] == "sp-" + intake["appId"][:8] for a in state["assign"])
assert len(state["grants"]) == 2 and len(state["users"]) == 2
apps = json.load(open(f"{d}/params-True.json"))
assert json.loads(apps["agentKeysJson"]["value"]).keys() >= {"extraction", "approval"}
assert apps["approvalSamples"]["value"] == "3"
print("first deploy: directory and parameters as expected")
PY

echo "--- second deploy: nothing new may be created"
: > "$FAKE_AZ_DIR/calls.log"
pwsh -NoProfile -File infra/azure/deploy.ps1 -Yes -SkipImages
if grep -E 'rest --method POST' "$FAKE_AZ_DIR/calls.log"; then echo "a second deploy created something"; exit 1; fi
if grep -q 'job start -n warden-data-loader' "$FAKE_AZ_DIR/calls.log"; then echo "the data was loaded twice"; exit 1; fi

echo "--- teardown"
pwsh -NoProfile -File infra/azure/teardown.ps1 -Yes
test ! -e .azure-warden.json
grep -q '^storage container immutability-policy delete' "$FAKE_AZ_DIR/calls.log"
test "$(grep -n '^storage container immutability-policy delete' "$FAKE_AZ_DIR/calls.log" | cut -d: -f1)" -lt \
     "$(grep -n '^group delete' "$FAKE_AZ_DIR/calls.log" | cut -d: -f1)"     # the policy goes first
grep -q '^group delete -n warden-rg --yes' "$FAKE_AZ_DIR/calls.log"
grep -q '^keyvault purge' "$FAKE_AZ_DIR/calls.log"
grep -q '^apim deletedservice purge' "$FAKE_AZ_DIR/calls.log"
echo "dry run passed"
rm -rf "$work"  # (kept on failure for inspection)
