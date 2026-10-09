# R130D native original-q0 seven-point physical cut-roof explicit leapfrog CFL — five-grid negative result

2026-10-10 JST. Migrated repo `aaw123m/htdt`; issue #53 (former #938); Draft PR #118. The user's original acceptance still **SELF_CONVERGENCE_FAILED**, independent physics **NOT_VALIDATED**, product **NO_GO**.

## Experiment preregistered before any new five-grid real-HDF5 spectral results

The exact plan was pushed to the working GitHub feature branch as [`98913220e22d50fe8f0a19454a2e26749e5f758f`](https://github.com/aaw123m/htdt/commit/98913220e22d50fe8f0a19454a2e26749e5f758f), `[skip ci]`, before the real SHA-pinned five-grid spectral-CFL calculations. The runner independently checks the GitHub plan bytes, local ancestor relation, remote advertised HEAD and original native HDF5 SHA; missing preregistration fails closed.

This study checks whether the previously rejected physical 7-point Cartesian / true inclined roof cut-Q1 **positive row-sum mass** operator can safely use the **original native explicit leapfrog time step** instead of the unconditionally stable implicit Newmark beta=1/4, without altering any original 8node q0 and 250ms point observer conditions.

**Precisely unchanged from prior seven-point study:** full wet interior Cartesian 7point stencil, true affine Neumann inclined roof cut-Q1 stiffness, all physically positive sliver nodes, 56m³ exact volume, diagonally row-lumped mass, real native PPW28/32/36/40/44 HDF5 SHA, original 8point source and 8point receiver, native h, Ts, Nt, impulse q0, c=343.2m/s, rho=1.2kg/m³, original complete 250ms signed 40/80Hz response, all originally frozen 3 acceptance gates and strict monotonicity.

## Complete original native time CFL eigenvalue certificate

For each of the **entire** physical 3D generalized eigenbasis `K v = lambda M v` (tensor x and yz), original explicit leapfrog potential recurrence is

`phi[n+1] = (2 - Ts² lambda) phi[n] - phi[n-1]`, `phi[0]=0`, `phi[1]=Ts² c² source_mode`.

No exponential mode arises from this recurrence if and only if `0 <= Ts² lambda <= 4` (strict `<4` also excludes the defective repeated root at 4); any `Ts² lambda >4` creates an exponentially growing discrete mode. The arbitrary native point impulse does not permit dropping unstable modes to claim an original-equivalent result.

| PPW | Retained physical 3D modes | Max Ts²λ | Modes with Ts²λ≥4 | Original timestep / theoretical maximum stable timestep | Stability |
|---|---:|---:|---:|---:|---|
| 28 | 37,835 | 49.038252 | 2,717 | ≈3.5016× | **UNSTABLE** |
| 32 | 53,001 | 7.119886 | 1,542 | ≈1.3342× | **UNSTABLE** |
| 36 | 75,504 | 7.947689 | 3,581 | ≈1.4096× | **UNSTABLE** |
| 40 | 102,949 | 59.576916 | 5,289 | ≈3.8594× | **UNSTABLE** |
| 44 | 132,341 | 7.164277 | 2,906 | ≈1.3383× | **UNSTABLE** |

The ratios are `Ts / Ts_max = sqrt(Ts² lambda_max)/2`, diagnostic only; **no timestep scaling was applied**, because original native Ts is frozen. Most importantly, the severe 28/40 versus 32/36/44 grid alternation is NOT simply an unstable "greater than Nyquist" band artifact: in the prior implicit-Newmark 7point evidence, PPW32/36/44 each had **zero modes above the conventional semidiscrete native Nyquist proxy**, yet all three now have a substantial number with `Ts² lambda >=4` (between 4 and π²). The *leapfrog-stability criterion* is more restrictive than the distinct `dt sqrt(lambda) > pi` diagnostic. Never conflate them.

## Important interpretation and guard

- This proves the previously tested physically conservative and interior-exact 7point full physical cut-roof model **cannot be advanced by an unchanged native explicit leapfrog step over its complete eigenspectrum**. It does not show that the previously used implicit Newmark has an instability: the implicit experiment is numerically stable but failed original convergence and early roof weak validation on all four pairs / all 15 fixed witnesses.
- Physical small cut-cell volumes and coupled boundary mass/stiffness are plausible sources of large discrete eigenvalues, but this spectral calculation does **not** yet prove *which nodes and modes* cause instability. An independent eigenvector localization/Rayleigh quotient follow-up would be needed; do not assert the sliver-specific causal mechanism without such a check.
- Per preregistration, after computing and archiving **all five eigen spectra and CFL counts**, the program deliberately did **NOT** run a physically unstable original full 250ms leapfrog waveform or score cherry-picked subsets. Saved array `full_signed_leapfrog_40_80_if_ALL_stable` and all adjacent refinement scores remain **empty**. Original native PFFDTD remains a different discrete Neumann graph, not requalified or invalidated by this alternative solver's instability.
- Independent direct-time synthetic oscillator integration (including exact rigid zero mode) verifies full original forward/center/backward pressure conversion against closed-form signed finite-record leapfrog spectra; deliberately unstable modes are rejected, never deleted.
- The documented safe physical engineering path requires a new **prospectively frozen** cut-cell stability treatment preserving the actual Neumann roof, source/receiver duality, consistency and the entire eigenbasis. Alternatively implicit integration remains stable but requires separate spatial/physical convergence resolution. **Changing the original timestep or ignoring high modes cannot itself qualify the original q0 gate.**

## Reproducible evidence

- Frozen plan: `benchmarks/acoustics/r130d_original_q0_true_roof_sevenpoint_native_leapfrog_stability_plan_2026-10-10.json`
- Full all-five original native CFL evidence: `benchmarks/acoustics/r130d_original_q0_true_roof_sevenpoint_native_leapfrog_stability_evidence_2026-10-10.json`
- Runner `scripts/run_r130d_original_q0_true_roof_sevenpoint_native_leapfrog_stability.py`
- Native exact leapfrog spectral finite-record signed observer: `backend/src/htdt/r130d_original_q0_leapfrog_modal_observer.py`
- Independent synthetic + full original-SHA q0 CFL evidence regression: `backend/tests/test_r130d_original_q0_true_roof_native_leapfrog_cfl.py`

Strict original 8-point q0 full-record 40/80Hz gates remain complex RMS relative <=0.2, magnitude relative <=0.25, max phase <=15°, all four adjacent pairs and strict monotonicity. **Original SELF_CONVERGENCE_FAILED / independent physics NOT_VALIDATED / product NO_GO / issue OPEN / PR Draft**. No PFFDTD wave runs, GitHub Actions or `scratch/` cleaning.
