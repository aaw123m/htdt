# Round 14 — Solver / Numerical Compute-Load Depth

Scope: the acoustic math core — IR analysis, room modes, transfer-function evaluation over frequency grids, fractional-octave smoothing/interpolation, FIR symmetry scoring, spatial correlation (IACC), multi-seat statistics, directivity balloon evaluation — profiled at realistic sizes and fixed where Python-level waste costs real time without changing a single output bit.

**Verification bar:** sealed/replay-bound outputs (canonical JSON / SHA-256 of results that `replay_*` asserts equal) require **bit-identical** output, strictly stronger than `allclose`. Every fix below was verified exact-equal (`tuple(...) == tuple(...)` / `np.array_equal`) against the pre-change code at multiple sizes, in `backend/tests/test_round14_cpu.py` (16 tests).

Machine: Windows Server 2022 VM (noisy, ±25% run-to-run); figures are min-of-N reps.

## Top-10 profile (before → after)

| # | Path | Size | Before | After | Change |
|---|------|------|--------|-------|--------|
| 1 | `run_ir_analysis` end-to-end | 96 kHz × 2 s impulse | ~950–1050 ms | ~950–1050 ms | ≈ (see below) |
|   | └ `canonical_json`/`canonical_sha256` (2× ~5 MB sealed dumps) | — | ~500–550 ms | — | not touchable: required sealing |
|   | └ pydantic `valid_result` re-validation | — | ~530 ms | — | not touchable: contract |
|   | └ math inside call (marker scan, energy, STFT) | — | ~25 ms | ~12 ms | marker vectorized, STFT batched (bitwise identical) |
| 2 | `_artifact_response` (FIR artifacts over freq grid) | 4096 taps × 512 freqs | ~110 ms | ~110 ms | already numpy; fine at size |
| 3 | `smoothed_level_trace` | 2400-pt axis, 1/3-oct | 174.8 ms | 2.4–2.6 ms | **~70×** |
| 4 | `_fractional_octave_smooth` | 2400-pt axis, 1/3-oct | 183.1 ms | 1.8–5.2 ms | **~35–100×** |
| 5 | `evaluate_directivity` ×400 evaluations | 10260-sample balloon | 645.4 ms | 20–37 ms | **~17–30×** |
|   | └ `_sample_map` rebuild per call | — | 571 ms (83%) | ~0 | identity cache |
| 6 | `_iacc` correlation | 1 s @ 96 kHz, ±1 ms lag | 15.7 ms | 15.2 ms | ~3% + ~290 MB alloc churn removed |
| 7 | `run_multi_seat_analysis` (8 seats) | 20 Hz–20 kHz grid | 16.8 ms | 15.2 ms | ~10% (interp+sealing dominate) |
|   | └ per-column stat block only | — | ~7 ms | ~3.5 ms | ~2× |
| 8 | `rectangular_room_modes` | 6.5×4.2×2.8 m room | 0.6 ms @300 Hz; 17.5 ms @1000 Hz | — | fine at real sizes |
| 9 | `_linear_phase_score` | 65536 taps | ~1.8 ms | ~0.9 ms | ~2× (vectorized compare) |
| 10 | `_sample_hash` in directivity tests | per sample | hot | ~0 | `lru_cache` |

## Findings and fixes

### Fixed (all bit-identical)

1. **O(N²) fractional-octave smoothing → O(N log N)** — `smoothed_level_trace` (measurement_analysis) and `_fractional_octave_smooth` (cad_correction_design_policy) scanned all N samples for each of N centers. On a sorted axis (the normal case; validated datasets are strictly increasing), window edges are now `center·2^(±½frac)` computed per center, bounds found by `np.searchsorted` ('left'/'right', matching `lo <= f <= hi` exactly), and `sum()` folds the **list slice** — Python 3.12 `sum` is compensated (Neumaier), so summing the same values in the same order is bitwise identical to the old comprehension. Unsorted/degenerate axes keep the original scan loop verbatim. NaN centers still yield `ZeroDivisionError` as before.

2. **`_sample_map` + `_sample_hash` rebuilt per call** — `evaluate_directivity` rebuilt a 10260-entry map (571 ms, 83% of a 400-call bench) and re-hashed samples on every evaluation. `_sample_map` now caches by dataset identity (`id` key + strong ref, LRU-bounded to 8); `_sample_hash` uses `lru_cache(8192)` on the hashable frozen models. Cache-miss correctness preserved: a mutated/rebound dataset is a different object → different `id` → fresh build.

