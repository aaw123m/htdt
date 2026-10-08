"""Issue #875 — automated multi-position measurement campaign (REV67).

CAMPAIGN -> POSITION -> CHANNEL -> REPETITION -> AUTO-SWEEP ->
QUALITY GATE -> RETRY/ADVANCE -> NEXT POSITION.

A registered campaign becomes a sealed executable plan whose exact run
queue is hash-pinned; the runner drives the #869 acquisition engine per
queue entry, gates each capture through the quality verdict, applies an
explicit retry/stop policy, pauses only for physical mic moves, and
journals every decision so a restart resumes the exact remaining work.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import pytest

from htdt.cad_authority_resolver import AuthorityRef
from htdt.cad_campaign_execution import (
    CAMPAIGN_EXECUTION_AUTHORITY_VERSION,
    CONFIRMATION_EVIDENCE_QUALITY,
    CampaignAutomationPolicy,
    CampaignChannelBinding,
    CampaignPlanError,
    CampaignPositionSpec,
    CampaignStateError,
    PositionConfirmation,
    PositionConfirmationError,
    build_campaign_execution_plan,
    derive_campaign_state,
    evaluate_position_confirmation,
    evaluate_retry_decision,
    materialize_run_queue,
)
from htdt.cad_campaign_execution_evidence import (
    CadCampaignExecutionEvent,
    CadCampaignRunRecord,
    build_campaign_event,
    build_campaign_run_record,
)
from htdt.cad_campaign_execution_runner import (
    MeasurementCampaignRunner,
    RunnerStepResult,
)
from htdt.cad_campaign_execution_repository import (
    CadCampaignExecutionRepository,
    CampaignExecutionConflictError,
    CampaignExecutionIntegrityError,
)
from htdt.cad_owned_room_campaign import MeasurementRole
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3
from htdt.cad_schema import connect_sqlite, ensure_native_schema
from htdt.cad_sweep_acquisition import (
    ArmConfirmation,
    FakeAudioBackend,
    MeasurementAcquisitionEngine,
    SweepStimulusSpec,
    default_fake_scenario,
)
from htdt.cad_sweep_acquisition_repository import CadSweepAcquisitionRepository

DOC = 'doc-875'


# ---------------------------------------------------------------------------
# helpers


def _clock() -> object:
    counter = {'n': 0}

    def _next() -> str:
        counter['n'] += 1
        return f'2026-10-07T00:{counter["n"] // 60:02d}:'
        + f'{counter["n"] % 60:02d}+00:00'

    return _next


def _stimulus(**kw) -> SweepStimulusSpec:
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


def _channel(channel_id: str = 'mic-1', **kw) -> CampaignChannelBinding:
    payload = dict(
        channel_entity_id=channel_id,
        playback_device_id='fake-duplex-0',
        playback_channel=0,
        capture_device_id='fake-duplex-0',
        capture_channel=0,
        loopback_input_channel=1,
    )
    payload.update(kw)
    return CampaignChannelBinding(**payload)


def _position(
    position_id: str = 'seat-1',
    roles: Sequence[MeasurementRole] = ('calibration',),
    **kw,
) -> CampaignPositionSpec:
    payload = dict(
        position_id=position_id,
        declared_position=Position3(x_m=1.0, y_m=2.0, z_m=1.0),
        roles=tuple(roles),
        required=True,
    )
    payload.update(kw)
    return CampaignPositionSpec(**payload)


def _campaign_ref(**kw) -> AuthorityRef:
    payload = dict(
        kind='campaign_preregistration',
        ref_id='crc-875',
        ref_sha256='a' * 64,
    )
    payload.update(kw)
    return AuthorityRef(**payload)


def _plan(
    positions: Sequence[CampaignPositionSpec] | None = None,
    channels: Sequence[CampaignChannelBinding] | None = None,
    repetitions: int = 1,
    policy: CampaignAutomationPolicy | None = None,
    **kw,
):
    payload = dict(
        document_id=DOC,
        campaign_ref=_campaign_ref(),
        stimulus_template=_stimulus(),
        channel_bindings=list(channels or [_channel()]),
        positions=list(positions or [_position()]),
        repetitions_per_entry=repetitions,
        policy=policy or CampaignAutomationPolicy(),
        generated_at_utc='2026-10-07T00:00:00+00:00',
    )
    payload.update(kw)
    return build_campaign_execution_plan(**payload)


def _engine_factory(
    scenarios: Sequence[dict] | dict | None = None,
):
    """One fresh engine per attempt; a scenario sequence drives per-attempt
    outcomes (first attempt uses scenarios[0], second scenarios[1], ...)."""

    queue: list[dict] = []
    if isinstance(scenarios, dict):
        queue = [scenarios]
    elif scenarios:
        queue = list(scenarios)
    calls = {'n': 0}
    default = queue[-1] if queue else {}

    def factory() -> MeasurementAcquisitionEngine:
        idx = calls['n']
        calls['n'] += 1
        kw = queue[idx] if idx < len(queue) else default
        return MeasurementAcquisitionEngine(
            FakeAudioBackend(default_fake_scenario(**kw)))

    return factory


def _arm(entry, request) -> ArmConfirmation:
    r = request.routing
    return ArmConfirmation(
        acknowledged_playback_device_id=r.playback_device_id,
        acknowledged_playback_channel=r.playback_channel,
        acknowledged_capture_device_id=r.capture_device_id,
        acknowledged_capture_channel=r.capture_channel,
        acknowledged_level_dbfs=request.stimulus.level_dbfs,
    )


def _runner(
    plan,
    *,
    scenarios=None,
    arm=_arm,
    events=None,
    records=None,
    clock=None,
    **kw,
) -> MeasurementCampaignRunner:
    return MeasurementCampaignRunner(
        plan=plan,
        engine_factory=_engine_factory(scenarios),
        event_sink=(events if events is not None else []).append,
        run_record_sink=(records if records is not None else []).append,
        arm_confirmation_provider=arm,
        clock=clock or _clock(),
        events=(),
        run_records=(),
        **kw,
    )


def _confirm_at(
    runner: MeasurementCampaignRunner,
    position_id: str,
    method: str = 'tracked_fixture',
) -> None:
    spec = runner.plan.position(position_id)
    assert spec is not None
    runner.confirm_position(PositionConfirmation(
        position_id=position_id,
        method=method,
        reported_position=spec.declared_position,
        at_utc=runner._clock(),
    ))


def _drive(
    runner: MeasurementCampaignRunner,
    method: str = 'tracked_fixture',
) -> RunnerStepResult:
    """Run the campaign to its first blocking/terminal state, auto-
    confirming every physical-move gate as it appears."""

    while True:
        result = runner.run_until_blocked()
        if result.action == 'awaiting_position':
            entry = runner.plan.entry(result.entry_key)
            assert entry is not None
            _confirm_at(runner, entry.position_id, method)
            continue
        return result


def _repos(tmp_path: Path):
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    scene = SceneRepository(db)
    sweep = CadSweepAcquisitionRepository(scene)
    campaign = CadCampaignExecutionRepository(
        scene, sweep_repository=sweep)
    return scene, sweep, campaign


# ---------------------------------------------------------------------------
# Plan materialization — the exact run queue
# ---------------------------------------------------------------------------


class TestQueueMaterialization:
    def test_position_channel_role_repetition_order(self) -> None:
        plan = _plan(
            positions=[
                _position('seat-a', roles=('calibration', 'repeatability')),
                _position('seat-b', roles=('calibration', 'repeatability')),
            ],
        )
        got = [
            (e.entry_key, e.position_id, e.role, e.run_index)
            for e in plan.queue
        ]
        assert got == [
            ('e0001', 'seat-a', 'calibration', 1),
            ('e0002', 'seat-a', 'repeatability', 1),
            ('e0003', 'seat-b', 'calibration', 1),
            ('e0004', 'seat-b', 'repeatability', 1),
        ]
        gates = [e.requires_position_confirmation for e in plan.queue]
        assert gates == [True, False, True, False]

    def test_channel_then_repetition_order(self) -> None:
        plan = _plan(
            channels=[_channel('ch-b'), _channel('ch-a')],
            repetitions=2,
        )
        # Channel order is the sorted binding order; repetitions nest inside.
        got = [(e.channel_entity_id, e.run_index) for e in plan.queue]
        assert got == [
            ('ch-a', 1), ('ch-a', 2), ('ch-b', 1), ('ch-b', 2)]

    def test_reconfirm_each_run_gates_every_entry(self) -> None:
        plan = _plan(
            repetitions=2,
            policy=CampaignAutomationPolicy(
                position_reconfirm_each_run=True),
        )
        assert all(e.requires_position_confirmation for e in plan.queue)

    def test_position_scoped_channel_subset(self) -> None:
        plan = _plan(
            channels=[_channel('ch-a'), _channel('ch-b')],
            positions=[_position(channel_entity_ids=('ch-b',))],
        )
        assert [e.channel_entity_id for e in plan.queue] == ['ch-b']

    def test_seal_is_deterministic(self) -> None:
        a = _plan()
        b = _plan()
        assert a.plan_id == b.plan_id
        assert a.plan_sha256 == b.plan_sha256

    def test_different_inputs_different_plan_identity(self) -> None:
        a = _plan()
        b = _plan(campaign_ref=_campaign_ref(ref_sha256='b' * 64))
        assert a.plan_id != b.plan_id

    def test_sealed_queue_must_match_materialization(self) -> None:
        plan = _plan(repetitions=2)
        payload = plan.model_dump(mode='python')
        payload['queue'] = payload['queue'][:-1]  # drop a required run
        with pytest.raises(ValueError, match='materialized queue'):
            type(plan)(**payload)

    def test_plan_tamper_rejected(self) -> None:
        plan = _plan()
        payload = plan.model_dump(mode='python')
        payload['positions'][0]['roles'] = ('holdout',)
        with pytest.raises(ValueError):
            type(plan)(**payload)

    def test_unbound_channel_rejected(self) -> None:
        with pytest.raises(CampaignPlanError, match='unbound'):
            _plan(positions=[_position(channel_entity_ids=('ghost',))])

    def test_duplicate_positions_rejected(self) -> None:
        with pytest.raises(CampaignPlanError, match='unique'):
            _plan(positions=[_position('p'), _position('p')])

    def test_no_required_run_rejected(self) -> None:
        with pytest.raises(CampaignPlanError, match='required'):
            _plan(positions=[_position(required=False)])

    def test_authority_version_embedded(self) -> None:
        assert _plan().authority_version == (
            CAMPAIGN_EXECUTION_AUTHORITY_VERSION)


# ---------------------------------------------------------------------------
# Position-confirmation ladder — attestation is never measured evidence
# ---------------------------------------------------------------------------


class TestPositionConfirmationLadder:
    def test_method_to_evidence_quality_mapping(self) -> None:
        assert CONFIRMATION_EVIDENCE_QUALITY == {
            'operator_attest': 'attested',
            'coordinate_entry': 'entered',
            'survey_import': 'entered',
            'tracked_fixture': 'measured',
            'position_tracker': 'measured',
        }

    def test_coordinate_methods_require_reported_position(self) -> None:
        for method in (
            'coordinate_entry', 'survey_import', 'tracked_fixture',
            'position_tracker'):
            with pytest.raises(ValueError, match='reported_position'):
                PositionConfirmation(
                    position_id='seat-1', method=method,
                    at_utc='2026-10-07T00:00:00+00:00')
        # operator_attest carries no coordinate — attested, not measured.
        att = PositionConfirmation(
            position_id='seat-1', method='operator_attest',
            at_utc='2026-10-07T00:00:00+00:00')
        assert att.evidence_quality == 'attested'
        assert not att.measured

    def test_measured_methods_mark_measured(self) -> None:
        conf = PositionConfirmation(
            position_id='seat-1', method='tracked_fixture',
            reported_position=Position3(x_m=1, y_m=2, z_m=1),
            at_utc='2026-10-07T00:00:00+00:00')
        assert conf.measured

    def test_out_of_tolerance_report_rejected(self) -> None:
        spec = _position('seat-1')
        conf = PositionConfirmation(
            position_id='seat-1', method='coordinate_entry',
            reported_position=Position3(x_m=1.5, y_m=2.0, z_m=1.0),
            at_utc='2026-10-07T00:00:00+00:00')
        problems = evaluate_position_confirmation(conf, spec, 0.05)
        assert problems and 'tolerance' in problems[0]

    def test_in_tolerance_report_accepted(self) -> None:
        spec = _position('seat-1')
        conf = PositionConfirmation(
            position_id='seat-1', method='survey_import',
            reported_position=Position3(x_m=1.01, y_m=2.0, z_m=1.0),
            at_utc='2026-10-07T00:00:00+00:00')
        assert evaluate_position_confirmation(conf, spec, 0.05) == ()

    def test_wrong_position_id_rejected(self) -> None:
        conf = PositionConfirmation(
            position_id='other', method='operator_attest',
            at_utc='2026-10-07T00:00:00+00:00')
        assert evaluate_position_confirmation(
            conf, _position('seat-1'), 0.05)


# ---------------------------------------------------------------------------
# Retry / stop policy — deterministic decision table
# ---------------------------------------------------------------------------


class TestRetryPolicy:
    def _entry(self, role='calibration'):
        return _plan(positions=[_position(roles=(role,))]).queue[0]

    def test_clipping_retry_once_at_reduced_level(self) -> None:
        policy = CampaignAutomationPolicy(
            retry_clipping_once_at_reduced_level=True,
            reduced_level_step_db=6.0)
        out = evaluate_retry_decision(
            policy=policy, entry=self._entry(),
            attempt_level_dbfs=-12.0, attempts_used=1,
            quality_reasons=('clipping_detected',),
            routing_drift=False, consecutive_quality_failures=1,
            clipping_retry_used=False)
        assert out.decision == 'retry_reduced_level'
        assert out.reduced_level_dbfs == -18.0

    def test_clipping_retry_not_offered_twice(self) -> None:
        policy = CampaignAutomationPolicy(
            retry_clipping_once_at_reduced_level=True)
        out = evaluate_retry_decision(
            policy=policy, entry=self._entry(),
            attempt_level_dbfs=-18.0, attempts_used=1,
            quality_reasons=('clipping_detected',),
            routing_drift=False, consecutive_quality_failures=1,
            clipping_retry_used=True)
        assert out.decision == 'fail_entry'

    def test_no_policy_means_no_retry(self) -> None:
        out = evaluate_retry_decision(
            policy=CampaignAutomationPolicy(), entry=self._entry(),
            attempt_level_dbfs=-12.0, attempts_used=1,
            quality_reasons=('clipping_detected',),
            routing_drift=False, consecutive_quality_failures=1,
            clipping_retry_used=False)
        assert out.decision == 'fail_entry'

    def test_transient_device_failure_retry(self) -> None:
        policy = CampaignAutomationPolicy(
            retry_transient_device_failure=True)
        out = evaluate_retry_decision(
            policy=policy, entry=self._entry(),
            attempt_level_dbfs=-12.0, attempts_used=1,
            quality_reasons=('device_lost',),
            routing_drift=False, consecutive_quality_failures=1,
            clipping_retry_used=False)
        assert out.decision == 'retry'

    def test_attempt_budget_exhausted(self) -> None:
        policy = CampaignAutomationPolicy(
            retry_transient_device_failure=True,
            max_attempts_per_entry=1)
        out = evaluate_retry_decision(
            policy=policy, entry=self._entry(),
            attempt_level_dbfs=-12.0, attempts_used=1,
            quality_reasons=('device_lost',),
            routing_drift=False, consecutive_quality_failures=1,
            clipping_retry_used=False)
        assert out.decision == 'fail_entry'
        assert 'budget' in out.reason

    def test_holdout_never_retries_after_drift(self) -> None:
        policy = CampaignAutomationPolicy(
            retry_transient_device_failure=True)
        out = evaluate_retry_decision(
            policy=policy, entry=self._entry('holdout'),
            attempt_level_dbfs=-12.0, attempts_used=1,
            quality_reasons=('device_lost',),
            routing_drift=True, consecutive_quality_failures=0,
            clipping_retry_used=False)
        assert out.decision == 'fail_entry'
        assert 'holdout' in out.reason

    def test_holdout_retry_allowed_only_with_explicit_opt_in(self) -> None:
        policy = CampaignAutomationPolicy(
            retry_transient_device_failure=True,
            holdout_retry_on_state_drift=True)
        out = evaluate_retry_decision(
            policy=policy, entry=self._entry('holdout'),
            attempt_level_dbfs=-12.0, attempts_used=1,
            quality_reasons=('device_lost',),
            routing_drift=True, consecutive_quality_failures=0,
            clipping_retry_used=False)
        assert out.decision == 'retry'

    def test_repeated_quality_failure_stops_campaign(self) -> None:
        policy = CampaignAutomationPolicy(
            retry_transient_device_failure=True,
            stop_after_consecutive_quality_failures=2)
        out = evaluate_retry_decision(
            policy=policy, entry=self._entry(),
            attempt_level_dbfs=-12.0, attempts_used=1,
            quality_reasons=('device_lost',),
            routing_drift=False, consecutive_quality_failures=2,
            clipping_retry_used=False)
        assert out.decision == 'stop_campaign'

    def test_cancelled_attempts_never_retry(self) -> None:
        policy = CampaignAutomationPolicy(
            retry_transient_device_failure=True)
        out = evaluate_retry_decision(
            policy=policy, entry=self._entry(),
            attempt_level_dbfs=-12.0, attempts_used=1,
            quality_reasons=('cancelled',),
            routing_drift=False, consecutive_quality_failures=0,
            clipping_retry_used=False)
        assert out.decision == 'fail_entry'


# ---------------------------------------------------------------------------
# Runner — full campaign drive
# ---------------------------------------------------------------------------


class TestRunnerHappyPath:
    def test_full_campaign_without_manual_starts(self) -> None:
        plan = _plan(
            positions=[_position('seat-a'), _position('seat-b')],
            channels=[_channel('ch-a'), _channel('ch-b')],
            repetitions=2,
        )
        events: list = []
        records: list = []
        runner = _runner(plan, events=events, records=records)
        result = _drive(runner)
        assert result.action == 'terminal'
        state = runner.state
        assert state.outcome == 'completed'
        assert state.progress.completed == 8
        assert state.progress.total == 8
        assert state.progress.remaining == 0
        # Exactly two physical-move prompts — one per position.
        gates = [e for e in events if e.kind == 'position_gate_opened']
        assert len(gates) == 2
        # Every attempt recorded once, in queue order.
        assert [r.entry_ordinal for r in records] == [1, 2, 3, 4, 5, 6, 7, 8]
        assert all(r.outcome == 'completed' for r in records)
        assert all(r.attempt == 1 for r in records)

    def test_terminal_campaign_completed_event_journaled(self) -> None:
        events: list = []
        runner = _runner(_plan(), events=events)
        _drive(runner)
        assert events[-1].kind == 'campaign_completed'
        assert events[0].kind == 'plan_registered'

    def test_position_gate_blocks_until_confirmed(self) -> None:
        engine_calls = {'n': 0}

        def counting_factory() -> MeasurementAcquisitionEngine:
            engine_calls['n'] += 1
            return MeasurementAcquisitionEngine(
                FakeAudioBackend(default_fake_scenario()))

        plan = _plan()
        runner = MeasurementCampaignRunner(
            plan=plan, engine_factory=counting_factory,
            event_sink=lambda e: None, run_record_sink=lambda r: None,
            arm_confirmation_provider=_arm, clock=_clock())
        result = runner.step()
        assert result.action == 'awaiting_position'
        assert engine_calls['n'] == 0
        assert runner.step().action == 'awaiting_position'
        _confirm_at(runner, 'seat-1')
        assert _drive(runner).action == 'terminal'
        assert engine_calls['n'] == 1

    def test_attested_confirmation_carries_attested_evidence(self) -> None:
        events: list = []
        runner = _runner(_plan(), events=events)
        _drive(runner, method='operator_attest')
        confirms = [e for e in events if e.kind == 'position_confirmed']
        assert confirms[0].confirmation_evidence_quality == 'attested'
        gate = runner.state.position_gates['seat-1']
        assert gate.confirmations == (
            ('operator_attest', 'attested', confirms[0].at_utc),)
        assert runner.state.progress.attested_confirmations == 1
        assert runner.state.progress.measured_confirmations == 0

    def test_confirmation_beyond_tolerance_rejected(self) -> None:
        plan = _plan()
        runner = _runner(plan)
        runner.step()
        with pytest.raises(PositionConfirmationError, match='tolerance'):
            runner.confirm_position(PositionConfirmation(
                position_id='seat-1', method='coordinate_entry',
                reported_position=Position3(x_m=9.0, y_m=9.0, z_m=9.0),
                at_utc='2026-10-07T00:00:00+00:00'))

    def test_run_identity_bound_per_record(self) -> None:
        plan = _plan(repetitions=2)
        records: list = []
        runner = _runner(plan, records=records)
        _drive(runner)
        assert len(records) == 2
        for i, record in enumerate(records, start=1):
            assert record.plan_sha256 == plan.plan_sha256
            assert record.entry_key == f'e{i:04d}'
            assert record.run_index == i
            assert record.role == 'calibration'
            assert record.required
            assert record.position_id == 'seat-1'
            assert record.channel_entity_id == 'mic-1'
            assert record.level_dbfs == pytest.approx(-12.0)
            assert record.engine_run_id
            assert record.backend_id == 'fake-audio-io'
            assert record.backend_is_simulated


class TestRunnerFailurePaths:
    def test_clipping_retries_once_at_reduced_level(self) -> None:
        plan = _plan(policy=CampaignAutomationPolicy(
            retry_clipping_once_at_reduced_level=True,
            reduced_level_step_db=8.0))
        events: list = []
        records: list = []
        runner = _runner(
            plan, events=events, records=records,
            scenarios=[{'clip_at_dbfs': -14.0}, {}],
        )
        assert _drive(runner).action == 'terminal'
        assert runner.state.outcome == 'completed'
        assert [r.attempt for r in records] == [1, 2]
        assert records[0].outcome == 'failed'
        assert 'clipping_detected' in records[0].quality_reasons
        assert records[0].level_dbfs == pytest.approx(-12.0)
        assert records[1].level_dbfs == pytest.approx(-20.0)
        assert records[1].outcome == 'completed'
        decisions = [e for e in events if e.kind == 'retry_scheduled']
        assert decisions[0].retry_decision == 'retry_reduced_level'
        assert decisions[0].reduced_level_dbfs == pytest.approx(-20.0)

    def test_failed_attempts_stay_evidence(self) -> None:
        plan = _plan(policy=CampaignAutomationPolicy(
            retry_transient_device_failure=True,
            max_attempts_per_entry=3))
        records: list = []
        runner = _runner(
            plan, records=records,
            scenarios={'device_loss_at_frame': 10})
        assert _drive(runner).action == 'terminal'
        assert runner.state.outcome == 'failed'
        # budget 3 -> three attempt records, all retained, none overwritten
        assert [r.attempt for r in records] == [1, 2, 3]
        assert all(r.outcome == 'failed' for r in records)
        assert len({r.run_record_id for r in records}) == 3
        fails = [e for e in runner.events if e.kind == 'entry_failed']
        assert len(fails) == 1 and 'budget' in fails[0].reason

    def test_policy_stop_after_repeated_quality_failure(self) -> None:
        plan = _plan(
            repetitions=3,
            policy=CampaignAutomationPolicy(
                stop_after_consecutive_quality_failures=2),
        )
        events: list = []
        runner = _runner(
            plan, events=events,
            scenarios={'clip_at_dbfs': -14.0})
        assert _drive(runner).action == 'terminal'
        state = runner.state
        assert state.outcome == 'failed'
        stops = [e for e in events if e.kind == 'policy_stop']
        assert len(stops) == 1
        # e0001 and e0002 failed; e0003 never started — nothing is skipped
        # silently, the journal shows exactly what remains.
        assert state.entries['e0003'].state == 'pending'
        assert state.progress.remaining == 1
        started = [e.entry_key for e in events if e.kind == 'run_started']
        assert started == ['e0001', 'e0002']

    def test_holdout_denied_retry_after_routing_change(self) -> None:
        plan = _plan(
            positions=[_position(roles=('holdout',))],
            policy=CampaignAutomationPolicy(
                retry_transient_device_failure=True),
        )
        runner = _runner(plan, scenarios={'device_loss_at_frame': 10})
        runner.report_routing_change('operator swapped the preamp')
        assert runner.state.outcome == 'blocked'
        runner.resume(reason='routing restored')
        assert _drive(runner).action == 'terminal'
        events = runner.events
        assert not any(e.kind == 'retry_scheduled' for e in events)
        failed = [e for e in events if e.kind == 'entry_failed']
        assert 'state drift' in failed[0].reason

    def test_calibration_role_still_retries_after_drift(self) -> None:
        plan = _plan(
            positions=[_position(roles=('calibration',))],
            policy=CampaignAutomationPolicy(
                retry_transient_device_failure=True),
        )
        runner = _runner(
            plan, scenarios=[{'device_loss_at_frame': 10}, {}])
        runner.report_routing_change('operator swapped the preamp')
        runner.resume()
        assert _drive(runner).action == 'terminal'
        assert runner.state.outcome == 'completed'
        kinds = [e.kind for e in runner.events]
        assert 'retry_scheduled' in kinds

    def test_unexpected_routing_change_blocks_until_resume(self) -> None:
        runner = _runner(_plan())
        runner.report_routing_change('capture device renumbered')
        state = runner.state
        assert state.outcome == 'blocked'
        assert runner.step().action == 'blocked'
        runner.resume()
        assert _drive(runner).action == 'terminal'

    def test_pause_resume(self) -> None:
        runner = _runner(_plan())
        runner.pause('operator stepping away')
        assert runner.step().action == 'paused'
        with pytest.raises(CampaignStateError):
            runner.pause()
        runner.resume()
        assert _drive(runner).action == 'terminal'
        assert runner.state.outcome == 'completed'

    def test_cancel_halts_remaining_work(self) -> None:
        plan = _plan(repetitions=3)
        events: list = []
        runner = _runner(plan, events=events)
        runner.step()   # opens the gate
        _confirm_at(runner, 'seat-1')
        runner.step()   # runs e0001
        runner.cancel()
        assert _drive(runner).action == 'terminal'
        state = runner.state
        assert state.outcome == 'cancelled'
        started = [e.entry_key for e in events if e.kind == 'run_started']
        assert started == ['e0001']
        assert state.entries['e0002'].state == 'pending'
        assert state.entries['e0003'].state == 'pending'
        assert state.progress.remaining == 2

    def test_arm_confirmation_unavailable_is_honest_failure(self) -> None:
        runner = _runner(_plan(), arm=None)
        assert _drive(runner).action == 'terminal'
        assert runner.state.outcome == 'failed'
        record = runner.run_records[0]
        assert record.outcome == 'failed'
        assert record.failure_kind == 'arm_confirmation_unavailable'
        assert record.acquisition_ref is None

    def test_arm_mismatch_blocks_the_attempt(self) -> None:
        def bad_arm(entry, request):
            return ArmConfirmation(
                acknowledged_playback_device_id='other-device',
                acknowledged_playback_channel=0,
                acknowledged_capture_device_id='fake-duplex-0',
                acknowledged_capture_channel=0,
                acknowledged_level_dbfs=-12.0)

        runner = _runner(_plan(), arm=bad_arm)
        assert _drive(runner).action == 'terminal'
        record = runner.run_records[0]
        assert record.outcome == 'failed'
        assert record.failure_kind == 'arm_blocked'

    def test_auto_arm_requires_explicit_policy(self) -> None:
        plan = _plan(policy=CampaignAutomationPolicy(auto_arm=True))
        runner = _runner(plan, arm=None)
        assert _drive(runner).action == 'terminal'
        assert runner.state.outcome == 'completed'


class TestRunnerControl:
    def test_waive_optional_entry(self) -> None:
        plan = _plan(positions=[
            _position('seat-a'),
            _position('seat-b', required=False),
        ])
        runner = _runner(plan)
        result = runner.step()
        assert result.action == 'awaiting_position'
        # Waive seat-b's entry while still parked at seat-a.
        waived = runner.waive_entry('e0002', reason='seat unused')
        assert waived.kind == 'entry_waived'
        _drive(runner)
        state = runner.state
        assert state.entries['e0002'].state == 'waived'
        assert state.outcome == 'completed'

    def test_required_entry_cannot_be_waived_by_default(self) -> None:
        runner = _runner(_plan())
        with pytest.raises(CampaignStateError, match='required'):
            runner.waive_entry('e0001', reason='skip it')

    def test_required_waive_needs_explicit_policy(self) -> None:
        plan = _plan(policy=CampaignAutomationPolicy(
            allow_waived_required=True))
        runner = _runner(plan)
        runner.waive_entry('e0001', reason='documented gap')
        result = _drive(runner)
        assert result.action == 'terminal'
        # 'waived' satisfies a required run ONLY under the explicit policy.
        assert runner.state.outcome == 'completed'

    def test_actions_on_terminal_campaign_rejected(self) -> None:
        runner = _runner(_plan())
        _drive(runner)
        with pytest.raises(CampaignStateError):
            runner.pause()
        with pytest.raises(CampaignStateError):
            runner.cancel()
        with pytest.raises(CampaignStateError):
            runner.report_routing_change('too late')


# ---------------------------------------------------------------------------
# Restart recovery — the journal IS the state
# ---------------------------------------------------------------------------


class TestRestartRecovery:
    def test_journal_fold_reproduces_state(self) -> None:
        plan = _plan(repetitions=2)
        events: list = []
        records: list = []
        runner = _runner(plan, events=events, records=records)
        _drive(runner)
        derived = derive_campaign_state(plan, events, records)
        assert derived.outcome == 'completed'
        assert derived.progress.completed == 2

    def test_recovery_resumes_exact_remaining_work(self) -> None:
        plan = _plan(
            positions=[_position('seat-a'), _position('seat-b')])
        events: list = []
        records: list = []
        runner = _runner(plan, events=events, records=records)
        # Complete seat-a's run, then the app "restarts".
        runner.step()
        _confirm_at(runner, 'seat-a')
        runner.step()
        assert len(records) == 1

        recovered = MeasurementCampaignRunner(
            plan=plan,
            engine_factory=_engine_factory(),
            event_sink=events.append,
            run_record_sink=records.append,
            arm_confirmation_provider=_arm,
            clock=_clock(),
            events=tuple(events), run_records=tuple(records))
        result = _drive(recovered)
        assert result.action == 'terminal'
        assert recovered.state.outcome == 'completed'
        assert len(records) == 2
        # seat-a was confirmed before restart — still confirmed after.
        assert recovered.state.position_gates['seat-a'].confirmed

    def test_in_flight_run_requeues_after_crash(self) -> None:
        plan = _plan()
        clock = _clock()
        # Simulate a crash mid-attempt: journal has run_started but no
        # run record — the crash never sealed evidence.
        events = [
            build_campaign_event(
                document_id=DOC, plan=plan, seq=1, kind='plan_registered',
                at_utc=clock()),
            build_campaign_event(
                document_id=DOC, plan=plan, seq=2,
                kind='position_gate_opened', entry_key='e0001',
                position_id='seat-1', at_utc=clock()),
            build_campaign_event(
                document_id=DOC, plan=plan, seq=3,
                kind='position_confirmed', position_id='seat-1',
                confirmation_method='operator_attest',
                confirmation_evidence_quality='attested',
                actor='operator', at_utc=clock()),
            build_campaign_event(
                document_id=DOC, plan=plan, seq=4, kind='run_started',
                entry_key='e0001', position_id='seat-1', attempt=1,
                at_utc=clock()),
        ]
        recovered_events: list = list(events)
        records: list = []
        runner = MeasurementCampaignRunner(
            plan=plan,
            engine_factory=_engine_factory(),
            event_sink=recovered_events.append,
            run_record_sink=records.append,
            arm_confirmation_provider=_arm,
            clock=clock,
            events=tuple(events), run_records=())
        recovered_kinds = [e.kind for e in recovered_events[len(events):]]
        assert recovered_kinds[0] == 'restart_recovered'
        # The interrupted entry is requeued — the run happens, it is
        # never assumed done.
        assert _drive(runner).action == 'terminal'
        assert runner.state.outcome == 'completed'
        assert len(records) == 1
        assert records[0].attempt == 1

    def test_journal_from_another_plan_rejected(self) -> None:
        plan_a = _plan()
        plan_b = _plan(campaign_ref=_campaign_ref(ref_sha256='b' * 64))
        foreign = [build_campaign_event(
            document_id=DOC, plan=plan_b, seq=1,
            kind='plan_registered', at_utc='2026-10-07T00:00:00+00:00')]
        with pytest.raises(CampaignStateError, match='different plan'):
            MeasurementCampaignRunner(
                plan=plan_a, engine_factory=_engine_factory(),
                event_sink=lambda e: None, run_record_sink=lambda r: None,
                clock=_clock(), events=tuple(foreign))

    def test_unordered_journal_rejected(self) -> None:
        plan = _plan()
        clock = _clock()
        events = [
            build_campaign_event(
                document_id=DOC, plan=plan, seq=1, kind='plan_registered',
                at_utc=clock()),
            build_campaign_event(
                document_id=DOC, plan=plan, seq=1, kind='paused',
                actor='operator', at_utc=clock()),
        ]
        with pytest.raises(CampaignStateError, match='seq'):
            MeasurementCampaignRunner(
                plan=plan, engine_factory=_engine_factory(),
                event_sink=lambda e: None, run_record_sink=lambda r: None,
                clock=clock, events=tuple(events))


# ---------------------------------------------------------------------------
# Evidence records — seal integrity
# ---------------------------------------------------------------------------


class TestSealedEvidence:
    def test_event_id_derived_from_payload(self) -> None:
        plan = _plan()
        event = build_campaign_event(
            document_id=DOC, plan=plan, seq=1, kind='plan_registered',
            at_utc='2026-10-07T00:00:00+00:00')
        assert event.event_id.startswith('mcevt-')
        assert len(event.event_sha256) == 64
        assert event.plan_sha256 == plan.plan_sha256

    def test_event_payload_tamper_rejected(self) -> None:
        plan = _plan()
        event = build_campaign_event(
            document_id=DOC, plan=plan, seq=1, kind='paused',
            actor='operator', at_utc='2026-10-07T00:00:00+00:00')
        payload = event.model_dump(mode='python')
        payload['kind'] = 'resumed'
        with pytest.raises(ValueError, match='hash mismatch'):
            CadCampaignExecutionEvent(**payload)

    def test_run_record_identity_and_sha(self) -> None:
        plan = _plan()
        entry = plan.queue[0]
        record = build_campaign_run_record(
            document_id=DOC, plan=plan, entry=entry, attempt=1,
            routing=plan.channel(entry.channel_entity_id).routing(),
            level_dbfs=-12.0, outcome='completed', failure_kind=None,
            quality_verdict='valid', quality_reasons=(),
            started_at_utc='2026-10-07T00:00:00+00:00',
            completed_at_utc='2026-10-07T00:00:01+00:00')
        assert record.run_record_id.startswith('mcrun-')
        payload = record.model_dump(mode='python')
        payload['attempt'] = 99
        with pytest.raises(ValueError, match='hash mismatch'):
            CadCampaignRunRecord(**payload)

    def test_failed_record_requires_failure_kind(self) -> None:
        plan = _plan()
        entry = plan.queue[0]
        with pytest.raises(ValueError, match='failure kind'):
            build_campaign_run_record(
                document_id=DOC, plan=plan, entry=entry, attempt=1,
                routing=plan.channel(
                    entry.channel_entity_id).routing(),
                level_dbfs=-12.0, outcome='failed', failure_kind=None,
                quality_verdict='invalid',
                quality_reasons=('clipping_detected',),
                started_at_utc='2026-10-07T00:00:00+00:00',
                completed_at_utc='2026-10-07T00:00:01+00:00')

    def test_completed_record_requires_quality_verdict(self) -> None:
        plan = _plan()
        entry = plan.queue[0]
        with pytest.raises(ValueError, match='quality verdict'):
            build_campaign_run_record(
                document_id=DOC, plan=plan, entry=entry, attempt=1,
                routing=plan.channel(
                    entry.channel_entity_id).routing(),
                level_dbfs=-12.0, outcome='completed', failure_kind=None,
                quality_verdict=None, quality_reasons=(),
                started_at_utc='2026-10-07T00:00:00+00:00',
                completed_at_utc='2026-10-07T00:00:01+00:00')


# ---------------------------------------------------------------------------
# Repository — append-only persistence + tamper detection
# ---------------------------------------------------------------------------


class TestRepository:
    def test_round_trip(self, tmp_path: Path) -> None:
        _scene, sweep, repo = _repos(tmp_path)
        plan = _plan()
        repo.save_plan(plan)
        assert repo.get_plan(plan.plan_id) == plan

        clock = _clock()
        runner = repo.runner_for(
            plan, engine_factory=_engine_factory(),
            arm_confirmation_provider=_arm, clock=clock)
        _drive(runner)
        stored_events = repo.list_events(plan.plan_id)
        assert len(stored_events) == len(runner.events)
        assert stored_events[0].kind == 'plan_registered'
        assert stored_events[-1].kind == 'campaign_completed'
        stored_runs = repo.list_run_records(plan.plan_id)
        assert len(stored_runs) == 1
        assert stored_runs[0].outcome == 'completed'
        # The sealed #869 evidence was persisted by the acquisition sink.
        assert stored_runs[0].acquisition_ref is not None
        acq = sweep.get_acquisition_run(stored_runs[0].acquisition_ref.ref_id)
        assert acq is not None
        assert acq.campaign_ref is not None
        assert acq.campaign_ref.ref_id == plan.plan_id

    def test_runner_for_recovers_from_persisted_journal(
            self, tmp_path: Path) -> None:
        _scene, _sweep, repo = _repos(tmp_path)
        plan = _plan(
            positions=[_position('seat-a'), _position('seat-b')])
        repo.save_plan(plan)
        runner = repo.runner_for(
            plan, engine_factory=_engine_factory(),
            arm_confirmation_provider=_arm, clock=_clock())
        runner.step()
        _confirm_at(runner, 'seat-a')
        runner.step()

        # "Restart": a fresh runner over the persisted journal resumes
        # at seat-b — seat-a's completed run is never re-executed.
        resumed = repo.runner_for(
            plan, engine_factory=_engine_factory(),
            arm_confirmation_provider=_arm, clock=_clock())
        assert _drive(resumed).action == 'terminal'
        assert resumed.state.outcome == 'completed'
        runs = repo.list_run_records(plan.plan_id)
        assert [r.position_id for r in runs] == ['seat-a', 'seat-b']

    def test_append_only_conflict(self, tmp_path: Path) -> None:
        _scene, _sweep, repo = _repos(tmp_path)
        plan = _plan()
        repo.save_plan(plan)
        repo.save_plan(plan)  # same sha — idempotent
        payload = plan.model_dump(mode='python')
        payload['notes'] = 'mutated'
        payload.pop('plan_sha256')
        payload.pop('plan_id')
        other = type(plan).create(**payload)
        assert other.plan_sha256 != plan.plan_sha256
        repo.save_plan(other)  # different id — fine
        # Same id, different sha: fabricate by resealing a mutated copy
        # with the original id — the model validator prevents that, so
        # simulate at the store layer with a tampered record object.
        forged = plan.model_dump(mode='python')
        forged['notes'] = 'forged'
        forged_plan = type(plan).create(**{
            k: v for k, v in forged.items()
            if k not in ('plan_id', 'plan_sha256')})
        object.__setattr__(forged_plan, 'plan_id', plan.plan_id)
        object.__setattr__(
            forged_plan, 'plan_sha256', plan.plan_sha256)
        # _assert_sealed catches the id/sha mismatch before any insert.
        with pytest.raises(CampaignExecutionIntegrityError):
            repo.save_plan(forged_plan)

    def test_stored_column_tamper_detected(self, tmp_path: Path) -> None:
        _scene, _sweep, repo = _repos(tmp_path)
        plan = _plan()
        repo.save_plan(plan)
        runner = repo.runner_for(
            plan, engine_factory=_engine_factory(),
            arm_confirmation_provider=_arm, clock=_clock())
        _drive(runner)
        record = repo.list_run_records(plan.plan_id)[0]
        with connect_sqlite(repo.path) as connection:
            connection.execute(
                'UPDATE cad_campaign_execution_runs SET outcome=? '
                'WHERE run_record_id=?',
                ('failed', record.run_record_id))
            connection.commit()
        with pytest.raises(CampaignExecutionIntegrityError):
            repo.get_run_record(record.run_record_id)

    def test_events_scoped_to_plan(self, tmp_path: Path) -> None:
        _scene, _sweep, repo = _repos(tmp_path)
        plan_a = _plan()
        plan_b = _plan(campaign_ref=_campaign_ref(ref_sha256='b' * 64))
        repo.save_plan(plan_a)
        repo.save_plan(plan_b)
        runner_a = repo.runner_for(
            plan_a, engine_factory=_engine_factory(),
            arm_confirmation_provider=_arm, clock=_clock())
        _drive(runner_a)
        events_b = repo.list_events(plan_b.plan_id)
        assert events_b == ()

    def test_audit_replay_probe_resolves(self, tmp_path: Path) -> None:
        from htdt.native_authority_audit import (
            audit_native_authority_graph,
        )

        _scene, _sweep, repo = _repos(tmp_path)
        plan = _plan()
        repo.save_plan(plan)
        runner = repo.runner_for(
            plan, engine_factory=_engine_factory(),
            arm_confirmation_provider=_arm, clock=_clock())
        _drive(runner)
        report = audit_native_authority_graph(repo.path)
        ours = [
            d for d in report.diagnostics
            if 'campaign_execution' in d.authority]
        assert ours == []
        modes = {
            table: mode for table, mode, _n in report.coverage
            if 'campaign_execution' in table}
        assert set(modes) == {
            'campaign_execution_plan',
            'campaign_execution_event',
            'campaign_execution_run'}
        assert all(mode == 'replay_canonical' for mode in modes.values())


# ---------------------------------------------------------------------------
# Derived-state honesty — nothing reads as success it did not earn
# ---------------------------------------------------------------------------


class TestHonesty:
    def test_failed_required_entry_fails_campaign(self) -> None:
        runner = _runner(
            _plan(), scenarios={'clip_at_dbfs': -14.0})
        assert _drive(runner).action == 'terminal'
        assert runner.state.outcome == 'failed'
        assert runner.state.progress.required_remaining == 1

    def test_completed_with_failures_requires_all_required_ok(self) -> None:
        # seat-b's screening entry is optional (required lives on the
        # position spec), so the campaign completes WITH a visible
        # failure instead of being held incomplete by it.
        optional = CampaignPositionSpec(
            position_id='seat-b', roles=('screening',), required=False,
            declared_position=Position3(x_m=2.0, y_m=2.0, z_m=1.0))
        plan = _plan(positions=[_position('seat-a'), optional])
        runner = _runner(
            plan,
            scenarios=[{}, {'device_loss_at_frame': 10}])
        assert _drive(runner).action == 'terminal'
        state = runner.state
        assert state.outcome == 'completed_with_failures'
        assert state.progress.failed == 1
        assert state.progress.completed == 1
        assert state.progress.remaining == 0

    def test_progress_counts_are_exact(self) -> None:
        plan = _plan(
            positions=[_position('a'), _position('b')],
            repetitions=2)
        runner = _runner(plan)
        runner.step()
        state = runner.state
        assert state.progress.total == 4
        assert state.progress.pending == 3
        assert state.progress.awaiting_position == 1
        assert state.progress.remaining == 4

    def test_unknown_positions_cannot_be_confirmed(self) -> None:
        runner = _runner(_plan())
        runner.step()
        with pytest.raises(PositionConfirmationError, match='unknown'):
            runner.confirm_position(PositionConfirmation(
                position_id='ghost', method='operator_attest',
                at_utc='2026-10-07T00:00:00+00:00'))
