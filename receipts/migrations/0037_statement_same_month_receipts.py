from django.db import migrations
from django.utils import timezone


SAME_MONTH_RECEIPT_RECONCILE_MARKER = (
    "【月次ルール更新】全社明細月と領収書発行月を同じ月として照合するため、最新の領収書と再照合します。"
)


def mark_existing_statements_for_reconciliation(apps, schema_editor):
    CardStatement = apps.get_model("receipts", "CardStatement")

    # 明細PDFは再解析せず、保存済みの明細行を、同じ領収書発行月に登録された
    # 全ユーザー領収書で一度だけ再照合する。
    for statement in CardStatement.objects.exclude(status__in=["processing", "failed"]).iterator():
        memo = statement.ai_admin_memo or ""
        if SAME_MONTH_RECEIPT_RECONCILE_MARKER not in memo:
            memo = f"{memo} {SAME_MONTH_RECEIPT_RECONCILE_MARKER}".strip()
        CardStatement.objects.filter(pk=statement.pk).update(
            ai_admin_memo=memo[:5000],
            reconciled_at=None,
            updated_at=timezone.now(),
        )


def reverse_noop(apps, schema_editor):
    # 再照合後の対応関係を安全に旧月次ルールへ戻せないため逆処理は行わない。
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("receipts", "0036_plan_change_user_inference_and_ui_cleanup"),
    ]

    operations = [
        migrations.RunPython(mark_existing_statements_for_reconciliation, reverse_noop),
    ]
