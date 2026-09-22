from __future__ import annotations

from math import asin, atan2, cos, degrees, isfinite, radians, sin, sqrt

from .cad_scene import (
    Direction3,
    Quaternion4,
    SceneDocument,
    SceneEntity,
    acoustic_reference_position,
    quaternion_from_euler_deg,
)


class TheaterObjectError(ValueError):
    pass


# Entities a speaker may be acoustically aimed at: anything that exposes an
# authored acoustic reference position (seats carry an ear-height offset;
# measurement points are already world reference positions).
AIM_TARGET_KINDS = frozenset({'seat', 'measurement_point'})


def aim_target_entities(document: SceneDocument) -> tuple[SceneEntity, ...]:
    """Entities the inspector may offer as acoustic-aim targets."""

    return tuple(entity for entity in document.entities if entity.kind in AIM_TARGET_KINDS)


def aim_yaw_pitch_deg(direction: Direction3) -> tuple[float, float]:
    """Return (aim yaw, aim pitch) in degrees for a known acoustic aim.

    Convention matches the O80/O90 search axes:
    - yaw is the horizontal heading in degrees measured from +Y (room rear)
      toward +X (room right), i.e. ``degrees(atan2(x, y))``;
    - pitch is elevation above the horizontal plane, ``degrees(asin(z))``.

    A vertical-only aim has no horizontal heading; yaw is reported as 0.0 for
    display purposes instead of raising (unlike the strict search helper).
    """

    horizontal = sqrt(direction.x * direction.x + direction.y * direction.y)
    yaw = 0.0 if horizontal <= 1e-9 else degrees(atan2(direction.x, direction.y))
    pitch = degrees(asin(max(-1.0, min(1.0, direction.z))))
    return yaw, pitch


def direction_from_yaw_pitch_deg(yaw_deg: float, pitch_deg: float) -> Direction3:
    """Build a normalized acoustic aim from aim yaw/pitch degrees.

    Inverse of :func:`aim_yaw_pitch_deg` and equivalent to the O80/O90
    ``aim_yaw_deg``/``aim_pitch_deg`` parameterization.
    """

    yaw_deg = float(yaw_deg)
    pitch_deg = float(pitch_deg)
    if not isfinite(yaw_deg) or not isfinite(pitch_deg):
        raise TheaterObjectError('aim yaw/pitch must be finite')
    if pitch_deg < -90.0 or pitch_deg > 90.0:
        raise TheaterObjectError('aim pitch must stay within [-90, 90] degrees')
    yaw = radians(yaw_deg)
    pitch = radians(pitch_deg)
    horizontal = cos(pitch)
    return Direction3(
        x=horizontal * sin(yaw),
        y=horizontal * cos(yaw),
        z=sin(pitch),
    )


def orientation_aligning_forward(direction: Direction3) -> Quaternion4:
    """Body orientation whose local +Y (cabinet front face) points along ``direction``.

    The upright canonical pose has zero twist about the forward axis: euler
    yaw = atan2(-x, y) turns the +Y front toward the horizontal heading, and
    euler roll = asin(z) tilts the front up/down while keeping the cabinet top
    as close to world +Z as possible.
    """

    yaw_deg = degrees(atan2(-direction.x, direction.y))
    roll_deg = degrees(asin(max(-1.0, min(1.0, direction.z))))
    return quaternion_from_euler_deg(yaw_deg=yaw_deg, pitch_deg=0.0, roll_deg=roll_deg)


def speaker_aim_replacements(
    document: SceneDocument,
    speaker_ids: tuple[str, ...],
    target_id: str,
) -> tuple[SceneEntity, ...]:
    """Return speaker replacements aimed at a seat/measurement acoustic reference.

    Body pose is intentionally untouched. The function only resolves explicit
    acoustic references into world-space normalized ``aim_xyz`` values.
    """

    if not speaker_ids:
        return ()
    if len(speaker_ids) != len(set(speaker_ids)):
        raise TheaterObjectError('speaker ids must be unique')

    target = document.entity(target_id)
    if target.kind not in {'seat', 'measurement_point'}:
        raise TheaterObjectError('aim target must be a seat or measurement point')
    target_position = acoustic_reference_position(target)
    if target_position is None:
        raise TheaterObjectError('aim target has no acoustic reference position')

    replacements: list[SceneEntity] = []
    for speaker_id in speaker_ids:
        speaker = document.entity(speaker_id)
        if speaker.kind != 'speaker':
            raise TheaterObjectError(f'aim source is not a speaker: {speaker_id}')
        source_position = acoustic_reference_position(speaker) or speaker.position
        delta = (
            target_position.x_m - source_position.x_m,
            target_position.y_m - source_position.y_m,
            target_position.z_m - source_position.z_m,
        )
        length = sqrt(sum(value * value for value in delta))
        if length <= 1e-12:
            raise TheaterObjectError(f'speaker acoustic reference overlaps target: {speaker_id}')
        aim = Direction3(
            x=delta[0] / length,
            y=delta[1] / length,
            z=delta[2] / length,
        )
        replacements.append(speaker.model_copy(update={'aim_xyz': aim}))
    return tuple(replacements)
