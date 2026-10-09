"""Issue #979 regression coverage: cursor-following snap HUD + numeric
delta input for the move/rotate gizmo.

Viewport-level tests drive the real ``RoomViewport3D.render_snap_feedback``
(with a recording plotter, so no GL is needed) to pin the cursor-side
``_SnapHud`` contract: it anchors at ``screen_position`` in interactor DIP
space, clamps inside the frame without covering the anchor, and the
renderer lower-left label stays as the fallback path.

Controller-level tests drive ``RoomEntityTransformController`` over the
real ``RoomWorkspace`` + a HUD-recording viewport double to pin the #979
interaction contract: typed deltas preview/commit through the same
working-document port as drags (one undo step, exact metres), Esc cancels
without touching SceneRevision, the HUD reports axis-constraint vs grid vs
object snap (and disabled reasons) in the display policy unit, and no edit
shortcut can misfire while numeric input owns the gesture.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QGuiApplication, QKeyEvent
from PySide6.QtWidgets import QApplication

from htdt.cad_display_units import display_length_policy
from htdt.cad_scene import (
    F1_DOCUMENT_ID,
    make_f1_scene,
    quaternion_to_euler_deg,
    rotate_orientation_world,
)
from htdt.cad_input import CadAxis
from htdt.cad_repository import SceneRepository
from htdt.room_transform_input import RoomEntityTransformController
from htdt.room_viewport import RoomViewport3D
from htdt.room_workspace import RoomWorkspace

from test_room_cadux import FakeRoomViewport


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _f1_repository(tmp_path) -> SceneRepository:
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(make_f1_scene(), parent_revision_id=None)
    return repository


class _RecordingPlotter:
    """Captures ``add_text``/``remove_actor``/``render`` — enough of the
    plotter surface for ``render_snap_feedback`` without GL."""

    def __init__(self) -> None:
        self.texts: list[tuple[str, dict]] = []
        self.removed: list[str] = []
        self.renders = 0

    def remove_actor(self, name, render=False, **_kwargs) -> None:
        self.removed.append(str(name))

    def add_text(self, text, **kwargs) -> None:
        self.texts.append((str(text), dict(kwargs)))

    def render(self) -> None:
        self.renders += 1

    def close(self) -> None:
        pass


class _FakeCamera:
    def GetPosition(self):
        return (0.0, 0.0, 10.0)

    def GetFocalPoint(self):
        return (0.0, 0.0, 0.0)

    def GetViewUp(self):
        return (0.0, 1.0, 0.0)

    def GetParallelProjection(self):
        return 1

    def GetParallelScale(self):
        return 5.0

    def GetViewAngle(self):
        return 30.0


class _FakeRenderWindow:
    def GetSize(self):
        return (800, 600)


class HudViewport(FakeRoomViewport):
    """Viewport double recording the #979 snap-feedback call shape."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.snap_feedback: list[tuple[object, object, tuple[str, ...]]] = []
        self.plotter.camera = _FakeCamera()
        self.plotter.render_window = _FakeRenderWindow()
        # Deterministic DIP projection: every world point lands at the same
        # interactor coordinate, so object snap deterministically acquires
        # (distance 0 px) unless the test disables it.
        self.projection_point = QPointF(400.0, 300.0)

    def render_snap_feedback(self, label, *, screen_position=None, hud_lines=None) -> None:
        self.snap_feedback.append((label, screen_position, tuple(hud_lines or ())))

    def world_to_widget_position(self, _position):
        return QPointF(self.projection_point)

    def last_feedback(self):
        return self.snap_feedback[-1] if self.snap_feedback else (None, None, ())


def _workspace(tmp_path):
    app = _app()
    repository = _f1_repository(tmp_path)
    workspace = RoomWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: HudViewport(parent),
    )
    return app, workspace, repository


def _key(key, modifiers=Qt.KeyboardModifier.NoModifier, text='') -> QKeyEvent:
    return QKeyEvent(QEvent.Type.KeyPress, key, modifiers, text)


