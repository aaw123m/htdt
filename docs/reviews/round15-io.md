# Round 15 — Real-File Fidelity / Round-Trip Identity

Scope: does import → store → export → re-import preserve the DATA
faithfully, not approximately? Method: built a corpus of
vendor-faithful files with real quirks (34 files: tab vs space vs
comma, CRLF vs LF, UTF-8 BOM, UTF-16LE, cp932, BOM+JP headers,
decimal-comma locale exports, scientific notation, junk rows, ragged
phase columns, multi-measurement concatenations, canonical REW IR
`//`-annotated exports, real Peace/E-APO configs, include chains and
cycles, a real `.mdat` binary, a vendored `.htdtcapture` fixture) and
drove them through the **real** pipeline — `stage_rew_text` →
`commit_pending` / `commit_batch` → `dataset_for_measurement` readback
→ declared-asset export → re-import — verifying every stored array
byte-for-byte against the parsed file content, never the importer's
self-report.

Journey drivers: `C:\t\round15\phase_ab.py`, `phase_c.py`,
`phase_d.py` — corpus in `C:\t\round15\corpus`, results JSON in
`C:\t\round15\out\`. The canonical REW IR export shape was
reconstructed from the REW documentation (metadata rows as
`<value> // <label>` followed by a bare amplitude column).

## Verdict matrix — which documented dialects parse vs error honestly

### A1 — REW frequency-response text

| File | Quirk | Result | Fidelity |
|---|---|---|---|
| `rew_v540_crlf.txt` | real V5.40 header block, CRLF, tab cols | parse | 240 rows bit-exact, 6 header lines in provenance |
| `rew_nophase.txt` | 2-col (freq+SPL) | parse | exact; `phase_status=absent` |
| `rew_bom.txt` | UTF-8 BOM | parse | exact (BOM stripped by utf-8-sig) |
| `rew_hp.txt` | `Hz\tdBSPL\tdeg` header | parse | exact; `phase_status=unknown` |
| `rew_jp_header.txt` | JP comment lines | parse | headers preserved verbatim in provenance |
| `rew_sci.txt` | `1.0E4` style mantissas | parse | exact |
| `rew_space_lf.txt` | space-separated, LF | parse | exact |
| `rew_tail_note.txt` | trailing non-numeric note | parse | note lands in headers, rows exact |
| `rew_numeric_junk.txt` | `40a 71` corrupted row | parse + `junk_numeric_line:3` warning | row dropped honestly (F3) |
| `rew_de_decimal.txt` | `29,5` decimal-comma | **reject** | honest: not a supported dialect |
| `rew_excel_de.csv` | Excel-de `;`-separated CSV | **reject** | honest |
| `rew_cp932.txt` | cp932-encoded header | **reject** | honest UTF-8-only message |
| `rew_utf16le.txt` | UTF-16LE | **reject** | honest message (generic wording, see deferred) |
| `rew_4col.txt` | 4 numeric columns | **reject** | "expected 2 or 3 numeric columns" |
| `rew_desc.txt`/`rew_dup.txt` | descending/duplicate freq | **reject** | strictly-increasing violated |
| `rew_multi_concat.txt` | two measurements concatenated | **reject** | second header trips increasing-freq check — honest (no silent merge) |
| `rew_ragged.txt` | phase column appears mid-file | **reject** | "phase column presence changes" |
| `rew_header_only.txt` | headers, no data | **reject** | "at least two rows required" |

### A2 — REW impulse-response text

| File | Quirk | Result | Fidelity |
|---|---|---|---|
| canonical REW export | `// Peak value`, `// Peak index`, `// Response length`, `// Sample interval (seconds)`, `// Start time (seconds)` + bare amp column | parse | sr/start taken from declared metadata — **exact** (F1 fix); length mismatch vs declared → `ir_declared_length_mismatch` warning |
| `ir_2col.txt` | time+amp, 9-decimal stamps @48 kHz | parse | amplitudes bit-exact; sr inferred `48000.77` from quantized stamps — honest inference (see deferred note) |
| `ir_amp_only.txt` | bare amplitude column | parse + `ir_time_axis_unresolved` | importer requires explicit rate declaration — honest |
| `ir_csv.csv` | comma-CSV `t,a` with dots | parse | sr `44099.5` inferred from stated decimals |
| `ir_de.txt` / `ir_amp_de.txt` | decimal-comma rows | **reject** | ambiguity guard (F2 fix) |
| `ir_nonuniform.txt` | non-uniform t axis | **reject** | cannot be regridded — honest |
| data row with `// note` | annotated sample | parse + `ir_commented_row` warning | row preserved in headers, not silently lost |

### E-APO (Equalizer APO / Peace)

