# R100B explicit impedance / reflection gate

Issue #101 / implementation slice tracked by #148.

## Scope

This slice evaluates the frozen R100A-2 `wave-normal-incidence-impedance-v1` fixture against the already-listed PFFDTD candidate. It does not replay the merged PR #116 complex-pressure convergence fixture and it does not select a production solver.

The R100A-2 manifest remains unchanged.

## Frozen fixture authority

The probe consumes the canonical manifest without solver-side edits:

- fixture: `wave-normal-incidence-impedance-v1`
- room: 6 x 4 x 2.5 m, with only the x-max face using `b-impedance`
- material: `z-2z0`, explicit specific impedance table at 100 / 200 / 300 Hz
- physical impedance: 823.2 + 0j Pa*s/m at every declared frequency
- density: 1.2 kg/m3
- sound speed: 343 m/s
- source: (4, 2, 1) m, unit `volume_velocity_m3_s`, phase 0 degrees, omnidirectional
- receiver: (3, 2, 1) m, ideal flat, `source_t0` timing reference
- Fourier sign: `exp(-i*omega*t)`
- interpolation: `linear_complex`
- window/filter: none
- expected reflection: `R = 1/3 + 0j`
- frozen tolerances: absolute 0.01, relative 0.02, phase 1 degree

No scalar absorption coefficient participates in this mapping.

## PFFDTD adapter boundary

Pinned PFFDTD represents wall materials as normalized specific-admittance DEF branches. For the canonical fixture only,

`rho*c = 1.2 * 343 = 411.6 Pa*s/m`

and therefore

`Zn = Z/(rho*c) = 823.2/411.6 = 2`.

PFFDTD's exact frequency-independent branch representation is therefore `DEF=[0, Zn, 0]`, i.e. `[0, 2, 0]`. The normalized admittance is 0.5.

`acoustic_pffdtd_impedance_adapter.compile_impedance_fixture_boundary()` accepts this exact, frequency-independent, purely resistive subset and refuses two cases rather than inventing an authority:

1. non-zero reactance, because an exact table-to-DEF fitting policy has not been frozen;
2. frequency-varying resistance, for the same reason.

The refusal is intentional capability-gap evidence. This slice does not use PFFDTD's scalar-absorption fitting helpers.

`acoustic_pffdtd_impedance_adapter.compile_impedance_fixture_model()` separately maps the canonical rigid faces and impedance face to PFFDTD material groups while preserving source and receiver locations.

## Raw-result and evaluator boundary

The dedicated probe checks out exact PFFDTD commit
`aa319f6c86517cb95aabfae8656277da62c3ead5`.

It asks pinned upstream to write the normalized material with
`write_freq_ind_mat_from_Zn()`, verifies the resulting HDF5 `DEF` dataset is byte-value-equivalent to the adapter output, and calls pinned upstream
`materials.adm_funcs.compute_Rf_from_DEF()` on the exact R100A frequency samples.

The adapter emits the returned complex reflection values as
`RawFixtureObservation` / `RawObservableObservation`. It does not emit PASS/FAIL. Existing central `evaluate_sampled_fixture()` performs exact identity/frequency checks and computes complex absolute, relative and wrapped-phase error against R100A.

The artifact also records `|R|` and phase in degrees per sample, resource evidence, PFFDTD source commit, HTDT source commit, dependency versions, adapter/probe hashes, material/model hashes, and the whole R100A semantic hash.

## PASS / FAIL / BLOCKED semantics

The probe process exits successfully after producing a valid evidence artifact even when the fixture result is FAIL or BLOCKED. GitHub Actions success means the evidence workflow completed; it does not mean the solver candidate passed.

- PASS: upstream produced raw reflection samples and the central evaluator accepted all frozen tolerances.
- FAIL: upstream produced valid raw samples but central R100A comparison failed.
- BLOCKED: exact source/adapter/runtime capability could not produce the required raw samples.

No tolerance is changed in response to candidate output.

## Deliberate non-claims

This gate is a native PFFDTD boundary-representation/reflection-function probe. It does **not** claim that a full spatial FDTD run has independently recovered the reflection coefficient from incident/reflected pressure decomposition. If that end-to-end propagation check is required for final R100B solver selection, it remains a separate bounded gate.

It also does not:

- authorize arbitrary reactive/frequency-dependent impedance fitting;
- authorize conversion from absorption to complex impedance;
- complete Windows product packaging;
- close candidate-wide physics/capability hard gates;
- select PFFDTD for production;
- implement R110 or later production acoustic authority.

## Verification

Focused validation is performed by:

- `backend/tests/test_acoustic_pffdtd_impedance_adapter.py`
- `.github/workflows/r100b-pffdtd-impedance.yml`
- `scripts/run_r100b_pffdtd_impedance.py`

The numerical PASS/FAIL/BLOCKED result belongs to the uploaded
`r100b-pffdtd-impedance-reflection` artifact and the corresponding GitHub issue/PR evidence record.
