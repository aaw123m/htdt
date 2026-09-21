from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import os
from pathlib import Path
import sqlite3
import subprocess
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from htdt.cad_document import WorkingDocument
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, make_f1_scene
import htdt.native_backup as native_backup
from htdt.native_backup import create_backup, restore_backup, validate_backup


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
        Position3(x_m=1.75, y_m=0.75, z_m=1.05),
    )
    return repository.save(
        working.committed_document,
        parent_revision_id=source.revision_id,
    ).revision


def _rewrite_zip(source: Path, destination: Path, replacements: dict[str, bytes]) -> None:
    with ZipFile(source, 'r') as original, ZipFile(
        destination,
        'w',
        compression=ZIP_DEFLATED,
    ) as rewritten:
        for info in original.infolist():
            payload = replacements.get(info.filename, original.read(info.filename))
            rewritten.writestr(info.filename, payload)


def test_native_backup_round_trip_restores_database_and_content_addressed_assets(tmp_path: Path):
    data_dir = tmp_path / 'data'
    repository, first, digest, raw = _seed_data(data_dir)
    backup_path = tmp_path / 'baseline.htdt-backup'

    manifest = create_backup(data_dir, backup_path)
    validated = validate_backup(backup_path)

    assert validated == manifest
    assert {entry.path for entry in manifest.files} == {
        'cad-scenes.sqlite3',
        f'measurement-assets/{digest}',
    }

    second = _mutate_scene(repository, first.revision_id)
    assert second.revision_id != first.revision_id
    assert SceneRepository(repository.path).latest(first.document_id).revision_id == second.revision_id

    restored, pre_restore = restore_backup(data_dir, backup_path)

    assert restored == manifest
    assert pre_restore is not None and pre_restore.is_file()
    validate_backup(pre_restore)
    reopened = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    assert reopened.latest(first.document_id).revision_id == first.revision_id
    assert (data_dir / 'measurement-assets' / digest).read_bytes() == raw


def test_backup_validation_rejects_asset_content_tampering(tmp_path: Path):
    data_dir = tmp_path / 'data'
    _repository, _first, digest, _raw = _seed_data(data_dir)
    valid = tmp_path / 'valid.htdt-backup'
    corrupt = tmp_path / 'corrupt.htdt-backup'
    create_backup(data_dir, valid)

    _rewrite_zip(
        valid,
        corrupt,
        {f'measurement-assets/{digest}': b'tampered payload'},
    )

    with pytest.raises(ValueError, match='size mismatch|SHA-256 mismatch'):
        validate_backup(corrupt)


def test_backup_validation_rejects_path_traversal_member(tmp_path: Path):
    data_dir = tmp_path / 'data'
    _seed_data(data_dir)
    valid = tmp_path / 'valid.htdt-backup'
    malicious = tmp_path / 'malicious.htdt-backup'
    create_backup(data_dir, valid)

    with ZipFile(valid, 'r') as original, ZipFile(
        malicious,
        'w',
        compression=ZIP_DEFLATED,
    ) as rewritten:
        for info in original.infolist():
            rewritten.writestr(info.filename, original.read(info.filename))
        rewritten.writestr('../escape.txt', b'escape')

    with pytest.raises(ValueError, match='unsafe backup archive path'):
        validate_backup(malicious)
    assert not (tmp_path / 'escape.txt').exists()


def test_failed_restore_leaves_current_native_data_unchanged(tmp_path: Path):
    data_dir = tmp_path / 'data'
    repository, first, digest, _raw = _seed_data(data_dir)
    baseline = tmp_path / 'baseline.htdt-backup'
    corrupt = tmp_path / 'corrupt.htdt-backup'
    create_backup(data_dir, baseline)

    second = _mutate_scene(repository, first.revision_id)
    _rewrite_zip(
        baseline,
        corrupt,
        {f'measurement-assets/{digest}': b'corrupt'},
    )

    with pytest.raises(ValueError):
        restore_backup(data_dir, corrupt)

    reopened = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    assert reopened.latest(first.document_id).revision_id == second.revision_id


