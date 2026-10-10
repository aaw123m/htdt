"""#992 isolated restore drill — verdict honesty, live-data invariance.

The drill must prove restorability inside a sandbox without ever touching
the live data tree, and every verdict must name exactly what it proved.
"""

from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import sqlite3
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
import htdt.native_backup as native_backup
from htdt.native_backup import BackupCancelledError, create_backup
from htdt.restore_drill import (
    DRILL_RESULTS_FILENAME,
    RestoreDrillError,
    latest_drill_result,
    run_restore_drill,
)


def _seed_data(data_dir: Path):
    repository = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    first = repository.save(make_f1_scene(), parent_revision_id=None).revision
    measurements = CadMeasurementRepository(repository)

    raw = b'owned raw measurement fixture\n'
    digest = sha256(raw).hexdigest()
    relative_path = f'measurement-assets/{digest}'
    asset = data_dir / relative_path
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(raw)
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            '''INSERT INTO cad_measurement_assets(
                sha256, filename, relative_path, size_bytes
            ) VALUES (?, ?, ?, ?)''',
            (digest, 'fixture.txt', relative_path, len(raw)),
        )
    return repository, first, digest, raw


def _live_db_sha(data_dir: Path) -> str:
    return sha256((data_dir / 'cad-scenes.sqlite3').read_bytes()).hexdigest()


def _make_backup(tmp_path: Path):
    data_dir = tmp_path / 'data'
    _seed_data(data_dir)
    backup_path = tmp_path / 'drill.htdt-backup'
    create_backup(data_dir, backup_path)
    return data_dir, backup_path


def _checks(result) -> dict[str, str]:
    return {check.check_id: check.status for check in result.checks}


def test_drill_restorable_backup_passes_every_leg(tmp_path: Path):
    data_dir, backup_path = _make_backup(tmp_path)
    live_sha_before = _live_db_sha(data_dir)
    backup_sha_before = sha256(backup_path.read_bytes()).hexdigest()

    result = run_restore_drill(
        backup_path, tmp_path / 'sandbox', data_dir
    )

    assert result.verdict == 'restorable'
    statuses = _checks(result)
    for check_id in (
        'archive_verified',
        'schema_compatibility',
        'disk_space',
        'isolated_restore',
        'sqlite_integrity',
        'landed_hashes',
        'same_machine_opened',
        'authority_audit',
        'scene_count',
        'live_data_untouched',
        'sandbox_cleanup',
    ):
        assert statuses[check_id] == 'passed', (
            check_id, statuses[check_id],
            [c.detail_ja for c in result.checks],
        )
    assert set(result.claims) == {
        'archive_verified',
        'isolated_restore_succeeded',
        'same_machine_opened',
    }
    assert 'other_pc_migration' in result.non_claims
    assert result.scene_count is not None and result.scene_count >= 1
    assert result.sandbox_cleaned
    # The drill directory itself is gone; nothing but the journal changed.
    assert not Path(result.sandbox_dir).exists()
    assert _live_db_sha(data_dir) == live_sha_before
    assert sha256(backup_path.read_bytes()).hexdigest() == backup_sha_before
    # Journal persisted and round-trips.
    journal = data_dir / DRILL_RESULTS_FILENAME
    assert journal.is_file()
    assert latest_drill_result(data_dir).verdict == 'restorable'


def test_drill_failed_archive_never_claims_restorable(tmp_path: Path):
    data_dir, backup_path = _make_backup(tmp_path)
    # Tamper one member: rewrite the zip with different DB bytes.
    tampered = tmp_path / 'tampered.htdt-backup'
    with ZipFile(backup_path, 'r') as original, ZipFile(
        tampered, 'w', compression=ZIP_DEFLATED
    ) as rewritten:
        for info in original.infolist():
            payload = original.read(info.filename)
            if info.filename == native_backup.DATABASE_NAME:
                payload = b'torn'
            rewritten.writestr(info.filename, payload)

    live_sha_before = _live_db_sha(data_dir)
    result = run_restore_drill(tampered, tmp_path / 'sandbox', data_dir)

    assert result.verdict == 'failed'
    statuses = _checks(result)
    assert statuses['archive_verified'] == 'failed'
    assert statuses['live_data_untouched'] == 'passed'
    assert 'isolated_restore_succeeded' not in result.claims
    assert _live_db_sha(data_dir) == live_sha_before


