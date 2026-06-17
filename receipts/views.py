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
from django.db.models import Count, Prefetch, Q
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.text import slugify

from .forms import (
    MonthSelectForm,
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


def managed_users_queryset():
    return User.objects.filter(is_active=True, is_staff=False, is_superuser=False).order_by("username")


def get_managed_user(user_id: int):
    return get_object_or_404(User, pk=user_id, is_active=True, is_staff=False, is_superuser=False)


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
        return redirect("dashboard")
    if request.method == "POST":
        form = RegisterForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            messages.success(request, "アカウントを作成しました。")
            return redirect("dashboard")
    else:
        form = RegisterForm()
    return render(request, "registration/register.html", {"form": form})


@login_required
def dashboard(request):
    selected_month, month_form = parse_month_from_request(request)
    submission, _ = Submission.objects.get_or_create(user=request.user, period_month=selected_month)
    active_services = RegisteredService.objects.filter(user=request.user, is_active=True).order_by("name", "billing_type")
    uploadable_services = RegisteredService.objects.uploadable_for(request.user, selected_month)
    stopped_services = RegisteredService.objects.filter(user=request.user, is_active=False).order_by("-deactivated_at", "name", "billing_type")
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

    uploaded_service_ids = set(receipts.values_list("service_id", flat=True))
    service_rows = [
        {"service": service, "uploaded": service.id in uploaded_service_ids}
        for service in active_services
    ]
    stopped_rows = [
        {"service": service, "uploadable": service.is_uploadable_for(selected_month)}
        for service in stopped_services
    ]
    return render(
        request,
        "receipts/dashboard.html",
        {
            "month_form": month_form,
            "receipt_form": receipt_form,
            "submission": submission,
            "receipts": receipts,
            "active_services": active_services,
            "uploadable_services": uploadable_services,
            "stopped_services": stopped_services,
            "service_rows": service_rows,
            "stopped_rows": stopped_rows,
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
    users = managed_users_queryset().annotate(
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
    selected_user = None
    services = RegisteredService.objects.select_related(
        "user", "catalog_service", "registered_by", "deactivated_by"
    ).filter(user__is_staff=False, user__is_superuser=False)
    user_id = request.GET.get("user")
    if user_id:
        selected_user = get_managed_user(user_id)
        services = services.filter(user=selected_user)
    services = services.order_by("user__username", "-is_active", "name", "billing_type")

    catalog_services = ServiceCatalog.objects.annotate(
        assigned_count=Count("registered_services", distinct=True),
        active_user_count=Count("registered_services", filter=Q(registered_services__is_active=True), distinct=True),
    ).order_by("name", "billing_type")

    user_change_services = (
        RegisteredService.objects.select_related("user", "catalog_service", "registered_by", "deactivated_by")
        .filter(Q(registration_source=ServiceRegistrationSource.USER) | Q(deactivation_source=ServiceDeactivationSource.USER))
        .order_by("-updated_at")[:25]
    )
    return render(
        request,
        "receipts/staff_services.html",
        {
            "users": users,
            "services": services,
            "selected_user": selected_user,
            "catalog_services": catalog_services,
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
    return redirect("staff_user_services", user_id=service.user_id)


@staff_member_required
def staff_service_activate(request, pk: int):
    service = get_object_or_404(RegisteredService.objects.select_related("user"), pk=pk, user__is_staff=False, user__is_superuser=False)
    if request.method != "POST":
        raise Http404
    service.activate()
    messages.success(request, f"{service.user.username} の {service.display_name} を利用中に戻しました。")
    return redirect("staff_user_services", user_id=service.user_id)


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
    return redirect("staff_services")


@staff_member_required
def staff_catalog_activate(request, pk: int):
    catalog = get_object_or_404(ServiceCatalog, pk=pk)
    if request.method != "POST":
        raise Http404
    catalog.is_active = True
    catalog.save(update_fields=["is_active", "updated_at"])
    messages.success(request, f"サービスマスター {catalog.display_name} を選択可能に戻しました。")
    return redirect("staff_services")


# 旧URL互換: 一般ユーザーには表示せず、管理者専用のサービス管理へ移行する。
service_create = staff_service_create
service_update = staff_service_update
service_archive = staff_service_archive


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


@login_required
def history(request):
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
    filename = receipt.original_filename or Path(receipt.file.name).name
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
    selected_month, month_form = parse_month_from_request(request)
    users = managed_users_queryset().annotate(
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
    receipt_prefetch = Prefetch("receipts", queryset=Receipt.objects.select_related("service"))
    submissions = (
        Submission.objects.filter(period_month=selected_month, user__in=users)
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

    stats = {
        "total_users": users.count(),
        "submitted": sum(1 for row in rows if row["status"] == "提出済み"),
        "draft": sum(1 for row in rows if row["status"] == "下書き"),
        "not_started": sum(1 for row in rows if row["status"] == "未着手"),
        "receipt_count": sum(row["receipt_count"] for row in rows),
        "available_file_count": sum(row["available_file_count"] for row in rows),
        "active_service_count": sum(row["active_service_count"] for row in rows),
        "user_registered_count": sum(row["user_registered_count"] for row in rows),
        "user_stopped_count": sum(row["user_stopped_count"] for row in rows),
    }
    return render(
        request,
        "receipts/staff_dashboard.html",
        {
            "rows": rows,
            "stats": stats,
            "month_form": month_form,
            "selected_month": selected_month,
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
        "original_filename",
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
                receipt.original_filename,
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
                service_part = safe_part(receipt.service_display_name_snapshot, f"receipt-{receipt.id}")
                original = receipt.original_filename or Path(receipt.file.name).name
                original_suffix = Path(original).suffix.lower() or Path(receipt.file.name).suffix.lower()
                arcname = f"{period}/{user_part}/{status_part}/{receipt.id}_{service_part}{original_suffix}"
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
