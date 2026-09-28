# Round 13 — import pipeline truth

Scope: every file/byte stream the app ingests must be **honestly parsed or
honestly rejected**. Malformed / hostile / truncated / huge inputs must never
silently corrupt data. Verified empirically — the matrix in
`backend/tests/test_import_truth_matrix.py` (1519 lines) feeds real bytes,
not mocks. Exports were covered by a sibling session; this document covers
imports only. Branch `devin/rev13-import`.

Prior rounds hardened most ingest paths already (bounded reads in
`ingress.py`, `Store.import_measurement` dedup + `dataset_sha256` (v6),
`project_bundle._validate_bundle_members` whitelist, `capture_bundle`
frozen-checksum verification). This round's job was adversarial
byte-feeding to find the residual dishonesty.

## Input → parser → verdict table

| Import surface | Parser | Truncated | Garbage bytes | Wrong encoding | Impossible values | Huge | Verdict |
|---|---|---|---|---|---|---|---|
| REW .txt (batch) | `rew_parser.parse_rew_frequency_response` / `measurement_workflow.stage_rew_text_files` | reject | reject | reject (UTF-16/cp932) | reject (neg/NaN/Inf freq, dup freq) | reject at cap | **honest** |
| REW API arrays | `rew_api.decode_rew_float_array` / `decode_frequency_response` | reject | reject | n/a | reject (leak fixed) | reject (`RewApiResponseTooLarge`) | **fixed**: tiny `ppo` raised raw `OverflowError`, not `RewApiError`; non-monotonic axis now rejected |
| IES LM-63 `.ies` | `cad_luminaire_photometric.parse_ies_lm63` | reject | reject | reject | artifact `ValidationError` leaked → now `invalid` verdict | bounded by input | **fixed**: pydantic `ValueError` now mapped to `invalid`; dead `units_type`→units code removed (LM-63 candela is always absolute `candela`) |
| EULUMDAT `.ldt` | `cad_luminaire_photometric.parse_eulumdat` | reject | reject | reject | **silent unit invention** | bounded by input | **fixed**: unknown measurement-type (`dtype`) silently labeled `candela_per_klm` — now `invalid`; pydantic `ValueError` → `invalid` |
| JCAL params CSV | `cad_foam_material_batch.parse_jcal_params_csv` | reject | reject | reject | IndexError | bounded | **fixed**: empty CSV → `IndexError`, one-column rows → `IndexError`; now `ValueError` with the offending parameter name |
| CGATS .ccmx/.ccss | `cad_meter_correction.import_meter_correction` / `parse_cgats_document` | reject | reject | **raw `UnicodeDecodeError`** | reject (field/set-count lies, short rows) | bounded | **fixed**: non-UTF-8 now `CgatsParseError` per the module contract (was raw `UnicodeDecodeError` — still `ValueError`-family, honest but off-contract) |
| Spectral XML (TM-27/33 `.spdx`) | `cad_spectral_lighting.parse_spectral_xml` | reject | reject | reject | reject | bounded | **honest** — DOCTYPE/ENTITY rejected up front; corrupt spectral table + good CCT degrades to `valid` with no invented samples |
| WAVE report JSON | `cad_wave_qualification.import_wave_report_json` | n/a | reject | reject | `incomplete`/`invalid` | bounded | **honest** |
| SOFA / HRTF | `cad_spatial_reproduction.load_sofa_dataset_profile` | reject | reject | reject | reject (wrong convention, missing vars, malformed IR) | bounded | **honest** |
| RAW visual mesh (OBJ/PLY/STL/GLB/HTDTMSH1) | `raw_mesh.import_raw_visual_mesh` | reject | reject | reject | reject (NaN verts, oob faces, STL count lies, GLB length lies, GLB v1) | bounded | **honest** |
| DXF underlay | `room_underlay.parse_dxf` | reject | → empty segments | reject | reject | cap → `truncated` flag (MAX_DXF_SEGMENTS=20000) | **honest** |
| Image underlay | `room_underlay.decode_image_bytes` | n/a | `UnderlayImportError` (JP message) | reject | reject | bounded | **honest** — raises JP error on undecodable bytes |
| PDF underlay | `room_underlay.render_pdf_page` | `render` error | reject | reject | page index clamped | bounded | **honest** |
| BW64 / ADM audio | `cad_adm_bw64_validator.parse_bw64_structure` | reject | reject | reject | reject (axml DOCTYPE rejected) | bounded | **honest** |
| FIR filter taps | `cad_fir_filter.import_fir_filter_artifact` / `_parse_tap_text` | reject | reject | reject | reject (NaN, empty, undeclared format) | bounded | **honest** |
| CamillaDSP YAML | `cad_camilladsp.load_camilladsp_config` | reject | reject | reject | reject (not a mapping) | `max_bytes` cap | **honest** |
| EDID binary | `edid_conformance.parse_edid` | reject | reject | reject | reject (bad length, bad checksum, ext-count mismatch) | fixed-size | **honest** |
| CLF directivity | `cad_loudspeaker_interchange.qualify_clf` | reject | reject | reject | `unqualified`/`not_clf` | bounded | **honest** |
| Polar-table directivity CSV | `cad_directivity_import.import_directivity_asset` | reject | reject | reject | `REJECTED`/`UNSUPPORTED` (incomplete grid, NaN, dup keys, schema-mismatch) | bounded | **honest** |
| Normalized-JSON directivity | `cad_directivity_import.import_directivity_asset` | reject | reject | reject | `REJECTED`/`UNSUPPORTED` | bounded | **honest** |
| Field-return bundle | `field_return_ingestion.stage_field_return` | reject | reject | reject | `malformed`/`unsupported` | bounded | **honest** |
| Capture bundle (`.zip`) | `capture_bundle.FrozenBundle` | `CaptureBundleError` | `CaptureBundleError` | reject | reject (traversal, dup member, absolute path, casefold collision, zip-bomb, symlink dir) | bounded | **honest** |
| Project bundle | `project_bundle._validate_bundle_members` | reject | reject | reject | whitelist only (`manifest.json`, `db/*.jsonl`, `assets/<sha>`) | bounded | **honest** |
| Native backup | `native_backup.inspect_backup` | reject | reject | reject | reject | bounded | **honest** |
| Store `import_measurement` | `database.Store` | n/a | reject | reject | reject (hostile filename → suffix-capped, confined to `assets/`) | bounded | **honest** — dedup: identical re-import → `duplicate_asset=True`, no double-rows, blob byte-identical, `dataset_sha256` + `integrity_valid` populated on both rows |
| `read_file_bounded` | `ingress.py` | `OSError` missing / `IsADirectory` | n/a | JP/emoji/spaces OK | `IngressTooLargeError` at exact cap+1 | **capped** | **honest** — >260-char paths fine on this filesystem |
| Batch multi-file | `stage_rew_text_files` | — | per-file `failed` | — | per-item status | bounded | **honest** — one bad file → `failed` with per-file error; siblings still `staged` |

