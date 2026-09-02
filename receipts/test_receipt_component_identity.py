from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from receipts.receipt_component_identity import component_fingerprint


class ReceiptComponentIdentityTests(unittest.TestCase):
    def test_canonical_merchant_key_makes_payee_spelling_irrelevant(self):
        first = component_fingerprint(
            merchant_key="ANTHROPIC",
            payee="Anthropic, PBC",
            role="charge",
            signed_amount=Decimal("22.00"),
            currency="USD",
            event_date=date(2026, 6, 29),
            invoice_number="LN81OSYJ-0002",
            receipt_id=1,
        )
        second = component_fingerprint(
            merchant_key="ANTHROPIC",
            payee="ANTHROPIC.COM",
            role="charge",
            signed_amount=Decimal("22"),
            currency="usd",
            event_date=date(2026, 7, 1),
            invoice_number="ln81osyj-0002",
            receipt_id=999,
        )
        self.assertEqual(first, second)

    def test_original_charge_in_refund_pdf_equals_original_receipt_charge(self):
        original_receipt = component_fingerprint(
            merchant_key="ANTHROPIC",
            role="charge",
            signed_amount="22.00",
            currency="USD",
            event_date="2026-06-29",
            invoice_number="LN81OSYJ-0002",
            file_sha256="first-upload",
            source_component_key="primary",
            receipt_id=10,
        )
        embedded_payment_history = component_fingerprint(
            merchant_key="ANTHROPIC",
            role="charge",
            signed_amount="22.00",
            currency="USD",
            event_date="2026-06-29",
            invoice_number="LN81OSYJ-0002",
            file_sha256="refund-upload",
            source_component_key="original-payment",
            receipt_id=11,
        )
        self.assertEqual(original_receipt, embedded_payment_history)

    def test_charge_and_refund_components_never_share_identity(self):
        common = dict(
            merchant_key="ANTHROPIC",
            currency="USD",
            invoice_number="LN81OSYJ-0002",
            event_date="2026-07-28",
        )
        charge = component_fingerprint(role="charge", signed_amount="22.00", **common)
        refund = component_fingerprint(
            role="refund",
            signed_amount="-0.64",
            transaction_id="LN81OSYJ-0002-CN-01",
            related_transaction_id="LN81OSYJ-0002",
            **common,
        )
        self.assertNotEqual(charge, refund)

    def test_renamed_reupload_without_financial_reference_keeps_identity(self):
        first = component_fingerprint(
            merchant_key="GITHUB",
            role="refund",
            signed_amount="-11.44",
            currency="USD",
            event_date="2026-06-24",
            file_sha256="ABCDEF012345",
            source_component_key="refund-1",
            receipt_id=20,
        )
        second = component_fingerprint(
            merchant_key="GITHUB",
            role="refund",
            signed_amount="-11.44",
            currency="USD",
            event_date="2026-06-24",
            file_sha256="abcdef012345",
            source_component_key="credit-note-row-renamed-by-reanalysis",
            receipt_id=99,
        )
        self.assertEqual(first, second)

    def test_different_refunds_against_same_invoice_remain_distinct(self):
        first = component_fingerprint(
            merchant_key="ANTHROPIC",
            role="refund",
            signed_amount="-4.62",
            currency="USD",
            event_date="2026-07-07",
            invoice_number="TWYREFAM-0008",
            transaction_id="TWYREFAM-0008-CN-01",
        )
        second = component_fingerprint(
            merchant_key="ANTHROPIC",
            role="refund",
            signed_amount="-0.64",
            currency="USD",
            event_date="2026-07-28",
            invoice_number="TWYREFAM-0008",
            transaction_id="TWYREFAM-0008-CN-02",
        )
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
