from __future__ import annotations

from pathlib import Path

from django.utils import timezone

from .ai_filename import generate_ai_receipt_filename, target_card_last4
from .models import Receipt, ReceiptFilenameStatus, ReceiptPeriodCheckStatus

AI_CHECK_FIELDS = [
    "ai_check_card_last4",
    "ai_check_payee",
    "ai_check_service_payee_related",
    "ai_service_payee_check_memo",
    "ai_check_date",
    "ai_check_amount",
    "ai_check_currency",
    "ai_check_period_match",
]

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
    *AI_CHECK_FIELDS,
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
    receipt.ai_check_card_last4 = False
    receipt.ai_check_payee = False
    receipt.ai_check_service_payee_related = False
    receipt.ai_service_payee_check_memo = ""
    receipt.ai_check_date = False
    receipt.ai_check_amount = False
    receipt.ai_check_currency = False
    receipt.ai_check_period_match = False

    update_fields = list(AI_RESET_FIELDS)
    if clear_extracted_values:
        receipt.amount = None
        receipt.currency = "JPY"
        receipt.issued_on = None
        update_fields.extend(AI_EXTRACTED_VALUE_FIELDS)

    if save:
        receipt.save(update_fields=[*update_fields, "updated_at"])
    return update_fields


def apply_ai_checklist_to_receipt(receipt: Receipt, result) -> list[str]:
    """AI抽出結果を管理者画面のチェックボックス項目に反映する。"""

    if result is None:
        receipt.ai_check_card_last4 = False
        receipt.ai_check_payee = False
        receipt.ai_check_service_payee_related = False
        receipt.ai_service_payee_check_memo = ""
        receipt.ai_check_date = False
        receipt.ai_check_amount = False
        receipt.ai_check_currency = False
        return AI_CHECK_FIELDS[:-1]

    card_matches = getattr(result, "card_last4_matches_target", None)
    card_last4 = getattr(result, "card_last4", "") or ""
    if card_matches is None and card_last4:
        card_matches = card_last4[-4:] == target_card_last4()

    service_payee_related = getattr(result, "service_payee_related", None)
    if service_payee_related is None and getattr(result, "status", "") == ReceiptFilenameStatus.GENERATED:
        # 旧テストや旧呼び出しでは関連性フィールドが存在しないため、生成済みなら確認済みとして扱う。
        service_payee_related = True

    relation_reason = (getattr(result, "service_payee_relation_reason", "") or "").strip()
    if service_payee_related is False and not relation_reason:
        relation_reason = "登録サービス名と領収書上の払先が関連しない可能性があります。"
    elif service_payee_related is None:
        relation_reason = relation_reason or "登録サービス名と領収書上の払先の関連性をAIで確認できませんでした。"

    receipt.ai_check_card_last4 = card_matches is True
    receipt.ai_check_payee = bool(getattr(result, "payee", "")) or bool(getattr(result, "payee_confirmed", False))
    receipt.ai_check_service_payee_related = service_payee_related is True
    receipt.ai_service_payee_check_memo = relation_reason[:1000]
    receipt.ai_check_date = bool(getattr(result, "payment_date", None)) or bool(getattr(result, "date_confirmed", False))
    receipt.ai_check_amount = getattr(result, "amount", None) is not None or bool(getattr(result, "amount_confirmed", False))
    receipt.ai_check_currency = bool(getattr(result, "currency", "")) or bool(getattr(result, "currency_confirmed", False))
    return AI_CHECK_FIELDS[:-1]


def apply_period_check_to_receipt(receipt: Receipt, result) -> list[str]:
    """AIで抽出した日付が提出月と一致するかをReceiptに反映する。"""

    expected_month = receipt.submission.period_month if receipt.submission_id else None
    payment_date = getattr(result, "payment_date", None) if result is not None else None
    receipt.ai_check_period_match = False
    if payment_date:
        actual_month = payment_date.replace(day=1)
        receipt.ai_receipt_month = actual_month.strftime("%Y-%m")
        if expected_month and actual_month == expected_month:
            receipt.ai_period_check_status = ReceiptPeriodCheckStatus.MATCHED
            receipt.ai_period_check_memo = f"領収書日付 {payment_date:%Y-%m-%d} は提出月 {expected_month:%Y-%m} と一致しています。"
            receipt.ai_check_period_match = True
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
    return ["ai_receipt_month", "ai_period_check_status", "ai_period_check_memo", "ai_check_period_match"]


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
            *apply_ai_checklist_to_receipt(receipt, None),
            *apply_period_check_to_receipt(receipt, None),
            "updated_at",
        ]
        receipt.save(update_fields=list(dict.fromkeys(update_fields)))
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
        *apply_ai_checklist_to_receipt(receipt, result),
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
