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
    MATCH_BILLING_BRIDGE,
    MATCH_DIRECT,
    MATCH_LINKED_REFUND_NET,
    MATCH_MERCHANT_REFUND_NET,
    MATCH_ORIGINAL_CHARGE,
    MATCH_REVERSAL_ORIGINAL_CHARGE,
    ROLE_CHARGE,
    ROLE_REFUND,
    STATEMENT_ROLE_REVERSAL,
    USAGE_MODE_REFERENCE,
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
    fingerprint: str = "",
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
        fingerprint=fingerprint,
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

    def test_unlinked_refund_outside_card_posting_window_is_not_used(self):
        line_value = StatementLine(
            key="L",
            sequence=1,
            transaction_date=date(2026, 6, 23),
            merchant_key="ANTHROPIC",
            amount_options=(AmountOption(Decimal("64"), "USD", "statement"),),
        )
        components = [
            component("charge", 1, 1, 23, "ANTHROPIC", "137.26"),
            EvidenceComponent(
                key="refund-a",
                receipt_id=2,
                receipt_order=2,
                filename="refund-a.pdf",
                merchant_key="ANTHROPIC",
                signed_amount=Decimal("-66.48"),
                currency="USD",
                event_date=date(2026, 8, 20),
                role=ROLE_REFUND,
                document_kind=DOC_REFUND,
                payee="ANTHROPIC",
            ),
            EvidenceComponent(
                key="refund-b",
                receipt_id=3,
                receipt_order=3,
                filename="refund-b.pdf",
                merchant_key="ANTHROPIC",
                signed_amount=Decimal("-6.78"),
                currency="USD",
                event_date=date(2026, 8, 20),
                role=ROLE_REFUND,
                document_kind=DOC_REFUND,
                payee="ANTHROPIC",
            ),
        ]
        result = reconcile_statement([line_value], components)
        self.assertNotIn("L", result.assignments)


    def test_corporate_card_refunds_are_allocated_globally_across_accounts(self):
        lines = [
            StatementLine(
                key="0424",
                sequence=1,
                transaction_date=date(2026, 7, 3),
                merchant_key="ANTHROPIC",
                amount_options=(AmountOption(Decimal("178.60"), "USD", "外貨金額"),),
                reference="0424",
            ),
            StatementLine(
                key="0465",
                sequence=2,
                transaction_date=date(2026, 7, 8),
                merchant_key="ANTHROPIC",
                amount_options=(AmountOption(Decimal("104.74"), "USD", "外貨金額"),),
                reference="0465",
            ),
            StatementLine(
                key="0466",
                sequence=3,
                transaction_date=date(2026, 6, 28),
                merchant_key="ANTHROPIC",
                amount_options=(AmountOption(Decimal("66.60"), "USD", "外貨金額"),),
                reference="0466",
            ),
        ]

        def dated_component(key, receipt_id, order, event_date, amount, role=ROLE_CHARGE):
            return EvidenceComponent(
                key=key,
                receipt_id=receipt_id,
                receipt_order=order,
                filename=f"{key}.pdf",
                merchant_key="ANTHROPIC",
                signed_amount=Decimal(amount),
                currency="USD",
                event_date=event_date,
                role=role,
                document_kind=DOC_REFUND if role == ROLE_REFUND else DOC_RECEIPT,
                payee="Anthropic, PBC",
            )

        components = [
            dated_component("team-29554", 1, 1, date(2026, 7, 2), "295.54"),
            dated_component("max5-110", 2, 2, date(2026, 7, 8), "110.00"),
            dated_component("fujimoto-220", 3, 3, date(2026, 6, 28), "220.00"),
            dated_component("saito-071", 10, 10, date(2026, 7, 5), "-0.71", ROLE_REFUND),
            dated_component("takahashi-090", 11, 11, date(2026, 7, 5), "-0.90", ROLE_REFUND),
            dated_component("hamaguchi-3237", 12, 12, date(2026, 7, 5), "-32.37", ROLE_REFUND),
            dated_component("takizawa-1241", 13, 13, date(2026, 7, 5), "-12.41", ROLE_REFUND),
            dated_component("sung-6558", 14, 14, date(2026, 7, 6), "-65.58", ROLE_REFUND),
            dated_component("takenaka-497", 15, 15, date(2026, 7, 5), "-4.97", ROLE_REFUND),
            dated_component("takaya-462", 16, 16, date(2026, 7, 7), "-4.62", ROLE_REFUND),
            dated_component("hiwatashi-064", 17, 17, date(2026, 7, 28), "-0.64", ROLE_REFUND),
            dated_component("masui-11920", 18, 18, date(2026, 7, 6), "-119.20", ROLE_REFUND),
            dated_component("nishikawa-3420", 19, 19, date(2026, 7, 29), "-34.20", ROLE_REFUND),
            # Numerically valid for 0424 with fewer documents, but posted before
            # the sale. Chronology must make the six later refunds preferable.
            dated_component("kim-old-6648", 20, 20, date(2026, 6, 22), "-66.48", ROLE_REFUND),
        ]

        result = reconcile_statement(lines, components)

        self.assertEqual(set(result.assignments), {"0424", "0465", "0466"})
        self.assertEqual(
            set(result.assignments["0424"].component_keys),
            {
                "team-29554", "saito-071", "takahashi-090", "hamaguchi-3237",
                "takizawa-1241", "sung-6558", "takenaka-497",
            },
        )
        self.assertEqual(
            set(result.assignments["0465"].component_keys),
            {"max5-110", "takaya-462", "hiwatashi-064"},
        )
        self.assertEqual(
            set(result.assignments["0466"].component_keys),
            {"fujimoto-220", "masui-11920", "nishikawa-3420"},
        )
        used = [key for assignment in result.assignments.values() for key in assignment.component_keys]
        self.assertEqual(len(used), len(set(used)))
        self.assertIn("kim-old-6648", result.unused_component_keys)

    def test_reversal_row_can_reference_same_original_charge_as_net_rebooking(self):
        lines = [
            StatementLine(
                key="0383",
                sequence=1,
                transaction_date=date(2026, 6, 28),
                merchant_key="ANTHROPIC",
                amount_options=(AmountOption(Decimal("36991"), "JPY", "円請求額"),),
                reference="0383",
                statement_role=STATEMENT_ROLE_REVERSAL,
            ),
            StatementLine(
                key="0466",
                sequence=2,
                transaction_date=date(2026, 6, 28),
                merchant_key="ANTHROPIC",
                amount_options=(AmountOption(Decimal("66.60"), "USD", "外貨金額"),),
                reference="0466",
            ),
        ]
        components = [
            EvidenceComponent(
                key="fujimoto-220",
                receipt_id=1,
                receipt_order=1,
                filename="0383_260629_藤本_Claude_220USD.pdf",
                merchant_key="ANTHROPIC",
                signed_amount=Decimal("220.00"),
                currency="USD",
                event_date=date(2026, 6, 28),
                role=ROLE_CHARGE,
                document_kind=DOC_RECEIPT,
                payee="Anthropic, PBC",
            ),
            EvidenceComponent(
                key="masui-11920",
                receipt_id=2,
                receipt_order=2,
                filename="masui-refund.pdf",
                merchant_key="ANTHROPIC",
                signed_amount=Decimal("-119.20"),
                currency="USD",
                event_date=date(2026, 7, 6),
                role=ROLE_REFUND,
                document_kind=DOC_REFUND,
                payee="Anthropic, PBC",
            ),
            EvidenceComponent(
                key="nishikawa-3420",
                receipt_id=3,
                receipt_order=3,
                filename="nishikawa-refund.pdf",
                merchant_key="ANTHROPIC",
                signed_amount=Decimal("-34.20"),
                currency="USD",
                event_date=date(2026, 7, 29),
                role=ROLE_REFUND,
                document_kind=DOC_REFUND,
                payee="Anthropic, PBC",
            ),
        ]

        result = reconcile_statement(lines, components)

        self.assertEqual(result.assignments["0466"].match_type, MATCH_MERCHANT_REFUND_NET)
        self.assertEqual(result.assignments["0383"].match_type, MATCH_REVERSAL_ORIGINAL_CHARGE)
        self.assertEqual(result.assignments["0383"].component_keys, ("fujimoto-220",))
        self.assertIn("fujimoto-220", result.assignments["0466"].component_keys)
        self.assertFalse(result.unused_component_keys)

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


    def test_google_play_billing_descriptor_matches_google_one_receipt_without_stealing_exact_match(self):
        lines = [
            StatementLine(
                key="0415",
                sequence=1,
                transaction_date=date(2026, 7, 10),
                merchant_key="GOOGLE_PLAY",
                amount_options=(AmountOption(Decimal("32000"), "JPY", "円請求額"),),
                reference="0415",
            ),
            StatementLine(
                key="0445",
                sequence=2,
                transaction_date=date(2026, 7, 24),
                merchant_key="GOOGLE_ONE",
                amount_options=(AmountOption(Decimal("32000"), "JPY", "円請求額"),),
                reference="0445",
            ),
        ]
        components = [
            EvidenceComponent(
                key="matsuzaki-google-one",
                receipt_id=1,
                receipt_order=1,
                filename="260711_ken.matsuzaki_Google_One_32000_JPY.pdf",
                merchant_key="GOOGLE_ONE",
                signed_amount=Decimal("32000"),
                currency="JPY",
                event_date=date(2026, 7, 11),
                document_kind=DOC_INVOICE,
                payee="Google Asia Pacific Pte. Ltd.",
                service_label="Google One",
            ),
            EvidenceComponent(
                key="kim-google-one",
                receipt_id=2,
                receipt_order=2,
                filename="260723_keeseon.kim_Google_One_32000_JPY.pdf",
                merchant_key="GOOGLE_ONE",
                signed_amount=Decimal("32000"),
                currency="JPY",
                event_date=date(2026, 7, 23),
                document_kind=DOC_INVOICE,
                payee="Google Asia Pacific Pte. Ltd.",
                service_label="Google One",
            ),
        ]

        result = reconcile_statement(lines, components, date_tolerance_days=1)

        self.assertEqual(set(result.assignments), {"0415", "0445"})
        self.assertEqual(result.assignments["0415"].component_keys, ("matsuzaki-google-one",))
        self.assertEqual(result.assignments["0415"].match_type, MATCH_BILLING_BRIDGE)
        self.assertEqual(result.assignments["0445"].component_keys, ("kim-google-one",))
        self.assertEqual(result.assignments["0445"].match_type, MATCH_DIRECT)
        self.assertFalse(result.unused_component_keys)

    def test_google_play_billing_bridge_does_not_match_google_cloud_receipt(self):
        result = reconcile_statement(
            [
                StatementLine(
                    key="L",
                    sequence=1,
                    transaction_date=date(2026, 7, 10),
                    merchant_key="GOOGLE_PLAY",
                    amount_options=(AmountOption(Decimal("32000"), "JPY", "円請求額"),),
                )
            ],
            [
                EvidenceComponent(
                    key="cloud",
                    receipt_id=1,
                    receipt_order=1,
                    filename="google-cloud.pdf",
                    merchant_key="GOOGLE_CLOUD",
                    signed_amount=Decimal("32000"),
                    currency="JPY",
                    event_date=date(2026, 7, 10),
                    document_kind=DOC_INVOICE,
                    payee="Google Cloud",
                    service_label="Google Cloud",
                )
            ],
        )
        self.assertNotIn("L", result.assignments)

    def test_exact_merchant_identity_outranks_billing_bridge(self):
        line_value = StatementLine(
            key="L",
            sequence=1,
            transaction_date=date(2026, 7, 10),
            merchant_key="GOOGLE_ONE",
            amount_options=(AmountOption(Decimal("32000"), "JPY", "円請求額"),),
        )
        components = [
            EvidenceComponent(
                key="bridge",
                receipt_id=1,
                receipt_order=1,
                filename="google-play.pdf",
                merchant_key="GOOGLE_PLAY",
                signed_amount=Decimal("32000"),
                currency="JPY",
                event_date=date(2026, 7, 10),
                document_kind=DOC_INVOICE,
                payee="Google Play Japan",
                service_label="Google Play",
            ),
            EvidenceComponent(
                key="exact",
                receipt_id=2,
                receipt_order=2,
                filename="google-one.pdf",
                merchant_key="GOOGLE_ONE",
                signed_amount=Decimal("32000"),
                currency="JPY",
                event_date=date(2026, 7, 11),
                document_kind=DOC_INVOICE,
                payee="Google Asia Pacific Pte. Ltd.",
                service_label="Google One",
            ),
        ]

        result = reconcile_statement([line_value], components, date_tolerance_days=1)

        self.assertEqual(result.assignments["L"].component_keys, ("exact",))
        self.assertEqual(result.assignments["L"].match_type, MATCH_DIRECT)


    def test_unavailable_component_cannot_be_directly_consumed(self):
        blocked = component(
            "blocked", 1, 1, 10, "ANTHROPIC", "22", fingerprint="fp-blocked"
        )
        result = reconcile_statement(
            [line("L", 1, 10, "ANTHROPIC", "22")],
            [blocked],
            unavailable_component_keys={"blocked"},
        )
        self.assertNotIn("L", result.assignments)
        self.assertEqual(result.reserved_component_keys, {"blocked"})

    def test_unavailable_charge_or_refund_cannot_be_used_in_net_calculation(self):
        charge = component(
            "charge", 1, 1, 10, "ANTHROPIC", "110", fingerprint="fp-charge"
        )
        refund = component(
            "refund", 2, 2, 11, "ANTHROPIC", "-5.26",
            role=ROLE_REFUND, kind=DOC_REFUND, fingerprint="fp-refund",
        )
        target = [line("L", 1, 10, "ANTHROPIC", "104.74")]
        for unavailable in ({"charge"}, {"refund"}):
            with self.subTest(unavailable=unavailable):
                result = reconcile_statement(
                    target,
                    [charge, refund],
                    unavailable_component_keys=set(unavailable),
                )
                self.assertNotIn("L", result.assignments)

    def test_globally_consumed_charge_can_still_be_reference_for_reversal(self):
        reversal = StatementLine(
            key="RETURN",
            sequence=1,
            transaction_date=date(2026, 6, 10),
            merchant_key="ANTHROPIC",
            amount_options=(AmountOption(Decimal("220"), "USD", "statement"),),
            statement_role=STATEMENT_ROLE_REVERSAL,
        )
        original = component(
            "original", 1, 1, 10, "ANTHROPIC", "220", fingerprint="fp-original"
        )
        result = reconcile_statement(
            [reversal],
            [original],
            unavailable_component_keys={"original"},
        )
        self.assertEqual(result.assignments["RETURN"].component_keys, ("original",))
        self.assertEqual(result.assignments["RETURN"].usage_mode, USAGE_MODE_REFERENCE)
        self.assertNotIn("original", result.unused_component_keys)
        self.assertEqual(result.reserved_component_keys, {"original"})

    def test_reference_only_component_remains_available_for_future_consume(self):
        reversal = StatementLine(
            key="RETURN",
            sequence=1,
            transaction_date=date(2026, 6, 10),
            merchant_key="ANTHROPIC",
            amount_options=(AmountOption(Decimal("220"), "USD", "statement"),),
            statement_role=STATEMENT_ROLE_REVERSAL,
        )
        original = component("original", 1, 1, 10, "ANTHROPIC", "220")
        result = reconcile_statement([reversal], [original])
        self.assertIn("original", result.referenced_component_keys)
        self.assertIn("original", result.unused_component_keys)
        self.assertNotIn("original", result.consumed_component_keys)

    def test_duplicate_fingerprint_is_deduplicated_across_reuploaded_rows(self):
        first = component(
            "upload-a", 1, 1, 10, "ANTHROPIC", "22", fingerprint="stable-financial-event"
        )
        second = component(
            "upload-b", 2, 2, 10, "ANTHROPIC", "22", fingerprint="stable-financial-event"
        )
        unique, removed = deduplicate_components([first, second])
        self.assertEqual(len(unique), 1)
        self.assertEqual(removed, {"upload-b"})

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
