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
from django.contrib.auth import get_user_model
from django.db import close_old_connections, transaction
from django.db.models import Q
from django.utils import timezone

from .ai_filename import extract_embedded_pdf_text, extract_receipt_text_fallback
from .models import (
    CardStatement,
    CardStatementItem,
    CardStatementPlanChangeInference,
    CardStatementReceiptEvidence,
    CardStatementStatus,
    BillingType,
    MonthlyServiceDeclaration,
    PlanChangeInferenceStatus,
    Receipt,
    ReceiptAdminReviewStatus,
    ReceiptFilenameStatus,
    ReceiptFinancialDocumentKind,
    RegisteredService,
    ServiceCatalog,
    StatementMatchReason,
    StatementMatchStatus,
    StatementReceiptEvidenceRole,
    add_months,
    receipt_month_for_statement,
    submission_month_for_receipt,
)
from .statement_ai import generate_card_statement_analysis
from .plan_change_matching import (
    HistoricalPlanReceipt,
    PlanAmountOption,
    PlanChangeDocument,
    PlanStatementLine,
    allocate_unique_plan_change_candidates,
    infer_plan_change_candidate,
)
from .statement_matching import (
    AmountOption,
    EvidenceComponent,
    MATCH_BILLING_BRIDGE,
    MATCH_DIRECT,
    MATCH_LINKED_REFUND_NET,
    MATCH_MERCHANT_REFUND_NET,
    MATCH_ORIGINAL_CHARGE,
    MATCH_REVERSAL_ORIGINAL_CHARGE,
    DEFAULT_REFUND_LOOKAHEAD_DAYS,
    DEFAULT_REFUND_LOOKBACK_DAYS,
    ROLE_CHARGE,
    ROLE_REFUND,
    STATEMENT_ROLE_CHARGE,
    STATEMENT_ROLE_REVERSAL,
    StatementLine,
    format_evidence_calculation,
    merchant_keys_compatible,
    reconcile_statement,
)

logger = logging.getLogger(__name__)


