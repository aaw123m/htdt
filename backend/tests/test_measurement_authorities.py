"""Tests for the measurement authority family (#471-#474, #599, #642/643/645, #659)."""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import json

import pytest
from pydantic import ValidationError

from htdt.cad_measurement_authorities import (
    POLARITY_INVERSION_TOKEN,
    build_acoustic_level_calibration,
    build_dataset_level_reference,
    build_routing_profile,
    build_timing_reference,
    build_wiring_check,
    calibration_supports_absolute_spl,
    latest_wiring_checks,
    net_polarity_state,
    routing_profile_staleness,
    timing_clocks_shared,
    timing_reference_supports_common_timing,
)
from htdt.cad_measurement_ir import (
    RewIrParseError,
    ir_observation_fields,
    normalize_rew_ir_text,
    parse_rew_impulse_response,
)
from htdt.cad_measurement_quality import (
    CadMicrophoneCapture,
    CadPlaybackCapture,
    build_acquisition_context,
    build_measurement_observation,
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
from htdt.cad_measurement_models import CadFrequencyResponseDataset
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Direction3,
    Offset3,
    Position3,
    SceneEntity,
    Size3,
    make_f1_scene,
)
from htdt.cad_measurement_targets import (
    MeasurementTargetError,
    derive_measurement_point_document,
    measurement_target_drift,
)
from htdt.measurement_workflow import (
    AcquisitionCapture,
    MeasurementAssignment,
    MeasurementWorkflowController,
    MeasurementWorkflowError,
)
from htdt.rew_api import RewApiClient


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
):
    raw = declared_fr_raw(
        frequency_hz=(20.0, 40.0, 80.0),
        level_db=(70.0, 71.0, 69.0),
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
        level_reference='unknown',
        processing_json='{}',
        source_sha256=sha256(raw).hexdigest(),
        importer_version=HTDT_DECLARED_IMPORTER_VERSION,
    )
    repository.save(
        record, dataset, raw_filename=f'{measurement_id}.json', raw_bytes=raw
    )
    return record, dataset


def _scene_with_seat(document):
    return document.model_copy(
        update={
            'entities': (
                *document.entities,
                SceneEntity(
                    entity_id='seat-1',
                    kind='seat',
                    name='Sofa',
                    position=Position3(x_m=3.0, y_m=2.6, z_m=0.5),
                    size_m=Size3(x_m=2.0, y_m=0.9, z_m=1.0),
                    acoustic_reference_offset_m=Offset3(x_m=0.0, y_m=0.0, z_m=0.6),
                ),
            )
        }
    )


# ---------------------------------------------------------------------------
# #642 timing-reference authority


def test_timing_reference_roundtrip(tmp_path):
    _, measurement_repository, quality_repository = _repositories(tmp_path)
    reference = build_timing_reference(
        method='loopback',
        reference_channel='output-2',
        input_clock_identity='umik-in',
        output_clock_identity='umik-out',
        sample_rate_hz=48000,
        t0_convention='loopback_edge',
        delay_corrections=(
            {'correction_kind': 'loopback_path', 'value_s': 0.0012},
        ),
        validity_scope='session',
    )
    quality_repository.save_timing_reference(reference)
    loaded = quality_repository.get_timing_reference(reference.timing_reference_id)
    assert loaded == reference
    assert timing_reference_supports_common_timing(reference)


def test_timing_reference_method_semantics():
    manual = build_timing_reference(method='manual')
    unknown = build_timing_reference(method='unknown')
    imported = build_timing_reference(method='imported')
    for reference in (manual, unknown, imported):
        assert not timing_reference_supports_common_timing(reference)
        assert not timing_clocks_shared(reference)


def test_shared_clock_requires_clock_identities():
    shared = build_timing_reference(
        method='shared_clock',
        input_clock_identity='rme-adi2',
        output_clock_identity='rme-adi2',
    )
    assert timing_clocks_shared(shared)
    # Same sample rate but different clocks is not a shared clock.
    split = build_timing_reference(
        method='shared_clock',
        input_clock_identity='rme-adi2',
        output_clock_identity='motu-m4',
    )
    assert not timing_clocks_shared(split)
    with pytest.raises(ValidationError, match='clock identities'):
        build_timing_reference(method='shared_clock', input_clock_identity=None)


