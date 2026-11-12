"""Independent runtime generation owns mutable storage, ports and image names without changing linked sources.

The deterministic compiler and lifecycle cases here complement the real multi-project
Docker test, which proves data and seed identity survive stop/resume alongside a shared Lab.
"""

import copy
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock

from lab_ui import paths
from lab_ui.catalogs import independent, standalone
from lab_ui.docker import compose
from tests import Case


class Independent(Case):
    def setUp(self):
        self.folder(paths)
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

    def test_prepare_and_rebuild_preserve_runtime_storage_and_port_assignments(self):
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
        self.assertEqual(self.prepare(rebuild=True)["ports"], value["ports"])
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
        self.assertIn("--no-recreate", sent)
        self.assertIn("a-app", sent)
        self.assertNotIn("a-seed", sent)
        independent.stop("local/a")
        _, steps, _ = self.started(jobs)
        steps[0](worker)
        self.assertEqual(worker.exec.call_args.args[0][-1], "stop")
        self.assertNotIn("down", worker.exec.call_args.args[0])
        self.assertTrue(job.called)

    def test_seed_endpoints_cannot_target_a_shared_or_remote_management_api(self):
        seed = self.inputs["local/a"][0]["services"]["a-seed"]
        for endpoint in ("http://localhost:8083", "https://management.example.test"):
            seed["environment"] = {"APIM_MANAGEMENT_URL": endpoint}
            with self.subTest(endpoint=endpoint), self.assertRaisesRegex(ValueError, "must address a service"):
                independent.plan("local/a")
        seed["environment"] = {"APIM_MANAGEMENT_URL": "http://db:8083"}
        self.assertTrue(independent.plan("local/a")["independent"])

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