CARD_STATEMENT_MONTH_SEMANTICS_RECONCILE_MARKER = (
    "【月次ルール更新】明細月と対象領収書月の対応を修正したため、最新の領収書と再照合します。"
)
CARD_STATEMENT_SAME_MONTH_RECEIPT_RECONCILE_MARKER = (
    "【月次ルール更新】全社明細月と領収書発行月を同じ月として照合するため、最新の領収書と再照合します。"
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
SERVICE_LABEL_RECONCILE_MARKER = (
    "【照合ルール更新】法的な払先と領収書本文のサービス名を分離し、Google One等をサービス名で照合するため再照合します。"
)
BILLING_DESCRIPTOR_BRIDGE_RECONCILE_MARKER = (
    "【照合ルール更新】Google Play等の決済名義と領収書本文のサービス名を既知の請求経路として照合し、明細未使用書類は未解決明細だけと比較するため再照合します。"
)
PLAN_CHANGE_INFERENCE_RECONCILE_MARKER = (
    "【照合ルール更新】契約変更書類と過去の旧プラン実績による推定対応を追加したため再照合します。"
)
PLAN_CHANGE_METADATA_REFRESH_RECONCILE_MARKER = (
    "【照合ルール更新】契約変更メタデータを再抽出し、旧プラン名を抽出できない定期契約も厳格条件で推定候補へ含めるため再照合します。"
)
PLAN_CHANGE_USER_INFERENCE_RECONCILE_MARKER = (
    "【照合ルール更新】契約変更書類のBill to利用者と前月カード明細の請求周期を推定対応に利用するため再照合します。"
)
RECEIPT_CHANGE_RECONCILE_MARKER = (
    "【領収書更新】領収書の追加・差し替え・AI解析結果更新があったため、最新状態で再照合します。"
)
CROSS_MONTH_CARD_NETTING_RECONCILE_MARKER = (
    "【照合ルール更新】明細内の実利用月を含む領収書参照、法人カード単位の後日返金相殺、返品元決済参照へ更新したため再照合します。"
)
UNMATCHED_RECEIPT_EVENT_SCOPE_RECONCILE_MARKER = (
    "【照合表示更新】保存先の提出月ではなくPDF本文の書類日・取引日を基準に、当月分と実明細に関連する月跨ぎ書類だけを明細未使用一覧へ表示するため再照合します。"
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
    ("GOOGLE_ONE", ("GOOGLEGOOGLEON", "GOOGLEONE", "GOOGLEAIULTRA")),
    ("GOOGLE_PLAY", ("GOOGLEPLAY",)),
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
        | Q(ai_admin_memo__contains=CARD_STATEMENT_SAME_MONTH_RECEIPT_RECONCILE_MARKER)
        | Q(ai_admin_memo__contains=CARD_STATEMENT_MATCHING_RULES_RECONCILE_MARKER)
        | Q(ai_admin_memo__contains=CARD_LAST4_EVIDENCE_RECONCILE_MARKER)
        | Q(ai_admin_memo__contains=EXACT_AMOUNT_MATCHING_RECONCILE_MARKER)
        | Q(ai_admin_memo__contains=SIMPLE_RECEIPT_MATCHING_RECONCILE_MARKER)
        | Q(ai_admin_memo__contains=EMPIRICAL_MATCHING_RECONCILE_MARKER)
        | Q(ai_admin_memo__contains=SERVICE_LABEL_RECONCILE_MARKER)
        | Q(ai_admin_memo__contains=BILLING_DESCRIPTOR_BRIDGE_RECONCILE_MARKER)
        | Q(ai_admin_memo__contains=PLAN_CHANGE_INFERENCE_RECONCILE_MARKER)
        | Q(ai_admin_memo__contains=PLAN_CHANGE_METADATA_REFRESH_RECONCILE_MARKER)
        | Q(ai_admin_memo__contains=PLAN_CHANGE_USER_INFERENCE_RECONCILE_MARKER)
        | Q(ai_admin_memo__contains=RECEIPT_CHANGE_RECONCILE_MARKER)
        | Q(ai_admin_memo__contains=CROSS_MONTH_CARD_NETTING_RECONCILE_MARKER)
        | Q(ai_admin_memo__contains=UNMATCHED_RECEIPT_EVENT_SCOPE_RECONCILE_MARKER)
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


def _available_receipts_for_statement_month(
    statement_month: date,
    *,
    transaction_dates: list[date] | tuple[date, ...] | None = None,
) -> list[Receipt]:
    """Return receipts from every receipt month represented in the statement.

    Card statements can include late-posted transactions from the previous
    calendar month. ReceiptHub stores receipt month M in submission cycle M+1,
    so a July statement containing a June 28 line must query both the July and
    August submission cycles. The statement month itself is always included.
    """

    receipt_months = {date(statement_month.year, statement_month.month, 1)}
    for transaction_date in transaction_dates or ():
        if transaction_date:
            receipt_months.add(date(transaction_date.year, transaction_date.month, 1))
    submission_months = [
        submission_month_for_receipt(receipt_month)
        for receipt_month in sorted(receipt_months)
    ]
    return list(
        Receipt.objects.available_files()
        .filter(
            submission__period_month__in=submission_months,
            submission__user__is_staff=False,
            submission__user__is_superuser=False,
        )
        .filter(Q(is_extra=True) | Q(p_card_usage_snapshot=True))
        .select_related("submission__user", "service", "service__catalog_service", "admin_reviewed_by")
        .order_by("submission__period_month", "issued_on", "uploaded_at", "pk")
        .distinct()
    )


def _unmatched_report_receipt_ids(
    statement_month: date,
    receipts: list[Receipt],
    components: list[EvidenceComponent],
    unresolved_receipts: list[Receipt],
    lines: list[StatementLine],
    catalogs: list[ServiceCatalog],
) -> set[int]:
    """Return physical PDFs that belong in this statement's unused-file report.

    Reconciliation scope is intentionally broader than display scope.  A July
    statement can contain late-posted June transactions, so matching may load
    both June and July receipt pools.  The UI must not label every unrelated
    historical PDF as "明細未使用" merely because that broader pool was read.

    Display a physical PDF when at least one of these conditions is true:

    * its extracted document/transaction date belongs to the selected statement
      month;
    * it is a genuinely relevant cross-month candidate for an actual statement
      line (same merchant and audited date window); or
    * no reliable date was extracted and the PDF was stored in the selected
      month's submission cycle, so hiding it would conceal a parse problem.

    The decision is made from PDF-derived dates first.  Submission.period_month
    is only a fallback because administrators can re-upload older receipts in a
    later cycle.
    """

    target_month = receipt_month_for_statement(statement_month).replace(day=1)
    primary_submission_month = submission_month_for_receipt(target_month).replace(day=1)
    components_by_receipt: dict[int, list[EvidenceComponent]] = {}
    for component in components:
        components_by_receipt.setdefault(component.receipt_id, []).append(component)
    unresolved_ids = {receipt.pk for receipt in unresolved_receipts}

    def is_target_month(value: date | None) -> bool:
        return bool(value and value.replace(day=1) == target_month)

    def component_is_cross_month_relevant(component: EvidenceComponent) -> bool:
        if component.event_date is None:
            return False
        for line in lines:
            if line.transaction_date is None:
                continue
            if not merchant_keys_compatible(line.merchant_key, component.merchant_key):
                continue
            distance = (component.event_date - line.transaction_date).days
            if component.role == ROLE_REFUND:
                if -DEFAULT_REFUND_LOOKBACK_DAYS <= distance <= DEFAULT_REFUND_LOOKAHEAD_DAYS:
                    return True
            elif abs(distance) <= DATE_MATCH_TOLERANCE_DAYS:
                return True
        return False

    scoped_ids: set[int] = set()
    for receipt in receipts:
        receipt_components = components_by_receipt.get(receipt.pk, [])
        extracted_dates = [
            component.event_date
            for component in receipt_components
            if component.event_date is not None
        ]
        if receipt.issued_on is not None:
            extracted_dates.append(receipt.issued_on)

        # Current-month PDFs remain visible even when they do not match any line.
        if any(is_target_month(value) for value in extracted_dates):
            scoped_ids.add(receipt.pk)
            continue

        # A previous-month PDF is visible only when it could plausibly explain an
        # actual line in this statement.  This preserves 0383/0466-style lookup
        # without surfacing unrelated receipts from the entire previous month.
        if any(component_is_cross_month_relevant(component) for component in receipt_components):
            scoped_ids.add(receipt.pk)
            continue

        if receipt.pk in unresolved_ids and receipt.issued_on is not None:
            merchant_key = _unresolved_receipt_key(receipt, catalogs)
            if merchant_key and any(
                line.transaction_date is not None
                and merchant_keys_compatible(line.merchant_key, merchant_key)
                and abs((receipt.issued_on - line.transaction_date).days) <= DATE_MATCH_TOLERANCE_DAYS
                for line in lines
            ):
                scoped_ids.add(receipt.pk)
                continue

        # Only undated/parse-failed PDFs use the storage cycle as a fallback.
        # A reliably dated old PDF re-uploaded this month must not be shown.
        if not extracted_dates and receipt.submission.period_month.replace(day=1) == primary_submission_month:
            scoped_ids.add(receipt.pk)

    return scoped_ids


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

    if (
        receipt.financial_metadata_checked_at is not None
        and receipt.plan_change_metadata_checked_at is not None
    ):
        return

    update_fields = ["financial_metadata_checked_at", "plan_change_metadata_checked_at", "updated_at"]
    checked_at = timezone.now()
    receipt.financial_metadata_checked_at = checked_at
    receipt.plan_change_metadata_checked_at = checked_at
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
        if fallback.service_label and not receipt.ai_extracted_service_label:
            receipt.ai_extracted_service_label = fallback.service_label[:160]
            update_fields.append("ai_extracted_service_label")
        if fallback.plan_name and not receipt.ai_extracted_plan_name:
            receipt.ai_extracted_plan_name = fallback.plan_name[:160]
            update_fields.append("ai_extracted_plan_name")
        if fallback.plan_change_details:
            receipt.plan_change_details = dict(fallback.plan_change_details)
            update_fields.append("plan_change_details")
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
                "service_label": receipt.ai_extracted_service_label,
                "invoice_number": receipt.financial_transaction_reference,
                "transaction_id": receipt.financial_transaction_reference,
                "related_transaction_id": receipt.financial_related_reference,
                "source_label": "主要取引",
                "confidence": 0.7,
                "document_kind": receipt.financial_document_kind,
            }
        ]
        update_fields.append("financial_transaction_components")

    # 明細再照合中のメタデータ補完で Receipt.save() を呼ぶと、Receipt の
    # post_save シグナルから同じ明細再照合が再帰的に起動する。特に
    # v1.14.1 の既存メタデータ再抽出後は、多数の領収書で再帰が連鎖し、
    # 新規アップロード要求までタイムアウトさせるため、ここではシグナルを
    # 発火しない QuerySet.update() で永続化する。
    unique_fields = list(dict.fromkeys(update_fields))
    updated_at = timezone.now()
    update_values = {
        field_name: getattr(receipt, field_name)
        for field_name in unique_fields
        if field_name != "updated_at"
    }
    update_values["updated_at"] = updated_at
    Receipt.objects.filter(pk=receipt.pk).update(**update_values)
    receipt.updated_at = updated_at


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
            service_label = str(raw.get("service_label") or receipt.ai_extracted_service_label or "").strip()
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
            # Google Asia Pacificのような法的販売者名だけでは、Google Oneと
            # Google Cloudを区別できない。領収書本文に製品・サービス名が明示
            # されている場合は、そのサービス名の既知キーを最優先する。
            service_merchant_key = _known_merchant_key(service_label)
            merchant_key = service_merchant_key or _canonical_merchant_key(payee, catalogs)
            if not merchant_key:
                continue
            evidence_payee = payee
            raw_key = str(raw.get("component_key") or f"component-{index + 1}")
            component_key = f"receipt-{receipt.pk}:{raw_key}"[:160]
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
                    service_label=service_label[:160],
                )
            )
            valid_count += 1
        if valid_count == 0:
            unresolved.append(receipt)
    return components, unresolved


