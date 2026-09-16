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
    """過去の旧プラン実績。

    通常は過去領収書を使うが、領収書メタデータが未整備でも、前月の
    カード明細に同一請求元・同額・同請求日の実績がある場合は、
    管理者確認前の推定候補を作るための補助証拠として利用できる。
    """

    receipt_id: int | None
    user_id: int | None
    filename: str
    merchant_key: str
    plan_name: str
    event_date: date | None
    amount: Decimal | None
    currency: str
    document_quality: int = 9
    recurring_service: bool = False
    evidence_key: str = ""
    source_type: str = "receipt"

    def __post_init__(self):
        if self.amount is not None:
            object.__setattr__(self, "amount", Decimal(str(self.amount)))
        object.__setattr__(self, "currency", (self.currency or "").upper())
        if not self.evidence_key:
            identifier = self.receipt_id if self.receipt_id is not None else self.filename
            object.__setattr__(self, "evidence_key", f"{self.source_type}:{identifier}")


@dataclass(frozen=True)
class PlanChangeInferenceCandidate:
    line_key: str
    user_id: int
    change_receipt_id: int
    historical_receipt_id: int | None
    historical_evidence_key: str
    historical_source_type: str
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
            f"history={self.historical_evidence_key};amount={self.amount};currency={self.currency};"
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
    - same user in the plan-change and historical receipt when a receipt is used;
    - historical receipt either explicitly names the old plan, or belongs to the
      same user's recurring-subscription service when the old plan label was
      not extractable;
    - when the historical receipt cannot be used, the previous month's card
      statement may serve as cadence evidence, but only for an administrator-
      reviewed inference and never as direct receipt evidence;
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
            if historical.user_id is not None and historical.user_id != change.user_id:
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
                # subscription receipt or a previous-card-statement cadence
                # record. This keeps one-time credit/API purchases from being
                # used as old-plan evidence.
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
            if historical.source_type == "statement":
                # A previous card statement proves the recurring amount/cadence,
                # not that a receipt was submitted. Keep it as a lower-confidence
                # administrator-reviewed inference.
                confidence = max(0.0, confidence - 0.12)
            elif not historical_plan_explicit:
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
                historical_evidence_key=historical.evidence_key,
                historical_source_type=historical.source_type,
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

    candidates.sort(key=lambda pair: (pair[0], pair[1].user_id, pair[1].change_receipt_id, pair[1].historical_evidence_key))
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
            entry[2].historical_evidence_key,
        ),
    )
    allocated: dict[str, PlanChangeInferenceCandidate] = {}
    used_change_receipts: set[int] = set()
    used_historical_evidence: set[str] = set()
    for _, _, candidate in ranked:
        if candidate.line_key in allocated:
            continue
        if candidate.change_receipt_id in used_change_receipts:
            continue
        if candidate.historical_evidence_key in used_historical_evidence:
            continue
        allocated[candidate.line_key] = candidate
        used_change_receipts.add(candidate.change_receipt_id)
        used_historical_evidence.add(candidate.historical_evidence_key)
    return allocated


# This version is independent of monetary matching and the consume ledger.
# Old, persisted contact hints must not be displayed under the new rules.
SUBMITTER_HINT_VERSION = 3


@dataclass(frozen=True)
class HistoricalSubmitterUsage:
    """Read-only financial history. Confirmation applies to ONE charge, not a PDF/user."""

    user_id: int
    user_label: str
    service_id: int | None
    service_label: str
    merchant_key: str
    event_date: date
    amount: Decimal
    currency: str
    receipt_id: int | None = None
    filename: str = ""
    billing_type: str = ""
    source_key: str = ""
    previously_matched: bool = False
    historical_statement_id: int | None = None
    historical_line_reference: str = ""
    document_event_date: date | None = None
    amount_options: tuple[PlanAmountOption, ...] = ()
    card_last4: str = ""
    contract_key: str = ""
    confirmed_statement_id: int | None = None
    confirmed_line_key: str = ""
    confirmed_line_reference: str = ""
    review_notes: tuple[str, ...] = ()

    def __post_init__(self):
        from decimal import InvalidOperation
        try:
            amount = Decimal(str(self.amount))
        except (InvalidOperation, TypeError, ValueError):
            amount = Decimal("NaN")
        object.__setattr__(self, "amount", amount)
        object.__setattr__(self, "currency", (self.currency or "").strip().upper())


