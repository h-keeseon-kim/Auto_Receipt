from __future__ import annotations

import csv
import zipfile
from datetime import timedelta
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
from django.db import transaction
from django.db.models import Count, Prefetch, Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.utils.http import url_has_allowed_host_and_scheme
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from .ai_processing import claim_pending_receipts_for_ai_processing, reset_ai_processing_state, start_background_ai_processing
from .ai_filename import target_card_last4
from .emailing import send_test_email
from .forms import (
    MonthSelectForm,
    CardStatementUploadForm,
    ReceiptFileReplaceForm,
    ReceiptUploadForm,
    RegisterForm,
    ServiceCatalogForm,
    StaffServiceForm,
    StaffUserCreateForm,
    StaffUserPasswordResetForm,
    StaffUserRoleForm,
    StaffUserStatusForm,
    EmailReminderScheduleForm,
    StaffEmailTestForm,
    StyledPasswordChangeForm,
    UserServiceRegistrationForm,
    UserServiceStopForm,
    current_month,
)
from .models import (
    BillingType,
    CardStatement,
    CardStatementItem,
    CardStatementStatus,
    MonthlyServiceDeclaration,
    Receipt,
    EmailDeliveryLog,
    EmailReminderSchedule,
    EmailDeliveryStatus,
    ReceiptFilenameStatus,
    ReceiptPeriodCheckStatus,
    ReceiptResubmissionRequest,
    RegisteredService,
    ResubmissionRequestStatus,
    ServiceCatalog,
    ServiceDeactivationSource,
    ServiceRegistrationSource,
    StatementMatchStatus,
    Submission,
    UserAccountStatus,
    SubmissionStatus,
    UserProfile,
    receipt_expiry_from,
)
from .monthly_status import build_user_month_summary
from .statement_processing import start_background_statement_processing


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


