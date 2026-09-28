# Round 12 — Numerical Pipeline Depth-2

Scope: a second accuracy pass over the acoustic/numerical pipeline with
angles distinct from rounds 7 and 9 — unit/reference honesty through the
full chain, frequency-grid semantics at module boundaries, float
precision, windowing/FIR conventions, statistical aggregation (power vs
amplitude averaging, regression bounds), and solver-facing numerics
(PPW/CFL honesty, boundary condition math).

Method: every candidate defect was reproduced numerically with synthetic
signals before being counted; fixes are small diffs with regression
tests. No persisted-version keys were bumped.

## Verdict table

| # | Area | Severity | Verdict | Status |
|---|------|----------|---------|--------|
| 1 | `AmplifierOutputImpedanceAuthority.magnitude_at` returned `Z_ref/DF` at every frequency | **High** | Defect — the declared reference frequency is required, stored and hashed but never consulted; a damping factor declared e.g. at 50 Hz/1 kHz silently drove `computed_magnitude` transfers at all frequencies as a broadband constant | **Fixed** |
| 2 | Declared `interpolation='nearest'` ignored on amplifier curves and on the load's complex path | **Medium** | Defect — `ImpedanceInterpolation` is a required, hashed field on curve tiers; `_interpolated_complex`/`_interpolated_magnitude` and `_load_impedance_at` always linear-interpolated while `SpeakerElectricalImpedanceAuthority.magnitude_at` honors 'nearest' | **Fixed** |
| 3 | `materialize_fir_artifact` quantized pcm16/pcm24 taps without clamping to the representable range | **Medium** | Defect — a +1.0 tap materialized as 'pcm16' while quantizing to int 32768, which overflows s16 (max 32767) and wraps to −1.0 — a sign flip at the device | **Fixed** |
| 4 | `_matrix_conditioning` computed `λ_min = F − √(F²−4\|det\|²)` by direct subtraction | **Medium** | Defect — catastrophic cancellation loses the small eigenvalue's digits near the declared κ₂ threshold: reported 9.49e7 where the true κ₂ is 1.0e8 (5% *under*-estimate → ill-conditioned matrices can read as conditioned) | **Fixed** |
| 5 | `cad_ir_analysis._fit_slope` masks all in-band samples instead of the first contiguous crossing | — | Verified **not a defect** — the input is the Schroeder curve, which is monotone non-increasing, so the in-band mask is always the contiguous first-crossing slice | Not a defect |
| 6 | `ga_scatter_fraction` comment claims "band-center-weighted mean" | Trivial | Doc nit — the code computes an unweighted mean; comment now matches | **Fixed** |

## Audited clean (spot list)

- `comparison.py` — octave-aligned log2 grid (PPO=96), `ceil`/`floor` grid
  edges stay strictly in-band, `linear_in_log2_frequency` interpolation,
  extrapolation forbidden, pointwise `a−b` differences with explicit
  reference-band offset semantics (round 9 audit re-verified).
- `measurement_analysis.smoothed_level_trace` — true fractional-octave
  power-mean over `f·2^±1/(2N)` windows; phase unwrap keeps successive
  deltas within ±180°.
- `cad_geometric_acoustics_response` — monopole `jωρ·e^(−jkd)/(4πd)`
  per unit volume velocity under the declared `exp(+iωt)` phasor;
  complex source/reflection/portal transfers multiply correctly and the
  magnitude-only path degrades honestly.
- `acoustic_spatial_decomposition` — two-point Cramer's solve for
  incident/reflected phasors verified analytically.
- `optimization_robustness` — one-sided and central sensitivity slopes
  have the correct `Δobjective/Δinput` sign conventions.
- `cad_display_units` — imperial↔SI at the UI boundary uses exact
  constants (`0.0254 m`, `12·in`); `display_to_si` divides by exact
  integer denominators so `120 mm` == `0.12 m` bit-for-bit.
- `cad_ir_analysis` (beyond _fit_slope) — window gating, noise-floor
  guard, truncated-record blocking of clarity metrics all consistent
  with declared policy.

## Finding 1 — DF-derived output impedance applied broadband (HIGH)

`magnitude_at` (backend/src/htdt/cad_speaker_level_transfer.py) returned
`Z_ref/DF` at **every** frequency for the `damping_factor_derived` tier,
despite the class docstring and method docstring both declaring it
"resolves only to its declared reference frequency — the DF figure is
exact there, not a broadband constant." The declared
`damping_factor_reference_frequency_hz` was a required, semantic-hashed
field that no computation ever read.

Reproduction (DF=200 declared at 1 kHz on an 8 Ω reference):

