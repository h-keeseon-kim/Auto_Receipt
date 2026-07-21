from __future__ import annotations

import string
from datetime import date
from pathlib import Path
import re

from django import forms
from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm, UserCreationForm
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator
from django.db.models import Q
from django.utils import timezone
from django.utils.crypto import get_random_string

from .models import (
    BillingType,
    CardStatement,
    EmailReminderSchedule,
    Receipt,
    ReceiptAdminReviewStatus,
    ReceiptFilenameStatus,
    ReceiptPeriodCheckStatus,
    RegisteredService,
    ServiceCatalog,
    ServiceDeactivationSource,
    ServiceExceptionRequest,
    ServiceExceptionRequestStatus,
    ServiceRegistrationSource,
    UserAccountStatus,
    UserProfile,
    receipt_month_for_submission,
    sync_user_account_status_from_services,
    validate_upload_size,
)


def apply_design_classes(form):
    for field in form.fields.values():
        widget = field.widget
        if isinstance(widget, forms.CheckboxInput):
            widget.attrs.setdefault("class", "form-check-input")
        elif isinstance(widget, forms.HiddenInput):
            continue
        elif isinstance(widget, forms.ClearableFileInput):
            widget.attrs.setdefault("class", "form-control")
        elif isinstance(widget, forms.Select):
            widget.attrs.setdefault("class", "form-select")
        else:
            widget.attrs.setdefault("class", "form-control")


def normalize_account_email(value: str) -> str:
    """このアプリでは一般ユーザーのアカウント名をメールアドレスとして扱う。"""
    return (value or "").strip().lower()


def ensure_unique_account_email(email: str, *, exclude_user: User | None = None):
    """ログイン名または連絡先メールとして使用済みのアドレスを拒否する。"""
    queryset = User.objects.all()
    if exclude_user is not None and exclude_user.pk:
        queryset = queryset.exclude(pk=exclude_user.pk)
    if queryset.filter(username__iexact=email).exists() or queryset.filter(email__iexact=email).exists():
        raise forms.ValidationError("このメールアドレスはすでに登録されています。")


def generate_initial_password(user: User | None = None, length: int = 16) -> str:
    """管理者が対象ユーザーへ一度だけ伝える初期パスワードを生成する。"""
    alphabet = string.ascii_letters + string.digits
    for _ in range(100):
        password = get_random_string(length, allowed_chars=alphabet)
        if not any(char.islower() for char in password):
            continue
        if not any(char.isupper() for char in password):
            continue
        if not any(char.isdigit() for char in password):
            continue
        try:
            validate_password(password, user=user)
        except ValidationError:
            continue
        return password
    # 事実上到達しないが、パスワードバリデータが厳しい場合にも生成できるようにする。
    password = f"{get_random_string(length, allowed_chars=alphabet)}Aa1"
    validate_password(password, user=user)
    return password


class MonthInput(forms.DateInput):
    input_type = "month"

    def format_value(self, value):
        if hasattr(value, "strftime"):
            return value.strftime("%Y-%m")
        if isinstance(value, str) and len(value) >= 7:
            return value[:7]
        return super().format_value(value)


class MonthField(forms.DateField):
    widget = MonthInput

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("input_formats", ["%Y-%m"])
        super().__init__(*args, **kwargs)

    def clean(self, value):
        cleaned = super().clean(value)
        if cleaned is None:
            return None
        return cleaned.replace(day=1)


def current_month():
    today = date.today()
    return today.replace(day=1)


class MonthSelectForm(forms.Form):
    month = MonthField(label="提出月", initial=current_month)

    def __init__(self, *args, month_label=None, **kwargs):
        super().__init__(*args, **kwargs)
        if month_label:
            self.fields["month"].label = month_label
        apply_design_classes(self)


def same_service_identity_q(catalog: ServiceCatalog) -> Q:
    """同一サービス判定。サービス名だけでなく支払い種別も含める。"""

    return Q(catalog_service=catalog) | Q(name__iexact=catalog.name, billing_type=catalog.billing_type)


