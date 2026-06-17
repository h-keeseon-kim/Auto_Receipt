from __future__ import annotations

import csv
import zipfile
from io import BytesIO, StringIO
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.contrib.auth.views import PasswordChangeView
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Prefetch, Q
from django.http import FileResponse, Http404, HttpResponse
from django.utils.http import url_has_allowed_host_and_scheme
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.text import slugify

from .ai_filename import generate_ai_receipt_filename
from .forms import (
    MonthSelectForm,
    ReceiptFileReplaceForm,
    ReceiptUploadForm,
    RegisterForm,
    ServiceCatalogForm,
    StaffServiceForm,
    StaffUserCreateForm,
    StyledPasswordChangeForm,
    UserServiceRegistrationForm,
    UserServiceStopForm,
    current_month,
)
from .models import (
    Receipt,
    ReceiptFilenameStatus,
    ReceiptPeriodCheckStatus,
    RegisteredService,
    ServiceCatalog,
    ServiceDeactivationSource,
    ServiceRegistrationSource,
    Submission,
    SubmissionStatus,
    UserProfile,
    receipt_expiry_from,
)


def safe_part(value: str, fallback: str = "item") -> str:
    value = slugify(value or "", allow_unicode=True)
    return value or fallback


def parse_month_from_request(request):
    form = MonthSelectForm(request.GET or None)
    if form.is_valid():
        return form.cleaned_data["month"], form
    initial_month = current_month()
    return initial_month, MonthSelectForm(initial={"month": initial_month})


def month_query(value) -> str:
    return value.strftime("%Y-%m")


def add_validation_errors(form, exc: ValidationError):
    if hasattr(exc, "message_dict"):
        for field, errors in exc.message_dict.items():
            target = field if field in form.fields else None
            for error in errors:
                form.add_error(target, error)
    else:
        for error in getattr(exc, "messages", [str(exc)]):
            form.add_error(None, error)


def month_label(value) -> str:
    return value.strftime("%Y年%m月")


def receipt_period_mismatch_message(receipt: Receipt) -> str:
    expected = month_label(receipt.submission.period_month)
    actual = receipt.ai_receipt_month.replace("-", "年") + "月" if receipt.ai_receipt_month else "不明"
    return (
        f"アップロードされた領収書は提出月（{expected}）ではなく {actual} の領収書として判定されました。"
        "正しい当月分の領収書を再度アップロードしてください。"
    )


def apply_period_check_to_receipt(receipt: Receipt, result) -> list[str]:
    """AIで抽出した日付が提出月と一致するかをReceiptに反映する。"""

    expected_month = receipt.submission.period_month if receipt.submission_id else None
    payment_date = getattr(result, "payment_date", None) if result is not None else None
    if payment_date:
        actual_month = payment_date.replace(day=1)
        receipt.ai_receipt_month = actual_month.strftime("%Y-%m")
        if expected_month and actual_month == expected_month:
            receipt.ai_period_check_status = ReceiptPeriodCheckStatus.MATCHED
            receipt.ai_period_check_memo = f"領収書日付 {payment_date:%Y-%m-%d} は提出月 {expected_month:%Y-%m} と一致しています。"
        elif expected_month:
            receipt.ai_period_check_status = ReceiptPeriodCheckStatus.MISMATCHED
            receipt.ai_period_check_memo = (
                f"領収書日付 {payment_date:%Y-%m-%d} は提出月 {expected_month:%Y-%m} と一致しません。"
                "ユーザーへ再アップロードを依頼してください。"
            )
        else:
            receipt.ai_period_check_status = ReceiptPeriodCheckStatus.UNKNOWN
            receipt.ai_period_check_memo = f"領収書日付 {payment_date:%Y-%m-%d} を抽出しましたが、提出月を確認できませんでした。"
    else:
        receipt.ai_receipt_month = ""
        if result is not None and getattr(result, "status", "") == ReceiptFilenameStatus.SKIPPED:
            receipt.ai_period_check_status = ReceiptPeriodCheckStatus.NOT_CHECKED
            receipt.ai_period_check_memo = "AIファイル名修正が未実行のため、提出月との一致確認も未実行です。"
        else:
            receipt.ai_period_check_status = ReceiptPeriodCheckStatus.UNKNOWN
            receipt.ai_period_check_memo = "領収書日付をAIで確認できなかったため、提出月との一致確認はできませんでした。"
    return ["ai_receipt_month", "ai_period_check_status", "ai_period_check_memo"]


