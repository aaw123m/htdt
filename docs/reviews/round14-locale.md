# Round 14 — Locale / Non-ASCII / Encoding Truth

Scope: this is a Japanese-localized app running on Windows where users have
non-ASCII usernames (`C:\Users\田中`), save projects with JP names, and export
to folders with full-width characters. Method: drove the **real** backend
journeys under a live non-ASCII tree (`C:\t\テストプロジェクト\…`) — scene
open/save, REW import with a JP filename, report/SVG/CSV export to a JP-named
folder, backup + restore to a JP path, project-bundle export/import, and
data-dir relocation with a JP bootstrap pointer. Then audited every
`decode(..., errors='replace')` / encoding boundary and every place numeric
parsing or collation could silently accept, mangle, or mojibake JP input.

Journey driver: `C:\t\journey_locale.py`, 20 checks, all PASS
(`C:\t\locale_journey.log` records the real JP strings; console output is
ASCII-sanitized so the cp1252 console cannot mask mojibake).

## Non-ASCII path journey — all green

| Operation | Path/anchor | Result |
|---|---|---|
| Scene save + reopen | `テストプロジェクト\データ\cad-scenes.sqlite3` | revisions persist; JP entity names roundtrip |
| Project library display name | `メインシアター・和室１２３` | stored + returned intact in recent list |
| REW import, JP filename | `測定_前面左.txt` under `測定ファイル\` | raw filename + bytes preserved |
| Project bundle export/import | `和室バンドル.htdtproject` → `復元先\` | entity names intact after re-import; zip member names all UTF-8 clean |
| Native backup + restore | `バックアップ.htdt-backup` → `復元データ\` | manifest validates, DB opens |
| Data-dir relocation | dest `引っ越し先\新データ`, bootstrap `ブート設定\htdt-bootstrap.json` | `data_dir` written with JP chars verbatim (UTF-8 JSON) |
| Analysis export, equipment catalog, report HTML | JP-named dirs + JP titles | written atomically, contents verified |
| SOFA profile under JP dir | `和室測定.sofa` via h5py | filename + attrs roundtrip |

No raw-bytes path, no `UnicodeEncodeError`, no fail-silent write anywhere on
the journey — Windows path objects are passed through `pathlib` end to end.

## Findings and fixes

### F1 — CamillaDSP config importer silently persisted mojibake — FIXED

`cad_camilladsp.load_camilladsp_config` decoded with
`utf-8-sig, errors='replace'`: a cp932-authored config (what JP Windows
Notepad "ANSI" produces) imported **successfully** with U+FFFD garbage —
observed title `atB^[` persisted into the imported artifact. The
user sees a "successful" import of corrupted data. Now strict:
undecodable input raises `CamillaDSPError('unsupported_encoding',
'config must be UTF-8 text')`.

### F2 — Equalizer APO artifact decode silently mojibaked channels — FIXED

`cad_external_calibration.build_equalizer_apo_artifact` had the same
`errors='replace'` on both the main file and every `Include:` target. A
cp932 main file produced an artifact whose channel map was garbage; a cp932
include produced channel `O` plus a mojibake `channel O has no HTDT
mapping` diagnostic. Now: main file → `ValueError('source is not UTF-8
text')`; include target → recorded as an *unresolved* `IncludeDependency`
(sha256 preserved for evidence) with `diagnostics=('include target is not
UTF-8 text',)` and a top-level `undecodable include: <path>` diagnostic —
honest, and consistent with how the module already reports missing
includes.

### F3 — REW parser accepted non-REW numeric dialects — FIXED

Data-row classification used `float()`, which accepts Unicode digits
(`float('２０') == 20.0`), underscores (`1_0`), and `inf`/`nan`. A pasted
`２０ ７０ / ４０ ７１` block was silently ingested as data rows
`(20.0, 40.0)×(70.0, 71.0)`; a full-width `．` decimal split the row into a
comment and vanished. Now a `_REW_NUMBER` ASCII-decimal regex
(`[0-9]`, not `\d` — Python `\d` matches Unicode digits) must fullmatch
every token in a numeric-starting row, else `RewParseError('Line N:
non-REW numeric characters')`. A *header* line leading with a non-ASCII
decimal digit (e.g. `２０２４年３月測定`) is still a comment but now records
provenance warning `non_ascii_numeric_line:<n>` instead of silently
disappearing. `NaN`/`Inf` and `1_0` are rejected as non-dialect too.

### F4 — CSV exports were BOM-less UTF-8; docs claimed utf-8-sig — FIXED

Round-13's exports review documents analysis CSV as `utf-8-sig`, but
`write_text_atomic` defaulted to plain `utf-8` and the read-back tests
decode with `utf-8-sig` (which tolerates missing BOM) — the claim was
never true. A BOM-less UTF-8 CSV opened in Excel on a JP machine decodes
as cp932 → mojibake. Now:

- `export_io.write_export_files` gains `bom_suffixes`; the two grouped
  exports pass `('.csv',)` (`{stem}_export.csv`, `{stem}_settings.csv`).
- Comparison CSV (`measurement_page_workspace._export_saved_comparison`)
  writes `encoding='utf-8-sig'`.
- Handoff package CSV members (`dimension_sheets.csv`, `settings.csv`,
  `installation_coordinates.csv`) carry a leading U+FEFF in their content,
  so the manifest digests hash exactly the BOM'd bytes on disk. JSON
  members and the manifest stay BOM-less.

## Audited clean

- **Path layer**: `pathlib` everywhere; zip arcnames ASCII+digest;
  `capture_bundle` validates NFC/casefold of member names.
- **Ingress**: REW text is byte-bounded then `utf-8-sig` strict; REW
  API/roomsim JSON arrives already decoded.
- **Strict-honest decoders already correct**: `cad_measurement_ir`,
  `raw_mesh`, REW `_decode`.
- **`errors='replace'` kept, deliberately**: `room_underlay` DXF reader
  tries strict UTF-8 first and only falls back for its ASCII group-code
  subset (non-UTF8 bytes land in ignored text, never in numbers);
  `cad_cec_adapter` OSD name and `cad_adm_bw64` ASCII protocol fields are
  spec-ASCII; `cad_spatial_reproduction` SOFA attrs — see deferred.
- **Subprocess/env**: all invocations are list-form `argv` (no shell
  joins), so non-ASCII args pass through unchanged on Windows.
- **Locale-dependent formatting**: `strftime`/`strptime` uses are
  fixed-format ISO stamps; `_csv_number` uses `.12g` (locale-invariant,
  `.` decimal always).
- **Sorting/collation**: no locale-sensitive name sort exists — project
  library, measurement lists, and exports order by seq/created_at/ID,
  which is deterministic and sane for JP names (no codepoint-ordered
  "alphabetical" list to fix).
- **Error display**: exception text is never shown raw — `warn_user`
  maps to localized messages; JP paths/names appear in details/logs
  correctly.
- **Input edge**: JP punctuation and full-width characters are accepted
  verbatim in entity names, measurement labels, project names
  (`speaker-…名前` entity ids, `解析・周波数特性比較１２３` titles all
  roundtrip).

## Deferred

- `cad_spatial_reproduction._as_text` (`errors='replace'` on SOFA
  attribute strings): SOFA attrs are spec-ASCII; a malformed non-UTF8
  attr would surface as U+FFFD in a display name — cosmetic, no numeric
  corruption. Not worth a behavioral change this round.
- SQLite `PRAGMA encoding` — databases are UTF-8 by construction; no
  per-db switch exists to misconfigure.

## Verification

- `C:\t\journey_locale.py` — 20/20 PASS under `C:\t\テストプロジェクト`.
- `C:\t\verify_locale_fixes.py` — 18/18 PASS (rejects, warnings, BOM bytes).
- `pytest backend/tests/test_parser.py test_cad_camilladsp.py
  test_cad_external_calibration.py test_cad_external_admission.py
  test_external_dependency_resolver.py test_installation_handoff.py
  test_analysis_export.py test_csv_export.py test_round13_export_truth.py
  test_import_truth_matrix.py test_security_review_round1.py -q -n 4`
  — 194 passed, 1 skipped.
- Full suite `pytest backend/tests -q -n 4` — see PR body.