_DIGIT_KEYS = {
    '0': Qt.Key.Key_0, '1': Qt.Key.Key_1, '2': Qt.Key.Key_2, '3': Qt.Key.Key_3,
    '4': Qt.Key.Key_4, '5': Qt.Key.Key_5, '6': Qt.Key.Key_6, '7': Qt.Key.Key_7,
    '8': Qt.Key.Key_8, '9': Qt.Key.Key_9,
    '.': Qt.Key.Key_Period, '-': Qt.Key.Key_Minus,
}


def _type(controller: RoomEntityTransformController, text: str) -> None:
    for char in text:
        key = _DIGIT_KEYS[char]
        assert controller._key_press(_key(key, text=char)) is True


def _hud_lines(viewport: HudViewport) -> tuple[str, ...]:
    return viewport.last_feedback()[2]


def _hud_text(viewport: HudViewport) -> str:
    return '\n'.join(_hud_lines(viewport))


def _hud_anchor(viewport: HudViewport):
    return viewport.last_feedback()[1]


def _transform(workspace) -> RoomEntityTransformController:
    transform = RoomEntityTransformController(workspace, workspace.viewport)
    workspace.attach_transform_input(transform)
    return transform


def _teardown(app, workspace, transform=None) -> None:
    if transform is not None:
        transform.dispose()
    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def _entity(workspace, entity_id: str):
    return workspace.controller.committed_document.entity(entity_id)


# -- viewport: cursor-side HUD vs lower-left fallback ---------------------------

def test_hud_anchors_at_screen_position_not_lower_left() -> None:
    app = _app()
    viewport = RoomViewport3D()
    viewport.plotter = _RecordingPlotter()
    viewport.interactor.resize(800, 600)

    viewport.render_snap_feedback(
        'スナップ: vertex',
        screen_position=(50.0, 60.0),
        hud_lines=('頂点 · Front Left (speaker-fl) · X=1.350 m · 6 px', 'ΔX +0.125 m'),
    )
    hud = viewport.snap_hud
    assert hud is not None, 'cursor-side HUD must be shown for a positioned feedback'
    assert 'Front Left' in hud.label.text()
    assert 'ΔX +0.125 m' in hud.label.text()
    # The renderer lower-left label must NOT be used while the HUD carries it.
    assert viewport.plotter.texts == []
    anchor = viewport.snap_hud_anchor
    assert anchor is not None
    # Default placement is right+below the anchor (18, 14 offsets).
    assert anchor.x() == pytest.approx(68.0)
    assert anchor.y() == pytest.approx(74.0)

    # The anchor actually tracks the passed position (not pinned anywhere).
    viewport.render_snap_feedback('x', screen_position=(20.0, 30.0), hud_lines=('b',))
    assert viewport.snap_hud_anchor.x() == pytest.approx(38.0)
    assert viewport.snap_hud_anchor.y() == pytest.approx(44.0)

    viewport.close()
    viewport.deleteLater()
    app.processEvents()


def test_hud_clamps_and_flips_at_edges() -> None:
    app = _app()
    viewport = RoomViewport3D()
    viewport.plotter = _RecordingPlotter()
    viewport.interactor.resize(300, 200)

    lines = ('snap line one', 'snap line two', 'ΔX +0.125 m')
    viewport.render_snap_feedback('x', screen_position=(280.0, 170.0), hud_lines=lines)
    anchor = viewport.snap_hud_anchor
    assert anchor is not None
    hud = viewport.snap_hud
    assert hud is not None
    # Near the right/bottom edge the HUD flips to the left/above side of the
    # cursor so it never covers the snap target under the pointer.
    assert anchor.x() < 280.0
    assert anchor.y() < 170.0
    # And it always stays fully inside the interactor (4 px margin).
    assert anchor.x() >= 4.0
    assert anchor.y() >= 4.0
    assert anchor.x() + hud.width() <= 300.0
    assert anchor.y() + hud.height() <= 200.0

    viewport.close()
    viewport.deleteLater()
    app.processEvents()


