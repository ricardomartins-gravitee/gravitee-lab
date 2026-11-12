"""Consumer scope is a subscription graph, including shared apps and historical credentials.

These deterministic API cases cover ownership changes, collisions and missing users before
any restore write. The real Docker workflow covers the Portal and gateway lifecycle.
"""

import copy
import re

from lab_seeder import apim, consumer_states
from lab_seeder.common import SeedError
from lab_seeder.testkit import Seed

V1 = "/management/organizations/DEFAULT/environments/DEFAULT"
V2 = "/management/v2/organizations/DEFAULT/environments/DEFAULT"
API = {"product": "apim", "kind": "api", "label": "APIs", "name": "Mine", "id": "mine"}
APP = {"id": "app", "name": "Consumer", "description": "Saved settings", "status": "ACTIVE",
       "owner": {"id": "developer"}, "settings": {"app": {"type": "SIMPLE"}}, "api_key_mode": "EXCLUSIVE"}
SUB = {"id": "sub", "api": {"id": "mine"}, "application": {"id": "app"}, "plan": {"id": "plan"}, "status": "ACCEPTED"}
KEY = {"id": "key-id", "key": "invented-demo-credential", "revoked": False, "expireAt": None, "subscriptions": [{"id": "sub"}]}


class ConsumerStates(Seed):
    def setUp(self):
        super().setUp()
        self.application = copy.deepcopy(APP)
        self.subscriptions = [copy.deepcopy(SUB)]
        self.credentials = [copy.deepcopy(KEY)]
        self.app_links = [{"id": "sub", "api": "mine", "status": "ACCEPTED"}]
        self.members = [{"id": "developer", "role": "PRIMARY_OWNER"}]
        self.user_exists = True
        self.key_available = True
        self.answer({
            ("GET", V1 + "/applications/app"): lambda call: (200, copy.deepcopy(self.application)),
            ("GET", V1 + "/applications/app/members"): lambda call: (200, copy.deepcopy(self.members)),
            ("GET", V1 + "/applications/app/metadata"): (200, [{"key": "team", "value": "Mine"}]),
            ("GET", V1 + "/applications/app/subscriptions"): lambda call: (200, {"data": copy.deepcopy(self.app_links)}),
            ("GET", V1 + "/applications/app/apikeys"): lambda call: (200, copy.deepcopy(self.credentials)),
            ("GET", V2 + "/apis/mine/subscriptions"): lambda call: (200, {"data": copy.deepcopy(self.subscriptions)}),
            ("GET", re.compile(V2 + r"/apis/mine/subscriptions/[^/]+")): self.subscription,
            ("GET", V2 + "/apis/mine/subscriptions/sub/api-keys"): lambda call: (200, {"data": copy.deepcopy(self.credentials)}),
            ("GET", "/management/organizations/DEFAULT/users/developer"): lambda call: (200 if self.user_exists else 404, {}),
            ("POST", V2 + "/apis/mine/subscriptions/_verify"): lambda call: (200, {"ok": self.key_available}),
        }, created=[API])
        self.consumers = consumer_states.Consumers(apim.ApiManagement())

    def subscription(self, call):
        ident = call.path.rsplit("/", 1)[-1]
        value = next((row for row in self.subscriptions if row["id"] == ident), None)
        return (200, copy.deepcopy(value)) if value else (404, {})

    def capture(self, records=None, foreign=(), anchors=None):
        return self.consumers.capture(records if records is not None else [API], foreign, anchors)

    def validate(self, saved, foreign=()):
        current = self.capture(anchors={"resources": [{"record": API}], "consumers": saved})
        self.consumers.validate(saved, current, [API], foreign, {("DEFAULT", "mine")})

    def assert_readonly(self):
        self.assertTrue(all(path.endswith("/_verify") for method, path in self.platform.changes()))

    def test_capture_includes_all_statuses_members_metadata_and_credentials_without_adopting_apps(self):
        for status in consumer_states.STATUSES:
            with self.subTest(status=status):
                self.subscriptions[0]["status"] = status
                saved = self.capture()
                self.assertTrue(saved["applications"][0]["managed"])
                self.assertEqual(saved["applications"][0]["entity"], APP)
                self.assertEqual(saved["applications"][0]["members"], self.members)
                self.assertEqual(saved["applications"][0]["metadata"][0]["value"], "Mine")
                self.assertEqual(saved["subscriptions"][0]["entity"]["status"], status)
                self.assertEqual(saved["subscriptions"][0]["keys"], [KEY])
        calls = self.platform.sent("GET", V2 + "/apis/mine/subscriptions")
        self.assertTrue(all(set(call.params["statuses"].split(",")) == set(consumer_states.STATUSES) for call in calls))
        self.assert_readonly()

    def test_external_or_foreign_recorded_app_is_a_readonly_reference_including_closed_links(self):
        for mode in ("other-api", "api-product", "foreign-record"):
            with self.subTest(mode=mode):
                self.app_links = [{"id": "sub", "api": "mine"}]
                foreign = []
                if mode == "other-api":
                    self.app_links.append({"id": "other-sub", "api": "other", "status": "CLOSED"})
                elif mode == "api-product":
                    self.app_links.append({"id": "other-sub", "referenceId": "mine", "referenceType": "API_PRODUCT"})
                else:
                    foreign = [{"product": "apim", "kind": "application", "env": "DEFAULT", "id": "app"}]
                item = self.capture(foreign=foreign)["applications"][0]
                self.assertFalse(item["managed"])
                self.assertEqual(item["entity"], {"id": "app", "status": "ACTIVE"})
                self.assertNotIn("members", item)
        self.assertFalse(self.platform.sent("GET", V1 + "/applications/app/members"))
        self.assert_readonly()

    def test_save_and_restore_refuse_before_writes_when_private_app_becomes_shared(self):
        saved = self.capture()
        self.app_links.append({"id": "other-sub", "api": "other"})
        with self.assertRaisesRegex(SeedError, "now shared"):
            self.validate(saved)
        seeded = {"product": "apim", "kind": "application", "label": "applications", "name": "Consumer", "id": "app"}
        with self.assertRaisesRegex(SeedError, "shared with another"):
            self.capture([API, seeded])
        self.assert_readonly()

    def test_foreign_subscription_and_shared_key_refuse_capture_without_touching_consumers(self):
        foreign = [{"product": "apim", "kind": "subscription", "id": "sub"}]
        with self.assertRaisesRegex(SeedError, "Another playground"):
            self.capture(foreign=foreign)
        self.credentials[0]["subscriptions"].append({"id": "other-sub"})
        with self.assertRaisesRegex(SeedError, "shared outside"):
            self.capture()
        self.assert_readonly()

    def test_key_taken_by_another_consumer_and_deleted_member_refuse_before_restore_writes(self):
        saved = self.capture()
        self.credentials = []
        self.key_available = False
        with self.assertRaisesRegex(SeedError, "another consumer"):
            self.validate(saved)
        self.credentials = [copy.deepcopy(KEY)]
        self.user_exists = False
        with self.assertRaisesRegex(SeedError, "member no longer exists"):
            self.validate(saved)
        self.assert_readonly()

    def test_key_linked_elsewhere_after_api_cleanup_is_detected_even_without_live_subscription(self):
        saved = self.capture()
        self.subscriptions = []
        self.credentials[0]["subscriptions"] = [{"id": "other-sub"}]
        with self.assertRaisesRegex(SeedError, "now grants access outside"):
            self.validate(saved)
        self.assert_readonly()

    def test_cleanup_anchors_find_the_app_after_ownership_records_and_api_are_removed(self):
        saved = self.capture()
        self.subscriptions = []
        anchored = self.capture(records=[], anchors={"resources": [{"record": API}], "consumers": saved})
        self.assertEqual(anchored["subscriptions"], [])
        self.assertEqual(anchored["applications"][0]["entity"], APP)
        self.assert_readonly()

    def test_saved_recorded_subscription_outside_owned_apis_cannot_be_silently_lost(self):
        row = {"product": "apim", "kind": "subscription", "label": "subscriptions", "name": "Outside", "id": "outside", "api": "other"}
        with self.assertRaisesRegex(SeedError, "outside this playground"):
            self.capture([API, row])
        self.assert_readonly()

    def test_revocation_keeps_expired_saved_keys_and_blocks_keys_issued_after_checkpoint(self):
        saved = self.capture()["subscriptions"][0]
        saved["keys"][0].update(expireAt="2000-01-01T00:00:00Z", expired=True)
        self.credentials[0] = copy.deepcopy(saved["keys"][0])
        self.credentials.append({**copy.deepcopy(KEY), "id": "new-key", "key": "invented-new-demo-credential"})
        self.answer({("POST", V2 + "/apis/mine/subscriptions/sub/api-keys/new-key/_revoke"): 204})
        self.consumers.restore_keys(saved)
        self.assertEqual(self.platform.changes(), [("POST", V2 + "/apis/mine/subscriptions/sub/api-keys/new-key/_revoke")])

    def test_recreated_subscriptions_use_portal_origin_and_keep_private_identity_aliases(self):
        saved = self.capture()["subscriptions"][0]
        saved["entity"].update(status="PENDING", origin="MANAGEMENT", consumerMessage="Original request")
        saved["keys"] = []
        self.subscriptions = []

        def create(call):
            self.assertEqual(call.json["request"], "Original request")
            self.subscriptions = [{**copy.deepcopy(SUB), "id": "replacement", "status": "PENDING", "origin": "MANAGEMENT"}]
            return 201, {"id": "replacement"}

        self.platform.routes.update({
            ("POST", "/portal/environments/DEFAULT/subscriptions"): create,
            ("PUT", "/portal/environments/DEFAULT/subscriptions/replacement"): (200, {}),
            ("PUT", V2 + "/apis/mine/subscriptions/spec/_import"): (200, {}),
            ("PUT", V2 + "/apis/mine/subscriptions/replacement"): (200, {}),
            ("GET", V2 + "/apis/mine/subscriptions/replacement/api-keys"): (200, {"data": []}),
        })
        self.consumers.import_subscription(saved)
        live = self.consumers.subscription(saved["record"])
        self.assertEqual((live["id"], live["origin"]), ("replacement", "MANAGEMENT"))
        reloaded = consumer_states.Consumers(apim.ApiManagement())
        self.assertEqual(reloaded.resolve(saved["record"])["id"], "replacement")
        self.assertEqual(reloaded.alias_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(reloaded.owned({"resources": [], "ownership": [saved["record"]]})[0]["id"], "replacement")
        self.assertTrue(all(call.json["id"] == "replacement" for call in
                            self.platform.sent("PUT", V2 + "/apis/mine/subscriptions/spec/_import")))
        # A later recreation follows the whole chain without changing another API's id.
        reloaded.link({**saved["record"], "id": "replacement"}, "latest")
        self.assertEqual(reloaded.resolve(saved["record"])["id"], "latest")
        self.assertEqual(reloaded.resolve({**saved["record"], "api": "other"})["id"], "sub")

    def test_aliases_cannot_bypass_foreign_ownership_or_create_a_cycle(self):
        saved = self.capture()
        self.consumers.link(saved["subscriptions"][0]["record"], "replacement")
        foreign = [{"product": "apim", "kind": "subscription", "id": "replacement"}]
        with self.assertRaisesRegex(SeedError, "exclusively"):
            self.validate(saved, foreign=foreign)
        self.consumers.link({**saved["subscriptions"][0]["record"], "id": "replacement"}, "sub")
        with self.assertRaisesRegex(SeedError, "cycle"):
            self.consumers.resolve(saved["subscriptions"][0]["record"])
