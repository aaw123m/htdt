from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_target_pattern_repository import (
    CadTargetPatternRepository,
)
from htdt.cad_measurement_target_pattern import (
    MaterializedPatternPoint,
    MeasurementTargetPattern,
    TargetPatternOffset,
)
from htdt.measurement_target_service import (
    build_target_pattern,
    materialize_target_pattern,
    rebase_target_pattern,
    resolved_pattern_positions,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Offset3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
    make_f1_scene,
)


def _repositories(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(make_f1_scene(), parent_revision_id=None).revision
    return scene_repository, revision, CadTargetPatternRepository(scene_repository)


def _offsets():
    return (
        TargetPatternOffset(
            offset_index=0,
            label='center',
            offset_m=(0.0, 0.0, 0.0),
            placement_tolerance_m=0.05,
        ),
        TargetPatternOffset(
            offset_index=1,
            label='left',
            offset_m=(-0.5, 0.0, 0.0),
            placement_tolerance_m=0.05,
        ),
        TargetPatternOffset(
            offset_index=2,
            label='right',
            offset_m=(0.5, 0.0, 0.0),
            purpose='holdout',
        ),
    )


def test_pattern_resolves_measurement_point_anchor_positions(tmp_path: Path):
    scene_repository, revision, _ = _repositories(tmp_path)
    pattern = build_target_pattern(
        scene_repository,
        document_id=revision.document_id,
        anchor_kind='measurement_point',
        anchor_entity_id='point-mlp',
        offsets=_offsets(),
        created_at='2026-09-23T00:00:00+00:00',
    )
    assert pattern.anchor_revision_id == revision.revision_id
    assert pattern.anchor_position == Position3(x_m=3.0, y_m=3.0, z_m=1.1)
    positions = resolved_pattern_positions(pattern)
    assert positions[0] == Position3(x_m=3.0, y_m=3.0, z_m=1.1)
    assert positions[1] == Position3(x_m=2.5, y_m=3.0, z_m=1.1)
    assert positions[2] == Position3(x_m=3.5, y_m=3.0, z_m=1.1)


def test_explicit_point_anchor_and_frame_rejection(tmp_path: Path):
    scene_repository, revision, _ = _repositories(tmp_path)
    pattern = build_target_pattern(
        scene_repository,
        document_id=revision.document_id,
        anchor_kind='explicit_point',
        explicit_position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
        offsets=_offsets()[:1],
        created_at='2026-09-23T00:00:00+00:00',
    )
    assert pattern.anchor_entity_id is None
    with pytest.raises(ValidationError, match='no local frame'):
        MeasurementTargetPattern(
            **{
                **pattern.model_dump(mode='python'),
                'offset_frame': 'anchor_local',
                'pattern_sha256': pattern.pattern_sha256,
            }
        )


def test_seat_anchor_uses_acoustic_reference_and_local_frame(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    document = SceneDocument(
        document_id='doc-seat',
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='seat-1',
                kind='seat',
                name='Main seat',
                position=Position3(x_m=3.0, y_m=3.0, z_m=0.5),
                size_m=Size3(x_m=0.7, y_m=0.9, z_m=1.0),
                acoustic_reference_offset_m=Offset3(x_m=0.0, y_m=0.0, z_m=0.6),
            ),
        ),
    )
    revision = scene_repository.save(document, parent_revision_id=None).revision
    pattern = build_target_pattern(
        scene_repository,
        document_id=document.document_id,
        anchor_kind='seat',
        anchor_entity_id='seat-1',
        offset_frame='anchor_local',
        offsets=_offsets()[:2],
        created_at='2026-09-23T00:00:00+00:00',
    )
    # Identity orientation → anchor-local axes align with world axes.
    assert pattern.anchor_position == Position3(x_m=3.0, y_m=3.0, z_m=1.1)
    assert pattern.anchor_orientation is not None
    positions = resolved_pattern_positions(pattern)
    assert positions[1] == Position3(x_m=2.5, y_m=3.0, z_m=1.1)


def test_anchor_entity_kind_mismatch_fails(tmp_path: Path):
    scene_repository, revision, _ = _repositories(tmp_path)
    with pytest.raises(ValueError, match='kind mismatch'):
        build_target_pattern(
            scene_repository,
            document_id=revision.document_id,
            anchor_kind='seat',
            anchor_entity_id='point-mlp',
            offsets=_offsets(),
            created_at='2026-09-23T00:00:00+00:00',
        )


