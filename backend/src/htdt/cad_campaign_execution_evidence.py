"""Sealed evidence records for #875 campaign execution (REV67).

Two append-only record families:

- :class:`CadCampaignExecutionEvent` (``mcevt-``) — the campaign journal.
  Every state change the runner makes (gates opened, confirmations,
  attempts started/recorded, retry decisions, pause/resume/cancel,
  restart recovery, policy stops) lands here exactly once, ordered by
  ``seq``. Folding this journal reproduces campaign state
  deterministically — that is what makes restart recovery exact.
- :class:`CadCampaignRunRecord` (``mcrun-``) — one record per capture
  attempt, binding the run to its immutable queue identity
  (position/channel/role/repetition), the exact routing and output
  level used, and — when the attempt reached the engine — the sealed
  #869 acquisition run and stimulus refs. Failed and cancelled attempts
  are written the same way and remain visible forever; nothing ever
  overwrites a recorded attempt.

Both records re-check their own ``<sha256>`` at validation time, so a
tampered record fails closed instead of reading as success.
"""

from __future__ import annotations

from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .cad_campaign_execution import (
    CAMPAIGN_EXECUTION_SCHEMA_VERSION,
    CampaignActor,
    CampaignEventKind,
    CampaignQueueEntry,
    CampaignExecutionPlan,
    CampaignRunFailureKind,
    CampaignRunOutcome,
    PositionConfirmationMethod,
    PositionEvidenceQuality,
    RetryDecision,
)
from .cad_delegated_provider import _require_iso8601, _require_refs, _seal
from .cad_owned_room_campaign import MeasurementRole
from .cad_scene import Position3
from .cad_sweep_acquisition import (
    AcquisitionQualityVerdict,
    ChannelRouting,
    MeasurementAcquisitionEngine,
)

_SHA256_PATTERN = r'^[0-9a-f]{64}$'


class CadCampaignExecutionEvent(BaseModel):
    """One sealed journal event. ``seq`` orders the journal inside a
    plan; ``plan_sha256`` pins the plan revision the event belongs to,
    so events can never be replayed against a different plan."""

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1)
    event_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)

    plan_id: str = Field(min_length=1)
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    seq: int = Field(ge=1)
    kind: CampaignEventKind
    at_utc: str = Field(min_length=1)
    actor: CampaignActor

    entry_key: str | None = None
    attempt: int | None = Field(default=None, ge=1)
    position_id: str | None = None
    reason: str = ''

    retry_decision: RetryDecision | None = None
    reduced_level_dbfs: float | None = None

    confirmation_method: PositionConfirmationMethod | None = None
    confirmation_evidence_quality: PositionEvidenceQuality | None = None
    reported_position: Position3 | None = None

    outcome: CampaignRunOutcome | None = None
    quality_verdict: AcquisitionQualityVerdict | None = None
    quality_reasons: tuple[str, ...] = ()
    interrupted_entry_keys: tuple[str, ...] = ()
    evidence_refs: tuple[AuthorityRef, ...] = ()
    notes: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def _valid(self) -> 'CadCampaignExecutionEvent':
        _require_iso8601(self.at_utc, 'at_utc')
        _require_refs(*self.evidence_refs)
        if self.kind == 'position_confirmed':
            if (self.confirmation_method is None
                    or self.confirmation_evidence_quality is None
                    or self.position_id is None):
                raise ValueError(
                    'position_confirmed events carry the method, its '
                    'evidence quality and the position id')
        if self.kind in ('run_started', 'run_recorded', 'retry_scheduled',
                         'entry_completed', 'entry_failed',
                         'entry_waived'):
            if not self.entry_key:
                raise ValueError(f'{self.kind} events carry entry_key')
        if self.kind == 'retry_scheduled' and self.retry_decision is None:
            raise ValueError('retry_scheduled events carry a decision')
        if self.kind == 'run_recorded' and self.outcome is None:
            raise ValueError('run_recorded events carry the outcome')
        if self.event_sha256 != self._expected_sha():
            raise ValueError('campaign execution event hash mismatch')
        if self.event_id != f'mcevt-{self.event_sha256[:24]}':
            raise ValueError('campaign execution event id mismatch')
        return self

    def _expected_sha(self) -> str:
        from .canonical_json import canonical_sha256
        return canonical_sha256(self.identity_payload())

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'event_id', 'event_sha256'})

    @classmethod
    def create(cls, **fields: Any) -> 'CadCampaignExecutionEvent':
        return _seal(
            cls, fields, 'event_id', 'event_sha256', 'mcevt',
        )  # type: ignore[return-value]


