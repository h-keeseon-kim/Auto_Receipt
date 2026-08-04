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
    ReceiptAdminReviewStatus,
    ReceiptFilenameStatus,
    ReceiptPeriodCheckStatus,
    RegisteredService,
    ServiceCatalog,
    StatementCandidateGateStatus,
    StatementCandidatePriorityTier,
    StatementCandidateStrength,
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


# v1.10.0: 候補は「必須条件ゲート → 優先順位 → 同順位内スコア」の順で評価する。
# スコアが高くても、明確な矛盾がある候補は曖昧候補へ昇格させない。
MAX_STORED_COMPATIBLE_CANDIDATES = 5
MAX_STORED_REJECTED_CANDIDATES = 3
CANDIDATE_TIE_MARGIN = 10
HIGH_CONFIDENCE_CATALOG_GATE = 0.90
CARD_LAST4_MATCH_SCORE = 40

CARD_LAST4_STATUS_MATCHED = "matched"
CARD_LAST4_STATUS_MISSING = "missing"
CARD_LAST4_STATUS_MISMATCHED = "mismatched"
CARD_LAST4_STATUS_ADMIN_CONFIRMED = "admin_confirmed"

AMOUNT_EXACT = "exact"
AMOUNT_MISSING = "missing"
AMOUNT_CONFLICT = "conflict"

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


