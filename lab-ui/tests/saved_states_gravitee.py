"""Save/restore through the real Lab, seeder, Gravitee management API and gateway.

Unlike saved_states_docker, this exercises HTTP playground start/switch/stop jobs and
real APIM resources. Run explicitly: PYTHONPATH=lab-ui python3 -m tests.saved_states_gravitee.
Every container, network, volume and published port belongs to a unique temporary lab.
"""

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from lab_ui.docker import compose

SOURCE = Path(__file__).resolve().parents[2]
ENVIRONMENT = "/management/v2/organizations/DEFAULT/environments/DEFAULT"


class GraviteeStates(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lab-gravitee-states-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.name = "lab-gravitee-test-" + uuid.uuid4().hex[:12]
        self.seed_image = self.name + "/seeder:local"
        self.env = {key: os.environ[key] for key in ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR",
                                                   "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG") if key in os.environ}
        self.env.update(compose.NO_REMOTE, COMPOSE_FILE="docker-compose.yml:.lab/on.yml")
        self.log = self.root / "lab.log"
        self.addCleanup(self.cleanup_lab)
        self.make_lab()
        print("Starting isolated Gravitee lab " + self.name, flush=True)
        self.command([*compose.COMPOSE, "up", "-d", "--wait", "--wait-timeout", "600"], timeout=660)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.lab = "http://127.0.0.1:" + str(port)
        self.output = open(self.log, "w", encoding="utf-8")
        self.process = subprocess.Popen([sys.executable, "lab-ui/server.py", "--no-open", "--port", str(port)],
                                        cwd=self.root, env=self.env, stdout=self.output, stderr=subprocess.STDOUT)
        self.until(lambda: self.request(self.lab + "/api/state")[0] == 200, "Lab HTTP server", timeout=120)

    def command(self, command, timeout=120):
        done = subprocess.run(command, cwd=self.root, env=self.env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
        self.assertEqual(done.returncode, 0, " ".join(command) + "\n" + done.stdout[-12000:])
        return done.stdout.strip()

    def make_lab(self):
        shutil.copytree(SOURCE / "lab-ui", self.root / "lab-ui", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(SOURCE / "seeder", self.root / "seeder", ignore=shutil.ignore_patterns("__pycache__"))
        (self.root / ".lab").mkdir()
        (self.root / ".lab/on.yml").write_text("")
        # Resolve the real core without any settings or credentials from the user's Lab.
        core = subprocess.run([*compose.COMPOSE, "-f", str(SOURCE / "docker-compose.yml"), "config", "--format", "json"],
                              cwd=SOURCE, env=compose.scrubbed_environment(), capture_output=True, text=True, check=True)
        core = json.loads(core.stdout)
        core["name"] = self.name
        names = ("mongodb", "elasticsearch", "redis", "apim-management-api", "apim-gateway")
        core["services"] = {name: core["services"][name] for name in names}
        for name, service in core["services"].items():
            service.pop("container_name", None)
            service["ports"] = ([{"target": 8083 if name == "apim-management-api" else 8082,
                                  "published": "0", "host_ip": "127.0.0.1", "protocol": "tcp"}]
                                if name in ("apim-management-api", "apim-gateway") else [])
            for volume in service.get("volumes", []):
                if volume["type"] == "bind":
                    relative = Path(volume["source"]).relative_to(SOURCE)
                    volume["source"] = str(self.root / relative)
            for network in service.get("networks", {}):
                core["networks"][network].pop("name", None)
            environment = service.get("environment", {})
            environment["gravitee_alerts_alertengine_enabled"] = "false"
            environment["gravitee_gamma_enabled"] = "false"
            if name == "elasticsearch":
                environment["ES_JAVA_OPTS"] = "-Xms256m -Xmx256m"
            if name == "apim-gateway":
                environment["GIO_MIN_MEM"] = "256m"
                environment["GIO_MAX_MEM"] = "512m"
                environment["gravitee_services_sync_cron"] = "*/2 * * * * *"
        for volume in core["volumes"].values():
            volume.pop("name", None)
        core["x-gravitee"]["seeder"]["image"] = self.seed_image.rsplit(":", 1)[0]
        (self.root / "docker-compose.yml").write_text(json.dumps(core, indent=2))
        (self.root / "certs/mtls").mkdir(parents=True)
        shutil.copy2(SOURCE / "certs/mtls/generate.sh", self.root / "certs/mtls/generate.sh")
        self.command(["sh", "certs/mtls/generate.sh"])
        self.command(["docker", "build", "-t", self.seed_image, "seeder"], timeout=600)
        for letter in ("a", "b"):
            self.make_playground(letter)

    def make_playground(self, letter):
        folder = self.root / ".lab/catalogs/local/playgrounds" / letter
        (folder / "seed/apim/apis").mkdir(parents=True)
        (folder / ".state").mkdir()
        (folder / "backend").mkdir()
        for version in ("seed", "edited", "edited-again"):
            (folder / "backend" / version).mkdir()
            (folder / "backend" / version / "index.html").write_text(letter + ":" + version)
        definition = json.loads((SOURCE / "lab-ui/tests/data/local/playgrounds/sandbox/seed/apim/apis/sandbox.json").read_text())
        definition["api"].update(name="Saved state " + letter.upper(), description="Seed definition " + letter)
        definition["api"]["listeners"][0]["paths"][0]["path"] = "/state-" + letter + "/"
        definition["api"]["endpointGroups"][0]["sharedConfiguration"] = {}
        definition["api"]["endpointGroups"][0]["endpoints"][0]["configuration"]["target"] = "${STATE_BACKEND_URL}"
        (folder / "seed/apim/apis/api.json").write_text(json.dumps(definition))
        services = {
            letter + "-backend": {
                "image": "busybox:1.37.0", "command": ["httpd", "-f", "-p", "8080", "-h", "/www"],
                "volumes": ["./backend:/www:ro"], "networks": ["internal"],
                "labels": {"io.gravitee.name": "Backend " + letter,
                           "io.gravitee.description": "Serves the saved API's responses"},
            },
            letter + "-seed": {
                "image": self.seed_image, "pull_policy": "never", "restart": "no",
                "labels": {"io.gravitee.role": "seed", "io.gravitee.name": "Seed " + letter,
                           "io.gravitee.description": "Creates the API and records ownership"},
                "depends_on": {"apim-management-api": {"condition": "service_healthy"},
                               "apim-gateway": {"condition": "service_healthy"},
                               letter + "-backend": {"condition": "service_started"}},
                "volumes": ["./seed:/seed:ro", "./.state:/state"], "networks": ["internal"],
                "environment": {"APIM_MANAGEMENT_URL": "http://apim-management-api:8083", "APIM_USERNAME": "admin",
                                "APIM_PASSWORD": "${STATE_TEST_PASSWORD:-admin}",
                                "STATE_BACKEND_URL": "http://" + letter + "-backend:8080/seed/"},
            },
        }
        content = {"x-gravitee": {"kind": "playground", "name": "State " + letter.upper(), "category": "Other",
                                 "description": "API checkpoint test " + letter, "tags": ["API"],
                                 "duration": "1 min", "first_start": "1 min"}, "services": services}
        (folder / "compose.yml").write_text(json.dumps(content, indent=2))

    def cleanup_lab(self):
        if hasattr(self, "process"):
            self.process.terminate()
            self.process.wait(timeout=30)
            self.output.close()
        if not (self.root / "docker-compose.yml").exists():
            return
        # Persist diagnostics before deleting only this test project's resources.
        artifacts = os.environ.get("GRAVITEE_STATE_E2E_ARTIFACTS")
        logs = subprocess.run([*compose.COMPOSE, "-p", self.name, "logs", "--no-color", "--tail", "150"],
                              cwd=self.root, env=self.env, capture_output=True, text=True, timeout=30)
        if artifacts:
            destination = Path(artifacts)
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "containers.log").write_text(logs.stdout + logs.stderr)
            if self.log.exists():
                shutil.copy2(self.log, destination / "lab.log")
        try:
            self.command([*compose.COMPOSE, "-p", self.name, "down", "-v", "--remove-orphans"])
        finally:
            for manifest in self.root.glob(".lab/saved-states/*/*/manifest.json"):
                for tag in {image["locked_tag"] for image in json.loads(manifest.read_text())["images"]}:
                    self.command(["docker", "image", "rm", tag])
            subprocess.run(["docker", "image", "rm", self.seed_image], cwd=self.root, env=self.env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)

    def request(self, url, method="GET", body=None, auth=False):
        headers = {"Content-Type": "application/json"}
        if url.startswith(getattr(self, "lab", "http://invalid")):
            headers["X-Gravitee-Lab-UI"] = "1"
        if auth:
            headers["Authorization"] = "Basic " + base64.b64encode(b"admin:admin").decode()
        request = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                         headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.read().decode()

    def json(self, url, method="GET", body=None, auth=False, expected=(200, 201, 202, 204)):
        status, text = self.request(url, method, body, auth)
        self.assertIn(status, expected, str(status) + " " + url + " " + text[-4000:])
        return json.loads(text) if text else None

    def until(self, predicate, what, timeout=300):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return
            except (OSError, urllib.error.URLError) as error:
                last = error
            time.sleep(2)
        self.fail("Timed out waiting for " + what + (": " + str(last) if last else ""))

    def endpoint(self, service, port):
        address = self.command([*compose.COMPOSE, "port", service, str(port)]).splitlines()[0]
        return "http://" + address

    def management(self, path="", method="GET", body=None, expected=(200, 201, 202, 204)):
        return self.json(self.endpoint("apim-management-api", 8083) + ENVIRONMENT + path,
                         method, body, auth=True, expected=expected)

    def job(self, path, body):
        job = self.json(self.lab + path, "POST", body)
        jid = job["id"]

        def completed():
            state = self.json(self.lab + "/api/state")
            current = state.get("job") or {}
            self.assertEqual(current.get("id"), jid)
            if not current.get("done"):
                return False
            self.assertTrue(current["ok"], current.get("error") or str(current))
            return True

        self.until(completed, "Lab job " + jid, timeout=600)

    def api(self, letter):
        rows = self.management("/apis?perPage=100")["data"]
        return next(row for row in rows if row["name"] == "Saved state " + letter.upper())

    def edit(self, letter, version):
        api = self.management("/apis/" + self.api(letter)["id"])
        api["description"] = "User edit " + letter + ":" + version
        api["endpointGroups"][0]["endpoints"][0]["configuration"]["target"] = "http://" + letter + "-backend:8080/" + version + "/"
        self.management("/apis/" + api["id"], "PUT", api)
        self.management("/apis/" + api["id"] + "/deployments", "POST", {"deploymentLabel": "User edits"})
        self.assert_serves(letter, version)
        return self.management("/apis/" + api["id"])

    def assert_serves(self, letter, version):
        def correct():
            status, text = self.request(self.endpoint("apim-gateway", 8082) + "/state-" + letter + "/")
            return status == 200 and text == letter + ":" + version
        self.until(correct, "gateway response " + letter + ":" + version, timeout=120)

    def states(self):
        return self.json(self.lab + "/api/playgrounds/local/a/saved-states")["states"]

    def assert_restored(self, sid, expected):
        self.job("/api/playgrounds/local/a/saved-states/" + sid + "/restore", {"confirm": True})
        actual = self.management("/apis/" + expected["id"])
        for key in ("id", "name", "description", "endpointGroups", "plans", "state", "lifecycleState"):
            if key in expected:
                self.assertEqual(actual.get(key), expected[key], key)
        version = expected["description"].split(":", 1)[1]
        self.assert_serves("a", version)
        self.assertEqual(self.command([*compose.COMPOSE, "ps", "-aq", "a-seed"]), "", "Restore ran the seed again")

    def test_saved_api_edits_survive_switch_stop_and_multiple_restores(self):
        print("Creating and editing playground A through Gravitee APIs", flush=True)
        self.job("/api/playgrounds/local/a/start", {})
        first_api = self.edit("a", "edited")
        print("Saving A before seeder cleanup while switching to B", flush=True)
        self.job("/api/playgrounds/local/b/start", {"remove": True, "save": True, "accept": ["playground:local/a"]})
        first = self.states()[0]["id"]
        self.management("/apis/" + first_api["id"], expected=(404,))
        self.assert_serves("b", "seed")
        b_id = self.api("b")["id"]
        print("Restoring A and checking API identity and real gateway traffic", flush=True)
        self.assert_restored(first, first_api)
        self.management("/apis/" + b_id, expected=(404,))
        self.assertTrue(next(row for row in self.json(self.lab + "/api/state")["playgrounds"] if row["id"] == "local/a")["on"])
        second_api = self.edit("a", "edited-again")
        print("Saving a second checkpoint using Stop, then restoring both versions", flush=True)
        self.job("/api/playgrounds/local/a/stop", {"remove": True, "save": True})
        self.management("/apis/" + first_api["id"], expected=(404,))
        states = self.states()
        self.assertEqual(len(states), 2)
        second = next(state["id"] for state in states if state["id"] != first)
        self.assert_restored(second, second_api)
        self.assert_restored(first, first_api)
        self.json(self.lab + "/api/playgrounds/local/a/saved-states/" + second + "/delete", "POST", {})
        self.assertEqual([state["id"] for state in self.states()], [first])
        self.assert_serves("a", "edited")
        print("Verified real Gravitee API save/switch/restore/stop/resave and checkpoint deletion", flush=True)


if __name__ == "__main__":
    unittest.main()
