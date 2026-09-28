# Round 9 — Numerical & Acoustic Accuracy Depth

Scope: the math layer under the UI — unit conversions end-to-end, dB families,
frequency grids and interpolation, smoothing windows, RT60/decay estimators,
IACC/lateral metrics, windowing, FFT scaling, coordinate/angle conventions,
resampling, calibration chain math, optimizer objective normalization, numeric
edge handling, and the previously-deferred minimum-phase estimator bias.

Method: every candidate defect was reproduced numerically against an analytic
reference before being counted as a finding.

## Verdict table

| # | Area | Severity | Verdict | Status |
|---|------|----------|---------|--------|
| 1 | `compute_minimum_phase_deg` extracted `atan2(Im, Re)` of the *log-spectrum* | **High** | Defect — reports `arg(log H)` instead of `Im(log H)`; a flat −3 dB input read ±180° | **Fixed** |
| 2 | `reconstruct_mode_shape` paired residue amplitudes with caller positions *positionally* | **High** | Defect — residues keyed by `position_id` were bound to anonymous `Position3` coords in caller order; any ordering slip silently mislabels a measured mode shape | **Fixed** |
| 3 | `cad_correction_design_policy._interpolate_db` interpolated target levels linear in Hz | **Medium** | Spec inconsistency — the canonical comparison authority pins `linear_in_log2_frequency`; residuals at mid-segment frequencies were biased (e.g. −3.4 dB vs −5.3 dB at the log-midpoint of a 20→160 Hz segment) | **Fixed** |
| 4 | `cad_correction_design_policy._fractional_octave_smooth` used arithmetic dB mean | **Medium** | Spec inconsistency — the declared smoothing authority is `fractional-octave-power-mean-1`; a −20 dB dip inside a window read −6.7 dB instead of the power-mean −1.7 dB, systematically exaggerating dips in correction evidence | **Fixed** |
| 5 | `analyze_minimum_excess_phase` ignored declared `lf_extension`/`hf_extension` | **Low** | Defect — `htdt_min_phase_v1` implements a flat tail extension; a spec declaring `linear_slope`/`producer` tails silently got flat semantics | **Fixed** (fail-closed gate) |
| 6 | `run_ir_analysis` crashed on windows that round to zero samples | **Low** | Defect — `window_start_s/window_end_s` pass the spec validator yet produce an empty `windowed` array → raw `numpy` "Invalid number of FFT data points (0)" instead of a domain ValueError | **Fixed** |
| 7 | `run_ir_analysis` crashed on silent / non-finite IR evidence | **Medium** | Defect — a zero-energy IR makes the ETC/Schroeder normalizations divide by a zero peak/total → NaN propagates through `np.maximum` floors into `identity_payload` → `canonical_sha256` raises `ValueError: Out of range float values are not JSON compliant: nan` at sealing. A muted-channel capture is a routine input; the module's own contract is fail-closed 'unknown', not crash | **Fixed** |
| 8 | `_point_in_triangle` m⁴-vs-m tolerance (standing, rounds 4 & 7) | Medium | Still deferred — barycentric predicate mixing area and area² magnitudes; needs a dedicated equivalence pass over projected-area scales | **Deferred (standing)** |
| 9 | `cad_units` single `level` family for all dB quantities (round 6 D2) | Low | Documented design trade-off; not re-reported | **Deferred (standing)** |

## Finding 1 — minimum-phase estimator extracted the wrong quantity (HIGH)

`compute_minimum_phase_deg` (backend/src/htdt/cad_phase_time_analysis.py)
builds the liftered spectrum `spectrum = DFT(liftered_cepstrum)`, which is the
complex log-spectrum `log H_min = log|H| + j·φ_min`. The minimum phase is the
**imaginary part**. The code instead returned `atan2(spectrum.imag,
spectrum.real)` — the *argument* of `log H_min`, which conflates the
log-magnitude real part into the phase.

Reproduction (two-zero discrete-time minimum-phase system,
`H(z) = (1 + 0.8·z⁻¹)(1 − 0.55·z⁻¹)`, uniform DC→24 kHz grid, N=257):

| extraction | max abs error vs analytic phase | flat −3 dB input |
|---|---|---|
| `spectrum.imag` (fixed) | **0.000000°** | 0° (1.6e-12 residual) |
| `atan2(imag, real)` (old) | **180.0°** | ±180° |

On the continuous one-pole fixture the old golden used, the old code's error
was −65° at 1 kHz (after its −85° DC offset) and ±180° at band edges; the
existing test masked all of it by subtracting `derived[0] − true[0]` and
asserting loose 15°/5° tolerances at two points.

