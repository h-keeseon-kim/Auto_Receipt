from __future__ import annotations

import logging
import threading
from collections.abc import Iterable
from datetime import timedelta
from pathlib import Path

from django.db import close_old_connections, transaction
from django.db.models import QuerySet
from django.utils import timezone

from .ai_filename import filename_user_part_from_user, generate_ai_receipt_filename, target_card_last4
from .models import (
    Receipt,
    ReceiptAdminReviewStatus,
    ReceiptFilenameStatus,
    ReceiptPeriodCheckStatus,
    ReceiptResubmissionRequest,
    SubmissionStatus,
    receipt_month_for_submission,
)

logger = logging.getLogger(__name__)

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
    "admin_review_status",
    "admin_reviewed_by",
    "admin_reviewed_at",
    "admin_review_note",
    "admin_filename_overridden",
    *AI_CHECK_FIELDS,
]

AI_EXTRACTED_VALUE_FIELDS = ["amount", "currency", "issued_on"]
PROCESSING_MEMO = "AIで情報を抽出中です。完了した領収書から管理者画面に反映されます。"


def definite_ai_rejection_reasons(receipt: Receipt, result) -> list[str]:
    """AIが明確に不一致と判定した項目だけを再提出理由として返す。

    読み取り不可・曖昧（None）は自動削除せず、管理者の手動確認対象として残す。
    """

    if result is None:
        return []

    reasons: list[str] = []
    card_match = getattr(result, "card_last4_matches_target", None)
    card_last4 = (getattr(result, "card_last4", "") or "")[-4:]
    if card_match is False or (card_match is None and card_last4 and card_last4 != target_card_last4()):
        reasons.append(
            f"支払カード末尾が {target_card_last4()} と一致しません"
            + (f"（読み取り値: {card_last4}）" if card_last4 else "")
        )

    if getattr(result, "service_payee_related", None) is False:
        payee = (getattr(result, "payee", "") or "").strip()
        reasons.append(
            (
                "入力メモと領収書の内容が一致していません"
                if receipt.is_extra
                else "登録サービスと領収書の払先が一致していません"
            )
            + (f"（払先: {payee}）" if payee else "")
        )

    if receipt.ai_period_check_status == ReceiptPeriodCheckStatus.MISMATCHED:
        target_receipt_month = receipt_month_for_submission(receipt.submission.period_month)
        reasons.append(
            f"領収書の対象月（{receipt.ai_receipt_month or '不明'}）が、"
            f"提出月 {receipt.submission.period_month:%Y-%m} の対象領収書月（{target_receipt_month:%Y-%m}）と一致しません"
        )
    return list(dict.fromkeys(reasons))