def reconcile_pending_card_statement_month_semantics(*, period_month=None, statement_id=None) -> int:
    """月次ルールまたは照合ルール更新対象の既存明細を、保存済み行だけで一度だけ再照合する。"""

    queryset = CardStatement.objects.filter(
        Q(ai_admin_memo__contains=CARD_STATEMENT_MONTH_SEMANTICS_RECONCILE_MARKER)
        | Q(ai_admin_memo__contains=CARD_STATEMENT_MATCHING_RULES_RECONCILE_MARKER)
        | Q(ai_admin_memo__contains=CARD_LAST4_EVIDENCE_RECONCILE_MARKER)
        | Q(ai_admin_memo__contains=EXACT_AMOUNT_MATCHING_RECONCILE_MARKER)
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
    """サービス・払先の関係を、一般語だけの一致を除外して厳格に判定する。"""

    left = _normalize_text(first)
    right = _normalize_text(second)
    if not left or not right:
        return False
    if left == right and len(left) >= 3:
        return True
    # 短い一般語（AI / API 等）の部分一致は許可しない。
    if min(len(left), len(right)) >= 5 and (left in right or right in left):
        return True
    left_tokens = _identity_tokens(first)
    right_tokens = _identity_tokens(second)
    return bool(left_tokens and right_tokens and left_tokens.intersection(right_tokens))


def _catalog_alias_values(catalog: ServiceCatalog | None) -> list[str]:
    if catalog is None:
        return []
    raw_values = [catalog.name]
    raw_values.extend(re.split(r"[,;\n]+", catalog.merchant_aliases or ""))
    return [item.strip() for item in raw_values if item and item.strip()]


def _merchant_matches_catalog(merchant: str, catalog: ServiceCatalog | None) -> bool:
    if not merchant or catalog is None:
        return False
    return any(_text_related(merchant, alias) for alias in _catalog_alias_values(catalog))


def _catalog_match_strength(value: str, catalog: ServiceCatalog) -> int:
    """請求名義とサービスマスターの一致強度。

    長い固有請求名義の完全一致を、短い会社名の部分一致より優先する。
    例: OPENAI *CHATGPT は ChatGPT を優先し、単なる OPENAI 部分一致でAPIへ広げない。
    """

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


def _confirmed_statement_catalog_ids(item: CardStatementItem, catalogs: list[ServiceCatalog]) -> set[int]:
    """明細のご利用先から確認できるサービス集合。

    OpenAIの一次候補は、高信頼度のmatchedだけを補助証拠として使う。
    ambiguousや低信頼度の候補は、サービス・利用者の自動確定には使わない。
    """

    catalog_ids = _catalog_ids_for_text(item.merchant_name, catalogs)
    if (
        not catalog_ids
        and item.matched_catalog_service_id
        and item.match_status == StatementMatchStatus.MATCHED
        and item.match_confidence >= HIGH_CONFIDENCE_CATALOG_GATE
    ):
        catalog_ids.add(item.matched_catalog_service_id)
    return catalog_ids


def _receipt_identity_catalog_ids(receipt: Receipt, catalogs: list[ServiceCatalog]) -> set[int]:
    """領収書自身から確認できるサービス集合。

    実際の払先を優先しつつ、ユーザーが選択した登録サービスも候補の身元として保持する。
    双方が異なる場合は自動照合せず、管理者確認または除外へ送る。
    """

    catalog_ids = _catalog_ids_for_text(receipt.ai_extracted_payee, catalogs)
    if receipt.service_id and receipt.service and receipt.service.catalog_service_id:
        catalog_ids.add(receipt.service.catalog_service_id)
    return catalog_ids


def _receipt_has_explicit_service_conflict(receipt: Receipt) -> bool:
    if receipt.ai_check_service_payee_related:
        return False
    memo = " ".join(
        part
        for part in (
            receipt.ai_service_payee_check_memo,
            receipt.ai_resubmission_recommendation_memo,
        )
        if part
    )
    conflict_phrases = (
        "一致していません",
        "関連していません",
        "関連していない",
        "内容が一致していません",
    )
    return receipt.ai_resubmission_recommended and any(phrase in memo for phrase in conflict_phrases)


def _receipt_has_explicit_recipient_conflict(receipt: Receipt) -> bool:
    if receipt.ai_check_recipient_name:
        return False
    memo = " ".join(
        part
        for part in (
            receipt.ai_recipient_name_check_memo,
            receipt.ai_resubmission_recommendation_memo,
        )
        if part
    )
    return receipt.ai_resubmission_recommended and (
        "利用者名" in memo or "宛名" in memo
    ) and any(phrase in memo for phrase in ("一致していません", "異なります", "別の"))


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
        errors.append(
            f"AI判定明細月が選択月{expected_period}ではなく{statement.statement_period}です。"
        )
    return errors


def _amounts_equal(left: Decimal | None, right: Decimal | None) -> bool:
    """通貨単位を含む金額を、許容差なしで比較する。

    Decimalでは ``22.00 == 22.000`` はTrueになる一方、``22.00`` と
    ``22.01``、``3595`` と ``3596`` は不一致になる。為替差・丸め差・
    手数料差をReceiptHub側で推測して吸収しない。
    """

    if left is None or right is None:
        return False
    return left == right


@dataclass(frozen=True)
class AmountAssessment:
    status: str
    basis: str = ""
    currency_match: bool = False
    memo: str = ""

    @property
    def exact(self) -> bool:
        return self.status == AMOUNT_EXACT

    @property
    def matched(self) -> bool:
        return self.status == AMOUNT_EXACT


@dataclass(frozen=True)
class CardLast4Evidence:
    status: str
    score: int
    blocks_auto_match: bool
    label: str

    @property
    def matched(self) -> bool:
        return self.status == CARD_LAST4_STATUS_MATCHED


@dataclass(frozen=True)
class ReceiptCandidateEvaluation:
    item: CardStatementItem
    receipt: Receipt
    score: int
    confidence: float
    strength: str
    gate_status: str
    priority_tier: str
    gate_memo: str
    amount_match: bool
    amount_match_basis: str
    currency_match: bool
    merchant_match: bool
    service_match: bool
    date_match: bool
    exact_amount: bool
    date_distance_days: int | None
    card_last4_match: bool
    card_last4_status: str
    card_last4_blocks_auto_match: bool
    rationale: str

    @property
    def can_auto_match(self) -> bool:
        return self.gate_status == StatementCandidateGateStatus.AUTO_ELIGIBLE

    @property
    def is_rejected(self) -> bool:
        return self.gate_status == StatementCandidateGateStatus.REJECTED

    @property
    def auto_priority(self) -> int:
        priorities = {
            StatementCandidatePriorityTier.EXACT_IDENTITY: 2,
            StatementCandidatePriorityTier.EXACT_AMOUNT_ONLY: 1,
            StatementCandidatePriorityTier.REJECTED: 0,
        }
        return priorities.get(self.priority_tier, 0)

    @property
    def sort_key(self):
        # 優先順位を必ずスコアより先にする。低い層が加点だけで上位層を逆転しない。
        date_rank = -self.date_distance_days if self.date_distance_days is not None else -9999
        return (
            self.auto_priority,
            self.score,
            self.exact_amount,
            self.merchant_match,
            self.service_match,
            date_rank,
            -self.receipt.pk,
        )


def _assess_amount(item: CardStatementItem, receipt: Receipt) -> AmountAssessment:
    """明細行と領収書の金額を完全一致だけで評価する。

    - 外貨は、明細の外貨金額と同一通貨の領収書金額を比較する。
    - 領収書がJPYの場合は明細の円金額と比較する。
    - 1セント、1円でも異なれば不一致。近似・丸め・為替差の許容はしない。
    - 金額または通貨を抽出できない領収書は、自動・曖昧候補にしない。
    """

    receipt_amount = receipt.amount
    receipt_currency = (receipt.currency or "").upper()
    statement_currency = (item.original_currency or "").upper()

    if receipt_amount is None or not receipt_currency:
        return AmountAssessment(
            AMOUNT_MISSING,
            memo="領収書の金額または通貨を確認できないため、照合候補にできません。",
        )

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
                AMOUNT_CONFLICT,
                memo=(
                    f"通貨が一致しません（明細: {statement_currency or '不明'} / "
                    f"領収書: {receipt_currency or '不明'}）。"
                ),
            )
    elif item.amount_jpy is not None and receipt_currency == "JPY":
        comparison_amount = item.amount_jpy
        basis = "jpy"
    else:
        return AmountAssessment(
            AMOUNT_MISSING,
            memo="明細と領収書で比較可能な金額・通貨の組み合わせがないため、照合候補にできません。",
        )

    if _amounts_equal(receipt_amount, comparison_amount):
        label = (
            f"外貨金額 {comparison_amount} {receipt_currency} が完全一致"
            if basis == "original"
            else f"円金額 {comparison_amount}円が完全一致"
        )
        return AmountAssessment(AMOUNT_EXACT, basis=basis, currency_match=True, memo=label)

    return AmountAssessment(
        AMOUNT_CONFLICT,
        basis=basis,
        currency_match=True,
        memo=(
            f"金額が完全一致しません（明細: {comparison_amount} {receipt_currency} / "
            f"領収書: {receipt_amount} {receipt_currency}）。許容差は設定していません。"
        ),
    )