| File | Result |
|---|---|
| `eapo_rew.txt` (real REW EQ output) | all 5 filters exact incl. `OFF` disabled state; `Fc/Gain/Q` each verified vs raw text |
| `eapo_peace.txt` (real Peace config) | per-channel preamp `-3.5`, delay `0.35 ms`→`0.00035 s`, filters exact; `Device:` → opaque `device_scope` |
| `eapo_inc_main.txt` | `Include:` chains resolved; ALL→global + per-channel scope correct; channel mapping `L/R` mapped, `ALL` flagged ambiguous (honest) |
| `eapo_cycle_a.txt` | include cycle detected and diagnosed, both files' deps recorded |
| `eapo_quirks.txt` | `If/ElseIf/EndIf` → opaque conditional blocks; `GraphicEQ`/`Copy`/`Command`/lowercase `channel`/`Filter` without index → opaque `unsupported_command`; `Delay: 128 samples` → opaque (needs a source rate); `Convolution:` → `external_file_reference` dependency |
| keyword-present-unparseable (`Fc 2e3 Hz`, `Fc 29,5 Hz`, `Preamp: boost 3`) | → opaque `malformed_line` with reason (F4 fix; previously silently nulled fields or parsed stray digits) |

### Versioned / container formats

| Artifact | Result |
|---|---|
| `.htdtproject` export → re-import | `all_datasets_eq=True`, 8 members byte-identical |
| bundle manifest `schema_version: 0.9.0` / `2.0.0` | both **rejected** — pinned `1.0.0`, honest |
| `.htdtcapture` phase6 fixture (vendored real bundle, `app_build=phase6-fixture`) | `import_capture_artifact` → `stage='committed'`, digest + lineage verified, `quality_state='validated'` |
| compat verdicts | v1 required → `supported`; v9 required → `update_consumer_required`; optional v9 → `supported_with_degraded_optional_features`; unknown kind → `unknown_protocol_kind` |

## Round-trip identity — verified numerically

- **FR text → store → readback**: all parsed files round-trip with
  `arrays_eq=True` — `frequency_hz`/`level_db`/`phase_deg` tuples
  identical, raw bytes SHA-256-identical, header lines in provenance.
- **Declared lane export → re-import**: `dataset_eq=True`; the tamper
  control (mutated dataset + same raw) is **rejected** at save —
  semantic replay `verify_imported_dataset` is enforced on write AND
  on every authoritative read.
- **IR**: real REW canonical file → `sample_rate_hz=48000.0` exact
  (from declared `2.0833333333333333E-5` step), `start_time_s=-4.17e-5`
  exact (from declared Start time), amplitudes exact; save→readback
  equal; tamper rejected.
- **REW API fixture** (`api_measurement_2026-09-16.json`): 958 f32
  magnitudes decode bit-exact into f64; axis derives exact; capture
  timestamp `2026-Sep-16 12:11:10` → `2026-09-16T12:11:10+00:00`
  with `captured_at_source='host_local_timezone'` (honest timezone
  provenance).
- **Analysis export**: `render_analysis_json` — 17-digit input
  survives exactly. `render_analysis_csv` — `.12g` formatting: max
  rel err `6.1e-13` (e.g. `20.123456789012344` → `20.123456789`).
  **Documented lossy lane** — JSON is the byte-faithful authority.
- **Multi-file batch** (`stage_rew_text_files` + `commit_batch` with
  distinct assignments): both items commit to distinct datasets, each
  array-eq vs its own source — no swap, no collapse.
- **Edit-then-export**: datasets are immutable evidence pinned to the
  bound revision; after mutating the scene (`rev1≠rev2`), export marks
  the series `historical=True` — honest staleness, never stale-rewrite.
