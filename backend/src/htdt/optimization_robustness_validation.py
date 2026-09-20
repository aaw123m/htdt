from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
from math import isclose
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_measurement_loop import CadMeasurementPlan
from .cad_measurement_quality import MeasurementCapabilityClaim
from .cad_model_validation import CadModelValidationRecord
from .cad_repository import SceneRevision
from .cad_scene import Position3, scene_content_hash
from .cad_validation_campaign import CadValidationCampaign
from .cad_validation_metrics import CadSensitivityCheck
from .optimization_robustness import (
    RobustnessSpec,
    UncertaintyAxis,
    apply_local_perturbation,
)


O90E_SCHEMA_VERSION = 1
O90E_AUTHORITY_VERSION = 'o90e-owned-room-validation-1'

O90EReason = Literal[
    'eligible',
    'missing_underlying_model_validation',
    'missing_preregistration',
    'missing_measurement_evidence',
    'insufficient_measurement_capability',
    'perturbation_domain_outside_validated_applicability',
    'stale_model_or_result',
    'wrong_scene_revision',
    'wrong_candidate',
    'quality_failure',
    'incomplete_required_perturbations',
    'retrospective_evidence',
    'synthetic_evidence',
    'wrong_observable_or_band',
]
O90ESupportState = Literal[
    'full',
    'partially_supported',
    'model_conditioned_only',
    'unsupported',
]


def canonical_o90e_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def canonical_o90e_sha256(value: Any) -> str:
    return sha256(canonical_o90e_json(value).encode('utf-8')).hexdigest()


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}:{digest}'


def _aware_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError('O90E timestamp must be valid ISO 8601') from exc
    if parsed.tzinfo is None:
        raise ValueError('O90E timestamp must be timezone-aware')
    return parsed


def _axis_for_id(spec: RobustnessSpec, axis_id: str) -> UncertaintyAxis:
    for axis in spec.axes:
        if axis.axis_id == axis_id:
            return axis
    raise ValueError(f'O90E case references unknown robustness axis: {axis_id}')


def _position_axis_value(revision: SceneRevision, axis: UncertaintyAxis) -> float:
    if not axis.parameter.endswith('_m'):
        raise ValueError(
            'O90E O60 sensitivity reuse currently supports positional axes only'
        )
    entity = revision.document.entity(axis.entity_id)
    coordinate = (
        axis.parameter.removeprefix('speaker_')
        if axis.parameter.startswith('speaker_')
        else axis.parameter.removeprefix('listener_')
    ).removesuffix('_m')
    return float(getattr(entity.position, f'{coordinate}_m'))


