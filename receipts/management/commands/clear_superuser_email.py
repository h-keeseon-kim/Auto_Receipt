from __future__ import annotations

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "指定したスーパーアカウントの連絡先メールアドレスを空欄にします。ログイン名とパスワードは変更しません。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--username",
            required=True,
            help="対象スーパーアカウントのログイン名。例: admin",
        )

    def handle(self, *args, **options):
        username = (options["username"] or "").strip()
        try:
            user = User.objects.get(username=username, is_superuser=True)
        except User.DoesNotExist as exc:
            raise CommandError(f"スーパーアカウント {username!r} が見つかりません。") from exc

        previous_email = user.email
        if not previous_email:
            self.stdout.write(self.style.SUCCESS(f"{username} の連絡先メールアドレスはすでに空欄です。"))
            return

        user.email = ""
        user.save(update_fields=["email"])
        self.stdout.write(
            self.style.SUCCESS(
                f"{username} の連絡先メールアドレスを空欄にしました。"
                f"解放したメールアドレス: {previous_email}。ログイン名とパスワードは変更していません。"
            )
        )
