# Round 18 — acoustic/numeric truth vs reference

Scope: every place the codebase turns samples, spectra or geometry into a
*number* — decay metrics, room modes, spectrograms, fractional-octave
smoothing, IACC/lateral metrics, SPL/dB bookkeeping, Hz↔rad/s↔samples unit
plumbing. Method: independent recomputation inside the test itself — not
"does it run" but "is the number right". Each claim is re-derived from the
textbook/reference formula (closed-form standing waves, `scipy.signal.stft`
and `scipy.signal.hilbert`, `scipy.signal.correlate`, direct DFT sums, RBJ
cookbook, brute-force power means) and compared under a stated tolerance.
New coverage: `backend/tests/test_rev18_acoustic_truth.py` (18 tests).
Environment: Python 3.12.10 / numpy 2.5.3 / scipy 1.18.1, Windows,
`QT_QPA_PLATFORM=offscreen`. Branch `devin/rev18-acoustic`.

## Suspicions → verdicts

| # | Numeric claim | Independent recompute | Verdict |
|---|---|---|---|
| 1 | Schroeder decay curve = 10·log10(reverse-cumsum energy / total) | Direct cumsum on a synthetic IR | Clean (float-exact, −140 dB floor) |
| 2 | EDT/T20/T30 recover known RT60 | `h(t)=exp(−αt)`, `α=3·ln(10)/T60` → energy decays exactly 60 dB at T60; also independent `polyfit` on the in-test Schroeder curve | Clean (within 2 % of truth; regression slope identical) |
| 3 | C50/C80/D50 energy ratios at canonical 50/80 ms boundaries | Hand-placed spikes straddling the boundary; direct `sum(h²)` early/late | Clean (10·log10 energy, not amplitude) |
| 4 | ETC = 10·log10(|hilbert|²/peak) | `scipy.signal.hilbert` on identical input | Clean |
| 5 | Bandpass edges: octave `f_c·2^(±1/2)`, third-octave `f_c·2^(±1/6)` | Recompute brickwall mask + `irfft` directly | Clean (exact) |
| 6 | Rectangular-room modes `(c/2)·√Σ(nᵢ/Lᵢ)²`, class by nonzero indices | Closed-form enumeration, all mode families | Clean (bit-identical) |
| 7 | First-order reflections = mirror image + t-param wall intersection | Geometric recompute of image, delay, `c/(2·excess)` | Clean |
| 8 | Spectrogram = `|rfft(frame·hann(M))|`, peak-normalized, `hop=(1−overlap)·M`, times `(start+M//2)/fs` | `scipy.signal.stft` (`sym` Hann, `boundary=None`, `padded=False`) on identical input | Clean (magnitudes within 1e-9 rel after shared peak normalization; grid identical) |
| 9 | Fractional-octave smoothing = 10·log10(mean(10^(L/10))) over ±1/(2N) oct | Brute-force power mean; also Jensen inequality (linear mean ≥ dB mean) | Clean — **energy-domain mean, not dB mean** |
| 10 | Comparison grid 96 PPO anchored at `2^(k/96)`; interp linear in log2(f) | Manual grid + `np.interp` on log2 axes | Clean; mean/rms/offset/shape metrics exact on linear-in-log2 inputs |
| 11 | IACC = max\|normalized xcorr\| over ±1 ms | `scipy.signal.correlate` (full) + `correlation_lags`, normalized by `√(Σl²·Σr²)` | Clean; bounded-lag window verified (3 ms shift rejected) |
| 12 | J_LF/J_LFC/L_J lateral metrics at canonical early/late windows | Textbook sums on constructed fig-8/omni signals | Clean (early 5–80 ms, late 80 ms→end, reference-normalized) |
| 13 | SPL: `20·log10(p/20 µPa)`; free-field 1/r² energy → −20·log10(d) per doubling | Constructed pressure/levels; +3.01 dB coherent pair, −6.02 dB per distance doubling | Clean (20 µPa reference, energy-domain sums) |
| 14 | RBJ biquads (peaking/low_pass/high_pass/all_pass) | Cookbook coefficients → direct H(z) eval; peaking = exactly `gain_db` at f₀, Butterworth LP −3.01 dB at f_c | Clean |
| 15 | FIR response `Σ taps·e^(−j2πfk/fₛ)` | Direct DFT recompute | Clean |
| 16 | **FIR `group_delay_s` = −dφ/dω** | Analytic `−Im(H'/H)`; linear-phase truth = (N−1)/2 samples | **CONFIRMED BUG — fixed** (see below) |
| 17 | Finite-record transfer = dt-weighted `Σx·e^(+j2πf·n·dt)` ratio | Direct kernel recompute + conjugate-DFT identity | Clean (declared `exp(+iωt)` kernel honored) |
| 18 | Causal boundary admittance `Σ 1/(jωd + e + f/jω)` | Direct complex recompute | Clean |
| 19 | T60 ↔ nepers: `rate = ln(10³)/T60` | `MeasuredMode` validator accept/reject | Clean |
| 20 | Two-point decomposition: Cramer solve of I,R against wall | Known I/R phasors recovered; `np.linalg.cond` vs reported conditioning | Clean |
| 21 | Phasor convention e^(−iωt)↔e^(+iωt) = conjugate; hybrid weights complementary linear-in-Hz | Direct conjugate/identity checks | Clean |
| 22 | Multi-seat per-grid-point min/max/mean/spread | Independent log2 interp + `np` column stats | Clean |
| 23 | Sabine/Eyring RT60 prediction vs synthetic enclosures | — | **N/A**: no RT60 *predictor* exists; decay metrics verified on synthetic IRs per dimension (1) intent |

