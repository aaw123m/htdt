from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from itertools import chain
from math import cos, floor, hypot, isfinite, pi, sin
from typing import Callable, Literal

from .cad_scene import Position3, SceneDocument, SceneEntity, quaternion_to_matrix3

AxisName = Literal['x', 'y', 'z']
SnapKind = Literal['vertex', 'midpoint', 'edge', 'alignment']
ScreenProjector = Callable[[Position3], tuple[float, float]]

SNAP_PRIORITY: dict[SnapKind, int] = {
    'vertex': 0,
    'midpoint': 1,
    'edge': 2,
    'alignment': 3,
}
ALL_SNAP_KINDS: frozenset[SnapKind] = frozenset(SNAP_PRIORITY)

_PROJECTION_SENTINELS: tuple[Position3, ...] = (
    Position3(x_m=0.0, y_m=0.0, z_m=0.0),
    Position3(x_m=1.0, y_m=0.0, z_m=0.0),
    Position3(x_m=0.0, y_m=1.0, z_m=0.0),
    Position3(x_m=0.0, y_m=0.0, z_m=1.0),
)
_BOX_EDGE_INDEX_PAIRS: tuple[tuple[int, int], ...] = tuple(
    (left, right)
    for left in range(8)
    for right in range(left + 1, 8)
    if (left ^ right) in (1, 2, 4)
)
# Compass samples on cylinder rims for vertex/edge snapping (issue #464).
_CYLINDER_SNAP_SEGMENTS = 8


def _prism_edge_index_pairs(ring_size: int) -> tuple[tuple[int, int], ...]:
    """Bottom ring, top ring, then vertical edges for an extruded footprint."""

    return tuple(chain(
        ((index, (index + 1) % ring_size) for index in range(ring_size)),
        ((ring_size + index, ring_size + (index + 1) % ring_size) for index in range(ring_size)),
        ((index, ring_size + index) for index in range(ring_size)),
    ))


@dataclass(frozen=True)
class SnapCandidate:
    stable_id: str
    kind: SnapKind
    entity_id: str
    target: Position3
    screen_anchor: Position3
    axis: AxisName
    label: str

    @property
    def priority(self) -> int:
        return SNAP_PRIORITY[self.kind]


@dataclass(frozen=True)
class SnapSelection:
    candidate: SnapCandidate
    distance_dip: float


@dataclass(frozen=True)
class _EdgeGeometry:
    start: Position3
    end: Position3
    midpoint: Position3
    dx: float
    dy: float
    dz: float
    length2: float


class SnapSelector:
    def __init__(self, *, acquire_radius_dip: float = 8.0, retain_radius_dip: float = 12.0) -> None:
        if acquire_radius_dip <= 0 or retain_radius_dip < acquire_radius_dip:
            raise ValueError('snap radii must satisfy 0 < acquire <= retain')
        self.acquire_radius_dip = float(acquire_radius_dip)
        self.retain_radius_dip = float(retain_radius_dip)
        self._retained_id: str | None = None
        self._projection_cache: dict[
            str,
            tuple[tuple[float, float, float], tuple[float, float]],
        ] = {}
        self._projection_signature: tuple[float, ...] | None = None

    @property
    def retained_id(self) -> str | None:
        return self._retained_id

    def reset(self) -> None:
        self._retained_id = None
        self._projection_cache.clear()
        self._projection_signature = None

    def _sync_projection_cache(self, project: ScreenProjector) -> None:
        signature = tuple(
            round(value, 6)
            for point in _PROJECTION_SENTINELS
            for value in project(point)
        )
        if signature != self._projection_signature:
            self._projection_cache.clear()
            self._projection_signature = signature

    def _project_candidate(self, candidate: SnapCandidate, project: ScreenProjector) -> tuple[float, float]:
        anchor_key = (
            candidate.screen_anchor.x_m,
            candidate.screen_anchor.y_m,
            candidate.screen_anchor.z_m,
        )
        cached = self._projection_cache.get(candidate.stable_id)
        if cached is not None and cached[0] == anchor_key:
            return cached[1]
        projected = project(candidate.screen_anchor)
        self._projection_cache[candidate.stable_id] = (anchor_key, projected)
        return projected

    def select(
        self,
        candidates: tuple[SnapCandidate, ...],
        probe: Position3,
        project: ScreenProjector,
    ) -> SnapSelection | None:
        self._sync_projection_cache(project)
        probe_screen = project(probe)

        if self._retained_id is not None:
            retained = next(
                (candidate for candidate in candidates if candidate.stable_id == self._retained_id),
                None,
            )
            if retained is not None:
                distance = _screen_distance(self._project_candidate(retained, project), probe_screen)
                if distance <= self.retain_radius_dip:
                    return SnapSelection(retained, distance)

        best: tuple[tuple[int, float, str], SnapCandidate, float] | None = None
        for candidate in candidates:
            distance = _screen_distance(self._project_candidate(candidate, project), probe_screen)
            if distance > self.acquire_radius_dip:
                continue
            key = (candidate.priority, distance, candidate.stable_id)
            if best is None or key < best[0]:
                best = (key, candidate, distance)
        if best is None:
            self._retained_id = None
            return None
        _, candidate, distance = best
        self._retained_id = candidate.stable_id
        return SnapSelection(candidate, distance)


