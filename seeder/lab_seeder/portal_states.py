"""Playground-owned portal resources and navigation, excluding shared theme activation.

Automation objects are addressed by their recorded hrids. Navigation is bounded by those
objects and recorded item ids; unowned children or occupied destinations refuse restoration.
"""

import copy
from urllib.parse import quote

from . import apim_portal
from .common import SeedError, created, making, record

SECTION = apim_portal.SECTION
KINDS = set(apim_portal.ORDER)


def key(row):
    return row["product"], row["kind"], row.get("env", "DEFAULT"), row["id"]


def remember(row, ident=None):
    record(row["product"], row["kind"], row["label"], row["name"], ident or row["id"],
           **{k: v for k, v in row.items() if k not in ("product", "kind", "label", "name", "id")})


class Portals:
    def __init__(self):
        self.platform = apim_portal.Portals()
        self.platform.connect()

    @staticmethod
    def collection(row):
        return {"portal-theme": "/portal-themes", "portal": "/portals",
                "portal-page": f"/portals/{quote(row.get('portal', ''), safe='')}/documentations"}.get(row["kind"])

    def entity(self, row):
        collection = self.collection(row)
        if not collection:
            return None
        url = self.platform.auto(row["env"]) + collection + "/" + quote(row["hrid"], safe="")
        response = self.platform.automation.get(url, expect=(200, 404))
        return response.json() if response.status_code == 200 else None

    def navigation(self, env, area="TOP_NAVBAR"):
        return self.platform.console.get(self.platform.v2(env) + "/portal-navigation-items",
                                         params={"area": area}).json().get("items", [])

    def content(self, env, item):
        if not item.get("portalPageContentId"):
            return None
        return self.platform.console.get(self.platform.v2(env) + "/portal-page-contents/" + item["portalPageContentId"]).json()

    @staticmethod
    def folders(body, navigation):
        tree = apim_portal.Tree(navigation)
        result = {}
        for entry in body.get("structure", {}).get("topNavbar", []):
            path = entry["path"]
            words = path.strip("/").split("/")
            titles = dict(zip(apim_portal.prefixes(path), words))
            titles[path] = entry.get("displayName") or words[-1]
            folder = tree.at(path, titles)
            if not folder:
                raise SeedError("An owned portal folder is missing or renamed; repair it before saving")
            for item in [*tree.above(folder), folder]:
                result[item["id"]] = item
        return list(result.values())

    def capture(self, rows):
        result = []
        for row in rows:
            if row["product"] != SECTION:
                continue
            if not all(isinstance(row.get(k), str) and row[k] and quote(row[k], safe="") == row[k]
                       for k in ("env", "hrid")):
                raise SeedError("A portal ownership record has an invalid environment or hrid")
            body = self.entity(row)
            if self.collection(row) and (body is None or body.get("id") != row["id"]):
                raise SeedError("A recorded portal resource is missing or its id changed")
            navigation = self.navigation(row["env"], (body or {}).get("area", "TOP_NAVBAR"))
            if row["kind"] == "portal":
                own = self.folders(body, navigation)
            elif row["kind"] == "portal-page":
                own = [item for item in navigation if item.get("portalPageContentId") == row["id"]]
            else:
                own = [item for item in navigation if item["id"] in row.get("items", [])]
                if len(own) != len(row.get("items", [])):
                    raise SeedError("A recorded portal navigation item is missing")
            result.append({"record": copy.deepcopy(row), "body": body,
                           "navigation": [{"item": copy.deepcopy(item), "content": self.content(row["env"], item)} for item in own]})
        self.bounded(result)
        return result

    def bounded(self, items):
        """Never delete or move an owned folder while unrecorded items depend on it."""
        for env in {item["record"]["env"] for item in items}:
            mine = {entry["item"]["id"] for item in items if item["record"]["env"] == env for entry in item["navigation"]}
            for entry in self.navigation(env):
                if entry.get("parentId") in mine and entry["id"] not in mine:
                    raise SeedError("An owned portal folder contains unrecorded data; nothing was changed")

    def validate(self, saved, current, apis):
        self.bounded(current)
        current_ids = {(item["record"]["env"], entry["item"]["id"])
                       for item in current for entry in item["navigation"]}
        current_records = {key(item["record"]) for item in current}
        for item in saved:
            row = item["record"]
            live = self.entity(row)
            if live is not None and key(row) not in current_records:
                raise SeedError("A saved portal hrid is now used by an unowned resource; nothing was changed")
            if live is not None and live.get("id") != row["id"]:
                raise SeedError("A saved portal id is now used by another resource")
            live_items = {entry["id"]: entry for entry in self.navigation(row["env"], (item.get("body") or {}).get("area", "TOP_NAVBAR"))}
            saved_ids = {entry["item"]["id"] for entry in item["navigation"]}
            for entry in item["navigation"]:
                nav = entry["item"]
                if nav.get("apiId") and (row["env"], nav["apiId"]) not in apis:
                    raise SeedError("A portal listing refers to an API not owned by this playground")
                if nav["id"] in live_items and (row["env"], nav["id"]) not in current_ids:
                    raise SeedError("A saved portal navigation id is now used by unowned data")
                if any(other.get("parentId") == nav["id"] and (row["env"], other["id"]) not in current_ids
                       and other["id"] not in saved_ids for other in live_items.values()):
                    raise SeedError("A saved portal folder now contains unowned data")
            if row["kind"] == "portal-page" and (item.get("body") or {}).get("area") == "HOMEPAGE":
                if any((row["env"], entry["id"]) not in current_ids for entry in live_items.values()):
                    raise SeedError("An unowned homepage is active; restoring this page would replace it")

    def apply(self, items):
        wanted = {(item["record"]["env"], entry["item"]["id"]) for item in items for entry in item["navigation"]}
        current = [row for row in created() if row["product"] == SECTION]
        # Record the whole bounded set before navigation writes: a partial create must be recoverable.
        for saved in items:
            row = saved["record"]
            if row["kind"] not in ("portal-listing", "portal-api-page"):
                continue
            old = next((before for before in current if key(before) == key(row)), {})
            remember({**row, "items": sorted(set(old.get("items", [])) | set(row.get("items", [])))})
        for row in current:
            if row["kind"] in ("portal-listing", "portal-api-page"):
                for ident in row.get("items", []):
                    if (row["env"], ident) not in wanted:
                        self.platform.console.delete(self.platform.v2(row["env"]) + "/portal-navigation-items/" + ident,
                                                     expect=(200, 204, 404))
        order = {kind: n for n, kind in enumerate(reversed(apim_portal.ORDER))}
        for saved in sorted(items, key=lambda item: order[item["record"]["kind"]]):
            row, body = saved["record"], saved["body"]
            collection = self.collection(row)
            if not collection:
                continue
            fields = {"portal-theme": ("hrid", "name", "definition", *apim_portal.IMAGES),
                      "portal": ("hrid", "name", "structure"),
                      "portal-page": ("hrid", "name", "type", "content", "area", "location", "order", "visibility")}[row["kind"]]
            payload = {field: copy.deepcopy(body[field]) for field in fields if field in body}
            if row["kind"] == "portal":
                # Portal upsert activates a theme globally. Keep the current link instead of rewinding it.
                live = self.entity(row)
                if (live or {}).get("activeThemeHrid"):
                    payload["activeThemeHrid"] = live["activeThemeHrid"]
            absent = self.entity(row) is None
            try:
                extra = {key: row[key] for key in ("env", "hrid", "portal") if key in row}
                with making(row["product"], row["kind"], row["label"], row["name"], missing=absent, **extra):
                    state = self.platform.automation.put(self.platform.auto(row["env"]) + collection, json=payload).json()
                    remember(row, state["id"])
            except SeedError:
                if absent and (self.entity(row) or {}).get("id") == row["id"]:
                    remember(row)
                raise
            if state["id"] != row["id"] or state.get("errors", {}).get("severe"):
                raise SeedError("The platform could not restore the recorded portal resource")
        navigation = {}
        for item in items:
            env = item["record"]["env"]
            for entry in item["navigation"]:
                navigation[(env, entry["item"]["id"])] = entry
        pending = dict(navigation)
        while pending:
            done = False
            for ident, entry in list(pending.items()):
                env, nav = ident[0], entry["item"]
                if (env, nav.get("parentId")) in pending:
                    continue
                self.write_navigation(env, entry)
                del pending[ident]
                done = True
            if not done:
                raise SeedError("The saved portal navigation has a parent cycle")
        for item in items:
            remember(item["record"])

    def write_navigation(self, env, saved):
        item, content = saved["item"], saved["content"]
        base = self.platform.v2(env) + "/portal-navigation-items"
        existing = next((entry for entry in self.navigation(env, item.get("area", "TOP_NAVBAR")) if entry["id"] == item["id"]), None)
        if existing is None:
            fields = ("id", "type", "title", "area", "parentId", "apiId", "categoryIds", "url", "order", "visibility")
            body = {field: item[field] for field in fields if field in item}
            if content:
                body["contentType"] = content["type"]
            live = self.platform.console.post(base, json=body).json()
        else:
            live = existing
        if live["id"] != item["id"]:
            raise SeedError("The platform did not preserve a portal navigation id")
        if content:
            body = {key: content[key] for key in ("type", "content", "configuration") if key in content}
            self.platform.console.put(self.platform.v2(env) + "/portal-page-contents/" + live["portalPageContentId"], json=body)
        fields = ("type", "title", "order", "published", "visibility", "apiId", "categoryIds", "url")
        changes = {field: item[field] for field in fields if field in item}
        changes["parentId"] = item.get("parentId")
        self.platform.put(env, "Restore owned portal navigation", live, changes)
