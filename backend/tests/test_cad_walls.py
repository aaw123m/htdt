import pytest

from htdt.cad_scene import RoomVertex, make_polygon_room
from htdt.cad_walls import (
    WallOpening,
    WallTopologyError,
    add_opening,
    delete_opening,
    delete_wall,
    make_wall_topology,
    merge_walls,
    move_wall,
    split_wall,
    update_opening,
    update_wall_thickness,
    validate_wall_topology,
)


def _rect_room():
    return make_polygon_room(
        (
            RoomVertex(vertex_id='a', x_m=0.0, y_m=0.0),
            RoomVertex(vertex_id='b', x_m=6.0, y_m=0.0),
            RoomVertex(vertex_id='c', x_m=6.0, y_m=4.0),
            RoomVertex(vertex_id='d', x_m=0.0, y_m=4.0),
        ),
        height_m=2.4,
    )


def test_topology_maps_one_wall_to_each_ordered_room_edge() -> None:
    room = _rect_room()
    topology = make_wall_topology(room, thickness_m=0.12)

    assert [wall.wall_id for wall in topology.walls] == [
        'wall:a->b',
        'wall:b->c',
        'wall:c->d',
        'wall:d->a',
    ]
    assert all(wall.thickness_m == pytest.approx(0.12) for wall in topology.walls)
    assert validate_wall_topology(room, topology) == topology


def test_opening_must_fit_wall_and_room_height() -> None:
    room = _rect_room()
    topology = make_wall_topology(room)
    topology = add_opening(
        room,
        topology,
        WallOpening(
            opening_id='door-1',
            wall_id='wall:a->b',
            offset_m=1.0,
            width_m=0.9,
            sill_m=0.0,
            height_m=2.0,
            kind='door',
            is_open=True,
        ),
    )
    assert topology.openings[0].wall_id == 'wall:a->b'

    with pytest.raises(WallTopologyError, match='exceeds wall'):
        add_opening(
            room,
            topology,
            WallOpening(
                opening_id='bad-width',
                wall_id='wall:a->b',
                offset_m=5.5,
                width_m=1.0,
                height_m=2.0,
            ),
        )

    with pytest.raises(WallTopologyError, match='exceeds room height'):
        add_opening(
            room,
            topology,
            WallOpening(
                opening_id='bad-height',
                wall_id='wall:b->c',
                offset_m=0.5,
                width_m=1.0,
                sill_m=1.0,
                height_m=1.5,
            ),
        )


def test_move_wall_preserves_wall_and_opening_ids() -> None:
    room = _rect_room()
    topology = add_opening(
        room,
        make_wall_topology(room),
        WallOpening(
            opening_id='door-1',
            wall_id='wall:a->b',
            offset_m=1.0,
            width_m=0.9,
            height_m=2.0,
        ),
    )

    moved_room, moved_topology = move_wall(
        room,
        topology,
        'wall:a->b',
        delta_x_m=0.0,
        delta_y_m=-0.5,
    )

    vertices = {vertex.vertex_id: vertex for vertex in moved_room.footprint_vertices or ()}
    assert vertices['a'].y_m == pytest.approx(-0.5)
    assert vertices['b'].y_m == pytest.approx(-0.5)
    assert moved_topology == topology
    assert moved_topology.openings[0].opening_id == 'door-1'


def test_split_migrates_openings_to_children_with_wall_local_offsets() -> None:
    room = _rect_room()
    topology = make_wall_topology(room)
    topology = add_opening(
        room,
        topology,
        WallOpening(
            opening_id='left-window',
            wall_id='wall:a->b',
            offset_m=0.5,
            width_m=1.0,
            sill_m=0.8,
            height_m=1.0,
            kind='window',
        ),
    )
    topology = add_opening(
        room,
        topology,
        WallOpening(
            opening_id='right-door',
            wall_id='wall:a->b',
            offset_m=4.0,
            width_m=1.0,
            height_m=2.0,
            kind='door',
        ),
    )

    split_room, split_topology = split_wall(
        room,
        topology,
        'wall:a->b',
        offset_m=3.0,
        new_vertex_id='ab-mid',
        first_wall_id='wall-a-mid',
        second_wall_id='wall-mid-b',
    )

    assert [vertex.vertex_id for vertex in split_room.footprint_vertices or ()] == ['a', 'ab-mid', 'b', 'c', 'd']
    first, second = split_topology.walls[:2]
    assert first.source_wall_id == 'wall:a->b'
    assert second.source_wall_id == 'wall:a->b'
    openings = {opening.opening_id: opening for opening in split_topology.openings}
    assert openings['left-window'].wall_id == 'wall-a-mid'
    assert openings['left-window'].offset_m == pytest.approx(0.5)
    assert openings['right-door'].wall_id == 'wall-mid-b'
    assert openings['right-door'].offset_m == pytest.approx(1.0)


