# Round 3 — Qt/UI Layer Correctness Review

Scope: PySide6 surface of `backend/src/htdt` (PySide6 6.11.2, pyvista
0.49.0, pyvistaqt 0.13.1, vtk 9.7.0 as pinned in `backend/pyproject.toml`).
Carries the round-2 security finding on `room_viewport._render_underlays`
verified and fixed first, then a sweep over the hunt axes: lambda-capture
connects, deleteLater/parenting leaks, QTimer leaks, GUI-thread blocking,
modal safety, deleted-C++-object access, model/view index misuse, and
closeEvent cleanup. Round-1/round-2 findings are not re-reported;
`data_management.py`'s deferred worker-termination sketch from round 2
stands unchanged.

## Fixed this round

| ID | Severity | Area | Finding | Resolution |
|----|----------|------|---------|------------|
| R3-F1 | High | `room_viewport.py` `_render_underlays` | `quad.active_t_coords` was deprecated in pyvista 0.43 and removed in 0.46 — on the pinned 0.49.0 the assignment raises `PyVistaAttributeError` (verified empirically), so any raster underlay render after import crashed with an uncaught exception in the workspace refresh path. | Switched to the supported `quad.active_texture_coordinates`, verified on 0.49.0 to register a real VTK `TCoords` array that `add_mesh(texture=...)` consumes. |
| R3-F2 | Low | `cad_composition.py:44`, `measurement_workspace.py:26,60` | Three `QTimer.singleShot(0, callable)` sites had no context receiver: a lambda capturing `self` (`statusBar().showMessage` after a rejected room replace) and two bound methods (`_sync_content_extent`, `_fit_initial_size_to_screen`). If the widget is destroyed between posting and event dispatch, the shot fires on a deleted C++ object → `RuntimeError`. Verified empirically that the 3-arg overload `QTimer.singleShot(msec, receiver, callable)` drops the call once the receiver's DeferredDelete is processed. | Added `self` as the context receiver at all three sites. |
| R3-F3 | Low | `workflow_settings.py` `DataManagementDialog.closeEvent` | `before_deactivate()` returns `(allowed, reason)` but the reason was discarded — clicking the window close control during a data operation (or in restart-required state) silently did nothing, leaving the user to guess why the dialog would not close. The host shell shows the same reason in `statusBar()`; a QDialog has none. | Surface the refusal reason via `QMessageBox.information(self, windowTitle, reason)` before `event.ignore()`. |

## Verified clean (checked this round, no findings)