def apply_ai_filename_to_receipt(receipt: Receipt):
    if not receipt.file_available:
        return None

    try:
        with receipt.file.open("rb") as file_obj:
            file_bytes = file_obj.read()
    except Exception as exc:
        receipt.generated_filename = ""
        receipt.ai_filename_status = ReceiptFilenameStatus.FAILED
        receipt.ai_filename_admin_memo = f"AIファイル名作成前にファイルを読み込めませんでした: {exc}"
        receipt.ai_filename_checked_at = timezone.now()
        update_fields = [
            "generated_filename",
            "ai_filename_status",
            "ai_filename_admin_memo",
            "ai_filename_checked_at",
            *apply_period_check_to_receipt(receipt, None),
            "updated_at",
        ]
        receipt.save(update_fields=update_fields)
        return None

    result = generate_ai_receipt_filename(
        file_bytes=file_bytes,
        original_filename=receipt.original_filename or Path(receipt.file.name).name,
        content_type=receipt.content_type,
        service_display_name=receipt.service_display_name_snapshot,
    )

    receipt.generated_filename = result.suggested_filename[:255] if result.suggested_filename else ""
    receipt.ai_filename_status = result.status
    receipt.ai_filename_admin_memo = result.admin_memo
    receipt.ai_filename_checked_at = timezone.now()
    receipt.ai_extracted_payee = result.payee[:160] if result.payee else ""
    receipt.ai_extracted_card_last4 = result.card_last4[-4:] if result.card_last4 else ""

    update_fields = [
        "generated_filename",
        "ai_filename_status",
        "ai_filename_admin_memo",
        "ai_filename_checked_at",
        "ai_extracted_payee",
        "ai_extracted_card_last4",
        *apply_period_check_to_receipt(receipt, result),
        "updated_at",
    ]
    if result.status == ReceiptFilenameStatus.GENERATED:
        if result.payment_date is not None:
            receipt.issued_on = result.payment_date
            update_fields.append("issued_on")
        if result.amount is not None:
            receipt.amount = result.amount
            update_fields.append("amount")
        if result.currency:
            receipt.currency = result.currency
            update_fields.append("currency")

    receipt.save(update_fields=update_fields)
    return result


def managed_users_queryset():
    return User.objects.filter(is_active=True, is_staff=False, is_superuser=False).order_by("username")


def get_managed_user(user_id: int):
    return get_object_or_404(User, pk=user_id, is_active=True, is_staff=False, is_superuser=False)


def home_redirect(request):
    if not request.user.is_authenticated:
        return redirect("login")
    if request.user.is_staff:
        return redirect("history")
    return redirect("user_services")


class ForcedPasswordChangeView(LoginRequiredMixin, PasswordChangeView):
    form_class = StyledPasswordChangeForm
    template_name = "registration/password_change_form.html"
    success_url = reverse_lazy("password_change_done")

    def form_valid(self, form):
        response = super().form_valid(form)
        profile, _ = UserProfile.objects.get_or_create(user=self.request.user)
        profile.mark_password_changed()
        self.request.session.pop("password_change_notice_shown", None)
        messages.success(self.request, "パスワードを変更しました。続けて機能をご利用ください。")
        return response


def register(request):
    if not settings.ALLOW_SIGNUP:
        messages.info(request, "ユーザー登録は現在無効です。アカウントが必要な場合は管理者に作成を依頼してください。")
        return redirect("login")
    if request.user.is_authenticated:
        return redirect("home")
    if request.method == "POST":
        form = RegisterForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            messages.success(request, "アカウントを作成しました。")
            return redirect("user_services")
    else:
        form = RegisterForm()
    return render(request, "registration/register.html", {"form": form})