class ServiceCatalogForm(forms.ModelForm):
    """管理者がユーザーに公開するサービスマスターを登録するフォーム。"""

    class Meta:
        model = ServiceCatalog
        fields = ["name", "billing_type", "is_active", "merchant_aliases", "memo"]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "例: ChatGPT / Claude / AWS"}),
            "merchant_aliases": forms.Textarea(
                attrs={
                    "rows": 3,
                    "placeholder": "例: OPENAI *CHATGPT, OPENAI.COM\nClaudeの場合: CLAUDE.AI SUBSCR, ANTHROPIC.COM",
                }
            ),
            "memo": forms.Textarea(attrs={"rows": 3, "placeholder": "任意: ユーザー向け補足、契約メモなど"}),
        }
        help_texts = {
            "is_active": "OFFにすると、ユーザーのサービス利用登録と管理者の割り当て候補から外れます。既存の利用履歴は残ります。",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_design_classes(self)

    def clean_name(self):
        name = " ".join((self.cleaned_data.get("name") or "").strip().split())
        if not name:
            raise forms.ValidationError("サービス名を入力してください。")
        return name

    def clean(self):
        cleaned = super().clean()
        name = cleaned.get("name")
        billing_type = cleaned.get("billing_type")
        if name and billing_type:
            duplicate = ServiceCatalog.objects.filter(name__iexact=name, billing_type=billing_type).exclude(pk=self.instance.pk)
            if duplicate.exists():
                raise forms.ValidationError("同じサービス名・同じ支払い種別のマスターがすでに登録されています。")
        return cleaned


class StaffServiceForm(forms.ModelForm):
    """管理者が一般ユーザーへサービスマスターを割り当てるためのフォーム。"""

    final_receipt_month = MonthField(label="最後にアップロードすべき領収書月", required=False)

    class Meta:
        model = RegisteredService
        fields = ["user", "catalog_service", "is_active", "memo", "final_receipt_month"]
        widgets = {
            "memo": forms.Textarea(attrs={"rows": 3, "placeholder": "任意: 用途、担当、契約メモなど"}),
        }
        labels = {
            "user": "対象ユーザー",
            "catalog_service": "サービス名",
        }
        help_texts = {
            "user": "このサービスを利用できる一般ユーザーを選択します。",
            "catalog_service": "管理者が登録したサービスマスターから選択します。先にサービスマスターを登録してください。",
            "is_active": "停止すると、通常の利用サービス一覧から外れます。最終領収書月を指定すると、その月まではアップロード選択肢に残せます。",
        }

    def __init__(self, *args, fixed_user: User | None = None, registered_by: User | None = None, **kwargs):
        self.fixed_user = fixed_user
        self.registered_by = registered_by
        super().__init__(*args, **kwargs)
        user_queryset = User.objects.filter(is_active=True, is_staff=False, is_superuser=False).order_by("username")
        if self.instance.pk and self.instance.user_id:
            user_queryset = (user_queryset | User.objects.filter(pk=self.instance.user_id)).distinct().order_by("username")
        self.fields["user"].queryset = user_queryset
        self.fields["user"].empty_label = "対象ユーザーを選択"
        if fixed_user is not None:
            self.fields["user"].initial = fixed_user.pk
            self.fields["user"].widget = forms.HiddenInput()
            self.fields["user"].required = False

        catalog_queryset = ServiceCatalog.objects.filter(is_active=True).order_by("name", "billing_type")
        if self.instance.pk and self.instance.catalog_service_id:
            catalog_queryset = (
                catalog_queryset | ServiceCatalog.objects.filter(pk=self.instance.catalog_service_id)
            ).distinct().order_by("name", "billing_type")
        self.fields["catalog_service"].queryset = catalog_queryset
        self.fields["catalog_service"].empty_label = "サービスマスターを選択"
        self.fields["catalog_service"].label_from_instance = lambda catalog: catalog.display_name
        apply_design_classes(self)

    def clean_user(self):
        if self.fixed_user is not None:
            return self.fixed_user
        user = self.cleaned_data.get("user")
        if user is None:
            raise forms.ValidationError("対象ユーザーを選択してください。")
        if user.is_staff or user.is_superuser:
            raise forms.ValidationError("一般ユーザーだけを選択してください。")
        return user

    def clean(self):
        cleaned = super().clean()
        user = cleaned.get("user")
        catalog = cleaned.get("catalog_service")
        is_active = cleaned.get("is_active")
        final_receipt_month = cleaned.get("final_receipt_month")
        if user and catalog:
            duplicate = RegisteredService.objects.filter(user=user).filter(same_service_identity_q(catalog)).exclude(pk=self.instance.pk)
            if duplicate.exists():
                self.add_error("catalog_service", "このユーザーには同じサービス・同じ支払い種別がすでに登録されています。停止中の場合は再開してください。")
        if not is_active and final_receipt_month is None and self.instance.deactivation_source == ServiceDeactivationSource.USER:
            self.add_error("final_receipt_month", "ユーザー停止の記録には最終領収書月が必要です。")
        return cleaned

    def save(self, commit=True):
        service = super().save(commit=False)
        if self.fixed_user is not None:
            service.user = self.fixed_user
        catalog = self.cleaned_data["catalog_service"]
        service.catalog_service = catalog
        service.name = catalog.name
        service.billing_type = catalog.billing_type
        if not service.pk:
            service.registration_source = ServiceRegistrationSource.ADMIN
            service.registered_by = self.registered_by
        if service.is_active:
            service.deactivation_source = ""
            service.deactivated_by = None
            service.deactivated_at = None
            service.final_receipt_month = None
            service.stop_note = ""
        elif not service.deactivation_source:
            service.deactivation_source = ServiceDeactivationSource.ADMIN
            service.deactivated_by = self.registered_by
            service.deactivated_at = timezone.now()
        if commit:
            service.save()
        return service


# 旧バージョンから参照される可能性があるため、互換用に残す。
RegisteredServiceForm = StaffServiceForm


class UserServiceRegistrationForm(forms.Form):
    """サービスマスターに登録済みのサービスをユーザー自身が利用開始するフォーム。"""

    catalog_service = forms.ModelChoiceField(
        label="利用を開始するサービス",
        queryset=ServiceCatalog.objects.none(),
        empty_label="サービスを選択",
        help_text="管理者が登録したサービスマスターから選択します。一覧にない新規サービスだけ、別の例外申請を利用してください。",
    )
    memo = forms.CharField(
        label="メモ",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "任意: 用途、担当案件、補足など"}),
    )

    def __init__(self, *args, user: User, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        queryset = ServiceCatalog.objects.filter(is_active=True).order_by("name", "billing_type")
        active_services = list(RegisteredService.objects.filter(user=user, is_active=True))
        exclude_query = Q()
        has_exclusions = False
        active_catalog_ids = [service.catalog_service_id for service in active_services if service.catalog_service_id]
        if active_catalog_ids:
            exclude_query |= Q(pk__in=active_catalog_ids)
            has_exclusions = True
        for service in active_services:
            exclude_query |= Q(name__iexact=service.name, billing_type=service.billing_type)
            has_exclusions = True
        if has_exclusions:
            queryset = queryset.exclude(exclude_query)
        self.fields["catalog_service"].queryset = queryset
        self.fields["catalog_service"].label_from_instance = lambda catalog: catalog.display_name
        apply_design_classes(self)

    def clean_catalog_service(self):
        catalog = self.cleaned_data["catalog_service"]
        if RegisteredService.objects.filter(user=self.user, is_active=True).filter(
            same_service_identity_q(catalog)
        ).exists():
            raise forms.ValidationError("このサービス・支払い方法はすでに利用中です。")
        return catalog

    def save(self) -> RegisteredService:
        catalog = self.cleaned_data["catalog_service"]
        memo = (self.cleaned_data.get("memo") or "").strip()
        service = (
            RegisteredService.objects.filter(user=self.user)
            .filter(same_service_identity_q(catalog))
            .order_by("id")
            .first()
        )
        if service is None:
            service = RegisteredService(
                user=self.user,
                name=catalog.name,
                catalog_service=catalog,
                billing_type=catalog.billing_type,
            )
        service.catalog_service = catalog
        service.name = catalog.name
        service.billing_type = catalog.billing_type
        service.is_active = True
        service.memo = memo or service.memo
        service.registration_source = ServiceRegistrationSource.USER
        service.registered_by = self.user
        service.deactivation_source = ""
        service.deactivated_by = None
        service.deactivated_at = None
        service.final_receipt_month = None
        service.stop_note = ""
        service.full_clean()
        service.save()
        return service


class ServiceExceptionRequestForm(forms.ModelForm):
    """サービスマスターに存在しない新規サービスだけを申請するフォーム。"""

    class Meta:
        model = ServiceExceptionRequest
        fields = ["service_name", "billing_type", "purpose"]
        labels = {
            "service_name": "サービス名",
            "billing_type": "支払い方法",
            "purpose": "用途",
        }
        widgets = {
            "service_name": forms.TextInput(attrs={"placeholder": "例: ChatGPT / Claude / Figma"}),
            "purpose": forms.Textarea(
                attrs={
                    "rows": 5,
                    "placeholder": "例: 広告クリエイティブ制作における画像生成と検証に使用します。",
                }
            ),
        }
        help_texts = {
            "service_name": "サービス利用登録の一覧に存在しないことを確認し、正式なサービス名を入力してください。",
            "billing_type": "サブスク、従量課金 / API、一回払い、その他から選択します。",
            "purpose": "誰が読んでも利用理由を判断できるように具体的に記載してください。",
        }

    def __init__(self, *args, user: User, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        apply_design_classes(self)

    def clean_service_name(self):
        name = " ".join((self.cleaned_data.get("service_name") or "").strip().split())
        if not name:
            raise forms.ValidationError("サービス名を入力してください。")
        return name

    def clean_purpose(self):
        purpose = (self.cleaned_data.get("purpose") or "").strip()
        if not purpose:
            raise forms.ValidationError("利用目的を入力してください。")
        return purpose

    def clean(self):
        cleaned = super().clean()
        service_name = cleaned.get("service_name")
        billing_type = cleaned.get("billing_type")
        if not service_name or not billing_type:
            return cleaned

        if RegisteredService.objects.filter(
            user=self.user,
            name__iexact=service_name,
            billing_type=billing_type,
            is_active=True,
        ).exists():
            self.add_error("service_name", "このサービス・支払い方法はすでに利用中です。")

        matching_catalog = (
            ServiceCatalog.objects.filter(
                name__iexact=service_name,
                billing_type=billing_type,
            )
            .order_by("pk")
            .first()
        )
        if matching_catalog is not None:
            if matching_catalog.is_active:
                self.add_error(
                    "service_name",
                    "このサービス・支払い方法はサービスマスターに登録済みです。「サービス利用登録」から選択してください。",
                )
            else:
                self.add_error(
                    "service_name",
                    "このサービス・支払い方法はサービスマスターに登録済みですが、現在は選択停止中です。管理者へ再開を依頼してください。",
                )

        if ServiceExceptionRequest.objects.filter(
            user=self.user,
            service_name__iexact=service_name,
            billing_type=billing_type,
            status=ServiceExceptionRequestStatus.PENDING,
        ).exists():
            self.add_error("service_name", "同じサービス・支払い方法の例外申請がすでに確認待ちです。")
        return cleaned

    def save(self, commit=True):
        request_item = super().save(commit=False)
        request_item.user = self.user
        request_item.status = ServiceExceptionRequestStatus.PENDING
        if commit:
            request_item.full_clean()
            request_item.save()
        return request_item


class StaffServiceExceptionReviewForm(forms.Form):
    DECISION_APPROVE = "approve"
    DECISION_REJECT = "reject"
    DECISION_CHOICES = (
        (DECISION_APPROVE, "承認"),
        (DECISION_REJECT, "却下"),
    )

    request_id = forms.IntegerField(widget=forms.HiddenInput)
    decision = forms.ChoiceField(choices=DECISION_CHOICES, widget=forms.HiddenInput)
    review_note = forms.CharField(
        label="管理者コメント",
        required=False,
        max_length=2000,
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "placeholder": "承認時の補足、または却下理由を入力してください。却下時は必須です。",
            }
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_design_classes(self)

    def clean_request_id(self):
        request_id = self.cleaned_data["request_id"]
        try:
            return ServiceExceptionRequest.objects.select_related("user").get(
                pk=request_id,
                status=ServiceExceptionRequestStatus.PENDING,
            )
        except ServiceExceptionRequest.DoesNotExist as exc:
            raise forms.ValidationError("対象の例外申請は確認済み、または存在しません。") from exc

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("decision") == self.DECISION_REJECT and not (cleaned.get("review_note") or "").strip():
            self.add_error("review_note", "却下する場合は理由を入力してください。")
        return cleaned


class UserServiceStopForm(forms.Form):
    final_receipt_month = MonthField(
        label="最後にアップロードすべき領収書月",
        initial=current_month,
        help_text="例: 2026年6月分まで領収書提出が必要な場合は 2026-06 を選択します。",
    )
    stop_note = forms.CharField(
        label="利用停止メモ",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "任意: 解約日、理由、補足など"}),
    )

    def __init__(self, *args, service: RegisteredService, **kwargs):
        self.service = service
        super().__init__(*args, **kwargs)
        apply_design_classes(self)

    def save(self, *, stopped_by: User) -> RegisteredService:
        self.service.deactivate(
            by=stopped_by,
            source=ServiceDeactivationSource.USER,
            final_receipt_month=self.cleaned_data["final_receipt_month"],
            note=self.cleaned_data.get("stop_note", ""),
        )
        return self.service


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """複数ファイルを1回の選択で検証して返すFileField。"""

    widget = MultipleFileInput

    def clean(self, data, initial=None):
        single_clean = super().clean
        if not data:
            return []
        if isinstance(data, (list, tuple)):
            return [single_clean(item, initial) for item in data]
        return [single_clean(data, initial)]


