from __future__ import annotations

import logging
import re
import threading
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.db import close_old_connections, transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    CardStatement,
    CardStatementItem,
    CardStatementMatchCandidate,
    CardStatementStatus,
    MonthlyServiceDeclaration,
    Receipt,
    ReceiptFilenameStatus,
    RegisteredService,
    ServiceCatalog,
    StatementCandidateStrength,
    StatementMatchStatus,
    receipt_month_for_statement,
)
from .statement_ai import generate_card_statement_analysis

logger = logging.getLogger(__name__)


CARD_STATEMENT_MONTH_SEMANTICS_RECONCILE_MARKER = (
    "【月次ルール更新】明細月と対象領収書月の対応を修正したため、最新の領収書と再照合します。"
)


def reconcile_pending_card_statement_month_semantics(*, period_month=None, statement_id=None) -> int:
    """v1.5.4移行で保留した既存明細を、保存済み行だけで一度だけ再照合する。"""

    queryset = CardStatement.objects.filter(
        ai_admin_memo__contains=CARD_STATEMENT_MONTH_SEMANTICS_RECONCILE_MARKER
    ).exclude(status__in=[CardStatementStatus.PROCESSING, CardStatementStatus.FAILED])
    if period_month is not None:
        queryset = queryset.filter(period_month=period_month)
    if statement_id is not None:
        queryset = queryset.filter(pk=statement_id)

    statement_ids = list(queryset.order_by("pk").values_list("pk", flat=True))
    for pending_statement_id in statement_ids:
        reconcile_card_statement_items(pending_statement_id)
    return len(statement_ids)


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").upper()
    return "".join(char for char in normalized if char.isalnum())


def _catalog_aliases(catalog: ServiceCatalog | None) -> list[str]:
    if catalog is None:
        return []
    raw_values = [catalog.name]
    raw_values.extend(re.split(r"[,;\n]+", catalog.merchant_aliases or ""))
    return [value for value in (_normalize_text(item.strip()) for item in raw_values) if value]


def _text_related(first: str, second: str) -> bool:
    left = _normalize_text(first)
    right = _normalize_text(second)
    if not left or not right:
        return False
    if left in right or right in left:
        return True
    # OPENAI * CHATGPT のように記号で分割された請求名義にも対応する。
    left_tokens = {token for token in re.findall(r"[A-Z0-9]{3,}", unicodedata.normalize("NFKC", first or "").upper())}
    right_tokens = {token for token in re.findall(r"[A-Z0-9]{3,}", unicodedata.normalize("NFKC", second or "").upper())}
    return bool(left_tokens and right_tokens and left_tokens.intersection(right_tokens))


def _merchant_matches_catalog(merchant: str, catalog: ServiceCatalog | None) -> bool:
    merchant_normalized = _normalize_text(merchant)
    if not merchant_normalized:
        return False
    return any(alias in merchant_normalized or merchant_normalized in alias for alias in _catalog_aliases(catalog))


def _amounts_equal(left: Decimal | None, right: Decimal | None, tolerance: Decimal) -> bool:
    if left is None or right is None:
        return False
    return abs(left - right) <= tolerance


def _amounts_close(left: Decimal | None, right: Decimal | None, *, minimum_tolerance: Decimal) -> bool:
    if left is None or right is None:
        return False
    tolerance = max(minimum_tolerance, abs(right) * Decimal("0.005"))
    return abs(left - right) <= tolerance


MAX_STORED_CANDIDATES = 5
CANDIDATE_TIE_MARGIN = 25
CROSS_USER_AMBIGUITY_MARGIN = 20