def _date_evidence(item: CardStatementItem, receipt: Receipt) -> tuple[bool, int | None, int, str]:
    if not receipt.issued_on or not item.transaction_date:
        return False, None, 0, ""
    day_delta = abs((receipt.issued_on - item.transaction_date).days)
    if day_delta == 0:
        return True, day_delta, 20, "日付一致"
    if day_delta <= 3:
        return True, day_delta, 12, f"日付差{day_delta}日"
    if (receipt.issued_on.year, receipt.issued_on.month) == (
        item.transaction_date.year,
        item.transaction_date.month,
    ):
        return True, day_delta, 4, "同月"
    return False, day_delta, 0, ""


def _evaluate_receipt_card_last4(receipt: Receipt) -> CardLast4Evidence:
    """領収書上のカード末尾を補助証拠として評価する。

    - 7210一致: 同じ候補区分内の補助スコアを加点
    - 記載なし・読取不可: 0点の中立。候補除外も自動照合停止も行わない
    - 明確な別番号: 候補は残すが自動確定せず、管理者確認へ送る
    - 管理者確認済み: 人の判断を優先し、自動照合停止を解除

    ご利用代金明細書そのもののカード末尾確認とは別である。明細書側は
    対象Pカードの明細かを判定するため、従来どおり statement 単位で確認する。
    """

    expected_last4 = str(getattr(settings, "RECEIPT_CARD_LAST4", "7210"))[-4:]
    extracted_last4 = "".join(
        char for char in (receipt.ai_extracted_card_last4 or "") if char.isdigit()
    )[-4:]
    if not extracted_last4:
        return CardLast4Evidence(
            status=CARD_LAST4_STATUS_MISSING,
            score=0,
            blocks_auto_match=False,
            label="カード末尾記載なし（減点・除外なし）",
        )
    if extracted_last4 == expected_last4:
        return CardLast4Evidence(
            status=CARD_LAST4_STATUS_MATCHED,
            score=CARD_LAST4_MATCH_SCORE,
            blocks_auto_match=False,
            label=f"カード末尾{expected_last4}一致（補助+{CARD_LAST4_MATCH_SCORE}）",
        )
    if receipt.admin_review_status == ReceiptAdminReviewStatus.CONFIRMED:
        return CardLast4Evidence(
            status=CARD_LAST4_STATUS_ADMIN_CONFIRMED,
            score=0,
            blocks_auto_match=False,
            label=(
                f"カード末尾差異を管理者確認済み（対象 {expected_last4} / 領収書 {extracted_last4}）"
            ),
        )
    return CardLast4Evidence(
        status=CARD_LAST4_STATUS_MISMATCHED,
        score=0,
        blocks_auto_match=True,
        label=(
            f"カード末尾要確認（対象 {expected_last4} / 領収書 {extracted_last4}）。"
            "必須条件ではないため候補から除外しませんが、自動確定は行いません"
        ),
    )


