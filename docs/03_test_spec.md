# Test Specification — INS-C2-945

## Test Strategy
- Coverage target: **80%+** (achieved **95%**, `--cov=src`)
- Test types: Unit (pre/post + inner nodes + services) / Unit (Cat 2 graph wiring + real invoke) / Integration / Proof-of-Boundary
- Determinism: **no LLM** — required-field presence, period/currency/claim-ref/amount consistency detection,
  evidence-checklist reconciliation, and exception composition are pure comparison + keyed profile/checklist/
  treaty composition (reproducible, auditable). No model is declared in `config/agent.yaml` and no LLM
  dependency in `pyproject.toml`.

## Framework Compliance Tests (Mandatory)

| TC-ID | Test | Expected Result | Result |
|-------|------|----------------|--------|
| TC-01 | State contract: flat TypedDict | `State(AgentState)`, NotRequired primitives + JSON strings; no PII/credential fields | ✅ PASS |
| TC-02 | S-2 rejection is degraded, never `status=ERROR` | `_extra_security_gate_input` sets `error_code` (`INJECTION_REJECTED`/`INPUT_TOO_LONG`) and returns `dict(state)`; never raises, never sets `status=ERROR` | ✅ PASS |
| TC-03 | No JWT/Credential in `src/` | `gate-credential-scan`: 0 violations | ✅ PASS |
| TC-04 | InvocationContext read-only via `from_state()` | never stored in State | ✅ PASS |
| TC-05 | S-4: no duplicate lifecycle events | only domain events emitted (never node_start/complete) | ✅ PASS |
| TC-06 | S-2 `_security_gate_input()` not overridden | `@final`; only `_extra_*` extended | ✅ (real SDK on CI; local-stub env-diff) |
| TC-07 | S-3 `_security_gate_output()` not overridden | `@final`; may raise via `_extra_*` | ✅ (real SDK on CI; local-stub env-diff) |
| TC-08 | `required_trust_level` explicit on every FunctionNode | `VERIFIED_EXTERNAL` (gate-trust-level-check) | ✅ PASS |
| TC-09 | Cat consistency | Template ID / config / README all Cat 2 (gate-cat-consistency) | ✅ PASS |
| TC-10 | Exact dependency pins | `==` in all sections incl. `[build-system]` setuptools==68.0.0 (gate-dep-pinning) | ✅ PASS |
| TC-11 | S-4: ≥1 domain `emit_trace_event()` per `execute()` | emitted on every path (incl. skip / safe branches) | ✅ PASS |
| TC-12 | Degraded path never `status=ERROR` | injection / oversize / empty / 0-entry → `SUCCESS.value + error_code`; `post_process` still runs | ✅ PASS |
| TC-13 | Pre-LLM PII minimisation: PII / secrets never persist in State | insured/claimant name / contact email dropped; credential / My-Number / email / phone redacted from `validated_input`; `reporting_period`/`treaty_ref` hygiened; `entry_id`/`package_id` tokenized | ✅ PASS |
| TC-14 | Real `Graph().invoke()` degraded path | injection & oversize → SUCCESS + out-of-scope, `PostProcessNode` in node_history, error_code in terminal S-4 audit, rejected body absent, DRAFT disclaimer present | ✅ PASS |
| TC-15 | S-3 fail-closed citation completeness | grounded register with any entry missing a citation → `needs_review` degrade (SUCCESS + `CITATION_INCOMPLETE`), register body withheld, disclaimer + audit still run; verified via real `Graph().invoke()` | ✅ PASS |
| TC-16 | Provenance allowlist + output redaction + injection neutralisation | unsafe caller `source` (name/phone) dropped, never in output; `reporting_period` name/phone/email redacted in a grounded output; injection markers neutralised at S-3 (independent of the pre-LLM input gate) | ✅ PASS |
| TC-17 | Opaque-id boundary — privacy tokenize (identifiers) | `entry_id`/`id`/`package_id` → `bdx:<sha8>` UNCONDITIONALLY (a no-space name `Alice`/`John.Smith`/`TaroYamada` is tokenized, not passed through); deterministic + referentially consistent (citation id == register id); surrogate namespace idempotent; arbitrary extra caller fields never reach output; verified via real `Graph().invoke()` | ✅ PASS |
| TC-18 | Provenance validation (separate from privacy) | a caller `source` is a grounded citation ONLY if it names an authorized system of record; unverifiable source (name / `unknown` / fabricated / **forged surrogate `src:…`/`bdx:…`**) → `None` → `needs_review` (CITATION_INCOMPLETE), never a citation; authorized `claims:…`/`treaty_system:…` → privacy-tokenized `src:<sha8>` (raw not in output); verified via real `Graph().invoke()` | ✅ PASS |

## Proof-of-Boundary Tests (Mandatory)