@dataclass(frozen=True)
class ReceiptCandidateEvaluation:
    item: CardStatementItem
    receipt: Receipt
    score: int
    confidence: float
    strength: str
    amount_match: bool
    amount_match_basis: str
    currency_match: bool
    merchant_match: bool
    service_match: bool
    date_match: bool
    exact_amount: bool
    rationale: str

    @property
    def auto_priority(self) -> int:
        # 完全な金額一致を最優先する。ご利用先名が異なっても一意なら提出済みと判定できる。
        if self.exact_amount and self.strength == StatementCandidateStrength.STRONG:
            return 4
        if self.exact_amount and self.strength == StatementCandidateStrength.AMOUNT_ONLY:
            return 3
        if self.strength == StatementCandidateStrength.STRONG:
            return 2
        return 0

    @property
    def sort_key(self):
        return (
            self.auto_priority,
            self.score,
            self.amount_match,
            self.merchant_match,
            self.service_match,
            self.date_match,
            -self.receipt.pk,
        )


def _evaluate_receipt_candidate(item: CardStatementItem, receipt: Receipt) -> ReceiptCandidateEvaluation | None:
    """明細行と領収書を、金額・払先・サービス・日付の複数根拠で評価する。"""

    score = 0
    receipt_catalog = receipt.service.catalog_service if receipt.service_id and receipt.service else None
    receipt_currency = (receipt.currency or "").upper()
    statement_currency = (item.original_currency or "").upper()

    currency_match = bool(statement_currency and receipt_currency == statement_currency)
    original_amount_exact = bool(
        currency_match and _amounts_equal(receipt.amount, item.original_amount, Decimal("0.02"))
    )
    original_amount_close = bool(
        currency_match
        and not original_amount_exact
        and _amounts_close(receipt.amount, item.original_amount, minimum_tolerance=Decimal("0.10"))
    )
    jpy_amount_exact = bool(
        receipt_currency == "JPY" and _amounts_equal(receipt.amount, item.amount_jpy, Decimal("1"))
    )
    jpy_amount_close = bool(
        receipt_currency == "JPY"
        and not jpy_amount_exact
        and _amounts_close(receipt.amount, item.amount_jpy, minimum_tolerance=Decimal("5"))
    )
    exact_amount = original_amount_exact or jpy_amount_exact
    amount_match = exact_amount or original_amount_close or jpy_amount_close
    if original_amount_exact:
        amount_match_basis = "original"
    elif jpy_amount_exact:
        amount_match_basis = "jpy"
    elif original_amount_close:
        amount_match_basis = "near_original"
    elif jpy_amount_close:
        amount_match_basis = "near_jpy"
    else:
        amount_match_basis = ""

    merchant_payee_match = bool(
        receipt.ai_extracted_payee and _text_related(item.merchant_name, receipt.ai_extracted_payee)
    )
    catalog_exact = bool(
        item.matched_catalog_service_id
        and receipt_catalog
        and item.matched_catalog_service_id == receipt_catalog.pk
    )
    merchant_catalog_match = bool(receipt_catalog and _merchant_matches_catalog(item.merchant_name, receipt_catalog))
    service_name_match = bool(receipt.service_id and _text_related(item.merchant_name, receipt.service.name))
    extra_memo_match = bool(receipt.is_extra and receipt.memo and _text_related(item.merchant_name, receipt.memo))
    merchant_match = merchant_payee_match
    service_match = catalog_exact or merchant_catalog_match or service_name_match or extra_memo_match
    catalog_conflict = bool(
        item.matched_catalog_service_id
        and receipt_catalog
        and item.matched_catalog_service_id != receipt_catalog.pk
    )

    date_match = False
    date_label = ""
    if receipt.issued_on and item.transaction_date:
        day_delta = abs((receipt.issued_on - item.transaction_date).days)
        if day_delta == 0:
            score += 75
            date_match = True
            date_label = "日付一致"
        elif day_delta <= 3:
            score += 42
            date_match = True
            date_label = f"日付差{day_delta}日"
        elif (receipt.issued_on.year, receipt.issued_on.month) == (
            item.transaction_date.year,
            item.transaction_date.month,
        ):
            score += 15
            date_match = True
            date_label = "同月"

    if currency_match:
        score += 20
    if original_amount_exact:
        score += 230
    elif jpy_amount_exact:
        score += 215
    elif original_amount_close:
        score += 105
    elif jpy_amount_close:
        score += 95

    if merchant_payee_match:
        score += 150
    if catalog_exact:
        score += 125
    if merchant_catalog_match:
        score += 90
    elif service_name_match:
        score += 60
    if extra_memo_match:
        score += 55

    # AIが示したサービスと異なっても、金額等が一致する領収書を候補から除外しない。
    if catalog_conflict:
        score -= 35
    if receipt.ai_filename_status == ReceiptFilenameStatus.GENERATED:
        score += 5

    # 日付だけでは候補にしない。
    if not (amount_match or merchant_match or service_match) or score < 55:
        return None

    if exact_amount and (merchant_match or service_match):
        strength = StatementCandidateStrength.STRONG
        confidence = 0.98 if merchant_match and service_match else 0.94
    elif exact_amount:
        strength = StatementCandidateStrength.AMOUNT_ONLY
        confidence = 0.78 if date_match else 0.72
    elif amount_match and (merchant_match or service_match):
        strength = StatementCandidateStrength.STRONG
        confidence = 0.86
    elif service_match and score >= 190:
        # 金額が領収書側に記録されていなくても、AI候補サービスと払先候補が重なる場合は従来どおり自動照合可能。
        strength = StatementCandidateStrength.STRONG
        confidence = 0.82
    else:
        strength = StatementCandidateStrength.POSSIBLE
        confidence = min(0.74, max(0.35, score / 500))

    reasons: list[str] = []
    if original_amount_exact:
        reasons.append(f"外貨金額 {item.original_amount} {statement_currency} が一致")
    elif jpy_amount_exact:
        reasons.append(f"円金額 {item.amount_jpy}円が一致")
    elif original_amount_close:
        reasons.append("外貨金額が近似")
    elif jpy_amount_close:
        reasons.append("円金額が近似")
    if currency_match:
        reasons.append("通貨一致")
    if merchant_payee_match:
        reasons.append("明細のご利用先と領収書の払先が関連")
    if catalog_exact:
        reasons.append("AI候補サービスと領収書サービスが一致")
    elif merchant_catalog_match:
        reasons.append("サービスマスターの払先候補と関連")
    elif service_name_match:
        reasons.append("サービス名と関連")
    if extra_memo_match:
        reasons.append("その他メモと関連")
    if date_label:
        reasons.append(date_label)
    if catalog_conflict:
        reasons.append("AI候補サービスとは異なる")

    return ReceiptCandidateEvaluation(
        item=item,
        receipt=receipt,
        score=score,
        confidence=confidence,
        strength=strength,
        amount_match=amount_match,
        amount_match_basis=amount_match_basis,
        currency_match=currency_match,
        merchant_match=merchant_match,
        service_match=service_match,
        date_match=date_match,
        exact_amount=exact_amount,
        rationale="、".join(reasons),
    )