def test_backup_normalizes_existing_windows_asset_relative_paths(tmp_path: Path):
    data_dir = tmp_path / 'data'
    repository = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)
    CadMeasurementRepository(repository)

    raw = b'windows-separator-fixture'
    digest = sha256(raw).hexdigest()
    asset = data_dir / 'measurement-assets' / digest
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(raw)
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            '''INSERT INTO cad_measurement_assets(
                sha256, filename, relative_path, size_bytes
            ) VALUES (?, ?, ?, ?)''',
            (
                digest,
                'fixture.txt',
                f'measurement-assets\\{digest}',
                len(raw),
            ),
        )

    archive = tmp_path / 'windows-path.htdt-backup'
    manifest = create_backup(data_dir, archive)

    assert f'measurement-assets/{digest}' in {entry.path for entry in manifest.files}
    validate_backup(archive)


def test_restore_swap_failure_rolls_back_original_database_and_assets(tmp_path: Path, monkeypatch):
    data_dir = tmp_path / 'data'
    repository, first, digest, raw = _seed_data(data_dir)
    baseline = tmp_path / 'baseline.htdt-backup'
    create_backup(data_dir, baseline)

    second = _mutate_scene(repository, first.revision_id)
    original_replace = native_backup.os.replace
    failed = False

    def fail_staged_asset_swap(source, destination):
        nonlocal failed
        source_path = Path(source)
        if (
            not failed
            and source_path.name == 'measurement-assets'
            and 'htdt-restore-stage-' in str(source_path.parent)
        ):
            failed = True
            raise OSError('injected staged asset swap failure')
        return original_replace(source, destination)

    monkeypatch.setattr(native_backup.os, 'replace', fail_staged_asset_swap)

    with pytest.raises(OSError, match='injected staged asset swap failure'):
        restore_backup(data_dir, baseline)

    reopened = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    assert reopened.latest(first.document_id).revision_id == second.revision_id
    assert (data_dir / 'measurement-assets' / digest).read_bytes() == raw


def test_backup_validation_rejects_excessive_expanded_size(
    tmp_path: Path,
    monkeypatch,
) -> None:
    data_dir = tmp_path / 'data'
    _seed_data(data_dir)
    archive = tmp_path / 'bounded.htdt-backup'
    create_backup(data_dir, archive)

    monkeypatch.setattr(native_backup, 'MAX_NATIVE_BACKUP_EXPANDED_BYTES', 1)

    with pytest.raises(ValueError, match='expanded size exceeds limit'):
        validate_backup(archive)


def test_backup_rejects_destination_equal_to_live_database(tmp_path: Path):
    data_dir = tmp_path / 'data'
    _repository, _first, _digest, _raw = _seed_data(data_dir)
    database = data_dir / 'cad-scenes.sqlite3'
    before = database.read_bytes()
    names_before = {entry.name for entry in data_dir.iterdir()}

    with pytest.raises(ValueError, match='overlaps the live native database'):
        create_backup(data_dir, database)

    assert database.read_bytes() == before
    assert {entry.name for entry in data_dir.iterdir()} == names_before


def test_backup_rejects_destination_aliasing_live_database_via_dotdot(tmp_path: Path):
    data_dir = tmp_path / 'data'
    _repository, _first, _digest, _raw = _seed_data(data_dir)
    database = data_dir / 'cad-scenes.sqlite3'
    before = database.read_bytes()

    alias = data_dir / 'exports' / '..' / 'cad-scenes.sqlite3'
    with pytest.raises(ValueError, match='overlaps the live native database'):
        create_backup(data_dir, alias)

    assert database.read_bytes() == before
    assert not (data_dir / 'exports').exists()


