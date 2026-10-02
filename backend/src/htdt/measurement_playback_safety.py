"""Measurement playback safety preflight (#734).

A bounded operational guard for any workflow that can trigger playback or
mutate live routing/volume relevant to a test: starting a REW sweep or test
signal, unmuting a device, changing measurement routing, or advancing
unattended/semi-automated playback steps.

This module is deliberately a *preflight authority*, not a hearing-safety
model. It never invents a universal safe SPL, exposure duration or
household limit, and never derives acoustic SPL from an AVR display value
whose mapping is unknown. All inputs are exact evidence (observed adapter
read-back, pinned routing/calibration identity, an optional stimulus
summary, and explicit user/project policy); UNKNOWN stays UNKNOWN.

Outcomes are typed:

- ``ready`` — every material condition was verified against policy.
- ``ready_with_confirmation`` — startable, but only after the operator
  confirms the resolved view (e.g. device currently muted, newly bound
  routing that earns a bounded low-level confirmation step).
- ``blocked`` — a configured limit or routing identity was violated, or
  the evidence was marked stale; the action must not start.
- ``unknown_manual_control_required`` — a material condition could not be
  verified; automated start is refused unless the operator explicitly
  continues under manual control.

Results carry ``inputs_sha256``, a fingerprint of every input: any change
to device state, routing, calibration, source or stimulus invalidates the
result (``matches``), which is how volume/routing/profile changes force a
re-run instead of reusing a stale READY.

Abort is honest: :class:`PlaybackAbortReport` records per-action outcomes
(``acknowledged`` vs ``attempted_unknown`` vs ``unsupported``) and never
claims a software stop is a guaranteed emergency stop — network/device
failure can prevent it.
"""

from __future__ import annotations

from math import isfinite
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _hash


PLAYBACK_SAFETY_SCHEMA_VERSION = 1






# ---------------------------------------------------------------------------
# Inputs


