from __future__ import annotations

import base64
import json
import mimetypes
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from django.conf import settings

from .models import ReceiptFilenameStatus

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


@dataclass(frozen=True)
class ReceiptFilenameResult:
    status: str
    suggested_filename: str = ""
    admin_memo: str = ""
    payee: str = ""
    payment_date: date | None = None
    amount: Decimal | None = None
    currency: str = ""
    card_last4: str = ""
    card_last4_matches_target: bool | None = None
    payee_confirmed: bool = False
    date_confirmed: bool = False
    amount_confirmed: bool = False
    currency_confirmed: bool = False
    service_payee_related: bool | None = None
    service_payee_relation_reason: str = ""
    confidence: float = 0.0


def target_card_last4() -> str:
    return re.sub(r"\D", "", getattr(settings, "RECEIPT_CARD_LAST4", "7210"))[-4:] or "7210"


def ai_filename_enabled() -> bool:
    return bool(
        getattr(settings, "RECEIPT_AI_FILENAME_ENABLED", True)
        and getattr(settings, "OPENAI_API_KEY", "")
        and getattr(settings, "OPENAI_MODEL", "")
    )


def generate_ai_receipt_filename(
    *,
    file_bytes: bytes,
    original_filename: str,
    content_type: str,
    service_display_name: str,
    user_filename_part: str = "",
    service_match_hints: str = "",
) -> ReceiptFilenameResult:
    """領収書ファイルからファイル名候補を作成する。

    失敗してもアップロード処理を止めないため、呼び出し元が管理者メモとして保存できる結果を返す。
    """

    if not ai_filename_enabled():
        return ReceiptFilenameResult(
            status=ReceiptFilenameStatus.SKIPPED,
            admin_memo="AIファイル名修正は未実行です: OPENAI_API_KEY / OPENAI_MODEL / RECEIPT_AI_FILENAME_ENABLED を確認してください。",
        )

    try:
        from openai import OpenAI
    except Exception as exc:  # pragma: no cover - 本番依存ライブラリ欠落時の保険
        return ReceiptFilenameResult(
            status=ReceiptFilenameStatus.FAILED,
            admin_memo=f"OpenAI Python SDKを読み込めませんでした: {exc}",
        )

    try:
        client = OpenAI(
            api_key=getattr(settings, "OPENAI_API_KEY", ""),
            timeout=getattr(settings, "RECEIPT_AI_TIMEOUT", 30),
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
                                "あなたは領収書・請求書からファイル名作成に必要な情報だけを抽出する監査補助AIです。"
                                "推測で埋めず、読めない項目は null を返してください。"
                                "カード番号や個人情報は必要最小限にし、カード末尾4桁だけを返してください。"
                            ),
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": build_openai_content(
                        file_bytes=file_bytes,
                        original_filename=original_filename,
                        content_type=content_type,
                        service_display_name=service_display_name,
                        user_filename_part=user_filename_part,
                        service_match_hints=service_match_hints,
                    ),
                },
            ],
            text={"format": receipt_filename_schema()},
            max_output_tokens=900,
        )
        payload = json.loads(extract_response_text(response))
        return build_result_from_payload(
            payload,
            original_filename=original_filename,
            user_filename_part=user_filename_part,
        )
    except Exception as exc:
        return ReceiptFilenameResult(
            status=ReceiptFilenameStatus.FAILED,
            admin_memo=f"OpenAI APIによるファイル名修正に失敗しました: {exc.__class__.__name__}: {exc}",
        )


