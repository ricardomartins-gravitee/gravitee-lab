"""Linked catalog checkpoints preserve development trees while restoring their machine state.

These cover the registered-folder adapter, including the nested worktree links a developer uses;
the runtime transaction and database behavior are covered by the saved-state Docker checks.
"""

import io
import json
import os
import stat
import sys
import tarfile
from pathlib import Path
from unittest import mock

from lab_ui import paths
from lab_ui.docker.jobs import JobFailed
from lab_ui.library import saved_states

from .. import Case


class LinkedCatalogs(Case):
    def setUp(self):
        self.folder()
        self.lab = self.root / "lab"
        self.patch(paths, "ROOT", self.lab)
        self.patch(saved_states, "ROOT", self.lab)
        self.source = self.root / "source"
        self.item = self.source / ".worktrees/demo/playgrounds/demo"
        (self.item / ".state").mkdir(parents=True)
        (self.item / ".state/created.json").write_text('{"api":"saved"}')
        (self.item / ".env").write_text("SETTING=saved\n")
        (self.item / "compose.yml").write_text("services: {}\n")
        (self.item / "secrets").mkdir(mode=0o700)
        (self.source / ".git").mkdir()
        (self.source / ".git/HEAD").write_text("source git is unchanged\n")
        (self.source / "playgrounds").mkdir()
        self.inner = self.source / "playgrounds/demo"
        self.inner.symlink_to(self.item, target_is_directory=True)
        self.link = self.lab / ".lab/catalogs/linked"
        self.link.parent.mkdir(parents=True)
        self.link.symlink_to(self.source, target_is_directory=True)
        (self.lab / ".lab/catalogs.json").write_text(json.dumps({"schema": 1, "catalogs": [
            {"id": "linked", "kind": "folder", "path": str(self.source)}]}))
        self.snapshot = self.root / "snapshot"
        self.snapshot.mkdir()

    def save(self):
        saved_states._archive_config(self.snapshot / "config.tar.gz", [])

    def test_restore_keeps_catalog_and_worktree_links_source_and_git_but_restores_machine_files(self):
        links = [(path, os.readlink(path), path.lstat().st_ino) for path in (self.link, self.inner)]
        self.save()
        (self.item / ".state/created.json").write_text('{"api":"deleted"}')
        (self.item / ".state/stale.json").write_text("not saved")
        (self.item / ".env").unlink()
        os.chmod(self.item / "secrets", 0o755)
        saved_states._restore_config(self.snapshot)
        self.assertEqual((self.item / ".state/created.json").read_text(), '{"api":"saved"}')
        self.assertFalse((self.item / ".state/stale.json").exists())
        self.assertEqual((self.item / ".env").read_text(), "SETTING=saved\n")
        self.assertEqual(stat.S_IMODE((self.item / "secrets").stat().st_mode), 0o700)
        for path, target, inode in links:
            self.assertTrue(path.is_symlink())
            self.assertEqual((os.readlink(path), path.lstat().st_ino), (target, inode))
        self.assertEqual((self.item / "compose.yml").read_text(), "services: {}\n")
        self.assertEqual((self.source / ".git/HEAD").read_text(), "source git is unchanged\n")
        with tarfile.open(self.snapshot / "config.tar.gz") as archive:
            self.assertFalse(any(".git" in Path(member.name).parts for member in archive))

    def test_changed_source_and_retargeted_links_refuse_before_replacing_any_configuration(self):
        self.save()
        (self.lab / ".env").write_text("LIVE=keep\n")
        code = self.item / "compose.yml"
        original = code.read_text()
        code.write_text("services: {new: {}}\n")
        with self.assertRaisesRegex(ValueError, "source or links changed"):
            saved_states._restore_config(self.snapshot)
        self.assertEqual((self.lab / ".env").read_text(), "LIVE=keep\n")
        self.assertEqual(code.read_text(), "services: {new: {}}\n")
        code.write_text(original)
        other = self.source / ".worktrees/other"
        other.mkdir()
        self.inner.unlink()
        self.inner.symlink_to(other, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "source or links changed"):
            saved_states._restore_config(self.snapshot)
        self.assertEqual((self.lab / ".env").read_text(), "LIVE=keep\n")

    def test_only_registered_catalog_links_and_bounded_worktree_links_are_followed(self):
        stray = self.link.parent / "stray"
        stray.symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "registered folder catalogs"):
            self.save()
        stray.unlink()
        for target, reason in ((self.root, "leaves its registered folder"), (self.source, "link cycle")):
            with self.subTest(target=target):
                extra = self.source / "extra"
                extra.symlink_to(target, target_is_directory=True)
                with self.assertRaisesRegex(ValueError, reason):
                    self.save()
                extra.unlink()
        (self.item / ".state/escape").symlink_to(self.root)
        with self.assertRaisesRegex(ValueError, "machine files must not contain links"):
            self.save()
        (self.item / ".state/escape").unlink()
        os.link(self.item / ".state/created.json", self.item / ".state/hardlink")
        with self.assertRaisesRegex(ValueError, "machine files must not contain links"):
            self.save()

    def test_bind_paths_accept_logical_and_resolved_catalog_state_but_refuse_writable_source(self):
        for path in (self.link / "playgrounds/demo/.state", self.item / ".state"):
            with self.subTest(path=path):
                self.assertIn(".lab/catalogs", saved_states._paths_to_save([
                    {"Source": str(path), "Destination": "/state", "RW": True}]))
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertIn(".lab/catalogs", saved_states._paths_to_save([
                {"Source": "/host_mnt" + str(self.item / ".state"), "Destination": "/state", "RW": True}]))
        self.assertIn(".lab/catalogs", saved_states._paths_to_save([
            {"Source": str(self.item), "Destination": "/seed", "RW": False}]))
        for path in (self.item, self.source / ".git", self.root):
            with self.subTest(path=path):
                with self.assertRaises(JobFailed):
                    saved_states._paths_to_save([{"Source": str(path), "Destination": "/write", "RW": True}])

    def test_a_forged_archive_cannot_overwrite_source_through_a_registered_link(self):
        self.save()
        file = self.snapshot / "config.tar.gz"
        with tarfile.open(file, "r:gz") as archive:
            contents = [(item, archive.extractfile(item).read() if item.isfile() else None) for item in archive]
        for name in (".lab/catalogs/linked/playgrounds/demo/compose.yml", "linked-state/0/../../outside",
                     "linked-state/0/playgrounds/demo/compose.yml", "./linked-state/0/playgrounds/demo/.env"):
            with self.subTest(name=name):
                with tarfile.open(file, "w:gz") as archive:
                    for info, content in contents:
                        archive.addfile(info, io.BytesIO(content) if content is not None else None)
                    info = tarfile.TarInfo(name)
                    info.size = 3
                    archive.addfile(info, io.BytesIO(b"bad"))
                with self.assertRaises(ValueError):
                    saved_states._restore_config(self.snapshot)
                self.assertEqual((self.item / "compose.yml").read_text(), "services: {}\n")
                self.assertEqual((self.item / ".env").read_text(), "SETTING=saved\n")

    def test_readonly_runtime_helpers_are_verified_without_replacing_the_source(self):
        file = self.lab / "lab-ui/frames/helper.sh"
        file.parent.mkdir(parents=True)
        file.write_text("saved helper\n")
        mounts = [{"Source": str(file), "Destination": "/helper.sh", "RW": False}]
        saved_states._archive_config(self.snapshot / "config.tar.gz", mounts)
        saved_states._config_members(self.snapshot)
        file.write_text("new development work\n")
        with self.assertRaisesRegex(ValueError, "read-only runtime file changed"):
            saved_states._restore_config(self.snapshot)
        self.assertEqual(file.read_text(), "new development work\n")
