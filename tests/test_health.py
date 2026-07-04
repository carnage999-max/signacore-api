from django.test import SimpleTestCase


class HealthCheckViewTests(SimpleTestCase):
    def test_health_check_returns_expected_payload(self) -> None:
        response = self.client.get("/api/health/")

        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(
            response.content,
            {"status": "ok", "version": "1.0"},
        )

