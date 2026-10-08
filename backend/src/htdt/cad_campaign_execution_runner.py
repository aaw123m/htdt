"""The #875 campaign runner — drives the #869 acquisition engine through
the sealed run queue (REV67).

:class:`MeasurementCampaignRunner` executes a sealed
:class:`CampaignExecutionPlan`:

- sequential cursor: the first non-terminal queue entry owns the
  machine — entries behind it never leapfrog, so POSITION → CHANNEL →
  REPETITION order is preserved exactly;
- position gates: entries flagged ``requires_position_confirmation``
  pause the campaign with a ``position_gate_opened`` event until a
  :class:`PositionConfirmation` passes the ladder check — measured
  methods produce measured evidence, attested methods never pretend to;
- execution: builds the #869 ``AcquisitionRequest`` for the entry's
  bound channel, drives configure → arm → start, persists the #869
  evidence via the caller's ``acquisition_sink``, then seals a
  ``CadCampaignRunRecord`` and a ``run_recorded`` event for EVERY
  attempt — failed captures stay visible and are never overwritten;
- quality gate + policy: after each attempt a deterministic
  ``evaluate_retry_decision`` picks retry / reduced-level retry /
  fail-entry / stop-campaign, and each outcome is journaled;
- control: ``pause``/``resume``/``cancel``/``report_routing_change``/
  ``waive_entry`` — human-facing actions land in the same journal;
- restart recovery: construct the runner with the persisted journal +
  run records; entries left ``in_progress`` by a crash are marked
  interrupted, flipped back to ``pending`` under a
  ``restart_recovered`` event, and execution resumes at exactly the
  remaining work.

The runner never fabricates evidence: pre-engine failures (precheck,
arm) seal run records with no acquisition ref; persistence failures
propagate instead of writing phantom state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal, Sequence

from .cad_authority_resolver import AuthorityRef
from .cad_campaign_execution import (
    CAMPAIGN_TERMINAL_OUTCOMES,
    ENTRY_TERMINAL_STATES,
    CampaignActor,
    CampaignExecutionPlan,
    CampaignExecutionState,
    CampaignQueueEntry,
    CampaignRunFailureKind,
    CampaignRunOutcome,
    CampaignStateError,
    PositionConfirmation,
    PositionConfirmationError,
    derive_campaign_state,
    evaluate_position_confirmation,
    evaluate_retry_decision,
)
from .cad_campaign_execution_evidence import (
    CadCampaignExecutionEvent,
    CadCampaignRunRecord,
    build_campaign_event,
    build_campaign_run_record,
)
from .cad_sweep_acquisition import (
    AcquisitionRequest,
    AcquisitionResult,
    ArmBlockedError,
    ArmConfirmation,
    CalibrationBindingState,
    MeasurementAcquisitionEngine,
)
from .clock import utc_now_iso as _utc_now


RunnerStepAction = Literal[
    'ran',
    'awaiting_position',
    'paused',
    'blocked',
    'cancelled',
    'terminal',
]


@dataclass(frozen=True)
class RunnerStepResult:
    """What one ``step()`` did — drives operator-facing UI text."""

    action: RunnerStepAction
    entry_key: str | None = None
    detail: str = ''


#: Persists the journal event (repository ``save_event``).
EventSink = Callable[[CadCampaignExecutionEvent], None]
#: Persists one sealed attempt record (repository ``save_run_record``).
RunRecordSink = Callable[[CadCampaignRunRecord], None]
#: Persists the #869 evidence for a finished engine run and returns
#: ``(stimulus_ref, acquisition_ref)`` — either may be None when the run
#: did not produce the corresponding artifact (e.g. cancelled before
#: stimulus generation).
AcquisitionSink = Callable[
    [MeasurementAcquisitionEngine, AcquisitionResult, CampaignQueueEntry],
    tuple[AuthorityRef | None, AuthorityRef | None],
]
#: Produces the operator arm confirmation for an entry. Returning None
#: aborts the attempt honestly as ``arm_confirmation_unavailable``.
ArmConfirmationProvider = Callable[
    [CampaignQueueEntry, AcquisitionRequest], ArmConfirmation | None]
#: Reports the current calibration binding for the entry (#813) —
#: ``'unknown'`` is the honest default; never claim binding.
CalibrationStateProvider = Callable[
    [CampaignQueueEntry], CalibrationBindingState]
#: Fresh #869 engine per attempt (engine lifecycle is per-run).
EngineFactory = Callable[[], MeasurementAcquisitionEngine]


class MeasurementCampaignRunner:
    """Executes a sealed campaign plan against a #869 engine factory.

    Deterministic and journal-first: every decision is an event, every
    attempt a sealed record, and the whole state can be recomputed from
    the persisted journal via :func:`derive_campaign_state`.
    """

    def __init__(
        self,
        *,
        plan: CampaignExecutionPlan,
        engine_factory: EngineFactory,
        event_sink: EventSink,
        run_record_sink: RunRecordSink,
        acquisition_sink: AcquisitionSink | None = None,
        arm_confirmation_provider: ArmConfirmationProvider | None = None,
        calibration_state_provider: CalibrationStateProvider | None = None,
        position_ref_for: Callable[[str], AuthorityRef | None]
        | None = None,
        clock: Callable[[], str] = _utc_now,
        events: Sequence[CadCampaignExecutionEvent] = (),
        run_records: Sequence[CadCampaignRunRecord] = (),
    ) -> None:
        self._plan = plan
        self._engine_factory = engine_factory
        self._event_sink = event_sink
        self._run_record_sink = run_record_sink
        self._acquisition_sink = acquisition_sink
        self._arm_provider = arm_confirmation_provider
        self._calibration_provider = calibration_state_provider
        self._position_ref_for = position_ref_for
        self._clock = clock
        self._events = list(events)
        self._run_records = list(run_records)
        self._validate_recovered_journal()
        if not self._events:
            self._emit(
                'plan_registered', actor='system',
                reason=f'{plan.entry_count} queued run(s) — '
                       'campaign execution begins')
        interrupted = [
            e.entry_key for e in plan.queue
            if self.state.entries[e.entry_key].state == 'in_progress']
        if interrupted:
            # Crash mid-attempt: the attempt never sealed a run record.
            # Mark the entries interrupted and requeue them — the work
            # is redone, never assumed done.
            self._emit(
                'restart_recovered', actor='system',
                reason='application restart — in-flight runs requeued',
                interrupted_entry_keys=interrupted)

    # -- state --------------------------------------------------------------

    @property
    def state(self) -> CampaignExecutionState:
        return derive_campaign_state(
            self._plan, self._events, self._run_records)

    @property
    def events(self) -> tuple[CadCampaignExecutionEvent, ...]:
        return tuple(self._events)

    @property
    def run_records(self) -> tuple[CadCampaignRunRecord, ...]:
        return tuple(self._run_records)

    @property
    def plan(self) -> CampaignExecutionPlan:
        return self._plan

    def _validate_recovered_journal(self) -> None:
        """Fail closed on a journal that does not belong to this plan."""

        for event in self._events:
            if (event.plan_id != self._plan.plan_id
                    or event.plan_sha256 != self._plan.plan_sha256):
                raise CampaignStateError(
                    'journal event belongs to a different plan')
        for record in self._run_records:
            if (record.plan_id != self._plan.plan_id
                    or record.plan_sha256 != self._plan.plan_sha256):
                raise CampaignStateError(
                    'run record belongs to a different plan')
        seqs = [e.seq for e in self._events]
        if seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
            raise CampaignStateError('journal seqs must be strictly ordered')

    # -- journal ------------------------------------------------------------

    def _emit(self, kind: str, **fields: Any) -> CadCampaignExecutionEvent:
        event = build_campaign_event(
            document_id=self._plan.document_id,
            plan=self._plan,
            seq=len(self._events) + 1,
            kind=kind,  # type: ignore[arg-type]
            at_utc=self._clock(),
            **fields)
        # Sink first: a persistence failure must not leave an in-memory
        # event that never reached durable storage.
        self._event_sink(event)
        self._events.append(event)
        return event

    # -- cursor -------------------------------------------------------------

    def _cursor(self) -> CampaignQueueEntry | None:
        """First non-terminal queue entry — the sequential run cursor."""

        entries = self.state.entries
        for entry in self._plan.queue:
            if entries[entry.entry_key].state not in ENTRY_TERMINAL_STATES:
                return entry
        return None

    # -- top-level drive ----------------------------------------------------

    def step(self) -> RunnerStepResult:
        """Advance exactly one unit of work."""

        state = self.state
        if state.outcome in CAMPAIGN_TERMINAL_OUTCOMES:
            # The fold may compute the terminal outcome from entry states
            # before a terminal event exists — finalize (idempotent) so
            # the journal always carries the terminal marker.
            self._finalize()
            return RunnerStepResult('terminal', detail=self.state.outcome)
        if state.paused:
            return RunnerStepResult('paused', detail='campaign paused')
        if state.outcome == 'blocked':
            return RunnerStepResult(
                'blocked',
                detail=state.blocked_reason or 'blocked')
        cursor = self._cursor()
        if cursor is None:
            self._finalize()
            return RunnerStepResult('terminal', detail=self.state.outcome)
        entry_state = state.entries[cursor.entry_key]
        if entry_state.state == 'awaiting_position':
            return RunnerStepResult(
                'awaiting_position', entry_key=cursor.entry_key,
                detail=f'move the microphone to {cursor.position_id} '
                       'and confirm the position')
        if entry_state.state != 'pending':
            # in_progress outside recovery is unreachable in synchronous
            # execution; fail closed rather than double-run.
            return RunnerStepResult(
                'blocked', entry_key=cursor.entry_key,
                detail=f'entry in unexpected state '
                       f'{entry_state.state}')
        if cursor.requires_position_confirmation:
            gate = state.position_gates.get(cursor.position_id)
            if gate is None or not gate.confirmed:
                self._emit(
                    'position_gate_opened',
                    entry_key=cursor.entry_key,
                    position_id=cursor.position_id,
                    reason='physical microphone move required — '
                           'confirm the position to resume')
                return RunnerStepResult(
                    'awaiting_position', entry_key=cursor.entry_key,
                    detail=f'move the microphone to '
                           f'{cursor.position_id} and confirm')
        self._execute_entry(cursor)
        return RunnerStepResult(
            'ran', entry_key=cursor.entry_key, detail=cursor.position_id)

    def run_until_blocked(self) -> RunnerStepResult:
        """Run entries until the campaign needs the operator or ends."""

        while True:
            result = self.step()
            if result.action != 'ran':
                return result

    _TERMINAL_EVENT_KINDS = frozenset(
        {'campaign_completed', 'campaign_failed', 'cancelled'})

    def _finalize(self) -> None:
        """Emit the terminal event once all entries are terminal.

        Idempotent: a terminal journal event (completed/failed/cancelled)
        means the campaign already ended — a policy stop gets its own
        campaign_failed marker on top of the policy_stop event.
        """

        if any(e.kind in self._TERMINAL_EVENT_KINDS
               for e in self._events):
            return
        outcome = self.state.outcome
        progress = self.state.progress
        if outcome == 'completed':
            self._emit(
                'campaign_completed',
                reason='all required runs reached an allowed terminal '
                       f'state ({progress.completed}/{progress.total} '
                       'runs completed)')
        elif outcome == 'completed_with_failures':
            self._emit(
                'campaign_completed',
                reason=f'completed with {progress.failed} failed '
                       'non-required run(s); all required runs completed')
        else:
            self._emit(
                'campaign_failed',
                reason=f'{progress.required_remaining} required run(s) '
                       'never reached an allowed terminal state')

    # -- operator actions ---------------------------------------------------

    def confirm_position(self, confirmation: PositionConfirmation
                         ) -> CadCampaignExecutionEvent:
        """Confirm the microphone is at a position — resumes the gate.

        The ladder check enforces the plan's tolerance when coordinates
        exist on both sides; an attestation confirms presence only and
        never produces measured evidence.
        """

        spec = self._plan.position(confirmation.position_id)
        if spec is None:
            raise PositionConfirmationError(
                f'unknown position {confirmation.position_id}')
        problems = evaluate_position_confirmation(
            confirmation, spec, self._plan.policy.position_tolerance_m)
        if problems:
            raise PositionConfirmationError('; '.join(problems))
        state = self.state
        if state.outcome in CAMPAIGN_TERMINAL_OUTCOMES:
            raise CampaignStateError('campaign already terminal')
        return self._emit(
            'position_confirmed',
            actor=confirmation.actor,
            position_id=confirmation.position_id,
            confirmation_method=confirmation.method,
            confirmation_evidence_quality=confirmation.evidence_quality,
            reported_position=confirmation.reported_position,
            reason=confirmation.note or 'position confirmed',
        )

    def pause(self, reason: str = '') -> CadCampaignExecutionEvent:
        state = self.state
        if state.outcome in CAMPAIGN_TERMINAL_OUTCOMES:
            raise CampaignStateError('campaign already terminal')
        if state.paused:
            raise CampaignStateError('campaign already paused')
        return self._emit('paused', actor='operator',
                          reason=reason or 'operator pause')

    def resume(self, actor: CampaignActor = 'operator',
               reason: str = '') -> CadCampaignExecutionEvent:
        """Resume after pause or after a blocked (human-required) state."""

        state = self.state
        if state.outcome in CAMPAIGN_TERMINAL_OUTCOMES:
            raise CampaignStateError('campaign already terminal')
        if not (state.paused or state.outcome == 'blocked'
                or state.blocked_reason):
            raise CampaignStateError('nothing to resume from')
        return self._emit('resumed', actor=actor,
                          reason=reason or 'operator resumed')

    def cancel(self, reason: str = '') -> CadCampaignExecutionEvent:
        state = self.state
        if state.outcome in CAMPAIGN_TERMINAL_OUTCOMES:
            raise CampaignStateError('campaign already terminal')
        return self._emit('cancelled', actor='operator',
                          reason=reason or 'operator cancelled')

    def report_routing_change(
        self, reason: str, actor: CampaignActor = 'operator',
    ) -> CadCampaignExecutionEvent:
        """An unexpected device/routing change mid-campaign.

        Sticky: the drift flag stays set (holdout retries stay denied),
        and per policy the campaign blocks until an operator explicitly
        resumes.
        """

        state = self.state
        if state.outcome in CAMPAIGN_TERMINAL_OUTCOMES:
            raise CampaignStateError('campaign already terminal')
        return self._emit('routing_change_reported', actor=actor,
                          reason=reason)

    def waive_entry(
        self, entry_key: str, *, reason: str,
        actor: CampaignActor = 'operator',
    ) -> CadCampaignExecutionEvent:
        """Waive a pending entry — required entries only when the policy
        explicitly allows it; never silent, always journaled."""

        entry = self._plan.entry(entry_key)
        if entry is None:
            raise CampaignStateError(f'unknown entry {entry_key}')
        current = self.state.entries[entry_key].state
        if current in ENTRY_TERMINAL_STATES:
            raise CampaignStateError(
                f'entry {entry_key} already terminal ({current})')
        if entry.required and not self._plan.policy.allow_waived_required:
            raise CampaignStateError(
                'required entries cannot be waived under this policy')
        return self._emit(
            'entry_waived', entry_key=entry_key, actor=actor,
            reason=reason or 'waived by operator')

    # -- attempt execution ----------------------------------------------------

    def _build_request(
        self, entry: CampaignQueueEntry, level_dbfs: float,
    ) -> AcquisitionRequest:
        binding = self._plan.channel(entry.channel_entity_id)
        assert binding is not None  # plan validator guarantees this
        calibration: CalibrationBindingState = 'unknown'
        if self._calibration_provider is not None:
            calibration = self._calibration_provider(entry)
        stimulus = self._plan.stimulus_template
        if level_dbfs != stimulus.level_dbfs:
            stimulus = self._plan.stimulus_for_level(level_dbfs)
        return AcquisitionRequest(
            stimulus=stimulus,
            routing=binding.routing(),
            level_policy=self._plan.level_policy,
            requires_absolute_level=self._plan.requires_absolute_level,
            calibration_state=calibration,
            declared_synchronized=self._plan.declared_synchronized,
            quality_thresholds=self._plan.quality_thresholds,
        )

    def _attempts_for(self, entry_key: str) -> int:
        return sum(
            1 for r in self._run_records if r.entry_key == entry_key)

    def _level_for(self, entry: CampaignQueueEntry) -> float:
        """Output level for the next attempt — read from the journal so
        a scheduled reduced-level retry survives a restart."""

        for event in reversed(self._events):
            if (event.kind == 'retry_scheduled'
                    and event.entry_key == entry.entry_key
                    and event.reduced_level_dbfs is not None):
                return event.reduced_level_dbfs
        return self._plan.stimulus_template.level_dbfs

    def _execute_entry(self, entry: CampaignQueueEntry) -> None:
        attempt = self._attempts_for(entry.entry_key) + 1
        level = self._level_for(entry)
        started = self._clock()
        engine = self._engine_factory()
        request = self._build_request(entry, level)
        self._emit(
            'run_started', entry_key=entry.entry_key, attempt=attempt,
            position_id=entry.position_id,
            reason=f'attempt {attempt} at {level:g} dBFS')

        failure_kind: CampaignRunFailureKind | None = None
        outcome: CampaignRunOutcome = 'failed'
        quality_verdict = None
        quality_reasons: tuple[str, ...] = ()
        stimulus_ref: AuthorityRef | None = None
        acquisition_ref: AuthorityRef | None = None

        precheck = engine.configure(request)
        if not precheck.ok:
            failure_kind = 'precheck_blocked'
            quality_reasons = precheck.blocked_reasons or ('precheck',)
        else:
            confirmation = self._confirm_for(entry, request)
            if confirmation is None:
                failure_kind = 'arm_confirmation_unavailable'
            else:
                try:
                    engine.arm(confirmation)
                except ArmBlockedError as exc:
                    failure_kind = 'arm_blocked'
                    quality_reasons = tuple(exc.reasons)
                else:
                    result = engine.start()
                    if self._acquisition_sink is not None:
                        stimulus_ref, acquisition_ref = (
                            self._acquisition_sink(
                                engine, result, entry))
                    outcome, failure_kind, quality_verdict, \
                        quality_reasons = self._classify(
                            engine, result)

        self._record_attempt(
            entry=entry, attempt=attempt, request=request,
            engine=engine, outcome=outcome, failure_kind=failure_kind,
            quality_verdict=quality_verdict,
            quality_reasons=quality_reasons,
            stimulus_ref=stimulus_ref, acquisition_ref=acquisition_ref,
            started=started)

    def _confirm_for(
        self, entry: CampaignQueueEntry, request: AcquisitionRequest,
    ) -> ArmConfirmation | None:
        if self._arm_provider is not None:
            return self._arm_provider(entry, request)
        if not self._plan.policy.auto_arm:
            return None
        routing = request.routing
        return ArmConfirmation(
            acknowledged_playback_device_id=routing.playback_device_id,
            acknowledged_playback_channel=routing.playback_channel,
            acknowledged_capture_device_id=routing.capture_device_id,
            acknowledged_capture_channel=routing.capture_channel,
            acknowledged_level_dbfs=request.stimulus.level_dbfs,
        )

    def _classify(
        self, engine: MeasurementAcquisitionEngine,
        result: AcquisitionResult,
    ) -> tuple[CampaignRunOutcome, CampaignRunFailureKind | None,
               Any, tuple[str, ...]]:
        stage = engine.stage
        verdict = result.quality.verdict if result.quality else None
        reasons = (tuple(result.quality.reasons)
                   if result.quality else ())
        if stage == 'cancelled':
            return 'cancelled', 'cancelled', verdict, reasons
        if stage == 'completed' and verdict in ('valid', 'limited'):
            return 'completed', None, verdict, reasons
        if stage == 'completed':
            return 'failed', 'quality_invalid', verdict, reasons
        return 'failed', 'capture_failed', verdict, reasons or ('failed',)

    def _record_attempt(
        self, *,
        entry: CampaignQueueEntry,
        attempt: int,
        request: AcquisitionRequest,
        engine: MeasurementAcquisitionEngine,
        outcome: CampaignRunOutcome,
        failure_kind: CampaignRunFailureKind | None,
        quality_verdict: Any,
        quality_reasons: tuple[str, ...],
        stimulus_ref: AuthorityRef | None,
        acquisition_ref: AuthorityRef | None,
        started: str,
    ) -> None:
        position_ref = (
            self._position_ref_for(entry.position_id)
            if self._position_ref_for is not None else None)
        record = build_campaign_run_record(
            document_id=self._plan.document_id,
            plan=self._plan,
            entry=entry,
            attempt=attempt,
            routing=request.routing,
            level_dbfs=request.stimulus.level_dbfs,
            outcome=outcome,
            failure_kind=failure_kind,
            quality_verdict=quality_verdict,
            quality_reasons=quality_reasons,
            started_at_utc=started,
            completed_at_utc=self._clock(),
            stimulus_ref=stimulus_ref,
            acquisition_ref=acquisition_ref,
            position_ref=position_ref,
            engine=engine,
        )
        # Persist first, then journal — a record that cannot be stored
        # propagates; the journal never claims a run it cannot cite.
        self._run_record_sink(record)
        self._run_records.append(record)
        record_ref = AuthorityRef(
            kind='campaign_run_record',
            ref_id=record.run_record_id,
            ref_sha256=record.run_record_sha256,
        )
        refs = [record_ref]
        if stimulus_ref is not None:
            refs.append(stimulus_ref)
        if acquisition_ref is not None:
            refs.append(acquisition_ref)
        self._emit(
            'run_recorded', entry_key=entry.entry_key, attempt=attempt,
            position_id=entry.position_id,
            outcome=outcome, quality_verdict=quality_verdict,
            quality_reasons=quality_reasons,
            reason=f'attempt {attempt} recorded: {outcome}'
                   + (f' ({failure_kind})' if failure_kind else ''),
            evidence_refs=refs,
        )
        self._decide(entry, attempt, outcome, quality_verdict,
                     quality_reasons, request.stimulus.level_dbfs)

    def _decide(
        self,
        entry: CampaignQueueEntry,
        attempt: int,
        outcome: CampaignRunOutcome,
        quality_verdict: Any,
        quality_reasons: tuple[str, ...],
        level_dbfs: float,
    ) -> None:
        if outcome == 'completed':
            self._emit(
                'entry_completed', entry_key=entry.entry_key,
                position_id=entry.position_id,
                attempt=attempt,
                quality_verdict=quality_verdict,
                quality_reasons=quality_reasons,
                reason=f'quality verdict {quality_verdict}')
            return
        state = self.state
        attempts_used = self._attempts_for(entry.entry_key)
        eval_result = evaluate_retry_decision(
            policy=self._plan.policy,
            entry=entry,
            attempt_level_dbfs=level_dbfs,
            attempts_used=attempts_used,
            quality_reasons=quality_reasons,
            routing_drift=state.routing_drift,
            consecutive_quality_failures=(
                state.consecutive_quality_failures),
            clipping_retry_used=state.entries[
                entry.entry_key].clipping_retry_used,
        )
        if eval_result.decision in ('retry', 'retry_reduced_level'):
            self._emit(
                'retry_scheduled', entry_key=entry.entry_key,
                position_id=entry.position_id,
                attempt=attempts_used + 1,
                retry_decision=eval_result.decision,
                reduced_level_dbfs=eval_result.reduced_level_dbfs,
                quality_reasons=quality_reasons,
                reason=eval_result.reason)
        elif eval_result.decision == 'stop_campaign':
            # The triggering entry is terminally failed AND the campaign
            # stops — two events, because both are true.
            self._emit(
                'entry_failed', entry_key=entry.entry_key,
                position_id=entry.position_id,
                attempt=attempts_used,
                quality_verdict=quality_verdict,
                quality_reasons=quality_reasons,
                reason=eval_result.reason)
            self._emit(
                'policy_stop', entry_key=entry.entry_key,
                position_id=entry.position_id,
                quality_reasons=quality_reasons,
                reason=eval_result.reason)
        else:
            self._emit(
                'entry_failed', entry_key=entry.entry_key,
                position_id=entry.position_id,
                attempt=attempts_used,
                quality_verdict=quality_verdict,
                quality_reasons=quality_reasons,
                reason=eval_result.reason)


__all__ = [
    'AcquisitionSink',
    'ArmConfirmationProvider',
    'CalibrationStateProvider',
    'EngineFactory',
    'EventSink',
    'MeasurementCampaignRunner',
    'RunRecordSink',
    'RunnerStepAction',
    'RunnerStepResult',
]
