# #810 — Uncertainty-aware acoustic validation (REV63)

Issue #810 retires the idea that one broadband holdout RMS threshold can mean
"physically valid". `max_holdout_rms_db` remains a useful software gate inside
`CadModelValidationRecord`, but validation now has an uncertainty-aware
authority: residuals are judged against declared uncertainty on **both** sides
of the comparison, per observable and per frequency band, under a
preregistered, versioned protocol.

Basis: ASME V&V 20 (comparison error vs. combined uncertainty), Thydal et al.
2021 VUQ (input uncertainty vs. model-form discrepancy stay separate),
JCGM 100/101 (combined and expanded uncertainty; `±x` is a bound, never a
sigma), ISO 3382-1/-2 (per-observable, per-band measurement practice).

## Scope

New sealed authority `cad_validation_uncertainty.py` with append-only
repository `cad_validation_uncertainty_repository.py`, additive on top of the
O60 record — a `CadModelValidationRecord` is bound as *evidence* (its scalar
gate is snapshotted onto the verdict), never replaced.

| Table | Record | Prefix | Role |
|---|---|---|---|
| `cad_validation_uncertainty_protocols` | `ValidationUncertaintyProtocol` | `vup-` | Preregistered, versioned decision authority |
| `cad_observable_uncertainty_evaluations` | `ObservableUncertaintyEvaluation` | `uoe-` | One candidate × one observable × one split, per-band residuals vs both sides' uncertainty |
| `cad_uncertainty_validation_verdicts` | `UncertaintyValidationVerdict` | `uvv-` | Whole-study verdict: absolute + ranking + calibration axes |

Schema: `NATIVE_SCHEMA_VERSION` 84 → 85 (`_migrate_84_to_85` creates the three
tables via the idempotent baseline).

## Verdict vocabulary (§5)

Per-band (`BandUncertaintyVerdict`):

| Verdict | Meaning |
|---|---|
| `consistent_within_uncertainty` | residual inside the combined gate, evidence resolves it |
| `discrepancy_significant` | residual exceeds the declared significance ratio × combined u |
| `below_evidence_resolution` | residual smaller than the resolution fraction — evidence cannot distinguish agreement |
| `evidence_too_uncertain` | combined u exceeds the protocol's tolerable comparison uncertainty |
| `insufficient_uncertainty_information` | a side has no usable statistic, or its semantics fall below the protocol minimum, or the band lies outside declared validity/spectral coverage |

Per-observable / study (`ObservableUncertaintyVerdict`):

| Verdict | Meaning |
|---|---|
| `consistent_with_reference_within_uncertainty` | every declared band consistent, full applicability coverage |
| `discrepancy_significant` | significant excess, and the model's own declared model-form envelope covers it — the discrepancy is recorded, not hidden |
| `model_form_discrepancy_required` | significant excess NOT covered by any declared model-form envelope — new model-form evidence is required |
| `reference_too_uncertain` | comparison evidence too uncertain; measurement category dominates |
| `input_uncertainty_dominates` | too uncertain; model-input category dominates |
| `numerical_uncertainty_dominates` | too uncertain; numerical category dominates |
| `band_coverage_incomplete` | declared bands excluded/unmeasured, or applicability range not covered — a coverage claim, never agreement |
| `insufficient_evidence` | zero evaluated bands or any insufficient-uncertainty band |

Ranking is a separate axis (`ranking_supported` / `ranking_contradicted` /
`ranking_not_evaluated`), derived from the bound legacy record's
trend/separation gates — direction accuracy can coexist with absolute bias.
Calibration is reported separately (`calibration_consistent` /
`calibration_discrepancy` / `no_calibration_evidence`) and can never migrate
into the holdout judgment.

No verdict here means "production validated" — recommendation authority stays
upstream; this layer deliberately does not weaken the O60 gates.

## Evidence model

- **`ValidationUncertaintyProtocol`** — sealed before evaluation: per-observable
  `quantity`/`unit`/`domain`/`comparison_metric`, applicability band, minimum
  uncertainty semantics, and the decision rule (`significance_ratio`,
  `resolution_fraction`, `max_tolerable_comparison_uncertainty`). Relaxed rules
  arrive only as a new `protocol_version` via `supersedes_protocol_ref`.
  `correlation_policy` (`independent_unless_declared`/`declared_correlated`)
  and `bounded_input_policy` are sealed here too.
- **`UncertaintySideEvidence`** — a bound snapshot of upstream authority:
  `measurement_uncertainty_budget` (#572 `mub:` ref), `uncertainty_budget_result`
  (#979), `propagated_interval` (#604 — always `combined_bound` semantics),
  `declared_bound`, or honest `none`. `none` semantics carry no quantified
  values; a declared bound is never silently a sigma.
- **`BandUncertaintyEvaluation`** — inputs (`band_hz`, `coverage`, residual,
  exclusion reason) only; every derived field (per-side u, combined u,
  semantics, ratio, verdict) is recomputed by the sealed validator — a stored
  verdict cannot drift from its evidence without breaking the seal.
- **`UncertaintyValidationVerdict`** — `EvaluationBinding` snapshots pin each
  bound evaluation (ref+sha+snapshot fields); the repository re-checks each
  snapshot against the stored row at write time.

## Integration points

- `native_authority_audit.py`: `validation_uncertainty` branch in
  `_RepositoryChain`, three `_ReplayProbe`s re-read every row canonically.
- `native_row_integrity.py`: `_ROW_BINDINGS` entries for all three tables.
- `application_pages.py`: JA lifecycle table labels.
- `measurement_evidence_display.py`: `uncertainty_validation_line` /
  `uncertainty_band_line` JA verdict helpers over `VUQ_LABELS`.
- Legacy `CadModelValidationRecord` binds via `validation_ref`; its
  `residual_gate`, `holdout_rms_db`, `max_holdout_rms_db`, trend and separation
  gates are snapshotted onto the verdict — the scalar gate is evidence, and can
  never alone produce `consistent_with_reference_within_uncertainty`.
- Owned-room verdicts require the same campaign + durable registration
  authority the O60 record requires; synthetic verdicts cannot claim either.

## Non-goals held

- No universal "acceptable dB" number is introduced; thresholds are ratios of
  declared uncertainty, per observable.
- Model discrepancy is never folded into measurement noise; `dominant_
  uncertainty_category` reports which #979 category dominates.
- JND is perceptual annotation only (`PerceptualInterpretation.role` is pinned
  to `perceptual_interpretation_only`); it never feeds a verdict.
