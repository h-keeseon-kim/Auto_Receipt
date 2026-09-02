from __future__ import annotations

"""Pure-Python reconciliation engine for company card statements.

The engine deliberately ignores user identity.  Its only task is to decide
whether each card-statement line has documentary evidence among all uploaded
receipts for the target receipt month.

Matching order:
1. De-duplicate documents only when an explicit transaction/invoice reference
   proves that they describe the same transaction.
2. Globally assign direct charge evidence by exact amount/currency and
   transaction date within ±1 day. Merchant identity normally must match, but
   a small audited bridge table may connect a statement billing descriptor to
   the actual service shown on the receipt (for example Google Play billing for
   Google One). Exact merchant matches always outrank bridge matches. The
   assignment maximises the number of matched lines before minimising relation
   tier and date distance, so processing order cannot consume the wrong receipt.
3. Match an original charge recorded inside a refund/credit-note document.
4. Match exact net amounts made from one charge plus explicitly linked refunds.
5. Globally allocate later refunds from the same corporate-card merchant pool,
   allowing different user accounts and invoices while preventing component
   reuse across statement lines.
6. Link card return/cancellation rows to the original charge shared with the
   corresponding net re-booking group.

No fuzzy amount tolerance is used. One evidence component can be consumed at
most once across monetary assignments, while one original charge may also be
referenced by its return row in the same decision group. One PDF may legitimately
contribute multiple components (for example an original payment and a later
refund shown on the same credit note).
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Iterable, Sequence


ROLE_CHARGE = "charge"
ROLE_REFUND = "refund"
# ReceiptFinancialDocumentKind.CHARGE is the value persisted by ReceiptHub for
# a paid receipt.  "receipt" is still accepted as a legacy/test input below.
DOC_RECEIPT = "charge"
DOC_RECEIPT_LEGACY = "receipt"
DOC_REFUND = "refund"
DOC_INVOICE = "invoice"
DOC_UNKNOWN = "unknown"

MATCH_DIRECT = "direct"
MATCH_BILLING_BRIDGE = "billing_bridge"
MATCH_ORIGINAL_CHARGE = "original_charge"
MATCH_LINKED_REFUND_NET = "linked_refund_net"
MATCH_MERCHANT_REFUND_NET = "merchant_refund_net"
MATCH_REVERSAL_ORIGINAL_CHARGE = "reversal_original_charge"

STATEMENT_ROLE_CHARGE = "charge"
STATEMENT_ROLE_REVERSAL = "reversal"

USAGE_MODE_CONSUME = "consume"
USAGE_MODE_REFERENCE = "reference"

DEFAULT_REFUND_LOOKBACK_DAYS = 14
DEFAULT_REFUND_LOOKAHEAD_DAYS = 45
DEFAULT_MAX_REFUNDS_PER_NET = 8

MERCHANT_MATCH_EXACT = "exact"
MERCHANT_MATCH_BILLING_BRIDGE = "billing_bridge"

# Statement descriptors can be the payment channel rather than the purchased
# service. Keep this table deliberately narrow and evidence-based: Google Play
# / Google Play Japan may bill a Google One subscription, while Google Cloud is
# intentionally excluded from the same family.
MERCHANT_COMPATIBILITY_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"GOOGLE_PLAY", "GOOGLE_ONE"}),
)

MERCHANT_KEY_DISPLAY_NAMES = {
    "GOOGLE_PLAY": "Google Play決済",
    "GOOGLE_ONE": "Google One",
}


def _merchant_key_display_name(value: str) -> str:
    return MERCHANT_KEY_DISPLAY_NAMES.get(value, value)


def merchant_match_kind(statement_key: str, evidence_key: str) -> str | None:
    """Return the audited merchant relation used for direct reconciliation.

    Exact merchant identity is strongest. A billing bridge is only allowed for
    explicitly registered pairs and still requires exact amount/currency and
    the normal date window.
    """

    if not statement_key or not evidence_key:
        return None
    if statement_key == evidence_key:
        return MERCHANT_MATCH_EXACT
    pair = frozenset({statement_key, evidence_key})
    if len(pair) == 2 and pair in MERCHANT_COMPATIBILITY_GROUPS:
        return MERCHANT_MATCH_BILLING_BRIDGE
    return None


def merchant_keys_compatible(statement_key: str, evidence_key: str) -> bool:
    return merchant_match_kind(statement_key, evidence_key) is not None


@dataclass(frozen=True)
class AmountOption:
    amount: Decimal
    currency: str
    basis: str = ""

    def __post_init__(self):
        object.__setattr__(self, "amount", Decimal(str(self.amount)))
        object.__setattr__(self, "currency", (self.currency or "").upper())


@dataclass(frozen=True)
class StatementLine:
    key: str
    sequence: int
    transaction_date: date | None
    merchant_key: str
    amount_options: tuple[AmountOption, ...]
    reference: str = ""
    statement_role: str = STATEMENT_ROLE_CHARGE

    def __post_init__(self):
        object.__setattr__(self, "statement_role", (self.statement_role or STATEMENT_ROLE_CHARGE).lower())


@dataclass(frozen=True)
class EvidenceComponent:
    key: str
    receipt_id: int
    receipt_order: int
    filename: str
    merchant_key: str
    signed_amount: Decimal
    currency: str
    event_date: date | None
    role: str = ROLE_CHARGE
    document_kind: str = DOC_UNKNOWN
    invoice_number: str = ""
    transaction_id: str = ""
    related_transaction_id: str = ""
    source_label: str = ""
    payee: str = ""
    service_label: str = ""
    fingerprint: str = ""

    def __post_init__(self):
        object.__setattr__(self, "signed_amount", Decimal(str(self.signed_amount)))
        object.__setattr__(self, "currency", (self.currency or "").upper())
        object.__setattr__(self, "role", (self.role or ROLE_CHARGE).lower())
        object.__setattr__(self, "document_kind", (self.document_kind or DOC_UNKNOWN).lower())

    @property
    def quality_rank(self) -> int:
        """Lower is better when duplicate evidence has the same explicit ID."""

        if self.role == ROLE_CHARGE and self.document_kind in {DOC_RECEIPT, DOC_RECEIPT_LEGACY}:
            return 0
        if self.role == ROLE_CHARGE and self.document_kind == DOC_REFUND:
            return 1  # original charge shown in a credit note/refund document
        if self.document_kind == DOC_INVOICE:
            return 2
        if self.document_kind == DOC_REFUND:
            return 1
        return 3


@dataclass(frozen=True)
class MatchAssignment:
    line_key: str
    match_type: str
    component_keys: tuple[str, ...]
    amount_basis: str = ""
    memo: str = ""
    usage_mode: str = USAGE_MODE_CONSUME


@dataclass
class ReconciliationResult:
    assignments: dict[str, MatchAssignment] = field(default_factory=dict)
    components_by_key: dict[str, EvidenceComponent] = field(default_factory=dict)
    deduplicated_component_keys: set[str] = field(default_factory=set)
    reserved_component_keys: set[str] = field(default_factory=set)
    consumed_component_keys: set[str] = field(default_factory=set)
    referenced_component_keys: set[str] = field(default_factory=set)
    unused_component_keys: set[str] = field(default_factory=set)

    @property
    def matched_line_keys(self) -> set[str]:
        return set(self.assignments)


@dataclass
class _FlowEdge:
    to: int
    rev: int
    capacity: int
    cost: int
    original_capacity: int


def _add_edge(graph: list[list[_FlowEdge]], source: int, target: int, capacity: int, cost: int) -> _FlowEdge:
    forward = _FlowEdge(target, len(graph[target]), capacity, cost, capacity)
    reverse = _FlowEdge(source, len(graph[source]), 0, -cost, 0)
    graph[source].append(forward)
    graph[target].append(reverse)
    return forward


def _normalise_reference(value: str) -> str:
    return "".join(char for char in (value or "").upper().strip() if char.isalnum())


def _dedupe_identity(component: EvidenceComponent) -> tuple | None:
    if component.fingerprint:
        return ("fingerprint", component.fingerprint)
    invoice = _normalise_reference(component.invoice_number)
    # Charge evidence often exposes both an invoice number and a receipt/
    # payment-history identifier.  A paid receipt and the original-payment row
    # embedded in a later credit note can have different receipt numbers but
    # the same invoice number.  Invoice identity therefore takes precedence for
    # charge components.  This is required to avoid counting the same $22
    # payment twice when both the receipt and credit note were uploaded.
    if component.role == ROLE_CHARGE and invoice:
        # An explicit invoice number identifies the same submitted transaction
        # even when an invoice and a later paid receipt expose slightly different
        # document dates.  Paid evidence still wins via ``quality_rank``.
        return (
            "invoice",
            component.role,
            component.merchant_key,
            invoice,
            component.currency,
            component.signed_amount,
        )

    transaction = _normalise_reference(component.transaction_id)
    if transaction:
        return ("transaction", component.role, component.merchant_key, transaction)

    if invoice:
        # A paid receipt and the original-payment row embedded in its credit note
        # share an invoice number and should count once.  A refund itself remains a
        # separate component, even if the credit note references the same invoice.
        return (
            "invoice",
            component.role,
            component.merchant_key,
            invoice,
            component.currency,
            component.signed_amount,
        )
    return None


def deduplicate_components(components: Iterable[EvidenceComponent]) -> tuple[list[EvidenceComponent], set[str]]:
    selected: dict[tuple, EvidenceComponent] = {}
    unique: list[EvidenceComponent] = []
    removed: set[str] = set()

    for component in components:
        identity = _dedupe_identity(component)
        if identity is None:
            unique.append(component)
            continue
        current = selected.get(identity)
        if current is None:
            selected[identity] = component
            continue
        current_rank = (current.quality_rank, current.receipt_order, current.key)
        candidate_rank = (component.quality_rank, component.receipt_order, component.key)
        if candidate_rank < current_rank:
            removed.add(current.key)
            selected[identity] = component
        else:
            removed.add(component.key)

    unique.extend(selected.values())
    unique.sort(key=lambda component: (component.receipt_order, component.key))
    return unique, removed


def _matching_amount_basis(line: StatementLine, component: EvidenceComponent) -> str | None:
    # この照合の目的は「カード明細行に対応する提出PDFが存在するか」の確認である。
    # そのため、書類本文から請求元・取引日・金額・通貨を確認できるInvoiceも
    # documentary evidenceとして扱う。支払済み領収書と同じInvoice番号が重複した
    # 場合は、重複排除時に支払済み領収書を優先する。
    if component.role != ROLE_CHARGE or component.signed_amount < 0:
        return None
    for option in line.amount_options:
        if option.currency == component.currency and option.amount == component.signed_amount:
            return option.basis or option.currency
    return None


def _direct_edge_cost(
    line: StatementLine,
    component: EvidenceComponent,
    *,
    component_rank: int,
    merchant_relation: str,
) -> int:
    distance = abs((line.transaction_date - component.event_date).days) if line.transaction_date and component.event_date else 99
    # Max-flow decides the number of matches first. Within the maximum matching,
    # exact merchant identity outranks a known billing-channel bridge, then date
    # distance, document quality and deterministic file order are considered.
    relation_rank = 0 if merchant_relation == MERCHANT_MATCH_EXACT else 1
    return (
        relation_rank * 10_000_000
        + distance * 1_000_000
        + component.quality_rank * 10_000
        + component_rank
    )


def _global_direct_matching(
    lines: Sequence[StatementLine],
    components: Sequence[EvidenceComponent],
    *,
    date_tolerance_days: int,
    unavailable_keys: set[str] | None = None,
) -> list[tuple[StatementLine, EvidenceComponent, str, str]]:
    line_count = len(lines)
    component_count = len(components)
    source = 0
    line_offset = 1
    component_offset = line_offset + line_count
    sink = component_offset + component_count
    graph: list[list[_FlowEdge]] = [[] for _ in range(sink + 1)]

    for line_index in range(line_count):
        _add_edge(graph, source, line_offset + line_index, 1, 0)
    for component_index in range(component_count):
        _add_edge(graph, component_offset + component_index, sink, 1, 0)

    unavailable_keys = set(unavailable_keys or ())
    candidate_edges: dict[tuple[int, int], tuple[_FlowEdge, str, str]] = {}
    for line_index, line in enumerate(lines):
        if line.statement_role == STATEMENT_ROLE_REVERSAL:
            continue
        if not line.transaction_date or not line.merchant_key:
            continue
        for component_index, component in enumerate(components):
            if component.key in unavailable_keys or component.role != ROLE_CHARGE:
                continue
            merchant_relation = merchant_match_kind(line.merchant_key, component.merchant_key)
            if merchant_relation is None:
                continue
            if not component.event_date:
                continue
            if abs((line.transaction_date - component.event_date).days) > date_tolerance_days:
                continue
            amount_basis = _matching_amount_basis(line, component)
            if amount_basis is None:
                continue
            edge = _add_edge(
                graph,
                line_offset + line_index,
                component_offset + component_index,
                1,
                _direct_edge_cost(
                    line,
                    component,
                    component_rank=component_index,
                    merchant_relation=merchant_relation,
                ),
            )
            candidate_edges[(line_index, component_index)] = (edge, amount_basis, merchant_relation)

    node_count = len(graph)
    inf = 10**30
    while True:
        distance = [inf] * node_count
        previous: list[tuple[int, int] | None] = [None] * node_count
        distance[source] = 0

        # Bellman-Ford is intentionally used because residual reverse edges have
        # negative costs.  Statement sizes in ReceiptHub are small (< hundreds).
        for _ in range(node_count - 1):
            updated = False
            for node, edges in enumerate(graph):
                if distance[node] == inf:
                    continue
                for edge_index, edge in enumerate(edges):
                    if edge.capacity <= 0:
                        continue
                    candidate_distance = distance[node] + edge.cost
                    if candidate_distance < distance[edge.to]:
                        distance[edge.to] = candidate_distance
                        previous[edge.to] = (node, edge_index)
                        updated = True
            if not updated:
                break

        if distance[sink] == inf:
            break

        node = sink
        while node != source:
            previous_step = previous[node]
            if previous_step is None:
                raise RuntimeError("Broken residual path in statement matching")
            previous_node, edge_index = previous_step
            edge = graph[previous_node][edge_index]
            edge.capacity -= 1
            graph[node][edge.rev].capacity += 1
            node = previous_node

    matches: list[tuple[StatementLine, EvidenceComponent, str, str]] = []
    for (line_index, component_index), (edge, amount_basis, merchant_relation) in candidate_edges.items():
        if edge.original_capacity == 1 and edge.capacity == 0:
            matches.append((lines[line_index], components[component_index], amount_basis, merchant_relation))
    matches.sort(key=lambda value: (value[0].sequence, value[0].key))
    return matches


def _component_matches_amount(line: StatementLine, amount: Decimal, currency: str) -> str | None:
    for option in line.amount_options:
        if option.currency == currency and option.amount == amount:
            return option.basis or option.currency
    return None


def _reference_linked(charge: EvidenceComponent, refund: EvidenceComponent) -> bool:
    related = _normalise_reference(refund.related_transaction_id)
    if not related:
        return False
    return related in {
        _normalise_reference(charge.transaction_id),
        _normalise_reference(charge.invoice_number),
    } - {""}


@dataclass(frozen=True)
class _RefundNetCandidate:
    line: StatementLine
    charge: EvidenceComponent
    refunds: tuple[EvidenceComponent, ...]
    amount_basis: str
    score: tuple

    @property
    def component_keys(self) -> tuple[str, ...]:
        return (self.charge.key, *(refund.key for refund in self.refunds))


def _refund_date_allowed(
    line: StatementLine,
    refund: EvidenceComponent,
    *,
    lookback_days: int,
    lookahead_days: int,
) -> bool:
    if not line.transaction_date or not refund.event_date:
        return False
    delta = (refund.event_date - line.transaction_date).days
    return -lookback_days <= delta <= lookahead_days


def _candidate_score(
    line: StatementLine,
    charge: EvidenceComponent,
    refunds: tuple[EvidenceComponent, ...],
) -> tuple:
    charge_distance = (
        abs((charge.event_date - line.transaction_date).days)
        if charge.event_date and line.transaction_date
        else 9999
    )
    refund_deltas = [
        (refund.event_date - line.transaction_date).days
        for refund in refunds
        if refund.event_date and line.transaction_date
    ]
    pre_refund_deltas = [abs(delta) for delta in refund_deltas if delta < 0]
    return (
        len(pre_refund_deltas),
        sum(pre_refund_deltas),
        charge_distance,
        max((abs(delta) for delta in refund_deltas), default=0),
        sum(abs(delta) for delta in refund_deltas),
        len(refunds),
        charge.quality_rank,
        charge.receipt_order,
        charge.key,
        tuple(refund.key for refund in refunds),
    )


def _exact_refund_subsets(
    refunds: Sequence[EvidenceComponent],
    required_refund: Decimal,
    *,
    max_refunds: int,
    limit: int = 300,
) -> list[tuple[EvidenceComponent, ...]]:
    """Return exact refund subsets without enumerating every combination."""

    required_abs = -required_refund
    if required_abs <= 0:
        return []
    ordered = sorted(
        (
            refund
            for refund in refunds
            if refund.signed_amount < 0 and abs(refund.signed_amount) <= required_abs
        ),
        key=lambda refund: (
            refund.event_date or date.min,
            refund.receipt_order,
            refund.key,
        ),
    )
    results: list[tuple[EvidenceComponent, ...]] = []

    def visit(index: int, remaining: Decimal, chosen: list[EvidenceComponent]) -> None:
        if len(results) >= limit:
            return
        if remaining == 0:
            results.append(tuple(chosen))
            return
        if remaining < 0 or index >= len(ordered) or len(chosen) >= max_refunds:
            return
        # Even using every remaining refund cannot reach the target.
        available = sum((abs(refund.signed_amount) for refund in ordered[index:]), Decimal("0"))
        if available < remaining:
            return

        refund = ordered[index]
        amount = abs(refund.signed_amount)
        if amount <= remaining:
            chosen.append(refund)
            visit(index + 1, remaining - amount, chosen)
            chosen.pop()
        visit(index + 1, remaining, chosen)

    visit(0, required_abs, [])
    return results


def _generate_refund_net_candidates(
    line: StatementLine,
    components: Sequence[EvidenceComponent],
    unavailable_keys: set[str],
    *,
    charge_date_tolerance_days: int,
    require_explicit_link: bool,
    refund_lookback_days: int,
    refund_lookahead_days: int,
    max_refunds: int,
) -> list[_RefundNetCandidate]:
    """Generate exact charge-minus-refund candidates for one statement line.

    The charge must remain close to the original statement date. Refunds may be
    posted later because a card processor can cancel an old sale and re-book a
    net amount after several independent refunds have reached the same corporate
    card.  Every calculation remains exact in the original currency.
    """

    if (
        line.statement_role == STATEMENT_ROLE_REVERSAL
        or not line.transaction_date
        or not line.merchant_key
    ):
        return []

    charges = [
        component
        for component in components
        if component.key not in unavailable_keys
        and component.role == ROLE_CHARGE
        and component.signed_amount >= 0
        and component.merchant_key == line.merchant_key
        and component.event_date
        and abs((component.event_date - line.transaction_date).days) <= charge_date_tolerance_days
    ]
    refunds = [
        component
        for component in components
        if component.key not in unavailable_keys
        and component.role == ROLE_REFUND
        and component.signed_amount < 0
        and component.merchant_key == line.merchant_key
        and component.event_date
        and _refund_date_allowed(
            line,
            component,
            lookback_days=refund_lookback_days,
            lookahead_days=refund_lookahead_days,
        )
    ]

    candidates: list[_RefundNetCandidate] = []
    for charge in charges:
        eligible_refunds = [
            refund
            for refund in refunds
            if refund.currency == charge.currency
            and (not require_explicit_link or _reference_linked(charge, refund))
        ]
        if not eligible_refunds:
            continue

        for option in line.amount_options:
            if option.currency != charge.currency:
                continue
            required_refund = option.amount - charge.signed_amount
            if required_refund >= 0:
                continue
            for refund_subset in _exact_refund_subsets(
                eligible_refunds,
                required_refund,
                max_refunds=max_refunds,
            ):
                ordered_refunds = tuple(
                    sorted(
                        refund_subset,
                        key=lambda refund: (
                            refund.event_date or date.min,
                            refund.receipt_order,
                            refund.key,
                        ),
                    )
                )
                candidates.append(
                    _RefundNetCandidate(
                        line=line,
                        charge=charge,
                        refunds=ordered_refunds,
                        amount_basis=option.basis or option.currency,
                        score=_candidate_score(line, charge, ordered_refunds),
                    )
                )

    # The exact amount requirement normally leaves only a handful of plans.  A
    # deterministic cap protects the application from pathological uploads with
    # hundreds of identical micro-refunds without changing normal results.
    candidates.sort(key=lambda candidate: candidate.score)
    return candidates[:300]


def _selection_objective(candidates: Sequence[_RefundNetCandidate]) -> tuple:
    if not candidates:
        return (0, 0, 0, 0, 0, 0, 0, ())
    numeric_scores = [candidate.score[:8] for candidate in candidates]
    totals = tuple(sum(score[index] for score in numeric_scores) for index in range(8))
    deterministic = tuple(
        sorted(
            (
                candidate.line.sequence,
                candidate.line.key,
                candidate.component_keys,
            )
            for candidate in candidates
        )
    )
    # More matched lines is always better. After that, chronology and proximity
    # outrank the number of documents, preventing an old prior-month refund from
    # beating a slightly larger set of refunds posted after the original sale.
    return (-len(candidates), *totals, deterministic)


def _select_global_refund_net_candidates(
    lines: Sequence[StatementLine],
    candidates_by_line: dict[str, list[_RefundNetCandidate]],
    unavailable_keys: set[str],
) -> list[_RefundNetCandidate]:
    groups = [
        (line, candidates_by_line.get(line.key, []))
        for line in sorted(lines, key=lambda value: (value.sequence, value.key))
        if candidates_by_line.get(line.key)
    ]
    if not groups:
        return []

    best: list[_RefundNetCandidate] = []
    best_objective = _selection_objective(best)

    def visit(
        index: int,
        used: set[str],
        selected: list[_RefundNetCandidate],
    ) -> None:
        nonlocal best, best_objective
        # Even matching every remaining group cannot beat the current solution.
        if len(selected) + (len(groups) - index) < len(best):
            return
        if index >= len(groups):
            objective = _selection_objective(selected)
            if objective < best_objective:
                best = list(selected)
                best_objective = objective
            return

        _line, candidates = groups[index]
        # Include candidates before the skip branch so deterministic ties favour
        # a documented exact plan rather than an unnecessary unmatched line.
        for candidate in candidates:
            keys = set(candidate.component_keys)
            if keys.intersection(used) or keys.intersection(unavailable_keys):
                continue
            selected.append(candidate)
            visit(index + 1, used | keys, selected)
            selected.pop()
        visit(index + 1, used, selected)

    visit(0, set(unavailable_keys), [])
    return sorted(best, key=lambda candidate: (candidate.line.sequence, candidate.line.key))


def _apply_refund_net_phase(
    *,
    result: ReconciliationResult,
    lines: Sequence[StatementLine],
    components: Sequence[EvidenceComponent],
    used_component_keys: set[str],
    date_tolerance_days: int,
    require_explicit_link: bool,
    refund_lookback_days: int,
    refund_lookahead_days: int,
    max_refunds: int,
) -> None:
    remaining_lines = [
        line
        for line in lines
        if line.key not in result.assignments
        and line.statement_role != STATEMENT_ROLE_REVERSAL
    ]
    candidates_by_line = {
        line.key: _generate_refund_net_candidates(
            line,
            components,
            used_component_keys,
            charge_date_tolerance_days=date_tolerance_days,
            require_explicit_link=require_explicit_link,
            refund_lookback_days=refund_lookback_days,
            refund_lookahead_days=refund_lookahead_days,
            max_refunds=max_refunds,
        )
        for line in remaining_lines
    }
    selected = _select_global_refund_net_candidates(
        remaining_lines,
        candidates_by_line,
        used_component_keys,
    )
    for candidate in selected:
        match_type = MATCH_LINKED_REFUND_NET if require_explicit_link else MATCH_MERCHANT_REFUND_NET
        result.assignments[candidate.line.key] = MatchAssignment(
            line_key=candidate.line.key,
            match_type=match_type,
            component_keys=candidate.component_keys,
            amount_basis=candidate.amount_basis,
            memo=(
                "元決済と明示的に紐付く返金を相殺した正味額が完全一致しました。"
                if require_explicit_link
                else (
                    "同一法人カード・同一請求元の決済と後日返金をカード単位で相殺した正味額が"
                    "完全一致しました。返金証拠は他の明細へ重複使用していません。"
                )
            ),
        )
        used_component_keys.update(candidate.component_keys)


def _match_reversal_lines(
    *,
    result: ReconciliationResult,
    lines: Sequence[StatementLine],
    components: Sequence[EvidenceComponent],
    referenced_component_keys: set[str],
    date_tolerance_days: int,
) -> None:
    """Reference the original charge for card return/cancellation rows.

    A processor can show both a full return line and a later net re-booking.  In
    that case the same original receipt is intentionally referenced by both
    statement rows, but the monetary component is consumed only once by the
    net calculation.
    """

    assigned_charge_keys = {
        key
        for assignment in result.assignments.values()
        for key in assignment.component_keys
        if key in result.components_by_key
        and result.components_by_key[key].role == ROLE_CHARGE
    }
    referenced_by_reversal: set[str] = set()

    for line in sorted(lines, key=lambda value: (value.sequence, value.key)):
        if line.statement_role != STATEMENT_ROLE_REVERSAL or line.key in result.assignments:
            continue
        if not line.transaction_date or not line.merchant_key:
            continue

        ranked: list[tuple[tuple, EvidenceComponent, str]] = []
        for charge in components:
            if (
                charge.key in referenced_by_reversal
                or charge.role != ROLE_CHARGE
                or charge.merchant_key != line.merchant_key
                or not charge.event_date
            ):
                continue
            distance = abs((charge.event_date - line.transaction_date).days)
            if distance > date_tolerance_days:
                continue
            basis = _component_matches_amount(line, charge.signed_amount, charge.currency)
            exact_amount_rank = 0 if basis is not None else 1
            # If the statement did not expose the original foreign-currency
            # amount, a unique same-date charge already used by a net re-booking
            # is still strong evidence for the return row.
            assigned_rank = 0 if charge.key in assigned_charge_keys else 1
            ranked.append(
                (
                    (
                        exact_amount_rank,
                        assigned_rank,
                        distance,
                        charge.quality_rank,
                        charge.receipt_order,
                        charge.key,
                    ),
                    charge,
                    basis or "同日返品元決済",
                )
            )

        if not ranked:
            continue
        ranked.sort(key=lambda value: value[0])
        best_rank, charge, basis = ranked[0]
        # Without an exact amount, do not guess when multiple equally strong
        # same-date original charges exist.
        if best_rank[0] == 1:
            tied = [value for value in ranked if value[0][0:3] == best_rank[0:3]]
            if len(tied) != 1:
                continue

        result.assignments[line.key] = MatchAssignment(
            line_key=line.key,
            match_type=MATCH_REVERSAL_ORIGINAL_CHARGE,
            component_keys=(charge.key,),
            amount_basis=basis,
            memo=(
                "カード明細の返品・取消行です。同一請求元・同一利用日の元決済領収書を確認し、"
                "同日の正味再計上で使われた元決済と同一取引として参照しました。"
            ),
            usage_mode=USAGE_MODE_REFERENCE,
        )
        referenced_by_reversal.add(charge.key)
        referenced_component_keys.add(charge.key)


def reconcile_statement(
    lines: Sequence[StatementLine],
    evidence_components: Sequence[EvidenceComponent],
    *,
    date_tolerance_days: int = 1,
    refund_lookback_days: int = DEFAULT_REFUND_LOOKBACK_DAYS,
    refund_lookahead_days: int = DEFAULT_REFUND_LOOKAHEAD_DAYS,
    max_refunds_per_net: int = DEFAULT_MAX_REFUNDS_PER_NET,
    unavailable_component_keys: set[str] | None = None,
) -> ReconciliationResult:
    components, deduplicated_keys = deduplicate_components(evidence_components)
    components_by_key = {component.key: component for component in components}
    reserved_component_keys = set(unavailable_component_keys or ()).intersection(components_by_key)
    result = ReconciliationResult(
        components_by_key=components_by_key,
        deduplicated_component_keys=deduplicated_keys,
        reserved_component_keys=reserved_component_keys,
    )
    consumed_component_keys: set[str] = set(reserved_component_keys)
    referenced_component_keys: set[str] = set()

    normal_lines = [line for line in lines if line.statement_role != STATEMENT_ROLE_REVERSAL]
    direct_matches = _global_direct_matching(
        normal_lines,
        components,
        date_tolerance_days=date_tolerance_days,
        unavailable_keys=consumed_component_keys,
    )
    for line, component, amount_basis, merchant_relation in direct_matches:
        match_type = (
            MATCH_ORIGINAL_CHARGE
            if component.document_kind == DOC_REFUND and component.role == ROLE_CHARGE
            else (MATCH_BILLING_BRIDGE if merchant_relation == MERCHANT_MATCH_BILLING_BRIDGE else MATCH_DIRECT)
        )
        date_distance = (
            abs((line.transaction_date - component.event_date).days)
            if line.transaction_date and component.event_date
            else None
        )
        date_note = (
            f"利用日と領収書日付の差{date_distance}日です。"
            if date_distance is not None
            else "利用日差を確認できません。"
        )
        if match_type == MATCH_ORIGINAL_CHARGE:
            memo = (
                "返金書類のPayment history等に記載された元決済を確認しました。"
                f"金額・通貨完全一致、請求元一致、{date_note}"
            )
        elif match_type == MATCH_BILLING_BRIDGE:
            memo = (
                "金額・通貨完全一致。カード明細の決済名義と領収書本文のサービス名を、"
                "既知の請求経路対応（"
                f"{_merchant_key_display_name(line.merchant_key)} ↔ "
                f"{_merchant_key_display_name(component.merchant_key)}）として確認しました。"
                f"{date_note}"
            )
        else:
            memo = f"金額・通貨完全一致、請求元一致、{date_note}"
        result.assignments[line.key] = MatchAssignment(
            line_key=line.key,
            match_type=match_type,
            component_keys=(component.key,),
            amount_basis=amount_basis,
            memo=memo,
        )
        consumed_component_keys.add(component.key)

    # Explicit invoice/transaction linkage is stronger and is allocated first.
    _apply_refund_net_phase(
        result=result,
        lines=normal_lines,
        components=components,
        used_component_keys=consumed_component_keys,
        date_tolerance_days=date_tolerance_days,
        require_explicit_link=True,
        refund_lookback_days=refund_lookback_days,
        refund_lookahead_days=refund_lookahead_days,
        max_refunds=max_refunds_per_net,
    )
    # Remaining card-level netting plans are solved globally, so a refund cannot
    # be reused for 0424 and 0466 and a locally attractive candidate cannot
    # reduce the total number of explained statement lines.
    _apply_refund_net_phase(
        result=result,
        lines=normal_lines,
        components=components,
        used_component_keys=consumed_component_keys,
        date_tolerance_days=date_tolerance_days,
        require_explicit_link=False,
        refund_lookback_days=refund_lookback_days,
        refund_lookahead_days=refund_lookahead_days,
        max_refunds=max_refunds_per_net,
    )

    _match_reversal_lines(
        result=result,
        lines=lines,
        components=components,
        referenced_component_keys=referenced_component_keys,
        date_tolerance_days=date_tolerance_days,
    )

    result.consumed_component_keys = set(consumed_component_keys) - reserved_component_keys
    result.referenced_component_keys = referenced_component_keys
    # ``reference`` proves a relationship (for example a reversal row pointing
    # at the original charge) but does not spend the monetary evidence.  Keep
    # reference-only components in the globally available/unused set so a later
    # statement can still consume them exactly once.
    result.unused_component_keys = set(components_by_key) - consumed_component_keys
    return result


def format_evidence_calculation(components: Sequence[EvidenceComponent], target: AmountOption | None = None) -> str:
    parts: list[str] = []
    for component in components:
        amount = abs(component.signed_amount)
        number = format(amount, "f").rstrip("0").rstrip(".") or "0"
        if not parts:
            prefix = "-" if component.signed_amount < 0 else ""
        else:
            prefix = "-" if component.signed_amount < 0 else "+"
        role = "返金" if component.role == ROLE_REFUND else "決済"
        parts.append(f"{prefix}{number} {component.currency}（{role}）")
    if target is not None:
        number = format(target.amount, "f").rstrip("0").rstrip(".") or "0"
        return " ".join(parts) + f" = {number} {target.currency}"
    return " ".join(parts)
