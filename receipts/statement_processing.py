from __future__ import annotations

import logging
import re
import threading
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import close_old_connections, transaction
from django.db.models import Q
from django.utils import timezone

from .ai_filename import extract_embedded_pdf_text, extract_receipt_text_fallback
from .models import (
    CardStatement,
    CardStatementItem,
    CardStatementReceiptEvidence,
    CardStatementStatus,
    MonthlyServiceDeclaration,
    Receipt,
    ReceiptAdminReviewStatus,
    ReceiptFilenameStatus,
    ReceiptFinancialDocumentKind,
    RegisteredService,
    ServiceCatalog,
    StatementMatchReason,
    StatementMatchStatus,
    StatementReceiptEvidenceRole,
    receipt_month_for_statement,
)
from .statement_ai import generate_card_statement_analysis
from .statement_matching import (
    AmountOption,
    EvidenceComponent,
    MATCH_DIRECT,
    MATCH_LINKED_REFUND_NET,
    MATCH_MERCHANT_REFUND_NET,
    MATCH_ORIGINAL_CHARGE,
    ROLE_CHARGE,
    ROLE_REFUND,
    StatementLine,
    format_evidence_calculation,
    reconcile_statement,
)

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
EMPIRICAL_MATCHING_RECONCILE_MARKER = (
    "【照合ルール更新】2026年7月実明細61行・提出PDF63件の全件検証に基づき、取引構成要素・重複排除・返金純額照合へ更新したため再照合します。"
)

# 実データでは通常一致56件がすべて同日または1日差だった。
DATE_MATCH_TOLERANCE_DAYS = 1

GENERIC_IDENTITY_TOKENS = {
    "AI", "API", "BILL", "BILLING", "CARD", "CO", "COM", "CORP",
    "CORPORATION", "INC", "JAPAN", "LLC", "LTD", "ONLINE", "PAYMENT",
    "PBC", "SERVICE", "SERVICES", "SUBSCR", "SUBSCRIPTION", "THE", "USD",
}

# 実明細と実領収書で確認した企業・サービス表記。順序は具体的表記を優先する。
KNOWN_MERCHANT_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("GOOGLE_CLOUD", ("GOOGLECLOUD", "GCLOUD", "GOOGLECLOUDPLATFORM")),
    ("GOOGLE_ONE", ("GOOGLEGOOGLEON", "GOOGLEONE")),
    ("ANTHROPIC", ("ANTHROPIC", "CLAUDE")),
    ("OPENAI", ("OPENAI", "CHATGPT")),
    ("AUDIOSHAKE", ("AUDIOSHAKE",)),
    ("JETBRAINS", ("JETBRAINS",)),
    ("GITHUB", ("GITHUB",)),
    ("RAILWAY", ("RAILWAY",)),
    ("CURSOR", ("CURSOR", "ANYSPHERE")),
    ("DIFY", ("DIFY", "LANGGENIUS")),
    ("SUNO", ("SUNO",)),
    ("GROK", ("GROK", "XAI")),
    ("FIGMA", ("FIGMA",)),
)


def reconcile_pending_card_statement_month_semantics(*, period_month=None, statement_id=None) -> int:
    queryset = CardStatement.objects.filter(
        Q(ai_admin_memo__contains=CARD_STATEMENT_MONTH_SEMANTICS_RECONCILE_MARKER)
        | Q(ai_admin_memo__contains=CARD_STATEMENT_MATCHING_RULES_RECONCILE_MARKER)
        | Q(ai_admin_memo__contains=CARD_LAST4_EVIDENCE_RECONCILE_MARKER)
        | Q(ai_admin_memo__contains=EXACT_AMOUNT_MATCHING_RECONCILE_MARKER)
        | Q(ai_admin_memo__contains=SIMPLE_RECEIPT_MATCHING_RECONCILE_MARKER)
        | Q(ai_admin_memo__contains=EMPIRICAL_MATCHING_RECONCILE_MARKER)
    ).exclude(status__in=[CardStatementStatus.PROCESSING, CardStatementStatus.FAILED])
    if period_month is not None:
        queryset = queryset.filter(period_month=period_month)
    if statement_id is not None:
        queryset = queryset.filter(pk=statement_id)
    ids = list(queryset.order_by("pk").values_list("pk", flat=True))
    for pk in ids:
        reconcile_card_statement_items(pk)
    return len(ids)


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").upper()
    return "".join(char for char in normalized if char.isalnum())


