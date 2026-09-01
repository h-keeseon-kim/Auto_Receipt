from django.db import migrations
from django.utils import timezone


UNMATCHED_RECEIPT_EVENT_SCOPE_RECONCILE_MARKER = (
    "【照合表示更新】保存先の提出月ではなくPDF本文の書類日・取引日を基準に、当月分と実明細に関連する月跨ぎ書類だけを明細未使用一覧へ表示するため再照合します。"
)


def mark_existing_statements_for_reconciliation(apps, schema_editor):
    CardStatement = apps.get_model("receipts", "CardStatement")
    queryset = CardStatement.objects.exclude(status__in=["processing", "failed"])
    for statement in queryset.iterator():
        memo = statement.ai_admin_memo or ""
        if UNMATCHED_RECEIPT_EVENT_SCOPE_RECONCILE_MARKER not in memo:
            # Put the marker first so the 5,000-character memo cap cannot trim
            # it before the next statement-page access triggers reconciliation.
            memo = f"{UNMATCHED_RECEIPT_EVENT_SCOPE_RECONCILE_MARKER} {memo}".strip()
        CardStatement.objects.filter(pk=statement.pk).update(
            ai_admin_memo=memo[:5000],
            reconciled_at=None,
            updated_at=timezone.now(),
        )


def reverse_noop(apps, schema_editor):
    # This migration only schedules deterministic reconciliation refreshes.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("receipts", "0039_cross_month_card_netting"),
    ]

    operations = [
        migrations.RunPython(mark_existing_statements_for_reconciliation, reverse_noop),
    ]
