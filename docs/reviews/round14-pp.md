# Round 14 — Optimization → Pareto → Applied-Candidate Depth

Scope: the optimizer's position search produces candidates, the workspace
builds pareto sets over their objective evaluations, and the user "applies" a
candidate to the working document. Audited end-to-end: candidate geometry ↔
recorded positions, metric honesty, pareto non-domination, constraint
enforcement, determinism, apply diff/undo, provenance, budget honesty, and
empty/degenerate edge cases.

Method: fresh clone of `main`. A real small deterministic optimization was run
end-to-end through the public engine (`build_cad_search_spec` →
`generate_cad_candidates` → `apply_candidate_positions` /
`apply_extended_candidate` → `CadObjectiveRepository` save/read round-trips →
`build_pareto_set`) on a scene with wall-clearance, exclusion-zone and
pair-distance constraints (raw grid 20 → feasible 9). Every claim the engine
makes was recomputed independently in a standalone harness (33 checks): grid
values regenerated with `Decimal`, each candidate's constraint verdict
re-evaluated by hand against its own `positions`, candidate ids rehashed from
`{search_spec_sha256, positions}`, reported counts summed, domination
brute-forced over the metric vectors, apply diffs compared field-by-field, and
undo verified byte-identical. Fixture tests added for each new gate.

## Findings & fixes

