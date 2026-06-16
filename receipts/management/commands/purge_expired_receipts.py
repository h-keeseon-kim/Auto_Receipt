from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from receipts.models import Receipt


class Command(BaseCommand):
    help = "保存期限を過ぎた領収書ファイルを削除し、提出メタデータは残します。"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="削除対象を表示するだけでファイルを削除しません。")
        parser.add_argument("--batch-size", type=int, default=500, help="一度に処理する最大件数。")
        parser.add_argument("--noinput", action="store_true", help="Railway Cron 等の非対話実行用。")

    def handle(self, *args, **options):
        now = timezone.now()
        dry_run = options["dry_run"]
        batch_size = options["batch_size"]
        queryset = Receipt.objects.expired().select_related("submission", "submission__user").order_by("expires_at")[:batch_size]
        receipts = list(queryset)

        if not receipts:
            self.stdout.write(self.style.SUCCESS("期限切れの保存中ファイルはありません。"))
            close_old_connections()
            return

        purged = 0
        for receipt in receipts:
            label = f"#{receipt.id} {receipt.submission.user} {receipt.submission.period_month:%Y-%m} {receipt.service_name_snapshot} expires_at={receipt.expires_at}"
            if dry_run:
                self.stdout.write(f"DRY-RUN {label}")
                continue
            if receipt.purge_file(reason="expired"):
                purged += 1
                self.stdout.write(f"PURGED {label}")

        if dry_run:
            self.stdout.write(self.style.WARNING(f"dry-run: {len(receipts)}件が削除対象です。基準時刻: {now.isoformat()}"))
        else:
            self.stdout.write(self.style.SUCCESS(f"{purged}件の領収書ファイルを削除しました。メタデータは保持されています。"))
        close_old_connections()
