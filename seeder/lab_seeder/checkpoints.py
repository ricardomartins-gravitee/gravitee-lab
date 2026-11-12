"""Save and restore only a playground's recorded resources through management APIs.

Adapters cover V4 HTTP proxy APIs, their plans and documentation, portal resources, and consumers.
Applications shared outside the playground are references; no shared database or settings are restored.
A failed restore puts back this playground's previous resources, never another playground's data.
"""

import copy
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

from . import apim, apim_portal, common, consumer_states, portal_states
from .common import HttpError, SeedError, created, log, record, save_created, setting

SCHEMA = 2


def _key(row):
    return row.get("product"), row.get("kind"), row.get("env", setting("APIM_ENV", "DEFAULT")), row.get("id")


def _records(rows):
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise SeedError("Saved ownership records are damaged")
    seen = set()
    for row in rows:
        if row.get("kind") not in {apim.SECTION: ("api", "application", "subscription"), apim_portal.SECTION: portal_states.KINDS}.get(row.get("product"), ()):
            raise SeedError("Playground-only saved states do not yet support "
                            + str(row.get("product")) + ":" + str(row.get("kind")) + "; nothing was deleted")
        if not all(isinstance(row.get(field), str) and row[field] for field in ("id", "name", "label")):
            raise SeedError("A playground ownership record is incomplete")
        if row["id"] != quote(row["id"], safe="") or _key(row) in seen:
            raise SeedError("A playground ownership record has an invalid or repeated id")
        seen.add(_key(row))
    return rows


