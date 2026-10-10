"""Calibration workflow UX backend (#452).

:class:`~htdt.cad_calibration.CadCalibrationPlan` and its lifecycle authority
already exist; this module is the thin workflow surface a native
Measurements / Optimize page drives without touching repository internals:

- a per-channel *review projection* of exactly what the plan computed —
  channel gains, delays, polarity, crossovers and PEQ — with support-state
  warnings surfaced before any export;
- deterministic JSON/CSV export through the existing generic-biquad adapter;
- an explicit *user-applied* transition: exporting settings is never the same
  as applying them. :class:`CadAppliedSettingsRecord` pins the exact export
  that was applied plus structured per-channel deviations when the user set
  something different than proposed (or on different channels), so
  "effective applied settings" is always derivable;
- verification flow: register a re-measure contract, record the
  re-measurement, and mark the plan validated — all bound to exact
  authority identities.

Device-neutral: nothing here knows about a specific AVR/processor, and
crossover values only ever come from the plan's own channels — no generic
80 Hz defaults are injected anywhere.
"""

from __future__ import annotations

from typing import Any, Literal, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..domain.cad_calibration import (
    CadCalibrationExportSnapshot,
    CadCalibrationLifecycleEvent,
    CadCalibrationPlan,
    CadVerificationMeasurementCompletion,
    CadVerificationMeasurementPlan,
    CadVerificationMeasurementPoint,
    CalibrationLifecycleState,
    MeasurementCapabilityClaim,
    build_calibration_lifecycle_event,
    build_generic_biquad_export,
    build_verification_measurement_completion,
    build_verification_measurement_plan,
    render_generic_biquad_csv,
    render_generic_biquad_json,
)
from ..persistence.cad_calibration_repository import CadCalibrationRepository
from ...canonical_json import canonical_json as _canonical_json, canonical_sha256 as _hash






class CalibrationChannelReview(BaseModel):
    """One channel row of the plan review — exactly what the plan proposes."""

    model_config = ConfigDict(frozen=True)

    channel_id: str = Field(min_length=1)
    role_id: str = Field(min_length=1)
    source_entity_id: str = Field(min_length=1)
    physical_output_id: str = Field(min_length=1)
    gain_db: float
    delay_ms: float
    polarity: Literal['normal', 'inverted']
    crossovers: tuple[dict[str, Any], ...] = ()
    peq_filters: tuple[dict[str, Any], ...] = ()
    routing: tuple[str, ...] = ()


class CalibrationPlanReview(BaseModel):
    """The reviewable projection a page renders before export."""

    model_config = ConfigDict(frozen=True)

    plan_id: str = Field(min_length=1)
    plan_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    sample_rate_hz: int = Field(gt=0)
    support_state: Literal['SUPPORTED', 'UNSUPPORTED']
    unsupported_reasons: tuple[str, ...] = ()
    channels: tuple[CalibrationChannelReview, ...] = Field(min_length=1)


class AppliedSettingsDeviation(BaseModel):
    """One explicit user deviation between exported and actually-applied."""

    model_config = ConfigDict(frozen=True)

    channel_id: str = Field(min_length=1)
    field_name: Literal[
        'gain_db', 'delay_s', 'polarity', 'crossover', 'peq', 'routing', 'level_db'
    ]
    exported_value_repr: str
    applied_value_repr: str
    reason: str | None = None


