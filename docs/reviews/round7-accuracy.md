# Round 7 — acoustic/numerical accuracy depth

Scope: solver & measurement math quality — interpolation/resampling,
grid/resolution defaults vs physics, cross-path consistency, measurement
math, missing sanity bounds — per the round-7 brief. Prior rounds 1–6 were
read first (esp. `round4-numerics`, `round6-perf`); their findings are not
re-reported. Branch `devin/rev7-accuracy`. All verification is local
(`pytest -q -n 4` under Python 3.12.7, full Windows dependency lock).

## Verdict table

| # | Finding | Severity | Verdict |
|---|---------|----------|---------|
| 1 | IR early-reflection `delay_s` anchored to window start, not the declared `time_zero_sample` | HIGH accuracy | FIXED |
| 2 | FIR materialization resamples taps with `np.interp` — no anti-aliasing on decimation, sinc² droop (~−8 dB at new Nyquist) on upsample | HIGH accuracy | FIXED (`htdt.fft_bandlimited_resample_v1`) |
| 3 | `time_reference_sample` not rescaled on resample — pre/post-ringing split lands at the wrong physical time | MED accuracy | FIXED (index scales with rate ratio) |
| 4 | PFFDTD `points_per_wavelength` accepts any positive float — PPW < 2 is below the spatial Nyquist bound and silently produces non-physical grids | MED accuracy | FIXED (`Field(ge=2.0)`) |
| 5 | `compute_minimum_phase_deg` returns `atan2(Im, Re)` of log H = arg(log H), not `Im` = φ_min | HIGH accuracy | DEFERRED — see below |
| 6 | `_point_in_triangle` dimensional tolerance (m⁴ vs m) | — | STILL STANDING (round4 item A, round6 item 12) — needs its own equivalence pass |
| 7 | `frequency_samples_hz` spacing below `1/duration_s` is correlated DTFT samples — not wrong, just not independent | LOW | DEFERRED — would need a warnings channel, not a bound |
| 8 | Estimated RT60 (EDT/T20/T30) has no upper bound — a near-flat decay curve can report implausibly large values | LOW | DEFERRED — see below |

## 1 — IR reflection markers anchored to window start (accuracy, fixed)

`cad_ir_analysis.py` documents `IRReflectionMarker.delay_s` as "relative to
the direct-arrival sample", and the energy metrics in the same function
already anchor to the declared time-zero via `t0_index = t0 - start`. The
marker loop instead hard-coded `direct_index = 0` and reported
`delay_s = times[i] - times[0]` = `i/fs` — the position in the analysis
window, not the delay after the direct arrival.

For a spec whose window starts at or before the declared time-zero
(`window_start_s=0`, `time_zero_sample>0` — the normal shape of a
measurement record with pre-roll), every marker delay was inflated by
`(t0 - start)/fs` and peaks *before* the direct arrival (pre-transients,
stimulus bleed) were reported as reflections. Verified live: direct at
t0=10 ms with a reflection 3 ms later reported `delay_s = 13 ms`.

Fix: `direct_index = t0 - start` (the same `t0_index` the energy section
already computes — now computed once before markers and reused), markers
require `i > t0_index`, `delay_s = (i - t0_index)/fs`. `time_s` stays
window-relative, consistent with `etc_time_s`. Behavior is unchanged when
`time_zero_sample` equals the window start — the default spec — so all
previously-valid results replay identically.
`IR_ANALYSIS_ALGORITHM_IDENTITY`/`_SHA256` deliberately not bumped, per the
round-6 precedent: the fix aligns implementation with the documented
contract, and stored pre-fix results stay loadable (replay fails closed on
them only where marker bytes differ — intended).

Regression: `test_markers_delay_anchored_to_time_zero` — a record with a
pre-t0 transient, a direct at 10 ms and a 0.5-amplitude reflection 3 ms
later must report exactly one marker class eligible: the reflection reads
delay ≈ 3 ms (never ~13 ms), time_s ≈ its window position, level ≈ −6 dB,
and no marker precedes t0.

## 2 — FIR materialization resample was linear interpolation (accuracy, fixed)

`cad_fir_filter._resample_taps` resampled the impulse response with
`np.interp` — linear interpolation with no anti-aliasing:

- **Decimation folds out-of-band content into the passband.** An 18 kHz
  component in 48 kHz taps materialized to 24 kHz aliases to ~6 kHz; the
  exported filter then *adds* correction energy at a band the source
  filter never had. Verified live under the old code path.
- **Upsampling droops** by the linear-interpolator frequency response —
  sinc² reaching ≈ −7.9 dB at the new Nyquist — silently dulling the top
  octave of any resampled filter.

`max_response_error_db` on the materialization report made the damage
measurable but never prevented it — it was a diagnostic on top of a wrong
kernel, and a tone at the wrong frequency is not a "response error" the
in-band grid reliably bounds.

