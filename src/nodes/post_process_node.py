"""INS-C2-945 — post_process node: OutputSanitise (S-3 output gate + S-4 audit).

S-3 (fail-closed): **enforce** per-entry citation completeness — a grounded register whose entries are not
all cited to a source is never presented; it degrades to a safe `needs_review` answer with the register body
withheld (`error_code=CITATION_INCOMPLETE`, still SUCCESS so post/S-4/disclaimer run). Re-redact any
credential / My-Number / email / phone / insured- or company-name leakage (defense-in-depth), **neutralise
injection markers in the output** (independent of the pre-LLM input gate), and append the mandatory DRAFT
advisory disclaimer — the register is a decision aid, not a bordereau approval; the final bordereau approval /
remediation and reinsurer contact are a human's, gated by the HumanApprovalGate, and fall outside 保険業法
募集/引受/査定. S-4 (no-persist): emit an audit event (counts / status distribution / review flag / error_code
only — never a raw insured/claimant name, amount, or claim narrative). Runs on the full register, the
citation-blocked branch, and the out-of-scope safe branch.
"""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar, cast

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.utils.audit import emit_trace_event

_DISCLAIMER = (
    "本レポートは提供された認可済みデータに基づく参考用の DRAFT 完全性検証結果（candidate completeness "
    "exception register）であり、bordereau の承認・coverage / 支払可否の決定・reserve 計算・査定・再保険者への"
    "連絡・保険の募集 / 引受に該当する判断を確定するものではありません。最終的な bordereau 承認・completeness "
    "是正判断・オーナー / 承認者の確定は、必ず認可された人手の再保険オーナーの承認（HumanApprovalGate）を経て"
    "ください。本エージェントは read-only の完全性検証と needs-review candidate の提示のみを行い、bordereau の"
    "承認・実行や本番システムの変更は行いません。"
)

_CITATION_INCOMPLETE_MSG = (
    "一部の bordereau entry に検証可能な出典（provenance / source）が確認できなかったため、"
    "根拠不十分な completeness exception register の提示を差し控えました。各 entry に認可されたシステム由来の"
    "source を付与のうえ再実行してください。"
)
_NEEDS_REVIEW_NOTE = (
    "Grounding could not be verified for every bordereau entry; the candidate register is withheld pending "
    "valid provenance and authorized reinsurance-owner review."
)

# S-3 defense-in-depth: re-redact secrets / contact info / insured-company names that could leak into any
# free-text field of the register (applied to the whole serialized report before it becomes the envelope).
_SECRET = re.compile(r"\b(?:sk-[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{12,}|eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}|\d{12})\b")
_EMAIL = re.compile(r"[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,4}")
# Phone numbers must not be matched inside a longer digit run or a decimal: without the
# guards a 7-zero run inside "90000000.0" was redacted to "9[REDACTED].0" and made the
# amount inversion in the exception detail unreadable (found on the Marketplace, 2026-09-14).
_PHONE = re.compile(r"(?<![\d.])(?:\+81[-\s]?\d{1,4}|0\d{1,4})[-\s]?\d{1,4}[-\s]?\d{3,4}(?![\d.])")
# Insured / company names: an English name run ending in a corporate suffix, or a Japanese company form.
_COMPANY = re.compile(
    r"(?:[A-Z][A-Za-z0-9&.\-]*\s){1,4}(?:Inc|Corp|Corporation|Ltd|LLC|LLP|GmbH|PLC|K\.?K|KK)\b\.?"
    r"|[^\s\"',]{1,24}(?:株式会社|有限会社|合同会社)"
    r"|(?:株式会社|有限会社|合同会社)[^\s\"',]{1,24}"
)
_REDACTORS = (_SECRET, _EMAIL, _PHONE, _COMPANY)
# S-3 output-side injection containment (independent of the pre-LLM input gate).
_INJECTION = re.compile(
    r"ignore (?:all )?previous|disregard the above|system prompt|you are now|###system|<\|im_start\|>",
    re.IGNORECASE,
)


