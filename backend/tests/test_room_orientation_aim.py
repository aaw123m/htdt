from __future__ import annotations

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QApplication, QFrame

from htdt.cad_document import EditStateError
from htdt.cad_objects import (
    TheaterObjectError,
    aim_yaw_pitch_deg,
    direction_from_yaw_pitch_deg,
    orientation_aligning_forward,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    F1_DOCUMENT_ID,
    Direction3,
    Position3,
    make_f1_scene,
    quaternion_from_euler_deg,
    quaternion_to_euler_deg,
    quaternion_to_matrix3,
)
from htdt.room_viewport import RoomOverlayState, _selection_direction_rays
from htdt.room_workspace import RoomWorkspace, RoomWorkspaceController


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


class FakeRoomViewport(QFrame):
    entitySelected = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.render_calls: list[tuple[str | None, RoomOverlayState, bool]] = []

    def render_document(
        self,
        document,
        *,
        selected_id: str | None,
        overlays: RoomOverlayState,
        reset_camera: bool = False,
    ) -> None:
        self.render_calls.append((selected_id, overlays, reset_camera))

    def fit_scene(self) -> None:
        pass

    def focus_entity(self, entity_id: str) -> None:
        pass


def _controller_repository(tmp_path) -> SceneRepository:
    """Repository with the F1 fixture persisted explicitly (#627)."""

    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(make_f1_scene(), parent_revision_id=None)
    return repository


def _workspace(tmp_path) -> RoomWorkspace:
    app = _app()
    repository = _controller_repository(tmp_path)
    workspace = RoomWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: FakeRoomViewport(parent),
    )
    workspace.resize(1200, 800)
    workspace.show()
    app.processEvents()
    return workspace


def _close(workspace: RoomWorkspace) -> None:
    app = _app()
    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def _forward_vector(entity) -> tuple[float, float, float]:
    matrix = quaternion_to_matrix3(entity.orientation)
    return (matrix[0][1], matrix[1][1], matrix[2][1])


# --- Inspector surface -------------------------------------------------------