class ReceiptBatchUploadForm(forms.Form):
    """登録サービスまたは「その他」を選び、1件以上の領収書をまとめて追加する。"""

    OTHER_VALUE = "other"

    service = forms.ChoiceField(
        label="サービス",
        choices=(),
        help_text="登録済みサービス、または「その他」を選択します。",
    )
    memo = forms.CharField(
        label="その他の内容メモ",
        required=False,
        max_length=500,
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "placeholder": "例: OpenAIからの返金領収書 / プラン変更に伴う追加請求",
            }
        ),
        help_text="「その他」を選択した場合は必須です。AIはメモを参考にしますが、領収書ファイル内の記載を優先します。",
    )
    files = MultipleFileField(
        label="領収書ファイル",
        required=True,
        validators=[
            FileExtensionValidator(allowed_extensions=["pdf", "png", "jpg", "jpeg", "webp"]),
            validate_upload_size,
        ],
        widget=MultipleFileInput(
            attrs={
                "accept": ".pdf,.png,.jpg,.jpeg,.webp",
                "multiple": True,
            }
        ),
        help_text="PDF / PNG / JPG / JPEG / WEBP。複数ファイルを一度に選択できます。各ファイル最大10MB。",
    )

    def __init__(
        self,
        *args,
        user: User,
        period_month=None,
        selected_choice: str = "",
        hide_file_input: bool = True,
        **kwargs,
    ):
        self.user = user
        self.period_month = period_month
        self.selected_service: RegisteredService | None = None
        self.is_extra = False
        super().__init__(*args, **kwargs)

        services = list(
            RegisteredService.objects.uploadable_for(user, period_month)
            .select_related("catalog_service")
            .order_by("-is_active", "name", "billing_type")
        )
        choices: list[tuple[str, str]] = [("", "サービスを選択")]
        for service in services:
            label = ReceiptUploadForm.service_label(service)
            choices.append((str(service.pk), label))
        choices.append((self.OTHER_VALUE, "その他"))
        self.fields["service"].choices = choices

        valid_values = {value for value, _label in choices}
        if not self.is_bound and selected_choice in valid_values:
            self.initial["service"] = selected_choice
        apply_design_classes(self)
        if hide_file_input:
            self.fields["files"].widget.attrs["class"] = "sr-only"

    def clean_service(self):
        value = self.cleaned_data.get("service") or ""
        if value == self.OTHER_VALUE:
            self.is_extra = True
            self.selected_service = None
            return value
        try:
            service_id = int(value)
        except (TypeError, ValueError) as exc:
            raise forms.ValidationError("サービスを選択してください。") from exc
        service = RegisteredService.objects.filter(pk=service_id, user=self.user).first()
        if service is None or not service.is_uploadable_for(self.period_month):
            raise forms.ValidationError("この提出月の対象領収書月で利用できるサービスを選択してください。")
        self.is_extra = False
        self.selected_service = service
        return value

    def clean_memo(self):
        return (self.cleaned_data.get("memo") or "").strip()

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("service") == self.OTHER_VALUE and not cleaned.get("memo"):
            self.add_error("memo", "「その他」を選択した場合は、どのような領収書かをメモに入力してください。")
        files = cleaned.get("files") or []
        if not files:
            self.add_error("files", "領収書ファイルを1件以上選択してください。")
        return cleaned


