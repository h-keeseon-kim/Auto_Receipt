from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Iterable

from django.conf import settings

from .ai_filename import (
    build_file_input_item,
    extract_response_text,
    normalize_card_last4,
    normalize_confidence,
    normalize_currency,
    normalize_payee,
    parse_amount,
    parse_iso_date,
    target_card_last4,
)
from .models import CardStatementStatus, StatementMatchStatus, receipt_month_for_statement


@dataclass(frozen=True)
class StatementAnalysisItem:
    line_reference: str
    transaction_date: date | None
    merchant_name: str
    amount_jpy: Decimal | None
    original_amount: Decimal | None
    original_currency: str
    service_catalog_id: int | None
    match_status: str
    receipt_required: bool
    confidence: float
    reason: str

    @property
    def registered_service_id(self) -> int | None:
        """v1.1.x互換。現在はサービスマスターIDを返す。"""

        return self.service_catalog_id


@dataclass(frozen=True)
class StatementAnalysisResult:
    status: str
    card_last4: str = ""
    statement_period: str = ""
    payment_date: date | None = None
    items: tuple[StatementAnalysisItem, ...] = ()
    admin_memo: str = ""


def statement_ai_enabled() -> bool:
    return bool(
        getattr(settings, "RECEIPT_AI_FILENAME_ENABLED", True)
        and getattr(settings, "OPENAI_API_KEY", "")
        and getattr(settings, "OPENAI_MODEL", "")
    )


def service_payload(catalogs: Iterable[Any]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for catalog in catalogs:
        payload.append(
            {
                "id": catalog.pk,
                "name": catalog.name,
                "billing_type": catalog.get_billing_type_display(),
                "aliases": catalog.merchant_aliases or "",
            }
        )
    return payload


def generate_card_statement_analysis(
    *,
    file_bytes: bytes,
    original_filename: str,
    content_type: str,
    period_month: date,
    service_catalogs: Iterable[Any],
) -> StatementAnalysisResult:
    """明細書の全行を抽出し、サービスマスター候補までAIで判定する。

    明細書はユーザー単位ではなく会社全体のものとして扱う。ユーザーの特定と
    領収書の一対一照合は、抽出後にサーバー側で全ユーザーの領収書を使って行う。
    """

    if not statement_ai_enabled():
        return StatementAnalysisResult(
            status=CardStatementStatus.FAILED,
            admin_memo="カード明細AI解析を実行できません。OPENAI_API_KEY / OPENAI_MODEL / RECEIPT_AI_FILENAME_ENABLED を確認してください。",
        )

    try:
        from openai import OpenAI
    except Exception as exc:  # pragma: no cover
        return StatementAnalysisResult(
            status=CardStatementStatus.FAILED,
            admin_memo=f"OpenAI Python SDKを読み込めませんでした: {exc}",
        )

    catalogs = service_payload(service_catalogs)
    target_month = period_month.strftime("%Y-%m")
    target_receipt_month = receipt_month_for_statement(period_month).strftime("%Y-%m")
    target_last4 = target_card_last4()
    prompt = (
        "このファイルは会社全体で利用している法人クレジットカードのご利用代金明細書です。"
        "特定の1ユーザーの明細ではありません。表にあるすべての利用明細行を漏れなく抽出してください。\n"
        f"管理対象のご利用代金明細月: {target_month}\n"
        f"この明細と照合する対象領収書月: {target_receipt_month}\n"
        f"確認対象カード末尾4桁: {target_last4}\n"
        "statement_period は利用日ではなく、明細書の請求・支払対象月を YYYY-MM で返してください。"
        "例えば利用日が6月で支払日が7月29日なら statement_period は 2026-07 です。"
        "この場合、領収書照合の対象月は前月の2026-06で、ユーザー提出月は2026-07です。\n"
        "各明細について、明細番号、ご利用先、利用日、当月請求金額（円）、外貨金額・通貨を抽出してください。\n"
        "service_catalog_id は次のサービスマスター一覧の id からだけ選びます。"
        "ユーザーはここでは特定しません。サービス名とカード明細の請求名義は完全一致しない場合があります。"
        "ChatGPT と OPENAI *CHATGPT / OPENAI.COM、Claude と CLAUDE.AI / ANTHROPIC.COM のような"
        "運営会社・請求名義の関連も考慮してください。\n"
        f"サービスマスター一覧: {json.dumps(catalogs, ensure_ascii=False)}\n"
        "match_status の基準: matched=サービスマスターと十分に一致、"
        "ambiguous=候補はあるが断定不可、unmatched=登録サービスに一致しない、"
        "ignored=明らかに領収書管理対象外。\n"
        "receipt_required は、ソフトウェア、SaaS、API、オンラインサービス等の請求で領収書確認が必要なら true。"
        "サービスマスターにないが領収書が必要そうな項目も、ambiguous または unmatched とし、"
        "receipt_required=true にして人が確認できるようにしてください。"
        "reason は簡潔にしてください。"
    )

    try:
        client = OpenAI(
            api_key=getattr(settings, "OPENAI_API_KEY", ""),
            timeout=max(float(getattr(settings, "RECEIPT_AI_TIMEOUT", 30)), 60.0),
        )
        response = client.responses.create(
            model=getattr(settings, "OPENAI_MODEL", ""),
            input=[
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "あなたは法人カード明細と、複数ユーザーが提出した前月分領収書を照合する監査補助AIです。"
                                "まず明細表の全行を正確に抽出し、確信できない関係は曖昧として残してください。"
                            ),
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        build_file_input_item(
                            file_bytes=file_bytes,
                            filename=original_filename,
                            content_type=content_type,
                        ),
                        {"type": "input_text", "text": prompt},
                    ],
                },
            ],
            text={"format": statement_schema()},
            max_output_tokens=int(getattr(settings, "STATEMENT_AI_MAX_OUTPUT_TOKENS", 16000)),
        )
        payload = json.loads(extract_response_text(response))
        return build_statement_result_from_payload(
            payload,
            target_month=target_month,
            allowed_catalog_ids={item["id"] for item in catalogs},
        )
    except Exception as exc:
        return StatementAnalysisResult(
            status=CardStatementStatus.FAILED,
            admin_memo=f"OpenAI APIによるカード明細解析に失敗しました: {exc.__class__.__name__}: {exc}",
        )


