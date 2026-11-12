"""Docker storage inventory strips environments and keeps cleanup helpers confined to one verified bind."""

import csv
import io
import json

from lab_ui.docker import compose, storage
from tests import Case


class Storage(Case):
    def test_inventory_keeps_ownership_and_ids_but_never_container_environments(self):
        self.patch(storage, "ask", return_value="id")
        self.patch(storage, "inspect", side_effect=[
            [{"Id": "container-id", "Name": "/own-db", "State": {"Status": "exited"},
              "Config": {"Env": ["PASSWORD=do-not-return"], "Labels": {storage.PROJECT: "own",
                          storage.PROJECT + ".working_dir": "/private", "com.docker.compose.service": "db"}}}],
            [{"Id": "network-id", "Name": "own_internal", "Labels": {storage.PROJECT: "own", "com.docker.compose.network": "internal"}}],
            [{"Name": "own_data", "CreatedAt": "today", "Driver": "local", "Labels": {storage.PROJECT: "own", "com.docker.compose.volume": "data"}}]])
        result = storage.resources("own")
        self.assertEqual(result["containers"][0]["id"], "container-id")
        self.assertEqual(result["networks"][0]["key"], "internal")
        self.assertNotIn("do-not-return", json.dumps(result))

    def test_helper_binds_only_one_private_folder_and_verifies_its_record_before_unlinking(self):
        command = storage.remove_files_command('/tmp/Lab, "test"/runtime', "expected-digest")
        mount = command[command.index("--mount") + 1]
        fields = next(csv.reader(io.StringIO(mount)))
        self.assertEqual(fields, ["type=bind", 'source=/tmp/Lab, "test"/runtime', "target=/runtime"])
        self.assertEqual(command[-1], "expected-digest")
        self.assertIn("sha256sum /runtime/runtime.json", command[-3])
        self.assertIn("--read-only", command)
        self.assertNotIn("--privileged", command)
        self.assertEqual(command[command.index("--network") + 1], "none")

    def test_disk_usage_does_not_charge_shared_image_layers_to_each_runtime(self):
        compose.CACHE.drop("local:runtime-storage-usage")
        self.addCleanup(compose.CACHE.drop, "local:runtime-storage-usage")
        self.patch(storage, "ask", return_value=json.dumps({"Volumes": [{"Name": "own_data", "Size": "2.5MB"}, {"Name": "unknown", "Size": "N/A"}]}))
        self.patch(compose, "run", return_value=(0, json.dumps({"Type": "Images", "Size": "3GB"}), ""))
        result = storage.usage()
        self.assertEqual(result["volumes"], {"own_data": 2500000, "unknown": None})
        self.assertEqual(result["shared"], [{"kind": "Images", "bytes": 3000000000}])

    def test_created_external_container_references_are_found_without_reading_its_environment(self):
        ask = self.patch(storage, "ask", side_effect=["external-id", json.dumps({"id": "external-id", "mode": "own_internal", "networks": {}})])
        self.assertEqual(storage.network_users([{"id": "network-id", "name": "own_internal"}]), {"network-id": {"external-id"}})
        self.assertNotIn("Config.Env", repr(ask.call_args_list))
