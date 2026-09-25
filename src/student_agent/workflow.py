from __future__ import annotations

import asyncio
import importlib
import re
from copy import deepcopy
from typing import Any

from .a2a import Result, Specialist, Task, validate_result
from .agents.entity import resolve
from .agents.verifier import verify
from .evidence_collector import EvidenceCollector
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


def load_specialists() -> dict[str, Specialist]:
    handlers = {}
    for name in ("order", "shipment", "payment", "policy"):
        module_name = f"student_agent.agents.{name}"
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name != module_name:
                raise
            raise RuntimeError(f"Integration incomplete: implement {module_name}.run") from exc
        handlers[f"{name}-agent"] = module.run
    return handlers


async def coordinate(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    handlers: dict[str, Specialist],
) -> dict[str, Any]:
    case_id = case.get("case_id")
    if not isinstance(case_id, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{2,63}", case_id):
        raise ValueError("Missing or invalid case_id")
    required = {"order-agent", "shipment-agent", "payment-agent", "policy-agent"}
    if not required <= handlers.keys():
        raise RuntimeError("Integration incomplete: missing specialist handlers")
    collector = None
    try:
        async with asyncio.timeout(600):
            collector = EvidenceCollector(case_id, gateway, trace, await gateway.discover_tools())

            async def dispatch(
                actor: str, kind: str, payload: dict[str, Any], scope: tuple[str, ...] = ()
            ) -> Result:
                task = Task(case_id, actor, kind, deepcopy(payload), scope)
                trace.emit(
                    case_id=case_id,
                    event_type="task_assigned",
                    actor="coordinator",
                    target=actor,
                    attributes={"message_id": task.message_id, "task_type": kind},
                )
                try:
                    async with asyncio.timeout(120):
                        handler = resolve if actor == "entity-agent" else handlers[actor]
                        result = await handler(task, collector)
                    validate_result(task, result, collector)
                except Exception:
                    trace.emit(
                        case_id=case_id,
                        event_type="handoff",
                        actor=actor,
                        target="coordinator",
                        decision_code="TASK_FAILED",
                        attributes={"message_id": task.message_id},
                    )
                    raise
                trace.emit(
                    case_id=case_id,
                    event_type="handoff",
                    actor=actor,
                    target="coordinator",
                    attributes={"message_id": task.message_id},
                )
                return result

            entity = await dispatch("entity-agent", "resolve_entity", case)
            scope = tuple(entity.payload["entity_resolution"]["resolved_order_ids"])
            findings = {}
            if scope:
                async with asyncio.TaskGroup() as group:
                    tasks = {
                        name: group.create_task(
                            dispatch(
                                f"{name}-agent",
                                f"investigate_{name}",
                                {"case": case, "entity": entity.payload},
                                scope,
                            )
                        )
                        for name in ("order", "payment", "shipment")
                    }
                findings = {
                    name: {
                        "payload": task.result().payload,
                        "evidence_refs": list(task.result().evidence_refs),
                    }
                    for name, task in tasks.items()
                }
            policy = await dispatch(
                "policy-agent",
                "apply_policy",
                {
                    "case": case,
                    "entity": entity.payload,
                    "entity_evidence_refs": list(entity.evidence_refs),
                    "findings": findings,
                },
                scope,
            )
            output = deepcopy(policy.payload)
            output["schema_version"] = "day09-l3b-output-v2"
            output["case_id"] = case_id
            output.update(deepcopy(entity.payload))
            # A policy agent must only return evidence it consumed itself for A2A
            # correlation. The final submission, however, includes every ref used
            # by entity and specialist agents in this case.
            all_refs = list(entity.evidence_refs) + list(policy.evidence_refs)
            for finding in findings.values():
                all_refs.extend(finding["evidence_refs"])
            output["evidence_refs"] = list(dict.fromkeys(all_refs))
            trace.emit(
                case_id=case_id, event_type="task_assigned", actor="coordinator", target="verifier"
            )
            verify(output, collector, entity.payload)
            trace.emit(case_id=case_id, event_type="verification_completed", actor="verifier")
            return output
    finally:
        if collector is not None:
            await collector.close()


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    # A case without any entity discriminator cannot be investigated safely.
    # Fail before MCP discovery rather than producing an unverifiable output.
    if not any(case.get(key) for key in ("order_id", "order_ids", "candidate_order_ids", "customer_unique_id")):
        raise RuntimeError("Integration incomplete: case has no entity discriminator")
    return await coordinate(case, gateway, trace, load_specialists())
