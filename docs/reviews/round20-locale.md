# Round 20 — Japanese locale & encoding depth (REV20-LOCALE)

Scope: Japanese-locale surfaces on `main` (a067b539) — CSV/TSV export BOM,
collation/sorting, IME/text ingress, number/date formats, display-vs-storage
round-trips, font fallback, width/full-width-digit handling. Each suspect
path was verified by writing a real Japanese/edge-case string through the
actual code path and reading it back, not by inspection alone. Verified
locally (Windows, `QT_QPA_PLATFORM=offscreen`, `pytest backend/tests -q -n 4`,
Python 3.12.10 NuGet). Branch `devin/rev20-locale`.

**Verdict: four real gap families found and fixed** — all of them the
locale-flavoured siblings of defects earlier rounds closed at one site.
Everything else audited is either already uniform or correct by design.

## Sweep ledger

| Dimension | Result |
|-----------|--------|
| CSV/TSV/text exports — BOM & encoding | **Clean.** `export_io.write_export_files` already supports `bom_suffixes` and every export caller passes `('.csv',)` (BOM + `utf-8-sig`); `installation_handoff` writes its CSV bodies with a literal `\ufeff` prefix; `csv_export.csv_safe_cell` escapes formula prefixes. No cp932 anywhere in `src` |
| Sorting / collation on Japanese strings | **Clean.** Every audited `sorted(...)`/`ORDER BY` on user-visible strings sorts on a stored ASCII key (identity, isoformat stamp, semantic key) or a deterministic technical order; `CandidateTreeItem.__lt__` sorts the 候補/番号 columns numerically, not byte-wise. No codepoint sort reaches Japanese display text |
| IME text ingress | **Clean.** No `QValidator`/line-edit regex rejects non-ASCII anywhere; project/session names, notes, labels persist verbatim through the repositories (verified live: 'メイン案 🎧' round-trips through `SceneRepository`) |
| Number formats — decimal/thousands | **Clean for display.** `localization` JP rules use `.` decimal + `,` thousands, matching JIS/EIAJ measurement conventions; `format_decimal`/`format_bytes`/`format_percent` are the single funnel. Qt `QDoubleSpinBox`/`QInputDialog.getDouble` handle locale digits themselves |
| Date/time display | **Fixed.** `format_datetime` rendered a *UTC* instant with no zone marker (`'2026年9月23日 22:17'`); in Japan this reads as 07:17 JST wall time — off by 9 hours of operator intuition. Now appends `' UTC'` in both locales (`'2026年9月23日 22:17 UTC'`), matching `data_management_ui`'s established convention ("an unlabeled local stamp reads as a different instant"). Live via `saved_label`/`revision_display_label`/`spec_display_label`/`format_versioned_label` in `joint_optimization_panel`, `measurement_page_workspace`, `optimization_workflow_workspace` |
| Display vs storage round-trips | **Clean.** JP strings verified end-to-end through scene save/reopen, project export bundle, attachment payloads, and canonical-JSON hashing |
| Font fallback | **Clean.** `ui_theme._platform_ui_font` picks Segoe UI Variable/Segoe UI on Windows — Windows font linking supplies CJK glyphs; no hardcoded non-CJK font family found; `install_japanese_translations` loads `qtbase_ja`/`qt_ja` |
| Width handling / truncation | **Clean.** No fixed-width `setFixedWidth` on translated labels in the audited panels; `QLabel` elision and dialogs size to content |

## Findings fixed this round

**F1 — numeric import dialect silently coerces full-width digits** (sibling
of the r14 `_REW_NUMBER` fix, which noted `float()` accepts Unicode digits,
underscores and lexical inf/nan but only guarded the REW parser). Verified:
`'５０'` was silently stored as `50.0` on every one of these paths.

