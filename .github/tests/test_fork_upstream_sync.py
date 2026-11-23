"""A release update must retain both histories and stop instead of publishing a conflicting merge."""

import contextlib
import importlib.util
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fork_upstream_sync.py"
SPEC = importlib.util.spec_from_file_location("fork_upstream_sync", SCRIPT)
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


class Releases(unittest.TestCase):
    def test_highest_stable_public_release(self):
        releases = [[{"tag_name": tag, "draft": draft, "prerelease": preview}
                     for tag, draft, preview in (("v1.3.9", False, False), ("v1.3.10", False, False),
                                                  ("v2.0.0", True, False), ("v3.0.0", False, True),
                                                  ("v1.3.10-rev.1", False, False))]]
        self.assertEqual(sync.newest_release(releases), "v1.3.10")


class Merge(unittest.TestCase):
    def git(self, *args):
        return subprocess.check_output(["git", *args], text=True, stderr=subprocess.PIPE).strip()

    def commit(self, name, contents):
        Path(name).write_text(contents, encoding="utf-8")
        self.git("add", name)
        self.git("commit", "-m", contents)
        return self.git("rev-parse", "HEAD")

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(folder.name)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.base = self.commit("base", "common")

    def test_combines_both_histories_and_preserves_fork_files(self):
        fork = self.commit("fork-feature", "fork-only change")
        self.git("checkout", "--detach", self.base)
        upstream = self.commit("upstream-feature", "upstream change")
        candidate = sync.merge_release(fork, upstream, "v1.2.3")
        self.assertEqual(self.git("show", "-s", "--format=%P", candidate), f"{fork} {upstream}")
        self.assertEqual(self.git("show", f"{candidate}:fork-feature"), "fork-only change")
        self.assertEqual(self.git("show", f"{candidate}:upstream-feature"), "upstream change")
        self.assertEqual(self.git("rev-parse", "main"), fork)

    def test_conflicts_report_paths_without_moving_main(self):
        fork = self.commit("base", "fork")
        self.git("checkout", "--detach", self.base)
        upstream = self.commit("base", "upstream")
        captured = io.StringIO()
        with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""}), contextlib.redirect_stdout(captured):
            with self.assertRaisesRegex(ValueError, "Resolve the upstream merge"):
                sync.merge_release(fork, upstream, "v1.2.3")
        self.assertIn("`base`", captured.getvalue())
        self.assertEqual(self.git("rev-parse", "main"), fork)
