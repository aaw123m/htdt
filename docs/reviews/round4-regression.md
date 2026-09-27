# Round 4 — regression audit of the review rounds themselves + convergence check

Scope: every change merged onto `main` by the three review rounds
(`git log --grep='devin/rev' main`), audited for bugs the *reviews* introduced —
behavior changes beyond stated intent, tests pinning the bug instead of the
contract, guards that silently change semantics, removed defensive code, new
import cycles — plus a fresh repo sweep for any remaining LOW-risk improvement.

Regression tests for the fixes below live in
`backend/tests/test_application_preferences.py`
(`test_unserializable_opaque_key_dropped_not_poisoned`).

## Regressions introduced by review rounds — found and fixed

| # | Site | Introduced by | Regression | Fix |
|---|------|---------------|------------|-----|
| 1 | `frontend/src/RewReadonly.tsx` `preview()` | rev3-frontend | The stale-call guard `if (seq !== previewSeq.current) return` in the `finally` skips `setLoading(false)`. Changing the measurement selection while a preview is in flight leaves `loading=true` forever — the preview button is permanently disabled until reload. | The invalidation effect that bumps `previewSeq` now also clears the loading flag it orphaned (`setLoading(false)` in the `[selectedId]` effect). |
| 2 | `frontend/src/App.tsx` `checkMeasurementReadiness` | rev3-frontend | Same pattern: `finally { if (seq === contextChangeSeq.current) setReadinessLoading(false) }`. Switching context mid-check leaves `readinessLoading` stuck → the "照合" button stays disabled. | The `[selectedContextId]` effect that bumps `contextChangeSeq` now clears `readinessLoading`. Other `contextChangeSeq` users (`runAcoustics`, `chooseMeasurementFile`, `reloadProjectData`) have no loading flag to orphan — verified safe. |
| 3 | `backend/src/htdt/application_preferences.py` `_load` | rev3-deferred (strict `canonical_json` delegation) | Unknown (future-build) keys are carried verbatim into `_opaque_values` so they round-trip. But `json.loads` admits bare `NaN`/`Infinity`, and `_persist` now serializes `{**_opaque_values, **_values}` through `canonical_json` (`allow_nan=False`). A poisoned unknown key made *every* subsequent save raise `ValueError` — and `PARTIAL_INVALID_VALUE` is a writable state, so the store self-"repaired" by crashing on each write. | `_load` now screens each opaque value with `_canonical_json` before carrying it; unserializable values are dropped and reported through `_load_error` with `PARTIAL_INVALID_VALUE` (still writable → the next save self-repairs). |

## Audited, no regression found

- **`canonical_json` NaN sweep (the flagged hypothesis):** every caller of the
  newly-strict helpers feeds validated/finite data —
  `PreferenceDefinition.validate` rejects non-finite for defined keys,
  `AcousticEnvironmentProfile` has `allow_inf_nan=False`, search-model payloads
  are typed. Only the opaque-preference passthrough (finding 3) could admit NaN.
- **`connect_sqlite` consolidation (rev2-arch, 109 sites):** all removed call
  sites used the identical `row_factory=Row` + `PRAGMA foreign_keys=ON` trio;
  `connect_sqlite` is byte-for-byte equivalent. `_repoint_lineage_parent`
  correctly commits any open transaction *before* `PRAGMA foreign_keys=OFF`
  (the pragma is a no-op inside a transaction — handled explicitly).
- **`destroyed.connect(lambda: ...)` fixes:** both Qt sites verified; no other
  `destroyed.connect` with a bound method remains in `backend/src`.
- **`QTimer.singleShot(0, self, ...)` receiver-scoping (rev3-qt):** the one
  remaining context-free `singleShot` (`native_cad.py`) is queued before the
  event loop runs and dies with it — safe.
- **`ParetoEmptyError` narrowing (rev2):** `cad_joint_execution` catching the
  typed error instead of blanket `ValueError` is correct; repository integrity
  errors now propagate as intended, and no caller relied on the blanket catch.
- **Vectorized `_interpolate_prepared` (rev2-perf):** `searchsorted` masks and
  `math.log2` on scalar inputs verified bit-identical to the scalar loop; the
  only divergence (NaN-containing grids) is unreachable because grid inputs are
  validated monotone/finite upstream.
- **`FrozenBundle` eager re-validation, `migration_guard` bounded extraction,
  `capture_receiver` caps/timeouts, `content_blobs` read-back hashing,
  `xml_guard` byte-scan, `HTDT_LEGACY_API_DOCS` gating, `native_cad` lazy
  exports (all `setattr` test targets resolve), `scene_revision_labels` DDL
  registration, `room_viewport.deferred_render`, pyvista-0.49 renames, Qt6
  palette enum fix:** all verified fail-closed or behavior-identical.
- **`cad_measurement_disposition` identity payload:** optional fields join only
  when present, preserving legacy digests — verified against existing fixtures.

## Tests that pin the bug rather than the contract — checked

The review-added tests were re-read against their contracts. One pre-existing
docs inconsistency: round1-architecture names a probe
`_verify_calibration_evidence_event_links` while the merged code defines two
distinct probes (`_verify_calibration_evidence_event`,
`_verify_calibration_evidence_event_ref`); the round1-performance doc has the
correct names. Doc-only nit — code is correct.

## Convergence assessment — remaining LOW-risk items (not regressions)

The well is nearly dry. Everything below is pre-existing or deliberate;

- **`connect_sqlite` sweep is incomplete:** 53 raw `sqlite3.connect` calls
  across 38 files remain; 27 files open connections *without*
  `PRAGMA foreign_keys=ON`. The rev2 sweep only normalized sites that already
  had the full trio — it never added FK enforcement where it was missing.
  Safe follow-up: route the remaining sites through `connect_sqlite` after
  checking each one's FK expectations.
- **8 unbounded `read_bytes()` remain** on operator-controlled local paths
  (`managed_assets`, `project_bundle`, `support_diagnostics` log zip,
  `capture_receiver` TLS cert/key, `installation_handoff` hashing,
  treatment-repository compare). Could route through
  `ingress.read_file_bounded` for symmetry; low risk since paths are
  local/admin-controlled.
- **116 `json.dumps` sites without `allow_nan=False`** — mostly
  display/indented payloads where strictness is irrelevant; a per-site audit
  for canonical/identity uses remains possible but none showed NaN reachability.
- **Unhandled `api()` rejections** in some frontend loader effects
  (`RewReadonly.loadProject`, `MeasurementSessions.loadSessions`, etc.) — a
  rejected load promise surfaces only via console; panels are inside
  `PanelErrorBoundary` but promise rejections don't reach it. Pre-existing
  pattern, not introduced by reviews.
- **`xml_guard.contains_xml_doctype` scans raw bytes ASCII-only** — a
  UTF-16/32-encoded payload with BOM bypasses the scan, but pyexpat rejects
  doctype declarations at parse time regardless of encoding → defense-in-depth
  nit, no exploit path.
- **`golden_path_preflight` temp dirs now persist per run** — deliberate
  fail-closed trade-off from rev3; a cleanup-on-success option could reclaim
  disk.
- **`native_cad` context-free `singleShot`** — safe today (pre-exec queue),
  could gain a receiver for symmetry.

Structural items remain deferred for reasons documented in round 3 (shared
repository connections vs. Qt threading, SCC growth gate needs CI which the
repo lacks, UI test-harness program decision, god-object splits out of scope,
local-boundary auth token needs a product decision). No new HIGH/MEDIUM items
surfaced in this pass.
