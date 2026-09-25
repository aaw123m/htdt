from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3

import pytest

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import RoomPrism, make_empty_scene
from htdt.native_backup import DATABASE_NAME
from htdt.project_lifecycle import (
    ProjectDeletionBlockedError,
    ProjectDeletionStaleError,
    ProjectLibrary,
    ProjectLifecycleError,
    ProjectNotFoundError,
)


def _data_dir(tmp_path: Path) -> Path:
    return tmp_path / 'data'


def _repository(tmp_path: Path) -> SceneRepository:
    return SceneRepository(_data_dir(tmp_path) / DATABASE_NAME)


def _seed_document(tmp_path: Path, document_id: str) -> None:
    repository = _repository(tmp_path)
    repository.save(
        make_empty_scene(document_id), parent_revision_id=None
    )


def _row_count(path: Path, table: str, document_id: str) -> int:
    with closing(sqlite3.connect(path)) as connection:
        (count,) = connection.execute(
            f'SELECT COUNT(*) FROM {table} WHERE document_id=?',
            (document_id,),
        ).fetchone()
    return int(count)


def test_register_and_list_projects(tmp_path: Path) -> None:
    library = ProjectLibrary(_data_dir(tmp_path) / DATABASE_NAME)
    record = library.register_project('doc-a', 'Room A')
    assert record.status == 'active'
    assert record.document_id == 'doc-a'
    # Idempotent: registering again returns the same project.
    again = library.register_project('doc-a', 'Room A renamed')
    assert again.project_id == record.project_id
    assert [p.project_id for p in library.list_projects()] == [
        record.project_id
    ]


def test_archive_hides_from_active_list_and_is_reversible(
    tmp_path: Path,
) -> None:
    library = ProjectLibrary(_data_dir(tmp_path) / DATABASE_NAME)
    record = library.register_project('doc-a', 'Room A')
    library.register_project('doc-b', 'Room B')

    archived = library.archive_project(record.project_id)
    assert archived.status == 'archived'
    assert archived.archived_at_utc is not None
    # Retire-by-default: archived projects disappear from the active list
    # without anything being destroyed.
    assert [p.document_id for p in library.list_projects()] == ['doc-b']
    assert len(library.list_projects(include_archived=True)) == 2

    restored = library.unarchive_project(record.project_id)
    assert restored.status == 'active'
    assert restored.archived_at_utc is None
    assert len(library.list_projects()) == 2


def test_adopt_existing_documents_registers_prior_data(tmp_path: Path) -> None:
    _seed_document(tmp_path, 'doc-legacy')
    library = ProjectLibrary(_data_dir(tmp_path) / DATABASE_NAME)
    adopted = library.adopt_existing_documents()
    assert [p.document_id for p in adopted] == ['doc-legacy']
    assert adopted[0].status == 'active'


def test_deletion_requires_archive_first(tmp_path: Path) -> None:
    _seed_document(tmp_path, 'doc-a')
    library = ProjectLibrary(_data_dir(tmp_path) / DATABASE_NAME)
    record = library.register_project('doc-a', 'Room A')

    plan = library.plan_project_deletion(record.project_id)
    kinds = {b.kind for b in plan.hard_blockers}
    assert 'project_not_archived' in kinds
    assert not plan.executable

    with pytest.raises(ProjectDeletionBlockedError):
        library.delete_project(record.project_id)
    # Nothing was removed.
    assert _row_count(
        _data_dir(tmp_path) / DATABASE_NAME, 'scene_revisions', 'doc-a'
    ) == 1


def test_deletion_plan_reports_counts_and_pending_work(tmp_path: Path) -> None:
    _seed_document(tmp_path, 'doc-a')
    library = ProjectLibrary(_data_dir(tmp_path) / DATABASE_NAME)
    record = library.archive_project(
        library.register_project('doc-a', 'Room A').project_id
    )

    plan = library.plan_project_deletion(record.project_id)
    assert plan.executable
    assert plan.document_id == 'doc-a'
    assert plan.total_rows > 0
    assert plan.estimated_bytes > 0
    tables = {a.table for a in plan.authorities}
    assert 'scene_revisions' in tables
    assert 'scene_document_heads' in tables
    assert plan.pending_mission_count == 0
    assert plan.pending_inbox_item_count == 0


def test_delete_project_removes_only_target_document(tmp_path: Path) -> None:
    _seed_document(tmp_path, 'doc-a')
    _seed_document(tmp_path, 'doc-b')
    library = ProjectLibrary(_data_dir(tmp_path) / DATABASE_NAME)
    target = library.archive_project(
        library.register_project('doc-a', 'Room A').project_id
    )
    other = library.register_project('doc-b', 'Room B')

    tombstone = library.delete_project(target.project_id)

    database = _data_dir(tmp_path) / DATABASE_NAME
    assert _row_count(database, 'scene_revisions', 'doc-a') == 0
    assert _row_count(database, 'scene_document_heads', 'doc-a') == 0
    # The other project is untouched.
    assert _row_count(database, 'scene_revisions', 'doc-b') == 1
    # Registry row removed; a bounded tombstone records what was removed.
    assert library.find_by_document('doc-a') is None
    assert library.get_project(other.project_id).status == 'active'
    tombstones = library.list_tombstones()
    assert len(tombstones) == 1
    assert tombstones[0].project_id == target.project_id
    assert tombstones[0].document_id == 'doc-a'
    assert tombstones[0].removed_rows > 0
    # Foreign-key integrity survived the atomic removal.
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []


def test_descendant_projects_block_deletion(tmp_path: Path) -> None:
    library = ProjectLibrary(_data_dir(tmp_path) / DATABASE_NAME)
    parent = library.register_project('doc-parent', 'Parent')
    library.register_project(
        'doc-child', 'Child', cloned_from_project_id=parent.project_id
    )
    library.archive_project(parent.project_id)

    plan = library.plan_project_deletion(parent.project_id)
    kinds = {b.kind for b in plan.hard_blockers}
    assert 'active_descendants' in kinds
    with pytest.raises(ProjectDeletionBlockedError):
        library.delete_project(parent.project_id)


def test_pending_inbox_items_block_deletion(tmp_path: Path) -> None:
    _seed_document(tmp_path, 'doc-a')
    library = ProjectLibrary(_data_dir(tmp_path) / DATABASE_NAME)
    record = library.archive_project(
        library.register_project('doc-a', 'Room A').project_id
    )
    database = _data_dir(tmp_path) / DATABASE_NAME
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.executescript(
            '''
            CREATE TABLE IF NOT EXISTS capture_ingestion_lineages (
                lineage_digest TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS capture_inbox_items (
                lineage_digest TEXT PRIMARY KEY
                    REFERENCES capture_ingestion_lineages(lineage_digest),
                inbox_item_id TEXT NOT NULL UNIQUE,
                scope TEXT NOT NULL,
                capture_series_id TEXT NOT NULL,
                capture_revision_id TEXT NOT NULL,
                bundle_digest TEXT NOT NULL,
                parent_revision_id TEXT,
                capture_session_ids_json TEXT NOT NULL,
                coordinate_space_ids_json TEXT NOT NULL,
                arrival_source TEXT NOT NULL,
                source_detail TEXT NOT NULL,
                first_arrived_at_utc TEXT NOT NULL,
                arrival_count INTEGER NOT NULL,
                primary_classification TEXT NOT NULL,
                classification_flags_json TEXT NOT NULL,
                conflict_lineage_digest TEXT,
                bundle_validation TEXT NOT NULL,
                validation_detail TEXT NOT NULL,
                dependency_state TEXT NOT NULL,
                dependency_detail TEXT NOT NULL,
                alignment_state TEXT NOT NULL,
                alignment_detail TEXT NOT NULL,
                world_alignment_authority_id TEXT,
                evidence_conflict_state TEXT NOT NULL,
                evidence_conflict_detail TEXT NOT NULL,
                disposition TEXT NOT NULL,
                disposition_reason TEXT NOT NULL,
                disposition_at_utc TEXT,
                operator_notes TEXT NOT NULL,
                has_connected_space_document INTEGER NOT NULL
            );
            '''
        )
        connection.execute(
            "INSERT INTO capture_ingestion_lineages VALUES ('lin-1')"
        )
        connection.execute(
            'INSERT INTO capture_inbox_items('
            'lineage_digest, inbox_item_id, scope, capture_series_id, '
            'capture_revision_id, bundle_digest, capture_session_ids_json, '
            'coordinate_space_ids_json, arrival_source, source_detail, '
            'first_arrived_at_utc, arrival_count, primary_classification, '
            'classification_flags_json, bundle_validation, validation_detail, '
            'dependency_state, dependency_detail, alignment_state, '
            'alignment_detail, evidence_conflict_state, '
            'evidence_conflict_detail, disposition, disposition_reason, '
            'operator_notes, has_connected_space_document'
            ") VALUES ('lin-1', 'item-1', 'doc-a', 'series', 'rev', 'bundle', "
            "'[]', '[]', 'receiver', '', '2026-01-01T00:00:00Z', 1, "
            "'extends_known_head', '[]', 'valid', '', 'satisfied', '', "
            "'aligned', '', 'none', '', 'pending', '', '', 0)"
        )

    plan = library.plan_project_deletion(record.project_id)
    assert plan.pending_inbox_item_count == 1
    kinds = {b.kind for b in plan.hard_blockers}
    assert 'pending_inbox_items' in kinds


