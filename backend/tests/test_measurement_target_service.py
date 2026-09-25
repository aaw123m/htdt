from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Offset3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.measurement_target_service import (
    TARGET_PATTERN_PRESETS,
    MeasurementTargetService,
)


DOCUMENT_ID = 'o987-target-fixture'


def _seat(entity_id: str, y_m: float) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='seat',
        name=entity_id,
        position=Position3(x_m=0.0, y_m=y_m, z_m=0.5),
        size_m=Size3(x_m=0.60, y_m=0.80, z_m=1.0),
        acoustic_reference_offset_m=Offset3(z_m=0.5),
    )


def _fixture(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    document = SceneDocument(
        document_id=DOCUMENT_ID,
        schema_version=2,
        room=RoomPrism(width_m=6.0, depth_m=6.0, height_m=2.5),
        entities=(_seat('seat-mlp', 2.0),),
    )
    revision = scene_repository.save(
        document, parent_revision_id=None
    ).revision
    service = MeasurementTargetService(scene_repository, DOCUMENT_ID)
    return scene_repository, revision, service


def test_create_preview_materialize_reopen(tmp_path):
    scene_repository, revision, service = _fixture(tmp_path)
    pattern = service.create_pattern(
        anchor_kind='seat',
        anchor_entity_id='seat-mlp',
        preset='mlp_cross',
        spacing_m=0.10,
    )
    preview = service.preview(pattern)
    assert len(preview.positions) == 7
    center = preview.positions[0]
    seat = revision.document.entity('seat-mlp')
    # Anchor point = seat position + acoustic reference offset.
    assert abs(center.x_m - seat.position.x_m) < 1e-9
    assert abs(center.y_m - seat.position.y_m) < 1e-9

    # Preview created no evidence: no measurement_point entities yet.
    assert not any(
        entity.kind == 'measurement_point'
        for entity in scene_repository.current_head(
            DOCUMENT_ID
        ).document.entities
    )

    points = service.materialize(pattern.pattern_id)
    assert len(points) == 7
    assert all(
        point.measurement_point_entity_id.startswith('measurement-point:')
        or point.measurement_point_entity_id
        for point in points
    )

    # Reopen through a fresh service: pattern + lineage replay from the store.
    reopened = MeasurementTargetService(scene_repository, DOCUMENT_ID)
    presentations = reopened.list_presentations()
    assert len(presentations) == 1
    item = presentations[0]
    assert item.point_count == 7
    assert item.materialized_count == 7
    # Materialization itself advanced the document head, so the pinned
    # anchor is no longer head — rebase is required before adding points.
    assert item.stale
    ids = reopened.campaign_target_ids(pattern.pattern_id)
    assert len(ids) == 7
    head = scene_repository.current_head(DOCUMENT_ID)
    for entity_id in ids:
        entity = head.document.entity(entity_id)
        assert entity is not None
        assert entity.kind == 'measurement_point'


def test_anchor_options_and_presets(tmp_path):
    _, _, service = _fixture(tmp_path)
    options = service.anchor_entity_options()
    assert ('seat', 'seat-mlp') in options
    for preset in TARGET_PATTERN_PRESETS:
        pattern = service.create_pattern(
            anchor_kind='seat',
            anchor_entity_id='seat-mlp',
            preset=preset,
        )
        assert len(pattern.offsets) >= 1


def test_rebase_after_head_move_keeps_history(tmp_path):
    scene_repository, revision, service = _fixture(tmp_path)
    pattern = service.create_pattern(
        anchor_kind='seat',
        anchor_entity_id='seat-mlp',
        preset='mlp_lateral',
    )
    points = service.materialize(pattern.pattern_id)
    original_entity_id = points[0].measurement_point_entity_id

    # Move the seat: the historical points must not move.
    head = scene_repository.current_head(DOCUMENT_ID)
    moved = head.document.model_copy(
        update={
            'entities': (
                _seat('seat-mlp', 3.0),
                *(
                    entity
                    for entity in head.document.entities
                    if entity.entity_id != 'seat-mlp'
                ),
            )
        }
    )
    scene_repository.save(moved, parent_revision_id=head.revision_id)

    stale = service.list_presentations()[0]
    assert stale.stale
    new_head = scene_repository.current_head(DOCUMENT_ID)
    historical = new_head.document.entity(original_entity_id)
    old_head = scene_repository.get(points[0].created_in_revision_id)
    original_point = old_head.document.entity(original_entity_id)
    assert (
        historical.position.y_m == original_point.position.y_m
    )

    # Materializing the stale pattern is refused; rebase makes a new
    # version pinned to the moved head.
    with pytest.raises(ValueError):
        service.materialize(pattern.pattern_id)
    rebased = service.rebase(pattern.pattern_id)
    assert rebased.pattern_version == pattern.pattern_version + 1
    assert rebased.anchor_revision_id != pattern.anchor_revision_id
    preview = service.preview(rebased)
    assert abs(preview.positions[0].y_m - 3.0) < 1e-6


def test_explicit_point_anchor(tmp_path):
    _, _, service = _fixture(tmp_path)
    pattern = service.create_pattern(
        anchor_kind='explicit_point',
        explicit_position=Position3(x_m=1.0, y_m=1.0, z_m=1.2),
        preset='mlp_center',
    )
    preview = service.preview(pattern)
    assert len(preview.positions) == 1
    assert preview.positions[0].x_m == 1.0