def _identity_tokens(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", value or "").upper()
    return {
        token for token in re.findall(r"[A-Z0-9]{3,}", normalized)
        if token not in GENERIC_IDENTITY_TOKENS and len(token) >= 4
    }


def _text_related(first: str, second: str) -> bool:
    left = _normalize_text(first)
    right = _normalize_text(second)
    if not left or not right:
        return False
    if left == right and len(left) >= 3:
        return True
    if min(len(left), len(right)) >= 5 and (left in right or right in left):
        return True
    return bool(_identity_tokens(first).intersection(_identity_tokens(second)))


def _catalog_alias_values(catalog: ServiceCatalog | None) -> list[str]:
    if catalog is None:
        return []
    values = [catalog.name]
    values.extend(re.split(r"[,;\n]+", catalog.merchant_aliases or ""))
    return [value.strip() for value in values if value and value.strip()]


def _known_merchant_key(value: str) -> str:
    normalized = _normalize_text(value)
    if not normalized:
        return ""
    for key, patterns in KNOWN_MERCHANT_PATTERNS:
        if any(pattern in normalized for pattern in patterns):
            return key
    return ""


def _catalog_match_strength(value: str, catalog: ServiceCatalog) -> int:
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
        elif min(len(normalized_value), len(normalized_alias)) >= 8 and (
            normalized_value in normalized_alias or normalized_alias in normalized_value
        ):
            best = max(best, 3)
        elif _text_related(value, alias):
            best = max(best, 2)
    return best


def _catalog_ids_for_text(value: str, catalogs: list[ServiceCatalog]) -> set[int]:
    ranked = [(catalog.pk, _catalog_match_strength(value, catalog)) for catalog in catalogs]
    best = max((strength for _, strength in ranked), default=0)
    if best <= 0:
        return set()
    return {catalog_id for catalog_id, strength in ranked if strength == best}


def _canonical_merchant_key(value: str, catalogs: list[ServiceCatalog]) -> str:
    known = _known_merchant_key(value)
    if known:
        return known

    ranked: list[tuple[int, ServiceCatalog]] = [
        (_catalog_match_strength(value, catalog), catalog) for catalog in catalogs
    ]
    best = max((strength for strength, _catalog in ranked), default=0)
    if best > 0:
        candidates = [catalog for strength, catalog in ranked if strength == best]
        known_keys = {_known_merchant_key(" ".join(_catalog_alias_values(catalog))) for catalog in candidates}
        known_keys.discard("")
        if len(known_keys) == 1:
            return next(iter(known_keys))
        return "CATALOG:" + ":".join(str(catalog.pk) for catalog in sorted(candidates, key=lambda c: c.pk))

    tokens = sorted(_identity_tokens(value), key=lambda token: (-len(token), token))
    return f"TEXT:{tokens[0]}" if tokens else f"TEXT:{_normalize_text(value)}"


def _statement_gate_errors(statement: CardStatement) -> list[str]:
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
        errors.append(f"AI判定明細月が選択月{expected_period}ではなく{statement.statement_period}です。")
    return errors


def _registered_services_for_period(statement_month: date) -> list[RegisteredService]:
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


def _parse_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _enrich_receipt_financial_metadata(receipt: Receipt) -> None:
    """既存PDFの埋め込みテキストから取引構成要素を補完する。

    ファイル名は使わない。画像PDFではOpenAI保存値を維持し、推測で補完しない。
    """

    if receipt.financial_metadata_checked_at is not None:
        return

    update_fields = ["financial_metadata_checked_at", "updated_at"]
    receipt.financial_metadata_checked_at = timezone.now()
    fallback = None
    if receipt.file_available:
        try:
            with receipt.file.open("rb") as file_obj:
                file_bytes = file_obj.read()
            text = extract_embedded_pdf_text(
                file_bytes=file_bytes,
                original_filename=receipt.original_filename or Path(receipt.file.name).name,
                content_type=receipt.content_type,
            )
            fallback = extract_receipt_text_fallback(text)
        except Exception:
            logger.exception("Could not enrich receipt %s financial metadata", receipt.pk)

    if fallback:
        if fallback.financial_document_kind != ReceiptFinancialDocumentKind.UNKNOWN:
            receipt.financial_document_kind = fallback.financial_document_kind
            update_fields.append("financial_document_kind")
        if fallback.transaction_reference:
            receipt.financial_transaction_reference = fallback.transaction_reference[:160]
            update_fields.append("financial_transaction_reference")
        if fallback.related_transaction_reference:
            receipt.financial_related_reference = fallback.related_transaction_reference[:160]
            update_fields.append("financial_related_reference")
        if fallback.transaction_components:
            receipt.financial_transaction_components = [dict(component) for component in fallback.transaction_components]
            update_fields.append("financial_transaction_components")
        if fallback.payee and not receipt.ai_extracted_payee:
            receipt.ai_extracted_payee = fallback.payee[:160]
            update_fields.append("ai_extracted_payee")
        if fallback.payment_date and (receipt.issued_on is None or fallback.financial_document_kind == ReceiptFinancialDocumentKind.REFUND):
            receipt.issued_on = fallback.payment_date
            update_fields.append("issued_on")
        if fallback.amount is not None and (receipt.amount is None or fallback.financial_document_kind == ReceiptFinancialDocumentKind.REFUND):
            receipt.amount = fallback.amount
            update_fields.append("amount")
        if fallback.currency and (not receipt.currency or fallback.financial_document_kind == ReceiptFinancialDocumentKind.REFUND):
            receipt.currency = fallback.currency
            update_fields.append("currency")

    # 旧データでも通常領収書の主要値が揃っていれば、1件の構成要素を作る。
    if not receipt.financial_transaction_components and receipt.amount is not None and receipt.issued_on and receipt.currency:
        role = ROLE_REFUND if receipt.financial_document_kind == ReceiptFinancialDocumentKind.REFUND else ROLE_CHARGE
        signed_amount = -abs(receipt.amount) if role == ROLE_REFUND else abs(receipt.amount)
        receipt.financial_transaction_components = [
            {
                "component_key": "primary",
                "role": role,
                "signed_amount": str(signed_amount),
                "currency": receipt.currency.upper(),
                "transaction_date": receipt.issued_on.isoformat(),
                "payee": receipt.ai_extracted_payee,
                "invoice_number": receipt.financial_transaction_reference,
                "transaction_id": receipt.financial_transaction_reference,
                "related_transaction_id": receipt.financial_related_reference,
                "source_label": "主要取引",
                "confidence": 0.7,
                "document_kind": receipt.financial_document_kind,
            }
        ]
        update_fields.append("financial_transaction_components")

    receipt.save(update_fields=list(dict.fromkeys(update_fields)))


def _receipt_components(
    receipts: list[Receipt],
    catalogs: list[ServiceCatalog],
) -> tuple[list[EvidenceComponent], list[Receipt]]:
    components: list[EvidenceComponent] = []
    unresolved: list[Receipt] = []

    for receipt_order, receipt in enumerate(receipts):
        _enrich_receipt_financial_metadata(receipt)
        raw_components = receipt.financial_transaction_components or []
        valid_count = 0
        for index, raw in enumerate(raw_components):
            if not isinstance(raw, dict):
                continue
            role = str(raw.get("role") or ROLE_CHARGE).lower()
            if role not in {ROLE_CHARGE, ROLE_REFUND}:
                continue
            amount = _parse_decimal(raw.get("signed_amount"))
            currency = str(raw.get("currency") or "").upper()[:3]
            event_date = _parse_date(raw.get("transaction_date"))
            payee = str(raw.get("payee") or receipt.ai_extracted_payee or "").strip()
            amount = -abs(amount) if role == ROLE_REFUND and amount is not None else (abs(amount) if amount is not None else None)
            document_kind = str(raw.get("document_kind") or receipt.financial_document_kind or "unknown").lower()

            # 自動照合の請求元はPDF本文から抽出した払先だけを使用する。
            # ユーザーが選んだ登録サービスや「その他」メモを払先の代用品にすると、
            # 内容を読めていない誤ファイルまで一致扱いになるため使用しない。
            # Invoiceも、本文から取引日・金額・通貨・請求元を確認できる場合は
            # 「提出PDFあり」の証拠として扱う。支払済み書類が同一Invoice番号で
            # 重複していれば、重複排除時に支払済み書類を優先する。
            if (
                amount is None
                or not currency
                or event_date is None
                or not payee
            ):
                continue
            merchant_key = _canonical_merchant_key(payee, catalogs)
            if not merchant_key:
                continue
            evidence_payee = payee
            raw_key = str(raw.get("component_key") or f"component-{index + 1}")
            component_key = f"receipt-{receipt.pk}:{raw_key}"[:240]
            components.append(
                EvidenceComponent(
                    key=component_key,
                    receipt_id=receipt.pk,
                    receipt_order=receipt_order,
                    filename=receipt.display_filename,
                    merchant_key=merchant_key,
                    signed_amount=amount,
                    currency=currency,
                    event_date=event_date,
                    role=role,
                    document_kind=document_kind,
                    invoice_number=str(raw.get("invoice_number") or "")[:160],
                    transaction_id=str(raw.get("transaction_id") or "")[:160],
                    related_transaction_id=str(raw.get("related_transaction_id") or "")[:160],
                    source_label=str(raw.get("source_label") or "")[:120],
                    payee=evidence_payee[:160],
                )
            )
            valid_count += 1
        if valid_count == 0:
            unresolved.append(receipt)
    return components, unresolved


def _statement_line(item: CardStatementItem, catalogs: list[ServiceCatalog]) -> StatementLine:
    options: list[AmountOption] = []
    if item.original_amount is not None and item.original_currency:
        options.append(AmountOption(item.original_amount, item.original_currency, "外貨金額"))
    if item.amount_jpy is not None and not any(option.currency == "JPY" and option.amount == item.amount_jpy for option in options):
        options.append(AmountOption(item.amount_jpy, "JPY", "円請求額"))
    return StatementLine(
        key=str(item.pk),
        sequence=item.sequence,
        transaction_date=item.transaction_date,
        merchant_key=_canonical_merchant_key(item.merchant_name, catalogs),
        amount_options=tuple(options),
    )


def _is_manual_override(item: CardStatementItem) -> bool:
    return item.match_confidence >= 1.0 and (item.match_memo or "").startswith("管理者")


def _base_match_memo(value: str) -> str:
    result = (value or "").strip()
    for marker in ("【領収書照合】", "【自動照合】", "【必須条件】", "【単純照合】", "【実データ照合】", "【全件検証ルール】"):
        result = result.split(marker, 1)[0].strip()
    return result


def _clear_item_match(item: CardStatementItem) -> None:
    item.matched_receipt = None
    item.matched_user = None
    item.matched_service = None
    item.matched_catalog_service = None


def _apply_primary_receipt(item: CardStatementItem, receipt: Receipt | None) -> None:
    item.matched_receipt = receipt
    if receipt is None:
        item.matched_user = None
        item.matched_service = None
        item.matched_catalog_service = None
        return
    item.matched_user = receipt.submission.user
    item.matched_service = receipt.service
    item.matched_catalog_service = (
        receipt.service.catalog_service if receipt.service_id and receipt.service.catalog_service_id else None
    )


def _mark_unmatched(item: CardStatementItem, *, base_memo: str, reason: str) -> None:
    _clear_item_match(item)
    item.match_status = StatementMatchStatus.UNMATCHED
    item.match_reason_code = StatementMatchReason.NO_COMPATIBLE_RECEIPT
    item.match_confidence = 0.0
    item.match_memo = " ".join(part for part in (base_memo, f"【全件検証ルール】{reason}") if part).strip()


def _mark_review(item: CardStatementItem, receipt: Receipt | None, *, base_memo: str, reason: str) -> None:
    _apply_primary_receipt(item, receipt)
    item.match_status = StatementMatchStatus.NEEDS_REVIEW
    item.match_reason_code = StatementMatchReason.PARSE_REVIEW
    item.match_confidence = 0.0
    item.match_memo = " ".join(part for part in (base_memo, f"【全件検証ルール】{reason}") if part).strip()


def _reason_for_match_type(match_type: str) -> str:
    return {
        MATCH_DIRECT: StatementMatchReason.AUTO_STRONG,
        MATCH_ORIGINAL_CHARGE: StatementMatchReason.ORIGINAL_CHARGE,
        MATCH_LINKED_REFUND_NET: StatementMatchReason.LINKED_REFUND_NET,
        MATCH_MERCHANT_REFUND_NET: StatementMatchReason.MERCHANT_REFUND_NET,
    }.get(match_type, StatementMatchReason.AUTO_STRONG)


def _match_type_label(match_type: str) -> str:
    return {
        MATCH_DIRECT: "直接一致",
        MATCH_ORIGINAL_CHARGE: "返金書内の元決済確認",
        MATCH_LINKED_REFUND_NET: "紐付返金相殺",
        MATCH_MERCHANT_REFUND_NET: "同一請求元内の近接返金相殺",
    }.get(match_type, "一致")


def _target_amount_option(line: StatementLine, components: list[EvidenceComponent]) -> AmountOption | None:
    total = sum((component.signed_amount for component in components), Decimal("0"))
    currency = components[0].currency if components else ""
    return next((option for option in line.amount_options if option.currency == currency and option.amount == total), None)


def _create_evidence_records(
    item: CardStatementItem,
    components: list[EvidenceComponent],
    receipt_by_id: dict[int, Receipt],
) -> list[CardStatementReceiptEvidence]:
    records: list[CardStatementReceiptEvidence] = []
    for sequence, component in enumerate(components, start=1):
        receipt = receipt_by_id.get(component.receipt_id)
        records.append(
            CardStatementReceiptEvidence(
                statement_item=item,
                receipt=receipt,
                component_key=component.key[:160],
                role=(
                    StatementReceiptEvidenceRole.REFUND
                    if component.role == ROLE_REFUND
                    else StatementReceiptEvidenceRole.CHARGE
                ),
                sequence=sequence,
                signed_amount=component.signed_amount,
                currency=component.currency,
                event_date=component.event_date,
                document_kind_snapshot=component.document_kind[:20],
                filename_snapshot=component.filename[:255],
                payee_snapshot=component.payee[:160],
                invoice_number_snapshot=component.invoice_number[:160],
                transaction_reference_snapshot=component.transaction_id[:160],
                related_transaction_reference_snapshot=component.related_transaction_id[:160],
                source_label=component.source_label[:120],
            )
        )
    return records


def _unresolved_receipt_key(receipt: Receipt, catalogs: list[ServiceCatalog]) -> str:
    context = receipt.ai_extracted_payee
    if not context and receipt.service_id:
        context = receipt.service.display_name
        if receipt.service.catalog_service_id:
            context += " " + " ".join(_catalog_alias_values(receipt.service.catalog_service))
    if not context and receipt.memo:
        context = receipt.memo
    return _canonical_merchant_key(context, catalogs)


def _shortage_note(
    item: CardStatementItem,
    items: list[CardStatementItem],
    components: list[EvidenceComponent],
    catalogs: list[ServiceCatalog],
) -> str:
    line = _statement_line(item, catalogs)
    if not line.amount_options:
        return "対応する提出書類を確認できません。"
    option = line.amount_options[0]
    same_lines = sum(
        1
        for candidate in items
        if candidate.receipt_required
        and _statement_line(candidate, catalogs).merchant_key == line.merchant_key
        and any(value.currency == option.currency and value.amount == option.amount for value in _statement_line(candidate, catalogs).amount_options)
    )
    support = sum(
        1
        for component in components
        if component.role == ROLE_CHARGE
        and component.merchant_key == line.merchant_key
        and component.currency == option.currency
        and component.signed_amount == option.amount
    )
    if same_lines > 1 and support < same_lines:
        return (
            f"同一請求元・金額・通貨の明細{same_lines}件に対して、明示参照番号で重複除外した"
            f"決済証憑は{support}件です。少なくとも{same_lines - support}件不足しています。"
        )
    return "対応する提出書類を確認できません。"


def reconcile_card_statement_items(statement_id: int, *, preserve_manual: bool = True) -> CardStatement:
    """全ユーザーの提出PDFを、実データ検証済みルールで明細へ照合する。"""

    statement = CardStatement.objects.get(pk=statement_id)
    if statement.status == CardStatementStatus.PROCESSING:
        return statement

    items = list(
        statement.items.select_related(
            "matched_user", "matched_catalog_service", "matched_service", "matched_receipt",
            "matched_receipt__submission__user", "matched_receipt__service__catalog_service",
        ).order_by("sequence", "id")
    )
    receipts = _available_receipts_for_statement_month(statement.period_month)
    receipt_by_id = {receipt.pk: receipt for receipt in receipts}
    catalogs = list(ServiceCatalog.objects.all().order_by("pk"))
    components, unresolved_receipts = _receipt_components(receipts, catalogs)
    statement_errors = _statement_gate_errors(statement)

    manual_item_ids = {item.pk for item in items if preserve_manual and _is_manual_override(item)}
    auto_items: list[CardStatementItem] = []
    auto_lines: list[StatementLine] = []
    for item in items:
        base_memo = _base_match_memo(item.match_memo)
        if item.pk in manual_item_ids:
            continue
        if not item.receipt_required:
            _clear_item_match(item)
            item.match_status = StatementMatchStatus.IGNORED
            item.match_reason_code = StatementMatchReason.IGNORED
            item.match_confidence = 1.0
            item.match_memo = base_memo or "領収書管理対象外です。"
            continue
        if statement_errors:
            _mark_review(item, None, base_memo=base_memo, reason="明細書自体の確認に問題があります。" + " ".join(statement_errors))
            continue
        line = _statement_line(item, catalogs)
        missing_line_fields: list[str] = []
        if not line.transaction_date:
            missing_line_fields.append("利用日")
        if not line.merchant_key:
            missing_line_fields.append("ご利用先")
        if not line.amount_options:
            missing_line_fields.append("金額・通貨")
        if missing_line_fields:
            _mark_review(
                item,
                None,
                base_memo=base_memo,
                reason=(
                    "ご利用代金明細から "
                    + "・".join(missing_line_fields)
                    + " を抽出できないため、未提出とは断定できません。明細解析結果を確認してください。"
                ),
            )
            continue
        auto_items.append(item)
        auto_lines.append(line)

    reconciliation = reconcile_statement(
        auto_lines,
        components,
        date_tolerance_days=DATE_MATCH_TOLERANCE_DAYS,
    )
    component_by_key = reconciliation.components_by_key
    item_by_key = {str(item.pk): item for item in auto_items}
    line_by_key = {line.key: line for line in auto_lines}
    evidence_components_by_item: dict[int, list[EvidenceComponent]] = {}

    for line_key, assignment in reconciliation.assignments.items():
        item = item_by_key[line_key]
        matched_components = [component_by_key[key] for key in assignment.component_keys]
        primary_component = next((component for component in matched_components if component.role == ROLE_CHARGE), matched_components[0])
        primary_receipt = receipt_by_id.get(primary_component.receipt_id)
        _apply_primary_receipt(item, primary_receipt)
        item.match_status = StatementMatchStatus.MATCHED
        item.match_reason_code = _reason_for_match_type(assignment.match_type)
        item.match_confidence = 1.0
        target = _target_amount_option(line_by_key[line_key], matched_components)
        calculation = format_evidence_calculation(matched_components, target)
        files = "、".join(dict.fromkeys(component.filename for component in matched_components))
        item.match_memo = " ".join(
            part
            for part in (
                _base_match_memo(item.match_memo),
                f"【全件検証ルール】{_match_type_label(assignment.match_type)}。{assignment.memo}",
                f"証拠: {files}。計算: {calculation}" if calculation else f"証拠: {files}",
            )
            if part
        ).strip()
        evidence_components_by_item[item.pk] = matched_components

    unresolved_pool = list(unresolved_receipts)
    for item in auto_items:
        if str(item.pk) in reconciliation.assignments:
            continue
        line = line_by_key[str(item.pk)]
        related_index = next(
            (
                index
                for index, receipt in enumerate(unresolved_pool)
                if _unresolved_receipt_key(receipt, catalogs) == line.merchant_key
            ),
            None,
        )
        if related_index is not None:
            receipt = unresolved_pool.pop(related_index)
            _mark_review(
                item,
                receipt,
                base_memo=_base_match_memo(item.match_memo),
                reason=(
                    f"関連する提出ファイル「{receipt.display_filename}」はありますが、明細照合に必要な"
                    "取引日・金額・通貨・払先の構成要素を抽出できません。AI再検査または管理者確認が必要です。"
                ),
            )
        else:
            _mark_unmatched(
                item,
                base_memo=_base_match_memo(item.match_memo),
                reason=_shortage_note(item, items, list(component_by_key.values()), catalogs),
            )

    no_usage_conflicts: list[str] = []
    for item in items:
        if item.match_status == StatementMatchStatus.MATCHED and item.matched_service_id:
            deleted, _ = MonthlyServiceDeclaration.objects.filter(
                user=item.matched_service.user,
                service=item.matched_service,
                period_month=statement.period_month,
                no_usage=True,
            ).delete()
            if deleted:
                text = (
                    f"{item.matched_service.user.username} の {item.matched_service.display_name} は"
                    "「対象領収書月は利用なし」申告でしたが、証拠が見つかったため申告を取り消しました。"
                )
                no_usage_conflicts.append(text)
                item.match_memo = f"{item.match_memo} {text}".strip()

    with transaction.atomic():
        CardStatementReceiptEvidence.objects.filter(statement_item__statement=statement).exclude(
            statement_item_id__in=manual_item_ids
        ).delete()
        for item in items:
            item.save(
                update_fields=[
                    "matched_user", "matched_catalog_service", "matched_service", "matched_receipt",
                    "match_status", "match_reason_code", "match_confidence", "match_memo", "receipt_required",
                ]
            )
        evidence_records: list[CardStatementReceiptEvidence] = []
        for item in items:
            if item.pk in manual_item_ids and item.evidence_count:
                continue
            matched_components = evidence_components_by_item.get(item.pk)
            if matched_components:
                evidence_records.extend(_create_evidence_records(item, matched_components, receipt_by_id))
        if evidence_records:
            CardStatementReceiptEvidence.objects.bulk_create(evidence_records)

        missing_count = sum(1 for item in items if item.receipt_required and item.match_status == StatementMatchStatus.UNMATCHED)
        review_count = sum(1 for item in items if item.receipt_required and item.match_status == StatementMatchStatus.NEEDS_REVIEW)
        direct_count = sum(1 for item in items if item.match_reason_code == StatementMatchReason.AUTO_STRONG)
        original_count = sum(1 for item in items if item.match_reason_code == StatementMatchReason.ORIGINAL_CHARGE)
        linked_count = sum(1 for item in items if item.match_reason_code == StatementMatchReason.LINKED_REFUND_NET)
        merchant_net_count = sum(1 for item in items if item.match_reason_code == StatementMatchReason.MERCHANT_REFUND_NET)

        card_or_period_problem = bool(statement_errors)
        if statement.status != CardStatementStatus.FAILED:
            statement.status = (
                CardStatementStatus.NEEDS_REVIEW
                if card_or_period_problem or not items or missing_count or review_count
                else CardStatementStatus.COMPLETED
            )

        extraction_memo = (statement.ai_admin_memo or "").split("【照合結果】", 1)[0]
        for marker in (
            CARD_STATEMENT_MONTH_SEMANTICS_RECONCILE_MARKER,
            CARD_STATEMENT_MATCHING_RULES_RECONCILE_MARKER,
            CARD_LAST4_EVIDENCE_RECONCILE_MARKER,
            EXACT_AMOUNT_MATCHING_RECONCILE_MARKER,
            SIMPLE_RECEIPT_MATCHING_RECONCILE_MARKER,
            EMPIRICAL_MATCHING_RECONCILE_MARKER,
        ):
            extraction_memo = extraction_memo.replace(marker, "")
        extraction_memo = extraction_memo.strip()
        target_receipt_month = receipt_month_for_statement(statement.period_month)
        reconciliation_memo = (
            f"【照合結果】明細月{statement.period_month:%Y-%m}（対象領収書月{target_receipt_month:%Y-%m}）の"
            f"提出PDF{len(receipts)}件を、ファイル名や利用者ではなくPDF本文の取引構成要素で照合しました。"
            f"直接一致{direct_count}件、返金書内の元決済{original_count}件、紐付返金相殺{linked_count}件、"
            f"同一請求元内の近接返金相殺{merchant_net_count}件、解析要確認{review_count}件、未一致{missing_count}件です。"
            f"通常取引は金額・通貨完全一致、請求元一致、利用日±{DATE_MATCH_TOLERANCE_DAYS}日を全体最適化で一対一割当し、"
            "明示Invoice/Transaction IDが同じ重複書類は1取引として扱っています。利用者特定は条件に含めません。"
        )
        if reconciliation.deduplicated_component_keys:
            reconciliation_memo += f" 重複証拠{len(reconciliation.deduplicated_component_keys)}件を二重計上から除外しました。"
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
        CardStatementItem.objects.bulk_create(
            [
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
        )
        statement.status = result.status
        statement.card_last4 = result.card_last4
        statement.statement_period = result.statement_period
        statement.payment_date = result.payment_date
        statement.ai_admin_memo = result.admin_memo
        statement.processed_at = timezone.now()
        statement.save(
            update_fields=[
                "status", "card_last4", "statement_period", "payment_date",
                "ai_admin_memo", "processed_at", "updated_at",
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
        except Exception as exc:  # pragma: no cover
            logger.exception("Card statement %s processing failed", statement_id)
            try:
                statement = CardStatement.objects.get(pk=statement_id)
                statement.status = CardStatementStatus.FAILED
                statement.ai_admin_memo = (
                    f"カード明細解析中に予期しないエラーが発生しました: {exc.__class__.__name__}: {exc}"
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
