from __future__ import annotations

import argparse
import ctypes
from pathlib import Path
import sys
import tempfile
import time

import numpy as np
import pyvista as pv
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication, QWidget

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend' / 'src'))

from htdt.cad_gizmo import TranslationWidget3D
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, SceneEntity, Size3, make_f1_scene
from htdt.cad_snap import SnapSelector, generate_snap_candidates
from htdt.native_editor import NativeEditorWindow

if sys.platform != 'win32':
    raise SystemExit('This acceptance harness requires Windows.')

user32 = ctypes.windll.user32
MOUSE_LEFTDOWN = 0x0002
MOUSE_LEFTUP = 0x0004
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11


def pump(app: QApplication, seconds: float = 0.04) -> None:
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        app.processEvents()
        time.sleep(0.002)


def p95(values: list[float]) -> float:
    return float(np.percentile(np.asarray(values, dtype=float), 95))



class ClickLatencyProbe(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.press_serial = 0
        self.paint_serial = 0
        self.setFixedSize(240, 160)
        self.setWindowTitle('HTDT input baseline')

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.press_serial += 1
            self.update()
        super().mousePressEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        self.paint_serial += 1
        super().paintEvent(event)


def measure_input_baseline(app: QApplication, samples: int = 40) -> list[float]:
    probe = ClickLatencyProbe()
    probe.show()
    probe.raise_()
    probe.activateWindow()
    pump(app, 0.16)
    point = probe.mapToGlobal(probe.rect().center())
    values: list[float] = []
    try:
        for _ in range(samples):
            QCursor.setPos(point)
            pump(app, 0.01)
            before_press = probe.press_serial
            before_paint = probe.paint_serial
            start = time.perf_counter()
            user32.mouse_event(MOUSE_LEFTDOWN, 0, 0, 0, 0)
            user32.mouse_event(MOUSE_LEFTUP, 0, 0, 0, 0)
            if not wait_until(app, lambda: probe.press_serial > before_press, 0.5):
                raise AssertionError('input baseline press timeout')
            if not wait_until(app, lambda: probe.paint_serial > before_paint, 0.5):
                raise AssertionError('input baseline paint timeout')
            values.append((time.perf_counter() - start) * 1000.0)
        return values
    finally:
        probe.close()
        pump(app, 0.10)

def display_to_global(window: NativeEditorWindow, x: float, y: float) -> QPoint:
    widget = window.viewport.interactor
    dpr = float(widget.devicePixelRatioF())
    _, height = window.viewport.render_window.GetSize()
    return widget.mapToGlobal(QPoint(int(round(x / dpr)), int(round((height - y) / dpr))))


def world_to_display(window: NativeEditorWindow, xyz) -> tuple[float, float]:
    renderer = window.viewport.renderer
    renderer.SetWorldPoint(float(xyz[0]), float(xyz[1]), float(xyz[2]), 1.0)
    renderer.WorldToDisplay()
    x, y, _ = renderer.GetDisplayPoint()
    return float(x), float(y)


def world_to_global(window: NativeEditorWindow, xyz) -> QPoint:
    return display_to_global(window, *world_to_display(window, xyz))


def click(point: QPoint, *, ctrl: bool = False) -> None:
    QCursor.setPos(point)
    if ctrl:
        user32.keybd_event(VK_CONTROL, 0, 0, 0)
    try:
        user32.mouse_event(MOUSE_LEFTDOWN, 0, 0, 0, 0)
        user32.mouse_event(MOUSE_LEFTUP, 0, 0, 0, 0)
    finally:
        if ctrl:
            user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


def wait_until(app: QApplication, predicate, seconds: float = 1.0) -> bool:
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.002)
    return bool(predicate())


def make_window(app: QApplication, document, root: Path) -> NativeEditorWindow:
    repo = SceneRepository(root / 'scene.sqlite3')
    repo.save(document, parent_revision_id=None)
    window = NativeEditorWindow(repo)
    window.show()
    pump(app, 0.25)
    return window


