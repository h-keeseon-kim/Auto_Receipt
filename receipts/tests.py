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
from .forms import (
    ExtraReceiptUploadForm,
    ReceiptBatchUploadForm,
    ReceiptUploadForm,
    ServiceExceptionRequestForm,
    StaffReceiptReviewForm,
    StaffServiceForm,
    UserServiceRegistrationForm,
)
from .monthly_status import build_user_month_summary
from .statement_ai import StatementAnalysisItem, StatementAnalysisResult, build_statement_result_from_payload
from .statement_processing import (
    CARD_STATEMENT_MONTH_SEMANTICS_RECONCILE_MARKER,
    _available_receipts_for_statement_month,
    _registered_services_for_period,
    process_card_statement,
)
from .models import (
    BillingType,
    CardStatement,
    CardStatementItem,
    CardStatementMatchCandidate,
    CardStatementStatus,
    EmailDeliveryLog,
    EmailReminderSchedule,
    EmailDeliveryStatus,
    EmailType,
    Receipt,
    ReceiptAdminReviewStatus,
    ReceiptFilenameStatus,
    ReceiptPeriodCheckStatus,
    ReceiptUploadSource,
    ReceiptResubmissionRequest,
    RegisteredService,
    MonthlyServiceDeclaration,
    ResubmissionRequestStatus,
    ServiceCatalog,
    ServiceDeactivationSource,
    ServiceExceptionRequest,
    ServiceExceptionRequestStatus,
    ServiceRegistrationSource,
    StatementCandidateStrength,
    StatementMatchStatus,
    Submission,
    SubmissionStatus,
    UserAccountStatus,
    UserProfile,
    receipt_month_for_submission,
    receipt_month_for_statement,
    submission_month_for_receipt,
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

    def test_submission_month_maps_to_previous_receipt_month_across_year_boundary(self):
        self.assertEqual(receipt_month_for_submission(date(2026, 7, 1)), date(2026, 6, 1))
        self.assertEqual(receipt_month_for_submission(date(2026, 1, 1)), date(2025, 12, 1))
        self.assertEqual(submission_month_for_receipt(date(2026, 6, 1)), date(2026, 7, 1))

    def test_dashboard_batch_upload_uses_plus_button_without_visible_submit_button(self):
        self.client.login(username="alice", password="password123")

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-receipt-batch-upload-form")
        self.assertContains(response, "data-receipt-file-picker-button")
        self.assertContains(response, "＋")
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
            reverse("dashboard") + "?month=2026-07",
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
    def test_ai_flags_receipt_from_wrong_month_and_waits_for_admin_decision(self, mocked_generate):
        mocked_generate.return_value = ReceiptFilenameResult(
            status=ReceiptFilenameStatus.GENERATED,
            suggested_filename="260701_alice_OpenAI_220_USD.pdf",
            payee="OpenAI",
            payment_date=date(2026, 7, 1),
            amount=Decimal("220.00"),
            currency="USD",
            card_last4="7210",
        )
        self.client.login(username="alice", password="password123")
        self.client.post(
            reverse("dashboard") + "?month=2026-07",
            {
                "action": "add_receipt",
                "service": self.service.id,
                "file": SimpleUploadedFile("july.pdf", b"%PDF-1.4 july", content_type="application/pdf"),
            },
        )

        call_command("process_pending_receipts", "--limit", "10")

        receipt = Receipt.objects.get()
        self.assertEqual(receipt.ai_period_check_status, ReceiptPeriodCheckStatus.MISMATCHED)
        self.assertTrue(receipt.ai_resubmission_recommended)
        self.assertEqual(receipt.ai_filename_status, ReceiptFilenameStatus.NEEDS_REVIEW)
        self.assertIn("対象領収書月", receipt.ai_resubmission_recommendation_memo)
        self.assertIn("2026-06", receipt.ai_resubmission_recommendation_memo)
        self.assertIn("2026-07", receipt.ai_resubmission_recommendation_memo)
        self.assertFalse(ReceiptResubmissionRequest.objects.exists())

    @mock.patch("receipts.ai_processing.generate_ai_receipt_filename")
    def test_ai_recipient_name_mismatch_waits_for_admin_decision(self, mocked_generate):
        mocked_generate.return_value = ReceiptFilenameResult(
            status=ReceiptFilenameStatus.GENERATED,
            suggested_filename="260619_alice_OpenAI_220_USD.pdf",
            payee="OpenAI",
            payment_date=date(2026, 6, 19),
            amount=Decimal("220.00"),
            currency="USD",
            card_last4="7210",
            card_last4_matches_target=True,
            recipient_name="別の利用者",
            recipient_name_matches_user=False,
            recipient_name_relation_reason="対象ユーザーとは別の宛名です。",
            service_payee_related=True,
        )
        self.client.login(username="alice", password="password123")
        self.client.post(
            reverse("dashboard") + "?month=2026-07",
            {
                "action": "add_receipt",
                "service": self.service.id,
                "file": SimpleUploadedFile("recipient.pdf", b"%PDF-1.4 recipient", content_type="application/pdf"),
            },
        )

        call_command("process_pending_receipts", "--limit", "10")

        receipt = Receipt.objects.get()
        self.assertTrue(receipt.ai_resubmission_recommended)
        self.assertFalse(receipt.ai_check_recipient_name)
        self.assertIn("利用者名（宛名）", receipt.ai_resubmission_recommendation_memo)
        self.assertIn("別の利用者", receipt.ai_resubmission_recommendation_memo)
        self.assertTrue(receipt.file_available)
        self.assertFalse(ReceiptResubmissionRequest.objects.exists())

    @mock.patch("receipts.ai_processing.generate_ai_receipt_filename")
    def test_background_ai_period_mismatch_keeps_receipt_until_staff_requests_resubmission(self, mocked_generate):
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
            reverse("dashboard") + "?month=2026-07",
            {
                "action": "add_receipt",
                "service": self.service.id,
                "file": SimpleUploadedFile("may.pdf", b"%PDF-1.4 may", content_type="application/pdf"),
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Receipt.objects.count(), 1)
        self.assertNotContains(response, "対象領収書月（2026年06月）ではなく 2026年05月")
        self.assertContains(response, "AIによるファイル名修正・検査は、管理者が実行")

        call_command("process_pending_receipts", "--limit", "10")
        receipt = Receipt.objects.get()
        self.assertTrue(receipt.ai_resubmission_recommended)
        self.assertEqual(receipt.ai_period_check_status, ReceiptPeriodCheckStatus.MISMATCHED)
        self.assertFalse(ReceiptResubmissionRequest.objects.exists())
        submission = Submission.objects.get(user=self.user, period_month=date(2026, 7, 1))
        self.assertEqual(submission.status, SubmissionStatus.DRAFT)

        user_response = self.client.get(reverse("dashboard") + "?month=2026-07")
        self.assertNotContains(user_response, "再提出依頼があります")

        admin = User.objects.create_superuser(username="admin", email="admin@example.com", password="admin-password-123")
        self.client.logout()
        self.client.login(username=admin.username, password="admin-password-123")
        response = self.client.post(
            reverse("staff_request_receipt_resubmission", args=[receipt.pk]),
            {"reason": "領収書発行月が対象月と異なります。"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Receipt.objects.filter(pk=receipt.pk).exists())
        request_item = ReceiptResubmissionRequest.objects.get()
        self.assertIn("領収書発行月が対象月と異なります。", request_item.message)

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
            reverse("dashboard") + "?month=2026-07",
            {
                "action": "add_receipt",
                "service": self.service.id,
                "file": SimpleUploadedFile("wrong-payee.pdf", b"%PDF-1.4 wrong", content_type="application/pdf"),
            },
        )
        self.client.post(reverse("dashboard") + "?month=2026-07", {"action": "submit"})
        submission = Submission.objects.get(user=self.user, period_month=date(2026, 7, 1))
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
        staff_response = self.client.get(reverse("history") + "?month=2026-07")
        self.assertContains(staff_response, "manual-review-row")
        self.assertContains(staff_response, "サービス/メモ関連要確認")
        self.assertContains(staff_response, "確認")
        self.assertNotContains(staff_response, ">再提出を依頼<")
        self.assertContains(staff_response, "Anthropic")
        review_response = self.client.get(reverse("staff_receipt_review", args=[receipt.pk]))
        self.assertContains(review_response, "再提出を依頼")

        response = self.client.post(
            reverse("staff_request_receipt_resubmission", args=[receipt.pk]),
            {"next": reverse("history") + "?month=2026-07"},
        )

        self.assertRedirects(response, reverse("history") + "?month=2026-07")
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
        response = self.client.get(reverse("dashboard") + "?month=2026-07")
        self.assertContains(response, "再提出依頼があります")
        self.assertContains(response, "ChatGPT")

        response = self.client.post(
            reverse("dashboard") + "?month=2026-07",
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
            period_month=date(2026, 7, 1),
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
    def test_user_replace_receipt_file_is_flagged_if_background_detects_different_month(self, mocked_generate):
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
            period_month=date(2026, 7, 1),
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
        self.assertTrue(receipt.ai_resubmission_recommended)
        self.assertEqual(receipt.ai_period_check_status, ReceiptPeriodCheckStatus.MISMATCHED)
        self.assertIn("再提出候補", receipt.ai_resubmission_recommendation_memo)
        self.assertFalse(ReceiptResubmissionRequest.objects.exists())
        submission.refresh_from_db()
        self.assertEqual(submission.status, SubmissionStatus.SUBMITTED)

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
            reverse("dashboard") + "?month=2026-07",
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
        response = self.client.get(reverse("history") + "?month=2026-07")

        self.assertContains(response, "manual-review-row")
        self.assertContains(response, "サービス/払先関連")
        self.assertNotContains(response, ">再提出を依頼<")
        self.assertContains(response, "ChatGPT と Anthropic の関連性を断定できません")
        review_response = self.client.get(reverse("staff_receipt_review", args=[receipt.pk]))
        self.assertContains(review_response, "再提出を依頼")

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

    def test_staff_can_delete_single_resubmission_request_and_user_notification(self):
        admin = User.objects.create_superuser(username="admin", email="admin@example.com", password="admin-password-123")
        request_item = ReceiptResubmissionRequest.objects.create(
            user=self.user,
            period_month=date(2026, 7, 1),
            service_name_snapshot=self.service.name,
            billing_type_snapshot=self.service.billing_type,
            original_filename="wrong.pdf",
            display_filename="wrong.pdf",
            message="正しい領収書を再度アップロードしてください。",
            created_by=admin,
        )

        self.client.login(username=self.user.username, password="password123")
        response = self.client.get(reverse("dashboard") + "?month=2026-07")
        self.assertContains(response, "再提出依頼があります")

        self.client.logout()
        self.client.login(username=admin.username, password="admin-password-123")
        response = self.client.post(
            reverse("staff_delete_resubmission_request", args=[request_item.pk]),
            {"next": reverse("history") + "?month=2026-07"},
        )
        self.assertRedirects(response, reverse("history") + "?month=2026-07")
        self.assertFalse(ReceiptResubmissionRequest.objects.exists())

        self.client.logout()
        self.client.login(username=self.user.username, password="password123")
        response = self.client.get(reverse("dashboard") + "?month=2026-07")
        self.assertNotContains(response, "再提出依頼があります")

    def test_staff_can_delete_all_resubmission_requests_across_months(self):
        admin = User.objects.create_superuser(username="admin", email="admin@example.com", password="admin-password-123")
        ReceiptResubmissionRequest.objects.create(
            user=self.user,
            period_month=date(2026, 7, 1),
            service_name_snapshot=self.service.name,
            billing_type_snapshot=self.service.billing_type,
            message="7月分",
            created_by=admin,
        )
        ReceiptResubmissionRequest.objects.create(
            user=self.user,
            period_month=date(2026, 8, 1),
            service_name_snapshot=self.service.name,
            billing_type_snapshot=self.service.billing_type,
            message="8月分",
            status=ResubmissionRequestStatus.RESOLVED,
            created_by=admin,
            resolved_by=self.user,
            resolved_at=timezone.now(),
        )

        self.client.login(username=admin.username, password="admin-password-123")
        response = self.client.post(
            reverse("staff_delete_resubmission_requests"),
            {
                "scope": "all",
                "confirm_all": "delete_all",
                "next": reverse("history") + "?month=2026-07",
            },
        )
        self.assertRedirects(response, reverse("history") + "?month=2026-07")
        self.assertFalse(ReceiptResubmissionRequest.objects.exists())

    def test_staff_can_delete_resubmission_requests_for_selected_month_only(self):
        admin = User.objects.create_superuser(username="admin", email="admin@example.com", password="admin-password-123")
        ReceiptResubmissionRequest.objects.create(
            user=self.user,
            period_month=date(2026, 7, 1),
            service_name_snapshot=self.service.name,
            billing_type_snapshot=self.service.billing_type,
            message="7月分",
            created_by=admin,
        )
        august_request = ReceiptResubmissionRequest.objects.create(
            user=self.user,
            period_month=date(2026, 8, 1),
            service_name_snapshot=self.service.name,
            billing_type_snapshot=self.service.billing_type,
            message="8月分",
            created_by=admin,
        )

        self.client.login(username=admin.username, password="admin-password-123")
        response = self.client.post(
            reverse("staff_delete_resubmission_requests"),
            {
                "scope": "month",
                "month": "2026-07",
                "next": reverse("history") + "?month=2026-07",
            },
        )
        self.assertRedirects(response, reverse("history") + "?month=2026-07")
        self.assertFalse(ReceiptResubmissionRequest.objects.filter(period_month=date(2026, 7, 1)).exists())
        self.assertTrue(ReceiptResubmissionRequest.objects.filter(pk=august_request.pk).exists())

    def test_bulk_resubmission_delete_requires_explicit_confirmation(self):
        admin = User.objects.create_superuser(username="admin", email="admin@example.com", password="admin-password-123")
        ReceiptResubmissionRequest.objects.create(
            user=self.user,
            period_month=date(2026, 7, 1),
            service_name_snapshot=self.service.name,
            billing_type_snapshot=self.service.billing_type,
            message="確認用",
            created_by=admin,
        )
        self.client.login(username=admin.username, password="admin-password-123")
        response = self.client.post(reverse("staff_delete_resubmission_requests"), {"scope": "all"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ReceiptResubmissionRequest.objects.count(), 1)

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
        self.assertEqual(receipt.upload_source, ReceiptUploadSource.USER)
        self.assertEqual(receipt.uploaded_by, self.user)

        response = self.client.post(reverse("dashboard") + "?month=2026-06", {"action": "submit"})
        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertTrue(submission.is_submitted)

    def test_user_can_add_multiple_receipts_to_same_service_in_one_selection_and_again_later(self):
        self.client.login(username="alice", password="password123")

        response = self.client.post(
            reverse("dashboard") + "?month=2026-06",
            {
                "action": "add_receipts",
                "service": str(self.service.id),
                "files": [
                    SimpleUploadedFile("receipt-a.pdf", b"%PDF-1.4 a", content_type="application/pdf"),
                    SimpleUploadedFile("receipt-b.pdf", b"%PDF-1.4 b", content_type="application/pdf"),
                ],
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"service={self.service.id}", response["Location"])

        second_response = self.client.post(
            reverse("dashboard") + "?month=2026-06",
            {
                "action": "add_receipts",
                "service": str(self.service.id),
                "files": [SimpleUploadedFile("receipt-c.pdf", b"%PDF-1.4 c", content_type="application/pdf")],
            },
        )
        self.assertEqual(second_response.status_code, 302)

        submission = Submission.objects.get(user=self.user, period_month=date(2026, 6, 1))
        self.assertEqual(submission.receipts.count(), 3)
        self.assertEqual(
            list(submission.receipts.order_by("original_filename").values_list("original_filename", flat=True)),
            ["receipt-a.pdf", "receipt-b.pdf", "receipt-c.pdf"],
        )
        self.assertEqual(submission.receipts.filter(service=self.service).count(), 3)

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

    def test_receipt_batch_upload_form_supports_service_other_and_multiple_files(self):
        form = ReceiptBatchUploadForm(user=self.user, period_month=date(2026, 6, 1))

        self.assertEqual(list(form.fields), ["service", "memo", "files"])
        self.assertEqual(form.fields["service"].label, "サービス")
        choices = dict(form.fields["service"].choices)
        self.assertIn(str(self.service.pk), choices)
        self.assertEqual(choices[ReceiptBatchUploadForm.OTHER_VALUE], "その他")
        self.assertTrue(form.fields["files"].required)
        self.assertTrue(form.fields["files"].widget.allow_multiple_selected)

    def test_dashboard_has_other_choice_and_plus_button_in_unified_upload_form(self):
        self.client.login(username="alice", password="password123")

        response = self.client.get(reverse("dashboard") + "?month=2026-06")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="other"')
        self.assertContains(response, "その他の内容メモ")
        self.assertContains(response, "data-receipt-file-picker-button")
        self.assertContains(response, "＋")
        self.assertNotContains(response, "data-extra-upload-form")

    def test_extra_receipt_form_requires_memo(self):
        form = ExtraReceiptUploadForm(
            data={"memo": ""},
            files={"file": SimpleUploadedFile("refund.pdf", b"%PDF-1.4 refund", content_type="application/pdf")},
        )

        self.assertEqual(list(form.fields), ["memo", "file"])
        self.assertFalse(form.is_valid())
        self.assertIn("memo", form.errors)

    def test_user_can_select_other_and_upload_without_any_registered_service(self):
        self.service.delete()
        self.client.login(username="alice", password="password123")

        page = self.client.get(reverse("dashboard") + "?month=2026-06")
        self.assertContains(page, 'value="other"')

        response = self.client.post(
            reverse("dashboard") + "?month=2026-06",
            {
                "action": "add_receipts",
                "service": ReceiptBatchUploadForm.OTHER_VALUE,
                "memo": "OpenAIからの返金領収書",
                "files": [SimpleUploadedFile("refund.pdf", b"%PDF-1.4 refund", content_type="application/pdf")],
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
        self.assertEqual(summary.total_services, 0)
        self.assertEqual(summary.resolved_count, 0, "その他領収書は登録サービスの提出済み判定に流用しない")

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
        submission = Submission.objects.create(user=self.user, period_month=date(2026, 7, 1))
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
    def test_clear_extra_memo_receipt_mismatch_waits_for_staff_and_keeps_context(self, mocked_generate):
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

        receipt.refresh_from_db()
        self.assertTrue(receipt.ai_resubmission_recommended)
        self.assertIn("入力メモと領収書の内容が一致していません", receipt.ai_resubmission_recommendation_memo)
        self.assertFalse(ReceiptResubmissionRequest.objects.exists())

        admin = User.objects.create_superuser(username="admin", email="admin@example.com", password="admin-password-123")
        self.client.login(username=admin.username, password="admin-password-123")
        self.client.post(reverse("staff_request_receipt_resubmission", args=[receipt.pk]))
        request_item = ReceiptResubmissionRequest.objects.get()
        self.assertTrue(request_item.is_extra)
        self.assertEqual(request_item.receipt_memo_snapshot, "OpenAIからの返金領収書")
        self.assertIn("入力メモと領収書の内容が一致していません", request_item.message)
        self.assertNotIn("領収書は削除せず保持しています", request_item.message)

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


    def test_receipt_issue_month_query_maps_to_following_internal_submission_cycle(self):
        self.client.login(username="alice", password="password123")

        response = self.client.get(reverse("dashboard") + "?receipt_month=2026-06")

        self.assertEqual(response.status_code, 200)
        submission = Submission.objects.get(user=self.user)
        self.assertEqual(submission.period_month, date(2026, 7, 1))
        self.assertEqual(response.context["selected_receipt_month"], date(2026, 6, 1))
        self.assertContains(response, "領収書発行月")
        self.assertNotContains(response, "提出月の前月")
        self.assertNotContains(response, "ファイル保存期限")

    def test_incomplete_submit_keeps_uploaded_receipt_and_shows_yellow_reminder(self):
        RegisteredService.objects.create(
            user=self.user,
            name="Slack",
            billing_type=BillingType.SUBSCRIPTION,
        )
        self.client.login(username="alice", password="password123")
        query = "?receipt_month=2026-06"
        upload_response = self.client.post(
            reverse("dashboard") + query,
            {
                "action": "add_receipts",
                "service": str(self.service.pk),
                "files": SimpleUploadedFile(
                    "openai.pdf",
                    b"%PDF-1.4 receipt",
                    content_type="application/pdf",
                ),
            },
        )
        self.assertEqual(upload_response.status_code, 302)
        self.assertEqual(Receipt.objects.count(), 1)

        response = self.client.post(
            reverse("dashboard") + query,
            {"action": "submit"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "今回アップロードした領収書は受け付け済みです")
        self.assertContains(response, "ほかの領収書も忘れずに追加してください")
        self.assertContains(response, 'class="message warning"')
        submission = Submission.objects.get(user=self.user, period_month=date(2026, 7, 1))
        self.assertEqual(submission.status, SubmissionStatus.DRAFT)
        self.assertEqual(submission.receipts.count(), 1)

    def test_user_history_splits_required_and_usage_based_services_and_hides_file_count(self):
        subscription = RegisteredService.objects.create(
            user=self.user,
            name="Slack",
            billing_type=BillingType.SUBSCRIPTION,
        )
        submission = Submission.objects.create(user=self.user, period_month=date(2026, 7, 1))
        Receipt.objects.create(
            submission=submission,
            service=subscription,
            service_name_snapshot=subscription.name,
            billing_type_snapshot=subscription.billing_type,
            file=SimpleUploadedFile("receipt.pdf", b"%PDF-1.4 receipt", content_type="application/pdf"),
            original_filename="receipt.pdf",
        )
        MonthlyServiceDeclaration.objects.create(
            user=self.user,
            service=self.service,
            period_month=date(2026, 7, 1),
            no_usage=True,
            declared_by=self.user,
        )
        self.client.login(username="alice", password="password123")

        response = self.client.get(reverse("history"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "領収書発行月")
        self.assertContains(response, "サービス登録数")
        self.assertContains(response, "必須提出（サブスク等）")
        self.assertContains(response, "利用時提出（API等）")
        self.assertContains(response, "対応済みサービス数")
        self.assertContains(response, "未対応サービス数")
        self.assertContains(response, "必須提出にはサブスク・一回払い・支払い種別「その他」の登録サービス")
        self.assertContains(response, "同じサービスへ複数の領収書を追加しても、1サービスとして数えます")
        self.assertNotContains(response, "領収書数")
        self.assertNotContains(response, "確認済みサービス数")
        self.assertNotContains(response, "提出日時")
        self.assertNotContains(response, "最終提出日時")

        row = response.context["history_rows"][0]
        self.assertEqual(row["required_service_count"], 1)
        self.assertEqual(row["usage_based_service_count"], 1)
        self.assertEqual(row["total_service_count"], 2)
        self.assertEqual(row["resolved_service_count"], 2)
        self.assertEqual(row["remaining_service_count"], 0)

    def test_ai_payload_extracts_recipient_name_as_independent_review_item(self):
        result = build_result_from_ai_payload(
            {
                "card_last4": "7210",
                "card_last4_matches_target": True,
                "payee": "OpenAI",
                "recipient_name": "Alice Example",
                "recipient_name_matches_user": True,
                "recipient_name_relation_reason": "登録ユーザー名と一致しました。",
                "filename_label": "OpenAI",
                "service_payee_related": True,
                "service_payee_relation_reason": "ChatGPTの請求元です。",
                "payment_date": "2026-06-19",
                "amount": "220.00",
                "currency": "USD",
                "can_create_filename": True,
                "confidence": 0.98,
                "reason": "",
            },
            original_filename="receipt.pdf",
            user_filename_part="alice",
        )

        self.assertEqual(result.recipient_name, "Alice Example")
        self.assertIs(result.recipient_name_matches_user, True)
        self.assertEqual(result.status, ReceiptFilenameStatus.GENERATED)

        prompt = build_openai_content(
            file_bytes=b"%PDF-1.4 receipt",
            original_filename="receipt.pdf",
            content_type="application/pdf",
            service_display_name="ChatGPT（サブスク）",
            user_filename_part="alice",
            expected_recipient_context="登録氏名=Alice Example / アカウント=alice@example.com",
        )[1]["text"]
        self.assertIn("利用者名・宛名との照合", prompt)
        self.assertIn("登録氏名=Alice Example", prompt)
        self.assertIn("recipient_name_matches_user", prompt)
        self.assertIn("ご使用者氏名", prompt)


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

    def test_staff_can_proxy_upload_multiple_receipts_for_user_service(self):
        service = RegisteredService.objects.create(
            user=self.user,
            catalog_service=self.catalog,
            name=self.catalog.name,
            billing_type=self.catalog.billing_type,
        )
        MonthlyServiceDeclaration.objects.create(
            user=self.user,
            service=service,
            period_month=date(2026, 6, 1),
            no_usage=True,
            declared_by=self.user,
        )
        self.client.login(username="admin", password="admin-password-123")

        page = self.client.get(
            reverse("staff_user_month_status", args=[self.user.pk]) + "?month=2026-06"
        )
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "管理者代理アップロード")
        self.assertContains(page, "領収書を代理アップロード")
        self.assertContains(page, service.display_name)

        response = self.client.post(
            reverse("staff_user_month_status", args=[self.user.pk]),
            {
                "action": "staff_add_receipts",
                "month": "2026-06",
                "service": str(service.pk),
                "files": [
                    SimpleUploadedFile("admin-a.pdf", b"%PDF-1.4 admin a", content_type="application/pdf"),
                    SimpleUploadedFile("admin-b.pdf", b"%PDF-1.4 admin b", content_type="application/pdf"),
                ],
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("month=2026-06", response["Location"])
        self.assertIn(f"service={service.pk}", response["Location"])
        self.assertIn("uploaded=2", response["Location"])
        submission = Submission.objects.get(user=self.user, period_month=date(2026, 6, 1))
        receipts = list(submission.receipts.order_by("original_filename"))
        self.assertEqual([receipt.original_filename for receipt in receipts], ["admin-a.pdf", "admin-b.pdf"])
        self.assertTrue(all(receipt.upload_source == ReceiptUploadSource.ADMIN for receipt in receipts))
        self.assertTrue(all(receipt.uploaded_by == self.admin for receipt in receipts))
        self.assertTrue(all(receipt.ai_filename_status == ReceiptFilenameStatus.NOT_PROCESSED for receipt in receipts))
        self.assertFalse(
            MonthlyServiceDeclaration.objects.filter(
                user=self.user,
                service=service,
                period_month=date(2026, 6, 1),
            ).exists()
        )

        self.client.logout()
        self.client.login(username="user@example.com", password="password123")
        user_page = self.client.get(reverse("dashboard") + "?month=2026-06")
        self.assertContains(user_page, "admin-a.pdf")
        self.assertContains(user_page, "admin-b.pdf")
        self.assertContains(user_page, "管理者代理アップロード")

    def test_staff_proxy_upload_page_switches_directly_between_users(self):
        self.client.login(username="admin", password="admin-password-123")

        response = self.client.get(
            reverse("staff_user_month_status", args=[self.other_user.pk])
            + "?month=2026-06&uploaded=1#staff-receipt-upload"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "代理アップロード対象")
        self.assertContains(response, "選択したユーザーへ")
        self.assertContains(response, "次のユーザー")
        self.assertContains(response, "1件の代理アップロードが完了しました")
        expected_url = (
            reverse("staff_user_month_status", args=[self.user.pk])
            + "?month=2026-06#staff-receipt-upload"
        )
        self.assertContains(response, expected_url)

    def test_staff_proxy_upload_to_submitted_month_returns_it_to_draft(self):
        service = RegisteredService.objects.create(
            user=self.user,
            catalog_service=self.catalog,
            name=self.catalog.name,
            billing_type=self.catalog.billing_type,
        )
        submission = Submission.objects.create(
            user=self.user,
            period_month=date(2026, 6, 1),
            status=SubmissionStatus.SUBMITTED,
            submitted_at=timezone.now(),
        )
        self.client.login(username="admin", password="admin-password-123")

        response = self.client.post(
            reverse("staff_user_month_status", args=[self.user.pk]),
            {
                "action": "staff_add_receipts",
                "month": "2026-06",
                "service": str(service.pk),
                "files": [SimpleUploadedFile("late.pdf", b"%PDF-1.4 late", content_type="application/pdf")],
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        submission.refresh_from_db()
        self.assertEqual(submission.status, SubmissionStatus.DRAFT)
        self.assertIsNone(submission.submitted_at)
        self.assertContains(response, "未提出領収書あり")

    def test_staff_proxy_upload_other_receipt_requires_memo_and_records_admin_source(self):
        self.client.login(username="admin", password="admin-password-123")
        url = reverse("staff_user_month_status", args=[self.user.pk])

        invalid = self.client.post(
            url,
            {
                "action": "staff_add_receipts",
                "month": "2026-06",
                "service": ReceiptBatchUploadForm.OTHER_VALUE,
                "files": [SimpleUploadedFile("refund.pdf", b"%PDF-1.4 refund", content_type="application/pdf")],
            },
        )
        self.assertEqual(invalid.status_code, 200)
        self.assertContains(invalid, "どのような領収書かをメモに入力してください")
        self.assertFalse(Receipt.objects.exists())

        valid = self.client.post(
            url,
            {
                "action": "staff_add_receipts",
                "month": "2026-06",
                "service": ReceiptBatchUploadForm.OTHER_VALUE,
                "memo": "OpenAIからの返金領収書",
                "files": [SimpleUploadedFile("refund.pdf", b"%PDF-1.4 refund", content_type="application/pdf")],
            },
        )
        self.assertEqual(valid.status_code, 302)
        receipt = Receipt.objects.get()
        self.assertTrue(receipt.is_extra)
        self.assertEqual(receipt.memo, "OpenAIからの返金領収書")
        self.assertEqual(receipt.upload_source, ReceiptUploadSource.ADMIN)
        self.assertEqual(receipt.uploaded_by, self.admin)

    def test_staff_proxy_upload_rejects_service_registered_to_another_user(self):
        other_service = RegisteredService.objects.create(
            user=self.other_user,
            name="Claude",
            billing_type=BillingType.SUBSCRIPTION,
        )
        self.client.login(username="admin", password="admin-password-123")

        response = self.client.post(
            reverse("staff_user_month_status", args=[self.user.pk]),
            {
                "action": "staff_add_receipts",
                "month": "2026-06",
                "service": str(other_service.pk),
                "files": [SimpleUploadedFile("wrong-user.pdf", b"%PDF-1.4 wrong", content_type="application/pdf")],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "候補にありません")
        self.assertFalse(Receipt.objects.exists())

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

    def test_staff_catalog_shows_which_active_users_use_each_service(self):
        RegisteredService.objects.create(
            user=self.user,
            catalog_service=self.catalog,
            name=self.catalog.name,
            billing_type=self.catalog.billing_type,
            uses_p_card=True,
        )
        RegisteredService.objects.create(
            user=self.other_user,
            catalog_service=self.catalog,
            name=self.catalog.name,
            billing_type=self.catalog.billing_type,
            uses_p_card=False,
        )
        stopped_user = User.objects.create_user(
            username="stopped@example.com",
            email="stopped@example.com",
            password="password123",
        )
        RegisteredService.objects.create(
            user=stopped_user,
            catalog_service=self.catalog,
            name=self.catalog.name,
            billing_type=self.catalog.billing_type,
            is_active=False,
        )
        self.client.login(username="admin", password="admin-password-123")

        response = self.client.get(reverse("staff_services") + "?tab=catalog")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "利用者を表示")
        self.assertContains(response, "2人")
        self.assertContains(response, self.user.username)
        self.assertContains(response, self.other_user.username)
        self.assertContains(response, "Pカード利用")
        self.assertContains(response, "Pカード未使用")
        self.assertContains(response, f"?tab=users&amp;user={self.user.pk}")
        self.assertNotContains(response, stopped_user.username)

        catalog = next(
            item for item in response.context["catalog_page_obj"].object_list if item.pk == self.catalog.pk
        )
        self.assertEqual(
            [assignment.user_id for assignment in catalog.active_user_assignments],
            [self.other_user.pk, self.user.pk],
        )

    def test_global_button_styles_keep_japanese_labels_on_one_line(self):
        css_path = Path(__file__).resolve().parents[1] / "static" / "css" / "app.css"
        css = css_path.read_text(encoding="utf-8")
        button_rule = css.split("\n.button {\n", 1)[1].split("}", 1)[0]

        self.assertIn("white-space: nowrap", button_rule)
        self.assertIn("word-break: keep-all", button_rule)
        self.assertIn("flex: 0 0 auto", button_rule)

        self.client.login(username=self.user.username, password="password123")
        response = self.client.get(reverse("user_services"))
        self.assertContains(response, ">サービス利用登録</a>")

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
                "uses_p_card": "true",
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
        self.assertContains(response, 'value="other"')
        choices = dict(response.context["upload_form"].fields["service"].choices)
        self.assertNotIn(str(service.pk), choices)
        self.assertEqual(choices[ReceiptBatchUploadForm.OTHER_VALUE], "その他")

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
                    "uses_p_card": "true",
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
        self.catalog = ServiceCatalog.objects.create(
            name="Slack",
            billing_type=BillingType.SUBSCRIPTION,
            created_by=self.admin,
        )

    def submit_exception_request(self, *, name="Figma", billing_type=BillingType.SUBSCRIPTION, purpose="デザイン制作", uses_p_card=True):
        self.client.login(username="user@example.com", password="password123")
        return self.client.post(
            reverse("service_exception_request_create"),
            {
                "service_name": name,
                "billing_type": billing_type,
                "uses_p_card": "true" if uses_p_card else "false",
                "purpose": purpose,
            },
        )

    def test_user_registers_existing_service_master_without_exception_approval(self):
        self.client.login(username="user@example.com", password="password123")

        response = self.client.get(reverse("user_service_create"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "サービス利用登録")
        self.assertContains(response, "Slack（サブスク）")
        self.assertNotContains(response, 'name="service_name"')
        self.assertNotContains(response, 'name="billing_type"')

        response = self.client.post(
            reverse("user_service_create"),
            {"catalog_service": self.catalog.pk, "uses_p_card": "true", "memo": "チーム連絡に利用"},
        )

        self.assertRedirects(response, reverse("user_services"))
        service = RegisteredService.objects.get(user=self.user, catalog_service=self.catalog)
        self.assertTrue(service.is_active)
        self.assertEqual(service.registration_source, ServiceRegistrationSource.USER)
        self.assertEqual(service.registered_by, self.user)
        self.assertEqual(service.memo, "チーム連絡に利用")
        self.assertFalse(ServiceExceptionRequest.objects.filter(user=self.user).exists())

    def test_p_card_choice_is_required_when_user_registers_service(self):
        self.client.login(username="user@example.com", password="password123")

        response = self.client.post(
            reverse("user_service_create"),
            {"catalog_service": self.catalog.pk},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pカードを利用するか選択してください")
        self.assertFalse(RegisteredService.objects.filter(user=self.user, catalog_service=self.catalog).exists())

    def test_new_service_forms_do_not_preselect_p_card_usage(self):
        registration_form = UserServiceRegistrationForm(user=self.user)
        exception_form = ServiceExceptionRequestForm(user=self.user)

        staff_form = StaffServiceForm(registered_by=self.admin)
        self.assertIn(registration_form["uses_p_card"].value(), (None, ""))
        self.assertEqual(exception_form["uses_p_card"].value(), "")
        self.assertEqual(staff_form["uses_p_card"].value(), "")
        self.assertEqual(list(registration_form.fields["uses_p_card"].choices)[0][0], "")
        self.assertEqual(list(exception_form.fields["uses_p_card"].choices)[0][0], "")

    def test_user_can_register_non_p_card_service_and_change_setting_later(self):
        self.client.login(username="user@example.com", password="password123")
        response = self.client.post(
            reverse("user_service_create"),
            {"catalog_service": self.catalog.pk, "uses_p_card": "false", "memo": "個人カードで支払い"},
        )
        self.assertRedirects(response, reverse("user_services"))
        service = RegisteredService.objects.get(user=self.user, catalog_service=self.catalog)
        self.assertFalse(service.uses_p_card)

        service_page = self.client.get(reverse("user_services"))
        self.assertContains(service_page, "Pカード未使用")
        self.assertContains(service_page, reverse("user_service_p_card_update", args=[service.pk]))

        summary = build_user_month_summary(self.user, date(2026, 7, 1))
        self.assertEqual(summary.total_services, 0)
        self.assertEqual(summary.non_p_card_service_count, 1)
        upload_page = self.client.get(reverse("dashboard") + "?receipt_month=2026-06")
        choices = dict(upload_page.context["upload_form"].fields["service"].choices)
        self.assertNotIn(str(service.pk), choices)
        self.assertContains(upload_page, "Pカード未使用サービスは提出不要です")
        self.assertEqual(upload_page.context["display_status_label"], "提出不要")
        self.assertFalse(upload_page.context["can_submit"])
        self.assertNotContains(upload_page, ">提出する</button>")

        history_page = self.client.get(reverse("history"))
        self.assertContains(history_page, "提出不要")
        self.assertContains(history_page, "提出不要（Pカード未使用）")

        response = self.client.post(
            reverse("user_service_p_card_update", args=[service.pk]),
            {"uses_p_card": "true"},
        )
        self.assertRedirects(response, reverse("user_services"))
        service.refresh_from_db()
        self.assertTrue(service.uses_p_card)
        summary = build_user_month_summary(self.user, date(2026, 7, 1))
        self.assertEqual(summary.total_services, 1)
        self.assertEqual(summary.non_p_card_service_count, 0)

    def test_exception_request_p_card_setting_is_applied_on_approval(self):
        self.submit_exception_request(name="New Non Card Tool", uses_p_card=False)
        request_item = ServiceExceptionRequest.objects.get(user=self.user, service_name="New Non Card Tool")
        self.assertFalse(request_item.uses_p_card)
        self.client.logout()
        self.client.login(username="admin", password="admin-password-123")

        response = self.client.post(
            reverse("staff_exception_requests"),
            {"request_id": request_item.pk, "decision": "approve", "review_note": "承認"},
        )

        self.assertRedirects(response, reverse("staff_exception_requests") + "?status=pending")
        request_item.refresh_from_db()
        service = request_item.approved_registered_service
        self.assertIsNotNone(service)
        self.assertFalse(service.uses_p_card)

    def test_service_registration_contains_exception_request_action_and_back_path(self):
        self.client.login(username="user@example.com", password="password123")
        response = self.client.get(reverse("user_service_create"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "サービス利用登録")
        self.assertContains(response, "一覧にサービスがない場合")
        self.assertContains(response, "新規サービス例外申請")
        self.assertContains(response, reverse("service_exception_request_create"))
        self.assertContains(response, 'data-tutorial-target="service-exception-button"')

        exception_page = self.client.get(reverse("service_exception_request_create"))
        self.assertEqual(exception_page.status_code, 200)
        self.assertContains(exception_page, "サービス利用登録へ戻る")
        self.assertContains(exception_page, reverse("user_service_create"))

    def test_exception_request_rejects_service_already_in_active_catalog(self):
        self.client.login(username="user@example.com", password="password123")
        response = self.client.post(
            reverse("service_exception_request_create"),
            {
                "service_name": "slack",
                "billing_type": BillingType.SUBSCRIPTION,
                "uses_p_card": "true",
                "purpose": "チーム連絡",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "サービスマスターに登録済みです")
        self.assertContains(response, "サービス利用登録")
        self.assertFalse(ServiceExceptionRequest.objects.filter(user=self.user).exists())

    def test_exception_request_rejects_inactive_catalog_and_asks_admin_reactivation(self):
        ServiceCatalog.objects.create(
            name="Old Service",
            billing_type=BillingType.OTHER,
            is_active=False,
            created_by=self.admin,
        )
        self.client.login(username="user@example.com", password="password123")
        response = self.client.post(
            reverse("service_exception_request_create"),
            {
                "service_name": "old service",
                "billing_type": BillingType.OTHER,
                "uses_p_card": "true",
                "purpose": "既存契約の再開",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "現在は選択停止中です")
        self.assertFalse(ServiceExceptionRequest.objects.filter(user=self.user).exists())

    def test_user_service_registration_form_excludes_active_and_inactive_catalogs(self):
        inactive_catalog = ServiceCatalog.objects.create(
            name="Inactive Tool",
            billing_type=BillingType.OTHER,
            is_active=False,
            created_by=self.admin,
        )
        active_service = RegisteredService.objects.create(
            user=self.user,
            catalog_service=self.catalog,
            name=self.catalog.name,
            billing_type=self.catalog.billing_type,
            registration_source=ServiceRegistrationSource.USER,
            registered_by=self.user,
        )

        form = UserServiceRegistrationForm(user=self.user)
        catalog_ids = set(form.fields["catalog_service"].queryset.values_list("id", flat=True))

        self.assertNotIn(active_service.catalog_service_id, catalog_ids)
        self.assertNotIn(inactive_catalog.pk, catalog_ids)

    def test_user_can_register_same_service_name_with_different_billing_types(self):
        subscription = ServiceCatalog.objects.create(
            name="ChatGPT",
            billing_type=BillingType.SUBSCRIPTION,
            created_by=self.admin,
        )
        metered = ServiceCatalog.objects.create(
            name="ChatGPT",
            billing_type=BillingType.METERED,
            created_by=self.admin,
        )
        self.client.login(username="user@example.com", password="password123")

        first = self.client.post(reverse("user_service_create"), {"catalog_service": subscription.pk, "uses_p_card": "true"})
        second = self.client.post(reverse("user_service_create"), {"catalog_service": metered.pk, "uses_p_card": "true"})

        self.assertRedirects(first, reverse("user_services"))
        self.assertRedirects(second, reverse("user_services"))
        services = RegisteredService.objects.filter(user=self.user, name="ChatGPT")
        self.assertEqual(services.count(), 2)
        self.assertEqual(
            set(services.values_list("billing_type", flat=True)),
            {BillingType.SUBSCRIPTION, BillingType.METERED},
        )

    def test_user_registration_reactivates_stopped_service_without_exception_request(self):
        stopped = RegisteredService.objects.create(
            user=self.user,
            catalog_service=self.catalog,
            name=self.catalog.name,
            billing_type=self.catalog.billing_type,
            is_active=False,
            registration_source=ServiceRegistrationSource.USER,
            registered_by=self.user,
            deactivation_source=ServiceDeactivationSource.USER,
            deactivated_by=self.user,
            final_receipt_month=date(2026, 6, 1),
            stop_note="一時停止",
        )
        self.client.login(username="user@example.com", password="password123")

        response = self.client.post(reverse("user_service_create"), {"catalog_service": self.catalog.pk, "uses_p_card": "true"})

        self.assertRedirects(response, reverse("user_services"))
        stopped.refresh_from_db()
        self.assertTrue(stopped.is_active)
        self.assertEqual(stopped.deactivation_source, "")
        self.assertIsNone(stopped.final_receipt_month)
        self.assertEqual(stopped.stop_note, "")
        self.assertFalse(ServiceExceptionRequest.objects.filter(user=self.user).exists())

    def test_exception_application_form_requires_service_payment_method_and_purpose(self):
        form = ServiceExceptionRequestForm(
            data={"service_name": "", "billing_type": "", "uses_p_card": "", "purpose": ""},
            user=self.user,
        )

        self.assertEqual(list(form.fields), ["service_name", "billing_type", "uses_p_card", "purpose"])
        self.assertFalse(form.is_valid())
        self.assertIn("service_name", form.errors)
        self.assertIn("billing_type", form.errors)
        self.assertIn("uses_p_card", form.errors)
        self.assertIn("purpose", form.errors)

    def test_user_submits_exception_request_without_immediate_service_registration(self):
        response = self.submit_exception_request()

        self.assertRedirects(response, reverse("user_services"))
        request_item = ServiceExceptionRequest.objects.get(user=self.user)
        self.assertEqual(request_item.service_name, "Figma")
        self.assertEqual(request_item.billing_type, BillingType.SUBSCRIPTION)
        self.assertEqual(request_item.purpose, "デザイン制作")
        self.assertEqual(request_item.status, ServiceExceptionRequestStatus.PENDING)
        self.assertFalse(RegisteredService.objects.filter(user=self.user, name="Figma").exists())

        page = self.client.get(reverse("user_services"))
        self.assertContains(page, "例外申請履歴")
        self.assertContains(page, "サービス利用登録")
        self.assertContains(page, "確認待ち")
        self.assertContains(page, "Figma")

    def test_staff_gets_pending_notice_and_can_approve_request(self):
        self.submit_exception_request(name="Figma", purpose="UIデザインと共同編集")
        self.client.logout()
        self.client.login(username="admin", password="admin-password-123")

        history = self.client.get(reverse("history"))
        self.assertContains(history, "例外申請")
        self.assertContains(history, "未対応のサービス例外申請が 1 件あります")

        request_item = ServiceExceptionRequest.objects.get(user=self.user)
        response = self.client.post(
            reverse("staff_exception_requests") + "?status=pending",
            {
                "request_id": request_item.pk,
                "decision": "approve",
                "review_note": "業務利用を承認",
            },
        )
        self.assertRedirects(response, reverse("staff_exception_requests") + "?status=pending")

        request_item.refresh_from_db()
        self.assertEqual(request_item.status, ServiceExceptionRequestStatus.APPROVED)
        self.assertEqual(request_item.reviewed_by, self.admin)
        self.assertEqual(request_item.review_note, "業務利用を承認")
        service = RegisteredService.objects.get(user=self.user, name="Figma", billing_type=BillingType.SUBSCRIPTION)
        self.assertTrue(service.is_active)
        self.assertEqual(service.registration_source, ServiceRegistrationSource.EXCEPTION_REQUEST)
        self.assertEqual(service.registered_by, self.admin)
        self.assertEqual(request_item.approved_registered_service, service)
        self.assertEqual(request_item.approved_catalog_service, service.catalog_service)
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.account_status, UserAccountStatus.ACTIVE)

    def test_approval_reuses_catalog_created_after_exception_submission(self):
        self.submit_exception_request(name="figma")
        request_item = ServiceExceptionRequest.objects.get(user=self.user)
        inactive_catalog = ServiceCatalog.objects.create(
            name="Figma",
            billing_type=BillingType.SUBSCRIPTION,
            is_active=False,
            created_by=self.admin,
        )
        self.client.logout()
        self.client.login(username="admin", password="admin-password-123")

        response = self.client.post(
            reverse("staff_exception_requests"),
            {"request_id": request_item.pk, "decision": "approve", "review_note": ""},
        )

        self.assertEqual(response.status_code, 302)
        inactive_catalog.refresh_from_db()
        self.assertTrue(inactive_catalog.is_active)
        request_item.refresh_from_db()
        self.assertEqual(request_item.approved_catalog_service, inactive_catalog)
        self.assertEqual(ServiceCatalog.objects.filter(name__iexact="Figma", billing_type=BillingType.SUBSCRIPTION).count(), 1)

    def test_staff_rejection_requires_reason_and_does_not_create_service(self):
        self.submit_exception_request(name="Unknown AI")
        request_item = ServiceExceptionRequest.objects.get(user=self.user)
        self.client.logout()
        self.client.login(username="admin", password="admin-password-123")

        invalid = self.client.post(
            reverse("staff_exception_requests"),
            {"request_id": request_item.pk, "decision": "reject", "review_note": ""},
        )
        self.assertEqual(invalid.status_code, 200)
        request_item.refresh_from_db()
        self.assertEqual(request_item.status, ServiceExceptionRequestStatus.PENDING)

        valid = self.client.post(
            reverse("staff_exception_requests"),
            {"request_id": request_item.pk, "decision": "reject", "review_note": "契約条件を確認できません。"},
        )
        self.assertRedirects(valid, reverse("staff_exception_requests") + "?status=pending")
        request_item.refresh_from_db()
        self.assertEqual(request_item.status, ServiceExceptionRequestStatus.REJECTED)
        self.assertEqual(request_item.review_note, "契約条件を確認できません。")
        self.assertFalse(RegisteredService.objects.filter(user=self.user, name="Unknown AI").exists())

    def test_duplicate_pending_request_is_blocked_but_different_billing_type_is_allowed(self):
        self.submit_exception_request(name="ChatGPT", billing_type=BillingType.SUBSCRIPTION)

        duplicate = self.client.post(
            reverse("service_exception_request_create"),
            {
                "service_name": "chatgpt",
                "billing_type": BillingType.SUBSCRIPTION,
                "uses_p_card": "true",
                "purpose": "別用途",
            },
        )
        self.assertEqual(duplicate.status_code, 200)
        self.assertContains(duplicate, "同じサービス・支払い方法の例外申請がすでに確認待ちです")

        different_type = self.client.post(
            reverse("service_exception_request_create"),
            {
                "service_name": "ChatGPT",
                "billing_type": BillingType.METERED,
                "uses_p_card": "true",
                "purpose": "API検証",
            },
        )
        self.assertRedirects(different_type, reverse("user_services"))
        self.assertEqual(ServiceExceptionRequest.objects.filter(user=self.user, service_name__iexact="ChatGPT").count(), 2)

    def test_user_can_stop_service_with_final_receipt_month_and_staff_can_see_it(self):
        service = RegisteredService.objects.create(
            user=self.user,
            catalog_service=self.catalog,
            name=self.catalog.name,
            billing_type=self.catalog.billing_type,
            registration_source=ServiceRegistrationSource.EXCEPTION_REQUEST,
            registered_by=self.admin,
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

        response = self.client.get(reverse("dashboard") + "?month=2026-07")
        self.assertContains(response, "停止済み・最終 2026-06")
        form = response.context["upload_form"]
        service_values = {value for value, _label in form.fields["service"].choices}
        self.assertIn(str(service.id), service_values)

        response = self.client.get(reverse("dashboard") + "?month=2026-08")
        form = response.context["upload_form"]
        service_values = {value for value, _label in form.fields["service"].choices}
        self.assertNotIn(str(service.id), service_values)
        self.assertIn(ReceiptBatchUploadForm.OTHER_VALUE, service_values)

        self.client.logout()
        self.client.login(username="admin", password="admin-password-123")
        response = self.client.get(reverse("staff_services") + f"?tab=users&user={self.user.pk}")
        self.assertContains(response, "ユーザー停止")
        self.assertContains(response, "2026年06月")
        self.assertContains(response, "解約済み")

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
        submission = Submission.objects.create(user=self.user, period_month=date(2026, 8, 1))
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
        registration_page = self.client.get(reverse("user_service_create"))
        self.assertContains(registration_page, 'data-tutorial-target="service-exception-button"')
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
        self.assertContains(response, f'data-user-service-create-url="{reverse("user_service_create")}"')
        self.assertContains(response, f'data-upload-url="{reverse("dashboard")}"')
        self.assertContains(response, f'data-history-url="{reverse("history")}"')
        self.assertContains(response, 'data-tutorial-target="service-stop-button"')

        upload_response = self.client.get(reverse("dashboard"))
        self.assertContains(upload_response, 'data-tutorial-target="upload-page"')

    def test_tutorial_script_forces_page_steps_and_returns_to_start_page(self):
        script = Path("static/js/tutorial.js").read_text()

        self.assertIn('pageName: "user_services"', script)
        self.assertIn('pageName: "user_service_create"', script)
        self.assertIn('pageName: "dashboard"', script)
        self.assertIn('pageName: "history"', script)
        self.assertIn("使わなくなったサービスを停止します", script)
        self.assertIn("領収書アップロードページです。", script)
        self.assertIn("提出履歴ページです。", script)
        self.assertIn("Pカードを利用するか必ず指定", script)
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
        self.assertIn("2026年05月分について", mail.outbox[0].body)
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

    def test_reminders_exclude_active_services_that_do_not_use_p_card(self):
        service = self.services[self.user_b.pk]
        service.uses_p_card = False
        service.save(update_fields=["uses_p_card", "updated_at"])
        self.user_b.profile.refresh_from_db()
        self.assertEqual(self.user_b.profile.account_status, UserAccountStatus.ACTIVE)

        call_command("send_receipt_reminders", "--kind", "initial", "--month", "2026-06")

        recipients = {message.to[0] for message in mail.outbox}
        self.assertEqual(recipients, {"a@example.com", "c@example.com"})
        self.assertFalse(EmailDeliveryLog.objects.filter(to_email="b@example.com").exists())

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

    def create_receipt(self, *, service=None, month=date(2026, 7, 1), filename="receipt.pdf"):
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

        summary = build_user_month_summary(self.user, date(2026, 7, 1))
        self.assertEqual(summary.uploaded_count, 1)
        self.assertEqual(summary.api_pending_count, 1)
        self.assertFalse(summary.is_complete)
        with self.assertRaises(ValidationError):
            submission.submit()

        MonthlyServiceDeclaration.objects.create(
            user=self.user,
            service=self.api_service,
            period_month=date(2026, 7, 1),
            no_usage=True,
            declared_by=self.user,
        )
        submission.submit()
        submission.refresh_from_db()
        self.assertTrue(submission.is_submitted)
        self.assertTrue(build_user_month_summary(self.user, date(2026, 7, 1)).is_complete)

    def test_dashboard_allows_metered_no_usage_declaration(self):
        self.client.login(username="user@example.com", password="password123")
        response = self.client.post(
            reverse("dashboard") + "?month=2026-07",
            {"action": "declare_no_usage", "service_id": self.api_service.pk},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            MonthlyServiceDeclaration.objects.filter(
                user=self.user,
                service=self.api_service,
                period_month=date(2026, 7, 1),
                no_usage=True,
            ).exists()
        )
        self.assertContains(response, "この月は利用なし")

    def test_normal_reminder_skips_api_only_pending_but_urgent_reminder_sends(self):
        self.create_receipt(service=self.subscription)

        call_command("send_receipt_reminders", "--kind", "initial", "--month", "2026-07")
        self.assertEqual(len(mail.outbox), 0)

        call_command("send_receipt_reminders", "--kind", "urgent", "--month", "2026-07")
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

        call_command("send_receipt_reminders", "--kind", "initial", "--month", "2026-07")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "2026年07月 未提出のお知らせ")
        self.assertIn("ChatGPT（サブスク）", mail.outbox[0].body)
        self.assertIn("https://receipthub.example.com/dashboard/?month=2026-07", mail.outbox[0].body)

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

    def test_statement_payload_treats_selected_month_as_statement_month(self):
        self.assertEqual(receipt_month_for_statement(date(2026, 7, 1)), date(2026, 6, 1))
        result = build_statement_result_from_payload(
            {
                "card_last4": "7210",
                "statement_period": "2026-07",
                "payment_date": "2026-07-29",
                "summary_reason": "2026年6月利用分を2026年7月に請求。",
                "items": [
                    {
                        "line_reference": "0276",
                        "transaction_date": "2026-06-03",
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
            target_month="2026-07",
            allowed_catalog_ids={self.subscription_catalog.pk, self.api_catalog.pk},
        )
        self.assertEqual(result.status, CardStatementStatus.COMPLETED)
        self.assertEqual(result.statement_period, "2026-07")
        self.assertEqual(result.payment_date, date(2026, 7, 29))
        self.assertEqual(result.items[0].transaction_date, date(2026, 6, 3))
        self.assertEqual(result.items[0].service_catalog_id, self.subscription_catalog.pk)

    @mock.patch("receipts.statement_processing.generate_card_statement_analysis")
    def test_statement_processing_highlights_missing_receipt_and_cancels_false_no_usage(self, mocked_analysis):
        MonthlyServiceDeclaration.objects.create(
            user=self.user,
            service=self.api_service,
            period_month=date(2026, 7, 1),
            no_usage=True,
            declared_by=self.user,
        )
        statement = CardStatement.objects.create(
            period_month=date(2026, 7, 1),
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
            statement_period="2026-07",
            payment_date=date(2026, 7, 29),
            items=(
                StatementAnalysisItem(
                    line_reference="0302",
                    transaction_date=date(2026, 6, 16),
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
                period_month=date(2026, 7, 1),
            ).exists()
        )
        self.assertTrue(item.needs_highlight)
        self.assertEqual(item.receipt_status_label, "領収書未提出")
        self.assertIn("対象領収書月は利用なし", item.match_memo)
        self.assertIn("利用なし", statement.ai_admin_memo)

    @mock.patch("receipts.statement_processing.generate_card_statement_analysis")
    def test_statement_processing_links_existing_receipt(self, mocked_analysis):
        receipt = self.create_receipt(service=self.subscription)
        statement = CardStatement.objects.create(
            period_month=date(2026, 7, 1),
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
            statement_period="2026-07",
            payment_date=date(2026, 7, 29),
            items=(
                StatementAnalysisItem(
                    line_reference="0276",
                    transaction_date=date(2026, 6, 3),
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
    def test_july_statement_matches_june_receipt_in_july_submission_not_august_submission(self, mocked_analysis):
        june_receipt = self.create_receipt(service=self.subscription, month=date(2026, 7, 1), filename="june.pdf")
        june_receipt.amount = Decimal("220.00")
        june_receipt.currency = "USD"
        june_receipt.issued_on = date(2026, 6, 3)
        june_receipt.ai_extracted_payee = "OPENAI *CHATGPT"
        june_receipt.save(update_fields=["amount", "currency", "issued_on", "ai_extracted_payee"])

        august_submission = Submission.objects.create(user=self.user, period_month=date(2026, 8, 1))
        august_receipt = Receipt.objects.create(
            submission=august_submission,
            service=self.subscription,
            service_name_snapshot=self.subscription.name,
            billing_type_snapshot=self.subscription.billing_type,
            original_filename="july.pdf",
            amount=Decimal("220.00"),
            currency="USD",
            issued_on=date(2026, 7, 3),
            ai_extracted_payee="OPENAI *CHATGPT",
            file=SimpleUploadedFile("july.pdf", b"%PDF-1.4 july", content_type="application/pdf"),
            expires_at=timezone.now() + timedelta(days=30),
        )
        statement = CardStatement.objects.create(
            period_month=date(2026, 7, 1),
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
            statement_period="2026-07",
            payment_date=date(2026, 7, 29),
            items=(
                StatementAnalysisItem(
                    line_reference="0276",
                    transaction_date=date(2026, 6, 3),
                    merchant_name="OPENAI *CHATGPT",
                    amount_jpy=Decimal("35949"),
                    original_amount=Decimal("220"),
                    original_currency="USD",
                    service_catalog_id=self.subscription_catalog.pk,
                    match_status=StatementMatchStatus.MATCHED,
                    receipt_required=True,
                    confidence=0.99,
                    reason="6月分ChatGPT領収書と一致。",
                ),
            ),
        )

        process_card_statement(statement.pk)
        item = statement.items.get()
        self.assertEqual(item.matched_receipt, june_receipt)
        self.assertNotEqual(item.matched_receipt, august_receipt)
        self.assertEqual(item.matched_receipt.submission.period_month, date(2026, 7, 1))

    def test_existing_statement_is_reconciled_once_with_same_month_submission_after_upgrade(self):
        june_receipt = self.create_receipt(service=self.subscription, month=date(2026, 7, 1), filename="june.pdf")
        june_receipt.amount = Decimal("220.00")
        june_receipt.currency = "USD"
        june_receipt.issued_on = date(2026, 6, 3)
        june_receipt.ai_extracted_payee = "OPENAI *CHATGPT"
        june_receipt.save(update_fields=["amount", "currency", "issued_on", "ai_extracted_payee"])

        august_submission = Submission.objects.create(user=self.user, period_month=date(2026, 8, 1))
        wrong_august_receipt = Receipt.objects.create(
            submission=august_submission,
            service=self.subscription,
            service_name_snapshot=self.subscription.name,
            billing_type_snapshot=self.subscription.billing_type,
            original_filename="wrong-august.pdf",
            amount=Decimal("220.00"),
            currency="USD",
            issued_on=date(2026, 7, 3),
            ai_extracted_payee="OPENAI *CHATGPT",
            file=SimpleUploadedFile("wrong-august.pdf", b"%PDF-1.4 wrong", content_type="application/pdf"),
            expires_at=timezone.now() + timedelta(days=30),
        )
        statement = CardStatement.objects.create(
            period_month=date(2026, 7, 1),
            file=SimpleUploadedFile("statement.pdf", b"%PDF-1.4 statement", content_type="application/pdf"),
            original_filename="statement.pdf",
            content_type="application/pdf",
            status=CardStatementStatus.NEEDS_REVIEW,
            card_last4="7210",
            statement_period="2026-07",
            payment_date=date(2026, 7, 29),
            ai_admin_memo=CARD_STATEMENT_MONTH_SEMANTICS_RECONCILE_MARKER,
            uploaded_by=self.superuser,
            expires_at=timezone.now() + timedelta(days=30),
        )
        item = CardStatementItem.objects.create(
            statement=statement,
            sequence=1,
            line_reference="0276",
            transaction_date=date(2026, 6, 3),
            merchant_name="OPENAI *CHATGPT",
            amount_jpy=Decimal("35949"),
            original_amount=Decimal("220"),
            original_currency="USD",
            matched_user=self.user,
            matched_catalog_service=self.subscription_catalog,
            matched_service=self.subscription,
            matched_receipt=wrong_august_receipt,
            match_status=StatementMatchStatus.MATCHED,
            match_confidence=0.95,
            match_memo="旧ルールによる自動照合。",
            receipt_required=True,
        )

        self.client.login(username="admin", password="admin-password-123")
        response = self.client.get(reverse("staff_card_statements") + "?month=2026-07")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ご利用代金明細月")
        self.assertContains(response, "対象領収書月")
        self.assertContains(response, "2026年06月")
        item.refresh_from_db()
        statement.refresh_from_db()
        self.assertEqual(item.matched_receipt, june_receipt)
        self.assertNotEqual(item.matched_receipt, wrong_august_receipt)
        self.assertNotIn(CARD_STATEMENT_MONTH_SEMANTICS_RECONCILE_MARKER, statement.ai_admin_memo)

    @mock.patch("receipts.statement_processing.generate_card_statement_analysis")
    def test_statement_rows_use_distinct_receipts_and_keep_extra_charge_highlighted(self, mocked_analysis):
        first_receipt = self.create_receipt(service=self.subscription, filename="first.pdf")
        statement = CardStatement.objects.create(
            period_month=date(2026, 7, 1),
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
            statement_period="2026-07",
            payment_date=date(2026, 7, 29),
            items=(
                StatementAnalysisItem(
                    line_reference="0276",
                    transaction_date=date(2026, 6, 3),
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
                    transaction_date=date(2026, 6, 4),
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
                "month": "2026-07",
                "file": SimpleUploadedFile(
                    "lifecard_meisai_user1_202607.pdf",
                    b"%PDF-1.4 statement",
                    content_type="application/pdf",
                ),
            },
        )
        self.assertRedirects(
            response,
            reverse("staff_card_statements") + "?month=2026-07",
        )
        statement = CardStatement.objects.get()
        self.assertEqual(statement.status, CardStatementStatus.PROCESSING)
        self.assertEqual(statement.original_filename, "lifecard_meisai_user1_202607.pdf")
        mocked_start.assert_called_once_with(statement.pk)

    def test_staff_month_page_shows_missing_services_and_links_to_global_statement_page(self):
        self.client.login(username="admin", password="admin-password-123")
        response = self.client.get(
            reverse("staff_user_month_status", args=[self.user.pk]) + "?month=2026-07"
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ChatGPT（サブスク）")
        self.assertContains(response, "OpenAI API（従量課金 / API）")
        self.assertContains(response, "領収書未提出")
        self.assertContains(response, "API利用確認待ち")
        self.assertContains(response, "全社ご利用代金明細との照合")
        self.assertContains(response, reverse("staff_card_statements") + "?month=2026-07")
        self.assertContains(response, "管理者代理アップロード")
        self.assertContains(response, 'type="file"')

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
            period_month=date(2026, 7, 1),
        )
        other_receipt = Receipt.objects.create(
            submission=other_submission,
            service=other_service,
            service_name_snapshot=other_service.name,
            billing_type_snapshot=other_service.billing_type,
            original_filename="other-openai.pdf",
            generated_filename="260603_other_OpenAI_220_USD.pdf",
            amount=Decimal("220.00"),
            currency="USD",
            issued_on=date(2026, 6, 3),
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
            period_month=date(2026, 7, 1),
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
            statement_period="2026-07",
            payment_date=date(2026, 7, 29),
            items=(
                StatementAnalysisItem(
                    line_reference="0276",
                    transaction_date=date(2026, 6, 3),
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
        response = self.client.get(reverse("staff_card_statements") + "?month=2026-07")
        self.assertContains(response, "other@example.com")
        self.assertContains(response, "260603_other_OpenAI_220_USD.pdf")
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
            period_month=date(2026, 7, 1),
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
            statement_period="2026-07",
            payment_date=date(2026, 7, 29),
            items=(
                StatementAnalysisItem(
                    line_reference="0276",
                    transaction_date=date(2026, 6, 3),
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
            period_month=date(2026, 7, 1),
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
            statement_period="2026-07",
            payment_date=date(2026, 7, 29),
            items=(
                StatementAnalysisItem(
                    line_reference="0302",
                    transaction_date=date(2026, 6, 16),
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
        response = self.client.get(reverse("staff_card_statements") + "?month=2026-07")
        self.assertContains(response, 'class="statement-missing-row"')
        self.assertContains(response, "領収書未提出")
        self.assertContains(response, "黄色は提出済み領収書を確定できていない行")
        self.assertContains(response, 'data-tutorial-target="staff-statement-upload-section"')
        self.assertContains(response, 'data-tutorial-target="staff-statement-results"')

    @mock.patch("receipts.statement_processing.generate_card_statement_analysis")
    def test_statement_unique_amount_candidate_counts_as_submitted_when_merchant_differs(self, mocked_analysis):
        receipt = self.create_receipt(service=self.subscription, filename="unexpected-billing.pdf")
        receipt.amount = Decimal("49.92")
        receipt.currency = "USD"
        receipt.issued_on = date(2026, 6, 16)
        receipt.ai_extracted_payee = "ANTHROPIC"
        receipt.save(update_fields=["amount", "currency", "issued_on", "ai_extracted_payee"])
        statement = CardStatement.objects.create(
            period_month=date(2026, 7, 1),
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
            statement_period="2026-07",
            payment_date=date(2026, 7, 29),
            items=(
                StatementAnalysisItem(
                    line_reference="0302",
                    transaction_date=date(2026, 6, 16),
                    merchant_name="UNRELATED BILLING NAME",
                    amount_jpy=Decimal("8236"),
                    original_amount=Decimal("49.92"),
                    original_currency="USD",
                    service_catalog_id=self.api_catalog.pk,
                    match_status=StatementMatchStatus.MATCHED,
                    receipt_required=True,
                    confidence=0.80,
                    reason="利用先からはサービスを断定できません。",
                ),
            ),
        )

        process_card_statement(statement.pk)
        item = statement.items.get()
        candidate = item.match_candidates.get(receipt=receipt)

        self.assertEqual(item.matched_receipt, receipt)
        self.assertEqual(item.match_status, StatementMatchStatus.AMBIGUOUS)
        self.assertFalse(item.needs_highlight)
        self.assertEqual(item.receipt_status_label, "領収書あり")
        self.assertEqual(statement.missing_receipt_count, 0)
        self.assertEqual(candidate.strength, StatementCandidateStrength.AMOUNT_ONLY)
        self.assertTrue(candidate.amount_match)
        self.assertFalse(candidate.merchant_match)
        self.assertIn("外貨金額", candidate.rationale)

        self.client.login(username="admin", password="admin-password-123")
        response = self.client.get(reverse("staff_card_statements") + "?month=2026-07")
        self.assertContains(response, "照合候補 1件")
        self.assertContains(response, "金額候補")
        self.assertContains(response, "現在の照合先")
        self.assertContains(response, "薄橙色は金額一致")

    @mock.patch("receipts.statement_processing.generate_card_statement_analysis")
    def test_statement_duplicate_exact_amounts_remain_candidates_without_auto_assignment(self, mocked_analysis):
        first = self.create_receipt(service=self.subscription, filename="first-22.pdf")
        first.amount = Decimal("22.00")
        first.currency = "USD"
        first.issued_on = date(2026, 6, 4)
        first.ai_extracted_payee = "FIRST VENDOR"
        first.save(update_fields=["amount", "currency", "issued_on", "ai_extracted_payee"])

        other_user = User.objects.create_user(
            username="other-amount@example.com",
            email="other-amount@example.com",
            password="password123",
        )
        other_service = RegisteredService.objects.create(
            user=other_user,
            catalog_service=self.subscription_catalog,
            name=self.subscription_catalog.name,
            billing_type=self.subscription_catalog.billing_type,
            registered_by=self.superuser,
        )
        other_submission = Submission.objects.create(user=other_user, period_month=date(2026, 7, 1))
        second = Receipt.objects.create(
            submission=other_submission,
            service=other_service,
            service_name_snapshot=other_service.name,
            billing_type_snapshot=other_service.billing_type,
            original_filename="second-22.pdf",
            amount=Decimal("22.00"),
            currency="USD",
            issued_on=date(2026, 6, 4),
            ai_extracted_payee="SECOND VENDOR",
            file=SimpleUploadedFile("second-22.pdf", b"%PDF-1.4 second", content_type="application/pdf"),
            expires_at=timezone.now() + timedelta(days=30),
        )
        statement = CardStatement.objects.create(
            period_month=date(2026, 7, 1),
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
            statement_period="2026-07",
            payment_date=date(2026, 7, 29),
            items=(
                StatementAnalysisItem(
                    line_reference="0277",
                    transaction_date=date(2026, 6, 4),
                    merchant_name="UNKNOWN MERCHANT",
                    amount_jpy=Decimal("3595"),
                    original_amount=Decimal("22.00"),
                    original_currency="USD",
                    service_catalog_id=self.api_catalog.pk,
                    match_status=StatementMatchStatus.MATCHED,
                    receipt_required=True,
                    confidence=0.80,
                    reason="利用先は不明です。",
                ),
            ),
        )

        process_card_statement(statement.pk)
        item = statement.items.get()

        self.assertIsNone(item.matched_receipt)
        self.assertTrue(item.needs_highlight)
        self.assertEqual(item.match_status, StatementMatchStatus.AMBIGUOUS)
        self.assertEqual(item.match_candidates.count(), 2)
        self.assertSetEqual(
            set(item.match_candidates.values_list("receipt_id", flat=True)),
            {first.pk, second.pk},
        )
        self.assertIn("金額が一致する領収書候補が複数", item.match_memo)

    @mock.patch("receipts.statement_processing.generate_card_statement_analysis")
    def test_statement_date_disambiguates_same_amount_candidates(self, mocked_analysis):
        exact_date = self.create_receipt(service=self.subscription, filename="exact-date.pdf")
        exact_date.amount = Decimal("22.00")
        exact_date.currency = "USD"
        exact_date.issued_on = date(2026, 6, 4)
        exact_date.ai_extracted_payee = "OTHER COMPANY"
        exact_date.save(update_fields=["amount", "currency", "issued_on", "ai_extracted_payee"])

        different_date = self.create_receipt(service=self.subscription, filename="different-date.pdf")
        different_date.amount = Decimal("22.00")
        different_date.currency = "USD"
        different_date.issued_on = date(2026, 6, 20)
        different_date.ai_extracted_payee = "ANOTHER COMPANY"
        different_date.save(update_fields=["amount", "currency", "issued_on", "ai_extracted_payee"])

        statement = CardStatement.objects.create(
            period_month=date(2026, 7, 1),
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
            statement_period="2026-07",
            payment_date=date(2026, 7, 29),
            items=(
                StatementAnalysisItem(
                    line_reference="0277",
                    transaction_date=date(2026, 6, 4),
                    merchant_name="UNKNOWN MERCHANT",
                    amount_jpy=Decimal("3595"),
                    original_amount=Decimal("22.00"),
                    original_currency="USD",
                    service_catalog_id=self.api_catalog.pk,
                    match_status=StatementMatchStatus.MATCHED,
                    receipt_required=True,
                    confidence=0.80,
                    reason="利用先は不明です。",
                ),
            ),
        )

        process_card_statement(statement.pk)
        item = statement.items.get()

        self.assertEqual(item.matched_receipt, exact_date)
        self.assertNotEqual(item.matched_receipt, different_date)
        self.assertEqual(item.match_status, StatementMatchStatus.AMBIGUOUS)
        self.assertEqual(item.match_candidates.count(), 2)
        self.assertTrue(item.match_candidates.get(receipt=exact_date).date_match)

    @mock.patch("receipts.statement_processing.generate_card_statement_analysis")
    def test_staff_can_confirm_one_of_statement_receipt_candidates(self, mocked_analysis):
        receipt = self.create_receipt(service=self.subscription, filename="candidate.pdf")
        receipt.amount = Decimal("22.00")
        receipt.currency = "USD"
        receipt.issued_on = date(2026, 6, 4)
        receipt.ai_extracted_payee = "OTHER COMPANY"
        receipt.save(update_fields=["amount", "currency", "issued_on", "ai_extracted_payee"])
        statement = CardStatement.objects.create(
            period_month=date(2026, 7, 1),
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
            statement_period="2026-07",
            payment_date=date(2026, 7, 29),
            items=(
                StatementAnalysisItem(
                    line_reference="0277",
                    transaction_date=date(2026, 6, 4),
                    merchant_name="UNKNOWN MERCHANT",
                    amount_jpy=Decimal("3595"),
                    original_amount=Decimal("22.00"),
                    original_currency="USD",
                    service_catalog_id=self.api_catalog.pk,
                    match_status=StatementMatchStatus.MATCHED,
                    receipt_required=True,
                    confidence=0.80,
                    reason="利用先は不明です。",
                ),
            ),
        )
        process_card_statement(statement.pk)
        item = statement.items.get()
        self.assertTrue(CardStatementMatchCandidate.objects.filter(item=item, receipt=receipt).exists())

        self.client.login(username="admin", password="admin-password-123")
        response = self.client.post(
            reverse("staff_update_statement_item", args=[item.pk]),
            {"item_action": "receipt", "receipt_id": receipt.pk},
        )
        self.assertRedirects(
            response,
            reverse("staff_card_statements") + "?month=2026-07#statement-1",
        )
        item.refresh_from_db()
        self.assertEqual(item.matched_receipt, receipt)
        self.assertEqual(item.match_status, StatementMatchStatus.MATCHED)
        self.assertEqual(item.match_confidence, 1.0)
        self.assertTrue(item.match_memo.startswith("管理者"))


    def test_p_card_setting_controls_statement_scope_and_can_be_changed_by_admin(self):
        self.client.login(username="admin", password="admin-password-123")
        response = self.client.post(
            reverse("staff_service_update", args=[self.subscription.pk]),
            {
                "catalog_service": self.subscription_catalog.pk,
                "uses_p_card": "false",
                "is_active": "on",
                "memo": "Pカードを利用しない契約へ変更",
            },
        )
        self.assertRedirects(response, reverse("staff_user_services", args=[self.user.pk]))
        self.subscription.refresh_from_db()
        self.assertFalse(self.subscription.uses_p_card)

        scoped_service_ids = {
            service.pk for service in _registered_services_for_period(date(2026, 7, 1))
        }
        self.assertNotIn(self.subscription.pk, scoped_service_ids)
        self.assertIn(self.api_service.pk, scoped_service_ids)

        receipt = self.create_receipt(service=self.subscription, filename="non-p-card.pdf")
        receipt.refresh_from_db()
        self.assertFalse(receipt.p_card_usage_snapshot)
        available_receipt_ids = {
            candidate.pk for candidate in _available_receipts_for_statement_month(date(2026, 7, 1))
        }
        self.assertNotIn(receipt.pk, available_receipt_ids)

        self.client.logout()
        self.client.login(username="user@example.com", password="password123")
        page = self.client.get(reverse("user_services"))
        self.assertContains(page, "Pカード未使用")
        upload_page = self.client.get(reverse("dashboard") + "?receipt_month=2026-06")
        choices = dict(upload_page.context["upload_form"].fields["service"].choices)
        self.assertNotIn(str(self.subscription.pk), choices)

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
            period_month=date(2026, 7, 1),
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
        receipt.generated_filename = "260603_user_OpenAI_220_USD.pdf"
        receipt.save(update_fields=["generated_filename"])
        statement = CardStatement.objects.create(
            period_month=date(2026, 7, 1),
            file=SimpleUploadedFile("company-statement.pdf", b"%PDF-1.4 statement", content_type="application/pdf"),
            original_filename="company-statement.pdf",
            content_type="application/pdf",
            status=CardStatementStatus.NEEDS_REVIEW,
            card_last4="7210",
            statement_period="2026-07",
            payment_date=date(2026, 7, 29),
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
            transaction_date=date(2026, 6, 3),
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
            transaction_date=date(2026, 6, 16),
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
        page = self.client.get(reverse("staff_card_statements") + "?month=2026-07")
        self.assertContains(page, "照合結果PDF")
        self.assertContains(page, reverse("staff_download_card_statement_report", args=[statement.pk]))

        response = self.client.get(reverse("staff_download_card_statement_report", args=[statement.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn("ReceiptHub_2026-07", response["Content-Disposition"])
        payload = b"".join(response.streaming_content)
        self.assertTrue(payload.startswith(b"%PDF-"))
        self.assertGreater(len(payload), 5000)

    def test_statement_pdf_uses_simplified_sections(self):
        from . import statement_pdf

        statement = CardStatement.objects.create(
            period_month=date(2026, 7, 1),
            file=SimpleUploadedFile("company-statement.pdf", b"%PDF-1.4 statement", content_type="application/pdf"),
            original_filename="company-statement.pdf",
            content_type="application/pdf",
            status=CardStatementStatus.NEEDS_REVIEW,
            card_last4="7210",
            statement_period="2026-07",
            payment_date=date(2026, 7, 29),
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
            transaction_date=date(2026, 6, 16),
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
        self.assertIn("2026年07月明細 / 対象領収書月 2026年06月", rendered_text)
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
            period_month=date(2026, 7, 1),
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

        page = self.client.get(reverse("staff_card_statements") + "?month=2026-07")
        self.assertNotContains(page, reverse("staff_download_card_statement_report", args=[statement.pk]))
        response = self.client.get(reverse("staff_download_card_statement_report", args=[statement.pk]))
        self.assertEqual(response.status_code, 404)

    def test_version_file_is_present_without_web_display_requirement(self):
        self.assertEqual(Path("VERSION").read_text(encoding="utf-8").strip(), "1.8.0")


@override_settings(PASSWORD_HASHERS=FAST_PASSWORD_HASHERS)
class DragDropAndStaffReceiptReviewTests(TestCase):
    def setUp(self):
        self.media_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.media_dir.cleanup)
        self.override = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.admin = User.objects.create_superuser(
            username="admin-review",
            email="",
            password="admin-password-123",
        )
        self.user = User.objects.create_user(
            username="review-user@example.com",
            email="review-user@example.com",
            password="user-password-123",
        )
        self.catalog = ServiceCatalog.objects.create(
            name="ChatGPT",
            billing_type=BillingType.SUBSCRIPTION,
            created_by=self.admin,
        )
        self.service = RegisteredService.objects.create(
            user=self.user,
            catalog_service=self.catalog,
            name=self.catalog.name,
            billing_type=self.catalog.billing_type,
            registered_by=self.admin,
        )
        self.submission = Submission.objects.create(
            user=self.user,
            period_month=date(2026, 7, 1),
        )
        self.receipt = Receipt.objects.create(
            submission=self.submission,
            service=self.service,
            service_name_snapshot=self.service.name,
            billing_type_snapshot=self.service.billing_type,
            file=SimpleUploadedFile("receipt.pdf", b"%PDF-1.4 receipt", content_type="application/pdf"),
            original_filename="receipt.pdf",
            content_type="application/pdf",
            file_size=18,
            uploaded_by=self.admin,
            upload_source=ReceiptUploadSource.ADMIN,
        )

    def test_receipt_and_statement_upload_pages_have_drag_drop_zones(self):
        self.client.login(username=self.user.username, password="user-password-123")
        dashboard = self.client.get(reverse("dashboard") + "?month=2026-07")
        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(dashboard, "data-file-dropzone")
        self.assertContains(dashboard, "ドラッグ＆ドロップ")

        self.client.logout()
        self.client.login(username=self.admin.username, password="admin-password-123")
        user_month = self.client.get(
            reverse("staff_user_month_status", args=[self.user.pk]) + "?month=2026-07"
        )
        self.assertContains(user_month, "代理アップロードする領収書を選択")
        self.assertContains(user_month, reverse("staff_receipt_review", args=[self.receipt.pk]))

        statement_page = self.client.get(reverse("staff_card_statements") + "?month=2026-07")
        self.assertContains(statement_page, "ご利用代金明細書を選択")
        self.assertContains(statement_page, "data-file-dropzone")

        dropzone_script = Path("static/js/file_dropzone.js").read_text(encoding="utf-8")
        self.assertIn("ここでファイルを離してください", dropzone_script)
        self.assertIn("ドロップで受け付けました", dropzone_script)
        self.assertIn("filedropzone:uploading", dropzone_script)
        dropzone_css = Path("static/css/app.css").read_text(encoding="utf-8")
        self.assertIn(".file-dropzone-drag-prompt", dropzone_css)
        self.assertIn(".file-dropzone-selection-summary", dropzone_css)

    def test_staff_review_page_previews_and_allows_manual_confirmation(self):
        self.receipt.ai_resubmission_recommended = True
        self.receipt.ai_resubmission_recommendation_memo = "AIはカード番号の不一致候補を検出しました。"
        self.receipt.save(
            update_fields=["ai_resubmission_recommended", "ai_resubmission_recommendation_memo"]
        )
        self.client.login(username=self.admin.username, password="admin-password-123")
        response = self.client.get(reverse("staff_receipt_review", args=[self.receipt.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "領収書プレビュー")
        self.assertContains(response, reverse("staff_preview_receipt", args=[self.receipt.pk]))
        self.assertContains(response, "別タブで開く")
        self.assertContains(response, "表示・ダウンロード用ファイル名")
        self.assertContains(response, "再提出不要として確認済みにする")

        payload = {
            "generated_filename": "260701_review-user_OpenAI_220_USD.pdf",
            "ai_check_card_last4": "on",
            "ai_check_payee": "on",
            "ai_check_recipient_name": "on",
            "ai_check_service_payee_related": "on",
            "ai_check_date": "on",
            "ai_check_amount": "on",
            "ai_check_currency": "on",
            "ai_check_period_match": "on",
            "admin_review_note": "領収書本体を確認しました。",
            "review_action": "confirm",
        }
        response = self.client.post(reverse("staff_receipt_review", args=[self.receipt.pk]), payload)
        self.assertRedirects(response, reverse("staff_receipt_review", args=[self.receipt.pk]))
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.admin_review_status, ReceiptAdminReviewStatus.CONFIRMED)
        self.assertEqual(self.receipt.admin_reviewed_by, self.admin)
        self.assertIsNotNone(self.receipt.admin_reviewed_at)
        self.assertTrue(self.receipt.admin_filename_overridden)
        self.assertEqual(self.receipt.generated_filename, "260701_review-user_OpenAI_220_USD.pdf")
        self.assertTrue(self.receipt.ai_all_checks_passed)
        self.assertTrue(self.receipt.ai_resubmission_recommended)
        self.assertFalse(ReceiptResubmissionRequest.objects.exists())
        self.assertIn("再提出不要と判断", self.receipt.ai_resubmission_recommendation_memo)
        self.assertFalse(self.receipt.needs_manual_review)

    def test_staff_can_confirm_ai_recommendation_without_requesting_resubmission(self):
        self.receipt.ai_filename_status = ReceiptFilenameStatus.NEEDS_REVIEW
        self.receipt.ai_filename_checked_at = timezone.now()
        self.receipt.ai_resubmission_recommended = True
        self.receipt.ai_resubmission_recommendation_memo = (
            "AIは再提出候補と判定しました。カード末尾を確認してください。"
        )
        self.receipt.save(
            update_fields=[
                "ai_filename_status",
                "ai_filename_checked_at",
                "ai_resubmission_recommended",
                "ai_resubmission_recommendation_memo",
            ]
        )
        self.client.login(username=self.admin.username, password="admin-password-123")
        payload = {
            "generated_filename": "260701_review-user_OpenAI_220_USD.pdf",
            "ai_check_card_last4": "on",
            "ai_check_payee": "on",
            "ai_check_recipient_name": "on",
            "ai_check_service_payee_related": "on",
            "ai_check_date": "on",
            "ai_check_amount": "on",
            "ai_check_currency": "on",
            "ai_check_period_match": "on",
            "admin_review_note": "",
            "review_action": "confirm",
        }
        response = self.client.post(reverse("staff_receipt_review", args=[self.receipt.pk]), payload)
        self.assertRedirects(response, reverse("staff_receipt_review", args=[self.receipt.pk]))
        self.receipt.refresh_from_db()
        self.assertTrue(Receipt.objects.filter(pk=self.receipt.pk).exists())
        self.assertFalse(ReceiptResubmissionRequest.objects.exists())
        self.assertEqual(self.receipt.admin_review_status, ReceiptAdminReviewStatus.CONFIRMED)
        self.assertTrue(self.receipt.ai_resubmission_recommended)
        self.assertIn("再提出不要", self.receipt.admin_review_note)
        self.assertIn("再提出不要と判断", self.receipt.ai_resubmission_recommendation_memo)
        self.assertFalse(self.receipt.needs_resubmission_decision)

    def test_staff_receipt_preview_allows_same_origin_iframe(self):
        self.client.login(username=self.admin.username, password="admin-password-123")
        response = self.client.get(reverse("staff_preview_receipt", args=[self.receipt.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("X-Frame-Options"), "SAMEORIGIN")
        self.assertEqual(response.headers.get("Content-Type"), "application/pdf")
        self.assertIn("inline", response.headers.get("Content-Disposition", ""))

    def test_staff_cannot_confirm_with_unchecked_items(self):
        self.client.login(username=self.admin.username, password="admin-password-123")
        response = self.client.post(
            reverse("staff_receipt_review", args=[self.receipt.pk]),
            {
                "generated_filename": "receipt.pdf",
                "ai_check_card_last4": "on",
                "review_action": "confirm",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "すべての確認項目へチェック")
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.admin_review_status, ReceiptAdminReviewStatus.NOT_REVIEWED)

    def test_processed_receipt_is_not_rechecked(self):
        self.receipt.ai_filename_status = ReceiptFilenameStatus.GENERATED
        self.receipt.ai_filename_checked_at = timezone.now()
        self.receipt.save(update_fields=["ai_filename_status", "ai_filename_checked_at"])
        self.client.login(username=self.admin.username, password="admin-password-123")
        response = self.client.post(
            reverse("staff_start_receipt_ai_processing", args=[self.receipt.pk]),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["started"])
        self.assertIn("すでにAI確認済み", response.json()["message"])

    @mock.patch("receipts.views.start_background_ai_processing")
    def test_unprocessed_receipt_can_start_single_ai_check(self, start_background):
        self.client.login(username=self.admin.username, password="admin-password-123")
        response = self.client.post(
            reverse("staff_start_receipt_ai_processing", args=[self.receipt.pk]),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertTrue(response.json()["started"])
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.ai_filename_status, ReceiptFilenameStatus.PROCESSING)
        start_background.assert_called_once_with([self.receipt.pk])

    def test_normal_user_cannot_open_staff_review(self):
        self.client.login(username=self.user.username, password="user-password-123")
        response = self.client.get(reverse("staff_receipt_review", args=[self.receipt.pk]))
        self.assertEqual(response.status_code, 302)

    def test_staff_review_form_keeps_original_extension(self):
        form = StaffReceiptReviewForm(
            {
                "generated_filename": "renamed.png",
                "review_action": "save",
            },
            receipt=self.receipt,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("元ファイルと同じ拡張子", str(form.errors))
