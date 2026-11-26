"""Sandbox generation owns mutable storage, ports and image names without changing linked sources.

The deterministic compiler and lifecycle cases here complement the real multi-project
Docker test, which proves data and seed identity survive stop/resume alongside a shared Lab.
"""

import copy
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock

from lab_ui import paths
from lab_ui.library import runtime_settings
from lab_ui.catalogs import independent, runtime_images, standalone
from lab_ui.docker import compose, images, seed_image
from tests import Case


class Independent(Case):
    def setUp(self):
        self.folder(paths)
        self.patch(independent.catalog_service, "preflight")
        self.inputs = {}
        for letter in ("a", "b"):
            pid = "local/" + letter
            folder = self.root / paths.unit_folder("playground", pid)
            (folder / "data").mkdir(parents=True)
            (folder / ".state").mkdir()
            (folder / "seed").mkdir()
            (folder / "seed/hosts.js").write_text('const api = "http://localhost:18000";')
            (folder / "data/value.txt").write_text("initial " + letter)
            (folder / ".state/created.json").write_text('[{"id":"shared-original"}]')
            services = {
                "db": {"image": "redis:8", "container_name": "gio-db", "restart": "unless-stopped",
                       "ports": [{"target": 6379, "published": "6379"}],
                       "volumes": [{"type": "volume", "source": "db-data", "target": "/data"}],
                       "networks": {"internal": None}},
                letter + "-app": {"image": "fixture/app:local", "build": {"context": str(folder)}, "restart": "unless-stopped",
                       "ports": [{"target": 8000, "published": "18000"}], "networks": {"internal": None},
                       "labels": {"io.gravitee.console.app.url": "http://localhost:18000"},
                       "environment": {"PUBLIC_URL": "http://localhost:18000/x", "PASSWORD": "invented$$literal"},
                       "volumes": [{"type": "bind", "source": str(folder / "data"), "target": "/data"}]},
                letter + "-seed": {"image": "fixture/app:local", "restart": "no", "networks": {"internal": None},
                       "volumes": [{"type": "bind", "source": str(folder / ".state"), "target": "/state"},
                                   {"type": "bind", "source": str(folder / "seed"), "target": "/seed", "read_only": True}]},
            }
            config = {"name": "gravitee", "services": services, "volumes": {"db-data": {"name": "gravitee_db", "external": True}},
                      "networks": {"internal": {"name": "gravitee_internal", "external": True}}}
            unit = {"id": pid, "name": letter, "file": paths.unit_file("playground", pid), "seed": letter + "-seed", "requires": {}}
            found = {"services": {key: {"oneshot": key.endswith("-seed")} for key in services}}
            gathered = {"unit": unit, "folder": folder, "nodes": [("playground", pid)], "found": found}
            self.inputs[pid] = (config, gathered)
        self.patch(standalone, "_gather", side_effect=lambda pid: copy.deepcopy(self.inputs[pid][1]))
        self.patch(standalone, "_products", return_value=set())
        self.patch(standalone, "_keep", side_effect=lambda config, *args: set(config["services"]))
        self.next_config = self.inputs["local/a"][0]
        self.patch(compose, "config_json", side_effect=lambda *args, **kwargs: copy.deepcopy(self.next_config))
        self.patch(independent, "_port", side_effect=range(31000, 31100))

    def prepare(self, pid="local/a", **kwargs):
        self.next_config = self.inputs[pid][0]
        return independent.prepare(pid, **kwargs)

    def config(self, pid="local/a"):
        return json.loads((independent.folder(pid) / "compose.json").read_text())

    def test_overlapping_writable_and_read_only_mounts_share_the_same_private_files(self):
        config = self.inputs["local/a"][0]
        state = self.inputs["local/a"][1]["folder"] / ".state"
        (state / "app").mkdir()
        config["services"]["a-app"]["volumes"] = [
            {"type": "bind", "source": str(state / "app"), "target": "/data"},
            {"type": "bind", "source": str(state / "app"), "target": "/view", "read_only": True}]
        value = self.prepare()
        app = self.config()["services"]["a-app"]["volumes"]
        self.assertEqual(app[0]["source"], app[1]["source"])
        self.assertEqual(Path(app[0]["source"]), Path(value["binds"][str(state)]) / "app")
        self.assertTrue(app[1]["read_only"])

    def test_apply_keeps_legacy_overlapping_mount_locations_and_their_existing_data(self):
        config, gathered = self.inputs["local/a"]
        state = gathered["folder"] / ".state"
        (state / "app").mkdir()
        config["services"]["a-app"]["volumes"] = [{"type": "bind", "source": str(state / "app"), "target": "/data"}]
        legacy = independent.folder("local/a") / "mounts/legacy-app"
        legacy.mkdir(parents=True)
        (legacy / "value").write_text("existing private data")
        previous = {"binds": {str(state / "app"): str(legacy)}}
        result, _, _ = independent._config("local/a", gathered, previous)
        self.assertEqual(result["services"]["a-app"]["volumes"][0]["source"], str(legacy))
        self.assertEqual((legacy / "value").read_text(), "existing private data")

    def test_container_commands_keep_internal_ports_while_browser_urls_change(self):
        config = {"healthcheck": {"test": ["CMD", "curl", "http://localhost:6379/health"]},
                  "labels": {"io.gravitee.snapshot.flush": '["curl", "http://localhost:6379/flush"]',
                             "io.gravitee.console.test.url": "http://localhost:6379"}}
        changed = independent.rewrite(config, {"6379": 31000})
        self.assertEqual(changed["healthcheck"], config["healthcheck"])
        self.assertEqual(changed["labels"]["io.gravitee.snapshot.flush"], config["labels"]["io.gravitee.snapshot.flush"])
        self.assertEqual(changed["labels"]["io.gravitee.console.test.url"], "http://localhost:31000")

    def test_the_portal_keeps_the_host_port_and_the_seed_reaches_the_gateway_inside_the_runtime(self):
        config = self.inputs["local/a"][0]
        config["services"]["apim-gateway"] = {"image": "gateway", "networks": {"internal": None},
                                              "ports": [{"target": 8082, "published": "8082", "protocol": "tcp"}]}
        config["services"]["apim-management-api"] = {
            "image": "management", "networks": {"internal": None},
            "environment": {"gravitee_portal_entrypoint": "http://localhost:8082"}}
        config["services"]["a-seed"]["environment"] = {
            "APIM_MANAGEMENT_URL": "http://apim-management-api:8083",
            "GATEWAY_INTERNAL_URL": "http://localhost:8082"}
        self.prepare()
        prepared = self.config()
        published = prepared["services"]["apim-gateway"]["ports"][0]["published"]
        self.assertEqual(prepared["services"]["apim-management-api"]["environment"]["gravitee_portal_entrypoint"],
                         "http://localhost:" + published)
        self.assertEqual(prepared["services"]["a-seed"]["environment"]["GATEWAY_INTERNAL_URL"], "http://apim-gateway:8082")
        self.assertNotEqual(published, "8082")

    def test_projects_ports_volumes_networks_built_images_and_mutable_binds_are_private(self):
        a, b = self.prepare("local/a"), self.prepare("local/b")
        self.assertNotEqual(a["project"], b["project"])
        self.assertFalse(set(a["ports"].values()) & set(b["ports"].values()))
        for pid, value in (("local/a", a), ("local/b", b)):
            config = self.config(pid)
            self.assertEqual(config["name"], value["project"])
            self.assertEqual(config["volumes"]["db-data"], {})
            self.assertEqual(config["networks"]["internal"], {})
            self.assertNotIn("container_name", config["services"]["db"])
            app = config["services"][pid[-1] + "-app"]
            self.assertEqual(app["environment"]["PASSWORD"], "invented$$literal")
            self.assertEqual(app["ports"][0]["host_ip"], "127.0.0.1")
            self.assertIn(str(value["ports"]["18000"]), app["environment"]["PUBLIC_URL"])
            self.assertEqual(config["services"][pid[-1] + "-seed"]["image"], app["image"])
            self.assertTrue(app["image"].startswith(value["project"] + "/"))
            source = self.inputs[pid][1]["folder"]
            state = Path(value["binds"][str(source / ".state")])
            data = Path(value["binds"][str(source / "data")])
            self.assertEqual(list(state.iterdir()), [])
            (data / "value.txt").write_text("private changes")
            self.assertEqual((source / "data/value.txt").read_text(), "initial " + pid[-1])
            self.assertEqual((source / ".state/created.json").read_text(), '[{"id":"shared-original"}]')
            self.assertEqual((independent.folder(pid) / "compose.json").stat().st_mode & 0o777, 0o600)

    def test_two_named_instances_of_the_same_source_keep_separate_mutable_data_and_ports(self):
        one = runtime_settings.create("local/a", "Demo")["id"]
        two = runtime_settings.create("local/a", "Sandbox")["id"]
        a, b = independent.prepare(one), independent.prepare(two)
        self.assertNotEqual(a["project"], b["project"])
        self.assertFalse(set(a["ports"].values()) & set(b["ports"].values()))
        source = self.inputs["local/a"][1]["folder"]
        left = Path(a["binds"][str(source / "data")])
        right = Path(b["binds"][str(source / "data")])
        (left / "value.txt").write_text("Only Demo")
        self.assertEqual((right / "value.txt").read_text(), "initial a")
        self.assertEqual((source / "data/value.txt").read_text(), "initial a")
        self.assertEqual(independent.prepare(one, refresh=True)["ports"], a["ports"])
        self.assertEqual((left / "value.txt").read_text(), "Only Demo")
        self.assertEqual(runtime_settings.mode("local/a"), "shared")

    def test_prepare_and_refresh_preserve_runtime_storage_and_port_assignments(self):
        value = self.prepare()
        source = self.inputs["local/a"][1]["folder"]
        target = Path(value["binds"][str(source / "data")])
        (target / "value.txt").write_text("keep private data")
        self.assertEqual(self.prepare(), value)
        config = self.config()
        private_seed = Path(config["services"]["a-seed"]["volumes"][1]["source"])
        self.assertIn(str(value["ports"]["18000"]), (private_seed / "hosts.js").read_text())
        self.assertIn("18000", (source / "seed/hosts.js").read_text())
        (source / "seed/hosts.js").write_text('const updated = "http://localhost:18000";')
        value.update(clone_pending=True, cloned_from="local/a")
        independent._write(independent.folder("local/a") / "runtime.json", value)
        self.assertEqual(self.prepare(refresh=True)["ports"], value["ports"])
        self.assertTrue(independent.read("local/a")["clone_pending"])
        self.assertIn("updated", (private_seed / "hosts.js").read_text())
        self.assertEqual((target / "value.txt").read_text(), "keep private data")
        self.assertEqual(independent.command("local/a", "stop")[-1], "stop")
        self.assertIn(value["project"], independent.command("local/a", "stop"))
        self.assertIn(str(independent.folder("local/a") / "compose.json"), independent.command("local/a", "stop"))

    def test_plan_is_readonly_and_unsafe_storage_and_host_access_are_refused(self):
        before = sorted(str(p) for p in self.root.rglob("*"))
        self.assertTrue(independent.plan("local/a")["independent"])
        self.assertEqual(sorted(str(p) for p in self.root.rglob("*")), before)
        for change in ({"network_mode": "host"}, {"privileged": True}, {"devices": ["/dev/sda"]}):
            with self.subTest(change=change):
                self.inputs["local/a"][0]["services"]["a-app"].update(change)
                with self.assertRaisesRegex(ValueError, "networks and mounts"):
                    self.prepare()
                for key in change:
                    self.inputs["local/a"][0]["services"]["a-app"].pop(key)
        self.inputs["local/a"][0]["volumes"]["db-data"]["driver_opts"] = {"device": "/outside"}
        with self.assertRaisesRegex(ValueError, "local Docker"):
            self.prepare()

    def test_writable_links_or_binds_outside_the_dependency_folders_are_refused(self):
        source = self.inputs["local/a"][1]["folder"]
        with tempfile.TemporaryDirectory() as outside:
            (source / "data/escape").symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "contains a link"):
                self.prepare()
            (source / "data/escape").unlink()
            self.inputs["local/a"][0]["services"]["a-app"]["volumes"][0]["source"] = outside
            with self.assertRaisesRegex(ValueError, "outside"):
                self.prepare()

    def test_resume_does_not_rerun_the_seed_and_stop_never_deletes_volumes(self):
        value = self.prepare()
        value["initialized"] = True
        independent._write(independent.folder("local/a") / "runtime.json", value)
        jobs = self.patch(independent, "JOBS")
        self.patch(independent, "_wait")
        independent.start("local/a")
        _, steps, _ = self.started(jobs)
        job = self.patch(independent, "_write")
        worker = Mock()
        steps[0](worker)
        sent = worker.exec.call_args.args[0]
        self.assertTrue(independent.tested.ran("local/a"), "A successful private start must enable the normal Share workflow")
        self.assertNotIn("--force-recreate", sent)
        self.assertIn("a-app", sent)
        self.assertNotIn("a-seed", sent)
        independent.stop("local/a")
        _, steps, _ = self.started(jobs)
        steps[0](worker)
        self.assertEqual(worker.exec.call_args.args[0][-1], "stop")
        self.assertNotIn("down", worker.exec.call_args.args[0])
        self.assertTrue(job.called)

    def test_seed_actions_apply_the_published_image_only_to_the_selected_runtime(self):
        pid = "local/a"
        repository = "ghcr.io/example/lab/seeder"
        value = self.prepare(pid)
        jobs = self.patch(independent, "JOBS")
        self.patch(independent, "_wait")
        self.patch(independent, "_tested")
        self.patch(independent, "describe", return_value={"running": True})
        base = independent.command(pid)
        override = ["-f", str(self.root / "published.yml")]
        choose = self.patch(seed_image, "files", return_value=[*base[len(compose.COMPOSE):], *override])
        self.assertEqual(value["seed"], "a-seed")
        for action, options, args, remove in (
                (independent.start, {}, ["up", "-d", "--build"], False),
                (independent.reseed, {}, ["run", "--rm", "--no-deps", "a-seed"], False),
                (independent.stop, {"remove": True}, ["run", "--rm", "--no-deps", "a-seed", "--remove"], True)):
            with self.subTest(action=action.__name__):
                worker = Mock()
                action(pid, published_seeder=repository, **options)
                _, steps, _ = self.started(jobs)
                steps[0](worker)
                choose.assert_called_with(worker, repository, "a-seed", args=base[len(compose.COMPOSE):], remove=remove)
                self.assertEqual(worker.exec.call_args_list[0].args[0], [*base, *override, *args])

    def test_seed_endpoints_cannot_target_a_shared_or_remote_management_api(self):
        seed = self.inputs["local/a"][0]["services"]["a-seed"]
        for endpoint in ("http://localhost:8083", "https://management.example.test"):
            seed["environment"] = {"APIM_MANAGEMENT_URL": endpoint}
            with self.subTest(endpoint=endpoint), self.assertRaisesRegex(ValueError, "must address a service"):
                independent.plan("local/a")
        seed["environment"] = {"APIM_MANAGEMENT_URL": "http://db:8083"}
        self.assertTrue(independent.plan("local/a")["independent"])

    def test_service_actions_resolve_the_selected_runtime_and_start_recreates_only_its_container(self):
        self.prepare("local/a")
        self.prepare("local/b")
        jobs = self.patch(independent, "JOBS")
        for pid in ("local/a", "local/b"):
            key = independent.service_key(pid, "db")
            self.assertEqual(independent.resolve_service(key), (pid, "db"))
            for operation in ("start", "stop", "restart"):
                with self.subTest(runtime=pid, operation=operation):
                    independent.service(*independent.resolve_service(key), operation)
                    _, steps, _ = self.started(jobs)
                    worker = Mock()
                    steps[0](worker)
                    args = ["up", "-d", "--no-deps", "db"] if operation == "start" else [operation, "db"]
                    worker.exec.assert_called_once_with(independent.command(pid, *args))
                    self.assertEqual(jobs.start.call_args.kwargs["key"], "service:" + key)
            for operation in ("start", "restart"):
                with self.assertRaisesRegex(ValueError, "Re-seed"):
                    independent.service(pid, pid[-1] + "-seed", operation)
        self.assertIsNone(independent.resolve_service("db"))
        with self.assertRaises(paths.NotFound):
            independent.resolve_service("runtime-missing--db")
        value = independent.read("local/a")
        value["clone_pending"] = True
        independent._write(independent.folder("local/a") / "runtime.json", value)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            independent.service("local/a", "db", "start")

    def test_group_controls_target_only_private_members_and_restart_keeps_stopped_services_stopped(self):
        self.prepare("local/a")
        self.prepare("local/b")
        jobs = self.patch(independent, "JOBS")
        self.patch(independent, "containers", return_value={"db": {"State": "running"}, "a-app": {"State": "exited"}})
        for operation, expected in (("restart", ["db"]), ("start", ["db", "a-app"]), ("stop", ["db", "a-app", "a-seed"])):
            independent.service_group("local/a", ["db", "a-app", "a-seed"], operation, "private-group", "Product")
            _, steps, more = self.started(jobs)
            worker = Mock()
            steps[0](worker)
            args = ["up", "-d", "--no-deps"] if operation == "start" else [operation]
            worker.exec.assert_called_once_with(independent.command("local/a", *args, *expected))
            self.assertEqual(more["key"], "group:private-group")
        with self.assertRaises(paths.NotFound):
            independent.service_group("local/a", ["b-app"], "stop", "private-group", "Product")

    def test_required_versions_and_settings_are_checked_without_changing_the_shared_lab(self):
        gathered = self.inputs["local/a"][1]
        gathered["unit"]["requires"] = {"images": {"FIXTURE_VERSION": "2.0.0"}}
        gathered["found"]["services"]["db"]["tag"] = "1.0.0"
        self.patch(independent.images, "components", return_value=[{"id": "FIXTURE_VERSION", "services": ["db"]}])
        with self.assertRaisesRegex(ValueError, "FIXTURE_VERSION"):
            independent.plan("local/a")
        gathered["found"]["services"]["db"]["tag"] = "2.1.0"
        self.assertTrue(independent.plan("local/a")["independent"])
        gathered["unit"].update(settings=[{"key": "FIXTURE_TOKEN", "required": True}], env_file=".env")
        self.patch(independent.layers, "_dotenv", return_value={})
        with self.assertRaisesRegex(ValueError, "FIXTURE_TOKEN"):
            independent.plan("local/a")
        self.assertFalse(independent.folder("local/a").exists())

    def test_success_evidence_uses_pinned_versions_and_private_container_images(self):
        gathered = self.inputs["local/a"][1]
        gathered["unit"]["requires"] = {"images": {"FIXTURE_VERSION": "8.0.0"}}
        gathered["found"]["services"]["db"].update(raw_image="redis:${FIXTURE_VERSION:-8.0.0}", tag="8.0.0",
                                                         owner={"name": "Database"})
        self.patch(independent.images, "components", return_value=[{"id": "FIXTURE_VERSION", "services": ["db"]}])
        value = self.prepare()
        identify = self.patch(compose, "image_key", return_value="sha256:private-image")
        independent._tested("local/a", value)
        proof = independent.tested.sets("local/a")[-1]
        self.assertEqual(proof["versions"], {"FIXTURE_VERSION": "8.0.0"})
        self.assertEqual(proof["images"], {"FIXTURE_VERSION": "sha256:private-image"})
        self.assertEqual(identify.call_args.args[0]["container"], value["project"] + "-db-1")

    def test_catalog_gate_runs_before_a_private_compilation_or_container_start(self):
        independent.catalog_service.preflight.side_effect = RuntimeError("Installed catalog changed")
        jobs = self.patch(independent, "JOBS")
        with self.assertRaisesRegex(RuntimeError, "catalog changed"):
            independent.plan("local/a")
        with self.assertRaisesRegex(RuntimeError, "catalog changed"):
            self.prepare()
        jobs.start.assert_not_called()
        self.assertFalse(independent.folder("local/a").exists())

    def test_apply_refreshes_read_only_inputs_but_keeps_stopped_services_data_and_ports(self):
        config, gathered = self.inputs["local/a"]
        file = gathered["folder"] / "config.txt"
        file.write_text("first")
        config["services"]["db"]["volumes"].append({"type": "bind", "source": str(file), "target": "/config", "read_only": True})
        value = self.prepare()
        value.update(initialized=True, active=True)
        independent._write(independent.folder("local/a") / "runtime.json", value)
        data = Path(value["binds"][str(gathered["folder"] / "data")]) / "value.txt"
        data.write_text("existing runtime data")
        file.write_text("second")
        self.patch(independent, "containers", return_value={"db": {"State": "running"}, "a-app": {"State": "exited"}})
        self.patch(independent, "_wait")
        jobs = self.patch(independent, "JOBS")
        independent.apply("local/a")
        title, steps, kwargs = self.started(jobs)
        worker = Mock()
        steps[0](worker)
        sent = worker.exec.call_args.args[0]
        self.assertEqual((title, kwargs["verb"]), ("Apply sandbox", "apply"))
        self.assertIn("--force-recreate", sent)
        self.assertEqual(sent[-1], "db")
        self.assertNotIn("a-app", sent)
        self.assertNotIn("a-seed", sent)
        current = independent.read("local/a")
        self.assertEqual(current["ports"], value["ports"])
        self.assertTrue(current["initialized"])
        self.assertTrue(current["active"])
        self.assertEqual(Path(current["binds"][str(file)]).read_text(), "second")
        self.assertEqual(data.read_text(), "existing runtime data")
        self.assertEqual((gathered["folder"] / "data/value.txt").read_text(), "initial a")

    def test_apply_while_stopped_saves_settings_for_start_and_does_not_run_the_seed(self):
        value = self.prepare()
        value.update(initialized=True, active=False)
        independent._write(independent.folder("local/a") / "runtime.json", value)
        self.inputs["local/a"][0]["services"]["db"]["environment"] = {"FIXTURE_SETTING": "new"}
        self.patch(independent, "containers", return_value={"db": {"State": "exited"}})
        self.patch(independent, "_wait")
        self.patch(independent, "_tested")
        jobs = self.patch(independent, "JOBS")
        independent.apply("local/a")
        _, steps, _ = self.started(jobs)
        worker = Mock()
        steps[0](worker)
        worker.exec.assert_not_called()
        self.assertFalse(independent.read("local/a")["active"])
        self.assertEqual(self.config()["services"]["db"]["environment"], {"FIXTURE_SETTING": "new"})
        independent.start("local/a")
        _, steps, _ = self.started(jobs)
        steps[0](worker)
        sent = worker.exec.call_args.args[0]
        self.assertNotIn("--no-recreate", sent, "Start must pick up settings applied while containers were stopped")
        self.assertNotIn("a-seed", sent)
        self.assertEqual(independent.read("local/a")["ports"], value["ports"])

    def test_update_uses_captured_configuration_and_only_recreates_running_services(self):
        value = self.prepare()
        value["initialized"] = True
        independent._write(independent.folder("local/a") / "runtime.json", value)
        before = (independent.folder("local/a") / "compose.json").read_bytes()
        self.inputs["local/a"][0]["services"]["db"]["environment"] = {"FIXTURE_SETTING": "not applied"}
        self.patch(independent, "containers", return_value={"db": {"State": "running"}, "a-app": {"State": "exited"}})
        self.patch(independent, "_wait")
        jobs = self.patch(independent, "JOBS")
        independent.update("local/a")
        _, steps, kwargs = self.started(jobs)
        worker = Mock()
        steps[0](worker)
        commands = [call.args[0] for call in worker.exec.call_args_list]
        self.assertEqual(kwargs["verb"], "update")
        self.assertIn("pull", commands[0])
        self.assertIn("--ignore-buildable", commands[0])
        self.assertEqual(commands[-1][-1], "db")
        self.assertNotIn("a-app", commands[-1])
        self.assertNotIn("a-seed", commands[-1])
        self.assertEqual((independent.folder("local/a") / "compose.json").read_bytes(), before)

    def test_sandbox_images_save_and_apply_target_only_the_selected_running_components(self):
        runtime_settings.initialize(["local/a"])
        pid = runtime_settings.create("local/a", "Image test")["id"]
        sibling = runtime_settings.create("local/a", "Keep")["id"]
        project = {"playgrounds": {"local/a": self.inputs["local/a"][1]["unit"]}, "addons": {},
                   "groups": [{"id": "databases", "name": "Databases"}], "services": {
                       "db": {"raw_image": "redis:${REDIS_VERSION:-8}", "image_tags": None, "label": "Redis",
                              "owner": {"id": "databases", "kind": "platform", "name": "Databases"}},
                       "a-app": {"raw_image": "fixture/app:${APP_VERSION:-local}", "image_tags": None, "label": "App",
                                 "owner": {"id": "local/a", "kind": "playground", "name": "a"}}}}
        first = independent.prepare(pid)
        independent.prepare(sibling)
        first.update(initialized=True, snapshot={"references": {"db": "redis:8"}, "local": ["a-app"]})
        independent._write(independent.folder(pid) / "runtime.json", first)
        sibling_before = self.config(sibling)
        config = self.config(pid)
        alias = f"{independent.name(pid)}/db:clone"
        config["services"]["db"].update(image=alias, pull_policy="never")
        independent._write(independent.folder(pid) / "compose.json", config)
        self.patch(compose, "image_ids", return_value={"redis:8": "old", "redis:8.2.1": "new"})
        rows = self.patch(independent, "containers", return_value={"db": {"State": "running", "Image": alias, "ID": "ours"},
                                                                  "a-app": {"State": "running", "Image": "local-app"}})
        tags = self.patch(images, "repository_tags", return_value=[{"name": "8.2.1"}])
        self.assertEqual(runtime_images.tags(project, pid, "REDIS_VERSION")["tags"], [{"name": "8.2.1"}])
        tags.assert_called_once_with(None, "library/redis")
        jobs = self.patch(runtime_images, "JOBS", current=None)
        waited = self.patch(independent, "_wait")
        values = {"REDIS_VERSION": "8.2.1"}
        with self.assertRaisesRegex(ValueError, "Invalid sandbox image version"):
            runtime_images.save(project, pid, {"values": {"APP_VERSION": "8.2.1"}})
        with self.assertRaises(paths.NotFound):
            runtime_images.save(project, "local/a", {"values": values})
        saved = runtime_images.save(project, pid, {"values": values})
        self.assertEqual(len(saved["components"]), 1)
        self.assertEqual(saved["components"][0]["configured"], ["redis:8.2.1"])
        self.assertEqual(saved["components"][0]["running"], ["redis:8"])
        self.assertEqual(saved["components"][0]["services"], [independent.service_key(pid, "db")])
        jobs.start.assert_not_called()
        preview = runtime_images.save(project, pid, {"check": True})
        self.assertEqual([row["id"] for row in preview["restarts"]], ["db"])
        apply = {"apply": True, "fingerprint": preview["fingerprint"]}
        runtime_images.save(project, pid, apply)
        _, steps, kwargs = self.started(jobs)
        worker = Mock()
        rows.return_value["db"]["ID"] = "replaced"
        with self.assertRaisesRegex(ValueError, "changed after the preview"):
            steps[0](worker)
        worker.exec.assert_not_called()
        rows.return_value["db"]["ID"] = "ours"
        steps[0](worker)
        self.assertEqual(kwargs["key"], f"playground:{pid}")
        self.assertEqual([call.args[0] for call in worker.exec.call_args_list],
                         [independent.command(pid, "pull", "db"), independent.command(pid, "up", "-d", "--no-deps", "db")])
        waited.assert_called_once_with(pid, worker, {"db": "ours"})
        self.assertEqual(independent.read(pid)["services"]["db"]["display"]["tag"], "8.2.1")
        self.assertEqual(self.config(sibling), sibling_before)
        self.assertEqual(self.config(pid)["volumes"], config["volumes"])
        self.inputs["local/a"][0]["services"]["db"]["image"] = "redis:9"
        independent.prepare(pid, refresh=True)
        self.assertEqual(self.config(pid)["services"]["db"]["image"], "redis:8.2.1")
        self.assertEqual(independent.read(pid)["ports"], first["ports"])

    def test_cloned_update_refreshes_only_its_image_aliases_and_off_update_is_refused(self):
        value = self.prepare()
        config = self.config()
        references = {"db": "registry.example/store:8", "a-app": "source/app:local", "a-seed": "source/seed:local"}
        for key, service in config["services"].items():
            service["image"] = value["project"] + "/" + key + ":clone"
            service.pop("build", None)
            service["pull_policy"] = "never"
            value["services"][key]["display"] = {"built": key != "db"}
        value.update(initialized=True, snapshot={"references": references})
        independent._write(independent.folder("local/a") / "runtime.json", value)
        independent._write(independent.folder("local/a") / "compose.json", config)
        rows = self.patch(independent, "containers", return_value={"db": {"State": "running"}, "a-app": {"State": "exited"}})
        self.patch(independent, "_wait")
        jobs = self.patch(independent, "JOBS")
        independent.update("local/a")
        _, steps, _ = self.started(jobs)
        worker = Mock()
        steps[0](worker)
        sent = [call.args[0] for call in worker.exec.call_args_list]
        self.assertEqual([cmd for cmd in sent if cmd[:2] == ["docker", "pull"]], [["docker", "pull", references["db"]]])
        tags = [cmd for cmd in sent if cmd[:3] == ["docker", "image", "tag"]]
        self.assertEqual({cmd[-2] for cmd in tags}, set(references.values()))
        self.assertTrue(all(cmd[-1].startswith(value["project"] + "/") for cmd in tags))
        self.assertNotIn("a-app", sent[-1])
        self.assertNotIn("a-seed", sent[-1])
        rows.return_value = {"db": {"State": "exited"}}
        with self.assertRaisesRegex(ValueError, "Start this sandbox first"):
            independent.update("local/a")

    def test_apply_recovers_failed_services_but_never_starts_their_stopped_dependencies(self):
        config, gathered = self.inputs["local/a"]
        config["services"]["a-app"]["depends_on"] = {"db": {"condition": "service_started"}}
        config["services"]["other"] = {"image": "fixture:local", "command": ["sleep", "86400"]}
        gathered["found"]["services"]["other"] = {"oneshot": False}
        value = self.prepare()
        value["initialized"] = True
        independent._write(independent.folder("local/a") / "runtime.json", value)
        rows = self.patch(independent, "containers", return_value={
            "db": {"State": "running"}, "a-app": {"State": "exited", "ExitCode": 1},
            "other": {"State": "exited", "ExitCode": 0}})
        self.patch(independent, "_wait")
        jobs = self.patch(independent, "JOBS")
        independent.apply("local/a")
        _, steps, _ = self.started(jobs)
        worker = Mock()
        steps[0](worker)
        sent = worker.exec.call_args.args[0]
        self.assertIn("db", sent)
        self.assertIn("a-app", sent)
        self.assertNotIn("other", sent)
        self.assertNotIn("a-seed", sent)
        rows.return_value = {"db": {"State": "exited", "ExitCode": 0},
            "a-app": {"State": "exited", "ExitCode": 1}, "other": {"State": "running"}}
        independent.apply("local/a")
        _, steps, _ = self.started(jobs)
        worker = Mock()
        steps[0](worker)
        self.assertEqual(worker.exec.call_args.args[0][-1], "other")
        self.assertNotIn("db", worker.exec.call_args.args[0])
        self.assertNotIn("a-app", worker.exec.call_args.args[0])

    def test_required_plugin_preparation_reports_only_private_consumers_that_need_recreation(self):
        value = self.prepare()
        source = self.inputs["local/a"][1]["folder"] / "data"
        value["plugins"] = {"fixture": {"folder": str(source), "services": {"db": "/plugins"}, "names": ["required-plugin"]}}
        required = self.patch(independent.plugins, "required", return_value=True)
        worker = Mock()
        self.assertEqual(independent._plugins("local/a", value, worker), {"db"})
        target = required.call_args.args[1]["products"]["fixture"]
        self.assertEqual(target["folder"], value["binds"][str(source)])
        self.assertNotEqual(Path(target["folder"]), source)
        required.call_args.args[4](["db"])
        self.assertEqual(worker.exec.call_args.args[0], independent.command("local/a", "pull", "db"))
        required.return_value = False
        self.assertEqual(independent._plugins("local/a", value, worker), set())
