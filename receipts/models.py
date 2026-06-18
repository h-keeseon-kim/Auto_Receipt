from __future__ import annotations

from calendar import monthrange
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.db.models.signals import post_delete, post_save
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


class ReceiptFilenameStatus(models.TextChoices):
    NOT_PROCESSED = "not_processed", "未確認"
    QUEUED = "queued", "AI待機中"
    PROCESSING = "processing", "AI抽出中"
    GENERATED = "generated", "作成済み"
    NEEDS_REVIEW = "needs_review", "要確認"
    FAILED = "failed", "失敗"
    SKIPPED = "skipped", "スキップ"


class ReceiptPeriodCheckStatus(models.TextChoices):
    NOT_CHECKED = "not_checked", "未確認"
    MATCHED = "matched", "当月確認済み"
    MISMATCHED = "mismatched", "提出月不一致"
    UNKNOWN = "unknown", "確認不可"


class ResubmissionRequestStatus(models.TextChoices):
    OPEN = "open", "再提出待ち"
    RESOLVED = "resolved", "対応済み"


class EmailType(models.TextChoices):
    REMINDER_INITIAL = "reminder_initial", "4日リマインダー"
    REMINDER_URGENT = "reminder_urgent", "10日重要リマインダー"
    TEST = "test", "テスト送信"


class EmailDeliveryStatus(models.TextChoices):
    SENT = "sent", "送信済み"
    FAILED = "failed", "失敗"
    SKIPPED = "skipped", "スキップ"


class ServiceRegistrationSource(models.TextChoices):
    ADMIN = "admin", "管理者登録"
    USER = "user", "ユーザー登録"


class ServiceDeactivationSource(models.TextChoices):
    ADMIN = "admin", "管理者停止"
    USER = "user", "ユーザー停止"


class ServiceCatalog(models.Model):
    """管理者が登録するサービスマスター。ユーザーはこの一覧から利用登録する。"""

    name = models.CharField("サービス名", max_length=120)
    billing_type = models.CharField("支払い種別", max_length=20, choices=BillingType.choices)
    is_active = models.BooleanField("選択可能", default=True)
    memo = models.TextField("メモ", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_service_catalogs",
        verbose_name="作成管理者",
    )
    created_at = models.DateTimeField("作成日時", auto_now_add=True)
    updated_at = models.DateTimeField("更新日時", auto_now=True)

    class Meta:
        ordering = ["name", "billing_type"]
        constraints = [
            models.UniqueConstraint(Lower("name"), "billing_type", name="unique_service_catalog_name_billing_type_ci"),
        ]
        verbose_name = "サービスマスター"
        verbose_name_plural = "サービスマスター"

    def clean(self):
        if self.name:
            self.name = " ".join(self.name.strip().split())
        if not self.name:
            raise ValidationError({"name": "サービス名を入力してください。"})

    @property
    def display_name(self) -> str:
        return f"{self.name}（{self.get_billing_type_display()}）"

    def __str__(self) -> str:
        return self.display_name


class RegisteredServiceQuerySet(models.QuerySet):
    def active(self):
        return self.filter(is_active=True)

    def uploadable_for(self, user, period_month=None):
        queryset = self.filter(user=user)
        if period_month is None:
            return queryset.filter(is_active=True).order_by("name", "billing_type")
        period_month = month_start(period_month)
        return queryset.filter(
            Q(is_active=True) | Q(is_active=False, final_receipt_month__gte=period_month)
        ).order_by("-is_active", "name", "billing_type")


