from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Q

from receipts.ai_processing import apply_ai_filename_to_receipt
from receipts.models import Receipt, ReceiptFilenameStatus, ReceiptPeriodCheckStatus


class Command(BaseCommand):
    help = "未処理の領収書に対してAIファイル名修正・提出月確認を実行します。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=50,
            help="1回の実行で処理する最大件数。デフォルトは50件です。",
        )
        parser.add_argument(
            "--retry-failed",
            action="store_true",
            help="失敗・要確認の領収書も再処理対象に含めます。",
        )
        parser.add_argument(
            "--receipt-id",
            type=int,
            action="append",
            dest="receipt_ids",
            help="特定の領収書IDだけを処理します。複数指定できます。",
        )

    def handle(self, *args, **options):
        limit = max(int(options["limit"] or 0), 1)
        receipt_ids = options.get("receipt_ids") or []
        retry_failed = bool(options["retry_failed"])

        filters = Q(ai_filename_status=ReceiptFilenameStatus.NOT_PROCESSED) | Q(
            ai_period_check_status=ReceiptPeriodCheckStatus.NOT_CHECKED
        )
        if retry_failed:
            filters |= Q(ai_filename_status__in=[ReceiptFilenameStatus.FAILED, ReceiptFilenameStatus.NEEDS_REVIEW])

        queryset = (
            Receipt.objects.available_files()
            .select_related("submission", "submission__user", "service")
            .filter(filters)
            .order_by("uploaded_at", "pk")
        )
        if receipt_ids:
            queryset = queryset.filter(pk__in=receipt_ids)

        receipts = list(queryset[:limit])
        if not receipts:
            self.stdout.write(self.style.SUCCESS("AI処理待ちの領収書はありません。"))
            return

        processed = 0
        failed = 0
        needs_review = 0
        generated = 0
        skipped = 0
        mismatched = 0

        for receipt in receipts:
            try:
                result = apply_ai_filename_to_receipt(receipt)
                receipt.refresh_from_db(fields=["ai_filename_status", "ai_period_check_status"])
            except Exception as exc:  # apply_ai_filename_to_receipt内でも通常は失敗保存するが、想定外エラーを隔離する。
                failed += 1
                self.stderr.write(f"Receipt {receipt.pk}: AI処理に失敗しました: {exc.__class__.__name__}: {exc}")
                continue

            processed += 1
            if receipt.ai_filename_status == ReceiptFilenameStatus.GENERATED:
                generated += 1
            elif receipt.ai_filename_status == ReceiptFilenameStatus.NEEDS_REVIEW:
                needs_review += 1
            elif receipt.ai_filename_status == ReceiptFilenameStatus.FAILED:
                failed += 1
            elif receipt.ai_filename_status == ReceiptFilenameStatus.SKIPPED:
                skipped += 1
            if receipt.ai_period_check_status == ReceiptPeriodCheckStatus.MISMATCHED:
                mismatched += 1

            result_status = getattr(result, "status", receipt.ai_filename_status) if result is not None else receipt.ai_filename_status
            self.stdout.write(f"Receipt {receipt.pk}: {result_status} / period={receipt.ai_period_check_status}")

        self.stdout.write(
            self.style.SUCCESS(
                "AI領収書処理が完了しました: "
                f"processed={processed}, generated={generated}, needs_review={needs_review}, "
                f"mismatched={mismatched}, skipped={skipped}, failed={failed}"
            )
        )
