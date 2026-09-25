from __future__ import annotations

import asyncio
import json

import pytest
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

from student_agent.a2a import Task, validate_result
from student_agent.agents.entity import resolve
from student_agent.evidence_collector import EvidenceCollector
from student_agent.mcp_gateway import EvidenceGateway
from test_workflow_tv1 import Gateway, Trace


def test_discovery_with_real_sdk_models_and_pagination():
    class Session:
        async def list_tools(self, *, params=None):
            return ListToolsResult(
                tools=[
                    Tool(
                        name="get_order" if params is None else "get_policy",
                        inputSchema={"type": "object"},
                    )
                ],
                nextCursor="page2" if params is None else None,
            )

    assert set(asyncio.run(EvidenceGateway(Session(), Trace().contracts).discover_tools())) == {
        "get_order",
        "get_policy",
    }


@pytest.mark.parametrize("structured", [True, False])
def test_call_with_real_sdk_models(structured):
    evidence = {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": "ev_" + "a" * 24,
        "result_hash": "sha256:" + "0" * 64,
        "domain": "order",
        "data": {"order_id": "ORDER_A"},
    }

    class Session:
        async def call_tool(self, name, *, arguments):
            assert arguments == {"case_id": "CASE_001", "order_id": "ORDER_A"}
            return CallToolResult(
                content=[] if structured else [TextContent(type="text", text=json.dumps(evidence))],
                structuredContent=evidence if structured else None,
                isError=False,
            )

    result = asyncio.run(
        EvidenceGateway(Session(), Trace().contracts).call(
            "get_order", case_id="CASE_001", order_id="ORDER_A"
        )
    )
    assert result == evidence


def test_sdk_error_flag_is_respected():
    class Session:
        async def call_tool(self, *args, **kwargs):
            return CallToolResult(
                content=[TextContent(type="text", text="unavailable")], isError=True
            )

    with pytest.raises(RuntimeError, match="unavailable"):
        asyncio.run(
            EvidenceGateway(Session(), Trace().contracts).call(
                "get_order", case_id="CASE_001", order_id="ORDER_A"
            )
        )


class EntityGateway(Gateway):
    def __init__(self, *, mismatch=False, duplicate=False):
        super().__init__()
        self.mismatch, self.duplicate = mismatch, duplicate
        self.requested = []

    async def call(self, tool, *, case_id, **args):
        self.requested.append((tool, args))
        if tool == "get_order" and args["order_id"] == "UNKNOWN":
            raise RuntimeError("generic server error")
        response = await super().call(tool, case_id=case_id, **args)
        if tool == "get_customer_history":
            response["data"] = {
                "customer_unique_id": args["customer_unique_id"],
                "orders": [
                    {"order_id": "ORDER_A", "customer_id": "ROW_A"},
                    {"order_id": "ORDER_A", "customer_id": "ROW_A"},
                ],
            }
            if self.duplicate:
                response["data"]["orders"].append({"order_id": "ORDER_B", "customer_id": "ROW_B"})
        else:
            response["data"] = {
                "order_id": args["order_id"],
                "customer_id": "OTHER_ROW"
                if self.mismatch
                else ("ROW_A" if args["order_id"] == "ORDER_A" else "ROW_B"),
            }
        return response


def run_entity(payload, gateway=None):
    async def scenario():
        gw = gateway or EntityGateway()
        c = EvidenceCollector("CASE_001", gw, Trace(), await gw.discover_tools(), backoff=0)
        try:
            task = Task("CASE_001", "entity-agent", "resolve_entity", payload)
            result = await resolve(task, c)
            validate_result(task, result, c)
            return result.payload
        finally:
            await c.close()

    return asyncio.run(scenario())


def real_input():
    return {
        "customer_request": {"claimed_order_id": "ORDER_A"},
        "customer_unique_id_hint": "CUSTOMER_A",
        "candidate_order_ids": ["ORDER_A", "UNKNOWN"],
    }


def test_nested_claim_and_customer_hint_are_verified_with_history():
    gw = EntityGateway()
    result = run_entity(real_input(), gw)
    assert result["entity_resolution"]["resolved_order_ids"] == ["ORDER_A"]
    assert result["entity_resolution"]["rejected_candidates"] == []
    assert result["customer_context"] == {
        "customer_unique_id": "CUSTOMER_A",
        "related_order_ids": ["ORDER_A"],
    }
    assert ("get_customer_history", {"customer_unique_id": "CUSTOMER_A"}) in gw.requested


def test_claim_does_not_override_conflicting_customer_evidence():
    result = run_entity(real_input(), EntityGateway(mismatch=True))
    assert result["entity_resolution"]["status"] == "ambiguous"
    assert result["entity_resolution"]["resolved_order_ids"] == []


def test_multiple_customer_orders_remain_ambiguous():
    payload = real_input()
    payload["candidate_order_ids"] = ["ORDER_A", "ORDER_B"]
    result = run_entity(payload, EntityGateway(duplicate=True))
    assert result["entity_resolution"]["status"] == "ambiguous"


def test_unavailable_candidate_without_verified_claim_is_not_rejected():
    payload = real_input()
    payload.pop("customer_request")
    result = run_entity(payload)
    assert result["entity_resolution"]["status"] == "ambiguous"
    assert result["entity_resolution"]["rejected_candidates"] == []


def test_disagreeing_customer_hints_are_not_silently_merged():
    payload = real_input()
    payload["customer_unique_id"] = "CUSTOMER_B"
    with pytest.raises(ValueError, match="Conflicting customer"):
        run_entity(payload)
