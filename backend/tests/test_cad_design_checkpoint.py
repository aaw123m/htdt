from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_constraint_models import CadConstraintSet
from htdt.cad_constraint_repository import CadConstraintRepository
from htdt.cad_design_checkpoint import (
    CheckpointAuthorityRef,
    build_design_checkpoint,
    diff_checkpoints,
    restore_design_checkpoint,
    snapshot_constraint_workspace,
)
from htdt.cad_design_checkpoint_repository import CadDesignCheckpointRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Direction3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)

NOW = '2026-09-23T00:00:00+00:00'


def _scene(document_id: str = 'doc-1', fl_x: float = 1.2) -> SceneDocument:
    return SceneDocument(
        document_id=document_id,
        room=RoomPrism(width_m=6.0, depth_m=4.5, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='fl',
                kind='speaker',
                name='FL',
                speaker_role='FL',
                position=Position3(x_m=fl_x, y_m=0.8, z_m=1.0),
                size_m=Size3(x_m=0.24, y_m=0.28, z_m=0.42),
                aim_xyz=Direction3(x=0.0, y=1.0, z=0.0),
            ),
        ),
    )


def _repositories(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    constraint_repository = CadConstraintRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(_scene(), parent_revision_id=None).revision
    repository = CadDesignCheckpointRepository(scene_repository)
    return scene_repository, constraint_repository, revision, repository


def _constraint_set(document_id: str) -> CadConstraintSet:
    return CadConstraintSet(document_id=document_id)


def _checkpoint(revision, snapshot, **overrides):
    payload = {
        'document_id': revision.document_id,
        'title': '設計案確定',
        'scene_revision': revision,
        'constraint_snapshot': snapshot,
        'created_at_utc': NOW,
    }
    payload.update(overrides)
    return build_design_checkpoint(**payload)


def test_checkpoint_pins_exact_scene_revision_and_constraint_snapshot(
    tmp_path: Path,
) -> None:
    _scenes, constraints, revision, repository = _repositories(tmp_path)
    constraint_set = _constraint_set(revision.document_id)
    constraints.save(constraint_set)
    snapshot = snapshot_constraint_workspace(
        constraint_set, created_at_utc=NOW, source_updated_at_utc=NOW
    )
    repository.save_snapshot(snapshot)
    checkpoint = _checkpoint(
        revision,
        snapshot,
        component_refs=(
            CheckpointAuthorityRef(
                kind='system_variant', ref_id='var-1', ref_sha256='a' * 64
            ),
        ),
    )
    repository.save_checkpoint(checkpoint)
    stored = repository.get_checkpoint(checkpoint.checkpoint_id)
    assert stored == checkpoint
    assert stored.scene_revision_id == revision.revision_id
    assert stored.scene_content_hash == revision.content_hash
    assert stored.constraint_snapshot_sha256 == snapshot.snapshot_sha256


def test_checkpoint_rejects_unpersisted_scene_revision(tmp_path: Path) -> None:
    _scenes, _constraints, revision, repository = _repositories(tmp_path)
    ghost = replace(revision, revision_id='ghost')
    checkpoint = _checkpoint(ghost, None)
    with pytest.raises(ValueError, match='not persisted'):
        repository.save_checkpoint(checkpoint)


def test_diff_reports_per_component_state(tmp_path: Path) -> None:
    scenes, constraints, revision, repository = _repositories(tmp_path)
    snapshot_a = snapshot_constraint_workspace(
        _constraint_set(revision.document_id), created_at_utc=NOW
    )
    repository.save_snapshot(snapshot_a)
    checkpoint_a = _checkpoint(revision, snapshot_a, checkpoint_id='cp-a')
    revision_b = scenes.save(
        _scene(fl_x=1.6), parent_revision_id=revision.revision_id
    ).revision
    snapshot_b = snapshot_constraint_workspace(
        _constraint_set(revision.document_id),
        created_at_utc='2026-09-23T01:00:00+00:00',
    )
    repository.save_snapshot(snapshot_b)
    checkpoint_b = _checkpoint(
        revision_b,
        snapshot_b,
        checkpoint_id='cp-b',
        created_at_utc='2026-09-23T01:00:00+00:00',
    )
    diff = diff_checkpoints(checkpoint_a, checkpoint_b)
    states = {item.kind: item.state for item in diff.components}
    assert states['scene_revision'] == 'changed'
    assert states['constraint_workspace'] == 'changed'


def test_restore_creates_new_head_never_rewrites(tmp_path: Path) -> None:
    scenes, constraints, revision, repository = _repositories(tmp_path)
    constraint_set = _constraint_set(revision.document_id)
    constraints.save(constraint_set)
    snapshot = snapshot_constraint_workspace(
        constraint_set, created_at_utc=NOW, source_updated_at_utc=NOW
    )
    repository.save_snapshot(snapshot)
    checkpoint = _checkpoint(revision, snapshot)
    repository.save_checkpoint(checkpoint)

    # evolve the world: new head revision + mutated constraint workspace
    evolved = scenes.save(
        _scene(fl_x=9.9), parent_revision_id=revision.revision_id
    ).revision
    constraints.save(
        CadConstraintSet(document_id=revision.document_id, constraints=())
    )

    record = restore_design_checkpoint(
        checkpoint,
        scene_repository=scenes,
        constraint_repository=constraints,
        snapshot=snapshot,
        created_at_utc='2026-09-23T02:00:00+00:00',
    )
    repository.save_restore(record)

    # the pinned scene state became the new head — nothing was rewritten
    head = scenes.current_head(revision.document_id)
    assert head.revision_id == record.new_scene_revision_id
    assert head.revision_id != revision.revision_id
    assert head.revision_id != evolved.revision_id
    assert head.document.entities[0].position.x_m == pytest.approx(1.2)
    assert head.parent_revision_id == evolved.revision_id
    # historical revisions survive untouched
    assert scenes.get(evolved.revision_id) is not None
    assert scenes.get(revision.revision_id) is not None
    assert set(record.applied_components) == {'scene_revision', 'constraint_workspace'}


def test_partial_restore_lists_applied_components(tmp_path: Path) -> None:
    scenes, constraints, revision, repository = _repositories(tmp_path)
    snapshot = snapshot_constraint_workspace(
        _constraint_set(revision.document_id), created_at_utc=NOW
    )
    repository.save_snapshot(snapshot)
    checkpoint = _checkpoint(revision, snapshot)
    repository.save_checkpoint(checkpoint)
    record = restore_design_checkpoint(
        checkpoint,
        scene_repository=scenes,
        constraint_repository=constraints,
        snapshot=snapshot,
        components=('scene_revision',),
        created_at_utc=NOW,
    )
    assert record.applied_components == ('scene_revision',)
    assert record.new_scene_revision_id is not None
    assert record.new_constraint_sha256 is None


def test_checkpoint_never_resolves_latest_at_read(tmp_path: Path) -> None:
    scenes, constraints, revision, repository = _repositories(tmp_path)
    snapshot = snapshot_constraint_workspace(
        _constraint_set(revision.document_id), created_at_utc=NOW
    )
    repository.save_snapshot(snapshot)
    checkpoint = _checkpoint(revision, snapshot)
    repository.save_checkpoint(checkpoint)
    # mutate the world after pinning — the checkpoint still reads the pins
    scenes.save(_scene(fl_x=9.9), parent_revision_id=revision.revision_id)
    stored = repository.get_checkpoint(checkpoint.checkpoint_id)
    assert stored.scene_revision_id == revision.revision_id
    assert stored.scene_content_hash == revision.content_hash


def test_tampered_checkpoint_hash_is_rejected(tmp_path: Path) -> None:
    _scenes, _constraints, revision, _repo = _repositories(tmp_path)
    checkpoint = _checkpoint(revision, None)
    payload = checkpoint.model_dump(mode='python')
    payload['title'] = 'tampered'
    with pytest.raises(ValidationError, match='hash mismatch'):
        type(checkpoint)(**payload)
