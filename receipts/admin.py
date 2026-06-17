from django.contrib import admin

from .models import Receipt, RegisteredService, ServiceCatalog, Submission, UserProfile


class ReceiptInline(admin.TabularInline):
    model = Receipt
    extra = 0
    readonly_fields = (
        "service_name_snapshot",
        "billing_type_snapshot",
        "original_filename",
        "generated_filename",
        "ai_filename_status",
        "ai_filename_admin_memo",
        "ai_filename_checked_at",
        "ai_extracted_payee",
        "ai_extracted_card_last4",
        "ai_receipt_month",
        "ai_period_check_status",
        "ai_period_check_memo",
        "file_size",
        "content_type",
        "uploaded_at",
        "expires_at",
        "file_deleted_at",
    )
    fields = (
        "service",
        "service_name_snapshot",
        "billing_type_snapshot",
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
        "expires_at",
        "file_deleted_at",
    )


@admin.register(ServiceCatalog)
class ServiceCatalogAdmin(admin.ModelAdmin):
    list_display = ("display_name", "name", "billing_type", "is_active", "created_by", "updated_at")
    list_filter = ("billing_type", "is_active")
    search_fields = ("name", "memo", "created_by__username", "created_by__email")


@admin.register(RegisteredService)
class RegisteredServiceAdmin(admin.ModelAdmin):
    list_display = (
        "display_name",
        "name",
        "user",
        "billing_type",
        "is_active",
        "registration_source",
        "deactivation_source",
        "final_receipt_month",
        "updated_at",
    )
    list_filter = ("billing_type", "is_active", "registration_source", "deactivation_source", "final_receipt_month")
    search_fields = ("name", "user__username", "user__email", "memo", "stop_note")
    readonly_fields = ("created_at", "updated_at", "deactivated_at")


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
        "billing_type_snapshot",
        "submission",
        "amount",
        "currency",
        "issued_on",
        "uploaded_at",
        "expires_at",
        "ai_filename_status",
        "ai_period_check_status",
        "file_status",
    )
    list_filter = ("billing_type_snapshot", "currency", "ai_filename_status", "ai_period_check_status", "submission__period_month", "file_deleted_at")
    search_fields = ("service_name_snapshot", "submission__user__username", "original_filename", "generated_filename", "ai_extracted_payee", "ai_filename_admin_memo", "ai_period_check_memo")
    readonly_fields = (
        "service_name_snapshot",
        "billing_type_snapshot",
        "original_filename",
        "generated_filename",
        "ai_filename_status",
        "ai_filename_admin_memo",
        "ai_filename_checked_at",
        "ai_extracted_payee",
        "ai_extracted_card_last4",
        "ai_receipt_month",
        "ai_period_check_status",
        "ai_period_check_memo",
        "file_size",
        "content_type",
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
    list_display = ("user", "must_change_password", "initial_password_generated_at", "password_changed_at", "created_by")
    list_filter = ("must_change_password", "initial_password_generated_at", "password_changed_at")
    search_fields = ("user__username", "user__email", "created_by__username", "created_by__email")
    readonly_fields = ("created_at", "updated_at", "initial_password_generated_at", "password_changed_at")