class RegisteredService(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="registered_services")
    catalog_service = models.ForeignKey(
        ServiceCatalog,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="registered_services",
        verbose_name="サービスマスター",
    )
    name = models.CharField("サービス名", max_length=120)
    billing_type = models.CharField("支払い種別", max_length=20, choices=BillingType.choices)
    is_active = models.BooleanField("利用中", default=True)
    memo = models.TextField("メモ", blank=True)
    registration_source = models.CharField(
        "登録元",
        max_length=20,
        choices=ServiceRegistrationSource.choices,
        default=ServiceRegistrationSource.ADMIN,
    )
    registered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="service_registrations_created",
        verbose_name="登録者",
    )
    deactivation_source = models.CharField(
        "停止元",
        max_length=20,
        choices=ServiceDeactivationSource.choices,
        blank=True,
    )
    deactivated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="service_registrations_deactivated",
        verbose_name="停止者",
    )
    deactivated_at = models.DateTimeField("停止日時", null=True, blank=True)
    final_receipt_month = models.DateField("最後にアップロードすべき領収書月", null=True, blank=True)
    stop_note = models.TextField("利用停止メモ", blank=True)
    created_at = models.DateTimeField("作成日時", auto_now_add=True)
    updated_at = models.DateTimeField("更新日時", auto_now=True)

    objects = RegisteredServiceQuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                "user",
                Lower("name"),
                "billing_type",
                name="unique_registered_service_per_user_name_billing_type_ci",
            ),
        ]
        ordering = ["-is_active", "name", "billing_type"]
        verbose_name = "登録サービス"
        verbose_name_plural = "登録サービス"

    def clean(self):
        if self.catalog_service_id:
            self.name = self.catalog_service.name
            self.billing_type = self.catalog_service.billing_type
            if self.catalog_service and not self.catalog_service.is_active and self.is_active and self.pk is None:
                raise ValidationError({"catalog_service": "停止中のサービスマスターは新規登録できません。"})
        if self.name:
            self.name = " ".join(self.name.strip().split())
        if not self.name:
            raise ValidationError({"name": "サービス名を入力してください。"})
        if self.final_receipt_month:
            self.final_receipt_month = month_start(self.final_receipt_month)
        if self.is_active and self.deactivation_source:
            raise ValidationError("利用中サービスには停止元を設定できません。")
        if not self.is_active and self.deactivation_source == ServiceDeactivationSource.USER and not self.final_receipt_month:
            raise ValidationError({"final_receipt_month": "ユーザー停止の場合は最後にアップロードすべき領収書月を選択してください。"})

    def save(self, *args, **kwargs):
        if self.catalog_service_id:
            self.name = self.catalog_service.name
            self.billing_type = self.catalog_service.billing_type
        if self.name:
            self.name = " ".join(self.name.strip().split())
        if self.final_receipt_month:
            self.final_receipt_month = month_start(self.final_receipt_month)
        super().save(*args, **kwargs)

    def is_uploadable_for(self, period_month) -> bool:
        period_month = month_start(period_month)
        return self.is_active or bool(self.final_receipt_month and period_month <= self.final_receipt_month)

    @property
    def display_name(self) -> str:
        return f"{self.name}（{self.get_billing_type_display()}）"

    @property
    def source_badge_class(self) -> str:
        return "draft" if self.registration_source == ServiceRegistrationSource.USER else "neutral"

    @property
    def stop_badge_class(self) -> str:
        return "draft" if self.deactivation_source == ServiceDeactivationSource.USER else "neutral"

    def deactivate(self, *, by, source: str, final_receipt_month=None, note: str = ""):
        self.is_active = False
        self.deactivation_source = source
        self.deactivated_by = by
        self.deactivated_at = timezone.now()
        self.final_receipt_month = month_start(final_receipt_month) if final_receipt_month else None
        self.stop_note = note or ""
        self.save(
            update_fields=[
                "is_active",
                "deactivation_source",
                "deactivated_by",
                "deactivated_at",
                "final_receipt_month",
                "stop_note",
                "updated_at",
            ]
        )

    def activate(self):
        self.is_active = True
        self.deactivation_source = ""
        self.deactivated_by = None
        self.deactivated_at = None
        self.final_receipt_month = None
        self.stop_note = ""
        self.save(
            update_fields=[
                "is_active",
                "deactivation_source",
                "deactivated_by",
                "deactivated_at",
                "final_receipt_month",
                "stop_note",
                "updated_at",
            ]
        )

    def __str__(self) -> str:
        return f"{self.display_name} / {self.user}"


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


class UserProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    must_change_password = models.BooleanField("次回ログイン時にパスワード変更を必須にする", default=False)
    initial_password_generated_at = models.DateTimeField("初期パスワード生成日時", null=True, blank=True)
    password_changed_at = models.DateTimeField("初回パスワード変更日時", null=True, blank=True)
    tutorial_completed_at = models.DateTimeField("チュートリアル完了日時", null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_user_profiles",
        verbose_name="作成管理者",
    )
    created_at = models.DateTimeField("作成日時", auto_now_add=True)
    updated_at = models.DateTimeField("更新日時", auto_now=True)

    class Meta:
        verbose_name = "ユーザープロファイル"
        verbose_name_plural = "ユーザープロファイル"

    def mark_initial_password_generated(self):
        self.initial_password_generated_at = timezone.now()
        self.password_changed_at = None
        self.save(update_fields=["must_change_password", "initial_password_generated_at", "password_changed_at", "created_by", "updated_at"])

    def mark_password_changed(self):
        self.must_change_password = False
        self.password_changed_at = timezone.now()
        self.save(update_fields=["must_change_password", "password_changed_at", "updated_at"])

    def mark_tutorial_completed(self):
        self.tutorial_completed_at = timezone.now()
        self.save(update_fields=["tutorial_completed_at", "updated_at"])

    def __str__(self) -> str:
        return f"{self.user} / password_change_required={self.must_change_password}"


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
    generated_filename = models.CharField("AI修正ファイル名", max_length=255, blank=True)
    ai_filename_status = models.CharField(
        "AIファイル名ステータス",
        max_length=20,
        choices=ReceiptFilenameStatus.choices,
        default=ReceiptFilenameStatus.NOT_PROCESSED,
    )
    ai_filename_admin_memo = models.TextField("AIファイル名管理者メモ", blank=True)
    ai_filename_checked_at = models.DateTimeField("AIファイル名確認日時", null=True, blank=True)
    ai_extracted_payee = models.CharField("AI抽出払先", max_length=160, blank=True)
    ai_extracted_card_last4 = models.CharField("AI抽出カード下4桁", max_length=4, blank=True)
    ai_receipt_month = models.CharField("AI判定領収書月", max_length=7, blank=True)
    ai_period_check_status = models.CharField(
        "AI提出月確認ステータス",
        max_length=20,
        choices=ReceiptPeriodCheckStatus.choices,
        default=ReceiptPeriodCheckStatus.NOT_CHECKED,
    )
    ai_period_check_memo = models.TextField("AI提出月確認メモ", blank=True)
    ai_check_card_last4 = models.BooleanField("AI確認: カード末尾", default=False)
    ai_check_payee = models.BooleanField("AI確認: 払先", default=False)
    ai_check_service_payee_related = models.BooleanField("AI確認: サービス名と払先の関連性", default=False)
    ai_service_payee_check_memo = models.TextField("AIサービス名・払先確認メモ", blank=True)
    ai_check_date = models.BooleanField("AI確認: 日付", default=False)
    ai_check_amount = models.BooleanField("AI確認: 金額", default=False)
    ai_check_currency = models.BooleanField("AI確認: 通貨", default=False)
    ai_check_period_match = models.BooleanField("AI確認: 提出月一致", default=False)
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
            raise ValidationError("自分の利用サービスだけを選択できます。")
        if self.service_id and self.submission_id and not self.service.is_uploadable_for(self.submission.period_month):
            raise ValidationError("この提出月では選択できないサービスです。利用停止済みの場合は、最終領収書月までしか選択できません。")

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
    def service_display_name_snapshot(self) -> str:
        return f"{self.service_name_snapshot}（{self.get_billing_type_snapshot_display()}）"

    @property
    def display_filename(self) -> str:
        if self.generated_filename:
            return self.generated_filename
        if self.original_filename:
            return self.original_filename
        if self.file:
            return Path(self.file.name).name
        return ""

    @property
    def ai_filename_badge_class(self) -> str:
        if self.ai_filename_status == ReceiptFilenameStatus.GENERATED:
            return "submitted"
        if self.ai_filename_status in {ReceiptFilenameStatus.QUEUED, ReceiptFilenameStatus.PROCESSING}:
            return "processing"
        if self.ai_filename_status in {ReceiptFilenameStatus.NEEDS_REVIEW, ReceiptFilenameStatus.FAILED}:
            return "draft"
        return "neutral"

    @property
    def needs_ai_filename_review(self) -> bool:
        return self.ai_filename_status in {ReceiptFilenameStatus.NEEDS_REVIEW, ReceiptFilenameStatus.FAILED}

    @property
    def ai_period_check_badge_class(self) -> str:
        if self.ai_period_check_status == ReceiptPeriodCheckStatus.MATCHED:
            return "submitted"
        if self.ai_period_check_status == ReceiptPeriodCheckStatus.MISMATCHED:
            return "draft"
        return "neutral"

    @property
    def needs_period_reupload(self) -> bool:
        return self.ai_period_check_status == ReceiptPeriodCheckStatus.MISMATCHED

    @property
    def ai_check_items(self) -> list[tuple[str, bool]]:
        return [
            ("カード末尾7210", self.ai_check_card_last4),
            ("払先", self.ai_check_payee),
            ("サービス/払先関連", self.ai_check_service_payee_related),
            ("日付", self.ai_check_date),
            ("金額", self.ai_check_amount),
            ("通貨", self.ai_check_currency),
            ("提出月一致", self.ai_check_period_match),
        ]

    @property
    def ai_all_checks_passed(self) -> bool:
        return all(checked for _label, checked in self.ai_check_items)

    @property
    def is_ai_processing(self) -> bool:
        return self.ai_filename_status == ReceiptFilenameStatus.PROCESSING

    @property
    def ai_is_queued(self) -> bool:
        return self.ai_filename_status == ReceiptFilenameStatus.QUEUED

    @property
    def ai_has_check_result(self) -> bool:
        completed_statuses = {
            ReceiptFilenameStatus.GENERATED,
            ReceiptFilenameStatus.NEEDS_REVIEW,
            ReceiptFilenameStatus.FAILED,
            ReceiptFilenameStatus.SKIPPED,
        }
        return bool(self.ai_filename_checked_at) or self.ai_filename_status in completed_statuses

    @property
    def ai_requires_manual_review(self) -> bool:
        return self.file_available and self.ai_has_check_result and not self.ai_all_checks_passed

    @property
    def needs_manual_review(self) -> bool:
        return self.ai_requires_manual_review

    @property
    def manual_review_badge_class(self) -> str:
        if self.ai_is_queued or self.is_ai_processing:
            return "processing"
        if not self.ai_has_check_result:
            return "neutral"
        return "draft" if self.ai_requires_manual_review else "submitted"

    @property
    def manual_review_label(self) -> str:
        if self.ai_is_queued:
            return "AI待機中"
        if self.is_ai_processing:
            return "AI抽出中"
        if not self.ai_has_check_result:
            return "チェック待ち"
        if self.needs_manual_review:
            return "手動確認"
        return "確認OK"

    @property
    def ai_unchecked_labels(self) -> list[str]:
        return [label for label, checked in self.ai_check_items if not checked]

    @property
    def ai_unchecked_summary(self) -> str:
        labels = self.ai_unchecked_labels
        return "、".join(labels) if labels else "なし"

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


