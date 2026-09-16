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


    def test_missing_historical_plan_name_is_allowed_for_same_users_subscription(self):
        history = HistoricalPlanReceipt(
            **{**self.history.__dict__, "plan_name": "", "recurring_service": True}
        )
        candidate = infer_plan_change_candidate(self.line, [self.change], [history])
        self.assertIsNotNone(candidate)
        self.assertFalse(candidate.historical_plan_explicit)
        self.assertLess(candidate.confidence, self.change.confidence + 0.01)

    def test_missing_historical_plan_name_is_rejected_for_non_subscription(self):
        history = HistoricalPlanReceipt(
            **{**self.history.__dict__, "plan_name": "", "recurring_service": False}
        )
        self.assertIsNone(infer_plan_change_candidate(self.line, [self.change], [history]))

    def test_explicit_old_plan_candidate_is_preferred_over_implicit_candidate(self):
        implicit = HistoricalPlanReceipt(
            **{
                **self.history.__dict__,
                "receipt_id": 999,
                "plan_name": "",
                "recurring_service": True,
            }
        )
        candidate = infer_plan_change_candidate(self.line, [self.change], [implicit, self.history])
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.historical_receipt_id, self.history.receipt_id)
        self.assertTrue(candidate.historical_plan_explicit)

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


    def test_previous_statement_can_support_admin_reviewed_inference(self):
        statement_history = HistoricalPlanReceipt(
            receipt_id=None,
            user_id=None,
            filename="前月カード明細 0285",
            merchant_key="ANTHROPIC",
            plan_name="",
            event_date=date(2026, 5, 8),
            amount=Decimal("22.00"),
            currency="USD",
            document_quality=5,
            recurring_service=True,
            evidence_key="statement:285:USD:22.00",
            source_type="statement",
        )
        candidate = infer_plan_change_candidate(self.line, [self.change], [statement_history])
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.user_id, self.change.user_id)
        self.assertIsNone(candidate.historical_receipt_id)
        self.assertEqual(candidate.historical_source_type, "statement")
        self.assertLess(candidate.confidence, self.change.confidence)

    def test_previous_statement_still_requires_exact_amount_and_billing_day(self):
        wrong_amount = HistoricalPlanReceipt(
            receipt_id=None, user_id=None, filename="前月カード明細 0285",
            merchant_key="ANTHROPIC", plan_name="", event_date=date(2026, 5, 8),
            amount=Decimal("22.01"), currency="USD", recurring_service=True,
            evidence_key="statement:285:USD:22.01", source_type="statement",
        )
        wrong_day = HistoricalPlanReceipt(
            receipt_id=None, user_id=None, filename="前月カード明細 0280",
            merchant_key="ANTHROPIC", plan_name="", event_date=date(2026, 5, 5),
            amount=Decimal("22.00"), currency="USD", recurring_service=True,
            evidence_key="statement:280:USD:22.00", source_type="statement",
        )
        self.assertIsNone(infer_plan_change_candidate(self.line, [self.change], [wrong_amount]))
        self.assertIsNone(infer_plan_change_candidate(self.line, [self.change], [wrong_day]))

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



