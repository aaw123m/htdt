# R130D original native q0 — x-endpoint and tiny-cut-roof mode localization of sevenpoint CFL failure

2026-10-10 JST. Repository `aaw123m/htdt`; migrated Issue #53 / Draft PR #118. This is an **independent cause-of-CFL analysis of the already rejected experimental physical sevenpoint + true inclined Neumann roof row-lumped-mass solver**. No new production solver and no original PFFDTD requalification.

## Provenance and protocol

Before any of the following *real native five-grid* eigenvector measurements were made, the exact analysis and fixed inclusion criteria were committed to GitHub at [`34547c88234695b7eadc96b191531ef9fba2ae9f`](https://github.com/aaw123m/htdt/commit/34547c88234695b7eadc96b191531ef9fba2ae9f), with `[skip ci]`. The runner verifies plan bytes, pinned SHA ancestry, and the real remote branch HEAD, otherwise fails before opening any original HDF5. Original PFFDTD SHA `aa319f6c86517cb95aabfae8656277da62c3ead5`; all five original comms/voxel SHA; original 8 point input and 8 point observer, native h/Ts/Nt, q0 zero after first impulse, unchanged full 250ms record and signed 40/80Hz authority.

The exact previously published row-lumped physical 7point/true-roof SPD operator was used **unchanged**, preserving all 37,835 / 53,001 / 75,504 / 102,949 / 132,341 positive-support 3D modes, including tiny cut-cell supports and both true x end boundaries. No modes dropped; no time step scaled; no high frequency cutoff, mass floor or pressure window changes.

Physical operators: `M = diag(Mx*1) ⊗ diag(Myz*1)`; `K = Kx ⊗ Myz_lumped + Mx_lumped ⊗ Kyz_hybrid`, where `Kyz_hybrid` is Cartesian full-wet 5point flux plus unmodified exact physical true-inclined-roof Neumann cut-Q1 stiffness. Its 3D eigenvalues are exactly `lambda_x_i + lambda_yz_j`, with generalized `Mx`/`Myz`-orthonormal complete basis. The failure of explicit native leapfrog occurs for `Ts²lambda≥4`.

## Independent Rayleigh and full eigenvector diagnostic

For a physical coordinate-basis vector `e_i`, the exact generalized Rayleigh quotient is `K_ii/M_ii`, so `lambda_max >= max_i K_ii/M_ii`. For the original 3D separable tensor model, a single `x⊗yz` node therefore produces the rigorous bound `Ts²λmax >= Ts²(max_i Kx_ii/Mx_ii + max_j Kyz_jj/Myz_jj)`. This is **independent of reconstructing any high-frequency q0 Fourier response**. Each of five grids exceeds the leapfrog limit 4 from the single coordinate trial vector alone.

| PPW | Total native Ts² λmax (full modes) | Single physical node lower bound Ts²(Kii/Mii) | x-fraction of maximal eigenvalue | Max x-node Rayleigh index / # physical x nodes | Largest y-z mode M-mass within abs roof distance ≤h |
|---|---:|---:|---:|---|---:|
| 28 | 49.0383 | 42.6655 | **84.58%** | 34 / 35 (end) | 99.70% |
| 32 | 7.1199 | 5.4890 | 51.76% | 0 / 39 (end) | 33.62% |
| 36 | 7.9477 | 5.9989 | 54.08% | 43 / 44 (end) | 51.71% |
| 40 | 59.5769 | 52.2134 | **85.90%** | 48 / 49 (end) | 29.94% |
| 44 | 7.1643 | 5.4890 | 51.44% | 0 / 53 (end) | 35.99% |

**Direct cause of the experimentally observed CFL failure:** the *physical mass-to-stiffness distribution*, especially the x-endpoint coordinate-basis quotients on all grids and the largest y-z physical eigenmodes, is sufficient to make explicit leapfrog at the original native Ts unconditionally unusable without further physical scheme modification. The maxima on PPW28 and PPW40 are predominantly **x-direction**, not the sloped roof. This explains why simply repairing the inclined roof's boundary geometry does not cure those worst CFL outliers.

The y-z largest generalized mode is strongly confined to **physically supported tiny-area Q1 nodes**, not a broad room-scale acoustic mode. With the same prospectively frozen support criterion `0 < (Myz·1)_i < 0.01 h²` across all grids:

| PPW | M-mass norm fraction of top y-z eigenvector on tiny support | Tiny support's actual share of physical cross-section area | Enrichment |
|---|---:|---:|---:|
| 28 | **99.51%** | 0.02635% | ≈3,776× |
| 32 | **66.36%** | 0.000422% | ≈157,287× |
| 36 | **94.53%** | 0.005834% | ≈16,204× |
| 40 | **99.63%** | 0.01470% | ≈6,776× |
| 44 | **63.98%** | 0.000229% | ≈279,245× |

Here `M`-mass norm means `sum m_i v_i²/(v.T M v)`; a tiny support can have a disproportionately large **mode shape** even while its total **physical volume** is very small. The 3 top y-z eigenmodes, all fixed support thresholds 1/5/10/50% of h², roof-distance bands ≤1h and ≤2h, top five coordinate Rayleigh nodes and full count/matrix proofs are stored for **all** five original SHA-verified grids. No post hoc choice of a single favorable grid or cut threshold.

## Correct interpretation and next numerical solver constraint

1. Unlike an analogy to Nyquist, the **actual explicit leapfrog bound is 4**. On every grid a *single local physical basis vector* already proves failure. The y-z high-frequency eigenvector's strong localization on tiny supports is verified, while x-endpoint eigenvalues dominate PPW28/40; **both** geometric ends and sloped-roof positive supports require stability treatment.
2. The uppermost mass-localized modes can be physical semidiscrete artifacts of positive-supported Q1 basis with tiny fractional volume. But this result is **not** a causal explanation of why the *original PFFDTD stair-stepped graph* fails its different **finite 250ms signed 40/80Hz point q0 convergence gates**. That separate code uses a different spatial graph; no production qualification follows.
3. A prospective correction must be variationally conservative and Neumann-consistent; it must address x physical end truncations **as well as** true cut-roof tiny masses without arbitrary mass floors, mode deletion or relaxed thresholds. An implicit stable integrator by itself has previously **failed** all original q0 gates, so stability alone is never an acceptance criterion. Geometry, source/receiver dual consistency and broadband finite endpoint behaviour remain separate obligations.

Reproducibility:
- Frozen prereg: `benchmarks/acoustics/r130d_original_q0_true_roof_sevenpoint_CFL_eigenvector_localization_plan_2026-10-10.json`
- Full 5-grid numerical evidence: `benchmarks/acoustics/r130d_original_q0_true_roof_sevenpoint_CFL_eigenvector_localization_evidence_2026-10-10.json`
- Core proof: `backend/src/htdt/r130d_true_roof_cfl_eigenvector_localization.py`
- HDF5 full 5-grid runner: `scripts/run_r130d_original_q0_true_roof_CFL_eigenvector_localization.py`
- Synthetic direct generalized eigensystem / independent Rayleigh and full frozen native SHA regression: `backend/tests/test_r130d_true_roof_cfl_eigenvector_localization.py`

Release gate unchanged: **original PFFDTD SELF_CONVERGENCE_FAILED; independent physics NOT_VALIDATED; product NO_GO; Issue #53 OPEN; PR #118 Draft.** No original PFFDTD waves, GitHub Actions triggers or scratch cleanup.
