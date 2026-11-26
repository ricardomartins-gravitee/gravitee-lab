"""Named children preserve Shared defaults and legacy private storage without reassigning either."""

import json

from lab_ui import paths
from lab_ui.catalogs import independent
from lab_ui.library import runtime_settings
from tests import Case


class RuntimeSettings(Case):
    def setUp(self):
        self.folder(paths)

    def test_shared_is_always_normal_and_multiple_named_children_have_distinct_identities(self):
        runtime_settings.initialize(["local/a"])
        demo = runtime_settings.create("local/a", "Demo")
        sandbox = runtime_settings.create("local/a", "Sandbox")
        self.assertNotEqual(demo["id"], sandbox["id"])
        self.assertEqual(runtime_settings.mode("local/a"), "shared")
        self.assertEqual(runtime_settings.mode("local/new"), "shared")
        for child in (demo, sandbox):
            self.assertEqual(runtime_settings.mode(child["id"]), "independent")
            self.assertEqual(runtime_settings.source(child["id"]), "local/a")
            self.assertFalse(independent.folder(child["id"]).exists(), "creation never starts or prepares containers")
        runtime_settings.initialize(["local/a", "local/new"])
        self.assertEqual(len(runtime_settings.instances()), 2)
        self.assertEqual(runtime_settings.read()["default"], "shared")

    def test_legacy_running_data_gets_a_child_without_changing_its_storage_or_project(self):
        pid = "local/old"
        self.write(runtime_settings.FILE, json.dumps({"schema": 1, "default": "independent", "playgrounds": {pid: "independent"}}))
        folder, project = independent.folder(pid), independent.name(pid)
        original = json.dumps({"schema": 1, "id": pid, "project": project, "active": True,
                              "catalogs": ["local"], "units": [["playground", pid]]})
        self.write(str((folder / "runtime.json").relative_to(self.root)), original)
        runtime_settings.initialize([pid])
        rid, row = next(iter(runtime_settings.instances().items()))
        self.assertEqual(row["playground"], pid)
        self.assertEqual(runtime_settings.mode(pid), "shared")
        self.assertEqual(independent.folder(rid), folder)
        self.assertEqual(independent.name(rid), project)
        self.assertEqual((folder / "runtime.json").read_text(), original)
        self.assertTrue(independent.read(rid)["active"])
        before = (self.root / runtime_settings.FILE).read_bytes()
        runtime_settings.initialize([pid])
        self.assertEqual((self.root / runtime_settings.FILE).read_bytes(), before)

    def test_invalid_and_duplicate_names_ids_and_damaged_registry_never_reassign_data(self):
        runtime_settings.create("local/a", "Demo")
        before = (self.root / runtime_settings.FILE).read_bytes()
        for pid, name in (("../outside", "Demo"), ("local/a", "demo"), ("local/a", " Shared "),
                          ("local/a", ""), ("local/a", "x" * 61), ("local/a", None), ("local/a", "a\nb")):
            with self.subTest(pid=pid, name=name), self.assertRaises((ValueError, paths.NotFound)):
                runtime_settings.create(pid, name)
        self.assertEqual((self.root / runtime_settings.FILE).read_bytes(), before)
        self.assertEqual(runtime_settings.create("local/b", "Demo")["playground"], "local/b")
        self.write(runtime_settings.FILE, "broken")
        with self.assertRaisesRegex(ValueError, "cannot be read"):
            runtime_settings.mode("local/a")

    def test_active_private_dependencies_hold_catalog_changes_until_explicitly_stopped(self):
        self.write(".lab/runtimes/test/runtime.json", json.dumps({"active": True, "catalogs": ["local", "linked"], "units": [["playground", "local/a"]]}))
        self.assertEqual(runtime_settings.active_catalogs(), {"local", "linked"})
        self.assertTrue(runtime_settings.holds("playground", "local/a"))
        self.assertFalse(runtime_settings.holds("playground", "local/b"))
        self.write(".lab/runtimes/test/runtime.json", json.dumps({"active": False, "catalogs": ["linked"]}))
        self.assertEqual(runtime_settings.active_catalogs(), set())
        self.write(".lab/runtimes/test/runtime.json", "broken")
        with self.assertRaisesRegex(ValueError, "catalog changes are held"):
            runtime_settings.active_catalogs()