def test_unarchive_between_preview_and_delete_is_blocked(tmp_path: Path) -> None:
    """A blocker appearing after the preview still stops the delete."""
    _seed_document(tmp_path, 'doc-a')
    library = ProjectLibrary(_data_dir(tmp_path) / DATABASE_NAME)
    record = library.archive_project(
        library.register_project('doc-a', 'Room A').project_id
    )
    plan = library.plan_project_deletion(record.project_id)
    assert plan.executable

    # The world moved: the project is active again. A stale expected plan
    # must not authorize the delete — the in-transaction revalidation
    # sees the live status (#753).
    library.unarchive_project(record.project_id)
    with pytest.raises(ProjectDeletionBlockedError):
        library.delete_project(record.project_id, expected_plan=plan)
    assert _row_count(
        _data_dir(tmp_path) / DATABASE_NAME, 'scene_revisions', 'doc-a'
    ) == 1
    assert library.get_project(record.project_id).status == 'active'


def test_stale_preview_fingerprint_refuses_delete(tmp_path: Path) -> None:
    _seed_document(tmp_path, 'doc-a')
    library = ProjectLibrary(_data_dir(tmp_path) / DATABASE_NAME)
    record = library.archive_project(
        library.register_project('doc-a', 'Room A').project_id
    )
    plan = library.plan_project_deletion(record.project_id)
    assert plan.executable
    # Another revision lands after the preview — the approved plan no
    # longer describes the world under the lock.
    repository = _repository(tmp_path)
    head = repository.current_head('doc-a')
    changed = head.document.model_copy(
        update={'room': RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4)}
    )
    repository.save(changed, parent_revision_id=head.revision_id)
    with pytest.raises(ProjectDeletionStaleError):
        library.delete_project(record.project_id, expected_plan=plan)


def test_matching_expected_plan_executes(tmp_path: Path) -> None:
    _seed_document(tmp_path, 'doc-a')
    library = ProjectLibrary(_data_dir(tmp_path) / DATABASE_NAME)
    record = library.archive_project(
        library.register_project('doc-a', 'Room A').project_id
    )
    plan = library.plan_project_deletion(record.project_id)
    tombstone = library.delete_project(record.project_id, expected_plan=plan)
    assert tombstone.document_id == 'doc-a'
    assert tombstone.removed_rows == plan.total_rows


def test_payload_document_id_drift_blocks_delete(tmp_path: Path) -> None:
    """Denormalized document_id columns must agree with the payload."""
    _seed_document(tmp_path, 'doc-a')
    library = ProjectLibrary(_data_dir(tmp_path) / DATABASE_NAME)
    record = library.archive_project(
        library.register_project('doc-a', 'Room A').project_id
    )
    database = _data_dir(tmp_path) / DATABASE_NAME
    with closing(sqlite3.connect(database)) as connection, connection:
        # Corrupt the canonical payload so its document_id disagrees with
        # the denormalized column.
        revision_id, payload_json = connection.execute(
            'SELECT revision_id, payload_json FROM scene_revisions '
            'WHERE document_id=?',
            ('doc-a',),
        ).fetchone()
        payload = json.loads(payload_json)
        payload['document_id'] = 'doc-other'
        connection.execute(
            'UPDATE scene_revisions SET payload_json=? WHERE revision_id=?',
            (json.dumps(payload), revision_id),
        )
    with pytest.raises(ProjectLifecycleError, match='disagrees'):
        library.delete_project(record.project_id)
    # Nothing was removed.
    assert _row_count(database, 'scene_revisions', 'doc-a') == 1


def test_tombstone_records_actual_deleted_counts(tmp_path: Path) -> None:
    """The tombstone reflects what the transaction removed, not a stale
    preview's guess (#753)."""
    _seed_document(tmp_path, 'doc-a')
    library = ProjectLibrary(_data_dir(tmp_path) / DATABASE_NAME)
    record = library.archive_project(
        library.register_project('doc-a', 'Room A').project_id
    )
    stale_plan = library.plan_project_deletion(record.project_id)
    # More data lands after the preview; the delete still records truth.
    repository = _repository(tmp_path)
    head = repository.current_head('doc-a')
    changed = head.document.model_copy(
        update={'room': RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4)}
    )
    repository.save(changed, parent_revision_id=head.revision_id)
    tombstone = library.delete_project(record.project_id)
    authorities = json.loads(tombstone.authorities_json)
    assert tombstone.removed_rows > stale_plan.total_rows
    assert sum(a['row_count'] for a in authorities) == tombstone.removed_rows
    revision_entry = next(
        a for a in authorities if a['table'] == 'scene_revisions'
    )
    assert revision_entry['row_count'] == 2
    database = _data_dir(tmp_path) / DATABASE_NAME
    assert _row_count(database, 'scene_revisions', 'doc-a') == 0


def test_unknown_project_raises(tmp_path: Path) -> None:
    library = ProjectLibrary(_data_dir(tmp_path) / DATABASE_NAME)
    with pytest.raises(ProjectNotFoundError):
        library.get_project('nope')
    with pytest.raises(ProjectNotFoundError):
        library.plan_project_deletion('nope')
    with pytest.raises(ProjectNotFoundError):
        library.archive_project('nope')
