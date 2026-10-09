# R130D — original point q0, all five native grids: modal-frequency and 250ms pressure-endpoint attribution

Date: 2026-10-10 JST. Repository: `aaw123m/htdt`, migrated Issue [#53](https://github.com/aaw123m/htdt/issues/53) (old #938), original un-restored Draft PR #1055. Feature branch `feat/r130d-embedded-neumann-fv-20261009`.

**Conclusion: nonconvergence persists.** Exact decomposition of the ALREADY failed Cartesian fivepoint / physical cut-roof hybrid local full-record signed 40/80Hz scores identifies large sensitivities to (1) the native pressure conversion's final sample, and (2) several high-frequency generalized modes. This is **an algebraic contribution attribution, not a proof of unique physical cause**. No grid is excluded, no source or receiver altered, no modes cut, no special temporal window, fitted phase, level correction or acceptance relaxation.

## Prospective precommit and input provenance

- The frozen plan is `benchmarks/acoustics/r130d_original_q0_hybrid_modal_endpoint_attribution_plan_2026-10-10.json`, GitHub commit `ff0f3b96ffa27aa3dc0fc072450f93f7a8fcb4ac` (`[skip ci]`), pushed **before this five-grid experiment**. Previous local preregistration `018f329` was not remotely pushed because of suspended old credentials; migrated plan is byte-identical. The runner proves actual GitHub preregistration existence via committed plan content, local remote ancestry and live `git ls-remote`. No bypass.
- Original PFFDTD commit `aa319f6c86517cb95aabfae8656277da62c3ead5`, native SHA-pinned five original `vox_out.h5`/`comms_out.h5`, original q0 in_sigs[:,0] 8 nodes, original 8-node `out_alpha` observer, no subsequent source time samples, physical 56m³ exact cut-roof room, original native `h`, `dt`, `Nt`, beta=1/4 Newmark, complete 250ms Fourier signed 40/80Hz. Original source [1.5,2,2]m, receiver [2.5,2,2]m; c=343.2m/s, rho=1.2kg/m³.
- Original hybrid system: full wet Cartesian 5-point stiffness flux + true inclined Neumann cut-Q1 roof closure, physical positive consistent Q1 mass, x-P1 tensor, **all** generalized modes; this is experimental and **not a replacement of the original PFFDTD**.
- Raw auditable output: `benchmarks/acoustics/r130d_original_q0_hybrid_modal_endpoint_attribution_evidence_2026-10-10.json`. Program: `scripts/run_r130d_original_q0_hybrid_modal_endpoint_attribution.py`, analysis core `backend/src/htdt/r130d_original_q0_modal_endpoint_attribution.py`.

## Canonical full 250ms signed gates — unchanged

Original acceptances: complex RMS relative ≤0.20, relative magnitude ≤0.25, max phase ≤15°, at **all four** adjacent refinement pairs plus strict decrease of all metrics. Results below compare both 40 and 80Hz in the original signed physical transfer ratio.

| PPW pair | Complex RMS rel | Magnitude max rel | Phase max deg | 3 gates |
|---|---:|---:|---:|---|
| 28→32 | 0.6682444354 | 0.8974124981 | 33.56947 | FAIL |
| 32→36 | 1.3096723913 | 1.5344309064 | 174.63343 | FAIL |
| 36→40 | 0.4445570266 | 0.6205228782 | 20.18423 | FAIL |
| 40→44 | 0.1542014693 | 0.1823933841 | 7.50414 | PASS |

All three sequences are **nonmonotone** (the 32→36 degradation invalidates monotonicity). The new decomposition reconstructs each prior committed original-hybrid signed transfer with relative discrepancies from 1.2e-15 to 4.4e-15 over 5 grids. Modal band reconstruction ≤2.6e-14, pressure endpoint reconstruction ≤7.6e-16 and Nyquist split reconstruction ≤4.4e-14; all original 3D modes retained.

| PPW | All original 3D hybrid modes | Modes above native semidiscrete Nyquist proxy |
|---|---:|---:|
| 28 | 37,835 | 10,856 |
| 32 | 53,001 | 12,525 |
| 36 | 75,504 | 19,642 |
| 40 | 102,949 | 26,338 |
| 44 | 132,341 | 29,890 |

The Nyquist diagnostic uses `dt*sqrt(lambda)>pi`; these modes remain in the full original 250ms signed spectrum.

## PPW32→36 40Hz: signed projection on actual complex error

With `D = T32 − T36`, and band/endpoint complex change `d_j`, the reported scalar fraction is `Re(conj(D)*d_j)/abs(D)^2`. Signed projections sum to 1 by construction; negative contributions and values above 100% are legitimate and represent phase cancellation, **not nonnegative variance partitions or unique physical causality**.

Original signed 40Hz difference `D≈−233.27343756 +3.18059810i` (same signed normalization as frozen prior evidence).

| Original pressure record sample category | 40Hz signed fractional change |
|---|---:|
| first forward one-sided derivative, n=0 | −0.00043% |
| interior centered differences, n=1…Nt−2 | **44.2518%** |
| final backward one-sided derivative, n=Nt−1 | **55.7487%** |

This means the **last native sample, whose pressure estimate uses a second-order backward derivative**, contributes more than half of the PPW32→36 40Hz complex difference when projected along the observed error. It **does not** establish that the last sample is "wrong" or that dropping it solves the original 250ms physics; the original window and derivative must remain unchanged in authority calculations.

| All-mode physical semidiscrete eigenfrequency band | 40Hz signed contribution |
|---|---:|
| 0–100Hz | +0.2654% |
| 100–200Hz | −1.5607% |
| 200–400Hz | +10.4592% |
| 400–800Hz | +3.4526% |
| 800–1600Hz | +25.8503% |
| 1600–3200Hz | **+50.3519%** |
| 3200Hz and above | +11.1814% |
| above-native-Nyquist modes (crosscut, not an extra additive band) | **+4.3022%** |

Although the observation bin is 40Hz, its *finite, original rectangular 250ms waveform transform* contains substantial signed contribution from higher semidiscrete eigenmodes. This can reflect the broadband original impulse, numerical temporal dispersion, finite window endpoints, or combinations thereof. Above-Nyquist modes are not the dominant signed contribution for this particular comparison. Diagnosing *which* physical mechanism causes nonconvergence still needs independent tests.

## Wider sample-endpoint behavior (40Hz signed contributions)

| PPW pair | First sample | Interior | Last sample |
|---|---:|---:|---:|
| 28→32 | +0.0094% | +5.3795% | +94.6111% |
| 32→36 | ~0% | +44.2518% | +55.7487% |
| 36→40 | ~0% | −37.5911% | +137.5913% |
| 40→44 | ~0% | −192.0011% | +292.0004% |

The last nominally passing 40→44 pair features large offsetting endpoint terms, so one passing pair cannot validate the full original q0 convergence. The same report includes **80Hz and all seven bands and all original grid cases without selection**.

## Verification, limitations, and next numerical direction

- Independent synthetic direct-sampled physical potential → original pressure conversion → rectangular signed Fourier proof compared with exact all-mode Newmark closed form. Finite-record first/interior/last terms match to strict tolerances.
- Five original SHA-pinned HDF5 experiments actually completed. Full spectra checked against the precommitted hybrid. Seven bands, three pressure sample partitions, two diagnostic Nyquist partitions and four signed error projections reconstruct exactly. Evidence-specific regression protects all results and release status.
- This is an **experimental hybrid**, not actual PFFDTD wave re-execution, not convergence correction, and not independent acoustic physics validation. Small physical-roof Neumann manufacturing residuals and early roof-echo diagnostics from previous experiment do not override these failure gates.
- A *next* separately preregistered, physical diagnostic should investigate finite-record end-of-record high-mode boundary/observer sensitivity without modifying q0, sample count, source, receiver, observation frequency, or original acceptance thresholds. Alternate observation methods can be studied as **diagnostics only**; they must never be substituted for canonical original q0 authority without an independent, precommitted physical argument and all-gate requalification.

**Original PFFDTD = SELF_CONVERGENCE_FAILED. Independent physics = NOT_VALIDATED. Product = NO_GO. Migrated issue #53 stays OPEN; replacement PR stays Draft.** No new PFFDTD waves, no GitHub Actions triggered, no `scratch/` deletion.
