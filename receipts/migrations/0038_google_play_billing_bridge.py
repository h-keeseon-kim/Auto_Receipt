from django.db import migrations
from django.utils import timezone


BILLING_DESCRIPTOR_BRIDGE_RECONCILE_MARKER = (
    "【照合ルール更新】Google Play等の決済名義と領収書本文のサービス名を既知の請求経路として照合し、明細未使用書類は未解決明細だけと比較するため再照合します。"
)


def mark_existing_statements_for_reconciliation(apps, schema_editor):
    CardStatement = apps.get_model("receipts", "CardStatement")

    # 明細PDFはOpenAIへ再送信せず、保存済み明細行と領収書メタデータを
    # 決済名義ブリッジを含む新ルールで一度だけ再照合する。
    for statement in CardStatement.objects.exclude(status__in=["processing", "failed"]).iterator():
        memo = statement.ai_admin_memo or ""
        if BILLING_DESCRIPTOR_BRIDGE_RECONCILE_MARKER not in memo:
            memo = f"{memo} {BILLING_DESCRIPTOR_BRIDGE_RECONCILE_MARKER}".strip()
        CardStatement.objects.filter(pk=statement.pk).update(
            ai_admin_memo=memo[:5000],
            reconciled_at=None,
            updated_at=timezone.now(),
        )


def reverse_noop(apps, schema_editor):
    # 再照合後の一対一割当を旧ルールへ安全に戻せないため逆処理は行わない。
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("receipts", "0037_statement_same_month_receipts"),
    ]

    operations = [
        migrations.RunPython(mark_existing_statements_for_reconciliation, reverse_noop),
    ]
