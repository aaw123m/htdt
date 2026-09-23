# N05 Windows acceptance — 2026-09-16

This record applies to Issue #41 and PR #43. The executable code under test was PR head `c4ee5c8d3856d7cf1ec05d590775d66e67ef244b`; later commits in the same PR only add the dependency lock and acceptance/status documentation unless stated otherwise.

## Environment

- Windows 11 Pro x64, version `10.0.26200`, build `26200`
- CPU: AMD Ryzen 7 8845HS with Radeon 780M Graphics
- GPU: AMD Radeon 780M Graphics, driver `32.0.13032.11`
- RAM: 31.3 GiB reported
- display: 2880×1800 physical, OS scaling 200% / 192 DPI
- Qt screen: 1440×900 logical, DPR 2.0
- Python 3.12.10 x64
- PySide6 6.11.2 / PyVista 0.49.0 / PyVistaQt 0.13.1 / VTK 9.7.0
- PyInstaller 6.22.3
- fixture: CAD acceptance F1, 6×4×2.4 m, FL/C/FR + MLP + left furniture, FL aim unknown and FR aim known

The complete third-party package closure resolved on this PC is fixed in `backend/requirements-n05-windows.lock`.

## Automated regression before GUI acceptance

`pip install -e ".\backend[dev,package]"` succeeded in the existing project venv. The complete backend pytest suite completed with exit code 0; one existing test was skipped. GitHub Actions CI run `35100502686` for PR #43 also completed successfully.

Tests added for N05 are limited to high-regression contracts: preview/cancel, one committed move as one Undo command, no-op history suppression, unknown aim preservation, domain↔VTK coordinate conversion, immutable save/reopen/no-op and parent revision behavior.

## A01 — F1 editing vertical slice

### Native 200% DPI result

A real Windows mouse input sequence was sent to the visible Qt/VTK application at the machine's native 200% scaling. The application state was read back from the same running `WorkingDocument`/repository rather than inferred from pixels.

1. viewport pick selected `speaker-fl`; Inspector synchronized to X=1.3500, Y=0.7500, Z=1.0500 m and `Aim=unknown`.
2. physical X-axis gizmo drag entered preview and moved X from 1.35 m to approximately 1.3664614 m.
3. mouse release committed exactly one history command (`history_length=1`).
4. Inspector numeric correction set X to 1.4165 m through the normal numeric edit handler (`history_length=2`).
5. a second real mouse drag entered preview; the Escape key cancelled it. Position remained 1.4165 m and history remained length 2.
6. Ctrl+Z returned to the first drag result (~1.3664614 m) and enabled Redo.
7. Ctrl+S created a new immutable SceneRevision with the original seed revision as parent and cleared dirty state.
8. the editor window was closed and a new editor window opened on the same SQLite repository. FL reopened at the saved position and its aim remained unknown.

Result: **A01 pass at native 200% OS DPI.**

### Additional scale-factor checks

The PC's OS display scaling was not changed automatically. To exercise coordinate conversion and handle hit-testing at the other acceptance scales without changing the user's desktop configuration, the same real Windows mouse sequence was repeated with Qt scale overrides:

- effective 100% (`QT_SCALE_FACTOR=0.5` on the 200% display): pass
- effective 150% (`QT_SCALE_FACTOR=0.75` on the 200% display): pass
- native 200%: pass as above

These 100%/150% runs are process-scale checks, not claims that Windows itself was switched to those DPI settings. Multi-monitor mixed-DPI movement remains untested because no second display was used.

## Rendering evidence and capture limitation

Qt `QWidget.grab()` and VTK framebuffer screenshots both showed F1 correctly, including room box, entities, selected FL, Inspector values, and the three-axis translation gizmo. Windows desktop/PrintWindow capture returned a white Qt/OpenGL client surface even while UI Automation and Qt/VTK internal capture showed the application correctly. This is recorded as a capture-tool limitation, not a renderer failure. Generated screenshots remain local acceptance artifacts and are not committed to avoid repository bloat.

## A02 — standalone package

`scripts/build-native.ps1` built an onedir package successfully with PyInstaller 6.22.3. The build uses a fresh Python 3.12 venv and does not reuse the development venv.

For the runtime check, the repository development `.venv` directory was temporarily renamed so it could not be imported or executed. `HTDT.exe` was then started with an empty `PYTHONPATH` from the package directory.

- main window created: `Home Theater Digital Twin — N05`
- no new Chrome/Edge/Firefox process was created
- no Qt platform plugin or VTK DLL failure occurred before window creation
- WM_CLOSE shut down the application normally with exit code 0
- the development `.venv` was restored immediately afterward

Result: **A02 pass on the owned Windows PC.**

PyInstaller emitted collection warnings for optional modules such as `trame_pyvista`/`tzdata`; the N05 desktop path does not import them and the packaged application started successfully. They are not blockers for this slice.

## Remaining N05 limitations

- Native OS DPI was directly tested only at 200%; 100%/150% are Qt process-scale checks as described above.
- mixed-DPI multi-monitor movement is not tested.
- N05 intentionally provides one selected speaker translation path, not N20 move/rotate/multi-select/snap completeness.
- package size/performance optimization is not an N05 gate; current PyInstaller collection favors reproducibility over minimum footprint.
- recovery snapshots, hide/lock/delete lifecycle and broader project management are N10 gates.

With these limitations stated, A01/A02 no longer block the selected PySide6 + PyVista/VTK stack from proceeding to N10.
