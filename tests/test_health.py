from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase


class HealthCheckViewTests(SimpleTestCase):
    def test_health_check_returns_expected_payload(self) -> None:
        response = self.client.get("/api/health/")

        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(
            response.content,
            {"status": "ok", "version": "1.0"},
        )


class AdminAndDocsViewTests(TestCase):
    def setUp(self) -> None:
        self.admin = get_user_model().objects.create_user(
            username="owner",
            email="owner@example.com",
            password="test-password-123",
            is_staff=True,
            is_superuser=True,
        )

    def test_api_docs_page_renders_custom_shell(self) -> None:
        self.client.force_login(self.admin)

        response = self.client.get("/api/docs/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SignaCore API")
        self.assertContains(response, "swagger-ui")

    def test_openapi_schema_is_available_to_staff(self) -> None:
        self.client.force_login(self.admin)

        response = self.client.get("/api/schema/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("application/vnd.oai.openapi", response["content-type"])

    def test_api_docs_redirect_anonymous_users_to_admin_login(self) -> None:
        response = self.client.get("/api/docs/")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_django_admin_uses_default_auth_flow(self) -> None:
        response = self.client.get("/admin/")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])