@login_required
def dashboard(request):
    if request.user.is_staff:
        return redirect("history")
    selected_month, month_form = parse_month_from_request(request)
    submission, _ = Submission.objects.get_or_create(user=request.user, period_month=selected_month)
    uploadable_services = RegisteredService.objects.uploadable_for(request.user, selected_month)
    receipts = submission.receipts.select_related("service").all()

    if request.method == "POST":
        action = request.POST.get("action")
        if submission.is_submitted:
            messages.error(request, "この提出月はすでに提出済みのため編集できません。")
            return redirect(submission.get_absolute_url())

        if action == "add_receipt":
            receipt_form = ReceiptUploadForm(request.POST, request.FILES, user=request.user, period_month=selected_month)
            if receipt_form.is_valid():
                upload = request.FILES["file"]
                receipt = receipt_form.save(commit=False)
                receipt.submission = submission
                receipt.service_name_snapshot = receipt.service.name
                receipt.billing_type_snapshot = receipt.service.billing_type
                receipt.original_filename = upload.name
                receipt.file_size = upload.size
                receipt.content_type = getattr(upload, "content_type", "") or ""
                receipt.expires_at = receipt_expiry_from(timezone.now())
                try:
                    receipt.full_clean()
                    receipt.save()
                except ValidationError as exc:
                    add_validation_errors(receipt_form, exc)
                else:
                    apply_ai_filename_to_receipt(receipt)
                    receipt.refresh_from_db()
                    if receipt.needs_period_reupload:
                        error_message = receipt_period_mismatch_message(receipt)
                        receipt.delete()
                        messages.error(request, error_message)
                        return redirect(f"{reverse('dashboard')}?month={month_query(selected_month)}")
                    messages.success(request, f"{receipt.service_display_name_snapshot} の領収書を追加しました。")
                    return redirect(f"{reverse('dashboard')}?month={month_query(selected_month)}")
        elif action == "submit":
            try:
                submission.submit()
                messages.success(request, f"{selected_month:%Y年%m月}分を提出しました。")
                return redirect(submission.get_absolute_url())
            except ValidationError as exc:
                messages.error(request, exc.message if hasattr(exc, "message") else exc.messages[0])
            receipt_form = ReceiptUploadForm(user=request.user, period_month=selected_month)
        else:
            messages.error(request, "不明な操作です。")
            receipt_form = ReceiptUploadForm(user=request.user, period_month=selected_month)
    else:
        receipt_form = ReceiptUploadForm(user=request.user, period_month=selected_month)

    return render(
        request,
        "receipts/dashboard.html",
        {
            "month_form": month_form,
            "receipt_form": receipt_form,
            "submission": submission,
            "receipts": receipts,
            "uploadable_services": uploadable_services,
            "selected_month": selected_month,
            "retention_months": settings.RECEIPT_RETENTION_MONTHS,
        },
    )


@login_required
def user_services(request):
    services = RegisteredService.objects.filter(user=request.user).select_related("catalog_service", "registered_by", "deactivated_by").order_by("-is_active", "name", "billing_type")
    active_services = [service for service in services if service.is_active]
    stopped_services = [service for service in services if not service.is_active]
    available_catalog_count = ServiceCatalog.objects.filter(is_active=True).count()
    return render(
        request,
        "receipts/user_services.html",
        {
            "services": services,
            "active_services": active_services,
            "stopped_services": stopped_services,
            "available_catalog_count": available_catalog_count,
        },
    )


@login_required
def user_service_create(request):
    if request.method == "POST":
        form = UserServiceRegistrationForm(request.POST, user=request.user)
        if form.is_valid():
            service = form.save()
            messages.success(request, f"{service.display_name} を利用サービスとして登録しました。管理者画面にもユーザー登録として記録されます。")
            return redirect("user_services")
    else:
        form = UserServiceRegistrationForm(user=request.user)
    return render(
        request,
        "receipts/user_service_form.html",
        {
            "title": "サービス利用登録",
            "form": form,
            "submit_label": "登録する",
            "back_url": reverse("user_services"),
        },
    )


@login_required
def user_service_stop(request, pk: int):
    service = get_object_or_404(RegisteredService, pk=pk, user=request.user, is_active=True)
    if request.method == "POST":
        form = UserServiceStopForm(request.POST, service=service)
        if form.is_valid():
            form.save(stopped_by=request.user)
            messages.success(
                request,
                f"{service.display_name} を利用停止にしました。最後にアップロードすべき領収書月は {service.final_receipt_month:%Y年%m月} として管理者画面にも記録されます。",
            )
            return redirect("user_services")
    else:
        form = UserServiceStopForm(service=service)
    return render(
        request,
        "receipts/user_service_stop.html",
        {
            "service": service,
            "form": form,
            "back_url": reverse("user_services"),
        },
    )


