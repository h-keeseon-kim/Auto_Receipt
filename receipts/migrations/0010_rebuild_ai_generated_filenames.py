from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.db import migrations


def sanitize_filename_part(value: str, fallback: str = "Unknown") -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).strip()
    value = re.sub(r"[\\/\0\r\n\t:*?\"<>|]+", "", value)
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"_+", "_", value)
    value = value.strip("._- ")
    return (value or fallback)[:80]


def format_amount_for_filename(amount) -> str:
    try:
        normalized = Decimal(str(amount)).copy_abs().quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return sanitize_filename_part(str(amount), fallback="0")
    if normalized == normalized.to_integral():
        return str(normalized.to_integral())
    return f"{normalized:.2f}".rstrip("0").rstrip(".")


def filename_user_part(user) -> str:
    last_name = sanitize_filename_part(getattr(user, "last_name", ""), fallback="")
    if last_name:
        return last_name
    value = getattr(user, "email", "") or getattr(user, "username", "") or ""
    if "@" in value:
        value = value.split("@", 1)[0]
    return sanitize_filename_part(value, fallback="user")


def company_name_for_filename(value: str, fallback: str = "Unknown") -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).strip()
    value = re.sub(
        r"\b(PBC|INCORPORATED|INC|LLC|L\.?L\.?C|LTD|LIMITED|CORPORATION|CORP|COMPANY|CO|GMBH|S\.?A\.?|K\.?K\.?|G\.?K\.?)\b\.?,?",
        "",
        value,
        flags=re.I,
    )
    value = re.sub(r"[,、，]+", " ", value)
    return sanitize_filename_part(value, fallback=fallback)


def extension_for(receipt) -> str:
    for value in [receipt.original_filename, receipt.generated_filename, str(receipt.file or "")]:
        suffix = Path(value or "").suffix.lower()
        if suffix:
            return suffix
    return ".pdf"


def rebuild_ai_generated_filenames(apps, schema_editor):
    Receipt = apps.get_model("receipts", "Receipt")

    receipts = (
        Receipt.objects.select_related("submission__user")
        .filter(ai_filename_status="generated")
        .exclude(generated_filename="")
    )
    for receipt in receipts:
        if not receipt.issued_on or receipt.amount is None or not receipt.currency or not receipt.ai_extracted_payee:
            continue
        user = receipt.submission.user
        filename = "_".join(
            [
                receipt.issued_on.strftime("%y%m%d"),
                filename_user_part(user),
                company_name_for_filename(receipt.ai_extracted_payee),
                format_amount_for_filename(receipt.amount),
                sanitize_filename_part(str(receipt.currency).upper(), fallback="CUR"),
            ]
        ) + extension_for(receipt)
        if filename and filename != receipt.generated_filename:
            receipt.generated_filename = filename[:255]
            receipt.save(update_fields=["generated_filename"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("receipts", "0009_receipt_ai_manual_processing"),
    ]

    operations = [
        migrations.RunPython(rebuild_ai_generated_filenames, noop_reverse),
    ]