def _next_month_same_day(value: date) -> date:
    from calendar import monthrange
    year = value.year + (value.month == 12)
    month = 1 if value.month == 12 else value.month + 1
    return date(year, month, min(value.day, monthrange(year, month)[1]))


def _submitter_event_key(row: HistoricalSubmitterUsage) -> str:
    # source_key is the canonical financial identity, shared by reuploads and
    # the document-date/card-date views of the same event. User scope prevents
    # an upload by a different user being silently treated as their contract.
    source = row.source_key or (
        f"receipt:{row.receipt_id}:{row.document_event_date or row.event_date}:"
        f"{row.merchant_key}:{row.amount}:{row.currency}")
    return f"user:{row.user_id}:{source}"


def _submitter_amounts(row: HistoricalSubmitterUsage) -> set[tuple[str, Decimal]]:
    options = (PlanAmountOption(row.amount, row.currency), *row.amount_options)
    return {(a.currency, a.amount) for a in options
            if a.currency and a.amount.is_finite() and a.amount > 0}


def _submitter_rows_compatible(old: HistoricalSubmitterUsage, new: HistoricalSubmitterUsage) -> bool:
    from .statement_matching import merchant_keys_compatible
    if old.user_id != new.user_id or not merchant_keys_compatible(old.merchant_key, new.merchant_key):
        return False
    if (old.service_id is not None and new.service_id is not None and old.service_id != new.service_id
            and not (old.contract_key and old.contract_key == new.contract_key)):
        return False
    if old.contract_key and new.contract_key and old.contract_key != new.contract_key:
        return False
    if old.card_last4 and new.card_last4 and old.card_last4 != new.card_last4:
        return False
    if old.billing_type and new.billing_type and old.billing_type != new.billing_type:
        return False
    return True


def _history_groups(rows: list[HistoricalSubmitterUsage]) -> dict[str, list[HistoricalSubmitterUsage]]:
    groups: dict[str, list[HistoricalSubmitterUsage]] = {}
    for row in rows:
        if not row.user_id or not row.event_date or not row.amount.is_finite() or row.amount <= 0:
            continue
        key = _submitter_event_key(row)
        if row not in groups.setdefault(key, []):
            groups[key].append(row)
    return groups