class O90EComparisonRule(BaseModel):
    """Exact preregistered tolerance copied from the existing O60 campaign."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority: Literal['O60_CadSensitivityCheck'] = 'O60_CadSensitivityCheck'
    max_observed_sensitivity_per_m: float = Field(gt=0.0)
    max_model_error_per_m: float = Field(gt=0.0)


class O90EValidationCase(BaseModel):
    """Immutable preregistration for one signed O90 perturbation measurement."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = O90E_SCHEMA_VERSION
    authority_version: Literal[
        'o90e-owned-room-validation-1'
    ] = O90E_AUTHORITY_VERSION

    case_id: str = Field(pattern=r'^o90e-case:[0-9a-f]{64}$')
    case_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    robustness_spec_id: str = Field(min_length=1)
    robustness_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_id: str = Field(min_length=1)
    candidate_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    axis_id: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    parameter: str = Field(min_length=1)
    unit: Literal['m', 'deg']
    direction: Literal['minus', 'plus']
    nominal_value: float
    target_value: float
    target_delta: float

    perturbation_candidate_id: str = Field(min_length=1)
    nominal_measurement_plan_id: str = Field(min_length=1)
    nominal_preregistered_plan_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    perturbation_measurement_plan_id: str = Field(min_length=1)
    perturbation_preregistered_plan_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    observable_id: str = Field(min_length=1)
    requested_band_hz: tuple[float, float]
    receiver_entity_id: str = Field(min_length=1)
    receiver_position: Position3
    channel_role: str = Field(min_length=1)
    source_speaker_ids: tuple[str, ...] = Field(min_length=1)
    radiation_scope: str = Field(min_length=1)
    required_capability: MeasurementCapabilityClaim

    expected_validation_purpose: Literal[
        'reuse_o60_placement_sensitivity'
    ] = 'reuse_o60_placement_sensitivity'
    comparison_rule: O90EComparisonRule

    o60_campaign_id: str = Field(min_length=1)
    o60_campaign_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    preregistration_status: Literal['prospective', 'retrospective']
    preregistered_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_identity(self) -> 'O90EValidationCase':
        low, high = self.requested_band_hz
        if low <= 0 or high <= low:
            raise ValueError('O90E validation case frequency band is invalid')
        if self.source_speaker_ids != tuple(sorted(set(self.source_speaker_ids))):
            raise ValueError('O90E source speaker ids must be unique and sorted')
        _aware_timestamp(self.preregistered_at_utc)
        if self.direction == 'minus' and self.target_delta >= 0:
            raise ValueError('O90E minus case requires a negative target delta')
        if self.direction == 'plus' and self.target_delta <= 0:
            raise ValueError('O90E plus case requires a positive target delta')
        if not isclose(
            self.nominal_value + self.target_delta,
            self.target_value,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError('O90E target state does not match nominal + delta')

        expected = canonical_o90e_sha256(self.identity_payload())
        if self.case_sha256 != expected:
            raise ValueError('O90E validation case semantic hash mismatch')
        if self.case_id != _semantic_id('o90e-case', expected):
            raise ValueError('O90E validation case id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'case_id', 'case_sha256'},
        )


class O90EMeasurementEvidenceRef(BaseModel):
    """Exact existing N60/quality authority used by one O90E assessment."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    role: Literal['nominal', 'perturbation']
    plan_id: str = Field(min_length=1)
    completed_plan_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_id: str = Field(min_length=1)
    measurement_id: str = Field(min_length=1)
    measurement_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    dataset_id: str = Field(min_length=1)
    dataset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    raw_asset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    quality_report_id: str = Field(min_length=1)
    quality_report_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    acquisition_context_id: str | None = Field(default=None, min_length=1)
    acquisition_context_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    capability_claim: MeasurementCapabilityClaim
    capability_decision: Literal['ALLOWED', 'BLOCKED', 'UNKNOWN']
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    measurement_entity_id: str = Field(min_length=1)
    captured_at: str | None = None

    @model_validator(mode='after')
    def valid_acquisition_ref(self) -> 'O90EMeasurementEvidenceRef':
        if (self.acquisition_context_id is None) != (
            self.acquisition_context_sha256 is None
        ):
            raise ValueError('O90E acquisition context id/hash must be paired')
        if self.captured_at is not None:
            _aware_timestamp(self.captured_at)
        return self


class O90EPredictionAuthorityRef(BaseModel):
    """Exact O20 result/config re-resolved through the existing O60 pair."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    role: Literal['nominal', 'perturbation']
    candidate_id: str = Field(min_length=1)
    prediction_attempt_id: str = Field(min_length=1)
    prediction_attempt_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    batch_run_id: str = Field(min_length=1)
    batch_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    model_id: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)


