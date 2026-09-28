---
name: testing-htdt-gui
description: How to launch and test HTDT's PySide6/VTK Qt desktop app on this GPU-less Windows VM (Mesa3D OpenGL drop-in required or every VTK viewport segfaults), plus which surfaces to use for dock/editor testing.
---

# Testing HTDT GUI (PySide6 + VTK) on this VM

## Critical: Mesa3D software OpenGL required

The VM display adapter is `IddSampleDriver` (virtual display, no GPU). Windows falls back to GDI Generic OpenGL 1.1, so every VTK `QtInteractor`/`QVTKRenderWindowInteractor` widget **segfaults immediately** on construction ("Failed to initialize OpenGL functions!" then `Segmentation fault`). `QT_OPENGL=software` does **not** help — VTK uses raw WGL, bypassing Qt.

**Fix (one-time):** drop Mesa3D's software `opengl32.dll` next to `python.exe` so it loads before `C:\Windows\System32\opengl32.dll`:

1. Download `mesa-dist-win-<ver>-release-msvc.7z` from https://www.mesa3d.org/ (e.g. 26.2.3).
2. Extract `x64\opengl32.dll` **and** `x64\libgallium_wgl.dll` — both are needed (opengl32.dll delegates to gallium).
   - `py7zr` may fail on this archive (BCJ2 filter); download standalone `7zr.exe` from https://www.7-zip.org/ and run `7zr x archive.7z`.
3. Copy both DLLs into `C:\devin\python\` (the interpreter directory).

Verify: `C:/devin/python/python.exe -m htdt.wall_editor` should show a window with rendered 3D axes instead of segfaulting.

## Launch commands

- Default workflow shell: `cd backend && PYTHONIOENCODING=utf-8 C:/devin/python/python.exe -m htdt.native_cad`
  - First run auto-creates project "My Home Theater" under `%LOCALAPPDATA%\HomeTheaterDigitalTwin` — no picker dialog.
  - If the previous run exited uncleanly (crash/taskkill), a Japanese recovery dialog appears before the main window.
  - `--legacy-ui` currently crashes with `AttributeError: ... 'search_generate_reason_label'` (pre-existing init-order bug in optimization controllers — unrelated to i18n).
- Standalone legacy windows (own `app.exec()`, no project needed):
  - `python -m htdt.wall_editor` — WallEditorWindow: room+wall toolbars, scene tree, status bar, right docks **vertically stacked** (no tabification here).
  - `python -m htdt.room_editor`, `python -m htdt.native_editor` — subsets of the same chain.
- Dock tab-stack surface (MeasurementWorkspaceWindow unifies right docks into one tab group — this is where dock-title/tabify fixes must be verified):

  ```python
  # launch_measurement.py
  import sys
  from PySide6.QtWidgets import QApplication
  from htdt.measurement_workspace import MeasurementWorkspaceWindow
  from htdt.scene_repository import SceneRepository
  from htdt.paths import default_data_dir
  app = QApplication(sys.argv)
  w = MeasurementWorkspaceWindow(SceneRepository(default_data_dir() / 'cad-scenes.sqlite3'))
  w.show()
  app.exec()
  ```

## Testing notes

- **Toolbar overflow `»` may not open on click** in this environment — room edit actions (部屋を閉じる/部屋編集を終了/頂点を挿入/頂点を削除) hide behind it at 1024px width. Workarounds: `Enter` closes an active sketch (sketch mode only — does NOT exit edit mode), `Esc` cancels sketch or exits wall-edit mode; entering wall edit auto-exits room edit.
- `Enter`/`Esc` are handled by the window eventFilter — click the viewport first if focus is in a dock widget.
- Status bar sits at the very bottom edge of a maximized window; zoom regions of ~y=735 work, but screenshots at full size are more reliable than narrow zoom bands.
- The statusbar shows the current tool's guidance — one of the best surfaces for verifying localized strings.
- Expected English leftovers (by design): REW, dB, Hz, GP, Pareto, channel ids (FL/FR), VTK axis widget labels ("X Axis", "Distance" legend), domain enum values (porous_absorber, baseline vs A), stock Qt buttons (OK/Cancel) on QInputDialogs.