class PreviousMonthSubmitterTests(unittest.TestCase):
    def history(self, user=1, day=11, amount="32000", merchant="GOOGLE_ONE", currency="JPY", **kwargs):
        from .plan_change_matching import HistoricalSubmitterUsage
        return HistoricalSubmitterUsage(user_id=user, user_label=f"user-{user}",
            service_id=user, service_label="Example subscription", merchant_key=merchant,
            event_date=date(2026, 7, day), amount=Decimal(amount), currency=currency,
            receipt_id=user, filename=f"previous-{user}.pdf",
            billing_type=kwargs.pop("billing_type", "subscription"), **kwargs)

    def line(self, day=10, amount="32000", merchant="GOOGLE_PLAY", currency="JPY"):
        return PlanStatementLine(key="current", transaction_date=date(2026, 8, day),
            merchant_key=merchant, amount_options=(PlanAmountOption(Decimal(amount), currency),))

    def suggest(self, history, line=None, **kwargs):
        from .plan_change_matching import suggest_previous_month_submitters
        return suggest_previous_month_submitters(line or self.line(), history, **kwargs)

    def test_subscription_same_amount_near_monthly_day_is_strong_but_unconfirmed(self):
        result = self.suggest([self.history()])
        candidate = result["candidates"][0]
        self.assertEqual(candidate["support_level"], 3)
        self.assertEqual(candidate["date_distance"], 1)
        self.assertIn("未確定", candidate["support_label"])
        self.assertTrue(result["suggestion_only"])
        self.assertFalse(result["consumes_receipts"])
        self.assertTrue(candidate["reference_only"])

    def test_subscription_wrong_billing_day_is_excluded_even_at_same_price(self):
        candidates = self.suggest([self.history(2, day=23), self.history(1)])["candidates"]
        self.assertEqual(candidates[0]["user_id"], 1)
        self.assertEqual(len(candidates), 1)

    def test_equal_candidates_are_not_arbitrarily_resolved(self):
        candidates = self.suggest([self.history(2), self.history(1)])["candidates"]
        self.assertEqual(len(candidates), 2)
        self.assertTrue(all(c["ambiguous"] for c in candidates))

    def test_old_month_not_used_as_previous_month(self):
        from dataclasses import replace
        result = self.suggest([replace(self.history(), event_date=date(2026, 6, 11))])
        self.assertEqual(result["candidates"], [])

    def test_different_merchant_same_price_is_excluded(self):
        self.assertEqual(self.suggest([self.history(merchant="GITHUB")])["candidates"], [])

    def test_google_cloud_and_google_one_are_not_merged(self):
        self.assertEqual(self.suggest([self.history(merchant="GOOGLE_CLOUD")])["candidates"], [])

    def test_different_currency_is_not_converted_or_compared(self):
        self.assertEqual(self.suggest([self.history(currency="USD")])["candidates"], [])

    def test_metered_same_amount_is_only_reference(self):
        h = self.history(amount="20", merchant="GROK", currency="USD", billing_type="metered")
        c = self.suggest([h], self.line(amount="20", merchant="GROK", currency="USD"))["candidates"][0]
        self.assertEqual(c["support_level"], 1)

    def test_multiple_metered_events_do_not_justify_an_unrelated_price(self):
        h = self.history(amount="20", merchant="GROK", currency="USD", billing_type="metered")
        line = self.line(amount="35", merchant="GROK", currency="USD")
        self.assertFalse(self.suggest([h], line)["candidates"])
        from dataclasses import replace
        second = replace(h, event_date=date(2026, 7, 13), receipt_id=2, source_key="different-event")
        self.assertFalse(self.suggest([h, second], line)["candidates"])

    def test_reuploaded_duplicate_does_not_count_as_multiple_events(self):
        from dataclasses import replace
        h = self.history(amount="20", merchant="GROK", currency="USD", billing_type="metered", source_key="one-event")
        line = self.line(amount="35", merchant="GROK", currency="USD")
        self.assertFalse(self.suggest([h, replace(h, receipt_id=99)], line)["candidates"])

    def test_present_current_document_is_not_called_unsubmitted(self):
        from dataclasses import replace
        h = self.history()
        current = replace(h, event_date=date(2026, 8, 11), receipt_id=11, filename="current.pdf")
        c = self.suggest([h], current_charges=[current])["candidates"][0]
        self.assertEqual(c["submission_state"], "uploaded_review")
        self.assertEqual(c["current_receipts"][0]["receipt_id"], 11)

    def test_another_users_document_does_not_hide_missing_candidate(self):
        from dataclasses import replace
        current = replace(self.history(2), event_date=date(2026, 8, 11))
        c = self.suggest([self.history(1)], current_charges=[current])["candidates"][0]
        self.assertEqual(c["submission_state"], "not_confirmed")

    def test_other_current_top_up_does_not_imply_this_top_up_is_submitted(self):
        from dataclasses import replace
        current = replace(self.history(1), event_date=date(2026, 8, 25))
        c = self.suggest([self.history(1)], current_charges=[current])["candidates"][0]
        self.assertEqual(c["submission_state"], "not_confirmed")

    def test_month_end_uses_clamped_calendar_date_not_30_days(self):
        from dataclasses import replace
        h = replace(self.history(), event_date=date(2028, 1, 31))
        line = replace(self.line(), transaction_date=date(2028, 2, 29))
        c = self.suggest([h], line)["candidates"][0]
        self.assertEqual(c["date_distance"], 0)

    def test_one_user_with_many_records_is_displayed_once(self):
        self.assertEqual(len(self.suggest([self.history(), self.history(day=20)])["candidates"]), 1)

    def test_limits_are_disclosed_instead_of_hiding_ambiguity(self):
        result = self.suggest([self.history(i) for i in range(1, 8)])
        self.assertEqual(result["total_candidates"], 7)
        self.assertEqual(len(result["candidates"]), 5)
        self.assertTrue(result["truncated"])
        self.assertTrue(all(c["ambiguous"] for c in result["candidates"]))

    def test_history_is_not_mutated_or_consumed_across_suggestions(self):
        h = self.history()
        first = self.suggest([h])
        second = self.suggest([h])
        self.assertEqual(first, second)
        self.assertFalse(first["consumes_receipts"])

    def test_unsupported_price_change_is_not_a_candidate(self):
        self.assertFalse(self.suggest([self.history(amount="35000")])["candidates"])

    def test_non_finite_history_is_ignored(self):
        self.assertEqual(self.suggest([self.history(amount="NaN")])["candidates"], [])

    def test_negative_statement_amount_never_suggests_purchase_owner(self):
        from dataclasses import replace
        line = replace(self.line(), amount_options=(PlanAmountOption(Decimal("-32000"), "JPY"),))
        self.assertEqual(self.suggest([self.history()], line)["candidates"], [])

    def test_card_boundary_date_retains_original_document_date_and_source(self):
        from dataclasses import replace
        h = replace(self.history(), event_date=date(2026, 7, 1),
                    historical_statement_id=10, historical_line_reference="PREV",
                    document_event_date=date(2026, 6, 30), previously_matched=True)
        line = replace(self.line(), transaction_date=date(2026, 8, 1))
        c = self.suggest([h], line)["candidates"][0]
        self.assertEqual(c["historical_document_date"], "2026-06-30")
        self.assertEqual(c["historical_event_date"], "2026-07-01")
        self.assertEqual(c["historical_statement_id"], 10)
        self.assertEqual(c["support_level"], 3)

    def test_non_finite_current_amount_is_not_proof_of_submission(self):
        from dataclasses import replace
        current = replace(self.history(amount="NaN"), event_date=date(2026, 8, 10))
        c = self.suggest([self.history()], current_charges=[current])["candidates"][0]
        self.assertEqual(c["submission_state"], "not_confirmed")

if __name__ == "__main__":
    unittest.main()
