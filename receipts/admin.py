from django.contrib import admin

from .models import Receipt, RegisteredService, Submission


class ReceiptInline(admin.TabularInline):
    model = Receipt
    extra = 0
    readonly_fields = (
        "service_name_snapshot",
        "billing_type_snapshot",
        "original_filename",
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
        "expires_at",
        "file_deleted_at",
    )


@admin.register(RegisteredService)
class RegisteredServiceAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "billing_type", "is_active", "updated_at")
    list_filter = ("billing_type", "is_active")
    search_fields = ("name", "user__username", "user__email")


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
        "file_status",
    )
    list_filter = ("billing_type_snapshot", "currency", "submission__period_month", "file_deleted_at")
    search_fields = ("service_name_snapshot", "submission__user__username", "original_filename")
    readonly_fields = (
        "service_name_snapshot",
        "billing_type_snapshot",
        "original_filename",
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
