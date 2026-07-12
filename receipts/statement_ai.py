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
from .models import CardStatementStatus, StatementMatchStatus


@dataclass(frozen=True)
class StatementAnalysisItem:
    line_reference: str
    transaction_date: date | None
    merchant_name: str
    amount_jpy: Decimal | None
    original_amount: Decimal | None
    original_currency: str
    registered_service_id: int | None
    match_status: str
    receipt_required: bool
    confidence: float
    reason: str


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


def service_payload(services: Iterable[Any]) -> list[dict[str, Any]]:
    payload = []
    for service in services:
        aliases = ""
        if getattr(service, "catalog_service_id", None) and getattr(service.catalog_service, "merchant_aliases", ""):
            aliases = service.catalog_service.merchant_aliases
        payload.append(
            {
                "id": service.pk,
                "name": service.name,
                "billing_type": service.get_billing_type_display(),
                "aliases": aliases,
            }
        )
    return payload


def generate_card_statement_analysis(
    *,
    file_bytes: bytes,
    original_filename: str,
    content_type: str,
    period_month: date,
    services: Iterable[Any],
) -> StatementAnalysisResult:
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

    registered_services = service_payload(services)
    target_month = period_month.strftime("%Y-%m")
    target_last4 = target_card_last4()
    prompt = (
        "このファイルはクレジットカードのご利用代金明細書です。すべての利用明細行を抽出し、"
        "登録サービスとの対応を判定してください。推測で確定せず、曖昧な場合は ambiguous にしてください。\n"
        f"管理対象月: {target_month}\n"
        f"確認対象カード末尾4桁: {target_last4}\n"
        "statement_period は利用日ではなく、請求・支払対象月を YYYY-MM で返してください。"
        "例えば利用日が5月でも支払日が6月29日なら statement_period は 2026-06 です。\n"
        "各明細について、ご利用先、利用日、当月請求金額（円）、外貨金額・通貨を抽出してください。\n"
        "registered_service_id は、次の登録サービス一覧の id からだけ選びます。"
        "サービス名とカード明細のご利用先は完全一致しない場合があります。"
        "ChatGPT と OPENAI *CHATGPT / OPENAI.COM、Claude と CLAUDE.AI / ANTHROPIC.COM のような運営会社・請求名義も関連として扱ってください。\n"
        f"登録サービス一覧: {json.dumps(registered_services, ensure_ascii=False)}\n"
        "match_status の基準: matched=十分に一致、ambiguous=候補はあるが断定不可、"
        "unmatched=登録サービスに一致しない、ignored=明らかに領収書管理対象外。\n"
        "receipt_required は、登録サービスに対応するソフトウェア/SaaS/API等の請求で領収書が必要なら true。"
        "登録サービスにないがソフトウェア利用料らしい項目は ambiguous または unmatched とし、receipt_required=true にして人が確認できるようにしてください。"
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
                                "あなたは法人カード明細と領収書提出状況を照合する監査補助AIです。"
                                "表の全明細行を漏れなく抽出し、確信できない関係は曖昧として残してください。"
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
            max_output_tokens=8000,
        )
        payload = json.loads(extract_response_text(response))
        return build_statement_result_from_payload(
            payload,
            target_month=target_month,
            allowed_service_ids={item["id"] for item in registered_services},
        )
    except Exception as exc:
        return StatementAnalysisResult(
            status=CardStatementStatus.FAILED,
            admin_memo=f"OpenAI APIによるカード明細解析に失敗しました: {exc.__class__.__name__}: {exc}",
        )


def statement_schema() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "name": "card_statement_extraction",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "card_last4": {"type": ["string", "null"]},
                "statement_period": {"type": ["string", "null"], "description": "請求・支払対象月 YYYY-MM"},
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
                            "registered_service_id": {"type": ["integer", "null"]},
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
                            "registered_service_id",
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
    allowed_service_ids: set[int],
) -> StatementAnalysisResult:
    card_last4 = normalize_card_last4(payload.get("card_last4"))
    statement_period = normalize_period(payload.get("statement_period"))
    payment_date = parse_iso_date(payload.get("payment_date"))
    target_last4 = target_card_last4()
    issues: list[str] = []
    if card_last4 != target_last4:
        issues.append(
            f"カード末尾が{target_last4}ではなく{card_last4 or '確認不可'}として解析されました。"
        )
    if statement_period != target_month:
        issues.append(
            f"明細対象月が{target_month}ではなく{statement_period or '確認不可'}として解析されました。"
        )

    items: list[StatementAnalysisItem] = []
    for index, raw in enumerate(payload.get("items") or [], start=1):
        merchant = normalize_payee(raw.get("merchant_name") or "")
        if not merchant:
            continue
        service_id = raw.get("registered_service_id")
        try:
            service_id = int(service_id) if service_id is not None else None
        except (TypeError, ValueError):
            service_id = None
        if service_id not in allowed_service_ids:
            service_id = None

        match_status = str(raw.get("match_status") or StatementMatchStatus.UNMATCHED)
        if match_status not in StatementMatchStatus.values:
            match_status = StatementMatchStatus.UNMATCHED
        if match_status == StatementMatchStatus.MATCHED and service_id is None:
            match_status = StatementMatchStatus.AMBIGUOUS

        item = StatementAnalysisItem(
            line_reference=str(raw.get("line_reference") or index)[:40],
            transaction_date=parse_iso_date(raw.get("transaction_date")),
            merchant_name=merchant,
            amount_jpy=parse_amount(raw.get("amount_jpy")),
            original_amount=parse_amount(raw.get("original_amount")),
            original_currency=normalize_currency(raw.get("original_currency") or ""),
            registered_service_id=service_id,
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
