"""Offline MongoDB URL relocation on real copied storage, preserving source data and BSON.

Run explicitly with Docker; every container and volume is uniquely named and disposable.
"""

import json
import shutil
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from lab_ui import paths
from lab_ui.catalogs import independent, runtime_clones, runtime_urls
from lab_ui.docker import compose, storage


class RuntimeUrlsDocker(unittest.TestCase):
    def test_only_the_copied_database_changes_and_bson_credentials_survive(self):
        with tempfile.TemporaryDirectory(prefix="lab-clone-urls-") as temp, patch.object(paths, "ROOT", Path(temp)):
            root = Path(temp)
            shutil.copytree(Path(__file__).resolve().parents[1] / "clone", root / "lab-ui/clone")
            name = "lab-clone-urls-" + uuid.uuid4().hex[:12]
            target = "runtime-" + uuid.uuid4().hex
            project = independent.name(target)
            volumes = [name + "-source", project + "_data"]
            containers = []
            owned_volumes = {}

            def run(command):
                return runtime_clones._execute(command)

            def claim(volume, owner):
                names = run(["docker", "volume", "ls", "--format", "{{.Name}}"]).split()
                self.assertNotIn(volume, names, "A URL test volume already exists; it must not be reused or removed")
                owned_volumes[volume] = owner

            def cleanup():
                # Never remove a planned name whose creation was refused, or one with changed ownership.
                for container in containers:
                    row = storage.inspect("container", [container])[0]
                    self.assertEqual(row["Config"]["Labels"].get(storage.PROJECT), name)
                names = run(["docker", "volume", "ls", "--format", "{{.Name}}"]).split()
                for volume, owner in owned_volumes.items():
                    if volume in names:
                        row = storage.inspect("volume", [volume])[0]
                        self.assertEqual((row.get("Labels") or {}).get(storage.PROJECT), owner)
                for container in containers:
                    run(["docker", "rm", "--force", "--volumes", container])
                for volume in owned_volumes:
                    if volume in names:
                        run(["docker", "volume", "rm", volume])
            try:
                claim(volumes[0], name)
                runtime_clones._execute(["docker", "volume", "create", "--label", storage.PROJECT + "=" + name, volumes[0]])
                cid = runtime_clones._execute(["docker", "run", "-d", "--network", "none", "--name", name,
                    "--label", storage.PROJECT + "=" + name, "--memory", "512m", "--env", "GLIBC_TUNABLES=glibc.pthread.rseq=1",
                    "--mount", "type=volume,source=" + volumes[0] + ",target=/data/db", "mongo:8.0.34",
                    "mongod", "--bind_ip", "127.0.0.1", "--wiredTigerCacheSizeGB", "0.25"])
                containers.append(cid)
                deadline = time.monotonic() + 90
                while True:
                    rc, _, _ = compose.run(["docker", "exec", cid, "mongosh", "--quiet", "--eval", "db.runCommand({ping:1})"], timeout=15)
                    if not rc:
                        break
                    self.assertLess(time.monotonic(), deadline, "The isolated MongoDB did not start")
                    time.sleep(1)

                def mongo(script):
                    return runtime_clones._execute(["docker", "exec", cid, "mongosh", "--norc", "--quiet", "--eval", script])

                mongo('db=db.getSiblingDB("copied"); db.apps.insertOne({_id:ObjectId("123456789012345678901234"), '
                      'url:"http://localhost:18083/callback", external:"https://example.test:18083/path", '
                      'nested:{definition:JSON.stringify({target:"http://host.docker.internal:18082/api"})}, '
                      'serial:NumberLong("9223372036854775806"), date:ISODate("2026-01-01T00:00:00Z"), '
                      'key:BinData(0,"b3BhcXVlLWNyZWRlbnRpYWw=")}); db.adminCommand({fsync:1})')
                original = mongo('print(EJSON.stringify(db.getSiblingDB("copied").apps.findOne(),null,0,{relaxed:false}))')
                claim(volumes[1], project)
                runtime_clones._paused([cid], lambda: runtime_clones._volume(volumes[0], target, "data"), {
                    cid: ["mongosh", "--quiet", "--eval", "db.adminCommand({fsync:1})"]})
                image = storage.inspect("container", [cid])[0]["Image"]
                config = {"x-gravitee": {"snapshot": {"db": {"relocate": ["sh", "/gravitee-lab-clone/mongodb.sh"]}}},
                    "services": {"db": {"image": image,
                    "environment": {"GLIBC_TUNABLES": "glibc.pthread.rseq=1"},
                    "volumes": [{"type": "volume", "source": "data", "target": "/data/db"}]}}}
                runtime_urls.relocate(config, target, {"18083": 28083, "18082": 28082}, runtime_clones._execute)
                self.assertEqual(mongo('print(EJSON.stringify(db.getSiblingDB("copied").apps.findOne(),null,0,{relaxed:false}))'), original)
                copied = runtime_clones._execute(["docker", "run", "-d", "--network", "none", "--label", storage.PROJECT + "=" + name, "--memory", "512m",
                    "--env", "GLIBC_TUNABLES=glibc.pthread.rseq=1", "--mount", "type=volume,source=" + volumes[1] + ",target=/data/db",
                    image, "mongod", "--bind_ip", "127.0.0.1", "--wiredTigerCacheSizeGB", "0.25"])
                containers.append(copied)
                deadline = time.monotonic() + 90
                while True:
                    rc, out, _ = compose.run(["docker", "exec", copied, "mongosh", "--norc", "--quiet", "--eval",
                        'print(EJSON.stringify(db.getSiblingDB("copied").apps.findOne(),null,0,{relaxed:false}))'], timeout=15)
                    if not rc:
                        break
                    self.assertLess(time.monotonic(), deadline, "The copied MongoDB did not start")
                    time.sleep(1)
                expected = json.loads(original)
                expected["url"] = "http://localhost:28083/callback"
                expected["nested"]["definition"] = '{"target":"http://host.docker.internal:28082/api"}'
                self.assertEqual(json.loads(out), expected)
                self.assertEqual(storage.inspect("container", [cid])[0]["State"]["Paused"], False)
            finally:
                cleanup()


if __name__ == "__main__":
    unittest.main()
