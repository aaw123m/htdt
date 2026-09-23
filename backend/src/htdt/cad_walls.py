from __future__ import annotations

from math import hypot

from .cad_scene import RoomPrism, RoomVertex, make_polygon_room, room_vertices
from .cad_wall_models import WallConstraintBinding, WallOpening, WallSegment, WallTopology


class WallTopologyError(ValueError):
    """Raised when a wall topology edit cannot be committed unambiguously."""


def _vertex_map(room: RoomPrism) -> dict[str, RoomVertex]:
    return {vertex.vertex_id: vertex for vertex in room_vertices(room)}


def _edge_pairs(room: RoomPrism) -> tuple[tuple[str, str], ...]:
    vertices = room_vertices(room)
    return tuple(
        (vertex.vertex_id, vertices[(index + 1) % len(vertices)].vertex_id)
        for index, vertex in enumerate(vertices)
    )


def wall_length(room: RoomPrism, wall: WallSegment) -> float:
    vertices = _vertex_map(room)
    try:
        start = vertices[wall.from_vertex_id]
        end = vertices[wall.to_vertex_id]
    except KeyError as exc:
        raise WallTopologyError(f'wall {wall.wall_id} references an unknown room vertex') from exc
    return hypot(end.x_m - start.x_m, end.y_m - start.y_m)


def validate_wall_topology(room: RoomPrism, topology: WallTopology) -> WallTopology:
    expected_edges = set(_edge_pairs(room))
    actual_edges = {(wall.from_vertex_id, wall.to_vertex_id) for wall in topology.walls}
    if actual_edges != expected_edges or len(topology.walls) != len(expected_edges):
        raise WallTopologyError('wall topology must map one-to-one to the ordered room boundary edges')
    walls = {wall.wall_id: wall for wall in topology.walls}
    for opening in topology.openings:
        length = wall_length(room, walls[opening.wall_id])
        if opening.offset_m + opening.width_m > length + 1e-9:
            raise WallTopologyError(f'opening {opening.opening_id} exceeds wall {opening.wall_id}')
        if opening.sill_m + opening.height_m > room.height_m + 1e-9:
            raise WallTopologyError(f'opening {opening.opening_id} exceeds room height')
    return topology


def make_wall_topology(room: RoomPrism, *, thickness_m: float = 0.10) -> WallTopology:
    walls = tuple(
        WallSegment(
            wall_id=f'wall:{from_vertex_id}->{to_vertex_id}',
            from_vertex_id=from_vertex_id,
            to_vertex_id=to_vertex_id,
            thickness_m=thickness_m,
        )
        for from_vertex_id, to_vertex_id in _edge_pairs(room)
    )
    return validate_wall_topology(room, WallTopology(walls=walls))


def add_opening(room: RoomPrism, topology: WallTopology, opening: WallOpening) -> WallTopology:
    candidate = WallTopology(
        walls=topology.walls,
        openings=topology.openings + (opening,),
        constraint_bindings=topology.constraint_bindings,
    )
    return validate_wall_topology(room, candidate)


def add_constraint_binding(
    room: RoomPrism,
    topology: WallTopology,
    binding: WallConstraintBinding,
) -> WallTopology:
    candidate = WallTopology(
        walls=topology.walls,
        openings=topology.openings,
        constraint_bindings=topology.constraint_bindings + (binding,),
    )
    return validate_wall_topology(room, candidate)


def move_wall(
    room: RoomPrism,
    topology: WallTopology,
    wall_id: str,
    *,
    delta_x_m: float,
    delta_y_m: float,
) -> tuple[RoomPrism, WallTopology]:
    wall = _wall(topology, wall_id)
    vertices = list(room_vertices(room))
    changed = False
    for index, vertex in enumerate(vertices):
        if vertex.vertex_id in {wall.from_vertex_id, wall.to_vertex_id}:
            vertices[index] = RoomVertex(
                vertex_id=vertex.vertex_id,
                x_m=vertex.x_m + float(delta_x_m),
                y_m=vertex.y_m + float(delta_y_m),
            )
            changed = True
    if not changed:
        raise WallTopologyError(f'wall {wall_id} has no room vertices')
    try:
        moved_room = make_polygon_room(vertices, height_m=room.height_m, room_id=room.room_id)
    except ValueError as exc:
        raise WallTopologyError(f'wall move would create invalid room geometry: {exc}') from exc
    return moved_room, validate_wall_topology(moved_room, topology)


