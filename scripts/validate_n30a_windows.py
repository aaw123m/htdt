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
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend' / 'src'))

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import RoomVertex, make_empty_scene, room_vertices
from htdt.room_editor import RoomEditorWindow

if sys.platform != 'win32':
    raise SystemExit('This acceptance harness requires Windows.')

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(errors='backslashreplace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(errors='backslashreplace')

user32 = ctypes.windll.user32
MOUSE_LEFTDOWN = 0x0002
MOUSE_LEFTUP = 0x0004

F2_POINTS = (
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


def display_to_global(window: RoomEditorWindow, x: float, y: float) -> QPoint:
    widget = window.viewport.interactor
    dpr = float(widget.devicePixelRatioF())
    _, height = window.viewport.render_window.GetSize()
    return widget.mapToGlobal(QPoint(int(round(x / dpr)), int(round((height - y) / dpr))))


def world_to_display(window: RoomEditorWindow, xyz) -> tuple[float, float]:
    renderer = window.viewport.renderer
    renderer.SetWorldPoint(float(xyz[0]), float(xyz[1]), float(xyz[2]), 1.0)
    renderer.WorldToDisplay()
    x, y, _ = renderer.GetDisplayPoint()
    return float(x), float(y)


def domain_to_global(window: RoomEditorWindow, x_m: float, y_m: float) -> QPoint:
    return display_to_global(window, *world_to_display(window, (x_m, -y_m, 0.0)))


def handle_to_global(window: RoomEditorWindow, vertex: RoomVertex) -> QPoint:
    qt_x, qt_y = window._project_room_to_qt(vertex)
    return window.viewport.interactor.mapToGlobal(QPoint(int(round(qt_x)), int(round(qt_y))))


def midpoint_vertex(start: RoomVertex, end: RoomVertex) -> RoomVertex:
    return RoomVertex(
        vertex_id='acceptance-midpoint',
        x_m=(start.x_m + end.x_m) / 2.0,
        y_m=(start.y_m + end.y_m) / 2.0,
    )


def point_inside_viewport(window: RoomEditorWindow, point: QPoint) -> bool:
    local = window.viewport.interactor.mapFromGlobal(point)
    return bool(window.viewport.interactor.rect().contains(local))


def click(point: QPoint, app: QApplication, *, settle_s: float = 0.04) -> None:
    QCursor.setPos(point)
    pump(app, settle_s)
    user32.mouse_event(MOUSE_LEFTDOWN, 0, 0, 0, 0)
    pump(app, 0.02)
    user32.mouse_event(MOUSE_LEFTUP, 0, 0, 0, 0)
    pump(app, 0.04)


def foreground_window(window: RoomEditorWindow, app: QApplication) -> None:
    window.show()
    window.showNormal()
    window.raise_()
    window.activateWindow()
    window.viewport.interactor.setFocus()
    user32.SetForegroundWindow(int(window.winId()))
    pump(app, 0.25)
    print('A08_WINDOW_ACTIVE', bool(window.isActiveWindow()), flush=True)


def prepare_handle_input(window: RoomEditorWindow, app: QApplication) -> None:
    """Restore the editor as foreground target and normalize OS left-button state."""
    foreground_window(window, app)
    user32.mouse_event(MOUSE_LEFTUP, 0, 0, 0, 0)
    pump(app, 0.05)


def drag(window: RoomEditorWindow, start: QPoint, end: QPoint, app: QApplication) -> None:
    prepare_handle_input(window, app)
    QCursor.setPos(start)
    pump(app, 0.12)
    user32.mouse_event(MOUSE_LEFTDOWN, 0, 0, 0, 0)
    pump(app, 0.08)
    QCursor.setPos(end)
    pump(app, 0.15)
    user32.mouse_event(MOUSE_LEFTUP, 0, 0, 0, 0)
    pump(app, 0.18)


def room_xy(window: RoomEditorWindow) -> tuple[tuple[float, float], ...]:
    room = window.working.committed_document.room
    if room is None:
        return ()
    return tuple((vertex.x_m, vertex.y_m) for vertex in room_vertices(room))


def set_top_fixture_camera(window: RoomEditorWindow) -> None:
    renderer = window.viewport.renderer
    camera = renderer.GetActiveCamera()
    camera.SetFocalPoint(3.0, -2.0, 0.0)
    camera.SetPosition(3.0, -2.0, 10.0)
    camera.SetViewUp(0.0, 1.0, 0.0)
    camera.ParallelProjectionOn()
    camera.SetParallelScale(3.5)
    renderer.ResetCameraClippingRange()
    window.viewport.render()


def fixture_points_are_visible(window: RoomEditorWindow) -> bool:
    visible = True
    for x_m, y_m in F2_POINTS:
        point = domain_to_global(window, x_m, y_m)
        local = window.viewport.interactor.mapFromGlobal(point)
        inside = point_inside_viewport(window, point)
        print('A08_POINT', x_m, y_m, 'LOCAL', local.x(), local.y(), 'INSIDE', inside, flush=True)
        visible = visible and inside
    return visible


def run_a08(app: QApplication, root: Path) -> bool:
    document_id = 'fixture-f2-a08'
    repo = SceneRepository(root / 'scene.sqlite3')
    repo.save(make_empty_scene(document_id), parent_revision_id=None)
    window = RoomEditorWindow(repo, document_id)
    foreground_window(window, app)
    try:
        window.start_room_sketch()
        set_top_fixture_camera(window)
        foreground_window(window, app)
        if not fixture_points_are_visible(window):
            print('A08_FIXTURE_VISIBLE False', flush=True)
            return False

        for x_m, y_m in F2_POINTS:
            click(domain_to_global(window, x_m, y_m), app)
        click(domain_to_global(window, *F2_POINTS[0]), app)
        pump(app, 0.15)

        room = window.working.committed_document.room
        draw_ok = (
            room is not None
            and room.footprint_vertices is not None
            and len(room.footprint_vertices) == 8
            and np.allclose(np.asarray(room_xy(window)), np.asarray(F2_POINTS), atol=0.03)
            and np.allclose(np.asarray(room.bounds_m), np.asarray((0.0, 0.0, 6.0, 4.0)), atol=0.03)
            and window.working.history_length == 1
        )
        print('A08_DRAW_F2', draw_ok, 'HISTORY', window.working.history_length, flush=True)
        if not draw_ok or room is None:
            return False

        set_top_fixture_camera(window)
        prepare_handle_input(window, app)
        before_insert = window.working.history_length
        window.insert_vertex_action.setChecked(True)
        current = room_vertices(room)
        midpoint = midpoint_vertex(current[0], current[1])
        insert_point = handle_to_global(window, midpoint)
        local = window.viewport.interactor.mapFromGlobal(insert_point)
        print('A08_INSERT_TARGET', (midpoint.x_m, midpoint.y_m), 'HIT', window._hit_room_handle(float(local.x()), float(local.y())), flush=True)
        click(insert_point, app, settle_s=0.10)
        inserted_room = window.working.committed_document.room
        inserted_id = window.selected_room_vertex_id
        inserted_vertex = None if inserted_room is None else next(
            (v for v in room_vertices(inserted_room) if v.vertex_id == inserted_id), None
        )
        insert_ok = (
            inserted_room is not None
            and len(room_vertices(inserted_room)) == 9
            and window.working.history_length == before_insert + 1
            and inserted_vertex is not None
        )
        print('A08_INSERT_VERTEX', insert_ok, inserted_id, flush=True)
        if not insert_ok or inserted_vertex is None:
            return False
        window.insert_vertex_action.setChecked(False)

        set_top_fixture_camera(window)
        before_move = window.working.history_length
        drag(window, handle_to_global(window, inserted_vertex), domain_to_global(window, 3.0, 0.5), app)
        moved_room = window.working.committed_document.room
        moved_vertex = None if moved_room is None else next(
            (v for v in room_vertices(moved_room) if v.vertex_id == inserted_id), None
        )
        move_ok = (
            moved_vertex is not None
            and abs(moved_vertex.x_m - 3.0) <= 0.03
            and abs(moved_vertex.y_m - 0.5) <= 0.03
            and window.working.history_length == before_move + 1
        )
        print('A08_MOVE_VERTEX', move_ok, None if moved_vertex is None else (moved_vertex.x_m, moved_vertex.y_m), flush=True)
        if not move_ok:
            return False

        set_top_fixture_camera(window)
        prepare_handle_input(window, app)
        current = room_vertices(window.working.committed_document.room)
        edge_midpoint = midpoint_vertex(current[1], current[2])
        click(handle_to_global(window, edge_midpoint), app, settle_s=0.10)
        edge_index = window.selected_room_edge_index
        if edge_index is None:
            print('A08_EDGE_SELECT False', flush=True)
            return False
        before_dimension = window.working.history_length
        original_length = window.room_edge_length.value()
        window.room_edge_length.setValue(original_length + 0.25)
        window.room_edge_length.editingFinished.emit()
        pump(app, 0.12)
        dimension_ok = window.working.history_length == before_dimension + 1
        print('A08_EDGE_DIMENSION', dimension_ok, 'EDGE', edge_index, flush=True)
        if not dimension_ok:
            return False

        before_height = window.working.history_length
        window.room_height.setValue(2.6)
        window.room_height.editingFinished.emit()
        pump(app, 0.10)
        height_room = window.working.committed_document.room
        height_ok = (
            height_room is not None
            and abs(height_room.height_m - 2.6) <= 1e-9
            and window.working.history_length == before_height + 1
        )
        print('A08_HEIGHT', height_ok, None if height_room is None else height_room.height_m, flush=True)
        if not height_ok:
            return False

        current = room_vertices(window.working.committed_document.room)
        current_inserted = next(v for v in current if v.vertex_id == inserted_id)
        set_top_fixture_camera(window)
        prepare_handle_input(window, app)
        click(handle_to_global(window, current_inserted), app, settle_s=0.10)
        before_delete = window.working.history_length
        window.delete_vertex_action.trigger()
        pump(app, 0.12)
        deleted_room = window.working.committed_document.room
        delete_ok = (
            deleted_room is not None
            and len(room_vertices(deleted_room)) == 8
            and all(v.vertex_id != inserted_id for v in room_vertices(deleted_room))
            and window.working.history_length == before_delete + 1
        )
        print('A08_DELETE_VERTEX', delete_ok, 'HISTORY', window.working.history_length, flush=True)
        if not delete_ok:
            return False

        room_before_invalid = window.working.committed_document.room
        assert room_before_invalid is not None
        second = room_vertices(room_before_invalid)[1]
        before_invalid = window.working.history_length
        set_top_fixture_camera(window)
        drag(window, handle_to_global(window, second), domain_to_global(window, 3.0, 3.0), app)
        invalid_ok = (
            window.working.committed_document.room == room_before_invalid
            and window.working.history_length == before_invalid
        )
        print('A08_INVALID_REJECT', invalid_ok, 'HISTORY', window.working.history_length, flush=True)
        if not invalid_ok:
            return False

        before_undo = window.working.committed_document.room
        undo_ok = window.working.undo()
        redo_ok = window.working.redo()
        exact_redo = window.working.committed_document.room == before_undo
        print('A08_UNDO_REDO', undo_ok and redo_ok and exact_redo, flush=True)
        return bool(undo_ok and redo_ok and exact_redo)
    finally:
        window.close()
        pump(app, 0.15)
        window.deleteLater()
        pump(app, 0.15)
        del window
        gc.collect()
        pump(app, 0.10)


def main() -> int:
    parser = argparse.ArgumentParser(description='Run N30a A08 Windows acceptance with real OS mouse input.')
    parser.add_argument('--keep-data', type=Path, default=None)
    args = parser.parse_args()
    app = QApplication.instance() or QApplication([sys.argv[0]])
    if args.keep_data is not None:
        args.keep_data.mkdir(parents=True, exist_ok=True)
        passed = run_a08(app, args.keep_data)
    else:
        with tempfile.TemporaryDirectory(prefix='htdt-n30a-a08-', ignore_cleanup_errors=True) as temp:
            passed = run_a08(app, Path(temp))
    print('A08_RESULT', 'PASS' if passed else 'FAIL', flush=True)
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
