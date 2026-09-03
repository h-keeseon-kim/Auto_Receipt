from __future__ import annotations

import hashlib
import json
import unicodedata
from decimal import Decimal, InvalidOperation

from django.db import migrations, models
from django.db.models import Q
from django.utils import timezone


RECONCILE_MARKER = (
    "【全明細使用履歴更新】取引構成要素の安定フィンガープリントを再構築し、"
    "前月・当月・翌月を含む全カード明細で金額証拠を一度だけ使用するため再照合します。"
)

KNOWN_MERCHANT_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("GOOGLE_CLOUD", ("GOOGLECLOUD", "GCLOUD", "GOOGLECLOUDPLATFORM")),
    ("GOOGLE_ONE", ("GOOGLEGOOGLEON", "GOOGLEONE", "GOOGLEAIULTRA")),
    ("GOOGLE_PLAY", ("GOOGLEPLAY",)),
    ("ANTHROPIC", ("ANTHROPIC", "CLAUDE")),
    ("OPENAI", ("OPENAI", "CHATGPT")),
    ("AUDIOSHAKE", ("AUDIOSHAKE",)),
    ("JETBRAINS", ("JETBRAINS",)),
    ("GITHUB", ("GITHUB",)),
    ("RAILWAY", ("RAILWAY",)),
    ("CURSOR", ("CURSOR", "ANYSPHERE")),
    ("DIFY", ("DIFY", "LANGGENIUS")),
    ("SUNO", ("SUNO",)),
    ("GROK", ("GROK", "XAI")),
    ("FIGMA", ("FIGMA",)),
)

REVERSAL_MERCHANT_MARKERS = (
    "返品",
    "取消",
    "キャンセル",
    "RETURN",
    "REVERSAL",
    "CREDITREVERSAL",
)
REVERSAL_MEMO_MARKERS = (
    "返品元決済確認",
    "カード明細の返品取消行",
    "同日返品元決済",
)


def _normalise(value) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).upper().strip()
    return "".join(character for character in text if character.isalnum())


def _merchant_key(*values) -> str:
    for value in values:
        normalized = _normalise(value)
        if not normalized:
            continue
        for key, patterns in KNOWN_MERCHANT_PATTERNS:
            if any(pattern in normalized for pattern in patterns):
                return key
    for value in values:
        normalized = _normalise(value)
        if normalized:
            return normalized
    return ""


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
    """Build the v2 identity without reading files from historical storage."""

    role = str(evidence.role or "charge").lower()
    invoice = _normalise(evidence.invoice_number_snapshot)
    transaction = _normalise(evidence.transaction_reference_snapshot)
    related = _normalise(evidence.related_transaction_reference_snapshot)
    source_key = _normalise(_source_component_key(evidence.component_key, evidence.receipt_id))
    file_sha256 = _normalise(
        getattr(getattr(evidence, "receipt", None), "file_sha256", "")
    )
    common = {
        "version": 2,
        "merchant": _merchant_key(
            evidence.service_label_snapshot,
            evidence.payee_snapshot,
        ),
        "role": role,
        "amount": _amount(evidence.signed_amount),
        "currency": str(evidence.currency or "").upper()[:3],
    }
    if invoice:
        payload = {**common, "identity": "invoice", "invoice": invoice}
        if role == "refund":
            payload.update(
                {
                    "event_date": _date(evidence.event_date),
                    "transaction": transaction,
                    "related": related,
                }
            )
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
    if file_sha256:
        return _digest(
            {
                **common,
                "identity": "file-financial-signature",
                "file_sha256": file_sha256,
                "event_date": _date(evidence.event_date),
            }
        )
    # Runtime reconciliation can hash an available historical file lazily. Do
    # not fall back to a Receipt PK here because a re-upload would bypass the
    # global use lock.
    return ""


def _is_manual(item) -> bool:
    return bool(
        float(item.match_confidence or 0) >= 1.0
        and str(item.match_memo or "").startswith("管理者")
    )


