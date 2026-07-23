from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("receipts", "0024_receipt_ai_resubmission_recommendation"),
    ]

    operations = [
        migrations.AddField(
            model_name="registeredservice",
            name="uses_p_card",
            field=models.BooleanField(
                default=True,
                help_text="OFFの場合、このサービスは領収書提出・リマインドメール・Pカード明細照合の対象外です。",
                verbose_name="Pカード利用",
            ),
        ),
        migrations.AddField(
            model_name="serviceexceptionrequest",
            name="uses_p_card",
            field=models.BooleanField(
                default=True,
                help_text="承認後にこのサービスの支払いへPカードを利用するかを記録します。",
                verbose_name="Pカード利用",
            ),
        ),
        migrations.AddField(
            model_name="receipt",
            name="p_card_usage_snapshot",
            field=models.BooleanField(
                default=True,
                help_text="登録サービスのPカード設定を後から変更しても、提出時点の監査情報を保持します。",
                verbose_name="提出時Pカード利用",
            ),
        ),
    ]
