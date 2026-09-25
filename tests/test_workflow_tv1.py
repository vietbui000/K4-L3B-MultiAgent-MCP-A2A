from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path

import pytest

from student_agent.a2a import Result, Task, validate_result
from student_agent.agents.entity import resolve
from student_agent.agents.verifier import verify
from student_agent.contracts import Contracts
from student_agent.evidence_collector import EvidenceCollector
from student_agent.workflow import coordinate, solve_case


class Trace:
    def __init__(self):
        self.contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts/schemas")
        self.events = []

    def emit(self, **event):
        self.events.append(event)


class Gateway:
    def __init__(self, fail_once=False):
        self.calls = 0
        self.fail_once = fail_once

    async def discover_tools(self):
        return {
            name: {
                "type": "object",
                "properties": {"case_id": {"type": "string"}, arg: {"type": "string"}},
                "required": ["case_id", arg],
                "additionalProperties": False,
            }
            for name, arg in [
                ("get_order", "order_id"),
                ("get_customer_history", "customer_unique_id"),
            ]
        }

    async def call(self, tool, *, case_id, **args):
        self.calls += 1
        await asyncio.sleep(0)
        if self.fail_once and self.calls == 1:
            raise TimeoutError
        data = (
            {"order_id": args["order_id"], "customer_unique_id": "CUSTOMER_A"}
            if tool == "get_order"
            else {
                "customer_unique_id": args["customer_unique_id"],
                "related_order_ids": ["ORDER_A"],
            }
        )
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": "ev_" + f"{case_id}_{self.calls}".ljust(24, "_"),
            "result_hash": "sha256:" + "0" * 64,
            "domain": "order" if tool == "get_order" else "customer",
            "data": data,
        }


async def collector(gateway=None, **kwargs):
    gateway = gateway or Gateway()
    return EvidenceCollector(
        "CASE_001", gateway, Trace(), await gateway.discover_tools(), backoff=0, **kwargs
    )


def test_cache_permissions_arguments_and_case_isolation():
    async def scenario():
        c = await collector()
        try:
            a, b = await asyncio.gather(
                c.call("entity-agent", "get_order", order_id="ORDER_A"),
                c.call("order-agent", "get_order", order_id="ORDER_A"),
            )
            assert a == b and c.calls == 1
            a["data"]["order_id"] = "MUTATED"
            assert b["data"]["order_id"] == "ORDER_A"
            with pytest.raises(ValueError, match="permission"):
                await c.call("payment-agent", "get_order", order_id="ORDER_A")
            with pytest.raises(ValueError, match="case_id"):
                await c.call("entity-agent", "get_order", case_id="CASE_999")
            c.consume("entity-agent", [b["evidence_ref"]])
            other = await collector()
            with pytest.raises(ValueError, match="Unknown"):
                other.consume("entity-agent", [b["evidence_ref"]])
            await other.close()
        finally:
            await c.close()

    asyncio.run(scenario())


def test_retry_is_counted_against_budget():
    async def scenario():
        c = await collector(Gateway(fail_once=True), budget=1)
        try:
            with pytest.raises(RuntimeError, match="BUDGET"):
                await c.call("entity-agent", "get_order", order_id="ORDER_A")
            assert c.calls == 1 and not c.registry
        finally:
            await c.close()
        c = await collector(Gateway(fail_once=True))
        try:
            await c.call("entity-agent", "get_order", order_id="ORDER_A")
            assert c.calls == 2
        finally:
            await c.close()

    asyncio.run(scenario())


def test_candidates_are_not_resolved_from_existence_alone():
    async def scenario():
        c = await collector()
        try:
            result = await resolve(
                Task(
                    "CASE_001",
                    "entity-agent",
                    "resolve_entity",
                    {"candidate_order_ids": ["ORDER_A"]},
                ),
                c,
            )
            assert result.payload["entity_resolution"]["status"] == "ambiguous"
            result = await resolve(
                Task(
                    "CASE_001",
                    "entity-agent",
                    "resolve_entity",
                    {"candidate_order_ids": ["ORDER_A"], "customer_unique_id": "CUSTOMER_B"},
                ),
                c,
            )
            assert result.payload["entity_resolution"]["status"] == "not_found"
            assert result.payload["entity_resolution"]["rejected_candidates"] == ["ORDER_A"]
        finally:
            await c.close()

    asyncio.run(scenario())


