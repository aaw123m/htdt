# Issue #937 — Decision Brief：最適化候補を「次の一手」に変換する

## Scope

A sealed **Decision Brief** authority that converts optimization candidates
(`SystemVariant` / comparison-set alternatives plus declared evidence pins)
into a ranked, explainable next-action recommendation the operator lands on
after a comparison run. The brief never collapses evidence into a single
proprietary score and never auto-adopts a candidate: it states *why* the top
action outranks the next in one sentence grounded in the pinned evidence, and
it lists every blocked action with the precise gap instead of silently
promoting it.

Lives in `backend/src/htdt/cad_decision_brief.py` (model + deterministic
builder), `cad_decision_brief_repository.py` (sealed persistence, native
schema **v113** table `cad_decision_briefs`), and `decision_brief_panel.py`
(the 比較 page surface wired into `optimization_workflow_workspace.py`).

## Vocabulary

| Term | Meaning |
|---|---|
| `CadDecisionBrief` | Sealed brief (`dbrief-…`) over one document + one pinned scene revision: baseline ref, optional comparison-set ref, ranked `DecisionAction` tuple, derived `top_tier`/`ready_count`, `ranking_explanation`, provenance, seal `brief_sha256`. |
| `DecisionAction` | One candidate's next step (`dact-…`, content-addressed from candidate ref + label): candidate `AuthorityRef`, changes, per-dimension `DecisionDelta`s, the five `DecisionGate`s, comparability, evidence profile, cost, recommendation, and **derived** `tier`/`disposition`/`gaps`/`rank`. |
| `DecisionGate` | One named evidence gate with an optional `DecisionEvidencePin` (kind/ref_id/ref_sha256 + evidence class + freshness) and observed verdict. A pin without a verdict — or a verdict without a pin — fails validation. |
| Gates | `solver_gate`, `channel_verify`, `deployment`, `campaign`, `production_gate` — always in this declared order. |
| Tiers | `ready` (every gate `verified` + `current` and comparability `comparable`), `conditional` (no gaps but at least one `conditional` verdict or non-current pin), `not_ready` (one or more gaps). `top_tier` is the rank-1 action's tier, `none` when the brief has no actions. |
| Gap codes | `evidence_missing` (no pin), `evidence_stale` (pin declared stale/unknown freshness), `verdict_failed` (observed verdict `failed`), `not_comparable` (incompatible fidelity) — each carries gate + detail so the gap is precise, never a bare flag. |
| Dispositions | `recommended` ⇔ `ready` only; everything else is `requires_verification` (要検証の候補) — matching the issue's rule that a candidate whose production gate is not established may never read as a product recommendation. |
| Recommendation | `kind` (apply_candidate / remeasure / verify_channel / deploy / collect_evidence), `why`, `actor`, `verify_by`. Only `ready` may carry `apply_candidate`; every other tier may only name a verification step — enforced at validation so a blocked action cannot pose as an adoption instruction. |

## Evidence model

- **Fail closed.** A gate with no `DecisionEvidencePin` is the
  `evidence_missing` gap — an absent pin is never an implied pass. Pins are
  caller-declared evidence (the brief seals the declaration; gate verdict
  authorities do not key by variant id so no honest automatic correlation
  exists yet). `save_brief` re-resolves every pinned + identity
  `AuthorityRef` through `ExactAuthorityResolver` before writing; reads
  re-resolve identity refs whose kind the deployment can resolve.
- **Determinism.** `build_decision_brief` is a pure function: actions are
  re-ranked by `(tier order, verified-gate count, improved-dimension count,
  action id)`, counts/top tier/explanation are recomputed, and the seal
  covers all of it — same candidates + same evidence produce a
  byte-identical `brief_sha256`.
- **No score collapse.** Deltas stay per-dimension (`improved` / `regressed`
  / `unchanged` / `unknown` with `measured` / `predicted` / `derived` /
  `unknown` basis); the brief never emits a single composite score. The
  generated `ranking_explanation` names the top two actions and the
  mechanical reason the first outranks the second (tier, verified gates, or
  improved dimensions).
- **Costs are provenance-bound.** `DecisionCost(state='unknown')` forbids
  any amount/provenance; `state='known'` requires a finite amount plus
  currency, source, quoted_on, quantity and assumptions — no fabricated
  figures.
- **Freshness.** `brief_freshness` reports `stale` when the pinned scene
  revision/content hash no longer matches the live head; the panel surfaces
  the stale notice and does not re-present the old conclusions as current.

## Surface

`DecisionBriefPanel` sits at the bottom of the 最適化 workspace's 比較 page —
where the operator lands after running a comparison. It renders the newest
persisted brief (freshness banner, top tier + ready count, ranking
explanation, per-action gate/gap/delta/cost/recommendation lines) and offers
「最新の比較セットから再計算」: it takes the newest saved
`DesignComparisonSet`, picks the alternative pinned to the live scene head
as baseline, and composes + persists a brief. Because gate evidence is
declared-pins only and the panel submits none, a composed brief honestly
lands every non-baseline alternative `not_ready` with `evidence_missing`
gaps until real gate verdicts are wired in — the UI never manufactures a
recommendation the evidence cannot back. All strings are Japanese;
repository/compose failures surface via `operation_error_message` behind
`# error-boundary:` markers.

## Boundaries

- No new top-level workspace, solver, or authority beyond the sealed table.
- The brief does not evaluate alternatives itself; it records and ranks the
  declared evidence chain. Wiring real per-candidate gate verdicts (solver
  gate, channel verify, deployment, campaign, production readiness) into
  pins is follow-up integration work — until then the panel's compose path
  stays fail-closed.
- Lifecycle/verification manifests untouched; the new sealed table is wired
  into `_ROW_BINDINGS`, the authority-audit repository chain + replay probe,
  and the JA lifecycle-table labels.

## Tests

`backend/tests/test_issue_937_decision_brief.py` — deterministic
byte-identical briefs (input order independent), every tier/gate/gap path,
blocked-with-gap honesty, `ready`↔`apply_candidate` exclusivity, forged
tier/seal rejection, unknown-gate rejection, cost-provenance rules, UTC
timestamp strictness, freshness stale/current, repository round-trip, and
tamper detection (column, payload_json, deleted identity ref).