- **`.mdat` binary**: stored as opaque attachment (`kind='mdat'`,
  SHA-256'd, listed) — not parsed, not corrupted.
- **Big file**: 1,000,000-row / 26.6 MB REW text parses in ~2.9 s and
  commits all rows (f64 `array('d')` BLOB); >32 MiB →
  `IngressTooLargeError`, an explicit honest cap, no silent truncation.

## Findings and fixes

### F1 — Canonical REW IR export was rejected outright — FIXED

`parse_rew_impulse_response` treated every line as data or junk:
real REW exports interleave `<value> // <label>` metadata rows
(`Peak value`, `Peak index`, `Response length`, `Sample interval
(seconds)`, `Start time (seconds)`) before the bare amplitude column,
and the old grammar failed them ("expected 1 or 2 numeric columns").
Separately, a 48 kHz two-column export written with 9-decimal
timestamps was rejected by a `1e-9` absolute uniformity tolerance —
the file's own quantization floor is `2.5e-10`, so `1e-9` was tighter
than the format can express.

Fixed in `cad_measurement_ir.py`: `//`-annotated rows are recognized
as provenance (five declared fields parsed; unrecognized commented
numeric rows warn `ir_commented_row:<n>` instead of silently
absorbing a data row); the uniformity test now scales to the file's
own timestamp resolution — `min(max(1e-9·|step|,
2.5·10^-decimals), 0.25·step)` — and amplitude-only files derive
`sample_rate_hz = 1/declared_step` and `start_time_s =
declared_start` when declared (still requiring an explicit
declaration otherwise). Declared-vs-actual length/peak mismatches
warn (`ir_declared_length_mismatch`, `ir_declared_peak_mismatch`).

### F2 — Decimal-comma IR columns silently corrupted data — FIXED

`ir_amp_de.txt` (`0,5\n1,4\n…`) tokenized `0,5` → two columns
`[0, 5]` via `_SPLIT` — a one-column decimal-comma amplitude file
turned into bogus time/amplitude pairs (and in probe form: amplitudes
×10 with `sr=1 Hz`). Now: a two-column row containing a comma and no
period is rejected — "comma-separated integer rows are ambiguous
with decimal-comma locale exports" — ambiguous files fail honestly
instead of becoming wrong data. Comma files whose numbers carry dots
(real CSVs) still parse.

### F3 — Corrupted FR data rows vanished into the header block — FIXED

`40a 71` in the middle of an otherwise clean FR file was appended to
`header_lines` with no signal — only *non-ASCII* leading digits
warned. Now an ASCII token that starts numeric but isn't a number
(`40a`, `20Hz`) warns `junk_numeric_line:<n>`; the line is still
preserved verbatim in provenance.

### F4 — E-APO keyword-present-but-unparseable silently nulled fields — FIXED

`Filter 1: ON PK Fc 2e3 Hz Gain -1 dB` previously returned
`frequency_hz=None` — a band with no frequency looked like a
field-missing band. And `Preamp: boost 3` matched `3` mid-string via
`re.search`. Now: an `Fc`/`Gain`/`Q`/`BW` keyword whose value fails to
parse turns the whole filter line opaque (`malformed_line` +
reason), and `Preamp`/`Delay` numbers anchor to the start of the
argument (`re.match`); `+`-signed values (`Preamp: +2.5 dB`,
real-world hand edits) parse correctly.

## Deferred / documented, not fixed

- **Inferred IR sample rate carries the file's quantization**:
  `ir_2col.txt` at 9 decimals infers `48000.77` (the file literally
  says `0.000020833` s steps), and `ir_csv.csv` infers `44099.5`. The
  parser reports what the file encodes rather than snapping to a
  "pretty" rate — faithful-but-imprecise is a property of the source
  data, and the declared-metadata lane (F1) is now the exact path.
- **CSV analysis export is `.12g`-lossy by design** (~13 sig digits);
  the JSON renderer is the byte-exact lane. Both documented; no
  silent re-quantization occurs — the formatting is a declared
  renderer choice, applied identically every time.
- **E-APO command names are case-sensitive** (`channel: C` → opaque
  `unsupported_command`). Real E-APO is documented as
  case-insensitive for some commands; HTDT conservatively keeps
  unparseable lines opaque rather than guessing — honest, no data
  loss (raw text retained).
- **UTF-16LE rejection message is generic** ("at least two rows"
  / decode error) rather than naming the encoding. Rejection is
  honest; only the wording could be better.
- **`captured_at=None` for text imports** is honest (the file has no
  timestamp field); REW `Dated:` headers survive verbatim in
  provenance instead.
- **`Filter n: ON LP 1000`** (bare value without `Fc`/`Hz`) still
  parses with `frequency_hz=None` — E-APO grammar doesn't define bare
  numbers as frequencies; treated as a field absent from the line
  rather than a corrupted keyword.
- **Semantic replay is pinned to the current parser for its version
  label**: `PARSER_VERSION`/`REW_IR_PARSER_VERSION` were intentionally
  left unchanged (the established convention — `non_ascii_numeric_line`
  shipped under `rew-text-1`), because the seal checks
  `dataset.importer_version` against the re-derived label and a bump
  would invalidate every previously imported dataset. The remaining
  edge: a dataset imported before this change whose raw file triggers
  a *new* warning (`junk_numeric_line`, `ir_commented_row`,
  `ir_declared_*`) re-derives a different `processing_json` warnings
  field and fails closed on the next authoritative read — fail-closed
  by design; re-importing the raw file resolves it.

## Files changed

- `backend/src/htdt/cad_measurement_ir.py` — `//` metadata lane,
  declared fields, quantization-aware uniformity, ambiguity guard
  for comma-integer rows, `ir_commented_row` warning.
- `backend/src/htdt/rew_parser.py` — `junk_numeric_line` warning for
  ASCII digit-prefixed junk rows.
- `backend/src/htdt/cad_external_calibration.py` — anchored number
  parsing, `+` sign support, keyword-present-unparseable → opaque.
- `docs/reviews/round15-io.md` — this document.

## Tests

`python -m pytest backend/tests/test_measurement_authorities.py
backend/tests/test_cad_external_calibration.py
backend/tests/test_import_truth_matrix.py -q` — 160 passed, 1 skipped.
Full suite: `python -m pytest backend/tests -q -n 4` (see PR body).
