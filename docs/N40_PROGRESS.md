# N40 implementation progress

Tracking: #55  
Branch: `feat/n40-theater-objects`  
Merged: PR #56 → `16474b53403ec03f9fb819a0421928c2ea49be33`

## 2026-09-17 — start

- Confirmed `main` head `51d7125103eb391a2645f394988aa650839b0dac`.
- Confirmed N30b is merged and A09 is complete.
- Re-read the N40 roadmap and A10 acceptance contract before implementation.
- Reviewed current `SceneEntity`, `WorkingDocument`, native editor, room editor, wall editor, and view-state contracts.
- Reuse decision: keep `SceneEntity` / `SceneDocument`, command history, accepted move/rotate gizmos, `EditorViewState` hide/lock, and `CadEditorWindow` as product composition.
- New N40 scope was limited to typed theater-object semantics, add/duplicate/property commands, palette/Inspector/rendering, persistence tests, and A10.
- Browser UI remains frozen.
- RDC was reserved for the final Windows A10 gate; all implementation changes were made on GitHub.

## 2026-09-17 — implementation

- Extended theater entities to speaker / seat / screen / furniture / AV equipment / measurement point while keeping physical body and acoustic reference semantics separate.
- Added body-local `acoustic_reference_offset_m` and exact world reference calculation.
- Added atomic add, duplicate, and property replacement command paths with Undo/Redo and stable IDs.
- Made the 3.0.2 template a single Undo unit without constraining later speaker roles.
- Added Japanese-first native object palette, object Inspector, differentiated viewport primitives, duplicate, and existing hide/lock integration.
- Split product composition into `cad_composition.py`, `theater_editor.py`, and `theater_workflow.py` so N30b room/wall guards remain isolated from N40 theater behavior.
- Added explicit `座席へ向ける` acoustic action. It updates only speaker `aim_xyz`, never body pose, and can update multiple speakers atomically in one Undo unit.
- Added focused domain/repository tests for physical dimensions, acoustic references, atomic add/property commands, speaker aim, Undo/Redo, and save/reopen exactness.
- Added `scripts/validate_n40_windows.py` plus viewport-aware `validate_n40_windows_gate.py` for the mouse-first A10 flow, and `validate_n40_precision_windows.py` for numeric dimension/role refinement and exact reopen.
- Accidental placeholder Issues #57 and #58 were immediately closed as `not planned`; N40 tracking remained Issue #55 / PR #56.

## 2026-09-17 — final A10 iterations

The Windows gate was used to discover first-use friction and the product was corrected before acceptance:

- Fixed A10 fixture framing for the narrower 200% DPI viewport created by N40 side docks.
- Changed successful room creation to return directly to object mode; explicit room editing remains available when needed.
- Moved the default screen closer to the front wall so it does not cover front speakers in Top view.
- Staggered default insertion lanes for furniture, AV equipment, measurement points, and standalone speakers so newly added objects are not stacked on the seat or each other.
- The repository-supported Python 3.12 `.venv` was used for the final gate; the machine's unrelated default Python 3.14 had no PySide6 installed.

Final accepted product code is `2e2a3f814276bc3fcd3f392addfd70a10efbe295`.

GitHub Actions CI #220 (`35196621511`) passed on that code.

Owned Windows hardware A10 also passed on that code:

- core mouse-first A10: PASS
- numeric precision/reopen companion: PASS
- first-use elapsed: 15.75 s
- final mis-selections: 0
- final extra guidance required: 0
- worktree before acceptance: clean
- worktree after acceptance: clean

Full evidence: `docs/N40_ACCEPTANCE_2026-09-17.md`.

## 2026-09-17 — completion

- PR #56 was marked ready after design, focused tests, CI, and Windows A10 were complete.
- PR #56 merged to `main` as merge commit `16474b53403ec03f9fb819a0421928c2ea49be33`.
- Issue #55 closed automatically with state reason `completed`.
- N40 is complete. The roadmap's next milestone is N50 — constraint spatial visualization / A11.

## Implementation sequence

1. ~~Extend N40 entity semantics and acoustic reference helper.~~
2. ~~Add add/duplicate/property commands to working document.~~
3. ~~Add focused domain/repository tests.~~
4. ~~Add native palette, dimensions/role/reference Inspector, and differentiated primitives.~~
5. ~~Compose with room/wall modes and Japanese UI.~~
6. ~~Add A10 harness and CI compile coverage.~~
7. ~~Complete final CI on accepted product code.~~
8. ~~Run A10 core + numeric precision acceptance on owned Windows hardware using real mouse input.~~
9. ~~Record acceptance evidence.~~
10. ~~Update implementation status, mark PR ready, and merge.~~

## Resolved design risks

- Body pose and acoustic reference point remain distinct.
- Unknown speaker aim does not become known through add/move/duplicate/resize.
- Hide/lock stay outside physical scene data.
- N30b stable wall/opening/constraint transaction rules remain in the product composition path.
- 3.0.2 remains a template/fixture, not a closed speaker-role schema.
- `aim_xyz` is an explicit world direction, not a persistent target constraint; moving a speaker after aiming does not silently re-aim it.
