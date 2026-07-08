from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from receipts.emailing import current_target_month, send_receipt_reminders
from receipts.forms import MonthField
from receipts.models import EmailReminderSchedule, EmailType


class Command(BaseCommand):
    help = "領収書アップロードの月次リマインダーメールを送信します。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--kind",
            choices=["auto", "initial", "urgent"],
            default="auto",
            help="initial=通常リマインダー、urgent=重要リマインダー、auto=管理者画面で設定した送信日に応じて自動判定。",
        )
        parser.add_argument(
            "--month",
            help="対象提出月。例: 2026-06。未指定時はRECEIPT_REMINDER_TARGET_MONTH_OFFSETに従います。",
        )
        parser.add_argument("--dry-run", action="store_true", help="送信対象数だけ確認し、メールは送信しません。")
        parser.add_argument("--force", action="store_true", help="同じ対象月・同じユーザーに対して再送します。通常は使いません。")

    def parse_month(self, value: str | None):
        if not value:
            return current_target_month()
        field = MonthField()
        try:
            return field.clean(value)
        except Exception as exc:
            raise CommandError("--month は YYYY-MM 形式で指定してください。") from exc

    def resolve_kind(self, value: str) -> tuple[str | None, EmailReminderSchedule]:
        schedule = EmailReminderSchedule.get_solo()
        if value == "initial":
            return EmailType.REMINDER_INITIAL, schedule
        if value == "urgent":
            return EmailType.REMINDER_URGENT, schedule
        day = timezone.localdate().day
        if day == schedule.reminder_day:
            return EmailType.REMINDER_INITIAL, schedule
        if day == schedule.warning_day:
            return EmailType.REMINDER_URGENT, schedule
        return None, schedule

    def handle(self, *args, **options):
        email_type, schedule = self.resolve_kind(options["kind"])
        if email_type is None:
            self.stdout.write(
                self.style.SUCCESS(
                    f"本日はリマインダー送信対象日ではありません。"
                    f"現在の設定: リマインダー日=毎月{schedule.reminder_day}日、警告日=毎月{schedule.warning_day}日。送信せず終了します。"
                )
            )
            return

        target_month = self.parse_month(options.get("month"))
        result = send_receipt_reminders(
            email_type=email_type,
            target_month=target_month,
            dry_run=options["dry_run"],
            force=options["force"],
        )
        if email_type == EmailType.REMINDER_INITIAL:
            label = f"通常リマインダー（毎月{schedule.reminder_day}日）"
        else:
            label = f"重要リマインダー（毎月{schedule.warning_day}日）"
        message = (
            f"{label} / 対象月 {result.target_month:%Y-%m}: "
            f"対象={result.selected_count}, 送信済み={result.sent_count}, "
            f"スキップ={result.skipped_count}, 失敗={result.failed_count}, dry-run={result.dry_run_count}"
        )
        if result.failed_count:
            self.stdout.write(self.style.WARNING(message))
        else:
            self.stdout.write(self.style.SUCCESS(message))