def test_lower_left_label_remains_fallback_without_position() -> None:
    app = _app()
    viewport = RoomViewport3D()
    viewport.plotter = _RecordingPlotter()

    viewport.render_snap_feedback('グリッド 0.050 m')
    assert viewport.snap_hud is None
    assert len(viewport.plotter.texts) == 1
    text, kwargs = viewport.plotter.texts[0]
    assert text == 'グリッド 0.050 m'
    assert kwargs['name'] == 'snap-feedback-label'
    assert kwargs['position'] == 'lower_left'

    # Clearing hides the HUD again after a positioned frame.
    viewport.render_snap_feedback('x', screen_position=(10.0, 10.0), hud_lines=('a',))
    assert viewport.snap_hud is not None
    viewport.render_snap_feedback(None)
    assert viewport.snap_hud is None

    viewport.close()
    viewport.deleteLater()
    app.processEvents()


# -- controller: HUD content during gestures -------------------------------------

def test_move_hud_reports_delta_and_disabled_snap_reason(tmp_path) -> None:
    app, workspace, _repository = _workspace(tmp_path)
    workspace.select_entity('speaker-fl')
    transform = _transform(workspace)
    view_state = workspace.controller.view_state
    view_state.grid_snap_enabled = False
    view_state.object_snap_enabled = False

    transform.arm_move()
    assert transform.begin_at(QPointF(200.0, 200.0))
    assert transform.drag_to(QPointF(260.0, 230.0))

    text = _hud_text(workspace.viewport)
    # Disabled reasons are shown rather than a silent empty HUD (#979).
    assert 'オフ' in text
    assert '移動' in text
    assert 'ΔX' in text and 'ΔY' in text and 'ΔZ' in text
    assert 'Enter' in text and 'Esc' in text
    # Anchor follows the pointer when no snap candidate exists.
    anchor = _hud_anchor(workspace.viewport)
    assert anchor == pytest.approx((260.0, 230.0))

    transform.cancel()
    _teardown(app, workspace, transform)


def test_move_hud_names_snap_target_and_units(tmp_path) -> None:
    app, workspace, _repository = _workspace(tmp_path)
    workspace.select_entity('speaker-fl')
    transform = _transform(workspace)
    view_state = workspace.controller.view_state
    view_state.object_snap_enabled = True

    transform.arm_move()
    transform.begin_at(QPointF(200.0, 200.0))
    transform.drag_to(QPointF(230.0, 215.0))

    text = _hud_text(workspace.viewport)
    # Candidate kind (JA), target name + id, coordinate, DIP distance (#979).
    assert any(k in text for k in ('頂点', '中点', '辺', '整列'))
    assert '·' in text and 'px' in text
    # Anchored at the projected candidate point, not the pointer.
    anchor = _hud_anchor(workspace.viewport)
    assert anchor == pytest.approx((400.0, 300.0))

    transform.cancel()
    _teardown(app, workspace, transform)


def test_move_hud_units_follow_display_policy(tmp_path) -> None:
    app, workspace, _repository = _workspace(tmp_path)
    workspace.select_entity('speaker-fl')
    transform = _transform(workspace)
    transform.set_length_policy(display_length_policy('mm'))
    workspace.controller.view_state.object_snap_enabled = False

    transform.arm_move()
    transform.begin_at(QPointF(200.0, 200.0))
    transform.drag_to(QPointF(206.0, 200.0))  # +6 px → +0.1 m == 100 mm

    text = _hud_text(workspace.viewport)
    assert 'mm' in text
    assert 'ΔX +100.0 mm' in text

    transform.cancel()
    _teardown(app, workspace, transform)


def test_rotate_hud_reports_angle(tmp_path) -> None:
    app, workspace, _repository = _workspace(tmp_path)
    workspace.select_entity('speaker-fl')
    transform = _transform(workspace)
    workspace.controller.view_state.angle_snap_enabled = False

    transform.arm_rotate()
    transform.begin_at(QPointF(200.0, 200.0))
    transform.drag_to(QPointF(230.0, 200.0))  # 30 px → 15.0°

    text = _hud_text(workspace.viewport)
    assert '回転' in text
    assert '角度 +15.0°' in text
    assert '角度スナップ: オフ' in text

    transform.cancel()
    _teardown(app, workspace, transform)