def _evaluate_receipt_candidate(
    item: CardStatementItem,
    receipt: Receipt,
    *,
    catalogs: list[ServiceCatalog],
    target_receipt_month: date,
    statement_gate_errors: tuple[str, ...] = (),
) -> ReceiptCandidateEvaluation | None:
    """候補を必須条件で選別し、その後に優先順位・スコアを付与する。"""

    receipt_catalog = receipt.service.catalog_service if receipt.service_id and receipt.service else None
    receipt_catalog_id = receipt_catalog.pk if receipt_catalog else None
    amount = _assess_amount(item, receipt)
    card_last4 = _evaluate_receipt_card_last4(receipt)

    merchant_payee_match = bool(
        receipt.ai_extracted_payee and _text_related(item.merchant_name, receipt.ai_extracted_payee)
    )
    statement_catalog_ids = _confirmed_statement_catalog_ids(item, catalogs)
    receipt_payee_catalog_ids = _catalog_ids_for_text(receipt.ai_extracted_payee, catalogs)
    receipt_identity_catalog_ids = _receipt_identity_catalog_ids(receipt, catalogs)
    catalog_exact = bool(receipt_catalog_id and receipt_catalog_id in statement_catalog_ids)
    merchant_catalog_match = bool(receipt_catalog and _merchant_matches_catalog(item.merchant_name, receipt_catalog))
    service_name_match = bool(receipt.service_id and _text_related(item.merchant_name, receipt.service.name))
    extra_memo_match = bool(receipt.is_extra and receipt.memo and _text_related(item.merchant_name, receipt.memo))

    identity_overlap = bool(statement_catalog_ids and receipt_identity_catalog_ids.intersection(statement_catalog_ids))
    merchant_match = merchant_payee_match or identity_overlap
    service_match = catalog_exact or merchant_catalog_match or service_name_match or extra_memo_match or identity_overlap
    identity_match = merchant_match or service_match

    hard_conflicts: list[str] = list(statement_gate_errors)

    if receipt.ai_period_check_status == ReceiptPeriodCheckStatus.MISMATCHED:
        hard_conflicts.append("対象領収書月が明細の対象月と一致しません。")
    elif receipt.issued_on and (receipt.issued_on.year, receipt.issued_on.month) != (
        target_receipt_month.year,
        target_receipt_month.month,
    ):
        hard_conflicts.append(
            f"領収書日付が対象領収書月 {target_receipt_month:%Y-%m} ではありません。"
        )

    if amount.status in {AMOUNT_CONFLICT, AMOUNT_MISSING}:
        hard_conflicts.append(amount.memo)

    # 領収書上の払先と、ユーザーが選択した登録サービスを双方で特定できる場合の内部矛盾。
    if receipt_payee_catalog_ids and receipt_catalog_id and receipt_catalog_id not in receipt_payee_catalog_ids:
        hard_conflicts.append("領収書上の払先と、領収書に指定された登録サービスが明確に異なります。")

    # 双方の請求元・サービスを独立に特定でき、その集合が交わらない場合は明確な不一致。
    if statement_catalog_ids and receipt_identity_catalog_ids and not statement_catalog_ids.intersection(
        receipt_identity_catalog_ids
    ):
        hard_conflicts.append("明細のご利用先と領収書の払先・サービスが明確に異なります。")

    # 領収書自身の登録サービスと払先が明確に矛盾している場合は、スコア候補にしない。
    if _receipt_has_explicit_service_conflict(receipt):
        hard_conflicts.append("領収書の登録サービスと実際の払先が明確に一致していません。")
    if _receipt_has_explicit_recipient_conflict(receipt):
        hard_conflicts.append("領収書の利用者名（宛名）が対象ユーザーと明確に一致していません。")

    # AIが領収書のサービス・払先関係を確認済みで、明細側の候補集合にそのサービスがない場合も除外。
    if (
        statement_catalog_ids
        and receipt_catalog_id
        and receipt.ai_check_service_payee_related
        and receipt_catalog_id not in statement_catalog_ids
        and not merchant_payee_match
    ):
        conflict = "明細のご利用先と、領収書で確認済みのサービスが一致しません。"
        if conflict not in hard_conflicts:
            hard_conflicts.append(conflict)

    date_match, date_distance_days, date_score, date_label = _date_evidence(item, receipt)

    # 何の接点もない候補は保存しない。明確な矛盾は、金額一致またはサービス関連がある場合だけ監査用に残す。
    has_candidate_signal = amount.matched or identity_match
    if hard_conflicts and not has_candidate_signal:
        return None

    score = card_last4.score
    reasons: list[str] = [card_last4.label]
    if amount.status == AMOUNT_EXACT:
        score += 100
        reasons.append(amount.memo)
    if amount.currency_match:
        score += 10
        reasons.append("通貨一致")
    if merchant_payee_match:
        score += 50
        reasons.append("明細のご利用先と領収書の払先が関連")
    if identity_overlap:
        score += 50
        reasons.append("ご利用先と払先のサービス候補が一致")
    if catalog_exact:
        score += 45
        reasons.append("AI候補サービスと領収書サービスが一致")
    elif merchant_catalog_match:
        score += 40
        reasons.append("サービスマスターの払先候補と関連")
    elif service_name_match:
        score += 30
        reasons.append("サービス名と関連")
    if extra_memo_match:
        score += 25
        reasons.append("その他メモと関連")
    if date_label:
        score += date_score
        reasons.append(date_label)
    if receipt.admin_review_status == ReceiptAdminReviewStatus.CONFIRMED:
        score += 5
        reasons.append("管理者確認済み領収書")
    elif receipt.ai_filename_status == ReceiptFilenameStatus.GENERATED:
        score += 2

    if hard_conflicts:
        return ReceiptCandidateEvaluation(
            item=item,
            receipt=receipt,
            score=score,
            confidence=0.0,
            strength=StatementCandidateStrength.POSSIBLE,
            gate_status=StatementCandidateGateStatus.REJECTED,
            priority_tier=StatementCandidatePriorityTier.REJECTED,
            gate_memo=" ".join(dict.fromkeys(hard_conflicts)),
            amount_match=amount.matched,
            amount_match_basis=amount.basis,
            currency_match=amount.currency_match,
            merchant_match=merchant_match,
            service_match=service_match,
            date_match=date_match,
            exact_amount=amount.exact,
            date_distance_days=date_distance_days,
            card_last4_match=card_last4.matched,
            card_last4_status=card_last4.status,
            card_last4_blocks_auto_match=card_last4.blocks_auto_match,
            rationale="、".join(reasons),
        )

    manual_constraints: list[str] = []
    if card_last4.blocks_auto_match:
        manual_constraints.append(card_last4.label)
    if (
        receipt.ai_extracted_recipient_name
        and not receipt.ai_check_recipient_name
        and receipt.admin_review_status != ReceiptAdminReviewStatus.CONFIRMED
    ):
        manual_constraints.append("利用者名（宛名）が未確認です。")
    if receipt.ai_resubmission_recommended and receipt.admin_review_status != ReceiptAdminReviewStatus.CONFIRMED:
        manual_constraints.append("領収書AI検査で再提出候補になっています。")

    if amount.status == AMOUNT_EXACT and identity_match:
        priority_tier = StatementCandidatePriorityTier.EXACT_IDENTITY
        strength = StatementCandidateStrength.STRONG
        confidence = 0.98 if merchant_match and service_match else 0.95
        gate_status = StatementCandidateGateStatus.AUTO_ELIGIBLE
        gate_memo = "金額・通貨が完全一致し、サービスまたは払先の関連も確認できました。"
    elif amount.status == AMOUNT_EXACT:
        priority_tier = StatementCandidatePriorityTier.EXACT_AMOUNT_ONLY
        strength = StatementCandidateStrength.AMOUNT_ONLY
        confidence = 0.78 if date_match else 0.72
        gate_status = StatementCandidateGateStatus.AUTO_ELIGIBLE
        gate_memo = "金額・通貨は完全一致していますが、ご利用先・払先・サービスの関連は未確認です。"
    else:
        # 金額・通貨の完全一致を確認できない領収書は、曖昧候補へ入れない。
        return None

    if manual_constraints:
        gate_status = StatementCandidateGateStatus.MANUAL_ONLY
        gate_memo = " ".join([gate_memo, *manual_constraints])
        confidence = min(confidence, 0.60)

    return ReceiptCandidateEvaluation(
        item=item,
        receipt=receipt,
        score=score,
        confidence=confidence,
        strength=strength,
        gate_status=gate_status,
        priority_tier=priority_tier,
        gate_memo=gate_memo,
        amount_match=amount.matched,
        amount_match_basis=amount.basis,
        currency_match=amount.currency_match,
        merchant_match=merchant_match,
        service_match=service_match,
        date_match=date_match,
        exact_amount=amount.exact,
        date_distance_days=date_distance_days,
        card_last4_match=card_last4.matched,
        card_last4_status=card_last4.status,
        card_last4_blocks_auto_match=card_last4.blocks_auto_match,
        rationale="、".join(reasons),
    )


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
    catalogs: list[ServiceCatalog],
    target_receipt_month: date,
) -> Receipt | None:
    candidates = [
        receipt for receipt in receipts if receipt.pk not in used_receipt_ids and receipt.service_id == service.pk
    ]
    if not candidates:
        return None
    evaluations = [
        evaluation
        for receipt in candidates
        if (
            evaluation := _evaluate_receipt_candidate(
                item,
                receipt,
                catalogs=catalogs,
                target_receipt_month=target_receipt_month,
            )
        )
        is not None
        and not evaluation.is_rejected
    ]
    if evaluations:
        return max(evaluations, key=lambda evaluation: evaluation.sort_key).receipt
    return None


