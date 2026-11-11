"""Real Docker adapter isolation and transaction-file handling, without a Gravitee platform.

A small stand-in adapter proves the engine mounts only the selected ownership folder,
runs current source over an existing image, and never rewinds a shared Redis database.
The real adapter and management APIs are exercised by saved_states_gravitee.
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

ADAPTER = '''import json, sys
from pathlib import Path
operation, file = sys.argv[1:]
p = Path(file)
state = Path('/state')
if (state/'crash').exists():
    raise SystemExit(2)
result = {'ok': True}
if (state/'refuse').exists():
    result = {'ok': False, 'error': 'Unsupported resource kind; nothing was deleted'}
elif operation == 'save':
    p.write_text(json.dumps({'scope': 'playground', 'value': (state/'value').read_text()}))
elif operation == 'restore':
    if (state/'fail-recovery').exists():
        (p.parent/'previous.json').write_text(json.dumps({'value': (state/'value').read_text()}))
        result = {'ok': False, 'error': 'Recovery incomplete'}
    else:
        (state/'value').write_text(json.loads(p.read_text())['value'])
(p.parent/'result.json').write_text(json.dumps(result))
'''


class DockerStates(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lab-scoped-states-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.linked_temp = tempfile.TemporaryDirectory(prefix="lab-state-worktree-")
        self.addCleanup(self.linked_temp.cleanup)
        self.linked = Path(self.linked_temp.name).resolve()
        self.machine = self.linked / ".state"
        self.machine.mkdir()
        (self.machine / "value").write_text("Saved own data")
        self.catalog = self.root / ".lab/catalogs/local/playgrounds/demo"
        self.catalog.parent.mkdir(parents=True)
        (self.root / ".lab/on.yml").write_text("services: {}\n")
        self.catalog.symlink_to(self.linked, target_is_directory=True)
        (self.linked / "compose.yml").write_text('services: {}\n')
        self.other = self.catalog.parent / "other/.state"
        self.other.mkdir(parents=True)
        (self.other / "value").write_text("Other playground original")
        source = self.root / "seeder/lab_seeder"
        source.mkdir(parents=True)
        (source / "__init__.py").touch()
        (source / "checkpoints.py").write_text(ADAPTER)
        self.name = "lab-state-test-" + uuid.uuid4().hex[:12]
        (self.root / "docker-compose.yml").write_text(f'''name: {self.name}
services:
  shared:
    image: redis:8.10.2-alpine
    volumes: [database:/data]
    healthcheck:
      test: [CMD, redis-cli, ping]
      interval: 1s
      timeout: 1s
      retries: 30
  demo-seed:
    image: python:3.15.1-slim
    volumes:
      - ./.lab/catalogs/local/playgrounds/demo/.state:/state
volumes:
  database: {{}}
''')
        self.states = self.root / ".lab/saved-states"
        for module, name, value in ((saved_states, "ROOT", self.root), (saved_states, "STATES", self.states),
                                    (compose, "ROOT", self.root), (paths, "ROOT", self.root),
                                    (saved_states, "JOBS", Jobs())):
            patch = mock.patch.object(module, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        compose._name.clear()
        self.addCleanup(compose._name.clear)
        self.model = {"playgrounds": {"local/demo": {"seed": "demo-seed", "file": str(self.linked / "compose.yml")}}}
        self.job = Job("Playground checkpoint", [], key="playground:local/demo", verb="stop")
        self.addCleanup(self.cleanup_docker)
        self.exec([*compose.COMPOSE, "up", "-d", "--wait", "shared"])
        self.exec(["docker", "pull", "python:3.15.1-slim"])
        self.exec([*compose.COMPOSE, "exec", "-T", "shared", "redis-cli", "SET", "other-playground", "before"])

    def exec(self, command):
        rc, out, err = compose.run(command, timeout=120)
        self.assertEqual(rc, 0, err or out)
        return out.strip()

    def cleanup_docker(self):
        self.exec([*compose.COMPOSE, "down", "-v", "--remove-orphans"])

    def restore(self, sid, ok=True):
        current = saved_states.restore(self.model, "local/demo", sid, confirmed=True)
        deadline = time.monotonic() + 60
        job = saved_states.JOBS.current
        self.assertEqual(current.id, job.id)
        while not job.done and time.monotonic() < deadline:
            time.sleep(.1)
        self.assertTrue(job.done, "Restore did not finish")
        self.assertEqual(job.ok, ok, job.error)

    def test_linked_playground_restore_keeps_other_playgrounds_and_shared_database_live(self):
        shared = self.exec([*compose.COMPOSE, "ps", "-q", "shared"])
        started = self.exec(["docker", "inspect", "--format", "{{.State.StartedAt}}", shared])
        sid = saved_states.capture(self.job, self.model, "local/demo")
        (self.machine / "value").write_text("Own later data")
        (self.other / "value").write_text("Other playground edited after save")
        self.exec([*compose.COMPOSE, "exec", "-T", "shared", "redis-cli", "SET", "other-playground", "after-save"])
        self.restore(sid)
        self.assertEqual((self.machine / "value").read_text(), "Saved own data")
        self.assertEqual((self.other / "value").read_text(), "Other playground edited after save")
        self.assertEqual(self.exec([*compose.COMPOSE, "exec", "-T", "shared", "redis-cli", "GET", "other-playground"]), "after-save")
        self.assertEqual(self.exec([*compose.COMPOSE, "ps", "-q", "shared"]), shared)
        self.assertEqual(self.exec(["docker", "inspect", "--format", "{{.State.StartedAt}}", shared]), started)
        self.assertTrue(self.catalog.is_symlink())
        files = {p.name for p in saved_states._location("local/demo", sid).iterdir()}
        self.assertEqual(files, {"manifest.json", "resources.json"})

    def test_adapter_refusal_and_failed_recovery_keep_data_and_only_own_recovery_files(self):
        (self.machine / "crash").touch()
        with self.assertRaisesRegex(JobFailed, "stopped without a result"):
            saved_states.capture(self.job, self.model, "local/demo")
        (self.machine / "crash").unlink()
        (self.machine / "refuse").touch()
        with self.assertRaisesRegex(JobFailed, "Unsupported resource"):
            saved_states.capture(self.job, self.model, "local/demo")
        self.assertEqual(saved_states.list_states("local/demo"), [])
        (self.machine / "refuse").unlink()
        sid = saved_states.capture(self.job, self.model, "local/demo")
        (self.machine / "value").write_text("Own current data")
        (self.machine / "fail-recovery").touch()
        self.restore(sid, ok=False)
        retained = list(self.states.glob(".restore-*/previous.json"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(json.loads(retained[0].read_text()), {"value": "Own current data"})
        self.assertEqual((self.other / "value").read_text(), "Other playground original")


if __name__ == "__main__":
    unittest.main()
