"""Real Gamma catalog/studio and AM credential cloning, alongside a foreign playground.

Run explicitly with Docker. An optional RUNTIME_CLONE_TEST_LICENSE enables the same
Enterprise features as a licensed installation; credentials and snapshots stay private.
"""

import base64
import json
import os
from pathlib import Path
import subprocess
import unittest
import urllib.parse
import urllib.request

from lab_ui.docker import compose
from tests.independent_docker import SOURCE
from tests import runtime_clone_docker


class GammaAmCloneDocker(runtime_clone_docker.RuntimeCloneDocker):
    test_shared_and_private_clones_copy_complete_storage_and_preserve_the_source = None
    GAMMA = True

    def make_lab(self):
        super().make_lab()
        core = json.loads((self.root / "docker-compose.yml").read_text())
        original = json.loads(subprocess.check_output([*compose.COMPOSE, "-f", str(SOURCE / "docker-compose.yml"),
                              "config", "--format", "json"], cwd=SOURCE, env=compose.scrubbed_environment()))
        for name in ("am-management-api", "am-gateway"):
            service = original["services"][name]
            service.pop("container_name", None)
            port = 8093 if name == "am-management-api" else 8092
            service["ports"] = [{"target": port, "published": "0", "host_ip": "127.0.0.1", "protocol": "tcp"}]
            for mount in service.get("volumes", []):
                if mount["type"] == "bind":
                    mount["source"] = str(self.root / os.path.relpath(mount["source"], SOURCE))
            service.setdefault("labels", {})["io.gravitee.console.test.url"] = "http://localhost:" + str(port)
            service["environment"]["GIO_MIN_MEM"] = "128m"
            service["environment"]["GIO_MAX_MEM"] = "384m"
            core["services"][name] = service
        (self.root / "plugins/am").mkdir()
        for service in core["services"].values():
            service.setdefault("environment", {})["gravitee_gamma_enabled"] = str(self.GAMMA).lower()
            service["environment"]["gravitee_license_key"] = os.getenv("RUNTIME_CLONE_TEST_LICENSE", "")
        (self.root / "docker-compose.yml").write_text(json.dumps(core))

    def make_playground(self, letter):
        super().make_playground(letter)
        folder = self.root / ".lab/catalogs/local/playgrounds" / letter
        file = folder / "seed/apim/apis/api.json"
        definition = json.loads(file.read_text())
        definition["pages"] = [{"name": "OpenAPI", "type": "SWAGGER", "published": True, "content": json.dumps({
            "openapi": "3.0.3", "info": {"title": "State " + letter, "version": "1"},
            "paths": {"/": {"get": {"operationId": "sample", "responses": {"200": {"description": "Sample"}}}}}})}]
        file.write_text(json.dumps(definition))
        files = {
            "aim/api-tools/api.yaml": {"api": "Independent " + letter.upper()},
            "aim/studios/tools.yaml": {"name": "Tools " + letter, "contextPath": "/tools-" + letter + "/",
                "supportedProtocolVersions": ["2025-06-18"], "tools": [{"api": "Independent " + letter.upper(), "tool": "sample"}],
                "plan": {"name": "Studio key", "security": {"type": "API_KEY", "configuration": {
                    "enableCustomApiKeyHeader": False, "propagateApiKey": False}}},
                "subscriptions": [{"application": "Studio consumer " + letter}]},
            "apim/applications/studio.yaml": {"name": "Studio consumer " + letter},
            "apim/applications/upstream.yaml": {"name": "Upstream consumer " + letter},
            "apim/settings.yaml": {"plan": {"security": {"customApiKey": {"enabled": True}}}},
            "apim/subscriptions/upstream.yaml": {"subscriptions": [{"application": "Upstream consumer " + letter,
                "api": "Independent " + letter.upper(), "plan": "API key", "apiKey": "upstream-clone-key-000000000000-" + letter}]},
            "am/domain.yaml": {"name": "clone-domain-" + letter, "users": [{"username": "seed-user", "password": "Seed-Password-123!",
                "email": "seed@example.test", "firstName": "Seed", "lastName": "User"}]},
            "am/applications/client.yaml": {"name": "Clone client", "type": "BROWSER", "clientId": "clone-client",
                "clientSecret": "clone-client-secret", "redirectUris": ["https://example.test/callback"],
                "oauth": {"grantTypes": ["password", "client_credentials"], "responseTypes": ["code"]}},
        }
        for name, data in files.items():
            if name.startswith("aim/") and not self.GAMMA:
                continue
            target = folder / "seed" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            # JSON is also a YAML document; avoid adding a host dependency to the Docker fixture.
            target.write_text(json.dumps(data))
        compose_file = folder / "compose.yml"
        value = json.loads(compose_file.read_text())
        value["services"][letter + "-seed"]["environment"].update(
            AM_MANAGEMENT_URL="http://am-management-api:8093", AM_GATEWAY_URL="http://am-gateway:8092",
            AM_USERNAME="admin", AM_PASSWORD="${LINKED_TEST_AM_PASSWORD:-adminadmin}",
            GATEWAY_INTERNAL_URL="http://apim-gateway:8082")
        value["services"][letter + "-seed"]["depends_on"]["am-management-api"] = {"condition": "service_healthy"}
        value["services"][letter + "-seed"]["depends_on"]["am-gateway"] = {"condition": "service_healthy"}
        compose_file.write_text(json.dumps(value))

    def gamma(self, path, method="GET", body=None):
        base = self.endpoint("apim-management-api", 8083) + "/gamma/organizations/DEFAULT/environments/DEFAULT/modules/aim"
        return self.json(base + path, method, body, auth=True)

    def am(self, path="", method="GET", body=None):
        base = self.endpoint("am-management-api", 8093)
        login = self.request(base + "/management/auth/token", "POST", {}, extra_headers={
            "Authorization": "Basic " + base64.b64encode(b"admin:adminadmin").decode()})
        self.assertEqual(login[0], 200, "AM management login")
        token = json.loads(login[1])["access_token"]
        status, text = self.request(base + "/management/organizations/DEFAULT/environments/DEFAULT" + path,
                                    method, body, extra_headers={"Authorization": "Bearer " + token})
        self.assertIn(status, (200, 201, 204), "AM scoped request: HTTP " + str(status))
        return json.loads(text) if text else None

    def records(self, current):
        file = self.linked / ".worktrees/playgrounds/a/.state/created.json"
        if current != "shared":
            value = self.metadata(current)
            source = str((self.linked / ".worktrees/playgrounds/a/.state").resolve())
            file = Path(value["binds"][source]) / "created.json"
        return json.loads(file.read_text())

    def password_login(self, domain, username, password):
        self.client_login(domain, "clone-client", "clone-client-secret", {"grant_type": "password", "username": username, "password": password})

    def client_login(self, domain, client, secret, form=None):
        url = self.endpoint("am-gateway", 8092) + "/" + domain + "/oauth/token"
        request = urllib.request.Request(url, method="POST", data=urllib.parse.urlencode(form or {"grant_type": "client_credentials"}).encode(),
            headers={"Authorization": "Basic " + base64.b64encode((client + ":" + secret).encode()).decode(),
                     "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(request, timeout=20) as response:
            self.assertTrue(json.load(response).get("access_token"))

    def assert_extra_data(self, ids, manual=True):
        if self.GAMMA:
            for collection in ("sources", "mcp-tools"):
                found = self.gamma("/catalog/" + collection + "?perPage=200")["data"]
                self.assertEqual({row["id"] for row in found}, set(ids[collection]))
            studio = self.gamma("/mcp-proxies/" + ids["studio"])
            self.assertEqual(studio.get("description"), ids["description"])
            self.assertEqual(self.management("/apis/" + ids["studio"])["description"], "Current studio description")
        domains = self.am("/domains")
        domains = domains.get("data", domains) if isinstance(domains, dict) else domains
        self.assertEqual({row["id"] for row in domains}, set(ids["domains"]))
        self.password_login("clone-domain-b", "seed-user", "Seed-Password-123!")
        self.until(lambda: self.password_login("clone-domain-a", "manual-user" if manual else "seed-user",
                   "Manual-Password-456!" if manual else "Seed-Password-123!") or True, "cloned AM user sign-in", timeout=120)
        self.until(lambda: self.client_login("clone-domain-a", "manual-clone-client", "Manual-Client-Secret-789!") or True,
                   "cloned client created after the seed", timeout=120)
        self.assert_key_access("a", "upstream-clone-key-000000000000-a", True)
        if self.GAMMA:
            self.until(lambda: self.studio_access(ids["key"]), "cloned studio tool call with the original keys", timeout=120)
        records = self.records(self.current)
        if self.GAMMA:
            self.assertTrue(any(row["product"] == "aim-proxies" and row["kind"] == "studio" and row["id"] == ids["studio"] for row in records))
        self.assertTrue(any(row["product"] == "am" and row["kind"] == "domain" and row["id"] == ids["domain"] for row in records))

    def studio_access(self, key):
        url = self.endpoint("apim-gateway", 8082) + "/tools-a/"
        headers = {"X-Gravitee-Api-Key": key, "Accept": "application/json, text/event-stream",
                   "Content-Type": "application/json", "MCP-Protocol-Version": "2025-06-18"}
        def rpc(method, params=None, notification=False):
            body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
            if not notification:
                body["id"] = 1
            with urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers), timeout=20) as response:
                session = response.headers.get("Mcp-Session-Id")
                if session:
                    headers["Mcp-Session-Id"] = session
                text = response.read().decode()
            if notification:
                return None
            value = json.loads(next((line[5:] for line in text.splitlines() if line.startswith("data:")), text))
            return value.get("result") or {}
        rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "clone-test", "version": "1"}})
        rpc("notifications/initialized", notification=True)
        tools = rpc("tools/list").get("tools", [])
        if not tools or tools[0]["name"] != "current-tool":
            return False
        result = rpc("tools/call", {"name": tools[0]["name"], "arguments": {}})
        return not result.get("isError") and "a:seed" in json.dumps(result)

    def test_complete_storage_keeps_gamma_am_credentials_and_all_shared_data(self):
        self.instances, self.current = {}, "shared"
        print("Start two Shared playgrounds with Gamma and AM", flush=True)
        self.job("/api/playgrounds/@linked/a/start", {})
        # AM playgrounds are mutually exclusive in the Lab UI. Populate a foreign
        # domain through Compose so the Shared snapshot must retain both playgrounds.
        foreign = self.root / ".lab/foreign.yml"
        foreign.write_text(json.dumps({"include": [str(self.linked / "playgrounds/b/compose.yml")]}))
        self.command([*compose.COMPOSE, "-f", "docker-compose.yml", "-f", ".lab/on.yml", "-f", str(foreign),
                      "up", "-d", "--wait", "b-seed"], timeout=300)
        self.command([*compose.COMPOSE, "-f", "docker-compose.yml", "-f", ".lab/on.yml", "-f", str(foreign),
                      "wait", "b-seed"], timeout=300)
        with (self.root / ".lab/on.yml").open("a") as active:
            active.write("  - " + json.dumps(str(self.linked / "playgrounds/b/compose.yml")) + "\n")
        records = self.records("shared")
        ids = {"domain": next(r["id"] for r in records if r["product"] == "am" and r["kind"] == "domain")}
        if self.GAMMA:
            ids["studio"] = next(r["id"] for r in records if r["kind"] == "studio")
            own_sources = [r["id"] for r in records if r["product"] == "aim-catalog"]
            ids["sources"] = [r["id"] for r in self.gamma("/catalog/sources?perPage=200")["data"]]
            ids["mcp-tools"] = [r["id"] for r in self.gamma("/catalog/mcp-tools?perPage=200")["data"]]
            own_tool = next(r["id"] for r in self.gamma("/catalog/mcp-tools?perPage=200")["data"] if r["sourceId"] in own_sources)
            ids["description"] = self.gamma("/mcp-proxies/" + ids["studio"]).get("description")
            subscription = next(r for r in records if r["product"] == "aim-proxies" and r["kind"] == "subscription")
            ids["key"] = next(row["key"] for row in self.consumer_keys(subscription) if not row["revoked"])
            self.gamma("/mcp-studios/" + ids["studio"] + "/specification", "PUT", {
                "specification": {"tools": [{"id": own_tool, "alias": "current-tool"}]},
                "upstreamAuth": [{"catalogSourceId": own_sources[0], "upstreamAuth": {
                    "type": "api-key", "apiKeyHeader": "X-Gravitee-Api-Key", "apiKey": "upstream-clone-key-000000000000-a"}}]})
            studio = self.management("/apis/" + ids["studio"])
            studio["description"] = "Current studio description"
            self.management("/apis/" + ids["studio"], "PUT", studio)
            self.management("/apis/" + ids["studio"] + "/deployments", "POST", {"deploymentLabel": "Current studio changes"})
            self.assert_key_access("a", "upstream-clone-key-000000000000-a", True)
            self.until(lambda: self.studio_access(ids["key"]), "source studio tool call", timeout=120)
            ids["description"] = self.gamma("/mcp-proxies/" + ids["studio"]).get("description")
        self.am("/domains/" + ids["domain"] + "/users", "POST", {"username": "manual-user", "password": "Manual-Password-456!",
                "email": "manual@example.test", "firstName": "Manual", "lastName": "User", "preRegistration": False, "forceResetPassword": False})
        self.password_login("clone-domain-a", "manual-user", "Manual-Password-456!")
        client = self.am("/domains/" + ids["domain"] + "/applications", "POST", {"name": "Manual client", "type": "BROWSER",
                         "clientId": "manual-clone-client", "clientSecret": "Manual-Client-Secret-789!", "redirectUris": ["https://example.test/callback"]})
        self.am("/domains/" + ids["domain"] + "/applications/" + client["id"], "PATCH", {"settings": {"oauth": {
            "grantTypes": ["client_credentials"], "tokenEndpointAuthMethod": "client_secret_basic"}}})
        self.until(lambda: self.client_login("clone-domain-a", "manual-clone-client", "Manual-Client-Secret-789!") or True,
                   "source manual client credentials", timeout=120)
        domains = self.am("/domains")
        ids["domains"] = [row["id"] for row in (domains.get("data", []) if isinstance(domains, dict) else domains)]
        foreign_before = self.gamma("/catalog/sources?perPage=200") if self.GAMMA else self.am("/domains")
        owned = (self.linked / ".worktrees/playgrounds/a/.state/created.json").read_bytes()
        print("Copy complete Shared storage with Gamma and AM credentials", flush=True)
        self.instances["a"] = self.clone("@linked/a", "Gamma AM copy")
        self.assertEqual(self.gamma("/catalog/sources?perPage=200") if self.GAMMA else self.am("/domains"), foreign_before)
        self.password_login("clone-domain-b", "seed-user", "Seed-Password-123!")
        self.assertEqual((self.linked / ".worktrees/playgrounds/a/.state/created.json").read_bytes(), owned)
        self.command([*compose.COMPOSE, "stop", "apim-gateway", "apim-management-api", "am-gateway", "am-management-api", "mongodb", "elasticsearch"])
        self.current = "a"
        self.job(f"/api/playgrounds/{self.instances['a']}/start", {})
        self.assert_extra_data(ids)
        print("Clone private Gamma/AM runtime and verify passwords again", flush=True)
        self.instances["b"] = self.clone(self.instances["a"], "Private Gamma AM copy")
        self.job(f"/api/playgrounds/{self.instances['a']}/stop", {})
        self.current = "b"
        self.job(f"/api/playgrounds/{self.instances['b']}/start", {})
        self.assert_extra_data(ids)
        self.assertTrue((self.root / ".lab/catalogs/linked").is_symlink())
        self.assertTrue((self.linked / "playgrounds/a").is_symlink())
        print("Complete storage snapshots preserve Gamma tools, keys, AM users and clients", flush=True)