def _base_match_memo(value: str) -> str:
    result = (value or "").strip()
    for marker in ("【領収書照合】", "【自動照合】", "【必須条件】"):
        result = result.split(marker, 1)[0].strip()
    return result


def _candidate_note(evaluation: ReceiptCandidateEvaluation) -> str:
    if evaluation.priority_tier == StatementCandidatePriorityTier.EXACT_AMOUNT_ONLY:
        return (
            f"【領収書照合】金額と通貨が完全かつ一意に一致した候補「{evaluation.receipt.display_filename}」を"
            "提出済み領収書として割り当てました。ご利用先・払先・サービスの関連は未確認のため、管理者確認対象です。"
        )
    return (
        f"【領収書照合】候補「{evaluation.receipt.display_filename}」を自動照合しました。"
        f"根拠: {evaluation.rationale or '必須条件と複数項目の一致'}。"
    )


def _candidate_tie(candidates: list[ReceiptCandidateEvaluation], *, margin: int = CANDIDATE_TIE_MARGIN) -> bool:
    if len(candidates) < 2:
        return False
    first, second = candidates[0], candidates[1]
    if first.priority_tier != second.priority_tier:
        return False
    # 同額でも日付差が明確なら、近い候補を優先できる。
    if first.date_distance_days is not None and second.date_distance_days is not None:
        if abs(first.date_distance_days - second.date_distance_days) >= 2:
            return False
    return first.score - second.score < margin


