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


def receipt_month_for_submission(period_month):
    """提出月に対応する領収書月を返す。

    ReceiptHubでは、7月提出は6月分の領収書を対象とする。
    年をまたぐ場合も、1月提出は前年12月分として扱う。
    """

    return add_months(month_start(period_month), -1)


def submission_month_for_receipt(receipt_month):
    """領収書月に対応する提出月を返す。"""

    return add_months(month_start(receipt_month), 1)


def receipt_month_for_statement(statement_month):
    """ご利用代金明細の月に対応する領収書月を返す。

    ReceiptHubでは、7月分の全社ご利用代金明細は6月分の領収書と照合する。
    ユーザーの7月提出サイクルにも、同じ6月分領収書が保存される。
    """

    return receipt_month_for_submission(statement_month)


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


def statement_upload_path(instance: "CardStatement", filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    period = instance.period_month.strftime("%Y-%m")
    return f"card_statements/{period}/{uuid4().hex}{suffix}"


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
    MATCHED = "matched", "対象領収書月確認済み"
    MISMATCHED = "mismatched", "対象領収書月不一致"
    UNKNOWN = "unknown", "確認不可"


class ReceiptUploadSource(models.TextChoices):
    USER = "user", "ユーザー本人"
    ADMIN = "admin", "管理者代理"


class ReceiptAdminReviewStatus(models.TextChoices):
    NOT_REVIEWED = "not_reviewed", "未確認"
    CONFIRMED = "confirmed", "管理者確認済み"


class ResubmissionRequestStatus(models.TextChoices):
    OPEN = "open", "再提出待ち"
    RESOLVED = "resolved", "対応済み"


class UserAccountStatus(models.TextChoices):
    ACTIVE = "active", "利用中"
    STOPPED = "stopped", "停止中"


class EmailType(models.TextChoices):
    REMINDER_INITIAL = "reminder_initial", "通常リマインダー"
    REMINDER_URGENT = "reminder_urgent", "重要リマインダー"
    TEST = "test", "テスト送信"


class EmailDeliveryStatus(models.TextChoices):
    SENT = "sent", "送信済み"
    FAILED = "failed", "失敗"
    SKIPPED = "skipped", "スキップ"


class CardStatementStatus(models.TextChoices):
    PROCESSING = "processing", "AI解析中"
    COMPLETED = "completed", "解析済み"
    NEEDS_REVIEW = "needs_review", "要確認"
    FAILED = "failed", "解析失敗"


class StatementMatchStatus(models.TextChoices):
    MATCHED = "matched", "サービス一致"
    AMBIGUOUS = "ambiguous", "曖昧"
    UNMATCHED = "unmatched", "未一致"
    IGNORED = "ignored", "対象外"


class ServiceRegistrationSource(models.TextChoices):
    ADMIN = "admin", "管理者登録"
    USER = "user", "ユーザー登録"
    EXCEPTION_REQUEST = "exception_request", "例外申請承認"


class ServiceDeactivationSource(models.TextChoices):
    ADMIN = "admin", "管理者停止"
    USER = "user", "ユーザー停止"


class ServiceExceptionRequestStatus(models.TextChoices):
    PENDING = "pending", "確認待ち"
    APPROVED = "approved", "承認済み"
    REJECTED = "rejected", "却下"


class ServiceCatalog(models.Model):
    """ユーザーの直接登録、管理者割り当て、例外申請承認で利用するサービスマスター。"""

    name = models.CharField("サービス名", max_length=120)
    billing_type = models.CharField("支払い種別", max_length=20, choices=BillingType.choices)
    is_active = models.BooleanField("選択可能", default=True)
    merchant_aliases = models.TextField(
        "カード明細・払先の表記候補",
        blank=True,
        help_text="例: OPENAI *CHATGPT, OPENAI.COM。カンマまたは改行で複数指定できます。",
    )
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
        """提出月に対してアップロード可能なサービスを返す。

        final_receipt_month は実際の領収書月で管理するため、提出月の前月と比較する。
        """

        queryset = self.filter(user=user)
        if period_month is None:
            return queryset.filter(is_active=True).order_by("name", "billing_type")
        target_receipt_month = receipt_month_for_submission(period_month)
        return queryset.filter(
            Q(is_active=True) | Q(is_active=False, final_receipt_month__gte=target_receipt_month)
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
        old_user_id = None
        old_is_active = None
        if self.pk:
            old_state = (
                type(self).objects.filter(pk=self.pk)
                .values("user_id", "is_active")
                .first()
            )
            if old_state:
                old_user_id = old_state["user_id"]
                old_is_active = old_state["is_active"]

        if self.catalog_service_id:
            self.name = self.catalog_service.name
            self.billing_type = self.catalog_service.billing_type
        if self.name:
            self.name = " ".join(self.name.strip().split())
        if self.final_receipt_month:
            self.final_receipt_month = month_start(self.final_receipt_month)
        super().save(*args, **kwargs)

        if old_user_id and old_user_id != self.user_id:
            sync_user_account_status_from_services(old_user_id)
        if old_is_active is None or old_is_active != self.is_active or old_user_id != self.user_id:
            sync_user_account_status_from_services(self.user_id)

    def is_uploadable_for(self, period_month) -> bool:
        target_receipt_month = receipt_month_for_submission(period_month)
        return self.is_active or bool(
            self.final_receipt_month and target_receipt_month <= self.final_receipt_month
        )

    @property
    def display_name(self) -> str:
        return f"{self.name}（{self.get_billing_type_display()}）"

    @property
    def source_badge_class(self) -> str:
        if self.registration_source == ServiceRegistrationSource.EXCEPTION_REQUEST:
            return "submitted"
        if self.registration_source == ServiceRegistrationSource.USER:
            return "draft"
        return "neutral"

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


class ServiceExceptionRequest(models.Model):
    """サービスマスターに存在しない新規サービスの利用例外申請。"""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="service_exception_requests",
        verbose_name="申請ユーザー",
    )
    service_name = models.CharField("サービス名", max_length=120)
    billing_type = models.CharField("支払い方法", max_length=20, choices=BillingType.choices)
    purpose = models.TextField("用途")
    status = models.CharField(
        "ステータス",
        max_length=20,
        choices=ServiceExceptionRequestStatus.choices,
        default=ServiceExceptionRequestStatus.PENDING,
    )
    review_note = models.TextField("管理者コメント", blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_service_exception_requests",
        verbose_name="確認管理者",
    )
    reviewed_at = models.DateTimeField("確認日時", null=True, blank=True)
    approved_catalog_service = models.ForeignKey(
        ServiceCatalog,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_exception_requests",
        verbose_name="承認後サービスマスター",
    )
    approved_registered_service = models.ForeignKey(
        RegisteredService,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_exception_requests",
        verbose_name="承認後利用サービス",
    )
    created_at = models.DateTimeField("申請日時", auto_now_add=True)
    updated_at = models.DateTimeField("更新日時", auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                Lower("service_name"),
                "user",
                "billing_type",
                condition=Q(status=ServiceExceptionRequestStatus.PENDING),
                name="unique_pending_service_exception_request_ci",
            )
        ]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["user", "status"]),
        ]
        verbose_name = "サービス例外申請"
        verbose_name_plural = "サービス例外申請"

    def clean(self):
        self.service_name = " ".join((self.service_name or "").strip().split())
        self.purpose = (self.purpose or "").strip()
        self.review_note = (self.review_note or "").strip()
        errors = {}
        if not self.service_name:
            errors["service_name"] = "サービス名を入力してください。"
        if not self.purpose:
            errors["purpose"] = "利用目的を入力してください。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.service_name = " ".join((self.service_name or "").strip().split())
        self.purpose = (self.purpose or "").strip()
        self.review_note = (self.review_note or "").strip()
        super().save(*args, **kwargs)

    @property
    def display_name(self) -> str:
        return f"{self.service_name}（{self.get_billing_type_display()}）"

    @property
    def badge_class(self) -> str:
        if self.status == ServiceExceptionRequestStatus.APPROVED:
            return "submitted"
        if self.status == ServiceExceptionRequestStatus.REJECTED:
            return "danger"
        return "draft"

    @property
    def is_pending(self) -> bool:
        return self.status == ServiceExceptionRequestStatus.PENDING

    def __str__(self) -> str:
        return f"{self.user} / {self.display_name} / {self.get_status_display()}"


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
    def target_receipt_month(self):
        return receipt_month_for_submission(self.period_month)

    @property
    def receipt_count(self) -> int:
        if hasattr(self, "_prefetched_objects_cache") and "receipts" in self._prefetched_objects_cache:
            return len(self.receipts.all())
        return self.receipts.count()

    @property
    def available_file_count(self) -> int:
        return self.receipts.available_files().count()

    def submit(self):
        from .monthly_status import build_user_month_summary

        summary = build_user_month_summary(self.user, self.period_month)
        has_extra_receipt = self.receipts.available_files().filter(is_extra=True).exists()
        if not summary.rows and not has_extra_receipt:
            raise ValidationError("対象領収書月に提出対象となる利用サービスがありません。利用サービスを確認してください。")
        if summary.rows and not summary.is_complete:
            unresolved = [row.service.display_name for row in (*summary.missing_required, *summary.api_pending)]
            preview = "、".join(unresolved[:5])
            if len(unresolved) > 5:
                preview += f" ほか{len(unresolved) - 5}件"
            raise ValidationError(
                f"未確認のサービスがあります（{preview}）。領収書をアップロードするか、従量課金 / APIは『対象領収書月は利用なし』を選択してください。"
            )
        self.status = SubmissionStatus.SUBMITTED
        self.submitted_at = timezone.now()
        self.save(update_fields=["status", "submitted_at", "updated_at"])

    def get_absolute_url(self):
        return reverse("submission_detail", kwargs={"pk": self.pk})

    def __str__(self) -> str:
        return f"{self.user} / {self.period_month:%Y-%m} / {self.get_status_display()}"


class UserProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    account_status = models.CharField(
        "利用ステータス",
        max_length=20,
        choices=UserAccountStatus.choices,
        default=UserAccountStatus.STOPPED,
    )
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

    @property
    def is_receipt_email_enabled(self) -> bool:
        return self.account_status == UserAccountStatus.ACTIVE

    def set_account_status(self, status: str):
        if status not in UserAccountStatus.values:
            raise ValidationError({"account_status": "利用ステータスが不正です。"})
        self.account_status = status
        self.save(update_fields=["account_status", "updated_at"])

    def mark_initial_password_generated(self):
        self.initial_password_generated_at = timezone.now()
        self.password_changed_at = None
        self.save(update_fields=["account_status", "must_change_password", "initial_password_generated_at", "password_changed_at", "created_by", "updated_at"])

    def mark_password_changed(self):
        self.must_change_password = False
        self.password_changed_at = timezone.now()
        self.save(update_fields=["must_change_password", "password_changed_at", "updated_at"])

    def mark_tutorial_completed(self):
        self.tutorial_completed_at = timezone.now()
        self.save(update_fields=["tutorial_completed_at", "updated_at"])

    def __str__(self) -> str:
        return f"{self.user} / {self.get_account_status_display()} / password_change_required={self.must_change_password}"


