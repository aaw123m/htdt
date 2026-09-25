"""Backend coverage for #501 storage & evidence maintenance.

The service distinguishes referenced evidence from unreferenced orphan
candidates, reports missing referenced assets as integrity failures (never
cleanup candidates), dry-runs first, and re-validates reachability inside
the cleanup transaction before deleting anything.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

import pytest

from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurements import normalize_rew_text
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
import htdt.storage_maintenance as storage_maintenance
from htdt.storage_maintenance import (
    MANAGED_ASSETS_DIRNAME,
    plan_storage_gc,
    run_storage_gc,
    scan_storage,
)


def _scene(document_id: str) -> SceneDocument:
    return SceneDocument(
        document_id=document_id,
        schema_version=2,
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='FL',
                position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
                size_m=Size3(x_m=0.22, y_m=0.28, z_m=0.42),
                speaker_role='FL',
            ),
            SceneEntity(
                entity_id='point-mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=3.0, y_m=3.0, z_m=1.1),
            ),
        ),
    )


def _seed_measurement(tmp_path: Path, *, orphan_raw: bytes | None = None):
    """One referenced measurement plus, optionally, a failed-import orphan
    (file + registry row that nothing references)."""

    data_dir = tmp_path / 'data'
    repository = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    head = repository.save(_scene('doc-1'), parent_revision_id=None).revision
    measurements = CadMeasurementRepository(repository)
    raw = b'Frequency SPL\n20 70.0\n40 71.5\n80 69.0\n'
    record, dataset, filename, source = normalize_rew_text(
        head,
        'point-mlp',
        raw,
        filename='mlp-fl.txt',
        evidence_type='measured',
        channel_role='front_left',
        source_speaker_ids=('speaker-fl',),
        routing_evidence='verified',
        imported_at='2026-09-20T09:30:00+00:00',
    )
    measurements.save(record, dataset, raw_filename=filename, raw_bytes=source)
    referenced_digest = dataset.source_sha256

    orphan_digest = None
    if orphan_raw is not None:
        orphan_digest = hashlib.sha256(orphan_raw).hexdigest()
        assets_root = data_dir / MANAGED_ASSETS_DIRNAME
        assets_root.mkdir(parents=True, exist_ok=True)
        (assets_root / orphan_digest).write_bytes(orphan_raw)
        # A failed transaction's leftover: registry row present, no referent.
        connection = sqlite3.connect(data_dir / 'cad-scenes.sqlite3')
        connection.execute(
            'INSERT INTO cad_measurement_assets('
            'sha256, filename, relative_path, size_bytes'
            ') VALUES (?, ?, ?, ?)',
            (
                orphan_digest,
                'failed-import.txt',
                f'{MANAGED_ASSETS_DIRNAME}/{orphan_digest}',
                len(orphan_raw),
            ),
        )
        connection.commit()
        connection.close()
    return repository, data_dir, referenced_digest, orphan_digest


def test_inventory_distinguishes_referenced_from_orphan(tmp_path):
    _repo, data_dir, referenced, orphan = _seed_measurement(
        tmp_path, orphan_raw=b'orphaned-failed-import-bytes'
    )

    report = scan_storage(data_dir)

    assert report.database_bytes > 0
    assert [f.digest for f in report.referenced_files] == [referenced]
    assert [f.digest for f in report.orphan_candidates] == [orphan]
    assert report.reclaimable_bytes > 0
    assert report.missing_referenced == ()

    assets = next(
        c for c in report.categories if c.category == 'managed-assets'
    )
    assert assets.unreferenced_bytes == len(
        b'orphaned-failed-import-bytes'
    )
    # Physical bytes count each digest file once — not per reference.
    assert assets.physical_unique_bytes == (
        len(b'orphaned-failed-import-bytes')
        + report.referenced_files[0].size_bytes
    )


def test_missing_referenced_asset_is_integrity_failure_not_candidate(
    tmp_path,
):
    _repo, data_dir, referenced, _orphan = _seed_measurement(tmp_path)
    asset = data_dir / MANAGED_ASSETS_DIRNAME / referenced
    asset.unlink()

    report = scan_storage(data_dir)

    assert [m.digest for m in report.missing_referenced] == [referenced]
    assert report.orphan_candidates == ()
    assert report.referenced_files == ()


def test_temporary_and_unmanaged_files_are_not_gc_candidates(tmp_path):
    _repo, data_dir, _referenced, _orphan = _seed_measurement(tmp_path)
    assets_root = data_dir / MANAGED_ASSETS_DIRNAME
    (assets_root / '.asset-zzz.tmp').write_bytes(b'partial')
    (assets_root / 'notes.txt').write_bytes(b'not an asset')

    report = scan_storage(data_dir)

    assert [f.classification for f in report.temporary_files] == [
        'temporary'
    ]
    assert [f.classification for f in report.unmanaged_files] == [
        'unmanaged'
    ]
    assert report.orphan_candidates == ()


def test_gc_deletes_orphan_and_registry_row_never_referenced(tmp_path):
    _repo, data_dir, referenced, orphan = _seed_measurement(
        tmp_path, orphan_raw=b'orphaned-failed-import-bytes'
    )

    plan = plan_storage_gc(data_dir)
    assert [f.digest for f in plan.orphan_candidates] == [orphan]

    result = run_storage_gc(data_dir)

    assert result.deleted_files == 1
    assert result.deleted_registry_rows == 1
    assert result.freed_bytes == len(b'orphaned-failed-import-bytes')
    assets_root = data_dir / MANAGED_ASSETS_DIRNAME
    assert not (assets_root / orphan).exists()
    # Referenced asset and its authority rows are untouched.
    assert (assets_root / referenced).is_file()
    connection = sqlite3.connect(data_dir / 'cad-scenes.sqlite3')
    rows = connection.execute(
        'SELECT sha256 FROM cad_measurement_assets'
    ).fetchall()
    measurements = connection.execute(
        'SELECT COUNT(*) FROM cad_measurements'
    ).fetchone()[0]
    connection.close()
    assert [r[0] for r in rows] == [referenced]
    assert measurements == 1


def test_gc_skips_candidate_that_became_referenced(
    tmp_path, monkeypatch
):
    """Reachability re-validated at delete time: a digest the dry-run scan
    saw as orphan but which is referenced by cleanup time is skipped, not
    race-deleted (#501 contract)."""

    _repo, data_dir, _referenced, orphan = _seed_measurement(
        tmp_path, orphan_raw=b'orphaned-failed-import-bytes'
    )
    assert orphan is not None
    calls = {'n': 0}
    real = storage_maintenance.referenced_asset_digests

    def _changing(connection):
        calls['n'] += 1
        digests = real(connection)
        if calls['n'] > 1:
            digests.add(orphan)  # became reachable between scan and delete
        return digests

    monkeypatch.setattr(
        storage_maintenance, 'referenced_asset_digests', _changing
    )

    result = run_storage_gc(data_dir)

    assert result.deleted_files == 0
    assert result.skipped_digests == (orphan,)
    assert (
        data_dir / MANAGED_ASSETS_DIRNAME / orphan
    ).is_file()


def test_referenced_orphan_registry_row_is_not_a_candidate(tmp_path):
    """A registry row whose digest is referenced by an authority is
    retained as long as the last reference exists."""
    _repo, data_dir, _referenced, orphan = _seed_measurement(
        tmp_path, orphan_raw=b'orphaned-failed-import-bytes'
    )
    assert orphan is not None
    connection = sqlite3.connect(data_dir / 'cad-scenes.sqlite3')
    connection.row_factory = sqlite3.Row
    revision = connection.execute(
        'SELECT revision_id FROM scene_revisions'
    ).fetchone()
    connection.execute(
        '''INSERT INTO cad_measurements(
            measurement_id, document_id, scene_revision_id, scene_content_hash,
            measurement_entity_id, measurement_position_json,
            measurement_direction_json, evidence_type, channel_role,
            source_speaker_ids_json, radiation_scope, routing_evidence,
            captured_at, imported_at, source_kind, external_source_id,
            quality_status, quality_reasons_json, quality_source,
            provenance_json
        ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)''',
        (
            'm-orphan',
            'doc-1',
            revision['revision_id'],
            'h',
            'point-mlp',
            '{}',
            'measured',
            'front_left',
            '[]',
            'point',
            'verified',
            '2026-09-20T00:00:00+00:00',
            'rew',
            None,
            'unverified',
            '[]',
            'import',
            f'{{"source_sha256": "{orphan}"}}',
        ),
    )
    connection.commit()
    connection.close()

    report = scan_storage(data_dir)

    assert report.orphan_candidates == ()
    assert [f.digest for f in report.referenced_files] == [
        _referenced,
        orphan,
    ]


def test_gc_without_database_fails_closed(tmp_path):
    with pytest.raises(Exception):
        run_storage_gc(tmp_path / 'missing')


def test_gc_failed_unlink_stays_retryable_via_pending_ledger(
    tmp_path, monkeypatch
):
    """A crash/failure after the registry-row commit must not strand the
    file as unmanaged: the pending ledger keeps it GC-eligible (#501)."""

    _repo, data_dir, referenced, orphan = _seed_measurement(
        tmp_path, orphan_raw=b'orphaned-failed-import-bytes'
    )
    assets_root = data_dir / MANAGED_ASSETS_DIRNAME
    db_path = data_dir / 'cad-scenes.sqlite3'

    real_unlink = Path.unlink

    def _flaky(self, *args, **kwargs):
        if self.name == orphan:
            raise OSError('simulated post-commit unlink failure')
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'unlink', _flaky)

    result = run_storage_gc(data_dir)

    # Registry row + pending row committed together; the file survived.
    assert result.deleted_files == 0
    assert orphan in result.skipped_digests
    assert (assets_root / orphan).is_file()
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            'SELECT sha256 FROM cad_measurement_assets'
        ).fetchall()
        pending = connection.execute(
            'SELECT sha256 FROM htdt_storage_gc_pending'
        ).fetchall()
    finally:
        connection.close()
    assert [r[0] for r in rows] == [referenced]
    assert [r[0] for r in pending] == [orphan]

    # The interrupted delete is still a candidate — never 'unmanaged'.
    report = scan_storage(data_dir)
    assert [f.digest for f in report.orphan_candidates] == [orphan]
    assert report.unmanaged_files == ()

    # A healthy retry finishes the unlink and clears the ledger row.
    monkeypatch.setattr(Path, 'unlink', real_unlink)
    retry = run_storage_gc(data_dir)
    assert retry.deleted_files == 1
    assert retry.deleted_registry_rows == 0
    assert not (assets_root / orphan).exists()
    connection = sqlite3.connect(db_path)
    try:
        pending = connection.execute(
            'SELECT COUNT(*) FROM htdt_storage_gc_pending'
        ).fetchone()[0]
    finally:
        connection.close()
    assert pending == 0


