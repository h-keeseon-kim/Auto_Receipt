from __future__ import annotations

from calendar import monthrange
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator, MinValueValidator
from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.urls import reverse
from django.utils import timezone


def month_start(value):
    return value.replace(day=1)


def add_months(value, months: int):
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def retention_months() -> int:
    return min(max(int(getattr(settings, "RECEIPT_RETENTION_MONTHS", 3)), 1), 3)


def receipt_expiry_from(value):
    return add_months(value, retention_months())


def validate_upload_size(uploaded_file):
    max_size = getattr(settings, "MAX_UPLOAD_SIZE", 10 * 1024 * 1024)
    if uploaded_file.size > max_size:
        mb = max_size // 1024 // 1024
        raise ValidationError(f"ファイルサイズは {mb}MB 以下にしてください。")


def receipt_upload_path(instance: "Receipt", filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    period = instance.submission.period_month.strftime("%Y-%m")
    return f"receipts/user_{instance.submission.user_id}/{period}/{uuid4().hex}{suffix}"


class BillingType(models.TextChoices):
    SUBSCRIPTION = "subscription", "サブスク"
    METERED = "metered", "従量課金 / API"
    ONE_TIME = "one_time", "一回払い"
    OTHER = "other", "その他"


class SubmissionStatus(models.TextChoices):
    DRAFT = "draft", "下書き"
    SUBMITTED = "submitted", "提出済み"


class RegisteredService(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="registered_services")
    name = models.CharField("サービス名", max_length=120)
    billing_type = models.CharField("支払い種別", max_length=20, choices=BillingType.choices)
    is_active = models.BooleanField("利用中", default=True)
    memo = models.TextField("メモ", blank=True)
    created_at = models.DateTimeField("作成日時", auto_now_add=True)
    updated_at = models.DateTimeField("更新日時", auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "name"], name="unique_registered_service_per_user"),
        ]
        ordering = ["name"]
        verbose_name = "登録サービス"
        verbose_name_plural = "登録サービス"

    def clean(self):
        if self.name:
            self.name = " ".join(self.name.strip().split())
        if not self.name:
            raise ValidationError({"name": "サービス名を入力してください。"})

    def __str__(self) -> str:
        return f"{self.name} / {self.user}"


class Submission(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="submissions")
    period_month = models.DateField("提出月")
    status = models.CharField("ステータス", max_length=20, choices=SubmissionStatus.choices, default=SubmissionStatus.DRAFT)
    submitted_at = models.DateTimeField("提出日時", null=True, blank=True)
    created_at = models.DateTimeField("作成日時", auto_now_add=True)
    updated_at = models.DateTimeField("更新日時", auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "period_month"], name="unique_submission_per_user_month"),
        ]
        ordering = ["-period_month", "user__username"]
        verbose_name = "提出"
        verbose_name_plural = "提出"

    def clean(self):
        if self.period_month and self.period_month.day != 1:
            raise ValidationError({"period_month": "提出月は月初日として保存してください。"})

    @property
    def is_submitted(self) -> bool:
        return self.status == SubmissionStatus.SUBMITTED

    @property
    def receipt_count(self) -> int:
        if hasattr(self, "_prefetched_objects_cache") and "receipts" in self._prefetched_objects_cache:
            return len(self.receipts.all())
        return self.receipts.count()

    @property
    def available_file_count(self) -> int:
        return self.receipts.available_files().count()

    def submit(self):
        if not self.receipts.available_files().exists():
            raise ValidationError("領収書ファイルを1件以上アップロードしてから提出してください。")
        self.status = SubmissionStatus.SUBMITTED
        self.submitted_at = timezone.now()
        self.save(update_fields=["status", "submitted_at", "updated_at"])

    def get_absolute_url(self):
        return reverse("submission_detail", kwargs={"pk": self.pk})

    def __str__(self) -> str:
        return f"{self.user} / {self.period_month:%Y-%m} / {self.get_status_display()}"


