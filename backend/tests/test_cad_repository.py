from contextlib import closing
import logging
from pathlib import Path
import sqlite3

import pytest

from htdt.cad_document import WorkingDocument
from htdt.cad_repository import SceneRepository, SceneRevisionConflictError
from htdt.cad_scene import (
    Position3,
    SceneDocument,
    canonical_scene_json,
    make_empty_scene,
    make_f1_scene,
    scene_content_hash,
)
from htdt.raw_mesh import import_raw_visual_mesh
from htdt.semantic_geometry import (
    SurfaceSemanticAssignment,
    convert_raw_visual_mesh_to_semantic_geometry,
    explicit_identity_source_to_scene_transform,
    make_semantic_geometry_conversion_request,
    raw_triangle_ids,
)


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


def _moved_scene(document, x_m: float):
    return document.model_copy(
        update={
            'entities': tuple(
                entity.model_copy(
                    update={'position': Position3(x_m=x_m, y_m=0.75, z_m=1.05)}
                )
                if entity.entity_id == 'speaker-fl'
                else entity
                for entity in document.entities
            )
        }
    )


def test_detached_revision_never_becomes_current_head(tmp_path: Path) -> None:
    """#626 regression: detached lineage must not hijack the document head."""
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

    # A detached revision descending from the historical first revision is
    # the newest row by seq, yet must not become the current document.
    detached = repository.save_detached_revision(
        _moved_scene(first.document, 1.6),
        parent_revision_id=first.revision_id,
        reason='test_fixture',
    )
    assert detached.created
    assert detached.revision.parent_revision_id == first.revision_id
    assert detached.revision.detached
    assert detached.revision.detached_reason == 'test_fixture'

    # The explicit head authority and the latest() alias keep returning the
    # true mainline head — not the newest inserted row.
    head = repository.current_head(first.document_id)
    assert head is not None
    assert head.revision_id == second.revision_id
    assert not head.detached
    assert repository.latest(first.document_id).revision_id == second.revision_id
    # Insertion chronology remains available as a separate, explicit query.
    assert (
        repository.most_recently_created_revision(first.document_id).revision_id
        == detached.revision.revision_id
    )
    # The detached row is still retrievable by exact id.
    fetched = repository.get(detached.revision.revision_id)
    assert fetched is not None and fetched.detached
    assert repository.get(second.revision_id) is not None

    # The next normal save compare-and-swaps against the true head, not the
    # detached tip.
    third = repository.save(
        _moved_scene(second.document, 1.7),
        parent_revision_id=second.revision_id,
    )
    assert third.created
    assert repository.current_head(first.document_id).revision_id == (
        third.revision.revision_id
    )

    # A detached revision is not a head, so it cannot parent a current save.
    with pytest.raises(SceneRevisionConflictError, match='stale parent'):
        repository.save(
            _moved_scene(detached.revision.document, 1.8),
            parent_revision_id=detached.revision.revision_id,
        )
    # Stale-parent protection still holds for the pre-detached head as well.
    with pytest.raises(SceneRevisionConflictError, match='stale parent'):
        repository.save(
            _moved_scene(first.document, 1.9),
            parent_revision_id=first.revision_id,
        )

    # A detached save always requires a parent; a second root is rejected
    # through the normal path too.
    with pytest.raises(ValueError, match='requires a parent'):
        repository.save_detached_revision(
            make_empty_scene(first.document_id),
            parent_revision_id=None,
        )
    with pytest.raises(SceneRevisionConflictError, match='duplicate root'):
        repository.save(make_empty_scene(first.document_id), parent_revision_id=None)


def test_detached_revision_does_not_clear_recovery_snapshot(tmp_path: Path) -> None:
    """#626: detached materialization must not disturb editing recovery state."""
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

    repository.save_detached_revision(
        _moved_scene(first.document, 1.7),
        parent_revision_id=first.revision_id,
        reason='analytical_candidate',
    )

    # The recovery snapshot still binds to the true editing source — the
    # detached insert neither cleared it nor re-pointed it.
    reopened = repository.recovery(first.document_id)
    assert reopened is not None
    assert reopened.source_revision_id == first.revision_id
    assert reopened.document.entity('speaker-fl').position.x_m == 1.6
    assert repository.current_head(first.document_id).revision_id == first.revision_id


