# N10 Windows acceptance — 2026-09-16

This record applies to Issue #44 and branch `feat/n10-editor-shell-recovery`. The Windows acceptance code under test was head `1f44d0f2a957f9d2908fbb9d713bbf853d262718`; later commits in the same PR may add only acceptance/status documentation unless stated otherwise.

## Environment

Same owned Windows machine used for N05:

- Windows 11 Pro x64, version `10.0.26200`, build `26200`
- CPU: AMD Ryzen 7 8845HS with Radeon 780M Graphics
- GPU: AMD Radeon 780M Graphics, driver `32.0.13032.11`
- RAM: 31.3 GiB reported
- display: 2880×1800 physical, OS scaling 200% / 192 DPI
- Qt screen: 1440×900 logical, DPR 2.0
- Python 3.12.10 x64
- PySide6 6.11.2 / PyVista 0.49.0 / PyVistaQt 0.13.1 / VTK 9.7.0
- fixture: F1

The N05 Windows dependency lock remains `backend/requirements-n05-windows.lock`; N10 added no third-party dependency.

## Automated regression

Before GUI acceptance, the focused editor/repository suite completed with 10 tests passing. After the picker fix described below, the complete backend suite again completed with exit code 0; one existing test was skipped. Only the previously known Starlette/httpx deprecation warnings were emitted.

N10 tests are restricted to state contracts with meaningful regression cost:

- Delete command Undo/Redo restores the same entity and original ordering.
- Undo followed by a new command discards the redo branch.
- EditorViewState remains outside the physical SceneDocument.
- recovery snapshot remains separate from formal SceneRevision.
- formal Save clears recovery only after success.
- failed Save leaves WorkingDocument/history/dirty state and the latest formal revision unchanged.
- persisted selection/hide/lock does not change SceneRevision content hash.

## A03 — shell selection / hide / lock / delete / reopen

A temporary acceptance harness outside the repository sent real Windows mouse input to the visible Qt/VTK application and read state from the running editor/repository. The harness is not a product artifact and is removed after this record is committed.

1. Real mouse click on the Scene tree selected `speaker-fl`; Inspector synchronized. The tree selection callback delta was exactly 1.
2. Real mouse click on the viewport selected `speaker-c`; Inspector synchronized.
3. During the first run, the viewport click produced **two** raw scene-pick callbacks even though selection was correct. Investigation found that `TranslationWidget3D._pick_handle()` reused `interactor.GetPicker()`. Calling `Pick()` for gizmo hit-testing therefore emitted the scene picker's `EndPickEvent` in addition to the normal scene pick.
4. The gizmo was changed to use its own `vtkPropPicker`. Repeating the same real mouse sequence reduced the viewport callback delta from **2 to 1**. This is the accepted implementation.
5. `speaker-fl` was hidden through the toolbar. Its tree item remained, its physical SceneDocument entity remained, and its viewport actor/pick target was absent.
6. `speaker-c` was locked. It remained selectable, but Inspector position fields were disabled and no translation gizmo was attached.
7. After unlocking, `speaker-c` was deleted. The entity disappeared from SceneDocument and no actor mapping remained for it.
8. Undo restored `speaker-c` with the same entity ID/content and a new actor mapping.
9. The editor was closed and reopened on the same DB. The Scene entity IDs were unchanged; `speaker-fl` remained hidden and `speaker-c` remained locked as separate EditorViewState.

Observed final sequence:

```text
A03_TREE_SELECTION_OK 1
A03_VIEWPORT_CALLBACK_DELTA 1
A03_VIEWPORT_SELECTION_OK 1
A03_HIDE_OK
A03_LOCK_OK
A03_DELETE_OK 3
A03_REOPEN_OK ('speaker-fl', 'speaker-c', 'speaker-fr', 'point-mlp', 'furniture-left')
```

Result: **A03 pass on the owned Windows PC.**

## A04 — history branching / Save failure / recovery

The same harness exercised normal editor handlers and repository transactions. A local `SceneRepository` test double injected one Save failure; only the error dialog was made non-blocking so the acceptance could continue automatically. Product code handled the failure.

1. FL X was changed from 1.35 m to 1.50 m. A recovery snapshot was created while the formal revision remained unchanged.
2. Save succeeded, creating a new immutable formal revision at X=1.50 m, clearing dirty state and clearing the recovery snapshot.
3. Undo returned the draft to X=1.35 m and enabled Redo without changing the saved revision.
4. A new numeric edit set X=1.40 m. The redo branch was discarded and a recovery snapshot at X=1.40 m was stored.
5. The next formal Save was deliberately failed. WorkingDocument content, history length, source revision and dirty state remained unchanged; the latest formal revision remained X=1.50 m, and recovery remained X=1.40 m.
6. The editor was closed and reopened. It displayed the formal X=1.50 m scene and reported a pending recovery; physical editing controls/gizmo were blocked until an explicit choice.
7. `Recover Draft` opened X=1.40 m as a dirty WorkingDocument whose source remained the X=1.50 m revision.
8. Saving the recovered draft created a new formal revision whose parent was the X=1.50 m revision. The parent revision remained unchanged and recovery was cleared only after successful Save.

Observed final sequence:

```text
A04_SAVE_OK <revision-2>
A04_REDO_BRANCH_OK
A04_SAVE_FAILURE_OK
A04_RECOVERY_CANDIDATE_OK
A04_RECOVER_OK
A04_RECOVERY_SAVE_OK <revision-3>
N10_A03_A04_PASS
```

Result: **A04 pass on the owned Windows PC.**

## Scope and remaining limits

- N10 implements ViewState hide/edit-lock, single-object Delete history and minimal recovery. It does not implement N20 Rotate, multi-select, snap, capture-loss/Alt+Tab handling, or full project management.
- ViewState persistence is intentionally separate from SceneRevision and does not represent acoustic/physical state.
- Recovery is one latest committed draft per document, not event sourcing or a revision browser.
- Recovery choice is explicit. While a startup recovery candidate is pending, physical edits are blocked so a new edit cannot overwrite the older crash-recovery draft.
- The N05 standalone package was not rebuilt solely for N10 because A03/A04 do not change the packaging stack; the final N10 PR still runs the repository Windows CI and native script syntax checks.

With A03/A04 passing, the N10 shell/save/recovery gate is ready for PR review and can proceed to N20a after merge.