"""Playground-scoped applications, subscriptions and credentials through management APIs.

A consumer app belongs to this snapshot only while all its subscription links point to
this playground's APIs and no other playground records it. Shared applications are
references: restoring our subscriptions never rewrites their settings or memberships.
Seed ownership stays separate from consumer apps created manually in the Portal.
"""

import copy
import json
import os
import uuid
from urllib.parse import quote

from . import apim, common
from .common import HttpError, SeedError, making, record, setting

STATUSES = ("PENDING", "ACCEPTED", "PAUSED", "CLOSED", "REJECTED")


def ident(value):
    return value.get("id") if isinstance(value, dict) else value


def env(row):
    return row.get("env", setting("APIM_ENV", "DEFAULT"))


def key(row):
    return env(row), row["id"]


def safe(row, kind):
    if (not isinstance(row, dict) or row.get("product") != apim.SECTION or row.get("kind") != kind
            or not all(isinstance(row.get(name), str) and row[name] for name in ("id", "name", "label"))
            or quote(row["id"], safe="") != row["id"]
            or not isinstance(env(row), str) or quote(env(row), safe="") != env(row)):
        raise SeedError("A saved consumer ownership record is damaged")


def ownership(saved):
    return saved.get("ownership", [item["record"] for item in saved["resources"]])


def consumer_records(saved):
    data = saved.get("consumers") or {}
    return [item["record"] for item in data.get("applications", []) if item["managed"]] + [
        item["record"] for item in data.get("subscriptions", [])]


def remember(row):
    record(apim.SECTION, row["kind"], row["label"], row["name"], row["id"],
           **{name: value for name, value in row.items() if name not in ("product", "kind", "label", "name", "id")})


def noted(kind, value, environment, **extra):
    label = "applications" if kind == "application" else "subscriptions"
    return {"product": apim.SECTION, "kind": kind, "label": label, "id": value["id"],
            "name": value.get("name") or f"Subscription {value['id']}", "env": environment, **extra}