def build_openai_content(
    *,
    file_bytes: bytes,
    original_filename: str,
    content_type: str,
    service_display_name: str,
    user_filename_part: str = "",
    service_match_hints: str = "",
) -> list[dict[str, Any]]:
    target = target_card_last4()
    return [
        build_file_input_item(file_bytes=file_bytes, filename=original_filename, content_type=content_type),
        {
            "type": "input_text",
            "text": (
                f"対象の登録サービス名: {service_display_name}\n"
                f"管理者が登録した払先・カード明細表記候補: {service_match_hints or '未設定'}\n"
                f"ファイル名に使うユーザー名部分: {sanitize_filename_part(user_filename_part, fallback='user')}\n"
                f"元ファイル名: {original_filename}\n"
                f"必ず次の順番で確認してください。\n"
                f"1. 領収書内の支払カードまたは支払方法に表示されるカード末尾4桁が {target} で終わるか確認する。"
                f"カード末尾が読めない場合は null、違う場合は読めた末尾4桁を返す。\n"
                f"2. 領収書内の実際の払先・販売者・請求元・merchant/payee を確認する。"
                f"登録サービス名は参考情報であり、ファイル名の払先としてそのまま使わない。"
                f"例えば ChatGPT（サブスク）の払先は OpenAI、Claude（サブスク）の払先は Anthropic のように、"
                f"領収書に表示された請求元を優先する。\n"
                f"3. 対象の登録サービス名と領収書上の払先が同一または合理的に関連しているか確認する。"
                f"完全一致だけで判定せず、ChatGPT と OpenAI、Claude と Anthropic のような運営会社・請求元の関係は関連ありとする。"
                f"一方で ChatGPT の登録サービスなのに Anthropic の領収書、Claude の登録サービスなのに OpenAI の領収書のような組み合わせは関連なしとする。"
                f"判断が曖昧、または払先やサービスとの関係を確認できない場合は service_payee_related を null にする。\n"
                f"4. 支払日または領収書日付、合計金額、通貨を確認する。\n"
                f"5. ファイル名はアプリ側で YYMMDD_ユーザー名_企業名_金額_通貨 の形式に整形する。"
                f"企業名は登録サービス名ではなく、領収書上の払先から Inc. / LLC / PBC などの法人格表記を除いた名称を使う。\n"
                f"6. can_create_filename は、カード末尾が {target} と確認でき、払先・日付・金額・通貨のすべてを高い確度で読め、"
                f"さらに登録サービス名と払先が関連すると確認できる場合だけ true にする。"
                f"作成が難しい場合は false にし、reason に管理者が確認すべき理由を日本語で短く書く。"
            ),
        },
    ]


def build_file_input_item(*, file_bytes: bytes, filename: str, content_type: str) -> dict[str, Any]:
    filename = filename or "receipt.pdf"
    suffix = Path(filename).suffix.lower()
    mime_type = normalize_content_type(filename, content_type)
    encoded = base64.b64encode(file_bytes).decode("ascii")
    if suffix in IMAGE_EXTENSIONS or mime_type.startswith("image/"):
        return {"type": "input_image", "image_url": f"data:{mime_type};base64,{encoded}"}
    if mime_type == "application/octet-stream" and suffix == ".pdf":
        mime_type = "application/pdf"
    return {
        "type": "input_file",
        "filename": filename,
        "file_data": f"data:{mime_type};base64,{encoded}",
    }


def receipt_filename_schema() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "name": "receipt_filename_extraction",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "card_last4": {"type": ["string", "null"], "description": "領収書に表示された支払カード末尾4桁。読めない場合は null。"},
                "card_last4_matches_target": {"type": ["boolean", "null"], "description": "カード末尾が指定された末尾4桁と一致するか。読めない場合は null。"},
                "payee": {"type": ["string", "null"], "description": "実際の払先・販売者・請求元。登録サービス名ではなく領収書上の相手先。"},
                "service_payee_related": {"type": ["boolean", "null"], "description": "対象の登録サービス名と領収書上の払先が同一または合理的に関連しているか。曖昧・確認不可の場合は null。"},
                "service_payee_relation_reason": {"type": "string", "description": "サービス名と払先の関連性について、管理者が確認すべき理由や根拠。"},
                "payment_date": {"type": ["string", "null"], "description": "支払日または領収書日付。YYYY-MM-DD。"},
                "amount": {"type": ["number", "string", "null"], "description": "合計支払金額。"},
                "currency": {"type": ["string", "null"], "description": "ISO 4217通貨コード。例: JPY, USD。"},
                "can_create_filename": {"type": "boolean"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reason": {"type": "string", "description": "作成不可または注意点がある場合の管理者向け理由。"},
            },
            "required": [
                "card_last4",
                "card_last4_matches_target",
                "payee",
                "service_payee_related",
                "service_payee_relation_reason",
                "payment_date",
                "amount",
                "currency",
                "can_create_filename",
                "confidence",
                "reason",
            ],
        },
    }