class PlaybackStimulusSummary(BaseModel):
    """Exact-known facts about the test signal to be produced.

    Every field optional: when the producer cannot say what the stimulus
    is, the missing field is UNKNOWN evidence, not an assumption. No SPL
    inference ever happens here — ``digital_level_dbfs`` is the signal's
    own level semantics (e.g. REW sweep level), nothing more.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal['sweep', 'pink_noise', 'tone', 'impulse', 'other']
    digital_level_dbfs: float | None = None
    band_hz: tuple[float, float] | None = None
    duration_s: float | None = None
    repetitions: int | None = None
    channel_count: int | None = Field(default=None, ge=1)
    #: Whether the outputs sum coherently at the radiators; None = unknown.
    coherent_channels: bool | None = None
    #: Which system produces the signal ('rew', 'htdt', 'external', ...).
    producer: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_stimulus(self) -> 'PlaybackStimulusSummary':
        if self.digital_level_dbfs is not None and not isfinite(
            float(self.digital_level_dbfs)
        ):
            raise ValueError('digital_level_dbfs must be finite')
        if self.band_hz is not None and not (
            isfinite(self.band_hz[0]) and isfinite(self.band_hz[1])
        ):
            raise ValueError('band_hz bounds must be finite')
        if self.duration_s is not None and self.duration_s <= 0:
            raise ValueError('duration_s must be positive')
        if self.repetitions is not None and self.repetitions < 1:
            raise ValueError('repetitions must be >= 1')
        return self


class ObservedOutputState(BaseModel):
    """Observed live-output state for the bound playback device.

    Values come from adapter read-back or explicit operator entry — never
    from copying intended settings (device ACK vs verified read-back stay
    distinct, matching ``cad_device_adapter`` semantics). ``None`` on a
    material field means UNKNOWN, which forces
    ``unknown_manual_control_required`` rather than a silent READY.
    """

    model_config = ConfigDict(frozen=True)

    observed_at_utc: str = Field(min_length=1)
    source: Literal['adapter_read_back', 'operator_entered']
    #: Device-native master level (e.g. AVR display units). Never SPL.
    master_volume: float | None = None
    #: What master_volume is measured in, e.g. 'avr_display_units'.
    master_volume_unit: str | None = None
    muted: bool | None = None
    #: Identity of the effective routing/management profile in force.
    routing_signature: str | None = None
    #: Free-form note, e.g. 'AVR rebooted 2 min ago'.
    observation_note: str | None = None

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json')


class PlaybackSafetyPolicy(BaseModel):
    """Explicit user/project policy for measurement playback.

    Only limits the operator or device provenance actually states apply —
    there is no implicit universal safe level.
    """

    model_config = ConfigDict(frozen=True)

    #: Maximum acceptable stimulus digital level (dBFS), or None = unset.
    max_digital_level_dbfs: float | None = None
    #: A routing mismatch between intended and observed always blocks.
    require_routing_match: bool = True
    #: Whether a bounded low-level source confirmation may be inserted for
    #: newly changed/bound routing when the stimulus supports it.
    allow_low_level_probe: bool = True
    #: Whether the operator may proceed under manual control when a
    #: material condition stays unverifiable.
    manual_control_permitted: bool = True


class PlaybackPreflightRequest(BaseModel):
    """One requested automated playback/output mutation.

    ``session_step_id`` binds the preflight to the exact measurement
    session/cell it guards; the result remains operational metadata and
    never becomes acoustic truth or MeasurementQuality evidence.
    """

    model_config = ConfigDict(frozen=True)

    request_id: str = Field(min_length=1)
    session_step_id: str | None = None
    #: The action HTDT is about to perform ('start_sweep', 'unmute',
    #: 'change_routing', 'set_measurement_level', 'advance_campaign').
    action: str = Field(min_length=1)
    intended_source_id: str | None = None
    intended_output_target: str | None = None
    #: Routing identity the action requires; None = not asserted.
    expected_routing_signature: str | None = None
    stimulus: PlaybackStimulusSummary | None = None
    #: Routing/binding changed since the operator last confirmed it.
    routing_newly_bound: bool = False
    #: Device+stimulus capability for a bounded low-level probe step.
    supports_low_level_probe: bool = False
    observed: ObservedOutputState | None = None
    #: Evidence categories reported changed since the observation
    #: ('master_volume', 'routing', 'calibration', 'source', 'stimulus',
    #: 'device_reconnect', 'playback_path'). Any entry marks the
    #: observation stale — the preflight must be re-run on fresh evidence.
    changed_inputs: tuple[str, ...] = ()
    policy: PlaybackSafetyPolicy = Field(default_factory=PlaybackSafetyPolicy)

    def inputs_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json')

    @property
    def inputs_sha256(self) -> str:
        return _hash(self.inputs_payload())


# ---------------------------------------------------------------------------
# Result


PreflightReasonCode = Literal[
    'unknown_volume',
    'configured_limit_exceeded',
    'routing_mismatch',
    'unknown_mute_state',
    'device_muted',
    'routing_unverified',
    'unknown_test_level',
    'stale_evidence',
    'missing_read_back',
    'manual_control_not_permitted',
]

PreflightOutcome = Literal[
    'ready',
    'ready_with_confirmation',
    'blocked',
    'unknown_manual_control_required',
]


class PlaybackPreflightReason(BaseModel):
    """One structured reason contributing to the outcome."""

    model_config = ConfigDict(frozen=True)

    code: PreflightReasonCode
    detail: str = Field(min_length=1)
    #: 'blocking' forbids automated start outright; 'confirm' makes the
    #: action startable only behind explicit operator confirmation or a
    #: manual-control decision.
    severity: Literal['blocking', 'confirm']


class PlaybackPreflightResult(BaseModel):
    """The preflight verdict for one request on one evidence set."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = PLAYBACK_SAFETY_SCHEMA_VERSION
    preflight_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    session_step_id: str | None
    evaluated_at_utc: str = Field(min_length=1)
    outcome: PreflightOutcome
    reasons: tuple[PlaybackPreflightReason, ...]
    inputs_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    #: The resolved view an operator confirms: source, target, observed
    #: volume/mute/routing, stimulus and level, plus the unknowns.
    resolved_view: dict[str, Any]
    #: Bounded low-level bring-up step when routing is newly bound and
    #: the capability exists — sequence the caller should offer first.
    low_level_confirmation_step: str | None = None
    #: Honest limits of this artifact (operational metadata, not acoustic
    #: truth; a software mute/stop is best-effort, never guaranteed).
    limitations: tuple[str, ...]

    def matches(self, request: PlaybackPreflightRequest) -> bool:
        """True only when every preflight input is unchanged."""

        return request.inputs_sha256 == self.inputs_sha256

    @property
    def requires_confirmation(self) -> bool:
        return self.outcome in (
            'ready_with_confirmation',
            'unknown_manual_control_required',
        )

    @property
    def automated_start_permitted(self) -> bool:
        """Whether the action may start without an operator decision."""

        return self.outcome == 'ready'


