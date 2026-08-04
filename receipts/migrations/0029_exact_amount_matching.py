from django.db import migrations, models


RECONCILE_MARKER = (
    "【照合ルール更新】金額照合を許容差なしの完全一致へ変更したため、"
    "最新の領収書と再照合します。"
)
LEGACY_CANDIDATE_MEMO = (
    "旧ルールの金額近似または金額未抽出候補です。v1.10.2以降は金額・通貨の"
    "完全一致を確認できない領収書を曖昧候補へ含めません。最新の領収書と再照合してください。"
)


def apply_exact_amount_matching(apps, schema_editor):
    CardStatement = apps.get_model("receipts", "CardStatement")
    CardStatementMatchCandidate = apps.get_model("receipts", "CardStatementMatchCandidate")
    Receipt = apps.get_model("receipts", "Receipt")

    # 旧ルールで作成された近似金額・金額不足候補を、再照合前でも互換候補として
    # 表示しないよう必須条件不一致へ移す。管理者が明示的に確定済みの明細行そのものは
    # ここでは変更せず、既存の手動判断を維持する。
    CardStatementMatchCandidate.objects.filter(
        priority_tier__in=["near_identity", "identity_only"]
    ).update(
        priority_tier="rejected",
        gate_status="rejected",
        strength="possible",
        confidence=0,
        amount_match=False,
        amount_match_basis="",
        gate_memo=LEGACY_CANDIDATE_MEMO,
    )

    # 金額を抽出できていない未確認領収書は、次回の管理者AI実行で再抽出できるようにする。
    # デプロイ時にOpenAI APIを自動実行するものではない。管理者確認済み・処理中の
    # 領収書は変更しない。
    Receipt.objects.filter(
        amount__isnull=True,
        admin_review_status="not_reviewed",
    ).exclude(
        ai_filename_status__in=["queued", "processing"]
    ).update(
        ai_filename_status="not_processed",
        ai_filename_checked_at=None,
        generated_filename="",
        ai_filename_admin_memo=(
            "ご利用代金明細との完全一致判定に必要な金額を抽出できていないため、"
            "次回のAI検査対象へ戻しました。"
        ),
        ai_check_amount=False,
        ai_check_currency=False,
    )

    # 保存済みの明細行・領収書だけで、新しい完全一致ルールを一度適用する。
    # 明細PDFをOpenAIへ再送信する処理ではない。
    for statement in CardStatement.objects.exclude(status__in=["processing", "failed"]).iterator():
        memo = (statement.ai_admin_memo or "").strip()
        if RECONCILE_MARKER in memo:
            continue
        CardStatement.objects.filter(pk=statement.pk).update(
            ai_admin_memo=" ".join(part for part in (memo, RECONCILE_MARKER) if part)[:5000]
        )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("receipts", "0028_card_last4_scoring"),
    ]

    operations = [
        migrations.RunPython(apply_exact_amount_matching, noop_reverse),
        migrations.AlterField(
            model_name="cardstatementmatchcandidate",
            name="priority_tier",
            field=models.CharField(
                choices=[
                    ("exact_identity", "優先度1: 金額・通貨完全一致＋サービス/払先一致"),
                    ("exact_amount_only", "優先度2: 金額・通貨完全一致・関係要確認"),
                    ("rejected", "除外: 金額未確認または必須条件不一致"),
                ],
                default="rejected",
                max_length=30,
                verbose_name="優先順位",
            ),
        ),
    ]
