# Generated for ReceiptHub tutorial completion tracking

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("receipts", "0007_receipt_ai_check_amount_receipt_ai_check_card_last4_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="tutorial_completed_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="チュートリアル完了日時"),
        ),
    ]
