from django.db import migrations, models


RECONCILE_MARKER = (
    "【照合ルール更新】利用日±1日・金額/通貨完全一致・ご利用先/払先関連の単純一対一照合へ変更したため、最新の領収書と再照合します。"
)


def prepare_existing_statements(apps, schema_editor):
    CardStatement = apps.get_model("receipts", "CardStatement")
    CardStatementItem = apps.get_model("receipts", "CardStatementItem")

    for statement in CardStatement.objects.exclude(status__in=["processing", "failed"]).iterator():
        memo = (statement.ai_admin_memo or "").strip()
        if RECONCILE_MARKER not in memo:
            statement.ai_admin_memo = " ".join(part for part in (memo, RECONCILE_MARKER) if part)[:5000]
            statement.save(update_fields=["ai_admin_memo"])

    # 旧「曖昧」状態は候補機能廃止後の再照合対象へ戻す。
    CardStatementItem.objects.filter(match_status="ambiguous").update(
        match_status="unmatched",
        match_reason_code="no_compatible_receipt",
        matched_user=None,
        matched_catalog_service=None,
        matched_service=None,
        matched_receipt=None,
        match_confidence=0,
        match_memo="旧照合候補機能を廃止したため、新しい3条件で再照合します。",
    )


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("receipts", "0030_requeue_incomplete_receipt_extraction"),
    ]

    operations = [
        migrations.RunPython(prepare_existing_statements, reverse_noop),
        migrations.DeleteModel(name="CardStatementMatchCandidate"),
        migrations.AlterField(
            model_name="cardstatementitem",
            name="match_status",
            field=models.CharField(
                choices=[
                    ("matched", "一致"),
                    ("ambiguous", "旧曖昧（再照合対象）"),
                    ("unmatched", "未一致"),
                    ("ignored", "対象外"),
                ],
                default="unmatched",
                max_length=20,
                verbose_name="一致ステータス",
            ),
        ),
        migrations.AlterField(
            model_name="cardstatementitem",
            name="match_reason_code",
            field=models.CharField(
                choices=[
                    ("auto_strong", "日付・金額・請求先一致"),
                    ("auto_amount_only", "旧金額一致（再照合対象）"),
                    ("multiple_compatible", "旧候補複数（再照合対象）"),
                    ("receipt_competition", "旧領収書競合（再照合対象）"),
                    ("user_ambiguous", "旧利用者未特定（再照合対象）"),
                    ("insufficient_evidence", "旧情報不足（再照合対象）"),
                    ("service_only", "旧サービス特定（再照合対象）"),
                    ("no_compatible_receipt", "領収書未提出"),
                    ("manual_confirmed", "管理者確定"),
                    ("ignored", "対象外"),
                ],
                default="no_compatible_receipt",
                max_length=40,
                verbose_name="判定理由",
            ),
        ),
    ]
