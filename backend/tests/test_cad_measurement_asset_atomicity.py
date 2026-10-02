from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import os
from pathlib import Path
import sqlite3
import threading

import pytest

from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurements import normalize_rew_text
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.native_backup import create_backup, validate_backup


RAW = b'Frequency SPL\n20 70.0\n40 71.5\n80 69.0\n'


def _saved_f1(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad-scenes.sqlite3')
    revision = scene_repository.save(make_f1_scene(), parent_revision_id=None).revision
    return scene_repository, revision


def _measurement(revision, raw: bytes = RAW, *, filename: str = 'measurement.txt'):
    return normalize_rew_text(
        revision,
        'point-mlp',
        raw,
        filename=filename,
        evidence_type='measured',
        channel_role='front_left',
        source_speaker_ids=('speaker-fl',),
        routing_evidence='verified',
        imported_at='2026-09-17T09:30:00+00:00',
    )


def _asset_rows(repository: CadMeasurementRepository) -> list[tuple]:
    with closing(sqlite3.connect(repository.path)) as connection:
        return connection.execute(
            'SELECT sha256, relative_path, size_bytes FROM cad_measurement_assets'
        ).fetchall()


def _temp_leftovers(repository: CadMeasurementRepository) -> list[Path]:
    return [
        path
        for path in repository.assets_dir.iterdir()
        if path.name.startswith('.asset-') or path.name.endswith('.tmp')
    ]


def test_interrupted_asset_write_never_leaves_poisoned_final_digest_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene_repository, revision = _saved_f1(tmp_path)
    repository = CadMeasurementRepository(scene_repository)
    record, dataset, filename, source = _measurement(revision)
    digest = sha256(source).hexdigest()
    assert digest == dataset.source_sha256
    target = repository.assets_dir / digest

    real_fsync = os.fsync

    def crashed_fsync(descriptor: int) -> None:
        raise OSError('simulated crash before durable install')

    monkeypatch.setattr(os, 'fsync', crashed_fsync)
    with pytest.raises(OSError, match='simulated crash'):
        repository.save(record, dataset, raw_filename=filename, raw_bytes=source)

    # The final digest path is never left behind as a partial file and no
    # temporary install file leaks.
    assert not target.exists()
    assert _temp_leftovers(repository) == []
    assert repository.get_measurement(record.measurement_id) is None

    # A retry of the same payload must recover instead of reporting a false
    # hash collision on a poisoned path.
    monkeypatch.setattr(os, 'fsync', real_fsync)
    repository.save(record, dataset, raw_filename=filename, raw_bytes=source)

    assert target.read_bytes() == source
    assert repository.get_measurement(record.measurement_id) == record
    assert repository.get_dataset(dataset.dataset_id) == dataset
    assert _temp_leftovers(repository) == []


def test_failed_install_via_replace_never_leaves_poisoned_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene_repository, revision = _saved_f1(tmp_path)
    repository = CadMeasurementRepository(scene_repository)
    record, dataset, filename, source = _measurement(revision)
    digest = dataset.source_sha256
    target = repository.assets_dir / digest

    def crashed_replace(source_path, target_path) -> None:
        raise OSError('simulated crash during atomic install')

    monkeypatch.setattr(os, 'replace', crashed_replace)
    with pytest.raises(OSError, match='simulated crash'):
        repository.save(record, dataset, raw_filename=filename, raw_bytes=source)

    assert not target.exists()
    assert _temp_leftovers(repository) == []


def test_db_failure_after_install_leaves_safe_orphan_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transaction failure must never remove the installed digest path."""
    scene_repository, revision = _saved_f1(tmp_path)
    repository = CadMeasurementRepository(scene_repository)
    record, dataset, filename, source = _measurement(revision)
    digest = dataset.source_sha256

    real_install = repository._asset_store.ensure_installed

    def fail_after_install(digest_value: str, data: bytes) -> None:
        # The install lands, then the write transaction fails around it —
        # the row inserts roll back but the verified file must stay.
        real_install(digest_value, data)
        raise sqlite3.OperationalError('simulated database outage')

    monkeypatch.setattr(
        repository._asset_store, 'ensure_installed', fail_after_install
    )
    with pytest.raises(sqlite3.OperationalError, match='simulated database outage'):
        repository.save(record, dataset, raw_filename=filename, raw_bytes=source)

    # The complete, verified asset stays behind as a safe orphan.
    assert (repository.assets_dir / digest).read_bytes() == source
    assert _temp_leftovers(repository) == []
    monkeypatch.delattr(repository._asset_store, 'ensure_installed')
    assert repository.get_measurement(record.measurement_id) is None


def test_failed_save_on_dedup_hit_never_removes_shared_asset(tmp_path: Path) -> None:
    """When a committed row already references the digest, a later save that
    fails inside the DB transaction must not delete the shared file."""
    scene_repository, revision = _saved_f1(tmp_path)
    repository = CadMeasurementRepository(scene_repository)
    record_a, dataset_a, filename_a, source = _measurement(revision, filename='a.txt')
    repository.save(record_a, dataset_a, raw_filename=filename_a, raw_bytes=source)
    digest = dataset_a.source_sha256

    record_b, dataset_b, filename_b, _ = _measurement(revision, filename='b.txt')
    record_b = record_b.model_copy(update={'measurement_id': record_a.measurement_id})
    dataset_b = dataset_b.model_copy(update={'measurement_id': record_a.measurement_id})
    with pytest.raises(ValueError, match='measurement already exists'):
        repository.save(record_b, dataset_b, raw_filename=filename_b, raw_bytes=source)

    assert (repository.assets_dir / digest).read_bytes() == source
    assert repository.get_measurement(record_a.measurement_id) == record_a
    assert repository.dataset_for_measurement(record_a.measurement_id) is not None


def test_concurrent_same_digest_save_cannot_orphan_committed_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two racing saves of identical content serialize on the write
    exclusion — the loser dedups onto the winner's committed install and
    must not delete the asset that the committed row references."""
    scene_repository, revision = _saved_f1(tmp_path)
    repository = CadMeasurementRepository(scene_repository)
    record, dataset, filename, source = _measurement(revision)
    digest = dataset.source_sha256
    target = repository.assets_dir / digest

    barrier = threading.Barrier(2)
    real_connect = repository._connect

    def gated_connect() -> sqlite3.Connection:
        # Release both workers together just before BEGIN IMMEDIATE so the
        # write-lock race is genuinely concurrent.
        barrier.wait(timeout=30)
        return real_connect()

    monkeypatch.setattr(repository, '_connect', gated_connect)

    results: list[object] = []

    def worker() -> None:
        try:
            repository.save(record, dataset, raw_filename=filename, raw_bytes=source)
            results.append('ok')
        except Exception as exc:  # noqa: BLE001 - collect for assertion
            results.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()
    monkeypatch.delattr(repository, '_connect')

    assert results.count('ok') == 1
    failure = next(result for result in results if result != 'ok')
    assert isinstance(failure, ValueError)
    assert 'already exists' in str(failure)

    # The committed row must never reference a missing asset.
    assert target.read_bytes() == source
    assert repository.get_measurement(record.measurement_id) == record
    assert repository.dataset_for_measurement(record.measurement_id) is not None
    assert _temp_leftovers(repository) == []


def test_concurrent_same_digest_distinct_measurements_share_single_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same-digest saves for different measurements serialize on the write
    exclusion and deduplicate onto one installed asset and one asset row."""
    scene_repository, revision = _saved_f1(tmp_path)
    repository = CadMeasurementRepository(scene_repository)
    record_a, dataset_a, filename_a, source = _measurement(revision, filename='a.txt')
    record_b, dataset_b, filename_b, _ = _measurement(revision, filename='b.txt')
    digest = dataset_a.source_sha256
    target = repository.assets_dir / digest

    barrier = threading.Barrier(2)
    real_connect = repository._connect

    def gated_connect() -> sqlite3.Connection:
        barrier.wait(timeout=30)
        return real_connect()

    monkeypatch.setattr(repository, '_connect', gated_connect)

    results: list[object] = []

    def worker(record, dataset, filename) -> None:
        try:
            repository.save(record, dataset, raw_filename=filename, raw_bytes=source)
            results.append('ok')
        except Exception as exc:  # noqa: BLE001 - collect for assertion
            results.append(exc)

    threads = [
        threading.Thread(target=worker, args=(record_a, dataset_a, filename_a)),
        threading.Thread(target=worker, args=(record_b, dataset_b, filename_b)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()
    monkeypatch.delattr(repository, '_connect')

    assert results.count('ok') == 2
    assert target.read_bytes() == source
    # Persisted relative paths are always POSIX ('/' separators) so a project
    # moved between platforms resolves identically.
    expected_relative = f'measurement-assets/{digest}'
    assert _asset_rows(repository) == [(digest, expected_relative, len(source))]
    assert repository.get_measurement(record_a.measurement_id) == record_a
    assert repository.get_measurement(record_b.measurement_id) == record_b
    assert _temp_leftovers(repository) == []


def test_duplicate_content_stays_deduplicated(tmp_path: Path) -> None:
    scene_repository, revision = _saved_f1(tmp_path)
    repository = CadMeasurementRepository(scene_repository)
    record_a, dataset_a, filename_a, source = _measurement(revision, filename='a.txt')
    record_b, dataset_b, filename_b, _ = _measurement(revision, filename='b.txt')

    repository.save(record_a, dataset_a, raw_filename=filename_a, raw_bytes=source)
    repository.save(record_b, dataset_b, raw_filename=filename_b, raw_bytes=source)

    digest = dataset_a.source_sha256
    assert (repository.assets_dir / digest).read_bytes() == source
    rows = _asset_rows(repository)
    assert [row[0] for row in rows] == [digest]
    assert repository.get_measurement(record_b.measurement_id) == record_b


def test_mismatched_file_at_digest_path_is_still_rejected(tmp_path: Path) -> None:
    """A corrupt or foreign file occupying the digest path is reported as a
    collision rather than silently trusted or overwritten."""
    scene_repository, revision = _saved_f1(tmp_path)
    repository = CadMeasurementRepository(scene_repository)
    record, dataset, filename, source = _measurement(revision)
    target = repository.assets_dir / dataset.source_sha256
    target.write_bytes(b'partial foreign payload')

    with pytest.raises(ValueError, match='hash collision'):
        repository.save(record, dataset, raw_filename=filename, raw_bytes=source)

    assert repository.get_measurement(record.measurement_id) is None


def test_saved_measurement_asset_passes_backup_validation(tmp_path: Path) -> None:
    data_dir = tmp_path / 'data'
    scene_repository = SceneRepository(data_dir / 'cad-scenes.sqlite3')
    revision = scene_repository.save(make_f1_scene(), parent_revision_id=None).revision
    repository = CadMeasurementRepository(scene_repository)
    record, dataset, filename, source = _measurement(revision)
    repository.save(record, dataset, raw_filename=filename, raw_bytes=source)

    backup_path = tmp_path / 'measurements.htdt-backup'
    manifest = create_backup(data_dir, backup_path)

    assert validate_backup(backup_path) == manifest
    digest = dataset.source_sha256
    assert f'measurement-assets/{digest}' in {entry.path for entry in manifest.files}
    assert (data_dir / 'measurement-assets' / digest).read_bytes() == source


def test_orphaned_asset_from_failed_save_is_reused_by_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An asset orphaned by a failed transaction is treated as an
    already-installed identical digest on the next save."""
    scene_repository, revision = _saved_f1(tmp_path)
    repository = CadMeasurementRepository(scene_repository)
    record, dataset, filename, source = _measurement(revision)
    digest = dataset.source_sha256

    real_install = repository._asset_store.ensure_installed

    def fail_after_install(digest_value: str, data: bytes) -> None:
        real_install(digest_value, data)
        raise sqlite3.OperationalError('simulated database outage')

    monkeypatch.setattr(
        repository._asset_store, 'ensure_installed', fail_after_install
    )
    with pytest.raises(sqlite3.OperationalError):
        repository.save(record, dataset, raw_filename=filename, raw_bytes=source)
    monkeypatch.delattr(repository._asset_store, 'ensure_installed')

    repository.save(record, dataset, raw_filename=filename, raw_bytes=source)

    assert (repository.assets_dir / digest).read_bytes() == source
    assert repository.get_measurement(record.measurement_id) == record
    assert [row[0] for row in _asset_rows(repository)] == [digest]
