from __future__ import annotations

import csv
from hashlib import sha256
import io
import json
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
    build_calibration_lifecycle_event,
    build_calibration_plan,
    build_generic_biquad_export,
    build_verification_measurement_plan,
    evaluate_biquad_db,
    read_generic_biquad_json,
    render_generic_biquad_csv,
    render_generic_biquad_json,
)
from htdt.cad_calibration_repository import CadCalibrationRepository
from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_measurement_quality import (
    CadAcquisitionContextBinding,
    CadMeasurementQualityEvidence,
    build_measurement_quality_profile,
    build_measurement_quality_report,
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


def _semantic_hash(payload: dict) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    ).encode('utf-8')
    return sha256(raw).hexdigest()


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
        created_at_utc='2026-09-19T12:30:00+00:00',
    )
    system_variant_repository.save_variant(variant)
    calibration_repository = CadCalibrationRepository(
        scene_repository=scene_repository,
        system_variant_repository=system_variant_repository,
        measurement_repository=measurement_repository,
        quality_repository=quality_repository,
    )
    return (
        revision,
        variant,
        measurement_repository,
        quality_repository,
        system_variant_repository,
        calibration_repository,
    )


def _save_measurement(
    measurement_repository: CadMeasurementRepository,
    revision,
    measurement_id: str,
    *,
    phase: bool = False,
):
    # The declared importer keeps fixture datasets honestly derived: the raw
    # asset literally declares the persisted samples, with the caller's raw
    # marker embedded so each measurement still gets a distinct asset.
    processing = {'fixture_raw': f'raw-{measurement_id}'}
    raw = declared_fr_raw(
        frequency_hz=(20.0, 80.0, 1000.0, 20000.0),
        level_db=(70.0, 71.0, 69.0, 68.0),
        phase_deg=(0.0, 5.0, 10.0, 15.0) if phase else None,
        phase_status='valid' if phase else 'absent',
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
        imported_at='2026-09-19T12:31:00+00:00',
        source_kind='unknown',
        external_source_id=f'rew-{measurement_id}',
    )
    dataset = CadFrequencyResponseDataset(
        dataset_id=f'dataset-{measurement_id}',
        measurement_id=measurement_id,
        frequency_hz=(20.0, 80.0, 1000.0, 20000.0),
        level_db=(70.0, 71.0, 69.0, 68.0),
        phase_deg=(0.0, 5.0, 10.0, 15.0) if phase else None,
        phase_status='valid' if phase else 'absent',
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


def _save_quality(
    quality_repository: CadMeasurementQualityRepository,
    measurement,
    dataset,
    *,
    report_id: str,
    common_timing: bool = False,
    polarity: bool = False,
    usable_band: bool = True,
):
    evidence = CadMeasurementQualityEvidence(
        usable_frequency_band_hz=(20.0, 20000.0) if usable_band else None,
        timing_reference_valid=True if common_timing else None,
        timing_reference_id='loopback-1' if common_timing else None,
        clock_source='shared-clock-1' if common_timing else None,
        sample_rate_hz=48000 if common_timing else None,
        delay_correction_s=0.0 if common_timing else None,
        polarity_correct=True if polarity else None,
        polarity_confidence=0.99 if polarity else None,
        evidence_source='manual',
    )
    acquisition = (
        CadAcquisitionContextBinding(
            acquisition_context_id='acq-shared',
            acquisition_context_sha256=sha256(b'acq-shared').hexdigest(),
            source_kind='manual',
        )
        if common_timing
        else None
    )
    profile = build_measurement_quality_profile(
        profile_version='calibration-fixture-quality-1',
        minimum_polarity_confidence=0.9 if polarity else None,
    )
    report = build_measurement_quality_report(
        measurement=measurement,
        dataset=dataset,
        evidence=evidence,
        profile=profile,
        acquisition_context=acquisition,
        report_id=report_id,
        created_at_utc='2026-09-19T12:32:00+00:00',
    )
    quality_repository.save_report(report)
    return report


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


def _peq(
    *,
    gain_db: float = 3.0,
    frequency_hz: float = 80.0,
    q: float = 1.0,
    filter_id: str = 'peq-1',
    filter_type: str = 'peaking',
):
    return build_biquad_filter(
        filter_id=filter_id,
        filter_type=filter_type,
        frequency_hz=frequency_hz,
        q=q,
        gain_db=gain_db if filter_type == 'peaking' else 0.0,
        sample_rate_hz=48000,
    )


def _channel(
    *,
    gain_db: float = 0.0,
    delay_s: float = 0.0,
    polarity: str = 'normal',
    peq=(),
    crossovers=(),
):
    return CadCalibrationChannel(
        channel_id='FL',
        role_id='FL',
        source_entity_id='speaker-fl',
        physical_output_id='out-fl',
        sample_rate_hz=48000,
        gain_db=gain_db,
        delay_s=delay_s,
        polarity=polarity,
        crossovers=tuple(crossovers),
        peq=tuple(peq),
        routing=('main',),
    )


def _plan(revision, variant, measurement, dataset, report, channel, **overrides):
    payload = {
        'scene_revision': revision,
        'system_variant': variant,
        'measurement': measurement,
        'dataset': dataset,
        'quality_report': report,
        'channels': (channel,),
        'sample_rate_hz': 48000,
        'device_constraints': _device(),
        'max_boost_db': 6.0,
        'max_cut_db': 12.0,
        'target_curve': _target(),
        'plan_id': 'plan-1',
        'plan_version': 'fixture-1',
        'created_at_utc': '2026-09-19T12:33:00+00:00',
        'source_kind': 'provided_fixture',
    }
    payload.update(overrides)
    return build_calibration_plan(**payload)


def test_biquad_reference_frequency_and_stability() -> None:
    peq = _peq(gain_db=3.0, frequency_hz=1000.0, q=1.0)
    assert evaluate_biquad_db(peq, 1000.0) == pytest.approx(3.0, abs=1e-9)
    assert peq.coefficient_convention == 'a0_normalized'
    assert peq.coefficient_ordering == 'b0,b1,b2,a1,a2'
    assert peq.sign_convention == 'denominator=1+a1*z^-1+a2*z^-2'


def test_magnitude_only_measurement_supports_simple_peq_without_phase_invention(tmp_path: Path) -> None:
    revision, variant, measurements, quality, _variants, _calibration = _repositories(tmp_path)
    measurement, dataset = _save_measurement(measurements, revision, 'magnitude-only')
    report = _save_quality(
        quality,
        measurement,
        dataset,
        report_id='quality-magnitude-only',
        usable_band=True,
    )
    plan = _plan(revision, variant, measurement, dataset, report, _channel(peq=(_peq(),)))

    assert report.capability('magnitude_response').decision == 'ALLOWED'
    assert report.capability('phase_response').decision == 'BLOCKED'
    assert report.capability('common_timing').decision == 'UNKNOWN'
    assert plan.support_state == 'SUPPORTED'


def test_common_timing_measurement_supports_gain_delay_polarity_crossover_plan(tmp_path: Path) -> None:
    revision, variant, measurements, quality, _variants, _calibration = _repositories(tmp_path)
    measurement, dataset = _save_measurement(measurements, revision, 'timed', phase=True)
    report = _save_quality(
        quality,
        measurement,
        dataset,
        report_id='quality-timed',
        common_timing=True,
        polarity=True,
    )
    channel = _channel(
        gain_db=-2.0,
        delay_s=0.002,
        polarity='inverted',
        crossovers=(
            CadCrossoverSetting(
                crossover_type='high_pass',
                frequency_hz=80.0,
                filter_order=4,
            ),
        ),
    )
    plan = _plan(revision, variant, measurement, dataset, report, channel)

    assert report.capability('common_timing').decision == 'ALLOWED'
    assert report.capability('polarity').decision == 'ALLOWED'
    assert plan.support_state == 'SUPPORTED'


def test_capability_shortage_blocks_absolute_delay(tmp_path: Path) -> None:
    revision, variant, measurements, quality, _variants, _calibration = _repositories(tmp_path)
    measurement, dataset = _save_measurement(measurements, revision, 'no-timing')
    report = _save_quality(
        quality,
        measurement,
        dataset,
        report_id='quality-no-timing',
    )
    plan = _plan(
        revision,
        variant,
        measurement,
        dataset,
        report,
        _channel(delay_s=0.003),
    )

    assert plan.support_state == 'UNSUPPORTED'
    assert any('absolute delay requires established common timing' in reason for reason in plan.unsupported_reasons)


def test_all_pass_remains_unsupported_without_coherent_phase_correction_authority(tmp_path: Path) -> None:
    revision, variant, measurements, quality, _variants, _calibration = _repositories(tmp_path)
    measurement, dataset = _save_measurement(measurements, revision, 'all-pass', phase=True)
    report = _save_quality(
        quality,
        measurement,
        dataset,
        report_id='quality-all-pass',
        common_timing=True,
    )
    device = _device(supported_filter_types=('peaking', 'all_pass'))
    plan = _plan(
        revision,
        variant,
        measurement,
        dataset,
        report,
        _channel(peq=(_peq(filter_type='all_pass'),)),
        device_constraints=device,
    )

    assert plan.support_state == 'UNSUPPORTED'
    assert any('coherent inter-channel phase correction authority' in reason for reason in plan.unsupported_reasons)


def test_device_filter_count_overflow_is_not_silently_omitted(tmp_path: Path) -> None:
    revision, variant, measurements, quality, _variants, _calibration = _repositories(tmp_path)
    measurement, dataset = _save_measurement(measurements, revision, 'filter-count')
    report = _save_quality(quality, measurement, dataset, report_id='quality-filter-count')
    filters = (
        _peq(filter_id='peq-a', frequency_hz=80.0),
        _peq(filter_id='peq-b', frequency_hz=120.0),
    )
    plan = _plan(
        revision,
        variant,
        measurement,
        dataset,
        report,
        _channel(peq=filters),
        device_constraints=_device(max_filters_per_channel=1),
    )

    assert plan.support_state == 'UNSUPPORTED'
    assert any('filter count 2 exceeds device maximum 1' in reason for reason in plan.unsupported_reasons)
    with pytest.raises(ValueError, match='unsupported CalibrationPlan cannot be exported'):
        build_generic_biquad_export(
            plan,
            export_id='export-filter-count',
            created_at_utc='2026-09-19T12:34:00+00:00',
        )


def test_max_boost_cut_violation_is_not_silently_clipped(tmp_path: Path) -> None:
    revision, variant, measurements, quality, _variants, _calibration = _repositories(tmp_path)
    measurement, dataset = _save_measurement(measurements, revision, 'boost')
    report = _save_quality(quality, measurement, dataset, report_id='quality-boost')
    plan = _plan(
        revision,
        variant,
        measurement,
        dataset,
        report,
        _channel(peq=(_peq(gain_db=7.0),)),
    )

    assert plan.support_state == 'UNSUPPORTED'
    assert any('exceeds plan maximum' in reason for reason in plan.unsupported_reasons)

    cut_plan = _plan(
        revision,
        variant,
        measurement,
        dataset,
        report,
        _channel(peq=(_peq(gain_db=-13.0, filter_id='peq-cut'),)),
    )
    assert cut_plan.support_state == 'UNSUPPORTED'
    assert any('exceeds plan maximum' in reason for reason in cut_plan.unsupported_reasons)


def test_generic_export_round_trip_preserves_exact_exported_transfer(tmp_path: Path) -> None:
    revision, variant, measurements, quality, _variants, _calibration = _repositories(tmp_path)
    measurement, dataset = _save_measurement(measurements, revision, 'export')
    report = _save_quality(quality, measurement, dataset, report_id='quality-export')
    plan = _plan(revision, variant, measurement, dataset, report, _channel(peq=(_peq(),)))
    snapshot = build_generic_biquad_export(
        plan,
        export_id='export-1',
        created_at_utc='2026-09-19T12:34:00+00:00',
    )
    text = render_generic_biquad_json(snapshot)
    readback = read_generic_biquad_json(text)

    assert readback == snapshot
    assert readback.exported_settings_semantic_sha256 == snapshot.exported_settings_semantic_sha256
    original = snapshot.channels[0].peq[0]
    restored = readback.channels[0].peq[0]
    for frequency in (20.0, 80.0, 1000.0, 10000.0):
        assert evaluate_biquad_db(restored, frequency) == pytest.approx(
            evaluate_biquad_db(original, frequency),
            abs=1e-12,
        )
    csv_text = render_generic_biquad_csv(snapshot)
    assert 'b0,b1,b2,a1,a2' in csv_text
    assert 'filter_type' in csv_text
    assert 'peq-1' in csv_text


def test_quantized_export_is_separate_and_re_evaluated(tmp_path: Path) -> None:
    revision, variant, measurements, quality, _variants, _calibration = _repositories(tmp_path)
    measurement, dataset = _save_measurement(measurements, revision, 'quantized')
    report = _save_quality(quality, measurement, dataset, report_id='quality-quantized')
    requested = _peq(gain_db=1.26, frequency_hz=63.2, q=0.93)
    device = _device(
        frequency_resolution_hz=1.0,
        q_resolution=0.1,
        filter_gain_resolution_db=0.5,
    )
    plan = _plan(
        revision,
        variant,
        measurement,
        dataset,
        report,
        _channel(peq=(requested,)),
        device_constraints=device,
    )
    snapshot = build_generic_biquad_export(
        plan,
        export_id='export-quantized',
        created_at_utc='2026-09-19T12:35:00+00:00',
    )
    actual = snapshot.channels[0].peq[0]

    assert snapshot.quantization_applied
    assert actual.frequency_hz == 63.0
    assert actual.q == 0.9
    assert actual.gain_db == 1.5
    assert actual.coefficients != requested.coefficients
    assert snapshot.requested_plan_semantic_sha256 == plan.plan_semantic_sha256
    assert snapshot.exported_settings_semantic_sha256 != plan.plan_semantic_sha256
    assert evaluate_biquad_db(actual, 63.0) == pytest.approx(1.5, abs=1e-9)


def test_verification_measurement_plan_keeps_exact_export_scene_system_and_before_after_lineage(tmp_path: Path) -> None:
    revision, variant, measurements, quality, _variants, calibration = _repositories(tmp_path)
    before, dataset = _save_measurement(measurements, revision, 'before')
    report = _save_quality(quality, before, dataset, report_id='quality-before')
    after, _after_dataset = _save_measurement(measurements, revision, 'after')
    plan = _plan(revision, variant, before, dataset, report, _channel(peq=(_peq(),)))
    calibration.save_plan(plan)
    snapshot = build_generic_biquad_export(
        plan,
        export_id='export-verification',
        created_at_utc='2026-09-19T12:36:00+00:00',
    )
    calibration.save_export(snapshot)
    verification = build_verification_measurement_plan(
        plan=plan,
        exported_settings=snapshot,
        measurement_points=(
            CadVerificationMeasurementPoint(
                point_id='point-mlp',
                position=Position3(x_m=3.0, y_m=3.0, z_m=1.1),
            ),
        ),
        routing=('FL',),
        reference_level_db_spl=75.0,
        required_measurement_capabilities=('magnitude_response', 'repeatability'),
        before_measurement_ids=(before.measurement_id,),
        after_measurement_ids=(after.measurement_id,),
        verification_plan_id='verification-1',
        created_at_utc='2026-09-19T12:37:00+00:00',
    )
    calibration.save_verification_plan(verification)

    reopened = CadCalibrationRepository(
        scene_repository=calibration.scene_repository,
        system_variant_repository=calibration.system_variant_repository,
        measurement_repository=measurements,
        quality_repository=quality,
    )
    assert reopened.get_verification_plan('verification-1') == verification
    assert verification.before_measurement_ids == ('before',)
    assert verification.after_measurement_ids == ('after',)


def test_save_reopen_preserves_plan_export_lifecycle_semantic_identity(tmp_path: Path) -> None:
    revision, variant, measurements, quality, variants, calibration = _repositories(tmp_path)
    measurement, dataset = _save_measurement(measurements, revision, 'reopen')
    report = _save_quality(quality, measurement, dataset, report_id='quality-reopen')
    plan = _plan(revision, variant, measurement, dataset, report, _channel(peq=(_peq(),)))
    calibration.save_plan(plan)
    snapshot = build_generic_biquad_export(
        plan,
        export_id='export-reopen',
        created_at_utc='2026-09-19T12:38:00+00:00',
    )
    calibration.save_export(snapshot)
    exported_event = build_calibration_lifecycle_event(
        plan=plan,
        state='exported',
        exported_settings=snapshot,
        event_id='lifecycle-exported',
        created_at_utc='2026-09-19T12:39:00+00:00',
    )
    calibration.save_lifecycle_event(exported_event)

    reopened = CadCalibrationRepository(
        scene_repository=calibration.scene_repository,
        system_variant_repository=variants,
        measurement_repository=measurements,
        quality_repository=quality,
    )
    assert reopened.get_plan(plan.plan_id) == plan
    assert reopened.get_export(snapshot.export_id) == snapshot
    assert reopened.list_lifecycle_events(plan.plan_id) == (exported_event,)
    assert reopened.list_lifecycle_events(plan.plan_id)[0].state == 'exported'


def test_quality_report_hash_mismatch_is_rejected_on_persistence(tmp_path: Path) -> None:
    revision, variant, measurements, quality, _variants, calibration = _repositories(tmp_path)
    measurement, dataset = _save_measurement(measurements, revision, 'hash-mismatch')
    report = _save_quality(quality, measurement, dataset, report_id='quality-hash-mismatch')
    plan = _plan(revision, variant, measurement, dataset, report, _channel(peq=(_peq(),)))

    payload = plan.model_dump(mode='python')
    payload['measurement_quality_report_sha256'] = '0' * 64
    provisional = plan.model_copy(
        update={
            'measurement_quality_report_sha256': '0' * 64,
            'plan_semantic_sha256': '0' * 64,
        }
    )
    payload['plan_semantic_sha256'] = _semantic_hash(provisional.semantic_payload())
    tampered = type(plan).model_validate(payload)

    with pytest.raises(ValueError, match='MeasurementQualityReport hash mismatch'):
        calibration.save_plan(tampered)


def test_generic_biquad_csv_neutralizes_formula_prefixed_identifiers(tmp_path: Path) -> None:
    revision, variant, measurements, quality, _variants, _calibration = _repositories(tmp_path)
    measurement, dataset = _save_measurement(measurements, revision, 'injection')
    report = _save_quality(quality, measurement, dataset, report_id='quality-injection')
    plan = _plan(revision, variant, measurement, dataset, report, _channel(peq=(_peq(),)))
    snapshot = build_generic_biquad_export(
        plan,
        export_id='export-injection',
        created_at_utc='2026-09-19T12:37:00+00:00',
    )
    channel = snapshot.channels[0]
    dangerous = snapshot.model_copy(update={
        'channels': (
            channel.model_copy(update={
                'channel_id': '=cmd|"/c calc"!A0',
                'role_id': '@role',
                'physical_output_id': '\t=out-fl',
                'peq': (
                    channel.peq[0].model_copy(update={'filter_id': '=evil-filter'}),
                ),
            }),
        ),
    })

    csv_text = render_generic_biquad_csv(dangerous)
    rows = [row for row in csv.reader(io.StringIO(csv_text)) if row]
    assert rows[0] == [
        'channel_id', 'role_id', 'physical_output_id', 'channel_gain_db',
        'delay_s', 'polarity', 'filter_index', 'filter_id', 'filter_type',
        'frequency_hz', 'q', 'filter_gain_db', 'b0', 'b1', 'b2', 'a1', 'a2',
    ]
    data = rows[1]
    assert data[0] == '\'=cmd|"/c calc"!A0'
    assert data[1] == "'@role"
    assert data[2] == "'\t=out-fl"
    assert data[7] == "'=evil-filter"
    assert data[8] == 'peaking'
    # Numeric cells remain plain parseable literals.
    assert float(data[3]) == channel.gain_db
    for cell in data:
        candidate = cell.lstrip()
        assert not candidate or candidate[0] not in ('=', '+', '@')
