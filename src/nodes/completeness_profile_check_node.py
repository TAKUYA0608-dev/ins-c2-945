"""INS-C2-945 — inner workflow step 1: completeness_profile_check.

Deterministic ingest + normalization of the supplied bordereau entries, then per-entry completeness checking
against the seeded carrier-approved completeness profile: required-field presence + period/currency/
claim-reference / ceded-vs-gross consistency, with cited profile clauses. Sets `checked_count`. **0 valid
entries (rejected input, non-JSON text, or all rows missing entry_id) routes to the out-of-scope safe
answer** — the agent never fabricates a completeness verdict for data it did not receive.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services.service import BordereauCompletenessService
from src.utils.audit import emit_trace_event


class CompletenessProfileCheckNode(FunctionNode):
    """Ingest + normalize supplied entries and check each against the completeness profile."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # Entries arrive already validated + provenance-resolved by pre_process (S-1): each `source` is a
        # grounded citation `src:<sha8>` or None (a forged surrogate was dropped at S-1). We do not re-run
        # provenance here — normalize trusts that single upstream resolution.
        slots = json.loads(state.get("validated_input") or state.get("user_input") or "{}")
        if not isinstance(slots, dict):
            slots = {}
        canonical = json.dumps(slots, ensure_ascii=False)
        entries = slots.get("entries") if isinstance(slots.get("entries"), list) else []

        if state.get("error_code") or not entries:
            emit_trace_event(
                "completeness_profile_check.skip", {"reason": state.get("error_code") or "no_entries"}, state
            )
            return {
                "validated_input": canonical,
                "checked_entries": "[]",
                "checked_count": 0,
                "error_code": state.get("error_code") or "NO_ENTRIES",
                "status": AgentStatus.SUCCESS.value,
            }

        normalized = BordereauCompletenessService.normalize(entries)
        if not normalized:
            emit_trace_event("completeness_profile_check.skip", {"reason": "all_malformed"}, state)
            return {
                "validated_input": canonical,
                "checked_entries": "[]",
                "checked_count": 0,
                "error_code": "NO_ENTRIES",
                "status": AgentStatus.SUCCESS.value,
            }

        dup_refs = BordereauCompletenessService.duplicate_claim_references(normalized)
        reporting_period = slots.get("reporting_period")
        checked = [BordereauCompletenessService.check_completeness(e, dup_refs, reporting_period) for e in normalized]
        distribution: dict[str, int] = {}
        for c in checked:
            distribution[c["completeness_state"]] = distribution.get(c["completeness_state"], 0) + 1
        emit_trace_event(
            "completeness_profile_check.complete",
            {"supplied": len(entries), "checked": len(checked), "state_distribution": distribution},
            state,
        )
        return {
            "validated_input": canonical,
            "checked_entries": json.dumps(checked, ensure_ascii=False),
            "checked_count": len(checked),
            "status": AgentStatus.SUCCESS.value,
        }
