from __future__ import annotations

import argparse
import ctypes
import gc
from pathlib import Path
import sys
import tempfile
import time

from PySide6.QtCore import QPoint
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend' / 'src'))

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Position3,
    RoomVertex,
    SceneDocument,
    SceneEntity,
    make_empty_scene,
    make_polygon_room,
    room_vertices,
)
from htdt.cad_wall_models import WallConstraintBinding, WallOpening
from htdt.cad_walls import add_constraint_binding, add_opening, make_wall_topology
from htdt.native_cad import CadEditorWindow as WallEditorWindow

if sys.platform != 'win32':
    raise SystemExit('This acceptance harness requires Windows.')

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(errors='backslashreplace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(errors='backslashreplace')

user32 = ctypes.windll.user32
MOUSE_LEFTDOWN = 0x0002
MOUSE_LEFTUP = 0x0004
FIXTURE_ID = 'fixture-f3-a09'


def make_f3_scene() -> SceneDocument:
    points = (
        (0.0, 0.0),
        (6.0, 0.0),
        (6.0, 4.0),
        (4.0, 4.0),
        (4.0, 2.0),
        (2.0, 2.0),
        (2.0, 4.0),
        (0.0, 4.0),
    )
    room = make_polygon_room(
        tuple(
            RoomVertex(vertex_id=f'v{index + 1}', x_m=x_m, y_m=y_m)
            for index, (x_m, y_m) in enumerate(points)
        ),
        height_m=2.4,
    )
    topology = make_wall_topology(room)
    front_wall_id = topology.walls[0].wall_id
    topology = add_opening(
        room,
        topology,
        WallOpening(
            opening_id='door-front',
            wall_id=front_wall_id,
            offset_m=0.5,
            width_m=0.9,
            height_m=2.0,
            kind='door',
        ),
    )
    topology = add_constraint_binding(
        room,
        topology,
        WallConstraintBinding(
            binding_id='clearance-front',
            wall_ids=(front_wall_id,),
            clearance_m=0.35,
        ),
    )
    base = make_empty_scene(FIXTURE_ID)
    candidate = base.model_copy(
        update={
            'schema_version': 3,
            'room': room,
            'wall_topology': topology,
            'entities': (
                SceneEntity(
                    entity_id='seat-left',
                    kind='measurement_point',
                    name='Seat Left',
                    position=Position3(x_m=2.4, y_m=1.2, z_m=1.1),
                ),
                SceneEntity(
                    entity_id='seat-right',
                    kind='measurement_point',
                    name='Seat Right',
                    position=Position3(x_m=3.6, y_m=1.2, z_m=1.1),
                ),
            ),
        }
    )
    return SceneDocument.model_validate(candidate.model_dump(mode='python'))


def pump(app: QApplication, seconds: float = 0.05) -> None:
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        app.processEvents()
        time.sleep(0.002)


def foreground_window(window: WallEditorWindow, app: QApplication) -> None:
    window.show()
    window.showNormal()
    window.raise_()
    window.activateWindow()
    window.viewport.interactor.setFocus()
    user32.SetForegroundWindow(int(window.winId()))
    pump(app, 0.25)


def prepare_pointer(window: WallEditorWindow, app: QApplication) -> None:
    foreground_window(window, app)
    user32.mouse_event(MOUSE_LEFTUP, 0, 0, 0, 0)
    pump(app, 0.05)


def qt_global(window: WallEditorWindow, x_m: float, y_m: float) -> QPoint:
    qt_x, qt_y = window._project_xy_to_qt(x_m, y_m)
    return window.viewport.interactor.mapToGlobal(QPoint(int(round(qt_x)), int(round(qt_y))))


def click(window: WallEditorWindow, point: QPoint, app: QApplication) -> None:
    prepare_pointer(window, app)
    QCursor.setPos(point)
    pump(app, 0.12)
    user32.mouse_event(MOUSE_LEFTDOWN, 0, 0, 0, 0)
    pump(app, 0.06)
    user32.mouse_event(MOUSE_LEFTUP, 0, 0, 0, 0)
    pump(app, 0.12)


def drag(window: WallEditorWindow, start: QPoint, end: QPoint, app: QApplication) -> None:
    prepare_pointer(window, app)
    QCursor.setPos(start)
    pump(app, 0.12)
    user32.mouse_event(MOUSE_LEFTDOWN, 0, 0, 0, 0)
    pump(app, 0.10)
    QCursor.setPos(end)
    pump(app, 0.20)
    user32.mouse_event(MOUSE_LEFTUP, 0, 0, 0, 0)
    pump(app, 0.20)


def set_top_fixture_camera(window: WallEditorWindow) -> None:
    renderer = window.viewport.renderer
    camera = renderer.GetActiveCamera()
    camera.SetFocalPoint(3.0, -2.0, 0.0)
    camera.SetPosition(3.0, -2.0, 10.0)
    camera.SetViewUp(0.0, 1.0, 0.0)
    camera.ParallelProjectionOn()
    camera.SetParallelScale(3.6)
    renderer.ResetCameraClippingRange()
    window.viewport.render()


def wall_midpoint(window: WallEditorWindow, wall_id: str) -> tuple[float, float]:
    room = window.working.committed_document.room
    topology = window.working.committed_document.wall_topology
    assert room is not None and topology is not None
    wall = next(wall for wall in topology.walls if wall.wall_id == wall_id)
    vertices = {vertex.vertex_id: vertex for vertex in room_vertices(room)}
    start = vertices[wall.from_vertex_id]
    end = vertices[wall.to_vertex_id]
    return ((start.x_m + end.x_m) / 2.0, (start.y_m + end.y_m) / 2.0)


def select_wall(window: WallEditorWindow, wall_id: str, app: QApplication) -> bool:
    set_top_fixture_camera(window)
    midpoint = wall_midpoint(window, wall_id)
    click(window, qt_global(window, *midpoint), app)
    return window.selected_wall_id == wall_id


def run_a09(app: QApplication, root: Path) -> bool:
    repository = SceneRepository(root / 'scene.sqlite3')
    repository.save(make_f3_scene(), parent_revision_id=None)
    window = WallEditorWindow(repository, FIXTURE_ID)
    foreground_window(window, app)
    try:
        japanese_ui_ok = (
            window.start_wall_action.text() == '壁を編集'
            and window.split_wall_action.text() == '壁を中央で分割'
            and window.add_clearance_action.text() == 'クリアランス参照を追加'
        )
        print('A09_JAPANESE_UI', japanese_ui_ok, flush=True)
        if not japanese_ui_ok:
            return False

        document = window.working.committed_document
        topology = document.wall_topology
        fixture_ok = (
            document.room is not None
            and len(room_vertices(document.room)) == 8
            and topology is not None
            and len(topology.walls) == 8
            and len(topology.openings) == 1
            and len(topology.constraint_bindings) == 1
            and len(document.entities) == 2
            and all(entity.kind == 'measurement_point' for entity in document.entities)
            and window.working.history_length == 0
        )
        print('A09_F3_FIXTURE', fixture_ok, flush=True)
        if not fixture_ok or topology is None:
            return False

        front_wall_id = 'wall:v1->v2'
        opening_id = 'door-front'
        binding_id = 'clearance-front'
        window.start_wall_edit()
        if not select_wall(window, front_wall_id, app):
            print('A09_MOUSE_SELECT', False, window.selected_wall_id, flush=True)
            return False
        print('A09_MOUSE_SELECT', True, front_wall_id, flush=True)

        before_move = window.working.history_length
        drag(
            window,
            qt_global(window, 3.0, 0.0),
            qt_global(window, 3.0, -0.40),
            app,
        )
        room = window.working.committed_document.room
        topology = window.working.committed_document.wall_topology
        vertices = {} if room is None else {vertex.vertex_id: vertex for vertex in room_vertices(room)}
        moved_opening = None if topology is None else next(
            (opening for opening in topology.openings if opening.opening_id == opening_id), None
        )
        moved_binding = None if topology is None else next(
            (binding for binding in topology.constraint_bindings if binding.binding_id == binding_id), None
        )
        move_ok = (
            room is not None
            and topology is not None
            and abs(vertices['v1'].y_m + 0.40) <= 0.04
            and abs(vertices['v2'].y_m + 0.40) <= 0.04
            and moved_opening is not None
            and moved_opening.wall_id == front_wall_id
            and moved_binding is not None
            and moved_binding.wall_ids == (front_wall_id,)
            and window.working.history_length == before_move + 1
        )
        print('A09_MOUSE_MOVE_REFERENCES', move_ok, flush=True)
        if not move_ok:
            return False

        before_split = window.working.history_length
        window.split_wall_action.trigger()
        pump(app, 0.15)
        split_topology = window.working.committed_document.wall_topology
        first_child = window.selected_wall_id
        split_opening = None if split_topology is None else next(
            (opening for opening in split_topology.openings if opening.opening_id == opening_id), None
        )
        split_binding = None if split_topology is None else next(
            (binding for binding in split_topology.constraint_bindings if binding.binding_id == binding_id), None
        )
        split_ok = (
            split_topology is not None
            and len(split_topology.walls) == 9
            and first_child is not None
            and split_opening is not None
            and split_opening.wall_id == first_child
            and split_binding is not None
            and len(split_binding.wall_ids) == 2
            and split_binding.clearance_m == 0.35
            and window.working.history_length == before_split + 1
        )
        print('A09_SPLIT_REFERENCE', split_ok, first_child, flush=True)
        if not split_ok or first_child is None:
            return False

        before_merge = window.working.history_length
        window.merge_wall_action.trigger()
        pump(app, 0.15)
        merged_topology = window.working.committed_document.wall_topology
        merged_id = window.selected_wall_id
        merged_opening = None if merged_topology is None else next(
            (opening for opening in merged_topology.openings if opening.opening_id == opening_id), None
        )
        merged_binding = None if merged_topology is None else next(
            (binding for binding in merged_topology.constraint_bindings if binding.binding_id == binding_id), None
        )
        merge_ok = (
            merged_topology is not None
            and len(merged_topology.walls) == 8
            and merged_id is not None
            and merged_opening is not None
            and merged_opening.wall_id == merged_id
            and merged_binding is not None
            and merged_binding.wall_ids == (merged_id,)
            and window.working.history_length == before_merge + 1
        )
        print('A09_MERGE_REFERENCE', merge_ok, merged_id, flush=True)
        if not merge_ok or merged_id is None:
            return False

        before_reject = window.working.history_length
        window.delete_wall_action.trigger()
        pump(app, 0.12)
        after_reject = window.working.committed_document.wall_topology
        reject_ok = (
            after_reject is not None
            and len(after_reject.walls) == 8
            and window.working.history_length == before_reject
            and any(opening.opening_id == opening_id for opening in after_reject.openings)
            and any(binding.binding_id == binding_id for binding in after_reject.constraint_bindings)
        )
        print('A09_REFERENCED_DELETE_REJECT', reject_ok, flush=True)
        if not reject_ok:
            return False

        ambiguous_wall_id = 'wall:v3->v4'
        if not select_wall(window, ambiguous_wall_id, app):
            print('A09_AMBIGUOUS_SELECT', False, window.selected_wall_id, flush=True)
            return False
        before_extra_opening = window.working.history_length
        window.add_opening_action.trigger()
        pump(app, 0.12)
        if window.working.history_length != before_extra_opening + 1:
            print('A09_AMBIGUOUS_OPENING_SETUP', False, flush=True)
            return False
        before_ambiguous_split = window.working.history_length
        wall_count_before = len(window.working.committed_document.wall_topology.walls)
        window.split_wall_action.trigger()
        pump(app, 0.12)
        ambiguous_topology = window.working.committed_document.wall_topology
        ambiguous_ok = (
            ambiguous_topology is not None
            and len(ambiguous_topology.walls) == wall_count_before
            and window.working.history_length == before_ambiguous_split
        )
        print('A09_AMBIGUOUS_SPLIT_REJECT', ambiguous_ok, flush=True)
        if not ambiguous_ok:
            return False

        delete_wall_id = 'wall:v7->v8'
        if not select_wall(window, delete_wall_id, app):
            print('A09_SELECT_DELETE_WALL', False, window.selected_wall_id, flush=True)
            return False
        before_delete = window.working.history_length
        window.delete_wall_action.trigger()
        pump(app, 0.15)
        deleted = window.working.committed_document
        deleted_topology = deleted.wall_topology
        delete_ok = (
            deleted.room is not None
            and deleted_topology is not None
            and len(room_vertices(deleted.room)) == 7
            and len(deleted_topology.walls) == 7
            and window.working.history_length == before_delete + 1
            and any(opening.opening_id == opening_id and opening.wall_id == merged_id for opening in deleted_topology.openings)
            and any(binding.binding_id == binding_id and binding.wall_ids == (merged_id,) for binding in deleted_topology.constraint_bindings)
        )
        print('A09_DELETE_UNREFERENCED', delete_ok, flush=True)
        if not delete_ok:
            return False

        committed = window.working.committed_document
        undo_ok = window.working.undo()
        redo_ok = window.working.redo()
        redo_exact = window.working.committed_document == committed
        print('A09_UNDO_REDO', undo_ok and redo_ok and redo_exact, flush=True)
        return bool(undo_ok and redo_ok and redo_exact)
    finally:
        window.close()
        pump(app, 0.15)
        window.deleteLater()
        pump(app, 0.15)
        del window
        gc.collect()
        pump(app, 0.10)


def main() -> int:
    parser = argparse.ArgumentParser(description='Run N30b A09 Windows acceptance with real OS mouse input.')
    parser.add_argument('--keep-data', type=Path, default=None)
    args = parser.parse_args()

    app = QApplication.instance() or QApplication([sys.argv[0]])
    if args.keep_data is not None:
        args.keep_data.mkdir(parents=True, exist_ok=True)
        ok = run_a09(app, args.keep_data)
    else:
        with tempfile.TemporaryDirectory(prefix='htdt-a09-') as temp:
            ok = run_a09(app, Path(temp))
    print('A09_RESULT', 'PASS' if ok else 'FAIL', flush=True)
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
