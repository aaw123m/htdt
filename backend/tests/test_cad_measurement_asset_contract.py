"""N60 runtime raw-asset integrity (issue #401).

Every authoritative measurement evidence read must prove that the
dataset's ``source_sha256`` still resolves to a managed
``cad_measurement_assets`` row whose declared relative path is contained
under the managed assets directory and names an existing regular file of
the stored size whose SHA-256 equals the digest — the same core contract
the native backup path enforces. These tests exercise the reusable
``validate_raw_asset*``/``get_evidence_bundle`` API and prove the gates
that consume raw-backed evidence fail closed.
"""
from __future__ import annotations

from contextlib import closing
from hashlib import sha256
from pathlib import Path
import shutil
import sqlite3

import pytest

from htdt.cad_measurement_quality import (
    CadMeasurementQualityEvidence,
    build_measurement_quality_profile,
    build_measurement_quality_report,
)
from htdt.cad_measurement_quality_repository import CadMeasurementQualityRepository
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurements import measurement_record_for_revision, normalize_rew_text
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.managed_assets import ManagedAssetError


def _repositories(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(make_f1_scene(), parent_revision_id=None).revision
    return revision, CadMeasurementRepository(scene_repository)


def _evidence(revision, raw: bytes | None = None):
    if raw is None:
        raw = b'Frequency SPL\n20 70.0\n40 71.5\n80 69.0\n160 68.0\n'
    return normalize_rew_text(
        revision,
        'point-mlp',
        raw,
        filename='fixture.txt',
        evidence_type='measured',
        imported_at='2026-09-19T00:00:00+00:00',
    )


def _update_asset_row(repository: CadMeasurementRepository, digest: str, **updates) -> None:
    assignments = ', '.join(f'{column}=?' for column in updates)
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            f'UPDATE cad_measurement_assets SET {assignments} WHERE sha256=?',
            (*updates.values(), digest),
        )


def test_valid_measurement_evidence_resolves_verified_asset_and_bundle(
    tmp_path: Path,
) -> None:
    revision, repository = _repositories(tmp_path)
    record, dataset, filename, raw = _evidence(revision)
    repository.save(record, dataset, raw_filename=filename, raw_bytes=raw)

    asset = repository.validate_raw_asset(dataset.source_sha256)
    assert asset.sha256 == dataset.source_sha256
    assert asset.filename == filename
    assert asset.relative_path == f'measurement-assets/{dataset.source_sha256}'
    assert asset.size_bytes == len(raw)
    assert asset.path.read_bytes() == raw

    by_dataset = repository.validate_raw_asset_for_dataset(dataset.dataset_id)
    assert by_dataset == asset

    bundle = repository.get_evidence_bundle(record.measurement_id)
    assert bundle.measurement == record
    assert bundle.dataset == dataset
    assert bundle.raw_asset == asset

    # Authoritative dataset reads still resolve the evidence unchanged.
    assert repository.get_dataset(dataset.dataset_id) == dataset
    assert repository.dataset_for_measurement(record.measurement_id) == dataset


def test_windows_separator_relative_path_is_accepted(tmp_path: Path) -> None:
    revision, repository = _repositories(tmp_path)
    record, dataset, filename, raw = _evidence(revision)
    repository.save(record, dataset, raw_filename=filename, raw_bytes=raw)

    _update_asset_row(
        repository,
        dataset.source_sha256,
        relative_path=f'measurement-assets\\{dataset.source_sha256}',
    )

    asset = repository.validate_raw_asset(dataset.source_sha256)
    assert asset.relative_path == f'measurement-assets/{dataset.source_sha256}'
    assert repository.get_dataset(dataset.dataset_id) == dataset


def test_unknown_dataset_and_measurement_fail_closed(tmp_path: Path) -> None:
    revision, repository = _repositories(tmp_path)
    record, dataset, filename, raw = _evidence(revision)
    repository.save(record, dataset, raw_filename=filename, raw_bytes=raw)

    with pytest.raises(KeyError):
        repository.validate_raw_asset_for_dataset('no-such-dataset')
    with pytest.raises(KeyError):
        repository.get_evidence_bundle('no-such-measurement')


def test_deleted_raw_asset_fails_authoritative_resolution(tmp_path: Path) -> None:
    revision, repository = _repositories(tmp_path)
    record, dataset, filename, raw = _evidence(revision)
    repository.save(record, dataset, raw_filename=filename, raw_bytes=raw)

    (repository.assets_dir / dataset.source_sha256).unlink()

    with pytest.raises(ManagedAssetError, match='missing or not a regular file'):
        repository.validate_raw_asset(dataset.source_sha256)
    with pytest.raises(ManagedAssetError, match='missing or not a regular file'):
        repository.validate_raw_asset_for_dataset(dataset.dataset_id)
    with pytest.raises(ManagedAssetError, match='missing or not a regular file'):
        repository.get_evidence_bundle(record.measurement_id)
    with pytest.raises(ManagedAssetError, match='missing or not a regular file'):
        repository.get_dataset(dataset.dataset_id)
    with pytest.raises(ManagedAssetError, match='missing or not a regular file'):
        repository.dataset_for_measurement(record.measurement_id)


def test_corrupted_raw_asset_with_unchanged_row_fails(tmp_path: Path) -> None:
    revision, repository = _repositories(tmp_path)
    record, dataset, filename, raw = _evidence(revision)
    repository.save(record, dataset, raw_filename=filename, raw_bytes=raw)

    path = repository.assets_dir / dataset.source_sha256

    # Same-length corruption reaches the SHA-256 check.
    path.write_bytes(b'X' * len(raw))
    with pytest.raises(ManagedAssetError, match='SHA-256 mismatch'):
        repository.validate_raw_asset_for_dataset(dataset.dataset_id)
    with pytest.raises(ManagedAssetError, match='SHA-256 mismatch'):
        repository.get_dataset(dataset.dataset_id)

    # Different-length corruption fails on the stored size first.
    path.write_bytes(b'truncated')
    with pytest.raises(ManagedAssetError, match='size mismatch'):
        repository.get_dataset(dataset.dataset_id)


