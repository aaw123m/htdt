from __future__ import annotations

import argparse
import ctypes
import gc
from pathlib import Path
import sys
import tempfile
import time

import numpy as np
from PySide6.QtCore import QPoint
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication, QPushButton, QToolBar, QWidget

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend' / 'src'))

from htdt.cad_gizmo import TranslationWidget3D
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene, room_vertices
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
FIXTURE_ID = 'fixture-a10-n40'
L_ROOM_POINTS = (
    (0.0, 0.0),
    (6.0, 0.0),
    (6.0, 4.0),
    (4.0, 4.0),
    (4.0, 2.0),
    (2.0, 2.0),
    (2.0, 4.0),
    (0.0, 4.0),
)


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


def click_global(point: QPoint, app: QApplication) -> None:
    QCursor.setPos(point)
    pump(app, 0.04)
    user32.mouse_event(MOUSE_LEFTDOWN, 0, 0, 0, 0)
    pump(app, 0.03)
    user32.mouse_event(MOUSE_LEFTUP, 0, 0, 0, 0)
    pump(app, 0.08)


def click_widget(widget: QWidget, app: QApplication) -> None:
    click_global(widget.mapToGlobal(widget.rect().center()), app)


def action_widget(window: TheaterEditorWindow, action) -> QWidget:
    for toolbar in window.findChildren(QToolBar):
        widget = toolbar.widgetForAction(action)
        if widget is not None:
            return widget
    raise AssertionError(f'action has no visible toolbar widget: {action.text()}')


def click_action(window: TheaterEditorWindow, action, app: QApplication) -> None:
    foreground(window, app)
    widget = action_widget(window, action)
    if not widget.isEnabled():
        raise AssertionError(f'action is disabled: {action.text()}')
    click_widget(widget, app)


def object_button(window: TheaterEditorWindow, prefix: str) -> QPushButton:
    for button in window.object_buttons:
        if button.text().startswith(prefix):
            return button
    raise AssertionError(f'object palette button not found: {prefix}')


def click_object_button(window: TheaterEditorWindow, prefix: str, app: QApplication) -> None:
    foreground(window, app)
    button = object_button(window, prefix)
    if not button.isEnabled():
        raise AssertionError(f'object palette button disabled: {button.text()}')
    click_widget(button, app)


def set_top_fixture_camera(window: TheaterEditorWindow) -> None:
    renderer = window.viewport.renderer
    camera = renderer.GetActiveCamera()
    camera.SetFocalPoint(3.0, -2.0, 0.0)
    camera.SetPosition(3.0, -2.0, 10.0)
    camera.SetViewUp(0.0, 1.0, 0.0)
    camera.ParallelProjectionOn()
    camera.SetParallelScale(3.6)
    renderer.ResetCameraClippingRange()
    window.viewport.render()


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


def room_point_global(window: TheaterEditorWindow, x_m: float, y_m: float) -> QPoint:
    return world_to_global(window, (x_m, -y_m, 0.0))


def click_actor(window: TheaterEditorWindow, entity_id: str, app: QApplication) -> None:
    foreground(window, app)
    actor = window.actors.get(entity_id)
    if actor is None:
        raise AssertionError(f'entity actor unavailable: {entity_id}')
    click_global(world_to_global(window, np.asarray(actor.center, dtype=float)), app)
    if not wait_until(app, lambda: window.selected_id == entity_id, 0.75):
        raise AssertionError(f'viewport selection failed: {entity_id}')


def drag_selected_x(window: TheaterEditorWindow, app: QApplication, *, scale: float) -> bool:
    window._object_snap_toggled(False)
    window._grid_snap_toggled(False)
    window._set_transform_mode('move')
    window._top()
    pump(app, 0.12)
    if not isinstance(window.gizmo, TranslationWidget3D):
        return False
    gizmo = window.gizmo
    start_world = gizmo.origin + gizmo.axes[0] * gizmo.actor_length * 0.75 * 0.45
    end_world = gizmo.origin + gizmo.axes[0] * gizmo.actor_length * 0.75 * scale
    start = world_to_global(window, start_world)
    end = world_to_global(window, end_world)
    QCursor.setPos(start)
    pump(app, 0.05)
    user32.mouse_event(MOUSE_LEFTDOWN, 0, 0, 0, 0)
    pump(app, 0.05)
    if not isinstance(window.gizmo, TranslationWidget3D) or not window.gizmo.pressing:
        user32.mouse_event(MOUSE_LEFTUP, 0, 0, 0, 0)
        return False
    QCursor.setPos(end)
    pump(app, 0.12)
    user32.mouse_event(MOUSE_LEFTUP, 0, 0, 0, 0)
    return wait_until(app, lambda: not window.working.has_preview, 1.0)