def test_acquisition_context_binds_timing_reference(tmp_path):
    _, measurement_repository, quality_repository = _repositories(tmp_path)
    revision = measurement_repository.scene_repository.current_head(
        'fixture-f1'
    )
    record, _ = _save_measurement(measurement_repository, revision, 'm-1')
    reference = build_timing_reference(method='acoustic_reference')
    quality_repository.save_timing_reference(reference)

    context = build_acquisition_context(
        source_kind='native',
        subject_measurement_ids=(record.measurement_id,),
        timing_reference_valid=True,
        timing_reference_id=reference.timing_reference_id,
        timing_reference_sha256=reference.timing_reference_sha256,
    )
    quality_repository.save_acquisition_context(context)
    loaded = quality_repository.get_acquisition_context(
        context.acquisition_context_id
    )
    assert loaded.timing_reference_sha256 == reference.timing_reference_sha256

    # A context binding a timing reference that does not resolve fails closed.
    bad = build_acquisition_context(
        source_kind='native',
        subject_measurement_ids=(record.measurement_id,),
        timing_reference_sha256='a' * 64,
    )
    with pytest.raises(ValueError):
        quality_repository.save_acquisition_context(bad)


# ---------------------------------------------------------------------------
# #643 absolute SPL calibration authority


def test_level_calibration_roundtrip(tmp_path):
    _, _, quality_repository = _repositories(tmp_path)
    calibration = build_acoustic_level_calibration(
        method='acoustic_calibrator',
        instrument_identity='sc-05 sn-1234',
        reference_level_db_spl=94.0,
        reference_frequency_hz=1000.0,
        calibrated_at_utc='2026-09-20T00:00:00+00:00',
        validity_scope='instrument',
    )
    quality_repository.save_level_calibration(calibration)
    loaded = quality_repository.get_level_calibration(calibration.calibration_id)
    assert loaded == calibration
    assert calibration_supports_absolute_spl(calibration)


def test_manufacturer_sensitivity_is_not_absolute_spl():
    datasheet = build_acoustic_level_calibration(method='manufacturer_sensitivity')
    assert not calibration_supports_absolute_spl(datasheet)
    for method in ('imported', 'manual', 'unknown'):
        assert not calibration_supports_absolute_spl(
            build_acoustic_level_calibration(method=method)
        )
    for method in ('acoustic_calibrator', 'rew_spl_session', 'reference_meter_transfer'):
        assert calibration_supports_absolute_spl(
            build_acoustic_level_calibration(method=method)
        )


def test_dataset_level_reference_binding(tmp_path):
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    record, dataset = _save_measurement(measurement_repository, revision, 'm-1')
    calibration = build_acoustic_level_calibration(
        method='acoustic_calibrator',
        calibrated_at_utc='2026-09-20T00:00:00+00:00',
    )
    quality_repository.save_level_calibration(calibration)

    reference = build_dataset_level_reference(
        measurement_id=record.measurement_id,
        dataset_id=dataset.dataset_id,
        dataset_sha256=dataset.dataset_sha256,
        level_reference_kind='absolute_spl',
        calibration_id=calibration.calibration_id,
        calibration_sha256=calibration.calibration_sha256,
    )
    quality_repository.save_dataset_level_reference(reference)
    loaded = quality_repository.get_dataset_level_reference(dataset.dataset_id)
    assert loaded == reference


def test_absolute_spl_requires_supporting_calibration(tmp_path):
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    record, dataset = _save_measurement(measurement_repository, revision, 'm-1')
    # A manual declaration is not a calibration authority for absolute SPL.
    manual = build_acoustic_level_calibration(method='manual')
    quality_repository.save_level_calibration(manual)
    reference = build_dataset_level_reference(
        measurement_id=record.measurement_id,
        dataset_id=dataset.dataset_id,
        dataset_sha256=dataset.dataset_sha256,
        level_reference_kind='absolute_spl',
        calibration_id=manual.calibration_id,
        calibration_sha256=manual.calibration_sha256,
    )
    with pytest.raises(ValueError, match='absolute SPL'):
        quality_repository.save_dataset_level_reference(reference)


def test_non_absolute_level_reference_forbids_calibration(tmp_path):
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    record, dataset = _save_measurement(measurement_repository, revision, 'm-1')
    with pytest.raises(ValidationError):
        build_dataset_level_reference(
            measurement_id=record.measurement_id,
            dataset_id=dataset.dataset_id,
            dataset_sha256=dataset.dataset_sha256,
            level_reference_kind='dbfs',
            calibration_id='cal-1',
            calibration_sha256='b' * 64,
        )
    reference = build_dataset_level_reference(
        measurement_id=record.measurement_id,
        dataset_id=dataset.dataset_id,
        dataset_sha256=dataset.dataset_sha256,
        level_reference_kind='spl_uncalibrated',
    )
    quality_repository.save_dataset_level_reference(reference)
    assert (
        quality_repository.get_dataset_level_reference(dataset.dataset_id)
        == reference
    )


