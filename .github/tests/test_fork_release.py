"""Fork release planning against real Git histories and publishing against a fake GitHub API.

The upstream next_version tests do not cover upstream provenance, fork revisions, or retrying partial publication.
No test calls a real publishing endpoint or changes the repository's tags.
"""

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fork_release as release  # noqa: E402


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class Planning(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="fork-release-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        patch = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
                                           "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                                           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"})
        patch.start()
        self.addCleanup(patch.stop)
        self.git("init", "-q", "-b", "main")
        self.count = 0
        self.base = self.commit("Upstream release")
        self.upstream("v1.20.15", self.base)
        self.main()

    def git(self, *args):
        return release.git(self.root, "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args)

    def commit(self, message):
        self.count += 1
        path = f"change-{self.count}.txt"
        (self.root / path).write_text(message + "\n", encoding="utf-8")
        self.git("add", path)
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def upstream(self, tag, sha):
        self.git("update-ref", release.UPSTREAM_TAGS + tag, sha)
        self.git("update-ref", release.UPSTREAM_MAIN, sha)

    def main(self):
        self.git("update-ref", release.MAIN, "HEAD")

    def plan(self, expected=""):
        return release.plan(self.root, "upstream/lab", expected)

    def test_parent_revisions_retries_and_a_new_upstream_parent(self):
        self.assertEqual(self.plan()["tag"], "v1.20.15")
        first = self.commit("Fix a fork issue")
        self.main()
        planned = self.plan()
        self.assertEqual((planned["tag"], planned["parent"]), ("v1.20.15-rev.1", "v1.20.15"))
        self.assertIn("Fix a fork issue", planned["notes"])
        self.assertNotIn("Upstream release\n", planned["notes"])
        self.git("tag", "v1.20.15-rev.1")
        self.assertEqual(self.plan()["tag"], "v1.20.15-rev.1", "a retry uses this commit's tag")
        self.commit("Another fork change")
        self.main()
        self.assertEqual(self.plan()["tag"], "v1.20.15-rev.2")
        self.git("tag", "v1.20.15-rev.9")
        self.commit("One more fork change")
        self.main()
        self.assertEqual(self.plan()["tag"], "v1.20.15-rev.10")
        self.git("checkout", "-qb", "upstream", self.base)
        newer = self.commit("New upstream release")
        self.upstream("v1.20.16", newer)
        self.git("checkout", "-q", "main")
        self.git("merge", "--no-ff", "-qm", "Merge the new upstream release", "upstream")
        self.main()
        self.assertEqual(self.plan()["tag"], "v1.20.16-rev.1", "a new parent starts its revision at 1")
        self.assertTrue(release.ancestor(self.root, first, "HEAD"))
        self.git("checkout", "-q", "upstream")
        self.main()
        self.assertEqual(self.plan()["tag"], "v1.20.16", "the exact upstream commit uses its bare tag")

    def test_an_upstream_release_not_merged_yet_does_not_change_the_base(self):
        self.git("checkout", "-qb", "upstream")
        newer = self.commit("Future upstream release")
        self.upstream("v1.20.16", newer)
        self.git("checkout", "-q", "main")
        self.commit("Fork change")
        self.main()
        self.assertEqual(self.plan()["tag"], "v1.20.15-rev.1")

    def test_upstream_tags_are_numeric_and_previews_are_not_parents(self):
        self.upstream("v1.9.99", self.base)
        self.upstream("v1.99.0-rc.1", self.base)
        self.assertEqual(self.plan()["parent"], "v1.20.15")

    def test_a_bare_fork_tag_cannot_masquerade_as_an_upstream_release(self):
        self.commit("Fork change")
        self.main()
        for tag in ("v1.20.15", "v1.20.16"):
            with self.subTest(tag=tag):
                self.git("tag", tag)
                with self.assertRaises(ValueError):
                    self.plan()
                self.git("tag", "-d", tag)

    def test_an_unchanged_upstream_release_cannot_be_given_a_fork_revision(self):
        self.git("tag", "v1.20.15-rev.1")
        with self.assertRaisesRegex(ValueError, "no fork changes"):
            self.plan()

    def test_a_revision_number_on_another_branch_is_not_reused(self):
        self.git("checkout", "-qb", "side")
        self.commit("Not merged")
        self.git("tag", "v1.20.15-rev.4")
        self.git("checkout", "-q", "main")
        self.commit("Fork change")
        self.main()
        self.assertEqual(self.plan()["tag"], "v1.20.15-rev.5")

    def test_only_main_and_the_reviewed_commit_can_be_planned(self):
        self.commit("Not the current main")
        with self.assertRaisesRegex(ValueError, "current main"):
            self.plan()
        self.main()
        for expected in (self.base, "a" * 39, "a" * 40 + "\n"):
            with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, "reviewed commit"):
                self.plan(expected)

    def test_the_preview_command_is_read_only_and_publishing_needs_a_reviewed_sha(self):
        self.commit("Fork change")
        self.main()
        before = self.git("for-each-ref")
        env = {**os.environ, "UPSTREAM_REPOSITORY": "upstream/lab", "PUBLISH": "false", "EXPECTED_COMMIT": "",
               "GITHUB_OUTPUT": str(self.root / "output"), "GITHUB_STEP_SUMMARY": str(self.root / "summary")}
        command = [sys.executable, "-I", str(Path(release.__file__)), "plan"]
        done = subprocess.run(command, cwd=self.root, env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["tag"], "v1.20.15-rev.1")
        self.assertEqual(before, self.git("for-each-ref"))
        self.assertIn("v1.20.15-rev.1", (self.root / "summary").read_text())
        done = subprocess.run(command, cwd=self.root, env={**env, "PUBLISH": "true"}, capture_output=True, text=True)
        self.assertEqual(done.returncode, 1)
        self.assertIn("full commit SHA", done.stderr)


