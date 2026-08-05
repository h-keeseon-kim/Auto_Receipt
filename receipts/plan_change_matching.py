from __future__ import annotations

"""Pure-Python inference for subscription plan-change statement lines.

A plan-change receipt can prove that a user moved from an old recurring plan
into a new plan, while a historical receipt proves the old plan's amount and
billing cadence.  This module does *not* treat those documents as direct
payment evidence for the current statement line.  It only builds a tightly
constrained inference candidate that must be reviewed by an administrator.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import re
import unicodedata


@dataclass(frozen=True)
class PlanAmountOption:
    amount: Decimal
    currency: str

    def __post_init__(self):
        object.__setattr__(self, "amount", Decimal(str(self.amount)))
        object.__setattr__(self, "currency", (self.currency or "").upper())


@dataclass(frozen=True)
class PlanStatementLine:
    key: str
    transaction_date: date | None
    merchant_key: str
    amount_options: tuple[PlanAmountOption, ...]


@dataclass(frozen=True)
class PlanChangeDocument:
    receipt_id: int
    user_id: int
    filename: str
    merchant_key: str
    previous_plan: str
    new_plan: str
    change_date: date | None
    previous_plan_end: date | None
    confidence: float = 0.0


@dataclass(frozen=True)
class HistoricalPlanReceipt:
    receipt_id: int
    user_id: int
    filename: str
    merchant_key: str
    plan_name: str
    event_date: date | None
    amount: Decimal | None
    currency: str
    document_quality: int = 9
    recurring_service: bool = False

    def __post_init__(self):
        if self.amount is not None:
            object.__setattr__(self, "amount", Decimal(str(self.amount)))
        object.__setattr__(self, "currency", (self.currency or "").upper())


@dataclass(frozen=True)
class PlanChangeInferenceCandidate:
    line_key: str
    user_id: int
    change_receipt_id: int
    historical_receipt_id: int
    change_filename: str
    historical_filename: str
    previous_plan: str
    new_plan: str
    change_date: date | None
    previous_plan_end: date
    historical_date: date
    amount: Decimal
    currency: str
    confidence: float
    end_date_distance: int
    billing_day_distance: int
    historical_plan_explicit: bool = False

    @property
    def fingerprint(self) -> str:
        return (
            f"line={self.line_key};change={self.change_receipt_id};"
            f"history={self.historical_receipt_id};amount={self.amount};currency={self.currency};"
            f"plan_explicit={int(self.historical_plan_explicit)}"
        )


def _normalise_plan(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").lower()
    value = re.sub(r"\b(?:monthly|annual|subscription|plan)\b", " ", value)
    value = re.sub(r"[^a-z0-9ぁ-んァ-ヶ一-龠]+", " ", value)
    return " ".join(value.split())


def plans_related(first: str, second: str) -> bool:
    left = _normalise_plan(first)
    right = _normalise_plan(second)
    if not left or not right:
        return False
    if left == right:
        return True
    if min(len(left), len(right)) >= 5 and (left in right or right in left):
        return True
    ignored = {"claude", "chatgpt", "openai", "anthropic", "google"}
    left_tokens = {token for token in left.split() if token not in ignored and len(token) >= 3}
    right_tokens = {token for token in right.split() if token not in ignored and len(token) >= 3}
    return bool(left_tokens and right_tokens and left_tokens.intersection(right_tokens))


def _previous_month(value: date) -> tuple[int, int]:
    if value.month == 1:
        return value.year - 1, 12
    return value.year, value.month - 1


def _matching_amount(line: PlanStatementLine, historical: HistoricalPlanReceipt) -> PlanAmountOption | None:
    if historical.amount is None or not historical.currency:
        return None
    for option in line.amount_options:
        if option.currency == historical.currency and option.amount == historical.amount:
            return option
    return None


def _candidate_core_rank(candidate: PlanChangeInferenceCandidate, historical_quality: int) -> tuple:
    # Lower is better.  Receipt IDs are intentionally excluded so we can detect
    # true ties across users instead of silently choosing one person.
    return (
        candidate.end_date_distance,
        0 if candidate.historical_plan_explicit else 1,
        candidate.billing_day_distance,
        -round(candidate.confidence, 4),
        historical_quality,
    )


def infer_plan_change_candidate(
    line: PlanStatementLine,
    changes: list[PlanChangeDocument],
    historical_receipts: list[HistoricalPlanReceipt],
    *,
    date_tolerance_days: int = 1,
    billing_day_tolerance_days: int = 1,
    minimum_confidence: float = 0.75,
) -> PlanChangeInferenceCandidate | None:
    """Return a unique, tightly constrained plan-change inference.

    Mandatory conditions:
    - direct merchant group equality;
    - an explicit previous plan and previous-plan end date;
    - statement date within ±1 day of that end date;
    - same user in the plan-change and historical receipts;
    - historical receipt either explicitly names the old plan, or belongs to the
      same user's recurring-subscription service when the old plan label was
      not extractable;
    - exact amount and currency equality;
    - historical receipt belongs to the previous calendar month and follows the
      same billing day within ±1 day.

    If equally strong candidates point to different users, no inference is
    returned.  This prevents a generic same-price plan change from selecting an
    arbitrary person.
    """

    if not line.transaction_date or not line.merchant_key or not line.amount_options:
        return None

    expected_year, expected_month = _previous_month(line.transaction_date)
    candidates: list[tuple[tuple, PlanChangeInferenceCandidate]] = []

    for change in changes:
        if change.merchant_key != line.merchant_key:
            continue
        if not change.previous_plan or not change.previous_plan_end:
            continue
        if float(change.confidence or 0) < minimum_confidence:
            continue
        end_distance = abs((line.transaction_date - change.previous_plan_end).days)
        if end_distance > date_tolerance_days:
            continue
        if change.change_date and change.change_date > line.transaction_date:
            continue

        for historical in historical_receipts:
            if historical.user_id != change.user_id:
                continue
            if historical.merchant_key != line.merchant_key:
                continue
            if not historical.event_date or historical.amount is None or not historical.currency:
                continue
            if historical.event_date.year != expected_year or historical.event_date.month != expected_month:
                continue
            historical_plan_explicit = bool((historical.plan_name or "").strip())
            if historical_plan_explicit:
                if not plans_related(change.previous_plan, historical.plan_name):
                    continue
            elif not historical.recurring_service:
                # A missing plan label is acceptable only for a recurring
                # subscription assigned to the same user.  This keeps one-time
                # credit/API purchases from being used as old-plan evidence.
                continue
            amount_option = _matching_amount(line, historical)
            if amount_option is None:
                continue
            billing_day_distance = abs(historical.event_date.day - line.transaction_date.day)
            if billing_day_distance > billing_day_tolerance_days:
                continue
            if change.change_date and historical.event_date >= change.change_date:
                continue

            confidence = min(0.99, max(0.0, float(change.confidence or 0)))
            if not historical_plan_explicit:
                # The inference still requires same user, merchant, exact
                # amount/currency, previous month and matching billing day, but
                # the missing old-plan label must lower confidence and remain
                # an administrator-reviewed inference.
                confidence = max(0.0, confidence - 0.08)
            if end_distance == 0:
                confidence += 0.005
            if billing_day_distance == 0:
                confidence += 0.005
            confidence = min(confidence, 0.99)
            candidate = PlanChangeInferenceCandidate(
                line_key=line.key,
                user_id=change.user_id,
                change_receipt_id=change.receipt_id,
                historical_receipt_id=historical.receipt_id,
                change_filename=change.filename,
                historical_filename=historical.filename,
                previous_plan=change.previous_plan,
                new_plan=change.new_plan,
                change_date=change.change_date,
                previous_plan_end=change.previous_plan_end,
                historical_date=historical.event_date,
                amount=amount_option.amount,
                currency=amount_option.currency,
                confidence=confidence,
                end_date_distance=end_distance,
                billing_day_distance=billing_day_distance,
                historical_plan_explicit=historical_plan_explicit,
            )
            candidates.append((_candidate_core_rank(candidate, historical.document_quality), candidate))

    if not candidates:
        return None

    candidates.sort(key=lambda pair: (pair[0], pair[1].user_id, pair[1].change_receipt_id, pair[1].historical_receipt_id))
    best_rank = candidates[0][0]
    equally_best = [candidate for rank, candidate in candidates if rank == best_rank]
    if len({candidate.user_id for candidate in equally_best}) > 1:
        return None
    return equally_best[0]


def allocate_unique_plan_change_candidates(
    candidates: list[tuple[int, int, PlanChangeInferenceCandidate]],
) -> dict[str, PlanChangeInferenceCandidate]:
    """Allocate inference evidence one-to-one across statement lines.

    ``candidates`` contains ``(line_sequence, stable_line_id, candidate)``.
    Exact old-plan end-date and billing-day matches are preferred.  A plan-change
    document and a historical old-plan receipt can each support at most one line.
    The deterministic ordering avoids changing results between reconciliations.
    """

    ranked = sorted(
        candidates,
        key=lambda entry: (
            entry[2].end_date_distance,
            0 if entry[2].historical_plan_explicit else 1,
            entry[2].billing_day_distance,
            -round(entry[2].confidence, 4),
            entry[0],
            entry[1],
            entry[2].change_receipt_id,
            entry[2].historical_receipt_id,
        ),
    )
    allocated: dict[str, PlanChangeInferenceCandidate] = {}
    used_change_receipts: set[int] = set()
    used_historical_receipts: set[int] = set()
    for _, _, candidate in ranked:
        if candidate.line_key in allocated:
            continue
        if candidate.change_receipt_id in used_change_receipts:
            continue
        if candidate.historical_receipt_id in used_historical_receipts:
            continue
        allocated[candidate.line_key] = candidate
        used_change_receipts.add(candidate.change_receipt_id)
        used_historical_receipts.add(candidate.historical_receipt_id)
    return allocated