def split_wall(
    room: RoomPrism,
    topology: WallTopology,
    wall_id: str,
    *,
    offset_m: float,
    new_vertex_id: str,
    first_wall_id: str,
    second_wall_id: str,
) -> tuple[RoomPrism, WallTopology]:
    wall = _wall(topology, wall_id)
    length = wall_length(room, wall)
    offset = float(offset_m)
    if not 1e-9 < offset < length - 1e-9:
        raise WallTopologyError('wall split offset must be strictly inside the wall')
    vertices = list(room_vertices(room))
    edge_index = _edge_index(room, wall)
    start = vertices[edge_index]
    end = vertices[(edge_index + 1) % len(vertices)]
    ratio = offset / length
    inserted = RoomVertex(
        vertex_id=new_vertex_id,
        x_m=start.x_m + (end.x_m - start.x_m) * ratio,
        y_m=start.y_m + (end.y_m - start.y_m) * ratio,
    )
    vertices.insert(edge_index + 1, inserted)
    split_room = make_polygon_room(vertices, height_m=room.height_m, room_id=room.room_id)

    first = WallSegment(
        wall_id=first_wall_id,
        from_vertex_id=wall.from_vertex_id,
        to_vertex_id=new_vertex_id,
        thickness_m=wall.thickness_m,
        source_wall_id=wall.wall_id,
    )
    second = WallSegment(
        wall_id=second_wall_id,
        from_vertex_id=new_vertex_id,
        to_vertex_id=wall.to_vertex_id,
        thickness_m=wall.thickness_m,
        source_wall_id=wall.wall_id,
    )
    walls = list(topology.walls)
    wall_index = walls.index(wall)
    walls[wall_index:wall_index + 1] = [first, second]

    migrated_openings: list[WallOpening] = []
    tolerance = 1e-9
    for opening in topology.openings:
        if opening.wall_id != wall.wall_id:
            migrated_openings.append(opening)
            continue
        opening_end = opening.offset_m + opening.width_m
        if opening_end <= offset + tolerance:
            migrated_openings.append(opening.model_copy(update={'wall_id': first.wall_id}))
        elif opening.offset_m >= offset - tolerance:
            migrated_openings.append(
                opening.model_copy(
                    update={'wall_id': second.wall_id, 'offset_m': opening.offset_m - offset}
                )
            )
        else:
            raise WallTopologyError(
                f'opening {opening.opening_id} crosses split point; resolve it before splitting the wall'
            )

    migrated_bindings = tuple(
        binding.model_copy(
            update={
                'wall_ids': _replace_binding_walls(
                    binding.wall_ids,
                    {wall.wall_id: (first.wall_id, second.wall_id)},
                )
            }
        )
        for binding in topology.constraint_bindings
    )
    candidate = WallTopology(
        walls=tuple(walls),
        openings=tuple(migrated_openings),
        constraint_bindings=migrated_bindings,
    )
    return split_room, validate_wall_topology(split_room, candidate)


