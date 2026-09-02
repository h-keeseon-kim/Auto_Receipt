from __future__ import annotations

import hashlib
import json
import unicodedata
from decimal import Decimal, InvalidOperation

from django.db import migrations, models
from django.db.models import Q


RECONCILE_MARKER = (
    "【照合使用履歴更新】取引構成要素の全明細共通フィンガープリントと消費・参照区分を導入し、"
    "過去明細で使用済みの証拠を再利用しないため再照合します。"
)
REVERSAL_MARKERS = ("返品", "取消", "キャンセル", "RETURN", "REVERSAL", "REFUND")


def _normalise(value) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).upper().strip()
    return "".join(character for character in text if character.isalnum())


def _amount(value) -> str:
    try:
        return format(Decimal(str(value)).quantize(Decimal("0.01")), "f")
    except (InvalidOperation, TypeError, ValueError):
        return ""


def _date(value) -> str:
    return value.isoformat() if value else ""


def _digest(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _source_component_key(component_key: str, receipt_id) -> str:
    value = str(component_key or "")
    prefix = f"receipt-{receipt_id}:" if receipt_id else ""
    if prefix and value.startswith(prefix):
        return value[len(prefix):]
    if value.startswith("receipt-") and ":" in value:
        return value.split(":", 1)[1]
    return value


def _explicit_fingerprint(evidence) -> str:
    """Backfill identities that do not require opening historical files.

    Rows without an invoice/transaction/related reference remain blank and are
    upgraded lazily by the runtime using the receipt file SHA-256. This keeps the
    migration safe when an old file has already expired from storage.
    """

    role = str(evidence.role or "charge").lower()
    invoice = _normalise(evidence.invoice_number_snapshot)
    transaction = _normalise(evidence.transaction_reference_snapshot)
    related = _normalise(evidence.related_transaction_reference_snapshot)
    common = {
        "version": 1,
        "merchant": _normalise(evidence.payee_snapshot),
        "role": role,
        "amount": _amount(evidence.signed_amount),
        "currency": str(evidence.currency or "").upper()[:3],
    }
    if invoice:
        payload = {**common, "identity": "invoice", "invoice": invoice}
        if role == "refund":
            payload.update({"event_date": _date(evidence.event_date), "transaction": transaction})
        return _digest(payload)
    if transaction:
        return _digest(
            {
                **common,
                "identity": "transaction",
                "transaction": transaction,
                "event_date": _date(evidence.event_date) if role == "refund" else "",
            }
        )
    if related:
        return _digest(
            {
                **common,
                "identity": "related",
                "related": related,
                "event_date": _date(evidence.event_date),
            }
        )
    return ""


def _is_reversal_item(item) -> bool:
    text = unicodedata.normalize(
        "NFKC",
        " ".join(
            str(value or "")
            for value in (item.merchant_name, item.merchant_normalized, item.match_memo)
        ),
    ).upper()
    return any(marker in text for marker in REVERSAL_MARKERS)


def backfill_global_component_usage(apps, schema_editor):
    Evidence = apps.get_model("receipts", "CardStatementReceiptEvidence")
    Statement = apps.get_model("receipts", "CardStatement")

    rows = list(
        Evidence.objects.select_related("statement_item__statement")
        .order_by(
            "statement_item__statement__period_month",
            "statement_item__statement__uploaded_at",
            "statement_item__sequence",
            "pk",
        )
    )
    consume_owner: dict[str, int] = {}
    statements_to_reconcile: set[int] = set()

    for evidence in rows:
        item = evidence.statement_item
        usage_mode = "reference" if _is_reversal_item(item) and evidence.role == "charge" else "consume"
        fingerprint = _explicit_fingerprint(evidence)

        if usage_mode == "consume" and fingerprint:
            existing_owner = consume_owner.get(fingerprint)
            if existing_owner is not None:
                is_manual = bool(
                    item.match_confidence >= 1.0
                    and str(item.match_memo or "").startswith("管理者")
                )
                if is_manual:
                    # Preserve a historical manual override. Leaving the stable
                    # key blank prevents the new constraint from deleting or
                    # mutating an administrator decision; the statement is
                    # explicitly queued for review/reconciliation.
                    fingerprint = ""
                else:
                    usage_mode = "reference"
                statements_to_reconcile.add(item.statement_id)
            else:
                consume_owner[fingerprint] = evidence.pk

        Evidence.objects.filter(pk=evidence.pk).update(
            component_fingerprint=fingerprint,
            usage_mode=usage_mode,
        )

    statements = Statement.objects.exclude(status__in=["processing", "failed"])
    for statement in statements.iterator():
        memo = str(statement.ai_admin_memo or "")
        if RECONCILE_MARKER not in memo:
            memo = f"{RECONCILE_MARKER} {memo}".strip()
        Statement.objects.filter(pk=statement.pk).update(
            ai_admin_memo=memo[:5000],
            reconciled_at=None,
            unmatched_receipt_components=[],
        )


def reverse_backfill(apps, schema_editor):
    Evidence = apps.get_model("receipts", "CardStatementReceiptEvidence")
    Evidence.objects.update(component_fingerprint="", usage_mode="consume")


class Migration(migrations.Migration):
    dependencies = [
        ("receipts", "0040_unmatched_receipt_event_scope"),
    ]

    operations = [
        migrations.AddField(
            model_name="receipt",
            name="file_sha256",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="同一PDFの再アップロードを識別し、取引構成要素の二重使用を防止します。",
                max_length=64,
                verbose_name="ファイルSHA-256",
            ),
        ),
        migrations.AddField(
            model_name="cardstatementreceiptevidence",
            name="component_fingerprint",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="再アップロードや月跨ぎでも同じ金融イベントを識別する安定キーです。",
                max_length=64,
                verbose_name="構成要素フィンガープリント",
            ),
        ),
        migrations.AddField(
            model_name="cardstatementreceiptevidence",
            name="usage_mode",
            field=models.CharField(
                choices=[("consume", "金額計算に使用"), ("reference", "参照のみ")],
                default="consume",
                help_text="金額計算に消費した証拠か、返品元として参照しただけかを区別します。",
                max_length=16,
                verbose_name="使用方法",
            ),
        ),
        migrations.RunPython(backfill_global_component_usage, reverse_backfill),
        migrations.AddConstraint(
            model_name="cardstatementreceiptevidence",
            constraint=models.UniqueConstraint(
                condition=Q(usage_mode="consume") & ~Q(component_fingerprint=""),
                fields=("component_fingerprint",),
                name="uniq_consumed_component_fp",
            ),
        ),
        migrations.AddIndex(
            model_name="cardstatementreceiptevidence",
            index=models.Index(
                fields=["component_fingerprint", "usage_mode"],
                name="stmt_ev_fp_usage_idx",
            ),
        ),
    ]