| f (Hz) | before | after |
|---|---|---|
| 1000 | 0.04 | 0.04 |
| 100 | 0.04 | `None` → point skipped with reason |
| 20000 | 0.04 | `None` → point skipped with reason |

`evaluate_speaker_level_transfer` already fails closed on `None`
("amplifier impedance not defined at this frequency"), so the fix plugs
into existing machinery — a DF-derived amp now yields exactly one honest
transfer point at its reference frequency instead of a fabricated curve.

## Finding 2 — declared 'nearest' interpolation ignored (MEDIUM)

`ImpedanceInterpolation = Literal['linear','nearest']` is mandatory on
curve tiers and is semantic-hashed. Three evaluation paths ignored it:

- `AmplifierOutputImpedanceAuthority.magnitude_at` →
  `_interpolated_magnitude` — always linear;
- `AmplifierOutputImpedanceAuthority.complex_at` →
  `_interpolated_complex` — always linear;
- `_load_impedance_at` complex_curve branch — always linear (the load's
  own `magnitude_at` does honor 'nearest', so magnitude and complex
  paths could disagree on the same authority).

Reproduction (samples 8 Ω@100 Hz, 4 Ω@200 Hz, declared 'nearest'):

| f (Hz) | declared semantics | before | after |
|---|---|---|---|
| 150 | 8.0 (tie → lower) | 6.0 | 8.0 |
| 160 | 4.0 | 5.6 | 4.0 |
| 190 | 4.0 | 4.4 | 4.0 |

Nearest semantics match the load-side implementation exactly
(`min by |f−f_i|`, ties to the lower-frequency sample).

## Finding 3 — PCM tap quantization without range clamp (MEDIUM)

`materialize_fir_artifact` applied the quantization step
(`1/32768`, `1/8388608`) but not the signed-format range. A tap of
exactly +1.0 survived as 'pcm16' though it encodes to int 32768 —
unrepresentable in int16 (max 32767) — where a device read wraps to
−1.0, a sign flip, not a saturation.

Reproduction (taps `0.5, 1.0, −1.0, 0.25`):

| format | tap +1.0 before | after |
|---|---|---|
| pcm16 | 1.0 (→ int 32768, overflow) | 0.999969482421875 (32767/32768) |
| pcm24 | 1.0 (→ int 8388608, overflow) | 0.9999998807907104 (8388607/8388608) |

The clamp targets the format's own LSB range `[−1.0, 1.0−lsb]` (not the
effective step, which a custom `quantization_step` can make finer or
coarser than the format grid). The deviation stays explicit through the
materialization report's `max_response_error_db`.

## Finding 4 — κ₂ underestimation via cancellation (MEDIUM)

`_matrix_conditioning` (backend/src/htdt/acoustic_spatial_decomposition.py)
solved the 2×2 `A^H A` eigenvalues as
`λ_min = ½(F − √(F²−4|det|²))`. When `4|det|² ≪ F²` the subtraction
cancels the small eigenvalue's significant bits.

Reproduction (diag(1, 1e-8), |det|=1e-8, F=1+1e-16):

| formula | reported κ₂ | rel. err vs `numpy.linalg.cond` = 1.0e8 |
|---|---|---|
| direct subtraction | 9.4906266e7 | 5.1e-2 (**under** — anti-conservative) |
| `λ_min = \|det\|²/λ_max` (new) | 1.0000000e8 | 1.5e-16 |

The error direction matters: reporting ~5% low right at the declared
`maximum_condition_number_2 = 1e8` policy edge can classify an
ill-conditioned decomposition as conditioned. The product-identity form
is algebraically identical (λ_max·λ_min = |det|²) and keeps
`λ_min ≤ 0 → inf` for exactly-zero determinants.

One consequence: a matrix whose determinant is a tiny nonzero
floating-point residue (e.g. |det|≈6.4e-16) now reports a finite κ≈6e15
instead of `inf` — the previous `inf` was itself a cancellation
artifact. The SINGULAR verdict is driven by `|det| < minimum_abs_determinant`
and is unchanged; the existing test was updated to assert the finite
value.

## Finding 5 — _fit_slope verified sound (NOT A DEFECT)

The mask `(levels ≤ hi) & (levels ≥ lo)` in
`cad_ir_analysis._fit_slope` looks non-contiguous — a decay re-entering
the band would contaminate T20/T30/EDT fits — but its only input is the
Schroeder curve `10·log10(cumsum reversed)`, which is monotone
non-increasing by construction. For a monotone trace the in-band mask
*is* the contiguous first-crossing slice. A synthetic non-monotone trace
confirmed the two semantics diverge only when the input cannot be a
Schroeder output. No change.
