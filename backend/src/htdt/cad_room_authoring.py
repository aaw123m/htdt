"""Rich room geometry authoring authority (Issue #456).

The editor authors room-scale geometric primitives — sloped ceilings,
soffits, risers, partial-height walls, and adjacent regions — as *semantic*
geometry, not decoration. Each primitive compiles deterministically into
planar surfaces of the shared R120 polyhedral authority
(:func:`make_r120_polyhedral_semantic_geometry`), so the result is solver and
material authority rather than a viewport approximation.

Geometry conventions:

- Coordinates are HTDT meters: footprint XY from the ``RoomPrism`` footprint
  (or the rectangular ``width_m`` × ``depth_m`` ring), Z up from the floor
  plane at ``z=0``.
- The base room contributes a floor surface, one wall surface per footprint
  edge, and a ceiling surface. A sloped ceiling spec makes the ceiling
  polygon planar-but-tilted; walls become trapezoids.
- Soffits and risers are boxes joined to ceiling/floor: their exposed faces
  become room-boundary surfaces, and the floor surface gains each riser
  footprint as a hole so surfaces never overlap.
- Partial-height walls are bounded prisms standing on the floor: all faces
  except the floor-contact face are boundary surfaces of the containing air
  region.
- Adjacent regions are separate footprints joined through a rectangular
  opening in a shared wall: the opening is emitted as a
  ``PolyhedralPortal`` surface binding both regions, and the shared wall is
  split into flanking + lintel surfaces around the opening.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError
from shapely.geometry import Polygon, box

from .cad_scene import (
    MAIN_REGION_ID,
    AdjacentRegionSpec,
    PartialHeightWallSpec,
    RiserSpec,
    RoomAuthoringModel,
    RoomPrism,
    SlopedCeilingSpec,
    SoffitSpec,
    room_vertices,
)
from .r120_polyhedral_geometry import (
    PlanarPolygonSurfaceSpec,
    PolyhedralAirVolume,
    PolyhedralPortal,
    R120PolyhedralSemanticGeometry,
    make_r120_polyhedral_semantic_geometry,
)


ROOM_AUTHORING_ALGORITHM_ID = 'htdt.room_authoring'
ROOM_AUTHORING_ALGORITHM_VERSION = '1'

__all__ = [
    'AdjacentRegionSpec',
    'MAIN_REGION_ID',
    'PartialHeightWallSpec',
    'RiserSpec',
    'RoomAuthoringError',
    'RoomAuthoringIssue',
    'RoomAuthoringModel',
    'SlopedCeilingSpec',
    'SoffitSpec',
    'ceiling_height_at',
    'compile_room_authoring_to_r120',
    'rebind_room_authoring',
    'validate_room_authoring_model',
]


# ---------------------------------------------------------------------------
# Issue #976 fail-closed validation.
#
# ``RoomAuthoringModel`` validators only cover self-contained invariants.
# Anything that needs the footprint polygon, the sloped-ceiling field, or the
# wall-topology authority is checked here so UI commits and room/topology
# edits reject dangerous shapes with a specific, fixable finding instead of a
# compiler traceback or a silently clamped result.
# ---------------------------------------------------------------------------

AUTHORING_KIND_LABELS: dict[str, str] = {
    'ceiling': '傾斜天井',
    'soffit': '下がり天井',
    'riser': '段床',
    'partial_wall': '腰壁',
    'adjacent_region': '隣接室',
}


@dataclass(frozen=True)
class RoomAuthoringIssue:
    """One operator-facing finding about an authoring candidate.

    ``category`` is the honest-failure bucket from the issue contract:
    ``geometry_invalid`` covers self-contradicting or unbuildable geometry,
    ``unsupported`` covers shapes the current code/solver surface cannot
    faithfully represent. ``target`` names the primitive id the fix applies
    to, so the panel can point at the row that must change.
    """

    category: Literal['geometry_invalid', 'unsupported']
    severity: Literal['error', 'warning']
    code: str
    message: str
    target: str | None = None


class RoomAuthoringError(ValueError):
    """Fail-closed rejection carrying typed issues for operator display.

    Messages are short single-line Japanese text so
    ``operation_error_message`` preserves them verbatim.
    """

    def __init__(self, issues) -> None:
        self.issues = tuple(issues)
        super().__init__(' / '.join(issue.message for issue in self.issues))


def _rect_polygon(min_x: float, min_y: float, max_x: float, max_y: float) -> Polygon:
    return box(min_x, min_y, max_x, max_y)


def _partial_wall_polygon(wall: PartialHeightWallSpec) -> Polygon:
    dx = wall.x2_m - wall.x1_m
    dy = wall.y2_m - wall.y1_m
    length = (dx * dx + dy * dy) ** 0.5
    nx = -dy / length * wall.thickness_m * 0.5
    ny = dx / length * wall.thickness_m * 0.5
    return Polygon(
        [
            (wall.x1_m + nx, wall.y1_m + ny),
            (wall.x2_m + nx, wall.y2_m + ny),
            (wall.x2_m - nx, wall.y2_m - ny),
            (wall.x1_m - nx, wall.y1_m - ny),
        ]
    )


def _region_footprint(
    model: RoomAuthoringModel,
    region: AdjacentRegionSpec,
) -> Polygon | None:
    """Plan footprint extruded outward from the shared edge; None if the edge
    index no longer exists (reported separately)."""

    vertices = room_vertices(model.room)
    if region.shared_edge_index >= len(vertices):
        return None
    a = vertices[region.shared_edge_index]
    b = vertices[(region.shared_edge_index + 1) % len(vertices)]
    ax, ay = a.x_m, a.y_m
    bx, by = b.x_m, b.y_m
    edge_dx, edge_dy = bx - ax, by - ay
    edge_len = (edge_dx * edge_dx + edge_dy * edge_dy) ** 0.5
    if edge_len <= 1e-9:
        return None
    ring = [(vertex.x_m, vertex.y_m) for vertex in vertices]
    centroid_x = sum(x for x, _ in ring) / len(ring)
    centroid_y = sum(y for _, y in ring) / len(ring)
    nx, ny = -edge_dy / edge_len, edge_dx / edge_len
    mid_x, mid_y = (ax + bx) * 0.5, (ay + by) * 0.5
    if (mid_x + nx - centroid_x) ** 2 + (mid_y + ny - centroid_y) ** 2 < (
        (mid_x - nx - centroid_x) ** 2 + (mid_y - ny - centroid_y) ** 2
    ):
        nx, ny = -nx, -ny
    d = region.outward_depth_m
    return Polygon([(ax, ay), (bx, by), (bx + nx * d, by + ny * d), (ax + nx * d, ay + ny * d)])


def validate_room_authoring_model(
    model: RoomAuthoringModel,
    *,
    wall_topology=None,
) -> tuple[RoomAuthoringIssue, ...]:
    """Fail-closed validation of an authoring candidate against the room.

    Returns every finding — callers block on ``severity == 'error'`` and may
    surface warnings next to the offending row. ``wall_topology`` (optional)
    enables the opening/topology conflict check so a shared-wall opening
    never double-books a span already owned by a ``WallOpening``.
    """

    issues: list[RoomAuthoringIssue] = []

    def error(code: str, message: str, target: str | None = None,
              *, category: str = 'geometry_invalid') -> None:
        issues.append(
            RoomAuthoringIssue(
                category=category, severity='error',
                code=code, message=message, target=target,
            )
        )

    def warning(code: str, message: str, target: str | None = None,
                *, category: str = 'unsupported') -> None:
        issues.append(
            RoomAuthoringIssue(
                category=category, severity='warning',
                code=code, message=message, target=target,
            )
        )

    room = model.room
    vertices = room_vertices(room)
    footprint = Polygon([(vertex.x_m, vertex.y_m) for vertex in vertices])
    if not footprint.is_valid:
        error('footprint_invalid', '部屋の外形が自己交差しています')
        return tuple(issues)

    # Model-level invariants re-checked here so a rebound candidate (room
    # replaced by a footprint edit) still reports named targets instead of a
    # bare pydantic error.
    seen_edges: dict[int, str] = {}
    for region in model.adjacent_regions:
        label = f'隣接室「{region.region_id}」'
        if region.shared_edge_index >= len(vertices):
            error(
                'region_edge_missing',
                f'{label}: 共有する壁が外形上に存在しません（頂点編集で辺が消えました）',
                region.region_id,
            )
            continue
        if region.shared_edge_index in seen_edges:
            error(
                'shared_edge_conflict',
                f'{label}: 隣接室「{seen_edges[region.shared_edge_index]}」と同じ壁を共有しています',
                region.region_id,
            )
        else:
            seen_edges[region.shared_edge_index] = region.region_id
        a = vertices[region.shared_edge_index]
        b = vertices[(region.shared_edge_index + 1) % len(vertices)]
        edge_len = ((b.x_m - a.x_m) ** 2 + (b.y_m - a.y_m) ** 2) ** 0.5
        offset_m, width_m, height_m = region.opening
        if offset_m + width_m > edge_len + 1e-9:
            error(
                'opening_exceeds_edge',
                f'{label}: 接続開口（{width_m:.3g} m）が共有壁の長さ（{edge_len:.3g} m）を超えています',
                region.region_id,
            )
        else:
            # Opening top must clear the local (possibly sloped) ceiling.
            t0 = offset_m / edge_len if edge_len > 1e-9 else 0.0
            t1 = (offset_m + width_m) / edge_len
            top_z = min(
                ceiling_height_at(model, a.x_m + (b.x_m - a.x_m) * t, a.y_m + (b.y_m - a.y_m) * t)
                for t in (t0, t1)
            )
            if height_m >= top_z:
                error(
                    'opening_above_ceiling',
                    f'{label}: 接続開口の上端が天井を超えています',
                    region.region_id,
                )
        if region.ceiling_height_m is not None and region.ceiling_height_m <= height_m:
            error(
                'region_ceiling_below_opening',
                f'{label}: 隣接室の天井高が接続開口の高さ以下です',
                region.region_id,
            )
        region_poly = _region_footprint(model, region)
        if region_poly is not None:
            interior = footprint.intersection(region_poly)
            if interior.area > 1e-9:
                error(
                    'region_overlaps_main',
                    f'{label}: 外形が部屋本体と重なっています（共有壁の外側へ出してください）',
                    region.region_id,
                )
            if wall_topology is not None:
                # The shared edge's WallOpening spans double-book the portal.
                wall = next(
                    (
                        item
                        for item in wall_topology.walls
                        if (item.from_vertex_id, item.to_vertex_id)
                        == (a.vertex_id, b.vertex_id)
                    ),
                    None,
                )
                if wall is not None:
                    for opening in wall_topology.openings:
                        if opening.wall_id != wall.wall_id:
                            continue
                        if (
                            opening.offset_m < offset_m + width_m
                            and offset_m < opening.offset_m + opening.width_m
                        ):
                            error(
                                'opening_topology_conflict',
                                f'{label}: 接続開口が同じ壁の既存開口「{opening.opening_id}」と重なっています',
                                region.region_id,
                                category='unsupported',
                            )

    region_polys = {
        region.region_id: poly
        for region in model.adjacent_regions
        if (poly := _region_footprint(model, region)) is not None
    }
    ids = list(region_polys)
    for i, first in enumerate(ids):
        for second in ids[i + 1:]:
            if region_polys[first].intersection(region_polys[second]).area > 1e-9:
                error(
                    'region_overlap',
                    f'隣接室「{first}」と「{second}」の外形が重なっています',
                    second,
                )

    def _rect_contained(kind: str, item, poly: Polygon) -> bool:
        if not footprint.covers(poly.buffer(-1e-9)):
            error(
                'primitive_outside_footprint',
                f'{AUTHORING_KIND_LABELS[kind]}「{target_of(kind, item)}」: '
                '平面形状が部屋の外形の外にはみ出しています',
                target_of(kind, item),
            )
            return False
        return True

    def target_of(kind: str, item) -> str:
        return getattr(
            item,
            {
                'soffit': 'soffit_id',
                'riser': 'riser_id',
                'partial_wall': 'wall_id',
                'adjacent_region': 'region_id',
            }[kind],
        )

    riser_polys: dict[str, Polygon] = {}
    for riser in model.risers:
        poly = _rect_polygon(riser.min_x_m, riser.min_y_m, riser.max_x_m, riser.max_y_m)
        riser_polys[riser.riser_id] = poly
        label = f'段床「{riser.riser_id}」'
        if not _rect_contained('riser', riser, poly):
            continue
        top_clearance = min(
            ceiling_height_at(model, x, y)
            for x in (riser.min_x_m, riser.max_x_m)
            for y in (riser.min_y_m, riser.max_y_m)
        )
        if riser.height_m >= top_clearance - 1e-9:
            error(
                'riser_reaches_ceiling',
                f'{label}: 天端がその位置の天井高（{top_clearance:.3g} m）に達しています',
                riser.riser_id,
            )
        for other_id, other in riser_polys.items():
            if other_id == riser.riser_id:
                continue
            if other.intersection(poly).area > 1e-9:
                error(
                    'riser_overlap',
                    f'{label}: 段床「{other_id}」と平面が重なっています',
                    riser.riser_id,
                )

    soffit_polys: dict[str, Polygon] = {}
    for soffit in model.soffits:
        poly = _rect_polygon(soffit.min_x_m, soffit.min_y_m, soffit.max_x_m, soffit.max_y_m)
        soffit_polys[soffit.soffit_id] = poly
        label = f'下がり天井「{soffit.soffit_id}」'
        if not _rect_contained('soffit', soffit, poly):
            continue
        bottom_z = min(
            ceiling_height_at(model, x, y)
            for x in (soffit.min_x_m, soffit.max_x_m)
            for y in (soffit.min_y_m, soffit.max_y_m)
        ) - soffit.drop_m
        if bottom_z <= 1e-9:
            error(
                'soffit_below_floor',
                f'{label}: 下面が床面を下回ります（下がり量を減らしてください）',
                soffit.soffit_id,
            )
        for other_id, other in soffit_polys.items():
            if other_id == soffit.soffit_id:
                continue
            if other.intersection(poly).area > 1e-9:
                error(
                    'soffit_overlap',
                    f'{label}: 下がり天井「{other_id}」と平面が重なっています',
                    soffit.soffit_id,
                )
    for riser_id, riser_poly in riser_polys.items():
        for soffit_id, soffit_poly in soffit_polys.items():
            if riser_poly.intersection(soffit_poly).area > 1e-9:
                error(
                    'riser_soffit_overlap',
                    f'段床「{riser_id}」と下がり天井「{soffit_id}」の平面が重なっています',
                    soffit_id,
                )

    for wall in model.partial_walls:
        poly = _partial_wall_polygon(wall)
        label = f'腰壁「{wall.wall_id}」'
        if not _rect_contained('partial_wall', wall, poly):
            continue
        top_m = wall.base_height_m + wall.height_m
        ceiling_at = min(
            ceiling_height_at(model, wall.x1_m, wall.y1_m),
            ceiling_height_at(model, wall.x2_m, wall.y2_m),
        )
        if top_m > ceiling_at + 1e-9:
            error(
                'wall_above_ceiling',
                f'{label}: 上端（{top_m:.3g} m）がその位置の天井高（{ceiling_at:.3g} m）を超えています',
                wall.wall_id,
            )
        for riser_id, riser_poly in riser_polys.items():
            riser = next(item for item in model.risers if item.riser_id == riser_id)
            if (
                poly.intersection(riser_poly).area > 1e-9
                and wall.base_height_m < riser.height_m
            ):
                error(
                    'wall_embedded_in_riser',
                    f'{label}: 段床「{riser_id}」の内部を通っています',
                    wall.wall_id,
                )
        for soffit_id, soffit_poly in soffit_polys.items():
            if poly.intersection(soffit_poly).area > 1e-9:
                error(
                    'wall_under_soffit',
                    f'{label}: 下がり天井「{soffit_id}」と平面が重なっています（分離してください）',
                    wall.wall_id,
                )

    if (
        model.ceiling is not None
        or model.soffits
        or model.risers
        or model.partial_walls
        or model.adjacent_regions
    ):
        warning(
            'material_binding_unsupported',
            '高度な形状の面は現行の材質権威（semantic-surface参照）に未対応です · 依存する解析・材質割り当ては再検証対象になります',
        )
    return tuple(issues)


def rebind_room_authoring(
    model: RoomAuthoringModel,
    room: RoomPrism,
    *,
    wall_topology=None,
) -> RoomAuthoringModel:
    """Re-embed ``room`` into ``model`` after a footprint/height edit.

    Fail-closed: raises ``RoomAuthoringError`` naming every primitive the
    edit would break (dangling shared edges, ceilings crossed by riser tops,
    …) so the caller rejects the room edit and the committed document stays
    unchanged.
    """

    candidate = model.model_copy(update={'room': room})
    issues = validate_room_authoring_model(candidate, wall_topology=wall_topology)
    errors = tuple(issue for issue in issues if issue.severity == 'error')
    if errors:
        raise RoomAuthoringError(errors)
    try:
        return RoomAuthoringModel.model_validate(candidate.model_dump(mode='python'))
    except ValidationError as exc:  # pragma: no cover - defensive; validator covers these
        raise RoomAuthoringError(
            (
                RoomAuthoringIssue(
                    category='geometry_invalid',
                    severity='error',
                    code='rebind_invalid',
                    message='高度な形状が部屋の変更と整合しません',
                ),
            )
        ) from exc


class _VertexPool:
    """World-space vertex deduplication keyed on exact coordinates."""

    def __init__(self) -> None:
        self._index: dict[tuple[float, float, float], int] = {}
        self.points: list[tuple[float, float, float]] = []

    def add(self, point: tuple[float, float, float]) -> int:
        index = self._index.get(point)
        if index is None:
            index = len(self.points)
            self._index[point] = index
            self.points.append(point)
        return index


def ceiling_height_at(model: RoomAuthoringModel, x_m: float, y_m: float) -> float:
    """Ceiling height over a plan point under the (optional) sloped spec."""

    ceiling = model.ceiling
    if ceiling is None:
        return float(model.room.height_m)
    min_x, min_y, max_x, max_y = model.room.bounds_m
    span_x = max_x - min_x
    span_y = max_y - min_y
    if ceiling.slope_direction in ('x+', 'x-'):
        if span_x <= 1e-9:
            raise ValueError('sloped ceiling requires a non-degenerate footprint')
        t = (x_m - min_x) / span_x
        if ceiling.slope_direction == 'x-':
            t = 1.0 - t
    else:
        if span_y <= 1e-9:
            raise ValueError('sloped ceiling requires a non-degenerate footprint')
        t = (y_m - min_y) / span_y
        if ceiling.slope_direction == 'y-':
            t = 1.0 - t
    t = min(1.0, max(0.0, t))
    return ceiling.low_height_m + (ceiling.high_height_m - ceiling.low_height_m) * t


def _opening_segment(a, b, opening) -> tuple[tuple[float, float], tuple[float, float]]:
    """Endpoints of the wall opening along edge a→b (offset, width, height)."""

    offset_m, width_m, _height_m = opening
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length = (dx * dx + dy * dy) ** 0.5
    if length <= 1e-9 or offset_m + width_m > length + 1e-9:
        raise ValueError('adjacent region opening exceeds its shared wall edge')
    t0 = offset_m / length
    t1 = (offset_m + width_m) / length
    return (ax + dx * t0, ay + dy * t0), (ax + dx * t1, ay + dy * t1)


def compile_room_authoring_to_r120(
    model: RoomAuthoringModel,
    *,
    source_geometry_identity: str | None = None,
) -> R120PolyhedralSemanticGeometry:
    """Compile semantic room primitives into R120 polyhedral authority.

    The result is an ``explicit_polyhedral`` geometry: every surface is a
    planar polygon emitted once, air volumes bind each region's boundary
    surfaces, and adjacent-region openings become explicit portals.
    """

    room = model.room
    floor_ring = [(v.x_m, v.y_m) for v in room_vertices(room)]
    pool = _VertexPool()
    surfaces: list[PlanarPolygonSurfaceSpec] = []
    region_boundaries: dict[str, list[str]] = {MAIN_REGION_ID: []}
    portals: list[PolyhedralPortal] = []

    def emit(surface_key: str, points, *, region: str = MAIN_REGION_ID) -> str:
        indices = tuple(pool.add(tuple(float(c) for c in p)) for p in points)
        surfaces.append(
            PlanarPolygonSurfaceSpec(
                surface_key=surface_key,
                semantic_class='room_boundary',
                outer_vertex_indices=indices,
            )
        )
        region_boundaries.setdefault(region, []).append(surface_key)
        return surface_key

    ceiling_z = [ceiling_height_at(model, x, y) for (x, y) in floor_ring]
    floor_indices = tuple(pool.add((x, y, 0.0)) for (x, y) in floor_ring)
    ceiling_indices = tuple(
        pool.add((x, y, z)) for (x, y), z in zip(floor_ring, ceiling_z)
    )

    riser_holes = tuple(
        tuple(
            pool.add(p)
            for p in (
                (riser.min_x_m, riser.min_y_m, 0.0),
                (riser.max_x_m, riser.min_y_m, 0.0),
                (riser.max_x_m, riser.max_y_m, 0.0),
                (riser.min_x_m, riser.max_y_m, 0.0),
            )
        )
        for riser in model.risers
    )
    surfaces.append(
        PlanarPolygonSurfaceSpec(
            surface_key='floor',
            semantic_class='room_boundary',
            outer_vertex_indices=floor_indices,
            hole_vertex_indices=riser_holes,
        )
    )
    region_boundaries[MAIN_REGION_ID].append('floor')
    surfaces.append(
        PlanarPolygonSurfaceSpec(
            surface_key='ceiling',
            semantic_class='room_boundary',
            outer_vertex_indices=tuple(reversed(ceiling_indices)),
        )
    )
    region_boundaries[MAIN_REGION_ID].append('ceiling')

    region_by_edge = {
        region.shared_edge_index: region for region in model.adjacent_regions
    }
    edge_count = len(floor_ring)
    for edge_index in range(edge_count):
        next_index = (edge_index + 1) % edge_count
        ax, ay = floor_ring[edge_index]
        bx, by = floor_ring[next_index]
        z_a = ceiling_z[edge_index]
        z_b = ceiling_z[next_index]
        base = f'wall:{edge_index}'
        region = region_by_edge.get(edge_index)
        if region is None:
            emit(base, [(ax, ay, 0.0), (bx, by, 0.0), (bx, by, z_b), (ax, ay, z_a)])
            continue

        (ox1, oy1), (ox2, oy2) = _opening_segment((ax, ay), (bx, by), region.opening)
        opening_h = region.opening[2]
        top_a = ceiling_height_at(model, ox1, oy1)
        top_b = ceiling_height_at(model, ox2, oy2)
        if opening_h >= min(top_a, top_b):
            raise ValueError('adjacent region opening reaches above the ceiling')
        emit(f'{base}:flank-a', [
            (ax, ay, 0.0), (ox1, oy1, 0.0), (ox1, oy1, top_a), (ax, ay, z_a),
        ])
        emit(f'{base}:flank-b', [
            (ox2, oy2, 0.0), (bx, by, 0.0), (bx, by, z_b), (ox2, oy2, top_b),
        ])
        emit(f'{base}:lintel', [
            (ox1, oy1, opening_h), (ox2, oy2, opening_h),
            (ox2, oy2, top_b), (ox1, oy1, top_a),
        ])
        portal_surface = emit(f'{base}:opening', [
            (ox1, oy1, 0.0), (ox2, oy2, 0.0),
            (ox2, oy2, opening_h), (ox1, oy1, opening_h),
        ])
        _build_adjacent_region(model, region, (ax, ay), (bx, by), floor_ring, emit)
        region_boundaries[region.region_id].append(portal_surface)
        portals.append(
            PolyhedralPortal(
                portal_id=f'portal:{region.region_id}',
                region_ids=(MAIN_REGION_ID, region.region_id),
                surface_keys=(portal_surface,),
            )
        )

    for riser in model.risers:
        z = riser.height_m
        ring = (
            (riser.min_x_m, riser.min_y_m),
            (riser.max_x_m, riser.min_y_m),
            (riser.max_x_m, riser.max_y_m),
            (riser.min_x_m, riser.max_y_m),
        )
        emit(f'riser:{riser.riser_id}:top', [(x, y, z) for (x, y) in ring])
        for edge in range(4):
            (x1, y1) = ring[edge]
            (x2, y2) = ring[(edge + 1) % 4]
            emit(
                f'riser:{riser.riser_id}:side:{edge}',
                [(x1, y1, 0.0), (x2, y2, 0.0), (x2, y2, z), (x1, y1, z)],
            )

    for soffit in model.soffits:
        z0 = min(
            ceiling_height_at(model, x, y)
            for x in (soffit.min_x_m, soffit.max_x_m)
            for y in (soffit.min_y_m, soffit.max_y_m)
        ) - soffit.drop_m
        if z0 <= 0:
            raise ValueError(f'soffit {soffit.soffit_id} drops below the floor')
        ring = (
            (soffit.min_x_m, soffit.min_y_m),
            (soffit.max_x_m, soffit.min_y_m),
            (soffit.max_x_m, soffit.max_y_m),
            (soffit.min_x_m, soffit.max_y_m),
        )
        emit(f'soffit:{soffit.soffit_id}:bottom', [(x, y, z0) for (x, y) in ring])
        for edge in range(4):
            (x1, y1) = ring[edge]
            (x2, y2) = ring[(edge + 1) % 4]
            emit(
                f'soffit:{soffit.soffit_id}:side:{edge}',
                [
                    (x1, y1, z0), (x2, y2, z0),
                    (x2, y2, ceiling_height_at(model, x2, y2)),
                    (x1, y1, ceiling_height_at(model, x1, y1)),
                ],
            )

    for wall in model.partial_walls:
        dx = wall.x2_m - wall.x1_m
        dy = wall.y2_m - wall.y1_m
        length = (dx * dx + dy * dy) ** 0.5
        nx = -dy / length * wall.thickness_m * 0.5
        ny = dx / length * wall.thickness_m * 0.5
        z0 = wall.base_height_m
        z1 = z0 + wall.height_m
        corners = (
            (wall.x1_m + nx, wall.y1_m + ny),
            (wall.x2_m + nx, wall.y2_m + ny),
            (wall.x2_m - nx, wall.y2_m - ny),
            (wall.x1_m - nx, wall.y1_m - ny),
        )
        emit(f'partial-wall:{wall.wall_id}:top', [(x, y, z1) for (x, y) in corners])
        for edge in range(4):
            (x1, y1) = corners[edge]
            (x2, y2) = corners[(edge + 1) % 4]
            emit(
                f'partial-wall:{wall.wall_id}:face:{edge}',
                [(x1, y1, z0), (x2, y2, z0), (x2, y2, z1), (x1, y1, z1)],
            )

    air_volumes = tuple(
        PolyhedralAirVolume(
            region_id=region_id,
            boundary_surface_keys=tuple(boundaries),
        )
        for region_id, boundaries in region_boundaries.items()
    )
    identity = source_geometry_identity or f'room-authoring:{room.room_id}'
    return make_r120_polyhedral_semantic_geometry(
        source_geometry_identity=identity,
        source_geometry_kind='explicit_polyhedral',
        vertices=tuple(pool.points),
        surfaces=tuple(surfaces),
        air_volumes=air_volumes,
        portals=tuple(portals),
    )


def _build_adjacent_region(
    model: RoomAuthoringModel,
    region: AdjacentRegionSpec,
    a: tuple[float, float],
    b: tuple[float, float],
    floor_ring: list[tuple[float, float]],
    emit,
) -> None:
    """Emit floor/ceiling/outer/side surfaces for an adjacent region."""

    ax, ay = a
    bx, by = b
    edge_dx, edge_dy = bx - ax, by - ay
    edge_len = (edge_dx * edge_dx + edge_dy * edge_dy) ** 0.5
    centroid_x = sum(x for x, _ in floor_ring) / len(floor_ring)
    centroid_y = sum(y for _, y in floor_ring) / len(floor_ring)
    nx, ny = -edge_dy / edge_len, edge_dx / edge_len
    mid_x, mid_y = (ax + bx) * 0.5, (ay + by) * 0.5
    if (mid_x + nx - centroid_x) ** 2 + (mid_y + ny - centroid_y) ** 2 < (
        (mid_x - nx - centroid_x) ** 2 + (mid_y - ny - centroid_y) ** 2
    ):
        nx, ny = -nx, -ny
    d = region.outward_depth_m
    outer_a = (ax + nx * d, ay + ny * d)
    outer_b = (bx + nx * d, by + ny * d)
    region_h = region.ceiling_height_m or min(
        ceiling_height_at(model, ax, ay), ceiling_height_at(model, bx, by)
    )

    rid = region.region_id
    emit(f'region:{rid}:floor', [
        (ax, ay, 0.0), (bx, by, 0.0), (outer_b[0], outer_b[1], 0.0),
        (outer_a[0], outer_a[1], 0.0),
    ], region=rid)
    emit(f'region:{rid}:ceiling', [
        (ax, ay, region_h), (outer_a[0], outer_a[1], region_h),
        (outer_b[0], outer_b[1], region_h), (bx, by, region_h),
    ], region=rid)
    emit(f'region:{rid}:outer', [
        (outer_a[0], outer_a[1], 0.0), (outer_b[0], outer_b[1], 0.0),
        (outer_b[0], outer_b[1], region_h), (outer_a[0], outer_a[1], region_h),
    ], region=rid)
    emit(f'region:{rid}:side-a', [
        (ax, ay, 0.0), (outer_a[0], outer_a[1], 0.0),
        (outer_a[0], outer_a[1], region_h), (ax, ay, region_h),
    ], region=rid)
    emit(f'region:{rid}:side-b', [
        (outer_b[0], outer_b[1], 0.0), (bx, by, 0.0),
        (bx, by, region_h), (outer_b[0], outer_b[1], region_h),
    ], region=rid)
