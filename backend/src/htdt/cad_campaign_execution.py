"""Automated multi-position measurement campaign execution (#875, REV67).

A registered measurement campaign becomes an executable, HTDT-owned
acquisition plan:

    CAMPAIGN -> POSITION -> CHANNEL -> REPETITION -> AUTO-SWEEP
    -> QUALITY GATE -> RETRY/ADVANCE -> NEXT POSITION

The operator only performs irreducibly physical steps (moving the
microphone, changing hardware). Everything else — channel selection,
sweep execution, run identity binding, quality gating, retry decisions,
progress accounting — is machine-driven and journaled as sealed,
append-only events so the campaign survives an application restart with
its exact remaining work.

This module owns:

- :class:`CampaignExecutionPlan` — the sealed, hash-bound executable
  plan materialized from a registered campaign authority (#581 spatial
  design or #813 preregistration, via ``campaign_ref``). The full run
  queue is embedded in the sealed payload, so the exact set of required
  runs is pinned by the plan hash — no run can appear, vanish or be
  reordered without a new plan identity.
- :func:`materialize_run_queue` — deterministic queue generation:
  position-major, then channel, then role, then repetition index. The
  first run at each position carries ``requires_position_confirmation``.
- :class:`PositionConfirmation` ladder — ``operator_attest`` produces
  *attested* evidence, ``coordinate_entry``/``survey_import`` produce
  *entered* evidence, ``tracked_fixture``/``position_tracker`` produce
  *measured* evidence. A clicked confirmation is never equivalent to
  measured position evidence, and the evidence quality is derived from
  the method, never claimed.
- :func:`evaluate_retry_decision` — the deterministic automation policy:
  retries only when policy explicitly permits (clipping once at reduced
  level, transient device failure), never auto-retries a holdout after
  policy-invalidating state drift, stops the campaign after repeated
  quality failures, and requires human intervention on unexpected
  routing/device changes.
- :func:`derive_campaign_state` — a pure fold of the sealed event
  journal + run records into per-entry state, position gates, progress
  counts and the campaign outcome. Completion requires every required
  entry to reach an allowed terminal state; nothing is ever skipped
  silently.
- :class:`MeasurementCampaignRunner` — the driver: executes the #869
  :class:`MeasurementAcquisitionEngine` per queue entry, gates every
  capture through the quality verdict immediately, persists one sealed
  run record per attempt (failed attempts remain visible forever),
  pauses only for physical steps, resumes automatically on position
  confirmation, and recovers from restart by folding the journal.

Calibration vs holdout (#813): a run's ``MeasurementRole`` is bound at
queue materialization and carried immutably into every run record —
roles can never be crossed or rebound, and holdout entries lose their
auto-retry privilege after state drift.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .cad_owned_room_campaign import MeasurementRole
from .cad_scene import Position3
from .cad_sweep_acquisition import (
    AcquisitionQualityReason,
    AcquisitionQualityVerdict,
    AcquisitionRequest,
    ArmBlockedError,
    ArmConfirmation,
    ChannelRouting,
    CalibrationBindingState,
    LevelSafetyPolicy,
    MeasurementAcquisitionEngine,
    QualityGateThresholds,
    SweepStimulusSpec,
)
from .cad_delegated_provider import _require_iso8601, _require_refs, _seal
from .clock import utc_now_iso as _utc_now


CAMPAIGN_EXECUTION_AUTHORITY_VERSION = 'rev67-875-campaign-execution-1'
CAMPAIGN_EXECUTION_SCHEMA_VERSION = 'rev67-875-1'

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


# ---------------------------------------------------------------------------
# Errors — every runner/policy rejection is one of these so the error
# boundary renders deterministic operator-facing text.
# ---------------------------------------------------------------------------


class CampaignExecutionError(ValueError):
    """Base class for campaign-execution domain rejections."""


class CampaignPlanError(CampaignExecutionError):
    """The plan could not be materialized or is internally inconsistent."""


class CampaignStateError(CampaignExecutionError):
    """Illegal action against the derived campaign state."""


class PositionConfirmationError(CampaignExecutionError):
    """A position confirmation did not satisfy the gate."""


# ---------------------------------------------------------------------------
# vocabulary
# ---------------------------------------------------------------------------

PositionConfirmationMethod = Literal[
    'operator_attest',
    'coordinate_entry',
    'survey_import',
    'tracked_fixture',
    'position_tracker',
]
"""Progressively stronger position confirmation (#875 §Position
confirmation). Later entries carry position evidence; a bare operator
attestation does not."""

PositionEvidenceQuality = Literal['attested', 'entered', 'measured']

#: The evidence quality a method can produce — derived, never claimed.
CONFIRMATION_EVIDENCE_QUALITY: dict[str, PositionEvidenceQuality] = {
    'operator_attest': 'attested',
    'coordinate_entry': 'entered',
    'survey_import': 'entered',
    'tracked_fixture': 'measured',
    'position_tracker': 'measured',
}

#: Methods that must carry a reported Position3.
_CONFIRMATION_METHODS_REQUIRING_POSITION = frozenset(
    {'coordinate_entry', 'survey_import', 'tracked_fixture',
     'position_tracker'})

CampaignEntryState = Literal[
    'pending',
    'awaiting_position',
    'in_progress',
    'interrupted',
    'blocked',
    'completed',
    'failed',
    'waived',
]

ENTRY_TERMINAL_STATES: frozenset[str] = frozenset(
    {'completed', 'failed', 'waived'})

CampaignEventKind = Literal[
    'plan_registered',
    'position_gate_opened',
    'position_confirmed',
    'run_started',
    'run_recorded',
    'retry_scheduled',
    'entry_completed',
    'entry_failed',
    'entry_waived',
    'routing_change_reported',
    'human_intervention_required',
    'paused',
    'resumed',
    'cancelled',
    'restart_recovered',
    'policy_stop',
    'campaign_completed',
    'campaign_failed',
]

CampaignActor = Literal['machine', 'operator', 'system']

RetryDecision = Literal[
    'retry',
    'retry_reduced_level',
    'fail_entry',
    'stop_campaign',
    'require_human',
]

CampaignOutcome = Literal[
    'in_progress',
    'awaiting_position',
    'paused',
    'blocked',
    'completed',
    'completed_with_failures',
    'failed',
    'cancelled',
]

CAMPAIGN_TERMINAL_OUTCOMES: frozenset[str] = frozenset(
    {'completed', 'completed_with_failures', 'failed', 'cancelled'})

#: Terminal states that satisfy a required entry by default. 'failed' is
#: terminal but never satisfies a required run; 'waived' only when the
#: policy explicitly allows waiving required entries.
ALLOWED_REQUIRED_TERMINAL: frozenset[str] = frozenset({'completed'})

CampaignRunOutcome = Literal['completed', 'failed', 'cancelled']

#: Why an attempt failed — distinct from the engine's quality reasons so
#: pre-engine failures (precheck/arm) are honest too.
CampaignRunFailureKind = Literal[
    'precheck_blocked',
    'arm_confirmation_unavailable',
    'arm_blocked',
    'capture_failed',
    'quality_invalid',
    'cancelled',
]


CAMPAIGN_ENTRY_STATE_LABELS: dict[str, str] = {
    'pending': '待機中',
    'awaiting_position': 'マイク移動待ち',
    'in_progress': '測定中',
    'interrupted': '中断（再開待ち）',
    'blocked': '人手介入待ち',
    'completed': '完了',
    'failed': '失敗',
    'waived': '免除',
}

CAMPAIGN_EVENT_LABELS: dict[str, str] = {
    'plan_registered': '実行計画を登録',
    'position_gate_opened': 'マイク移動が必要',
    'position_confirmed': '位置確認済み',
    'run_started': '測定開始',
    'run_recorded': '測定結果を記録',
    'retry_scheduled': '再測定を予定',
    'entry_completed': '実行項目完了',
    'entry_failed': '実行項目失敗',
    'entry_waived': '実行項目を免除',
    'routing_change_reported': '配線/デバイス変更を検知',
    'human_intervention_required': '人手介入が必要',
    'paused': '一時停止',
    'resumed': '再開',
    'cancelled': '中止',
    'restart_recovered': '再起動から復帰',
    'policy_stop': 'ポリシーにより停止',
    'campaign_completed': 'キャンペーン完了',
    'campaign_failed': 'キャンペーン失敗',
}

CAMPAIGN_OUTCOME_LABELS: dict[str, str] = {
    'in_progress': '実行中',
    'awaiting_position': 'マイク移動待ち',
    'paused': '一時停止中',
    'blocked': '介入待ち',
    'completed': '完了',
    'completed_with_failures': '一部失敗で完了',
    'failed': '失敗',
    'cancelled': '中止',
}

CONFIRMATION_METHOD_LABELS: dict[str, str] = {
    'operator_attest': 'オペレータ確認',
    'coordinate_entry': '座標入力',
    'survey_import': 'サーベイ点インポート',
    'tracked_fixture': 'エンコーダ付き治具',
    'position_tracker': '位置トラッカー',
}

CONFIRMATION_QUALITY_LABELS: dict[str, str] = {
    'attested': '申告のみ',
    'entered': '座標入力済み',
    'measured': '実測済み',
}

RETRY_DECISION_LABELS: dict[str, str] = {
    'retry': '再測定',
    'retry_reduced_level': 'レベルを下げて再測定',
    'fail_entry': '項目を失敗として確定',
    'stop_campaign': 'キャンペーンを停止',
    'require_human': '人手介入を要求',
}

CAMPAIGN_RUN_FAILURE_LABELS: dict[str, str] = {
    'precheck_blocked': '事前チェック不合格',
    'arm_confirmation_unavailable': 'アーム確認が取得できません',
    'arm_blocked': 'レベル安全ゲート不合格',
    'capture_failed': 'キャプチャ失敗',
    'quality_invalid': '品質ゲート不合格',
    'cancelled': '中止',
}


def campaign_entry_state_label(state: str) -> str:
    return CAMPAIGN_ENTRY_STATE_LABELS.get(state, state)


def campaign_event_label(kind: str) -> str:
    return CAMPAIGN_EVENT_LABELS.get(kind, kind)


def campaign_outcome_label(outcome: str) -> str:
    return CAMPAIGN_OUTCOME_LABELS.get(outcome, outcome)


def confirmation_method_label(method: str) -> str:
    return CONFIRMATION_METHOD_LABELS.get(method, method)


def confirmation_quality_label(quality: str) -> str:
    return CONFIRMATION_QUALITY_LABELS.get(quality, quality)


# ---------------------------------------------------------------------------
# Plan inputs — channel bindings, positions, automation policy
# ---------------------------------------------------------------------------


class CampaignChannelBinding(BaseModel):
    """One logical measurement channel bound to exact physical routing.

    Channel selection is never inferred at run time: the plan pins which
    playback/capture device and channel each logical channel maps to.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    channel_entity_id: str = Field(min_length=1)
    playback_device_id: str = Field(min_length=1)
    playback_channel: int = Field(ge=0)
    capture_device_id: str = Field(min_length=1)
    capture_channel: int = Field(ge=0)
    loopback_input_channel: int | None = Field(default=None, ge=0)

    def routing(self) -> ChannelRouting:
        return ChannelRouting(
            playback_device_id=self.playback_device_id,
            playback_channel=self.playback_channel,
            capture_device_id=self.capture_device_id,
            capture_channel=self.capture_channel,
            loopback_input_channel=self.loopback_input_channel,
        )