@staff_member_required
def staff_services(request):
    users = list(
        managed_users_queryset().annotate(
            total_service_count=Count("registered_services", distinct=True),
            active_service_count=Count(
                "registered_services",
                filter=Q(registered_services__is_active=True),
                distinct=True,
            ),
            user_registered_count=Count(
                "registered_services",
                filter=Q(registered_services__registration_source=ServiceRegistrationSource.USER),
                distinct=True,
            ),
            user_stopped_count=Count(
                "registered_services",
                filter=Q(registered_services__deactivation_source=ServiceDeactivationSource.USER),
                distinct=True,
            ),
        )
    )

    selected_user = None
    selected_user_id = request.GET.get("user")
    if selected_user_id:
        selected_user = next((account for account in users if str(account.pk) == selected_user_id), None)
        if selected_user is None:
            raise Http404("対象ユーザーが見つかりません。")
    elif users:
        selected_user = users[0]

    services = RegisteredService.objects.none()
    user_change_services = RegisteredService.objects.none()
    if selected_user is not None:
        services = (
            RegisteredService.objects.select_related("user", "catalog_service", "registered_by", "deactivated_by")
            .filter(user=selected_user)
            .order_by("-is_active", "name", "billing_type")
        )
        user_change_services = services.filter(
            Q(registration_source=ServiceRegistrationSource.USER) | Q(deactivation_source=ServiceDeactivationSource.USER)
        ).order_by("-updated_at")

    catalog_queryset = ServiceCatalog.objects.annotate(
        assigned_count=Count("registered_services", distinct=True),
        active_user_count=Count("registered_services", filter=Q(registered_services__is_active=True), distinct=True),
    ).order_by("name", "billing_type")
    catalog_paginator = Paginator(catalog_queryset, 25)
    catalog_page_obj = catalog_paginator.get_page(request.GET.get("catalog_page"))

    return render(
        request,
        "receipts/staff_services.html",
        {
            "users": users,
            "services": services,
            "selected_user": selected_user,
            "catalog_page_obj": catalog_page_obj,
            "user_change_services": user_change_services,
        },
    )


@staff_member_required
def staff_user_services(request, user_id: int):
    managed_user = get_managed_user(user_id)
    if request.method == "POST":
        form = StaffServiceForm(request.POST, fixed_user=managed_user, registered_by=request.user)
        if form.is_valid():
            service = form.save()
            messages.success(request, f"{managed_user.username} に {service.display_name} を登録しました。")
            return redirect("staff_user_services", user_id=managed_user.pk)
    else:
        form = StaffServiceForm(fixed_user=managed_user, registered_by=request.user)
    services = RegisteredService.objects.select_related("catalog_service", "registered_by", "deactivated_by").filter(user=managed_user).order_by("-is_active", "name", "billing_type")
    return render(
        request,
        "receipts/staff_user_services.html",
        {
            "managed_user": managed_user,
            "services": services,
            "form": form,
        },
    )


@staff_member_required
def staff_service_create(request):
    initial = {}
    requested_user_id = request.GET.get("user")
    if requested_user_id:
        initial["user"] = get_managed_user(requested_user_id).pk
    if request.method == "POST":
        form = StaffServiceForm(request.POST, registered_by=request.user)
        if form.is_valid():
            service = form.save()
            messages.success(request, f"{service.user.username} に {service.display_name} を登録しました。")
            return redirect("staff_user_services", user_id=service.user_id)
    else:
        form = StaffServiceForm(initial=initial, registered_by=request.user)
    return render(
        request,
        "receipts/staff_service_form.html",
        {
            "title": "利用サービス登録",
            "form": form,
            "back_url": reverse("staff_services"),
            "submit_label": "登録する",
        },
    )


@staff_member_required
def staff_service_update(request, pk: int):
    service = get_object_or_404(RegisteredService.objects.select_related("user"), pk=pk, user__is_staff=False, user__is_superuser=False)
    if request.method == "POST":
        form = StaffServiceForm(request.POST, instance=service, fixed_user=service.user, registered_by=request.user)
        if form.is_valid():
            service = form.save()
            messages.success(request, f"{service.user.username} の {service.display_name} を更新しました。")
            return redirect("staff_user_services", user_id=service.user_id)
    else:
        form = StaffServiceForm(instance=service, fixed_user=service.user, registered_by=request.user)
    return render(
        request,
        "receipts/staff_service_form.html",
        {
            "title": "利用サービス編集",
            "form": form,
            "service": service,
            "managed_user": service.user,
            "back_url": reverse("staff_user_services", kwargs={"user_id": service.user_id}),
            "submit_label": "保存する",
        },
    )


@staff_member_required
def staff_service_archive(request, pk: int):
    service = get_object_or_404(RegisteredService.objects.select_related("user"), pk=pk, user__is_staff=False, user__is_superuser=False)
    if request.method != "POST":
        raise Http404
    service.deactivate(by=request.user, source=ServiceDeactivationSource.ADMIN)
    messages.success(request, f"{service.user.username} の {service.display_name} を利用停止にしました。過去の提出履歴は残ります。")
    return redirect_back_or(request, f"{reverse('staff_services')}?user={service.user_id}")


