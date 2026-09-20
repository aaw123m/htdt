# R100B MFEM semidiscrete modal evolution experiment — 2026-09-20

Issue: #101  
Base main at task start: `49901a6b46ad889bd06b34e4482a5b6121545d1d`  
RDC usage: **0**

## Purpose

PR #274 established that the current MFEM concave finite-record time track fails even when the
spatial discretization is fixed to H1 p2 / one uniform refinement:

- 6000 -> 9000 complex-RMS relative error: `1.3397652498`
- 9000 -> 12000 complex-RMS relative error: `1.3596844444`
- final 9000 -> 12000 delta: `21.4094258604 dB / 9.0797431125 relative / 176.710382952 deg`
- linear residuals were approximately `1e-12`
- the h2 spatial attempt was separately BLOCKED by wall time

This experiment changes only the homogeneous time evolution method for the exact same p2/h1
semidiscrete MFEM mass/stiffness/source/receiver system. It is intended to separate Newmark
temporal evolution from the remaining source/receiver/finite-record representation.

The current production-adoption decision remains **NO_GO** before this experiment.

## Frozen authority

No R100A fixture or tolerance is changed.

- R100A semantic hash: `a9d45a3d650f20747368dd5610a6a91f93cdad881dcb10fcac88cd9d17e211e7`
- fixture: `wave-concave-l-room-v1`
- candidate: `mfem-v4.10-d964264`
- MFEM source: `mfem/mfem@d964264cdb9a13e94a201b6c236c7721e0c8765f`
- H1 order: **2**
- uniform h refinement: **1**
- expected elements: **40**
- expected DOFs: **525**
- geometry: exact five-hex L-prism
- rigid boundary: natural Neumann
- mass: MFEM `MassIntegrator`
- stiffness: MFEM `DiffusionIntegrator(c^2)`
- source/receiver: MFEM `DomainLFIntegrator(DeltaCoefficient)`

The existing finite-record contract remains:

- quantity: `P_T/Q_T`
- unit: `Pa/(m3/s)`
- source normalization: volume velocity
- coordinate convention: `x_right_y_rear_z_up`
- interpolation: `linear_complex`
- Fourier convention: `exp(-i*omega*t)`
- DTFT kernel: `exp(+i*2*pi*f*n*dt)`
- half-open `[0,2 s)` record
- 20–300 Hz / 1 Hz
- magnitude null mask: -60 dB
- phase null mask: -40 dB
- tolerance: **0.75 dB / 0.05 relative magnitude / 8 deg**

## Predeclared modal authority

The immutable plan is:

`benchmarks/acoustics/r100b_mfem_modal_experiment_plan.json`

Generalized eigenproblem:

`Kc2 v = lambda M v`

Mass normalization:

`V^T M V = I`

The basis is **not selected after observing results**. The experiment retains all 525 generalized
eigenpairs. There is no modal cutoff and no truncation.

Numerical rule:

- IEEE-754 float64
- NumPy `2.1.3`
- SciPy `1.14.1`
- `scipy.linalg.eigh(..., type=1, driver="gvd")`
- mass-orthonormality maximum absolute tolerance: `1e-10`
- generalized eigen residual relative tolerance: `1e-10`
- materially negative eigenvalue rule: block if
  `lambda < -1e-10 * max(1, max_abs_lambda)`
- numerical negative values inside that bound are clamped to zero before `sqrt(lambda)`

No spectral range is selected after the run.

## Source projection and initial conditions

To isolate Newmark rather than silently change the current finite-record source mapping, the
existing source kick is retained exactly for each output grid:

`phi_t(0+) = c^2 * dt * M^-1 * b * q[0]`

with `phi(0)=0`.

Modal projection:

`g = V^T M phi_t(0+)`

Receiver projection:

`r^T V`

Exact homogeneous reconstruction:

`p(t) = rho * sum_j ((r^T v_j) g_j cos(sqrt(lambda_j) t))`

This is intentionally important: if the current sample-rate-dependent source kick or finite-record
normalization itself prevents convergence, the modal experiment must expose that rather than
renormalize it after seeing results.