def _registered_services_for_period(statement_month: date) -> list[RegisteredService]:
    target_receipt_month = receipt_month_for_statement(statement_month)
    return list(
        RegisteredService.objects.filter(user__is_active=True, user__is_staff=False, user__is_superuser=False)
        .filter(Q(is_active=True) | Q(is_active=False, final_receipt_month__gte=target_receipt_month))
        .select_related("user", "catalog_service")
        .order_by("user__username", "name", "billing_type")
    )


def _available_receipts_for_statement_month(statement_month: date) -> list[Receipt]:
    """明細月と同じ提出サイクルに保存された前月分領収書を返す。"""

    return list(
        Receipt.objects.available_files()
        .filter(
            submission__period_month=statement_month,
            submission__user__is_staff=False,
            submission__user__is_superuser=False,
        )
        .select_related("submission__user", "service", "service__catalog_service")
        .order_by("uploaded_at", "pk")
    )


def _is_manual_override(item: CardStatementItem) -> bool:
    return item.match_confidence >= 1.0 and (item.match_memo or "").startswith("管理者")


def _pick_best_receipt_for_service(
    *,
    item: CardStatementItem,
    service: RegisteredService,
    receipts: list[Receipt],
    used_receipt_ids: set[int],
) -> Receipt | None:
    candidates = [
        receipt for receipt in receipts if receipt.pk not in used_receipt_ids and receipt.service_id == service.pk
    ]
    if not candidates:
        return None
    evaluations = [
        evaluation
        for receipt in candidates
        if (evaluation := _evaluate_receipt_candidate(item, receipt)) is not None
    ]
    if evaluations:
        return max(evaluations, key=lambda evaluation: evaluation.sort_key).receipt
    return min(candidates, key=lambda receipt: receipt.pk)