def _screen_distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return hypot(a[0] - b[0], a[1] - b[1])


def _validate_step(step: float, *, label: str) -> float:
    step = float(step)
    if not isfinite(step) or step <= 0.0:
        raise ValueError(f'{label} must be a finite positive value')
    return step


def snap_scalar(value: float, step: float) -> float:
    """Round to the nearest step, with exact halves away from zero."""
    step = _validate_step(step, label='snap step')
    value = float(value)
    if not isfinite(value):
        raise ValueError('snap value must be finite')
    sign = -1.0 if value < 0.0 else 1.0
    return sign * floor(abs(value) / step + 0.5) * step


def _position(x_m: float, y_m: float, z_m: float) -> Position3:
    """Construct from already validated finite scene values in the snap hot path."""
    return Position3.model_construct(x_m=float(x_m), y_m=float(y_m), z_m=float(z_m))


def snap_position_axis(position: Position3, axis: AxisName, step_m: float) -> Position3:
    step_m = _validate_step(step_m, label='grid step')
    values = {'x': position.x_m, 'y': position.y_m, 'z': position.z_m}
    if axis not in values:
        raise ValueError(f'unsupported snap axis: {axis}')
    values[axis] = snap_scalar(values[axis], step_m)
    return _position(values['x'], values['y'], values['z'])


def snap_angle_deg(angle_deg: float, step_deg: float) -> float:
    step_deg = _validate_step(step_deg, label='angle step')
    return snap_scalar(float(angle_deg), step_deg)


def _axis_value(position: Position3, axis: AxisName) -> float:
    return {'x': position.x_m, 'y': position.y_m, 'z': position.z_m}[axis]


def _with_axis(position: Position3, axis: AxisName, value: float) -> Position3:
    values = {'x': position.x_m, 'y': position.y_m, 'z': position.z_m}
    values[axis] = float(value)
    return _position(values['x'], values['y'], values['z'])


@lru_cache(maxsize=2048)
def _entity_vertices(entity: SceneEntity) -> tuple[Position3, ...]:
    """Snap vertices from the authored body geometry where one exists.

    Issue #464: cylinder bodies snap on their rim compass points, polygon
    extrusions on each footprint corner at both Z extremes, and mesh assets on
    their local-transformed vertices; box/legacy bodies keep the eight
    bounding-envelope corners.
    """

    if entity.size_m is None:
        return (entity.position,)
    matrix = quaternion_to_matrix3(entity.orientation)
    half_z = entity.size_m.z_m / 2.0
    body = entity.body_geometry
    locals_: list[tuple[float, float, float]]
    if body is not None and body.kind == 'cylinder' and body.radius_m is not None:
        radius = float(body.radius_m)
        locals_ = [
            (
                radius * cos(2.0 * pi * index / _CYLINDER_SNAP_SEGMENTS),
                radius * sin(2.0 * pi * index / _CYLINDER_SNAP_SEGMENTS),
                sz * half_z,
            )
            for sz in (-1.0, 1.0)
            for index in range(_CYLINDER_SNAP_SEGMENTS)
        ]
    elif (
        body is not None
        and body.kind == 'extruded_polygon'
        and body.footprint_vertices
    ):
        locals_ = [
            (float(vertex.x_m), float(vertex.y_m), sz * half_z)
            for sz in (-1.0, 1.0)
            for vertex in body.footprint_vertices
        ]
    elif body is not None and body.kind == 'mesh_asset' and body.mesh is not None:
        mesh = body.mesh
        scale = float(mesh.uniform_scale)
        offset = mesh.local_offset_m
        locals_ = [
            (
                float(vertex.x_m) * scale + offset.x_m,
                float(vertex.y_m) * scale + offset.y_m,
                float(vertex.z_m) * scale + offset.z_m,
            )
            for vertex in mesh.vertices
        ]
    else:
        half = (entity.size_m.x_m / 2.0, entity.size_m.y_m / 2.0, half_z)
        locals_ = [
            (sx * half[0], sy * half[1], sz * half[2])
            for sx in (-1.0, 1.0)
            for sy in (-1.0, 1.0)
            for sz in (-1.0, 1.0)
        ]
    result: list[Position3] = []
    for local in locals_:
        rotated = tuple(
            sum(matrix[row][col] * local[col] for col in range(3))
            for row in range(3)
        )
        result.append(_position(
            entity.position.x_m + rotated[0],
            entity.position.y_m + rotated[1],
            entity.position.z_m + rotated[2],
        ))
    return tuple(result)