## Findings (proven, then fixed)

1. **`rew_api.decode_frequency_response`** — hostile `ppo=1e-300` raised
   raw `OverflowError`, escaping the `RewApiError` contract a caller
   would catch. Fixed: `OverflowError` → `RewApiError('REW frequency
   axis overflowed the reported spacing')`; added strictly-increasing
   axis check (matches the text parser's contract).
2. **`cad_luminaire_photometric.parse_eulumdat`** — unknown measurement
   `dtype` (e.g. `'3'`, `''`) silently defaulted to `candela_per_klm` —
   a cd/lm or lumens-per-lumen file would be recorded with invented
   units. Fixed: dtype not in `{1: cd/klm, 2: cd}` → `invalid` verdict.
3. **`parse_eulumdat` / `parse_ies_lm63`** — `PhotometricArtifact`
   `model_validator` raised pydantic `ValidationError` (a `ValueError`)
   past the parser boundary instead of the `invalid` verdict both
   parsers advertise for hostile content. Fixed: construction wrapped
   → `invalid` with detail.
4. **`cad_foam_material_batch.parse_jcal_params_csv`** — `rows[0]` on an
   empty/blank CSV → `IndexError`; a one-column row → `IndexError` at
   `row[1]`/`float(row[1])`. Fixed: empty → `ValueError`, short row →
   `ValueError` naming the parameter; non-numeric value → `ValueError`
   naming it.
5. **`cad_meter_correction.import_meter_correction`** — non-UTF-8 bytes
   raised raw `UnicodeDecodeError` instead of `CgatsParseError` as the
   docstring's fail-closed contract states. Still honest rejection
   (`ValueError` family) but off-contract. Fixed: decode wrapped.
6. **`parse_ies_lm63`** dead code: `units_type` was read into a discarded
   `units` expression. LM-63 photometric_type is the goniometer type
   (C/B/A), not a units field — candela is always absolute. Removed the
   dead expression, documented the convention.

## Verified-no-gap

- **Dedup** — second `Store.import_measurement` of identical bytes:
  `duplicate_asset=True`, `existing_dataset_count=1`, one blob on disk
  byte-identical, `dataset_sha256` populated on every ingest row.
- **Path honesty** — `read_file_bounded` handles missing (`OSError`),
  directory, exact-cap boundary (`IngressTooLargeError`), unicode names,
  and >260-char deep paths on Windows.
- **Partial import** — `stage_rew_text_files` attributes failures per
  filename, never all-or-nothing.
- **Container truth** — capture-bundle member traversal (abs paths,
  `..`, casefold collisions, symlink dirs, dup members, zip-bomb) all
  rejected; project-bundle member whitelist enforced.
- **No silent-corruption paths found** in any tested surface beyond the
  six above; every parser either parsed faithfully or rejected with a
  typed error.

## Deferred / out of scope

- The photometric parsers (`cad_luminaire_photometric`) and spectral /
  CGATS / WAVE importers are library-boundary modules with no current
  production caller — tested at the module contract level regardless.
- 2 GB+ file behavior: every bounded-read path enforces its documented
  cap; nothing in this codebase reads unbounded streams — verified by
  cap-boundary tests, not by allocating real 2 GB fixtures.
- JP strings: the errors surfaced to UI go through the app's error
  mapper; the new parsers' error strings are English detail lines that
  upstream `UnderlayImportError`/`IngressTooLargeError`-style wrappers
  translate at the shell boundary, same as the rest of the codebase.
