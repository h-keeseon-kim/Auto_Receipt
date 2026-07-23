from django.contrib import admin

from .models import (
    CardStatement,
    CardStatementItem,
    CardStatementMatchCandidate,
    EmailDeliveryLog,
    EmailReminderSchedule,
    MonthlyServiceDeclaration,
    Receipt,
    ReceiptResubmissionRequest,
    RegisteredService,
    ServiceCatalog,
    ServiceExceptionRequest,
    Submission,
    UserProfile,
)


class ReceiptInline(admin.TabularInline):
    model = Receipt
    extra = 0
    readonly_fields = (
        "service_name_snapshot",
        "billing_type_snapshot",
        "p_card_usage_snapshot",
        "original_filename",
        "generated_filename",
        "ai_filename_status",
        "ai_filename_admin_memo",
        "ai_filename_checked_at",
        "ai_extracted_payee",
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
        "admin_review_status",
        "admin_reviewed_by",
        "admin_reviewed_at",
        "admin_review_note",
        "admin_filename_overridden",
        "file_size",
        "content_type",
        "uploaded_at",
        "expires_at",
        "file_deleted_at",
    )
    fields = (
        "is_extra",
        "service",
        "service_name_snapshot",
        "billing_type_snapshot",
        "p_card_usage_snapshot",
        "memo",
        "amount",
        "currency",
        "issued_on",
        "file",
        "original_filename",
        "generated_filename",
        "ai_filename_status",
        "ai_filename_admin_memo",
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
        "admin_review_status",
        "admin_reviewed_by",
        "admin_reviewed_at",
        "admin_review_note",
        "admin_filename_overridden",
        "expires_at",
        "file_deleted_at",
    )


@admin.register(ServiceCatalog)
class ServiceCatalogAdmin(admin.ModelAdmin):
    list_display = ("display_name", "name", "billing_type", "is_active", "created_by", "updated_at")
    list_filter = ("billing_type", "is_active")
    search_fields = ("name", "merchant_aliases", "memo", "created_by__username", "created_by__email")


@admin.register(RegisteredService)
class RegisteredServiceAdmin(admin.ModelAdmin):
    list_display = (
        "display_name",
        "name",
        "user",
        "billing_type",
        "uses_p_card",
        "is_active",
        "registration_source",
        "deactivation_source",
        "final_receipt_month",
        "updated_at",
    )
    list_filter = ("billing_type", "uses_p_card", "is_active", "registration_source", "deactivation_source", "final_receipt_month")
    search_fields = ("name", "user__username", "user__email", "memo", "stop_note")
    readonly_fields = ("created_at", "updated_at", "deactivated_at")


@admin.register(ServiceExceptionRequest)
class ServiceExceptionRequestAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "service_name",
        "billing_type",
        "uses_p_card",
        "status",
        "reviewed_by",
        "created_at",
        "reviewed_at",
    )
    list_filter = ("status", "billing_type", "uses_p_card", "created_at", "reviewed_at")
    search_fields = ("user__username", "user__email", "service_name", "purpose", "review_note")
    readonly_fields = ("created_at", "updated_at", "reviewed_at")


@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    list_display = ("user", "period_month", "status", "receipt_total", "available_files", "submitted_at", "updated_at")
    list_filter = ("status", "period_month")
    search_fields = ("user__username", "user__email", "receipts__service_name_snapshot")
    date_hierarchy = "period_month"
    inlines = [ReceiptInline]

    @admin.display(description="領収書数")
    def receipt_total(self, obj):
        return obj.receipt_count

    @admin.display(description="保存中ファイル")
    def available_files(self, obj):
        return obj.available_file_count


@admin.register(Receipt)
class ReceiptAdmin(admin.ModelAdmin):
    list_display = (
        "service_name_snapshot",
        "is_extra",
        "billing_type_snapshot",
        "p_card_usage_snapshot",
        "submission",
        "amount",
        "currency",
        "issued_on",
        "upload_source",
        "uploaded_by",
        "uploaded_at",
        "expires_at",
        "ai_filename_status",
        "ai_period_check_status",
        "ai_check_recipient_name",
        "ai_check_service_payee_related",
        "ai_check_period_match",
        "ai_resubmission_recommended",
        "admin_review_status",
        "file_status",
    )
    list_filter = ("is_extra", "p_card_usage_snapshot", "upload_source", "admin_review_status", "ai_resubmission_recommended", "billing_type_snapshot", "currency", "ai_filename_status", "ai_period_check_status", "ai_check_recipient_name", "ai_check_service_payee_related", "ai_check_period_match", "submission__period_month", "file_deleted_at")
    search_fields = ("service_name_snapshot", "memo", "submission__user__username", "uploaded_by__username", "uploaded_by__email", "original_filename", "generated_filename", "ai_extracted_payee", "ai_extracted_recipient_name", "ai_filename_admin_memo", "ai_period_check_memo", "ai_recipient_name_check_memo", "ai_service_payee_check_memo", "ai_resubmission_recommendation_memo")
    readonly_fields = (
        "service_name_snapshot",
        "billing_type_snapshot",
        "p_card_usage_snapshot",
        "original_filename",
        "generated_filename",
        "ai_filename_status",
        "ai_filename_admin_memo",
        "ai_filename_checked_at",
        "ai_extracted_payee",
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
        "content_type",
        "upload_source",
        "uploaded_by",
        "uploaded_at",
        "expires_at",
        "file_deleted_at",
        "file_delete_reason",
    )

    @admin.display(description="ファイル状態")
    def file_status(self, obj):
        return obj.file_status_label


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "account_status", "must_change_password", "initial_password_generated_at", "password_changed_at", "tutorial_completed_at", "created_by")
    list_filter = ("account_status", "must_change_password", "initial_password_generated_at", "password_changed_at", "tutorial_completed_at")
    search_fields = ("user__username", "user__email", "created_by__username", "created_by__email")
    readonly_fields = ("created_at", "updated_at", "initial_password_generated_at", "password_changed_at", "tutorial_completed_at")


