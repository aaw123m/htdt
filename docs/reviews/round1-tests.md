# Round 1 review — test quality & coverage gaps

Scope: `backend/tests` (461 test files) vs `backend/src/htdt` (590 modules).
Baseline: **5,123 tests collected, 0 collection errors**, 0 xfails, 18 conditional
skips (all legitimately environment-gated). `tests/conftest.py` isolates
`LOCALAPPDATA` per xdist worker — sound. `tests/capture_fixture_support.py`
reviewed — canonical Capture Bundle v1 builders, no bugs found.

Runner: `cd backend && TMPDIR=/c/t C:/devin/python/python.exe -m pytest <files> -q -n 4`

## Findings

| Sev | Area | Finding | Status |
|-----|------|---------|--------|
| HIGH | `src/htdt/content_blobs.py` | `store_content_blob` post-write integrity check compared **length only** (`SELECT length(payload_blob)`): a forged row carrying the victim's digest + same-length junk passed silently — silent dedup on corrupt data. Reproduced, then fixed to re-hash the stored payload (`sha256(payload_blob) == digest`). | FIXED (sha pending) |
| MED | `src/htdt/cad_schema_ddl.py` | `NATIVE_SCHEMA_TABLES` registry missing `scene_revision_labels` — table created by `NATIVE_BASELINE_DDL` and used by `cad_repository`, absent from the registry the audit/migration contract tests enumerate. Added to registry; new contract test asserts every declared table exists post-migration. | FIXED |
| MED | 5 test files | **Tautological `assert X or True`** (always-true, assert nothing): `test_authority_audit_coverage.py` (rewritten as conditional invariant — `sqlite_sequence` must be `non_authority` when present), `test_cad_direct_view.py` (now a real `!=` — guarantees the save below actually exercises the reject path), `test_raw_mesh_health.py` (removed — buggy substring check; correct `blocking_codes` check already follows), `test_ui_workflows.py` (real `!=` — `semantic_payload` includes version+name so a new version must hash differently; also required by UNIQUE column), `test_workflow_application.py` (replaced vacuous `isVisible` with window-title persistence check). | FIXED ×5 |
| MED | `capture_schema_eval` (JSON-schema validator for capture bundles) | Whole evaluator untested — a money path (rejects/accepts every imported bundle). Added ~30 tests: type matrix (bool≠number, integral-float=integer), const/enum via `json_equal`, string/number/array/object keywords, asserted formats, allOf/anyOf/oneOf, if/then/else, local `$ref` with `~0`/`~1`, draft-2020-12 `$ref` sibling enforcement, external/`#`/`#/` ref fail-closed, boolean schemas, unknown-keyword fail-closed. | FIXED (new file `test_capture_schema_eval.py`) |
| MED | Capture binary codecs (`_meshbin`, `_pixelbin`, `_depthbin`, `_confidencebin`) | Parsers had no corruption coverage — fail-closed behavior is the security property. Added ~45 tests: valid roundtrips + bad magic/version/header/index_width/flags, zero dims, non-finite vertex/normal/depth, out-of-bounds index, invalid plane layout, non-contiguous payload, truncation, trailing bytes. | FIXED (`test_capture_binary_formats.py`) |
| MED | `cad_units` | Unit registry had no direct tests. Added conversion matrix, cross-family `ValueError`, `dimensionless` isolation, unknown-unit fail-closed, `units_convertible` table. | FIXED (`test_cad_units.py`) |
| MED | `native_command_adapter` | Command availability gating (undo/save/import/predict/optimize/speaker-add reason-code ladders) untested. Added 11 tests with offscreen Qt: 8-command registry, dispatch, enabled/disabled reason codes, workspace-scoped rebind, unbind → `unavailable_in_context`. | FIXED (`test_native_command_adapter.py`) |
| LOW | `test_application_pages.py`, `test_measurement_authorities.py`, `test_authority_integrity_s8.py` | Weak assertions strengthened: project-identity label now asserts text+visibility toggle; IR observation now pins raw asset sha256 (≠ dataset identity); wiring check now round-trips through repository read-back. | FIXED ×3 |
| LOW | `test_authority_audit_coverage.py` | `assert 'non_authority:scene_recovery_snapshots' in labels or any(...)` — softer than it looks (any non_authority label satisfies it). Intentional tolerance? Left as-is; flagged for owner decision. | NOTED |
| MED | ~32 `src/htdt` modules imported by zero tests | Dominated by Qt UI panels/windows (e.g. `optimization_*_controller` heavyweight windows) — needs a UI-test-harness program, not per-file tests. `server.py` is a 22-line import-side-effect launcher, covered transitively. | DEFERRED (program) |
| LOW | ~8 pydantic model files | `Field name "schema" ... shadows an attribute in parent "BaseModel"` collection warnings — renaming a serialized field is wire-format-impacting; needs schema-version decision. | DEFERRED |

## Verification

- New files: `test_content_blobs.py` (11), `test_cad_units.py`, `test_cad_schema_ddl_contract.py` (5),
  `test_capture_schema_eval.py` (~30), `test_capture_binary_formats.py` (~45), `test_native_command_adapter.py` (11) — all green.
- Edited files re-run: `test_authority_audit_coverage`, `test_cad_direct_view`, `test_raw_mesh_health`,
  `test_ui_workflows`, `test_workflow_application` — 57 tests, all pass.
- Regression on `content_blobs` callers (`test_cad_repository`, `test_capture_ingestion_transaction`,
  `test_capture_ingestion_dedup`, `test_capture_semantic_promotion`, `test_raw_mesh_repair`,
  `test_installation_output_authority`, `test_native_authority_audit`) — all pass.