# ---------------------------------------------------------------------------
# #473 routing-profile authority


def _profile(**kwargs):
    return build_routing_profile(
        profile_name='main-theater',
        entries=(
            {
                'output_device_label': 'EXCL: DENON-AVR (WASAPI)',
                'rew_channel_label': 'C:FL',
                'hardware_channel_index': 0,
                'logical_role': 'front_left',
                'expected_speaker_ids': ('speaker-fl',),
                'observed_speaker_ids': ('speaker-fl',),
                'verification': 'verified',
                'verified_at_utc': '2026-09-20T00:00:00+00:00',
                'avr_configuration_id': 'avr-xt32-movie',
            },
            {
                'output_device_label': 'EXCL: DENON-AVR (WASAPI)',
                'rew_channel_label': 'C:FR',
                'hardware_channel_index': 1,
                'logical_role': 'front_right',
                'expected_speaker_ids': ('speaker-fr',),
                'observed_speaker_ids': ('speaker-fr',),
                'verification': 'verified',
                'verified_at_utc': '2026-09-20T00:00:00+00:00',
                'avr_configuration_id': 'avr-xt32-movie',
            },
        ),
        **kwargs,
    )


def test_routing_profile_roundtrip(tmp_path):
    _, _, quality_repository = _repositories(tmp_path)
    profile = _profile()
    quality_repository.save_routing_profile(profile)
    assert quality_repository.get_routing_profile(profile.routing_profile_id) == profile
    listed = quality_repository.list_routing_profiles()
    assert profile in listed
    entry = profile.entry_for_role('front_left')
    assert entry is not None
    assert entry.observed_speaker_ids == ('speaker-fl',)


def test_routing_profile_rejects_duplicate_paths():
    entry = {
        'output_device_label': 'dev',
        'rew_channel_label': 'C:FL',
        'hardware_channel_index': 0,
    }
    with pytest.raises(ValidationError, match='duplicate channel map'):
        build_routing_profile(entries=(entry, dict(entry)))


def test_routing_profile_staleness_reasons():
    profile = _profile()
    assert routing_profile_staleness(profile) == ()
    reasons = routing_profile_staleness(
        profile, avr_configuration_id='avr-stereo-direct'
    )
    assert len(reasons) == 2  # one reason per affected entry
    reasons = routing_profile_staleness(
        profile, output_device_labels=('Other device',)
    )
    assert any('output device label' in reason for reason in reasons)
    reasons = routing_profile_staleness(
        profile, speaker_ids=('speaker-fl',)  # speaker-fr removed
    )
    assert any('topology' in reason for reason in reasons)


def test_multi_band_entries_are_representable():
    profile = build_routing_profile(
        entries=(
            {
                'output_device_label': 'dev',
                'rew_channel_label': 'C:FL',
                'hardware_channel_index': 0,
                'logical_role': 'front_left',
                'verified_band_hz': (80.0, 20000.0),
            },
            {
                'output_device_label': 'dev',
                'rew_channel_label': 'C:FL',
                'hardware_channel_index': 0,
                'logical_role': 'subwoofer',
                'verified_band_hz': (20.0, 80.0),
            },
        )
    )
    assert len(profile.entries) == 2


def test_assignment_resolves_routing_profile(tmp_path):
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    controller = MeasurementWorkflowController(
        measurement_repository.scene_repository,
        'fixture-f1',
        measurement_repository=measurement_repository,
        quality_repository=quality_repository,
        rew_client=object(),  # no REW available; engine_session stays absent
    )
    profile = _profile()
    quality_repository.save_routing_profile(profile)

    raw = b'freq level\n20.0 70.0\n40.0 71.0\n80.0 69.0\n'
    controller.stage_rew_text(raw, 'fl.txt')
    record = controller.commit_pending(
        MeasurementAssignment(
            measurement_entity_id='point-mlp',
            channel_role='front_left',
            routing_evidence='verified',
            routing_profile_id=profile.routing_profile_id,
        )
    )
    provenance = json.loads(record.provenance_json)
    assert (
        provenance['routing_profile']['routing_profile_sha256']
        == profile.routing_profile_sha256
    )
    # The profile's verified observation supplied the source speakers.
    assert record.source_speaker_ids == ('speaker-fl',)


