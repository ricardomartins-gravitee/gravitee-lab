import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

import requests

TYPES = ("A2A_PROXY", "LLM_PROXY", "MCP_PROXY")

def write(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")

def seed(*args):
    subprocess.run([sys.executable, "seeder/seed.py", *args], check=True)

def group(kind, phase):
    return {"name": f"SPG proof {kind} {phase}", "crossId": str(uuid.uuid4()),
            "apiType": kind, "phase": phase, "steps": []}

def probe_proxy_types(manager):
    groups_url = f"{manager.v2}/shared-policy-groups"
    unexpected = []
    for kind in TYPES:
        for phase in ("REQUEST", "RESPONSE"):
            result = manager.api.post(groups_url, json=group(kind, phase), expect=(201, 404))
            if result.status_code == 201:
                manager.api.delete(f"{groups_url}/{result.json()['id']}", expect=(204,))
                unexpected.append((kind, phase))
                continue
            assert result.json().get("technicalCode") == "sharedPolicyGroup.invalidPhase"
            print(f"{kind} {phase}: Management API returned 404 sharedPolicyGroup.invalidPhase", flush=True)
    assert not unexpected, f"Unexpectedly accepted types and phases: {unexpected}"

def add_cases(root):
    flow = {"name": "Shared policy proof", "enabled": True,
            "selectors": [{"type": "HTTP", "path": "/", "pathOperator": "STARTS_WITH"}]}
    groups = root / "apim/shared-policy-groups"
    for phase in ("REQUEST", "RESPONSE"):
        definition = group("PROXY", phase)
        definition["steps"] = [{"name": definition["name"], "policy": "transform-headers",
            "configuration": {"addHeaders": [{"name": f"X-SPG-Proof-{phase.title()}", "value": "yes"}]}}]
        write(groups / f"proxy-{phase.lower()}.json", definition)
        flow[phase.lower()] = [{"name": definition["name"], "enabled": True, "policy": "shared-policy-group-policy",
                               "configuration": {"sharedPolicyGroupCrossId": definition["crossId"]}}]
    write(groups / "message-publish.json", group("MESSAGE", "PUBLISH"))
    api = {"id": str(uuid.uuid4()), "name": "SPG proof API", "definitionVersion": "V4",
           "apiVersion": "1.0", "description": "Disposable shared policy proof", "type": "PROXY",
           "listeners": [{"type": "HTTP", "paths": [{"path": "/spg-proof/"}],
                          "entrypoints": [{"type": "http-proxy", "configuration": {}}]}],
           "endpointGroups": [{"name": "default", "type": "http-proxy", "loadBalancer": {"type": "ROUND_ROBIN"},
                               "endpoints": [{"name": "fixture", "type": "http-proxy", "weight": 1,
                                              "configuration": {"target": "http://fixture:8080"}}]}],
           "flows": [flow]}
    plan = {"id": str(uuid.uuid4()), "name": "Proof access", "description": "Keyless proof",
            "definitionVersion": "V4", "validation": "AUTO", "mode": "STANDARD", "status": "PUBLISHED",
            "flows": [], "security": {"type": "KEY_LESS"}}
    write(root / "apim/apis/proof.json", {"api": api, "plans": [plan]})

def gateway_proof():
    for _ in range(24):
        reply = requests.get("http://127.0.0.1:8082/spg-proof/", timeout=8)
        if reply.status_code not in (404, 503):
            break
        time.sleep(5)
    assert reply.status_code == 200, f"PROXY gateway returned {reply.status_code}"
    assert reply.text == "shared request policy ran"
    assert reply.headers.get("X-SPG-Proof-Response") == "yes", "Shared response policy did not run"
    print("PROXY gateway executed both the shared request and response groups", flush=True)

def validate(root):
    os.environ.update(SEED_DIR=str(root / "seed"), STATE_DIR=str(root / "state"),
                      APIM_MANAGEMENT_URL="http://127.0.0.1:8083", SEED_WORKERS="1",
                      APIM_USERNAME="admin", APIM_PASSWORD="admin")
    from lab_seeder.apim import ApiManagement
    from lab_seeder.common import created

    manager = ApiManagement()
    manager.connect()
    probe_proxy_types(manager)
    add_cases(root / "seed")
    records = []
    try:
        seed()
        records = created()
        groups = [row for row in records if row["kind"] == "shared-policy-group"]
        apis = [row for row in records if row["kind"] == "api"]
        assert len(groups) == 3 and len(apis) == 1, "Expected three policy groups and one API"
        for row in groups:
            live = manager.api.get(f"{manager.v2}/shared-policy-groups/{row['id']}").json()
            assert live["lifecycleState"] == "DEPLOYED", f"{row['name']} is not deployed"
            assert live["crossId"] == row["crossId"], f"{row['name']} has a different crossId"
        print("PROXY request/response and MESSAGE publish groups deployed", flush=True)
        gateway_proof()
        before = {(row["kind"], row["id"], row.get("digest")) for row in records}
        seed()
        after = {(row["kind"], row["id"], row.get("digest")) for row in created()}
        assert before == after, "Reseeding changed managed identities or digests"
        gateway_proof()
        print("Reseed retained API and group identities and digests", flush=True)
    finally:
        records = created() or records
        seed("--remove")
        assert not created(), "Test seed left records after removal"
        for row in records:
            family = {"shared-policy-group": "shared-policy-groups", "api": "apis"}.get(row["kind"])
            if family and row.get("id"):
                manager.api.get(f"{manager.v2}/{family}/{row['id']}", expect=(404,))
        print("All recorded test APIs and shared policy groups are absent", flush=True)

with tempfile.TemporaryDirectory(prefix="spg-proof-") as sandbox:
    validate(Path(sandbox))
