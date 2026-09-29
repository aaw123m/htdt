# Round 16 — degenerate real-data shapes

Scope: data shapes a real user legitimately hits, pushed through the real
code paths (no fuzz junk). Categories: EMPTY (zero measurements/entities/
results/comparison sets), SINGLE (1-point curves, 1-seat analyses,
1-sample IRs, n-1 division risk), CONSTANT (flat responses → 0/0
normalization, zero autoscale ranges), EXTREME (±200 dB+, frequencies at
0/Nyquist, absurd phase magnitudes, zero vs huge durations), NAMES
(unicode/emoji, duplicates, 500-char, whitespace-only), BOUNDARY
(non-power-of-2 grid ends, band-edge bins, 44.1k/48k mixes). Method:
minimal reproductions against the shipped APIs, checking *correctness*
(is the output honest?) not just absence of crashes. Python 3.12,
Windows, pytest `-n 4`, `QT_QPA_PLATFORM=offscreen`.
Branch `devin/rev16-edge`.

## Verified issues and fixes

### 1. `smoothed_level_trace` — EXTREME: deep-null smoothing window crashed (`log10(0)`)

`backend/src/htdt/measurement_analysis.py`

A smoothing window whose members all sit below roughly −3080 dB produced
`10 ** (v/10) == 0.0` for every element — window mean 0 →
`10 * log10(0)` → `ValueError: math domain error`. The dataset model
accepts any finite level, so this is reachable with a legitimate (if
extreme) imported measurement. The call site
(`measurement_page_workspace._refresh_quality_plot`) invokes it
unguarded, so selecting a smoothing fraction on such a dataset crashed
the panel. A single −4000 dB edge point in an otherwise normal dataset
also poisons *its* window.

**Fix**: saturate the power exponent at ±300
(`10.0 ** min(max(v/10.0, -300.0), 300.0)`) in both the sorted and
unsorted window paths. The power mean stays finite and monotone;
ordinary data is bit-identical since real dB magnitudes never approach
±3000 dB. Output floor/ceiling is an honest ±3000 dB rather than a
fabricated number.

### 2. `phase_trace(unwrap=True)` — EXTREME: UI hang on huge stored phase

`backend/src/htdt/measurement_analysis.py`

The unwrap loop folded each sample toward its predecessor with
`while candidate - previous > 180: candidate -= 360` — one iteration per
360° of delta. A stored phase of 5e7 deg took 0.25 s (~139k iterations);
a phase of 1e12 deg needs ~3e9 iterations — a hang triggered by ticking
the unwrap checkbox. Phase magnitude is not bounded by the dataset
validator beyond finiteness, so a corrupted or pathological import hits
this.

**Fix**: replace the loop with one `math.remainder(candidate - previous,
360.0)` fold — `remainder` returns the delta in [−180, 180] via IEEE
exact rounding, and the −180 tie is folded to +180 to keep the
half-open (−180, 180] interval the loop converged to. Verified identical
output on every boundary case (±180, ±540, 180.0000001, −180.5, 900/1260
chains, 5e7) and equal-or-more-accurate on wild magnitudes (the old loop
accumulated ~1e-7 deg of subtraction noise over ~10⁶ iterations; the new
fold is correctly rounded).

### 3. `_volume_axes` — BOUNDARY: `stride_m=0` escaped as `ZeroDivisionError`

`backend/src/htdt/cad_field_explorer.py`

The volume-grid builder validated stride for finiteness but divided
`extent / stride` before checking the sign. `stride_m=0.0` raised
`ZeroDivisionError`, which is *not* a `ValueError` and therefore escaped
the caller's `except ValueError` handler in
`field_explorer_panel.py` — an uncaught crash instead of an honest
rejection. A negative stride fell through to a cryptic
`RegularGridAxis.spacing_m` pydantic message. (The shipped spin box
floors at 0.02 m, but the authority function is public API and is what
the panel's error contract promises to handle.)

**Fix**: `if stride <= 0.0: raise ValueError('grid stride must be
positive')` after the finiteness check — one honest rejection for zero
and negative strides alike.

### 4. `SceneEntity.name` — NAMES: whitespace-only names accepted

`backend/src/htdt/cad_scene.py`

`Field(min_length=1)` admits `'   '` and `'\t\n'`, which then render as
blank rows in pickers and produce labels like `' · kind · semantics'`.
The project's own convention (`ProjectCreate.validate_name_not_blank`,
`project_library_repository` display-name handling) strips and rejects
blank names; entity names were the odd one out. Unicode, emoji and
500-char names are correctly accepted verbatim and stay that way.

**Fix**: `if not self.name.strip(): raise ValueError('name must not be
blank')` in the existing `semantic_fields` model validator — same
message and mechanics as `ProjectCreate`.

## Verified honest (no change needed)

| Path | Degenerate input | Behavior |
|---|---|---|
| `comparison.py` | empty response set, empty grid, single-point, duplicated freq axis, ±300 dB levels, constant level | typed `ComparisonError` for invalid input; empty-but-valid grid yields metrics `None`; extremes stay finite |
| `cad_measurement_models` | 1-sample / empty dataset, non-increasing freq axis | rejected at the model (≥2 samples, strictly increasing positive freqs) — SINGLE and EMPTY can't reach the traces |
| `analysis_export` | empty bundle, single point, constant axis, ±1e300 levels, `<script>` in labels | empty bundle exports honest empty state; constant axes padded +1; non-finite points dropped; HTML escaped |
| `cad_ir_analysis` | silent IR, 4-sample IR, band above Nyquist, 0.001 Hz band, fs=1 THz / 1 mHz | honest degradation — silent/BLOCKED capability states, no fabricated metrics |
| `cad_phase_time_analysis` | 2-sample group delay, constant phase, phase 1e15 | <3 samples rejected; constant → −0.0 group delay; huge phase → large but finite |
| `pareto.py` | empty candidate set, single candidate | `ParetoEmptyError`; singleton is non-dominated |
| `cad_ambient_noise` | UNKNOWN noise authority | degrades to unknown state, not fabricated values |
| `cad_mic_response_calibration` | coincident mic positions | `unknown_orientation`, not a bogus axis |
| `cad_speaker_level_transfer` | zero impedance | skipped honestly |
| `cad_measured_modal_analysis` | zero-height peak | rejected |
| `cad_spatial_field.probe_field` | trilinear at exact hi/lo edges, count-1 axis, off-domain | snap-tolerance + domain check make the edge branches unreachable; single-sample axis raises; off-domain fails closed |
| `SceneEntity` names | emoji, 500 chars, embedded spaces | accepted verbatim — correct |

## Tests

`backend/tests/test_review_round16_degenerate.py` — 18 tests covering
each fix (finite saturated smoothing at ±3000 dB floors, constant-time
unwrap on 1e12 deg, (−180, 180] invariant and tie semantics, stride ≤ 0 →
`ValueError`, blank-name rejection) plus the kept-honest paths
(unicode/long names, unchanged ordinary smoothing).
