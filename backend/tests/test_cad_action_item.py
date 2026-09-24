from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_action_item import (
    ActionSpatialAnchor,
    ActionSubjectRef,
    build_action_item,
    update_action_item,
)
from htdt.cad_action_item_repository import (
    ActionItemConflictError,
    CadActionItemRepository,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene

NOW = '2026-09-24T00:00:00+00:00'


def _repository(tmp_path: Path) -> CadActionItemRepository:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    scene_repository.save(make_empty_scene('doc-1'), parent_revision_id=None)
    return CadActionItemRepository(scene_repository)


def _item(**overrides):
    options = dict(
        document_id='doc-1',
        title='サブウーファーの位置を確認',
        created_at_utc=NOW,
        subject_refs=(ActionSubjectRef(kind='scene_entity', ref_id='sub-1'),),
    )
    options.update(overrides)
    return build_action_item(**options)


def test_action_item_is_deterministic_and_hashed() -> None:
    item = _item()
    assert len(item.action_sha256) == 64
    assert item.status == 'open'
    assert item.priority == 'normal'
    assert not item.archived


def test_duplicate_subject_refs_rejected() -> None:
    with pytest.raises(ValidationError, match='unique'):
        _item(
            subject_refs=(
                ActionSubjectRef(kind='scene_entity', ref_id='sub-1'),
                ActionSubjectRef(kind='scene_entity', ref_id='sub-1'),
            )
        )


def test_position_anchor_shape() -> None:
    anchor = ActionSpatialAnchor(kind='position', position_m=(1.0, 2.0, 3.0))
    assert anchor.position_m == (1.0, 2.0, 3.0)
    with pytest.raises(ValidationError, match='position_m'):
        ActionSpatialAnchor(kind='position')
    with pytest.raises(ValidationError, match='ref_id'):
        ActionSpatialAnchor(kind='position', position_m=(0, 0, 0), ref_id='w1')
    with pytest.raises(ValidationError, match='ref_id'):
        ActionSpatialAnchor(kind='surface_ref')
    with pytest.raises(ValidationError, match='position_m'):
        ActionSpatialAnchor(
            kind='entity_ref', ref_id='e1', position_m=(0.0, 0.0, 0.0)
        )
    with pytest.raises(ValidationError, match='finite'):
        ActionSpatialAnchor(
            kind='position', position_m=(float('nan'), 0.0, 0.0)
        )


def test_update_status_stamps_and_reopen_clears() -> None:
    item = _item()
    done = update_action_item(
        item,
        status='done',
        resolution_ref=ActionSubjectRef(kind='scene_revision', ref_id='rev-9'),
        updated_at_utc='2026-09-24T01:00:00+00:00',
    )
    assert done.status == 'done'
    assert done.completed_at_utc == '2026-09-24T01:00:00+00:00'
    assert done.resolution_ref.ref_id == 'rev-9'
    reopened = update_action_item(
        done, status='open', updated_at_utc='2026-09-24T02:00:00+00:00'
    )
    assert reopened.completed_at_utc is None
    assert reopened.resolution_ref is None


def test_unfinished_statuses_reject_completed_fields() -> None:
    with pytest.raises(ValidationError, match='completed_at_utc'):
        _item(status='done')
    with pytest.raises(ValueError, match='resolution_ref'):
        update_action_item(
            _item(),
            status='waiting',
            resolution_ref=ActionSubjectRef(kind='project', ref_id='p'),
            updated_at_utc='2026-09-24T01:00:00+00:00',
        )


def test_repository_upsert_and_filters(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    item = _item()
    repository.save(item)
    assert repository.get(item.action_id) == item
    done = update_action_item(
        item, status='done', updated_at_utc='2026-09-24T01:00:00+00:00'
    )
    repository.save(done)
    assert repository.get(item.action_id).status == 'done'
    open_items = repository.list_actions('doc-1', status='open')
    assert open_items == ()
    done_items = repository.list_actions('doc-1', status='done')
    assert len(done_items) == 1


def test_repository_archive_hides_but_keeps(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    item = _item()
    repository.save(item)
    archived = update_action_item(
        item, archived=True, updated_at_utc='2026-09-24T01:00:00+00:00'
    )
    repository.save(archived)
    assert repository.list_actions('doc-1') == ()
    assert len(repository.list_actions('doc-1', include_archived=True)) == 1


def test_repository_rejects_foreign_document_rebind(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    item = _item()
    repository.save(item)
    foreign = build_action_item(
        document_id='doc-2',
        title='別プロジェクト',
        created_at_utc=NOW,
        action_id=item.action_id,
    )
    with pytest.raises(ActionItemConflictError):
        repository.save(foreign)
