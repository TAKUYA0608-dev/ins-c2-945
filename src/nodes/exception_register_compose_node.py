"""INS-C2-945 — inner workflow step 3: exception_register_compose.

Composes the candidate **Completeness Exception Register** deliverable: a package summary, and a per-entry
exception block (missing required fields, period/currency/claim-ref/amount inconsistencies, absent
supporting-evidence references, cited profile/treaty clauses, exception severity, candidate disposition),
each cited to its source record. Entries are ordered by exception severity (high first). The register is
advisory / needs-review only — it never approves a bordereau, decides coverage/payability, or contacts a
reinsurer. On the 0-entry / rejected branch it emits the out-of-scope safe answer.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services.service import BordereauCompletenessService
from src.utils.audit import emit_trace_event

_OUT_OF_SCOPE = (
    "検証可能な再保険 bordereau データが入力に見つかりませんでした。"
    "entries 配列に entry_id と claim_reference / period / currency / gross_amount / ceded_amount / "
    "date_of_loss / cause_of_loss 等を含む JSON をご指定いただくか、reporting_period を明確にしてください。"
)


class ExceptionRegisterComposeNode(FunctionNode):
    """Compose the candidate Completeness Exception Register with citations (or safe answer on 0-entry)."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        checked = json.loads(state.get("checked_entries") or "[]")
        if state.get("error_code") or not checked:
            emit_trace_event(
                "exception_register_compose.safe", {"reason": state.get("error_code") or "no_checked"}, state
            )
            report: dict[str, Any] = {
                "status_kind": "out_of_scope",
                "message": _OUT_OF_SCOPE,
                "package_summary": {},
                "exception_register": [],
                "citations": [],
            }
            return {"result": json.dumps(report, ensure_ascii=False), "status": AgentStatus.SUCCESS.value}

        reconciled_by_id = {r["entry_id"]: r for r in json.loads(state.get("reconciled") or "[]")}
        register: list[dict[str, Any]] = []
        citations: list[dict[str, str]] = []
        for c in checked:
            reconciled = reconciled_by_id.get(c["entry_id"], {})
            register.append(BordereauCompletenessService.compose_exception(c, reconciled))
            citations.append({"entry_id": c["entry_id"], "source": c["source"]})
        register.sort(
            key=lambda e: (-BordereauCompletenessService.severity_rank(e["exception_severity"]), e["entry_id"])
        )

        summary = BordereauCompletenessService.package_summary(register)
        scope = self._scope(state)
        report = {
            "status_kind": "completeness_exception_register",
            "package_id": scope.get("package_id"),
            "reporting_period": scope.get("reporting_period"),
            "treaty_ref": scope.get("treaty_ref"),
            "package_summary": summary,
            "exception_register": register,
            "citations": citations,
        }
        emit_trace_event(
            "exception_register_compose.complete",
            {
                "entry_count": len(register),
                "exception_count": sum(1 for e in register if e["completeness_status"] == "needs_review"),
                "citation_count": len(citations),
            },
            state,
        )
        return {"result": json.dumps(report, ensure_ascii=False), "status": AgentStatus.SUCCESS.value}

    @staticmethod
    def _scope(state: dict[str, Any]) -> dict[str, Any]:
        slots = json.loads(state.get("validated_input") or "{}")
        return {
            "package_id": slots.get("package_id"),
            "reporting_period": slots.get("reporting_period"),
            "treaty_ref": slots.get("treaty_ref"),
        }
