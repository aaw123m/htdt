"""#733 playback level authority tests."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurement_disposition import build_measurement_disposition
from htdt.cad_measurement_quality_repository import CadMeasurementQualityRepository
from htdt.cad_measurements import (
    HTDT_DECLARED_IMPORTER_VERSION,
    canonical_json,
    declared_fr_raw,
    measurement_record_for_revision,
)
from htdt.cad_playback_level import (
    DeviceVolumeIndication,
    LfeReferenceSemantics,
    PlaybackAcousticEvidence,
    PlaybackLevelProvenanceItem,
    PlaybackProgramLevel,
    ReferenceProfileRef,
    build_playback_level_condition,
    build_reference_profile,
)
from htdt.cad_playback_level_repository import (
    CadPlaybackLevelRepository,
    PlaybackLevelConflictError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene

NOW = '2026-09-23T00:00:00+00:00'
AFTER = '2026-09-23T12:31:00+00:00'


def _repositories(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    return scene_repository, revision, CadPlaybackLevelRepository(
        scene_repository
    )


def _save_measurement(measurement_repository, revision, measurement_id):
    processing = {'fixture_raw': f'raw-{measurement_id}'}
    raw = declared_fr_raw(
        frequency_hz=(20.0, 80.0, 1000.0, 20000.0),
        level_db=(70.0, 71.0, 69.0, 68.0),
        phase_status='absent',
        level_reference='spl',
        processing=processing,
    )
    record = measurement_record_for_revision(
        revision,
        'point-mlp',
        measurement_id=measurement_id,
        evidence_type='measured',
        channel_role='FL',
        source_speaker_ids=('speaker-fl',),
        radiation_scope='single',
        routing_evidence='verified',
        imported_at=AFTER,
        source_kind='unknown',
        external_source_id=f'rew-{measurement_id}',
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id=f'dataset-{measurement_id}',
        measurement_id=measurement_id,
        frequency_hz=(20.0, 80.0, 1000.0, 20000.0),
        level_db=(70.0, 71.0, 69.0, 68.0),
        phase_status='absent',
        level_reference='spl',
        processing_json=canonical_json(processing),
        source_sha256=sha256(raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    measurement_repository.save(
        record, dataset, raw_filename=f'{measurement_id}.json', raw_bytes=raw
    )
    return record


def _condition(revision, **overrides):
    payload = {
        'document_id': revision.document_id,
        'name': 'Movie reference',
        'scene_revision_id': revision.revision_id,
        'scene_content_hash': revision.content_hash,
        'created_at_utc': NOW,
    }
    payload.update(overrides)
    return build_playback_level_condition(**payload)


def test_three_db_domains_stay_separate(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    condition = _condition(
        revision,
        device_volume=DeviceVolumeIndication(
            scale='relative_db', value=-20.0, display_label='-20.0 dB',
            device_ref='avr-x4800',
        ),
        program_level=PlaybackProgramLevel(
            kind='test_sweep',
            level=-20.0,
            level_unit='dbfs_rms',
            stimulus_label='REW sweep',
        ),
    )
    repository.save_condition(condition)
    stored = repository.get_condition(condition.condition_id)
    assert stored is not None
    # Same numeric value, different domains — recorded verbatim, never equated.
    assert stored.device_volume.value == -20.0
    assert stored.device_volume.scale == 'relative_db'
    assert stored.program_level.level_unit == 'dbfs_rms'
    assert stored.acoustic_evidence == ()


def test_device_scale_is_preserved_even_when_unknown(tmp_path: Path) -> None:
    _scenes, revision, _repo = _repositories(tmp_path)
    # A vendor 0-100 scale stores the displayed number; an UNKNOWN scale
    # rejects numeric claims entirely.
    vendor = DeviceVolumeIndication(scale='vendor_numeric', value=62.0)
    assert vendor.value == 62.0
    with pytest.raises(ValidationError):
        DeviceVolumeIndication(scale='unknown', value=62.0)
    assert DeviceVolumeIndication(scale='unknown').value is None


def test_acoustic_spl_requires_exact_evidence(tmp_path: Path) -> None:
    scenes, revision, repository = _repositories(tmp_path)
    measurements = CadMeasurementRepository(scenes)
    record = _save_measurement(measurements, revision, 'm-spl')
    condition = _condition(
        revision,
        acoustic_evidence=(
            PlaybackAcousticEvidence(
                measurement_id=record.measurement_id,
                listener_ref='point-mlp',
                level_db_spl=79.0,
                weighting='C',
                uncertainty_db=1.0,
                calibration_ref='spl-cal-2026-09',
            ),
        ),
    )
    repository.save_condition(condition)
    stored = repository.get_condition(condition.condition_id)
    assert stored.acoustic_evidence[0].level_db_spl == 79.0

    # Unknown measurement -> rejected.
    ghost = _condition(
        revision,
        acoustic_evidence=(
            PlaybackAcousticEvidence(
                measurement_id='ghost-m',
                listener_ref='point-mlp',
                level_db_spl=79.0,
                weighting='C',
                calibration_ref='spl-cal-2026-09',
            ),
        ),
    )
    with pytest.raises(ValueError, match='unknown measurement'):
        repository.save_condition(ghost)


def test_acoustic_evidence_rejects_ineligible_disposition(tmp_path: Path) -> None:
    scenes, revision, repository = _repositories(tmp_path)
    measurements = CadMeasurementRepository(scenes)
    quality = CadMeasurementQualityRepository(measurements)
    _save_measurement(measurements, revision, 'm-testonly')
    quality.save_disposition(
        build_measurement_disposition(
            document_id=revision.document_id,
            measurement_id='m-testonly',
            disposition='test_only',
            reason='fixture sweep',
            created_at_utc=AFTER,
        )
    )
    condition = _condition(
        revision,
        acoustic_evidence=(
            PlaybackAcousticEvidence(
                measurement_id='m-testonly',
                listener_ref='point-mlp',
                level_db_spl=79.0,
                weighting='C',
                calibration_ref='spl-cal-2026-09',
            ),
        ),
    )
    with pytest.raises(ValueError, match='not eligible'):
        repository.save_condition(condition)


def test_reference_profile_is_versioned_and_source_bound(
    tmp_path: Path,
) -> None:
    scenes, revision, repository = _repositories(tmp_path)
    profile = build_reference_profile(
        name='Home cinema target',
        source='user_authored',
        document_id=revision.document_id,
        target_level_db_spl=80.0,
        target_weighting='C',
        lfe=LfeReferenceSemantics(lfe_program_offset_db=10.0),
        created_at_utc=NOW,
    )
    repository.save_profile(profile)
    assert repository.get_profile(profile.profile_id, '1') == profile
    assert repository.get_profile_by_hash(profile.semantic_sha256) == profile

    # Append-only per (profile_id, version).
    with pytest.raises(PlaybackLevelConflictError):
        repository.save_profile(profile)

    # A sourced profile must bind exact source label+version.
    with pytest.raises(ValidationError, match='source_label'):
        build_reference_profile(
            name='Cinema spec',
            source='external_standard',
            created_at_utc=NOW,
        )
    # A user-authored profile is project-scoped.
    with pytest.raises(ValidationError, match='document_id'):
        build_reference_profile(
            name='Mine', source='user_authored', created_at_utc=NOW
        )


def test_condition_binds_reference_profile_exactly(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    profile = build_reference_profile(
        name='Home cinema target',
        source='user_authored',
        document_id=revision.document_id,
        created_at_utc=NOW,
    )
    repository.save_profile(profile)
    condition = _condition(
        revision,
        reference_profile=ReferenceProfileRef(
            profile_id=profile.profile_id,
            version=profile.version,
            semantic_sha256=profile.semantic_sha256,
        ),
    )
    repository.save_condition(condition)
    assert repository.get_condition(condition.condition_id) == condition

    # Unknown hash -> rejected.
    bad = _condition(
        revision,
        reference_profile=ReferenceProfileRef(
            profile_id=profile.profile_id,
            version='9',
            semantic_sha256='c' * 64,
        ),
    )
    with pytest.raises(ValueError, match='not persisted'):
        repository.save_condition(bad)


def test_condition_scene_revision_integrity(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    with pytest.raises(ValueError, match='not persisted'):
        repository.save_condition(
            _condition(revision, scene_revision_id='ghost-revision')
        )
    with pytest.raises(ValueError, match='content hash'):
        repository.save_condition(
            _condition(revision, scene_content_hash='d' * 64)
        )


def test_condition_is_append_only_and_hashed(tmp_path: Path) -> None:
    _scenes, revision, repository = _repositories(tmp_path)
    condition = _condition(
        revision,
        provenance=(
            PlaybackLevelProvenanceItem(key='setup', value='avr-x4800'),
        ),
    )
    repository.save_condition(condition)
    assert (
        repository.get_condition_by_hash(condition.condition_sha256)
        == condition
    )
    with pytest.raises(PlaybackLevelConflictError):
        repository.save_condition(condition)

    payload = condition.model_dump(mode='python')
    payload['name'] = 'Tampered'
    with pytest.raises(ValidationError, match='hash mismatch'):
        type(condition)(**payload)


def test_unknown_program_level_cannot_carry_numeric_claim() -> None:
    PlaybackProgramLevel(kind='unknown')
    with pytest.raises(ValidationError):
        PlaybackProgramLevel(kind='unknown', level=-20.0)
    with pytest.raises(ValidationError):
        PlaybackProgramLevel(kind='test_sweep', level=-20.0)