@login_required
@require_POST
def tutorial_complete(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    profile.mark_tutorial_completed()
    return JsonResponse({"ok": True, "tutorial_completed": True})


def resolve_matching_resubmission_requests(receipt: Receipt, *, by) -> int:
    """再提出依頼と同じ月・サービスの領収書が再アップロードされたら対応済みにする。"""

    now = timezone.now()
    return (
        ReceiptResubmissionRequest.objects.filter(
            user=receipt.submission.user,
            period_month=receipt.submission.period_month,
            service_name_snapshot=receipt.service_name_snapshot,
            billing_type_snapshot=receipt.billing_type_snapshot,
            status=ResubmissionRequestStatus.OPEN,
        )
        .update(status=ResubmissionRequestStatus.RESOLVED, resolved_at=now, resolved_by=by)
    )


def managed_users_queryset():
    return User.objects.filter(is_active=True, is_staff=False, is_superuser=False).select_related("profile").order_by("username")


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
    uploadable_services = RegisteredService.objects.uploadable_for(request.user, selected_month).select_related("catalog_service")
    open_resubmission_requests = ReceiptResubmissionRequest.objects.filter(
        user=request.user,
        period_month=selected_month,
        status=ResubmissionRequestStatus.OPEN,
    ).order_by("service_name_snapshot", "created_at")

    receipt_form = ReceiptUploadForm(user=request.user, period_month=selected_month)
    if request.method == "POST":
        action = request.POST.get("action")

        if action in {"declare_no_usage", "clear_no_usage"}:
            if submission.is_submitted:
                messages.error(request, "提出済みの月はAPI利用状況を変更できません。再提出が必要な場合は管理者へ連絡してください。")
                return redirect(submission.get_absolute_url())
            service = get_object_or_404(
                RegisteredService,
                pk=request.POST.get("service_id"),
                user=request.user,
                billing_type=BillingType.METERED,
            )
            if not service.is_uploadable_for(selected_month):
                messages.error(request, "このサービスは対象月の提出対象ではありません。")
            elif action == "declare_no_usage":
                if Receipt.objects.filter(submission=submission, service=service).exists():
                    messages.error(request, f"{service.display_name} には領収書が登録済みのため『当月利用なし』にできません。")
                else:
                    MonthlyServiceDeclaration.objects.update_or_create(
                        user=request.user,
                        service=service,
                        period_month=selected_month,
                        defaults={"no_usage": True, "declared_by": request.user},
                    )
                    messages.success(request, f"{service.display_name} を {selected_month:%Y年%m月} は利用なしとして記録しました。")
            else:
                deleted, _ = MonthlyServiceDeclaration.objects.filter(
                    user=request.user,
                    service=service,
                    period_month=selected_month,
                ).delete()
                if deleted:
                    messages.success(request, f"{service.display_name} の『当月利用なし』を取り消しました。")
            return redirect(f"{reverse('dashboard')}?month={month_query(selected_month)}")

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
                    MonthlyServiceDeclaration.objects.filter(
                        user=request.user,
                        service=receipt.service,
                        period_month=selected_month,
                    ).delete()
                    resolved_count = resolve_matching_resubmission_requests(receipt, by=request.user)
                    if resolved_count:
                        messages.success(request, f"{receipt.service_display_name_snapshot} の再提出依頼を対応済みにしました。")
                    messages.success(
                        request,
                        f"{receipt.service_display_name_snapshot} の領収書を追加しました。AIによるファイル名修正・検査は、管理者が実行した後に管理者画面へ反映されます。",
                    )
                    return redirect(f"{reverse('dashboard')}?month={month_query(selected_month)}")
        elif action == "submit":
            try:
                submission.submit()
                messages.success(request, f"{selected_month:%Y年%m月}分を提出しました。")
                return redirect(submission.get_absolute_url())
            except ValidationError as exc:
                messages.error(request, exc.message if hasattr(exc, "message") else exc.messages[0])
        else:
            messages.error(request, "不明な操作です。")

    receipts = submission.receipts.select_related("service").all()
    monthly_summary = build_user_month_summary(request.user, selected_month)
    return render(
        request,
        "receipts/dashboard.html",
        {
            "month_form": month_form,
            "receipt_form": receipt_form,
            "submission": submission,
            "receipts": receipts,
            "uploadable_services": uploadable_services,
            "monthly_summary": monthly_summary,
            "open_resubmission_requests": open_resubmission_requests,
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

    receipt.file = upload
    receipt.original_filename = upload.name
    receipt.file_size = upload.size
    receipt.content_type = getattr(upload, "content_type", "") or ""
    receipt.expires_at = receipt_expiry_from(timezone.now())
    receipt.file_deleted_at = None
    receipt.file_delete_reason = ""
    ai_reset_fields = reset_ai_processing_state(receipt, save=False, clear_extracted_values=True)
    receipt.save(
        update_fields=[
            "file",
            "original_filename",
            "file_size",
            "content_type",
            "expires_at",
            "file_deleted_at",
            "file_delete_reason",
            *ai_reset_fields,
            "updated_at",
        ]
    )

    if old_storage is not None and old_file_name and old_file_name != receipt.file.name:
        try:
            if old_storage.exists(old_file_name):
                old_storage.delete(old_file_name)
        except Exception:
            # ストレージ削除に失敗しても、ユーザーの差し替え処理自体は完了させる。
            pass

    MonthlyServiceDeclaration.objects.filter(
        user=receipt.submission.user,
        service=receipt.service,
        period_month=receipt.submission.period_month,
    ).delete()
    messages.success(request, f"{receipt.service_display_name_snapshot} の領収書ファイルを修正しました。AIによるファイル名修正・検査は、管理者が再実行した後に反映されます。")
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


@staff_member_required
def staff_request_receipt_resubmission(request, pk: int):
    receipt = get_object_or_404(
        Receipt.objects.select_related("submission", "submission__user"),
        pk=pk,
        submission__user__is_staff=False,
        submission__user__is_superuser=False,
    )
    if request.method != "POST":
        raise Http404

    fallback_url = f"{reverse('history')}?month={month_query(receipt.submission.period_month)}"
    with transaction.atomic():
        submission = receipt.submission
        selected_month = submission.period_month
        username = submission.user.get_username()
        service_name = receipt.service_display_name_snapshot
        display_filename = receipt.display_filename
        message = (
            f"管理者確認の結果、{selected_month:%Y年%m月}分の {service_name} の領収書について再提出が必要になりました。"
            "該当ファイルは提出項目から削除済みです。正しい領収書ファイルを再度アップロードして、提出してください。"
        )
        ReceiptResubmissionRequest.objects.create(
            user=submission.user,
            period_month=selected_month,
            service_name_snapshot=receipt.service_name_snapshot,
            billing_type_snapshot=receipt.billing_type_snapshot,
            original_receipt_id=receipt.pk,
            original_filename=receipt.original_filename,
            display_filename=display_filename,
            message=message,
            created_by=request.user,
        )
        receipt.delete()
        if submission.status == SubmissionStatus.SUBMITTED:
            submission.status = SubmissionStatus.DRAFT
            submission.submitted_at = None
            submission.save(update_fields=["status", "submitted_at", "updated_at"])

    messages.success(
        request,
        f"{username} / {service_name} に再提出を指示しました。対象領収書は提出項目から削除され、ユーザーは該当月で再アップロードできます。",
    )
    return redirect_back_or(request, fallback_url)



def parse_month_value(value):
    form = MonthSelectForm({"month": value})
    if form.is_valid():
        return form.cleaned_data["month"]
    raise Http404("月の指定が不正です。")


def staff_month_receipts_queryset(selected_month):
    user_ids = managed_users_queryset().values_list("id", flat=True)
    return (
        Receipt.objects.filter(submission__period_month=selected_month, submission__user_id__in=user_ids)
        .select_related("submission", "submission__user", "service")
    )


def staff_ai_summary_for_month(selected_month) -> dict:
    receipts = staff_month_receipts_queryset(selected_month)
    available = receipts.available_files()
    manual_review_filter = (
        Q(ai_check_card_last4=False)
        | Q(ai_check_payee=False)
        | Q(ai_check_service_payee_related=False)
        | Q(ai_check_date=False)
        | Q(ai_check_amount=False)
        | Q(ai_check_currency=False)
        | Q(ai_check_period_match=False)
    )
    return {
        "ai_ready_count": available.filter(ai_filename_status=ReceiptFilenameStatus.NOT_PROCESSED).count(),
        "ai_pending_count": receipts.filter(ai_filename_status=ReceiptFilenameStatus.NOT_PROCESSED).count(),
        "ai_processing_count": available.filter(
            ai_filename_status__in=[ReceiptFilenameStatus.QUEUED, ReceiptFilenameStatus.PROCESSING]
        ).count(),
        "ai_queued_count": available.filter(ai_filename_status=ReceiptFilenameStatus.QUEUED).count(),
        "ai_review_count": receipts.filter(ai_filename_status__in=[ReceiptFilenameStatus.NEEDS_REVIEW, ReceiptFilenameStatus.FAILED]).count(),
        "period_mismatch_count": receipts.filter(ai_period_check_status=ReceiptPeriodCheckStatus.MISMATCHED).count(),
        "manual_review_count": available.filter(ai_filename_checked_at__isnull=False).filter(manual_review_filter).count(),
        "service_payee_review_count": available.filter(ai_filename_checked_at__isnull=False, ai_check_service_payee_related=False).count(),
    }



@staff_member_required
@require_POST
def staff_start_ai_processing(request):
    selected_month = parse_month_value(request.POST.get("month"))
    limit = int(request.POST.get("limit") or getattr(settings, "RECEIPT_AI_MANUAL_BATCH_SIZE", 100))
    limit = min(max(limit, 1), 500)
    base_queryset = staff_month_receipts_queryset(selected_month).available_files()
    claimed_ids = claim_pending_receipts_for_ai_processing(base_queryset, limit=limit)
    if claimed_ids:
        start_background_ai_processing(claimed_ids)

    if claimed_ids:
        message = f"AIで情報を抽出中です。{len(claimed_ids)}件を処理開始しました。完了したものから一覧に反映されます。"
    else:
        message = "AI未確認の領収書はありません。AI確認済み・要確認・失敗・スキップ済みの項目は再検査しません。"

    payload = {
        "ok": True,
        "started_count": len(claimed_ids),
        "message": message,
        "stats": staff_ai_summary_for_month(selected_month),
    }
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse(payload)

    if claimed_ids:
        messages.success(request, message)
    else:
        messages.info(request, message)
    return redirect(f"{reverse('history')}?month={month_query(selected_month)}")


@staff_member_required
def staff_ai_processing_status(request):
    selected_month = parse_month_value(request.GET.get("month"))
    receipts = staff_month_receipts_queryset(selected_month).order_by("-uploaded_at", "-pk")
    stats = staff_ai_summary_for_month(selected_month)
    receipts_html = render_to_string(
        "receipts/_staff_receipt_rows.html",
        {"receipts": receipts},
        request=request,
    )
    return JsonResponse(
        {
            "ok": True,
            "receipts_html": receipts_html,
            "stats": stats,
            "processing_count": stats["ai_processing_count"],
            "processable_count": stats["ai_ready_count"],
            "done": stats["ai_processing_count"] == 0,
        }
    )



@staff_member_required
def staff_user_month_status(request, user_id: int):
    managed_user = get_managed_user(user_id)
    selected_month, month_form = parse_month_from_request(request)
    monthly_summary = build_user_month_summary(managed_user, selected_month)
    available_services = RegisteredService.objects.uploadable_for(managed_user, selected_month).select_related("catalog_service")
    submission = (
        Submission.objects.filter(user=managed_user, period_month=selected_month)
        .prefetch_related(Prefetch("receipts", queryset=Receipt.objects.select_related("service").order_by("uploaded_at", "pk")))
        .first()
    )
    statements = (
        CardStatement.objects.filter(user=managed_user, period_month=selected_month)
        .select_related("uploaded_by")
        .prefetch_related(
            Prefetch(
                "items",
                queryset=CardStatementItem.objects.select_related("matched_service", "matched_receipt").order_by("sequence", "pk"),
            )
        )
        .order_by("-uploaded_at")
    )
    return render(
        request,
        "receipts/staff_user_month_status.html",
        {
            "managed_user": managed_user,
            "selected_month": selected_month,
            "month_form": month_form,
            "monthly_summary": monthly_summary,
            "available_services": available_services,
            "submission": submission,
            "statements": statements,
            "statement_form": CardStatementUploadForm(),
            "target_card_last4": target_card_last4(),
        },
    )


@staff_member_required
@require_POST
def staff_upload_card_statement(request, user_id: int):
    managed_user = get_managed_user(user_id)
    selected_month = parse_month_value(request.POST.get("month"))
    form = CardStatementUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, error)
        return redirect(f"{reverse('staff_user_month_status', args=[managed_user.pk])}?month={month_query(selected_month)}")

    upload = form.cleaned_data["file"]
    statement = form.save(commit=False)
    statement.user = managed_user
    statement.period_month = selected_month
    statement.original_filename = upload.name
    statement.file_size = upload.size
    statement.content_type = getattr(upload, "content_type", "") or ""
    statement.status = CardStatementStatus.PROCESSING
    statement.uploaded_by = request.user
    statement.expires_at = receipt_expiry_from(timezone.now())
    try:
        statement.full_clean()
        statement.save()
    except ValidationError as exc:
        messages.error(request, exc.messages[0] if exc.messages else str(exc))
    else:
        start_background_statement_processing(statement.pk)
        messages.success(request, "ご利用代金明細書をアップロードしました。AIで明細項目を解析中です。完了後、未提出の領収書をハイライトします。")
    return redirect(f"{reverse('staff_user_month_status', args=[managed_user.pk])}?month={month_query(selected_month)}")


@staff_member_required
def staff_card_statement_status(request, user_id: int):
    managed_user = get_managed_user(user_id)
    selected_month = parse_month_value(request.GET.get("month"))
    available_services = RegisteredService.objects.uploadable_for(managed_user, selected_month).select_related("catalog_service")
    statements = (
        CardStatement.objects.filter(user=managed_user, period_month=selected_month)
        .select_related("uploaded_by")
        .prefetch_related(
            Prefetch(
                "items",
                queryset=CardStatementItem.objects.select_related("matched_service", "matched_receipt").order_by("sequence", "pk"),
            )
        )
        .order_by("-uploaded_at")
    )
    html = render_to_string(
        "receipts/_staff_card_statements.html",
        {
            "managed_user": managed_user,
            "selected_month": selected_month,
            "statements": statements,
            "available_services": available_services,
            "target_card_last4": target_card_last4(),
        },
        request=request,
    )
    processing_count = statements.filter(status=CardStatementStatus.PROCESSING).count()
    return JsonResponse({"ok": True, "html": html, "processing_count": processing_count, "done": processing_count == 0})


@staff_member_required
def staff_download_card_statement(request, pk: int):
    statement = get_object_or_404(CardStatement.objects.select_related("user"), pk=pk, user__is_staff=False, user__is_superuser=False)
    if not statement.file_available:
        raise Http404("保存期限が過ぎたか、明細ファイルが削除済みです。")
    filename = statement.original_filename or Path(statement.file.name).name
    return FileResponse(statement.file.open("rb"), as_attachment=True, filename=filename)


@staff_member_required
@require_POST
def staff_delete_card_statement(request, pk: int):
    statement = get_object_or_404(CardStatement.objects.select_related("user"), pk=pk, user__is_staff=False, user__is_superuser=False)
    user_id = statement.user_id
    selected_month = statement.period_month
    filename = statement.original_filename or "ご利用代金明細書"
    statement.delete()
    messages.success(request, f"{filename} と解析履歴を削除しました。")
    return redirect(f"{reverse('staff_user_month_status', args=[user_id])}?month={month_query(selected_month)}")


def refresh_card_statement_review_status(statement: CardStatement):
    target_last4 = str(getattr(settings, "RECEIPT_CARD_LAST4", "7210"))[-4:]
    target_month = statement.period_month.strftime("%Y-%m")
    unresolved = statement.items.filter(
        receipt_required=True,
        match_status__in=[StatementMatchStatus.AMBIGUOUS, StatementMatchStatus.UNMATCHED],
    ).exists()
    statement.status = (
        CardStatementStatus.NEEDS_REVIEW
        if unresolved or statement.card_last4 != target_last4 or statement.statement_period != target_month
        else CardStatementStatus.COMPLETED
    )
    statement.save(update_fields=["status", "updated_at"])


@staff_member_required
@require_POST
def staff_update_statement_item(request, pk: int):
    item = get_object_or_404(
        CardStatementItem.objects.select_related("statement", "statement__user"),
        pk=pk,
        statement__user__is_staff=False,
        statement__user__is_superuser=False,
    )
    action = request.POST.get("item_action") or "match"
    if action == "ignore":
        item.matched_service = None
        item.matched_receipt = None
        item.match_status = StatementMatchStatus.IGNORED
        item.receipt_required = False
        item.match_confidence = 1.0
        item.match_memo = "管理者確認により領収書管理対象外としました。"
    else:
        service_id = request.POST.get("service_id")
        if not service_id:
            messages.error(request, "対応サービスを選択してください。")
            return redirect(
                f"{reverse('staff_user_month_status', args=[item.statement.user_id])}?month={month_query(item.statement.period_month)}#statement-{item.statement_id}"
            )
        service = get_object_or_404(
            RegisteredService,
            pk=service_id,
            user=item.statement.user,
        )
        item.matched_service = service
        used_receipt_ids = CardStatementItem.objects.filter(
            statement=item.statement,
            matched_receipt__isnull=False,
        ).exclude(pk=item.pk).values_list("matched_receipt_id", flat=True)
        item.matched_receipt = (
            Receipt.objects.available_files()
            .filter(
                submission__user=item.statement.user,
                submission__period_month=item.statement.period_month,
                service=service,
            )
            .exclude(pk__in=used_receipt_ids)
            .order_by("uploaded_at", "pk")
            .first()
        )
        item.match_status = StatementMatchStatus.MATCHED
        item.receipt_required = request.POST.get("receipt_required") == "on"
        item.match_confidence = 1.0
        item.match_memo = "管理者が対応サービスを確認しました。"
    item.save(
        update_fields=[
            "matched_service",
            "matched_receipt",
            "match_status",
            "receipt_required",
            "match_confidence",
            "match_memo",
        ]
    )
    refresh_card_statement_review_status(item.statement)
    messages.success(request, f"明細 {item.line_reference or item.pk} の対応を更新しました。")
    return redirect(
        f"{reverse('staff_user_month_status', args=[item.statement.user_id])}?month={month_query(item.statement.period_month)}#statement-{item.statement_id}"
    )

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
    receipt_prefetch = Prefetch("receipts", queryset=Receipt.objects.select_related("service").order_by("uploaded_at", "pk"))
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
        summary = build_user_month_summary(user, selected_month)
        submission_receipts = list(submission.receipts.all()) if submission is not None else []
        if submission is None:
            status = "未着手"
        elif submission.status == SubmissionStatus.SUBMITTED:
            status = "提出済み"
        else:
            status = "下書き"
        receipt_count = len(submission_receipts)
        available_file_count = sum(1 for receipt in submission_receipts if receipt.file_available)
        purged_file_count = sum(1 for receipt in submission_receipts if receipt.file_deleted_at)
        manual_review_count = sum(1 for receipt in submission_receipts if receipt.needs_manual_review)
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
                "monthly_summary": summary,
                "manual_review_count": manual_review_count,
                "needs_attention": summary.missing_required_count > 0 or summary.api_pending_count > 0 or manual_review_count > 0,
            }
        )

    receipts = staff_month_receipts_queryset(selected_month).order_by("-uploaded_at", "-pk")
    ai_stats = staff_ai_summary_for_month(selected_month)
    open_resubmission_requests = (
        ReceiptResubmissionRequest.objects.filter(
            period_month=selected_month,
            user_id__in=user_ids,
            status=ResubmissionRequestStatus.OPEN,
        )
        .select_related("user", "created_by")
        .order_by("user__username", "service_name_snapshot", "created_at")
    )
    stats = {
        "total_users": len(users),
        "active_users": sum(1 for user in users if getattr(user.profile, "account_status", None) == UserAccountStatus.ACTIVE),
        "submitted": sum(1 for row in rows if row["status"] == "提出済み"),
        "draft": sum(1 for row in rows if row["status"] == "下書き"),
        "not_started": sum(1 for row in rows if row["status"] == "未着手"),
        "incomplete_users": sum(1 for row in rows if not row["monthly_summary"].is_complete),
        "missing_service_count": sum(row["monthly_summary"].missing_required_count for row in rows),
        "api_pending_count": sum(row["monthly_summary"].api_pending_count for row in rows),
        "receipt_count": sum(row["receipt_count"] for row in rows),
        "available_file_count": sum(row["available_file_count"] for row in rows),
        "active_service_count": sum(row["active_service_count"] for row in rows),
        "user_registered_count": sum(row["user_registered_count"] for row in rows),
        "user_stopped_count": sum(row["user_stopped_count"] for row in rows),
        "ai_pending_count": ai_stats["ai_pending_count"],
        "ai_ready_count": ai_stats["ai_ready_count"],
        "ai_queued_count": ai_stats["ai_queued_count"],
        "ai_processing_count": ai_stats["ai_processing_count"],
        "ai_review_count": ai_stats["ai_review_count"],
        "period_mismatch_count": ai_stats["period_mismatch_count"],
        "manual_review_count": ai_stats["manual_review_count"],
        "service_payee_review_count": ai_stats["service_payee_review_count"],
        "open_resubmission_request_count": open_resubmission_requests.count(),
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
            "open_resubmission_requests": open_resubmission_requests,
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
    password_result_user = None
    password_result = None
    password_result_was_random = False

    # 既存データや手動作成ユーザーでプロファイルが欠けていても、
    # ユーザー管理画面が500にならないように補完する。
    for account in User.objects.filter(is_superuser=False, profile__isnull=True):
        UserProfile.objects.get_or_create(user=account)

    action = request.POST.get("action") if request.method == "POST" else ""
    create_form_kwargs = {"allow_admin_role": request.user.is_superuser}

    if action == "update_status":
        status_form = StaffUserStatusForm(request.POST)
        if status_form.is_valid():
            account = status_form.save(updated_by=request.user)
            messages.success(request, f"{account.username} のステータスを {account.profile.get_account_status_display()} に変更しました。")
        else:
            for errors in status_form.errors.values():
                for error in errors:
                    messages.error(request, error)
        return redirect("staff_user_create")

    if action == "update_role":
        if not request.user.is_superuser:
            raise PermissionDenied("権限を変更できるのはスーパーアカウントだけです。")
        role_form = StaffUserRoleForm(request.POST)
        if role_form.is_valid():
            account = role_form.save()
            role_label = "管理者ユーザー" if account.is_staff else "一般ユーザー"
            messages.success(request, f"{account.username} の権限を {role_label} に変更しました。")
        else:
            for errors in role_form.errors.values():
                for error in errors:
                    messages.error(request, error)
        return redirect("staff_user_create")

    if action == "delete_user":
        account = get_object_or_404(User, pk=request.POST.get("user_id"), is_superuser=False)
        if account.pk == request.user.pk:
            messages.error(request, "現在ログイン中の自分自身は削除できません。")
        elif account.is_staff and not request.user.is_superuser:
            raise PermissionDenied("管理者ユーザーを削除できるのはスーパーアカウントだけです。")
        else:
            username = account.username
            # Receipt.service は PROTECT のため、ユーザー削除前に領収書とサービスを明示的な順序で削除する。
            # post_delete シグナルにより実ファイルも同時に削除される。
            with transaction.atomic():
                Receipt.objects.filter(submission__user=account).delete()
                CardStatement.objects.filter(user=account).delete()
                Submission.objects.filter(user=account).delete()
                RegisteredService.objects.filter(user=account).delete()
                account.delete()
            messages.success(request, f"{username} を削除しました。関連するサービス、提出履歴、領収書ファイル、カード明細も削除されました。")
        return redirect("staff_user_create")

    if action == "reset_password":
        password_form = StaffUserPasswordResetForm(request.POST)
        if password_form.is_valid():
            target = password_form.cleaned_data["user_id"]
            if target.is_staff and not request.user.is_superuser:
                raise PermissionDenied("管理者ユーザーのパスワードを変更できるのはスーパーアカウントだけです。")
            password_result_user, password_result, password_result_was_random = password_form.save(updated_by=request.user)
            if password_result_was_random:
                messages.success(request, f"{password_result_user.username} のパスワードをランダム再発行しました。")
            else:
                messages.success(request, f"{password_result_user.username} のパスワードを変更しました。")
        else:
            for errors in password_form.errors.values():
                for error in errors:
                    messages.error(request, error)
        form = StaffUserCreateForm(**create_form_kwargs)
    elif request.method == "POST":
        form = StaffUserCreateForm(request.POST, **create_form_kwargs)
        if form.is_valid():
            generated_user, generated_password = form.save(created_by=request.user)
            messages.success(request, f"{generated_user.username} のアカウントを作成しました。初期パスワードを対象ユーザーへ安全に伝えてください。")
            form = StaffUserCreateForm(**create_form_kwargs)
    else:
        form = StaffUserCreateForm(**create_form_kwargs)

    managed_accounts = User.objects.filter(is_superuser=False).select_related("profile")
    if not request.user.is_superuser:
        managed_accounts = managed_accounts.filter(is_staff=False)
    managed_accounts = (
        managed_accounts.annotate(
            active_service_count=Count(
                "registered_services",
                filter=Q(registered_services__is_active=True),
                distinct=True,
            )
        )
        .order_by("-is_staff", "username")
    )
    return render(
        request,
        "receipts/staff_user_create.html",
        {
            "form": form,
            "generated_user": generated_user,
            "generated_password": generated_password,
            "password_result_user": password_result_user,
            "password_result": password_result,
            "password_result_was_random": password_result_was_random,
            "managed_accounts": managed_accounts,
            "status_choices": UserAccountStatus.choices,
            "role_choices": StaffUserCreateForm.ROLE_CHOICES,
            "can_manage_roles": request.user.is_superuser,
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
def staff_email(request):
    reminder_schedule = EmailReminderSchedule.get_solo()
    initial = {}
    if request.user.email:
        initial["to_email"] = request.user.email
    form = StaffEmailTestForm(initial=initial)
    schedule_form = EmailReminderScheduleForm(instance=reminder_schedule)

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "update_reminder_schedule":
            schedule_data = request.POST.copy()
            # 旧画面・APIクライアントから日付だけが送られた場合も、既存テンプレートを維持する。
            for field_name in (
                "initial_subject_template",
                "initial_body_template",
                "urgent_subject_template",
                "urgent_body_template",
            ):
                if field_name not in schedule_data:
                    schedule_data[field_name] = getattr(reminder_schedule, field_name)
            schedule_form = EmailReminderScheduleForm(schedule_data, instance=reminder_schedule)
            if schedule_form.is_valid():
                updated_schedule = schedule_form.save(commit=False)
                updated_schedule.updated_by = request.user
                updated_schedule.save()
                messages.success(
                    request,
                    f"リマインダー日を毎月{updated_schedule.reminder_day}日、警告日を毎月{updated_schedule.warning_day}日に変更し、メール内容を保存しました。",
                )
                return redirect("staff_email")
        else:
            form = StaffEmailTestForm(request.POST)
            if form.is_valid():
                log, sent = send_test_email(
                    to_email=form.cleaned_data["to_email"],
                    subject=form.cleaned_data["subject"],
                    body=form.cleaned_data["body"],
                    created_by=request.user,
                )
                if sent:
                    messages.success(request, f"テストメールを {log.to_email} へ送信しました。")
                elif log.status == EmailDeliveryStatus.SKIPPED:
                    messages.warning(request, f"{log.to_email} は停止中ユーザーのため、テストメールを送信しませんでした。")
                else:
                    messages.error(request, f"テストメール送信に失敗しました: {log.error or '詳細不明'}")
                return redirect("staff_email")

    recent_logs = EmailDeliveryLog.objects.select_related("user", "created_by").order_by("-created_at")[:30]
    smtp_settings = {
        "host": settings.SMTP_HOST,
        "port": settings.SMTP_PORT,
        "username": settings.SMTP_USERNAME,
        "from_email": settings.SMTP_FROM,
        "starttls": settings.SMTP_STARTTLS,
        "ssl": settings.SMTP_SSL,
        "timeout": settings.SMTP_TIMEOUT_SECONDS,
        "password_set": bool(settings.SMTP_PASSWORD),
        "app_base_url": settings.APP_BASE_URL,
    }
    return render(
        request,
        "receipts/staff_email.html",
        {
            "form": form,
            "schedule_form": schedule_form,
            "reminder_schedule": reminder_schedule,
            "recent_logs": recent_logs,
            "smtp_settings": smtp_settings,
        },
    )


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
        "ai_check_card_last4",
        "ai_check_payee",
        "ai_check_service_payee_related",
        "ai_service_payee_check_memo",
        "ai_check_date",
        "ai_check_amount",
        "ai_check_currency",
        "ai_check_period_match",
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
                "yes" if receipt.ai_check_card_last4 else "no",
                "yes" if receipt.ai_check_payee else "no",
                "yes" if receipt.ai_check_service_payee_related else "no",
                receipt.ai_service_payee_check_memo,
                "yes" if receipt.ai_check_date else "no",
                "yes" if receipt.ai_check_amount else "no",
                "yes" if receipt.ai_check_currency else "no",
                "yes" if receipt.ai_check_period_match else "no",
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
    queryset = (
        Submission.objects.filter(period_month=selected_month, status=SubmissionStatus.SUBMITTED)
        .select_related("user")
        .prefetch_related(Prefetch("receipts", queryset=Receipt.objects.select_related("service")))
    )
    return build_receipts_zip(queryset, f"receipts_{selected_month:%Y-%m}_submitted")


@staff_member_required
def staff_download_submission(request, pk: int):
    submission = get_object_or_404(
        Submission.objects.select_related("user").prefetch_related(Prefetch("receipts", queryset=Receipt.objects.select_related("service"))),
        pk=pk,
    )
    return build_receipts_zip([submission], f"receipts_{submission.period_month:%Y-%m}_{safe_part(submission.user.get_username())}")
