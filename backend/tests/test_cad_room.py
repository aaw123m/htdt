from pathlib import Path
import json

import pytest

from htdt.cad_repository import SceneRepository
from htdt.cad_room import RoomWorkingDocument
from htdt.cad_scene import (
    RoomPrism,
    RoomVertex,
    canonical_scene_json,
    make_empty_scene,
    make_f1_scene,
    make_polygon_room,
    room_vertices,
)


def _f2_vertices() -> tuple[RoomVertex, ...]:
    points = (
        (0.0, 0.0),
        (6.0, 0.0),
        (6.0, 4.0),
        (4.0, 4.0),
        (4.0, 2.0),
        (2.0, 2.0),
        (2.0, 4.0),
        (0.0, 4.0),
    )
    return tuple(
        RoomVertex(vertex_id=f'v{index + 1}', x_m=x_m, y_m=y_m)
        for index, (x_m, y_m) in enumerate(points)
    )


def test_f2_concave_room_has_stable_vertices_and_derived_bounds() -> None:
    room = make_polygon_room(_f2_vertices(), height_m=2.4)
    assert room.width_m == pytest.approx(6.0)
    assert room.depth_m == pytest.approx(4.0)
    assert room.height_m == pytest.approx(2.4)
    assert room.bounds_m == pytest.approx((0.0, 0.0, 6.0, 4.0))
    assert tuple(vertex.vertex_id for vertex in room_vertices(room)) == tuple(f'v{i}' for i in range(1, 9))


def test_polygon_room_bounds_are_derived_and_allow_negative_coordinates() -> None:
    room = make_polygon_room(
        (
            RoomVertex(vertex_id='a', x_m=-1.5, y_m=-0.5),
            RoomVertex(vertex_id='b', x_m=2.0, y_m=-0.5),
            RoomVertex(vertex_id='c', x_m=2.0, y_m=1.5),
            RoomVertex(vertex_id='d', x_m=-1.5, y_m=1.5),
        ),
        height_m=2.7,
    )
    assert room.bounds_m == pytest.approx((-1.5, -0.5, 2.0, 1.5))
    assert room.width_m == pytest.approx(3.5)
    assert room.depth_m == pytest.approx(2.0)


def test_self_intersection_and_duplicate_vertices_cannot_be_committed() -> None:
    with pytest.raises(ValueError, match='Invalid room polygon'):
        make_polygon_room(
            (
                RoomVertex(vertex_id='a', x_m=0.0, y_m=0.0),
                RoomVertex(vertex_id='b', x_m=2.0, y_m=2.0),
                RoomVertex(vertex_id='c', x_m=0.0, y_m=2.0),
                RoomVertex(vertex_id='d', x_m=2.0, y_m=0.0),
            ),
            height_m=2.4,
        )

    with pytest.raises(ValueError, match='duplicate vertices'):
        make_polygon_room(
            (
                RoomVertex(vertex_id='a', x_m=0.0, y_m=0.0),
                RoomVertex(vertex_id='b', x_m=2.0, y_m=0.0),
                RoomVertex(vertex_id='c', x_m=2.0, y_m=0.0),
                RoomVertex(vertex_id='d', x_m=0.0, y_m=2.0),
            ),
            height_m=2.4,
        )


def test_room_commit_is_one_undo_and_redo_restores_exact_room() -> None:
    working = RoomWorkingDocument(make_empty_scene('fixture-f2'))
    room = make_polygon_room(_f2_vertices(), height_m=2.4)

    assert working.replace_room(room)
    assert working.history_length == 1
    assert working.committed_document.room == room
    assert working.committed_document.schema_version == 2

    assert working.undo()
    assert working.committed_document.room is None
    assert working.redo()
    assert working.committed_document.room == room


def test_room_edit_noop_does_not_add_history() -> None:
    room = make_polygon_room(_f2_vertices(), height_m=2.4)
    document = make_empty_scene('fixture-f2').model_copy(update={'room': room})
    working = RoomWorkingDocument(document)

    assert not working.replace_room(room)
    assert working.history_length == 0


def test_invalid_room_replacement_leaves_document_and_history_unchanged() -> None:
    room = make_polygon_room(_f2_vertices(), height_m=2.4)
    document = make_empty_scene('fixture-f2').model_copy(update={'room': room})
    working = RoomWorkingDocument(document)
    invalid = RoomPrism.model_construct(
        room_id='room',
        width_m=2.0,
        depth_m=2.0,
        height_m=2.4,
        footprint_vertices=(
            RoomVertex(vertex_id='a', x_m=0.0, y_m=0.0),
            RoomVertex(vertex_id='b', x_m=2.0, y_m=2.0),
            RoomVertex(vertex_id='c', x_m=0.0, y_m=2.0),
            RoomVertex(vertex_id='d', x_m=2.0, y_m=0.0),
        ),
    )

    with pytest.raises(ValueError, match='Invalid room polygon'):
        working.replace_room(invalid)

    assert working.committed_document == document
    assert working.history_length == 0


def test_polygon_room_revision_round_trips_through_repository(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    initial = repository.save(make_empty_scene('fixture-f2'), parent_revision_id=None).revision
    working = RoomWorkingDocument(
        initial.document,
        source_revision_id=initial.revision_id,
        saved_content_hash=initial.content_hash,
    )
    room = make_polygon_room(_f2_vertices(), height_m=2.4)
    assert working.replace_room(room)

    saved = repository.save(working.committed_document, parent_revision_id=initial.revision_id).revision
    reopened = repository.latest('fixture-f2')

    assert reopened is not None
    assert reopened.revision_id == saved.revision_id
    assert reopened.document.room == room
    assert tuple(vertex.vertex_id for vertex in room_vertices(reopened.document.room)) == tuple(
        vertex.vertex_id for vertex in _f2_vertices()
    )
    assert reopened.document.room.bounds_m == pytest.approx((0.0, 0.0, 6.0, 4.0))


def test_legacy_f1_rectangle_serialization_omits_new_optional_footprint_field() -> None:
    payload = json.loads(canonical_scene_json(make_f1_scene()))
    assert payload['room'] == {
        'depth_m': 4.0,
        'height_m': 2.4,
        'room_id': 'room',
        'width_m': 6.0,
    }
    assert len(room_vertices(make_f1_scene().room)) == 4
