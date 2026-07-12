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

from .models import (
    DEFAULT_INITIAL_BODY,
    DEFAULT_INITIAL_SUBJECT,
    DEFAULT_URGENT_BODY,
    DEFAULT_URGENT_SUBJECT,
    EmailDeliveryLog,
    EmailDeliveryStatus,
    EmailReminderSchedule,
    EmailType,
    UserAccountStatus,
    add_months,
    month_start,
)
from .monthly_status import UserMonthSummary, build_user_month_summary


def current_target_month(offset: int | None = None) -> date:
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


@dataclass(frozen=True)
class ReminderRecipient:
    user: User
    summary: UserMonthSummary


def reminder_candidates(email_type: str, target_month: date) -> list[ReminderRecipient]:
    target_month = month_start(target_month)
    candidates: list[ReminderRecipient] = []
    for user in active_general_users():
        summary = build_user_month_summary(user, target_month)
        if email_type == EmailType.REMINDER_INITIAL:
            # 通常リマインダーでは、APIのみ未確認のユーザーには送らない。
            selected = summary.missing_required_count > 0
        elif email_type == EmailType.REMINDER_URGENT:
            selected = summary.missing_required_count > 0 or summary.api_pending_count > 0
        else:
            raise ValueError("email_type must be reminder_initial or reminder_urgent")
        if selected:
            candidates.append(ReminderRecipient(user=user, summary=summary))
    return candidates


def users_without_submitted_submission(target_month: date) -> Iterable[User]:
    """旧関数名との互換用。現在は未解決サービスがあるユーザーを返す。"""

    return [item.user for item in reminder_candidates(EmailType.REMINDER_URGENT, target_month)]


def build_app_url(path: str) -> str:
    base_url = getattr(settings, "APP_BASE_URL", "") or ""
    if base_url:
        return f"{base_url}{path}"
    return path


def month_label(value: date) -> str:
    return value.strftime("%Y年%m月")


def upload_url_for_month(target_month: date) -> str:
    return build_app_url(f"{reverse('dashboard')}?month={target_month:%Y-%m}")


def service_list_text(rows) -> str:
    values = [f"・{row.service.display_name}" for row in rows]
    return "\n".join(values) if values else "・なし"


def reminder_template_context(user: User, target_month: date, summary: UserMonthSummary | None) -> dict[str, str]:
    return {
        "app_name": getattr(settings, "APP_NAME", "ReceiptHub"),
        "user_name": user.get_full_name() or user.get_username(),
        "target_month": month_label(target_month),
        "upload_url": upload_url_for_month(target_month),
        "missing_services": service_list_text(summary.missing_required if summary else ()),
        "api_pending_services": service_list_text(summary.api_pending if summary else ()),
    }


class _TemplateValues(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def render_email_template(template: str, context: dict[str, str]) -> str:
    try:
        return (template or "").format_map(_TemplateValues(context))
    except (ValueError, KeyError):
        return template or ""


def reminder_subject(
    email_type: str,
    target_month: date,
    *,
    user: User | None = None,
    summary: UserMonthSummary | None = None,
    schedule: EmailReminderSchedule | None = None,
) -> str:
    schedule = schedule or EmailReminderSchedule.get_solo()
    fallback_user = user or User(username="user")
    context = reminder_template_context(fallback_user, target_month, summary)
    if email_type == EmailType.REMINDER_URGENT:
        subject = render_email_template(schedule.urgent_subject_template or DEFAULT_URGENT_SUBJECT, context).strip()
        if not subject.startswith("【重要】"):
            subject = f"【重要】{subject}"
        return subject[:255]
    return render_email_template(schedule.initial_subject_template or DEFAULT_INITIAL_SUBJECT, context).strip()[:255]


def reminder_body(
    user: User,
    email_type: str,
    target_month: date,
    *,
    summary: UserMonthSummary | None = None,
    schedule: EmailReminderSchedule | None = None,
) -> str:
    schedule = schedule or EmailReminderSchedule.get_solo()
    context = reminder_template_context(user, target_month, summary)
    if email_type == EmailType.REMINDER_URGENT:
        template = schedule.urgent_body_template or DEFAULT_URGENT_BODY
    else:
        template = schedule.initial_body_template or DEFAULT_INITIAL_BODY
    return render_email_template(template, context)


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
        values = {
            "email_type": email_type,
            "user": user,
            "target_month": target_month,
            "to_email": to_email,
            "subject": subject,
            "status": status,
            "message": body,
            "error": error,
            "sent_at": None,
            "created_by": created_by,
        }
        if log_to_update is not None:
            for key, value in values.items():
                if key == "created_by" and value is None:
                    continue
                setattr(log_to_update, key, value)
            log_to_update.save()
            return log_to_update, False
        return EmailDeliveryLog.objects.create(idempotency_key=effective_key, **values), False

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
    except Exception as exc:  # pragma: no cover
        status = EmailDeliveryStatus.FAILED
        error = str(exc)

    values = {
        "email_type": email_type,
        "user": user,
        "target_month": target_month,
        "to_email": to_email,
        "subject": subject,
        "status": status,
        "message": body,
        "error": error,
        "sent_at": sent_at,
        "created_by": created_by,
    }
    if log_to_update is not None:
        for key, value in values.items():
            if key == "created_by" and value is None:
                continue
            setattr(log_to_update, key, value)
        log_to_update.save()
        return log_to_update, status == EmailDeliveryStatus.SENT

    try:
        log = EmailDeliveryLog.objects.create(idempotency_key=effective_key, **values)
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
    candidates = reminder_candidates(email_type, target_month)
    schedule = EmailReminderSchedule.get_solo()
    result = ReminderRunResult(email_type=email_type, target_month=target_month, selected_count=len(candidates))
    for candidate in candidates:
        user = candidate.user
        to_email = user_email(user)
        if not to_email:
            result.skipped_count += 1
            continue
        subject = reminder_subject(
            email_type,
            target_month,
            user=user,
            summary=candidate.summary,
            schedule=schedule,
        )
        body = reminder_body(
            user,
            email_type,
            target_month,
            summary=candidate.summary,
            schedule=schedule,
        )
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
        if not did_send and log.status in {EmailDeliveryStatus.SENT, EmailDeliveryStatus.SKIPPED}:
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
