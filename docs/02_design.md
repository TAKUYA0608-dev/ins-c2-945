# Template Design Specification — INS-C2-945

Reinsurance Bordereau Evidence Completeness Validator (Cat 2, GraphNode-in-main).

## Position in AgentCore Architecture

- **Agent Class**: `InsuranceReinsuranceBordereauCompletenessValidatorAgent` (module-level alias of `Graph`)
- **L1 Base**: AgentBaseGraph (L1 direct — Cat 2 GraphNode-in-main; **not** AutonomousBaseGraph)
- **Category**: Cat 2 — orchestrates a fixed multi-step workflow to produce one job-to-be-done deliverable
  (a candidate Completeness Exception Register for a supplied reinsurance bordereau package).
- **Three-Layer Separation**:
  - State: flat TypedDict composition (no Pydantic — msgpack incompatible); complex fields are JSON strings (ADR-005)
  - Node: L1 inheritance (Template Method: `execute(self, state: dict) -> dict` override only — no `config` param)
  - Graph: composition (`register_nodes()` for node substitution; domain complexity behind a `GraphNode`)

## Architecture Overview

### Node Configuration (outer 5-slot backbone)

| Node | Responsibility | Input State | Output State | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------------|
| initialize | schema/session/trust setup | user_input | caller_trust_level, session_id | InitializeNode (default) |
| pre_process | `BordereauPackageIngest` + `SensitiveDataDetectAndMinimise` — S-1/S-2 validation, NFKC, size cap, injection→degraded, **pre-LLM PII minimisation**: field-level input hygiene (credential/My-Number/email/phone redaction; insured/claimant/broker PII display fields dropped; **`entry_id`/`id` UNCONDITIONALLY privacy-tokenized to `bdx:<sha8>` — no syntactic passthrough, a bare name is opaque like any value**; **`package_id` tokenized**; **provenance `source` resolved to a citation ONLY if it names an authorized system of record (privacy-tokenized `src:<sha8>`), else dropped to `None`** — S-3 then blocks; `currency`/evidence keys constrained to safe enum tokens; `reporting_period`/`treaty_ref` hygiened), entry-slot extraction | user_input | validated_input, input_format, enriched_context, (error_code) | PreProcessNode (FunctionNode) |
| main | `BordereauCompletenessWorkflowGraphNode` — wraps inner `BordereauCompletenessWorkflow` (composition criterion #9) | validated_input | result, checked_count, human_review_required, (error_code), status | GraphNode (subgraph) |
| post_process | `OutputSanitise` — S-3 output gate: **fail-closed per-entry citation completeness** (ungrounded register → `needs_review` degrade, register body withheld, `error_code=CITATION_INCOMPLETE`) + insured/claimant-name/company/phone/email/credential/My-Number redaction + injection neutralisation + DRAFT disclaimer, S-4 no-persist audit | result | formatted_output, disclaimer, audit_logged, (error_code) | PostProcessNode (FunctionNode) |
| finalize | build response envelope | formatted_output | output, status | FinalizeNode (default) |

### Inner workflow (`src/graph/domain_workflow_graph.py` — BaseGraph, linear + per-node skip guard)

```
START → completeness_profile_check → evidence_reference_reconcile → exception_register_compose → human_gate → END
```

| Inner Node | Responsibility | Skip guard |
|------|---------------|-----------|
| completeness_profile_check | Deterministic ingest + normalize of the supplied bordereau entries; per-entry **required-field presence** + **period/currency/claim-reference / ceded-vs-gross consistency** checks against the seeded carrier-approved completeness profile, with cited profile clauses; set `checked_count`. **0 valid entries → `error_code=NO_ENTRIES` → out-of-scope safe answer** | — (first node; emits `.skip` on rejected/no-entry input) |
| evidence_reference_reconcile | Deterministic reconciliation of the seeded evidence checklist (conditional on large-loss / recovery signals) against each entry's supplied `evidence_refs`; records `absent_evidence_refs` + cited treaty/reference clauses | no-op `return {}` (after `.skip` emit) on `error_code` / `checked_count == 0` |
| exception_register_compose | Compose the candidate **Completeness Exception Register** deliverable: per entry, missing required fields + inconsistencies + absent evidence refs + cited profile/treaty refs + exception severity + per-entry citation; package summary. On 0-entry/rejected → out-of-scope safe answer | emits safe answer on `error_code` / no entries |
| human_gate | Deterministic **HumanApprovalGate**: mark `human_review_required=True` + `review_status="pending_human_approval"`, record the material exceptions (entries in `needs_review`) that require an authorized reinsurance owner's sign-off before any bordereau approval / remediation. Owner / approver assignment is **never** made by the agent | no-op `return {}` (after `.skip` emit) on `error_code` / `checked_count == 0` (safe answer needs no human gate) |

> **Conditional edges do not propagate across the subgraph boundary** (GraphNode wraps the inner graph),
> so the inner topology is a **static linear backbone with per-node skip guards** — the portable Cat 2 form
> shipped across the fleet. `add_conditional_edges` is intentionally **not** used inside the subgraph.

### Data Flow

```
START → initialize → pre_process → main(GraphNode) → {route} → post_process → finalize → END
                                        ↓ (retry, max 3)
                                     pre_process
```

**Degraded / rejected path (mandatory contract).** An injection marker, an oversize payload, empty input,
or zero valid entries never sets `status=ERROR`. Instead the node returns `status=SUCCESS` **plus an
`error_code`** (`INJECTION_REJECTED` / `INPUT_TOO_LONG` / `INPUT_REJECTED` / `NO_ENTRIES`). This is
deliberate: in the production framework `status=ERROR` short-circuits `AgentBaseGraph.route()` straight to
`finalize`, so **`main`/`post_process` would be skipped and the mandatory DRAFT disclaimer + S-3 redaction
+ S-4 terminal audit would never run**. With `SUCCESS + error_code`, `route()` reaches `post_process`,
which always emits the out-of-scope safe answer, disclaimer, and audit. On the injection/oversize path
`pre_process` **discards the offending body** (`validated_input="{}"`, `user_input` cleared) so no rejected
content is ever processed. Because `GraphNode.extract_input()` passes only `validated_input` into the fresh
inner state, `merge_output` surfaces the **outer** `error_code` first (`state.get("error_code") or
sub_result.get("error_code")`) so a pre-stage rejection code survives to the terminal S-4 audit.

### State Definition (`src/schemas/state.py`)

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| validated_input | NotRequired[str] | JSON `{package_id, reporting_period, treaty_ref, entries[]}` from pre_process (PII minimised) | no |
| input_format | NotRequired[str] | `json` / `text` / `empty` | no |
| enriched_context | NotRequired[str] | JSON `{source, channel}` (read-only caller context) | no |
| checked_entries | NotRequired[str] | JSON per-entry completeness (missing fields + inconsistencies + cited profile refs + source) | no |
| checked_count | NotRequired[int] | entries checked (0 → out-of-scope safe answer) | no |
| reconciled | NotRequired[str] | JSON per-entry absent evidence refs + cited treaty refs | no |
| result | NotRequired[str] | JSON assembled candidate Completeness Exception Register (incl. human_review) | no |
| human_review_required | NotRequired[bool] | True once HumanApprovalGate flags material exceptions | no |
| review_status | NotRequired[str] | `pending_human_approval` / `not_required` | no |
| formatted_output | NotRequired[str] | JSON final response envelope (register + disclaimer) | no |
| disclaimer | NotRequired[str] | mandatory DRAFT / advisory-only disclaimer | no |
| audit_logged | NotRequired[bool] | True once terminal audit event emitted | no |
| error_code | NotRequired[str] | `INPUT_REJECTED` / `INJECTION_REJECTED` / `INPUT_TOO_LONG` / `NO_ENTRIES` / `CITATION_INCOMPLETE` | no |
| error_message | NotRequired[str] | operator-facing detail | no |

**State Constraints (mandatory):**
- Flat TypedDict only (primitives + JSON-serializable types); complex fields serialized as JSON strings (ADR-005)
- No JWT, API keys, credentials in State (checkpoint DB leakage) — pre_process input hygiene redacts them
- **Opaque-id boundary — privacy tokenize vs provenance validation are SEPARATE (primary defense = input
  side).**
  - **Privacy (identifiers).** `entry_id`/`id` (and `package_id`) is **unconditionally** tokenized to
    `bdx:<sha8>` — there is NO syntactic "this looks like a safe id" passthrough (that was a leak: a name such
    as `Alice` / `John.Smith` / `TaroYamada` with no spaces/symbols matched a loose grammar and passed
    through). Tokenizing hides PII; it makes **no** claim that the value is authorized.
  - **Provenance (citations).** A caller `source` becomes a grounded citation **only when it resolves to an
    authorized system of record** (`AUTHORIZED_PROVENANCE_SYSTEMS`, the deploying org's / CoE's trusted-context
    registry — a *semantic* allowlist of authorized systems, not a character class). Then it is
    privacy-tokenized to `src:<sha8>` (raw label never verbatim). Any other value — an insured/claimant name,
    `unknown`, a fabricated string, **or a value merely shaped like a surrogate (`src:1a2b3c4d` /
    `bdx:deadbeef`)** — is **not** verifiable provenance → `None` → S-3 blocks the register as
    `CITATION_INCOMPLETE` (fail-closed). "Tokenized" is never sufficient for a citation. No synthetic
    provenance is fabricated.
  - Only values already in the strict surrogate namespace (`bdx:` / `src:` + 8 hex — which a name can never
    match) pass through, making re-tokenization idempotent. `currency` / evidence keys are safe enum tokens;
    `reporting_period`/`treaty_ref` are hygiened. Numeric fields are coerced (`_num`). Every other output
    field is derived (presence/consistency verdicts / deterministic exception text) — arbitrary extra caller
    fields are never copied into the output. The whole-report name/phone/contact redactor is defense-in-depth only.
- InvocationContext via `config["configurable"]` only (not in State)
- No Pydantic models, dataclass, arbitrary Python objects (msgpack incompatible)

## Framework Utilization

### Shared Components Used
- [x] InvocationContext (correlation_id, session_id, caller_trust_level) — read-only inside nodes
- [x] **S-2**: `_extra_security_gate_input(self, state) -> dict` on `PreProcessNode` — size cap +
      prompt-injection markers + **pre-LLM field-level input hygiene / PII minimisation** (claim-level
      contract-holder / insured / claimant data). SDK 1.0.0 contract: **MUST NOT raise, and MUST NOT return
      `status=ERROR`**. A rejection is surfaced as a **degraded `SUCCESS + error_code`**
      (`INJECTION_REJECTED` / `INPUT_TOO_LONG`); `pre_process.execute()` re-checks the same conditions
      (the `@final` hook is not invoked by the local stub framework) and discards the offending body so the
      pipeline reaches `post_process` and always emits the disclaimer + audit. **Injection containment is
      performed pre-LLM (here at input) AND independently at S-3 output — two separate gates.**
- [x] **S-3**: `PostProcessNode.execute()` enforces **fail-closed per-entry citation completeness** — a
      grounded register with any entry missing a verifiable `source` is not presented; it degrades to a safe
      `needs_review` answer (register body withheld, `error_code=CITATION_INCOMPLETE`, still SUCCESS so the
      disclaimer + S-4 audit run). Provenance is an allowlisted authorized reference (validated in pre_process
      and never fabricated). A whole-report redactor re-redacts credential / My-Number / email / **phone** /
      **insured-/claimant-/company-name** leakage + neutralises injection markers.
      `_extra_security_gate_output(self, result) -> dict` additionally verifies the mandatory DRAFT
      disclaimer is present and MAY raise to block.
- [x] **S-4**: `emit_trace_event()` inside **every** `execute()` path (including skip / safe branches) —
      domain event only (never `node_start`/`node_complete` which `BaseNode.__call__()` emits automatically).
      Counts / status distribution / error_code only — never a raw insured/claimant name, amount, or
      claim narrative. **No-persist**: raw bordereau records / PII are never retained in the audit.

> **S-2/S-3 gate behaviour by node type (ADR-017):**
> - `FunctionNode` subclass (`PreProcessNode`, `PostProcessNode`, and the four inner nodes) → framework
>   `@final` gate always runs automatically; extend via `_extra_security_gate_input()` / `_extra_security_gate_output()` only.
> - `GraphNode` (`BordereauCompletenessWorkflowGraphNode`) → deliberate no-op (the wrapped subgraph nodes' gates already apply).

### Trust Level (S-1)
All concrete `FunctionNode` subclasses declare `required_trust_level = TrustLevel.VERIFIED_EXTERNAL`
explicitly (gate-trust-level-check), matching the agent-level `required_trust_level` in `config/agent.yaml`.
`GraphNode` is excluded by design (delegated S-1).

### Determinism (no LLM)
The template is **fully deterministic** — no LLM is used. Required-field presence, period/currency/claim-ref
consistency detection, evidence-checklist reconciliation, and exception composition are pure comparison +
keyed KB composition (the completeness profile + evidence checklist + treaty reference metadata in
`src/services/service.py`), so the register is reproducible and auditable. `config/agent.yaml` declares no
model and `pyproject.toml` declares no LLM dependency. (The proposal's "bounded LLM interpretation" of prose
evidence is scoped to a future extension; the shipped template is deterministic — see docs/01 reconciliation.)

### Composition Pattern
- **Pattern**: GraphNode (subgraph) — outer `AgentBaseGraph` 5-slot backbone with a `GraphNode` in the `main`
  slot wrapping an inner `BaseGraph` (`BordereauCompletenessWorkflow`).
- **Composition target**: `src/graph/domain_workflow_graph.py::BordereauCompletenessWorkflow`
- **Subgraph caching**: `get_subgraph()` caches the inner graph on the **class attribute** (`BordereauCompletenessWorkflowGraphNode._subgraph`, not `self` — avoids mutable node-instance state per §9; built once).
- **Error propagation strategy**: `propagate` — an inner ERROR surfaces as `SubgraphError`; the deterministic
  degraded path (0-entry / rejected) instead returns `status = SUCCESS + error_code` and a safe answer.

## Import Isolation Confirmation
- [x] Template does not import agenticstar-platform SDK (Level 0) — PB-4
- [x] Import targets: `framework/` and `shared/` only (via `src.utils.audit` fallback shim)

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | **AgentBaseGraph** | AutonomousBaseGraph | **AgentBaseGraph** | Fixed multi-step workflow, no autonomous reasoning loop — Cat 2 |
| Composition pattern | FunctionNode-in-main (Cat 1) | **GraphNode-in-main (Cat 2)** | **GraphNode-in-main** | Domain workflow has ≥4 ordered steps → encapsulate behind a subgraph |
| Inner topology | conditional edges | **linear + skip guards** | **linear + skip guards** | Conditional edges do not propagate across the subgraph boundary |
| Completeness check | LLM narrative | **deterministic profile/checklist reconciliation** | **deterministic** | Auditable, reproducible; no LLM — see Determinism |
| Human sign-off | agent auto-approves bordereau | **HumanApprovalGate (candidate register)** | **HumanApprovalGate** | Advisory only; bordereau approval / remediation is the reinsurance owner's, never the agent's |
| Degraded path | status=ERROR | **status=SUCCESS + error_code** | **SUCCESS + error_code** | ERROR would skip post_process (S-3/S-4) in production FW |

## Open Items (deferred to Stage ③ Implementation MR)
- Node `execute()` bodies (`completeness_profile_check`, `evidence_reference_reconcile`,
  `exception_register_compose`, `human_gate`, rewritten `pre_process` / `post_process`), the inner
  `BordereauCompletenessWorkflow`, `BordereauCompletenessService` (completeness profile + evidence checklist
  + treaty reference metadata + normalization), `src/utils/audit.py` (S-4 shim), unit / integration /
  boundary tests, and docs/03 + docs/07 land in the Stage ③ implementation MR. This design MR ships
  `docs/02_design.md` + `src/schemas/state.py` (+ docs/01 reference copy) only.