def test_inspector_exposes_yaw_pitch_roll_for_physical_entities(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    try:
        workspace.select_entity("speaker-fl")
        inspector = workspace.inspector
        for field in inspector.orientation_fields.values():
            assert field.isVisible()
            assert field.isEnabled()
            assert field.suffix() == "°"
        # speaker-fl has no aim yet: unknown aim is shown as text, not 0.00°.
        assert inspector.aim_section.isVisible()
        assert not inspector.aim_known_host.isVisible()
        assert "未設定" in inspector.aim_state_label.text()

        workspace.select_entity("point-mlp")
        for field in inspector.orientation_fields.values():
            assert not field.isVisible()
        assert not inspector.orientation_header.isVisible()
        # Acoustic aim is speaker-only authority.
        assert not inspector.aim_section.isVisible()

        workspace.select_entity("furniture-left")
        for field in inspector.orientation_fields.values():
            assert field.isVisible()
        assert not inspector.aim_section.isVisible()
    finally:
        _close(workspace)


def test_numeric_orientation_edit_commits_through_quaternion_authority(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    try:
        workspace.select_entity("speaker-fl")
        before = workspace.controller.committed_document.entity("speaker-fl")

        inspector = workspace.inspector
        inspector.orientation_fields["Yaw"].setValue(30.0)
        inspector.orientation_fields["Yaw"].editingFinished.emit()

        entity = workspace.controller.committed_document.entity("speaker-fl")
        yaw, pitch, roll = quaternion_to_euler_deg(entity.orientation)
        assert yaw == pytest.approx(30.0)
        assert pitch == pytest.approx(0.0, abs=1e-9)
        assert roll == pytest.approx(0.0, abs=1e-9)
        # Body rotation must not fabricate an unknown acoustic aim.
        assert entity.aim_xyz is None
        assert entity.aim_xyz == before.aim_xyz
        assert entity.position == before.position

        # One numeric edit is exactly one Undo transaction.
        assert workspace.controller.working.history_length == 1
        assert workspace.undo()
        restored = workspace.controller.committed_document.entity("speaker-fl")
        assert restored.orientation == before.orientation
    finally:
        _close(workspace)


def test_orientation_edit_preserves_untouched_euler_axes_exactly(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    try:
        workspace.select_entity("furniture-left")
        exact = quaternion_from_euler_deg(
            yaw_deg=10.0,
            pitch_deg=12.345678,
            roll_deg=-8.765432,
        )
        workspace.controller.working.update_entity(
            "furniture-left",
            orientation=exact,
        )
        workspace._refresh()

        workspace.inspector.orientation_fields["Yaw"].setValue(-45.0)
        workspace.inspector.orientation_fields["Yaw"].editingFinished.emit()

        entity = workspace.controller.committed_document.entity("furniture-left")
        yaw, pitch, roll = quaternion_to_euler_deg(entity.orientation)
        assert yaw == pytest.approx(-45.0)
        # Untouched axes keep their exact authority values instead of snapping
        # to the 3-decimal display rounding.
        assert pitch == pytest.approx(12.345678, abs=1e-6)
        assert roll == pytest.approx(-8.765432, abs=1e-6)
    finally:
        _close(workspace)


def test_numeric_and_gizmo_rotation_share_quaternion_authority(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    try:
        workspace.select_entity("speaker-fl")
        working = workspace.controller.working
        before = working.committed_document.entity("speaker-fl")

        # Gizmo-style rotation commits a TransformEntitiesCommand preview.
        working.begin_rotate("speaker-fl")
        working.preview_rotate(
            quaternion_from_euler_deg(yaw_deg=25.0, pitch_deg=0.0, roll_deg=0.0)
        )
        assert working.commit_preview()
        workspace._refresh()

        # The same quaternion authority feeds the numeric Inspector field.
        assert workspace.inspector.orientation_fields["Yaw"].value() == pytest.approx(
            25.0, abs=1e-3
        )

        workspace.inspector.orientation_fields["Yaw"].setValue(-10.0)
        workspace.inspector.orientation_fields["Yaw"].editingFinished.emit()
        yaw, _, _ = quaternion_to_euler_deg(
            workspace.controller.committed_document.entity("speaker-fl").orientation
        )
        assert yaw == pytest.approx(-10.0)
        assert workspace.controller.committed_document.entity("speaker-fl").aim_xyz == before.aim_xyz
    finally:
        _close(workspace)


# --- Acoustic aim ------------------------------------------------------------


def test_aim_at_measurement_point_establishes_known_aim(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    try:
        workspace.select_entity("speaker-fl")
        before = workspace.controller.committed_document.entity("speaker-fl")
        assert before.aim_xyz is None

        combo = workspace.inspector.aim_target_combo
        index = combo.findData("point-mlp")
        assert index >= 0
        combo.setCurrentIndex(index)
        workspace.inspector.aim_apply_button.click()

        entity = workspace.controller.committed_document.entity("speaker-fl")
        assert entity.aim_xyz is not None
        target = workspace.controller.committed_document.entity("point-mlp").position
        source = before.position
        delta = np.asarray(
            (target.x_m - source.x_m, target.y_m - source.y_m, target.z_m - source.z_m)
        )
        expected = delta / np.linalg.norm(delta)
        assert entity.aim_xyz.x == pytest.approx(expected[0])
        assert entity.aim_xyz.y == pytest.approx(expected[1])
        assert entity.aim_xyz.z == pytest.approx(expected[2])
        # Setting aim never rotates the cabinet.
        assert entity.orientation == before.orientation
        assert workspace.controller.working.history_length == 1

        assert workspace.undo()
        assert workspace.controller.committed_document.entity("speaker-fl").aim_xyz is None
    finally:
        _close(workspace)


def test_aim_at_seat_uses_acoustic_reference_position(tmp_path) -> None:
    repository = _controller_repository(tmp_path)
    controller = RoomWorkspaceController(repository, F1_DOCUMENT_ID)
    seat = controller.add_object("seat")
    controller.set_selection("speaker-fl")

    target_ids = [entity.entity_id for entity in controller.aim_targets()]
    assert seat.entity_id in target_ids
    assert "point-mlp" in target_ids

    assert controller.aim_selected_speaker_at(seat.entity_id)
    entity = controller.document.entity("speaker-fl")
    assert entity.aim_xyz is not None
    assert entity.orientation == controller.document.entity("speaker-fl").orientation
    reference = seat.position.model_copy(
        update={"z_m": seat.position.z_m + 0.65}
    )
    delta = np.asarray(
        (
            reference.x_m - entity.position.x_m,
            reference.y_m - entity.position.y_m,
            reference.z_m - entity.position.z_m,
        )
    )
    expected = delta / np.linalg.norm(delta)
    assert entity.aim_xyz.y == pytest.approx(expected[1])


def test_clear_aim_returns_explicit_unknown_and_undo_restores(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    try:
        workspace.select_entity("speaker-fr")
        before = workspace.controller.committed_document.entity("speaker-fr")
        assert before.aim_xyz is not None

        workspace.inspector.aim_clear_button.click()

        entity = workspace.controller.committed_document.entity("speaker-fr")
        assert entity.aim_xyz is None
        assert entity.orientation == before.orientation
        assert workspace.controller.working.history_length == 1

        assert workspace.undo()
        restored = workspace.controller.committed_document.entity("speaker-fr")
        assert restored.aim_xyz == before.aim_xyz
    finally:
        _close(workspace)


def test_align_cabinet_to_aim_is_explicit_and_preserves_aim(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    try:
        workspace.select_entity("speaker-fl")
        assert workspace.controller.aim_selected_speaker_at("point-mlp")
        aimed = workspace.controller.committed_document.entity("speaker-fl")
        assert aimed.aim_xyz is not None

        workspace.inspector.aim_align_button.click()

        entity = workspace.controller.committed_document.entity("speaker-fl")
        forward = _forward_vector(entity)
        assert forward[0] == pytest.approx(entity.aim_xyz.x, abs=1e-6)
        assert forward[1] == pytest.approx(entity.aim_xyz.y, abs=1e-6)
        assert forward[2] == pytest.approx(entity.aim_xyz.z, abs=1e-6)
        # The explicit alignment does not modify the acoustic aim authority.
        assert entity.aim_xyz == aimed.aim_xyz
        # Aim and align are separate commands: two Undo transactions.
        assert workspace.controller.working.history_length == 2
        assert workspace.undo()
        restored = workspace.controller.committed_document.entity("speaker-fl")
        assert restored.orientation == aimed.orientation
        assert restored.aim_xyz == aimed.aim_xyz
    finally:
        _close(workspace)


def test_aim_yaw_pitch_fields_edit_aim_without_touching_body(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    try:
        workspace.select_entity("speaker-fr")
        before = workspace.controller.committed_document.entity("speaker-fr")
        assert before.aim_xyz is not None
        exact_yaw, exact_pitch = aim_yaw_pitch_deg(before.aim_xyz)

        inspector = workspace.inspector
        assert inspector.aim_known_host.isVisible()
        assert inspector.aim_yaw_field.value() == pytest.approx(exact_yaw, abs=1e-3)
        assert inspector.aim_pitch_field.value() == pytest.approx(exact_pitch, abs=1e-3)

        inspector.aim_yaw_field.setValue(-20.0)
        inspector.aim_yaw_field.editingFinished.emit()

        entity = workspace.controller.committed_document.entity("speaker-fr")
        yaw, pitch = aim_yaw_pitch_deg(entity.aim_xyz)
        assert yaw == pytest.approx(-20.0)
        # The untouched pitch component keeps its exact authority value.
        assert pitch == pytest.approx(exact_pitch, abs=1e-6)
        # Aim edits never rotate the cabinet.
        assert entity.orientation == before.orientation
        assert workspace.controller.working.history_length == 1
    finally:
        _close(workspace)


def test_unrelated_inspector_edits_never_touch_orientation_or_aim(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    try:
        workspace.select_entity("speaker-fr")
        exact = quaternion_from_euler_deg(
            yaw_deg=17.0, pitch_deg=3.25, roll_deg=-6.5
        )
        workspace.controller.working.update_entity("speaker-fr", orientation=exact)
        workspace._refresh()
        before = workspace.controller.committed_document.entity("speaker-fr")

        inspector = workspace.inspector
        inspector.name_field.setText("Front Right renamed")
        inspector.name_field.editingFinished.emit()

        entity = workspace.controller.committed_document.entity("speaker-fr")
        assert entity.name == "Front Right renamed"
        # No drift: the quaternion is byte-identical, not re-saved from a
        # rounded Euler display, and aim is untouched.
        assert entity.orientation == exact
        assert entity.aim_xyz == before.aim_xyz
    finally:
        _close(workspace)


def test_unknown_aim_is_not_fabricated_by_numeric_edits(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    try:
        workspace.select_entity("speaker-fl")
        inspector = workspace.inspector
        assert not inspector.aim_known_host.isVisible()

        inspector.position_fields["X"].setValue(1.9)
        inspector.position_fields["X"].editingFinished.emit()
        inspector.orientation_fields["Yaw"].setValue(12.0)
        inspector.orientation_fields["Yaw"].editingFinished.emit()

        entity = workspace.controller.committed_document.entity("speaker-fl")
        assert entity.aim_xyz is None
        yaw, _, _ = quaternion_to_euler_deg(entity.orientation)
        assert yaw == pytest.approx(12.0)
    finally:
        _close(workspace)


# --- Controller guards -------------------------------------------------------


def test_aim_actions_fail_closed(tmp_path) -> None:
    repository = _controller_repository(tmp_path)
    controller = RoomWorkspaceController(repository, F1_DOCUMENT_ID)

    with pytest.raises(EditStateError):
        controller.aim_selected_speaker_at("point-mlp")

    controller.set_selection("furniture-left")
    with pytest.raises(EditStateError):
        controller.aim_selected_speaker_at("point-mlp")
    with pytest.raises(EditStateError):
        controller.align_selected_cabinet_to_aim()

    controller.set_selection("speaker-fl")
    with pytest.raises(TheaterObjectError):
        controller.aim_selected_speaker_at("furniture-left")
    with pytest.raises(EditStateError, match="未設定"):
        controller.align_selected_cabinet_to_aim()

    controller.view_state.set_locked("speaker-fl", True)
    with pytest.raises(EditStateError, match="ロック"):
        controller.aim_selected_speaker_at("point-mlp")
    with pytest.raises(EditStateError, match="ロック"):
        controller.clear_selected_speaker_aim()


def test_update_selected_rejects_orientation_and_aim_for_wrong_kinds(tmp_path) -> None:
    repository = _controller_repository(tmp_path)
    controller = RoomWorkspaceController(repository, F1_DOCUMENT_ID)
    point = controller.document.entity("point-mlp")
    controller.set_selection("point-mlp")
    with pytest.raises(EditStateError):
        controller.update_selected(
            name=point.name,
            position=point.position,
            size_m=None,
            speaker_role=None,
            orientation=quaternion_from_euler_deg(
                yaw_deg=10.0, pitch_deg=0.0, roll_deg=0.0
            ),
        )

    furniture = controller.document.entity("furniture-left")
    controller.set_selection("furniture-left")
    with pytest.raises(EditStateError):
        controller.update_selected(
            name=furniture.name,
            position=furniture.position,
            size_m=furniture.size_m,
            speaker_role=None,
            aim_yaw_pitch_deg=(10.0, 0.0),
        )


# --- Viewport direction cues --------------------------------------------------


def test_selection_direction_rays_show_forward_and_aim_distinctly() -> None:
    document = make_f1_scene()

    speaker = document.entity("speaker-fr")
    rays = _selection_direction_rays(speaker)
    assert [ray.role for ray in rays] == ["forward", "aim"]
    assert rays[0].color != rays[1].color

    # speaker-fl has identity orientation and unknown aim: forward only.
    rays = _selection_direction_rays(document.entity("speaker-fl"))
    assert [ray.role for ray in rays] == ["forward"]

    # A measurement point has no physical pose: no direction cues at all.
    assert _selection_direction_rays(document.entity("point-mlp")) == ()


def test_selection_forward_ray_follows_quaternion_orientation() -> None:
    document = make_f1_scene()
    speaker = document.entity("speaker-fl")
    rotated = speaker.model_copy(
        update={
            "orientation": quaternion_from_euler_deg(
                yaw_deg=-90.0, pitch_deg=0.0, roll_deg=0.0
            )
        }
    )
    rays = _selection_direction_rays(rotated)
    assert len(rays) == 1
    points = np.asarray(rays[0].mesh.points, dtype=float)
    direction = points[1] - points[0]
    direction /= np.linalg.norm(direction)
    # Body yaw -90° turns the +Y front toward +X; render maps +Y -> -Y.
    assert direction[0] == pytest.approx(1.0, abs=1e-6)
    assert direction[1] == pytest.approx(0.0, abs=1e-6)
    assert direction[2] == pytest.approx(0.0, abs=1e-6)


# --- Applied optimization round-trip ------------------------------------------


def test_applied_orientation_aim_candidate_is_visible_and_exactly_editable(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    try:
        working = workspace.controller.working
        before = working.committed_document.entity("speaker-fl")
        applied = before.model_copy(
            update={
                "position": Position3(x_m=1.5, y_m=0.9, z_m=1.05),
                "orientation": quaternion_from_euler_deg(
                    yaw_deg=-12.5, pitch_deg=0.0, roll_deg=0.0
                ),
                "aim_xyz": direction_from_yaw_pitch_deg(
                    yaw_deg=15.0, pitch_deg=5.0
                ),
            }
        )
        # Applying an O80/O100 candidate lands as one transform command.
        assert working.transform_entities((before,), (applied,))
        workspace.select_entity("speaker-fl")

        inspector = workspace.inspector
        assert inspector.orientation_fields["Yaw"].value() == pytest.approx(
            -12.5, abs=1e-3
        )
        assert inspector.aim_known_host.isVisible()
        assert inspector.aim_yaw_field.value() == pytest.approx(15.0, abs=1e-3)
        assert inspector.aim_pitch_field.value() == pytest.approx(5.0, abs=1e-3)

        # Exact independent editing of the applied acoustic aim.
        inspector.aim_pitch_field.setValue(0.0)
        inspector.aim_pitch_field.editingFinished.emit()
        entity = workspace.controller.committed_document.entity("speaker-fl")
        yaw, pitch = aim_yaw_pitch_deg(entity.aim_xyz)
        assert yaw == pytest.approx(15.0, abs=1e-6)
        assert pitch == pytest.approx(0.0, abs=1e-6)
        body_yaw, _, _ = quaternion_to_euler_deg(entity.orientation)
        assert body_yaw == pytest.approx(-12.5)
    finally:
        _close(workspace)


# --- Helper authority ---------------------------------------------------------


def test_aim_yaw_pitch_round_trips_and_validates() -> None:
    direction = direction_from_yaw_pitch_deg(yaw_deg=-31.5, pitch_deg=12.0)
    yaw, pitch = aim_yaw_pitch_deg(direction)
    assert yaw == pytest.approx(-31.5)
    assert pitch == pytest.approx(12.0)

    vertical = direction_from_yaw_pitch_deg(yaw_deg=40.0, pitch_deg=90.0)
    assert vertical.z == pytest.approx(1.0)
    assert aim_yaw_pitch_deg(vertical) == (pytest.approx(0.0), pytest.approx(90.0))

    with pytest.raises(TheaterObjectError):
        direction_from_yaw_pitch_deg(yaw_deg=0.0, pitch_deg=120.0)


def test_orientation_aligning_forward_points_body_front_along_aim() -> None:
    document = make_f1_scene()
    aim = document.entity("speaker-fr").aim_xyz
    assert aim is not None
    entity = document.entity("speaker-fr").model_copy(
        update={"orientation": orientation_aligning_forward(aim)}
    )
    forward = _forward_vector(entity)
    # The fixture aim is stored at 1e-6 quantization; the aligned forward is
    # the same direction normalized exactly.
    assert forward[0] == pytest.approx(aim.x, abs=1e-6)
    assert forward[1] == pytest.approx(aim.y, abs=1e-6)
    assert forward[2] == pytest.approx(aim.z, abs=1e-6)

    straight_up = orientation_aligning_forward(Direction3(x=0.0, y=0.0, z=1.0))
    matrix = quaternion_to_matrix3(straight_up)
    assert matrix[2][1] == pytest.approx(1.0, abs=1e-9)
