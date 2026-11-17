"""Docker fixture refusals, including failed setup and cleanup, without a Docker daemon.

The real workflows exercise the accepted path in CI; these cases prove foreign inputs
never reach a start, removal or host-file cleanup command.
"""

import copy
import json
import tempfile
import subprocess
import sys
from unittest import mock

from tests import Case, independent_docker, runtime_urls_docker
from lab_ui.docker import testlab as dockersafety
from tests.fakedocker import FakeDocker


class DockerSafety(Case):
    def setUp(self):
        self.folder()
        self.project = "lab-independent-test-123456789abc"
        self.good = {"name": self.project, "volumes": {"data": {"name": self.project + "_data"}},
                     "networks": {"internal": {"name": self.project + "_internal"}},
                     "services": {"db": {"volumes": [{"type": "bind", "source": str(self.root / "data")}]}}}

    def test_resolved_shared_names_external_resources_and_host_paths_are_refused(self):
        cases = []
        for kind in ("volumes", "networks"):
            key = next(iter(self.good[kind]))
            for change in ({"name": "gravitee_data-redis"}, {"external": True},
                           {"driver_opts": {"device": "/existing-lab"}}, {"driver": "remote"}):
                value = copy.deepcopy(self.good)
                value[kind][key].update(change)
                cases.append(value)
        for change in ({"container_name": "gio-redis"}, {"network_mode": "host"}, {"pid": "container:gio-redis"}, {"ipc": "container:gio-redis"},
                       {"volumes_from": ["gio-redis"]}, {"privileged": True},
                       {"volumes": [{"type": "bind", "source": "/var/run/docker.sock", "read_only": True}]},
                       {"build": {"context": "/existing-lab"}}):
            value = copy.deepcopy(self.good)
            value["services"]["db"].update(change)
            cases.append(value)
        for kind in ("secrets", "configs"):
            for change in ({"file": "/existing-lab/secret"}, {"external": True}):
                cases.append({**self.good, kind: {"secret": change}})
        cases.append({**self.good, "name": "gravitee"})
        for value in cases:
            with self.subTest(value=value):
                run = mock.Mock()
                with self.assertRaises(AssertionError):
                    dockersafety.resources(value, self.project, (self.root,), run, fresh=True)
                run.assert_not_called()

    def test_symlinks_and_relative_parent_paths_cannot_escape_the_temporary_roots(self):
        with tempfile.TemporaryDirectory() as other:
            (self.root / "escape").symlink_to(other, target_is_directory=True)
            for source in (self.root / "escape/data", self.root / "../outside"):
                value = copy.deepcopy(self.good)
                value["services"]["db"]["volumes"][0]["source"] = str(source)
                with self.assertRaises(AssertionError):
                    dockersafety.config(value, self.project, (self.root,))

    def test_clean_fixture_and_owned_resources_are_accepted(self):
        run = mock.Mock(return_value="")
        dockersafety.resources(self.good, self.project, (self.root,), run, fresh=True)
        run = mock.Mock(side_effect=["", self.project + "_data", json.dumps([
            {"Labels": {dockersafety.PROJECT: self.project}, "Driver": "local"}]), ""])
        dockersafety.resources(self.good, self.project, (self.root,), run)

    def test_existing_names_foreign_labels_and_network_members_refuse_cleanup(self):
        volume = self.project + "_data"
        network = self.project + "_internal"
        cases = [(["container"], True), (["", volume], True),
                 (["", volume, json.dumps([{"Labels": {dockersafety.PROJECT: "gravitee"}}])], False),
                 (["", "", network, json.dumps([{"Labels": {dockersafety.PROJECT: self.project},
                                                  "Containers": {"foreign": {}}}])], False)]
        for answers, fresh in cases:
            with self.subTest(answers=answers):
                run = mock.Mock(side_effect=answers)
                with self.assertRaises(AssertionError):
                    dockersafety.resources(self.good, self.project, (self.root,), run, fresh=fresh)
                self.assertFalse(any("rm" in call.args[0] or "down" in call.args[0] for call in run.call_args_list))

    def test_inspection_failure_is_not_treated_as_an_absent_resource(self):
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            dockersafety.resources(self.good, self.project, (self.root,), mock.Mock(side_effect=RuntimeError("unavailable")))

    def test_setup_refusal_does_not_register_docker_cleanup_or_start_any_container(self):
        fixture = independent_docker.IndependentDocker()
        def make_lab():
            (fixture.root / "docker-compose.yml").write_text("{}")
        fixture.make_lab = make_lab
        fixture.raw_command = mock.Mock(return_value=json.dumps({**self.good, "name": "gravitee"}))
        try:
            with self.assertRaises(AssertionError):
                fixture.setUp()
            self.assertFalse(any(fn == fixture.cleanup_lab for fn, _, _ in fixture._cleanups))
            self.assertTrue(all("config" in call.args[0] for call in fixture.raw_command.call_args_list))
        finally:
            fixture.doCleanups()

    def test_changed_cleanup_config_is_refused_before_removing_containers_or_chowning_files(self):
        fixture = independent_docker.IndependentDocker()
        fixture.root, fixture.linked, fixture.name = self.root, self.root, self.project
        (self.root / "docker-compose.yml").write_text("{}")
        value = copy.deepcopy(self.good)
        value["volumes"]["data"]["name"] = "gravitee_data-redis"
        fixture.raw_command = mock.Mock(return_value=json.dumps(value))
        with self.assertRaises(AssertionError):
            fixture.cleanup_lab()
        self.assertTrue(all("config" in call.args[0] for call in fixture.raw_command.call_args_list))

    def test_the_url_workflow_never_removes_a_planned_volume_it_did_not_create(self):
        commands = []
        def run(command):
            commands.append(command)
            return "lab-clone-urls-aaaaaaaaaaaa-source"
        with mock.patch.object(runtime_urls_docker.uuid, "uuid4", return_value=mock.Mock(hex="a" * 32)), \
                mock.patch.object(runtime_urls_docker.runtime_clones, "_execute", side_effect=run):
            with self.assertRaisesRegex(AssertionError, "already exists"):
                runtime_urls_docker.RuntimeUrlsDocker().test_only_the_copied_database_changes_and_bson_credentials_survive()
        self.assertTrue(all(command[:3] == ["docker", "volume", "ls"] for command in commands))


