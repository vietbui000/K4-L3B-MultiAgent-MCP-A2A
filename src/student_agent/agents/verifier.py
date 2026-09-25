from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from ..evidence_collector import EvidenceCollector
from .business_checks import validate_business_output


def verify(output: dict[str, Any], collector: EvidenceCollector, entity: dict[str, Any]) -> None:
    json.dumps(output, allow_nan=False)
    collector.trace.contracts.validate_output(output, "verifier output")
    if output["case_id"] != collector.case_id:
        raise ValueError("Output case mismatch")
    for field in ("entity_resolution", "customer_context"):
        if output[field] != entity[field]:
            raise ValueError("Policy changed verified entity context")
    resolved = set(entity["entity_resolution"]["resolved_order_ids"])
    rejected = set(entity["entity_resolution"]["rejected_candidates"])
    if resolved & rejected or not set(output["affected_entities"]["order_ids"]) <= resolved:
        raise ValueError("Output entity scope mismatch")
    refs = set(output["evidence_refs"])
    consumed = set().union(*collector.consumed.values()) if collector.consumed else set()
    if resolved and not refs:
        raise ValueError("Resolved output requires evidence")
    if not refs <= consumed or not refs <= collector.registry.keys():
        raise ValueError("Missing, foreign or unconsumed output evidence")
    for claim in output.get("claim_assessments", []):
        if not set(claim["evidence_refs"]) <= refs:
            raise ValueError("Claim evidence not included in output")
    validate_business_output(output)
    financial = output["financial_resolution"]
    amount = Decimal(str(financial["recommended_refund_brl"]))
    total = sum((Decimal(str(x["amount_brl"])) for x in financial["refund_lines"]), Decimal(0))
    if amount != total:
        raise ValueError("Refund lines do not sum to recommended refund")
    available = output["payment_analysis"]["refundable_total_brl"]
    if amount > 0 and (available is None or amount > Decimal(str(available))):
        raise ValueError("Refund exceeds verified available amount")
    if entity["entity_resolution"]["status"] != "resolved" and (
        amount > 0 or output["assessment"]["case_status"] != "needs_investigation"
    ):
        raise ValueError("Unresolved entity cannot authorize a financial decision")
    for conflict in output["data_conflicts"]:
        if (
            conflict["selected_source"] is not None
            and conflict["selected_source"] not in conflict["sources"]
        ):
            raise ValueError("Selected conflict source is not a source")