## Predeclared output grids

Exactly three attempts are allowed:

| attempt | spatial system | sampled output grid |
|---|---|---:|
| `modal-6000` | p2/h1, 40 elements, 525 DOFs | 6000 Hz |
| `modal-9000` | same exact system | 9000 Hz |
| `modal-12000` | same exact system | 12000 Hz |

The semidiscrete system is exported once from the existing MFEM finite-record probe assembly path.
Only output sampling and the already-authoritative `dt`-dependent source kick differ between
attempts.

## Decision

Adjacent pairs use the same current R100A comparison semantics as PR #274.

**PASS** requires all three attempts to complete, adjacent complex-RMS error to strictly decrease,
and the final 9000 -> 12000 pair to satisfy all current tolerances.

A PASS is diagnostic only. It does not select or promote MFEM as production solver.

**FAIL** means the full-basis non-dissipative modal evolution still does not converge to current
tolerance. That excludes Newmark as the sole explanation. The next isolation target is chosen from
the actual error structure, without altering this experiment after results.

**BLOCKED** preserves any resource/library/numerical limitation. No modal count, cutoff, mesh,
sample rate, source mapping, or tolerance may be substituted automatically.

No experiment outcome is promoted into production selection by this task.

## Implementation

New task-owned files:

- `backend/src/htdt/acoustic_bakeoff_mfem_modal_experiment.py`
- `backend/tests/test_acoustic_bakeoff_mfem_modal_experiment.py`
- `scripts/run_r100b_mfem_modal_experiment.py`
- `benchmarks/acoustics/r100b_mfem_modal_experiment_plan.json`
- `.github/workflows/r100b-mfem-modal-experiment.yml`

Minimal existing benchmark-helper change:

- `benchmarks/acoustics/mfem_probe/concave_finite_record.cpp`
  - normal Newmark execution is unchanged;
  - an assemble-only system-export path exposes the exact assembled M/K/source/receiver arrays used
    by the same probe.

No R120/R130/R140/R150/R160/R170, GUI/UX, canonical status/roadmap, or HTDT-Capture file is changed.

## Executed result

Dedicated GitHub Actions run `35505076672` completed successfully as an evidence-generation
workflow. The numerical/physics outcome is **PASS**.

Immutable artifact:

- artifact id: `10602989820`
- name: `r100b-mfem-modal-experiment`
- size: `1089889` bytes
- artifact digest:
  `sha256:6670c8206bbcf73752fba07bd7f32857aa70f8f29c186ff167bdb45c64b15593`
- artifact `report.json` SHA-256:
  `754ae55a478b0306d0d059f72444b1f795971dcae908681c381b8ac4ec6f4432`
- deterministic report identity:
  `d282519330aef2ddc6991e4fefc4d090fccb397e3f7a251400f159cf9b9811bf`

Execution head:

`2dea6beef81528497511266c2278be0e4124663f`

Exact semidiscrete-system numerical identity:

`f48eb9a7fc5881fd8d2f26b32fc1f20df0f71290ecc4ed0337087073555cc61d`

The system remained the exact frozen p2/h1 discretization: 40 elements / 525 DOFs. The exported
mass and stiffness matrices were exactly symmetric at the recorded float64 representation.

### Modal numerical qualification

The predeclared full 525-vector generalized eigenbasis was retained. No modal truncation or cutoff
was used.

- retained basis: `525 / 525`
- generalized eigen residual: `1.9766731790476663e-15`
- mass-orthonormality maximum absolute error: `2.6645352591003757e-15`
- retained modal frequency range: approximately
  `2.5052661663e-06 Hz .. 687.0555312344 Hz`
- negative eigenvalues clamped: `0`
- eigensolve: `0.0317006 s`

These diagnostics are far inside the predeclared `1e-10` numerical controls.

### Attempts

| attempt | status | samples | modal reconstruction | source mass residual |
|---|---|---:|---:|---:|
| `modal-6000` | COMPLETED | 12000 | `0.0655399 s` | `4.1315160e-16` |
| `modal-9000` | COMPLETED | 18000 | `0.1043478 s` | `3.7496045e-16` |
| `modal-12000` | COMPLETED | 24000 | `0.1300986 s` | `4.1315160e-16` |

