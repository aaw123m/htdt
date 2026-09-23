"""Constraint authoring helpers for the workflow-first Room surface (#486).

Pure functions that create the same persisted ``CadConstraintSet`` shapes the
legacy dock editor produced — identical geometry math (shapely-clipped
regions around a subject, nearest-wall clearance, pairwise clearance) — so
constraints authored in the Room workspace are indistinguishable to the
evaluation/save/consume chain used by Optimize.
"""

from __future__ import annotations

from math import hypot
from uuid import uuid4

from shapely.geometry import Point, Polygon, box

from .cad_constraint_models import (
    CadAllowedRegionConstraint,
    CadConstraintPoint2D,
    CadConstraintSet,
    CadExclusionRegionConstraint,
    CadPairDistanceConstraint,
    CadPlacementConstraint,
    CadWallClearanceConstraint,
)
from .cad_scene import SceneDocument, room_vertices


WALKWAY_HALF_WIDTH_M = 0.45
WALKWAY_HALF_DEPTH_M = 0.90
ALLOWED_REGION_HALF_EXTENT_M = 1.00
DEFAULT_CLEARANCE_M = 0.50

CONSTRAINT_KIND_LABELS: dict[str, str] = {
    'allowed_region': '許可領域',
    'walkway': '通路',
    'exclusion': '除外領域',
    'wall_clearance': '壁離隔',
    'pair_distance': '物体間離隔',
}


def constraint_entity_ids(constraint: CadPlacementConstraint) -> tuple[str, ...]:
    if isinstance(constraint, (CadAllowedRegionConstraint, CadExclusionRegionConstraint, CadWallClearanceConstraint)):
        return tuple(constraint.entity_ids)
    return (constraint.entity_a, constraint.entity_b)


def region_vertices_around_entity(
    document: SceneDocument,
    entity_id: str,
    *,
    half_width_m: float,
    half_depth_m: float,
) -> tuple[CadConstraintPoint2D, ...]:
    """Rectangle around an entity clipped to the room polygon (same as legacy)."""

    if document.room is None:
        raise ValueError('部屋が必要です')
    entity = document.entity(entity_id)
    room_points = [(vertex.x_m, vertex.y_m) for vertex in room_vertices(document.room)]
    room_polygon = Polygon(room_points)
    proposed = box(
        entity.position.x_m - half_width_m,
        entity.position.y_m - half_depth_m,
        entity.position.x_m + half_width_m,
        entity.position.y_m + half_depth_m,
    )
    clipped = room_polygon.intersection(proposed)
    if clipped.is_empty:
        raise ValueError('選択位置の周囲に領域を作れません')
    if clipped.geom_type == 'MultiPolygon':
        point = Point(entity.position.x_m, entity.position.y_m)
        polygons = list(clipped.geoms)
        containing = [polygon for polygon in polygons if polygon.covers(point)]
        clipped = max(containing or polygons, key=lambda polygon: polygon.area)
    if clipped.geom_type != 'Polygon' or clipped.area <= 1e-9:
        raise ValueError('有効な領域ポリゴンを作れません')
    coordinates = list(clipped.exterior.coords)[:-1]
    if len(coordinates) < 3:
        raise ValueError('領域には3点以上必要です')
    return tuple(CadConstraintPoint2D(x_m=float(x), y_m=float(y)) for x, y in coordinates)


def wall_points(
    document: SceneDocument,
    wall_id: str,
) -> tuple[tuple[float, float], tuple[float, float]]:
    if document.room is None or document.wall_topology is None:
        raise ValueError('壁トポロジーが必要です')
    wall = next(
        (item for item in document.wall_topology.walls if item.wall_id == wall_id),
        None,
    )
    if wall is None:
        raise ValueError(f'wall not found: {wall_id}')
    vertices = {vertex.vertex_id: vertex for vertex in room_vertices(document.room)}
    start = vertices[wall.from_vertex_id]
    end = vertices[wall.to_vertex_id]
    return ((float(start.x_m), float(start.y_m)), (float(end.x_m), float(end.y_m)))