def test_head_generation_tracks_mainline_advances_only(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    first = repository.save(make_f1_scene(), parent_revision_id=None).revision
    second = repository.save(
        _moved_scene(first.document, 1.5),
        parent_revision_id=first.revision_id,
    ).revision
    repository.save_detached_revision(
        _moved_scene(first.document, 1.6),
        parent_revision_id=first.revision_id,
    )

    with closing(sqlite3.connect(repository.path)) as connection:
        row = connection.execute(
            'SELECT head_revision_id, generation FROM scene_document_heads '
            'WHERE document_id=?',
            (first.document_id,),
        ).fetchone()
        detached_flag = connection.execute(
            'SELECT detached FROM scene_revisions ORDER BY seq DESC LIMIT 1'
        ).fetchone()[0]
    assert row[0] == second.revision_id
    assert row[1] == 2
    assert detached_flag == 1


def test_existing_database_backfills_head_from_mainline_ancestry(tmp_path: Path) -> None:
    """Pre-#626 databases get a deterministic head reconstructed by replaying
    single-head ancestry; off-mainline rows are flagged detached, and the
    newest seq never wins just by insertion order."""
    path = tmp_path / 'legacy.sqlite3'
    root = make_f1_scene()
    second = _moved_scene(root, 1.5)
    branch = _moved_scene(root, 1.6)
    payload = {
        'root': (canonical_scene_json(root), scene_content_hash(root), None),
        'second': (canonical_scene_json(second), scene_content_hash(second), 'rev-root'),
        # The branch row descends from the root but is inserted last: under
        # the old contract it would have won latest() by seq alone.
        'branch': (canonical_scene_json(branch), scene_content_hash(branch), 'rev-root'),
    }
    with sqlite3.connect(path) as connection:
        connection.execute(
            '''CREATE TABLE scene_revisions (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                revision_id TEXT NOT NULL UNIQUE,
                document_id TEXT NOT NULL,
                parent_revision_id TEXT,
                created_at_utc TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(parent_revision_id) REFERENCES scene_revisions(revision_id)
            )'''
        )
        for revision_id, key in (
            ('rev-root', 'root'),
            ('rev-second', 'second'),
            ('rev-branch', 'branch'),
        ):
            payload_json, content_hash, parent = payload[key]
            connection.execute(
                'INSERT INTO scene_revisions('
                'revision_id, document_id, parent_revision_id, created_at_utc,'
                ' content_hash, payload_json) VALUES (?, ?, ?, ?, ?, ?)',
                (
                    revision_id,
                    root.document_id,
                    parent,
                    '2026-09-17T00:00:00+00:00',
                    content_hash,
                    payload_json,
                ),
            )

    repository = SceneRepository(path)

    # The reconstructed head is the last row of the uninterrupted mainline
    # (rev-second), not the newest inserted row (rev-branch).
    head = repository.current_head(root.document_id)
    assert head is not None
    assert head.revision_id == 'rev-second'
    assert repository.latest(root.document_id).revision_id == 'rev-second'
    assert (
        repository.most_recently_created_revision(root.document_id).revision_id
        == 'rev-branch'
    )
    branch_revision = repository.get('rev-branch')
    assert branch_revision is not None
    assert branch_revision.detached

    # Normal saves compare-and-swap off the reconstructed head.
    third = repository.save(
        _moved_scene(second, 1.7),
        parent_revision_id='rev-second',
    )
    assert third.created
    assert repository.current_head(root.document_id).revision_id == (
        third.revision.revision_id
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


_KEYED_PAYLOAD_STORES: dict[str, tuple[str, str]] = {
    'named_view': ('editor_named_views', 'view_id'),
    'underlay': ('floor_plan_underlays', 'underlay_id'),
    'seating_spec': ('seating_layout_specs', 'spec_id'),
}


def _corrupt_keyed_payload(
    path: Path,
    store: str,
    document_id: str,
    record_id: str,
    payload_json: str = '{corrupt',
) -> None:
    """Overwrite one keyed editor-payload row with malformed JSON."""
    table, key_column = _KEYED_PAYLOAD_STORES[store]
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            f'UPDATE {table} SET payload_json=? WHERE document_id=? AND {key_column}=?',
            (payload_json, document_id, record_id),
        )


def _payload_row_count(path: Path, store: str, document_id: str) -> int:
    table, _ = _KEYED_PAYLOAD_STORES[store]
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            f'SELECT COUNT(*) FROM {table} WHERE document_id=?',
            (document_id,),
        ).fetchone()[0]


