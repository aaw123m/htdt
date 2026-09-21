from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_measurement_models import CadFrequencyResponseDataset, CadMeasurementRecord
from .cad_measurement_quality import (
    CadMeasurementQualityReport,
    MeasurementCapabilityClaim,
    dataset_sha256,
    gate_measurement_claim,
    measurement_sha256,
)
from .cad_measurement_quality_repository import CadMeasurementQualityRepository
from .cad_measurement_repository import CadMeasurementRepository
from .cad_repository import SceneRepository
from .cad_scene import Position3
from .cad_schema import ensure_native_schema
from .cad_system_variant_lifecycle import (
    CadSystemVariantLifecycleRepository,
    SystemVariantAsBuiltRecord,
)
from .cad_system_variant_measured_lifecycle import (
    CadSystemVariantMeasuredLifecycleRepository,
    SystemVariantMeasuredRecord,
    build_system_variant_measured_record,
)
from .cad_system_variant_repository import CadSystemVariantRepository


O100G_MEASUREMENT_PLAN_SCHEMA_VERSION = 1
O100G_MEASUREMENT_PLAN_AUTHORITY_VERSION = 'o100g-system-variant-measurement-plan-1'
O100G_MEASUREMENT_CAMPAIGN_AUTHORITY_VERSION = 'o100g-system-variant-measurement-campaign-1'
O100G_MEASUREMENT_PLAN_COMPLETION_AUTHORITY_VERSION = 'o100g-system-variant-measurement-plan-completion-1'
O100G_MEASUREMENT_CAMPAIGN_COMPLETION_AUTHORITY_VERSION = 'o100g-system-variant-measurement-campaign-completion-1'
O100G_MEASUREMENT_CAMPAIGN_REGISTRATION_AUTHORITY_VERSION = 'o100g-system-variant-measurement-campaign-registration-1'


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return sha256(_canonical(value).encode('utf-8')).hexdigest()


def _parse_timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')
    return parsed


def _utc_now() -> str:
    """Repository commit clock; the only source of durable registration time."""
    return datetime.now(timezone.utc).isoformat()


class VariantMeasurementAcquisitionRequirement(BaseModel):
    """Plan-level requirement over the existing AcquisitionContext authority."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    require_context: bool = True
    acquisition_context_id: str | None = Field(default=None, min_length=1)
    acquisition_context_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )

    @model_validator(mode='after')
    def paired_exact_context(self) -> 'VariantMeasurementAcquisitionRequirement':
        if (self.acquisition_context_id is None) != (
            self.acquisition_context_sha256 is None
        ):
            raise ValueError(
                'acquisition context id/hash must be supplied together'
            )
        if self.acquisition_context_id is not None and not self.require_context:
            raise ValueError(
                'exact acquisition context requires require_context=true'
            )
        return self


class SystemVariantMeasurementTarget(BaseModel):
    """One preregistered measurement requirement for an exact as-built state."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    target_id: str = Field(min_length=1)
    measurement_point_entity_id: str = Field(min_length=1)
    measurement_position: Position3
    source_entity_ids: tuple[str, ...] = Field(min_length=1)
    channel_role: str = Field(min_length=1)
    observable: MeasurementCapabilityClaim
    required_band_hz: tuple[float, float] | None = None
    acquisition: VariantMeasurementAcquisitionRequirement = Field(
        default_factory=VariantMeasurementAcquisitionRequirement
    )
    expected_measurement_count: int = Field(default=1, ge=1)
    repeatability_required: bool = False
    validation_purpose: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def validate_target(self) -> 'SystemVariantMeasurementTarget':
        if self.source_entity_ids != tuple(sorted(set(self.source_entity_ids))):
            raise ValueError(
                'measurement target source entity ids must be unique and sorted'
            )
        if self.required_band_hz is not None:
            low, high = self.required_band_hz
            if low <= 0 or high <= low:
                raise ValueError('measurement target required band is invalid')
        if self.repeatability_required and self.expected_measurement_count < 2:
            raise ValueError(
                'repeatability target requires at least two measurements'
            )
        return self


class SystemVariantMeasurementPlan(BaseModel):
    """Immutable SystemVariant-specific extension over existing measurement authority.

    It does not turn the variant into measured state. It preregisters exact
    targets against one persisted application and one explicit as-built record.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = O100G_MEASUREMENT_PLAN_SCHEMA_VERSION
    authority_version: Literal[
        'o100g-system-variant-measurement-plan-1'
    ] = O100G_MEASUREMENT_PLAN_AUTHORITY_VERSION

    plan_id: str = Field(pattern=r'^system-variant-measurement-plan:[0-9a-f]{64}$')
    plan_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    variant_id: str = Field(min_length=1)
    variant_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    application_id: str = Field(min_length=1)
    application_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    document_id: str = Field(min_length=1)

    applied_revision_id: str = Field(min_length=1)
    applied_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    as_built_record_id: str = Field(
        pattern=r'^system-variant-as-built:[0-9a-f]{64}$'
    )
    as_built_record_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    as_built_revision_id: str = Field(min_length=1)
    as_built_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')

    targets: tuple[SystemVariantMeasurementTarget, ...] = Field(min_length=1)
    expected_measurement_count: int = Field(ge=1)
    created_at_utc: str = Field(min_length=1)
    purpose: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def validate_identity(self) -> 'SystemVariantMeasurementPlan':
        target_ids = tuple(item.target_id for item in self.targets)
        if target_ids != tuple(sorted(set(target_ids))):
            raise ValueError('measurement plan targets must be unique and sorted')
        expected_count = sum(
            item.expected_measurement_count for item in self.targets
        )
        if self.expected_measurement_count != expected_count:
            raise ValueError('measurement plan expected count mismatch')
        _parse_timestamp(self.created_at_utc, 'measurement plan created_at_utc')
        expected = _digest(self.semantic_payload())
        if self.plan_sha256 != expected:
            raise ValueError('SystemVariantMeasurementPlan semantic hash mismatch')
        if self.plan_id != f'system-variant-measurement-plan:{expected}':
            raise ValueError('SystemVariantMeasurementPlan id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'plan_id', 'plan_sha256'},
        )


class SystemVariantMeasurementPlanRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    plan_id: str = Field(pattern=r'^system-variant-measurement-plan:[0-9a-f]{64}$')
    plan_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class SystemVariantMeasurementCampaign(BaseModel):
    """Preregistered immutable campaign over exact SystemVariant measurement plans.

    `preregistered_at_utc` is the caller-declared intended preregistration
    instant. It is hashed into the campaign identity as planning metadata only;
    it is never proof of durable preregistration. The durable authority is the
    repository-committed
    `SystemVariantMeasurementCampaignRegistration.registered_at_utc`.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = O100G_MEASUREMENT_PLAN_SCHEMA_VERSION
    authority_version: Literal[
        'o100g-system-variant-measurement-campaign-1'
    ] = O100G_MEASUREMENT_CAMPAIGN_AUTHORITY_VERSION

    campaign_id: str = Field(
        pattern=r'^system-variant-measurement-campaign:[0-9a-f]{64}$'
    )
    campaign_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    variant_id: str = Field(min_length=1)
    variant_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    application_id: str = Field(min_length=1)
    application_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    document_id: str = Field(min_length=1)
    applied_revision_id: str = Field(min_length=1)
    applied_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    as_built_record_id: str = Field(
        pattern=r'^system-variant-as-built:[0-9a-f]{64}$'
    )
    as_built_record_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    as_built_revision_id: str = Field(min_length=1)
    as_built_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')

    plan_refs: tuple[SystemVariantMeasurementPlanRef, ...] = Field(min_length=1)
    purpose: str = Field(min_length=1)
    preregistered_at_utc: str = Field(min_length=1)
    required_measurement_count: int = Field(ge=1)

    @model_validator(mode='after')
    def validate_identity(self) -> 'SystemVariantMeasurementCampaign':
        plan_ids = tuple(item.plan_id for item in self.plan_refs)
        if plan_ids != tuple(sorted(set(plan_ids))):
            raise ValueError('measurement campaign plan refs must be unique and sorted')
        _parse_timestamp(
            self.preregistered_at_utc,
            'measurement campaign preregistered_at_utc',
        )
        expected = _digest(self.semantic_payload())
        if self.campaign_sha256 != expected:
            raise ValueError('SystemVariantMeasurementCampaign semantic hash mismatch')
        if self.campaign_id != f'system-variant-measurement-campaign:{expected}':
            raise ValueError('SystemVariantMeasurementCampaign id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'campaign_id', 'campaign_sha256'},
        )


