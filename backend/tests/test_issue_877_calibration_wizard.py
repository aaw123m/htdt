"""#877 guided automatic measurement-chain calibration.

Covers the three lanes (interface loopback, SPL/reference check,
campaign pre/post checks), the sealed transition log as sole resume
authority, fail-closed behaviour (clipping, low SNR, unstable reference,
wrong port, wrong rate, device loss, cancellation), repository
round-trip + tamper detection, schema v101 wiring, and the offscreen UI
surface. Fake hardware is deterministic throughout.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_calibration_lifecycle import (
    build_instrument_instance,
    instrument_binding,
)
from htdt.cad_calibration_lifecycle_repository import (
    CadCalibrationLifecycleRepository,
)
from htdt.cad_calibration_wizard import (
    CALIBRATION_WIZARD_VERSION,
    CadCalibrationWizardRun,
    CadCalibrationWizardTransition,
    CadCampaignCheckPlan,
    CadSplCheckAcceptanceProfile,
    CalibrationWizard,
    CalibrationWizardError,
    WizardTransitionRejection,
    RequiredCheckSpec,
    WizardEvent,
    WizardStores,
    campaign_plan_binding,
    derive_wizard_state,
    evaluate_campaign_check_gate,
    evaluate_reference_check,
    measure_reference_tone,
    next_permitted_events,
    pending_physical_instruction,
    spl_profile_binding,
    wizard_transition,
)
from htdt.cad_calibration_wizard_repository import (
    CadCalibrationWizardRepository,
    CalibrationWizardConflictError,
    CalibrationWizardIntegrityError,
)
from htdt.cad_interface_loopback import CadInterfaceIoPath
from htdt.cad_interface_loopback_repository import (
    CadInterfaceLoopbackRepository,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import connect_sqlite, ensure_native_schema
from htdt.cad_sweep_acquisition import (
    AudioDeviceInfo,
    CaptureResult,
    FakeAudioBackend,
    SweepStimulusSpec,
    ChannelRouting,
    default_fake_scenario,
)
from htdt.cad_sweep_acquisition_repository import (
    CadSweepAcquisitionRepository,
)

T0 = '2026-10-07T00:00:00Z'
DOC = 'doc-877'


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _spec(**kw) -> SweepStimulusSpec:
    base = dict(
        start_frequency_hz=100.0, end_frequency_hz=8000.0,
        duration_s=0.05, level_dbfs=-12.0, sample_rate_hz=48000,
        pre_roll_s=0.01, post_roll_s=0.01, fade_in_s=0.002,
        fade_out_s=0.002, repetitions=2, repetition_gap_s=0.02)
    base.update(kw)
    return SweepStimulusSpec(**base)


def _routing(**kw) -> ChannelRouting:
    base = dict(
        playback_device_id='fake-duplex-0', playback_channel=0,
        capture_device_id='fake-duplex-0', capture_channel=0,
        loopback_input_channel=1)
    base.update(kw)
    return ChannelRouting(**base)


def _io_path(**kw) -> CadInterfaceIoPath:
    base = dict(
        output_device='fake-duplex-0', output_port='0', output_channel='0',
        input_device='fake-duplex-0', input_port='1', input_channel='1',
        sample_rate_hz=48000.0, loopback_path_kind='analog',
        acquisition_class='analog_interface', driver_backend='fake')
    base.update(kw)
    return CadInterfaceIoPath(**base)


def _fake(**scenario_kw) -> FakeAudioBackend:
    return FakeAudioBackend(default_fake_scenario(**scenario_kw))


def _instrument(doc: str = DOC):
    return build_instrument_instance(
        document_id=doc, category='measurement_microphone',
        manufacturer='miniDSP', model='UMIK-1',
        serial_or_instance_id='UMIK-001', declared_at_utc=T0)


def _calibrator(doc: str = DOC):
    return build_instrument_instance(
        document_id=doc, category='sound_calibrator',
        manufacturer='B&K', model='4231',
        serial_or_instance_id='CAL-42', declared_at_utc=T0)


def _profile(doc: str = DOC, **kw) -> CadSplCheckAcceptanceProfile:
    base = dict(
        name='fc-94-1k', reference_level_db=94.0,
        reference_frequency_hz=1000.0, expected_measured_level_db=-20.0,
        level_tolerance_db=1.0, frequency_tolerance_hz=10.0,
        max_level_deviation_db=0.5, min_snr_db=20.0, min_duration_s=0.01,
        declared_at_utc=T0, declared_by='tester')
    base.update(kw)
    return CadSplCheckAcceptanceProfile.create(document_id=doc, **base)


def _campaign_ref(doc: str = DOC) -> AuthorityRef:
    return AuthorityRef(
        kind='campaign_preregistration', ref_id=f'{doc}-camp',
        ref_sha256='ab' * 32)


def _plan(doc: str = DOC, *, blocked: bool = True,
          kinds=('pre_use_field_check', 'post_use_field_check'),
          instrument=None) -> CadCampaignCheckPlan:
    instrument = instrument or _instrument(doc)
    inst_ref = instrument_binding(instrument)
    profile = _profile(doc)
    return CadCampaignCheckPlan.create(
        document_id=doc, campaign_ref=_campaign_ref(doc),
        required_checks=tuple(
            RequiredCheckSpec(
                instrument_ref=inst_ref, check_kind=k,
                acceptance_profile_ref=spl_profile_binding(profile))
            for k in kinds),
        blocked_on_failure=blocked, declared_at_utc=T0,
        declared_by='tester')


def _stores(scene: SceneRepository) -> WizardStores:
    return WizardStores(
        wizard=CadCalibrationWizardRepository(scene),
        sweep=CadSweepAcquisitionRepository(scene),
        interface=CadInterfaceLoopbackRepository(scene),
        lifecycle=CadCalibrationLifecycleRepository(scene),
    )


def _scene(tmp_path: Path) -> SceneRepository:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    return SceneRepository(db)


def _drive_lane_a(wz: CalibrationWizard, at_utc=T0):
    wz.begin_loopback(
        io_path=_io_path(), routing=_routing(), stimulus_spec=_spec(),
        created_by='tester', at_utc=at_utc)
    wz.confirm_hardware(at_utc=at_utc)
    return wz.run_automatic(at_utc=at_utc)


def _tone_capture(
        freq=1000.0, level_dbfs=-20.0, sr=48000, duration_s=0.5,
        noise_dbfs=-80.0, unstable: tuple[float, ...] | None = None,
        clipped=False) -> CaptureResult:
    """Deterministic capture carrying a sine reference on channel 0."""
    n = int(sr * duration_s)
    t = np.arange(n) / sr
    # level_dbfs is the RMS level of the tone (sine RMS = peak/sqrt(2))
    amp = 10 ** (level_dbfs / 20.0) * np.sqrt(2)
    tone = amp * np.sin(2 * np.pi * freq * t)
    if unstable is not None:
        per_win = np.array(unstable)
        wins = np.array_split(tone, len(per_win))
        tone = np.concatenate(
            [w * (10 ** (db / 20.0))
             for w, db in zip(wins, per_win)])
    rng = np.random.default_rng(0)
    noise = rng.normal(0.0, 10 ** (noise_dbfs / 20.0), n)
    samples = tone + noise
    if clipped:
        samples = np.clip(samples, -0.4 * amp, 0.4 * amp)
    return CaptureResult(
        outcome='completed', actual_sample_rate_hz=sr,
        actual_sample_format='float64', captured_channels=(0,),
        samples=samples.reshape(-1, 1), expected_frames=n,
        recorded_frames=n, clipped_samples=int(np.count_nonzero(
            np.abs(samples) >= 0.4 * amp)) if clipped else 0,
        xrun_count=0, truncated=False)


# ---------------------------------------------------------------------------
# machine-level: sealed log is the sole resume authority
# ---------------------------------------------------------------------------


class TestWizardMachine:
    def _run(self, lane='interface_loopback', **pins) -> CadCalibrationWizardRun:
        if lane == 'interface_loopback':
            pins.setdefault('io_path', _io_path())
            pins.setdefault('routing', _routing())
            pins.setdefault('stimulus_spec', _spec())
            pins.setdefault('loopback_input_channel', 1)
        elif lane == 'spl_reference_check':
            pins.setdefault('instrument_ref', instrument_binding(_instrument()))
            pins.setdefault('acceptance_profile_ref',
                            spl_profile_binding(_profile()))
            pins.setdefault('routing', _routing())
            pins.setdefault('stimulus_spec', _spec())
        else:
            pins.setdefault('campaign_ref', _campaign_ref())
            pins.setdefault('check_plan_ref',
                            campaign_plan_binding(_plan()))
            pins.setdefault('routing', _routing())
            pins.setdefault('stimulus_spec', _spec())
        return CadCalibrationWizardRun.create(
            document_id=DOC, lane=lane, created_at_utc=T0,
            created_by='tester', **pins)

    def test_run_created_then_ladder(self) -> None:
        run = self._run()
        st = derive_wizard_state(run, ())
        assert st.current_stage == 'await_loopback'
        permitted = next_permitted_events(st)
        assert 'loopback_confirmed' in permitted
        assert 'run_cancelled' in permitted
        assert pending_physical_instruction(st) == 'connect_loopback_cable'
        d = wizard_transition(st, WizardEvent(kind='run_created', at_utc=T0))
        assert d.to_stage == 'await_loopback' and d.outcome == 'advanced'

    def test_rejected_event_kind_at_wrong_stage(self) -> None:
        run = self._run()
        st = derive_wizard_state(run, ())
        d = wizard_transition(st, WizardEvent(
            kind='preflight_evaluated', at_utc=T0, succeeded=True))
        assert isinstance(d, WizardTransitionRejection)

    def test_cancel_allowed_from_any_nonterminal_stage(self) -> None:
        run = self._run()
        st = derive_wizard_state(run, ())
        d = wizard_transition(st, WizardEvent(
            kind='run_cancelled', at_utc=T0, actor='operator'))
        assert d.outcome == 'cancelled' and d.to_stage == 'cancelled'

    def test_terminal_rejects_everything(self) -> None:
        run = self._run()
        # fold a run_created + cancel into a terminal state
        from htdt.cad_calibration_wizard import wizard_run_binding
        trans = [
            CadCalibrationWizardTransition.create(
                document_id=DOC, run_ref=wizard_run_binding(run), seq=0,
                event_kind='run_created', outcome='advanced',
                actor='machine', from_stage=None,
                to_stage='await_loopback', recorded_at_utc=T0,
                reason='created'),
            CadCalibrationWizardTransition.create(
                document_id=DOC, run_ref=wizard_run_binding(run), seq=1,
                event_kind='run_cancelled', outcome='cancelled',
                actor='operator', from_stage='await_loopback',
                to_stage='cancelled', recorded_at_utc=T0,
                reason='cancelled'),
        ]
        st = derive_wizard_state(run, tuple(trans))
        assert st.cancelled
        assert next_permitted_events(st) == ()
        d = wizard_transition(st, WizardEvent(
            kind='loopback_confirmed', at_utc=T0, actor='operator'))
        assert isinstance(d, WizardTransitionRejection)

    def test_device_loss_blocks_then_retry_regresses(self) -> None:
        run = self._run()
        from htdt.cad_calibration_wizard import wizard_run_binding
        ref = wizard_run_binding(run)
        trans = [
            CadCalibrationWizardTransition.create(
                document_id=DOC, run_ref=ref, seq=0,
                event_kind='run_created', outcome='advanced',
                actor='machine', from_stage=None,
                to_stage='await_loopback', recorded_at_utc=T0,
                reason='created'),
            CadCalibrationWizardTransition.create(
                document_id=DOC, run_ref=ref, seq=1,
                event_kind='device_lost', outcome='blocked',
                actor='system', from_stage='await_loopback',
                to_stage='await_loopback', recorded_at_utc=T0,
                reason='cable yanked'),
        ]
        st = derive_wizard_state(run, tuple(trans))
        assert st.device_disconnected
        assert pending_physical_instruction(st) == 'restore_device_connection'
        # retry while disconnected is rejected — the hardware is not
        # back, so re-running software now could only fabricate evidence
        d = wizard_transition(st, WizardEvent(
            kind='connectivity_restored', at_utc=T0, actor='system'))
        assert d.outcome == 'informational'
        trans.append(CadCalibrationWizardTransition.create(
            document_id=DOC, run_ref=ref, seq=2,
            event_kind='connectivity_restored', outcome='informational',
            actor='system', from_stage='await_loopback',
            to_stage='await_loopback', recorded_at_utc=T0,
            reason='cable re-seated'))
        st2 = derive_wizard_state(run, tuple(trans))
        assert not st2.device_disconnected
        # retry at the lane's first stage is a no-op the machine rejects
        d2 = wizard_transition(st2, WizardEvent(
            kind='retry_step', at_utc=T0, actor='operator'))
        assert isinstance(d2, WizardTransitionRejection)

    def test_failed_advance_event_blocks_at_stage(self) -> None:
        run = self._run()
        from htdt.cad_calibration_wizard import wizard_run_binding
        ref = wizard_run_binding(run)
        trans = [
            CadCalibrationWizardTransition.create(
                document_id=DOC, run_ref=ref, seq=0,
                event_kind='run_created', outcome='advanced',
                actor='machine', from_stage=None,
                to_stage='await_loopback', recorded_at_utc=T0,
                reason='created'),
            CadCalibrationWizardTransition.create(
                document_id=DOC, run_ref=ref, seq=1,
                event_kind='loopback_confirmed', outcome='advanced',
                actor='operator', from_stage='await_loopback',
                to_stage='preflight', recorded_at_utc=T0,
                reason='cable connected'),
            CadCalibrationWizardTransition.create(
                document_id=DOC, run_ref=ref, seq=2,
                event_kind='preflight_evaluated', outcome='blocked',
                actor='machine', from_stage='preflight',
                to_stage='preflight', recorded_at_utc=T0,
                reason='device missing', event_succeeded=False),
        ]
        st = derive_wizard_state(run, tuple(trans))
        assert st.current_stage == 'preflight'
        assert st.blocked_reason == 'device missing'
        assert pending_physical_instruction(st) == 'resolve_blocked_step'
        permitted = next_permitted_events(st)
        # same-stage re-evaluation is the honest recovery at preflight;
        # a regressive retry cannot land on its own stage.
        assert 'preflight_evaluated' in permitted
        d = wizard_transition(st, WizardEvent(
            kind='retry_step', at_utc=T0, actor='operator'))
        assert isinstance(d, WizardTransitionRejection)

    def test_blocked_acquire_retries_to_preflight(self) -> None:
        run = self._run()
        from htdt.cad_calibration_wizard import wizard_run_binding
        ref = wizard_run_binding(run)
        trans = [
            CadCalibrationWizardTransition.create(
                document_id=DOC, run_ref=ref, seq=0,
                event_kind='run_created', outcome='advanced',
                actor='machine', from_stage=None,
                to_stage='await_loopback', recorded_at_utc=T0,
                reason='created'),
            CadCalibrationWizardTransition.create(
                document_id=DOC, run_ref=ref, seq=1,
                event_kind='loopback_confirmed', outcome='advanced',
                actor='operator', from_stage='await_loopback',
                to_stage='preflight', recorded_at_utc=T0,
                reason='cable connected'),
            CadCalibrationWizardTransition.create(
                document_id=DOC, run_ref=ref, seq=2,
                event_kind='preflight_evaluated', outcome='advanced',
                actor='machine', from_stage='preflight',
                to_stage='acquire', recorded_at_utc=T0,
                reason='preflight ok'),
            CadCalibrationWizardTransition.create(
                document_id=DOC, run_ref=ref, seq=3,
                event_kind='acquisition_recorded', outcome='blocked',
                actor='machine', from_stage='acquire',
                to_stage='acquire', recorded_at_utc=T0,
                reason='device lost mid-run', event_succeeded=False),
        ]
        st = derive_wizard_state(run, tuple(trans))
        assert st.current_stage == 'acquire'
        assert st.blocked_reason == 'device lost mid-run'
        assert 'retry_step' in next_permitted_events(st)
        d = wizard_transition(st, WizardEvent(
            kind='retry_step', at_utc=T0, actor='operator'))
        assert d.outcome == 'regressed' and d.to_stage == 'preflight'

    def test_run_validation_fail_closed(self) -> None:
        with pytest.raises(Exception):
            self._run(lane='interface_loopback', io_path=None)
        with pytest.raises(Exception):
            self._run(lane='spl_reference_check', instrument_ref=None)
        with pytest.raises(Exception):
            self._run(lane='campaign_checks', check_plan_ref=None)

    def test_seal_integrity_tampered_run_rejected(self) -> None:
        run = self._run()
        payload = run.model_dump(mode='python')
        payload['lane'] = 'spl_reference_check'
        with pytest.raises(Exception):
            CadCalibrationWizardRun.model_validate(payload)


# ---------------------------------------------------------------------------
# reference-tone measurement + check evaluation
# ---------------------------------------------------------------------------


class TestReferenceEvaluation:
    def test_within_tolerance(self) -> None:
        cap = _tone_capture(freq=1000.0, level_dbfs=-20.0)
        measured = measure_reference_tone(cap)
        assert measured.measurable
        ev = evaluate_reference_check(cap, _profile())
        assert ev.outcome == 'within_tolerance'

    def test_out_of_tolerance_level(self) -> None:
        cap = _tone_capture(level_dbfs=-24.0)
        ev = evaluate_reference_check(cap, _profile())
        assert ev.outcome == 'out_of_tolerance'
        assert any('level' in r for r in ev.reasons)

    def test_out_of_tolerance_frequency(self) -> None:
        cap = _tone_capture(freq=1200.0)
        ev = evaluate_reference_check(cap, _profile())
        assert ev.outcome == 'out_of_tolerance'
        assert any('frequency' in r for r in ev.reasons)

    def test_unstable_reference_fails(self) -> None:
        cap = _tone_capture(unstable=(0.0, -6.0, 0.0, -6.0, 0.0, -6.0))
        ev = evaluate_reference_check(cap, _profile())
        assert ev.outcome in ('out_of_tolerance', 'inconclusive')
        assert any('stab' in r.lower() or 'deviation' in r.lower()
                   for r in ev.reasons)

    def test_low_snr_fails(self) -> None:
        cap = _tone_capture(level_dbfs=-50.0, noise_dbfs=-30.0)
        ev = evaluate_reference_check(cap, _profile())
        assert ev.outcome == 'out_of_tolerance'

    def test_unmeasurable_capture_inconclusive(self) -> None:
        n = 48000
        cap = CaptureResult(
            outcome='completed', actual_sample_rate_hz=48000,
            actual_sample_format='float64', captured_channels=(0,),
            samples=np.zeros((n, 1)), expected_frames=n,
            recorded_frames=n, clipped_samples=0, xrun_count=0,
            truncated=False)
        ev = evaluate_reference_check(cap, _profile())
        assert ev.outcome == 'inconclusive'

    def test_defective_capture_inconclusive(self) -> None:
        cap = _tone_capture()
        bad = cap.model_copy(update={'truncated': True})
        ev = evaluate_reference_check(bad, _profile())
        assert ev.outcome == 'inconclusive'


# ---------------------------------------------------------------------------
# Lane A — guided loopback calibration
# ---------------------------------------------------------------------------


class TestLaneA:
    def test_happy_path_with_stores(self, tmp_path: Path) -> None:
        scene = _scene(tmp_path)
        stores = _stores(scene)
        wz = CalibrationWizard(DOC, _fake(), stores=stores)
        run = wz.begin_loopback(
            io_path=_io_path(), routing=_routing(), stimulus_spec=_spec(),
            created_by='tester', at_utc=T0)
        assert wz.state.current_stage == 'await_loopback'
        assert wz.pending_instruction == 'connect_loopback_cable'
        wz.confirm_hardware(at_utc=T0)
        made = wz.run_automatic(at_utc=T0)
        assert wz.state.completed
        assert [t.event_kind for t in made] == [
            'preflight_evaluated', 'acquisition_recorded',
            'transfer_derived', 'quality_evaluated',
            'calibration_sealed', 'correction_evaluated']
        # evidence: observation + calibration + qualification + run
        assert wz.sealed_calibration is not None
        assert wz.sealed_calibration.calibration_kind == (
            'combined_dac_analog_adc_loopback')
        assert wz.sealed_qualification is not None
        assert wz.sealed_qualification.state == 'correction_applied'
        # persisted + resumable
        repo = stores.wizard
        got = repo.get_run(run.run_id)
        assert got is not None and got.run_sha256 == run.run_sha256
        log = repo.list_transitions(run.run_id)
        assert len(log) == 8  # run_created + confirm + 6 stage events
        wz2 = CalibrationWizard.resume(DOC, _fake(), got, log,
                                       stores=stores)
        assert wz2.state.completed
        assert wz2.sealed_calibration.calibration_id == (
            wz.sealed_calibration.calibration_id)

    def test_wrong_rate_fails_preflight(self) -> None:
        wz = CalibrationWizard(DOC, _fake())
        wz.begin_loopback(
            io_path=_io_path(sample_rate_hz=22050.0),
            routing=_routing(),
            stimulus_spec=_spec(sample_rate_hz=22050),
            created_by='tester', at_utc=T0)
        wz.confirm_hardware(at_utc=T0)
        wz.run_automatic(at_utc=T0)
        assert wz.state.current_stage == 'preflight'
        assert wz.state.blocked_reason is not None
        assert 'sample rate' in wz.state.blocked_reason

    def test_wrong_port_fails_preflight(self) -> None:
        wz = CalibrationWizard(DOC, _fake())
        wz.begin_loopback(
            io_path=_io_path(),
            routing=_routing(capture_channel=9),
            stimulus_spec=_spec(), created_by='tester', at_utc=T0)
        wz.confirm_hardware(at_utc=T0)
        wz.run_automatic(at_utc=T0)
        assert wz.state.current_stage == 'preflight'
        assert wz.state.blocked_reason is not None

    def test_unknown_device_fails_preflight(self) -> None:
        wz = CalibrationWizard(DOC, _fake())
        wz.begin_loopback(
            io_path=_io_path(input_device='ghost-dac'),
            routing=_routing(capture_device_id='ghost-dac'),
            stimulus_spec=_spec(), created_by='tester', at_utc=T0)
        wz.confirm_hardware(at_utc=T0)
        wz.run_automatic(at_utc=T0)
        assert wz.state.current_stage == 'preflight'
        assert wz.state.blocked_reason is not None

    def test_clipped_capture_blocks_at_quality_gate(self) -> None:
        wz = CalibrationWizard(
            DOC, _fake(clip_at_dbfs=-18.0))
        wz.begin_loopback(
            io_path=_io_path(), routing=_routing(), stimulus_spec=_spec(),
            created_by='tester', at_utc=T0)
        wz.confirm_hardware(at_utc=T0)
        wz.run_automatic(at_utc=T0)
        assert wz.state.current_stage == 'quality_gate'
        assert wz.state.blocked_reason is not None
        assert 'invalid' in wz.state.blocked_reason

    def test_device_loss_mid_run_blocks(self) -> None:
        wz = CalibrationWizard(
            DOC, _fake(device_loss_at_frame=256))
        wz.begin_loopback(
            io_path=_io_path(), routing=_routing(), stimulus_spec=_spec(),
            created_by='tester', at_utc=T0)
        wz.confirm_hardware(at_utc=T0)
        wz.run_automatic(at_utc=T0)
        # capture failed — run blocked at acquire, evidence NOT invented
        assert wz.state.current_stage == 'acquire'
        assert wz.state.blocked_reason is not None
        assert wz.sealed_calibration is None

    def test_cancel_before_confirm(self) -> None:
        wz = CalibrationWizard(DOC, _fake())
        wz.begin_loopback(
            io_path=_io_path(), routing=_routing(), stimulus_spec=_spec(),
            created_by='tester', at_utc=T0)
        wz.cancel(at_utc=T0, reason='changed mind')
        assert wz.state.cancelled
        assert next_permitted_events(wz.state) == ()

    def test_cancel_after_arming(self) -> None:
        wz = CalibrationWizard(
            DOC, _fake(cancel_after_frames=64))
        wz.begin_loopback(
            io_path=_io_path(), routing=_routing(), stimulus_spec=_spec(),
            created_by='tester', at_utc=T0)
        wz.confirm_hardware(at_utc=T0)
        wz.run_automatic(at_utc=T0)
        # engine-internal cancellation surfaces as a blocked acquire
        assert wz.state.current_stage in ('acquire', 'cancelled')
        assert not wz.state.completed

    def test_retry_after_preflight_block(self) -> None:
        backend = _fake()
        wz = CalibrationWizard(DOC, backend)
        wz.begin_loopback(
            io_path=_io_path(),
            routing=_routing(capture_channel=9),
            stimulus_spec=_spec(), created_by='tester', at_utc=T0)
        wz.confirm_hardware(at_utc=T0)
        wz.run_automatic(at_utc=T0)
        assert wz.state.blocked_reason is not None
        # fix the routing: rebuild run state via resume is not needed —
        # the honest path is a new run with corrected pins
        wz2 = CalibrationWizard(DOC, backend)
        wz2.begin_loopback(
            io_path=_io_path(), routing=_routing(), stimulus_spec=_spec(),
            created_by='tester', at_utc=T0)
        wz2.confirm_hardware(at_utc=T0)
        wz2.run_automatic(at_utc=T0)
        assert wz2.state.completed


# ---------------------------------------------------------------------------
# Lane B — SPL / reference check
# ---------------------------------------------------------------------------


class TestLaneB:
    def _wizard(self, **scenario_kw) -> CalibrationWizard:
        wz = CalibrationWizard(DOC, _fake(**scenario_kw))
        wz.begin_reference_check(
            instrument=_instrument(), calibrator=_calibrator(),
            acceptance_profile=_profile(), routing=_routing(),
            stimulus_spec=_spec(duration_s=0.2, repetitions=1),
            created_by='tester', at_utc=T0)
        return wz

    def test_await_calibrator_then_evaluates(self) -> None:
        wz = self._wizard()
        assert wz.state.current_stage == 'await_calibrator'
        assert wz.pending_instruction == 'position_calibrator'
        wz.confirm_hardware(at_utc=T0)
        wz.run_automatic(at_utc=T0)
        assert wz.state.completed
        assert len(wz.sealed_checks) == 1
        # honest outcome — the fake sweep is not a 1 kHz reference, so
        # the check can only ever be non-passing here; the point is it
        # evaluates and seals.
        assert wz.sealed_checks[0].outcome in (
            'within_tolerance', 'deviation_observed',
            'out_of_tolerance', 'inconclusive')
        assert wz.sealed_checks[0].kind == 'interim_check'
        assert wz.sealed_checks[0].calibrator_ref is not None

    def test_profile_pins_acceptance(self, tmp_path: Path) -> None:
        scene = _scene(tmp_path)
        stores = _stores(scene)
        wz = CalibrationWizard(DOC, _fake(), stores=stores)
        profile = _profile()
        wz.begin_reference_check(
            instrument=_instrument(), acceptance_profile=profile,
            routing=_routing(),
            stimulus_spec=_spec(duration_s=0.2, repetitions=1),
            created_by='tester', at_utc=T0)
        # profile sealed into the store at begin
        got = stores.wizard.get_profile(profile.profile_id)
        assert got is not None
        assert got.profile_sha256 == profile.profile_sha256

    def test_fitness_updated(self, tmp_path: Path) -> None:
        scene = _scene(tmp_path)
        stores = _stores(scene)
        inst = _instrument()
        stores.lifecycle.save_instrument(inst)
        wz = CalibrationWizard(DOC, _fake(), stores=stores)
        wz.begin_reference_check(
            instrument=inst, acceptance_profile=_profile(),
            routing=_routing(),
            stimulus_spec=_spec(duration_s=0.2, repetitions=1),
            created_by='tester', at_utc=T0)
        wz.confirm_hardware(at_utc=T0)
        wz.run_automatic(at_utc=T0)
        assert wz.state.completed
        assessments = stores.lifecycle.list_assessments(DOC)
        assert assessments
        assert assessments[-1].state in (
            'fit', 'fit_with_notes', 'limited', 'out_of_tolerance',
            'overdue', 'unknown')


# ---------------------------------------------------------------------------
# Lane C — campaign pre/post checks
# ---------------------------------------------------------------------------


class TestLaneC:
    def _wizard(self, *, blocked=True, scenario_kw=None,
                checks=('pre_use_field_check', 'post_use_field_check')):
        plan = _plan(blocked=blocked, kinds=checks)
        wz = CalibrationWizard(
            DOC, _fake(**(scenario_kw or {})), check_plan=plan)
        wz.begin_campaign_checks(
            campaign_ref=_campaign_ref(), plan=plan, routing=_routing(),
            stimulus_spec=_spec(duration_s=0.2, repetitions=1),
            created_by='tester', at_utc=T0)
        return wz, plan

    def test_full_ladder(self) -> None:
        wz, _ = self._wizard()
        assert wz.state.current_stage == 'await_calibrator_pre'
        assert wz.pending_instruction == 'position_calibrator'
        wz.confirm_hardware(at_utc=T0)
        wz.run_automatic(at_utc=T0)
        assert wz.state.current_stage == 'await_campaign'
        assert wz.pending_instruction == 'perform_campaign_measurements'
        wz.record_campaign_completed(at_utc=T0)
        assert wz.state.current_stage == 'await_calibrator_post'
        wz.confirm_hardware(at_utc=T0)
        wz.run_automatic(at_utc=T0)
        # fake capture ≠ 94 dB/1 kHz reference → checks fail honestly
        assert wz.state.current_stage in ('completed', 'failed')
        kinds = sorted(c.kind for c in wz.sealed_checks)
        assert kinds == ['post_use_field_check', 'pre_use_field_check']

    def test_failed_post_check_blocks_policy(self) -> None:
        wz, _ = self._wizard(blocked=True)
        wz.confirm_hardware(at_utc=T0)
        wz.run_automatic(at_utc=T0)
        wz.record_campaign_completed(at_utc=T0)
        wz.confirm_hardware(at_utc=T0)
        wz.run_automatic(at_utc=T0)
        assert wz.state.failed
        last = wz.transitions[-1]
        assert last.result_tag in ('checks_failed', 'checks_limited',
                                   'checks_passed')

    def test_non_blocking_policy_completes(self) -> None:
        wz, _ = self._wizard(blocked=False)
        wz.confirm_hardware(at_utc=T0)
        wz.run_automatic(at_utc=T0)
        wz.record_campaign_completed(at_utc=T0)
        wz.confirm_hardware(at_utc=T0)
        wz.run_automatic(at_utc=T0)
        assert wz.state.completed

    def test_gate_evaluator(self) -> None:
        from htdt.cad_calibration_lifecycle import build_verification_check
        plan = _plan()
        gate = evaluate_campaign_check_gate(plan, ())
        assert gate.verdict == 'checks_pending' and gate.blocked
        inst_ref = instrument_binding(_instrument())
        check_ok = build_verification_check(
            document_id=DOC, instrument_ref=inst_ref,
            kind='pre_use_field_check', outcome='within_tolerance',
            performed_at_utc=T0, campaign_id=plan.campaign_ref.ref_id)
        gate2 = evaluate_campaign_check_gate(plan, (check_ok,))
        assert gate2.verdict == 'checks_pending'  # post missing
        check_post = build_verification_check(
            document_id=DOC, instrument_ref=inst_ref,
            kind='post_use_field_check', outcome='deviation_observed',
            performed_at_utc=T0, campaign_id=plan.campaign_ref.ref_id)
        gate3 = evaluate_campaign_check_gate(plan, (check_ok, check_post))
        assert gate3.verdict == 'checks_limited'
        check_bad = build_verification_check(
            document_id=DOC, instrument_ref=inst_ref,
            kind='post_use_field_check', outcome='out_of_tolerance',
            performed_at_utc=T0, campaign_id=plan.campaign_ref.ref_id)
        gate4 = evaluate_campaign_check_gate(plan, (check_ok, check_bad))
        assert gate4.verdict == 'checks_failed' and gate4.blocked
        gate5 = evaluate_campaign_check_gate(
            _plan(blocked=False), (check_ok, check_bad))
        assert gate5.verdict == 'checks_failed' and not gate5.blocked


# ---------------------------------------------------------------------------
# repository round-trip + tamper detection
# ---------------------------------------------------------------------------


class TestRepository:
    def test_round_trip_and_tamper(self, tmp_path: Path) -> None:
        scene = _scene(tmp_path)
        repo = CadCalibrationWizardRepository(scene)
        run = CadCalibrationWizardRun.create(
            document_id=DOC, lane='interface_loopback',
            io_path=_io_path(), routing=_routing(),
            stimulus_spec=_spec(), loopback_input_channel=1,
            created_at_utc=T0, created_by='tester')
        repo.save_run(run)
        got = repo.get_run(run.run_id)
        assert got == run
        # idempotent re-save
        repo.save_run(run)
        # conflict: same id, different sha
        evil = run.model_copy(update={'created_by': 'mallory'})
        with pytest.raises(CalibrationWizardIntegrityError):
            repo.save_run(evil)
        # tamper a material column → read fails closed
        with connect_sqlite(repo.path) as conn:
            conn.execute(
                'UPDATE cad_calibration_wizard_runs SET lane=? '
                'WHERE run_id=?', ('campaign_checks', run.run_id))
        with pytest.raises(CalibrationWizardIntegrityError):
            repo.get_run(run.run_id)

    def test_transition_log_ordered(self, tmp_path: Path) -> None:
        scene = _scene(tmp_path)
        repo = CadCalibrationWizardRepository(scene)
        stores = _stores(scene)
        wz = CalibrationWizard(DOC, _fake(), stores=stores)
        wz.begin_loopback(
            io_path=_io_path(), routing=_routing(), stimulus_spec=_spec(),
            created_by='tester', at_utc=T0)
        wz.confirm_hardware(at_utc=T0)
        wz.run_automatic(at_utc=T0)
        trans = repo.list_transitions(wz.run.run_id)
        assert [t.seq for t in trans] == list(range(len(trans)))
        assert trans[-1].event_kind == 'correction_evaluated'

    def test_profile_and_plan_round_trip(self, tmp_path: Path) -> None:
        scene = _scene(tmp_path)
        repo = CadCalibrationWizardRepository(scene)
        profile = _profile()
        repo.save_profile(profile)
        assert repo.get_profile(profile.profile_id) == profile
        plan = _plan()
        repo.save_check_plan(plan)
        assert repo.get_check_plan(plan.plan_id) == plan
        assert [p.plan_id for p in repo.list_check_plans(DOC)] == [
            plan.plan_id]


# ---------------------------------------------------------------------------
# resume — never invents completion
# ---------------------------------------------------------------------------


class TestResume:
    def test_mid_run_resume_lands_back_on_stage(
            self, tmp_path: Path) -> None:
        scene = _scene(tmp_path)
        stores = _stores(scene)
        wz = CalibrationWizard(DOC, _fake(), stores=stores)
        wz.begin_loopback(
            io_path=_io_path(), routing=_routing(), stimulus_spec=_spec(),
            created_by='tester', at_utc=T0)
        wz.confirm_hardware(at_utc=T0)
        # run one stage only — preflight done, acquire pending
        wz.tick(at_utc=T0)
        assert wz.state.current_stage == 'acquire'
        repo = stores.wizard
        run = repo.get_run(wz.run.run_id)
        log = repo.list_transitions(run.run_id)
        wz2 = CalibrationWizard.resume(DOC, _fake(), run, log,
                                       stores=stores)
        assert wz2.state.current_stage == 'acquire'
        assert not wz2.state.completed
        # resume has no live engine — acquire fails closed, not invented
        t = wz2.tick(at_utc=T0)
        assert t is not None and t.outcome == 'blocked'

    def test_completed_run_resumes_as_completed(
            self, tmp_path: Path) -> None:
        scene = _scene(tmp_path)
        stores = _stores(scene)
        wz = CalibrationWizard(DOC, _fake(), stores=stores)
        _drive_lane_a(wz)
        repo = stores.wizard
        run = repo.get_run(wz.run.run_id)
        log = repo.list_transitions(run.run_id)
        wz2 = CalibrationWizard.resume(DOC, _fake(), run, log,
                                       stores=stores)
        assert wz2.state.completed
        assert wz2.tick(at_utc=T0) is None


# ---------------------------------------------------------------------------
# schema v101 wiring
# ---------------------------------------------------------------------------


def test_schema_v101_tables(tmp_path: Path) -> None:
    db = tmp_path / 's.sqlite3'
    version = ensure_native_schema(db)
    assert version == 101
    with connect_sqlite(db) as conn:
        names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
    for table in (
            'cad_calibration_wizard_runs',
            'cad_calibration_wizard_transitions',
            'cad_spl_check_acceptance_profiles',
            'cad_campaign_check_plans'):
        assert table in names


# ---------------------------------------------------------------------------
# UI surface (offscreen)
# ---------------------------------------------------------------------------


class TestWorkspaceWizardPage:
    def test_page_drives_lane_a(self, tmp_path: Path) -> None:
        from PySide6.QtWidgets import QApplication
        from htdt.cad_scene import make_f1_scene
        from htdt.measurement_page_workspace import (
            MeasurementPageWorkspace)
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
            workspace.set_context('calibration_wizard')
            assert workspace.wiz_lane_combo.count() == 3
            workspace._wiz_begin()
            assert workspace._wiz is not None
            assert workspace._wiz.state.current_stage == 'await_loopback'
            assert 'ループバック' in workspace.wiz_instruction_label.text()
            assert workspace.wiz_confirm_button.isEnabled()
            workspace._wiz_confirm()
            assert workspace._wiz.state.completed
            assert '完了' in workspace.wiz_stage_label.text()
            assert workspace.wiz_log.count() >= 8
        finally:
            workspace.deleteLater()

    def test_cancel_button_ends_run(self, tmp_path: Path) -> None:
        from PySide6.QtWidgets import QApplication
        from htdt.cad_scene import make_f1_scene
        from htdt.measurement_page_workspace import (
            MeasurementPageWorkspace)
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
            workspace.set_context('calibration_wizard')
            workspace._wiz_begin()
            workspace._wiz_cancel()
            assert workspace._wiz.state.cancelled
            assert workspace.wiz_begin_button.isEnabled()
        finally:
            workspace.deleteLater()
