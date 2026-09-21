from contextlib import closing
import logging
from pathlib import Path
import sqlite3

import pytest

from htdt.cad_document import WorkingDocument
from htdt.cad_repository import SceneRepository, SceneRevisionConflictError
from htdt.cad_scene import Position3, make_empty_scene, make_f1_scene


def test_scene_revision_save_reopen_noop_and_parent_history(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    first = repository.save(make_f1_scene(), parent_revision_id=None)
    assert first.created

    working = WorkingDocument(
        first.revision.document,
        source_revision_id=first.revision.revision_id,
        saved_content_hash=first.revision.content_hash,
    )
    assert not working.is_dirty

    working.move_entity('speaker-fl', Position3(x_m=1.5, y_m=0.75, z_m=1.05))
    assert working.is_dirty
    second = repository.save(working.committed_document, parent_revision_id=working.source_revision_id)
    assert second.created
    working.mark_saved(second.revision.revision_id, second.revision.content_hash)
    assert not working.is_dirty

    reopened = repository.latest(make_f1_scene().document_id)
    assert reopened is not None
    assert reopened.revision_id == second.revision.revision_id
    assert reopened.document.entity('speaker-fl').position.x_m == 1.5
    first_reopened = repository.get(first.revision.revision_id)
    assert first_reopened is not None
    assert first_reopened.document.entity('speaker-fl').position.x_m == 1.35

    noop = repository.save(reopened.document, parent_revision_id=reopened.revision_id)
    assert not noop.created
    assert noop.revision.revision_id == reopened.revision_id

    restored = WorkingDocument(
        reopened.document,
        source_revision_id=reopened.revision_id,
        saved_content_hash=reopened.content_hash,
    )
    restored.move_entity('speaker-fl', Position3(x_m=1.35, y_m=0.75, z_m=1.05))
    third = repository.save(restored.committed_document, parent_revision_id=reopened.revision_id)
    assert third.created
    assert third.revision.parent_revision_id == reopened.revision_id
    assert third.revision.content_hash == first.revision.content_hash


def test_recovery_is_separate_from_formal_revision_and_formal_save_clears_it(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    first = repository.save(make_f1_scene(), parent_revision_id=None).revision
    working = WorkingDocument(
        first.document,
        source_revision_id=first.revision_id,
        saved_content_hash=first.content_hash,
    )
    working.move_entity('speaker-fl', Position3(x_m=1.6, y_m=0.75, z_m=1.05))

    recovery = repository.save_recovery(
        working.committed_document,
        source_revision_id=working.source_revision_id,
    )
    assert recovery is not None
    assert recovery.document.entity('speaker-fl').position.x_m == 1.6
    assert repository.latest(first.document_id).revision_id == first.revision_id

    reopened_recovery = repository.recovery(first.document_id)
    assert reopened_recovery is not None
    recovered_working = WorkingDocument(
        reopened_recovery.document,
        source_revision_id=first.revision_id,
        saved_content_hash=first.content_hash,
    )
    assert recovered_working.is_dirty

    saved = repository.save(
        recovered_working.committed_document,
        parent_revision_id=recovered_working.source_revision_id,
    )
    assert saved.created
    assert repository.recovery(first.document_id) is None
    first_after = repository.get(first.revision_id)
    assert first_after is not None
    assert first_after.document.entity('speaker-fl').position.x_m == 1.35


def test_failed_save_does_not_change_working_document_or_formal_revision(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    first = repository.save(make_f1_scene(), parent_revision_id=None).revision
    working = WorkingDocument(
        first.document,
        source_revision_id=first.revision_id,
        saved_content_hash=first.content_hash,
    )
    working.move_entity('speaker-fl', Position3(x_m=1.7, y_m=0.75, z_m=1.05))
    before = working.committed_document
    history_length = working.history_length

    with pytest.raises(ValueError, match='unknown parent revision'):
        repository.save(before, parent_revision_id='missing-revision')

    assert working.committed_document == before
    assert working.history_length == history_length
    assert working.is_dirty
    assert repository.latest(first.document_id).revision_id == first.revision_id


def test_second_root_save_for_existing_document_is_rejected(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    first = repository.save(make_f1_scene(), parent_revision_id=None)
    assert first.created

    changed = make_empty_scene(first.revision.document_id)
    with pytest.raises(SceneRevisionConflictError, match='duplicate root'):
        repository.save(changed, parent_revision_id=None)

    latest = repository.latest(first.revision.document_id)
    assert latest is not None
    assert latest.revision_id == first.revision.revision_id
    with closing(sqlite3.connect(repository.path)) as connection:
        count = connection.execute(
            'SELECT COUNT(*) FROM scene_revisions WHERE document_id=?',
            (first.revision.document_id,),
        ).fetchone()[0]
    assert count == 1

    # The contract is per-document: a root for another document still saves.
    other = repository.save(make_empty_scene('other-document'), parent_revision_id=None)
    assert other.created
    assert (
        repository.latest('other-document').revision_id
        == other.revision.revision_id
    )


def test_stale_parent_save_is_rejected_and_latest_is_unchanged(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    first = repository.save(make_f1_scene(), parent_revision_id=None).revision

    working = WorkingDocument(
        first.document,
        source_revision_id=first.revision_id,
        saved_content_hash=first.content_hash,
    )
    working.move_entity('speaker-fl', Position3(x_m=1.5, y_m=0.75, z_m=1.05))
    second = repository.save(
        working.committed_document,
        parent_revision_id=working.source_revision_id,
    ).revision
    assert repository.latest(first.document_id).revision_id == second.revision_id

    stale_working = WorkingDocument(
        first.document,
        source_revision_id=first.revision_id,
        saved_content_hash=first.content_hash,
    )
    stale_working.move_entity('speaker-fl', Position3(x_m=1.6, y_m=0.75, z_m=1.05))
    with pytest.raises(SceneRevisionConflictError, match='stale parent'):
        repository.save(
            stale_working.committed_document,
            parent_revision_id=first.revision_id,
        )

    # A stale expected head is a conflict even when the payload equals the
    # stale parent's content (no silent no-op against a moved head).
    with pytest.raises(SceneRevisionConflictError, match='stale parent'):
        repository.save(first.document, parent_revision_id=first.revision_id)

    assert repository.latest(first.document_id).revision_id == second.revision_id
    assert repository.get(first.revision_id) is not None


def test_second_writer_from_same_head_cannot_advance(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    first = repository.save(make_f1_scene(), parent_revision_id=None).revision

    def stale_edit(x_m: float) -> WorkingDocument:
        working = WorkingDocument(
            first.document,
            source_revision_id=first.revision_id,
            saved_content_hash=first.content_hash,
        )
        working.move_entity('speaker-fl', Position3(x_m=x_m, y_m=0.75, z_m=1.05))
        return working

    # Two writers load the same head; only the first compare-and-swap commits.
    writer_a = stale_edit(1.5)
    writer_b = stale_edit(1.6)
    saved = repository.save(
        writer_a.committed_document,
        parent_revision_id=writer_a.source_revision_id,
    )
    assert saved.created
    with pytest.raises(SceneRevisionConflictError, match='stale parent'):
        repository.save(
            writer_b.committed_document,
            parent_revision_id=writer_b.source_revision_id,
        )
    assert (
        repository.latest(first.document_id).revision_id
        == saved.revision.revision_id
    )


def test_stale_parent_inside_caller_transaction_rejects_and_rolls_back(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    first = repository.save(make_f1_scene(), parent_revision_id=None).revision
    changed = first.document.model_copy(
        update={
            'entities': tuple(
                entity.model_copy(
                    update={'position': Position3(x_m=1.7, y_m=0.75, z_m=1.05)}
                )
                if entity.entity_id == 'speaker-fl'
                else entity
                for entity in first.document.entities
            )
        }
    )
    repository.save(changed, parent_revision_id=first.revision_id)

    stale_edit = first.document.model_copy(
        update={
            'entities': tuple(
                entity.model_copy(
                    update={'position': Position3(x_m=1.8, y_m=0.75, z_m=1.05)}
                )
                if entity.entity_id == 'speaker-fl'
                else entity
                for entity in first.document.entities
            )
        }
    )
    with closing(repository._connect()) as connection:
        connection.execute('BEGIN IMMEDIATE')
        with pytest.raises(SceneRevisionConflictError, match='stale parent'):
            repository._save_in_transaction(
                connection,
                stale_edit,
                parent_revision_id=first.revision_id,
            )
        connection.rollback()

    latest = repository.latest(first.document_id)
    assert latest is not None
    assert latest.revision_id != first.revision_id


def test_allow_branch_writes_deliberate_non_head_revision(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    first = repository.save(make_f1_scene(), parent_revision_id=None).revision

    working = WorkingDocument(
        first.document,
        source_revision_id=first.revision_id,
        saved_content_hash=first.content_hash,
    )
    working.move_entity('speaker-fl', Position3(x_m=1.5, y_m=0.75, z_m=1.05))
    second = repository.save(
        working.committed_document,
        parent_revision_id=working.source_revision_id,
    ).revision

    branch_document = first.document.model_copy(
        update={
            'entities': tuple(
                entity.model_copy(
                    update={'position': Position3(x_m=1.6, y_m=0.75, z_m=1.05)}
                )
                if entity.entity_id == 'speaker-fl'
                else entity
                for entity in first.document.entities
            )
        }
    )
    branch = repository.save(
        branch_document,
        parent_revision_id=first.revision_id,
        allow_branch=True,
    )
    assert branch.created
    assert branch.revision.parent_revision_id == first.revision_id
    # latest() still selects by insertion order; deliberate branch rows become
    # the reported head, so callers must bind them by id (documented contract).
    assert (
        repository.latest(first.document_id).revision_id
        == branch.revision.revision_id
    )
    assert repository.get(second.revision_id) is not None

    # allow_branch never permits a second root.
    with pytest.raises(SceneRevisionConflictError, match='duplicate root'):
        repository.save(
            make_empty_scene(first.document_id),
            parent_revision_id=None,
            allow_branch=True,
        )


def test_editor_view_state_round_trip_is_not_part_of_scene_revision(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    first = repository.save(make_f1_scene(), parent_revision_id=None).revision

    repository.save_view_state(
        first.document_id,
        selected_id='speaker-fl',
        selected_ids=('speaker-c', 'speaker-fl'),
        hidden_ids={'speaker-fr'},
        locked_ids={'speaker-fl', 'speaker-c'},
    )
    state = repository.view_state(first.document_id)

    assert state is not None
    assert state.selected_id == 'speaker-fl'
    assert state.selected_ids == ('speaker-c', 'speaker-fl')
    assert state.hidden_ids == ('speaker-fr',)
    assert state.locked_ids == ('speaker-c', 'speaker-fl')
    assert repository.latest(first.document_id).content_hash == first.content_hash


def test_legacy_view_state_schema_migrates_primary_selection(tmp_path: Path) -> None:
    path = tmp_path / 'legacy.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.execute(
            '''CREATE TABLE editor_view_states (
                document_id TEXT PRIMARY KEY,
                selected_id TEXT,
                hidden_ids_json TEXT NOT NULL,
                locked_ids_json TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            )'''
        )
        connection.execute(
            "INSERT INTO editor_view_states VALUES (?, ?, ?, ?, ?)",
            ('doc', 'speaker-fl', '[]', '[]', '2026-09-17T00:00:00+00:00'),
        )

    repository = SceneRepository(path)
    state = repository.view_state('doc')
    assert state is not None
    assert state.selected_id == 'speaker-fl'
    assert state.selected_ids == ('speaker-fl',)


def _corrupt_view_state(
    path: Path,
    document_id: str,
    *,
    selected_id=...,  # sentinel: keep the stored value unless overridden
    selected_ids_json: str | None = None,
    hidden_ids_json: str | None = None,
    locked_ids_json: str | None = None,
) -> None:
    """Overwrite one persisted editor_view_states row with malformed data."""
    assignments: list[str] = []
    values: list[object] = []
    if selected_id is not ...:
        assignments.append('selected_id=?')
        values.append(selected_id)
    for column, payload in (
        ('selected_ids_json', selected_ids_json),
        ('hidden_ids_json', hidden_ids_json),
        ('locked_ids_json', locked_ids_json),
    ):
        if payload is not None:
            assignments.append(f'{column}=?')
            values.append(payload)
    values.append(document_id)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            f"UPDATE editor_view_states SET {', '.join(assignments)} WHERE document_id=?",
            values,
        )


def _view_state_row_count(path: Path, document_id: str) -> int:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            'SELECT COUNT(*) FROM editor_view_states WHERE document_id=?',
            (document_id,),
        ).fetchone()[0]


@pytest.mark.parametrize(
    'column',
    ['selected_ids_json', 'hidden_ids_json', 'locked_ids_json'],
)
@pytest.mark.parametrize(
    'payload',
    [
        '{corrupt',          # not JSON at all
        '{"speaker-fl": 1}', # JSON object, not an array
        '"speaker-fl"',      # JSON string: iterates into characters
        '7',                 # JSON number
        'null',              # JSON null
        '[1, 2]',            # non-string members
        '[["speaker-fl"]]',  # nested arrays
    ],
)
def test_malformed_view_state_fails_soft_and_discards_row(
    tmp_path: Path, caplog, column: str, payload: str
) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    first = repository.save(make_f1_scene(), parent_revision_id=None).revision
    repository.save_view_state(
        first.document_id,
        selected_id='speaker-fl',
        selected_ids=('speaker-c', 'speaker-fl'),
        hidden_ids={'speaker-fr'},
        locked_ids={'speaker-c'},
    )
    _corrupt_view_state(repository.path, first.document_id, **{column: payload})

    with caplog.at_level(logging.WARNING, logger='htdt.native'):
        state = repository.view_state(first.document_id)

    # Non-authoritative UI state resets instead of propagating the corruption.
    assert state is None
    assert _view_state_row_count(repository.path, first.document_id) == 0
    assert any(
        'editor view state' in record.message and first.document_id in record.message
        for record in caplog.records
    )
    # The authoritative SceneRevision is byte/semantic unchanged.
    latest = repository.latest(first.document_id)
    assert latest is not None
    assert latest.content_hash == first.content_hash
    assert latest.document == first.document


def test_view_state_rejects_absurdly_large_id_list(
    tmp_path: Path, caplog, monkeypatch
) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    first = repository.save(make_f1_scene(), parent_revision_id=None).revision
    repository.save_view_state(
        first.document_id,
        selected_id='speaker-fl',
        selected_ids=('speaker-fl',),
        hidden_ids=set(),
        locked_ids=set(),
    )
    monkeypatch.setattr('htdt.cad_repository.MAX_VIEW_STATE_ID_COUNT', 2)
    _corrupt_view_state(
        repository.path,
        first.document_id,
        hidden_ids_json='["a", "b", "c"]',
    )

    with caplog.at_level(logging.WARNING, logger='htdt.native'):
        assert repository.view_state(first.document_id) is None
    assert _view_state_row_count(repository.path, first.document_id) == 0


def test_view_state_rejects_non_string_selected_id(tmp_path: Path, caplog) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    first = repository.save(make_f1_scene(), parent_revision_id=None).revision
    repository.save_view_state(
        first.document_id,
        selected_id='speaker-fl',
        hidden_ids=set(),
        locked_ids=set(),
    )
    # A BLOB survives TEXT affinity and reads back as bytes, not str.
    _corrupt_view_state(
        repository.path, first.document_id, selected_id=sqlite3.Binary(b'\xff')
    )

    with caplog.at_level(logging.WARNING, logger='htdt.native'):
        assert repository.view_state(first.document_id) is None
    assert _view_state_row_count(repository.path, first.document_id) == 0


def test_corrupt_view_state_is_replaced_by_next_persist(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    first = repository.save(make_f1_scene(), parent_revision_id=None).revision
    repository.save_view_state(
        first.document_id,
        selected_id='speaker-fl',
        hidden_ids=set(),
        locked_ids=set(),
    )
    _corrupt_view_state(
        repository.path, first.document_id, hidden_ids_json='{corrupt'
    )
    assert repository.view_state(first.document_id) is None

    repository.save_view_state(
        first.document_id,
        selected_id='speaker-c',
        hidden_ids={'speaker-fr'},
        locked_ids=set(),
    )
    state = repository.view_state(first.document_id)
    assert state is not None
    assert state.selected_id == 'speaker-c'
    assert state.hidden_ids == ('speaker-fr',)


def test_authoritative_revision_still_fails_closed_on_corruption(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    first = repository.save(make_f1_scene(), parent_revision_id=None).revision
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            "UPDATE scene_revisions SET payload_json='{corrupt' WHERE revision_id=?",
            (first.revision_id,),
        )

    # Fail-soft is scoped to non-authoritative view state only: a corrupt
    # SceneRevision payload must still raise instead of silently resetting.
    with pytest.raises(ValueError):
        repository.latest(first.document_id)
