# Issue #814 — Evidence-backed applicability/confidence envelope in UI and reports

An applicability-envelope presentation, **not** a single score. The
composer reads the sealed records minted by the landed authorities
(#809 benchmark qualification, #810 uncertainty-aware validation,
#811 input-authority bounds, plus the #580-family capability manifest
and the accuracy-envelope records) and produces a per-dimension
envelope: each of the seven issue-required dimensions maps to an
evidence state derived strictly from stored evidence — fail-closed,
never inferred.

There is **no aggregate confidence number** anywhere in the emitted
structure. Absent evidence renders as `未取得` (UNKNOWN), never as a
green/passed state. Capability declarations never promote to
verification: `サポート` (SUPPORTED) is a declared ability, always
displayed below every verified/validated class.

## Modules

- `htdt/cad_applicability_envelope.py` — sealed model
  (`ApplicabilityEnvelope`, `EnvelopeDimensionState`,
  `EnvelopePhenomenonCell`, `EnvelopeClaimRow`, `EnvelopeDecisionRow`),
  evidence bundle + context derivation, the fail-closed composer,
  and the report/export path (`ApplicabilityEnvelopeReport`,
  `build_envelope_report`, `render_envelope_report_text`, JA line
  helpers).
- `htdt/applicability_envelope_panel.py` —
  `ApplicabilityEnvelopePanel` + `ApplicabilityEnvelopeDialog`
  (Qt surface; semantic states map absence to UNSUPPORTED, never
  SUCCESS).
- `htdt/measurement_evidence_display.py` — `applicability_*` JA line
  helpers alongside the existing #810/#811 helpers.
- `application_pages.py` — `SupportPage` gains an
  「適用範囲エンベロープ」 button; `workflow_application.py` composes
  the envelope from the open document's stores and opens the dialog
  (lazy imports, same convention as the other inspector surfaces).

## The seven dimensions and their evidence sources

