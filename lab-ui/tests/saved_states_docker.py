"""Real Docker checkpoint transactions, separate from the daemon-free unit suite.

Run from the repository with PYTHONPATH=lab-ui python3 -m tests.saved_states_docker.
Only a unique temporary Compose project is created and removed; an existing Lab is untouched.
"""

import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from lab_ui import paths
from lab_ui.docker import compose
from lab_ui.docker.jobs import Job, JobFailed, Jobs
from lab_ui.library import saved_states


class DockerStates(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lab-saved-states-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.linked_temp = tempfile.TemporaryDirectory(prefix="lab-state-folder-")
        self.addCleanup(self.linked_temp.cleanup)
        self.linked = Path(self.linked_temp.name).resolve()
        self.machine = self.linked / ".worktrees/playgrounds/demo/.state"
        self.machine.mkdir(parents=True)
        (self.machine.parent / "compose.yml").write_text("services: {}\n")
        (self.linked / "playgrounds").mkdir()
        (self.linked / "playgrounds/demo").symlink_to(self.machine.parent, target_is_directory=True)
        self.project = "lab-state-test-" + uuid.uuid4().hex[:12]
        self.states = self.root / ".lab/saved-states"
        self.states.mkdir(parents=True)
        self.catalog = self.root / ".lab/catalogs/local/playgrounds/demo"
        self.catalog.mkdir(parents=True)
        (self.catalog / "edited.json").write_text('{"api":"edited before save"}')
        (self.root / ".lab/catalogs/folder").symlink_to(self.linked, target_is_directory=True)
        (self.root / ".lab/catalogs.json").write_text(json.dumps({"schema": 1, "catalogs": [
            {"id": "folder", "kind": "folder", "path": str(self.linked)}]}))
        (self.root / ".lab/on.yml").write_text("services: {}\n")
        (self.root / "docker-compose.yml").write_text(f"""name: {self.project}
services:
  app:
    image: alpine:3.20
    command: [sh, -c, 'exec sleep 86400']
    volumes:
      - data:/data
      - ./.lab/catalogs/folder/playgrounds/demo/.state:/state
  database:
    image: redis:8.10.2-alpine
    command: [redis-server, --appendonly, 'yes']
    volumes:
      - database:/data
    healthcheck:
      test: [CMD, redis-cli, ping]
      interval: 1s
      timeout: 1s
      retries: 30
  seed:
    image: alpine:3.20
    command: [sh, -c, 'echo seed-ran >> /data/seed-runs']
    labels:
      io.gravitee.role: seed
    volumes:
      - data:/data
volumes:
  data: {{}}
  database: {{}}
""")
        for module, name, value in ((saved_states, "ROOT", self.root),
                                    (saved_states, "STATES", self.states),
                                    (compose, "ROOT", self.root), (paths, "ROOT", self.root),
                                    (saved_states, "JOBS", Jobs())):
            patch = mock.patch.object(module, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        compose._name.clear()
        self.addCleanup(compose._name.clear)
        self.model = {"playgrounds": {"local/demo": {}}}
        self.job = Job("Docker checkpoint test", [], key="playground:local/demo", verb="stop")
        self.addCleanup(self.cleanup_docker)
        self.exec([*compose.COMPOSE, "up", "-d", "--wait", "app", "database"])
        self.exec([*compose.COMPOSE, "run", "--rm", "--no-deps", "seed"])

    def exec(self, command):
        rc, out, err = compose.run(command, timeout=120)
        self.assertEqual(rc, 0, err or out)
        return out.strip()

    def cleanup_docker(self):
        self.exec([*compose.COMPOSE, "down", "-v", "--remove-orphans"])
        # Include volumes that a deliberately failed restore left outside the Compose model.
        names = self.exec(["docker", "volume", "ls", "-q", "--filter",
                           "label=com.docker.compose.project=" + self.project]).split()
        if names:
            self.exec(["docker", "volume", "rm", *names])
        for folder in self.states.rglob("manifest.json"):
            for tag in {item["locked_tag"] for item in json.loads(folder.read_text())["images"]}:
                self.exec(["docker", "image", "rm", tag])

    def write(self, value):
        (self.machine / "created.json").write_text(value)
        self.exec([*compose.COMPOSE, "exec", "-T", "database", "redis-cli", "SET", "edited-api", value])
        self.exec([*compose.COMPOSE, "exec", "-T", "app", "sh", "-c",
                   "printf '%s' '" + value + "' > /data/api.json"])

    def read(self):
        value = self.exec([*compose.COMPOSE, "exec", "-T", "app", "cat", "/data/api.json"])
        self.assertEqual(self.exec([*compose.COMPOSE, "exec", "-T", "database", "redis-cli", "GET", "edited-api"]), value)
        self.assertEqual(self.exec([*compose.COMPOSE, "exec", "-T", "app", "cat", "/state/created.json"]), value)
        self.assertEqual((self.machine / "created.json").read_text(), value)
        self.assertTrue((self.root / ".lab/catalogs/folder").is_symlink())
        self.assertTrue((self.linked / "playgrounds/demo").is_symlink())
        return value

    def save(self, name):
        return saved_states.capture(self.job, self.model, "local/demo", name)

    def restore(self, sid, ok=True):
        job = saved_states.restore(self.model, "local/demo", sid, confirmed=True)
        deadline = time.monotonic() + 90
        while not job.done and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(job.done, "Restore did not finish")
        self.assertEqual(job.ok, ok, "\n".join(job.lines))
        return job

    def test_multiple_checkpoints_restore_and_resave_exact_data_without_reseeding(self):
        self.write("edited API A")
        first = self.save("Before more edits")
        self.write("edited API B")
        (self.catalog / "edited.json").write_text('{"api":"later edit"}')
        second = self.save("After more edits")
        self.write("data created after save")
        (self.catalog / "stale.txt").write_text("remove on restore")
        self.restore(first)
        self.assertEqual(self.read(), "edited API A")
        self.assertEqual(json.loads((self.catalog / "edited.json").read_text())["api"], "edited before save")
        self.assertFalse((self.catalog / "stale.txt").exists())
        self.assertEqual(self.exec([*compose.COMPOSE, "exec", "-T", "app", "cat", "/data/seed-runs"]), "seed-ran")
        third = self.save("After restore")
        self.restore(second)
        self.assertEqual(self.read(), "edited API B")
        self.restore(third)
        self.assertEqual(self.read(), "edited API A")
        saved_states.delete_state(self.model, "local/demo", second)
        self.assertEqual({row["id"] for row in saved_states.list_states("local/demo")}, {first, third})
        self.assertEqual(self.read(), "edited API A")

    def test_failed_restore_recovers_live_data_and_removes_new_volumes(self):
        extra = self.project + "_extra"
        self.exec(["docker", "volume", "create", "--label", "com.docker.compose.project=" + self.project, extra])
        self.write("saved A")
        sid = self.save("A")
        self.exec(["docker", "volume", "rm", extra])
        self.write("live B")
        original = saved_states._retag_images
        calls = []

        def fail_once(job, images):
            calls.append(True)
            if len(calls) == 1:
                raise JobFailed("Injected startup failure")
            return original(job, images)

        with mock.patch.object(saved_states, "_retag_images", side_effect=fail_once):
            job = self.restore(sid, ok=False)
        self.assertIn("Injected startup failure", job.error())
        self.assertEqual(self.read(), "live B")
        self.assertNotIn(extra, self.exec(["docker", "volume", "ls", "-q"]).split())
        self.assertEqual(list(self.states.glob(".rollback-*")), [])
        self.assertEqual(len(saved_states.list_states("local/demo")), 1)
        self.restore(sid)
        self.assertEqual(self.read(), "saved A")


if __name__ == "__main__":
    unittest.main()
