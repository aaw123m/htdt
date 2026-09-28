# Round 13 — Number Truth End-to-End

Dimension: do the numbers a user sees in reports, comparisons, charts and
optimizer output actually equal what the raw data says? Method: feed known
synthetic inputs through the real pipeline and recompute every displayed
quantity independently — in the test (`backend/tests/test_round13_recompute.py`)
or by hand. A finding without a computed counter-example is not a finding.

## Surface → recomputed → verdict

| Surface | Input | Pipeline value | Independent recomputation | Verdict |
|---|---|---|---|---|
| IR decay metrics (EDT/T20/T30) | h(t)=exp(−t/τ), τ chosen for RT60=0.400 s | edt=0.4000, t20=0.4000, t30=0.4000 s | RT60 = 3·ln10·τ = 0.4 s (energy decays at 2/τ) | **pass** |
| IR clarity C50/D50 | impulses 1.0@0ms, 0.5@60ms, 0.25@100ms | c50=5.0515 dB, d50=0.7619 | 10·log10(1.0/0.3125); 1.0/1.3125 | **pass** |
| IR clarity C80 + early_energy@80 | same triplet | c80=13.010 dB; ee=0.9524 | early window covers the 60 ms impulse: 10·log10(1.25/0.0625); 1.25/1.3125 | **pass** |
| Comparison mean/RMS/offset/shape | log2-linear A−B difference 0.5·log2(f/100) | mean/rms/level_offset/shape_rms | exact by construction on the 96-PPO grid; hand-computed over `_grid`/`_valid_grid` | **pass** |
| `smoothed_level_trace` | 3-point dataset, 1/12-oct window | 0 / 10 / 0 dB | power-mean over the ±1/24-oct window — only the centre sample falls inside | **pass** |
| Multi-seat min/max/spread/mean | 3 seats at +0/+3/+9 dB | spread=9, mean=member mean, outlier=seat 2 | per-point min/max; arithmetic mean in dB; max RMS deviation | **pass** |
| `seat_pairwise_objectives` | same 3-seat set | max=9.0, rms=6.481 | pairwise diffs 3/9/6 → max 9, rms √42 | **pass** |
| `target_response_objectives` | B = A −3 −1.5·cos | peak_excess=0, dip_deficit=4.5 | max(0, max d), max(0, −min d) | **pass** |
| `movement_objectives` | Δ(3,4,0) m | total=max=5.0 m | Euclidean | **pass** |
| Acoustic target verdict boundary | obs=0.5 vs rule max 0.5 | inclusive → MET; exclusive → UNMET | `_compare` honours `lower_inclusive`/`upper_inclusive` via Decimal | **pass** |
| Target unit conversion | obs 500 ms vs criterion 0.5 s | MET, member value 0.5, converted=True | `convert_unit` ms→s then compare | **pass** |
| IACC | identical L/R | 1.0 | max \|Σlr\|/√(Σl²Σr²) over ±1 ms | **pass** |
| Lateral fraction J_LF | omni E=100, lateral E=5 in window | 0.05 | Σp_L²[window] / Σp²[0→end] | **pass** |
| REW PPO freq axis | startFreq=20, ppo=2 | 20, 28.28, 40, 56.57 | start·2^(i/ppo) | **pass** |
| Room modes | 4×5×2.5 m room | (0,1,0)=34.3 Hz first | c/2·√Σ(nᵢ/dᵢ)² | **pass** |
| First-order reflections | image method | excess/delay per surface | reflected − direct path, /c | **pass** |
| Report metrics tiles | saved comparison | `.3f` of stored double | same stored value as SPA `.toFixed(3)` | **pass** |
| Export precision | CSV/JSON | `.12g` / canonical JSON | same stored value, more digits — consistent | **pass** |
| SPL display gating | field without absolute reference | SPL unavailable | `supports_quantity` fails closed | **pass** |
| SPL conversion | magnitude → dB | 20·log10(p/20 µPa) | textbook reference pressure | **pass** |

## Units trace — RT60 (decay metric) end to end

`run_ir_analysis` computes `IRDecayMetric.value_s` in **seconds** from the
Schroeder slope → persisted on the sealed `IRAnalysisResult` →
`energy_metric_observation`/`AcousticTargetObservation(unit='s')` →
`_convert_observation_value` converts to the criterion unit before comparing
→ verdict carries `observed_value` in criterion unit plus `source_unit`. The
value stays in seconds at every hop; conversion is explicit and flagged
(`converted=True`). No ms/s slip found.

## Precision audit

Display layers round presentationally — SPA `.toFixed(3)`/`.1f`, report
`.3f`, optimizer `.4g`, CSV/JSON `.12g`, embedded snapshot full precision —
all reading the same stored double. A user is never shown a rounded value
that disagrees with the exported value of the same quantity: the export is
strictly more precise than the tile.

## Aggregates / NaN handling

Multi-seat mean is arithmetic in dB only when `central_tendency` declares it
(domain explicit on the result). Comparison interpolates only inside the
common overlap — no extrapolated points ever join aggregates. Zero-energy IR
evidence fails closed (clamped floors, metrics `unknown`) instead of NaN
leaking into the sealed JSON (`allow_nan=False`).

## Finding REV13-01 — spec builder rejects int inputs for float fields (fixed)

`build_ir_analysis_spec` sealed `spec_sha256` from a `model_construct`
provisional holding the *raw* arguments, but validation coerces float-typed
fields afterwards. Canonical JSON serializes an int as `48000` and a float
as `48000.0`, so any int literal reaching a float-typed field
(`sample_rate_hz=48000`, `window_start_s=0`, `clarity_split_times_ms=(50,80)`,
`tf_window_s=1`, …) hashed differently pre- vs post-validation and the
constructor always raised `ValueError: IR analysis spec hash mismatch`.
Same number, two canonical serializations — a user-facing authority the
documented builder could never produce.

Reproduced: `build_ir_analysis_spec(..., sample_rate_hz=48000)` →
ValidationError while `48000.0` succeeded.

Fix (`cad_ir_analysis.py`): normalize float-typed fields to `float()` (and
integral floats for `time_zero_sample`) in the payload before hashing, so
the provisional identity payload already carries the validated
representation. Pinned by `test_spec_builder_accepts_int_for_float_fields`
and `test_spec_builder_float_semantics_unchanged`.

## Deferred

- **Same latent pattern elsewhere.** ~115 modules seal identities the same
  way (raw-payload `model_construct` + hash + validated construct). Modules
  whose `identity_payload()`/`semantic_payload()` returns
  `model_dump(mode='json')` are safe — dump coerces on serialization — but
  hand-built payload dicts that inline `self.<float field>` share the
  exposure (e.g. any other builder taking a float-typed argument). Needs a
  repo-wide sweep (candidate: hash via a shared canonicalize-then-digest
  helper) — out of this round's small-diff scope.
- `IRReflectionMarker.delay_s` can exceed the window when
  `time_zero_sample` precedes `window_start_s`; semantics stay correct
  (delay is t0-relative), noted for completeness.
