from django.db import migrations, models


def classify_existing_rows(apps, schema_editor):
    CardStatement = apps.get_model("receipts", "CardStatement")
    CardStatementItem = apps.get_model("receipts", "CardStatementItem")
    CardStatementMatchCandidate = apps.get_model("receipts", "CardStatementMatchCandidate")

    for item in CardStatementItem.objects.all().iterator():
        memo = (item.match_memo or "").strip()
        if item.match_status == "ignored":
            reason = "ignored"
        elif item.match_confidence >= 1.0 and memo.startswith("管理者"):
            reason = "manual_confirmed"
        elif item.matched_receipt_id and item.match_status == "matched":
            reason = "auto_strong"
        elif item.matched_receipt_id and item.match_status == "ambiguous":
            reason = "auto_amount_only"
        elif item.matched_service_id:
            reason = "service_only"
        elif item.match_status == "ambiguous":
            reason = "insufficient_evidence"
        else:
            reason = "no_compatible_receipt"
        CardStatementItem.objects.filter(pk=item.pk).update(match_reason_code=reason)

    CardStatementMatchCandidate.objects.filter(strength="strong").update(
        gate_status="auto_eligible",
        priority_tier="exact_identity",
        gate_memo="既存候補をv1.10.0の必須条件・優先順位方式へ移行しました。最新の領収書と再照合してください。",
    )
    CardStatementMatchCandidate.objects.filter(strength="amount_only").update(
        gate_status="auto_eligible",
        priority_tier="exact_amount_only",
        gate_memo="既存の金額候補をv1.10.0の優先順位方式へ移行しました。最新の領収書と再照合してください。",
    )
    CardStatementMatchCandidate.objects.filter(strength="possible").update(
        gate_status="manual_only",
        priority_tier="identity_only",
        gate_memo="既存の参考候補を手動確認候補として移行しました。最新の領収書と再照合してください。",
    )

    marker = "【照合ルール更新】必須条件・優先順位方式へ更新したため、最新の領収書と再照合します。"
    for statement in CardStatement.objects.exclude(status__in=["processing", "failed"]).iterator():
        memo = (statement.ai_admin_memo or "").strip()
        if marker not in memo:
            CardStatement.objects.filter(pk=statement.pk).update(
                ai_admin_memo=" ".join(part for part in (memo, marker) if part)[:5000]
            )


class Migration(migrations.Migration):

    dependencies = [
        ("receipts", "0026_email_resubmission_request"),
    ]

    operations = [
        migrations.AddField(
            model_name="cardstatementitem",
            name="match_reason_code",
            field=models.CharField(
                choices=[
                    ("auto_strong", "必須条件一致"),
                    ("auto_amount_only", "金額一致・関係要確認"),
                    ("multiple_compatible", "適合候補複数"),
                    ("receipt_competition", "同一領収書が複数明細で競合"),
                    ("user_ambiguous", "利用者未特定"),
                    ("insufficient_evidence", "情報不足・手動確認"),
                    ("service_only", "サービス特定・領収書未提出"),
                    ("no_compatible_receipt", "適合する領収書なし"),
                    ("manual_confirmed", "管理者確定"),
                    ("ignored", "対象外"),
                ],
                default="no_compatible_receipt",
                max_length=40,
                verbose_name="判定理由",
            ),
        ),
        migrations.AddField(
            model_name="cardstatementmatchcandidate",
            name="gate_memo",
            field=models.TextField(blank=True, verbose_name="必須条件メモ"),
        ),
        migrations.AddField(
            model_name="cardstatementmatchcandidate",
            name="gate_status",
            field=models.CharField(
                choices=[
                    ("auto_eligible", "自動照合対象"),
                    ("manual_only", "手動確認のみ"),
                    ("rejected", "必須条件不一致"),
                ],
                default="manual_only",
                max_length=20,
                verbose_name="必須条件判定",
            ),
        ),
        migrations.AddField(
            model_name="cardstatementmatchcandidate",
            name="priority_tier",
            field=models.CharField(
                choices=[
                    ("exact_identity", "優先度1: 金額・通貨＋サービス/払先一致"),
                    ("exact_amount_only", "優先度2: 金額・通貨一意"),
                    ("near_identity", "優先度3: 金額近似＋サービス/払先一致"),
                    ("identity_only", "優先度4: サービス/払先一致・金額不足"),
                    ("rejected", "除外: 必須条件不一致"),
                ],
                default="identity_only",
                max_length=30,
                verbose_name="優先順位",
            ),
        ),
        migrations.RunPython(classify_existing_rows, migrations.RunPython.noop),
    ]