def statement_schema() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "name": "global_card_statement_extraction",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "card_last4": {"type": ["string", "null"]},
                "statement_period": {"type": ["string", "null"], "description": "明細書の請求・支払月 YYYY-MM"},
                "payment_date": {"type": ["string", "null"], "description": "支払日 YYYY-MM-DD"},
                "summary_reason": {"type": "string"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "line_reference": {"type": "string"},
                            "transaction_date": {"type": ["string", "null"]},
                            "merchant_name": {"type": "string"},
                            "amount_jpy": {"type": ["number", "string", "null"]},
                            "original_amount": {"type": ["number", "string", "null"]},
                            "original_currency": {"type": ["string", "null"]},
                            "service_catalog_id": {"type": ["integer", "null"]},
                            "match_status": {
                                "type": "string",
                                "enum": [
                                    StatementMatchStatus.MATCHED,
                                    StatementMatchStatus.AMBIGUOUS,
                                    StatementMatchStatus.UNMATCHED,
                                    StatementMatchStatus.IGNORED,
                                ],
                            },
                            "receipt_required": {"type": "boolean"},
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            "reason": {"type": "string"},
                        },
                        "required": [
                            "line_reference",
                            "transaction_date",
                            "merchant_name",
                            "amount_jpy",
                            "original_amount",
                            "original_currency",
                            "service_catalog_id",
                            "match_status",
                            "receipt_required",
                            "confidence",
                            "reason",
                        ],
                    },
                },
            },
            "required": ["card_last4", "statement_period", "payment_date", "summary_reason", "items"],
        },
    }


def normalize_period(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) >= 7 and text[4] == "-" and text[:4].isdigit() and text[5:7].isdigit():
        month = int(text[5:7])
        if 1 <= month <= 12:
            return text[:7]
    return ""


def build_statement_result_from_payload(
    payload: dict[str, Any],
    *,
    target_month: str,
    allowed_catalog_ids: set[int] | None = None,
    # v1.1.xのテストや呼び出しとの互換用。値はサービスマスターIDとして扱う。
    allowed_service_ids: set[int] | None = None,
) -> StatementAnalysisResult:
    if allowed_catalog_ids is None:
        allowed_catalog_ids = allowed_service_ids or set()

    card_last4 = normalize_card_last4(payload.get("card_last4"))
    statement_period = normalize_period(payload.get("statement_period"))
    payment_date = parse_iso_date(payload.get("payment_date"))
    target_last4 = target_card_last4()
    issues: list[str] = []
    if card_last4 != target_last4:
        issues.append(f"カード末尾が{target_last4}ではなく{card_last4 or '確認不可'}として解析されました。")
    if statement_period != target_month:
        issues.append(f"明細月が{target_month}ではなく{statement_period or '確認不可'}として解析されました。")

    items: list[StatementAnalysisItem] = []
    for index, raw in enumerate(payload.get("items") or [], start=1):
        merchant = normalize_payee(raw.get("merchant_name") or "")
        if not merchant:
            continue
        catalog_id = raw.get("service_catalog_id", raw.get("registered_service_id"))
        try:
            catalog_id = int(catalog_id) if catalog_id is not None else None
        except (TypeError, ValueError):
            catalog_id = None
        if catalog_id not in allowed_catalog_ids:
            catalog_id = None

        match_status = str(raw.get("match_status") or StatementMatchStatus.UNMATCHED)
        if match_status not in StatementMatchStatus.values:
            match_status = StatementMatchStatus.UNMATCHED
        if match_status == StatementMatchStatus.MATCHED and catalog_id is None:
            match_status = StatementMatchStatus.AMBIGUOUS

        item = StatementAnalysisItem(
            line_reference=str(raw.get("line_reference") or index)[:40],
            transaction_date=parse_iso_date(raw.get("transaction_date")),
            merchant_name=merchant,
            amount_jpy=parse_amount(raw.get("amount_jpy")),
            original_amount=parse_amount(raw.get("original_amount")),
            original_currency=normalize_currency(raw.get("original_currency") or ""),
            service_catalog_id=catalog_id,
            match_status=match_status,
            receipt_required=bool(raw.get("receipt_required")),
            confidence=normalize_confidence(raw.get("confidence")),
            reason=str(raw.get("reason") or "").strip()[:2000],
        )
        items.append(item)
        if item.receipt_required and item.match_status in {
            StatementMatchStatus.AMBIGUOUS,
            StatementMatchStatus.UNMATCHED,
        }:
            issues.append(f"明細{item.line_reference}「{item.merchant_name}」はサービス対応を要確認です。")

    if not items:
        issues.append("利用明細行を抽出できませんでした。")

    summary_reason = str(payload.get("summary_reason") or "").strip()
    memo_parts = list(dict.fromkeys(issues))
    if summary_reason:
        memo_parts.append(summary_reason)

    status = CardStatementStatus.NEEDS_REVIEW if issues else CardStatementStatus.COMPLETED
    return StatementAnalysisResult(
        status=status,
        card_last4=card_last4,
        statement_period=statement_period,
        payment_date=payment_date,
        items=tuple(items),
        admin_memo=" ".join(dict.fromkeys(memo_parts))[:4000],
    )
