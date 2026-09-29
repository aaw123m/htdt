# Round 17 — boundedness / memory & resource growth

Scope: everything that grows while the app simply *runs* — caches keyed by
entity/path/task, finished-work retention, bulk reads, append-only tables,
resource handles on error/close paths, GL/pixmap accumulation, hot-path
per-request allocation. Round 16 (STATE) already fixed the preference-store
zombie listeners and the dead-shell chain; this round hunted the rest of the
registry. Method: AST sweep over `backend/src/htdt` for module-level and
`self.` dicts/sets/lists without eviction, manual audit of every
`@lru_cache`/`cache` and every `open(`/`sqlite3.connect(` call site, then
loop-and-count probes under `QT_QPA_PLATFORM=offscreen`, Python 3.12,
Windows. Branch `devin/rev17-mem`.

## Suspicions → verdicts

| # | Suspicion | Traced | Verdict |
|---|---|---|---|
| 1 | `@lru_cache` / `functools.cache` without maxsize | Every decorator in `htdt` carries an explicit `maxsize` (largest 1024 in `cad_directivity`); none bare | Clean |
| 2 | `cad_search._enumeration_cache` unbounded | `OrderedDict` LRU with `popitem(last=False)` at a fixed cap | Clean |
| 3 | Sample-map / ordered-sample caches grow per curve | Capped-then-clear policy (cap then `clear()`); steady-state bounded | Clean |
| 4 | `CommandHistory` / `NavigationHistory` grow forever | `_limit` eviction on undo stack; `CAPACITY = 64` on navigation | Clean |
| 5 | Diagnostics log grows forever | `RotatingFileHandler(MAX_LOG_BYTES, LOG_BACKUP_COUNT)` | Clean |
| 6 | Capture-receiver / ingress buffers unbounded | Byte budgets enforced (`read_file_bounded`, receiver byte caps, `IngressTooLargeError`) | Clean |
| 7 | `FrozenBundle` reads whole payloads | Documented design — `MAX_ENTRIES = 10_000`, `MAX_FILE_BYTES = 512MB` hard caps before materialization | Clean |
| 8 | `native_worker._tasks` / `_LINGERING_THREADS` | Popped on task finish; lingering list is bounded shutdown detach | Clean |
| 9 | `file_dialog_memory._dirs` unbounded | Keyed by a finite set of dialog keys — cannot grow per entity | Clean |
| 10 | DB event/audit tables grow forever | `scene_events`, `schema_migrations`, audit rows are append-only **by design** — a project DB is an audit trail, not a cache. No compaction requirement in the product contract | Deferred (documented design; revisit if a size budget lands) |
| 11 | GL/pixmap buffers per repaint/entity | Pixmaps are small, transient per-frame surfaces; the VTK scene is rebuilt, not accumulated (round-14 viewport audit) | Clean |
| 12 | Fixture transports' `requests`/`transmitted` lists | Test doubles only — never shipped paths | Clean |
| 13 | **`ActivityCenter._records` never evicts** | **CONFIRMED** — every submitted operation kept a `_OperationRecord` (snapshot + `cancel_callback` + `domain_payload`) for the center's lifetime; the center is composition-scoped ≈ session lifetime. `_history` was capped at 200 but each record pinned its domain payload | **Fixed** |
| 14 | **`_spawned_compositions` accumulates dead compositions** | **CONFIRMED** — each respawn switch `append`ed; a long session of project switches kept every closed shell's composition (repository handles, mounts, subscribers) reachable from the live app | **Fixed** |
| 15 | **Activity page listener re-subscribed per remount** | **CONFIRMED** — `activity_center.subscribe(_queue_refresh)` ran at each `_make_activity`; `dispose_mounts` dropped the widget but the center retained the closure → one dead listener per in-place switch | **Fixed** |
| 16 | **`PredictionExecutionController` per-task dicts unbounded** | **CONFIRMED** — `_progress`, `_submitted`, `_cancelling` keyed by task id, populated on submit/progress/cancel, only `_cancelling` was cleared per schedule (and only for tasks in it) | **Fixed** |
| 17 | **Job-guard `_cancelled` sets unbounded** | **CONFIRMED** — `MeasurementJobGuard` / `PredictionJobGuard` `cancel()` added a uuid per job; the set never shrank. A session that cancels N jobs retained N uuid strings forever | **Fixed** |
| 18 | **`cad_schema` signature memos unbounded by path** | **CONFIRMED** — `_ENSURED_SCHEMA_SIGNATURES` / `_COMPATIBLE_SCHEMA_SIGNATURES` keyed by `str(path)`; every distinct project DB ever opened in a session left a permanent entry | **Fixed** |
| 19 | **Storage health probe leaks sqlite handle** | **CONFIRMED** — `with sqlite3.connect(...) as conn:` commits/rolls back but never closes; the probe left an open descriptor on `cad-scenes.sqlite3` on the success path and on the `sqlite3.Error` path | **Fixed** |
| 20 | **Profile import `open().read()` unclosed** | **CONFIRMED** — `standards_profile_editor` read the selected JSON via a bare `open()`, leaking the handle until GC (C-implementation dependent) | **Fixed** |