# ---------------------------------------------------------------------------
# #645 wiring commissioning checks


def _wiring_check(**kwargs):
    kwargs.setdefault('check_kind', 'continuity')
    kwargs.setdefault('method', 'DCR probe')
    kwargs.setdefault('result', 'PASS')
    kwargs.setdefault('measured_at_utc', '2026-09-20T00:00:00+00:00')
    return build_wiring_check(document_id='fixture-f1', **kwargs)


def test_wiring_check_roundtrip(tmp_path):
    _, _, quality_repository = _repositories(tmp_path)
    check = _wiring_check(
        expected_output_reference='AVR preout FL',
        expected_speaker_ids=('speaker-fl',),
        observed_output_reference='AVR preout FL',
        observed_speaker_ids=('speaker-fl',),
        operator='tester',
    )
    quality_repository.save_wiring_check(check)
    assert quality_repository.get_wiring_check(check.check_id) == check
    assert check in quality_repository.list_wiring_checks('fixture-f1')


def test_wiring_fail_requires_reason():
    with pytest.raises(ValidationError, match='reason'):
        _wiring_check(result='FAIL')
    with pytest.raises(ValidationError, match='reason'):
        _wiring_check(result='UNKNOWN', reason='  ')
    check = _wiring_check(result='FAIL', reason='open circuit on FL pair')
    assert check.result == 'FAIL'


def test_latest_wiring_checks_per_kind(tmp_path):
    _, _, quality_repository = _repositories(tmp_path)
    continuity_old = _wiring_check(
        check_kind='continuity', measured_at_utc='2026-09-19T00:00:00+00:00'
    )
    continuity_new = _wiring_check(
        check_kind='continuity', measured_at_utc='2026-09-20T00:00:00+00:00'
    )
    polarity = _wiring_check(check_kind='terminal_polarity', result='FAIL',
                             reason='wired reversed at binding posts')
    quality_repository.save_wiring_check(continuity_old)
    quality_repository.save_wiring_check(continuity_new)
    quality_repository.save_wiring_check(polarity)
    latest = latest_wiring_checks(
        quality_repository.list_wiring_checks('fixture-f1')
    )
    assert latest['continuity'].check_id == continuity_new.check_id
    assert latest['terminal_polarity'].result == 'FAIL'


def test_net_polarity_state_composes_inversions():
    # No inversion anywhere: correct terminal polarity and positive acoustic
    # impulse match the expected sign.
    assert net_polarity_state(
        terminal_polarity_correct=True,
        applied_compensation=(),
        acoustic_polarity_correct=True,
    ) == 'PASS'
    # One intended DSP inversion on correctly-wired terminals should read
    # inverted acoustically.
    assert net_polarity_state(
        terminal_polarity_correct=True,
        applied_compensation=(POLARITY_INVERSION_TOKEN,),
        acoustic_polarity_correct=False,
    ) == 'PASS'
    # The same DSP inversion on inverted terminals nets to positive.
    assert net_polarity_state(
        terminal_polarity_correct=False,
        applied_compensation=(POLARITY_INVERSION_TOKEN,),
        acoustic_polarity_correct=True,
    ) == 'PASS'
    # Mismatched composition fails.
    assert net_polarity_state(
        terminal_polarity_correct=True,
        applied_compensation=(),
        acoustic_polarity_correct=False,
    ) == 'FAIL'
    # Missing observations stay UNKNOWN rather than collapsing into PASS.
    assert net_polarity_state(
        terminal_polarity_correct=None,
        applied_compensation=(),
        acoustic_polarity_correct=True,
    ) == 'UNKNOWN'


# ---------------------------------------------------------------------------
# #471 acquisition context: microphone/playback/direction + presets


def test_acquisition_context_microphone_playback(tmp_path):
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    record, _ = _save_measurement(measurement_repository, revision, 'm-1')
    mic = CadMicrophoneCapture(
        manufacturer='miniDSP',
        model='UMIK-1',
        serial='712-3456',
        sample_rate_hz=48000,
        calibration_profile='90deg',
        calibration_filename='712-3456_90deg.txt',
        calibration_sha256='c' * 64,
        direction=Direction3(x=0.0, y=-1.0, z=0.0),
    )
    playback = CadPlaybackCapture(
        output_device_label='EXCL: DENON-AVR (WASAPI)',
        avr_manufacturer='Denon',
        avr_model='AVR-X3800H',
        avr_firmware='1310-9317-1105',
        avr_input_name='AUX1',
        avr_volume_db=-15.0,
        avr_processing_mode='stereo',
        avr_peq_mode='audyssey_flat',
    )
    context = build_acquisition_context(
        source_kind='native',
        subject_measurement_ids=(record.measurement_id,),
        microphone=mic,
        playback=playback,
        measurement_direction=Direction3(x=0.0, y=-1.0, z=0.0),
    )
    quality_repository.save_acquisition_context(context)
    loaded = quality_repository.get_acquisition_context(
        context.acquisition_context_id
    )
    assert loaded == context
    assert loaded.microphone.calibration_profile == '90deg'
    assert loaded.playback.avr_volume_db == -15.0
    # The instrument/playback fields are inside the sealed identity.
    assert 'microphone' in context.identity_payload()
    assert 'playback' in context.identity_payload()