### Adjacent-pair result

Complex-RMS relative error:

- 6000 -> 9000: `0.0035565489753706346`
- 9000 -> 12000: `0.0018495018125891524`

The sequence is strictly decreasing.

6000 -> 9000:

- magnitude absolute delta: `0.9108070393695016 dB`
- magnitude relative delta: `0.0995499054132496`
- phase delta: `4.542899727851477 deg`

9000 -> 12000:

- magnitude absolute delta: `0.44114943637837456 dB`
- magnitude relative delta: `0.04952099512093281`
- phase delta: `2.398810541827885 deg`

The first adjacent pair is not required to satisfy the final tolerance. The predeclared decision
requires decreasing adjacent complex-RMS error and the **final** pair to satisfy the unchanged
current R100A limits. The final pair satisfies all three:

- `0.4411494 < 0.75 dB`
- `0.049520995 < 0.05`
- `2.398811 < 8 deg`

Therefore the modal time track is **PASS**.

### Interpretation

PR #274 showed gross non-convergence with Newmark on the same p2/h1 spatial configuration. This
experiment replaced only that homogeneous time evolution with exact full-basis modal evolution
while preserving the same assembled M/K/source/receiver system, current `dt`-dependent source
kick, sampled output grids, finite-record observable, masks, and tolerances.

The changed factor is therefore strong evidence that **Newmark temporal dispersion is a major
cause of the current PR #274 time-track failure**. The experiment does not establish that every
remaining source/receiver/finite-record modeling choice is production-valid, and it does not make
MFEM production-ready by itself.

No attempt is promoted. The production-adoption decision remains **NO_GO** and no production
solver is selected.

The checked-in candidate-local evidence is:

`benchmarks/acoustics/evidence/r100b_mfem_modal_2026-09-20.json`

Historical negative evidence from PR #274 remains unchanged.

### Resource evidence

- MFEM configure/build: `598.7060038 s`
- eigensolve: `0.0317006 s`
- total three modal reconstructions: `0.2999863 s`
- observed Python process RSS: `98.89453125 MiB`
- experiment work disk: `9.25096035 MiB`

The modal solve itself is small relative to the native MFEM build. No experiment resource ceiling
was approached.

### Execution-history note

The first dedicated run, `35503575607`, successfully generated all three numerical traces but
failed in post-processing because the runner referenced a non-existent comparison helper name.
That was an implementation failure, not a physics BLOCKED result. Run `35504382119` was
cancelled after the exact helper-signature correction superseded it, before numerical execution.
No modal count, cutoff, mesh, output grid, source mapping, or tolerance was changed in response to
those failures.

### Validation

- focused modal contract: **7 passed**
- dedicated modal workflow `35505076672`: **PASS as evidence generation**
- physics outcome: **PASS**
- ordinary CI `35505076628`: **PASS — 1018 passed, 2 skipped**
- Windows Release Artifact `35505076736`: **PASS**
- RDC usage: **0**
- HTDT-Capture changes: **0**

Workflow PASS and physics PASS remain separate fields even though both are PASS for the final run.

## Production-readiness effect

The production-adoption decision remains **NO_GO**. This experiment is diagnostic evidence, not a
production-selection gate. It establishes a selection-relevant reason to continue with a
production-suitable time integrator rather than spending the next experiment on the blocked h2
mesh.

## Smallest next selection-changing experiment

Keep the exact current R100A fixture and the same p2/h1 semidiscrete M/K/source/receiver system.
Test a production-suitable **low-dispersion, non-dissipative transient integration** method against
the full-basis modal result.

The next experiment must not:

- relax the `0.75 dB / 0.05 / 8 deg` tolerance;
- change source/receiver authority;
- change the p2/h1 spatial discretization;
- substitute a favorable output grid after seeing results;
- retry h2 as the next step.

A qualifying transient method should reproduce the same 6000/9000/12000 convergence behavior and
final current-R100A tolerance without relying on full dense modal decomposition as the production
execution path.
