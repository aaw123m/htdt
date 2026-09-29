"""Round 14 geometry & spatial-modeling correctness regression tests.

Every expectation is hand-computed from the documented conventions:
canonical SI metres inside the domain; +X right / +Y rear / +Z up; entity
poses rotate a local offset by the quaternion matrix then translate; the
VTK render world is ``(x, -y, z)`` and pose matrices conjugate by
``C = diag(1, -1, 1)``; operational zones are entity-local with +Y as the
entity's forward axis.
"""

from __future__ import annotations

import math
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from pydantic import ValidationError
from shapely.geometry import Point

from htdt.cad_display_units import display_to_si, parse_length_input, si_to_display
from htdt.cad_document import WorkingDocument
from htdt.cad_listener_pose import (
    build_listener_pose,
    pose_acoustic_reference_position,
    pose_eye_reference_position,
)
from htdt.cad_operational_geometry import operational_zone_footprint
from htdt.cad_repository import SceneRevision
from htdt.cad_scene import (
    Offset3,
    OperationalZone,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
    acoustic_reference_position,
    canonical_scene_json,
    domain_pose_to_render_matrix,
    domain_to_render,
    quaternion_from_euler_deg,
    quaternion_to_euler_deg,
    quaternion_to_matrix3,
    render_delta_to_domain,
    rotate_orientation_world,
    rotate_position_world,
    scene_content_hash,
)
from htdt.geometry import polygon_from_vertices


def _seat(
    entity_id: str = 'seat-1',
    *,
    x: float,
    y: float,
    z: float = 0.55,
    yaw_deg: float = 180.0,
    offset: Offset3 | None = Offset3(z_m=0.65),
) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='seat',
        name=entity_id,
        position=Position3(x_m=x, y_m=y, z_m=z),
        orientation=quaternion_from_euler_deg(
            yaw_deg=yaw_deg, pitch_deg=0.0, roll_deg=0.0
        ),
        size_m=Size3(x_m=0.6, y_m=0.6, z_m=1.1),
        acoustic_reference_offset_m=offset,
    )


def _revision(document: SceneDocument) -> SceneRevision:
    return SceneRevision(
        revision_id='rev-geo',
        document_id=document.document_id,
        parent_revision_id=None,
        created_at_utc='2026-09-01T00:00:00+00:00',
        content_hash='a' * 64,
        document=document,
    )


# --- Units: UI boundary -> model -> solver-bound binding, all metres -------


def test_ui_length_entry_reaches_the_solver_boundary_in_exact_meters() -> None:
    # The display->SI contract is bit-exact for metric units: input divided
    # by an exact integer denominator equals the SI literal float.
    assert display_to_si(120.0, 'mm') == 0.12
    assert display_to_si(450.0, 'cm') == 4.5
    assert display_to_si(2.0, 'inch') == pytest.approx(0.0508)
    assert parse_length_input('120 mm') == 0.12
    assert si_to_display(0.12, 'mm') == 120.0

    # Ear height typed as 650 mm becomes the canonical 0.65 m offset on a
    # yaw-180 seat; the acoustic reference (and hence the solver receiver)
    # carries that exact metre value — never a display number.
    ear_z = display_to_si(650.0, 'mm')
    seat = _seat(x=2.0, y=3.0, offset=Offset3(z_m=ear_z))
    world = acoustic_reference_position(seat)
    assert world is not None
    assert (world.x_m, world.y_m, world.z_m) == pytest.approx((2.0, 3.0, 1.2))
    assert world.z_m == pytest.approx(seat.position.z_m + 0.65)

    # The solver boundary: the receiver binding's world_position field IS the
    # solver input — it must be the same metres the model computed.
    from htdt.cad_acoustic_snapshot import receiver_binding_from_scene

    document = SceneDocument(document_id='doc-geo', room=None, entities=(seat,))
    binding = receiver_binding_from_scene(
        scene_revision=_revision(document),
        entity_id='seat-1',
        requested_output_capabilities=('magnitude_response',),
    )
    assert binding.world_position == world
    assert binding.world_position.z_m == pytest.approx(1.2)


# --- Axes: asymmetric geometry survives scene -> render unchanged ----------


