"""Closed-loop commissioning orchestrator (issue #868).

The issue's commissioning loop is machine-controlled, human-authorized
and machine-verified:

    PRECHECK -> BASELINE_MEASUREMENT -> INGEST/QUALITY_GATE -> ANALYZE
    -> OPTIMIZE -> OPERATOR_APPROVAL -> COMPILE_FOR_TARGET -> DEPLOY
    -> READBACK_VERIFY -> POST_MEASUREMENT -> BEFORE/AFTER
    -> ACCEPTANCE_DECISION

HTDT owns the state machine and the evidence chain; measurement runs
through the delegated-provider contract (#838 — REW et al. stay
external specialists), device mutation runs through the
:class:`CalibrationDeviceAdapter` contract (#609/#865) and the #806
deployment authority. Nothing in this module hardcodes a provider or
device: the run pins a sealed provider manifest and an exact device
binding, and every stage consumes only sealed evidence.

Fail-closed rules enforced here:

* the stage-transition function is pure, deterministic and total —
  ``stage_transition(state, event)`` returns a decision or an explicit
  rejection reason for every input; there are no implicit transitions;
* a device/provider disconnect blocks the run until an explicit
  ``connectivity_restored`` event — a disconnect can never silently
  advance anything;
* deploy-ack is below read-back verification: ``deploy`` only produces
  ``deploy_acked``/``deployment_unverified`` evidence, and only a
  machine read-back produces ``config_readback_matched``;
* operator authorization is an explicit sealed record bound to the
  exact materialization hash (deploy) or deployment ref (rollback) —
  an authorization for another payload cannot be replayed;
* before/after pins the exact read-back config hash, and the
  acceptance verdict fails closed on stale or missing evidence;
* scene, provider manifest, device binding, routing profile and
  observed-config changes invalidate downstream evidence
  deterministically via :func:`evaluate_invalidation`;
* every record is sealed and append-only — re-running a stage after a
  state change produces new evidence, never an overwrite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .cad_calibration import CadCalibrationExportSnapshot, CadCalibrationPlan
from .cad_calibration_deployment import (
    CalibrationDeployment,
    DeploymentCapabilityDeclaration,
    DeploymentTargetClass,
    EffectivenessMetricDelta,
    EffectivenessVerdict,
    MetricOutcome,
    _require_refs,
    _seal,
    derive_deployment_state,
    evaluate_deployment_gate,
)
from .cad_camilladsp_deploy import (
    CamillaDSPCalibrationAdapter,
    CamillaDSPDeploymentSession,
    CamillaDSPRollbackEvidence,
    config_sha256,
    _parse_config_document,
)
from .cad_deployment_pipeline import (
    DeploymentPathCandidate,
    DeploymentPathVerdict,
    rank_deployment_paths,
    select_strongest_deployment_path,
    select_strongest_production_path,
)
from .cad_delegated_provider import (
    DelegatedCapability,
    DelegatedProviderManifest,
    ProviderAcquisitionRecord,
    build_provider_acquisition,
    evaluate_provider_gate,
    manifest_ref,
)
from .cad_device_adapter import (
    AdapterCapabilityReport,
    AdapterDeviceBinding,
    CalibrationDeviceAdapter,
    DeviceApplyAck,
    MaterializedCalibrationSettings,
    build_observation,
    validate_materialization_result,
)
from .cad_measurement_quality import (
    CadMeasurementQualityReport,
    MeasurementCapabilityClaim,
)
from .canonical_json import canonical_sha256 as _hash

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


# ----------------------------------------------------------------------
# vocabulary

CommissioningStage = Literal[
    'precheck',
    'baseline_measurement',
    'ingest_quality_gate',
    'analyze',
    'optimize',
    'operator_approval',
    'compile_for_target',
    'deploy',
    'readback_verify',
    'post_measurement',
    'before_after',
    'acceptance_decision',
    'completed',
    'rolled_back',
    'aborted',
]

#: The forward ladder — the 12 stages the issue lists, in order.
COMMISSIONING_STAGE_ORDER: tuple[CommissioningStage, ...] = (
    'precheck',
    'baseline_measurement',
    'ingest_quality_gate',
    'analyze',
    'optimize',
    'operator_approval',
    'compile_for_target',
    'deploy',
    'readback_verify',
    'post_measurement',
    'before_after',
    'acceptance_decision',
)

TERMINAL_STAGES: frozenset[CommissioningStage] = frozenset(
    ('completed', 'rolled_back', 'aborted'))

#: Stages at or after which a device mutation is live and rollback is
#: meaningful — the device-mutating boundary opens at DEPLOY.
_DEVICE_MUTATING_STAGES: frozenset[CommissioningStage] = frozenset(
    COMMISSIONING_STAGE_ORDER[
        COMMISSIONING_STAGE_ORDER.index('deploy'):])

#: Public name for the UI layer (private constant kept for the machine).
COMMISSIONING_DEVICE_MUTATING_STAGES = _DEVICE_MUTATING_STAGES

CommissioningEventKind = Literal[
    'run_created',
    'precheck_evaluated',
    'baseline_acquisition_recorded',
    'quality_gate_evaluated',
    'analysis_bound',
    'optimization_committed',
    'operator_authorized',
    'compile_completed',
    'deploy_acked',
    'readback_evaluated',
    'runtime_observed',
    'post_measurement_recorded',
    'before_after_evaluated',
    'acceptance_decided',
    'rollback_completed',
    'provider_disconnected',
    'device_disconnected',
    'connectivity_restored',
    'authority_invalidated',
    'abort',
]

CommissioningTransitionOutcome = Literal[
    'advanced',
    'rejected',
    'blocked',
    'completed',
    'rolled_back',
    'aborted',
    'invalidated',
    'informational',
]

CommissioningActor = Literal['machine', 'operator', 'system']

#: How far up the evidence ladder the run's newest evidence supports.
#: ``deploy_acked`` is strictly weaker than ``config_readback_matched``
#: — an ack is never verification.
CommissioningEvidenceStrength = Literal[
    'no_evidence',
    'provider_observed',
    'quality_gated',
    'analyzed',
    'optimized',
    'authorized',
    'compiled',
    'deploy_acked',
    'config_readback_matched',
    'runtime_observed',
    'post_measurement_verified',
    'accepted',
    'rejected',
    'indeterminate',
    'rolled_back',
    'aborted',
]

CommissioningRollbackOutcome = Literal[
    'restored_verified',
    'restored_unverified',
    'failed',
    'not_attempted',
]

CommissioningAcceptanceOutcome = Literal[
    'accepted',
    'rejected',
    'indeterminate',
]

CriterionExpectation = Literal['improvement', 'no_regression']

CriterionOutcome = Literal['satisfied', 'failed', 'indeterminate']


# ----------------------------------------------------------------------
# embedded value objects


class CommissioningAcceptanceCriterion(BaseModel):
    """One acceptance rule the operator configured on the run.

    ``expected`` names the required direction on the named before/after
    metric; ``required`` criteria that are missing or indeterminate keep
    the verdict from ever being ``accepted``.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    metric_id: str = Field(min_length=1)
    expected: CriterionExpectation
    required: bool = True
    note: str | None = None


