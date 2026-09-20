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

Pending dedicated GitHub Actions run.

Workflow success will mean evidence generation succeeded; it will not be reported as physics PASS
unless the numerical decision itself is PASS.