def run_a07(app: QApplication, root: Path) -> bool:
    window = make_window(app, make_f1_scene(), root / 'a07')
    try:
        window._top()
        pump(app, 0.15)

        click(world_to_global(window, np.asarray(window.actors['speaker-fl'].center, dtype=float)))
        pump(app, 0.08)
        click(world_to_global(window, np.asarray(window.actors['speaker-c'].center, dtype=float)), ctrl=True)
        pump(app, 0.12)
        multi_ok = set(window.view_state.selection) == {'speaker-fl', 'speaker-c'}
        print('A07_CTRL_MULTISELECT', multi_ok, window.view_state.selection, flush=True)
        if not multi_ok:
            return False

        candidates = generate_snap_candidates(
            window.working.committed_document,
            exclude_ids=set(window.view_state.selection),
            axis='x',
            probe=Position3(x_m=4.0, y_m=0.0, z_m=1.0),
        )
        vertex = next(c for c in candidates if c.entity_id == 'speaker-fr' and c.kind == 'vertex')
        selector = SnapSelector()
        before = selector.select(candidates, vertex.screen_anchor, window._project_domain_to_dip)
        window.viewport.camera.zoom(1.35)
        window.viewport.render()
        after = selector.select(candidates, vertex.screen_anchor, window._project_domain_to_dip)
        snap_ok = (
            before is not None
            and after is not None
            and before.candidate.kind == 'vertex'
            and after.candidate.stable_id == before.candidate.stable_id
            and before.distance_dip <= 1e-6
            and after.distance_dip <= 1e-6
        )
        print('A07_SNAP_ZOOM', snap_ok, None if before is None else before.candidate.stable_id, flush=True)
        if not snap_ok:
            return False

        window._object_snap_toggled(False)
        window._grid_snap_toggled(False)
        window._set_transform_mode('move')
        window._top()
        pump(app, 0.18)
        assert isinstance(window.gizmo, TranslationWidget3D)
        gizmo = window.gizmo
        before_fl = window.working.committed_document.entity('speaker-fl')
        before_c = window.working.committed_document.entity('speaker-c')
        before_history = window.working.history_length
        rel_before = np.array((
            before_c.position.x_m - before_fl.position.x_m,
            before_c.position.y_m - before_fl.position.y_m,
            before_c.position.z_m - before_fl.position.z_m,
        ))
        start_world = gizmo.origin + gizmo.axes[0] * gizmo.actor_length * 0.75 * 0.45
        end_world = gizmo.origin + gizmo.axes[0] * gizmo.actor_length * 0.75 * 1.05
        start = world_to_global(window, start_world)
        end = world_to_global(window, end_world)
        QCursor.setPos(start)
        pump(app, 0.03)
        user32.mouse_event(MOUSE_LEFTDOWN, 0, 0, 0, 0)
        pump(app, 0.04)
        if not isinstance(window.gizmo, TranslationWidget3D) or not window.gizmo.pressing:
            print('A07_GROUP_MOVE_PRESS False', flush=True)
            user32.mouse_event(MOUSE_LEFTUP, 0, 0, 0, 0)
            return False
        QCursor.setPos(end)
        pump(app, 0.10)
        user32.mouse_event(MOUSE_LEFTUP, 0, 0, 0, 0)
        released = wait_until(app, lambda: not window.working.has_preview, 1.0)
        after_fl = window.working.committed_document.entity('speaker-fl')
        after_c = window.working.committed_document.entity('speaker-c')
        rel_after = np.array((
            after_c.position.x_m - after_fl.position.x_m,
            after_c.position.y_m - after_fl.position.y_m,
            after_c.position.z_m - after_fl.position.z_m,
        ))
        moved = after_fl.position != before_fl.position and after_c.position != before_c.position
        group_ok = (
            released
            and moved
            and window.working.history_length == before_history + 1
            and np.allclose(rel_before, rel_after, atol=1e-9)
            and after_fl.aim_xyz is None
        )
        print('A07_GROUP_MOVE', group_ok, 'RELEASED', released, 'HISTORY', window.working.history_length, flush=True)
        if not group_ok:
            return False
        window.undo()
        restored_fl = window.working.committed_document.entity('speaker-fl')
        restored_c = window.working.committed_document.entity('speaker-c')
        undo_ok = restored_fl.position == before_fl.position and restored_c.position == before_c.position
        print('A07_GROUP_UNDO', undo_ok, flush=True)
        return undo_ok
    finally:
        window.close()
        pump(app, 0.12)


def make_f4_document():
    base = make_f1_scene()
    extras = []
    for i in range(50):
        col, row = i % 10, i // 10
        extras.append(SceneEntity(
            entity_id=f'perf-{i:02d}',
            kind='furniture',
            name=f'Perf {i:02d}',
            position=Position3(x_m=0.35 + col * 0.56, y_m=0.35 + row * 0.62, z_m=0.18),
            size_m=Size3(x_m=0.12, y_m=0.12, z_m=0.12),
        ))
    return base.model_copy(update={'entities': tuple(base.entities) + tuple(extras)})