def test_asymmetric_placement_survives_scene_snapshot_and_render() -> None:
    seat = _seat(x=1.25, y=3.75, yaw_deg=30.0, offset=Offset3(
        x_m=0.1, y_m=0.2, z_m=0.65
    ))
    world = acoustic_reference_position(seat)
    assert world is not None
    # Hand-computed: R_z(30) * (0.1, 0.2, 0.65) + (1.25, 3.75, 0.55)
    c, s = math.cos(math.radians(30.0)), math.sin(math.radians(30.0))
    expected = (
        1.25 + c * 0.1 - s * 0.2,
        3.75 + s * 0.1 + c * 0.2,
        1.2,
    )
    assert (world.x_m, world.y_m, world.z_m) == pytest.approx(expected)

    # Render world is exactly (x, -y, z) — no permutation, no extra mirror.
    rx, ry, rz = domain_to_render(world)
    assert (rx, ry, rz) == (world.x_m, -world.y_m, world.z_m)

    # Pose matrix conjugates by C = diag(1, -1, 1): applying the render
    # rotation to the render image of a local axis must equal the render
    # image of the domain-rotated axis (C*R*C * C*v == C*(R*v)).
    entity = seat
    domain_matrix = quaternion_to_matrix3(entity.orientation)
    render_matrix = domain_pose_to_render_matrix(entity.position, entity.orientation)
    local_y = (0.0, 1.0, 0.0)
    domain_world = tuple(
        sum(domain_matrix[row][col] * local_y[col] for col in range(3))
        for row in range(3)
    )
    expected_render_axis = (domain_world[0], -domain_world[1], domain_world[2])
    # v_render = C * v_local
    local_render = (local_y[0], -local_y[1], local_y[2])
    actual_render_axis = tuple(
        render_matrix[row][0] * local_render[0]
        + render_matrix[row][1] * local_render[1]
        + render_matrix[row][2] * local_render[2]
        for row in range(3)
    )
    assert actual_render_axis == pytest.approx(expected_render_axis)
    # The render translation column is the render image of the position.
    assert render_matrix[0][3] == entity.position.x_m
    assert render_matrix[1][3] == -entity.position.y_m
    assert render_matrix[2][3] == entity.position.z_m

    # A render-space drag delta maps back by re-negating Y only.
    base = Position3(x_m=1.0, y_m=2.0, z_m=0.5)
    moved = render_delta_to_domain((0.3, 0.4, 0.1), base)
    assert (moved.x_m, moved.y_m, moved.z_m) == pytest.approx((1.3, 1.6, 0.6))


# --- Rotation: seat offsets land where a human computes them ---------------


def test_rotated_seat_reference_positions_match_hand_computation() -> None:
    # Yaw 180 maps local +Y to world -Y: offsets mirror in both XY axes.
    front = _seat(x=2.0, y=3.0, yaw_deg=180.0, offset=Offset3(
        x_m=0.1, y_m=0.2, z_m=0.65
    ))
    world = acoustic_reference_position(front)
    assert world is not None
    assert (world.x_m, world.y_m, world.z_m) == pytest.approx((1.9, 2.8, 1.2))

    # Yaw 90 maps (x, y) -> (-y, x).
    side = _seat(x=2.0, y=3.0, yaw_deg=90.0, offset=Offset3(
        x_m=0.1, y_m=0.2, z_m=0.65
    ))
    world = acoustic_reference_position(side)
    assert world is not None
    assert (world.x_m, world.y_m, world.z_m) == pytest.approx((1.8, 3.1, 1.2))

    # The listener-pose resolver uses the identical rotate-then-translate
    # convention for eye and acoustic references.
    pose = build_listener_pose(
        seat_entity_id='seat-1',
        label='pose',
        head_center_offset_local_m=Offset3(z_m=1.15),
        eye_reference_offset_local_m=Offset3(y_m=0.05, z_m=1.1),
        acoustic_reference_offset_local_m=Offset3(z_m=0.65),
        provenance='test',
    )
    eye = pose_eye_reference_position(side, pose)
    # R_z(90) * (0, 0.05, 1.1) = (-0.05, 0, 1.1) -> world (1.95, 3.0, 1.65)
    assert (eye.x_m, eye.y_m, eye.z_m) == pytest.approx((1.95, 3.0, 1.65))
    ear = pose_acoustic_reference_position(side, pose)
    assert (ear.x_m, ear.y_m, ear.z_m) == pytest.approx((2.0, 3.0, 1.2))

    # Euler round-trip recovers the authored pose.
    yaw, pitch, roll = quaternion_to_euler_deg(
        quaternion_from_euler_deg(yaw_deg=30.0, pitch_deg=10.0, roll_deg=5.0)
    )
    assert (yaw, pitch, roll) == pytest.approx((30.0, 10.0, 5.0))


