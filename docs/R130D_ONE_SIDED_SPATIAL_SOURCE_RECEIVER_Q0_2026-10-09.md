# R130D original temporal q0: source-only versus receiver-only spatial regularization

Date: 2026-10-09 JST | Issue #938 | PR #1055 Draft

## Preregistered question

The previous P1 spatial experiment had altered **both** source and receiver operators simultaneously. That supported the importance of their combined spatial width, but did not identify whether applying the physical Gaussian only to the source, or only to the receiver, materially changes the grid sensitivity. This follow-up independently changes exactly one operator while leaving the **original unsmoothed discrete temporal source** `q[0]=1 m3/s; q[n>0]=0` unchanged. It is a changed **spatial** model and must not count as canonical point-to-point success.

The complete design was committed before actual numerical observation as [r130d_one_sided_spatial_operator_plan_2026-10-09.json](../benchmarks/acoustics/r130d_one_sided_spatial_operator_plan_2026-10-09.json), commit `def3dca7`. The 56 m³ rigid sloped room is `0<=x<=4, 0<=y<=4, 0<=z<=4-0.25*y`; c=343.2 m/s, rho=1.2 kg/m³. Source (1.5, 2, 2) m and receiver (2.5, 2, 2) m are fixed. Sigma 0.35 and 0.70 m are fixed **physical** isotropic Gaussian widths, each normalized by actual integrated air-domain volume. FV integrates Gaussian over exact cut cells with fixed tensor 4-point Gauss-Legendre and roof-split y intervals; independent P2 MFEM integrates its own Gaussian `DomainLFIntegrator` form using order-10 tetrahedral quadrature, not reused FV weights.

The frozen grids are FV n20/n32 and pinned MFEM P2 r3/r4. The time record is a 0.25s rectangular window, exactly 1000 steps at dt 250μs, both 40 and 80 Hz, signed `P_T/Q_T` with `exp(+iωt_mid)`. Source-normalization and receiver-normalization remain unity. Existing original point-point and both-Gaussian full-wave complex observations are read as immutable hashed controls, and only the two genuinely new mixed configurations are time-integrated on each grid. Baseline SHA-256: original point response `fa2a4aae2c6b00f4e1eccda9d0e9bf9271c420eb0a5f971b26927d9ed76e3cf2`, prior independent both-Gaussian response `627881b9f216f229ce0c2fa9c735e6a5f6ff956e2effd43192494004a5b6650b`.

## Invariance check

The room is geometrically invariant under x↦4−x, mapping its original source and receiver to each other. The conservative FV discrete mass and stiffness operators were verified to be invariant under this exact grid permutation. Point and Gaussian spatial source/receiver stencils also map under the same reflection. Therefore FV *source-only* and *receiver-only* finite-time transfer functions should agree up to solver roundoff, a stronger integrity check than either spatial convergence or simple reciprocity alone. The independent tetrahedral MFEM P2 mesh does not necessarily have the same exact discrete reflection symmetry and is examined without fitting away any discrepancy. A failure to exhibit symmetry is an observation, not an automatic change of predeclared acceptance thresholds.

## Numerical results

The raw signed 40/80 Hz complex finite-window results, magnitude and phase, true linear residuals, point and both-Gaussian controls, and all adjacent-grid comparisons are stored in [r130d_one_sided_spatial_operator_evidence_2026-10-09.json](../benchmarks/acoustics/r130d_one_sided_spatial_operator_evidence_2026-10-09.json).

### Complete actual spatial refinement comparisons

Values are computed from actual wave solves for the two *new* mixed spatial operator combinations and immutable point/point and Gaussian/Gaussian controls. Each cell is `normalized_complex_l2` between the preregistered adjacent spatial grids; **no filtering or retuning**. All 40/80 Hz signed complex values, magnitude errors, phase differences, linear residuals, two Gaussian widths and 16 actual mixed-operator wave results are retained in the raw evidence.

| Method and physical Gaussian sigma | point source, point receiver | Gaussian source, point receiver | point source, Gaussian receiver | Gaussian source, Gaussian receiver |
|---|---:|---:|---:|---:|
| FV n20→n32, σ=0.35 m | 0.288459 | 0.163607 | 0.163607 | 0.157134 |
| FV n20→n32, σ=0.70 m | 0.288459 | 0.167534 | 0.167534 | 0.166888 |
| independent MFEM P2 r3→r4, σ=0.35 m | 0.264249 | 0.145773 | 0.147137 | 0.047765 |
| independent MFEM P2 r3→r4, σ=0.70 m | 0.264249 | 0.015114 | 0.015463 | 0.017375 |

Do **not** interpret low complex L2 alone as acceptance. The σ=0.35 m P2 r3→r4 **source-only** case has maximum per-frequency relative magnitude error **0.625552**, and **receiver-only** **0.632388**; prior both-Gaussian σ=0.35 m has maximum magnitude error **0.189280**. These unfavorable 40/80 Hz values are preserved, not masked. At σ=0.70 m, the P2 mixed source-only and receiver-only magnitude maxima were **0.024992** and **0.024052** respectively, and maximum phase differences **0.947°** and **0.933°**. As this *new one-sided diagnostic* has no newly preregistered independent PASS gate, these are descriptive comparisons, not a manufactured numeric PASS.

### Reflection and reciprocity evidence

The absolute L2 norm across the two frequency bins of (Gaussian-source/point-receiver) minus (point-source/Gaussian-receiver) was:

| Discretization | σ=0.35 m | σ=0.70 m |
|---|---:|---:|
| FV n20 | 3.36e−11 | 3.24e−11 |
| FV n32 | 6.19e−11 | 6.99e−11 |
| independent MFEM P2 r3 | 0.257168 | 0.086455 |
| independent MFEM P2 r4 | 0.033731 | 0.010617 |

The exact FV difference is roundoff-level and agrees with a separately tested discrete x↦4−x reflection mapping of its Neumann mass, stiffness and both source/receiver spatial functionals. MFEM P2 is independently tetrahedralized and may not have that exact discrete reflection permutation; its source/receiver mixed-response asymmetry is visibly smaller at the finer r4 level. This **supports spatial-discretization asymmetry as a contributor** and cross-checks source/receiver assembly consistency, but does not prove an exact causal explanation of canonical run25/run76 nonconvergence.

### Remaining scientific limitations and shipping gate

Neither this one-sided spatial diagnostic nor the previous σ=0.70 m combined spatial-Gaussian candidate evaluates the original *point-to-point* spatial contract. Both source/receiver point functionals at full broad-band q0 still fail spatial self-convergence. There remains no evidence of original PFFDTD PPW8–44 full-band PASS or independently measured BRAS/owned-room physical validation. General nonconvex CAD and multiple connected fluid regions remain unsupported. A direct canonical PFFDTD solver numerical/geometry/source-and-receiver discretization redesign and physical held-out validation are required before considering production promotion.

Canonical original R130D = **SELF_CONVERGENCE_FAILED**; original point/point candidate = **NOT_QUALIFIED**; measured physical holdout = **NOT_VALIDATED**; product = **NO_GO**; PR #1055 remains Draft, Issue #938 OPEN. CI green is a reproducibility result, not physical or canonical point-source acceptance.