class ReceiptUploadForm(forms.ModelForm):
    class Meta:
        model = Receipt
        fields = ["service", "file"]
        widgets = {
            "file": forms.ClearableFileInput(attrs={"accept": ".pdf,.png,.jpg,.jpeg,.webp"}),
        }
        labels = {
            "service": "サービス選択（登録サービス）",
            "file": "領収書ファイルアップロード",
        }
        help_texts = {
            "service": "登録済みサービスから選択します。利用停止済みサービスは、最終領収書月に対応する提出月まで選択できます。",
            "file": "PDF / PNG / JPG / JPEG / WEBP。最大10MB。ファイル本体は最大3ヶ月保存されます。",
        }

    def __init__(self, *args, user=None, period_month=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user is not None:
            self.fields["service"].queryset = RegisteredService.objects.uploadable_for(user, period_month).order_by("-is_active", "name", "billing_type")
        self.fields["service"].empty_label = "登録サービスを選択"
        self.fields["service"].label_from_instance = self.service_label
        self.fields["file"].required = True
        apply_design_classes(self)

    @staticmethod
    def service_label(service: RegisteredService) -> str:
        if service.is_active:
            return service.display_name
        if service.final_receipt_month:
            return f"{service.display_name}（停止済み・最終 {service.final_receipt_month:%Y-%m}）"
        return f"{service.display_name}（停止済み）"

    def clean_file(self):
        uploaded_file = self.cleaned_data.get("file")
        if not uploaded_file:
            return uploaded_file
        max_size = getattr(settings, "MAX_UPLOAD_SIZE", 10 * 1024 * 1024)
        if uploaded_file.size > max_size:
            raise forms.ValidationError(f"ファイルサイズは {max_size // 1024 // 1024}MB 以下にしてください。")
        return uploaded_file


class ExtraReceiptUploadForm(forms.ModelForm):
    """登録サービスに紐づかない返金・プラン変更等の追加領収書フォーム。"""

    memo = forms.CharField(
        label="領収書の内容メモ",
        required=True,
        max_length=500,
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "placeholder": "例: ChatGPTプラン変更に伴う追加請求 / OpenAIからの返金領収書",
            }
        ),
        help_text="必須。どのような領収書かを具体的に記載してください。AIはこのメモを参考にしますが、領収書ファイル内の情報を優先して確認します。",
    )

    class Meta:
        model = Receipt
        fields = ["memo", "file"]
        widgets = {
            "file": forms.ClearableFileInput(attrs={"accept": ".pdf,.png,.jpg,.jpeg,.webp"}),
        }
        labels = {
            "file": "領収書ファイルアップロード",
        }
        help_texts = {
            "file": "PDF / PNG / JPG / JPEG / WEBP。最大10MB。ファイル本体は最大3ヶ月保存されます。",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.is_extra = True
        self.instance.service = None
        self.instance.service_name_snapshot = "その他"
        self.instance.billing_type_snapshot = BillingType.OTHER
        self.fields["file"].required = True
        apply_design_classes(self)

    def clean_memo(self):
        memo = (self.cleaned_data.get("memo") or "").strip()
        if not memo:
            raise forms.ValidationError("どのような領収書かをメモに入力してください。")
        return memo

    def clean_file(self):
        uploaded_file = self.cleaned_data.get("file")
        if not uploaded_file:
            return uploaded_file
        max_size = getattr(settings, "MAX_UPLOAD_SIZE", 10 * 1024 * 1024)
        if uploaded_file.size > max_size:
            raise forms.ValidationError(f"ファイルサイズは {max_size // 1024 // 1024}MB 以下にしてください。")
        return uploaded_file

    def save(self, commit=True):
        receipt = super().save(commit=False)
        receipt.is_extra = True
        receipt.service = None
        receipt.service_name_snapshot = "その他"
        receipt.billing_type_snapshot = BillingType.OTHER
        if commit:
            receipt.save()
        return receipt


class ReceiptFileReplaceForm(forms.Form):
    file = forms.FileField(
        label="修正後ファイル",
        validators=[
            FileExtensionValidator(allowed_extensions=["pdf", "png", "jpg", "jpeg", "webp"]),
            validate_upload_size,
        ],
        widget=forms.ClearableFileInput(attrs={"accept": ".pdf,.png,.jpg,.jpeg,.webp"}),
        help_text="PDF / PNG / JPG / JPEG / WEBP。最大10MB。修正後のファイルも最大3ヶ月保存されます。",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_design_classes(self)


class StaffReceiptReviewForm(forms.Form):
    """管理者がAI結果を確認・補正し、表示ファイル名とチェック項目を確定する。"""

    generated_filename = forms.CharField(
        label="表示・ダウンロード用ファイル名",
        required=False,
        max_length=255,
        help_text="実ファイルの保存名は変更せず、画面・ダウンロード・ZIPで使う名前だけを変更します。",
    )
    ai_check_card_last4 = forms.BooleanField(label="カード末尾7210", required=False)
    ai_check_payee = forms.BooleanField(label="払先", required=False)
    ai_check_service_payee_related = forms.BooleanField(label="サービス / 払先関連", required=False)
    ai_check_date = forms.BooleanField(label="日付", required=False)
    ai_check_amount = forms.BooleanField(label="金額", required=False)
    ai_check_currency = forms.BooleanField(label="通貨", required=False)
    ai_check_period_match = forms.BooleanField(label="対象領収書月一致", required=False)
    admin_review_note = forms.CharField(
        label="管理者確認メモ",
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 4, "placeholder": "確認内容や補足を記載します。ユーザーには表示されません。"}),
    )

    CHECK_FIELDS = (
        "ai_check_card_last4",
        "ai_check_payee",
        "ai_check_service_payee_related",
        "ai_check_date",
        "ai_check_amount",
        "ai_check_currency",
        "ai_check_period_match",
    )

    def __init__(self, *args, receipt: Receipt, **kwargs):
        self.receipt = receipt
        initial = kwargs.setdefault("initial", {})
        initial.setdefault("generated_filename", receipt.display_filename)
        for field_name in self.CHECK_FIELDS:
            initial.setdefault(field_name, getattr(receipt, field_name))
        initial.setdefault("admin_review_note", receipt.admin_review_note)
        super().__init__(*args, **kwargs)
        if receipt.is_extra:
            self.fields["ai_check_service_payee_related"].label = "メモ / 領収書関連"
        target_receipt_month = receipt_month_for_submission(receipt.submission.period_month)
        self.fields["ai_check_period_match"].label = (
            f"対象領収書月一致（{target_receipt_month:%Y年%m月}）"
        )
        apply_design_classes(self)

    def clean_generated_filename(self):
        raw = (self.cleaned_data.get("generated_filename") or "").strip()
        if not raw:
            return ""
        if Path(raw).name != raw or any(separator in raw for separator in ("/", "\\")):
            raise forms.ValidationError("フォルダーを含まないファイル名だけを入力してください。")
        cleaned = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "_", raw)
        cleaned = re.sub(r"\s+", "_", cleaned).strip("._ ")
        if not cleaned:
            raise forms.ValidationError("有効なファイル名を入力してください。")

        current_suffix = Path(
            self.receipt.original_filename
            or (self.receipt.file.name if self.receipt.file else "")
        ).suffix.lower()
        suffix = Path(cleaned).suffix.lower()
        allowed_suffixes = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
        if not suffix and current_suffix:
            cleaned = f"{cleaned}{current_suffix}"
            suffix = current_suffix
        if suffix not in allowed_suffixes:
            raise forms.ValidationError("拡張子は PDF / PNG / JPG / JPEG / WEBP のいずれかにしてください。")
        if current_suffix and suffix != current_suffix:
            raise forms.ValidationError("元ファイルと同じ拡張子を使用してください。")
        return cleaned[:255]

    def clean(self):
        cleaned = super().clean()
        if self.data.get("review_action") == "confirm":
            unchecked = [self.fields[name].label for name in self.CHECK_FIELDS if not cleaned.get(name)]
            if unchecked:
                raise forms.ValidationError(
                    "確認済みにするには、すべての確認項目へチェックを入れてください。未確認: "
                    + "、".join(unchecked)
                )
            if not cleaned.get("generated_filename"):
                self.add_error("generated_filename", "確認済みにするにはファイル名を入力してください。")
        return cleaned

    def save(self, *, reviewed_by: User, confirm: bool) -> Receipt:
        receipt = self.receipt
        previous_display_filename = receipt.display_filename
        receipt.generated_filename = self.cleaned_data.get("generated_filename") or ""
        receipt.admin_filename_overridden = receipt.admin_filename_overridden or bool(
            receipt.generated_filename and receipt.generated_filename != previous_display_filename
        )
        for field_name in self.CHECK_FIELDS:
            setattr(receipt, field_name, bool(self.cleaned_data.get(field_name)))
        receipt.admin_review_note = (self.cleaned_data.get("admin_review_note") or "").strip()
        receipt.ai_filename_checked_at = receipt.ai_filename_checked_at or timezone.now()

        if confirm:
            receipt.admin_review_status = ReceiptAdminReviewStatus.CONFIRMED
            receipt.admin_reviewed_by = reviewed_by
            receipt.admin_reviewed_at = timezone.now()
            receipt.ai_filename_status = ReceiptFilenameStatus.GENERATED
            receipt.ai_period_check_status = ReceiptPeriodCheckStatus.MATCHED
            target_receipt_month = receipt_month_for_submission(receipt.submission.period_month)
            receipt.ai_period_check_memo = (
                f"管理者 {reviewed_by.get_username()} が、提出月 {receipt.submission.period_month:%Y-%m} の "
                f"対象領収書月 {target_receipt_month:%Y-%m} との一致を確認しました。"
            )
        else:
            receipt.admin_review_status = ReceiptAdminReviewStatus.NOT_REVIEWED
            receipt.admin_reviewed_by = None
            receipt.admin_reviewed_at = None
            if receipt.ai_has_check_result and not receipt.ai_all_checks_passed:
                receipt.ai_filename_status = ReceiptFilenameStatus.NEEDS_REVIEW

        receipt.save(
            update_fields=[
                "generated_filename",
                "admin_filename_overridden",
                *self.CHECK_FIELDS,
                "admin_review_note",
                "admin_review_status",
                "admin_reviewed_by",
                "admin_reviewed_at",
                "ai_filename_status",
                "ai_filename_checked_at",
                "ai_period_check_status",
                "ai_period_check_memo",
                "updated_at",
            ]
        )
        return receipt


