"""Persisted frequency-response evidence integrity (issue #329).

These tests prove the FR persistence boundary no longer trusts a dataset
that merely pairs valid arrays with a matching raw-asset SHA: every save
reruns the exact declared importer transformation over the retained raw
bytes, every authoritative read re-verifies the persisted dataset semantic
hash, the versioned transformation seal, the content-addressed raw asset
and the full importer replay.
"""
from __future__ import annotations

import base64
from contextlib import closing
from datetime import timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import struct

import pytest

from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurement_quality import (
    CadMeasurementQualityEvidence,
    build_measurement_quality_profile,
    build_measurement_quality_report,
)
from htdt.cad_measurement_quality_repository import CadMeasurementQualityRepository
from htdt.cad_measurement_repository import CadMeasurementRepository, _pack
from htdt.cad_measurements import (
    HTDT_DECLARED_IMPORTER_VERSION,
    canonical_json,
    declared_fr_raw,
    import_transformation_sha256,
    measurement_record_for_revision,
    normalize_rew_api_snapshot,
    normalize_rew_text,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.rew_api import RewFrequencyResponseSnapshot, decode_frequency_response


def _repositories(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(make_f1_scene(), parent_revision_id=None).revision
    return revision, CadMeasurementRepository(scene_repository)


def _rew_text_evidence(revision, raw: bytes | None = None):
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


def _rew_api_evidence(revision, *, phase: tuple[float, ...] | None = None):
    fr_payload: dict[str, object] = {
        'unit': 'SPL',
        'startFreq': 20.0,
        'freqStep': 20.0,
        'magnitude': base64.b64encode(
            struct.pack('>4f', 70.0, 71.0, 69.0, 68.0)
        ).decode('ascii'),
    }
    if phase is not None:
        fr_payload['phase'] = base64.b64encode(
            struct.pack(f'>{len(phase)}f', *phase)
        ).decode('ascii')
    decoded = decode_frequency_response(
        'rew-uuid-integrity', fr_payload, requested_unit='SPL'
    )
    snapshot = RewFrequencyResponseSnapshot(
        measurement_summary={
            'uuid': 'rew-uuid-integrity',
            'date': '2026-09-17T18:00:00+09:00',
        },
        query={'unit': 'SPL'},
        raw_frequency_response=fr_payload,
        decoded=decoded,
    )
    return normalize_rew_api_snapshot(
        revision,
        'point-mlp',
        snapshot,
        evidence_type='measured',
        captured_timezone=timezone(timedelta(hours=9)),
    )


def _declared_evidence(
    revision,
    measurement_id: str,
    *,
    levels: tuple[float, ...] = (70.0, 71.0, 69.0, 72.0),
):
    raw = declared_fr_raw(
        frequency_hz=(20.0, 40.0, 80.0, 160.0),
        level_db=levels,
        phase_status='absent',
    )
    record = measurement_record_for_revision(
        revision,
        'point-mlp',
        measurement_id=measurement_id,
        evidence_type='measured',
        imported_at='2026-09-19T00:00:00+00:00',
        source_kind='unknown',
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id=f'dataset-{measurement_id}',
        measurement_id=record.measurement_id,
        frequency_hz=(20.0, 40.0, 80.0, 160.0),
        level_db=levels,
        phase_status='absent',
        source_sha256=sha256(raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    return record, dataset, raw


def test_honest_rew_api_snapshot_import_persists_and_reopens(
    tmp_path: Path,
) -> None:
    revision, repository = _repositories(tmp_path)
    record, dataset, filename, raw = _rew_api_evidence(
        revision, phase=(1.0, 2.0, 3.0, 4.0)
    )
    repository.save(record, dataset, raw_filename=filename, raw_bytes=raw)

    reopened = repository.get_dataset(dataset.dataset_id)
    assert reopened == dataset
    assert repository.dataset_for_measurement(record.measurement_id) == dataset
    assert reopened.dataset_sha256 == dataset.dataset_sha256


def test_declared_import_persists_and_reopens(tmp_path: Path) -> None:
    """Self-declared normalized FR payloads are a supported authority: the raw
    asset literally declares the persisted samples and interpretation."""
    revision, repository = _repositories(tmp_path)
    processing = {'provided_by': 'measurement_fixture'}
    raw = declared_fr_raw(
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(70.0, 71.0, 69.0),
        phase_deg=(10.0, 20.0, 30.0),
        phase_status='valid',
        level_reference='spl',
        processing=processing,
    )
    record = measurement_record_for_revision(
        revision,
        'point-mlp',
        evidence_type='measured',
        imported_at='2026-09-19T00:00:00+00:00',
        source_kind='unknown',
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id='declared-dataset-1',
        measurement_id=record.measurement_id,
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(70.0, 71.0, 69.0),
        phase_deg=(10.0, 20.0, 30.0),
        phase_status='valid',
        level_reference='spl',
        processing_json=canonical_json(processing),
        source_sha256=sha256(raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    repository.save(record, dataset, raw_filename='declared.json', raw_bytes=raw)
    assert repository.get_dataset(dataset.dataset_id) == dataset


def test_saved_dataset_persists_and_reverifies_semantic_identity(tmp_path: Path) -> None:
    revision, repository = _repositories(tmp_path)
    record, dataset, filename, raw = _rew_text_evidence(revision)
    repository.save(record, dataset, raw_filename=filename, raw_bytes=raw)

    with closing(sqlite3.connect(repository.path)) as connection:
        row = connection.execute(
            'SELECT dataset_sha256, transformation_sha256 '
            'FROM cad_frequency_responses WHERE dataset_id=?',
            (dataset.dataset_id,),
        ).fetchone()

    assert row[0] == dataset.dataset_sha256
    assert row[1] == import_transformation_sha256(
        source_sha256=dataset.source_sha256,
        importer_version=dataset.importer_version,
        dataset_sha256=dataset.dataset_sha256,
    )

    reopened = repository.get_dataset(dataset.dataset_id)
    assert reopened == dataset
    assert reopened.dataset_sha256 == row[0]


def test_quality_report_references_the_persisted_dataset_identity(tmp_path: Path) -> None:
    revision, repository = _repositories(tmp_path)
    record, dataset, filename, raw = _rew_text_evidence(revision)
    repository.save(record, dataset, raw_filename=filename, raw_bytes=raw)

    report = build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=CadMeasurementQualityEvidence(),
        profile=build_measurement_quality_profile(profile_version='integrity-1'),
        report_id='report-integrity',
        created_at_utc='2026-09-19T00:05:00+00:00',
    )
    with closing(sqlite3.connect(repository.path)) as connection:
        stored = connection.execute(
            'SELECT dataset_sha256 FROM cad_frequency_responses WHERE dataset_id=?',
            (dataset.dataset_id,),
        ).fetchone()[0]

    assert report.dataset_sha256 == stored == dataset.dataset_sha256
    assert report.raw_asset_sha256 == dataset.source_sha256

    quality_repository = CadMeasurementQualityRepository(repository)
    quality_repository.save_report(report)
    assert quality_repository.get_report(report.report_id) == report
    with pytest.raises(ValueError, match='dataset hash mismatch'):
        quality_repository.save_report(
            report.model_copy(update={'dataset_sha256': 'a' * 64})
        )


@pytest.mark.parametrize(
    'update',
    (
        {'level_db': (70.0, 99.0, 69.0, 68.0)},
        {'frequency_hz': (20.0, 41.0, 80.0, 160.0)},
        {'level_reference': 'spl'},
        {'processing_json': '{"tampered":true}'},
    ),
    ids=('levels', 'frequencies', 'level_reference', 'processing'),
)
def test_rew_text_save_rejects_samples_and_metadata_not_derived_from_raw(
    tmp_path: Path, update: dict
) -> None:
    revision, repository = _repositories(tmp_path)
    record, dataset, filename, raw = _rew_text_evidence(revision)
    forged = dataset.model_copy(update=update)

    with pytest.raises(ValueError, match='canonical import transformation'):
        repository.save(record, forged, raw_filename=filename, raw_bytes=raw)


def test_rew_text_save_rejects_altered_phase_samples(tmp_path: Path) -> None:
    revision, repository = _repositories(tmp_path)
    record, dataset, filename, raw = _rew_text_evidence(
        revision, raw=b'20 70 10\n40 71 20\n80 69 30\n160 68 40\n'
    )
    assert dataset.phase_deg == (10.0, 20.0, 30.0, 40.0)
    forged = dataset.model_copy(update={'phase_deg': (1.0, 2.0, 3.0, 4.0)})

    with pytest.raises(ValueError, match='canonical import transformation'):
        repository.save(record, forged, raw_filename=filename, raw_bytes=raw)


def test_rew_api_save_rejects_altered_decoded_samples(tmp_path: Path) -> None:
    revision, repository = _repositories(tmp_path)
    record, dataset, filename, raw = _rew_api_evidence(
        revision, phase=(1.0, 2.0, 3.0, 4.0)
    )
    assert dataset.phase_deg == (1.0, 2.0, 3.0, 4.0)

    for update in (
        {'level_db': (99.0, 98.0, 97.0, 96.0)},
        {'phase_deg': (9.0, 9.0, 9.0, 9.0)},
        {'smoothing': '1/3'},
    ):
        forged = dataset.model_copy(update=update)
        with pytest.raises(ValueError, match='canonical import transformation'):
            repository.save(record, forged, raw_filename=filename, raw_bytes=raw)


def test_rew_api_wrapper_with_altered_payload_is_rejected(tmp_path: Path) -> None:
    """A coherent wrapper whose decoded payload differs from the dataset fails."""
    revision, repository = _repositories(tmp_path)
    record, dataset, filename, raw = _rew_api_evidence(revision)

    wrapper = json.loads(raw.decode('utf-8'))
    wrapper['frequency_response']['magnitude'] = base64.b64encode(
        struct.pack('>4f', 99.0, 98.0, 97.0, 96.0)
    ).decode('ascii')
    forged_raw = canonical_json(wrapper).encode('utf-8')
    forged = dataset.model_copy(
        update={'source_sha256': sha256(forged_raw).hexdigest()}
    )

    with pytest.raises(ValueError, match='canonical import transformation'):
        repository.save(record, forged, raw_filename=filename, raw_bytes=forged_raw)


def test_unregistered_importer_version_fails_closed(tmp_path: Path) -> None:
    revision, repository = _repositories(tmp_path)
    record, dataset, raw = _declared_evidence(revision, 'm-unknown-importer')
    forged = dataset.model_copy(update={'importer_version': 'legacy-unregistered-1'})

    with pytest.raises(
        ValueError, match='no registered import transformation authority'
    ):
        repository.save(record, forged, raw_filename='x.json', raw_bytes=raw)


def test_importer_source_kind_must_match_measurement_source_kind(tmp_path: Path) -> None:
    revision, repository = _repositories(tmp_path)
    record, dataset, raw = _declared_evidence(revision, 'm-source-kind')
    wrong_kind = record.model_copy(update={'source_kind': 'rew_text'})

    with pytest.raises(
        ValueError, match='does not match the measurement source kind'
    ):
        repository.save(wrong_kind, dataset, raw_filename='x.json', raw_bytes=raw)


def test_declared_import_requires_canonical_raw_form(tmp_path: Path) -> None:
    revision, repository = _repositories(tmp_path)
    record, dataset, raw = _declared_evidence(revision, 'm-noncanonical')
    sloppy = json.dumps(json.loads(raw.decode('utf-8'))).encode('utf-8')
    forged = dataset.model_copy(
        update={'source_sha256': sha256(sloppy).hexdigest()}
    )

    with pytest.raises(ValueError, match='not in canonical form'):
        repository.save(record, forged, raw_filename='x.json', raw_bytes=sloppy)


def test_declared_import_rejects_unexpected_payload_fields(tmp_path: Path) -> None:
    revision, repository = _repositories(tmp_path)
    record, dataset, raw = _declared_evidence(revision, 'm-extra-field')
    payload = json.loads(raw.decode('utf-8'))
    payload['extra'] = True
    forged_raw = canonical_json(payload).encode('utf-8')
    forged = dataset.model_copy(
        update={'source_sha256': sha256(forged_raw).hexdigest()}
    )

    with pytest.raises(ValueError, match='unexpected fields'):
        repository.save(record, forged, raw_filename='x.json', raw_bytes=forged_raw)


def test_coherently_resealed_tampered_row_fails_closed_on_read(tmp_path: Path) -> None:
    """Recomputing both seals over tampered samples still fails importer replay."""
    revision, repository = _repositories(tmp_path)
    record, dataset, filename, raw = _rew_text_evidence(revision)
    repository.save(record, dataset, raw_filename=filename, raw_bytes=raw)

    forged = dataset.model_copy(update={'level_db': (80.0, 81.0, 79.0, 82.0)})
    forged_dataset_sha = forged.dataset_sha256
    forged_transformation = import_transformation_sha256(
        source_sha256=forged.source_sha256,
        importer_version=forged.importer_version,
        dataset_sha256=forged_dataset_sha,
    )
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            '''UPDATE cad_frequency_responses
               SET level_blob=?, dataset_sha256=?, transformation_sha256=?
               WHERE dataset_id=?''',
            (
                _pack(forged.level_db),
                forged_dataset_sha,
                forged_transformation,
                dataset.dataset_id,
            ),
        )

    # Both persisted seals verify against the tampered row, but the pinned
    # importer replay still proves the stored samples do not derive from the
    # retained raw bytes.
    with pytest.raises(ValueError, match='canonical import transformation'):
        repository.get_dataset(dataset.dataset_id)


def test_tampered_raw_asset_content_fails_closed_on_read(tmp_path: Path) -> None:
    revision, repository = _repositories(tmp_path)
    record, dataset, filename, raw = _rew_text_evidence(revision)
    repository.save(record, dataset, raw_filename=filename, raw_bytes=raw)

    (repository.assets_dir / dataset.source_sha256).write_bytes(b'tampered bytes')

    with pytest.raises(ValueError, match='size mismatch|SHA-256 mismatch'):
        repository.get_dataset(dataset.dataset_id)


def test_missing_raw_asset_fails_closed_on_read(tmp_path: Path) -> None:
    revision, repository = _repositories(tmp_path)
    record, dataset, filename, raw = _rew_text_evidence(revision)
    repository.save(record, dataset, raw_filename=filename, raw_bytes=raw)

    (repository.assets_dir / dataset.source_sha256).unlink()

    with pytest.raises(ValueError, match='missing or not a regular file'):
        repository.get_dataset(dataset.dataset_id)


def test_verify_measurement_asset_authority_accepts_intact_asset(
    tmp_path: Path,
) -> None:
    revision, repository = _repositories(tmp_path)
    record, dataset, filename, raw = _rew_text_evidence(revision)
    repository.save(record, dataset, raw_filename=filename, raw_bytes=raw)

    repository.verify_measurement_asset_authority(record.measurement_id)


def test_verify_measurement_asset_authority_fails_closed_on_missing_asset(
    tmp_path: Path,
) -> None:
    revision, repository = _repositories(tmp_path)
    record, dataset, filename, raw = _rew_text_evidence(revision)
    repository.save(record, dataset, raw_filename=filename, raw_bytes=raw)

    (repository.assets_dir / dataset.source_sha256).unlink()

    with pytest.raises(ValueError, match='missing or not a regular file'):
        repository.verify_measurement_asset_authority(record.measurement_id)


def test_verify_measurement_asset_authority_fails_closed_on_corrupt_asset(
    tmp_path: Path,
) -> None:
    revision, repository = _repositories(tmp_path)
    record, dataset, filename, raw = _rew_text_evidence(revision)
    repository.save(record, dataset, raw_filename=filename, raw_bytes=raw)

    (repository.assets_dir / dataset.source_sha256).write_bytes(b'corrupt')

    with pytest.raises(ValueError, match='mismatch'):
        repository.verify_measurement_asset_authority(record.measurement_id)


def test_verify_measurement_asset_authority_fails_closed_on_size_drift(
    tmp_path: Path,
) -> None:
    revision, repository = _repositories(tmp_path)
    record, dataset, filename, raw = _rew_text_evidence(revision)
    repository.save(record, dataset, raw_filename=filename, raw_bytes=raw)

    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            'UPDATE cad_measurement_assets SET size_bytes=size_bytes+1 WHERE sha256=?',
            (dataset.source_sha256,),
        )

    with pytest.raises(ValueError, match='asset size mismatch'):
        repository.verify_measurement_asset_authority(record.measurement_id)


def test_verify_measurement_asset_authority_fails_closed_on_missing_registry_row(
    tmp_path: Path,
) -> None:
    revision, repository = _repositories(tmp_path)
    record, dataset, filename, raw = _rew_text_evidence(revision)
    repository.save(record, dataset, raw_filename=filename, raw_bytes=raw)

    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            'DELETE FROM cad_measurement_assets WHERE sha256=?',
            (dataset.source_sha256,),
        )

    with pytest.raises(ValueError, match='has no cad_measurement_assets row'):
        repository.verify_measurement_asset_authority(record.measurement_id)


def test_verify_measurement_asset_authority_fails_closed_on_unknown_measurement(
    tmp_path: Path,
) -> None:
    _revision, repository = _repositories(tmp_path)

    with pytest.raises(ValueError, match='no bound frequency-response dataset'):
        repository.verify_measurement_asset_authority('unknown-measurement')


def test_pre_binding_row_without_persisted_identity_is_non_authoritative(
    tmp_path: Path,
) -> None:
    """Historical policy: pre-binding rows keep NULL seals and fail closed."""
    revision, repository = _repositories(tmp_path)
    record, dataset, filename, raw = _rew_text_evidence(revision)
    repository.save(record, dataset, raw_filename=filename, raw_bytes=raw)

    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            'UPDATE cad_frequency_responses '
            'SET dataset_sha256=NULL, transformation_sha256=NULL '
            'WHERE dataset_id=?',
            (dataset.dataset_id,),
        )

    with pytest.raises(ValueError, match='non-authoritative'):
        repository.get_dataset(dataset.dataset_id)
    with pytest.raises(ValueError, match='non-authoritative'):
        repository.dataset_for_measurement(record.measurement_id)


@pytest.mark.parametrize(
    'cleared_column', ('dataset_sha256', 'transformation_sha256')
)
def test_partially_missing_identity_columns_fail_closed(
    tmp_path: Path, cleared_column: str
) -> None:
    revision, repository = _repositories(tmp_path)
    record, dataset, filename, raw = _rew_text_evidence(revision)
    repository.save(record, dataset, raw_filename=filename, raw_bytes=raw)

    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            f'UPDATE cad_frequency_responses SET {cleared_column}=NULL '
            'WHERE dataset_id=?',
            (dataset.dataset_id,),
        )

    with pytest.raises(ValueError, match='non-authoritative'):
        repository.get_dataset(dataset.dataset_id)
