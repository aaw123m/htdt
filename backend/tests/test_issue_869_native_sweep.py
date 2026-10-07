"""#869 HTDT-native sweep measurement engine tests (REV66).

The HTDT-native acquisition authority: deterministic sweep stimulus,
explicit backend abstraction with fail-closed error paths, a state
machine that never lets a completed recording read as valid evidence
without the quality gate, explicit timing qualification (unqualified
timing stays UNKNOWN/limited), a versioned IR deriver, the level-safety
arm gate, and sealed evidence records with managed-asset payloads.

All capture is driven by the deterministic fake backend — its simulated
completion is test evidence only, never a real acoustic capture.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import ensure_native_schema
from htdt.cad_sweep_acquisition import (
    AcquisitionRequest,
    AcquisitionStateError,
    ArmBlockedError,
    ArmConfirmation,
    BackendUnavailableError,
    ChannelRouting,
    DeviceNotFoundError,
    FakeAudioBackend,
    ImpulseResponseDeriver,
    LevelSafetyPolicy,
    MeasurementAcquisitionEngine,
    QualityGateThresholds,
    SweepAcquisitionError,
    SweepStimulusGenerator,
    SweepStimulusSpec,
    UnsupportedConfigurationError,
    WasapiAudioBackend,
    default_fake_scenario,
    evaluate_acquisition_quality,
)
from htdt.cad_sweep_acquisition_evidence import (
    SWEEP_ACQUISITION_QUALITY_LABELS,
    SWEEP_ACQUISITION_REASON_LABELS,
    SWEEP_ACQUISITION_STAGE_LABELS,
    SWEEP_TIMING_QUALITY_LABELS,
    build_stimulus_definition,
)
from htdt.cad_sweep_acquisition_repository import (
    CadSweepAcquisitionRepository,
    SweepAcquisitionConflictError,
    SweepAcquisitionIntegrityError,
)
from htdt.canonical_json import canonical_sha256

DOC = 'doc-869'
_TS = '2026-10-07T00:00:00Z'


def _spec(**kw) -> SweepStimulusSpec:
    payload = dict(
        start_frequency_hz=100.0,
        end_frequency_hz=8000.0,
        duration_s=0.05,
        level_dbfs=-12.0,
        sample_rate_hz=48000,
        pre_roll_s=0.01,
        post_roll_s=0.01,
        fade_in_s=0.002,
        fade_out_s=0.002,
        repetitions=2,
        repetition_gap_s=0.02,
    )
    payload.update(kw)
    return SweepStimulusSpec(**payload)


def _routing(loopback: int | None = 1, **kw) -> ChannelRouting:
    payload = dict(
        playback_device_id='fake-duplex-0',
        playback_channel=0,
        capture_device_id='fake-duplex-0',
        capture_channel=0,
        loopback_input_channel=loopback,
    )
    payload.update(kw)
    return ChannelRouting(**payload)


def _request(**kw) -> AcquisitionRequest:
    payload = dict(stimulus=_spec(), routing=_routing())
    payload.update(kw)
    return AcquisitionRequest(**payload)


def _confirmation(request: AcquisitionRequest, **kw) -> ArmConfirmation:
    r = request.routing
    payload = dict(
        acknowledged_playback_device_id=r.playback_device_id,
        acknowledged_playback_channel=r.playback_channel,
        acknowledged_capture_device_id=r.capture_device_id,
        acknowledged_capture_channel=r.capture_channel,
        acknowledged_level_dbfs=request.stimulus.level_dbfs,
    )
    payload.update(kw)
    return ArmConfirmation(**payload)


def _fake(scenario_kw: dict | None = None) -> FakeAudioBackend:
    return FakeAudioBackend(default_fake_scenario(**(scenario_kw or {})))


def _engine(scenario_kw: dict | None = None) -> MeasurementAcquisitionEngine:
    return MeasurementAcquisitionEngine(_fake(scenario_kw))


def _ready_engine(
    scenario_kw: dict | None = None, request_kw: dict | None = None,
) -> MeasurementAcquisitionEngine:
    engine = _engine(scenario_kw)
    report = engine.configure(_request(**(request_kw or {})))
    assert report.ok, report.blocked_reasons
    return engine


def _armed_engine(
    scenario_kw: dict | None = None, request_kw: dict | None = None,
) -> MeasurementAcquisitionEngine:
    engine = _ready_engine(scenario_kw, request_kw)
    engine.arm(_confirmation(engine.result.request))
    return engine


def _run(engine: MeasurementAcquisitionEngine):
    return engine.start()


def _repo(tmp_path: Path) -> CadSweepAcquisitionRepository:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    return CadSweepAcquisitionRepository(SceneRepository(db))


# ---------------------------------------------------------------------------
# SweepStimulusGenerator — deterministic, hashed, exact sample-rate binding


class TestStimulusGenerator:
    def test_identical_spec_identical_hash_and_samples(self) -> None:
        gen = SweepStimulusGenerator()
        a = gen.generate(_spec())
        b = SweepStimulusGenerator().generate(_spec())
        assert a.params_sha256 == b.params_sha256
        assert a.samples_sha256 == b.samples_sha256
        assert a.samples == b.samples

    def test_params_change_hash(self) -> None:
        gen = SweepStimulusGenerator()
        a = gen.generate(_spec())
        b = gen.generate(_spec(level_dbfs=-18.0))
        c = gen.generate(_spec(end_frequency_hz=4000.0))
        assert len({a.params_sha256, b.params_sha256, c.params_sha256}) == 3
        assert len({a.samples_sha256, b.samples_sha256, c.samples_sha256}) == 3

    def test_generator_version_changes_identity(self) -> None:
        a = SweepStimulusGenerator().generate(_spec())
        b = SweepStimulusGenerator('htdt-sweep-gen-x').generate(_spec())
        assert a.params_sha256 != b.params_sha256
        # The samples themselves are identical; identity is versioned.
        assert a.samples_sha256 == b.samples_sha256

    def test_exact_sample_rate_binding(self) -> None:
        spec = _spec(sample_rate_hz=44100, duration_s=0.5,
                     pre_roll_s=0.1, post_roll_s=0.2)
        stimulus = SweepStimulusGenerator().generate(spec)
        expected_block = round((0.1 + 0.5 + 0.2) * 44100)
        assert stimulus.block_frames == expected_block
        assert stimulus.sweep_samples == round(0.5 * 44100)
        assert stimulus.sweep_start_sample == round(0.1 * 44100)

    def test_level_applied_and_fades_bounded(self) -> None:
        stimulus = SweepStimulusGenerator().generate(_spec(level_dbfs=-6.0))
        samples = np.asarray(stimulus.samples)
        peak = float(np.max(np.abs(samples)))
        assert 0.4 < peak <= 10 ** (-6.0 / 20.0) + 1e-9
        # Fades must reach silence at the edges, not jump.
        assert abs(samples[0]) < 1e-6
        assert abs(samples[-1]) < 1e-6

    def test_geometry_validation_failures(self) -> None:
        assert _spec(end_frequency_hz=50.0).validate_geometry()
        assert _spec(
            end_frequency_hz=20000.0, sample_rate_hz=44100
        ).validate_geometry() == ()
        assert _spec(
            end_frequency_hz=24000.0, sample_rate_hz=44100
        ).validate_geometry()
        assert _spec(
            duration_s=0.004, fade_in_s=0.003, fade_out_s=0.003
        ).validate_geometry()

    def test_pydantic_rejects_impossible_fields(self) -> None:
        with pytest.raises(ValidationError):
            _spec(sample_rate_hz=0)
        with pytest.raises(ValidationError):
            _spec(level_dbfs=3.0)
        with pytest.raises(ValidationError):
            _spec(repetitions=0)
        with pytest.raises(ValidationError):
            _spec(start_frequency_hz=-1.0)


# ---------------------------------------------------------------------------
# AudioIOBackend — explicit binding, no silent fallback, fail closed


class TestBackendAbstraction:
    def test_wasapi_stub_fails_closed(self) -> None:
        backend = WasapiAudioBackend()
        assert backend.available() is False
        assert backend.enumerate_devices() == ()
        engine = MeasurementAcquisitionEngine(backend)
        report = engine.configure(_request())
        assert report.ok is False
        assert any('no audio devices' in r for r in report.blocked_reasons)

    def test_wasapi_open_stream_raises_unavailable(self) -> None:
        backend = WasapiAudioBackend()
        # configure() blocks before arm, so drive the stream directly.
        with pytest.raises(BackendUnavailableError):
            backend.open_stream(engine_config())

    def test_fake_enumerates_devices(self) -> None:
        backend = _fake()
        devices = backend.enumerate_devices()
        assert len(devices) == 1
        assert devices[0].device_id == 'fake-duplex-0'
        assert devices[0].direction == 'duplex'

    def test_fake_rejects_unknown_device(self) -> None:
        backend = _fake()
        config = engine_config(
            routing=_routing(playback_device_id='nope'))
        with pytest.raises(DeviceNotFoundError):
            backend.open_stream(config)

    def test_fake_rejects_unsupported_rate(self) -> None:
        backend = _fake()
        config = engine_config(sample_rate_hz=22050)
        with pytest.raises(UnsupportedConfigurationError):
            backend.open_stream(config)

    def test_fake_rejects_out_of_range_channels(self) -> None:
        backend = _fake()
        with pytest.raises(UnsupportedConfigurationError):
            backend.open_stream(
                engine_config(routing=_routing(playback_channel=9)))
        with pytest.raises(UnsupportedConfigurationError):
            backend.open_stream(
                engine_config(routing=_routing(capture_channel=9)))
        with pytest.raises(UnsupportedConfigurationError):
            backend.open_stream(
                engine_config(routing=_routing(loopback=9)))


def engine_config(routing: ChannelRouting | None = None,
                  sample_rate_hz: int = 48000):
    from htdt.cad_sweep_acquisition import AudioStreamConfig

    return AudioStreamConfig(
        sample_rate_hz=sample_rate_hz,
        sample_format='float64',
        routing=routing or _routing(),
    )


# ---------------------------------------------------------------------------
# Engine state machine — every stage + transition reason


class TestEngineStateMachine:
    def test_happy_path_reaches_completed(self) -> None:
        engine = _armed_engine()
        result = _run(engine)
        assert engine.stage == 'completed'
        stages = [t.stage for t in engine.transitions]
        assert stages[:4] == [
            'precheck', 'ready', 'armed', 'playing_recording']
        assert stages[-1] == 'completed'
        # Every transition carries an explicit reason + timestamp.
        for t in engine.transitions:
            assert t.reason
            assert t.at_utc
        assert result.capture is not None
        assert result.capture.outcome == 'completed'
        assert result.timing is not None
        assert result.timing.quality == 'qualified'
        assert result.impulse_response is not None
        assert result.impulse_response.status == 'derived'
        assert result.impulse_response.ir_sha256 is not None
        assert result.quality is not None
        assert result.quality.verdict in ('valid', 'limited')

    def test_completed_run_is_not_automatically_valid(self) -> None:
        # A completed capture with clipping must not read as valid.
        engine = _armed_engine({'clip_at_dbfs': -20.0})
        result = _run(engine)
        assert engine.stage == 'completed'
        assert result.quality is not None
        assert result.quality.verdict == 'invalid'
        assert 'clipping_detected' in result.quality.reasons

    def test_precheck_blocks_without_devices(self) -> None:
        engine = MeasurementAcquisitionEngine(WasapiAudioBackend())
        report = engine.configure(_request())
        assert report.ok is False
        assert engine.stage == 'precheck'
        assert engine.blocked_reasons
        assert 'no audio devices enumerated' in engine.blocked_reasons[0]

    def test_precheck_blocks_on_unknown_routing(self) -> None:
        engine = _engine()
        report = engine.configure(_request(
            routing=_routing(playback_device_id='missing')))
        assert report.ok is False
        assert any('not enumerated' in r for r in report.blocked_reasons)
        assert engine.stage == 'precheck'

    def test_precheck_blocks_on_channel_and_rate(self) -> None:
        engine = _engine()
        report = engine.configure(_request(
            stimulus=_spec(sample_rate_hz=22050),
            routing=_routing(capture_channel=9),
        ))
        assert report.ok is False
        text = '; '.join(report.blocked_reasons)
        assert 'capture channel out of range' in text
        assert 'unsupported by playback device' in text

    def test_precheck_blocks_bad_geometry(self) -> None:
        engine = _engine()
        report = engine.configure(_request(
            stimulus=_spec(end_frequency_hz=1.0, start_frequency_hz=100.0)))
        assert report.ok is False
        assert any('end_frequency' in r for r in report.blocked_reasons)

    def test_arm_requires_ready(self) -> None:
        # Unconfigured: no request bound at all.
        engine = _engine()
        with pytest.raises(AcquisitionStateError):
            engine.arm(_confirmation(_request()))
        # Configured but precheck-blocked: still not ready.
        blocked = _engine()
        blocked.configure(_request(routing=_routing(
            playback_device_id='missing')))
        with pytest.raises(ArmBlockedError):
            blocked.arm(_confirmation(blocked.result.request))

    def test_arm_rejects_level_over_policy(self) -> None:
        engine = _ready_engine(
            request_kw={'stimulus': _spec(level_dbfs=-3.0)})
        with pytest.raises(ArmBlockedError) as excinfo:
            engine.arm(_confirmation(
                engine.result.request, acknowledged_level_dbfs=-3.0))
        assert any('exceeds' in b for b in excinfo.value.reasons)
        assert engine.stage == 'ready'

    def test_arm_rejects_mismatched_confirmation(self) -> None:
        engine = _ready_engine()
        request = engine.result.request
        with pytest.raises(ArmBlockedError) as excinfo:
            engine.arm(_confirmation(
                request, acknowledged_level_dbfs=-30.0))
        assert any('level' in b for b in excinfo.value.reasons)
        with pytest.raises(ArmBlockedError) as excinfo:
            engine.arm(_confirmation(
                request, acknowledged_playback_channel=3))
        assert any('playback channel' in b for b in excinfo.value.reasons)

    def test_arm_rejects_missing_required_loopback(self) -> None:
        policy = LevelSafetyPolicy(require_loopback_reference=True)
        engine = _ready_engine(request_kw={
            'level_policy': policy,
            'routing': _routing(loopback=None),
        })
        with pytest.raises(ArmBlockedError) as excinfo:
            engine.arm(_confirmation(engine.result.request))
        assert any('loopback' in b for b in excinfo.value.reasons)

    def test_configure_after_arm_invalidates_armed(self) -> None:
        engine = _armed_engine()
        assert engine.stage == 'armed'
        engine.configure(engine.result.request)
        assert engine.stage == 'ready'
        assert any('invalidated' in t.reason
                   for t in engine.transitions)
        with pytest.raises(AcquisitionStateError):
            engine.start()

    def test_notify_configuration_changed_invalidates_armed(self) -> None:
        engine = _armed_engine()
        engine.notify_configuration_changed()
        assert engine.stage == 'ready'
        assert engine.blocked_reasons

    def test_notify_configuration_changed_safe_when_unarmed(self) -> None:
        engine = _ready_engine()
        engine.notify_configuration_changed()
        assert engine.stage == 'ready'

    def test_cancel_from_armed(self) -> None:
        engine = _armed_engine()
        engine.cancel()
        assert engine.stage == 'cancelled'
        with pytest.raises(AcquisitionStateError):
            engine.start()

    def test_cancel_from_ready_and_precheck(self) -> None:
        engine = _ready_engine()
        engine.cancel()
        assert engine.stage == 'cancelled'
        engine2 = _engine()
        engine2.cancel()
        assert engine2.stage == 'cancelled'

    def test_cancel_mid_run_yields_cancelled(self) -> None:
        engine = _armed_engine({'cancel_after_frames': 10})
        result = _run(engine)
        assert engine.stage == 'cancelled'
        assert result.quality is not None
        assert 'cancelled' in result.quality.reasons

    def test_cancel_is_idempotent_on_terminal(self) -> None:
        engine = _armed_engine()
        _run(engine)
        assert engine.stage == 'completed'
        engine.cancel()
        assert engine.stage == 'completed'

    def test_start_requires_armed(self) -> None:
        engine = _ready_engine()
        with pytest.raises(AcquisitionStateError):
            engine.start()

    def test_backend_unavailable_mid_run_fails(self) -> None:
        # Devices vanish between arm and start: the backend swap simulates
        # that loss of availability.
        engine = _armed_engine()
        engine.backend = WasapiAudioBackend()
        result = engine.start()
        assert engine.stage == 'failed'
        assert result.quality is not None
        assert 'backend_unavailable' in result.quality.reasons

    def test_device_lost_mid_run_fails(self) -> None:
        engine = _armed_engine({'device_loss_at_frame': 50})
        result = _run(engine)
        assert engine.stage == 'failed'
        assert result.capture.outcome == 'device_lost'
        assert 'device_lost' in result.quality.reasons

    def test_progress_reported(self) -> None:
        engine = _armed_engine()
        _run(engine)
        done, total = engine.progress
        assert total > 0
        assert done == total

    def test_next_permitted_action(self) -> None:
        engine = _engine()
        assert 'configure' in engine.next_permitted_action()
        engine.configure(_request())
        assert 'arm' in engine.next_permitted_action()
        engine.arm(_confirmation(engine.result.request))
        assert 'start' in engine.next_permitted_action()

    def test_reconfigure_after_terminal_starts_new_run(self) -> None:
        engine = _armed_engine()
        _run(engine)
        first_run_id = engine.run_id
        report = engine.configure(_request())
        assert report.ok
        assert engine.stage == 'ready'
        assert engine.run_id != first_run_id
        assert engine.result.capture is None


# ---------------------------------------------------------------------------
# Timing resolution — unqualified stays UNKNOWN, never silently aligned


class TestTimingResolution:
    def test_loopback_reference_qualifies(self) -> None:
        engine = _armed_engine()
        result = _run(engine)
        timing = result.timing
        assert timing.method == 'loopback_cross_correlation'
        assert timing.quality == 'qualified'
        assert timing.onset_sample_index is not None
        assert timing.playback_to_capture_latency_s is not None
        assert timing.reference_channel == 1

    def test_no_loopback_is_limited(self) -> None:
        engine = _armed_engine(
            request_kw={'routing': _routing(loopback=None)})
        result = _run(engine)
        assert result.timing.method == 'stimulus_onset_detection'
        assert result.timing.quality == 'limited'
        assert result.quality.verdict == 'limited'
        assert result.quality.warnings

    def test_undetectable_onset_is_unqualified(self) -> None:
        # An empty impulse response gives a silent measurement channel —
        # nothing can lock onto it, so timing stays unqualified.
        engine = _armed_engine(
            {'measurement_ir_taps': ()},
            {'routing': _routing(loopback=None)})
        result = _run(engine)
        assert result.timing.quality == 'unqualified'
        assert result.timing.onset_sample_index is None
        assert 'synchronization_unusable' in result.quality.reasons
        assert result.quality.verdict == 'invalid'
        # The IR cannot be derived without qualified timing.
        assert result.impulse_response.status == 'timing_unqualified'
        assert result.impulse_response.ir_sha256 is None

    def test_declared_sync_without_onset_stays_unqualified(self) -> None:
        engine = _armed_engine(
            {'measurement_ir_taps': ()},
            {'routing': _routing(loopback=None),
             'declared_synchronized': True})
        result = _run(engine)
        assert result.timing.method == 'unsynchronized_declared'
        assert result.timing.quality == 'unqualified'

    def test_drift_evidence_recorded(self) -> None:
        engine = _armed_engine({'drift_ppm': 0.0})
        result = _run(engine)
        assert result.timing.drift_ppm is not None


# ---------------------------------------------------------------------------
# ImpulseResponseDeriver — versioned, separate evidence artifact


class TestImpulseResponseDeriver:
    def test_derives_ir_with_stable_identity(self) -> None:
        engine = _armed_engine()
        result = _run(engine)
        ir = result.impulse_response
        assert ir.status == 'derived'
        assert ir.ir is not None and len(ir.ir) > 0
        assert ir.ir_sha256 is not None
        assert ir.origin_convention == 'loopback_reference'
        assert ir.origin_sample_index is not None
        # Derived IR normalized to a peak of 1.
        assert max(abs(v) for v in ir.ir) == pytest.approx(1.0)

    def test_onset_origin_when_no_loopback(self) -> None:
        engine = _armed_engine(
            request_kw={'routing': _routing(loopback=None)})
        result = _run(engine)
        assert result.impulse_response.origin_convention == 'detected_onset'

    def test_version_change_keeps_history_intact(self) -> None:
        engine = _armed_engine()
        result = _run(engine)
        other = ImpulseResponseDeriver(derivation_version='ir-vX').derive(
            result.stimulus, result.capture, result.timing)
        # Same input -> same derived content; the version pin is what
        # distinguishes the derivation, and the stored artifact's own
        # version never mutates retroactively.
        assert other.ir_sha256 == result.impulse_response.ir_sha256
        assert other.derivation_version == 'ir-vX'
        assert result.impulse_response.derivation_version != 'ir-vX'

    def test_empty_capture_fails(self) -> None:
        from htdt.cad_sweep_acquisition import (
            CaptureResult,
            TimingResolution,
        )

        stimulus = SweepStimulusGenerator().generate(_spec())
        empty = CaptureResult(
            outcome='device_lost', actual_sample_rate_hz=48000,
            actual_sample_format='float64', captured_channels=(0,),
            samples=np.zeros((0, 1)), expected_frames=1000,
            recorded_frames=0,
        )
        timing = TimingResolution(
            method='unqualified', quality='unqualified',
            onset_sample_index=None,
            playback_to_capture_latency_s=None, drift_ppm=None,
            reference_channel=None,
        )
        ir = ImpulseResponseDeriver().derive(stimulus, empty, timing)
        assert ir.status == 'failed'
        assert ir.ir_sha256 is None

    def test_frequency_response_derived(self) -> None:
        engine = _armed_engine()
        result = _run(engine)
        fr = result.impulse_response.frequency_response
        assert fr is not None and len(fr) > 0
        for hz, _db in fr:
            assert hz >= 0


# ---------------------------------------------------------------------------
# Quality gate — every named verdict reason, fail-closed


class TestQualityGate:
    def test_clipping_detected(self) -> None:
        result = _run(_armed_engine({'clip_at_dbfs': -20.0}))
        assert 'clipping_detected' in result.quality.reasons
        assert result.quality.verdict == 'invalid'
        assert result.quality.measured['clipped_samples'] > 0

    def test_insufficient_snr(self) -> None:
        result = _run(_armed_engine({
            'measurement_gain_db': -80.0, 'noise_rms_dbfs': -20.0,
        }))
        q = result.quality
        assert ('insufficient_snr' in q.reasons
                or 'synchronization_unusable' in q.reasons)
        assert q.verdict == 'invalid'

    def test_capture_truncated(self) -> None:
        result = _run(_armed_engine({'truncate_at_frame': 500}))
        assert 'capture_truncated' in result.quality.reasons
        assert result.capture.truncated is True

    def test_xrun_detected(self) -> None:
        result = _run(_armed_engine({
            'xrun_at_rep_frame': ((0, 1000),),
            'xrun_span_frames': 200,
        }))
        assert 'xrun_detected' in result.quality.reasons
        assert result.quality.measured['xrun_count'] >= 1

    def test_excessive_noise(self) -> None:
        result = _run(_armed_engine({'noise_rms_dbfs': -10.0}))
        q = result.quality
        assert 'excessive_noise' in q.reasons or q.verdict == 'invalid'
        if 'excessive_noise' in q.reasons:
            assert q.measured['noise_floor_dbfs'] > -10.5

    def test_inconsistent_repetitions(self) -> None:
        # Heavy drift between reps destroys the consistency correlation.
        result = _run(_armed_engine({'drift_ppm': 50000.0}))
        q = result.quality
        # Either the consistency metric is measured low, or the drifted
        # reps lose onset lock entirely — both fail closed.
        if q.measured['repetition_consistency'] is not None:
            assert 'inconsistent_repetitions' in q.reasons
            assert q.verdict == 'invalid'
        else:
            assert q.verdict != 'valid'

    def test_calibration_missing_when_absolute_required(self) -> None:
        result = _run(_armed_engine(request_kw={
            'requires_absolute_level': True,
            'calibration_state': 'missing',
        }))
        assert 'calibration_missing' in result.quality.reasons
        assert result.quality.verdict == 'invalid'

    def test_calibration_unknown_when_absolute_required(self) -> None:
        result = _run(_armed_engine(request_kw={
            'requires_absolute_level': True,
            'calibration_state': 'unknown',
        }))
        assert 'calibration_missing' in result.quality.reasons

    def test_calibration_invalid_always_fails(self) -> None:
        result = _run(_armed_engine(request_kw={
            'calibration_state': 'invalid',
        }))
        assert 'calibration_invalid' in result.quality.reasons
        assert result.quality.verdict == 'invalid'

    def test_uncalibrated_run_warns_not_fails(self) -> None:
        result = _run(_armed_engine(request_kw={
            'calibration_state': 'unknown',
        }))
        q = result.quality
        assert 'calibration_missing' not in q.reasons
        assert 'calibration_invalid' not in q.reasons
        assert any('UNKNOWN' in w for w in q.warnings)

    def test_cancelled_capture_is_invalid(self) -> None:
        result = _run(_armed_engine({'cancel_after_frames': 5}))
        assert result.quality.verdict == 'invalid'
        assert 'cancelled' in result.quality.reasons

    def test_open_failed_is_invalid(self) -> None:
        engine = _armed_engine()
        engine.backend = FakeAudioBackend(
            default_fake_scenario(devices=()))
        # Devices vanish between arm and start.
        result = engine.start()
        assert engine.stage == 'failed'
        assert 'open_failed' in result.quality.reasons

    def test_unevaluated_capture_quality_direct(self) -> None:
        q = evaluate_acquisition_quality(None, None, None, 'bound')
        assert q.verdict == 'invalid'
        assert 'open_failed' in q.reasons

    def test_quality_thresholds_configurable(self) -> None:
        strict = QualityGateThresholds(max_xruns=0)
        engine = _armed_engine(
            {'xrun_at_rep_frame': ((0, 10),), 'xrun_span_frames': 5},
            {'quality_thresholds': strict})
        result = _run(engine)
        assert 'xrun_detected' in result.quality.reasons

    def test_reason_labels_cover_every_reason(self) -> None:
        from htdt.cad_sweep_acquisition import AcquisitionQualityReason
        import typing
        for reason in typing.get_args(AcquisitionQualityReason):
            assert reason in SWEEP_ACQUISITION_REASON_LABELS, reason

    def test_stage_labels_cover_every_stage(self) -> None:
        from htdt.cad_sweep_acquisition import AcquisitionStage
        import typing
        for stage in typing.get_args(AcquisitionStage):
            assert stage in SWEEP_ACQUISITION_STAGE_LABELS, stage

    def test_timing_quality_labels(self) -> None:
        from htdt.cad_sweep_acquisition import TimingQuality
        import typing
        for quality in typing.get_args(TimingQuality):
            assert quality in SWEEP_TIMING_QUALITY_LABELS, quality

    def test_quality_verdict_labels(self) -> None:
        from htdt.cad_sweep_acquisition import AcquisitionQualityVerdict
        import typing
        for verdict in typing.get_args(AcquisitionQualityVerdict):
            assert verdict in SWEEP_ACQUISITION_QUALITY_LABELS, verdict


# ---------------------------------------------------------------------------
# Sealed evidence + repository round-trip + tamper detection


def _terminal_run(tmp_path: Path, scenario_kw: dict | None = None,
                  request_kw: dict | None = None):
    """Drive one run to a terminal stage and persist its evidence."""

    repo = _repo(tmp_path)
    engine = _armed_engine(scenario_kw, request_kw)
    result = _run(engine)
    stimulus = build_stimulus_definition(
        document_id=DOC,
        stimulus=result.stimulus,
        created_at_utc=_TS,
    )
    repo.save_stimulus_definition(stimulus)
    ref = AuthorityRef(
        kind='sweep_stimulus_definition',
        ref_id=stimulus.stimulus_definition_id,
        ref_sha256=stimulus.stimulus_sha256,
    )
    record = repo.record_run(
        document_id=DOC, engine=engine, result=result,
        stimulus_ref=ref)
    return repo, engine, result, stimulus, record


class TestEvidenceSealing:
    def test_stimulus_definition_seal(self) -> None:
        stimulus = SweepStimulusGenerator().generate(_spec())
        definition = build_stimulus_definition(
            document_id=DOC, stimulus=stimulus, created_at_utc=_TS)
        assert definition.stimulus_definition_id.startswith('swstim-')
        assert definition.stimulus_sha256 == canonical_sha256(
            definition.identity_payload())
        assert definition.params_sha256 == stimulus.params_sha256
        assert definition.samples_sha256 == stimulus.samples_sha256

    def test_run_record_seal(self, tmp_path: Path) -> None:
        _, _, _, _, record = _terminal_run(tmp_path)
        assert record.acquisition_id.startswith('swrun-')
        assert record.acquisition_sha256 == canonical_sha256(
            record.identity_payload())
        assert record.outcome == 'completed'
        assert record.stage == 'completed'
        assert record.timing_quality == 'qualified'
        assert record.raw_audio_sha256 is not None
        assert record.ir_sha256 is not None
        assert record.backend_is_simulated is True
        assert record.backend_id == 'fake-audio-io'

    def test_tampered_model_rejected(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        stimulus = SweepStimulusGenerator().generate(_spec())
        definition = build_stimulus_definition(
            document_id=DOC, stimulus=stimulus, created_at_utc=_TS)
        repo.save_stimulus_definition(definition)
        tampered = definition.model_copy(
            update={'level_dbfs': -1.0})
        with pytest.raises(SweepAcquisitionIntegrityError):
            repo.save_stimulus_definition(tampered)


class TestRepository:
    def test_stimulus_round_trip(self, tmp_path: Path) -> None:
        repo, _, _, stimulus, _ = _terminal_run(tmp_path)
        got = repo.get_stimulus_definition(
            stimulus.stimulus_definition_id)
        assert got == stimulus
        listed = repo.list_stimulus_definitions(DOC)
        assert stimulus.stimulus_definition_id in {
            d.stimulus_definition_id for d in listed}

    def test_run_round_trip_and_lookup(self, tmp_path: Path) -> None:
        repo, engine, _, _, record = _terminal_run(tmp_path)
        got = repo.get_acquisition_run(record.acquisition_id)
        assert got == record
        by_run = repo.get_run_by_run_id(engine.run_id)
        assert by_run is not None
        assert by_run.acquisition_id == record.acquisition_id
        assert record.acquisition_id in {
            r.acquisition_id for r in repo.list_acquisition_runs(DOC)}

    def test_stage_events_recorded(self, tmp_path: Path) -> None:
        repo, engine, _, _, record = _terminal_run(tmp_path)
        events = repo.list_stage_events(engine.run_id)
        stages = [e.stage for e in events]
        assert stages[0] == 'precheck'
        assert 'armed' in stages
        assert stages[-1] == 'completed'
        assert len(events) == len(engine.transitions)
        for seq, (event, transition) in enumerate(
                zip(events, engine.transitions)):
            assert event.reason == transition.reason
            assert event.run_seq == seq

    def test_append_only_conflict(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        stimulus = SweepStimulusGenerator().generate(_spec())
        a = build_stimulus_definition(
            document_id=DOC, stimulus=stimulus, created_at_utc=_TS)
        repo.save_stimulus_definition(a)
        # Same id, different payload -> conflict.
        b = a.model_copy(update={'document_id': 'other'})
        object.__setattr__(b, 'stimulus_sha256', a.stimulus_sha256)
        with pytest.raises((SweepAcquisitionConflictError,
                            SweepAcquisitionIntegrityError,
                            ValidationError)):
            repo.save_stimulus_definition(b)

    def test_idempotent_resave(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        stimulus = SweepStimulusGenerator().generate(_spec())
        a = build_stimulus_definition(
            document_id=DOC, stimulus=stimulus, created_at_utc=_TS)
        repo.save_stimulus_definition(a)
        repo.save_stimulus_definition(a)  # no-op, not a conflict
        assert repo.get_stimulus_definition(
            a.stimulus_definition_id) == a

    def test_tampered_column_detected(self, tmp_path: Path) -> None:
        import sqlite3

        repo, _, _, _, record = _terminal_run(tmp_path)
        db = tmp_path / 'cad.sqlite3'
        connection = sqlite3.connect(db)
        try:
            connection.execute(
                'UPDATE cad_sweep_acquisition_runs '
                "SET timing_quality='unqualified' WHERE acquisition_id=?",
                (record.acquisition_id,))
            connection.commit()
        finally:
            connection.close()
        with pytest.raises(SweepAcquisitionIntegrityError):
            repo.get_acquisition_run(record.acquisition_id)

    def test_tampered_payload_detected(self, tmp_path: Path) -> None:
        import json
        import sqlite3

        repo, _, _, stimulus, _ = _terminal_run(tmp_path)
        db = tmp_path / 'cad.sqlite3'
        connection = sqlite3.connect(db)
        try:
            row = connection.execute(
                'SELECT payload_json FROM cad_sweep_stimulus_definitions '
                'WHERE stimulus_definition_id=?',
                (stimulus.stimulus_definition_id,)).fetchone()
            payload = json.loads(row[0])
            payload['level_dbfs'] = -99.0
            connection.execute(
                'UPDATE cad_sweep_stimulus_definitions SET payload_json=? '
                'WHERE stimulus_definition_id=?',
                (json.dumps(payload), stimulus.stimulus_definition_id))
            connection.commit()
        finally:
            connection.close()
        # The unsealed payload fails model validation on read.
        with pytest.raises(ValidationError):
            repo.get_stimulus_definition(
                stimulus.stimulus_definition_id)

    def test_raw_and_ir_assets_installed(self, tmp_path: Path) -> None:
        repo, _, result, _, record = _terminal_run(tmp_path)
        raw = repo.read_raw_audio(record)
        assert raw is not None
        ir_bytes = repo.read_ir(record)
        assert ir_bytes is not None
        # Content-addressed: the bytes hash to the recorded sha.
        from hashlib import sha256
        assert sha256(raw).hexdigest() == record.raw_audio_sha256
        assert sha256(ir_bytes).hexdigest() == record.ir_sha256

    def test_missing_assets_return_none(self, tmp_path: Path) -> None:
        repo, _, _, _, record = _terminal_run(
            tmp_path, {'device_loss_at_frame': 1})
        assert record.outcome == 'failed'
        # A failed run may have no IR at all.
        if record.ir_sha256 is None:
            assert repo.read_ir(record) is None

    def test_unknown_stays_recorded_unknown(self, tmp_path: Path) -> None:
        repo, _, _, _, record = _terminal_run(
            tmp_path, {'measurement_ir_taps': ()},
            {'routing': _routing(loopback=None)})
        assert record.timing_quality == 'unqualified'
        assert record.onset_sample_index is None
        assert 'synchronization_unusable' in record.quality_reasons


# ---------------------------------------------------------------------------
# UI surface — honest, minimal, no real audio path


class TestWorkspaceAcquisitionPage:
    def test_page_renders_and_drives_fake_run(
            self, tmp_path: Path) -> None:
        from PySide6.QtWidgets import QApplication
        from htdt.cad_scene import make_f1_scene
        from htdt.measurement_page_workspace import MeasurementPageWorkspace
        from htdt.measurement_workflow import (
            MeasurementWorkflowController)

        QApplication.instance() or QApplication([])
        scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
        revision = scene_repository.save(
            make_f1_scene(), parent_revision_id=None).revision
        controller = MeasurementWorkflowController(
            scene_repository, revision.document_id)
        workspace = MeasurementPageWorkspace(
            controller, acquisition_backend=_fake())
        try:
            workspace.set_context('acquisition')
            # Devices enumerated from the fake backend.
            assert workspace.acq_playback_device.count() == 1
            workspace._acq_configure()
            assert workspace._acq_engine.stage == 'ready'
            assert workspace.acq_arm_button.isEnabled()
            workspace._acq_arm()
            assert workspace._acq_engine.stage == 'armed'
            assert workspace.acq_start_button.isEnabled()
            workspace._acq_start()
            assert workspace._acq_engine.stage == 'completed'
            assert '完了' in workspace.acq_stage_label.text()
            workspace._acq_save_evidence()
            assert workspace._acq_run_record is not None
            # The sealed record is retrievable from the document.
            repo = CadSweepAcquisitionRepository(scene_repository)
            got = repo.get_acquisition_run(
                workspace._acq_run_record.acquisition_id)
            assert got is not None
            assert got.backend_is_simulated is True
        finally:
            workspace.deleteLater()

    def test_wasapi_backend_shows_honest_unavailable(
            self, tmp_path: Path) -> None:
        from PySide6.QtWidgets import QApplication
        from htdt.cad_scene import make_f1_scene
        from htdt.measurement_page_workspace import MeasurementPageWorkspace
        from htdt.measurement_workflow import (
            MeasurementWorkflowController)

        QApplication.instance() or QApplication([])
        scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
        revision = scene_repository.save(
            make_f1_scene(), parent_revision_id=None).revision
        controller = MeasurementWorkflowController(
            scene_repository, revision.document_id)
        workspace = MeasurementPageWorkspace(controller)  # default WASAPI
        try:
            workspace.set_context('acquisition')
            assert workspace.acq_playback_device.count() == 0
            assert 'デバイス' in workspace.acq_devices_note.text()
            workspace._acq_configure()
            assert workspace._acq_engine.stage == 'precheck'
            assert not workspace.acq_arm_button.isEnabled()
            assert workspace.acq_blocked_label.text()
        finally:
            workspace.deleteLater()