class CardStatementUploadForm(forms.ModelForm):
    class Meta:
        model = CardStatement
        fields = ["file"]
        widgets = {
            "file": forms.ClearableFileInput(attrs={"accept": ".pdf,.png,.jpg,.jpeg,.webp"}),
        }
        labels = {"file": "全社ご利用代金明細書"}
        help_texts = {
            "file": "全ユーザー分の利用履歴が載った明細書を選択してください。PDF / PNG / JPG / JPEG / WEBP、最大10MB。ファイル本体は最大3ヶ月保存されます。",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_design_classes(self)


class RegisterForm(UserCreationForm):
    username = forms.EmailField(
        label="メールアドレス",
        max_length=150,
        widget=forms.EmailInput(attrs={"autocomplete": "email", "placeholder": "user@example.com"}),
    )

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username",)
        labels = {"username": "メールアドレス"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_design_classes(self)

    def clean_username(self):
        email = normalize_account_email(self.cleaned_data["username"])
        ensure_unique_account_email(email)
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        email = self.cleaned_data["username"]
        user.username = email
        user.email = email
        if commit:
            user.save()
        return user


class StaffSuperuserEmailForm(forms.Form):
    email = forms.EmailField(
        label="スーパーアカウントの連絡先メールアドレス",
        required=False,
        max_length=254,
        widget=forms.EmailInput(attrs={"autocomplete": "email", "placeholder": "空欄にできます"}),
        help_text=(
            "空欄で保存すると、現在のメールアドレスを一般ユーザーのアカウント名として使用できるようになります。"
            "スーパーアカウントのログイン名は変更されません。"
        ),
    )

    def __init__(self, *args, user: User, **kwargs):
        if not user.is_superuser:
            raise ValueError("スーパーアカウントだけが連絡先メールアドレスを変更できます。")
        self.user = user
        kwargs.setdefault("initial", {"email": user.email})
        super().__init__(*args, **kwargs)
        apply_design_classes(self)

    def clean_email(self):
        email = normalize_account_email(self.cleaned_data.get("email") or "")
        if email:
            ensure_unique_account_email(email, exclude_user=self.user)
        return email

    def save(self) -> User:
        self.user.email = self.cleaned_data["email"]
        self.user.save(update_fields=["email"])
        return self.user


class StaffUserCreateForm(forms.Form):
    ROLE_GENERAL = "general"
    ROLE_ADMIN = "admin"
    ROLE_CHOICES = ((ROLE_GENERAL, "一般ユーザー"), (ROLE_ADMIN, "管理者ユーザー"))

    email = forms.EmailField(
        label="新しく登録するユーザー名（メールアドレス）",
        max_length=150,
        widget=forms.EmailInput(attrs={"autocomplete": "off", "placeholder": "user@example.com"}),
        help_text="このメールアドレスをログイン時のアカウント名として使います。",
    )
    account_role = forms.ChoiceField(
        label="権限",
        choices=ROLE_CHOICES,
        initial=ROLE_GENERAL,
        required=False,
        help_text="管理者ユーザーを発行できるのはスーパーアカウントだけです。",
    )
    account_status = forms.ChoiceField(
        label="初期ステータス",
        choices=UserAccountStatus.choices,
        initial=UserAccountStatus.STOPPED,
        required=False,
        help_text="停止中ユーザーにはリマインダーメール・テストメールを送信しません。サービス利用開始時は自動で利用中に切り替わります。",
    )

    def __init__(self, *args, allow_admin_role: bool = False, **kwargs):
        self.allow_admin_role = allow_admin_role
        super().__init__(*args, **kwargs)
        if not allow_admin_role:
            self.fields.pop("account_role", None)
        apply_design_classes(self)

    def clean_email(self):
        email = normalize_account_email(self.cleaned_data["email"])
        ensure_unique_account_email(email)
        return email

    def clean_account_role(self):
        role = self.cleaned_data.get("account_role") or self.ROLE_GENERAL
        if role == self.ROLE_ADMIN and not self.allow_admin_role:
            raise forms.ValidationError("管理者ユーザーを発行する権限がありません。")
        return role

    def clean_account_status(self):
        return self.cleaned_data.get("account_status") or UserAccountStatus.STOPPED

    def save(self, *, created_by: User) -> tuple[User, str]:
        email = self.cleaned_data["email"]
        role = self.cleaned_data.get("account_role") or self.ROLE_GENERAL
        user = User(
            username=email,
            email=email,
            is_active=True,
            is_staff=role == self.ROLE_ADMIN,
            is_superuser=False,
        )
        password = generate_initial_password(user=user)
        user.set_password(password)
        user.full_clean()
        user.save()

        profile = user.profile
        profile.account_status = (
            UserAccountStatus.STOPPED if user.is_staff else self.cleaned_data["account_status"]
        )
        profile.must_change_password = True
        profile.created_by = created_by
        profile.mark_initial_password_generated()
        return user, password


class StaffUserStatusForm(forms.Form):
    user_id = forms.IntegerField(widget=forms.HiddenInput)
    account_status = forms.ChoiceField(label="ステータス", choices=UserAccountStatus.choices)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_design_classes(self)

    def clean_user_id(self):
        user_id = self.cleaned_data["user_id"]
        try:
            user = User.objects.get(pk=user_id, is_active=True, is_staff=False, is_superuser=False)
        except User.DoesNotExist as exc:
            raise forms.ValidationError("対象ユーザーが見つかりません。") from exc
        UserProfile.objects.get_or_create(user=user)
        return user

    def save(self, *, updated_by: User | None = None) -> User:
        user = self.cleaned_data["user_id"]
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.account_status = self.cleaned_data["account_status"]
        profile.save(update_fields=["account_status", "updated_at"])
        return user


class StaffUserPasswordResetForm(forms.Form):
    user_id = forms.IntegerField(widget=forms.HiddenInput)
    new_password = forms.CharField(
        label="新パスワード",
        required=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password", "placeholder": "空欄ならランダム生成"}),
        help_text="空欄で送信するとランダムパスワードを生成します。入力する場合は確認欄にも同じ値を入れてください。",
    )
    new_password_confirm = forms.CharField(
        label="新パスワード確認",
        required=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password", "placeholder": "確認用"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_design_classes(self)

    def clean_user_id(self):
        user_id = self.cleaned_data["user_id"]
        try:
            user = User.objects.get(pk=user_id, is_active=True, is_superuser=False)
        except User.DoesNotExist as exc:
            raise forms.ValidationError("対象ユーザーが見つかりません。") from exc
        UserProfile.objects.get_or_create(user=user)
        return user

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("new_password") or ""
        password_confirm = cleaned_data.get("new_password_confirm") or ""
        user = cleaned_data.get("user_id")
        if password or password_confirm:
            if password != password_confirm:
                self.add_error("new_password_confirm", "新パスワードと確認欄が一致しません。")
            elif user:
                try:
                    validate_password(password, user=user)
                except ValidationError as exc:
                    self.add_error("new_password", exc)
        return cleaned_data

    def save(self, *, updated_by: User | None = None) -> tuple[User, str, bool]:
        user = self.cleaned_data["user_id"]
        manual_password = self.cleaned_data.get("new_password") or ""
        generated_random = not bool(manual_password)
        password = manual_password or generate_initial_password(user=user)
        user.set_password(password)
        user.save(update_fields=["password"])

        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.must_change_password = True
        profile.initial_password_generated_at = timezone.now()
        profile.password_changed_at = None
        profile.save(update_fields=["must_change_password", "initial_password_generated_at", "password_changed_at", "updated_at"])
        return user, password, generated_random


class StaffUserRoleForm(forms.Form):
    ROLE_GENERAL = StaffUserCreateForm.ROLE_GENERAL
    ROLE_ADMIN = StaffUserCreateForm.ROLE_ADMIN
    user_id = forms.IntegerField(widget=forms.HiddenInput)
    account_role = forms.ChoiceField(label="権限", choices=StaffUserCreateForm.ROLE_CHOICES)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_design_classes(self)

    def clean_user_id(self):
        user_id = self.cleaned_data["user_id"]
        try:
            return User.objects.get(pk=user_id, is_active=True, is_superuser=False)
        except User.DoesNotExist as exc:
            raise forms.ValidationError("対象ユーザーが見つかりません。") from exc

    def save(self) -> User:
        user = self.cleaned_data["user_id"]
        role = self.cleaned_data["account_role"]
        user.is_staff = role == self.ROLE_ADMIN
        user.save(update_fields=["is_staff"])
        UserProfile.objects.get_or_create(user=user)
        if not user.is_staff:
            sync_user_account_status_from_services(user.pk)
        return user


class EmailOrUsernameAuthenticationForm(AuthenticationForm):
    username = forms.CharField(
        label="メールアドレス / 管理者ユーザー名",
        max_length=150,
        widget=forms.TextInput(attrs={"autofocus": True, "autocomplete": "username"}),
    )

    def __init__(self, request=None, *args, **kwargs):
        super().__init__(request, *args, **kwargs)
        apply_design_classes(self)

    def clean_username(self):
        value = self.cleaned_data["username"].strip()
        if "@" in value:
            return normalize_account_email(value)
        return value


class StyledPasswordChangeForm(PasswordChangeForm):
    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        apply_design_classes(self)




class EmailReminderScheduleForm(forms.ModelForm):
    ALLOWED_PLACEHOLDERS = {
        "app_name",
        "user_name",
        "target_month",
        "receipt_month",
        "upload_url",
        "missing_services",
        "api_pending_services",
    }

    reminder_day = forms.IntegerField(
        label="リマインダー日",
        min_value=1,
        max_value=28,
        help_text="未アップロードのサブスク等がある利用中ユーザーへ送信します。APIのみ未確認の場合は送信しません。",
    )
    warning_day = forms.IntegerField(
        label="警告日",
        min_value=1,
        max_value=28,
        help_text="未アップロード項目またはAPI利用確認待ちがある利用中ユーザーへ送信します。",
    )

    class Meta:
        model = EmailReminderSchedule
        fields = (
            "reminder_day",
            "warning_day",
            "initial_subject_template",
            "initial_body_template",
            "urgent_subject_template",
            "urgent_body_template",
        )
        widgets = {
            "initial_body_template": forms.Textarea(attrs={"rows": 10}),
            "urgent_body_template": forms.Textarea(attrs={"rows": 12}),
        }
        help_texts = {
            "initial_subject_template": "利用可能な変数: {app_name} {target_month} {receipt_month} {user_name}",
            "initial_body_template": "利用可能な変数: {app_name} {user_name} {target_month} {receipt_month} {upload_url} {missing_services}",
            "urgent_subject_template": "【重要】は未入力でも送信時に自動付与されます。",
            "urgent_body_template": "利用可能な変数: {app_name} {user_name} {target_month} {receipt_month} {upload_url} {missing_services} {api_pending_services}",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_design_classes(self)

    def _validate_placeholders(self, field_name: str):
        import string

        value = self.cleaned_data.get(field_name) or ""
        try:
            names = {name for _literal, name, _format_spec, _conversion in string.Formatter().parse(value) if name}
        except ValueError as exc:
            self.add_error(field_name, f"テンプレートの波括弧が正しくありません: {exc}")
            return
        unknown = sorted(names - self.ALLOWED_PLACEHOLDERS)
        if unknown:
            self.add_error(field_name, "利用できない変数があります: " + ", ".join(unknown))

    def clean(self):
        cleaned_data = super().clean()
        reminder_day = cleaned_data.get("reminder_day")
        warning_day = cleaned_data.get("warning_day")
        if reminder_day and warning_day and warning_day <= reminder_day:
            self.add_error("warning_day", "警告日はリマインダー日より後の日付にしてください。")
        for field_name in (
            "initial_subject_template",
            "initial_body_template",
            "urgent_subject_template",
            "urgent_body_template",
        ):
            self._validate_placeholders(field_name)
        return cleaned_data


class StaffEmailTestForm(forms.Form):
    to_email = forms.EmailField(
        label="テスト送信先メールアドレス",
        widget=forms.EmailInput(attrs={"placeholder": "user@example.com"}),
    )
    subject = forms.CharField(
        label="件名",
        max_length=255,
        initial="ReceiptHub メール送信テスト",
    )
    body = forms.CharField(
        label="本文",
        widget=forms.Textarea(attrs={"rows": 6}),
        initial="ReceiptHub からのテストメールです。SMTP設定が正しく動作しています。",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_design_classes(self)