@lru_cache(maxsize=2048)
def _entity_edges(entity: SceneEntity) -> tuple[_EdgeGeometry, ...]:
    if entity.size_m is None:
        return ()
    body = entity.body_geometry
    if body is None or body.kind == 'box':
        pairs = _BOX_EDGE_INDEX_PAIRS
    elif body.kind == 'cylinder':
        pairs = _prism_edge_index_pairs(_CYLINDER_SNAP_SEGMENTS)
    elif body.kind == 'extruded_polygon' and body.footprint_vertices:
        pairs = _prism_edge_index_pairs(len(body.footprint_vertices))
    else:
        # Imported meshes can carry thousands of edges; snapping keeps vertex
        # and alignment candidates only.
        return ()
    vertices = _entity_vertices(entity)
    result: list[_EdgeGeometry] = []
    for left, right in pairs:
        if left >= len(vertices) or right >= len(vertices):
            return ()
        start, end = vertices[left], vertices[right]
        dx = end.x_m - start.x_m
        dy = end.y_m - start.y_m
        dz = end.z_m - start.z_m
        result.append(_EdgeGeometry(
            start=start,
            end=end,
            midpoint=_position(
                (start.x_m + end.x_m) / 2.0,
                (start.y_m + end.y_m) / 2.0,
                (start.z_m + end.z_m) / 2.0,
            ),
            dx=dx,
            dy=dy,
            dz=dz,
            length2=dx * dx + dy * dy + dz * dz,
        ))
    return tuple(result)


def _closest_point_on_edge(probe: Position3, edge: _EdgeGeometry) -> Position3:
    if edge.length2 <= 1e-18:
        return edge.start
    t = (
        (probe.x_m - edge.start.x_m) * edge.dx
        + (probe.y_m - edge.start.y_m) * edge.dy
        + (probe.z_m - edge.start.z_m) * edge.dz
    ) / edge.length2
    t = max(0.0, min(1.0, t))
    return _position(
        edge.start.x_m + edge.dx * t,
        edge.start.y_m + edge.dy * t,
        edge.start.z_m + edge.dz * t,
    )


def _candidate(
    entity: SceneEntity,
    kind: SnapKind,
    feature_id: str,
    feature_position: Position3,
    axis: AxisName,
    probe: Position3,
) -> SnapCandidate:
    value = _axis_value(feature_position, axis)
    return SnapCandidate(
        stable_id=f'{entity.entity_id}:{kind}:{feature_id}:{axis}',
        kind=kind,
        entity_id=entity.entity_id,
        target=_with_axis(probe, axis, value),
        screen_anchor=feature_position,
        axis=axis,
        label=f'{kind} · {entity.name} · {axis.upper()}={value:.3f} m',
    )


def generate_snap_candidates(
    document: SceneDocument,
    *,
    exclude_ids: set[str],
    axis: AxisName,
    probe: Position3,
    kinds: set[SnapKind] | frozenset[SnapKind] | None = None,
) -> tuple[SnapCandidate, ...]:
    enabled = ALL_SNAP_KINDS if kinds is None else frozenset(kinds)
    unknown = enabled - ALL_SNAP_KINDS
    if unknown:
        raise ValueError(f'unsupported snap kinds: {sorted(unknown)}')

    candidates: list[SnapCandidate] = []
    for entity in document.entities:
        if entity.entity_id in exclude_ids:
            continue
        vertices = _entity_vertices(entity)
        if 'vertex' in enabled:
            for index, vertex in enumerate(vertices):
                candidates.append(_candidate(entity, 'vertex', str(index), vertex, axis, probe))
        if entity.size_m is not None and ({'midpoint', 'edge'} & enabled):
            for edge_index, edge in enumerate(_entity_edges(entity)):
                if 'midpoint' in enabled:
                    candidates.append(_candidate(entity, 'midpoint', str(edge_index), edge.midpoint, axis, probe))
                if 'edge' in enabled:
                    closest = _closest_point_on_edge(probe, edge)
                    candidates.append(_candidate(entity, 'edge', str(edge_index), closest, axis, probe))
        if 'alignment' in enabled:
            candidates.append(_candidate(entity, 'alignment', 'origin', entity.position, axis, probe))
    return tuple(candidates)