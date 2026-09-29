"""Operational geometry and swept-clearance authority (Issue #651).

Physical entities need clearance beyond their static body: doors swing,
recliners extend, racks slide out for service, turntables rotate. This
module authors and evaluates those operational zones as first-class,
persisted scene data — ``SceneEntity.operational_zones`` — so clearance
conflicts are detected from authority geometry instead of ad-hoc viewport
checks.

- ``operational_zone_footprint`` projects a zone into a world-space shapely
  polygon (entity position + orientation applied), so conflict checks use
  the same exact-footprint machinery as static bodies.
- ``operational_clearance_conflicts`` reports where a swept zone intersects
  another entity's collision footprint or leaves the room boundary — a
  conflict is a report record, never a silent rejection.
- Zones are entity-local: ``direction``/``hinge_offset_m`` are interpreted
  in the entity's local XY frame (local +Y is the entity's forward), then
  rotated into the world by the entity orientation.
"""

from __future__ import annotations

from math import atan2, cos, degrees, pi, radians, sin
from typing import Iterable, Literal

from pydantic import BaseModel, ConfigDict
from shapely.geometry import Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .cad_orientation_constraints import entity_horizontal_footprint
from .cad_scene import (
    OperationalZone,
    SceneDocument,
    SceneEntity,
    quaternion_to_matrix3,
    room_vertices,
)
from .geometry import polygon_from_vertices


OPERATIONAL_GEOMETRY_VERSION = '1'

_SECTOR_SEGMENTS = 24
_EPS = 1e-9


class OperationalClearanceConflict(BaseModel):
    """One swept-clearance violation between an operational zone and a body."""

    model_config = ConfigDict(frozen=True)
    entity_id: str
    zone_id: str
    conflict_kind: Literal['intersects_entity', 'leaves_room', 'overlaps_zone']
    other_entity_id: str | None = None
    other_zone_id: str | None = None


def _entity_local_to_world_xy(
    entity: SceneEntity, local_x: float, local_y: float
) -> tuple[float, float]:
    matrix = quaternion_to_matrix3(entity.orientation)
    return (
        float(entity.position.x_m)
        + matrix[0][0] * local_x
        + matrix[0][1] * local_y,
        float(entity.position.y_m)
        + matrix[1][0] * local_x
        + matrix[1][1] * local_y,
    )


def _entity_local_forward_angle(entity: SceneEntity) -> float:
    """World XY angle of the entity-local +Y (forward) axis."""

    matrix = quaternion_to_matrix3(entity.orientation)
    return atan2(matrix[1][1], matrix[0][1])


def _sector_polygon(
    center: tuple[float, float],
    radius: float,
    start_angle: float,
    sweep_deg: float,
) -> Polygon:
    steps = max(4, int(_SECTOR_SEGMENTS * sweep_deg / 360.0))
    points = [center]
    for i in range(steps + 1):
        angle = start_angle + radians(sweep_deg) * i / steps
        points.append((center[0] + radius * cos(angle), center[1] + radius * sin(angle)))
    points.append(center)
    return Polygon(points)


def _swept_rectangle(
    corners: list[tuple[float, float]], direction: tuple[float, float], distance: float
) -> Polygon:
    moved = [(x + direction[0] * distance, y + direction[1] * distance) for (x, y) in corners]
    return unary_union([Polygon(corners), Polygon(moved)]).convex_hull