Fix: reuse the repo's existing audited resampler
`htdt.fft_bandlimited_resample_v1` (`cad_auralization._resample_band_limited`,
already test-verified end-to-end: in-band tone preserved, out-of-band tone
dropped, time origin pinned). Brick-wall anti-aliasing on downsample, sinc
reconstruction on upsample, duration-preserving `n_out = round(n_in·r)`,
amplitude preserved by the `n_out/n_in` normalization. Same convention
(periodic edges, no tapering) — and now one resample authority serves both
auralization and FIR export. `FIR_MATERIALIZATION_AUTHORITY_VERSION` bumped
`fir10-fir-materialization-1` → `-2`: the produced tap vector legitimately
changes, and provenance must say which resampler made it.

Regression: `test_materialization_resample_is_band_limited` — 18 kHz taps
materialized 48k→24k must come out silent (spectrum max < 1e-6); the
in-band control (6 kHz) survives with amplitude preserved
(`spec[peak_bin] ≈ n_out/2`).

## 3 — `time_reference_sample` did not scale with resample (accuracy, fixed)

`materialize_fir_artifact` carried `min(time_reference_sample, len-1)`
straight into the new tap vector. The reference marks a physical instant
(the t=0 split used by `pre_ringing_energy` / `post_ringing_energy` /
`pre_event_peak_amplitude` in `evaluate_fir_artifact`), so its *index* must
scale with the resample ratio. For 48k→96k a reference at index 2 of 5
taps stayed 2 instead of becoming 4 — the pre/post split then measures
ringing against the wrong instant (post-ringing energy absorbs the first
half of what is physically pre-event). On decimation it clamped to the
tail, which reports essentially zero post-ringing.

Fix: `reference = min(round(ref · target_rate / source_rate), len(taps)-1)`.

Regression: `test_materialization_rescales_time_reference_sample` — the
5-tap symmetric fixture (ref 2 @48k) must land at ref 4 at 96k and ref 1
at 24k.

## 4 — PFFDTD `points_per_wavelength` had no physics floor (accuracy, fixed)

`PffdtdCandidateConfiguration.points_per_wavelength` was `Field(gt=0.0)`.
Grid spacing is `c / (fmax · PPW)`; at PPW < 2 a Cartesian cell is larger
than half a wavelength at `fmax` — the spatial Nyquist bound — so the
solver grid cannot represent the wave it is asked to propagate. That is
not "inaccurate", it is non-physical, yet the configuration validated,
planned, and would consume a full solver run returning numbers with no
wave content. (Every in-repo caller and the R130D convergence sweep uses
PPW 8–12; nothing legitimate sits near the bound.)

Fix: `Field(ge=2.0)` — fail closed at the spec, matching the codebase's
bounds-for-impossibility convention. Practical guidance stays the existing
8–12 sweep; the bound only rejects configurations that cannot represent
the physics at all.

Regression: `test_candidate_configuration_rejects_sub_nyquist_ppw` —
PPW 1.99 raises `ValidationError`, PPW 2.0 builds.

## 5 — `compute_minimum_phase_deg` extracts the wrong quantity (accuracy, DEFERRED)

In `cad_phase_time_analysis.compute_minimum_phase_deg` the cepstral
pipeline is textbook up to the last line: real cepstrum of the log
magnitude, minimum-phase lifter `[c0, 2c(1..N/2), c(N/2), 0…]`, forward DFT
→ `spectrum = log H_min = log|H| + j·φ_min`. The correct minimum phase is
`spectrum.imag`. The code instead returns
`atan2(spectrum.imag, spectrum.real)` — the argument of the complex number
`log H`, which is not a phase of `H` at all.

Demonstrable counter-example: for a flat magnitude of −3 dB, `spectrum`
is a negative real constant, so `atan2(0, negative)` reports a minimum
phase of **+180°** — the true minimum phase of a constant response is 0°.
The existing golden test `test_min_phase_matches_min_phase_system` passes
today only incidentally: the test subtracts `derived[0] − true_phase[0]`
(a DC-normalization offset), and for a decaying low-pass `arg(log H)`
happens to land within the loose 15°/5° tolerances mid-band.

Why deferred, not swapped: the mathematically correct `spectrum.imag`
extraction **fails the existing golden test by ~35°** on its one-pole
fixture — because the reference implementation's own truncation bias on a
finite 0–10 kHz band (the mirrored two-sided extension is not the true
continuation of a still-decaying response) limits accuracy well inside the
current tolerances. Fixing this properly is a redesign of the estimator
(longer/zero-padded cepstrum, or a different homomorphic windowing), plus
a re-derivation of the golden test's expected values from first
principles — a dedicated equivalence pass, exactly the pattern round 4 set
for `_point_in_triangle`. Swapping the line without that pass would
replace a passing incidentally-wrong result with a failing correctly-wrong
one — motion without improvement. Flagged HIGH because the shipped number
is mislabeled physics.

## Areas audited clean

- **`comparison.py`** (`COMPARISON_ALGORITHM_IDENTITY` = log2-linear
  interpolation): `_interpolate_prepared` verified bit-identical to the
  mathematical definition; `_validate_response` enforces finite/positive/
  length-matched inputs; extrapolation beyond the evidence grid is
  forbidden — round-4 state holds.
