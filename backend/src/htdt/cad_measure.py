"""Ephemeral Room measurement authority (#491).

Pure geometry math for the ruler/measure tool: distance (3D + horizontal),
per-axis components, azimuth/elevation following the canonical
``aim_yaw_pitch_deg`` convention (+Y rear = 0°, +X right = +90°), and
three-point vertex angle. Results are display-only — measurement NEVER
mutates ``SceneDocument``, produces constraints, or leaves Undo steps.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import acos, asin, atan2, degrees, sqrt

from .cad_scene import Position3


#: Measurement endpoint/reference choice — the user must see which semantic
#: point was measured rather than an anonymous ray hit.
MEASURE_REFERENCE_KINDS = ('position', 'acoustic_reference', 'snap_point', 'free_point')

MEASURE_REFERENCE_LABELS: dict[str, str] = {
    'position': '基準位置（本体原点）',
    'acoustic_reference': '音響基準位置',
    'snap_point': 'スナップ点（頂点/中点/辺）',
    'free_point': '自由点',
}

MEASURE_MODES = ('distance', 'angle')

MEASURE_MODE_LABELS: dict[str, str] = {
    'distance': '距離（2点）',
    'angle': '角度（3点）',
}


@dataclass(frozen=True)
class MeasureEndpoint:
    """One resolved measurement endpoint with its semantic provenance."""

    position: Position3
    entity_id: str | None
    entity_name: str | None
    reference_kind: str  # one of MEASURE_REFERENCE_KINDS
    reference_label: str

    def describe(self) -> str:
        base = self.entity_name if self.entity_name is not None else '（自由点）'
        return f'{base} · {self.reference_label}'


@dataclass(frozen=True)
class MeasureResult:
    """Resolved measurement display data. Never touches SceneDocument."""

    mode: str  # one of MEASURE_MODES
    endpoints: tuple[MeasureEndpoint, ...]
    distance_m: float | None = None
    dx_m: float | None = None
    dy_m: float | None = None
    dz_m: float | None = None
    horizontal_m: float | None = None
    azimuth_deg: float | None = None
    elevation_deg: float | None = None
    angle_deg: float | None = None


def measure_components(start: Position3, end: Position3) -> MeasureResult:
    """Distance + components + azimuth/elevation between two endpoints.

    Azimuth uses the canonical aim convention (0° = +Y toward room rear,
    positive toward +X right); elevation is degrees above horizontal.
    """

    dx = end.x_m - start.x_m
    dy = end.y_m - start.y_m
    dz = end.z_m - start.z_m
    distance = sqrt(dx * dx + dy * dy + dz * dz)
    horizontal = sqrt(dx * dx + dy * dy)
    azimuth = 0.0 if horizontal <= 1e-9 else degrees(atan2(dx, dy))
    elevation = degrees(asin(max(-1.0, min(1.0, dz / distance)))) if distance > 1e-9 else 0.0
    return MeasureResult(
        mode='distance',
        endpoints=(),
        distance_m=distance,
        dx_m=dx,
        dy_m=dy,
        dz_m=dz,
        horizontal_m=horizontal,
        azimuth_deg=azimuth,
        elevation_deg=elevation,
    )


def measure_angle_deg(a: Position3, vertex: Position3, b: Position3) -> float:
    """Angle A-V-B at the vertex in degrees (0–180)."""

    ax, ay, az = a.x_m - vertex.x_m, a.y_m - vertex.y_m, a.z_m - vertex.z_m
    bx, by, bz = b.x_m - vertex.x_m, b.y_m - vertex.y_m, b.z_m - vertex.z_m
    norm_a = sqrt(ax * ax + ay * ay + az * az)
    norm_b = sqrt(bx * bx + by * by + bz * bz)
    if norm_a <= 1e-9 or norm_b <= 1e-9:
        raise ValueError('angle measurement requires distinct endpoints')
    cosine = (ax * bx + ay * by + az * bz) / (norm_a * norm_b)
    cosine = max(-1.0, min(1.0, cosine))
    return degrees(acos(cosine))


def build_distance_result(a: MeasureEndpoint, b: MeasureEndpoint) -> MeasureResult:
    result = measure_components(a.position, b.position)
    return MeasureResult(
        mode='distance',
        endpoints=(a, b),
        distance_m=result.distance_m,
        dx_m=result.dx_m,
        dy_m=result.dy_m,
        dz_m=result.dz_m,
        horizontal_m=result.horizontal_m,
        azimuth_deg=result.azimuth_deg,
        elevation_deg=result.elevation_deg,
    )


def build_angle_result(a: MeasureEndpoint, vertex: MeasureEndpoint, b: MeasureEndpoint) -> MeasureResult:
    return MeasureResult(
        mode='angle',
        endpoints=(a, vertex, b),
        angle_deg=measure_angle_deg(a.position, vertex.position, b.position),
    )


def format_measure_result(result: MeasureResult) -> str:
    """Single-line copyable text form of a measurement."""

    if result.mode == 'angle':
        assert result.angle_deg is not None
        return f'角度 {result.angle_deg:.1f}°'
    assert result.distance_m is not None
    return (
        f'距離 {result.distance_m:.3f} m '
        f'（ΔX {result.dx_m:+.3f} / ΔY {result.dy_m:+.3f} / ΔZ {result.dz_m:+.3f} m, '
        f'水平 {result.horizontal_m:.3f} m, 方位 {result.azimuth_deg:+.1f}°, '
        f'仰角 {result.elevation_deg:+.1f}°）'
    )
