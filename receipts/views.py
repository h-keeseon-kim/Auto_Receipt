from __future__ import annotations

import csv
import logging
import json
import zipfile
from datetime import date, timedelta
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
from django.db import IntegrityError, transaction
from django.db.models import Case, Count, IntegerField, Prefetch, Q, When
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.utils.http import url_has_allowed_host_and_scheme
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_POST

from .ai_processing import claim_pending_receipts_for_ai_processing, reset_ai_processing_state, start_background_ai_processing
from .ai_filename import target_card_last4
from .emailing import send_resubmission_request_email, send_test_email
from .forms import (
    MonthSelectForm,
    ReceiptMonthSelectForm,
    StatementMonthSelectForm,
    CardStatementUploadForm,
    ReceiptBatchUploadForm,
    ReceiptFileReplaceForm,
    StaffReceiptReviewForm,
    ReceiptUploadForm,
    RegisterForm,
    ServiceExceptionRequestForm,
    ServiceCatalogForm,
    StaffServiceExceptionReviewForm,
    StaffServiceForm,
    StaffUserCreateForm,
    StaffSuperuserEmailForm,
    StaffUserPasswordResetForm,
    StaffUserRoleForm,
    StaffUserStatusForm,
    EmailReminderScheduleForm,
    StaffEmailTestForm,
    StyledPasswordChangeForm,
    UserServicePCardForm,
    UserServiceRegistrationForm,
    UserServiceStopForm,
    current_month,
    current_receipt_month,
)
from .models import (
    BillingType,
    CardStatement,
    CardStatementItem,
    CardStatementPlanChangeInference,
    CardStatementReceiptEvidence,
    CardStatementStatus,
    MonthlyServiceDeclaration,
    PlanChangeInferenceStatus,
    Receipt,
    ReceiptAdminReviewStatus,
    EmailDeliveryLog,
    EmailReminderSchedule,
    EmailDeliveryStatus,
    ReceiptFilenameStatus,
    ReceiptPeriodCheckStatus,
    ReceiptUploadSource,
    ReceiptResubmissionRequest,
    RegisteredService,
    ResubmissionRequestStatus,
    ServiceCatalog,
    ServiceDeactivationSource,
    ServiceExceptionRequest,
    ServiceExceptionRequestStatus,
    ServiceRegistrationSource,
    StatementMatchReason,
    StatementMatchStatus,
    StatementReceiptEvidenceUsageMode,
    Submission,
    UserAccountStatus,
    SubmissionStatus,
    UserProfile,
    receipt_expiry_from,
    receipt_month_for_submission,
    receipt_month_for_statement,
    submission_month_for_receipt,
)
from .monthly_status import build_user_month_summary
from .statement_processing import (
    mark_card_statements_for_global_usage_refresh,
    mark_later_card_statements_for_global_usage_refresh,
    reconcile_card_statement_items,
    reconcile_pending_card_statement_month_semantics,
    start_background_statement_processing,
)
from .statement_pdf import build_card_statement_reconciliation_pdf, reconciliation_report_filename


logger = logging.getLogger(__name__)


def safe_part(value: str, fallback: str = "item") -> str:
    value = slugify(value or "", allow_unicode=True)
    return value or fallback


def parse_month_from_request(request, *, month_label=None):
    form = MonthSelectForm(request.GET or None, month_label=month_label)
    if form.is_valid():
        return form.cleaned_data["month"], form
    initial_month = current_month()
    return initial_month, MonthSelectForm(initial={"month": initial_month}, month_label=month_label)


def parse_statement_month_from_request(request):
    """ご利用代金明細ページでは、直接表示時の初期月を前月にする。"""

    form = StatementMonthSelectForm(request.GET or None)
    if form.is_valid():
        return form.cleaned_data["month"], form
    initial_month = current_receipt_month()
    return initial_month, StatementMonthSelectForm(initial={"month": initial_month})


def parse_receipt_month_from_request(request):
    """ユーザー画面では領収書発行月を直接選ばせる。

    v1.6.0以前の ``?month=YYYY-MM`` は内部の提出サイクル月として扱い、
    既存リンクやブックマークとの互換性を維持する。
    """

    if "receipt_month" in request.GET:
        form = ReceiptMonthSelectForm(request.GET)
        if form.is_valid():
            receipt_month = form.cleaned_data["receipt_month"]
            return receipt_month, submission_month_for_receipt(receipt_month), form

    if "month" in request.GET:
        legacy_form = MonthSelectForm({"month": request.GET.get("month")})
        if legacy_form.is_valid():
            submission_month = legacy_form.cleaned_data["month"]
            receipt_month = receipt_month_for_submission(submission_month)
            return (
                receipt_month,
                submission_month,
                ReceiptMonthSelectForm(initial={"receipt_month": receipt_month}),
            )

    receipt_month = receipt_month_for_submission(current_month())
    return (
        receipt_month,
        submission_month_for_receipt(receipt_month),
        ReceiptMonthSelectForm(initial={"receipt_month": receipt_month}),
    )


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


def month_submission_display_status(
    submission: Submission | None,
    summary,
    *,
    has_available_receipts: bool = False,
    has_open_resubmission_requests: bool = False,
) -> tuple[str, str]:
    """月次画面向けの表示ステータスを返す。

    Pカード提出対象がなく、追加領収書・再提出依頼もない月は、
    DB上に閲覧時作成された下書きがあっても「提出不要」と表示する。
    """

    if submission is not None and submission.is_submitted:
        return "提出済み", "submitted"
    if (
        summary.total_services == 0
        and not has_available_receipts
        and not has_open_resubmission_requests
    ):
        return "提出不要", "neutral"
    if submission is not None:
        return submission.get_status_display(), submission.status
    return "未着手", "neutral"


