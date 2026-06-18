from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable

from django.conf import settings
from django.contrib.auth.models import User
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from .models import EmailDeliveryLog, EmailDeliveryStatus, EmailType, Submission, SubmissionStatus, UserAccountStatus, add_months, month_start


def current_target_month(offset: int | None = None) -> date:
    """Return the first day of the reminder target month."""

    today = timezone.localdate()
    first_day = today.replace(day=1)
    if offset is None:
        offset = getattr(settings, "RECEIPT_REMINDER_TARGET_MONTH_OFFSET", 0)
    return add_months(first_day, int(offset))


def user_email(user: User) -> str:
    value = (user.email or "").strip().lower()
    if value:
        return value
    username = (user.get_username() or "").strip().lower()
    return username if "@" in username else ""


def active_general_users() -> Iterable[User]:
    return (
        User.objects.filter(
            is_active=True,
            is_staff=False,
            is_superuser=False,
            profile__account_status=UserAccountStatus.ACTIVE,
        )
        .select_related("profile")
        .order_by("username")
    )


def find_general_user_by_email(email: str) -> User | None:
    email = (email or "").strip().lower()
    if not email:
        return None
    return (
        User.objects.filter(is_active=True, is_staff=False, is_superuser=False)
        .filter(Q(email__iexact=email) | Q(username__iexact=email))
        .select_related("profile")
        .order_by("id")
        .first()
    )


def user_allows_receipt_email(user: User | None) -> bool:
    if user is None:
        return True
    try:
        return user.profile.account_status == UserAccountStatus.ACTIVE
    except Exception:
        return False


def users_without_submitted_submission(target_month: date) -> Iterable[User]:
    target_month = month_start(target_month)
    submitted_user_ids = Submission.objects.filter(
        period_month=target_month,
        status=SubmissionStatus.SUBMITTED,
    ).values_list("user_id", flat=True)
    return active_general_users().exclude(pk__in=submitted_user_ids)


def build_app_url(path: str) -> str:
    base_url = getattr(settings, "APP_BASE_URL", "") or ""
    if base_url:
        return f"{base_url}{path}"
    return path


def month_label(value: date) -> str:
    return value.strftime("%Y年%m月")


def upload_url_for_month(target_month: date) -> str:
    return build_app_url(f"{reverse('dashboard')}?month={target_month:%Y-%m}")


def reminder_subject(email_type: str, target_month: date) -> str:
    app_name = getattr(settings, "APP_NAME", "ReceiptHub")
    label = month_label(target_month)
    if email_type == EmailType.REMINDER_URGENT:
        return f"【重要】{app_name}: {label}分の領収書を至急アップロードしてください"
    return f"{app_name}: {label}分の領収書アップロードをお願いします"


def reminder_body(user: User, email_type: str, target_month: date) -> str:
    app_name = getattr(settings, "APP_NAME", "ReceiptHub")
    label = month_label(target_month)
    url = upload_url_for_month(target_month)
    name = user.get_full_name() or user.get_username()
    if email_type == EmailType.REMINDER_URGENT:
        lead = f"{label}分の領収書提出がまだ確認できていません。至急、領収書をアップロードして提出してください。"
    else:
        lead = f"{label}分の領収書をアップロードしてください。"
    return "\n".join(
        [
            f"{name} 様",
            "",
            lead,
            "",
            f"アップロードページ: {url}",
            "",
            "アップロード後は、対象月の領収書が揃っていることを確認して「提出する」を押してください。",
            "",
            f"{app_name}",
        ]
    )


def reminder_idempotency_key(email_type: str, target_month: date, user: User, to_email: str) -> str:
    return f"receipthub:{email_type}:{target_month:%Y-%m}:user-{user.pk}:{to_email.lower()}"