def _persist_candidates(
    statement: CardStatement,
    items: list[CardStatementItem],
    compatible_by_item: dict[int, list[ReceiptCandidateEvaluation]],
    rejected_by_item: dict[int, list[ReceiptCandidateEvaluation]],
) -> tuple[int, int]:
    CardStatementMatchCandidate.objects.filter(item__statement=statement).delete()
    candidate_rows: list[CardStatementMatchCandidate] = []
    compatible_count = 0
    rejected_count = 0
    for item in items:
        compatible = compatible_by_item.get(item.pk, [])[:MAX_STORED_COMPATIBLE_CANDIDATES]
        rejected = rejected_by_item.get(item.pk, [])[:MAX_STORED_REJECTED_CANDIDATES]
        compatible_count += len(compatible)
        rejected_count += len(rejected)
        for rank, evaluation in enumerate([*compatible, *rejected], start=1):
            candidate_rows.append(
                CardStatementMatchCandidate(
                    item=item,
                    receipt=evaluation.receipt,
                    rank=rank,
                    score=evaluation.score,
                    confidence=evaluation.confidence,
                    strength=evaluation.strength,
                    gate_status=evaluation.gate_status,
                    priority_tier=evaluation.priority_tier,
                    gate_memo=evaluation.gate_memo,
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
    return compatible_count, rejected_count


def reconcile_card_statement_items(statement_id: int, *, preserve_manual: bool = True) -> CardStatement:
    """明細行と領収書を、必須条件・優先順位・同順位スコアの順で一対一照合する。"""

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
    target_receipt_month = receipt_month_for_statement(statement.period_month)
    catalogs = list(ServiceCatalog.objects.all().order_by("pk"))
    statement_gate_errors = tuple(_statement_gate_errors(statement))

    services_by_catalog: dict[int, list[RegisteredService]] = defaultdict(list)
    for service in services:
        if service.catalog_service_id:
            services_by_catalog[service.catalog_service_id].append(service)

    used_receipt_ids: set[int] = set()
    manual_items: set[int] = set()

    # 管理者が明示的に確定した行は維持する。
    for item in items:
        if preserve_manual and _is_manual_override(item):
            manual_items.add(item.pk)
            item.match_reason_code = (
                StatementMatchReason.IGNORED
                if item.match_status == StatementMatchStatus.IGNORED
                else StatementMatchReason.MANUAL_CONFIRMED
            )
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
                    catalogs=catalogs,
                    target_receipt_month=target_receipt_month,
                )
                item.matched_receipt = chosen
                if chosen:
                    used_receipt_ids.add(chosen.pk)

    compatible_by_item: dict[int, list[ReceiptCandidateEvaluation]] = {}
    rejected_by_item: dict[int, list[ReceiptCandidateEvaluation]] = {}
    for item in items:
        compatible: list[ReceiptCandidateEvaluation] = []
        rejected: list[ReceiptCandidateEvaluation] = []
        for receipt in receipts:
            evaluation = _evaluate_receipt_candidate(
                item,
                receipt,
                catalogs=catalogs,
                target_receipt_month=target_receipt_month,
                statement_gate_errors=statement_gate_errors,
            )
            if evaluation is None:
                continue
            if evaluation.is_rejected:
                rejected.append(evaluation)
            else:
                compatible.append(evaluation)
        compatible.sort(key=lambda candidate: candidate.sort_key, reverse=True)
        rejected.sort(key=lambda candidate: (candidate.score, -candidate.receipt.pk), reverse=True)
        compatible_by_item[item.pk] = compatible
        rejected_by_item[item.pk] = rejected

    assigned_item_ids: set[int] = set()
    ambiguous_item_reasons: dict[int, tuple[str, str]] = {}

    def assign(item: CardStatementItem, candidate: ReceiptCandidateEvaluation):
        receipt = candidate.receipt
        item.matched_receipt = receipt
        item.matched_user = receipt.submission.user
        item.matched_service = receipt.service
        if receipt.service_id and receipt.service.catalog_service_id:
            item.matched_catalog_service = receipt.service.catalog_service
        if candidate.priority_tier == StatementCandidatePriorityTier.EXACT_AMOUNT_ONLY:
            item.match_status = StatementMatchStatus.AMBIGUOUS
            item.match_reason_code = StatementMatchReason.AUTO_AMOUNT_ONLY
        else:
            item.match_status = StatementMatchStatus.MATCHED
            item.match_reason_code = StatementMatchReason.AUTO_STRONG
        item.match_confidence = max(candidate.confidence, item.match_confidence)
        base_memo = _base_match_memo(item.match_memo)
        item.match_memo = " ".join(part for part in (base_memo, _candidate_note(candidate)) if part).strip()
        used_receipt_ids.add(receipt.pk)
        assigned_item_ids.add(item.pk)

    # 自動照合対象だけを、一対一かつ双方で一意な場合に割り当てる。
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

        item_candidates: dict[int, list[ReceiptCandidateEvaluation]] = {}
        receipt_candidates: dict[int, list[ReceiptCandidateEvaluation]] = defaultdict(list)
        for item_id in remaining_item_ids:
            candidates = [
                candidate
                for candidate in compatible_by_item.get(item_id, [])
                if candidate.can_auto_match and candidate.receipt.pk in available_receipt_ids
            ]
            candidates.sort(key=lambda candidate: candidate.sort_key, reverse=True)
            item_candidates[item_id] = candidates
            for candidate in candidates:
                receipt_candidates[candidate.receipt.pk].append(candidate)
        for candidates in receipt_candidates.values():
            candidates.sort(key=lambda candidate: candidate.sort_key, reverse=True)

        mutual_best: list[ReceiptCandidateEvaluation] = []
        for item_id, candidates in item_candidates.items():
            if not candidates:
                continue
            if _candidate_tie(candidates):
                ambiguous_item_reasons[item_id] = (
                    StatementMatchReason.MULTIPLE_COMPATIBLE,
                    "必須条件を満たす領収書候補が複数あるため、自動では確定していません。",
                )
                continue
            top = candidates[0]
            reverse_candidates = receipt_candidates.get(top.receipt.pk, [])
            if not reverse_candidates or reverse_candidates[0].item.pk != item_id or _candidate_tie(reverse_candidates):
                ambiguous_item_reasons[item_id] = (
                    StatementMatchReason.RECEIPT_COMPETITION,
                    "同じ領収書が複数の明細行で適合候補になっているため、自動確定していません。",
                )
                continue
            mutual_best.append(top)

        mutual_best.sort(key=lambda candidate: candidate.sort_key, reverse=True)
        for candidate in mutual_best:
            if candidate.item.pk in assigned_item_ids or candidate.receipt.pk in used_receipt_ids:
                continue
            assign(candidate.item, candidate)
            progress = True

    no_usage_conflicts: list[str] = []
    missing_count = 0
    manual_review_count = 0

    for item in items:
        if item.pk not in manual_items and item.pk not in assigned_item_ids:
            item.matched_receipt = None
            item.matched_service = None
            item.matched_user = None
            base_memo = _base_match_memo(item.match_memo)
            compatible = compatible_by_item.get(item.pk, [])
            rejected = rejected_by_item.get(item.pk, [])
            confirmed_catalog_ids = _confirmed_statement_catalog_ids(item, catalogs)
            candidate_services = []
            if len(confirmed_catalog_ids) == 1:
                candidate_services = services_by_catalog.get(next(iter(confirmed_catalog_ids)), [])
            ambiguity = ambiguous_item_reasons.get(item.pk)

            if statement_gate_errors and item.receipt_required:
                item.match_status = StatementMatchStatus.UNMATCHED
                item.match_reason_code = StatementMatchReason.NO_COMPATIBLE_RECEIPT
                item.match_confidence = 0
                note = "【必須条件】" + " ".join(statement_gate_errors)
                item.match_memo = " ".join(part for part in (base_memo, note) if part).strip()
            elif ambiguity:
                reason_code, reason_text = ambiguity
                item.match_status = StatementMatchStatus.AMBIGUOUS
                item.match_reason_code = reason_code
                if compatible:
                    item.match_confidence = max(item.match_confidence, compatible[0].confidence)
                note = f"【領収書照合】{reason_text} 候補一覧から管理者が確認してください。"
                item.match_memo = " ".join(part for part in (base_memo, note) if part).strip()
            elif compatible and item.receipt_required:
                item.match_status = StatementMatchStatus.AMBIGUOUS
                item.match_reason_code = StatementMatchReason.INSUFFICIENT_EVIDENCE
                item.match_confidence = max(item.match_confidence, compatible[0].confidence)
                note = (
                    f"【領収書照合】必須条件の一部が不足する手動確認候補を{len(compatible)}件作成しました。"
                    "スコアだけでは自動確定せず、候補一覧から確認してください。"
                )
                item.match_memo = " ".join(part for part in (base_memo, note) if part).strip()
            elif len(candidate_services) == 1:
                item.matched_service = candidate_services[0]
                item.matched_user = candidate_services[0].user
                item.matched_catalog_service = candidate_services[0].catalog_service
                item.match_status = StatementMatchStatus.MATCHED
                item.match_reason_code = StatementMatchReason.SERVICE_ONLY
                note = "【領収書照合】サービスと利用者は特定できましたが、適合する提出済み領収書はありません。"
                if rejected:
                    note += f" 必須条件不一致の候補{len(rejected)}件は照合対象から除外しました。"
                item.match_memo = " ".join(part for part in (base_memo, note) if part).strip()
            elif len(candidate_services) > 1:
                item.match_status = StatementMatchStatus.AMBIGUOUS
                item.match_reason_code = StatementMatchReason.USER_AMBIGUOUS
                note = "【領収書照合】同じサービスを複数ユーザーが利用しているため、未提出ユーザーを自動特定できません。"
                item.match_memo = " ".join(part for part in (base_memo, note) if part).strip()
            elif item.receipt_required:
                item.match_status = StatementMatchStatus.UNMATCHED
                item.match_reason_code = StatementMatchReason.NO_COMPATIBLE_RECEIPT
                if rejected:
                    rejection_summary = " ".join(dict.fromkeys(candidate.gate_memo for candidate in rejected[:2]))
                    note = (
                        f"【必須条件】金額一致等の接点はありましたが、明確な矛盾がある候補{len(rejected)}件を除外しました。"
                        f" {rejection_summary}"
                    )
                else:
                    note = "【領収書照合】必須条件を満たす提出済み領収書候補はありません。"
                item.match_memo = " ".join(part for part in (base_memo, note) if part).strip()
            else:
                item.match_reason_code = StatementMatchReason.IGNORED
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
                    "match_reason_code",
                    "match_confidence",
                    "match_memo",
                ]
            )
        compatible_count, rejected_count = _persist_candidates(
            statement,
            items,
            compatible_by_item,
            rejected_by_item,
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
        extraction_memo = (statement.ai_admin_memo or "").split("【照合結果】", 1)[0]
        extraction_memo = extraction_memo.replace(
            CARD_STATEMENT_MONTH_SEMANTICS_RECONCILE_MARKER,
            "",
        ).replace(
            CARD_STATEMENT_MATCHING_RULES_RECONCILE_MARKER,
            "",
        ).replace(
            CARD_LAST4_EVIDENCE_RECONCILE_MARKER,
            "",
        ).replace(
            EXACT_AMOUNT_MATCHING_RECONCILE_MARKER,
            "",
        ).strip()
        reconciliation_memo = (
            f"【照合結果】明細月 {statement.period_month:%Y-%m} "
            f"（対象領収書月 {target_receipt_month:%Y-%m} / 提出月 {statement.period_month:%Y-%m}）の"
            f"全ユーザー領収書{len(receipts)}件を、金額・通貨の完全一致（許容差なし）→必須条件ゲート→"
            f"優先順位→同順位内スコアの順で照合し、領収書のカード末尾は一致時のみ補助+"
            f"{CARD_LAST4_MATCH_SCORE}点（記載なしは中立）として、"
            f"適合候補{compatible_count}件、必須条件不一致で除外{rejected_count}件、"
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
