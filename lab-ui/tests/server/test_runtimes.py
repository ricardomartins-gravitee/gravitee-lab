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
        jobs = self.patch(runtimes, "JOBS")
        runtimes.action(self.project, "local/a", "restart")
        _, commands, _ = self.started(jobs)
        self.assertEqual(commands[0][-2:], ["restart", "db"])
