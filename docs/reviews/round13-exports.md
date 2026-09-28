# Round 13 — exported artifact truth

Scope: do the files the app exports actually contain what they claim,
byte-for-byte? Every export surface was enumerated; each writer was
verified by generating a small known input, exporting, and parsing the
produced file back — bytes, rows, samples — not just checking that a file
exists. Round-trips through every matching importer were exercised, and
partial-failure honesty was probed by interrupting writes mid-stream.
Branch `devin/rev13-export`. Python 3.12.10, pytest,
`QT_QPA_PLATFORM=offscreen`. Verification harness: `verify_exports.py`
(82 content checks) + `verify_atomicity.py` (11 torn-write demos).

## Method

1. Enumerated every writer that produces a file a user or external tool
   can open: `*.py` `write_text`/`write_bytes`/`ZipFile`/`QImage.save`/
   `shutil.copy`/`export(` audit across `backend/src/htdt`.
2. For each writer: staged known data → exported → parsed the artifact
   independently (csv.reader, json.loads, wave/RIFF header walk, zip
   member iteration + digest check, PNG magic + QImage decode).
3. Round-trips: exported → imported back → compared field-by-field.
4. Partial-failure: injected a crash mid-write (monkeypatched writer)
   and classified what remains on disk — absent, torn-but-detectable,
   or torn-but-indistinguishable-from-complete.
5. Filename honesty: overwrite prompt flow, stem claiming, parent-dir
   creation, unwritable/missing-directory behavior.

## Export surface → verified table