@staff_member_required
def staff_service_activate(request, pk: int):
    service = get_object_or_404(RegisteredService.objects.select_related("user"), pk=pk, user__is_staff=False, user__is_superuser=False)
    if request.method != "POST":
        raise Http404
    service.activate()
    messages.success(request, f"{service.user.username} の {service.display_name} を利用中に戻しました。")
    return redirect_back_or(request, f"{reverse('staff_services')}?user={service.user_id}")


@staff_member_required
def staff_catalog_create(request):
    if request.method == "POST":
        form = ServiceCatalogForm(request.POST)
        if form.is_valid():
            catalog = form.save(commit=False)
            catalog.created_by = request.user
            catalog.save()
            messages.success(request, f"サービスマスター {catalog.display_name} を登録しました。")
            return redirect("staff_services")
    else:
        form = ServiceCatalogForm()
    return render(
        request,
        "receipts/staff_catalog_form.html",
        {
            "title": "サービスマスター登録",
            "form": form,
            "back_url": reverse("staff_services"),
            "submit_label": "登録する",
        },
    )


@staff_member_required
def staff_catalog_update(request, pk: int):
    catalog = get_object_or_404(ServiceCatalog, pk=pk)
    if request.method == "POST":
        form = ServiceCatalogForm(request.POST, instance=catalog)
        if form.is_valid():
            catalog = form.save()
            # マスター名や支払い種別を変更した場合、未提出のユーザー別登録にも同期する。
            RegisteredService.objects.filter(catalog_service=catalog).update(name=catalog.name, billing_type=catalog.billing_type)
            messages.success(request, f"サービスマスター {catalog.display_name} を更新しました。")
            return redirect("staff_services")
    else:
        form = ServiceCatalogForm(instance=catalog)
    return render(
        request,
        "receipts/staff_catalog_form.html",
        {
            "title": "サービスマスター編集",
            "form": form,
            "catalog": catalog,
            "back_url": reverse("staff_services"),
            "submit_label": "保存する",
        },
    )


@staff_member_required
def staff_catalog_archive(request, pk: int):
    catalog = get_object_or_404(ServiceCatalog, pk=pk)
    if request.method != "POST":
        raise Http404
    catalog.is_active = False
    catalog.save(update_fields=["is_active", "updated_at"])
    messages.success(request, f"サービスマスター {catalog.display_name} を新規選択不可にしました。既存のユーザー別利用登録は維持されます。")
    return redirect_back_or(request, reverse("staff_services"))


@staff_member_required
def staff_catalog_activate(request, pk: int):
    catalog = get_object_or_404(ServiceCatalog, pk=pk)
    if request.method != "POST":
        raise Http404
    catalog.is_active = True
    catalog.save(update_fields=["is_active", "updated_at"])
    messages.success(request, f"サービスマスター {catalog.display_name} を選択可能に戻しました。")
    return redirect_back_or(request, reverse("staff_services"))


# 旧URL互換: 一般ユーザーには表示せず、管理者専用のサービス管理へ移行する。
service_create = staff_service_create
service_update = staff_service_update
service_archive = staff_service_archive


def redirect_back_or(request, fallback_url: str):
    next_url = request.POST.get("next") or request.GET.get("next")
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect(fallback_url)


