# R100B MFEM GL2 exact-half-step experiment — 2026-09-20

## Scope

Issue #101 / R100B next numerical experiment after PR #281.

Task-start main: `1fb07fa210aae55d91c7d86b3f4c5ee993be7c93`.

The only numerical variable changed from PR #281 is the internal GL2 step construction:

```text
output interval Δt
  -> GL2 step Δt/2
  -> GL2 step Δt/2
  -> output sample
```

`substeps_per_output_interval = 2` was fixed before execution. Adaptive stepping and rate-specific hidden heuristics are not used.

## Frozen authority

The experiment preserves the PR #281 authority:

- exact p2 / h1 MFEM semidiscrete spatial system;
- same source and receiver;
- same source kick using the output interval: `phi_t(0+)=c^2*Δt*M^-1*b*q[0]`;
- same finite-record `P_T / Q_T` observable and half-open 2 s record;
- output rates 6000 / 9000 / 12000 Hz;
- same full-basis modal reference;
- same normalization and Fourier/phasor convention;
- scored bins 20..300 Hz at 1 Hz spacing;
- magnitude / phase null masks -60 / -40 dB;
- tolerances 0.75 dB / 0.05 relative / 8 deg;
- same adjacent-rate, modal-reference, and resource decision structure.

No tolerance fitting, record shortening, comparison-bin exclusion, bad-mode removal, normalization substitution, or result-driven retry was performed.

Authority identities:

- plan SHA-256: `969eaedae041d1c523246e9013b5a34c9b57d934919bbe5f03d2daaa577c6632`
- spatial configuration: `908005a1f9b7aa431d7b20e9ccdb84ae58cbc77e0ab6f0f36d20fc9d52f9de49`
- semidiscrete numerical identity: `f48eb9a7fc5881fd8d2f26b32fc1f20df0f71290ecc4ed0337087073555cc61d`
- integrator configuration: `bf17c1faac30b4f8ccc37705f627bef640581f609df1bd6300601af65874d004`
- output-grid configuration: `4437d61b18add95c918ac7a08bd1c02bb14770189023d664b4239a807064d8e4`

The semidiscrete identity is unchanged from PR #281 / PR #277.

## Implementation

The production-candidate path remains sparse fourth-order 2-stage Gauss-Legendre / [2/2] Padé propagation. For each output rate, the Padé denominator is built at `internal_step = output_interval / 2`, factored once, and reused for both half-steps and all subsequent output intervals.

The output sampling grid is unchanged. With a half-open record, an attempt with `N` output samples performs exactly `2 * (N - 1)` internal GL2 steps.

Explicit step authority:

| output rate | output interval | internal GL2 step | output samples | internal steps |
|---:|---:|---:|---:|---:|
| 6000 Hz | 1/6000 s | 1/12000 s | 12000 | 23998 |
| 9000 Hz | 1/9000 s | 1/18000 s | 18000 | 35998 |
| 12000 Hz | 1/12000 s | 1/24000 s | 24000 | 47998 |

The artifact records the output interval, `substeps_per_output_interval=2`, internal step size, internal step count, system identity, pressure-record hash, factorization identity, residuals, and resource observations for every attempt.

## Reproducible command

The authoritative workflow invoked:

```text
python scripts/run_r100b_mfem_transient_experiment.py --manifest benchmarks/acoustics/r100a_manifest.json --candidates benchmarks/acoustics/r100b_candidates.json --plan benchmarks/acoustics/r100b_mfem_transient_experiment_plan.json --modal-plan benchmarks/acoustics/r100b_mfem_modal_experiment_plan.json --modal-evidence benchmarks/acoustics/evidence/r100b_mfem_modal_2026-09-20.json --mfem-root $MFEM_ROOT --executable $MFEM_PROBE_EXE --work-dir artifacts/r100b/mfem_transient/raw --output artifacts/r100b/mfem_transient/report.json --native-build-s $MFEM_BUILD_S
```

MFEM remains pinned to `d964264cdb9a13e94a201b6c236c7721e0c8765f`.

## Actual numerical result

Authoritative execution:

