from __future__ import annotations

import logging
from typing import Any

from ..a2a import Result, Task
from ..evidence_collector import EvidenceCollector
from .business_checks import reconcile_payments

logger = logging.getLogger(__name__)


def rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        for key in ("payments", "transactions", "refunds", "timeline", "events"):
            if key in value:
                value = value[key]
                break
        else:
            value = [value] if value else None
    if not isinstance(value, list) or any(not isinstance(x, dict) for x in value):
        raise ValueError("Unsupported payment evidence shape")
    return value


async def run(task: Task, collector: EvidenceCollector) -> Result:
    refs, payments, refunds, references = [], [], [], []
    complete = bool(task.entity_scope)
    refund_complete = bool(task.entity_scope)
    for order_id in task.entity_scope:
        for tool, target in (
            ("get_order_payments", payments),
            ("get_payment_timeline", payments),
            ("get_refund_timeline", refunds),
        ):
            try:
                response = await collector.call(task.recipient, tool, order_id=order_id)
                records = rows(response["data"])
                if any(x.get("order_id", order_id) != order_id for x in records):
                    raise ValueError("Payment evidence outside order scope")
                collector.consume(task.recipient, [response["evidence_ref"]])
                refs.append(response["evidence_ref"])
                target.extend({**x, "_order_id": order_id} for x in records)
                if target is payments:
                    for row in records:
                        ref = row.get("payment_reference") or row.get("payment_id")
                        ref = ref or row.get("transaction_id")
                        if ref:
                            references.append(str(ref))
            except Exception as exc:
                logger.warning(
                    "Payment evidence unavailable: case_id=%s order_id=%s tool=%s (%s): %s",
                    task.case_id,
                    order_id,
                    tool,
                    type(exc).__name__,
                    exc,
                )
                if target is refunds:
                    refund_complete = False
                else:
                    complete = False
    result = reconcile_payments(
        payments, refunds, payments_complete=complete, refunds_complete=refund_complete
    )
    analysis = {"verdict": result["verdict"]}
    for key in ("captured", "refunded", "refundable"):
        analysis[f"{key}_total_brl"] = float(result[key]) if result[key] is not None else None
    payload = {
        "payment_analysis": analysis,
        "capture_count": result["capture_count"],
        "payment_references": list(dict.fromkeys(references))[:20],
    }
    return Result(
        task.case_id, task.recipient, task.message_id, payload, tuple(dict.fromkeys(refs))
    )
