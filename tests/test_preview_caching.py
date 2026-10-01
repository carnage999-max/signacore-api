"""A page the reader already holds should not be drawn again.

Rendering one page of a document costs around half a second of CPU on the production server, and
the portal asks for every page of a document plus a thumbnail of each. A signer reading a long
agreement therefore pays that cost once per page, and again every time they scroll back to
something they have already seen, because the response carried no cache headers at all.

These tests pin the two halves that matter: that a reader who offers the tag they hold is answered
without a render, and that offering a tag never substitutes for being allowed to see the page.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Organization, OrganizationMembership
from apps.documents.models import Document, DocumentField
from apps.signing.models import SigningRequest

from . import pdf_builders as builders
from .test_pdf_form_import import TEST_MEDIA_ROOT

RENDER = "apps.signing.views.fitz.open"


@override_settings(MEDIA_ROOT=TEST_MEDIA_ROOT)
class SignerPreviewCachingTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        self.client = APIClient()
        user = get_user_model().objects.create_user(username="admin", email="a@e.com", password="pw123456")
        self.organization = Organization.objects.create(name="Co", created_by=user)
        self.document = Document.objects.create(
            title="Consent Form",
            original_pdf=SimpleUploadedFile("f.pdf", builders.build_flat_pdf(), content_type="application/pdf"),
            created_by=user,
            organization=self.organization,
            status=Document.StatusEnum.SENT,
        )
        self.signing_request = SigningRequest.objects.create(
            document=self.document,
            signer_email="j@e.com",
            signer_name="Jane",
            expires_at=timezone.now() + timedelta(days=7),
        )
        self.url = f"/api/sign/{self.signing_request.id}/pages/1/preview/"

    def verify(self) -> None:
        with self.settings(SIGNACORE_TEST_OTP_CODE="123456"):
            self.client.post(f"/api/sign/{self.signing_request.id}/otp/send/")
            self.client.post(f"/api/sign/{self.signing_request.id}/otp/verify/", {"otp": "123456"}, format="json")

    def test_a_first_view_returns_the_image_and_names_it(self) -> None:
        self.verify()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertTrue(response["ETag"].startswith('"'))
        self.assertIn("private", response["Cache-Control"])
        self.assertIn("no-cache", response["Cache-Control"])

    def test_a_page_the_reader_already_holds_is_not_drawn_again(self) -> None:
        """The whole point: a 304 must cost no render."""
        self.verify()
        etag = self.client.get(self.url)["ETag"]

        with patch(RENDER, side_effect=AssertionError("the page was rendered again")) as render:
            response = self.client.get(self.url, HTTP_IF_NONE_MATCH=etag)

        self.assertEqual(response.status_code, 304)
        self.assertFalse(response.content)
        render.assert_not_called()

    def test_a_stale_tag_still_gets_a_fresh_image(self) -> None:
        self.verify()

        response = self.client.get(self.url, HTTP_IF_NONE_MATCH='"not-the-tag-you-hold"')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")

    def test_replacing_the_document_changes_the_tag(self) -> None:
        self.verify()
        before = self.client.get(self.url)["ETag"]

        self.document.original_pdf.save("replacement.pdf", SimpleUploadedFile("r.pdf", builders.build_flat_pdf()))
        self.document.save(update_fields=["updated_at"])

        self.assertNotEqual(self.client.get(self.url)["ETag"], before)

    def test_a_thumbnail_and_a_full_page_are_not_the_same_image(self) -> None:
        """Both are page one at the same URL path; only the width differs."""
        self.verify()

        full = self.client.get(self.url)["ETag"]
        thumbnail = self.client.get(self.url, {"width": "150"})["ETag"]

        self.assertNotEqual(full, thumbnail)

    def test_a_tag_is_not_a_way_past_the_session_check(self) -> None:
        """A 304 would otherwise tell an unverified visitor that the page exists and is unchanged."""
        self.verify()
        etag = self.client.get(self.url)["ETag"]
        self.client.cookies.clear()

        response = self.client.get(self.url, HTTP_IF_NONE_MATCH=etag)

        self.assertEqual(response.status_code, 403)


@override_settings(
    MEDIA_ROOT=TEST_MEDIA_ROOT,
    SIGNACORE_SHARED_SECRET="test-signacore-secret",
    SIGNACORE_SERVICE_USERNAME="signacore-service",
)
class AdminPreviewCachingTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="admin", email="admin@mysignacore.com", password="pw123456", is_staff=True
        )
        self.organization = Organization.objects.create(name="Co", created_by=self.user)
        OrganizationMembership.objects.create(
            organization=self.organization,
            user=self.user,
            role=OrganizationMembership.RoleEnum.ADMIN,
        )
        self.client.credentials(
            HTTP_X_SIGNACORE_SECRET=settings.SIGNACORE_SHARED_SECRET,
            HTTP_X_SIGNACORE_ADMIN_ID=str(self.user.id),
            HTTP_X_SIGNACORE_ORGANIZATION_ID=str(self.organization.id),
        )
        self.document = Document.objects.create(
            title="Consent Form",
            original_pdf=SimpleUploadedFile("f.pdf", builders.build_flat_pdf(), content_type="application/pdf"),
            created_by=self.user,
            organization=self.organization,
            status=Document.StatusEnum.DRAFT,
        )
        DocumentField.objects.create(
            document=self.document,
            field_type=DocumentField.FieldTypeEnum.TEXT,
            label="Full name",
            page=1,
            x=72,
            y=620,
            width=180,
            height=24,
            is_required=True,
            detection_source=DocumentField.DetectionSourceEnum.MANUAL,
            order=1,
        )
        self.url = f"/api/admin/documents/{self.document.id}/pages/1/preview/"

    def test_the_editor_is_told_what_it_already_holds(self) -> None:
        first = self.client.get(self.url)

        self.assertEqual(first.status_code, 200)

        with patch("apps.documents.views.fitz.open", side_effect=AssertionError("rendered again")):
            second = self.client.get(self.url, HTTP_IF_NONE_MATCH=first["ETag"])

        self.assertEqual(second.status_code, 304)

    def test_looking_at_a_page_is_audited_even_when_it_is_not_redrawn(self) -> None:
        """Caching must not quietly thin the audit history."""
        from apps.documents.models import AdminAuditLog

        first = self.client.get(self.url)
        before = AdminAuditLog.objects.filter(action=AdminAuditLog.ActionEnum.DOCUMENT_VIEW).count()

        self.client.get(self.url, HTTP_IF_NONE_MATCH=first["ETag"])

        self.assertEqual(
            AdminAuditLog.objects.filter(action=AdminAuditLog.ActionEnum.DOCUMENT_VIEW).count(),
            before + 1,
        )

    def test_editing_a_field_does_not_throw_away_the_rendered_pages(self) -> None:
        """Fields are drawn over the image by the browser, so they are not part of it.

        If they changed the tag, every page of the document would be redrawn each time an
        administrator nudged a field - which is the editor's most common action.
        """
        before = self.client.get(self.url)["ETag"]

        field = self.document.fields.first()
        field.x = field.x + 10
        field.save(update_fields=["x"])

        self.assertEqual(self.client.get(self.url)["ETag"], before)
