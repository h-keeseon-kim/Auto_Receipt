from __future__ import annotations

from django.core.management.base import BaseCommand

from receipts.ai_processing import claim_pending_receipts_for_ai_processing, process_claimed_receipts
from receipts.models import Receipt, ReceiptFilenameStatus


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
            help="失敗・要確認の領収書も再処理対象に含めます。手動運用では通常使いません。",
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

        queryset = Receipt.objects.available_files().select_related("submission", "submission__user", "service")
        if receipt_ids:
            queryset = queryset.filter(pk__in=receipt_ids)
        if retry_failed:
            retry_queryset = queryset.filter(ai_filename_status__in=[ReceiptFilenameStatus.FAILED, ReceiptFilenameStatus.NEEDS_REVIEW])
            retry_queryset.update(ai_filename_status=ReceiptFilenameStatus.NOT_PROCESSED, ai_filename_checked_at=None)

        claimed_ids = claim_pending_receipts_for_ai_processing(queryset, limit=limit)
        if not claimed_ids:
            self.stdout.write(self.style.SUCCESS("AI処理待ちの領収書はありません。"))
            return

        summary = process_claimed_receipts(claimed_ids)
        for receipt_id in claimed_ids:
            self.stdout.write(f"Receipt {receipt_id}: processed")

        self.stdout.write(
            self.style.SUCCESS(
                "AI領収書処理が完了しました: "
                f"processed={summary['processed']}, generated={summary['generated']}, "
                f"needs_review={summary['needs_review']}, mismatched={summary['mismatched']}, "
                f"skipped={summary['skipped']}, failed={summary['failed']}"
            )
        )
