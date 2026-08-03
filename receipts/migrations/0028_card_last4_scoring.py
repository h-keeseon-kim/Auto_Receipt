from django.db import migrations


RECONCILE_MARKER = (
    "【照合ルール更新】領収書のカード末尾を必須条件から補助加点へ変更したため、"
    "最新の領収書と再照合します。"
)
RESET_MEMO = (
    "旧ルールではカード末尾の未記載を理由にAI確認が保留されていたため、"
    "カード末尾を任意項目として再検査できる未確認状態へ戻しました。"
)


def apply_card_last4_scoring(apps, schema_editor):
    CardStatement = apps.get_model("receipts", "CardStatement")
    Receipt = apps.get_model("receipts", "Receipt")

    # 保存済み明細行と領収書を、新しい補助加点ルールで一度だけ再照合する。
    # 明細PDFをOpenAIへ再送信する処理ではない。
    queryset = CardStatement.objects.exclude(status__in=["processing", "failed"])
    for statement in queryset.iterator():
        memo = (statement.ai_admin_memo or "").strip()
        if RECONCILE_MARKER in memo:
            continue
        if memo:
            available = max(0, 5000 - len(RECONCILE_MARKER) - 1)
            statement.ai_admin_memo = f"{memo[:available]} {RECONCILE_MARKER}"
        else:
            statement.ai_admin_memo = RECONCILE_MARKER
        statement.save(update_fields=["ai_admin_memo"])

    # 旧プロンプトで「カード末尾が記載されていない」ことを理由にファイル名作成が
    # 保留された未確定領収書だけを、管理者が次回のAI実行ボタンで再検査できる状態へ戻す。
    # 既に生成済み・管理者確認済み・再提出候補の領収書は変更しない。
    Receipt.objects.filter(
        ai_filename_status="needs_review",
        generated_filename="",
        ai_extracted_card_last4="",
        admin_review_status="not_reviewed",
        ai_resubmission_recommended=False,
        ai_filename_admin_memo__contains="カード末尾",
    ).update(
        ai_filename_status="not_processed",
        ai_filename_checked_at=None,
        ai_filename_admin_memo=RESET_MEMO,
        ai_check_card_last4=False,
    )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("receipts", "0027_statement_match_gates_and_priorities"),
    ]

    operations = [
        migrations.RunPython(apply_card_last4_scoring, noop_reverse),
    ]