def remove_receipt_for_automatic_resubmission(receipt_id: int, reasons: list[str]) -> bool:
    """明確な不一致がある領収書を提出項目から外し、再提出依頼を残す。"""

    if not reasons:
        return False
    with transaction.atomic():
        receipt = (
            Receipt.objects.select_for_update()
            .select_related("submission", "submission__user")
            .filter(pk=receipt_id)
            .first()
        )
        if receipt is None:
            return False
        submission = receipt.submission
        reason_text = "、".join(reasons)
        receipt_context = receipt.service_display_name_snapshot
        if receipt.is_extra and receipt.memo:
            receipt_context = f"{receipt_context}（{receipt.memo}）"
        ReceiptResubmissionRequest.objects.create(
            user=submission.user,
            period_month=submission.period_month,
            service_name_snapshot=receipt.service_name_snapshot,
            billing_type_snapshot=receipt.billing_type_snapshot,
            is_extra=receipt.is_extra,
            receipt_memo_snapshot=receipt.memo,
            original_receipt_id=receipt.pk,
            original_filename=receipt.original_filename,
            display_filename=receipt.display_filename,
            message=(
                f"自動確認の結果、{submission.period_month:%Y年%m月}提出"
                f"（対象領収書月: {submission.target_receipt_month:%Y年%m月}）の "
                f"{receipt_context} の領収書に明確な不一致が見つかりました。"
                f"理由: {reason_text}。該当ファイルは提出項目から取り下げました。"
                "内容を確認し、正しい領収書を再度アップロードしてください。"
            ),
            created_by=None,
        )
        receipt.delete()
        if submission.status == SubmissionStatus.SUBMITTED:
            submission.status = SubmissionStatus.DRAFT
            submission.submitted_at = None
            submission.save(update_fields=["status", "submitted_at", "updated_at"])
    return True


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
    receipt.admin_review_status = ReceiptAdminReviewStatus.NOT_REVIEWED
    receipt.admin_reviewed_by = None
    receipt.admin_reviewed_at = None
    receipt.admin_review_note = ""
    receipt.admin_filename_overridden = False
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
        relation_reason = (
            "入力メモと領収書内容が関連しない可能性があります。"
            if receipt.is_extra
            else "登録サービス名と領収書上の払先が関連しない可能性があります。"
        )
    elif service_payee_related is None:
        relation_reason = relation_reason or (
            "入力メモと領収書内容の関連性をAIで確認できませんでした。"
            if receipt.is_extra
            else "登録サービス名と領収書上の払先の関連性をAIで確認できませんでした。"
        )

    receipt.ai_check_card_last4 = card_matches is True
    receipt.ai_check_payee = bool(getattr(result, "payee", "")) or bool(getattr(result, "payee_confirmed", False))
    receipt.ai_check_service_payee_related = service_payee_related is True
    receipt.ai_service_payee_check_memo = relation_reason[:1000]
    receipt.ai_check_date = bool(getattr(result, "payment_date", None)) or bool(getattr(result, "date_confirmed", False))
    receipt.ai_check_amount = getattr(result, "amount", None) is not None or bool(getattr(result, "amount_confirmed", False))
    receipt.ai_check_currency = bool(getattr(result, "currency", "")) or bool(getattr(result, "currency_confirmed", False))
    return AI_CHECK_FIELDS[:-1]


def apply_period_check_to_receipt(receipt: Receipt, result) -> list[str]:
    """AIで抽出した日付が、提出月の前月に当たる領収書月かを反映する。"""

    submission_month = receipt.submission.period_month if receipt.submission_id else None
    expected_receipt_month = receipt_month_for_submission(submission_month) if submission_month else None
    payment_date = getattr(result, "payment_date", None) if result is not None else None
    receipt.ai_check_period_match = False
    if payment_date:
        actual_month = payment_date.replace(day=1)
        receipt.ai_receipt_month = actual_month.strftime("%Y-%m")
        if expected_receipt_month and actual_month == expected_receipt_month:
            receipt.ai_period_check_status = ReceiptPeriodCheckStatus.MATCHED
            receipt.ai_period_check_memo = (
                f"領収書日付 {payment_date:%Y-%m-%d} は、提出月 {submission_month:%Y-%m} の "
                f"対象領収書月 {expected_receipt_month:%Y-%m} と一致しています。"
            )
            receipt.ai_check_period_match = True
        elif expected_receipt_month:
            receipt.ai_period_check_status = ReceiptPeriodCheckStatus.MISMATCHED
            receipt.ai_period_check_memo = (
                f"領収書日付 {payment_date:%Y-%m-%d} は、提出月 {submission_month:%Y-%m} の "
                f"対象領収書月 {expected_receipt_month:%Y-%m} と一致しません。"
                "ユーザーへ再アップロードを依頼してください。"
            )
        else:
            receipt.ai_period_check_status = ReceiptPeriodCheckStatus.UNKNOWN
            receipt.ai_period_check_memo = (
                f"領収書日付 {payment_date:%Y-%m-%d} を抽出しましたが、対象領収書月を確認できませんでした。"
            )
    else:
        receipt.ai_receipt_month = ""
        if result is not None and getattr(result, "status", "") == ReceiptFilenameStatus.SKIPPED:
            receipt.ai_period_check_status = ReceiptPeriodCheckStatus.NOT_CHECKED
            receipt.ai_period_check_memo = "AIファイル名修正が未実行のため、対象領収書月との一致確認も未実行です。"
        else:
            receipt.ai_period_check_status = ReceiptPeriodCheckStatus.UNKNOWN
            receipt.ai_period_check_memo = "領収書日付をAIで確認できなかったため、対象領収書月との一致確認はできませんでした。"
    return ["ai_receipt_month", "ai_period_check_status", "ai_period_check_memo", "ai_check_period_match"]


