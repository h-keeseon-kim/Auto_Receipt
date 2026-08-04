from __future__ import annotations

import logging
import re
import threading
import unicodedata
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
    CardStatementStatus,
    MonthlyServiceDeclaration,
    Receipt,
    RegisteredService,
    ServiceCatalog,
    StatementMatchReason,
    StatementMatchStatus,
    receipt_month_for_statement,
)
from .statement_ai import generate_card_statement_analysis

logger = logging.getLogger(__name__)


CARD_STATEMENT_MONTH_SEMANTICS_RECONCILE_MARKER = (
    "【月次ルール更新】明細月と対象領収書月の対応を修正したため、最新の領収書と再照合します。"
)
CARD_STATEMENT_MATCHING_RULES_RECONCILE_MARKER = (
    "【照合ルール更新】必須条件・優先順位方式へ更新したため、最新の領収書と再照合します。"
)
CARD_LAST4_EVIDENCE_RECONCILE_MARKER = (
    "【照合ルール更新】領収書のカード末尾を必須条件から補助加点へ変更したため、最新の領収書と再照合します。"
)
EXACT_AMOUNT_MATCHING_RECONCILE_MARKER = (
    "【照合ルール更新】金額照合を許容差なしの完全一致へ変更したため、最新の領収書と再照合します。"
)
SIMPLE_RECEIPT_MATCHING_RECONCILE_MARKER = (
    "【照合ルール更新】利用日±1日・金額/通貨完全一致・ご利用先/払先関連の単純一対一照合へ変更したため、最新の領収書と再照合します。"
)

# v1.11.0: 誰の利用かを推定せず、提出済み領収書の有無だけを判定する。
# 明細行と領収書は次の3条件をすべて満たす場合だけ一致とする。
# 1) 金額・通貨の完全一致（許容差なし）
# 2) 明細利用日と領収書日付の差が±1日以内
# 3) 明細のご利用先と領収書の実際の払先が関連
DATE_MATCH_TOLERANCE_DAYS = 1

# 請求名義の識別には使わない一般語。AI/APIだけの一致などで別サービスを関連扱いしない。
GENERIC_IDENTITY_TOKENS = {
    "AI",
    "API",
    "BILL",
    "BILLING",
    "CARD",
    "CO",
    "COM",
    "CORP",
    "CORPORATION",
    "INC",
    "JAPAN",
    "LLC",
    "LTD",
    "ONLINE",
    "PAYMENT",
    "PBC",
    "SERVICE",
    "SERVICES",
    "SUBSCR",
    "SUBSCRIPTION",
    "THE",
    "USD",
}


@dataclass(frozen=True)
class AmountAssessment:
    matched: bool
    basis: str = ""
    memo: str = ""


@dataclass(frozen=True)
class ReceiptMatchEvaluation:
    receipt: Receipt
    date_distance_days: int
    amount_basis: str
    rationale: str

    @property
    def sort_key(self):
        # 複数の似た領収書がある場合は、最も日付が近いものを先にし、
        # それでも同じならアップロード日時・IDの順で決定する。
        return (
            self.date_distance_days,
            self.receipt.issued_on or date.max,
            self.receipt.uploaded_at,
            self.receipt.pk,
        )