def test_split_rejects_opening_that_crosses_split_point() -> None:
    room = _rect_room()
    topology = add_opening(
        room,
        make_wall_topology(room),
        WallOpening(
            opening_id='crossing-door',
            wall_id='wall:a->b',
            offset_m=2.5,
            width_m=1.0,
            height_m=2.0,
        ),
    )

    with pytest.raises(WallTopologyError, match='crosses split point'):
        split_wall(
            room,
            topology,
            'wall:a->b',
            offset_m=3.0,
            new_vertex_id='ab-mid',
            first_wall_id='wall-a-mid',
            second_wall_id='wall-mid-b',
        )


def test_merge_restores_collinear_boundary_and_migrates_openings() -> None:
    room = _rect_room()
    topology = add_opening(
        room,
        make_wall_topology(room),
        WallOpening(
            opening_id='door-1',
            wall_id='wall:a->b',
            offset_m=4.0,
            width_m=1.0,
            height_m=2.0,
        ),
    )
    split_room, split_topology = split_wall(
        room,
        topology,
        'wall:a->b',
        offset_m=3.0,
        new_vertex_id='ab-mid',
        first_wall_id='wall-a-mid',
        second_wall_id='wall-mid-b',
    )

    merged_room, merged_topology = merge_walls(
        split_room,
        split_topology,
        'wall-a-mid',
        'wall-mid-b',
        merged_wall_id='wall-ab-merged',
    )

    assert [vertex.vertex_id for vertex in merged_room.footprint_vertices or ()] == ['a', 'b', 'c', 'd']
    merged = merged_topology.walls[0]
    assert merged.wall_id == 'wall-ab-merged'
    opening = merged_topology.openings[0]
    assert opening.wall_id == 'wall-ab-merged'
    assert opening.offset_m == pytest.approx(4.0)


def test_merge_rejects_non_collinear_or_different_thickness_walls() -> None:
    room = _rect_room()
    topology = make_wall_topology(room)

    with pytest.raises(WallTopologyError, match='collinear'):
        merge_walls(room, topology, 'wall:a->b', 'wall:b->c', merged_wall_id='bad-corner')

    split_room, split_topology = split_wall(
        room,
        topology,
        'wall:a->b',
        offset_m=3.0,
        new_vertex_id='ab-mid',
        first_wall_id='wall-a-mid',
        second_wall_id='wall-mid-b',
    )
    changed_walls = list(split_topology.walls)
    changed_walls[1] = changed_walls[1].model_copy(update={'thickness_m': 0.2})
    changed = split_topology.model_copy(update={'walls': tuple(changed_walls)})

    with pytest.raises(WallTopologyError, match='different thickness'):
        merge_walls(
            split_room,
            changed,
            'wall-a-mid',
            'wall-mid-b',
            merged_wall_id='bad-thickness',
        )


def test_delete_wall_removes_end_vertex_and_creates_explicit_replacement_wall() -> None:
    room = _rect_room()
    topology = make_wall_topology(room)

    deleted_room, deleted_topology = delete_wall(
        room,
        topology,
        'wall:a->b',
        replacement_wall_id='wall:a->c',
    )

    assert [vertex.vertex_id for vertex in deleted_room.footprint_vertices or ()] == ['a', 'c', 'd']
    assert [wall.wall_id for wall in deleted_topology.walls] == [
        'wall:a->c',
        'wall:c->d',
        'wall:d->a',
    ]
    assert validate_wall_topology(deleted_room, deleted_topology) == deleted_topology


def test_delete_wall_rejects_referenced_opening_in_affected_pair() -> None:
    room = _rect_room()
    topology = add_opening(
        room,
        make_wall_topology(room),
        WallOpening(
            opening_id='door-next',
            wall_id='wall:b->c',
            offset_m=1.0,
            width_m=0.9,
            height_m=2.0,
        ),
    )

    with pytest.raises(WallTopologyError, match='would orphan openings'):
        delete_wall(
            room,
            topology,
            'wall:a->b',
            replacement_wall_id='wall:a->c',
        )


def test_update_wall_thickness_reuses_topology_validation() -> None:
    room = _rect_room()
    topology = make_wall_topology(room)

    changed = update_wall_thickness(
        room,
        topology,
        "wall:a->b",
        thickness_m=0.18,
    )

    assert changed.walls[0].thickness_m == pytest.approx(0.18)
    assert changed.walls[1:] == topology.walls[1:]
    assert validate_wall_topology(room, changed) == changed


def test_update_and_delete_opening_preserve_wall_topology_authority() -> None:
    room = _rect_room()
    topology = add_opening(
        room,
        make_wall_topology(room),
        WallOpening(
            opening_id="door-1",
            wall_id="wall:a->b",
            offset_m=1.0,
            width_m=0.9,
            height_m=2.0,
        ),
    )
    replacement = topology.openings[0].model_copy(
        update={
            "offset_m": 1.5,
            "width_m": 1.1,
            "kind": "passage",
            "is_open": True,
        }
    )

    changed = update_opening(room, topology, replacement)
    assert changed.openings == (replacement,)
    assert changed.walls == topology.walls

    deleted = delete_opening(room, changed, "door-1")
    assert deleted.openings == ()
    assert deleted.walls == topology.walls

    with pytest.raises(WallTopologyError, match="unknown opening"):
        delete_opening(room, deleted, "door-1")