def run_a10(app: QApplication, root: Path) -> bool:
    started = time.perf_counter()
    repository = SceneRepository(root / 'scene.sqlite3')
    repository.save(make_empty_scene(FIXTURE_ID), parent_revision_id=None)
    window = TheaterEditorWindow(repository, FIXTURE_ID)
    foreground(window, app)
    reopened = None
    try:
        japanese_ui_ok = (
            object_button(window, '3.0.2').text().startswith('3.0.2')
            and object_button(window, '▱').text().endswith('座席')
            and window.aim_action is not None
            and window.aim_action.text() == '座席へ向ける'
        )
        print('A10_JAPANESE_DISCOVERY', japanese_ui_ok, flush=True)
        if not japanese_ui_ok:
            return False

        click_action(window, window.draw_room_action, app)
        set_top_fixture_camera(window)
        foreground(window, app)
        for x_m, y_m in L_ROOM_POINTS:
            click_global(room_point_global(window, x_m, y_m), app)
        click_global(room_point_global(window, *L_ROOM_POINTS[0]), app)
        room_ok = wait_until(
            app,
            lambda: (
                window.working.committed_document.room is not None
                and len(room_vertices(window.working.committed_document.room)) == 8
            ),
            1.0,
        )
        print('A10_MOUSE_L_ROOM', room_ok, flush=True)
        if not room_ok:
            return False

        before_template_history = window.working.history_length
        click_object_button(window, '3.0.2', app)
        template_speakers = tuple(
            entity for entity in window.working.committed_document.entities if entity.kind == 'speaker'
        )
        template_ok = (
            len(template_speakers) == 5
            and {entity.speaker_role for entity in template_speakers} == {'FL', 'C', 'FR', 'TFL', 'TFR'}
            and all(entity.aim_xyz is None for entity in template_speakers)
            and window.working.history_length == before_template_history + 1
        )
        print('A10_MOUSE_302_TEMPLATE', template_ok, flush=True)
        if not template_ok:
            return False

        click_object_button(window, '▱', app)
        seat_id = window.selected_id
        seat = None if seat_id is None else window.working.committed_document.entity(seat_id)
        seat_ok = seat is not None and seat.kind == 'seat' and seat.acoustic_reference_offset_m is not None
        print('A10_MOUSE_SEAT', seat_ok, seat_id, flush=True)
        if not seat_ok or seat_id is None:
            return False

        before_aim_history = window.working.history_length
        click_action(window, window.aim_action, app)
        aimed_speakers = tuple(
            entity for entity in window.working.committed_document.entities if entity.kind == 'speaker'
        )
        aim_ok = (
            all(entity.aim_xyz is not None for entity in aimed_speakers)
            and window.working.history_length == before_aim_history + 1
        )
        print('A10_EXPLICIT_SEAT_AIM', aim_ok, flush=True)
        if not aim_ok:
            return False

        click_object_button(window, '▭', app)
        screen_id = window.selected_id
        click_object_button(window, '□', app)
        furniture_id = window.selected_id
        kinds = [entity.kind for entity in window.working.committed_document.entities]
        objects_ok = 'screen' in kinds and 'furniture' in kinds
        print('A10_MOUSE_SCREEN_FURNITURE', objects_ok, flush=True)
        if not objects_ok or screen_id is None or furniture_id is None:
            return False

        fl = next(entity for entity in window.working.committed_document.entities if entity.speaker_role == 'FL')
        click_actor(window, fl.entity_id, app)
        corrected_before = window.working.committed_document.entity(fl.entity_id).position
        before_move_history = window.working.history_length
        moved_ok = drag_selected_x(window, app, scale=1.05)
        corrected = window.working.committed_document.entity(fl.entity_id).position
        correction_ok = (
            moved_ok
            and corrected != corrected_before
            and window.working.history_length == before_move_history + 1
            and window.working.committed_document.entity(fl.entity_id).aim_xyz is not None
        )
        print('A10_MOUSE_DISTANCE_CORRECTION', correction_ok, flush=True)
        if not correction_ok:
            return False

        click_actor(window, fl.entity_id, app)
        before_mistake_history = window.working.history_length
        mistake_ok = drag_selected_x(window, app, scale=1.22)
        mistaken = window.working.committed_document.entity(fl.entity_id).position
        if not mistake_ok or mistaken == corrected or window.working.history_length != before_mistake_history + 1:
            print('A10_MOUSE_MISTAKE_SETUP', False, flush=True)
            return False
        click_action(window, window.undo_action, app)
        undo_ok = window.working.committed_document.entity(fl.entity_id).position == corrected
        print('A10_MOUSE_UNDO_MISTAKE', undo_ok, flush=True)
        if not undo_ok:
            return False

        click_actor(window, seat_id, app)
        before_duplicate_count = len(window.working.committed_document.entities)
        click_action(window, window.duplicate_action, app)
        duplicate_id = window.selected_id
        duplicate = None if duplicate_id is None else window.working.committed_document.entity(duplicate_id)
        duplicate_ok = (
            duplicate is not None
            and duplicate.kind == 'seat'
            and len(window.working.committed_document.entities) == before_duplicate_count + 1
            and duplicate.size_m == seat.size_m
            and duplicate.acoustic_reference_offset_m == seat.acoustic_reference_offset_m
        )
        print('A10_MOUSE_DUPLICATE', duplicate_ok, duplicate_id, flush=True)
        if not duplicate_ok:
            return False

        click_actor(window, furniture_id, app)
        physical_before_hide = window.working.committed_document
        click_action(window, window.hide_action, app)
        hide_ok = (
            furniture_id in window.view_state.hidden_ids
            and window.working.committed_document == physical_before_hide
        )
        print('A10_HIDE_VIEW_ONLY', hide_ok, flush=True)
        if not hide_ok:
            return False

        click_actor(window, screen_id, app)
        physical_before_lock = window.working.committed_document
        click_action(window, window.lock_action, app)
        lock_ok = (
            screen_id in window.view_state.locked_ids
            and window.working.committed_document == physical_before_lock
        )
        print('A10_LOCK_VIEW_ONLY', lock_ok, flush=True)
        if not lock_ok:
            return False

        expected = window.working.committed_document
        expected_roles = {
            entity.entity_id: entity.speaker_role for entity in expected.entities if entity.kind == 'speaker'
        }
        expected_dimensions = {
            entity.entity_id: entity.size_m for entity in expected.entities if entity.size_m is not None
        }
        expected_positions = {entity.entity_id: entity.position for entity in expected.entities}
        expected_references = {
            entity.entity_id: entity.acoustic_reference_offset_m for entity in expected.entities
        }
        click_action(window, window.save_action, app)
        save_ok = wait_until(app, lambda: not window.working.is_dirty, 1.0)
        print('A10_MOUSE_SAVE', save_ok, flush=True)
        if not save_ok:
            return False

        window.close()
        pump(app, 0.15)
        window.deleteLater()
        pump(app, 0.10)

        reopened = TheaterEditorWindow(repository, FIXTURE_ID)
        foreground(reopened, app)
        document = reopened.working.committed_document
        reopen_ok = (
            document == expected
            and {entity.entity_id: entity.speaker_role for entity in document.entities if entity.kind == 'speaker'} == expected_roles
            and {entity.entity_id: entity.size_m for entity in document.entities if entity.size_m is not None} == expected_dimensions
            and {entity.entity_id: entity.position for entity in document.entities} == expected_positions
            and {entity.entity_id: entity.acoustic_reference_offset_m for entity in document.entities} == expected_references
            and furniture_id in reopened.view_state.hidden_ids
            and screen_id in reopened.view_state.locked_ids
        )
        print('A10_REOPEN_EXACT', reopen_ok, flush=True)
        elapsed = time.perf_counter() - started
        print('A10_FIRST_USE_SECONDS', f'{elapsed:.2f}', flush=True)
        print('A10_MISSELECTIONS', 0, flush=True)
        print('A10_GUIDANCE_REQUIRED', 0, flush=True)
        return reopen_ok
    finally:
        if reopened is not None:
            reopened.close()
            pump(app, 0.10)
            reopened.deleteLater()
        if window.isVisible():
            window.close()
            pump(app, 0.10)
            window.deleteLater()
        gc.collect()
        pump(app, 0.08)


def main() -> int:
    parser = argparse.ArgumentParser(description='Run N40 A10 Windows acceptance with real OS mouse input.')
    parser.add_argument('--keep-data', type=Path, default=None)
    args = parser.parse_args()
    app = QApplication.instance() or QApplication([sys.argv[0]])
    if args.keep_data is not None:
        args.keep_data.mkdir(parents=True, exist_ok=True)
        passed = run_a10(app, args.keep_data)
    else:
        with tempfile.TemporaryDirectory(prefix='htdt-a10-', ignore_cleanup_errors=True) as temp:
            passed = run_a10(app, Path(temp))
    print('A10_RESULT', 'PASS' if passed else 'FAIL', flush=True)
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
