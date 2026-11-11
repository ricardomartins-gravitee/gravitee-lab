"""Selective management API checkpoints preserve unowned resources and roll back only their own writes.

The fake platform makes ownership collisions, partial imports and failed recovery deterministic;
the Docker Gravitee scenario verifies the real export/import contract and gateway behavior.
"""

import copy
import json
import re
from unittest import mock

from lab_seeder import checkpoints, common
from lab_seeder.common import SeedError
from lab_seeder.testkit import Seed

V2 = "/management/v2/organizations/DEFAULT/environments/DEFAULT"
V1 = "/management/organizations/DEFAULT/environments/DEFAULT"
ROW = {"product": "apim", "kind": "api", "label": "APIs", "name": "Mine", "id": "mine", "digest": "seed-digest"}
ENTITY = {"id": "mine", "name": "Mine edited", "description": "Saved changes", "crossId": "cross-mine",
          "definitionVersion": "V4", "type": "PROXY", "state": "STARTED", "deploymentState": "DEPLOYED",
          "lifecycleState": "PUBLISHED", "listeners": [{"type": "HTTP", "paths": [{"path": "/mine/"}]}]}


class Checkpoints(Seed):
    def setUp(self):
        super().setUp()
        self.entities = {"mine": copy.deepcopy(ENTITY), "other": {**copy.deepcopy(ENTITY), "id": "other",
                         "name": "Other", "crossId": "cross-other", "listeners": [{"type": "HTTP", "paths": [{"path": "/other/"}]}]}}
        self.other = copy.deepcopy(self.entities["other"])
        self.path = self.state.parent / "resources.json"
        (self.path.parent / "foreign.json").write_text("[]")
        self.subscriptions = []
        self.fail_imports = 0

        def entity(call):
            ident = call.path.rsplit("/", 1)[-1]
            if call.method == "DELETE":
                self.entities.pop(ident, None)
                return 204
            if call.method == "PUT":
                self.entities[ident] = copy.deepcopy(call.json)
            return (200, copy.deepcopy(self.entities[ident])) if ident in self.entities else (404, {})

        def exported(call):
            ident = call.path.split("/apis/", 1)[1].split("/", 1)[0]
            return 200, {"api": copy.deepcopy(self.entities[ident]), "plans": [{"name": "Keyless", "status": "PUBLISHED"}]}

        def imported(call):
            if self.fail_imports:
                self.fail_imports -= 1
                self.entities.setdefault("mine", copy.deepcopy(ENTITY))["description"] = "Partial failed import"
                return 400, {"error": "Fake import refused"}
            api = copy.deepcopy(call.json["api"])
            self.entities[api["id"]] = api
            return 200, api

        self.answer({
            ("GET", V2 + "/apis"): lambda call: (200, {"data": copy.deepcopy(list(self.entities.values()))}),
            ("GET", re.compile(V2 + r"/apis/[^/]+")): entity,
            ("PUT", re.compile(V2 + r"/apis/[^/]+")): entity,
            ("DELETE", re.compile(V1 + r"/apis/[^/]+")): entity,
            ("DELETE", re.compile(V2 + r"/apis/[^/]+")): entity,
            ("GET", re.compile(V2 + r"/apis/[^/]+/_export/definition")): exported,
            ("GET", re.compile(V2 + r"/apis/[^/]+/plans")): (200, {"data": [{"id": "plan-mine", "name": "Keyless"}]}),
            ("GET", re.compile(V2 + r"/apis/[^/]+/subscriptions")): lambda call: (200, {"data": self.subscriptions}),
            ("POST", V2 + "/apis/_import/definition"): imported,
            ("PUT", re.compile(V2 + r"/apis/[^/]+/_import/definition")): imported,
            ("POST", re.compile(V2 + r"/apis/[^/]+/(deployments|_start|_stop)")): 204,
        }, created=[ROW])

    def save(self):
        checkpoints.run("save", self.path)
        return json.loads(self.path.read_text())

    def test_export_and_restore_preserve_only_owned_api_identity_edits_and_plan_ids(self):
        saved = self.save()
        self.assertEqual([item["record"]["id"] for item in saved["resources"]], ["mine"])
        self.assertEqual(saved["resources"][0]["definition"]["plans"][0]["id"], "plan-mine")
        self.entities["mine"]["description"] = "Later edit"
        self.entities["other"]["description"] = "Other playground edited after save"
        self.other = copy.deepcopy(self.entities["other"])
        checkpoints.run("restore", self.path)
        self.assertEqual(self.entities["mine"]["description"], "Saved changes")
        self.assertEqual(self.entities["other"], self.other)
        self.assertEqual(common.created(), [ROW])
        self.assertFalse((self.path.parent / "previous.json").exists())
        self.assertTrue(all("/apis/mine" in path for method, path in self.platform.changes()))
        # Already started: no second start and no replacement, which would lose children.
        self.assertFalse(self.platform.sent("POST", V2 + "/apis/mine/_start"))
        self.assertFalse(self.platform.sent("DELETE", V1 + "/apis/mine"))

    def test_missing_owned_api_can_be_recreated_after_cleanup_without_modifying_others(self):
        self.save()
        self.entities.pop("mine")
        common.save_created([])
        checkpoints.run("restore", self.path)
        self.assertEqual(self.entities["mine"], ENTITY)
        self.assertEqual(self.entities["other"], self.other)
        self.assertEqual(common.created(), [ROW])

    def test_subscriptions_unsupported_products_shared_dependencies_and_pending_edits_refuse_before_writes(self):
        for change, pattern in (({"product": "am", "kind": "domain"}, "am:domain"),
                                ({"kind": "application"}, "apim:application")):
            common.save_created([{**ROW, **change}])
            with self.subTest(change=change), self.assertRaisesRegex(SeedError, pattern):
                checkpoints.run("save", self.path)
        common.save_created([ROW])
        for value, pattern in (({"groups": ["shared-group"]}, "shared dependencies"),
                               ({"type": "LLM_PROXY"}, "type"),
                               ({"deploymentState": "NEED_REDEPLOY"}, "Deploy")):
            self.entities["mine"] = {**copy.deepcopy(ENTITY), **value}
            with self.subTest(value=value), self.assertRaisesRegex(SeedError, pattern):
                checkpoints.run("save", self.path)
        self.entities["mine"] = copy.deepcopy(ENTITY)
        self.subscriptions = [{"id": "someone-elses-subscription"}]
        with self.assertRaisesRegex(SeedError, "subscriptions"):
            checkpoints.run("save", self.path)
        self.assertEqual(self.platform.changes(), [])
        self.assertFalse(self.path.exists())

    def test_foreign_ownership_id_path_and_target_collisions_refuse_before_any_write(self):
        saved = self.save()
        for mode in ("foreign", "unowned-id", "path", "target"):
            with self.subTest(mode=mode):
                common.save_created([ROW])
                self.entities = {"mine": copy.deepcopy(ENTITY), "other": copy.deepcopy(self.other)}
                candidate = copy.deepcopy(saved)
                foreign = []
                if mode == "foreign":
                    foreign = [ROW]
                elif mode == "unowned-id":
                    common.save_created([])
                elif mode == "path":
                    self.entities["other"]["listeners"] = copy.deepcopy(ENTITY["listeners"])
                else:
                    candidate["target"]["env"] = "ANOTHER"
                self.path.write_text(json.dumps(candidate))
                (self.path.parent / "foreign.json").write_text(json.dumps(foreign))
                with self.assertRaisesRegex(SeedError, "another|unowned"):
                    checkpoints.run("restore", self.path)
                self.assertEqual(self.platform.changes(), [])

    def test_partial_failed_restore_rolls_back_own_api_without_rewinding_other_playground(self):
        self.save()
        self.entities["mine"]["description"] = "Before restore"
        self.entities["other"]["description"] = "Keep latest other playground data"
        self.other = copy.deepcopy(self.entities["other"])
        self.fail_imports = 1
        with self.assertRaises(SeedError):
            checkpoints.run("restore", self.path)
        self.assertEqual(self.entities["mine"]["description"], "Before restore")
        self.assertEqual(self.entities["other"], self.other)
        self.assertFalse((self.path.parent / "previous.json").exists())
        self.fail_imports = 2
        with self.assertRaisesRegex(SeedError, "recovery is incomplete"):
            checkpoints.run("restore", self.path)
        previous = json.loads((self.path.parent / "previous.json").read_text())
        self.assertEqual([item["record"]["id"] for item in previous["resources"]], ["mine"])
        self.assertEqual(self.entities["other"], self.other)

    def test_cli_hides_secrets_from_management_errors_and_reports_completion(self):
        error = common.HttpError("Refused: secret-token", 400, "secret-token")
        with mock.patch.object(checkpoints, "run", side_effect=error), \
             mock.patch("sys.argv", ["checkpoints", "restore", str(self.path)]):
            self.assertEqual(checkpoints.main(), 1)
        self.assertNotIn("secret-token", self.stdout.getvalue())
        result = json.loads((self.path.parent / "result.json").read_text())
        self.assertFalse(result["ok"])
        self.assertIn("400", result["error"])
        self.assertNotIn("secret-token", result["error"])

    def test_import_error_after_creating_api_still_records_and_rolls_back_only_that_api(self):
        self.save()
        self.entities.pop("mine")
        common.save_created([])
        self.fail_imports = 1
        with self.assertRaises(SeedError):
            checkpoints.run("restore", self.path)
        self.assertNotIn("mine", self.entities)
        self.assertEqual(self.entities["other"], self.other)
        self.assertEqual(common.created(), [])
        self.assertFalse((self.path.parent / "previous.json").exists())

    def test_unresolved_seed_creates_are_never_overwritten_by_a_save_or_restore(self):
        self.save()
        pending = {**ROW, "id": None, "count": 0}
        common.save_created([pending])
        for operation in ("save", "restore"):
            with self.subTest(operation=operation), self.assertRaisesRegex(SeedError, "incomplete"):
                checkpoints.run(operation, self.path)
        self.assertEqual(common._read(), [pending])
        self.assertEqual(self.platform.changes(), [])

    def test_private_adapter_outputs_remain_readable_by_the_host_user_on_linux(self):
        (self.path.parent / "owner.json").write_text('{"uid":1001,"gid":1002}')
        with mock.patch("os.geteuid", return_value=0), mock.patch("os.fchown") as chown:
            checkpoints._write(self.path, {"ok": True})
        self.assertEqual(chown.call_args.args[1:], (1001, 1002))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.path.read_text()), {"ok": True})