def _historical_receipts_for_statement_month(statement_month: date) -> list[Receipt]:
    """契約変更推定に使う過去3領収書月のメタデータ。

    現在の明細月Mに対応する領収書は提出サイクルM+1へ保存されるため、
    過去3領収書月はその直前3提出サイクルから取得する。ファイル保存期限を
    過ぎても、抽出済みの日付・金額・プラン名はDBに残るためavailable_files()
    では絞らない。
    """

    current_submission_month = submission_month_for_receipt(statement_month)
    start_submission_month = add_months(current_submission_month, -3)
    return list(
        Receipt.objects.filter(
            submission__period_month__gte=start_submission_month,
            submission__period_month__lt=current_submission_month,
            submission__user__is_staff=False,
            submission__user__is_superuser=False,
        )
        .filter(Q(is_extra=True) | Q(p_card_usage_snapshot=True))
        .select_related("submission__user", "service", "service__catalog_service")
        .order_by("submission__period_month", "issued_on", "uploaded_at", "pk")
    )


def _recipient_email(value: str) -> str:
    match = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", value or "", flags=re.I)
    return match.group(0).lower() if match else ""


def _plan_change_user_map() -> dict[str, int]:
    user_model = get_user_model()
    mapping: dict[str, int] = {}
    collisions: set[str] = set()
    for user in user_model.objects.filter(is_active=True, is_staff=False, is_superuser=False).only(
        "id", "username", "email"
    ):
        for raw in (user.username, user.email):
            key = (raw or "").strip().lower()
            if not key:
                continue
            if key in mapping and mapping[key] != user.pk:
                collisions.add(key)
            else:
                mapping[key] = user.pk
    for key in collisions:
        mapping.pop(key, None)
    return mapping


