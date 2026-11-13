"""Transfer a bounded playground snapshot to a new, empty management API.

Export and verification read the source only. Import refuses an occupied destination
and uses the existing checkpoint adapters, keeping keys and resource identities while
mapping application members to the destination's configured identity providers.
"""

import copy
import json
import sys
import traceback
import uuid
from pathlib import Path
from urllib.parse import quote

from . import checkpoints, common
from .common import HttpError, SeedError


def _users(resources, saved):
    users = {}
    for app in saved["consumers"]["applications"]:
        if not app["managed"]:
            raise SeedError("A consumer application is shared outside this playground; clone needs an exclusive application")
        if app["entity"].get("groups"):
            raise SeedError("A consumer application uses shared groups; its memberships cannot be cloned safely")
        target = resources.at(app["record"])
        ids = {member["id"] for member in app.get("members", [])}
        if (app["entity"].get("owner") or {}).get("id"):
            ids.add(app["entity"]["owner"]["id"])
        for uid in ids:
            body = target.api.get(f"{target.org_v1}/users/{quote(uid, safe='')}").json()
            if not all(isinstance(body.get(key), str) and body[key] for key in ("id", "source", "sourceId")):
                raise SeedError("An application member has no portable identity")
            users[uid] = {key: body[key] for key in ("id", "source", "sourceId", "email", "firstname", "lastname") if key in body}
    return sorted(users.values(), key=lambda user: user["id"])


def _capture(resources, rows, foreign):
    saved = resources.capture(rows, foreign)
    resources.validate(saved, rows, foreign)
    saved["users"] = _users(resources, saved)
    return saved


def _comparable(snapshot):
    saved = copy.deepcopy(snapshot)
    for item in saved["resources"]:
        # APIM stamps each export with the time of the request. It is not an API edit.
        metadata = item.get("definition", {}).get("export")
        if isinstance(metadata, dict):
            metadata.pop("date", None)
    return saved


def _difference(a, b, path="snapshot"):
    """Name a changing structural field, never its potentially secret value."""
    if isinstance(a, dict) and isinstance(b, dict):
        if a.keys() != b.keys():
            return path + ".fields"
        return next((found for key in a for found in [_difference(a[key], b[key], path + "." + key)] if found), "")
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return path + ".length"
        return next((found for i, (left, right) in enumerate(zip(a, b))
                     for found in [_difference(left, right, path + "." + str(i))] if found), "")
    return path if a != b else ""


def _member(target, spec):
    found = target.find_user(spec["source"], spec["sourceId"])
    if found:
        return found["id"]
    fields = {key: spec[key] for key in ("source", "sourceId", "email", "firstname", "lastname") if key in spec}
    if fields.get("email"):
        answer = target.api.post(f"{target.v1}/users", json=fields, expect=(200, 201, 404))
        if answer.status_code != 404:
            return answer.json()["id"]
    # Memory-provider users have no registration endpoint. Adding their provider
    # reference to a temporary group materializes just this referenced identity.
    before = common._read()
    group = target.api.post(f"{target.v1}/configuration/groups", json={"name": "Runtime clone " + uuid.uuid4().hex}).json()
    try:
        target.waiting[(spec["source"], spec["sourceId"])] = fields
        target.enroll(group["id"], "Runtime clone", {**fields, "roles": {"APPLICATION": "USER"}})
        user = target.appears(spec["source"], spec["sourceId"])
        if not user:
            raise SeedError("The clone could not register a referenced application member")
        return user["id"]
    finally:
        target.api.delete(f"{target.v1}/configuration/groups/{group['id']}")
        common.save_created(before)


def _import(resources, saved):
    if common._read() or resources.management.all_apis():
        raise SeedError("Clone import requires a new, empty runtime; nothing was overwritten")
    saved = copy.deepcopy(saved)
    mapping = {user["id"]: _member(resources.management, user) for user in saved.get("users", [])}
    for app in saved["consumers"]["applications"]:
        owner = app["entity"].get("owner") or {}
        if owner.get("id"):
            owner["id"] = mapping[owner["id"]]
        for member in app.get("members", []):
            member["id"] = mapping[member["id"]]
    saved["target"] = resources.target()
    previous = resources.capture([], [])
    resources.apply(saved, previous=previous)
    # Seed ownership stays distinct from manually created consumer applications.
    common.save_created(copy.deepcopy(resources.consumers.owned(saved)))


def run(operation, file):
    common.configure(workers=1)
    path = Path(file)
    resources = checkpoints.Resources()
    if operation == "import":
        _import(resources, json.loads(path.read_text(encoding="utf-8")))
        common.log("clone", "Imported only the playground's resources and consumer access")
        return
    rows = checkpoints._records(common._read())
    foreign = json.loads((path.parent / "foreign.json").read_text(encoding="utf-8"))
    saved = _capture(resources, rows, foreign)
    if operation == "export":
        checkpoints._write(path, saved)
    elif operation == "verify":
        changed = _difference(_comparable(saved), _comparable(json.loads(path.read_text(encoding="utf-8"))))
        if changed:
            raise SeedError("The playground changed during cloning; retry with a new runtime (" + changed + ")")
    else:
        raise SeedError("Unknown runtime clone operation")


def main():
    file = Path(sys.argv[-1])
    result = {"ok": True}
    try:
        with common.held():
            run(*sys.argv[1:])
    except Exception as error:  # noqa: BLE001 - management response bodies can carry credentials
        checkpoints._write(file.parent / "failure.json", {"frames": [frame.name for frame in traceback.extract_tb(error.__traceback__)],
                           "response": error.body if isinstance(error, HttpError) else type(error).__name__})
        message = ("The management API refused the clone operation (HTTP " + str(error.status) + ")" if isinstance(error, HttpError)
                   else str(error) if isinstance(error, SeedError) else type(error).__name__ + ": runtime clone failed")
        result = {"ok": False, "error": message}
    checkpoints._write(file.parent / "result.json", result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