def test_legacy_context_identity_hash_unchanged(tmp_path):
    """Optional #471 fields excluded from the seal when absent (#471)."""
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    record, _ = _save_measurement(measurement_repository, revision, 'm-1')
    context = build_acquisition_context(
        source_kind='manual',
        subject_measurement_ids=(record.measurement_id,),
        created_at_utc='2026-09-19T00:00:00+00:00',
    )
    payload = context.identity_payload()
    assert 'microphone' not in payload
    assert 'playback' not in payload
    assert 'measurement_direction' not in payload
    assert 'timing_reference_sha256' not in payload


def test_commit_persists_acquisition_capture(tmp_path):
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    controller = MeasurementWorkflowController(
        measurement_repository.scene_repository,
        'fixture-f1',
        measurement_repository=measurement_repository,
        quality_repository=quality_repository,
        rew_client=object(),
    )
    raw = b'20.0 70.0\n40.0 71.0\n80.0 69.0\n'
    controller.stage_rew_text(raw, 'fl.txt')
    record = controller.commit_pending(
        MeasurementAssignment(
            measurement_entity_id='point-mlp',
            channel_role='front_left',
            source_speaker_ids=('speaker-fl',),
            measurement_direction=Direction3(x=0.0, y=0.0, z=1.0),
            acquisition=AcquisitionCapture(
                source_kind='manual',
                microphone=CadMicrophoneCapture(
                    model='UMIK-1',
                    calibration_profile='0deg',
                ),
                playback=CadPlaybackCapture(
                    output_device_label='EXCL: DENON-AVR',
                    avr_volume_db=-20.0,
                ),
                sample_rate_hz=48000,
            ),
        )
    )
    contexts = controller.acquisition_contexts()
    assert len(contexts) == 1
    context = contexts[0]
    assert context.subject_measurement_ids == (record.measurement_id,)
    assert context.microphone.model == 'UMIK-1'
    assert context.measurement_direction.z == 1.0
    # A later commit can reuse it as a preset.
    capture = AcquisitionCapture.from_context(context)
    assert capture.microphone.model == 'UMIK-1'
    assert capture.sample_rate_hz == 48000


# ---------------------------------------------------------------------------
# #474 measured IR workflow


def _ir_text() -> bytes:
    return (
        b'Impulse response export\n'
        b'Sample rate: 48000\n'
        b'0.0000000000 0.0001\n'
        b'0.0000208333 0.0500\n'
        b'0.0000416667 0.2000\n'
        b'0.0000625000 -0.0500\n'
        b'0.0000833333 -0.0100\n'
    )


def test_parse_rew_ir_two_column():
    parsed = parse_rew_impulse_response(_ir_text())
    assert len(parsed.amplitudes) == 5
    assert parsed.sample_rate_hz == pytest.approx(48000.0, rel=1e-4)
    assert parsed.time_s[0] == 0.0
    assert parsed.header_lines
    assert not parsed.warnings


def test_parse_rew_ir_amplitude_only_needs_declared_rate():
    raw = b'0.0\n0.5\n1.0\n-0.2\n0.0\n'
    parsed = parse_rew_impulse_response(raw)
    assert parsed.sample_rate_hz is None
    assert 'ir_time_axis_unresolved' in parsed.warnings[0]
    with pytest.raises(RewIrParseError, match='sample rate'):
        normalize_rew_ir_text('m-1', raw, filename='ir.txt')
    dataset, _, _ = normalize_rew_ir_text(
        'm-1', raw, filename='ir.txt', sample_rate_hz=48000.0
    )
    assert dataset.sample_rate_hz == 48000.0
    assert dataset.start_time_s == 0.0