**Important caveat now honestly documented:** the mirrored even extension is a
*flat* tail policy. On a magnitude still rolling off at the grid edge (the old
continuous one-pole fixture), the corrected estimator carries an inherent
truncation bias — +6.8° at 1 kHz, +35° at 5 kHz, +87° at 9.9 kHz for an
fc=100 Hz pole truncated at 10 kHz. That residual is the method's documented
limitation (finite-span tails), not an implementation error: on inputs that
respect the discrete-time domain the method is exact. The new golden test uses
a discrete-time reference (zeros inside the unit circle) where cepstral tail
truncation scales like `a^(2N)` — measurably exact, not approximately.

`HTDT_MIN_PHASE_ALGORITHM` stays `htdt_min_phase_v1` and no authority hash was
bumped: this is a pure math fix. **Persisted-computation impact:**
`minimum_phase_deg`/`excess_phase_deg` trace values change for any previously
computed result — those traces were numerically wrong before; spec identity is
unchanged, so a sealed spec replays to the corrected values.

## Finding 2 — reconstructed mode shape bound amplitudes to the wrong positions (HIGH)

`reconstruct_mode_shape` (backend/src/htdt/cad_measured_modal_analysis.py)
normalized `mode.residues` in residue order — residues keyed by
`position_id` — then stored them next to a caller-supplied
`evaluation_positions` tuple of `Position3` *coordinates*, which carry no
identity. Only array length was validated. A caller listing positions in any
order other than the residue insertion order silently produced a mode shape
that attributes each measured amplitude to the wrong physical position —
exactly the fabricated-evidence scenario the module's "reconstructed, never
measured at unmeasured locations" contract exists to prevent.

Fix: the function now requires `evaluation_position_ids` parallel to
`evaluation_positions`; the id set must equal the residue position set, and
normalized values are emitted in evaluation order by id lookup. Order swaps
produce correctly reordered values; missing/extra ids fail closed.

## Finding 3 — target-curve interpolation linear in Hz (MEDIUM)

`_interpolate_db` evaluated `CadTargetCurve` level at arbitrary frequencies by
linear-in-Hz interpolation, while `comparison.py` — the pinned comparison
authority (`COMPARISON_ALGORITHM_SHA256`) — interpolates
`linear_in_log2_frequency`. Example: a target falling 0 → −8 dB between 20 Hz
and 160 Hz evaluates to −5.33 dB at 80 Hz under log-frequency interpolation
(the convention every other frequency-axis consumer in the codebase uses,
including the octave-aligned PPO grid) vs −3.43 dB under the old code — a 1.9 dB
residual bias on a single mid-segment point. Endpoint clamping is retained
(flat extension of the edge level; a target-curve clamp is a policy choice,
distinct from the comparison authority's extrapolation-forbidden rule, which
exists because comparing two *measured* sets may not invent data).

**Persisted-computation impact:** `SpatialCorrectionEvidence` band residuals
computed under `fractional_octave` smoothing or non-flat targets change; the
policy's semantic hash already binds the policy (not the algorithm), so stored
evidence retains its recorded values but new runs differ.

## Finding 4 — fractional-octave smoothing was arithmetic dB mean (MEDIUM)

`_fractional_octave_smooth` averaged dB levels arithmetically over the
`center·2^±frac/2` window, while the declared smoothing authority
(`measurement_analysis.SMOOTHING_ALGORITHM_ID =
'fractional-octave-power-mean-1'`) combines them as power:
`10·log10(mean(10^(L/10)))`. For a −20 dB notch bracketed by 0 dB points in a
1/3-octave window the old code read −6.67 dB where power-mean reads −1.74 dB —
deep narrow dips were systematically overweighted in spatial-correction
residuals, biasing the correction design toward cutting peaks that the
declared smoothing model says are narrower than the band evidence suggests.
Now power-mean, same window bounds (unchanged — `±frac/2` octaves equals the
canonical `±1/(2N)` span for `frac = 1/N`).

## Finding 5 — extension-policy fields silently ignored (LOW)

`PhaseTimeAnalysisSpec` carries `lf_extension`/`hf_extension` tail-policy
fields, but `analyze_minimum_excess_phase` ran the cepstral estimator (flat
extension) regardless. A spec declaring `hf_extension='linear_slope'` or
`'producer'` would seal a result whose declared semantics it never honored.
The native path now rejects non-flat HF extensions explicitly; `'none'` /
`'flat'` / `'unknown'` remain accepted — LF extension is moot because the
estimator already requires a DC-reaching grid.

## Finding 6 — empty IR analysis window crashed deep in numpy (LOW)

`IRAnalysisSpec` validates `window_end_s > window_start_s` in *seconds*, but
`window_start_s·fs` and `window_end_s·fs` can round to the same sample index —
e.g. `1.5/fs → 2`, `2.4/fs → 2` at fs=48 kHz — leaving `windowed` empty and
failing with `ValueError: Invalid number of FFT data points (0)` inside
`_hilbert_envelope_db`. Now guarded up front with a domain message.

## Finding 7 — silent IR crashed on NaN at result sealing (MEDIUM)

