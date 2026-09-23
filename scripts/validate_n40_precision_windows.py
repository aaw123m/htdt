from __future__ import annotations

import ctypes
import gc
from pathlib import Path
import sys
import tempfile
import time

import numpy as np
from PySide6.QtCore import QPoint
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend' / 'src'))

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import F1_DOCUMENT_ID, make_f1_scene
from htdt.native_cad import TheaterEditorWindow

if sys.platform != 'win32':
    raise SystemExit('This acceptance harness requires Windows.')

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(errors='backslashreplace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(errors='backslashreplace')

user32 = ctypes.windll.user32
MOUSE_LEFTDOWN = 0x0002
MOUSE_LEFTUP = 0x0004


def pump(app: QApplication, seconds: float = 0.05) -> None:
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        app.processEvents()
        time.sleep(0.002)


def wait_until(app: QApplication, predicate, seconds: float = 1.0) -> bool:
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.002)
    return bool(predicate())


def foreground(window: TheaterEditorWindow, app: QApplication) -> None:
    window.show()
    window.showNormal()
    window.raise_()
    window.activateWindow()
    window.viewport.interactor.setFocus()
    user32.SetForegroundWindow(int(window.winId()))
    user32.mouse_event(MOUSE_LEFTUP, 0, 0, 0, 0)
    pump(app, 0.20)


def world_to_global(window: TheaterEditorWindow, xyz) -> QPoint:
    renderer = window.viewport.renderer
    renderer.SetWorldPoint(float(xyz[0]), float(xyz[1]), float(xyz[2]), 1.0)
    renderer.WorldToDisplay()
    display_x, display_y, _ = renderer.GetDisplayPoint()
    dpr = max(float(window.viewport.interactor.devicePixelRatioF()), 1e-9)
    _, render_height = window.viewport.render_window.GetSize()
    local = QPoint(
        int(round(float(display_x) / dpr)),
        int(round((float(render_height) - float(display_y)) / dpr)),
    )
    return window.viewport.interactor.mapToGlobal(local)


def click_actor(window: TheaterEditorWindow, entity_id: str, app: QApplication) -> bool:
    foreground(window, app)
    actor = window.actors.get(entity_id)
    if actor is None:
        return False
    point = world_to_global(window, np.asarray(actor.center, dtype=float))
    QCursor.setPos(point)
    pump(app, 0.05)
    user32.mouse_event(MOUSE_LEFTDOWN, 0, 0, 0, 0)
    pump(app, 0.03)
    user32.mouse_event(MOUSE_LEFTUP, 0, 0, 0, 0)
    return wait_until(app, lambda: window.selected_id == entity_id, 0.75)


def run_precision(app: QApplication, root: Path) -> bool:
    repository = SceneRepository(root / 'scene.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)
    window = TheaterEditorWindow(repository, F1_DOCUMENT_ID)
    foreground(window, app)
    reopened = None
    try:
        selected = click_actor(window, 'speaker-fl', app)
        print('A10_PRECISION_MOUSE_SELECT', selected, flush=True)
        if not selected:
            return False

        before = window.working.committed_document.entity('speaker-fl')
        if before.size_m is None:
            return False
        before_history = window.working.history_length
        target_x = before.size_m.x_m + 0.031

        window.size_fields['X'].setValue(target_x)
        window.size_fields['X'].editingFinished.emit()
        pump(app, 0.10)
        window.speaker_role_field.setText('FL-precision')
        window.speaker_role_field.editingFinished.emit()
        pump(app, 0.10)

        after = window.working.committed_document.entity('speaker-fl')
        edit_ok = (
            after.size_m is not None
            and abs(after.size_m.x_m - target_x) <= 1e-9
            and after.speaker_role == 'FL-precision'
            and after.position == before.position
            and after.orientation == before.orientation
            and after.aim_xyz == before.aim_xyz
            and window.working.history_length == before_history + 2
        )
        print('A10_PRECISION_NUMERIC_EDIT', edit_ok, flush=True)
        if not edit_ok:
            return False

        expected = window.working.committed_document
        window.save()
        pump(app, 0.10)
        save_ok = not window.working.is_dirty
        print('A10_PRECISION_SAVE', save_ok, flush=True)
        if not save_ok:
            return False

        window.close()
        pump(app, 0.10)
        window.deleteLater()
        pump(app, 0.08)

        reopened = TheaterEditorWindow(repository, F1_DOCUMENT_ID)
        foreground(reopened, app)
        reopened_entity = reopened.working.committed_document.entity('speaker-fl')
        reopen_ok = (
            reopened.working.committed_document == expected
            and reopened_entity.size_m == after.size_m
            and reopened_entity.speaker_role == 'FL-precision'
            and reopened_entity.position == before.position
            and reopened_entity.orientation == before.orientation
            and reopened_entity.aim_xyz == before.aim_xyz
        )
        print('A10_PRECISION_REOPEN_EXACT', reopen_ok, flush=True)
        return reopen_ok
    finally:
        if reopened is not None:
            reopened.close()
            pump(app, 0.08)
            reopened.deleteLater()
        if window.isVisible():
            window.close()
            pump(app, 0.08)
            window.deleteLater()
        gc.collect()
        pump(app, 0.05)


def main() -> int:
    app = QApplication.instance() or QApplication([sys.argv[0]])
    with tempfile.TemporaryDirectory(prefix='htdt-a10-precision-', ignore_cleanup_errors=True) as temp:
        passed = run_precision(app, Path(temp))
    print('A10_PRECISION_RESULT', 'PASS' if passed else 'FAIL', flush=True)
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