def reconcile_pending_card_statement_month_semantics(*, period_month=None, statement_id=None) -> int:
    """ルール更新対象の既存明細を、保存済み行だけで一度だけ再照合する。"""

    queryset = CardStatement.objects.filter(
        Q(ai_admin_memo__contains=CARD_STATEMENT_MONTH_SEMANTICS_RECONCILE_MARKER)
        | Q(ai_admin_memo__contains=CARD_STATEMENT_MATCHING_RULES_RECONCILE_MARKER)
        | Q(ai_admin_memo__contains=CARD_LAST4_EVIDENCE_RECONCILE_MARKER)
        | Q(ai_admin_memo__contains=EXACT_AMOUNT_MATCHING_RECONCILE_MARKER)
        | Q(ai_admin_memo__contains=SIMPLE_RECEIPT_MATCHING_RECONCILE_MARKER)
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


def _identity_tokens(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", value or "").upper()
    return {
        token
        for token in re.findall(r"[A-Z0-9]{3,}", normalized)
        if token not in GENERIC_IDENTITY_TOKENS and len(token) >= 4
    }


def _text_related(first: str, second: str) -> bool:
    """明細のご利用先と領収書の払先を、一般語だけの一致を除いて比較する。"""

    left = _normalize_text(first)
    right = _normalize_text(second)
    if not left or not right:
        return False
    if left == right and len(left) >= 3:
        return True
    if min(len(left), len(right)) >= 5 and (left in right or right in left):
        return True
    left_tokens = _identity_tokens(first)
    right_tokens = _identity_tokens(second)
    return bool(left_tokens and right_tokens and left_tokens.intersection(right_tokens))


def _merchant_payee_related(
    merchant_name: str,
    payee: str,
    *,
    alias_groups: tuple[tuple[str, ...], ...] = (),
) -> bool:
    """請求名義と払先が直接または同一サービスマスターの別名群で関連するか。"""

    if _text_related(merchant_name, payee):
        return True
    for aliases in alias_groups:
        merchant_related = any(_text_related(merchant_name, alias) for alias in aliases)
        if not merchant_related:
            continue
        payee_related = any(_text_related(payee, alias) for alias in aliases)
        if payee_related:
            return True
    return False


def _catalog_alias_values(catalog: ServiceCatalog | None) -> list[str]:
    if catalog is None:
        return []
    raw_values = [catalog.name]
    raw_values.extend(re.split(r"[,;\n]+", catalog.merchant_aliases or ""))
    return [item.strip() for item in raw_values if item and item.strip()]


def _catalog_match_strength(value: str, catalog: ServiceCatalog) -> int:
    """後方互換用のサービス名義一致強度。明細の一次解析でのみ利用する。"""

    normalized_value = _normalize_text(value)
    if not normalized_value:
        return 0
    best = 0
    for alias in _catalog_alias_values(catalog):
        normalized_alias = _normalize_text(alias)
        if not normalized_alias:
            continue
        if normalized_value == normalized_alias:
            best = max(best, 4)
            continue
        if min(len(normalized_value), len(normalized_alias)) >= 8 and (
            normalized_value in normalized_alias or normalized_alias in normalized_value
        ):
            best = max(best, 3)
            continue
        if _text_related(value, alias):
            best = max(best, 2)
    return best


def _catalog_ids_for_text(value: str, catalogs: list[ServiceCatalog]) -> set[int]:
    """最も具体的な一致強度を持つサービス集合だけを返す。"""

    ranked = [(catalog.pk, _catalog_match_strength(value, catalog)) for catalog in catalogs]
    best = max((strength for _, strength in ranked), default=0)
    if best <= 0:
        return set()
    return {catalog_id for catalog_id, strength in ranked if strength == best}


def _statement_gate_errors(statement: CardStatement) -> list[str]:
    """アップロードされた全社明細書自体の検証。個別領収書のカード番号とは別。"""

    errors: list[str] = []
    target_last4 = str(getattr(settings, "RECEIPT_CARD_LAST4", "7210"))[-4:]
    if not statement.card_last4:
        errors.append("明細書のカード末尾を確認できません。")
    elif statement.card_last4 != target_last4:
        errors.append(f"明細書のカード末尾が{target_last4}ではなく{statement.card_last4}です。")
    expected_period = statement.period_month.strftime("%Y-%m")
    if not statement.statement_period:
        errors.append("明細書の対象月を確認できません。")
    elif statement.statement_period != expected_period:
        errors.append(
            f"AI判定明細月が選択月{expected_period}ではなく{statement.statement_period}です。"
        )
    return errors


def _amounts_equal(left: Decimal | None, right: Decimal | None) -> bool:
    """金額を許容差なしで比較する。22.00と22.000は同一、22.01は不一致。"""

    if left is None or right is None:
        return False
    return left == right


def _assess_amount(item: CardStatementItem, receipt: Receipt) -> AmountAssessment:
    """明細行と領収書の金額・通貨を完全一致だけで評価する。"""

    receipt_amount = receipt.amount
    receipt_currency = (receipt.currency or "").upper()
    statement_currency = (item.original_currency or "").upper()

    if receipt_amount is None or not receipt_currency:
        return AmountAssessment(False, memo="領収書の金額または通貨を確認できません。")

    comparison_amount: Decimal | None = None
    basis = ""
    if item.original_amount is not None and statement_currency:
        if receipt_currency == statement_currency:
            comparison_amount = item.original_amount
            basis = "original"
        elif receipt_currency == "JPY" and item.amount_jpy is not None:
            comparison_amount = item.amount_jpy
            basis = "jpy"
        else:
            return AmountAssessment(
                False,
                memo=(
                    f"通貨不一致（明細: {statement_currency or '不明'} / "
                    f"領収書: {receipt_currency or '不明'}）。"
                ),
            )
    elif item.amount_jpy is not None and receipt_currency == "JPY":
        comparison_amount = item.amount_jpy
        basis = "jpy"
    else:
        return AmountAssessment(False, memo="明細と領収書で比較可能な金額・通貨がありません。")

    if not _amounts_equal(receipt_amount, comparison_amount):
        return AmountAssessment(
            False,
            basis=basis,
            memo=(
                f"金額不一致（明細: {comparison_amount} {receipt_currency} / "
                f"領収書: {receipt_amount} {receipt_currency}）。許容差はありません。"
            ),
        )

    label = (
        f"外貨金額 {comparison_amount} {receipt_currency} 完全一致"
        if basis == "original"
        else f"円金額 {comparison_amount}円 完全一致"
    )
    return AmountAssessment(True, basis=basis, memo=label)


def _receipt_match_evaluation(
    item: CardStatementItem,
    receipt: Receipt,
    *,
    alias_groups: tuple[tuple[str, ...], ...] = (),
) -> ReceiptMatchEvaluation | None:
    """明細行に対する領収書の一致可否を、3つの必須条件だけで判定する。"""

    amount = _assess_amount(item, receipt)
    if not amount.matched:
        return None

    if not item.transaction_date or not receipt.issued_on:
        return None
    date_distance = abs((receipt.issued_on - item.transaction_date).days)
    if date_distance > DATE_MATCH_TOLERANCE_DAYS:
        return None

    payee = (receipt.ai_extracted_payee or "").strip()
    if not payee or not _merchant_payee_related(
        item.merchant_name,
        payee,
        alias_groups=alias_groups,
    ):
        return None

    return ReceiptMatchEvaluation(
        receipt=receipt,
        date_distance_days=date_distance,
        amount_basis=amount.basis,
        rationale=(
            f"金額・通貨完全一致、利用日と領収書日付の差{date_distance}日、"
            f"ご利用先「{item.merchant_name}」と払先「{payee}」が関連。"
        ),
    )


def _registered_services_for_period(statement_month: date) -> list[RegisteredService]:
    """管理画面・既存テスト用。Pカード利用中のサービスを返す。"""

    target_receipt_month = receipt_month_for_statement(statement_month)
    return list(
        RegisteredService.objects.filter(
            user__is_active=True,
            user__is_staff=False,
            user__is_superuser=False,
            uses_p_card=True,
        )
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
        .filter(Q(is_extra=True) | Q(p_card_usage_snapshot=True))
        .select_related("submission__user", "service", "service__catalog_service", "admin_reviewed_by")
        .order_by("issued_on", "uploaded_at", "pk")
    )


def _is_manual_override(item: CardStatementItem) -> bool:
    return item.match_confidence >= 1.0 and (item.match_memo or "").startswith("管理者")


def _base_match_memo(value: str) -> str:
    result = (value or "").strip()
    for marker in ("【領収書照合】", "【自動照合】", "【必須条件】", "【単純照合】"):
        result = result.split(marker, 1)[0].strip()
    return result


def _mark_unmatched(item: CardStatementItem, *, base_memo: str, reason: str) -> None:
    item.matched_receipt = None
    item.matched_user = None
    item.matched_service = None
    item.matched_catalog_service = None
    item.match_status = StatementMatchStatus.UNMATCHED
    item.match_reason_code = StatementMatchReason.NO_COMPATIBLE_RECEIPT
    item.match_confidence = 0.0
    item.match_memo = " ".join(part for part in (base_memo, f"【単純照合】{reason}") if part).strip()


def reconcile_card_statement_items(statement_id: int, *, preserve_manual: bool = True) -> CardStatement:
    """明細行と全ユーザー領収書を、単純な3条件で一対一照合する。

    利用者の特定や候補スコアリングは行わない。複数の領収書が同じ条件を
    満たす場合は、日付差・アップロード順で先頭のものを割り当てる。
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
        ).order_by("transaction_date", "sequence", "pk")
    )
    receipts = _available_receipts_for_statement_month(statement.period_month)
    alias_group_values: list[tuple[str, ...]] = []
    for catalog in ServiceCatalog.objects.all().only("name", "merchant_aliases"):
        aliases = tuple(_catalog_alias_values(catalog))
        if aliases:
            alias_group_values.append(aliases)
    alias_groups = tuple(alias_group_values)
    target_receipt_month = receipt_month_for_statement(statement.period_month)
    statement_errors = tuple(_statement_gate_errors(statement))

    used_receipt_ids: set[int] = set()
    manual_item_ids: set[int] = set()

    # 管理者が対象外にした行、または領収書を直接確定した旧データは維持する。
    for item in items:
        if not (preserve_manual and _is_manual_override(item)):
            continue
        if item.match_status == StatementMatchStatus.IGNORED or not item.receipt_required:
            manual_item_ids.add(item.pk)
            item.match_status = StatementMatchStatus.IGNORED
            item.match_reason_code = StatementMatchReason.IGNORED
            item.matched_receipt = None
            item.matched_user = None
            item.matched_service = None
            item.matched_catalog_service = None
            continue
        if item.matched_receipt_id and item.matched_receipt and item.matched_receipt.file_available:
            manual_item_ids.add(item.pk)
            used_receipt_ids.add(item.matched_receipt_id)
            item.match_status = StatementMatchStatus.MATCHED
            item.match_reason_code = StatementMatchReason.MANUAL_CONFIRMED

    for item in items:
        if item.pk in manual_item_ids:
            continue

        base_memo = _base_match_memo(item.match_memo)
        if not item.receipt_required:
            item.matched_receipt = None
            item.matched_user = None
            item.matched_service = None
            item.matched_catalog_service = None
            item.match_status = StatementMatchStatus.IGNORED
            item.match_reason_code = StatementMatchReason.IGNORED
            item.match_confidence = 1.0
            item.match_memo = base_memo or "領収書管理対象外です。"
            continue

        if statement_errors:
            _mark_unmatched(
                item,
                base_memo=base_memo,
                reason="明細書自体の確認に問題があります。" + " ".join(statement_errors),
            )
            continue

        evaluations = [
            evaluation
            for receipt in receipts
            if receipt.pk not in used_receipt_ids
            if (
                evaluation := _receipt_match_evaluation(
                    item,
                    receipt,
                    alias_groups=alias_groups,
                )
            ) is not None
        ]
        evaluations.sort(key=lambda evaluation: evaluation.sort_key)

        if not evaluations:
            _mark_unmatched(
                item,
                base_memo=base_memo,
                reason=(
                    f"提出済み領収書の中に、①金額・通貨の完全一致、"
                    f"②利用日±{DATE_MATCH_TOLERANCE_DAYS}日以内、"
                    "③ご利用先と払先の関連、の3条件をすべて満たす未使用領収書がありません。"
                ),
            )
            continue

        chosen = evaluations[0]
        receipt = chosen.receipt
        used_receipt_ids.add(receipt.pk)
        item.matched_receipt = receipt
        item.matched_user = receipt.submission.user
        item.matched_service = receipt.service
        item.matched_catalog_service = (
            receipt.service.catalog_service if receipt.service_id and receipt.service.catalog_service_id else None
        )
        item.match_status = StatementMatchStatus.MATCHED
        item.match_reason_code = StatementMatchReason.AUTO_STRONG
        item.match_confidence = 1.0
        item.match_memo = " ".join(
            part
            for part in (
                base_memo,
                f"【単純照合】領収書「{receipt.display_filename}」を提出済みとして割り当てました。{chosen.rationale}",
            )
            if part
        ).strip()

    no_usage_conflicts: list[str] = []
    missing_count = 0
    for item in items:
        if item.receipt_required and item.matched_service_id and item.matched_receipt_id:
            deleted, _ = MonthlyServiceDeclaration.objects.filter(
                user=item.matched_service.user,
                service=item.matched_service,
                period_month=statement.period_month,
                no_usage=True,
            ).delete()
            if deleted:
                conflict = (
                    f"{item.matched_service.user.username} の {item.matched_service.display_name} は"
                    "「対象領収書月は利用なし」申告でしたが、対応する領収書が見つかったため申告を取り消しました。"
                )
                no_usage_conflicts.append(conflict)
                if conflict not in (item.match_memo or ""):
                    item.match_memo = f"{item.match_memo} {conflict}".strip()

        if item.receipt_required and not (
            item.matched_receipt_id and item.matched_receipt and item.matched_receipt.file_available
        ):
            missing_count += 1

    with transaction.atomic():
        for item in items:
            item.save(
                update_fields=[
                    "matched_user",
                    "matched_catalog_service",
                    "matched_service",
                    "matched_receipt",
                    "match_status",
                    "match_reason_code",
                    "match_confidence",
                    "match_memo",
                    "receipt_required",
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
                if card_or_period_problem or not items or missing_count
                else CardStatementStatus.COMPLETED
            )

        extraction_memo = (statement.ai_admin_memo or "").split("【照合結果】", 1)[0]
        for marker in (
            CARD_STATEMENT_MONTH_SEMANTICS_RECONCILE_MARKER,
            CARD_STATEMENT_MATCHING_RULES_RECONCILE_MARKER,
            CARD_LAST4_EVIDENCE_RECONCILE_MARKER,
            EXACT_AMOUNT_MATCHING_RECONCILE_MARKER,
            SIMPLE_RECEIPT_MATCHING_RECONCILE_MARKER,
        ):
            extraction_memo = extraction_memo.replace(marker, "")
        extraction_memo = extraction_memo.strip()

        matched_count = sum(
            1
            for item in items
            if item.receipt_required
            and item.matched_receipt_id
            and item.matched_receipt
            and item.matched_receipt.file_available
        )
        reconciliation_memo = (
            f"【照合結果】明細月 {statement.period_month:%Y-%m} "
            f"（対象領収書月 {target_receipt_month:%Y-%m}）に提出された領収書{len(receipts)}件を、"
            f"金額・通貨完全一致、利用日±{DATE_MATCH_TOLERANCE_DAYS}日以内、"
            "ご利用先・払先関連の3条件で一対一照合しました。"
            f"提出確認済み{matched_count}件、未一致{missing_count}件です。"
            "同条件の領収書が複数ある場合は日付差・アップロード順で割り当てています。"
        )
        if no_usage_conflicts:
            reconciliation_memo += " " + " ".join(dict.fromkeys(no_usage_conflicts))
        statement.ai_admin_memo = " ".join(
            part for part in (extraction_memo, reconciliation_memo) if part
        )[:5000]
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
        ServiceCatalog.objects.filter(
            registered_services__user__is_staff=False,
            registered_services__user__is_superuser=False,
            registered_services__uses_p_card=True,
        )
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
                statement.ai_admin_memo = (
                    f"カード明細解析中に予期しないエラーが発生しました: "
                    f"{exc.__class__.__name__}: {exc}"
                )
                statement.processed_at = timezone.now()
                statement.save(update_fields=["status", "ai_admin_memo", "processed_at", "updated_at"])
            except Exception:
                logger.exception("Card statement %s could not be marked failed", statement_id)
        finally:
            close_old_connections()

    thread = threading.Thread(target=worker, name=f"card-statement-{statement_id}", daemon=True)
    thread.start()
    return thread