class O90ECaseAssessment(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    case_id: str = Field(pattern=r'^o90e-case:[0-9a-f]{64}$')
    case_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    axis_id: str = Field(min_length=1)
    direction: Literal['minus', 'plus']
    target_delta: float
    status: Literal['supported', 'unsupported', 'missing']
    reasons: tuple[O90EReason, ...]
    sensitivity_evidence_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    nominal_measurement: O90EMeasurementEvidenceRef | None = None
    perturbation_measurement: O90EMeasurementEvidenceRef | None = None
    nominal_prediction: O90EPredictionAuthorityRef | None = None
    perturbation_prediction: O90EPredictionAuthorityRef | None = None


class O90EAxisCoverage(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    axis_id: str = Field(min_length=1)
    required_minus_delta: float = Field(gt=0.0)
    required_plus_delta: float = Field(gt=0.0)
    tested_minus_delta: float | None = None
    tested_plus_delta: float | None = None
    state: Literal['full', 'partial', 'none']


class O90EValidationDecision(BaseModel):
    """Immutable production-owned-room gate decision; no independent validation flag."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = O90E_SCHEMA_VERSION
    authority_version: Literal[
        'o90e-owned-room-validation-1'
    ] = O90E_AUTHORITY_VERSION

    decision_id: str = Field(pattern=r'^o90e-decision:[0-9a-f]{64}$')
    decision_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    robustness_spec_id: str = Field(min_length=1)
    robustness_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_id: str = Field(min_length=1)
    candidate_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')

    model_id: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    prediction_provider_id: str = Field(min_length=1)
    fidelity: str = Field(min_length=1)
    objective_evaluation_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    underlying_authority_kind: Literal['O60'] = 'O60'
    o60_validation_id: str = Field(min_length=1)
    o60_validation_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    o60_campaign_id: str | None = Field(default=None, min_length=1)
    o60_campaign_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )

    assessments: tuple[O90ECaseAssessment, ...]
    axis_coverage: tuple[O90EAxisCoverage, ...]

    support_state: O90ESupportState
    production_gate: Literal['eligible', 'closed']
    reasons: tuple[O90EReason, ...] = Field(min_length=1)

    decided_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_identity(self) -> 'O90EValidationDecision':
        _aware_timestamp(self.decided_at_utc)
        if (self.o60_campaign_id is None) != (self.o60_campaign_sha256 is None):
            raise ValueError('O90E O60 campaign id/hash must be paired')
        if self.production_gate == 'eligible':
            if self.o60_campaign_id is None or self.o60_campaign_sha256 is None:
                raise ValueError('O90E eligible decision requires O60 campaign authority')
            if self.support_state != 'full' or self.reasons != ('eligible',):
                raise ValueError(
                    'O90E eligible gate requires full support and eligible-only reason'
                )
            if any(item.status != 'supported' for item in self.assessments):
                raise ValueError('O90E eligible decision requires supported cases')
            if any(item.state != 'full' for item in self.axis_coverage):
                raise ValueError('O90E eligible decision requires full axis coverage')
            if self.o60_validation_sha256 is None:
                raise ValueError('O90E eligible decision requires resolved O60 authority')
        elif 'eligible' in self.reasons:
            raise ValueError('closed O90E decision must not contain eligible reason')

        if len(self.reasons) != len(set(self.reasons)):
            raise ValueError('O90E decision reasons must be unique')
        case_ids = [item.case_id for item in self.assessments]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError('O90E decision assessments must be unique by case')
        axis_ids = [item.axis_id for item in self.axis_coverage]
        if len(axis_ids) != len(set(axis_ids)):
            raise ValueError('O90E axis coverage must be unique by axis')

        expected = canonical_o90e_sha256(self.identity_payload())
        if self.decision_sha256 != expected:
            raise ValueError('O90E decision semantic hash mismatch')
        if self.decision_id != _semantic_id('o90e-decision', expected):
            raise ValueError('O90E decision id mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'decision_id', 'decision_sha256'},
        )


def matching_campaign_sensitivity(
    campaign: CadValidationCampaign,
    *,
    observable_id: str,
    candidate_a_id: str,
    candidate_b_id: str,
):
    pair = tuple(sorted((candidate_a_id, candidate_b_id)))
    matches = tuple(
        item
        for item in campaign.sensitivity
        if item.objective_id == observable_id
        and tuple(sorted((item.candidate_a_id, item.candidate_b_id))) == pair
    )
    if len(matches) != 1:
        raise ValueError(
            'O90E case requires one exact preregistered O60 sensitivity pair'
        )
    return matches[0]


def matching_o60_sensitivity(
    record: CadModelValidationRecord,
    case: O90EValidationCase,
) -> CadSensitivityCheck | None:
    pair = tuple(sorted((case.candidate_id, case.perturbation_candidate_id)))
    matches = tuple(
        item
        for item in record.sensitivity_checks
        if item.objective_id == case.observable_id
        and tuple(sorted((item.candidate_a_id, item.candidate_b_id))) == pair
    )
    if len(matches) != 1:
        return None
    check = matches[0]
    if not isclose(
        check.placement_delta_m,
        abs(case.target_delta),
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        return None
    return check


def build_o90e_validation_case(
    *,
    spec: RobustnessSpec,
    axis_id: str,
    direction: Literal['minus', 'plus'],
    nominal_plan: CadMeasurementPlan,
    perturbation_plan: CadMeasurementPlan,
    nominal_revision: SceneRevision,
    perturbation_revision: SceneRevision,
    campaign: CadValidationCampaign,
    observable_id: str,
    receiver_entity_id: str,
    required_capability: MeasurementCapabilityClaim,
    channel_role: str,
    source_speaker_ids: Sequence[str],
    radiation_scope: str,
    preregistered_at_utc: str,
) -> O90EValidationCase:
    """Freeze a strict specialization of an existing O60 campaign before capture."""

    preregistered_at = _aware_timestamp(preregistered_at_utc)
    campaign_created_at = _aware_timestamp(campaign.created_at_utc)
    if preregistered_at < campaign_created_at:
        raise ValueError('O90E case cannot predate its O60 preregistration campaign')
    axis = _axis_for_id(spec, axis_id)
    if not axis.parameter.endswith('_m'):
        raise ValueError(
            'O90E O60 sensitivity reuse currently supports positional axes only'
        )

    if campaign.document_id != spec.document_id:
        raise ValueError('O90E campaign belongs to another document')
    if (
        campaign.search_spec_id != spec.search_spec_id
        or campaign.search_spec_sha256 != spec.search_spec_sha256
        or campaign.candidate_set_sha256 != spec.candidate_set_sha256
    ):
        raise ValueError('O90E campaign/search authority mismatch')
    if campaign.model_id != spec.model_id or campaign.model_version != spec.model_version:
        raise ValueError('O90E campaign/model authority mismatch')
    if campaign.objective_evaluation_spec_sha256 != spec.objective_evaluation_spec_sha256:
        raise ValueError('O90E observable authority does not match O60 campaign')
    if observable_id not in campaign.objective_ids:
        raise ValueError('O90E observable is not preregistered by O60 campaign')
    requested_band_hz = tuple(float(v) for v in campaign.requested_band_hz)

    if (
        nominal_plan.document_id != spec.document_id
        or nominal_plan.search_spec_id != spec.search_spec_id
        or nominal_plan.search_spec_sha256 != spec.search_spec_sha256
        or nominal_plan.candidate_id != spec.candidate_id
        or nominal_plan.candidate_set_sha256 != spec.candidate_set_sha256
    ):
        raise ValueError('O90E nominal MeasurementPlan/candidate authority mismatch')
    if (
        perturbation_plan.document_id != spec.document_id
        or perturbation_plan.search_spec_id != spec.search_spec_id
        or perturbation_plan.search_spec_sha256 != spec.search_spec_sha256
        or perturbation_plan.candidate_set_sha256 != spec.candidate_set_sha256
    ):
        raise ValueError('O90E perturbation MeasurementPlan authority mismatch')
    if perturbation_plan.candidate_id == spec.candidate_id:
        raise ValueError('O90E perturbation must use a distinct candidate')

    for plan, revision, label in (
        (nominal_plan, nominal_revision, 'nominal'),
        (perturbation_plan, perturbation_revision, 'perturbation'),
    ):
        if (
            revision.revision_id != plan.applied_scene_revision_id
            or revision.document_id != plan.document_id
            or revision.content_hash != plan.applied_scene_content_hash
        ):
            raise ValueError(f'O90E {label} MeasurementPlan SceneRevision mismatch')

    nominal_value = _position_axis_value(nominal_revision, axis)
    if not isclose(nominal_value, axis.nominal_value, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError('O90E nominal MeasurementPlan does not match axis nominal state')
    target_delta = (
        -float(axis.minus_delta)
        if direction == 'minus'
        else float(axis.plus_delta)
    )
    target_value = nominal_value + target_delta
    expected_document = apply_local_perturbation(
        nominal_revision.document,
        axis,
        target_delta,
    )
    if scene_content_hash(expected_document) != perturbation_revision.content_hash:
        raise ValueError(
            'O90E perturbation candidate is not the exact declared one-axis state'
        )

    receiver_nominal = nominal_revision.document.entity(receiver_entity_id)
    receiver_perturbed = perturbation_revision.document.entity(receiver_entity_id)
    if receiver_nominal.position != receiver_perturbed.position:
        raise ValueError('O90E receiver position must stay fixed for sensitivity reuse')

    source_ids = tuple(sorted(set(source_speaker_ids)))
    if not source_ids:
        raise ValueError('O90E validation case requires source speaker ids')
    if len(source_ids) != len(tuple(source_speaker_ids)):
        raise ValueError('O90E source speaker ids must be unique')
    for source_id in source_ids:
        nominal_revision.document.entity(source_id)
        perturbation_revision.document.entity(source_id)

    requirement = matching_campaign_sensitivity(
        campaign,
        observable_id=observable_id,
        candidate_a_id=spec.candidate_id,
        candidate_b_id=perturbation_plan.candidate_id,
    )
    rule = O90EComparisonRule(
        max_observed_sensitivity_per_m=requirement.max_observed_sensitivity_per_m,
        max_model_error_per_m=requirement.max_model_error_per_m,
    )

    status: Literal['prospective', 'retrospective'] = (
        'prospective'
        if nominal_plan.status == 'planned' and perturbation_plan.status == 'planned'
        else 'retrospective'
    )
    payload = {
        'schema_version': O90E_SCHEMA_VERSION,
        'authority_version': O90E_AUTHORITY_VERSION,
        'robustness_spec_id': spec.robustness_spec_id,
        'robustness_spec_sha256': spec.robustness_spec_sha256,
        'document_id': spec.document_id,
        'scene_revision_id': spec.scene_revision_id,
        'scene_content_hash': spec.scene_content_hash,
        'candidate_id': spec.candidate_id,
        'candidate_sha256': spec.candidate_sha256,
        'axis_id': axis.axis_id,
        'entity_id': axis.entity_id,
        'parameter': axis.parameter,
        'unit': axis.unit,
        'direction': direction,
        'nominal_value': nominal_value,
        'target_value': target_value,
        'target_delta': target_delta,
        'perturbation_candidate_id': perturbation_plan.candidate_id,
        'nominal_measurement_plan_id': nominal_plan.plan_id,
        'nominal_preregistered_plan_sha256': nominal_plan.plan_sha256,
        'perturbation_measurement_plan_id': perturbation_plan.plan_id,
        'perturbation_preregistered_plan_sha256': perturbation_plan.plan_sha256,
        'observable_id': observable_id,
        'requested_band_hz': requested_band_hz,
        'receiver_entity_id': receiver_entity_id,
        'receiver_position': receiver_nominal.position,
        'channel_role': channel_role,
        'source_speaker_ids': source_ids,
        'radiation_scope': radiation_scope,
        'required_capability': required_capability,
        'expected_validation_purpose': 'reuse_o60_placement_sensitivity',
        'comparison_rule': rule,
        'o60_campaign_id': campaign.campaign_id,
        'o60_campaign_sha256': campaign.campaign_sha256,
        'preregistration_status': status,
        'preregistered_at_utc': preregistered_at_utc,
    }
    provisional = O90EValidationCase.model_construct(
        **payload,
        case_id='o90e-case:' + ('0' * 64),
        case_sha256='0' * 64,
    )
    digest = canonical_o90e_sha256(provisional.identity_payload())
    return O90EValidationCase(
        **payload,
        case_id=_semantic_id('o90e-case', digest),
        case_sha256=digest,
    )


def sensitivity_evidence_sha256(check: CadSensitivityCheck) -> str:
    return canonical_o90e_sha256(check.model_dump(mode='json'))


def build_o90e_decision(
    *,
    spec: RobustnessSpec,
    validation_id: str,
    validation_sha256: str | None,
    campaign_id: str | None,
    campaign_sha256: str | None,
    assessments: Sequence[O90ECaseAssessment],
    axis_coverage: Sequence[O90EAxisCoverage],
    support_state: O90ESupportState,
    reasons: Sequence[O90EReason],
    decided_at_utc: str,
) -> O90EValidationDecision:
    ordered_reasons = tuple(dict.fromkeys(reasons))
    if not ordered_reasons:
        ordered_reasons = ('eligible',)
    gate: Literal['eligible', 'closed'] = (
        'eligible' if ordered_reasons == ('eligible',) else 'closed'
    )
    payload = {
        'schema_version': O90E_SCHEMA_VERSION,
        'authority_version': O90E_AUTHORITY_VERSION,
        'robustness_spec_id': spec.robustness_spec_id,
        'robustness_spec_sha256': spec.robustness_spec_sha256,
        'candidate_id': spec.candidate_id,
        'candidate_sha256': spec.candidate_sha256,
        'scene_revision_id': spec.scene_revision_id,
        'scene_content_hash': spec.scene_content_hash,
        'model_id': spec.model_id,
        'model_version': spec.model_version,
        'prediction_provider_id': spec.prediction_provider_id,
        'fidelity': spec.fidelity,
        'objective_evaluation_spec_sha256': spec.objective_evaluation_spec_sha256,
        'underlying_authority_kind': 'O60',
        'o60_validation_id': validation_id,
        'o60_validation_sha256': validation_sha256,
        'o60_campaign_id': campaign_id,
        'o60_campaign_sha256': campaign_sha256,
        'assessments': tuple(assessments),
        'axis_coverage': tuple(axis_coverage),
        'support_state': support_state,
        'production_gate': gate,
        'reasons': ordered_reasons,
        'decided_at_utc': decided_at_utc,
    }
    provisional = O90EValidationDecision.model_construct(
        **payload,
        decision_id='o90e-decision:' + ('0' * 64),
        decision_sha256='0' * 64,
    )
    digest = canonical_o90e_sha256(provisional.identity_payload())
    return O90EValidationDecision(
        **payload,
        decision_id=_semantic_id('o90e-decision', digest),
        decision_sha256=digest,
    )