class ReceiptQuerySet(models.QuerySet):
    def available_files(self):
        return self.filter(file_deleted_at__isnull=True).exclude(file="")

    def expired(self):
        return self.available_files().filter(expires_at__lte=timezone.now())


class Receipt(models.Model):
    submission = models.ForeignKey(Submission, on_delete=models.CASCADE, related_name="receipts")
    service = models.ForeignKey(RegisteredService, on_delete=models.PROTECT, related_name="receipts", verbose_name="登録サービス")
    service_name_snapshot = models.CharField("提出時サービス名", max_length=120)
    billing_type_snapshot = models.CharField("提出時支払い種別", max_length=20, choices=BillingType.choices)
    amount = models.DecimalField("金額", max_digits=12, decimal_places=2, validators=[MinValueValidator(0)], null=True, blank=True)
    currency = models.CharField("通貨", max_length=3, default="JPY")
    issued_on = models.DateField("発行日 / 支払日", null=True, blank=True)
    memo = models.TextField("メモ", blank=True)
    file = models.FileField(
        "領収書ファイル",
        upload_to=receipt_upload_path,
        blank=True,
        validators=[
            FileExtensionValidator(allowed_extensions=["pdf", "png", "jpg", "jpeg", "webp"]),
            validate_upload_size,
        ],
    )
    original_filename = models.CharField("元ファイル名", max_length=255, blank=True)
    file_size = models.PositiveIntegerField("ファイルサイズ", null=True, blank=True)
    content_type = models.CharField("Content-Type", max_length=120, blank=True)
    uploaded_at = models.DateTimeField("アップロード日時", auto_now_add=True)
    expires_at = models.DateTimeField("ファイル保存期限", null=True, blank=True)
    file_deleted_at = models.DateTimeField("ファイル削除日時", null=True, blank=True)
    file_delete_reason = models.CharField("ファイル削除理由", max_length=40, blank=True)
    created_at = models.DateTimeField("作成日時", auto_now_add=True)
    updated_at = models.DateTimeField("更新日時", auto_now=True)

    objects = ReceiptQuerySet.as_manager()

    class Meta:
        ordering = ["service_name_snapshot", "uploaded_at"]
        verbose_name = "領収書"
        verbose_name_plural = "領収書"

    def clean(self):
        if self.submission_id and self.submission.is_submitted:
            raise ValidationError("提出済みの領収書は変更できません。")
        if self.service_id and self.submission_id and self.service.user_id != self.submission.user_id:
            raise ValidationError("自分の登録サービスだけを選択できます。")

    def save(self, *args, **kwargs):
        if self.service_id:
            self.service_name_snapshot = self.service.name
            self.billing_type_snapshot = self.service.billing_type
        if self.currency:
            self.currency = self.currency.upper()
        if self.file and not self.expires_at:
            self.expires_at = receipt_expiry_from(timezone.now())
        super().save(*args, **kwargs)

    @property
    def file_available(self) -> bool:
        return bool(self.file) and self.file_deleted_at is None

    @property
    def file_status_label(self) -> str:
        if self.file_available:
            return "保存中"
        if self.file_deleted_at:
            return "削除済み"
        return "未保存"

    def purge_file(self, reason: str = "expired") -> bool:
        if not self.file_available:
            return False
        storage = self.file.storage
        name = self.file.name
        if name and storage.exists(name):
            storage.delete(name)
        self.file = ""
        self.file_deleted_at = timezone.now()
        self.file_delete_reason = reason
        self.save(update_fields=["file", "file_deleted_at", "file_delete_reason", "updated_at"])
        return True

    def __str__(self) -> str:
        return f"{self.service_name_snapshot} ({self.submission})"


@receiver(post_delete, sender=Receipt)
def delete_receipt_file(sender, instance: Receipt, **kwargs):
    if instance.file:
        instance.file.delete(save=False)
