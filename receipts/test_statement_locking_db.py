"""DB regressions for nullable Receipt joins and retained evidence locks.

Run against a disposable PostgreSQL test database (Django creates test DB):
    python manage.py test receipts.test_statement_locking_db -v 2
These tests do not send email or call an AI API. PostgreSQL-specific checks are
explicitly skipped on SQLite instead of pretending SQLite verifies FOR UPDATE.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.db import (DatabaseError, IntegrityError, NotSupportedError,
                       close_old_connections, connection, connections, transaction)
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext

from receipts.models import (BillingType, CardStatement, CardStatementItem,
    CardStatementReceiptEvidence, CardStatementStatus, Receipt, Submission)
from receipts.statement_processing import (
    _backfill_missing_evidence_fingerprints, _partition_global_consume_ownership)


class StatementLockingDatabaseTests(TransactionTestCase):
    def setUp(self):
        self.earlier = CardStatement.objects.create(
            period_month=date(2026, 7, 1), file="",
            status=CardStatementStatus.COMPLETED, card_last4="7210")
        self.current = CardStatement.objects.create(
            period_month=date(2026, 8, 1), file="",
            status=CardStatementStatus.COMPLETED, card_last4="7210")
        self.item = CardStatementItem.objects.create(
            statement=self.earlier, merchant_name="Anthropic",
            sequence=1, line_reference="LOCK-A", receipt_required=True,
            transaction_date=date(2026, 7, 5), original_currency="USD",
            original_amount=Decimal("22.00"))
        self.evidence = CardStatementReceiptEvidence.objects.create(
            statement_item=self.item, receipt=None, component_key="charge-test",
            component_fingerprint="", usage_mode="consume", role="charge",
            signed_amount=Decimal("22.00"), currency="USD", event_date=date(2026, 7, 5),
            filename_snapshot="test-invoice.pdf", payee_snapshot="Anthropic, PBC",
            service_label_snapshot="Claude Pro", invoice_number_snapshot="LOCK-TEST-01")

    def test_backfill_keeps_and_identifies_history_without_receipt(self):
        with transaction.atomic():
            changed = _backfill_missing_evidence_fingerprints()
        self.assertEqual(changed, 1)
        self.evidence.refresh_from_db()
        self.assertIsNone(self.evidence.receipt_id)
        self.assertEqual(len(self.evidence.component_fingerprint), 64)

    def test_backfill_also_works_when_receipt_exists(self):
        user = get_user_model().objects.create_user(username="locking-test-user")
        submission = Submission.objects.create(user=user, period_month=date(2026, 8, 1))
        receipt = Receipt.objects.create(
            submission=submission, service_name_snapshot="Claude",
            billing_type_snapshot=BillingType.SUBSCRIPTION, file="",
            file_sha256="a" * 64, original_filename="test-invoice.pdf")
        self.evidence.receipt = receipt
        self.evidence.save(update_fields=["receipt"])
        with transaction.atomic():
            self.assertEqual(_backfill_missing_evidence_fingerprints(), 1)
        self.evidence.refresh_from_db()
        self.assertEqual(self.evidence.receipt_id, receipt.pk)
        self.assertEqual(len(self.evidence.component_fingerprint), 64)

    def test_ownership_keeps_deleted_receipt_as_blocker_without_outer_join(self):
        self.evidence.component_fingerprint = "b" * 64
        self.evidence.save(update_fields=["component_fingerprint"])
        with transaction.atomic(), CaptureQueriesContext(connection) as captured:
            blockers, later = _partition_global_consume_ownership(
                self.current, manual_item_ids=set())
        self.assertIn("b" * 64, blockers)
        self.assertFalse(later)
        if connection.vendor == "postgresql":
            locked = [q["sql"] for q in captured if "FOR UPDATE" in q["sql"].upper()]
            self.assertTrue(locked)
            self.assertTrue(all("LEFT OUTER JOIN" not in sql.upper() for sql in locked))

    @skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL")
    def test_old_nullable_join_reproduces_reported_error(self):
        # Negative control: verify the environment can reproduce the original bug.
        with self.assertRaisesMessage(NotSupportedError, "nullable side of an outer join"):
            with transaction.atomic():
                list(CardStatementReceiptEvidence.objects.select_for_update()
                     .select_related("receipt", "statement_item__statement")
                     .filter(pk=self.evidence.pk))

    @skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL")
    def test_ownership_query_still_locks_evidence(self):
        self.evidence.component_fingerprint = "c" * 64
        self.evidence.save(update_fields=["component_fingerprint"])

        def competing_lock():
            close_old_connections()
            try:
                with transaction.atomic():
                    CardStatementReceiptEvidence.objects.select_for_update(nowait=True).get(
                        pk=self.evidence.pk)
                return None
            except DatabaseError as exc:
                cause = exc.__cause__
                return getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)
            finally:
                connections["default"].close()

        with transaction.atomic():
            _partition_global_consume_ownership(self.current, manual_item_ids=set())
            with ThreadPoolExecutor(max_workers=1) as executor:
                self.assertEqual(executor.submit(competing_lock).result(timeout=10), "55P03")

    def test_global_consume_uniqueness_still_rejects_duplicate(self):
        self.evidence.component_fingerprint = "d" * 64
        self.evidence.save(update_fields=["component_fingerprint"])
        other = CardStatementItem.objects.create(
            statement=self.current, merchant_name="Anthropic", sequence=1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            CardStatementReceiptEvidence.objects.create(
                statement_item=other, receipt=None, component_key="other-charge",
                component_fingerprint="d" * 64, usage_mode="consume", role="charge",
                signed_amount=Decimal("22.00"), currency="USD", filename_snapshot="copy.pdf")


# Application-level regressions for the usage-month and read-only contact hints.
# These require Django; the locked-query tests above additionally exercise PG.
import tempfile
from datetime import timedelta
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from receipts.models import (
    RegisteredService, ServiceCatalog, StatementMatchStatus, StatementMatchReason,
    ReceiptFinancialDocumentKind, UserAccountStatus, submission_month_for_receipt,
)
from receipts.statement_ai import (
    StatementAnalysisItem, StatementAnalysisResult, build_statement_result_from_payload,
)
from receipts.statement_processing import (
    process_card_statement, reconcile_card_statement_items,
    reconcile_pending_card_statement_month_semantics,
    STATEMENT_PERIOD_AND_SUBMITTER_RECONCILE_MARKER,
)


@override_settings(RECEIPT_AI_FILENAME_ENABLED=False, OPENAI_API_KEY="", OPENAI_MODEL="")
class StatementPeriodAndSubmitterDatabaseTests(TestCase):
    """No real email/API calls or production DB data are used by this suite."""

    def setUp(self):
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        setting = override_settings(MEDIA_ROOT=media.name)
        setting.enable()
        self.addCleanup(setting.disable)
        User = get_user_model()
        self.admin = User.objects.create_superuser("admin", "admin@example.com", "pw-12345678")
        self.user = User.objects.create_user("contact@example.com", "contact@example.com", "pw-12345678")
        self.catalog = ServiceCatalog.objects.create(
            name="ChatGPT", billing_type=BillingType.SUBSCRIPTION,
            merchant_aliases="OPENAI *CHATGPT, CHATGPT", created_by=self.admin)
        self.service = RegisteredService.objects.create(
            user=self.user, catalog_service=self.catalog, name="ChatGPT",
            billing_type=BillingType.SUBSCRIPTION, uses_p_card=True,
            registered_by=self.admin)

    def receipt(self, issued_on, *, amount="22.00", kind=ReceiptFinancialDocumentKind.CHARGE,
                stored_month=None):
        submission, _ = Submission.objects.get_or_create(
            user=self.user, period_month=stored_month or submission_month_for_receipt(issued_on))
        invoice = f"TEST-{issued_on.isoformat()}-{Receipt.objects.count()}"
        return Receipt.objects.create(
            submission=submission, service=self.service,
            service_name_snapshot="ChatGPT", billing_type_snapshot=BillingType.SUBSCRIPTION,
            p_card_usage_snapshot=True, original_filename=f"{invoice}.pdf",
            file=SimpleUploadedFile(f"{invoice}.pdf", b"%PDF-1.4 fixture-" + invoice.encode(),
                                    content_type="application/pdf"),
            issued_on=issued_on, amount=Decimal(amount), currency="USD",
            ai_extracted_payee="OpenAI", ai_extracted_service_label="ChatGPT",
            ai_extracted_card_last4="7210", financial_document_kind=kind,
            financial_transaction_reference=invoice,
            financial_transaction_components=[{
                "component_key": "primary", "role": "charge", "signed_amount": amount,
                "currency": "USD", "transaction_date": issued_on.isoformat(),
                "payee": "OpenAI", "service_label": "ChatGPT", "invoice_number": invoice,
                "source_label": "test", "document_kind": kind,
            }], financial_metadata_checked_at=timezone.now(), plan_change_metadata_checked_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=30))

    def statement(self, month=date(2026, 8, 1), event=date(2026, 8, 3), *, reported="2026-09"):
        s = CardStatement.objects.create(
            period_month=month, status=CardStatementStatus.NEEDS_REVIEW,
            card_last4="7210", statement_period=reported, payment_date=date(2026, 9, 28),
            uploaded_by=self.admin,
            ai_admin_memo="支払対象月は2026-09。" + STATEMENT_PERIOD_AND_SUBMITTER_RECONCILE_MARKER)
        i = CardStatementItem.objects.create(
            statement=s, sequence=1, line_reference="TEST", merchant_name="OPENAI *CHATGPT",
            transaction_date=event, original_amount=Decimal("22.00"), original_currency="USD",
            match_status=StatementMatchStatus.UNMATCHED, receipt_required=True)
        return s, i

    def test_existing_august_rows_are_corrected_without_ai_or_reupload(self):
        r = self.receipt(date(2026, 8, 3))
        s, i = self.statement()
        with mock.patch("receipts.statement_processing.generate_card_statement_analysis") as ai:
            reconcile_pending_card_statement_month_semantics(period_month=s.period_month)
        s.refresh_from_db(); i.refresh_from_db()
        ai.assert_not_called()
        self.assertEqual(s.statement_period, "2026-08")
        self.assertEqual(s.payment_date, date(2026, 9, 28))
        self.assertEqual(s.period_validation["reported_period"], "2026-09")
        self.assertIn("支払対象月は2026-09", s.period_validation["source_summary"])
        self.assertTrue(s.period_validation["valid"])
        self.assertEqual(i.matched_receipt_id, r.pk)
        self.assertEqual(i.receipt_status_label, "提出済み")
        self.assertNotIn("明細月が2026-08ではなく", s.ai_admin_memo)

    def test_wrong_usage_month_cannot_be_forced_to_selected_month(self):
        self.receipt(date(2026, 7, 3))
        s, i = self.statement(event=date(2026, 7, 3), reported="2026-08")
        reconcile_card_statement_items(s.pk)
        s.refresh_from_db(); i.refresh_from_db()
        self.assertFalse(s.period_validation["valid"])
        self.assertEqual(i.effective_match_status, StatementMatchStatus.NEEDS_REVIEW)
        self.assertFalse(i.receipt_evidences.exists())
        self.assertEqual(i.submitter_candidates, {})

    def test_previous_owner_is_reference_only_and_current_row_remains_unmatched(self):
        r = self.receipt(date(2026, 7, 3))
        old, old_i = self.statement(date(2026, 7, 1), date(2026, 7, 3), reported="2026-07")
        reconcile_card_statement_items(old.pk)
        ledger = list(CardStatementReceiptEvidence.objects.values_list(
            "pk", "statement_item_id", "component_fingerprint", "usage_mode"))
        s, i = self.statement()
        reconcile_card_statement_items(s.pk)
        i.refresh_from_db()
        self.assertEqual(i.match_status, StatementMatchStatus.UNMATCHED)
        self.assertIsNone(i.matched_receipt_id)
        self.assertIsNone(i.matched_user_id)
        self.assertFalse(i.receipt_evidences.exists())
        c = i.submitter_candidates["candidates"][0]
        self.assertEqual(c["user_id"], self.user.pk)
        self.assertEqual(c["historical_receipt_id"], r.pk)
        self.assertTrue(c["reference_only"])
        self.assertFalse(i.submitter_candidates["consumes_receipts"])
        self.assertEqual(ledger, list(CardStatementReceiptEvidence.objects.values_list(
            "pk", "statement_item_id", "component_fingerprint", "usage_mode")))

    def test_current_receipt_matches_instead_of_leaving_stale_suggestion(self):
        self.receipt(date(2026, 7, 3))
        s, i = self.statement()
        reconcile_card_statement_items(s.pk)
        current = self.receipt(date(2026, 8, 3))
        reconcile_card_statement_items(s.pk)
        i.refresh_from_db()
        self.assertEqual(i.matched_receipt_id, current.pk)
        self.assertEqual(i.submitter_candidates, {})

    def test_current_document_with_parse_problem_is_not_called_unsubmitted(self):
        self.receipt(date(2026, 7, 3))
        current = self.receipt(date(2026, 8, 3))
        Receipt.objects.filter(pk=current.pk).update(
            amount=None, financial_transaction_components=[], financial_document_kind="unknown")
        s, i = self.statement()
        reconcile_card_statement_items(s.pk)
        i.refresh_from_db()
        c = i.submitter_candidates["candidates"][0]
        self.assertEqual(c["submission_state"], "uploaded_review")
        self.assertNotEqual(i.effective_match_status, StatementMatchStatus.MATCHED)

    def test_old_receipt_reuploaded_now_is_not_a_current_document(self):
        self.receipt(date(2026, 7, 3))
        self.receipt(date(2026, 7, 3), stored_month=date(2026, 9, 1))
        s, i = self.statement()
        reconcile_card_statement_items(s.pk)
        i.refresh_from_db()
        self.assertEqual(i.submitter_candidates["candidates"][0]["submission_state"], "not_confirmed")

    def test_stopped_users_are_not_suggested(self):
        self.receipt(date(2026, 7, 3))
        self.user.profile.account_status = UserAccountStatus.STOPPED
        self.user.profile.save()
        s, i = self.statement()
        reconcile_card_statement_items(s.pk); i.refresh_from_db()
        self.assertEqual(i.submitter_candidates["candidates"], [])

    def test_non_pcard_service_is_not_suggested(self):
        self.receipt(date(2026, 7, 3))
        RegisteredService.objects.filter(pk=self.service.pk).update(uses_p_card=False)
        s, i = self.statement()
        reconcile_card_statement_items(s.pk); i.refresh_from_db()
        self.assertEqual(i.submitter_candidates["candidates"], [])

    def test_known_different_card_is_not_suggested(self):
        r = self.receipt(date(2026, 7, 3))
        Receipt.objects.filter(pk=r.pk).update(ai_extracted_card_last4="9999")
        s, i = self.statement()
        reconcile_card_statement_items(s.pk); i.refresh_from_db()
        self.assertEqual(i.submitter_candidates["candidates"], [])

    def test_service_classification_alone_never_displays_submitted(self):
        s, i = self.statement()
        i.match_status = StatementMatchStatus.MATCHED
        i.matched_catalog_service = self.catalog
        i.save()
        self.assertEqual(i.effective_match_status, StatementMatchStatus.NEEDS_REVIEW)
        self.assertEqual(i.receipt_status_label, "照合要確認")
        self.assertEqual(s.manual_review_count, 1)

    def test_extraction_stays_processing_until_reconciliation_commits(self):
        s, _ = self.statement()
        s.file = SimpleUploadedFile("test.pdf", b"%PDF-1.4 test", content_type="application/pdf")
        s.status = CardStatementStatus.PROCESSING
        s.save()
        result = StatementAnalysisResult(
            status=CardStatementStatus.COMPLETED, card_last4="7210", statement_period="2026-08",
            payment_date=date(2026, 9, 28), items=(StatementAnalysisItem(
                line_reference="TEST", transaction_date=date(2026, 8, 3), merchant_name="OpenAI",
                amount_jpy=None, original_amount=Decimal("22.00"), original_currency="USD",
                service_catalog_id=self.catalog.pk, match_status=StatementMatchStatus.MATCHED,
                receipt_required=True, confidence=0.99, reason="サービス識別のみ"),))
        with mock.patch("receipts.statement_processing.generate_card_statement_analysis", return_value=result), \
                mock.patch("receipts.statement_processing.reconcile_card_statement_items") as reconcile:
            process_card_statement(s.pk)
        s.refresh_from_db(); i = s.items.get()
        self.assertEqual(s.status, CardStatementStatus.PROCESSING)
        self.assertEqual(i.receipt_status_label, "照合中")
        self.assertNotEqual(i.match_status, StatementMatchStatus.MATCHED)
        reconcile.assert_called_once_with(s.pk, preserve_manual=False, finish_processing=True)

    def test_page_and_status_endpoint_share_updated_counts(self):
        self.receipt(date(2026, 7, 3))
        s, i = self.statement()
        reconcile_card_statement_items(s.pk)
        self.client.force_login(self.admin)
        page = self.client.get(reverse("staff_card_statements") + "?month=2026-08")
        status = self.client.get(reverse("staff_card_statement_status") + "?month=2026-08")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(page.context["stats"], status.json()["stats"])
        self.assertEqual(status.json()["stats"]["line_count"], 1)
        self.assertContains(page, "提出者候補")
        self.assertContains(page, "未確定")
        self.assertNotContains(page, '>提出済み</span>', html=False)

    def test_month_boundary_uses_confirmed_previous_card_date_only_as_reference(self):
        r = self.receipt(date(2026, 6, 30))
        old, old_i = self.statement(date(2026, 7, 1), date(2026, 7, 1), reported="2026-07")
        # The receipt belongs to the previous submission cycle. The old
        # statement contains a late June row so it is a legitimate candidate.
        CardStatementItem.objects.create(statement=old, sequence=2,
            transaction_date=date(2026, 6, 30), merchant_name="Outside", receipt_required=False)
        reconcile_card_statement_items(old.pk)
        old_i.refresh_from_db()
        self.assertEqual(old_i.matched_receipt_id, r.pk)
        s, i = self.statement(event=date(2026, 8, 1))
        reconcile_card_statement_items(s.pk); i.refresh_from_db()
        c = i.submitter_candidates["candidates"][0]
        self.assertEqual(c["historical_event_date"], "2026-07-01")
        self.assertEqual(c["historical_document_date"], "2026-06-30")
        self.assertEqual(c["historical_statement_id"], old.pk)
        self.assertFalse(i.receipt_evidences.exists())

    def test_refund_history_does_not_become_recurring_submitter_candidate(self):
        self.receipt(date(2026, 7, 3), kind=ReceiptFinancialDocumentKind.REFUND)
        s, i = self.statement()
        reconcile_card_statement_items(s.pk); i.refresh_from_db()
        self.assertEqual(i.submitter_candidates["candidates"], [])

    def test_august_first_can_match_july_last_receipt_without_extra_august_posting(self):
        r = self.receipt(date(2026, 7, 31))
        s, i = self.statement(event=date(2026, 8, 1))
        reconcile_card_statement_items(s.pk); i.refresh_from_db()
        self.assertEqual(i.matched_receipt_id, r.pk)
        self.assertEqual(i.effective_match_status, StatementMatchStatus.MATCHED)
        self.assertEqual(i.submitter_candidates, {})