def resolve_submitted_recurring_history(
    history: list[HistoricalSubmitterUsage], current_charges: list[HistoricalSubmitterUsage],
    *, month: date, date_tolerance_days: int = 3,
) -> dict[str, dict]:
    """Explain prior charges by confirmed current renewals, one event to one event.

    This is NOT an evidence allocation and writes nothing. Positive and negative
    histories remain intact. Never exclude a whole user just because one receipt
    matched. Unknown billing types can be explained by a precise monthly pair,
    but metered and one-time purchases have no assumed monthly obligation.

    Unique mutual-best pairs are resolved first. A completely covered ambiguous
    component can be excluded as a group; a partially covered tie stays unresolved
    rather than arbitrarily hiding a different contract's missing receipt.
    """
    tolerance = max(0, min(int(date_tolerance_days), 7))
    previous = _previous_month(month)
    old_groups = _history_groups([h for h in history
        if h.event_date and (h.event_date.year, h.event_date.month) == previous
        and h.billing_type not in {"metered", "one_time"}])
    new_groups = _history_groups([c for c in current_charges
        if c.confirmed_line_key and c.event_date
        and (c.event_date.year, c.event_date.month) == (month.year, month.month)])
    edges: dict[tuple[str, str], tuple] = {}
    for hk, olds in old_groups.items():
        for ck, news in new_groups.items():
            ranks = []
            for h in olds:
                for c in news:
                    if not _submitter_rows_compatible(h, c) or not (_submitter_amounts(h) & _submitter_amounts(c)):
                        continue
                    distance = abs((c.event_date - _next_month_same_day(h.event_date)).days)
                    if distance <= tolerance:
                        ranks.append((0 if h.contract_key and c.contract_key else 1,
                                      0 if h.service_id is not None and h.service_id == c.service_id else 1,
                                      0 if (h.currency, h.amount) == (c.currency, c.amount) else 1,
                                      distance))
            if ranks:
                edges[hk, ck] = min(ranks)
    result: dict[str, dict] = {}

    def explain(hk, current_keys, *, group_covered=False):
        h = min(old_groups[hk], key=lambda r: (not r.previously_matched,
                                             r.historical_statement_id is None, r.event_date, r.filename))
        current = []
        seen = set()
        for ck in sorted(current_keys):
            for c in new_groups[ck]:
                identity = (c.confirmed_statement_id, c.confirmed_line_key)
                if identity in seen:
                    continue
                seen.add(identity)
                current.append({"statement_id": c.confirmed_statement_id,
                    "line_reference": c.confirmed_line_reference or c.confirmed_line_key,
                    "receipt_id": c.receipt_id, "filename": c.filename,
                    "event_date": c.event_date.isoformat(), "amount": format(c.amount, "f"),
                    "currency": c.currency})
        result[hk] = {"history_key": hk, "user_id": h.user_id, "user_label": h.user_label,
            "historical_receipt_id": h.receipt_id, "historical_filename": h.filename,
            "historical_event_date": h.event_date.isoformat(),
            "historical_amount": format(h.amount, "f"), "currency": h.currency,
            "current_matches": current, "group_covered": group_covered,
            "reason": ("同条件の前月取引は当月の照合済み取引で全件説明済みです。個別対応は未確定です。"
                       if group_covered else "この前月取引の翌月分は別の当月明細で照合済みのため、候補から除外しました。")}

    remaining = dict(edges)
    while remaining:
        old_best, new_best = {}, {}
        for (h, c), rank in remaining.items():
            old_best[h] = min(old_best.get(h, rank), rank)
            new_best[c] = min(new_best.get(c, rank), rank)
        pairs = []
        for (h, c), rank in sorted(remaining.items()):
            if rank != old_best[h] or rank != new_best[c]:
                continue
            if sum(1 for (hh, _), r in remaining.items() if hh == h and r == rank) != 1:
                continue
            if sum(1 for (_, cc), r in remaining.items() if cc == c and r == rank) != 1:
                continue
            pairs.append((h, c))
        if not pairs:
            break
        for h, c in pairs:
            explain(h, [c])
        hs, cs = {h for h, _ in pairs}, {c for _, c in pairs}
        remaining = {(h, c): rank for (h, c), rank in remaining.items() if h not in hs and c not in cs}

    # Connected components, then maximum-cardinality matching. Do not equate
    # counts alone with coverage (a Hall-deficient graph can have enough rows).
    unseen = {h for h, _ in remaining}
    while unseen:
        hs, cs, todo = set(), set(), [min(unseen)]
        while todo:
            h = todo.pop()
            if h in hs:
                continue
            hs.add(h)
            for hh, c in remaining:
                if hh != h:
                    continue
                if c not in cs:
                    cs.add(c)
                    todo.extend(other for other, cc in remaining if cc == c and other not in hs)
        unseen -= hs
        owner = {}
        def augment(h, visited):
            for c in sorted(cc for hh, cc in remaining if hh == h):
                if c in visited:
                    continue
                visited.add(c)
                if c not in owner or augment(owner[c], visited):
                    owner[c] = h
                    return True
            return False
        covered = all(augment(h, set()) for h in sorted(hs))
        if covered:
            for h in sorted(hs):
                explain(h, cs, group_covered=True)
    return result


