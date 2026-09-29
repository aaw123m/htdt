# Round 14 — Measured memory & startup/idle resource footprint

Scope: real resource behavior, not asymptotic analysis. Boot was timed end
to end with `scripts/round14_resource_probe.py` (real `native_cad.main`
launch, offscreen Qt, milestones + psutil RSS + idle CPU sampling; modal
notices auto-accepted offscreen), imports dissected with
`-X importtime`/cProfile, and a scripted work session (workspace cycles,
dialog open/close, palette, project switches) measured RSS deltas per
operation with `tracemalloc` diffs. Windows 2022, Python 3.12.10,
`QT_QPA_PLATFORM=offscreen`.

## Headline measurements

| Metric | Before | After | Delta |
|---|---|---|---|
| Boot to interactive — steady-state (warm, existing data dir) | 4.74 s | 3.42 s | **−1.32 s (−28%)** |
| RSS at `QApplication.exec` entry — steady-state | 335.5 MB | 174.6 MB | **−160.9 MB (−48%)** |
| Boot to interactive — first boot on a fresh data dir | 5.9–6.2 s | 5.5–6.0 s | ~−0.4 s |
| RSS at exec — fresh data dir | 336–341 MB | 174–180 MB | **~−160 MB** |
| Idle CPU, app sitting after boot | 0.00% (measured) | 0.00–0.28% | ≈flat |
| RSS — project switch (×6) | +52.8 MB (~8.8 MB/switch) | ~0 MB (−2 MB over 4) | **leak removed** |
| Top-level widgets after 4 project switches | 197 → 297 (+25/switch steady-state) | flat 12 after heavy-mount dispose (was growing +26/cycle) | **leak removed** |
| RSS — workspace cycle ×10 (all 4 destinations) | — | ~0.9 MB/cycle residual | noted, small |
| Dialogs (settings ×15, palette ×15) | — | 0.0 MB / +0.1 MB | clean |

## Where the boot time went (profiled)

| Phase | Before | After |
|---|---|---|
| `import workflow_application` | 2.7–3.0 s | ~1.05 s |
| `native_cad.__getattr__` lazy-export resolutions during boot | 15 calls incl. `optimization_workspace` chain | only the names `_run_gui` actually uses |
| `optimization_workspace` → `measurement_workspace` → `measurement_editor` chain (legacy `--legacy-ui` window + its `pyvistaqt`/`pyvista.plotting`/`matplotlib`/`pyqtgraph`/`vtk` dependencies) | loaded on **every** boot (~2 s+ of import work, ~160 MB RSS of DLLs) | never loaded unless `--legacy-ui` |
| `execute_native_upgrade` on an existing-current-schema dir | ~5 ms no-op | unchanged |
| `ensure_native_schema` on a *fresh* dir (DDL `executescript` + sqlite `execute`) | ~1–1.5 s one-time first-boot cost | unchanged (schema must be created once) |
| Composition + shell build (incl. overview mount) | ~1.8 s | ~1.6 s |

## Findings and fixes

### Fixed

1. **Legacy optimization UI imported at every startup.** `_run_gui`
   eagerly bound `OptimizationWorkspaceWindow = _self.OptimizationWorkspaceWindow`
   in its prelude, so the lazy export resolved `optimization_workspace`
   (and transitively `measurement_workspace`, `measurement_editor`,
   `cad_adaptive_repository`, `optimization_*_controller`,
   `pyvistaqt`/`pyvista.plotting`/`matplotlib`/`pyqtgraph`/`vtk`) even
   when launching the workflow shell. Moved the resolution into the
   `--legacy-ui` branch that uses it (`native_cad.py`). ~160 MB RSS and
   ~1.3 s steady-state boot saved.
2. **Workspace-mount disposal leak.** `WorkspaceRouter.dispose_mounts`
   queued `deleteLater()` on each mount, but the mounts keep receiving
   posted events while their own teardown unwinds (close handlers, child
   menus, plotters); Qt reposts the pending DeferredDelete behind them,
   so across successive project switches the old mounts — and every
   unparented popup child (QMenu/QFrame/ViewBoxMenu, ~45 top-level
   widgets) — survived forever as hidden windows. Measured **+47
   top-level widgets and ~8.8 MB per project switch**. Now flushes
   `QEvent.DeferredDelete` at the end of `dispose_mounts`, so mounts are
   really destroyed before the next project mounts; per-switch RSS delta
   ~0 (`workflow_shell.py`).
   Runtime re-verification then showed a residual: the mounts die, but
   their **unparented popup children** (plot context menus, `ViewBoxMenu`,
   `QFrame` popups — not `QObject` children of the mount tree, so no
   `deleteLater` ever reaches them) still accumulated +26 hidden
   `Qt.Popup` top-levels per heavy-mount dispose, linear/unbounded
   (58→116 over 3 iterations). `dispose_mounts` now also queues every
   unparented `Qt.Popup` top-level for deletion before the flush —
   application-level top-levels (dialogs, the parented command palette)
   are unaffected. Post-dispose top-levels flat at 12 every cycle.
