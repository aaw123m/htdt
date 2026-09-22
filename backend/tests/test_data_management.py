from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from zipfile import ZipFile, ZIP_DEFLATED

import pytest

from htdt.cad_document import WorkingDocument
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, make_f1_scene
from htdt.cad_schema import NATIVE_SCHEMA_VERSION
from htdt.data_management import (
    ApplicationDataLifecycle,
    DataLifecycleState,
    DataManagementBackend,
    RestorePreviewStaleError,
)
from htdt.native_backup import (
    BACKUP_SCHEMA_VERSION,
    BackupManifest,
    inspect_backup,
    validate_backup,
    _canonical_json,
    _manifest_hash,
)


def _seed_data(data_dir: Path):
    repository = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    first = repository.save(make_f1_scene(), parent_revision_id=None).revision
    CadMeasurementRepository(repository)

    raw = b'data-management-owned-measurement\n'
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


def _mutate_scene(repository: SceneRepository, source_revision_id: str):
    source = repository.get(source_revision_id)
    assert source is not None
    working = WorkingDocument(
        source.document,
        source_revision_id=source.revision_id,
        saved_content_hash=source.content_hash,
    )
    working.move_entity(
        'speaker-fl',
        Position3(x_m=1.85, y_m=0.75, z_m=1.05),
    )
    return repository.save(
        working.committed_document,
        parent_revision_id=source.revision_id,
    ).revision


