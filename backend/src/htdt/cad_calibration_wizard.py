"""Guided automatic measurement-chain calibration wizard (#877).

A resumable, evidence-sealed state machine that turns each required
*physical* action into an explicit operator prompt while automating all
software-controlled work. Three lanes:

- ``interface_loopback`` (Lane A): bind an exact I/O path, prompt the
  operator to wire the loopback cable, preflight through the #869 sweep
  engine, run the native sweep, derive the transfer, pass the quality
  gate, seal the combined DAC+ADC interface calibration (#699) and bind
  it to the exact path.
- ``spl_reference_check`` (Lane B): bind an instrument instance, prompt
  the calibrator/reference setup, capture a record-only window, evaluate
  level/frequency/stability against a sealed acceptance profile, seal a
  #611 verification check and update the instrument's fitness state.
- ``campaign_checks`` (Lane C): bind a campaign check plan, record the
  required pre-use field checks, wait through the campaign window, then
  record the post-use checks and evaluate the gate that blocks dependent
  evidence when policy requires.

Honesty rules:

- Wizard state lives in sealed append-only transitions; restarting the
  app folds the same log to the same state — a stage whose evidence was
  never sealed is never reported complete.
- A physical instruction is surfaced only while software cannot proceed
  (an ``await_*`` stage or a blocked step); everything else runs to
  completion without asking.
- Capture runs through the #869 engine only: no silent device, channel
  or sample-rate substitution — the engine's requested-vs-actual record
  is the authority.
- Failed checks, clipping, low SNR, unstable references and stale
  calibration state fail closed: the run blocks or the check records an
  honest non-passing outcome — never a silent pass.
- A combined DAC+ADC loopback calibration stays
  ``combined_dac_analog_adc_loopback``; it never claims component truth.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .canonical_json import canonical_sha256 as _hash
from .cad_delegated_provider import _seal, _require_iso8601, _require_refs
from .clock import utc_now_iso as _utc_now
from .cad_calibration_lifecycle import (
    CadInstrumentFitnessAssessment,
    CadInstrumentInstance,
    CadInstrumentVerificationCheck,
    InstrumentFitnessState,
    VerificationCheckKind,
    VerificationOutcome,
    build_verification_check,
    evaluate_instrument_fitness,
)
from .cad_interface_loopback import (
    CadFrequencyCoverage,
    CadInterfaceCorrectionQualification,
    CadInterfaceIoPath,
    CadInterfaceTransferCalibration,
    CadLoopbackObservation,
    CadPhaseSemantics,
    CalibrationKind,
    CalibrationQuantity,
    CorrectionState,
    build_interface_calibration,
    build_loopback_observation,
    evaluate_interface_correction,
)
from .cad_sweep_acquisition import (
    ArmBlockedError,
    ArmConfirmation,
    AudioIOBackend,
    CaptureResult,
    GeneratedStimulus,
    MeasurementAcquisitionEngine,
    SweepStimulusSpec,
    AcquisitionRequest,
    ChannelRouting,
    SWEEP_ACQUISITION_ENGINE_VERSION,
    IR_DERIVATION_VERSION,
)
from .cad_sweep_acquisition_evidence import (
    CadSweepAcquisitionRun,
    build_stimulus_definition,
)


CALIBRATION_WIZARD_VERSION = 'htdt-calwiz-1'
CHECK_EVALUATION_VERSION = 'htdt-calwiz-eval-1'
_CAMPAIGN_GATE_VERSION = 'htdt-calwiz-gate-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


# ---------------------------------------------------------------------------
# Taxonomies
# ---------------------------------------------------------------------------

CalibrationWizardLane = Literal[
    'interface_loopback',
    'spl_reference_check',
    'campaign_checks',
]

CalibrationWizardStage = Literal[
    'await_loopback',
    'await_calibrator',
    'await_calibrator_pre',
    'await_calibrator_post',
    'await_campaign',
    'preflight',
    'acquire',
    'derive_transfer',
    'quality_gate',
    'seal_calibration',
    'bind_correction',
    'evaluate_check',
    'update_fitness',
    'pre_check',
    'post_check',
    'completed',
    'cancelled',
    'failed',
]

WizardEventKind = Literal[
    'run_created',
    'loopback_confirmed',
    'calibrator_confirmed',
    'preflight_evaluated',
    'acquisition_recorded',
    'transfer_derived',
    'quality_evaluated',
    'calibration_sealed',
    'correction_evaluated',
    'check_evaluated',
    'fitness_evaluated',
    'pre_check_recorded',
    'campaign_completed',
    'post_check_recorded',
    'device_lost',
    'connectivity_restored',
    'retry_step',
    'run_cancelled',
    'run_failed',
]

WizardTransitionOutcome = Literal[
    'advanced',
    'blocked',
    'rejected',
    'informational',
    'completed',
    'cancelled',
    'failed',
    'regressed',
]

WizardActor = Literal['operator', 'machine', 'system']

#: Physical instructions the wizard may ask for — the ONLY vocabulary the
#: UI may present as "do this now". Software never waits on one of these
#: unless the current stage genuinely cannot proceed without it.
WizardPhysicalInstruction = Literal[
    'connect_loopback_cable',
    'position_calibrator',
    'perform_campaign_measurements',
    'restore_device_connection',
    'resolve_blocked_step',
]

CampaignGateVerdict = Literal[
    'checks_pending',
    'checks_passed',
    'checks_limited',
    'checks_failed',
]

#: Lane stage ladders. ``completed`` is listed last; ``cancelled`` and
#: ``failed`` are reachable from every non-terminal stage.
LANE_STAGE_ORDER: dict[str, tuple[CalibrationWizardStage, ...]] = {
    'interface_loopback': (
        'await_loopback',
        'preflight',
        'acquire',
        'derive_transfer',
        'quality_gate',
        'seal_calibration',
        'bind_correction',
        'completed',
    ),
    'spl_reference_check': (
        'await_calibrator',
        'preflight',
        'acquire',
        'evaluate_check',
        'update_fitness',
        'completed',
    ),
    'campaign_checks': (
        'await_calibrator_pre',
        'pre_check',
        'await_campaign',
        'await_calibrator_post',
        'post_check',
        'completed',
    ),
}

TERMINAL_WIZARD_STAGES: frozenset[CalibrationWizardStage] = frozenset(
    {'completed', 'cancelled', 'failed'}
)

#: Where a cancelled/failed check or a hardware hiccup may be retried
#: from. A blocked run never silently rewinds — the operator requests the
#: retry and it lands as a sealed ``regressed`` transition.
_RETRY_TARGET: dict[str, CalibrationWizardStage] = {
    'interface_loopback': 'preflight',
    'spl_reference_check': 'preflight',
    'campaign_checks': 'pre_check',
}

#: (lane, stage, event) -> next stage. Anything not listed is rejected
#: outright or handled by the global rules below.
_ADVANCE_RULES: dict[
    tuple[str, CalibrationWizardStage, WizardEventKind],
    CalibrationWizardStage,
] = {
    ('interface_loopback', 'await_loopback', 'loopback_confirmed'):
        'preflight',
    ('interface_loopback', 'preflight', 'preflight_evaluated'):
        'acquire',
    ('interface_loopback', 'acquire', 'acquisition_recorded'):
        'derive_transfer',
    ('interface_loopback', 'derive_transfer', 'transfer_derived'):
        'quality_gate',
    ('interface_loopback', 'quality_gate', 'quality_evaluated'):
        'seal_calibration',
    ('interface_loopback', 'seal_calibration', 'calibration_sealed'):
        'bind_correction',
    ('interface_loopback', 'bind_correction', 'correction_evaluated'):
        'completed',
    ('spl_reference_check', 'await_calibrator', 'calibrator_confirmed'):
        'preflight',
    ('spl_reference_check', 'preflight', 'preflight_evaluated'):
        'acquire',
    ('spl_reference_check', 'acquire', 'acquisition_recorded'):
        'evaluate_check',
    ('spl_reference_check', 'evaluate_check', 'check_evaluated'):
        'update_fitness',
    ('spl_reference_check', 'update_fitness', 'fitness_evaluated'):
        'completed',
    ('campaign_checks', 'await_calibrator_pre',
     'calibrator_confirmed'): 'pre_check',
    ('campaign_checks', 'pre_check', 'pre_check_recorded'):
        'await_campaign',
    ('campaign_checks', 'await_campaign', 'campaign_completed'):
        'await_calibrator_post',
    ('campaign_checks', 'await_calibrator_post',
     'calibrator_confirmed'): 'post_check',
    ('campaign_checks', 'post_check', 'post_check_recorded'):
        'completed',
}

#: Events whose ``succeeded=False`` answer ends the run instead of
#: blocking it — a recorded verdict that cannot be retried inside the
#: same run (a sealed ineligible binding, a sealed gate verdict).
_FAILING_EVENTS: frozenset[WizardEventKind] = frozenset({
    'correction_evaluated',
    'post_check_recorded',
})

_WIZARD_RUN_KIND = 'calibration_wizard_run'
_SPL_PROFILE_KIND = 'spl_check_acceptance_profile'
_CAMPAIGN_PLAN_KIND = 'campaign_check_plan'
_CAMPAIGN_KINDS = ('campaign_preregistration', 'measurement_campaign')
_INSTRUMENT_KIND = 'instrument_instance'


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class WizardEvent(BaseModel):
    """One attempted state-machine event — always recorded."""

    model_config = ConfigDict(frozen=True)

    kind: WizardEventKind
    at_utc: str = Field(min_length=1)
    reason: str = ''
    actor: WizardActor = 'machine'
    #: For evaluated events: whether the underlying check passed.
    succeeded: bool | None = None
    #: Optional result tag (e.g. the campaign gate verdict, the check
    #: outcome) kept verbatim on the sealed transition.
    result_tag: str | None = None
    evidence_refs: tuple[AuthorityRef, ...] = ()

    @model_validator(mode='after')
    def _valid(self) -> 'WizardEvent':
        _require_iso8601(self.at_utc, 'at_utc')
        _require_refs(*self.evidence_refs)
        return self


# ---------------------------------------------------------------------------
# Sealed record: SPL/reference acceptance profile
# ---------------------------------------------------------------------------


class CadSplCheckAcceptanceProfile(BaseModel):
    """The configured acceptance profile one SPL/reference check is
    evaluated against (cwspl-).

    ``reference_level_db`` records the calibrator's declared acoustic
    level (e.g. 94.0 dB for a class-2 pistonphone); it is traceability
    metadata mirrored onto the verification check, not the pass
    criterion. The pass criterion is ``expected_measured_level_db`` —
    the level the *bound measurement chain* is expected to report when
    the reference is applied — plus tolerances on level, frequency,
    per-window stability, SNR and capture duration.
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    reference_level_db: float
    reference_frequency_hz: float
    expected_measured_level_db: float
    level_tolerance_db: float = Field(gt=0)
    frequency_tolerance_hz: float = Field(gt=0)
    max_level_deviation_db: float = Field(gt=0)
    min_snr_db: float
    min_duration_s: float = Field(gt=0)
    declared_at_utc: str = Field(min_length=1)
    declared_by: str = Field(min_length=1)
    notes: str | None = None

    @model_validator(mode='after')
    def _valid(self) -> 'CadSplCheckAcceptanceProfile':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        for label, value in (
            ('reference_level_db', self.reference_level_db),
            ('reference_frequency_hz', self.reference_frequency_hz),
            ('expected_measured_level_db', self.expected_measured_level_db),
        ):
            if not math.isfinite(float(value)):
                raise ValueError(f'{label} must be finite')
        if self.reference_frequency_hz <= 0:
            raise ValueError('reference_frequency_hz must be positive')
        if self.profile_sha256 != _hash(self.identity_payload()):
            raise ValueError('CadSplCheckAcceptanceProfile hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'profile_id', 'profile_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'CadSplCheckAcceptanceProfile':
        return _seal(
            cls, payload, 'profile_id', 'profile_sha256', 'cwspl')


def spl_profile_binding(
    profile: CadSplCheckAcceptanceProfile,
) -> AuthorityRef:
    return AuthorityRef(
        kind=_SPL_PROFILE_KIND,
        ref_id=profile.profile_id,
        ref_sha256=profile.profile_sha256,
    )


# ---------------------------------------------------------------------------
# Sealed record: campaign check plan (Lane C)
# ---------------------------------------------------------------------------


class RequiredCheckSpec(BaseModel):
    """One check the campaign requires on one instrument."""

    model_config = ConfigDict(frozen=True)

    instrument_ref: AuthorityRef
    check_kind: Literal['pre_use_field_check', 'post_use_field_check']
    acceptance_profile_ref: AuthorityRef | None = None
    #: Optional capture-window duration override for this check.
    capture_duration_s: float | None = Field(default=None, gt=0)

    @model_validator(mode='after')
    def _valid(self) -> 'RequiredCheckSpec':
        _require_refs(self.instrument_ref, self.acceptance_profile_ref)
        if self.instrument_ref.kind != _INSTRUMENT_KIND:
            raise ValueError(
                "instrument_ref kind must be 'instrument_instance'")
        if self.acceptance_profile_ref is not None and (
            self.acceptance_profile_ref.kind != _SPL_PROFILE_KIND
        ):
            raise ValueError(
                "acceptance_profile_ref kind must be "
                "'spl_check_acceptance_profile'")
        return self


class CadCampaignCheckPlan(BaseModel):
    """Sealed declaration of the checks a campaign requires (cwchk-).

    Declared before the campaign runs — the requirement set is itself
    pinned evidence, so a later pass/fail can never reframe what was
    required. ``blocked_on_failure`` is the policy: when true, a failed
    or missing required check blocks dependent campaign evidence.
    """

    model_config = ConfigDict(frozen=True)

    plan_id: str
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    campaign_ref: AuthorityRef
    required_checks: tuple[RequiredCheckSpec, ...] = Field(min_length=1)
    blocked_on_failure: bool = True
    declared_at_utc: str = Field(min_length=1)
    declared_by: str = Field(min_length=1)
    notes: str | None = None

    @model_validator(mode='after')
    def _valid(self) -> 'CadCampaignCheckPlan':
        _require_iso8601(self.declared_at_utc, 'declared_at_utc')
        _require_refs(self.campaign_ref)
        if self.campaign_ref.kind not in _CAMPAIGN_KINDS:
            raise ValueError(
                'campaign_ref kind must be a campaign authority '
                f'({"/".join(_CAMPAIGN_KINDS)})')
        seen = set()
        for spec in self.required_checks:
            key = (spec.instrument_ref.ref_id, spec.check_kind)
            if key in seen:
                raise ValueError(
                    'duplicate required check '
                    f'{key[0]}:{spec.check_kind}')
            seen.add(key)
        if self.plan_sha256 != _hash(self.identity_payload()):
            raise ValueError('CadCampaignCheckPlan hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'plan_id', 'plan_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'CadCampaignCheckPlan':
        return _seal(cls, payload, 'plan_id', 'plan_sha256', 'cwchk')

    def specs_for(
        self, check_kind: str,
    ) -> tuple[RequiredCheckSpec, ...]:
        return tuple(
            spec for spec in self.required_checks
            if spec.check_kind == check_kind)


def campaign_plan_binding(plan: CadCampaignCheckPlan) -> AuthorityRef:
    return AuthorityRef(
        kind=_CAMPAIGN_PLAN_KIND,
        ref_id=plan.plan_id,
        ref_sha256=plan.plan_sha256,
    )


# ---------------------------------------------------------------------------
# Sealed record: wizard run
# ---------------------------------------------------------------------------


class CadCalibrationWizardRun(BaseModel):
    """One guided-calibration run bound to its exact pins (cwrun-).

    The run is created with its lane and every pin the lane needs —
    Lane A cannot start without the exact I/O path + routing + stimulus,
    Lane B without the instrument + acceptance profile, Lane C without
    the campaign + check plan. A changed pin is a different run.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    run_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    lane: CalibrationWizardLane
    wizard_version: str = CALIBRATION_WIZARD_VERSION
    engine_version: str = SWEEP_ACQUISITION_ENGINE_VERSION
    derivation_version: str = IR_DERIVATION_VERSION
    check_evaluation_version: str = CHECK_EVALUATION_VERSION
    #: Lane A: the exact interface path the calibration will bind to.
    io_path: CadInterfaceIoPath | None = None
    #: Lanes A+B: the device/channel binding handed to the engine.
    routing: ChannelRouting | None = None
    #: Lanes A+B: the stimulus the engine plays — for Lane B this is the
    #: near-silent record window (the physical calibrator supplies the
    #: acoustic signal; software plays silence and records).
    stimulus_spec: SweepStimulusSpec | None = None
    #: Lane B: the instrument under check + optional calibrator identity.
    instrument_ref: AuthorityRef | None = None
    calibrator_ref: AuthorityRef | None = None
    #: Lane B (required) / Lane C specs (per check): acceptance profile.
    acceptance_profile_ref: AuthorityRef | None = None
    #: Lane B: which verification-check kind the lane records.
    check_kind: VerificationCheckKind = 'interim_check'
    #: Lane C: the campaign + its check plan.
    campaign_ref: AuthorityRef | None = None
    check_plan_ref: AuthorityRef | None = None
    #: Lane C: routing/spec used when the wizard itself captures the
    #: campaign checks (kept on the run, not the plan — the plan stays
    #: capture-agnostic).
    created_at_utc: str = Field(min_length=1)
    created_by: str = Field(min_length=1)
    notes: str | None = None

    @model_validator(mode='after')
    def _valid(self) -> 'CadCalibrationWizardRun':
        _require_iso8601(self.created_at_utc, 'created_at_utc')
        _require_refs(
            self.instrument_ref, self.calibrator_ref,
            self.acceptance_profile_ref, self.campaign_ref,
            self.check_plan_ref)
        if self.lane == 'interface_loopback':
            for label, value in (
                ('io_path', self.io_path),
                ('routing', self.routing),
                ('stimulus_spec', self.stimulus_spec),
            ):
                if value is None:
                    raise ValueError(
                        f'interface_loopback run requires {label}')
            if self.routing.loopback_input_channel is None:
                raise ValueError(
                    'interface_loopback run requires a wired loopback '
                    'reference channel in its routing')
        elif self.lane == 'spl_reference_check':
            if self.instrument_ref is None:
                raise ValueError(
                    'spl_reference_check run requires instrument_ref')
            if self.instrument_ref.kind != _INSTRUMENT_KIND:
                raise ValueError(
                    "instrument_ref kind must be 'instrument_instance'")
            if self.calibrator_ref is not None and (
                self.calibrator_ref.kind != _INSTRUMENT_KIND
            ):
                raise ValueError(
                    "calibrator_ref kind must be 'instrument_instance'")
            if self.acceptance_profile_ref is None:
                raise ValueError(
                    'spl_reference_check run requires '
                    'acceptance_profile_ref')
            if self.acceptance_profile_ref.kind != _SPL_PROFILE_KIND:
                raise ValueError(
                    "acceptance_profile_ref kind must be "
                    "'spl_check_acceptance_profile'")
            if self.routing is None or self.stimulus_spec is None:
                raise ValueError(
                    'spl_reference_check run requires routing and '
                    'stimulus_spec (the record window)')
        elif self.lane == 'campaign_checks':
            if self.routing is None or self.stimulus_spec is None:
                raise ValueError(
                    'campaign_checks run requires routing and '
                    'stimulus_spec (the record window for its checks)')
            if self.campaign_ref is None:
                raise ValueError(
                    'campaign_checks run requires campaign_ref')
            if self.campaign_ref.kind not in _CAMPAIGN_KINDS:
                raise ValueError(
                    'campaign_ref kind must be a campaign authority')
            if self.check_plan_ref is None:
                raise ValueError(
                    'campaign_checks run requires check_plan_ref')
            if self.check_plan_ref.kind != _CAMPAIGN_PLAN_KIND:
                raise ValueError(
                    "check_plan_ref kind must be 'campaign_check_plan'")
        if self.run_sha256 != _hash(self.identity_payload()):
            raise ValueError('CadCalibrationWizardRun hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'run_id', 'run_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'CadCalibrationWizardRun':
        return _seal(cls, payload, 'run_id', 'run_sha256', 'cwrun')


def wizard_run_binding(run: CadCalibrationWizardRun) -> AuthorityRef:
    return AuthorityRef(
        kind=_WIZARD_RUN_KIND,
        ref_id=run.run_id,
        ref_sha256=run.run_sha256,
    )


# ---------------------------------------------------------------------------
# Sealed record: wizard transition
# ---------------------------------------------------------------------------


class CadCalibrationWizardTransition(BaseModel):
    """One sealed wizard state-machine transition (cwtrn-).

    Every attempted event — advance, block, reject, regress, cancel —
    is recorded with the stage it started from, where it landed, the
    actor, an explicit reason, the optional result tag and the evidence
    refs it produced or consumed. The sealed log is the resume authority:
    the folded state is recomputed from it, never persisted separately.
    """

    model_config = ConfigDict(frozen=True)

    transition_id: str
    transition_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    run_ref: AuthorityRef
    seq: int = Field(ge=0)
    event_kind: WizardEventKind
    outcome: WizardTransitionOutcome
    actor: WizardActor = 'machine'
    from_stage: CalibrationWizardStage | None = None
    to_stage: CalibrationWizardStage
    event_succeeded: bool | None = None
    result_tag: str | None = None
    reason: str = Field(min_length=1)
    evidence_refs: tuple[AuthorityRef, ...] = ()
    recorded_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _valid(self) -> 'CadCalibrationWizardTransition':
        _require_refs(self.run_ref)
        if self.run_ref.kind != _WIZARD_RUN_KIND:
            raise ValueError(
                "run_ref kind must be 'calibration_wizard_run'")
        _require_refs(*self.evidence_refs)
        if self.event_kind == 'run_created':
            if self.from_stage is not None:
                raise ValueError('run_created has no from_stage')
        elif self.from_stage is None:
            raise ValueError('non-creation transitions require from_stage')
        if self.outcome == 'advanced' \
                and self.to_stage == self.from_stage:
            raise ValueError('advanced transitions must change stage')
        if self.outcome in ('rejected', 'blocked', 'informational') \
                and self.to_stage != self.from_stage:
            raise ValueError(
                f'{self.outcome} transitions must not change stage')
        if self.outcome == 'regressed' \
                and self.to_stage == self.from_stage:
            raise ValueError('regressed transitions must change stage')
        for outcome, stage in (
            ('completed', 'completed'),
            ('cancelled', 'cancelled'),
            ('failed', 'failed'),
        ):
            if self.outcome == outcome and self.to_stage != stage:
                raise ValueError(
                    f'{outcome} transitions must land on {stage}')
        if self.transition_sha256 != _hash(self.identity_payload()):
            raise ValueError(
                'CadCalibrationWizardTransition hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'transition_id', 'transition_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'CadCalibrationWizardTransition':
        return _seal(
            cls, payload, 'transition_id', 'transition_sha256', 'cwtrn')


# ---------------------------------------------------------------------------
# Folded state — the resume authority
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalibrationWizardState:
    """The folded state of one wizard run — pure derivation."""

    run_ref: AuthorityRef
    lane: CalibrationWizardLane
    current_stage: CalibrationWizardStage
    furthest_stage: CalibrationWizardStage
    last_event_at_utc: str | None = None
    last_reason: str = ''
    blocked_reason: str | None = None
    device_disconnected: bool = False
    completed: bool = False
    cancelled: bool = False
    failed: bool = False
    transition_count: int = 0
    seen_kinds: frozenset[str] = frozenset()
    evidence: dict[str, AuthorityRef] = field(default_factory=dict)
    #: Latest result_tag recorded by an evaluated event (e.g. the check
    #: outcome or the campaign gate verdict).
    last_result_tag: str | None = None


@dataclass(frozen=True)
class WizardTransitionDecision:
    to_stage: CalibrationWizardStage
    outcome: WizardTransitionOutcome
    reason: str


@dataclass(frozen=True)
class WizardTransitionRejection:
    reason: str


def _stage_index(lane: str, stage: CalibrationWizardStage) -> int:
    try:
        return LANE_STAGE_ORDER[lane].index(stage)
    except ValueError:
        return len(LANE_STAGE_ORDER[lane])


def wizard_transition(
    state: CalibrationWizardState,
    event: WizardEvent,
) -> WizardTransitionDecision | WizardTransitionRejection:
    """Pure, deterministic, total: (state, event) -> decision|rejection.

    No implicit transitions and no side effects. A disconnect flag
    rejects every event except restore/cancel; a ``succeeded=False``
    evaluated event lands ``blocked`` at the same stage (or ``failed``
    for the events in :data:`_FAILING_EVENTS`); an illegal stage/event
    pair is an explicit rejection with a reason.
    """
    kind = event.kind
    lane = state.lane
    current = state.current_stage

    if kind == 'run_created':
        first = LANE_STAGE_ORDER[lane][0]
        return WizardTransitionDecision(first, 'advanced', event.reason)

    if current in TERMINAL_WIZARD_STAGES:
        return WizardTransitionRejection(f'run_terminal:{current}')

    if kind == 'run_cancelled':
        return WizardTransitionDecision(
            'cancelled', 'cancelled', event.reason or 'operator cancelled')
    if kind == 'run_failed':
        return WizardTransitionDecision('failed', 'failed', event.reason)

    if kind == 'connectivity_restored':
        if not state.device_disconnected:
            return WizardTransitionRejection('nothing_disconnected')
        return WizardTransitionDecision(
            current, 'informational', event.reason)

    if state.device_disconnected:
        if kind == 'device_lost':
            return WizardTransitionRejection('already_disconnected:device')
        return WizardTransitionRejection('device_disconnected')

    if kind == 'device_lost':
        return WizardTransitionDecision(current, 'blocked', event.reason)

    if kind == 'retry_step':
        target = _RETRY_TARGET[lane]
        if _stage_index(lane, target) >= _stage_index(lane, current):
            return WizardTransitionRejection(
                f'no_regression:{current}->{target}')
        return WizardTransitionDecision(
            target, 'regressed', event.reason or 'operator retry')

    nxt = _ADVANCE_RULES.get((lane, current, kind))
    if nxt is None:
        return WizardTransitionRejection(
            f'event_not_permitted:{kind}@{current}')
    if event.succeeded is False:
        if kind in _FAILING_EVENTS:
            return WizardTransitionDecision(
                'failed', 'failed', event.reason or 'step failed')
        return WizardTransitionDecision(current, 'blocked', event.reason)
    if nxt == 'completed':
        return WizardTransitionDecision(
            'completed', 'completed', event.reason)
    return WizardTransitionDecision(nxt, 'advanced', event.reason)


def derive_wizard_state(
    run: CadCalibrationWizardRun,
    transitions: tuple[CadCalibrationWizardTransition, ...],
) -> CalibrationWizardState:
    """Fold the sealed transition log into the current wizard state.

    Pure and deterministic — restarting the app folds the same log to
    the same state. A stage whose transition was never sealed does not
    exist, so an interrupted run resumes at the last sealed stage and
    can never read as more complete than the evidence.
    """
    run_ref = wizard_run_binding(run)
    first = LANE_STAGE_ORDER[run.lane][0]
    state = CalibrationWizardState(
        run_ref=run_ref, lane=run.lane,
        current_stage=first, furthest_stage=first)
    furthest_index = 0
    seen: set[str] = set()
    evidence: dict[str, AuthorityRef] = {}
    completed = cancelled = failed = device_down = False
    current: CalibrationWizardStage = first
    blocked_reason: str | None = None
    last_reason = ''
    last_at: str | None = None
    last_tag: str | None = None

    for t in sorted(transitions, key=lambda item: item.seq):
        seen.add(t.event_kind)
        last_at = t.recorded_at_utc
        last_reason = t.reason
        if t.result_tag is not None:
            last_tag = t.result_tag
        if t.event_kind == 'device_lost' \
                and t.outcome in ('blocked', 'advanced'):
            device_down = True
        elif t.event_kind == 'connectivity_restored' \
                and t.outcome == 'informational':
            device_down = False
            blocked_reason = None

        if t.outcome == 'advanced' or t.outcome == 'regressed':
            current = t.to_stage
            blocked_reason = None
            furthest_index = max(
                furthest_index, _stage_index(run.lane, current))
        elif t.outcome == 'completed':
            current = 'completed'
            completed = True
            blocked_reason = None
        elif t.outcome == 'cancelled':
            current = 'cancelled'
            cancelled = True
            blocked_reason = None
        elif t.outcome == 'failed':
            current = 'failed'
            failed = True
            blocked_reason = None
        elif t.outcome in ('blocked', 'rejected'):
            blocked_reason = t.reason
        for ref in t.evidence_refs:
            evidence[ref.kind] = ref

    furthest = LANE_STAGE_ORDER[run.lane][
        min(furthest_index, len(LANE_STAGE_ORDER[run.lane]) - 1)]
    return CalibrationWizardState(
        run_ref=run_ref,
        lane=run.lane,
        current_stage=current,
        furthest_stage=furthest,
        last_event_at_utc=last_at,
        last_reason=last_reason,
        blocked_reason=blocked_reason,
        device_disconnected=device_down,
        completed=completed,
        cancelled=cancelled,
        failed=failed,
        transition_count=len(transitions),
        seen_kinds=frozenset(seen),
        evidence=evidence,
        last_result_tag=last_tag,
    )


def next_permitted_events(
    state: CalibrationWizardState,
) -> tuple[WizardEventKind, ...]:
    """Event kinds the machine would accept right now — the UI's menu."""
    candidates: tuple[WizardEventKind, ...] = (
        'loopback_confirmed',
        'calibrator_confirmed',
        'preflight_evaluated',
        'acquisition_recorded',
        'transfer_derived',
        'quality_evaluated',
        'calibration_sealed',
        'correction_evaluated',
        'check_evaluated',
        'fitness_evaluated',
        'pre_check_recorded',
        'campaign_completed',
        'post_check_recorded',
        'retry_step',
        'device_lost',
        'connectivity_restored',
        'run_cancelled',
        'run_failed',
    )
    probe = WizardEvent(kind='run_created', at_utc='2026-01-01T00:00:00Z',
                        reason='probe')
    permitted: list[WizardEventKind] = []
    for kind in candidates:
        probe = probe.model_copy(update={'kind': kind})
        if isinstance(
                wizard_transition(state, probe), WizardTransitionDecision):
            permitted.append(kind)
    return tuple(permitted)


#: Stage -> instruction vocabulary. The instruction is the *physical* ask
#: of the current stage; nothing else may be presented as a physical ask.
_AWAIT_INSTRUCTIONS: dict[CalibrationWizardStage, WizardPhysicalInstruction] = {
    'await_loopback': 'connect_loopback_cable',
    'await_calibrator': 'position_calibrator',
    'await_calibrator_pre': 'position_calibrator',
    'await_calibrator_post': 'position_calibrator',
    'await_campaign': 'perform_campaign_measurements',
}


def pending_physical_instruction(
    state: CalibrationWizardState,
) -> WizardPhysicalInstruction | None:
    """The physical action the operator must take, if any.

    Software-driven stages return ``None`` — the wizard never asks for
    physical handling while it can still proceed on its own. A blocked
    run asks either for the device to come back or for the blocked step
    to be resolved (retry is the operator's explicit choice).
    """
    if state.current_stage in TERMINAL_WIZARD_STAGES:
        return None
    if state.device_disconnected:
        return 'restore_device_connection'
    if state.current_stage in _AWAIT_INSTRUCTIONS:
        return _AWAIT_INSTRUCTIONS[state.current_stage]
    if state.blocked_reason is not None:
        return 'resolve_blocked_step'
    return None


# ---------------------------------------------------------------------------
# Reference-tone measurement + SPL/reference check evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReferenceToneMeasurement:
    """What the captured record window actually measured."""

    measurable: bool
    observed_level_dbfs: float | None = None
    observed_frequency_hz: float | None = None
    level_stability_db: float | None = None
    snr_db: float | None = None
    duration_s: float = 0.0
    reasons: tuple[str, ...] = ()


def measure_reference_tone(
    capture: CaptureResult,
    *,
    channel_position: int = 0,
    window_s: float = 0.25,
) -> ReferenceToneMeasurement:
    """Measure level/frequency/stability of a captured reference window.

    Pure numpy, deterministic: overall RMS for level, an FFT peak for the
    dominant frequency, per-window RMS spread for stability, and a
    peak-bin-vs-median ratio for SNR. A capture that cannot be measured
    returns ``measurable=False`` with the reason — unknown stays unknown.
    """
    if capture.recorded_frames == 0 or len(capture.captured_channels) == 0:
        return ReferenceToneMeasurement(
            measurable=False, reasons=('empty capture',))
    samples = np.asarray(
        capture.channel_samples(channel_position), dtype=np.float64)
    rate = int(capture.actual_sample_rate_hz)
    duration_s = capture.recorded_frames / rate
    rms = float(np.sqrt(np.mean(samples ** 2))) if len(samples) else 0.0
    level_dbfs = 20.0 * math.log10(max(rms, 1e-15))

    # Dominant frequency: FFT peak on the full window. NaN/inf content
    # makes the spectrum unmeasurable — fail closed.
    if not np.isfinite(samples).all():
        return ReferenceToneMeasurement(
            measurable=False, duration_s=duration_s,
            observed_level_dbfs=level_dbfs,
            reasons=('capture contains non-finite samples',))
    spectrum = np.abs(np.fft.rfft(samples))
    if len(spectrum) < 2 or float(spectrum.max()) <= 0.0:
        return ReferenceToneMeasurement(
            measurable=False, duration_s=duration_s,
            observed_level_dbfs=level_dbfs,
            reasons=('no measurable tone energy',))
    peak_bin = int(np.argmax(spectrum))
    frequency_hz = peak_bin * rate / len(samples)
    # SNR: peak-bin energy vs the median spectral floor.
    median = float(np.median(spectrum))
    snr_db = (
        20.0 * math.log10(float(spectrum[peak_bin]) / max(median, 1e-15))
        if median > 0 else None
    )

    # Stability: per-window RMS spread (max - min across full windows).
    win = max(8, int(round(window_s * rate)))
    n_win = len(samples) // win
    stability_db: float | None = None
    if n_win >= 2:
        levels = [
            20.0 * math.log10(max(
                float(np.sqrt(np.mean(samples[i * win:(i + 1) * win] ** 2))),
                1e-15))
            for i in range(n_win)
        ]
        stability_db = max(levels) - min(levels)

    return ReferenceToneMeasurement(
        measurable=True,
        observed_level_dbfs=level_dbfs,
        observed_frequency_hz=frequency_hz,
        level_stability_db=stability_db,
        snr_db=snr_db,
        duration_s=duration_s,
        reasons=(),
    )


@dataclass(frozen=True)
class ReferenceCheckEvaluation:
    """The sealed-check outcome + reasons — what gets recorded."""

    outcome: VerificationOutcome
    reasons: tuple[str, ...]
    observed_value: float | None
    observed_deviation_db: float | None
    measured: ReferenceToneMeasurement


def evaluate_reference_check(
    capture: CaptureResult | None,
    profile: CadSplCheckAcceptanceProfile,
    *,
    channel_position: int = 0,
    window_s: float = 0.25,
) -> ReferenceCheckEvaluation:
    """Fail-closed evaluation of one SPL/reference check capture.

    A capture that never completed, clipped, underran, or could not be
    measured is ``inconclusive`` — evidence exists but passes nothing.
    Measurable results map to ``within_tolerance`` /
    ``deviation_observed`` (≤2× tolerance) / ``out_of_tolerance``.
    """
    if capture is None:
        return ReferenceCheckEvaluation(
            outcome='inconclusive', reasons=('no capture was produced',),
            observed_value=None, observed_deviation_db=None,
            measured=ReferenceToneMeasurement(
                measurable=False, reasons=('no capture',)))

    reasons: list[str] = []
    if capture.outcome != 'completed':
        reasons.append(f'capture_{capture.outcome}')
    if capture.truncated \
            or capture.recorded_frames < capture.expected_frames:
        reasons.append('capture_truncated')
    if capture.clipped_samples > 0:
        reasons.append('clipping_detected')
    if capture.xrun_count > 0:
        reasons.append('xrun_detected')
    measured = measure_reference_tone(
        capture, channel_position=channel_position, window_s=window_s)
    if measured.duration_s < profile.min_duration_s:
        reasons.append('insufficient_duration')
    if not measured.measurable:
        reasons.extend(measured.reasons)
    if measured.snr_db is not None \
            and measured.snr_db < profile.min_snr_db:
        reasons.append('insufficient_snr')
    if reasons:
        return ReferenceCheckEvaluation(
            outcome='inconclusive', reasons=tuple(reasons),
            observed_value=measured.observed_level_dbfs,
            observed_deviation_db=None, measured=measured)

    level_dev = (
        measured.observed_level_dbfs - profile.expected_measured_level_db)
    freq_dev = abs(
        measured.observed_frequency_hz - profile.reference_frequency_hz)
    reasons.append(f'level_deviation={level_dev:+.2f}dB')
    reasons.append(f'frequency_deviation={freq_dev:.2f}Hz')
    if measured.level_stability_db is not None:
        reasons.append(
            f'level_stability={measured.level_stability_db:.2f}dB')

    if measured.level_stability_db is not None \
            and measured.level_stability_db > profile.max_level_deviation_db:
        reasons.append('unstable_reference')
        return ReferenceCheckEvaluation(
            outcome='out_of_tolerance', reasons=tuple(reasons),
            observed_value=measured.observed_level_dbfs,
            observed_deviation_db=level_dev, measured=measured)
    if freq_dev > profile.frequency_tolerance_hz:
        reasons.append('frequency_mismatch')
        return ReferenceCheckEvaluation(
            outcome='out_of_tolerance', reasons=tuple(reasons),
            observed_value=measured.observed_level_dbfs,
            observed_deviation_db=level_dev, measured=measured)
    if abs(level_dev) > 2.0 * profile.level_tolerance_db:
        return ReferenceCheckEvaluation(
            outcome='out_of_tolerance', reasons=tuple(reasons),
            observed_value=measured.observed_level_dbfs,
            observed_deviation_db=level_dev, measured=measured)
    if abs(level_dev) > profile.level_tolerance_db:
        return ReferenceCheckEvaluation(
            outcome='deviation_observed', reasons=tuple(reasons),
            observed_value=measured.observed_level_dbfs,
            observed_deviation_db=level_dev, measured=measured)
    return ReferenceCheckEvaluation(
        outcome='within_tolerance', reasons=tuple(reasons),
        observed_value=measured.observed_level_dbfs,
        observed_deviation_db=level_dev, measured=measured)


# ---------------------------------------------------------------------------
# Campaign check gate (Lane C)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CampaignCheckGateResult:
    """Whether the campaign's required checks satisfy the plan."""

    verdict: CampaignGateVerdict
    blocked: bool
    reasons: tuple[str, ...]
    satisfied: tuple[str, ...]
    missing: tuple[str, ...]
    failed: tuple[str, ...]


def evaluate_campaign_check_gate(
    plan: CadCampaignCheckPlan,
    checks: tuple[CadInstrumentVerificationCheck, ...] | list[
        CadInstrumentVerificationCheck],
) -> CampaignCheckGateResult:
    """Fail-closed gate over the campaign's required checks.

    Only checks bound to this campaign (``campaign_id`` equal to the
    pinned campaign ref id) satisfy a requirement — a check for another
    campaign can never stand in. ``blocked`` reports the policy outcome:
    with ``blocked_on_failure`` set, any missing/failed/inconclusive
    required check blocks dependent campaign evidence.
    """
    satisfied: list[str] = []
    missing: list[str] = []
    limited: list[str] = []
    failed: list[str] = []
    reasons: list[str] = []
    campaign_id = plan.campaign_ref.ref_id

    for spec in plan.required_checks:
        key = f'{spec.instrument_ref.ref_id}:{spec.check_kind}'
        bound = [
            c for c in checks
            if c.instrument_ref.ref_id == spec.instrument_ref.ref_id
            and c.kind == spec.check_kind
            and c.campaign_id == campaign_id
        ]
        if not bound:
            missing.append(key)
            reasons.append(f'{key}: no check recorded for this campaign')
            continue
        latest = max(bound, key=lambda c: c.performed_at_utc)
        if latest.outcome == 'within_tolerance':
            satisfied.append(key)
        elif latest.outcome == 'deviation_observed':
            limited.append(key)
            reasons.append(f'{key}: deviation_observed — limited')
        else:
            failed.append(key)
            reasons.append(f'{key}: {latest.outcome}')

    if missing or failed:
        verdict: CampaignGateVerdict = (
            'checks_pending' if not failed else 'checks_failed')
    elif limited:
        verdict = 'checks_limited'
    else:
        verdict = 'checks_passed'
    blocked = plan.blocked_on_failure and verdict in (
        'checks_pending', 'checks_failed')
    if blocked:
        reasons.append(
            'dependent campaign evidence blocked by plan policy')
    return CampaignCheckGateResult(
        verdict=verdict, blocked=blocked, reasons=tuple(reasons),
        satisfied=tuple(satisfied), missing=tuple(missing),
        failed=tuple(failed))




# ---------------------------------------------------------------------------
# Persistence bundle — every store is optional; the machine + artifact
# builders work identically without them (records are sealed and kept in
# memory; persistence is the repository's job, not the machine's).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WizardStores:
    """Repositories the wizard persists through when present.

    ``wizard`` stores the run/transition/plan/profile records of this
    authority; ``sweep``/``interface``/``lifecycle`` persist the reused
    #869/#699/#611 artifacts the wizard produces.
    """

    wizard: Any = None
    sweep: Any = None
    interface: Any = None
    lifecycle: Any = None


class CalibrationWizardError(ValueError):
    """A wizard operation was invoked against the wrong state."""


def _ref(record: Any, kind: str, id_field: str, sha_field: str) -> AuthorityRef:
    return AuthorityRef(
        kind=kind,
        ref_id=getattr(record, id_field),
        ref_sha256=getattr(record, sha_field),
    )


def _qualification_binding(
    qualification: CadInterfaceCorrectionQualification,
) -> AuthorityRef:
    return _ref(
        qualification, 'interface_correction_qualification',
        'qualification_id', 'qualification_sha256')


def _assessment_binding(
    assessment: CadInstrumentFitnessAssessment,
) -> AuthorityRef:
    return _ref(
        assessment, 'instrument_fitness_assessment',
        'assessment_id', 'assessment_sha256')


#: The acquisition run record's authority kind (see #869 evidence).
_SWEEP_RUN_KIND = 'sweep_acquisition_run'
_STIMULUS_KIND = 'sweep_stimulus_definition'


class CalibrationWizard:
    """The guided driver — the machine + the software steps between its
    physical prompts.

    Contract with the UI:

    - ``begin_*`` seals the run and lands on the first ``await_*`` stage.
    - ``run_automatic()`` executes every software stage until the machine
      needs a physical action, blocks, or terminates. It never invents
      completion: each stage writes exactly one sealed transition.
    - ``confirm_hardware()`` is the only way past an ``await_*`` stage —
      it records the operator's physical confirmation.
    - ``resume(...)`` rebuilds the driver from the sealed log: stages
      whose artifacts already sealed are reloaded through the stores, an
      interrupted ``acquire`` re-runs fresh, and nothing is ever reported
      more complete than the sealed evidence.
    """

    def __init__(
        self,
        document_id: str,
        backend: AudioIOBackend,
        *,
        stores: WizardStores | None = None,
        clock: Callable[[], str] = _utc_now,
        run: CadCalibrationWizardRun | None = None,
        transitions: tuple[CadCalibrationWizardTransition, ...] = (),
        instrument: CadInstrumentInstance | None = None,
        acceptance_profile: CadSplCheckAcceptanceProfile | None = None,
        check_plan: CadCampaignCheckPlan | None = None,
        level_policy: Any = None,
    ) -> None:
        self.document_id = document_id
        self.backend = backend
        self.stores = stores or WizardStores()
        self._clock = clock
        self._run = run
        self._transitions: list[CadCalibrationWizardTransition] = list(
            transitions)
        self._instrument = instrument
        self._profile = acceptance_profile
        self._plan = check_plan
        self._level_policy = level_policy
        # transient working artifacts — rebuilt on resume where possible
        self._engine: MeasurementAcquisitionEngine | None = None
        self._observation: CadLoopbackObservation | None = None
        self._calibration: CadInterfaceTransferCalibration | None = None
        self._qualification: (
            CadInterfaceCorrectionQualification | None) = None
        self._acquisition_run: CadSweepAcquisitionRun | None = None
        self._stimulus_ref: AuthorityRef | None = None
        self._stimulus_definition: Any = None
        self._checks: list[CadInstrumentVerificationCheck] = []
        self._profiles: dict[str, CadSplCheckAcceptanceProfile] = {}
        if acceptance_profile is not None:
            self._profiles[acceptance_profile.profile_id] = (
                acceptance_profile)
        if run is not None:
            self._hydrate_from_log()

    # -- introspection -------------------------------------------------

    @property
    def run(self) -> CadCalibrationWizardRun | None:
        return self._run

    @property
    def transitions(self) -> tuple[CadCalibrationWizardTransition, ...]:
        return tuple(self._transitions)

    @property
    def state(self) -> CalibrationWizardState | None:
        if self._run is None:
            return None
        return derive_wizard_state(self._run, tuple(self._transitions))

    @property
    def pending_instruction(self) -> WizardPhysicalInstruction | None:
        state = self.state
        return (
            pending_physical_instruction(state) if state is not None
            else None
        )

    @property
    def engine(self) -> MeasurementAcquisitionEngine | None:
        return self._engine

    @property
    def sealed_checks(self) -> tuple[CadInstrumentVerificationCheck, ...]:
        return tuple(self._checks)

    @property
    def sealed_calibration(self) -> (
            CadInterfaceTransferCalibration | None):
        return self._calibration

    @property
    def sealed_observation(self) -> CadLoopbackObservation | None:
        return self._observation

    @property
    def sealed_qualification(self) -> (
            CadInterfaceCorrectionQualification | None):
        return self._qualification

    @property
    def acquisition_run(self) -> CadSweepAcquisitionRun | None:
        return self._acquisition_run

    # -- run creation ----------------------------------------------------

    def begin_loopback(
        self,
        *,
        io_path: CadInterfaceIoPath,
        routing: ChannelRouting,
        stimulus_spec: SweepStimulusSpec,
        created_by: str,
        at_utc: str | None = None,
        notes: str | None = None,
    ) -> CadCalibrationWizardRun:
        """Start Lane A. Lands on ``await_loopback`` — the cable prompt."""
        return self._begin(
            lane='interface_loopback', io_path=io_path, routing=routing,
            stimulus_spec=stimulus_spec, created_by=created_by,
            at_utc=at_utc, notes=notes)

    def begin_reference_check(
        self,
        *,
        instrument: CadInstrumentInstance,
        acceptance_profile: CadSplCheckAcceptanceProfile,
        routing: ChannelRouting,
        stimulus_spec: SweepStimulusSpec,
        calibrator: CadInstrumentInstance | None = None,
        check_kind: VerificationCheckKind = 'interim_check',
        created_by: str,
        at_utc: str | None = None,
        notes: str | None = None,
    ) -> CadCalibrationWizardRun:
        """Start Lane B. Lands on ``await_calibrator`` — attach the
        calibrator/reference to the bound instrument."""
        if self.stores.wizard is not None:
            self.stores.wizard.save_profile(acceptance_profile)
        return self._begin(
            lane='spl_reference_check', routing=routing,
            stimulus_spec=stimulus_spec,
            instrument_ref=_ref(
                instrument, _INSTRUMENT_KIND,
                'instrument_id', 'instrument_sha256'),
            calibrator_ref=(
                _ref(calibrator, _INSTRUMENT_KIND,
                     'instrument_id', 'instrument_sha256')
                if calibrator is not None else None),
            acceptance_profile_ref=spl_profile_binding(acceptance_profile),
            check_kind=check_kind, created_by=created_by, at_utc=at_utc,
            notes=notes, _instrument=instrument,
            _profile=acceptance_profile)

    def begin_campaign_checks(
        self,
        *,
        campaign_ref: AuthorityRef,
        plan: CadCampaignCheckPlan,
        routing: ChannelRouting,
        stimulus_spec: SweepStimulusSpec,
        created_by: str,
        at_utc: str | None = None,
        notes: str | None = None,
    ) -> CadCalibrationWizardRun:
        """Start Lane C. The plan is sealed first — requirements are
        pinned before any check may claim to satisfy them."""
        if plan.campaign_ref.ref_id != campaign_ref.ref_id:
            raise CalibrationWizardError(
                'check plan campaign does not match the bound campaign')
        if self.stores.wizard is not None:
            self.stores.wizard.save_check_plan(plan)
        return self._begin(
            lane='campaign_checks', campaign_ref=campaign_ref,
            check_plan_ref=campaign_plan_binding(plan), routing=routing,
            stimulus_spec=stimulus_spec, created_by=created_by,
            at_utc=at_utc, notes=notes, _plan=plan)

    def _begin(self, *, lane: CalibrationWizardLane, created_by: str,
               at_utc: str | None, notes: str | None,
               _instrument=None, _profile=None, _plan=None,
               **pins: Any) -> CadCalibrationWizardRun:
        if self._run is not None:
            raise CalibrationWizardError('wizard already has a run')
        created_at = at_utc or self._clock()
        run = CadCalibrationWizardRun.create(
            document_id=self.document_id, lane=lane,
            created_at_utc=created_at, created_by=created_by,
            notes=notes, **pins)
        self._run = run
        if _instrument is not None:
            self._instrument = _instrument
        if _profile is not None:
            self._profile = _profile
            self._profiles[_profile.profile_id] = _profile
        if _plan is not None:
            self._plan = _plan
        if self.stores.wizard is not None:
            self.stores.wizard.save_run(run)
        self._apply(WizardEvent(
            kind='run_created', at_utc=created_at,
            reason=f'{lane} run created'))
        return run

    # -- the sealed log --------------------------------------------------

    def _apply(
        self, event: WizardEvent,
    ) -> CadCalibrationWizardTransition:
        """Ask the machine, seal the answer, persist it. Rejections are
        sealed too — the log is the audit."""
        if self._run is None:
            raise CalibrationWizardError('no run — call a begin_* first')
        state = derive_wizard_state(self._run, tuple(self._transitions))
        decision = wizard_transition(state, event)
        # run_created is sealed with a null from_stage — the validator
        # requires it even though the folded pre-run state has one.
        from_stage = (
            None if event.kind == 'run_created' else state.current_stage)
        if isinstance(decision, WizardTransitionRejection):
            transition = CadCalibrationWizardTransition.create(
                document_id=self.document_id,
                run_ref=wizard_run_binding(self._run),
                seq=len(self._transitions),
                event_kind=event.kind,
                outcome='rejected',
                actor=event.actor,
                from_stage=from_stage,
                to_stage=state.current_stage,
                event_succeeded=event.succeeded,
                result_tag=event.result_tag,
                reason=decision.reason or event.reason or 'rejected',
                evidence_refs=tuple(event.evidence_refs),
                recorded_at_utc=event.at_utc,
            )
        else:
            transition = CadCalibrationWizardTransition.create(
                document_id=self.document_id,
                run_ref=wizard_run_binding(self._run),
                seq=len(self._transitions),
                event_kind=event.kind,
                outcome=decision.outcome,
                actor=event.actor,
                from_stage=from_stage,
                to_stage=decision.to_stage,
                event_succeeded=event.succeeded,
                result_tag=event.result_tag,
                reason=decision.reason or event.reason or event.kind,
                evidence_refs=tuple(event.evidence_refs),
                recorded_at_utc=event.at_utc,
            )
        self._transitions.append(transition)
        if self.stores.wizard is not None:
            self.stores.wizard.save_transition(transition)
        return transition

    # -- physical prompts -------------------------------------------------

    def confirm_hardware(
        self, *, at_utc: str | None = None, reason: str = '',
    ) -> CadCalibrationWizardTransition:
        """The operator's one answer to a physical prompt.

        Accepted only while the machine is parked at an ``await_*`` stage
        that asks for hardware handling — a confirmation at any other
        stage is recorded as a rejected event, never an implicit advance.
        """
        state = self._require_state()
        stage = state.current_stage
        kind: WizardEventKind
        if stage == 'await_loopback':
            kind = 'loopback_confirmed'
        elif stage in (
                'await_calibrator',
                'await_calibrator_pre',
                'await_calibrator_post'):
            kind = 'calibrator_confirmed'
        else:
            return self._apply(WizardEvent(
                kind='loopback_confirmed',
                at_utc=at_utc or self._clock(),
                reason=reason or 'hardware confirmation at wrong stage',
                actor='operator'))
        return self._apply(WizardEvent(
            kind=kind, at_utc=at_utc or self._clock(),
            reason=reason or 'operator confirmed physical setup',
            actor='operator'))

    def record_campaign_completed(
        self, *, at_utc: str | None = None, reason: str = '',
    ) -> CadCalibrationWizardTransition:
        """Lane C: the campaign's measurements are done — operator or
        observing system reports it; the machine moves to post-checks."""
        return self._apply(WizardEvent(
            kind='campaign_completed', at_utc=at_utc or self._clock(),
            reason=reason or 'campaign measurements completed',
            actor='operator'))

    def cancel(
        self, *, at_utc: str | None = None, reason: str = '',
    ) -> CadCalibrationWizardTransition:
        """Operator abort — first-class from every non-terminal stage."""
        if self._engine is not None and \
                self._engine.stage not in (
                    'completed', 'failed', 'cancelled'):
            try:
                self._engine.cancel()
            except Exception:  # error-boundary: best-effort cancel — an engine-cancel failure is benign: the operator intent is still recorded as run_cancelled below and the engine stage re-derives on next poll (noqa: BLE001)
                pass
        return self._apply(WizardEvent(
            kind='run_cancelled', at_utc=at_utc or self._clock(),
            reason=reason or 'operator cancelled', actor='operator'))

    def notify_device_lost(
        self, *, at_utc: str | None = None, reason: str = '',
    ) -> CadCalibrationWizardTransition:
        return self._apply(WizardEvent(
            kind='device_lost', at_utc=at_utc or self._clock(),
            reason=reason or 'device disconnected mid-run',
            actor='system'))

    def notify_connectivity_restored(
        self, *, at_utc: str | None = None, reason: str = '',
    ) -> CadCalibrationWizardTransition:
        return self._apply(WizardEvent(
            kind='connectivity_restored', at_utc=at_utc or self._clock(),
            reason=reason or 'device connection restored',
            actor='system'))

    def request_retry(
        self, *, at_utc: str | None = None, reason: str = '',
    ) -> CadCalibrationWizardTransition:
        """Explicit operator retry — regresses to the lane's re-entry
        stage so a blocked capture/preflight can be re-run honestly."""
        return self._apply(WizardEvent(
            kind='retry_step', at_utc=at_utc or self._clock(),
            reason=reason or 'operator requested retry',
            actor='operator'))

    # -- the automatic engine ----------------------------------------------

    def _require_state(self) -> CalibrationWizardState:
        state = self.state
        if state is None:
            raise CalibrationWizardError('no run — call a begin_* first')
        return state

    def tick(
        self, *, at_utc: str | None = None,
    ) -> CadCalibrationWizardTransition | None:
        """Execute the current software stage once.

        Returns the sealed transition the stage produced, or ``None``
        when the machine is parked (awaiting a physical confirmation),
        blocked, disconnected, or terminal. ``None`` is an honest answer:
        software has nothing to do right now.
        """
        state = self._require_state()
        if state.current_stage in TERMINAL_WIZARD_STAGES:
            return None
        if state.device_disconnected:
            return None
        # blocked_reason never blocks execution outright: a software
        # stage may legitimately be re-run (the machine permits the same
        # event again and only the new evaluation decides), while
        # run_automatic stops looping on any non-advance outcome.
        stage = state.current_stage
        handler = _SOFTWARE_STEPS.get(stage)
        if handler is None:
            return None  # physical prompt — operator's turn
        return handler(self, at_utc=at_utc or self._clock())

    def run_automatic(
        self, *, at_utc: str | None = None,
        max_steps: int = 32,
    ) -> tuple[CadCalibrationWizardTransition, ...]:
        """Run every pending software stage until the machine must wait.

        Each stage writes exactly one transition; the loop returns them
        all. It never crosses an ``await_*``, blocked, disconnected, or
        terminal stage.
        """
        made: list[CadCalibrationWizardTransition] = []
        for _ in range(max_steps):
            transition = self.tick(at_utc=at_utc)
            if transition is None:
                break
            made.append(transition)
            # Only a clean advance continues the chain — a blocked,
            # failed or same-stage outcome needs the operator.
            if transition.outcome != 'advanced':
                break
        return tuple(made)

    # -- resume -------------------------------------------------------------

    @classmethod
    def resume(
        cls,
        document_id: str,
        backend: AudioIOBackend,
        run: CadCalibrationWizardRun,
        transitions: tuple[CadCalibrationWizardTransition, ...] | list[
            CadCalibrationWizardTransition],
        *,
        stores: WizardStores | None = None,
        clock: Callable[[], str] = _utc_now,
        instrument: CadInstrumentInstance | None = None,
        acceptance_profile: CadSplCheckAcceptanceProfile | None = None,
        check_plan: CadCampaignCheckPlan | None = None,
    ) -> 'CalibrationWizard':
        """Rebuild the driver from the sealed log + stored artifacts.

        Anything a completed stage sealed is reloaded and verified by the
        repositories; a stage that was mid-flight simply has no later
        transition, so the fold lands the run back at it. Nothing is
        reconstructed that was never sealed.
        """
        driver = cls(
            document_id, backend, stores=stores, clock=clock, run=run,
            transitions=tuple(transitions), instrument=instrument,
            acceptance_profile=acceptance_profile, check_plan=check_plan)
        return driver

    def _hydrate_from_log(self) -> None:
        """Reload the artifacts earlier stages sealed (resume support)."""
        if self._run is None:
            return
        state = derive_wizard_state(self._run, tuple(self._transitions))
        for kind, ref in state.evidence.items():
            if kind == _SWEEP_RUN_KIND and self.stores.sweep is not None:
                self._acquisition_run = self.stores.sweep.get_acquisition_run(
                    ref.ref_id)
            elif kind == 'loopback_observation' \
                    and self.stores.interface is not None:
                self._observation = self.stores.interface.get_observation(
                    ref.ref_id)
            elif kind == 'interface_transfer_calibration' \
                    and self.stores.interface is not None:
                self._calibration = self.stores.interface.get_calibration(
                    ref.ref_id)
            elif kind == 'interface_correction_qualification' \
                    and self.stores.interface is not None:
                self._qualification = (
                    self.stores.interface.get_qualification(ref.ref_id))
            elif kind == 'instrument_verification_check' \
                    and self.stores.lifecycle is not None:
                check = self.stores.lifecycle.get_check(ref.ref_id)
                if check is not None:
                    self._checks.append(check)
            elif kind == _STIMULUS_KIND and self.stores.sweep is not None:
                self._stimulus_ref = ref
        if self._profile is None and self.stores.wizard is not None and (
                self._run.acceptance_profile_ref is not None):
            self._profile = self.stores.wizard.get_profile(
                self._run.acceptance_profile_ref.ref_id)
            if self._profile is not None:
                self._profiles[self._profile.profile_id] = self._profile
        if self._plan is None and self.stores.wizard is not None and (
                self._run.check_plan_ref is not None):
            self._plan = self.stores.wizard.get_check_plan(
                self._run.check_plan_ref.ref_id)
        if self._plan is not None and self.stores.wizard is not None:
            for spec in self._plan.required_checks:
                if spec.acceptance_profile_ref is not None:
                    prof = self.stores.wizard.get_profile(
                        spec.acceptance_profile_ref.ref_id)
                    if prof is not None:
                        self._profiles[prof.profile_id] = prof
        if self._instrument is None \
                and self.stores.lifecycle is not None \
                and self._run.instrument_ref is not None:
            self._instrument = self.stores.lifecycle.get_instrument(
                self._run.instrument_ref.ref_id)

    # -- Lane A + B shared: drive the #869 engine ---------------------------

    def _acquisition_request(self) -> AcquisitionRequest:
        run = self._run
        request_kw: dict[str, Any] = {}
        if self._level_policy is not None:
            request_kw['level_policy'] = self._level_policy
        return AcquisitionRequest(
            stimulus=run.stimulus_spec,
            routing=run.routing,
            **request_kw)

    def _exec_preflight(
            self, *, at_utc: str) -> CadCalibrationWizardTransition:
        """Engine configure — devices, routing, rate, geometry."""
        self._engine = MeasurementAcquisitionEngine(self.backend)
        report = self._engine.configure(self._acquisition_request())
        reason = (
            'preflight ok — devices, routing and rate verified'
            if report.ok
            else 'preflight blocked: ' + '; '.join(report.blocked_reasons))
        return self._apply(WizardEvent(
            kind='preflight_evaluated', at_utc=at_utc,
            succeeded=report.ok, reason=reason))

    def _exec_acquire(
            self, *, at_utc: str) -> CadCalibrationWizardTransition:
        """Arm + capture through the engine, then seal the run evidence."""
        engine = self._engine
        if engine is None:
            # Resumed into acquire — the capture may not proceed on a
            # stale device binding; the operator retries from preflight.
            return self._apply(WizardEvent(
                kind='acquisition_recorded', at_utc=at_utc,
                succeeded=False,
                reason='acquire reached without a live engine context — '
                       'retry from preflight'))
        request = self._acquisition_request()
        routing = request.routing
        try:
            engine.arm(ArmConfirmation(
                acknowledged_playback_device_id=routing.playback_device_id,
                acknowledged_playback_channel=routing.playback_channel,
                acknowledged_capture_device_id=routing.capture_device_id,
                acknowledged_capture_channel=routing.capture_channel,
                acknowledged_level_dbfs=request.stimulus.level_dbfs,
            ))
        except ArmBlockedError as exc:
            return self._apply(WizardEvent(
                kind='acquisition_recorded', at_utc=at_utc,
                succeeded=False,
                reason='arm blocked: ' + '; '.join(exc.reasons)))
        result = engine.start()
        capture = result.capture
        completed = (
            engine.stage == 'completed'
            and capture is not None
            and capture.outcome == 'completed'
        )
        evidence: list[AuthorityRef] = []
        if result.stimulus is not None:
            # The stimulus definition is sealed regardless of stores —
            # it is the spec hash every derived artifact pins to.
            definition = build_stimulus_definition(
                document_id=self.document_id, stimulus=result.stimulus,
                created_at_utc=at_utc)
            self._stimulus_definition = definition
            self._stimulus_ref = AuthorityRef(
                kind=_STIMULUS_KIND,
                ref_id=definition.stimulus_definition_id,
                ref_sha256=definition.stimulus_sha256)
            evidence.append(self._stimulus_ref)
        if self.stores.sweep is not None and result.stimulus is not None:
            self.stores.sweep.save_stimulus_definition(
                self._stimulus_definition)
            record = self.stores.sweep.record_run(
                document_id=self.document_id, engine=engine,
                result=result, stimulus_ref=self._stimulus_ref,
                campaign_ref=self._run.campaign_ref,
                notes=(f'calibration wizard lane={self._run.lane}',))
            self._acquisition_run = record
            evidence.append(AuthorityRef(
                kind=_SWEEP_RUN_KIND,
                ref_id=record.acquisition_id,
                ref_sha256=record.acquisition_sha256))
        outcome = capture.outcome if capture is not None else 'no_capture'
        reason = (
            f'acquisition completed — {self._acquisition_run.acquisition_id}'
            if completed and self._acquisition_run is not None
            else f'acquisition outcome={outcome} '
                 f'engine_stage={engine.stage}'
        )
        return self._apply(WizardEvent(
            kind='acquisition_recorded', at_utc=at_utc,
            succeeded=completed, reason=reason,
            evidence_refs=tuple(evidence)))

    # -- Lane A -----------------------------------------------------------

    def _exec_derive_transfer(
            self, *, at_utc: str) -> CadCalibrationWizardTransition:
        """Derive the loopback transfer + seal the raw observation."""
        result = self._engine_result()
        if result is None or result.impulse_response is None:
            return self._apply(WizardEvent(
                kind='transfer_derived', at_utc=at_utc, succeeded=False,
                reason='no derived impulse response available'))
        ir = result.impulse_response
        if ir.status != 'derived' or ir.ir_sha256 is None:
            return self._apply(WizardEvent(
                kind='transfer_derived', at_utc=at_utc, succeeded=False,
                reason=f'transfer derivation {ir.status}: '
                       + '; '.join(ir.notes)))
        run = self._run
        capture = result.capture
        loop_level: float | None = None
        if (capture is not None
                and len(capture.captured_channels) >= 2):
            loop = capture.channel_samples(len(capture.captured_channels) - 1)
            rms = float(np.sqrt(np.mean(loop ** 2))) if len(loop) else 0.0
            loop_level = 20.0 * math.log10(max(rms, 1e-15))
        observation = build_loopback_observation(
            document_id=self.document_id,
            io_path=run.io_path,
            stimulus_ref=self._require_stimulus_ref(),
            capture_ref=(
                _ref(self._acquisition_run, _SWEEP_RUN_KIND,
                     'acquisition_id', 'acquisition_sha256')
                if self._acquisition_run is not None else None),
            observed_level_dbfs=loop_level,
            level_semantics='rms' if loop_level is not None else 'unknown',
            method_version=run.derivation_version,
            observed_at_utc=at_utc,
        )
        self._observation = observation
        if self.stores.interface is not None:
            self.stores.interface.save_observation(observation)
        return self._apply(WizardEvent(
            kind='transfer_derived', at_utc=at_utc, succeeded=True,
            reason=f'loopback transfer derived — ir {ir.ir_sha256[:12]}… '
                   f'({ir.origin_convention})',
            evidence_refs=(
                AuthorityRef(
                    kind='loopback_observation',
                    ref_id=observation.observation_id,
                    ref_sha256=observation.observation_sha256),),
        ))

    def _require_stimulus_ref(self) -> AuthorityRef:
        if self._stimulus_ref is None:
            raise CalibrationWizardError(
                'no stimulus definition is bound to this run — '
                'acquire must seal one first')
        return self._stimulus_ref

    def _engine_result(self):
        if self._engine is not None:
            return self._engine.result
        return None

    def _exec_quality_gate(
            self, *, at_utc: str) -> CadCalibrationWizardTransition:
        result = self._engine_result()
        quality = result.quality if result is not None else None
        if quality is None:
            return self._apply(WizardEvent(
                kind='quality_evaluated', at_utc=at_utc, succeeded=False,
                reason='no quality report — acquisition evidence missing'))
        passed = quality.verdict in ('valid', 'limited')
        reason = (
            f'quality verdict={quality.verdict}'
            + (f' reasons={",".join(quality.reasons)}' if quality.reasons
               else '')
            + (f' warnings={len(quality.warnings)}'
               if quality.warnings else ''))
        evidence: list[AuthorityRef] = []
        if self._acquisition_run is not None:
            evidence.append(_ref(
                self._acquisition_run, _SWEEP_RUN_KIND,
                'acquisition_id', 'acquisition_sha256'))
        return self._apply(WizardEvent(
            kind='quality_evaluated', at_utc=at_utc, succeeded=passed,
            reason=reason, result_tag=quality.verdict,
            evidence_refs=tuple(evidence)))

    def _exec_seal_calibration(
            self, *, at_utc: str) -> CadCalibrationWizardTransition:
        """Seal the combined DAC+ADC loopback calibration (#699)."""
        run = self._run
        result = self._engine_result()
        if self._observation is None or result is None \
                or result.impulse_response is None:
            return self._apply(WizardEvent(
                kind='calibration_sealed', at_utc=at_utc, succeeded=False,
                reason='no derived transfer evidence to calibrate from'))
        ir = result.impulse_response
        timing = result.timing
        spec = run.stimulus_spec
        band = self._measured_band(result, spec)
        curve_ref = (
            _ref(self._acquisition_run, _SWEEP_RUN_KIND,
                 'acquisition_id', 'acquisition_sha256')
            if self._acquisition_run is not None else None)
        # Phase-bearing quantities need a sealed timebase anchor; without
        # one the honest claim is magnitude only.
        qualified = (
            timing is not None and timing.quality == 'qualified'
            and ir.origin_convention == 'loopback_reference'
            and curve_ref is not None)
        phase_semantics = (
            CadPhaseSemantics(
                timebase_ref=curve_ref,
                pure_delay_removed=False,
                unwrapping_convention='loopback_timebase',
                frequency_grid_description=(
                    'linear rfft grid over the derived IR window'),
            ) if qualified else None)
        quantities: tuple[CalibrationQuantity, ...] = (
            ('magnitude_response', 'complex_response', 'latency_delay')
            if qualified else ('magnitude_response',))
        calibration = build_interface_calibration(
            document_id=self.document_id,
            io_path=run.io_path,
            calibration_kind='combined_dac_analog_adc_loopback',
            quantities=quantities,
            frequency_coverage=band,
            phase_semantics=phase_semantics,
            correction_curve_ref=curve_ref,
            calibration_level_dbfs=spec.level_dbfs,
            level_semantics='rms',
            observation_refs=(
                AuthorityRef(
                    kind='loopback_observation',
                    ref_id=self._observation.observation_id,
                    ref_sha256=self._observation.observation_sha256),),
            declared_at_utc=at_utc,
        )
        self._calibration = calibration
        if self.stores.interface is not None:
            self.stores.interface.save_calibration(calibration)
        return self._apply(WizardEvent(
            kind='calibration_sealed', at_utc=at_utc, succeeded=True,
            reason='interface calibration sealed '
                   f'({calibration.calibration_id}) — '
                   'combined_dac_analog_adc_loopback',
            evidence_refs=(
                AuthorityRef(
                    kind='interface_transfer_calibration',
                    ref_id=calibration.calibration_id,
                    ref_sha256=calibration.calibration_sha256),),
        ))

    @staticmethod
    def _measured_band(result, spec) -> CadFrequencyCoverage:
        """Honest coverage: the swept band is measured, usable equals it."""
        low = float(spec.start_frequency_hz)
        high = float(spec.end_frequency_hz)
        fr = (
            result.impulse_response.frequency_response
            if result is not None and result.impulse_response is not None
            else None)
        if fr:
            freqs = [f for f, _ in fr]
            low = max(low, min(freqs))
            high = min(high, max(freqs))
        return CadFrequencyCoverage(
            measured_low_hz=low, measured_high_hz=high,
            usable_low_hz=low, usable_high_hz=high,
            interpolation='none_piecewise_constant',
            out_of_range_policy='reject')

    def _exec_bind_correction(
            self, *, at_utc: str) -> CadCalibrationWizardTransition:
        """Bind the sealed calibration to the exact I/O path — the
        evaluation decides applicability, never the wizard."""
        run = self._run
        if self._calibration is None:
            return self._apply(WizardEvent(
                kind='correction_evaluated', at_utc=at_utc, succeeded=False,
                reason='no calibration record to bind'))
        qualification = evaluate_interface_correction(
            document_id=self.document_id,
            calibration=self._calibration,
            measurement_io_path=run.io_path,
            requested_quantities=tuple(self._calibration.quantities),
            requested_band_low_hz=run.stimulus_spec.start_frequency_hz,
            requested_band_high_hz=run.stimulus_spec.end_frequency_hz,
            evaluated_at_utc=at_utc,
        )
        self._qualification = qualification
        if self.stores.interface is not None:
            self.stores.interface.save_qualification(qualification)
        bound = qualification.state in (
            'correction_applied', 'correction_applied_with_limitations')
        reason = (
            f'correction state={qualification.state}'
            + (f' reasons={"; ".join(qualification.reasons)}'
               if qualification.reasons else ''))
        return self._apply(WizardEvent(
            kind='correction_evaluated', at_utc=at_utc, succeeded=bound,
            reason=reason, result_tag=qualification.state,
            evidence_refs=(_qualification_binding(qualification),)))

    # -- Lane B -----------------------------------------------------------

    def _exec_evaluate_check(
            self, *, at_utc: str) -> CadCalibrationWizardTransition:
        """Measure the captured reference window and seal the check."""
        run = self._run
        result = self._engine_result()
        capture = result.capture if result is not None else None
        if self._profile is None:
            return self._apply(WizardEvent(
                kind='check_evaluated', at_utc=at_utc, succeeded=False,
                reason='no acceptance profile bound'))
        evaluation = evaluate_reference_check(capture, self._profile)
        check = build_verification_check(
            document_id=self.document_id,
            instrument_ref=run.instrument_ref,
            kind=run.check_kind,
            outcome=evaluation.outcome,
            performed_at_utc=at_utc,
            calibrator_ref=run.calibrator_ref,
            campaign_id=(
                run.campaign_ref.ref_id
                if run.campaign_ref is not None else None),
            reference_level_db=self._profile.reference_level_db,
            reference_frequency_hz=self._profile.reference_frequency_hz,
            observed_value=evaluation.observed_value,
            observed_deviation_db=evaluation.observed_deviation_db,
            acceptance_profile=self._profile.name,
        )
        self._checks.append(check)
        if self.stores.lifecycle is not None:
            self.stores.lifecycle.save_check(check)
        evidence: list[AuthorityRef] = [
            AuthorityRef(kind='instrument_verification_check',
                         ref_id=check.check_id,
                         ref_sha256=check.check_sha256)]
        if self._acquisition_run is not None:
            evidence.append(_ref(
                self._acquisition_run, _SWEEP_RUN_KIND,
                'acquisition_id', 'acquisition_sha256'))
        return self._apply(WizardEvent(
            kind='check_evaluated', at_utc=at_utc, succeeded=True,
            reason=f'check outcome={evaluation.outcome} — '
                   + '; '.join(evaluation.reasons),
            result_tag=evaluation.outcome,
            evidence_refs=tuple(evidence)))

    def _exec_update_fitness(
            self, *, at_utc: str) -> CadCalibrationWizardTransition:
        """Re-evaluate instrument fitness with the new check on record."""
        run = self._run
        instrument = self._instrument
        if instrument is None and self.stores.lifecycle is not None:
            instrument = self.stores.lifecycle.get_instrument(
                run.instrument_ref.ref_id)
        if instrument is None:
            return self._apply(WizardEvent(
                kind='fitness_evaluated', at_utc=at_utc, succeeded=False,
                reason='instrument record unresolvable — '
                       'fitness stays unclaimed'))
        calibrations: list = []
        policies: list = []
        service_events: list = []
        checks: list = list(self._checks)
        if self.stores.lifecycle is not None:
            seen = {c.check_id for c in self._checks}
            checks.extend(
                c for c in self.stores.lifecycle.list_checks(
                    self.document_id)
                if c.check_id not in seen)
            calibrations = list(self.stores.lifecycle.list_calibrations(
                self.document_id))
            policies = list(self.stores.lifecycle.list_policies(
                self.document_id))
            service_events = list(
                self.stores.lifecycle.list_service_events(
                    self.document_id))

        def _calibrator_fitness(instrument_id: str, when: str):
            if self.stores.lifecycle is None:
                return 'unknown'
            other = self.stores.lifecycle.get_instrument(instrument_id)
            if other is None:
                return 'unknown'
            sub = evaluate_instrument_fitness(
                document_id=self.document_id, instrument=other,
                calibrations=calibrations, checks=checks,
                service_events=service_events, policies=policies,
                at_utc=when, evaluated_at_utc=when)
            return sub.state

        assessment = evaluate_instrument_fitness(
            document_id=self.document_id, instrument=instrument,
            calibrations=calibrations, checks=checks,
            service_events=service_events, policies=policies,
            at_utc=at_utc, calibrator_fitness=_calibrator_fitness,
            evaluated_at_utc=at_utc)
        if self.stores.lifecycle is not None:
            self.stores.lifecycle.save_assessment(assessment)
        return self._apply(WizardEvent(
            kind='fitness_evaluated', at_utc=at_utc, succeeded=True,
            reason=f'fitness={assessment.state}',
            result_tag=assessment.state,
            evidence_refs=(_assessment_binding(assessment),)))

    # -- Lane C -----------------------------------------------------------

    def _perform_check_captures(
        self, *, check_kind: str, at_utc: str,
    ) -> tuple[bool, str, tuple[AuthorityRef, ...], str]:
        """Record every required check of ``check_kind`` not yet sealed.

        Each check drives the engine over the run's record window and is
        evaluated against its pinned acceptance profile (a spec without a
        pinned profile is honestly recorded ``inconclusive``). Idempotent:
        an instrument+campaign check already sealed is re-referenced, not
        re-run — resume after a mid-stage crash never duplicates evidence.
        """
        run = self._run
        plan = self._plan
        if plan is None:
            return False, 'no check plan bound', (), ''
        evidence: list[AuthorityRef] = []
        campaign_id = plan.campaign_ref.ref_id
        detail = ''
        for spec in plan.specs_for(check_kind):
            existing = [
                c for c in self._checks
                if c.instrument_ref.ref_id == spec.instrument_ref.ref_id
                and c.kind == spec.check_kind
                and c.campaign_id == campaign_id]
            if existing:
                evidence.append(AuthorityRef(
                    kind='instrument_verification_check',
                    ref_id=existing[-1].check_id,
                    ref_sha256=existing[-1].check_sha256))
                continue
            profile = (
                self._profiles.get(spec.acceptance_profile_ref.ref_id)
                if spec.acceptance_profile_ref is not None else None)
            spec_stimulus = run.stimulus_spec
            if spec.capture_duration_s is not None:
                spec_stimulus = spec_stimulus.model_copy(
                    update={'duration_s': spec.capture_duration_s})
            engine = MeasurementAcquisitionEngine(self.backend)
            request = AcquisitionRequest(
                stimulus=spec_stimulus, routing=run.routing)
            report = engine.configure(request)
            if not report.ok:
                return False, (
                    f'{spec.instrument_ref.ref_id}: preflight blocked — '
                    + '; '.join(report.blocked_reasons)), tuple(evidence), ''
            routing = request.routing
            try:
                engine.arm(ArmConfirmation(
                    acknowledged_playback_device_id=(
                        routing.playback_device_id),
                    acknowledged_playback_channel=routing.playback_channel,
                    acknowledged_capture_device_id=(
                        routing.capture_device_id),
                    acknowledged_capture_channel=routing.capture_channel,
                    acknowledged_level_dbfs=request.stimulus.level_dbfs))
            except ArmBlockedError as exc:
                return False, (
                    f'{spec.instrument_ref.ref_id}: arm blocked — '
                    + '; '.join(exc.reasons)), tuple(evidence), ''
            result = engine.start()
            capture = result.capture
            if (engine.stage != 'completed' or capture is None
                    or capture.outcome != 'completed'):
                outcome = (
                    capture.outcome if capture is not None
                    else 'no_capture')
                return False, (
                    f'{spec.instrument_ref.ref_id}: capture {outcome}'), \
                    tuple(evidence), ''
            if profile is not None:
                evaluation = evaluate_reference_check(capture, profile)
                observed = evaluation.observed_value
                deviation = evaluation.observed_deviation_db
                outcome: VerificationOutcome = evaluation.outcome
                profile_name = profile.name
                ref_level = profile.reference_level_db
                ref_freq = profile.reference_frequency_hz
                detail = '; '.join(evaluation.reasons)
            else:
                measured = measure_reference_tone(capture)
                observed = measured.observed_level_dbfs
                deviation = None
                outcome = 'inconclusive'
                profile_name = None
                ref_level = ref_freq = None
                detail = 'no acceptance profile pinned — unverifiable'
            check = build_verification_check(
                document_id=self.document_id,
                instrument_ref=spec.instrument_ref,
                kind=spec.check_kind,
                outcome=outcome,
                performed_at_utc=at_utc,
                campaign_id=campaign_id,
                reference_level_db=ref_level,
                reference_frequency_hz=ref_freq,
                observed_value=observed,
                observed_deviation_db=deviation,
                acceptance_profile=profile_name,
            )
            self._checks.append(check)
            if self.stores.lifecycle is not None:
                self.stores.lifecycle.save_check(check)
            evidence.append(AuthorityRef(
                kind='instrument_verification_check',
                ref_id=check.check_id,
                ref_sha256=check.check_sha256))
        return True, (
            f'{len(evidence)} {check_kind} check(s) on record'), \
            tuple(evidence), detail

    def _exec_pre_check(
            self, *, at_utc: str) -> CadCalibrationWizardTransition:
        ok, reason, evidence, _ = self._perform_check_captures(
            check_kind='pre_use_field_check', at_utc=at_utc)
        return self._apply(WizardEvent(
            kind='pre_check_recorded', at_utc=at_utc, succeeded=ok,
            reason=reason, evidence_refs=evidence))

    def _exec_post_check(
            self, *, at_utc: str) -> CadCalibrationWizardTransition:
        ok, reason, evidence, _ = self._perform_check_captures(
            check_kind='post_use_field_check', at_utc=at_utc)
        if not ok:
            return self._apply(WizardEvent(
                kind='post_check_recorded', at_utc=at_utc, succeeded=False,
                reason=reason, evidence_refs=evidence))
        plan = self._plan
        gate = evaluate_campaign_check_gate(plan, self._checks)
        # A blocked-policy failure ends the run failed — the gate verdict
        # is the run's honest answer, recorded for dependent evidence.
        succeeded = not (plan.blocked_on_failure
                         and gate.verdict == 'checks_failed')
        reason = f'{reason}; gate={gate.verdict}'
        if gate.reasons:
            reason += ' — ' + '; '.join(gate.reasons)
        return self._apply(WizardEvent(
            kind='post_check_recorded', at_utc=at_utc, succeeded=succeeded,
            reason=reason, result_tag=gate.verdict,
            evidence_refs=evidence))


#: Stages the wizard executes itself — everything else waits for the
#: operator's physical confirmation (or is blocked/terminal).
_SOFTWARE_STEPS: dict[
    CalibrationWizardStage,
    Callable[['CalibrationWizard'], CadCalibrationWizardTransition],
] = {
    'preflight': CalibrationWizard._exec_preflight,
    'acquire': CalibrationWizard._exec_acquire,
    'derive_transfer': CalibrationWizard._exec_derive_transfer,
    'quality_gate': CalibrationWizard._exec_quality_gate,
    'seal_calibration': CalibrationWizard._exec_seal_calibration,
    'bind_correction': CalibrationWizard._exec_bind_correction,
    'evaluate_check': CalibrationWizard._exec_evaluate_check,
    'update_fitness': CalibrationWizard._exec_update_fitness,
    'pre_check': CalibrationWizard._exec_pre_check,
    'post_check': CalibrationWizard._exec_post_check,
}


# ---------------------------------------------------------------------------
# JA vocabulary labels — operator-facing text lives in one registry so the
# UI never invents its own wording for the state machine.
# ---------------------------------------------------------------------------

CALIBRATION_WIZARD_LANE_LABELS: dict[str, str] = {
    'interface_loopback': 'インターフェース ループバック校正',
    'spl_reference_check': 'マイク/SPLリファレンス チェック',
    'campaign_checks': 'キャンペーン前後チェック',
}

CALIBRATION_WIZARD_STAGE_LABELS: dict[str, str] = {
    'await_loopback': 'ループバックケーブルの接続を確認',
    'await_calibrator': '校正器・基準器のセットアップを確認',
    'await_calibrator_pre': '事前チェック用校正器のセットアップを確認',
    'await_calibrator_post': '事後チェック用校正器のセットアップを確認',
    'await_campaign': 'キャンペーン測定の完了を待機',
    'preflight': '事前チェック',
    'acquire': '掃引の再生・録音',
    'derive_transfer': '伝達特性の導出',
    'quality_gate': '品質ゲート',
    'seal_calibration': '校正レコードの封緘',
    'bind_correction': 'パスへの校正バインド',
    'evaluate_check': 'チェック評価',
    'update_fitness': '機器適合性の更新',
    'pre_check': '事前チェックの記録',
    'post_check': '事後チェックの記録',
    'completed': '完了',
    'cancelled': '中止',
    'failed': '失敗',
}

CALIBRATION_WIZARD_INSTRUCTION_LABELS: dict[str, str] = {
    'connect_loopback_cable': (
        '出力から入力へループバックケーブルを接続してください。'
        '接続したら「接続を確認」を押します。'),
    'position_calibrator': (
        '校正器/基準ソースを対象機器に取り付け、安定した状態で'
        '「セットアップ完了」を押してください。'),
    'perform_campaign_measurements': (
        'キャンペーンの実測を実施してください。完了したら'
        '「キャンペーン完了を記録」を押します。'),
    'restore_device_connection': (
        'デバイスの接続が失われました。接続を復帰してから'
        '「復帰を記録」を押してください。'),
    'resolve_blocked_step': (
        'ステップがブロックされています。理由を確認し、修正後に'
        '「再試行」を押してください。'),
}


__all__ = [
    'CALIBRATION_WIZARD_INSTRUCTION_LABELS',
    'CALIBRATION_WIZARD_LANE_LABELS',
    'CALIBRATION_WIZARD_STAGE_LABELS',
    'CALIBRATION_WIZARD_VERSION',
    'CHECK_EVALUATION_VERSION',
    'CadCalibrationWizardRun',
    'CadCalibrationWizardTransition',
    'CadCampaignCheckPlan',
    'CadSplCheckAcceptanceProfile',
    'CalibrationWizard',
    'CalibrationWizardError',
    'CalibrationWizardLane',
    'CalibrationWizardStage',
    'CalibrationWizardState',
    'CampaignCheckGateResult',
    'CampaignGateVerdict',
    'LANE_STAGE_ORDER',
    'ReferenceCheckEvaluation',
    'ReferenceToneMeasurement',
    'RequiredCheckSpec',
    'TERMINAL_WIZARD_STAGES',
    'WizardActor',
    'WizardEvent',
    'WizardEventKind',
    'WizardPhysicalInstruction',
    'WizardStores',
    'WizardTransitionDecision',
    'WizardTransitionOutcome',
    'WizardTransitionRejection',
    'campaign_plan_binding',
    'derive_wizard_state',
    'evaluate_campaign_check_gate',
    'evaluate_reference_check',
    'measure_reference_tone',
    'next_permitted_events',
    'pending_physical_instruction',
    'spl_profile_binding',
    'wizard_run_binding',
    'wizard_transition',
]