@admin.register(ReceiptResubmissionRequest)
class ReceiptResubmissionRequestAdmin(admin.ModelAdmin):
    list_display = ("user", "period_month", "service_name_snapshot", "is_extra", "billing_type_snapshot", "status", "created_by", "created_at", "resolved_at")
    list_filter = ("status", "is_extra", "period_month", "billing_type_snapshot", "created_at", "resolved_at")
    search_fields = ("user__username", "user__email", "service_name_snapshot", "receipt_memo_snapshot", "original_filename", "display_filename", "message")
    readonly_fields = ("created_at", "resolved_at")


@admin.register(EmailDeliveryLog)
class EmailDeliveryLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "email_type", "target_month", "to_email", "subject", "status", "sent_at", "created_by")
    list_filter = ("email_type", "status", "target_month", "created_at")
    search_fields = ("to_email", "subject", "message", "error", "idempotency_key", "user__username", "user__email")
    readonly_fields = ("created_at", "sent_at", "idempotency_key", "error")


@admin.register(EmailReminderSchedule)
class EmailReminderScheduleAdmin(admin.ModelAdmin):
    list_display = ("reminder_day", "warning_day", "updated_by", "updated_at")
    fields = (
        "reminder_day",
        "warning_day",
        "initial_subject_template",
        "initial_body_template",
        "urgent_subject_template",
        "urgent_body_template",
        "updated_by",
        "created_at",
        "updated_at",
    )
    readonly_fields = ("created_at", "updated_at")

    def has_add_permission(self, request):
        return not EmailReminderSchedule.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(MonthlyServiceDeclaration)
class MonthlyServiceDeclarationAdmin(admin.ModelAdmin):
    list_display = ("user", "period_month", "service", "no_usage", "declared_by", "updated_at")
    list_filter = ("no_usage", "period_month", "service__billing_type")
    search_fields = ("user__username", "user__email", "service__name", "note")
    readonly_fields = ("declared_at", "updated_at")


class CardStatementItemInline(admin.TabularInline):
    model = CardStatementItem
    extra = 0
    fields = (
        "sequence",
        "line_reference",
        "transaction_date",
        "merchant_name",
        "amount_jpy",
        "original_amount",
        "original_currency",
        "matched_user",
        "matched_catalog_service",
        "matched_service",
        "match_status",
        "receipt_required",
        "matched_receipt",
        "match_memo",
    )
    readonly_fields = ("sequence", "line_reference", "transaction_date", "merchant_name", "amount_jpy", "original_amount", "original_currency")


@admin.register(CardStatement)
class CardStatementAdmin(admin.ModelAdmin):
    list_display = (
        "period_month",
        "status",
        "card_last4",
        "statement_period",
        "missing_receipt_count",
        "manual_review_count",
        "uploaded_at",
        "file_deleted_at",
    )
    list_filter = ("status", "period_month", "file_deleted_at")
    search_fields = ("original_filename", "ai_admin_memo", "items__merchant_name", "items__matched_user__username")
    readonly_fields = ("uploaded_at", "processed_at", "reconciled_at", "expires_at", "file_deleted_at", "file_delete_reason", "updated_at")
    inlines = [CardStatementItemInline]


@admin.register(CardStatementItem)
class CardStatementItemAdmin(admin.ModelAdmin):
    list_display = (
        "statement",
        "line_reference",
        "transaction_date",
        "merchant_name",
        "amount_jpy",
        "matched_user",
        "matched_catalog_service",
        "matched_service",
        "match_status",
        "receipt_required",
        "matched_receipt",
    )
    list_filter = ("match_status", "receipt_required", "statement__period_month")
    search_fields = ("merchant_name", "line_reference", "matched_user__username", "matched_service__name", "match_memo")


@admin.register(CardStatementMatchCandidate)
class CardStatementMatchCandidateAdmin(admin.ModelAdmin):
    list_display = (
        "item",
        "rank",
        "receipt",
        "strength",
        "score",
        "confidence",
        "amount_match",
        "merchant_match",
        "service_match",
        "date_match",
    )
    list_filter = ("strength", "amount_match", "currency_match", "merchant_match", "service_match", "date_match")
    search_fields = (
        "item__merchant_name",
        "item__line_reference",
        "receipt__original_filename",
        "receipt__generated_filename",
        "receipt__submission__user__username",
        "rationale",
    )
    readonly_fields = (
        "item",
        "receipt",
        "rank",
        "score",
        "confidence",
        "strength",
        "amount_match",
        "amount_match_basis",
        "currency_match",
        "merchant_match",
        "service_match",
        "date_match",
        "rationale",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
