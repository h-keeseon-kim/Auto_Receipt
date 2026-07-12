from __future__ import annotations

import logging
import threading
from pathlib import Path

from django.db import close_old_connections, transaction
from django.utils import timezone

from .models import (
    CardStatement,
    CardStatementItem,
    CardStatementStatus,
    MonthlyServiceDeclaration,
    Receipt,
    RegisteredService,
    StatementMatchStatus,
)
from .statement_ai import generate_card_statement_analysis

logger = logging.getLogger(__name__)


def _receipt_match_score(receipt: Receipt, extracted) -> tuple[int, int]:
    """カード明細1行に対する領収書候補の優先度。

    同じサービスの領収書が複数ある場合、AI抽出済みの日付・金額・通貨を
    使って近い候補を優先する。情報が未抽出ならアップロード順を維持する。
    戻り値の2番目は同点時に古い領収書を優先するための値。
    """

    score = 0
    receipt_currency = (receipt.currency or "").upper()
    statement_currency = (getattr(extracted, "original_currency", "") or "").upper()
    receipt_amount = receipt.amount

    if receipt_amount is not None:
        if statement_currency and receipt_currency == statement_currency and extracted.original_amount is not None:
            if abs(receipt_amount - extracted.original_amount) <= 0.01:
                score += 100
        if receipt_currency == "JPY" and extracted.amount_jpy is not None:
            if abs(receipt_amount - extracted.amount_jpy) <= 1:
                score += 100

    if receipt.issued_on and extracted.transaction_date:
        if receipt.issued_on == extracted.transaction_date:
            score += 50
        elif (receipt.issued_on.year, receipt.issued_on.month) == (
            extracted.transaction_date.year,
            extracted.transaction_date.month,
        ):
            score += 10

    return score, -int(receipt.pk or 0)


def _take_best_unused_receipt(receipts: list[Receipt], extracted) -> Receipt | None:
    """候補リストから未使用の領収書を1件だけ割り当てる。"""

    if not receipts:
        return None
    best = max(receipts, key=lambda receipt: _receipt_match_score(receipt, extracted))
    receipts.remove(best)
    return best


def process_card_statement(statement_id: int):
    statement = CardStatement.objects.select_related("user").get(pk=statement_id)
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

    services = list(
        RegisteredService.objects.uploadable_for(statement.user, statement.period_month)
        .select_related("catalog_service")
        .order_by("name", "billing_type")
    )
    result = generate_card_statement_analysis(
        file_bytes=file_bytes,
        original_filename=statement.original_filename or Path(statement.file.name).name,
        content_type=statement.content_type,
        period_month=statement.period_month,
        services=services,
    )

    service_by_id = {service.pk: service for service in services}
    receipts = list(
        Receipt.objects.available_files()
        .filter(
            submission__user=statement.user,
            submission__period_month=statement.period_month,
        )
        .select_related("service")
        .order_by("uploaded_at", "pk")
    )
    receipts_by_service: dict[int, list[Receipt]] = {}
    for receipt in receipts:
        if receipt.service_id is not None:
            receipts_by_service.setdefault(receipt.service_id, []).append(receipt)

    no_usage_service_ids = set(
        MonthlyServiceDeclaration.objects.filter(
            user=statement.user,
            period_month=statement.period_month,
            no_usage=True,
        ).values_list("service_id", flat=True)
    )

    with transaction.atomic():
        CardStatementItem.objects.filter(statement=statement).delete()
        items = []
        no_usage_conflict_service_ids: set[int] = set()
        for sequence, extracted in enumerate(result.items, start=1):
            service = service_by_id.get(extracted.registered_service_id)
            matched_receipt = None
            if service is not None and extracted.receipt_required:
                # 1つの領収書を同じ明細書内の複数行に使い回さない。
                # 同一サービスの領収書が複数ある場合は、日付・金額が近いものを優先する。
                matched_receipt = _take_best_unused_receipt(
                    receipts_by_service.get(service.pk, []),
                    extracted,
                )

            memo = extracted.reason
            if service is not None and service.pk in no_usage_service_ids and extracted.receipt_required:
                conflict = (
                    "ユーザーはこの月を『当月利用なし』と申告していましたが、カード明細に請求が見つかりました。"
                    "利用なし申告を取り消し、領収書または利用状況の再確認対象に戻しました。"
                )
                memo = f"{memo} {conflict}".strip()
                no_usage_conflict_service_ids.add(service.pk)

            match_status = extracted.match_status
            if service is None and match_status == StatementMatchStatus.MATCHED:
                match_status = StatementMatchStatus.AMBIGUOUS

            items.append(
                CardStatementItem(
                    statement=statement,
                    sequence=sequence,
                    line_reference=extracted.line_reference,
                    transaction_date=extracted.transaction_date,
                    merchant_name=extracted.merchant_name,
                    merchant_normalized=extracted.merchant_name,
                    amount_jpy=extracted.amount_jpy,
                    original_amount=extracted.original_amount,
                    original_currency=extracted.original_currency,
                    matched_service=service,
                    match_status=match_status,
                    match_confidence=extracted.confidence,
                    match_memo=memo,
                    receipt_required=extracted.receipt_required,
                    matched_receipt=matched_receipt,
                )
            )
        CardStatementItem.objects.bulk_create(items)

        if no_usage_conflict_service_ids:
            MonthlyServiceDeclaration.objects.filter(
                user=statement.user,
                period_month=statement.period_month,
                service_id__in=no_usage_conflict_service_ids,
            ).delete()

        statement.status = (
            CardStatementStatus.NEEDS_REVIEW
            if no_usage_conflict_service_ids and result.status == CardStatementStatus.COMPLETED
            else result.status
        )
        statement.card_last4 = result.card_last4
        statement.statement_period = result.statement_period
        statement.payment_date = result.payment_date
        conflict_memo = ""
        if no_usage_conflict_service_ids:
            conflict_names = [service_by_id[service_id].display_name for service_id in sorted(no_usage_conflict_service_ids)]
            conflict_memo = (
                "カード明細に請求が見つかったため、次の『当月利用なし』申告を取り消しました: "
                + "、".join(conflict_names)
            )
        statement.ai_admin_memo = " ".join(part for part in (result.admin_memo, conflict_memo) if part).strip()
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
