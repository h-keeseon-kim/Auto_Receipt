from __future__ import annotations

from pathlib import Path

from django.utils import timezone

from .ai_filename import generate_ai_receipt_filename
from .models import Receipt, ReceiptFilenameStatus, ReceiptPeriodCheckStatus


AI_RESET_FIELDS = [
    "generated_filename",
    "ai_filename_status",
    "ai_filename_admin_memo",
    "ai_filename_checked_at",
    "ai_extracted_payee",
    "ai_extracted_card_last4",
    "ai_receipt_month",
    "ai_period_check_status",
    "ai_period_check_memo",
]

AI_EXTRACTED_VALUE_FIELDS = ["amount", "currency", "issued_on"]


def reset_ai_processing_state(receipt: Receipt, *, save: bool = False, clear_extracted_values: bool = False) -> list[str]:
    """アップロード直後・ファイル差し替え直後にAI確認状態を未処理へ戻す。"""

    receipt.generated_filename = ""
    receipt.ai_filename_status = ReceiptFilenameStatus.NOT_PROCESSED
    receipt.ai_filename_admin_memo = ""
    receipt.ai_filename_checked_at = None
    receipt.ai_extracted_payee = ""
    receipt.ai_extracted_card_last4 = ""
    receipt.ai_receipt_month = ""
    receipt.ai_period_check_status = ReceiptPeriodCheckStatus.NOT_CHECKED
    receipt.ai_period_check_memo = ""

    update_fields = list(AI_RESET_FIELDS)
    if clear_extracted_values:
        receipt.amount = None
        receipt.currency = "JPY"
        receipt.issued_on = None
        update_fields.extend(AI_EXTRACTED_VALUE_FIELDS)

    if save:
        receipt.save(update_fields=[*update_fields, "updated_at"])
    return update_fields


def apply_period_check_to_receipt(receipt: Receipt, result) -> list[str]:
    """AIで抽出した日付が提出月と一致するかをReceiptに反映する。"""

    expected_month = receipt.submission.period_month if receipt.submission_id else None
    payment_date = getattr(result, "payment_date", None) if result is not None else None
    if payment_date:
        actual_month = payment_date.replace(day=1)
        receipt.ai_receipt_month = actual_month.strftime("%Y-%m")
        if expected_month and actual_month == expected_month:
            receipt.ai_period_check_status = ReceiptPeriodCheckStatus.MATCHED
            receipt.ai_period_check_memo = f"領収書日付 {payment_date:%Y-%m-%d} は提出月 {expected_month:%Y-%m} と一致しています。"
        elif expected_month:
            receipt.ai_period_check_status = ReceiptPeriodCheckStatus.MISMATCHED
            receipt.ai_period_check_memo = (
                f"領収書日付 {payment_date:%Y-%m-%d} は提出月 {expected_month:%Y-%m} と一致しません。"
                "ユーザーへ再アップロードを依頼してください。"
            )
        else:
            receipt.ai_period_check_status = ReceiptPeriodCheckStatus.UNKNOWN
            receipt.ai_period_check_memo = f"領収書日付 {payment_date:%Y-%m-%d} を抽出しましたが、提出月を確認できませんでした。"
    else:
        receipt.ai_receipt_month = ""
        if result is not None and getattr(result, "status", "") == ReceiptFilenameStatus.SKIPPED:
            receipt.ai_period_check_status = ReceiptPeriodCheckStatus.NOT_CHECKED
            receipt.ai_period_check_memo = "AIファイル名修正が未実行のため、提出月との一致確認も未実行です。"
        else:
            receipt.ai_period_check_status = ReceiptPeriodCheckStatus.UNKNOWN
            receipt.ai_period_check_memo = "領収書日付をAIで確認できなかったため、提出月との一致確認はできませんでした。"
    return ["ai_receipt_month", "ai_period_check_status", "ai_period_check_memo"]


def apply_ai_filename_to_receipt(receipt: Receipt):
    if not receipt.file_available:
        return None

    try:
        with receipt.file.open("rb") as file_obj:
            file_bytes = file_obj.read()
    except Exception as exc:
        receipt.generated_filename = ""
        receipt.ai_filename_status = ReceiptFilenameStatus.FAILED
        receipt.ai_filename_admin_memo = f"AIファイル名作成前にファイルを読み込めませんでした: {exc}"
        receipt.ai_filename_checked_at = timezone.now()
        update_fields = [
            "generated_filename",
            "ai_filename_status",
            "ai_filename_admin_memo",
            "ai_filename_checked_at",
            *apply_period_check_to_receipt(receipt, None),
            "updated_at",
        ]
        receipt.save(update_fields=update_fields)
        return None

    result = generate_ai_receipt_filename(
        file_bytes=file_bytes,
        original_filename=receipt.original_filename or Path(receipt.file.name).name,
        content_type=receipt.content_type,
        service_display_name=receipt.service_display_name_snapshot,
    )

    receipt.generated_filename = result.suggested_filename[:255] if result.suggested_filename else ""
    receipt.ai_filename_status = result.status
    receipt.ai_filename_admin_memo = result.admin_memo
    receipt.ai_filename_checked_at = timezone.now()
    receipt.ai_extracted_payee = result.payee[:160] if result.payee else ""
    receipt.ai_extracted_card_last4 = result.card_last4[-4:] if result.card_last4 else ""

    update_fields = [
        "generated_filename",
        "ai_filename_status",
        "ai_filename_admin_memo",
        "ai_filename_checked_at",
        "ai_extracted_payee",
        "ai_extracted_card_last4",
        *apply_period_check_to_receipt(receipt, result),
        "updated_at",
    ]
    if result.status == ReceiptFilenameStatus.GENERATED:
        if result.payment_date is not None:
            receipt.issued_on = result.payment_date
            update_fields.append("issued_on")
        if result.amount is not None:
            receipt.amount = result.amount
            update_fields.append("amount")
        if result.currency:
            receipt.currency = result.currency
            update_fields.append("currency")

    receipt.save(update_fields=update_fields)
    return result
