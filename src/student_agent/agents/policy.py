from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any

from ..a2a import Result, Task
from ..evidence_collector import EvidenceCollector
from .business_checks import money, validate_business_output

logger = logging.getLogger(__name__)


async def run(task: Task, collector: EvidenceCollector) -> Result:
    case, entity = task.payload["case"], task.payload["entity"]
    findings = task.payload.get("findings", {})
    refs = []
    policy_data: dict[str, Any] = {}
    if "get_policy" in collector.tools:
        try:
            properties = collector.tools["get_policy"].get("properties", {})
            arguments = {key: case[key] for key in properties if key != "case_id" and key in case}
            response = await collector.call(task.recipient, "get_policy", **arguments)
            if not isinstance(response["data"], dict):
                raise ValueError("Unsupported policy evidence shape")
            collector.consume(task.recipient, [response["evidence_ref"]])
            refs.append(response["evidence_ref"])
            policy_data = response["data"]
        except Exception as exc:
            logger.warning("Policy unavailable (%s)", type(exc).__name__)
    order, ship, pay = (
        findings.get(name, {}).get("payload", {}) for name in ("order", "shipment", "payment")
    )
    payment = deepcopy(
        pay.get(
            "payment_analysis",
            {
                "verdict": "insufficient_evidence",
                "captured_total_brl": None,
                "refunded_total_brl": None,
                "refundable_total_brl": None,
            },
        )
    )
    shipment = deepcopy(
        ship.get(
            "shipment_analysis",
            {"verdict": "insufficient_evidence", "late_seller_ids": [], "timeline_complete": False},
        )
    )
    conflicts = []
    for finding in findings.values():
        for conflict in finding["payload"].get("data_conflicts", []):
            conflict = deepcopy(conflict)
            # No source selection without an adapter for an evidenced precedence rule.
            conflict["selected_source"] = None
            if conflict not in conflicts:
                conflicts.append(conflict)
    issues, causes, parties, actions = [], [], [], []
    if shipment["verdict"] == "seller_delay" and shipment["late_seller_ids"]:
        issues.append("late_delivery_seller")
        causes.append({"cause_code": "SELLER_DELAY", "rank": 1})
        parties = [
            {"party_type": "seller", "party_id": sid} for sid in shipment["late_seller_ids"][:5]
        ]
    elif shipment["verdict"] == "logistics_delay":
        issues.append("late_delivery_logistics")
        causes.append({"cause_code": "LOGISTICS_DELAY", "rank": 1})
        parties = [{"party_type": "logistics_provider", "party_id": None}]
    payment_issue = {
        "duplicate_capture": "duplicate_charge",
        "capture_mismatch": "payment_mismatch",
        "refund_pending": "refund_pending",
        "refund_failed": "refund_failed",
    }
    if payment["verdict"] in payment_issue:
        issues.append(payment_issue[payment["verdict"]])
    captured = payment["captured_total_brl"]
    scope = entity["entity_resolution"]["resolved_order_ids"]
    if len(scope) == 1 and captured is not None and captured > 0:
        for record in order.get("orders", []):
            state = record.get("order_status", record.get("status"))
            issue = {
                "canceled": "canceled_order_paid",
                "cancelled": "canceled_order_paid",
                "unavailable": "unavailable_order_paid",
            }.get(state)
            if issue and issue not in issues:
                issues.append(issue)
    uncertain = (
        entity["entity_resolution"]["status"] != "resolved"
        or bool(conflicts)
        or shipment["verdict"] in {"lost", "returned", "conflicting", "insufficient_evidence"}
        or payment["verdict"] == "insufficient_evidence"
    )
    if shipment["verdict"] in {"lost", "returned"}:
        actions.append("investigate shipment " + shipment["verdict"])
    if not issues and payment["verdict"] == "reconciled" and pay.get("capture_count", 0) > 1:
        # The current adapter accepts an explicit total_brl on the order evidence.
        records = order.get("orders", [])
        expected = money(records[0].get("total_brl")) if len(records) == 1 else None
        if len(scope) == 1 and expected is not None and expected == money(captured):
            issues.append("valid_split_payment")
        elif len(scope) == 1 and expected is not None:
            payment["verdict"] = "capture_mismatch"
            payment["refundable_total_brl"] = None
            issues.append("payment_mismatch")
        else:
            actions.append("verify split payment against order total")
    status = "needs_investigation" if uncertain or not issues else "action_required"
    if issues == ["valid_split_payment"] and not uncertain:
        status = "no_action"
    if issues and issues != ["valid_split_payment"]:
        actions.append("review " + issues[0])
    if not refs:
        status = "needs_investigation"
        actions.append("obtain applicable policy")
    available = payment["refundable_total_brl"]
    scope = entity["entity_resolution"]["resolved_order_ids"]
    refund = 0.0
    # refund_full is the only currently supported policy adapter. Multiple orders
    # require per-order amounts before refund lines can be allocated safely.
    if (
        policy_data.get("refund_full") is True
        and len(scope) == 1
        and status == "action_required"
        and available is not None
        and payment["verdict"] not in {"refund_pending", "refund_failed", "capture_mismatch"}
    ):
        refund = available
    all_refs = list(task.payload.get("entity_evidence_refs", [])) + refs
    for finding in findings.values():
        all_refs.extend(finding.get("evidence_refs", []))
    output = {
        "assessment": {
            "primary_issue": issues[0] if issues else "insufficient_evidence",
            "secondary_issues": issues[1:10],
            "case_status": status,
            "confidence": 0.2 if status == "needs_investigation" else 0.7,
        },
        "affected_entities": {
            "order_ids": scope,
            "item_ids": order.get("item_ids", []),
            "seller_ids": list(
                dict.fromkeys(order.get("seller_ids", []) + ship.get("seller_ids", []))
            )[:20],
            "payment_references": pay.get("payment_references", []),
            "shipment_ids": ship.get("shipment_ids", []),
        },
        "shipment_analysis": shipment,
        "payment_analysis": payment,
        "root_cause_analysis": {"ranked_causes": causes, "responsible_parties": parties},
        "evidence_refs": list(dict.fromkeys(all_refs)),
        "data_conflicts": conflicts[:5],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund,
            "refund_lines": [
                {"reason_code": "POLICY_REFUND", "amount_brl": refund, "entity_id": scope[0]}
            ]
            if refund
            else [],
        },
        "resolution_actions": actions[:8],
    }
    validate_business_output(output)
    return Result(task.case_id, task.recipient, task.message_id, output, tuple(refs))
