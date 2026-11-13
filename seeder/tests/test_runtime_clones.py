"""Cross-target clone imports remap referenced identities without widening their scope.

Checkpoint tests cover the resource adapters themselves. These cases enforce the
additional destination and membership rules that distinguish a clone from restore.
"""

import copy
import unittest
from unittest.mock import Mock, patch

from lab_seeder import common, runtime_clones
from lab_seeder.common import SeedError


class RuntimeClones(unittest.TestCase):
    def test_export_request_timestamps_are_ignored_but_actual_resource_changes_are_detected(self):
        resources = Mock()
        resources.capture.side_effect = [{"resources": [{"definition": {"export": {"date": date}, "api": {"description": description}}}]}
                                        for date, description in ((1, "current"), (2, "current"), (3, "edited"))]
        with patch.object(runtime_clones, "_users", return_value=[]):
            first, second, third = [runtime_clones._capture(resources, [], []) for _ in range(3)]
        self.assertEqual(runtime_clones._comparable(first), runtime_clones._comparable(second))
        self.assertEqual(first["resources"][0]["definition"]["export"]["date"], 1)
        self.assertEqual(runtime_clones._difference(runtime_clones._comparable(first), runtime_clones._comparable(third)),
                         "snapshot.resources.0.definition.api.description")

    def test_shared_consumer_apps_cannot_expand_a_clone_to_other_playgrounds(self):
        resources = Mock()
        saved = {"consumers": {"applications": [{"managed": False}]}}
        with self.assertRaisesRegex(SeedError, "shared outside"):
            runtime_clones._users(resources, saved)
        resources.at.assert_not_called()

    def test_shared_group_memberships_are_not_silently_dropped_or_copied(self):
        resources = Mock()
        saved = {"consumers": {"applications": [{"managed": True, "entity": {"groups": ["other-group"]}}]}}
        with self.assertRaisesRegex(SeedError, "shared groups"):
            runtime_clones._users(resources, saved)
        resources.at.assert_not_called()

    def test_an_occupied_destination_is_refused_before_creating_members_or_resources(self):
        for records, apis in (([{"id": "existing"}], []), ([], [{"id": "foreign"}])):
            resources = Mock()
            resources.management.all_apis.return_value = apis
            with self.subTest(records=records, apis=apis), patch.object(common, "_read", return_value=records), \
                    self.assertRaisesRegex(SeedError, "new, empty"):
                runtime_clones._import(resources, {})
            resources.apply.assert_not_called()

    def test_import_remaps_owners_and_members_on_a_copy_and_never_changes_the_source_snapshot(self):
        resources = Mock()
        resources.management.all_apis.return_value = []
        resources.target.return_value = {"url": "private"}
        resources.consumers.owned.return_value = []
        saved = {"target": {"url": "shared"}, "users": [{"id": "source-user"}], "consumers": {"applications": [{
            "entity": {"owner": {"id": "source-user"}}, "members": [{"id": "source-user", "role": "PRIMARY_OWNER"}]}]}}
        original = copy.deepcopy(saved)
        with patch.object(common, "_read", return_value=[]), patch.object(common, "save_created"), \
                patch.object(runtime_clones, "_member", return_value="target-user"):
            runtime_clones._import(resources, saved)
        imported = resources.apply.call_args.args[0]
        self.assertEqual(imported["target"], {"url": "private"})
        app = imported["consumers"]["applications"][0]
        self.assertEqual(app["entity"]["owner"]["id"], "target-user")
        self.assertEqual(app["members"][0]["id"], "target-user")
        self.assertEqual(saved, original)

    def test_an_existing_identity_is_referenced_without_copying_other_user_settings(self):
        target = Mock()
        target.find_user.return_value = {"id": "target-user"}
        self.assertEqual(runtime_clones._member(target, {"source": "memory", "sourceId": "user"}), "target-user")
        target.api.post.assert_not_called()

    def test_memory_users_without_email_are_enrolled_through_the_existing_provider_reference(self):
        target = Mock()
        target.v1, target.waiting = "/management", {}
        target.find_user.return_value = None
        target.appears.return_value = {"id": "registered"}
        target.api.post.return_value.json.return_value = {"id": "temporary-group"}
        with patch.object(common, "_read", return_value=[]), patch.object(common, "save_created"):
            self.assertEqual(runtime_clones._member(target, {"id": "original", "source": "memory", "sourceId": "user"}), "registered")
        self.assertEqual(target.api.post.call_count, 1, "no registration POST that requires an absent email")
        target.enroll.assert_called_once()
        target.api.delete.assert_called_once_with("/management/configuration/groups/temporary-group")
