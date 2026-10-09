# R130D #938 — Full physical 56 m³ roof-conforming P1 FEM with unchanged original eight-node point q0: five-grid FAIL

Date: 2026-10-09 JST. Work branch `feat/r130d-embedded-neumann-fv-20261009`, PR #1055 Draft, Issue #938 OPEN.

## Physical spatial numerical scheme actually implemented

This is a NEW, genuine, symmetric and energy-consistent three-dimensional variational finite-element spatial solver for the *physical planar Neumann roof*, **NOT** simply another `KM^-1K` correction, source filter or alternative pressure scoring.

The original native PFFDTD source/receiver HDF5, SHA-pinned upstream code revision `aa319f6c86517cb95aabfae8656277da62c3ead5`, physical source (1.5,2,2)m and receiver (2.5,2,2)m, all **eight original Cartesian interpolation weights and indices**, original native h/Ts/Nt/q[0]=1 then q[n>0]=0, recorded full 250 ms, **both signed** 40 and 80 Hz pressure `P_T/Q_T`, fixed physical sound speed 343.2 m/s and density 1.2 kg/m³ and original acceptance **0.20 complex / 0.25 relative magnitude / 15° phase** are all retained. Source and receiver are mapped to the **exact same 8 original Cartesian nodes**, without relocation, recomputation of input weights, Gaussian smoothing or temporal waveform replacement. Added boundary FEM nodes receive **zero** original source impulse load. Original native upstream PFFDTD files and numerics unchanged.

Spatial operator:
- Cross section is the true **convex physical trapezoid** `0<=y<=4`, `0<=z<=4-.25y`, area **14 m²**. The P1 mesh contains **every original native Cartesian interior (y,z) node** plus exact original grid-line intersections with the **true** sloped roof and all other physical outer boundaries. SciPy Delaunay yields a complete physical conforming triangle mesh; **no triangles removed, small triangle area cutoff, nodal agglomeration, fitted boundary shifts or modal filtering**.
- Each triangle contributes the true Galerkin stiffness `K_ab=c²*Area*(grad Na·grad Nb)` with the exact physical P1 element-lumped mass `Maa=sum Area/3`. The roof and other walls impose **natural zero-flux Neumann**. The x-axis is original native physical nodes with exact x=0 and x=4 added, with exact P1 line-element mass lumping and natural Neumann stiffness.
- Exact full 3D tensor variational operator `M=Mx⊗Myz`, `K=Kx⊗Myz + Mx⊗Kyz` assembled independently as real sparse full 3D matrices, and true symmetric, nullspace, generalized eigenpair and physical-volume checks performed.
- All physical mass-normalized generalized modes **including every high-frequency mode** are retained. The analytically exact rigid Neumann constant mode is fixed to its exact mass-normalized constant vector to avoid floating eigensolver cancellation on very narrow boundary elements; other eigenvectors and eigenvalues are unchanged and independently verified with **unchanged strict 2e−7 true generalized residual** and 5e−8 M-orthonormality controls. The FEM stiffness diagonal is reconstructed using its actual off-diagonal Galerkin terms to enforce K·1=0 against floating roundoff; physical spatial stiffness/energy/source remain the same to machine accuracy. This does **NOT** truncate a mode or change the q0 source.
- The time integrator is the earlier physically conservative beta=1/4 Newmark `A=dt²*c²*(original physical 8node modal interaction)/(1+dt²lambda/4)`, preserving original sample clock and complete centered pressure derivative plus both one-sided endpoints for the full original 250 ms signed 40/80 Hz finite spectrum. The *previous* exact-roof FV/Newmark actual true **full-state CG wave** results are independently retained as the baseline comparator.

The true P1 FEM weak-form stiffness passes an **independent manufactured affine-field energy test** for a full 3D gradient `g=(.37,.81,-.2)`: exactly `u^T K u = c² * 56m³ * ||g||²` (numerically to prescribed relative 2e−10). It also passes a physical Neumann constant-mode test and explicit synthetic eight-node source tensor coupling checks. Thus the solver itself implements a physically meaningful variational discretization rather than a speculative scalar dispersion multiplier.

## Preregistration, capacity addendum, provenance

The full physical mesh, mass/stiffness/time, 8node input, original gates and **five grids** were frozen in plan committed/pushed **before any FEM q0 outcomes** at `4950dc5bc33754fbb482700972aa01fc111893d2`. Actual PPW28/32/36/40 point q0 results were then computed. The original resource-only max yz vertices 2,200 was **too small for PPW44**, and the runner stopped **before PPW44 eigensolver/scoring**. Before calculating PPW44, a separate **prospective resource-only addendum** was committed and pushed as `bf0464b783d25661302c13018b4df5902d38dffa`: increase **ONLY PPW44 yz capacity 2,200→3,000**, retain true maximum 180,000 3D DOFs. The four earlier outcomes were openly labeled previously observed. No changes whatsoever to physical method, source, window, signed scoring or acceptance gates. The actual PPW44 mesh has 2,497 yz vertices, so the addendum enables all true physical modes. Both frozen plans are embedded with exact hashes in the JSON, and fail-closed guards reject relaxing the resource cap or acceptance values.

