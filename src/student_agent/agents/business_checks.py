from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


def money(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() and result >= 0 else None


def _amount(row: dict[str, Any]) -> Decimal | None:
    for key in ("amount_brl", "amount", "value", "transaction_amount"):
        if key in row:
            return money(row[key])
    return None


def reconcile_payments(payment_data, refund_data, *, payments_complete=True, refunds_complete=True):
    """Reconcile observations by transaction identity; unknown amounts remain unknown.

    Distinct transactions sharing an authorization are duplicate captures. Repeated
    observations of one transaction are not additional money movements.
    """

    def collect(rows, refund=False):
        records = {}
        complete = True
        pending = failed = conflict = False
        for row in rows:
            status = str(
                row.get("status")
                or row.get("refund_status" if refund else "payment_status")
                or row.get("type")
                or ""
            ).lower()
            pending |= refund and status in {"pending", "processing", "requested"}
            failed |= refund and status in {"failed", "rejected", "denied"}
            successful = (
                {"refunded", "completed", "success", "succeeded"}
                if refund
                else {"capture", "captured", "paid", "approved", "completed"}
            )
            ignored = {
                "pending",
                "processing",
                "requested",
                "failed",
                "rejected",
                "denied",
                "authorized",
                "authorization",
                "cancelled",
                "canceled",
            }
            if status in ignored:
                continue
            if status not in successful:
                complete = False
                continue
            identity = (row.get("refund_id") if refund else None) or row.get("transaction_id")
            identity = identity or (None if refund else row.get("payment_id"))
            amount = _amount(row)
            if identity is None or amount is None:
                complete = False
                continue
            key = (row.get("_order_id", row.get("order_id")), str(identity))
            if key in records and records[key][0] != amount:
                conflict = True
                complete = False
            records[key] = (amount, row.get("authorization_code"))
        return records, complete, pending, failed, conflict

    captures, pc, _, _, capture_conflict = collect(payment_data)
    refunds, rc, pending, failed, refund_conflict = collect(refund_data, True)
    captured = (
        sum((x[0] for x in captures.values()), Decimal(0)) if pc and payments_complete else None
    )
    refunded = (
        sum((x[0] for x in refunds.values()), Decimal(0)) if rc and refunds_complete else None
    )
    authorizations = [(key[0], value[1]) for key, value in captures.items() if value[1]]
    duplicate = len(authorizations) != len(set(authorizations))
    mismatch = (
        capture_conflict
        or refund_conflict
        or (captured is not None and refunded is not None and refunded > captured)
    )
    refundable = (
        captured - refunded
        if captured is not None
        and refunded is not None
        and not mismatch
        and not pending
        and not failed
        else None
    )
    if mismatch:
        verdict = "capture_mismatch"
    elif captured is None or refunded is None:
        verdict = "insufficient_evidence"
    elif duplicate:
        verdict = "duplicate_capture"
    elif failed:
        verdict = "refund_failed"
    elif pending:
        verdict = "refund_pending"
    elif refunded:
        verdict = "refunded"
    else:
        verdict = "reconciled" if captures else "insufficient_evidence"
    return {
        "verdict": verdict,
        "captured": captured,
        "refunded": refunded,
        "refundable": refundable,
        "capture_count": len(captures),
    }


def validate_business_output(output: dict[str, Any]) -> None:
    payment = output["payment_analysis"]
    captured, refunded, available = (
        money(payment[key])
        for key in ("captured_total_brl", "refunded_total_brl", "refundable_total_brl")
    )
    if available is not None and (
        captured is None or refunded is None or available != captured - refunded
    ):
        raise ValueError("Payment totals are inconsistent or unverified")
    financial = output["financial_resolution"]
    amount = money(financial["recommended_refund_brl"])
    lines = financial["refund_lines"]
    total = sum((money(x["amount_brl"]) or Decimal(0) for x in lines), Decimal(0))
    if amount is None or amount != total:
        raise ValueError("Refund lines do not sum to recommended refund")
    identities = [(x["entity_id"], x["reason_code"]) for x in lines]
    if len(identities) != len(set(identities)):
        raise ValueError("Duplicate refund lines")
    if amount and (available is None or amount > available):
        raise ValueError("Refund exceeds verified available amount")
    if amount and payment["verdict"] in {
        "refund_pending",
        "refund_failed",
        "capture_mismatch",
        "insufficient_evidence",
    }:
        raise ValueError("Unresolved payment cannot authorize another refund")
    assessment = output["assessment"]
    if not 0 <= assessment["confidence"] <= 1:
        raise ValueError("Invalid confidence")
    unresolved = any(x["selected_source"] is None for x in output["data_conflicts"])
    if unresolved and (amount or assessment["case_status"] != "needs_investigation"):
        raise ValueError("Unresolved conflicts require investigation")
    if amount and assessment["case_status"] != "action_required":
        raise ValueError("Refund requires action_required status")
