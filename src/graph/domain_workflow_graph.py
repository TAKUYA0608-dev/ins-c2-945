"""INS-C2-945 — inner domain workflow graph (Cat 2).

Instantiated by BordereauCompletenessWorkflowGraphNode.get_subgraph() in graph.py. Linear topology with
per-node skip guards (the portable Cat 2 form; conditional edges don't propagate across the subgraph
boundary):

    START → completeness_profile_check → evidence_reference_reconcile → exception_register_compose → human_gate → END

On rejected / 0-entry input, completeness_profile_check sets checked_count=0 (+error_code);
evidence_reference_reconcile and human_gate no-op and exception_register_compose emits the out-of-scope safe
answer — no fabricated completeness verdict.
"""

from __future__ import annotations
from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState

from src.nodes.completeness_profile_check_node import CompletenessProfileCheckNode
from src.nodes.evidence_reference_reconcile_node import EvidenceReferenceReconcileNode
from src.nodes.exception_register_compose_node import ExceptionRegisterComposeNode
from src.nodes.human_gate_node import HumanGateNode
from src.schemas.state import State


class BordereauCompletenessWorkflow(BaseGraph):
    """Inner graph: completeness_profile_check → evidence_reference_reconcile → exception_register_compose → human_gate."""

    @property
    def name(self) -> str:
        return "BordereauCompletenessWorkflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        pass

    def register_nodes(self) -> None:
        # No super() — BaseGraph.register_nodes() is abstract.
        self._nodes["completeness_profile_check"] = CompletenessProfileCheckNode()
        self._nodes["evidence_reference_reconcile"] = EvidenceReferenceReconcileNode()
        self._nodes["exception_register_compose"] = ExceptionRegisterComposeNode()
        self._nodes["human_gate"] = HumanGateNode()

    def add_edges(self) -> None:
        # Static linear backbone; the 0-entry / rejected skip is handled by per-node guards.
        self._sg.add_edge(START, "completeness_profile_check")
        self._sg.add_edge("completeness_profile_check", "evidence_reference_reconcile")
        self._sg.add_edge("evidence_reference_reconcile", "exception_register_compose")
        self._sg.add_edge("exception_register_compose", "human_gate")
        self._sg.add_edge("human_gate", END)

    def route(self, state: AgentState) -> str:
        """Required by the BaseGraph ABC. Linear topology → not wired to a conditional edge."""
        if state.get("error_code") or state.get("checked_count", 0) == 0:
            return "exception_register_compose"
        return "evidence_reference_reconcile"

    def get_output(self, state: AgentState) -> dict[str, Any]:
        return {
            "output": state.get("result"),
            "status": state.get("status"),
            "checked_count": state.get("checked_count", 0),
            "human_review_required": state.get("human_review_required", False),
            "error_code": state.get("error_code"),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }
