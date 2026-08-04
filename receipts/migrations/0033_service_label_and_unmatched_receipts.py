from django.db import migrations, models
from django.db.models import Q


SERVICE_LABEL_RECONCILE_MARKER = (
    "【照合ルール更新】法的な払先と領収書本文のサービス名を分離し、"
    "Google One等をサービス名で照合するため再照合します。"
)


def prepare_existing_data(apps, schema_editor):
    Receipt = apps.get_model("receipts", "Receipt")
    CardStatement = apps.get_model("receipts", "CardStatement")

    # Googleの法的販売者名だけで生成された既存ファイル名を、新しい
    # 「法的販売者」と「本文上のサービス名」の分離ルールで再解析できるようにする。
    google_receipts = Receipt.objects.filter(admin_filename_overridden=False).filter(
        Q(ai_extracted_payee__icontains="Google Asia Pacific")
        | Q(generated_filename__icontains="Google_Asia_Pacific")
        | Q(original_filename__icontains="GOOGLEONE")
        | Q(original_filename__icontains="GOOGLE_ONE")
    )
    google_receipts.update(
        ai_extracted_service_label="",
        generated_filename="",
        ai_filename_status="not_processed",
        ai_filename_checked_at=None,
        financial_metadata_checked_at=None,
        financial_transaction_components=[],
    )

    # 保存済み明細はPDFをOpenAIへ再送せず、次回表示時に保存済み明細行と
    # 最新の領収書本文情報を使って再照合する。
    for statement in CardStatement.objects.exclude(status__in=["processing", "failed"]).iterator():
        memo = statement.ai_admin_memo or ""
        if SERVICE_LABEL_RECONCILE_MARKER not in memo:
            statement.ai_admin_memo = f"{memo} {SERVICE_LABEL_RECONCILE_MARKER}".strip()
            statement.save(update_fields=["ai_admin_memo"])


def reverse_prepare_existing_data(apps, schema_editor):
    # 再解析・再照合の結果を巻き戻すことはできないため、逆処理は行わない。
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("receipts", "0032_empirical_statement_reconciliation"),
    ]

    operations = [
        migrations.AddField(
            model_name="receipt",
            name="ai_extracted_service_label",
            field=models.CharField(
                blank=True,
                help_text="領収書本文に明示された製品・サービス・プラン名。法的な払先名とは分けて保存します。",
                max_length=160,
                verbose_name="AI抽出サービス名",
            ),
        ),
        migrations.AddField(
            model_name="cardstatement",
            name="unmatched_receipt_components",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="対象月に提出されたものの、カード明細のどの行にも使用されなかった領収書・取引構成要素です。カード明細側の誤記や別取引の確認に利用します。",
                verbose_name="明細未使用の提出証拠",
            ),
        ),
        migrations.AddField(
            model_name="cardstatementreceiptevidence",
            name="service_label_snapshot",
            field=models.CharField(blank=True, max_length=160, verbose_name="サービス名"),
        ),
        migrations.RunPython(prepare_existing_data, reverse_prepare_existing_data),
    ]
