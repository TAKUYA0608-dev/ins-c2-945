"""INS-C2-945 — pre_process node: BordereauPackageIngest + SensitiveDataDetectAndMinimise (S-1/S-2).

Accepts a structured JSON reinsurance-bordereau package (an ``entries[]`` array of bordereau records, plus
optional ``package_id`` / ``reporting_period`` / ``treaty_ref``) or NL text, normalizes it (NFKC), enforces
S-1/S-2, and extracts the analysis slots. The agent is read-only: it never mutates the source package.

Degraded contract (SDK 1.0.0): injection markers / oversize / empty never set `status=ERROR`. They return
`status=SUCCESS + error_code` (`INJECTION_REJECTED` / `INPUT_TOO_LONG` / `INPUT_REJECTED`) and **discard the
offending body** so `main`/`post_process` still run (disclaimer + S-3 + S-4). The `@final` framework hook is
not invoked by the local stub framework, so `execute()` re-checks the same S-2 conditions itself. Injection
containment is performed pre-LLM here at input AND independently at the S-3 output gate (two separate gates).

Pre-LLM PII minimisation (`SensitiveDataDetectAndMinimise`): every string written into `validated_input` is
passed through `_hygiene()` (redacts credential / My-Number / email / phone patterns) and insured/claimant
PII display fields are dropped — entries are keyed by an opaque `entry_id`. Caller-supplied provenance
(`source`) is constrained to a **safe authorized reference** (allowlist); anything else is dropped so
untrusted text can never reach an output citation. `package_id` is tokenized; `reporting_period` /
`treaty_ref` are hygiened too.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services.service import resolve_provenance, safe_identifier
from src.utils.audit import emit_trace_event

_MAX_INPUT = 200_000  # bordereau packages carry many entries → larger cap than a chat prompt
_INJECTION_MARKERS = (
    "ignore previous",
    "ignore all previous",
    "disregard the above",
    "system prompt",
    "you are now",
    "###system",
    "<|im_start|>",
)
_REJECT_CODES = frozenset({"INJECTION_REJECTED", "INPUT_TOO_LONG"})
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# ── input hygiene: redact secrets a caller may inadvertently include before persisting to State ──
_CREDENTIAL = re.compile(r"\b(sk-[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{12,}|eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,})\b")
_MY_NUMBER = re.compile(r"\b\d{12}\b")  # Japanese My-Number / 個人番号
_EMAIL = re.compile(r"\b[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,4}\b")
_PHONE = re.compile(r"(?<![\d.])(?:(?:\+81[-\s]?\d{1,4}|0\d{1,4})[-\s]?\d{1,4}[-\s]?\d{3,4})(?![\d.])")
_REDACTED = "[REDACTED]"
# PII display fields dropped entirely — entries are keyed by opaque entry_id, not by an insured/claimant name.
_PII_DROP_FIELDS = frozenset(
    {
        "insured_name",
        "claimant_name",
        "policyholder_name",
        "insured",
        "claimant",
        "policyholder",
        "contact_name",
        "primary_contact",
        "broker_contact",
        "adjuster_name",
        "contact_email",
        "contact_phone",
        "email",
        "phone",
    }
)
# Identifier keys are unconditionally tokenized to an opaque surrogate (see safe_identifier) — no syntactic
# passthrough — so a PII / free-text entry_id can never leak into State, a citation, or output.
_ID_FIELDS = frozenset({"entry_id", "id"})


def _nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


def _hygiene(text: str) -> str:
    """Redact credential / My-Number / email / phone patterns from a free-text value."""
    out = _CREDENTIAL.sub(_REDACTED, text)
    out = _MY_NUMBER.sub(_REDACTED, out)
    out = _EMAIL.sub(_REDACTED, out)
    out = _PHONE.sub(_REDACTED, out)
    return out


def _hygiene_obj(obj: Any) -> Any:
    """Recursively drop PII display fields, constrain provenance to a safe authorized reference, tokenize
    identifiers, and redact secrets in every string value."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            key = k.lower()
            if key in _PII_DROP_FIELDS:
                continue
            if key in _ID_FIELDS:
                # Identifier → opaque surrogate (unconditional tokenize; empty stays absent).
                text = str(v).strip() if v is not None else ""
                out[k] = safe_identifier(text) if text else None
                continue
            if key == "source":
                # Provenance → grounded citation only if it resolves to an authorized system of record
                # (privacy-tokenized); unverifiable / free-text source → None → S-3 blocks (needs_review).
                out[k] = resolve_provenance(v)
                continue
            out[k] = _hygiene_obj(v)
        return out
    if isinstance(obj, list):
        return [_hygiene_obj(v) for v in obj]
    if isinstance(obj, str):
        return _hygiene(obj)
    return obj


