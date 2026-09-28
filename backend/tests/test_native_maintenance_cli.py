from __future__ import annotations

from pathlib import Path

import pytest

from htdt.cad_document import WorkingDocument
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, make_f1_scene
from htdt.native_backup import create_backup, validate_backup
from htdt.runtime_instance import SingleInstanceGuard
import htdt.native_cad as native_cad
from htdt.cad_synthetic_demo import SYNTHETIC_DEMO_DOCUMENT_ID


def _seed(data_dir: Path):
    repository = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    first = repository.save(make_f1_scene(), parent_revision_id=None).revision
    return repository, first


def _forbid_qapplication(*_args, **_kwargs):
    raise AssertionError('maintenance CLI must complete before QApplication creation')


def test_backup_cli_runs_before_qapplication(tmp_path: Path, monkeypatch):
    data_dir = tmp_path / 'data'
    _repository, _first = _seed(data_dir)
    archive = tmp_path / 'cli.htdt-backup'
    monkeypatch.setattr(native_cad, 'QApplication', _forbid_qapplication)

    assert native_cad.main([
        '--data-dir', str(data_dir),
        '--backup', str(archive),
    ]) == 0

    assert validate_backup(archive).files[0].path == 'cad-scenes.sqlite3'


def test_restore_cli_runs_before_qapplication_and_restores_revision(tmp_path: Path, monkeypatch):
    data_dir = tmp_path / 'data'
    repository, first = _seed(data_dir)
    archive = tmp_path / 'cli.htdt-backup'
    create_backup(data_dir, archive)

    working = WorkingDocument(
        first.document,
        source_revision_id=first.revision_id,
        saved_content_hash=first.content_hash,
    )
    working.move_entity('speaker-fl', Position3(x_m=1.9, y_m=0.75, z_m=1.05))
    second = repository.save(
        working.committed_document,
        parent_revision_id=first.revision_id,
    ).revision
    assert second.revision_id != first.revision_id

    monkeypatch.setattr(native_cad, 'QApplication', _forbid_qapplication)
    assert native_cad.main([
        '--data-dir', str(data_dir),
        '--restore', str(archive),
    ]) == 0

    restored = SceneRepository(data_dir / 'cad-scenes.sqlite3').latest(first.document_id)
    assert restored is not None
    assert restored.revision_id == first.revision_id


def test_synthetic_demo_cli_runs_before_qapplication(tmp_path: Path, monkeypatch):
    data_dir = tmp_path / 'data'
    monkeypatch.setattr(native_cad, 'QApplication', _forbid_qapplication)

    assert native_cad.main([
        '--data-dir', str(data_dir),
        '--seed-synthetic-demo',
    ]) == 0

    seeded = SceneRepository(data_dir / 'cad-scenes.sqlite3').latest(
        SYNTHETIC_DEMO_DOCUMENT_ID
    )
    assert seeded is not None


def test_backup_cli_rejects_destination_overlapping_live_database(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    data_dir = tmp_path / 'data'
    _repository, _first = _seed(data_dir)
    database = data_dir / 'cad-scenes.sqlite3'
    before = database.read_bytes()

    monkeypatch.setattr(native_cad, 'QApplication', _forbid_qapplication)
    assert native_cad.main([
        '--data-dir', str(data_dir),
        '--backup', str(database),
    ]) == 1

    assert database.read_bytes() == before
    assert 'overlaps the live native database' in capsys.readouterr().err


def test_backup_cli_rejects_destination_inside_measurement_assets(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    data_dir = tmp_path / 'data'
    _repository, _first = _seed(data_dir)
    destination = data_dir / 'measurement-assets' / 'cli.htdt-backup'

    monkeypatch.setattr(native_cad, 'QApplication', _forbid_qapplication)
    assert native_cad.main([
        '--data-dir', str(data_dir),
        '--backup', str(destination),
    ]) == 1

    assert not destination.exists()
    assert 'measurement-assets' in capsys.readouterr().err


def test_native_cli_rejects_data_dir_already_in_use(
    tmp_path: Path,
    capsys,
) -> None:
    data_dir = tmp_path / 'data'
    guard = SingleInstanceGuard(data_dir)
    assert guard.acquire()
    try:
        assert native_cad.main([
            '--data-dir', str(data_dir),
            '--seed-synthetic-demo',
        ]) == 2
    finally:
        guard.release()

    captured = capsys.readouterr()
    assert '使用中' in captured.err
    assert not (data_dir / 'cad-scenes.sqlite3').exists()