def test_parse_rew_ir_rejects_nonuniform_timing():
    raw = b'0.0 0.0\n0.001 0.5\n0.003 1.0\n'
    with pytest.raises(RewIrParseError, match='not uniform'):
        parse_rew_impulse_response(raw)


def test_ir_dataset_save_and_verify(tmp_path):
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    record, _ = _save_measurement(measurement_repository, revision, 'm-1')
    dataset, filename, raw = normalize_rew_ir_text(
        record.measurement_id,
        _ir_text(),
        filename='ir.txt',
        t0_semantics='export_t0',
        amplitude_reference='normalized',
        normalized=True,
        window_kind='tukey_0.25',
        calibration_state='uncalibrated',
    )
    measurement_repository.save_ir_dataset(dataset, raw_filename=filename, raw_bytes=raw)
    loaded = measurement_repository.get_ir_dataset(dataset.dataset_id)
    assert loaded == dataset
    assert loaded.t0_semantics == 'export_t0'
    assert loaded.amplitude_reference == 'normalized'
    assert loaded.calibration_state == 'uncalibrated'
    fields = ir_observation_fields(loaded)
    assert fields['has_impulse_response'] is True
    assert fields['ir_window_start_s'] == 0.0
    assert fields['ir_window_end_s'] > 0.0


def test_ir_verify_rejects_tampered_dataset(tmp_path):
    revision, measurement_repository, _ = _repositories(tmp_path)
    record, _ = _save_measurement(measurement_repository, revision, 'm-1')
    dataset, filename, raw = normalize_rew_ir_text(
        record.measurement_id, _ir_text(), filename='ir.txt'
    )
    tampered = dataset.model_copy(
        update={'amplitudes': (0.0,) * len(dataset.amplitudes)}
    )
    with pytest.raises(ValueError, match='canonical import transformation'):
        measurement_repository.save_ir_dataset(tampered, raw_filename=filename, raw_bytes=raw)
    with pytest.raises(ValueError, match='SHA-256'):
        measurement_repository.save_ir_dataset(dataset, raw_filename=filename, raw_bytes=b'other raw')


def test_ir_machine_observation_pins_ir_asset(tmp_path):
    """Machine observation can pin the IR raw asset, not only the FR one (#474)."""
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    record, _ = _save_measurement(measurement_repository, revision, 'm-1')
    raw = _ir_text()
    dataset, filename, _ = normalize_rew_ir_text(
        record.measurement_id, raw, filename='ir.txt'
    )
    measurement_repository.save_ir_dataset(dataset, raw_filename=filename, raw_bytes=raw)
    fields = ir_observation_fields(dataset)
    observation = build_measurement_observation(
        measurement_id=record.measurement_id,
        source_kind='raw_asset',
        source_asset_sha256=sha256(raw).hexdigest(),
        observed_at_utc='2026-09-20T00:00:00+00:00',
        **fields,
    )
    quality_repository.save_observation(observation)


def test_import_ir_workflow_binds_same_measurement(tmp_path):
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    controller = MeasurementWorkflowController(
        measurement_repository.scene_repository,
        'fixture-f1',
        measurement_repository=measurement_repository,
        quality_repository=quality_repository,
        rew_client=object(),
    )
    raw_fr = b'20.0 70.0\n40.0 71.0\n80.0 69.0\n'
    controller.stage_rew_text(raw_fr, 'fl.txt')
    record = controller.commit_pending(
        MeasurementAssignment(
            measurement_entity_id='point-mlp',
            channel_role='front_left',
            source_speaker_ids=('speaker-fl',),
        )
    )
    dataset = controller.import_ir_for_measurement(
        record.measurement_id,
        _ir_text(),
        filename='fl-ir.txt',
    )
    assert dataset.measurement_id == record.measurement_id
    listed = controller.ir_datasets_for_measurement(record.measurement_id)
    assert listed == (dataset,)


# ---------------------------------------------------------------------------
# #599 REW engine session contract


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def getheader(self, name):
        return None

    def read(self, n=-1):
        return self._payload


def _fake_opener(mapper):
    def open_(request, timeout=None):
        url = request.full_url if hasattr(request, 'full_url') else str(request)
        path = url.split('127.0.0.1:4735', 1)[-1]
        payload = mapper[path.split('?', 1)[0]]
        return _FakeResponse(json.dumps(payload).encode('utf-8'))
    return open_


