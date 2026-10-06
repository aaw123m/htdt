# REV61-COMPUTE — solver / prediction / optimization numerics review

Scope: `origin/main` @ `3695e6f8`. Audited the compute/numerics slice of
`backend/src/htdt/` — prioritizing verdict/evaluation paths in modules
touched by REV57-60 (`cad_spatial_campaign`, `cad_adaptive_identification`,
`cad_bass_management_qualification`, `cad_correction_*`,
`cad_response_target`, `acoustic_metric_applicability`,
`acoustic_validation_envelope`, `cad_spatial_image_authority`,
`cad_isolation_authority`, `cad_modal_decay_view`,
`cad_coverage_aim_authority`, `cad_decay_processing`, `cad_noise_ingress`,
`cad_spectral_*`, `cad_field_interpolation`, PFFDTD/R130 grid plumbing,
`cad_geometric_acoustics_adapter` math primitives). Regression tests live
in `backend/tests/test_rev61_compute.py` — every FIXED verdict below was
proven red on the unfixed tree via `git stash`.

## Verdict table

| # | Finding | Severity | Verdict |
|---|---------|----------|---------|
| 1 | `compute_coverage_metrics` (`cad_spatial_campaign.py`) divided by `total_spatial` for the density share — a design whose points are all non-spatial roles (repeatability/diagnostic-only) crashed with `ZeroDivisionError`, so `evaluate_campaign_design`'s intended `no_spatial_coverage_points` fail-closed verdict was unreachable. Probe: repeatability+diagnostic-only design raised at the share comprehension | HIGH — crash masks fail-closed path | FIXED — `density_share` emits 0.0 per zone when `total_spatial == 0`; `test_cmp61_coverage_metrics_without_spatial_points` |
| 2 | Same function fabricate `nearest_neighbor_m = {point: 0.0}` for a lone spatial point (`elif len(ids) == 1`), so `min_spacing_m = 0.0` produced a spurious `points_below_declared_spacing` warning whenever a declared minimum was set. A single point has no pair — spacing is undefined, not zero | MED — fabricated advisory on a valid design | FIXED — lone-point fallback removed; spacing stats stay `None`; `test_cmp61_single_point_has_no_spacing_artifact` |
| 3 | `instantiate_campaign_template` placed +Y-side outside-area points at `mins.y + spacing` instead of `maxs.y + spacing`: any zone with y-extent ≥ `spacing_m` swallowed `p-out-01`-side points back inside the declared area, silently violating the "deliberately just-outside" template contract (metrics stayed honest — the points simply never were outside) | MED — declared probe geometry silently wrong | FIXED — `y_m = (mins.y if side < 0 else maxs.y) + side * spacing`; `test_cmp61_template_outside_points_land_outside` |
| 4 | `evaluate_adaptive_claim` (`cad_adaptive_identification.py`) did not include `partially_converged` in the not-converged gate — a partially converged estimate fell through to `qualified_adaptive_estimate`, contradicting the module's own "unconverged is not evidence" rule | HIGH — fail-open qualification gate | FIXED — `partially_converged` added to the gate; `test_aai61_partially_converged_is_not_qualified` |
| 5 | Same function: `stimulus_band_insufficient` defect state was not gated — a stimulus declared to lack energy in part of the claimed band could still yield `qualified_adaptive_estimate` whenever the estimate's excitation_support claimed `identified` | MED — declared defect ignored | FIXED — maps to `band_limited_estimate` (the honest partial-evidence state); `test_aai61_band_insufficient_stimulus_caps_the_claim` |
| 6 | `evaluate_splice` (`cad_bass_management_qualification.py`) evaluated margins and the predicted-vs-measured RMS error at every summed-curve sample inside the crossover band — but `ResponseCurve.magnitude_at`/`np.interp` flat-clamp outside each isolated curve's measured range, so samples beyond the shared domain compared the summed measurement against a fabricated endpoint level (spurious cancellations / device_state_mismatch, or hidden real ones) | MED — fabricated evidence at domain edges | FIXED — band indices restricted to `[max(main lo, sub lo), min(main hi, sub hi)]`; no shared-domain sample → `unknown`; `test_bmq61_splice_*` |
| 7 | `evaluate_isolation_qualification` (`cad_isolation_authority.py`) evaluated every criterion kind with the SPL-cap margin `limit − estimated_receiving_spl`. For `relative_reduction_target` the limit is a required level difference — the SPL margin compares two different quantities and produces meaningless pass/fail | HIGH — wrong quantity in verdict | FIXED — reduction targets now compare `level_difference_db − limit_db` over measured difference-metric bands; `test_iso61_relative_reduction_target_*` |
| 8 | Same function: `level_difference_db` on the band verdict was populated from any measured band regardless of metric — a `receiving_spl` band's absolute level appeared under a "level difference" name, and a directly measured receiving SPL never fed `estimated_receiving_spl_db` (usable only via source-minus-difference), so the strongest possible SPL evidence yielded criterion verdicts of `unknown` | MED — mislabeled quantity + strongest evidence unusable | FIXED — `level_difference_db` restricted to difference metrics (dnt/dn/d/R′/R); `receiving_spl` bands populate `estimated_receiving_spl_db` directly; `test_iso61_receiving_spl_metric_feeds_spl_estimate` |
| 9 | `_normalization_offset` `band_average` (`cad_response_target.py`) averaged the response over ALL in-band response points but the target over only the points where interpolation succeeded — asymmetric sample sets bias the offset when target coverage is partial (e.g. −56 vs −60 dB means → 4 dB of fabricated offset) | MED — biased normalization | FIXED — paired samples only; `test_rt61_band_average_pairs_sample_sets` |