def _base_match_memo(value: str) -> str:
    result = (value or "").strip()
    for marker in ("【領収書照合】", "【自動照合】"):
        result = result.split(marker, 1)[0].strip()
    return result


def _candidate_note(evaluation: ReceiptCandidateEvaluation) -> str:
    if evaluation.strength == StatementCandidateStrength.AMOUNT_ONLY:
        return (
            f"【領収書照合】金額と通貨が一意に一致した候補「{evaluation.receipt.display_filename}」を"
            "提出済み領収書として割り当てました。ご利用先・払先の関連は確認できないため、管理者確認対象です。"
        )
    return (
        f"【領収書照合】候補「{evaluation.receipt.display_filename}」を自動照合しました。"
        f"根拠: {evaluation.rationale or '複数項目の一致'}。"
    )


def _candidate_tie(candidates: list[ReceiptCandidateEvaluation], *, margin: int = CANDIDATE_TIE_MARGIN) -> bool:
    if len(candidates) < 2:
        return False
    first, second = candidates[0], candidates[1]
    return first.auto_priority == second.auto_priority and first.score - second.score < margin


def _persist_candidates(
    statement: CardStatement,
    items: list[CardStatementItem],
    evaluations_by_item: dict[int, list[ReceiptCandidateEvaluation]],
) -> int:
    CardStatementMatchCandidate.objects.filter(item__statement=statement).delete()
    candidate_rows: list[CardStatementMatchCandidate] = []
    for item in items:
        for rank, evaluation in enumerate(evaluations_by_item.get(item.pk, [])[:MAX_STORED_CANDIDATES], start=1):
            candidate_rows.append(
                CardStatementMatchCandidate(
                    item=item,
                    receipt=evaluation.receipt,
                    rank=rank,
                    score=evaluation.score,
                    confidence=evaluation.confidence,
                    strength=evaluation.strength,
                    amount_match=evaluation.amount_match,
                    amount_match_basis=evaluation.amount_match_basis,
                    currency_match=evaluation.currency_match,
                    merchant_match=evaluation.merchant_match,
                    service_match=evaluation.service_match,
                    date_match=evaluation.date_match,
                    rationale=evaluation.rationale,
                )
            )
    if candidate_rows:
        CardStatementMatchCandidate.objects.bulk_create(candidate_rows)
    return len(candidate_rows)