class CampaignPositionSpec(BaseModel):
    """One seat/measurement position the campaign must visit.

    ``declared_position`` is the planned coordinate — optional because a
    position may legitimately be named without coordinates ("seat 2").
    It is never treated as measured evidence: only a measured-quality
    ``PositionConfirmation`` can verify it.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    position_id: str = Field(min_length=1)
    declared_position: Position3 | None = None
    roles: tuple[MeasurementRole, ...] = Field(min_length=1)
    required: bool = True
    channel_entity_ids: tuple[str, ...] = ()
    orientation_ref: AuthorityRef | None = None
    note: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def _valid(self) -> 'CampaignPositionSpec':
        if len(set(self.roles)) != len(self.roles):
            raise ValueError('position roles must be unique')
        ids = self.channel_entity_ids
        if len(set(ids)) != len(ids) or any(not i for i in ids):
            raise ValueError('channel_entity_ids must be unique non-empty')
        _require_refs(self.orientation_ref)
        return self


class CampaignAutomationPolicy(BaseModel):
    """Explicit retry/stop policy — nothing auto-retries without it.

    Every retryable failure class is opt-in; the default policy retries
    nothing and stops the campaign after three consecutive quality
    failures.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    max_attempts_per_entry: int = Field(ge=1, le=8, default=2)
    retry_clipping_once_at_reduced_level: bool = False
    reduced_level_step_db: float = Field(gt=0.0, le=24.0, default=6.0)
    retry_transient_device_failure: bool = False
    holdout_retry_on_state_drift: bool = False
    stop_after_consecutive_quality_failures: int = Field(
        ge=1, le=16, default=3)
    require_human_on_unexpected_routing_change: bool = True
    allow_waived_required: bool = False
    position_reconfirm_each_run: bool = False
    position_tolerance_m: float = Field(gt=0.0, le=5.0, default=0.05)
    auto_arm: bool = False


