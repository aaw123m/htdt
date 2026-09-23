from __future__ import annotations

from math import sqrt
from typing import Iterable, Literal

from shapely.geometry import LineString, MultiPoint, Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .cad_constraint_models import (
    CadAllowedRegionConstraint,
    CadConstraintSet,
    CadExclusionRegionConstraint,
    CadPairDistanceConstraint,
    CadWallClearanceConstraint,
)
from .cad_scene import (
    SceneDocument,
    SceneEntity,
    mesh_body_envelope_state,
    quaternion_to_matrix3,
    room_vertices,
)
from .geometry import polygon_from_vertices


_EPS = 1e-9

# Segments per quarter circle in the shapely ``Point.buffer`` polygon
# approximation used for circular footprints.
_CIRCLE_BUFFER_QUAD_SEGS = 32

# Mesh footprints union one projected polygon per source triangle; beyond a
# generous triangle budget the union cost stops paying for itself and the
# entity keeps the (verified) bounding-envelope footprint instead.
_MESH_FOOTPRINT_TRIANGLE_LIMIT = 4096

BodyCollisionAuthority = Literal[
    'exact_body_geometry', 'bounding_envelope', 'envelope_unverified'
]


def _local_z_is_world_up(entity: SceneEntity) -> bool:
    """True when the entity-local +Z axis maps to world +Z (upright pose).

    Extruded/prism body geometry is only exact in XY while the body stays
    upright; any pitch/roll tilt widens the true XY projection, so tilted
    bodies fall back to the conservative bounding-envelope hull.
    """

    matrix = quaternion_to_matrix3(entity.orientation)
    return (
        abs(matrix[0][2]) <= _EPS
        and abs(matrix[1][2]) <= _EPS
        and matrix[2][2] >= 1.0 - 1e-6
    )


def entity_exact_body_footprint(entity: SceneEntity) -> BaseGeometry | None:
    """Exact world XY footprint of an authored body shape, or ``None``.

    Returns ``None`` for entities without exact extrusion geometry (box,
    missing size), for tilted bodies whose exact footprint is no longer
    their extruded XY profile, and for mesh bodies whose triangle soup is
    not loaded or is too large to union cheaply — callers then use the
    bounding-envelope hull.
    """

    body = entity.body_geometry
    if (
        entity.size_m is None
        or body is None
        or body.kind not in ('cylinder', 'extruded_polygon', 'mesh_asset')
        or not _local_z_is_world_up(entity)
    ):
        return None

    x_m = float(entity.position.x_m)
    y_m = float(entity.position.y_m)
    if body.kind == 'mesh_asset':
        mesh = body.mesh
        if mesh is None or len(mesh.triangles) > _MESH_FOOTPRINT_TRIANGLE_LIMIT:
            # An unresolved reference keeps the (bounds-verified) envelope;
            # the body is not treated as an exact footprint it cannot prove.
            return None
        return _mesh_asset_world_footprint(entity, mesh)

    if body.kind == 'cylinder':
        assert body.radius_m is not None
        return Point(x_m, y_m).buffer(
            float(body.radius_m),
            quad_segs=_CIRCLE_BUFFER_QUAD_SEGS,
        )

    matrix = quaternion_to_matrix3(entity.orientation)
    coords = []
    for vertex in body.footprint_vertices or ():
        local_x = float(vertex.x_m)
        local_y = float(vertex.y_m)
        coords.append((
            x_m + matrix[0][0] * local_x + matrix[0][1] * local_y,
            y_m + matrix[1][0] * local_x + matrix[1][1] * local_y,
        ))
    polygon = Polygon(coords)
    if not polygon.is_valid or polygon.is_empty:  # defensive: authorship validates already
        return None
    return polygon