- New shared `strict_ascii_number` + `_ASCII_NUMBER` (`[0-9]`, not `\d` —
  `re`'s `\d` matches Unicode digits) in `ingress.py`, the module that
  already owns bounded-read ingress helpers.
- `cad_directivity_import._finite_number` now delegates to it (polar-table
  `frequency_hz`, angle columns, `magnitude`, `phase_deg`).
- `equipment_library._capability_from_source` polar rows — the
  `import_directivity` side-path that re-parsed frequency/angle cells with
  bare `float()` and bypassed `_finite_number` entirely.
- `cad_fir_filter._parse_tap_text` — tap tokens.
- `cad_foam_material_batch.parse_jcal_params_csv` — JCAL `parameter,value`
  rows (`airflow_resistivity,１２０００` was silently 12000).
- `cad_external_calibration._NUMBER` — `\d` → `[0-9]`, so
  `Fc ５０ Hz` reports "Fc value is not parseable" instead of coercing.
- `cad_camilladsp._import_filter` — `Gain`/`Delay` parameters arriving as
  YAML strings now take the strict path (non-string typed values unchanged).
- `raw_mesh` OBJ `v` lines and ASCII STL `vertex` lines — `v １ 0 0` is a
  malformed vertex, not `1.0`.
- `cad_meter_correction` — CGATS data cells and `SPECTRAL_*` values via
  `strict_ascii_number`; `NUMBER_OF_FIELDS`/`NUMBER_OF_SETS`/
  `SPECTRAL_BANDS` via `_cgats_int` (`int('５')` silently parsed).
- `cad_loudspeaker_interchange.qualify_clf` — the frequency-row counter and
  the `R(` rotation-row regex now use the ASCII dialect, so a full-width
  row no longer inflates polar coverage.

**F2 — UTF-8 BOM rejected on file-ingress paths the repo already tolerates
elsewhere.** `cad_directivity_import._parse_metadata_and_rows` decoded
`utf-8` strict, so an Excel "CSV UTF-8" polar table failed with the
misleading "must start with explicit metadata headers"; the same for
`cad_fir_filter._parse_tap_text` (`\ufeff1.0` → "could not convert"). The
sibling file decodes now match too: `cad_directivity` normalized-JSON,
`equipment_library` normalized-JSON, `cad_wave_excitation` (both the source
table and the source-response payload), `cad_loudspeaker_interchange`
(CLF), `cad_meter_correction` (CGATS) — all `utf-8-sig`, the established
read convention (`rew_parser`, `cad_camilladsp`, `raw_mesh`,
`cad_external_calibration`). `capture_bundle` *deliberately* rejects a BOM
on the bundle-manifest contract — left strict. Wire/embedded JSON decoders
(`__main__` CLI payloads, capture-receiver receipts, `rew_api` responses,
the GLB JSON chunk, `field_return_ingestion` artifacts) stay strict —
those are internal payloads, not user-saved files.

**F3 — search surfaces missing the NFKC fold.** `palette_search` and
`command_registry` established `NFKC.casefold().strip()` on both sides;
three later search surfaces only did `.lower()`/`.casefold()`:

- `_candidate_matches_filter` (shared by `optimization_search_controller`
  and `optimization_extended_controller` candidate trees) — a full-width
  '１' or half-width 'ｽﾋﾟｰｶｰ' query missed rows it should hit.
- `help_registry.search` — `search('ｽﾋﾟｰｶｰ')` returned `()` while
  `search('スピーカー')` hits; the docstring already promised
  typing either form.
- `reference_libraries.search` — same gap on `display_name`/`description`/
  `identity`/`capability_summary`.

**F4 — unlabeled UTC in the locale datetime funnel** (above).

## Regression tests

`backend/tests/test_review_round20_locale.py` — 14 tests: the strict
dialect accept/reject matrix; polar-table BOM + full-width rejection on
both the main parser and the equipment-library path; FIR tap BOM +
rejection; JCAL/OBJ/STL/APO-filter/CamillaDSP/CLF/CGATS/wave-excitation
coverage; the ` UTC` marker; and NFKC-fold coverage on the candidate
filter, help registry and reference library.

Existing expectations updated: `test_localization.py`,
`test_cad_display_labels.py`, `test_review_round19_debt.py` (the
`'…18:42 の保存'` → `'…18:42 UTC の保存'` family).

## Deferred (none from this round's scope)

- `LocalizationService` remains configuration plumbing not wired to
  widgets (r9 product decision; hardcoded JP literals are the shipped
  convention) — unchanged.
- True Japanese collation (gojūon ordering with kana-equivalence) on
  sorted lists: every user-visible sort key is a stored ASCII/identity
  key by design, so there is no surface where collation order is
  observable — listed for completeness, not a defect.