def _write(file, obj):
    file = Path(file)
    temporary = file.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        owner = file.parent / "owner.json"
        if owner.is_file() and os.geteuid() == 0:
            # Docker writes as root on Linux; the host UI must be able to read its private output.
            ids = json.loads(owner.read_text(encoding="utf-8"))
            if not all(type(ids.get(key)) is int and ids[key] >= 0 for key in ("uid", "gid")):
                raise SeedError("Invalid saved-state output owner")
            os.fchown(stream.fileno(), ids["uid"], ids["gid"])
        json.dump(obj, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(file)


def _supported(entity):
    if (entity.get("definitionVersion") != "V4" or entity.get("type") != "PROXY"
            or entity.get("allowedInApiProducts") or entity.get("groups")
            or any(listener.get("type") != "HTTP" for listener in entity.get("listeners", []))):
        raise SeedError("This API needs a playground-only saved-state adapter for its type or shared dependencies")
    if entity.get("state") == "STARTED" and entity.get("deploymentState") != "DEPLOYED":
        raise SeedError("Deploy this playground's pending API changes before saving its state")


class Resources:
    def __init__(self):
        self.management = apim.ApiManagement()
        self.management.connect()
        self._portals = None
        self.consumers = consumer_states.Consumers(self.management)

    def portals(self):
        if self._portals is None:
            self._portals = portal_states.Portals()
        return self._portals

    def at(self, row):
        env = row.get("env")
        return self.management.at(quote(env, safe="")) if env else self.management

    def target(self):
        return {"url": setting("APIM_MANAGEMENT_URL"), "org": self.management.org,
                "path": self.management.path, "env": self.management.env,
                "automation_url": setting("AIM_AUTOMATION_URL", setting("APIM_MANAGEMENT_URL"))}

    def entity(self, row):
        target = self.at(row)
        response = target.api.get(f"{target.v2}/apis/{row['id']}", expect=(200, 404))
        return response.json() if response.status_code == 200 else None

    def capture(self, rows, foreign=(), anchors=None):
        rows = _records(rows)
        items = []
        for row in rows:
            if (row["product"], row["kind"]) != (apim.SECTION, "api"):
                continue
            entity = self.entity(row)
            if entity is None:
                raise SeedError("A recorded API is missing; repair its ownership record before saving")
            _supported(entity)
            target = self.at(row)
            definition = target.api.get(f"{target.v2}/apis/{row['id']}/_export/definition",
                                        params={"excludeAdditionalData": "groups"}).json()
            # Exports omit identifiers for portability. A local checkpoint keeps API and plan identity.
            definition["api"]["id"] = row["id"]
            definition["api"]["crossId"] = entity.get("crossId")
            definition["api"].pop("groups", None)
            plans = self.listed(target, f"{target.v2}/apis/{row['id']}/plans")
            for plan in definition.get("plans", []):
                found = [candidate for candidate in plans if candidate.get("name") == plan.get("name")]
                if len(found) != 1:
                    raise SeedError("An API has ambiguous plan names; it cannot be saved safely")
                plan["id"] = found[0]["id"]
            items.append({"record": copy.deepcopy(row), "entity": entity, "definition": definition})
        if any(row["product"] == apim_portal.SECTION for row in rows):
            items.extend(self.portals().capture(rows))
        consumers = self.consumers.capture(rows, foreign, anchors=anchors)
        return {"schema": SCHEMA, "scope": "playground", "target": self.target(), "resources": items,
                "ownership": copy.deepcopy(rows), "consumers": consumers}

    @staticmethod
    def listed(target, path):
        rows, page = [], 1
        while True:
            result = target.api.get(path, params={"page": page, "perPage": 100}).json()
            current = result.get("data", [])
            rows.extend(current)
            if len(current) < 100:
                return rows
            page += 1

    def validate(self, saved, current, foreign):
        if (not isinstance(saved, dict) or saved.get("schema") not in (1, SCHEMA) or saved.get("scope") != "playground"
                or saved.get("target") != self.target() or not isinstance(saved.get("resources"), list)):
            raise SeedError("The saved resources belong to another management API or use an unsupported format")
        wanted = _records(consumer_states.ownership(saved))
        _records([item.get("record") for item in saved["resources"]])
        exported = {_key(item["record"]) for item in saved["resources"]}
        expected = {_key(row) for row in wanted if row["product"] != apim.SECTION or row["kind"] == "api"}
        if exported != expected:
            raise SeedError("The saved resources do not match their ownership records")
        mine = {_key(row) for row in _records(current)}
        others = {_key(row) for row in foreign}
        if (mine | {_key(row) for row in wanted}) & others:
            raise SeedError("An API is recorded by another playground; nothing was changed")
        for item in saved["resources"]:
            row, entity, definition = item["record"], item.get("entity", {}), item.get("definition", {})
            if (row["product"], row["kind"]) != (apim.SECTION, "api"):
                continue
            _supported(entity)
            if entity.get("id") != row["id"] or definition.get("api", {}).get("id") != row["id"]:
                raise SeedError("A saved API does not match its ownership record")
            target = self.at(row)
            live = self.entity(row)
            if live is not None:
                if _key(row) not in mine:
                    raise SeedError("The saved API's id is now used by an unowned API; nothing was changed")
            address = apim.ApiManagement.address(entity)
            for other in target.all_apis():
                if _key({**row, "id": other["id"]}) in mine:
                    continue
                if ((address and address == apim.ApiManagement.address(other))
                        or (entity.get("crossId") and entity["crossId"] == other.get("crossId"))):
                    raise SeedError("The saved API's path or crossId is now used by an unowned API; nothing was changed")
        portal_items = [item for item in saved["resources"] if item["record"]["product"] == apim_portal.SECTION]
        current_portals = [row for row in current if row["product"] == apim_portal.SECTION]
        if portal_items or current_portals:
            existing = self.portals().capture(current_portals)
            apis = {(row.get("env", self.management.env), row["id"]) for row in wanted if row["product"] == apim.SECTION}
            self.portals().validate(portal_items, existing, apis)
        apis = {(env, item["record"]["id"]) for item in saved["resources"]
                if (item["record"]["product"], item["record"]["kind"]) == (apim.SECTION, "api")
                for env in (item["record"].get("env", self.management.env),)}
        current_consumers = self.consumers.capture(current, foreign, anchors=saved)
        self.consumers.validate(saved.get("consumers"), current_consumers, current, foreign, apis)
        return wanted

    def apply(self, saved, previous=None):
        _records(common._read())  # An unresolved create must keep its note and recovery data.
        wanted = consumer_states.ownership(saved)
        previous = previous or self.capture(created(), anchors=saved)
        consumers = saved.get("consumers", previous.get("consumers", {"applications": [], "subscriptions": []}))
        prior_consumers = previous.get("consumers", {"applications": [], "subscriptions": []})
        desired = {_key(row) for row in [*wanted, *consumer_states.consumer_records({"consumers": consumers})]}
        # Keep credentials offline until their saved state and memberships are in place.
        offline = {}
        for item in [*previous["resources"], *saved["resources"]]:
            row = item["record"]
            if (row["product"], row["kind"]) == (apim.SECTION, "api"):
                if _key(row) in offline:
                    continue
                offline[_key(row)] = item
                target = self.at(row)
                live = self.entity(row)
                if live is not None and live.get("state") == "STARTED":
                    target.api.post(f"{target.v2}/apis/{row['id']}/_stop", json={}, idempotent=True)
        if offline:
            self.management.watch()
            if self.management.gateway:
                for item in offline.values():
                    target = self.at(item["record"])
                    target.until_dropped(item["record"]["name"], target.first_path(item["entity"]))
            else:
                common.pause(apim.SYNC_PAUSE)
        self.consumers.prepare(consumers, prior_consumers)
        stale = [row for row in _records(created()) if _key(row) not in desired]
        apim_portal.remove(stale)
        apim.remove(stale)
        for item in saved["resources"]:
            if (item["record"]["product"], item["record"]["kind"]) != (apim.SECTION, "api"):
                continue
            row, entity = item["record"], item["entity"]
            target = self.at(row)
            definition = copy.deepcopy(item["definition"])
            definition["api"]["state"] = "STOPPED"
            exists = self.entity(row) is not None
            url = f"{target.v2}/apis/{row['id']}/_import/definition" if exists else f"{target.v2}/apis/_import/definition"
            if exists:
                actual_id = target.api.put(url, json=definition).json()["id"]
            else:
                try:
                    actual_id = target.imported(definition, row["name"])
                except SeedError:
                    # A lost import response can still leave the fixed-id API in APIM.
                    # It was absent at preflight: retain its ownership so rollback can remove it.
                    if self.entity(row) is not None:
                        portal_states.remember(row)
                    raise
            record(apim.SECTION, "api", row["label"], row["name"], actual_id,
                   **{key: value for key, value in row.items() if key not in ("product", "kind", "label", "name", "id")})
            if actual_id != row["id"]:
                raise SeedError("The platform did not preserve the saved API id")
            # Import updates definitions; the entity also carries lifecycle and visibility.
            live = target.entity(actual_id, row["name"])
            target.api.put(f"{target.v2}/apis/{actual_id}", json={**live, **entity, "state": "STOPPED"})
            if target.entity(actual_id, row["name"]).get("state") == "STARTED":
                target.api.post(f"{target.v2}/apis/{actual_id}/_stop", json={}, idempotent=True)
        self.consumers.apply(consumers, prior_consumers)
        portal_items = [item for item in saved["resources"] if item["record"]["product"] == apim_portal.SECTION]
        if portal_items:
            self.portals().apply(portal_items)
        for item in saved["resources"]:
            row = item["record"]
            if (row["product"], row["kind"]) != (apim.SECTION, "api"):
                continue
            target = self.at(row)
            target.api.post(f"{target.v2}/apis/{row['id']}/deployments",
                            json={"deploymentLabel": "Restore playground state"}, idempotent=True)
            if target.entity(row["id"], row["name"]).get("state") != item["entity"].get("state"):
                operation = "_start" if item["entity"].get("state") == "STARTED" else "_stop"
                target.api.post(f"{target.v2}/apis/{row['id']}/{operation}", expect=(200, 202, 204), idempotent=True)
        save_created(copy.deepcopy(wanted))


def run(operation, file):
    common.configure(workers=1)
    path = Path(file)
    rows = _records(common._read())
    saved = None
    if operation in ("restore", "check"):
        saved = json.loads(path.read_text(encoding="utf-8"))
    if not rows and operation == "save":
        raise SeedError("This playground has no recorded resources to save")
    resources = Resources()
    foreign = json.loads((path.parent / "foreign.json").read_text(encoding="utf-8"))
    if operation == "save":
        saved = resources.capture(rows, foreign)
        resources.validate(saved, rows, foreign)
        _write(path, saved)
        log("state", f"Saved {len(rows)} playground resources")
        return
    if operation not in ("restore", "check"):
        raise SeedError("Unknown saved-state operation")
    resources.validate(saved, rows, foreign)
    previous = resources.capture(rows, foreign, anchors=saved)
    if operation == "check":
        log("state", "Verified playground-only restore")
        return
    # Keep this on disk until both API writes and ownership recording finish successfully.
    backup = path.parent / "previous.json"
    _write(backup, previous)
    try:
        resources.apply(saved, previous=previous)
    except Exception:
        log("state", "Restore failed; putting back only this playground's previous resources")
        common.configure(workers=1)
        try:
            current = resources.capture(created(), foreign, anchors=saved)
            resources.apply(previous, previous=current)
        except Exception as error:
            raise SeedError("Playground recovery is incomplete; previous.json was retained for recovery") from error
        backup.unlink()
        raise
    backup.unlink()
    log("state", f"Restored {len(saved['resources'])} playground resources")


def main():
    file = Path(sys.argv[-1])
    result = {"ok": True}
    try:
        with common.held():
            run(*sys.argv[1:])
    except Exception as error:  # noqa: BLE001 - isolate errors without exposing management response bodies
        # Management responses can include credentials; never echo their bodies into the Lab's job log.
        if isinstance(error, HttpError):
            message = "The management API refused the saved-state operation (HTTP " + str(error.status) + ")"
        else:
            message = str(error) if isinstance(error, SeedError) else type(error).__name__ + ": saved-state operation failed"
        log("state", "✗ " + message)
        result = {"ok": False, "error": message}
    _write(file.parent / "result.json", result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
