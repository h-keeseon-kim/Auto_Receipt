from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from receipts.statement_matching import (
    AmountOption,
    DOC_INVOICE,
    DOC_RECEIPT,
    DOC_REFUND,
    EvidenceComponent,
    MATCH_DIRECT,
    MATCH_LINKED_REFUND_NET,
    MATCH_MERCHANT_REFUND_NET,
    MATCH_ORIGINAL_CHARGE,
    ROLE_CHARGE,
    ROLE_REFUND,
    StatementLine,
    deduplicate_components,
    reconcile_statement,
)


def line(key: str, seq: int, day: int, merchant: str, amount: str, currency: str = "USD") -> StatementLine:
    return StatementLine(
        key=key,
        sequence=seq,
        transaction_date=date(2026, 6, day),
        merchant_key=merchant,
        amount_options=(AmountOption(Decimal(amount), currency, "statement"),),
    )


def component(
    key: str,
    receipt_id: int,
    order: int,
    day: int,
    merchant: str,
    amount: str,
    *,
    currency: str = "USD",
    role: str = ROLE_CHARGE,
    kind: str = DOC_RECEIPT,
    invoice: str = "",
    transaction: str = "",
    related: str = "",
) -> EvidenceComponent:
    return EvidenceComponent(
        key=key,
        receipt_id=receipt_id,
        receipt_order=order,
        filename=f"{key}.pdf",
        merchant_key=merchant,
        signed_amount=Decimal(amount),
        currency=currency,
        event_date=date(2026, 6, day),
        role=role,
        document_kind=kind,
        invoice_number=invoice,
        transaction_id=transaction,
        related_transaction_id=related,
        source_label="test",
        payee=merchant,
    )


