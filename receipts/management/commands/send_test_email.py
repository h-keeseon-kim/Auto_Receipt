from __future__ import annotations

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from receipts.emailing import send_test_email
from receipts.models import EmailDeliveryStatus


class Command(BaseCommand):
    help = "SMTP設定確認用のテストメールを送信します。"

    def add_arguments(self, parser):
        parser.add_argument("--to", required=True, help="テスト送信先メールアドレス")
        parser.add_argument("--subject", default="ReceiptHub メール送信テスト")
        parser.add_argument("--body", default="ReceiptHub からのテストメールです。SMTP設定が正しく動作しています。")
        parser.add_argument("--created-by", type=int, help="送信ログに記録する管理者ユーザーID。任意。")

    def handle(self, *args, **options):
        created_by = None
        if options.get("created_by"):
            try:
                created_by = User.objects.get(pk=options["created_by"], is_staff=True)
            except User.DoesNotExist as exc:
                raise CommandError("--created-by には存在する管理者ユーザーIDを指定してください。") from exc
        log, sent = send_test_email(
            to_email=options["to"],
            subject=options["subject"],
            body=options["body"],
            created_by=created_by,
        )
        if sent:
            self.stdout.write(self.style.SUCCESS(f"テストメールを送信しました: {log.to_email}"))
        elif log.status == EmailDeliveryStatus.SKIPPED:
            self.stdout.write(self.style.WARNING(f"テストメールをスキップしました: {log.to_email} / {log.error or '停止中ユーザー'}"))
        else:
            self.stdout.write(self.style.ERROR(f"テストメール送信に失敗しました: {log.error or '詳細不明'}"))
