from __future__ import annotations

"""Pure-Python reconciliation engine for company card statements.

The engine deliberately ignores user identity.  Its only task is to decide
whether each card-statement line has documentary evidence among all uploaded
receipts for the target receipt month.

Matching order:
1. De-duplicate documents only when an explicit transaction/invoice reference
   proves that they describe the same transaction.
2. Globally assign direct charge evidence by exact merchant, amount/currency,
   and transaction date within ±1 day.  The assignment maximises the number of
   matched lines before minimising date distance, so processing order cannot
   consume the wrong duplicate receipt.
3. Match an original charge recorded inside a refund/credit-note document.
4. Match exact net amounts made from one charge plus explicitly linked refunds.
5. Match exact same-merchant nearby net amounts only when a charge and
   unlinked refunds occur within the same ±1-day evidence window.

No fuzzy amount tolerance is used.  One evidence component can be consumed at
most once, while one PDF may legitimately contribute multiple components
(e.g. an original payment and a later refund shown on the same credit note).
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from itertools import combinations
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
MATCH_ORIGINAL_CHARGE = "original_charge"
MATCH_LINKED_REFUND_NET = "linked_refund_net"
MATCH_MERCHANT_REFUND_NET = "merchant_refund_net"


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


@dataclass
class ReconciliationResult:
    assignments: dict[str, MatchAssignment] = field(default_factory=dict)
    components_by_key: dict[str, EvidenceComponent] = field(default_factory=dict)
    deduplicated_component_keys: set[str] = field(default_factory=set)
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
) -> int:
    distance = abs((line.transaction_date - component.event_date).days) if line.transaction_date and component.event_date else 99
    # The scales preserve the semantic order: date difference first, then
    # evidence quality, then deterministic file/component order.
    return distance * 1_000_000 + component.quality_rank * 10_000 + component_rank


def _global_direct_matching(
    lines: Sequence[StatementLine],
    components: Sequence[EvidenceComponent],
    *,
    date_tolerance_days: int,
) -> list[tuple[StatementLine, EvidenceComponent, str]]:
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

    candidate_edges: dict[tuple[int, int], tuple[_FlowEdge, str]] = {}
    for line_index, line in enumerate(lines):
        if not line.transaction_date or not line.merchant_key:
            continue
        for component_index, component in enumerate(components):
            if component.role != ROLE_CHARGE:
                continue
            if component.merchant_key != line.merchant_key:
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
                _direct_edge_cost(line, component, component_rank=component_index),
            )
            candidate_edges[(line_index, component_index)] = (edge, amount_basis)

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

    matches: list[tuple[StatementLine, EvidenceComponent, str]] = []
    for (line_index, component_index), (edge, amount_basis) in candidate_edges.items():
        if edge.original_capacity == 1 and edge.capacity == 0:
            matches.append((lines[line_index], components[component_index], amount_basis))
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


def _find_refund_net_match(
    line: StatementLine,
    components: Sequence[EvidenceComponent],
    used_keys: set[str],
    *,
    date_tolerance_days: int,
    require_explicit_link: bool,
    max_refunds: int = 3,
) -> tuple[EvidenceComponent, tuple[EvidenceComponent, ...], str] | None:
    if not line.transaction_date or not line.merchant_key:
        return None

    charges = [
        component
        for component in components
        if component.key not in used_keys
        and component.role == ROLE_CHARGE
        and component.merchant_key == line.merchant_key
        and component.event_date
        and abs((component.event_date - line.transaction_date).days) <= date_tolerance_days
    ]
    refunds = [
        component
        for component in components
        if component.key not in used_keys
        and component.role == ROLE_REFUND
        and component.merchant_key == line.merchant_key
        and component.event_date
        and component.event_date.year == line.transaction_date.year
        and component.event_date.month == line.transaction_date.month
        # Explicit transaction linkage can connect a later refund to the original
        # card line (the empirical GitHub case was 22 days later).  Without an
        # explicit link, the audited corpus only supported merchant-level netting
        # when the refund date was also within ±1 day of the statement line.
        and (
            require_explicit_link
            or abs((component.event_date - line.transaction_date).days) <= date_tolerance_days
        )
    ]

    candidates: list[tuple[tuple, EvidenceComponent, tuple[EvidenceComponent, ...], str]] = []
    for charge in charges:
        eligible_refunds = [
            refund
            for refund in refunds
            if refund.currency == charge.currency
            and (not require_explicit_link or _reference_linked(charge, refund))
        ]
        for refund_count in range(1, min(max_refunds, len(eligible_refunds)) + 1):
            for refund_subset in combinations(eligible_refunds, refund_count):
                net_amount = charge.signed_amount + sum(
                    (refund.signed_amount for refund in refund_subset), Decimal("0")
                )
                basis = _component_matches_amount(line, net_amount, charge.currency)
                if basis is None:
                    continue
                sort_key = (
                    refund_count,
                    abs((charge.event_date - line.transaction_date).days),
                    max(abs((refund.event_date - line.transaction_date).days) for refund in refund_subset),
                    charge.quality_rank,
                    charge.receipt_order,
                    charge.key,
                    tuple(refund.key for refund in refund_subset),
                )
                candidates.append((sort_key, charge, tuple(refund_subset), basis))

    if not candidates:
        return None
    _sort_key, charge, refunds_found, basis = min(candidates, key=lambda value: value[0])
    return charge, refunds_found, basis


def reconcile_statement(
    lines: Sequence[StatementLine],
    evidence_components: Sequence[EvidenceComponent],
    *,
    date_tolerance_days: int = 1,
) -> ReconciliationResult:
    components, deduplicated_keys = deduplicate_components(evidence_components)
    components_by_key = {component.key: component for component in components}
    result = ReconciliationResult(
        components_by_key=components_by_key,
        deduplicated_component_keys=deduplicated_keys,
    )
    used_component_keys: set[str] = set()

    direct_matches = _global_direct_matching(
        lines,
        components,
        date_tolerance_days=date_tolerance_days,
    )
    for line, component, amount_basis in direct_matches:
        match_type = (
            MATCH_ORIGINAL_CHARGE
            if component.document_kind == DOC_REFUND and component.role == ROLE_CHARGE
            else MATCH_DIRECT
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
        memo = (
            f"返金書類のPayment history等に記載された元決済を確認しました。金額・通貨完全一致、請求元一致、{date_note}"
            if match_type == MATCH_ORIGINAL_CHARGE
            else f"金額・通貨完全一致、請求元一致、{date_note}"
        )
        result.assignments[line.key] = MatchAssignment(
            line_key=line.key,
            match_type=match_type,
            component_keys=(component.key,),
            amount_basis=amount_basis,
            memo=memo,
        )
        used_component_keys.add(component.key)

    remaining_lines = [line for line in lines if line.key not in result.assignments]

    # Explicit transaction/invoice linkage is stronger than merchant-cycle netting.
    for line in remaining_lines:
        linked = _find_refund_net_match(
            line,
            components,
            used_component_keys,
            date_tolerance_days=date_tolerance_days,
            require_explicit_link=True,
        )
        if linked is None:
            continue
        charge, refunds, basis = linked
        component_keys = (charge.key, *(refund.key for refund in refunds))
        result.assignments[line.key] = MatchAssignment(
            line_key=line.key,
            match_type=MATCH_LINKED_REFUND_NET,
            component_keys=component_keys,
            amount_basis=basis,
            memo="元決済と明示的に紐付く返金を相殺した正味額が完全一致しました。",
        )
        used_component_keys.update(component_keys)

    remaining_lines = [line for line in lines if line.key not in result.assignments]
    for line in remaining_lines:
        merchant_net = _find_refund_net_match(
            line,
            components,
            used_component_keys,
            date_tolerance_days=date_tolerance_days,
            require_explicit_link=False,
        )
        if merchant_net is None:
            continue
        charge, refunds, basis = merchant_net
        component_keys = (charge.key, *(refund.key for refund in refunds))
        result.assignments[line.key] = MatchAssignment(
            line_key=line.key,
            match_type=MATCH_MERCHANT_REFUND_NET,
            component_keys=component_keys,
            amount_basis=basis,
            memo="同一請求元の決済と近接する返金を相殺した正味額が完全一致しました。",
        )
        used_component_keys.update(component_keys)

    result.unused_component_keys = set(components_by_key) - used_component_keys
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
