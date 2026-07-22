from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("receipts", "0023_receipt_recipient_check_and_status_label"),
    ]

    operations = [
        migrations.AddField(
            model_name="receipt",
            name="ai_resubmission_recommended",
            field=models.BooleanField(default=False, verbose_name="AI再提出候補"),
        ),
        migrations.AddField(
            model_name="receipt",
            name="ai_resubmission_recommendation_memo",
            field=models.TextField(blank=True, verbose_name="AI再提出候補メモ"),
        ),
    ]