# --- Transform composition: repeated edits compose, not square/drop --------


def test_transform_edits_compose_honestly() -> None:
    pivot = Position3(x_m=0.0, y_m=0.0, z_m=0.0)

    # Two 45-degree rotations of (1, 0, 0) about Z land at (0, 1, 0).
    once = rotate_position_world(
        Position3(x_m=1.0, y_m=0.0, z_m=0.0), pivot, 'z', 45.0
    )
    twice = rotate_position_world(once, pivot, 'z', 45.0)
    assert (twice.x_m, twice.y_m, twice.z_m) == pytest.approx((0.0, 1.0, 0.0))

    # Two committed 30-degree group rotates compose to exactly 60 degrees.
    working = WorkingDocument(
        SceneDocument(
            document_id='doc-geo',
            room=None,
            entities=(
                SceneEntity(
                    entity_id='spk',
                    kind='speaker',
                    name='spk',
                    speaker_role='L',
                    position=Position3(x_m=1.0, y_m=0.0, z_m=0.5),
                    size_m=Size3(x_m=0.3, y_m=0.3, z_m=0.3),
                ),
            ),
        )
    )
    for _ in range(2):
        working.begin_group_rotate(('spk',))
        working.preview_group_rotate('z', 30.0, pivot)
        assert working.commit_preview()
    entity = working.committed_document.entity('spk')
    assert (entity.position.x_m, entity.position.y_m) == pytest.approx(
        (0.5, math.sqrt(3.0) / 2.0)
    )
    yaw, _, _ = quaternion_to_euler_deg(entity.orientation)
    assert yaw == pytest.approx(60.0)

    # Within one gesture the preview is absolute — a second preview call with
    # the same delta re-bases, it does not accumulate twice.
    working.begin_group_move(('spk',))
    working.preview_group_move((1.0, 0.0, 0.0))
    working.preview_group_move((1.0, 0.0, 0.0))
    assert working.commit_preview()
    moved_once = working.committed_document.entity('spk')
    assert moved_once.position.x_m == pytest.approx(1.5)
    # The next committed gesture adds one more delta — total displacement 2.
    working.begin_group_move(('spk',))
    working.preview_group_move((1.0, 0.0, 0.0))
    assert working.commit_preview()
    assert working.committed_document.entity('spk').position.x_m == pytest.approx(2.5)


# --- Operational zones: sectors follow the entity-local frame --------------


def _zone_entity(
    entity_id: str,
    *,
    x: float,
    y: float,
    yaw_deg: float,
    zones: tuple[OperationalZone, ...],
) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='furniture',
        name=entity_id,
        position=Position3(x_m=x, y_m=y, z_m=0.5),
        orientation=quaternion_from_euler_deg(
            yaw_deg=yaw_deg, pitch_deg=0.0, roll_deg=0.0
        ),
        size_m=Size3(x_m=0.6, y_m=0.6, z_m=1.0),
        operational_zones=zones,
    )


def test_door_swing_sector_starts_along_local_forward() -> None:
    """Hinge offset places the hinge; the leaf sweeps from local +Y."""
    door = _zone_entity(
        'door', x=1.0, y=1.0, yaw_deg=0.0,
        zones=(OperationalZone(
            zone_id='swing', kind='door_swing',
            hinge_offset_m=(0.3, 0.0), angle_deg=90.0, radius_m=0.8,
        ),),
    )
    footprint = operational_zone_footprint(door, door.operational_zones[0])
    hinge_x, hinge_y = 1.3, 1.0
    # Leaf rest direction is world +Y: sector spans [90 deg, 180 deg] about
    # the hinge. A point 0.5 m out along the leaf lies on the sector edge.
    assert footprint.distance(Point(hinge_x, hinge_y + 0.5)) < 1e-6
    # Angle 135 deg from the hinge is inside the swept quadrant.
    inside = (
        hinge_x + 0.4 * math.cos(math.radians(135.0)),
        hinge_y + 0.4 * math.sin(math.radians(135.0)),
    )
    assert footprint.covers(Point(*inside))
    # Angle 45 deg is the mirror quadrant a hinge-offset direction would
    # produce (atan2 of the offset vector) — it must NOT be swept.
    mirror = (
        hinge_x + 0.4 * math.cos(math.radians(45.0)),
        hinge_y + 0.4 * math.sin(math.radians(45.0)),
    )
    assert not footprint.covers(Point(*mirror))

    # Yaw 180 turns local +Y into world -Y: the sector spans [-90, 0] deg.
    flipped = _zone_entity(
        'door', x=1.0, y=1.0, yaw_deg=180.0,
        zones=(OperationalZone(
            zone_id='swing', kind='door_swing',
            hinge_offset_m=(0.3, 0.0), angle_deg=90.0, radius_m=0.8,
        ),),
    )
    footprint = operational_zone_footprint(flipped, flipped.operational_zones[0])
    # hinge world = position + R_z(180)*(0.3, 0) = (1.0 - 0.3, 1.0) = (0.7, 1.0)
    assert footprint.distance(Point(0.7, 0.5)) < 1e-6
    rotated_inside = (
        0.7 + 0.4 * math.cos(math.radians(-45.0)),
        1.0 + 0.4 * math.sin(math.radians(-45.0)),
    )
    assert footprint.covers(Point(*rotated_inside))
    assert not footprint.covers(Point(*inside))