- **Lambda `.connect()` sites (~47 across the surface).** Every lambda
  sender is a child widget/object owned by the connecting object or has
  the same lifetime (dialog buttons, per-row controls in rebuild loops
  whose widgets are `deleteLater`'d — which drops their connections).
  Long-lived senders (`app.focusChanged`, `QApplication` signals) use
  bound methods, which are receiver-tracked and auto-disconnect on
  receiver destruction — the round-2-hardened `native_editor.py:237-243`
  pattern. No stale-receiver hazard remains after R3-F2.
- **`deleteLater` / widget parenting.** Unparented `QWidget()`/`QLabel()`/
  `QFrame()` constructions are all reparented through a layout before
  escape (`application_pages.py:290`, `equipment_library.py:652`,
  `commissioning_wizard.py:395`, …). `setParent(None)` sites are either
  immediately re-added (`measurement_workspace.py:89` panel → scroll
  rewrap), or left for Python GC to finalize after overwrite
  (`room_workspace.py:4000/4010` panel swap), or followed by
  `deleteLater` (`workflow_shell.dispose_mounts`). `native_worker.py`
  deliberately does *not* `deleteLater` workers — the lingering-thread
  detach map and the PySide6/Windows teardown race are documented in the
  file and already covered by round 2.
- **`QTimer` leaks.** All periodic timers are `QTimer(self)`-parented and
  stopped in their closeEvent (`native_editor.py` stops four;
  `capture_watch`/`gizmo_rebuild`/`view_state_save`/`preview_inspect`).
  `native_cad.py:643-666`'s unparented `intent_pump` is deliberate
  app-lifetime state per its comment — safe.
- **GUI-thread blocking.** Long work runs on `NativeWorkerPool` with job
  guards (`OptimizationWorkflowController.dispose` cancels
  `_rew_tokens` then shuts down `_search_pool`/`_extended_pool`/
  `_rew_pool`; `room_prediction.dispose` cancels tokens +
  `pool.shutdown()`). `capture_receiver_controller._on_delivery` emits
  `delivery_staged` from the HTTP thread and Qt auto-queues it onto the
  GUI thread (`workflow_application.py:350`) — correct marshalling.
  `QFileDialog.getOpenFileName`/`QMessageBox.exec` calls are the expected
  modal-blocking pattern.
- **Modal safety.** All `exec()` dialogs are parented (`QMessageBox(self)`,
  `MaterialDialog` inside `room_acoustics_panel`, profile editors in
  `standards_workspace`). `CommandPalette` and `DataManagementDialog` are
  non-modal by design (`setModal(False)` + `show()`/`hide()`), and the
  latter now reports why a close was refused (R3-F3).
- **Deleted-C++ access.** `dispose()` paths wrap `removeEventFilter` in
  `try/RuntimeError` (`room_geometry_input.py:88-95`,
  `room_transform_input.py:79-84`). Viewport pick paths
  (`pick_actor_at`, `pick_world_position`) and overlay removal
  (`optimization_robustness_controller._remove_robustness_overlays`) are
  exception-guarded. `eventFilter` guards `watched is viewport.interactor`
  and `mode == "idle"` before touching VTK state.
- **Model/view index misuse.** No `QAbstractItemModel`/`QAbstractListModel`
  subclasses exist in the UI layer — only `QTreeWidget`/`QTableWidget`
  item models, repopulated under `blockSignals` discipline
  (`field_explorer_panel` `refresh_sessions`/`_refresh_coordinates`,
  `optimization`/`measurement` trees). `QStackedWidget` users
  (`right_stack` in `room_workspace`) keep `removeWidget` + reparent
  consistent.
- **`closeEvent` cleanup.** Verified every workspace close path:
  `measurement_editor.py:823`, `measurement_page_workspace.py:4083`,
  `optimization_workspace.py:1094`, `prediction_workspace.py:687`,
  `optimization_workflow_workspace` (disposes controller) — all set a
  `_disposed` flag, cancel job-guard tokens, `pool.shutdown()`, warn on
  lingering workers, then `super().closeEvent`. `native_editor.py:1245`
  stops all four timers, cancels preview work, removes the vtk observer,
  and closes the viewport. `room_workspace.py:6578` disposes both input
  controllers, closes the controller and viewport. `workflow_shell.py:933`
  runs `router.resolve_dispose_all("exit")` + `router.shutdown()`;
  `dispose_mounts` (`workflow_shell.py:331`) unwinds mounts with
  removeWidget → setParent(None) → deleteLater. `RoomViewport3D.closeEvent`
  closes the `QtInteractor` plotter.
- **Signal emission off-thread.** The only cross-thread emit is the
  capture receiver's `delivery_staged`; everything else emits on the GUI
  thread. `QThread` subclassing is confined to `NativeWorkerPool` and the
  data-management worker — both move work via `moveToThread`-free
  run-in-thread + finished-signal marshalling.

## Deferred / informational

- **Synchronous field compute on the GUI thread**
  (`field_explorer_panel.py:200,273,367,399`):
  `build_mode_field_explorer_session`, `explorer_slice`, and
  `explorer_probe` run modal-field math inline in click/change handlers.
  It is user-triggered, bounded by the requested stride, and matches the
  app's light-compute-on-GUI convention — but a fine stride on a large
  room can block the event loop for seconds. Sketch: wrap
  `_build_session` in `NativeWorkerPool` like the other heavy panels, or
  set a wait cursor + `QTimer.singleShot` progress guard while computing.
- **`cad_input.dispose()` leaves window-parented `QShortcut`s**
  (`cad_input.py:257-267`): only the binder is `deleteLater`'d; the
  shortcuts die with the window. Bounded — `unbind_cad_input_commands`
  clears execution and `CommandRegistry.execute` returns False on a stale
  fire — noted only so future shortcut-lifecycle changes keep the
  invariant.
- **`data_management.py:675-704` worker-thread teardown**: unchanged from
  round 2's deferred sketch (thread parented to the app-lifetime manager,
  `finished.connect(deleteLater)`, worker never deleted — documented
  teardown-race mitigation, not re-fixed here).

## Regression coverage

`backend/tests/test_room_viewport_semantics.py`:

- `test_render_underlays_registers_tcoords_and_actors` — builds a real
  `RoomViewport3D` (offscreen) with a recording plotter, renders a raster
  + vector `UnderlayRenderItem`, and asserts the quad carries a real VTK
  `TCoords` array (`GetPointData().GetTCoords()` + `active_texture_coordinates`),
  the texture kwarg reaches `add_mesh`, and both actors register the
  underlay id. Fails on the old `active_t_coords` code path.

The `QTimer.singleShot` context fix was verified empirically on the
shipped interpreter: after `deleteLater()` + `sendPostedEvents`
(`DeferredDelete`), a receiver-scoped singleShot does not fire, while the
context-less form does.
