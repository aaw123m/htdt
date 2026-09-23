# N30a A08 acceptance — 2026-09-17

Status: **PASS**

Branch: `feat/n30a-room-sketch`  
Issue: #51  
PR: #52  
Actual-mouse acceptance product commit: `34536335129df73c1cf19442d9f3d2a1a3ee90de`

## Environment

- Windows 11 x64, owned PC
- Ryzen 7 8845HS / Radeon 780M / 31.3 GiB
- Physical display: 2880×1800
- OS scaling: 200%
- Qt logical screen: 1440×900 / DPR 2.0
- Python 3.12.10
- PySide6 6.11.2 / PyVista 0.49.0 / PyVistaQt 0.13.1 / VTK 9.7.0 / Shapely 2.1.2
- Fixture: `CAD_EDITOR_ACCEPTANCE.md` F2, 8-vertex concave room, height 2.4 m

## Implemented N30a scope

- empty SceneDocument seed with no room;
- explicit polygon-prism room with stable vertex IDs;
- concave simple polygon validation and self-intersection rejection;
- negative XY coordinates and footprint-derived bounds;
- Top-view click sketch, start-point/Enter close, Esc cancel;
- vertex selection, mouse drag, midpoint insertion and vertex deletion;
- exact vertex X/Y, edge length and ceiling-height numeric editing;
- polygon-prism viewport rendering and edit handles;
- room replacement through CommandHistory as one operation per committed edit;
- Undo/Redo and recovery snapshot integration;
- N20b entity editing remains in the inherited native editor shell;
- `htdt-native`, PowerShell launcher and package entry now open the N30a room editor.

## A08 actual Windows input result

`python scripts/validate_n30a_windows.py` was executed from the tracked checkout at `C:\Users\ka092\Desktop\HTDT\repo` using Win32 OS mouse input against the real Qt/VTK window.

| Check | Result |
|---|---|
| F2 click sketch from empty scene | PASS — 8 vertices, one history command |
| F2 bounds | PASS — approximately 0…6 m X and 0…4 m Y; small sub-pixel click quantization stays within 0.03 m harness tolerance |
| edge-midpoint vertex insertion | PASS — 9 vertices, stable new vertex ID, one Undo |
| inserted vertex mouse drag | PASS — moved to approximately (3.000, 0.501) m, one Undo |
| exact edge-length edit | PASS — one history command |
| ceiling-height edit to 2.6 m | PASS — one history command |
| inserted vertex deletion | PASS — back to 8 vertices, one history command |
| invalid/self-intersecting drag | PASS — committed room unchanged and history length unchanged |
| Undo/Redo after room edits | PASS — exact room snapshot restored |

Harness final output:

```text
A08_DRAW_F2 True HISTORY 1
A08_INSERT_VERTEX True
A08_MOVE_VERTEX True
A08_EDGE_DIMENSION True
A08_HEIGHT True 2.6
A08_DELETE_VERTEX True HISTORY 6
A08_INVALID_REJECT True HISTORY 6
A08_UNDO_REDO True
A08_RESULT PASS
A08_EXIT=0
```

## DPI/input correction discovered during acceptance

The owned machine uses 200% DPI. The first harness revision mixed VTK physical display pixels with Qt logical pixels when targeting room edit handles. F2 sketch itself worked, but midpoint/vertex handle presses were unstable.

The final harness deliberately separates the coordinate paths:

- floor/sketch targets: domain → VTK display → global OS cursor;
- room handle presses: the editor's own `_project_room_to_qt()` logical-pixel projection → global OS cursor.

After room rebuilds, the harness also restores the editor as the foreground window and normalizes the Win32 left-button state before handle input. This is an acceptance-harness correction, not a geometry tolerance change in the product.

## Automated validation

N30a adds focused tests only for meaningful domain/revision invariants: concave room construction, negative coordinates and derived bounds, invalid polygon rejection, one-command room Undo/Redo, no-op history behavior, legacy rectangular serialization compatibility, and polygon room repository round-trip.

The normal Windows PR CI runs the full backend regression, native launcher CLI check, both Windows CAD harness syntax checks, PowerShell syntax, frontend build and built-app smoke. The final PR head must be green before merge.

## Non-scope / next milestone

N30a does **not** claim completion of stable wall IDs, wall split/merge, openings, wall thickness or constraint-reference migration. Those remain N30b / A09.

Next native CAD milestone: **N30b — wall / opening editing and reference-preserving transactions**.