class CampaignQueueEntry(BaseModel):
    """One materialized run in the queue — position/channel/role/
    repetition identity is immutable once the plan is sealed."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    entry_key: str = Field(min_length=1)
    ordinal: int = Field(ge=1)
    position_id: str = Field(min_length=1)
    channel_entity_id: str = Field(min_length=1)
    role: MeasurementRole
    run_index: int = Field(ge=1)
    required: bool
    requires_position_confirmation: bool


# ---------------------------------------------------------------------------
# The sealed executable plan
# ---------------------------------------------------------------------------


class CampaignExecutionPlan(BaseModel):
    """Sealed executable plan (``mcplan-`` prefix).

    The materialized queue is part of the sealed payload: the plan hash
    pins the exact set of required runs, their order, roles, channels and
    the automation policy applied to them. Re-deriving the queue from the
    same inputs must reproduce the sealed queue byte-identically.
    """

    model_config = ConfigDict(frozen=True)

    plan_id: str = Field(min_length=1)
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)

    authority_version: str = Field(min_length=1)
    campaign_ref: AuthorityRef
    scene_ref: AuthorityRef | None = None

    stimulus_template: SweepStimulusSpec
    channel_bindings: tuple[CampaignChannelBinding, ...] = Field(
        min_length=1)
    positions: tuple[CampaignPositionSpec, ...] = Field(min_length=1)
    repetitions_per_entry: int = Field(ge=1, le=16, default=1)
    policy: CampaignAutomationPolicy
    level_policy: LevelSafetyPolicy = Field(
        default_factory=LevelSafetyPolicy)
    requires_absolute_level: bool = False
    declared_synchronized: bool = False
    quality_thresholds: QualityGateThresholds = Field(
        default_factory=QualityGateThresholds)

    queue: tuple[CampaignQueueEntry, ...] = Field(min_length=1)
    generated_at_utc: str = Field(min_length=1)
    notes: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def _valid(self) -> 'CampaignExecutionPlan':
        _require_iso8601(self.generated_at_utc, 'generated_at_utc')
        _require_refs(self.campaign_ref, self.scene_ref)
        position_ids = [p.position_id for p in self.positions]
        if len(set(position_ids)) != len(position_ids):
            raise ValueError('plan positions must be unique')
        channel_ids = {c.channel_entity_id for c in self.channel_bindings}
        for pos in self.positions:
            unknown = set(pos.channel_entity_ids) - channel_ids
            if unknown:
                raise ValueError(
                    f'position {pos.position_id} names unbound channels: '
                    + ', '.join(sorted(unknown)))
        expected = materialize_run_queue(
            positions=self.positions,
            channel_ids=sorted(c.channel_entity_id
                               for c in self.channel_bindings),
            repetitions_per_entry=self.repetitions_per_entry,
            reconfirm_each_run=self.policy.position_reconfirm_each_run,
        )
        if list(self.queue) != list(expected):
            raise ValueError(
                'sealed queue does not match the materialized queue — '
                'the run plan is not reproducible')
        required = [e for e in self.queue if e.required]
        if not required:
            raise ValueError('plan requires at least one required run')
        if self.plan_sha256 != self._expected_sha():
            raise ValueError('campaign execution plan hash mismatch')
        if self.plan_id != f'mcplan-{self.plan_sha256[:24]}':
            raise ValueError('campaign execution plan id mismatch')
        return self

    def _expected_sha(self) -> str:
        from .canonical_json import canonical_sha256
        return canonical_sha256(self.identity_payload())

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'plan_id', 'plan_sha256'})

    @classmethod
    def create(cls, **fields: Any) -> 'CampaignExecutionPlan':
        return _seal(
            cls, fields, 'plan_id', 'plan_sha256', 'mcplan',
        )  # type: ignore[return-value]

    # -- views --------------------------------------------------------------

    @property
    def entry_count(self) -> int:
        return len(self.queue)

    def entry(self, entry_key: str) -> CampaignQueueEntry | None:
        return next(
            (e for e in self.queue if e.entry_key == entry_key), None)

    def channel(self, channel_entity_id: str) -> CampaignChannelBinding | None:
        return next(
            (c for c in self.channel_bindings
             if c.channel_entity_id == channel_entity_id), None)

    def position(self, position_id: str) -> CampaignPositionSpec | None:
        return next(
            (p for p in self.positions
             if p.position_id == position_id), None)

    def stimulus_for_level(self, level_dbfs: float) -> SweepStimulusSpec:
        """Rebuild the template at a different output level (retry path)."""

        payload = self.stimulus_template.model_dump(mode='python')
        payload['level_dbfs'] = level_dbfs
        return SweepStimulusSpec(**payload)


def materialize_run_queue(
    *,
    positions: Sequence[CampaignPositionSpec],
    channel_ids: Sequence[str],
    repetitions_per_entry: int,
    reconfirm_each_run: bool,
) -> tuple[CampaignQueueEntry, ...]:
    """Deterministic queue: POSITION -> CHANNEL -> ROLE -> REPETITION.

    Position order is the declared physical order so the operator moves
    the microphone once per stop. Within a position, channels iterate in
    the plan's sorted channel-id order, then each declared role, then the
    run repetition index. The first entry per position requires position
    confirmation (or every entry when the policy reconfirms each run).
    """

    entries: list[CampaignQueueEntry] = []
    ordinal = 0
    for pos in positions:
        position_channels = tuple(pos.channel_entity_ids) or tuple(
            channel_ids)
        first_for_position = True
        for channel_id in position_channels:
            for role in pos.roles:
                for run_index in range(1, repetitions_per_entry + 1):
                    ordinal += 1
                    gate = (
                        reconfirm_each_run or first_for_position
                    )
                    entries.append(CampaignQueueEntry(
                        entry_key=f'e{ordinal:04d}',
                        ordinal=ordinal,
                        position_id=pos.position_id,
                        channel_entity_id=channel_id,
                        role=role,
                        run_index=run_index,
                        required=pos.required,
                        requires_position_confirmation=gate,
                    ))
                    first_for_position = False
    return tuple(entries)


def build_campaign_execution_plan(
    *,
    document_id: str,
    campaign_ref: AuthorityRef,
    stimulus_template: SweepStimulusSpec,
    channel_bindings: Sequence[CampaignChannelBinding],
    positions: Sequence[CampaignPositionSpec],
    generated_at_utc: str,
    scene_ref: AuthorityRef | None = None,
    repetitions_per_entry: int = 1,
    policy: CampaignAutomationPolicy | None = None,
    level_policy: LevelSafetyPolicy | None = None,
    requires_absolute_level: bool = False,
    declared_synchronized: bool = False,
    quality_thresholds: QualityGateThresholds | None = None,
    notes: str | None = None,
) -> CampaignExecutionPlan:
    """Materialize + seal an executable plan from a registered campaign."""

    if not positions:
        raise CampaignPlanError('a campaign needs at least one position')
    if not channel_bindings:
        raise CampaignPlanError('a campaign needs at least one channel')
    chan_ids = [c.channel_entity_id for c in channel_bindings]
    if len(set(chan_ids)) != len(chan_ids):
        raise CampaignPlanError('channel bindings must be unique')
    position_ids = [p.position_id for p in positions]
    if len(set(position_ids)) != len(position_ids):
        raise CampaignPlanError('positions must be unique')
    unknown = {
        c for p in positions for c in p.channel_entity_ids
    } - set(chan_ids)
    if unknown:
        raise CampaignPlanError(
            'positions name unbound channels: ' + ', '.join(sorted(unknown)))
    problems = stimulus_template.validate_geometry()
    if problems:
        raise CampaignPlanError(
            'stimulus template invalid: ' + '; '.join(problems))
    eff_policy = policy or CampaignAutomationPolicy()
    eff_level = level_policy or LevelSafetyPolicy()
    if stimulus_template.level_dbfs > eff_level.max_output_level_dbfs:
        raise CampaignPlanError(
            'stimulus level exceeds the level-safety policy ceiling')
    queue = materialize_run_queue(
        positions=positions,
        channel_ids=sorted(chan_ids),
        repetitions_per_entry=repetitions_per_entry,
        reconfirm_each_run=eff_policy.position_reconfirm_each_run,
    )
    if not any(e.required for e in queue):
        raise CampaignPlanError('the queue contains no required run')
    try:
        return CampaignExecutionPlan.create(
            document_id=document_id,
            authority_version=CAMPAIGN_EXECUTION_AUTHORITY_VERSION,
            campaign_ref=campaign_ref.model_dump(mode='python'),
            scene_ref=(
                scene_ref.model_dump(mode='python')
                if scene_ref is not None else None),
            stimulus_template=stimulus_template.model_dump(mode='python'),
            channel_bindings=[c.model_dump(mode='python')
                              for c in channel_bindings],
            positions=[p.model_dump(mode='python') for p in positions],
            repetitions_per_entry=repetitions_per_entry,
            policy=eff_policy.model_dump(mode='python'),
            level_policy=eff_level.model_dump(mode='python'),
            requires_absolute_level=requires_absolute_level,
            declared_synchronized=declared_synchronized,
            quality_thresholds=(
                quality_thresholds or QualityGateThresholds()),
            queue=[e.model_dump(mode='python') for e in queue],
            generated_at_utc=generated_at_utc,
            notes=notes,
        )
    except ValueError as exc:
        raise CampaignPlanError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Position confirmation ladder
# ---------------------------------------------------------------------------


class PositionConfirmation(BaseModel):
    """An operator/system claim that the microphone is at the position.

    ``evidence_quality`` is derived from ``method`` — an attestation can
    never present itself as measured evidence.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    position_id: str = Field(min_length=1)
    method: PositionConfirmationMethod
    reported_position: Position3 | None = None
    actor: CampaignActor = 'operator'
    at_utc: str = Field(min_length=1)
    note: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def _valid(self) -> 'PositionConfirmation':
        _require_iso8601(self.at_utc, 'at_utc')
        if self.method in _CONFIRMATION_METHODS_REQUIRING_POSITION \
                and self.reported_position is None:
            raise ValueError(
                f'{self.method} requires a reported_position')
        return self

    @property
    def evidence_quality(self) -> PositionEvidenceQuality:
        return CONFIRMATION_EVIDENCE_QUALITY[self.method]

    @property
    def measured(self) -> bool:
        return self.evidence_quality == 'measured'


