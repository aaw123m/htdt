# R130D #938 — Original discrete impulse: 12 genuine Neumann modes vs actual full spatial wave

**Date:** 2026-10-09, preregistered before numerical projection.

## Major new numerical finding

Although the first 11 nonzero natural room modes converge well,
the original non-Gaussian `q[0]=1,q[n>0]=0` 250ms finite-window
wave `P_T/Q_T` does not. We have now **reintegrated the exact
same original discrete impulse on a 12-mode mass-orthonormal
modal subspace** using the eigenvalues and actual signed
source/receiver modal couplings extracted independently
from full conservative FV and pinned P2 MFEM.

| 250µs, 250ms; 40/80 Hz; original q[0]=1 | Full 3D wave spatial refinement complex L2 | Only first 12 Neumann modes reconstructed at same q[n] |
|---|---:|---:|
| Exact-cut FV n20→n32 | **0.288459** | **0.165639** |
| Pinned independent P2 MFEM r3→r4 | **0.264249** | **0.016229** |

This is a numerical sensitivity diagnosis of the **same time source
samples**, not a Gaussian replacement or a changed time integration.
In the independently assembled MFEM example, limiting the
source/receiver to the lowest 12 spatial eigenmodes reduces
the grid difference from26.4% to1.62%. Thus the FV/MFEM
original broad-band impulse failure cannot be explained
entirely by errors in these low natural frequencies. Spatial
modes ABOVE the truncated 12-mode band and their source/receiver
coupling are clearly **material to the full discrete time
response**, especially for MFEM.

It is NOT evidence that a bandlimited candidate solves the
original broadband physics: discarding modes changes the
physical observable. Full space r4 and FV n32 still fail their
prospective original-impulse numerical self gates.

## How the modal actual wave was rebuilt

For original sloped room56m³, independent generalized eigenmodes

`K*x_j = lambda_j*M*x_j`, `x_j.T*M*x_j=1`,

the normalized semidiscrete acoustic equation becomes

`eta_j'' + lambda_j eta_j = c² (x_j.T b) q(t)`.

The actual pressure sample at the receiver is

`p_n = rho * Σ (r.T x_j) *
       (eta_dot_j[n]+eta_dot_j[n+1])/2`.

We reconstruct it without arbitrary signed eigenvector choices
using the actual sign-invariant product
`(b.T x_j)*(r.T x_j)` stored for all 72
eigenpairs in the preceding independent FV/P2 modal
evidence. Each independent modal oscillator uses the
**same implicit midpoint time step** dt250µs, n=1000,
and direct `q[0]=1` RHS forcing as the actual full
spatial wave solver; P_T and Q_T use the same
`exp(+iωt)` midpoint Fourier integration at 40/80Hz.

This is a **physical 12-mode Galerkin truncation experiment**
and not a fake fit to the measured full response. All
12 reconstructed modes, including the genuine Neumann
constant mode, and both signed complex bins are preserved.

For example the directly reconstructed P2 MFEM:

- r3: 40Hz `36.1451 + 2.7502i`;
  80Hz `61.9448 − 204.6236i`.
- r4: 40Hz `36.5669 + 2.9835i`;
  80Hz `64.9380 − 202.8593i`.

**Identity-of-method validation:** two new independent tests
compute a **complete** dense generalized eigensystem for
small 3D sloped FV grids (n3 and n4), reconstruct the true
original q[0]=1 finite-window transfer using *ALL*
spatial eigenmodes, and verify that the numerical
result agrees with the separate full-state FV midpoint
solver to `rtol=2e−6,atol=2e−5`. This checks both the
modal RHS and receiver pressure formula against a full
wave step, not merely against our previously stored
truncated numbers. Five new tests passed.

## Same-grid full vs truncated differences (diagnostic)

| Full solver level | Full-response vs 12-modal response relative complex L2 |
|---|---:|
| FV n12 | 0.205326 |
| FV n20 | 0.131825 |
| FV n32 | **0.460270** |
| Independent MFEM P2 r2 | 0.190708 |
| Independent MFEM P2 r3 | **0.579904** |
| Independent MFEM P2 r4 | **0.270557** |

Large differences between truncated and full responses
confirm that the finite record response is not entirely
spanned by the lowest 12 room modes. Values are computed
with signed complex arithmetic, not magnitude fitting.
Individual modes near degeneracy may rotate but their
combined physical receiver response is determined by
the signed products of modal projections.

**Limit of inference:** Difference caused by removing
higher spatial modes does not by itself establish whether
their numerical representation, the point-source
regularity, reflection / finite-time beating, or temporal
sampling is the sole mechanism of nonconvergence. Distinguish
stable low natural eigenfrequencies from reliable
full-band transfer functions.

## Frozen provenance

- Prospective plan: `benchmarks/acoustics/r130d_original_impulse_12mode_projection_plan_2026-10-09.json`.
- Actual modal signed eigenvalue / source-receiver coupling:
  `benchmarks/acoustics/r130d_sloped_neumann_modal_spectrum_evidence_2026-10-09.json`.
- All actual full-space original impulse observations:
  `benchmarks/acoustics/r130d_exact_discrete_impulse_candidate_evidence_2026-10-09.json`.
- Computed modal original impulse 40/80Hz transfers for
  all six grids and original/full-versus-truncated
  discrepancies: `benchmarks/acoustics/r130d_original_impulse_modal_projection_evidence_2026-10-09.json`.
- Executable: `scripts/run_r130d_impulse_modal_projection.py`.
- Five regression/independent complete-basis checks:
  `backend/tests/test_r130d_modal_projection_evidence.py`.

To reproduce on pinned Python:

```pwsh
$env:PYTHONPATH = 'backend/src'
python scripts/run_r130d_impulse_modal_projection.py `
  --plan benchmarks/acoustics/r130d_original_impulse_12mode_projection_plan_2026-10-09.json `
  --modes benchmarks/acoustics/r130d_sloped_neumann_modal_spectrum_evidence_2026-10-09.json `
  --full-impulse benchmarks/acoustics/r130d_exact_discrete_impulse_candidate_evidence_2026-10-09.json `
  --out scratch/ci_original_impulse_modal12.json
```

**Frozen authority:** original R130D canonical fullband
`SELF_CONVERGENCE_FAILED`; new candidate unfiltered
impulse `NOT_QUALIFIED`; curved/concave/multicompartment
CAD `NOT_VALIDATED`; BRAS/owned room physical data
`NOT_VALIDATED`; product **NO_GO**. Issue #938 remains
OPEN and PR #1055 DRAFT. Nothing here validates a
new filter in a production source signal.