def test_grid_snap_line_distinguishes_from_axis_and_object(tmp_path) -> None:
    app, workspace, _repository = _workspace(tmp_path)
    workspace.select_entity('speaker-fl')
    transform = _transform(workspace)
    view_state = workspace.controller.view_state
    view_state.grid_snap_enabled = True
    view_state.object_snap_enabled = False

    transform.arm_move()
    transform.begin_at(QPointF(200.0, 200.0))
    # +7 px ≈ +0.117 m off-grid → snapped to the +0.10 m grid line.
    transform.drag_to(QPointF(207.0, 200.0))

    text = _hud_text(workspace.viewport)
    assert 'グリッドスナップ 0.050 m' in text
    assert 'ΔX +0.100 m' in text

    # Axis constraint is labelled as such — separate from the snap state
    # (production path: the room.transform.axis_x QShortcut → set_axis).
    transform.set_axis(CadAxis.X)
    text = _hud_text(workspace.viewport)
    assert 'X軸拘束' in text
    assert 'グリッドスナップ' in text

    transform.cancel()
    _teardown(app, workspace, transform)


def test_shift_temporarily_releases_snap_and_reports_it(tmp_path, monkeypatch) -> None:
    app, workspace, _repository = _workspace(tmp_path)
    workspace.select_entity('speaker-fl')
    transform = _transform(workspace)
    view_state = workspace.controller.view_state
    view_state.object_snap_enabled = True
    monkeypatch.setattr(
        QGuiApplication, 'keyboardModifiers',
        staticmethod(lambda: Qt.KeyboardModifier.ShiftModifier),
    )

    transform.arm_move()
    transform.begin_at(QPointF(200.0, 200.0))
    transform.drag_to(QPointF(230.0, 215.0))

    text = _hud_text(workspace.viewport)
    assert 'スナップ一時解除中' in text

    transform.cancel()
    _teardown(app, workspace, transform)


# -- numeric delta input ----------------------------------------------------------

def test_numeric_move_commits_exact_0125m_as_one_undo_step(tmp_path) -> None:
    app, workspace, _repository = _workspace(tmp_path)
    workspace.select_entity('speaker-fl')
    transform = _transform(workspace)
    before = _entity(workspace, 'speaker-fl').position

    transform.arm_move()
    _type(transform, '0.125')
    text = _hud_text(workspace.viewport)
    assert '入力: 0.125' in text
    assert 'ΔX +0.125 m' in text

    assert transform._key_press(_key(Qt.Key.Key_Return)) is True
    moved = _entity(workspace, 'speaker-fl').position
    assert moved.x_m == pytest.approx(before.x_m + 0.125)
    assert moved.y_m == pytest.approx(before.y_m)
    assert moved.z_m == pytest.approx(before.z_m)

    assert workspace.undo() is True
    assert _entity(workspace, 'speaker-fl').position == before
    assert workspace.redo() is True
    assert _entity(workspace, 'speaker-fl').position.x_m == pytest.approx(before.x_m + 0.125)

    _teardown(app, workspace, transform)


def test_numeric_move_parses_display_unit(tmp_path) -> None:
    app, workspace, _repository = _workspace(tmp_path)
    workspace.select_entity('speaker-fl')
    transform = _transform(workspace)
    transform.set_length_policy(display_length_policy('mm'))
    before = _entity(workspace, 'speaker-fl').position

    transform.arm_move()
    _type(transform, '125')
    assert 'mm' in _hud_text(workspace.viewport)
    assert transform._key_press(_key(Qt.Key.Key_Return)) is True
    moved = _entity(workspace, 'speaker-fl').position
    assert moved.x_m == pytest.approx(before.x_m + 0.125)

    _teardown(app, workspace, transform)


def test_numeric_axis_key_retargets_delta(tmp_path) -> None:
    app, workspace, _repository = _workspace(tmp_path)
    workspace.select_entity('speaker-fl')
    transform = _transform(workspace)
    before = _entity(workspace, 'speaker-fl').position

    transform.arm_move()
    _type(transform, '0.5')
    assert transform._key_press(_key(Qt.Key.Key_Y, text='y')) is True
    assert 'ΔY +0.500 m' in _hud_text(workspace.viewport)
    assert transform._key_press(_key(Qt.Key.Key_Return)) is True
    moved = _entity(workspace, 'speaker-fl').position
    assert moved.y_m == pytest.approx(before.y_m + 0.5)
    assert moved.x_m == pytest.approx(before.x_m)

    _teardown(app, workspace, transform)