def add_markers(window: NativeEditorWindow) -> None:
    xs = np.linspace(0.2, 5.8, 20)
    ys = np.linspace(-3.8, -0.2, 10)
    zs = np.linspace(0.15, 2.25, 5)
    points = np.array([(x, y, z) for z in zs for y in ys for x in xs], dtype=float)
    assert len(points) == 1000
    window.viewport.add_mesh(
        pv.PolyData(points), style='points', point_size=4, pickable=False,
        name='perf-analysis-markers', render=False,
    )
    window.viewport.render()


def actual_pick(
    app: QApplication,
    window: NativeEditorWindow,
    entity_id: str,
    feedback_serial: list[int],
) -> float:
    point = world_to_global(window, np.asarray(window.actors[entity_id].center, dtype=float))
    QCursor.setPos(point)
    pump(app, 0.01)
    before_feedback = feedback_serial[0]
    start = time.perf_counter()
    user32.mouse_event(MOUSE_LEFTDOWN, 0, 0, 0, 0)
    user32.mouse_event(MOUSE_LEFTUP, 0, 0, 0, 0)
    if not wait_until(
        app,
        lambda: window.selected_id == entity_id and feedback_serial[0] > before_feedback,
        0.75,
    ):
        raise AssertionError(f'viewport pick/render timeout: {entity_id}')
    return (time.perf_counter() - start) * 1000.0

def drag_path(window: NativeEditorWindow) -> tuple[QPoint, list[QPoint]]:
    assert isinstance(window.gizmo, TranslationWidget3D)
    gizmo = window.gizmo
    start_world = gizmo.origin + gizmo.axes[0] * gizmo.actor_length * 0.75 * 0.45
    end_world = gizmo.origin + gizmo.axes[0] * gizmo.actor_length * 0.75 * 1.25
    start = world_to_global(window, start_world)
    end = world_to_global(window, end_world)
    path = [QPoint(
        int(round(start.x() + (end.x() - start.x()) * i / 16.0)),
        int(round(start.y() + (end.y() - start.y()) * i / 16.0)),
    ) for i in range(1, 17)]
    path += list(reversed(path[:-1]))
    return start, path


def measure_drag(app: QApplication, window: NativeEditorWindow, seconds: float) -> list[float]:
    window._select('perf-00')
    pump(app, 0.10)
    window._set_transform_mode('move')
    window._object_snap_toggled(True)
    window._grid_snap_toggled(False)
    window._top()
    pump(app, 0.16)
    start, path = drag_path(window)
    QCursor.setPos(start)
    pump(app, 0.03)
    user32.mouse_event(MOUSE_LEFTDOWN, 0, 0, 0, 0)
    pump(app, 0.04)
    if not isinstance(window.gizmo, TranslationWidget3D) or not window.gizmo.pressing:
        user32.mouse_event(MOUSE_LEFTUP, 0, 0, 0, 0)
        raise AssertionError('F4 drag handle press failed')
    samples: list[float] = []
    index = 0
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        start_time = time.perf_counter()
        QCursor.setPos(path[index % len(path)])
        index += 1
        app.processEvents()
        samples.append((time.perf_counter() - start_time) * 1000.0)
    user32.mouse_event(MOUSE_LEFTUP, 0, 0, 0, 0)
    if not wait_until(app, lambda: not window.working.has_preview, 1.0):
        raise AssertionError('F4 drag preview remained after release')
    return samples


def find_blank_point(window: NativeEditorWindow) -> QPoint:
    widget = window.viewport.interactor
    dpr = float(widget.devicePixelRatioF())
    _, render_h = window.viewport.render_window.GetSize()
    candidates = (
        QPoint(int(widget.width() * 0.82), int(widget.height() * 0.20)),
        QPoint(int(widget.width() * 0.75), int(widget.height() * 0.75)),
        QPoint(int(widget.width() * 0.55), int(widget.height() * 0.12)),
    )
    for local in candidates:
        x = int(round(local.x() * dpr))
        y = int(round(render_h - local.y() * dpr))
        window.scene_picker.Pick(x, y, 0, window.viewport.renderer)
        if window.scene_picker.GetActor() is None:
            return widget.mapToGlobal(local)
    raise AssertionError('no blank orbit start point found')


