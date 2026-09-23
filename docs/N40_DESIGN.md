# N40 theater objects — design

> Tracking: #55
> Base: `main` at `51d7125103eb391a2645f394988aa650839b0dac`
> Acceptance: A10 in `CAD_EDITOR_ACCEPTANCE.md`

## 1. Goal

N40 turns the accepted N20b/N30b CAD shell into a theater-layout editor. A user must be able to start from an empty project, draw the room, place a 3.0.2 layout plus seat/screen/furniture with the mouse, refine exact values numerically, save, and reopen the same physical scene.

The browser UI remains frozen. N40 is implemented only in the native PySide6/PyVista editor.

## 2. Domain decisions

### 2.1 Entity kinds

`SceneEntity` remains the stable scene entity and is extended from the current `speaker | measurement_point | furniture` set to:

- `speaker`
- `seat`
- `screen`
- `furniture`
- `av_equipment`
- `measurement_point`

All physical objects have a body `position`, `orientation`, and `size_m`. Measurement points are reference points and do not require physical dimensions.

### 2.2 Physical body vs acoustic reference

A seat is a physical body. The listener/acoustic reference is not inferred from the body's origin. Physical dimensions and acoustic reference points must therefore be represented separately.

N40 adds an optional entity-local `acoustic_reference_offset_m`. The world acoustic reference is `body position + body-orientation * offset`. This allows a seat reference point (ear/listener point), a speaker acoustic center, or another equipment reference to remain attached to the body without conflating it with the body's center.

Measurement points remain standalone point entities when a reference is not attached to a body.

### 2.3 Speaker roles and aim

`speaker_role` remains a string and is not restricted to a hard-coded 3.0.2 enum. The 3.0.2 layout is a palette/template convenience and an A10 fixture.

Speaker `aim_xyz=None` continues to mean unknown. Adding, moving, duplicating, or resizing a speaker must not silently convert unknown aim into known aim.

### 2.4 Hide / lock

Hide and lock remain editor view/edit state (`EditorViewState` + repository view state), not physical `SceneDocument` data. They must not change measurement conditions or scene content hashes.

## 3. Command / history contract

N40 adds domain commands for:

- add entity
- duplicate entity
- replace entity properties (dimensions / role / acoustic reference)

Every committed action is one Undo unit. Duplicate creates a new stable ID and keeps the original entity unchanged. Property edits preserve unrelated fields.

Delete remains the existing command. Move/rotate remain the accepted N20 path.

## 4. Native UI

### 4.1 Object palette

Add a left-side `オブジェクト` palette with compact, directly clickable items:

- スピーカー
- 座席
- スクリーン
- 家具
- AV機器
- 測定点
- `3.0.2` template

Adding a single object creates it at a deterministic visible placement near the room center / current working area, selects it immediately, switches to Move, and lets the existing gizmo perform precise mouse placement. This keeps insertion and transform on the same accepted interaction path instead of introducing a second drag engine.

The 3.0.2 template creates FL/C/FR plus two height speakers using standard roles but does not constrain later edits or additional roles.

### 4.2 Inspector

Extend the existing Japanese Inspector with:

- body dimensions X/Y/Z for physical objects
- speaker role for speakers
- acoustic reference offset X/Y/Z when supported
- duplicate action

Position/orientation continue to use the existing numeric controls. Numeric controls are refinement, not the primary placement workflow.

### 4.3 Scene tree and rendering

Scene tree groups are Japanese and distinguish all N40 kinds. Shapes must also distinguish object categories rather than relying on color alone:

- speaker: box with forward marker
- seat: body box + back marker
- screen: thin rectangular panel
- furniture: box
- AV equipment: rack-like box
- measurement point: sphere/cross marker

The first implementation may use simple generated PyVista primitives; imported asset/model libraries are not part of N40.

## 5. Tool-mode composition

Object creation/edit is disabled while room sketch/edit or wall edit owns viewport interaction. Starting room/wall editing cancels object insertion state. Existing N30b topology transaction rules are unchanged.

After wall topology exists, `CadEditorWindow` remains the product composition point; N40 is layered there rather than bypassing the room/wall guard.

## 6. Persistence / schema

The new optional fields and kinds are persisted through the existing `SceneDocument` JSON/SQLite revision repository. Schema version is bumped only if validation requires an explicit contract change; old optional-field hashes should not be gratuitously changed.

A10 explicitly verifies exact role, dimensions, position, and acoustic reference values after save/reopen.

## 7. Focused automated verification

Add tests only for new invariants with meaningful regression cost:

- each physical kind validates dimensions; measurement point remains dimensionless-capable
- speaker-only fields remain speaker-only
- acoustic reference transform is deterministic
- add / duplicate / property replacement are one Undo/Redo unit and preserve unrelated fields
- repository save/reopen preserves N40 entity data exactly

Do not add pixel tests or tests for reversible label/layout details.

## 8. A10 Windows gate

RDC is reserved for the final A10 gate. The harness will use the normal `htdt.native_cad` product composition and real Win32 mouse input, similar to A08/A09.

Required flow:

1. empty project
2. draw room (including the short L-shaped first-use check)
3. create 3.0.2 speakers from palette/template and reposition with mouse
4. create seat, screen, furniture
5. duplicate at least one object
6. exercise hide/lock without modifying physical document
7. change a dimension and role numerically
8. make an incorrect transform and Undo it
9. Save, close/reopen
10. compare IDs/roles/dimensions/positions/reference data exactly

Record elapsed time and first-use friction separately from correctness. Fix confusing workflow before marking N40 complete.
