from django.db import migrations
from django.db.models import Q


REQUEUE_MEMO = (
    "v1.10.4でPDF埋め込みテキスト補完と部分抽出値の保存方式を修正したため、"
    "金額・日付・通貨が未確認の領収書を再度AI検査できる状態へ戻しました。"
)


def requeue_incomplete_receipts(apps, schema_editor):
    Receipt = apps.get_model("receipts", "Receipt")
    (
        Receipt.objects.filter(
            file_deleted_at__isnull=True,
            admin_review_status="not_reviewed",
            ai_filename_status__in=["generated", "needs_review", "failed", "skipped"],
        )
        .exclude(file="")
        .filter(Q(ai_check_amount=False) | Q(ai_check_date=False) | Q(ai_check_currency=False))
        .update(
            generated_filename="",
            ai_filename_status="not_processed",
            ai_filename_admin_memo=REQUEUE_MEMO,
            ai_filename_checked_at=None,
            ai_resubmission_recommended=False,
            ai_resubmission_recommendation_memo="",
        )
    )


class Migration(migrations.Migration):
    dependencies = [
        ("receipts", "0029_exact_amount_matching"),
    ]

    operations = [
        migrations.RunPython(requeue_incomplete_receipts, migrations.RunPython.noop),
    ]
