from __future__ import annotations

import asyncio
from copy import deepcopy
from decimal import Decimal

import pytest

from student_agent.a2a import Task
from student_agent.agents.business_checks import reconcile_payments, validate_business_output
from student_agent.agents.payment import run as payment_run
from student_agent.agents.policy import run as policy_run
from student_agent.agents.shipment import run as shipment_run
from student_agent.workflow import solve_case
from test_workflow_tv1 import Gateway, Trace


class Collector:
    def __init__(self, responses):
        self.responses = responses
        self.tools = {
            key[0] if isinstance(key, tuple) else key: {"properties": {}} for key in responses
        }

    async def call(self, actor, tool, **args):
        value = self.responses.get((tool, args.get("order_id")), self.responses.get(tool))
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise ValueError("missing tool")
        return {"data": deepcopy(value), "evidence_ref": "ev_" + tool.ljust(24, "_")}

    def consume(self, *args):
        pass


def capture(identity="P1", amount=100, **extra):
    return {"payment_id": identity, "amount": amount, "status": "captured", **extra}


def test_same_capture_is_not_double_counted():
    result = reconcile_payments([capture(), capture()], [])
    assert result["captured"] == Decimal(100)
    assert result["verdict"] == "reconciled"


def test_distinct_captures_same_authorization_are_duplicates():
    result = reconcile_payments(
        [capture(authorization_code="A"), capture("P2", authorization_code="A")], []
    )
    assert result["captured"] == 200
    assert result["verdict"] == "duplicate_capture"


@pytest.mark.parametrize("records", [[capture(amount=None)], [capture(), capture("P2", None)]])
def test_missing_amount_never_becomes_confirmed_zero_or_partial_total(records):
    result = reconcile_payments(records, [])
    assert result["captured"] is None
    assert result["refundable"] is None
    assert result["verdict"] == "insufficient_evidence"


def test_conflicting_capture_amounts():
    result = reconcile_payments([capture(), capture(amount=120)], [])
    assert result["captured"] is None
    assert result["verdict"] == "capture_mismatch"


@pytest.mark.parametrize("status", ["pending", "failed"])
def test_pending_and_failed_refunds_do_not_allow_another_refund(status):
    result = reconcile_payments([capture()], [{"refund_id": "R1", "amount": 40, "status": status}])
    assert result["verdict"] == "refund_" + status
    assert result["refundable"] is None


def test_refund_observations_are_deduplicated():
    refund = {"refund_id": "R1", "amount": 40, "status": "completed"}
    result = reconcile_payments([capture()], [refund, refund])
    assert result["refunded"] == 40
    assert result["refundable"] == 60


def test_refund_timeout_leaves_money_unknown():
    c = Collector(
        {
            "get_order_payments": [capture()],
            "get_payment_timeline": [capture()],
            "get_refund_timeline": TimeoutError(),
        }
    )
    task = Task("CASE_001", "payment-agent", "payment", {}, ("O1",))
    result = asyncio.run(payment_run(task, c)).payload["payment_analysis"]
    assert result["captured_total_brl"] == 100
    assert result["refunded_total_brl"] is None
    assert result["refundable_total_brl"] is None
    assert result["verdict"] == "insufficient_evidence"


def shipping(handoff, delivered, estimated):
    return {
        "delivered_carrier_date": handoff,
        "delivered_customer_date": delivered,
        "estimated_delivery_date": estimated,
    }


def test_late_delivery_without_seller_deadlines_does_not_blame_logistics():
    c = Collector(
        {
            "get_shipment_summary": shipping("2020-01-02", "2020-01-12", "2020-01-10"),
            "get_order_items": [],
        }
    )
    result = asyncio.run(shipment_run(Task("CASE_001", "shipment-agent", "ship", {}, ("O1",)), c))
    assert result.payload["shipment_analysis"]["verdict"] == "insufficient_evidence"


def test_shipping_deadlines_are_scoped_to_order():
    c = Collector(
        {
            ("get_shipment_summary", "O1"): shipping("2020-01-02", "2020-01-12", "2020-01-10"),
            ("get_order_items", "O1"): [{"seller_id": "S1", "shipping_limit_date": "2020-01-03"}],
            ("get_shipment_summary", "O2"): shipping("2020-02-02", "2020-02-09", "2020-02-10"),
            ("get_order_items", "O2"): [{"seller_id": "S2", "shipping_limit_date": "2020-02-03"}],
        }
    )
    result = asyncio.run(
        shipment_run(Task("CASE_001", "shipment-agent", "ship", {}, ("O1", "O2")), c)
    )
    assert result.payload["shipment_analysis"]["verdict"] == "logistics_delay"
    assert result.payload["shipment_analysis"]["late_seller_ids"] == []


