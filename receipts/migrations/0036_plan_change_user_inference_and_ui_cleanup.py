from django.db import migrations


PLAN_CHANGE_USER_INFERENCE_RECONCILE_MARKER = (
    "【照合ルール更新】契約変更書類のBill to利用者と前月カード明細の請求周期を推定対応に利用するため再照合します。"
)


def mark_existing_statements_for_reconciliation(apps, schema_editor):
    CardStatement = apps.get_model("receipts", "CardStatement")

    # 明細PDFをOpenAIへ再送信せず、保存済みの明細行・領収書メタデータ・
    # 前月カード明細を使って新しい推定ルールを一度だけ適用する。
    for statement in CardStatement.objects.exclude(status__in=["processing", "failed"]).iterator():
        memo = statement.ai_admin_memo or ""
        if PLAN_CHANGE_USER_INFERENCE_RECONCILE_MARKER in memo:
            continue
        statement.ai_admin_memo = (
            f"{memo} {PLAN_CHANGE_USER_INFERENCE_RECONCILE_MARKER}".strip()[:5000]
        )
        statement.save(update_fields=["ai_admin_memo"])


def reverse_noop(apps, schema_editor):
    # 再照合後の結果を安全に巻き戻すことはできないため逆処理は行わない。
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("receipts", "0035_refresh_plan_change_metadata"),
    ]

    operations = [
        migrations.RunPython(mark_existing_statements_for_reconciliation, reverse_noop),
    ]
