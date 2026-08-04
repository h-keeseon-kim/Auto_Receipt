from django.db import migrations, models
import django.db.models.deletion


RECONCILE_MARKER = (
    "【照合ルール更新】2026年7月実明細61行・提出PDF63件の全件検証に基づき、"
    "取引構成要素・重複排除・返金純額照合へ更新したため再照合します。"
)


def prepare_existing_data(apps, schema_editor):
    CardStatement = apps.get_model("receipts", "CardStatement")
    Receipt = apps.get_model("receipts", "Receipt")

    # 新しい本文解析を一度だけ実行できるよう、既存領収書は金融メタデータ未確認へ戻す。
    # OpenAI APIはこのマイグレーションでは呼ばず、次回の再照合でPDF埋め込みテキストを解析する。
    Receipt.objects.update(
        financial_document_kind="unknown",
        financial_transaction_reference="",
        financial_related_reference="",
        financial_transaction_components=[],
        financial_metadata_checked_at=None,
    )

    for statement in CardStatement.objects.exclude(status__in=["processing", "failed"]).iterator():
        memo = (statement.ai_admin_memo or "").strip()
        if RECONCILE_MARKER not in memo:
            statement.ai_admin_memo = " ".join(
                part for part in (memo, RECONCILE_MARKER) if part
            )[:5000]
            statement.save(update_fields=["ai_admin_memo"])


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("receipts", "0031_simple_statement_receipt_matching"),
    ]

    operations = [
        migrations.AddField(
            model_name="receipt",
            name="financial_document_kind",
            field=models.CharField(
                choices=[
                    ("unknown", "未判定"),
                    ("charge", "支払済み領収書"),
                    ("invoice", "請求書"),
                    ("refund", "返金書類"),
                ],
                default="unknown",
                help_text="領収書・請求書・返金書類を区別し、カード明細との純額照合に利用します。",
                max_length=20,
                verbose_name="金融書類区分",
            ),
        ),
        migrations.AddField(
            model_name="receipt",
            name="financial_transaction_reference",
            field=models.CharField(
                blank=True,
                help_text="請求番号、Invoice番号、決済Transaction IDなど、同一取引を識別する参照番号です。",
                max_length=160,
                verbose_name="取引参照番号",
            ),
        ),
        migrations.AddField(
            model_name="receipt",
            name="financial_related_reference",
            field=models.CharField(
                blank=True,
                help_text="返金元のSale Transaction IDなど、元取引との関連を示す参照番号です。",
                max_length=160,
                verbose_name="関連取引参照番号",
            ),
        ),
        migrations.AddField(
            model_name="receipt",
            name="financial_transaction_components",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text=(
                    "1つのPDFに含まれる元決済・返金・支払履歴を構造化して保存します。"
                    "カード明細照合ではファイル単位ではなく、この構成要素を一対一で使用します。"
                ),
                verbose_name="取引構成要素",
            ),
        ),
        migrations.AddField(
            model_name="receipt",
            name="financial_metadata_checked_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="金融メタデータ確認日時"),
        ),
        migrations.AlterField(
            model_name="cardstatementitem",
            name="match_status",
            field=models.CharField(
                choices=[
                    ("matched", "一致"),
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
            name="CardStatementReceiptEvidence",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("component_key", models.CharField(help_text="同じPDF内の元決済と返金を別々に識別するキーです。", max_length=160, verbose_name="構成要素キー")),
                ("role", models.CharField(choices=[("charge", "請求・支払"), ("refund", "返金")], default="charge", max_length=20, verbose_name="役割")),
                ("sequence", models.PositiveIntegerField(default=0, verbose_name="並び順")),
                ("signed_amount", models.DecimalField(decimal_places=2, max_digits=14, verbose_name="符号付き金額")),
                ("currency", models.CharField(max_length=3, verbose_name="通貨")),
                ("event_date", models.DateField(blank=True, null=True, verbose_name="取引日")),
                ("document_kind_snapshot", models.CharField(blank=True, max_length=20, verbose_name="書類区分")),
                ("filename_snapshot", models.CharField(max_length=255, verbose_name="ファイル名")),
                ("payee_snapshot", models.CharField(blank=True, max_length=160, verbose_name="払先")),
                ("invoice_number_snapshot", models.CharField(blank=True, max_length=160, verbose_name="請求書番号")),
                ("transaction_reference_snapshot", models.CharField(blank=True, max_length=160, verbose_name="取引参照番号")),
                ("related_transaction_reference_snapshot", models.CharField(blank=True, max_length=160, verbose_name="関連取引参照番号")),
                ("source_label", models.CharField(blank=True, max_length=120, verbose_name="抽出元")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="作成日時")),
                ("receipt", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="statement_evidences", to="receipts.receipt", verbose_name="証拠領収書")),
                ("statement_item", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="receipt_evidences", to="receipts.cardstatementitem", verbose_name="カード明細項目")),
            ],
            options={
                "verbose_name": "カード明細照合証拠",
                "verbose_name_plural": "カード明細照合証拠",
                "ordering": ["sequence", "id"],
            },
        ),
        migrations.AddConstraint(
            model_name="cardstatementreceiptevidence",
            constraint=models.UniqueConstraint(
                fields=("statement_item", "receipt", "component_key"),
                name="unique_statement_receipt_component_evidence",
            ),
        ),
        migrations.AddIndex(
            model_name="cardstatementreceiptevidence",
            index=models.Index(fields=["statement_item", "role"], name="stmt_ev_item_role_idx"),
        ),
        migrations.AddIndex(
            model_name="cardstatementreceiptevidence",
            index=models.Index(fields=["receipt", "component_key"], name="stmt_ev_receipt_comp_idx"),
        ),
        migrations.RunPython(prepare_existing_data, reverse_noop),
    ]