class FakeDockerSafety(Case):
    def test_daemon_commands_and_config_output_are_never_forwarded(self):
        fake = FakeDocker(self)
        real = fake.folder / "real-docker"
        marker = fake.folder / "forwarded"
        real.write_text("#!" + sys.executable + "\nfrom pathlib import Path\nPath(" + repr(str(marker)) + ").touch()\n")
        real.chmod(0o700)
        (fake.folder / "real.json").write_text(json.dumps(str(real)))
        for tail in (["up", "-d"], ["down", "-v"], ["stop"], ["restart"], ["exec", "redis", "redis-cli", "FLUSHALL"],
                     ["rm", "-f"], ["run", "--rm", "redis"], ["config", "--output", "/existing-lab/file"],
                     ["config", "--output=/existing-lab/file"], ["config", "-oexisting-data"], ["--unknown", "config", "down"]):
            with self.subTest(tail=tail):
                done = subprocess.run([str(fake.bin / "docker"), "compose", "--progress", "plain", "-f", "config",
                                       *tail], capture_output=True)
                self.assertNotEqual(done.returncode, 0)
                self.assertFalse(marker.exists())
        for tail in (["version", "--short"], ["config", "--format", "json"]):
            done = subprocess.run([str(fake.bin / "docker"), "compose", "--progress=plain", *tail], capture_output=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertTrue(marker.exists())
            marker.unlink()
