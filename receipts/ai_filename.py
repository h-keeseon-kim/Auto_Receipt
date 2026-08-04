from __future__ import annotations

import base64
import json
import mimetypes
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from typing import Any

from django.conf import settings

from .models import ReceiptFilenameStatus, ReceiptFinancialDocumentKind

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
PDF_TEXT_MAX_CHARS = 16000


@dataclass(frozen=True)
class ReceiptTextFallback:
    """PDFの埋め込みテキストから安全に補完できた領収書情報。"""

    text: str = ""
    payee: str = ""
    recipient_name: str = ""
    recipient_name_matches_user: bool | None = None
    recipient_name_relation_reason: str = ""
    payment_date: date | None = None
    amount: Decimal | None = None
    currency: str = ""
    financial_document_kind: str = ReceiptFinancialDocumentKind.UNKNOWN
    transaction_reference: str = ""
    related_transaction_reference: str = ""
    transaction_components: tuple[dict[str, Any], ...] = ()
    confidence: float = 0.0


@dataclass(frozen=True)
class ReceiptFilenameResult:
    status: str
    suggested_filename: str = ""
    admin_memo: str = ""
    payee: str = ""
    filename_label: str = ""
    payment_date: date | None = None
    amount: Decimal | None = None
    currency: str = ""
    card_last4: str = ""
    card_last4_matches_target: bool | None = None
    recipient_name: str = ""
    recipient_name_matches_user: bool | None = None
    recipient_name_relation_reason: str = ""
    payee_confirmed: bool = False
    date_confirmed: bool = False
    amount_confirmed: bool = False
    currency_confirmed: bool = False
    service_payee_related: bool | None = None
    service_payee_relation_reason: str = ""
    financial_document_kind: str = ReceiptFinancialDocumentKind.UNKNOWN
    transaction_reference: str = ""
    related_transaction_reference: str = ""
    transaction_components: tuple[dict[str, Any], ...] = ()
    confidence: float = 0.0


def target_card_last4() -> str:
    return re.sub(r"\D", "", getattr(settings, "RECEIPT_CARD_LAST4", "7210"))[-4:] or "7210"


def ai_filename_enabled() -> bool:
    return bool(
        getattr(settings, "RECEIPT_AI_FILENAME_ENABLED", True)
        and getattr(settings, "OPENAI_API_KEY", "")
        and getattr(settings, "OPENAI_MODEL", "")
    )


def extract_embedded_pdf_text(
    *,
    file_bytes: bytes,
    original_filename: str,
    content_type: str,
) -> str:
    """テキスト埋め込み型PDFから文字列を取得する。

    OpenAIへは従来どおりPDF本体も渡すが、PDF内に検索可能なテキストがある場合は
    同じ内容を明示的なテキストとしても渡し、表組みや小さい文字の読み落としを減らす。
    画像PDFにOCRを実行する処理ではない。
    """

    suffix = Path(original_filename or "").suffix.lower()
    normalized_content_type = normalize_content_type(original_filename, content_type)
    if suffix != ".pdf" and normalized_content_type != "application/pdf":
        return ""

    try:
        from pypdf import PdfReader
    except Exception:
        return ""

    try:
        reader = PdfReader(BytesIO(file_bytes))
        page_texts: list[str] = []
        for page in reader.pages:
            extracted = page.extract_text() or ""
            if extracted.strip():
                page_texts.append(extracted)
        text = "\n".join(page_texts)
    except Exception:
        return ""

    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:PDF_TEXT_MAX_CHARS]


def _parse_labeled_date_value(value: str) -> date | None:
    value = unicodedata.normalize("NFKC", value or "").strip()
    value = re.sub(r"\s+", " ", value)
    if not value:
        return None

    iso_match = re.search(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b", value)
    if iso_match:
        try:
            return date(int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3)))
        except ValueError:
            pass

    jp_match = re.search(r"\b(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", value)
    if jp_match:
        try:
            return date(int(jp_match.group(1)), int(jp_match.group(2)), int(jp_match.group(3)))
        except ValueError:
            pass

    dmy_match = re.search(r"\b(\d{1,2})[./](\d{1,2})[./](20\d{2})\b", value)
    if dmy_match:
        try:
            return date(int(dmy_match.group(3)), int(dmy_match.group(2)), int(dmy_match.group(1)))
        except ValueError:
            pass

    month_name_match = re.search(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December|"
        r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\s+"
        r"(\d{1,2})(?:st|nd|rd|th)?[,]?\s+(20\d{2})\b",
        value,
        flags=re.I,
    )
    if month_name_match:
        value = f"{month_name_match.group(1)} {month_name_match.group(2)} {month_name_match.group(3)}"
        for pattern in ("%B %d %Y", "%b %d %Y"):
            try:
                return datetime.strptime(value, pattern).date()
            except ValueError:
                continue
    return None