3. **`--legacy-ui` rollback boot dead since Round 10** (pre-existing
   regression found while re-verifying the new lazy path).
   `OptimizationWorkspaceWindow.__init__` crashed in
   `optimization_search_controller` with `AttributeError:
   search_reauthor_button` — the reauthor/reason/filter controls were
   added only on the new-path `optimization_workflow_controller` build
   (`d90c770e`), so the legacy window's `None`-default init block never
   declared them. Added the three missing `None` defaults
   (`optimization_workspace.py`); `--legacy-ui` boots to a constructed
   window again.
4. **Eager workspace module imports in `workflow_application`.**
   `room_viewport`, `room_workspace`, `application_pages`,
   `overview_workspace`, `measurement_*`, `optimization_*`, room panel
   modules were top-level imports although every use site lives inside
   the `_make_*` factory methods. Replaced with a `_LAZY_IMPORTS` map +
   module `__getattr__`/`__dir__` (same PEP 562 idiom as
   `native_cad.py`, `_self.X` resolution inside factories so
   `monkeypatch.setattr` keeps working). `import workflow_application`
   2.7→1.05 s. Heavy modules now load only when their workspace is first
   mounted.
5. **800 ms launch-intent poll for the app's whole lifetime.** The
   intent pump stat()-polled `intents/incoming/` every 800 ms forever.
   Replaced with a `QFileSystemWatcher` on the incoming dir (created via
   new `launch_intents.ensure_intent_incoming_dir`) plus a 5 s fallback
   tick that re-arms the watch if the queue dir is replaced — 6× fewer
   lifetime wakeups with the same delivery semantics (`native_cad.py`,
   `launch_intents.py`).

### Verified clean (no fix needed)

- **Caches**: `cad_search._enumeration_cache` is LRU-bounded
  (`_ENUMERATION_CACHE_MAX`); all `lru_cache` uses carry `maxsize`
  (build_info 1, cad_snap 2048, comparison 64/128); calibration residual
  cache and snap projection cache are per-instance/per-run scoped.
- **Workers**: `NativeWorkerPool` drops `_tasks` entries on `finished`;
  `_LINGERING_THREADS` only holds shutdown-timeout detachments and is
  popped when the thread actually ends; worker results travel by signal
  and aren't retained. `data_management` mirrors the same pattern.
- **Dialogs**: settings and palette open/close cycles are flat (0.0 /
  +0.1 MB over 15 iterations).
- **Idle CPU**: 0.00–0.28% of one core over ~6 s after boot; the only
  periodic sources are the (now rare) intent fallback tick and the
  backup scheduler's due-check.

### Deferred / noted (not fixed this round)

- `WorkflowApplicationComposition.__init__` still builds
  `DataManagementDialog`, `PreferencesWidget`, `RetentionPolicyWidget`
  and the palette/help registries eagerly (~part of the remaining ~1.6 s
  composition block). Lazifying the settings dialog requires a proxy
  slot + a late-bound `data_management_component` for the shutdown path
  — a lifecycle change larger than this round's diff budget.
- ~40 `cad_*` repository modules remain top-level imports in
  `workflow_application` (~0.9 s of the residual import cost). They are
  only referenced inside method bodies, so deferral would need dozens of
  `_self.` sites — deferred as a follow-up if boot time keeps mattering.
- ~0.9 MB residual RSS per full workspace cycle (activation-time churn,
  not a monotonic leak — mounts are cached).
- First-boot `ensure_native_schema` (~1–1.5 s DDL on a fresh data dir)
  is inherent one-time work; steady-state boots skip it entirely.

## Repro

```bash
# boot to interactive + idle CPU/RSS sampling (fresh dir)
python scripts/round14_resource_probe.py boot --data-dir <dir> --idle-seconds 6
# same dir again = steady-state boot
# scripted work session with per-op RSS deltas + tracemalloc diffs
python scripts/round14_resource_probe.py session --data-dir <dir>
```

## Files changed

- `backend/src/htdt/native_cad.py` — resolve `OptimizationWorkspaceWindow`
  only in the `--legacy-ui` branch; QFileSystemWatcher intent delivery +
  5 s fallback poll.
- `backend/src/htdt/launch_intents.py` — public
  `ensure_intent_incoming_dir` for watch targets.
- `backend/src/htdt/workflow_application.py` — `_LAZY_IMPORTS` lazy
  facade for workspace/page modules.
- `backend/src/htdt/optimization_workspace.py` — declare the three
  newer search controls as `None` in the legacy init block (unbreaks
  `--legacy-ui` boot).
- `backend/src/htdt/workflow_shell.py` — `dispose_mounts` flushes
  deferred deletes so disposed mounts actually die, and reaps orphaned
  unparented `Qt.Popup` top-levels (plot/context menus) so they cannot
  accumulate across project switches.
- `scripts/round14_resource_probe.py` — the measurement harness (boot +
  session modes, cProfile option).