class SystemVariantMeasurementCampaignRegistration(BaseModel):
    """Durable repository-attested preregistration for one exact campaign.

    `registered_at_utc` is generated by the repository inside the campaign
    commit transaction after the no-qualifying-evidence check passes. It is the
    only timestamp completion gates may treat as durable preregistration.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'o100g-system-variant-measurement-campaign-registration-1'
    ] = O100G_MEASUREMENT_CAMPAIGN_REGISTRATION_AUTHORITY_VERSION

    registration_id: str = Field(
        pattern=r'^system-variant-measurement-campaign-registration:[0-9a-f]{64}$'
    )
    registration_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    campaign_id: str = Field(
        pattern=r'^system-variant-measurement-campaign:[0-9a-f]{64}$'
    )
    campaign_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    registered_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def validate_identity(self) -> 'SystemVariantMeasurementCampaignRegistration':
        _parse_timestamp(
            self.registered_at_utc,
            'campaign registration registered_at_utc',
        )
        expected = _digest(self.semantic_payload())
        if self.registration_sha256 != expected:
            raise ValueError(
                'SystemVariantMeasurementCampaignRegistration semantic hash mismatch'
            )
        if self.registration_id != (
            f'system-variant-measurement-campaign-registration:{expected}'
        ):
            raise ValueError(
                'SystemVariantMeasurementCampaignRegistration id mismatch'
            )
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'registration_id', 'registration_sha256'},
        )


class VariantMeasurementEvidenceRef(BaseModel):
    """Exact accepted N60/quality evidence assigned to one preregistered target."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    target_id: str = Field(min_length=1)
    measurement_id: str = Field(min_length=1)
    measurement_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    dataset_id: str = Field(min_length=1)
    dataset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    quality_report_id: str = Field(min_length=1)
    quality_report_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    acquisition_context_id: str = Field(min_length=1)
    acquisition_context_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    captured_at: str = Field(min_length=1)


class SystemVariantMeasurementPlanCompletion(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'o100g-system-variant-measurement-plan-completion-1'
    ] = O100G_MEASUREMENT_PLAN_COMPLETION_AUTHORITY_VERSION
    completion_id: str = Field(
        pattern=r'^system-variant-measurement-plan-completion:[0-9a-f]{64}$'
    )
    completion_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    plan_ref: SystemVariantMeasurementPlanRef
    campaign_id: str = Field(
        pattern=r'^system-variant-measurement-campaign:[0-9a-f]{64}$'
    )
    campaign_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    campaign_registration_id: str = Field(
        pattern=r'^system-variant-measurement-campaign-registration:[0-9a-f]{64}$'
    )
    campaign_registration_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    evidence: tuple[VariantMeasurementEvidenceRef, ...] = Field(min_length=1)
    completed_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def validate_identity(self) -> 'SystemVariantMeasurementPlanCompletion':
        measurement_ids = tuple(item.measurement_id for item in self.evidence)
        if measurement_ids != tuple(sorted(set(measurement_ids))):
            raise ValueError('plan completion evidence must be unique and sorted')
        _parse_timestamp(self.completed_at_utc, 'plan completion timestamp')
        expected = _digest(self.semantic_payload())
        if self.completion_sha256 != expected:
            raise ValueError('measurement plan completion hash mismatch')
        if self.completion_id != (
            f'system-variant-measurement-plan-completion:{expected}'
        ):
            raise ValueError('measurement plan completion id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'completion_id', 'completion_sha256'},
        )