def merge_walls(
    room: RoomPrism,
    topology: WallTopology,
    first_wall_id: str,
    second_wall_id: str,
    *,
    merged_wall_id: str,
) -> tuple[RoomPrism, WallTopology]:
    first = _wall(topology, first_wall_id)
    second = _wall(topology, second_wall_id)
    if first.to_vertex_id != second.from_vertex_id:
        raise WallTopologyError('walls must be ordered neighbors to merge')
    if abs(first.thickness_m - second.thickness_m) > 1e-9:
        raise WallTopologyError('walls with different thickness cannot be merged implicitly')

    vertices_by_id = _vertex_map(room)
    a = vertices_by_id[first.from_vertex_id]
    b = vertices_by_id[first.to_vertex_id]
    c = vertices_by_id[second.to_vertex_id]
    ab = (b.x_m - a.x_m, b.y_m - a.y_m)
    bc = (c.x_m - b.x_m, c.y_m - b.y_m)
    cross = ab[0] * bc[1] - ab[1] * bc[0]
    dot = ab[0] * bc[0] + ab[1] * bc[1]
    scale = max(hypot(*ab) * hypot(*bc), 1.0)
    if abs(cross) > 1e-9 * scale or dot <= 0.0:
        raise WallTopologyError('only collinear walls with the same direction can be merged')

    first_length = wall_length(room, first)
    vertices = [vertex for vertex in room_vertices(room) if vertex.vertex_id != first.to_vertex_id]
    merged_room = make_polygon_room(vertices, height_m=room.height_m, room_id=room.room_id)
    source_wall_id = first.source_wall_id if first.source_wall_id == second.source_wall_id else None
    merged = WallSegment(
        wall_id=merged_wall_id,
        from_vertex_id=first.from_vertex_id,
        to_vertex_id=second.to_vertex_id,
        thickness_m=first.thickness_m,
        source_wall_id=source_wall_id,
    )
    walls = list(topology.walls)
    first_index = walls.index(first)
    second_index = walls.index(second)
    if second_index != first_index + 1:
        raise WallTopologyError('walls must be adjacent in topology order to merge')
    walls[first_index:second_index + 1] = [merged]

    migrated_openings: list[WallOpening] = []
    for opening in topology.openings:
        if opening.wall_id == first.wall_id:
            migrated_openings.append(opening.model_copy(update={'wall_id': merged.wall_id}))
        elif opening.wall_id == second.wall_id:
            migrated_openings.append(
                opening.model_copy(
                    update={'wall_id': merged.wall_id, 'offset_m': first_length + opening.offset_m}
                )
            )
        else:
            migrated_openings.append(opening)
    replacements = {
        first.wall_id: (merged.wall_id,),
        second.wall_id: (merged.wall_id,),
    }
    migrated_bindings = tuple(
        binding.model_copy(update={'wall_ids': _replace_binding_walls(binding.wall_ids, replacements)})
        for binding in topology.constraint_bindings
    )
    candidate = WallTopology(
        walls=tuple(walls),
        openings=tuple(migrated_openings),
        constraint_bindings=migrated_bindings,
    )
    return merged_room, validate_wall_topology(merged_room, candidate)


def delete_wall(
    room: RoomPrism,
    topology: WallTopology,
    wall_id: str,
    *,
    replacement_wall_id: str,
) -> tuple[RoomPrism, WallTopology]:
    """Delete one boundary wall by removing its end vertex.

    The selected wall and its successor become one replacement wall. The
    operation is intentionally conservative: references on either affected
    wall or conflicting thickness stop the commit instead of being guessed.
    """

    vertices = list(room_vertices(room))
    if len(vertices) <= 3:
        raise WallTopologyError('a room must keep at least three walls')
    wall = _wall(topology, wall_id)
    wall_index = _edge_index(room, wall)
    successor_pair = _edge_pairs(room)[(wall_index + 1) % len(vertices)]
    successor = next(
        (candidate for candidate in topology.walls if (candidate.from_vertex_id, candidate.to_vertex_id) == successor_pair),
        None,
    )
    if successor is None:
        raise WallTopologyError('successor wall is missing from topology')
    if abs(wall.thickness_m - successor.thickness_m) > 1e-9:
        raise WallTopologyError('affected walls have different thickness; deletion requires explicit resolution')
    affected = {wall.wall_id, successor.wall_id}
    referenced_openings = [opening.opening_id for opening in topology.openings if opening.wall_id in affected]
    if referenced_openings:
        raise WallTopologyError(
            f'wall deletion would orphan openings {referenced_openings}; reassign or remove them first'
        )
    referenced_bindings = [
        binding.binding_id
        for binding in topology.constraint_bindings
        if any(bound_wall in affected for bound_wall in binding.wall_ids)
    ]
    if referenced_bindings:
        raise WallTopologyError(
            f'wall deletion would orphan constraint bindings {referenced_bindings}; resolve them first'
        )

    removed_vertex_id = wall.to_vertex_id
    remaining_vertices = [vertex for vertex in vertices if vertex.vertex_id != removed_vertex_id]
    try:
        deleted_room = make_polygon_room(
            remaining_vertices,
            height_m=room.height_m,
            room_id=room.room_id,
        )
    except ValueError as exc:
        raise WallTopologyError(f'wall deletion would create invalid room geometry: {exc}') from exc

    replacement = WallSegment(
        wall_id=replacement_wall_id,
        from_vertex_id=wall.from_vertex_id,
        to_vertex_id=successor.to_vertex_id,
        thickness_m=wall.thickness_m,
    )
    survivors = {
        (candidate.from_vertex_id, candidate.to_vertex_id): candidate
        for candidate in topology.walls
        if candidate.wall_id not in affected
    }
    survivors[(replacement.from_vertex_id, replacement.to_vertex_id)] = replacement
    ordered_walls: list[WallSegment] = []
    for pair in _edge_pairs(deleted_room):
        candidate = survivors.get(pair)
        if candidate is None:
            raise WallTopologyError(f'wall deletion cannot map replacement boundary edge {pair}')
        ordered_walls.append(candidate)
    candidate_topology = WallTopology(
        walls=tuple(ordered_walls),
        openings=topology.openings,
        constraint_bindings=topology.constraint_bindings,
    )
    return deleted_room, validate_wall_topology(deleted_room, candidate_topology)



