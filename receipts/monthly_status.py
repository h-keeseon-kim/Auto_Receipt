from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from django.contrib.auth.models import User

from .models import (
    BillingType,
    MonthlyServiceDeclaration,
    Receipt,
    RegisteredService,
    Submission,
    month_start,
)


@dataclass(frozen=True)
class ServiceMonthStatus:
    service: RegisteredService
    receipts: tuple[Receipt, ...]
    no_usage_declared: bool

    @property
    def has_receipt(self) -> bool:
        return any(receipt.file_available for receipt in self.receipts)

    @property
    def is_metered(self) -> bool:
        return self.service.billing_type == BillingType.METERED

    @property
    def status_code(self) -> str:
        if self.has_receipt:
            return "uploaded"
        if self.is_metered and self.no_usage_declared:
            return "no_usage"
        if self.is_metered:
            return "api_pending"
        return "missing"

    @property
    def status_label(self) -> str:
        return {
            "uploaded": "領収書あり",
            "no_usage": "当月利用なし",
            "api_pending": "API利用確認待ち",
            "missing": "領収書未提出",
        }[self.status_code]

    @property
    def badge_class(self) -> str:
        return {
            "uploaded": "submitted",
            "no_usage": "neutral",
            "api_pending": "draft",
            "missing": "draft",
        }[self.status_code]


@dataclass(frozen=True)
class UserMonthSummary:
    user: User
    period_month: date
    rows: tuple[ServiceMonthStatus, ...]

    @property
    def total_services(self) -> int:
        return len(self.rows)

    @property
    def uploaded_count(self) -> int:
        return sum(row.has_receipt for row in self.rows)

    @property
    def no_usage_count(self) -> int:
        return sum(row.status_code == "no_usage" for row in self.rows)

    @property
    def metered_rows(self) -> tuple[ServiceMonthStatus, ...]:
        return tuple(row for row in self.rows if row.is_metered)

    @property
    def non_metered_rows(self) -> tuple[ServiceMonthStatus, ...]:
        return tuple(row for row in self.rows if not row.is_metered)

    @property
    def missing_required(self) -> tuple[ServiceMonthStatus, ...]:
        return tuple(row for row in self.rows if row.status_code == "missing")

    @property
    def api_pending(self) -> tuple[ServiceMonthStatus, ...]:
        return tuple(row for row in self.rows if row.status_code == "api_pending")

    @property
    def missing_required_count(self) -> int:
        return len(self.missing_required)

    @property
    def api_pending_count(self) -> int:
        return len(self.api_pending)

    @property
    def resolved_count(self) -> int:
        return self.uploaded_count + self.no_usage_count

    @property
    def is_complete(self) -> bool:
        return self.missing_required_count == 0 and self.api_pending_count == 0

    @property
    def progress_label(self) -> str:
        return f"{self.resolved_count}/{self.total_services}"


def build_user_month_summary(user: User, period_month: date) -> UserMonthSummary:
    period_month = month_start(period_month)
    services = list(
        RegisteredService.objects.uploadable_for(user, period_month)
        .select_related("catalog_service")
        .order_by("name", "billing_type")
    )
    submission = Submission.objects.filter(user=user, period_month=period_month).first()
    receipts_by_service: dict[int, list[Receipt]] = {}
    if submission is not None:
        for receipt in (
            Receipt.objects.filter(submission=submission)
            .select_related("service")
            .order_by("uploaded_at", "pk")
        ):
            receipts_by_service.setdefault(receipt.service_id, []).append(receipt)

    declaration_service_ids = set(
        MonthlyServiceDeclaration.objects.filter(
            user=user,
            period_month=period_month,
            no_usage=True,
        ).values_list("service_id", flat=True)
    )
    rows = tuple(
        ServiceMonthStatus(
            service=service,
            receipts=tuple(receipts_by_service.get(service.pk, [])),
            no_usage_declared=service.pk in declaration_service_ids,
        )
        for service in services
    )
    return UserMonthSummary(user=user, period_month=period_month, rows=rows)


def can_submit_without_receipt(user: User, period_month: date) -> bool:
    summary = build_user_month_summary(user, period_month)
    return bool(summary.rows) and summary.is_complete and summary.no_usage_count == summary.total_services
