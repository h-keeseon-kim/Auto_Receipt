from __future__ import annotations

import tempfile
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock
from pathlib import Path

from django.contrib.auth.models import User
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .ai_filename import (
    ReceiptFilenameResult,
    build_openai_content,
    build_result_from_ai_payload,
    filename_user_part_from_user,
)
from .forms import ExtraReceiptUploadForm, ReceiptUploadForm
from .monthly_status import build_user_month_summary
from .statement_ai import StatementAnalysisItem, StatementAnalysisResult, build_statement_result_from_payload
from .statement_processing import process_card_statement
from .models import (
    BillingType,
    CardStatement,
    CardStatementItem,
    CardStatementStatus,
    EmailDeliveryLog,
    EmailReminderSchedule,
    EmailDeliveryStatus,
    EmailType,
    Receipt,
    ReceiptFilenameStatus,
    ReceiptPeriodCheckStatus,
    ReceiptResubmissionRequest,
    RegisteredService,
    MonthlyServiceDeclaration,
    ResubmissionRequestStatus,
    ServiceCatalog,
    ServiceDeactivationSource,
    ServiceRegistrationSource,
    StatementMatchStatus,
    Submission,
    SubmissionStatus,
    UserAccountStatus,
    UserProfile,
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
            user_filename_part="alice",
        )

        self.assertEqual(result.status, ReceiptFilenameStatus.GENERATED)
        self.assertEqual(result.suggested_filename, "260619_alice_OpenAI_220_USD.pdf")
        self.assertEqual(result.payee, "OpenAI")


    def test_ai_filename_format_uses_user_email_local_part_and_company_name(self):
        user = User.objects.create_user(username="test@hakuhodo.co.jp", email="test@hakuhodo.co.jp", password="password123")
        result = build_result_from_ai_payload(
            {
                "card_last4": "7210",
                "card_last4_matches_target": True,
                "payee": "Anthropic, PBC",
                "service_payee_related": True,
                "service_payee_relation_reason": "Claudeの請求元として関連あり。",
                "payment_date": "2026-06-02",
                "amount": "220.00",
                "currency": "USD",
                "confidence": 0.98,
                "can_create_filename": True,
                "reason": "",
            },
            original_filename="Receipt-2990-1089-4605.pdf",
            user_filename_part=filename_user_part_from_user(user),
        )

        self.assertEqual(result.status, ReceiptFilenameStatus.GENERATED)
        self.assertEqual(result.suggested_filename, "260602_test_Anthropic_220_USD.pdf")

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
            suggested_filename="260619_alice_OpenAI_220_USD.pdf",
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
        self.assertEqual(receipt.generated_filename, "260619_alice_OpenAI_220_USD.pdf")
        self.assertEqual(receipt.display_filename, "260619_alice_OpenAI_220_USD.pdf")
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
    def test_background_ai_period_mismatch_removes_receipt_and_requests_resubmission(self, mocked_generate):
        mocked_generate.return_value = ReceiptFilenameResult(
            status=ReceiptFilenameStatus.GENERATED,
            suggested_filename="260531_alice_OpenAI_220_USD.pdf",
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
        self.assertContains(response, "AIによるファイル名修正・検査は、管理者が実行")

        call_command("process_pending_receipts", "--limit", "10")
        self.assertFalse(Receipt.objects.exists())
        request_item = ReceiptResubmissionRequest.objects.get()
        self.assertEqual(request_item.status, ResubmissionRequestStatus.OPEN)
        self.assertIn("対象月", request_item.message)
        self.assertIn("2026-05", request_item.message)
        submission = Submission.objects.get(user=self.user, period_month=date(2026, 6, 1))
        self.assertEqual(submission.status, SubmissionStatus.DRAFT)

        user_response = self.client.get(reverse("dashboard") + "?month=2026-06")
        self.assertContains(user_response, "再提出依頼")
        self.assertContains(user_response, "正しい領収書を再度アップロード")

    @mock.patch("receipts.ai_processing.generate_ai_receipt_filename")
    def test_ai_filename_review_memo_is_visible_to_staff_only(self, mocked_generate):
        mocked_generate.return_value = ReceiptFilenameResult(
            status=ReceiptFilenameStatus.NEEDS_REVIEW,
            admin_memo="カード下4桁が7210として確認できませんでした。",
            payee="OpenAI",
            card_last4="",
            card_last4_matches_target=None,
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
            service_payee_related=None,
            service_payee_relation_reason="払先がAnthropicと読めますが、登録サービスとの関連性を断定できません。",
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
        self.assertContains(staff_response, "サービス/メモ関連要確認")
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
            suggested_filename="260619_alice_OpenAI_220_USD.pdf",
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
        self.assertEqual(receipt.generated_filename, "260619_alice_OpenAI_220_USD.pdf")
        self.assertEqual(receipt.ai_period_check_status, ReceiptPeriodCheckStatus.MATCHED)
        mocked_generate.assert_called_once()

    @mock.patch("receipts.ai_processing.generate_ai_receipt_filename")
    def test_user_replace_receipt_file_is_removed_if_background_detects_different_month(self, mocked_generate):
        mocked_generate.return_value = ReceiptFilenameResult(
            status=ReceiptFilenameStatus.GENERATED,
            suggested_filename="260531_alice_OpenAI_220_USD.pdf",
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
        self.assertFalse(Receipt.objects.filter(pk=receipt.pk).exists())
        request_item = ReceiptResubmissionRequest.objects.get(original_receipt_id=receipt.pk)
        self.assertIn("対象月", request_item.message)
        submission.refresh_from_db()
        self.assertEqual(submission.status, SubmissionStatus.DRAFT)

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
            service_payee_related=None,
            service_payee_relation_reason="ChatGPT と Anthropic の関連性を断定できません。",
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
        self.assertContains(response, "ChatGPT と Anthropic の関連性を断定できません")

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

    def test_dashboard_has_plus_button_for_unexpected_extra_receipt(self):
        self.client.login(username="alice", password="password123")

        response = self.client.get(reverse("dashboard") + "?month=2026-06")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-extra-receipt-toggle")
        self.assertContains(response, "＋</span> その他の領収書")
        self.assertContains(response, "data-extra-upload-form")
        self.assertContains(response, "領収書の内容メモ")

    def test_extra_receipt_form_requires_memo(self):
        form = ExtraReceiptUploadForm(
            data={"memo": ""},
            files={"file": SimpleUploadedFile("refund.pdf", b"%PDF-1.4 refund", content_type="application/pdf")},
        )

        self.assertEqual(list(form.fields), ["memo", "file"])
        self.assertFalse(form.is_valid())
        self.assertIn("memo", form.errors)

    def test_user_can_upload_extra_receipt_without_registered_service(self):
        self.client.login(username="alice", password="password123")

        response = self.client.post(
            reverse("dashboard") + "?month=2026-06",
            {
                "action": "add_extra_receipt",
                "memo": "OpenAIからの返金領収書",
                "file": SimpleUploadedFile("refund.pdf", b"%PDF-1.4 refund", content_type="application/pdf"),
            },
        )

        self.assertEqual(response.status_code, 302)
        receipt = Receipt.objects.get()
        self.assertTrue(receipt.is_extra)
        self.assertIsNone(receipt.service_id)
        self.assertEqual(receipt.service_name_snapshot, "その他")
        self.assertEqual(receipt.billing_type_snapshot, BillingType.OTHER)
        self.assertEqual(receipt.service_display_name_snapshot, "その他")
        self.assertEqual(receipt.memo, "OpenAIからの返金領収書")
        summary = build_user_month_summary(self.user, date(2026, 6, 1))
        self.assertEqual(summary.api_pending_count, 1, "その他領収書は登録サービスの提出済み判定に流用しない")

    def test_submission_with_only_extra_receipt_is_allowed_when_no_registered_services_exist(self):
        extra_only_user = User.objects.create_user(username="extra-only@example.com", password="password123")
        submission = Submission.objects.create(user=extra_only_user, period_month=date(2026, 6, 1))
        Receipt.objects.create(
            submission=submission,
            service=None,
            is_extra=True,
            service_name_snapshot="その他",
            billing_type_snapshot=BillingType.OTHER,
            memo="OpenAIからの返金領収書",
            original_filename="refund.pdf",
            file=SimpleUploadedFile("refund.pdf", b"%PDF-1.4 refund", content_type="application/pdf"),
            expires_at=timezone.now() + timedelta(days=30),
        )

        submission.submit()

        submission.refresh_from_db()
        self.assertEqual(submission.status, SubmissionStatus.SUBMITTED)
        self.assertIsNotNone(submission.submitted_at)

    def test_extra_receipt_added_after_submission_reopens_month_as_draft(self):
        submission = Submission.objects.create(
            user=self.user,
            period_month=date(2026, 6, 1),
            status=SubmissionStatus.SUBMITTED,
            submitted_at=timezone.now(),
        )
        self.client.login(username="alice", password="password123")

        response = self.client.post(
            reverse("dashboard") + "?month=2026-06",
            {
                "action": "add_extra_receipt",
                "memo": "プラン変更による追加請求",
                "file": SimpleUploadedFile("adjustment.pdf", b"%PDF-1.4 adjustment", content_type="application/pdf"),
            },
        )

        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertEqual(submission.status, SubmissionStatus.DRAFT)
        self.assertIsNone(submission.submitted_at)
        self.assertTrue(submission.receipts.get().is_extra)

    @mock.patch("receipts.ai_processing.generate_ai_receipt_filename")
    def test_extra_receipt_ai_uses_required_memo_as_hint_and_receipt_content_as_priority(self, mocked_generate):
        submission = Submission.objects.create(user=self.user, period_month=date(2026, 6, 1))
        receipt = Receipt.objects.create(
            submission=submission,
            service=None,
            is_extra=True,
            service_name_snapshot="その他",
            billing_type_snapshot=BillingType.OTHER,
            memo="OpenAIからの返金領収書",
            original_filename="refund.pdf",
            file=SimpleUploadedFile("refund.pdf", b"%PDF-1.4 refund", content_type="application/pdf"),
            expires_at=timezone.now() + timedelta(days=30),
        )
        mocked_generate.return_value = ReceiptFilenameResult(
            status=ReceiptFilenameStatus.GENERATED,
            suggested_filename="260602_alice_OpenAI返金_220_USD.pdf",
            payee="OpenAI, LLC",
            filename_label="OpenAI返金",
            payment_date=date(2026, 6, 2),
            amount=Decimal("220.00"),
            currency="USD",
            card_last4="7210",
            card_last4_matches_target=True,
            service_payee_related=True,
        )

        call_command("process_pending_receipts", "--limit", "10")

        receipt.refresh_from_db()
        self.assertEqual(receipt.generated_filename, "260602_alice_OpenAI返金_220_USD.pdf")
        self.assertTrue(receipt.ai_check_service_payee_related)
        call_kwargs = mocked_generate.call_args.kwargs
        self.assertTrue(call_kwargs["is_extra"])
        self.assertEqual(call_kwargs["receipt_memo"], "OpenAIからの返金領収書")
        self.assertEqual(call_kwargs["service_display_name"], "その他")

        prompt = build_openai_content(
            file_bytes=b"%PDF-1.4 refund",
            original_filename="refund.pdf",
            content_type="application/pdf",
            service_display_name="その他",
            receipt_memo="OpenAIからの返金領収書",
            is_extra=True,
        )[1]["text"]
        self.assertIn("ユーザーが入力した必須メモ: OpenAIからの返金領収書", prompt)
        self.assertIn("領収書ファイル内の明確な記載を常に最優先", prompt)

    @mock.patch("receipts.ai_processing.generate_ai_receipt_filename")
    def test_clear_extra_memo_receipt_mismatch_removes_item_and_keeps_resubmission_context(self, mocked_generate):
        submission = Submission.objects.create(user=self.user, period_month=date(2026, 6, 1))
        receipt = Receipt.objects.create(
            submission=submission,
            service=None,
            is_extra=True,
            service_name_snapshot="その他",
            billing_type_snapshot=BillingType.OTHER,
            memo="OpenAIからの返金領収書",
            original_filename="wrong.pdf",
            file=SimpleUploadedFile("wrong.pdf", b"%PDF-1.4 wrong", content_type="application/pdf"),
            expires_at=timezone.now() + timedelta(days=30),
        )
        mocked_generate.return_value = ReceiptFilenameResult(
            status=ReceiptFilenameStatus.NEEDS_REVIEW,
            admin_memo="メモと領収書内容が不一致です。",
            payee="Anthropic",
            filename_label="Anthropic",
            payment_date=date(2026, 6, 2),
            amount=Decimal("220.00"),
            currency="USD",
            card_last4="7210",
            card_last4_matches_target=True,
            service_payee_related=False,
            service_payee_relation_reason="メモはOpenAI返金だが領収書はAnthropicの通常請求です。",
        )

        call_command("process_pending_receipts", "--limit", "10")

        self.assertFalse(Receipt.objects.filter(pk=receipt.pk).exists())
        request_item = ReceiptResubmissionRequest.objects.get()
        self.assertTrue(request_item.is_extra)
        self.assertEqual(request_item.receipt_memo_snapshot, "OpenAIからの返金領収書")
        self.assertIn("入力メモと領収書の内容が一致していません", request_item.message)

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

    def test_superuser_can_clear_own_contact_email_and_reuse_it_for_general_user(self):
        previous_email = self.admin.email

        response = self.client.post(
            reverse("staff_user_create"),
            {"action": "update_superuser_email", "email": ""},
        )

        self.assertRedirects(response, reverse("staff_user_create"))
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.username, "admin")
        self.assertEqual(self.admin.email, "")
        self.assertTrue(self.admin.check_password("admin-password-123"))

        response = self.client.post(
            reverse("staff_user_create"),
            {"action": "create_user", "email": previous_email},
        )
        self.assertEqual(response.status_code, 200)
        account = User.objects.get(username=previous_email)
        self.assertFalse(account.is_staff)
        self.assertEqual(account.email, previous_email)

    def test_superuser_contact_email_rejects_address_used_by_another_account(self):
        User.objects.create_user(username="used@example.com", email="used@example.com", password="password123")

        response = self.client.post(
            reverse("staff_user_create"),
            {"action": "update_superuser_email", "email": "USED@example.com"},
            follow=True,
        )

        self.assertContains(response, "このメールアドレスはすでに登録されています")
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.email, "admin@example.com")

    def test_clear_superuser_email_management_command_keeps_login_credentials(self):
        call_command("clear_superuser_email", "--username", "admin")

        self.admin.refresh_from_db()
        self.assertEqual(self.admin.email, "")
        self.assertEqual(self.admin.username, "admin")
        self.assertTrue(self.admin.check_password("admin-password-123"))

    def test_non_superuser_staff_cannot_change_superuser_contact_email(self):
        manager = User.objects.create_user(
            username="manager@example.com",
            email="manager@example.com",
            password="manager-password-123",
            is_staff=True,
        )
        self.client.logout()
        self.client.login(username=manager.username, password="manager-password-123")

        response = self.client.post(
            reverse("staff_user_create"),
            {"action": "update_superuser_email", "email": ""},
        )

        self.assertEqual(response.status_code, 403)
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.email, "admin@example.com")

    def test_staff_can_change_user_status_on_user_create_page(self):
        user = User.objects.create_user(username="status@example.com", email="status@example.com", password="password123")
        self.assertEqual(user.profile.account_status, UserAccountStatus.STOPPED)

        response = self.client.post(
            reverse("staff_user_create"),
            {
                "action": "update_status",
                "user_id": user.pk,
                "account_status": UserAccountStatus.ACTIVE,
            },
        )

        self.assertRedirects(response, reverse("staff_user_create"))
        user.profile.refresh_from_db()
        self.assertEqual(user.profile.account_status, UserAccountStatus.ACTIVE)

    def test_created_user_can_start_as_active_status(self):
        response = self.client.post(
            reverse("staff_user_create"),
            {"email": "active.user@example.com", "account_status": UserAccountStatus.ACTIVE},
        )

        self.assertEqual(response.status_code, 200)
        user = User.objects.get(username="active.user@example.com")
        self.assertEqual(user.profile.account_status, UserAccountStatus.ACTIVE)
        self.assertContains(response, "利用中")

    def test_staff_can_reset_user_password_randomly_and_view_it_once(self):
        user = User.objects.create_user(username="reset@example.com", email="reset@example.com", password="OldPassword123")
        user.profile.must_change_password = False
        user.profile.save(update_fields=["must_change_password", "updated_at"])

        response = self.client.post(
            reverse("staff_user_create"),
            {
                "action": "reset_password",
                "user_id": user.pk,
                "new_password": "",
                "new_password_confirm": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        user.profile.refresh_from_db()
        generated_password = response.context["password_result"]
        self.assertIsNotNone(generated_password)
        self.assertTrue(user.check_password(generated_password))
        self.assertTrue(user.profile.must_change_password)
        self.assertIsNotNone(user.profile.initial_password_generated_at)
        self.assertContains(response, "変更後パスワード")
        self.assertContains(response, generated_password)

    def test_staff_can_set_manual_user_password_on_user_management_page(self):
        user = User.objects.create_user(username="manual@example.com", email="manual@example.com", password="OldPassword123")

        response = self.client.post(
            reverse("staff_user_create"),
            {
                "action": "reset_password",
                "user_id": user.pk,
                "new_password": "ManualPassword12345",
                "new_password_confirm": "ManualPassword12345",
            },
        )

        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        user.profile.refresh_from_db()
        self.assertTrue(user.check_password("ManualPassword12345"))
        self.assertEqual(response.context["password_result"], "ManualPassword12345")
        self.assertFalse(response.context["password_result_was_random"])
        self.assertTrue(user.profile.must_change_password)

    def test_user_management_page_repairs_missing_profile_instead_of_500(self):
        user = User.objects.create_user(username="noprofile@example.com", email="noprofile@example.com", password="Password12345")
        user.profile.delete()

        response = self.client.get(reverse("staff_user_create"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(hasattr(User.objects.get(pk=user.pk), "profile"))
        self.assertContains(response, "noprofile@example.com")

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
        status_section = content[content.index("<h2>提出状況</h2>"):content.index("<h2>アップロード済み領収書</h2>")]
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

    def test_staff_services_catalog_and_user_status_are_separate_tabs(self):
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

        catalog_response = self.client.get(reverse("staff_services") + "?tab=catalog")
        self.assertEqual(catalog_response.status_code, 200)
        self.assertEqual(catalog_response.context["active_tab"], "catalog")
        self.assertEqual(catalog_response.context["catalog_page_obj"].paginator.num_pages, 2)
        self.assertContains(catalog_response, "1 / 2ページ")
        self.assertContains(catalog_response, "サービスマスター登録")
        self.assertNotContains(catalog_response, "対象ユーザー")

        response = self.client.get(reverse("staff_services") + f"?tab=users&user={self.user.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["active_tab"], "users")
        self.assertContains(response, "対象ユーザー")
        self.assertContains(response, "scroll-box")
        self.assertNotContains(response, "サービスマスター登録")
        self.assertNotContains(response, "ユーザー操作の通知")
        content = response.content.decode()
        self.assertLess(content.index("登録状況"), content.index("登録サービス一覧"))
        self.assertLess(content.index("登録サービス一覧"), content.index("新規登録/停止"))

    def test_staff_can_start_ai_processing_manually_and_processed_items_are_skipped(self):
        service = RegisteredService.objects.create(
            user=self.user,
            catalog_service=self.catalog,
            name=self.catalog.name,
            billing_type=self.catalog.billing_type,
        )
        submission = Submission.objects.create(user=self.user, period_month=date(2026, 6, 1))
        pending_receipt = Receipt.objects.create(
            submission=submission,
            service=service,
            service_name_snapshot=service.name,
            billing_type_snapshot=service.billing_type,
            original_filename="pending.pdf",
            file=SimpleUploadedFile("pending.pdf", b"%PDF-1.4 pending", content_type="application/pdf"),
            expires_at=timezone.now() + timedelta(days=30),
        )
        processed_receipt = Receipt.objects.create(
            submission=submission,
            service=service,
            service_name_snapshot=service.name,
            billing_type_snapshot=service.billing_type,
            original_filename="done.pdf",
            generated_filename="260619_alice_OpenAI_220_USD.pdf",
            ai_filename_status=ReceiptFilenameStatus.GENERATED,
            ai_filename_checked_at=timezone.now(),
            file=SimpleUploadedFile("done.pdf", b"%PDF-1.4 done", content_type="application/pdf"),
            expires_at=timezone.now() + timedelta(days=30),
        )
        self.client.login(username="admin", password="admin-password-123")

        with mock.patch("receipts.views.start_background_ai_processing") as mocked_start:
            response = self.client.post(
                reverse("staff_start_ai_processing"),
                {"month": "2026-06", "limit": "50"},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["started_count"], 1)
        mocked_start.assert_called_once_with([pending_receipt.pk])
        pending_receipt.refresh_from_db()
        processed_receipt.refresh_from_db()
        self.assertEqual(pending_receipt.ai_filename_status, ReceiptFilenameStatus.PROCESSING)
        self.assertEqual(processed_receipt.ai_filename_status, ReceiptFilenameStatus.GENERATED)

        response = self.client.get(reverse("history") + "?month=2026-06")
        self.assertContains(response, "AIでファイル名を修正")
        self.assertContains(response, "AI抽出中")

        with mock.patch("receipts.views.start_background_ai_processing") as mocked_start_again:
            response = self.client.post(
                reverse("staff_start_ai_processing"),
                {"month": "2026-06", "limit": "50"},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
        self.assertEqual(response.json()["started_count"], 0)
        mocked_start_again.assert_not_called()

    def test_staff_ai_status_endpoint_returns_rendered_rows_for_realtime_refresh(self):
        service = RegisteredService.objects.create(
            user=self.user,
            catalog_service=self.catalog,
            name=self.catalog.name,
            billing_type=self.catalog.billing_type,
        )
        submission = Submission.objects.create(user=self.user, period_month=date(2026, 6, 1))
        Receipt.objects.create(
            submission=submission,
            service=service,
            service_name_snapshot=service.name,
            billing_type_snapshot=service.billing_type,
            original_filename="processing.pdf",
            ai_filename_status=ReceiptFilenameStatus.PROCESSING,
            file=SimpleUploadedFile("processing.pdf", b"%PDF-1.4 processing", content_type="application/pdf"),
            expires_at=timezone.now() + timedelta(days=30),
        )
        self.client.login(username="admin", password="admin-password-123")

        response = self.client.get(
            reverse("staff_ai_processing_status") + "?month=2026-06",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["processing_count"], 1)
        self.assertIn("processing.pdf", payload["receipts_html"])
        self.assertIn("AI抽出中", payload["receipts_html"])

    def test_staff_services_action_buttons_are_placed_inside_relevant_sections(self):
        RegisteredService.objects.create(
            user=self.user,
            catalog_service=self.catalog,
            name=self.catalog.name,
            billing_type=self.catalog.billing_type,
        )
        self.client.login(username="admin", password="admin-password-123")

        catalog_response = self.client.get(reverse("staff_services") + "?tab=catalog")
        self.assertEqual(catalog_response.status_code, 200)
        self.assertNotContains(catalog_response, "提出履歴へ")
        catalog_content = catalog_response.content.decode()
        self.assertEqual(catalog_content.count("サービスマスター登録"), 1)
        self.assertLess(catalog_content.index("<h2>サービスマスター</h2>"), catalog_content.index("サービスマスター登録"))

        user_response = self.client.get(reverse("staff_services") + f"?tab=users&user={self.user.pk}")
        self.assertEqual(user_response.status_code, 200)
        user_content = user_response.content.decode()
        self.assertEqual(user_content.count("このユーザーへサービス登録"), 1)
        self.assertLess(user_content.index("<h2>ユーザー別サービス状況</h2>"), user_content.index("このユーザーへサービス登録"))
        self.assertNotContains(user_response, ">サービスを登録</a>")

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

    def test_user_status_auto_updates_when_services_start_and_stop(self):
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.account_status, UserAccountStatus.STOPPED)

        service = RegisteredService.objects.create(
            user=self.user,
            catalog_service=self.catalog,
            name=self.catalog.name,
            billing_type=self.catalog.billing_type,
        )
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.account_status, UserAccountStatus.ACTIVE)

        service.deactivate(by=self.admin, source=ServiceDeactivationSource.ADMIN)
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.account_status, UserAccountStatus.STOPPED)

        service.activate()
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.account_status, UserAccountStatus.ACTIVE)

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
        response = self.client.get(reverse("staff_services") + f"?tab=users&user={self.user.pk}")
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
        response = self.client.get(reverse("staff_services") + f"?tab=users&user={self.user.pk}")
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


@override_settings(PASSWORD_HASHERS=FAST_PASSWORD_HASHERS)
class TutorialTests(TestCase):
    def test_app_name_is_receipthub_and_user_tutorial_auto_opens_until_completed(self):
        user = User.objects.create_user(username="tutorial@example.com", email="tutorial@example.com", password="password123")
        self.client.login(username="tutorial@example.com", password="password123")

        response = self.client.get(reverse("user_services"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ReceiptHub")
        self.assertContains(response, "data-tutorial-open")
        self.assertContains(response, 'data-auto-start="true"')
        self.assertContains(response, 'data-tutorial-target="user-services-nav"')
        self.assertContains(response, 'data-tutorial-target="service-registration-button"')
        self.assertNotContains(response, "完了後も右上の「?」からいつでも再表示できます。")
        self.assertNotContains(response, "data-tutorial-hint")
        self.assertNotContains(response, "tutorial-note")

        complete_response = self.client.post(reverse("tutorial_complete"), HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        self.assertEqual(complete_response.status_code, 200)
        self.assertJSONEqual(complete_response.content, {"ok": True, "tutorial_completed": True})
        user.profile.refresh_from_db()
        self.assertIsNotNone(user.profile.tutorial_completed_at)

        response = self.client.get(reverse("user_services"))
        self.assertContains(response, 'data-auto-start="false"')
        self.assertContains(response, "data-tutorial-open")


    def test_tutorial_has_page_routing_data_and_service_stop_target(self):
        user = User.objects.create_user(username="stop-tutorial@example.com", email="stop-tutorial@example.com", password="password123")
        catalog = ServiceCatalog.objects.create(name="ChatGPT", billing_type=BillingType.SUBSCRIPTION)
        RegisteredService.objects.create(
            user=user,
            catalog_service=catalog,
            name=catalog.name,
            billing_type=catalog.billing_type,
        )
        self.client.login(username="stop-tutorial@example.com", password="password123")

        response = self.client.get(reverse("user_services"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-current-url-name="user_services"')
        self.assertContains(response, f'data-user-services-url="{reverse("user_services")}"')
        self.assertContains(response, f'data-upload-url="{reverse("dashboard")}"')
        self.assertContains(response, f'data-history-url="{reverse("history")}"')
        self.assertContains(response, 'data-tutorial-target="service-stop-button"')

        upload_response = self.client.get(reverse("dashboard"))
        self.assertContains(upload_response, 'data-tutorial-target="upload-page"')

    def test_tutorial_script_forces_page_steps_and_returns_to_start_page(self):
        script = Path("static/js/tutorial.js").read_text()

        self.assertIn('pageName: "user_services"', script)
        self.assertIn('pageName: "dashboard"', script)
        self.assertIn('pageName: "history"', script)
        self.assertIn("使わなくなったサービスを停止します", script)
        self.assertIn("領収書アップロードページです。", script)
        self.assertIn("提出履歴ページです。", script)
        self.assertNotIn("提出履歴ページへ移動します", script)
        self.assertNotIn("hint:", script)
        self.assertNotIn("data-tutorial-hint", script)
        self.assertIn("returnUrl", script)
        self.assertIn("window.location.assign(url)", script)

    def test_staff_gets_staff_tutorial_steps(self):
        admin = User.objects.create_superuser(username="admin", email="admin@example.com", password="admin-password-123")
        self.client.login(username="admin", password="admin-password-123")

        response = self.client.get(reverse("history"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-auto-start="true"')
        self.assertContains(response, 'data-tutorial-target="staff-history-nav"')
        self.assertContains(response, 'data-tutorial-target="staff-status-table"')
        self.assertContains(response, 'data-tutorial-target="staff-receipt-table"')
        self.assertContains(response, "ReceiptHub")

    def test_tutorial_completion_requires_login_and_post(self):
        get_response = self.client.get(reverse("tutorial_complete"))
        self.assertEqual(get_response.status_code, 302)
        self.assertIn(reverse("login"), get_response["Location"])

        user = User.objects.create_user(username="postonly@example.com", password="password123")
        self.client.login(username="postonly@example.com", password="password123")
        response = self.client.get(reverse("tutorial_complete"))
        self.assertEqual(response.status_code, 405)
        user.profile.refresh_from_db()
        self.assertIsNone(user.profile.tutorial_completed_at)


@override_settings(
    PASSWORD_HASHERS=FAST_PASSWORD_HASHERS,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="noreply@mkt-dev3.info",
    APP_BASE_URL="https://receipthub.example.com",
)
class EmailReminderTests(TestCase):
    def setUp(self):
        mail.outbox = []
        self.user_a = User.objects.create_user(username="a@example.com", email="a@example.com", password="password123")
        self.user_b = User.objects.create_user(username="b@example.com", email="b@example.com", password="password123")
        self.user_c = User.objects.create_user(username="c@example.com", email="c@example.com", password="password123")
        self.admin = User.objects.create_superuser(username="admin", email="admin@example.com", password="admin-password-123")
        self.services = {}
        for account in [self.user_a, self.user_b, self.user_c]:
            service = RegisteredService.objects.create(
                user=account,
                name="ReceiptHub Test Subscription",
                billing_type=BillingType.SUBSCRIPTION,
            )
            self.services[account.pk] = service
            account.profile.refresh_from_db()
            self.assertEqual(account.profile.account_status, UserAccountStatus.ACTIVE)

    def test_initial_reminder_sends_to_all_general_users_once(self):
        call_command("send_receipt_reminders", "--kind", "initial", "--month", "2026-06")

        self.assertEqual(len(mail.outbox), 3)
        self.assertEqual(EmailDeliveryLog.objects.filter(email_type=EmailType.REMINDER_INITIAL, status=EmailDeliveryStatus.SENT).count(), 3)
        self.assertIn("2026年06月分について", mail.outbox[0].body)
        self.assertIn("https://receipthub.example.com/dashboard/?month=2026-06", mail.outbox[0].body)

        call_command("send_receipt_reminders", "--kind", "initial", "--month", "2026-06")

        self.assertEqual(len(mail.outbox), 3)
        self.assertEqual(EmailDeliveryLog.objects.filter(email_type=EmailType.REMINDER_INITIAL).count(), 3)

    def test_urgent_reminder_sends_only_to_users_without_submitted_submission(self):
        submitted = Submission.objects.create(
            user=self.user_b,
            period_month=date(2026, 6, 1),
            status=SubmissionStatus.SUBMITTED,
            submitted_at=timezone.now(),
        )
        Receipt.objects.create(
            submission=submitted,
            service=self.services[self.user_b.pk],
            service_name_snapshot=self.services[self.user_b.pk].name,
            billing_type_snapshot=BillingType.SUBSCRIPTION,
            original_filename="submitted.pdf",
            file="receipts/submitted.pdf",
            expires_at=timezone.now() + timedelta(days=30),
        )
        Submission.objects.create(user=self.user_c, period_month=date(2026, 6, 1), status=SubmissionStatus.DRAFT)

        call_command("send_receipt_reminders", "--kind", "urgent", "--month", "2026-06")

        self.assertEqual(len(mail.outbox), 2)
        recipients = {message.to[0] for message in mail.outbox}
        self.assertEqual(recipients, {"a@example.com", "c@example.com"})
        self.assertTrue(all(message.subject.startswith("【重要】") for message in mail.outbox))

    def test_reminders_exclude_stopped_users(self):
        self.user_b.profile.account_status = UserAccountStatus.STOPPED
        self.user_b.profile.save(update_fields=["account_status", "updated_at"])

        call_command("send_receipt_reminders", "--kind", "initial", "--month", "2026-06")

        recipients = {message.to[0] for message in mail.outbox}
        self.assertEqual(recipients, {"a@example.com", "c@example.com"})
        self.assertFalse(EmailDeliveryLog.objects.filter(to_email="b@example.com").exists())

    def test_test_email_is_skipped_for_stopped_user(self):
        self.user_a.profile.account_status = UserAccountStatus.STOPPED
        self.user_a.profile.save(update_fields=["account_status", "updated_at"])
        self.client.login(username="admin", password="admin-password-123")

        response = self.client.post(
            reverse("staff_email"),
            {
                "to_email": "a@example.com",
                "subject": "ReceiptHub テスト",
                "body": "テスト本文です。",
            },
        )

        self.assertRedirects(response, reverse("staff_email"))
        self.assertEqual(len(mail.outbox), 0)
        log = EmailDeliveryLog.objects.get(email_type=EmailType.TEST)
        self.assertEqual(log.to_email, "a@example.com")
        self.assertEqual(log.status, EmailDeliveryStatus.SKIPPED)
        self.assertEqual(log.user, self.user_a)

    def test_staff_email_test_page_sends_test_email_and_logs_result(self):
        self.client.login(username="admin", password="admin-password-123")

        response = self.client.post(
            reverse("staff_email"),
            {
                "to_email": "audit@example.com",
                "subject": "ReceiptHub テスト",
                "body": "テスト本文です。",
            },
        )

        self.assertRedirects(response, reverse("staff_email"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["audit@example.com"])
        log = EmailDeliveryLog.objects.get(email_type=EmailType.TEST)
        self.assertEqual(log.to_email, "audit@example.com")
        self.assertEqual(log.status, EmailDeliveryStatus.SENT)

    def test_staff_email_page_updates_reminder_schedule(self):
        self.client.login(username="admin", password="admin-password-123")

        response = self.client.get(reverse("staff_email"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "メール送信")
        self.assertContains(response, "リマインダー送信日")
        self.assertNotContains(response, "リマインダー実行設定")

        response = self.client.post(
            reverse("staff_email"),
            {
                "action": "update_reminder_schedule",
                "reminder_day": "6",
                "warning_day": "13",
            },
        )

        self.assertRedirects(response, reverse("staff_email"))
        schedule = EmailReminderSchedule.get_solo()
        self.assertEqual(schedule.reminder_day, 6)
        self.assertEqual(schedule.warning_day, 13)
        self.assertEqual(schedule.updated_by, self.admin)

    def test_auto_reminder_uses_configured_days(self):
        schedule = EmailReminderSchedule.get_solo()
        schedule.reminder_day = 8
        schedule.warning_day = 14
        schedule.save()

        with mock.patch("receipts.management.commands.send_receipt_reminders.timezone.localdate", return_value=date(2026, 6, 8)):
            call_command("send_receipt_reminders", "--kind", "auto", "--month", "2026-06")

        self.assertEqual(len(mail.outbox), 3)
        self.assertEqual(EmailDeliveryLog.objects.filter(email_type=EmailType.REMINDER_INITIAL).count(), 3)

        mail.outbox = []
        with mock.patch("receipts.management.commands.send_receipt_reminders.timezone.localdate", return_value=date(2026, 6, 9)):
            call_command("send_receipt_reminders", "--kind", "auto", "--month", "2026-06")

        self.assertEqual(len(mail.outbox), 0)


@override_settings(
    PASSWORD_HASHERS=FAST_PASSWORD_HASHERS,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="noreply@mkt-dev3.info",
    APP_BASE_URL="https://receipthub.example.com",
)
class FinalWorkflowAcceptanceTests(TestCase):
    """最終運用フローで追加された機能の受け入れテスト。"""

    def setUp(self):
        mail.outbox = []
        self.media_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.media_dir.cleanup)
        self.media_override = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)

        self.superuser = User.objects.create_superuser(
            username="admin",
            email="admin@example.com",
            password="admin-password-123",
        )
        self.user = User.objects.create_user(
            username="user@example.com",
            email="user@example.com",
            password="password123",
        )
        self.subscription_catalog = ServiceCatalog.objects.create(
            name="ChatGPT",
            billing_type=BillingType.SUBSCRIPTION,
            merchant_aliases="OPENAI *CHATGPT, OPENAI.COM",
            created_by=self.superuser,
        )
        self.api_catalog = ServiceCatalog.objects.create(
            name="OpenAI API",
            billing_type=BillingType.METERED,
            merchant_aliases="OPENAI, OPENAI.COM",
            created_by=self.superuser,
        )
        self.subscription = RegisteredService.objects.create(
            user=self.user,
            catalog_service=self.subscription_catalog,
            name=self.subscription_catalog.name,
            billing_type=self.subscription_catalog.billing_type,
            registered_by=self.superuser,
        )
        self.api_service = RegisteredService.objects.create(
            user=self.user,
            catalog_service=self.api_catalog,
            name=self.api_catalog.name,
            billing_type=self.api_catalog.billing_type,
            registered_by=self.superuser,
        )
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.account_status, UserAccountStatus.ACTIVE)

    def create_receipt(self, *, service=None, month=date(2026, 6, 1), filename="receipt.pdf"):
        service = service or self.subscription
        submission, _ = Submission.objects.get_or_create(user=self.user, period_month=month)
        return Receipt.objects.create(
            submission=submission,
            service=service,
            service_name_snapshot=service.name,
            billing_type_snapshot=service.billing_type,
            original_filename=filename,
            file=SimpleUploadedFile(filename, b"%PDF-1.4 test", content_type="application/pdf"),
            expires_at=timezone.now() + timedelta(days=30),
        )

    def test_submission_requires_receipt_or_no_usage_for_every_service(self):
        receipt = self.create_receipt(service=self.subscription)
        submission = receipt.submission

        summary = build_user_month_summary(self.user, date(2026, 6, 1))
        self.assertEqual(summary.uploaded_count, 1)
        self.assertEqual(summary.api_pending_count, 1)
        self.assertFalse(summary.is_complete)
        with self.assertRaises(ValidationError):
            submission.submit()

        MonthlyServiceDeclaration.objects.create(
            user=self.user,
            service=self.api_service,
            period_month=date(2026, 6, 1),
            no_usage=True,
            declared_by=self.user,
        )
        submission.submit()
        submission.refresh_from_db()
        self.assertTrue(submission.is_submitted)
        self.assertTrue(build_user_month_summary(self.user, date(2026, 6, 1)).is_complete)

    def test_dashboard_allows_metered_no_usage_declaration(self):
        self.client.login(username="user@example.com", password="password123")
        response = self.client.post(
            reverse("dashboard") + "?month=2026-06",
            {"action": "declare_no_usage", "service_id": self.api_service.pk},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            MonthlyServiceDeclaration.objects.filter(
                user=self.user,
                service=self.api_service,
                period_month=date(2026, 6, 1),
                no_usage=True,
            ).exists()
        )
        self.assertContains(response, "当月利用なし")

    def test_normal_reminder_skips_api_only_pending_but_urgent_reminder_sends(self):
        self.create_receipt(service=self.subscription)

        call_command("send_receipt_reminders", "--kind", "initial", "--month", "2026-06")
        self.assertEqual(len(mail.outbox), 0)

        call_command("send_receipt_reminders", "--kind", "urgent", "--month", "2026-06")
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(mail.outbox[0].subject.startswith("【重要】"))
        self.assertIn("OpenAI API（従量課金 / API）", mail.outbox[0].body)
        self.assertIn("本日中", mail.outbox[0].body)

    def test_no_usage_declaration_suppresses_api_urgent_reminder(self):
        self.create_receipt(service=self.subscription)
        MonthlyServiceDeclaration.objects.create(
            user=self.user,
            service=self.api_service,
            period_month=date(2026, 7, 1),
            no_usage=True,
            declared_by=self.user,
        )
        # 7月のサブスク領収書も登録済みにする。
        self.create_receipt(service=self.subscription, month=date(2026, 7, 1), filename="july.pdf")

        call_command("send_receipt_reminders", "--kind", "urgent", "--month", "2026-07")
        self.assertEqual(len(mail.outbox), 0)

    def test_admin_custom_email_templates_are_used(self):
        schedule = EmailReminderSchedule.get_solo()
        schedule.initial_subject_template = "{target_month} 未提出のお知らせ"
        schedule.initial_body_template = "{user_name}\n不足:\n{missing_services}\n{upload_url}"
        schedule.save()

        call_command("send_receipt_reminders", "--kind", "initial", "--month", "2026-06")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "2026年06月 未提出のお知らせ")
        self.assertIn("ChatGPT（サブスク）", mail.outbox[0].body)
        self.assertIn("https://receipthub.example.com/dashboard/?month=2026-06", mail.outbox[0].body)

    def test_superuser_can_change_role_and_normal_admin_cannot(self):
        self.client.login(username="admin", password="admin-password-123")
        response = self.client.post(
            reverse("staff_user_create"),
            {"action": "update_role", "user_id": self.user.pk, "account_role": "admin"},
        )
        self.assertRedirects(response, reverse("staff_user_create"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_staff)
        self.assertFalse(self.user.is_superuser)

        target = User.objects.create_user(
            username="target@example.com",
            email="target@example.com",
            password="password123",
        )
        self.client.logout()
        self.client.login(username="user@example.com", password="password123")
        denied = self.client.post(
            reverse("staff_user_create"),
            {"action": "update_role", "user_id": target.pk, "account_role": "admin"},
        )
        self.assertEqual(denied.status_code, 403)
        target.refresh_from_db()
        self.assertFalse(target.is_staff)

    def test_superuser_can_create_admin_account_with_forced_password_change(self):
        self.client.login(username="admin", password="admin-password-123")
        response = self.client.post(
            reverse("staff_user_create"),
            {
                "email": "manager@example.com",
                "account_role": "admin",
                "account_status": UserAccountStatus.STOPPED,
            },
        )
        self.assertEqual(response.status_code, 200)
        account = User.objects.get(username="manager@example.com")
        self.assertTrue(account.is_staff)
        self.assertFalse(account.is_superuser)
        self.assertTrue(account.profile.must_change_password)
        self.assertIsNotNone(response.context["generated_password"])

    def test_user_delete_removes_receipt_file_and_related_records(self):
        receipt = self.create_receipt()
        receipt_path = Path(receipt.file.path)
        self.assertTrue(receipt_path.exists())

        self.client.login(username="admin", password="admin-password-123")
        response = self.client.post(
            reverse("staff_user_create"),
            {"action": "delete_user", "user_id": self.user.pk},
        )
        self.assertRedirects(response, reverse("staff_user_create"))
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())
        self.assertFalse(Receipt.objects.filter(pk=receipt.pk).exists())
        self.assertFalse(receipt_path.exists())

    def test_statement_payload_treats_payment_month_as_target_month(self):
        result = build_statement_result_from_payload(
            {
                "card_last4": "7210",
                "statement_period": "2026-06",
                "payment_date": "2026-06-29",
                "summary_reason": "2026年5月利用分を2026年6月に請求。",
                "items": [
                    {
                        "line_reference": "0276",
                        "transaction_date": "2026-05-03",
                        "merchant_name": "OPENAI *CHATGPT",
                        "amount_jpy": "35949",
                        "original_amount": "220",
                        "original_currency": "USD",
                        "service_catalog_id": self.subscription_catalog.pk,
                        "match_status": StatementMatchStatus.MATCHED,
                        "receipt_required": True,
                        "confidence": 0.99,
                        "reason": "ChatGPTの請求元として一致。",
                    }
                ],
            },
            target_month="2026-06",
            allowed_catalog_ids={self.subscription_catalog.pk, self.api_catalog.pk},
        )
        self.assertEqual(result.status, CardStatementStatus.COMPLETED)
        self.assertEqual(result.statement_period, "2026-06")
        self.assertEqual(result.payment_date, date(2026, 6, 29))
        self.assertEqual(result.items[0].transaction_date, date(2026, 5, 3))
        self.assertEqual(result.items[0].service_catalog_id, self.subscription_catalog.pk)

    @mock.patch("receipts.statement_processing.generate_card_statement_analysis")
    def test_statement_processing_highlights_missing_receipt_and_cancels_false_no_usage(self, mocked_analysis):
        MonthlyServiceDeclaration.objects.create(
            user=self.user,
            service=self.api_service,
            period_month=date(2026, 6, 1),
            no_usage=True,
            declared_by=self.user,
        )
        statement = CardStatement.objects.create(
            period_month=date(2026, 6, 1),
            file=SimpleUploadedFile("statement.pdf", b"%PDF-1.4 statement", content_type="application/pdf"),
            original_filename="statement.pdf",
            content_type="application/pdf",
            status=CardStatementStatus.PROCESSING,
            uploaded_by=self.superuser,
            expires_at=timezone.now() + timedelta(days=30),
        )
        mocked_analysis.return_value = StatementAnalysisResult(
            status=CardStatementStatus.COMPLETED,
            card_last4="7210",
            statement_period="2026-06",
            payment_date=date(2026, 6, 29),
            items=(
                StatementAnalysisItem(
                    line_reference="0302",
                    transaction_date=date(2026, 5, 16),
                    merchant_name="OPENAI",
                    amount_jpy=Decimal("8236"),
                    original_amount=Decimal("49.92"),
                    original_currency="USD",
                    service_catalog_id=self.api_catalog.pk,
                    match_status=StatementMatchStatus.MATCHED,
                    receipt_required=True,
                    confidence=0.98,
                    reason="OpenAI APIと一致。",
                ),
            ),
        )

        process_card_statement(statement.pk)
        statement.refresh_from_db()
        item = statement.items.get()
        self.assertEqual(statement.status, CardStatementStatus.NEEDS_REVIEW)
        self.assertFalse(
            MonthlyServiceDeclaration.objects.filter(
                user=self.user,
                service=self.api_service,
                period_month=date(2026, 6, 1),
            ).exists()
        )
        self.assertTrue(item.needs_highlight)
        self.assertEqual(item.receipt_status_label, "領収書未提出")
        self.assertIn("当月利用なし", item.match_memo)
        self.assertIn("利用なし", statement.ai_admin_memo)

    @mock.patch("receipts.statement_processing.generate_card_statement_analysis")
    def test_statement_processing_links_existing_receipt(self, mocked_analysis):
        receipt = self.create_receipt(service=self.subscription)
        statement = CardStatement.objects.create(
            period_month=date(2026, 6, 1),
            file=SimpleUploadedFile("statement.pdf", b"%PDF-1.4 statement", content_type="application/pdf"),
            original_filename="statement.pdf",
            content_type="application/pdf",
            status=CardStatementStatus.PROCESSING,
            uploaded_by=self.superuser,
            expires_at=timezone.now() + timedelta(days=30),
        )
        mocked_analysis.return_value = StatementAnalysisResult(
            status=CardStatementStatus.COMPLETED,
            card_last4="7210",
            statement_period="2026-06",
            payment_date=date(2026, 6, 29),
            items=(
                StatementAnalysisItem(
                    line_reference="0276",
                    transaction_date=date(2026, 5, 3),
                    merchant_name="OPENAI *CHATGPT",
                    amount_jpy=Decimal("35949"),
                    original_amount=Decimal("220"),
                    original_currency="USD",
                    service_catalog_id=self.subscription_catalog.pk,
                    match_status=StatementMatchStatus.MATCHED,
                    receipt_required=True,
                    confidence=0.99,
                    reason="一致。",
                ),
            ),
        )

        process_card_statement(statement.pk)
        item = statement.items.get()
        self.assertEqual(item.matched_receipt, receipt)
        self.assertFalse(item.needs_highlight)
        self.assertEqual(item.receipt_status_label, "領収書あり")

    @mock.patch("receipts.statement_processing.generate_card_statement_analysis")
    def test_statement_rows_use_distinct_receipts_and_keep_extra_charge_highlighted(self, mocked_analysis):
        first_receipt = self.create_receipt(service=self.subscription, filename="first.pdf")
        statement = CardStatement.objects.create(
            period_month=date(2026, 6, 1),
            file=SimpleUploadedFile("statement.pdf", b"%PDF-1.4 statement", content_type="application/pdf"),
            original_filename="statement.pdf",
            content_type="application/pdf",
            status=CardStatementStatus.PROCESSING,
            uploaded_by=self.superuser,
            expires_at=timezone.now() + timedelta(days=30),
        )
        mocked_analysis.return_value = StatementAnalysisResult(
            status=CardStatementStatus.COMPLETED,
            card_last4="7210",
            statement_period="2026-06",
            payment_date=date(2026, 6, 29),
            items=(
                StatementAnalysisItem(
                    line_reference="0276",
                    transaction_date=date(2026, 5, 3),
                    merchant_name="OPENAI *CHATGPT",
                    amount_jpy=Decimal("35949"),
                    original_amount=Decimal("220"),
                    original_currency="USD",
                    service_catalog_id=self.subscription_catalog.pk,
                    match_status=StatementMatchStatus.MATCHED,
                    receipt_required=True,
                    confidence=0.99,
                    reason="一致。",
                ),
                StatementAnalysisItem(
                    line_reference="0277",
                    transaction_date=date(2026, 5, 4),
                    merchant_name="OPENAI *CHATGPT",
                    amount_jpy=Decimal("3595"),
                    original_amount=Decimal("22"),
                    original_currency="USD",
                    service_catalog_id=self.subscription_catalog.pk,
                    match_status=StatementMatchStatus.MATCHED,
                    receipt_required=True,
                    confidence=0.99,
                    reason="一致。",
                ),
            ),
        )

        process_card_statement(statement.pk)
        items = list(statement.items.order_by("sequence"))
        self.assertEqual(items[0].matched_receipt, first_receipt)
        self.assertIsNone(items[1].matched_receipt)
        self.assertTrue(items[1].needs_highlight)

        second_receipt = self.create_receipt(service=self.subscription, filename="second.pdf")
        items = list(statement.items.order_by("sequence"))
        self.assertEqual(items[0].matched_receipt, first_receipt)
        self.assertEqual(items[1].matched_receipt, second_receipt)
        self.assertNotEqual(items[0].matched_receipt_id, items[1].matched_receipt_id)

    @mock.patch("receipts.views.start_background_statement_processing")
    def test_staff_can_upload_statement_and_start_background_analysis(self, mocked_start):
        self.client.login(username="admin", password="admin-password-123")
        response = self.client.post(
            reverse("staff_upload_card_statement"),
            {
                "month": "2026-06",
                "file": SimpleUploadedFile(
                    "lifecard_meisai_user1_202606.pdf",
                    b"%PDF-1.4 statement",
                    content_type="application/pdf",
                ),
            },
        )
        self.assertRedirects(
            response,
            reverse("staff_card_statements") + "?month=2026-06",
        )
        statement = CardStatement.objects.get()
        self.assertEqual(statement.status, CardStatementStatus.PROCESSING)
        self.assertEqual(statement.original_filename, "lifecard_meisai_user1_202606.pdf")
        mocked_start.assert_called_once_with(statement.pk)

    def test_staff_month_page_shows_missing_services_and_links_to_global_statement_page(self):
        self.client.login(username="admin", password="admin-password-123")
        response = self.client.get(
            reverse("staff_user_month_status", args=[self.user.pk]) + "?month=2026-06"
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ChatGPT（サブスク）")
        self.assertContains(response, "OpenAI API（従量課金 / API）")
        self.assertContains(response, "領収書未提出")
        self.assertContains(response, "API利用確認待ち")
        self.assertContains(response, "全社ご利用代金明細との照合")
        self.assertContains(response, reverse("staff_card_statements") + "?month=2026-06")
        self.assertNotContains(response, 'type="file"')

    @mock.patch("receipts.statement_processing.generate_card_statement_analysis")
    def test_global_statement_matches_receipt_from_another_user(self, mocked_analysis):
        other_user = User.objects.create_user(
            username="other@example.com",
            email="other@example.com",
            password="password123",
        )
        other_service = RegisteredService.objects.create(
            user=other_user,
            catalog_service=self.subscription_catalog,
            name=self.subscription_catalog.name,
            billing_type=self.subscription_catalog.billing_type,
            registered_by=self.superuser,
        )
        other_submission = Submission.objects.create(
            user=other_user,
            period_month=date(2026, 6, 1),
        )
        other_receipt = Receipt.objects.create(
            submission=other_submission,
            service=other_service,
            service_name_snapshot=other_service.name,
            billing_type_snapshot=other_service.billing_type,
            original_filename="other-openai.pdf",
            generated_filename="260503_other_OpenAI_220_USD.pdf",
            amount=Decimal("220.00"),
            currency="USD",
            issued_on=date(2026, 5, 3),
            ai_extracted_payee="OPENAI *CHATGPT",
            ai_filename_status=ReceiptFilenameStatus.GENERATED,
            file=SimpleUploadedFile(
                "other-openai.pdf",
                b"%PDF-1.4 other",
                content_type="application/pdf",
            ),
            expires_at=timezone.now() + timedelta(days=30),
        )
        statement = CardStatement.objects.create(
            period_month=date(2026, 6, 1),
            file=SimpleUploadedFile("statement.pdf", b"%PDF-1.4 statement", content_type="application/pdf"),
            original_filename="statement.pdf",
            content_type="application/pdf",
            status=CardStatementStatus.PROCESSING,
            uploaded_by=self.superuser,
            expires_at=timezone.now() + timedelta(days=30),
        )
        mocked_analysis.return_value = StatementAnalysisResult(
            status=CardStatementStatus.COMPLETED,
            card_last4="7210",
            statement_period="2026-06",
            payment_date=date(2026, 6, 29),
            items=(
                StatementAnalysisItem(
                    line_reference="0276",
                    transaction_date=date(2026, 5, 3),
                    merchant_name="OPENAI *CHATGPT",
                    amount_jpy=Decimal("35949"),
                    original_amount=Decimal("220"),
                    original_currency="USD",
                    service_catalog_id=self.subscription_catalog.pk,
                    match_status=StatementMatchStatus.MATCHED,
                    receipt_required=True,
                    confidence=0.99,
                    reason="ChatGPTの請求名義と一致。",
                ),
            ),
        )

        process_card_statement(statement.pk)
        item = statement.items.get()
        self.assertEqual(item.matched_receipt, other_receipt)
        self.assertEqual(item.matched_user, other_user)
        self.assertEqual(item.matched_service, other_service)
        self.assertFalse(item.needs_highlight)

        self.client.login(username="admin", password="admin-password-123")
        response = self.client.get(reverse("staff_card_statements") + "?month=2026-06")
        self.assertContains(response, "other@example.com")
        self.assertContains(response, "260503_other_OpenAI_220_USD.pdf")
        self.assertNotContains(response, 'class="statement-missing-row"')

    @mock.patch("receipts.statement_processing.generate_card_statement_analysis")
    def test_global_statement_does_not_guess_user_when_same_service_has_multiple_users(self, mocked_analysis):
        other_user = User.objects.create_user(
            username="other@example.com",
            email="other@example.com",
            password="password123",
        )
        RegisteredService.objects.create(
            user=other_user,
            catalog_service=self.subscription_catalog,
            name=self.subscription_catalog.name,
            billing_type=self.subscription_catalog.billing_type,
            registered_by=self.superuser,
        )
        statement = CardStatement.objects.create(
            period_month=date(2026, 6, 1),
            file=SimpleUploadedFile("statement.pdf", b"%PDF-1.4 statement", content_type="application/pdf"),
            original_filename="statement.pdf",
            content_type="application/pdf",
            status=CardStatementStatus.PROCESSING,
            uploaded_by=self.superuser,
            expires_at=timezone.now() + timedelta(days=30),
        )
        mocked_analysis.return_value = StatementAnalysisResult(
            status=CardStatementStatus.COMPLETED,
            card_last4="7210",
            statement_period="2026-06",
            payment_date=date(2026, 6, 29),
            items=(
                StatementAnalysisItem(
                    line_reference="0276",
                    transaction_date=date(2026, 5, 3),
                    merchant_name="OPENAI *CHATGPT",
                    amount_jpy=Decimal("35949"),
                    original_amount=Decimal("220"),
                    original_currency="USD",
                    service_catalog_id=self.subscription_catalog.pk,
                    match_status=StatementMatchStatus.MATCHED,
                    receipt_required=True,
                    confidence=0.99,
                    reason="ChatGPT候補。",
                ),
            ),
        )

        process_card_statement(statement.pk)
        item = statement.items.get()
        self.assertIsNone(item.matched_user)
        self.assertIsNone(item.matched_service)
        self.assertEqual(item.matched_catalog_service, self.subscription_catalog)
        self.assertEqual(item.match_status, StatementMatchStatus.AMBIGUOUS)
        self.assertTrue(item.needs_highlight)
        self.assertIn("複数ユーザー", item.match_memo)

    @mock.patch("receipts.statement_processing.generate_card_statement_analysis")
    def test_global_statement_highlights_missing_receipt_row(self, mocked_analysis):
        statement = CardStatement.objects.create(
            period_month=date(2026, 6, 1),
            file=SimpleUploadedFile("statement.pdf", b"%PDF-1.4 statement", content_type="application/pdf"),
            original_filename="statement.pdf",
            content_type="application/pdf",
            status=CardStatementStatus.PROCESSING,
            uploaded_by=self.superuser,
            expires_at=timezone.now() + timedelta(days=30),
        )
        mocked_analysis.return_value = StatementAnalysisResult(
            status=CardStatementStatus.COMPLETED,
            card_last4="7210",
            statement_period="2026-06",
            payment_date=date(2026, 6, 29),
            items=(
                StatementAnalysisItem(
                    line_reference="0302",
                    transaction_date=date(2026, 5, 16),
                    merchant_name="OPENAI",
                    amount_jpy=Decimal("8236"),
                    original_amount=Decimal("49.92"),
                    original_currency="USD",
                    service_catalog_id=self.api_catalog.pk,
                    match_status=StatementMatchStatus.MATCHED,
                    receipt_required=True,
                    confidence=0.98,
                    reason="OpenAI API候補。",
                ),
            ),
        )

        process_card_statement(statement.pk)
        item = statement.items.get()
        self.assertTrue(item.needs_highlight)

        self.client.login(username="admin", password="admin-password-123")
        response = self.client.get(reverse("staff_card_statements") + "?month=2026-06")
        self.assertContains(response, 'class="statement-missing-row"')
        self.assertContains(response, "領収書未提出")
        self.assertContains(response, "黄色は領収書が必要")
        self.assertContains(response, 'data-tutorial-target="staff-statement-upload-section"')
        self.assertContains(response, 'data-tutorial-target="staff-statement-results"')

    def test_user_management_places_superuser_settings_last_and_removes_service_shortcut(self):
        self.client.login(username="admin", password="admin-password-123")
        response = self.client.get(reverse("staff_user_create"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertLess(content.index("登録アカウント"), content.index("スーパーアカウント設定"))
        self.assertNotContains(response, reverse("staff_user_services", args=[self.user.pk]))
        self.assertContains(response, reverse("staff_user_month_status", args=[self.user.pk]))

    def test_expired_statement_file_is_purged_but_metadata_remains(self):
        statement = CardStatement.objects.create(
            period_month=date(2026, 6, 1),
            file=SimpleUploadedFile("expired.pdf", b"%PDF-1.4 statement", content_type="application/pdf"),
            original_filename="expired.pdf",
            status=CardStatementStatus.COMPLETED,
            expires_at=timezone.now() - timedelta(days=1),
        )
        path = Path(statement.file.path)
        self.assertTrue(path.exists())

        call_command("purge_expired_receipts", "--noinput")
        statement.refresh_from_db()
        self.assertFalse(statement.file_available)
        self.assertIsNotNone(statement.file_deleted_at)
        self.assertFalse(path.exists())
        self.assertTrue(CardStatement.objects.filter(pk=statement.pk).exists())

    def test_staff_can_download_slack_shareable_statement_reconciliation_pdf(self):
        receipt = self.create_receipt(service=self.subscription, filename="matched.pdf")
        receipt.generated_filename = "260503_user_OpenAI_220_USD.pdf"
        receipt.save(update_fields=["generated_filename"])
        statement = CardStatement.objects.create(
            period_month=date(2026, 6, 1),
            file=SimpleUploadedFile("company-statement.pdf", b"%PDF-1.4 statement", content_type="application/pdf"),
            original_filename="company-statement.pdf",
            content_type="application/pdf",
            status=CardStatementStatus.NEEDS_REVIEW,
            card_last4="7210",
            statement_period="2026-06",
            payment_date=date(2026, 6, 29),
            ai_admin_memo="1件の領収書が未提出です。",
            uploaded_by=self.superuser,
            processed_at=timezone.now(),
            reconciled_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=30),
        )
        CardStatementItem.objects.create(
            statement=statement,
            sequence=1,
            line_reference="0276",
            transaction_date=date(2026, 5, 3),
            merchant_name="OPENAI *CHATGPT",
            amount_jpy=Decimal("35949"),
            original_amount=Decimal("220"),
            original_currency="USD",
            matched_user=self.user,
            matched_catalog_service=self.subscription_catalog,
            matched_service=self.subscription,
            matched_receipt=receipt,
            match_status=StatementMatchStatus.MATCHED,
            receipt_required=True,
            match_memo="提出済み領収書と一致しました。",
        )
        CardStatementItem.objects.create(
            statement=statement,
            sequence=2,
            line_reference="0302",
            transaction_date=date(2026, 5, 16),
            merchant_name="OPENAI",
            amount_jpy=Decimal("8236"),
            original_amount=Decimal("49.92"),
            original_currency="USD",
            matched_user=self.user,
            matched_catalog_service=self.api_catalog,
            matched_service=self.api_service,
            match_status=StatementMatchStatus.MATCHED,
            receipt_required=True,
            match_memo="対応する領収書が見つかりません。",
        )

        self.client.login(username="admin", password="admin-password-123")
        page = self.client.get(reverse("staff_card_statements") + "?month=2026-06")
        self.assertContains(page, "照合結果PDF")
        self.assertContains(page, reverse("staff_download_card_statement_report", args=[statement.pk]))

        response = self.client.get(reverse("staff_download_card_statement_report", args=[statement.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn("ReceiptHub_2026-06", response["Content-Disposition"])
        payload = b"".join(response.streaming_content)
        self.assertTrue(payload.startswith(b"%PDF-"))
        self.assertGreater(len(payload), 5000)

    def test_statement_pdf_uses_simplified_sections(self):
        from . import statement_pdf

        statement = CardStatement.objects.create(
            period_month=date(2026, 6, 1),
            file=SimpleUploadedFile("company-statement.pdf", b"%PDF-1.4 statement", content_type="application/pdf"),
            original_filename="company-statement.pdf",
            content_type="application/pdf",
            status=CardStatementStatus.NEEDS_REVIEW,
            card_last4="7210",
            statement_period="2026-06",
            payment_date=date(2026, 6, 29),
            ai_admin_memo="この解析メモはPDFに表示しません。",
            uploaded_by=self.superuser,
            processed_at=timezone.now(),
            reconciled_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=30),
        )
        CardStatementItem.objects.create(
            statement=statement,
            sequence=1,
            line_reference="0302",
            transaction_date=date(2026, 5, 16),
            merchant_name="OPENAI",
            amount_jpy=Decimal("8236"),
            original_amount=Decimal("49.92"),
            original_currency="USD",
            matched_user=self.user,
            matched_catalog_service=self.api_catalog,
            matched_service=self.api_service,
            match_status=StatementMatchStatus.MATCHED,
            receipt_required=True,
            match_memo="対応する領収書が見つかりません。",
        )

        captured_text: list[str] = []
        original_paragraph = statement_pdf.Paragraph

        def capture_paragraph(text, style, *args, **kwargs):
            captured_text.append(str(text))
            return original_paragraph(text, style, *args, **kwargs)

        with mock.patch.object(statement_pdf, "Paragraph", side_effect=capture_paragraph):
            payload = statement_pdf.build_card_statement_reconciliation_pdf(statement)

        self.assertTrue(payload.startswith(b"%PDF-"))
        rendered_text = "\n".join(captured_text)
        self.assertIn("2026年06月分", rendered_text)
        self.assertIn("未提出・確認対象", rendered_text)
        self.assertNotIn("未提出・手動確認対象", rendered_text)
        self.assertNotIn("ユーザー別 要対応サマリー", rendered_text)
        self.assertNotIn("解析メモ", rendered_text)
        self.assertNotIn("Slack共有用PDF", rendered_text)
        self.assertNotIn("生成日時:", rendered_text)
        self.assertNotIn("このPDFには複数ユーザー", rendered_text)
        self.assertNotIn("明細行", rendered_text)
        self.assertNotIn("領収書対象", rendered_text)
        self.assertNotIn("領収書確認済み", rendered_text)

    def test_processing_statement_report_is_not_downloadable(self):
        statement = CardStatement.objects.create(
            period_month=date(2026, 6, 1),
            file=SimpleUploadedFile("processing.pdf", b"%PDF-1.4 statement", content_type="application/pdf"),
            original_filename="processing.pdf",
            status=CardStatementStatus.PROCESSING,
            expires_at=timezone.now() + timedelta(days=30),
        )
        CardStatementItem.objects.create(
            statement=statement,
            sequence=1,
            merchant_name="OPENAI",
            receipt_required=True,
        )
        self.client.login(username="admin", password="admin-password-123")

        page = self.client.get(reverse("staff_card_statements") + "?month=2026-06")
        self.assertNotContains(page, reverse("staff_download_card_statement_report", args=[statement.pk]))
        response = self.client.get(reverse("staff_download_card_statement_report", args=[statement.pk]))
        self.assertEqual(response.status_code, 404)

    def test_version_file_is_present_without_web_display_requirement(self):
        self.assertEqual(Path("VERSION").read_text(encoding="utf-8").strip(), "1.3.1")
