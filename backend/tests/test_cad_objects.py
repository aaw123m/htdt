from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_document import EditStateError, WorkingDocument
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Offset3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
    acoustic_reference_position,
    make_f1_scene,
    quaternion_from_euler_deg,
)


def test_physical_entities_require_dimensions_and_measurement_points_stay_reference_only() -> None:
    with pytest.raises(ValidationError, match='size_m is required'):
        SceneEntity(
            entity_id='seat-no-size',
            kind='seat',
            name='Seat',
            position=Position3(x_m=1.0, y_m=2.0, z_m=0.4),
        )

    point = SceneEntity(
        entity_id='point-a',
        kind='measurement_point',
        name='Point A',
        position=Position3(x_m=1.0, y_m=2.0, z_m=1.1),
    )
    assert acoustic_reference_position(point) == point.position

    with pytest.raises(ValidationError, match='measurement points do not have physical size_m'):
        SceneEntity(
            entity_id='point-sized',
            kind='measurement_point',
            name='Invalid point',
            position=Position3(x_m=1.0, y_m=2.0, z_m=1.1),
            size_m=Size3(x_m=0.1, y_m=0.1, z_m=0.1),
        )


def test_acoustic_reference_offset_is_body_local_and_rotates_with_pose() -> None:
    seat = SceneEntity(
        entity_id='seat-main',
        kind='seat',
        name='Main seat',
        position=Position3(x_m=1.0, y_m=2.0, z_m=0.45),
        orientation=quaternion_from_euler_deg(yaw_deg=90.0, pitch_deg=0.0, roll_deg=0.0),
        size_m=Size3(x_m=0.7, y_m=0.8, z_m=0.9),
        acoustic_reference_offset_m=Offset3(x_m=0.10, y_m=0.0, z_m=0.65),
    )

    reference = acoustic_reference_position(seat)
    assert reference is not None
    assert reference.x_m == pytest.approx(1.0)
    assert reference.y_m == pytest.approx(2.10)
    assert reference.z_m == pytest.approx(1.10)


def test_add_duplicate_and_property_update_are_individual_undo_units() -> None:
    working = WorkingDocument(make_f1_scene())
    seat = SceneEntity(
        entity_id='seat-main',
        kind='seat',
        name='Main seat',
        position=Position3(x_m=3.0, y_m=3.0, z_m=0.45),
        size_m=Size3(x_m=0.7, y_m=0.8, z_m=0.9),
        acoustic_reference_offset_m=Offset3(x_m=0.0, y_m=0.0, z_m=0.65),
    )

    assert working.add_entity(seat)
    assert working.history_length == 1
    assert working.committed_document.entity('seat-main') == seat
    assert working.undo()
    with pytest.raises(KeyError):
        working.committed_document.entity('seat-main')
    assert working.redo()
    assert working.committed_document.entity('seat-main') == seat

    source = working.committed_document.entity('speaker-fl')
    duplicate_position = Position3(x_m=1.7, y_m=0.9, z_m=1.05)
    assert working.duplicate_entity(
        source.entity_id,
        new_entity_id='speaker-fl-copy',
        name='Front Left Copy',
        position=duplicate_position,
    )
    duplicate = working.committed_document.entity('speaker-fl-copy')
    assert duplicate.position == duplicate_position
    assert duplicate.size_m == source.size_m
    assert duplicate.orientation == source.orientation
    assert duplicate.speaker_role == source.speaker_role
    assert duplicate.aim_xyz is None

    resized = Size3(x_m=0.30, y_m=0.32, z_m=0.48)
    assert working.update_entity('speaker-fl-copy', size_m=resized, speaker_role='FL2')
    updated = working.committed_document.entity('speaker-fl-copy')
    assert updated.size_m == resized
    assert updated.speaker_role == 'FL2'
    assert updated.position == duplicate_position
    assert updated.aim_xyz is None
    assert working.undo()
    assert working.committed_document.entity('speaker-fl-copy') == duplicate

    with pytest.raises(EditStateError, match='entity_id cannot be changed'):
        working.update_entity('speaker-fl-copy', entity_id='renamed')


def test_batch_add_is_one_atomic_undo_unit() -> None:
    working = WorkingDocument(make_f1_scene())
    before_ids = tuple(entity.entity_id for entity in working.committed_document.entities)
    screen = SceneEntity(
        entity_id='screen-template',
        kind='screen',
        name='Screen',
        position=Position3(x_m=3.0, y_m=0.2, z_m=1.3),
        size_m=Size3(x_m=2.4, y_m=0.04, z_m=1.35),
    )
    seat = SceneEntity(
        entity_id='seat-template',
        kind='seat',
        name='Seat',
        position=Position3(x_m=3.0, y_m=3.0, z_m=0.45),
        size_m=Size3(x_m=0.7, y_m=0.8, z_m=0.9),
        acoustic_reference_offset_m=Offset3(z_m=0.65),
    )

    assert working.add_entities((screen, seat))
    assert working.history_length == 1
    assert tuple(entity.entity_id for entity in working.committed_document.entities)[-2:] == (
        'screen-template',
        'seat-template',
    )
    assert working.undo()
    assert tuple(entity.entity_id for entity in working.committed_document.entities) == before_ids
    assert working.redo()
    assert working.committed_document.entity('screen-template') == screen
    assert working.committed_document.entity('seat-template') == seat


def test_n40_entities_round_trip_exactly_through_scene_repository(tmp_path: Path) -> None:
    document = SceneDocument(
        document_id='fixture-n40-roundtrip',
        schema_version=3,
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='Front Left',
                position=Position3(x_m=1.4, y_m=0.8, z_m=1.0),
                size_m=Size3(x_m=0.24, y_m=0.28, z_m=0.42),
                acoustic_reference_offset_m=Offset3(x_m=0.0, y_m=0.12, z_m=0.0),
                speaker_role='FL',
                aim_xyz=None,
            ),
            SceneEntity(
                entity_id='seat-main',
                kind='seat',
                name='Main seat',
                position=Position3(x_m=3.0, y_m=3.0, z_m=0.45),
                size_m=Size3(x_m=0.7, y_m=0.8, z_m=0.9),
                acoustic_reference_offset_m=Offset3(x_m=0.0, y_m=0.0, z_m=0.65),
            ),
            SceneEntity(
                entity_id='screen-main',
                kind='screen',
                name='Screen',
                position=Position3(x_m=3.0, y_m=0.15, z_m=1.35),
                size_m=Size3(x_m=2.6, y_m=0.04, z_m=1.46),
            ),
            SceneEntity(
                entity_id='furniture-sofa',
                kind='furniture',
                name='Sofa',
                position=Position3(x_m=3.0, y_m=3.2, z_m=0.45),
                size_m=Size3(x_m=2.1, y_m=0.9, z_m=0.9),
            ),
            SceneEntity(
                entity_id='av-rack',
                kind='av_equipment',
                name='AV rack',
                position=Position3(x_m=5.4, y_m=1.1, z_m=0.65),
                size_m=Size3(x_m=0.6, y_m=0.55, z_m=1.3),
            ),
            SceneEntity(
                entity_id='point-mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=3.0, y_m=3.0, z_m=1.1),
            ),
        ),
    )

    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    saved = repository.save(document, parent_revision_id=None)
    assert saved.created
    reopened = repository.latest(document.document_id)
    assert reopened is not None
    assert reopened.document == document
    assert reopened.document.entity('speaker-fl').speaker_role == 'FL'
    assert reopened.document.entity('seat-main').acoustic_reference_offset_m == Offset3(
        x_m=0.0, y_m=0.0, z_m=0.65
    )