def test_materialize_creates_lined_measurement_points(tmp_path: Path):
    scene_repository, revision, pattern_repository = _repositories(tmp_path)
    pattern = build_target_pattern(
        scene_repository,
        document_id=revision.document_id,
        anchor_kind='measurement_point',
        anchor_entity_id='point-mlp',
        offsets=_offsets(),
        created_at='2026-09-23T00:00:00+00:00',
    )
    pattern_repository.save_pattern(pattern)
    result, points = materialize_target_pattern(
        scene_repository,
        pattern_repository,
        pattern,
        created_at='2026-09-23T01:00:00+00:00',
    )
    assert len(points) == 3
    head = scene_repository.latest(revision.document_id)
    assert head is not None and head.revision_id == result.revision.revision_id
    for point in points:
        entity = head.document.entity(point.measurement_point_entity_id)
        assert entity.kind == 'measurement_point'
        assert entity.position == point.position
        assert point.anchor_revision_id == revision.revision_id
        assert point.created_in_revision_id == head.revision_id
        assert point.placement_tolerance_m == (
            0.05 if point.offset_index in (0, 1) else None
        )
    assert (
        pattern_repository.list_pattern_points(pattern.pattern_id) == points
    )


def test_moving_anchor_does_not_move_historical_points(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    document = SceneDocument(
        document_id='doc-move',
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='seat-1',
                kind='seat',
                name='Main seat',
                position=Position3(x_m=3.0, y_m=3.0, z_m=0.5),
                size_m=Size3(x_m=0.7, y_m=0.9, z_m=1.0),
                acoustic_reference_offset_m=Offset3(x_m=0.0, y_m=0.0, z_m=0.6),
            ),
        ),
    )
    revision = scene_repository.save(document, parent_revision_id=None).revision
    pattern_repository = CadTargetPatternRepository(scene_repository)
    pattern = build_target_pattern(
        scene_repository,
        document_id=document.document_id,
        anchor_kind='seat',
        anchor_entity_id='seat-1',
        offsets=_offsets()[:1],
        created_at='2026-09-23T00:00:00+00:00',
    )
    pattern_repository.save_pattern(pattern)
    _, points = materialize_target_pattern(
        scene_repository,
        pattern_repository,
        pattern,
        created_at='2026-09-23T01:00:00+00:00',
    )
    historical_position = points[0].position
    # Move the seat: historical point keeps its frozen position/revision.
    moved = document.model_copy(
        update={
            'entities': (
                document.entities[0].model_copy(
                    update={'position': Position3(x_m=4.0, y_m=3.0, z_m=0.5)}
                ),
                *scene_repository.latest(document.document_id).document.entities[1:],
            )
        }
    )
    scene_repository.save(moved, parent_revision_id=scene_repository.latest(document.document_id).revision_id)
    assert points[0].position == historical_position
    assert points[0].anchor_revision_id == revision.revision_id
    # Rebase produces a new pattern version pinned to the moved anchor.
    rebased = rebase_target_pattern(
        scene_repository,
        pattern,
        created_at='2026-09-23T02:00:00+00:00',
    )
    assert rebased.pattern_version == pattern.pattern_version + 1
    assert rebased.supersedes_pattern_sha256 == pattern.pattern_sha256
    assert rebased.anchor_position == Position3(x_m=4.0, y_m=3.0, z_m=1.1)
    assert rebased.pattern_id != pattern.pattern_id


def test_materialize_on_stale_anchor_fails_closed(tmp_path: Path):
    scene_repository, revision, pattern_repository = _repositories(tmp_path)
    pattern = build_target_pattern(
        scene_repository,
        document_id=revision.document_id,
        anchor_kind='measurement_point',
        anchor_entity_id='point-mlp',
        offsets=_offsets()[:1],
        created_at='2026-09-23T00:00:00+00:00',
    )
    pattern_repository.save_pattern(pattern)
    # Advance head without materializing — the pinned anchor is now stale.
    head = scene_repository.latest(revision.document_id)
    moved = head.document.model_copy(
        update={
            'entities': (
                *head.document.entities,
                SceneEntity(
                    entity_id='furniture-new',
                    kind='furniture',
                    name='Shelf',
                    position=Position3(x_m=1.0, y_m=1.0, z_m=0.5),
                    size_m=Size3(x_m=0.5, y_m=0.5, z_m=1.0),
                ),
            )
        }
    )
    scene_repository.save(moved, parent_revision_id=head.revision_id)
    with pytest.raises(ValueError, match='current head'):
        materialize_target_pattern(
            scene_repository,
            pattern_repository,
            pattern,
            created_at='2026-09-23T01:00:00+00:00',
        )