def _submitter_support(line, h, tolerance):
    from .statement_matching import merchant_keys_compatible
    if (not line.transaction_date or not h.event_date or
            (h.event_date.year, h.event_date.month) != _previous_month(line.transaction_date) or
            not merchant_keys_compatible(line.merchant_key, h.merchant_key)):
        return None
    amounts = {(a.currency, a.amount) for a in line.amount_options
               if a.currency and a.amount.is_finite() and a.amount > 0}
    if not (amounts & _submitter_amounts(h)):
        # Neither nearby dates nor invented price changes justify naming a person.
        return None
    expected = _next_month_same_day(h.event_date)
    distance = abs((line.transaction_date - expected).days)
    # A confirmed prior association is stronger than today's registration flags.
    # Even metered/"other" charges can identify a contact; they do not prove a
    # recurring obligation or this month's buyer. Never turn a hint into MATCHED.
    if h.previously_matched and distance <= tolerance:
        return (3, "有力な確認先候補（前月照合実績・未確定）", distance, expected)
    if h.billing_type == "one_time":
        return None
    if h.billing_type == "metered":
        return (1, "参考候補（従量課金・未確定）", distance, expected)
    if distance > tolerance:
        return None
    if h.billing_type == "subscription":
        return (3, "有力候補（未確定）", distance, expected)
    return (2, "参考候補（金額・日付一致・未確定）", distance, expected)