def test_rotate_zone_sector_follows_entity_orientation() -> None:
    """A partial rotate zone is entity-local, not anchored to world +X."""
    table = _zone_entity(
        'turntable', x=1.0, y=1.0, yaw_deg=0.0,
        zones=(OperationalZone(
            zone_id='rot', kind='rotate', angle_deg=90.0, radius_m=1.0,
        ),),
    )
    footprint = operational_zone_footprint(table, table.operational_zones[0])
    # Forward-anchored sector [90 deg, 180 deg]: NW quadrant of the centre.
    assert footprint.covers(Point(1.0 - 0.35, 1.0 + 0.35))
    assert not footprint.covers(Point(1.0 + 0.35, 1.0 + 0.35))

    rotated = _zone_entity(
        'turntable', x=1.0, y=1.0, yaw_deg=90.0,
        zones=(OperationalZone(
            zone_id='rot', kind='rotate', angle_deg=90.0, radius_m=1.0,
        ),),
    )
    footprint = operational_zone_footprint(rotated, rotated.operational_zones[0])
    # Forward now maps to world -X (180 deg): sector spans [180, 270] = SW.
    assert footprint.covers(Point(1.0 - 0.35, 1.0 - 0.35))
    assert not footprint.covers(Point(1.0 - 0.35, 1.0 + 0.35))

    # A full turn sweeps the disk regardless of orientation.
    full = _zone_entity(
        'turntable', x=1.0, y=1.0, yaw_deg=45.0,
        zones=(OperationalZone(
            zone_id='rot', kind='rotate', angle_deg=360.0, radius_m=1.0,
        ),),
    )
    footprint = operational_zone_footprint(full, full.operational_zones[0])
    assert footprint.covers(Point(1.0 + 0.9, 1.0))
    assert footprint.covers(Point(1.0 - 0.9, 1.0))


# --- Precision & degenerate input ------------------------------------------


def test_scene_round_trip_is_bit_exact() -> None:
    entity = _seat(
        x=0.1 + 0.2,  # 0.30000000000000004 — the awkward float
        y=1.0 / 3.0,
        z=0.55,
        offset=Offset3(x_m=1.0e-7, y_m=-0.3333333333333333, z_m=0.65),
    )
    document = SceneDocument(document_id='doc-geo', room=None, entities=(entity,))
    payload = canonical_scene_json(document)
    reopened = SceneDocument.model_validate_json(payload)
    reopened_entity = reopened.entity('seat-1')
    assert reopened_entity == entity
    assert scene_content_hash(reopened) == scene_content_hash(document)


def test_degenerate_geometry_inputs_fail_closed() -> None:
    with pytest.raises(ValidationError):
        RoomPrism(width_m=0.0, depth_m=8.0, height_m=3.0)
    # Collinear walls carry no area.
    with pytest.raises(ValueError, match='area is too small|Invalid'):
        polygon_from_vertices([(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)])
    with pytest.raises(ValueError, match='duplicate vertices'):
        polygon_from_vertices([(0.0, 0.0), (1.0, 0.0), (1.0, 0.0)])
    with pytest.raises(ValueError, match='closing vertex'):
        polygon_from_vertices([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 0.0)])
    with pytest.raises(ValueError):
        polygon_from_vertices([(0.0, 0.0), (1.0, 0.0)])
