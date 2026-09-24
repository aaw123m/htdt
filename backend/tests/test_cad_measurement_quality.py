from __future__ import annotations

from contextlib import closing
from hashlib import sha256
from pathlib import Path
import sqlite3
import threading

import pytest
from pydantic import ValidationError

from htdt.cad_measurement_authorities import (
    build_acoustic_level_calibration,
    build_dataset_level_reference,
)
from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurement_quality import (
    QUALITY_ALGORITHM_SHA256,
    CadAcquisitionContextBinding,
    CadMeasurementObservationBinding,
    CadMeasurementQualityCheck,
    CadMeasurementQualityEvidence,
    CadMeasurementQualityReport,
    _hash,
    acquisition_context_binding,
    build_acquisition_context,
    build_measurement_lineage,
    build_measurement_observation,
    build_measurement_quality_profile,
    build_measurement_quality_report,
    gate_measurement_claim,
    measurement_repeatability_rms_db,
    measurement_retake_guidance,
    observation_binding,
    replay_measurement_quality_report,
)
from htdt.cad_measurement_quality_repository import (
    CadMeasurementQualityRepository,
    MeasurementLineageConflictError,
)
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurements import (
    HTDT_DECLARED_IMPORTER_VERSION,
    canonical_json,
    declared_fr_raw,
    measurement_record_for_revision,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.managed_assets import ManagedAssetError


def _repositories(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(make_f1_scene(), parent_revision_id=None).revision
    measurement_repository = CadMeasurementRepository(scene_repository)
    quality_repository = CadMeasurementQualityRepository(measurement_repository)
    return revision, measurement_repository, quality_repository


def _save_measurement(
    repository: CadMeasurementRepository,
    revision,
    measurement_id: str,
    *,
    raw: bytes,
    phase_status: str = 'absent',
    phase_deg: tuple[float, ...] | None = None,
    level_reference: str = 'unknown',
    level_db: tuple[float, ...] = (70.0, 71.0, 69.0),
):
    # The declared importer keeps fixture datasets honestly derived: the raw
    # asset literally declares the persisted samples, with the caller's raw
    # marker embedded so each measurement still gets a distinct asset.
    processing = {'fixture_raw': raw.decode('utf-8')}
    declared_raw = declared_fr_raw(
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=level_db,
        phase_deg=phase_deg,
        phase_status=phase_status,
        level_reference=level_reference,
        processing=processing,
    )
    record = measurement_record_for_revision(
        revision,
        'point-mlp',
        measurement_id=measurement_id,
        evidence_type='measured',
        channel_role='front_left',
        source_speaker_ids=('speaker-fl',),
        radiation_scope='single',
        routing_evidence='verified',
        imported_at='2026-09-19T00:00:00+00:00',
        source_kind='unknown',
        external_source_id=f'rew-{measurement_id}',
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id=f'dataset-{measurement_id}',
        measurement_id=measurement_id,
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=level_db,
        phase_deg=phase_deg,
        phase_status=phase_status,
        level_reference=level_reference,
        processing_json=canonical_json(processing),
        source_sha256=sha256(declared_raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    repository.save(
        record,
        dataset,
        raw_filename=f'{measurement_id}.json',
        raw_bytes=declared_raw,
    )
    return record, dataset


def _persist_context(
    quality_repository: CadMeasurementQualityRepository,
    *,
    context_id: str,
    subject_ids: tuple[str, ...],
    source_kind: str = 'native',
    created_at_utc: str = '2026-09-19T00:00:00+00:00',
    **timing_fields,
) -> CadAcquisitionContextBinding:
    """Persist an acquisition-context authority and return its exact binding."""
    context = build_acquisition_context(
        acquisition_context_id=context_id,
        source_kind=source_kind,
        subject_measurement_ids=subject_ids,
        created_at_utc=created_at_utc,
        **timing_fields,
    )
    quality_repository.save_acquisition_context(context)
    return acquisition_context_binding(context)


def _persist_observation(
    quality_repository: CadMeasurementQualityRepository,
    measurement_id: str,
    *,
    observation_id: str,
    source_kind: str = 'manual',
    source_asset_sha256: str | None = None,
    observed_at_utc: str = '2026-09-19T00:00:30+00:00',
    **fields,
):
    """Persist a measurement-observation authority and return its binding."""
    observation = build_measurement_observation(
        observation_id=observation_id,
        measurement_id=measurement_id,
        source_kind=source_kind,
        source_asset_sha256=source_asset_sha256,
        observed_at_utc=observed_at_utc,
        **fields,
    )
    quality_repository.save_observation(observation)
    return observation_binding(observation)


def _rehashed(report: CadMeasurementQualityReport) -> CadMeasurementQualityReport:
    """Recompute report_sha256 so a tampered payload stays self-consistent."""
    return report.model_copy(
        update={'report_sha256': _hash(report.identity_payload())}
    )


def _rewrite_report_row(path: Path, report: CadMeasurementQualityReport) -> None:
    """Overwrite a persisted report row directly, bypassing save-time validation."""
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            '''
            UPDATE cad_measurement_quality_reports
            SET report_sha256=?, profile_sha256=?, payload_json=?
            WHERE report_id=?
            ''',
            (
                report.report_sha256,
                report.profile.profile_sha256,
                report.model_dump_json(),
                report.report_id,
            ),
        )


def _insert_lineage_row(path: Path, lineage) -> None:
    """Persist a lineage row directly, bypassing save-time chain validation."""
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            '''
            INSERT INTO cad_measurement_lineage(
                lineage_id, document_id, measurement_id, supersedes_measurement_id,
                selected_measurement_id, lineage_sha256, created_at_utc, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                lineage.lineage_id,
                lineage.document_id,
                lineage.measurement_id,
                lineage.supersedes_measurement_id,
                lineage.selected_measurement_id,
                lineage.lineage_sha256,
                lineage.created_at_utc,
                lineage.model_dump_json(),
            ),
        )


def _delete_lineage_row(path: Path, lineage_id: str) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            'DELETE FROM cad_measurement_lineage WHERE lineage_id=?',
            (lineage_id,),
        )


def test_fr_only_quality_keeps_magnitude_and_does_not_invent_missing_evidence(tmp_path: Path) -> None:
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    record, dataset = _save_measurement(
        measurement_repository,
        revision,
        'fr-only',
        raw=b'fr-only',
    )
    profile = build_measurement_quality_profile()
    report = build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=CadMeasurementQualityEvidence(),
        profile=profile,
        report_id='report-fr-only',
        created_at_utc='2026-09-19T00:01:00+00:00',
    )

    assert report.algorithm_sha256 == QUALITY_ALGORITHM_SHA256
    assert report.profile.profile_sha256 == profile.profile_sha256
    assert report.clipping.status == 'UNKNOWN'
    assert report.noise_snr.status == 'UNKNOWN'
    assert report.usable_frequency_band.status == 'UNKNOWN'
    assert report.ir_window.status == 'NOT_EVALUATED'
    assert report.calibration.status == 'UNKNOWN'
    assert report.retake_recommendation == 'UNKNOWN'
    assert report.capability('magnitude_response').decision == 'ALLOWED'
    assert report.capability('phase_response').decision == 'BLOCKED'
    assert report.capability('common_timing').decision == 'UNKNOWN'
    assert report.capability('arrival_time').decision == 'BLOCKED'
    assert report.capability('decay').decision == 'BLOCKED'
    assert gate_measurement_claim(
        report, 'magnitude_response', required_band_hz=(20.0, 80.0)
    ).decision == 'UNKNOWN'

    quality_repository.save_report(report)
    assert quality_repository.get_report(report.report_id) == report


def test_phase_array_does_not_imply_common_timing(tmp_path: Path) -> None:
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    record, dataset = _save_measurement(
        measurement_repository,
        revision,
        'phase-no-timing',
        raw=b'phase-no-timing',
        phase_status='valid',
        phase_deg=(5.0, 10.0, 15.0),
    )
    report = build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=CadMeasurementQualityEvidence(),
        profile=build_measurement_quality_profile(),
        report_id='report-phase-no-timing',
        created_at_utc='2026-09-19T00:02:00+00:00',
    )

    assert report.capability('phase_response').decision == 'ALLOWED'
    assert report.timing_reference.status == 'UNKNOWN'
    assert report.capability('common_timing').decision == 'UNKNOWN'
    assert report.capability('arrival_time').decision == 'BLOCKED'

    quality_repository.save_report(report)


def test_explicit_quality_metadata_opens_only_supported_claims(tmp_path: Path) -> None:
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    first, first_dataset = _save_measurement(
        measurement_repository,
        revision,
        'repeat-a',
        raw=b'repeat-a',
        phase_status='valid',
        phase_deg=(5.0, 10.0, 15.0),
        level_reference='spl',
    )
    record, dataset = _save_measurement(
        measurement_repository,
        revision,
        'repeat-b',
        raw=b'repeat-b',
        phase_status='valid',
        phase_deg=(6.0, 11.0, 16.0),
        level_reference='spl',
    )
    calibration_sha = quality_repository.save_calibration_file(
        filename='umik.txt',
        raw_bytes=b'umik-calibration',
    )
    acquisition = _persist_context(
        quality_repository,
        context_id='acq-1',
        subject_ids=(first.measurement_id, record.measurement_id),
        source_kind='native',
        timing_reference_valid=True,
        timing_reference_id='loopback-1',
        clock_source='umik-1-usb',
        sample_rate_hz=48000,
        delay_correction_s=0.00025,
    )
    observation = _persist_observation(
        quality_repository,
        record.measurement_id,
        observation_id='obs-repeat-b',
        source_kind='rew_metadata',
        source_asset_sha256=dataset.source_sha256,
        clipping_detected=False,
        peak_dbfs=-3.0,
        noise_floor_db_spl=30.0,
        signal_level_db_spl=70.0,
        snr_db=40.0,
        usable_frequency_band_hz=(20.0, 80.0),
        polarity_correct=True,
        polarity_confidence=0.99,
        has_impulse_response=True,
        ir_window_start_s=-0.01,
        ir_window_end_s=0.5,
        ir_truncated=False,
    )
    evidence = CadMeasurementQualityEvidence(
        clipping_detected=False,
        peak_dbfs=-3.0,
        noise_floor_db_spl=30.0,
        signal_level_db_spl=70.0,
        snr_db=40.0,
        usable_frequency_band_hz=(20.0, 80.0),
        timing_reference_valid=True,
        timing_reference_id='loopback-1',
        clock_source='umik-1-usb',
        sample_rate_hz=48000,
        delay_correction_s=0.00025,
        polarity_correct=True,
        polarity_confidence=0.99,
        has_impulse_response=True,
        ir_window_start_s=-0.01,
        ir_window_end_s=0.5,
        ir_truncated=False,
        calibration_filename='umik.txt',
        calibration_file_sha256=calibration_sha,
        expected_calibration_file_sha256=calibration_sha,
        repeat_measurement_ids=(first.measurement_id, record.measurement_id),
        repeatability_rms_db=measurement_repeatability_rms_db(
            (first_dataset, dataset)
        ),
        evidence_source='rew_metadata',
    )
    profile = build_measurement_quality_profile(
        required_usable_band_hz=(20.0, 80.0),
        minimum_snr_db=20.0,
        minimum_polarity_confidence=0.9,
        maximum_repeatability_rms_db=1.0,
    )
    # Absolute-level claims (#861) additionally resolve the persisted
    # dataset level reference and its bound acoustic level calibration.
    level_calibration = build_acoustic_level_calibration(
        method='acoustic_calibrator',
        reference_level_db_spl=94.0,
        reference_frequency_hz=1000.0,
        calibrated_at_utc='2026-09-18T00:00:00+00:00',
        validity_scope='measurement',
    )
    quality_repository.save_level_calibration(level_calibration)
    level_reference = build_dataset_level_reference(
        measurement_id=record.measurement_id,
        dataset_id=dataset.dataset_id,
        dataset_sha256=dataset.dataset_sha256,
        level_reference_kind='absolute_spl',
        calibration_id=level_calibration.calibration_id,
        calibration_sha256=level_calibration.calibration_sha256,
    )
    quality_repository.save_dataset_level_reference(level_reference)
    acquisition_record = quality_repository.get_acquisition_context(
        acquisition.acquisition_context_id
    )
    report = build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=evidence,
        profile=profile,
        acquisition_context=acquisition,
        observation=observation,
        dataset_level_reference=level_reference,
        level_calibration=level_calibration,
        acquisition_context_record=acquisition_record,
        observation_record=quality_repository.get_observation(
            observation.observation_id
        ),
        report_id='report-rich',
        created_at_utc='2026-09-19T00:03:00+00:00',
    )

    assert {
        report.clipping.status,
        report.noise_snr.status,
        report.usable_frequency_band.status,
        report.timing_reference.status,
        report.polarity.status,
        report.ir_window.status,
        report.calibration.status,
        report.repeatability.status,
    } == {'PASS'}
    assert report.retake_recommendation == 'NOT_NEEDED'
    assert all(item.decision == 'ALLOWED' for item in report.capabilities)
    assert gate_measurement_claim(
        report, 'phase_response', required_band_hz=(20.0, 80.0)
    ).decision == 'ALLOWED'
    assert gate_measurement_claim(
        report, 'phase_response', required_band_hz=(10.0, 100.0)
    ).decision == 'BLOCKED'
    historical_without_polarity = report.model_copy(
        update={'capabilities': report.capabilities[:-1]}
    )
    assert gate_measurement_claim(
        historical_without_polarity, 'polarity'
    ).decision == 'UNKNOWN'

    quality_repository.save_report(report)
    assert quality_repository.latest_report(record.measurement_id) == report
    reopened_repository = CadMeasurementQualityRepository(measurement_repository)
    assert reopened_repository.get_report(report.report_id) == report

    with pytest.raises(ValidationError):
        report.retake_recommendation = 'RETAKE'

    tampered = report.model_copy(update={'measurement_sha256': '0' * 64})
    with pytest.raises(ValueError, match='measurement hash mismatch'):
        quality_repository.save_report(tampered)
    tampered = report.model_copy(update={'dataset_sha256': '0' * 64})
    with pytest.raises(ValueError, match='dataset hash mismatch'):
        quality_repository.save_report(tampered)
    tampered = report.model_copy(update={'raw_asset_sha256': '0' * 64})
    with pytest.raises(ValueError, match='raw asset hash mismatch'):
        quality_repository.save_report(tampered)
    tampered = report.model_copy(update={'scene_revision_id': 'tampered-revision'})
    with pytest.raises(ValueError, match='SceneRevision/entity/measurement-point binding mismatch'):
        quality_repository.save_report(tampered)





def test_unconfigured_thresholds_never_turn_explicit_evidence_into_pass(tmp_path: Path) -> None:
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    first, first_dataset = _save_measurement(
        measurement_repository,
        revision,
        'unconfigured-a',
        raw=b'unconfigured-a',
    )
    record, dataset = _save_measurement(
        measurement_repository,
        revision,
        'unconfigured-b',
        raw=b'unconfigured-b',
    )
    observation = _persist_observation(
        quality_repository,
        record.measurement_id,
        observation_id='obs-unconfigured-b',
        clipping_detected=False,
        snr_db=40.0,
        usable_frequency_band_hz=(20.0, 80.0),
        polarity_correct=True,
        polarity_confidence=0.99,
    )
    report = build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=CadMeasurementQualityEvidence(
            clipping_detected=False,
            snr_db=40.0,
            usable_frequency_band_hz=(20.0, 80.0),
            polarity_correct=True,
            polarity_confidence=0.99,
            repeat_measurement_ids=(first.measurement_id, record.measurement_id),
            repeatability_rms_db=measurement_repeatability_rms_db(
                (first_dataset, dataset)
            ),
            evidence_source='manual',
        ),
        profile=build_measurement_quality_profile(profile_version='unconfigured-thresholds-1'),
        observation=observation,
        report_id='report-unconfigured-thresholds',
        created_at_utc='2026-09-19T00:03:10+00:00',
    )

    assert report.clipping.status == 'PASS'
    assert report.usable_frequency_band.status == 'PASS'
    assert report.noise_snr.status == 'NOT_EVALUATED'
    assert report.polarity.status == 'NOT_EVALUATED'
    assert report.repeatability.status == 'NOT_EVALUATED'
    assert report.capability('polarity').decision == 'UNKNOWN'
    assert report.capability('repeatability').decision == 'UNKNOWN'
    quality_repository.save_report(report)


def test_explicit_quality_failures_block_their_downstream_claims(tmp_path: Path) -> None:
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    # Repeat datasets 4 dB apart at every sample reproduce a canonical
    # repeatability RMS of exactly 2.0 dB, over the 1.0 dB profile maximum.
    first, first_dataset = _save_measurement(
        measurement_repository,
        revision,
        'fail-repeat-a',
        raw=b'fail-repeat-a',
        phase_status='valid',
        phase_deg=(5.0, 10.0, 15.0),
        level_db=(74.0, 75.0, 73.0),
    )
    record, dataset = _save_measurement(
        measurement_repository,
        revision,
        'fail-repeat-b',
        raw=b'fail-repeat-b',
        phase_status='valid',
        phase_deg=(6.0, 11.0, 16.0),
    )
    assert measurement_repeatability_rms_db((first_dataset, dataset)) == 2.0
    quality_repository.save_calibration_file(
        filename='wrong.txt', raw_bytes=b'wrong-cal'
    )
    quality_repository.save_calibration_file(
        filename='expected.txt', raw_bytes=b'expected-cal'
    )
    acquisition = _persist_context(
        quality_repository,
        context_id='acq-fail',
        subject_ids=(record.measurement_id,),
        source_kind='native',
        timing_reference_valid=False,
    )
    observation = _persist_observation(
        quality_repository,
        record.measurement_id,
        observation_id='obs-fail-repeat-b',
        source_kind='rew_metadata',
        source_asset_sha256=dataset.source_sha256,
        clipping_detected=True,
        snr_db=10.0,
        usable_frequency_band_hz=(30.0, 70.0),
        polarity_correct=False,
        polarity_confidence=0.99,
        has_impulse_response=True,
        ir_window_start_s=-0.01,
        ir_window_end_s=0.5,
        ir_truncated=True,
    )
    report = build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=CadMeasurementQualityEvidence(
            clipping_detected=True,
            snr_db=10.0,
            usable_frequency_band_hz=(30.0, 70.0),
            timing_reference_valid=False,
            polarity_correct=False,
            polarity_confidence=0.99,
            has_impulse_response=True,
            ir_window_start_s=-0.01,
            ir_window_end_s=0.5,
            ir_truncated=True,
            calibration_file_sha256=sha256(b'wrong-cal').hexdigest(),
            expected_calibration_file_sha256=sha256(b'expected-cal').hexdigest(),
            repeat_measurement_ids=(first.measurement_id, record.measurement_id),
            repeatability_rms_db=2.0,
            evidence_source='rew_metadata',
        ),
        profile=build_measurement_quality_profile(
            required_usable_band_hz=(20.0, 80.0),
            minimum_snr_db=20.0,
            maximum_repeatability_rms_db=1.0,
        ),
        acquisition_context=acquisition,
        observation=observation,
        report_id='report-failures',
        created_at_utc='2026-09-19T00:03:15+00:00',
    )

    assert report.capability('magnitude_response').decision == 'ALLOWED'
    assert report.capability('phase_response').decision == 'ALLOWED'
    for claim in (
        'common_timing',
        'arrival_time',
        'decay',
        'calibrated_response',
        'repeatability',
        'polarity',
    ):
        assert report.capability(claim).decision == 'BLOCKED'
    assert report.retake_recommendation == 'RETAKE'
    quality_repository.save_report(report)


def test_calibration_match_does_not_open_calibrated_response_without_capture_quality(tmp_path: Path) -> None:
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    record, dataset = _save_measurement(
        measurement_repository,
        revision,
        'cal-match-no-quality',
        raw=b'cal-match-no-quality',
        level_reference='spl',
    )
    calibration_sha = quality_repository.save_calibration_file(
        filename='umik.txt', raw_bytes=b'calibration'
    )
    acquisition = _persist_context(
        quality_repository,
        context_id='acq-cal-match',
        subject_ids=(record.measurement_id,),
        source_kind='unknown',
    )
    report = build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=CadMeasurementQualityEvidence(
            calibration_filename='umik.txt',
            calibration_file_sha256=calibration_sha,
            expected_calibration_file_sha256=calibration_sha,
        ),
        profile=build_measurement_quality_profile(),
        acquisition_context=acquisition,
        report_id='report-cal-match-no-quality',
        created_at_utc='2026-09-19T00:03:30+00:00',
    )

    assert report.calibration.status == 'PASS'
    assert report.clipping.status == 'UNKNOWN'
    assert report.noise_snr.status == 'UNKNOWN'
    assert report.usable_frequency_band.status == 'UNKNOWN'
    assert report.capability('calibrated_response').decision == 'UNKNOWN'
    assert report.capability('polarity').decision == 'UNKNOWN'
    quality_repository.save_report(report)


def test_calibration_file_mismatch_blocks_calibrated_claim_and_recommends_retake(tmp_path: Path) -> None:
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    record, dataset = _save_measurement(
        measurement_repository,
        revision,
        'cal-mismatch',
        raw=b'cal-mismatch',
    )
    quality_repository.save_calibration_file(
        filename='wrong.txt', raw_bytes=b'wrong-cal'
    )
    quality_repository.save_calibration_file(
        filename='expected.txt', raw_bytes=b'expected-cal'
    )
    report = build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=CadMeasurementQualityEvidence(
            calibration_filename='wrong.txt',
            calibration_file_sha256=sha256(b'wrong-cal').hexdigest(),
            expected_calibration_file_sha256=sha256(b'expected-cal').hexdigest(),
        ),
        profile=build_measurement_quality_profile(),
        report_id='report-cal-mismatch',
        created_at_utc='2026-09-19T00:04:00+00:00',
    )

    assert report.calibration.status == 'FAIL'
    assert report.capability('calibrated_response').decision == 'BLOCKED'
    assert report.retake_recommendation == 'RETAKE'
    quality_repository.save_report(report)


def test_profile_change_and_retake_preserve_old_reports_and_do_not_reassign_campaign_evidence(tmp_path: Path) -> None:
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    old_record, old_dataset = _save_measurement(
        measurement_repository,
        revision,
        'old',
        raw=b'old',
    )

    old_profile = build_measurement_quality_profile(profile_version='profile-1', minimum_snr_db=20.0)
    old_observation = _persist_observation(
        quality_repository,
        old_record.measurement_id,
        observation_id='obs-old',
        snr_db=25.0,
    )
    old_report = build_measurement_quality_report(
        measurement=old_record,
        dataset=old_dataset,
        evidence=CadMeasurementQualityEvidence(
            snr_db=25.0,
            evidence_source='manual',
        ),
        profile=old_profile,
        observation=old_observation,
        report_id='old-report',
        created_at_utc='2026-09-19T00:05:00+00:00',
    )
    quality_repository.save_report(old_report)

    stricter_profile = build_measurement_quality_profile(
        profile_version='profile-2',
        minimum_snr_db=30.0,
    )
    recheck = build_measurement_quality_report(
        measurement=old_record,
        dataset=old_dataset,
        evidence=old_report.evidence,
        profile=stricter_profile,
        observation=old_observation,
        report_id='old-report-profile-2',
        created_at_utc='2026-09-19T00:06:00+00:00',
    )
    quality_repository.save_report(recheck)

    new_record, new_dataset = _save_measurement(
        measurement_repository,
        revision,
        'retake',
        raw=b'retake',
    )
    retake_observation = _persist_observation(
        quality_repository,
        new_record.measurement_id,
        observation_id='obs-retake',
        snr_db=35.0,
    )
    retake_report = build_measurement_quality_report(
        measurement=new_record,
        dataset=new_dataset,
        evidence=CadMeasurementQualityEvidence(
            snr_db=35.0,
            evidence_source='manual',
        ),
        profile=stricter_profile,
        observation=retake_observation,
        report_id='retake-report',
        created_at_utc='2026-09-19T00:07:00+00:00',
    )
    quality_repository.save_report(retake_report)
    lineage = build_measurement_lineage(
        document_id=revision.document_id,
        measurement_id=new_record.measurement_id,
        supersedes_measurement_id=old_record.measurement_id,
        selected_measurement_id=new_record.measurement_id,
        reason='explicit retake after quality review',
        lineage_id='retake-lineage',
        created_at_utc='2026-09-19T00:08:00+00:00',
    )
    quality_repository.save_lineage(lineage)

    newest_record, _newest_dataset = _save_measurement(
        measurement_repository,
        revision,
        'retake-2',
        raw=b'retake-2',
    )
    newest_lineage = build_measurement_lineage(
        document_id=revision.document_id,
        measurement_id=newest_record.measurement_id,
        supersedes_measurement_id=new_record.measurement_id,
        selected_measurement_id=newest_record.measurement_id,
        reason='second explicit retake',
        lineage_id='retake-lineage-2',
        created_at_utc='2026-09-19T00:09:00+00:00',
    )
    quality_repository.save_lineage(newest_lineage)

    reports = quality_repository.list_reports(old_record.measurement_id)
    assert reports == (old_report, recheck)
    assert quality_repository.get_report(old_report.report_id) == old_report
    assert old_report.profile.profile_sha256 != recheck.profile.profile_sha256
    assert old_report.noise_snr.status == 'PASS'
    assert recheck.noise_snr.status == 'FAIL'
    assert quality_repository.list_reports(new_record.measurement_id) == (retake_report,)
    assert quality_repository.list_lineage(revision.document_id) == (lineage, newest_lineage)
    assert quality_repository.selected_measurement_for_lineage(old_record.measurement_id) == newest_record.measurement_id
    assert quality_repository.selected_measurement_for_lineage(newest_record.measurement_id) == newest_record.measurement_id

    # Retake lineage is independent of preregistered O50/O60 calibration/holdout plans:
    # no measurement-plan or campaign row is created or rewritten as a side effect.
    assert measurement_repository.list_measurement_plans('not-a-search') == ()


def test_persisted_report_decisions_are_replayed_on_read(tmp_path: Path) -> None:
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    record, dataset = _save_measurement(
        measurement_repository,
        revision,
        'read-replay',
        raw=b'read-replay',
    )
    observation = _persist_observation(
        quality_repository,
        record.measurement_id,
        observation_id='obs-read-replay',
        clipping_detected=False,
        snr_db=30.0,
    )
    report = build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=CadMeasurementQualityEvidence(
            clipping_detected=False,
            snr_db=30.0,
            evidence_source='manual',
        ),
        profile=build_measurement_quality_profile(minimum_snr_db=20.0),
        observation=observation,
        report_id='report-read-replay',
        created_at_utc='2026-09-19T00:10:00+00:00',
    )
    quality_repository.save_report(report)
    assert quality_repository.get_report(report.report_id) == report
    assert quality_repository.list_reports(record.measurement_id) == (report,)
    assert quality_repository.latest_report(record.measurement_id) == report

    # A coherently rehashed payload can keep every binding/hash valid while
    # upgrading persisted checks, capability decisions and retake guidance;
    # reads must rerun the pinned algorithm instead of trusting the payload.
    tampered = _rehashed(report.model_copy(update={
        'clipping': CadMeasurementQualityCheck(
            status='FAIL',
            reason='forged clipping failure',
        ),
        'capabilities': tuple(
            item.model_copy(update={'decision': 'ALLOWED'})
            if item.claim == 'calibrated_response'
            else item
            for item in report.capabilities
        ),
        'retake_recommendation': 'NOT_NEEDED',
        'retake_reasons': (),
    }))
    _rewrite_report_row(quality_repository.path, tampered)

    with pytest.raises(ValueError, match='canonical quality algorithm output'):
        quality_repository.get_report(report.report_id)
    with pytest.raises(ValueError, match='canonical quality algorithm output'):
        quality_repository.list_reports(record.measurement_id)
    with pytest.raises(ValueError, match='canonical quality algorithm output'):
        quality_repository.latest_report(record.measurement_id)

    # Honest reports bound to other measurements remain readable.
    other_record, other_dataset = _save_measurement(
        measurement_repository,
        revision,
        'read-replay-honest',
        raw=b'read-replay-honest',
    )
    honest = build_measurement_quality_report(
        measurement=other_record,
        dataset=other_dataset,
        evidence=CadMeasurementQualityEvidence(),
        profile=build_measurement_quality_profile(),
        report_id='report-read-replay-honest',
        created_at_utc='2026-09-19T00:11:00+00:00',
    )
    quality_repository.save_report(honest)
    assert quality_repository.get_report(honest.report_id) == honest
    assert quality_repository.latest_report(other_record.measurement_id) == honest


def test_persisted_report_algorithm_identity_is_dispatched_on_read(tmp_path: Path) -> None:
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    record, dataset = _save_measurement(
        measurement_repository,
        revision,
        'algorithm-identity',
        raw=b'algorithm-identity',
    )
    report = build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=CadMeasurementQualityEvidence(),
        profile=build_measurement_quality_profile(),
        report_id='report-algorithm-identity',
        created_at_utc='2026-09-19T00:12:00+00:00',
    )
    quality_repository.save_report(report)

    # A valid historical report replays through explicit version support.
    assert replay_measurement_quality_report(
        report,
        measurement=record,
        dataset=dataset,
    ) == report

    unknown_version = _rehashed(report.model_copy(update={
        'algorithm_version': 'measurement-quality-0',
    }))
    _rewrite_report_row(quality_repository.path, unknown_version)
    with pytest.raises(ValueError, match='algorithm version is not replayable'):
        quality_repository.get_report(report.report_id)
    with pytest.raises(ValueError, match='algorithm version is not replayable'):
        quality_repository.latest_report(record.measurement_id)

    forged_algorithm_hash = _rehashed(report.model_copy(update={
        'algorithm_sha256': 'f' * 64,
    }))
    _rewrite_report_row(quality_repository.path, forged_algorithm_hash)
    with pytest.raises(ValueError, match='algorithm hash mismatch'):
        quality_repository.get_report(report.report_id)
    with pytest.raises(ValueError, match='algorithm hash mismatch'):
        quality_repository.list_reports(record.measurement_id)

    _rewrite_report_row(quality_repository.path, report)
    assert quality_repository.get_report(report.report_id) == report


def test_persisted_report_fails_closed_when_bound_evidence_is_deleted(tmp_path: Path) -> None:
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    first, first_dataset = _save_measurement(
        measurement_repository,
        revision,
        'repeat-source',
        raw=b'repeat-source',
    )
    record, dataset = _save_measurement(
        measurement_repository,
        revision,
        'repeat-target',
        raw=b'repeat-target',
    )
    report = build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=CadMeasurementQualityEvidence(
            repeat_measurement_ids=(first.measurement_id, record.measurement_id),
            repeatability_rms_db=measurement_repeatability_rms_db(
                (first_dataset, dataset)
            ),
        ),
        profile=build_measurement_quality_profile(maximum_repeatability_rms_db=1.0),
        report_id='report-deleted-evidence',
        created_at_utc='2026-09-19T00:13:00+00:00',
    )
    quality_repository.save_report(report)
    assert quality_repository.get_report(report.report_id) == report
    assert report.repeatability.status == 'PASS'
    assert report.capability('repeatability').decision == 'ALLOWED'

    # Deleting a bound repeat measurement fails closed: the persisted
    # repeatability capability can no longer be replayed against evidence.
    with closing(sqlite3.connect(quality_repository.path)) as connection, connection:
        connection.execute(
            'DELETE FROM cad_measurements WHERE measurement_id=?',
            (first.measurement_id,),
        )
    with pytest.raises(ValueError, match='unknown repeat measurement'):
        quality_repository.get_report(report.report_id)
    with pytest.raises(ValueError, match='unknown repeat measurement'):
        quality_repository.latest_report(record.measurement_id)

    # Deleting the bound dataset also fails closed on every authoritative read.
    with closing(sqlite3.connect(quality_repository.path)) as connection, connection:
        connection.execute(
            'DELETE FROM cad_frequency_responses WHERE dataset_id=?',
            (dataset.dataset_id,),
        )
    with pytest.raises(ValueError, match='unknown dataset'):
        quality_repository.get_report(report.report_id)
    with pytest.raises(ValueError, match='unknown dataset'):
        quality_repository.list_reports(record.measurement_id)

    # Deleting the bound measurement itself fails closed as well.
    with closing(sqlite3.connect(quality_repository.path)) as connection, connection:
        connection.execute(
            'DELETE FROM cad_measurements WHERE measurement_id=?',
            (record.measurement_id,),
        )
    with pytest.raises(ValueError, match='unknown measurement'):
        quality_repository.get_report(report.report_id)


def _retake(
    revision,
    *,
    measurement_id: str,
    supersedes_measurement_id: str,
    selected_measurement_id: str,
    lineage_id: str,
    created_at_utc: str,
    reason: str = 'explicit retake',
):
    return build_measurement_lineage(
        document_id=revision.document_id,
        measurement_id=measurement_id,
        supersedes_measurement_id=supersedes_measurement_id,
        selected_measurement_id=selected_measurement_id,
        reason=reason,
        lineage_id=lineage_id,
        created_at_utc=created_at_utc,
    )


def test_retake_lineage_rejects_stale_head_forks_and_cycles(tmp_path: Path) -> None:
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    first, _ = _save_measurement(
        measurement_repository, revision, 'chain-first', raw=b'chain-first'
    )
    second, _ = _save_measurement(
        measurement_repository, revision, 'chain-second', raw=b'chain-second'
    )
    third, _ = _save_measurement(
        measurement_repository, revision, 'chain-third', raw=b'chain-third'
    )
    fourth, _ = _save_measurement(
        measurement_repository, revision, 'chain-fourth', raw=b'chain-fourth'
    )

    ab = _retake(
        revision,
        measurement_id=second.measurement_id,
        supersedes_measurement_id=first.measurement_id,
        selected_measurement_id=second.measurement_id,
        lineage_id='retake-a-b',
        created_at_utc='2026-09-19T00:20:00+00:00',
    )
    quality_repository.save_lineage(ab)

    # A competing retake claiming the same superseded measurement is a
    # stale-head fork: first was already superseded by second.
    fork = _retake(
        revision,
        measurement_id=third.measurement_id,
        supersedes_measurement_id=first.measurement_id,
        selected_measurement_id=third.measurement_id,
        lineage_id='retake-a-c-fork',
        created_at_utc='2026-09-19T00:21:00+00:00',
        reason='competing retake of the same head',
    )
    with pytest.raises(MeasurementLineageConflictError, match='current lineage head'):
        quality_repository.save_lineage(fork)

    # Reusing the retake side as a new supersession edge back over its own
    # successor closes a cycle (first superseding second after second already
    # superseded first).
    cycle = _retake(
        revision,
        measurement_id=first.measurement_id,
        supersedes_measurement_id=second.measurement_id,
        selected_measurement_id=first.measurement_id,
        lineage_id='retake-b-a-cycle',
        created_at_utc='2026-09-19T00:22:00+00:00',
        reason='cycle back over the retake',
    )
    with pytest.raises(MeasurementLineageConflictError, match='cycle'):
        quality_repository.save_lineage(cycle)

    # A measurement that already superseded a predecessor cannot claim a
    # second one; that would merge two chains into one node.
    merge = _retake(
        revision,
        measurement_id=second.measurement_id,
        supersedes_measurement_id=fourth.measurement_id,
        selected_measurement_id=second.measurement_id,
        lineage_id='retake-d-b-merge',
        created_at_utc='2026-09-19T00:23:00+00:00',
        reason='second predecessor claim',
    )
    with pytest.raises(MeasurementLineageConflictError, match='at most one predecessor'):
        quality_repository.save_lineage(merge)

    # Extending the current head stays valid: first -> second -> third.
    bc = _retake(
        revision,
        measurement_id=third.measurement_id,
        supersedes_measurement_id=second.measurement_id,
        selected_measurement_id=third.measurement_id,
        lineage_id='retake-b-c',
        created_at_utc='2026-09-19T00:24:00+00:00',
    )
    quality_repository.save_lineage(bc)
    assert quality_repository.list_lineage(revision.document_id) == (ab, bc)
    assert (
        quality_repository.selected_measurement_for_lineage(first.measurement_id)
        == third.measurement_id
    )

    # The validated chain and its selection reopen unchanged.
    reopened = CadMeasurementQualityRepository(measurement_repository)
    assert reopened.list_lineage(revision.document_id) == (ab, bc)
    assert (
        reopened.selected_measurement_for_lineage(first.measurement_id)
        == third.measurement_id
    )
    assert (
        reopened.selected_measurement_for_lineage(fourth.measurement_id)
        == fourth.measurement_id
    )


def test_retake_lineage_concurrent_supersession_single_winner(tmp_path: Path) -> None:
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    base, _ = _save_measurement(
        measurement_repository, revision, 'race-base', raw=b'race-base'
    )
    contender_a, _ = _save_measurement(
        measurement_repository, revision, 'race-a', raw=b'race-a'
    )
    contender_b, _ = _save_measurement(
        measurement_repository, revision, 'race-b', raw=b'race-b'
    )
    retake_a = _retake(
        revision,
        measurement_id=contender_a.measurement_id,
        supersedes_measurement_id=base.measurement_id,
        selected_measurement_id=contender_a.measurement_id,
        lineage_id='race-retake-a',
        created_at_utc='2026-09-19T00:30:00+00:00',
    )
    retake_b = _retake(
        revision,
        measurement_id=contender_b.measurement_id,
        supersedes_measurement_id=base.measurement_id,
        selected_measurement_id=contender_b.measurement_id,
        lineage_id='race-retake-b',
        created_at_utc='2026-09-19T00:30:01+00:00',
    )

    barrier = threading.Barrier(2)
    outcomes: dict[str, str] = {}

    def attempt(key, record) -> None:
        barrier.wait(timeout=10)
        try:
            quality_repository.save_lineage(record)
            outcomes[key] = 'saved'
        except MeasurementLineageConflictError:
            outcomes[key] = 'conflict'

    threads = (
        threading.Thread(target=attempt, args=('a', retake_a)),
        threading.Thread(target=attempt, args=('b', retake_b)),
    )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    # Two retakes of one current head cannot both become authoritative.
    assert sorted(outcomes.values()) == ['conflict', 'saved']
    winner = contender_a if outcomes['a'] == 'saved' else contender_b
    assert (
        quality_repository.selected_measurement_for_lineage(base.measurement_id)
        == winner.measurement_id
    )


def test_selected_measurement_resolves_from_validated_chain_head(tmp_path: Path) -> None:
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    first, _ = _save_measurement(
        measurement_repository, revision, 'select-first', raw=b'select-first'
    )
    second, _ = _save_measurement(
        measurement_repository, revision, 'select-second', raw=b'select-second'
    )
    third, _ = _save_measurement(
        measurement_repository, revision, 'select-third', raw=b'select-third'
    )

    ab = _retake(
        revision,
        measurement_id=second.measurement_id,
        supersedes_measurement_id=first.measurement_id,
        selected_measurement_id=second.measurement_id,
        lineage_id='select-a-b',
        created_at_utc='2026-09-19T00:40:00+00:00',
    )
    quality_repository.save_lineage(ab)
    # The head record may deliberately keep the superseded side selected;
    # selection is a per-node decision on the chain, not an ordering artifact.
    bc = _retake(
        revision,
        measurement_id=third.measurement_id,
        supersedes_measurement_id=second.measurement_id,
        selected_measurement_id=second.measurement_id,
        lineage_id='select-b-c',
        created_at_utc='2026-09-19T00:41:00+00:00',
        reason='retake taken but prior evidence kept selected',
    )
    quality_repository.save_lineage(bc)

    for member in (first, second, third):
        assert (
            quality_repository.selected_measurement_for_lineage(member.measurement_id)
            == second.measurement_id
        )


def test_selected_measurement_is_topology_derived_not_insertion_order(tmp_path: Path) -> None:
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    first, _ = _save_measurement(
        measurement_repository, revision, 'order-first', raw=b'order-first'
    )
    second, _ = _save_measurement(
        measurement_repository, revision, 'order-second', raw=b'order-second'
    )
    third, _ = _save_measurement(
        measurement_repository, revision, 'order-third', raw=b'order-third'
    )

    # Persist a topologically valid first -> second -> third chain whose rows
    # sit in the table in non-chain order (only reachable past save_lineage,
    # e.g. a restored/imported database). Selection must follow the chain
    # head's record, never the last row by seq.
    head_edge = _retake(
        revision,
        measurement_id=third.measurement_id,
        supersedes_measurement_id=second.measurement_id,
        selected_measurement_id=third.measurement_id,
        lineage_id='order-b-c',
        created_at_utc='2026-09-19T00:50:00+00:00',
    )
    root_edge = _retake(
        revision,
        measurement_id=second.measurement_id,
        supersedes_measurement_id=first.measurement_id,
        selected_measurement_id=first.measurement_id,
        lineage_id='order-a-b',
        created_at_utc='2026-09-19T00:49:00+00:00',
    )
    _insert_lineage_row(quality_repository.path, head_edge)
    _insert_lineage_row(quality_repository.path, root_edge)

    assert quality_repository.list_lineage(revision.document_id) == (head_edge, root_edge)
    for member in (first, second, third):
        assert (
            quality_repository.selected_measurement_for_lineage(member.measurement_id)
            == third.measurement_id
        )


def test_persisted_lineage_corruption_surfaces_on_reads(tmp_path: Path) -> None:
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    first, _ = _save_measurement(
        measurement_repository, revision, 'corrupt-first', raw=b'corrupt-first'
    )
    second, _ = _save_measurement(
        measurement_repository, revision, 'corrupt-second', raw=b'corrupt-second'
    )
    third, _ = _save_measurement(
        measurement_repository, revision, 'corrupt-third', raw=b'corrupt-third'
    )

    honest = _retake(
        revision,
        measurement_id=second.measurement_id,
        supersedes_measurement_id=first.measurement_id,
        selected_measurement_id=second.measurement_id,
        lineage_id='corrupt-a-b',
        created_at_utc='2026-09-19T01:00:00+00:00',
    )
    quality_repository.save_lineage(honest)

    # A fork persisted before the contract existed (or injected past
    # save_lineage) is detected instead of silently winning by insert order.
    fork = _retake(
        revision,
        measurement_id=third.measurement_id,
        supersedes_measurement_id=first.measurement_id,
        selected_measurement_id=third.measurement_id,
        lineage_id='corrupt-a-c',
        created_at_utc='2026-09-19T01:01:00+00:00',
    )
    _insert_lineage_row(quality_repository.path, fork)
    with pytest.raises(ValueError, match='superseded more than once'):
        quality_repository.list_lineage(revision.document_id)
    with pytest.raises(ValueError, match='superseded more than once'):
        quality_repository.selected_measurement_for_lineage(first.measurement_id)
    _delete_lineage_row(quality_repository.path, fork.lineage_id)

    # A persisted cycle is surfaced the same way.
    cycle = _retake(
        revision,
        measurement_id=first.measurement_id,
        supersedes_measurement_id=second.measurement_id,
        selected_measurement_id=first.measurement_id,
        lineage_id='corrupt-b-a',
        created_at_utc='2026-09-19T01:02:00+00:00',
    )
    _insert_lineage_row(quality_repository.path, cycle)
    with pytest.raises(ValueError, match='cycle'):
        quality_repository.list_lineage(revision.document_id)
    with pytest.raises(ValueError, match='cycle'):
        quality_repository.selected_measurement_for_lineage(first.measurement_id)
    _delete_lineage_row(quality_repository.path, cycle.lineage_id)

    # A stored column that disagrees with its payload fails closed.
    with closing(sqlite3.connect(quality_repository.path)) as connection, connection:
        connection.execute(
            'UPDATE cad_measurement_lineage SET selected_measurement_id=? WHERE lineage_id=?',
            (first.measurement_id, honest.lineage_id),
        )
    with pytest.raises(ValueError, match='disagrees with its payload'):
        quality_repository.list_lineage(revision.document_id)
    with closing(sqlite3.connect(quality_repository.path)) as connection, connection:
        connection.execute(
            'UPDATE cad_measurement_lineage SET selected_measurement_id=? WHERE lineage_id=?',
            (honest.selected_measurement_id, honest.lineage_id),
        )

    # A lineage row whose bound measurement disappeared is dead evidence.
    with closing(sqlite3.connect(quality_repository.path)) as connection, connection:
        connection.execute(
            'DELETE FROM cad_measurements WHERE measurement_id=?',
            (first.measurement_id,),
        )
    with pytest.raises(ValueError, match='unknown measurement evidence'):
        quality_repository.list_lineage(revision.document_id)

    # Honest chains in a clean repository still resolve normally.
    _delete_lineage_row(quality_repository.path, honest.lineage_id)
    assert quality_repository.list_lineage(revision.document_id) == ()




def test_measurement_retake_guidance_maps_missing_evidence_and_remeasure(tmp_path: Path) -> None:
    """#468: retake guidance names the missing acquisition context/evidence."""
    revision, measurement_repository, _ = _repositories(tmp_path)
    record, dataset = _save_measurement(
        measurement_repository, revision, 'm-guidance', raw=b'guidance-raw'
    )

    # No evidence supplied at all: nothing fails, but every acquisition input
    # is honestly listed as missing and the recommendation stays UNKNOWN.
    empty = build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=CadMeasurementQualityEvidence(),
        profile=build_measurement_quality_profile(),
    )
    guidance = measurement_retake_guidance(empty)
    assert guidance.recommendation == 'UNKNOWN'
    assert guidance.failed_checks == ()
    assert 'clipping' in guidance.unknown_checks
    assert 'repeatability' in guidance.not_evaluated_checks
    assert guidance.missing_evidence == (
        'clipping_metadata',
        'snr_evidence',
        'usable_band_evidence',
        'timing_reference_evidence',
        'polarity_evidence',
        'impulse_response',
        'calibration_provenance',
        'repeat_measurements',
        'acquisition_context',
    )
    assert 'same_binding' not in guidance.remeasure
    assert 'timed_acquisition' in guidance.remeasure
    assert 'calibrated_microphone' in guidance.remeasure
    assert 'repeat_measurement' in guidance.remeasure
    assert 'impulse_response_capture' in guidance.remeasure
    assert 'acquisition_metadata' in guidance.remeasure

    # Fully provisioned evidence under a thresholded profile: nothing missing,
    # no re-measure action, recommendation NOT_NEEDED.
    calibration_sha = sha256(b'mic-calibration').hexdigest()
    complete = build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=CadMeasurementQualityEvidence(
            clipping_detected=False,
            peak_dbfs=-3.0,
            snr_db=42.0,
            usable_frequency_band_hz=(10.0, 22000.0),
            timing_reference_valid=True,
            timing_reference_id='loopback-1',
            clock_source='umik-usb',
            sample_rate_hz=48000,
            delay_correction_s=0.00025,
            polarity_correct=True,
            polarity_confidence=0.95,
            has_impulse_response=True,
            ir_window_start_s=0.0,
            ir_window_end_s=0.5,
            ir_truncated=False,
            calibration_file_sha256=calibration_sha,
            expected_calibration_file_sha256=calibration_sha,
            repeat_measurement_ids=('repeat-a', 'repeat-b'),
            repeatability_rms_db=0.4,
        ),
        profile=build_measurement_quality_profile(
            minimum_snr_db=20.0,
            required_usable_band_hz=(20.0, 20000.0),
            minimum_polarity_confidence=0.5,
            maximum_repeatability_rms_db=1.0,
        ),
        acquisition_context=CadAcquisitionContextBinding(
            acquisition_context_id='acq-1',
            acquisition_context_sha256=sha256(b'acq-1').hexdigest(),
            source_kind='native',
        ),
    )
    guidance = measurement_retake_guidance(complete)
    assert guidance.recommendation == 'NOT_NEEDED'
    assert guidance.missing_evidence == ()
    assert guidance.remeasure == ()
    assert guidance.failed_checks == ()
    assert guidance.unknown_checks == ()


def _timed_report(
    record,
    dataset,
    *,
    acquisition,
    evidence_overrides: dict | None = None,
    report_id: str = 'report-timed',
) -> CadMeasurementQualityReport:
    """Report claiming full timing provenance through a context binding."""
    fields = {
        'timing_reference_valid': True,
        'timing_reference_id': 'loopback-1',
        'clock_source': 'umik-usb',
        'sample_rate_hz': 48000,
        'delay_correction_s': 0.00025,
    }
    if evidence_overrides:
        fields.update(evidence_overrides)
    return build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=CadMeasurementQualityEvidence(**fields),
        profile=build_measurement_quality_profile(),
        acquisition_context=acquisition,
        report_id=report_id,
        created_at_utc='2026-09-19T02:00:00+00:00',
    )


def test_fabricated_acquisition_context_cannot_unlock_common_timing(
    tmp_path: Path,
) -> None:
    """#392: a binding's id/hash/source_kind string is never proof of existence."""
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    record, dataset = _save_measurement(
        measurement_repository, revision, 'fab-context', raw=b'fab-context'
    )
    fabricated = CadAcquisitionContextBinding(
        acquisition_context_id='acq-invented',
        acquisition_context_sha256=sha256(b'acq-invented').hexdigest(),
        source_kind='manual',
    )
    report = _timed_report(record, dataset, acquisition=fabricated)
    # The pure builder still trusts the declared binding — the vulnerability
    # the repository boundary now closes.
    assert report.timing_reference.status == 'PASS'
    assert report.capability('common_timing').decision == 'ALLOWED'
    with pytest.raises(ValueError, match='unknown acquisition context'):
        quality_repository.save_report(report)

    # A persisted context must be bound by exact content hash and source
    # kind: neither a forged hash nor a provenance upgrade is accepted.
    real = build_acquisition_context(
        acquisition_context_id='acq-real',
        source_kind='manual',
        subject_measurement_ids=(record.measurement_id,),
        timing_reference_valid=True,
        timing_reference_id='loopback-1',
        clock_source='umik-usb',
        sample_rate_hz=48000,
        delay_correction_s=0.00025,
        created_at_utc='2026-09-19T01:00:00+00:00',
    )
    quality_repository.save_acquisition_context(real)

    forged_hash = CadAcquisitionContextBinding(
        acquisition_context_id=real.acquisition_context_id,
        acquisition_context_sha256=sha256(b'forged').hexdigest(),
        source_kind='manual',
    )
    with pytest.raises(ValueError, match='acquisition context hash mismatch'):
        quality_repository.save_report(
            _timed_report(
                record, dataset, acquisition=forged_hash, report_id='r-forged-hash'
            )
        )

    upgraded = CadAcquisitionContextBinding(
        acquisition_context_id=real.acquisition_context_id,
        acquisition_context_sha256=real.acquisition_context_sha256,
        source_kind='native',
    )
    with pytest.raises(ValueError, match='acquisition context source kind mismatch'):
        quality_repository.save_report(
            _timed_report(record, dataset, acquisition=upgraded, report_id='r-upgraded')
        )

    # Honestly bound to the persisted manual context: resolves, replays and
    # unlocks common timing.
    honest = _timed_report(
        record,
        dataset,
        acquisition=acquisition_context_binding(real),
        report_id='r-honest',
    )
    quality_repository.save_report(honest)
    assert honest.capability('common_timing').decision == 'ALLOWED'
    assert quality_repository.get_report(honest.report_id) == honest


def test_timing_evidence_from_another_measurement_or_session_is_rejected(
    tmp_path: Path,
) -> None:
    """#392: context must cover the report measurement; evidence must equal it."""
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    other, _ = _save_measurement(
        measurement_repository, revision, 'session-a', raw=b'session-a'
    )
    record, dataset = _save_measurement(
        measurement_repository, revision, 'session-b', raw=b'session-b'
    )

    # Timing evidence without any bound context is unattributable.
    unbound = _timed_report(record, dataset, acquisition=None, report_id='r-unbound')
    with pytest.raises(ValueError, match='requires a bound acquisition context'):
        quality_repository.save_report(unbound)

    # A real context that only covers another measurement/session must not
    # lend this measurement its timing authority.
    foreign = build_acquisition_context(
        acquisition_context_id='acq-foreign-session',
        source_kind='native',
        subject_measurement_ids=(other.measurement_id,),
        timing_reference_valid=True,
        timing_reference_id='loopback-1',
        clock_source='umik-usb',
        sample_rate_hz=48000,
        delay_correction_s=0.00025,
        created_at_utc='2026-09-19T01:10:00+00:00',
    )
    quality_repository.save_acquisition_context(foreign)
    with pytest.raises(ValueError, match='does not cover the report measurement'):
        quality_repository.save_report(
            _timed_report(
                record,
                dataset,
                acquisition=acquisition_context_binding(foreign),
                report_id='r-foreign',
            )
        )

    # Same-session context but divergent claimed timing values fail closed.
    session_binding = _persist_context(
        quality_repository,
        context_id='acq-session-b',
        subject_ids=(record.measurement_id,),
        source_kind='native',
        timing_reference_valid=True,
        timing_reference_id='loopback-1',
        clock_source='umik-usb',
        sample_rate_hz=48000,
        delay_correction_s=0.00025,
    )
    divergent = _timed_report(
        record,
        dataset,
        acquisition=session_binding,
        report_id='r-divergent',
        evidence_overrides={'sample_rate_hz': 44100},
    )
    with pytest.raises(ValueError, match='timing evidence field sample_rate_hz diverges'):
        quality_repository.save_report(divergent)


def test_repeatability_rms_is_recomputed_from_exact_datasets(tmp_path: Path) -> None:
    """#392: real repeat ids cannot launder an arbitrary low RMS claim."""
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    first, first_dataset = _save_measurement(
        measurement_repository,
        revision,
        'rms-a',
        raw=b'rms-a',
        level_db=(74.0, 75.0, 73.0),
    )
    record, dataset = _save_measurement(
        measurement_repository,
        revision,
        'rms-b',
        raw=b'rms-b',
    )
    canonical = measurement_repeatability_rms_db((first_dataset, dataset))
    assert canonical == 2.0

    understated = build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=CadMeasurementQualityEvidence(
            repeat_measurement_ids=(first.measurement_id, record.measurement_id),
            repeatability_rms_db=0.01,
        ),
        profile=build_measurement_quality_profile(maximum_repeatability_rms_db=1.0),
        report_id='report-rms-understated',
        created_at_utc='2026-09-19T02:10:00+00:00',
    )
    # The payload claims an artificial PASS; the bound datasets disagree.
    assert understated.repeatability.status == 'PASS'
    with pytest.raises(ValueError, match='diverges from the exact repeat datasets'):
        quality_repository.save_report(understated)

    # Claiming no metric while two repeat datasets resolve is also a
    # divergence: the claim is derivable and must be reproduced.
    missing = build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=CadMeasurementQualityEvidence(
            repeat_measurement_ids=(first.measurement_id, record.measurement_id),
        ),
        profile=build_measurement_quality_profile(maximum_repeatability_rms_db=1.0),
        report_id='report-rms-missing',
        created_at_utc='2026-09-19T02:11:00+00:00',
    )
    with pytest.raises(ValueError, match='diverges from the exact repeat datasets'):
        quality_repository.save_report(missing)

    # A single repeat id cannot support a claimed metric.
    orphan_metric = build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=CadMeasurementQualityEvidence(
            repeat_measurement_ids=(first.measurement_id,),
            repeatability_rms_db=0.01,
        ),
        profile=build_measurement_quality_profile(maximum_repeatability_rms_db=1.0),
        report_id='report-rms-orphan',
        created_at_utc='2026-09-19T02:12:00+00:00',
    )
    with pytest.raises(ValueError, match='at least two repeat measurements'):
        quality_repository.save_report(orphan_metric)

    # Repeat datasets on divergent grids cannot produce the canonical metric.
    odd_grid_raw = declared_fr_raw(
        frequency_hz=(25.0, 50.0, 100.0),
        level_db=(70.0, 71.0, 69.0),
        phase_status='absent',
        processing={'fixture_raw': 'rms-odd-grid'},
    )
    odd_record = measurement_record_for_revision(
        revision,
        'point-mlp',
        measurement_id='rms-odd-grid',
        evidence_type='measured',
        channel_role='front_left',
        source_speaker_ids=('speaker-fl',),
        radiation_scope='single',
        routing_evidence='verified',
        imported_at='2026-09-19T00:00:00+00:00',
        source_kind='unknown',
        external_source_id='rew-rms-odd-grid',
    )
    odd_dataset = CadFrequencyResponseDataset(
        dataset_id='dataset-rms-odd-grid',
        measurement_id='rms-odd-grid',
        frequency_hz=(25.0, 50.0, 100.0),
        level_db=(70.0, 71.0, 69.0),
        phase_status='absent',
        processing_json=canonical_json({'fixture_raw': 'rms-odd-grid'}),
        source_sha256=sha256(odd_grid_raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    measurement_repository.save(
        odd_record,
        odd_dataset,
        raw_filename='rms-odd-grid.json',
        raw_bytes=odd_grid_raw,
    )
    mixed_grid = build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=CadMeasurementQualityEvidence(
            repeat_measurement_ids=(first.measurement_id, odd_record.measurement_id),
            repeatability_rms_db=0.5,
        ),
        profile=build_measurement_quality_profile(maximum_repeatability_rms_db=1.0),
        report_id='report-rms-mixed-grid',
        created_at_utc='2026-09-19T02:13:00+00:00',
    )
    with pytest.raises(ValueError, match='identical frequency grid'):
        quality_repository.save_report(mixed_grid)

    # The exact canonical value persists and replays unchanged.
    honest = build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=CadMeasurementQualityEvidence(
            repeat_measurement_ids=(first.measurement_id, record.measurement_id),
            repeatability_rms_db=canonical,
        ),
        profile=build_measurement_quality_profile(maximum_repeatability_rms_db=3.0),
        report_id='report-rms-honest',
        created_at_utc='2026-09-19T02:14:00+00:00',
    )
    quality_repository.save_report(honest)
    assert honest.repeatability.status == 'PASS'
    assert quality_repository.get_report(honest.report_id) == honest


def test_calibration_pass_requires_retained_calibration_authority(
    tmp_path: Path,
) -> None:
    """#392: bare matching hashes cannot pass calibration without retained files."""
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    record, dataset = _save_measurement(
        measurement_repository, revision, 'cal-auth', raw=b'cal-auth'
    )
    calibration_sha = sha256(b'umik-calibration').hexdigest()
    report = build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=CadMeasurementQualityEvidence(
            calibration_filename='umik.txt',
            calibration_file_sha256=calibration_sha,
            expected_calibration_file_sha256=calibration_sha,
        ),
        profile=build_measurement_quality_profile(),
        report_id='report-cal-unretained',
        created_at_utc='2026-09-19T02:20:00+00:00',
    )
    assert report.calibration.status == 'PASS'
    with pytest.raises(ManagedAssetError, match='no retained authority row'):
        quality_repository.save_report(report)

    # Once the exact file is retained, the same claim resolves and replays.
    retained = quality_repository.save_calibration_file(
        filename='umik.txt', raw_bytes=b'umik-calibration'
    )
    assert retained == calibration_sha
    quality_repository.save_report(report)
    assert quality_repository.get_report(report.report_id) == report

    # The retained filename is part of the authority: a mismatched
    # descriptive filename is rejected rather than silently descriptive.
    renamed = _rehashed(
        report.model_copy(
            update={
                'report_id': 'report-cal-renamed',
                'evidence': report.evidence.model_copy(
                    update={'calibration_filename': 'other-name.txt'}
                ),
            }
        )
    )
    with pytest.raises(ValueError, match='calibration filename does not match'):
        quality_repository.save_report(renamed)


def test_observation_authority_is_required_for_acquisition_metadata(
    tmp_path: Path,
) -> None:
    """#392: per-measurement metadata requires a bound observation authority."""
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    other, _ = _save_measurement(
        measurement_repository, revision, 'obs-other', raw=b'obs-other'
    )
    record, dataset = _save_measurement(
        measurement_repository, revision, 'obs-subject', raw=b'obs-subject'
    )

    # Non-default observation fields without a binding are rejected.
    unbound = build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=CadMeasurementQualityEvidence(snr_db=40.0),
        profile=build_measurement_quality_profile(minimum_snr_db=20.0),
        report_id='report-obs-unbound',
        created_at_utc='2026-09-19T02:30:00+00:00',
    )
    with pytest.raises(ValueError, match='requires a bound measurement observation'):
        quality_repository.save_report(unbound)

    # A fabricated id/hash pair cannot stand in for a persisted observation.
    fabricated = build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=CadMeasurementQualityEvidence(snr_db=40.0, evidence_source='manual'),
        profile=build_measurement_quality_profile(minimum_snr_db=20.0),
        observation=CadMeasurementObservationBinding(
            observation_id='obs-invented',
            observation_sha256=sha256(b'obs-invented').hexdigest(),
        ),
        report_id='report-obs-invented',
        created_at_utc='2026-09-19T02:31:00+00:00',
    )
    with pytest.raises(ValueError, match='unknown measurement observation'):
        quality_repository.save_report(fabricated)

    # An observation for another measurement is not this subject's authority.
    foreign_binding = _persist_observation(
        quality_repository,
        other.measurement_id,
        observation_id='obs-foreign',
        source_kind='manual',
        snr_db=40.0,
    )
    mismatched = build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=CadMeasurementQualityEvidence(snr_db=40.0, evidence_source='manual'),
        profile=build_measurement_quality_profile(minimum_snr_db=20.0),
        observation=foreign_binding,
        report_id='report-obs-foreign',
        created_at_utc='2026-09-19T02:33:00+00:00',
    )
    with pytest.raises(ValueError, match='subject does not match'):
        quality_repository.save_report(mismatched)

    # Divergent claimed values fail closed even with an honestly bound
    # observation on the right subject.
    subject_binding = _persist_observation(
        quality_repository,
        record.measurement_id,
        observation_id='obs-subject-1',
        source_kind='manual',
        snr_db=40.0,
    )
    divergent = build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=CadMeasurementQualityEvidence(snr_db=55.0, evidence_source='manual'),
        profile=build_measurement_quality_profile(minimum_snr_db=20.0),
        observation=subject_binding,
        report_id='report-obs-divergent',
        created_at_utc='2026-09-19T02:35:00+00:00',
    )
    with pytest.raises(ValueError, match='observation evidence field snr_db diverges'):
        quality_repository.save_report(divergent)

    # Machine-derived observations pin the subject's exact raw asset: a pin
    # to foreign bytes is rejected when the observation is persisted.
    with pytest.raises(ValueError, match='must pin the subject'):
        quality_repository.save_observation(
            build_measurement_observation(
                observation_id='obs-wrong-asset',
                measurement_id=record.measurement_id,
                source_kind='rew_metadata',
                source_asset_sha256=sha256(b'foreign-bytes').hexdigest(),
                observed_at_utc='2026-09-19T02:36:00+00:00',
                snr_db=40.0,
            )
        )
    machine = build_measurement_observation(
        observation_id='obs-machine',
        measurement_id=record.measurement_id,
        source_kind='rew_metadata',
        source_asset_sha256=dataset.source_sha256,
        observed_at_utc='2026-09-19T02:37:00+00:00',
        snr_db=40.0,
    )
    quality_repository.save_observation(machine)

    # The free-form label must agree with the resolved typed authority.
    mislabeled = build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=CadMeasurementQualityEvidence(
            snr_db=40.0,
            evidence_source='rew_metadata',
        ),
        profile=build_measurement_quality_profile(minimum_snr_db=20.0),
        observation=subject_binding,
        report_id='report-obs-mislabeled',
        created_at_utc='2026-09-19T02:38:00+00:00',
    )
    with pytest.raises(ValueError, match='evidence_source diverges'):
        quality_repository.save_report(mislabeled)

    honest = build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=CadMeasurementQualityEvidence(
            snr_db=40.0,
            evidence_source='rew_metadata',
        ),
        profile=build_measurement_quality_profile(minimum_snr_db=20.0),
        observation=observation_binding(machine),
        report_id='report-obs-honest',
        created_at_utc='2026-09-19T02:39:00+00:00',
    )
    quality_repository.save_report(honest)
    assert honest.noise_snr.status == 'PASS'
    assert quality_repository.get_report(honest.report_id) == honest

    # Tampering with a persisted report's observation binding fails closed on
    # read: the payload no longer matches the resolved authority.
    tampered = _rehashed(
        honest.model_copy(
            update={
                'observation': CadMeasurementObservationBinding(
                    observation_id=machine.observation_id,
                    observation_sha256=sha256(b'tampered').hexdigest(),
                )
            }
        )
    )
    _rewrite_report_row(quality_repository.path, tampered)
    with pytest.raises(ValueError, match='observation hash mismatch'):
        quality_repository.get_report(honest.report_id)

