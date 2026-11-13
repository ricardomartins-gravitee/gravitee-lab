"""Cloning refuses ambiguous storage and always resumes its own paused writers.

The real Docker scenario covers resource transfer; these cases exercise failures
between pause, copy and resume without depending on a Docker daemon.
"""

import copy
import json
from unittest.mock import Mock, call

from lab_ui import paths
from lab_ui.catalogs import independent, runtime_clones, runtime_storage
from lab_ui.library import runtime_settings
from lab_ui.docker import compose, storage
from lab_ui.docker.jobs import JobFailed
from tests import Case


class Clones(Case):
    def setUp(self):
        self.folder(paths)

    def test_all_paused_writers_resume_after_a_failed_copy(self):
        execute = self.patch(runtime_clones, "_execute")
        run = self.patch(compose, "run", return_value=(0, "", ""))
        copy = Mock(side_effect=ValueError("Changed source"))
        with self.assertRaisesRegex(ValueError, "Changed source"):
            runtime_clones._paused(["own-a", "own-b"], copy)
        self.assertEqual(execute.call_args_list, [call(["docker", "pause", "own-a"], timeout=30),
                                                call(["docker", "pause", "own-b"], timeout=30)])
        self.assertEqual(run.call_args_list, [call(["docker", "unpause", "own-b"], timeout=30),
                                            call(["docker", "unpause", "own-a"], timeout=30)])

    def test_datastores_flush_after_their_writers_pause_and_resume_before_them(self):
        execute = self.patch(runtime_clones, "_execute")
        resume = self.patch(compose, "run", return_value=(0, "", ""))
        runtime_clones._paused(["store", "app"], lambda: None, {"store": ["store-cli", "flush"]})
        self.assertEqual(execute.call_args_list, [call(["docker", "pause", "app"], timeout=30),
            call(["docker", "exec", "store", "store-cli", "flush"], timeout=120), call(["docker", "pause", "store"], timeout=30)])
        self.assertEqual(resume.call_args_list, [call(["docker", "unpause", "store"], timeout=30),
                                              call(["docker", "unpause", "app"], timeout=30)])

    def test_a_failed_flush_resumes_the_writers_without_pausing_the_store(self):
        self.patch(runtime_clones, "_execute", side_effect=[None, JobFailed("Flush failed")])
        resume = self.patch(compose, "run", return_value=(0, "", ""))
        with self.assertRaisesRegex(JobFailed, "Flush failed"):
            runtime_clones._paused(["app", "store"], Mock(), {"store": ["store-cli", "flush"]})
        resume.assert_called_once_with(["docker", "unpause", "app"], timeout=30)

    def test_legacy_flush_metadata_is_inherited_only_for_the_exact_running_image(self):
        label = "io.gravitee.snapshot.flush"
        self.patch(compose, "config_json", return_value={"services": {"db": {"image": "store:1", "labels": {label: '["flush"]'}}}})
        run = self.patch(compose, "run", return_value=(0, "sha256:running\n", ""))
        inspected = [{"Config": {"Labels": {"com.docker.compose.service": "db"}}, "Image": "sha256:running"}]
        config = {"services": {"db": {}}}
        runtime_clones._inherit_flushes(config, inspected)
        self.assertEqual(runtime_clones._flushes(config, [{"id": "ours", "service": "db", "state": "running"}]), {"ours": ["flush"]})
        run.return_value = (0, "sha256:changed\n", "")
        with self.assertRaisesRegex(ValueError, "different images"):
            runtime_clones._inherit_flushes({"services": {"db": {}}}, inspected)

    def test_pause_can_hide_ports_but_cannot_change_container_or_storage_identity(self):
        before = {"containers": [{"id": "ours", "state": "running", "ports": ["18000"], "project": "private"}],
                  "networks": [{"id": "own-network"}], "volumes": [{"name": "data", "created": "original"}]}
        after = copy.deepcopy(before)
        after["containers"][0].update(state="paused", ports=[])
        self.assertTrue(runtime_clones._same_paused_resources(before, after))
        for kind, key, value in (("containers", "id", "replacement"), ("containers", "project", "foreign"),
                                 ("networks", "id", "replacement"), ("volumes", "created", "new")):
            changed = copy.deepcopy(after)
            changed[kind][0][key] = value
            self.assertFalse(runtime_clones._same_paused_resources(before, changed))

    def test_partial_pause_failure_resumes_only_the_containers_paused_by_this_job(self):
        self.patch(runtime_clones, "_execute", side_effect=[None, JobFailed("pause failed")])
        resume = self.patch(compose, "run", return_value=(0, "", ""))
        copy = Mock()
        with self.assertRaisesRegex(JobFailed, "pause failed"):
            runtime_clones._paused(["ours", "replaced"], copy)
        copy.assert_not_called()
        resume.assert_called_once_with(["docker", "unpause", "ours"], timeout=30)

    def test_every_resume_is_attempted_and_a_failure_is_reported(self):
        self.patch(runtime_clones, "_execute")
        resume = self.patch(compose, "run", side_effect=[(1, "", "lost"), (0, "", "")])
        with self.assertRaisesRegex(JobFailed, "unpause own-b"):
            runtime_clones._paused(["own-a", "own-b"], lambda: None)
        self.assertEqual(resume.call_count, 2)

    def test_a_destination_volume_is_never_overwritten(self):
        self.patch(compose, "run", return_value=(0, "exists", ""))
        copy = self.patch(runtime_clones, "_execute")
        with self.assertRaisesRegex(ValueError, "already exists"):
            runtime_clones._volume("ours", "runtime-new", "data")
        copy.assert_not_called()

    def test_file_links_are_refused_before_copying_with_the_helper(self):
        source = self.root / "source"
        source.mkdir()
        (source / "outside").symlink_to(self.root)
        copy = self.patch(runtime_clones, "_execute")
        with self.assertRaisesRegex(ValueError, "link"):
            runtime_clones._files(source, self.root / "target")
        copy.assert_not_called()

    def test_the_copy_helper_has_a_read_only_source_and_an_empty_destination_guard(self):
        command = storage.copy_command("source-volume", "new-volume")
        self.assertIn("type=volume,source=source-volume,target=/from,readonly", command)
        self.assertIn("type=volume,source=new-volume,target=/to", command)
        self.assertIn("test -z", command[-1])
        self.assertIn("--network", command)
        self.assertIn("none", command)

    def test_shared_resources_without_adapters_are_refused_before_any_copy(self):
        self.patch(runtime_clones, "_configuration", return_value={"name": "shared"})
        self.patch(runtime_clones, "_running")
        file = self.root / paths.unit_folder("playground", "local/source") / ".state/created.json"
        file.parent.mkdir(parents=True)
        file.write_text(json.dumps([{"product": "aim-proxies", "kind": "studio"}]))
        with self.assertRaisesRegex(ValueError, "Shared cloning.*Gamma"):
            runtime_clones.check("local/source")
        self.assertFalse((self.root / ".lab/runtimes").exists())

    def test_a_failed_compiler_leaves_an_unusable_clone_that_can_be_deleted(self):
        rid = runtime_settings.create("local/source", "Copy")["id"]
        self.patch(runtime_clones, "check", return_value={})
        start = self.patch(runtime_clones.JOBS, "start")
        execute = self.patch(runtime_clones, "_execute")
        self.patch(independent, "prepare", side_effect=ValueError("Input changed"))
        runtime_clones.create({}, "local/source", rid)
        with self.assertRaisesRegex(ValueError, "Input changed"):
            start.call_args.args[1][0](Mock())
        self.assertTrue(independent.read(rid)["clone_pending"])
        execute.assert_not_called()
        self.patch(storage, "resources", return_value={"containers": [], "networks": [], "volumes": []})
        self.patch(storage, "network_users", return_value={})
        plan = runtime_storage.preview(rid, "delete")
        self.assertEqual(plan["removes"]["folders"], [independent.folder(rid).relative_to(paths.ROOT).as_posix()])

    def test_success_discards_export_credentials_and_foreign_ownership_metadata(self):
        rid = runtime_settings.create("local/source", "Copy")["id"]
        self.patch(runtime_clones, "check", return_value={})
        start = self.patch(runtime_clones.JOBS, "start")
        self.patch(runtime_clones, "_helper")
        record = {"schema": 1, "id": rid, "project": independent.name(rid), "services": {}}
        self.patch(independent, "prepare", return_value=record)
        def copy_resources(*args):
            folder = independent.folder(rid) / "clone"
            folder.mkdir()
            (folder / "foreign.json").write_text("other ownership metadata")
            return record
        self.patch(runtime_clones, "_shared", side_effect=copy_resources)
        runtime_clones.create({}, "local/source", rid)
        job = Mock()
        job._canceled = False
        start.call_args.args[1][0](job)
        self.assertFalse((independent.folder(rid) / "clone").exists())
        self.assertFalse(independent.read(rid)["clone_pending"])

    def test_anonymous_image_volumes_are_explicit_and_private_in_the_copy(self):
        config = {"services": {"db": {}}, "volumes": {}}
        inspected = [{"Config": {"Labels": {"com.docker.compose.service": "db"}},
                      "Mounts": [{"Type": "volume", "Name": "anonymous", "Destination": "/data/config", "RW": True}]}]
        self.patch(storage, "inspect", return_value=[{"Driver": "local", "Options": {}}])
        self.patch(storage, "ask", return_value="own-db")
        rows = {"containers": [{"id": "own-db"}], "volumes": []}
        result = runtime_clones._volumes(config, inspected, rows)
        key = result["anonymous"]
        self.assertEqual(config["services"]["db"]["volumes"], [{"type": "volume", "source": key, "target": "/data/config", "read_only": False}])
        self.assertEqual(config["volumes"], {key: {}})
        self.patch(storage, "ask", return_value="own-db\nforeign")
        with self.assertRaisesRegex(ValueError, "exclusive local storage"):
            runtime_clones._volumes({"services": {"db": {}}, "volumes": {}}, inspected, rows)
