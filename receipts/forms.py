from __future__ import annotations

from datetime import date

from django import forms
from django.conf import settings
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User

from .models import Receipt, RegisteredService


def apply_design_classes(form):
    for field in form.fields.values():
        widget = field.widget
        if isinstance(widget, forms.CheckboxInput):
            widget.attrs.setdefault("class", "form-check-input")
        elif isinstance(widget, forms.ClearableFileInput):
            widget.attrs.setdefault("class", "form-control")
        elif isinstance(widget, forms.Select):
            widget.attrs.setdefault("class", "form-select")
        else:
            widget.attrs.setdefault("class", "form-control")


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


class RegisteredServiceForm(forms.ModelForm):
    class Meta:
        model = RegisteredService
        fields = ["name", "billing_type", "is_active", "memo"]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "例: OpenAI API / Notion / AWS"}),
            "memo": forms.Textarea(attrs={"rows": 3, "placeholder": "任意: 用途、担当、契約メモなど"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_design_classes(self)


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
            "amount": "任意。確認用に税込金額などを入力できます。",
            "file": "PDF / PNG / JPG / JPEG / WEBP。最大10MB。ファイル本体は最大3ヶ月保存されます。",
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user is not None:
            self.fields["service"].queryset = RegisteredService.objects.filter(user=user, is_active=True).order_by("name")
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
    email = forms.EmailField(label="メールアドレス", required=False)

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "email")
        labels = {"username": "ユーザー名"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_design_classes(self)

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data.get("email", "")
        if commit:
            user.save()
        return user