def _redact_report(report: dict[str, Any]) -> dict[str, Any]:
    """Serialize → redact secret / contact / company-name patterns + neutralise injection markers →
    deserialize (whole-report defense)."""
    text = json.dumps(report, ensure_ascii=False)
    for pattern in _REDACTORS:
        text = pattern.sub("[REDACTED]", text)
    text = _INJECTION.sub("[NEUTRALIZED]", text)
    return cast(dict[str, Any], json.loads(text))


class PostProcessNode(FunctionNode):
    """Verify citations, redact leakage, neutralise injection, append the DRAFT disclaimer, emit audit."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _extra_security_gate_output(self, result: dict[str, Any]) -> dict[str, Any]:
        """S-3 preservation check: the DRAFT advisory disclaimer must be present in the output envelope.

        SDK 1.0.0 contract: receives the **result dict from `execute()`**; returns the (possibly filtered)
        result. MAY raise to block an output missing the mandatory disclaimer.
        """
        out = result.get("formatted_output", "")
        if out and "参考" not in out and "DRAFT" not in out:
            raise ValueError("S-3: DRAFT advisory disclaimer missing from output")
        return dict(result)

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        report: dict[str, Any] = _redact_report(json.loads(state.get("result", "{}") or "{}"))

        grounded = report.get("status_kind") == "completeness_exception_register"
        citations = report.get("citations", [])
        register = report.get("exception_register", [])
        # S-3 per-entry authoritative correspondence: every register entry must carry BOTH its own local
        # citation AND an exact top-level {entry_id, source} citation for the same entry (not merely a
        # non-empty citation list — a partially ungrounded entry, or a top-level citation belonging to a
        # different entry, must fail closed).
        cited_sources = {c.get("entry_id"): c.get("source") for c in citations if c.get("source")}
        citation_complete = (not grounded) or (
            bool(register)
            and all(e.get("citation") for e in register)
            and all(cited_sources.get(e.get("entry_id")) == e.get("citation") for e in register)
        )

        # S-3 fail-closed: an ungrounded register (any entry missing a verifiable citation) is never
        # presented. Degrade to a safe needs-review answer (SUCCESS + error_code), withhold the register
        # body, and still run the disclaimer + terminal S-4 audit.
        if grounded and not citation_complete:
            error_code = state.get("error_code") or "CITATION_INCOMPLETE"
            blocked: dict[str, Any] = {
                "status_kind": "needs_review",
                "package_id": report.get("package_id"),
                "reporting_period": report.get("reporting_period"),
                "package_summary": {},
                "exception_register": [],  # incomplete register body withheld
                "human_review": {"required": True, "status": "pending_human_approval", "note": _NEEDS_REVIEW_NOTE},
                "citations": [],
                "citation_complete": False,
                "message": _CITATION_INCOMPLETE_MSG,
                "disclaimer": _DISCLAIMER,
            }
            emit_trace_event(
                "post_process.citation_blocked",
                {"entry_count": len(register), "error_code": error_code},
                state,
            )
            return {
                "formatted_output": json.dumps(blocked, ensure_ascii=False),
                "disclaimer": _DISCLAIMER,
                "audit_logged": True,
                "error_code": error_code,
                "status": AgentStatus.SUCCESS.value,
            }

        human_review = report.get("human_review", {"required": False, "status": "not_required"})

        formatted = {
            "status_kind": report.get("status_kind"),
            "package_id": report.get("package_id"),
            "reporting_period": report.get("reporting_period"),
            "package_summary": report.get("package_summary", {}),
            "exception_register": register,
            "human_review": human_review,
            "citations": citations,
            "citation_complete": citation_complete,
            "message": report.get("message"),
            "disclaimer": _DISCLAIMER,
        }
        emit_trace_event(
            "post_process.complete",
            {
                "status_kind": report.get("status_kind"),
                "entry_count": len(register),
                "review_required": human_review.get("required", False),
                "citation_complete": citation_complete,
                "error_code": state.get("error_code"),
            },
            state,
        )
        return {
            "formatted_output": json.dumps(formatted, ensure_ascii=False),
            "disclaimer": _DISCLAIMER,
            "audit_logged": True,
            "status": AgentStatus.SUCCESS.value,
        }
