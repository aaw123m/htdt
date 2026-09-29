"""Round 19 (REV19-SWEEP2): convergence-sweep regressions.

A fresh-eyes pass found the r18 ORDER-BY family had further stragglers —
timestamp-only listings whose same-instant rows resolved by SQLite rowid
instead of a stable key — plus one more unclosed-handle sibling and the
Windows-broken path expectations from the r18 deferred list:

- The legacy dev store (``htdt.database``) sorted nine listings on
  ``created_at``/``imported_at`` alone; the row's own ``id`` is now the
  DESC tiebreak, matching the convention the rest of the schema uses.
- ``CaptureIngestionRepository`` sorted the revision registry
  (``capture_series_id, registered_at_utc``), revision conflicts
  (``recorded_at_utc``) and retained bundles (``created_at``) without a
  unique last key; the migration-time registry rebuild had the same
  shape on ``recorded_at_utc``.
- ``ProjectLibrary.delete_project``'s tombstone read-back picked LIMIT 1
  on ``deleted_at_utc`` alone; ``tombstone_id`` is now the tiebreak, same
  as the retention prune already used.
- ``SceneRepository._payloads`` keyed stores sorted on ``updated_at_utc``
  alone; the store's key column is now the ASC tiebreak.
- ``validate_bundle`` was the third capture-bundle open path that leaked
  the ``ZipSource``'s ``ZipFile`` handle — ``ZipSource.__init__`` and
  ``capture_reference`` already closed explicitly.
- Three tests formatted the stored managed-asset ``relative_path`` with
  ``str(Path(...) / digest)`` — backslashes on Windows — while the stored
  contract is POSIX (``safe_managed_relative_path`` requires a
  ``PurePosixPath`` and every write site stores ``.as_posix()``). The
  expectations now assert ``.as_posix()`` (the r18 deferred item).
"""

from __future__ import annotations

import sqlite3
import zipfile
from contextlib import closing
from hashlib import sha256
from pathlib import Path

from htdt import capture_bundle
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_empty_scene
from htdt.capture_ingestion_transaction import CaptureIngestionRepository
from htdt.database import Store
from htdt.native_backup import DATABASE_NAME
import htdt.project_lifecycle as project_lifecycle
from htdt.project_lifecycle import ProjectLibrary

import capture_fixture_support as support

STAMP = '2026-01-01T00:00:00+00:00'

# -- dev-store listing tiebreaks (r18 sibling class) ------------------------


def _dev_store(tmp_path: Path) -> Store:
    return Store(tmp_path / 'devstore')


def test_list_projects_orders_ties_by_id(tmp_path: Path) -> None:
    store = _dev_store(tmp_path)
    with store.connect() as db, db:
        for project_id in ('aaa-tie', 'zzz-tie'):
            db.execute(
                'INSERT INTO projects(id, name, created_at) '
                'VALUES (?, ?, ?)',
                (project_id, project_id, STAMP),
            )
    assert [p['id'] for p in store.list_projects()] == [
        'zzz-tie',
        'aaa-tie',
    ]


def test_list_sessions_orders_ties_by_id(tmp_path: Path) -> None:
    store = _dev_store(tmp_path)
    with store.connect() as db, db:
        db.execute(
            'INSERT INTO projects(id, name, created_at) VALUES (?, ?, ?)',
            ('p-1', 'p-1', STAMP),
        )
        for session_id in ('aaa-tie', 'zzz-tie'):
            db.execute(
                'INSERT INTO sessions('
                'id, project_id, purpose, started_at, notes, created_at'
                ') VALUES (?, ?, NULL, NULL, NULL, ?)',
                (session_id, 'p-1', STAMP),
            )
    assert [s['id'] for s in store.list_sessions('p-1')] == [
        'zzz-tie',
        'aaa-tie',
    ]


