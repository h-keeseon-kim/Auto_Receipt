from __future__ import annotations

import logging
import re
import threading
import unicodedata
from collections import defaultdict
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
    CardStatementStatus,
    MonthlyServiceDeclaration,
    Receipt,
    ReceiptFilenameStatus,
    RegisteredService,
    ServiceCatalog,
    StatementMatchStatus,
)
from .statement_ai import generate_card_statement_analysis

logger = logging.getLogger(__name__)


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


def _receipt_match_score(item: CardStatementItem, receipt: Receipt) -> int:
    """全ユーザーの領収書から、カード明細1行に最も近い候補を評価する。"""

    score = 0
    receipt_catalog = receipt.service.catalog_service if receipt.service_id and receipt.service else None
    receipt_currency = (receipt.currency or "").upper()
    statement_currency = (item.original_currency or "").upper()

    if item.matched_catalog_service_id and receipt_catalog and item.matched_catalog_service_id == receipt_catalog.pk:
        score += 150
    elif item.matched_catalog_service_id and receipt.service_id:
        # AIが別サービスマスターを示している場合は誤照合を避ける。
        score -= 120

    if receipt.ai_extracted_payee and _text_related(item.merchant_name, receipt.ai_extracted_payee):
        score += 110
    if receipt_catalog and _merchant_matches_catalog(item.merchant_name, receipt_catalog):
        score += 70
    elif receipt.service_id and _text_related(item.merchant_name, receipt.service.name):
        score += 45
    if receipt.is_extra and receipt.memo and _text_related(item.merchant_name, receipt.memo):
        score += 35

    if statement_currency and receipt_currency == statement_currency:
        score += 10
        if _amounts_equal(receipt.amount, item.original_amount, Decimal("0.02")):
            score += 170
    if receipt_currency == "JPY" and _amounts_equal(receipt.amount, item.amount_jpy, Decimal("1")):
        score += 170

    if receipt.issued_on and item.transaction_date:
        day_delta = abs((receipt.issued_on - item.transaction_date).days)
        if day_delta == 0:
            score += 70
        elif day_delta <= 3:
            score += 35
        elif (receipt.issued_on.year, receipt.issued_on.month) == (
            item.transaction_date.year,
            item.transaction_date.month,
        ):
            score += 12

    if receipt.ai_filename_status == ReceiptFilenameStatus.GENERATED:
        score += 5
    return score


def _registered_services_for_period(period_month: date) -> list[RegisteredService]:
    return list(
        RegisteredService.objects.filter(user__is_active=True, user__is_staff=False, user__is_superuser=False)
        .filter(Q(is_active=True) | Q(is_active=False, final_receipt_month__gte=period_month))
        .select_related("user", "catalog_service")
        .order_by("user__username", "name", "billing_type")
    )


def _available_receipts_for_period(period_month: date) -> list[Receipt]:
    return list(
        Receipt.objects.available_files()
        .filter(submission__period_month=period_month, submission__user__is_staff=False, submission__user__is_superuser=False)
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
        receipt
        for receipt in receipts
        if receipt.pk not in used_receipt_ids and receipt.service_id == service.pk
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda receipt: (_receipt_match_score(item, receipt), -receipt.pk))


