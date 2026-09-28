# Round 13 — cross-project / cross-document state isolation

Scope: when a user switches projects, opens a second document, or closes
and reopens, does state from context A leak into context B? Enumerated
every process-global / module-global mutable state holder, then traced
both project-switch paths, the second-instance forward path, worker
completions, dialog scope, and shared on-disk resources. Offscreen Qt
tests (`QT_QPA_PLATFORM=offscreen`, Python 3.12.10 + pytest) reproduce
each leak before the fix. Branch `devin/rev13-isol`.

## State inventory — scope vs. requirement

| State | Scope required | Scope actual | Verdict |
|-------|----------------|--------------|---------|
| `SceneRepository` rows (scene, measurements, variants, calibrations, checks, plans, notes…) | per `document_id` | per `document_id` key on every repository method | VERIFIED |
| Project registry `htdt_project_documents` | data-root (maps project↔document) | data-root, `project_id` is cross-device identity | VERIFIED |
| `WorkspaceRouter._mounts` + per-mount controllers, `WorkingDocument`/`CommandHistory` (undo), `is_dirty`, preview tokens, `_job_pool`s | per mounted workspace (per open document) | created by factories that read `self.document_id` lazily; destroyed by `resolve_dispose_all`+`dispose_data_workspaces` on every switch | VERIFIED — undo can never cross documents |
| Job/prediction tokens (`MeasurementJobToken`, `PredictionJobToken`) | per document + scene revision | frozen dataclass pinned to `document_id` + `scene_revision_id` + content hashes; `can_apply` rejects stale/cancelled | VERIFIED |
| Worker pools (`NativeWorkerPool` per mount/panel, module-global `_LINGERING_THREADS`) | per mount; late completions must not reach a dead owner | `pool.shutdown()` on `closeEvent` disconnects `on_completed`; still-running threads detach to module scope with no owner reference | VERIFIED — a project-A worker finishing after switch writes only A's store (its captured document_id), never B's UI |
| `switch` busy gates (`before_deactivate`/`dirty_state` on measurement, room, optimization workspaces + joint/robustness/compare panels) | block switch while work in flight | wired into `resolve_dispose_all('project_switch')` | VERIFIED |
| `WorkflowShellWindow._selected_context` (live per-workspace context picks) | per project | **process-global to the shell — persisted per-project only at composition build/close; the in-place switch never saved or re-seeded it** | **BROKEN → FIXED (F1)** |
| Persisted `window-state/<project_ref>.json` | per project | per-project file since round9; legacy `window-state.json` only fallback+unbound target, never mirrored | VERIFIED |
| `NavigationHistory` | app session, project-scoped entries | typed entries carry `project_id`; `drop_unscoped_project_entries` on switch prevents legacy replay; Back through a project boundary replays the same guarded `_switch_project` | VERIFIED |
| `ActivityCenter` + `ACTIVITY_HISTORY_FILENAME` | app/data-root | app-scoped by design; persisted history is cross-project on purpose | VERIFIED |
| `ApplicationPreferenceStore`, `DataManagementDialog`, `CaptureRetentionService`, `file_dialog_memory` store | app/data-root | all bound to `data_dir`, not a document — dialogs left open across a switch keep data-root objects, which is honest | VERIFIED |
| `settings_dialog` (non-modal) across close+respawn | app | parented to the closed shell but bound to data-root services — stays open, still valid; no document references | VERIFIED (cosmetic) |
| Command palette providers (`entities`, `measurement_items`, `revision_items`, `variant_items`, `inbox_items`) | per current document | all closures re-read `self.document_id` per query | VERIFIED |
| `cad_search._enumeration_cache` (module-global, max 4, locked) | semantically keyed | keyed by `search_spec_sha256` — the spec sha pins scene revision, constraints, bindings; sharing across projects is content-addressed reuse | VERIFIED |
| `cad_schema._ENSURED_SCHEMA_SIGNATURES` / `_COMPATIBLE_SCHEMA_SIGNATURES` | per DB file | keyed by db path + (dev,ino,mtime_ns,ctime_ns,size); every write bumps the signature | VERIFIED |
| `managed_assets` / `measurement-assets` / auralization artifact dirs | shared-by-content OK | SHA-256 content-addressed under data root; `ensure_installed` dedups, `read_verified` re-hashes | VERIFIED — sharing is correct by design |
| Temp dirs (`mkdtemp`, `mkstemp`, handoff export temp) | per operation | inside data root or export target, per-call | VERIFIED |
| `runtime_instance` lock + `runtime.json` | per data root | `.instance.lock` byte-range lock + advisory owner metadata | VERIFIED |
| `file_dialog_memory` module-global `_active_store`/`_fallback_store` | app | bound once per composition to data-dir JSON (ephemeral in Safe Mode) | VERIFIED |
| `native_diagnostics._uncaught_sinks` | per composition | push/release via close hook; routes to `window.workflow_application` | VERIFIED |
| Launch-intent queue (`intents/incoming`) | per data root | atomic per-intent files, stale-age expiry, drained by the owning instance only | VERIFIED |
| `_drain_queued_launch_intents` pump target window | the window the user sees | **bound forever to the FIRST shell; after a close+respawn switch the pump's `application` is the hidden, closed composition** | **BROKEN → FIXED (F2)** |
| `AutomaticBackupRunner` per composition | app | close hook shuts it down; **respawned compositions never restarted it** | **BROKEN → FIXED (F3)** |