## Finding details

### FIXED — FIR `group_delay_s` finite-difference aliases on sparse grids
(`cad_fir_filter._artifact_response`)

`group_delay_s` was computed as `-np.gradient(np.unwrap(phase), freqs)/(2π)`
over the **caller-supplied** evaluation grid. Two compounding errors:

* `np.unwrap` needs adjacent phase samples closer than π; on any grid where
  the true phase moves more than π between evaluation points the unwrap
  silently produces a wrong ramp, and the finite difference inherits it.
* Even with correct unwrapping, a finite difference is only as good as the
  grid — the number reported depends on where the *other* evaluation points
  happen to sit, which violates per-frequency truth.

Observed: a symmetric linear-phase FIR (taps `[0.1,0.2,0.5,0.2,0.1]`, true
group delay exactly 2 samples = 41.67 µs at every frequency) reported
`group_delay_s` of 1.95 samples at 1 kHz and **−0.087 samples** at Nyquist
when evaluated on `{100, 431.5, 1000, 24000}` Hz — a negative delay for a
causal symmetric filter.

Fix: analytic derivative — with `H(ω)=Σtₖe^(−jωk/fₛ)`,
`dH/dω = Σ(−jk/fₛ)tₖe^(−jωk/fₛ)` and `τ_g = −Im(H′/H)` — computed per
evaluation frequency, independent of the grid. Singular points
(|H| = 0, where group delay is genuinely undefined) are kept finite via
`nan_to_num` (0/0 → 0, ±∞ → ±max float) so serialized diagnostics stay
valid. Regression: `test_fir_transfer_matches_direct_dft_and_group_delay_seconds`
now asserts the exact 2-sample delay at every point of the sparse grid.

Note the sibling `htdt_group_delay_v1` algorithm in
`cad_phase_time_analysis` deliberately pins unwrap+gradient on *dense*
measured phase traces — a different, declared convention on a different
input; left untouched.

## Verification detail per charter dimension

1. **RT60/decay vs analytic**: no Sabine/Eyring predictor exists in the
   codebase (verified by sweep) — the acoustic chain exposes decay *metrics*
   of impulse responses instead. Verified against a synthetic enclosure IR
   with analytically known decay (energy −60 dB at T60 = 0.4 s): EDT/T20/T30
   recover 0.4 s within 2 %, and the reported slopes match an in-test
   `polyfit` recompute to 1e-9 rel. Schroeder curve is float-exact.
2. **Mode frequencies**: all modes ≤ 300 Hz of a 4.0×3.2×2.6 m room
   (c = 343 m/s) enumerated by closed form; axial/tangential/oblique
   classification by nonzero index count matches.
3. **STFT/spectrogram**: `scipy.signal.stft` reference on identical input,
   window, overlap; production output equals `|Zxx|` peak-normalized →
   20·log10, clamped at 1e-6; time centers identical.
4. **Fractional-octave smoothing**: brute-force power mean over the
   ±1/(2N)-octave window — the code averages *energy* (`mean(10^(L/10))`)
   then re-logs; Jensen's inequality confirms the domain (a dB-domain mean
   would be a systematic ~−0.5 dB bias at ±10 dB spread — not present).
5. **IACC/lateral**: textbook formulas on constructed signals; bounded ±1 ms
   lag window rejects a 3 ms shift (|IACC| < 0.6 while in-window 7-sample
   shift yields ≈0.97).
6. **SPL/dB**: 20 µPa reference, 20·log10 for pressure amplitudes,
   10·log10 for energies, energy-domain summation (+3.01 dB per equal-power
   doubling), −6.02 dB per distance doubling — no log10/ln or 10/20 factor
   confusion found anywhere.
7. **Unit conversions**: Hz→rad/s inside admittance branches
   (`jω = j·2πf`), dt-weighted DTFT kernel (`n·dt` exponent), samples↔s in
   canonical windows (5/50/80 ms boundaries), T60↔nepers `ln(10³)`
   (= 6.907755…), phasor-convention conjugation — all verified; no mm
   conversion on solver paths (geometry is meters throughout).

## Tolerance notes

* Float-identical recomputes (Schroeder, modes, brickwall mask, grid):
  atol 1e-12 — any difference is a real ordering/rounding change.
* scipy-reference checks (hilbert, stft): rel 1e-9–1e-10 — same math,
  different FFT plans.
* Regression-fit metrics (EDT/T20/T30 vs constructed truth): rel 2 % —
  discrete-time energy decay of a sampled exponential has quantization
  error ~O(1/N); the fit itself is then cross-checked to 1e-9.
* IACC: abs 1e-6 — normalized correlation is float-stable.
* Slope/delay exact values (FIR delay, biquad center gain): rel 1e-6 —
  analytically exact modulo float64 evaluation.