def send_logged_email(
    *,
    email_type: str,
    to_email: str,
    subject: str,
    body: str,
    user: User | None = None,
    target_month: date | None = None,
    idempotency_key: str | None = None,
    created_by: User | None = None,
    force: bool = False,
) -> tuple[EmailDeliveryLog, bool]:
    """Send one email and persist the result.

    Returns (log, did_send). For idempotent reminder emails, previously sent logs are
    returned with did_send=False.
    """

    target_month = month_start(target_month) if target_month else None
    to_email = (to_email or "").strip().lower()
    log_to_update = None
    if idempotency_key and not force:
        existing = EmailDeliveryLog.objects.filter(idempotency_key=idempotency_key).first()
        if existing and existing.status == EmailDeliveryStatus.SENT:
            return existing, False
        log_to_update = existing
    effective_key = idempotency_key
    if force and idempotency_key:
        effective_key = f"{idempotency_key}:force:{timezone.now().strftime('%Y%m%d%H%M%S%f')}"

    if not user_allows_receipt_email(user):
        status = EmailDeliveryStatus.SKIPPED
        error = "停止中ユーザーのため送信しませんでした。"
        if log_to_update is not None:
            log_to_update.email_type = email_type
            log_to_update.user = user
            log_to_update.target_month = target_month
            log_to_update.to_email = to_email
            log_to_update.subject = subject
            log_to_update.status = status
            log_to_update.message = body
            log_to_update.error = error
            log_to_update.sent_at = None
            log_to_update.created_by = created_by or log_to_update.created_by
            log_to_update.save(
                update_fields=[
                    "email_type",
                    "user",
                    "target_month",
                    "to_email",
                    "subject",
                    "status",
                    "message",
                    "error",
                    "sent_at",
                    "created_by",
                ]
            )
            return log_to_update, False
        log = EmailDeliveryLog.objects.create(
            email_type=email_type,
            user=user,
            target_month=target_month,
            to_email=to_email,
            subject=subject,
            status=status,
            message=body,
            error=error,
            idempotency_key=effective_key,
            sent_at=None,
            created_by=created_by,
        )
        return log, False

    headers = {}
    if effective_key:
        headers["Resend-Idempotency-Key"] = effective_key

    status = EmailDeliveryStatus.SENT
    sent_at = None
    error = ""
    try:
        message = EmailMultiAlternatives(
            subject=subject,
            body=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[to_email],
            headers=headers,
        )
        message.send(fail_silently=False)
        sent_at = timezone.now()
    except Exception as exc:  # pragma: no cover - exact SMTP errors depend on provider/network
        status = EmailDeliveryStatus.FAILED
        error = str(exc)

    if log_to_update is not None:
        log_to_update.email_type = email_type
        log_to_update.user = user
        log_to_update.target_month = target_month
        log_to_update.to_email = to_email
        log_to_update.subject = subject
        log_to_update.status = status
        log_to_update.message = body
        log_to_update.error = error
        log_to_update.sent_at = sent_at
        log_to_update.created_by = created_by or log_to_update.created_by
        log_to_update.save(
            update_fields=[
                "email_type",
                "user",
                "target_month",
                "to_email",
                "subject",
                "status",
                "message",
                "error",
                "sent_at",
                "created_by",
            ]
        )
        return log_to_update, status == EmailDeliveryStatus.SENT

    try:
        log = EmailDeliveryLog.objects.create(
            email_type=email_type,
            user=user,
            target_month=target_month,
            to_email=to_email,
            subject=subject,
            status=status,
            message=body,
            error=error,
            idempotency_key=effective_key,
            sent_at=sent_at,
            created_by=created_by,
        )
    except IntegrityError:
        log = EmailDeliveryLog.objects.get(idempotency_key=effective_key)
        return log, False
    return log, status == EmailDeliveryStatus.SENT


@dataclass
class ReminderRunResult:
    email_type: str
    target_month: date
    selected_count: int = 0
    sent_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    dry_run_count: int = 0
    logs: list[EmailDeliveryLog] = field(default_factory=list)


def send_receipt_reminders(
    *,
    email_type: str,
    target_month: date | None = None,
    dry_run: bool = False,
    force: bool = False,
    created_by: User | None = None,
) -> ReminderRunResult:
    target_month = month_start(target_month or current_target_month())
    if email_type == EmailType.REMINDER_URGENT:
        users = list(users_without_submitted_submission(target_month))
    elif email_type == EmailType.REMINDER_INITIAL:
        users = list(active_general_users())
    else:
        raise ValueError("email_type must be reminder_initial or reminder_urgent")

    result = ReminderRunResult(email_type=email_type, target_month=target_month, selected_count=len(users))
    for user in users:
        to_email = user_email(user)
        if not to_email:
            result.skipped_count += 1
            continue
        subject = reminder_subject(email_type, target_month)
        body = reminder_body(user, email_type, target_month)
        key = reminder_idempotency_key(email_type, target_month, user, to_email)
        if dry_run:
            result.dry_run_count += 1
            continue
        log, did_send = send_logged_email(
            email_type=email_type,
            user=user,
            target_month=target_month,
            to_email=to_email,
            subject=subject,
            body=body,
            idempotency_key=key,
            created_by=created_by,
            force=force,
        )
        result.logs.append(log)
        if not did_send and log.status == EmailDeliveryStatus.SENT:
            result.skipped_count += 1
        elif log.status == EmailDeliveryStatus.SENT:
            result.sent_count += 1
        else:
            result.failed_count += 1
    return result


def send_test_email(*, to_email: str, subject: str, body: str, created_by: User | None = None) -> tuple[EmailDeliveryLog, bool]:
    matched_user = find_general_user_by_email(to_email)
    key = f"receipthub:test:{timezone.now().strftime('%Y%m%d%H%M%S%f')}:{to_email.lower()}"
    return send_logged_email(
        email_type=EmailType.TEST,
        user=matched_user,
        to_email=to_email,
        subject=subject,
        body=body,
        idempotency_key=key,
        created_by=created_by,
        force=True,
    )
