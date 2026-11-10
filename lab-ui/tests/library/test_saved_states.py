"""Saved playground states: metadata, safety checks, and save-before-delete ordering.

No Docker daemon is needed for these tests. The engine's archive and Docker commands
are separately tested with mocks so failure cannot silently run seed deletion.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lab_ui import paths
from lab_ui.docker.jobs import JobFailed
from lab_ui.gravitee import places
from lab_ui.library import playgrounds, saved_states


class SavedStates(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / ".lab" / "saved-states").mkdir(parents=True)
        (self.root / "docker-compose.yml").write_text("name: gravitee\n", encoding="utf-8")
        self.patch_root = mock.patch.object(saved_states, "ROOT", self.root)
        self.patch_states = mock.patch.object(saved_states, "STATES", self.root / ".lab" / "saved-states")
        self.patch_root.start()
        self.patch_states.start()
        self.addCleanup(self.patch_root.stop)
        self.addCleanup(self.patch_states.stop)
        self.sid1 = "1" * 32
        self.sid2 = "2" * 32

    def record(self, pid, sid, name):
        folder = saved_states._location(pid, sid)
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text(json.dumps({
            "schema": 1, "complete": True, "playground": pid, "id": sid,
            "name": name, "created": f"2026-11-{9 if sid == self.sid1 else 10}T00:00:00+00:00",
            "images": [], "size": 1,
        }), encoding="utf-8")
        return folder

    def test_many_states_belong_to_one_playground_without_a_new_global_profile(self):
        self.record("welcome", self.sid1, "Before edits")
        self.record("welcome", self.sid2, "After edits")
        self.record("other", "3" * 32, "Other playground")
        self.assertEqual([row["name"] for row in saved_states.list_states("welcome")],
                         ["After edits", "Before edits"])
        self.assertEqual(len(saved_states.list_states("other")), 1)

    def test_pending_corrupt_and_linked_states_are_never_listed(self):
        folder = self.record("welcome", self.sid1, "Good")
        bad = saved_states._location("welcome", self.sid2)
        bad.mkdir(parents=True)
        (bad / "manifest.json").write_text('{"complete":false}', encoding="utf-8")
        (saved_states._home("welcome") / (".pending-" + self.sid2)).mkdir()
        (saved_states._home("welcome") / ("3" * 32)).symlink_to(folder, target_is_directory=True)
        self.assertEqual([row["name"] for row in saved_states.list_states("welcome")], ["Good"])

    def test_delete_one_state_preserves_the_other_and_live_configuration(self):
        first = self.record("welcome", self.sid1, "Before edits")
        second = self.record("welcome", self.sid2, "After edits")
        (self.root / ".env").write_text("KEY=unchanged\n", encoding="utf-8")
        with mock.patch.object(saved_states.JOBS, "current", None):
            result = saved_states.delete_state({"playgrounds": {"welcome": {}}}, "welcome", self.sid1)
        self.assertEqual(result, {"deleted": self.sid1})
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual((self.root / ".env").read_text(), "KEY=unchanged\n")

    def test_delete_rejects_wrong_playground_and_invalid_state_id(self):
        self.record("welcome", self.sid1, "Good")
        with self.assertRaises(paths.NotFound):
            saved_states.delete_state({"playgrounds": {}}, "welcome", self.sid1)
        with self.assertRaises(ValueError):
            saved_states._location("welcome", "../../outside")
        with self.assertRaises(ValueError):
            saved_states.delete_state({"playgrounds": {"other": {}}}, "other", self.sid1)

    def test_external_or_unknown_writable_mount_refuses_an_exact_save(self):
        outside = {"Type": "bind", "RW": True, "Source": "/var/lib/foreign-data",
                   "Destination": "/data"}
        with self.assertRaisesRegex(JobFailed, "external writable bind mount"):
            saved_states._paths_to_save([outside])
        untracked = {"Type": "bind", "RW": True, "Source": str(self.root / "app-data"),
                     "Destination": "/data"}
        with self.assertRaisesRegex(JobFailed, "needs a storage adapter"):
            saved_states._paths_to_save([untracked])
        tracked = {"Type": "bind", "RW": True, "Source": str(self.root / ".lab/catalogs/sample/.state"),
                   "Destination": "/state"}
        self.assertIn(".lab/catalogs/sample/.state", saved_states._paths_to_save([tracked]))

    def test_manifest_integrity_is_checked_before_restore(self):
        folder = self.record("welcome", self.sid1, "Good")
        archive = folder / "config.tar.gz"
        archive.write_bytes(b"test")
        data = json.loads((folder / "manifest.json").read_text())
        data.update(core_sha256=saved_states._digest(self.root / "docker-compose.yml"),
                    config={"file": "config.tar.gz", "sha256": saved_states._digest(archive)},
                    volumes=[])
        (folder / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
        with mock.patch.object(saved_states, "_docker", return_value=""):
            self.assertEqual(saved_states._verified("welcome", self.sid1)[1]["name"], "Good")
            archive.write_bytes(b"corrupted")
            with self.assertRaisesRegex(ValueError, "integrity"):
                saved_states._verified("welcome", self.sid1)

    def test_save_before_delete_and_delete_never_runs_after_failed_save(self):
        events = []
        job = object()
        def delete(job):
            events.append("delete")
        wrapped = playgrounds._save_before_delete(
            {"playgrounds": {"welcome": {}}}, {"id": "welcome"}, delete)
        with mock.patch.object(saved_states, "capture", side_effect=lambda *args: events.append("save")):
            wrapped(job)
        self.assertEqual(events, ["save", "delete"])
        events.clear()
        with mock.patch.object(saved_states, "capture", side_effect=JobFailed("No space left")):
            with self.assertRaises(JobFailed):
                wrapped(job)
        self.assertEqual(events, [])

    def test_snapshot_failure_restarts_containers_and_publishes_no_state(self):
        pid = "welcome"
        class Dummy:
            lines = []
            def step(self, name):
                pass
        job = Dummy()
        events = []
        with mock.patch.object(places, "recorded", return_value=[]), \
             mock.patch.object(saved_states, "_runtime",
                               return_value=(["gravitee_data-mongo"], [], ["container1"], [])), \
             mock.patch.object(saved_states, "_stop", side_effect=lambda *a: events.append("stop")), \
             mock.patch.object(saved_states, "_restart", side_effect=lambda *a: events.append("restart")), \
             mock.patch.object(saved_states, "_capture_stopped", side_effect=JobFailed("Snapshot failed")):
            with self.assertRaises(JobFailed):
                saved_states.capture(job, {"playgrounds": {pid: {}}}, pid)
        self.assertEqual(events, ["stop", "restart"])
        self.assertEqual(saved_states.list_states(pid), [])


if __name__ == "__main__":
    unittest.main()
