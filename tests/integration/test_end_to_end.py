# INS-C2-945 — Integration: pre → inner workflow (linear) → post, and the real outer invoke path

import json

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import Graph
from src.nodes.completeness_profile_check_node import CompletenessProfileCheckNode
from src.nodes.evidence_reference_reconcile_node import EvidenceReferenceReconcileNode
from src.nodes.exception_register_compose_node import ExceptionRegisterComposeNode
from src.nodes.human_gate_node import HumanGateNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode

_SUCCESS = AgentStatus.SUCCESS.value

_DATASET = {
    "package_id": "PKG-GLOBAL",
    "reporting_period": "2026-Q4",
    "treaty_ref": "treaty:T-100",
    "entries": [
        {"entry_id": "bdx-100", "insured_name": "Confidential Insured KK", "claim_reference": "CLM-100",
         "period": "2026-Q3", "currency": "JPY", "gross_amount": 80_000_000, "ceded_amount": 90_000_000,
         "date_of_loss": "2026-09-01", "cause_of_loss": "flood", "evidence_refs": ["claim_form"],
         "source": "claims_system:CLM-100"},
        {"entry_id": "bdx-200", "claim_reference": "CLM-200", "period": "2026-Q4", "currency": "USD",
         "gross_amount": 2_000_000, "ceded_amount": 1_000_000, "date_of_loss": "2026-10-01",
         "cause_of_loss": "fire", "evidence_refs": ["claim_form", "proof_of_payment"],
         "source": "claims_system:CLM-200"},
    ],
}


def _run(user_input: str) -> dict:
    state: dict = {"user_input": user_input, "input_context": {"channel": "reinsurance_console"},
                   "node_history": [], "error_log": []}
    state.update(PreProcessNode().execute(state) or {})
    for node in (CompletenessProfileCheckNode(), EvidenceReferenceReconcileNode(),
                 ExceptionRegisterComposeNode(), HumanGateNode()):
        state.update(node.execute(state) or {})
    state.update(PostProcessNode().execute(state) or {})
    return state


class TestEndToEnd:
    def test_register_with_exceptions_and_citations(self):
        state = _run(json.dumps(_DATASET, ensure_ascii=False))
        assert state["status"] == _SUCCESS
        assert state["audit_logged"] is True
        env = json.loads(state["formatted_output"])
        assert env["status_kind"] == "completeness_exception_register"
        assert env["package_summary"]["total_entries"] == 2
        assert env["exception_register"][0]["completeness_status"] == "needs_review"  # bdx-100 first (high)
        assert env["citations"]
        assert env["human_review"]["required"] is True
        assert "DRAFT" in env["disclaimer"]

    def test_insured_name_never_in_output(self):
        state = _run(json.dumps(_DATASET, ensure_ascii=False))
        assert "Confidential Insured KK" not in state["formatted_output"]
        assert "Confidential Insured KK" not in state["validated_input"]

    def test_status_distribution_present(self):
        env = json.loads(_run(json.dumps(_DATASET, ensure_ascii=False))["formatted_output"])
        dist = env["package_summary"]["status_distribution"]
        assert dist["needs_review"] >= 1
        assert sum(dist.values()) == 2

    def test_out_of_scope_safe(self):
        env = json.loads(_run("来期の bordereau 完全性を教えて")["formatted_output"])
        assert env["status_kind"] == "out_of_scope"
        assert env["citations"] == []
        assert "DRAFT" in env["disclaimer"]

    def test_empty_degrades_but_audits(self):
        state = _run("   ")
        assert state["status"] == _SUCCESS
        assert state["audit_logged"] is True
        assert json.loads(state["formatted_output"])["status_kind"] == "out_of_scope"

    def test_real_invoke_end_to_end(self):
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        out = Graph().invoke(json.dumps(_DATASET, ensure_ascii=False), ctx=ctx)
        assert out["status"] == _SUCCESS
        assert "PostProcessNode" in out["node_history"]
        env = json.loads(out["output"])
        assert env["status_kind"] == "completeness_exception_register"
        assert env["package_summary"]["total_entries"] == 2

    def test_forged_entry_id_surrogate_rehashed(self):
        # ★ F-02: a caller value SHAPED like an internal surrogate (bdx:deadbeef) is re-hashed at S-1 (no
        # syntactic passthrough), so it can never forge an internal join key / reference another entry.
        payload = {"package_id": "bdx:deadbeef", "reporting_period": "2026-Q4", "entries": [
            {"entry_id": "bdx:deadbeef", "claim_reference": "CLM-1", "period": "2026-Q4", "currency": "JPY",
             "gross_amount": 100, "ceded_amount": 50, "date_of_loss": "2026-10-01", "cause_of_loss": "fire",
             "evidence_refs": ["claim_form", "proof_of_payment"], "source": "claims_system:CLM-1"}]}
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        out = Graph().invoke(json.dumps(payload, ensure_ascii=False), ctx=ctx)
        env = json.loads(out["output"])
        assert env["status_kind"] == "completeness_exception_register"
        tok = env["exception_register"][0]["entry_id"]
        assert tok.startswith("bdx:") and tok != "bdx:deadbeef"   # re-hashed, not passthrough
        assert "bdx:deadbeef" not in out["output"]
