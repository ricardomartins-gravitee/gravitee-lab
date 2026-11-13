"""Portal checkpoints bound navigation by ownership and preserve shared theme activation.

These cases cover collisions and incomplete navigation that the real Gravitee scenario
cannot inject deterministically, using the same fake HTTP platform as the seeder tests.
"""

import copy
import re

from lab_seeder import common, portal_states
from lab_seeder.common import SeedError
from lab_seeder.testkit import Seed

V2 = "/management/v2/organizations/DEFAULT/environments/DEFAULT"
AUTO = "/automation/organizations/DEFAULT/environments/DEFAULT"


def row(kind, ident, **more):
    return {"product": "apim-portal", "kind": kind, "id": ident, "label": kind, "name": kind,
            "env": "DEFAULT", "hrid": "owned-" + kind, **more}


class PortalStates(Seed):
    def setUp(self):
        super().setUp()
        self.rows = [row("portal-theme", "theme"), row("portal", "portal"),
                     row("portal-page", "contact-content", portal="owned-portal"),
                     row("portal-listing", "listing", portal="owned-portal", items=["listing-item"]),
                     row("portal-api-page", "api-pages", portal="owned-portal", items=["api-page"], legacy=[])]
        self.objects = {
            "/portal-themes/owned-portal-theme": {"id": "theme", "hrid": "owned-portal-theme", "name": "Theme", "definition": {}},
            "/portals/owned-portal": {"id": "portal", "hrid": "owned-portal", "name": "Portal", "activeThemeHrid": "saved-theme",
                                     "structure": {"topNavbar": [{"path": "/owned", "displayName": "Owned"}]}},
            "/portals/owned-portal/documentations/owned-portal-page": {
                "id": "contact-content", "hrid": "owned-portal-page", "name": "Contact", "content": "Saved contact edit",
                "type": "GRAVITEE_MARKDOWN", "area": "TOP_NAVBAR", "location": "/owned"},
        }
        self.nav = {item["id"]: item for item in [
            {"id": "folder", "type": "FOLDER", "title": "Owned", "area": "TOP_NAVBAR", "visibility": "PUBLIC", "published": True},
            {"id": "listing-item", "type": "API", "title": "Mine", "parentId": "folder", "apiId": "mine",
             "area": "TOP_NAVBAR", "visibility": "PUBLIC", "published": True},
            {"id": "api-page", "type": "PAGE", "title": "Overview", "parentId": "listing-item", "portalPageContentId": "api-content",
             "area": "TOP_NAVBAR", "visibility": "PUBLIC", "published": True},
            {"id": "contact", "type": "PAGE", "title": "Contact", "parentId": "folder", "portalPageContentId": "contact-content",
             "area": "TOP_NAVBAR", "visibility": "PUBLIC", "published": True},
            {"id": "unowned", "type": "LINK", "title": "Latest unowned docs", "area": "TOP_NAVBAR", "url": "https://example.invalid"},
        ]}
        self.contents = {"api-content": {"id": "api-content", "type": "GRAVITEE_MARKDOWN", "content": "Saved overview edit"},
                         "contact-content": {"id": "contact-content", "type": "GRAVITEE_MARKDOWN", "content": "Saved contact edit"}}
        self.ids = {path: body["id"] for path, body in self.objects.items()}

        def automation(call):
            path = call.path[len(AUTO):]
            if call.method == "GET":
                return (200, copy.deepcopy(self.objects[path])) if path in self.objects else (404, {})
            path += "/" + call.json["hrid"]
            body = {**copy.deepcopy(call.json), "id": self.ids[path]}
            self.objects[path] = body
            return 200, body

        def navigation(call):
            if call.method == "GET":
                return 200, {"items": copy.deepcopy(list(self.nav.values()))}
            body = copy.deepcopy(call.json)
            if call.method == "PUT":
                ident = call.path.rsplit("/", 1)[-1]
                self.nav[ident].update(body)
                return 200, copy.deepcopy(self.nav[ident])
            ident = body["id"]
            if body["type"] == "PAGE":
                body["portalPageContentId"] = "new-" + ident
            self.nav[ident] = body
            return 201, copy.deepcopy(body)

        def content(call):
            ident = call.path.rsplit("/", 1)[-1]
            if call.method == "PUT":
                self.contents[ident] = {"id": ident, **copy.deepcopy(call.json)}
            return 200, copy.deepcopy(self.contents[ident])

        self.answer({
            ("GET", "/management/organizations/DEFAULT/environments"): (200, []),
            ("GET", re.compile(AUTO + r"/.*")): automation,
            ("PUT", re.compile(AUTO + r"/.*")): automation,
            ("GET", V2 + "/portal-navigation-items"): navigation,
            ("POST", V2 + "/portal-navigation-items"): navigation,
            ("PUT", re.compile(V2 + r"/portal-navigation-items/[^/]+")): navigation,
            ("GET", re.compile(V2 + r"/portal-page-contents/[^/]+")): content,
            ("PUT", re.compile(V2 + r"/portal-page-contents/[^/]+")): content,
        }, created=self.rows)
        self.portals = portal_states.Portals()

    def test_capture_excludes_unowned_navigation_and_rejects_unowned_children_of_owned_folders(self):
        saved = self.portals.capture(self.rows)
        ids = {entry["item"]["id"] for item in saved for entry in item["navigation"]}
        self.assertEqual(ids, {"folder", "listing-item", "api-page", "contact"})
        self.nav["unowned"]["parentId"] = "folder"
        with self.assertRaisesRegex(SeedError, "unrecorded data"):
            self.portals.capture(self.rows)
        self.assertEqual(self.platform.changes(), [])

    def test_restore_keeps_current_theme_activation_and_never_writes_unowned_navigation(self):
        saved = self.portals.capture(self.rows)
        before = copy.deepcopy(self.nav["unowned"])
        self.objects["/portals/owned-portal"]["activeThemeHrid"] = "current-other-theme"
        self.portals.validate(saved, self.portals.capture(self.rows), {("DEFAULT", "mine")})
        self.portals.apply(saved)
        put = self.platform.sent("PUT", AUTO + "/portals")[-1]
        self.assertEqual(put.json["activeThemeHrid"], "current-other-theme")
        self.assertEqual(self.nav["unowned"], before)
        self.assertFalse(any(path.endswith("/unowned") for method, path in self.platform.changes()))
        self.assertEqual(self.contents["api-content"]["content"], "Saved overview edit")

    def test_missing_page_gets_its_new_content_id_without_pointing_to_a_stale_saved_content(self):
        saved = self.portals.capture(self.rows)
        del self.nav["api-page"]
        del self.contents["api-content"]
        self.portals.apply(saved)
        self.assertEqual(self.nav["api-page"]["portalPageContentId"], "new-api-page")
        self.assertEqual(self.contents["new-api-page"]["content"], "Saved overview edit")
        posted = self.platform.sent("POST", V2 + "/portal-navigation-items")[-1]
        self.assertNotIn("portalPageContentId", posted.json)
        updated = self.platform.sent("PUT", V2 + "/portal-navigation-items/api-page")[-1]
        self.assertNotIn("portalPageContentId", updated.json)

    def test_live_hrid_and_navigation_collisions_or_unowned_api_links_refuse_before_writes(self):
        saved = self.portals.capture(self.rows)
        with self.assertRaisesRegex(SeedError, "unowned resource"):
            self.portals.validate(saved, [], {("DEFAULT", "mine")})
        current = self.portals.capture(self.rows)
        with self.assertRaisesRegex(SeedError, "API not owned"):
            self.portals.validate(saved, current, set())
        self.nav["unowned"]["parentId"] = "folder"
        with self.assertRaisesRegex(SeedError, "unrecorded data"):
            self.portals.validate(saved, current, {("DEFAULT", "mine")})
        self.assertEqual(self.platform.changes(), [])

    def test_restoring_a_missing_portal_does_not_reactivate_its_saved_shared_theme(self):
        saved = self.portals.capture(self.rows)
        del self.objects["/portals/owned-portal"]
        self.portals.apply(saved)
        put = self.platform.sent("PUT", AUTO + "/portals")[-1]
        self.assertNotIn("activeThemeHrid", put.json)
        self.assertEqual({row["id"] for row in common.created()}, {row["id"] for row in self.rows})