| # | dimension | evidence read (sealed stores) |
|---|-----------|-------------------------------|
| 1 | representational_capability | `cad_solver_capability_manifests` rows bound via the input envelope's `capability_manifest_ref` (sha-pinned) or context `adapter_descriptor_id`; staleness on adapter id/version/descriptor drift |
| 2 | verification | `AccuracyEnvelopeRecord`s passed in by the caller (in-memory solver envelopes filtered by `solver_id`, stale on solver-version drift) + qualification `convergence` fields |
| 3 | external_validation | `BenchmarkQualification` records filtered by `mapping.solver_path == context.solver_path`; frozen-configuration checks already done by #809; staleness on preregistration provider id/version drift |
| 4 | input_qualification | `SolverInputEnvelope` + `ClaimBoundRecord` rows (per-claim verdict/ceiling, weakest dimensions); staleness on scene-revision sha drift |
| 5 | owned_room_evidence | `UncertaintyValidationVerdict` with `evidence_scope == 'owned_room'` only (campaign id+sha+registration required by #810); `ranking_verdict` tracked separately, never as absolute validation; stale on protocol `model_version` drift |
| 6 | uncertainty_limitations | `ObservableUncertaintyEvaluation` per-observable summary verdicts + `limitations` + `dominant_uncertainty_category`; headline = worst unresolved negative |
| 7 | context_of_use | derived from dimensions 1–6 — see decision matrix below |

## State vocabulary (`EnvelopeEvidenceClass`)

```
externally_validated  外部実測検証済み   external measured PASS_WITHIN_DOMAIN
numerically_verified  数値検証済み       VALIDATED_FOR_DECLARED_DOMAIN / converged corpus
holdout_validated     実室ホールドアウト検証済み  owned-room absolute verdict
qualified             適格               envelope_inherited claims, no weakest dims
bounded               限定条件付き        bounded_by_input / VALIDATED_WITH_LIMITATIONS
supported             サポート対象        capability declaration — NOT a validation
insufficient_evidence 証拠不足           UNKNOWN verdicts / EXPERIMENTAL / partial
stale                 陳腐化             identity drift vs the evaluated context
failed                失格・失敗          FAIL / nonconverged / denied claims
outside_applicability 適用範囲外         OUTSIDE_APPLICABILITY verdicts
unsupported           非対応             declared UNSUPPORTED / NOT_APPLICABLE
absent                未取得             no stored evidence at all — renders UNKNOWN
```

Positive precedence (strongest first): externally_validated >
numerically_verified > holdout_validated > qualified > bounded >
supported. A dimension's headline is the strongest positive entry
**unless** a negative entry exists — then it is the most-informative
negative (failed > outside_applicability > unsupported >
insufficient_evidence > stale > absent) and the surviving positives
stay visible as `conflicts`. A FAIL is never hidden behind an absence,
and a stale record is never silently counted: it moves to
`stale_refs` + `staleness_notes` and the dimension falls back to
whatever evidence remains.

## Context of use (dimension 7)

Decisions, not scores:

- `inspect_prediction` （予測の確認） — allowed only when
  representational_capability is supported/bounded or better.
- `compare_candidates` （候補比較） — `allowed` only when owned-room
  evidence is `holdout_validated` AND external validation is
  `externally_validated`; `trend_only` when a ranking verdict
  (`ranking_supported`/`ranking_supported_with_limitations`) exists;
  `blocked` otherwise.
- `automatic_recommendation` （自動推奨） — **always `blocked`.** No
  landed authority can mint OWNED_ROOM_VALIDATED or
  PRODUCTION_RECOMMENDATION_ELIGIBLE evidence, so the envelope refuses
  to represent automatic recommendation as permitted. When such an
  authority lands, the gate opens honestly.

The per-phenomenon matrix (`EnvelopePhenomenonCell`) crosses each
declared phenomenon (capability state + capability band) with external
qualification and the claim verdicts whose requirement maps name that
phenomenon (`_CLAIM_REQUIREMENTS`).

## Model and integrity

`ApplicabilityEnvelope` is a sealed model (`extra='forbid'`, frozen):
`envelope_hash` covers the full identity payload, `envelope_id` is
`aer-<sha24>`-style semantic id, validators require all seven
dimensions in fixed order, all three decisions, and `absent` ⇒ no
verdict/refs while non-absent ⇒ verdict + refs required. Tampered
payloads fail at construction or load, never silently.

`ApplicabilityEnvelopeReport` seals the presentation snapshot
(`aer-` id + `report_sha256`) and binds each external qualification to
its benchmark identity (benchmark_id/version/sha256 from the
preregistration) so the exported report is replayable to exact
evidence.

## Integration points

- `load_envelope_evidence(scene_repository, document_id=...)` reads
  the landed repositories document-scoped; `context_for_document`
  derives the solver-path/adapter identity only when the sealed refs
  agree on exactly one identity (otherwise fail-closed to what is
  pinned).
- `compose_applicability_envelope(bundle, context, solver_envelopes=...)`
  is pure: same evidence ⇒ same envelope.
- UI: `workflow_application._open_applicability_envelope` (Support
  page) opens `ApplicabilityEnvelopeDialog`.
- Export: `render_envelope_report_text` produces the JA text block;
  `envelope_lines` / `envelope_dimension_line` /
  `envelope_decision_line` are the line helpers reused by report and
  UI, plus the `measurement_evidence_display.applicability_*` helpers.

## What stays manual

- **Real evidence population.** The composer reads only sealed stores:
  if no benchmark qualification, no claim bound record, no VUQ
  protocol/verdict was ever run for a document, those dimensions read
  `未取得`. The envelope surface does not mint, simulate, or infer
  evidence.
- Accuracy-envelope records remain caller-supplied (the accuracy
  envelope repository is in-memory by design); callers pass
  `solver_envelopes` into composition.
- Owned-room evidence requires #810 verdicts with
  `evidence_scope='owned_room'` — synthetic-fixture scope is tracked
  on the uncertainty dimension but never counts as owned-room
  validation.
- No schema change: the composition layer adds no sealed tables, so
  `NATIVE_SCHEMA_VERSION` is untouched.

## Tests

`backend/tests/test_issue_814_envelope.py` — every dimension state
(present/absent/insufficient), fail-closed rendering (no evidence ⇒
UNKNOWN, never PASS), no aggregate score anywhere in the emitted
structure (recursive structural assert), seal/id integrity, repository
round-trip + tamper detection, solver-path isolation, stale-identity
paths, JA label coverage, and the Qt panel.