class CommissioningCriterionResult(BaseModel):
    """How one acceptance criterion evaluated on the sealed comparison."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    metric_id: str = Field(min_length=1)
    expected: CriterionExpectation
    outcome: CriterionOutcome
    observed: MetricOutcome | Literal['missing']
    reason: str = Field(min_length=1)


# ----------------------------------------------------------------------
# sealed records


class CommissioningOrchestrationRun(BaseModel):
    """One commissioning run bound to exact pins (cor-).

    The run pins the scene revision + content hash, the delegated
    provider manifest, the measurement capability the loop exercises,
    the exact device binding, the routing profile hash, and the
    operator-configured acceptance criteria. Every later stage consumes
    evidence against these pins — a changed pin is a different
    authority context and invalidates downstream evidence.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    run_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=_SHA256_PATTERN)
    provider_manifest_ref: AuthorityRef
    measurement_capability: DelegatedCapability = 'frequency_response'
    campaign_ref: AuthorityRef | None = None
    device_binding_id: str = Field(min_length=1)
    device_binding_sha256: str = Field(pattern=_SHA256_PATTERN)
    routing_profile_id: str | None = None
    routing_profile_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN)
    #: Measurement-capability claims the quality gate must ALLOW.
    required_capability_claims: tuple[MeasurementCapabilityClaim, ...] = (
        'magnitude_response',)
    acceptance_criteria: tuple[CommissioningAcceptanceCriterion, ...] = ()
    target_profile_id: str | None = None
    created_at_utc: str = Field(min_length=1)
    created_by: str = Field(min_length=1)
    notes: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'CommissioningOrchestrationRun':
        _require_refs(self.provider_manifest_ref, self.campaign_ref)
        if self.provider_manifest_ref.kind != 'delegated_provider_manifest':
            raise ValueError(
                "provider_manifest_ref kind must be "
                "'delegated_provider_manifest'")
        if self.campaign_ref is not None and self.campaign_ref.kind \
                not in ('campaign_preregistration', 'measurement_campaign'):
            raise ValueError(
                "campaign_ref kind must be 'campaign_preregistration' "
                "or 'measurement_campaign'")
        if (self.routing_profile_id is None) != (
                self.routing_profile_sha256 is None):
            raise ValueError(
                'routing_profile_id and routing_profile_sha256 must be '
                'pinned together')
        metric_ids = [c.metric_id for c in self.acceptance_criteria]
        if len(metric_ids) != len(set(metric_ids)):
            raise ValueError('acceptance criteria metric ids must be unique')
        if self.run_sha256 != _hash(self.identity_payload()):
            raise ValueError(
                'CommissioningOrchestrationRun hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'run_id', 'run_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'CommissioningOrchestrationRun':
        return _seal(cls, payload, 'run_id', 'run_sha256', 'cor')


class CommissioningStageTransition(BaseModel):
    """One sealed state-machine transition (cot-).

    Every attempted event — advance, block, reject, invalidate,
    rollback — is recorded with the stage it started from, where it
    landed, who/what emitted it, an explicit reason and the evidence
    refs it produced or consumed. Rejected attempts are kept too: the
    log is the audit.
    """

    model_config = ConfigDict(frozen=True)

    transition_id: str
    transition_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    run_ref: AuthorityRef
    seq: int = Field(ge=0)
    event_kind: CommissioningEventKind
    outcome: CommissioningTransitionOutcome
    actor: CommissioningActor = 'machine'
    from_stage: CommissioningStage | None = None
    to_stage: CommissioningStage
    #: For evaluated events: whether the underlying check passed. None =
    #: not an evaluated event.
    event_succeeded: bool | None = None
    reason: str = Field(min_length=1)
    evidence_refs: tuple[AuthorityRef, ...] = ()
    recorded_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def _validate(self) -> 'CommissioningStageTransition':
        _require_refs(self.run_ref)
        if self.run_ref.kind != 'commissioning_orchestration_run':
            raise ValueError(
                "run_ref kind must be 'commissioning_orchestration_run'")
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
        if self.outcome == 'completed' and self.to_stage != 'completed':
            raise ValueError('completed transitions must land on completed')
        if self.outcome == 'rolled_back' \
                and self.to_stage != 'rolled_back':
            raise ValueError('rolled_back transitions must land on rolled_back')
        if self.outcome == 'aborted' and self.to_stage != 'aborted':
            raise ValueError('aborted transitions must land on aborted')
        if self.outcome == 'invalidated' \
                and self.to_stage == self.from_stage:
            raise ValueError('invalidated transitions must regress stage')
        if self.transition_sha256 != _hash(self.identity_payload()):
            raise ValueError('CommissioningStageTransition hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'transition_id', 'transition_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'CommissioningStageTransition':
        return _seal(
            cls, payload, 'transition_id', 'transition_sha256', 'cot')


class CommissioningOperatorAuthorization(BaseModel):
    """Explicit operator authorization for one device mutation (coa-).

    One-shot and exact: a deploy authorization pins the semantic hash
    of the approved calibration candidate (the plan the operator saw);
    the deploy step then verifies the compiled materialization descends
    from exactly that candidate before any device mutation. A rollback
    authorization pins the deployment it restores. Authorizing a
    different payload is a different sealed record — nothing can be
    replayed.
    """

    model_config = ConfigDict(frozen=True)

    authorization_id: str
    authorization_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    run_ref: AuthorityRef
    scope: Literal['deploy_apply', 'rollback_apply']
    #: Exact approved calibration candidate for ``deploy_apply``
    #: (``CadCalibrationPlan.plan_semantic_sha256``).
    candidate_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN)
    deployment_ref: AuthorityRef | None = None
    operator_id: str = Field(min_length=1)
    authorized_at_utc: str = Field(min_length=1)
    expires_at_utc: str | None = None
    note: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'CommissioningOperatorAuthorization':
        _require_refs(self.run_ref, self.deployment_ref)
        if self.run_ref.kind != 'commissioning_orchestration_run':
            raise ValueError(
                "run_ref kind must be 'commissioning_orchestration_run'")
        if self.scope == 'deploy_apply':
            if self.candidate_sha256 is None:
                raise ValueError(
                    'deploy_apply authorization pins a calibration '
                    'candidate hash')
            if self.deployment_ref is not None:
                raise ValueError(
                    'deploy_apply authorization must not pin a '
                    'deployment ref')
        if self.scope == 'rollback_apply':
            if self.deployment_ref is None:
                raise ValueError(
                    'rollback_apply authorization pins a deployment ref')
            if self.deployment_ref.kind != 'calibration_deployment':
                raise ValueError(
                    "rollback deployment_ref kind must be "
                    "'calibration_deployment'")
            if self.candidate_sha256 is not None:
                raise ValueError(
                    'rollback_apply authorization must not pin a '
                    'candidate hash')
        if self.expires_at_utc is not None \
                and self.expires_at_utc <= self.authorized_at_utc:
            raise ValueError('expiry must be after authorized_at_utc')
        if self.authorization_sha256 != _hash(self.identity_payload()):
            raise ValueError(
                'CommissioningOperatorAuthorization hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'authorization_id', 'authorization_sha256'})

    @classmethod
    def create(
        cls, **payload: Any,
    ) -> 'CommissioningOperatorAuthorization':
        return _seal(
            cls, payload, 'authorization_id', 'authorization_sha256',
            'coa')


class CommissioningRollback(BaseModel):
    """Rollback record for one commissioning run (crl-).

    Binds the run to the deployment that was rolled back, the operator
    authorization that permitted it, and the adapter-level evidence —
    ``restored_verified`` is only claimable when the adapter produced
    machine read-back evidence of the restored state.
    """

    model_config = ConfigDict(frozen=True)

    rollback_id: str
    rollback_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    run_ref: AuthorityRef
    deployment_ref: AuthorityRef
    authorization_ref: AuthorityRef
    adapter_evidence_ref: AuthorityRef | None = None
    outcome: CommissioningRollbackOutcome
    requested_at_utc: str = Field(min_length=1)
    completed_at_utc: str | None = None
    reason: str = Field(min_length=1)
    error_detail: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'CommissioningRollback':
        _require_refs(
            self.run_ref,
            self.deployment_ref,
            self.authorization_ref,
            self.adapter_evidence_ref,
        )
        if self.run_ref.kind != 'commissioning_orchestration_run':
            raise ValueError(
                "run_ref kind must be 'commissioning_orchestration_run'")
        if self.deployment_ref.kind != 'calibration_deployment':
            raise ValueError(
                "deployment_ref kind must be 'calibration_deployment'")
        if self.authorization_ref.kind \
                != 'commissioning_operator_authorization':
            raise ValueError(
                "authorization_ref kind must be "
                "'commissioning_operator_authorization'")
        if self.adapter_evidence_ref is not None \
                and self.adapter_evidence_ref.kind \
                != 'camilladsp_rollback_evidence':
            raise ValueError(
                "adapter_evidence_ref kind must be "
                "'camilladsp_rollback_evidence'")
        if self.outcome == 'restored_verified' \
                and self.adapter_evidence_ref is None:
            raise ValueError(
                'restored_verified requires adapter rollback evidence')
        if self.outcome == 'failed' and not self.error_detail:
            raise ValueError('failed rollbacks record the error detail')
        if self.rollback_sha256 != _hash(self.identity_payload()):
            raise ValueError('CommissioningRollback hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'rollback_id', 'rollback_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'CommissioningRollback':
        return _seal(
            cls, payload, 'rollback_id', 'rollback_sha256', 'crl')


class CommissioningBeforeAfter(BaseModel):
    """Before/after comparison bound to the exact read-back config (cba-).

    The comparison is bound to the deployed state the machine read back
    (``readback_config_sha256``) plus the accepted baseline and
    post-deploy measurement refs — never to the file export or the
    operator's intent.
    """

    model_config = ConfigDict(frozen=True)

    comparison_id: str
    comparison_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    run_ref: AuthorityRef
    deployment_ref: AuthorityRef
    readback_config_sha256: str = Field(pattern=_SHA256_PATTERN)
    baseline_evidence_refs: tuple[AuthorityRef, ...] = Field(
        min_length=1)
    post_measurement_refs: tuple[AuthorityRef, ...] = ()
    deltas: tuple[EffectivenessMetricDelta, ...] = ()
    verdict: EffectivenessVerdict = 'unmeasured'
    evaluated_at_utc: str = Field(min_length=1)
    evaluator_note: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'CommissioningBeforeAfter':
        _require_refs(
            self.run_ref,
            self.deployment_ref,
            *self.baseline_evidence_refs,
            *self.post_measurement_refs,
        )
        if self.run_ref.kind != 'commissioning_orchestration_run':
            raise ValueError(
                "run_ref kind must be 'commissioning_orchestration_run'")
        if self.deployment_ref.kind != 'calibration_deployment':
            raise ValueError(
                "deployment_ref kind must be 'calibration_deployment'")
        if self.verdict == 'improvement_verified':
            if not self.post_measurement_refs:
                raise ValueError(
                    'improvement_verified requires post measurements')
            if not any(d.outcome == 'improved' for d in self.deltas):
                raise ValueError(
                    'improvement_verified requires an improved delta')
            if any(d.outcome == 'regressed' for d in self.deltas):
                raise ValueError(
                    'improvement_verified cannot include regressed '
                    'deltas')
        if self.verdict == 'regression_observed' \
                and not any(d.outcome == 'regressed' for d in self.deltas):
            raise ValueError(
                'regression_observed requires a regressed delta')
        if self.verdict == 'unmeasured' and self.post_measurement_refs:
            raise ValueError('unmeasured cannot pin post measurements')
        if self.comparison_sha256 != _hash(self.identity_payload()):
            raise ValueError('CommissioningBeforeAfter hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'comparison_id', 'comparison_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'CommissioningBeforeAfter':
        return _seal(
            cls, payload, 'comparison_id', 'comparison_sha256', 'cba')


class CommissioningAcceptanceVerdict(BaseModel):
    """Final acceptance decision for a run (cav-).

    ``accepted`` requires every required criterion satisfied and no
    stale evidence; ``rejected`` requires at least one criterion with
    concrete regression evidence; everything else — stale, missing or
    unclear evidence — is ``indeterminate``, never a success.
    """

    model_config = ConfigDict(frozen=True)

    verdict_id: str
    verdict_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    run_ref: AuthorityRef
    before_after_ref: AuthorityRef
    criteria_results: tuple[CommissioningCriterionResult, ...] = ()
    stale_evidence: bool = False
    verdict: CommissioningAcceptanceOutcome
    decided_at_utc: str = Field(min_length=1)
    evaluator_note: str | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'CommissioningAcceptanceVerdict':
        _require_refs(self.run_ref, self.before_after_ref)
        if self.run_ref.kind != 'commissioning_orchestration_run':
            raise ValueError(
                "run_ref kind must be 'commissioning_orchestration_run'")
        if self.before_after_ref.kind != 'commissioning_before_after':
            raise ValueError(
                "before_after_ref kind must be "
                "'commissioning_before_after'")
        if self.verdict == 'accepted':
            if not self.criteria_results:
                raise ValueError('accepted requires evaluated criteria')
            if any(r.outcome != 'satisfied'
                    for r in self.criteria_results):
                raise ValueError(
                    'accepted requires every criterion satisfied')
            if self.stale_evidence:
                raise ValueError('accepted cannot hold on stale evidence')
        if self.verdict == 'rejected' and not any(
                r.outcome == 'failed' for r in self.criteria_results):
            raise ValueError('rejected requires a failed criterion')
        if self.verdict_sha256 != _hash(self.identity_payload()):
            raise ValueError(
                'CommissioningAcceptanceVerdict hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'verdict_id', 'verdict_sha256'})

    @classmethod
    def create(cls, **payload: Any) -> 'CommissioningAcceptanceVerdict':
        return _seal(
            cls, payload, 'verdict_id', 'verdict_sha256', 'cav')


# ----------------------------------------------------------------------
# events + the pure transition machine


class CommissioningEvent(BaseModel):
    """One machine/operator/system fact offered to the state machine.

    ``succeeded`` carries the evaluated outcome for evidence-producing
    events (precheck, quality gate, read-back, …); ``False`` records a
    legitimate attempt that could not advance and lands as ``blocked``.
    ``target_stage`` is only meaningful for ``authority_invalidated``
    (the deterministic regression target from
    :func:`evaluate_invalidation`).
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: CommissioningEventKind
    at_utc: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    actor: CommissioningActor = 'machine'
    succeeded: bool | None = None
    evidence_refs: tuple[AuthorityRef, ...] = ()
    target_stage: CommissioningStage | None = None

    @model_validator(mode='after')
    def _validate(self) -> 'CommissioningEvent':
        if self.kind == 'authority_invalidated':
            if self.target_stage is None:
                raise ValueError(
                    'authority_invalidated requires target_stage')
        elif self.target_stage is not None:
            raise ValueError(
                'target_stage is only valid for authority_invalidated')
        _require_refs(*self.evidence_refs)
        return self


@dataclass(frozen=True)
class CommissioningRunState:
    """The folded state of one run — pure derivation, no store reads."""

    run_ref: AuthorityRef
    current_stage: CommissioningStage = 'precheck'
    furthest_stage: CommissioningStage = 'precheck'
    last_event_at_utc: str | None = None
    last_reason: str = ''
    blocked_reason: str | None = None
    provider_disconnected: bool = False
    device_disconnected: bool = False
    completed: bool = False
    rolled_back: bool = False
    aborted: bool = False
    readback_matched: bool | None = None
    transition_count: int = 0
    seen_kinds: frozenset[str] = frozenset()
    stale_stages: tuple[CommissioningStage, ...] = ()
    evidence: dict[str, AuthorityRef] = field(default_factory=dict)


@dataclass(frozen=True)
class TransitionDecision:
    """The machine's accepted answer: stage, outcome, reason."""

    to_stage: CommissioningStage
    outcome: CommissioningTransitionOutcome
    reason: str


@dataclass(frozen=True)
class TransitionRejection:
    """The machine's refused answer — always carries a reason."""

    reason: str


def _stage_index(stage: CommissioningStage) -> int:
    try:
        return COMMISSIONING_STAGE_ORDER.index(stage)
    except ValueError:
        return len(COMMISSIONING_STAGE_ORDER)


#: (stage, event) -> next stage. The complete set of advances —
#: everything else is either a non-advancing event handled below or an
#: explicit rejection.
_ADVANCE_RULES: dict[
    tuple[CommissioningStage, CommissioningEventKind], CommissioningStage
] = {
    ('precheck', 'precheck_evaluated'): 'baseline_measurement',
    ('baseline_measurement', 'baseline_acquisition_recorded'):
        'ingest_quality_gate',
    ('ingest_quality_gate', 'quality_gate_evaluated'): 'analyze',
    ('analyze', 'analysis_bound'): 'optimize',
    ('optimize', 'optimization_committed'): 'operator_approval',
    ('operator_approval', 'operator_authorized'): 'compile_for_target',
    ('compile_for_target', 'compile_completed'): 'deploy',
    ('deploy', 'deploy_acked'): 'readback_verify',
    ('readback_verify', 'readback_evaluated'): 'post_measurement',
    ('post_measurement', 'post_measurement_recorded'): 'before_after',
    ('before_after', 'before_after_evaluated'): 'acceptance_decision',
    ('acceptance_decision', 'acceptance_decided'): 'completed',
}

#: Events allowed after the deploy boundary without advancing —
#: rollback-scope authorizations and runtime observations.
_POST_DEPLOY_STAGES: frozenset[CommissioningStage] = frozenset((
    'deploy', 'readback_verify', 'post_measurement', 'before_after',
    'acceptance_decision'))


def stage_transition(
    state: CommissioningRunState,
    event: CommissioningEvent,
) -> TransitionDecision | TransitionRejection:
    """Pure, deterministic, total: (state, event) -> decision | rejection.

    No implicit transitions, no side effects. A disconnect flag rejects
    every event except restore/abort; a ``succeeded=False`` attempt lands
    as ``blocked`` at the same stage; illegal event/stage pairs are
    explicit rejections with a reason.
    """
    kind = event.kind
    current = state.current_stage

    if kind == 'run_created':
        return TransitionDecision('precheck', 'advanced', event.reason)

    if current in TERMINAL_STAGES:
        return TransitionRejection(f'run_terminal:{current}')

    if kind == 'abort':
        return TransitionDecision('aborted', 'aborted', event.reason)

    if kind == 'connectivity_restored':
        if not state.provider_disconnected and not state.device_disconnected:
            return TransitionRejection('nothing_disconnected')
        return TransitionDecision(current, 'informational', event.reason)

    if state.provider_disconnected:
        if kind == 'provider_disconnected':
            return TransitionRejection('already_disconnected:provider')
        return TransitionRejection('provider_disconnected')
    if state.device_disconnected:
        if kind == 'device_disconnected':
            return TransitionRejection('already_disconnected:device')
        return TransitionRejection('device_disconnected')

    if kind == 'provider_disconnected' or kind == 'device_disconnected':
        return TransitionDecision(current, 'blocked', event.reason)

    if kind == 'rollback_completed':
        if current not in _DEVICE_MUTATING_STAGES:
            return TransitionRejection(
                f'nothing_to_rollback:{current}')
        # Only a machine-verified restore ends the run rolled_back —
        # a failed or unverified rollback leaves it blocked so the
        # operator can retry or abort honestly.
        if event.succeeded is False:
            return TransitionDecision(current, 'blocked', event.reason)
        return TransitionDecision(
            'rolled_back', 'rolled_back', event.reason)

    if kind == 'authority_invalidated':
        # ``succeeded=False`` means the pin re-check found nothing stale —
        # informational evidence, never a regression.
        if event.succeeded is False:
            return TransitionDecision(current, 'informational', event.reason)
        target = event.target_stage
        if target is None or target in TERMINAL_STAGES:
            return TransitionRejection('invalid_regression_target')
        if _stage_index(target) >= _stage_index(current):
            return TransitionRejection(
                f'no_regression:{current}->{target}')
        return TransitionDecision(target, 'invalidated', event.reason)

    if kind == 'operator_authorized':
        # At OPERATOR_APPROVAL the deploy authorization advances the
        # ladder; after the deploy boundary a rollback-scope
        # authorization is informational evidence.
        if current == 'operator_approval':
            return TransitionDecision(
                'compile_for_target', 'advanced', event.reason)
        if current in _DEVICE_MUTATING_STAGES:
            return TransitionDecision(
                current, 'informational', event.reason)
        return TransitionRejection(
            f'event_not_permitted:{kind}@{current}')

    if kind == 'runtime_observed':
        if _stage_index(current) >= _stage_index('readback_verify') \
                and state.readback_matched is True:
            return TransitionDecision(
                current, 'informational', event.reason)
        return TransitionRejection(
            'runtime_requires_matched_readback')

    nxt = _ADVANCE_RULES.get((current, kind))
    if nxt is None:
        return TransitionRejection(
            f'event_not_permitted:{kind}@{current}')
    if event.succeeded is False:
        return TransitionDecision(current, 'blocked', event.reason)
    if nxt == 'completed':
        return TransitionDecision('completed', 'completed', event.reason)
    return TransitionDecision(nxt, 'advanced', event.reason)


def next_permitted_actions(
    state: CommissioningRunState,
) -> tuple[CommissioningEventKind, ...]:
    """Event kinds the machine would accept right now — the UI's menu."""
    permitted: list[CommissioningEventKind] = []
    candidates: tuple[CommissioningEventKind, ...] = (
        'precheck_evaluated',
        'baseline_acquisition_recorded',
        'quality_gate_evaluated',
        'analysis_bound',
        'optimization_committed',
        'operator_authorized',
        'compile_completed',
        'deploy_acked',
        'readback_evaluated',
        'runtime_observed',
        'post_measurement_recorded',
        'before_after_evaluated',
        'acceptance_decided',
        'rollback_completed',
        'provider_disconnected',
        'device_disconnected',
        'connectivity_restored',
        'abort',
    )
    probe_event = CommissioningEvent(
        kind='run_created', at_utc=state.last_event_at_utc or 'x',
        reason='probe')
    for kind in candidates:
        probe = probe_event.model_copy(update={'kind': kind})
        if isinstance(stage_transition(state, probe), TransitionDecision):
            permitted.append(kind)
    return tuple(permitted)


def derive_run_state(
    run: CommissioningOrchestrationRun,
    transitions: tuple[CommissioningStageTransition, ...],
) -> CommissioningRunState:
    """Fold the sealed transition log into the current run state.

    Pure and deterministic — restarting the app folds the same log to
    the same state, which is what makes a run resumable.
    """
    run_ref = AuthorityRef(
        kind='commissioning_orchestration_run',
        ref_id=run.run_id,
        ref_sha256=run.run_sha256,
    )
    state = CommissioningRunState(run_ref=run_ref)
    furthest_index = 0
    stale: list[CommissioningStage] = []
    seen: set[str] = set()
    evidence: dict[str, AuthorityRef] = {}
    readback_matched: bool | None = None
    provider_down = device_down = False
    completed = rolled_back = aborted = False
    current: CommissioningStage = 'precheck'
    blocked_reason: str | None = None
    last_reason = ''
    last_at: str | None = None

    for t in sorted(transitions, key=lambda item: item.seq):
        seen.add(t.event_kind)
        last_at = t.recorded_at_utc
        last_reason = t.reason
        if t.event_kind == 'provider_disconnected' \
                and t.outcome in ('blocked', 'advanced'):
            provider_down = True
        elif t.event_kind == 'device_disconnected' \
                and t.outcome in ('blocked', 'advanced'):
            device_down = True
        elif t.event_kind == 'connectivity_restored' \
                and t.outcome == 'informational':
            provider_down = device_down = False
            blocked_reason = None
        elif t.event_kind == 'readback_evaluated':
            readback_matched = t.event_succeeded

        if t.outcome == 'advanced' or t.outcome == 'invalidated':
            current = t.to_stage
            blocked_reason = None
            idx = _stage_index(current)
            furthest_index = max(furthest_index, idx)
            if t.outcome == 'invalidated':
                stale = [
                    s for s in COMMISSIONING_STAGE_ORDER
                    if _stage_index(s) > idx
                    and _stage_index(s) <= furthest_index
                ]
            else:
                stale = [
                    s for s in stale
                    if _stage_index(s) > _stage_index(current)
                ]
        elif t.outcome == 'completed':
            current = 'completed'
            completed = True
            blocked_reason = None
        elif t.outcome == 'rolled_back':
            current = 'rolled_back'
            rolled_back = True
            blocked_reason = None
        elif t.outcome == 'aborted':
            current = 'aborted'
            aborted = True
            blocked_reason = None
        elif t.outcome in ('blocked', 'rejected'):
            blocked_reason = t.reason
        for ref in t.evidence_refs:
            evidence[ref.kind] = ref

    furthest = COMMISSIONING_STAGE_ORDER[
        min(furthest_index, len(COMMISSIONING_STAGE_ORDER) - 1)]
    if completed:
        furthest = 'acceptance_decision'
    return CommissioningRunState(
        run_ref=run_ref,
        current_stage=current,
        furthest_stage=furthest,
        last_event_at_utc=last_at,
        last_reason=last_reason,
        blocked_reason=blocked_reason,
        provider_disconnected=provider_down,
        device_disconnected=device_down,
        completed=completed,
        rolled_back=rolled_back,
        aborted=aborted,
        readback_matched=readback_matched,
        transition_count=len(transitions),
        seen_kinds=frozenset(seen),
        stale_stages=tuple(stale),
        evidence=evidence,
    )


def derive_evidence_strength(
    state: CommissioningRunState,
    verdict: CommissioningAcceptanceVerdict | None = None,
) -> CommissioningEvidenceStrength:
    """The strongest honest claim the newest evidence supports."""
    if state.aborted:
        return 'aborted'
    if state.rolled_back:
        return 'rolled_back'
    if state.completed:
        return verdict.verdict if verdict is not None else 'indeterminate'
    seen = state.seen_kinds
    if 'post_measurement_recorded' in seen \
            and state.readback_matched is True:
        return 'post_measurement_verified'
    if 'runtime_observed' in seen and state.readback_matched is True:
        return 'runtime_observed'
    if state.readback_matched is True:
        return 'config_readback_matched'
    if 'deploy_acked' in seen:
        return 'deploy_acked'
    if 'compile_completed' in seen:
        return 'compiled'
    if 'commissioning_operator_authorization' in state.evidence:
        return 'authorized'
    if 'optimization_committed' in seen:
        return 'optimized'
    if 'analysis_bound' in seen:
        return 'analyzed'
    if 'quality_gate_evaluated' in seen:
        return 'quality_gated'
    if 'baseline_acquisition_recorded' in seen:
        return 'provider_observed'
    return 'no_evidence'


# ----------------------------------------------------------------------
# invalidation evaluator


@dataclass(frozen=True)
class InvalidationReport:
    """Deterministic result of a relevant-state-change check."""

    stale_pins: tuple[str, ...]
    reasons: tuple[str, ...]
    earliest_stale_stage: CommissioningStage | None


def evaluate_invalidation(
    run: CommissioningOrchestrationRun,
    *,
    scene_content_hash: str | None = None,
    provider_manifest_sha256: str | None = None,
    device_binding_sha256: str | None = None,
    routing_profile_sha256: str | None = None,
    expected_readback_config_sha256: str | None = None,
    observed_config_sha256: str | None = None,
) -> InvalidationReport:
    """Map relevant-state changes to the earliest stale stage.

    Each pin is checked only when both the run's pin and a live value
    are supplied — absence of a live value is not a change (the check
    stays honest about what it cannot see, and a missing device/provider
    answer is a disconnect, not evidence drift).
    """
    stale: list[tuple[str, int, str]] = []

    def _check(
        name: str,
        pinned: str | None,
        live: str | None,
        stage: CommissioningStage,
        reason: str,
    ) -> None:
        if pinned is not None and live is not None and pinned != live:
            stale.append((name, _stage_index(stage), reason))

    _check(
        'scene', run.scene_content_hash, scene_content_hash,
        'baseline_measurement',
        'scene_content_hash changed — baseline evidence no longer '
        'describes this scene revision')
    _check(
        'provider_manifest',
        run.provider_manifest_ref.ref_sha256,
        provider_manifest_sha256,
        'baseline_measurement',
        'provider manifest resealed — provider identity/capability '
        'changed under the run')
    _check(
        'device_binding', run.device_binding_sha256,
        device_binding_sha256, 'precheck',
        'device binding changed — a different device/firmware/routing '
        'is being commissioned')
    _check(
        'routing_profile', run.routing_profile_sha256,
        routing_profile_sha256, 'precheck',
        'routing profile changed — channel/output assumptions no '
        'longer hold')
    if expected_readback_config_sha256 is not None \
            and observed_config_sha256 is not None \
            and expected_readback_config_sha256 != observed_config_sha256:
        stale.append((
            'deployed_config',
            _stage_index('readback_verify'),
            'device config diverged from the verified read-back — '
            'post-deploy evidence is stale'))

    if not stale:
        return InvalidationReport((), (), None)
    earliest = min(stale, key=lambda item: item[1])
    return InvalidationReport(
        tuple(item[0] for item in stale),
        tuple(item[2] for item in stale),
        COMMISSIONING_STAGE_ORDER[earliest[1]],
    )


# ----------------------------------------------------------------------
# orchestrator service


def _run_ref(run: CommissioningOrchestrationRun) -> AuthorityRef:
    return AuthorityRef(
        kind='commissioning_orchestration_run',
        ref_id=run.run_id,
        ref_sha256=run.run_sha256)


def _ref_for(kind: str, record_id: str, sha: str) -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=record_id, ref_sha256=sha)


class CommissioningOrchestrationError(RuntimeError):
    """Raised when an orchestrator call cannot produce honest evidence."""


class CommissioningOrchestrator:
    """Drives the commissioning state machine against sealed evidence.

    Owns no truth of its own: every method evaluates real inputs
    (manifests, adapters, reports), seals records, and emits exactly one
    transition through :func:`stage_transition` — including the blocked
    and rejected ones.
    """

    def __init__(
        self,
        scene_repository: Any,
        *,
        repository: Any = None,
        delegated_repository: Any = None,
        deployment_repository: Any = None,
        camilladsp_repository: Any = None,
    ) -> None:
        from .cad_commissioning_orchestrator_repository import (
            CadCommissioningOrchestratorRepository,
        )
        from .cad_delegated_provider_repository import (
            CadDelegatedProviderRepository,
        )
        from .cad_calibration_deployment_repository import (
            CadCalibrationDeploymentRepository,
        )
        from .cad_camilladsp_deployment_repository import (
            CadCamillaDSPDeploymentRepository,
        )
        self.scene_repository = scene_repository
        self.repository = repository or (
            CadCommissioningOrchestratorRepository(scene_repository))
        self.delegated_repository = delegated_repository or (
            CadDelegatedProviderRepository(scene_repository))
        self.deployment_repository = deployment_repository or (
            CadCalibrationDeploymentRepository(scene_repository))
        self.camilladsp_repository = camilladsp_repository or (
            CadCamillaDSPDeploymentRepository(scene_repository))

    # -- reads --------------------------------------------------------

    def get_run(
        self, run_id: str,
    ) -> CommissioningOrchestrationRun | None:
        return self.repository.get_run(run_id)

    def list_runs(
        self, document_id: str | None = None,
    ) -> tuple[CommissioningOrchestrationRun, ...]:
        return self.repository.list_runs(document_id)

    def transitions(
        self, run: CommissioningOrchestrationRun,
    ) -> tuple[CommissioningStageTransition, ...]:
        return tuple(
            item for item in self.repository.list_transitions(
                run.document_id)
            if item.run_ref.ref_id == run.run_id)

    def derive_state(
        self, run: CommissioningOrchestrationRun,
    ) -> CommissioningRunState:
        return derive_run_state(run, self.transitions(run))

    def get_open_run(
        self, document_id: str,
    ) -> tuple[CommissioningOrchestrationRun, CommissioningRunState] | None:
        """The newest non-terminal run — the resumable one."""
        for run in reversed(self.list_runs(document_id)):
            state = self.derive_state(run)
            if state.current_stage not in TERMINAL_STAGES:
                return run, state
        return None

    # -- transition emission ------------------------------------------

    def _emit(
        self,
        run: CommissioningOrchestrationRun,
        event: CommissioningEvent,
    ) -> CommissioningStageTransition:
        """Run one event through the pure machine and persist the answer."""
        state = self.derive_state(run)
        decision = stage_transition(state, event)
        from_stage: CommissioningStage | None = (
            None if event.kind == 'run_created' else state.current_stage)
        if isinstance(decision, TransitionRejection):
            transition = CommissioningStageTransition.create(
                document_id=run.document_id,
                run_ref=_run_ref(run),
                seq=state.transition_count,
                event_kind=event.kind,
                outcome='rejected',
                actor=event.actor,
                from_stage=from_stage,
                to_stage=state.current_stage,
                event_succeeded=event.succeeded,
                reason=f'rejected:{decision.reason} '
                       f'({event.reason})',
                evidence_refs=event.evidence_refs,
                recorded_at_utc=event.at_utc,
            )
        else:
            transition = CommissioningStageTransition.create(
                document_id=run.document_id,
                run_ref=_run_ref(run),
                seq=state.transition_count,
                event_kind=event.kind,
                outcome=decision.outcome,
                actor=event.actor,
                from_stage=from_stage,
                to_stage=decision.to_stage,
                event_succeeded=event.succeeded,
                reason=decision.reason,
                evidence_refs=event.evidence_refs,
                recorded_at_utc=event.at_utc,
            )
        self.repository.save_transition(transition)
        # #883: mirror the run's lifecycle into the session journal so a
        # crash mid-run advertises a resumable checkpoint — the sealed
        # transitions remain the resume source, never the journal.
        try:
            from .session_recovery import (
                declare_operation_finished,
                declare_operation_started,
            )

            if event.kind == 'run_created':
                declare_operation_started(
                    'commissioning_run', run.run_id,
                    document_id=run.document_id,
                )
            if transition.to_stage in TERMINAL_STAGES:
                declare_operation_finished(run.run_id)
        except Exception:  # error-boundary: best-effort marker — an operation-declare failure is benign bookkeeping; the stage transition itself already committed (noqa: BLE001)
            pass
        return transition

    # -- run creation ---------------------------------------------------

    def create_run(
        self,
        *,
        document_id: str,
        scene_revision_id: str,
        scene_content_hash: str,
        manifest: DelegatedProviderManifest,
        binding: AdapterDeviceBinding,
        created_by: str,
        created_at_utc: str,
        measurement_capability: DelegatedCapability = (
            'frequency_response'),
        campaign_ref: AuthorityRef | None = None,
        routing_profile_id: str | None = None,
        routing_profile_sha256: str | None = None,
        required_capability_claims: tuple[
            MeasurementCapabilityClaim, ...] = ('magnitude_response',),
        acceptance_criteria: tuple[
            CommissioningAcceptanceCriterion, ...] = (),
        target_profile_id: str | None = None,
        notes: str | None = None,
    ) -> CommissioningOrchestrationRun:
        if manifest.document_id != document_id:
            raise ValueError('manifest document does not match the run')
        self.delegated_repository.save_provider_manifest(manifest)
        run = CommissioningOrchestrationRun.create(
            document_id=document_id,
            scene_revision_id=scene_revision_id,
            scene_content_hash=scene_content_hash,
            provider_manifest_ref=manifest_ref(manifest),
            measurement_capability=measurement_capability,
            campaign_ref=campaign_ref,
            device_binding_id=binding.binding_id,
            device_binding_sha256=binding.binding_sha256,
            routing_profile_id=routing_profile_id,
            routing_profile_sha256=routing_profile_sha256,
            required_capability_claims=required_capability_claims,
            acceptance_criteria=acceptance_criteria,
            target_profile_id=target_profile_id,
            created_at_utc=created_at_utc,
            created_by=created_by,
            notes=notes,
        )
        self.repository.save_run(run)
        self._emit(run, CommissioningEvent(
            kind='run_created',
            at_utc=created_at_utc,
            reason='commissioning run created',
            actor='system',
            evidence_refs=(manifest_ref(manifest),),
        ))
        return run

    # -- PRECHECK -------------------------------------------------------

    def record_precheck(
        self,
        run: CommissioningOrchestrationRun,
        *,
        manifest: DelegatedProviderManifest,
        capability_report: AdapterCapabilityReport | None,
        capability: DelegatedCapability | None = None,
        at_utc: str,
        detail: str | None = None,
    ) -> CommissioningStageTransition:
        """Evaluate provider gate + adapter capability; emit the verdict."""
        cap = capability or run.measurement_capability
        verdict, reason = evaluate_provider_gate(manifest, cap)
        if verdict != 'provider_capable':
            return self._emit(run, CommissioningEvent(
                kind='precheck_evaluated', at_utc=at_utc,
                reason=f'provider gate: {reason}', succeeded=False,
                evidence_refs=(manifest_ref(manifest),)))
        if capability_report is None:
            return self._emit(run, CommissioningEvent(
                kind='precheck_evaluated', at_utc=at_utc,
                reason='no adapter capability report — closed loop '
                       'cannot be commissioned', succeeded=False,
                evidence_refs=(manifest_ref(manifest),)))
        missing = tuple(
            name for name, supported in (
                ('materialization',
                 capability_report.supports_materialization),
                ('apply', capability_report.supports_apply),
                ('read_back', capability_report.supports_read_back),
            ) if not supported)
        if missing:
            return self._emit(run, CommissioningEvent(
                kind='precheck_evaluated', at_utc=at_utc,
                reason='adapter_capability_missing:'
                       + ','.join(missing),
                succeeded=False,
                evidence_refs=(manifest_ref(manifest),)))
        return self._emit(run, CommissioningEvent(
            kind='precheck_evaluated', at_utc=at_utc,
            reason=detail or f'provider gate: {reason}; adapter '
                             f'{capability_report.adapter_id} capable',
            succeeded=True,
            evidence_refs=(manifest_ref(manifest),)))

    # -- #878 strongest-path evaluation ---------------------------------

    def evaluate_deployment_paths(
        self,
        candidates: tuple[DeploymentPathCandidate, ...],
    ) -> tuple[DeploymentPathVerdict, ...]:
        """#878: rank deployment paths by evidence strength.

        machine read-back > applied ack > file-verified > assisted
        attestation; unknown capability fails closed to unavailable and
        non-production control surfaces stay visibly ineligible.
        """
        return rank_deployment_paths(candidates)

    def select_deployment_path(
        self,
        candidates: tuple[DeploymentPathCandidate, ...],
        *,
        production_only: bool = False,
    ) -> DeploymentPathVerdict | None:
        """#878: strongest path for this run — None when none exist."""
        if production_only:
            return select_strongest_production_path(candidates)
        return select_strongest_deployment_path(candidates)

    # -- BASELINE_MEASUREMENT / POST_MEASUREMENT ------------------------

    def record_acquisition(
        self,
        run: CommissioningOrchestrationRun,
        *,
        manifest: DelegatedProviderManifest,
        request_identity_repr: str,
        request_sha256: str,
        observed_at_utc: str,
        stage: Literal['baseline', 'post'] = 'baseline',
        acquire: Callable[[], tuple[
            tuple[AuthorityRef, ...], str | None, str | None]] | None = None,
        error_detail: str | None = None,
    ) -> tuple[ProviderAcquisitionRecord, CommissioningStageTransition]:
        """Drive one delegated measurement and seal the acquisition.

        ``acquire`` performs the provider call and returns
        ``(evidence_refs, raw_artifact_sha256, artifact_note)``; raising
        or returning no evidence records an ``error``/``observed``
        acquisition honestly and lands the run ``blocked`` — a failed
        measurement can never produce valid evidence.
        """
        cap = run.measurement_capability
        verdict, gate_reason = evaluate_provider_gate(manifest, cap)
        event_kind: CommissioningEventKind = (
            'baseline_acquisition_recorded' if stage == 'baseline'
            else 'post_measurement_recorded')
        if verdict != 'provider_capable':
            record = build_provider_acquisition(
                manifest,
                capability=cap,
                request_identity_repr=request_identity_repr,
                request_sha256=request_sha256,
                outcome='capability_rejected',
                observed_at_utc=observed_at_utc,
                evidence_refs=(),
                error_detail=gate_reason,
            )
            self.delegated_repository.save_acquisition(record)
            transition = self._emit(run, CommissioningEvent(
                kind=event_kind, at_utc=observed_at_utc,
                reason=f'acquisition rejected: {gate_reason}',
                succeeded=False,
                evidence_refs=(_ref_for(
                    'provider_acquisition', record.acquisition_id,
                    record.acquisition_sha256),)))
            return record, transition
        evidence_refs: tuple[AuthorityRef, ...] = ()
        raw_sha: str | None = None
        artifact_note: str | None = None
        error = error_detail
        outcome: str = 'observed'
        if acquire is not None:
            try:
                evidence_refs, raw_sha, artifact_note = acquire()
            except Exception as exc:  # error-boundary: provider call
                outcome = 'error'
                error = f'{type(exc).__name__}: {exc}'
        elif error_detail is None:
            outcome = 'error'
            error = 'no acquisition driver supplied'
        else:
            outcome = 'error'
        if outcome == 'observed' and not evidence_refs:
            outcome = 'error'
            error = 'provider returned no evidence refs'
        record = build_provider_acquisition(
            manifest,
            capability=cap,
            request_identity_repr=request_identity_repr,
            request_sha256=request_sha256,
            outcome=outcome,  # type: ignore[arg-type]
            observed_at_utc=observed_at_utc,
            evidence_refs=evidence_refs,
            raw_artifact_sha256=raw_sha,
            artifact_note=artifact_note,
            error_detail=error,
        )
        self.delegated_repository.save_acquisition(record)
        acq_evidence = (
            _ref_for('provider_acquisition', record.acquisition_id,
                     record.acquisition_sha256),
        ) + tuple(evidence_refs)
        transition = self._emit(run, CommissioningEvent(
            kind=event_kind, at_utc=observed_at_utc,
            reason=(
                f'acquisition {outcome}'
                + (f': {error}' if error else '')),
            succeeded=outcome == 'observed',
            evidence_refs=acq_evidence,
        ))
        return record, transition

    # -- INGEST/QUALITY_GATE --------------------------------------------

    def record_quality_gate(
        self,
        run: CommissioningOrchestrationRun,
        *,
        report: CadMeasurementQualityReport,
        at_utc: str,
    ) -> CommissioningStageTransition:
        """Gate the ingested measurement — invalid input never advances."""
        report_ref = _ref_for(
            'measurement_quality_report', report.report_id,
            report.report_sha256)
        state = self.derive_state(run)
        baseline_ref = state.evidence.get('provider_acquisition')
        reasons: list[str] = []
        if report.scene_content_hash != run.scene_content_hash:
            reasons.append('report_scene_mismatch')
        if baseline_ref is not None:
            baseline = self.delegated_repository.get_acquisition(
                baseline_ref.ref_id)
            if baseline is not None and not any(
                    ref.ref_sha256 == report.measurement_sha256
                    for ref in baseline.evidence_refs):
                reasons.append('report_measurement_not_in_baseline')
        failed = tuple(
            name for name, check in (
                ('clipping', report.clipping),
                ('noise_snr', report.noise_snr),
                ('usable_frequency_band', report.usable_frequency_band),
                ('timing_reference', report.timing_reference),
                ('polarity', report.polarity),
                ('ir_window', report.ir_window),
                ('calibration', report.calibration),
                ('repeatability', report.repeatability),
            ) if check.status != 'PASS')
        if failed:
            reasons.append('checks_not_pass:' + ','.join(failed))
        blocked_claims = tuple(
            cap.claim for cap in report.capabilities
            if cap.claim in run.required_capability_claims
            and cap.decision != 'ALLOWED')
        if blocked_claims:
            reasons.append('claims_blocked:' + ','.join(blocked_claims))
        ok = not reasons
        return self._emit(run, CommissioningEvent(
            kind='quality_gate_evaluated', at_utc=at_utc,
            reason='quality gate passed' if ok else '; '.join(reasons),
            succeeded=ok,
            evidence_refs=(report_ref,)))

    # -- ANALYZE / OPTIMIZE -----------------------------------------------

    def bind_analysis(
        self,
        run: CommissioningOrchestrationRun,
        *,
        evidence_refs: tuple[AuthorityRef, ...],
        at_utc: str,
        reason: str = 'analysis evidence bound',
    ) -> CommissioningStageTransition:
        """Bind exact analysis evidence — it can never be anonymous."""
        if not evidence_refs:
            return self._emit(run, CommissioningEvent(
                kind='analysis_bound', at_utc=at_utc,
                reason='analysis produced no evidence refs',
                succeeded=False))
        return self._emit(run, CommissioningEvent(
            kind='analysis_bound', at_utc=at_utc, reason=reason,
            succeeded=True, evidence_refs=evidence_refs))

    def commit_optimization(
        self,
        run: CommissioningOrchestrationRun,
        *,
        plan: CadCalibrationPlan,
        at_utc: str,
    ) -> CommissioningStageTransition:
        """Commit a solver plan — its baseline pins must match the run's."""
        plan_ref = _ref_for(
            'calibration_plan', plan.plan_id,
            plan.plan_semantic_sha256)
        state = self.derive_state(run)
        report_ref = state.evidence.get('measurement_quality_report')
        baseline_ref = state.evidence.get('provider_acquisition')
        reasons: list[str] = []
        if plan.scene_content_hash != run.scene_content_hash:
            reasons.append('plan_scene_mismatch')
        if report_ref is not None and \
                plan.measurement_quality_report_sha256 \
                != report_ref.ref_sha256:
            reasons.append('plan_quality_report_mismatch')
        if baseline_ref is not None:
            baseline = self.delegated_repository.get_acquisition(
                baseline_ref.ref_id)
            if baseline is not None and not any(
                    ref.ref_sha256 == plan.source_measurement_sha256
                    for ref in baseline.evidence_refs):
                reasons.append('plan_baseline_measurement_mismatch')
        if plan.support_state != 'SUPPORTED':
            reasons.append(
                'plan_unsupported:' + '; '.join(plan.unsupported_reasons))
        if reasons:
            return self._emit(run, CommissioningEvent(
                kind='optimization_committed', at_utc=at_utc,
                reason='; '.join(reasons), succeeded=False,
                evidence_refs=(plan_ref,)))
        return self._emit(run, CommissioningEvent(
            kind='optimization_committed', at_utc=at_utc,
            reason='calibration plan committed: '
                   f'{plan.authority_version} v{plan.plan_version}',
            succeeded=True, evidence_refs=(plan_ref,)))

    # -- OPERATOR_APPROVAL ------------------------------------------------

    def authorize(
        self,
        run: CommissioningOrchestrationRun,
        *,
        operator_id: str,
        scope: Literal['deploy_apply', 'rollback_apply'],
        at_utc: str,
        candidate_sha256: str | None = None,
        deployment_ref: AuthorityRef | None = None,
        expires_at_utc: str | None = None,
        note: str | None = None,
    ) -> tuple[
        CommissioningOperatorAuthorization, CommissioningStageTransition]:
        """Seal an explicit operator authorization for one mutation."""
        authorization = CommissioningOperatorAuthorization.create(
            document_id=run.document_id,
            run_ref=_run_ref(run),
            scope=scope,
            candidate_sha256=candidate_sha256,
            deployment_ref=deployment_ref,
            operator_id=operator_id,
            authorized_at_utc=at_utc,
            expires_at_utc=expires_at_utc,
            note=note,
        )
        self.repository.save_authorization(authorization)
        transition = self._emit(run, CommissioningEvent(
            kind='operator_authorized', at_utc=at_utc,
            reason=f'{scope} authorized by {operator_id}',
            actor='operator', succeeded=True,
            evidence_refs=(_ref_for(
                'commissioning_operator_authorization',
                authorization.authorization_id,
                authorization.authorization_sha256),)))
        return authorization, transition

    # -- COMPILE_FOR_TARGET -----------------------------------------------

    def compile_for_target(
        self,
        run: CommissioningOrchestrationRun,
        *,
        adapter: CalibrationDeviceAdapter,
        export: CadCalibrationExportSnapshot,
        binding: AdapterDeviceBinding,
        declaration: DeploymentCapabilityDeclaration | None,
        at_utc: str,
    ) -> tuple[
        MaterializedCalibrationSettings | None,
        CommissioningStageTransition]:
        """Compile the plan's export to device-native settings."""
        if binding.binding_sha256 != run.device_binding_sha256:
            transition = self._emit(run, CommissioningEvent(
                kind='compile_completed', at_utc=at_utc,
                reason='binding_mismatch:compile target is not the run '
                       'device', succeeded=False))
            return None, transition
        try:
            materialization = adapter.materialize(
                export, binding, created_at_utc=at_utc)
        except Exception as exc:  # error-boundary: adapter compile
            transition = self._emit(run, CommissioningEvent(
                kind='compile_completed', at_utc=at_utc,
                reason=f'compile_error:{type(exc).__name__}: {exc}',
                succeeded=False))
            return None, transition
        try:
            validate_materialization_result(
                adapter.capability(), export, binding, materialization)
        except Exception as exc:  # error-boundary: adapter postcondition
            transition = self._emit(run, CommissioningEvent(
                kind='compile_completed', at_utc=at_utc,
                reason=f'materialization_mismatch:{exc}',
                succeeded=False,
                evidence_refs=(_ref_for(
                    'materialized_settings',
                    materialization.materialization_id,
                    materialization.materialization_sha256),)))
            return materialization, transition
        if declaration is not None:
            self.deployment_repository.save_capability_declaration(
                declaration)
        verdict, gate_reason = evaluate_deployment_gate(
            materialization, declaration)
        if verdict != 'deployable':
            transition = self._emit(run, CommissioningEvent(
                kind='compile_completed', at_utc=at_utc,
                reason=f'deployment gate: {gate_reason}',
                succeeded=False,
                evidence_refs=(_ref_for(
                    'materialized_settings',
                    materialization.materialization_id,
                    materialization.materialization_sha256),)))
            return materialization, transition
        transition = self._emit(run, CommissioningEvent(
            kind='compile_completed', at_utc=at_utc,
            reason=f'compiled for target ({gate_reason})',
            succeeded=True,
            evidence_refs=(_ref_for(
                'materialized_settings',
                materialization.materialization_id,
                materialization.materialization_sha256),)))
        return materialization, transition

    # -- DEPLOY ------------------------------------------------------------

    def _load_authorization(
        self,
        run: CommissioningOrchestrationRun,
        authorization_id: str,
    ) -> CommissioningOperatorAuthorization:
        record = self.repository.get_authorization(authorization_id)
        if record is None:
            raise ValueError(
                f'authorization {authorization_id!r} not found')
        if record.run_ref.ref_id != run.run_id:
            raise ValueError('authorization belongs to a different run')
        return record

    def _authorization_consumed(
        self,
        run: CommissioningOrchestrationRun,
        authorization_id: str,
        event_kind: str,
    ) -> bool:
        for t in self.transitions(run):
            if t.event_kind != event_kind or t.outcome != 'advanced':
                continue
            if any(ref.ref_id == authorization_id
                    for ref in t.evidence_refs):
                return True
        return False

    def deploy(
        self,
        run: CommissioningOrchestrationRun,
        *,
        adapter: CalibrationDeviceAdapter,
        binding: AdapterDeviceBinding,
        materialization: MaterializedCalibrationSettings,
        export: CadCalibrationExportSnapshot,
        declaration: DeploymentCapabilityDeclaration,
        authorization_id: str,
        target_class: DeploymentTargetClass = 'unknown',
        at_utc: str,
    ) -> tuple[
        CalibrationDeployment | None,
        CamillaDSPDeploymentSession | None,
        CommissioningStageTransition]:
        """Apply the compiled config under an exact operator authorization.

        An ack is never verification: the sealed deployment lands as
        ``deployment_unverified`` and only the read-back stage can raise
        the evidence. Authorization is one-shot — already-consumed,
        expired, scope- or payload-mismatched authorizations are
        rejected, never silently honored.
        """
        auth_reason: str | None = None
        try:
            auth = self._load_authorization(run, authorization_id)
        except ValueError as exc:
            auth = None
            auth_reason = str(exc)
        if auth is not None:
            if auth.scope != 'deploy_apply':
                auth_reason = 'authorization scope is not deploy_apply'
            elif auth.candidate_sha256 \
                    != export.requested_plan_semantic_sha256:
                auth_reason = (
                    'authorization pins a different calibration '
                    'candidate')
            elif materialization.export_id != export.export_id \
                    or materialization.exported_settings_semantic_sha256 \
                    != export.exported_settings_semantic_sha256:
                auth_reason = (
                    'materialization does not descend from the '
                    'authorized export')
            elif auth.expires_at_utc is not None \
                    and auth.expires_at_utc < at_utc:
                auth_reason = 'authorization expired'
            elif self._authorization_consumed(
                    run, authorization_id, 'deploy_acked'):
                auth_reason = 'authorization already consumed'
        if auth_reason is not None:
            transition = self._emit(run, CommissioningEvent(
                kind='deploy_acked', at_utc=at_utc,
                reason=f'deploy_rejected:{auth_reason}',
                succeeded=False, actor='operator'))
            return None, None, transition

        previous_sha: str | None = None
        capture = getattr(adapter, 'capture_baseline', None)
        if callable(capture):
            try:
                _canonical, previous_sha = capture(binding)
            except Exception:  # error-boundary: baseline probe — a capture failure degrades to 'no previous sha' honestly, never a forged baseline (noqa: BLE001)
                previous_sha = None
        try:
            ack: DeviceApplyAck = adapter.apply(
                materialization, binding,
                operator_confirmed=True, applied_at_utc=at_utc)
        except Exception as exc:  # error-boundary: device mutation
            transition = self._emit(run, CommissioningEvent(
                kind='deploy_acked', at_utc=at_utc,
                reason=f'apply_error:{type(exc).__name__}: {exc}',
                succeeded=False))
            return None, None, transition

        state, evidence_mode = derive_deployment_state(None)
        deployment = CalibrationDeployment.create(
            document_id=run.document_id,
            calibration_plan_id=export.calibration_plan_id,
            calibration_plan_sha256=(
                export.requested_plan_semantic_sha256),
            export_id=export.export_id,
            export_sha256=export.exported_settings_semantic_sha256,
            materialization_id=materialization.materialization_id,
            materialization_sha256=materialization.materialization_sha256,
            binding_id=binding.binding_id,
            binding_sha256=binding.binding_sha256,
            adapter_id=materialization.adapter_id,
            adapter_version=materialization.adapter_version,
            capability_declaration_sha256=declaration.declaration_sha256,
            target_class=target_class,
            apply_ack_id=ack.ack_id,
            evidence_mode=evidence_mode,
            deployment_state=state,
            deployed_at_utc=at_utc,
        )
        self.deployment_repository.save_deployment(deployment)
        deployment_ref = _ref_for(
            'calibration_deployment', deployment.deployment_id,
            deployment.deployment_sha256)
        # The CamillaDSP deployment session binds protocol-level
        # evidence (candidate/baseline config hashes) — a CamillaDSP-
        # specific artifact that generic adapters do not produce.
        session: CamillaDSPDeploymentSession | None = None
        if isinstance(adapter, CamillaDSPCalibrationAdapter):
            candidate_sha = config_sha256(
                _parse_config_document(materialization.payload_text))
            session = CamillaDSPDeploymentSession.create(
                document_id=run.document_id,
                deployment_ref=deployment_ref,
                binding_sha256=binding.binding_sha256,
                materialization_sha256=(
                    materialization.materialization_sha256),
                candidate_config_sha256=candidate_sha,
                previous_config_sha256=previous_sha,
                deploy_ack_id=ack.ack_id,
                deployed_at_utc=at_utc,
            )
            self.camilladsp_repository.save_deployment_session(session)
        evidence = [deployment_ref]
        if session is not None:
            evidence.append(_ref_for(
                'camilladsp_deployment_session',
                session.session_id, session.session_sha256))
        evidence.append(_ref_for(
            'commissioning_operator_authorization',
            auth.authorization_id, auth.authorization_sha256))
        transition = self._emit(run, CommissioningEvent(
            kind='deploy_acked', at_utc=at_utc,
            reason=f'deploy acknowledged (ack {ack.ack_id}) — '
                   'unverified until read-back',
            succeeded=True,
            evidence_refs=tuple(evidence)))
        return deployment, session, transition

    # -- READBACK_VERIFY ----------------------------------------------------

    def verify_readback(
        self,
        run: CommissioningOrchestrationRun,
        *,
        adapter: CalibrationDeviceAdapter,
        binding: AdapterDeviceBinding,
        export: CadCalibrationExportSnapshot,
        deployment: CalibrationDeployment,
        materialization: MaterializedCalibrationSettings,
        declaration: DeploymentCapabilityDeclaration,
        at_utc: str,
    ) -> tuple[
        CalibrationDeployment | None,
        CamillaDSPDeploymentSession | None,
        CommissioningStageTransition]:
        """Machine read-back — the only path to config_readback_matched."""
        deployment_ref = _ref_for(
            'calibration_deployment', deployment.deployment_id,
            deployment.deployment_sha256)
        try:
            observed = adapter.read_back(
                binding, observed_at_utc=at_utc)
        except Exception as exc:  # error-boundary: device read
            transition = self._emit(run, CommissioningEvent(
                kind='readback_evaluated', at_utc=at_utc,
                reason=f'readback_error:{type(exc).__name__}: {exc}',
                succeeded=False, evidence_refs=(deployment_ref,)))
            return None, None, transition
        snapshot = build_observation(
            document_id=run.document_id,
            binding=binding,
            export=export,
            observed_channels=observed,
            observed_at_utc=at_utc,
            source='read_back',
        )
        matched = not snapshot.deviations
        # The CamillaDSP deployment session pins the exact device-config
        # hash read back — only a CamillaDSP adapter can produce it.
        readback_config = getattr(adapter, 'read_back_config', None)
        session: CamillaDSPDeploymentSession | None = None
        if callable(readback_config):
            try:
                _config, readback_sha = readback_config(binding)
            except Exception:  # error-boundary: device config read
                readback_sha = None
        else:
            readback_sha = None
        if readback_sha is not None:
            prev_sha = None
            candidate_sha = config_sha256(
                _parse_config_document(materialization.payload_text))
            for prior in reversed(
                    self.camilladsp_repository.list_deployment_sessions(
                        run.document_id)):
                if prior.deployment_ref.ref_id == deployment.deployment_id:
                    prev_sha = prior.previous_config_sha256
                    break
            session = CamillaDSPDeploymentSession.create(
                document_id=run.document_id,
                deployment_ref=deployment_ref,
                binding_sha256=binding.binding_sha256,
                materialization_sha256=(
                    materialization.materialization_sha256),
                candidate_config_sha256=candidate_sha,
                previous_config_sha256=prev_sha,
                deploy_ack_id=deployment.apply_ack_id,
                deployed_at_utc=deployment.deployed_at_utc,
                readback_config_sha256=readback_sha,
                readback_matched=matched,
            )
            self.camilladsp_repository.save_deployment_session(session)
        state, evidence_mode = derive_deployment_state(
            'read_back', observed_divergence=not matched)
        verified_deployment = CalibrationDeployment.create(
            document_id=run.document_id,
            calibration_plan_id=deployment.calibration_plan_id,
            calibration_plan_sha256=deployment.calibration_plan_sha256,
            export_id=deployment.export_id,
            export_sha256=deployment.export_sha256,
            materialization_id=deployment.materialization_id,
            materialization_sha256=deployment.materialization_sha256,
            binding_id=deployment.binding_id,
            binding_sha256=deployment.binding_sha256,
            adapter_id=deployment.adapter_id,
            adapter_version=deployment.adapter_version,
            capability_declaration_sha256=(
                declaration.declaration_sha256),
            target_class=deployment.target_class,
            apply_ack_id=deployment.apply_ack_id,
            observed_snapshot_id=snapshot.snapshot_id,
            observed_snapshot_sha256=snapshot.snapshot_sha256,
            evidence_mode=evidence_mode,
            deployment_state=state,
            deployed_at_utc=deployment.deployed_at_utc,
        )
        self.deployment_repository.save_deployment(verified_deployment)
        evidence = [
            _ref_for('calibration_deployment',
                     verified_deployment.deployment_id,
                     verified_deployment.deployment_sha256),
            _ref_for('effective_settings_snapshot',
                     snapshot.snapshot_id, snapshot.snapshot_sha256),
        ]
        if session is not None:
            evidence.append(_ref_for(
                'camilladsp_deployment_session',
                session.session_id, session.session_sha256))
        transition = self._emit(run, CommissioningEvent(
            kind='readback_evaluated', at_utc=at_utc,
            reason=(
                'config read-back matched'
                if matched else
                'config read-back diverged: '
                + ','.join(
                    f'{d.channel_id}.{d.field_name}'
                    for d in snapshot.deviations)),
            succeeded=matched,
            evidence_refs=tuple(evidence)))
        return verified_deployment, session, transition

    # -- RUNTIME_OBSERVED ----------------------------------------------------

    def observe_runtime(
        self,
        run: CommissioningOrchestrationRun,
        *,
        adapter: CalibrationDeviceAdapter,
        binding: AdapterDeviceBinding,
        deployment: CalibrationDeployment,
        at_utc: str,
    ) -> CommissioningStageTransition:
        """Seal device runtime telemetry — distinct from acoustic proof."""
        observe = getattr(adapter, 'observe_runtime', None)
        if not callable(observe):
            raise ValueError(
                'adapter does not expose runtime observation')
        observation = observe(
            binding, document_id=run.document_id,
            observed_at_utc=at_utc,
            deployment_ref=_ref_for(
                'calibration_deployment', deployment.deployment_id,
                deployment.deployment_sha256))
        self.camilladsp_repository.save_runtime_observation(observation)
        return self._emit(run, CommissioningEvent(
            kind='runtime_observed', at_utc=at_utc,
            reason='runtime telemetry sealed (runtime evidence, not '
                   'acoustic verification)',
            evidence_refs=(_ref_for(
                'camilladsp_runtime_observation',
                observation.observation_id,
                observation.observation_sha256),)))

    # -- BEFORE/AFTER --------------------------------------------------------

    def evaluate_before_after(
        self,
        run: CommissioningOrchestrationRun,
        *,
        deployment: CalibrationDeployment,
        readback_config_sha256: str,
        baseline_refs: tuple[AuthorityRef, ...],
        post_refs: tuple[AuthorityRef, ...],
        deltas: tuple[EffectivenessMetricDelta, ...],
        at_utc: str,
        verdict: EffectivenessVerdict | None = None,
        note: str | None = None,
    ) -> tuple[CommissioningBeforeAfter, CommissioningStageTransition]:
        """Seal the before/after comparison bound to the read-back config."""
        if verdict is None:
            if not post_refs:
                verdict = 'unmeasured'
            elif any(d.outcome == 'regressed' for d in deltas):
                verdict = 'regression_observed'
            elif any(d.outcome == 'improved' for d in deltas) \
                    and not any(d.outcome == 'indeterminate'
                                for d in deltas):
                verdict = 'improvement_verified'
            elif all(d.outcome == 'unchanged' for d in deltas) and deltas:
                verdict = 'no_measurable_change'
            else:
                verdict = 'inconclusive'
        comparison = CommissioningBeforeAfter.create(
            document_id=run.document_id,
            run_ref=_run_ref(run),
            deployment_ref=_ref_for(
                'calibration_deployment', deployment.deployment_id,
                deployment.deployment_sha256),
            readback_config_sha256=readback_config_sha256,
            baseline_evidence_refs=baseline_refs,
            post_measurement_refs=post_refs,
            deltas=deltas,
            verdict=verdict,
            evaluated_at_utc=at_utc,
            evaluator_note=note,
        )
        self.repository.save_before_after(comparison)
        transition = self._emit(run, CommissioningEvent(
            kind='before_after_evaluated', at_utc=at_utc,
            reason=f'before/after evaluated: {verdict}',
            succeeded=True,
            evidence_refs=(_ref_for(
                'commissioning_before_after', comparison.comparison_id,
                comparison.comparison_sha256),)))
        return comparison, transition

    # -- ACCEPTANCE_DECISION -------------------------------------------------

    def decide_acceptance(
        self,
        run: CommissioningOrchestrationRun,
        *,
        comparison: CommissioningBeforeAfter,
        at_utc: str,
        evaluator_note: str | None = None,
    ) -> tuple[
        CommissioningAcceptanceVerdict, CommissioningStageTransition]:
        """Evaluate the configured criteria — fails closed on stale/missing."""
        state = self.derive_state(run)
        if state.current_stage != 'acceptance_decision':
            raise ValueError(
                'acceptance is decided at the acceptance_decision stage '
                f'(run is at {state.current_stage})')
        stale = bool(state.stale_stages)
        deltas = {d.metric_id: d.outcome for d in comparison.deltas}
        results: list[CommissioningCriterionResult] = []
        for criterion in run.acceptance_criteria:
            observed = deltas.get(criterion.metric_id)
            if observed is None:
                outcome: CriterionOutcome = 'indeterminate'
                reason = 'metric absent from before/after comparison'
                observed_label: MetricOutcome | Literal[
                    'missing'] = 'missing'
            elif observed == 'indeterminate':
                outcome = 'indeterminate'
                reason = 'metric indeterminate'
                observed_label = observed
            else:
                observed_label = observed
                if criterion.expected == 'improvement':
                    ok = observed == 'improved'
                else:
                    ok = observed in ('improved', 'unchanged')
                if ok:
                    outcome = 'satisfied'
                    reason = f'observed {observed}'
                elif criterion.required:
                    outcome = 'failed'
                    reason = (
                        f'expected {criterion.expected}, '
                        f'observed {observed}')
                else:
                    outcome = 'indeterminate'
                    reason = (
                        f'non-required criterion off target: '
                        f'{observed}')
            results.append(CommissioningCriterionResult(
                metric_id=criterion.metric_id,
                expected=criterion.expected,
                outcome=outcome,
                observed=observed_label,
                reason=reason))
        result_tuple = tuple(results)
        if any(r.outcome == 'failed' for r in result_tuple):
            verdict: CommissioningAcceptanceOutcome = 'rejected'
        elif result_tuple and all(
                r.outcome == 'satisfied' for r in result_tuple) \
                and not stale:
            verdict = 'accepted'
        else:
            verdict = 'indeterminate'
        acceptance = CommissioningAcceptanceVerdict.create(
            document_id=run.document_id,
            run_ref=_run_ref(run),
            before_after_ref=_ref_for(
                'commissioning_before_after', comparison.comparison_id,
                comparison.comparison_sha256),
            criteria_results=result_tuple,
            stale_evidence=stale,
            verdict=verdict,
            decided_at_utc=at_utc,
            evaluator_note=evaluator_note,
        )
        self.repository.save_verdict(acceptance)
        transition = self._emit(run, CommissioningEvent(
            kind='acceptance_decided', at_utc=at_utc,
            reason=f'acceptance verdict: {verdict}'
                   + (' (stale evidence)' if stale else ''),
            succeeded=verdict == 'accepted',
            evidence_refs=(_ref_for(
                'commissioning_acceptance_verdict',
                acceptance.verdict_id, acceptance.verdict_sha256),)))
        return acceptance, transition

    # -- ROLLBACK ------------------------------------------------------------

    def rollback(
        self,
        run: CommissioningOrchestrationRun,
        *,
        adapter: CalibrationDeviceAdapter,
        binding: AdapterDeviceBinding,
        deployment: CalibrationDeployment,
        authorization_id: str,
        reason: str,
        at_utc: str,
    ) -> tuple[CommissioningRollback, CommissioningStageTransition]:
        """Roll the deployment back under an exact rollback authorization."""
        deployment_ref = _ref_for(
            'calibration_deployment', deployment.deployment_id,
            deployment.deployment_sha256)
        auth_reason: str | None = None
        try:
            auth = self._load_authorization(run, authorization_id)
        except ValueError as exc:
            auth = None
            auth_reason = str(exc)
        if auth is not None:
            if auth.scope != 'rollback_apply':
                auth_reason = 'authorization scope is not rollback_apply'
            elif auth.deployment_ref is None \
                    or auth.deployment_ref.ref_id \
                    != deployment.deployment_id:
                auth_reason = (
                    'authorization does not pin this deployment')
            elif auth.expires_at_utc is not None \
                    and auth.expires_at_utc < at_utc:
                auth_reason = 'authorization expired'
        if auth_reason is not None:
            self._emit(run, CommissioningEvent(
                kind='rollback_completed', at_utc=at_utc,
                reason=f'rollback_rejected:{auth_reason}',
                succeeded=False, actor='operator'))
            raise CommissioningOrchestrationError(auth_reason)

        adapter_ref: AuthorityRef | None = None
        outcome: CommissioningRollbackOutcome = 'not_attempted'
        error_detail: str | None = None
        evidence: CamillaDSPRollbackEvidence | None = None
        rollback_previous = getattr(adapter, 'rollback_previous', None)
        if callable(rollback_previous):
            session = None
            for prior in reversed(
                    self.camilladsp_repository.list_deployment_sessions(
                        run.document_id)):
                if prior.deployment_ref.ref_id == deployment.deployment_id:
                    session = prior
                    break
            try:
                evidence = rollback_previous(
                    binding,
                    document_id=run.document_id,
                    deployment_ref=deployment_ref,
                    requested_at_utc=at_utc,
                    session=session,
                )
                self.camilladsp_repository.save_rollback_evidence(evidence)
                adapter_ref = _ref_for(
                    'camilladsp_rollback_evidence',
                    evidence.evidence_id, evidence.evidence_sha256)
                outcome = {
                    'restored_verified': 'restored_verified',
                    'restored_previous_diverged': 'restored_unverified',
                    'restored_mismatch': 'restored_unverified',
                    'failed': 'failed',
                    'not_attempted': 'not_attempted',
                }[evidence.outcome]
                if evidence.outcome != 'restored_verified':
                    error_detail = evidence.error_detail
            except Exception as exc:  # error-boundary: device mutation
                outcome = 'failed'
                error_detail = f'{type(exc).__name__}: {exc}'
        else:
            error_detail = (
                'adapter does not expose machine rollback — '
                'restored state stays unverified')
        record = CommissioningRollback.create(
            document_id=run.document_id,
            run_ref=_run_ref(run),
            deployment_ref=deployment_ref,
            authorization_ref=_ref_for(
                'commissioning_operator_authorization',
                auth.authorization_id, auth.authorization_sha256),  # type: ignore[union-attr]
            adapter_evidence_ref=adapter_ref,
            outcome=outcome,
            requested_at_utc=at_utc,
            completed_at_utc=at_utc if evidence is not None else None,
            reason=reason,
            error_detail=error_detail,
        )
        self.repository.save_rollback(record)
        transition = self._emit(run, CommissioningEvent(
            kind='rollback_completed', at_utc=at_utc,
            reason=f'rollback {outcome}: {reason}',
            actor='operator', succeeded=outcome == 'restored_verified',
            evidence_refs=(
                _ref_for('commissioning_rollback', record.rollback_id,
                         record.rollback_sha256),
                deployment_ref)
                + ((adapter_ref,) if adapter_ref is not None else ())))
        return record, transition

    # -- disconnects / invalidation / abort ---------------------------------

    def report_disconnect(
        self,
        run: CommissioningOrchestrationRun,
        *,
        scope: Literal['provider', 'device'],
        reason: str,
        at_utc: str,
    ) -> CommissioningStageTransition:
        return self._emit(run, CommissioningEvent(
            kind=('provider_disconnected' if scope == 'provider'
                  else 'device_disconnected'),
            at_utc=at_utc, reason=reason, actor='system'))

    def restore_connectivity(
        self,
        run: CommissioningOrchestrationRun,
        *,
        reason: str,
        at_utc: str,
    ) -> CommissioningStageTransition:
        return self._emit(run, CommissioningEvent(
            kind='connectivity_restored', at_utc=at_utc,
            reason=reason, actor='system'))

    def apply_invalidation(
        self,
        run: CommissioningOrchestrationRun,
        *,
        at_utc: str,
        reason: str = 'authority pins re-evaluated',
        **pins: str | None,
    ) -> tuple[InvalidationReport, CommissioningStageTransition]:
        """Check live pins against the run and regress stale evidence."""
        report = evaluate_invalidation(run, **pins)
        target = report.earliest_stale_stage or 'precheck'
        transition = self._emit(run, CommissioningEvent(
            kind='authority_invalidated', at_utc=at_utc,
            reason=(f'{reason}: ' + '; '.join(report.reasons))
                    if report.stale_pins else reason,
            succeeded=bool(report.stale_pins),
            target_stage=target))
        return report, transition

    def abort(
        self,
        run: CommissioningOrchestrationRun,
        *,
        reason: str,
        at_utc: str,
        actor: CommissioningActor = 'operator',
    ) -> CommissioningStageTransition:
        return self._emit(run, CommissioningEvent(
            kind='abort', at_utc=at_utc, reason=reason, actor=actor))


# ----------------------------------------------------------------------
# JA label registry

COMMISSIONING_STAGE_LABELS: dict[str, str] = {
    'precheck': '事前チェック',
    'baseline_measurement': 'ベースライン測定',
    'ingest_quality_gate': '取り込み/品質ゲート',
    'analyze': '解析',
    'optimize': '最適化',
    'operator_approval': 'オペレーター承認',
    'compile_for_target': 'ターゲット向けコンパイル',
    'deploy': 'デプロイ',
    'readback_verify': '読み戻し検証',
    'post_measurement': 'ポスト測定',
    'before_after': 'ビフォー/アフター比較',
    'acceptance_decision': '受け入れ判定',
    'completed': '完了',
    'rolled_back': 'ロールバック済み',
    'aborted': '中止',
}

COMMISSIONING_EVENT_LABELS: dict[str, str] = {
    'run_created': 'ラン作成',
    'precheck_evaluated': '事前チェック評価',
    'baseline_acquisition_recorded': 'ベースライン取得記録',
    'quality_gate_evaluated': '品質ゲート評価',
    'analysis_bound': '解析証跡バインド',
    'optimization_committed': '最適化コミット',
    'operator_authorized': 'オペレーター承認記録',
    'compile_completed': 'コンパイル完了',
    'deploy_acked': 'デプロイ ACK',
    'readback_evaluated': '読み戻し評価',
    'runtime_observed': 'ランタイム観測',
    'post_measurement_recorded': 'ポスト測定記録',
    'before_after_evaluated': 'ビフォー/アフター評価',
    'acceptance_decided': '受け入れ判定記録',
    'rollback_completed': 'ロールバック完了',
    'provider_disconnected': 'プロバイダ切断',
    'device_disconnected': 'デバイス切断',
    'connectivity_restored': '接続復帰',
    'authority_invalidated': '権威無効化',
    'abort': '中止要求',
}

COMMISSIONING_OUTCOME_LABELS: dict[str, str] = {
    'advanced': '進行',
    'rejected': '拒否',
    'blocked': 'ブロック',
    'completed': '完了',
    'rolled_back': 'ロールバック',
    'aborted': '中止',
    'invalidated': '無効化（回帰）',
    'informational': '記録のみ',
}

COMMISSIONING_EVIDENCE_STRENGTH_LABELS: dict[str, str] = {
    'no_evidence': '証跡なし',
    'provider_observed': 'プロバイダ観測済み',
    'quality_gated': '品質ゲート通過',
    'analyzed': '解析済み',
    'optimized': '最適化済み',
    'authorized': '承認済み',
    'compiled': 'コンパイル済み',
    'deploy_acked': 'デプロイ ACK のみ（未検証）',
    'config_readback_matched': '構成読み戻し一致',
    'runtime_observed': 'ランタイム観測済み',
    'post_measurement_verified': 'ポスト測定検証済み',
    'accepted': '受け入れ済み',
    'rejected': '拒否判定',
    'indeterminate': '判定不能',
    'rolled_back': 'ロールバック済み',
    'aborted': '中止',
}

COMMISSIONING_ROLLBACK_LABELS: dict[str, str] = {
    'restored_verified': '復元済み（機器読み戻し一致）',
    'restored_unverified': '復元試行（検証なし/不一致）',
    'failed': 'ロールバック失敗',
    'not_attempted': 'ロールバック未実施',
}

COMMISSIONING_VERDICT_LABELS: dict[str, str] = {
    'accepted': '受け入れ',
    'rejected': '拒否',
    'indeterminate': '判定不能',
}


__all__ = [
    'COMMISSIONING_DEVICE_MUTATING_STAGES',
    'COMMISSIONING_EVENT_LABELS',
    'COMMISSIONING_EVIDENCE_STRENGTH_LABELS',
    'COMMISSIONING_OUTCOME_LABELS',
    'COMMISSIONING_ROLLBACK_LABELS',
    'COMMISSIONING_STAGE_LABELS',
    'COMMISSIONING_STAGE_ORDER',
    'COMMISSIONING_VERDICT_LABELS',
    'CommissioningAcceptanceCriterion',
    'CommissioningAcceptanceVerdict',
    'CommissioningBeforeAfter',
    'CommissioningCriterionResult',
    'CommissioningEvent',
    'CommissioningOperatorAuthorization',
    'CommissioningOrchestrationError',
    'CommissioningOrchestrationRun',
    'CommissioningOrchestrator',
    'CommissioningRollback',
    'CommissioningRunState',
    'CommissioningStageTransition',
    'InvalidationReport',
    'TERMINAL_STAGES',
    'TransitionDecision',
    'TransitionRejection',
    'derive_evidence_strength',
    'derive_run_state',
    'evaluate_invalidation',
    'next_permitted_actions',
    'stage_transition',
]