def _plan_change_documents(
    receipts: list[Receipt],
    catalogs: list[ServiceCatalog],
) -> list[PlanChangeDocument]:
    documents: list[PlanChangeDocument] = []
    user_map = _plan_change_user_map()
    for receipt in receipts:
        details = receipt.plan_change_details or {}
        previous_plan = str(details.get("previous_plan") or "").strip()
        previous_end = _parse_date(details.get("previous_plan_end"))
        if not previous_plan or previous_end is None:
            continue
        context = receipt.ai_extracted_service_label or receipt.ai_extracted_payee
        merchant_key = _known_merchant_key(context) or _canonical_merchant_key(context, catalogs)
        if not merchant_key:
            continue
        try:
            confidence = float(details.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0

        # 管理者代理アップロード先が誤っていても、AIがBill toのメールを
        # 明確に読み取れている場合は、そのメールと一意に一致する一般ユーザーを
        # 推定利用者として優先する。曖昧な表示名だけではユーザーを変更しない。
        user_id = receipt.submission.user_id
        recipient_email = _recipient_email(
            " ".join(
                part
                for part in (
                    receipt.ai_extracted_recipient_name,
                    receipt.ai_recipient_name_check_memo,
                )
                if part
            )
        )
        if recipient_email and receipt.ai_check_recipient_name:
            user_id = user_map.get(recipient_email, user_id)

        documents.append(
            PlanChangeDocument(
                receipt_id=receipt.pk,
                user_id=user_id,
                filename=receipt.display_filename,
                merchant_key=merchant_key,
                previous_plan=previous_plan,
                new_plan=str(details.get("new_plan") or receipt.ai_extracted_plan_name or "").strip(),
                change_date=_parse_date(details.get("change_date")),
                previous_plan_end=previous_end,
                confidence=confidence,
            )
        )
    return documents


def _service_for_inferred_user(
    *,
    user_id: int,
    change_receipt: Receipt,
    historical_receipt: Receipt | None,
) -> RegisteredService | None:
    """推定利用者に属するサービスだけを明細行へ紐付ける。

    Bill toメールから推定した利用者と、管理者代理アップロード時に選択された
    ユーザーが異なる場合でも、別ユーザーのRegisteredServiceを保存しない。
    """

    for candidate in (historical_receipt.service if historical_receipt else None, change_receipt.service):
        if candidate is not None and candidate.user_id == user_id:
            return candidate

    catalog_id = None
    for candidate in (historical_receipt.service if historical_receipt else None, change_receipt.service):
        if candidate is not None and candidate.catalog_service_id:
            catalog_id = candidate.catalog_service_id
            break
    if catalog_id:
        service = (
            RegisteredService.objects.filter(user_id=user_id, catalog_service_id=catalog_id)
            .order_by("-is_active", "pk")
            .first()
        )
        if service is not None:
            return service

    # カタログ紐付けがない旧データ向けの安全なフォールバック。
    source = (historical_receipt.service if historical_receipt else None) or change_receipt.service
    if source is None:
        return None
    return (
        RegisteredService.objects.filter(
            user_id=user_id,
            name__iexact=source.name,
            billing_type=source.billing_type,
        )
        .order_by("-is_active", "pk")
        .first()
    )


def _historical_statement_plan_evidences(
    statement_month: date,
    catalogs: list[ServiceCatalog],
) -> list[HistoricalPlanReceipt]:
    """前月カード明細から旧プランの金額・請求周期だけを補助証拠化する。

    これは領収書提出の証拠ではない。契約変更書類が利用者、旧プラン、
    終了日を明示している場合に限り、管理者確認前の「推定対応」を作る
    ための補助証拠として使用する。
    """

    previous_statement_month = add_months(statement_month, -1)
    items = (
        CardStatementItem.objects.filter(
            statement__period_month=previous_statement_month,
            statement__status__in=[CardStatementStatus.COMPLETED, CardStatementStatus.NEEDS_REVIEW],
            receipt_required=True,
            transaction_date__isnull=False,
        )
        .exclude(match_status=StatementMatchStatus.IGNORED)
        .select_related("statement")
        .order_by("transaction_date", "sequence", "pk")
    )
    evidences: list[HistoricalPlanReceipt] = []
    for item in items:
        merchant_key = _canonical_merchant_key(item.merchant_name, catalogs)
        if not merchant_key:
            continue
        options: list[tuple[Decimal, str]] = []
        if item.original_amount is not None and item.original_currency:
            options.append((item.original_amount, item.original_currency.upper()))
        if item.amount_jpy is not None:
            jpy = (item.amount_jpy, "JPY")
            if jpy not in options:
                options.append(jpy)
        for amount, currency in options:
            reference = item.line_reference or str(item.sequence)
            evidences.append(
                HistoricalPlanReceipt(
                    receipt_id=None,
                    user_id=None,
                    filename=f"前月カード明細 {reference}",
                    merchant_key=merchant_key,
                    plan_name="",
                    event_date=item.transaction_date,
                    amount=amount,
                    currency=currency,
                    document_quality=5,
                    recurring_service=True,
                    evidence_key=f"statement:{item.pk}:{currency}:{amount}",
                    source_type="statement",
                )
            )
    return evidences

def _historical_plan_evidences(
    receipts: list[Receipt],
    catalogs: list[ServiceCatalog],
) -> list[HistoricalPlanReceipt]:
    evidences: list[HistoricalPlanReceipt] = []
    for receipt in receipts:
        _enrich_receipt_financial_metadata(receipt)
        plan_name = (receipt.ai_extracted_plan_name or "").strip()
        if receipt.amount is None or not receipt.currency or not receipt.issued_on:
            continue
        billing_type = (receipt.billing_type_snapshot or "").strip()
        if receipt.service_id and receipt.service:
            billing_type = receipt.service.billing_type or billing_type
        recurring_service = billing_type == BillingType.SUBSCRIPTION
        if not plan_name and not recurring_service:
            continue
        context = receipt.ai_extracted_service_label or receipt.ai_extracted_payee
        merchant_key = _known_merchant_key(context) or _canonical_merchant_key(context, catalogs)
        if not merchant_key:
            continue
        document_quality = 0 if receipt.financial_document_kind == ReceiptFinancialDocumentKind.CHARGE else 1
        if receipt.admin_review_status != ReceiptAdminReviewStatus.CONFIRMED:
            document_quality += 1
        evidences.append(
            HistoricalPlanReceipt(
                receipt_id=receipt.pk,
                user_id=receipt.submission.user_id,
                filename=receipt.display_filename,
                merchant_key=merchant_key,
                plan_name=plan_name,
                event_date=receipt.issued_on,
                amount=receipt.amount,
                currency=receipt.currency,
                document_quality=document_quality,
                recurring_service=recurring_service,
            )
        )
    return evidences


def _plan_statement_line(line: StatementLine) -> PlanStatementLine:
    return PlanStatementLine(
        key=line.key,
        transaction_date=line.transaction_date,
        merchant_key=line.merchant_key,
        amount_options=tuple(PlanAmountOption(option.amount, option.currency) for option in line.amount_options),
    )


def _plan_inference_reason(candidate) -> str:
    change_date = candidate.change_date.isoformat() if candidate.change_date else "未抽出"
    if candidate.historical_source_type == "statement":
        historical_basis = (
            f"前月のカード明細「{candidate.historical_filename}」に "
            f"{candidate.historical_date.isoformat()}、{candidate.amount} {candidate.currency} の同一請求周期があり、"
        )
    elif candidate.historical_plan_explicit:
        historical_basis = (
            f"過去領収書「{candidate.historical_filename}」には旧プラン "
            f"{candidate.previous_plan} が明記され、"
            f"{candidate.historical_date.isoformat()}、{candidate.amount} {candidate.currency} の実績があり、"
        )
    else:
        historical_basis = (
            f"過去領収書「{candidate.historical_filename}」では旧プラン名を抽出できませんでしたが、"
            "同一ユーザーの定期契約サービスとして、"
            f"{candidate.historical_date.isoformat()}、{candidate.amount} {candidate.currency} の実績があり、"
        )
    return (
        "契約変更情報による推定対応です。"
        f"契約変更書類「{candidate.change_filename}」に、旧プラン "
        f"{candidate.previous_plan}、新プラン {candidate.new_plan or '未抽出'}、変更日 {change_date}、"
        f"旧プラン終了日 {candidate.previous_plan_end.isoformat()} が明記されています。"
        + historical_basis
        + "今回の明細の請求元・金額・通貨・請求周期と一致します。"
        "当月の直接領収書ではないため、管理者が根拠を確認して一致確定または不採用を選択してください。"
    )


REVERSAL_TEXT_MARKERS = ("返品", "取消", "キャンセル", "REVERSAL", "REFUND", "CREDITREVERSAL")


def _statement_item_is_reversal(item: CardStatementItem) -> bool:
    for amount in (item.original_amount, item.amount_jpy):
        if amount is not None and amount < 0:
            return True
    text = unicodedata.normalize(
        "NFKC",
        " ".join(
            value
            for value in (
                item.merchant_name,
                item.merchant_normalized,
                _base_match_memo(item.match_memo),
            )
            if value
        ),
    ).upper()
    return any(marker in text for marker in REVERSAL_TEXT_MARKERS)


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
        reference=item.line_reference,
        statement_role=(
            STATEMENT_ROLE_REVERSAL
            if _statement_item_is_reversal(item)
            else STATEMENT_ROLE_CHARGE
        ),
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
        MATCH_BILLING_BRIDGE: StatementMatchReason.AUTO_STRONG,
        MATCH_ORIGINAL_CHARGE: StatementMatchReason.ORIGINAL_CHARGE,
        MATCH_REVERSAL_ORIGINAL_CHARGE: StatementMatchReason.ORIGINAL_CHARGE,
        MATCH_LINKED_REFUND_NET: StatementMatchReason.LINKED_REFUND_NET,
        MATCH_MERCHANT_REFUND_NET: StatementMatchReason.MERCHANT_REFUND_NET,
    }.get(match_type, StatementMatchReason.AUTO_STRONG)