def update_wall_thickness(
    room: RoomPrism,
    topology: WallTopology,
    wall_id: str,
    *,
    thickness_m: float,
) -> WallTopology:
    """Return a topology with one wall thickness changed and fully revalidated."""

    wall = _wall(topology, wall_id)
    replacement = wall.model_copy(update={'thickness_m': float(thickness_m)})
    candidate = WallTopology(
        walls=tuple(replacement if item.wall_id == wall_id else item for item in topology.walls),
        openings=topology.openings,
        constraint_bindings=topology.constraint_bindings,
    )
    return validate_wall_topology(room, candidate)


def update_opening(
    room: RoomPrism,
    topology: WallTopology,
    opening: WallOpening,
) -> WallTopology:
    """Replace one existing opening by ID and revalidate wall-local dimensions."""

    if not any(item.opening_id == opening.opening_id for item in topology.openings):
        raise WallTopologyError(f'unknown opening: {opening.opening_id}')
    candidate = WallTopology(
        walls=topology.walls,
        openings=tuple(
            opening if item.opening_id == opening.opening_id else item
            for item in topology.openings
        ),
        constraint_bindings=topology.constraint_bindings,
    )
    return validate_wall_topology(room, candidate)


def delete_opening(
    room: RoomPrism,
    topology: WallTopology,
    opening_id: str,
) -> WallTopology:
    """Delete one opening without changing wall identities or bindings."""

    if not any(item.opening_id == opening_id for item in topology.openings):
        raise WallTopologyError(f'unknown opening: {opening_id}')
    candidate = WallTopology(
        walls=topology.walls,
        openings=tuple(
            item for item in topology.openings if item.opening_id != opening_id
        ),
        constraint_bindings=topology.constraint_bindings,
    )
    return validate_wall_topology(room, candidate)

def _replace_binding_walls(
    wall_ids: tuple[str, ...],
    replacements: dict[str, tuple[str, ...]],
) -> tuple[str, ...]:
    result: list[str] = []
    for wall_id in wall_ids:
        for replacement in replacements.get(wall_id, (wall_id,)):
            if replacement not in result:
                result.append(replacement)
    return tuple(result)


def _wall(topology: WallTopology, wall_id: str) -> WallSegment:
    for wall in topology.walls:
        if wall.wall_id == wall_id:
            return wall
    raise WallTopologyError(f'unknown wall: {wall_id}')


def _edge_index(room: RoomPrism, wall: WallSegment) -> int:
    for index, pair in enumerate(_edge_pairs(room)):
        if pair == (wall.from_vertex_id, wall.to_vertex_id):
            return index
    raise WallTopologyError(f'wall {wall.wall_id} does not match the room boundary')