class PreProcessNode(FunctionNode):
    """Validate the bordereau package, minimise PII, and extract its entries / period / treaty slots."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _reject_code(self, raw: str) -> str | None:
        if len(raw) > _MAX_INPUT:
            return "INPUT_TOO_LONG"
        if any(marker in _nfkc(raw).lower() for marker in _INJECTION_MARKERS):
            return "INJECTION_REJECTED"
        return None

    def _extra_security_gate_input(self, state: dict[str, Any]) -> dict[str, Any]:
        """S-2 domain checks: size cap + prompt-injection markers.

        SDK 1.0.0 contract: MUST NOT raise, and MUST NOT set status=ERROR (that would short-circuit the
        pipeline past post_process). A rejection is surfaced as a degraded `SUCCESS + error_code`; the
        offending body is discarded by execute().
        """
        raw = state.get("user_input", "") or ""
        code = self._reject_code(raw)
        if code:
            out = dict(state)
            out["error_code"] = code
            return out
        return dict(state)

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        raw = state.get("user_input", "") or ""
        input_context = state.get("input_context", {})  # read-only [C1]
        enriched = json.dumps(
            {
                "source": "InsuranceReinsuranceBordereauCompletenessValidatorAgent",
                "channel": input_context.get("channel", "unknown"),
            },
            ensure_ascii=False,
        )

        # Degrade on rejection: the S-2 hook may already have set error_code (real SDK); re-detect here
        # because the local stub framework does not invoke the hook. Discard the offending body entirely.
        prior = state.get("error_code")
        code = prior if prior in _REJECT_CODES else self._reject_code(raw)
        if code:
            emit_trace_event("bordereau_ingest.rejected", {"reason": code}, state)
            return {
                "validated_input": "{}",
                "input_format": "rejected",
                "enriched_context": enriched,
                "user_input": "",
                "error_code": code,
                "status": AgentStatus.SUCCESS.value,
            }

        if not raw.strip():
            emit_trace_event("bordereau_ingest.rejected", {"reason": "empty_input"}, state)
            return {
                "validated_input": "{}",
                "input_format": "empty",
                "enriched_context": enriched,
                "error_code": "INPUT_REJECTED",
                "status": AgentStatus.SUCCESS.value,
            }

        slots, fmt = self._parse(_CONTROL.sub("", _nfkc(raw)))
        emit_trace_event(
            "bordereau_ingest.validated",
            {
                "input_format": fmt,
                "entry_count": len(slots["entries"]),
                "reporting_period": slots.get("reporting_period"),
            },
            state,
        )
        return {
            "validated_input": json.dumps(slots, ensure_ascii=False),
            "input_format": fmt,
            "enriched_context": enriched,
            "status": AgentStatus.SUCCESS.value,
        }

    def _parse(self, text: str) -> tuple[dict[str, Any], str]:
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            return {"entries": [], "package_id": None, "reporting_period": None, "treaty_ref": None}, "text"
        if isinstance(obj, dict):
            entries = obj.get("entries")
            entries = entries if isinstance(entries, list) else []
            pkg = obj.get("package_id")
            # package_id is a caller identifier → privacy-tokenize; other top-level fields are hygiened.
            return {
                "entries": _hygiene_obj(entries),
                "package_id": safe_identifier(str(pkg).strip()) if pkg else None,
                "reporting_period": _hygiene_obj(obj.get("reporting_period")),
                "treaty_ref": _hygiene_obj(obj.get("treaty_ref")),
            }, "json"
        if isinstance(obj, list):  # bare entries array
            return {
                "entries": _hygiene_obj(obj),
                "package_id": None,
                "reporting_period": None,
                "treaty_ref": None,
            }, "json"
        return {"entries": [], "package_id": None, "reporting_period": None, "treaty_ref": None}, "text"