class PlaybackPreflightDecision(BaseModel):
    """A preflight result plus the operator's explicit decision on it."""

    model_config = ConfigDict(frozen=True)

    result: PlaybackPreflightResult
    decision: Literal[
        'confirmed',
        'manual_control_accepted',
        'cancelled',
    ]
    decided_at_utc: str = Field(min_length=1)
    operator_note: str | None = None

    @property
    def playback_permitted(self) -> bool:
        if self.decision == 'cancelled':
            return False
        if self.result.outcome == 'ready':
            return True
        if self.result.outcome == 'blocked':
            return False
        if (
            self.result.outcome == 'ready_with_confirmation'
            and self.decision == 'confirmed'
        ):
            return True
        return (
            self.result.outcome == 'unknown_manual_control_required'
            and self.decision == 'manual_control_accepted'
        )


# ---------------------------------------------------------------------------
# Evaluation


def _unknowns(request: PlaybackPreflightRequest) -> list[str]:
    observed = request.observed
    unknowns: list[str] = []
    if observed is None:
        unknowns.extend(
            ['master_volume', 'mute_state', 'routing_state']
        )
    else:
        if observed.master_volume is None:
            unknowns.append('master_volume')
        if observed.muted is None:
            unknowns.append('mute_state')
        if observed.routing_signature is None:
            unknowns.append('routing_state')
    if request.stimulus is None:
        unknowns.append('test_signal')
    elif request.stimulus.digital_level_dbfs is None:
        unknowns.append('test_level')
    return unknowns


def _resolved_view(
    request: PlaybackPreflightRequest, unknowns: list[str]
) -> dict[str, Any]:
    observed = request.observed
    stimulus = request.stimulus
    return {
        'action': request.action,
        'session_step_id': request.session_step_id,
        'intended_source_id': request.intended_source_id,
        'intended_output_target': request.intended_output_target,
        'observed_master_volume': (
            observed.master_volume if observed else None
        ),
        'observed_master_volume_unit': (
            observed.master_volume_unit if observed else None
        ),
        'observed_muted': observed.muted if observed else None,
        'observed_routing_signature': (
            observed.routing_signature if observed else None
        ),
        'expected_routing_signature': request.expected_routing_signature,
        'stimulus': (
            stimulus.model_dump(mode='json') if stimulus else None
        ),
        'unknowns': list(unknowns),
    }