def _match_type_label(match_type: str) -> str:
    return {
        MATCH_DIRECT: "直接一致",
        MATCH_BILLING_BRIDGE: "決済名義互換一致",
        MATCH_ORIGINAL_CHARGE: "返金書内の元決済確認",
        MATCH_REVERSAL_ORIGINAL_CHARGE: "返品元決済確認",
        MATCH_LINKED_REFUND_NET: "紐付返金相殺",
        MATCH_MERCHANT_REFUND_NET: "法人カード単位の後日返金相殺",
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
                service_label_snapshot=component.service_label[:160],
                invoice_number_snapshot=component.invoice_number[:160],
                transaction_reference_snapshot=component.transaction_id[:160],
                related_transaction_reference_snapshot=component.related_transaction_id[:160],
                source_label=component.source_label[:120],
            )
        )
    return records


def _unresolved_receipt_key(receipt: Receipt, catalogs: list[ServiceCatalog]) -> str:
    context = receipt.ai_extracted_service_label or receipt.ai_extracted_payee
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
        and merchant_keys_compatible(
            line.merchant_key,
            _statement_line(candidate, catalogs).merchant_key,
        )
        and any(value.currency == option.currency and value.amount == option.amount for value in _statement_line(candidate, catalogs).amount_options)
    )
    support = sum(
        1
        for component in components
        if component.role == ROLE_CHARGE
        and merchant_keys_compatible(line.merchant_key, component.merchant_key)
        and component.currency == option.currency
        and component.signed_amount == option.amount
    )
    if same_lines > 1 and support < same_lines:
        return (
            f"同一請求元・金額・通貨の明細{same_lines}件に対して、明示参照番号で重複除外した"
            f"決済証憑は{support}件です。少なくとも{same_lines - support}件不足しています。"
            "同額・同請求元の明細を利用者情報なしで一対一割当しているため、この行は不足件数を代表して"
            "未一致表示しており、特定ユーザーの不足を意味しません。"
        )
    return "対応する提出書類を確認できません。"



def _line_amount_for_currency(line: StatementLine, currency: str) -> AmountOption | None:
    return next((option for option in line.amount_options if option.currency == currency), None)


def _unused_component_reason(
    component: EvidenceComponent,
    lines: list[StatementLine],
) -> dict[str, Any]:
    """未使用の提出証拠が明細へ紐付かなかった理由を説明する。"""

    if not lines:
        return {
            "reason_code": "no_unresolved_statement_line",
            "reason": "未一致・解析要確認の明細行は残っていません。提出書類の重複または対象外月を確認してください。",
        }

    merchant_lines = [
        line for line in lines
        if merchant_keys_compatible(line.merchant_key, component.merchant_key)
    ]
    if not merchant_lines:
        return {
            "reason_code": "merchant_not_found",
            "reason": "未一致・解析要確認の明細行に、同じ請求元・サービスまたは既知の決済名義対応がありません。",
        }

    amount_compatible: list[tuple[StatementLine, AmountOption]] = []
    same_currency: list[tuple[StatementLine, AmountOption]] = []
    for line in merchant_lines:
        option = _line_amount_for_currency(line, component.currency)
        if option is None:
            continue
        same_currency.append((line, option))
        if option.amount == component.signed_amount:
            amount_compatible.append((line, option))

    def date_distance(line: StatementLine) -> int:
        if line.transaction_date and component.event_date:
            return abs((line.transaction_date - component.event_date).days)
        return 9999

    if amount_compatible:
        line, option = min(amount_compatible, key=lambda pair: (date_distance(pair[0]), pair[0].sequence, pair[0].key))
        distance = date_distance(line)
        if distance > DATE_MATCH_TOLERANCE_DAYS:
            return {
                "reason_code": "date_outside_window",
                "reason": (
                    f"請求元・金額・通貨は明細 {line.reference or line.sequence} と一致しますが、"
                    f"日付差が{distance}日あり、±{DATE_MATCH_TOLERANCE_DAYS}日の照合範囲外です。"
                ),
                "closest_line_key": line.key,
                "closest_line_reference": line.reference,
                "closest_line_sequence": line.sequence,
                "closest_statement_date": line.transaction_date.isoformat() if line.transaction_date else "",
                "closest_statement_amount": format(option.amount, "f"),
                "closest_statement_currency": option.currency,
            }
        return {
            "reason_code": "surplus_evidence",
            "reason": (
                "同じ請求元・金額・通貨・近接日付の明細はありますが、他の提出証拠が先に一対一で割り当てられています。"
                "明細件数より提出証拠が多い、または重複書類の可能性があります。"
            ),
            "closest_line_key": line.key,
            "closest_line_reference": line.reference,
            "closest_line_sequence": line.sequence,
            "closest_statement_date": line.transaction_date.isoformat() if line.transaction_date else "",
            "closest_statement_amount": format(option.amount, "f"),
            "closest_statement_currency": option.currency,
        }

    if same_currency:
        line, option = min(same_currency, key=lambda pair: (date_distance(pair[0]), pair[0].sequence, pair[0].key))
        distance = date_distance(line)
        if distance <= DATE_MATCH_TOLERANCE_DAYS:
            return {
                "reason_code": "amount_mismatch",
                "reason": (
                    f"同じ請求元で日付が近い明細 {line.reference or line.sequence} がありますが、"
                    f"金額が明細 {format(option.amount, 'f')} {option.currency} に対し、"
                    f"提出書類は {format(component.signed_amount, 'f')} {component.currency} です。"
                ),
                "closest_line_key": line.key,
                "closest_line_reference": line.reference,
                "closest_line_sequence": line.sequence,
                "closest_statement_date": line.transaction_date.isoformat() if line.transaction_date else "",
                "closest_statement_amount": format(option.amount, "f"),
                "closest_statement_currency": option.currency,
            }
        return {
            "reason_code": "amount_and_date_mismatch",
            "reason": "同じ請求元の明細はありますが、金額完全一致かつ日付±1日の条件を満たしません。",
            "closest_line_key": line.key,
            "closest_line_reference": line.reference,
            "closest_line_sequence": line.sequence,
            "closest_statement_date": line.transaction_date.isoformat() if line.transaction_date else "",
            "closest_statement_amount": format(option.amount, "f"),
            "closest_statement_currency": option.currency,
        }

    line = min(merchant_lines, key=lambda candidate: (date_distance(candidate), candidate.sequence, candidate.key))
    return {
        "reason_code": "currency_mismatch",
        "reason": "同じ請求元の明細はありますが、比較可能な同一通貨の金額がありません。",
        "closest_line_key": line.key,
        "closest_line_reference": line.reference,
        "closest_line_sequence": line.sequence,
        "closest_statement_date": line.transaction_date.isoformat() if line.transaction_date else "",
    }


