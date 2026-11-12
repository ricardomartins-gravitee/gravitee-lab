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
        self.linked_temp = tempfile.TemporaryDirectory(prefix="lab-linked-catalog-")
        self.addCleanup(self.linked_temp.cleanup)
        self.linked = Path(self.linked_temp.name).resolve()
        self.name = "lab-gravitee-test-" + uuid.uuid4().hex[:12]
        self.seed_image = self.name + "/seeder:local"
        self.env = {key: os.environ[key] for key in ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR",
                                                   "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG") if key in os.environ}
        self.env.update(compose.NO_REMOTE, COMPOSE_FILE="docker-compose.yml:.lab/on.yml")
        self.log = self.root / "lab.log"
        self.log.touch()
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
        # Match a developer's installation: a folder catalog linked into the Lab, with items linked into worktrees.
        shutil.move(str(self.root / ".lab/catalogs/local"), self.linked / ".worktrees")
        (self.linked / "playgrounds").mkdir()
        for letter in ("a", "b"):
            (self.linked / "playgrounds" / letter).symlink_to(self.linked / ".worktrees/playgrounds" / letter,
                                                            target_is_directory=True)
        (self.root / ".lab/catalogs/linked").symlink_to(self.linked, target_is_directory=True)
        (self.linked / "catalog.yml").write_text("id: linked\nname: Linked checkpoint test\n")
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
        definition["api"].update(name="Saved state " + letter.upper(), description="Seed definition " + letter,
                                 crossId="saved-state-" + letter)
        definition["api"]["listeners"][0]["paths"][0]["path"] = "/state-" + letter + "/"
        definition["api"]["endpointGroups"][0]["sharedConfiguration"] = {}
        definition["api"]["endpointGroups"][0]["endpoints"][0]["configuration"]["target"] = "${STATE_BACKEND_URL}"
        if self._testMethodName == "test_consumer_states_restore_only_this_playgrounds_access":
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
                "listings/apis.yaml": "hrid: state-a-apis\napis:\n  - apiHrid: saved-state-a\n    location: State A\n",
                "content/home.md": "# State A home\n", "content/contact.md": "# Contact\n",
                "api-overview.md": "# Saved API overview\n", "api-details.md": "# Saved API details\n",
            }.items():
                (portal / name).parent.mkdir(parents=True, exist_ok=True)
                (portal / name).write_text(text)
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
                                "APIM_PASSWORD": "${LINKED_" + letter.upper() + "_TEST_PASSWORD:-admin}",
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
                for tag in {image["locked_tag"] for image in json.loads(manifest.read_text()).get("images", [])}:
                    self.command(["docker", "image", "rm", tag])
            subprocess.run(["docker", "image", "rm", self.seed_image], cwd=self.root, env=self.env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)

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
        return self.json(self.lab + "/api/playgrounds/@linked/a/saved-states")["states"]

    def portal_entities(self, edit=False):
        rows = json.loads((self.linked / "playgrounds/a/.state/created.json").read_text())
        result = {}
        base = self.endpoint("apim-management-api", 8083) + "/automation/organizations/DEFAULT/environments/DEFAULT"
        for row in rows:
            collection = {"portal": "/portals", "portal-theme": "/portal-themes",
                          "portal-page": "/portals/" + row.get("portal", "") + "/documentations"}.get(row["kind"])
            if not collection:
                continue
            body = self.json(base + collection + "/" + row["hrid"], auth=True)
            if edit and row["kind"] == "portal-page":
                payload = {key: value for key, value in body.items() if key in
                           ("hrid", "name", "type", "content", "area", "location", "order", "visibility")}
                payload["content"] = "# User-edited saved portal page\n"
                self.json(base + collection, "PUT", payload, auth=True)
                body = self.json(base + collection + "/" + row["hrid"], auth=True)
            result[row["id"]] = {key: value for key, value in body.items()
                                 if key not in ("activeThemeHrid", "navigation", "organizationId", "environmentId")}
        return result

    def assert_restored(self, sid, expected):
        plan = self.json(self.lab + "/api/playgrounds/@linked/a/saved-states/" + sid + "/plan")
        accept = [row["kind"] + ":" + row["id"] for part in ("stops", "turns_off") for row in plan.get(part, [])]
        self.job("/api/playgrounds/@linked/a/saved-states/" + sid + "/restore", {"confirm": True, "accept": accept})
        self.assertTrue((self.root / ".lab/catalogs/linked").is_symlink())
        self.assertTrue((self.linked / "playgrounds/a").is_symlink())
        self.assertEqual((self.linked / "playgrounds/a/.state/created.json").read_bytes(), self.ownership)
        actual = self.management("/apis/" + expected["id"])
        for key in ("id", "name", "description", "endpointGroups", "plans", "state", "lifecycleState"):
            if key in expected:
                self.assertEqual(actual.get(key), expected[key], key)
        version = expected["description"].split(":", 1)[1]
        self.assert_serves("a", version)
        self.assertEqual(self.portal_entities(), self.expected_portals, "Owned portal edits were not restored")
        self.assertEqual(self.command([*compose.COMPOSE, "ps", "-aq", "a-seed"]), "", "Restore ran the seed again")
        if hasattr(self, "unrecorded"):
            actual = self.management("/apis/" + self.unrecorded["id"])
            self.assertEqual(actual, self.unrecorded, "An unrecorded API changed during restore")
            self.assertEqual(self.command([*compose.COMPOSE, "exec", "-T", "redis", "redis-cli", "GET", "not-this-playground"]),
                             "changed-after-save", "Shared Redis data was rewound")
            navigation = self.management("/portal-navigation-items?area=TOP_NAVBAR")["items"]
            self.assertEqual(next(item for item in navigation if item["id"] == self.unowned_navigation["id"]),
                             self.unowned_navigation, "Unowned portal navigation was changed")
            for service, started in self.core_started.items():
                ident = self.command([*compose.COMPOSE, "ps", "-q", service])
                self.assertEqual(self.command(["docker", "inspect", "--format", "{{.State.StartedAt}}", ident]), started,
                                 "Shared service was restarted: " + service)

    def test_saved_api_edits_survive_switch_stop_and_multiple_restores(self):
        print("Creating and editing playground A through Gravitee APIs", flush=True)
        self.job("/api/playgrounds/@linked/a/start", {})
        first_api = self.edit("a", "edited")
        self.expected_portals = self.portal_entities(edit=True)
        self.ownership = (self.linked / "playgrounds/a/.state/created.json").read_bytes()
        print("Saving A before seeder cleanup while switching to B", flush=True)
        self.job("/api/playgrounds/@linked/b/start", {"remove": True, "save": True, "accept": ["playground:@linked/a"]})
        first = self.states()[0]["id"]
        self.management("/apis/" + first_api["id"], expected=(404,))
        self.assert_serves("b", "seed")
        b_id = self.api("b")["id"]
        b_edited = self.edit("b", "edited-again")
        core_ids = {service: self.command([*compose.COMPOSE, "ps", "-q", service])
                    for service in ("mongodb", "elasticsearch", "redis", "apim-management-api", "apim-gateway")}
        self.core_started = {service: self.command(["docker", "inspect", "--format", "{{.State.StartedAt}}", ident])
                             for service, ident in core_ids.items()}
        definition = json.loads((SOURCE / "lab-ui/tests/data/local/playgrounds/sandbox/seed/apim/apis/sandbox.json").read_text())
        definition["api"]["name"] = "Unrecorded API created after A was saved"
        definition["api"]["listeners"][0]["paths"][0]["path"] = "/unrecorded/"
        definition["api"]["endpointGroups"][0]["sharedConfiguration"] = {}
        definition["api"]["endpointGroups"][0]["endpoints"][0]["configuration"]["target"] = "http://b-backend:8080/seed/"
        unrecorded_id = self.management("/apis/_import/definition", "POST", definition)["id"]
        self.unrecorded = self.management("/apis/" + unrecorded_id)
        nav_id = str(uuid.uuid4())
        self.management("/portal-navigation-items", "POST", {"id": nav_id, "type": "LINK", "title": "Unowned docs",
                        "area": "TOP_NAVBAR", "visibility": "PUBLIC", "url": "https://example.invalid/"})
        self.unowned_navigation = next(item for item in self.management("/portal-navigation-items?area=TOP_NAVBAR")["items"]
                                      if item["id"] == nav_id)
        self.command([*compose.COMPOSE, "exec", "-T", "redis", "redis-cli", "SET", "not-this-playground", "changed-after-save"])
        other_source = self.linked / "playgrounds/b/seed/apim/apis/api.json"
        other_source.write_bytes(other_source.read_bytes() + b"\n")
        source = self.linked / "playgrounds/a/seed/apim/apis/api.json"
        original = source.read_bytes()
        source.write_bytes(original + b"\n")
        refused = self.json(self.lab + "/api/playgrounds/@linked/a/saved-states/" + first + "/restore",
                            "POST", {"confirm": True}, expected=(400,))
        self.assertIn("source or links changed", refused["error"])
        self.assert_serves("b", "edited-again")
        source.write_bytes(original)
        print("Restoring A and checking API identity and real gateway traffic", flush=True)
        self.assert_restored(first, first_api)
        b_after = self.management("/apis/" + b_id)
        for key in ("id", "description", "endpointGroups", "state", "lifecycleState", "updatedAt"):
            self.assertEqual(b_after.get(key), b_edited.get(key), "Playground B changed: " + key)
        self.assertEqual(core_ids, {service: self.command([*compose.COMPOSE, "ps", "-q", service]) for service in core_ids})
        self.assertTrue(next(row for row in self.json(self.lab + "/api/state")["playgrounds"] if row["id"] == "@linked/a")["on"])
        second_api = self.edit("a", "edited-again")
        print("Saving a second checkpoint using Stop, then restoring both versions", flush=True)
        self.job("/api/playgrounds/@linked/a/stop", {"remove": True, "save": True})
        self.management("/apis/" + first_api["id"], expected=(404,))
        states = self.states()
        self.assertEqual(len(states), 2)
        second = next(state["id"] for state in states if state["id"] != first)
        self.assert_restored(second, second_api)
        self.assert_restored(first, first_api)
        self.json(self.lab + "/api/playgrounds/@linked/a/saved-states/" + second + "/delete", "POST", {})
        self.assertEqual([state["id"] for state in self.states()], [first])
        self.assert_serves("a", "edited")
        print("Verified real Gravitee API save/switch/restore/stop/resave and checkpoint deletion", flush=True)


    def consumer(self, path, method="GET", body=None, developer=False):
        base = "/portal/environments/DEFAULT" if developer else "/management/organizations/DEFAULT/environments/DEFAULT"
        return self.json(self.endpoint("apim-management-api", 8083) + base + path, method, body,
                         auth="developer" if developer else True)

    def consumer_app(self, name):
        return self.consumer("/applications", "POST", {"name": name, "description": "Saved consumer settings",
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
        actual = row["id"]
        path = self.linked / "playgrounds/a/.state/saved-state-subscriptions.json"
        aliases = json.loads(path.read_text()) if path.exists() else []
        seen = set()
        while actual not in seen:
            seen.add(actual)
            found = next((entry for entry in aliases if entry["api"] == row["api"] and entry["id"] == actual), None)
            if not found:
                break
            actual = found["actual"]
        return "/apis/" + row["api"] + "/subscriptions/" + actual

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

    def restore_consumers(self, sid):
        plan = self.json(self.lab + "/api/playgrounds/@linked/a/saved-states/" + sid + "/plan")
        accept = [row["kind"] + ":" + row["id"] for part in ("stops", "turns_off") for row in plan.get(part, [])]
        self.job("/api/playgrounds/@linked/a/saved-states/" + sid + "/restore", {"confirm": True, "accept": accept})
        self.assertTrue((self.root / ".lab/catalogs/linked").is_symlink())
        self.assertTrue((self.linked / "playgrounds/a").is_symlink())
        self.assertEqual((self.linked / "playgrounds/a/.state/created.json").read_bytes(), self.consumer_ownership[sid])
        rows = self.management("/apis/" + self.api("a")["id"] + "/subscriptions?statuses=PENDING,ACCEPTED,PAUSED,CLOSED,REJECTED")["data"]
        self.assertTrue(all(row["origin"] == "MANAGEMENT" for row in rows), "Restore locked subscriptions in the Console")

    def save_consumers(self):
        before = {state["id"] for state in self.states()}
        ownership = (self.linked / "playgrounds/a/.state/created.json").read_bytes()
        self.job("/api/playgrounds/@linked/a/stop", {"remove": True, "save": True})
        state = next(state for state in self.states() if state["id"] not in before)
        if not hasattr(self, "consumer_ownership"):
            self.consumer_ownership = {}
        self.consumer_ownership[state["id"]] = ownership
        return state["id"]

    def assert_saved_keys(self, row, saved):
        actual = {value["key"]: value for value in self.consumer_keys(row)}
        for value in saved:
            self.assertIn(value["key"], actual, "A saved credential was lost")
            for field in ("revoked", "expireAt"):
                self.assertTrue(actual[value["key"]].get(field) == value.get(field), "Credential " + field + " changed")
        wanted = {value["key"] for value in saved}
        self.assertTrue(all(value.get("revoked") or value.get("expired") for key, value in actual.items() if key not in wanted),
                        "A credential issued after the checkpoint is still active")

    def test_consumer_states_restore_only_this_playgrounds_access(self):
        print("Testing the actual Developer Portal application and approval workflow", flush=True)
        self.job("/api/playgrounds/@linked/a/start", {})
        self.ownership = (self.linked / "playgrounds/a/.state/created.json").read_bytes()
        app = self.consumer_app("Consumer App A")
        self.consumer("/applications/" + app["id"] + "/metadata", "POST",
                      {"key": "team", "name": "Team", "value": "Saved team", "format": "STRING"})
        saved_metadata = self.consumer("/applications/" + app["id"] + "/metadata")
        sub = self.consumer_sub("a", app, approve=False)
        self.assertEqual(self.management(self.sub_path(sub))["status"], "PENDING")
        pending = self.save_consumers()
        self.restore_consumers(pending)
        self.assertEqual(self.management(self.sub_path(sub))["status"], "PENDING")
        self.assertEqual(self.management(self.sub_path(sub))["origin"], "MANAGEMENT")
        self.assertEqual(self.management(self.sub_path(sub))["consumerMessage"], "Consumer requests access")
        self.assertTrue(all(value["revoked"] or value.get("expired") for value in self.consumer_keys(sub)), "Pending access received an active key")
        self.sub_action(sub, "_accept")
        active = next(value for value in self.consumer_keys(sub) if not value["revoked"])
        self.management(self.sub_path(sub) + "/api-keys/" + active["id"], "PUT", {"expireAt": "2036-01-01T00:00:00Z"})
        self.assert_key_access("a", active["key"], True)
        shared = self.consumer_app("Shared consumer")
        shared_a = self.consumer_sub("a", shared)
        shared_key = next(value for value in self.consumer_keys(shared_a) if not value["revoked"])
        self.job("/api/playgrounds/@linked/b/start", {"accept": ["playground:@linked/a"]})
        other_app = self.consumer_app("Consumer App B")
        other_sub = self.consumer_sub("b", other_app)
        shared_b = self.consumer_sub("b", shared)
        self.job("/api/playgrounds/@linked/a/start", {"accept": ["playground:@linked/b"]})
        saved_keys = self.consumer_keys(sub)
        accepted = self.save_consumers()
        self.job("/api/playgrounds/@linked/b/start", {})
        # These edits are newer than A's checkpoint and must remain exactly as they are.
        for application in (other_app, shared):
            self.consumer("/applications/" + application["id"], "PUT",
                          {"name": application["name"], "description": "Keep B's newer settings",
                           "settings": application["settings"]})
        other_key = next(value for value in self.consumer_keys(other_sub) if not value["revoked"])
        self.sub_action(other_sub, "api-keys/" + other_key["id"] + "/_revoke")
        other_before = {application["id"]: self.consumer("/applications/" + application["id"])
                        for application in (other_app, shared)}
        subscriptions_before = {row["id"]: (self.management(self.sub_path(row)), self.consumer_keys(row))
                                for row in (other_sub, shared_b)}
        original_owner = self.consumer("/applications/" + app["id"])["owner"]["id"]
        admin = self.json(self.endpoint("apim-management-api", 8083) + "/management/organizations/DEFAULT/user", auth=True)["id"]
        self.consumer("/applications/" + app["id"] + "/members", "POST", {"id": admin, "role": "OWNER"})
        self.consumer("/applications/" + app["id"] + "/members/transfer_ownership", "POST", {"id": admin, "poRole": "OWNER"})
        self.consumer("/applications/" + app["id"] + "/metadata/team", "PUT",
                      {"key": "team", "name": "Team", "value": "Later team", "format": "STRING"})
        self.consumer("/applications/" + app["id"], "DELETE")
        core = {service: self.command([*compose.COMPOSE, "ps", "-q", service])
                for service in ("mongodb", "elasticsearch", "redis", "apim-management-api", "apim-gateway")}
        print("Restoring deleted API, archived app, ownership, subscriptions and original credentials", flush=True)
        self.restore_consumers(accepted)
        self.assertEqual(self.consumer("/applications/" + app["id"])["owner"]["id"], original_owner)
        self.assertEqual(self.consumer("/applications/" + app["id"])["description"], app["description"])
        self.assertEqual(self.consumer("/applications/" + app["id"] + "/metadata"), saved_metadata)
        self.assert_saved_keys(sub, saved_keys)
        self.assert_key_access("a", active["key"], True)
        self.assert_key_access("a", shared_key["key"], True)
        for application in (other_app, shared):
            self.assertEqual(self.consumer("/applications/" + application["id"]), other_before[application["id"]])
        for row in (other_sub, shared_b):
            actual = (self.management(self.sub_path(row)), self.consumer_keys(row))
            self.assertTrue(actual == subscriptions_before[row["id"]], "Playground B's subscription or keys changed")
        self.assertEqual(core, {service: self.command([*compose.COMPOSE, "ps", "-q", service]) for service in core})
        # A restore must revoke credentials issued since the checkpoint, including normal renewal.
        self.sub_action(sub, "api-keys/_renew")
        newer = next(value for value in self.consumer_keys(sub) if value["key"] != active["key"] and not value["revoked"])
        self.assert_key_access("a", newer["key"], True)
        self.restore_consumers(accepted)
        self.assert_saved_keys(sub, saved_keys)
        self.assert_key_access("a", active["key"], True)
        self.assert_key_access("a", newer["key"], False)
        current = next(value for value in self.consumer_keys(sub) if value["key"] == active["key"])
        self.sub_action(sub, "api-keys/" + current["id"] + "/_revoke")
        revoked_keys = self.consumer_keys(sub)
        revoked = self.save_consumers()
        self.restore_consumers(revoked)
        self.assert_saved_keys(sub, revoked_keys)
        self.assert_key_access("a", active["key"], False)
        self.restore_consumers(accepted)
        self.assert_key_access("a", active["key"], True)
        current = next(value for value in self.consumer_keys(sub) if value["key"] == active["key"])
        self.management(self.sub_path(sub) + "/api-keys/" + current["id"], "PUT", {"expireAt": "2000-01-01T00:00:00Z"})
        expired_keys = self.consumer_keys(sub)
        expired = self.save_consumers()
        self.restore_consumers(expired)
        self.assert_saved_keys(sub, expired_keys)
        self.assert_key_access("a", active["key"], False)
        self.restore_consumers(accepted)
        self.assert_key_access("a", active["key"], True)
        # Other subscription states must survive real cleanup too, not just an in-place update.
        for status, action in (("PAUSED", "_pause"), ("CLOSED", "_close"), ("REJECTED", "_reject")):
            if status == "REJECTED":
                self.consumer("/subscriptions/" + self.sub_path(sub).rsplit("/", 1)[-1], "PUT", {"configuration": None, "metadata": {}}, developer=True)
            self.sub_action(sub, action, {"reason": "Saved subscription state"})
            checkpoint = self.save_consumers()
            self.restore_consumers(checkpoint)
            self.assertEqual(self.management(self.sub_path(sub))["status"], status)
            self.assert_key_access("a", active["key"], False)
            self.restore_consumers(accepted)
            self.assert_key_access("a", active["key"], True)
        self.restore_consumers(pending)
        self.assertEqual(self.management(self.sub_path(sub))["status"], "PENDING")
        self.assert_key_access("a", active["key"], False)
        print("Verified pending/accepted/paused/closed/rejected subscriptions, revocation, renewal, expiry and shared-app isolation", flush=True)


if __name__ == "__main__":
    unittest.main()