def evaluate_position_confirmation(
    confirmation: PositionConfirmation,
    spec: CampaignPositionSpec,
    tolerance_m: float,
) -> tuple[str, ...]:
    """Gate checks — returns rejection reasons (empty = accepted).

    Coordinate checks apply only when BOTH the plan and the
    confirmation carry coordinates; an attestation has nothing to check
    and that asymmetry is exactly why it is not measured evidence.
    """

    problems: list[str] = []
    if confirmation.position_id != spec.position_id:
        problems.append('confirmation names a different position')
        return tuple(problems)
    reported = confirmation.reported_position
    declared = spec.declared_position
    if reported is not None and declared is not None:
        distance = math.sqrt(
            (reported.x_m - declared.x_m) ** 2
            + (reported.y_m - declared.y_m) ** 2
            + (reported.z_m - declared.z_m) ** 2)
        if distance > tolerance_m:
            problems.append(
                f'reported position is {distance:.3f} m from the declared '
                f'position (tolerance {tolerance_m:.3f} m)')
    return tuple(problems)


# ---------------------------------------------------------------------------
# Retry / stop policy evaluation — pure and deterministic
# ---------------------------------------------------------------------------

_TRANSIENT_REASONS = frozenset(
    {'device_lost', 'open_failed', 'backend_unavailable'})


