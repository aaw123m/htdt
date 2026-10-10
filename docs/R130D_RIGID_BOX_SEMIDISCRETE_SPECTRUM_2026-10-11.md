# R130D rigid-box semidiscrete spectrum — independent physics validation — 2026-10-11

Preregistered plan: `benchmarks/acoustics/r130d_rigid_box_semidiscrete_spectrum_plan_2026-10-11.json` (pushed in `dd85ba3d` before any eigencomputation). Evidence: `benchmarks/acoustics/r130d_rigid_box_semidiscrete_spectrum_evidence_2026-10-11.json` via `scripts/run_r130d_rigid_box_semidiscrete_spectrum.py`. **0 time-domain solver runs.**

## Why this axis existed

The reproduction-isolation `PHYSICALLY_VALIDATED` check `analytic_rigid_box_modal_spectrum` was UNKNOWN: record-based peak detection resolved only 3–4 of 12 analytic clusters. A 250 ms record has ~4 Hz frequency resolution and degenerate box modes cluster inside it; source/receiver nodal planes kill others. Peak-detection resolution is not operator error — so the independent check compares the discrete Neumann-operator spectrum itself against physics.

## Method

Seven-point cell-centered Neumann Laplacian (same stencil convention as the canonical solver) on the rigid box 2.5×2.0×1.6 m at h=0.1 (25×20×16) and h=0.05 (50×40×32); `eigsh` lowest modes vs (a) the closed-form discrete Neumann eigenvalues, (b) the 18 analytic rigid-box modes below ~214 Hz, (c) a declared per-mode seven-point dispersion bound, and (d) coverage of every mode the record-peak detector left unresolved.

## Result — PHYSICS_VALIDATED_SEMIDISCRETE_SPECTRUM

| h | discrete-exactness max rel | continuum max rel | dispersion bound | unresolved coverage |
|---|---|---|---|---|
| 0.10 | 2.7e-15 | 0.64% | all modes within | 18/18 |
| 0.05 | 4.0e-14 | 0.16% | all modes within | 18/18 |

- `OPERATOR_DISCRETIZATION_CONFIRMED` — the discrete operator reproduces its closed-form spectrum to solver precision; continuum error scales ~4× under h/2 refinement (O(h²) dispersion, seven-point theory).
- `RECORD_PEAK_DETECTION_LIMIT_CONFIRMED` — every mode the impulse-record peak detector missed is present in the discrete spectrum; the UNKNOWN record-based check was a detection-resolution limitation, not solver error.

## Authority statement

This validates the discrete spatial operator's spectrum against independent physics. The canonical original impulse contract remains `SELF_CONVERGENCE_FAILED`; the band-limited requalified contract's convergence is now independently anchored: the operator's low-mode spectrum is physics-correct, so the ≤200 Hz convergent transfer is physically meaningful, not a numerical coincidence. Product remains `NO_GO` pending the product gate.