def operational_zone_footprint(
    entity: SceneEntity, zone: OperationalZone
) -> BaseGeometry:
    """World-space XY footprint swept by one operational zone.

    door_swing: circular sector about the world-projected hinge point.
    recline/slide_out/service_access: the entity footprint translated along
    the declared direction (unioned with its start, so the swept corridor is
    covered). rotate: circular sector about the entity center.
    """

    half_x = float(entity.size_m.x_m) * 0.5 if entity.size_m else 0.0
    half_y = float(entity.size_m.y_m) * 0.5 if entity.size_m else 0.0

    if zone.kind == 'door_swing':
        assert zone.hinge_offset_m is not None and zone.radius_m is not None
        hx, hy = _entity_local_to_world_xy(entity, *zone.hinge_offset_m)
        # Hinge at a door edge: the door rest position points along the
        # entity-local +Y edge direction; sweep from there.
        rest_angle = _entity_local_forward_angle(entity)
        return _sector_polygon((hx, hy), float(zone.radius_m), rest_angle, float(zone.angle_deg))

    if zone.kind == 'rotate':
        assert zone.radius_m is not None and zone.angle_deg is not None
        center = (float(entity.position.x_m), float(entity.position.y_m))
        if float(zone.angle_deg) >= 360.0:
            return Point(*center).buffer(float(zone.radius_m), quad_segs=32)
        # The partial sweep starts along the entity-local +Y forward axis.
        return _sector_polygon(
            center,
            float(zone.radius_m),
            _entity_local_forward_angle(entity),
            float(zone.angle_deg),
        )

    # recline / slide_out / service_access: swept corridor of the body
    # rectangle along the declared direction.
    assert zone.direction is not None and zone.distance_m is not None
    corners_local = [
        (-half_x, -half_y), (half_x, -half_y), (half_x, half_y), (-half_x, half_y)
    ]
    corners = [_entity_local_to_world_xy(entity, x, y) for (x, y) in corners_local]
    world_direction = _entity_local_to_world_xy(entity, *zone.direction)
    base = (float(entity.position.x_m), float(entity.position.y_m))
    direction = (world_direction[0] - base[0], world_direction[1] - base[1])
    return _swept_rectangle(corners, direction, float(zone.distance_m))


def operational_swept_footprint(entity: SceneEntity) -> BaseGeometry | None:
    """Union of all of an entity's operational zones, or None."""

    zones = entity.operational_zones or ()
    if not zones:
        return None
    footprints = [operational_zone_footprint(entity, zone) for zone in zones]
    if not footprints:
        return None
    return unary_union(footprints)


def operational_clearance_conflicts(
    document: SceneDocument,
    *,
    entity_ids: Iterable[str] | None = None,
) -> tuple[OperationalClearanceConflict, ...]:
    """Report where operational zones collide with bodies, zones, or the room.

    Zones are checked against every other entity's horizontal footprint, and
    against the room boundary polygon when a room exists. ``entity_ids``
    scopes evaluation to a changed subset; others' zones are still considered
    as obstacles.
    """

    entities = {entity.entity_id: entity for entity in document.entities}
    scope = set(entity_ids) if entity_ids is not None else set(entities)
    unknown = scope - set(entities)
    if unknown:
        raise ValueError('operational clearance references unknown entities: ' + ', '.join(sorted(unknown)))

    room_polygon = (
        polygon_from_vertices(
            [(float(v.x_m), float(v.y_m)) for v in room_vertices(document.room)]
        )
        if document.room is not None
        else None
    )
    static_footprints = {
        entity_id: entity_horizontal_footprint(entity)
        for entity_id, entity in entities.items()
    }
    zone_footprints: dict[tuple[str, str], BaseGeometry] = {}
    for entity_id, entity in entities.items():
        for zone in entity.operational_zones or ():
            zone_footprints[(entity_id, zone.zone_id)] = operational_zone_footprint(
                entity, zone
            )

    conflicts: list[OperationalClearanceConflict] = []
    seen_zone_pairs: set[tuple[tuple[str, str], tuple[str, str]]] = set()
    for (entity_id, zone_id), zone_polygon in sorted(zone_footprints.items()):
        if entity_id not in scope:
            continue
        if room_polygon is not None and not room_polygon.covers(zone_polygon):
            conflicts.append(
                OperationalClearanceConflict(
                    entity_id=entity_id,
                    zone_id=zone_id,
                    conflict_kind='leaves_room',
                )
            )
        for other_id, other_footprint in static_footprints.items():
            if other_id == entity_id:
                continue
            if zone_polygon.intersects(other_footprint):
                conflicts.append(
                    OperationalClearanceConflict(
                        entity_id=entity_id,
                        zone_id=zone_id,
                        conflict_kind='intersects_entity',
                        other_entity_id=other_id,
                    )
                )
        for (other_entity, other_zone), other_zone_polygon in zone_footprints.items():
            if other_entity == entity_id:
                continue
            pair = tuple(sorted(((entity_id, zone_id), (other_entity, other_zone))))
            if pair in seen_zone_pairs:
                continue
            seen_zone_pairs.add(pair)
            if zone_polygon.intersects(other_zone_polygon):
                conflicts.append(
                    OperationalClearanceConflict(
                        entity_id=entity_id,
                        zone_id=zone_id,
                        conflict_kind='overlaps_zone',
                        other_entity_id=other_entity,
                        other_zone_id=other_zone,
                    )
                )
    return tuple(conflicts)
