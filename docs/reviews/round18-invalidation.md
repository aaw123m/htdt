# Round 18 — derived-data invalidation

Scope: when source data changes, do downstream artifacts get correctly
invalidated/recomputed? Hunt list per assignment: edit a
measurement/IR/entity → do cached analyses, comparisons, previews, reports
recompute or serve stale; undo/redo → derived caches invalidated
consistently; import-over existing data → consumers see the new version;
hash-keyed caches — does the key actually cover the data it claims;
persisted artifacts → signature/version checks honest when inputs changed;
staleness flags — set but never surfaced, or cleared without recompute;
spec-sha / evaluation-sha bindings — hash input construction with missing
fields. Method: trace each invalidation chain end-to-end from pin to
consumer, then mutate input and assert the downstream output changes or
honestly reports staleness (`backend/tests/test_round18_invalidation.py`,
`QT_QPA_PLATFORM=offscreen`, Python 3.12, Windows). Branch
`devin/rev18-invalid`.

Prior coverage assumed (rounds 9–17): undo/dirty tracking (12, 14),
provisional-hash sealing (13-hash-sweep), import honesty (13-imports),
data safety (9), boundedness (17).

## Suspicions → verdicts

| # | Suspicion | Traced | Verdict |
|---|---|---|---|
| 1 | **`assess_currency` snapshot-drift check is dead code** | **CONFIRMED** — `PredictionMatrixService.assess_currency` passed `current_snapshot_sha256=spec.acoustic_scene_snapshot_sha256` — the spec's *own pin* — so `spec.acoustic_scene_snapshot_sha256 != current_snapshot_sha256` could never be true. The `AcousticSceneSnapshot.semantic_sha256` covers boundary configs, excitation/screen/treatment bindings, environment, operating state and domains — it can rotate while `scene_content_hash` is identical, so the matrix could render ready cells against a superseded snapshot while reporting CURRENT. The providers' `current_authority.acoustic_scene_snapshot_sha256` (the live authority `create_matrix` itself pins from) was already available and ignored | **Fixed** |
| 2 | **Unverifiable bindings read as exact** | **CONFIRMED** — `assess_matrix_currency` guarded per-source/receiver drift with `current is not None`: a spec source or receiver absent from a non-empty binding map was silently skipped, so a column/row whose binding could not be re-verified stayed CURRENT. Same for a truthy-but-unmatched providers dict (empty map → all checks skipped) | **Fixed** |
| 3 | **Activity-center staleness machinery unreachable** | **CONFIRMED** — `input_authority_refs`, `note_authorities_changed`, `COMPLETED_FOR_HISTORICAL_INPUT` and `RESULT_STALE` existed but no production caller ever supplied input refs or notified a change (only tests did). A backup op in the activity history could never be marked historical even after a restore replaced the whole managed tree | **Fixed** |
| 4 | `result_trust_adapters` freshness optimistic | `_freshness_for_scene_bound` proves CURRENT only by exact head comparison of `revision_id` + `content_hash`; degrades INCOMPLETE_DEPENDENCY/STALE/HISTORICAL — never optimistic | Clean |
| 5 | Prediction preflight admits stale scope | `build_prediction_execution_preflight` compares `scope.scene_content_hash` to the live head → `STALE_SCENE`/`STALE_INPUT`, and a stale preflight cannot be ADMITTED | Clean |
| 6 | `result_is_current` skips env/variant drift | `room_prediction` checks `source_revision_id` + `scene_content_hash` + `constraint_workspace_hash`, and env-profile sha for non-provider results; `_provider_evidence` fails closed | Clean |
| 7 | Design-comparison refs loose | `ComparisonAlternative`/`DesignComparisonSet` pin exact `scene_revision_id`, `scene_content_hash`, variant/checkpoint/as-built refs with sha-verified `alternative_sha256`/`set_sha256`; `evaluate_comparison_set` reports `incompatible_baseline`/`semantic_hash_conflict`/`foreign_project`/`context_mismatch`/`unresolvable` honestly | Clean |
| 8 | Measurement comparison validation | `cad_measurement_repository._validate_current_comparison` re-verifies dataset hashes, revision bindings and replays the algorithm — fails closed on altered evidence | Clean |
| 9 | Undo/redo leaves derived caches dirty | `WorkingDocument` `is_dirty` is a memoized `scene_content_hash` vs `_saved_hash` ORed with the sidecar digest; `_content_hash_cache` cleared on `_document` swap; CommandHistory push/undo/redo re-derives attached positions | Clean |
| 10 | lru_cache keys mutable/wrong | `comparison.py` caches are pure-function keyed on grid tuples; `cad_objective_repository._validated_evaluation` memo is keyed by `(evaluation_id, evaluation_sha256)` and still re-checks row equality; `cad_joint_execution` authority memo is run-scoped | Clean |
| 11 | `cad_schema` signature memos serve stale | Keyed by `(dev, ino, mtime_ns, ctime_ns, size)` — sound because the store never enables WAL (journal sidecars invalidate on write) | Clean |
| 12 | `managed_data_fingerprint` misses asset changes | Covers db stat + SQLite header change counter (bytes 24-27, bumps per commit) + `-wal`/`-shm`/`-journal` stats + flat `measurement-assets/` dir (count/bytes/newest-mtime); assets are flat digest-named files so `iterdir()` covers them all | Clean |
| 13 | Restore preview stale after archive change | `RestorePreviewStaleError` on manifest drift; post-restore migration is routed through the journaled upgrade lifecycle | Clean |
| 14 | Other currency helpers pass pins as "current" | `field_explorer_session_currency` (live `revision` from the panel), `assess_object_promotion_currency` (live `revision`), `assess_auralization_currency` / `assess_session_currency` (caller-supplied current authorities; no production caller feeds pins back as current) — matrix service was the only dead-check instance | Clean |

