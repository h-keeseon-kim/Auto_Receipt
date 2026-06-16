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
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, Prefetch
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from .forms import MonthSelectForm, ReceiptUploadForm, RegisterForm, RegisteredServiceForm, current_month
from .models import Receipt, RegisteredService, Submission, SubmissionStatus, receipt_expiry_from


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
    active_services = RegisteredService.objects.filter(user=request.user, is_active=True).order_by("name")
    receipts = submission.receipts.select_related("service").all()

    if request.method == "POST":
        action = request.POST.get("action")
        if submission.is_submitted:
            messages.error(request, "この提出月はすでに提出済みのため編集できません。")
            return redirect(submission.get_absolute_url())

        if action == "add_receipt":
            receipt_form = ReceiptUploadForm(request.POST, request.FILES, user=request.user)
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
                    messages.success(request, f"{receipt.service_name_snapshot} の領収書を追加しました。")
                    return redirect(f"{reverse('dashboard')}?month={month_query(selected_month)}")
        elif action == "submit":
            try:
                submission.submit()
                messages.success(request, f"{selected_month:%Y年%m月}分を提出しました。")
                return redirect(submission.get_absolute_url())
            except ValidationError as exc:
                messages.error(request, exc.message if hasattr(exc, "message") else exc.messages[0])
            receipt_form = ReceiptUploadForm(user=request.user)
        else:
            messages.error(request, "不明な操作です。")
            receipt_form = ReceiptUploadForm(user=request.user)
    else:
        receipt_form = ReceiptUploadForm(user=request.user)

    uploaded_service_ids = set(receipts.values_list("service_id", flat=True))
    service_rows = [
        {"service": service, "uploaded": service.id in uploaded_service_ids}
        for service in active_services
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
            "service_rows": service_rows,
            "selected_month": selected_month,
            "retention_months": settings.RECEIPT_RETENTION_MONTHS,
        },
    )


@login_required
def service_create(request):
    if request.method == "POST":
        form = RegisteredServiceForm(request.POST)
        if form.is_valid():
            service = form.save(commit=False)
            service.user = request.user
            try:
                service.full_clean()
                service.save()
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                messages.success(request, f"{service.name} を登録しました。")
                return redirect("dashboard")
    else:
        form = RegisteredServiceForm()
    return render(request, "receipts/service_form.html", {"form": form, "title": "サービス登録"})


@login_required
def service_update(request, pk: int):
    service = get_object_or_404(RegisteredService, pk=pk, user=request.user)
    if request.method == "POST":
        form = RegisteredServiceForm(request.POST, instance=service)
        if form.is_valid():
            service = form.save(commit=False)
            try:
                service.full_clean()
                service.save()
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                messages.success(request, f"{service.name} を更新しました。")
                return redirect("dashboard")
    else:
        form = RegisteredServiceForm(instance=service)
    return render(request, "receipts/service_form.html", {"form": form, "title": "サービス編集", "service": service})


@login_required
def service_archive(request, pk: int):
    service = get_object_or_404(RegisteredService, pk=pk, user=request.user)
    if request.method != "POST":
        raise Http404
    service.is_active = False
    service.save(update_fields=["is_active", "updated_at"])
    messages.success(request, f"{service.name} を利用停止にしました。過去の提出履歴は残ります。")
    return redirect("dashboard")


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
    service_name = receipt.service_name_snapshot
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
def staff_dashboard(request):
    selected_month, month_form = parse_month_from_request(request)
    users = User.objects.filter(is_active=True, is_staff=False).order_by("username")
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
            }
        )

    stats = {
        "total_users": users.count(),
        "submitted": sum(1 for row in rows if row["status"] == "提出済み"),
        "draft": sum(1 for row in rows if row["status"] == "下書き"),
        "not_started": sum(1 for row in rows if row["status"] == "未着手"),
        "receipt_count": sum(row["receipt_count"] for row in rows),
        "available_file_count": sum(row["available_file_count"] for row in rows),
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
                service_part = safe_part(receipt.service_name_snapshot, f"receipt-{receipt.id}")
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