def mark_receipt_ai_failed(receipt: Receipt, message: str):
    receipt.generated_filename = ""
    receipt.ai_filename_status = ReceiptFilenameStatus.FAILED
    receipt.ai_filename_admin_memo = message[:2000]
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


def mark_receipt_ai_skipped(receipt: Receipt, message: str):
    receipt.generated_filename = ""
    receipt.ai_filename_status = ReceiptFilenameStatus.SKIPPED
    receipt.ai_filename_admin_memo = message[:2000]
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


def apply_ai_filename_to_receipt(receipt: Receipt):
    if not receipt.file_available:
        mark_receipt_ai_skipped(receipt, "領収書ファイルが保存されていないため、AI確認をスキップしました。")
        return None

    try:
        with receipt.file.open("rb") as file_obj:
            file_bytes = file_obj.read()
    except Exception as exc:
        mark_receipt_ai_failed(receipt, f"AIファイル名作成前にファイルを読み込めませんでした: {exc}")
        return None

    result = generate_ai_receipt_filename(
        file_bytes=file_bytes,
        original_filename=receipt.original_filename or Path(receipt.file.name).name,
        content_type=receipt.content_type,
        service_display_name=receipt.service_display_name_snapshot,
        user_filename_part=filename_user_part_from_user(receipt.submission.user),
        service_match_hints=(
            receipt.service.catalog_service.merchant_aliases
            if receipt.service_id and receipt.service.catalog_service_id and receipt.service.catalog_service
            else ""
        ),
        receipt_memo=receipt.memo,
        is_extra=receipt.is_extra,
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

    receipt.save(update_fields=list(dict.fromkeys(update_fields)))

    # 明確な不一致だけは自動的に提出項目から取り下げる。
    # 読み取り不可・曖昧な項目は削除せず、管理者画面の要確認ステータスに残す。
    reasons = definite_ai_rejection_reasons(receipt, result)
    if reasons:
        remove_receipt_for_automatic_resubmission(receipt.pk, reasons)
    return result


def reset_stale_ai_processing_receipts(queryset: QuerySet | None = None, *, stale_after_minutes: int = 30) -> int:
    """途中で中断されたAI抽出中レコードを未確認へ戻す。

    Webプロセス再起動などでバックグラウンドスレッドが終了した場合に、
    管理者が次回ボタンを押したタイミングで再実行できるようにする。
    """

    base = queryset if queryset is not None else Receipt.objects.all()
    cutoff = timezone.now() - timedelta(minutes=max(int(stale_after_minutes), 1))
    return (
        base.available_files()
        .filter(ai_filename_status=ReceiptFilenameStatus.PROCESSING, updated_at__lt=cutoff)
        .update(
            ai_filename_status=ReceiptFilenameStatus.NOT_PROCESSED,
            ai_filename_admin_memo="前回のAI抽出が中断されたため、再実行可能に戻しました。",
            ai_filename_checked_at=None,
            updated_at=timezone.now(),
        )
    )


def pending_ai_receipts_queryset(queryset: QuerySet | None = None) -> QuerySet:
    """管理者の手動AI処理ボタンで対象にする領収書。

    既に生成済み・要確認・失敗・スキップ済みの領収書は再検査しない。
    """

    base = queryset if queryset is not None else Receipt.objects.all()
    return base.available_files().filter(ai_filename_status=ReceiptFilenameStatus.NOT_PROCESSED)


def claim_pending_receipts_for_ai_processing(queryset: QuerySet, *, limit: int | None = None) -> list[int]:
    """未処理の領収書だけをAI抽出中として確保し、処理対象IDを返す。"""

    reset_stale_ai_processing_receipts(queryset)
    queryset = pending_ai_receipts_queryset(queryset).order_by("uploaded_at", "pk")
    if limit is not None:
        queryset = queryset[: max(int(limit), 0)]

    with transaction.atomic():
        ids = list(queryset.values_list("pk", flat=True))
        if not ids:
            return []
        now = timezone.now()
        Receipt.objects.filter(pk__in=ids, ai_filename_status=ReceiptFilenameStatus.NOT_PROCESSED).update(
            generated_filename="",
            ai_filename_status=ReceiptFilenameStatus.PROCESSING,
            ai_filename_admin_memo=PROCESSING_MEMO,
            ai_filename_checked_at=None,
            ai_extracted_payee="",
            ai_extracted_card_last4="",
            ai_receipt_month="",
            ai_period_check_status=ReceiptPeriodCheckStatus.NOT_CHECKED,
            ai_period_check_memo="",
            ai_check_card_last4=False,
            ai_check_payee=False,
            ai_check_service_payee_related=False,
            ai_service_payee_check_memo="",
            ai_check_date=False,
            ai_check_amount=False,
            ai_check_currency=False,
            ai_check_period_match=False,
            admin_review_status=ReceiptAdminReviewStatus.NOT_REVIEWED,
            admin_reviewed_by=None,
            admin_reviewed_at=None,
            admin_review_note="",
            admin_filename_overridden=False,
            updated_at=now,
        )
        return list(
            Receipt.objects.filter(pk__in=ids, ai_filename_status=ReceiptFilenameStatus.PROCESSING)
            .order_by("uploaded_at", "pk")
            .values_list("pk", flat=True)
        )


def process_claimed_receipt(receipt_id: int):
    receipt = Receipt.objects.select_related("submission", "submission__user", "service", "service__catalog_service").get(pk=receipt_id)
    if receipt.ai_filename_status != ReceiptFilenameStatus.PROCESSING:
        return None
    return apply_ai_filename_to_receipt(receipt)


def process_claimed_receipts(receipt_ids: Iterable[int]) -> dict[str, int]:
    summary = {"processed": 0, "generated": 0, "needs_review": 0, "failed": 0, "skipped": 0, "mismatched": 0, "rejected": 0}
    for receipt_id in receipt_ids:
        try:
            result = process_claimed_receipt(int(receipt_id))
            receipt = Receipt.objects.only("ai_filename_status", "ai_period_check_status").filter(pk=receipt_id).first()
        except Exception as exc:  # pragma: no cover - スレッド内の最終防衛。
            logger.exception("Receipt %s AI processing failed unexpectedly", receipt_id)
            try:
                receipt = Receipt.objects.select_related("submission").get(pk=receipt_id)
                mark_receipt_ai_failed(receipt, f"AI処理中に予期しないエラーが発生しました: {exc.__class__.__name__}: {exc}")
            except Exception:
                logger.exception("Receipt %s could not be marked as failed", receipt_id)
            summary["failed"] += 1
            continue

        summary["processed"] += 1
        if receipt is None:
            summary["rejected"] += 1
            _ = result
            continue
        if receipt.ai_filename_status == ReceiptFilenameStatus.GENERATED:
            summary["generated"] += 1
        elif receipt.ai_filename_status == ReceiptFilenameStatus.NEEDS_REVIEW:
            summary["needs_review"] += 1
        elif receipt.ai_filename_status == ReceiptFilenameStatus.FAILED:
            summary["failed"] += 1
        elif receipt.ai_filename_status == ReceiptFilenameStatus.SKIPPED:
            summary["skipped"] += 1
        if receipt.ai_period_check_status == ReceiptPeriodCheckStatus.MISMATCHED:
            summary["mismatched"] += 1
        _ = result
    return summary


def start_background_ai_processing(receipt_ids: Iterable[int]) -> threading.Thread | None:
    ids = [int(receipt_id) for receipt_id in receipt_ids]
    if not ids:
        return None

    def worker():
        close_old_connections()
        try:
            process_claimed_receipts(ids)
        finally:
            close_old_connections()

    thread = threading.Thread(target=worker, name="receipt-ai-manual-processing", daemon=True)
    thread.start()
    return thread
