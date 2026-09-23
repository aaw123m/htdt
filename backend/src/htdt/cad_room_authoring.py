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

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_scene import RoomPrism, room_vertices
from .r120_polyhedral_geometry import (
    PlanarPolygonSurfaceSpec,
    PolyhedralAirVolume,
    PolyhedralPortal,
    R120PolyhedralSemanticGeometry,
    make_r120_polyhedral_semantic_geometry,
)


ROOM_AUTHORING_ALGORITHM_ID = 'htdt.room_authoring'
ROOM_AUTHORING_ALGORITHM_VERSION = '1'

MAIN_REGION_ID = 'main'


class SlopedCeilingSpec(BaseModel):
    """Linear ceiling slope from ``low_height_m`` to ``high_height_m`` along
    ``slope_direction`` across the room extent."""

    model_config = ConfigDict(frozen=True)
    slope_direction: Literal['x+', 'x-', 'y+', 'y-']
    low_height_m: float = Field(gt=0)
    high_height_m: float = Field(gt=0)

    @model_validator(mode='after')
    def non_degenerate(self) -> 'SlopedCeilingSpec':
        if abs(self.high_height_m - self.low_height_m) < 1e-9:
            raise ValueError('a flat ceiling is not a sloped ceiling')
        return self


class SoffitSpec(BaseModel):
    """Rectangular box dropped below the ceiling plane."""

    model_config = ConfigDict(frozen=True)
    soffit_id: str = Field(min_length=1)
    min_x_m: float
    min_y_m: float
    max_x_m: float
    max_y_m: float
    drop_m: float = Field(gt=0)  # depth below the local ceiling height

    @model_validator(mode='after')
    def non_degenerate(self) -> 'SoffitSpec':
        if self.max_x_m <= self.min_x_m or self.max_y_m <= self.min_y_m:
            raise ValueError('soffit footprint must have positive extent')
        return self


class RiserSpec(BaseModel):
    """Raised floor platform: a box sitting on the floor."""

    model_config = ConfigDict(frozen=True)
    riser_id: str = Field(min_length=1)
    min_x_m: float
    min_y_m: float
    max_x_m: float
    max_y_m: float
    height_m: float = Field(gt=0)

    @model_validator(mode='after')
    def non_degenerate(self) -> 'RiserSpec':
        if self.max_x_m <= self.min_x_m or self.max_y_m <= self.min_y_m:
            raise ValueError('riser footprint must have positive extent')
        return self


class PartialHeightWallSpec(BaseModel):
    """Bounded wall prism standing on the floor, below ceiling height."""

    model_config = ConfigDict(frozen=True)
    wall_id: str = Field(min_length=1)
    x1_m: float
    y1_m: float
    x2_m: float
    y2_m: float
    thickness_m: float = Field(gt=0)
    height_m: float = Field(gt=0)
    base_height_m: float = Field(default=0.0, ge=0)

    @model_validator(mode='after')
    def non_degenerate(self) -> 'PartialHeightWallSpec':
        if abs(self.x2_m - self.x1_m) < 1e-9 and abs(self.y2_m - self.y1_m) < 1e-9:
            raise ValueError('partial-height wall requires a non-zero span')
        return self


class AdjacentRegionSpec(BaseModel):
    """A separate footprint volume connected through a wall opening.

    ``shared_edge_index`` is the index into ``room_vertices(room)`` whose
    edge (i → i+1) the region shares; the region footprint extrudes outward
    along the edge normal by ``outward_depth_m`` for the full edge length.
    ``opening`` = (offset_m along the edge, width_m, height_m).
    """

    model_config = ConfigDict(frozen=True)
    region_id: str = Field(min_length=1)
    shared_edge_index: int = Field(ge=0)
    outward_depth_m: float = Field(gt=0)
    opening: tuple[float, float, float]
    ceiling_height_m: float | None = None

    @model_validator(mode='after')
    def non_degenerate(self) -> 'AdjacentRegionSpec':
        offset, width, height = self.opening
        if width <= 0 or height <= 0 or offset < 0:
            raise ValueError('adjacent region opening must be positive')
        return self


class RoomAuthoringModel(BaseModel):
    """Semantic room primitives over a base ``RoomPrism`` footprint."""

    model_config = ConfigDict(frozen=True)
    room: RoomPrism
    ceiling: SlopedCeilingSpec | None = None
    soffits: tuple[SoffitSpec, ...] = ()
    risers: tuple[RiserSpec, ...] = ()
    partial_walls: tuple[PartialHeightWallSpec, ...] = ()
    adjacent_regions: tuple[AdjacentRegionSpec, ...] = ()

    @model_validator(mode='after')
    def unique_ids(self) -> 'RoomAuthoringModel':
        ids = (
            [item.soffit_id for item in self.soffits]
            + [item.riser_id for item in self.risers]
            + [item.wall_id for item in self.partial_walls]
            + [item.region_id for item in self.adjacent_regions]
        )
        if len(ids) != len(set(ids)):
            raise ValueError('room authoring primitive ids must be unique')
        if MAIN_REGION_ID in {item.region_id for item in self.adjacent_regions}:
            raise ValueError(f'{MAIN_REGION_ID!r} is reserved for the base room region')
        for region in self.adjacent_regions:
            if region.shared_edge_index >= len(room_vertices(self.room)):
                raise ValueError('adjacent region references an unknown footprint edge')
        for riser in self.risers:
            if riser.height_m >= float(self.room.height_m):
                raise ValueError('riser height must stay below the room ceiling')
        for soffit in self.soffits:
            if soffit.drop_m >= float(self.room.height_m):
                raise ValueError('soffit drop must stay above the room floor')
        for wall in self.partial_walls:
            if wall.base_height_m + wall.height_m > float(self.room.height_m):
                raise ValueError('partial-height wall top exceeds the room ceiling')
        return self


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


def _ceiling_height_at(model: RoomAuthoringModel, x_m: float, y_m: float) -> float:
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

    ceiling_z = [_ceiling_height_at(model, x, y) for (x, y) in floor_ring]
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
        top_a = _ceiling_height_at(model, ox1, oy1)
        top_b = _ceiling_height_at(model, ox2, oy2)
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
            _ceiling_height_at(model, x, y)
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
                    (x2, y2, _ceiling_height_at(model, x2, y2)),
                    (x1, y1, _ceiling_height_at(model, x1, y1)),
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
        _ceiling_height_at(model, ax, ay), _ceiling_height_at(model, bx, by)
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