class CadAppliedSettingsRecord(BaseModel):
    """Structured 'effective applied settings' — export applied ≠ export.

    The record never restates settings the user applied exactly as exported;
    it pins the exact export (id + semantic hash) and lists only what the
    user deliberately set differently, so consumers can always reconstruct
    effective settings as ``export ⊕ deviations``.
    """

    model_config = ConfigDict(frozen=True)

    applied_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    calibration_plan_id: str = Field(min_length=1)
    calibration_plan_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    exported_settings_id: str = Field(min_length=1)
    exported_settings_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    applied_at_utc: str = Field(min_length=1)
    device_context: str | None = None
    deviations: tuple[AppliedSettingsDeviation, ...] = ()
    note: str | None = None
    applied_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_record(self) -> 'CadAppliedSettingsRecord':
        channel_field = tuple(
            (item.channel_id, item.field_name) for item in self.deviations
        )
        if len(channel_field) != len(set(channel_field)):
            raise ValueError('deviations must be unique per channel/field')
        if self.applied_sha256 != _hash(self.semantic_payload()):
            raise ValueError('CadAppliedSettingsRecord hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'applied_id': self.applied_id,
            'document_id': self.document_id,
            'calibration_plan_id': self.calibration_plan_id,
            'calibration_plan_semantic_sha256': self.calibration_plan_semantic_sha256,
            'exported_settings_id': self.exported_settings_id,
            'exported_settings_semantic_sha256': self.exported_settings_semantic_sha256,
            'applied_at_utc': self.applied_at_utc,
            'device_context': self.device_context,
            'deviations': [item.model_dump(mode='json') for item in self.deviations],
            'note': self.note,
        }


class CalibrationWorkflowExport(BaseModel):
    """Deterministic export bundle handed to the page for save-as."""

    model_config = ConfigDict(frozen=True)

    export: CadCalibrationExportSnapshot
    json_text: str = Field(min_length=1)
    csv_text: str = Field(min_length=1)