def extract_response_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if output_text:
        return output_text
    if isinstance(response, dict):
        if response.get("output_text"):
            return response["output_text"]
        output = response.get("output", [])
    else:
        output = getattr(response, "output", [])
    parts: list[str] = []
    for item in output or []:
        content = item.get("content", []) if isinstance(item, dict) else getattr(item, "content", [])
        for chunk in content or []:
            if isinstance(chunk, dict):
                text = chunk.get("text") or chunk.get("output_text")
            else:
                text = getattr(chunk, "text", None)
            if text:
                parts.append(text)
    if parts:
        return "".join(parts)
    raise ValueError("OpenAI response did not contain output_text")


def build_result_from_payload(payload: dict[str, Any], *, original_filename: str, user_filename_part: str = "") -> ReceiptFilenameResult:
    target = target_card_last4()
    card_last4 = normalize_card_last4(payload.get("card_last4"))
    card_matches = payload.get("card_last4_matches_target")
    if card_matches is None:
        card_matches = payload.get("card_ends_with_7210")
    if card_matches is not None:
        card_matches = bool(card_matches)

    service_relation_supplied = "service_payee_related" in payload
    service_payee_related = payload.get("service_payee_related")
    if service_payee_related is not None:
        service_payee_related = bool(service_payee_related)
    service_relation_reason = str(payload.get("service_payee_relation_reason") or "").strip()

    payee = normalize_payee(payload.get("payee") or "")
    payment_date = parse_iso_date(payload.get("payment_date"))
    amount = parse_amount(payload.get("amount"))
    currency = normalize_currency(payload.get("currency") or "")
    confidence = normalize_confidence(payload.get("confidence"))
    can_create = bool(payload.get("can_create_filename", payload.get("can_generate_filename", False)))
    model_reason = str(payload.get("reason") or payload.get("admin_memo") or "").strip()

    issues: list[str] = []
    if card_matches is not True:
        if card_last4:
            issues.append(f"カード末尾が {target} ではなく {card_last4} と読み取られました。")
        else:
            issues.append(f"カード末尾 {target} を確認できませんでした。")
    if not payee:
        issues.append("払先を確認できませんでした。")
    if service_relation_supplied and service_payee_related is not True:
        if service_payee_related is False:
            issues.append("登録サービス名と領収書の払先が関連していない可能性があります。")
        else:
            issues.append("登録サービス名と領収書の払先の関連性を確認できませんでした。")
        if service_relation_reason:
            issues.append(service_relation_reason)
    if payment_date is None:
        issues.append("日付を確認できませんでした。")
    if amount is None:
        issues.append("金額を確認できませんでした。")
    if not currency:
        issues.append("通貨を確認できませんでした。")
    if not can_create:
        issues.append(model_reason or "AIがファイル名作成に必要な項目を十分な確度で確認できませんでした。")
    if confidence < 0.65:
        issues.append(f"抽出信頼度が低いです（{confidence:.2f}）。")

    suggested_filename = ""
    if payee and payment_date is not None and amount is not None and currency:
        suggested_filename = build_receipt_filename(
            payment_date=payment_date,
            user_filename_part=user_filename_part,
            payee=payee,
            amount=amount,
            currency=currency,
            extension=Path(original_filename).suffix.lower() or ".pdf",
        )

    result_kwargs = dict(
        suggested_filename=suggested_filename if can_create and not issues else "",
        payee=payee,
        payment_date=payment_date,
        amount=amount,
        currency=currency,
        card_last4=card_last4,
        card_last4_matches_target=card_matches,
        payee_confirmed=bool(payee),
        date_confirmed=payment_date is not None,
        amount_confirmed=amount is not None,
        currency_confirmed=bool(currency),
        service_payee_related=service_payee_related if service_relation_supplied else None,
        service_payee_relation_reason=service_relation_reason,
        confidence=confidence,
    )

    if issues:
        return ReceiptFilenameResult(
            status=ReceiptFilenameStatus.NEEDS_REVIEW,
            admin_memo="AIファイル名修正不可: " + " ".join(dict.fromkeys(issues)),
            **result_kwargs,
        )

    return ReceiptFilenameResult(
        status=ReceiptFilenameStatus.GENERATED,
        admin_memo="",
        **result_kwargs,
    )


