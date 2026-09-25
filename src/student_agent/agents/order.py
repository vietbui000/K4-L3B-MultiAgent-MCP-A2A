from __future__ import annotations

import logging
from typing import Any

from ..a2a import Result, Task
from ..evidence_collector import EvidenceCollector

logger = logging.getLogger(__name__)


def _extract_items(items_data: Any) -> list[dict[str, Any]]:
    if isinstance(items_data, list):
        return [it for it in items_data if isinstance(it, dict)]
    if isinstance(items_data, dict):
        raw = items_data.get("items") or items_data.get("order_items") or []
        if isinstance(raw, list):
            return [it for it in raw if isinstance(it, dict)]
        return [items_data]
    return []


async def run(task: Task, collector: EvidenceCollector) -> Result:
    """Order/Item specialist agent (TV2).

    Investigates order, items, sellers, and product context within entity scope.
    Allowed tools: get_order, get_order_items, get_product_context, get_sellers.
    """
    case = task.payload.get("case", {})
    resolved_order_ids = list(dict.fromkeys(task.entity_scope))
    consumed_refs: list[str] = []
    data_conflicts: list[dict[str, Any]] = []

    if not resolved_order_ids:
        return Result(
            case_id=task.case_id,
            sender=task.recipient,
            message_id=task.message_id,
            payload={
                "order_ids": [],
                "item_ids": [],
                "seller_ids": [],
                "orders": [],
                "items": [],
                "products": [],
                "sellers": [],
                "data_conflicts": [],
            },
            evidence_refs=(),
            status="completed",
            error_code=None,
        )

    collected_orders: list[dict[str, Any]] = []
    collected_items: list[dict[str, Any]] = []
    collected_products: list[dict[str, Any]] = []
    collected_sellers: list[dict[str, Any]] = []

    raw_item_ids: list[str] = []
    raw_seller_ids: list[str] = []

    scope_cfg = case.get("investigation_scope", {})
    include_product = scope_cfg.get("include_product_context", True)

    for order_id in resolved_order_ids:
        # 1. Fetch order details
        if "get_order" in collector.tools:
            try:
                order_resp = await collector.call(task.recipient, "get_order", order_id=order_id)
                ref = order_resp["evidence_ref"]
                collector.consume(task.recipient, [ref])
                consumed_refs.append(ref)
                order_data = order_resp.get("data")
                if isinstance(order_data, dict):
                    if order_data.get("order_id", order_id) != order_id:
                        raise ValueError("Order evidence outside scope")
                    collected_orders.append({**order_data, "order_id": order_id})
            except Exception as exc:
                logger.warning(f"Failed to get_order for {order_id}: {exc}")

        # 2. Fetch order items
        if "get_order_items" in collector.tools:
            try:
                items_resp = await collector.call(
                    task.recipient, "get_order_items", order_id=order_id
                )
                ref = items_resp["evidence_ref"]
                collector.consume(task.recipient, [ref])
                consumed_refs.append(ref)
                items_list = _extract_items(items_resp.get("data"))

                for it in items_list:
                    if it.get("order_id", order_id) != order_id:
                        raise ValueError("Item evidence outside order scope")
                    collected_items.append({**it, "order_id": order_id})
                    item_id_val = it.get("item_id")
                    if item_id_val is not None:
                        raw_item_ids.append(str(item_id_val))
                    elif it.get("order_item_id") is not None:
                        raw_item_ids.append(f"{order_id}_{it['order_item_id']}")
                    elif it.get("product_id") is not None:
                        raw_item_ids.append(f"{order_id}_{it['product_id']}")

                    seller_id = it.get("seller_id")
                    if seller_id:
                        raw_seller_ids.append(str(seller_id))

            except Exception as exc:
                logger.warning(f"Failed to get_order_items for {order_id}: {exc}")

    # 3. Tools return context for an order; the shared collector enforces budget.
    if include_product and "get_product_context" in collector.tools:
        for order_id in resolved_order_ids:
            try:
                prod_resp = await collector.call(
                    task.recipient, "get_product_context", order_id=order_id
                )
                ref = prod_resp["evidence_ref"]
                collector.consume(task.recipient, [ref])
                consumed_refs.append(ref)
                p_data = prod_resp.get("data")
                if isinstance(p_data, dict):
                    collected_products.append(p_data)
                elif isinstance(p_data, list):
                    collected_products.extend(p for p in p_data if isinstance(p, dict))
            except Exception as exc:
                logger.warning(f"Failed to get_product_context for {order_id}: {exc}")

    # 4. Fetch sellers once per resolved order, even when items are unavailable.
    if "get_sellers" in collector.tools:
        for order_id in resolved_order_ids:
            try:
                seller_resp = await collector.call(task.recipient, "get_sellers", order_id=order_id)
                ref = seller_resp["evidence_ref"]
                collector.consume(task.recipient, [ref])
                consumed_refs.append(ref)
                s_data = seller_resp.get("data")
                if isinstance(s_data, dict):
                    collected_sellers.append(s_data)
                elif isinstance(s_data, list):
                    collected_sellers.extend([s for s in s_data if isinstance(s, dict)])
            except Exception as exc:
                logger.warning(f"Failed to get_sellers for {order_id}: {exc}")

    # 5. Detect data conflicts / anomalies
    for o in collected_orders:
        status = o.get("order_status") or o.get("status")
        if status == "delivered" and not any(
            item["order_id"] == o["order_id"] for item in collected_items
        ):
            data_conflicts.append(
                {
                    "field": "order_items",
                    "sources": ["get_order", "get_order_items"],
                    "selected_source": None,
                    "resolution_code": "EMPTY_ITEMS_ON_DELIVERED_ORDER",
                }
            )

    dedup_order_ids = list(dict.fromkeys(resolved_order_ids))[:20]
    dedup_item_ids = list(dict.fromkeys(raw_item_ids))[:20]
    dedup_seller_ids = list(dict.fromkeys(raw_seller_ids))[:20]

    payload = {
        "order_ids": dedup_order_ids,
        "item_ids": dedup_item_ids,
        "seller_ids": dedup_seller_ids,
        "orders": collected_orders,
        "items": collected_items,
        "products": collected_products,
        "sellers": collected_sellers,
        "data_conflicts": data_conflicts[:5],
    }

    return Result(
        case_id=task.case_id,
        sender=task.recipient,
        message_id=task.message_id,
        payload=payload,
        evidence_refs=tuple(dict.fromkeys(consumed_refs)),
        status="completed",
        error_code=None,
    )