class CadCampaignRunRecord(BaseModel):
    """Sealed per-attempt record (``mcrun-``).

    ``entry_key`` + ``attempt`` identify the attempt inside the sealed
    plan queue; position/channel/role/run_index are copied from the
    queue entry (and verified by the repository) so the record itself
    carries the full run identity even when read standalone.
    """

    model_config = ConfigDict(frozen=True)

    run_record_id: str = Field(min_length=1)
    run_record_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_id: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)

    plan_id: str = Field(min_length=1)
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    entry_key: str = Field(min_length=1)
    entry_ordinal: int = Field(ge=1)
    attempt: int = Field(ge=1)
    position_id: str = Field(min_length=1)
    channel_entity_id: str = Field(min_length=1)
    role: MeasurementRole
    run_index: int = Field(ge=1)
    required: bool

    playback_device_id: str = Field(min_length=1)
    playback_channel: int = Field(ge=0)
    capture_device_id: str = Field(min_length=1)
    capture_channel: int = Field(ge=0)
    level_dbfs: float = Field(le=0.0)

    stimulus_ref: AuthorityRef | None = None
    acquisition_ref: AuthorityRef | None = None
    position_ref: AuthorityRef | None = None
    position_confirmation_event_id: str | None = None

    engine_run_id: str | None = None
    engine_version: str | None = None
    backend_id: str | None = None
    backend_is_simulated: bool | None = None

    outcome: CampaignRunOutcome
    failure_kind: CampaignRunFailureKind | None = None
    quality_verdict: AcquisitionQualityVerdict | None = None
    quality_reasons: tuple[str, ...] = ()

    started_at_utc: str = Field(min_length=1)
    completed_at_utc: str = Field(min_length=1)
    notes: tuple[str, ...] = ()

    @model_validator(mode='after')
    def _valid(self) -> 'CadCampaignRunRecord':
        _require_iso8601(self.started_at_utc, 'started_at_utc')
        _require_iso8601(self.completed_at_utc, 'completed_at_utc')
        if self.completed_at_utc < self.started_at_utc:
            raise ValueError('completed_at_utc precedes started_at_utc')
        _require_refs(
            self.stimulus_ref, self.acquisition_ref, self.position_ref)
        if self.outcome == 'completed':
            if self.quality_verdict is None:
                raise ValueError(
                    'completed attempts carry the quality verdict')
            if self.failure_kind is not None:
                raise ValueError('completed attempts carry no failure kind')
        else:
            if self.failure_kind is None:
                raise ValueError(
                    'failed/cancelled attempts carry a failure kind')
        if self.run_record_sha256 != self._expected_sha():
            raise ValueError('campaign run record hash mismatch')
        if self.run_record_id != f'mcrun-{self.run_record_sha256[:24]}':
            raise ValueError('campaign run record id mismatch')
        return self

    def _expected_sha(self) -> str:
        from .canonical_json import canonical_sha256
        return canonical_sha256(self.identity_payload())

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json', exclude={'run_record_id', 'run_record_sha256'})

    @classmethod
    def create(cls, **fields: Any) -> 'CadCampaignRunRecord':
        return _seal(
            cls, fields, 'run_record_id', 'run_record_sha256', 'mcrun',
        )  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Builders — the only way events/records should be produced
# ---------------------------------------------------------------------------