def test_numeric_rotate_commits_exact_15deg(tmp_path) -> None:
    app, workspace, _repository = _workspace(tmp_path)
    workspace.select_entity('speaker-fl')
    transform = _transform(workspace)

    transform.arm_rotate()
    _type(transform, '15')
    assert '角度 +15.0°' in _hud_text(workspace.viewport)
    assert transform._key_press(_key(Qt.Key.Key_Return)) is True
    yaw, pitch, roll = quaternion_to_euler_deg(_entity(workspace, 'speaker-fl').orientation)
    assert yaw == pytest.approx(15.0)
    assert roll == pytest.approx(0.0)
    assert pitch == pytest.approx(0.0)

    _teardown(app, workspace, transform)


def test_numeric_group_move_applies_one_delta_to_all(tmp_path) -> None:
    app, workspace, _repository = _workspace(tmp_path)
    workspace.controller.view_state.set_selection(('speaker-fl', 'speaker-fr'))
    transform = _transform(workspace)
    fl_before = _entity(workspace, 'speaker-fl').position
    fr_before = _entity(workspace, 'speaker-fr').position

    transform.arm_move()
    _type(transform, '0.125')
    assert transform._key_press(_key(Qt.Key.Key_Return)) is True
    fl = _entity(workspace, 'speaker-fl').position
    fr = _entity(workspace, 'speaker-fr').position
    assert fl.x_m == pytest.approx(fl_before.x_m + 0.125)
    assert fr.x_m == pytest.approx(fr_before.x_m + 0.125)
    # One gesture = one undo step for the whole group.
    assert workspace.undo() is True
    assert _entity(workspace, 'speaker-fl').position == fl_before
    assert _entity(workspace, 'speaker-fr').position == fr_before

    _teardown(app, workspace, transform)


def test_numeric_esc_cancels_without_touching_scene_revision(tmp_path) -> None:
    app, workspace, repository = _workspace(tmp_path)
    workspace.select_entity('speaker-fl')
    transform = _transform(workspace)
    head_before = repository.current_head(F1_DOCUMENT_ID).revision_id

    transform.arm_move()
    _type(transform, '0.125')
    assert workspace.controller.working.has_preview
    assert transform._key_press(_key(Qt.Key.Key_Escape)) is True

    assert repository.current_head(F1_DOCUMENT_ID).revision_id == head_before
    assert not workspace.controller.working.has_preview
    assert transform.mode is None
    # HUD cleared with the gesture.
    assert workspace.viewport.last_feedback()[0] is None

    _teardown(app, workspace, transform)


def test_numeric_entry_swallows_edit_shortcut_keys(tmp_path) -> None:
    app, workspace, _repository = _workspace(tmp_path)
    workspace.select_entity('speaker-fl')
    transform = _transform(workspace)
    before = _entity(workspace, 'speaker-fl').position

    transform.arm_move()
    _type(transform, '1')
    assert transform._numeric_entry
    # While the entry owns the gesture, edit verbs must not fire.
    assert transform._key_press(_key(Qt.Key.Key_L, text='l')) is True
    assert transform._key_press(_key(Qt.Key.Key_Delete)) is True
    assert transform._key_press(_key(Qt.Key.Key_Left)) is True
    assert not workspace.controller.view_state.is_locked('speaker-fl')
    assert _entity(workspace, 'speaker-fl').position.x_m == pytest.approx(before.x_m)
    assert workspace.controller.working.has_preview

    assert transform._key_press(_key(Qt.Key.Key_Return)) is True
    assert _entity(workspace, 'speaker-fl').position.x_m == pytest.approx(before.x_m + 1.0)

    _teardown(app, workspace, transform)


