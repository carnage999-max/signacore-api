"""Only one container may migrate, and the others have to wait for it.

Every service is built from the same image and so runs the same entrypoint. While all three ran
``migrate`` they raced for the same DDL, and a deploy ended with two of them dead on what the third
had just applied:

    psycopg.errors.DuplicateColumn: column "signed_pdf" of relation "signing_signingrequest"
    already exists

Django takes no lock around a migration run, so nothing but this arrangement prevents it. It is
configuration rather than code, which is exactly the kind of thing that is quietly undone, so it is
asserted here.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from django.test import SimpleTestCase

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPOSITORY_ROOT / "docker-compose.yml"
ENTRYPOINT = REPOSITORY_ROOT / "docker" / "entrypoint.sh"
MIGRATION_FLAG = "SIGNACORE_RUN_MIGRATIONS"


class ComposeMigrationOwnershipTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.services = yaml.safe_load(COMPOSE_FILE.read_text())["services"]

    def test_exactly_one_service_runs_migrations(self) -> None:
        migrating = [
            name
            for name, service in self.services.items()
            if str(service.get("environment", {}).get(MIGRATION_FLAG, "0")) == "1"
        ]

        self.assertEqual(migrating, ["api"], "exactly one service may run migrations")

    def test_every_service_states_its_answer(self) -> None:
        """All three share one env_file, so a value left in it must not decide this."""
        for name, service in self.services.items():
            with self.subTest(service=name):
                self.assertIn(
                    MIGRATION_FLAG,
                    service.get("environment", {}),
                    f"{name} leaves {MIGRATION_FLAG} to whatever the shared .env happens to say",
                )


class EntrypointTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.script = ENTRYPOINT.read_text()

    def test_migrating_is_conditional(self) -> None:
        self.assertIn(f'if [ "${{{MIGRATION_FLAG}:-0}}" = "1" ]', self.script)

    def test_a_container_that_does_not_migrate_waits_for_the_one_that_does(self) -> None:
        """Starting a worker against a schema it was not built for is the thing being avoided."""
        self.assertIn("wait_for_migrations", self.script)
        self.assertIn("migrate --check", self.script)

    def test_waiting_gives_up_rather_than_hanging_forever(self) -> None:
        """A deploy that fails loudly beats one that never finishes and never says why."""
        self.assertIn("SIGNACORE_MIGRATION_WAIT_SECONDS", self.script)
        self.assertIn("refusing to start", self.script)