class StatementMatchingEngineTests(unittest.TestCase):
    def test_global_assignment_maximises_match_count_before_date_distance(self):
        # L2 can only use C1. A greedy L1->C1 assignment would leave L2 unmatched.
        lines = [
            line("L1", 1, 10, "ANTHROPIC", "22"),
            line("L2", 2, 11, "ANTHROPIC", "22"),
        ]
        components = [
            component("C1", 1, 1, 10, "ANTHROPIC", "22"),
            component("C2", 2, 2, 9, "ANTHROPIC", "22"),
        ]
        result = reconcile_statement(lines, components, date_tolerance_days=1)
        self.assertEqual(set(result.assignments), {"L1", "L2"})
        self.assertEqual(result.assignments["L2"].component_keys, ("C1",))
        self.assertEqual(result.assignments["L1"].component_keys, ("C2",))

    def test_amount_is_exact_with_no_tolerance(self):
        result = reconcile_statement(
            [line("L", 1, 10, "ANTHROPIC", "22.00")],
            [component("C", 1, 1, 10, "ANTHROPIC", "22.01")],
        )
        self.assertNotIn("L", result.assignments)

    def test_invoice_and_paid_receipt_are_deduplicated_only_by_explicit_invoice(self):
        paid = component(
            "paid", 1, 1, 10, "ANTHROPIC", "22", kind=DOC_RECEIPT, invoice="INV-100"
        )
        invoice = component(
            "invoice", 2, 2, 10, "ANTHROPIC", "22", kind=DOC_INVOICE, invoice="INV-100"
        )
        unique, removed = deduplicate_components([invoice, paid])
        self.assertEqual([value.key for value in unique], ["paid"])
        self.assertEqual(removed, {"invoice"})

    def test_explicit_invoice_reference_deduplicates_even_if_document_dates_differ(self):
        paid = component(
            "paid", 1, 1, 11, "ANTHROPIC", "22", kind=DOC_RECEIPT, invoice="INV-101"
        )
        invoice = component(
            "invoice", 2, 2, 10, "ANTHROPIC", "22", kind=DOC_INVOICE, invoice="INV-101"
        )
        unique, removed = deduplicate_components([invoice, paid])
        self.assertEqual([value.key for value in unique], ["paid"])
        self.assertEqual(removed, {"invoice"})

    def test_same_invoice_reference_with_different_amount_is_not_deduplicated(self):
        first = component(
            "first", 1, 1, 10, "ANTHROPIC", "22", kind=DOC_RECEIPT, invoice="INV-SPLIT"
        )
        second = component(
            "second", 2, 2, 11, "ANTHROPIC", "55", kind=DOC_RECEIPT, invoice="INV-SPLIT"
        )
        unique, removed = deduplicate_components([first, second])
        self.assertEqual({value.key for value in unique}, {"first", "second"})
        self.assertFalse(removed)

    def test_invoice_can_support_statement_when_exactly_consistent(self):
        # ReceiptHub's statement view answers whether a corresponding submitted
        # document exists.  An Invoice therefore counts when merchant, exact
        # amount/currency and date all agree.
        result = reconcile_statement(
            [line("L", 1, 10, "ANTHROPIC", "22")],
            [component("invoice", 1, 1, 10, "ANTHROPIC", "22", kind=DOC_INVOICE, invoice="INV-ONLY")],
        )
        self.assertEqual(result.assignments["L"].component_keys, ("invoice",))

    def test_same_amount_and_date_without_reference_are_not_deduplicated(self):
        first = component("first", 1, 1, 10, "ANTHROPIC", "22")
        second = component("second", 2, 2, 10, "ANTHROPIC", "22")
        unique, removed = deduplicate_components([first, second])
        self.assertEqual({value.key for value in unique}, {"first", "second"})
        self.assertFalse(removed)

    def test_original_charge_inside_refund_document_can_support_statement(self):
        original = component(
            "refund-doc-original",
            10,
            1,
            2,
            "ANTHROPIC",
            "220",
            kind=DOC_REFUND,
            role=ROLE_CHARGE,
            invoice="INV-REFUND-SOURCE",
        )
        result = reconcile_statement([line("L", 1, 2, "ANTHROPIC", "220")], [original])
        self.assertEqual(result.assignments["L"].match_type, MATCH_ORIGINAL_CHARGE)

    def test_explicitly_linked_refund_is_netted_exactly(self):
        charge = component(
            "github-charge",
            1,
            1,
            2,
            "GITHUB",
            "42.90",
            transaction="SALE-123",
        )
        refund = component(
            "github-refund",
            2,
            2,
            3,
            "GITHUB",
            "-11.44",
            role=ROLE_REFUND,
            kind=DOC_REFUND,
            transaction="REFUND-456",
            related="SALE-123",
        )
        result = reconcile_statement(
            [line("L", 1, 2, "GITHUB", "31.46")],
            [charge, refund],
        )
        assignment = result.assignments["L"]
        self.assertEqual(assignment.match_type, MATCH_LINKED_REFUND_NET)
        self.assertEqual(set(assignment.component_keys), {"github-charge", "github-refund"})

    def test_same_merchant_nearby_refunds_can_explain_card_net_amount(self):
        components = [
            component("charge", 1, 1, 23, "ANTHROPIC", "137.26"),
            component("refund-a", 2, 2, 22, "ANTHROPIC", "-66.48", role=ROLE_REFUND, kind=DOC_REFUND),
            component("refund-b", 3, 3, 22, "ANTHROPIC", "-6.78", role=ROLE_REFUND, kind=DOC_REFUND),
        ]
        result = reconcile_statement([line("L", 1, 23, "ANTHROPIC", "64")], components)
        assignment = result.assignments["L"]
        self.assertEqual(assignment.match_type, MATCH_MERCHANT_REFUND_NET)
        self.assertEqual(set(assignment.component_keys), {"charge", "refund-a", "refund-b"})

    def test_unlinked_distant_refund_is_not_used_for_merchant_netting(self):
        components = [
            component("charge", 1, 1, 23, "ANTHROPIC", "137.26"),
            component("refund-a", 2, 2, 4, "ANTHROPIC", "-66.48", role=ROLE_REFUND, kind=DOC_REFUND),
            component("refund-b", 3, 3, 8, "ANTHROPIC", "-6.78", role=ROLE_REFUND, kind=DOC_REFUND),
        ]
        result = reconcile_statement([line("L", 1, 23, "ANTHROPIC", "64")], components)
        self.assertNotIn("L", result.assignments)

    def test_one_component_is_never_used_for_two_statement_lines(self):
        lines = [line(f"L{i}", i, i, "ANTHROPIC", "22") for i in range(1, 21)]
        components = [component(f"C{i}", i, i, i, "ANTHROPIC", "22") for i in range(1, 20)]
        result = reconcile_statement(lines, components)
        self.assertEqual(len(result.assignments), 19)
        used = [key for assignment in result.assignments.values() for key in assignment.component_keys]
        self.assertEqual(len(used), len(set(used)))

    def test_google_one_invoices_match_both_google_one_statement_lines(self):
        lines = [
            StatementLine(
                key="0356",
                sequence=1,
                transaction_date=date(2026, 6, 12),
                merchant_key="GOOGLE_ONE",
                amount_options=(AmountOption(Decimal("32000"), "JPY", "円請求額"),),
                reference="0356",
            ),
            StatementLine(
                key="0379",
                sequence=2,
                transaction_date=date(2026, 6, 24),
                merchant_key="GOOGLE_ONE",
                amount_options=(AmountOption(Decimal("32000"), "JPY", "円請求額"),),
                reference="0379",
            ),
        ]
        components = [
            EvidenceComponent(
                key="google-one-june-11",
                receipt_id=1,
                receipt_order=1,
                filename="google-one-1.pdf",
                merchant_key="GOOGLE_ONE",
                signed_amount=Decimal("32000"),
                currency="JPY",
                event_date=date(2026, 6, 11),
                document_kind=DOC_INVOICE,
                payee="Google Asia Pacific Pte. Ltd.",
                service_label="Google One",
            ),
            EvidenceComponent(
                key="google-one-june-23",
                receipt_id=2,
                receipt_order=2,
                filename="google-one-2.pdf",
                merchant_key="GOOGLE_ONE",
                signed_amount=Decimal("32000"),
                currency="JPY",
                event_date=date(2026, 6, 23),
                document_kind=DOC_INVOICE,
                payee="Google Asia Pacific Pte. Ltd.",
                service_label="Google One",
            ),
        ]

        result = reconcile_statement(lines, components, date_tolerance_days=1)

        self.assertEqual(set(result.assignments), {"0356", "0379"})
        self.assertEqual(result.assignments["0356"].component_keys, ("google-one-june-11",))
        self.assertEqual(result.assignments["0379"].component_keys, ("google-one-june-23",))

    def test_unrelated_merchant_does_not_match_even_if_amount_and_date_match(self):
        result = reconcile_statement(
            [line("L", 1, 10, "OPENAI", "22")],
            [component("C", 1, 1, 10, "ANTHROPIC", "22")],
        )
        self.assertNotIn("L", result.assignments)

    def test_direct_match_type_is_reported(self):
        result = reconcile_statement(
            [line("L", 1, 10, "JETBRAINS", "15400", currency="JPY")],
            [component("C", 1, 1, 10, "JETBRAINS", "15400", currency="JPY")],
        )
        self.assertEqual(result.assignments["L"].match_type, MATCH_DIRECT)


if __name__ == "__main__":
    unittest.main()