def reconcile_card_statement_items(statement_id: int, *, preserve_manual: bool = True) -> CardStatement:
    """明細行と前月分領収書を、複数候補・複数根拠で一対一照合する。

    ご利用先名が一致しない場合でも、外貨または円金額が一意に一致し、
    他候補との競合がなければ提出済み領収書として採用する。
    同額候補が複数ある場合は自動で決めず、管理者向け候補一覧を残す。
    """

    statement = CardStatement.objects.get(pk=statement_id)
    if statement.status == CardStatementStatus.PROCESSING:
        return statement

    items = list(
        statement.items.select_related(
            "matched_user",
            "matched_catalog_service",
            "matched_service__user",
            "matched_service__catalog_service",
            "matched_receipt__submission__user",
            "matched_receipt__service__catalog_service",
        ).order_by("sequence", "pk")
    )
    receipts = _available_receipts_for_statement_month(statement.period_month)
    services = _registered_services_for_period(statement.period_month)

    services_by_catalog: dict[int, list[RegisteredService]] = defaultdict(list)
    for service in services:
        if service.catalog_service_id:
            services_by_catalog[service.catalog_service_id].append(service)

    used_receipt_ids: set[int] = set()
    manual_items: set[int] = set()

    # 管理者が明示的に確定した行は、そのユーザー・サービス・領収書指定を維持する。
    for item in items:
        if preserve_manual and _is_manual_override(item):
            manual_items.add(item.pk)
            if item.match_status == StatementMatchStatus.IGNORED:
                item.matched_receipt = None
                continue
            if item.matched_receipt_id and item.matched_receipt and item.matched_receipt.file_available:
                used_receipt_ids.add(item.matched_receipt_id)
                continue
            if item.matched_service_id:
                item.matched_user = item.matched_service.user
                item.matched_catalog_service = item.matched_service.catalog_service
                chosen = _pick_best_receipt_for_service(
                    item=item,
                    service=item.matched_service,
                    receipts=receipts,
                    used_receipt_ids=used_receipt_ids,
                )
                item.matched_receipt = chosen
                if chosen:
                    used_receipt_ids.add(chosen.pk)

    evaluations_by_item: dict[int, list[ReceiptCandidateEvaluation]] = {}
    for item in items:
        evaluations = [
            evaluation
            for receipt in receipts
            if (evaluation := _evaluate_receipt_candidate(item, receipt)) is not None
        ]
        evaluations.sort(key=lambda candidate: candidate.sort_key, reverse=True)
        evaluations_by_item[item.pk] = evaluations

    assigned_item_ids: set[int] = set()
    ambiguous_item_reasons: dict[int, str] = {}

    def assign(item: CardStatementItem, candidate: ReceiptCandidateEvaluation):
        receipt = candidate.receipt
        item.matched_receipt = receipt
        item.matched_user = receipt.submission.user
        item.matched_service = receipt.service
        if receipt.service_id and receipt.service.catalog_service_id:
            item.matched_catalog_service = receipt.service.catalog_service
        item.match_status = (
            StatementMatchStatus.AMBIGUOUS
            if candidate.strength == StatementCandidateStrength.AMOUNT_ONLY
            else StatementMatchStatus.MATCHED
        )
        item.match_confidence = max(candidate.confidence, item.match_confidence)
        base_memo = _base_match_memo(item.match_memo)
        item.match_memo = " ".join(part for part in (base_memo, _candidate_note(candidate)) if part).strip()
        used_receipt_ids.add(receipt.pk)
        assigned_item_ids.add(item.pk)

    # まず金額を使う候補を、明細側・領収書側の双方で一意な場合だけ割り当てる。
    progress = True
    while progress:
        progress = False
        remaining_item_ids = {
            item.pk
            for item in items
            if item.pk not in manual_items and item.pk not in assigned_item_ids and item.receipt_required
        }
        available_receipt_ids = {receipt.pk for receipt in receipts if receipt.pk not in used_receipt_ids}
        if not remaining_item_ids or not available_receipt_ids:
            break

        item_amount_candidates: dict[int, list[ReceiptCandidateEvaluation]] = {}
        receipt_amount_candidates: dict[int, list[ReceiptCandidateEvaluation]] = defaultdict(list)
        for item_id in remaining_item_ids:
            candidates = [
                candidate
                for candidate in evaluations_by_item.get(item_id, [])
                if candidate.amount_match
                and candidate.auto_priority > 0
                and candidate.receipt.pk in available_receipt_ids
            ]
            candidates.sort(key=lambda candidate: candidate.sort_key, reverse=True)
            item_amount_candidates[item_id] = candidates
            for candidate in candidates:
                receipt_amount_candidates[candidate.receipt.pk].append(candidate)
        for candidates in receipt_amount_candidates.values():
            candidates.sort(key=lambda candidate: candidate.sort_key, reverse=True)

        mutual_best: list[ReceiptCandidateEvaluation] = []
        for item_id, candidates in item_amount_candidates.items():
            if not candidates or _candidate_tie(candidates):
                if len(candidates) > 1 and candidates[0].exact_amount:
                    ambiguous_item_reasons[item_id] = (
                        "金額が一致する領収書候補が複数あるため、自動では確定していません。"
                    )
                continue
            top = candidates[0]
            reverse_candidates = receipt_amount_candidates.get(top.receipt.pk, [])
            if not reverse_candidates or reverse_candidates[0].item.pk != item_id or _candidate_tie(reverse_candidates):
                ambiguous_item_reasons[item_id] = (
                    "金額が一致する候補がありますが、同じ領収書が複数の明細行に該当するため自動確定していません。"
                )
                continue
            mutual_best.append(top)

        mutual_best.sort(key=lambda candidate: candidate.sort_key, reverse=True)
        for candidate in mutual_best:
            if candidate.item.pk in assigned_item_ids or candidate.receipt.pk in used_receipt_ids:
                continue
            assign(candidate.item, candidate)
            progress = True

    # 金額情報がなくても、サービス・払先の関連が十分強い候補は従来どおり一対一で割り当てる。
    service_pairs: list[ReceiptCandidateEvaluation] = []
    for item in items:
        if item.pk in manual_items or item.pk in assigned_item_ids or not item.receipt_required:
            continue
        candidates = [
            candidate
            for candidate in evaluations_by_item.get(item.pk, [])
            if not candidate.amount_match
            and candidate.strength == StatementCandidateStrength.STRONG
            and candidate.receipt.pk not in used_receipt_ids
        ]
        candidates.sort(key=lambda candidate: candidate.sort_key, reverse=True)
        if len(candidates) >= 2:
            first, second = candidates[0], candidates[1]
            if (
                first.receipt.submission.user_id != second.receipt.submission.user_id
                and first.score - second.score < CROSS_USER_AMBIGUITY_MARGIN
            ):
                ambiguous_item_reasons[item.pk] = (
                    "複数ユーザーの領収書が同程度に一致するため、自動ではユーザーを確定していません。"
                )
                continue
        service_pairs.extend(candidates)
    service_pairs.sort(
        key=lambda candidate: (candidate.score, -candidate.item.sequence, -candidate.receipt.pk),
        reverse=True,
    )
    for candidate in service_pairs:
        if candidate.item.pk in assigned_item_ids or candidate.receipt.pk in used_receipt_ids:
            continue
        assign(candidate.item, candidate)

    no_usage_conflicts: list[str] = []
    missing_count = 0
    manual_review_count = 0

    for item in items:
        if item.pk not in manual_items and item.pk not in assigned_item_ids:
            # 前回の自動照合結果を外し、現在保存されている領収書だけで再判定する。
            item.matched_receipt = None
            item.matched_service = None
            item.matched_user = None
            base_memo = _base_match_memo(item.match_memo)

            candidate_services = services_by_catalog.get(item.matched_catalog_service_id, [])
            if len(candidate_services) == 1:
                item.matched_service = candidate_services[0]
                item.matched_user = candidate_services[0].user
                if item.match_status == StatementMatchStatus.UNMATCHED:
                    item.match_status = StatementMatchStatus.MATCHED
            elif len(candidate_services) > 1:
                item.matched_service = None
                item.matched_user = None
                if item.receipt_required:
                    item.match_status = StatementMatchStatus.AMBIGUOUS
                    suffix = "同じサービスを複数ユーザーが利用しているため、未提出ユーザーは自動特定できません。"
                    base_memo = " ".join(part for part in (base_memo, suffix) if part).strip()
            elif item.receipt_required and item.match_status == StatementMatchStatus.MATCHED:
                item.match_status = StatementMatchStatus.AMBIGUOUS

            display_candidates = evaluations_by_item.get(item.pk, [])[:MAX_STORED_CANDIDATES]
            ambiguity_reason = ambiguous_item_reasons.get(item.pk)
            if ambiguity_reason:
                item.match_status = StatementMatchStatus.AMBIGUOUS
                if display_candidates:
                    item.match_confidence = max(item.match_confidence, display_candidates[0].confidence)
                note = f"【領収書照合】{ambiguity_reason} 候補一覧から管理者が確認してください。"
                item.match_memo = " ".join(part for part in (base_memo, note) if part).strip()
            elif display_candidates and item.receipt_required:
                item.match_status = StatementMatchStatus.AMBIGUOUS
                item.match_confidence = max(item.match_confidence, display_candidates[0].confidence)
                note = (
                    f"【領収書照合】領収書候補を{len(display_candidates)}件作成しましたが、"
                    "自動確定に必要な根拠または一意性が不足しています。候補一覧から確認してください。"
                )
                item.match_memo = " ".join(part for part in (base_memo, note) if part).strip()
            else:
                item.match_memo = base_memo

        if item.matched_receipt_id and item.matched_receipt:
            item.matched_user = item.matched_receipt.submission.user
            item.matched_service = item.matched_receipt.service
            if item.matched_service_id:
                item.matched_catalog_service = item.matched_service.catalog_service

        if item.receipt_required and item.matched_service_id:
            deleted, _ = MonthlyServiceDeclaration.objects.filter(
                user=item.matched_service.user,
                service=item.matched_service,
                period_month=statement.period_month,
                no_usage=True,
            ).delete()
            if deleted:
                conflict = (
                    f"{item.matched_service.user.username} の {item.matched_service.display_name} は「対象領収書月は利用なし」申告でしたが、"
                    "カード明細に請求があるため申告を取り消しました。"
                )
                no_usage_conflicts.append(conflict)
                if conflict not in (item.match_memo or ""):
                    item.match_memo = f"{item.match_memo} {conflict}".strip()

        if item.receipt_required and not (
            item.matched_receipt_id and item.matched_receipt and item.matched_receipt.file_available
        ):
            missing_count += 1
        if item.match_status in {StatementMatchStatus.AMBIGUOUS, StatementMatchStatus.UNMATCHED} or (
            item.receipt_required and item.matched_user_id is None
        ):
            manual_review_count += 1

    with transaction.atomic():
        for item in items:
            item.save(
                update_fields=[
                    "matched_user",
                    "matched_catalog_service",
                    "matched_service",
                    "matched_receipt",
                    "match_status",
                    "match_confidence",
                    "match_memo",
                ]
            )
        candidate_count = _persist_candidates(statement, items, evaluations_by_item)

        target_month = statement.period_month.strftime("%Y-%m")
        card_or_period_problem = (
            statement.card_last4 != str(getattr(settings, "RECEIPT_CARD_LAST4", "7210"))[-4:]
            or statement.statement_period != target_month
        )
        if statement.status != CardStatementStatus.FAILED:
            statement.status = (
                CardStatementStatus.NEEDS_REVIEW
                if card_or_period_problem or not items or missing_count or manual_review_count
                else CardStatementStatus.COMPLETED
            )
        extraction_memo = (statement.ai_admin_memo or "").split("【照合結果】", 1)[0]
        extraction_memo = extraction_memo.replace(
            CARD_STATEMENT_MONTH_SEMANTICS_RECONCILE_MARKER,
            "",
        ).strip()
        target_receipt_month = receipt_month_for_statement(statement.period_month)
        reconciliation_memo = (
            f"【照合結果】明細月 {statement.period_month:%Y-%m} "
            f"（対象領収書月 {target_receipt_month:%Y-%m} / 提出月 {statement.period_month:%Y-%m}）の"
            f"全ユーザー領収書{len(receipts)}件を、金額・通貨・日付・ご利用先・サービスの複数根拠で照合し、"
            f"候補{candidate_count}件、領収書未提出{missing_count}件、手動確認{manual_review_count}件です。"
        )
        if no_usage_conflicts:
            reconciliation_memo += " " + " ".join(dict.fromkeys(no_usage_conflicts))
        statement.ai_admin_memo = " ".join(part for part in (extraction_memo, reconciliation_memo) if part)[:5000]
        statement.reconciled_at = timezone.now()
        statement.save(update_fields=["status", "ai_admin_memo", "reconciled_at", "updated_at"])
    return statement


