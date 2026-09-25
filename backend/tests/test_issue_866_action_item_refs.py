"""#866: ProjectActionItem subject refs, spatial anchors and resolution
refs are proven against canonical authority at save, and stored rows fail
closed when indexed columns drift from the self-hashed payload.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from htdt.cad_action_item import (
    ActionSpatialAnchor,
    ActionSubjectRef,
    build_action_item,
    update_action_item,
)
from htdt.cad_action_item_repository import (
    ActionItemIntegrityError,
    ActionItemRefError,
    CadActionItemRepository,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Position3,
    SceneDocument,
    SceneEntity,
)

NOW = '2026-09-24T00:00:00+00:00'


def _scene(document_id: str, entity_ids=('sub-1',)) -> SceneDocument:
    return SceneDocument(
        document_id=document_id,
        schema_version=2,
        room=None,
        entities=tuple(
            SceneEntity(
                entity_id=entity_id,
                kind='measurement_point',
                name=entity_id,
                position=Position3(x_m=1.0, y_m=1.0, z_m=0.0),
            )
            for entity_id in entity_ids
        ),
    )


def _stores(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        _scene('doc-a'), parent_revision_id=None
    ).revision
    repository = CadActionItemRepository(scene_repository)
    return scene_repository, repository, revision


def _item(document_id='doc-a', **overrides):
    options = dict(
        document_id=document_id,
        title='follow-up',
        created_at_utc=NOW,
    )
    options.update(overrides)
    return build_action_item(**options)


# -- subject refs ----------------------------------------------------------------


def test_missing_scene_entity_rejected(tmp_path: Path) -> None:
    _, repository, _ = _stores(tmp_path)
    item = _item(
        subject_refs=(
            ActionSubjectRef(kind='scene_entity', ref_id='sub-2'),
        )
    )
    with pytest.raises(ActionItemRefError, match='unknown'):
        repository.save(item)


def test_existing_scene_entity_accepted(tmp_path: Path) -> None:
    _, repository, _ = _stores(tmp_path)
    item = _item(
        subject_refs=(
            ActionSubjectRef(kind='scene_entity', ref_id='sub-1'),
        )
    )
    repository.save(item)
    assert repository.get(item.action_id) == item


def test_foreign_scene_entity_rejected(tmp_path: Path) -> None:
    scene_repository, repository, _ = _stores(tmp_path)
    scene_repository.save(
        _scene('doc-b', entity_ids=('spk-b',)), parent_revision_id=None
    )
    # 'spk-b' exists only in doc-b — a doc-a item may not claim it.
    item = _item(
        subject_refs=(
            ActionSubjectRef(kind='scene_entity', ref_id='spk-b'),
        )
    )
    with pytest.raises(ActionItemRefError, match='unknown'):
        repository.save(item)


def test_foreign_scene_revision_subject_rejected(tmp_path: Path) -> None:
    scene_repository, repository, _ = _stores(tmp_path)
    foreign = scene_repository.save(
        _scene('doc-b'), parent_revision_id=None
    ).revision
    item = _item(
        subject_refs=(
            ActionSubjectRef(
                kind='scene_revision',
                ref_id=foreign.revision_id,
                ref_sha256=foreign.content_hash,
            ),
        )
    )
    with pytest.raises(ActionItemRefError, match='another project'):
        repository.save(item)


def test_missing_measurement_and_variant_rejected(tmp_path: Path) -> None:
    _, repository, _ = _stores(tmp_path)
    for kind in ('measurement', 'system_variant'):
        item = _item(
            subject_refs=(
                ActionSubjectRef(kind=kind, ref_id='not-there'),
            )
        )
        with pytest.raises(ActionItemRefError, match='unknown'):
            repository.save(item)


def test_hash_bearing_ref_requires_exact_hash(tmp_path: Path) -> None:
    _, repository, revision = _stores(tmp_path)
    # scene_revision is hash-bearing (semantic_sha256 = content_hash).
    missing_hash = _item(
        subject_refs=(
            ActionSubjectRef(
                kind='scene_revision', ref_id=revision.revision_id
            ),
        )
    )
    with pytest.raises(ActionItemRefError, match='hash-bearing'):
        repository.save(missing_hash)
    wrong_hash = _item(
        subject_refs=(
            ActionSubjectRef(
                kind='scene_revision',
                ref_id=revision.revision_id,
                ref_sha256='f' * 64,
            ),
        )
    )
    with pytest.raises(ActionItemRefError, match='hash mismatch'):
        repository.save(wrong_hash)
    exact = _item(
        subject_refs=(
            ActionSubjectRef(
                kind='scene_revision',
                ref_id=revision.revision_id,
                ref_sha256=revision.content_hash,
            ),
        )
    )
    repository.save(exact)


def test_other_kind_is_the_escape_hatch(tmp_path: Path) -> None:
    _, repository, _ = _stores(tmp_path)
    item = _item(
        subject_refs=(
            ActionSubjectRef(
                kind='other', ref_id='vendor-ticket-42', label='external'
            ),
        )
    )
    repository.save(item)
    assert repository.get(item.action_id) == item


# -- spatial anchors ---------------------------------------------------------------


def test_position_anchor_requires_scene_revision(tmp_path: Path) -> None:
    _, repository, _ = _stores(tmp_path)
    item = _item(
        spatial_anchor=ActionSpatialAnchor(
            kind='position', position_m=(1.0, 2.0, 0.5)
        )
    )
    with pytest.raises(ActionItemRefError, match='scene_revision_id'):
        repository.save(item)


def test_position_anchor_with_revision_persists(tmp_path: Path) -> None:
    _, repository, revision = _stores(tmp_path)
    item = _item(
        spatial_anchor=ActionSpatialAnchor(
            kind='position',
            position_m=(1.0, 2.0, 0.5),
            scene_revision_id=revision.revision_id,
        )
    )
    repository.save(item)
    assert repository.get(item.action_id) == item


def test_entity_anchor_must_exist_in_pinned_revision(tmp_path: Path) -> None:
    scene_repository, repository, revision = _stores(tmp_path)
    anchor = ActionSpatialAnchor(
        kind='entity_ref',
        ref_id='sub-1',
        scene_revision_id=revision.revision_id,
    )
    repository.save(_item(spatial_anchor=anchor))
    # A later revision that removed the entity must not anchor it.
    revision2 = scene_repository.save(
        _scene('doc-a', entity_ids=('other-thing',)),
        parent_revision_id=revision.revision_id,
    ).revision
    removed = _item(
        spatial_anchor=ActionSpatialAnchor(
            kind='entity_ref',
            ref_id='sub-1',
            scene_revision_id=revision2.revision_id,
        )
    )
    with pytest.raises(ActionItemRefError, match='absent'):
        repository.save(removed)


def test_foreign_revision_anchor_rejected(tmp_path: Path) -> None:
    scene_repository, repository, _ = _stores(tmp_path)
    foreign = scene_repository.save(
        _scene('doc-b'), parent_revision_id=None
    ).revision
    item = _item(
        spatial_anchor=ActionSpatialAnchor(
            kind='position',
            position_m=(0.0, 0.0, 0.0),
            scene_revision_id=foreign.revision_id,
        )
    )
    with pytest.raises(ActionItemRefError, match='another project'):
        repository.save(item)


def test_unknown_revision_anchor_rejected(tmp_path: Path) -> None:
    _, repository, _ = _stores(tmp_path)
    item = _item(
        spatial_anchor=ActionSpatialAnchor(
            kind='position',
            position_m=(0.0, 0.0, 0.0),
            scene_revision_id='rev-not-real',
        )
    )
    with pytest.raises(ActionItemRefError, match='unknown'):
        repository.save(item)


def test_surface_anchor_requires_known_surface(tmp_path: Path) -> None:
    _, repository, revision = _stores(tmp_path)
    item = _item(
        spatial_anchor=ActionSpatialAnchor(
            kind='surface_ref',
            ref_id='wall-north',
            scene_revision_id=revision.revision_id,
        )
    )
    with pytest.raises(ActionItemRefError, match='surface'):
        repository.save(item)


# -- resolution refs ---------------------------------------------------------------


def test_foreign_or_missing_resolution_ref_rejected(tmp_path: Path) -> None:
    scene_repository, repository, revision = _stores(tmp_path)
    foreign = scene_repository.save(
        _scene('doc-b'), parent_revision_id=None
    ).revision
    item = _item()
    foreign_ref = update_action_item(
        item,
        status='done',
        resolution_ref=ActionSubjectRef(
            kind='scene_revision',
            ref_id=foreign.revision_id,
            ref_sha256=foreign.content_hash,
        ),
        updated_at_utc='2026-09-24T01:00:00+00:00',
    )
    with pytest.raises(ActionItemRefError, match='another project'):
        repository.save(foreign_ref)
    missing_ref = update_action_item(
        item,
        status='done',
        resolution_ref=ActionSubjectRef(
            kind='measurement', ref_id='m-999'
        ),
        updated_at_utc='2026-09-24T01:00:00+00:00',
    )
    with pytest.raises(ActionItemRefError, match='unknown'):
        repository.save(missing_ref)
    exact_ref = update_action_item(
        item,
        status='done',
        resolution_ref=ActionSubjectRef(
            kind='scene_revision',
            ref_id=revision.revision_id,
            ref_sha256=revision.content_hash,
        ),
        updated_at_utc='2026-09-24T01:00:00+00:00',
    )
    repository.save(exact_ref)
    assert repository.get(item.action_id).status == 'done'


# -- historical classification -----------------------------------------------------


def test_historical_entity_subject_remains_resolvable(tmp_path: Path) -> None:
    scene_repository, repository, revision = _stores(tmp_path)
    ref = ActionSubjectRef(kind='scene_entity', ref_id='sub-1')
    item = _item(subject_refs=(ref,))
    repository.save(item)
    assert repository.subject_ref_state(ref, 'doc-a') == 'current'

    # The entity disappears from the new head — the stored item stays
    # readable and classifies historical, never rebound.
    scene_repository.save(
        _scene('doc-a', entity_ids=('other-thing',)),
        parent_revision_id=revision.revision_id,
    )
    assert repository.subject_ref_state(ref, 'doc-a') == 'historical'
    assert repository.get(item.action_id) == item

    unknown = ActionSubjectRef(kind='scene_entity', ref_id='never-existed')
    assert repository.subject_ref_state(unknown, 'doc-a') == 'unresolved'
    unowned = ActionSubjectRef(kind='other', ref_id='x')
    assert repository.subject_ref_state(unowned, 'doc-a') == 'unresolved'


def test_read_never_revalidates_stored_refs(tmp_path: Path) -> None:
    scene_repository, repository, revision = _stores(tmp_path)
    item = _item(subject_refs=(ActionSubjectRef(
        kind='scene_entity', ref_id='sub-1'
    ),))
    repository.save(item)
    # Head moves on — the stored item remains retrievable unchanged.
    scene_repository.save(
        _scene('doc-a', entity_ids=()),
        parent_revision_id=revision.revision_id,
    )
    assert repository.get(item.action_id) == item
    assert repository.list_actions('doc-a') == (item,)


# -- row/payload drift ---------------------------------------------------------------


def _corrupt_row(path: Path, action_id: str, **column_updates) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            'UPDATE project_action_items SET '
            + ', '.join(f'{column} = ?' for column in column_updates)
            + ' WHERE action_id = ?',
            (*column_updates.values(), action_id),
        )


def test_row_column_drift_fails_closed(tmp_path: Path) -> None:
    scene_repository, repository, _ = _stores(tmp_path)
    item = _item()
    repository.save(item)
    _corrupt_row(
        repository.path, item.action_id, status='done'
    )
    with pytest.raises(ActionItemIntegrityError):
        repository.get(item.action_id)
    with pytest.raises(ActionItemIntegrityError):
        repository.list_actions('doc-a')


def test_row_document_drift_fails_closed(tmp_path: Path) -> None:
    _, repository, _ = _stores(tmp_path)
    item = _item()
    repository.save(item)
    _corrupt_row(
        repository.path, item.action_id, document_id='doc-b'
    )
    with pytest.raises(ActionItemIntegrityError):
        repository.get(item.action_id)


def test_forged_payload_hash_fails_closed(tmp_path: Path) -> None:
    _, repository, _ = _stores(tmp_path)
    item = _item()
    repository.save(item)
    forged = item.model_copy(
        update={'title': 'forged title', 'action_sha256': 'f' * 64}
    )
    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            'UPDATE project_action_items SET payload_json = ?, '
            'action_sha256 = ? WHERE action_id = ?',
            (
                json.dumps(forged.model_dump(mode='json')),
                forged.action_sha256,
                item.action_id,
            ),
        )
    with pytest.raises(Exception, match='[Hh]ash|drift|mismatch'):
        repository.get(item.action_id)