| Export | Writer | Verified contents | Result |
|---|---|---|---|
| Analysis CSV | `render_analysis_csv` → `write_text_atomic` (`measurement_page_workspace._export_saved_comparison`, workflow CSV export) | utf-8-sig; every series row incl. `comparison:<id>:a`, `:b`, difference; `.12g` precision; `=`-prefix cells escaped (`'=`) — Excel-formula injection blocked | OK |
| Analysis JSON | `build_analysis_export` → `write_text_atomic` | All series + points; NaN/Inf forbidden (`allow_nan=False`) | OK |
| Analysis HTML | `render_analysis_html` → `write_text_atomic` | Embedded JSON payload byte-equal to the `.json` sibling; both polylines present in SVG (`class="a"` and `class="b"`) | OK |
| Comparison CSV (per-comparison export) | `workflow_application` + `write_export_files` | `claim_export_stem` no-clobber (`-2`, `-3`, …); `FileExistsError` + rollback when a member exists | OK |
| Comparison plot PNG | `measurement_page_workspace._export_difference_plot_png` | `ImageExporter.export` → `QImage.save` bool **was ignored** — silent failure reported as 成功. Now checked + staged temp + `os.replace`. | **BROKEN → FIXED** |
| Biquad settings JSON + CSV | `_export_calibration_settings` → `write_export_files` | 17-col CSV (per-cell biquad coeffs), JSON field-equal; both files published or neither | OK |
| Project bundle `.htdtproj` | `project_bundle` (mkstemp+os.replace) | manifest digests match member bytes; `assets/<sha256>` members byte-equal; re-import round-trips | OK |
| Native backup `.htdt-backup` | `native_backup` (temp+os.replace) | manifest + member digests verified; no undeclared members | OK |
| Installation handoff package | `installation_handoff` (staged+verify+promote+rollback) | manifest digests; entities CSV restricted to supported kinds (speaker/seat/screen/projector/measurement_point — furniture excluded by design); `x_m` fields in metres | OK |
| Equipment catalog snapshot | `equipment_catalog_export` (NamedTemporaryFile+os.replace) | `byte_count`, `sha256`, definition identity + `semantic_sha256`, JP labels intact, UTF-8 bytes | OK |
| Auralization WAV | `CadAuralizationRepository.save_artifact` → managed asset store | RIFF header walk: mono, sampwidth=2 (s16le), declared rate, frame count == declared; decoded samples equal source quantized; sha256 verified on write | OK |
| External mesh `.meshbin` | `serialize_external_reference_mesh` / repo blob store | serialize→deserialize round-trip exact; canonical bytes deterministic | OK |
| Diagnostics zip | `support_diagnostics._staged_zip_archive` (mkstemp+os.replace) | members complete; planted log bytes byte-identical (CRLF preserved); per-member status recorded in manifest | OK |
| Mission package | `capture_receiver.mission_package_bytes` → `write_bytes_atomic` | declared digest+size verified vs bytes; exported file byte-identical; parent dirs created | OK |
| Pre-migration backup zip | `migration_guard._create_pre_migration_backup` | **Was** `ZipFile(path, 'x')` straight to destination: `ZipFile.__exit__` writes a central directory even on exception → a crashed write leaves a *member-incomplete* archive that opens cleanly and reads as a complete backup. Now staged + `os.link` published (atomic, keeps no-clobber 'x' semantics). | **BROKEN → FIXED** |
| Treatment CAS asset (`measurement-assets/<sha256>`) | `cad_acoustic_treatment_repository.save_source_asset` | Was `target.write_bytes(data)`: torn write leaves `<sha256>` file whose bytes ≠ digest → next identical save misreports 'hash collision'; `verify_managed_asset` fails on the residue. Now `write_bytes_atomic`. | **BROKEN → FIXED** |
| File-adapter materialization `mat:<id>.json` | `cad_device_adapter_file.materialize` | Was plain `write_text`; `mat:` makes the name an NTFS ADS on Windows where rename-atomic publishing is impossible — now direct write + read-back verification so a torn write raises instead of silently persisting. | **BROKEN → FIXED** |
| Recovery launch metadata | `startup_recovery._store_metadata` | Was `write_text` → torn JSON → `load_recovery_metadata` catches `ValueError` → returns *empty* metadata → crash history silently lost. Now `write_text_atomic`. | **BROKEN → FIXED** |
| Window state | `window_state.save_window_state` | Was `write_text` → torn JSON → warned + defaults (honest degrade, but avoidable). Now `write_text_atomic`. | FIXED |
| Receiver TLS cert/key pair | `capture_receiver.start` + `_ensure_certificate` | Was `cert_path.write_bytes`/`key_path.write_bytes` under an `exists()` guard, and openssl wrote directly to the canonical paths — a torn pair was kept forever (`exists()` → never regenerated) and `load_cert_chain` failed every start. Now openssl output is staged + `os.replace`d, in-memory writes are `write_bytes_atomic`, and `load_cert_chain` SSLError deletes + regenerates once. | **BROKEN → FIXED** |
| PFFDTD vendored-source patch | `acoustic_pffdtd_adapter` | Was `write_text` in place — torn patch leaves corrupt source (post-write hash check *does* catch it, but the file is left damaged). Now `write_text_atomic`. | FIXED |
| Solver scratch `pffdtd_model.json` | `cad_candidate_wave_execution` | Was `write_text` into the per-run scratch dir — torn model file → solver reads corrupt input. Now `write_text_atomic`. | FIXED |
| Bakeoff readiness `--output` | `acoustic_bakeoff_readiness` | Was `write_text` — truncated JSON artifact. Now `write_text_atomic`. | FIXED |
| R130D evidence envelope | `r130d_general3d_validation` | Was `write_text` — truncated evidence envelope. Now `write_text_atomic`. | FIXED |
| Perf benchmark manifest/report | `project_performance.write_benchmark_artifacts` | Was `write_text` pair — torn manifest/baseline JSON. Now `write_text_atomic`. | FIXED |
| Legacy-migration crash journal | `legacy_data` | Was `write_text` — presence-only sentinel so already fail-safe, now atomic for stronger semantics. | FIXED |
| `.htdtcapture` delivery staging | `capture_receiver._ingest_delivery_payload` | TemporaryDirectory staging + sha256 manifest check before ingest | OK |
| `report.json`/`report.html` | `main.py` endpoints | In-memory render → HTTP body; `filename="htdt-comparison-<id8>.<ext>"`; SVG chart filters non-finite triples jointly and draws **both** series polylines | OK |
| `application_preferences`, `activity_center`, `file_dialog_memory`, `reference_libraries` state | mkstemp+fsync+os.replace | byte-exact reads after write | OK |
| `launch_intents`, `runtime_instance`, `native_upgrade`, `automatic_backup`, `data_relocation` | temp+os.replace (+dir-fsync for relocation) | atomic | OK |
| `commissioning_plan`, `standards_profile_editor` | `write_text_atomic` | atomic | OK |
| Perf JSONL journal append | `project_performance` | append-mode journal — torn tail is normal journal semantics; included as-is in diagnostics packages | DEFERRED (append semantics) |