def test_engine_session_reports_version_and_capabilities():
    client = RewApiClient(
        opener=_fake_opener({
            '/version': {'status': True, 'message': 'REW V5.40.beta'},
            '/measurements': [{'uuid': 'u-1', 'title': 'FL'}],
        })
    )
    session = client.engine_session()
    assert session.engine_kind == 'rew'
    assert session.engine_version == 'REW V5.40.beta'
    assert session.capability_snapshot['capabilities'] == {
        'version': True,
        'list_measurements': True,
        'frequency_response': True,
    }
    assert session.capability_snapshot['measurement_count'] == 1
    assert session.adapter_version == 'rew-api-snapshot-1'
    assert len(session.semantic_sha256) == 64


def test_status_reports_producer_contract():
    client = RewApiClient(
        opener=_fake_opener({
            '/version': {'status': True, 'message': 'REW V5.40.beta'},
            '/measurements': [],
        })
    )
    status = client.status()
    assert status['connected'] is True
    assert status['rew_version'] == 'REW V5.40.beta'
    assert status['capabilities']['frequency_response'] is True
    assert status['adapter_versions']['rew_api_snapshot'] == 'rew-api-snapshot-1'


def test_commit_binds_engine_session_provenance(tmp_path):
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    client = RewApiClient(
        opener=_fake_opener({
            '/version': {'status': True, 'message': 'REW V5.40.beta'},
            '/measurements': [],
        })
    )
    controller = MeasurementWorkflowController(
        measurement_repository.scene_repository,
        'fixture-f1',
        measurement_repository=measurement_repository,
        quality_repository=quality_repository,
        rew_client=client,
    )
    raw = b'20.0 70.0\n40.0 71.0\n80.0 69.0\n'
    controller.stage_rew_text(raw, 'fl.txt')
    record = controller.commit_pending(
        MeasurementAssignment(
            measurement_entity_id='point-mlp',
            channel_role='front_left',
            source_speaker_ids=('speaker-fl',),
        )
    )
    provenance = json.loads(record.provenance_json)
    session = provenance['engine_session']
    assert session['engine_kind'] == 'rew'
    assert session['engine_version'] == 'REW V5.40.beta'
    assert session['endpoint'] == client.base_url
    assert len(session['semantic_sha256']) == 64


# ---------------------------------------------------------------------------
# #472 seat-derived measurement targets


def test_derive_measurement_point_from_seat(tmp_path):
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    scene_repository = measurement_repository.scene_repository
    revision = scene_repository.save(
        _scene_with_seat(revision.document),
        parent_revision_id=revision.revision_id,
    ).revision
    controller = MeasurementWorkflowController(
        scene_repository,
        'fixture-f1',
        measurement_repository=measurement_repository,
        quality_repository=quality_repository,
        rew_client=object(),
    )
    lineage = controller.derive_measurement_point_from_seat(
        'seat-1', measurement_point_id='point-sofa'
    )
    head = scene_repository.current_head('fixture-f1')
    point = head.document.entity('point-sofa')
    assert point.kind == 'measurement_point'
    seat = head.document.entity('seat-1')
    from htdt.cad_scene import acoustic_reference_position

    expected = acoustic_reference_position(seat)
    assert point.position == expected
    assert lineage.source_seat_id == 'seat-1'
    assert lineage.initial_position == expected
    assert quality_repository.get_target_lineage('point-sofa') == lineage


def test_derived_point_survives_seat_move_and_reports_drift(tmp_path):
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    scene_repository = measurement_repository.scene_repository
    revision = scene_repository.save(
        _scene_with_seat(revision.document),
        parent_revision_id=revision.revision_id,
    ).revision
    controller = MeasurementWorkflowController(
        scene_repository,
        'fixture-f1',
        measurement_repository=measurement_repository,
        quality_repository=quality_repository,
        rew_client=object(),
    )
    lineage = controller.derive_measurement_point_from_seat(
        'seat-1', measurement_point_id='point-sofa'
    )

    # Move the seat 0.5 m forward — the point keeps its stored position.
    head = scene_repository.current_head('fixture-f1')
    moved = head.document.model_copy(
        update={
            'entities': tuple(
                entity.model_copy(
                    update={'position': Position3(x_m=3.0, y_m=2.1, z_m=0.5)}
                )
                if entity.entity_id == 'seat-1'
                else entity
                for entity in head.document.entities
            )
        }
    )
    scene_repository.save(moved, parent_revision_id=head.revision_id)

    point = scene_repository.current_head('fixture-f1').document.entity(
        'point-sofa'
    )
    assert point.position == lineage.initial_position  # history untouched

    drift = measurement_target_drift(scene_repository, lineage)
    assert drift is not None
    assert drift.drift_m == pytest.approx(0.5)
    assert drift.initial_drift_m == pytest.approx(0.0)