@dataclass(frozen=True)
class RetryEvaluation:
    decision: RetryDecision
    reason: str
    reduced_level_dbfs: float | None = None


def evaluate_retry_decision(
    *,
    policy: CampaignAutomationPolicy,
    entry: CampaignQueueEntry,
    attempt_level_dbfs: float,
    attempts_used: int,
    quality_reasons: Sequence[str],
    routing_drift: bool,
    consecutive_quality_failures: int,
    clipping_retry_used: bool,
) -> RetryEvaluation:
    """Decide what happens after a failed/cancelled attempt.

    Deterministic precedence: exhaustion, holdout-drift guard, repeated-
    failure stop, then the opt-in retries, then terminal failure.
    """

    reasons = frozenset(quality_reasons)
    if attempts_used >= policy.max_attempts_per_entry:
        return RetryEvaluation(
            'fail_entry',
            f'attempt budget exhausted ({attempts_used}/'
            f'{policy.max_attempts_per_entry})')
    if (entry.role == 'holdout' and routing_drift
            and not policy.holdout_retry_on_state_drift):
        return RetryEvaluation(
            'fail_entry',
            'holdout run after state drift — auto-retry denied by policy')
    if (consecutive_quality_failures
            >= policy.stop_after_consecutive_quality_failures):
        return RetryEvaluation(
            'stop_campaign',
            f'{consecutive_quality_failures} consecutive quality '
            'failures reached the policy stop')
    if 'cancelled' in reasons:
        return RetryEvaluation(
            'fail_entry', 'cancelled attempts are never retried')
    if (policy.retry_clipping_once_at_reduced_level
            and 'clipping_detected' in reasons
            and not clipping_retry_used):
        reduced = attempt_level_dbfs - policy.reduced_level_step_db
        return RetryEvaluation(
            'retry_reduced_level',
            f'clipping — retry once at {reduced:g} dBFS '
            f'(-{policy.reduced_level_step_db:g} dB)',
            reduced_level_dbfs=reduced)
    if (policy.retry_transient_device_failure
            and reasons & _TRANSIENT_REASONS):
        return RetryEvaluation(
            'retry',
            'transient device/backend failure — policy permits one retry')
    return RetryEvaluation(
        'fail_entry',
        'no applicable retry policy: ' + ','.join(sorted(reasons))
        if reasons else 'no applicable retry policy')