def _build_unmatched_receipt_snapshot(
    *,
    unused_components: list[EvidenceComponent],
    unresolved_receipts: list[Receipt],
    lines: list[StatementLine],
    receipt_by_id: dict[int, Receipt],
) -> list[dict[str, Any]]:
    """Build one unmatched row per physical uploaded PDF.

    Refund documents can contain both the original charge and a credit component.
    The UI section is explicitly a list of *files*, so the same PDF must not be
    repeated once for every extracted financial component.
    """

    snapshots: list[dict[str, Any]] = []
    grouped: dict[int, list[EvidenceComponent]] = {}
    for component in unused_components:
        grouped.setdefault(component.receipt_id, []).append(component)

    for receipt_id, receipt_components in sorted(
        grouped.items(),
        key=lambda value: (
            min(component.receipt_order for component in value[1]),
            value[0],
        ),
    ):
        receipt = receipt_by_id.get(receipt_id)
        if receipt is None:
            continue
        ordered = sorted(
            receipt_components,
            key=lambda component: (
                0 if component.role == ROLE_REFUND else 1,
                component.event_date or date.min,
                component.key,
            ),
        )
        representative = ordered[0]
        reason = _unused_component_reason(representative, lines)
        snapshots.append(
            {
                "receipt_id": receipt.pk,
                "component_key": representative.key,
                "component_count": len(ordered),
                "components": [
                    {
                        "component_key": component.key,
                        "event_date": component.event_date.isoformat() if component.event_date else "",
                        "amount": format(component.signed_amount, "f"),
                        "currency": component.currency,
                        "role": component.role,
                    }
                    for component in ordered
                ],
                "filename": receipt.display_filename,
                "original_filename": receipt.original_filename,
                "user": receipt.submission.user.username,
                "service": receipt.service_display_name_snapshot,
                "service_label": representative.service_label or receipt.ai_extracted_service_label,
                "payee": representative.payee or receipt.ai_extracted_payee,
                "event_date": representative.event_date.isoformat() if representative.event_date else "",
                "amount": format(representative.signed_amount, "f"),
                "currency": representative.currency,
                "role": representative.role,
                "document_kind": representative.document_kind,
                "source_label": representative.source_label,
                **reason,
            }
        )

    represented_receipt_ids = set(grouped)
    for receipt in unresolved_receipts:
        if receipt.pk in represented_receipt_ids:
            continue
        snapshots.append(
            {
                "receipt_id": receipt.pk,
                "component_key": "",
                "component_count": 0,
                "components": [],
                "filename": receipt.display_filename,
                "original_filename": receipt.original_filename,
                "user": receipt.submission.user.username,
                "service": receipt.service_display_name_snapshot,
                "service_label": receipt.ai_extracted_service_label,
                "payee": receipt.ai_extracted_payee,
                "event_date": receipt.issued_on.isoformat() if receipt.issued_on else "",
                "amount": format(receipt.amount, "f") if receipt.amount is not None else "",
                "currency": receipt.currency or "",
                "role": "",
                "document_kind": receipt.financial_document_kind,
                "source_label": "",
                "reason_code": "parse_review",
                "reason": "提出ファイルはありますが、明細照合に必要な取引構成要素を抽出できません。AI再検査または管理者確認が必要です。",
            }
        )
    return snapshots


