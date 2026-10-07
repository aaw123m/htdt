# #809 R130/R150 external benchmark qualification — authority layer

Issue #809 is a planning issue: "Implementation changes: none." The
implementation-side slice it requires — the authority that keeps the
benchmark program honest — landed here on top of the existing
`cad_benchmark` import/evaluation harness.

## What exists already (`cad_benchmark`)

- `BenchmarkSourceAsset`: dataset name/version, origin URI, content hash,
  licence/admission ref, evidence class (`external_simulated` can never be
  promoted to measured) — satisfies the §2 dataset-authority fields.
- `BenchmarkCase`: normalized case; fields the corpus doesn't supply stay
  `None` — no invented input authority.
- `run_benchmark` → `BenchmarkValidationEvidence`: per-observable results
  with band/window/metric/tolerance identity; never an aggregate score.

## What this change adds (`cad_benchmark_qualification`)

Three sealed records + one fail-closed evaluator:

- `BenchmarkSceneMapping` — corpus scene (pinned by `AuthorityRef` to the
  admitted asset) → phenomenon + `solver_path` (`wave_r130` /
  `geometric_r150` / `hybrid`) + declared observables + applicability
  domain (band, boundary-model family, `curvature_class` — the RS8
  curved-surface lane stays separate from planar).
- `BenchmarkPreregistration` — frozen run spec created before comparison:
  case hash, provider id/version/config hash, evaluation profile,
  declared observables, planned convergence axes. `run_mode` is
  `preregistered_unfitted` or `informed_calibrated`; informed runs must
  link the prior unfitted evidence and name every tuned parameter —
  Aspöck's uninformed/informed distinction is structural, not a label.
- `evaluate_qualification` — frozen-configuration violations raise
  (evidence identity must equal the preregistration); verdicts follow
  §8: `PASS_WITHIN_DOMAIN`, `FAIL`, `INSUFFICIENT_REFERENCE_QUALITY`,
  `INSUFFICIENT_INPUT_AUTHORITY`, `NUMERICAL_NONCONVERGENCE`,
  `OUTSIDE_APPLICABILITY`, `UNSUPPORTED_OBSERVABLE`. Convergence evidence
  is required independently of experimental match; missing planned axes
  block PASS.
- `BenchmarkQualification` — sealed verdict. `level_attained` caps at
  `EXTERNAL_BENCHMARK_VALIDATED` (requires `external_measured` evidence);
  analytic/independent-numerical corpora cap at `NUMERICALLY_VERIFIED`;
  `OWNED_ROOM_VALIDATED` and `PRODUCTION_RECOMMENDATION_ELIGIBLE` are
  unrepresentable here — the §6 capability-vs-validation ladder stays
  honest and the owned-room gate (#801) stays separate. `predictive`
  marks unfitted runs so #801 can demand blind evidence.

## Acceptance-criteria coverage

| Criterion | Where |
|---|---|
| analytical vs measured lanes separate | `BenchmarkEvidenceClass` + level cap |
| corpus admitted by version/DOI/hash | `BenchmarkSourceAsset` + pinned `asset_ref` |
| RS1–RS7 mapped to phenomena | `BenchmarkSceneMapping` |
| RS8 curved-surface lane | `curvature_class` |
| unfitted vs informed distinguishable | `run_mode` + `predictive` + unfitted-link enforcement |
| solver/config frozen and retained | preregistration fields + `_check_frozen_configuration` |
| reference quality/limitations retained | asset limitations + `INSUFFICIENT_REFERENCE_QUALITY` |
| no single hidden score | per-observable `observable_statuses` |
| convergence independent of match | `NUMERICAL_NONCONVERGENCE` gate |
| capability != validation | `QualificationLevel` ladder + model validator |
| verdicts are band/domain limited | `verdict_band_hz` ⊆ `applicability_band_hz` |
| feeds corpus/envelope without second truth | sealed rows replayable via `_ReplayProbe` audit |

## Remaining (non-software)

- Fetch + admit BRAS v3 (DOI 10.14279/depositonce-6726.3) through the
  ledger and write the canonical-JSON importer for its geometry/material
  assets — requires dataset download + licence check.
- Actual R130/R150 solver `PredictionProvider` bindings and the first
  preregistered runs.
- RS8 (2026) dataset admission once released per DOI
  10.14279/depositonce-25649.

Refs #809. Does not close the issue — the program still needs the dataset
work above, and #809 alone never enables production adoption (#801 gate).
