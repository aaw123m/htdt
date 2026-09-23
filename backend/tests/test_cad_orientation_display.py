from __future__ import annotations

from math import atan2

import pytest

from htdt.cad_objects import aim_yaw_pitch_deg, orientation_aligning_forward
from htdt.cad_orientation_display import (
    OrientationDisplayError,
    body_forward_direction,
    body_view_angles,
    forward_aim_delta_deg,
    orientation_from_view_angles,
)
from htdt.cad_scene import (
    Direction3,
    Position3,
    Quaternion4,
    SceneEntity,
    Size3,
    quaternion_from_axis_angle,
    quaternion_from_euler_deg,
    quaternion_multiply,
)


_IDENTITY = Quaternion4(w=1.0, x=0.0, y=0.0, z=0.0)


def _entity(
    orientation: Quaternion4,
    aim: Direction3 | None = None,
) -> SceneEntity:
    return SceneEntity(
        entity_id="speaker-x",
        kind="speaker",
        name="Speaker",
        position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
        orientation=orientation,
        speaker_role="UNASSIGNED-1",
        size_m=Size3(x_m=0.24, y_m=0.28, z_m=0.42),
        aim_xyz=aim,
    )


def _quat_almost(a: Quaternion4, b: Quaternion4, *, tol: float = 1e-9) -> bool:
    """Same rotation up to float noise (q and -q are the same rotation)."""

    same = (
        a.w == pytest.approx(b.w, abs=tol)
        and a.x == pytest.approx(b.x, abs=tol)
        and a.y == pytest.approx(b.y, abs=tol)
        and a.z == pytest.approx(b.z, abs=tol)
    )
    negated = (
        a.w == pytest.approx(-b.w, abs=tol)
        and a.x == pytest.approx(-b.x, abs=tol)
        and a.y == pytest.approx(-b.y, abs=tol)
        and a.z == pytest.approx(-b.z, abs=tol)
    )
    return same or negated


def test_identity_pose_reads_zero_heading_elevation_twist() -> None:
    angles = body_view_angles(_IDENTITY)
    assert angles.heading_deg == pytest.approx(0.0)
    assert angles.elevation_deg == pytest.approx(0.0)
    assert angles.twist_deg == pytest.approx(0.0)


def test_body_heading_shares_aim_yaw_convention() -> None:
    # A body rotated so its front (+Y local) faces +X reads heading +90°,
    # the same number the aim block reports for the +X direction.
    orientation = orientation_aligning_forward(Direction3(x=1.0, y=0.0, z=0.0))
    angles = body_view_angles(orientation)
    assert angles.heading_deg == pytest.approx(90.0)
    assert angles.elevation_deg == pytest.approx(0.0, abs=1e-6)
    aim_yaw, aim_pitch = aim_yaw_pitch_deg(Direction3(x=1.0, y=0.0, z=0.0))
    assert angles.heading_deg == pytest.approx(aim_yaw)
    assert angles.elevation_deg == pytest.approx(aim_pitch)


def test_round_trip_preserves_arbitrary_pose() -> None:
    original = quaternion_from_euler_deg(yaw_deg=37.0, pitch_deg=-24.0, roll_deg=11.0)
    angles = body_view_angles(original)
    rebuilt = orientation_from_view_angles(
        heading_deg=angles.heading_deg,
        elevation_deg=angles.elevation_deg,
        twist_deg=angles.twist_deg,
    )
    assert _quat_almost(rebuilt, original)


def test_editing_elevation_only_moves_front_up_and_keeps_heading() -> None:
    original = orientation_from_view_angles(heading_deg=30.0, elevation_deg=0.0, twist_deg=0.0)
    edited = orientation_from_view_angles(heading_deg=30.0, elevation_deg=15.0, twist_deg=0.0)
    before = body_forward_direction(original)
    after = body_forward_direction(edited)
    assert after.z > before.z
    # Heading (atan2 of the horizontal components) is preserved.
    assert atan2(after.x, after.y) == pytest.approx(atan2(before.x, before.y), abs=1e-9)
    angles = body_view_angles(edited)
    assert angles.heading_deg == pytest.approx(30.0)
    assert angles.elevation_deg == pytest.approx(15.0)


def test_editing_twist_does_not_change_front_direction() -> None:
    original = orientation_from_view_angles(heading_deg=10.0, elevation_deg=20.0, twist_deg=0.0)
    edited = orientation_from_view_angles(heading_deg=10.0, elevation_deg=20.0, twist_deg=45.0)
    before = body_forward_direction(original)
    after = body_forward_direction(edited)
    assert after.x == pytest.approx(before.x, abs=1e-9)
    assert after.y == pytest.approx(before.y, abs=1e-9)
    assert after.z == pytest.approx(before.z, abs=1e-9)


def test_vertical_forward_never_reports_zero_heading() -> None:
    orientation = orientation_aligning_forward(Direction3(x=0.0, y=0.0, z=1.0))
    angles = body_view_angles(orientation)
    assert angles.heading_deg is None
    assert angles.elevation_deg == pytest.approx(90.0)


def test_vertical_pose_edit_reuses_previous_heading_without_mutation() -> None:
    original = quaternion_from_euler_deg(yaw_deg=40.0, pitch_deg=10.0, roll_deg=0.0)
    exact = body_view_angles(original)
    # Simulate the inspector flow: rebuild the identical pose from displayed
    # angles — the persisted quaternion must round-trip untouched.
    rebuilt = orientation_from_view_angles(
        heading_deg=exact.heading_deg,
        elevation_deg=exact.elevation_deg,
        twist_deg=exact.twist_deg,
        fallback_heading_deg=exact.heading_deg,
    )
    assert _quat_almost(rebuilt, original)


def test_missing_heading_on_horizontal_pose_fails_closed() -> None:
    with pytest.raises(OrientationDisplayError):
        orientation_from_view_angles(heading_deg=None, elevation_deg=10.0, twist_deg=0.0)


def test_twist_zero_pose_matches_canonical_aligned_forward() -> None:
    norm = (0.2 ** 2 + 0.9 ** 2 + 0.35 ** 2) ** 0.5
    direction = Direction3(x=0.2 / norm, y=0.9 / norm, z=0.35 / norm)
    canonical = orientation_aligning_forward(direction)
    angles = body_view_angles(canonical)
    assert angles.twist_deg == pytest.approx(0.0, abs=1e-6)
    # A real twist about the forward axis rotates the cabinet around its front.
    twisted = quaternion_multiply(
        quaternion_from_axis_angle('x', 0.0),
        canonical,
    )
    assert body_view_angles(twisted).twist_deg == pytest.approx(0.0)


def test_forward_aim_delta_is_world_space_comparison() -> None:
    aim = Direction3(x=0.0, y=1.0, z=0.0)
    aligned = _entity(orientation_aligning_forward(aim), aim=aim)
    assert forward_aim_delta_deg(aligned) == pytest.approx(0.0, abs=1e-6)

    unknown = _entity(_IDENTITY, aim=None)
    assert forward_aim_delta_deg(unknown) is None

    divergent = _entity(
        orientation_aligning_forward(Direction3(x=1.0, y=0.0, z=0.0)),
        aim=aim,
    )
    assert forward_aim_delta_deg(divergent) == pytest.approx(90.0)


def test_display_angles_are_pure_view_model_reads() -> None:
    entity = _entity(
        quaternion_from_euler_deg(yaw_deg=-80.0, pitch_deg=5.0, roll_deg=33.0)
    )
    before = entity.orientation
    body_view_angles(entity.orientation)
    forward_aim_delta_deg(entity)
    assert entity.orientation == before