def test_gc_pending_row_cleared_when_file_vanished(tmp_path):
    """A pending row whose file is already gone is swept, not retried."""

    _repo, data_dir, _referenced, orphan = _seed_measurement(
        tmp_path, orphan_raw=b'orphaned-failed-import-bytes'
    )
    db_path = data_dir / 'cad-scenes.sqlite3'
    connection = sqlite3.connect(db_path)
    connection.execute(
        'DELETE FROM cad_measurement_assets WHERE sha256=?', (orphan,)
    )
    connection.execute(
        'INSERT INTO htdt_storage_gc_pending('
        'sha256, size_bytes, queued_at_utc'
        ') VALUES (?, ?, ?)',
        (orphan, len(b'orphaned-failed-import-bytes'), '2026-01-01T00:00:00Z'),
    )
    connection.commit()
    connection.close()
    (data_dir / MANAGED_ASSETS_DIRNAME / orphan).unlink()

    run_storage_gc(data_dir)

    connection = sqlite3.connect(db_path)
    try:
        pending = connection.execute(
            'SELECT COUNT(*) FROM htdt_storage_gc_pending'
        ).fetchone()[0]
    finally:
        connection.close()
    assert pending == 0


def test_pending_delete_cancelled_when_digest_referenced_again(tmp_path):
    """If a pending digest becomes reachable again, GC clears the row and
    keeps the file rather than deleting referenced evidence."""

    _repo, data_dir, _referenced, orphan = _seed_measurement(
        tmp_path, orphan_raw=b'orphaned-failed-import-bytes'
    )
    db_path = data_dir / 'cad-scenes.sqlite3'
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    revision = connection.execute(
        'SELECT revision_id FROM scene_revisions'
    ).fetchone()
    connection.execute(
        'DELETE FROM cad_measurement_assets WHERE sha256=?', (orphan,)
    )
    connection.execute(
        'INSERT INTO htdt_storage_gc_pending('
        'sha256, size_bytes, queued_at_utc'
        ') VALUES (?, ?, ?)',
        (orphan, len(b'orphaned-failed-import-bytes'), '2026-01-01T00:00:00Z'),
    )
    connection.execute(
        '''INSERT INTO cad_measurements(
            measurement_id, document_id, scene_revision_id, scene_content_hash,
            measurement_entity_id, measurement_position_json,
            measurement_direction_json, evidence_type, channel_role,
            source_speaker_ids_json, radiation_scope, routing_evidence,
            captured_at, imported_at, source_kind, external_source_id,
            quality_status, quality_reasons_json, quality_source,
            provenance_json
        ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)''',
        (
            'm-reattached',
            'doc-1',
            revision['revision_id'],
            'h',
            'point-mlp',
            '{}',
            'measured',
            'front_left',
            '[]',
            'point',
            'verified',
            '2026-09-20T00:00:00+00:00',
            'rew',
            None,
            'unverified',
            '[]',
            'import',
            f'{{"source_sha256": "{orphan}"}}',
        ),
    )
    connection.commit()
    connection.close()

    run_storage_gc(data_dir)

    assert (data_dir / MANAGED_ASSETS_DIRNAME / orphan).is_file()
    connection = sqlite3.connect(db_path)
    try:
        pending = connection.execute(
            'SELECT COUNT(*) FROM htdt_storage_gc_pending'
        ).fetchone()[0]
    finally:
        connection.close()
    assert pending == 0


