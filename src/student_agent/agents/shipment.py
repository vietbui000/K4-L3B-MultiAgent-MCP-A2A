from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from ..a2a import Result, Task
from ..evidence_collector import EvidenceCollector

logger = logging.getLogger(__name__)


def parse_iso_datetime(value: Any) -> datetime | None:
    """Parse ISO-8601 or standard datetime string to UTC datetime."""
    if not value or not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        cleaned = raw.replace(" ", "T")
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        return None


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
    """Shipment specialist agent (TV2).

    Analyzes shipment summaries, item delivery deadlines, seller delays,
    and logistics delays within entity scope.
    Allowed tools: get_shipment_summary, get_order_items.
    """
    resolved_order_ids = list(task.entity_scope)
    consumed_refs: list[str] = []
    data_conflicts: list[dict[str, Any]] = []

    if not resolved_order_ids:
        return Result(
            case_id=task.case_id,
            sender=task.recipient,
            message_id=task.message_id,
            payload={
                "shipment_analysis": {
                    "verdict": "insufficient_evidence",
                    "late_seller_ids": [],
                    "timeline_complete": False,
                },
                "shipment_ids": [],
                "seller_ids": [],
                "data_conflicts": [],
            },
            evidence_refs=(),
            status="completed",
            error_code=None,
        )

    shipment_summaries: list[dict[str, Any]] = []
    all_items: list[dict[str, Any]] = []
    collected_shipment_ids: list[str] = []
    collected_seller_ids: list[str] = []

    # 1. Fetch shipment summaries and order items for each order
    for order_id in resolved_order_ids:
        if "get_shipment_summary" in collector.tools:
            try:
                ship_resp = await collector.call(
                    task.recipient, "get_shipment_summary", order_id=order_id
                )
                ref = ship_resp["evidence_ref"]
                collector.consume(task.recipient, [ref])
                consumed_refs.append(ref)
                s_data = ship_resp.get("data")
                if isinstance(s_data, dict):
                    shipment_summaries.append({**s_data, "_order_id": order_id})
                elif isinstance(s_data, list):
                    shipment_summaries.extend(
                        {**s, "_order_id": order_id} for s in s_data if isinstance(s, dict)
                    )
            except Exception as exc:
                logger.warning(f"Failed to get_shipment_summary for {order_id}: {exc}")

        if "get_order_items" in collector.tools:
            try:
                items_resp = await collector.call(
                    task.recipient, "get_order_items", order_id=order_id
                )
                ref = items_resp["evidence_ref"]
                collector.consume(task.recipient, [ref])
                consumed_refs.append(ref)
                all_items.extend(
                    {**item, "_order_id": order_id}
                    for item in _extract_items(items_resp.get("data"))
                )
            except Exception as exc:
                logger.warning(f"Failed to get_order_items for {order_id}: {exc}")

    # 2. Extract seller shipping limits from order items
    seller_limits: dict[tuple[str, str], list[datetime]] = {}
    incomplete_orders: set[str] = set()
    for it in all_items:
        sid = it.get("seller_id")
        if sid:
            collected_seller_ids.append(str(sid))
            limit_dt = parse_iso_datetime(
                it.get("shipping_limit_date") or it.get("order_item_shipping_limit_date")
            )
            if limit_dt:
                seller_limits.setdefault((it["_order_id"], str(sid)), []).append(limit_dt)
            else:
                incomplete_orders.add(it["_order_id"])
        else:
            incomplete_orders.add(it["_order_id"])

    # 3. Analyze timeline
    verdict = "insufficient_evidence"
    late_sellers: list[str] = []
    timeline_complete = False

    if not shipment_summaries:
        verdict = "insufficient_evidence"
        timeline_complete = False
    else:
        is_conflicting = False
        is_lost = False
        is_returned = False

        has_valid_delivery = False
        all_timelines_complete = {s["_order_id"] for s in shipment_summaries} == set(
            resolved_order_ids
        )
        attribution_complete = True
        delivery_is_late = False

        for s in shipment_summaries:
            order_id = s["_order_id"]
            limits_for_shipment = {
                sid: limits
                for (oid, sid), limits in seller_limits.items()
                if oid == order_id and (not s.get("seller_id") or sid == s["seller_id"])
            }
            seller_known = (
                bool(limits_for_shipment)
                and order_id not in incomplete_orders
                and (
                    bool(s.get("seller_id"))
                    or sum(x["_order_id"] == order_id for x in shipment_summaries) == 1
                )
            )
            attribution_complete &= seller_known
            all_timelines_complete &= seller_known
            if s.get("order_id", order_id) != order_id:
                attribution_complete = False
                all_timelines_complete = False
                continue
            shipment_id = s.get("shipment_id") or s.get("tracking_code") or s.get("tracking_number")
            if shipment_id:
                collected_shipment_ids.append(str(shipment_id))

            status = str(s.get("status") or s.get("shipment_status") or "").lower()
            if status in ("lost", "missing", "package_lost"):
                is_lost = True
            elif status in ("returned", "return_to_sender", "returned_to_seller"):
                is_returned = True

            carrier_dt = parse_iso_datetime(
                s.get("delivered_carrier_date")
                or s.get("shipped_at")
                or s.get("order_delivered_carrier_date")
            )
            delivery_dt = parse_iso_datetime(
                s.get("delivered_customer_date")
                or s.get("actual_delivery_date")
                or s.get("order_delivered_customer_date")
            )
            estimated_dt = parse_iso_datetime(
                s.get("estimated_delivery_date")
                or s.get("order_estimated_delivery_date")
                or s.get("estimated_date")
            )

            # Check contradictory dates
            if carrier_dt and delivery_dt and delivery_dt < carrier_dt:
                is_conflicting = True
                data_conflicts.append(
                    {
                        "field": "delivered_customer_date",
                        "sources": [
                            "get_shipment_summary.delivered_customer_date",
                            "get_shipment_summary.delivered_carrier_date",
                        ],
                        "selected_source": None,
                        "resolution_code": "DELIVERY_BEFORE_CARRIER_HANDOFF",
                    }
                )

            # Check completeness of milestone dates
            if not (carrier_dt and delivery_dt and estimated_dt):
                all_timelines_complete = False
            else:
                has_valid_delivery = True

            # Evaluate customer delivery vs estimated date
            if delivery_dt and estimated_dt and delivery_dt > estimated_dt:
                delivery_is_late = True

            # Evaluate seller delay vs item shipping limit dates
            if carrier_dt and seller_known:
                for sid, limits in limits_for_shipment.items():
                    earliest_limit = min(limits)
                    if carrier_dt > earliest_limit:
                        late_sellers.append(sid)

        if is_conflicting:
            verdict = "conflicting"
            timeline_complete = False
        elif is_lost:
            verdict = "lost"
            timeline_complete = False
        elif is_returned:
            verdict = "returned"
            timeline_complete = False
        elif has_valid_delivery:
            timeline_complete = all_timelines_complete
            if delivery_is_late:
                if not attribution_complete or not all_timelines_complete:
                    verdict = "insufficient_evidence"
                else:
                    verdict = "seller_delay" if late_sellers else "logistics_delay"
            else:
                verdict = "on_time" if all_timelines_complete else "insufficient_evidence"
        else:
            verdict = "insufficient_evidence"
            timeline_complete = False

    dedup_late_sellers = list(dict.fromkeys(late_sellers))[:20]
    dedup_shipment_ids = list(dict.fromkeys(collected_shipment_ids))[:20]
    dedup_seller_ids = list(dict.fromkeys(collected_seller_ids))[:20]

    shipment_analysis = {
        "verdict": verdict,
        "late_seller_ids": dedup_late_sellers if verdict == "seller_delay" else [],
        "timeline_complete": timeline_complete,
    }

    payload = {
        "shipment_analysis": shipment_analysis,
        "shipment_ids": dedup_shipment_ids,
        "seller_ids": dedup_seller_ids,
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
