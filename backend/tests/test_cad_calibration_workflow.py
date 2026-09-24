from __future__ import annotations

import csv
import io
import json
from hashlib import sha256
from pathlib import Path

import pytest

from htdt.cad_calibration import (
    CadCalibrationChannel,
    CadCrossoverSetting,
    CadDeviceCapabilityConstraints,
    CadTargetCurve,
    CadTargetCurvePoint,
    CadTargetNormalizationCondition,
    CadVerificationMeasurementPoint,
    build_biquad_filter,
    build_calibration_plan,
)
from htdt.cad_calibration_repository import CadCalibrationRepository
from htdt.cad_calibration_workflow import (
    AppliedSettingsDeviation,
    CadCalibrationWorkflowService,
)
from htdt.cad_calibration_workflow_repository import CadAppliedSettingsRepository
from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurement_quality import (
    CadMeasurementQualityEvidence,
    build_measurement_observation,
    build_measurement_quality_profile,
    build_measurement_quality_report,
    observation_binding,
)
from htdt.cad_measurement_quality_repository import CadMeasurementQualityRepository
from htdt.cad_measurement_repository import CadMeasurementRepository
from htdt.cad_measurements import (
    HTDT_DECLARED_IMPORTER_VERSION,
    canonical_json,
    declared_fr_raw,
    measurement_record_for_revision,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, make_f1_scene
from htdt.cad_system_variant import build_system_variant
from htdt.cad_system_variant_repository import CadSystemVariantRepository

NOW = '2026-09-23T00:00:00+00:00'


def _device(**overrides):
    payload = {
        'capability_id': 'generic-device',
        'capability_version': '1',
        'supported_sample_rates_hz': (48000,),
        'supported_filter_types': ('peaking',),
        'max_filters_per_channel': 4,
        'max_boost_db': 6.0,
        'max_cut_db': 12.0,
        'min_gain_db': -12.0,
        'max_gain_db': 6.0,
        'max_delay_s': 0.050,
        'supported_crossover_orders': (2, 4),
        'allowed_physical_outputs': ('out-fl',),
    }
    payload.update(overrides)
    return CadDeviceCapabilityConstraints(**payload)


def _target():
    return CadTargetCurve(
        points=(
            CadTargetCurvePoint(frequency_hz=20.0, level_db=0.0),
            CadTargetCurvePoint(frequency_hz=20000.0, level_db=-6.0),
        ),
        normalization=CadTargetNormalizationCondition(
            method='reference_frequency',
            reference_frequency_hz=1000.0,
        ),
    )


def _channel(**overrides):
    payload = {
        'channel_id': 'FL',
        'role_id': 'FL',
        'source_entity_id': 'speaker-fl',
        'physical_output_id': 'out-fl',
        'sample_rate_hz': 48000,
        'gain_db': -1.5,
        'delay_s': 0.0,
        'polarity': 'normal',
        'crossovers': (
            CadCrossoverSetting(
                crossover_type='high_pass', frequency_hz=90.0, filter_order=4
            ),
        ),
        'peq': (
            build_biquad_filter(
                filter_id='peq-1',
                filter_type='peaking',
                frequency_hz=63.0,
                q=1.4,
                gain_db=-3.0,
                sample_rate_hz=48000,
            ),
        ),
        'routing': ('main',),
    }
    payload.update(overrides)
    return CadCalibrationChannel(**payload)


def _repositories(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(make_f1_scene(), parent_revision_id=None).revision
    measurement_repository = CadMeasurementRepository(scene_repository)
    quality_repository = CadMeasurementQualityRepository(measurement_repository)
    system_variant_repository = CadSystemVariantRepository(scene_repository)
    variant = build_system_variant(
        baseline=revision,
        name='Calibration fixture system',
        role_bindings=(),
        proposed_entities=(),
        created_at_utc=NOW,
    )
    system_variant_repository.save_variant(variant)
    calibration_repository = CadCalibrationRepository(
        scene_repository=scene_repository,
        system_variant_repository=system_variant_repository,
        measurement_repository=measurement_repository,
        quality_repository=quality_repository,
    )
    applied_repository = CadAppliedSettingsRepository(scene_repository)
    service = CadCalibrationWorkflowService(
        calibration_repository,
        applied_repository=applied_repository,
        measurement_repository=measurement_repository,
        quality_repository=quality_repository,
    )
    return (
        revision,
        variant,
        measurement_repository,
        quality_repository,
        calibration_repository,
        applied_repository,
        service,
    )


def _save_measurement(
    measurement_repository: CadMeasurementRepository,
    revision,
    measurement_id: str,
    *,
    captured_at: str | None = None,
):
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
        captured_at=captured_at,
        imported_at=NOW,
        source_kind='unknown',
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
        record,
        dataset,
        raw_filename=f'{measurement_id}.json',
        raw_bytes=raw,
    )
    return record, dataset


def _save_quality(quality_repository, measurement, dataset, report_id: str):
    observation_fields = {
        'usable_frequency_band_hz': (20.0, 20000.0),
    }
    observation = build_measurement_observation(
        observation_id=f'obs-{report_id}',
        measurement_id=measurement.measurement_id,
        source_kind='manual',
        observed_at_utc=NOW,
        **observation_fields,
    )
    quality_repository.save_observation(observation)
    evidence = CadMeasurementQualityEvidence(
        **observation_fields,
        evidence_source='manual',
    )
    profile = build_measurement_quality_profile(
        profile_version='workflow-fixture-quality-1',
    )
    report = build_measurement_quality_report(
        measurement=measurement,
        dataset=dataset,
        evidence=evidence,
        profile=profile,
        observation=observation_binding(observation),
        report_id=report_id,
        created_at_utc=NOW,
    )
    quality_repository.save_report(report)
    return report


def _plan(tmp_path: Path):
    (
        revision,
        variant,
        measurements,
        quality,
        _calibration,
        _applied,
        service,
    ) = _repositories(tmp_path)
    measurement, dataset = _save_measurement(measurements, revision, 'm-1')
    report = _save_quality(quality, measurement, dataset, 'q-1')
    helpers = (revision, measurements, quality)
    plan = build_calibration_plan(
        scene_revision=revision,
        system_variant=variant,
        measurement=measurement,
        dataset=dataset,
        quality_report=report,
        channels=(_channel(),),
        sample_rate_hz=48000,
        device_constraints=_device(),
        max_boost_db=6.0,
        max_cut_db=12.0,
        target_curve=_target(),
        plan_id='plan-1',
        plan_version='fixture-1',
        created_at_utc=NOW,
        source_kind='provided_fixture',
    )
    service.repository.save_plan(plan)
    return plan, service, helpers


def test_review_plan_exposes_per_channel_settings(tmp_path: Path) -> None:
    plan, service, _helpers = _plan(tmp_path)
    review = service.review_plan(plan.plan_id)
    assert review.plan_id == plan.plan_id
    assert review.support_state == 'SUPPORTED'
    channel = review.channels[0]
    assert channel.channel_id == 'FL'
    assert channel.gain_db == pytest.approx(-1.5)
    assert channel.delay_ms == pytest.approx(0.0)
    # the review shows exactly the plan's own crossover — never an injected default
    assert channel.crossovers[0]['frequency_hz'] == pytest.approx(90.0)
    assert len(channel.peq_filters) == 1


def test_export_is_deterministic_json_and_csv(tmp_path: Path) -> None:
    plan, service, _helpers = _plan(tmp_path)
    bundle = service.export_settings(plan.plan_id, created_at_utc=NOW)
    assert bundle.export.calibration_plan_id == plan.plan_id
    parsed = json.loads(bundle.json_text)
    assert 'FL' in bundle.csv_text
    rows = list(csv.DictReader(io.StringIO(bundle.csv_text)))
    assert rows
    assert service.lifecycle_state(plan.plan_id) == 'exported'


def test_export_never_marks_applied(tmp_path: Path) -> None:
    plan, service, _helpers = _plan(tmp_path)
    service.export_settings(plan.plan_id, created_at_utc=NOW)
    assert service.lifecycle_state(plan.plan_id) == 'exported'


def test_mark_user_applied_records_effective_deviations(tmp_path: Path) -> None:
    plan, service, _helpers = _plan(tmp_path)
    bundle = service.export_settings(plan.plan_id, created_at_utc=NOW)
    record = service.mark_user_applied(
        plan.plan_id,
        export_id=bundle.export.export_id,
        applied_at_utc='2026-09-23T01:00:00+00:00',
        deviations=(
            AppliedSettingsDeviation(
                channel_id='FL',
                field_name='gain_db',
                exported_value_repr='-1.5',
                applied_value_repr='-1.0',
                reason='部屋の鳴りで手動微調整',
            ),
        ),
        device_context='avr-fixture',
    )
    assert record.exported_settings_id == bundle.export.export_id
    stored = service.applied_repository.list_applied(plan.plan_id)
    assert stored[0].deviations[0].applied_value_repr == '-1.0'
    assert service.lifecycle_state(plan.plan_id) == 'user_applied'


def test_mark_applied_rejects_foreign_export(tmp_path: Path) -> None:
    plan, service, _helpers = _plan(tmp_path)
    service.export_settings(plan.plan_id, created_at_utc=NOW)
    with pytest.raises(ValueError, match='unknown calibration export'):
        service.mark_user_applied(
            plan.plan_id,
            export_id='ghost-export',
            applied_at_utc=NOW,
        )


def test_deviation_to_unknown_channel_is_rejected(tmp_path: Path) -> None:
    plan, service, _helpers = _plan(tmp_path)
    bundle = service.export_settings(plan.plan_id, created_at_utc=NOW)
    with pytest.raises(ValueError, match='unknown channel'):
        service.mark_user_applied(
            plan.plan_id,
            export_id=bundle.export.export_id,
            applied_at_utc=NOW,
            deviations=(
                AppliedSettingsDeviation(
                    channel_id='SUB',
                    field_name='gain_db',
                    exported_value_repr='0',
                    applied_value_repr='1',
                ),
            ),
        )


def test_verification_remeasure_validated_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, service, helpers = _plan(tmp_path)
    revision, measurements, quality = helpers
    bundle = service.export_settings(plan.plan_id, created_at_utc=NOW)
    service.mark_user_applied(
        plan.plan_id,
        export_id=bundle.export.export_id,
        applied_at_utc='2026-09-23T01:00:00+00:00',
    )
    monkeypatch.setattr(
        'htdt.cad_calibration_repository._utc_now',
        lambda: '2026-09-23T02:05:00+00:00',
    )
    verification = service.plan_verification(
        plan.plan_id,
        export_id=bundle.export.export_id,
        measurement_points=(
            CadVerificationMeasurementPoint(
                point_id='point-mlp',
                position=Position3(x_m=3.0, y_m=3.0, z_m=1.1),
            ),
        ),
        routing=('FL',),
        reference_level_db_spl=75.0,
        required_measurement_capabilities=('magnitude_response',),
        before_measurement_ids=('m-1',),
        created_at_utc='2026-09-23T02:00:00+00:00',
    )
    monkeypatch.undo()
    after_measurement, after_dataset = _save_measurement(
        measurements, revision, 'm-2', captured_at='2026-09-23T02:30:00+00:00'
    )
    _save_quality(quality, after_measurement, after_dataset, 'q-2')
    service.record_remeasurement(
        plan.plan_id,
        verification_plan_id=verification.verification_plan_id,
        export_id=bundle.export.export_id,
        measurement_ids=('m-2',),
        created_at_utc='2026-09-23T03:00:00+00:00',
    )
    assert service.lifecycle_state(plan.plan_id) == 'remeasured'
    service.mark_validated(
        plan.plan_id,
        verification_plan_id=verification.verification_plan_id,
        export_id=bundle.export.export_id,
        measurement_ids=('m-2',),
        created_at_utc='2026-09-23T04:00:00+00:00',
    )
    assert service.lifecycle_state(plan.plan_id) == 'validated'
    events = service.repository.list_lifecycle_events(plan.plan_id)
    assert [event.state for event in events] == [
        'exported',
        'user_applied',
        'remeasured',
        'validated',
    ]