def reconcile_card_statement_items(statement_id: int, *, preserve_manual: bool = True) -> CardStatement:
    """カード明細を、対象月に全ユーザーが提出した領収書と一対一で再照合する。"""

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
    receipts = _available_receipts_for_period(statement.period_month)
    services = _registered_services_for_period(statement.period_month)

    services_by_catalog: dict[int, list[RegisteredService]] = defaultdict(list)
    for service in services:
        if service.catalog_service_id:
            services_by_catalog[service.catalog_service_id].append(service)

    used_receipt_ids: set[int] = set()
    manual_items: set[int] = set()

    # 管理者が明示的に確定した行は、そのユーザー・サービス指定を維持する。
    for item in items:
        if preserve_manual and _is_manual_override(item):
            manual_items.add(item.pk)
            if item.match_status == StatementMatchStatus.IGNORED:
                item.matched_receipt = None
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

    # 未確定行と未使用領収書の全組み合わせを評価し、得点の高い順に一対一で割り当てる。
    scored_pairs: list[tuple[int, int, int, CardStatementItem, Receipt]] = []
    for item in items:
        if item.pk in manual_items or not item.receipt_required:
            continue
        item_candidates: list[tuple[int, Receipt]] = []
        for receipt in receipts:
            if receipt.pk in used_receipt_ids:
                continue
            score = _receipt_match_score(item, receipt)
            # サービス一致のみ、または払先+金額等の十分な根拠がある場合に候補とする。
            if score >= 130:
                item_candidates.append((score, receipt))
        item_candidates.sort(key=lambda row: (row[0], -row[1].pk), reverse=True)
        if len(item_candidates) >= 2:
            first_score, first_receipt = item_candidates[0]
            second_score, second_receipt = item_candidates[1]
            if (
                first_receipt.submission.user_id != second_receipt.submission.user_id
                and first_score - second_score < 20
            ):
                item.match_status = StatementMatchStatus.AMBIGUOUS
                note = "複数ユーザーの領収書が同程度に一致するため、自動ではユーザーを確定していません。"
                if note not in (item.match_memo or ""):
                    item.match_memo = f"{item.match_memo} {note}".strip()
                continue
        for score, receipt in item_candidates:
            scored_pairs.append((score, -item.sequence, -receipt.pk, item, receipt))
    scored_pairs.sort(reverse=True, key=lambda row: (row[0], row[1], row[2]))

    assigned_item_ids: set[int] = set()
    for _score, _sequence, _receipt_pk, item, receipt in scored_pairs:
        if item.pk in assigned_item_ids or receipt.pk in used_receipt_ids:
            continue
        item.matched_receipt = receipt
        item.matched_user = receipt.submission.user
        item.matched_service = receipt.service
        if receipt.service_id and receipt.service.catalog_service_id:
            item.matched_catalog_service = receipt.service.catalog_service
        item.match_status = StatementMatchStatus.MATCHED
        item.match_confidence = max(item.match_confidence, 0.95)
        base_memo = (item.match_memo or "").strip()
        receipt_note = f"全ユーザーの提出領収書「{receipt.display_filename}」と照合しました。"
        if receipt_note not in base_memo:
            item.match_memo = f"{base_memo} {receipt_note}".strip()
        used_receipt_ids.add(receipt.pk)
        assigned_item_ids.add(item.pk)

    no_usage_conflicts: list[str] = []
    missing_count = 0
    manual_review_count = 0

    for item in items:
        if item.pk not in manual_items and item.pk not in assigned_item_ids:
            # 前回の自動照合結果を外し、現在保存されている領収書だけで再判定する。
            # 管理者が確定した行は manual_items として上で保持される。
            item.matched_receipt = None
            item.matched_service = None
            item.matched_user = None
            # AIがサービスマスターを特定できた場合、利用者が1人だけならユーザーまで特定する。
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
                    if suffix not in (item.match_memo or ""):
                        item.match_memo = f"{item.match_memo} {suffix}".strip()
            elif item.receipt_required and item.match_status == StatementMatchStatus.MATCHED:
                item.match_status = StatementMatchStatus.AMBIGUOUS

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
                    f"{item.matched_service.user.username} の {item.matched_service.display_name} は「当月利用なし」申告でしたが、"
                    "カード明細に請求があるため申告を取り消しました。"
                )
                no_usage_conflicts.append(conflict)
                if conflict not in (item.match_memo or ""):
                    item.match_memo = f"{item.match_memo} {conflict}".strip()

        if item.receipt_required and not (item.matched_receipt_id and item.matched_receipt and item.matched_receipt.file_available):
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
        extraction_memo = (statement.ai_admin_memo or "").split("【照合結果】", 1)[0].strip()
        reconciliation_memo = (
            f"【照合結果】対象月の全ユーザー領収書{len(receipts)}件と照合し、"
            f"領収書未提出{missing_count}件、手動確認{manual_review_count}件です。"
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
