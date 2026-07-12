from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from receipts.models import CardStatement, Receipt


class Command(BaseCommand):
    help = "保存期限を過ぎた領収書・カード明細ファイルを削除し、メタデータは残します。"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="削除対象を表示するだけでファイルを削除しません。")
        parser.add_argument("--batch-size", type=int, default=500, help="ファイル種別ごとに一度に処理する最大件数。")
        parser.add_argument("--noinput", action="store_true", help="Railway Cron 等の非対話実行用。")

    def handle(self, *args, **options):
        now = timezone.now()
        dry_run = options["dry_run"]
        batch_size = max(int(options["batch_size"]), 1)
        receipts = list(
            Receipt.objects.expired()
            .select_related("submission", "submission__user")
            .order_by("expires_at")[:batch_size]
        )
        statements = list(
            CardStatement.objects.filter(
                file_deleted_at__isnull=True,
                expires_at__lte=now,
            )
            .exclude(file="")
            .select_related("user")
            .order_by("expires_at")[:batch_size]
        )

        if not receipts and not statements:
            self.stdout.write(self.style.SUCCESS("期限切れの保存中ファイルはありません。"))
            close_old_connections()
            return

        receipt_purged = 0
        for receipt in receipts:
            label = (
                f"領収書#{receipt.id} {receipt.submission.user} "
                f"{receipt.submission.period_month:%Y-%m} {receipt.service_name_snapshot} "
                f"expires_at={receipt.expires_at}"
            )
            if dry_run:
                self.stdout.write(f"DRY-RUN {label}")
                continue
            if receipt.purge_file(reason="expired"):
                receipt_purged += 1
                self.stdout.write(f"PURGED {label}")

        statement_purged = 0
        for statement in statements:
            label = (
                f"カード明細#{statement.id} {statement.user} "
                f"{statement.period_month:%Y-%m} expires_at={statement.expires_at}"
            )
            if dry_run:
                self.stdout.write(f"DRY-RUN {label}")
                continue
            if statement.purge_file(reason="expired"):
                statement_purged += 1
                self.stdout.write(f"PURGED {label}")

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"dry-run: 領収書{len(receipts)}件、カード明細{len(statements)}件が削除対象です。"
                    f"基準時刻: {now.isoformat()}"
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"領収書{receipt_purged}件、カード明細{statement_purged}件のファイルを削除しました。"
                    "メタデータは保持されています。"
                )
            )
        close_old_connections()