# ---------------------------------------------------------------------------
# Derived state — a pure fold over the sealed journal + run records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntryExecutionState:
    entry_key: str
    state: CampaignEntryState
    attempts: int
    clipping_retry_used: bool
    run_record_refs: tuple[str, ...]
    last_outcome: CampaignRunOutcome | None
    last_quality_verdict: AcquisitionQualityVerdict | None
    waived_by: str | None


@dataclass(frozen=True)
class PositionGateState:
    position_id: str
    confirmed: bool
    confirmations: tuple[tuple[str, str, str], ...]  # (method, quality, at)
    last_reported_position: Position3 | None


@dataclass(frozen=True)
class CampaignProgress:
    """Operator-facing counts — every run is accounted for; nothing is
    silently skipped."""

    total: int
    completed: int
    failed: int
    waived: int
    in_progress: int
    awaiting_position: int
    blocked: int
    pending: int
    remaining: int
    required_total: int
    required_remaining: int
    measured_confirmations: int
    attested_confirmations: int


@dataclass(frozen=True)
class CampaignExecutionState:
    outcome: CampaignOutcome
    entries: dict[str, EntryExecutionState]
    position_gates: dict[str, PositionGateState]
    progress: CampaignProgress
    awaiting_position_id: str | None
    blocked_reason: str | None
    consecutive_quality_failures: int
    routing_drift: bool
    paused: bool
    interrupted_entries: tuple[str, ...]
    event_count: int


def _empty_entry_state() -> EntryExecutionState:
    return EntryExecutionState(
        entry_key='', state='pending', attempts=0,
        clipping_retry_used=False, run_record_refs=(),
        last_outcome=None, last_quality_verdict=None, waived_by=None)