def run_playback_preflight(
    request: PlaybackPreflightRequest,
    *,
    evaluated_at_utc: str,
    preflight_id: str | None = None,
) -> PlaybackPreflightResult:
    """Evaluate one requested playback/output mutation against evidence.

    Pure: never touches a device, never assumes a level it was not given.
    """

    policy = request.policy
    observed = request.observed
    stimulus = request.stimulus
    reasons: list[PlaybackPreflightReason] = []

    if request.changed_inputs:
        reasons.append(
            PlaybackPreflightReason(
                code='stale_evidence',
                severity='blocking',
                detail=(
                    'evidence changed since observation: '
                    + ', '.join(request.changed_inputs)
                    + ' — re-observe before starting'
                ),
            )
        )

    if observed is None:
        severity = (
            'confirm' if policy.manual_control_permitted else 'blocking'
        )
        reasons.append(
            PlaybackPreflightReason(
                code='missing_read_back',
                severity=severity,
                detail=(
                    'no adapter read-back of live output state; '
                    'volume, mute and routing are unknown'
                ),
            )
        )
    else:
        if observed.muted is True:
            reasons.append(
                PlaybackPreflightReason(
                    code='device_muted',
                    severity='confirm',
                    detail=(
                        'observed device is muted; the action will '
                        'unmute or start with output muted'
                    ),
                )
            )
        elif observed.muted is None:
            reasons.append(
                PlaybackPreflightReason(
                    code='unknown_mute_state',
                    severity='confirm',
                    detail='observed device mute state is unknown',
                )
            )
        if observed.master_volume is None:
            reasons.append(
                PlaybackPreflightReason(
                    code='unknown_volume',
                    severity='confirm',
                    detail=(
                        'observed master volume is unknown; output '
                        'level cannot be confirmed not excessive'
                    ),
                )
            )
        if (
            request.expected_routing_signature is not None
            and observed.routing_signature is not None
            and observed.routing_signature
            != request.expected_routing_signature
        ):
            reasons.append(
                PlaybackPreflightReason(
                    code='routing_mismatch',
                    severity=(
                        'blocking'
                        if policy.require_routing_match
                        else 'confirm'
                    ),
                    detail=(
                        f'observed routing {observed.routing_signature} '
                        'does not match required routing '
                        f'{request.expected_routing_signature}'
                    ),
                )
            )
        elif (
            request.expected_routing_signature is not None
            and observed.routing_signature is None
        ) or request.routing_newly_bound:
            reasons.append(
                PlaybackPreflightReason(
                    code='routing_unverified',
                    severity='confirm',
                    detail=(
                        'required routing could not be verified against '
                        'the observed device state'
                    ),
                )
            )

    if stimulus is None:
        reasons.append(
            PlaybackPreflightReason(
                code='unknown_test_level',
                severity='confirm',
                detail=(
                    'no stimulus summary is available; the test signal '
                    'kind and level are unknown'
                ),
            )
        )
    elif stimulus.digital_level_dbfs is None:
        reasons.append(
            PlaybackPreflightReason(
                code='unknown_test_level',
                severity='confirm',
                detail=(
                    'the stimulus level is unknown; the test signal '
                    'level cannot be confirmed not excessive'
                ),
            )
        )
    elif (
        policy.max_digital_level_dbfs is not None
        and stimulus.digital_level_dbfs > policy.max_digital_level_dbfs
    ):
        reasons.append(
            PlaybackPreflightReason(
                code='configured_limit_exceeded',
                severity='blocking',
                detail=(
                    f'stimulus level {stimulus.digital_level_dbfs:g} dBFS '
                    'exceeds the configured limit '
                    f'{policy.max_digital_level_dbfs:g} dBFS'
                ),
            )
        )

    unknowns = _unknowns(request)
    blocking = any(r.severity == 'blocking' for r in reasons)
    confirmable = any(r.severity == 'confirm' for r in reasons)

    if not policy.manual_control_permitted and confirmable:
        reasons.append(
            PlaybackPreflightReason(
                code='manual_control_not_permitted',
                severity='blocking',
                detail=(
                    'policy does not permit continuing under manual '
                    'control when output state is unverifiable'
                ),
            )
        )
        blocking = True

    if blocking:
        outcome: PreflightOutcome = 'blocked'
    elif unknowns and not observed_is_verified(observed):
        outcome = 'unknown_manual_control_required'
    elif confirmable or (
        request.routing_newly_bound
        and policy.allow_low_level_probe
        and request.supports_low_level_probe
    ):
        outcome = 'ready_with_confirmation'
    else:
        outcome = 'ready'

    low_level_step: str | None = None
    if (
        request.routing_newly_bound
        and policy.allow_low_level_probe
        and request.supports_low_level_probe
        and outcome != 'blocked'
    ):
        low_level_step = (
            'verify route/mute -> low-level source confirmation -> '
            'requested measurement level -> sweep'
        )

    return PlaybackPreflightResult(
        preflight_id=preflight_id or uuid4().hex,
        request_id=request.request_id,
        session_step_id=request.session_step_id,
        evaluated_at_utc=evaluated_at_utc,
        outcome=outcome,
        reasons=tuple(reasons),
        inputs_sha256=request.inputs_sha256,
        resolved_view=_resolved_view(request, unknowns),
        low_level_confirmation_step=low_level_step,
        limitations=(
            'preflight is operational metadata; it does not prove '
            'achieved SPL and does not replace MeasurementQuality or '
            'commissioning evidence',
            'a software mute or stop request is best-effort and is not '
            'a guaranteed emergency stop',
        ),
    )