def test_missing_asset_row_fails_closed(tmp_path: Path) -> None:
    revision, repository = _repositories(tmp_path)
    record, dataset, filename, raw = _evidence(revision)
    repository.save(record, dataset, raw_filename=filename, raw_bytes=raw)

    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute('PRAGMA foreign_keys=OFF')
        connection.execute(
            'DELETE FROM cad_measurement_assets WHERE sha256=?',
            (dataset.source_sha256,),
        )

    with pytest.raises(ManagedAssetError, match='no cad_measurement_assets row'):
        repository.validate_raw_asset(dataset.source_sha256)
    with pytest.raises(ManagedAssetError, match='no cad_measurement_assets row'):
        repository.get_dataset(dataset.dataset_id)


def test_wrong_asset_size_in_row_fails(tmp_path: Path) -> None:
    revision, repository = _repositories(tmp_path)
    record, dataset, filename, raw = _evidence(revision)
    repository.save(record, dataset, raw_filename=filename, raw_bytes=raw)

    _update_asset_row(repository, dataset.source_sha256, size_bytes=len(raw) + 1)

    with pytest.raises(ManagedAssetError, match='size mismatch'):
        repository.validate_raw_asset_for_dataset(dataset.dataset_id)
    with pytest.raises(ManagedAssetError, match='size mismatch'):
        repository.get_dataset(dataset.dataset_id)


def test_relative_path_traversal_fails_closed(tmp_path: Path) -> None:
    revision, repository = _repositories(tmp_path)
    record, dataset, filename, raw = _evidence(revision)
    repository.save(record, dataset, raw_filename=filename, raw_bytes=raw)

    _update_asset_row(
        repository,
        dataset.source_sha256,
        relative_path='../cad.sqlite3',
    )

    with pytest.raises(ManagedAssetError, match='unsafe managed asset path'):
        repository.validate_raw_asset_for_dataset(dataset.dataset_id)


def test_relative_path_outside_managed_assets_dir_fails(tmp_path: Path) -> None:
    """A contained-but-foreign declared path is not the managed asset."""
    revision, repository = _repositories(tmp_path)
    record, dataset, filename, raw = _evidence(revision)
    repository.save(record, dataset, raw_filename=filename, raw_bytes=raw)

    # A byte-identical copy outside measurement-assets still fails: the
    # declared path must stay inside the managed assets directory and name
    # the digest.
    foreign = repository.path.parent / 'exports' / dataset.source_sha256
    foreign.parent.mkdir()
    shutil.copyfile(repository.assets_dir / dataset.source_sha256, foreign)
    _update_asset_row(
        repository,
        dataset.source_sha256,
        relative_path=f'exports/{dataset.source_sha256}',
    )

    with pytest.raises(
        ManagedAssetError, match='escapes the managed assets directory'
    ):
        repository.validate_raw_asset_for_dataset(dataset.dataset_id)


def test_relative_path_leaf_must_equal_digest(tmp_path: Path) -> None:
    """Identical bytes under a non-content-addressed name fail the contract."""
    revision, repository = _repositories(tmp_path)
    record, dataset, filename, raw = _evidence(revision)
    repository.save(record, dataset, raw_filename=filename, raw_bytes=raw)

    alias = repository.assets_dir / 'renamed-copy'
    shutil.copyfile(repository.assets_dir / dataset.source_sha256, alias)
    _update_asset_row(
        repository,
        dataset.source_sha256,
        relative_path='measurement-assets/renamed-copy',
    )

    with pytest.raises(ManagedAssetError, match='does not match its content address'):
        repository.validate_raw_asset_for_dataset(dataset.dataset_id)


def test_symlinked_asset_path_fails_closed(tmp_path: Path) -> None:
    revision, repository = _repositories(tmp_path)
    record, dataset, filename, raw = _evidence(revision)
    repository.save(record, dataset, raw_filename=filename, raw_bytes=raw)

    target = repository.assets_dir / dataset.source_sha256
    real = repository.assets_dir / 'elsewhere'
    target.rename(real)
    try:
        target.symlink_to(real)
    except OSError:
        real.rename(target)
        pytest.skip('symlinks are not supported on this platform')

    with pytest.raises(ManagedAssetError, match='not a regular file'):
        repository.validate_raw_asset_for_dataset(dataset.dataset_id)


def test_quality_gate_consumes_verified_raw_backed_evidence(tmp_path: Path) -> None:
    revision, repository = _repositories(tmp_path)
    record, dataset, filename, raw = _evidence(revision)
    repository.save(record, dataset, raw_filename=filename, raw_bytes=raw)

    quality_repository = CadMeasurementQualityRepository(repository)
    report = build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=CadMeasurementQualityEvidence(),
        profile=build_measurement_quality_profile(profile_version='integrity-1'),
        report_id='report-asset-contract',
        created_at_utc='2026-09-19T00:05:00+00:00',
    )
    quality_repository.save_report(report)
    assert quality_repository.get_report(report.report_id) == report

    (repository.assets_dir / dataset.source_sha256).unlink()

    with pytest.raises(ManagedAssetError, match='missing or not a regular file'):
        quality_repository.get_report(report.report_id)
    with pytest.raises(ManagedAssetError, match='missing or not a regular file'):
        quality_repository.latest_report(record.measurement_id)
