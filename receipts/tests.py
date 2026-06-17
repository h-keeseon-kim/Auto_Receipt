from __future__ import annotations

import tempfile
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock
from pathlib import Path

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .ai_filename import ReceiptFilenameResult, build_result_from_ai_payload
from .forms import ReceiptUploadForm
from .models import (
    BillingType,
    Receipt,
    ReceiptFilenameStatus,
    ReceiptPeriodCheckStatus,
    ReceiptResubmissionRequest,
    RegisteredService,
    ResubmissionRequestStatus,
    ServiceCatalog,
    ServiceDeactivationSource,
    ServiceRegistrationSource,
    Submission,
    SubmissionStatus,
)


FAST_PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


@override_settings(PASSWORD_HASHERS=FAST_PASSWORD_HASHERS)
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

    def test_home_redirects_user_to_service_management(self):
        self.client.login(username="alice", password="password123")

        response = self.client.get(reverse("home"))

        self.assertRedirects(response, reverse("user_services"))

    def test_dashboard_auto_upload_form_has_no_visible_upload_button(self):
        self.client.login(username="alice", password="password123")

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-auto-upload-form")
        self.assertContains(response, "data-file-upload-field hidden")
        self.assertNotContains(response, ">アップロード</button>")


    def test_ai_payload_uses_receipt_payee_for_filename_not_selected_service_name(self):
        result = build_result_from_ai_payload(
            {
                "card_last4": "7210",
                "card_ends_with_7210": True,
                "payee": "OpenAI",
                "payment_date": "2026-06-19",
                "amount": "220.00",
                "currency": "USD",
                "confidence": "high",
                "can_generate_filename": True,
                "admin_memo": "",
            },
            original_filename="chatgpt-subscription.pdf",
        )

        self.assertEqual(result.status, ReceiptFilenameStatus.GENERATED)
        self.assertEqual(result.suggested_filename, "260619_金_OpenAI_220_USD.pdf")
        self.assertEqual(result.payee, "OpenAI")

    def test_ai_payload_marks_unrelated_service_payee_for_review(self):
        result = build_result_from_ai_payload(
            {
                "card_last4": "7210",
                "card_last4_matches_target": True,
                "payee": "Anthropic",
                "service_payee_related": False,
                "service_payee_relation_reason": "登録サービスはChatGPTだが、領収書の払先はAnthropic。",
                "payment_date": "2026-06-19",
                "amount": "220.00",
                "currency": "USD",
                "confidence": 0.96,
                "can_create_filename": False,
                "reason": "サービスと払先の組み合わせが不一致です。",
            },
            original_filename="chatgpt.pdf",
        )

        self.assertEqual(result.status, ReceiptFilenameStatus.NEEDS_REVIEW)
        self.assertEqual(result.payee, "Anthropic")
        self.assertIs(result.service_payee_related, False)
        self.assertEqual(result.suggested_filename, "")
        self.assertIn("関連していない可能性", result.admin_memo)

    @mock.patch("receipts.ai_processing.generate_ai_receipt_filename")
    def test_user_upload_saves_pending_ai_receipt_and_command_processes_later(self, mocked_generate):
        mocked_generate.return_value = ReceiptFilenameResult(
            status=ReceiptFilenameStatus.GENERATED,
            suggested_filename="260619_金_OpenAI_220_USD.pdf",
            payee="OpenAI",
            payment_date=date(2026, 6, 19),
            amount=Decimal("220.00"),
            currency="USD",
            card_last4="7210",
        )
        self.client.login(username="alice", password="password123")

        response = self.client.post(
            reverse("dashboard") + "?month=2026-06",
            {
                "action": "add_receipt",
                "service": self.service.id,
                "file": SimpleUploadedFile("raw.pdf", b"%PDF-1.4 test", content_type="application/pdf"),
            },
        )

        self.assertEqual(response.status_code, 302)
        mocked_generate.assert_not_called()
        receipt = Receipt.objects.get()
        self.assertEqual(receipt.original_filename, "raw.pdf")
        self.assertEqual(receipt.generated_filename, "")
        self.assertEqual(receipt.display_filename, "raw.pdf")
        self.assertEqual(receipt.ai_filename_status, ReceiptFilenameStatus.NOT_PROCESSED)
        self.assertEqual(receipt.ai_period_check_status, ReceiptPeriodCheckStatus.NOT_CHECKED)
        self.assertIsNone(receipt.amount)
        self.assertIsNone(receipt.issued_on)

        call_command("process_pending_receipts", "--limit", "10")
        receipt.refresh_from_db()
        self.assertEqual(receipt.generated_filename, "260619_金_OpenAI_220_USD.pdf")
        self.assertEqual(receipt.display_filename, "260619_金_OpenAI_220_USD.pdf")
        self.assertEqual(receipt.ai_filename_status, ReceiptFilenameStatus.GENERATED)
        self.assertEqual(receipt.ai_extracted_payee, "OpenAI")
        self.assertEqual(receipt.ai_extracted_card_last4, "7210")
        self.assertEqual(receipt.issued_on, date(2026, 6, 19))
        self.assertEqual(receipt.amount, Decimal("220.00"))
        self.assertEqual(receipt.currency, "USD")
        self.assertEqual(receipt.ai_receipt_month, "2026-06")
        self.assertEqual(receipt.ai_period_check_status, ReceiptPeriodCheckStatus.MATCHED)
        mocked_generate.assert_called_once()

    @mock.patch("receipts.ai_processing.generate_ai_receipt_filename")
    def test_background_ai_period_mismatch_keeps_receipt_and_records_admin_memo(self, mocked_generate):
        mocked_generate.return_value = ReceiptFilenameResult(
            status=ReceiptFilenameStatus.GENERATED,
            suggested_filename="260531_日_OpenAI_220_USD.pdf",
            payee="OpenAI",
            payment_date=date(2026, 5, 31),
            amount=Decimal("220.00"),
            currency="USD",
            card_last4="7210",
        )
        self.client.login(username="alice", password="password123")

        response = self.client.post(
            reverse("dashboard") + "?month=2026-06",
            {
                "action": "add_receipt",
                "service": self.service.id,
                "file": SimpleUploadedFile("may.pdf", b"%PDF-1.4 may", content_type="application/pdf"),
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Receipt.objects.count(), 1)
        self.assertNotContains(response, "提出月（2026年06月）ではなく 2026年05月")
        self.assertContains(response, "AI確認は裏側で実行")

        call_command("process_pending_receipts", "--limit", "10")
        receipt = Receipt.objects.get()
        self.assertEqual(receipt.ai_period_check_status, ReceiptPeriodCheckStatus.MISMATCHED)
        self.assertEqual(receipt.ai_receipt_month, "2026-05")
        self.assertIn("ユーザーへ再アップロードを依頼してください", receipt.ai_period_check_memo)

        admin = User.objects.create_superuser(username="admin", email="admin@example.com", password="admin-password-123")
        self.client.logout()
        self.client.login(username="admin", password="admin-password-123")
        staff_response = self.client.get(reverse("history") + "?month=2026-06")
        self.assertContains(staff_response, "提出月不一致")
        self.assertContains(staff_response, "判定月: 2026-05")
        self.assertContains(staff_response, "ユーザーへ再アップロード")

    @mock.patch("receipts.ai_processing.generate_ai_receipt_filename")
    def test_ai_filename_review_memo_is_visible_to_staff_only(self, mocked_generate):
        mocked_generate.return_value = ReceiptFilenameResult(
            status=ReceiptFilenameStatus.NEEDS_REVIEW,
            admin_memo="カード下4桁が7210として確認できませんでした。",
            payee="OpenAI",
            card_last4="1234",
        )
        self.client.login(username="alice", password="password123")
        self.client.post(
            reverse("dashboard") + "?month=2026-06",
            {
                "action": "add_receipt",
                "service": self.service.id,
                "file": SimpleUploadedFile("unclear.pdf", b"%PDF-1.4 unclear", content_type="application/pdf"),
            },
        )

        receipt = Receipt.objects.get()
        self.assertEqual(receipt.ai_filename_status, ReceiptFilenameStatus.NOT_PROCESSED)
        self.assertEqual(receipt.display_filename, "unclear.pdf")

        call_command("process_pending_receipts", "--limit", "10")
        receipt.refresh_from_db()
        self.assertEqual(receipt.ai_filename_status, ReceiptFilenameStatus.NEEDS_REVIEW)
        self.assertEqual(receipt.display_filename, "unclear.pdf")

        user_response = self.client.get(reverse("dashboard") + "?month=2026-06")
        self.assertNotContains(user_response, "カード下4桁が7210として確認できませんでした")
        self.assertNotContains(user_response, "AI:")

        admin = User.objects.create_superuser(username="admin", email="admin@example.com", password="admin-password-123")
        self.client.logout()
        self.client.login(username="admin", password="admin-password-123")
        staff_response = self.client.get(reverse("history") + "?month=2026-06")
        self.assertContains(staff_response, "AI要確認")
        self.assertContains(staff_response, "カード下4桁が7210として確認できませんでした")
        self.assertContains(staff_response, "OpenAI")

    @mock.patch("receipts.ai_processing.generate_ai_receipt_filename")
    def test_staff_can_request_resubmission_for_failed_checklist_item(self, mocked_generate):
        self.service.name = "ChatGPT"
        self.service.billing_type = BillingType.SUBSCRIPTION
        self.service.save(update_fields=["name", "billing_type", "updated_at"])
        mocked_generate.return_value = ReceiptFilenameResult(
            status=ReceiptFilenameStatus.NEEDS_REVIEW,
            admin_memo="登録サービス名と領収書の払先が関連していない可能性があります。",
            payee="Anthropic",
            payment_date=date(2026, 6, 19),
            amount=Decimal("220.00"),
            currency="USD",
            card_last4="7210",
            card_last4_matches_target=True,
            service_payee_related=False,
            service_payee_relation_reason="ChatGPTの想定払先はOpenAIだが、領収書の払先はAnthropic。",
        )
        self.client.login(username="alice", password="password123")
        self.client.post(
            reverse("dashboard") + "?month=2026-06",
            {
                "action": "add_receipt",
                "service": self.service.id,
                "file": SimpleUploadedFile("wrong-payee.pdf", b"%PDF-1.4 wrong", content_type="application/pdf"),
            },
        )
        self.client.post(reverse("dashboard") + "?month=2026-06", {"action": "submit"})
        submission = Submission.objects.get(user=self.user, period_month=date(2026, 6, 1))
        self.assertTrue(submission.is_submitted)

        call_command("process_pending_receipts", "--limit", "10")
        receipt = Receipt.objects.get()
        receipt_path = Path(receipt.file.path)
        self.assertTrue(receipt_path.exists())
        self.assertTrue(receipt.ai_check_card_last4)
        self.assertTrue(receipt.ai_check_payee)
        self.assertFalse(receipt.ai_check_service_payee_related)
        self.assertTrue(receipt.ai_check_date)
        self.assertTrue(receipt.ai_check_amount)
        self.assertTrue(receipt.ai_check_currency)
        self.assertTrue(receipt.ai_check_period_match)
        self.assertTrue(receipt.needs_manual_review)

        admin = User.objects.create_superuser(username="admin", email="admin@example.com", password="admin-password-123")
        self.client.logout()
        self.client.login(username="admin", password="admin-password-123")
        staff_response = self.client.get(reverse("history") + "?month=2026-06")
        self.assertContains(staff_response, "manual-review-row")
        self.assertContains(staff_response, "サービス/払先要確認")
        self.assertContains(staff_response, "再提出指示")
        self.assertContains(staff_response, "Anthropic")

        response = self.client.post(
            reverse("staff_request_receipt_resubmission", args=[receipt.pk]),
            {"next": reverse("history") + "?month=2026-06"},
        )

        self.assertRedirects(response, reverse("history") + "?month=2026-06")
        self.assertFalse(Receipt.objects.filter(pk=receipt.pk).exists())
        self.assertFalse(receipt_path.exists())
        request_item = ReceiptResubmissionRequest.objects.get()
        self.assertEqual(request_item.status, ResubmissionRequestStatus.OPEN)
        self.assertEqual(request_item.user, self.user)
        self.assertEqual(request_item.service_name_snapshot, "ChatGPT")
        submission.refresh_from_db()
        self.assertFalse(submission.is_submitted)

        self.client.logout()
        self.client.login(username="alice", password="password123")
        response = self.client.get(reverse("dashboard") + "?month=2026-06")
        self.assertContains(response, "再提出依頼があります")
        self.assertContains(response, "ChatGPT")

        response = self.client.post(
            reverse("dashboard") + "?month=2026-06",
            {
                "action": "add_receipt",
                "service": self.service.id,
                "file": SimpleUploadedFile("correct.pdf", b"%PDF-1.4 correct", content_type="application/pdf"),
            },
        )
        self.assertEqual(response.status_code, 302)
        request_item.refresh_from_db()
        self.assertEqual(request_item.status, ResubmissionRequestStatus.RESOLVED)
        self.assertEqual(request_item.resolved_by, self.user)
        self.assertEqual(Receipt.objects.count(), 1)

    @mock.patch("receipts.ai_processing.generate_ai_receipt_filename")
    def test_user_replace_receipt_file_marks_ai_pending_and_command_processes_later(self, mocked_generate):
        mocked_generate.return_value = ReceiptFilenameResult(
            status=ReceiptFilenameStatus.GENERATED,
            suggested_filename="260619_金_OpenAI_220_USD.pdf",
            payee="OpenAI",
            payment_date=date(2026, 6, 19),
            amount=Decimal("220.00"),
            currency="USD",
            card_last4="7210",
        )
        self.client.login(username="alice", password="password123")
        submission = Submission.objects.create(
            user=self.user,
            period_month=date(2026, 6, 1),
            status="submitted",
            submitted_at=timezone.now(),
        )
        receipt = Receipt.objects.create(
            submission=submission,
            service=self.service,
            service_name_snapshot=self.service.name,
            billing_type_snapshot=self.service.billing_type,
            original_filename="wrong.pdf",
            generated_filename="old-ai.pdf",
            ai_filename_status=ReceiptFilenameStatus.GENERATED,
            ai_period_check_status=ReceiptPeriodCheckStatus.MATCHED,
            ai_receipt_month="2026-06",
            amount=Decimal("100.00"),
            currency="USD",
            issued_on=date(2026, 6, 1),
            file=SimpleUploadedFile("wrong.pdf", b"%PDF-1.4 wrong", content_type="application/pdf"),
            expires_at=timezone.now() + timedelta(days=30),
        )

        response = self.client.post(
            reverse("replace_receipt_file", args=[receipt.pk]),
            {
                "file": SimpleUploadedFile("correct.pdf", b"%PDF-1.4 correct", content_type="application/pdf"),
                "next": reverse("submission_detail", args=[submission.pk]),
            },
        )

        self.assertRedirects(response, reverse("submission_detail", args=[submission.pk]))
        mocked_generate.assert_not_called()
        receipt.refresh_from_db()
        self.assertEqual(receipt.original_filename, "correct.pdf")
        self.assertEqual(receipt.generated_filename, "")
        self.assertEqual(receipt.display_filename, "correct.pdf")
        self.assertEqual(receipt.ai_filename_status, ReceiptFilenameStatus.NOT_PROCESSED)
        self.assertEqual(receipt.ai_period_check_status, ReceiptPeriodCheckStatus.NOT_CHECKED)
        self.assertIsNone(receipt.amount)
        self.assertIsNone(receipt.issued_on)

        call_command("process_pending_receipts", "--limit", "10")
        receipt.refresh_from_db()
        self.assertEqual(receipt.generated_filename, "260619_金_OpenAI_220_USD.pdf")
        self.assertEqual(receipt.ai_period_check_status, ReceiptPeriodCheckStatus.MATCHED)
        mocked_generate.assert_called_once()

    @mock.patch("receipts.ai_processing.generate_ai_receipt_filename")
    def test_user_replace_receipt_file_keeps_new_file_even_if_background_detects_different_month(self, mocked_generate):
        mocked_generate.return_value = ReceiptFilenameResult(
            status=ReceiptFilenameStatus.GENERATED,
            suggested_filename="260531_日_OpenAI_220_USD.pdf",
            payee="OpenAI",
            payment_date=date(2026, 5, 31),
            amount=Decimal("220.00"),
            currency="USD",
            card_last4="7210",
        )
        self.client.login(username="alice", password="password123")
        submission = Submission.objects.create(
            user=self.user,
            period_month=date(2026, 6, 1),
            status="submitted",
            submitted_at=timezone.now(),
        )
        receipt = Receipt.objects.create(
            submission=submission,
            service=self.service,
            service_name_snapshot=self.service.name,
            billing_type_snapshot=self.service.billing_type,
            original_filename="correct-june.pdf",
            file=SimpleUploadedFile("correct-june.pdf", b"%PDF-1.4 june", content_type="application/pdf"),
            expires_at=timezone.now() + timedelta(days=30),
            ai_period_check_status=ReceiptPeriodCheckStatus.MATCHED,
            ai_receipt_month="2026-06",
        )
        old_file_name = receipt.file.name

        response = self.client.post(
            reverse("replace_receipt_file", args=[receipt.pk]),
            {
                "file": SimpleUploadedFile("wrong-may.pdf", b"%PDF-1.4 may", content_type="application/pdf"),
                "next": reverse("submission_detail", args=[submission.pk]),
            },
        )

        self.assertRedirects(response, reverse("submission_detail", args=[submission.pk]))
        receipt.refresh_from_db()
        self.assertEqual(receipt.original_filename, "wrong-may.pdf")
        self.assertNotEqual(receipt.file.name, old_file_name)
        self.assertEqual(receipt.ai_period_check_status, ReceiptPeriodCheckStatus.NOT_CHECKED)
        with receipt.file.open("rb") as fp:
            self.assertEqual(fp.read(), b"%PDF-1.4 may")

        call_command("process_pending_receipts", "--limit", "10")
        receipt.refresh_from_db()
        self.assertEqual(receipt.ai_period_check_status, ReceiptPeriodCheckStatus.MISMATCHED)
        self.assertEqual(receipt.ai_receipt_month, "2026-05")
        self.assertIn("ユーザーへ再アップロードを依頼してください", receipt.ai_period_check_memo)

    def test_submission_submit_allows_persisted_period_mismatch_for_staff_follow_up(self):
        submission = Submission.objects.create(user=self.user, period_month=date(2026, 6, 1))
        Receipt.objects.create(
            submission=submission,
            service=self.service,
            service_name_snapshot=self.service.name,
            billing_type_snapshot=self.service.billing_type,
            original_filename="may.pdf",
            file=SimpleUploadedFile("may.pdf", b"%PDF-1.4 may", content_type="application/pdf"),
            expires_at=timezone.now() + timedelta(days=30),
            ai_period_check_status=ReceiptPeriodCheckStatus.MISMATCHED,
            ai_receipt_month="2026-05",
        )

        submission.submit()
        submission.refresh_from_db()
        self.assertTrue(submission.is_submitted)

    def test_ai_payload_flags_service_payee_mismatch_for_staff_review(self):
        result = build_result_from_ai_payload(
            {
                "card_last4": "7210",
                "card_last4_matches_target": True,
                "payee": "Anthropic",
                "service_payee_related": False,
                "service_payee_relation_reason": "登録サービス ChatGPT に対して払先が Anthropic です。",
                "payment_date": "2026-06-19",
                "amount": "220.00",
                "currency": "USD",
                "confidence": 0.95,
                "can_create_filename": True,
                "reason": "",
            },
            original_filename="chatgpt.pdf",
        )

        self.assertEqual(result.status, ReceiptFilenameStatus.NEEDS_REVIEW)
        self.assertFalse(result.service_payee_related)
        self.assertIn("払先が関連していない", result.admin_memo)
        self.assertIn("Anthropic", result.admin_memo)

    @mock.patch("receipts.ai_processing.generate_ai_receipt_filename")
    def test_staff_history_shows_checkboxes_and_highlights_service_payee_mismatch(self, mocked_generate):
        self.service.name = "ChatGPT"
        self.service.billing_type = BillingType.SUBSCRIPTION
        self.service.save(update_fields=["name", "billing_type", "updated_at"])
        mocked_generate.return_value = ReceiptFilenameResult(
            status=ReceiptFilenameStatus.NEEDS_REVIEW,
            admin_memo="登録サービス名と払先の関連性を確認してください。",
            payee="Anthropic",
            payment_date=date(2026, 6, 19),
            amount=Decimal("220.00"),
            currency="USD",
            card_last4="7210",
            card_last4_matches_target=True,
            payee_confirmed=True,
            date_confirmed=True,
            amount_confirmed=True,
            currency_confirmed=True,
            service_payee_related=False,
            service_payee_relation_reason="ChatGPT の領収書として Anthropic は関連なしです。",
        )
        self.client.login(username="alice", password="password123")
        self.client.post(
            reverse("dashboard") + "?month=2026-06",
            {
                "action": "add_receipt",
                "service": self.service.id,
                "file": SimpleUploadedFile("chatgpt.pdf", b"%PDF-1.4 test", content_type="application/pdf"),
            },
        )
        call_command("process_pending_receipts", "--limit", "10")
        receipt = Receipt.objects.get()
        self.assertTrue(receipt.ai_check_card_last4)
        self.assertTrue(receipt.ai_check_payee)
        self.assertFalse(receipt.ai_check_service_payee_related)
        self.assertTrue(receipt.ai_check_date)
        self.assertTrue(receipt.ai_check_amount)
        self.assertTrue(receipt.ai_check_currency)
        self.assertTrue(receipt.ai_check_period_match)
        self.assertTrue(receipt.needs_manual_review)

        admin = User.objects.create_superuser(username="admin", email="admin@example.com", password="admin-password-123")
        self.client.logout()
        self.client.login(username="admin", password="admin-password-123")
        response = self.client.get(reverse("history") + "?month=2026-06")

        self.assertContains(response, "manual-review-row")
        self.assertContains(response, "サービス/払先関連")
        self.assertContains(response, "再提出指示")
        self.assertContains(response, "ChatGPT の領収書として Anthropic")

    def test_staff_resubmission_request_deletes_receipt_keeps_draft_and_user_can_reupload(self):
        admin = User.objects.create_superuser(username="admin", email="admin@example.com", password="admin-password-123")
        submission = Submission.objects.create(
            user=self.user,
            period_month=date(2026, 6, 1),
            status=SubmissionStatus.SUBMITTED,
            submitted_at=timezone.now(),
        )
        receipt = Receipt.objects.create(
            submission=submission,
            service=self.service,
            service_name_snapshot=self.service.name,
            billing_type_snapshot=self.service.billing_type,
            original_filename="wrong.pdf",
            file=SimpleUploadedFile("wrong.pdf", b"%PDF-1.4 wrong", content_type="application/pdf"),
            expires_at=timezone.now() + timedelta(days=30),
            ai_filename_checked_at=timezone.now(),
            ai_check_card_last4=True,
            ai_check_payee=True,
            ai_check_date=True,
            ai_check_amount=True,
            ai_check_currency=True,
            ai_check_period_match=True,
            ai_check_service_payee_related=False,
            ai_service_payee_check_memo="サービスと払先が一致しません。",
        )
        receipt_path = Path(receipt.file.path)
        self.assertTrue(receipt_path.exists())

        self.client.login(username="admin", password="admin-password-123")
        response = self.client.post(
            reverse("staff_request_receipt_resubmission", args=[receipt.pk]),
            {"next": reverse("history") + "?month=2026-06"},
        )

        self.assertRedirects(response, reverse("history") + "?month=2026-06")
        self.assertFalse(Receipt.objects.filter(pk=receipt.pk).exists())
        self.assertFalse(receipt_path.exists())
        submission.refresh_from_db()
        self.assertEqual(submission.status, SubmissionStatus.DRAFT)
        self.assertIsNone(submission.submitted_at)
        request_item = ReceiptResubmissionRequest.objects.get(user=self.user, period_month=date(2026, 6, 1))
        self.assertEqual(request_item.status, ResubmissionRequestStatus.OPEN)
        self.assertEqual(request_item.service_name_snapshot, self.service.name)

        self.client.logout()
        self.client.login(username="alice", password="password123")
        dashboard_response = self.client.get(reverse("dashboard") + "?month=2026-06")
        self.assertContains(dashboard_response, "再提出依頼があります")
        self.assertContains(dashboard_response, self.service.display_name)

        upload_response = self.client.post(
            reverse("dashboard") + "?month=2026-06",
            {
                "action": "add_receipt",
                "service": self.service.id,
                "file": SimpleUploadedFile("correct.pdf", b"%PDF-1.4 correct", content_type="application/pdf"),
            },
        )
        self.assertEqual(upload_response.status_code, 302)
        request_item.refresh_from_db()
        self.assertEqual(request_item.status, ResubmissionRequestStatus.RESOLVED)
        self.assertEqual(Receipt.objects.count(), 1)

    def test_user_can_upload_and_submit(self):
        self.client.login(username="alice", password="password123")
        upload = SimpleUploadedFile("receipt.pdf", b"%PDF-1.4 test", content_type="application/pdf")
        response = self.client.post(
            reverse("dashboard") + "?month=2026-06",
            {
                "action": "add_receipt",
                "service": self.service.id,
                "file": upload,
            },
        )
        self.assertEqual(response.status_code, 302)
        submission = Submission.objects.get(user=self.user, period_month=date(2026, 6, 1))
        self.assertEqual(submission.receipts.count(), 1)
        receipt = submission.receipts.get()
        self.assertIsNone(receipt.amount)
        self.assertIsNone(receipt.issued_on)
        self.assertEqual(receipt.memo, "")

        response = self.client.post(reverse("dashboard") + "?month=2026-06", {"action": "submit"})
        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertTrue(submission.is_submitted)

    def test_user_can_upload_multiple_receipts_before_submit(self):
        self.client.login(username="alice", password="password123")

        for filename in ["receipt-a.pdf", "receipt-b.pdf"]:
            upload = SimpleUploadedFile(filename, b"%PDF-1.4 test", content_type="application/pdf")
            response = self.client.post(
                reverse("dashboard") + "?month=2026-06",
                {
                    "action": "add_receipt",
                    "service": self.service.id,
                    "file": upload,
                },
            )
            self.assertEqual(response.status_code, 302)

        submission = Submission.objects.get(user=self.user, period_month=date(2026, 6, 1))
        self.assertEqual(submission.receipts.count(), 2)
        self.assertEqual(
            list(submission.receipts.order_by("original_filename").values_list("original_filename", flat=True)),
            ["receipt-a.pdf", "receipt-b.pdf"],
        )

    def test_user_can_replace_receipt_file_after_submit(self):
        self.client.login(username="alice", password="password123")
        submission = Submission.objects.create(
            user=self.user,
            period_month=date(2026, 6, 1),
            status="submitted",
            submitted_at=timezone.now(),
        )
        receipt = Receipt.objects.create(
            submission=submission,
            service=self.service,
            service_name_snapshot=self.service.name,
            billing_type_snapshot=self.service.billing_type,
            original_filename="wrong.pdf",
            file=SimpleUploadedFile("wrong.pdf", b"%PDF-1.4 wrong", content_type="application/pdf"),
            expires_at=timezone.now() + timedelta(days=30),
        )
        old_path = Path(receipt.file.path)
        self.assertTrue(old_path.exists())

        response = self.client.post(
            reverse("replace_receipt_file", args=[receipt.pk]),
            {
                "file": SimpleUploadedFile("correct.pdf", b"%PDF-1.4 correct", content_type="application/pdf"),
                "next": reverse("submission_detail", args=[submission.pk]),
            },
        )

        self.assertRedirects(response, reverse("submission_detail", args=[submission.pk]))
        receipt.refresh_from_db()
        self.assertEqual(receipt.original_filename, "correct.pdf")
        self.assertTrue(receipt.file_available)
        self.assertIsNone(receipt.file_deleted_at)
        self.assertFalse(old_path.exists())
        with receipt.file.open("rb") as fp:
            self.assertEqual(fp.read(), b"%PDF-1.4 correct")

    def test_receipt_upload_form_has_only_service_and_file_fields(self):
        form = ReceiptUploadForm(user=self.user, period_month=date(2026, 6, 1))

        self.assertEqual(list(form.fields), ["service", "file"])
        self.assertEqual(form.fields["service"].label, "サービス選択（登録サービス）")
        self.assertEqual(form.fields["file"].label, "領収書ファイルアップロード")
        self.assertTrue(form.fields["file"].required)

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


@override_settings(PASSWORD_HASHERS=FAST_PASSWORD_HASHERS)
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


@override_settings(PASSWORD_HASHERS=FAST_PASSWORD_HASHERS)
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


@override_settings(PASSWORD_HASHERS=FAST_PASSWORD_HASHERS)
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
        self.assertEqual(response["Location"], reverse("home"))


@override_settings(PASSWORD_HASHERS=FAST_PASSWORD_HASHERS)
class StaffServiceAssignmentTests(TestCase):
    def setUp(self):
        self.media_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.media_dir.cleanup)
        self.override = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.admin = User.objects.create_superuser(
            username="admin",
            email="admin@example.com",
            password="admin-password-123",
        )
        self.user = User.objects.create_user(
            username="user@example.com",
            email="user@example.com",
            password="password123",
        )
        self.other_user = User.objects.create_user(
            username="other@example.com",
            email="other@example.com",
            password="password123",
        )
        self.catalog = ServiceCatalog.objects.create(
            name="OpenAI API",
            billing_type=BillingType.METERED,
            created_by=self.admin,
        )

    def test_home_redirects_staff_to_history(self):
        self.client.login(username="admin", password="admin-password-123")

        response = self.client.get(reverse("home"))

        self.assertRedirects(response, reverse("history"))

    def test_staff_has_history_page_instead_of_upload_or_admin_page(self):
        self.client.login(username="admin", password="admin-password-123")

        dashboard_response = self.client.get(reverse("dashboard"))
        self.assertRedirects(dashboard_response, reverse("history"))

        response = self.client.get(reverse("history"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "提出履歴")
        self.assertNotContains(response, ">アップロード</a>")
        self.assertNotContains(response, ">管理者</a>")
        self.assertNotContains(response, "下書き含むZIP")

    def test_staff_history_lists_receipts_by_upload_date_and_delete_removes_user_history(self):
        user_service = RegisteredService.objects.create(
            user=self.user,
            catalog_service=self.catalog,
            name=self.catalog.name,
            billing_type=self.catalog.billing_type,
        )
        other_service = RegisteredService.objects.create(
            user=self.other_user,
            name="Notion",
            billing_type=BillingType.SUBSCRIPTION,
        )
        user_submission = Submission.objects.create(user=self.user, period_month=date(2026, 6, 1))
        other_submission = Submission.objects.create(user=self.other_user, period_month=date(2026, 6, 1))
        user_receipt = Receipt.objects.create(
            submission=user_submission,
            service=user_service,
            service_name_snapshot=user_service.name,
            billing_type_snapshot=user_service.billing_type,
            original_filename="user.pdf",
            file=SimpleUploadedFile("user.pdf", b"%PDF-1.4 user", content_type="application/pdf"),
            expires_at=timezone.now() + timedelta(days=30),
        )
        other_receipt = Receipt.objects.create(
            submission=other_submission,
            service=other_service,
            service_name_snapshot=other_service.name,
            billing_type_snapshot=other_service.billing_type,
            original_filename="other.pdf",
            file=SimpleUploadedFile("other.pdf", b"%PDF-1.4 other", content_type="application/pdf"),
            expires_at=timezone.now() + timedelta(days=30),
        )
        user_receipt_path = Path(user_receipt.file.path)
        self.assertTrue(user_receipt_path.exists())
        older_uploaded_at = timezone.now() - timedelta(days=2)
        newer_uploaded_at = timezone.now() - timedelta(hours=1)
        Receipt.objects.filter(pk=user_receipt.pk).update(uploaded_at=older_uploaded_at)
        Receipt.objects.filter(pk=other_receipt.pk).update(uploaded_at=newer_uploaded_at)

        self.client.login(username="admin", password="admin-password-123")
        response = self.client.get(reverse("history") + "?month=2026-06")

        self.assertContains(response, "アップロード済み領収書")
        self.assertContains(response, "アップロード日時の新しい順")
        self.assertContains(response, "receipt-history-table")
        self.assertContains(response, "table-scroll-wide")
        content = response.content.decode()
        upload_section = content[content.index("アップロード済み領収書"):]
        self.assertLess(upload_section.index("other.pdf"), upload_section.index("user.pdf"))
        status_section = content[content.index("提出状況"):content.index("アップロード済み領収書")]
        self.assertLess(status_section.index("other@example.com"), status_section.index("user@example.com"))
        self.assertContains(response, "other.pdf")
        self.assertContains(response, "user.pdf")

        response = self.client.post(
            reverse("staff_delete_receipt", args=[user_receipt.pk]),
            {"next": reverse("history") + "?month=2026-06"},
        )

        self.assertRedirects(response, reverse("history") + "?month=2026-06")
        self.assertFalse(Receipt.objects.filter(pk=user_receipt.pk).exists())
        self.assertFalse(Submission.objects.filter(pk=user_submission.pk).exists())
        self.assertFalse(user_receipt_path.exists())
        self.assertTrue(Receipt.objects.filter(pk=other_receipt.pk).exists())

        self.client.logout()
        self.client.login(username="user@example.com", password="password123")
        response = self.client.get(reverse("history"))
        self.assertNotContains(response, "2026年06月")

    def test_staff_services_catalog_is_paginated_and_user_sections_are_combined(self):
        for index in range(25):
            ServiceCatalog.objects.create(
                name=f"Catalog {index:02d}",
                billing_type=BillingType.SUBSCRIPTION,
                created_by=self.admin,
            )
        RegisteredService.objects.create(
            user=self.user,
            catalog_service=self.catalog,
            name=self.catalog.name,
            billing_type=self.catalog.billing_type,
        )
        self.client.login(username="admin", password="admin-password-123")

        response = self.client.get(reverse("staff_services") + f"?user={self.user.pk}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["catalog_page_obj"].paginator.num_pages, 2)
        self.assertContains(response, "1 / 2ページ")
        self.assertContains(response, "対象ユーザー")
        self.assertContains(response, "scroll-box")
        self.assertNotContains(response, "ユーザー操作の通知")
        content = response.content.decode()
        self.assertLess(content.index("登録状況"), content.index("登録サービス一覧"))
        self.assertLess(content.index("登録サービス一覧"), content.index("新規登録/停止"))

    def test_staff_services_action_buttons_are_placed_inside_relevant_sections(self):
        RegisteredService.objects.create(
            user=self.user,
            catalog_service=self.catalog,
            name=self.catalog.name,
            billing_type=self.catalog.billing_type,
        )
        self.client.login(username="admin", password="admin-password-123")

        response = self.client.get(reverse("staff_services") + f"?user={self.user.pk}")

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "提出履歴へ")
        content = response.content.decode()
        self.assertEqual(content.count("サービスマスター登録"), 1)
        self.assertEqual(content.count("このユーザーへサービス登録"), 1)
        self.assertLess(content.index("<h2>サービスマスター</h2>"), content.index("サービスマスター登録"))
        self.assertLess(content.index("<h2>ユーザー別サービス状況</h2>"), content.index("このユーザーへサービス登録"))
        self.assertNotContains(response, ">サービスを登録</a>")

    def test_staff_registers_service_for_user_and_user_sees_it(self):
        self.client.login(username="admin", password="admin-password-123")
        response = self.client.post(
            reverse("staff_user_services", args=[self.user.pk]),
            {
                "catalog_service": self.catalog.pk,
                "is_active": "on",
                "memo": "API利用料",
            },
        )

        self.assertRedirects(response, reverse("staff_user_services", args=[self.user.pk]))
        service = RegisteredService.objects.get(name="OpenAI API")
        self.assertEqual(service.user, self.user)
        self.assertEqual(service.catalog_service, self.catalog)
        self.assertEqual(service.registration_source, ServiceRegistrationSource.ADMIN)
        self.assertTrue(service.is_active)

        self.client.logout()
        self.client.login(username="user@example.com", password="password123")
        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "OpenAI API")
        self.assertContains(response, "領収書を追加")
        self.assertNotContains(response, "<h2>利用サービス</h2>", html=True)
        self.assertNotContains(response, "サービス編集")

    def test_user_cannot_upload_receipt_for_another_users_service(self):
        own_service = RegisteredService.objects.create(
            user=self.user,
            name="Notion",
            billing_type=BillingType.SUBSCRIPTION,
        )
        other_service = RegisteredService.objects.create(
            user=self.other_user,
            name="AWS",
            billing_type=BillingType.METERED,
        )

        form = ReceiptUploadForm(user=self.user, period_month=date(2026, 6, 1))
        service_ids = set(form.fields["service"].queryset.values_list("id", flat=True))
        self.assertIn(own_service.id, service_ids)
        self.assertNotIn(other_service.id, service_ids)

        submission = Submission.objects.create(user=self.user, period_month=date(2026, 6, 1))
        receipt = Receipt(
            submission=submission,
            service=other_service,
            service_name_snapshot=other_service.name,
            billing_type_snapshot=other_service.billing_type,
            original_filename="receipt.pdf",
            file=SimpleUploadedFile("receipt.pdf", b"%PDF-1.4 test", content_type="application/pdf"),
        )

        with self.assertRaises(ValidationError):
            receipt.full_clean()

    def test_non_staff_cannot_access_service_management(self):
        self.client.login(username="user@example.com", password="password123")

        response = self.client.get(reverse("staff_user_services", args=[self.user.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

        response = self.client.get(reverse("service_create"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_staff_can_archive_and_reactivate_user_service(self):
        service = RegisteredService.objects.create(
            user=self.user,
            catalog_service=self.catalog,
            name=self.catalog.name,
            billing_type=self.catalog.billing_type,
        )
        self.client.login(username="admin", password="admin-password-123")

        response = self.client.post(reverse("staff_service_archive", args=[service.pk]))
        self.assertRedirects(response, reverse("staff_services") + f"?user={self.user.pk}")
        service.refresh_from_db()
        self.assertFalse(service.is_active)
        self.assertEqual(service.deactivation_source, ServiceDeactivationSource.ADMIN)

        self.client.logout()
        self.client.login(username="user@example.com", password="password123")
        response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, "AWS")
        self.assertContains(response, "領収書をアップロードするには、利用サービス登録が必要です")

        self.client.logout()
        self.client.login(username="admin", password="admin-password-123")
        response = self.client.post(reverse("staff_service_activate", args=[service.pk]))
        self.assertRedirects(response, reverse("staff_services") + f"?user={self.user.pk}")
        service.refresh_from_db()
        self.assertTrue(service.is_active)
        self.assertEqual(service.deactivation_source, "")

    def test_staff_can_create_catalog_service(self):
        self.client.login(username="admin", password="admin-password-123")
        response = self.client.post(
            reverse("staff_catalog_create"),
            {
                "name": "Notion",
                "billing_type": BillingType.SUBSCRIPTION,
                "is_active": "on",
                "memo": "ドキュメント管理",
            },
        )

        self.assertRedirects(response, reverse("staff_services"))
        catalog = ServiceCatalog.objects.get(name="Notion")
        self.assertEqual(catalog.billing_type, BillingType.SUBSCRIPTION)
        self.assertEqual(catalog.created_by, self.admin)

    def test_staff_can_create_same_catalog_name_with_different_billing_type(self):
        self.client.login(username="admin", password="admin-password-123")
        response = self.client.post(
            reverse("staff_catalog_create"),
            {
                "name": "ChatGPT",
                "billing_type": BillingType.SUBSCRIPTION,
                "is_active": "on",
            },
        )
        self.assertRedirects(response, reverse("staff_services"))

        response = self.client.post(
            reverse("staff_catalog_create"),
            {
                "name": "ChatGPT",
                "billing_type": BillingType.METERED,
                "is_active": "on",
            },
        )
        self.assertRedirects(response, reverse("staff_services"))

        self.assertEqual(ServiceCatalog.objects.filter(name="ChatGPT").count(), 2)
        response = self.client.get(reverse("staff_services"))
        self.assertContains(response, "ChatGPT（サブスク）")
        self.assertContains(response, "ChatGPT（従量課金 / API）")

    def test_staff_cannot_create_same_catalog_name_and_same_billing_type(self):
        ServiceCatalog.objects.create(name="ChatGPT", billing_type=BillingType.SUBSCRIPTION, created_by=self.admin)
        self.client.login(username="admin", password="admin-password-123")
        response = self.client.post(
            reverse("staff_catalog_create"),
            {
                "name": "chatgpt",
                "billing_type": BillingType.SUBSCRIPTION,
                "is_active": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "同じサービス名・同じ支払い種別のマスターがすでに登録されています。")

    def test_staff_can_assign_same_name_with_different_billing_type_to_same_user(self):
        subscription = ServiceCatalog.objects.create(name="ChatGPT", billing_type=BillingType.SUBSCRIPTION, created_by=self.admin)
        metered = ServiceCatalog.objects.create(name="ChatGPT", billing_type=BillingType.METERED, created_by=self.admin)
        self.client.login(username="admin", password="admin-password-123")

        for catalog in [subscription, metered]:
            response = self.client.post(
                reverse("staff_user_services", args=[self.user.pk]),
                {
                    "catalog_service": catalog.pk,
                    "is_active": "on",
                },
            )
            self.assertRedirects(response, reverse("staff_user_services", args=[self.user.pk]))

        services = RegisteredService.objects.filter(user=self.user, name="ChatGPT").order_by("billing_type")
        self.assertEqual(services.count(), 2)
        self.assertEqual({service.billing_type for service in services}, {BillingType.SUBSCRIPTION, BillingType.METERED})

        response = self.client.get(reverse("staff_user_services", args=[self.user.pk]))
        self.assertContains(response, "ChatGPT（サブスク）")
        self.assertContains(response, "ChatGPT（従量課金 / API）")


@override_settings(PASSWORD_HASHERS=FAST_PASSWORD_HASHERS)
class UserServiceRegistrationTests(TestCase):
    def setUp(self):
        self.media_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.media_dir.cleanup)
        self.override = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.admin = User.objects.create_superuser(username="admin", email="admin@example.com", password="admin-password-123")
        self.user = User.objects.create_user(username="user@example.com", email="user@example.com", password="password123")
        self.catalog = ServiceCatalog.objects.create(name="Slack", billing_type=BillingType.SUBSCRIPTION, created_by=self.admin)

    def test_user_registers_service_from_admin_catalog_and_staff_can_see_it(self):
        self.client.login(username="user@example.com", password="password123")
        response = self.client.post(
            reverse("user_service_create"),
            {"catalog_service": self.catalog.pk, "memo": "チーム連絡"},
        )

        self.assertRedirects(response, reverse("user_services"))
        service = RegisteredService.objects.get(user=self.user, name="Slack")
        self.assertEqual(service.catalog_service, self.catalog)
        self.assertEqual(service.registration_source, ServiceRegistrationSource.USER)
        self.assertEqual(service.registered_by, self.user)
        self.assertTrue(service.is_active)

        self.client.logout()
        self.client.login(username="admin", password="admin-password-123")
        response = self.client.get(reverse("staff_services"))
        self.assertContains(response, "新規登録/停止")
        self.assertNotContains(response, "ユーザー操作の通知")
        self.assertContains(response, "Slack（サブスク）")
        self.assertContains(response, "ユーザー登録")

    def test_user_services_page_places_registration_button_in_active_services_section(self):
        self.client.login(username="user@example.com", password="password123")
        response = self.client.get(reverse("user_services"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "サービス利用登録")
        self.assertNotContains(response, "アップロードへ")
        self.assertContains(response, "<h2>利用中サービス</h2>", html=True)

    def test_user_can_register_same_name_with_different_billing_type(self):
        subscription = ServiceCatalog.objects.create(name="ChatGPT", billing_type=BillingType.SUBSCRIPTION, created_by=self.admin)
        metered = ServiceCatalog.objects.create(name="ChatGPT", billing_type=BillingType.METERED, created_by=self.admin)
        self.client.login(username="user@example.com", password="password123")

        response = self.client.get(reverse("user_service_create"))
        self.assertContains(response, "ChatGPT（サブスク）")
        self.assertContains(response, "ChatGPT（従量課金 / API）")

        response = self.client.post(reverse("user_service_create"), {"catalog_service": subscription.pk})
        self.assertRedirects(response, reverse("user_services"))

        response = self.client.get(reverse("user_service_create"))
        self.assertNotContains(response, "ChatGPT（サブスク）")
        self.assertContains(response, "ChatGPT（従量課金 / API）")

        response = self.client.post(reverse("user_service_create"), {"catalog_service": metered.pk})
        self.assertRedirects(response, reverse("user_services"))

        services = RegisteredService.objects.filter(user=self.user, name="ChatGPT")
        self.assertEqual(services.count(), 2)
        self.assertEqual({service.billing_type for service in services}, {BillingType.SUBSCRIPTION, BillingType.METERED})

        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "ChatGPT（サブスク）")
        self.assertContains(response, "ChatGPT（従量課金 / API）")
        self.assertNotContains(response, "<h2>利用サービス</h2>", html=True)

    def test_user_can_stop_service_with_final_receipt_month_and_staff_can_see_it(self):
        service = RegisteredService.objects.create(
            user=self.user,
            catalog_service=self.catalog,
            name=self.catalog.name,
            billing_type=self.catalog.billing_type,
            registration_source=ServiceRegistrationSource.USER,
            registered_by=self.user,
        )
        self.client.login(username="user@example.com", password="password123")
        response = self.client.post(
            reverse("user_service_stop", args=[service.pk]),
            {"final_receipt_month": "2026-06", "stop_note": "解約済み"},
        )

        self.assertRedirects(response, reverse("user_services"))
        service.refresh_from_db()
        self.assertFalse(service.is_active)
        self.assertEqual(service.deactivation_source, ServiceDeactivationSource.USER)
        self.assertEqual(service.deactivated_by, self.user)
        self.assertEqual(service.final_receipt_month, date(2026, 6, 1))
        self.assertEqual(service.stop_note, "解約済み")

        response = self.client.get(reverse("dashboard") + "?month=2026-06")
        self.assertContains(response, "停止済み・最終 2026-06")
        form = response.context["receipt_form"]
        self.assertIn(service.id, set(form.fields["service"].queryset.values_list("id", flat=True)))

        response = self.client.get(reverse("dashboard") + "?month=2026-07")
        form = response.context["receipt_form"]
        self.assertNotIn(service.id, set(form.fields["service"].queryset.values_list("id", flat=True)))

        self.client.logout()
        self.client.login(username="admin", password="admin-password-123")
        response = self.client.get(reverse("staff_services"))
        self.assertContains(response, "ユーザー停止")
        self.assertContains(response, "2026年06月")
        self.assertContains(response, "解約済み")

    def test_user_cannot_register_service_that_is_not_in_catalog(self):
        inactive_catalog = ServiceCatalog.objects.create(name="Old Service", billing_type=BillingType.OTHER, is_active=False)
        self.client.login(username="user@example.com", password="password123")
        response = self.client.get(reverse("user_service_create"))

        form = response.context["form"]
        catalog_ids = set(form.fields["catalog_service"].queryset.values_list("id", flat=True))
        self.assertIn(self.catalog.id, catalog_ids)
        self.assertNotIn(inactive_catalog.id, catalog_ids)

    def test_stopped_service_cannot_be_used_after_final_receipt_month(self):
        service = RegisteredService.objects.create(
            user=self.user,
            catalog_service=self.catalog,
            name=self.catalog.name,
            billing_type=self.catalog.billing_type,
            is_active=False,
            deactivation_source=ServiceDeactivationSource.USER,
            deactivated_by=self.user,
            final_receipt_month=date(2026, 6, 1),
        )
        submission = Submission.objects.create(user=self.user, period_month=date(2026, 7, 1))
        receipt = Receipt(
            submission=submission,
            service=service,
            service_name_snapshot=service.name,
            billing_type_snapshot=service.billing_type,
            original_filename="receipt.pdf",
            file=SimpleUploadedFile("receipt.pdf", b"%PDF-1.4 test", content_type="application/pdf"),
        )

        with self.assertRaises(ValidationError):
            receipt.full_clean()