- **`acoustics.py`**: mirror-source reflection timing, mode frequencies
  `c/2·√((nx/lx)²+…)`, bounds checks on geometry — consistent formulas on
  the GA path.
- **Hybrid composition** (`cad_hybrid_numerical_composition`,
  `cad_hybrid_grid_reconciliation`): complementary-weight complex blend
  re-validated by model validators; Cartesian complex interpolation only;
  phase interpolation explicitly unsupported rather than faked; exact-bin
  mode verified; extrapolation forbidden; phasor-convention conjugation
  correct — the two-path consistency the brief asked about holds because
  the blend operates on complex pressure, not on derived scalars.
- **REW import** (`rew_parser.py`): strictly-increasing frequency axis,
  finite-only, consistent column counts; level semantics stay relative —
  absolute-SPL claims are only possible through a separately-bound
  calibration authority (`cad_ambient_noise` `evaluate_ambient_criterion`
  refuses absolute verdicts on uncalibrated profiles — verified in code).
- **`cad_measurement_ir.py`**: uniform-sample-spacing verification
  (|Δ−step| ≤ 1e-9·max(1,|step|)), normalization/window/calibration must
  be explicitly declared, rederive-on-read verification.
- **`measurement_analysis.smoothed_level_trace`**: ±1/(2·fraction) octave
  power-mean — correct dB-domain averaging; the window always contains
  its center sample so no empty-window degenerate.
- **Decay-metric sanity**: `_fit_slope` already returns `None` for
  `slope >= 0` (non-decaying record → `unknown`, never a negative RT60),
  and noise-floor gating blocks fits without usable dynamic range.
- **`cad_measured_modal_analysis`**: decay_rate/decay_time consistency via
  `rate = ln(10³)/T60`; normalized shapes bounded ≤ 1; associations
  require spatial evidence.
- **`cad_external_calibration`**: Equalizer APO import normalizes only
  documented filter forms; unknown commands surface as opaque sections
  rather than being guessed.

## 7/8 — smaller deferred notes

- **Sub-resolution frequency samples**: `PffdtdCandidateConfiguration.
  frequency_samples_hz` may be spaced below `1/duration_s`; the DTFT
  produces smooth, mutually-correlated samples — correct values, but
  presenting them as independent spectral evidence would be overreach.
  A warning (not a rejection) is the right instrument; the model has no
  warnings channel, so it is documented rather than implemented.
- **Estimated-RT60 ceiling**: with the interval-coverage and noise-floor
  gates, a reported T20/T30 requires the decay curve to actually span the
  fit interval within the record — implausible values are mostly
  unreachable already. A hard cap (e.g. the brief's `RT60 > 100 s` example)
  is a product-threshold decision, not a mathematical bound — recommend a
  declared `implausibility` flag rather than a silent rejection if adopted.

## Files changed

- `backend/src/htdt/cad_ir_analysis.py` — marker anchor to declared t0
- `backend/src/htdt/cad_fir_filter.py` — band-limited resample, rescaled
  reference index, `fir10-fir-materialization-2`
- `backend/src/htdt/cad_candidate_wave_execution.py` — PPW ≥ 2 bound
- `backend/tests/test_cad_ir_analysis.py` — `test_markers_delay_anchored_to_time_zero`
- `backend/tests/test_cad_fir_filter.py` — `test_materialization_resample_is_band_limited`,
  `test_materialization_rescales_time_reference_sample`
- `backend/tests/test_cad_candidate_wave_execution.py` — `test_candidate_configuration_rejects_sub_nyquist_ppw`
- `docs/reviews/round7-accuracy.md` — this report

## Tests

- Scoped: `test_cad_ir_analysis.py test_cad_fir_filter.py
  test_cad_candidate_wave_execution.py test_issue_971_phase_time.py
  test_cad_auralization.py` — 56 passed.
- Full: `cd backend && TMPDIR=/c/t C:/devin/python/python.exe -m pytest -q -n 4`
  — 5303 tests: all green except two, neither related to this branch:
  - `test_save_focused_editor.py::test_ctrl_s_commits_focused_inspector_position`
    — `AttributeError: 'WorkflowApplicationComposition' object has no
    attribute 'preferences'` in `workflow_application.py._make_room`;
    verified failing identically on clean `main` (UI workspace wiring,
    pre-existing).
  - `test_cad_hybrid_prediction_provider.py::
    test_evidence_lifecycle_rejects_illegal_promotions` — failed once
    under `-n 4`, passed on isolated rerun (ordering/timing flake under
    parallel load; no overlap with this branch's modules).

Environment note: no Python was present on the box; the official installer
stalled in silent mode, so Python 3.12.7 was provisioned from the NuGet
`python` package into `C:\devin\python` and the full dependency lock
(`requirements-n05-windows.lock`, `--require-hashes`) plus dev extras were
installed under it.