def _seed_context(db: sqlite3.Connection) -> None:
    db.execute(
        'INSERT INTO projects(id, name, created_at) VALUES (?, ?, ?)',
        ('p-1', 'p-1', STAMP),
    )
    db.execute(
        'INSERT INTO contexts('
        'id, project_id, revision_number, parent_context_id, created_at, '
        'payload_json'
        ') VALUES (?, ?, 1, NULL, ?, ?)',
        ('ctx-1', 'p-1', STAMP, '{}'),
    )


def test_list_constraint_sets_orders_ties_by_id(tmp_path: Path) -> None:
    store = _dev_store(tmp_path)
    with store.connect() as db, db:
        _seed_context(db)
        for set_id in ('aaa-tie', 'zzz-tie'):
            db.execute(
                'INSERT INTO constraint_sets('
                'id, project_id, context_id, name, spec_json, '
                'spec_sha256, created_at'
                ') VALUES (?, ?, ?, NULL, ?, ?, ?)',
                (set_id, 'p-1', 'ctx-1', '{}', set_id, STAMP),
            )
    # Both the project-wide and the context-filtered variant share the
    # same ORDER BY — exercise the filtered one.
    assert [
        s['id'] for s in store.list_constraint_sets('p-1', 'ctx-1')
    ] == ['zzz-tie', 'aaa-tie']


def test_list_search_specs_orders_ties_by_id(tmp_path: Path) -> None:
    store = _dev_store(tmp_path)
    with store.connect() as db, db:
        _seed_context(db)
        db.execute(
            'INSERT INTO constraint_sets('
            'id, project_id, context_id, name, spec_json, spec_sha256, '
            'created_at'
            ') VALUES (?, ?, ?, NULL, ?, ?, ?)',
            ('cs-1', 'p-1', 'ctx-1', '{}', 'cs-1', STAMP),
        )
        for spec_id in ('aaa-tie', 'zzz-tie'):
            db.execute(
                'INSERT INTO search_specs('
                'id, project_id, context_id, constraint_set_id, name, '
                'spec_json, spec_sha256, created_at'
                ') VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
                (spec_id, 'p-1', 'ctx-1', 'cs-1', '{}', spec_id, STAMP),
            )
    assert [s['id'] for s in store.list_search_specs('p-1')] == [
        'zzz-tie',
        'aaa-tie',
    ]


def _seed_measurement(db: sqlite3.Connection, measurement_id: str) -> None:
    db.execute(
        'INSERT INTO measurements('
        'id, project_id, context_id, session_id, channel_role, '
        'evidence_type, source_speaker_ids_json, radiation_scope, '
        'routing_evidence, captured_at, imported_at, notes, '
        'quality_status, quality_reasons_json, quality_source, '
        'repeat_group'
        ') VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?, NULL)',
        (
            measurement_id,
            'p-1',
            'ctx-1',
            'ch',
            'et',
            '[]',
            'scope',
            'unknown',
            STAMP,
            'unknown',
            '[]',
            'unknown',
        ),
    )
    db.execute(
        'INSERT INTO assets('
        'sha256, relative_path, original_filename, size_bytes, created_at'
        ') VALUES (?, ?, ?, 1, ?)',
        (f'{measurement_id}-asset', 'p', 'f.wav', STAMP),
    )
    db.execute(
        'INSERT INTO datasets('
        'id, measurement_id, asset_sha256, kind, frequency_blob, '
        'level_blob, phase_blob, metadata_json, dataset_sha256, created_at'
        ') VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?)',
        (
            f'{measurement_id}-ds',
            measurement_id,
            f'{measurement_id}-asset',
            'kind',
            b'\x00' * 8,
            b'\x00' * 8,
            '{}',
            STAMP,
        ),
    )


def test_list_measurements_orders_ties_by_id(tmp_path: Path) -> None:
    store = _dev_store(tmp_path)
    with store.connect() as db, db:
        _seed_context(db)
        _seed_measurement(db, 'aaa-tie')
        _seed_measurement(db, 'zzz-tie')
    assert [m['id'] for m in store.list_measurements('p-1')] == [
        'zzz-tie',
        'aaa-tie',
    ]


