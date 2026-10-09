# R130D original q0 hybrid modal / record-endpoint attribution — preregistered, awaiting real five-grid results

2026-10-10 JST. Migrated repository: `aaw123m/htdt`. Migrated R130D issue: [#53](https://github.com/aaw123m/htdt/issues/53) (old #938); old Draft PR #1055 metadata was not restored.

## Chronology: preregistration and access recovery

Originally the frozen plan was authored as local commit `018f32932f2a3105d8c5864cea644cc2925eafed` (before any new real five-grid experiment). The suspended old-account Git credentials denied `git push` (HTTP 403); the numerical experiment was **not** executed while the plan was remote-unavailable. After migrating to `aaw123m/htdt` and reauthorizing its GitHub app installation, the **identical content** was uploaded through GitHub's contents API on the same feature branch as commit **`ff0f3b96ffa27aa3dc0fc072450f93f7a8fcb4ac`** (`[skip ci]`). The runner's remote preregistration guard is pinned to that actual reachable GitHub commit. There is no permission to bypass this guard, no changed scientific plan, and no post hoc choice of frequency bands. The real five-grid modal/endpoints attribution results will be committed separately after the run.

## Fixed algorithm and source contract

- `backend/src/htdt/r130d_original_q0_modal_endpoint_attribution.py`: EXACT original beta=1/4 Newmark pressure P_T/Q_T for ALL 3D hybrid modes on the native full 250ms record, original 8 source / 8 receiver weights. Disjoint semidiscrete frequency bands [0,100), [100,200), [200,400), [400,800), [800,1600), [1600,3200), [3200,infinity) Hz. Never truncate high modes. Diagnostic native below/above Nyquist also retained in full sum.
- Pressure Fourier contribution of the FIRST second-order forward sample n=0, center n=1..N−2, LAST second-order backward sample n=N−1. The three components must algebraically reconstruct unchanged original full 250ms complex 40/80Hz spectra. No tapered window, smoothing, fitted time/phase/level, or other new canonical transfer.
- Pairwise complex delta is decomposed by the signed `Re(conj(full_difference)*component_difference)/abs(full_difference)**2`. Fractions MUST sum to 1 at each 40 and 80Hz, and can be negative or >1; this is not independently a causal explanation.
- `scripts/run_r130d_original_q0_hybrid_modal_endpoint_attribution.py`: SHA-verifies each original HDF5 and native clock, reproduces exact existing five-grid hybrid signed spectra within 2e-8 and all four frozen three-gate results, includes all bands/endpoints and Nyquist parts, refuses to run unless `ff0f3b9` is ancestor of live original feature GitHub branch and plan bytes match.
- `backend/tests/test_r130d_original_q0_modal_endpoint_attribution.py`: independent small-mode directly time-sampled finite-record Fourier, endpoint and band partition, signed projections, frozen-plan drift rejection and strict remote prerequisite gate.
- On the earlier pre-migration locally committed implementation, all 65 R130D modules / **673 tests PASS** with `pytest --noconftest` (numerical venv lacks unrelated GUI PySide6 conftest). The previous guard test correctly failed closed when remote preregistration was unavailable.

## Previously established baseline (unchanged, *not* newly requalified)

Experimental Cartesian + true cut roof hybrid, original 250ms q0 all modes: 28→32, 32→36 and 36→40 **FAIL** the frozen three gates; only 40→44 **PASS**. PPW32→36 40Hz exhibits a 174.63° phase change. First-roof independent weak diagnostics improve most cases yet cannot establish full 250ms physical convergence. Prior source is GitHub commit `f877413`.

## Next

1. Verify GitHub `ff0f3b9` preregistration live and run the already specified all-five actual original-HDF5 PPWs. Keep all signed 40/80Hz values, four adjacent pairs, frozen three gates and strict monotonicity.
2. Record absolute and signed-band, endpoint, and Nyquist reconstructions, attribute PPW32→36 40Hz coherent cancellation without changing full all-mode signal.
3. Commit the complete evidence and any execution fixes with `[skip ci]`, update migrated Issue #53; preserve `scratch/` and no extra GitHub Actions.

Independently of this diagnostic, the **original PFFDTD remains SELF_CONVERGENCE_FAILED**, independent physics **NOT_VALIDATED**, product **NO_GO**. Issue stays open and any replacement PR stays Draft until independent acceptance.
