from __future__ import annotations

import string
from datetime import date

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
    Receipt,
    RegisteredService,
    ServiceCatalog,
    ServiceDeactivationSource,
    ServiceRegistrationSource,
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


def ensure_unique_account_email(email: str):
    if User.objects.filter(username__iexact=email).exists() or User.objects.filter(email__iexact=email).exists():
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_design_classes(self)


def same_service_identity_q(catalog: ServiceCatalog) -> Q:
    """同一サービス判定。サービス名だけでなく支払い種別も含める。"""

    return Q(catalog_service=catalog) | Q(name__iexact=catalog.name, billing_type=catalog.billing_type)


class ServiceCatalogForm(forms.ModelForm):
    """管理者がユーザーに公開するサービスマスターを登録するフォーム。"""

    class Meta:
        model = ServiceCatalog
        fields = ["name", "billing_type", "is_active", "memo"]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "例: OpenAI API / Notion / AWS"}),
            "memo": forms.Textarea(attrs={"rows": 3, "placeholder": "任意: ユーザー向け補足、契約メモなど"}),
        }
        help_texts = {
            "is_active": "OFFにすると、ユーザーの新規利用登録画面と管理者の割り当て画面から外れます。既存の利用履歴は残ります。",
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
    catalog_service = forms.ModelChoiceField(
        label="新しく利用するサービス",
        queryset=ServiceCatalog.objects.none(),
        empty_label="サービスを選択",
        help_text="管理者が登録したサービスマスターから選択します。一覧にない場合は管理者へ追加を依頼してください。",
    )
    memo = forms.CharField(
        label="メモ",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "任意: 用途、補足など"}),
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
        active_duplicate = RegisteredService.objects.filter(user=self.user, is_active=True).filter(same_service_identity_q(catalog))
        if active_duplicate.exists():
            raise forms.ValidationError("このサービス・支払い種別はすでに利用中です。")
        return catalog

    def save(self) -> RegisteredService:
        catalog = self.cleaned_data["catalog_service"]
        memo = self.cleaned_data.get("memo", "")
        service = RegisteredService.objects.filter(user=self.user).filter(same_service_identity_q(catalog)).order_by("id").first()
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
        service.save()
        return service


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
            "service": "登録済みサービスから選択します。利用停止済みサービスは最終領収書月まで選択できます。",
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


class StaffUserCreateForm(forms.Form):
    email = forms.EmailField(
        label="新しく登録するユーザー名（メールアドレス）",
        max_length=150,
        widget=forms.EmailInput(attrs={"autocomplete": "off", "placeholder": "user@example.com"}),
        help_text="このメールアドレスをログイン時のアカウント名として使います。",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_design_classes(self)

    def clean_email(self):
        email = normalize_account_email(self.cleaned_data["email"])
        ensure_unique_account_email(email)
        return email

    def save(self, *, created_by: User) -> tuple[User, str]:
        email = self.cleaned_data["email"]
        user = User(username=email, email=email, is_active=True, is_staff=False, is_superuser=False)
        password = generate_initial_password(user=user)
        user.set_password(password)
        user.full_clean()
        user.save()

        profile = user.profile
        profile.must_change_password = True
        profile.created_by = created_by
        profile.mark_initial_password_generated()
        return user, password


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
