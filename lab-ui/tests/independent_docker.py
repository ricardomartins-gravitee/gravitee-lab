"""Real independent playgrounds through Lab HTTP, Gravitee APIs, gateway and linked catalogs.

Run explicitly: PYTHONPATH=lab-ui python3 -m tests.independent_docker.
Only uniquely named temporary projects are started or removed. No installed Lab is used.
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

from lab_ui.docker import compose, storage

SOURCE = Path(__file__).resolve().parents[2]
ENVIRONMENT = "/management/v2/organizations/DEFAULT/environments/DEFAULT"


class IndependentDocker(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lab-independent-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.linked_temp = tempfile.TemporaryDirectory(prefix="lab-linked-catalog-")
        self.addCleanup(self.linked_temp.cleanup)
        self.linked = Path(self.linked_temp.name).resolve()
        self.name = "lab-independent-test-" + uuid.uuid4().hex[:12]
        self.seed_image = self.name + "/seeder:local"
        self.env = {key: os.environ[key] for key in ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR",
                                                   "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG") if key in os.environ}
        self.env.update(compose.NO_REMOTE, COMPOSE_FILE="docker-compose.yml:.lab/on.yml")
        self.log = self.root / "lab.log"
        self.log.touch()
        self.addCleanup(self.cleanup_lab)
        self.make_lab()
        print("Starting isolated Gravitee lab " + self.name, flush=True)
        self.command([*compose.COMPOSE, "up", "-d", "--wait", "redis"], timeout=120)
        self.command([*compose.COMPOSE, "exec", "-T", "redis", "redis-cli", "SET", "shared-sentinel", "keep-original"])
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
        (self.root / "plugins/apim").mkdir(parents=True)
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
            if name in ("apim-management-api", "apim-gateway"):
                service.setdefault("labels", {})["io.gravitee.console.test.url"] = "http://localhost:" + str(8083 if name == "apim-management-api" else 8082)
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
        concurrent = self.root / ".lab/catalogs/local/playgrounds/concurrent"
        concurrent.mkdir(parents=True)
        (concurrent / "compose.yml").write_text(json.dumps({"x-gravitee": {"kind": "playground", "name": "Concurrent"},
            "services": {"concurrent-app": {"image": "busybox:1.37.0", "restart": "unless-stopped",
                "command": ["sleep", "86400"], "depends_on": {"redis": {"condition": "service_healthy"}},
                "networks": ["internal"]}}}))
        # Match a developer's installation: a folder catalog linked into the Lab, with items linked into worktrees.
        shutil.move(str(self.root / ".lab/catalogs/local"), self.linked / ".worktrees")
        concurrent_local = self.root / ".lab/catalogs/local/playgrounds/concurrent"
        concurrent_local.parent.mkdir(parents=True)
        shutil.move(str(self.linked / ".worktrees/playgrounds/concurrent"), concurrent_local)
        (self.linked / "playgrounds").mkdir()
        for letter in ("a", "b"):
            (self.linked / "playgrounds" / letter).symlink_to(self.linked / ".worktrees/playgrounds" / letter,
                                                            target_is_directory=True)
        (self.root / ".lab/catalogs/linked").symlink_to(self.linked, target_is_directory=True)
        (self.linked / "catalog.yml").write_text("id: linked\nname: Linked independence test\n")
        (self.root / ".lab/catalogs.json").write_text(json.dumps({"schema": 1, "catalogs": [
            {"id": "linked", "kind": "folder", "path": str(self.linked)}]}))

    def make_playground(self, letter):
        folder = self.root / ".lab/catalogs/local/playgrounds" / letter
        (folder / "seed/apim/apis").mkdir(parents=True)
        (folder / ".state").mkdir()
        (folder / "backend").mkdir()
        for version in ("seed", "edited", "edited-again"):
            (folder / "backend" / version).mkdir()
            (folder / "backend" / version / "index.html").write_text(letter + ":" + version)
        definition = json.loads((SOURCE / "lab-ui/tests/data/local/playgrounds/sandbox/seed/apim/apis/sandbox.json").read_text())
        definition["api"].update(name="Independent " + letter.upper(), description="Seed definition " + letter,
                                 crossId="independent-" + letter)
        definition["api"]["listeners"][0]["paths"][0]["path"] = "/state-" + letter + "/"
        definition["api"]["endpointGroups"][0]["sharedConfiguration"] = {}
        definition["api"]["endpointGroups"][0]["endpoints"][0]["configuration"]["target"] = "${STATE_BACKEND_URL}"
        definition["api"]["visibility"] = "PUBLIC"
        definition["plans"][0].update(name="API key", security={"type": "API_KEY"}, validation="MANUAL")
        (folder / "seed/apim/apis/api.json").write_text(json.dumps(definition))
        if letter == "a":
            portal = folder / "seed/apim/portal"
            for name, text in {
                "theme.yaml": "hrid: state-a-theme\nname: State A theme\ndefinition: {}\n",
                "portal.yaml": "hrid: state-a-portal\nname: State A portal\nactiveThemeHrid: state-a-theme\n"
                               "structure:\n  topNavbar:\n    - path: State A\n",
                "pages/home.yaml": "hrid: state-a-home\nname: State A home\narea: HOMEPAGE\nfile: ../content/home.md\n",
                "pages/contact.yaml": "hrid: state-a-contact\nname: Contact\nlocation: State A\nfile: ../content/contact.md\n",
                "listings/apis.yaml": "hrid: state-a-apis\napis:\n  - apiHrid: independent-a\n    location: State A\n",
                "content/home.md": "# State A home\n", "content/contact.md": "# Contact\n",
                "api-overview.md": "# Saved API overview\n", "api-details.md": "# Saved API details\n",
            }.items():
                (portal / name).parent.mkdir(parents=True, exist_ok=True)
                (portal / name).write_text(text)
        services = {
            letter + "-backend": {
                "image": "busybox:1.37.0", "restart": "unless-stopped", "command": ["httpd", "-f", "-p", "8080", "-h", "/www"],
                "volumes": ["./backend:/www:ro", "./.state/app:/data"], "networks": ["internal"],
                "labels": {"io.gravitee.name": "Backend " + letter,
                           "io.gravitee.description": "Serves the independent API's responses"},
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
                                "APIM_PASSWORD": "${LINKED_" + letter.upper() + "_TEST_PASSWORD:-admin}",
                                "STATE_BACKEND_URL": "http://" + letter + "-backend:8080/seed/"},
            },
        }
        content = {"x-gravitee": {"kind": "playground", "name": "State " + letter.upper(), "category": "Other", "consoles": ["apim-management-api"],
                                 "description": "API independence test " + letter, "tags": ["API"],
                                 "duration": "1 min", "first_start": "1 min"}, "services": services}
        (folder / "compose.yml").write_text(json.dumps(content, indent=2))

    def request(self, url, method="GET", body=None, auth=False, extra_headers=None):
        headers = {"Content-Type": "application/json"}
        if url.startswith(getattr(self, "lab", "http://invalid")):
            headers["X-Gravitee-Lab-UI"] = "1"
        headers.update(extra_headers or {})
        if auth:
            credentials = b"user:password" if auth == "developer" else b"admin:admin"
            headers["Authorization"] = "Basic " + base64.b64encode(credentials).decode()
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
        address = self.command([*self.runtime_command(self.current), "port", service, str(port)]).splitlines()[0]
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
        return next(row for row in rows if row["name"] == "Independent " + letter.upper())

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

    def consumer(self, path, method="GET", body=None, developer=False):
        base = "/portal/environments/DEFAULT" if developer else "/management/organizations/DEFAULT/environments/DEFAULT"
        return self.json(self.endpoint("apim-management-api", 8083) + base + path, method, body,
                         auth="developer" if developer else True)

    def consumer_app(self, name):
        return self.consumer("/applications", "POST", {"name": name, "description": "Independent consumer settings",
                             "settings": {"app": {"type": "SIMPLE"}}}, developer=True)

    def consumer_sub(self, letter, app, approve=True):
        api = self.api(letter)["id"]
        plan = self.management("/apis/" + api + "/plans")["data"][0]["id"]
        sub = self.consumer("/subscriptions", "POST", {"application": app["id"], "plan": plan,
                            "request": "Consumer requests access"}, developer=True)
        row = {"api": api, "id": sub["id"]}
        if approve:
            self.sub_action(row, "_accept")
        return row

    def sub_path(self, row):
        return "/apis/" + row["api"] + "/subscriptions/" + row["id"]

    def sub_action(self, row, action, body=None):
        return self.management(self.sub_path(row) + "/" + action, "POST", body or {})

    def consumer_keys(self, row):
        result, page = [], 1
        while True:
            rows = self.management(self.sub_path(row) + "/api-keys?perPage=100&page=" + str(page))["data"]
            result.extend(rows)
            if len(rows) < 100:
                return result
            page += 1

    def assert_key_access(self, letter, value, allowed):
        def correct():
            status, text = self.request(self.endpoint("apim-gateway", 8082) + "/state-" + letter + "/",
                                        extra_headers={"X-Gravitee-Api-Key": value})
            return (status == 200 and text == letter + ":seed") if allowed else status in (401, 403)
        self.until(correct, "restored consumer access" if allowed else "revoked consumer access", timeout=120)

    def metadata(self, letter):
        return next(json.loads(file.read_text()) for file in self.root.glob(".lab/runtimes/*/runtime.json")
                    if json.loads(file.read_text())["id"] == self.instances[letter])

    def runtime_command(self, letter):
        value = self.metadata(letter)
        folder = next(file.parent for file in self.root.glob(".lab/runtimes/*/runtime.json")
                      if json.loads(file.read_text())["id"] == self.instances[letter])
        return [*compose.COMPOSE, "--project-directory", str(folder), "-f", str(folder / "compose.json"), "-p", value["project"]]

    def cleanup_lab(self):
        if hasattr(self, "process"):
            self.process.terminate()
            self.process.wait(timeout=30)
            self.output.close()
        for file in self.root.glob(".lab/runtimes/*/runtime.json"):
            value = json.loads(file.read_text())
            cmd = [*compose.COMPOSE, "-f", str(file.parent / "compose.json"), "-p", value["project"]]
            artifacts = os.environ.get("INDEPENDENT_E2E_ARTIFACTS")
            if artifacts:
                destination = Path(artifacts)
                destination.mkdir(parents=True, exist_ok=True)
                logs = self.command([*cmd, "logs", "--no-color", "--tail", "100"])
                (destination / (value["id"].rsplit("/", 1)[-1] + ".log")).write_text(logs)
                shutil.copy2(self.log, destination / "lab.log")
            self.command([*cmd, "down", "-v", "--remove-orphans"])
        if (self.root / "docker-compose.yml").exists():
            self.command([*compose.COMPOSE, "-p", self.name, "down", "-v", "--remove-orphans"])
        # Containers and volume copies can leave root-owned bind folders on Linux.
        # Restore only these disposable trees, without following their catalog links.
        self.command(["docker", "run", "--rm", "--network", "none", "--read-only",
                      "--security-opt", "no-new-privileges",
                      "--mount", f"type=bind,source={self.root},target=/lab",
                      "--mount", f"type=bind,source={self.linked},target=/catalog",
                      "--entrypoint", "chown", storage.FILES_IMAGE,
                      "-Rh", f"{os.getuid()}:{os.getgid()}", "/lab", "/catalog"])
        subprocess.run(["docker", "image", "rm", self.seed_image], cwd=self.root, env=self.env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)

    def test_private_data_consumers_links_and_shared_platform_survive_apply_and_update(self):
        self.instances = {}
        for letter, name in (("a", "Demo"), ("b", "Sandbox")):
            created = self.json(self.lab + "/api/playgrounds/@linked/a/runtimes", "POST", {"name": name})
            self.instances[letter] = created["id"]
        self.assertNotEqual(self.instances["a"], self.instances["b"])
        unused = self.json(self.lab + "/api/playgrounds/@linked/a/runtimes", "POST", {"name": "Unused"})["id"]
        unused_path = f"/api/runtimes/{unused}/cleanup"
        preview = self.json(self.lab + unused_path, "POST", {"operation": "delete", "check": True})
        self.assertEqual(preview["removes"]["volumes"], [])
        self.job(unused_path, {"operation": "delete", "fingerprint": preview["fingerprint"]})
        self.assertFalse(any(p["id"] == unused for p in self.json(self.lab + "/api/state")["playgrounds"]))
        self.json(self.lab + "/api/playgrounds/@linked/a/runtimes", "POST", {"name": "demo"}, expected=(400,))
        self.concurrent = self.json(self.lab + "/api/playgrounds/local/concurrent/runtimes", "POST", {"name": "Concurrent"})["id"]
        source_a = self.linked / "playgrounds/a"
        original = (source_a / "compose.yml").read_bytes()
        image = source_a / "action-image"
        image.mkdir()
        (image / "Dockerfile").write_text("FROM busybox:1.37.0\nRUN printf action-v1 > /action-version\n")
        (source_a / "backend/action.txt").write_text("read-only-v1")
        settings = json.loads(original)
        settings["services"]["a-backend"].update(image=self.name + "/action-backend:local",
            build={"context": "./action-image"}, pull_policy="never", environment={"RUNTIME_ACTION_TEST": "initial"})
        settings["services"]["a-idle"] = {"image": "busybox:1.37.0", "restart": "unless-stopped",
            "command": ["sleep", "86400"], "networks": ["internal"]}
        (source_a / "compose.yml").write_text(json.dumps(settings))
        parent = next(p for p in self.json(self.lab + "/api/state")["playgrounds"] if p["id"] == "@linked/a")
        self.assertEqual(parent["runtime"]["mode"], "shared")
        self.assertFalse(parent["on"])
        self.current = "a"
        print("Start private A and create real consumer access", flush=True)
        self.job(f"/api/playgrounds/{self.instances['a']}/start", {})
        self.assertIn(self.instances["a"], json.loads((self.root / ".lab/tested.json").read_text()))
        app = self.consumer_app("Independent consumer A")
        sub = self.consumer_sub("a", app)
        key = next(row for row in self.consumer_keys(sub) if not row["revoked"])
        self.assert_key_access("a", key["key"], True)
        self.job(f"/api/playgrounds/{self.concurrent}/start", {})
        self.assert_key_access("a", key["key"], True)
        self.assertTrue(next(item for item in self.json(self.lab + "/api/state")["playgrounds"] if item["id"] == self.instances["a"])["on"])
        self.job(f"/api/playgrounds/{self.concurrent}/stop", {})
        api_id = self.api("a")["id"]
        own = self.metadata("a")
        state_file = Path(own["binds"][str((source_a / ".state").resolve())]) / "created.json"
        ownership = state_file.read_bytes()
        data = Path(own["binds"][str((source_a / ".state/app").resolve())])
        (data / "sentinel").write_text("private user data")
        self.command([*self.runtime_command("a"), "exec", "-T", "redis", "redis-cli", "SET", "private-sentinel", "only-a"])
        self.job(f"/api/playgrounds/{self.instances['a']}/stop", {})
        self.current = "b"
        print("Start private B; prove A and shared Redis are separate", flush=True)
        self.job(f"/api/playgrounds/{self.instances['b']}/start", {})
        self.assertFalse(any(row["id"] == api_id for row in self.management("/apis?perPage=100")["data"]))
        self.assertEqual(self.command([*self.runtime_command("b"), "exec", "-T", "redis", "redis-cli", "GET", "private-sentinel"]), "")
        self.assertFalse(set(own["ports"].values()) & set(self.metadata("b")["ports"].values()))
        self.assert_key_access("a", key["key"], False)
        other_api = self.api("a")["id"]
        self.assertNotEqual(other_api, api_id)
        self.current = "a"
        print("Resume A with the same API, subscription, key and user data", flush=True)
        self.job(f"/api/playgrounds/{self.instances['a']}/start", {})
        self.assertEqual(self.api("a")["id"], api_id)
        self.assertEqual(self.consumer("/applications/" + app["id"])["id"], app["id"])
        self.assertEqual(self.management(self.sub_path(sub))["status"], "ACCEPTED")
        self.assertIn(key["key"], [row["key"] for row in self.consumer_keys(sub)])
        self.assert_key_access("a", key["key"], True)
        self.assertEqual(state_file.read_bytes(), ownership, "Resume reran its seed")
        self.assertEqual((data / "sentinel").read_text(), "private user data")
        self.assertEqual(self.command([*self.runtime_command("a"), "exec", "-T", "redis", "redis-cli", "GET", "private-sentinel"]), "only-a")
        print("Apply refreshes saved settings and read-only inputs without seeding or starting stopped services", flush=True)
        self.command([*self.runtime_command("a"), "stop", "a-idle"])
        def ids():
            return dict(line.split() for line in self.command([*self.runtime_command("a"), "ps", "--all", "--format", "{{.Service}} {{.ID}}"]).splitlines())
        before = ids()
        settings["services"]["a-backend"]["environment"]["RUNTIME_ACTION_TEST"] = "applied"
        (source_a / "compose.yml").write_text(json.dumps(settings))
        (source_a / "backend/action.txt").write_text("read-only-v2")
        self.job(f"/api/runtimes/{self.instances['a']}/apply", {})
        after = ids()
        self.assertNotEqual(after.pop("a-backend"), before.pop("a-backend"))
        self.assertEqual(after, before, "Apply recreated an unchanged or stopped container")
        self.assertEqual(self.command([*self.runtime_command("a"), "exec", "-T", "a-backend", "cat", "/www/action.txt"]), "read-only-v2")
        self.assertEqual(self.command([*self.runtime_command("a"), "exec", "-T", "a-backend", "printenv", "RUNTIME_ACTION_TEST"]), "applied")
        before = ids()
        self.job(f"/api/runtimes/{self.instances['a']}/apply", {})
        self.assertEqual(ids(), before, "No-op Apply recreated containers")
        self.job(f"/api/runtimes/{self.instances['a']}/restart", {})
        self.assertEqual(self.command([*self.runtime_command("a"), "ps", "--all", "--format", "{{.State}}", "a-idle"]), "exited")
        print("Update rebuilds the configured local image without adopting unapplied settings", flush=True)
        settings["services"]["a-backend"]["environment"]["RUNTIME_ACTION_TEST"] = "not-applied"
        saved_source = json.dumps(settings).encode()
        (source_a / "compose.yml").write_bytes(saved_source)
        (image / "Dockerfile").write_text("FROM busybox:1.37.0\nRUN printf action-v2 > /action-version\n")
        self.job(f"/api/runtimes/{self.instances['a']}/update", {})
        self.assertEqual(self.command([*self.runtime_command("a"), "exec", "-T", "a-backend", "cat", "/action-version"]), "action-v2")
        self.assertEqual(self.command([*self.runtime_command("a"), "exec", "-T", "a-backend", "printenv", "RUNTIME_ACTION_TEST"]), "applied")
        self.assertEqual(self.command([*self.runtime_command("a"), "ps", "--all", "--format", "{{.State}}", "a-idle"]), "exited")
        self.assertEqual(state_file.read_bytes(), ownership, "Apply or Update replayed the seed")
        self.assert_key_access("a", key["key"], True)
        self.job(f"/api/playgrounds/{self.instances['a']}/stop", {})
        self.job(f"/api/runtimes/{self.instances['a']}/apply", {})
        self.assertFalse(self.metadata("a")["active"])
        self.assertNotIn("running", self.command([*self.runtime_command("a"), "ps", "--all", "--format", "{{.State}} "]))
        self.job(f"/api/playgrounds/{self.instances['a']}/start", {})
        self.assertEqual(self.command([*self.runtime_command("a"), "exec", "-T", "a-backend", "printenv", "RUNTIME_ACTION_TEST"]), "not-applied")
        self.assertEqual(self.metadata("a")["ports"], own["ports"])
        self.assertEqual((data / "sentinel").read_text(), "private user data")
        self.assertEqual(state_file.read_bytes(), ownership, "Start after Apply replayed the seed")
        self.assert_key_access("a", key["key"], True)
        self.job(f"/api/playgrounds/{self.instances['a']}/stop", {})
        self.cleanup_workflow(api_id, app, sub, key, own, state_file, ownership, data, source_a)
        self.assertEqual(self.command([*compose.COMPOSE, "exec", "-T", "redis", "redis-cli", "GET", "shared-sentinel"]), "keep-original")
        self.assertTrue((self.root / ".lab/catalogs/linked").is_symlink())
        self.assertTrue(source_a.is_symlink())
        self.assertEqual((source_a / "compose.yml").read_bytes(), saved_source)
        self.assertFalse((source_a / ".state/created.json").exists())
        self.current = "b"
        self.assertEqual(self.api("a")["id"], other_api)
        self.job(f"/api/playgrounds/{self.instances['b']}/stop", {})
        print("Named runtime end-to-end checks passed", flush=True)


    def cleanup_workflow(self, api_id, app, sub, key, own, state_file, ownership, data, source):
        print("Remove A's containers while C stays running; resume the same data", flush=True)
        self.job(f"/api/runtimes/{self.concurrent}/start", {})
        path = f"/api/runtimes/{self.instances['a']}/cleanup"
        listing = self.json(self.lab + "/api/runtimes")
        a = next(row for row in listing["runtimes"] if row["id"] == self.instances["a"])
        b = next(row for row in listing["runtimes"] if row["id"] == self.instances["b"])
        self.assertFalse(a["running"])
        self.assertTrue(a["cleanup"]["containers"]["enabled"])
        self.assertGreater(a["local_bytes"], 0)
        volumes = [row["name"] for row in a["volumes"]]
        other_volumes = [row["name"] for row in b["volumes"]]
        plan = self.json(self.lab + path, "POST", {"operation": "containers", "check": True})
        self.assertEqual(plan["removes"]["volumes"], [])
        external = self.command(["docker", "create", "--network", plan["removes"]["networks"][0], "busybox:1.37.0", "true"])
        try:
            self.json(self.lab + path, "POST", {"operation": "containers", "check": True}, expected=(400,))
        finally:
            self.command(["docker", "container", "rm", external])
        plan = self.json(self.lab + path, "POST", {"operation": "containers", "check": True})
        self.job(path, {"operation": "containers", "fingerprint": plan["fingerprint"]})
        self.command(["docker", "volume", "inspect", *volumes])
        self.assertEqual(state_file.read_bytes(), ownership)
        self.job(f"/api/runtimes/{self.instances['a']}/start", {})
        self.assertEqual(self.api("a")["id"], api_id)
        self.assertEqual(self.consumer("/applications/" + app["id"])["id"], app["id"])
        self.assertIn(key["key"], [row["key"] for row in self.consumer_keys(sub)])
        self.assert_key_access("a", key["key"], True)
        self.assertEqual((data / "sentinel").read_text(), "private user data")
        self.assertEqual(state_file.read_bytes(), ownership, "Recreating containers replayed the seed")
        self.assertTrue(next(p for p in self.json(self.lab + "/api/state")["playgrounds"] if p["id"] == self.instances["a"])["up"])
        self.json(self.lab + path, "POST", {"operation": "data", "check": True}, expected=(400,))
        self.command([*self.runtime_command("a"), "exec", "-T", "a-backend", "sh", "-c",
                      "mkdir -p /data/root-owned; echo private > /data/root-owned/file"])
        (data / "catalog-link").symlink_to(source.resolve(), target_is_directory=True)
        self.job(f"/api/runtimes/{self.instances['a']}/stop", {})
        print("Reject a stale deletion, then remove only A's data and start it fresh", flush=True)
        plan = self.json(self.lab + path, "POST", {"operation": "data", "check": True})
        self.assertEqual(set(plan["removes"]["volumes"]), set(volumes))
        self.assertFalse(set(plan["removes"]["volumes"]) & set(other_volumes))
        (data / "sentinel").write_text("Changed after preview")
        self.json(self.lab + path, "POST", {"operation": "data", "fingerprint": plan["fingerprint"]}, expected=(400,))
        self.command(["docker", "volume", "inspect", *volumes])
        plan = self.json(self.lab + path, "POST", {"operation": "data", "check": True})
        self.job(path, {"operation": "data", "fingerprint": plan["fingerprint"]})
        self.assertFalse(state_file.parent.exists())
        removed = subprocess.run(["docker", "volume", "inspect", *volumes], cwd=self.root, env=self.env,
                                 capture_output=True, timeout=30)
        self.assertNotEqual(removed.returncode, 0)
        self.command(["docker", "volume", "inspect", *other_volumes])
        listing = self.json(self.lab + "/api/runtimes")
        self.assertFalse(next(row for row in listing["runtimes"] if row["id"] == self.instances["a"])["prepared"])
        self.assertTrue(next(row for row in listing["runtimes"] if row["id"] == self.concurrent)["running"])
        self.assertTrue(source.is_symlink())
        self.job(f"/api/runtimes/{self.instances['a']}/start", {})
        self.assertNotEqual(self.api("a")["id"], api_id)
        self.assert_key_access("a", key["key"], False)
        seeded = self.api("a")["id"]
        self.job(f"/api/playgrounds/{self.instances['a']}/reseed", {})
        self.assertEqual(self.api("a")["id"], seeded, "Re-seed did not reconcile the runtime's own API")
        self.job(f"/api/runtimes/{self.instances['a']}/stop", {})
        delete = self.json(self.lab + path, "POST", {"operation": "delete", "check": True})
        self.job(path, {"operation": "delete", "fingerprint": delete["fingerprint"]})
        self.assertFalse(any(p["id"] == self.instances["a"] for p in self.json(self.lab + "/api/state")["playgrounds"]))
        self.job(f"/api/runtimes/{self.concurrent}/stop", {})


if __name__ == "__main__":
    unittest.main()