## Verified-clean surfaces

| Surface | How verified |
|---------|--------------|
| `cad_spatial_campaign`: `_pearson` degenerate-guard, `skipped_pairs` honest counting, `evaluate_placements` binding checks, partition disjointness, `_lattice_positions` interior walk, `_distribute` seen/reserved dedup, per-(point,viewpoint,quantity) coverage keying | Code review + probes; `CadSpatialMeasurementSet` validator enforces observation-key uniqueness so `len(obs_list) == len(plan.points)` cannot be inflated by duplicates |
| `cad_adaptive_identification`: `_seal` content-derived ids, profile license gates, `file_segment` asset+segment requirements, `generated_noise` seed requirement, `system_changed` requires stationarity_ref, TDN≠THD residual guard, remaining claim-ladder states | Code review + `test_rev59_audiometb.py` ladder re-run |
| `cad_correction_qualification` (1765 lines): identity/usable_band/headroom/pre_ringing/control_fidelity/holdout_independence/closed_loop/observables/state-scope gates all fail-closed; `_residual_db` `(target_db or 0.0)` unreachable-ish since `_interpolate_db` flat-clamps | Code review of every gate branch |
| `cad_correction_design_policy`: `_interpolate_db` declared flat-clamp convention, `_fractional_octave_smooth` bit-identity-conscious `searchsorted` fold order, `_aggregate_band` all methods, `insufficient_evidence` below `min_positions` | Code review |
| `acoustic_metric_applicability`: Schroeder coefficient 2000, modal-overlap limit 1.0 < applicability threshold 3.0 correctly ordered, Morse & Bolt density/overlap formulas, fail-closed context gates | Code review |
| `acoustic_validation_envelope`: p95 nearest-rank statistic, `insufficient` fixture gate when no threshold rule, hybrid-boundary gate precedence, envelope `covers()` exact solver-version/observable/range matching | Code review |
| `cad_spatial_image_authority`: set-level uniqueness on (point,viewpoint,quantity) blocks count inflation; uniformity coverage ladder + state-order min-aggregation + stabilization block | Code review |
| `cad_modal_decay_view`: transform-kind required-parameter matrix, ridge-width ≥ resolution guard, single-exp forbidden under overlap, honest qualification ladder | Code review |
| `cad_coverage_aim_authority`: evaluation ladder binding/quantity/early-window ValueErrors, tri-state `measured_agree`, demotion precedence | Code review |
| `cad_isolation_authority` (rest of): LF-domain classification (`below_validated_domain` never extrapolated), unmodelled-path demotion of `modelled` bands while `measured` keeps measured truth, lifecycle demotion (`claimed_measurement_not_bound`, `qualification_requires_all_criteria`), sealed verdicts | Code review + `test_rev56_building.py` re-run |
| `evaluate_splice` `delay_cancels` (half-period ±25% mod period) and `combined_load_db` coherent worst-case sum | Code review + `test_rev56_bassstim.py` re-run |
| PFFDTD `_planned_pffdtd_grid` (spacing = c/(fmax·ppw), symmetric 3.5-cell margins), polyhedral containment gates, signed-volume orientation check | Code review |

## Residual notes (not defects, recorded for the parent)

- `PlacementAssessment.unbound_measurement_ids` carries the ids of
  *bound* measurements (the assessment's measurement set), not unbound
  ones — field-name/semantics mismatch worth a rename in a schema pass.
- `evaluate_splice` resolves a seat's role from the first evidence record
  seen; disagreeing records keep first-match. Acceptable (roles are
  declared per seat) but noted.
- `coverage_qualification` `limiting_band_hz` is declared but never
  populated anywhere — dead output field.
- `noise_margin_db < 0` (vs `<= 0`) in decay processing treats a margin
  of exactly 0 as acceptable — defensible at floating-point equality.
- `evaluate_placements` order-divergence loop checks indices only up to
  `len(declared)` — extra captured measurements beyond the declared
  sequence are ignored by that check.
- Hybrid-boundary `consistent` state can emit when one of
  transition/timing is unevaluated — the joined gates are honest overall.
- `level_difference_db` restricted to real difference metrics in fix #8:
  `receiving_spl`/`other_declared` band values now appear only as
  `estimated_receiving_spl_db` (for `receiving_spl`) or nowhere
  (`other_declared`) — previously the raw value was mislabeled rather
  than lost.
- REV54-60 already covered much of the older surface; I verified the
  gates listed above rather than re-reading every helper line — the
  8 222-line `cad_geometric_acoustics_adapter` was spot-checked on its
  math primitives, interpolation, containment and ray helpers, not
  fully re-derived.

## Scoped pytest

```bash
TMPDIR=/c/t PYTHONIOENCODING=utf-8 QT_QPA_PLATFORM=offscreen \
  /c/devin/python/python.exe -m pytest \
  backend/tests/test_rev61_compute.py \
  backend/tests/test_rev56_campprofile.py \
  backend/tests/test_rev59_audiometb.py \
  backend/tests/test_rev56_bassstim.py \
  backend/tests/test_rev56_building.py \
  backend/tests/test_rev56_targets.py \
  -q --basetemp=C:/t/compute-scoped
# 203 passed — all green on the fixed tree; every new test red on unfixed.
```