def policy_task(conflict=False, verdict="on_time"):
    findings = {
        "order": {
            "payload": {
                "data_conflicts": [
                    {
                        "field": "status",
                        "sources": ["a", "b"],
                        "selected_source": "a",
                        "resolution_code": "MISMATCH",
                    }
                ]
                if conflict
                else []
            }
        },
        "shipment": {
            "payload": {
                "shipment_analysis": {
                    "verdict": verdict,
                    "late_seller_ids": [],
                    "timeline_complete": verdict == "on_time",
                }
            }
        },
        "payment": {
            "payload": {
                "capture_count": 1,
                "payment_analysis": {
                    "verdict": "reconciled",
                    "captured_total_brl": 100,
                    "refunded_total_brl": 0,
                    "refundable_total_brl": 100,
                },
            }
        },
    }
    return Task(
        "CASE_001",
        "policy-agent",
        "policy",
        {
            "case": {},
            "entity": {"entity_resolution": {"status": "resolved", "resolved_order_ids": ["O1"]}},
            "findings": findings,
        },
        ("O1",),
    )


def test_single_payment_is_not_valid_split_payment():
    result = asyncio.run(policy_run(policy_task(), Collector({"get_policy": {}})))
    assert result.payload["assessment"]["primary_issue"] != "valid_split_payment"


def test_order_conflicts_are_preserved_and_prevent_refund():
    result = asyncio.run(
        policy_run(policy_task(True), Collector({"get_policy": {"refund_full": True}}))
    )
    assert result.payload["data_conflicts"][0]["selected_source"] is None
    assert result.payload["assessment"]["case_status"] == "needs_investigation"
    assert result.payload["financial_resolution"]["recommended_refund_brl"] == 0


@pytest.mark.parametrize("verdict", ["lost", "returned"])
def test_lost_returned_shipments_require_followup(verdict):
    result = asyncio.run(policy_run(policy_task(verdict=verdict), Collector({"get_policy": {}})))
    assert result.payload["assessment"]["case_status"] == "needs_investigation"
    assert any(verdict in action for action in result.payload["resolution_actions"])


def test_business_verifier_rejects_unverified_available_money():
    output = asyncio.run(policy_run(policy_task(), Collector({"get_policy": {}}))).payload
    output["payment_analysis"]["refunded_total_brl"] = None
    with pytest.raises(ValueError, match="unverified"):
        validate_business_output(output)


def test_real_specialists_integrate_with_gateway_and_public_schema():
    class FullGateway(Gateway):
        async def discover_tools(self):
            tools = await super().discover_tools()
            for name in (
                "get_order_items",
                "get_shipment_summary",
                "get_order_payments",
                "get_payment_timeline",
                "get_refund_timeline",
                "get_policy",
            ):
                tools[name] = {
                    "type": "object",
                    "properties": {"case_id": {"type": "string"}, "order_id": {"type": "string"}},
                    "required": ["case_id"] if name == "get_policy" else ["case_id", "order_id"],
                    "additionalProperties": False,
                }
            return tools

        async def call(self, tool, *, case_id, **args):
            if tool == "get_order":
                return await super().call(tool, case_id=case_id, **args)
            self.calls += 1
            data = {
                "get_order_items": [],
                "get_shipment_summary": {},
                "get_order_payments": [capture()],
                "get_payment_timeline": [capture()],
                "get_refund_timeline": [],
                "get_policy": {},
            }[tool]
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_" + f"{case_id}_{self.calls}".ljust(24, "_"),
                "result_hash": "sha256:" + "0" * 64,
                "domain": "order",
                "data": data,
            }

    trace = Trace()
    output = asyncio.run(
        solve_case({"case_id": "CASE_001", "order_id": "ORDER_A"}, FullGateway(), trace)
    )
    trace.contracts.validate_output(output, "integration")
    assert output["payment_analysis"]["captured_total_brl"] == 100
    assert trace.events[-1]["event_type"] == "verification_completed"


def test_no_identifiers_produces_honest_unresolved_output():
    output = asyncio.run(solve_case({"case_id": "CASE_001"}, Gateway(), Trace()))
    assert output["entity_resolution"]["status"] == "not_found"
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert output["evidence_refs"] == []


@pytest.mark.parametrize(
    "total,issue,status",
    [
        (100, "valid_split_payment", "no_action"),
        (120, "payment_mismatch", "action_required"),
    ],
)
def test_split_payment_requires_matching_order_total(total, issue, status):
    task = policy_task()
    task.payload["findings"]["payment"]["payload"]["capture_count"] = 2
    task.payload["findings"]["order"]["payload"]["orders"] = [{"total_brl": total}]
    output = asyncio.run(policy_run(task, Collector({"get_policy": {}}))).payload
    assert output["assessment"]["primary_issue"] == issue
    assert output["assessment"]["case_status"] == status


def test_verified_policy_refund_and_duplicate_line_guard():
    task = policy_task(verdict="logistics_delay")
    output = asyncio.run(policy_run(task, Collector({"get_policy": {"refund_full": True}}))).payload
    assert output["financial_resolution"]["recommended_refund_brl"] == 100
    line = output["financial_resolution"]["refund_lines"][0]
    line["amount_brl"] = 50
    output["financial_resolution"]["refund_lines"].append(deepcopy(line))
    with pytest.raises(ValueError, match="Duplicate refund"):
        validate_business_output(output)