class ReceiptResubmissionRequest(models.Model):
    """管理者が領収書単位で再提出を依頼した記録。

    元のReceiptは削除されるため、ユーザーへ表示するために提出月・サービス・ファイル名をスナップショットとして保持する。
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="receipt_resubmission_requests")
    period_month = models.DateField("提出月")
    service_name_snapshot = models.CharField("対象サービス名", max_length=120)
    billing_type_snapshot = models.CharField("対象支払い種別", max_length=20, choices=BillingType.choices)
    original_receipt_id = models.PositiveIntegerField("元領収書ID", null=True, blank=True)
    original_filename = models.CharField("元ファイル名", max_length=255, blank=True)
    display_filename = models.CharField("表示ファイル名", max_length=255, blank=True)
    message = models.TextField("ユーザー向けメッセージ", blank=True)
    status = models.CharField("ステータス", max_length=20, choices=ResubmissionRequestStatus.choices, default=ResubmissionRequestStatus.OPEN)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_receipt_resubmission_requests",
        verbose_name="依頼管理者",
    )
    created_at = models.DateTimeField("依頼日時", auto_now_add=True)
    resolved_at = models.DateTimeField("対応日時", null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_receipt_resubmission_requests",
        verbose_name="対応ユーザー",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "period_month", "status"]),
            models.Index(fields=["service_name_snapshot", "billing_type_snapshot"]),
        ]
        verbose_name = "領収書再提出依頼"
        verbose_name_plural = "領収書再提出依頼"

    def clean(self):
        if self.period_month:
            self.period_month = month_start(self.period_month)

    def save(self, *args, **kwargs):
        if self.period_month:
            self.period_month = month_start(self.period_month)
        super().save(*args, **kwargs)

    @property
    def service_display_name_snapshot(self) -> str:
        return f"{self.service_name_snapshot}（{self.get_billing_type_snapshot_display()}）"

    @property
    def is_open(self) -> bool:
        return self.status == ResubmissionRequestStatus.OPEN

    def mark_resolved(self, *, by):
        self.status = ResubmissionRequestStatus.RESOLVED
        self.resolved_by = by
        self.resolved_at = timezone.now()
        self.save(update_fields=["status", "resolved_by", "resolved_at"])

    def __str__(self) -> str:
        return f"{self.user} / {self.period_month:%Y-%m} / {self.service_display_name_snapshot} / {self.get_status_display()}"


class EmailDeliveryLog(models.Model):
    """リマインダー・テストメールの送信結果ログ。重複送信防止にも使う。"""

    email_type = models.CharField("メール種別", max_length=40, choices=EmailType.choices)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="receipt_email_logs",
        verbose_name="対象ユーザー",
    )
    target_month = models.DateField("対象提出月", null=True, blank=True)
    to_email = models.EmailField("送信先")
    subject = models.CharField("件名", max_length=255)
    status = models.CharField("ステータス", max_length=20, choices=EmailDeliveryStatus.choices, default=EmailDeliveryStatus.SENT)
    message = models.TextField("本文", blank=True)
    error = models.TextField("エラー", blank=True)
    idempotency_key = models.CharField("重複防止キー", max_length=255, unique=True, null=True, blank=True)
    sent_at = models.DateTimeField("送信日時", null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_receipt_email_logs",
        verbose_name="実行管理者",
    )
    created_at = models.DateTimeField("作成日時", auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["email_type", "target_month", "status"]),
            models.Index(fields=["to_email", "created_at"]),
        ]
        verbose_name = "メール送信ログ"
        verbose_name_plural = "メール送信ログ"

    def clean(self):
        if self.target_month:
            self.target_month = month_start(self.target_month)
        if self.to_email:
            self.to_email = self.to_email.strip().lower()

    def save(self, *args, **kwargs):
        if self.target_month:
            self.target_month = month_start(self.target_month)
        if self.to_email:
            self.to_email = self.to_email.strip().lower()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        target = self.target_month.strftime("%Y-%m") if self.target_month else "-"
        return f"{self.get_email_type_display()} / {target} / {self.to_email} / {self.get_status_display()}"


@receiver(post_delete, sender=Receipt)
def delete_receipt_file(sender, instance: Receipt, **kwargs):
    if instance.file:
        instance.file.delete(save=False)


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def ensure_user_profile(sender, instance, created, **kwargs):
    UserProfile.objects.get_or_create(user=instance)