def derive_campaign_state(
    plan: CampaignExecutionPlan,
    events: Sequence[Any],  # CadCampaignExecutionEvent
    run_records: Sequence[Any],  # CadCampaignRunRecord
) -> CampaignExecutionState:
    """Fold the event journal + run records into campaign state.

    Pure: same inputs always produce the same state, so a restarted
    application resumes exactly where the journal left off.
    """

    entries: dict[str, EntryExecutionState] = {}
    gates: dict[str, PositionGateState] = {}
    interrupted: list[str] = []
    drift = False
    drift_block = False
    paused = False
    cancelled = False
    human_required: str | None = None
    policy_stopped = False
    final: CampaignOutcome | None = None

    records_by_entry: dict[str, list[Any]] = {}
    for record in run_records:
        records_by_entry.setdefault(record.entry_key, []).append(record)

    def base_state(entry: CampaignQueueEntry) -> EntryExecutionState:
        records = records_by_entry.get(entry.entry_key, [])
        refs = tuple(r.run_record_id for r in records)
        clip_used = any(
            r.level_dbfs < plan.stimulus_template.level_dbfs
            for r in records)
        last = records[-1] if records else None
        return EntryExecutionState(
            entry_key=entry.entry_key, state='pending',
            attempts=len(records), clipping_retry_used=clip_used,
            run_record_refs=refs,
            last_outcome=(last.outcome if last is not None else None),
            last_quality_verdict=(
                last.quality_verdict if last is not None else None),
            waived_by=None)

    for entry in plan.queue:
        entries[entry.entry_key] = base_state(entry)
        gates.setdefault(entry.position_id, PositionGateState(
            position_id=entry.position_id, confirmed=False,
            confirmations=(), last_reported_position=None))

    def set_entry(key: str, **kw: Any) -> None:
        prev = entries[key]
        entries[key] = EntryExecutionState(**{
            'entry_key': key,
            'state': kw.get('state', prev.state),
            'attempts': kw.get('attempts', prev.attempts),
            'clipping_retry_used': kw.get(
                'clipping_retry_used', prev.clipping_retry_used),
            'run_record_refs': kw.get(
                'run_record_refs', prev.run_record_refs),
            'last_outcome': kw.get('last_outcome', prev.last_outcome),
            'last_quality_verdict': kw.get(
                'last_quality_verdict', prev.last_quality_verdict),
            'waived_by': kw.get('waived_by', prev.waived_by),
        })

    awaiting: str | None = None
    for event in events:
        kind = event.kind
        key = event.entry_key
        if kind == 'position_gate_opened':
            if key is not None:
                set_entry(key, state='awaiting_position')
                target = plan.entry(key)
                awaiting = (target.position_id
                            if target is not None else None)
        elif kind == 'position_confirmed':
            pid = getattr(event, 'position_id', None) or ''
            gate = gates.get(pid)
            if gate is not None:
                method = getattr(event, 'confirmation_method', None) or ''
                quality = getattr(
                    event, 'confirmation_evidence_quality', None) or ''
                gates[pid] = PositionGateState(
                    position_id=pid, confirmed=True,
                    confirmations=gate.confirmations
                    + ((method, quality, event.at_utc),),
                    last_reported_position=getattr(
                        event, 'reported_position', None))
                if awaiting == pid:
                    awaiting = None
                for e in plan.queue:
                    st = entries[e.entry_key]
                    if e.position_id == pid and st.state == 'awaiting_position':
                        set_entry(e.entry_key, state='pending')
        elif kind == 'run_started':
            if key is not None:
                set_entry(key, state='in_progress')
        elif kind == 'run_recorded':
            # Terminal attempt evidence lands via the run record; the
            # entry stays in_progress until a decision event.
            pass
        elif kind == 'retry_scheduled':
            if key is not None:
                set_entry(key, state='pending')
        elif kind == 'entry_completed':
            if key is not None:
                set_entry(key, state='completed')
        elif kind == 'entry_failed':
            if key is not None:
                set_entry(key, state='failed')
        elif kind == 'entry_waived':
            if key is not None:
                set_entry(key, state='waived', waived_by=event.actor)
        elif kind == 'routing_change_reported':
            drift = True
            drift_block = True
        elif kind == 'human_intervention_required':
            human_required = event.reason
        elif kind == 'paused':
            paused = True
        elif kind == 'resumed':
            paused = False
            human_required = None
            drift_block = False
        elif kind == 'cancelled':
            cancelled = True
        elif kind == 'restart_recovered':
            for e in plan.queue:
                if entries[e.entry_key].state == 'in_progress':
                    set_entry(e.entry_key, state='pending')
                    interrupted.append(e.entry_key)
        elif kind == 'policy_stop':
            policy_stopped = True
        elif kind == 'campaign_completed':
            final = 'completed'
        elif kind == 'campaign_failed':
            final = 'failed'

    # Consecutive quality failures: the trailing run of failed/invalid
    # attempts in chronological order; a passing run resets it and a
    # cancelled attempt is neutral (it is not a quality judgement).
    chronological = sorted(
        (r for r in run_records),
        key=lambda r: (r.completed_at_utc, r.run_record_id))
    consecutive = 0
    for record in chronological:
        if record.outcome == 'completed' \
                and record.quality_verdict in ('valid', 'limited'):
            consecutive = 0
        elif record.outcome == 'failed' \
                or record.quality_verdict == 'invalid':
            consecutive += 1

    counts = {'completed': 0, 'failed': 0, 'waived': 0, 'in_progress': 0,
              'awaiting_position': 0, 'blocked': 0, 'pending': 0,
              'interrupted': 0}
    allowed_required = set(ALLOWED_REQUIRED_TERMINAL)
    if plan.policy.allow_waived_required:
        # 'waived' satisfies a required run only under the explicit
        # opt-in — the waiver is journaled evidence, never silent.
        allowed_required.add('waived')
    required_remaining = 0
    required_total = 0
    for entry in plan.queue:
        state = entries[entry.entry_key].state
        counts[state] = counts.get(state, 0) + 1
        if entry.required:
            required_total += 1
            if state not in allowed_required:
                required_remaining += 1
    remaining = sum(
        counts.get(s, 0) for s in
        ('pending', 'awaiting_position', 'in_progress', 'interrupted'))
    measured = sum(
        1 for g in gates.values()
        for _m, q, _t in g.confirmations if q == 'measured')
    attested = sum(
        1 for g in gates.values()
        for _m, q, _t in g.confirmations if q != 'measured')
    progress = CampaignProgress(
        total=len(plan.queue),
        completed=counts['completed'],
        failed=counts['failed'],
        waived=counts['waived'],
        in_progress=counts['in_progress'],
        awaiting_position=counts['awaiting_position'],
        blocked=counts['blocked'],
        pending=counts['pending'] + counts['interrupted'],
        remaining=remaining,
        required_total=required_total,
        required_remaining=required_remaining,
        measured_confirmations=measured,
        attested_confirmations=attested,
    )

    if final == 'completed':
        # The event kind only marks a successful finish; whether it was
        # clean or scarred is derived from the entries — a failed run
        # can never be folded away by the terminal marker.
        outcome = 'completed_with_failures' if counts['failed'] \
            else 'completed'
    elif final is not None:
        outcome = final
    elif cancelled:
        outcome = 'cancelled'
    elif policy_stopped:
        outcome = 'failed'
    elif human_required is not None or (
            drift_block
            and plan.policy.require_human_on_unexpected_routing_change):
        outcome = 'blocked'
    elif paused:
        outcome = 'paused'
    elif awaiting is not None:
        outcome = 'awaiting_position'
    elif remaining == 0:
        any_failure = counts['failed'] > 0
        if required_remaining:
            outcome = 'failed'
        elif any_failure:
            outcome = 'completed_with_failures'
        else:
            outcome = 'completed'
    else:
        outcome = 'in_progress'

    return CampaignExecutionState(
        outcome=outcome,
        entries=entries,
        position_gates=gates,
        progress=progress,
        awaiting_position_id=awaiting,
        blocked_reason=human_required or (
            'unexpected routing/device change reported' if drift else None),
        consecutive_quality_failures=consecutive,
        routing_drift=drift,
        paused=paused,
        interrupted_entries=tuple(interrupted),
        event_count=len(events),
    )


