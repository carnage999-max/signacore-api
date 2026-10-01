"""Every task a worker is asked to run must be one it can find.

Publishing a task and running it are separated by the broker, so nothing fails at the point a task
is sent. ``delay()`` succeeds as long as the broker accepts the message; whether any worker knows
the name is discovered later, by the worker, which logs the message and discards it. A scheduled
task can therefore be published every fifteen minutes for weeks while never running once.

That is what happened to ``issue_outstanding_completed_documents``: beat published it, the worker
answered "Received unregistered task of type", and the sweep meant to catch documents that were
signed but never issued had never run outside the test suite.
"""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from signacore_api.celery import app

TASKS_PACKAGE = Path(settings.BASE_DIR) / "tasks"


def registered_tasks() -> set[str]:
    """What a worker would know about, loaded the way a worker loads it."""
    app.loader.import_default_modules()
    return set(app.tasks)


class CeleryTaskRegistrationTests(SimpleTestCase):
    def test_every_scheduled_task_is_registered(self) -> None:
        """The check that would have caught this before a deploy rather than after."""
        known = registered_tasks()

        missing = sorted(
            entry["task"] for entry in settings.CELERY_BEAT_SCHEDULE.values() if entry["task"] not in known
        )

        self.assertEqual(missing, [], "beat would publish these to a worker that cannot run them")

    def test_every_task_module_is_declared(self) -> None:
        """A new module under tasks/ has to be named, since nothing discovers it.

        Listing the modules is what makes registration deliberate. Without this, adding
        ``tasks/billing.py`` would repeat the fault silently - its tasks would publish and vanish.
        """
        on_disk = {f"tasks.{path.stem}" for path in TASKS_PACKAGE.glob("*.py") if not path.stem.startswith("__")}

        self.assertEqual(
            sorted(on_disk - set(settings.CELERY_IMPORTS)),
            [],
            "add the module to CELERY_IMPORTS, or a worker will not find its tasks",
        )

    def test_the_tasks_that_run_on_a_schedule_are_the_ones_we_expect(self) -> None:
        known = registered_tasks()

        for name in ("tasks.signing.expire_signing_links", "tasks.signing.issue_outstanding_completed_documents"):
            with self.subTest(task=name):
                self.assertIn(name, known)

    def test_registration_does_not_depend_on_the_views_importing_a_module(self) -> None:
        """``tasks.notifications`` registered only because the views happen to import it.

        That made every email task dependent on an import in unrelated code. Declaring the module
        means a refactor of the views cannot quietly stop invitations being sent.
        """
        self.assertIn("tasks.notifications", settings.CELERY_IMPORTS)
        self.assertIn("tasks.notifications.send_invitation_email_for_request", registered_tasks())
