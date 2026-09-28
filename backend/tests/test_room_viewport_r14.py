"""Round-14 render-truth regression tests.

Covers the sightline overlay eye position: the drawn line must start at the
evaluator's authoritative eye point (local offset rotated by the seat's
orientation, as stored on ``SeatViewingResult.eye_position``), and seats
without eye authority (legacy bindings, ``eye_position=None``) must not get a
fabricated sightline at all. Also covers envelope-actor world bounds.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from htdt.cad_scene import (
    Offset3,
    Position3,
    Quaternion4,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
    domain_to_render,
    quaternion_from_euler_deg,
    scene_content_hash,
)
from htdt.cad_video_geometry import (
    SeatGeometryBinding,
    SeatSightlineResult,
    SeatViewingResult,
    _seat_eye,
)
from htdt.room_viewport import RoomOverlayState, RoomViewport3D, _entity_mesh


@pytest.fixture(scope="session", autouse=True)
def _app() -> QApplication:
    return QApplication.instance() or QApplication(["pytest-r14"])


class _CapturingPlotter:
    """Plotter double that stores (mesh, kwargs) per add_mesh call."""

    def __init__(self) -> None:
        self.renders = 0
        self.meshes: list[tuple[object, dict]] = []
        self.suppress_rendering = False

    def clear(self) -> None:
        pass

    def set_background(self, *args, **kwargs) -> None:
        pass

    def add_mesh(self, mesh=None, **kwargs):
        self.meshes.append((mesh, kwargs))
        return SimpleNamespace(name=kwargs.get("name", ""))

    def add_point_labels(self, *args, **kwargs):
        return SimpleNamespace(name="labels")

    def add_legend(self, *args, **kwargs):
        pass

    def add_axes(self, *args, **kwargs):
        pass

    def add_text(self, *args, **kwargs):
        pass

    def remove_actor(self, *args, **kwargs):
        pass

    def render(self) -> None:
        self.renders += 1


def _document(seat: SceneEntity) -> SceneDocument:
    return SceneDocument(
        document_id="doc-r14",
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=(seat,),
    )


def _seat(yaw_deg: float = 0.0) -> SceneEntity:
    return SceneEntity(
        entity_id="seat-1",
        kind="seat",
        name="seat-1",
        position=Position3(x_m=2.0, y_m=2.0, z_m=0.45),
        orientation=quaternion_from_euler_deg(
            yaw_deg=yaw_deg, pitch_deg=0.0, roll_deg=0.0
        ),
        speaker_role=None,
        size_m=Size3(x_m=0.6, y_m=0.7, z_m=1.0),
    )


def _binding(seat: SceneEntity) -> SeatGeometryBinding:
    return SeatGeometryBinding(
        entity_id=seat.entity_id,
        row_id="row-a",
        eye_reference_offset_local_m=Offset3(x_m=0.0, y_m=0.30, z_m=1.10),
        head_center_offset_local_m=Offset3(x_m=0.0, y_m=0.35, z_m=1.25),
        head_radius_m=0.10,
    )


def _evaluation(
    document: SceneDocument,
    *,
    binding: SeatGeometryBinding,
    eye_position: Position3 | None,
) -> SimpleNamespace:
    viewing = (
        SeatViewingResult(
            seat_entity_id="seat-1",
            row_id="row-a",
            eye_position=eye_position,
            horizontal_viewing_angle_deg=38.0 if eye_position else None,
            vertical_viewing_angle_deg=15.0 if eye_position else None,
            center_elevation_angle_deg=4.0 if eye_position else None,
            horizontal_status="PASS" if eye_position else "UNKNOWN",
            vertical_status="PASS" if eye_position else "UNKNOWN",
            center_elevation_status="PASS" if eye_position else "UNKNOWN",
        ),
    )
    corners = (
        Position3(x_m=1.0, y_m=0.1, z_m=0.8),
        Position3(x_m=3.0, y_m=0.1, z_m=0.8),
        Position3(x_m=3.0, y_m=0.1, z_m=2.0),
        Position3(x_m=1.0, y_m=0.1, z_m=2.0),
    )
    return SimpleNamespace(
        target=SimpleNamespace(scene_content_hash=scene_content_hash(document)),
        projection=None,
        surface=SimpleNamespace(
            status="PASS",
            image_center=Position3(x_m=2.0, y_m=0.1, z_m=1.4),
            image_plane_corners=corners,
        ),
        viewing=viewing,
        sightlines=(
            SeatSightlineResult(
                seat_entity_id="seat-1",
                row_id="row-a",
                status="PASS" if eye_position else "UNKNOWN",
                blocking_seat_ids=(),
                blocking_row_ids=(),
                blocked_sample_ids=(),
                minimum_head_ray_clearance_m=None,
            ),
        ),
        collisions=(),
        request=SimpleNamespace(seats=(binding,)),
    )


def _sightline_meshes(plotter: _CapturingPlotter) -> list[object]:
    return [
        mesh
        for mesh, kwargs in plotter.meshes
        if kwargs.get("name") == "video-sightline-seat-1"
    ]


def test_sightline_draws_from_rotated_evaluator_eye(_app) -> None:
    viewport = RoomViewport3D()
    plotter = _CapturingPlotter()
    viewport.plotter = plotter
    seat = _seat(yaw_deg=90.0)
    document = _document(seat)
    viewport.render_document(
        document, selected_id=None, overlays=RoomOverlayState()
    )
    binding = _binding(seat)
    eye = _seat_eye(seat, binding)
    viewport.render_video_overlay(
        _evaluation(
            document,
            binding=binding,
            eye_position=Position3(x_m=eye[0], y_m=eye[1], z_m=eye[2]),
        )
    )
    lines = _sightline_meshes(plotter)
    assert len(lines) == 1
    expected = domain_to_render(Position3(x_m=eye[0], y_m=eye[1], z_m=eye[2]))
    start = tuple(float(value) for value in lines[0].points[0])
    # The rotated eye is offset laterally from seat.position — the previous
    # unrotated computation placed it ~0.42 m away.
    assert start == pytest.approx(expected, abs=1e-6)
    unrotated = (
        seat.position.x_m + 0.0,
        -(seat.position.y_m + 0.30),
        seat.position.z_m + 1.10,
    )
    assert start != pytest.approx(unrotated, abs=1e-6)


def test_sightline_not_fabricated_without_eye_authority(_app) -> None:
    viewport = RoomViewport3D()
    plotter = _CapturingPlotter()
    viewport.plotter = plotter
    seat = _seat()
    document = _document(seat)
    viewport.render_document(
        document, selected_id=None, overlays=RoomOverlayState()
    )
    viewport.render_video_overlay(
        _evaluation(document, binding=_binding(seat), eye_position=None)
    )
    assert _sightline_meshes(plotter) == []


def test_entity_mesh_world_bounds_match_model(_app) -> None:
    seat = _seat()
    mesh = _entity_mesh(seat)
    p, s = seat.position, seat.size_m
    assert s is not None
    expected = (
        p.x_m - s.x_m / 2,
        p.x_m + s.x_m / 2,
        -p.y_m - s.y_m / 2,
        -p.y_m + s.y_m / 2,
        p.z_m - s.z_m / 2,
        p.z_m + s.z_m / 2,
    )
    # float32 point storage in vtkPoints: ~1e-7 relative rounding.
    assert mesh.bounds == pytest.approx(expected, abs=1e-6)
