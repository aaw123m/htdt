# R130D #938: Independent Neumann room modal spectrum versus unresolved impulsive transfer

**Date:** 2026-10-09. **Authority:** original 56m³ sloped room;
strictly **diagnostic only**, product **NO_GO**.

## A directly executed numerical eigenproblem

The new conservative sloped Neumann FV and the independently
compiled pinned MFEM P2 tetrahedral model were used to solve the
**generalized 3D acoustic natural-frequency eigenproblem**

`K u_j = lambda_j M u_j`,
`f_j = sqrt(lambda_j)/(2*pi)`,
`u_j^T M u_j = 1`,

where `K` already contains `c²`. The original room is
`0≤x,y,z≤4; z+y/4≤4`, with exact volume56m³,
c343.2m/s, rigid natural Neumann walls. This is **not**
a Fourier transform of the driven numerical impulse results:
it is a separate direct spatial operator computation and
contains no time integration or fitted Gaussian source.

The plan was committed **before** execution:
`benchmarks/acoustics/r130d_sloped_neumann_modal_spectrum_plan_2026-10-09.json`.
Reproducible results, including all 12 true mass-normalized eigenpairs'
frequencies, generalized true residuals and signed source/receiver
modal coupling products for every operator:
`benchmarks/acoustics/r130d_sloped_neumann_modal_spectrum_evidence_2026-10-09.json`.

The independent comparator uses the original **actual** pinned MFEM
`d964264cdb9a13e94a201b6c236c7721e0c8765f` quadratic
tetrahedron CSR mass/stiffness matrices at r2/r3/r4, independently
hashed and verified. Not one element of the FV stiffness/face
aperture is reused as a FEM stiffness coefficient. Generalized
eigensystems were solved by SciPy `eigsh`, 12 smallest eigenvalues
including a genuine zero Neumann constant mode, shift `sigma=-1`.
All **72 eigenpairs** passed the prospective generalized residual
bound `≤1e−7`, with maximum observed residual 1.9552e−10.
Zero-mode numerical frequencies were <0.000012Hz; positive
mode residuals are measured against true `K u-lambda M u`, not
an eigensolver exit code. Zero-mode residual uses an
operator-scale denominator to avoid dividing by the true
zero `K u`.

## Source and receiver sensitive frequency neighborhood

| Natural eigenmode (counting zero as mode0) | FV n12 | FV n20 | FV n32 | Independent MFEM P2 r3 | Independent MFEM P2 r4 |
|---|---:|---:|---:|---:|---:|
| Mode1, near 40Hz | 41.8475 | 41.9194 | **41.9441** | 41.9606 | **41.9600** |
| Mode2 | 42.7776 | 42.8559 | 42.8828 | 42.9007 | 42.9000 |
| Mode7, closest to 80Hz | 78.4540 | 78.6369 | **78.6998** | 78.7563 | **78.7411** |
| Mode8, above 80Hz | 84.7268 | 85.3325 | **85.5414** | 85.6965 | **85.6771** |
| Mode9, above 80Hz | 84.8232 | 85.4476 | 85.6622 | 85.8207 | **85.8014** |

The signed modal product
`(b_source^T u_j)*(r_receiver^T u_j)` is invariant
under the global +/- eigenvector sign (though individual mode
orientations inside exactly or nearly degenerate eigenspaces
can rotate). It is reported to prevent overattributing transfer
to the mode *closest in frequency*. For independent MFEM r4:
mode7 near 78.741Hz has signed source/receiver product
**−0.00108870**; the mode8 at 85.677Hz has product
**+0.0345557**. These weights have the chosen delta-functional
normalization, and should be compared **within the same method**
only; the forced response depends on frequency denominators,
modal sums and the windowed source as well.

## Independent spatial mesh refinement of modal frequencies

For the first **11 nonzero** modes, the largest relative
ordered-mode frequency difference across each refinement pair is:

| Operator | Two spatial levels | Largest relative frequency change |
|---|---|---:|
| FV cut-cell | n12→20 | 0.73068% |
| FV cut-cell | n20→32 | **0.25057%** |
| Independent P2 MFEM | r2→r3 | 0.51599% |
| Independent P2 MFEM | r3→r4 | **0.04416%** |

This is strong evidence that the **low-order Neumann
eigenfrequencies** of the original 56m³ geometry are approaching
consistent values, with FM r4 and FV n32 agreeing to tenths
of a hertz. However the actual same-source **unsmoothed**
unit-impulse `P_T/Q_T` problem was separately observed to
have MFEM r3→r4 normalized complex difference **0.26425**,
versus the prior frozen numerical acceptance limit **0.05**,
and FV n28→32 **0.23197** versus limit **0.20**
and an increasing trend.

Therefore the hypothesis *"the impulse nonconvergence is solely
because the low-order 40/80Hz eigenfrequencies are poorly
approximated"* is **not supported** by these calculations.
Well-converged low-order modal frequencies do **not imply**
finite-record broadband impulse transfer convergence. High
modes/point-source spatial regularity, temporal source
representation, residual finite-time modal beating and
receiver functional convergence are plausible further
contributions; the modal calculations alone cannot
identify their separate causal weights.

## Actual files and tests

- `scripts/run_r130d_sloped_neumann_modal_spectrum.py`
- `backend/tests/test_r130d_sloped_neumann_modal_spectrum.py`
- `benchmarks/acoustics/r130d_sloped_neumann_modal_spectrum_plan_2026-10-09.json`
- `benchmarks/acoustics/r130d_sloped_neumann_modal_spectrum_evidence_2026-10-09.json`

To repeat:

```pwsh
$env:PYTHONPATH = 'backend/src'
python scripts/run_r130d_sloped_neumann_modal_spectrum.py `
  --plan benchmarks/acoustics/r130d_sloped_neumann_modal_spectrum_plan_2026-10-09.json `
  --systems benchmarks/acoustics/r130d_mfem_independent_sparse_systems `
  --out scratch/ci_modal_replay.json
```

The analysis is fail-closed on missing spatial levels, missing
Neumann zero mode, source/receiver and original pinned matrix
hash changes, true generalized eigensolver residual, missing
frequency bins, and product authority mutation. No
post-observation tolerances or synthetic reference spectra
were substituted.

### Current decision

- **Original canonical PFFDTD fullband impulse**:
  `SELF_CONVERGENCE_FAILED`.
- **New candidate original unsmoothed impulse**:
  still `NOT_QUALIFIED` (both numerical self gates fail).
- **Changed smooth Gaussian candidate / 3 rigid convex
  rooms**: separate bounded numerical PASS; cannot bypass
  original impulse.
- **Arbitrary nonconvex/curved CAD, owned room/BRAS
  physical data**: `NOT_VALIDATED`.
- **Production**: `NO_GO`, draft PR #1055,
  issue #938 remains open.