def sync_user_account_status_from_services(user_id: int) -> str | None:
    """利用サービスの有無に合わせてユーザーステータスを自動更新する。

    1件以上の利用中サービスがあれば「利用中」、0件なら「停止中」にする。
    管理者が手動で停止したユーザーも、後からサービスを新規登録・再開したタイミングで自動的に利用中へ戻る。
    """

    if not user_id:
        return None
    try:
        profile = UserProfile.objects.select_related("user").get(user_id=user_id)
    except UserProfile.DoesNotExist:
        return None
    if profile.user.is_staff or profile.user.is_superuser or not profile.user.is_active:
        return profile.account_status

    has_active_service = RegisteredService.objects.filter(user_id=user_id, is_active=True).exists()
    next_status = UserAccountStatus.ACTIVE if has_active_service else UserAccountStatus.STOPPED
    if profile.account_status != next_status:
        profile.account_status = next_status
        profile.save(update_fields=["account_status", "updated_at"])
    return next_status


class ReceiptQuerySet(models.QuerySet):
    def available_files(self):
        return self.filter(file_deleted_at__isnull=True).exclude(file="")

    def expired(self):
        return self.available_files().filter(expires_at__lte=timezone.now())


class Receipt(models.Model):
    submission = models.ForeignKey(Submission, on_delete=models.CASCADE, related_name="receipts")
    service = models.ForeignKey(
        RegisteredService,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="receipts",
        verbose_name="登録サービス",
    )
    is_extra = models.BooleanField(
        "登録外の追加領収書",
        default=False,
        help_text="返金・プラン変更など、登録サービスに紐づかない『その他』の領収書です。",
    )
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
        "AI対象領収書月確認ステータス",
        max_length=20,
        choices=ReceiptPeriodCheckStatus.choices,
        default=ReceiptPeriodCheckStatus.NOT_CHECKED,
    )
    ai_period_check_memo = models.TextField("AI対象領収書月確認メモ", blank=True)
    ai_check_card_last4 = models.BooleanField("AI確認: カード末尾", default=False)
    ai_check_payee = models.BooleanField("AI確認: 払先", default=False)
    ai_check_service_payee_related = models.BooleanField("AI確認: サービス名と払先の関連性", default=False)
    ai_service_payee_check_memo = models.TextField("AIサービス名・払先確認メモ", blank=True)
    ai_check_date = models.BooleanField("AI確認: 日付", default=False)
    ai_check_amount = models.BooleanField("AI確認: 金額", default=False)
    ai_check_currency = models.BooleanField("AI確認: 通貨", default=False)
    ai_check_period_match = models.BooleanField("AI確認: 対象領収書月一致", default=False)
    file_size = models.PositiveIntegerField("ファイルサイズ", null=True, blank=True)
    content_type = models.CharField("Content-Type", max_length=120, blank=True)
    upload_source = models.CharField(
        "アップロード元",
        max_length=20,
        choices=ReceiptUploadSource.choices,
        default=ReceiptUploadSource.USER,
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_receipts",
        verbose_name="アップロード実行者",
    )
    admin_review_status = models.CharField(
        "管理者確認ステータス",
        max_length=20,
        choices=ReceiptAdminReviewStatus.choices,
        default=ReceiptAdminReviewStatus.NOT_REVIEWED,
    )
    admin_reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_receipts",
        verbose_name="確認管理者",
    )
    admin_reviewed_at = models.DateTimeField("管理者確認日時", null=True, blank=True)
    admin_review_note = models.TextField("管理者確認メモ", blank=True)
    admin_filename_overridden = models.BooleanField("管理者によるファイル名修正", default=False)
    uploaded_at = models.DateTimeField("アップロード日時", auto_now_add=True)
    expires_at = models.DateTimeField("ファイル保存期限", null=True, blank=True)
    file_deleted_at = models.DateTimeField("ファイル削除日時", null=True, blank=True)
    file_delete_reason = models.CharField("ファイル削除理由", max_length=40, blank=True)
    created_at = models.DateTimeField("作成日時", auto_now_add=True)
    updated_at = models.DateTimeField("更新日時", auto_now=True)

    objects = ReceiptQuerySet.as_manager()

    class Meta:
        ordering = ["service_name_snapshot", "uploaded_at"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(is_extra=True, service__isnull=True)
                    | Q(is_extra=False, service__isnull=False)
                ),
                name="receipt_extra_service_consistency",
            ),
            models.CheckConstraint(
                condition=Q(is_extra=False) | ~Q(memo=""),
                name="receipt_extra_memo_required",
            ),
        ]
        verbose_name = "領収書"
        verbose_name_plural = "領収書"

    def clean(self):
        if self.submission_id and self.submission.is_submitted:
            raise ValidationError("提出済みの領収書は変更できません。")
        self.memo = (self.memo or "").strip()
        if self.is_extra:
            self.service = None
            self.service_name_snapshot = "その他"
            self.billing_type_snapshot = BillingType.OTHER
            if not self.memo:
                raise ValidationError({"memo": "その他の領収書では、どのような領収書かをメモに入力してください。"})
        else:
            if not self.service_id:
                raise ValidationError({"service": "登録サービスを選択してください。"})
            if self.submission_id and self.service.user_id != self.submission.user_id:
                raise ValidationError("自分の利用サービスだけを選択できます。")
            if self.submission_id and not self.service.is_uploadable_for(self.submission.period_month):
                raise ValidationError("この提出月の対象領収書月では選択できないサービスです。利用停止済みの場合は、最終領収書月までしか選択できません。")

    def save(self, *args, **kwargs):
        self.memo = (self.memo or "").strip()
        if self.is_extra:
            self.service = None
            self.service_name_snapshot = "その他"
            self.billing_type_snapshot = BillingType.OTHER
        elif self.service_id:
            self.service_name_snapshot = self.service.name
            self.billing_type_snapshot = self.service.billing_type
        if self.currency:
            self.currency = self.currency.upper()
        if self.file and not self.expires_at:
            self.expires_at = receipt_expiry_from(timezone.now())
        super().save(*args, **kwargs)

    @property
    def service_display_name_snapshot(self) -> str:
        if self.is_extra:
            return "その他"
        return f"{self.service_name_snapshot}（{self.get_billing_type_snapshot_display()}）"

    @property
    def uploader_label(self) -> str:
        if self.upload_source == ReceiptUploadSource.ADMIN:
            return "管理者代理アップロード"
        return "ユーザー本人アップロード"

    @property
    def uploaded_by_label(self) -> str:
        if self.uploaded_by_id and self.uploaded_by:
            return self.uploaded_by.get_username()
        if self.upload_source == ReceiptUploadSource.USER and self.submission_id:
            return self.submission.user.get_username()
        return "-"

    @property
    def admin_reviewed(self) -> bool:
        return self.admin_review_status == ReceiptAdminReviewStatus.CONFIRMED and bool(self.admin_reviewed_at)

    @property
    def admin_reviewer_label(self) -> str:
        if self.admin_reviewed_by_id and self.admin_reviewed_by:
            return self.admin_reviewed_by.get_username()
        return "-"

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
        relation_label = "メモ/領収書関連" if self.is_extra else "サービス/払先関連"
        return [
            ("カード末尾7210", self.ai_check_card_last4),
            ("払先", self.ai_check_payee),
            (relation_label, self.ai_check_service_payee_related),
            ("日付", self.ai_check_date),
            ("金額", self.ai_check_amount),
            ("通貨", self.ai_check_currency),
            ("対象領収書月一致", self.ai_check_period_match),
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
        return self.ai_requires_manual_review and not self.admin_reviewed

    @property
    def manual_review_badge_class(self) -> str:
        if self.ai_is_queued or self.is_ai_processing:
            return "processing"
        if self.admin_reviewed:
            return "submitted"
        if not self.ai_has_check_result:
            return "neutral"
        return "draft" if self.ai_requires_manual_review else "submitted"

    @property
    def manual_review_label(self) -> str:
        if self.ai_is_queued:
            return "AI待機中"
        if self.is_ai_processing:
            return "AI抽出中"
        if self.admin_reviewed:
            return "管理者確認済み"
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
        label = self.service_display_name_snapshot
        if self.is_extra and self.memo:
            label = f"{label}: {self.memo[:40]}"
        return f"{label} ({self.submission})"


class ReceiptResubmissionRequest(models.Model):
    """管理者が領収書単位で再提出を依頼した記録。

    元のReceiptは削除されるため、ユーザーへ表示するために提出月・サービス・ファイル名をスナップショットとして保持する。
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="receipt_resubmission_requests")
    period_month = models.DateField("提出月")
    service_name_snapshot = models.CharField("対象サービス名", max_length=120)
    billing_type_snapshot = models.CharField("対象支払い種別", max_length=20, choices=BillingType.choices)
    is_extra = models.BooleanField("登録外の追加領収書", default=False)
    receipt_memo_snapshot = models.TextField("領収書メモ", blank=True)
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
        if self.is_extra:
            return "その他"
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




class MonthlyServiceDeclaration(models.Model):
    """従量課金/APIサービスについて、ユーザーが当月利用なしと申告した記録。"""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="monthly_service_declarations")
    service = models.ForeignKey(RegisteredService, on_delete=models.CASCADE, related_name="monthly_declarations")
    period_month = models.DateField("対象月")
    no_usage = models.BooleanField("当月利用なし", default=True)
    note = models.TextField("補足", blank=True)
    declared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="service_usage_declarations_created",
        verbose_name="申告者",
    )
    declared_at = models.DateTimeField("申告日時", auto_now_add=True)
    updated_at = models.DateTimeField("更新日時", auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "service", "period_month"], name="unique_monthly_service_declaration"),
        ]
        ordering = ["-period_month", "service__name"]
        verbose_name = "月次サービス利用申告"
        verbose_name_plural = "月次サービス利用申告"

    def clean(self):
        if self.period_month:
            self.period_month = month_start(self.period_month)
        if self.service_id and self.user_id and self.service.user_id != self.user_id:
            raise ValidationError("自分に登録されたサービスだけを申告できます。")
        if self.service_id and self.service.billing_type != BillingType.METERED:
            raise ValidationError("当月利用なし申告は従量課金 / APIサービスだけで利用できます。")

    def save(self, *args, **kwargs):
        if self.period_month:
            self.period_month = month_start(self.period_month)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.user} / {self.period_month:%Y-%m} / {self.service.display_name} / 利用なし"


class CardStatement(models.Model):
    """管理者がアップロードする全ユーザー共通のカード利用代金明細書。"""

    period_month = models.DateField("明細月")
    file = models.FileField(
        "ご利用代金明細書",
        upload_to=statement_upload_path,
        validators=[
            FileExtensionValidator(allowed_extensions=["pdf", "png", "jpg", "jpeg", "webp"]),
            validate_upload_size,
        ],
    )
    original_filename = models.CharField("元ファイル名", max_length=255, blank=True)
    file_size = models.PositiveIntegerField("ファイルサイズ", null=True, blank=True)
    content_type = models.CharField("Content-Type", max_length=120, blank=True)
    status = models.CharField("解析ステータス", max_length=20, choices=CardStatementStatus.choices, default=CardStatementStatus.PROCESSING)
    card_last4 = models.CharField("カード下4桁", max_length=4, blank=True)
    statement_period = models.CharField("AI判定明細月", max_length=7, blank=True)
    payment_date = models.DateField("支払日", null=True, blank=True)
    ai_admin_memo = models.TextField("AI管理者メモ", blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_card_statements",
        verbose_name="アップロード管理者",
    )
    uploaded_at = models.DateTimeField("アップロード日時", auto_now_add=True)
    processed_at = models.DateTimeField("解析完了日時", null=True, blank=True)
    reconciled_at = models.DateTimeField("領収書照合日時", null=True, blank=True)
    expires_at = models.DateTimeField("ファイル保存期限", null=True, blank=True)
    file_deleted_at = models.DateTimeField("ファイル削除日時", null=True, blank=True)
    file_delete_reason = models.CharField("ファイル削除理由", max_length=40, blank=True)
    updated_at = models.DateTimeField("更新日時", auto_now=True)

    class Meta:
        ordering = ["-uploaded_at"]
        indexes = [models.Index(fields=["period_month", "status"])]
        verbose_name = "ご利用代金明細書"
        verbose_name_plural = "ご利用代金明細書"

    def clean(self):
        if self.period_month:
            self.period_month = month_start(self.period_month)

    def save(self, *args, **kwargs):
        if self.period_month:
            self.period_month = month_start(self.period_month)
        if self.file and not self.expires_at:
            self.expires_at = receipt_expiry_from(timezone.now())
        super().save(*args, **kwargs)

    @property
    def file_available(self) -> bool:
        return bool(self.file) and self.file_deleted_at is None

    @property
    def target_receipt_month(self):
        """この明細月と照合する実際の領収書月。"""

        return receipt_month_for_statement(self.period_month)

    @property
    def submission_month(self):
        """対象領収書が保存される提出月。

        明細月と提出月は同じで、どちらも前月分の領収書を対象とする。
        """

        return month_start(self.period_month)

    @property
    def missing_receipt_count(self) -> int:
        return self.items.filter(receipt_required=True).filter(
            Q(matched_receipt__isnull=True)
            | Q(matched_receipt__file_deleted_at__isnull=False)
            | Q(matched_receipt__file="")
        ).count()

    @property
    def manual_review_count(self) -> int:
        return self.items.filter(
            Q(match_status__in=[StatementMatchStatus.AMBIGUOUS, StatementMatchStatus.UNMATCHED])
            | Q(receipt_required=True, matched_user__isnull=True)
        ).distinct().count()

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
        return f"全ユーザー / 明細月 {self.period_month:%Y-%m} / {self.get_status_display()}"


class CardStatementItem(models.Model):
    statement = models.ForeignKey(CardStatement, on_delete=models.CASCADE, related_name="items")
    sequence = models.PositiveIntegerField("並び順", default=0)
    line_reference = models.CharField("明細番号", max_length=40, blank=True)
    transaction_date = models.DateField("利用日", null=True, blank=True)
    merchant_name = models.CharField("ご利用先", max_length=255)
    merchant_normalized = models.CharField("正規化ご利用先", max_length=255, blank=True)
    amount_jpy = models.DecimalField("請求金額（円）", max_digits=14, decimal_places=2, null=True, blank=True)
    original_amount = models.DecimalField("外貨金額", max_digits=14, decimal_places=2, null=True, blank=True)
    original_currency = models.CharField("外貨通貨", max_length=3, blank=True)
    matched_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="statement_items",
        verbose_name="一致ユーザー",
    )
    matched_catalog_service = models.ForeignKey(
        ServiceCatalog,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="statement_items",
        verbose_name="一致サービスマスター",
    )
    matched_service = models.ForeignKey(
        RegisteredService,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="statement_items",
        verbose_name="一致サービス",
    )
    match_status = models.CharField("一致ステータス", max_length=20, choices=StatementMatchStatus.choices, default=StatementMatchStatus.UNMATCHED)
    match_confidence = models.FloatField("一致信頼度", default=0)
    match_memo = models.TextField("確認メモ", blank=True)
    receipt_required = models.BooleanField("領収書が必要", default=False)
    matched_receipt = models.ForeignKey(
        Receipt,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="statement_items",
        verbose_name="一致領収書",
    )
    created_at = models.DateTimeField("作成日時", auto_now_add=True)

    class Meta:
        ordering = ["sequence", "id"]
        indexes = [models.Index(fields=["statement", "match_status", "receipt_required"])]
        verbose_name = "カード明細項目"
        verbose_name_plural = "カード明細項目"

    @property
    def receipt_status_label(self) -> str:
        if not self.receipt_required:
            return "対象外"
        if self.matched_receipt_id and self.matched_receipt and self.matched_receipt.file_available:
            return "領収書あり"
        if self.matched_service_id or self.matched_catalog_service_id:
            return "領収書未提出"
        return "要確認"

    @property
    def matched_user_label(self) -> str:
        if self.matched_user_id and self.matched_user:
            return self.matched_user.username
        if self.matched_receipt_id and self.matched_receipt:
            return self.matched_receipt.submission.user.username
        if self.matched_service_id and self.matched_service:
            return self.matched_service.user.username
        return "-"

    @property
    def matched_service_label(self) -> str:
        if self.matched_service_id and self.matched_service:
            return self.matched_service.display_name
        if self.matched_catalog_service_id and self.matched_catalog_service:
            return self.matched_catalog_service.display_name
        return "-"

    @property
    def needs_highlight(self) -> bool:
        return self.receipt_required and not (self.matched_receipt_id and self.matched_receipt and self.matched_receipt.file_available)

    @property
    def row_class(self) -> str:
        if self.needs_highlight:
            return "statement-missing-row"
        if self.match_status in {StatementMatchStatus.AMBIGUOUS, StatementMatchStatus.UNMATCHED}:
            return "manual-review-row"
        return ""

    def __str__(self) -> str:
        return f"{self.statement} / {self.line_reference or self.sequence} / {self.merchant_name}"


DEFAULT_INITIAL_SUBJECT = "{app_name}: {receipt_month}分の領収書アップロードをお願いします"
DEFAULT_INITIAL_BODY = """{user_name} 様

まだ領収書をアップロードしていない方にお送りします。
提出月 {target_month} の対象となる {receipt_month}分について、以下のサービスの領収書をアップロードしてください。

{missing_services}

アップロードページ: {upload_url}

アップロード後は、対象領収書月の領収書が揃っていることを確認して「提出する」を押してください。

{app_name}"""
DEFAULT_URGENT_SUBJECT = "【重要】{app_name}: {receipt_month}分の領収書を本日中にアップロードしてください"
DEFAULT_URGENT_BODY = """{user_name} 様

まだ領収書をアップロードしていない方にお送りします。
提出月 {target_month} の対象となる {receipt_month}分の確認が完了していません。本日中にご対応ください。

未アップロードのサービス:
{missing_services}

従量課金 / APIの利用確認が必要なサービス:
{api_pending_services}

APIサービスを利用していない場合は、アップロードページで「対象領収書月は利用なし」を選択してください。

アップロードページ: {upload_url}

{app_name}"""


class EmailReminderSchedule(models.Model):
    """Monthly reminder day settings managed from the staff email page.

    The application keeps a single row with pk=1. Railway Cron should run the
    reminder command daily with --kind auto; this schedule decides whether the
    current day sends the normal reminder, the urgent warning, or nothing.
    """

    reminder_day = models.PositiveSmallIntegerField("リマインダー日", default=4)
    warning_day = models.PositiveSmallIntegerField("警告日", default=10)
    initial_subject_template = models.CharField("通常リマインダー件名", max_length=255, default=DEFAULT_INITIAL_SUBJECT)
    initial_body_template = models.TextField("通常リマインダー本文", default=DEFAULT_INITIAL_BODY)
    urgent_subject_template = models.CharField("重要リマインダー件名", max_length=255, default=DEFAULT_URGENT_SUBJECT)
    urgent_body_template = models.TextField("重要リマインダー本文", default=DEFAULT_URGENT_BODY)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_email_reminder_schedules",
        verbose_name="更新管理者",
    )
    updated_at = models.DateTimeField("更新日時", auto_now=True)
    created_at = models.DateTimeField("作成日時", auto_now_add=True)

    class Meta:
        verbose_name = "メールリマインダー日設定"
        verbose_name_plural = "メールリマインダー日設定"

    @classmethod
    def get_solo(cls) -> "EmailReminderSchedule":
        obj, _created = cls.objects.get_or_create(
            pk=1,
            defaults={
                "reminder_day": 4,
                "warning_day": 10,
                "initial_subject_template": DEFAULT_INITIAL_SUBJECT,
                "initial_body_template": DEFAULT_INITIAL_BODY,
                "urgent_subject_template": DEFAULT_URGENT_SUBJECT,
                "urgent_body_template": DEFAULT_URGENT_BODY,
            },
        )
        return obj

    def clean(self):
        errors = {}
        if self.reminder_day is None or not 1 <= int(self.reminder_day) <= 28:
            errors["reminder_day"] = "リマインダー日は1〜28日の間で設定してください。"
        if self.warning_day is None or not 1 <= int(self.warning_day) <= 28:
            errors["warning_day"] = "警告日は1〜28日の間で設定してください。"
        if not errors and int(self.warning_day) <= int(self.reminder_day):
            errors["warning_day"] = "警告日はリマインダー日より後の日付にしてください。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.pk = 1
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"通常: 毎月{self.reminder_day}日 / 警告: 毎月{self.warning_day}日"

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


@receiver(post_delete, sender=RegisteredService)
def sync_user_account_status_after_service_delete(sender, instance: RegisteredService, **kwargs):
    sync_user_account_status_from_services(instance.user_id)


@receiver(post_save, sender=Receipt)
def sync_statement_items_after_receipt_save(sender, instance: Receipt, **kwargs):
    if not instance.file_available:
        return
    # カード明細月と提出月は同じ月を使い、その前月分の領収書を照合する。
    # 例: 7月分明細 ↔ 7月提出サイクルに保存された6月分領収書。
    # OpenAI APIは呼ばず、保存済みの明細行に対してローカル照合だけを行う。
    from .statement_processing import reconcile_card_statement_items

    statement_ids = list(
        CardStatement.objects.filter(
            period_month=month_start(instance.submission.period_month)
        )
        .exclude(status__in=[CardStatementStatus.PROCESSING, CardStatementStatus.FAILED])
        .values_list("pk", flat=True)
    )
    for statement_id in statement_ids:
        reconcile_card_statement_items(statement_id)


@receiver(post_delete, sender=Receipt)
def delete_receipt_file(sender, instance: Receipt, **kwargs):
    if instance.file:
        instance.file.delete(save=False)


@receiver(post_delete, sender=CardStatement)
def delete_card_statement_file(sender, instance: CardStatement, **kwargs):
    if instance.file:
        instance.file.delete(save=False)


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def ensure_user_profile(sender, instance, created, **kwargs):
    UserProfile.objects.get_or_create(user=instance)