- implementation head: `4bbfd0554dbc399853cd72919c00895f364172fc`
- Actions checkout merge SHA: `223a8318ab4db85161b6608854a1d2c3be2ecb33`
- workflow run: `35510597729`
- workflow conclusion: **success**
- artifact id: `10605416313`
- artifact digest: `sha256:dd039ca4c547897659cb3a0d03dad629bdea80982f73ea91877c83536af89425`
- deterministic decision identity: `858fb334f00f28f2ea21a7f71a1d36e0b471f5d49f0f8c13a18b869e204c7d89`
- repository evidence summary: `benchmarks/acoustics/evidence/r100b_mfem_gl2_halfstep_2026-09-20.json`

Adjacent-rate comparison:

| pair | complex RMS relative | max magnitude dB | max relative magnitude | max phase |
|---|---:|---:|---:|---:|
| 6000 -> 9000 | 0.0049760290 | 1.14526435 | 0.12353055 | 7.24231828 deg |
| 9000 -> 12000 | 0.00211093735 | 0.48904053 | 0.05474720 | 2.66296419 deg |

Complex RMS decreases strictly, so **numerical convergence = PASS**.

For the final 9000 -> 12000 comparison:

- magnitude dB: `0.48904053 <= 0.75` — PASS
- relative magnitude: `0.05474720 > 0.05` — FAIL
- phase: `2.66296419 <= 8` — PASS

Therefore **current R100A tolerance = FAIL**. The failure is retained exactly; the tolerance was not modified.

Same-rate candidate vs diagnostic full-basis modal reference:

| rate | complex RMS relative | max magnitude dB | max relative magnitude | max phase |
|---:|---:|---:|---:|---:|
| 6000 | 0.00213294110 | 0.30575473 | 0.03458895 | 3.33031884 deg |
| 9000 | 0.000456723447 | 0.07129743 | 0.00817482 | 0.63090029 deg |
| 12000 | 0.000149137996 | 0.02340633 | 0.00269113 | 0.19326552 deg |

All three rates are inside the unchanged tolerances, so **modal-reference agreement = PASS**.

## Resource behavior

The sparse execution contract remains satisfied:

- sparse M/K only; no candidate-path eigendecomposition or dense inverse;
- one sparse mass factorization reused for source kicks;
- one 1050 x 1050 Padé denominator factorization per output rate, reused for every internal half-step;
- denominator nnz: 93,636 for each rate;
- LU nnz: 318,771 / 318,650 / 318,540;
- factor storage: approximately 3.66 MiB per attempt;
- maximum checked internal-step relative residual: `8.07e-15`, below `1e-10`;
- attempt peak RSS observations: approximately 96.6 / 101.6 / 103.4 MiB;
- final reporting-process RSS: 95.68 MiB;
- experiment work disk: 17.30 MiB;
- native MFEM build: 965.94 s;
- total transient factorization: 0.02976 s;
- total transient stepping: 35.12919 s.

PR #281 recorded 17.98990 s total transient stepping for one GL2 step per output interval. The exact-two-half-step experiment therefore used about **1.953x** the stepping time (+17.13929 s), consistent with doubling the internal step count while retaining factorization reuse.

All predeclared resource ceilings are satisfied, so **execution suitability = PASS**. This does not imply candidate-wide production adoption.

## Decision

The independent gates are:

- workflow execution: **PASS**
- numerical convergence: **PASS**
- modal-reference agreement: **PASS**
- current R100A tolerance: **FAIL**
- execution suitability: **PASS**
- overall experiment: **FAIL**
- production adoption: **NO_GO**

The half-step construction materially improves temporal agreement: compared with PR #281, the 9000 -> 12000 comparison changes from 0.972198 dB / 0.105892 relative / 9.03055 deg to 0.489041 dB / 0.0547472 relative / 2.66296 deg, and the all-rate modal-reference gate changes from FAIL to PASS. It still misses the frozen relative-magnitude tolerance by approximately 0.0047472, so this experiment does not qualify the candidate or justify tolerance adjustment.

## Validation

For implementation head `4bbfd0554dbc399853cd72919c00895f364172fc`:

- focused R100B transient tests: **13 passed**
- dedicated exact-half-step workflow `35510597729`: **success**
- ordinary CI `35510597722`: **success — 1079 passed, 2 skipped**
- Windows Release Artifact `35510597777`: **success**
- RDC usage: **0**
- HTDT-Capture changes: **0**

The dedicated workflow artifact retains the full report and raw candidate/modal/system outputs; the repository evidence JSON retains the decision-critical numerical and provenance summary.