def reconcile_card_statement_items(statement_id: int, *, preserve_manual: bool = True) -> CardStatement:
    """全ユーザーの提出PDFを、実データ検証済みルールで明細へ照合する。"""

    statement = CardStatement.objects.get(pk=statement_id)
    if statement.status == CardStatementStatus.PROCESSING:
        return statement

    items = list(
        statement.items.select_related(
            "matched_user", "matched_catalog_service", "matched_service", "matched_receipt",
            "matched_receipt__submission__user", "matched_receipt__service__catalog_service",
            "plan_change_inference__change_receipt",
            "plan_change_inference__historical_receipt",
            "plan_change_inference__user",
        ).order_by("sequence", "id")
    )
    receipts = _available_receipts_for_statement_month(
        statement.period_month,
        transaction_dates=[item.transaction_date for item in items if item.transaction_date],
    )
    receipt_by_id = {receipt.pk: receipt for receipt in receipts}
    catalogs = list(ServiceCatalog.objects.all().order_by("pk"))
    components, unresolved_receipts = _receipt_components(receipts, catalogs)
    historical_receipts = _historical_receipts_for_statement_month(statement.period_month)
    plan_change_documents = _plan_change_documents(receipts, catalogs)
    historical_plan_evidences = [
        *_historical_plan_evidences(historical_receipts, catalogs),
        *_historical_statement_plan_evidences(statement.period_month, catalogs),
    ]
    existing_inferences = {
        inference.statement_item_id: inference
        for inference in CardStatementPlanChangeInference.objects.filter(statement_item__statement=statement)
        .select_related("change_receipt", "historical_receipt", "user")
    }
    statement_errors = _statement_gate_errors(statement)

    manual_item_ids = {item.pk for item in items if preserve_manual and _is_manual_override(item)}
    manual_component_keys = set(
        CardStatementReceiptEvidence.objects.filter(statement_item_id__in=manual_item_ids)
        .values_list("component_key", flat=True)
    )
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

    unmatched_report_receipt_ids = _unmatched_report_receipt_ids(
        statement.period_month,
        receipts,
        components,
        unresolved_receipts,
        auto_lines,
        catalogs,
    )

    reconciliation = reconcile_statement(
        auto_lines,
        [component for component in components if component.key not in manual_component_keys],
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

    historical_receipt_by_id = {receipt.pk: receipt for receipt in historical_receipts}
    inferred_candidates_by_item: dict[int, Any] = {}
    unresolved_pool = list(unresolved_receipts)
    unassigned_items = [
        item for item in auto_items if str(item.pk) not in reconciliation.assignments
    ]

    # Build candidates for every still-unmatched line first, then allocate them
    # globally.  The same plan-change document or historical old-plan receipt
    # must never explain more than one statement line, and an exact old-plan
    # end-date match must be preferred over a merely adjacent line.
    ranked_plan_candidates: list[tuple[tuple, CardStatementItem, Any]] = []
    for item in unassigned_items:
        line = line_by_key[str(item.pk)]
        candidate = infer_plan_change_candidate(
            _plan_statement_line(line),
            plan_change_documents,
            historical_plan_evidences,
            date_tolerance_days=1,
            billing_day_tolerance_days=1,
            minimum_confidence=0.75,
        )
        existing_inference = existing_inferences.get(item.pk)
        if (
            candidate is not None
            and existing_inference is not None
            and existing_inference.status == PlanChangeInferenceStatus.REJECTED
            and existing_inference.candidate_fingerprint == candidate.fingerprint
        ):
            candidate = None
        if candidate is None:
            continue
        if candidate.change_receipt_id not in receipt_by_id:
            continue
        if (
            candidate.historical_receipt_id is not None
            and candidate.historical_receipt_id not in historical_receipt_by_id
        ):
            continue
        ranked_plan_candidates.append(
            (
                (
                    candidate.end_date_distance,
                    candidate.billing_day_distance,
                    -round(candidate.confidence, 4),
                    item.sequence,
                    item.pk,
                ),
                item,
                candidate,
            )
        )

    allocated_plan_candidates = allocate_unique_plan_change_candidates(
        [(item.sequence, item.pk, candidate) for _, item, candidate in ranked_plan_candidates]
    )
    inferred_candidates_by_item = {
        int(line_key): candidate for line_key, candidate in allocated_plan_candidates.items()
    }

    for item in unassigned_items:
        line = line_by_key[str(item.pk)]
        candidate = inferred_candidates_by_item.get(item.pk)
        if candidate is not None:
            change_receipt = receipt_by_id[candidate.change_receipt_id]
            historical_receipt = (
                historical_receipt_by_id.get(candidate.historical_receipt_id)
                if candidate.historical_receipt_id is not None
                else None
            )
            _clear_item_match(item)
            item.matched_user_id = candidate.user_id
            item.matched_service = _service_for_inferred_user(
                user_id=candidate.user_id,
                change_receipt=change_receipt,
                historical_receipt=historical_receipt,
            )
            item.matched_catalog_service = (
                item.matched_service.catalog_service
                if item.matched_service_id and item.matched_service.catalog_service_id
                else None
            )
            item.match_status = StatementMatchStatus.INFERRED
            item.match_reason_code = StatementMatchReason.PLAN_CHANGE_INFERRED
            item.match_confidence = candidate.confidence
            item.match_memo = _plan_inference_reason(candidate)
            continue

        related_index = next(
            (
                index
                for index, receipt in enumerate(unresolved_pool)
                if merchant_keys_compatible(
                    line.merchant_key,
                    _unresolved_receipt_key(receipt, catalogs),
                )
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

    used_receipt_ids = {
        component.receipt_id
        for matched_components in evidence_components_by_item.values()
        for component in matched_components
    }
    unused_components = [
        component_by_key[key]
        for key in reconciliation.unused_component_keys
        if key in component_by_key
        and component_by_key[key].receipt_id not in used_receipt_ids
        and component_by_key[key].receipt_id in unmatched_report_receipt_ids
    ]
    unmatched_report_unresolved_receipts = [
        receipt
        for receipt in unresolved_pool
        if receipt.pk not in used_receipt_ids
        and receipt.pk in unmatched_report_receipt_ids
    ]
    unresolved_line_keys = {
        str(item.pk)
        for item in auto_items
        if item.match_status in {StatementMatchStatus.UNMATCHED, StatementMatchStatus.NEEDS_REVIEW}
    }
    unresolved_lines = [line for line in auto_lines if line.key in unresolved_line_keys]
    unmatched_receipt_snapshot = _build_unmatched_receipt_snapshot(
        unused_components=unused_components,
        unresolved_receipts=unmatched_report_unresolved_receipts,
        lines=unresolved_lines,
        receipt_by_id=receipt_by_id,
    )

    no_usage_conflicts: list[str] = []
    for item in items:
        if item.match_status == StatementMatchStatus.MATCHED and item.matched_service_id:
            deleted, _ = MonthlyServiceDeclaration.objects.filter(
                user=item.matched_service.user,
                service=item.matched_service,
                period_month=statement.submission_month,
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
            candidate = inferred_candidates_by_item.get(item.pk)
            existing = existing_inferences.get(item.pk)
            if candidate is not None:
                change_receipt = receipt_by_id[candidate.change_receipt_id]
                historical_receipt = (
                    historical_receipt_by_id.get(candidate.historical_receipt_id)
                    if candidate.historical_receipt_id is not None
                    else None
                )
                user_model = get_user_model()
                inferred_user = user_model.objects.filter(pk=candidate.user_id).first()
                defaults = {
                    "user": inferred_user,
                    "user_snapshot": inferred_user.get_username() if inferred_user else str(candidate.user_id),
                    "change_receipt": change_receipt,
                    "historical_receipt": historical_receipt,
                    "change_filename_snapshot": candidate.change_filename[:255],
                    "historical_filename_snapshot": candidate.historical_filename[:255],
                    "previous_plan": candidate.previous_plan[:160],
                    "new_plan": candidate.new_plan[:160],
                    "change_date": candidate.change_date,
                    "previous_plan_end": candidate.previous_plan_end,
                    "historical_receipt_date": candidate.historical_date,
                    "amount": candidate.amount,
                    "currency": candidate.currency[:3],
                    "confidence": candidate.confidence,
                    "reason": _plan_inference_reason(candidate),
                    "candidate_fingerprint": candidate.fingerprint[:255],
                    "status": PlanChangeInferenceStatus.PENDING,
                    "reviewed_by": None,
                    "reviewed_at": None,
                }
                CardStatementPlanChangeInference.objects.update_or_create(
                    statement_item=item,
                    defaults=defaults,
                )
            elif existing is not None and item.pk not in manual_item_ids:
                if not (
                    existing.status == PlanChangeInferenceStatus.REJECTED
                    and item.match_status == StatementMatchStatus.UNMATCHED
                ):
                    existing.delete()

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
        inferred_count = sum(1 for item in items if item.receipt_required and item.match_status == StatementMatchStatus.INFERRED)
        direct_count = sum(1 for item in items if item.match_reason_code == StatementMatchReason.AUTO_STRONG)
        original_count = sum(1 for item in items if item.match_reason_code == StatementMatchReason.ORIGINAL_CHARGE)
        linked_count = sum(1 for item in items if item.match_reason_code == StatementMatchReason.LINKED_REFUND_NET)
        merchant_net_count = sum(1 for item in items if item.match_reason_code == StatementMatchReason.MERCHANT_REFUND_NET)

        card_or_period_problem = bool(statement_errors)
        if statement.status != CardStatementStatus.FAILED:
            statement.status = (
                CardStatementStatus.NEEDS_REVIEW
                if card_or_period_problem or not items or missing_count or review_count or inferred_count
                else CardStatementStatus.COMPLETED
            )

        extraction_memo = (statement.ai_admin_memo or "").split("【照合結果】", 1)[0]
        for marker in (
            CARD_STATEMENT_MONTH_SEMANTICS_RECONCILE_MARKER,
            CARD_STATEMENT_SAME_MONTH_RECEIPT_RECONCILE_MARKER,
            CARD_STATEMENT_MATCHING_RULES_RECONCILE_MARKER,
            CARD_LAST4_EVIDENCE_RECONCILE_MARKER,
            EXACT_AMOUNT_MATCHING_RECONCILE_MARKER,
            SIMPLE_RECEIPT_MATCHING_RECONCILE_MARKER,
            EMPIRICAL_MATCHING_RECONCILE_MARKER,
            SERVICE_LABEL_RECONCILE_MARKER,
            BILLING_DESCRIPTOR_BRIDGE_RECONCILE_MARKER,
            PLAN_CHANGE_INFERENCE_RECONCILE_MARKER,
            PLAN_CHANGE_METADATA_REFRESH_RECONCILE_MARKER,
            PLAN_CHANGE_USER_INFERENCE_RECONCILE_MARKER,
            RECEIPT_CHANGE_RECONCILE_MARKER,
            CROSS_MONTH_CARD_NETTING_RECONCILE_MARKER,
            UNMATCHED_RECEIPT_EVENT_SCOPE_RECONCILE_MARKER,
        ):
            extraction_memo = extraction_memo.replace(marker, "")
        extraction_memo = extraction_memo.strip()
        target_receipt_months = sorted(
            {
                date(statement.period_month.year, statement.period_month.month, 1),
                *(
                    date(item.transaction_date.year, item.transaction_date.month, 1)
                    for item in items
                    if item.transaction_date
                ),
            }
        )
        target_receipt_month_label = "・".join(month.strftime("%Y-%m") for month in target_receipt_months)
        unused_report_scope_count = len(unmatched_report_receipt_ids)
        internal_reference_only_count = max(len(receipts) - unused_report_scope_count, 0)
        receipt_scope_summary = (
            f"照合候補PDF{len(receipts)}件（明細未使用一覧の表示対象PDF{unused_report_scope_count}件"
        )
        if internal_reference_only_count:
            receipt_scope_summary += f"、無関係な月跨ぎ補助候補PDF{internal_reference_only_count}件を表示対象外"
        receipt_scope_summary += "）"
        reconciliation_memo = (
            f"【照合結果】明細月{statement.period_month:%Y-%m}（参照領収書月{target_receipt_month_label}）の"
            f"{receipt_scope_summary}を、ファイル名や利用者ではなくPDF本文の取引構成要素で照合しました。"
            f"直接一致{direct_count}件、返金書内の元決済{original_count}件、紐付返金相殺{linked_count}件、"
            f"法人カード単位の後日返金相殺{merchant_net_count}件、推定対応{inferred_count}件、解析要確認{review_count}件、未一致{missing_count}件、"
            f"明細未使用の対象月・関連月跨ぎPDF{len(unmatched_receipt_snapshot)}件です。"
            f"通常取引は金額・通貨完全一致、請求元一致または既知の決済名義対応、利用日±{DATE_MATCH_TOLERANCE_DAYS}日を全体最適化で一対一割当し、"
            "明示Invoice/Transaction IDが同じ重複書類は1取引として扱っています。通常照合では利用者特定を条件に含めません。"
            "返品行は同一請求元・同一利用日の元決済を参照し、同一法人カードの後日返金は最大45日後まで厳密な金額一致で全体最適割当します。"
            "直接一致しない明細についてのみ、当月の契約変更書類に明記されたBill to利用者・旧プラン終了情報と、"
            "過去領収書または前月カード明細の同一請求元・金額通貨完全一致・前月同請求日の実績が揃う場合に限り、"
            "管理者確認前の推定対応として提示します。前月カード明細は領収書提出証拠ではなく、旧プランの請求周期を裏付ける補助証拠です。"
            "未使用一覧は当月書類と実明細に関連する月跨ぎ書類だけを対象とし、照合候補として読み込んだだけの無関係な過去月PDFは表示しません。"
        )
        if reconciliation.deduplicated_component_keys:
            reconciliation_memo += f" 重複証拠{len(reconciliation.deduplicated_component_keys)}件を二重計上から除外しました。"
        if no_usage_conflicts:
            reconciliation_memo += " " + " ".join(dict.fromkeys(no_usage_conflicts))
        statement.ai_admin_memo = " ".join(part for part in (extraction_memo, reconciliation_memo) if part)[:5000]
        statement.unmatched_receipt_components = unmatched_receipt_snapshot
        statement.reconciled_at = timezone.now()
        statement.save(
            update_fields=[
                "status", "ai_admin_memo", "unmatched_receipt_components",
                "reconciled_at", "updated_at",
            ]
        )
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