## Finding details

### ActivityCenter `_records` eviction (activity_center.py)

`_records` now has a budget (`record_limit`, default = `history_limit`):
`_archive` evicts the oldest *terminal* records once the registry exceeds it.
Active records are never evicted — an operation in flight keeps its record no
matter how much history piles up behind it. Semantics preserved:

- `get()`/`retry()`/`submit` duplicate & `retry_of` checks fall back to a
  history-row scan (`_history_snapshot`), so a just-evicted operation is
  still addressable while it sits in the bounded rendered history.
- `note_authorities_changed` got a parallel in-place pass over `_history`
  for evicted rows — the same COMPLETED→COMPLETED_FOR_HISTORICAL_INPUT /
  `current_for_input=False` reclassification, then `_emit_snapshot` so the
  activity page refreshes.
- `unsubscribe(listener)` was added (idempotent), mirroring
  `ApplicationPreferenceStore.unsubscribe` — it did not exist, which is why
  the mount fix needed it.

What eviction *cannot* preserve (documented): `retry()` inherits
`domain_payload`/`cancel_callback` only while the record survives — a retry
of a long-dead op needs the caller's `retry_factory` (already the explicit
contract).

### Spawned-composition chain flattening (workflow_application.py)

Each composition now holds `_switch_parent: weakref.ref(...)` set when it is
spawned. `_open_document`'s respawn branch replaces (not appends) each
ancestor's `_spawned_compositions` with the newest leaf via a weak walk —
every dead hop points straight at the live leaf, loses its own strong
references, and is GC-able. `live_composition()` still resolves via `[-1]`,
O(1).

### Per-task / per-job / per-path registries (LRU caps)

- `PredictionExecutionController`: `_progress`/`_submitted`/`_cancelling`
  became `OrderedDict` MRU caches capped at `EXECUTION_PROGRESS_CACHE_LIMIT
  = 512` — terminal tasks older than the 512 most recent lose their live
  view, which degrades to the attempt record already persisted in the
  runtime repository.
- `MeasurementJobGuard`/`PredictionJobGuard`: `_cancelled` became an
  `OrderedDict` cap at `CANCELLED_JOB_LIMIT = 4096` — a completion still
  pending after 4096 subsequent cancels is past any live window.
- `cad_schema`: both signature memos became `OrderedDict` LRU at
  `_SCHEMA_SIGNATURE_CACHE_LIMIT = 512` paths; entries are already
  invalidated by file signature, so eviction only re-runs a cheap ro check.

### Handle hygiene

- `support_diagnostics._check_database`: `closing(sqlite3.connect(...))` —
  the health probe can no longer pin an open descriptor on the project
  database on either the success or the error path.
- `standards_profile_editor._import_profile`: `with open(...)` replaces a
  bare `open().read()` — no reliance on refcount GC.

## Regression probes (backend/tests/test_round17_boundedness.py)

13 tests: record-budget eviction + history fallbacks (get/retry/duplicate/
reclassify), payload & callback release via weakref, active-record pinning,
listener unsubscribe idempotence, composition-chain flatten + GC of the dead
intermediate, activity-mount listener release on dispose & remount, both
job-guard caps, the execution bookkeeping caps, both schema-memo caps, and
the health-probe connection close.

## Deferred

- Append-only DB tables (`scene_events`, `schema_migrations`, audit rows):
  growth is the audit-trail contract, not a leak — bounded by project scope,
  not time. If a project-size budget is ever adopted, pruning belongs in the
  migration authority, not in per-call reads.
