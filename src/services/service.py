"""INS-C2-945 — deterministic domain services (no framework imports, no LLM).

BordereauCompletenessService: normalizes supplied reinsurance-bordereau entries into a canonical field set,
checks each entry's administrative completeness against the seeded carrier-approved completeness profile
(required-field presence + period/currency/claim-reference / ceded-vs-gross consistency), reconciles the
seeded evidence checklist (conditional on large-loss / recovery signals) against the supplied evidence
references, and composes a candidate Completeness Exception Register (needs-review).

Everything here is deterministic and auditable (presence/consistency comparison + keyed KB composition) —
there is **no LLM**. Entries are keyed by an opaque ``entry_id``; raw insured/claimant names and monetary
amounts are minimised (dropped / redacted) at input, and the S-3 output gate re-redacts any that leak. The
agent computes no reserve, decides no coverage/payability, approves no bordereau, and contacts no reinsurer.
Seeded profile / checklist / treaty metadata are overridable by CoE without touching node logic.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

# Two SEPARATE concerns — do not conflate them:
#   (1) PRIVACY (safe_identifier): every caller identifier (entry_id / package_id) is UNCONDITIONALLY
#       tokenized to a deterministic opaque surrogate so PII (even a bare name like ``Alice`` / ``John.Smith``
#       / ``TaroYamada``, no spaces/symbols) can never reach a citation or the output. Tokenizing is a
#       privacy measure — it does NOT assert the value is authorized/verifiable.
#   (2) PROVENANCE (resolve_provenance): a caller ``source`` becomes a grounded CITATION only when it is
#       resolvable against the authorized provenance registry (names a trusted system of record). Any other
#       free text (an insured/claimant name, ``unknown``, a fabricated value) is NOT verifiable provenance →
#       it yields NO citation → S-3 blocks the register as CITATION_INCOMPLETE (fail-closed). "Tokenized" is
#       never sufficient for a citation; the value must first pass provenance validation.
# Tokenization is UNCONDITIONAL (no syntactic passthrough): a caller value merely *shaped* like a surrogate
# (``bdx:deadbeef``) is re-hashed, never trusted, so it can never forge an internal join key. Identifiers /
# provenance are resolved exactly once at S-1 (pre_process); downstream trusts that resolution verbatim.
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_\-]{1,24}$")

# Authorized provenance registry: the systems of record a reinsurance-ops org trusts as verifiable data
# sources. A caller ``source`` is accepted as a grounded citation ONLY when its leading namespace names one
# of these (the "trusted context"). This is the deploying org's / CoE's registry — overridable without
# touching node logic; it is a SEMANTIC allowlist of authorized systems, not a syntactic character class.
AUTHORIZED_PROVENANCE_SYSTEMS = frozenset(
    {
        "policy_admin",
        "pas",
        "policy_system",
        "policy_repository",
        "claims",
        "claims_system",
        "claim_ledger",
        "claims_repository",
        "treaty",
        "treaty_system",
        "reinsurance_system",
        "ri_system",
        "ceded_system",
        "bordereau_system",
        "bordereau_repository",
        "bdx_repository",
        "gl",
        "general_ledger",
        "finance_system",
        "accounting_system",
        "dms",
        "document_management",
        "evidence_repository",
        "system_of_record",
        "sor",
        "authorized_feed",
        "data_warehouse",
        "dwh",
    }
)


def _sha8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def safe_identifier(value: Any) -> str:
    """PRIVACY tokenize a caller identifier to a deterministic opaque surrogate ``bdx:<sha8>``.

    Caller identifiers (``entry_id`` / ``package_id``) are **always** tokenized — no syntactic passthrough —
    so a name (with or without spaces) can never survive into a citation or the output, and a caller value
    merely *shaped* like a surrogate (``bdx:deadbeef``) is re-hashed rather than trusted (it can never forge
    an internal join key). Same input → same surrogate (register / citations / summary stay joinable). This
    is a privacy measure only; it makes no claim that the identifier is authorized.
    """
    return "bdx:" + _sha8(str(value or "").strip())


def resolve_provenance(value: Any) -> str | None:
    """Resolve a **raw** caller ``source`` to a grounded, privacy-tokenized CITATION — or ``None``.

    Provenance validation (separate from privacy) and the **single** resolution point (S-1 / pre_process).
    A citation is emitted **only** when the source names an authorized system of record
    (``<authorized-namespace>[:<ref>]``). Any other value — an insured/claimant name, ``unknown``, a
    fabricated value, **or a value that merely looks like a surrogate (``src:1a2b3c4d`` / ``bdx:deadbeef``)**
    — is not verifiable provenance and returns ``None`` so the S-3 gate blocks the register as
    CITATION_INCOMPLETE (fail-closed). When authorized, the raw label is never used verbatim: the citation is
    a privacy hash (``src:<sha8>``) of the authorized reference. No synthetic provenance is fabricated.

    ★ Forged-surrogate defence: there is **no format-based passthrough**. A caller-supplied ``src:<hex>`` /
    ``bdx:<hex>`` has namespace ``src`` / ``bdx``, which is not an authorized system of record, so it resolves
    to ``None`` — it is dropped here at S-1 and can never reach a citation. Because provenance is resolved
    exactly once (here), the produced ``src:<sha8>`` is the trusted citation downstream and is **never** fed
    back through this function, so no forged value can imitate an internal surrogate.
    """
    text = str(value or "").strip()
    if not text:
        return None
    namespace = text.split(":", 1)[0].strip().lower()
    if namespace not in AUTHORIZED_PROVENANCE_SYSTEMS:
        return None  # unverifiable / forged-surrogate provenance → fail-closed (no citation → needs_review)
    return "src:" + _sha8(text)


# ── seeded carrier-approved completeness profile: required field → (description, cited clause) ──────
BORDEREAU_REQUIRED_FIELDS: dict[str, str] = {
    "claim_reference": "Unique claim / risk reference identifying the ceded item",
    "period": "Reporting period the entry belongs to",
    "currency": "ISO currency code of the monetary amounts",
    "gross_amount": "Gross paid / incurred amount",
    "ceded_amount": "Amount ceded to the reinsurer under the treaty",
    "date_of_loss": "Date of loss / occurrence",
    "cause_of_loss": "Cause / peril classification",
}
COMPLETENESS_PROFILE_REFS: dict[str, str] = {
    "claim_reference": "PROFILE-1.1",
    "period": "PROFILE-1.2",
    "currency": "PROFILE-1.3",
    "gross_amount": "PROFILE-2.1",
    "ceded_amount": "PROFILE-2.2",
    "date_of_loss": "PROFILE-3.1",
    "cause_of_loss": "PROFILE-3.2",
}
# consistency-rule clause references (period / currency / amount / uniqueness)
CONSISTENCY_PROFILE_REFS: dict[str, str] = {
    "period_mismatch": "PROFILE-1.2 (entry period must equal the package reporting period)",
    "currency_mismatch": "PROFILE-1.3 (gross/ceded currency must equal the entry currency)",
    "ceded_exceeds_gross": "PROFILE-2.3 (ceded amount must not exceed the gross amount)",
    "duplicate_claim_reference": "PROFILE-1.1 (claim reference must be unique within the package)",
}

# ── seeded evidence checklist: item → (description, condition, cited treaty clause) ──
# condition: "always" | "large_loss" | "recovery". Each absent item is a needs-review candidate only —
# never an executed action; the reinsurance owner decides remediation at the HumanApprovalGate.
EVIDENCE_CHECKLIST: dict[str, dict[str, str]] = {
    "claim_form": {
        "description": "Signed claim form / loss notification",
        "condition": "always",
        "treaty_ref": "TREATY-EVID-1",
    },
    "proof_of_payment": {"description": "Proof of gross payment", "condition": "always", "treaty_ref": "TREATY-EVID-2"},
    "loss_adjuster_report": {
        "description": "Independent loss-adjuster report (large losses)",
        "condition": "large_loss",
        "treaty_ref": "TREATY-EVID-3",
    },
    "subrogation_note": {
        "description": "Subrogation / recovery note (when a recovery is present)",
        "condition": "recovery",
        "treaty_ref": "TREATY-EVID-4",
    },
}

# ── seeded treaty / reference metadata: clause key → cited reference ──
TREATY_REFERENCE_METADATA: dict[str, str] = {
    "reporting_cadence": "TREATY-3.1 reporting cadence & period alignment",
    "currency_of_account": "TREATY-3.2 currency of account",
    "evidence_requirements": "TREATY-4.0 supporting-evidence requirements",
}

_LARGE_LOSS_THRESHOLD = 50_000_000.0  # currency-of-account units; large losses require an adjuster report
_SEVERITY_RANK = {"high": 3, "med": 2, "low": 1}


def _num_or_none(value: Any) -> float | None:
    """Coerce to float; absent / non-numeric → None (so presence checks can distinguish missing)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean_str(value: Any) -> str | None:
    """Return a stripped string, or None if absent / empty."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _safe_token(value: Any) -> str | None:
    """Keep a short safe enum-like token (currency / evidence key); anything else → None."""
    text = str(value or "").strip()
    return text if _SAFE_TOKEN.match(text) else None


class BordereauCompletenessService:
    """Deterministic normalization, completeness checking, evidence reconciliation, and register composition."""

    # ── normalization ────────────────────────────────────────────────────────
    @staticmethod
    def normalize(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Validate + canonicalize bordereau entries into a checkable field set. Rows without ``entry_id``
        are dropped.

        ``entry_id`` is always privacy-tokenized. ``source`` was already resolved to a grounded citation
        (``src:<sha8>``) or ``None`` by pre_process (S-1), the single provenance-resolution point — a forged
        surrogate was dropped there. normalize trusts that value verbatim; it never re-resolves and never
        fabricates provenance. Absent required numerics are kept as ``None`` so presence checks are exact.
        """
        out: list[dict[str, Any]] = []
        for raw in entries or []:
            if not isinstance(raw, dict):
                continue
            raw_id = str(raw.get("entry_id") or raw.get("id") or "").strip()
            if not raw_id:
                continue
            currency = _safe_token(raw.get("currency"))
            gross = _num_or_none(raw.get("gross_amount"))
            evidence_refs = [t for t in (_safe_token(e) for e in raw.get("evidence_refs", []) or []) if t]
            # Tokenize entry_id unconditionally (no forgeable passthrough). pre_process (S-1) already tokenized
            # it to an opaque surrogate; re-tokenizing here is deterministic and idempotent-in-effect (entry_id
            # is a per-entry label joined only within this invocation's own output — register and citations are
            # both built from this value, so they stay consistent — never against the S-1 value).
            out.append(
                {
                    "entry_id": safe_identifier(raw_id),
                    "claim_reference": _clean_str(raw.get("claim_reference")),
                    "period": _clean_str(raw.get("period")),
                    "currency": currency,
                    "gross_currency": _safe_token(raw.get("gross_currency")) or currency,
                    "ceded_currency": _safe_token(raw.get("ceded_currency")) or currency,
                    "gross_amount": gross,
                    "ceded_amount": _num_or_none(raw.get("ceded_amount")),
                    "date_of_loss": _clean_str(raw.get("date_of_loss")),
                    "cause_of_loss": _clean_str(raw.get("cause_of_loss")),
                    "recovery_amount": _num(raw.get("recovery_amount")),
                    "large_loss": bool(raw.get("large_loss")) or (gross is not None and gross >= _LARGE_LOSS_THRESHOLD),
                    "evidence_refs": evidence_refs,
                    # pre_process (S-1) already resolved `source` to `src:<sha8>` or None — passed through verbatim.
                    "source": raw.get("source"),
                }
            )
        return out

    @staticmethod
    def duplicate_claim_references(entries: list[dict[str, Any]]) -> set[str]:
        """Claim references that appear on more than one entry (uniqueness-consistency input)."""
        seen: dict[str, int] = {}
        for e in entries:
            ref = e.get("claim_reference")
            if ref:
                seen[ref] = seen.get(ref, 0) + 1
        return {ref for ref, n in seen.items() if n > 1}

    # ── completeness check ────────────────────────────────────────────────────
    @staticmethod
    def check_completeness(entry: dict[str, Any], dup_refs: set[str], reporting_period: str | None) -> dict[str, Any]:
        """Check one normalized entry for required-field presence + period/currency/claim-ref/amount
        consistency against the seeded completeness profile. Carries downstream fields (evidence signals)."""
        missing: list[dict[str, str]] = []
        for field, desc in BORDEREAU_REQUIRED_FIELDS.items():
            val = entry.get(field)
            if val is None or (isinstance(val, str) and not val.strip()):
                missing.append({"field": field, "description": desc, "profile_ref": COMPLETENESS_PROFILE_REFS[field]})

        inconsistencies: list[dict[str, str]] = []
        period = entry.get("period")
        if reporting_period and period and str(period) != str(reporting_period):
            inconsistencies.append(
                {
                    "kind": "period_mismatch",
                    "detail": f"entry period {period} != reporting period {reporting_period}",
                    "profile_ref": CONSISTENCY_PROFILE_REFS["period_mismatch"],
                }
            )
        currency = entry.get("currency")
        for cur_field in ("gross_currency", "ceded_currency"):
            cur = entry.get(cur_field)
            if currency and cur and cur != currency:
                inconsistencies.append(
                    {
                        "kind": "currency_mismatch",
                        "detail": f"{cur_field} {cur} != currency {currency}",
                        "profile_ref": CONSISTENCY_PROFILE_REFS["currency_mismatch"],
                    }
                )
        gross, ceded = entry.get("gross_amount"), entry.get("ceded_amount")
        if gross is not None and ceded is not None and ceded > gross:
            inconsistencies.append(
                {
                    "kind": "ceded_exceeds_gross",
                    "detail": f"ceded_amount {ceded} > gross_amount {gross}",
                    "profile_ref": CONSISTENCY_PROFILE_REFS["ceded_exceeds_gross"],
                }
            )
        claim_ref = entry.get("claim_reference")
        if claim_ref and claim_ref in dup_refs:
            inconsistencies.append(
                {
                    "kind": "duplicate_claim_reference",
                    "detail": "claim_reference is not unique within the package",
                    "profile_ref": CONSISTENCY_PROFILE_REFS["duplicate_claim_reference"],
                }
            )

        # deterministic retrieval: the register cites every profile clause the check evaluated against, plus
        # any consistency clause that fired.
        cited_profile_refs = sorted(
            set(COMPLETENESS_PROFILE_REFS.values()) | {i["profile_ref"] for i in inconsistencies}
        )
        state = "complete" if not missing and not inconsistencies else "needs_review"
        return {
            "entry_id": entry["entry_id"],
            "claim_reference": claim_ref,
            "completeness_state": state,
            "missing_required_fields": missing,
            "inconsistencies": inconsistencies,
            "cited_profile_refs": cited_profile_refs,
            "source": entry["source"],
            # carry-through for evidence reconciliation
            "large_loss": entry["large_loss"],
            "recovery_amount": entry["recovery_amount"],
            "evidence_refs": entry["evidence_refs"],
        }

    # ── evidence reconciliation ───────────────────────────────────────────────
    @staticmethod
    def reconcile_evidence(checked: dict[str, Any]) -> dict[str, Any]:
        """Reconcile the seeded evidence checklist (conditional on large-loss / recovery) against the
        supplied evidence references; record absent items + cited treaty clauses."""
        required: list[str] = []
        for key, spec in EVIDENCE_CHECKLIST.items():
            cond = spec["condition"]
            applies = (
                cond == "always"
                or (cond == "large_loss" and checked.get("large_loss"))
                or (cond == "recovery" and checked.get("recovery_amount", 0.0) > 0)
            )
            if applies:
                required.append(key)
        provided = set(checked.get("evidence_refs", []))
        absent = [
            {
                "item": key,
                "description": EVIDENCE_CHECKLIST[key]["description"],
                "treaty_ref": EVIDENCE_CHECKLIST[key]["treaty_ref"],
            }
            for key in required
            if key not in provided
        ]
        cited_treaty_refs = sorted(
            {EVIDENCE_CHECKLIST[key]["treaty_ref"] for key in required}
            | {
                TREATY_REFERENCE_METADATA["evidence_requirements"],
                TREATY_REFERENCE_METADATA["reporting_cadence"],
                TREATY_REFERENCE_METADATA["currency_of_account"],
            }
        )
        return {
            "entry_id": checked["entry_id"],
            "required_evidence": required,
            "absent_evidence_refs": absent,
            "cited_treaty_refs": cited_treaty_refs,
        }

    # ── exception composition ─────────────────────────────────────────────────
    @staticmethod
    def compose_exception(checked: dict[str, Any], reconciled: dict[str, Any]) -> dict[str, Any]:
        """Build the per-entry candidate exception-register block (never an approval / disposition)."""
        absent = reconciled.get("absent_evidence_refs", [])
        needs_review = bool(checked["missing_required_fields"] or checked["inconsistencies"] or absent)
        if checked["missing_required_fields"]:
            severity = "high"
        elif checked["inconsistencies"] or absent:
            severity = "med"
        else:
            severity = "low"
        return {
            "entry_id": checked["entry_id"],
            "claim_reference": checked["claim_reference"],
            "completeness_status": "needs_review" if needs_review else "complete",
            "exception_severity": severity,
            "missing_required_fields": checked["missing_required_fields"],
            "inconsistencies": checked["inconsistencies"],
            "absent_evidence_refs": absent,
            "cited_profile_refs": checked["cited_profile_refs"],
            "cited_treaty_refs": reconciled.get("cited_treaty_refs", []),
            # candidate only — the reinsurance owner decides the actual disposition at the HumanApprovalGate.
            "candidate_disposition": (
                "escalate_to_reinsurance_owner" if needs_review else "accept_pending_owner_confirmation"
            ),
            "citation": checked["source"],
        }

    @staticmethod
    def package_summary(register: list[dict[str, Any]]) -> dict[str, Any]:
        """Package-level rollup: entry count, completeness-status distribution, entries needing review."""
        distribution = {"complete": 0, "needs_review": 0}
        for e in register:
            distribution[e["completeness_status"]] = distribution.get(e["completeness_status"], 0) + 1
        needs_review = [e["entry_id"] for e in register if e["completeness_status"] == "needs_review"]
        return {
            "total_entries": len(register),
            "status_distribution": distribution,
            "entries_needing_review": needs_review,
            "overall_completeness_status": "needs-review" if needs_review else "complete",
        }

    @staticmethod
    def severity_rank(severity: str) -> int:
        return _SEVERITY_RANK.get(severity, 0)
