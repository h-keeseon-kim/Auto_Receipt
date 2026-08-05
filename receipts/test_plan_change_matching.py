from datetime import date
from decimal import Decimal
import unittest

from .plan_change_matching import (
    HistoricalPlanReceipt,
    PlanAmountOption,
    PlanChangeDocument,
    PlanStatementLine,
    allocate_unique_plan_change_candidates,
    infer_plan_change_candidate,
)


class PlanChangeInferenceTests(unittest.TestCase):
    def setUp(self):
        self.line = PlanStatementLine(
            key="0343",
            transaction_date=date(2026, 6, 8),
            merchant_key="ANTHROPIC",
            amount_options=(PlanAmountOption(Decimal("22.00"), "USD"),),
        )
        self.change = PlanChangeDocument(
            receipt_id=341,
            user_id=7,
            filename="260606_uchiyama_Claude_Max_218.01_USD.pdf",
            merchant_key="ANTHROPIC",
            previous_plan="Claude Pro",
            new_plan="Max plan - 20x",
            change_date=date(2026, 6, 6),
            previous_plan_end=date(2026, 6, 8),
            confidence=0.98,
        )
        self.history = HistoricalPlanReceipt(
            receipt_id=285,
            user_id=7,
            filename="260508_uchiyama_Claude_Pro_22_USD.pdf",
            merchant_key="ANTHROPIC",
            plan_name="Claude Pro",
            event_date=date(2026, 5, 8),
            amount=Decimal("22.00"),
            currency="USD",
            document_quality=0,
        )

    def test_exact_plan_change_scenario_returns_candidate(self):
        candidate = infer_plan_change_candidate(self.line, [self.change], [self.history])
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.user_id, 7)
        self.assertEqual(candidate.previous_plan, "Claude Pro")
        self.assertEqual(candidate.new_plan, "Max plan - 20x")
        self.assertEqual(candidate.amount, Decimal("22.00"))
        self.assertEqual(candidate.currency, "USD")

    def test_amount_must_match_exactly(self):
        history = HistoricalPlanReceipt(**{**self.history.__dict__, "amount": Decimal("22.01")})
        self.assertIsNone(infer_plan_change_candidate(self.line, [self.change], [history]))

    def test_old_plan_must_match(self):
        history = HistoricalPlanReceipt(**{**self.history.__dict__, "plan_name": "Claude Max"})
        self.assertIsNone(infer_plan_change_candidate(self.line, [self.change], [history]))

    def test_history_must_be_previous_calendar_month(self):
        history = HistoricalPlanReceipt(**{**self.history.__dict__, "event_date": date(2026, 4, 8)})
        self.assertIsNone(infer_plan_change_candidate(self.line, [self.change], [history]))

    def test_statement_date_must_be_near_previous_plan_end(self):
        line = PlanStatementLine(
            key="0343",
            transaction_date=date(2026, 6, 10),
            merchant_key="ANTHROPIC",
            amount_options=self.line.amount_options,
        )
        self.assertIsNone(infer_plan_change_candidate(line, [self.change], [self.history]))

    def test_billing_day_must_repeat(self):
        history = HistoricalPlanReceipt(**{**self.history.__dict__, "event_date": date(2026, 5, 5)})
        self.assertIsNone(infer_plan_change_candidate(self.line, [self.change], [history]))

    def test_equally_strong_candidates_for_different_users_are_not_guessed(self):
        other_change = PlanChangeDocument(**{**self.change.__dict__, "receipt_id": 999, "user_id": 8})
        other_history = HistoricalPlanReceipt(**{**self.history.__dict__, "receipt_id": 998, "user_id": 8})
        self.assertIsNone(
            infer_plan_change_candidate(
                self.line,
                [self.change, other_change],
                [self.history, other_history],
            )
        )

    def test_low_confidence_change_document_is_not_used(self):
        change = PlanChangeDocument(**{**self.change.__dict__, "confidence": 0.5})
        self.assertIsNone(infer_plan_change_candidate(self.line, [change], [self.history]))

    def test_global_allocation_uses_same_evidence_once_and_prefers_exact_end_date(self):
        exact = infer_plan_change_candidate(self.line, [self.change], [self.history])
        adjacent_line = PlanStatementLine(
            key="0340",
            transaction_date=date(2026, 6, 7),
            merchant_key="ANTHROPIC",
            amount_options=self.line.amount_options,
        )
        adjacent = infer_plan_change_candidate(adjacent_line, [self.change], [self.history])
        self.assertIsNotNone(exact)
        self.assertIsNotNone(adjacent)

        allocated = allocate_unique_plan_change_candidates(
            [(18, 340, adjacent), (21, 343, exact)]
        )

        self.assertEqual(set(allocated), {"0343"})
        self.assertEqual(allocated["0343"].end_date_distance, 0)


if __name__ == "__main__":
    unittest.main()
