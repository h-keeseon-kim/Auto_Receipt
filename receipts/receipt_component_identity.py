from __future__ import annotations

"""Stable identity helpers for financial components extracted from receipts.

A database primary key is an upload identity, not a financial transaction
identity.  The same PDF can be uploaded again under another Receipt row and a
refund PDF can contain more than one financial event.  This module generates a
stable fingerprint per *financial component* so that monetary evidence already
consumed by one card statement cannot be consumed again by another statement.
"""

import hashlib
import json
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any


def normalise_identity_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).upper().strip()
    return "".join(character for character in normalized if character.isalnum())


def canonical_amount(value: Any) -> str:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return ""
    return format(amount, "f")


def canonical_date(value: date | str | None) -> str:
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    return text[:10] if text else ""


def _digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def component_fingerprint(
    *,
    merchant_key: str = "",
    payee: str = "",
    role: str,
    signed_amount: Any,
    currency: str,
    event_date: date | str | None = None,
    invoice_number: str = "",
    transaction_id: str = "",
    related_transaction_id: str = "",
    file_sha256: str = "",
    source_component_key: str = "",
    receipt_id: int | str | None = None,
) -> str:
    """Return a stable SHA-256 fingerprint for one monetary component.

    Identity priority is deliberately conservative:

    * A charge with an invoice number is identified by that invoice, canonical
      merchant family, currency and amount.  This intentionally treats the paid receipt and the
      original-payment row embedded in a later refund PDF as the same charge.
    * A refund with a credit-note/invoice number includes its event date and
      amount so multiple credits against the same original invoice remain
      separate.
    * When no explicit financial reference exists, the immutable PDF digest and
      canonical financial signature are used.  Extraction-local component labels
      are deliberately excluded so re-analysis or rename cannot bypass the lock.
    * The Receipt primary key is only the final fallback for legacy/deleted
      files whose bytes can no longer be hashed.
    """

    role_value = str(role or "charge").strip().lower()
    currency_value = str(currency or "").strip().upper()[:3]
    amount_value = canonical_amount(signed_amount)
    merchant_value = normalise_identity_text(merchant_key) or normalise_identity_text(payee)
    invoice_value = normalise_identity_text(invoice_number)
    transaction_value = normalise_identity_text(transaction_id)
    related_value = normalise_identity_text(related_transaction_id)
    date_value = canonical_date(event_date)
    component_value = normalise_identity_text(source_component_key)
    file_hash_value = normalise_identity_text(file_sha256)

    common = {
        "version": 2,
        "merchant": merchant_value,
        "role": role_value,
        "amount": amount_value,
        "currency": currency_value,
    }

    if invoice_value:
        payload = {
            **common,
            "identity": "invoice",
            "invoice": invoice_value,
        }
        if role_value == "refund":
            # Credit-note numbers are normally unique, but date and transaction
            # reference protect against providers that repeat the base invoice.
            payload.update(
                {
                    "event_date": date_value,
                    "transaction": transaction_value,
                    "related": related_value,
                }
            )
        return _digest(payload)

    if transaction_value:
        return _digest(
            {
                **common,
                "identity": "transaction",
                "transaction": transaction_value,
                "event_date": date_value if role_value == "refund" else "",
            }
        )

    if related_value:
        return _digest(
            {
                **common,
                "identity": "related",
                "related": related_value,
                "event_date": date_value,
            }
        )

    if file_hash_value:
        # Component labels are extraction-local and can change after an AI
        # re-analysis (for example ``primary`` -> ``payment-history``).  The
        # immutable file digest together with merchant/role/amount/currency/date
        # is therefore the stable identity.  If two no-ID rows in the same PDF
        # have the exact same financial signature, treat them conservatively as
        # duplicate evidence rather than risk double-spending one transaction.
        return _digest(
            {
                **common,
                "identity": "file-financial-signature",
                "file_sha256": file_hash_value,
                "event_date": date_value,
            }
        )

    return _digest(
        {
            **common,
            "identity": "legacy-receipt-component",
            "receipt_id": str(receipt_id or ""),
            "event_date": date_value,
            "component": component_value,
        }
    )


def source_component_key(component_key: str, receipt_id: int | str | None = None) -> str:
    """Strip the historical ``receipt-<pk>:`` prefix from a local key."""

    value = str(component_key or "")
    prefix = f"receipt-{receipt_id}:" if receipt_id not in (None, "") else ""
    if prefix and value.startswith(prefix):
        return value[len(prefix):]
    if value.startswith("receipt-") and ":" in value:
        return value.split(":", 1)[1]
    return value
