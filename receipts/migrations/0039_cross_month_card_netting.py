from django.db import migrations, models
from django.utils import timezone


CROSS_MONTH_CARD_NETTING_RECONCILE_MARKER = (
    "【照合ルール更新】明細内の実利用月を含む領収書参照、法人カード単位の後日返金相殺、返品元決済参照へ更新したため再照合します。"
)


def mark_existing_statements_for_reconciliation(apps, schema_editor):
    CardStatement = apps.get_model("receipts", "CardStatement")
    for statement in CardStatement.objects.exclude(status__in=["processing", "failed"]).iterator():
        memo = statement.ai_admin_memo or ""
        if CROSS_MONTH_CARD_NETTING_RECONCILE_MARKER not in memo:
            memo = f"{memo} {CROSS_MONTH_CARD_NETTING_RECONCILE_MARKER}".strip()
        CardStatement.objects.filter(pk=statement.pk).update(
            ai_admin_memo=memo[:5000],
            reconciled_at=None,
            updated_at=timezone.now(),
        )


def reverse_noop(apps, schema_editor):
    # Reconciliation assignments produced by the new global rule cannot be
    # safely reconstructed with the previous greedy rule.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("receipts", "0038_google_play_billing_bridge"),
    ]

    operations = [
        migrations.AlterField(
            model_name="cardstatementitem",
            name="match_reason_code",
            field=models.CharField(
                choices=[
                    ("auto_strong", "直接一致"),
                    ("plan_change_inferred", "契約変更情報による推定"),
                    ("plan_change_confirmed", "契約変更推定を管理者確定"),
                    ("original_charge", "返金書内の元決済確認"),
                    ("linked_refund_net", "紐付返金相殺"),
                    ("merchant_refund_net", "法人カード単位の後日返金相殺"),
                    ("refund_adjusted", "旧返金調整（再照合対象）"),
                    ("parse_review", "解析要確認"),
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
        migrations.RunPython(mark_existing_statements_for_reconciliation, reverse_noop),
    ]
