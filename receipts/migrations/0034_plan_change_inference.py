from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


PLAN_CHANGE_INFERENCE_RECONCILE_MARKER = (
    "【照合ルール更新】契約変更書類と過去の旧プラン実績による推定対応を追加したため再照合します。"
)


def prepare_existing_data(apps, schema_editor):
    CardStatement = apps.get_model("receipts", "CardStatement")
    for statement in CardStatement.objects.exclude(status__in=["processing", "failed"]).iterator():
        memo = statement.ai_admin_memo or ""
        if PLAN_CHANGE_INFERENCE_RECONCILE_MARKER not in memo:
            statement.ai_admin_memo = f"{memo} {PLAN_CHANGE_INFERENCE_RECONCILE_MARKER}".strip()
            statement.save(update_fields=["ai_admin_memo"])


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("receipts", "0033_service_label_and_unmatched_receipts"),
    ]

    operations = [
        migrations.AddField(
            model_name="receipt",
            name="ai_extracted_plan_name",
            field=models.CharField(
                blank=True,
                help_text="領収書本文に明示された契約プラン名。例: Claude Pro、Max plan - 20x。",
                max_length=160,
                verbose_name="AI抽出プラン名",
            ),
        ),
        migrations.AddField(
            model_name="receipt",
            name="plan_change_details",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="旧プラン、新プラン、変更日、旧プラン終了日、日割り調整額等を構造化して保存します。",
                verbose_name="契約・プラン変更情報",
            ),
        ),
        migrations.AddField(
            model_name="receipt",
            name="plan_change_metadata_checked_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="契約変更メタデータ確認日時"),
        ),
        migrations.AlterField(
            model_name="cardstatementitem",
            name="match_status",
            field=models.CharField(
                choices=[
                    ("matched", "一致"),
                    ("inferred", "推定対応"),
                    ("needs_review", "解析要確認"),
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
                    ("auto_strong", "直接一致"),
                    ("plan_change_inferred", "契約変更情報による推定"),
                    ("plan_change_confirmed", "契約変更推定を管理者確定"),
                    ("original_charge", "返金書内の元決済確認"),
                    ("linked_refund_net", "紐付返金相殺"),
                    ("merchant_refund_net", "同一請求元内の近接返金相殺"),
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
        migrations.CreateModel(
            name="CardStatementPlanChangeInference",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("user_snapshot", models.CharField(blank=True, max_length=255, verbose_name="利用者スナップショット")),
                ("change_filename_snapshot", models.CharField(blank=True, max_length=255, verbose_name="契約変更書類名")),
                ("historical_filename_snapshot", models.CharField(blank=True, max_length=255, verbose_name="過去領収書名")),
                ("previous_plan", models.CharField(max_length=160, verbose_name="旧プラン")),
                ("new_plan", models.CharField(blank=True, max_length=160, verbose_name="新プラン")),
                ("change_date", models.DateField(blank=True, null=True, verbose_name="変更日")),
                ("previous_plan_end", models.DateField(verbose_name="旧プラン終了日")),
                ("historical_receipt_date", models.DateField(verbose_name="過去領収書日")),
                ("amount", models.DecimalField(decimal_places=2, max_digits=14, verbose_name="推定金額")),
                ("currency", models.CharField(max_length=3, verbose_name="通貨")),
                ("confidence", models.FloatField(default=0, verbose_name="推定信頼度")),
                ("reason", models.TextField(verbose_name="推定根拠")),
                ("candidate_fingerprint", models.CharField(max_length=255, verbose_name="候補指紋")),
                ("status", models.CharField(choices=[("pending", "管理者確認待ち"), ("confirmed", "一致として確定"), ("rejected", "推定不採用")], default="pending", max_length=20, verbose_name="確認状態")),
                ("reviewed_at", models.DateTimeField(blank=True, null=True, verbose_name="確認日時")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="作成日時")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新日時")),
                ("change_receipt", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="plan_change_inferences_as_change_document", to="receipts.receipt", verbose_name="契約変更書類")),
                ("historical_receipt", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="plan_change_inferences_as_history", to="receipts.receipt", verbose_name="過去契約実績")),
                ("reviewed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="reviewed_statement_plan_change_inferences", to=settings.AUTH_USER_MODEL, verbose_name="確認管理者")),
                ("statement_item", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="plan_change_inference", to="receipts.cardstatementitem", verbose_name="カード明細項目")),
                ("user", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="statement_plan_change_inferences", to=settings.AUTH_USER_MODEL, verbose_name="推定利用者")),
            ],
            options={
                "verbose_name": "契約変更推定対応",
                "verbose_name_plural": "契約変更推定対応",
                "ordering": ["statement_item__sequence", "pk"],
            },
        ),
        migrations.AddIndex(
            model_name="cardstatementplanchangeinference",
            index=models.Index(fields=["status", "updated_at"], name="stmt_plan_inf_status_idx"),
        ),
        migrations.RunPython(prepare_existing_data, reverse_noop),
    ]
