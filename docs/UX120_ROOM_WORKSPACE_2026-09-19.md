# UX120 Room workspace implementation record — 2026-09-19

Issue: #118  
Scope: UX120 Room workspace only

## Implemented

The new Room workspace is a viewport-centric component composition rather than a rearrangement of the legacy QMainWindow/QDockWidget inheritance chain.

New modules:

- backend/src/htdt/room_workspace.py
  - RoomWorkspace QWidget composition
  - RoomWorkspaceController application boundary
  - contextual tool strip
  - object palette
  - selection Inspector
  - overlay/focus controls
  - recovery banner
  - UX110 WorkspaceMount factory
- backend/src/htdt/room_viewport.py
  - dark PyVista/VTK viewport
  - low-contrast floor grid
  - room wireframe
  - entity rendering
  - selection outline
  - optional labels
  - acoustic-reference / speaker-aim overlay
  - selection focus mode

No QDockWidget is used by the new workspace.

## Authority boundaries

The new workspace does not introduce a second scene/document authority.

| Concern | Authority reused |
| --- | --- |
| Scene content | SceneDocument / SceneEntity |
| Editing and undo/redo | TheaterWorkingDocument / existing command history |
| Formal persistence | SceneRepository.save() / SceneRevision |
| Recovery draft | SceneRepository.save_recovery() / recovery() |
| Selection/hidden/locked persistence | existing editor view-state repository |
| Theme tokens | ui_theme.DARK_THEME |
| Shell navigation contract | workflow_shell.WorkspaceMount |
| Room context IDs | existing UX110 workflow_navigation contract |

The viewport receives immutable SceneDocument snapshots and never owns domain truth.

## Shell integration point

The module exports:

    build_room_workspace_mount(repository, document_id)

It returns the existing WorkspaceMount contract with:

- on_activate -> refresh from the latest SceneRevision only when the local working document is clean
- before_deactivate -> fail closed while a transform preview, unsaved draft, or recovery decision is active
- on_context_changed -> switch geometry / objects / placement / acoustics contextual controls
- on_entity_requested -> select the requested entity for deep links
- on_close -> standard QWidget close lifecycle; recovery and view state are persisted by RoomWorkspace

The future shell composition change is intentionally small:

    WorkspaceId.ROOM: lambda: build_room_workspace_mount(repository, document_id)

native_cad.py and workflow_shell.py are intentionally unchanged in this PR, per UX120 task constraints.

## CAD input-controller boundary

This PR does not implement the Agent B CAD shortcut/navigation controller.

RoomViewport3D exposes its Qt/VTK interactor through the public interactor attribute and structurally implements Agent B PR #129's CadViewportInputPort (`begin_pan` / `pan_by` / `end_pan`, orbit equivalents, `zoom_by`, `open_context_menu`). Camera mutation stays renderer-local; the RMB menu request is emitted outward so command content remains owned by the Room workspace / central registry.

RoomWorkspace also emits toolRequested for geometry tools that require CAD input state:

- draw-room
- edit-room

Selection picking provided by the viewport is a local presentation convenience. The external input controller may replace/own selection input while continuing to call RoomWorkspace.select_entity().

No new shortcut policy is defined here.

## Context behavior

Geometry:
- viewport remains primary
- object palette hidden
- draw/edit geometry tools are delegated through toolRequested

Objects:
- object palette visible
- additions commit through TheaterWorkingDocument
- new selection is persisted through the existing view-state repository

Placement:
- object palette visible
- focus selected entity / fit scene controls
- selection Inspector remains contextual

Acoustics:
- acoustic overlay enabled by default for the context
- speaker aim vectors and acoustic-reference points use existing scene semantics

## Recovery and stale-state behavior

- edits create/update the existing repository recovery snapshot
- a pre-existing recovery snapshot blocks editing until recovered or discarded
- save uses SceneRepository.save(parent_revision_id=current source revision)
- workspace activation reloads a newer formal revision only when the local working document is clean and no recovery is pending
- active transform previews, unsaved drafts, and pending recovery decisions fail closed on workspace deactivation

This keeps the new component compatible with the stale-state fence introduced by UX110.

## Tests

backend/tests/test_room_workspace.py covers:

- repository/WorkingDocument/recovery authority reuse
- formal save only after explicit save
- persisted selection
- fail-closed preview deactivation
- absence of QDockWidget composition
- contextual palette/overlay behavior
- shell WorkspaceMount callbacks and deep-link selection

## Remaining work

1. Agent B integration after PR #129 lands: construct CadInputController with `viewport=RoomViewport3D.interactor` and `viewport_port=RoomViewport3D`, then bind Room-owned edit callbacks. Geometry `toolRequested` signals remain the handoff for draw/edit-room interaction state.
2. Shell integration: replace the temporary legacy Room factory in native_cad.py with build_room_workspace_mount after this PR is accepted. This PR does not change native_cad.py by requirement.
3. Command adapter integration: bind existing save/undo/redo/add-object commands to RoomWorkspace methods when the shell switches to the new mount. Command authority remains in the existing registry.
4. Geometry authoring UI: draw/edit-room interaction state remains Agent B territory; this PR only provides the viewport and component boundary.
5. UX150/UX160: Windows real-device DPI, lighting/readability and visual acceptance. RDC was not used for this implementation.

## Explicit non-changes

- native_cad.py: unchanged
- workflow_shell.py: unchanged
- Measurements workspace: unchanged
- Optimization workspace: unchanged
- Scene / WorkingDocument / repository semantics: unchanged
- CAD shortcut/input policy: unchanged


## Integration update — 2026-09-19

Integration branch `integration/ux120-140-workspaces` connected PR #129's `CadInputController` to this workspace, added WorkingDocument-backed polygon geometry input and M/R entity transform previews, and switched the workflow-shell Room factory to this component. The remaining Room gaps are N70 prediction execution/result presentation, advanced midpoint/numeric/wall/opening editing parity, and UX150/UX160 visual acceptance.


## N70 prediction follow-up — 2026-09-19

New Roomの `acoustics` contextへ既存N70矩形幾何予測を接続した。
`RoomPredictionController` はexisting request identity / PredictionJobGuard /
CadPredictionRepository / constraint snapshot hashを再利用し、独自prediction semanticsを作らない。

central `prediction.run`、background cancel、current/stale run表示、current runだけの
direct/first-reflection overlayまで接続した。

詳細: [UX120 Room prediction integration](UX120_ROOM_PREDICTION_2026-09-19.md)

Room側の残件は高度geometry editingとUX150/UX160 visual acceptance。


## Advanced geometry follow-up — 2026-09-19

Geometry contextのright surfaceを専用Inspectorへ拡張し、vertex/edge numeric edit、midpoint、
wall topology、wall move/merge/delete、opening add/edit/deleteを接続した。
`RoomWorkingDocument.replace_room_topology()` と既存 `cad_walls` をauthorityとして使用し、
opening/bindingをorphanする操作はfail closedする。

詳細: [UX120 Room advanced geometry](UX120_ROOM_ADVANCED_GEOMETRY_2026-09-19.md)
