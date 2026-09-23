import pytest

from htdt.cad_scene import RoomVertex, make_polygon_room
from htdt.cad_wall_models import WallConstraintBinding
from htdt.cad_walls import (
    WallTopologyError,
    add_constraint_binding,
    delete_wall,
    make_wall_topology,
    merge_walls,
    split_wall,
)


def _room():
    return make_polygon_room(
        (
            RoomVertex(vertex_id='a', x_m=0.0, y_m=0.0),
            RoomVertex(vertex_id='b', x_m=6.0, y_m=0.0),
            RoomVertex(vertex_id='c', x_m=6.0, y_m=4.0),
            RoomVertex(vertex_id='d', x_m=0.0, y_m=4.0),
        ),
        height_m=2.4,
    )


def test_clearance_binding_follows_split_and_merge_without_changing_value() -> None:
    room = _room()
    topology = add_constraint_binding(
        room,
        make_wall_topology(room),
        WallConstraintBinding(
            binding_id='clearance-front',
            wall_ids=('wall:a->b',),
            clearance_m=0.35,
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
    binding = split_topology.constraint_bindings[0]
    assert binding.binding_id == 'clearance-front'
    assert binding.wall_ids == ('wall-a-mid', 'wall-mid-b')
    assert binding.clearance_m == pytest.approx(0.35)

    _, merged_topology = merge_walls(
        split_room,
        split_topology,
        'wall-a-mid',
        'wall-mid-b',
        merged_wall_id='wall-ab-merged',
    )
    merged_binding = merged_topology.constraint_bindings[0]
    assert merged_binding.binding_id == 'clearance-front'
    assert merged_binding.wall_ids == ('wall-ab-merged',)
    assert merged_binding.clearance_m == pytest.approx(0.35)


def test_delete_rejects_wall_pair_with_constraint_binding() -> None:
    room = _room()
    topology = add_constraint_binding(
        room,
        make_wall_topology(room),
        WallConstraintBinding(
            binding_id='clearance-next',
            wall_ids=('wall:b->c',),
            clearance_m=0.25,
        ),
    )

    with pytest.raises(WallTopologyError, match='constraint bindings'):
        delete_wall(
            room,
            topology,
            'wall:a->b',
            replacement_wall_id='wall:a->c',
        )
