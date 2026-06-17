from __future__ import annotations

import tempfile
from datetime import date, timedelta
from pathlib import Path

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import BillingType, Receipt, RegisteredService, Submission


class ReceiptFlowTests(TestCase):
    def setUp(self):
        self.media_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.media_dir.cleanup)
        self.override = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.user = User.objects.create_user(username="alice", password="password123")
        self.service = RegisteredService.objects.create(
            user=self.user,
            name="OpenAI API",
            billing_type=BillingType.METERED,
        )

    def test_user_can_upload_and_submit(self):
        self.client.login(username="alice", password="password123")
        upload = SimpleUploadedFile("receipt.pdf", b"%PDF-1.4 test", content_type="application/pdf")
        response = self.client.post(
            reverse("dashboard") + "?month=2026-06",
            {
                "action": "add_receipt",
                "service": self.service.id,
                "amount": "1200",
                "currency": "JPY",
                "file": upload,
            },
        )
        self.assertEqual(response.status_code, 302)
        submission = Submission.objects.get(user=self.user, period_month=date(2026, 6, 1))
        self.assertEqual(submission.receipts.count(), 1)

        response = self.client.post(reverse("dashboard") + "?month=2026-06", {"action": "submit"})
        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertTrue(submission.is_submitted)

    def test_purge_expired_receipts_deletes_file_but_keeps_metadata(self):
        submission = Submission.objects.create(user=self.user, period_month=date(2026, 6, 1))
        upload = SimpleUploadedFile("receipt.pdf", b"%PDF-1.4 test", content_type="application/pdf")
        receipt = Receipt.objects.create(
            submission=submission,
            service=self.service,
            service_name_snapshot=self.service.name,
            billing_type_snapshot=self.service.billing_type,
            original_filename="receipt.pdf",
            file=upload,
            expires_at=timezone.now() - timedelta(days=1),
        )
        path = Path(receipt.file.path)
        self.assertTrue(path.exists())

        call_command("purge_expired_receipts", "--noinput")

        receipt.refresh_from_db()
        self.assertFalse(path.exists())
        self.assertFalse(receipt.file_available)
        self.assertEqual(receipt.service_name_snapshot, "OpenAI API")


class HealthcheckTests(TestCase):
    def test_healthcheck_returns_200_for_railway_healthcheck_host(self):
        response = self.client.get("/health/", HTTP_HOST="healthcheck.railway.app")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    @override_settings(DEBUG=False, SECURE_SSL_REDIRECT=True, SECURE_REDIRECT_EXEMPT=[r"^health/$"])
    def test_healthcheck_is_not_redirected_when_ssl_redirect_is_enabled(self):
        response = self.client.get("/health/", HTTP_HOST="healthcheck.railway.app", secure=False)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")


class SignupSettingsTests(TestCase):
    @override_settings(ALLOW_SIGNUP=True)
    def test_register_page_is_available_when_signup_is_enabled(self):
        response = self.client.get(reverse("register"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ユーザー登録")

    @override_settings(ALLOW_SIGNUP=False)
    def test_register_page_redirects_to_login_when_signup_is_disabled(self):
        response = self.client.get(reverse("register"), follow=True)

        self.assertRedirects(response, reverse("login"))
        self.assertContains(response, "ユーザー登録は現在無効です")
        self.assertContains(response, "管理者に作成を依頼")


class StaffUserProvisioningTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="admin",
            email="admin@example.com",
            password="admin-password-123",
        )
        self.client.login(username="admin", password="admin-password-123")

    def test_staff_can_create_email_user_with_random_initial_password(self):
        response = self.client.post(reverse("staff_user_create"), {"email": "NEW.USER@example.COM"})

        self.assertEqual(response.status_code, 200)
        user = User.objects.get(username="new.user@example.com")
        self.assertEqual(user.email, "new.user@example.com")
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertTrue(user.profile.must_change_password)
        self.assertEqual(user.profile.created_by, self.admin)

        generated_password = response.context["generated_password"]
        self.assertIsNotNone(generated_password)
        self.assertGreaterEqual(len(generated_password), 16)
        self.assertTrue(user.check_password(generated_password))
        self.assertContains(response, "new.user@example.com")
        self.assertContains(response, generated_password)

    def test_staff_user_create_rejects_duplicate_email(self):
        User.objects.create_user(username="existing@example.com", email="existing@example.com", password="password123")
        response = self.client.post(reverse("staff_user_create"), {"email": "EXISTING@example.com"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "このメールアドレスはすでに登録されています")
        self.assertEqual(User.objects.filter(username__iexact="existing@example.com").count(), 1)

    def test_non_staff_cannot_access_staff_user_create(self):
        self.client.logout()
        user = User.objects.create_user(username="user@example.com", email="user@example.com", password="password123")
        self.client.login(username=user.username, password="password123")

        response = self.client.get(reverse("staff_user_create"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])


class ForcedPasswordChangeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="user@example.com",
            email="user@example.com",
            password="InitialPassword123",
        )
        self.user.profile.must_change_password = True
        self.user.profile.save(update_fields=["must_change_password", "updated_at"])

    def test_user_must_change_initial_password_before_using_app(self):
        self.client.login(username="user@example.com", password="InitialPassword123")

        response = self.client.get(reverse("dashboard"))
        self.assertRedirects(response, reverse("password_change"))

        response = self.client.post(
            reverse("password_change"),
            {
                "old_password": "InitialPassword123",
                "new_password1": "ChangedPassword12345",
                "new_password2": "ChangedPassword12345",
            },
        )
        self.assertRedirects(response, reverse("password_change_done"))

        self.user.refresh_from_db()
        self.user.profile.refresh_from_db()
        self.assertFalse(self.user.profile.must_change_password)
        self.assertTrue(self.user.check_password("ChangedPassword12345"))

        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)

    def test_email_login_is_case_insensitive_for_email_accounts(self):
        response = self.client.post(
            reverse("login"),
            {"username": "USER@EXAMPLE.COM", "password": "InitialPassword123"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("dashboard"))