def test_pointer_release_commits_typed_value_not_pointer_position(tmp_path) -> None:
    app, workspace, _repository = _workspace(tmp_path)
    workspace.select_entity('speaker-fl')
    transform = _transform(workspace)
    before = _entity(workspace, 'speaker-fl').position

    transform.arm_move()
    _type(transform, '0.125')
    # Pointer moves during entry are inert for the delta.
    assert transform.drag_to(QPointF(999.0, 999.0)) is True
    assert 'ΔX +0.125 m' in _hud_text(workspace.viewport)
    # A click-release mid-entry commits the typed delta.
    assert transform.finish_at(QPointF(999.0, 999.0)) is True
    assert _entity(workspace, 'speaker-fl').position.x_m == pytest.approx(before.x_m + 0.125)

    _teardown(app, workspace, transform)


def test_numeric_backspace_edits_buffer(tmp_path) -> None:
    app, workspace, _repository = _workspace(tmp_path)
    workspace.select_entity('speaker-fl')
    transform = _transform(workspace)
    before = _entity(workspace, 'speaker-fl').position

    transform.arm_move()
    _type(transform, '0.25')
    assert transform._key_press(_key(Qt.Key.Key_Backspace)) is True
    assert transform._key_press(_key(Qt.Key.Key_Backspace)) is True
    # Buffer '0.' → 0.0 preview.
    assert '入力: 0.' in _hud_text(workspace.viewport)
    assert transform._key_press(_key(Qt.Key.Key_Escape)) is True
    assert _entity(workspace, 'speaker-fl').position == before

    _teardown(app, workspace, transform)


def test_armed_enter_and_esc_still_commit_and_cancel(tmp_path) -> None:
    app, workspace, _repository = _workspace(tmp_path)
    workspace.select_entity('speaker-fl')
    transform = _transform(workspace)

    transform.arm_move()
    assert transform.mode == 'move'
    assert transform._key_press(_key(Qt.Key.Key_Escape)) is True
    assert transform.mode is None

    transform.arm_rotate()
    assert transform.mode == 'rotate'
    assert transform._key_press(_key(Qt.Key.Key_Return)) is True
    assert transform.mode is None

    _teardown(app, workspace, transform)


def test_locked_selection_cannot_arm_gizmo(tmp_path) -> None:
    app, workspace, _repository = _workspace(tmp_path)
    workspace.select_entity('speaker-fl')
    workspace.controller.view_state.set_locked('speaker-fl', True)
    transform = _transform(workspace)

    transform.arm_move()
    assert transform.mode is None
    transform.arm_rotate()
    assert transform.mode is None

    _teardown(app, workspace, transform)


def test_group_rotate_commits_all_members(tmp_path) -> None:
    app, workspace, _repository = _workspace(tmp_path)
    # Primary (last id) is the rotation pivot — its position stays put while
    # the other member orbits it; both orientations take the 90° turn.
    workspace.controller.view_state.set_selection(('speaker-fr', 'speaker-fl'))
    transform = _transform(workspace)
    fl_pos = _entity(workspace, 'speaker-fl').position
    fr_pos = _entity(workspace, 'speaker-fr').position
    fl_orient = _entity(workspace, 'speaker-fl').orientation
    fr_orient = _entity(workspace, 'speaker-fr').orientation

    transform.arm_rotate()
    _type(transform, '90')
    assert transform._key_press(_key(Qt.Key.Key_Return)) is True
    fl = _entity(workspace, 'speaker-fl').position
    fr = _entity(workspace, 'speaker-fr').position
    # Pivot member does not translate; orbiting member does.
    assert fl == fl_pos
    assert (fr.x_m, fr.y_m) != pytest.approx((fr_pos.x_m, fr_pos.y_m))
    # Both orientations take the same world-axis +90° turn (fr already has
    # a non-identity aim, so compare quaternions, not absolute yaw).
    for entity_id, before_orient in (
        ('speaker-fl', fl_orient),
        ('speaker-fr', fr_orient),
    ):
        expected = rotate_orientation_world(before_orient, 'z', 90.0)
        actual = _entity(workspace, entity_id).orientation
        for field in ('w', 'x', 'y', 'z'):
            assert getattr(actual, field) == pytest.approx(
                getattr(expected, field), abs=1e-6
            )

    _teardown(app, workspace, transform)
