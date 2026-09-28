"""INS-C2-945 — inner workflow step 2: evidence_reference_reconcile.

Deterministic reconciliation of the seeded evidence checklist (conditional on large-loss / recovery signals)
against each entry's supplied evidence references, from the treaty reference metadata; records absent
supporting-evidence references + the cited treaty/reference clauses. Skips (no-op) on rejected / 0-entry
input (after emitting a skip audit event).
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services.service import BordereauCompletenessService
from src.utils.audit import emit_trace_event


class EvidenceReferenceReconcileNode(FunctionNode):
    """Reconcile required supporting evidence against supplied references per checked entry."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("error_code") or state.get("checked_count", 0) == 0:
            emit_trace_event(
                "evidence_reference_reconcile.skip", {"reason": state.get("error_code") or "no_checked"}, state
            )
            return {}

        checked = json.loads(state.get("checked_entries") or "[]")
        reconciled = [BordereauCompletenessService.reconcile_evidence(c) for c in checked]
        emit_trace_event(
            "evidence_reference_reconcile.complete",
            {
                "entries": len(reconciled),
                "absent_evidence_total": sum(len(r["absent_evidence_refs"]) for r in reconciled),
            },
            state,
        )
        return {"reconciled": json.dumps(reconciled, ensure_ascii=False), "status": AgentStatus.SUCCESS.value}