class FreshApiStudioDocker(GammaAmCloneDocker):
    """A fresh runtime seeds its own studio. The portal entrypoint is the host port; the tool call uses the gateway."""

    test_complete_storage_keeps_gamma_am_credentials_and_all_shared_data = None

    def test_a_fresh_runtime_calls_an_api_tool_on_its_own_gateway(self):
        self.instances = {}
        self.instances["fresh"] = self.json(self.lab + "/api/playgrounds/@linked/a/runtimes", "POST",
                                            {"name": "Fresh", "mode": "fresh"})["id"]
        self.current = "fresh"
        print("Start a fresh runtime and call its API-backed tool", flush=True)
        self.job(f"/api/playgrounds/{self.instances['fresh']}/start", {})
        records = self.records("fresh")
        studio = next(row["id"] for row in records if row["kind"] == "studio")
        subscription = next(row for row in records if row["product"] == "aim-proxies" and row["kind"] == "subscription")
        key = next(row["key"] for row in self.consumer_keys(subscription) if not row["revoked"])
        api = self.management("/apis/" + studio)
        targets = [endpoint["configuration"]["target"] for group in api.get("endpointGroups") or []
                   for endpoint in group.get("endpoints") or [] if (endpoint.get("configuration") or {}).get("target")]
        self.assertTrue(targets, "the studio has no HTTP backend")
        self.assertTrue(all(target.startswith("http://apim-gateway:8082/") for target in targets), targets)
        for group in api.get("endpointGroups") or []:
            for endpoint in group.get("endpoints") or []:
                configuration = endpoint.setdefault("configuration", {})
                if configuration.get("target"):
                    configuration["upstreamAuth"] = {"type": "api-key", "apiKeyHeader": "X-Gravitee-Api-Key",
                                                     "apiKey": "upstream-clone-key-000000000000-a"}
        kept = {name: value for name, value in api.items() if name not in (
            "id", "deploymentState", "createdAt", "updatedAt", "state", "definitionContext", "links", "_links",
            "primaryOwner", "originContext", "apiPicture", "apiBackground", "deployedAt")}
        self.management("/apis/" + studio, "PUT", kept)
        self.management("/apis/" + studio + "/deployments", "POST", {"deploymentLabel": "Upstream key"})
        self.until(lambda: self.fresh_tool(key), "fresh runtime API tool call", timeout=120)
        print("Fresh initialization serves the API tool through the runtime gateway", flush=True)

    def fresh_tool(self, key):
        url = self.endpoint("apim-gateway", 8082) + "/tools-a/"
        headers = {"X-Gravitee-Api-Key": key, "Accept": "application/json, text/event-stream",
                   "Content-Type": "application/json", "MCP-Protocol-Version": "2025-06-18"}

        def rpc(method, params=None, notification=False):
            body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
            if not notification:
                body["id"] = 1
            with urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers), timeout=20) as response:
                session = response.headers.get("Mcp-Session-Id")
                if session:
                    headers["Mcp-Session-Id"] = session
                text = response.read().decode()
            if notification:
                return None
            value = json.loads(next((line[5:] for line in text.splitlines() if line.startswith("data:")), text))
            return value.get("result") or {}

        rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "fresh-test", "version": "1"}})
        rpc("notifications/initialized", notification=True)
        tools = rpc("tools/list").get("tools", [])
        if not tools:
            return False
        result = rpc("tools/call", {"name": tools[0]["name"], "arguments": {}})
        return not result.get("isError") and "a:seed" in json.dumps(result)


class AmCloneDocker(GammaAmCloneDocker):
    """The AM credential workflow runs in public CI without an Enterprise license."""
    GAMMA = False


if __name__ == "__main__":
    unittest.main()