class CadCalibrationWorkflowService:
    """UI-facing lifecycle facade over :class:`CadCalibrationRepository`."""

    def __init__(
        self,
        repository: CadCalibrationRepository,
        applied_repository: Any | None = None,
        measurement_repository: Any | None = None,
        quality_repository: Any | None = None,
    ) -> None:
        self.repository = repository
        self.applied_repository = applied_repository
        self.measurement_repository = measurement_repository
        self.quality_repository = quality_repository

    # ------------------------------------------------------------------

    def review_plan(self, plan_id: str) -> CalibrationPlanReview:
        """Structured per-channel review of exactly what the plan proposes."""
        plan = self._plan(plan_id)
        rows = tuple(
            CalibrationChannelReview(
                channel_id=channel.channel_id,
                role_id=channel.role_id,
                source_entity_id=channel.source_entity_id,
                physical_output_id=channel.physical_output_id,
                gain_db=channel.gain_db,
                delay_ms=channel.delay_s * 1000.0,
                polarity=channel.polarity,
                crossovers=tuple(
                    item.model_dump(mode='json') for item in channel.crossovers
                ),
                peq_filters=tuple(
                    item.model_dump(mode='json') for item in channel.peq
                ),
                routing=channel.routing,
            )
            for channel in plan.channels
        )
        return CalibrationPlanReview(
            plan_id=plan.plan_id,
            plan_semantic_sha256=plan.plan_semantic_sha256,
            document_id=plan.document_id,
            scene_revision_id=plan.scene_revision_id,
            sample_rate_hz=plan.sample_rate_hz,
            support_state=plan.support_state,
            unsupported_reasons=plan.unsupported_reasons,
            channels=rows,
        )

    def export_settings(
        self,
        plan_id: str,
        *,
        created_at_utc: str,
    ) -> CalibrationWorkflowExport:
        """Deterministic generic-biquad export + 'exported' lifecycle fact.

        The export is authoritative output for the user to apply — it is NOT
        an applied state; calling ``mark_user_applied`` is required for that.
        """
        plan = self._plan(plan_id)
        snapshot = build_generic_biquad_export(
            plan=plan,
            created_at_utc=created_at_utc,
        )
        self.repository.save_export(snapshot)
        self._append_lifecycle(
            plan,
            'exported',
            created_at_utc=created_at_utc,
            exported_settings=snapshot,
            note='設定を出力（適用はユーザー操作待ち）',
        )
        return CalibrationWorkflowExport(
            export=snapshot,
            json_text=render_generic_biquad_json(snapshot),
            csv_text=render_generic_biquad_csv(snapshot),
        )

    def mark_user_applied(
        self,
        plan_id: str,
        *,
        export_id: str,
        applied_at_utc: str,
        deviations: Sequence[AppliedSettingsDeviation] = (),
        device_context: str | None = None,
        note: str | None = None,
    ) -> CadAppliedSettingsRecord:
        """Record the explicit user-applied transition.

        ``deviations`` declare what the user actually set differently than
        exported (or on different channels); an empty tuple means the export
        was applied verbatim.
        """
        plan = self._plan(plan_id)
        snapshot = self.repository.get_export(export_id)
        if snapshot is None:
            raise ValueError(f'unknown calibration export {export_id}')
        if snapshot.calibration_plan_id != plan.plan_id:
            raise ValueError('export does not belong to this calibration plan')
        if snapshot.requested_plan_semantic_sha256 != plan.plan_semantic_sha256:
            raise ValueError('export was rendered for a different plan revision')
        plan_channels = {channel.channel_id for channel in plan.channels}
        for deviation in deviations:
            if deviation.channel_id not in plan_channels:
                raise ValueError(
                    f'deviation references unknown channel {deviation.channel_id}'
                )
        payload: dict[str, Any] = {
            'applied_id': str(uuid4()),
            'document_id': plan.document_id,
            'calibration_plan_id': plan.plan_id,
            'calibration_plan_semantic_sha256': plan.plan_semantic_sha256,
            'exported_settings_id': snapshot.export_id,
            'exported_settings_semantic_sha256': snapshot.exported_settings_semantic_sha256,
            'applied_at_utc': applied_at_utc,
            'device_context': device_context,
            'deviations': tuple(deviations),
            'note': note,
        }
        provisional = CadAppliedSettingsRecord.model_construct(
            **payload, applied_sha256='0' * 64
        )
        record = CadAppliedSettingsRecord(
            **payload,
            applied_sha256=_hash(provisional.semantic_payload()),
        )
        if self.applied_repository is not None:
            self.applied_repository.save_applied(
                record, calibration_repository=self.repository
            )
        self._append_lifecycle(
            plan,
            'user_applied',
            created_at_utc=applied_at_utc,
            exported_settings=snapshot,
            note=note,
        )
        return record

    def plan_verification(
        self,
        plan_id: str,
        *,
        export_id: str,
        measurement_points: Sequence[CadVerificationMeasurementPoint],
        routing: Sequence[str],
        reference_level_db_spl: float,
        required_measurement_capabilities: Sequence[MeasurementCapabilityClaim],
        before_measurement_ids: Sequence[str],
        created_at_utc: str,
    ) -> CadVerificationMeasurementPlan:
        """Register the exact re-measure contract for a plan+export pair."""
        plan = self._plan(plan_id)
        snapshot = self.repository.get_export(export_id)
        if snapshot is None:
            raise ValueError(f'unknown calibration export {export_id}')
        verification = build_verification_measurement_plan(
            plan=plan,
            exported_settings=snapshot,
            measurement_points=measurement_points,
            routing=routing,
            reference_level_db_spl=reference_level_db_spl,
            required_measurement_capabilities=required_measurement_capabilities,
            before_measurement_ids=before_measurement_ids,
            created_at_utc=created_at_utc,
        )
        self.repository.save_verification_plan(verification)
        return verification

    def record_remeasurement(
        self,
        plan_id: str,
        *,
        verification_plan_id: str,
        export_id: str,
        measurement_ids: Sequence[str],
        created_at_utc: str,
        note: str | None = None,
    ) -> CadVerificationMeasurementCompletion:
        """Record the verification re-measurement as a persisted completion.

        The measurement ids must be honest after-evidence: measured captures
        on the contract's exact SceneRevision, taken after the durable
        registration timestamp. Non-qualifying ids are rejected by the
        completion authority — the lifecycle event can never outrun the
        evidence it claims.
        """
        plan, snapshot, verification = self._workflow_refs(
            plan_id, export_id, verification_plan_id
        )
        if self.measurement_repository is None or self.quality_repository is None:
            raise ValueError(
                'remeasurement requires measurement and quality repositories'
            )
        registration = self.repository.get_verification_plan_registration(
            verification.verification_plan_id
        )
        if registration is None:
            raise ValueError('verification plan has no durable registration')
        completion = build_verification_measurement_completion(
            verification=verification,
            registration=registration,
            after_measurement_ids=measurement_ids,
            measurement_repository=self.measurement_repository,
            quality_repository=self.quality_repository,
            completed_at_utc=created_at_utc,
        )
        self.repository.save_verification_completion(completion)
        self._append_lifecycle(
            plan,
            'remeasured',
            created_at_utc=created_at_utc,
            exported_settings=snapshot,
            verification=verification,
            measurement_ids=measurement_ids,
            note=note,
        )
        return completion

    def mark_validated(
        self,
        plan_id: str,
        *,
        verification_plan_id: str,
        export_id: str,
        measurement_ids: Sequence[str],
        created_at_utc: str,
        note: str | None = None,
    ) -> CadCalibrationLifecycleEvent:
        """Mark the applied settings as verified by re-measurement evidence."""
        plan, snapshot, verification = self._workflow_refs(
            plan_id, export_id, verification_plan_id
        )
        return self._append_lifecycle(
            plan,
            'validated',
            created_at_utc=created_at_utc,
            exported_settings=snapshot,
            verification=verification,
            measurement_ids=measurement_ids,
            note=note,
        )

    def lifecycle_state(self, plan_id: str) -> str:
        events = self.repository.list_lifecycle_events(plan_id)
        return events[-1].state if events else 'proposed'

    # ------------------------------------------------------------------

    def _plan(self, plan_id: str) -> CadCalibrationPlan:
        plan = self.repository.get_plan(plan_id)
        if plan is None:
            raise ValueError(f'unknown calibration plan {plan_id}')
        return plan

    def _workflow_refs(
        self,
        plan_id: str,
        export_id: str,
        verification_plan_id: str,
    ) -> tuple[
        CadCalibrationPlan,
        CadCalibrationExportSnapshot,
        CadVerificationMeasurementPlan,
    ]:
        plan = self._plan(plan_id)
        snapshot = self.repository.get_export(export_id)
        if snapshot is None:
            raise ValueError(f'unknown calibration export {export_id}')
        verification = self.repository.get_verification_plan(verification_plan_id)
        if verification is None:
            raise ValueError(f'unknown verification plan {verification_plan_id}')
        if verification.calibration_plan_id != plan.plan_id:
            raise ValueError('verification plan does not belong to this plan')
        if verification.exported_settings_id != snapshot.export_id:
            raise ValueError('verification plan was registered for another export')
        return plan, snapshot, verification

    def _append_lifecycle(
        self,
        plan: CadCalibrationPlan,
        state: CalibrationLifecycleState,
        *,
        created_at_utc: str,
        exported_settings: CadCalibrationExportSnapshot | None = None,
        verification: CadVerificationMeasurementPlan | None = None,
        measurement_ids: Sequence[str] = (),
        note: str | None = None,
    ) -> CadCalibrationLifecycleEvent:
        events = self.repository.list_lifecycle_events(plan.plan_id)
        supersedes = events[-1] if events else None
        event = build_calibration_lifecycle_event(
            plan=plan,
            state=state,
            exported_settings=exported_settings,
            verification_plan=verification,
            measurement_ids=measurement_ids,
            supersedes_event=supersedes,
            created_at_utc=created_at_utc,
            note=note,
        )
        self.repository.save_lifecycle_event(event)
        return event