def _mesh_asset_world_footprint(entity: SceneEntity, mesh) -> BaseGeometry | None:
    """Concave world XY footprint derived from the resolved mesh triangles.

    Each source triangle is transformed by the mesh's local scale/offset,
    rotated by the entity orientation, projected to XY, and unioned — so
    non-convex bodies (L-shapes, recliners, equipment cut-outs) contribute
    their true silhouette instead of the size_m envelope rectangle. The
    body must be upright: a tilted mesh widens its true XY projection.
    """

    matrix = quaternion_to_matrix3(entity.orientation)
    px = float(entity.position.x_m)
    py = float(entity.position.y_m)
    scale = float(mesh.uniform_scale)
    offset = mesh.local_offset_m
    projected: list[Polygon] = []
    for triangle in mesh.triangles:
        coords = []
        for index in (triangle.a, triangle.b, triangle.c):
            vertex = mesh.vertices[index]
            local = (
                float(vertex.x_m) * scale + offset.x_m,
                float(vertex.y_m) * scale + offset.y_m,
                float(vertex.z_m) * scale + offset.z_m,
            )
            coords.append((
                px + sum(matrix[0][column] * local[column] for column in range(3)),
                py + sum(matrix[1][column] * local[column] for column in range(3)),
            ))
        if coords[0] != coords[1] and coords[1] != coords[2] and coords[0] != coords[2]:
            projected.append(Polygon(coords))
    if not projected:
        return None
    footprint = unary_union(projected)
    if footprint.is_empty or not footprint.is_valid:
        return None
    return footprint


def entity_collision_geometry_authority(entity: SceneEntity) -> BodyCollisionAuthority:
    """The authority behind this entity's collision footprint (Issue #656).

    ``exact_body_geometry`` — the footprint is the body's true (possibly
    concave) silhouette. ``bounding_envelope`` — the size_m envelope is in
    use AND is verified to contain the authored mesh body. ``envelope_
    unverified`` — a mesh body is present but not provably inside size_m:
    the envelope may under-report clearance and the entity is visibly
    non-safe for collision claims until reconciled.
    """

    if entity_collision_geometry_authority_is_exact(entity):
        return 'exact_body_geometry'
    if (
        entity.size_m is not None
        and mesh_body_envelope_state(entity.body_geometry, entity.size_m)
        == 'envelope_unverified'
    ):
        return 'envelope_unverified'
    return 'bounding_envelope'


def entity_collision_geometry_authority_is_exact(entity: SceneEntity) -> bool:
    return entity_exact_body_footprint(entity) is not None


def entity_horizontal_footprint(entity: SceneEntity) -> BaseGeometry:
    """Return the exact XY projection of the oriented entity body.

    Entities with authored extrusion body geometry (cylinder, polygon
    footprint) contribute their exact upright footprint; resolved upright
    mesh bodies contribute their mesh-derived concave silhouette. Other
    physical entities use the convex hull of all eight oriented box
    corners so pitch/roll remain conservative in XY. Non-physical entities
    degrade to a point at their world position.
    """

    if entity.size_m is None:
        return Point(float(entity.position.x_m), float(entity.position.y_m))

    exact = entity_exact_body_footprint(entity)
    if exact is not None:
        return exact

    hx = float(entity.size_m.x_m) * 0.5
    hy = float(entity.size_m.y_m) * 0.5
    hz = float(entity.size_m.z_m) * 0.5
    matrix = quaternion_to_matrix3(entity.orientation)
    points: list[tuple[float, float]] = []
    for x in (-hx, hx):
        for y in (-hy, hy):
            for z in (-hz, hz):
                local = (x, y, z)
                world_x = float(entity.position.x_m) + sum(
                    matrix[0][column] * local[column] for column in range(3)
                )
                world_y = float(entity.position.y_m) + sum(
                    matrix[1][column] * local[column] for column in range(3)
                )
                points.append((world_x, world_y))
    return MultiPoint(points).convex_hull


def _region(vertices) -> BaseGeometry:
    return polygon_from_vertices(
        [(float(vertex.x_m), float(vertex.y_m)) for vertex in vertices]
    )