def test_backup_rejects_destination_aliasing_database_through_symlinked_directory(
    tmp_path: Path,
):
    data_dir = tmp_path / 'data'
    _repository, _first, _digest, _raw = _seed_data(data_dir)
    link = tmp_path / 'data-link'
    try:
        link.symlink_to(data_dir, target_is_directory=True)
    except OSError:
        pytest.skip('directory symlinks are not supported on this platform')
    database = data_dir / 'cad-scenes.sqlite3'
    before = database.read_bytes()

    with pytest.raises(ValueError, match='overlaps the live native database'):
        create_backup(data_dir, link / 'cad-scenes.sqlite3')

    assert database.read_bytes() == before


def test_backup_rejects_destination_aliasing_assets_through_windows_junction(
    tmp_path: Path,
):
    if os.name != 'nt':
        pytest.skip('junction aliasing is Windows-specific')

    data_dir = tmp_path / 'data'
    _repository, _first, digest, raw = _seed_data(data_dir)
    junction = tmp_path / 'assets-junction'
    result = subprocess.run(
        ['cmd.exe', '/c', 'mklink', '/J', str(junction), str(data_dir)],
        capture_output=True,
    )
    if result.returncode != 0 or not junction.exists():
        pytest.skip('directory junctions are not supported in this environment')
    asset = data_dir / 'measurement-assets' / digest

    with pytest.raises(ValueError, match='measurement-assets'):
        create_backup(data_dir, junction / 'measurement-assets' / digest)

    assert asset.read_bytes() == raw


def test_backup_rejects_destination_overlapping_managed_measurement_asset(
    tmp_path: Path,
):
    data_dir = tmp_path / 'data'
    _repository, _first, digest, raw = _seed_data(data_dir)
    asset = data_dir / 'measurement-assets' / digest

    with pytest.raises(ValueError, match='measurement-assets'):
        create_backup(data_dir, asset)

    assert asset.read_bytes() == raw


def test_backup_rejects_new_destination_inside_measurement_assets_subtree(
    tmp_path: Path,
):
    data_dir = tmp_path / 'data'
    _repository, _first, _digest, _raw = _seed_data(data_dir)
    nested = data_dir / 'measurement-assets' / 'exports' / 'nested.htdt-backup'

    with pytest.raises(ValueError, match='measurement-assets'):
        create_backup(data_dir, nested)

    assert not (data_dir / 'measurement-assets' / 'exports').exists()


def test_backup_rejects_hardlink_alias_of_managed_measurement_asset(tmp_path: Path):
    data_dir = tmp_path / 'data'
    _repository, _first, digest, raw = _seed_data(data_dir)
    asset = data_dir / 'measurement-assets' / digest
    alias = tmp_path / f'{digest}.htdt-backup'
    try:
        os.link(asset, alias)
    except OSError:
        pytest.skip('hard links are not supported on this platform')

    with pytest.raises(ValueError, match='managed measurement asset'):
        create_backup(data_dir, alias)

    assert asset.read_bytes() == raw
    assert alias.read_bytes() == raw


def test_backup_allows_destination_inside_data_dir_outside_managed_data(
    tmp_path: Path,
):
    data_dir = tmp_path / 'data'
    _repository, _first, digest, raw = _seed_data(data_dir)
    archive = data_dir / 'exports' / 'nested.htdt-backup'

    manifest = create_backup(data_dir, archive)

    assert validate_backup(archive) == manifest
    assert (data_dir / 'measurement-assets' / digest).read_bytes() == raw


def test_backup_rejects_destination_that_is_an_existing_directory(tmp_path: Path):
    data_dir = tmp_path / 'data'
    _repository, _first, _digest, _raw = _seed_data(data_dir)
    directory = tmp_path / 'existing-dir'
    directory.mkdir()

    with pytest.raises(ValueError, match='is a directory'):
        create_backup(data_dir, directory)

    assert not list(directory.iterdir())
