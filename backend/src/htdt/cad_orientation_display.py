from __future__ import annotations

from dataclasses import dataclass
from math import acos, asin, atan2, cos, degrees, isfinite, radians, sin, sqrt

from .cad_objects import orientation_aligning_forward
from .cad_scene import (
    Direction3,
    Quaternion4,
    SceneEntity,
    quaternion_from_axis_angle_vector,
    quaternion_multiply,
    quaternion_to_matrix3,
)


class OrientationDisplayError(ValueError):
    pass


_VERTICAL_EPS = 1e-6


@dataclass(frozen=True, slots=True)
class BodyViewAngles:
    """Installation-facing display angles for a persisted body quaternion.

    These are NOT the persisted Z-Y-X Euler parameterization; they are a
    view-model contract shared with the acoustic-aim fields so that "the same
    direction" reads as the same numbers in both blocks:

    - ``heading_deg``: horizontal heading of the cabinet front (local +Y),
      measured from +Y (room rear) toward +X (room right) — identical to the
      aim yaw convention. ``None`` when the front axis is near-vertical and no
      horizontal heading exists; never silently reported as 0.
    - ``elevation_deg``: elevation of the cabinet front above the horizontal
      plane — identical to the aim pitch convention.
    - ``twist_deg``: roll about the cabinet's own front axis. Zero is the
      canonical upright pose produced by
      :func:`orientation_aligning_forward` (cabinet top as close to world +Z
      as the forward direction allows).
    """

    heading_deg: float | None
    elevation_deg: float
    twist_deg: float


def _forward_from_orientation(orientation: Quaternion4) -> tuple[float, float, float]:
    matrix = quaternion_to_matrix3(orientation)
    # Local +Y is the authored cabinet front axis.
    return (matrix[0][1], matrix[1][1], matrix[2][1])


def body_forward_direction(orientation: Quaternion4) -> Direction3:
    """World-space cabinet front (local +Y) as a normalized direction."""

    fx, fy, fz = _forward_from_orientation(orientation)
    length = sqrt(fx * fx + fy * fy + fz * fz)
    return Direction3(x=fx / length, y=fy / length, z=fz / length)


def body_view_angles(orientation: Quaternion4) -> BodyViewAngles:
    """Decompose a persisted quaternion into installation display angles.

    Reading an arbitrary persisted pose is a pure view-model operation: it
    never rewrites the quaternion, so SceneRevision hashes are untouched.
    """

    fx, fy, fz = _forward_from_orientation(orientation)
    elevation = degrees(asin(max(-1.0, min(1.0, fz))))
    horizontal = sqrt(fx * fx + fy * fy)
    heading = None if horizontal <= _VERTICAL_EPS else degrees(atan2(fx, fy))

    # Twist = residual rotation about the forward axis relative to the
    # canonical upright aligned pose. residual = q_align⁻¹ · q is a pure
    # local-+Y rotation because both poses share the same forward axis.
    length = sqrt(fx * fx + fy * fy + fz * fz)
    aligned = orientation_aligning_forward(
        Direction3(x=fx / length, y=fy / length, z=fz / length)
    )
    conjugate = Quaternion4(w=aligned.w, x=-aligned.x, y=-aligned.y, z=-aligned.z)
    residual = quaternion_multiply(conjugate, orientation)
    # Quaternion sign is ambiguous (q == -q); canonicalize to w >= 0 so the
    # reported twist stays inside (-180, 180].
    rw = residual.w if residual.w >= 0.0 else -residual.w
    ry = residual.y if residual.w >= 0.0 else -residual.y
    twist = degrees(2.0 * atan2(ry, rw))
    if twist > 180.0:
        twist -= 360.0
    elif twist <= -180.0:
        twist += 360.0
    if not isfinite(twist):
        twist = 0.0
    return BodyViewAngles(
        heading_deg=heading,
        elevation_deg=elevation,
        twist_deg=twist,
    )


def orientation_from_view_angles(
    *,
    heading_deg: float | None,
    elevation_deg: float,
    twist_deg: float,
    fallback_heading_deg: float | None = None,
) -> Quaternion4:
    """Build the persisted quaternion for edited installation display angles.

    ``heading_deg`` is required unless the elevation is effectively vertical;
    in the vertical case the previous heading (``fallback_heading_deg``) is
    reused so editing elevation cannot invent a horizontal heading. Callers
    must pass the entity's current heading as the fallback.
    """

    elevation = float(elevation_deg)
    twist = float(twist_deg)
    if not isfinite(elevation) or not isfinite(twist):
        raise OrientationDisplayError('elevation/twist must be finite')
    if elevation < -90.0 or elevation > 90.0:
        raise OrientationDisplayError('elevation must stay within [-90, 90] degrees')

    heading = heading_deg
    if heading is None:
        if abs(elevation) >= 90.0 - _VERTICAL_EPS:
            heading = fallback_heading_deg
        if heading is None:
            raise OrientationDisplayError(
                'a near-vertical forward axis has no horizontal heading; '
                'keep the current heading or set one explicitly'
            )
    heading = float(heading)
    if not isfinite(heading):
        raise OrientationDisplayError('heading must be finite')

    yaw = radians(heading)
    pitch = radians(elevation)
    forward = (sin(yaw) * cos(pitch), cos(yaw) * cos(pitch), sin(pitch))
    aligned = orientation_aligning_forward(
        Direction3(x=forward[0], y=forward[1], z=forward[2])
    )
    twist_rotation = quaternion_from_axis_angle_vector(forward, twist)
    return quaternion_multiply(twist_rotation, aligned)


def forward_aim_delta_deg(entity: SceneEntity) -> float | None:
    """Angle between cabinet front and acoustic aim, or None when aim unknown.

    The comparison is derived from world-space directions, never from the
    differently-parameterized numeric fields: matching directions report 0
    even though the persisted yaw conventions carry opposite signs.
    """

    if entity.aim_xyz is None:
        return None
    fx, fy, fz = _forward_from_orientation(entity.orientation)
    dot = fx * entity.aim_xyz.x + fy * entity.aim_xyz.y + fz * entity.aim_xyz.z
    return degrees(acos(max(-1.0, min(1.0, dot))))


__all__ = [
    'BodyViewAngles',
    'OrientationDisplayError',
    'body_forward_direction',
    'body_view_angles',
    'forward_aim_delta_deg',
    'orientation_from_view_angles',
]
