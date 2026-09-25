from __future__ import annotations

from typing import Any

from ..a2a import Result, Task
from ..evidence_collector import EvidenceCollector


def ids(value: Any) -> list[str]:
    if value is None:
        return []
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or any(not isinstance(x, str) or not x for x in values):
        raise ValueError("Unsupported identifier shape; adapt against the official input")
    return list(dict.fromkeys(values))


async def resolve(task: Task, collector: EvidenceCollector) -> Result:
    """Resolve both flat inputs and the observed L3B customer_request/hint format."""
    case = task.payload
    request = case.get("customer_request", {})
    if not isinstance(request, dict):
        raise ValueError("Invalid customer_request")
    claimed = ids(request.get("claimed_order_id"))
    exact = ids(case.get("order_ids")) + ids(case.get("order_id"))
    exact = list(dict.fromkeys(exact))
    candidates = ids(case.get("candidate_order_ids"))
    customer = case.get("customer_unique_id")
    hint = case.get("customer_unique_id_hint")
    if hint is not None and (not isinstance(hint, str) or not hint):
        raise ValueError("Invalid customer_unique_id_hint")
    if customer is not None and hint is not None and customer != hint:
        raise ValueError("Conflicting customer identifiers")
    customer = customer if customer is not None else hint
    if customer is not None and (not isinstance(customer, str) or not customer):
        raise ValueError("Invalid customer_unique_id")
    refs, related, rejected, confirmed = [], [], [], []
    history: dict[str, list[dict[str, Any]]] = {}
    unavailable = []
    if customer:
        response = await collector.call(
            "entity-agent", "get_customer_history", customer_unique_id=customer
        )
        data = response["data"]
        if not isinstance(data, dict) or data.get("customer_unique_id") != customer:
            raise ValueError("Unsupported or mismatched customer evidence")
        related = ids(data.get("related_order_ids"))
        if "orders" in data:
            orders = data["orders"]
            if not isinstance(orders, list) or any(not isinstance(row, dict) for row in orders):
                raise ValueError("Unsupported customer history orders")
            for row in orders:
                order_ids = ids(row.get("order_id"))
                if len(order_ids) != 1:
                    raise ValueError("Customer history order has no identifier")
                history.setdefault(order_ids[0], []).append(row)
            related = list(dict.fromkeys(related + list(history)))
        refs.append(response["evidence_ref"])
        collector.consume("entity-agent", [response["evidence_ref"]])
    checked = list(dict.fromkeys(exact + claimed + candidates))
    for order in checked:
        try:
            response = await collector.call("entity-agent", "get_order", order_id=order)
        except (RuntimeError, TimeoutError):
            # A generic tool/transport failure is not evidence that a candidate is invalid.
            unavailable.append(order)
            continue
        data = response["data"]
        if not isinstance(data, dict) or data.get("order_id") != order:
            raise ValueError("Unsupported or mismatched order evidence")
        refs.append(response["evidence_ref"])
        collector.consume("entity-agent", [response["evidence_ref"]])
        if not customer:
            confirmed.append(order)
            continue
        direct_customer = data.get("customer_unique_id")
        rows = history.get(order, [])
        row_customers = {row["customer_id"] for row in rows if row.get("customer_id")}
        row_customer = data.get("customer_id")
        history_conflict = bool(row_customers and row_customer not in row_customers)
        if direct_customer is not None and direct_customer != customer:
            rejected.append(order)
        elif history_conflict or len(row_customers) > 1:
            unavailable.append(order)
        elif direct_customer == customer or rows or order in related:
            confirmed.append(order)
        else:
            unavailable.append(order)
    if exact:
        resolved = [x for x in exact if x in confirmed]
        status = "resolved" if len(resolved) == len(exact) else "ambiguous"
        if status != "resolved":
            resolved = []
    else:
        # A claimed ID is usable only after order + customer evidence confirm it.
        # Unavailable alternatives are not rejected. Without a verified claim,
        # unresolved alternatives prevent selecting a candidate by elimination.
        verified_claim = claimed and confirmed == claimed
        resolved = (
            confirmed
            if customer and len(confirmed) == 1 and (verified_claim or not unavailable)
            else []
        )
        status = "resolved" if resolved else "ambiguous"
    if not checked or (checked and len(rejected) == len(checked)):
        status, resolved = "not_found", []
    return Result(
        task.case_id,
        task.recipient,
        task.message_id,
        {
            "entity_resolution": {
                "status": status,
                "resolved_order_ids": resolved,
                "rejected_candidates": rejected,
                "confidence": 0.9 if resolved else 0.0,
            },
            "customer_context": {"customer_unique_id": customer, "related_order_ids": related},
        },
        tuple(refs),
    )