| PB-ID | Boundary | Expected Result | Result |
|-------|----------|----------------|--------|
| PB-2/5 | Post-invoke State is primitives only; no credential fields | AST scan: 0 violations | ✅ PASS |
| PB-4 | Import isolation — no Level 0 (`agenticstar`) imports | AST scan: 0 violations | ✅ PASS |
| PB-6 | Invoke order S-1 → S-4(start) → S-2 → execute → S-3 → S-4(complete) | Order verified | ✅ (real SDK on CI; local-stub env-diff) |
| PB-7 | HITL interrupt propagation | conditional — SKIPPED (`hitl.enabled` not set for this template) | ✅ (n/a, skip) |
| S-0 | Cat 2 `GraphNode`-in-main wraps inner `BaseGraph` (cached `get_subgraph`) | gate-composition passes | ✅ PASS |

## Business Logic Tests

| BL-ID | Test | Input | Expected Result | Result |
|-------|------|-------|----------------|--------|
| BL-01 | Entry normalization | entry with claim/period/currency/amounts/evidence | tokenized entry_id; absent required numerics → None; large_loss derived; malformed rows dropped | ✅ PASS |
| BL-02 | Required-field presence check | entry missing date_of_loss/cause_of_loss | missing_required_fields flagged with cited profile clauses | ✅ PASS |
| BL-03 | Consistency checks | period/currency mismatch, ceded>gross, duplicate claim_reference | each inconsistency detected with cited profile clause | ✅ PASS |
| BL-04 | Evidence reconciliation | large-loss / recovery entry with partial evidence_refs | absent evidence items reported (adjuster report / subrogation note) + cited treaty clauses | ✅ PASS |
| BL-05 | Exception composition | checked + reconciled entry | severity high/med/low, needs_review vs complete, candidate_disposition (never an approval), per-entry citation | ✅ PASS |
| BL-06 | Register ordering + summary | mixed package | entries ordered high-severity-first; status distribution + entries-needing-review; overall needs-review | ✅ PASS |
| BL-07 | HumanApprovalGate | register with needs_review entries | `human_review_required=True`, material exceptions recorded, no bordereau approval / owner assignment | ✅ PASS |
| BL-08 | Out-of-scope (no entries) | NL text / empty entries | `out_of_scope`, no citations, safe message | ✅ PASS |
| BL-09 | Empty input degrades | "   " | `SUCCESS.value + INPUT_REJECTED`, still audits | ✅ PASS |
| BL-10 | Insured name / amounts never in output | entry with insured_name | name absent from validated_input and output | ✅ PASS |
| BL-11 | Mandatory DRAFT disclaimer | any register | S-3 gate blocks output missing 参考/DRAFT | ✅ PASS |
| BL-12 | Ungrounded register withheld | entry with no/unsafe `source` | grounded register degrades to `needs_review`, no register body, `CITATION_INCOMPLETE` | ✅ PASS |
| BL-13 | Provenance safety / no leak + injection neutralised | unsafe `source` (name+phone) / PII in period / injection marker | dropped/redacted/neutralised — not in output | ✅ PASS |
| BL-14 | Privacy tokenize (entry_id) | `entry_id` = `Taro Yamada 090-…` **or no-space name** (`Alice`/`John.Smith`/`TaroYamada`) | tokenized to `bdx:<sha8>`; original absent; citation ↔ register id consistent | ✅ PASS |
| BL-15 | Unknown caller field not echoed | entry with arbitrary PII field | field never copied into output | ✅ PASS |
| BL-16 | Provenance validation (fail-closed) | `source` = name / `unknown` / fabricated / forged surrogate | no citation → `needs_review` (CITATION_INCOMPLETE); source not in output | ✅ PASS |
| BL-17 | Authorized provenance accepted | `source` = `claims:…` / `treaty_system:…` | grounded register; citation = tokenized `src:<sha8>`; raw not in output | ✅ PASS |

## Test Execution Summary
- Total: 91 template tests (unit-nodes 48 + unit-graph 37 + integration 6) + scaffold PB/compliance
  (import_isolation, state_safety, TC-06/07, PB-6, PB-7)
- Pass (template + boundary, local stub env): 92 · Skip: server-import (local stub env-diff) + PB-7 ×2 (conditional, HITL off)
- env-diff: `test_pb_invoke_order` (PB-6) + `test_framework_compliance_tc06_tc07` (TC-06/07) assert against
  the **real SDK on CI** (the CI dual-mode installs `agenticstar-agentcore` when the local SDK stub is absent — and
  the local SDK stub is gitignored, so CI is wheel-era); the local SDK stub lacks the `@final` gate enforcement / `emit_trace_event`
  surface, so these 3 pass on CI and env-diff locally (identical to the shipped fleet reference).
- Coverage: **95%** (`--cov=src`)