## Finding details

### Matrix snapshot-sha tautology (prediction_matrix_service.py)

`assess_currency` now derives the "current" snapshot from the supplied
providers' `current_authority.acoustic_scene_snapshot_sha256`:

- all providers agree on one sha → that is the comparison input;
- a provider lacks the authority, or providers disagree → `None` is
  passed, which the currency check reads as "snapshot cannot be proven
  current" and marks STALE (fail closed);
- no providers at all → the pin is echoed as before (unverifiable — same
  degradation as the binding checks, which also skip without provider
  evidence).

`create_matrix` requires every provider to share one snapshot at pin
time, so divergence at assess time is itself drift.

### Unverifiable bindings (cad_prediction_matrix.py)

`assess_matrix_currency` now treats a non-`None` binding map as the full
re-verification surface: a spec source or receiver absent from it adds a
`'matrix source/receiver binding cannot be re-verified for: …'` reason
and stales that column/row — distinct from `'binding changed'`, since
"cannot re-verify" is not "changed". `None` maps (caller supplied no
provider evidence) still skip the per-cell checks entirely.
`current_snapshot_sha256` is now `str | None`; `None` fails closed.

### Activity-center staleness wiring (data_management.py, workflow_application.py)

- Every `DataManagementController` operation pins
  `input_authority_refs=('managed-data:<fingerprint>',)` —
  `managed_data_fingerprint` at submit (db stat + header change counter +
  journal + assets dir; cheap, stat-level).
- `_MUTATING_DATA_KINDS` = restore / relocate / storage GC. On both the
  success and failure completion paths, `_note_superseded_inputs`
  recomputes the fingerprint; when it differs from the pinned one it
  calls `note_authorities_changed` with the superseded ref, so every
  operation bound to the pre-mutation state flips
  `COMPLETED`→`COMPLETED_FOR_HISTORICAL_INPUT` (or
  `current_for_input=False` while still active). Comparing fingerprints
  means a failed op that left the tree intact marks nothing — and a
  mid-op failure that did mutate still marks, fail-closed.
- The mutating op itself pinned the pre-mutation fingerprint, so it ends
  `COMPLETED` with `current_for_input=False` — literally true: the
  managed state it referenced no longer exists, and the mutation is its
  result rather than stale output.
- `automatic_backup` operations pin the same ref at submit, so a restore
  reclassifies earlier auto-backups as historical too.

## Regression probes (backend/tests/test_round18_invalidation.py)

6 tests: snapshot drift stales the whole matrix (control: unchanged
providers → CURRENT); divergent provider snapshots fail closed; a spec
source missing from the supplied map stales its column; a spec receiver
missing stales its row; a scan result pinned to the pre-mutation
fingerprint reclassifies `COMPLETED_FOR_HISTORICAL_INPUT` after a
mutating op changes the fingerprint (the mutating op stays COMPLETED,
honestly not-current-for-input); and a no-mutation run leaves earlier
results CURRENT (no false staleness).

## Deferred

- **Scene-edit granularity on the activity center**: ordinary document
  mutations (scene commands) do not call `note_authorities_changed` — a
  completed backup stays "current" until the next mutating
  data-management op recomputes the fingerprint. Wiring every
  CommandHistory commit into the center is a broad change; the history
  is app-local diagnostics, not evidence authority, and the pin is
  re-checked honestly at each lifecycle event.
- **`matrix_presentation()` snapshot blind spot**: the presentation path
  assesses without providers, so snapshot drift is unverifiable there
  (scene-hash staleness still shows) — same documented degradation as
  the binding checks.
- **`ActivityCenter.retry()` pin inheritance**: retry copies
  `input_authority_refs` verbatim; a retried op inherits the superseded
  pin until the next note event. No production retry caller exists yet;
  re-pinning belongs in the domain adapter's `retry_factory` when one
  lands.
- **Unregistered operation kinds**: prediction/search/measurement jobs
  are not mirrored into the ActivityCenter at all, so their results have
  no input pin to stale — a registration gap, not an invalidation bug.