@login_required
def replace_receipt_file(request, pk: int):
    receipt = get_object_or_404(Receipt.objects.select_related("submission", "submission__user"), pk=pk)
    if receipt.submission.user != request.user:
        raise PermissionDenied
    if request.method != "POST":
        raise Http404

    fallback_url = (
        receipt.submission.get_absolute_url()
        if receipt.submission.is_submitted
        else f"{reverse('dashboard')}?month={month_query(receipt.submission.period_month)}"
    )
    form = ReceiptFileReplaceForm(request.POST, request.FILES)
    if not form.is_valid():
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, error)
        return redirect_back_or(request, fallback_url)

    upload = form.cleaned_data["file"]
    old_storage = receipt.file.storage if receipt.file else None
    old_file_name = receipt.file.name if receipt.file else ""
    restore_fields = [
        "file",
        "original_filename",
        "generated_filename",
        "file_size",
        "content_type",
        "expires_at",
        "file_deleted_at",
        "file_delete_reason",
        "ai_filename_status",
        "ai_filename_admin_memo",
        "ai_filename_checked_at",
        "ai_extracted_payee",
        "ai_extracted_card_last4",
        "ai_receipt_month",
        "ai_period_check_status",
        "ai_period_check_memo",
        "amount",
        "currency",
        "issued_on",
    ]
    old_values = {field: (receipt.file.name if field == "file" and receipt.file else getattr(receipt, field)) for field in restore_fields}

    receipt.file = upload
    receipt.original_filename = upload.name
    receipt.generated_filename = ""
    receipt.file_size = upload.size
    receipt.content_type = getattr(upload, "content_type", "") or ""
    receipt.expires_at = receipt_expiry_from(timezone.now())
    receipt.file_deleted_at = None
    receipt.file_delete_reason = ""
    receipt.save(
        update_fields=[
            "file",
            "original_filename",
            "generated_filename",
            "file_size",
            "content_type",
            "expires_at",
            "file_deleted_at",
            "file_delete_reason",
            "updated_at",
        ]
    )

    apply_ai_filename_to_receipt(receipt)
    receipt.refresh_from_db()
    if receipt.needs_period_reupload:
        rejected_storage = receipt.file.storage if receipt.file else None
        rejected_file_name = receipt.file.name if receipt.file else ""
        error_message = receipt_period_mismatch_message(receipt)
        for field, value in old_values.items():
            setattr(receipt, field, value)
        receipt.save(update_fields=[*restore_fields, "updated_at"])
        if rejected_storage is not None and rejected_file_name and rejected_file_name != old_file_name:
            try:
                if rejected_storage.exists(rejected_file_name):
                    rejected_storage.delete(rejected_file_name)
            except Exception:
                pass
        messages.error(request, error_message)
        return redirect_back_or(request, fallback_url)

    if old_storage is not None and old_file_name and old_file_name != receipt.file.name:
        try:
            if old_storage.exists(old_file_name):
                old_storage.delete(old_file_name)
        except Exception:
            # ストレージ削除に失敗しても、ユーザーの差し替え処理自体は完了させる。
            pass

    messages.success(request, f"{receipt.service_display_name_snapshot} の領収書ファイルを修正しました。")
    return redirect_back_or(request, fallback_url)


@login_required
def delete_receipt(request, pk: int):
    receipt = get_object_or_404(Receipt.objects.select_related("submission", "submission__user"), pk=pk)
    if receipt.submission.user != request.user:
        raise PermissionDenied
    if request.method != "POST":
        raise Http404
    if receipt.submission.is_submitted:
        messages.error(request, "提出済みの領収書は削除できません。")
        return redirect(receipt.submission.get_absolute_url())
    selected_month = receipt.submission.period_month
    service_name = receipt.service_display_name_snapshot
    receipt.delete()
    messages.success(request, f"{service_name} の領収書を削除しました。")
    return redirect(f"{reverse('dashboard')}?month={month_query(selected_month)}")


@staff_member_required
def staff_delete_receipt(request, pk: int):
    receipt = get_object_or_404(
        Receipt.objects.select_related("submission", "submission__user"),
        pk=pk,
        submission__user__is_staff=False,
        submission__user__is_superuser=False,
    )
    if request.method != "POST":
        raise Http404

    submission = receipt.submission
    selected_month = submission.period_month
    username = submission.user.get_username()
    service_name = receipt.service_display_name_snapshot
    receipt.delete()
    fallback_url = f"{reverse('history')}?month={month_query(selected_month)}"
    if not submission.receipts.exists():
        submission.delete()
        messages.success(request, f"{username} / {service_name} の領収書を削除しました。空になった提出履歴も削除しました。")
        return redirect(fallback_url)

    messages.success(request, f"{username} / {service_name} の領収書を削除しました。")
    return redirect_back_or(request, fallback_url)


