# Issue #101 / R130C causal frequency-dependent boundary — 2026-09-20

## Scope

R130C extends the existing R130A/R130B candidate wave vertical slice without changing its exact-authority chain:

`AcousticSceneSnapshot surface binding -> exact boundary authority -> deterministic PFFDTD compilation -> bounded CPU execution -> immutable complex-pressure artifact/result envelope`.

This slice adds an explicit causal frequency-dependent boundary representation and actual pinned PFFDTD execution. It does **not** select a production solver, add GPU execution, integrate R170, perform general material fitting/identification, or establish owned-room validity.

## Exact boundary authority

The source authority is `CausalFrequencyDependentBoundaryAuthority` in `backend/src/htdt/acoustic_pffdtd_causal_boundary.py`.

It is a versioned analytic rational authority, not a bag of frequency samples:

- physical quantity: specific acoustic admittance, `m/(Pa*s)`;
- normalization: `Yn = rho*c*Y_specific`;
- representation: parallel branches whose normalized branch impedance is `Zn_k(s) = D_k*s + E_k + F_k/s`;
- total normalized admittance: `Yn(s) = sum_k 1/Zn_k(s)`;
- exact SceneRevision content hash, semantic surface id, material id/version, provenance and optional uncertainty;
- explicit valid frequency domain;
- analytic evaluation semantics; interpolation is not used;
- extrapolation is forbidden;
- semantic SHA-256 and content-derived authority id.

R130C requires at least one dynamic term (`D>0` or `F>0`). Frequency-independent purely resistive input remains R130B authority rather than being silently reclassified.

Measured/manufacturer/fitted/analytic evidence states are explicit. A fitted authority must additionally retain source-data hash, fitting algorithm/version, order, error metric, valid band, passivity-correction state, residual and residual unit. R130C itself does not introduce a black-box fitting algorithm.

Scalar absorption coefficients are never converted into complex impedance/admittance.

## Causality, passivity and stability

The accepted solver input is structurally constrained to positive-real series-RLC branches:

- `D >= 0`;
- `E > 0`;
- `F >= 0`;
- all coefficients finite.

For this bounded representation, the contract is recorded as `CAUSAL / PASSIVE / STABLE`. Compilation additionally evaluates the requested valid band and rejects any normal-incidence reflection sample with `|R| > 1`, where `R=(1-Yn)/(1+Yn)`.

This is not an IFFT of sampled response data. PFFDTD consumes the same `DEF` coefficients through its native recursive time-domain boundary state.

Unsupported, malformed, out-of-band, non-passive, unstable, wrong-quantity, missing-authority, stale SceneRevision/surface, or unsupported mapping input fails closed. There is no fallback to rigid or R130B frequency-independent impedance.

## PFFDTD deterministic compilation

The pinned candidate remains `bsxfun/pffdtd@aa319f6c86517cb95aabfae8656277da62c3ead5`.

The R130C mapping authority is separately versioned as:

`htdt.pffdtd.causal_frequency_dependent_normalized_admittance_def@1`.

Compilation produces `PffdtdCausalBoundaryCompilation`, whose semantic hash is intentionally distinct from the source boundary authority hash. It records:

- source boundary authority ref;
- exact SceneRevision/content/surface binding;
- mapping authority ref;
- valid/requested frequency domains;
- exact density and sound-speed refs;
- `rho*c`;
- solver `DEF` coefficients;
- complex normalized admittance, physical admittance and physical impedance samples;
- provenance/uncertainty and causal/passive/stable states.

The shared R130A/R130B executor remains backward compatible. Rigid surfaces still use the rigid path; R130B still uses `write_freq_ind_mat_from_Zn`; R130C uses PFFDTD `write_freq_dep_mat`. Packaged `DEF` is compared byte-for-value after PFFDTD material packaging, and the mapped material must own at least one active boundary node.

## Independent reference and bounded numerical evidence

`scripts/run_r130c_candidate_causal_boundary_execution.py` uses a bounded analytic positive-real one-branch fixture over 40–80 Hz:

- `D = 8.0e-4 s`;
- `E = 1.5`;
- `F = 120 s^-1`.

The runner independently evaluates `Yn(jw)` and normal-incidence `R=(1-Yn)/(1+Yn)`, then compares complex reflection magnitude and phase against pinned PFFDTD `compute_Rf_from_DEF`. It also checks the dense valid band for `|R| <= 1`, verifies that magnitude and phase are genuinely frequency-dependent, and rejects deliberately invalid passive/stability coefficients and out-of-band evaluation.

The same fixture is then executed through the real PFFDTD Python/Numba CPU backend to produce the immutable complex-pressure artifact. Solver execution evidence is intentionally distinct from physics-reference acceptance.

## Resource estimate and persistence

The existing R140 PFFDTD solver-specific estimator is conservatively extended to mixed rigid/non-rigid material groups. For non-rigid work it adds the pinned `MMb=12` lossy-boundary state upper bound and material-HDF5 scratch reserve while preserving the existing rigid estimate path.

R130C execution provenance stores the exact workload-estimate ref. The workload binds candidate execution input id/hash, solver model hash, solver implementation/configuration, grid/Nt and bounded resource quantities.

The result remains an immutable `AcousticSolverResultEnvelope` bound to exact snapshot/request/dispatch identity. Artifact provenance stores the material-boundary configuration hash, exact R130C source authority, compiled boundary hash, PFFDTD material asset hash, backend/configuration, resource estimate, raw solver asset hash and complex-pressure artifact hash. Save/reopen re-resolves those exact authorities; missing or modified artifacts fail closed.

A boundary-authority change changes the compiled boundary hash and candidate execution identity. Therefore a historical result remains reproducible as history but is not current/reusable for the changed boundary identity.

## Focused verification

Focused tests cover:

- deterministic R130C authority identity;
- explicit fit-audit requirements;
- causal/passive/stable structural rejection;
- valid-band enforcement/no extrapolation;
- deterministic source-authority -> compiled-boundary identity;
- source/compiled hash separation;
- boundary-change invalidation of compiled/input identity;
- existing R130B impedance tests and shared candidate-executor regression;
- mixed-boundary R140 resource estimation;
- actual pinned PFFDTD CPU execution;
- immutable artifact/provenance save/reopen and tamper rejection.

Dedicated workflow: `.github/workflows/r130c-candidate-causal-boundary-execution.yml`.

## Supported / unsupported

Supported in this slice: bounded single-region mixed rigid + explicit causal frequency-dependent specific-admittance `DEF` boundary on the pinned CPU candidate, with an explicit 40–80 Hz fixture and exact provenance.

Not claimed: production solver adoption, GPU backend or CPU/GPU equivalence, R170 integration, general measured-material identification/fitting, scattering/diffraction, spatial incident/reflected decomposition, broadband production validity, or R180/owned-room validation.

RDC usage for this implementation: **0**.