@login_required
@require_POST
def tutorial_complete(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    profile.mark_tutorial_completed()
    return JsonResponse({"ok": True, "tutorial_completed": True})


def resolve_matching_resubmission_requests(receipt: Receipt, *, by) -> int:
    """再提出依頼と同じ月・サービスの領収書が再アップロードされたら対応済みにする。"""

    now = timezone.now()
    requests = ReceiptResubmissionRequest.objects.filter(
        user=receipt.submission.user,
        period_month=receipt.submission.period_month,
        status=ResubmissionRequestStatus.OPEN,
    )
    if receipt.is_extra:
        requests = requests.filter(is_extra=True)
        exact = requests.filter(receipt_memo_snapshot__iexact=receipt.memo).order_by("created_at").first()
        target = exact or requests.order_by("created_at").first()
        if target is None:
            return 0
        return ReceiptResubmissionRequest.objects.filter(pk=target.pk).update(
            status=ResubmissionRequestStatus.RESOLVED,
            resolved_at=now,
            resolved_by=by,
        )
    return requests.filter(
        is_extra=False,
        service_name_snapshot=receipt.service_name_snapshot,
        billing_type_snapshot=receipt.billing_type_snapshot,
    ).update(status=ResubmissionRequestStatus.RESOLVED, resolved_at=now, resolved_by=by)


def save_receipt_batch(*, submission: Submission, form: ReceiptBatchUploadForm, uploaded_by) -> tuple[list[Receipt], bool, int]:
    """複数ファイルを同じサービス（またはその他）へ一括登録する。

    提出済み月へ追加した場合は下書きへ戻す。DBロールバック時に、先に保存された
    ストレージファイルが孤児化しないよう可能な範囲で削除する。
    """

    uploads = list(form.cleaned_data.get("files") or [])
    service = form.selected_service
    is_extra = form.is_extra
    memo = (form.cleaned_data.get("memo") or "").strip() if is_extra else ""
    upload_source = (
        ReceiptUploadSource.ADMIN
        if uploaded_by.is_staff and uploaded_by.pk != submission.user_id
        else ReceiptUploadSource.USER
    )
    was_submitted = submission.is_submitted
    created_receipts: list[Receipt] = []

    try:
        with transaction.atomic():
            locked_submission = Submission.objects.select_for_update().get(pk=submission.pk)
            was_submitted = locked_submission.is_submitted
            if was_submitted:
                locked_submission.status = SubmissionStatus.DRAFT
                locked_submission.submitted_at = None
                locked_submission.save(update_fields=["status", "submitted_at", "updated_at"])

            for upload in uploads:
                receipt = Receipt(
                    submission=locked_submission,
                    service=None if is_extra else service,
                    is_extra=is_extra,
                    service_name_snapshot="その他" if is_extra else service.name,
                    billing_type_snapshot=BillingType.OTHER if is_extra else service.billing_type,
                    memo=memo,
                    file=upload,
                    original_filename=upload.name,
                    file_size=upload.size,
                    content_type=getattr(upload, "content_type", "") or "",
                    upload_source=upload_source,
                    uploaded_by=uploaded_by,
                    expires_at=receipt_expiry_from(timezone.now()),
                )
                receipt.full_clean()
                created_receipts.append(receipt)
                # 同じサービスへ後から1件ずつ追加した場合も、既存レコードを更新せず
                # 必ず新しい領収書レコードとして追記する。
                receipt.save(force_insert=True)

            if service is not None:
                MonthlyServiceDeclaration.objects.filter(
                    user=locked_submission.user,
                    service=service,
                    period_month=locked_submission.period_month,
                ).delete()
    except Exception:
        for receipt in created_receipts:
            try:
                if (
                    receipt.file
                    and getattr(receipt.file, "_committed", False)
                    and receipt.file.name
                    and receipt.file.storage.exists(receipt.file.name)
                ):
                    receipt.file.storage.delete(receipt.file.name)
            except Exception:
                pass
        raise

    resolved_count = 0
    for receipt in created_receipts:
        resolved_count += resolve_matching_resubmission_requests(receipt, by=uploaded_by)
    return created_receipts, was_submitted, resolved_count


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

    selected_receipt_month, selected_month, month_form = parse_receipt_month_from_request(request)
    submission, _ = Submission.objects.get_or_create(user=request.user, period_month=selected_month)
    uploadable_services = RegisteredService.objects.uploadable_for(request.user, selected_month).select_related("catalog_service")
    open_resubmission_requests = ReceiptResubmissionRequest.objects.filter(
        user=request.user,
        period_month=selected_month,
        status=ResubmissionRequestStatus.OPEN,
    ).order_by("service_name_snapshot", "created_at")

    selected_upload_choice = request.GET.get("service", "")
    upload_form = ReceiptBatchUploadForm(
        user=request.user,
        period_month=selected_month,
        selected_choice=selected_upload_choice,
    )

    if request.method == "POST":
        action = request.POST.get("action")

        if action in {"declare_no_usage", "clear_no_usage"}:
            if submission.is_submitted:
                messages.error(request, "提出済みの内容はAPI利用状況を変更できません。再提出が必要な場合は管理者へ連絡してください。")
                return redirect(submission.get_absolute_url())
            service = get_object_or_404(
                RegisteredService,
                pk=request.POST.get("service_id"),
                user=request.user,
                billing_type=BillingType.METERED,
            )
            if not service.is_uploadable_for(selected_month):
                messages.error(request, "このサービスは選択した領収書発行月の対象ではありません。")
            elif action == "declare_no_usage":
                if Receipt.objects.filter(submission=submission, service=service).exists():
                    messages.error(request, f"{service.display_name} には領収書が登録済みのため『この月は利用なし』にできません。")
                else:
                    MonthlyServiceDeclaration.objects.update_or_create(
                        user=request.user,
                        service=service,
                        period_month=selected_month,
                        defaults={"no_usage": True, "declared_by": request.user},
                    )
                    messages.success(
                        request,
                        f"{service.display_name} を {selected_receipt_month:%Y年%m月} は利用なしとして記録しました。",
                    )
            else:
                deleted, _ = MonthlyServiceDeclaration.objects.filter(
                    user=request.user,
                    service=service,
                    period_month=selected_month,
                ).delete()
                if deleted:
                    messages.success(request, f"{service.display_name} の『この月は利用なし』を取り消しました。")
            return redirect(
                f"{reverse('dashboard')}?receipt_month={month_query(selected_receipt_month)}"
            )

        if action in {"add_receipts", "add_receipt", "add_extra_receipt"}:
            # v1.3系の画面を開いたままデプロイされた場合も、旧単一ファイルPOSTを受け付ける。
            form_data = request.POST.copy()
            files_data = request.FILES.copy()
            if action == "add_receipt":
                form_data["service"] = request.POST.get("service", "")
                files_data.setlist("files", request.FILES.getlist("file"))
            elif action == "add_extra_receipt":
                form_data["service"] = ReceiptBatchUploadForm.OTHER_VALUE
                files_data.setlist("files", request.FILES.getlist("file"))
            upload_form = ReceiptBatchUploadForm(
                form_data,
                files_data,
                user=request.user,
                period_month=selected_month,
            )
            if upload_form.is_valid():
                try:
                    created_receipts, was_submitted, resolved_count = save_receipt_batch(
                        submission=submission,
                        form=upload_form,
                        uploaded_by=request.user,
                    )
                except ValidationError as exc:
                    add_validation_errors(upload_form, exc)
                except Exception:
                    logger.exception(
                        "Receipt upload failed for user=%s period_month=%s",
                        request.user.pk,
                        selected_month,
                    )
                    messages.error(
                        request,
                        "領収書ファイルの保存に失敗しました。ファイルは登録されていません。"
                        "管理者はRailwayのDeploy Logsで『Receipt upload failed』直後のエラーを確認してください。",
                    )
                else:
                    count = len(created_receipts)
                    selected_label = "その他" if upload_form.is_extra else upload_form.selected_service.display_name
                    if resolved_count:
                        messages.success(request, f"{selected_label} の再提出依頼を対応済みにしました。")
                    if was_submitted:
                        messages.info(
                            request,
                            "領収書を追加したため、状態を『未提出領収書あり』に戻しました。内容を確認して再度提出してください。",
                        )
                    if upload_form.is_extra:
                        total_for_selection = Receipt.objects.filter(
                            submission=submission,
                            is_extra=True,
                            file_deleted_at__isnull=True,
                        ).exclude(file="").count()
                        messages.success(
                            request,
                            f"その他の領収書を新しく{count}件追加しました。現在の登録件数は計{total_for_selection}件です。メモはAIの参考情報として使われますが、領収書ファイル内の情報を優先して確認します。",
                        )
                    else:
                        total_for_selection = Receipt.objects.filter(
                            submission=submission,
                            service=upload_form.selected_service,
                            file_deleted_at__isnull=True,
                        ).exclude(file="").count()
                        messages.success(
                            request,
                            f"{selected_label} の領収書を新しく{count}件追加しました。現在このサービスには計{total_for_selection}件登録されています。AIによるファイル名修正・検査は、管理者が実行した後に反映されます。",
                        )
                    selected_value = ReceiptBatchUploadForm.OTHER_VALUE if upload_form.is_extra else str(upload_form.selected_service.pk)
                    return redirect(
                        f"{reverse('dashboard')}?receipt_month={month_query(selected_receipt_month)}&service={selected_value}"
                    )

        elif action == "submit":
            monthly_summary = build_user_month_summary(request.user, selected_month)
            if monthly_summary.rows and not monthly_summary.is_complete:
                unresolved = [
                    row.service.display_name
                    for row in (*monthly_summary.missing_required, *monthly_summary.api_pending)
                ]
                preview = "、".join(unresolved[:5])
                if len(unresolved) > 5:
                    preview += f" ほか{len(unresolved) - 5}件"
                messages.warning(
                    request,
                    "今回アップロードした領収書は受け付け済みです。"
                    f"まだ領収書の提出またはAPI利用確認が済んでいないサービスがあります（{preview}）。"
                    "ほかの領収書も忘れずに追加してください。",
                )
                return redirect(
                    f"{reverse('dashboard')}?receipt_month={month_query(selected_receipt_month)}"
                )
            try:
                submission.submit()
                messages.success(
                    request,
                    f"{selected_receipt_month:%Y年%m月}発行分の領収書提出を完了しました。",
                )
                return redirect(submission.get_absolute_url())
            except ValidationError as exc:
                messages.error(request, exc.message if hasattr(exc, "message") else exc.messages[0])
        else:
            messages.error(request, "不明な操作です。")

    receipts = submission.receipts.select_related("service").all()
    monthly_summary = build_user_month_summary(request.user, selected_month)
    has_available_receipts = receipts.filter(file_deleted_at__isnull=True).exclude(file="").exists()
    has_extra_receipts = receipts.filter(
        is_extra=True,
        file_deleted_at__isnull=True,
    ).exclude(file="").exists()
    has_open_resubmission_requests = open_resubmission_requests.exists()
    display_status_label, display_status_class = month_submission_display_status(
        submission,
        monthly_summary,
        has_available_receipts=(
            has_available_receipts if monthly_summary.total_services else has_extra_receipts
        ),
        has_open_resubmission_requests=has_open_resubmission_requests,
    )
    can_submit = (
        not submission.is_submitted
        and (monthly_summary.total_services > 0 or has_extra_receipts)
    )
    return render(
        request,
        "receipts/dashboard.html",
        {
            "month_form": month_form,
            "upload_form": upload_form,
            # 旧テンプレート拡張との互換用。新画面は upload_form を使用する。
            "receipt_form": upload_form,
            "submission": submission,
            "receipts": receipts,
            "uploadable_services": uploadable_services,
            "monthly_summary": monthly_summary,
            "open_resubmission_requests": open_resubmission_requests,
            "selected_month": selected_month,
            "selected_receipt_month": selected_receipt_month,
            "target_receipt_month": selected_receipt_month,
            "selected_upload_choice": selected_upload_choice,
            "display_status_label": display_status_label,
            "display_status_class": display_status_class,
            "has_available_receipts": has_available_receipts,
            "can_submit": can_submit,
        },
    )


@login_required
def user_services(request):
    services = RegisteredService.objects.filter(user=request.user).select_related("catalog_service", "registered_by", "deactivated_by").order_by("-is_active", "name", "billing_type")
    active_services = [service for service in services if service.is_active]
    stopped_services = [service for service in services if not service.is_active]
    available_catalog_count = UserServiceRegistrationForm(user=request.user).fields[
        "catalog_service"
    ].queryset.count()
    exception_requests = ServiceExceptionRequest.objects.filter(user=request.user).select_related(
        "reviewed_by",
        "approved_registered_service",
    ).order_by("-created_at")
    pending_exception_count = exception_requests.filter(status=ServiceExceptionRequestStatus.PENDING).count()
    active_p_card_count = sum(service.uses_p_card for service in active_services)
    active_non_p_card_count = len(active_services) - active_p_card_count
    return render(
        request,
        "receipts/user_services.html",
        {
            "services": services,
            "active_services": active_services,
            "stopped_services": stopped_services,
            "available_catalog_count": available_catalog_count,
            "exception_requests": exception_requests,
            "pending_exception_count": pending_exception_count,
            "active_p_card_count": active_p_card_count,
            "active_non_p_card_count": active_non_p_card_count,
        },
    )


@login_required
def user_service_create(request):
    if request.user.is_staff:
        return redirect("staff_services")
    if request.method == "POST":
        form = UserServiceRegistrationForm(request.POST, user=request.user)
        if form.is_valid():
            try:
                with transaction.atomic():
                    service = form.save()
            except ValidationError as exc:
                add_validation_errors(form, exc)
            except IntegrityError:
                # 二重クリックや同時POSTでも500にせず、すでに登録済みとして案内する。
                form.add_error(None, "このサービス・支払い方法はすでに利用サービスへ登録されています。")
            else:
                messages.success(
                    request,
                    f"{service.display_name} を利用サービスとして登録しました。Pカード設定: {service.p_card_usage_label}。管理者画面にもユーザー登録として記録されます。",
                )
                return redirect("user_services")
    else:
        form = UserServiceRegistrationForm(user=request.user)
    return render(
        request,
        "receipts/user_service_form.html",
        {
            "title": "サービス利用登録",
            "form": form,
            "submit_label": "利用サービスへ登録する",
            "back_url": reverse("user_services"),
            "exception_request_url": reverse("service_exception_request_create"),
            "available_catalog_count": form.fields["catalog_service"].queryset.count(),
        },
    )


@login_required
def service_exception_request_create(request):
    """サービスマスターに存在しないサービスについてのみ例外申請を受け付ける。"""

    if request.user.is_staff:
        return redirect("staff_exception_requests")
    if request.method == "POST":
        form = ServiceExceptionRequestForm(request.POST, user=request.user)
        if form.is_valid():
            try:
                with transaction.atomic():
                    request_item = form.save()
            except IntegrityError:
                # 二重クリックや同時POSTでも500にせず、確認待ちの重複として案内する。
                form.add_error(None, "同じサービス・支払い方法の例外申請がすでに確認待ちです。")
            else:
                messages.success(
                    request,
                    f"{request_item.display_name} の例外申請を提出しました。Pカード設定: {request_item.p_card_usage_label}。管理者が承認するまで利用サービスには追加されません。",
                )
                return redirect("user_services")
    else:
        form = ServiceExceptionRequestForm(user=request.user)
    return render(
        request,
        "receipts/service_exception_request_form.html",
        {
            "title": "新規サービス例外申請",
            "form": form,
            "submit_label": "例外申請を提出する",
            "back_url": reverse("user_service_create"),
        },
    )


@login_required
def user_service_p_card_update(request, pk: int):
    """ユーザーが自分の利用サービスのPカード利用有無を変更する。"""

    if request.user.is_staff:
        return redirect("staff_services")
    service = get_object_or_404(RegisteredService, pk=pk, user=request.user)
    previous_value = service.uses_p_card
    if request.method == "POST":
        form = UserServicePCardForm(request.POST, instance=service)
        if form.is_valid():
            service = form.save()
            if previous_value and not service.uses_p_card:
                ReceiptResubmissionRequest.objects.filter(
                    user=request.user,
                    service_name_snapshot=service.name,
                    billing_type_snapshot=service.billing_type,
                    is_extra=False,
                    status=ResubmissionRequestStatus.OPEN,
                ).update(
                    status=ResubmissionRequestStatus.RESOLVED,
                    resolved_at=timezone.now(),
                    resolved_by=request.user,
                )
                messages.success(
                    request,
                    f"{service.display_name} をPカード未使用に変更しました。このサービスは領収書提出・リマインドメール・Pカード明細照合の対象外です。既存の領収書履歴は保持されます。",
                )
            elif not previous_value and service.uses_p_card:
                messages.success(
                    request,
                    f"{service.display_name} をPカード利用に変更しました。領収書提出・リマインドメール・Pカード明細照合の対象に含まれます。",
                )
            else:
                messages.info(request, f"{service.display_name} のPカード設定に変更はありません。")
            return redirect("user_services")
    else:
        form = UserServicePCardForm(instance=service)
    return render(
        request,
        "receipts/user_service_p_card_form.html",
        {
            "service": service,
            "form": form,
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
            if service.final_receipt_month:
                detail = f"最後にアップロードすべき領収書月は {service.final_receipt_month:%Y年%m月} として管理者画面にも記録されます。"
            else:
                detail = "Pカード未使用のため、最後にアップロードすべき領収書月はありません。"
            messages.success(request, f"{service.display_name} を利用停止にしました。{detail}")
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


def approve_service_exception_request(request_id: int, *, reviewer, review_note: str = "") -> ServiceExceptionRequest:
    """例外申請を承認し、サービスマスターとユーザー利用サービスへ反映する。"""

    with transaction.atomic():
        request_item = (
            ServiceExceptionRequest.objects.select_for_update()
            .select_related("user")
            .get(pk=request_id, status=ServiceExceptionRequestStatus.PENDING)
        )
        catalog = ServiceCatalog.objects.filter(
            name__iexact=request_item.service_name,
            billing_type=request_item.billing_type,
        ).order_by("pk").first()
        if catalog is None:
            try:
                # 別ユーザーの同一サービス申請が同時承認された場合も、既存マスターを再利用する。
                with transaction.atomic():
                    catalog = ServiceCatalog.objects.create(
                        name=request_item.service_name,
                        billing_type=request_item.billing_type,
                        is_active=True,
                        memo="例外申請の承認時に自動作成されました。",
                        created_by=reviewer,
                    )
            except IntegrityError:
                catalog = ServiceCatalog.objects.get(
                    name__iexact=request_item.service_name,
                    billing_type=request_item.billing_type,
                )
        elif not catalog.is_active:
            catalog.is_active = True
            catalog.save(update_fields=["is_active", "updated_at"])

        service = RegisteredService.objects.filter(
            user=request_item.user,
            name__iexact=request_item.service_name,
            billing_type=request_item.billing_type,
        ).order_by("pk").first()
        purpose_note = f"例外申請用途: {request_item.purpose}"
        if service is None:
            service = RegisteredService(
                user=request_item.user,
                catalog_service=catalog,
                name=catalog.name,
                billing_type=catalog.billing_type,
                is_active=True,
                uses_p_card=request_item.uses_p_card,
                memo=purpose_note,
                registration_source=ServiceRegistrationSource.EXCEPTION_REQUEST,
                registered_by=reviewer,
            )
        else:
            service.catalog_service = catalog
            service.name = catalog.name
            service.billing_type = catalog.billing_type
            service.is_active = True
            service.uses_p_card = request_item.uses_p_card
            if purpose_note not in (service.memo or ""):
                service.memo = "\n".join(part for part in [service.memo.strip(), purpose_note] if part)
            service.registration_source = ServiceRegistrationSource.EXCEPTION_REQUEST
            service.registered_by = reviewer
            service.deactivation_source = ""
            service.deactivated_by = None
            service.deactivated_at = None
            service.final_receipt_month = None
            service.stop_note = ""
        service.full_clean()
        service.save()

        request_item.status = ServiceExceptionRequestStatus.APPROVED
        request_item.review_note = (review_note or "").strip()
        request_item.reviewed_by = reviewer
        request_item.reviewed_at = timezone.now()
        request_item.approved_catalog_service = catalog
        request_item.approved_registered_service = service
        request_item.save(
            update_fields=[
                "status",
                "review_note",
                "reviewed_by",
                "reviewed_at",
                "approved_catalog_service",
                "approved_registered_service",
                "updated_at",
            ]
        )
    return request_item


def reject_service_exception_request(request_id: int, *, reviewer, review_note: str) -> ServiceExceptionRequest:
    with transaction.atomic():
        request_item = ServiceExceptionRequest.objects.select_for_update().get(
            pk=request_id,
            status=ServiceExceptionRequestStatus.PENDING,
        )
        request_item.status = ServiceExceptionRequestStatus.REJECTED
        request_item.review_note = (review_note or "").strip()
        request_item.reviewed_by = reviewer
        request_item.reviewed_at = timezone.now()
        request_item.save(
            update_fields=["status", "review_note", "reviewed_by", "reviewed_at", "updated_at"]
        )
    return request_item


@staff_member_required
def staff_exception_requests(request):
    status_filter = request.GET.get("status", "pending")
    if status_filter not in {"pending", "approved", "rejected", "all"}:
        status_filter = "pending"

    if request.method == "POST":
        form = StaffServiceExceptionReviewForm(request.POST)
        if form.is_valid():
            request_item = form.cleaned_data["request_id"]
            decision = form.cleaned_data["decision"]
            note = form.cleaned_data.get("review_note", "")
            try:
                if decision == StaffServiceExceptionReviewForm.DECISION_APPROVE:
                    reviewed = approve_service_exception_request(
                        request_item.pk,
                        reviewer=request.user,
                        review_note=note,
                    )
                    messages.success(
                        request,
                        f"{reviewed.user.username} の {reviewed.display_name} を承認し、利用サービスへ追加しました。",
                    )
                else:
                    reviewed = reject_service_exception_request(
                        request_item.pk,
                        reviewer=request.user,
                        review_note=note,
                    )
                    messages.success(request, f"{reviewed.user.username} の {reviewed.display_name} を却下しました。")
            except ServiceExceptionRequest.DoesNotExist:
                messages.error(request, "この例外申請は別の管理者によってすでに処理されています。")
            return redirect(f"{reverse('staff_exception_requests')}?status={status_filter}")
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, error)

    queryset = ServiceExceptionRequest.objects.select_related(
        "user",
        "reviewed_by",
        "approved_catalog_service",
        "approved_registered_service",
    )
    if status_filter != "all":
        queryset = queryset.filter(status=status_filter)
    queryset = queryset.order_by(
        Case(
            When(status=ServiceExceptionRequestStatus.PENDING, then=0),
            default=1,
            output_field=IntegerField(),
        ),
        "-created_at",
    )
    page_obj = Paginator(queryset, 30).get_page(request.GET.get("page"))
    pending_count = ServiceExceptionRequest.objects.filter(status=ServiceExceptionRequestStatus.PENDING).count()
    return render(
        request,
        "receipts/staff_exception_requests.html",
        {
            "page_obj": page_obj,
            "status_filter": status_filter,
            "pending_count": pending_count,
        },
    )


@staff_member_required
def staff_services(request):
    active_tab = request.GET.get("tab") or ("users" if request.GET.get("user") else "catalog")
    if active_tab not in {"catalog", "users"}:
        active_tab = "catalog"
    users = list(
        managed_users_queryset().annotate(
            total_service_count=Count("registered_services", distinct=True),
            active_service_count=Count(
                "registered_services",
                filter=Q(registered_services__is_active=True),
                distinct=True,
            ),
            active_p_card_service_count=Count(
                "registered_services",
                filter=Q(registered_services__is_active=True, registered_services__uses_p_card=True),
                distinct=True,
            ),
            active_non_p_card_service_count=Count(
                "registered_services",
                filter=Q(registered_services__is_active=True, registered_services__uses_p_card=False),
                distinct=True,
            ),
            user_registered_count=Count(
                "registered_services",
                filter=Q(registered_services__registration_source__in=[ServiceRegistrationSource.USER, ServiceRegistrationSource.EXCEPTION_REQUEST]),
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
            Q(registration_source__in=[ServiceRegistrationSource.USER, ServiceRegistrationSource.EXCEPTION_REQUEST])
            | Q(deactivation_source=ServiceDeactivationSource.USER)
        ).order_by("-updated_at")

    active_catalog_assignments = (
        RegisteredService.objects.select_related("user")
        .filter(is_active=True, user__is_staff=False)
        .order_by("user__username", "user_id")
    )
    catalog_queryset = (
        ServiceCatalog.objects.annotate(
            assigned_count=Count("registered_services", distinct=True),
            active_user_count=Count(
                "registered_services__user",
                filter=Q(registered_services__is_active=True, registered_services__user__is_staff=False),
                distinct=True,
            ),
        )
        .prefetch_related(
            Prefetch(
                "registered_services",
                queryset=active_catalog_assignments,
                to_attr="active_user_assignments",
            )
        )
        .order_by("name", "billing_type")
    )
    catalog_paginator = Paginator(catalog_queryset, 25)
    catalog_page_obj = catalog_paginator.get_page(request.GET.get("catalog_page"))

    return render(
        request,
        "receipts/staff_services.html",
        {
            "active_tab": active_tab,
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
            messages.success(request, f"{managed_user.username} に {service.display_name} を登録しました。Pカード設定: {service.p_card_usage_label}。")
            return redirect("staff_user_services", user_id=managed_user.pk)
    else:
        form = StaffServiceForm(fixed_user=managed_user, registered_by=request.user)
    services = RegisteredService.objects.select_related("catalog_service", "registered_by", "deactivated_by").filter(user=managed_user).order_by("-is_active", "name", "billing_type")
    active_services = [service for service in services if service.is_active]
    active_p_card_count = sum(service.uses_p_card for service in active_services)
    active_non_p_card_count = len(active_services) - active_p_card_count
    return render(
        request,
        "receipts/staff_user_services.html",
        {
            "managed_user": managed_user,
            "services": services,
            "form": form,
            "active_p_card_count": active_p_card_count,
            "active_non_p_card_count": active_non_p_card_count,
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
            messages.success(request, f"{service.user.username} に {service.display_name} を登録しました。Pカード設定: {service.p_card_usage_label}。")
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
    previous_p_card_value = service.uses_p_card
    if request.method == "POST":
        form = StaffServiceForm(request.POST, instance=service, fixed_user=service.user, registered_by=request.user)
        if form.is_valid():
            service = form.save()
            if previous_p_card_value and not service.uses_p_card:
                ReceiptResubmissionRequest.objects.filter(
                    user=service.user,
                    service_name_snapshot=service.name,
                    billing_type_snapshot=service.billing_type,
                    is_extra=False,
                    status=ResubmissionRequestStatus.OPEN,
                ).update(
                    status=ResubmissionRequestStatus.RESOLVED,
                    resolved_at=timezone.now(),
                    resolved_by=request.user,
                )
            messages.success(
                request,
                f"{service.user.username} の {service.display_name} を更新しました。Pカード設定: {service.p_card_usage_label}。",
            )
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
        else (
            f"{reverse('dashboard')}?receipt_month="
            f"{month_query(receipt.submission.target_receipt_month)}"
        )
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

    if receipt.service_id:
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
    return redirect(
        f"{reverse('dashboard')}?receipt_month="
        f"{month_query(receipt_month_for_submission(selected_month))}"
    )


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
    if not receipt.can_request_resubmission:
        messages.error(request, "AIによるファイル名修正・検査が完了した領収書だけ再提出依頼を送信できます。")
        return redirect_back_or(request, fallback_url)

    with transaction.atomic():
        submission = receipt.submission
        selected_month = submission.period_month
        username = submission.user.get_username()
        service_name = receipt.service_display_name_snapshot
        receipt_context = service_name
        if receipt.is_extra and receipt.memo:
            receipt_context = f"{service_name}（{receipt.memo}）"
        display_filename = receipt.display_filename
        admin_reason = (request.POST.get("reason") or "").strip()[:1000]
        if not admin_reason:
            if receipt.ai_resubmission_recommendation_memo:
                recommendation = receipt.ai_resubmission_recommendation_memo.strip()
                # 管理者向けの説明文はユーザーへそのまま転記せず、「理由:」以降だけを利用する。
                if "理由:" in recommendation:
                    recommendation = recommendation.split("理由:", 1)[1].strip()
                # 監査用に追記された管理者判断メモはユーザー向け理由へ混ぜない。
                recommendation = recommendation.splitlines()[0].strip()
                admin_reason = recommendation[:1000]
            elif receipt.ai_unchecked_labels:
                admin_reason = f"管理者確認で未確認項目が残りました: {receipt.ai_unchecked_summary}"[:1000]
            else:
                admin_reason = "管理者が領収書本体を確認し、再提出が必要と判断しました"
        admin_reason = admin_reason.rstrip("。．. ")
        message = (
            f"管理者確認の結果、{selected_month:%Y年%m月}提出（対象領収書月: {receipt_month_for_submission(selected_month):%Y年%m月}）の {receipt_context} の領収書について再提出が必要になりました。"
            f"理由: {admin_reason}。"
            "該当ファイルは提出項目から削除済みです。正しい領収書ファイルを再度アップロードして、提出してください。"
        )
        request_item = ReceiptResubmissionRequest.objects.create(
            user=submission.user,
            period_month=selected_month,
            service_name_snapshot=receipt.service_name_snapshot,
            billing_type_snapshot=receipt.billing_type_snapshot,
            is_extra=receipt.is_extra,
            receipt_memo_snapshot=receipt.memo,
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

    email_log, email_sent = send_resubmission_request_email(request_item, created_by=request.user)
    success_message = (
        f"{username} / {service_name} に再提出を依頼しました。"
        "対象領収書は提出項目から削除され、再提出依頼管理へ登録されました。"
    )
    if email_sent:
        success_message += "ユーザーへ再提出依頼メールも送信しました。"
    messages.success(request, success_message)

    if email_log is None:
        messages.warning(request, "対象ユーザーに送信可能なメールアドレスがないため、メールは送信していません。アプリ内の再提出依頼は登録済みです。")
    elif email_log.status == EmailDeliveryStatus.SKIPPED:
        messages.warning(request, "対象ユーザーが停止中のため、再提出依頼メールは送信していません。アプリ内の再提出依頼は登録済みです。")
    elif email_log.status == EmailDeliveryStatus.FAILED:
        messages.error(request, "再提出依頼は登録しましたが、メール送信に失敗しました。メール送信ログを確認してください。")

    return redirect_back_or(request, fallback_url)


@staff_member_required
@require_POST
def staff_delete_resubmission_request(request, pk: int):
    """再提出依頼レコードとユーザー通知を削除する。

    依頼作成時に削除済みとなった元の領収書ファイルは復元しない。
    """

    request_item = get_object_or_404(
        ReceiptResubmissionRequest.objects.select_related("user"),
        pk=pk,
    )
    fallback_url = f"{reverse('history')}?month={month_query(request_item.period_month)}"
    user_label = request_item.user.get_username()
    service_label = request_item.service_display_name_snapshot
    request_item.delete()
    messages.success(
        request,
        f"{user_label} / {service_label} の再提出依頼を削除しました。ユーザー画面の再提出通知も削除されました。",
    )
    return redirect_back_or(request, fallback_url)


@staff_member_required
@require_POST
def staff_delete_resubmission_requests(request):
    """選択月または全期間の再提出依頼を一括削除する。"""

    fallback_url = reverse("history")
    scope = (request.POST.get("scope") or "").strip()
    queryset = ReceiptResubmissionRequest.objects.all()

    if scope == "month":
        month_form = MonthSelectForm({"month": request.POST.get("month")})
        if not month_form.is_valid():
            messages.error(request, "削除対象月を確認できませんでした。")
            return redirect_back_or(request, fallback_url)
        selected_month = month_form.cleaned_data["month"]
        queryset = queryset.filter(period_month=selected_month)
        fallback_url = f"{reverse('history')}?month={month_query(selected_month)}"
        scope_label = f"{selected_month:%Y年%m月}の"
    elif scope == "all":
        if request.POST.get("confirm_all") != "delete_all":
            messages.error(request, "全期間の削除確認が不足しています。")
            return redirect_back_or(request, fallback_url)
        scope_label = "全期間の"
    else:
        messages.error(request, "再提出依頼の削除範囲を確認できませんでした。")
        return redirect_back_or(request, fallback_url)

    count = queryset.count()
    queryset.delete()
    if count:
        messages.success(
            request,
            f"{scope_label}再提出依頼を{count}件削除しました。ユーザー画面の再提出通知も削除されました。",
        )
    else:
        messages.info(request, f"{scope_label}再提出依頼はありませんでした。")
    return redirect_back_or(request, fallback_url)


def staff_receipt_queryset():
    return Receipt.objects.select_related(
        "submission",
        "submission__user",
        "service",
        "service__catalog_service",
        "uploaded_by",
        "admin_reviewed_by",
    ).filter(
        submission__user__is_staff=False,
        submission__user__is_superuser=False,
    )


@staff_member_required
def staff_receipt_review(request, pk: int):
    """領収書をプレビューし、AI結果・チェック項目・表示ファイル名を管理者が確定する。"""

    receipt = get_object_or_404(staff_receipt_queryset(), pk=pk)
    form = StaffReceiptReviewForm(request.POST or None, receipt=receipt)
    if request.method == "POST":
        if receipt.ai_is_queued or receipt.is_ai_processing:
            messages.error(request, "AI抽出中は確認内容を変更できません。処理完了後に再度確認してください。")
        elif form.is_valid():
            confirm = request.POST.get("review_action") == "confirm"
            form.save(reviewed_by=request.user, confirm=confirm)
            if confirm:
                if receipt.ai_resubmission_recommended:
                    messages.success(request, f"{receipt.display_filename} を再提出不要として管理者確認済みにしました。")
                else:
                    messages.success(request, f"{receipt.display_filename} を管理者確認済みにしました。")
            else:
                messages.success(request, "確認内容とファイル名を保存しました。未チェック項目があるため、確認待ちのままです。")
            reconcile_card_statement_items_for_submission_month(receipt.submission.period_month)
            return redirect("staff_receipt_review", pk=receipt.pk)

    return render(
        request,
        "receipts/staff_receipt_review.html",
        {
            "receipt": receipt,
            "review_form": form,
        },
    )


def reconcile_card_statement_items_for_submission_month(period_month):
    """Reconcile every chronological statement affected by a receipt update.

    A refund PDF uploaded in one submission cycle can contain an original
    payment from an earlier month and a later refund.  All statements from the
    earliest represented financial-event month are first marked pending, then
    rebuilt in chronological fixed-point order.  This avoids leaving an earlier
    statement snapshot stale after a later statement changes global ownership.
    """

    target_month = receipt_month_for_submission(period_month)
    affected_months = {target_month}
    receipt_components = Receipt.objects.filter(
        submission__period_month=period_month,
        submission__user__is_staff=False,
        submission__user__is_superuser=False,
    ).values_list("financial_transaction_components", flat=True)
    for components in receipt_components:
        for component in components or []:
            if not isinstance(component, dict):
                continue
            raw_date = str(component.get("transaction_date") or "")[:10]
            try:
                event_date = date.fromisoformat(raw_date)
            except (TypeError, ValueError):
                continue
            affected_months.add(event_date.replace(day=1))

    earliest_month = min(affected_months)
    statements = list(
        CardStatement.objects.filter(period_month__gte=earliest_month)
        .exclude(status__in=[CardStatementStatus.PROCESSING, CardStatementStatus.FAILED])
        .only("pk", "period_month", "uploaded_at", "ai_admin_memo")
        .order_by("period_month", "uploaded_at", "pk")
    )
    if not statements:
        return 0

    # Reuse the public invalidation helper for the whole affected range. The
    # helper writes the global-usage marker and clears stale unused-file
    # snapshots without deleting current evidence rows.
    mark_card_statements_for_global_usage_refresh(period_month_gte=earliest_month)
    latest_month = max(statement.period_month for statement in statements)
    return reconcile_pending_card_statement_month_semantics(period_month=latest_month)


@staff_member_required
@require_POST
def staff_start_receipt_ai_processing(request, pk: int):
    receipt = get_object_or_404(staff_receipt_queryset(), pk=pk)
    force_reanalysis = request.POST.get("force_reanalysis") == "1"
    if force_reanalysis and not (receipt.ai_is_queued or receipt.is_ai_processing):
        # 通常の一括処理は従来どおりAI確認済みを再検査しない。
        # 管理者が領収書確認画面から明示的に実行した場合だけ、
        # 契約変更情報を含む最新スキーマでこの1件を再解析する。
        reset_ai_processing_state(receipt, save=True, clear_extracted_values=False)
    claimed_ids = claim_pending_receipts_for_ai_processing(
        Receipt.objects.filter(pk=receipt.pk).available_files(),
        limit=1,
    )
    if claimed_ids:
        start_background_ai_processing(claimed_ids)
        message = (
            "AIで契約変更情報を含めて再解析中です。完了後、この画面へ自動反映します。"
            if force_reanalysis
            else "AIで情報を抽出中です。完了後、この画面へ自動反映します。"
        )
    elif receipt.ai_is_queued or receipt.is_ai_processing:
        message = "この領収書はAIで情報を抽出中です。"
    else:
        message = "この領収書はすでにAI確認済みです。再検査せず、必要な補正は管理者確認欄で行ってください。"

    payload = {"ok": True, "started": bool(claimed_ids), "message": message}
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse(payload)
    if claimed_ids:
        messages.success(request, message)
    else:
        messages.info(request, message)
    return redirect("staff_receipt_review", pk=receipt.pk)


@staff_member_required
def staff_receipt_ai_status(request, pk: int):
    receipt = staff_receipt_queryset().filter(pk=pk).first()
    if receipt is None:
        resubmission = (
            ReceiptResubmissionRequest.objects.select_related("user")
            .filter(original_receipt_id=pk)
            .order_by("-created_at")
            .first()
        )
        redirect_url = reverse("history")
        if resubmission is not None:
            redirect_url = (
                f"{reverse('staff_user_month_status', args=[resubmission.user_id])}"
                f"?month={month_query(resubmission.period_month)}"
            )
        return JsonResponse(
            {
                "ok": True,
                "deleted": True,
                "redirect_url": redirect_url,
                "message": (
                    "管理者が再提出を依頼したため、領収書を提出項目から取り下げました。"
                    if resubmission is not None
                    else "領収書が削除されたため、一覧へ戻ります。"
                ),
            }
        )
    html = render_to_string(
        "receipts/_staff_receipt_review_panel.html",
        {
            "receipt": receipt,
            "review_form": StaffReceiptReviewForm(receipt=receipt),
        },
        request=request,
    )
    return JsonResponse(
        {
            "ok": True,
            "html": html,
            "processing": receipt.ai_is_queued or receipt.is_ai_processing,
            "status": receipt.ai_filename_status,
        }
    )


@staff_member_required
@xframe_options_sameorigin
def staff_preview_receipt(request, pk: int):
    receipt = get_object_or_404(staff_receipt_queryset(), pk=pk)
    if not receipt.file_available:
        raise Http404("保存期限が過ぎたか、領収書ファイルが削除済みです。")
    filename = receipt.display_filename or receipt.original_filename or Path(receipt.file.name).name
    content_type = {
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(Path(filename).suffix.lower(), "application/octet-stream")
    response = FileResponse(
        receipt.file.open("rb"),
        as_attachment=False,
        filename=filename,
        content_type=content_type,
    )
    response["X-Content-Type-Options"] = "nosniff"
    return response


@staff_member_required
@require_POST
def staff_replace_receipt_file(request, pk: int):
    receipt = get_object_or_404(staff_receipt_queryset(), pk=pk)
    form = ReceiptFileReplaceForm(request.POST, request.FILES)
    if not form.is_valid():
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, error)
        return redirect("staff_receipt_review", pk=receipt.pk)

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
    receipt.upload_source = ReceiptUploadSource.ADMIN
    receipt.uploaded_by = request.user
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
            "upload_source",
            "uploaded_by",
            *ai_reset_fields,
            "updated_at",
        ]
    )
    if old_storage is not None and old_file_name and old_file_name != receipt.file.name:
        try:
            if old_storage.exists(old_file_name):
                old_storage.delete(old_file_name)
        except Exception:
            pass
    reconcile_card_statement_items_for_submission_month(receipt.submission.period_month)
    messages.success(request, "領収書ファイルを差し替えました。AI確認は未確認へ戻ったため、再度実行してください。")
    return redirect("staff_receipt_review", pk=receipt.pk)



def parse_month_value(value):
    form = MonthSelectForm({"month": value})
    if form.is_valid():
        return form.cleaned_data["month"]
    raise Http404("月の指定が不正です。")


def staff_month_receipts_queryset(selected_month):
    user_ids = managed_users_queryset().values_list("id", flat=True)
    return (
        Receipt.objects.filter(submission__period_month=selected_month, submission__user_id__in=user_ids)
        .select_related("submission", "submission__user", "service", "uploaded_by", "admin_reviewed_by")
    )


STAFF_RECEIPT_STATUS_FILTERS = (
    ("all", "すべて"),
    ("resubmission_decision", "再提出判断待ち"),
    ("manual_review", "確認が必要"),
    ("ai_unprocessed", "AI未確認"),
)
STAFF_RECEIPT_STATUS_KEYS = {key for key, _label in STAFF_RECEIPT_STATUS_FILTERS}


def normalize_staff_receipt_status_filter(value: str | None) -> str:
    value = (value or "all").strip()
    return value if value in STAFF_RECEIPT_STATUS_KEYS else "all"


def build_staff_receipt_status_view(receipts, selected_status: str):
    """領収書を排他的な確認ステータスへ分類し、絞り込み用情報を返す。"""

    receipt_list = list(receipts)
    counts = {key: 0 for key, _label in STAFF_RECEIPT_STATUS_FILTERS}
    counts["all"] = len(receipt_list)
    for receipt in receipt_list:
        status_key = receipt.staff_review_status_key
        if status_key in counts:
            counts[status_key] += 1

    filtered_receipts = (
        receipt_list
        if selected_status == "all"
        else [receipt for receipt in receipt_list if receipt.staff_review_status_key == selected_status]
    )
    label_by_key = dict(STAFF_RECEIPT_STATUS_FILTERS)
    options = [
        {
            "key": key,
            "label": label,
            "count": counts[key],
            "active": key == selected_status,
        }
        for key, label in STAFF_RECEIPT_STATUS_FILTERS
    ]
    return {
        "receipts": filtered_receipts,
        "counts": counts,
        "options": options,
        "selected_key": selected_status,
        "selected_label": label_by_key[selected_status],
    }


def add_staff_receipt_status_counts_to_stats(stats: dict, counts: dict) -> dict:
    for key, count in counts.items():
        stats[f"receipt_status_count_{key}"] = count
    return stats


def staff_ai_summary_for_month(selected_month) -> dict:
    receipts = staff_month_receipts_queryset(selected_month)
    available = receipts.available_files()
    unresolved_available = available.filter(admin_review_status=ReceiptAdminReviewStatus.NOT_REVIEWED)
    manual_review_filter = (
        Q(ai_check_payee=False)
        | Q(ai_check_recipient_name=False)
        | Q(ai_check_service_payee_related=False)
        | Q(ai_check_date=False)
        | Q(ai_check_amount=False)
        | Q(ai_check_currency=False)
        | Q(ai_check_period_match=False)
    )
    return {
        "ai_ready_count": available.filter(ai_filename_status=ReceiptFilenameStatus.NOT_PROCESSED).count(),
        "ai_pending_count": available.filter(ai_filename_status=ReceiptFilenameStatus.NOT_PROCESSED).count(),
        "ai_processing_count": available.filter(
            ai_filename_status__in=[ReceiptFilenameStatus.QUEUED, ReceiptFilenameStatus.PROCESSING]
        ).count(),
        "ai_queued_count": available.filter(ai_filename_status=ReceiptFilenameStatus.QUEUED).count(),
        "ai_review_count": unresolved_available.filter(
            ai_filename_status__in=[ReceiptFilenameStatus.NEEDS_REVIEW, ReceiptFilenameStatus.FAILED]
        ).count(),
        "period_mismatch_count": unresolved_available.filter(
            ai_period_check_status=ReceiptPeriodCheckStatus.MISMATCHED
        ).count(),
        "manual_review_count": unresolved_available.filter(
            ai_filename_checked_at__isnull=False
        ).filter(manual_review_filter).count(),
        "recipient_name_review_count": unresolved_available.filter(
            ai_filename_checked_at__isnull=False,
            ai_check_recipient_name=False,
        ).count(),
        "service_payee_review_count": unresolved_available.filter(
            ai_filename_checked_at__isnull=False,
            ai_check_service_payee_related=False,
        ).count(),
        "resubmission_decision_count": available.filter(
            ai_resubmission_recommended=True,
            admin_review_status=ReceiptAdminReviewStatus.NOT_REVIEWED,
        ).count(),
    }



@staff_member_required
@require_POST
def staff_start_ai_processing(request):
    selected_month = parse_month_value(request.POST.get("month"))
    selected_receipt_status = normalize_staff_receipt_status_filter(
        request.POST.get("receipt_status")
    )
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

    status_view = build_staff_receipt_status_view(
        staff_month_receipts_queryset(selected_month).order_by("-uploaded_at", "-pk"),
        selected_receipt_status,
    )
    stats = staff_ai_summary_for_month(selected_month)
    add_staff_receipt_status_counts_to_stats(stats, status_view["counts"])
    payload = {
        "ok": True,
        "started_count": len(claimed_ids),
        "message": message,
        "stats": stats,
    }
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse(payload)

    if claimed_ids:
        messages.success(request, message)
    else:
        messages.info(request, message)
    return redirect(
        f"{reverse('history')}?month={month_query(selected_month)}"
        f"&receipt_status={selected_receipt_status}#uploaded-receipts"
    )


@staff_member_required
def staff_ai_processing_status(request):
    selected_month = parse_month_value(request.GET.get("month"))
    selected_receipt_status = normalize_staff_receipt_status_filter(
        request.GET.get("receipt_status")
    )
    receipt_status_view = build_staff_receipt_status_view(
        staff_month_receipts_queryset(selected_month).order_by("-uploaded_at", "-pk"),
        selected_receipt_status,
    )
    stats = staff_ai_summary_for_month(selected_month)
    add_staff_receipt_status_counts_to_stats(stats, receipt_status_view["counts"])
    receipts_html = render_to_string(
        "receipts/_staff_receipt_rows.html",
        {
            "receipts": receipt_status_view["receipts"],
            "selected_receipt_status": receipt_status_view["selected_key"],
            "selected_receipt_status_label": receipt_status_view["selected_label"],
        },
        request=request,
    )
    return JsonResponse(
        {
            "ok": True,
            "receipts_html": receipts_html,
            "stats": stats,
            "visible_count": len(receipt_status_view["receipts"]),
            "processing_count": stats["ai_processing_count"],
            "processable_count": stats["ai_ready_count"],
            "done": stats["ai_processing_count"] == 0,
        }
    )



@staff_member_required
def staff_user_month_status(request, user_id: int):
    managed_user = get_managed_user(user_id)
    if request.method == "POST":
        selected_month = parse_month_value(request.POST.get("month"))
        month_form = MonthSelectForm(initial={"month": selected_month})
    else:
        selected_month, month_form = parse_month_from_request(request)

    # 管理者の月別状況・代理アップロード画面では、選択した提出サイクル月に
    # 対応する前月の領収書発行月を表示する。v1.15.0のUI整理時にこの代入が
    # 抜け、render contextで未定義変数を参照して500になっていた。
    target_receipt_month = receipt_month_for_submission(selected_month)

    selected_upload_choice = (
        request.POST.get("service", "")
        if request.method == "POST"
        else request.GET.get("service", "")
    )
    staff_upload_form = ReceiptBatchUploadForm(
        request.POST or None,
        request.FILES or None,
        user=managed_user,
        period_month=selected_month,
        selected_choice=selected_upload_choice,
        hide_file_input=True,
    )

    if request.method == "POST":
        action = request.POST.get("action")
        if action != "staff_add_receipts":
            messages.error(request, "不明な操作です。")
        elif staff_upload_form.is_valid():
            submission, _ = Submission.objects.get_or_create(
                user=managed_user,
                period_month=selected_month,
            )
            try:
                created_receipts, was_submitted, resolved_count = save_receipt_batch(
                    submission=submission,
                    form=staff_upload_form,
                    uploaded_by=request.user,
                )
            except ValidationError as exc:
                add_validation_errors(staff_upload_form, exc)
            except Exception:
                logger.exception(
                    "Staff proxy receipt upload failed staff=%s target_user=%s period_month=%s",
                    request.user.pk,
                    managed_user.pk,
                    selected_month,
                )
                messages.error(
                    request,
                    "領収書ファイルの代理保存に失敗しました。ファイルは登録されていません。"
                    "RailwayのDeploy Logsで『Staff proxy receipt upload failed』直後のエラーを確認してください。",
                )
            else:
                count = len(created_receipts)
                selected_label = (
                    "その他"
                    if staff_upload_form.is_extra
                    else staff_upload_form.selected_service.display_name
                )
                if staff_upload_form.is_extra:
                    total_for_selection = Receipt.objects.filter(
                        submission=submission,
                        is_extra=True,
                        file_deleted_at__isnull=True,
                    ).exclude(file="").count()
                else:
                    total_for_selection = Receipt.objects.filter(
                        submission=submission,
                        service=staff_upload_form.selected_service,
                        file_deleted_at__isnull=True,
                    ).exclude(file="").count()
                messages.success(
                    request,
                    f"{managed_user.username} の {selected_month:%Y年%m月}提出（対象領収書月: {receipt_month_for_submission(selected_month):%Y年%m月}）へ、{selected_label} の領収書を新しく{count}件代理アップロードしました。現在の登録件数は計{total_for_selection}件です。",
                )
                if resolved_count:
                    messages.success(request, f"再提出依頼を{resolved_count}件、対応済みにしました。")
                if was_submitted:
                    messages.warning(
                        request,
                        "提出済みの月へ領収書を追加したため、「未提出領収書あり」に戻しました。ユーザーに内容確認と再提出を依頼してください。",
                    )
                selected_value = (
                    ReceiptBatchUploadForm.OTHER_VALUE
                    if staff_upload_form.is_extra
                    else str(staff_upload_form.selected_service.pk)
                )
                return redirect(
                    f"{reverse('staff_user_month_status', args=[managed_user.pk])}"
                    f"?month={month_query(selected_month)}&service={selected_value}&uploaded={count}#staff-receipt-upload"
                )

    proxy_users = list(managed_users_queryset())
    proxy_user_index = next(
        (index for index, account in enumerate(proxy_users) if account.pk == managed_user.pk),
        0,
    )
    previous_proxy_user = proxy_users[proxy_user_index - 1] if proxy_user_index > 0 else None
    next_proxy_user = (
        proxy_users[proxy_user_index + 1]
        if proxy_user_index + 1 < len(proxy_users)
        else None
    )
    try:
        proxy_upload_completed_count = max(int(request.GET.get("uploaded", "0")), 0)
    except (TypeError, ValueError):
        proxy_upload_completed_count = 0

    monthly_summary = build_user_month_summary(managed_user, selected_month)
    available_services = RegisteredService.objects.uploadable_for(managed_user, selected_month).select_related("catalog_service")
    submission = (
        Submission.objects.filter(user=managed_user, period_month=selected_month)
        .prefetch_related(
            Prefetch(
                "receipts",
                queryset=Receipt.objects.select_related("service", "uploaded_by", "admin_reviewed_by").order_by("uploaded_at", "pk"),
            )
        )
        .first()
    )
    has_available_receipts = bool(
        submission
        and any(receipt.file_available for receipt in submission.receipts.all())
    )
    has_extra_receipts = bool(
        submission
        and any(receipt.file_available and receipt.is_extra for receipt in submission.receipts.all())
    )
    has_open_resubmission_requests = ReceiptResubmissionRequest.objects.filter(
        user=managed_user,
        period_month=selected_month,
        status=ResubmissionRequestStatus.OPEN,
    ).exists()
    display_status_label, display_status_class = month_submission_display_status(
        submission,
        monthly_summary,
        has_available_receipts=(
            has_available_receipts if monthly_summary.total_services else has_extra_receipts
        ),
        has_open_resubmission_requests=has_open_resubmission_requests,
    )
    global_statement_count = CardStatement.objects.filter(period_month=target_receipt_month).count()
    return render(
        request,
        "receipts/staff_user_month_status.html",
        {
            "managed_user": managed_user,
            "selected_month": selected_month,
            "target_receipt_month": target_receipt_month,
            "month_form": month_form,
            "monthly_summary": monthly_summary,
            "available_services": available_services,
            "submission": submission,
            "global_statement_count": global_statement_count,
            "staff_upload_form": staff_upload_form,
            "selected_upload_choice": selected_upload_choice,
            "proxy_users": proxy_users,
            "proxy_user_position": proxy_user_index + 1,
            "proxy_user_total": len(proxy_users),
            "previous_proxy_user": previous_proxy_user,
            "next_proxy_user": next_proxy_user,
            "proxy_upload_completed_count": proxy_upload_completed_count,
            "display_status_label": display_status_label,
            "display_status_class": display_status_class,
        },
    )


STATEMENT_RESULT_FILTERS = {
    "all": "すべて",
    "unmatched": "未一致",
    "needs_review": "解析要確認",
}
STATEMENT_RESULT_VISIBLE_FILTER_KEYS = ("unmatched", "needs_review")


def normalize_statement_result_filter(value: str | None) -> str:
    value = (value or "all").strip().lower()
    # v1.15.0までの「推定対応」リンクは、統合後の「未一致」へ寄せる。
    if value == "inferred":
        return "unmatched"
    return value if value in STATEMENT_RESULT_FILTERS else "all"


def build_statement_result_filter_options(counts: dict[str, int], current: str) -> list[dict[str, object]]:
    return [
        {
            "key": key,
            "label": STATEMENT_RESULT_FILTERS[key],
            "count": counts.get(key, 0),
            "active": key == current,
            # 「すべて」ボタンは置かず、選択中のボタンを再度押すと解除する。
            "target_key": "all" if key == current else key,
        }
        for key in STATEMENT_RESULT_VISIBLE_FILTER_KEYS
    ]


def prepare_statement_result_display(statements, result_filter: str):
    # `unmatched` は管理者が確認したい3区分をまとめる。
    #   1. 契約変更等による推定対応
    #   2. 直接・推定ともに根拠がない未一致
    #   3. 全カード明細を通じて未消費の構成要素が残る提出書類
    counts = {
        "all": 0,
        "unmatched": 0,
        "unmatched_only": 0,
        "inferred": 0,
        "needs_review": 0,
        "unused_receipts": 0,
    }

    for statement in statements:
        all_items = list(statement.items.all())
        inferred_items = [
            item
            for item in all_items
            if item.receipt_required and item.match_status == StatementMatchStatus.INFERRED
        ]
        unmatched_items = [
            item
            for item in all_items
            if item.receipt_required and item.match_status == StatementMatchStatus.UNMATCHED
        ]
        review_items = [
            item
            for item in all_items
            if item.receipt_required and item.match_status == StatementMatchStatus.NEEDS_REVIEW
        ]
        unused_receipts = list(statement.unmatched_receipt_components or [])

        counts["inferred"] += len(inferred_items)
        counts["unmatched_only"] += len(unmatched_items)
        counts["needs_review"] += len(review_items)
        counts["unused_receipts"] += len(unused_receipts)
        counts["unmatched"] += len(inferred_items) + len(unmatched_items) + len(unused_receipts)
        counts["all"] += len(all_items) + len(unused_receipts)

        statement.display_inferred_items = inferred_items
        statement.display_unmatched_items = unmatched_items
        statement.display_unused_receipts = (
            unused_receipts if result_filter in {"all", "unmatched"} else []
        )

        if result_filter == "unmatched":
            # 未一致画面では個別グループをテンプレート側で描画する。
            statement.display_items = []
            statement.display_item_count = (
                len(inferred_items) + len(unmatched_items) + len(unused_receipts)
            )
        elif result_filter == "needs_review":
            statement.display_items = review_items
            statement.display_item_count = len(review_items)
        else:
            statement.display_items = all_items
            statement.display_item_count = len(all_items) + len(unused_receipts)
    return counts


def global_statement_queryset(period_month):
    evidence_queryset = CardStatementReceiptEvidence.objects.select_related(
        "receipt",
        "receipt__submission__user",
        "receipt__service__catalog_service",
    ).order_by("sequence", "pk")
    item_queryset = (
        CardStatementItem.objects.select_related(
            "matched_user",
            "matched_catalog_service",
            "matched_service__user",
            "matched_service__catalog_service",
            "matched_receipt__submission__user",
            "matched_receipt__service__catalog_service",
            "plan_change_inference__user",
            "plan_change_inference__change_receipt__submission__user",
            "plan_change_inference__change_receipt__service__catalog_service",
            "plan_change_inference__historical_receipt__submission__user",
            "plan_change_inference__historical_receipt__service__catalog_service",
            "plan_change_inference__reviewed_by",
        )
        .prefetch_related(Prefetch("receipt_evidences", queryset=evidence_queryset))
        .order_by("sequence", "pk")
    )
    return (
        CardStatement.objects.filter(period_month=period_month)
        .select_related("uploaded_by")
        .prefetch_related(Prefetch("items", queryset=item_queryset))
        .order_by("-uploaded_at", "-pk")
    )


@staff_member_required
def staff_card_statements(request):
    selected_month, month_form = parse_statement_month_from_request(request)
    reconcile_pending_card_statement_month_semantics(period_month=selected_month)
    # 全社明細月と領収書発行月は同じ月。対象ファイルは翌月の提出サイクルに保存される。
    result_filter = normalize_statement_result_filter(request.GET.get("result"))
    statement_queryset = global_statement_queryset(selected_month)
    statements = list(statement_queryset)
    result_filter_counts = prepare_statement_result_display(statements, result_filter)
    stats = {
        "line_count": sum(statement.items.count() for statement in statements),
        "unresolved_count": sum(statement.unresolved_count for statement in statements),
        "review_count": sum(statement.manual_review_count for statement in statements),
        "unused_receipt_count": sum(len(statement.unmatched_receipt_components or []) for statement in statements),
    }
    return render(
        request,
        "receipts/staff_card_statements.html",
        {
            "selected_month": selected_month,
            "target_receipt_month": receipt_month_for_statement(selected_month),
            "month_form": month_form,
            "statements": statements,
            "statement_form": CardStatementUploadForm(),
            "target_card_last4": target_card_last4(),
            "stats": stats,
            "result_filter": result_filter,
            "result_filter_label": STATEMENT_RESULT_FILTERS[result_filter],
            "result_filter_counts": result_filter_counts,
            "result_filter_count": result_filter_counts.get(result_filter, result_filter_counts.get("all", 0)),
            "statement_result_filter_options": build_statement_result_filter_options(
                result_filter_counts, result_filter
            ),
        },
    )


@staff_member_required
@require_POST
def staff_upload_card_statement(request):
    selected_month = parse_month_value(request.POST.get("month"))
    form = CardStatementUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, error)
        return redirect(f"{reverse('staff_card_statements')}?month={month_query(selected_month)}")

    upload = form.cleaned_data["file"]
    statement = form.save(commit=False)
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
        messages.success(
            request,
            f"全ユーザー共通のご利用代金明細書をアップロードしました。AIで全明細行を抽出し、"
            f"ご利用代金明細月 {selected_month:%Y年%m月} と同じ領収書発行月 "
            f"{receipt_month_for_statement(selected_month):%Y年%m月} として登録された全ユーザー領収書と照合しています。",
        )
    return redirect(f"{reverse('staff_card_statements')}?month={month_query(selected_month)}")


@staff_member_required
def staff_card_statement_status(request):
    selected_month = parse_month_value(request.GET.get("month"))
    reconcile_pending_card_statement_month_semantics(period_month=selected_month)
    result_filter = normalize_statement_result_filter(request.GET.get("result"))
    statements = list(global_statement_queryset(selected_month))
    result_filter_counts = prepare_statement_result_display(statements, result_filter)
    html = render_to_string(
        "receipts/_staff_card_statements.html",
        {
            "selected_month": selected_month,
            "target_receipt_month": receipt_month_for_statement(selected_month),
            "statements": statements,
            "target_card_last4": target_card_last4(),
            "result_filter": result_filter,
            "result_filter_label": STATEMENT_RESULT_FILTERS[result_filter],
            "result_filter_counts": result_filter_counts,
            "result_filter_count": result_filter_counts.get(result_filter, result_filter_counts.get("all", 0)),
            "statement_result_filter_options": build_statement_result_filter_options(
                result_filter_counts, result_filter
            ),
        },
        request=request,
    )
    processing_count = sum(1 for statement in statements if statement.status == CardStatementStatus.PROCESSING)
    return JsonResponse({"ok": True, "html": html, "processing_count": processing_count, "done": processing_count == 0})


@staff_member_required
def staff_download_card_statement(request, pk: int):
    statement = get_object_or_404(CardStatement, pk=pk)
    if not statement.file_available:
        raise Http404("保存期限が過ぎたか、明細ファイルが削除済みです。")
    filename = statement.original_filename or Path(statement.file.name).name
    return FileResponse(statement.file.open("rb"), as_attachment=True, filename=filename)


@staff_member_required
def staff_download_card_statement_report(request, pk: int):
    base_statement = get_object_or_404(CardStatement, pk=pk)
    # A direct report download must settle any earlier pending ownership changes
    # first; otherwise the selected statement could be exported with a component
    # that an older statement is about to reclaim.
    reconcile_pending_card_statement_month_semantics(period_month=base_statement.period_month)
    statement = get_object_or_404(global_statement_queryset(base_statement.period_month), pk=pk)
    if statement.status in {CardStatementStatus.PROCESSING, CardStatementStatus.FAILED}:
        raise Http404("AI解析・照合が完了してから照合結果PDFをダウンロードしてください。")
    if not statement.items.exists():
        raise Http404("PDFに出力できる照合結果がありません。")

    pdf_bytes = build_card_statement_reconciliation_pdf(statement)
    return FileResponse(
        BytesIO(pdf_bytes),
        as_attachment=True,
        filename=reconciliation_report_filename(statement),
        content_type="application/pdf",
    )


@staff_member_required
@require_POST
def staff_delete_card_statement(request, pk: int):
    statement = get_object_or_404(CardStatement, pk=pk)
    selected_month = statement.period_month
    result_filter = normalize_statement_result_filter(request.POST.get("result"))
    filename = statement.original_filename or "ご利用代金明細書"
    # Deleting an earlier statement releases its consumed evidence. Queue every
    # downstream statement so the newly available components and unused-file
    # snapshots are rebuilt chronologically.
    mark_later_card_statements_for_global_usage_refresh(statement)
    statement.delete()
    messages.success(request, f"{filename} と全ユーザー照合履歴を削除しました。")
    return redirect(
        f"{reverse('staff_card_statements')}?month={month_query(selected_month)}&result={result_filter}"
    )


@staff_member_required
@require_POST
def staff_reconcile_card_statement(request, pk: int):
    statement = get_object_or_404(CardStatement, pk=pk)
    result_filter = normalize_statement_result_filter(request.POST.get("result"))
    if statement.status == CardStatementStatus.PROCESSING:
        messages.info(request, "AI解析中のため、完了後に再照合してください。")
    elif statement.status == CardStatementStatus.FAILED:
        messages.error(request, "AI解析に失敗した明細書は再照合できません。明細書を削除して再アップロードしてください。")
    else:
        reconcile_card_statement_items(statement.pk)
        messages.success(request, "対象領収書月に提出された領収書と再照合しました。")
    return redirect(
        f"{reverse('staff_card_statements')}?month={month_query(statement.period_month)}&result={result_filter}"
        f"#statement-{statement.pk}"
    )


@staff_member_required
@require_POST
def staff_update_statement_item(request, pk: int):
    item = get_object_or_404(
        CardStatementItem.objects.select_related(
            "statement",
            "matched_receipt",
            "plan_change_inference__user",
            "plan_change_inference__change_receipt__submission__user",
            "plan_change_inference__change_receipt__service__catalog_service",
            "plan_change_inference__historical_receipt__submission__user",
            "plan_change_inference__historical_receipt__service__catalog_service",
            "plan_change_inference__reviewed_by",
        ),
        pk=pk,
    )
    action = request.POST.get("item_action") or "required"
    result_filter = normalize_statement_result_filter(request.POST.get("result"))
    redirect_url = (
        f"{reverse('staff_card_statements')}?month={month_query(item.statement.period_month)}&result={result_filter}"
        f"#statement-{item.statement_id}"
    )

    inference = getattr(item, "plan_change_inference", None)
    old_statement_consume_fingerprints = set(
        CardStatementReceiptEvidence.objects.filter(
            statement_item__statement=item.statement,
            usage_mode=StatementReceiptEvidenceUsageMode.CONSUME,
        )
        .exclude(component_fingerprint="")
        .values_list("component_fingerprint", flat=True)
    )

    if action == "confirm_plan_change":
        if inference is None or inference.status != PlanChangeInferenceStatus.PENDING:
            messages.error(request, "確認待ちの契約変更推定がありません。最新の領収書と再照合してください。")
            return redirect(redirect_url)
        service = None
        if inference.historical_receipt_id and inference.historical_receipt:
            service = inference.historical_receipt.service
        if service is None and inference.change_receipt_id and inference.change_receipt:
            service = inference.change_receipt.service
        with transaction.atomic():
            inference.status = PlanChangeInferenceStatus.CONFIRMED
            inference.reviewed_by = request.user
            inference.reviewed_at = timezone.now()
            inference.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])

            item.receipt_required = True
            item.matched_user = inference.user
            item.matched_service = service
            item.matched_catalog_service = (
                service.catalog_service if service is not None and service.catalog_service_id else None
            )
            item.matched_receipt = None
            item.match_status = StatementMatchStatus.MATCHED
            item.match_reason_code = StatementMatchReason.PLAN_CHANGE_CONFIRMED
            item.match_confidence = 1.0
            item.match_memo = (
                "管理者確認: 契約変更書類と過去の定期契約実績による推定対応を一致として確定しました。 "
                + inference.reason
            )[:4000]
            item.save(update_fields=[
                "receipt_required", "matched_user", "matched_service", "matched_catalog_service",
                "matched_receipt", "match_status", "match_reason_code", "match_confidence", "match_memo",
            ])
            CardStatementReceiptEvidence.objects.filter(statement_item=item).delete()
        reconcile_card_statement_items(item.statement_id, preserve_manual=True)
        messages.success(
            request,
            f"明細 {item.line_reference or item.pk} の契約変更推定を一致として確定しました。",
        )
    elif action == "reject_plan_change":
        if inference is None or inference.status != PlanChangeInferenceStatus.PENDING:
            messages.error(request, "確認待ちの契約変更推定がありません。最新の領収書と再照合してください。")
            return redirect(redirect_url)
        with transaction.atomic():
            inference.status = PlanChangeInferenceStatus.REJECTED
            inference.reviewed_by = request.user
            inference.reviewed_at = timezone.now()
            inference.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])

            item.receipt_required = True
            item.matched_user = None
            item.matched_catalog_service = None
            item.matched_service = None
            item.matched_receipt = None
            item.match_status = StatementMatchStatus.UNMATCHED
            item.match_reason_code = StatementMatchReason.NO_COMPATIBLE_RECEIPT
            item.match_confidence = 0.0
            item.match_memo = (
                "管理者が契約変更情報による推定対応を採用せず、未一致として確定しました。 "
                + inference.reason
            )[:4000]
            item.save(update_fields=[
                "receipt_required", "matched_user", "matched_catalog_service", "matched_service",
                "matched_receipt", "match_status", "match_reason_code", "match_confidence", "match_memo",
            ])
            CardStatementReceiptEvidence.objects.filter(statement_item=item).delete()
        reconcile_card_statement_items(item.statement_id, preserve_manual=True)
        messages.success(
            request,
            f"明細 {item.line_reference or item.pk} の推定を採用せず、未一致として確定しました。",
        )
    elif action == "ignore":
        item.matched_user = None
        item.matched_catalog_service = None
        item.matched_service = None
        item.matched_receipt = None
        item.match_status = StatementMatchStatus.IGNORED
        item.match_reason_code = StatementMatchReason.IGNORED
        item.receipt_required = False
        item.match_confidence = 1.0
        item.match_memo = "管理者確認により領収書管理対象外としました。"
        item.save(
            update_fields=[
                "matched_user",
                "matched_catalog_service",
                "matched_service",
                "matched_receipt",
                "match_status",
                "match_reason_code",
                "receipt_required",
                "match_confidence",
                "match_memo",
            ]
        )
        CardStatementReceiptEvidence.objects.filter(statement_item=item).delete()
        CardStatementPlanChangeInference.objects.filter(statement_item=item).delete()
        reconcile_card_statement_items(item.statement_id, preserve_manual=True)
        messages.success(request, f"明細 {item.line_reference or item.pk} を領収書管理対象外にしました。")
    elif action == "required":
        item.receipt_required = True
        item.match_status = StatementMatchStatus.UNMATCHED
        item.match_reason_code = StatementMatchReason.NO_COMPATIBLE_RECEIPT
        item.match_confidence = 0.0
        item.match_memo = "領収書照合対象へ戻しました。"
        item.save(
            update_fields=[
                "receipt_required",
                "match_status",
                "match_reason_code",
                "match_confidence",
                "match_memo",
            ]
        )
        CardStatementReceiptEvidence.objects.filter(statement_item=item).delete()
        CardStatementPlanChangeInference.objects.filter(statement_item=item).delete()
        reconcile_card_statement_items(item.statement_id, preserve_manual=True)
        messages.success(request, f"明細 {item.line_reference or item.pk} を領収書照合対象へ戻しました。")
    else:
        messages.error(request, "この操作は廃止されました。最新の領収書と再照合してください。")

    new_statement_consume_fingerprints = set(
        CardStatementReceiptEvidence.objects.filter(
            statement_item__statement=item.statement,
            usage_mode=StatementReceiptEvidenceUsageMode.CONSUME,
        )
        .exclude(component_fingerprint="")
        .values_list("component_fingerprint", flat=True)
    )
    if old_statement_consume_fingerprints != new_statement_consume_fingerprints:
        # The item action may delete its evidence before the statement-level
        # reconciler can observe the old set. Explicitly invalidate every other
        # statement so a released component can be reclaimed and all global
        # unused snapshots are refreshed.
        mark_later_card_statements_for_global_usage_refresh(item.statement)

    return redirect(redirect_url)



def staff_history(request):
    target_receipt_month, selected_month, month_form = parse_receipt_month_from_request(request)
    users = list(
        managed_users_queryset().annotate(
            active_service_count=Count(
                "registered_services",
                filter=Q(registered_services__is_active=True),
                distinct=True,
            ),
            active_p_card_service_count=Count(
                "registered_services",
                filter=Q(registered_services__is_active=True, registered_services__uses_p_card=True),
                distinct=True,
            ),
            active_non_p_card_service_count=Count(
                "registered_services",
                filter=Q(registered_services__is_active=True, registered_services__uses_p_card=False),
                distinct=True,
            ),
            user_registered_count=Count(
                "registered_services",
                filter=Q(registered_services__registration_source__in=[ServiceRegistrationSource.USER, ServiceRegistrationSource.EXCEPTION_REQUEST]),
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
    open_resubmission_user_ids = set(
        ReceiptResubmissionRequest.objects.filter(
            period_month=selected_month,
            status=ResubmissionRequestStatus.OPEN,
            user_id__in=user_ids,
        ).values_list("user_id", flat=True)
    )

    rows = []
    for user in users:
        submission = submissions_by_user.get(user.id)
        summary = build_user_month_summary(user, selected_month)
        submission_receipts = list(submission.receipts.all()) if submission is not None else []
        receipt_count = len(submission_receipts)
        available_file_count = sum(1 for receipt in submission_receipts if receipt.file_available)
        extra_file_count = sum(
            1 for receipt in submission_receipts if receipt.file_available and receipt.is_extra
        )
        status, status_class = month_submission_display_status(
            submission,
            summary,
            has_available_receipts=(
                available_file_count > 0 if summary.total_services else extra_file_count > 0
            ),
            has_open_resubmission_requests=user.pk in open_resubmission_user_ids,
        )
        purged_file_count = sum(1 for receipt in submission_receipts if receipt.file_deleted_at)
        manual_review_count = sum(1 for receipt in submission_receipts if receipt.needs_manual_review)
        rows.append(
            {
                "user": user,
                "submission": submission,
                "status": status,
                "status_class": status_class,
                "receipt_count": receipt_count,
                "available_file_count": available_file_count,
                "purged_file_count": purged_file_count,
                "active_service_count": user.active_service_count,
                "active_p_card_service_count": user.active_p_card_service_count,
                "active_non_p_card_service_count": user.active_non_p_card_service_count,
                "user_registered_count": user.user_registered_count,
                "user_stopped_count": user.user_stopped_count,
                "monthly_summary": summary,
                "manual_review_count": manual_review_count,
                "needs_attention": summary.missing_required_count > 0 or summary.api_pending_count > 0 or manual_review_count > 0,
            }
        )

    selected_receipt_status = normalize_staff_receipt_status_filter(
        request.GET.get("receipt_status")
    )
    receipt_status_view = build_staff_receipt_status_view(
        staff_month_receipts_queryset(selected_month).order_by("-uploaded_at", "-pk"),
        selected_receipt_status,
    )
    receipts = receipt_status_view["receipts"]
    ai_stats = staff_ai_summary_for_month(selected_month)
    month_resubmission_requests = (
        ReceiptResubmissionRequest.objects.filter(period_month=selected_month)
        .select_related("user", "created_by")
        .order_by("status", "user__username", "service_name_snapshot", "created_at")
    )
    open_resubmission_request_count = month_resubmission_requests.filter(
        status=ResubmissionRequestStatus.OPEN
    ).count()
    all_resubmission_request_count = ReceiptResubmissionRequest.objects.count()
    stats = {
        "total_users": len(users),
        "active_users": sum(1 for user in users if getattr(user.profile, "account_status", None) == UserAccountStatus.ACTIVE),
        "submitted": sum(1 for row in rows if row["status"] == "提出済み"),
        "draft": sum(1 for row in rows if row["status"] == SubmissionStatus.DRAFT.label),
        "not_started": sum(1 for row in rows if row["status"] == "未着手"),
        "not_required": sum(1 for row in rows if row["status"] == "提出不要"),
        "incomplete_users": sum(1 for row in rows if not row["monthly_summary"].is_complete),
        "missing_service_count": sum(row["monthly_summary"].missing_required_count for row in rows),
        "api_pending_count": sum(row["monthly_summary"].api_pending_count for row in rows),
        "api_pending_user_count": sum(1 for row in rows if row["monthly_summary"].api_pending_count > 0),
        "receipt_count": sum(row["receipt_count"] for row in rows),
        "available_file_count": sum(row["available_file_count"] for row in rows),
        "active_service_count": sum(row["active_service_count"] for row in rows),
        "active_p_card_service_count": sum(row["active_p_card_service_count"] for row in rows),
        "active_non_p_card_service_count": sum(row["active_non_p_card_service_count"] for row in rows),
        "user_registered_count": sum(row["user_registered_count"] for row in rows),
        "user_stopped_count": sum(row["user_stopped_count"] for row in rows),
        "ai_pending_count": ai_stats["ai_pending_count"],
        "ai_ready_count": ai_stats["ai_ready_count"],
        "ai_queued_count": ai_stats["ai_queued_count"],
        "ai_processing_count": ai_stats["ai_processing_count"],
        "ai_review_count": ai_stats["ai_review_count"],
        "period_mismatch_count": ai_stats["period_mismatch_count"],
        "manual_review_count": ai_stats["manual_review_count"],
        "recipient_name_review_count": ai_stats["recipient_name_review_count"],
        "service_payee_review_count": ai_stats["service_payee_review_count"],
        "resubmission_decision_count": ai_stats["resubmission_decision_count"],
        "open_resubmission_request_count": open_resubmission_request_count,
    }
    add_staff_receipt_status_counts_to_stats(stats, receipt_status_view["counts"])
    return render(
        request,
        "receipts/staff_history.html",
        {
            "rows": rows,
            "stats": stats,
            "month_form": month_form,
            "selected_month": selected_month,
            "target_receipt_month": target_receipt_month,
            "receipts": receipts,
            "receipt_status_options": receipt_status_view["options"],
            "selected_receipt_status": receipt_status_view["selected_key"],
            "selected_receipt_status_label": receipt_status_view["selected_label"],
            "month_resubmission_requests": month_resubmission_requests,
            "month_resubmission_request_count": month_resubmission_requests.count(),
            "all_resubmission_request_count": all_resubmission_request_count,
        },
    )


@login_required
def history(request):
    if request.user.is_staff:
        return staff_history(request)
    submissions = list(
        Submission.objects.filter(user=request.user)
        .prefetch_related("receipts")
        .order_by("-period_month", "-created_at")
    )
    rows = []
    for submission in submissions:
        summary = build_user_month_summary(request.user, submission.period_month)
        remaining_service_count = summary.missing_required_count + summary.api_pending_count
        submission_receipts = list(submission.receipts.all())
        has_available_receipts = any(receipt.file_available for receipt in submission_receipts)
        has_extra_receipts = any(
            receipt.file_available and receipt.is_extra for receipt in submission_receipts
        )
        has_open_resubmission_requests = ReceiptResubmissionRequest.objects.filter(
            user=request.user,
            period_month=submission.period_month,
            status=ResubmissionRequestStatus.OPEN,
        ).exists()
        status_label, status_class = month_submission_display_status(
            submission,
            summary,
            has_available_receipts=(
                has_available_receipts if summary.total_services else has_extra_receipts
            ),
            has_open_resubmission_requests=has_open_resubmission_requests,
        )
        rows.append(
            {
                "submission": submission,
                "receipt_month": submission.target_receipt_month,
                "status_label": status_label,
                "status_class": status_class,
                "required_service_count": summary.required_service_count,
                "usage_based_service_count": summary.usage_based_service_count,
                "total_service_count": summary.total_services,
                "non_p_card_service_count": summary.non_p_card_service_count,
                "total_registered_service_count": summary.total_registered_service_count,
                "resolved_service_count": summary.resolved_count,
                "remaining_service_count": remaining_service_count,
            }
        )
    return render(request, "receipts/history.html", {"history_rows": rows})


@login_required
def submission_detail(request, pk: int):
    submission = get_object_or_404(
        Submission.objects.select_related("user").prefetch_related(
            Prefetch("receipts", queryset=Receipt.objects.select_related("service", "uploaded_by", "admin_reviewed_by"))
        ),
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

    if action == "update_superuser_email":
        if not request.user.is_superuser:
            raise PermissionDenied("スーパーアカウントのメールアドレスを変更できるのは本人だけです。")
        superuser_email_form = StaffSuperuserEmailForm(request.POST, user=request.user)
        if superuser_email_form.is_valid():
            previous_email = request.user.email
            account = superuser_email_form.save()
            if account.email:
                messages.success(request, f"スーパーアカウントの連絡先メールアドレスを {account.email} に変更しました。")
            elif previous_email:
                messages.success(request, "スーパーアカウントの連絡先メールアドレスを空欄にしました。以前のメールアドレスを一般ユーザー登録に使用できます。")
            else:
                messages.info(request, "スーパーアカウントの連絡先メールアドレスはすでに空欄です。")
        else:
            for errors in superuser_email_form.errors.values():
                for error in errors:
                    messages.error(request, error)
        return redirect("staff_user_create")

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
                Submission.objects.filter(user=account).delete()
                RegisteredService.objects.filter(user=account).delete()
                account.delete()
            messages.success(request, f"{username} を削除しました。関連するサービス、提出履歴、領収書ファイルも削除されました。全社カード明細の行データは監査履歴として残り、ユーザー紐付けだけが解除されます。")
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
            "superuser_email_form": (
                StaffSuperuserEmailForm(user=request.user) if request.user.is_superuser else None
            ),
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
        Submission.objects.select_related("user").prefetch_related(
            Prefetch("receipts", queryset=Receipt.objects.select_related("service", "uploaded_by", "admin_reviewed_by"))
        ),
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
        "receipt_kind",
        "service_name",
        "billing_type",
        "p_card_usage_at_upload",
        "amount",
        "currency",
        "issued_on",
        "upload_source",
        "uploaded_by",
        "uploaded_at",
        "expires_at",
        "file_status",
        "display_filename",
        "original_filename",
        "ai_filename_status",
        "ai_filename_admin_memo",
        "ai_extracted_payee",
        "ai_extracted_service_label",
        "ai_extracted_plan_name",
        "plan_change_details",
        "ai_extracted_recipient_name",
        "ai_extracted_card_last4",
        "ai_receipt_month",
        "ai_period_check_status",
        "ai_period_check_memo",
        "ai_check_card_last4",
        "ai_check_payee",
        "ai_check_recipient_name",
        "ai_recipient_name_check_memo",
        "ai_check_service_payee_related",
        "ai_service_payee_check_memo",
        "ai_check_date",
        "ai_check_amount",
        "ai_check_currency",
        "ai_check_period_match",
        "ai_resubmission_recommended",
        "ai_resubmission_recommendation_memo",
        "admin_review_status",
        "admin_reviewed_by",
        "admin_reviewed_at",
        "admin_review_note",
        "admin_filename_overridden",
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
                "extra" if receipt.is_extra else "registered_service",
                receipt.service_name_snapshot,
                receipt.get_billing_type_snapshot_display(),
                "yes" if receipt.p_card_usage_snapshot else "no",
                receipt.amount if receipt.amount is not None else "",
                receipt.currency,
                receipt.issued_on.isoformat() if receipt.issued_on else "",
                receipt.get_upload_source_display(),
                receipt.uploaded_by_label,
                receipt.uploaded_at.isoformat() if receipt.uploaded_at else "",
                receipt.expires_at.isoformat() if receipt.expires_at else "",
                receipt.file_status_label,
                receipt.display_filename,
                receipt.original_filename,
                receipt.get_ai_filename_status_display(),
                receipt.ai_filename_admin_memo,
                receipt.ai_extracted_payee,
                receipt.ai_extracted_service_label,
                receipt.ai_extracted_plan_name,
                json.dumps(receipt.plan_change_details or {}, ensure_ascii=False, sort_keys=True),
                receipt.ai_extracted_recipient_name,
                receipt.ai_extracted_card_last4,
                receipt.ai_receipt_month,
                receipt.get_ai_period_check_status_display(),
                receipt.ai_period_check_memo,
                "yes" if receipt.ai_check_card_last4 else "no",
                "yes" if receipt.ai_check_payee else "no",
                "yes" if receipt.ai_check_recipient_name else "no",
                receipt.ai_recipient_name_check_memo,
                "yes" if receipt.ai_check_service_payee_related else "no",
                receipt.ai_service_payee_check_memo,
                "yes" if receipt.ai_check_date else "no",
                "yes" if receipt.ai_check_amount else "no",
                "yes" if receipt.ai_check_currency else "no",
                "yes" if receipt.ai_check_period_match else "no",
                "yes" if receipt.ai_resubmission_recommended else "no",
                receipt.ai_resubmission_recommendation_memo,
                receipt.get_admin_review_status_display(),
                receipt.admin_reviewer_label,
                receipt.admin_reviewed_at.isoformat() if receipt.admin_reviewed_at else "",
                receipt.admin_review_note,
                "yes" if receipt.admin_filename_overridden else "no",
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
        .prefetch_related(Prefetch("receipts", queryset=Receipt.objects.select_related("service", "uploaded_by", "admin_reviewed_by")))
    )
    return build_receipts_zip(queryset, f"receipts_{selected_month:%Y-%m}_submitted")


@staff_member_required
def staff_download_submission(request, pk: int):
    submission = get_object_or_404(
        Submission.objects.select_related("user").prefetch_related(
            Prefetch("receipts", queryset=Receipt.objects.select_related("service", "uploaded_by", "admin_reviewed_by"))
        ),
        pk=pk,
    )
    return build_receipts_zip([submission], f"receipts_{submission.period_month:%Y-%m}_{safe_part(submission.user.get_username())}")