An all-zero IR is routine evidence (muted channel, failed capture). With
zero energy the Hilbert ETC's `envelope/peak` and Schroeder's
`cumulative/cumulative[0]` divide 0/0 → NaN; `np.maximum` propagates NaN
through the −120/−140 dB clamp floors, NaN lands in `etc_db`, `decay_db`,
`noise_floor_db`, and `usable_dynamic_range_db`, and
`canonical_json(allow_nan=False)` raises
`ValueError: Out of range float values are not JSON compliant: nan` while
sealing the result — so silent evidence crashed instead of degrading to
unknown metrics. Reproduction: `run_ir_analysis(spec, (0.0,)*4800)` raised
at hashing; now it seals with ETC at −120 dB, decay at −140 dB,
`noise_floor`/`usable_dynamic_range` `None` (the clamp floor is not a
measured noise floor), all metrics `unknown`, and a `silent` warning. Also
guards non-finite input up front (`IR samples must be finite`), which hit
the same NaN-sealing path.

## Areas audited clean

- **dB discipline**: `20·log10` consistently used for amplitude/voltage/
  distance ratios, `10·log10` for power/energy (cad_direct_level,
  cad_amplifier_headroom, cad_ambient_noise energy sum, cad_equipment_self_noise
  energy combine). dBFS only at auralization headroom, correctly named. SPL vs
  gain conflation: not found.
- **Comparison grid/interpolation**: octave-aligned log2 grid (PPO=96),
  `linear_in_log2_frequency`, extrapolation forbidden; the vectorized path is
  bit-identical to the scalar reference (round 4 verified, still holds).
- **IR analysis**: Hilbert ETC, Schroeder reverse-cumulative decay, octave/
  third-octave FFT bandpass ratios `2^±1/2`/`2^±1/6`, fit intervals EDT [0,−10],
  T20 [−5,−25], T30 [−5,−35], markers anchored to t0, C50/C80/D50 energy
  ratios, fail-closed on truncation/insufficient dynamic range.
- **Spatial metrics** (cad_spatial_ir_metrics): IACC = max|corr| over ±1 ms
  with full-window normalization (ISO 3382), J_LF fig-8/omni energy,
  J_LFC, late lateral level — all correct.
- **FFT/scaling**: auralization `_resample_band_limited` Hermitian repacking
  with `n_out/n_in` scaling — amplitude-exact including cosine-at-Nyquist;
  convolution length `next_pow2(N+M−1)`; spectrogram Hann + peak-normalized
  amplitude dB.
- **Calibration chain**: RBJ peaking biquad (`a=10^(G/40)`, `α=sinω/2Q`,
  a0-normalized), pole-magnitude stability check, `H(e^{jω})` evaluation —
  standard and correct.
- **Angle conventions**: azimuth `atan2(x, y)` measured from +Y throughout
  (round 6 spec), consistent across coverage/directivity/geometric adapters.
- **Group delay**: unwrap-then-central-difference works on non-uniform grids;
  endpoint one-sided differences correct.
- **REW IR parser**: uniform-spacing validation fail-closed.
- **Optimizer objectives**: per-pairwise RMS/max aggregates, valid-domain
  bounds, identity payloads — sane.
- **Units**: exact scale factors (mm 0.001, cm 0.01, inch 0.0254, deg π/180,
  ms 0.001); parse/format split clean.

## Still standing (prior-round deferrals, unchanged)

- `_point_in_triangle` mixed m⁴/m tolerance — needs its own equivalence pass
  (round 7 standing item).
- Single `level` dB family (round 6 D2) — documented trade-off.
- RT60 decay metrics have no upper sanity bound — `insufficient_dynamic_range`
  gating covers the practical cases; standing note.

## Files changed

- `backend/src/htdt/cad_phase_time_analysis.py` — `spectrum.imag` extraction;
  flat-extension docstring; `hf_extension` fail-closed gate.
- `backend/src/htdt/cad_measured_modal_analysis.py` —
  `reconstruct_mode_shape` binds by `position_id`.
- `backend/src/htdt/cad_correction_design_policy.py` — `_interpolate_db` is
  log2-frequency; `_fractional_octave_smooth` is power-mean.
- `backend/src/htdt/cad_ir_analysis.py` — empty-window ValueError guard;
  silent/non-finite evidence fails closed (finite clamp-floor traces,
  unknown metrics, no NaN into canonical hashing).
- `backend/tests/test_issue_971_phase_time.py` — discrete-time golden (exact),
  flat-response regression, extension-gate test.
- `backend/tests/test_issue_972_measured_modal.py` — id-bound shape tests
  (reorder + fail-closed).
- `backend/tests/test_cad_ir_analysis.py` — empty-window, silent-IR, and
  non-finite-IR fail-closed tests.
- `backend/tests/test_cad_correction_design_policy.py` — log2-interp +
  power-mean pins.