3. **`sorted(samples)` re-run per grid point** — `_interpolated_complex`, `_interpolated_magnitude` and `_load_impedance_at` in cad_speaker_level_transfer sorted the impedance samples on every frequency lookup. `_ordered_samples` caches by `samples` identity (strong-ref, size 64) so the ordering work is O(N log N) once per dataset, not per evaluation.

4. **`_iacc` per-lag allocation** — each lag segment allocated a fresh 1.5 MB `np.multiply` product (≈290 MB churn over ±1 ms of lags at 96 kHz). Now `np.multiply(..., out=product)` into a preallocated buffer then `np.sum(product)` — same accumulation order, bitwise identical.

5. **IR marker scan** — per-sample Python scan for the first 8 ETC threshold crossings → boolean mask + `np.nonzero`, same tie-breaks (strict `>`, first-after-t0 ordering).

6. **STFT frame loop → batched rfft** — `sliding_window_view(windowed, window_n)[::hop] * win` then one `np.fft.rfft(frames, axis=1)`. Verified per-frame bitwise identical (pocketfft, same transform per row); ~1.6× on the transform itself.

7. **`_linear_phase_score`** — per-tap Python compare → `np.all(np.abs(front ∓ back) <= tol)` on reversed slices; same short-circuit semantics (verified all four sym/asym branches).

8. **Multi-seat per-column stats** — M×G stat loops (min/max/spread/mean/argmax/argmin/outlier) → `zip(*rows)` transpose + builtin `min`/`max`/`sum` per column (compensated fold identical), `np.argmax/argmin` for seat indices (first-occurrence tie-break matches `max(range, key)`), `np.isnan` fallback to the scalar path for NaN input (np treats NaN as extremum; `max(range,key)` does not). Band deviations use elementwise `(row − mean)²` then compensated `sum` per seat — bitwise identical.

### Honest scope notes

- **`run_ir_analysis` is ~85% required sealing** (canonical JSON `dumps` ×2 + pydantic re-validation). In-call math work is ~25 ms of ~1000 ms; the fixes shave ~half of that but are invisible at call scale on this VM. Reported, not hidden.
- **`_artifact_response` (110 ms)** is already batched numpy — the cost is genuine math, not waste.
- **`rectangular_room_modes`** is genuinely fast at real sizes; the `floor() + 1` index headroom loses a couple of modes per dimension vs `ceil` and is doc-worthy only — changing it would alter sealed output.
- **`_iacc`** could go to FFT-correlation O(N log N) for large lag windows, but lag spans here are ~1 ms (≤193 lags ≈ N×193 work); the alloc fix removes the real waste. FFT convolve would change accumulation order → deferred, flagged.

### Deferred (would change output bits — flagged, not done)

- FFT-based `_iacc` correlation (would need allclose-level acceptance; mathematically equal, not bit-equal).
- `orjson`/faster serializer for canonical JSON — changes float formatting, breaks sealed-hash equality with stored artifacts.
- `rectangular_room_modes` `floor()+1` → `ceil` index headroom — changes returned mode list.
- Per-frame Hanning window allocation in `_linear_phase_score` callers — already cheap.
- `_fractional_octave_smooth` unsorted-axis path could use argsort once, but unsorted input is rejected upstream by dataset validation; the retained scan is the safe fallback.

### Accuracy-vs-cost honesty

- `smoothed_level_trace`/`_fractional_octave_smooth` previously did the *exact* mean over the window — now the same exact mean, just found faster. No approximation was introduced anywhere: zero tolerance or error-bound debt added.
- Canonical seals are computed on full-precision doubles — required for replay equality; no cheaper truncation is possible.

## Verification

`backend/tests/test_round14_cpu.py` — 16 tests, all asserting **exact equality** (not allclose) against verbatim pre-change reference loops: smoothing at sizes 24/240/960/2400, fractions 1/3, 1/12, 1/24, unsorted axes, duplicate frequencies, NaN-center ZeroDivisionError, full multi-seat result + `replay_multi_seat_analysis` byte-equality, `_iacc` vs old loop, `run_ir_analysis` markers + STFT + whole-result equality, `_linear_phase_score` branches, `_ordered_samples`/`_sample_map` cache identity + repeat-equality of `evaluate_directivity`.

Suites re-run green: test_cad_ir_analysis, test_cad_multi_seat_analysis, test_cad_directivity(+5), test_cad_fir_filter, test_cad_spatial_ir_metrics, test_cad_speaker_level_transfer, test_cad_correction_design_policy, test_cad_measurements(+2), test_round14_cpu — 161 tests, 0 failures.