class Consumers:
    def __init__(self, management):
        self.management = management
        self.alias_file = common.STATE_FILE.with_name("saved-state-subscriptions.json")
        self.aliases = json.loads(self.alias_file.read_text()) if self.alias_file.exists() else []
        if not isinstance(self.aliases, list) or not all(isinstance(row, dict) and
                all(isinstance(row.get(name), str) and row[name] and quote(row[name], safe="") == row[name]
                    for name in ("env", "api", "id", "actual")) for row in self.aliases):
            raise SeedError("Saved subscription identity records are damaged")

    def resolve(self, row):
        if row.get("kind") != "subscription" and "api" not in row:
            return row
        actual, seen = row["id"], set()
        aliases = {(entry["env"], entry["api"], entry["id"]): entry["actual"] for entry in self.aliases}
        while (env(row), row["api"], actual) in aliases:
            if actual in seen:
                raise SeedError("Saved subscription identity records contain a cycle")
            seen.add(actual)
            actual = aliases[(env(row), row["api"], actual)]
        return {**row, "id": actual}

    def link(self, row, actual):
        if row["id"] == actual:
            return
        # The normal Portal create keeps subscriptions editable; its new id is local bookkeeping.
        self.aliases = [entry for entry in self.aliases if (entry["env"], entry["api"], entry["id"]) !=
                        (env(row), row["api"], row["id"])]
        self.aliases.append({"env": env(row), "api": row["api"], "id": row["id"], "actual": actual})
        partial = self.alias_file.with_name(self.alias_file.name + "." + uuid.uuid4().hex + ".part")
        try:
            with partial.open("x", encoding="utf-8") as stream:
                os.chmod(partial, 0o600)
                if os.geteuid() == 0:
                    owner = self.alias_file.parent.stat()
                    os.fchown(stream.fileno(), owner.st_uid, owner.st_gid)
                json.dump(self.aliases, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            partial.replace(self.alias_file)
        finally:
            partial.unlink(missing_ok=True)

    def owned(self, saved):
        return [self.resolve(row) if (row["product"], row["kind"]) == (apim.SECTION, "subscription") else row
                for row in ownership(saved)]

    def at(self, row):
        return self.management if env(row) == self.management.env else self.management.at(quote(env(row), safe=""))

    @staticmethod
    def listed(target, path, legacy=False, **params):
        result, page = [], 1
        while True:
            size = "size" if legacy else "perPage"
            obj = target.api.get(path, params={**params, size: 100, "page": page}).json()
            if isinstance(obj, list):
                return obj
            rows = obj.get("data", [])
            result.extend(rows)
            if len(rows) < 100:
                return result
            page += 1

    def app(self, row):
        target = self.at(row)
        answer = target.api.get(f"{target.v1}/applications/{row['id']}", expect=(200, 404))
        return answer.json() if answer.status_code == 200 else None

    def subscription(self, row, resolve=True):
        row = self.resolve(row) if resolve else row
        target = self.at(row)
        answer = target.api.get(f"{target.v2}/apis/{row['api']}/subscriptions/{row['id']}", expect=(200, 404))
        return answer.json() if answer.status_code == 200 else None

    def keys(self, row, resolve=True):
        row = self.resolve(row) if resolve else row
        target = self.at(row)
        return self.listed(target, f"{target.v2}/apis/{row['api']}/subscriptions/{row['id']}/api-keys")

    def links(self, row):
        target = self.at(row)
        return self.listed(target, f"{target.v1}/applications/{row['id']}/subscriptions", legacy=True,
                           status=",".join(STATUSES))

    @staticmethod
    def api_of(sub):
        if sub.get("referenceType") not in (None, "API"):
            return None
        return ident(sub.get("api")) or sub.get("referenceId")

    def capture(self, records, foreign, anchors=None):
        anchors = anchors or {}
        roots = {(env(row), row["id"]) for row in records if (row["product"], row["kind"]) == (apim.SECTION, "api")}
        roots |= {(env(item["record"]), item["record"]["id"]) for item in anchors.get("resources", [])
                  if (item["record"]["product"], item["record"]["kind"]) == (apim.SECTION, "api")}
        foreign_apps = {key(row) for row in foreign if (row.get("product"), row.get("kind")) == (apim.SECTION, "application")}
        foreign_subs = {key(row) for row in foreign if (row.get("product"), row.get("kind")) == (apim.SECTION, "subscription")}
        seed_apps = {key(row): row for row in records if (row["product"], row["kind"]) == (apim.SECTION, "application")}
        seed_subs = {key(row): row for row in records if (row["product"], row["kind"]) == (apim.SECTION, "subscription")}
        apps = dict(seed_apps)
        for item in (anchors.get("consumers") or {}).get("applications", []):
            apps.setdefault(key(item["record"]), item["record"])
        subscriptions = []
        for environment, api_id in sorted(roots):
            row = {"env": environment, "id": api_id}
            target = self.at(row)
            for value in self.listed(target, f"{target.v2}/apis/{api_id}/subscriptions", statuses=",".join(STATUSES)):
                subrow = seed_subs.get((environment, value["id"])) or noted("subscription", value, environment, api=api_id)
                if key(subrow) in foreign_subs:
                    raise SeedError("Another playground records a subscription to this API; its data must be kept")
                entity = self.subscription(subrow, resolve=False)
                if entity is None:
                    raise SeedError("A subscription changed during capture; retry saving this playground")
                if entity.get("status") not in STATUSES:
                    raise SeedError("The platform returned an unknown subscription state")
                app_id = ident(entity.get("application"))
                if not isinstance(app_id, str) or not app_id:
                    raise SeedError("A subscription does not identify its consumer application")
                apps.setdefault((environment, app_id), noted("application", {"id": app_id}, environment))
                subscriptions.append({"record": copy.deepcopy(subrow), "entity": entity, "keys": self.keys(subrow, resolve=False)})
        mine = {key(item["record"]) for item in subscriptions}
        if set(seed_subs) - mine:
            raise SeedError("A recorded subscription is missing or points outside this playground's APIs")
        for item in subscriptions:
            for credential in item["keys"]:
                links = {ident(link) for link in credential.get("subscriptions", [])}
                if any((env(item["record"]), link) not in mine for link in links):
                    raise SeedError("An API key is shared outside this playground; restoring it would change other consumers")
        applications = []
        for appkey, row in sorted(apps.items()):
            value = self.app(row)
            if value is None:
                if appkey in seed_apps or any(ident(item["entity"]["application"]) == row["id"] for item in subscriptions):
                    raise SeedError("A recorded consumer application is missing")
                continue
            if value.get("status") not in ("ACTIVE", "ARCHIVED"):
                raise SeedError("The platform returned an unknown application state")
            links = self.links(row)
            managed = appkey not in foreign_apps and all((env(row), self.api_of(link)) in roots and
                                                        (env(row), link["id"]) not in foreign_subs for link in links)
            if appkey in seed_apps and not managed:
                raise SeedError("A recorded application is shared with another playground; deleting it would change other data")
            row = seed_apps.get(appkey) or noted("application", value, env(row))
            item = {"record": copy.deepcopy(row), "managed": managed, "entity": value if managed else {"id": value["id"], "status": value["status"]}}
            if managed:
                target = self.at(row)
                item["members"] = target.api.get(f"{target.v1}/applications/{row['id']}/members").json()
                item["metadata"] = target.api.get(f"{target.v1}/applications/{row['id']}/metadata").json()
            applications.append(item)
        return {"applications": applications, "subscriptions": subscriptions}

    def validate(self, saved, current, records, foreign, apis):
        if saved is None:
            return
        if not isinstance(saved, dict) or not all(isinstance(saved.get(name), list) for name in ("applications", "subscriptions")):
            raise SeedError("Saved consumer resources are damaged")
        for item in saved["applications"]:
            safe(item.get("record"), "application")
        for item in saved["subscriptions"]:
            safe(item.get("record"), "subscription")
        current_apps = {key(item["record"]): item for item in current["applications"]}
        foreign_apps = {key(row) for row in foreign if (row.get("product"), row.get("kind")) == (apim.SECTION, "application")}
        foreign_subs = {key(row) for row in foreign if (row.get("product"), row.get("kind")) == (apim.SECTION, "subscription")}
        subscriptions = {key(item["record"]): item for item in saved["subscriptions"]}
        applications = {key(item["record"]): item for item in saved["applications"]}
        if len(subscriptions) != len(saved["subscriptions"]) or len(applications) != len(saved["applications"]):
            raise SeedError("Saved consumer resources contain repeated ids")
        for appkey, item in applications.items():
            row = item["record"]
            if item.get("managed") is True:
                if appkey in foreign_apps or (appkey in current_apps and not current_apps[appkey]["managed"]):
                    raise SeedError("A saved application is now shared with another playground; nothing was changed")
                if item.get("entity", {}).get("id") != row["id"]:
                    raise SeedError("A saved application does not match its ownership")
                if item["entity"].get("status") not in ("ACTIVE", "ARCHIVED"):
                    raise SeedError("A saved application has an unknown state")
                target = self.at(row)
                for member in item.get("members", []):
                    member_id = member.get("id")
                    if not isinstance(member_id, str) or quote(member_id, safe="") != member_id:
                        raise SeedError("A saved application member is damaged")
                    answer = target.api.get(f"{target.org_v1}/users/{member_id}", expect=(200, 404))
                    if answer.status_code != 200:
                        raise SeedError("A saved application member no longer exists; restore cannot recreate shared users")
            else:
                live = self.app(row)
                if live is None or live.get("status") != "ACTIVE":
                    raise SeedError("A shared consumer application is missing or archived; it cannot be changed by this playground")
        for subkey, item in subscriptions.items():
            row, entity = item["record"], item["entity"]
            if subkey in foreign_subs or key(self.resolve(row)) in foreign_subs or (env(row), row["api"]) not in apis:
                raise SeedError("A saved subscription does not belong exclusively to this playground")
            if entity.get("id") != row["id"] or self.api_of(entity) != row["api"] or entity.get("status") not in STATUSES:
                raise SeedError("A saved subscription does not match its API or ownership")
            app_id, plan_id = ident(entity.get("application")), ident(entity.get("plan"))
            if (env(row), app_id) not in applications or not isinstance(plan_id, str):
                raise SeedError("A saved subscription has no saved application or plan")
            live = self.subscription(row)
            if live and (ident(live.get("application")), ident(live.get("plan"))) != (app_id, plan_id):
                raise SeedError("A saved subscription id now belongs to another application or plan")
            for credential in item["keys"]:
                if not isinstance(credential.get("key"), str) or not credential["key"]:
                    raise SeedError("A saved API key is incomplete")
                if any((env(row), ident(link)) not in subscriptions for link in credential.get("subscriptions", [])):
                    raise SeedError("A saved API key is shared outside this playground")
            allowed = set(subscriptions) | {key(self.resolve(value["record"])) for value in saved["subscriptions"]}
            allowed |= {key(value["record"]) for value in current["subscriptions"]}
            self.validate_keys(item, allowed)

    def validate_keys(self, item, subscriptions):
        row = item["record"]
        target = self.at(row)
        app_id = ident(item["entity"]["application"])
        app = self.app({"env": env(row), "id": app_id})
        existing = target.api.get(f"{target.v1}/applications/{app_id}/apikeys").json() if app else []
        for credential in item["keys"]:
            matches = [candidate for candidate in existing if candidate["key"] == credential["key"]]
            if matches:
                if any((env(row), ident(link)) not in subscriptions for candidate in matches
                       for link in candidate.get("subscriptions", [])):
                    raise SeedError("A saved API key now grants access outside this playground; nothing was changed")
            else:
                # Verification is read-only. It also detects the key being taken by another application.
                answer = target.api.post(f"{target.v2}/apis/{row['api']}/subscriptions/_verify",
                                         json={"applicationId": app_id, "apiKey": credential["key"]}).json()
                if answer.get("ok") is not True:
                    raise SeedError("A saved API key is now used by another consumer; nothing was changed")

    def close(self, row):
        row = self.resolve(row)
        live = self.subscription(row)
        if live and live["status"] not in ("CLOSED", "REJECTED"):
            target = self.at(row)
            target.api.post(f"{target.v2}/apis/{row['api']}/subscriptions/{row['id']}/_close", json={}, idempotent=True)

    def prepare(self, saved, current):
        wanted = {key(self.resolve(item["record"])) for item in saved["subscriptions"]}
        for item in current["subscriptions"]:
            if key(self.resolve(item["record"])) not in wanted:
                self.close(item["record"])

    @staticmethod
    def app_body(value):
        fields = ("name", "description", "settings", "groups", "type", "api_key_mode", "disable_membership_notifications", "domain")
        return {name: copy.deepcopy(value[name]) for name in fields if name in value}

    def write_application(self, item):
        if not item["managed"]:
            return
        row, value = item["record"], item["entity"]
        target, live = self.at(row), self.app(row)
        base = f"{target.v1}/applications/{row['id']}"
        if live is None:
            with making(apim.SECTION, "application", row["label"], row["name"], missing=True, env=env(row)):
                result = target.api.post(f"{target.v1}/applications", json={"id": row["id"], **self.app_body(value)}).json()
                remember({**row, "id": result["id"]})
                if result["id"] != row["id"]:
                    raise SeedError("The platform did not preserve the saved application id")
        elif live.get("status") == "ARCHIVED":
            target.api.post(base + "/_restore", json={}, idempotent=True)
        target.api.put(base, json={**self.app_body(value), "status": "ACTIVE"})
        self.write_members(item)
        current_metadata = target.api.get(base + "/metadata").json()
        desired = {value["key"]: value for value in item.get("metadata", [])}
        for metadata in current_metadata:
            if metadata["key"] not in desired:
                target.api.delete(base + "/metadata/" + quote(metadata["key"], safe=""))
        existing = {value["key"] for value in current_metadata}
        for name, metadata in desired.items():
            if name in existing:
                target.api.put(base + "/metadata/" + quote(name, safe=""), json=metadata)
            else:
                target.api.post(base + "/metadata", json=metadata)

    def write_members(self, item):
        row, value = item["record"], item["entity"]
        target = self.at(row)
        base = f"{target.v1}/applications/{row['id']}/members"
        owner = (value.get("owner") or {}).get("id")
        live = self.app(row)
        if owner and (live.get("owner") or {}).get("id") != owner:
            target.api.post(base + "/transfer_ownership", json={"id": owner, "poRole": "OWNER"})
        current = target.api.get(base).json()
        wanted = {member["id"]: member for member in item.get("members", [])}
        for member in current:
            if member["id"] not in wanted and member.get("role") != "PRIMARY_OWNER":
                target.api.delete(base, params={"user": member["id"]})
        for member in wanted.values():
            if member.get("role") != "PRIMARY_OWNER":
                target.api.post(base, json={"id": member["id"], "role": member["role"]})

    def import_subscription(self, item):
        original, entity = item["record"], item["entity"]
        row = self.resolve(original)
        target = self.at(row)
        base = f"{target.v2}/apis/{row['api']}/subscriptions"
        live = self.subscription(row)
        if live is None:
            with making(apim.SECTION, "subscription", row["label"], row["name"], missing=True, api=row["api"], env=env(row)):
                result = target.api.post(target.portal + "/subscriptions", json={
                    "application": ident(entity["application"]), "plan": ident(entity["plan"]),
                    "request": entity.get("consumerMessage") or "", "metadata": entity.get("metadata") or {},
                    "configuration": entity.get("consumerConfiguration")}).json()
                remember({**row, "id": result["id"]})
                self.link(row, result["id"])
            row = self.resolve(original)
            live = self.subscription(row)
        spec = {"id": row["id"], "applicationId": ident(entity["application"]), "referenceId": row["api"], "referenceType": "API",
                "planId": ident(entity["plan"]), "endingAt": None, "metadata": entity.get("metadata") or {},
                "consumerConfiguration": entity.get("consumerConfiguration")}
        if live["status"] == "CLOSED":
            # Reopening an existing row retains its MANAGEMENT origin; CRD creation would lock the Console.
            try:
                target.api.put(base + "/spec/_import", json={**spec, "apiKeys": []})
            except HttpError as error:
                after = self.subscription(row)
                try:
                    code = json.loads(error.body).get("technicalCode")
                except (TypeError, ValueError):
                    code = None
                if not after or after["status"] != "PENDING" or error.status != 400 or code != "subscription.notUpdatable":
                    raise
            live = self.subscription(row)
        if live["status"] == "REJECTED":
            self.pending(row, entity)
            live = self.subscription(row)
        status = entity["status"]
        if status in ("ACCEPTED", "PAUSED", "CLOSED") and live["status"] == "PENDING":
            target.api.post(base + "/" + row["id"] + "/_accept",
                            json={"reason": entity.get("publisherMessage") or "",
                                  "startingAt": entity.get("startingAt"), "endingAt": None}, idempotent=True)
        elif status in ("ACCEPTED", "CLOSED") and live["status"] == "PAUSED":
            target.api.post(base + "/" + row["id"] + "/_resume", json={}, idempotent=True)
        desired_keys = [{"key": credential["key"], "expireAt": credential.get("expireAt")} for credential in item["keys"]]
        target.api.put(base + "/spec/_import", json={**spec, "apiKeys": desired_keys})
        dates = {name: entity.get(name) for name in ("startingAt", "endingAt", "consumerConfiguration", "metadata")}
        target.api.put(base + "/" + row["id"], json=dates)
        if status == "PENDING":
            self.pending(row, entity)
        elif status == "REJECTED":
            self.pending(row, entity)
            target.api.post(base + "/" + row["id"] + "/_reject", json={"reason": entity.get("publisherMessage") or ""}, idempotent=True)
        self.restore_keys(item)
        live = self.subscription(row)
        if status == "PAUSED" and live["status"] != "PAUSED":
            target.api.post(base + "/" + row["id"] + "/_pause", json={}, idempotent=True)
        elif status == "CLOSED" and live["status"] != "CLOSED":
            self.close(row)
        final = self.subscription(row)
        if final["status"] != status:
            raise SeedError("The platform did not restore the saved subscription status")
        if entity.get("origin") in (None, "MANAGEMENT") and final.get("origin", "MANAGEMENT") != "MANAGEMENT":
            raise SeedError("The restored subscription cannot be managed in the Console")

    def pending(self, row, entity):
        row = self.resolve(row)
        target = self.at(row)
        target.api.put(f"{target.portal}/subscriptions/{row['id']}",
                       json={"configuration": entity.get("consumerConfiguration"), "metadata": entity.get("metadata") or {}})

    def restore_keys(self, item):
        row = self.resolve(item["record"])
        target = self.at(row)
        base = f"{target.v2}/apis/{row['api']}/subscriptions/{row['id']}/api-keys"
        wanted = {credential["key"]: credential for credential in item["keys"]}
        current = self.keys(row)
        for credential in current:
            expected = wanted.get(credential["key"])
            url = base + "/" + credential["id"]
            if expected is None or expected.get("revoked"):
                if not credential.get("revoked"):
                    target.api.post(url + "/_revoke", json={}, idempotent=True)
            # The spec import already reconciled expiry and reactivated saved keys.
            # Normal key PUT rejects expired/revoked keys and cannot restore a past expiry.
        if not set(wanted).issubset({credential["key"] for credential in current}):
            raise SeedError("The platform did not restore every saved API key")

    def apply(self, saved, previous):
        wanted_apps = {key(item["record"]) for item in saved["applications"]}
        for item in previous["applications"]:
            if item["managed"] and key(item["record"]) not in wanted_apps:
                target = self.at(item["record"])
                target.api.delete(f"{target.v1}/applications/{item['record']['id']}")
        for item in saved["applications"]:
            self.write_application(item)
        ordered = sorted(saved["subscriptions"], key=lambda item: item["entity"]["status"] not in ("CLOSED", "REJECTED"))
        for item in ordered:
            self.import_subscription(item)
        for item in saved["applications"]:
            if item["managed"] and item["entity"]["status"] == "ARCHIVED":
                target = self.at(item["record"])
                target.api.delete(f"{target.v1}/applications/{item['record']['id']}")
