from __future__ import annotations

import string
from datetime import date

from django import forms
from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm, UserCreationForm
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.utils.crypto import get_random_string

from .models import Receipt, RegisteredService


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
        return cleaned.replace(day=1)


def current_month():
    today = date.today()
    return today.replace(day=1)


class MonthSelectForm(forms.Form):
    month = MonthField(label="提出月", initial=current_month)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_design_classes(self)


class StaffServiceForm(forms.ModelForm):
    """管理者が一般ユーザーへ利用サービスを割り当てるためのフォーム。"""

    class Meta:
        model = RegisteredService
        fields = ["user", "name", "billing_type", "is_active", "memo"]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "例: OpenAI API / Notion / AWS"}),
            "memo": forms.Textarea(attrs={"rows": 3, "placeholder": "任意: 用途、担当、契約メモなど"}),
        }
        labels = {
            "user": "対象ユーザー",
        }
        help_texts = {
            "user": "このサービスを利用できる一般ユーザーを選択します。",
            "is_active": "停止すると、ユーザーのアップロード画面の選択肢から外れます。過去の提出履歴は残ります。",
        }

    def __init__(self, *args, fixed_user: User | None = None, **kwargs):
        self.fixed_user = fixed_user
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
        apply_design_classes(self)

    def clean_name(self):
        name = " ".join((self.cleaned_data.get("name") or "").strip().split())
        if not name:
            raise forms.ValidationError("サービス名を入力してください。")
        return name

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
        name = cleaned.get("name")
        if user and name:
            duplicate = RegisteredService.objects.filter(user=user, name__iexact=name).exclude(pk=self.instance.pk)
            if duplicate.exists():
                self.add_error("name", "このユーザーには同じサービス名がすでに登録されています。")
        return cleaned


# 旧バージョンから参照される可能性があるため、互換用に残す。
RegisteredServiceForm = StaffServiceForm


class ReceiptUploadForm(forms.ModelForm):
    class Meta:
        model = Receipt
        fields = ["service", "amount", "currency", "issued_on", "memo", "file"]
        widgets = {
            "issued_on": forms.DateInput(attrs={"type": "date"}),
            "memo": forms.Textarea(attrs={"rows": 3, "placeholder": "任意: 請求期間、補足など"}),
            "file": forms.ClearableFileInput(attrs={"accept": ".pdf,.png,.jpg,.jpeg,.webp"}),
        }
        help_texts = {
            "service": "管理者が登録した利用サービスから選択します。",
            "amount": "任意。確認用に税込金額などを入力できます。",
            "file": "PDF / PNG / JPG / JPEG / WEBP。最大10MB。ファイル本体は最大3ヶ月保存されます。",
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user is not None:
            self.fields["service"].queryset = RegisteredService.objects.filter(user=user, is_active=True).order_by("name")
        self.fields["service"].empty_label = "利用サービスを選択"
        apply_design_classes(self)

    def clean_currency(self):
        currency = self.cleaned_data.get("currency", "JPY")
        return currency.upper()

    def clean_file(self):
        uploaded_file = self.cleaned_data.get("file")
        if not uploaded_file:
            return uploaded_file
        max_size = getattr(settings, "MAX_UPLOAD_SIZE", 10 * 1024 * 1024)
        if uploaded_file.size > max_size:
            raise forms.ValidationError(f"ファイルサイズは {max_size // 1024 // 1024}MB 以下にしてください。")
        return uploaded_file


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