def build_campaign_event(
    *,
    document_id: str,
    plan: CampaignExecutionPlan,
    seq: int,
    kind: CampaignEventKind,
    at_utc: str,
    actor: CampaignActor = 'machine',
    entry_key: str | None = None,
    attempt: int | None = None,
    position_id: str | None = None,
    reason: str = '',
    retry_decision: RetryDecision | None = None,
    reduced_level_dbfs: float | None = None,
    confirmation_method: PositionConfirmationMethod | None = None,
    confirmation_evidence_quality: PositionEvidenceQuality | None = None,
    reported_position: Position3 | None = None,
    outcome: CampaignRunOutcome | None = None,
    quality_verdict: AcquisitionQualityVerdict | None = None,
    quality_reasons: Sequence[str] = (),
    interrupted_entry_keys: Sequence[str] = (),
    evidence_refs: Sequence[AuthorityRef] = (),
    notes: str | None = None,
) -> CadCampaignExecutionEvent:
    return CadCampaignExecutionEvent.create(
        document_id=document_id,
        schema_version=CAMPAIGN_EXECUTION_SCHEMA_VERSION,
        plan_id=plan.plan_id,
        plan_sha256=plan.plan_sha256,
        seq=seq,
        kind=kind,
        at_utc=at_utc,
        actor=actor,
        entry_key=entry_key,
        attempt=attempt,
        position_id=position_id,
        reason=reason,
        retry_decision=retry_decision,
        reduced_level_dbfs=reduced_level_dbfs,
        confirmation_method=confirmation_method,
        confirmation_evidence_quality=confirmation_evidence_quality,
        reported_position=(
            reported_position.model_dump(mode='python')
            if reported_position is not None else None),
        outcome=outcome,
        quality_verdict=quality_verdict,
        quality_reasons=tuple(quality_reasons),
        interrupted_entry_keys=tuple(interrupted_entry_keys),
        evidence_refs=[r.model_dump(mode='python')
                       for r in evidence_refs],
        notes=notes,
    )


def build_campaign_run_record(
    *,
    document_id: str,
    plan: CampaignExecutionPlan,
    entry: CampaignQueueEntry,
    attempt: int,
    routing: ChannelRouting,
    level_dbfs: float,
    outcome: CampaignRunOutcome,
    failure_kind: CampaignRunFailureKind | None,
    quality_verdict: AcquisitionQualityVerdict | None,
    quality_reasons: Sequence[str],
    started_at_utc: str,
    completed_at_utc: str,
    stimulus_ref: AuthorityRef | None = None,
    acquisition_ref: AuthorityRef | None = None,
    position_ref: AuthorityRef | None = None,
    position_confirmation_event_id: str | None = None,
    engine: MeasurementAcquisitionEngine | None = None,
    notes: Sequence[str] = (),
) -> CadCampaignRunRecord:
    backend = getattr(engine, 'backend', None)
    backend_id = getattr(backend, 'backend_id', None)
    simulated = (
        backend_id in ('fake-audio-io',)
        if backend_id is not None else None)
    return CadCampaignRunRecord.create(
        document_id=document_id,
        schema_version=CAMPAIGN_EXECUTION_SCHEMA_VERSION,
        plan_id=plan.plan_id,
        plan_sha256=plan.plan_sha256,
        entry_key=entry.entry_key,
        entry_ordinal=entry.ordinal,
        attempt=attempt,
        position_id=entry.position_id,
        channel_entity_id=entry.channel_entity_id,
        role=entry.role,
        run_index=entry.run_index,
        required=entry.required,
        playback_device_id=routing.playback_device_id,
        playback_channel=routing.playback_channel,
        capture_device_id=routing.capture_device_id,
        capture_channel=routing.capture_channel,
        level_dbfs=level_dbfs,
        stimulus_ref=(
            stimulus_ref.model_dump(mode='python')
            if stimulus_ref is not None else None),
        acquisition_ref=(
            acquisition_ref.model_dump(mode='python')
            if acquisition_ref is not None else None),
        position_ref=(
            position_ref.model_dump(mode='python')
            if position_ref is not None else None),
        position_confirmation_event_id=position_confirmation_event_id,
        engine_run_id=(engine.run_id if engine is not None else None),
        engine_version=(
            getattr(engine, 'engine_version', None)
            if engine is not None else None),
        backend_id=backend_id,
        backend_is_simulated=simulated,
        outcome=outcome,
        failure_kind=failure_kind,
        quality_verdict=quality_verdict,
        quality_reasons=tuple(quality_reasons),
        started_at_utc=started_at_utc,
        completed_at_utc=completed_at_utc,
        notes=tuple(notes),
    )


__all__ = [
    'CadCampaignExecutionEvent',
    'CadCampaignRunRecord',
    'build_campaign_event',
    'build_campaign_run_record',
]