def test_shared_asset_retained_until_last_reference_gone(tmp_path):
    _repo, data_dir, referenced, _o = _seed_measurement(tmp_path)
    # Second referent in another table's payload keeps the asset rooted.
    connection = sqlite3.connect(data_dir / 'cad-scenes.sqlite3')
    connection.row_factory = sqlite3.Row
    revision = connection.execute(
        'SELECT revision_id FROM scene_revisions'
    ).fetchone()
    connection.execute(
        '''INSERT INTO cad_measurements(
            measurement_id, document_id, scene_revision_id, scene_content_hash,
            measurement_entity_id, measurement_position_json,
            measurement_direction_json, evidence_type, channel_role,
            source_speaker_ids_json, radiation_scope, routing_evidence,
            captured_at, imported_at, source_kind, external_source_id,
            quality_status, quality_reasons_json, quality_source,
            provenance_json
        ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)''',
        (
            'm-shared',
            'doc-1',
            revision['revision_id'],
            'h',
            'point-mlp',
            '{}',
            'measured',
            'front_left',
            '[]',
            'point',
            'verified',
            '2026-09-20T00:00:00+00:00',
            'rew',
            None,
            'unverified',
            '[]',
            'import',
            f'{{"source_sha256": "{referenced}"}}',
        ),
    )
    connection.commit()
    connection.close()

    report = scan_storage(data_dir)
    assert report.orphan_candidates == ()
    assert (data_dir / MANAGED_ASSETS_DIRNAME / referenced).is_file()
