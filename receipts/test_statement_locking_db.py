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
