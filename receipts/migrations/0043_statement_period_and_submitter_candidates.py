from django.db import migrations, models
from django.utils import timezone


MARKER = "【対象月・候補表示更新】支払月と利用対象月を分離し、提出者候補を前月履歴の参照として再構築します。"


def schedule_refresh(apps, schema_editor):
    Statement = apps.get_model("receipts", "CardStatement")
    alias = schema_editor.connection.alias
    batch = []
    for statement in Statement.objects.using(alias).exclude(status__in=["processing", "failed"]).iterator(chunk_size=500):
        statement.ai_admin_memo = (MARKER + " " + (statement.ai_admin_memo or ""))[:5000]
        statement.reconciled_at = None
        statement.updated_at = timezone.now()
        batch.append(statement)
        if len(batch) >= 500:
            Statement.objects.using(alias).bulk_update(batch, ["ai_admin_memo", "reconciled_at", "updated_at"])
            batch = []
    if batch:
        Statement.objects.using(alias).bulk_update(batch, ["ai_admin_memo", "reconciled_at", "updated_at"])


class Migration(migrations.Migration):
    dependencies = [("receipts", "0042_global_component_usage_consistency")]
    operations = [
        migrations.AlterField(
            model_name="cardstatement", name="statement_period",
            field=models.CharField(blank=True, max_length=7, verbose_name="利用日検証月")),
        migrations.AddField(
            model_name="cardstatement", name="period_validation",
            field=models.JSONField(blank=True, default=dict, verbose_name="対象月の検証情報",
                help_text="利用日による対象月検証と、支払日・元のAI判定・抽出メモを分離して保持します。")),
        migrations.AddField(
            model_name="cardstatementitem", name="submitter_candidates",
            field=models.JSONField(blank=True, default=dict, verbose_name="前月履歴による提出者候補",
                help_text="前月履歴を参照した未確定の候補。今月の提出証拠・金額計算・使用済み台帳には使いません。")),
        migrations.RunPython(schedule_refresh, migrations.RunPython.noop),
    ]
