"""Cleanup previews bind explicit Docker identities to one private filesystem tree.

These refusal and race cases complement the real HTTP/Docker cleanup workflow in independent_docker.
"""

import copy
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock

from lab_ui import paths
from lab_ui.catalogs import independent, runtime_storage
from lab_ui.docker import compose, storage
from lab_ui.library import runtime_settings
from tests import Case


class RuntimeStorage(Case):
    def setUp(self):
        self.folder(paths)
        self.pid = "local/a"
        self.root_path = independent.folder(self.pid)
        self.root_path.mkdir(parents=True)
        self.record = {"schema": 1, "id": self.pid, "project": independent.name(self.pid), "active": False,
                       "catalogs": ["local"], "services": {}, "initialized": True}
        (self.root_path / "runtime.json").write_text(json.dumps(self.record))
        (self.root_path / "compose.json").write_text('{}')
        (self.root_path / "mounts").mkdir()
        (self.root_path / "mounts/data").write_text("private data")
        name = self.record["project"]
        self.resources = {"containers": [{"id": "container-a", "name": name + "-db-1", "state": "exited",
                         "project": name, "directory": str(self.root_path), "service": "db", "ports": []}],
            "networks": [{"id": "network-a", "name": name + "_internal", "project": name, "key": "internal", "containers": []}],
            "volumes": [{"name": name + "_data", "project": name, "key": "data", "created": "2026-11-12T00:00:00Z",
                         "driver": "local", "options": {}}]}
        self.patch(storage, "resources", side_effect=lambda project: copy.deepcopy(self.resources))
        self.network_users = self.patch(storage, "network_users", return_value={"network-a": set()})
        self.users = self.patch(storage, "ask", return_value="container-a")

    def plan(self, operation="containers"):
        return runtime_storage.preview(self.pid, operation)

    def test_preview_names_only_the_selected_runtime_and_preserves_all_sources_images_and_other_data(self):
        plan = self.plan()
        self.assertEqual(plan["removes"]["containers"], [self.resources["containers"][0]["name"]])
        self.assertEqual(plan["removes"]["volumes"], [])
        self.assertIn("Volumes and runtime files", plan["keeps"])
        plan = self.plan("data")
        self.assertEqual(plan["removes"]["volumes"], [self.resources["volumes"][0]["name"]])
        self.assertEqual(plan["removes"]["folders"], [self.root_path.relative_to(self.root).as_posix()])
        self.assertGreater(plan["local_bytes"], 0)
        self.assertTrue((self.root_path / "mounts/data").exists())

    def test_running_shared_or_ambiguous_resources_and_external_users_are_refused(self):
        cases = [("containers", "state", "running", "Stop"), ("containers", "project", "shared", "belong"),
                 ("containers", "directory", "/another-lab", "belong"), ("volumes", "name", "shared-data", "ownership"),
                 ("volumes", "options", {"device": "/outside"}, "private local"),
                 ("networks", "containers", ["somebody-else"], "outside")]
        for kind, key, value, reason in cases:
            with self.subTest(kind=kind, key=key):
                original = self.resources[kind][0][key]
                self.resources[kind][0][key] = value
                with self.assertRaisesRegex(ValueError, reason):
                    self.plan("data")
                self.resources[kind][0][key] = original
        self.network_users.return_value = {"network-a": {"outside-created-container"}}
        with self.assertRaisesRegex(ValueError, "outside"):
            self.plan("data")
        self.network_users.return_value = {"network-a": set()}
        self.users.return_value = "outside-container"
        with self.assertRaisesRegex(ValueError, "outside"):
            self.plan("data")

    def test_links_are_never_followed_and_a_linked_runtime_root_cannot_be_deleted(self):
        with tempfile.TemporaryDirectory() as temporary:
            outside = Path(temporary)
            (outside / "keep").write_text("outside data")
            (self.root_path / "mounts/link").symlink_to(outside, target_is_directory=True)
            self.plan("data")
            self.assertEqual((outside / "keep").read_text(), "outside data")
            linked = independent.folder("local/linked")
            linked.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "contains a link"):
                runtime_storage.directory("local/linked")

    def test_stale_preview_or_changes_while_a_job_waits_remove_nothing(self):
        jobs = self.patch(runtime_storage, "JOBS")
        plan = self.plan()
        (self.root_path / "mounts/data").write_text("changed")
        with self.assertRaisesRegex(ValueError, "changed after the preview"):
            runtime_storage.cleanup(self.pid, "containers", plan["fingerprint"])
        jobs.start.assert_not_called()
        plan = self.plan()
        runtime_storage.cleanup(self.pid, "containers", plan["fingerprint"])
        _, steps, _ = self.started(jobs)
        self.resources["containers"][0]["id"] = "new-container"
        self.users.return_value = "new-container"
        worker = Mock()
        with self.assertRaisesRegex(ValueError, "changed after confirmation"):
            steps[0](worker)
        worker.exec.assert_not_called()

    def test_remove_containers_keeps_volumes_files_and_initialization_and_uses_only_explicit_ids(self):
        jobs = self.patch(runtime_storage, "JOBS")
        plan = self.plan()
        runtime_storage.cleanup(self.pid, "containers", plan["fingerprint"])
        _, steps, _ = self.started(jobs)
        worker = Mock()
        steps[0](worker)
        self.assertEqual([c.args[0] for c in worker.exec.call_args_list],
                         [["docker", "container", "rm", "container-a"], ["docker", "network", "rm", "network-a"]])
        self.assertEqual((self.root_path / "mounts/data").read_text(), "private data")
        self.assertTrue(independent.read(self.pid)["initialized"])
        self.assertFalse(independent.read(self.pid)["active"])

    def test_data_job_checks_volume_identity_again_before_deleting_it(self):
        self.patch(runtime_storage, "JOBS")
        plan = self.plan("data")
        runtime_storage.cleanup(self.pid, "data", plan["fingerprint"])
        _, steps, _ = self.started(runtime_storage.JOBS)
        self.patch(compose, "run", return_value=(0, "", ""))
        self.patch(storage, "inspect", return_value=[{"Name": self.resources["volumes"][0]["name"], "CreatedAt": "new-volume"}])
        worker = Mock()
        with self.assertRaisesRegex(ValueError, "volume changed"):
            steps[0](worker)
        self.assertFalse(any(c.args[0][1:3] == ["volume", "rm"] for c in worker.exec.call_args_list))
        self.assertTrue((self.root_path / "mounts/data").exists())

    def test_unused_runtime_deletion_rechecks_its_definition_without_touching_docker_or_a_sibling(self):
        unused = runtime_settings.create("local/a", "Unused")["id"]
        sibling = runtime_settings.create("local/a", "Keep")["id"]
        self.resources = {"containers": [], "volumes": [], "networks": []}
        plan = runtime_storage.preview(unused, "delete")
        self.assertEqual(plan["removes"]["runtime"], ["Unused"])
        jobs = self.patch(runtime_storage, "JOBS")
        runtime_storage.cleanup(unused, "delete", plan["fingerprint"])
        _, commands, _ = self.started(jobs)
        job = Mock()
        commands[0](job)
        self.assertNotIn(unused, runtime_settings.instances())
        self.assertIn(sibling, runtime_settings.instances())
        job.exec.assert_not_called()
        with self.assertRaises(paths.NotFound):
            runtime_storage.preview("local/a", "delete")