def distance_to_segment(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> tuple[float, tuple[float, float]]:
    """(distance, closest point) from a 2D point to a wall segment."""

    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-18:
        return (hypot(point[0] - start[0], point[1] - start[1]), start)
    ratio = ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length_sq
    ratio = max(0.0, min(1.0, ratio))
    closest = (start[0] + ratio * dx, start[1] + ratio * dy)
    return (hypot(point[0] - closest[0], point[1] - closest[1]), closest)


def nearest_wall_id(document: SceneDocument, entity_id: str) -> str | None:
    """Wall segment closest to an entity — the default for wall clearance."""

    topology = document.wall_topology
    if topology is None:
        return None
    entity = document.entity(entity_id)
    point = (float(entity.position.x_m), float(entity.position.y_m))
    best: tuple[float, str] | None = None
    for wall in topology.walls:
        distance, _ = distance_to_segment(point, *wall_points(document, wall.wall_id))
        candidate = (distance, wall.wall_id)
        if best is None or candidate < best:
            best = candidate
    return None if best is None else best[1]


def make_walkway_constraint(document: SceneDocument, entity_id: str) -> CadExclusionRegionConstraint:
    return CadExclusionRegionConstraint(
        constraint_id=f'walkway-{uuid4().hex[:10]}',
        name='通路',
        entity_ids=(entity_id,),
        vertices=region_vertices_around_entity(
            document,
            entity_id,
            half_width_m=WALKWAY_HALF_WIDTH_M,
            half_depth_m=WALKWAY_HALF_DEPTH_M,
        ),
        region_role='walkway',
    )


def make_allowed_region_constraint(document: SceneDocument, entity_id: str) -> CadAllowedRegionConstraint:
    return CadAllowedRegionConstraint(
        constraint_id=f'allowed-{uuid4().hex[:10]}',
        name='許可領域',
        entity_ids=(entity_id,),
        vertices=region_vertices_around_entity(
            document,
            entity_id,
            half_width_m=ALLOWED_REGION_HALF_EXTENT_M,
            half_depth_m=ALLOWED_REGION_HALF_EXTENT_M,
        ),
    )


def make_wall_clearance_constraint(
    document: SceneDocument,
    entity_id: str,
    *,
    wall_id: str | None = None,
    min_m: float = DEFAULT_CLEARANCE_M,
) -> CadWallClearanceConstraint:
    resolved_wall_id = wall_id or nearest_wall_id(document, entity_id)
    if resolved_wall_id is None:
        raise ValueError('壁離隔には壁トポロジーが必要です')
    return CadWallClearanceConstraint(
        constraint_id=f'wall-clearance-{uuid4().hex[:10]}',
        name='壁離隔',
        entity_ids=(entity_id,),
        wall_id=resolved_wall_id,
        min_m=min_m,
    )


def make_pair_distance_constraint(
    document: SceneDocument,
    entity_a: str,
    entity_b: str,
    *,
    min_m: float = DEFAULT_CLEARANCE_M,
) -> CadPairDistanceConstraint:
    if entity_a == entity_b:
        raise ValueError('物体間離隔は2物体を選択してください')
    document.entity(entity_a)
    document.entity(entity_b)
    return CadPairDistanceConstraint(
        constraint_id=f'pair-clearance-{uuid4().hex[:10]}',
        name='物体間離隔',
        entity_a=entity_a,
        entity_b=entity_b,
        min_m=min_m,
        distance_mode='horizontal_xy',
        distance_reference='envelope_clearance',
    )


def add_constraint(constraint_set: CadConstraintSet, constraint: CadPlacementConstraint) -> CadConstraintSet:
    return CadConstraintSet(
        document_id=constraint_set.document_id,
        constraints=constraint_set.constraints + (constraint,),
    )


def remove_constraint(constraint_set: CadConstraintSet, constraint_id: str) -> CadConstraintSet:
    return CadConstraintSet(
        document_id=constraint_set.document_id,
        constraints=tuple(
            item for item in constraint_set.constraints if item.constraint_id != constraint_id
        ),
    )