class Publishing(unittest.TestCase):
    def setUp(self):
        self.head = "a" * 40
        self.proposed = {"tag": "v1.20.15-rev.1", "commit": self.head, "notes": "Based on upstream v1.20.15"}
        self.main, self.tags, self.releases, self.calls = self.head, [], [], []

    def api(self, repository, path, data=None, pages=False):
        self.assertEqual(repository, "owner/lab")
        self.calls.append((path, copy.deepcopy(data)))
        if path == "git/ref/heads/main":
            return {"object": {"sha": self.main}}
        if path.startswith("git/matching-refs/tags/"):
            return self.tags
        if path == "git/tags" and data:
            return {"sha": "b" * 40}
        if path == "git/tags/" + "b" * 40:
            return {"object": {"type": "commit", "sha": self.head}}
        if path == "git/refs":
            self.tags.append({"ref": data["ref"], "object": {"type": "tag", "sha": data["sha"]}})
            return {}
        if path == "releases?per_page=100":
            self.assertTrue(pages)
            return [self.releases]
        if path == "releases" and data:
            created = {**data, "html_url": "https://github.com/owner/lab/releases/tag/" + data["tag_name"]}
            self.releases.append(created)
            return created
        self.fail(f"Unexpected API request: {path}")

    def publish(self):
        return release.publish(self.proposed, "owner/lab", self.api)

    def writes(self):
        return [(path, data) for path, data in self.calls if data is not None]

    def test_an_annotated_tag_and_stable_release_are_created_once(self):
        url = self.publish()
        self.assertTrue(url.endswith("/v1.20.15-rev.1"))
        self.assertEqual([path for path, _ in self.writes()], ["git/tags", "git/refs", "releases"])
        published = self.writes()[-1][1]
        self.assertEqual((published["draft"], published["prerelease"], published["make_latest"]), (False, False, "true"))
        self.calls.clear()
        self.assertEqual(self.publish(), url)
        self.assertEqual(self.writes(), [])

    def test_a_retry_finishes_a_tag_whose_release_was_not_created(self):
        self.tags = [{"ref": "refs/tags/v1.20.15-rev.1", "object": {"type": "commit", "sha": self.head}}]
        self.publish()
        self.assertEqual([path for path, _ in self.writes()], ["releases"])

    def test_moved_main_or_a_conflicting_tag_causes_no_write(self):
        self.main = "c" * 40
        with self.assertRaisesRegex(ValueError, "Main moved"):
            self.publish()
        self.assertEqual(self.writes(), [])
        self.main = self.head
        self.tags = [{"ref": "refs/tags/v1.20.15-rev.1", "object": {"type": "commit", "sha": "c" * 40}}]
        with self.assertRaisesRegex(ValueError, "never moved"):
            self.publish()
        self.assertEqual(self.writes(), [])

    def test_other_tag_forms_and_unsafe_repository_names_are_refused(self):
        for tag in ("v1.20.15-rev.0", "v1.20.15-rev.01", "v1.20.15-rc.1", "v1.20.15-rev.1 "):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                release.publish({**self.proposed, "tag": tag}, "owner/lab", self.api)
        for repository in ("../lab", "owner/lab/extra", "owner/lab\n"):
            with self.subTest(repository=repository), self.assertRaises(ValueError):
                release.publish(self.proposed, repository, self.api)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