def measure_orbit(app: QApplication, window: NativeEditorWindow, seconds: float) -> tuple[list[float], bool]:
    window._select(None)
    pump(app, 0.10)
    start = find_blank_point(window)
    camera_before = np.asarray(window.viewport.camera.position, dtype=float)
    path = [QPoint(start.x() + dx, start.y() + dy) for dx, dy in (
        (12, 0), (20, 8), (20, 18), (10, 24), (0, 18), (-8, 8), (0, 0)
    )]
    QCursor.setPos(start)
    user32.mouse_event(MOUSE_LEFTDOWN, 0, 0, 0, 0)
    pump(app, 0.03)
    samples: list[float] = []
    index = 0
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        start_time = time.perf_counter()
        QCursor.setPos(path[index % len(path)])
        index += 1
        app.processEvents()
        samples.append((time.perf_counter() - start_time) * 1000.0)
    user32.mouse_event(MOUSE_LEFTUP, 0, 0, 0, 0)
    pump(app, 0.08)
    moved = not np.allclose(camera_before, np.asarray(window.viewport.camera.position, dtype=float))
    return samples, moved


def run_f4(app: QApplication, root: Path, seconds: float) -> bool:
    window = make_window(app, make_f4_document(), root / 'f4')
    try:
        add_markers(window)
        window._top()
        pump(app, 0.15)
        assert len(window.working.committed_document.entities) == 55
        feedback_serial = [0]
        window.gizmo_rebuild_timer.timeout.connect(
            lambda: feedback_serial.__setitem__(0, feedback_serial[0] + 1)
        )
        ids = [f'perf-{i:02d}' for i in range(20)] * 2
        picks = [actual_pick(app, window, entity_id, feedback_serial) for entity_id in ids]
        pick_p95 = p95(picks)
        print('F4_PICK', 'N', len(picks), 'P95_MS', round(pick_p95, 2), 'MAX_MS', round(max(picks), 2), flush=True)

        drag = measure_drag(app, window, seconds)
        drag_p95 = p95(drag)
        drag_stalls = sum(value > 100.0 for value in drag)
        print('F4_DRAG', 'N', len(drag), 'P95_MS', round(drag_p95, 2), 'MAX_MS', round(max(drag), 2), 'GT100', drag_stalls, flush=True)

        orbit, camera_moved = measure_orbit(app, window, seconds)
        orbit_p95 = p95(orbit)
        orbit_stalls = sum(value > 100.0 for value in orbit)
        print('F4_ORBIT', 'N', len(orbit), 'P95_MS', round(orbit_p95, 2), 'MAX_MS', round(max(orbit), 2), 'GT100', orbit_stalls, 'CAMERA_MOVED', camera_moved, flush=True)

        pick_ok = pick_p95 <= 100.0
        drag_ok = drag_p95 <= 33.0 and drag_stalls == 0
        orbit_ok = orbit_p95 <= 33.0 and orbit_stalls == 0 and camera_moved
        print('F4_TARGETS', 'PICK_OK', pick_ok, 'DRAG_OK', drag_ok, 'ORBIT_OK', orbit_ok, flush=True)
        return pick_ok and drag_ok and orbit_ok
    finally:
        window.close()
        pump(app, 0.15)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--duration', type=float, default=30.0)
    args = parser.parse_args()
    app = QApplication.instance() or QApplication([])
    print('N20B_ENV', 'DPR', app.primaryScreen().devicePixelRatio(), 'SCREEN', app.primaryScreen().size().width(), app.primaryScreen().size().height(), flush=True)
    baseline = measure_input_baseline(app)
    print(
        'INPUT_BASELINE', 'N', len(baseline), 'P95_MS', round(p95(baseline), 2),
        'MAX_MS', round(max(baseline), 2), flush=True,
    )
    with tempfile.TemporaryDirectory(prefix='htdt-n20b-', ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        a07_ok = run_a07(app, root)
        print('A07_PASS', a07_ok, flush=True)
        if not a07_ok:
            return 2
        f4_ok = run_f4(app, root, args.duration)
        print('N20B_WINDOWS_ACCEPTANCE_PASS', f4_ok, flush=True)
        return 0 if f4_ok else 3


if __name__ == '__main__':
    raise SystemExit(main())
