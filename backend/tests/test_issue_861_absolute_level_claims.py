"""#861: absolute-SPL capability is a separate, calibrated authority.

The v2 quality algorithm splits the old ``calibrated_response`` claim into
the explicit ``frequency_response_corrected`` claim (mic response correction
provenance, unchanged semantics — ``calibrated_response`` stays its legacy
alias) and the independent ``absolute_spl`` / ``absolute_noise_level`` claims
that resolve the persisted ``CadDatasetLevelReference`` +
``CadAcousticLevelCalibration`` authorities.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from htdt.cad_measurement_authorities import (
    build_acoustic_level_calibration,
    build_dataset_level_reference,
)
from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurement_quality import (
    CadAcquisitionContextBinding,
    CadDatasetLevelReferenceBinding,
    CadMeasurementObservation,
    CadMeasurementQualityEvidence,
    _hash,
    acquisition_context_binding,
    build_acquisition_context,
    build_measurement_observation,
    build_measurement_quality_profile,
    build_measurement_quality_report,
    build_measurement_quality_report_v1,
    gate_measurement_claim,
    observation_binding,
)
from htdt.cad_measurement_quality_repository import (
    CadMeasurementQualityRepository,
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
from htdt.cad_measurement_quality import CadMicrophoneCapture


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
    level_reference: str = 'unknown',
):
    processing = {'fixture_raw': measurement_id}
    raw = declared_fr_raw(
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(70.0, 71.0, 69.0),
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
        source_kind='unknown',
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id=f'dataset-{measurement_id}',
        measurement_id=measurement_id,
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(70.0, 71.0, 69.0),
        phase_status='absent',
        level_reference=level_reference,
        processing_json=canonical_json(processing),
        source_sha256=sha256(raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    repository.save(
        record, dataset, raw_filename=f'{measurement_id}.json', raw_bytes=raw
    )
    return record, dataset


def _context(
    quality_repository: CadMeasurementQualityRepository,
    measurement_id: str,
    *,
    context_id: str = 'acq-1',
    microphone: CadMicrophoneCapture | None = None,
):
    context = build_acquisition_context(
        acquisition_context_id=context_id,
        source_kind='native',
        subject_measurement_ids=(measurement_id,),
        microphone=microphone,
        created_at_utc='2026-09-19T00:00:00+00:00',
    )
    quality_repository.save_acquisition_context(context)
    return context


def _persist_calibration(
    quality_repository: CadMeasurementQualityRepository,
    *,
    method: str = 'acoustic_calibrator',
    validity_scope: str = 'measurement',
    instrument_identity: str | None = None,
):
    calibration = build_acoustic_level_calibration(
        method=method,
        instrument_identity=instrument_identity,
        reference_level_db_spl=94.0,
        reference_frequency_hz=1000.0,
        calibrated_at_utc='2026-09-18T00:00:00+00:00',
        validity_scope=validity_scope,
    )
    quality_repository.save_level_calibration(calibration)
    return calibration


def _persist_reference(
    quality_repository: CadMeasurementQualityRepository,
    record,
    dataset,
    calibration,
    *,
    kind: str = 'absolute_spl',
):
    reference = build_dataset_level_reference(
        measurement_id=record.measurement_id,
        dataset_id=dataset.dataset_id,
        dataset_sha256=dataset.dataset_sha256,
        level_reference_kind=kind,
        calibration_id=None if calibration is None else calibration.calibration_id,
        calibration_sha256=(
            None if calibration is None else calibration.calibration_sha256
        ),
    )
    quality_repository.save_dataset_level_reference(reference)
    return reference


def _persist_observation(
    quality_repository: CadMeasurementQualityRepository,
    measurement_id: str,
    **fields,
) -> CadMeasurementObservation:
    observation = build_measurement_observation(
        observation_id=f'obs-{measurement_id}',
        measurement_id=measurement_id,
        source_kind='manual',
        observed_at_utc='2026-09-19T00:00:30+00:00',
        **fields,
    )
    quality_repository.save_observation(observation)
    return observation


def _report(
    *,
    record,
    dataset,
    context=None,
    level_reference=None,
    level_calibration=None,
    observation=None,
    evidence_fields: dict | None = None,
):
    binding: CadAcquisitionContextBinding | None = (
        None if context is None else acquisition_context_binding(context)
    )
    fields = dict(evidence_fields or {})
    if observation is not None:
        # The report's loose evidence must mirror the bound observation's
        # attested fields verbatim (the repository enforces this on save).
        fields['evidence_source'] = observation.source_kind
    return build_measurement_quality_report(
        measurement=record,
        dataset=dataset,
        evidence=CadMeasurementQualityEvidence(**fields),
        profile=build_measurement_quality_profile(
            required_usable_band_hz=(20.0, 80.0),
            minimum_snr_db=20.0,
        ),
        acquisition_context=binding,
        observation=None if observation is None else observation_binding(observation),
        observation_record=observation,
        dataset_level_reference=level_reference,
        level_calibration=level_calibration,
        acquisition_context_record=context,
        created_at_utc='2026-09-19T00:01:00+00:00',
    )


def test_mic_correction_alone_never_authorizes_absolute_spl(tmp_path: Path) -> None:
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    record, dataset = _save_measurement(measurement_repository, revision, 'm-1')
    context = _context(quality_repository, record.measurement_id)
    calibration_sha = quality_repository.save_calibration_file(
        filename='umik.txt', raw_bytes=b'umik-correction'
    )

    report = _report(
        record=record,
        dataset=dataset,
        context=context,
        evidence_fields={
            'clipping_detected': False,
            'snr_db': 40.0,
            'usable_frequency_band_hz': (20.0, 80.0),
            'calibration_file_sha256': calibration_sha,
            'expected_calibration_file_sha256': calibration_sha,
        },
    )

    # The mic response-correction file authorizes the *corrected response*
    # claims only — never absolute level semantics.
    assert report.capability('frequency_response_corrected').decision == 'ALLOWED'
    assert report.capability('calibrated_response').decision == 'ALLOWED'
    assert report.capability('absolute_spl').decision == 'UNKNOWN'
    assert report.capability('absolute_noise_level').decision == 'UNKNOWN'
    assert (
        report.capability('frequency_response_corrected').decision
        == report.capability('calibrated_response').decision
    )


def test_exact_spl_calibration_and_reference_allow_absolute_spl(tmp_path: Path) -> None:
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    record, dataset = _save_measurement(measurement_repository, revision, 'm-1')
    context = _context(quality_repository, record.measurement_id)
    calibration = _persist_calibration(quality_repository)
    reference = _persist_reference(
        quality_repository, record, dataset, calibration
    )

    observation = _persist_observation(
        quality_repository, record.measurement_id, noise_floor_db_spl=30.0
    )
    report = _report(
        record=record,
        dataset=dataset,
        context=context,
        level_reference=reference,
        level_calibration=calibration,
        observation=observation,
        evidence_fields={'noise_floor_db_spl': 30.0},
    )
    quality_repository.save_report(report)

    assert report.level_reference is not None
    assert report.capability('absolute_spl').decision == 'ALLOWED'
    assert report.capability('absolute_noise_level').decision == 'ALLOWED'
    assert quality_repository.get_report(report.report_id) == report


def test_non_absolute_level_reference_blocks_absolute_spl(tmp_path: Path) -> None:
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    record, dataset = _save_measurement(
        measurement_repository, revision, 'm-1', level_reference='dbfs'
    )
    context = _context(quality_repository, record.measurement_id)
    reference = _persist_reference(
        quality_repository, record, dataset, None, kind='dbfs'
    )

    report = _report(
        record=record,
        dataset=dataset,
        context=context,
        level_reference=reference,
    )
    assert report.capability('absolute_spl').decision == 'BLOCKED'
    assert 'dbfs' in report.capability('absolute_spl').reasons[0]


def test_unscoped_or_inapplicable_calibration_never_allows(tmp_path: Path) -> None:
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    record, dataset = _save_measurement(measurement_repository, revision, 'm-1')

    # A calibration whose validity scope is unestablished cannot authorize.
    calibration = _persist_calibration(
        quality_repository, validity_scope='unknown'
    )
    reference = _persist_reference(
        quality_repository, record, dataset, calibration
    )
    report = _report(
        record=record,
        dataset=dataset,
        level_reference=reference,
        level_calibration=calibration,
    )
    assert report.capability('absolute_spl').decision == 'UNKNOWN'

    # #859 applicability: an instrument-scoped calibration for a different
    # microphone is provably inapplicable to this acquisition.
    foreign_calibration = _persist_calibration(
        quality_repository,
        validity_scope='instrument',
        instrument_identity='minidsp umik-2 s/n9999',
    )
    # The incompatible-instrument gate is exercised through a second
    # measurement's dataset (each dataset binds one level reference).
    record2, dataset2 = _save_measurement(
        measurement_repository, revision, 'm-2'
    )
    context2 = _context(
        quality_repository,
        record2.measurement_id,
        context_id='acq-foreign-2',
        microphone=CadMicrophoneCapture(
            manufacturer='minidsp', model='umik-1', serial='sn-0001'
        ),
    )
    reference2 = _persist_reference(
        quality_repository, record2, dataset2, foreign_calibration
    )
    report2 = _report(
        record=record2,
        dataset=dataset2,
        context=context2,
        level_reference=reference2,
        level_calibration=foreign_calibration,
    )
    assert report2.capability('absolute_spl').decision == 'BLOCKED'
    assert 'instrument' in report2.capability('absolute_spl').reasons[0]


def test_instrument_scoped_calibration_matching_microphone_allows(tmp_path: Path) -> None:
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    record, dataset = _save_measurement(measurement_repository, revision, 'm-1')
    context = _context(
        quality_repository,
        record.measurement_id,
        microphone=CadMicrophoneCapture(
            manufacturer='minidsp', model='umik-1', serial='sn-0001'
        ),
    )
    calibration = _persist_calibration(
        quality_repository,
        validity_scope='instrument',
        instrument_identity='minidsp umik-1 sn-0001',
    )
    reference = _persist_reference(
        quality_repository, record, dataset, calibration
    )
    observation = _persist_observation(
        quality_repository, record.measurement_id, noise_floor_db_spl=29.5
    )
    report = _report(
        record=record,
        dataset=dataset,
        context=context,
        level_reference=reference,
        level_calibration=calibration,
        observation=observation,
        evidence_fields={'noise_floor_db_spl': 29.5},
    )
    quality_repository.save_report(report)
    assert report.capability('absolute_spl').decision == 'ALLOWED'
    assert report.capability('absolute_noise_level').decision == 'ALLOWED'


def test_session_scoped_calibration_requires_authoritative_context(
    tmp_path: Path,
) -> None:
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    record, dataset = _save_measurement(measurement_repository, revision, 'm-1')
    calibration = _persist_calibration(
        quality_repository, validity_scope='session'
    )
    reference = _persist_reference(
        quality_repository, record, dataset, calibration
    )

    # No acquisition context at all -> UNKNOWN.
    report = _report(
        record=record,
        dataset=dataset,
        level_reference=reference,
        level_calibration=calibration,
    )
    assert report.capability('absolute_spl').decision == 'UNKNOWN'

    # An unknown-provenance context is still not authoritative.
    manual_context = build_acquisition_context(
        acquisition_context_id='acq-manual',
        source_kind='unknown',
        subject_measurement_ids=(record.measurement_id,),
        created_at_utc='2026-09-19T00:00:00+00:00',
    )
    quality_repository.save_acquisition_context(manual_context)
    report = _report(
        record=record,
        dataset=dataset,
        context=manual_context,
        level_reference=reference,
        level_calibration=calibration,
    )
    assert report.capability('absolute_spl').decision == 'UNKNOWN'

    # An authoritative context makes the session-scoped calibration apply.
    native = _context(quality_repository, record.measurement_id)
    report = _report(
        record=record,
        dataset=dataset,
        context=native,
        level_reference=reference,
        level_calibration=calibration,
    )
    assert report.capability('absolute_spl').decision == 'ALLOWED'


def test_spl_named_observation_fields_never_authorize_alone(tmp_path: Path) -> None:
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    record, dataset = _save_measurement(measurement_repository, revision, 'm-1')
    # Numeric producer-declared SPL fields are preserved as evidence but can
    # never authorize the absolute-level claims by themselves.
    report = _report(
        record=record,
        dataset=dataset,
        evidence_fields={
            'noise_floor_db_spl': 30.0,
            'signal_level_db_spl': 75.0,
        },
    )
    assert report.evidence.noise_floor_db_spl == 30.0
    assert report.capability('absolute_spl').decision == 'UNKNOWN'
    assert report.capability('absolute_noise_level').decision == 'UNKNOWN'


def test_snr_stays_evaluable_without_absolute_calibration(tmp_path: Path) -> None:
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    record, dataset = _save_measurement(measurement_repository, revision, 'm-1')
    context = _context(quality_repository, record.measurement_id)
    report = _report(
        record=record,
        dataset=dataset,
        context=context,
        evidence_fields={'snr_db': 40.0, 'clipping_detected': False},
    )
    assert report.noise_snr.status in {'PASS', 'NOT_EVALUATED'}
    assert report.capability('absolute_spl').decision == 'UNKNOWN'


def test_absolute_noise_requires_observation_noise_floor(
    tmp_path: Path,
) -> None:
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    record, dataset = _save_measurement(measurement_repository, revision, 'm-1')
    calibration = _persist_calibration(quality_repository)
    reference = _persist_reference(
        quality_repository, record, dataset, calibration
    )
    report = _report(
        record=record,
        dataset=dataset,
        level_reference=reference,
        level_calibration=calibration,
    )
    assert report.capability('absolute_spl').decision == 'ALLOWED'
    assert report.capability('absolute_noise_level').decision == 'UNKNOWN'

    # An observation that attests the noise floor opens the claim.
    record2, dataset2 = _save_measurement(
        measurement_repository, revision, 'm-2'
    )
    calibration2 = _persist_calibration(quality_repository)
    reference2 = _persist_reference(
        quality_repository, record2, dataset2, calibration2
    )
    observation = _persist_observation(
        quality_repository, record2.measurement_id, noise_floor_db_spl=31.0
    )
    report2 = _report(
        record=record2,
        dataset=dataset2,
        level_reference=reference2,
        level_calibration=calibration2,
        observation=observation,
        evidence_fields={'noise_floor_db_spl': 31.0},
    )
    assert report2.capability('absolute_noise_level').decision == 'ALLOWED'


def test_historical_v1_report_replays_and_absolute_spl_stays_unknown(
    tmp_path: Path,
) -> None:
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    record, dataset = _save_measurement(measurement_repository, revision, 'm-1')
    report = build_measurement_quality_report_v1(
        measurement=record,
        dataset=dataset,
        evidence=CadMeasurementQualityEvidence(),
        profile=build_measurement_quality_profile(),
        report_id='report-v1',
        created_at_utc='2026-09-19T00:01:00+00:00',
    )
    quality_repository.save_report(report)
    reopened = quality_repository.get_report(report.report_id)
    assert reopened == report
    assert reopened.algorithm_version == 'measurement-quality-1'
    # The report predates the absolute-level claims: gating stays UNKNOWN.
    assert (
        gate_measurement_claim(reopened, 'absolute_spl').decision == 'UNKNOWN'
    )
    assert (
        gate_measurement_claim(reopened, 'absolute_noise_level').decision
        == 'UNKNOWN'
    )
    assert (
        gate_measurement_claim(reopened, 'frequency_response_corrected').decision
        == 'UNKNOWN'
    )


def test_report_with_forged_level_reference_pin_fails_closed(tmp_path: Path) -> None:
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    record, dataset = _save_measurement(measurement_repository, revision, 'm-1')
    calibration = _persist_calibration(quality_repository)
    reference = _persist_reference(
        quality_repository, record, dataset, calibration
    )
    report = _report(
        record=record,
        dataset=dataset,
        level_reference=reference,
        level_calibration=calibration,
    )
    quality_repository.save_report(report)

    # A persisted report whose stored pin claims the persisted ref is
    # 'spl_uncalibrated' disagrees with the resolved authority on read.
    forged = report.model_copy(
        update={
            'level_reference': CadDatasetLevelReferenceBinding(
                level_reference_id=reference.level_reference_id,
                level_reference_sha256=reference.level_reference_sha256,
                level_reference_kind='spl_uncalibrated',
            )
        }
    )
    forged = forged.model_copy(
        update={'report_sha256': _hash(forged.identity_payload())}
    )
    with pytest.raises(ValueError, match='level reference'):
        quality_repository._validate_current_report(forged)

    # A persisted pin naming a reference that was never bound fails closed.
    ghost = report.model_copy(
        update={
            'level_reference': CadDatasetLevelReferenceBinding(
                level_reference_id='ref-ghost',
                level_reference_sha256='a' * 64,
                level_reference_kind='absolute_spl',
            )
        }
    )
    ghost = ghost.model_copy(
        update={'report_sha256': _hash(ghost.identity_payload())}
    )
    with pytest.raises(ValueError, match='level reference'):
        quality_repository._validate_current_report(ghost)


def test_legacy_dataset_field_disagreement_fails_closed(tmp_path: Path) -> None:
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    # Producer-declared 'dbfs' dataset bound to an absolute_spl authority is
    # contradictory: the canonical reference governs, and the disagreement is
    # rejected rather than silently overridden.
    record, dataset = _save_measurement(
        measurement_repository, revision, 'm-1', level_reference='dbfs'
    )
    calibration = _persist_calibration(quality_repository)
    reference = _persist_reference(
        quality_repository, record, dataset, calibration
    )
    report = _report(
        record=record,
        dataset=dataset,
        level_reference=reference,
        level_calibration=calibration,
    )
    with pytest.raises(ValueError, match='level_reference'):
        quality_repository.save_report(report)


def test_gate_absolute_spl_is_the_downstream_contract(tmp_path: Path) -> None:
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    record, dataset = _save_measurement(measurement_repository, revision, 'm-1')
    context = _context(quality_repository, record.measurement_id)
    calibration_sha = quality_repository.save_calibration_file(
        filename='umik.txt', raw_bytes=b'umik-correction'
    )
    observation = _persist_observation(
        quality_repository,
        record.measurement_id,
        clipping_detected=False,
        snr_db=40.0,
        usable_frequency_band_hz=(20.0, 80.0),
    )
    # Legacy-calibrated-only report: downstream absolute-level consumers get
    # an explicit UNKNOWN, never an inferred ALLOWED.
    report = _report(
        record=record,
        dataset=dataset,
        context=context,
        observation=observation,
        evidence_fields={
            'clipping_detected': False,
            'snr_db': 40.0,
            'usable_frequency_band_hz': (20.0, 80.0),
            'calibration_file_sha256': calibration_sha,
            'expected_calibration_file_sha256': calibration_sha,
        },
    )
    quality_repository.save_report(report)
    reopened = quality_repository.latest_report(record.measurement_id)
    assert gate_measurement_claim(reopened, 'absolute_spl').decision == 'UNKNOWN'
    assert (
        gate_measurement_claim(reopened, 'frequency_response_corrected').decision
        == 'ALLOWED'
    )