def test_a2a_rejects_foreign_case():
    async def scenario():
        c = await collector()
        task = Task("CASE_001", "order-agent", "investigate_order", {})
        with pytest.raises(ValueError, match="correlation"):
            validate_result(task, Result("CASE_002", "order-agent", task.message_id, {}), c)

    asyncio.run(scenario())


def draft(entity, refs):
    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": "CASE_001",
        **entity,
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "secondary_issues": [],
            "case_status": "needs_investigation",
            "confidence": 0,
        },
        "affected_entities": {
            "order_ids": ["ORDER_A"],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "shipment_analysis": {
            "verdict": "insufficient_evidence",
            "late_seller_ids": [],
            "timeline_complete": False,
        },
        "payment_analysis": {
            "verdict": "insufficient_evidence",
            "captured_total_brl": None,
            "refunded_total_brl": None,
            "refundable_total_brl": None,
        },
        "root_cause_analysis": {"ranked_causes": [], "responsible_parties": []},
        "evidence_refs": refs,
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0,
            "refund_lines": [],
        },
        "resolution_actions": [],
    }


def test_verifier_rejects_foreign_evidence_and_inconsistent_refund():
    async def scenario():
        c = await collector()
        try:
            result = await resolve(
                Task("CASE_001", "entity-agent", "resolve_entity", {"order_id": "ORDER_A"}), c
            )
            output = draft(result.payload, list(result.evidence_refs))
            verify(output, c, result.payload)
            broken = deepcopy(output)
            broken["financial_resolution"]["recommended_refund_brl"] = 80
            with pytest.raises(ValueError, match="sum"):
                verify(broken, c, result.payload)
            broken = deepcopy(output)
            broken["evidence_refs"] = ["ev_" + "x" * 24]
            with pytest.raises(ValueError, match="evidence"):
                verify(broken, c, result.payload)
        finally:
            await c.close()

    asyncio.run(scenario())


def test_coordinator_invokes_handlers_and_emits_verification_only_after_success():
    async def scenario():
        trace = Trace()
        calls = []

        async def specialist(task, c):
            calls.append(task.recipient)
            return Result(task.case_id, task.recipient, task.message_id, {})

        async def policy(task, c):
            calls.append(task.recipient)
            return Result(
                task.case_id,
                task.recipient,
                task.message_id,
                draft(task.payload["entity"], task.payload["entity_evidence_refs"]),
            )

        handlers = {f"{x}-agent": specialist for x in ("order", "shipment", "payment")}
        handlers["policy-agent"] = policy
        output = await coordinate(
            {"case_id": "CASE_001", "order_id": "ORDER_A"}, Gateway(), trace, handlers
        )
        assert output["evidence_refs"]
        assert set(calls) == set(handlers)
        assert trace.events[-1]["event_type"] == "verification_completed"
        assert not any(e["event_type"] in {"case_received", "case_finalized"} for e in trace.events)

    asyncio.run(scenario())


def test_missing_specialists_fail_before_discovery(monkeypatch):
    import student_agent.workflow as workflow

    def missing(name):
        raise ModuleNotFoundError(name=name)

    monkeypatch.setattr(workflow.importlib, "import_module", missing)
    with pytest.raises(RuntimeError, match="Integration incomplete"):
        asyncio.run(solve_case({"case_id": "CASE_001"}, Gateway(), Trace()))


def test_discovery_uses_sdk_pagination_params():
    from types import SimpleNamespace

    from student_agent.mcp_gateway import EvidenceGateway

    class Session:
        def __init__(self):
            self.cursors = []

        async def list_tools(self, *, params=None):
            self.cursors.append(None if params is None else params.cursor)
            if params is None:
                return SimpleNamespace(
                    tools=[SimpleNamespace(name="get_order", inputSchema={"type": "object"})],
                    nextCursor="page2",
                )
            return SimpleNamespace(
                tools=[SimpleNamespace(name="get_policy", inputSchema={"type": "object"})],
                nextCursor=None,
            )

    session = Session()
    result = asyncio.run(EvidenceGateway(session, Trace().contracts).discover_tools())
    assert set(result) == {"get_order", "get_policy"}
    assert session.cursors == [None, "page2"]