def _extract_labeled_payment_date(text: str) -> date | None:
    if not text:
        return None
    labels = (
        r"payment\s+date",
        r"date\s+paid",
        r"paid\s+on",
        r"date\s+of\s+issue",
        r"issue\s+date",
        r"invoice\s+date",
        r"date\s+due",
        r"tax\s+point\s+date",
        r"^date$",
        r"支払日",
        r"支払い日",
        r"決済日",
        r"領収日",
        r"請求書発行日",
        r"発行日",
    )
    for label in labels:
        for match in re.finditer(rf"(?im)^.*?{label}\s*[:：]?\s*(.+)$", text):
            parsed = _parse_labeled_date_value(match.group(1))
            if parsed:
                return parsed
    # GitHub等の領収書は単に "Date 2026-06-02 11:28AM PDT" と記載する。
    for match in re.finditer(r"(?im)^\s*Date\s+(.+)$", text):
        parsed = _parse_labeled_date_value(match.group(1))
        if parsed:
            return parsed

    # 表形式PDFではラベルと値の間に点線や改行が入ることがある。
    broad_labels = (
        r"invoice\s+date", r"date\s+of\s+issue", r"payment\s+date", r"date\s+paid", r"date\s+due",
        r"請求書発行日", r"支払い日", r"支払日",
    )
    for label in broad_labels:
        match = re.search(
            rf"(?is){label}.{{0,120}}?((?:20\d{{2}}[-/.]\d{{1,2}}[-/.]\d{{1,2}})|"
            rf"(?:20\d{{2}}年\s*\d{{1,2}}月\s*\d{{1,2}}日)|"
            rf"(?:(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\s+\d{{1,2}}(?:st|nd|rd|th)?[,]?\s+20\d{{2}}))",
            text,
        )
        if match:
            parsed = _parse_labeled_date_value(match.group(1))
            if parsed:
                return parsed

    # Google系日本語請求書は、抽出順の都合で日付値が「請求書発行日」ラベルより前に
    # 並ぶ場合がある。請求書本文に限定し、最初の日本語日付を発行日として扱う。
    if re.search(r"請求書発行日|請求書番号", text):
        match = re.search(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
        if match:
            try:
                return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                pass
    return None


_CURRENCY_CODES = ("JPY", "USD", "EUR", "GBP", "AUD", "CAD", "CHF", "CNY", "KRW", "SGD", "HKD", "NZD", "INR")
_CURRENCY_TOKEN_PATTERN = r"(?:" + "|".join(_CURRENCY_CODES) + r"|¥|￥|\$|€|£)"
_AMOUNT_TOKEN_PATTERN = r"[-+]?\d[\d,]*(?:\.\d+)?"


def _normalize_currency_token(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").strip().upper()
    return {
        "¥": "JPY",
        "￥": "JPY",
        "$": "USD",
        "€": "EUR",
        "£": "GBP",
    }.get(value, value if value in _CURRENCY_CODES else "")


def _extract_amount_currency_from_line(line: str) -> tuple[Decimal | None, str]:
    line = unicodedata.normalize("NFKC", line or "")
    patterns = (
        rf"(?P<currency>{_CURRENCY_TOKEN_PATTERN})\s*(?P<amount>{_AMOUNT_TOKEN_PATTERN})",
        rf"(?P<amount>{_AMOUNT_TOKEN_PATTERN})\s*(?P<currency>{_CURRENCY_TOKEN_PATTERN})",
    )
    for pattern in patterns:
        matches = list(re.finditer(pattern, line, flags=re.I))
        if not matches:
            continue
        # Amount paid / PAIDの行には同じ金額が複数出ない前提だが、出た場合は最後の値を優先する。
        for match in reversed(matches):
            amount = parse_amount(match.group("amount"))
            currency = _normalize_currency_token(match.group("currency"))
            if amount is not None and currency:
                return amount, currency
    return None, ""


def _extract_labeled_amount_currency(text: str) -> tuple[Decimal | None, str]:
    if not text:
        return None, ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    label_patterns = (
        r"\bamount\s+paid\b",
        r"\bamount\s+due\b",
        r"^paid\s*[:：]",
        r"\btotal\s+paid\b",
        r"^grand\s+total\b",
        r"^invoice\s+total\b",
        r"^total\s+in\s+[A-Z]{3}\b",
        r"^total\s+amount\b",
        r"^total\b(?!\s+excluding)",
        r"^支払(?:済)?額\b",
        r"^支払い金額\b",
        r"^領収金額\b",
        r"^合計\s*[:：]",
    )
    for label_pattern in label_patterns:
        for line in lines:
            if not re.search(label_pattern, line, flags=re.I):
                continue
            amount, currency = _extract_amount_currency_from_line(line)
            if amount is not None and currency:
                return amount, currency

    # "$22.00 USD due ..." のように金額が先に書かれる請求書形式。
    for line in lines:
        if not re.search(r"\bdue\b", line, flags=re.I):
            continue
        amount, currency = _extract_amount_currency_from_line(line)
        if amount is not None and currency:
            return amount, currency

    # "$22.00 paid on ..." のように金額が先に書かれる形式を補完する。
    for line in lines:
        if not re.search(r"\bpaid\b|領収いたしました", line, flags=re.I):
            continue
        amount, currency = _extract_amount_currency_from_line(line)
        if amount is not None and currency:
            return amount, currency

    # Google系日本語請求書では、PDF抽出順により「￥150」の後に
    # 「Google Cloud / 合計（JPY）」が並ぶ。合計ラベルの直前値も読む。
    google_total = re.search(
        r"(?is)[¥￥]\s*([0-9][0-9,]*)\s*(?:Google\s+(?:Cloud|One)[^\n]*\s*)?"
        r"合計\s*[（(]\s*JPY\s*[）)]",
        text,
    )
    if google_total:
        amount = parse_amount(google_total.group(1))
        if amount is not None:
            return amount, "JPY"

    # 反対順（合計（JPY）の後に金額）のPDFにも対応する。
    google_total_after = re.search(
        r"(?is)合計\s*[（(]\s*JPY\s*[）)].{0,80}?[¥￥]\s*([0-9][0-9,]*)",
        text,
    )
    if google_total_after:
        amount = parse_amount(google_total_after.group(1))
        if amount is not None:
            return amount, "JPY"
    return None, ""


def _extract_probable_payee(text: str) -> str:
    if not text:
        return ""
    corporate_pattern = re.compile(
        r"\b(?:PBC|INCORPORATED|INC\.?|LLC|LTD\.?|LIMITED|CORPORATION|CORP\.?|GMBH|S\.?R\.?O\.?|GK)\b"
        r"|株式会社|合同会社|有限会社",
        flags=re.I,
    )
    excluded = re.compile(r"invoice|receipt|bill\s+to|page\s+\d|reference|order|customer|address", flags=re.I)
    for raw_line in text.splitlines()[:80]:
        line = normalize_payee(raw_line)
        if not line or len(line) > 100 or excluded.search(line):
            continue
        if corporate_pattern.search(line):
            return line

    domain_match = re.search(r"(?im)^www\.([a-z0-9-]+)\.[a-z.]+\s*$", text)
    if domain_match:
        return domain_match.group(1).replace("-", " ").title()

    # 法人格がないサービス（Suno等）は公式メールドメインから補完する。
    email_match = re.search(r"(?i)\b(?:billing|support|receipts?|invoice)@([a-z0-9-]+)\.[a-z.]+", text)
    if email_match:
        domain = email_match.group(1).lower()
        known = {
            "suno": "Suno",
            "dify": "Dify",
            "anthropic": "Anthropic",
            "openai": "OpenAI",
            "github": "GitHub",
            "railway": "Railway",
            "jetbrains": "JetBrains",
        }
        if domain in known:
            return known[domain]
    return ""


def _extract_expected_email(expected_recipient_context: str) -> str:
    emails = re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", expected_recipient_context or "", flags=re.I)
    return emails[0].lower() if emails else ""


def _extract_financial_document_kind(text: str) -> str:
    """ファイル名ではなく書類本文から、請求・領収・返金を分類する。"""

    if not text:
        return ReceiptFinancialDocumentKind.UNKNOWN
    if re.search(
        r"(?im)^\s*refund\s*$|\brefunded\s+on\b|\brefund\s+amount\b|"
        r"\bcredited\s+total\b|\byou(?:'|’)ve\s+been\s+issued\s+a\s+refund\b|"
        r"返金(?:額|日|済|処理)",
        text,
    ):
        return ReceiptFinancialDocumentKind.REFUND
    if re.search(
        r"(?im)^\s*receipt\s*$|\bamount\s+paid\b|\bpaid\s+on\b|\bcharged\s+to\b|"
        r"\bthanks\s+for\s+your\s+purchase\b|^\s*PAID\s*:?\s*(?:[\d$¥￥]|$)|"
        r"\bapplied\s+transactions\b|\bbalance\s+due\s*:?\s*[$¥￥]?\s*0(?:\.0+)?\b|"
        r"領収書|領収金額|支払済|支払い金額",
        text,
    ):
        return ReceiptFinancialDocumentKind.CHARGE
    if re.search(r"(?im)^\s*invoice\s*$|\binvoice\s+(?:number|#|reference)\b|請求書", text):
        return ReceiptFinancialDocumentKind.INVOICE
    return ReceiptFinancialDocumentKind.UNKNOWN


def _extract_reference(text: str, patterns: tuple[str, ...]) -> str:
    for pattern in patterns:
        match = re.search(pattern, text or "", flags=re.I | re.M)
        if match:
            value = unicodedata.normalize("NFKC", match.group(1) or "").strip()
            value = re.sub(r"[^A-Za-z0-9._:/-]+", "", value)
            if value:
                return value[:160]
    return ""


def _extract_financial_references(text: str) -> tuple[str, str]:
    """自身の取引参照番号と、返金元取引の参照番号を抽出する。"""

    transaction_reference = _extract_reference(
        text,
        (
            r"^[ \t]*Transaction[ \t]+ID[ \t]*[:：]?[ \t]*([^\s]+)",
            r"^[ \t]*Invoice[ \t]+(?:number|#|reference)[ \t]*[:：]?[ \t]*([^\s]+)",
            r"^[ \t]*Reference[ \t]+number[ \t]*[:：]?[ \t]*([^\s]+)",
            r"^[ \t]*Receipt[ \t]+number[ \t]*[:：]?[ \t]*([^\s]+)",
            r"^[ \t]*請求書番号[ \t]*[:：]?[ \t]*([^\s]+)",
            r"^[ \t]*領収書番号[ \t]*[:：]?[ \t]*([^\s]+)",
        ),
    )
    related_reference = _extract_reference(
        text,
        (
            r"^\s*Sale\s+Transaction\s+ID[ \t]*[:：]?[ \t]*([^\s]+)",
            r"^\s*Original\s+(?:Transaction|Charge)\s+ID[ \t]*[:：]?[ \t]*([^\s]+)",
            r"^\s*元取引(?:番号|ID)[ \t]*[:：]?[ \t]*([^\s]+)",
        ),
    )
    if not transaction_reference or transaction_reference.lower() in {"invoice", "receipt", "reference"}:
        inline_invoice = re.search(r"\b(INV[A-Z0-9._/-]{5,})\b", text or "", flags=re.I)
        if inline_invoice:
            transaction_reference = inline_invoice.group(1)[:160]
    return transaction_reference, related_reference


def _extract_refund_amount_currency(text: str) -> tuple[Decimal | None, str]:
    """返金書類の実返金額を正の絶対値で返す。元請求額や調整後合計は使わない。"""

    if not text:
        return None, ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    label_patterns = (
        r"\brefund\s+amount\b",
        r"\brefunded\s+on\b",
        r"^credited\s+total\b(?!\s+excluding)",
        r"^返金額\b",
    )
    for label_pattern in label_patterns:
        for line in lines:
            if not re.search(label_pattern, line, flags=re.I):
                continue
            amount, currency = _extract_amount_currency_from_line(line)
            if amount is not None and currency:
                return abs(amount), currency
    return None, ""


def _extract_invoice_number(text: str) -> str:
    return _extract_reference(
        text,
        (
            r"^[ \t]*Invoice[ \t]+(?:number|#|reference)[ \t]*[:：]?[ \t]*([^\s]+)",
            r"^[ \t]*請求書番号[ \t]*[:：]?[ \t]*([^\s]+)",
        ),
    )


def _extract_credit_note_reference(text: str) -> str:
    match = re.search(r"\(([A-Z0-9._/-]+-CN-\d+)\)", text or "", flags=re.I)
    return (match.group(1) if match else "")[:160]


def _extract_generic_transaction_date(text: str) -> date | None:
    """``Date 2026-06-24 09:37PM PDT`` のような決済日を補完する。"""

    for match in re.finditer(
        r"(?im)^\s*Date\s*[:：]?\s*(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2})\b",
        text or "",
    ):
        parsed = _parse_labeled_date_value(match.group(1))
        if parsed:
            return parsed
    return None


def _extract_payment_history_component(text: str, *, payee: str, invoice_number: str) -> dict[str, Any] | None:
    """返金書類のPayment historyから元決済を抽出する。

    Stripe系の返金PDFは、返金額だけでなく元のAmount paid、日付、Receipt numberを
    Payment historyに持つ。これを独立した決済証拠として保存する。
    """

    if not text or not re.search(r"(?i)payment\s+history", text):
        return None
    section = re.split(r"(?i)payment\s+history", text, maxsplit=1)[-1]
    # 行が折り返されても、Date -> Amount paid の順で探索する。
    date_matches = list(re.finditer(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
        r"(\d{1,2})(?:st|nd|rd|th)?[,]?\s+(20\d{2})",
        section,
        flags=re.I,
    ))
    for date_match in date_matches:
        transaction_date = _parse_labeled_date_value(date_match.group(0))
        tail = section[date_match.end():date_match.end() + 500]
        amount, currency = _extract_amount_currency_from_line(tail)
        if transaction_date and amount is not None and currency:
            receipt_number = _extract_reference(
                tail,
                (r"(?:Receipt\s+number\s*)?([0-9]{3,5}-[0-9]{3,5}(?:-[0-9]{3,5})?)",),
            )
            return {
                "component_key": f"original:{invoice_number or receipt_number or transaction_date.isoformat()}",
                "role": "charge",
                "signed_amount": str(abs(amount)),
                "currency": currency,
                "transaction_date": transaction_date.isoformat(),
                "payee": payee,
                "invoice_number": invoice_number,
                "transaction_id": receipt_number,
                "related_transaction_id": "",
                "source_label": "Payment historyの元決済",
                "confidence": 0.96,
            }
    return None


def _build_fallback_transaction_components(
    *,
    text: str,
    payee: str,
    payment_date: date | None,
    amount: Decimal | None,
    currency: str,
    financial_document_kind: str,
    transaction_reference: str,
    related_transaction_reference: str,
) -> tuple[dict[str, Any], ...]:
    components: list[dict[str, Any]] = []
    invoice_number = _extract_invoice_number(text)

    if financial_document_kind == ReceiptFinancialDocumentKind.REFUND:
        original = _extract_payment_history_component(
            text,
            payee=payee,
            invoice_number=invoice_number,
        )
        if original:
            components.append(original)
        if amount is not None and currency and payment_date:
            refund_reference = _extract_credit_note_reference(text) or transaction_reference
            components.append(
                {
                    "component_key": f"refund:{refund_reference or payment_date.isoformat()}",
                    "role": "refund",
                    "signed_amount": str(-abs(amount)),
                    "currency": currency,
                    "transaction_date": payment_date.isoformat(),
                    "payee": payee,
                    "invoice_number": refund_reference or invoice_number,
                    "transaction_id": transaction_reference,
                    "related_transaction_id": related_transaction_reference or invoice_number,
                    "source_label": "返金額",
                    "confidence": 0.96,
                }
            )
    elif amount is not None and currency and payment_date:
        components.append(
            {
                "component_key": f"primary:{transaction_reference or invoice_number or payment_date.isoformat()}",
                "role": "charge",
                "signed_amount": str(abs(amount)),
                "currency": currency,
                "transaction_date": payment_date.isoformat(),
                "payee": payee,
                "invoice_number": invoice_number,
                "transaction_id": transaction_reference,
                "related_transaction_id": related_transaction_reference,
                "source_label": "支払済み合計" if financial_document_kind != ReceiptFinancialDocumentKind.INVOICE else "請求金額",
                "confidence": 0.96,
            }
        )
    return tuple(components)


def parse_signed_amount(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    text = unicodedata.normalize("NFKC", str(value)).strip().replace(",", "")
    text = re.sub(r"[^0-9.\-+]", "", text)
    if not text or text in {"-", "+", "."}:
        return None
    try:
        return Decimal(text).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def normalize_transaction_components(
    values: Any,
    *,
    fallback_payee: str = "",
    fallback_kind: str = ReceiptFinancialDocumentKind.UNKNOWN,
) -> tuple[dict[str, Any], ...]:
    if not isinstance(values, (list, tuple)):
        return ()
    normalized: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for index, raw in enumerate(values):
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role") or "charge").strip().lower()
        if role not in {"charge", "refund"}:
            continue
        amount = parse_signed_amount(raw.get("signed_amount"))
        currency = normalize_currency(raw.get("currency") or "")
        transaction_date = parse_iso_date(raw.get("transaction_date"))
        if amount is None or not currency or transaction_date is None:
            continue
        amount = -abs(amount) if role == "refund" else abs(amount)
        component_key = sanitize_filename_part(
            str(raw.get("component_key") or f"component-{index + 1}"),
            fallback=f"component-{index + 1}",
        )[:160]
        if component_key in seen_keys:
            component_key = f"{component_key}-{index + 1}"[:160]
        seen_keys.add(component_key)
        confidence = normalize_confidence(raw.get("confidence"))
        normalized.append(
            {
                "component_key": component_key,
                "role": role,
                "signed_amount": format(amount, "f"),
                "currency": currency,
                "transaction_date": transaction_date.isoformat(),
                "payee": normalize_payee(raw.get("payee") or fallback_payee),
                "invoice_number": str(raw.get("invoice_number") or "").strip()[:160],
                "transaction_id": str(raw.get("transaction_id") or "").strip()[:160],
                "related_transaction_id": str(raw.get("related_transaction_id") or "").strip()[:160],
                "source_label": str(raw.get("source_label") or "").strip()[:120],
                "confidence": confidence,
                "document_kind": str(raw.get("document_kind") or fallback_kind).strip().lower(),
            }
        )
    return tuple(normalized)


def extract_receipt_text_fallback(
    text: str,
    *,
    expected_recipient_context: str = "",
) -> ReceiptTextFallback:
    """埋め込みテキストから、ラベルが明確な項目だけを抽出する。"""

    if not text:
        return ReceiptTextFallback()

    # PDF埋め込みテキストではハイフンがNUL文字へ化けることがある。
    # 取引ID・Invoice番号を重複排除に使うため、解析前に正規化する。
    text = unicodedata.normalize("NFKC", text).replace("\x00", "-")

    payee = _extract_probable_payee(text)
    financial_document_kind = _extract_financial_document_kind(text)
    payment_date = _extract_labeled_payment_date(text)
    if payment_date is None and re.search(
        r"(?i)\btransaction\s+id\b|\bcharged\s+to\b|\brefund\s+amount\b|"
        r"\byou(?:'|’)ve\s+been\s+issued\s+a\s+refund\b",
        text,
    ):
        payment_date = _extract_generic_transaction_date(text)
    if financial_document_kind == ReceiptFinancialDocumentKind.REFUND:
        refund_date_match = re.search(r"(?i)refunded\s+on\s+(.+)$", text, flags=re.M)
        if refund_date_match:
            payment_date = _parse_labeled_date_value(refund_date_match.group(1)) or payment_date
        amount, currency = _extract_refund_amount_currency(text)
    else:
        amount, currency = _extract_labeled_amount_currency(text)
    transaction_reference, related_transaction_reference = _extract_financial_references(text)
    transaction_components = _build_fallback_transaction_components(
        text=text,
        payee=payee,
        payment_date=payment_date,
        amount=amount,
        currency=currency,
        financial_document_kind=financial_document_kind,
        transaction_reference=transaction_reference,
        related_transaction_reference=related_transaction_reference,
    )
    expected_email = _extract_expected_email(expected_recipient_context)
    lowered_text = text.lower()
    recipient_name = ""
    recipient_match: bool | None = None
    recipient_reason = ""
    if expected_email and expected_email in lowered_text:
        recipient_name = expected_email
        recipient_match = True
        recipient_reason = "領収書本文に対象ユーザーのメールアドレスが記載されています。"

    core_count = sum(bool(value) for value in (payee, payment_date, amount is not None, currency))
    confidence = 0.0
    if core_count == 4:
        confidence = 0.96
    elif core_count >= 3:
        confidence = 0.88
    elif core_count >= 2:
        confidence = 0.74

    return ReceiptTextFallback(
        text=text,
        payee=payee,
        recipient_name=recipient_name,
        recipient_name_matches_user=recipient_match,
        recipient_name_relation_reason=recipient_reason,
        payment_date=payment_date,
        amount=amount,
        currency=currency,
        financial_document_kind=financial_document_kind,
        transaction_reference=transaction_reference,
        related_transaction_reference=related_transaction_reference,
        transaction_components=transaction_components,
        confidence=confidence,
    )


def _normalized_relation_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").lower()
    value = re.sub(r"\b(?:pbc|incorporated|inc|llc|ltd|limited|corporation|corp|gmbh|s\.?r\.?o)\b", " ", value)
    value = re.sub(r"[^a-z0-9ぁ-んァ-ヶ一-龠]+", " ", value)
    return " ".join(value.split())


def infer_context_relation(payee: str, context: str) -> bool | None:
    """払先とサービス名・払先候補・その他メモの明確な文字関連だけを補助判定する。"""

    left = _normalized_relation_text(payee)
    right = _normalized_relation_text(context)
    if not left or not right:
        return None
    if left in right or right in left:
        return True
    left_tokens = {token for token in left.split() if len(token) >= 4}
    right_tokens = {token for token in right.split() if len(token) >= 4}
    return True if left_tokens & right_tokens else None


def merge_payload_with_text_fallback(
    payload: dict[str, Any],
    fallback: ReceiptTextFallback,
    *,
    service_context: str,
) -> tuple[dict[str, Any], bool]:
    merged = dict(payload)
    fallback_used = False

    def fill(field: str, value: Any):
        nonlocal fallback_used
        if value in (None, ""):
            return
        if merged.get(field) in (None, ""):
            merged[field] = value
            fallback_used = True

    fill("payee", fallback.payee)
    fill("filename_label", fallback.payee)
    fill("recipient_name", fallback.recipient_name)
    fill("payment_date", fallback.payment_date.isoformat() if fallback.payment_date else None)
    fill("amount", str(fallback.amount) if fallback.amount is not None else None)
    fill("currency", fallback.currency)
    fill("financial_document_kind", fallback.financial_document_kind)
    fill("transaction_reference", fallback.transaction_reference)
    fill("related_transaction_reference", fallback.related_transaction_reference)
    if fallback.transaction_components:
        existing_components = normalize_transaction_components(
            merged.get("transaction_components") or (),
            fallback_payee=str(merged.get("payee") or fallback.payee),
            fallback_kind=str(
                merged.get("financial_document_kind") or fallback.financial_document_kind
            ),
        )
        fallback_components = normalize_transaction_components(
            fallback.transaction_components,
            fallback_payee=fallback.payee,
            fallback_kind=fallback.financial_document_kind,
        )

        def component_identity(component: dict[str, Any]) -> tuple:
            reference = (
                component.get("invoice_number")
                or component.get("transaction_id")
                or component.get("related_transaction_id")
                or ""
            )
            return (
                component.get("role"),
                component.get("signed_amount"),
                component.get("currency"),
                component.get("transaction_date"),
                _normalized_relation_text(str(component.get("payee") or "")),
                re.sub(r"[^A-Za-z0-9]+", "", str(reference).upper()),
            )

        combined = [dict(component) for component in existing_components]
        seen = {component_identity(component) for component in combined}
        for component in fallback_components:
            identity = component_identity(component)
            if identity in seen:
                continue
            combined.append(dict(component))
            seen.add(identity)
            fallback_used = True
        if combined:
            merged["transaction_components"] = combined

    if merged.get("recipient_name_matches_user") is None and fallback.recipient_name_matches_user is not None:
        merged["recipient_name_matches_user"] = fallback.recipient_name_matches_user
        merged["recipient_name_relation_reason"] = (
            merged.get("recipient_name_relation_reason") or fallback.recipient_name_relation_reason
        )
        fallback_used = True

    if merged.get("service_payee_related") is None:
        relation = infer_context_relation(str(merged.get("payee") or fallback.payee), service_context)
        if relation is not None:
            merged["service_payee_related"] = relation
            merged["service_payee_relation_reason"] = (
                merged.get("service_payee_relation_reason")
                or "PDF埋め込みテキストの払先と、登録サービス・払先候補または入力メモに明確な文字関連があります。"
            )
            fallback_used = True

    if fallback_used:
        try:
            merged["confidence"] = max(float(merged.get("confidence") or 0), fallback.confidence)
        except (TypeError, ValueError):
            merged["confidence"] = fallback.confidence

        core_ready = bool(
            merged.get("filename_label")
            and merged.get("payment_date")
            and merged.get("amount") not in (None, "")
            and merged.get("currency")
        )
        card_mismatch = merged.get("card_last4_matches_target") is False
        relation_ready = merged.get("service_payee_related") is True
        if core_ready and relation_ready and not card_mismatch:
            merged["can_create_filename"] = True
            if merged.get("reason"):
                merged["reason"] = str(merged["reason"]) + " PDF埋め込みテキストで不足項目を補完しました。"
            else:
                merged["reason"] = "PDF埋め込みテキストで不足項目を補完しました。"

    return merged, fallback_used


def generate_ai_receipt_filename(
    *,
    file_bytes: bytes,
    original_filename: str,
    content_type: str,
    service_display_name: str,
    user_filename_part: str = "",
    expected_recipient_context: str = "",
    service_match_hints: str = "",
    receipt_memo: str = "",
    is_extra: bool = False,
) -> ReceiptFilenameResult:
    """領収書ファイルからファイル名候補を作成する。

    失敗してもアップロード処理を止めないため、呼び出し元が管理者メモとして保存できる結果を返す。
    """

    embedded_text = extract_embedded_pdf_text(
        file_bytes=file_bytes,
        original_filename=original_filename,
        content_type=content_type,
    )
    text_fallback = extract_receipt_text_fallback(
        embedded_text,
        expected_recipient_context=expected_recipient_context,
    )
    service_context = " / ".join(
        part
        for part in (
            receipt_memo if is_extra else service_display_name,
            service_match_hints,
        )
        if part
    )

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
                        expected_recipient_context=expected_recipient_context,
                        service_match_hints=service_match_hints,
                        receipt_memo=receipt_memo,
                        is_extra=is_extra,
                        embedded_text=embedded_text,
                    ),
                },
            ],
            text={"format": receipt_filename_schema()},
            max_output_tokens=2600,
        )
        payload = json.loads(extract_response_text(response))
        payload, _ = merge_payload_with_text_fallback(
            payload,
            text_fallback,
            service_context=service_context,
        )
        return build_result_from_payload(
            payload,
            original_filename=original_filename,
            user_filename_part=user_filename_part,
            is_extra=is_extra,
        )
    except Exception as exc:
        fallback_relation = infer_context_relation(text_fallback.payee, service_context)
        fallback_core_ready = bool(
            text_fallback.payee
            and text_fallback.payment_date
            and text_fallback.amount is not None
            and text_fallback.currency
        )
        fallback_payload = {
            "card_last4": None,
            "card_last4_matches_target": None,
            "payee": text_fallback.payee or None,
            "recipient_name": text_fallback.recipient_name or None,
            "recipient_name_matches_user": text_fallback.recipient_name_matches_user,
            "recipient_name_relation_reason": text_fallback.recipient_name_relation_reason,
            "filename_label": text_fallback.payee or None,
            "service_payee_related": fallback_relation,
            "service_payee_relation_reason": (
                "PDF埋め込みテキストの払先と、登録サービス・払先候補または入力メモに明確な文字関連があります。"
                if fallback_relation is True
                else ""
            ),
            "payment_date": text_fallback.payment_date.isoformat() if text_fallback.payment_date else None,
            "amount": str(text_fallback.amount) if text_fallback.amount is not None else None,
            "currency": text_fallback.currency or None,
            "financial_document_kind": text_fallback.financial_document_kind,
            "transaction_reference": text_fallback.transaction_reference or None,
            "related_transaction_reference": text_fallback.related_transaction_reference or None,
            "transaction_components": list(text_fallback.transaction_components),
            "can_create_filename": bool(fallback_core_ready and fallback_relation is True),
            "confidence": text_fallback.confidence,
            "reason": f"OpenAI API呼び出しに失敗しました: {exc.__class__.__name__}: {exc}",
        }
        fallback_payload, fallback_used = merge_payload_with_text_fallback(
            fallback_payload,
            text_fallback,
            service_context=service_context,
        )
        if fallback_used or any(
            (
                text_fallback.payment_date,
                text_fallback.amount is not None,
                text_fallback.currency,
                text_fallback.payee,
            )
        ):
            fallback_result = build_result_from_payload(
                fallback_payload,
                original_filename=original_filename,
                user_filename_part=user_filename_part,
                is_extra=is_extra,
            )
            fallback_memo = (
                f"OpenAI API呼び出しに失敗しましたが、PDF埋め込みテキストから抽出できる項目を補完しました: "
                f"{exc.__class__.__name__}: {exc}"
            )
            if fallback_result.admin_memo:
                fallback_memo += " " + fallback_result.admin_memo
            return replace(fallback_result, admin_memo=fallback_memo[:2000])

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
    expected_recipient_context: str = "",
    service_match_hints: str = "",
    receipt_memo: str = "",
    is_extra: bool = False,
    embedded_text: str = "",
) -> list[dict[str, Any]]:
    target = target_card_last4()
    if is_extra:
        context_lines = (
            "対象項目: その他（登録サービス外の追加領収書）\n"
            f"ユーザーが入力した必須メモ: {receipt_memo or '未入力'}\n"
            "このメモは、返金・プラン変更・追加請求など領収書の背景を理解するための参考情報です。"
            "ただし、払先、日付、金額、通貨、カード番号など領収書ファイル内の明確な記載を常に最優先してください。"
            "メモと領収書の内容が明確に矛盾する場合は、メモに合わせて推測せず service_payee_related を false にしてください。"
            "関連性を断定できない場合は null にしてください。\n"
        )
        relation_instruction = (
            "3. ユーザーのメモと、領収書上の払先・取引内容が同一または合理的に関連しているか確認する。"
            "例えば『OpenAIからの返金』というメモとOpenAIの返金領収書は関連あり、"
            "同じメモなのにAnthropicの通常請求書であれば関連なしとする。"
            "曖昧または確認できない場合は service_payee_related を null にする。\n"
        )
        filename_instruction = (
            "5. filename_label は、領収書本体で確認できた払先・取引内容を中心に、必須メモを補助情報として使って、"
            "ファイル名に適した短い名称を返す。例: OpenAI返金、ChatGPTプラン変更。"
            "領収書上の明確な払先と矛盾する名称をメモだけから作らない。"
            "メモ全体をそのままコピーせず、企業名または企業名+短い取引種別に要約する。\n"
        )
        relation_name = "メモと領収書内容"
    else:
        context_lines = (
            f"対象の登録サービス名: {service_display_name}\n"
            f"管理者が登録した払先・カード明細表記候補: {service_match_hints or '未設定'}\n"
        )
        relation_instruction = (
            "3. 対象の登録サービス名と領収書上の払先が同一または合理的に関連しているか確認する。"
            "完全一致だけで判定せず、ChatGPT と OpenAI、Claude と Anthropic のような運営会社・請求元の関係は関連ありとする。"
            "一方で ChatGPT の登録サービスなのに Anthropic の領収書、Claude の登録サービスなのに OpenAI の領収書のような組み合わせは関連なしとする。"
            "判断が曖昧、または払先やサービスとの関係を確認できない場合は service_payee_related を null にする。\n"
        )
        filename_instruction = (
            "5. filename_label は登録サービス名ではなく、領収書上の実際の払先から Inc. / LLC / PBC などの法人格表記を除いた短い企業名を返す。\n"
        )
        relation_name = "登録サービス名と払先"

    embedded_text_instruction = ""
    if embedded_text:
        embedded_text_instruction = (
            "\nこのPDFには検索可能な埋め込みテキストがあります。画像表示と同じ領収書内容です。"
            "表組みや小さい文字を読み落とさないため、次のテキストも必ず確認してください。"
            "画像とテキストが矛盾する場合は、最終支払額・支払日・払先が明確にラベル付けされた記載を優先してください。\n"
            "--- PDF埋め込みテキスト開始 ---\n"
            + embedded_text[:PDF_TEXT_MAX_CHARS]
            + "\n--- PDF埋め込みテキスト終了 ---\n"
        )

    return [
        build_file_input_item(file_bytes=file_bytes, filename=original_filename, content_type=content_type),
        {
            "type": "input_text",
            "text": (
                context_lines
                + f"ファイル名に使うユーザー名部分: {sanitize_filename_part(user_filename_part, fallback='user')}\n"
                + f"領収書の利用者名・宛名との照合に使う対象ユーザー情報: {expected_recipient_context or '氏名情報なし'}\n"
                + f"元ファイル名: {original_filename}\n"
                + "必ず次の順番で確認してください。\n"
                + f"1. 領収書内に支払カード末尾4桁の記載がある場合だけ、{target} で終わるか確認する。"
                + "カード末尾の記載がない、または読めない場合は null を返す。"
                + "カード情報がないことだけを理由にファイル名作成不可・確認失敗にしない。\n"
                + "2. 領収書内の実際の払先・販売者・請求元・merchant/payee を確認する。"
                + "画面上のサービス名やユーザー入力メモより、領収書に表示された請求元を優先する。"
                + "例えば ChatGPT（サブスク）の払先は OpenAI、Claude（サブスク）の払先は Anthropic のように判断する。\n"
                + "3. 領収書の Billed to、Bill to、Customer、Account holder、宛名、利用者名、ご使用者氏名、購入者名など、"
                + "支払先ではなく利用者側の名前を確認する。対象ユーザー情報と明確に対応する場合は "
                + "recipient_name_matches_user を true、明確に別人・別利用者の場合は false、"
                + "宛名がない・略称だけ・判断材料不足の場合は null にする。"
                + "メールアドレスのローカル部（例: keeseon.kim）と Keeseon Kim のような表記揺れは関連ありとしてよい。"
                + "会社名や部署名だけで個人との対応を断定できない場合は null にする。\n"
                + relation_instruction.replace("3. ", "4. ", 1)
                + "5. 書類本文から financial_document_kind を charge（支払済み領収書）、invoice（未支払を含む請求書）、refund（返金書類）、unknown のいずれかで返す。"
                + "ファイル名に『返金』とあっても本文が通常の請求書ならinvoiceとし、必ず本文を優先する。"
                + "返金書類では amount は元の請求額やAdjusted invoice totalではなく、Refund Amount、refunded、Credited total等の実返金額を正の絶対値で返す。\n"
                + "6. 支払日または領収書日付、実際に支払った最終合計金額、通貨を確認する。"
                + "金額は Amount paid、Total、Paid、支払額、領収金額などの最終支払額を優先し、"
                + "Subtotal、税抜額、税額、換算レート、参考表示だけを合計金額として返さない。"
                + "同じ金額が本文とPayment historyに重複していても1回の支払額として返す。\n"
                + "7. transaction_reference にはTransaction ID、Invoice number、Receipt number等の自身の取引識別子を返す。"
                + "返金書類にSale Transaction IDやOriginal Transaction IDがある場合は related_transaction_reference に元取引IDを返す。読めない場合はnull。\n"
                + "8. transaction_components には、このPDFで証拠確認できる金融イベントを重複なく列挙する。"
                + "通常の支払済み領収書はchargeを1件、返金書類はrefundを負数で1件返す。"
                + "返金書類のPayment history等に元のAmount paidと日付が明記される場合は、その元決済も別のchargeとして追加する。"
                + "未支払のInvoiceはdocument_kind=invoiceのchargeとして返してよいが、支払済みと推測しない。"
                + "signed_amountはchargeを正、refundを負にし、transaction_dateは各イベントの日付、"
                + "invoice_number、transaction_id、related_transaction_idを本文から読める範囲で返す。"
                + "本文の同じ取引がTotalとPayment historyに重複表示されても1構成要素にする。\n"
                + filename_instruction.replace("5. ", "9. ", 1)
                + "10. ファイル名はアプリ側で YYMMDD_ユーザー名_filename_label_金額_通貨 の形式に整形する。\n"
                + "11. can_create_filename は、払先・filename_label・日付・金額・通貨を高い確度で読め、"
                + f"さらに{relation_name}が関連すると確認できる場合に true にする。"
                + f"カード末尾{target}の一致は補助証拠であり、記載なしでも他の条件が揃えば true にする。"
                + "別のカード末尾が明記された場合は reason に記載し、管理者確認対象とする。"
                + "利用者名・宛名の一致はファイル名作成可否には含めず、独立した管理者確認項目として返す。"
                + "作成が難しい場合は false にし、reason に管理者が確認すべき理由を日本語で短く書く。"
                + embedded_text_instruction
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
                "recipient_name": {"type": ["string", "null"], "description": "領収書の利用者名、宛名、ご使用者氏名、購入者名、Billed to、Customer、Account holder。払先名ではない。"},
                "recipient_name_matches_user": {"type": ["boolean", "null"], "description": "領収書の利用者名・宛名が対象ユーザー情報と明確に対応するか。曖昧・記載なしの場合は null。"},
                "recipient_name_relation_reason": {"type": "string", "description": "利用者名・宛名と対象ユーザーの対応について、管理者が確認すべき根拠または不足情報。"},
                "filename_label": {"type": ["string", "null"], "description": "ファイル名に使う短い名称。通常は払先企業名。その他領収書では領収書内容を優先しつつ必須メモを補助情報にした企業名または企業名+短い取引種別。"},
                "service_payee_related": {"type": ["boolean", "null"], "description": "通常領収書では登録サービスと払先、その他領収書では必須メモと領収書内容が合理的に関連しているか。曖昧・確認不可の場合は null。"},
                "service_payee_relation_reason": {"type": "string", "description": "関連性について管理者が確認すべき理由や根拠。"},
                "payment_date": {"type": ["string", "null"], "description": "支払日または領収書日付。YYYY-MM-DD。"},
                "amount": {"type": ["number", "string", "null"], "description": "合計支払金額。"},
                "currency": {"type": ["string", "null"], "description": "ISO 4217通貨コード。例: JPY, USD。"},
                "financial_document_kind": {"type": "string", "enum": ["unknown", "charge", "invoice", "refund"], "description": "書類本文に基づく金融書類区分。"},
                "transaction_reference": {"type": ["string", "null"], "description": "この書類自身のTransaction ID、Invoice番号、Receipt番号など。"},
                "related_transaction_reference": {"type": ["string", "null"], "description": "返金元のSale Transaction IDなど、元取引との関連参照番号。"},
                "transaction_components": {
                    "type": "array",
                    "maxItems": 10,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "component_key": {"type": "string"},
                            "role": {"type": "string", "enum": ["charge", "refund"]},
                            "signed_amount": {"type": ["number", "string"]},
                            "currency": {"type": "string"},
                            "transaction_date": {"type": "string"},
                            "payee": {"type": ["string", "null"]},
                            "invoice_number": {"type": ["string", "null"]},
                            "transaction_id": {"type": ["string", "null"]},
                            "related_transaction_id": {"type": ["string", "null"]},
                            "source_label": {"type": "string"},
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            "document_kind": {"type": "string", "enum": ["unknown", "charge", "invoice", "refund"]},
                        },
                        "required": [
                            "component_key", "role", "signed_amount", "currency", "transaction_date",
                            "payee", "invoice_number", "transaction_id", "related_transaction_id",
                            "source_label", "confidence", "document_kind"
                        ],
                    },
                },
                "can_create_filename": {"type": "boolean"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reason": {"type": "string", "description": "作成不可または注意点がある場合の管理者向け理由。"},
            },
            "required": [
                "card_last4",
                "card_last4_matches_target",
                "payee",
                "recipient_name",
                "recipient_name_matches_user",
                "recipient_name_relation_reason",
                "filename_label",
                "service_payee_related",
                "service_payee_relation_reason",
                "payment_date",
                "amount",
                "currency",
                "financial_document_kind",
                "transaction_reference",
                "related_transaction_reference",
                "transaction_components",
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


def build_result_from_payload(
    payload: dict[str, Any],
    *,
    original_filename: str,
    user_filename_part: str = "",
    is_extra: bool = False,
) -> ReceiptFilenameResult:
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

    recipient_name = normalize_recipient_name(payload.get("recipient_name") or "")
    recipient_match_supplied = "recipient_name_matches_user" in payload
    recipient_name_matches_user = payload.get("recipient_name_matches_user")
    if recipient_name_matches_user is not None:
        recipient_name_matches_user = bool(recipient_name_matches_user)
    recipient_name_relation_reason = str(payload.get("recipient_name_relation_reason") or "").strip()

    payee = normalize_payee(payload.get("payee") or "")
    filename_label = normalize_filename_label(payload.get("filename_label") or payee)
    payment_date = parse_iso_date(payload.get("payment_date"))
    amount = parse_amount(payload.get("amount"))
    currency = normalize_currency(payload.get("currency") or "")
    financial_document_kind = str(payload.get("financial_document_kind") or ReceiptFinancialDocumentKind.UNKNOWN).strip().lower()
    if financial_document_kind not in ReceiptFinancialDocumentKind.values:
        financial_document_kind = ReceiptFinancialDocumentKind.UNKNOWN
    transaction_reference = str(payload.get("transaction_reference") or "").strip()[:160]
    related_transaction_reference = str(payload.get("related_transaction_reference") or "").strip()[:160]
    confidence = normalize_confidence(payload.get("confidence"))
    transaction_components = normalize_transaction_components(
        payload.get("transaction_components"),
        fallback_payee=payee,
        fallback_kind=financial_document_kind,
    )
    if not transaction_components and payment_date is not None and amount is not None and currency:
        role = "refund" if financial_document_kind == ReceiptFinancialDocumentKind.REFUND else "charge"
        signed_amount = -abs(amount) if role == "refund" else abs(amount)
        transaction_components = normalize_transaction_components(
            [
                {
                    "component_key": "primary",
                    "role": role,
                    "signed_amount": str(signed_amount),
                    "currency": currency,
                    "transaction_date": payment_date.isoformat(),
                    "payee": payee,
                    "invoice_number": transaction_reference if financial_document_kind != ReceiptFinancialDocumentKind.REFUND else "",
                    "transaction_id": transaction_reference,
                    "related_transaction_id": related_transaction_reference,
                    "source_label": "主要取引",
                    "confidence": confidence if "confidence" in payload else 0.8,
                    "document_kind": financial_document_kind,
                }
            ],
            fallback_payee=payee,
            fallback_kind=financial_document_kind,
        )
    can_create = bool(payload.get("can_create_filename", payload.get("can_generate_filename", False)))
    model_reason = str(payload.get("reason") or payload.get("admin_memo") or "").strip()

    if card_matches is None and card_last4:
        card_matches = card_last4 == target
    card_not_printed = card_matches is None and not card_last4
    core_filename_ready = bool(filename_label and payment_date is not None and amount is not None and currency)
    relation_ready = not service_relation_supplied or service_payee_related is True
    # 旧プロンプト準拠のモデルが「カード記載なし」だけを理由に false を返しても、
    # ファイル名作成に必要な主要項目が揃っていれば生成可能として正規化する。
    if (
        not can_create
        and card_not_printed
        and core_filename_ready
        and relation_ready
        and confidence >= 0.65
    ):
        can_create = True

    issues: list[str] = []
    if card_matches is False or (card_last4 and card_last4 != target):
        issues.append(f"カード末尾が {target} ではなく {card_last4} と読み取られました。")
    if not payee:
        issues.append("払先を確認できませんでした。")
    if not filename_label:
        issues.append("ファイル名に使う名称を確認できませんでした。")
    if service_relation_supplied and service_payee_related is not True:
        if service_payee_related is False:
            issues.append(
                "入力メモと領収書内容が関連していない可能性があります。"
                if is_extra
                else "登録サービス名と領収書の払先が関連していない可能性があります。"
            )
        else:
            issues.append(
                "入力メモと領収書内容の関連性を確認できませんでした。"
                if is_extra
                else "登録サービス名と領収書の払先の関連性を確認できませんでした。"
            )
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
    if filename_label and payment_date is not None and amount is not None and currency:
        suggested_filename = build_receipt_filename(
            payment_date=payment_date,
            user_filename_part=user_filename_part,
            payee=filename_label,
            amount=amount,
            currency=currency,
            extension=Path(original_filename).suffix.lower() or ".pdf",
        )

    result_kwargs = dict(
        suggested_filename=suggested_filename if can_create and not issues else "",
        payee=payee,
        filename_label=filename_label,
        payment_date=payment_date,
        amount=amount,
        currency=currency,
        card_last4=card_last4,
        card_last4_matches_target=card_matches,
        recipient_name=recipient_name,
        recipient_name_matches_user=(
            recipient_name_matches_user if recipient_match_supplied else None
        ),
        recipient_name_relation_reason=recipient_name_relation_reason,
        payee_confirmed=bool(payee),
        date_confirmed=payment_date is not None,
        amount_confirmed=amount is not None,
        currency_confirmed=bool(currency),
        service_payee_related=service_payee_related if service_relation_supplied else None,
        service_payee_relation_reason=service_relation_reason,
        financial_document_kind=financial_document_kind,
        transaction_reference=transaction_reference,
        related_transaction_reference=related_transaction_reference,
        transaction_components=transaction_components,
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


def build_result_from_ai_payload(
    payload: dict[str, Any],
    *,
    original_filename: str,
    user_filename_part: str = "",
    is_extra: bool = False,
) -> ReceiptFilenameResult:
    """旧テスト・旧実装名との互換用。"""

    return build_result_from_payload(
        payload,
        original_filename=original_filename,
        user_filename_part=user_filename_part,
        is_extra=is_extra,
    )


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


def recipient_reference_context_from_user(user: Any) -> str:
    """AIが利用者名・宛名を確認するための最小限の照合情報を作る。"""

    values: list[str] = []
    full_name = " ".join(
        part.strip()
        for part in (getattr(user, "first_name", ""), getattr(user, "last_name", ""))
        if part and part.strip()
    ).strip()
    if full_name:
        values.append(f"登録氏名={full_name}")

    email = str(getattr(user, "email", "") or getattr(user, "username", "") or "").strip()
    if email:
        values.append(f"アカウント={email}")
        local_part = email.split("@", 1)[0]
        readable_local_part = re.sub(r"[._\-]+", " ", local_part).strip()
        if readable_local_part:
            values.append(f"氏名候補={readable_local_part}")

    return " / ".join(dict.fromkeys(values))[:500]


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


def normalize_recipient_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").strip()
    value = re.sub(r"\s+", " ", value)
    value = re.sub(
        r"^(billed\s+to|bill\s+to|customer|account\s+holder|recipient|宛名|利用者名|ご使用者氏名|購入者名)\s*[:：]\s*",
        "",
        value,
        flags=re.I,
    )
    return value[:160]


def normalize_filename_label(value: str) -> str:
    """AIが返したファイル名用ラベルを表示可能な短い文字列へ正規化する。"""

    value = unicodedata.normalize("NFKC", value or "").strip()
    value = re.sub(r"\s+", " ", value)
    return value[:160]


def sanitize_company_name_for_filename(value: str, fallback: str = "Unknown") -> str:
    value = unicodedata.normalize("NFKC", value or "").strip()
    value = re.sub(
        r"\b(PBC|INCORPORATED|INC|LLC|L\.?L\.?C|LTD|LIMITED|CORPORATION|CORP|COMPANY|CO|GMBH|S\.?R\.?O\.?|S\.?A\.?|K\.?K\.?|G\.?K\.?)\b\.?",
        "",
        value,
        flags=re.I,
    )
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
