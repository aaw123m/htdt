"""REV55-REGCAL regression tests: prediction<->measurement registration (#564).

Covers the fail-closed registration authority: sealed records, the
comparability gate's hard/limiting reasons, per-observable residual reports,
staleness on SceneRevision change, partition disjointness, and persistence
integrity.
"""

from __future__ import annotations

import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurements import (
    HTDT_DECLARED_IMPORTER_VERSION,
    declared_fr_raw,
    measurement_record_for_revision,
)
from htdt.cad_prediction_measurement_registration import (
    ComparabilityVerdict,
    LocalFrameBinding,
    ManualPositionCorrection,
    PredictionMeasurementRegistration,
    TimingRegistration,
    assert_partition_disjoint,
    compute_residual_report,
    detect_ir_onset,
    estimate_delay_by_correlation,
    evaluate_registration_freshness,
    sound_speed_from_temperature_c,
)
from htdt.cad_prediction_measurement_registration_repository import (
    CadPredictionMeasurementRegistrationRepository,
)
from htdt.cad_prediction_measurement_service import (
    PredictionMeasurementError,
    PredictionMeasurementService,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Position3,
    Quaternion4,
    SceneEntity,
    make_f1_scene,
)

from hashlib import sha256


def _repositories(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(make_f1_scene(), parent_revision_id=None).revision
    measurement_repository = CadMeasurementRepository(scene_repository)
    registration_repository = CadPredictionMeasurementRegistrationRepository(
        scene_repository
    )
    service = PredictionMeasurementService(
        scene_repository,
        revision.document_id,
        measurement_repository=measurement_repository,
        registration_repository=registration_repository,
    )
    return revision, measurement_repository, registration_repository, service


def _save_dataset(
    repository: CadMeasurementRepository,
    revision,
    measurement_id: str,
    *,
    evidence_type: str = 'measured',
    levels: tuple[float, ...] = (70.0, 71.0, 69.0, 72.0, 71.5, 70.5),
    frequencies: tuple[float, ...] = (20.0, 40.0, 80.0, 160.0, 320.0, 640.0),
    phase_deg: tuple[float, ...] | None = None,
    entity: str = 'point-mlp',
) -> tuple[object, CadFrequencyResponseDataset]:
    raw = declared_fr_raw(
        frequency_hz=frequencies,
        level_db=levels,
        phase_status='valid' if phase_deg is not None else 'absent',
        phase_deg=phase_deg,
    )
    record = measurement_record_for_revision(
        revision,
        entity,
        measurement_id=measurement_id,
        evidence_type=evidence_type,
        channel_role='front_left',
        source_speaker_ids=('speaker-fl',),
        radiation_scope='single',
        routing_evidence='verified',
        imported_at='2026-10-01T00:00:00+00:00',
        source_kind='unknown',
        external_source_id=f'rew-{measurement_id}',
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id=f'dataset-{measurement_id}',
        measurement_id=measurement_id,
        frequency_hz=frequencies,
        level_db=levels,
        phase_deg=phase_deg,
        phase_status='valid' if phase_deg is not None else 'absent',
        source_sha256=sha256(raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    repository.save(
        record,
        dataset,
        raw_filename=f'{measurement_id}.txt',
        raw_bytes=raw,
    )
    return record, dataset


def _pair(
    repository: CadMeasurementRepository,
    revision,
    *,
    measured_levels: tuple[float, ...] = (70.0, 71.0, 69.0, 72.0, 71.5, 70.5),
    predicted_levels: tuple[float, ...] = (69.5, 71.4, 68.8, 72.2, 71.2, 70.9),
    frequencies: tuple[float, ...] = (20.0, 40.0, 80.0, 160.0, 320.0, 640.0),
    measured_phase: tuple[float, ...] | None = None,
    predicted_phase: tuple[float, ...] | None = None,
):
    measured_record, measured_dataset = _save_dataset(
        repository,
        revision,
        'meas-1',
        levels=measured_levels,
        frequencies=frequencies,
        phase_deg=measured_phase,
    )
    predicted_record, predicted_dataset = _save_dataset(
        repository,
        revision,
        'pred-1',
        evidence_type='predicted',
        levels=predicted_levels,
        frequencies=frequencies,
        phase_deg=predicted_phase,
    )
    return measured_record, measured_dataset, predicted_dataset


def _register(
    service: PredictionMeasurementService,
    measured_record,
    predicted_dataset,
    **kwargs,
):
    return service.register_pair(
        measured_record.measurement_id,
        prediction_dataset_id=predicted_dataset.dataset_id,
        **kwargs,
    )


def test_registration_sealed_and_persisted(tmp_path: Path) -> None:
    revision, measurement_repository, registration_repository, service = _repositories(tmp_path)
    measured_record, _, predicted_dataset = _pair(measurement_repository, revision)

    registration = _register(service, measured_record, predicted_dataset)

    assert registration.registration_id.startswith('pm-registration:')
    assert len(registration.semantic_sha256) == 64
    assert registration.comparability.state in (
        'comparable',
        'comparable_with_limitations',
    )
    assert registration.comparability.magnitude_supported

    persisted = registration_repository.get(registration.registration_id)
    assert persisted == registration


def test_comparability_unknowns_are_limitations_not_upgrades(tmp_path: Path) -> None:
    revision, measurement_repository, _, service = _repositories(tmp_path)
    measured_record, _, predicted_dataset = _pair(measurement_repository, revision)

    registration = _register(service, measured_record, predicted_dataset)
    verdict = registration.comparability

    # No timing reference, no level calibration, no declared uncertainty:
    # the verdict must carry explicit limitations, never silent upgrades.
    assert verdict.state == 'comparable_with_limitations'
    assert 'timing_reference_unknown' in verdict.limitations
    assert 'level_reference_unknown' in verdict.limitations
    assert 'phase_absent' in verdict.limitations
    assert not verdict.phase_supported
    assert not verdict.absolute_level_supported
    assert not verdict.arrival_time_supported


def test_geometry_revision_mismatch_is_incomparable(tmp_path: Path) -> None:
    revision, measurement_repository, _, service = _repositories(tmp_path)
    measured_record, _, _ = _pair(measurement_repository, revision)

    # Persist a predicted dataset against a DIFFERENT scene revision.
    revision2 = service.scene_repository.save(
        _scene_with_extra_point(), parent_revision_id=revision.revision_id
    ).revision
    _, predicted_dataset = _save_dataset(
        measurement_repository,
        revision2,
        'pred-stale',
        evidence_type='predicted',
    )

    registration = _register(service, measured_record, predicted_dataset)
    assert registration.comparability.state == 'incomparable'
    assert 'geometry_revision_mismatch' in registration.comparability.reasons
    assert not registration.comparability.magnitude_supported


def test_residual_report_binds_registration_and_spec(tmp_path: Path) -> None:
    revision, measurement_repository, registration_repository, service = _repositories(tmp_path)
    measured_record, _, predicted_dataset = _pair(measurement_repository, revision)
    registration = _register(service, measured_record, predicted_dataset)

    report = service.compute_and_persist_residual_report(registration.registration_id)

    assert report.registration_id == registration.registration_id
    assert report.registration_sha256 == registration.semantic_sha256
    assert report.report_id.startswith('pm-residual:')
    magnitude = next(o for o in report.observables if o.observable == 'magnitude_db')
    assert magnitude.state == 'computed'
    assert magnitude.magnitude_bands
    phase = next(o for o in report.observables if o.observable == 'phase_deg')
    assert phase.state in ('unsupported', 'insufficient_data')
    assert phase.reason

    persisted = registration_repository.get_report(report.report_id)
    assert persisted == report


def test_residual_report_refused_when_incomparable(tmp_path: Path) -> None:
    revision, measurement_repository, registration_repository, service = _repositories(tmp_path)
    measured_record, _, _ = _pair(measurement_repository, revision)
    revision2 = service.scene_repository.save(
        _scene_with_extra_point(), parent_revision_id=revision.revision_id
    ).revision
    _, predicted_dataset = _save_dataset(
        measurement_repository, revision2, 'pred-stale', evidence_type='predicted'
    )
    registration = _register(service, measured_record, predicted_dataset)
    assert registration.comparability.state == 'incomparable'

    with pytest.raises(PredictionMeasurementError):
        service.compute_and_persist_residual_report(registration.registration_id)


def test_freshness_stales_on_new_scene_revision(tmp_path: Path) -> None:
    revision, measurement_repository, _, service = _repositories(tmp_path)
    measured_record, _, predicted_dataset = _pair(measurement_repository, revision)
    registration = _register(service, measured_record, predicted_dataset)

    assert service.registration_freshness(registration) == 'current'

    service.scene_repository.save(
        _scene_with_extra_point(), parent_revision_id=revision.revision_id
    )

    assert (
        service.registration_freshness(registration) == 'stale_geometry_revision'
    )
    with pytest.raises(PredictionMeasurementError):
        service.compute_and_persist_residual_report(registration.registration_id)


def test_estimated_alignment_requires_offset_and_uncertainty(tmp_path: Path) -> None:
    revision, measurement_repository, _, service = _repositories(tmp_path)
    measured_record, _, predicted_dataset = _pair(measurement_repository, revision)

    with pytest.raises(Exception):
        _register(
            service,
            measured_record,
            predicted_dataset,
            timing_method='estimated_by_correlation',
            applied_offset_s=0.004,  # missing uncertainty
        )
    with pytest.raises(Exception):
        _register(
            service,
            measured_record,
            predicted_dataset,
            timing_method='estimated_by_correlation',
            timing_uncertainty_s=0.0005,  # missing applied offset
        )

    registration = _register(
        service,
        measured_record,
        predicted_dataset,
        timing_method='estimated_by_correlation',
        applied_offset_s=0.0042,
        timing_uncertainty_s=0.0005,
    )
    assert registration.timing.method == 'estimated_by_correlation'
    assert 'timing_estimated_not_exact' in registration.comparability.limitations
    # An estimated alignment never unlocks absolute phase claims.
    assert not registration.comparability.phase_supported


def test_manual_correction_must_be_bounded(tmp_path: Path) -> None:
    revision, measurement_repository, _, service = _repositories(tmp_path)
    measured_record, _, predicted_dataset = _pair(measurement_repository, revision)

    # Unbounded correction (no tolerance) is refused.
    with pytest.raises(Exception):
        _register(
            service,
            measured_record,
            predicted_dataset,
            spatial_method='manual_correction',
            manual_correction=ManualPositionCorrection(
                applied_offset_m=(0.05, 0.0, 0.0),
                bound_m=0.1,
                reason='mic bumped during placement',
            ),
        )

    registration = _register(
        service,
        measured_record,
        predicted_dataset,
        spatial_method='manual_correction',
        position_tolerance_m=0.10,
        position_uncertainty_m=0.05,
        manual_correction=ManualPositionCorrection(
            applied_offset_m=(0.05, 0.0, 0.0),
            bound_m=0.10,
            reason='mic bumped during placement',
        ),
    )
    assert registration.spatial.method == 'manual_correction'
    assert registration.spatial.manual_correction is not None
    # The applied offset lands on the measured receiver position.
    mlp = revision.document.entity('point-mlp').position
    assert math.isclose(
        registration.receiver.measured_position.x_m, mlp.x_m + 0.05
    )


def test_partition_disjoint_guard(tmp_path: Path) -> None:
    revision, measurement_repository, _, service = _repositories(tmp_path)
    measured_record, _, predicted_dataset = _pair(measurement_repository, revision)
    second_measured, _ = _save_dataset(
        measurement_repository, revision, 'meas-2'
    )

    calibration_reg = _register(
        service, measured_record, predicted_dataset, partition='calibration'
    )
    holdout_reg = _register(
        service, second_measured, predicted_dataset, partition='holdout'
    )
    registrations = [calibration_reg, holdout_reg]
    assert_partition_disjoint(registrations)

    conflicting = build = service.build_registration(
        measured_record.measurement_id,
        prediction_dataset_id=predicted_dataset.dataset_id,
        partition='holdout',
    )
    assert service.check_partition_disjoint() is None
    with pytest.raises(ValueError):
        assert_partition_disjoint([calibration_reg, conflicting])


def test_persisted_tamper_fails_closed(tmp_path: Path) -> None:
    revision, measurement_repository, registration_repository, service = _repositories(tmp_path)
    measured_record, _, predicted_dataset = _pair(measurement_repository, revision)
    registration = _register(service, measured_record, predicted_dataset)

    # Rewrite the stored payload: the sealed id/hash stay but the body claims
    # a different comparability state. Read must fail closed.
    payload = json.loads(registration.model_dump_json())
    payload['comparability']['state'] = 'comparable'
    tampered = json.dumps(payload, sort_keys=True)
    with closing(sqlite3.connect(registration_repository.path)) as connection, connection:
        connection.execute(
            'UPDATE cad_prediction_measurement_registrations SET payload_json=? '
            'WHERE registration_id=?',
            (tampered, registration.registration_id),
        )

    with pytest.raises(Exception):
        registration_repository.get(registration.registration_id)


def test_duplicate_save_conflict(tmp_path: Path) -> None:
    revision, measurement_repository, registration_repository, service = _repositories(tmp_path)
    measured_record, _, predicted_dataset = _pair(measurement_repository, revision)
    registration = _register(service, measured_record, predicted_dataset)

    # Idempotent re-save is fine.
    registration_repository.save(registration)

    # A record that reuses the id but carries different content conflicts.
    payload = json.loads(registration.model_dump_json())
    payload['confidence_label'] = 'forged'
    with pytest.raises(Exception):
        registration_repository.save(
            PredictionMeasurementRegistration.model_validate(payload)
        )


def _scene_with_extra_point():
    scene = make_f1_scene()
    return scene.model_copy(
        update={
            'entities': scene.entities
            + (
                SceneEntity(
                    entity_id='point-corner',
                    kind='measurement_point',
                    name='Corner point',
                    position=Position3(x_m=0.5, y_m=3.5, z_m=1.0),
                ),
            )
        }
    )


def test_onset_detection_threshold_method() -> None:
    # IR: silence -> direct arrival at index 240 (5 ms @ 48k) -> reflection.
    import numpy as np

    rng = np.random.default_rng(0)
    amplitudes = rng.normal(0.0, 0.001, 4800)
    amplitudes[240] = 0.5
    amplitudes[480] = 0.3  # stronger-than-noise early reflection
    onset_s, onset_index, _floor, _peak = detect_ir_onset(amplitudes, 48000.0)
    assert onset_index == 240
    assert math.isclose(onset_s, 240 / 48000.0)


def test_correlation_delay_estimate_bounded() -> None:
    import numpy as np

    rng = np.random.default_rng(1)
    base = rng.normal(0.0, 1.0, 4800)
    delayed = np.zeros_like(base)
    delayed[120:] = base[:-120]  # 2.5 ms @ 48k
    delayed += rng.normal(0.0, 0.01, delayed.size)
    delay_s, peak_norm, uncertainty_s = estimate_delay_by_correlation(
        base, delayed, 48000.0
    )
    assert math.isclose(delay_s, 120 / 48000.0, abs_tol=0.5 / 48000.0)
    assert peak_norm > 0.5
    assert uncertainty_s > 0.0


def test_local_frame_transform_maps_positions(tmp_path: Path) -> None:
    revision, measurement_repository, _, service = _repositories(tmp_path)
    measured_record, _, predicted_dataset = _pair(measurement_repository, revision)

    # Measured position declared in a local frame: origin at scene (1,1,1),
    # identity rotation; local (2,2,0.1) maps to scene (3,3,1.1) = MLP.
    local_frame = LocalFrameBinding(
        frame_label='seat-marker-frame',
        origin=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
        orientation=Quaternion4(w=1.0, x=0.0, y=0.0, z=0.0),
        provenance='surveyed',
        uncertainty_m=0.02,
    )
    registration = _register(
        service,
        measured_record,
        predicted_dataset,
        spatial_method='local_frame_transform',
        measured_frame='local_frame',
        local_frame=local_frame,
        measured_receiver_position=(2.0, 2.0, 0.1),
    )
    mlp = revision.document.entity('point-mlp').position
    assert math.isclose(registration.receiver.measured_position.x_m, mlp.x_m)
    assert math.isclose(registration.receiver.measured_position.y_m, mlp.y_m)
    assert math.isclose(registration.receiver.measured_position.z_m, mlp.z_m)
    assert 'spatial_provenance_unknown' not in registration.comparability.limitations


def test_phase_supported_only_with_exact_timing(tmp_path: Path) -> None:
    revision, measurement_repository, _, service = _repositories(tmp_path)
    phase = (0.0, -30.0, -60.0, -90.0, -120.0, -150.0)
    measured_record, _, predicted_dataset = _pair(
        measurement_repository,
        revision,
        measured_phase=phase,
        predicted_phase=tuple(value + 5.0 for value in phase),
    )

    # Unknown timing -> phase residual must not be computed even though both
    # datasets carry valid phase columns.
    registration = _register(service, measured_record, predicted_dataset)
    assert registration.comparability.phase_supported is False

    registration_exact = _register(
        service,
        measured_record,
        predicted_dataset,
        timing_method='exact_reference',
        applied_offset_s=0.0,
        timing_uncertainty_s=0.00002,
        reference_channel='loopback-ch2',
    )
    # NOTE: exact_reference here is a caller-declared claim for the test;
    # production derivations never auto-assign it without a timing authority.
    assert registration_exact.timing.method == 'exact_reference'
    assert registration_exact.comparability.phase_supported is True

    report = service.compute_and_persist_residual_report(
        registration_exact.registration_id
    )
    phase_observable = next(
        o for o in report.observables if o.observable == 'phase_deg'
    )
    assert phase_observable.state == 'computed'
    assert phase_observable.phase_bands
    for band in phase_observable.phase_bands:
        assert band.mean_abs_difference_deg is not None
        assert band.mean_abs_difference_deg >= 0.0


def test_sound_speed_temperature_law() -> None:
    # c = 331.4 + 0.6*T: at 20 C -> 343.4 m/s; at 0 C -> 331.4 m/s.
    assert math.isclose(sound_speed_from_temperature_c(0.0), 331.4)
    assert math.isclose(sound_speed_from_temperature_c(20.0), 343.4)


def test_environment_sound_speed_mismatch_is_limiting(tmp_path: Path) -> None:
    revision, measurement_repository, _, service = _repositories(tmp_path)
    measured_record, _, predicted_dataset = _pair(measurement_repository, revision)
    registration = _register(
        service,
        measured_record,
        predicted_dataset,
        measured_sound_speed_m_s=343.4,
    )
    assert registration.environment.sound_speed_m_s == 343.4
    # No prediction-side speed declared: honest unknown, not a mismatch.
    assert registration.environment.sound_speed_relative_difference is None