def _is_true_reversal(item) -> bool:
    for amount in (item.original_amount, item.amount_jpy):
        if amount is not None and amount < 0:
            return True
    merchant = _normalise(f"{item.merchant_name or ''} {item.merchant_normalized or ''}")
    if any(_normalise(marker) in merchant for marker in REVERSAL_MERCHANT_MARKERS):
        return True
    memo = _normalise(item.match_memo)
    return any(_normalise(marker) in memo for marker in REVERSAL_MEMO_MARKERS)


def _statement_order(evidence):
    statement = evidence.statement_item.statement
    return (
        statement.period_month,
        statement.uploaded_at,
        statement.pk,
        evidence.statement_item.sequence,
        evidence.statement_item.pk,
        evidence.sequence,
        evidence.pk,
    )


def rebuild_global_component_usage(apps, schema_editor):
    """Rebuild global ownership using bulk SQL operations.

    The original v1.16.2 migration issued one UPDATE per evidence row and one
    UPDATE per statement. Moving migrations to Railway pre-deploy prevents an
    HTTP healthcheck timeout, while this bulk implementation also makes the
    migration itself substantially faster and easier to observe in deploy logs.
    """

    Evidence = apps.get_model("receipts", "CardStatementReceiptEvidence")
    Item = apps.get_model("receipts", "CardStatementItem")
    Inference = apps.get_model("receipts", "CardStatementPlanChangeInference")
    Statement = apps.get_model("receipts", "CardStatement")

    rows = list(
        Evidence.objects.select_related("receipt", "statement_item__statement")
        .order_by(
            "statement_item__statement__period_month",
            "statement_item__statement__uploaded_at",
            "statement_item__statement_id",
            "statement_item__sequence",
            "pk",
        )
    )
    print(f"ReceiptHub 0042: examining {len(rows)} evidence rows.")

    fingerprints: dict[int, str] = {}
    modes: dict[int, str] = {}
    grouped_consumers: dict[str, list] = {}
    for evidence in rows:
        fingerprint = _explicit_fingerprint(evidence)
        usage_mode = (
            "reference"
            if evidence.role == "charge" and _is_true_reversal(evidence.statement_item)
            else "consume"
        )
        fingerprints[evidence.pk] = fingerprint
        modes[evidence.pk] = usage_mode
        if fingerprint and usage_mode == "consume":
            grouped_consumers.setdefault(fingerprint, []).append(evidence)

    automatic_loser_item_ids: set[int] = set()
    manual_conflict_evidence_ids: set[int] = set()
    for consumers in grouped_consumers.values():
        consumers.sort(key=_statement_order)
        manual_consumers = [row for row in consumers if _is_manual(row.statement_item)]
        if manual_consumers:
            manual_consumers.sort(key=_statement_order)
            winner = manual_consumers[0]
            for conflict in manual_consumers[1:]:
                manual_conflict_evidence_ids.add(conflict.pk)
            for row in consumers:
                if row.pk == winner.pk or row.pk in manual_conflict_evidence_ids:
                    continue
                automatic_loser_item_ids.add(row.statement_item_id)
        else:
            winner = consumers[0]
            for row in consumers[1:]:
                automatic_loser_item_ids.add(row.statement_item_id)

    # A net calculation becomes invalid when even one component loses global
    # ownership. Remove the full automatic evidence group in one database pass.
    if automatic_loser_item_ids:
        Evidence.objects.filter(statement_item_id__in=automatic_loser_item_ids).delete()
        Inference.objects.filter(statement_item_id__in=automatic_loser_item_ids).delete()
        Item.objects.filter(pk__in=automatic_loser_item_ids).update(
            matched_user=None,
            matched_catalog_service=None,
            matched_service=None,
            matched_receipt=None,
            match_status="unmatched",
            match_reason_code="no_compatible_receipt",
            match_confidence=0.0,
            match_memo=(
                "【全明細使用履歴】同じ取引構成要素がより前のカード明細で使用済みのため、"
                "旧自動割当を解放しました。最新の領収書と再照合してください。"
            ),
        )

    evidence_updates = []
    manual_item_warnings: dict[int, str] = {}
    for evidence in rows:
        if evidence.statement_item_id in automatic_loser_item_ids:
            continue
        evidence.component_fingerprint = (
            "" if evidence.pk in manual_conflict_evidence_ids else fingerprints[evidence.pk]
        )
        evidence.usage_mode = modes[evidence.pk]
        evidence_updates.append(evidence)

        if evidence.pk in manual_conflict_evidence_ids:
            item = evidence.statement_item
            memo = str(item.match_memo or "")
            warning = (
                "【全明細使用履歴・管理者確認】同じ金融イベントを複数の管理者確定明細が使用しています。"
                "どちらを正しい所有先とするか確認してください。"
            )
            if warning not in memo:
                manual_item_warnings[item.pk] = f"{warning} {memo}"[:4000]

    if evidence_updates:
        Evidence.objects.bulk_update(
            evidence_updates,
            ["component_fingerprint", "usage_mode"],
            batch_size=1000,
        )

    if manual_item_warnings:
        warning_items = list(Item.objects.filter(pk__in=manual_item_warnings))
        for item in warning_items:
            item.match_memo = manual_item_warnings[item.pk]
        Item.objects.bulk_update(warning_items, ["match_memo"], batch_size=500)

    now = timezone.now()
    statement_updates = []
    for statement in Statement.objects.exclude(
        status__in=["processing", "failed"]
    ).iterator(chunk_size=500):
        memo = str(statement.ai_admin_memo or "")
        if RECONCILE_MARKER not in memo:
            memo = f"{RECONCILE_MARKER} {memo}".strip()
        statement.ai_admin_memo = memo[:5000]
        statement.reconciled_at = None
        statement.unmatched_receipt_components = []
        statement.updated_at = now
        statement_updates.append(statement)

        if len(statement_updates) >= 500:
            Statement.objects.bulk_update(
                statement_updates,
                [
                    "ai_admin_memo",
                    "reconciled_at",
                    "unmatched_receipt_components",
                    "updated_at",
                ],
                batch_size=500,
            )
            statement_updates.clear()

    if statement_updates:
        Statement.objects.bulk_update(
            statement_updates,
            [
                "ai_admin_memo",
                "reconciled_at",
                "unmatched_receipt_components",
                "updated_at",
            ],
            batch_size=500,
        )

    print(
        "ReceiptHub 0042: global usage rebuild complete; "
        f"released {len(automatic_loser_item_ids)} conflicting automatic item(s), "
        f"flagged {len(manual_conflict_evidence_ids)} manual conflict(s)."
    )

def reverse_rebuild(apps, schema_editor):
    Evidence = apps.get_model("receipts", "CardStatementReceiptEvidence")
    Evidence.objects.update(component_fingerprint="", usage_mode="consume")


class Migration(migrations.Migration):
    dependencies = [
        ("receipts", "0041_global_component_usage_ledger"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="cardstatementreceiptevidence",
            name="uniq_consumed_component_fp",
        ),
        migrations.RunPython(rebuild_global_component_usage, reverse_rebuild),
        migrations.AddConstraint(
            model_name="cardstatementreceiptevidence",
            constraint=models.UniqueConstraint(
                condition=Q(usage_mode="consume") & ~Q(component_fingerprint=""),
                fields=("component_fingerprint",),
                name="uniq_consumed_component_fp",
            ),
        ),
        migrations.AlterField(
            model_name="cardstatement",
            name="unmatched_receipt_components",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text=(
                    "対象月または実明細に関連する月跨ぎPDFのうち、全カード明細を通じて金額計算に未使用の"
                    "取引構成要素が残る書類です。部分使用済みPDFは構成要素ごとの使用先も保存します。"
                ),
                verbose_name="全明細で未消費の提出証拠",
            ),
        ),
    ]
