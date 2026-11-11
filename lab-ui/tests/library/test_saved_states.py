"""Saved playground states: metadata, safety checks, and save-before-delete ordering.

No Docker daemon is needed for these tests. The engine's archive and Docker commands
are separately tested with mocks so failure cannot silently run seed deletion.
"""

import json
import os
import stat
import tarfile
import unittest
from pathlib import Path
from unittest import mock

from lab_ui import paths
from lab_ui.docker import compose
from lab_ui.docker.jobs import JobFailed
from lab_ui.gravitee import places
from lab_ui.library import layers, playgrounds, saved_states

from .. import Case


class SavedStates(Case):
    def setUp(self):
        self.folder(saved_states, files={"docker-compose.yml": "name: gravitee\n"})
        self.patch(saved_states, "STATES", self.root / ".lab/saved-states")
        saved_states.STATES.mkdir(parents=True)
        self.sid1 = "1" * 32
        self.sid2 = "2" * 32

    def record(self, pid, sid, name):
        folder = saved_states._location(pid, sid)
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text(json.dumps({
            "schema": 1, "complete": True, "playground": pid, "id": sid,
            "name": name, "created": f"2026-11-{9 if sid == self.sid1 else 10:02d}T00:00:00+00:00",
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
        # Broken metadata must not turn the whole list into a server error.
        valid = json.loads((folder / "manifest.json").read_text())
        for key, value in (("created", None), ("created", "not-a-date"), ("name", None), ("size", -1),
                           ("images", [None]), ("schema", True)):
            with self.subTest(key=key, value=value):
                candidate = {**valid, "id": self.sid2, key: value}
                (bad / "manifest.json").write_text(json.dumps(candidate), encoding="utf-8")
                self.assertEqual([row["name"] for row in saved_states.list_states("welcome")], ["Good"])

    def test_delete_one_state_preserves_the_other_and_live_configuration(self):
        first = self.record("welcome", self.sid1, "Before edits")
        second = self.record("welcome", self.sid2, "After edits")
        (self.root / ".env").write_text("KEY=unchanged\n", encoding="utf-8")
        with mock.patch.object(saved_states.JOBS, "current", None):
            result = saved_states.delete_state({"playgrounds": {"welcome": {}}}, "welcome", self.sid1)
        self.assertEqual(result, {"deleted": self.sid1, "image_cleanup_pending": False})
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

    def test_external_or_unknown_bind_mount_refuses_an_exact_save(self):
        outside = {"Type": "bind", "RW": True, "Source": "/var/lib/foreign-data",
                   "Destination": "/data"}
        with self.assertRaisesRegex(JobFailed, "external bind mount"):
            saved_states._paths_to_save([outside])
        untracked = {"Type": "bind", "RW": True, "Source": str(self.root / "app-data"),
                     "Destination": "/data"}
        with self.assertRaisesRegex(JobFailed, "needs a storage adapter"):
            saved_states._paths_to_save([untracked])
        tracked = {"Type": "bind", "RW": True, "Source": str(self.root / ".lab/catalogs/sample/.state"),
                   "Destination": "/state"}
        self.assertIn(".lab/catalogs", saved_states._paths_to_save([tracked]))

    def test_manifest_integrity_is_checked_before_restore(self):
        folder = self.record("welcome", self.sid1, "Good")
        archive = folder / "config.tar.gz"
        with tarfile.open(archive, "w:gz"):
            pass
        data = json.loads((folder / "manifest.json").read_text())
        data.update(core_sha256=saved_states._digest(self.root / "docker-compose.yml"),
                    config={"file": "config.tar.gz", "sha256": saved_states._digest(archive)},
                    volumes=[], images=[{"id": "sha256:removed", "reference": "seed:obsolete", "running": False}])
        (folder / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
        def without_seed(args):
            self.assertNotIn("sha256:removed", args)
            return ""
        with mock.patch.object(saved_states, "_docker", side_effect=without_seed), \
             mock.patch.object(compose, "project_name", return_value="gravitee"):
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
        job = mock.Mock(lines=[])
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
        # A missing running image fails before services are stopped, rather than after archiving their data.
        with mock.patch.object(places, "recorded", return_value=[]), \
             mock.patch.object(saved_states, "_runtime", return_value=(["gravitee_data"], [], ["one"], [])), \
             mock.patch.object(saved_states, "_pin_images", side_effect=JobFailed("Required image missing")), \
             mock.patch.object(saved_states, "_stop") as stopped, \
             mock.patch.object(saved_states, "_restart") as restarted:
            with self.assertRaisesRegex(JobFailed, "Required image missing"):
                saved_states.capture(job, {"playgrounds": {pid: {}}}, pid)
        stopped.assert_not_called()
        restarted.assert_not_called()

    def test_two_successful_checkpoints_are_published_only_after_restart(self):
        events = []

        def write_config(dest, mounts):
            events.append("config")
            dest.write_bytes(b"safe config")

        def write_volume(job, name, dest, restore=False):
            events.append("volume")
            dest.write_bytes(b"safe data")

        with mock.patch.object(places, "recorded", return_value=[]), \
             mock.patch.object(saved_states, "_runtime",
                               return_value=(["gravitee_data-mongo"], [], ["container1"], [])), \
             mock.patch.object(saved_states, "_stop", side_effect=lambda *a: events.append("stop")), \
             mock.patch.object(saved_states, "_restart", side_effect=lambda *a: events.append("restart")), \
             mock.patch.object(saved_states, "_archive_config", side_effect=write_config), \
             mock.patch.object(saved_states, "_volume", side_effect=write_volume):
            first = saved_states.capture(mock.Mock(lines=[]), {"playgrounds": {"welcome": {}}}, "welcome", "Before")
            second = saved_states.capture(mock.Mock(lines=[]), {"playgrounds": {"welcome": {}}}, "welcome", "After")
        self.assertNotEqual(first, second)
        self.assertEqual([s["name"] for s in saved_states.list_states("welcome")], ["After", "Before"])
        self.assertEqual(events, ["stop", "config", "volume", "restart"] * 2)

    def test_restore_volume_uses_archive_basename_and_not_an_absolute_container_path(self):
        commands = []
        job = mock.Mock()
        job.exec.side_effect = commands.append
        saved_states._volume(job, "gravitee_data-mongo",
                             Path("/some/private/path/volume-0.tar.gz"), restore=True)
        self.assertIn("/backup/volume-0.tar.gz", commands[0][-1])
        self.assertNotIn("/some/private/path/volume-0.tar.gz -C", commands[0][-1])

    def test_restoring_configuration_removes_stale_files_and_preserves_access_modes(self):
        folder = self.root / "snapshot"
        folder.mkdir()
        original = self.root / ".lab/catalogs/local/playgrounds/test"
        original.mkdir(parents=True)
        (original / "kept.txt").write_text("saved", encoding="utf-8")
        plugins = self.root / "plugins/apim"
        plugins.mkdir(parents=True)
        secrets = original / "secrets"
        secrets.mkdir()
        os.chmod(plugins, 0o777)   # containers unpack plugins as another user
        os.chmod(secrets, 0o700)
        os.chmod(original / "kept.txt", 0o600)
        with tarfile.open(folder / "config.tar.gz", "w:gz") as archive:
            archive.add(self.root / ".lab/catalogs", arcname=".lab/catalogs")
            archive.add(self.root / "plugins", arcname="plugins")
        (original / "stale.txt").write_text("added later", encoding="utf-8")
        saved_states._restore_config(folder)
        self.assertTrue((original / "kept.txt").is_file())
        self.assertFalse((original / "stale.txt").exists())
        for path, expected in ((plugins, 0o777), (secrets, 0o700), (original / "kept.txt", 0o600)):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected, str(path))

    def test_unsafe_configuration_is_refused_before_any_file_is_removed(self):
        folder = self.root / "snapshot"
        folder.mkdir()
        live = self.root / ".env"
        live.write_text("KEEP=live\n")
        for name in ("../escape", "/absolute", ".git/config", ".lab/saved-states/overwrite"):
            with self.subTest(name=name):
                with tarfile.open(folder / "config.tar.gz", "w:gz") as archive:
                    archive.addfile(tarfile.TarInfo(name))
                with self.assertRaises((ValueError, JobFailed)):
                    saved_states._restore_config(folder)
                self.assertEqual(live.read_text(), "KEEP=live\n")

    def test_partial_stop_failure_still_resumes_containers_and_keeps_no_checkpoint(self):
        job = mock.Mock(lines=[])
        with mock.patch.object(places, "recorded", return_value=[]), \
             mock.patch.object(saved_states, "_runtime", return_value=(["gravitee_data"], [], ["one", "two"], [])), \
             mock.patch.object(saved_states, "_stop", side_effect=JobFailed("Canceled during stop")), \
             mock.patch.object(saved_states, "_restart") as restart:
            with self.assertRaisesRegex(JobFailed, "Canceled during stop"):
                saved_states.capture(job, {"playgrounds": {"welcome": {}}}, "welcome")
        self.assertEqual(restart.call_args.args[1], ["one", "two"])
        self.assertIsInstance(restart.call_args.args[0], saved_states._Recovery)
        self.assertEqual(saved_states.list_states("welcome"), [])

    def test_failed_rollback_keeps_the_previous_runtime_archive(self):
        folder = self.root / "state"
        folder.mkdir()
        data = {"name": "Saved", "volumes": [], "images": []}
        job = mock.Mock(lines=[])

        def capture(job, destination, *args):
            destination.mkdir(parents=True)
            (destination / "config.tar.gz").write_bytes(b"previous config")
            return data

        with mock.patch.object(places, "recorded", return_value=[]), \
             mock.patch.object(saved_states, "_verified", return_value=(folder, data)), \
             mock.patch.object(saved_states, "_runtime", return_value=([], [], [], [])), \
             mock.patch.object(saved_states, "_capture_stopped", side_effect=capture), \
             mock.patch.object(saved_states, "_apply", side_effect=JobFailed("Disk unavailable")), \
             mock.patch.object(saved_states.JOBS, "start") as start:
            saved_states.restore({"playgrounds": {"welcome": {}}}, "welcome", self.sid1, confirmed=True)
            with self.assertRaisesRegex(JobFailed, "Disk unavailable"):
                start.call_args.args[1][0](job)
        backups = list(saved_states.STATES.glob(".rollback-*/previous/config.tar.gz"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"previous config")
        self.assertEqual(json.loads((backups[0].parent / "manifest.json").read_text()), data)

    def test_unconfirmed_restore_never_reads_or_replaces_volumes(self):
        with self.assertRaisesRegex(ValueError, "Confirm replacing"):
            saved_states.restore({"playgrounds": {"welcome": {}}}, "welcome", self.sid1)

    def test_switch_to_previously_seeded_playground_still_runs_save_and_delete(self):
        def before(job):
            pass

        after = ["docker", "compose", "up"]

        def delete(job):
            pass

        plan = {"id": "welcome", "label": "Start", "fresh": False}
        project = {"playgrounds": {"welcome": {"name": "Welcome"}}}
        with mock.patch.object(layers, "steps", return_value=[before, after]), \
             mock.patch.object(layers, "waits", return_value=[{}]), \
             mock.patch.object(layers, "changes", return_value={}), \
             mock.patch.object(playgrounds, "_job", return_value="job") as job:
            self.assertEqual(playgrounds._started(project, plan, delete), "job")
        self.assertEqual(job.call_args.args[1], [before, delete, after])

    def test_checkpoint_image_tags_pin_exact_images_without_copying_layers(self):
        # The image of an exited seed may have been removed; restore does not recreate that container.
        containers = [
            {"Id": "app", "Image": "sha256:live", "State": {"Running": True},
             "Config": {"Image": "app:local", "Labels": {"com.docker.compose.service": "app"}}},
            {"Id": "seed", "Image": "sha256:removed", "State": {"Running": False},
             "Config": {"Image": "seed:obsolete", "Labels": {"com.docker.compose.service": "seed", "io.gravitee.role": "seed"}}},
        ]
        with mock.patch.object(compose, "project_name", return_value="gravitee"), \
             mock.patch.object(saved_states, "_check_applied"), \
             mock.patch.object(saved_states, "_docker", side_effect=["app seed", json.dumps(containers), ""]):
            _, _, running, images = saved_states._runtime()
        self.assertEqual(running, ["app"])
        self.assertEqual([image["id"] for image in images], ["sha256:live"])
        commands = []
        job = mock.Mock()
        job.exec.side_effect = commands.append
        items = [{"id": "sha256:a"}, {"id": "sha256:a"}, {"id": "sha256:b"},
                 {"id": "sha256:removed", "reference": "seed:obsolete", "running": False}]
        tags = saved_states._pin_images(job, items, self.sid1)
        self.assertEqual(len(tags), 2)
        self.assertEqual(items[0]["locked_tag"], items[1]["locked_tag"])
        self.assertNotEqual(items[1]["locked_tag"], items[2]["locked_tag"])
        self.assertEqual(commands[0][:3], ["docker", "tag", "sha256:a"])
        self.assertNotIn("locked_tag", items[-1])
        commands.clear()
        saved_states._retag_images(job, [items[-1]])
        self.assertEqual(commands, [])
        job = mock.Mock(lines=[])
        job.exec.side_effect = [None, JobFailed("Pin failed")]
        with mock.patch.object(saved_states, "_docker", side_effect=JobFailed("Image in use")):
            with self.assertRaisesRegex(JobFailed, "Pin failed"):
                saved_states._pin_images(job, [{"id": "sha256:a"}, {"id": "sha256:b"}], self.sid1)
        self.assertTrue(any("Image in use" in line for line in job.lines))

    def test_refuse_state_save_when_running_configuration_has_not_been_applied(self):
        container = {"State": {"Running": True}, "Config": {"Labels": {
            "com.docker.compose.service": "apim-management-api",
            "com.docker.compose.config-hash": "saved-hash",
        }}}
        with mock.patch.object(compose, "run", return_value=(0, "apim-management-api saved-hash\n", "")):
            saved_states._check_applied([container])
        with mock.patch.object(compose, "run", return_value=(0, "apim-management-api newer-hash\n", "")):
            with self.assertRaisesRegex(JobFailed, "Apply the pending settings"):
                saved_states._check_applied([container])


if __name__ == "__main__":
    unittest.main()