__all__ = [
    'ALLOWED_REQUIRED_TERMINAL',
    'CAMPAIGN_ENTRY_STATE_LABELS',
    'CAMPAIGN_EVENT_LABELS',
    'CAMPAIGN_EXECUTION_AUTHORITY_VERSION',
    'CAMPAIGN_EXECUTION_SCHEMA_VERSION',
    'CAMPAIGN_OUTCOME_LABELS',
    'CAMPAIGN_RUN_FAILURE_LABELS',
    'CAMPAIGN_TERMINAL_OUTCOMES',
    'CONFIRMATION_METHOD_LABELS',
    'CONFIRMATION_QUALITY_LABELS',
    'CONFIRMATION_EVIDENCE_QUALITY',
    'RETRY_DECISION_LABELS',
    'CampaignActor',
    'CampaignAutomationPolicy',
    'CampaignChannelBinding',
    'CampaignEntryState',
    'CampaignEventKind',
    'CampaignExecutionError',
    'CampaignExecutionPlan',
    'CampaignExecutionState',
    'CampaignOutcome',
    'CampaignPlanError',
    'CampaignPositionSpec',
    'CampaignProgress',
    'CampaignQueueEntry',
    'CampaignRunOutcome',
    'CampaignRunFailureKind',
    'CampaignStateError',
    'EntryExecutionState',
    'ENTRY_TERMINAL_STATES',
    'PositionConfirmation',
    'PositionConfirmationError',
    'PositionConfirmationMethod',
    'PositionEvidenceQuality',
    'PositionGateState',
    'RetryDecision',
    'RetryEvaluation',
    'build_campaign_execution_plan',
    'campaign_entry_state_label',
    'campaign_event_label',
    'campaign_outcome_label',
    'confirmation_method_label',
    'confirmation_quality_label',
    'derive_campaign_state',
    'evaluate_position_confirmation',
    'evaluate_retry_decision',
    'materialize_run_queue',
]