def test_corrupt_named_view_purges_only_that_record(tmp_path: Path, caplog) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    first = repository.save(make_f1_scene(), parent_revision_id=None).revision
    repository.save_named_view(first.document_id, 'view-a', {'a': 1})
    repository.save_named_view(first.document_id, 'view-b', {'b': 2})
    repository.save_named_view(first.document_id, 'view-c', {'c': 3})
    _corrupt_keyed_payload(repository.path, 'named_view', first.document_id, 'view-b')

    with caplog.at_level(logging.WARNING, logger='htdt.native'):
        views = repository.named_views(first.document_id)
    assert [record.record_id for record in views] == ['view-a', 'view-c']
    assert any(
        'named_view' in record.message
        and 'view-b' in record.message
        and first.document_id in record.message
        for record in caplog.records
    )

    # Healthy siblings must survive the read-side cleanup itself.
    caplog.clear()
    assert [record.record_id for record in repository.named_views(first.document_id)] == [
        'view-a',
        'view-c',
    ]
    assert _payload_row_count(repository.path, 'named_view', first.document_id) == 2
    # ...and a repository restart.
    assert [
        record.record_id
        for record in SceneRepository(repository.path).named_views(first.document_id)
    ] == ['view-a', 'view-c']


def test_corrupt_underlay_and_seating_spec_purge_single_records(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    first = repository.save(make_f1_scene(), parent_revision_id=None).revision
    repository.save_underlay(first.document_id, 'underlay-a', {'a': 1})
    repository.save_underlay(first.document_id, 'underlay-b', {'b': 2})
    repository.save_seating_spec(first.document_id, 'spec-a', {'a': 1})
    repository.save_seating_spec(first.document_id, 'spec-b', {'b': 2})
    repository.save_seating_spec(first.document_id, 'spec-c', {'c': 3})
    _corrupt_keyed_payload(
        repository.path, 'underlay', first.document_id, 'underlay-b'
    )
    _corrupt_keyed_payload(repository.path, 'seating_spec', first.document_id, 'spec-c')

    assert [record.record_id for record in repository.underlays(first.document_id)] == [
        'underlay-a'
    ]
    assert [record.record_id for record in repository.seating_specs(first.document_id)] == [
        'spec-a',
        'spec-b',
    ]
    assert _payload_row_count(repository.path, 'underlay', first.document_id) == 1
    assert _payload_row_count(repository.path, 'seating_spec', first.document_id) == 2


def test_corrupt_singleton_payload_resets_only_that_store(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    first = repository.save(make_f1_scene(), parent_revision_id=None).revision
    repository.save_camera_state(first.document_id, {'zoom': 2})
    repository.save_named_view(first.document_id, 'view-a', {'a': 1})
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            "UPDATE editor_camera_states SET payload_json='{corrupt' WHERE document_id=?",
            (first.document_id,),
        )

    assert repository.camera_state(first.document_id) is None
    # The singleton reset must not bleed into keyed sibling stores.
    assert [record.record_id for record in repository.named_views(first.document_id)] == [
        'view-a'
    ]
    with closing(sqlite3.connect(repository.path)) as connection:
        assert connection.execute(
            'SELECT COUNT(*) FROM editor_camera_states WHERE document_id=?',
            (first.document_id,),
        ).fetchone()[0] == 0


def test_semantically_invalid_payload_row_is_handled_per_record(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    first = repository.save(make_f1_scene(), parent_revision_id=None).revision
    repository.save_named_view(first.document_id, 'view-a', {'a': 1})
    repository.save_named_view(first.document_id, 'view-b', {'b': 2})
    # Valid JSON, wrong shape for a payload record: per-record recovery only.
    _corrupt_keyed_payload(
        repository.path, 'named_view', first.document_id, 'view-b', '["not-a-dict"]'
    )

    assert [record.record_id for record in repository.named_views(first.document_id)] == [
        'view-a'
    ]
    assert _payload_row_count(repository.path, 'named_view', first.document_id) == 1


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


_CLOSED_TETRA = b'v 0 0 0\nv 5 0 0\nv 0 4 0\nv 0 0 2.5\nf 1 3 2\nf 1 2 4\nf 1 4 3\nf 2 3 4\n'


def _tetra_geometry(source_revision_id: str | None):
    mesh = import_raw_visual_mesh(_CLOSED_TETRA, source_name='revision-test.obj')
    request = make_semantic_geometry_conversion_request(
        mesh,
        source_scene_revision_id=source_revision_id,
        source_to_scene_transform=explicit_identity_source_to_scene_transform(
            reason='test mesh coordinates are explicit HTDT metres',
        ),
        surface_assignments=(
            SurfaceSemanticAssignment(
                surface_key='room-shell',
                triangle_ids=raw_triangle_ids(mesh),
                semantic_class='room_boundary',
            ),
        ),
    )
    return convert_raw_visual_mesh_to_semantic_geometry(mesh, request)


def _geometry_document(document_id: str, geometry) -> SceneDocument:
    return SceneDocument(
        document_id=document_id,
        schema_version=4,
        room=None,
        entities=(),
        r120_semantic_geometry=geometry,
    )


def test_save_reintroduces_historical_geometry_with_true_binding(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    first = repository.save(
        _geometry_document('geo-doc', _tetra_geometry(None)),
        parent_revision_id=None,
    ).revision
    second = repository.save(
        _geometry_document('geo-doc', _tetra_geometry(first.revision_id)),
        parent_revision_id=first.revision_id,
    ).revision
    third = repository.save(
        _geometry_document('geo-doc', _tetra_geometry(second.revision_id)),
        parent_revision_id=second.revision_id,
    ).revision

    # Restoring an old revision reintroduces its superseded geometry: the
    # source pointer it carries is the TRUE derivation revision, not the new
    # parent — the restored payload stays byte-identical to the stored one.
    restored = repository.save(first.document, parent_revision_id=third.revision_id)
    assert restored.created
    assert restored.revision.parent_revision_id == third.revision_id
    assert restored.revision.content_hash == first.content_hash
    assert (
        restored.revision.document.r120_semantic_geometry.geometry_id
        == first.document.r120_semantic_geometry.geometry_id
    )


def test_new_geometry_must_still_bind_to_the_parent(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    first = repository.save(
        _geometry_document('geo-doc', _tetra_geometry(None)),
        parent_revision_id=None,
    ).revision
    # A geometry that never existed in this document's lineage still has to
    # declare the exact parent it was derived from.
    forged = _tetra_geometry('revision-never-committed')
    with pytest.raises(ValueError, match='must bind to the exact parent'):
        repository.save(
            _geometry_document('geo-doc', forged),
            parent_revision_id=first.revision_id,
        )


def test_get_memoizes_revisions_but_never_misses(tmp_path: Path) -> None:
    """REV25-UIPERF: repeated ``get`` re-fetches the row every call but
    memoizes deserialization, returning the same immutable revision."""
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision

    first = repository.get(revision.revision_id)
    second = repository.get(revision.revision_id)
    assert first is second

    # A miss is intentionally not memoized: a revision first requested
    # before its writer commits must resolve once it exists.
    assert repository.get('revision-not-written') is None
