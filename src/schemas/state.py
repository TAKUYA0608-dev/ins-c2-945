"""INS-C2-945 — Agent state (Reinsurance Bordereau Evidence Completeness Validator, Cat 2).

ADR-005: State is a flat TypedDict — never a validation/BaseModel instance. Complex fields are stored
as JSON strings (``NotRequired[str]`` + ``# JSON:``); nodes ``json.dumps`` on write / ``json.loads`` on read.

Read-only / advisory: the agent ingests an authorized reinsurance bordereau package (bordereau extract +
treaty/reference metadata + evidence checklist + carrier-approved completeness profile + reporting-period
policy) and produces a **candidate Completeness Exception Register (needs-review)** deliverable — it never
approves a bordereau, decides coverage / payability, computes a reserve, adjusts a claim, or contacts a
reinsurer. Final bordereau approval, completeness-remediation decisions, and owner / approver assignment are
always an authorized human's (the reinsurance owner), gated by a mandatory HumanApprovalGate.

All agent-specific fields are NotRequired (populated progressively; absent at empty-start invoke).
"""

from __future__ import annotations


from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Agent state for the reinsurance-bordereau completeness-validation workflow."""

    # ── pre_process (BordereauPackageIngest + SensitiveDataDetectAndMinimise, S-1/S-2 validated + hygiened) ─
    validated_input: str  # JSON: {package_id, reporting_period, treaty_ref, entries[]} (PII minimised)
    input_format: str  # "json" | "text" | "empty"
    enriched_context: str  # JSON: {source, channel} (read-only caller context)

    # ── inner workflow (completeness_profile_check → evidence_reference_reconcile → exception_register_compose → human_gate) ─
    checked_entries: str  # JSON: [{entry_id, claim_reference, completeness_state, missing_required_fields[], inconsistencies[], cited_profile_refs[], source}]
    checked_count: int  # bordereau entries checked (0 → out-of-scope safe answer)
    reconciled: str  # JSON: [{entry_id, absent_evidence_refs[], cited_treaty_refs[]}]
    result: str  # JSON: assembled candidate Completeness Exception Register (incl. human_review)
    human_review_required: bool  # True once HumanApprovalGate flags material exceptions
    review_status: str  # "pending_human_approval" | "not_required"

    # ── post_process (OutputSanitise — S-3 gate + S-4 audit) ──────────────────
    formatted_output: str  # JSON: final response envelope (register + disclaimer)
    disclaimer: str  # mandatory DRAFT / advisory-only disclaimer
    audit_logged: bool  # True once the terminal audit event is emitted

    # ── degraded-path signalling (SUCCESS + error_code, never status=ERROR) ───
    # INPUT_REJECTED | INJECTION_REJECTED | INPUT_TOO_LONG | NO_ENTRIES | CITATION_INCOMPLETE
    error_code: str
    error_message: str  # operator-facing detail
