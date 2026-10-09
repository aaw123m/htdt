# R130D original q0 — Cartesian seven-point interior + physical true-roof row-lumped mass — REJECTED

Date: 2026-10-10 JST. Migrated repo: `aaw123m/htdt`. R130D issue #53, Draft PR #118. **Negative result, preserved without exclusion.**

## Experimental motivation and preregistration

The previous symmetric full-wet Cartesian y-z 5-point / exact true inclined-Neumann cut-Q1 roof solver with fully consistent x/y-z mass remained nonconvergent. A physically weighted POSITIVE row-sum mass (diagonal in x and y-z) makes the complete 3D **fully wet interior** reproduce the Cartesian **seven-point axis stencil**, while retaining exact physical inclined-roof weak Neumann stiffness on boundary-cut elements. The transition operator remains symmetric/conservative and volume-preserving. This was not another variation of the previous all-cut Q1 diagonal mass experiment: it COMBINES the native Cartesian 5-point flux stabilization in the intact interior with physical cut-roof positive diagonal masses. No fitted coefficient.

The immutable experimental plan `benchmarks/acoustics/r130d_original_q0_cartesian_roof_row_lumped_mass_plan_2026-10-10.json` was **committed and pushed to GitHub** as [`d2ea0a531853988e79ae38f8c9a2922ab9be0ae7`](https://github.com/aaw123m/htdt/commit/d2ea0a531853988e79ae38f8c9a2922ab9be0ae7) **before new all-five q0 results were computed**. The executable verifies the live remote ref, plan SHA and byte identity, otherwise fails closed. No GitHub Actions triggered.

## Original SHA-native experiment and physically verified 3D operator

- Original PFFDTD pin `aa319f6c86517cb95aabfae8656277da62c3ead5` and true SHA-verified `comms_out.h5`/`vox_out.h5` for all five PPWs **28,32,36,40,44**. Original 8+8 native Cartesian source/receiver indices and weights, q0 impulse followed by zeros, native dt/h/Nt, full 250ms recorded pressure, unwindowed signed 40Hz/80Hz complex transfer. All 37,835/53,001/75,504/102,949/132,341 3D modes are retained. No sliver nodes removed, no source smoothing, mode fitting, damping, time/phase/level tuning, high-mode cutoff or invented frequencies.
- Old x consistent mass `Mx` and Q1 cut-roof consistent mass `Myz` have row-sum mass `diag(Mx·1)`, `diag(Myz·1)`, each strictly positive and physical-volume exact. Full `M=Mx_lump ⊗ Myz_lump`, `K=Kx ⊗ Myz_lump + Mx_lump ⊗ Kyz_cartesian_true_cut_roof`. Full mass is positive diagonal with physical **56 m³** sum; natural Neumann constant `K1=0` and roof tangential affine Neumann manufactured test pass on every grid.
- At an untouched Cartesian interior node, **Mii = h³**, `Kii=6 c² h`, and its six axis neighbors `Kij=−c² h` (everything else zero to numerical tolerance). The measured seven-point stencil relative error across five PPWs was at most **7.8×10⁻¹⁵**. Thus the FULL native interior stencil was verified exactly; this does **not** imply that inclined-roof boundary closure converges for broadband impulses.
- Analytic complete-mode Newmark pressure transfer and the same upstream normal/forward/backward native pressure observer were used at native 250ms. All original signed comparison thresholds, four adjacent pairs and strict monotonicity retained.

## All five real full-original-q0 convergence gates

| PPW adjacent pair | Complex RMS relative, max 0.20 | Magnitude relative, max 0.25 | Phase max, max 15° | Verdict |
|---|---:|---:|---:|---|
| 28→32 | 0.31835655 | 2.09526574 | 14.12779° | FAIL |
| 32→36 | 0.32707544 | 0.73561485 | 13.57365° | FAIL |
| 36→40 | 0.22642250 | 0.68125286 | 10.44700° | FAIL |
| 40→44 | 0.58903424 | 0.16342611 | 175.65871° | FAIL |

**ZERO of four pairs passes all three gates.** The original strictly decreasing three-metric condition also fails. Native high modes and complete signed window were preserved. This experimental solver is **NOT accepted** and cannot be substituted for the original PFFDTD or previous consistent mass hybrid.

## Independent early first-roof analytic weak reflection: negative evidence

Compare the exact original 8-source × 8-receiver physical true-roof single-bounce analytical weak pressure against the **all-mode** numerical early roof weak witness. The same preregistered center/time radius and three widths (0.35/0.60/0.85ms) were used; original native q0 window and acceptance were **not** modified. The witness may contain numerical direct-pulse tails and is not an isolated complete roof-response solution.

| PPW | New 7-point positive-mass relative error, 3 widths | Prior consistent-mass hybrid relative error, 3 widths |
|---|---|---|
| 28 | 1.393 / 1.399 / 1.381 | 0.047 / 0.042 / 0.013 |
| 32 | 0.829 / 0.841 / 0.845 | 0.002 / 0.115 / 0.103 |
| 36 | 1.157 / 0.996 / 0.911 | 0.017 / 0.053 / 0.025 |
| 40 | 0.715 / 0.863 / 0.907 | 0.051 / 0.098 / 0.148 |
| 44 | 0.866 / 0.908 / 0.859 | 0.023 / 0.006 / 0.011 |

**0 of 15 weak cases improves versus prior hybrid.** Increased direct-pulse leakage, high-frequency mass-lumping dispersion and geometric cut sliver observability all remain candidates, but these errors do not alone uniquely identify cause. Row-sum masses conserve volume but demonstrably do **not** rescue physical or full original q0 convergence.

## Reproducibility and release status

- Executable: `scripts/run_r130d_original_q0_cartesian_roof_row_lumped_mass.py`, physical 7point invariant module: `backend/src/htdt/r130d_cartesian_true_roof_lumped_sevenpoint.py`, full signed evidence: `benchmarks/acoustics/r130d_original_q0_cartesian_roof_row_lumped_mass_evidence_2026-10-10.json`, regression: `backend/tests/test_r130d_cartesian_true_roof_lumped_sevenpoint.py`.
- Reference: prior consistent-mass hybrid [`f877413`](https://github.com/aaw123m/htdt/commit/f877413bc9af6e9cafb710cb4bf2a61fb987f086), full modal endpoint attribution [`8e4cc8a`](https://github.com/aaw123m/htdt/commit/8e4cc8a1818668b4491f466b20cffab205bbb75b). The latter already shows that PPW32→36 40Hz finite signed transfer has 55.75% aligned with the terminal backward pressure sample and 50.35% with 1600–3200Hz semidiscrete modes, not uniquely causal.
- Future precommitted physics studies should focus on broadband point-to-point Green's function, finite-record endpoint sensitivity, modal dispersion and native source/observer compatibility. Do **not** truncate modes or change original q0 for product acceptance.

**Original PFFDTD: SELF_CONVERGENCE_FAILED. Independent physics: NOT_VALIDATED. Product: NO_GO.** Migrated Issue #53 OPEN; PR #118 DRAFT; no automatic merge, no new original PFFDTD waves, no GitHub Actions launched; `scratch/` preserved.
