# Round 16 — multi-project / session-lifecycle state leakage

Scope: does state belonging to project A survive into project B when the app
opens/closes/switches projects? The app has two switch mechanisms — the
close+respawn path (`_switch_to_project`: `shell.close()` → `_open_document` →
new `WorkflowApplicationComposition` + new `WorkflowShellWindow`) used by the
menu / new / open / duplicate / import actions, and the in-place guarded swap
(`_switch_project`) used by typed navigation, launch intents, and the project
wizard. Every cache, singleton, subscription, timer, thread, and pump that
crosses either boundary was traced at both ends. Method: code trace at both
ends + offscreen reproduction probes (open A → switch to B → assert absence)
under `QT_QPA_PLATFORM=offscreen`, Python 3.12, Windows. Branch
`devin/rev16-state`.

## Suspicions → verdicts

| # | Suspicion | Traced | Verdict |
|---|---|---|---|
| 1 | Module-level caches / singletons hold A's document or DB connection after close | `cad_schema` signature caches keyed by `str(path)` **plus** a `(dev, ino, mtime_ns, ctime_ns, size)` signature — delete+recreate produces a new signature, so a different project at the same path is never served A's schema. `cad_search._enumeration_cache` is keyed by `search_spec_sha256` (content hash, bounded LRU). `cad_directivity` caches keyed by `id()` while the entry itself pins the dataset (no id-reuse hazard). `SceneRepository` is a path-bound handle — every call opens its own `sqlite3` connection; the post-restore `_release_data_handles`/`_reopen_data_handles` rebind is equivalent, not stale | Clean |
| 2 | Path-keyed caches reused across delete+recreate at same path | Same sweep as (1) — every path-keyed cache carries a live signature or content hash | Clean |
| 3 | Launch-intent pump / background dispatch bound to a closed composition | `native_cad._route_launch_intent` resolves `window.workflow_application` then `live_composition()` and re-binds `window = application.shell` before `raise_()` — the round-13 ISOL fix confirmed live. Verified: reading `workflow_application` off a *deleted* wrapper still returns the python attribute (the pump's anchor), so routing survives shell destruction | Clean (and held by this round's fix) |
| 4 | UI panels retain A's selection / undo / activity feed / comparison picks into B | Close+respawn gets a fresh shell; in-place `_switch_project` runs `dispose_data_workspaces` → `reset_selected_contexts` → reseed — ordering verified. Undo stacks are mount-scoped `WorkingDocument`s disposed with the mount; `ActivityCenter` is per-composition | Clean |
| 5 | Timers/watchers/threads of A still writing after close | Backup runner shut down by close hook and restarted on the new comp; mount timers are widget-parented; `native_worker._LINGERING_THREADS` is deliberate module-owned detach with bounded `shutdown` | Clean |
| 6 | Recent-files / file-dialog memory / settings resolved against wrong project | `file_dialog_memory` is app-scoped by design (`__last__` global key); window-state writes are per-project (`window-state/<ref>.json`) — a scoped write never mirrors to the global file | Clean |
| 7 | Transfer/capture lanes during a switch hitting A or B | `launch_router` stages to `CAPTURE_INBOX_UNASSIGNED_SCOPE` — captures never guess a project; `cad_screen_transfer` keys transfers by explicit `document_id` and `select_transfer` refuses mismatched documents | Clean |
| 8 | **Shared `ApplicationPreferenceStore` subscriber list across respawn** | **LEAK CONFIRMED** (see below) | **Fixed** |
| 9 | **`_spawned_compositions` chain retains every dead shell** | **LEAK CONFIRMED** (see below) | **Fixed** |

## Finding 1 — zombie preference observers on the app-scoped store

`ApplicationPreferenceStore.subscribe()` had no `unsubscribe`. The store is
shared across every spawned composition, but each composition registered
per-composition listeners that never detached on close:

- `composition._on_preference_change` — walks the dead shell's router mounts
  and rewrites dead `RewApiClient` endpoints on every live REW-endpoint commit.
- `PreferencesWidget._on_external_change` — re-syncs the dead settings form's
  editors on every live commit.
- `bind_inspector_display_length_policy` / `bind_measure_display_length_policy`
  `on_change` closures — weakref-guarded so they degrade to no-ops, but each
  mounted room added two permanent entries.

Offscreen probe (before fix): `len(store._listeners)` went **2 → 4 → 6** across
two menu-style switches — unbounded accumulation of dead observers. Beyond
wasted fan-out, a dead listener that raises lands in `PreferenceNotificationError`
on an unrelated write; the moment dead widgets were deleted (Finding 2 fix),
the dead panel's `_sync_editor` on destroyed editors would have become exactly
that raising listener.

**Fix**: `ApplicationPreferenceStore.unsubscribe(listener)`; the composition
registers a close hook `_release_preference_watch` (runs only on accepted
close, before teardown) that unsubscribes its own listener and calls
`preferences_panel.release()`; `PreferencesWidget.release()` is also wired to
its `destroyed` signal as a backstop for host-driven lifetimes; the two
`bind_*_policy` closures self-prune when their widget weakref dies. Probe after
fix: listener count stays **2 → 2 → 2**.

## Finding 2 — dead shells retained forever

`_spawned_compositions` exists so `live_composition()` can walk to the visible
window, but each menu-driven switch also left the entire old
`WorkflowShellWindow` — dialogs, panels, command palette, menus, actions,
controllers — alive and hidden for the process lifetime
(`topLevelWidgets` probe showed 3 `WorkflowShellWindow`s + 3
`DataManagementDialog`s after two switches).

**Fix**: `self.shell.deleteLater()` at the end of the respawn branch in
`_open_document`. Verified safe: launch-intent routing reads the python-level
`workflow_application` attribute off the dead wrapper (readable after C++
deletion), mounts are already disposed by the close path, and busy-work is
guarded by close guards before the shell ever closes. Probe after fix: one
shell total, dead wrapper raises `RuntimeError` on Qt calls while
`workflow_application` still rebinds to the live composition.

## Verification

- New `backend/tests/test_round16_state_leakage.py` — 6 tests: listeners
  detached on switch, listener count stable over 3 switches, dead shell
  C++-deleted while `workflow_application` still reads, a live commit never
  reaches a dead panel's `_sync_editor`, the live composition still receives
  REW-endpoint notifications, and a vetoed/aborted switch leaves the
  composition subscribed.
- On pre-change code the same probe shows listeners 2 → 4 → 6 and triple
  retained shells; post-change 2 → 2 → 2 and a single shell.
- Neighboring suites (`test_project_switch_lifecycle`, `test_round13_isolation`,
  `test_application_preferences`, `test_workflow_application`,
  `test_workflow_shell`, `test_window_state`, `test_activity_center`,
  `test_automatic_backup_runner`) all pass.