def staff_history(request):
    selected_month, month_form = parse_month_from_request(request)
    users = list(
        managed_users_queryset().annotate(
            active_service_count=Count(
                "registered_services",
                filter=Q(registered_services__is_active=True),
                distinct=True,
            ),
            user_registered_count=Count(
                "registered_services",
                filter=Q(registered_services__registration_source=ServiceRegistrationSource.USER),
                distinct=True,
            ),
            user_stopped_count=Count(
                "registered_services",
                filter=Q(registered_services__deactivation_source=ServiceDeactivationSource.USER),
                distinct=True,
            ),
        )
    )
    user_ids = [user.pk for user in users]
    receipt_prefetch = Prefetch("receipts", queryset=Receipt.objects.select_related("service"))
    submissions = (
        Submission.objects.filter(period_month=selected_month, user_id__in=user_ids)
        .select_related("user")
        .prefetch_related(receipt_prefetch)
        .annotate(receipts_count=Count("receipts"))
    )
    submissions_by_user = {submission.user_id: submission for submission in submissions}
    rows = []
    for user in users:
        submission = submissions_by_user.get(user.id)
        if submission is None:
            status = "未着手"
            receipt_count = 0
            available_file_count = 0
            purged_file_count = 0
        elif submission.status == SubmissionStatus.SUBMITTED:
            status = "提出済み"
            receipt_count = submission.receipt_count
            available_file_count = sum(1 for receipt in submission.receipts.all() if receipt.file_available)
            purged_file_count = sum(1 for receipt in submission.receipts.all() if receipt.file_deleted_at)
        else:
            status = "下書き"
            receipt_count = submission.receipt_count
            available_file_count = sum(1 for receipt in submission.receipts.all() if receipt.file_available)
            purged_file_count = sum(1 for receipt in submission.receipts.all() if receipt.file_deleted_at)
        rows.append(
            {
                "user": user,
                "submission": submission,
                "status": status,
                "receipt_count": receipt_count,
                "available_file_count": available_file_count,
                "purged_file_count": purged_file_count,
                "active_service_count": user.active_service_count,
                "user_registered_count": user.user_registered_count,
                "user_stopped_count": user.user_stopped_count,
            }
        )

    receipts = (
        Receipt.objects.filter(submission__period_month=selected_month, submission__user_id__in=user_ids)
        .select_related("submission", "submission__user", "service")
        .order_by("submission__user__username", "service_name_snapshot", "uploaded_at")
    )
    ai_review_count = receipts.filter(ai_filename_status__in=[ReceiptFilenameStatus.NEEDS_REVIEW, ReceiptFilenameStatus.FAILED]).count()
    period_mismatch_count = receipts.filter(ai_period_check_status=ReceiptPeriodCheckStatus.MISMATCHED).count()
    stats = {
        "total_users": len(users),
        "submitted": sum(1 for row in rows if row["status"] == "提出済み"),
        "draft": sum(1 for row in rows if row["status"] == "下書き"),
        "not_started": sum(1 for row in rows if row["status"] == "未着手"),
        "receipt_count": sum(row["receipt_count"] for row in rows),
        "available_file_count": sum(row["available_file_count"] for row in rows),
        "active_service_count": sum(row["active_service_count"] for row in rows),
        "user_registered_count": sum(row["user_registered_count"] for row in rows),
        "user_stopped_count": sum(row["user_stopped_count"] for row in rows),
        "ai_review_count": ai_review_count,
        "period_mismatch_count": period_mismatch_count,
    }
    return render(
        request,
        "receipts/staff_history.html",
        {
            "rows": rows,
            "stats": stats,
            "month_form": month_form,
            "selected_month": selected_month,
            "receipts": receipts,
        },
    )


@login_required
def history(request):
    if request.user.is_staff:
        return staff_history(request)
    submissions = (
        Submission.objects.filter(user=request.user)
        .prefetch_related("receipts")
        .order_by("-period_month", "-created_at")
    )
    return render(request, "receipts/history.html", {"submissions": submissions})


@login_required
def submission_detail(request, pk: int):
    submission = get_object_or_404(
        Submission.objects.select_related("user").prefetch_related("receipts__service"),
        pk=pk,
    )
    if submission.user != request.user and not request.user.is_staff:
        raise PermissionDenied
    template = "receipts/staff_submission_detail.html" if request.user.is_staff and submission.user != request.user else "receipts/submission_detail.html"
    return render(request, template, {"submission": submission})


@login_required
def download_receipt(request, pk: int):
    receipt = get_object_or_404(Receipt.objects.select_related("submission", "submission__user"), pk=pk)
    if receipt.submission.user != request.user and not request.user.is_staff:
        raise PermissionDenied
    if not receipt.file_available:
        raise Http404("保存期限が過ぎたか、ファイルが削除済みです。")
    filename = receipt.display_filename or Path(receipt.file.name).name
    return FileResponse(receipt.file.open("rb"), as_attachment=True, filename=filename)


@staff_member_required
def staff_user_create(request):
    generated_user = None
    generated_password = None
    if request.method == "POST":
        form = StaffUserCreateForm(request.POST)
        if form.is_valid():
            generated_user, generated_password = form.save(created_by=request.user)
            messages.success(request, f"{generated_user.username} のアカウントを作成しました。初期パスワードを対象ユーザーへ安全に伝えてください。")
            form = StaffUserCreateForm()
    else:
        form = StaffUserCreateForm()

    recent_users = (
        User.objects.filter(is_staff=False, is_superuser=False)
        .select_related("profile")
        .order_by("-date_joined")[:10]
    )
    return render(
        request,
        "receipts/staff_user_create.html",
        {
            "form": form,
            "generated_user": generated_user,
            "generated_password": generated_password,
            "recent_users": recent_users,
        },
    )