| # | Area | Verified failure | Fix |
|---|---|---|---|
| 1 | `apply_candidate_positions` | A fabricated candidate whose `positions` violate the spec's declared constraints (wall clearance, exclusion region, pair distance, room boundary) applied silently — the working document took the illegal placement in one undo step, no flag. `apply_extended_candidate` already re-checked *orientation* constraint rejections but never re-checked the *position* constraints either. `cad_search.py`, `cad_extended_search.py` | New `require_candidate_position_feasibility(document, current_constraint_set, spec, positions)` re-compiles the spec's declared placement authority (`_constraint_engine_spec` — constraint set + axes' entity ids + linked-variable membership) and replays `evaluate_constraint_set` over the candidate's `positions`; infeasible → `ValueError('candidate does not satisfy the SearchSpec placement constraints')`. Called from both apply paths. |
| 2 | `apply_candidate_positions` | Unlike `apply_extended_candidate` (which recomputes `ec-` from positions + orientation before touching the document), the base apply never re-verified `candidate_id` — a candidate whose `positions` payload did not hash to its claimed `pc-` id applied anyway, so provenance ("which spec produced this exact geometry") was forgeable. `cad_search.py` | `apply_candidate_positions` now recomputes `'pc-' + canonical_sha256({search_spec_sha256, positions})[:20]` and raises `ValueError('candidate identity does not match its declared positions')` on mismatch — same asymmetric-protection fix the extended path already had. |
| 3 | `build_pareto_set` / `_require_pareto_authority` | Pareto binding checked `scene_revision_id`, `search_spec_id`/`search_spec_sha256` and metric-set uniformity, but not `evaluation_spec_sha256`: two evaluations under different objective recipes (e.g. `candidate_movement` with different weights, or a different target curve/band) hash to different `evaluation_spec_sha256` yet could be combined into one pareto set. For legacy synthesized definitions (`definition_id` derived from objective_id+unit+direction only) the spec parameters are invisible to `definition_id` matching, so `refresh_pareto_comparison`'s per-candidate objective-id check could not catch it — dominated-in-one-recipe members presented as optimal-in-another. `cad_objectives.py`, `cad_objective_repository.py` | `build_pareto_set` now requires a uniform `evaluation_spec_sha256` across all bound evaluations (`'Pareto evaluations must share one SceneRevision, SearchSpec and evaluation spec'`); `_require_pareto_authority` re-verifies the same on every repository read of a persisted set (`'Pareto objective evaluation spec mismatch'`), so sets persisted under the old permissive rule fail closed rather than serving stale results. |

## Verified honest (no change needed)

- **Candidate geometry ↔ positions**: every generated `CadCandidate.positions`
  entry re-evaluated against the compiled `PlacementEvaluationRequest`
  verbatim — feasibility verdicts match the engine's membership decision for
  all 20 raw combinations; the 9 feasible members are exactly the 9 my harness
  computed.
- **Recorded position == encoded geometry**: `candidate_preview_document` +
  `extended_candidate_preview_document` rebuild the scene from `positions`
  only; diffing the preview against the applied working document post-apply
  shows byte-identical `entities` (position + aim/body orientation) — the
  applied scene *is* the candidate's claimed geometry.
- **Objective/direction honesty**: metric vectors store `ObjectiveMetric`s
  with explicit `definition_id`/`unit`/`direction`/`valid_domain`; pareto
  binding rejects direction-mismatched vectors and the workspace rejects
  per-candidate metric-id or unit divergence before display. Labels shown in
  the tree (`非劣`/`支配あり`) come straight from `result.non_dominated_candidate_ids`.
- **Pareto truth**: for every saved pareto set, `_require_pareto_authority`
  re-loads the referenced evaluations, re-checks each evaluation's own
  authority (vector recomputed from `evaluation_spec` + resolved evidence,
  membership re-enumerated against the candidate set), then recomputes
  `pareto_front` and requires equality — verified by brute-forcing domination
  over the same vectors independently (identical non-dominated set), plus a
  mixed-direction vector check. A persisted set whose refs are swapped for a
  different evaluation is rejected on read.
- **Constraint enforcement**: `generate_search_space` evaluates every raw
  combination under the engine spec — `feasible_candidate_count`,
  `rejected_count`, and per-constraint `rejection_counts` all sum correctly to
  `raw_candidate_count`; rejected candidates are absent from the candidate set
  (not flagged-and-included). `candidate_limit` over-commitment is refused at
  spec build.
- **Determinism**: two `generate_cad_candidates` runs on the same spec produce
  identical `candidate_set_sha256` and identical ordered candidate ids; grid
  values are `Decimal`-exact (no float drift — 0.2-step axes produce exactly
  the declared bounds); paging is stable and `all_candidates` == concatenated
  pages.
- **Apply diff/undo**: applying candidate C diffs the working document as
  exactly `changed_entity_ids` — verified entity-by-entity (only the moved
  entity's position/aim fields changed; room, sizes, other entities untouched);
  undo restores the pre-apply document hash exactly.
- **Provenance**: candidate id binds `{search_spec_sha256, positions}` (pc-) /
  `{extended_search_sha256, base_candidate_id, positions, aim_yaw_deg, …}`
  (ec-); spec sha binds scene revision + constraint set + axes + linked
  variables; `require_search_spec_authority` re-derives the spec byte-exact on
  every read so a re-search under "same conditions" compares honestly.
- **Budget honesty**: `raw_candidate_count`/`feasible_candidate_count`/
  `rejected_count`/`duplicate_count` are the real counts computed during
  enumeration, not derived; `extended` search carries `base_candidate_count`
  and `base_candidate_set_sha256` so the layer-over-base claim is checkable.
- **Edge cases**: zero-feasible spec → honest empty page with counts
  (`feasible=0`, no fabricated members); single-feasible spec → single
  candidate, apply/undo clean; duplicate positions collapse under dedupe with
  `duplicate_count` incremented (no fabricated diversity); a baseline scene
  violating its own constraints makes every candidate honestly infeasible.
- **Objective evaluation honesty**: `latest_evaluations_by_candidate` picks
  last-in-seq per candidate; a saved evaluation re-derives its vector from the
  resolved input refs on every read — tampering with the stored metrics raises
  rather than serving stale numbers.
- **Linked variables**: mirror-linked slave positions are re-derived from the
  master value at both enumeration and apply-verification time (verified with
  a `FR.x = 6 − FL.x` mirror), so a candidate's claimed slave position is the
  formula's output, not a free parameter.

## Tests

- `backend/tests/test_cad_search.py` — `test_candidate_apply_rejects_violating_positions_and_identity_mismatch`: forged-but-self-consistent pc- id over exclusion-violating positions refused; real candidate with mismatched id refused; working document untouched and `history_length == 0` in both cases.
- `backend/tests/test_cad_extended_search.py` — `test_extended_apply_rejects_positions_violating_placement_constraints`: fabricated ec- id recomputed over room-boundary-violating positions refused.
- `backend/tests/test_cad_objective_repository.py` — `test_pareto_set_rejects_mixed_evaluation_specs`: `build_pareto_set` refuses mixed-`evaluation_spec_sha256` inputs, and `save_pareto_set` rejects a hand-constructed set whose refs point at evaluations under different specs (legacy data fails closed on read).
- Full `backend/tests` suite re-run under `QT_QPA_PLATFORM=offscreen -n 4`: see PR body for the result.
