"""Seed image selection preserves a playground's Compose configuration when no matching image can be used."""

from unittest.mock import Mock

from lab_ui import paths
from lab_ui.docker import compose, seed_image
from lab_ui.docker.jobs import JobFailed
from tests import Case


class PublishedSeed(Case):
    def setUp(self):
        self.folder(paths, compose, files={
            "seeder/.dockerignore": "tests\n",
            "seeder/Dockerfile": "FROM example.test/demo\n",
            "seeder/requirements.txt": "library==1\n",
            "seeder/seed.py": "",
            "seeder/lab_seeder/__init__.py": "",
        })
        self.patch(compose, "scratch", return_value=self.root)
        self.repository = "ghcr.io/example/lab/seeder"
        self.image = self.repository + ":" + seed_image.tag(self.root)
        self.service = {"build": {"context": str(self.root / "seeder")}, "pull_policy": "build",
                        "labels": {"io.gravitee.role": "seed"}, "image": "ghcr.io/upstream/lab/seeder:latest"}
        self.config = self.patch(compose, "config_json", return_value={"services": {"shop-seed": self.service}})
        self.inspect = self.patch(compose, "run", return_value=(1, "", "not found"))
        self.job = Mock(lines=[], canceled=False)

    def test_runtime_sources_change_the_tag_but_test_files_do_not(self):
        self.write("seeder/tests/test_seed.py", "# tests\n")
        self.assertEqual(self.image, self.repository + ":" + seed_image.tag(self.root))
        self.write("seeder/lab_seeder/__init__.py", "# local modification\n")
        self.assertNotEqual(self.image, self.repository + ":" + seed_image.tag(self.root))

    def test_the_override_is_for_the_selected_seed_and_removes_its_build(self):
        args = seed_image.files(self.job, self.repository, "shop-seed")
        self.assertEqual(args[:-2], ["-f", compose.CORE_FILE, "-f", compose.ON_FILE])
        text = (self.root / args[-1]).read_text()
        self.assertIn(f'image: "{self.image}"', text)
        self.assertIn("build: !reset null", text)
        self.assertIn("pull_policy: missing", text)
        self.job.exec.assert_called_once_with(["docker", "pull", self.image])
        self.assertEqual(self.service["pull_policy"], "build")

    def test_a_custom_build_or_an_explicit_pull_policy_keeps_the_catalog_configuration(self):
        for changes in ({"build": {"context": str(self.root / "custom")}}, {"pull_policy": "never"},
                        {"build": {"context": str(self.root / "seeder"), "args": {"CUSTOM": "true"}}}):
            with self.subTest(changes=changes):
                self.config.return_value = {"services": {"shop-seed": {**self.service, **changes}}}
                self.assertEqual((), seed_image.files(self.job, self.repository, "shop-seed"))
        self.inspect.assert_not_called()
        self.job.exec.assert_not_called()

    def test_pull_failure_preserves_the_local_build_but_cancellation_stops_the_job(self):
        self.job.exec.side_effect = JobFailed("image unavailable")
        self.assertEqual((), seed_image.files(self.job, self.repository, "shop-seed"))
        self.job.canceled = True
        with self.assertRaises(JobFailed):
            seed_image.files(self.job, self.repository, "shop-seed")

    def test_an_already_downloaded_image_is_reused_without_a_pull(self):
        self.inspect.return_value = (0, "sha256:abc", "")
        self.assertTrue(seed_image.files(self.job, self.repository, "shop-seed"))
        self.job.exec.assert_not_called()