def _wall_line(document: SceneDocument, wall_id: str) -> LineString:
    topology = document.wall_topology
    if topology is None:
        raise ValueError('orientation-aware wall clearance requires wall topology')
    wall = next((item for item in topology.walls if item.wall_id == wall_id), None)
    if wall is None:
        raise ValueError(f'orientation-aware wall clearance references unknown wall: {wall_id}')
    vertices = {vertex.vertex_id: vertex for vertex in room_vertices(document.room)}
    try:
        start = vertices[wall.from_vertex_id]
        end = vertices[wall.to_vertex_id]
    except KeyError as exc:
        raise ValueError(
            f'orientation-aware wall clearance references stale wall endpoint: {wall_id}'
        ) from exc
    return LineString(
        (
            (float(start.x_m), float(start.y_m)),
            (float(end.x_m), float(end.y_m)),
        )
    )


def orientation_constraint_rejections(
    document: SceneDocument,
    constraint_set: CadConstraintSet,
    *,
    changed_entity_ids: Iterable[str],
) -> tuple[str, ...]:
    """Return hard-constraint IDs rejected by exact oriented XY envelopes.

    The existing O10 engine remains the base authority for position feasibility.
    This is an additional O80P refinement for body-rotation candidates. Only
    constraints affected by a rotated entity are reevaluated here.
    """

    changed = {str(entity_id) for entity_id in changed_entity_ids}
    if not changed:
        return ()
    if document.room is None:
        raise ValueError('orientation-aware constraints require a room')

    known = {entity.entity_id for entity in document.entities}
    unknown = changed - known
    if unknown:
        raise ValueError(
            'orientation-aware constraints reference unknown entities: '
            + ', '.join(sorted(unknown))
        )

    room_polygon = polygon_from_vertices(
        [(float(vertex.x_m), float(vertex.y_m)) for vertex in room_vertices(document.room)]
    )
    footprints = {
        entity.entity_id: entity_horizontal_footprint(entity)
        for entity in document.entities
    }

    rejected: set[str] = set()
    for entity_id in sorted(changed):
        if not room_polygon.covers(footprints[entity_id]):
            rejected.add(f'__room_boundary__:{entity_id}')

    for constraint in constraint_set.constraints:
        if isinstance(constraint, CadAllowedRegionConstraint):
            region = _region(constraint.vertices)
            for entity_id in constraint.entity_ids:
                if entity_id in changed and not region.covers(footprints[entity_id]):
                    rejected.add(constraint.constraint_id)

        elif isinstance(constraint, CadExclusionRegionConstraint):
            region = _region(constraint.vertices)
            for entity_id in constraint.entity_ids:
                if entity_id in changed and region.intersects(footprints[entity_id]):
                    rejected.add(constraint.constraint_id)

        elif isinstance(constraint, CadWallClearanceConstraint):
            if not changed.intersection(constraint.entity_ids):
                continue
            edge = _wall_line(document, constraint.wall_id)
            for entity_id in constraint.entity_ids:
                if entity_id not in changed:
                    continue
                clearance = float(footprints[entity_id].distance(edge))
                if (
                    (constraint.min_m is not None and clearance + _EPS < constraint.min_m)
                    or (constraint.max_m is not None and clearance > constraint.max_m + _EPS)
                ):
                    rejected.add(constraint.constraint_id)

        elif isinstance(constraint, CadPairDistanceConstraint):
            if not changed.intersection((constraint.entity_a, constraint.entity_b)):
                continue
            if constraint.distance_reference != 'envelope_clearance':
                continue
            distance = float(
                footprints[constraint.entity_a].distance(footprints[constraint.entity_b])
            )
            if (
                (constraint.min_m is not None and distance + _EPS < constraint.min_m)
                or (constraint.max_m is not None and distance > constraint.max_m + _EPS)
            ):
                rejected.add(constraint.constraint_id)

    return tuple(sorted(rejected))