class SystemVariantMeasurementCampaignCompletion(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_version: Literal[
        'o100g-system-variant-measurement-campaign-completion-1'
    ] = O100G_MEASUREMENT_CAMPAIGN_COMPLETION_AUTHORITY_VERSION
    completion_id: str = Field(
        pattern=r'^system-variant-measurement-campaign-completion:[0-9a-f]{64}$'
    )
    completion_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    campaign_id: str = Field(
        pattern=r'^system-variant-measurement-campaign:[0-9a-f]{64}$'
    )
    campaign_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    campaign_registration_id: str = Field(
        pattern=r'^system-variant-measurement-campaign-registration:[0-9a-f]{64}$'
    )
    campaign_registration_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    plan_completion_ids: tuple[str, ...] = Field(min_length=1)
    plan_completion_sha256: tuple[str, ...] = Field(min_length=1)
    measured_record_id: str = Field(
        pattern=r'^system-variant-measured:[0-9a-f]{64}$'
    )
    measured_record_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    completed_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def validate_identity(self) -> 'SystemVariantMeasurementCampaignCompletion':
        if len(self.plan_completion_ids) != len(self.plan_completion_sha256):
            raise ValueError('campaign completion plan completion refs mismatch')
        if self.plan_completion_ids != tuple(sorted(set(self.plan_completion_ids))):
            raise ValueError(
                'campaign completion plan completion ids must be unique and sorted'
            )
        _parse_timestamp(self.completed_at_utc, 'campaign completion timestamp')
        expected = _digest(self.semantic_payload())
        if self.completion_sha256 != expected:
            raise ValueError('measurement campaign completion hash mismatch')
        if self.completion_id != (
            f'system-variant-measurement-campaign-completion:{expected}'
        ):
            raise ValueError('measurement campaign completion id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'completion_id', 'completion_sha256'},
        )


def _exact_as_built(
    lifecycle_repository: CadSystemVariantLifecycleRepository,
    record: SystemVariantAsBuiltRecord,
) -> SystemVariantAsBuiltRecord:
    persisted = lifecycle_repository.get(record.record_id)
    if persisted is None:
        raise ValueError('SystemVariant measurement authority requires persisted AsBuilt')
    if persisted != record:
        raise ValueError('SystemVariant measurement AsBuilt exact authority mismatch')
    return persisted


def _validate_target_against_as_built(
    scene_repository: SceneRepository,
    as_built: SystemVariantAsBuiltRecord,
    target: SystemVariantMeasurementTarget,
) -> None:
    revision = scene_repository.get(as_built.as_built_revision_id)
    if revision is None or revision.content_hash != as_built.as_built_content_hash:
        raise ValueError('SystemVariant measurement AsBuilt SceneRevision mismatch')
    try:
        point = revision.document.entity(target.measurement_point_entity_id)
    except KeyError as exc:
        raise ValueError('measurement target point entity is absent from AsBuilt') from exc
    if point.position != target.measurement_position:
        raise ValueError('measurement target point does not match exact AsBuilt position')
    for entity_id in target.source_entity_ids:
        try:
            source = revision.document.entity(entity_id)
        except KeyError as exc:
            raise ValueError('measurement target source entity is absent from AsBuilt') from exc
        if source.kind != 'speaker':
            raise ValueError('measurement target source entity must be a speaker')


def build_system_variant_measurement_plan(
    *,
    scene_repository: SceneRepository,
    variant_repository: CadSystemVariantRepository,
    lifecycle_repository: CadSystemVariantLifecycleRepository,
    as_built_record: SystemVariantAsBuiltRecord,
    targets: Sequence[SystemVariantMeasurementTarget],
    created_at_utc: str,
    purpose: str | None = None,
) -> SystemVariantMeasurementPlan:
    as_built = _exact_as_built(lifecycle_repository, as_built_record)
    variant = variant_repository.get_variant(as_built.variant_id)
    application = variant_repository.get_application(as_built.application_id)
    if variant is None or application is None:
        raise ValueError('SystemVariant measurement plan source authority disappeared')
    if (
        variant.variant_sha256 != as_built.variant_sha256
        or application.application_sha256 != as_built.application_sha256
        or application.variant_id != variant.variant_id
    ):
        raise ValueError('SystemVariant measurement plan source authority mismatch')
    applied = scene_repository.get(as_built.applied_revision_id)
    actual = scene_repository.get(as_built.as_built_revision_id)
    if (
        applied is None
        or applied.content_hash != as_built.applied_content_hash
        or actual is None
        or actual.content_hash != as_built.as_built_content_hash
    ):
        raise ValueError('SystemVariant measurement plan revision authority mismatch')

    ordered = tuple(sorted(
        (
            SystemVariantMeasurementTarget.model_validate(
                item.model_dump(mode='python')
            )
            for item in targets
        ),
        key=lambda item: item.target_id,
    ))
    if not ordered:
        raise ValueError('SystemVariant measurement plan requires at least one target')
    for target in ordered:
        _validate_target_against_as_built(scene_repository, as_built, target)

    payload = {
        'schema_version': O100G_MEASUREMENT_PLAN_SCHEMA_VERSION,
        'authority_version': O100G_MEASUREMENT_PLAN_AUTHORITY_VERSION,
        'variant_id': variant.variant_id,
        'variant_sha256': variant.variant_sha256,
        'application_id': application.application_id,
        'application_sha256': application.application_sha256,
        'document_id': variant.document_id,
        'applied_revision_id': as_built.applied_revision_id,
        'applied_content_hash': as_built.applied_content_hash,
        'as_built_record_id': as_built.record_id,
        'as_built_record_sha256': as_built.record_sha256,
        'as_built_revision_id': as_built.as_built_revision_id,
        'as_built_content_hash': as_built.as_built_content_hash,
        'targets': [item.model_dump(mode='json') for item in ordered],
        'expected_measurement_count': sum(
            item.expected_measurement_count for item in ordered
        ),
        'created_at_utc': created_at_utc,
        'purpose': purpose,
    }
    digest = _digest(payload)
    return SystemVariantMeasurementPlan(
        plan_id=f'system-variant-measurement-plan:{digest}',
        plan_sha256=digest,
        **payload,
    )


def build_system_variant_measurement_campaign(
    *,
    plans: Sequence[SystemVariantMeasurementPlan],
    purpose: str,
    preregistered_at_utc: str,
) -> SystemVariantMeasurementCampaign:
    ordered = tuple(sorted(
        (
            SystemVariantMeasurementPlan.model_validate(
                item.model_dump(mode='python')
            )
            for item in plans
        ),
        key=lambda item: item.plan_id,
    ))
    if not ordered:
        raise ValueError('measurement campaign requires at least one plan')
    first = ordered[0]
    common_fields = (
        'variant_id',
        'variant_sha256',
        'application_id',
        'application_sha256',
        'document_id',
        'applied_revision_id',
        'applied_content_hash',
        'as_built_record_id',
        'as_built_record_sha256',
        'as_built_revision_id',
        'as_built_content_hash',
    )
    for plan in ordered[1:]:
        if any(getattr(plan, field) != getattr(first, field) for field in common_fields):
            raise ValueError(
                'measurement campaign plans must share exact SystemVariant/AsBuilt authority'
            )
    preregistered = _parse_timestamp(
        preregistered_at_utc,
        'measurement campaign preregistered_at_utc',
    )
    for plan in ordered:
        if _parse_timestamp(
            plan.created_at_utc,
            'measurement plan created_at_utc',
        ) > preregistered:
            raise ValueError('measurement campaign cannot preregister before a plan exists')

    payload = {
        'schema_version': O100G_MEASUREMENT_PLAN_SCHEMA_VERSION,
        'authority_version': O100G_MEASUREMENT_CAMPAIGN_AUTHORITY_VERSION,
        **{field: getattr(first, field) for field in common_fields},
        'plan_refs': [
            SystemVariantMeasurementPlanRef(
                plan_id=plan.plan_id,
                plan_sha256=plan.plan_sha256,
            ).model_dump(mode='json')
            for plan in ordered
        ],
        'purpose': purpose,
        'preregistered_at_utc': preregistered_at_utc,
        'required_measurement_count': sum(
            plan.expected_measurement_count for plan in ordered
        ),
    }
    digest = _digest(payload)
    return SystemVariantMeasurementCampaign(
        campaign_id=f'system-variant-measurement-campaign:{digest}',
        campaign_sha256=digest,
        **payload,
    )


def build_system_variant_measurement_campaign_registration(
    *,
    campaign: SystemVariantMeasurementCampaign,
    registered_at_utc: str,
) -> SystemVariantMeasurementCampaignRegistration:
    payload = {
        'authority_version': O100G_MEASUREMENT_CAMPAIGN_REGISTRATION_AUTHORITY_VERSION,
        'campaign_id': campaign.campaign_id,
        'campaign_sha256': campaign.campaign_sha256,
        'registered_at_utc': registered_at_utc,
    }
    digest = _digest(payload)
    return SystemVariantMeasurementCampaignRegistration(
        registration_id=(
            f'system-variant-measurement-campaign-registration:{digest}'
        ),
        registration_sha256=digest,
        **payload,
    )


def _exact_campaign_registration(
    campaign: SystemVariantMeasurementCampaign,
    registration: SystemVariantMeasurementCampaignRegistration,
) -> SystemVariantMeasurementCampaignRegistration:
    if (
        registration.campaign_id != campaign.campaign_id
        or registration.campaign_sha256 != campaign.campaign_sha256
    ):
        raise ValueError('measurement campaign registration authority mismatch')
    registered = _parse_timestamp(
        registration.registered_at_utc,
        'campaign registration registered_at_utc',
    )
    claimed = _parse_timestamp(
        campaign.preregistered_at_utc,
        'campaign preregistered_at_utc',
    )
    if registered < claimed:
        raise ValueError(
            'campaign registration cannot predate claimed preregistration'
        )
    return registration


def _resolve_evidence(
    *,
    measurement_repository: CadMeasurementRepository,
    quality_repository: CadMeasurementQualityRepository,
    measurement_id: str,
) -> tuple[CadMeasurementRecord, CadFrequencyResponseDataset, CadMeasurementQualityReport]:
    measurement = measurement_repository.get_measurement(measurement_id)
    if measurement is None:
        raise ValueError(f'measurement evidence does not exist: {measurement_id}')
    dataset = measurement_repository.dataset_for_measurement(measurement_id)
    if dataset is None:
        raise ValueError(f'measurement dataset does not exist: {measurement_id}')
    report = quality_repository.latest_report(measurement_id)
    if report is None:
        raise ValueError(f'measurement quality report does not exist: {measurement_id}')
    if report.measurement_sha256 != measurement_sha256(measurement):
        raise ValueError('measurement quality report measurement hash mismatch')
    if report.dataset_sha256 != dataset_sha256(dataset):
        raise ValueError('measurement quality report dataset hash mismatch')
    return measurement, dataset, report


def _match_target(
    *,
    campaign: SystemVariantMeasurementCampaign,
    registration: SystemVariantMeasurementCampaignRegistration,
    target: SystemVariantMeasurementTarget,
    measurement: CadMeasurementRecord,
    dataset: CadFrequencyResponseDataset,
    report: CadMeasurementQualityReport,
) -> VariantMeasurementEvidenceRef:
    if (
        measurement.document_id != campaign.document_id
        or measurement.scene_revision_id != campaign.as_built_revision_id
        or measurement.scene_content_hash != campaign.as_built_content_hash
    ):
        raise ValueError('measurement evidence binds wrong AsBuilt SceneRevision')
    if measurement.evidence_type != 'measured':
        raise ValueError('variant campaign requires measured evidence')
    if measurement.measurement_entity_id != target.measurement_point_entity_id:
        raise ValueError('measurement evidence binds wrong measurement point entity')
    if measurement.measurement_position != target.measurement_position:
        raise ValueError('measurement evidence binds wrong measurement point position')
    if measurement.channel_role != target.channel_role:
        raise ValueError('measurement evidence binds wrong channel role')
    if tuple(sorted(measurement.source_speaker_ids)) != target.source_entity_ids:
        raise ValueError('measurement evidence binds wrong source entity role')
    if measurement.captured_at is None:
        raise ValueError('variant campaign requires explicit capture timestamp')
    captured = _parse_timestamp(measurement.captured_at, 'measurement captured_at')
    registered = _parse_timestamp(
        registration.registered_at_utc,
        'campaign registration registered_at_utc',
    )
    if captured < registered:
        raise ValueError('pre-registration measurement cannot satisfy campaign evidence')

    if (
        report.document_id != measurement.document_id
        or report.scene_revision_id != measurement.scene_revision_id
        or report.scene_content_hash != measurement.scene_content_hash
        or report.measurement_entity_id != measurement.measurement_entity_id
        or report.measurement_position != measurement.measurement_position
    ):
        raise ValueError('measurement quality report exact binding mismatch')
    if report.acquisition_context is None:
        raise ValueError('variant campaign requires acquisition context')
    requirement = target.acquisition
    if requirement.require_context and report.acquisition_context is None:
        raise ValueError('measurement acquisition context is required')
    if requirement.acquisition_context_id is not None and (
        report.acquisition_context.acquisition_context_id
        != requirement.acquisition_context_id
        or report.acquisition_context.acquisition_context_sha256
        != requirement.acquisition_context_sha256
    ):
        raise ValueError('measurement acquisition context requirement mismatch')

    decision = gate_measurement_claim(
        report,
        target.observable,
        required_band_hz=target.required_band_hz,
    )
    if decision.decision != 'ALLOWED':
        raise ValueError(
            f'measurement quality capability is insufficient: '
            f'{target.observable}:{decision.decision}'
        )
    if target.repeatability_required and not report.allows('repeatability'):
        raise ValueError('measurement repeatability capability is insufficient')

    return VariantMeasurementEvidenceRef(
        target_id=target.target_id,
        measurement_id=measurement.measurement_id,
        measurement_sha256=measurement_sha256(measurement),
        dataset_id=dataset.dataset_id,
        dataset_sha256=dataset_sha256(dataset),
        quality_report_id=report.report_id,
        quality_report_sha256=report.report_sha256,
        acquisition_context_id=report.acquisition_context.acquisition_context_id,
        acquisition_context_sha256=(
            report.acquisition_context.acquisition_context_sha256
        ),
        captured_at=measurement.captured_at,
    )


def complete_system_variant_measurement_plan(
    *,
    campaign: SystemVariantMeasurementCampaign,
    registration: SystemVariantMeasurementCampaignRegistration,
    plan: SystemVariantMeasurementPlan,
    assignments: dict[str, Sequence[str]],
    measurement_repository: CadMeasurementRepository,
    quality_repository: CadMeasurementQualityRepository,
    completed_at_utc: str,
) -> tuple[
    SystemVariantMeasurementPlanCompletion,
    tuple[
        tuple[
            CadMeasurementRecord,
            CadFrequencyResponseDataset,
            CadMeasurementQualityReport,
        ],
        ...,
    ],
]:
    registration = _exact_campaign_registration(campaign, registration)
    exact_ref = SystemVariantMeasurementPlanRef(
        plan_id=plan.plan_id,
        plan_sha256=plan.plan_sha256,
    )
    if exact_ref not in campaign.plan_refs:
        raise ValueError('measurement plan is not preregistered in campaign')
    all_evidence: list[VariantMeasurementEvidenceRef] = []
    resolved: list[
        tuple[
            CadMeasurementRecord,
            CadFrequencyResponseDataset,
            CadMeasurementQualityReport,
        ]
    ] = []
    used: set[str] = set()
    targets = {item.target_id: item for item in plan.targets}
    if set(assignments) != set(targets):
        raise ValueError('measurement plan assignments must cover exact target set')

    for target_id in sorted(targets):
        target = targets[target_id]
        measurement_ids = tuple(assignments[target_id])
        if len(measurement_ids) != target.expected_measurement_count:
            raise ValueError('measurement target evidence count mismatch')
        for measurement_id in sorted(measurement_ids):
            if measurement_id in used:
                raise ValueError('measurement cannot satisfy multiple target slots')
            used.add(measurement_id)
            measurement, dataset, report = _resolve_evidence(
                measurement_repository=measurement_repository,
                quality_repository=quality_repository,
                measurement_id=measurement_id,
            )
            all_evidence.append(
                _match_target(
                    campaign=campaign,
                    registration=registration,
                    target=target,
                    measurement=measurement,
                    dataset=dataset,
                    report=report,
                )
            )
            resolved.append((measurement, dataset, report))

    evidence = tuple(sorted(all_evidence, key=lambda item: item.measurement_id))
    payload = {
        'authority_version': O100G_MEASUREMENT_PLAN_COMPLETION_AUTHORITY_VERSION,
        'plan_ref': exact_ref.model_dump(mode='json'),
        'campaign_id': campaign.campaign_id,
        'campaign_sha256': campaign.campaign_sha256,
        'campaign_registration_id': registration.registration_id,
        'campaign_registration_sha256': registration.registration_sha256,
        'evidence': [item.model_dump(mode='json') for item in evidence],
        'completed_at_utc': completed_at_utc,
    }
    digest = _digest(payload)
    completion = SystemVariantMeasurementPlanCompletion(
        completion_id=(
            f'system-variant-measurement-plan-completion:{digest}'
        ),
        completion_sha256=digest,
        **payload,
    )
    return completion, tuple(resolved)


def complete_system_variant_measurement_campaign(
    *,
    scene_repository: SceneRepository,
    lifecycle_repository: CadSystemVariantLifecycleRepository,
    campaign: SystemVariantMeasurementCampaign,
    registration: SystemVariantMeasurementCampaignRegistration,
    plans: Sequence[SystemVariantMeasurementPlan],
    assignments_by_plan: dict[str, dict[str, Sequence[str]]],
    measurement_repository: CadMeasurementRepository,
    quality_repository: CadMeasurementQualityRepository,
    completed_at_utc: str,
    notes: Sequence[str] = (),
) -> tuple[
    SystemVariantMeasurementCampaignCompletion,
    tuple[SystemVariantMeasurementPlanCompletion, ...],
    SystemVariantMeasuredRecord,
]:
    """Validate evidence and build the campaign completion artifacts.

    Pure composition: it resolves every authority and returns the campaign
    completion, the exact plan completions and the measured lifecycle record
    without persisting anything. Durable completion goes through
    `CadSystemVariantMeasurementCampaignRepository.complete_campaign`, which
    commits all three under one BEGIN IMMEDIATE transaction.
    """
    as_built = lifecycle_repository.get(campaign.as_built_record_id)
    if as_built is None or (
        as_built.record_sha256 != campaign.as_built_record_sha256
        or as_built.variant_id != campaign.variant_id
        or as_built.variant_sha256 != campaign.variant_sha256
        or as_built.as_built_revision_id != campaign.as_built_revision_id
        or as_built.as_built_content_hash != campaign.as_built_content_hash
    ):
        raise ValueError('measurement campaign AsBuilt authority is missing/stale')
    registration = _exact_campaign_registration(campaign, registration)

    by_id = {plan.plan_id: plan for plan in plans}
    if set(by_id) != {ref.plan_id for ref in campaign.plan_refs}:
        raise ValueError('campaign completion requires exact preregistered plan set')
    if set(assignments_by_plan) != set(by_id):
        raise ValueError('campaign assignments must cover exact preregistered plans')

    completions: list[SystemVariantMeasurementPlanCompletion] = []
    evidence_by_measurement: dict[
        str,
        tuple[
            CadMeasurementRecord,
            CadFrequencyResponseDataset,
            CadMeasurementQualityReport,
        ],
    ] = {}
    for ref in campaign.plan_refs:
        plan = by_id[ref.plan_id]
        if plan.plan_sha256 != ref.plan_sha256:
            raise ValueError('campaign plan exact authority mismatch')
        completion, resolved = complete_system_variant_measurement_plan(
            campaign=campaign,
            registration=registration,
            plan=plan,
            assignments=assignments_by_plan[plan.plan_id],
            measurement_repository=measurement_repository,
            quality_repository=quality_repository,
            completed_at_utc=completed_at_utc,
        )
        completions.append(completion)
        for item in resolved:
            measurement_id = item[0].measurement_id
            if measurement_id in evidence_by_measurement:
                raise ValueError('campaign measurement evidence is duplicated')
            evidence_by_measurement[measurement_id] = item

    if len(evidence_by_measurement) != campaign.required_measurement_count:
        raise ValueError('campaign required measurement count mismatch')

    measured = build_system_variant_measured_record(
        scene_repository=scene_repository,
        as_built_record=as_built,
        evidence=tuple(
            evidence_by_measurement[key]
            for key in sorted(evidence_by_measurement)
        ),
        bound_at_utc=completed_at_utc,
        notes=notes,
    )

    ordered_completions = tuple(sorted(
        completions,
        key=lambda item: item.completion_id,
    ))
    completion_payload = {
        'authority_version': O100G_MEASUREMENT_CAMPAIGN_COMPLETION_AUTHORITY_VERSION,
        'campaign_id': campaign.campaign_id,
        'campaign_sha256': campaign.campaign_sha256,
        'campaign_registration_id': registration.registration_id,
        'campaign_registration_sha256': registration.registration_sha256,
        'plan_completion_ids': [
            item.completion_id for item in ordered_completions
        ],
        'plan_completion_sha256': [
            item.completion_sha256 for item in ordered_completions
        ],
        'measured_record_id': measured.record_id,
        'measured_record_sha256': measured.record_sha256,
        'completed_at_utc': completed_at_utc,
    }
    digest = _digest(completion_payload)
    completion = SystemVariantMeasurementCampaignCompletion(
        completion_id=(
            f'system-variant-measurement-campaign-completion:{digest}'
        ),
        completion_sha256=digest,
        **completion_payload,
    )
    return completion, ordered_completions, measured


class CadSystemVariantMeasurementCampaignRepository:
    """Append-only O100G plan/campaign persistence and exact reopen validation.

    `save_campaign` is the durable preregistration authority: it commits the
    campaign row together with a repository-attested registration record under
    one BEGIN IMMEDIATE write transaction, after proving that no qualifying
    measured evidence for the preregistered targets already exists. Completion
    gates therefore use `registered_at_utc` from the persisted registration,
    never the caller-supplied `preregistered_at_utc` planning metadata.

    `complete_campaign` is the durable completion authority: it re-resolves
    the persisted campaign/registration/plan authorities and validates every
    artifact before the shared write transaction begins, then commits every
    plan completion, the campaign completion and the measured lifecycle
    record under one BEGIN IMMEDIATE transaction. A failure at any point
    leaves no partial completion state, so a SystemVariant can never be
    durable as measured without the preregistered campaign's persisted
    completion evidence (or the reverse).
    """

    def __init__(
        self,
        *,
        scene_repository: SceneRepository,
        variant_repository: CadSystemVariantRepository,
        lifecycle_repository: CadSystemVariantLifecycleRepository,
        measurement_repository: CadMeasurementRepository,
        quality_repository: CadMeasurementQualityRepository,
        measured_lifecycle_repository: CadSystemVariantMeasuredLifecycleRepository,
    ) -> None:
        self.scene_repository = scene_repository
        self.variant_repository = variant_repository
        self.lifecycle_repository = lifecycle_repository
        self.measurement_repository = measurement_repository
        self.quality_repository = quality_repository
        self.measured_lifecycle_repository = measured_lifecycle_repository
        self.path = Path(scene_repository.path)
        for name, repository in (
            ('variant', variant_repository),
            ('lifecycle', lifecycle_repository),
            ('measurement', measurement_repository),
            ('quality', quality_repository),
            ('measured lifecycle', measured_lifecycle_repository),
        ):
            if Path(repository.path) != self.path:
                raise ValueError(
                    f'SystemVariant measurement campaign and {name} repository '
                    'must share one native CAD database'
                )
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        ensure_native_schema(self.path)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cad_system_variant_measurement_plans (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id TEXT NOT NULL UNIQUE,
                    plan_sha256 TEXT NOT NULL UNIQUE,
                    variant_id TEXT NOT NULL,
                    as_built_record_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cad_system_variant_measurement_campaigns (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_id TEXT NOT NULL UNIQUE,
                    campaign_sha256 TEXT NOT NULL UNIQUE,
                    variant_id TEXT NOT NULL,
                    as_built_record_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cad_system_variant_measurement_campaign_registrations (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    registration_id TEXT NOT NULL UNIQUE,
                    registration_sha256 TEXT NOT NULL UNIQUE,
                    campaign_id TEXT NOT NULL UNIQUE,
                    campaign_sha256 TEXT NOT NULL,
                    registered_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cad_system_variant_measurement_plan_completions (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    completion_id TEXT NOT NULL UNIQUE,
                    completion_sha256 TEXT NOT NULL UNIQUE,
                    plan_id TEXT NOT NULL,
                    campaign_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cad_system_variant_measurement_campaign_completions (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    completion_id TEXT NOT NULL UNIQUE,
                    completion_sha256 TEXT NOT NULL UNIQUE,
                    campaign_id TEXT NOT NULL UNIQUE,
                    measured_record_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL
                );
                """
            )

    def _validate_plan(
        self,
        plan: SystemVariantMeasurementPlan,
    ) -> SystemVariantMeasurementPlan:
        plan = SystemVariantMeasurementPlan.model_validate(
            plan.model_dump(mode='python')
        )
        as_built = self.lifecycle_repository.get(plan.as_built_record_id)
        if as_built is None:
            raise ValueError('measurement plan references missing AsBuilt')
        rebuilt = build_system_variant_measurement_plan(
            scene_repository=self.scene_repository,
            variant_repository=self.variant_repository,
            lifecycle_repository=self.lifecycle_repository,
            as_built_record=as_built,
            targets=plan.targets,
            created_at_utc=plan.created_at_utc,
            purpose=plan.purpose,
        )
        if rebuilt != plan:
            raise ValueError('measurement plan does not reproduce exactly')
        return plan

    def save_plan(
        self,
        plan: SystemVariantMeasurementPlan,
    ) -> SystemVariantMeasurementPlan:
        plan = self._validate_plan(plan)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_system_variant_measurement_plans '
                'WHERE plan_id=?',
                (plan.plan_id,),
            ).fetchone()
            if row is not None:
                persisted = SystemVariantMeasurementPlan.model_validate_json(
                    row['payload_json']
                )
                if persisted != plan:
                    raise ValueError('measurement plan id has different semantics')
                return self._validate_plan(persisted)
            connection.execute(
                """
                INSERT INTO cad_system_variant_measurement_plans(
                    plan_id, plan_sha256, variant_id, as_built_record_id,
                    payload_json, recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.plan_id,
                    plan.plan_sha256,
                    plan.variant_id,
                    plan.as_built_record_id,
                    plan.model_dump_json(),
                    plan.created_at_utc,
                ),
            )
        return plan

    def get_plan(self, plan_id: str) -> SystemVariantMeasurementPlan | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_system_variant_measurement_plans '
                'WHERE plan_id=?',
                (plan_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate_plan(
            SystemVariantMeasurementPlan.model_validate_json(row['payload_json'])
        )

    def _campaign_plans(
        self,
        campaign: SystemVariantMeasurementCampaign,
    ) -> tuple[SystemVariantMeasurementPlan, ...]:
        plans = []
        for ref in campaign.plan_refs:
            plan = self.get_plan(ref.plan_id)
            if plan is None or plan.plan_sha256 != ref.plan_sha256:
                raise ValueError('measurement campaign plan authority missing/stale')
            plans.append(plan)
        return tuple(plans)

    def _validate_campaign(
        self,
        campaign: SystemVariantMeasurementCampaign,
    ) -> SystemVariantMeasurementCampaign:
        campaign = SystemVariantMeasurementCampaign.model_validate(
            campaign.model_dump(mode='python')
        )
        rebuilt = build_system_variant_measurement_campaign(
            plans=self._campaign_plans(campaign),
            purpose=campaign.purpose,
            preregistered_at_utc=campaign.preregistered_at_utc,
        )
        if rebuilt != campaign:
            raise ValueError('measurement campaign does not reproduce exactly')
        return campaign

    def _preexisting_qualifying_evidence(
        self,
        connection: sqlite3.Connection,
        campaign: SystemVariantMeasurementCampaign,
        plans: Sequence[SystemVariantMeasurementPlan],
    ) -> str | None:
        """Return an existing measurement id that could satisfy a campaign target.

        Runs inside the campaign commit transaction so a concurrent measurement
        import and a campaign registration serialize into one ordering: either
        the campaign commits first, or the already-committed evidence makes the
        registration fail.
        """
        targets = tuple(target for plan in plans for target in plan.targets)
        rows = connection.execute(
            """
            SELECT measurement_id, measurement_entity_id, measurement_position_json,
                   channel_role, source_speaker_ids_json
            FROM cad_measurements
            WHERE document_id=? AND scene_revision_id=? AND scene_content_hash=?
              AND evidence_type='measured'
            """,
            (
                campaign.document_id,
                campaign.as_built_revision_id,
                campaign.as_built_content_hash,
            ),
        ).fetchall()
        for row in rows:
            position = Position3.model_validate(
                json.loads(row['measurement_position_json'])
            )
            source_ids = tuple(sorted(json.loads(row['source_speaker_ids_json'])))
            for target in targets:
                if (
                    row['measurement_entity_id']
                    == target.measurement_point_entity_id
                    and position == target.measurement_position
                    and row['channel_role'] == target.channel_role
                    and source_ids == target.source_entity_ids
                ):
                    return str(row['measurement_id'])
        return None

    def save_campaign(
        self,
        campaign: SystemVariantMeasurementCampaign,
    ) -> SystemVariantMeasurementCampaignRegistration:
        campaign = self._validate_campaign(campaign)
        plans = self._campaign_plans(campaign)
        persisted: SystemVariantMeasurementCampaign | None = None
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute(
                'SELECT payload_json FROM cad_system_variant_measurement_campaigns '
                'WHERE campaign_id=?',
                (campaign.campaign_id,),
            ).fetchone()
            if row is not None:
                persisted = SystemVariantMeasurementCampaign.model_validate_json(
                    row['payload_json']
                )
                if persisted != campaign:
                    raise ValueError('measurement campaign id has different semantics')
                registration = self._registration_for(connection, campaign.campaign_id)
                if registration is None:
                    # No historical registration time is silently inferred.
                    raise ValueError(
                        'measurement campaign registration authority missing/stale'
                    )
                _exact_campaign_registration(persisted, registration)
            else:
                registration = self._commit_registration(
                    connection,
                    campaign,
                    plans,
                )
            connection.commit()
        if persisted is not None:
            self._validate_campaign(persisted)
        return registration

    def _commit_registration(
        self,
        connection: sqlite3.Connection,
        campaign: SystemVariantMeasurementCampaign,
        plans: Sequence[SystemVariantMeasurementPlan],
    ) -> SystemVariantMeasurementCampaignRegistration:
        """Attest durable preregistration inside the campaign write transaction.

        The registration timestamp is generated here at commit and the
        no-qualifying-evidence check runs under the same BEGIN IMMEDIATE
        boundary, so evidence import and campaign registration have exactly one
        deterministic ordering.
        """
        registered_at_utc = _utc_now()
        if _parse_timestamp(
            campaign.preregistered_at_utc,
            'campaign preregistered_at_utc',
        ) > _parse_timestamp(registered_at_utc, 'registered_at_utc'):
            raise ValueError(
                'measurement campaign preregistered_at_utc cannot postdate '
                'durable registration'
            )
        blocker = self._preexisting_qualifying_evidence(
            connection,
            campaign,
            plans,
        )
        if blocker is not None:
            raise ValueError(
                'measurement campaign cannot register over existing '
                f'qualifying measurement evidence: {blocker}'
            )
        registration = build_system_variant_measurement_campaign_registration(
            campaign=campaign,
            registered_at_utc=registered_at_utc,
        )
        connection.execute(
            """
            INSERT INTO cad_system_variant_measurement_campaigns(
                campaign_id, campaign_sha256, variant_id, as_built_record_id,
                payload_json, recorded_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                campaign.campaign_id,
                campaign.campaign_sha256,
                campaign.variant_id,
                campaign.as_built_record_id,
                campaign.model_dump_json(),
                registered_at_utc,
            ),
        )
        connection.execute(
            """
            INSERT INTO cad_system_variant_measurement_campaign_registrations(
                registration_id, registration_sha256, campaign_id,
                campaign_sha256, registered_at_utc, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                registration.registration_id,
                registration.registration_sha256,
                registration.campaign_id,
                registration.campaign_sha256,
                registration.registered_at_utc,
                registration.model_dump_json(),
            ),
        )
        return registration

    @staticmethod
    def _registration_for(
        connection: sqlite3.Connection,
        campaign_id: str,
    ) -> SystemVariantMeasurementCampaignRegistration | None:
        row = connection.execute(
            'SELECT payload_json FROM '
            'cad_system_variant_measurement_campaign_registrations '
            'WHERE campaign_id=?',
            (campaign_id,),
        ).fetchone()
        if row is None:
            return None
        return SystemVariantMeasurementCampaignRegistration.model_validate_json(
            row['payload_json']
        )

    def get_campaign_registration(
        self,
        campaign_id: str,
    ) -> SystemVariantMeasurementCampaignRegistration | None:
        with closing(self._connect()) as connection, connection:
            registration = self._registration_for(connection, campaign_id)
        if registration is None:
            return None
        campaign = self.get_campaign(campaign_id)
        if campaign is None:
            raise ValueError(
                'measurement campaign registration references missing campaign'
            )
        return _exact_campaign_registration(campaign, registration)

    def get_campaign(
        self,
        campaign_id: str,
    ) -> SystemVariantMeasurementCampaign | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_system_variant_measurement_campaigns '
                'WHERE campaign_id=?',
                (campaign_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate_campaign(
            SystemVariantMeasurementCampaign.model_validate_json(
                row['payload_json']
            )
        )

    def save_plan_completion(
        self,
        completion: SystemVariantMeasurementPlanCompletion,
    ) -> SystemVariantMeasurementPlanCompletion:
        completion = self._validate_plan_completion(completion)
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            return self._save_plan_completion_in_transaction(
                connection,
                completion,
            )

    def _validate_plan_completion(
        self,
        completion: SystemVariantMeasurementPlanCompletion,
    ) -> SystemVariantMeasurementPlanCompletion:
        completion = SystemVariantMeasurementPlanCompletion.model_validate(
            completion.model_dump(mode='python')
        )
        campaign = self.get_campaign(completion.campaign_id)
        plan = self.get_plan(completion.plan_ref.plan_id)
        if campaign is None or campaign.campaign_sha256 != completion.campaign_sha256:
            raise ValueError('plan completion campaign authority missing/stale')
        registration = self.get_campaign_registration(completion.campaign_id)
        if registration is None:
            raise ValueError(
                'measurement campaign registration authority missing/stale'
            )
        if (
            completion.campaign_registration_id != registration.registration_id
            or completion.campaign_registration_sha256
            != registration.registration_sha256
        ):
            raise ValueError('plan completion registration authority mismatch')
        if plan is None or plan.plan_sha256 != completion.plan_ref.plan_sha256:
            raise ValueError('plan completion plan authority missing/stale')
        assignments: dict[str, list[str]] = {
            target.target_id: [] for target in plan.targets
        }
        for ref in completion.evidence:
            assignments.setdefault(ref.target_id, []).append(ref.measurement_id)
        rebuilt, _ = complete_system_variant_measurement_plan(
            campaign=campaign,
            registration=registration,
            plan=plan,
            assignments=assignments,
            measurement_repository=self.measurement_repository,
            quality_repository=self.quality_repository,
            completed_at_utc=completion.completed_at_utc,
        )
        if rebuilt != completion:
            raise ValueError('plan completion does not reproduce exactly')
        return completion

    def _save_plan_completion_in_transaction(
        self,
        connection: sqlite3.Connection,
        completion: SystemVariantMeasurementPlanCompletion,
    ) -> SystemVariantMeasurementPlanCompletion:
        """Persist one validated plan completion inside the caller's transaction.

        The caller owns BEGIN/COMMIT/ROLLBACK and must have validated the
        completion first; persisted rows were validated on commit. Used by
        save_plan_completion and by complete_campaign, which commits every
        plan completion together with the campaign completion and the
        measured lifecycle record under one shared BEGIN IMMEDIATE.
        """
        row = connection.execute(
            'SELECT payload_json FROM cad_system_variant_measurement_plan_completions '
            'WHERE completion_id=?',
            (completion.completion_id,),
        ).fetchone()
        if row is not None:
            persisted = SystemVariantMeasurementPlanCompletion.model_validate_json(
                row['payload_json']
            )
            if persisted != completion:
                raise ValueError('plan completion id has different semantics')
            return persisted
        connection.execute(
            """
            INSERT INTO cad_system_variant_measurement_plan_completions(
                completion_id, completion_sha256, plan_id, campaign_id,
                payload_json, recorded_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                completion.completion_id,
                completion.completion_sha256,
                completion.plan_ref.plan_id,
                completion.campaign_id,
                completion.model_dump_json(),
                completion.completed_at_utc,
            ),
        )
        return completion

    def get_plan_completion(
        self,
        completion_id: str,
    ) -> SystemVariantMeasurementPlanCompletion | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_system_variant_measurement_plan_completions '
                'WHERE completion_id=?',
                (completion_id,),
            ).fetchone()
        if row is None:
            return None
        completion = SystemVariantMeasurementPlanCompletion.model_validate_json(
            row['payload_json']
        )
        return self.save_plan_completion(completion)

    def save_campaign_completion(
        self,
        completion: SystemVariantMeasurementCampaignCompletion,
    ) -> SystemVariantMeasurementCampaignCompletion:
        completion = self._validate_campaign_completion(completion)
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            return self._save_campaign_completion_in_transaction(
                connection,
                completion,
            )

    def _validate_campaign_completion(
        self,
        completion: SystemVariantMeasurementCampaignCompletion,
        *,
        measured: SystemVariantMeasuredRecord | None = None,
        plan_completions: Sequence[
            SystemVariantMeasurementPlanCompletion
        ] | None = None,
    ) -> SystemVariantMeasurementCampaignCompletion:
        """Reproduce campaign completion validity from exact authorities.

        `measured`/`plan_completions` default to the persisted authorities;
        complete_campaign passes the just-validated in-memory artifacts so the
        same checks hold before they are committed in the shared transaction.
        """
        completion = SystemVariantMeasurementCampaignCompletion.model_validate(
            completion.model_dump(mode='python')
        )
        campaign = self.get_campaign(completion.campaign_id)
        if campaign is None or campaign.campaign_sha256 != completion.campaign_sha256:
            raise ValueError('campaign completion campaign authority missing/stale')
        registration = self.get_campaign_registration(completion.campaign_id)
        if registration is None:
            raise ValueError(
                'measurement campaign registration authority missing/stale'
            )
        if (
            completion.campaign_registration_id != registration.registration_id
            or completion.campaign_registration_sha256
            != registration.registration_sha256
        ):
            raise ValueError('campaign completion registration authority mismatch')
        if measured is None:
            measured = self.measured_lifecycle_repository.get(
                completion.measured_record_id
            )
        if measured is None or measured.record_sha256 != completion.measured_record_sha256:
            raise ValueError('campaign completion measured lifecycle missing/stale')
        if (
            measured.as_built_record_id != campaign.as_built_record_id
            or measured.variant_id != campaign.variant_id
            or measured.variant_sha256 != campaign.variant_sha256
        ):
            raise ValueError('campaign completion measured lifecycle authority mismatch')
        by_id = (
            None
            if plan_completions is None
            else {item.completion_id: item for item in plan_completions}
        )
        resolved: list[SystemVariantMeasurementPlanCompletion] = []
        for completion_id, expected_hash in zip(
            completion.plan_completion_ids,
            completion.plan_completion_sha256,
            strict=True,
        ):
            item = (
                self.get_plan_completion(completion_id)
                if by_id is None
                else by_id.get(completion_id)
            )
            if item is None or item.completion_sha256 != expected_hash:
                raise ValueError('campaign completion plan completion missing/stale')
            if item.campaign_id != campaign.campaign_id:
                raise ValueError('campaign completion plan belongs to another campaign')
            resolved.append(item)
        expected_plan_ids = {ref.plan_id for ref in campaign.plan_refs}
        actual_plan_ids = {item.plan_ref.plan_id for item in resolved}
        if actual_plan_ids != expected_plan_ids:
            raise ValueError('campaign completion does not cover exact plan set')

        payload = completion.semantic_payload()
        digest = _digest(payload)
        if digest != completion.completion_sha256:
            raise ValueError('campaign completion failed exact reconstruction')
        return completion

    def _save_campaign_completion_in_transaction(
        self,
        connection: sqlite3.Connection,
        completion: SystemVariantMeasurementCampaignCompletion,
    ) -> SystemVariantMeasurementCampaignCompletion:
        """Persist one validated campaign completion inside the caller's transaction.

        The caller owns BEGIN/COMMIT/ROLLBACK and must have validated the
        completion first; persisted rows were validated on commit. Used by
        save_campaign_completion and by complete_campaign, which commits the
        campaign completion together with every plan completion and the
        measured lifecycle record under one shared BEGIN IMMEDIATE.
        """
        row = connection.execute(
            'SELECT payload_json FROM cad_system_variant_measurement_campaign_completions '
            'WHERE campaign_id=?',
            (completion.campaign_id,),
        ).fetchone()
        if row is not None:
            persisted = SystemVariantMeasurementCampaignCompletion.model_validate_json(
                row['payload_json']
            )
            if persisted != completion:
                raise ValueError(
                    'campaign already has different immutable completion evidence'
                )
            return persisted
        connection.execute(
            """
            INSERT INTO cad_system_variant_measurement_campaign_completions(
                completion_id, completion_sha256, campaign_id,
                measured_record_id, payload_json, recorded_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                completion.completion_id,
                completion.completion_sha256,
                completion.campaign_id,
                completion.measured_record_id,
                completion.model_dump_json(),
                completion.completed_at_utc,
            ),
        )
        return completion

    def get_campaign_completion(
        self,
        campaign_id: str,
    ) -> SystemVariantMeasurementCampaignCompletion | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_system_variant_measurement_campaign_completions '
                'WHERE campaign_id=?',
                (campaign_id,),
            ).fetchone()
        if row is None:
            return None
        completion = SystemVariantMeasurementCampaignCompletion.model_validate_json(
            row['payload_json']
        )
        return self.save_campaign_completion(completion)

    def complete_campaign(
        self,
        *,
        campaign: SystemVariantMeasurementCampaign,
        assignments_by_plan: dict[str, dict[str, Sequence[str]]],
        completed_at_utc: str,
        notes: Sequence[str] = (),
    ) -> tuple[
        SystemVariantMeasurementCampaignCompletion,
        tuple[SystemVariantMeasurementPlanCompletion, ...],
        SystemVariantMeasuredRecord,
    ]:
        """Validate then atomically publish one campaign completion.

        The persisted campaign, its durable registration and the persisted
        plans are re-resolved and every artifact — plan completions, campaign
        completion, measured lifecycle record — is validated before the
        shared write transaction begins. All three then commit or roll back
        together under one BEGIN IMMEDIATE over the shared native database,
        so a fault at any validation/write boundary can never leave a
        half-published completion: either the exact plan completions, the
        campaign completion and the measured lifecycle record are all
        durable, or none of them are.
        """
        campaign = SystemVariantMeasurementCampaign.model_validate(
            campaign.model_dump(mode='python')
        )
        persisted = self.get_campaign(campaign.campaign_id)
        if persisted is None or persisted != campaign:
            raise ValueError(
                'campaign completion requires the exact persisted campaign'
            )
        registration = self.get_campaign_registration(campaign.campaign_id)
        if registration is None:
            raise ValueError(
                'measurement campaign registration authority missing/stale'
            )
        plans = self._campaign_plans(campaign)
        completion, plan_completions, measured = (
            complete_system_variant_measurement_campaign(
                scene_repository=self.scene_repository,
                lifecycle_repository=self.lifecycle_repository,
                campaign=campaign,
                registration=registration,
                plans=plans,
                assignments_by_plan=assignments_by_plan,
                measurement_repository=self.measurement_repository,
                quality_repository=self.quality_repository,
                completed_at_utc=completed_at_utc,
                notes=notes,
            )
        )
        plan_completions = tuple(
            self._validate_plan_completion(item) for item in plan_completions
        )
        measured = self.measured_lifecycle_repository._validate(measured)
        completion = self._validate_campaign_completion(
            completion,
            measured=measured,
            plan_completions=plan_completions,
        )
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            persisted_plans = tuple(
                self._save_plan_completion_in_transaction(connection, item)
                for item in plan_completions
            )
            persisted_completion = self._save_campaign_completion_in_transaction(
                connection,
                completion,
            )
            persisted_measured = (
                self.measured_lifecycle_repository._save_in_transaction(
                    connection,
                    measured,
                )
            )
        return persisted_completion, persisted_plans, persisted_measured