def test_list_attachments_orders_ties_by_id(tmp_path: Path) -> None:
    store = _dev_store(tmp_path)
    with store.connect() as db, db:
        db.execute(
            'INSERT INTO projects(id, name, created_at) VALUES (?, ?, ?)',
            ('p-1', 'p-1', STAMP),
        )
        db.execute(
            'INSERT INTO assets('
            'sha256, relative_path, original_filename, size_bytes, '
            'created_at'
            ') VALUES (?, ?, ?, 1, ?)',
            ('asset-1', 'p', 'f.wav', STAMP),
        )
        for link_id in ('aaa-tie', 'zzz-tie'):
            db.execute(
                'INSERT INTO asset_links('
                'id, project_id, asset_sha256, measurement_id, '
                'context_id, kind, label, filename, created_at'
                ') VALUES (?, ?, ?, NULL, NULL, ?, NULL, ?, ?)',
                (link_id, 'p-1', 'asset-1', 'kind', 'f.wav', STAMP),
            )
    assert [a['id'] for a in store.list_attachments('p-1')] == [
        'zzz-tie',
        'aaa-tie',
    ]


def test_list_comparisons_orders_ties_by_id(tmp_path: Path) -> None:
    store = _dev_store(tmp_path)
    with store.connect() as db, db:
        _seed_context(db)
        _seed_measurement(db, 'm-1')
        _seed_measurement(db, 'm-2')
        for comparison_id in ('aaa-tie', 'zzz-tie'):
            db.execute(
                'INSERT INTO comparisons('
                'id, project_id, dataset_a_id, dataset_b_id, spec_json, '
                'result_json, created_at'
                ') VALUES (?, ?, ?, ?, ?, ?, ?)',
                (
                    comparison_id,
                    'p-1',
                    'm-1-ds',
                    'm-2-ds',
                    '{}',
                    '{}',
                    STAMP,
                ),
            )
    assert [c['id'] for c in store.list_comparisons('p-1')] == [
        'zzz-tie',
        'aaa-tie',
    ]


# -- capture-ingestion registry tiebreaks ---------------------------------


def _ingestion(tmp_path: Path) -> CaptureIngestionRepository:
    return CaptureIngestionRepository(SceneRepository(tmp_path / 'cad.sqlite3'))


def test_registered_revisions_order_ties_by_revision_id(
    tmp_path: Path,
) -> None:
    repository = _ingestion(tmp_path)
    with closing(repository._connect()) as connection, connection:
        for revision_id in ('zzz-tie', 'aaa-tie'):
            connection.execute(
                'INSERT INTO capture_revisions('
                'capture_revision_id, capture_series_id, '
                'parent_revision_id, bundle_digest, capture_schema, '
                'capture_schema_version, topology_state, '
                'first_lineage_digest, registered_at_utc'
                ') VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)',
                (
                    revision_id,
                    'series-1',
                    f'{revision_id}-digest',
                    'schema',
                    'v1',
                    'topology',
                    f'{revision_id}-lineage',
                    STAMP,
                ),
            )
    assert tuple(
        record.capture_revision_id
        for record in repository.list_registered_revisions()
    ) == ('aaa-tie', 'zzz-tie')


def test_revision_conflicts_order_ties_by_insertion(
    tmp_path: Path,
) -> None:
    """Conflicts share the composite PK ``(capture_revision_id, detail)``;
    the rowid tiebreak keeps same-instant rows in insertion order."""
    repository = _ingestion(tmp_path)
    with closing(repository._connect()) as connection, connection:
        for revision_id in ('zzz-first', 'aaa-second'):
            connection.execute(
                'INSERT INTO capture_revision_conflicts('
                'capture_revision_id, detail, recorded_at_utc'
                ') VALUES (?, ?, ?)',
                (revision_id, 'conflict', STAMP),
            )
    assert tuple(
        conflict.capture_revision_id
        for conflict in repository.list_revision_conflicts()
    ) == ('zzz-first', 'aaa-second')