def test_data_management_preview_metadata_is_derived_from_validated_manifest(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / 'data'
    _repository, _first, _digest, raw = _seed_data(data_dir)
    backend = DataManagementBackend(data_dir)
    backup_path = tmp_path / 'portable.htdt-backup'

    created = backend.create_backup(backup_path)
    preview = backend.preview_restore(backup_path)

    assert preview.manifest == created.manifest
    assert preview.metadata.backup_path == backup_path
    assert preview.metadata.archive_size_bytes == backup_path.stat().st_size
    assert preview.metadata.application_version == created.manifest.application_version
    assert preview.metadata.backup_schema_version == created.manifest.schema_version
    assert preview.metadata.database_size_bytes > 0
    assert preview.metadata.measurement_asset_count == 1
    assert preview.metadata.measurement_asset_size_bytes == len(raw)
    assert preview.metadata.file_count == 2
    assert preview.metadata.manifest_sha256 == created.manifest.manifest_sha256


def test_restore_revalidates_preview_and_rejects_changed_valid_archive(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / 'data'
    repository, first, _digest, _raw = _seed_data(data_dir)
    backend = DataManagementBackend(data_dir)
    backup_path = tmp_path / 'selected.htdt-backup'

    backend.create_backup(backup_path)
    preview = backend.preview_restore(backup_path)

    second = _mutate_scene(repository, first.revision_id)
    backend.create_backup(backup_path)

    with pytest.raises(RestorePreviewStaleError, match='changed after the restore preview'):
        backend.restore(preview)

    reopened = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    assert reopened.latest(first.document_id).revision_id == second.revision_id


def test_restore_wrapper_uses_native_pre_restore_backup_and_reopens_restored_data(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / 'data'
    repository, first, digest, raw = _seed_data(data_dir)
    backend = DataManagementBackend(data_dir)
    backup_path = tmp_path / 'baseline.htdt-backup'

    backend.create_backup(backup_path)
    preview = backend.preview_restore(backup_path)
    second = _mutate_scene(repository, first.revision_id)
    assert second.revision_id != first.revision_id

    result = backend.restore(preview)

    assert result.manifest == preview.manifest
    assert result.pre_restore_backup is not None
    assert result.pre_restore_backup.is_file()
    validate_backup(result.pre_restore_backup)
    reopened = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    assert reopened.latest(first.document_id).revision_id == first.revision_id
    assert (data_dir / 'measurement-assets' / digest).read_bytes() == raw


def test_data_management_backend_rejects_backup_destination_overlapping_live_data(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / 'data'
    _repository, _first, digest, raw = _seed_data(data_dir)
    backend = DataManagementBackend(data_dir)
    database = data_dir / 'cad-scenes.sqlite3'
    asset = data_dir / 'measurement-assets' / digest
    database_before = database.read_bytes()

    with pytest.raises(ValueError, match='overlaps the live native database'):
        backend.create_backup(database)
    with pytest.raises(ValueError, match='measurement-assets'):
        backend.create_backup(asset)

    assert database.read_bytes() == database_before
    assert asset.read_bytes() == raw


def test_application_data_lifecycle_releases_old_handles_before_restore_and_rebuilds(
) -> None:
    events: list[str] = []
    lifecycle = ApplicationDataLifecycle(
        freeze_mutations=lambda: events.append('freeze'),
        release_data_handles=lambda: events.append('release'),
        reopen_data_handles=lambda: events.append('reopen'),
        thaw_mutations=lambda: events.append('thaw'),
    )

    lifecycle.begin_restore()

    assert lifecycle.state is DataLifecycleState.QUIESCED
    assert lifecycle.generation == 0
    assert events == ['freeze', 'release']

    lifecycle.resume_after_restore_attempt()

    assert lifecycle.state is DataLifecycleState.ACTIVE
    assert lifecycle.generation == 1
    assert events == ['freeze', 'release', 'reopen', 'thaw']


def test_application_data_lifecycle_keeps_old_handles_detached_when_reload_fails(
) -> None:
    events: list[str] = []

    def fail_reopen() -> None:
        events.append('reopen')
        raise RuntimeError('injected reload failure')

    lifecycle = ApplicationDataLifecycle(
        freeze_mutations=lambda: events.append('freeze'),
        release_data_handles=lambda: events.append('release'),
        reopen_data_handles=fail_reopen,
        thaw_mutations=lambda: events.append('thaw'),
    )
    lifecycle.begin_restore()

    with pytest.raises(RuntimeError, match='injected reload failure'):
        lifecycle.resume_after_restore_attempt()

    assert lifecycle.state is DataLifecycleState.RESTART_REQUIRED
    assert lifecycle.restart_required
    assert lifecycle.generation == 0
    assert events == ['freeze', 'release', 'reopen']


def _rewrite_manifest(backup_path: Path, mutate) -> Path:
    """Rewrite an archive with a mutated but hash-valid manifest."""
    source = ZipFile(backup_path, 'r')
    manifest = BackupManifest.model_validate_json(source.read('manifest.json'))
    members = {info.filename: source.read(info) for info in source.infolist()}
    source.close()

    payload = manifest.model_dump(mode='json')
    mutate(payload)
    identity = {
        'schema_version': payload['schema_version'],
        'application_version': payload['application_version'],
        'created_at_utc': payload['created_at_utc'],
        'files': payload['files'],
    }
    if payload.get('build') is not None:
        identity['build'] = {
            key: value for key, value in payload['build'].items() if value is not None
        }
    if payload.get('native_schema_version') is not None:
        identity['native_schema_version'] = payload['native_schema_version']
    payload['manifest_sha256'] = _manifest_hash(identity)
    final = BackupManifest.model_validate(payload)
    members['manifest.json'] = _canonical_json(
        final.model_dump(mode='json')
    ).encode('utf-8')

    rewritten = backup_path.parent / f'{backup_path.stem}-rewritten{backup_path.suffix}'
    with ZipFile(rewritten, 'w', compression=ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return rewritten


def test_metadata_distinguishes_native_db_schema_from_archive_schema(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / 'data'
    _seed_data(data_dir)
    backend = DataManagementBackend(data_dir)
    backup_path = tmp_path / 'schemas.htdt-backup'

    created = backend.create_backup(backup_path)
    preview = backend.preview_restore(backup_path)

    for metadata in (created.metadata, preview.metadata):
        # Archive-format schema and native DB schema are different
        # authorities with different values; conflating them fails here.
        assert metadata.backup_schema_version == BACKUP_SCHEMA_VERSION
        assert metadata.native_schema_version == NATIVE_SCHEMA_VERSION
        assert metadata.native_schema_version != metadata.backup_schema_version
        assert metadata.supported_native_schema_version == NATIVE_SCHEMA_VERSION
        assert metadata.native_schema_compatibility == 'current'
    assert preview.manifest.native_schema_version == NATIVE_SCHEMA_VERSION


def test_older_native_schema_backup_reports_migration_required(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / 'data'
    _seed_data(data_dir)
    older_version = NATIVE_SCHEMA_VERSION - 1
    with closing(sqlite3.connect(data_dir / 'cad-scenes.sqlite3')) as connection, connection:
        connection.execute(
            'UPDATE native_schema_metadata SET schema_version=? WHERE singleton=1',
            (older_version,),
        )
        connection.execute(
            'DELETE FROM native_schema_migrations WHERE schema_version>?',
            (older_version,),
        )

    backend = DataManagementBackend(data_dir)
    backup_path = tmp_path / 'older.htdt-backup'
    created = backend.create_backup(backup_path)
    preview = backend.preview_restore(backup_path)

    assert created.metadata.native_schema_version == older_version
    assert created.metadata.native_schema_compatibility == 'migration_required'
    assert preview.metadata.native_schema_version == older_version
    assert preview.metadata.native_schema_compatibility == 'migration_required'


def test_manifest_native_schema_version_is_verified_against_staged_database(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / 'data'
    _seed_data(data_dir)
    backend = DataManagementBackend(data_dir)
    backup_path = tmp_path / 'forged.htdt-backup'
    backend.create_backup(backup_path)

    forged = _rewrite_manifest(
        backup_path,
        lambda payload: payload.__setitem__(
            'native_schema_version', NATIVE_SCHEMA_VERSION - 1
        ),
    )

    with pytest.raises(ValueError, match='native schema version does not match'):
        backend.preview_restore(forged)


def test_archive_without_native_schema_field_reports_staged_db_version(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / 'data'
    _seed_data(data_dir)
    backend = DataManagementBackend(data_dir)
    backup_path = tmp_path / 'legacy.htdt-backup'
    backend.create_backup(backup_path)

    legacy = _rewrite_manifest(
        backup_path,
        lambda payload: payload.pop('native_schema_version'),
    )

    manifest, staged_version = inspect_backup(legacy)
    assert manifest.native_schema_version is None
    assert staged_version == NATIVE_SCHEMA_VERSION

    preview = backend.preview_restore(legacy)
    assert preview.metadata.native_schema_version == NATIVE_SCHEMA_VERSION
    assert preview.metadata.native_schema_compatibility == 'current'
