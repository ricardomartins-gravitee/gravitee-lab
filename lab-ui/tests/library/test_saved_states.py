"""Playground resource snapshot boundaries, legacy rejection and save-before-delete ordering.

These cases use a fake resource adapter. Actual Gravitee exports and Docker process isolation
are covered by saved_states_gravitee and saved_states_docker, outside the unit suite.
"""

import json
import stat
from unittest import mock

from lab_ui import paths
from lab_ui.docker.jobs import JobFailed
from lab_ui.library import layers, playgrounds, saved_states

from .. import Case


class SavedStates(Case):
    def setUp(self):
        self.folder(saved_states, files={
            "docker-compose.yml": "name: gravitee\n", ".env": "GLOBAL=unchanged\n",
            ".lab/catalogs/local/playgrounds/demo/compose.yml": "services: {}\n",
            ".lab/catalogs/local/playgrounds/other/compose.yml": "services: {}\n",
            ".lab/catalogs/local/playgrounds/other/.state/created.json": "[]\n",
        })
        self.patch(saved_states, "STATES", self.root / ".lab/saved-states")
        self.patch(saved_states.places, "recorded", lambda pid: False)
        self.patch(saved_states.places, "read_state", lambda pid, file: "[]")
        self.model = {"playgrounds": {
            "local/demo": {"seed": "demo-seed", "file": ".lab/catalogs/local/playgrounds/demo/compose.yml"},
            "local/other": {"seed": "other-seed", "file": ".lab/catalogs/local/playgrounds/other/compose.yml"},
        }}
        self.job = mock.Mock(lines=[])
        self.sid = "1" * 32

    def adapter(self, job, project, pid, operation, folder):
        self.assertEqual(pid, "local/demo")
        if operation == "save":
            (folder / "resources.json").write_text('{"scope":"playground","resources":[]}', encoding="utf-8")

    def save(self, name="First"):
        with mock.patch.object(saved_states, "_execute", side_effect=self.adapter):
            return saved_states.capture(self.job, self.model, "local/demo", name)

    def test_multiple_saves_and_deletion_touch_only_selected_checkpoint_files(self):
        first, second = self.save("First"), self.save("Second")
        before = (self.root / ".env").read_bytes()
        self.assertEqual(len(saved_states.list_states("local/demo")), 2)
        self.assertEqual(saved_states.list_states("local/other"), [])
        with mock.patch.object(saved_states.JOBS, "current", None):
            result = saved_states.delete_state(self.model, "local/demo", first)
        self.assertEqual(result["deleted"], first)
        self.assertEqual([row["id"] for row in saved_states.list_states("local/demo")], [second])
        self.assertEqual((self.root / ".env").read_bytes(), before)

    def test_legacy_whole_lab_checkpoints_are_retained_but_cannot_be_listed_or_restored(self):
        folder = saved_states._location("local/demo", self.sid)
        folder.mkdir(parents=True)
        manifest = folder / "manifest.json"
        manifest.write_text(json.dumps({"schema": 1, "complete": True, "id": self.sid,
                                        "playground": "local/demo", "volumes": [{"name": "gravitee_mongo"}]}))
        before = manifest.read_bytes()
        self.assertEqual(saved_states.list_states("local/demo"), [])
        with mock.patch.object(saved_states, "_execute") as execute:
            with self.assertRaisesRegex(ValueError, "whole Lab"):
                saved_states.restore(self.model, "local/demo", self.sid, confirmed=True)
        execute.assert_not_called()
        self.assertEqual(manifest.read_bytes(), before)

    def test_invalid_metadata_paths_links_and_corruption_never_reach_adapter(self):
        sid = self.save()
        folder = saved_states._location("local/demo", sid)
        original = json.loads((folder / "manifest.json").read_text())
        for key, value in (("created", None), ("created", "not-a-date"), ("name", None), ("size", -1),
                           ("schema", True), ("scope", "lab"), ("complete", False)):
            with self.subTest(key=key):
                (folder / "manifest.json").write_text(json.dumps({**original, key: value}))
                self.assertEqual(saved_states.list_states("local/demo"), [])
        (folder / "manifest.json").write_text(json.dumps(original))
        (folder / "resources.json").write_text("corrupt")
        with self.assertRaisesRegex(ValueError, "integrity"):
            saved_states._verified("local/demo", sid)
        with self.assertRaises(ValueError):
            saved_states._location("local/demo", "../../outside")
        with self.assertRaises(paths.NotFound):
            saved_states.restore(self.model, "missing", sid, confirmed=True)
        (folder / "manifest.json").unlink()
        (folder / "manifest.json").symlink_to(self.root / ".env")
        self.assertEqual(saved_states.list_states("local/demo"), [])

    def test_linked_playground_inputs_are_bound_without_scanning_or_replacing_other_items(self):
        folder = self.root / ".lab/catalogs/local/playgrounds/demo"
        linked = self.root / "worktree"
        folder.rename(linked)
        folder.symlink_to(linked, target_is_directory=True)
        sid = self.save()
        other = self.root / ".lab/catalogs/local/playgrounds/other/compose.yml"
        other.write_text("Edited another playground\n")
        self.assertEqual(saved_states._verified("local/demo", sid)[1]["id"], sid)
        self.assertTrue(folder.is_symlink())
        (linked / "compose.yml").write_text("Edited this playground\n")
        with self.assertRaisesRegex(ValueError, "source or links changed"):
            saved_states._verified("local/demo", sid)
        self.assertEqual(other.read_text(), "Edited another playground\n")

    def test_ownership_symlinks_refuse_without_writing_to_another_playground(self):
        state = self.root / ".lab/catalogs/local/playgrounds/demo/.state"
        state.symlink_to(self.root / ".lab/catalogs/local/playgrounds/other/.state", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "state folder"):
            self.save()

    def test_failed_save_publishes_nothing_and_never_runs_cleanup(self):
        calls = []
        wrapped = playgrounds._save_before_delete(self.model, {"id": "local/demo"}, lambda job: calls.append("delete"))
        with mock.patch.object(saved_states, "_execute", side_effect=JobFailed("Unsupported resources")):
            with self.assertRaisesRegex(JobFailed, "Unsupported"):
                wrapped(self.job)
        self.assertEqual(calls, [])
        self.assertEqual(saved_states.list_states("local/demo"), [])
        self.assertEqual(list(saved_states._home("local/demo").iterdir()), [])
        with mock.patch.object(saved_states, "capture", side_effect=lambda *args: calls.append("save")):
            wrapped(self.job)
        self.assertEqual(calls, ["save", "delete"])

    def test_save_does_not_stop_containers_archive_shared_data_or_change_root_configuration(self):
        with mock.patch.object(saved_states.compose, "run") as run:
            sid = self.save()
        run.assert_not_called()
        folder = saved_states._location("local/demo", sid)
        self.assertEqual({path.name for path in folder.iterdir()}, {"manifest.json", "resources.json"})
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((folder / "manifest.json").stat().st_mode), 0o600)
        self.assertEqual((self.root / ".env").read_text(), "GLOBAL=unchanged\n")

    def test_restore_validates_before_activation_and_retains_only_own_rollback_on_failure(self):
        sid = self.save()
        events = []

        def execute(job, project, pid, operation, folder):
            events.append(operation)
            if operation == "restore":
                (folder / "previous.json").write_text("previous resources")
                raise JobFailed("Recovery incomplete")

        with mock.patch.object(saved_states, "_execute", side_effect=execute), \
             mock.patch.object(saved_states.JOBS, "start") as start:
            saved_states.restore(self.model, "local/demo", sid, confirmed=True,
                                 before=[lambda job: events.append("activate")])
            steps = start.call_args.args[1]
            with self.assertRaisesRegex(JobFailed, "Recovery incomplete"):
                for step in steps:
                    step(self.job)
        self.assertEqual(events, ["check", "activate", "restore"])
        self.assertEqual(len(list(saved_states.STATES.glob(".restore-*/previous.json"))), 1)
        self.assertEqual((self.root / ".env").read_text(), "GLOBAL=unchanged\n")
        self.assertEqual(len(saved_states.list_states("local/demo")), 1)

    def test_unconfirmed_restore_never_runs_adapter_or_starts_apps(self):
        with mock.patch.object(saved_states, "_verified") as verify:
            with self.assertRaisesRegex(ValueError, "Confirm replacing this playground"):
                saved_states.restore(self.model, "local/demo", self.sid)
        verify.assert_not_called()

    def test_switch_to_previously_seeded_playground_still_runs_save_and_delete(self):
        before, delete, after = mock.Mock(), mock.Mock(), ["docker", "compose", "up"]
        plan = {"id": "welcome", "label": "Start", "fresh": False}
        project = {"playgrounds": {"welcome": {"name": "Welcome"}}}
        with mock.patch.object(layers, "steps", return_value=[before, after]), \
             mock.patch.object(layers, "waits", return_value=[{}]), \
             mock.patch.object(layers, "changes", return_value={}), \
             mock.patch.object(playgrounds, "_job", return_value="job") as job:
            self.assertEqual(playgrounds._started(project, plan, delete), "job")
        self.assertEqual(job.call_args.args[1], [before, delete, after])