def observed_is_verified(observed: ObservedOutputState | None) -> bool:
    """Whether read-back verified every material live-output field."""

    return (
        observed is not None
        and observed.master_volume is not None
        and observed.muted is not None
        and observed.routing_signature is not None
    )


def decide_preflight(
    result: PlaybackPreflightResult,
    *,
    decision: Literal[
        'confirmed', 'manual_control_accepted', 'cancelled'
    ],
    decided_at_utc: str,
    operator_note: str | None = None,
) -> PlaybackPreflightDecision:
    """Record the operator's decision on a preflight result."""

    return PlaybackPreflightDecision(
        result=result,
        decision=decision,
        decided_at_utc=decided_at_utc,
        operator_note=operator_note,
    )


# ---------------------------------------------------------------------------
# Abort


AbortActionOutcome = Literal[
    'acknowledged',
    'attempted_unknown',
    'unsupported',
    'not_attempted',
]


class PlaybackAbortAction(BaseModel):
    """One stop action attempted when aborting playback."""

    model_config = ConfigDict(frozen=True)

    action: Literal[
        'stop_sweep',
        'request_mute',
        'halt_campaign_advancement',
    ]
    outcome: AbortActionOutcome
    detail: str | None = None


class PlaybackAbortReport(BaseModel):
    """What an abort actually achieved — acknowledged vs unknown (#734).

    ``workflow_halted`` is always True: cancelling campaign advancement is
    under HTDT's own control. Device-side stop actions are reported
    per-action and are NEVER summarized as a guaranteed physical stop.
    """

    model_config = ConfigDict(frozen=True)

    abort_id: str = Field(min_length=1)
    preflight_id: str | None = None
    aborted_at_utc: str = Field(min_length=1)
    workflow_halted: Literal[True] = True
    actions: tuple[PlaybackAbortAction, ...]
    software_stop_is_guaranteed: Literal[False] = False
    note: str = (
        'software stop/mute requests are best-effort; network or device '
        'failure can prevent them — verify the physical output state'
    )

    @property
    def acknowledged_actions(self) -> tuple[str, ...]:
        return tuple(
            item.action for item in self.actions
            if item.outcome == 'acknowledged'
        )

    @property
    def unresolved_actions(self) -> tuple[str, ...]:
        return tuple(
            item.action for item in self.actions
            if item.outcome in ('attempted_unknown', 'not_attempted')
        )


def build_abort_report(
    *,
    aborted_at_utc: str,
    preflight_id: str | None = None,
    stop_sweep_outcome: AbortActionOutcome = 'not_attempted',
    mute_outcome: AbortActionOutcome = 'not_attempted',
    detail: str | None = None,
) -> PlaybackAbortReport:
    """Assemble the honest abort record for one abort sequence.

    Campaign advancement always halts (HTDT's own state); device actions
    carry the outcome the adapter actually reported — 'acknowledged' only
    on a real ack, 'attempted_unknown' when the request was sent but no
    trustworthy read-back exists, 'unsupported' when the bound adapter
    cannot perform it.
    """

    return PlaybackAbortReport(
        abort_id=uuid4().hex,
        preflight_id=preflight_id,
        aborted_at_utc=aborted_at_utc,
        actions=(
            PlaybackAbortAction(
                action='stop_sweep',
                outcome=stop_sweep_outcome,
                detail=detail,
            ),
            PlaybackAbortAction(
                action='request_mute',
                outcome=mute_outcome,
            ),
            PlaybackAbortAction(
                action='halt_campaign_advancement',
                outcome='acknowledged',
            ),
        ),
    )


__all__ = [
    'AbortActionOutcome',
    'ObservedOutputState',
    'PLAYBACK_SAFETY_SCHEMA_VERSION',
    'PlaybackAbortAction',
    'PlaybackAbortReport',
    'PlaybackPreflightDecision',
    'PlaybackPreflightReason',
    'PlaybackPreflightRequest',
    'PlaybackPreflightResult',
    'PlaybackSafetyPolicy',
    'PlaybackStimulusSummary',
    'PreflightOutcome',
    'PreflightReasonCode',
    'build_abort_report',
    'decide_preflight',
    'observed_is_verified',
    'run_playback_preflight',
]