def build_result_from_ai_payload(payload: dict[str, Any], *, original_filename: str, user_filename_part: str = "") -> ReceiptFilenameResult:
    """旧テスト・旧実装名との互換用。"""

    return build_result_from_payload(payload, original_filename=original_filename, user_filename_part=user_filename_part)


def build_receipt_filename(
    *,
    payment_date: date,
    user_filename_part: str,
    payee: str,
    amount: Decimal,
    currency: str,
    extension: str,
) -> str:
    return "_".join(
        [
            payment_date.strftime("%y%m%d"),
            sanitize_filename_part(user_filename_part, fallback="user"),
            sanitize_company_name_for_filename(payee),
            format_amount_for_filename(amount),
            sanitize_filename_part(currency.upper(), fallback="CUR"),
        ]
    ) + (extension.lower() or ".pdf")


def filename_user_part_from_user(user: Any) -> str:
    """ファイル名に入れるユーザー識別子を作る。

    ユーザー名はメール形式で運用するため、Djangoの姓が未設定の場合はメールアドレスの
    @ 前を使う。例: test@hakuhodo.co.jp -> test。
    """

    last_name = sanitize_filename_part(getattr(user, "last_name", ""), fallback="")
    if last_name:
        return last_name
    email = getattr(user, "email", "") or getattr(user, "username", "") or ""
    local_part = str(email).split("@", 1)[0]
    return sanitize_filename_part(local_part, fallback="user")


def normalize_confidence(value: Any) -> float:
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "high":
            return 0.95
        if lowered == "medium":
            return 0.75
        if lowered == "low":
            return 0.30
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def normalize_card_last4(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))[-4:]


def normalize_payee(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").strip()
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"^(merchant|payee|seller|vendor|billed by|paid to)\s*[:：]\s*", "", value, flags=re.I)
    return value[:160]


def sanitize_company_name_for_filename(value: str, fallback: str = "Unknown") -> str:
    value = unicodedata.normalize("NFKC", value or "").strip()
    value = re.sub(r"\b(PBC|INCORPORATED|INC|LLC|L\.?L\.?C|LTD|LIMITED|CORPORATION|CORP|COMPANY|CO|GMBH|S\.?A\.?|K\.?K\.?|G\.?K\.?)\b\.?", "", value, flags=re.I)
    value = re.sub(r"[,、，]+", " ", value)
    return sanitize_filename_part(value, fallback=fallback)


def sanitize_filename_part(value: str, fallback: str = "Unknown") -> str:
    value = unicodedata.normalize("NFKC", value or "").strip()
    value = re.sub(r"[\\/\0\r\n\t:*?\"<>|]+", "", value)
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"_+", "_", value)
    value = value.strip("._- ")
    return (value or fallback)[:80]


def parse_iso_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def parse_amount(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    text = str(value).strip().replace(",", "")
    text = re.sub(r"[^0-9.\-]", "", text)
    if not text or text in {"-", "."}:
        return None
    try:
        return Decimal(text).copy_abs().quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def normalize_currency(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").strip().upper()
    if value in {"円", "¥", "JPY円"}:
        return "JPY"
    if value in {"$", "US$", "USD$"}:
        return "USD"
    value = re.sub(r"[^A-Z]", "", value)
    return value[:3] if len(value) >= 3 else ""


def format_amount_for_filename(amount: Decimal) -> str:
    amount = amount.quantize(Decimal("0.01"))
    if amount == amount.to_integral_value():
        return str(amount.quantize(Decimal("1")))
    return format(amount.normalize(), "f")


def normalize_content_type(filename: str, content_type: str = "") -> str:
    if content_type and content_type != "application/octet-stream":
        return content_type
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or content_type or "application/octet-stream"