## Full untruncated native-source true-domain q0 five-grid results

Actual original-clock exact roof full 3D generalized modal content, all retained:

| PPW | Full true 3D P1 modes | Physical yz triangles | Min retained triangle area (m²) | Frequencies above native Nyquist |
|---|---:|---:|---:|---:|
| 28 | 37,835 | 2,024 | 2.452e−6 | 2,874 |
| 32 | 53,001 | 2,564 | 6.730e−4 | 354 |
| 36 | 75,504 | 3,258 | 1.253e−4 | 710 |
| 40 | 102,949 | 4,008 | 7.257e−5 | 5,050 |
| 44 | 132,341 | 4,784 | 3.001e−4 | 766 |

**Total 401,630 true 3D finite-element modes** evaluated across 5 levels; none capped/clamped or removed. Even without volume-cutcell slivers, the exact roof-conforming P1 triangles can become narrow near original grid intersections, causing large non-monotonic high-frequency populations. This is a **numerical observation**, not proof of a unique root cause or a justification to remove these modes. All original physical q0 high-mode contributions remain scored.

**Two-bin signed 40/80 Hz self-refinement, complex relative norm (gate ≤0.20), maximum relative amplitude (gate ≤0.25), maximum phase (gate ≤15°):**

| PPW adjacent pair | Previous true physical exact-roof FV/Newmark full-state CG complex | NEW conforming P1 FEM complex | P1 FEM max relative magnitude | P1 FEM max phase | P1 FEM full frozen original 3-gate verdict |
|---|---:|---:|---:|---:|---|
| 28→32 | **0.052369** | **0.152290** | **2.550578** | **21.703°** | **FAIL** |
| 32→36 | **0.218508** | **0.178714** | **0.564197** | **168.872°** | **FAIL** |
| 36→40 | **0.106168** | **0.407179** | **0.481237** | **177.468°** | **FAIL** |
| 40→44 | **0.872666** | **0.578551** | **0.744449** | **29.556°** | **FAIL** |

All four pairs FAIL the frozen original simultaneous magnitude/phase/complex thresholds for FEM; the error sequence is **not** monotonic. The attractive PPW28→32 or 32→36 complex-only gate **MUST NOT** be described as full convergence: relative magnitude and/or phase fail markedly. At PPW32→36, phase is almost anti-correlated (168.872°), despite complex norm 0.179, demonstrating signed amplitude cancellation and frequency normalization pitfalls.

New FEM differs physically/numerically from existing exact FV/Newmark by relative full signed 40/80 outputs up to **0.947410 at PPW44**; this difference is an expected operator change, not an independent physical reference. No favorable grid or metric was selected for acceptance.

All real per-grid **signed** complex 40/80Hz source transfer coefficients, all unfavorable per-bin magnitude/phase details, original SHA checks, true physical mesh counts, geometry energy/null/eigen residuals, previous FV full-wave data and prospective plans are preserved without rounding in [the full evidence JSON](../benchmarks/acoustics/r130d_original_q0_conforming_roof_p1_fem_multigrid_evidence_2026-10-09.json). Implementation `backend/src/htdt/r130d_conforming_roof_p1_fem.py`; real native HDF5 five-grid solver `scripts/run_r130d_original_q0_conforming_roof_p1_fem_multigrid.py`; independent manufactured P1 energy/8node/real-wave negative tests `backend/tests/test_r130d_original_q0_conforming_roof_p1_fem_multigrid.py`.

## No qualification promotion

This is a **real** coherent variational spatial numerical scheme and it did NOT cure original broadband point-q0 self-convergence. The exact roof natural Neumann FEM is stronger spatial physics than staircase or aperture-only FV, but this single scheme is not a proof of convergence order for a singular point delta Green function observed over an unwindowed 250ms trace. Needed future work: define a mathematically convergent point-source/point-observation continuum target with explicit temporal distribution interpretation, independently validate source and receiver Galerkin consistency, perform held-out grid studies under the unchanged complete original q0 and frequency bins, and finally re-run canonical original run25/run76 PPW8/10/12 and BRAS/owned-room physical validation. Modal suppression, cutoff of skinny triangles, retuning q0, smoothing, tapering, movement of source or receiver and relaxing gates are forbidden as retroactive fixes.

**Canonical ORIGINAL upstream PFFDTD original 8node point q0: SELF_CONVERGENCE_FAILED. Independent physics: NOT_VALIDATED. Product: NO_GO. PR #1055 Draft OPEN, Issue #938 OPEN.** Local tests only, no new original PFFDTD waves and no manual GitHub Actions runs; `scratch/` preserved.
