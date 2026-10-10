"""Calibration wizard sealed records (#877, moved to domain layer under #807).

The guided-calibration wizard's persisted authority records — the lane/stage
vocabulary, the sealed run, its append-only transition log, the SPL reference
acceptance profile and the campaign check plan — are domain models: the
persistence layer stores and reloads them, services drive the state machine
that produces them. They live at domain rank so
``calibration.persistence.cad_calibration_wizard_repository`` can hold them
without importing the services layer (ui -> services -> persistence ->
domain).

Everything here is Qt-free and persistence-free: sealing hashes come from
``canonical_json`` (kernel), refs are ``cad_authority_registry.AuthorityRef``
(domain), and lane pin types are flat domain acquisition models.
"""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...cad_authority_registry import AuthorityRef
from ...canonical_json import canonical_sha256 as _hash
from ...cad_delegated_provider import _seal, _require_iso8601, _require_refs
from .cad_calibration_lifecycle import VerificationCheckKind
from ...cad_interface_loopback import CadInterfaceIoPath
from ...cad_sweep_acquisition import (
    ChannelRouting,
    SweepStimulusSpec,
    SWEEP_ACQUISITION_ENGINE_VERSION,
    IR_DERIVATION_VERSION,
)


CALIBRATION_WIZARD_VERSION = 'htdt-calwiz-1'
CHECK_EVALUATION_VERSION = 'htdt-calwiz-eval-1'

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


