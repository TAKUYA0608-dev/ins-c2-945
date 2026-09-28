"""INS-C2-945 — inner workflow step 4: human_gate (HumanApprovalGate).

Deterministic human-in-the-loop gate. It does **not** approve a bordereau and it never assigns a real owner —
it flags the material completeness exceptions (entries in `needs_review`: missing required fields,
period/currency/claim-ref/amount inconsistencies, or absent supporting evidence) that require an authorized
reinsurance owner's sign-off before any bordereau approval / remediation, records them + the review status
into the register, and sets `human_review_required`. Skips (no-op) on the rejected / 0-entry safe-answer
branch (no human gate needed) after emitting a skip audit event.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.utils.audit import emit_trace_event


class HumanGateNode(FunctionNode):
    """Flag material completeness exceptions requiring authorized human approval; set human_review_required."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        report = json.loads(state.get("result") or "{}")
        if (
            state.get("error_code")
            or state.get("checked_count", 0) == 0
            or report.get("status_kind") != "completeness_exception_register"
        ):
            emit_trace_event("human_gate.skip", {"reason": state.get("error_code") or "no_register"}, state)
            return {
                "human_review_required": False,
                "review_status": "not_required",
                "status": AgentStatus.SUCCESS.value,
            }

        material: list[dict[str, Any]] = []
        for entry in report.get("exception_register", []):
            if entry["completeness_status"] == "needs_review":
                material.append(
                    {
                        "entry_id": entry["entry_id"],
                        "claim_reference": entry.get("claim_reference"),
                        "exception_severity": entry["exception_severity"],
                        "missing_field_count": len(entry.get("missing_required_fields", [])),
                        "inconsistency_count": len(entry.get("inconsistencies", [])),
                        "absent_evidence_count": len(entry.get("absent_evidence_refs", [])),
                        "reason": "Completeness exception (missing fields / inconsistency / absent evidence) — "
                        "requires authorized reinsurance-owner review before bordereau approval",
                    }
                )

        required = bool(material)
        review = {
            "required": required,
            "status": "pending_human_approval" if required else "not_required",
            "note": "Bordereau approval, completeness remediation, and owner / approver assignment must be "
            "confirmed by an authorized reinsurance owner. This agent produces a candidate register "
            "only.",
            "material_exceptions": material,
        }
        report["human_review"] = review
        emit_trace_event(
            "human_gate.complete", {"review_required": required, "material_exception_count": len(material)}, state
        )
        return {
            "result": json.dumps(report, ensure_ascii=False),
            "human_review_required": required,
            "review_status": review["status"],
            "status": AgentStatus.SUCCESS.value,
        }