def test_drill_missing_backup_reports_failure_not_crash(tmp_path: Path):
    data_dir, _ = _make_backup(tmp_path)
    result = run_restore_drill(
        tmp_path / 'absent.htdt-backup', tmp_path / 'sandbox', data_dir
    )
    assert result.verdict == 'failed'
    assert _checks(result)['archive_verified'] == 'failed'


def test_drill_refuses_sandbox_inside_live_root(tmp_path: Path):
    data_dir, backup_path = _make_backup(tmp_path)
    with pytest.raises(RestoreDrillError):
        run_restore_drill(
            backup_path, data_dir / 'inside', data_dir
        )
    with pytest.raises(RestoreDrillError):
        run_restore_drill(
            backup_path, data_dir.parent, data_dir
        )


def test_drill_cancel_propagates_as_cancelled(tmp_path: Path):
    data_dir, backup_path = _make_backup(tmp_path)
    calls = {'n': 0}

    def cancelled() -> bool:
        calls['n'] += 1
        return calls['n'] > 1

    with pytest.raises(BackupCancelledError):
        run_restore_drill(
            backup_path,
            tmp_path / 'sandbox',
            data_dir,
            is_cancelled=cancelled,
        )


def test_drill_never_deletes_other_backups(tmp_path: Path):
    data_dir, backup_path = _make_backup(tmp_path)
    keep = tmp_path / 'keep.htdt-backup'
    create_backup(data_dir, keep)
    run_restore_drill(backup_path, tmp_path / 'sandbox', data_dir)
    assert backup_path.is_file()
    assert keep.is_file()


def test_drill_journal_appends_multiple_results(tmp_path: Path):
    data_dir, backup_path = _make_backup(tmp_path)
    run_restore_drill(backup_path, tmp_path / 's1', data_dir)
    run_restore_drill(
        tmp_path / 'absent.htdt-backup', tmp_path / 's2', data_dir
    )
    lines = (data_dir / DRILL_RESULTS_FILENAME).read_text(
        encoding='utf-8'
    ).splitlines()
    assert len(lines) == 2
    assert latest_drill_result(data_dir).verdict == 'failed'


def test_legacy_schema_archive_drills_as_conditional(tmp_path: Path):
    """A pre-versioning archive must still drill: the migration leg runs in
    the sandbox and the verdict honestly reports the extra condition."""
    data_dir, _backup = _make_backup(tmp_path)

    # Craft a structurally valid archive around a legacy (unversioned) DB.
    legacy_db = tmp_path / 'legacy.sqlite3'
    with sqlite3.connect(legacy_db) as connection:
        connection.execute(
            '''CREATE TABLE scene_revisions (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                revision_id TEXT NOT NULL UNIQUE,
                document_id TEXT NOT NULL,
                parent_revision_id TEXT,
                created_at_utc TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(parent_revision_id)
                    REFERENCES scene_revisions(revision_id)
            )'''
        )
    legacy_bytes = legacy_db.read_bytes()
    entry = {
        'path': native_backup.DATABASE_NAME,
        'kind': 'database',
        'size_bytes': len(legacy_bytes),
        'sha256': sha256(legacy_bytes).hexdigest(),
    }
    payload = {
        'schema_version': native_backup.BACKUP_SCHEMA_VERSION,
        'application_version': '0.0.0-legacy',
        'created_at_utc': '2026-10-01T00:00:00+00:00',
        'files': [entry],
    }
    manifest = native_backup.BackupManifest(
        **payload,
        manifest_sha256=native_backup._manifest_hash(payload),
    )
    legacy_archive = tmp_path / 'legacy.htdt-backup'
    with ZipFile(legacy_archive, 'w', compression=ZIP_DEFLATED) as zipped:
        zipped.writestr(
            native_backup.MANIFEST_NAME,
            native_backup._canonical_json(
                manifest.model_dump(mode='json')
            ).encode('utf-8'),
        )
        zipped.writestr(native_backup.DATABASE_NAME, legacy_bytes)

    result = run_restore_drill(
        legacy_archive, tmp_path / 'sandbox', data_dir
    )

    statuses = _checks(result)
    # Legacy DDL is adoptable: schema check flags the condition, migration
    # proves itself in the sandbox, verdict stays honest — never 'restorable'.
    assert statuses['schema_compatibility'] == 'conditional'
    assert statuses['isolated_restore'] == 'passed'
    assert statuses['schema_migration'] in ('conditional', 'failed')
    if statuses['schema_migration'] == 'conditional':
        assert result.verdict == 'restorable_with_conditions'
    else:
        assert result.verdict == 'failed'
    assert statuses['live_data_untouched'] == 'passed'
