"""Real fresh/clone creation through Lab HTTP, with shared foreign data and linked sources.

The existing independent Docker fixture supplies disposable projects and Gravitee;
this scenario proves whole-runtime storage snapshots and pinned running images.
"""

import json
import os
from pathlib import Path
import shutil
import unittest

from lab_ui.docker import compose
from tests import independent_docker


class RuntimeCloneDocker(independent_docker.IndependentDocker):
    test_private_data_consumers_links_and_shared_platform_survive_stop_resume_and_rebuild = None

    def make_playground(self, letter):
        super().make_playground(letter)
        if letter != "a":
            return
        folder = self.root / ".lab/catalogs/local/playgrounds/a"
        (folder / "image").mkdir()
        (folder / "image/Dockerfile").write_text("FROM busybox:1.37.0\nRUN printf running-image > /image-version\n")
        file = folder / "compose.yml"
        value = json.loads(file.read_text())
        value["services"]["a-backend"].update(image=self.name + "/backend:local", build={"context": "./image"}, pull_policy="never")
        file.write_text(json.dumps(value))

    def cleanup_lab(self):
        images = {self.name + "/backend:local"}
        for file in self.root.glob(".lab/runtimes/*/runtime.json"):
            value = json.loads(file.read_text())
            images.update(service["image"] for service in value.get("services", {}).values()
                          if service.get("image", "").startswith(value["project"] + "/"))
        destination = os.getenv("INDEPENDENT_E2E_ARTIFACTS")
        if destination:
            folder = Path(destination)
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            for file in self.root.glob(".lab/runtimes/*/clone/failure.json"):
                target = folder / (file.parent.parent.name + "-failure.json")
                shutil.copyfile(file, target)
                target.chmod(0o600)
        super().cleanup_lab()
        # Only this disposable lab's private aliases; keep shared image layers and build caches.
        for tag in images:
            compose.run(["docker", "image", "rm", "--no-prune", tag], timeout=30)

    def runtime_command(self, letter):
        return list(compose.COMPOSE) if letter == "shared" else super().runtime_command(letter)

    def clone(self, source, label):
        answer = self.json(self.lab + "/api/playgrounds/@linked/a/runtimes", "POST",
                           {"name": label, "mode": "clone", "source": source})
        jid = answer["job"]["id"]
        def done():
            job = self.json(self.lab + "/api/state").get("job") or {}
            self.assertEqual(job.get("id"), jid)
            if not job.get("done"):
                return False
            self.assertTrue(job.get("ok"), job.get("error"))
            return True
        self.until(done, "runtime clone " + label, timeout=900)
        return answer["id"]

    def test_shared_and_private_clones_copy_complete_storage_and_preserve_the_source(self):
        self.instances = {}
        self.current = "shared"
        source = self.root / ".lab/catalogs/linked/playgrounds/a"
        source_config = (source / "compose.yml").read_bytes()
        print("Start shared playground and add current consumer data", flush=True)
        self.job("/api/playgrounds/@linked/a/start", {})
        (source / "image/Dockerfile").write_text("FROM busybox:1.37.0\nRUN printf edited-image > /image-version\n")
        api = self.api("a")
        api_id = api["id"]
        edited = self.management("/apis/" + api_id)
        edited["description"] = "Current source changes, absent from the initial seed"
        self.management("/apis/" + api_id, "PUT", edited)
        self.management("/apis/" + api_id + "/deployments", "POST", {"deploymentLabel": "Current changes"})
        app = self.consumer_app("Current consumer")
        sub = self.consumer_sub("a", app)
        key = next(row["key"] for row in self.consumer_keys(sub) if not row["revoked"])
        self.assert_key_access("a", key, True)
        # Docker can own a Shared app's bind directory on Linux.
        self.command([*compose.COMPOSE, "exec", "-T", "a-backend", "sh", "-c",
                      "printf '%s' 'current playground files' > /data/sentinel"])
        ownership = (source / ".state/created.json").read_bytes()

        definition = self.management("/apis/" + api_id + "/_export/definition")
        definition["api"].pop("id", None)
        definition["api"].update(name="Independent B", crossId="foreign-b")
        definition["api"]["listeners"][0]["paths"][0]["path"] = "/state-b/"
        for plan in definition["plans"]:
            plan.pop("id", None)
        foreign = self.management("/apis/_import/definition", "POST", definition)
        public = self.management("/apis/" + foreign["id"])
        public.update(visibility="PUBLIC", lifecycleState="PUBLISHED")
        self.management("/apis/" + foreign["id"], "PUT", public)
        self.management("/apis/" + foreign["id"] + "/deployments", "POST", {"deploymentLabel": "Other playground"})
        foreign_app = self.consumer_app("Other playground consumer")
        foreign_sub = self.consumer_sub("b", foreign_app)
        foreign_keys = self.consumer_keys(foreign_sub)
        other_record = source.parent / "b/.state/created.json"
        other_record.write_text(json.dumps([{"product": "apim", "kind": "api", "label": "APIs", "name": "Independent B", "id": foreign["id"]}]))
        foreign_before = self.management("/apis/" + foreign["id"])
        source_rows = self.command([*compose.COMPOSE, "ps", "--format", "{{.ID}} {{.State}}"])
        print("Clone all Shared storage, including its other API, consumer and Redis sentinel", flush=True)
        self.instances["a"] = self.clone("@linked/a", "Copy of Shared")
        self.assertEqual(self.command([*compose.COMPOSE, "ps", "--format", "{{.ID}} {{.State}}"]), source_rows)
        self.assertEqual(self.management("/apis/" + foreign["id"]), foreign_before)
        self.assertEqual(self.consumer_keys(foreign_sub), foreign_keys)
        self.assert_key_access("a", key, True)
        self.assertEqual((source / ".state/created.json").read_bytes(), ownership)
        self.assertEqual(self.command([*compose.COMPOSE, "exec", "-T", "a-backend", "cat", "/image-version"]), "running-image")
        self.assertEqual(self.metadata("a")["active"], False)
        self.assertFalse(self.metadata("a")["clone_pending"])
        self.assertFalse(any(self.root.glob(".lab/runtimes/*/clone/foreign.json")))
        self.assertNotIn("running", self.command([*self.runtime_command("a"), "ps", "--all", "--format", "{{.State}} "]))

        # Keep peak memory bounded: only disposable source services are stopped here.
        self.command([*compose.COMPOSE, "stop", "apim-gateway", "apim-management-api", "mongodb", "elasticsearch"])
        self.current = "a"
        self.job(f"/api/playgrounds/{self.instances['a']}/start", {})
        self.assertEqual(self.api("a")["id"], api_id)
        self.assertEqual(self.command([*self.runtime_command("a"), "exec", "-T", "a-backend", "cat", "/image-version"]), "running-image")
        self.assertEqual(self.management("/apis/" + api_id)["description"], edited["description"])
        copied_foreign = self.management("/apis/" + foreign["id"])
        for field in ("id", "name", "description", "endpointGroups", "listeners"):
            self.assertEqual(copied_foreign[field], foreign_before[field])
        self.assertEqual(self.consumer("/applications/" + app["id"])["name"], app["name"])
        self.assertEqual(self.consumer("/applications/" + foreign_app["id"])["name"], foreign_app["name"])
        self.assertEqual(self.consumer_keys(foreign_sub), foreign_keys)
        self.assert_key_access("a", key, True)
        data = Path(self.metadata("a")["binds"][str((source / ".state/app").resolve())])
        self.assertEqual((data / "sentinel").read_text(), "current playground files")
        self.assertEqual(self.command([*self.runtime_command("a"), "exec", "-T", "redis", "redis-cli", "GET", "shared-sentinel"]), "keep-original")
        self.command([*self.runtime_command("a"), "exec", "-T", "redis", "redis-cli", "SET", "private-clone", "current private data"])
        self.command([*self.runtime_command("a"), "exec", "-T", "mongodb", "sh", "-c", "printf cloned > /data/configdb/clone-marker"])
        identities = self.command([*self.runtime_command("a"), "ps", "--format", "{{.ID}} {{.State}}"])
        print("Clone the running independent runtime, including private volumes", flush=True)
        self.instances["b"] = self.clone(self.instances["a"], "Copy of independent")
        self.assertEqual(self.command([*self.runtime_command("a"), "ps", "--format", "{{.ID}} {{.State}}"]), identities)
        self.assert_key_access("a", key, True)
        self.assertFalse(set(self.metadata("a")["ports"].values()) & set(self.metadata("b")["ports"].values()))
        self.job(f"/api/playgrounds/{self.instances['a']}/stop", {})
        self.current = "b"
        self.job(f"/api/playgrounds/{self.instances['b']}/start", {})
        self.assertEqual(self.api("a")["id"], api_id)
        self.assert_key_access("a", key, True)
        self.assertEqual(self.command([*self.runtime_command("b"), "exec", "-T", "redis", "redis-cli", "GET", "private-clone"]), "current private data")
        self.assertEqual(self.command([*self.runtime_command("b"), "exec", "-T", "mongodb", "cat", "/data/configdb/clone-marker"]), "cloned")
        self.job(f"/api/playgrounds/{self.instances['b']}/stop", {})
        print("Fresh creation uses the seed instead of copying current changes", flush=True)
        self.instances["fresh"] = self.json(self.lab + "/api/playgrounds/@linked/a/runtimes", "POST", {"name": "Fresh", "mode": "fresh"})["id"]
        self.current = "fresh"
        self.job(f"/api/playgrounds/{self.instances['fresh']}/start", {})
        self.assertNotEqual(self.api("a")["id"], api_id)
        self.assertEqual(self.command([*self.runtime_command("fresh"), "exec", "-T", "a-backend", "cat", "/image-version"]), "edited-image")
        self.assertNotEqual(self.management("/apis/" + self.api("a")["id"])["description"], edited["description"])
        self.assert_key_access("a", key, False)
        self.assertEqual((source / "compose.yml").read_bytes(), source_config)
        self.assertEqual((source / ".state/created.json").read_bytes(), ownership)
        self.assertTrue(source.is_symlink())
        self.assertTrue((self.root / ".lab/catalogs/linked").is_symlink())
        self.assertEqual(self.command([*compose.COMPOSE, "exec", "-T", "redis", "redis-cli", "GET", "shared-sentinel"]), "keep-original")
        print("Fresh and complete-storage clone workflows passed", flush=True)


if __name__ == "__main__":
    unittest.main()