def process_card_statement(statement_id: int):
    statement = CardStatement.objects.get(pk=statement_id)
    if statement.status != CardStatementStatus.PROCESSING:
        return None
    if not statement.file_available:
        statement.status = CardStatementStatus.FAILED
        statement.ai_admin_memo = "明細ファイルが保存されていないため解析できません。"
        statement.processed_at = timezone.now()
        statement.save(update_fields=["status", "ai_admin_memo", "processed_at", "updated_at"])
        return None

    try:
        with statement.file.open("rb") as file_obj:
            file_bytes = file_obj.read()
    except Exception as exc:
        statement.status = CardStatementStatus.FAILED
        statement.ai_admin_memo = f"明細ファイルを読み込めませんでした: {exc}"
        statement.processed_at = timezone.now()
        statement.save(update_fields=["status", "ai_admin_memo", "processed_at", "updated_at"])
        return None

    catalogs = list(
        ServiceCatalog.objects.filter(registered_services__user__is_staff=False, registered_services__user__is_superuser=False)
        .distinct()
        .order_by("name", "billing_type")
    )
    result = generate_card_statement_analysis(
        file_bytes=file_bytes,
        original_filename=statement.original_filename or Path(statement.file.name).name,
        content_type=statement.content_type,
        period_month=statement.period_month,
        service_catalogs=catalogs,
    )
    catalog_by_id = {catalog.pk: catalog for catalog in catalogs}

    with transaction.atomic():
        CardStatementItem.objects.filter(statement=statement).delete()
        items = [
            CardStatementItem(
                statement=statement,
                sequence=sequence,
                line_reference=extracted.line_reference,
                transaction_date=extracted.transaction_date,
                merchant_name=extracted.merchant_name,
                merchant_normalized=_normalize_text(extracted.merchant_name),
                amount_jpy=extracted.amount_jpy,
                original_amount=extracted.original_amount,
                original_currency=extracted.original_currency,
                matched_catalog_service=catalog_by_id.get(extracted.service_catalog_id),
                match_status=extracted.match_status,
                match_confidence=extracted.confidence,
                match_memo=extracted.reason,
                receipt_required=extracted.receipt_required,
            )
            for sequence, extracted in enumerate(result.items, start=1)
        ]
        CardStatementItem.objects.bulk_create(items)
        statement.status = result.status
        statement.card_last4 = result.card_last4
        statement.statement_period = result.statement_period
        statement.payment_date = result.payment_date
        statement.ai_admin_memo = result.admin_memo
        statement.processed_at = timezone.now()
        statement.save(
            update_fields=[
                "status",
                "card_last4",
                "statement_period",
                "payment_date",
                "ai_admin_memo",
                "processed_at",
                "updated_at",
            ]
        )

    if result.status != CardStatementStatus.FAILED:
        reconcile_card_statement_items(statement.pk, preserve_manual=False)
    return result


def start_background_statement_processing(statement_id: int) -> threading.Thread:
    def worker():
        close_old_connections()
        try:
            process_card_statement(statement_id)
        except Exception as exc:  # pragma: no cover - final safety net
            logger.exception("Card statement %s processing failed", statement_id)
            try:
                statement = CardStatement.objects.get(pk=statement_id)
                statement.status = CardStatementStatus.FAILED
                statement.ai_admin_memo = f"カード明細解析中に予期しないエラーが発生しました: {exc.__class__.__name__}: {exc}"
                statement.processed_at = timezone.now()
                statement.save(update_fields=["status", "ai_admin_memo", "processed_at", "updated_at"])
            except Exception:
                logger.exception("Card statement %s could not be marked failed", statement_id)
        finally:
            close_old_connections()

    thread = threading.Thread(target=worker, name=f"card-statement-{statement_id}", daemon=True)
    thread.start()
    return thread
