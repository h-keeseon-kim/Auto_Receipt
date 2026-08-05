from django.db import migrations


PLAN_CHANGE_METADATA_REFRESH_RECONCILE_MARKER = (
    "【照合ルール更新】契約変更メタデータを再抽出し、旧プラン名を抽出できない定期契約も厳格条件で推定候補へ含めるため再照合します。"
)


def refresh_existing_metadata(apps, schema_editor):
    Receipt = apps.get_model("receipts", "Receipt")
    CardStatement = apps.get_model("receipts", "CardStatement")

    # v1.14.0以前にAI確認済みだった領収書は、新設した契約変更項目を
    # 持っていない場合がある。OpenAI APIはここでは呼ばず、次回の明細
    # 再照合時にPDF埋め込みテキストから一度だけ再抽出できるようにする。
    Receipt.objects.filter(file_deleted_at__isnull=True).exclude(file="").update(
        plan_change_metadata_checked_at=None,
    )

    # 保存済みの明細PDFを再解析するのではなく、保存済み明細行と領収書を
    # 新しい推定条件で再照合するためのマーカーを付ける。
    for statement in CardStatement.objects.exclude(status__in=["processing", "failed"]).iterator():
        memo = statement.ai_admin_memo or ""
        if PLAN_CHANGE_METADATA_REFRESH_RECONCILE_MARKER not in memo:
            statement.ai_admin_memo = f"{memo} {PLAN_CHANGE_METADATA_REFRESH_RECONCILE_MARKER}".strip()
            statement.save(update_fields=["ai_admin_memo"])


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("receipts", "0034_plan_change_inference"),
    ]

    operations = [
        migrations.RunPython(refresh_existing_metadata, reverse_noop),
    ]
