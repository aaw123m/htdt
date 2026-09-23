from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any


ACOUSTICS_ALGORITHM_VERSION = 'rect-room-geometry-1'


@dataclass(frozen=True)
class RoomMode:
    n_x: int
    n_y: int
    n_z: int
    frequency_hz: float
    mode_class: str


@dataclass(frozen=True)
class ReflectionCandidate:
    speaker_id: str
    speaker_role: str
    surface: str
    reflection_point_m: tuple[float, float, float]
    direct_length_m: float
    reflected_length_m: float
    excess_length_m: float
    excess_delay_ms: float
    first_destructive_hz: float | None


def rectangular_room_modes(
    width_m: float,
    depth_m: float,
    height_m: float,
    *,
    max_hz: float = 300.0,
    sound_speed_m_s: float = 343.0,
) -> tuple[RoomMode, ...]:
    dimensions = (width_m, depth_m, height_m)
    if any(value <= 0 for value in dimensions):
        raise ValueError('Room dimensions must be positive')
    if max_hz <= 0 or sound_speed_m_s <= 0:
        raise ValueError('max_hz and sound_speed_m_s must be positive')

    max_indices = tuple(max(1, math.floor((2.0 * max_hz * dimension) / sound_speed_m_s) + 1) for dimension in dimensions)
    modes: list[RoomMode] = []
    for n_x in range(max_indices[0] + 1):
        for n_y in range(max_indices[1] + 1):
            for n_z in range(max_indices[2] + 1):
                if n_x == n_y == n_z == 0:
                    continue
                frequency = (sound_speed_m_s / 2.0) * math.sqrt(
                    (n_x / width_m) ** 2 + (n_y / depth_m) ** 2 + (n_z / height_m) ** 2
                )
                if frequency > max_hz:
                    continue
                nonzero = sum(index > 0 for index in (n_x, n_y, n_z))
                mode_class = {1: 'axial', 2: 'tangential', 3: 'oblique'}[nonzero]
                modes.append(RoomMode(n_x, n_y, n_z, frequency, mode_class))
    modes.sort(key=lambda mode: (mode.frequency_hz, mode.n_x, mode.n_y, mode.n_z))
    return tuple(modes)


def _distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt(sum((left - right) ** 2 for left, right in zip(a, b, strict=True)))


def _mirror(source: tuple[float, float, float], axis: int, plane: float) -> tuple[float, float, float]:
    values = list(source)
    values[axis] = 2.0 * plane - values[axis]
    return values[0], values[1], values[2]


def _reflection_point(
    mirrored_source: tuple[float, float, float],
    receiver: tuple[float, float, float],
    axis: int,
    plane: float,
) -> tuple[float, float, float] | None:
    denominator = receiver[axis] - mirrored_source[axis]
    if abs(denominator) < 1e-12:
        return None
    t = (plane - mirrored_source[axis]) / denominator
    if not (0.0 <= t <= 1.0):
        return None
    return tuple(mirrored_source[index] + t * (receiver[index] - mirrored_source[index]) for index in range(3))  # type: ignore[return-value]


def first_order_reflections(
    width_m: float,
    depth_m: float,
    height_m: float,
    source: tuple[float, float, float],
    receiver: tuple[float, float, float],
    *,
    speaker_id: str,
    speaker_role: str,
    sound_speed_m_s: float = 343.0,
) -> tuple[ReflectionCandidate, ...]:
    dimensions = (width_m, depth_m, height_m)
    if any(value <= 0 for value in dimensions) or sound_speed_m_s <= 0:
        raise ValueError('Room dimensions and sound speed must be positive')
    for label, point in (('source', source), ('receiver', receiver)):
        if any(value < 0 or value > dimensions[index] for index, value in enumerate(point)):
            raise ValueError(f'{label} is outside room bounds')

    direct = _distance(source, receiver)
    surfaces = (
        ('left_x0', 0, 0.0),
        ('right_xW', 0, width_m),
        ('front_y0', 1, 0.0),
        ('rear_yD', 1, depth_m),
        ('floor_z0', 2, 0.0),
        ('ceiling_zH', 2, height_m),
    )
    results: list[ReflectionCandidate] = []
    for surface, axis, plane in surfaces:
        mirrored = _mirror(source, axis, plane)
        point = _reflection_point(mirrored, receiver, axis, plane)
        if point is None:
            continue
        if any(value < -1e-9 or value > dimensions[index] + 1e-9 for index, value in enumerate(point)):
            continue
        reflected = _distance(mirrored, receiver)
        excess = max(0.0, reflected - direct)
        first_destructive = sound_speed_m_s / (2.0 * excess) if excess > 1e-12 else None
        results.append(
            ReflectionCandidate(
                speaker_id=speaker_id,
                speaker_role=speaker_role,
                surface=surface,
                reflection_point_m=point,
                direct_length_m=direct,
                reflected_length_m=reflected,
                excess_length_m=excess,
                excess_delay_ms=(excess / sound_speed_m_s) * 1000.0,
                first_destructive_hz=first_destructive,
            )
        )
    return tuple(results)


def analyze_rectangular_context(
    payload: dict[str, Any],
    *,
    max_hz: float = 300.0,
    sound_speed_m_s: float = 343.0,
) -> dict[str, Any]:
    room = payload['room']
    if room.get('geometry_kind', 'rectangular') != 'rectangular':
        raise ValueError('Acoustic geometry analysis requires a rectangular room')
    width = float(room['width_m'])
    depth = float(room['depth_m'])
    height = float(room['height_m'])
    point = payload['measurement_point']['position']
    receiver = (float(point['x_m']), float(point['y_m']), float(point['z_m']))

    reflections: list[dict[str, Any]] = []
    skipped_speakers: list[str] = []
    for speaker in payload.get('speakers', []):
        position = speaker.get('position')
        if position is None:
            skipped_speakers.append(str(speaker.get('speaker_id', 'unknown')))
            continue
        source = (float(position['x_m']), float(position['y_m']), float(position['z_m']))
        reflections.extend(
            asdict(candidate)
            for candidate in first_order_reflections(
                width,
                depth,
                height,
                source,
                receiver,
                speaker_id=str(speaker['speaker_id']),
                speaker_role=str(speaker['role']),
                sound_speed_m_s=sound_speed_m_s,
            )
        )

    return {
        'classification': 'predicted_geometry_candidate',
        'algorithm_version': ACOUSTICS_ALGORITHM_VERSION,
        'assumptions': [
            'rectangular_room',
            'point_source_geometry',
            'specular_first_order_reflection',
            'reflection_amplitude_and_phase_not_modelled',
            'candidate_frequencies_are_not_measured_diagnoses',
        ],
        'sound_speed_m_s': sound_speed_m_s,
        'max_mode_hz': max_hz,
        'room_modes': [asdict(mode) for mode in rectangular_room_modes(width, depth, height, max_hz=max_hz, sound_speed_m_s=sound_speed_m_s)],
        'first_order_reflections': reflections,
        'skipped_speaker_ids': skipped_speakers,
    }
