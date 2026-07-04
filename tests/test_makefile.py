from pathlib import Path
from unittest import TestCase


class MakefileTests(TestCase):
    def test_makefile_defines_signacore_workflow_targets(self) -> None:
        makefile_path = Path(__file__).resolve().parent.parent / "Makefile"

        self.assertTrue(makefile_path.exists(), "Makefile should exist at the Signacore API root.")

        contents = makefile_path.read_text()

        for target in (
            "help:",
            "makemigrations:",
            "migrate:",
            "collectstatic:",
            "test:",
            "up: docker-up",
            "down: docker-down",
            "restart: docker-restart",
            "destroy: docker-destroy",
            "build: docker-build",
            "build-no-cache: docker-build-no-cache",
            "docker-up:",
            "docker-down:",
            "docker-restart:",
            "docker-destroy:",
            "docker-build:",
            "docker-build-no-cache:",
        ):
            self.assertIn(target, contents)
