"""Private runtime state and persona identities stay separate from the shared Lab's controls.

Compiler and Docker lifecycle behavior live in catalogs/test_independent and independent_docker.
"""

import copy

from lab_ui import paths
from lab_ui.catalogs import independent
from lab_ui.library import runtime_settings
from lab_ui.server import runtimes, personas
from tests import Case


class RuntimeState(Case):
    def setUp(self):
        self.folder(paths)
        runtime_settings.initialize(["local/a", "local/b"])
        runtime_settings.save(pid="local/a", selected="independent")
        self.project = {"playgrounds": {"local/a": {"seed": None}, "local/b": {}}, "links": []}
        self.body = {"playgrounds": [{"id": "local/a", "name": "A"}, {"id": "local/b", "name": "B"}],
                     "services": {}, "problems": []}

    def test_existing_shared_items_are_unchanged_and_private_unprepared_items_can_start(self):
        self.patch(independent, "read", return_value=None)
        result = runtimes.overlay(copy.deepcopy(self.body), self.project)
        a, b = result["playgrounds"]
        self.assertTrue(a["action"]["enabled"])
        self.assertFalse(a["on"])
        self.assertEqual(a["services"], [])
        self.assertEqual(b, {"id": "local/b", "name": "B", "runtime": {"mode": "shared"}})

    def test_damaged_runtime_does_not_hide_the_other_playgrounds_or_offer_an_unsafe_start(self):
        self.patch(independent, "read", side_effect=ValueError("Damaged private record"))
        result = runtimes.overlay(copy.deepcopy(self.body), self.project)
        self.assertFalse(result["playgrounds"][0]["action"]["enabled"])
        self.assertEqual(result["problems"][0]["message"], "Damaged private record")
        self.assertEqual(result["playgrounds"][1]["id"], "local/b")

    def test_private_personas_are_distinct_and_unknown_private_keys_never_use_the_shared_api(self):
        self.patch(runtimes.library_project, "load", return_value=self.project)
        self.patch(runtimes.frames, "declared", return_value=[{"persona": "user-a"}])
        self.patch(independent, "read", return_value={"ports": {str(personas.API[1]): 32123}})
        key = runtimes.persona_key("local/a", "user-a")
        self.assertLessEqual(len(key), 63)
        self.assertNotEqual(key, runtimes.persona_key("local/b", "user-a"))
        self.assertEqual(runtimes.persona_api(key), (personas.API[0], 32123))
        self.assertIsNone(runtimes.persona_api("runtime-missing--unknown"))
        self.assertEqual(runtimes.persona_api("shared-user"), personas.API)

    def test_private_state_exposes_only_display_fields_and_restart_never_replays_seed(self):
        self.project["playgrounds"]["local/a"]["name"] = "A"
        value = {"project": "private-test", "ports": {}, "oneshots": ["seed"],
                 "services": {"db": {"label": "Database", "image": "redis:8", "display": {"command": "--token=do-not-return"}},
                              "seed": {"label": "Seed", "image": "fixture:local"}}}
        rows = {"db": {"Name": "private-test-db-1", "State": "running"}, "seed": {"State": "exited", "ExitCode": 0}}
        self.patch(independent, "read", return_value=value)
        self.patch(independent, "created", return_value=[])
        self.patch(runtimes, "_snapshot", return_value=({"prepared": True, "running": True, "on": True}, rows))
        self.patch(runtimes.frames, "declared", return_value=[])
        result = runtimes.overlay(copy.deepcopy(self.body), self.project)
        self.assertNotIn("do-not-return", repr(result))
        self.assertTrue(result["playgrounds"][0]["up"])
        value["initialized"] = True
        rows.pop("seed")
        resumed = runtimes.overlay(copy.deepcopy(self.body), self.project)
        self.assertTrue(resumed["playgrounds"][0]["up"], "Removed completed one-shots must not leave the runtime partly running")
        jobs = self.patch(runtimes, "JOBS")
        runtimes.action(self.project, "local/a", "restart")
        _, commands, _ = self.started(jobs)
        self.assertEqual(commands[0][-2:], ["restart", "db"])

    def test_central_inventory_keeps_stopped_and_missing_source_runtimes_and_counts_exclusive_storage(self):
        records = [("local/a", {"active": False, "label": "A", "tested": {"versions": {"VERSION": "1"}}, "services": {}}),
                   ("local/orphan", {"active": False, "label": "Orphan", "services": {}})]
        self.patch(runtimes.runtime_storage, "known", return_value=(records, []))
        self.patch(runtimes.storage, "usage", return_value={"volumes": {"own-volume": 200}, "shared": [{"kind": "Images", "bytes": 9000}]})
        self.patch(runtimes.storage, "resources", return_value={"containers": [], "volumes": [], "networks": []})
        private = {"resources": {"containers": [], "volumes": [{"name": "own-volume"}], "networks": []},
                   "local": {"bytes": 100}, "error": None, "running": False}
        self.patch(runtimes.runtime_storage, "inventory", return_value=private)
        self.patch(runtimes.storage, "load", return_value={})
        result = runtimes.listing(self.project)
        rows = {row["id"]: row for row in result["runtimes"]}
        self.assertEqual(rows["local/a"]["volume_bytes"], 200)
        self.assertEqual(rows["local/a"]["local_bytes"], 100)
        self.assertTrue(rows["local/orphan"]["cleanup"]["data"]["enabled"])
        self.assertFalse(rows["local/orphan"]["source"])
        self.assertEqual(rows["local/orphan"]["name"], "Orphan")
        self.assertEqual(result["shared_storage"][0]["bytes"], 9000)

    def test_missing_source_runtime_can_be_stopped_without_rewriting_catalogs_or_choices(self):
        self.patch(independent, "read", return_value={"id": "local/orphan"})
        stop = self.patch(independent, "stop", return_value="job")
        self.assertEqual(runtimes.managed_action(self.project, "local/orphan", "stop"), "job")
        stop.assert_called_once_with("local/orphan")

    def test_unreadable_record_is_not_presented_as_an_empty_fresh_runtime(self):
        self.patch(runtimes.runtime_storage, "known", return_value=([], [{
            "file": (independent.folder("local/a") / "runtime.json").relative_to(self.root).as_posix(), "message": "Damaged record"}]))
        self.patch(runtimes.storage, "usage", return_value={"volumes": {}, "shared": []})
        self.patch(runtimes.storage, "resources", return_value={"containers": [], "volumes": [], "networks": []})
        self.patch(runtimes.storage, "load", return_value={})
        row = next(row for row in runtimes.listing(self.project)["runtimes"] if row["id"] == "local/a")
        self.assertEqual(row["problem"], "Damaged record")
        self.assertIsNone(row["volume_bytes"])
        self.assertFalse(row["cleanup"]["data"]["enabled"])