def suggest_previous_month_submitters(
    line: PlanStatementLine, history: list[HistoricalSubmitterUsage],
    *, current_charges: list[HistoricalSubmitterUsage] | None = None,
    date_tolerance_days: int = 3, limit: int = 5,
    target_lines: list[PlanStatementLine] | None = None,
    resolved_history: dict[str, dict] | None = None,
) -> dict:
    """Find contacts from residual financial history, never certify non-submission.

    Confirmed same-contract monthly renewals are removed first. Fixed/unknown
    subscriptions require BOTH exact documented amount/currency and cadence.
    Metered exact-price history is a reference only. A known closer unmatched
    line takes precedence; ties stay alternative candidates with explicit warnings.
    A current document already allocated elsewhere is never shown as a missing
    document for this line. No database state or monetary evidence is changed.
    """
    from .statement_matching import merchant_keys_compatible
    result = {"version": SUBMITTER_HINT_VERSION, "suggestion_only": True,
              "consumes_receipts": False, "candidates": [], "total_candidates": 0,
              "truncated": False, "explained_history": [], "explained_history_count": 0,
              "no_candidate_reason": "前月の金額・通貨・請求周期と当月照合を確認しても、確認先を絞り込めません。"}
    if not line.transaction_date or not line.merchant_key or not line.amount_options:
        return result
    limit = max(1, min(int(limit), 20))
    tolerance = max(0, min(int(date_tolerance_days), 7))
    current_charges = current_charges or []
    covered = resolved_history if resolved_history is not None else resolve_submitted_recurring_history(
        history, current_charges, month=line.transaction_date, date_tolerance_days=tolerance)
    groups = _history_groups(history)
    explained = [covered[key] for key, rows in groups.items() if key in covered
                 and any(merchant_keys_compatible(line.merchant_key, h.merchant_key) for h in rows)]
    explained.sort(key=lambda r: (r["user_label"], r["historical_event_date"], r["history_key"]))
    result["explained_history_count"] = len(explained)
    result["explained_history"] = explained[:10]
    result["explained_history_truncated"] = len(explained) > 10
    best: dict[int, tuple[tuple, dict]] = {}
    for key, variants in groups.items():
        if key in covered:
            continue
        eligible = []
        for h in variants:
            support = _submitter_support(line, h, tolerance)
            if support:
                eligible.append((support, h))
        if not eligible:
            continue
        support, h = min(eligible, key=lambda pair: (-pair[0][0], pair[0][2],
            not pair[1].previously_matched, pair[1].historical_statement_id is None,
            pair[1].event_date, pair[1].filename))
        level, label, distance, expected = support
        competing = []
        if h.billing_type != "metered":
            supports = []
            for other in target_lines or [line]:
                scores = [_submitter_support(other, variant, tolerance) for variant in variants]
                scores = [score for score in scores if score]
                if scores:
                    supports.append((other.key, min(score[2] for score in scores)))
            closest = min((d for _, d in supports), default=distance)
            if distance > closest:
                continue
            competing = sorted({k for k, d in supports if d == closest and k != line.key})
        reasons = ["前月の同じ請求元（または既知の決済名義）の提出履歴",
                   f"前月利用日 {h.event_date.isoformat()}", "書類に記載された金額・通貨が一致"]
        if h.billing_type in {"metered", "one_time"}:
            reasons.append(f"翌月同日から{distance}日差。定期契約とは断定せず、前月の同額取引の確認先として表示しています")
        else:
            reasons.append(f"翌月同日から{distance}日差")
        if h.previously_matched:
            reasons.append("前月明細と提出書類の紐付け実績あり")
        if h.historical_statement_id is not None:
            reasons.append(f"前月明細 {h.historical_line_reference} の確定済み対応を参照")
        reasons.extend(h.review_notes)
        current, seen = [], set()
        for c in current_charges:
            if not _submitter_rows_compatible(h, c) or not c.event_date or c.confirmed_line_key:
                continue
            line_amounts = {(a.currency, a.amount) for a in line.amount_options}
            if (line_amounts & _submitter_amounts(c) and
                    abs((c.event_date - line.transaction_date).days) <= tolerance):
                identity = _submitter_event_key(c)
                if identity not in seen:
                    seen.add(identity)
                    current.append({"receipt_id": c.receipt_id, "filename": c.filename})
        candidate = {
            "user_id": h.user_id, "user_label": h.user_label,
            "service_id": h.service_id, "service_label": h.service_label,
            "support_level": level, "support_label": label,
            "reasons": reasons, "ambiguous": bool(competing),
            "historical_source_key": key,
            "historical_receipt_id": h.receipt_id, "historical_filename": h.filename,
            "historical_statement_id": h.historical_statement_id,
            "historical_line_reference": h.historical_line_reference,
            "historical_document_date": (h.document_event_date or h.event_date).isoformat(),
            "historical_event_date": h.event_date.isoformat(),
            "historical_amount": format(h.amount, "f"), "currency": h.currency,
            "expected_date": expected.isoformat(), "date_distance": distance,
            "current_receipts": current, "competing_line_keys": competing,
            "submission_state": "uploaded_review" if current else "not_confirmed",
            "submission_label": "当該取引の書類あり・照合要確認" if current else "当該取引の提出状況は未確認",
            "reference_only": True,
            "review_notes": list(h.review_notes),
        }
        if competing:
            candidate["support_label"] = "複数明細の確認先候補（未確定）"
            reasons.append("同じ前月取引が複数の未一致明細の候補です。複数件の未提出や各明細の購入者を意味しません")
        score = (level, -distance, int(h.previously_matched))
        if h.user_id not in best or score > best[h.user_id][0]:
            best[h.user_id] = (score, candidate)
    ranked = sorted(best.values(), key=lambda pair: (tuple(-v for v in pair[0]), pair[1]["user_label"]))
    top_score = ranked[0][0] if ranked else None
    top_tie = sum(1 for score, _ in ranked if score == top_score) > 1
    for score, candidate in ranked:
        if top_tie and score == top_score:
            candidate["ambiguous"] = True
            candidate["reasons"].append("同条件の候補者が複数いるため一人に特定できません")
    result["candidates"] = [candidate for _, candidate in ranked[:limit]]
    result["total_candidates"] = len(ranked)
    result["truncated"] = len(ranked) > limit
    return result