def test_derive_rejects_seat_without_reference(tmp_path):
    revision, measurement_repository, _ = _repositories(tmp_path)
    bare_seat = SceneEntity(
        entity_id='seat-bare',
        kind='seat',
        name='Bench',
        position=Position3(x_m=1.0, y_m=1.0, z_m=0.45),
        size_m=Size3(x_m=1.2, y_m=0.5, z_m=0.5),
    )
    document = revision.document.model_copy(
        update={'entities': (*revision.document.entities, bare_seat)}
    )
    with pytest.raises(MeasurementTargetError, match='no acoustic reference'):
        derive_measurement_point_document(
            document, source_seat_id='seat-bare',
            measurement_point_id='point-x',
        )


# ---------------------------------------------------------------------------
# #659 explicit acquisition-time SceneRevision selection


def _fr_raw() -> bytes:
    return b'20.0 70.0\n40.0 71.0\n80.0 69.0\n'


def _controller(tmp_path):
    revision, measurement_repository, quality_repository = _repositories(tmp_path)
    controller = MeasurementWorkflowController(
        measurement_repository.scene_repository,
        'fixture-f1',
        measurement_repository=measurement_repository,
        quality_repository=quality_repository,
        rew_client=object(),
    )
    return controller, revision


def _move_speaker(controller):
    """Commit a new head revision that moves speaker-fl."""
    head = controller.scene_repository.current_head('fixture-f1')
    moved = head.document.model_copy(
        update={
            'entities': tuple(
                entity.model_copy(
                    update={
                        'position': Position3(x_m=2.0, y_m=0.75, z_m=1.05)
                    }
                )
                if entity.entity_id == 'speaker-fl'
                else entity
                for entity in head.document.entities
            )
        }
    )
    return controller.scene_repository.save(
        moved, parent_revision_id=head.revision_id
    ).revision


def test_stage_against_explicit_past_revision(tmp_path):
    controller, first = _controller(tmp_path)
    second = _move_speaker(controller)
    pending = controller.stage_rew_text(
        _fr_raw(), 'fl.txt', scene_revision_id=first.revision_id
    )
    assert pending.scene_revision_id == first.revision_id
    assert pending.scene_revision_explicit is True
    assert controller.pending_divergence() is True

    # Committing to the acquired revision leaves the head untouched.
    record = controller.commit_pending(
        MeasurementAssignment(
            measurement_entity_id='point-mlp',
            channel_role='front_left',
            source_speaker_ids=('speaker-fl',),
        ),
        on_divergence='historical',
    )
    assert record.scene_revision_id == first.revision_id
    current = controller.scene_repository.current_head('fixture-f1')
    assert current.revision_id == second.revision_id


def test_default_commit_rejects_divergence(tmp_path):
    controller, first = _controller(tmp_path)
    controller.stage_rew_text(_fr_raw(), 'fl.txt')
    _move_speaker(controller)
    assert controller.pending_divergence() is True
    with pytest.raises(MeasurementWorkflowError, match='変更'):
        controller.commit_pending(
            MeasurementAssignment(measurement_entity_id='point-mlp')
        )
    # The import stays pending — the user can still choose.
    assert controller.pending_import is not None


def test_accept_current_rebinds_to_head(tmp_path):
    controller, first = _controller(tmp_path)
    controller.stage_rew_text(_fr_raw(), 'fl.txt')
    second = _move_speaker(controller)
    record = controller.commit_pending(
        MeasurementAssignment(
            measurement_entity_id='point-mlp',
            channel_role='front_left',
            source_speaker_ids=('speaker-fl',),
        ),
        on_divergence='accept_current',
    )
    assert record.scene_revision_id == second.revision_id


def test_select_pending_revision_rebinds(tmp_path):
    controller, first = _controller(tmp_path)
    _move_speaker(controller)
    controller.stage_rew_text(_fr_raw(), 'fl.txt')
    pending = controller.select_pending_revision(first.revision_id)
    assert pending.scene_revision_id == first.revision_id
    assert pending.scene_revision_explicit is True
    with pytest.raises(MeasurementWorkflowError):
        controller.select_pending_revision('not-a-revision')


def test_revision_options_newest_first(tmp_path):
    controller, first = _controller(tmp_path)
    second = _move_speaker(controller)
    options = controller.revision_options()
    assert options[0].revision_id == second.revision_id
    assert options[-1].revision_id == first.revision_id
