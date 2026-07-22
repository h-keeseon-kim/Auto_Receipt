from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("receipts", "0022_cardstatementmatchcandidate"),
    ]

    operations = [
        migrations.AlterField(
            model_name="submission",
            name="status",
            field=models.CharField(
                choices=[
                    ("draft", "未提出領収書あり"),
                    ("submitted", "提出済み"),
                ],
                default="draft",
                max_length=20,
                verbose_name="ステータス",
            ),
        ),
        migrations.AddField(
            model_name="receipt",
            name="ai_extracted_recipient_name",
            field=models.CharField(blank=True, max_length=160, verbose_name="AI抽出利用者名（宛名）"),
        ),
        migrations.AddField(
            model_name="receipt",
            name="ai_check_recipient_name",
            field=models.BooleanField(default=False, verbose_name="AI確認: 利用者名（宛名）"),
        ),
        migrations.AddField(
            model_name="receipt",
            name="ai_recipient_name_check_memo",
            field=models.TextField(blank=True, verbose_name="AI利用者名（宛名）確認メモ"),
        ),
    ]