def test_capture_bundles_order_ties_by_digest(tmp_path: Path) -> None:
    repository = _ingestion(tmp_path)
    manifest = b'{}'
    digest_of = sha256(manifest).hexdigest()
    with closing(repository._connect()) as connection, connection:
        for bundle_digest in ('zzz-tie', 'aaa-tie'):
            connection.execute(
                'INSERT INTO capture_bundles('
                'bundle_digest, capture_revision_id, manifest_sha256, '
                'app_name, app_version, app_build, created_at, '
                'finalized_at, manifest_blob'
                ') VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    bundle_digest,
                    f'{bundle_digest}-rev',
                    digest_of,
                    'app',
                    'v',
                    'b',
                    STAMP,
                    STAMP,
                    manifest,
                ),
            )
    assert tuple(
        record.bundle_digest for record in repository.list_capture_bundles()
    ) == ('aaa-tie', 'zzz-tie')


# -- editor payload stores -------------------------------------------------


def test_named_views_order_ties_by_view_id(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    repository.save_named_view('doc-1', 'zzz-tie', {'eye': 1})
    repository.save_named_view('doc-1', 'aaa-tie', {'eye': 2})
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            'UPDATE editor_named_views SET updated_at_utc=? '
            'WHERE document_id=?',
            (STAMP, 'doc-1'),
        )
    assert tuple(
        record.record_id for record in repository.named_views('doc-1')
    ) == ('aaa-tie', 'zzz-tie')


# -- tombstone read-back ----------------------------------------------------


def test_delete_project_tombstone_readback_is_deterministic(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A same-instant earlier tombstone for the project (e.g. a restored
    then re-deleted clone) must not shadow the just-written row: the
    ``tombstone_id DESC`` tiebreak picks the lexically largest id, and the
    fresh tombstone's uuid always outranks a floor id."""
    monkeypatch.setattr(project_lifecycle, '_utc_now', lambda: STAMP)
    database = tmp_path / 'data' / DATABASE_NAME
    repository = SceneRepository(database)
    repository.save(make_empty_scene('doc-a'), parent_revision_id=None)
    library = ProjectLibrary(database)
    record = library.register_project('doc-a', 'Room A')
    floor_tombstone_id = '0' * 64
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            'INSERT INTO htdt_project_tombstones('
            'tombstone_id, project_id, document_id, display_name, '
            'deleted_at_utc, removed_rows, estimated_bytes, '
            'authorities_json'
            ') VALUES (?, ?, ?, ?, ?, 0, 0, ?)',
            (
                floor_tombstone_id,
                record.project_id,
                'doc-a',
                'Room A',
                STAMP,
                '[]',
            ),
        )
    library.archive_project(record.project_id)
    tombstone = library.delete_project(record.project_id)
    assert tombstone.tombstone_id != floor_tombstone_id
    assert tombstone.document_id == 'doc-a'


# -- validate_bundle archive handle ----------------------------------------


def test_validate_bundle_closes_archive_handle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """``validate_bundle`` is the third capture-bundle open path; the other
    two already closed their ``ZipFile`` deterministically. Without the
    close, an open handle outlives the call until GC."""
    bundle_dir = tmp_path / 'bundle'
    support.write_bundle(bundle_dir, support.default_file_specs())
    archive_path = tmp_path / 'bundle.htdtcapture'
    with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as archive:
        for member in sorted(bundle_dir.rglob('*')):
            if member.is_file():
                archive.write(
                    member,
                    member.relative_to(bundle_dir).as_posix(),
                )

    opened: list[zipfile.ZipFile] = []
    closed: list[zipfile.ZipFile] = []
    zipfile_cls = zipfile.ZipFile

    class ProbeZipFile(zipfile_cls):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            opened.append(self)

        def close(self) -> None:
            closed.append(self)
            super().close()

    monkeypatch.setattr(zipfile, 'ZipFile', ProbeZipFile)
    report = capture_bundle.validate_bundle(archive_path)
    assert report['valid'] is True
    assert opened, 'ZipSource never opened the archive'
    assert closed == opened