## Finding 1 — in-place switch dropped/leaked window state (MEDIUM)

Two switch paths existed with different state contracts:

- `_switch_to_project` (menu/library/dialogs): closes the shell → close
  hooks persist the outgoing project's `window-state/<ref>.json` → a new
  composition replays the target's persisted layout. Correct.
- `_switch_project` (typed navigation, deep links, forwarded
  `.htdtproject` opens): disposed mounts and rebound `document_id`
  **in place** — never persisted the outgoing project's live layout and
  never replayed the target's. The shell's `_selected_context` map kept
  the outgoing project's context selections, and the target always
  landed on Overview.

Concrete leak: in project A pick the room `history` context →
`_switch_project(B)` → B's room workspace opens on `history` (A's pick);
B's own saved contexts are ignored; A's mid-session changes are never
written under A's ref (the close hook writes later under whichever
project is current at exit). Switch back to A → you see the context you
picked in B, not A's.

Fix: once the guarded switch is committed (resolution passed, target
opened) `_switch_project` calls `_save_window_state()` under the
outgoing ref, and after `navigate(OVERVIEW)` it resets the live context
map (`WorkflowShellWindow.reset_selected_contexts()` — registration
defaults) and replays the target's record via `_restore_window_state()`.
Safe Mode still skips both directions.

## Finding 2 — launch intents rebound a dead composition (HIGH)

`_run_gui` builds the intent pump once with `window` = the first shell.
`_switch_to_project` then closes that shell and spawns a new composition
(kept alive in `self._spawned_compositions`). A forwarded `.htdtproject`
open afterwards took `getattr(window, 'workflow_application')` = the
**closed** composition and ran `application._switch_project(document_id)`
on it — rebinding a hidden window to project C while the visible window
stayed on B. The intent reported `routed_and_opened` success, project C
was marked `last_opened` in the registry (stealing the next launch's
startup project), and the user saw nothing happen.

Fix: `WorkflowApplicationComposition.live_composition()` walks the
spawn chain to the deepest (visible) composition, and
`_route_launch_intent` resolves it before raising/activating and before
dispatch — project opens, inbox deep links, and restore previews now act
on the window the user actually sees.

## Finding 3 — respawned composition lost the backup tick (LOW)

`start_automatic_backup` is invoked once in `_run_gui` on the first
composition. `_switch_to_project`'s close hooks shut that runner down,
and `_open_document` never started one on the spawned composition — the
due-backup check silently stopped for the rest of the process. The fix
restarts it on the new composition when the outgoing one had it running
(`_automatic_backup_runner` stays non-None after `shutdown()`, which is
the "was started" marker).

## Second instance — verified honest

`SingleInstanceGuard` holds an exclusive byte-range lock on
`<data_dir>/.instance.lock` (`msvcrt.locking`/`flock`). A second GUI
instance fails `acquire()`, forwards each open intent plus an activation
intent to the running instance via the atomic drop queue, logs
contention, and exits 0; if forwarding fails it shows an honest error
and exits 2. A second *maintenance* invocation (`--backup`, `--restore`…)
refuses on stderr with exit 2 — never silently runs against a locked
data root. The first instance drains the queue every 800 ms and defers
the whole drain while any modal dialog is active (round12). Intents now
route to the live composition (F2).

## Deferred / notes

- `cad_search._enumeration_cache` skips the `scene_repository.get` existence
  check on cache hit — a stale sha could return hits for a scene revision
  that was since deleted. Bounded (4 entries), spec-sha keyed, marginal.
- `file_dialog_memory` fallback store lives in `gettempdir()` —
  app-scoped convenience state shared across data roots only when the
  data dir is unwritable; acceptable.
- `NavigationHistory` keeps A's project-scoped entries across a switch
  by design — Back through a project boundary replays the guarded
  switch, which now also round-trips window state.
- `settings_dialog` left open across a close+respawn switch stays bound
  to the old composition's data-root services — operations remain valid;
  its hidden parent is cosmetic only.

## Tests

`backend/tests/test_round13_isolation.py` (6 tests) — all FAIL on the
pre-fix code, all pass after:

| Test | Asserts |
|------|---------|
| `test_in_place_switch_persists_outgoing_window_state` | leaving A writes `window-state/<A>.json` with A's workspace+contexts |
| `test_in_place_switch_replays_target_contexts_not_outgoing` | B sees its own saved context, not A's live pick |
| `test_in_place_switch_restores_target_workspace` | B lands on its saved workspace, not forced Overview |
| `test_in_place_switch_round_trip_keeps_a_intact` | A→B→A replays A's persisted context |
| `test_launch_intent_routes_to_live_composition` | forwarded open switches the visible composition, not the closed one |
| `test_respawned_composition_restarts_backup_runner` | respawned composition has a live backup runner |

Regression: `test_window_state`, `test_project_switch_lifecycle`,
`test_workflow_application`, `test_workflow_shell`, `test_launch_router`,
`test_launch_intents`, `test_workflow_integration`, `test_ui_workflows`,
`test_native_launch`, `test_runtime_instance`,
`test_review_round9_prefs`, `test_review_round7_workflow` — all green;
full suite `pytest backend/tests -q -n 4` green.
