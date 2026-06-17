# Generated for ReceiptHub MVP

import django.core.validators
import django.db.models.deletion
import receipts.models
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="RegisteredService",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120, verbose_name="サービス名")),
                ("billing_type", models.CharField(choices=[("subscription", "サブスク"), ("metered", "従量課金 / API"), ("one_time", "一回払い"), ("other", "その他")], max_length=20, verbose_name="支払い種別")),
                ("is_active", models.BooleanField(default=True, verbose_name="利用中")),
                ("memo", models.TextField(blank=True, verbose_name="メモ")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="作成日時")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新日時")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="registered_services", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "verbose_name": "登録サービス",
                "verbose_name_plural": "登録サービス",
                "ordering": ["name"],
            },
        ),
        migrations.CreateModel(
            name="Submission",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("period_month", models.DateField(verbose_name="提出月")),
                ("status", models.CharField(choices=[("draft", "下書き"), ("submitted", "提出済み")], default="draft", max_length=20, verbose_name="ステータス")),
                ("submitted_at", models.DateTimeField(blank=True, null=True, verbose_name="提出日時")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="作成日時")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新日時")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="submissions", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "verbose_name": "提出",
                "verbose_name_plural": "提出",
                "ordering": ["-period_month", "user__username"],
            },
        ),
        migrations.CreateModel(
            name="Receipt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("service_name_snapshot", models.CharField(max_length=120, verbose_name="提出時サービス名")),
                ("billing_type_snapshot", models.CharField(choices=[("subscription", "サブスク"), ("metered", "従量課金 / API"), ("one_time", "一回払い"), ("other", "その他")], max_length=20, verbose_name="提出時支払い種別")),
                ("amount", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True, validators=[django.core.validators.MinValueValidator(0)], verbose_name="金額")),
                ("currency", models.CharField(default="JPY", max_length=3, verbose_name="通貨")),
                ("issued_on", models.DateField(blank=True, null=True, verbose_name="発行日 / 支払日")),
                ("memo", models.TextField(blank=True, verbose_name="メモ")),
                ("file", models.FileField(blank=True, upload_to=receipts.models.receipt_upload_path, validators=[django.core.validators.FileExtensionValidator(allowed_extensions=["pdf", "png", "jpg", "jpeg", "webp"]), receipts.models.validate_upload_size], verbose_name="領収書ファイル")),
                ("original_filename", models.CharField(blank=True, max_length=255, verbose_name="元ファイル名")),
                ("file_size", models.PositiveIntegerField(blank=True, null=True, verbose_name="ファイルサイズ")),
                ("content_type", models.CharField(blank=True, max_length=120, verbose_name="Content-Type")),
                ("uploaded_at", models.DateTimeField(auto_now_add=True, verbose_name="アップロード日時")),
                ("expires_at", models.DateTimeField(blank=True, null=True, verbose_name="ファイル保存期限")),
                ("file_deleted_at", models.DateTimeField(blank=True, null=True, verbose_name="ファイル削除日時")),
                ("file_delete_reason", models.CharField(blank=True, max_length=40, verbose_name="ファイル削除理由")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="作成日時")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新日時")),
                ("service", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="receipts", to="receipts.registeredservice", verbose_name="登録サービス")),
                ("submission", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="receipts", to="receipts.submission")),
            ],
            options={
                "verbose_name": "領収書",
                "verbose_name_plural": "領収書",
                "ordering": ["service_name_snapshot", "uploaded_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="registeredservice",
            constraint=models.UniqueConstraint(fields=("user", "name"), name="unique_registered_service_per_user"),
        ),
        migrations.AddConstraint(
            model_name="submission",
            constraint=models.UniqueConstraint(fields=("user", "period_month"), name="unique_submission_per_user_month"),
        ),
    ]