## Findings (fixed)

1. **PNG export reported success on failure (HIGH)** —
   `ImageExporter.export(fileName)` returns `QImage.save`'s bool; `False`
   on an unwritable path was ignored → success notice displayed for a
   file that was never written; a torn save could also leave a partial
   PNG masquerading as the export. Now writes to a sibling temp,
   checks the return, and `os.replace`s only on success.

2. **Pre-migration backup could be left member-incomplete yet valid
   (HIGH)** — `ZipFile(archive_path, 'x')` wrote straight to the
   destination; `ZipFile.__exit__` still writes a central directory on
   exception, so an interrupted write produces a zip that opens cleanly
   but silently lacks members — indistinguishable from a full backup
   until restore. Now staged in `backups/` and hardlink-published
   (`os.link` preserves the 'x' no-clobber semantics atomically).

3. **CAS poisoned slot → misleading 'hash collision' (MEDIUM)** — a
   torn `target.write_bytes` in `save_source_asset` left a file named
   `<sha256>` with mismatched bytes; the next identical save compared
   bytes and raised 'content-addressed ... hash collision', and
   `verify_managed_asset` reports the digest mismatch forever after.

4. **Receiver TLS pair could brick the listener forever (MEDIUM)** —
   openssl wrote cert/key straight to the canonical paths; a torn pair
   survived under `cert_path.exists()` and `load_cert_chain` raised
   SSLError on every subsequent start (never regenerated). Staged
   generation + atomic publish + regenerate-once-on-load-failure.

5. **Recovery metadata torn write silently erased crash history
   (MEDIUM)** — `load_recovery_metadata` returns empty `RecoveryMetadata`
   on `ValueError`, so a torn write looked identical to "no history".

6. **File-adapter `mat:<id>.json` torn write persisted corrupt JSON
   (LOW)** — ADS names cannot be rename-atomically published on
   Windows; the write is now verified by reading the file back.

## Verified not issues

- **SVG all-series**: `_svg_chart` renders both `class="a"` and
  `class="b"` polylines; non-finite points are dropped jointly
  (x,y,value triples), never mis-pairing.
- **UTF-8**: every JSON/CSV writer uses `utf-8`/`utf-8-sig` explicitly;
  JP strings round-trip (label `実測 A` verified in snapshot bytes).
- **CSV quoting**: `csv.writer` defaults; embedded commas/CR/newlines
  re-parse identically.
- **WAV bit depth**: RIFF walk confirms s16le mono at the declared rate;
  quantized decode equals the source samples.
- **Overwrite prompting**: `QFileDialog.getSaveFileName` native dialog
  gives OS-level overwrite confirmation; `claim_export_stem` numbers
  `-2`, `-3`, … instead of clobbering for multi-file exports;
  `write_export_files` raises `FileExistsError` and rolls back created
  members.
- **Locked-file overwrite**: `os.replace` onto an open file verified —
  exported PNG overwrites cleanly; the dialog's own overwrite prompt
  covers the UX.

## Deferred

- `project_performance` JSONL perf journal (`perf-events.jsonl`) is
  append-mode; a crash can leave a torn last line — normal journal
  semantics, consumed only inside diagnostic packages.
- `main.py` HTTP `report.*` endpoints stream in-memory renders — no
  partial-file risk, verified by code read.