@staff_member_required
def staff_dashboard(request):
    query_string = request.META.get("QUERY_STRING", "")
    url = reverse("history")
    if query_string:
        url = f"{url}?{query_string}"
    return redirect(url)


@staff_member_required
def staff_submission_detail(request, pk: int):
    submission = get_object_or_404(
        Submission.objects.select_related("user").prefetch_related("receipts__service"),
        pk=pk,
    )
    return render(request, "receipts/staff_submission_detail.html", {"submission": submission})


def receipt_manifest_csv(submissions) -> str:
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "period_month",
        "username",
        "email",
        "submission_status",
        "submitted_at",
        "service_name",
        "billing_type",
        "amount",
        "currency",
        "issued_on",
        "uploaded_at",
        "expires_at",
        "file_status",
        "display_filename",
        "original_filename",
        "ai_filename_status",
        "ai_filename_admin_memo",
        "ai_extracted_payee",
        "ai_extracted_card_last4",
        "ai_receipt_month",
        "ai_period_check_status",
        "ai_period_check_memo",
        "file_size",
        "memo",
    ])
    for submission in submissions:
        for receipt in submission.receipts.all():
            writer.writerow([
                submission.period_month.strftime("%Y-%m"),
                submission.user.get_username(),
                submission.user.email,
                submission.get_status_display(),
                submission.submitted_at.isoformat() if submission.submitted_at else "",
                receipt.service_name_snapshot,
                receipt.get_billing_type_snapshot_display(),
                receipt.amount if receipt.amount is not None else "",
                receipt.currency,
                receipt.issued_on.isoformat() if receipt.issued_on else "",
                receipt.uploaded_at.isoformat() if receipt.uploaded_at else "",
                receipt.expires_at.isoformat() if receipt.expires_at else "",
                receipt.file_status_label,
                receipt.display_filename,
                receipt.original_filename,
                receipt.get_ai_filename_status_display(),
                receipt.ai_filename_admin_memo,
                receipt.ai_extracted_payee,
                receipt.ai_extracted_card_last4,
                receipt.ai_receipt_month,
                receipt.get_ai_period_check_status_display(),
                receipt.ai_period_check_memo,
                receipt.file_size or "",
                receipt.memo,
            ])
    return output.getvalue()


def build_receipts_zip(submissions, zip_label: str) -> HttpResponse:
    submissions = list(submissions)
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.csv", receipt_manifest_csv(submissions))
        has_files = False
        for submission in submissions:
            period = submission.period_month.strftime("%Y-%m")
            user_part = safe_part(submission.user.get_username(), f"user-{submission.user_id}")
            status_part = safe_part(submission.get_status_display(), submission.status)
            for receipt in submission.receipts.all():
                if not receipt.file_available:
                    continue
                display_name = receipt.display_filename or Path(receipt.file.name).name
                display_stem = safe_part(Path(display_name).stem, f"receipt-{receipt.id}")
                display_suffix = Path(display_name).suffix.lower() or Path(receipt.file.name).suffix.lower()
                arcname = f"{period}/{user_part}/{status_part}/{receipt.id}_{display_stem}{display_suffix}"
                with receipt.file.open("rb") as file_obj:
                    archive.writestr(arcname, file_obj.read())
                    has_files = True
        if not has_files:
            archive.writestr("README.txt", "ダウンロード対象の保存中領収書ファイルはありません。manifest.csv を確認してください。")

    buffer.seek(0)
    response = HttpResponse(buffer.getvalue(), content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="{zip_label}.zip"'
    return response


@staff_member_required
def staff_download_month(request):
    selected_month, _ = parse_month_from_request(request)
    include_drafts = request.GET.get("include_drafts") == "1"
    queryset = (
        Submission.objects.filter(period_month=selected_month)
        .select_related("user")
        .prefetch_related(Prefetch("receipts", queryset=Receipt.objects.select_related("service")))
    )
    if not include_drafts:
        queryset = queryset.filter(status=SubmissionStatus.SUBMITTED)
    label_suffix = "all" if include_drafts else "submitted"
    return build_receipts_zip(queryset, f"receipts_{selected_month:%Y-%m}_{label_suffix}")


@staff_member_required
def staff_download_submission(request, pk: int):
    submission = get_object_or_404(
        Submission.objects.select_related("user").prefetch_related(Prefetch("receipts", queryset=Receipt.objects.select_related("service"))),
        pk=pk,
    )
    return build_receipts_zip([submission], f"receipts_{submission.period_month:%Y-%m}_{safe_part(submission.user.get_username())}")
