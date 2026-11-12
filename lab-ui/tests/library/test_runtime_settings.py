"""Runtime assignment migrates existing playgrounds safely and freezes each chosen mode independently of defaults."""

import json

from lab_ui import paths
from lab_ui.library import runtime_settings
from tests import Case


class RuntimeSettings(Case):
    def setUp(self):
        self.folder(paths)

    def test_existing_playgrounds_stay_shared_and_new_ones_take_the_default_once(self):
        self.assertEqual(runtime_settings.mode("local/old"), "shared")
        runtime_settings.initialize(["local/old"])
        self.assertEqual(runtime_settings.read()["default"], "independent")
        self.assertEqual(runtime_settings.mode("local/old"), "shared")
        self.assertEqual(runtime_settings.assign("local/new"), "independent")
        runtime_settings.save(default="shared")
        self.assertEqual(runtime_settings.mode("local/new"), "independent")
        self.assertEqual(runtime_settings.assign("local/another"), "shared")
        runtime_settings.initialize(["local/new"])
        self.assertEqual(runtime_settings.mode("local/new"), "independent")

    def test_overrides_are_local_and_applying_the_default_is_an_explicit_choice(self):
        runtime_settings.initialize(["local/old"])
        runtime_settings.save(pid="local/old", selected="default")
        self.assertEqual(runtime_settings.mode("local/old"), "independent")
        runtime_settings.save(pid="local/old", selected="shared")
        self.assertEqual(runtime_settings.mode("local/old"), "shared")
        self.assertFalse((self.root / ".lab/catalogs").exists())

    def test_invalid_modes_ids_and_damaged_settings_never_reassign_data(self):
        runtime_settings.initialize(["local/old"])
        before = (self.root / runtime_settings.FILE).read_bytes()
        for args in ({"default": "unknown"}, {"pid": "../outside", "selected": "independent"},
                     {"pid": "local/old", "selected": True}):
            with self.subTest(args=args), self.assertRaises((ValueError, paths.NotFound)):
                runtime_settings.save(**args)
        self.assertEqual((self.root / runtime_settings.FILE).read_bytes(), before)
        self.write(runtime_settings.FILE, json.dumps({"schema": 1, "default": "missing", "playgrounds": {}}))
        with self.assertRaisesRegex(ValueError, "damaged"):
            runtime_settings.mode("local/old")

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
