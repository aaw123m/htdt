from pathlib import Path
import json

import pytest

from htdt.cad_repository import SceneRepository
from htdt.cad_room import RoomWorkingDocument
from htdt.cad_scene import (
    RoomVertex,
    SceneDocument,
    canonical_scene_json,
    make_empty_scene,
    make_f1_scene,
    make_polygon_room,
)
from htdt.cad_wall_models import WallOpening
from htdt.cad_walls import add_opening, make_wall_topology


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


def _room_and_topology():
    room = _room()
    topology = add_opening(
        room,
        make_wall_topology(room),
        WallOpening(
            opening_id='door-1',
            wall_id='wall:a->b',
            offset_m=1.0,
            width_m=0.9,
            height_m=2.0,
            kind='door',
            is_open=True,
        ),
    )
    return room, topology


def _room_with_renamed_vertex():
    return make_polygon_room(
        (
            RoomVertex(vertex_id='a', x_m=0.0, y_m=0.0),
            RoomVertex(vertex_id='b2', x_m=6.0, y_m=0.0),
            RoomVertex(vertex_id='c', x_m=6.0, y_m=4.0),
            RoomVertex(vertex_id='d', x_m=0.0, y_m=4.0),
        ),
        height_m=2.4,
    )


def test_legacy_scene_canonical_json_does_not_gain_wall_topology_field() -> None:
    payload = json.loads(canonical_scene_json(make_f1_scene()))
    assert 'wall_topology' not in payload


def test_wall_topology_requires_schema_v3_and_matching_room_boundary() -> None:
    room, topology = _room_and_topology()
    base = make_empty_scene('wall-scene')

    with pytest.raises(ValueError, match='schema_version >= 3'):
        SceneDocument.model_validate(
            base.model_copy(
                update={'room': room, 'wall_topology': topology}
            ).model_dump(mode='python')
        )

    document = SceneDocument.model_validate(
        base.model_copy(
            update={'schema_version': 3, 'room': room, 'wall_topology': topology}
        ).model_dump(mode='python')
    )
    assert document.wall_topology == topology

    with pytest.raises(ValueError, match='one-to-one'):
        SceneDocument.model_validate(
            document.model_copy(update={'room': _room_with_renamed_vertex()}).model_dump(mode='python')
        )


def test_room_and_topology_commit_is_one_undo_redo_transaction() -> None:
    room, topology = _room_and_topology()
    working = RoomWorkingDocument(make_empty_scene('wall-scene'))

    assert working.replace_room_topology(room, topology)
    assert working.history_length == 1
    committed = working.committed_document
    assert committed.schema_version == 3
    assert committed.room == room
    assert committed.wall_topology == topology

    assert working.undo()
    assert working.committed_document.room is None
    assert working.committed_document.wall_topology is None
    assert working.redo()
    assert working.committed_document == committed


def test_plain_room_edit_cannot_leave_existing_topology_dangling() -> None:
    room, topology = _room_and_topology()
    working = RoomWorkingDocument(make_empty_scene('wall-scene'))
    assert working.replace_room_topology(room, topology)
    before = working.committed_document
    before_history = working.history_length

    with pytest.raises(ValueError, match='one-to-one'):
        working.replace_room(_room_with_renamed_vertex())

    assert working.committed_document == before
    assert working.history_length == before_history


def test_wall_scene_round_trips_through_revision_repository(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    initial = repository.save(make_empty_scene('wall-scene'), parent_revision_id=None).revision
    room, topology = _room_and_topology()
    working = RoomWorkingDocument(
        initial.document,
        source_revision_id=initial.revision_id,
        saved_content_hash=initial.content_hash,
    )
    assert working.replace_room_topology(room, topology)

    saved = repository.save(working.committed_document, parent_revision_id=initial.revision_id).revision
    reopened = repository.latest('wall-scene')

    assert reopened is not None
    assert reopened.revision_id == saved.revision_id
    assert reopened.document.schema_version == 3
    assert reopened.document.room == room
    assert reopened.document.wall_topology == topology
