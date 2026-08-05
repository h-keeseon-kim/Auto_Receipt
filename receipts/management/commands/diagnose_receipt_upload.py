from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from receipts.models import CardStatement, Receipt


class Command(BaseCommand):
    help = "領収書アップロードに必要なDB・MEDIA_ROOT・書き込み権限を診断します。"

    def handle(self, *args, **options):
        failures: list[str] = []

        self.stdout.write(f"MEDIA_ROOT: {settings.MEDIA_ROOT}")
        self.stdout.write(f"MAX_UPLOAD_SIZE: {settings.MAX_UPLOAD_SIZE} bytes")
        self.stdout.write(f"FILE_UPLOAD_MAX_MEMORY_SIZE: {settings.FILE_UPLOAD_MAX_MEMORY_SIZE} bytes")

        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            receipt_count = Receipt.objects.count()
            statement_count = CardStatement.objects.count()
            self.stdout.write(self.style.SUCCESS(
                f"DB: OK (receipts={receipt_count}, card_statements={statement_count})"
            ))
        except Exception as exc:  # pragma: no cover - production diagnostic
            failures.append(f"DB接続/Receiptテーブル: {type(exc).__name__}: {exc}")
            self.stderr.write(self.style.ERROR(failures[-1]))

        media_root = Path(settings.MEDIA_ROOT)
        test_file = media_root / f".receipthub-upload-test-{uuid4().hex}"
        try:
            media_root.mkdir(parents=True, exist_ok=True)
            test_file.write_bytes(b"ReceiptHub upload diagnostic")
            if test_file.read_bytes() != b"ReceiptHub upload diagnostic":
                raise OSError("書き込んだテストデータを読み戻せませんでした。")
            test_file.unlink()
            self.stdout.write(self.style.SUCCESS("MEDIA_ROOT read/write/delete: OK"))
        except Exception as exc:  # pragma: no cover - production diagnostic
            failures.append(f"MEDIA_ROOT書き込み: {type(exc).__name__}: {exc}")
            self.stderr.write(self.style.ERROR(failures[-1]))
            try:
                if test_file.exists():
                    test_file.unlink()
            except Exception:
                pass

        if failures:
            raise CommandError("領収書アップロード診断で問題を検出しました。上記エラーを確認してください。")

        self.stdout.write(self.style.SUCCESS("領収書アップロードの基礎診断はすべて正常です。"))
