from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("receipts", "0025_p_card_usage"),
    ]

    operations = [
        migrations.AlterField(
            model_name="emaildeliverylog",
            name="email_type",
            field=models.CharField(
                choices=[
                    ("reminder_initial", "通常リマインダー"),
                    ("reminder_urgent", "重要リマインダー"),
                    ("resubmission_request", "再提出依頼"),
                    ("test", "テスト送信"),
                ],
                max_length=40,
                verbose_name="メール種別",
            ),
        ),
    ]
