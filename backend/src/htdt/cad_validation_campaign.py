from __future__ import annotations

from datetime import datetime, timezone
import json
from math import isfinite
from typing import Any, Literal, Mapping, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_objective_models import canonical_objective_json, canonical_objective_sha256


CAMPAIGN_SCHEMA_VERSION = 1
O60_CAMPAIGN_REGISTRATION_AUTHORITY_VERSION = 'o60-validation-campaign-registration-1'


def _parse_timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'{label} must be ISO-8601') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{label} must be timezone-aware')
    return parsed


def _utc_now() -> str:
    """Caller-side campaign build clock; never proof of durable preregistration."""
    return datetime.now(timezone.utc).isoformat()


class CadValidationCampaignCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate_id: str = Field(min_length=1)
    split: Literal['calibration', 'holdout']


class CadValidationTargetResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    frequency_hz: tuple[float, ...] = Field(min_length=2)
    level_db: tuple[float, ...] = Field(min_length=2)

    @model_validator(mode='after')
    def valid_response(self) -> 'CadValidationTargetResponse':
        if len(self.frequency_hz) != len(self.level_db):
            raise ValueError('validation target frequency and level arrays must match')
        previous = 0.0
        for frequency in self.frequency_hz:
            value = float(frequency)
            if not isfinite(value) or value <= 0:
                raise ValueError('validation target frequencies must be finite and positive')
            if previous and value <= previous:
                raise ValueError('validation target frequencies must be strictly increasing')
            previous = value
        if any(not isfinite(float(value)) for value in self.level_db):
            raise ValueError('validation target levels must be finite')
        return self


def _evaluation_spec(
    *,
    objective_ids: Sequence[str],
    requested_band_hz: tuple[float, float],
    target_response: CadValidationTargetResponse,
    reference_band_hz: tuple[float, float] | None,
    excluded_bands: Sequence[tuple[float, float]],
) -> dict[str, Any]:
    return {
        'algorithm_version': 'objective-vector-1',
        'objective_method': 'target_response',
        'objectives': list(objective_ids),
        'response_band_hz': list(requested_band_hz),
        'reference_band_hz': (
            None if reference_band_hz is None else list(reference_band_hz)
        ),
        'excluded_bands': [list(item) for item in excluded_bands],
        'target_response': target_response.model_dump(mode='json'),
    }


class CadValidationCampaignSensitivity(BaseModel):
    model_config = ConfigDict(frozen=True)

    objective_id: str = Field(min_length=1)
    candidate_a_id: str = Field(min_length=1)
    candidate_b_id: str = Field(min_length=1)
    max_observed_sensitivity_per_m: float = Field(gt=0)
    max_model_error_per_m: float = Field(gt=0)


class CadValidationCampaignRepeatability(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate_id: str = Field(min_length=1)
    min_measurements: int = Field(default=2, ge=2)
    reference_band_hz: tuple[float, float] | None = None

    @model_validator(mode='after')
    def valid_band(self) -> 'CadValidationCampaignRepeatability':
        if self.reference_band_hz is not None:
            low, high = self.reference_band_hz
            if low <= 0 or high <= low:
                raise ValueError('repeatability reference band is invalid')
        return self


class CadValidationCampaignSeparation(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate_a_id: str = Field(min_length=1)
    candidate_b_id: str = Field(min_length=1)
    repeatability_candidate_id: str = Field(min_length=1)
    min_repeatability_multiple: float = Field(gt=0)


class CadValidationCampaign(BaseModel):
    """Immutable preregistration intent for a real owned-room O60 study.

    `created_at_utc` is the caller's claimed campaign-build timestamp. It is
    part of the campaign identity, but it is not proof of durable
    preregistration: only the repository-attested
    `CadValidationCampaignRegistration` establishes the registration authority
    that downstream evidence and model-validation gates compare against.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = CAMPAIGN_SCHEMA_VERSION
    campaign_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    search_spec_id: str = Field(min_length=1)
    search_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_set_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    model_id: str = Field(min_length=1)
    model_version: str = Field(min_length=1)

    requested_band_hz: tuple[float, float]
    max_holdout_rms_db: float = Field(gt=0)

    candidates: tuple[CadValidationCampaignCandidate, ...] = Field(min_length=2)
    objective_ids: tuple[str, ...] = Field(min_length=1)
    target_response: CadValidationTargetResponse
    reference_band_hz: tuple[float, float] | None = None
    excluded_bands: tuple[tuple[float, float], ...] = ()
    objective_evaluation_spec_json: str = Field(min_length=2)
    objective_evaluation_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    trend_tolerance_by_objective: dict[str, float] = Field(default_factory=dict)
    trend_min_comparable_pairs: int = Field(default=1, ge=1)
    trend_min_agreement_ratio: float = Field(default=0.75, ge=0, le=1)

    sensitivity: tuple[CadValidationCampaignSensitivity, ...] = Field(min_length=1)
    repeatability: tuple[CadValidationCampaignRepeatability, ...] = Field(min_length=1)
    separation: tuple[CadValidationCampaignSeparation, ...] = Field(min_length=1)
    required_applicability_codes: tuple[str, ...] = Field(min_length=1)

    created_at_utc: str = Field(min_length=1)
    campaign_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_identity(self) -> 'CadValidationCampaign':
        low, high = self.requested_band_hz
        if low <= 0 or high <= low:
            raise ValueError('validation campaign frequency band is invalid')

        candidate_ids = [item.candidate_id for item in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError('validation campaign candidate ids must be unique')
        split_by_candidate = {item.candidate_id: item.split for item in self.candidates}
        if 'calibration' not in split_by_candidate.values():
            raise ValueError('validation campaign requires calibration candidates')
        holdout_ids = [
            candidate_id
            for candidate_id, split in split_by_candidate.items()
            if split == 'holdout'
        ]
        if len(holdout_ids) < 2:
            raise ValueError('validation campaign requires at least two holdout candidates')

        if len(self.objective_ids) != len(set(self.objective_ids)):
            raise ValueError('validation campaign objective ids must be unique')
        supported_objectives = {
            'response.rms_difference_db',
            'response.peak_excess_db',
            'response.dip_deficit_db',
            'response.shape_rms_db',
        }
        if not set(self.objective_ids).issubset(supported_objectives):
            raise ValueError('validation campaign contains unsupported acoustic objectives')
        if (
            'response.shape_rms_db' in self.objective_ids
            and self.reference_band_hz is None
        ):
            raise ValueError('response.shape_rms_db requires a reference band')
        if self.target_response.frequency_hz[0] > low or self.target_response.frequency_hz[-1] < high:
            raise ValueError('validation target response must cover the requested band')
        if self.reference_band_hz is not None:
            ref_low, ref_high = self.reference_band_hz
            if ref_low <= 0 or ref_high <= ref_low:
                raise ValueError('validation campaign reference band is invalid')
            if (
                self.target_response.frequency_hz[0] > ref_low
                or self.target_response.frequency_hz[-1] < ref_high
            ):
                raise ValueError('validation target response must cover the reference band')
        for excluded_low, excluded_high in self.excluded_bands:
            if excluded_low <= 0 or excluded_high < excluded_low:
                raise ValueError('validation campaign excluded band is invalid')
        if any(value < 0 for value in self.trend_tolerance_by_objective.values()):
            raise ValueError('validation campaign trend tolerances must be non-negative')
        if not set(self.trend_tolerance_by_objective).issubset(self.objective_ids):
            raise ValueError('trend tolerance references an objective outside the campaign')

        try:
            evaluation_spec = json.loads(self.objective_evaluation_spec_json)
        except json.JSONDecodeError as exc:
            raise ValueError('campaign objective evaluation spec must contain JSON') from exc
        if canonical_objective_json(evaluation_spec) != self.objective_evaluation_spec_json:
            raise ValueError('campaign objective evaluation spec must be canonical JSON')
        if canonical_objective_sha256(evaluation_spec) != self.objective_evaluation_spec_sha256:
            raise ValueError('campaign objective evaluation spec hash mismatch')
        expected_evaluation_spec = _evaluation_spec(
            objective_ids=self.objective_ids,
            requested_band_hz=self.requested_band_hz,
            target_response=self.target_response,
            reference_band_hz=self.reference_band_hz,
            excluded_bands=self.excluded_bands,
        )
        if evaluation_spec != expected_evaluation_spec:
            raise ValueError('campaign objective evaluation spec does not match preregistration')

        sensitivity_keys: set[tuple[str, tuple[str, str]]] = set()
        for requirement in self.sensitivity:
            if requirement.objective_id not in self.objective_ids:
                raise ValueError('campaign sensitivity references an unknown objective')
            pair = tuple(sorted((requirement.candidate_a_id, requirement.candidate_b_id)))
            if pair[0] == pair[1]:
                raise ValueError('campaign sensitivity requires two candidates')
            if not set(pair).issubset(split_by_candidate):
                raise ValueError('campaign sensitivity references an unknown candidate')
            if any(split_by_candidate[item] != 'holdout' for item in pair):
                raise ValueError('campaign sensitivity requires holdout candidates')
            key = (requirement.objective_id, pair)
            if key in sensitivity_keys:
                raise ValueError('campaign sensitivity requirements must be unique')
            sensitivity_keys.add(key)

        repeatability_ids = [item.candidate_id for item in self.repeatability]
        if len(repeatability_ids) != len(set(repeatability_ids)):
            raise ValueError('campaign repeatability candidates must be unique')
        if not set(repeatability_ids).issubset(split_by_candidate):
            raise ValueError('campaign repeatability references an unknown candidate')

        separation_keys: set[tuple[str, str]] = set()
        for requirement in self.separation:
            if requirement.candidate_a_id == requirement.candidate_b_id:
                raise ValueError('campaign candidate separation requires two candidates')
            if not {
                requirement.candidate_a_id,
                requirement.candidate_b_id,
                requirement.repeatability_candidate_id,
            }.issubset(split_by_candidate):
                raise ValueError('campaign candidate separation references an unknown candidate')
            if requirement.repeatability_candidate_id not in repeatability_ids:
                raise ValueError(
                    'campaign candidate separation requires a preregistered repeatability candidate'
                )
            key = tuple(sorted((requirement.candidate_a_id, requirement.candidate_b_id)))
            if key in separation_keys:
                raise ValueError('campaign candidate separation requirements must be unique')
            separation_keys.add(key)

        codes = self.required_applicability_codes
        if len(codes) != len(set(codes)) or any(not code for code in codes):
            raise ValueError('campaign applicability codes must be unique non-empty values')

        _parse_timestamp(
            self.created_at_utc,
            'validation campaign created_at_utc',
        )

        if self.campaign_sha256 != canonical_objective_sha256(self.identity_payload()):
            raise ValueError('validation campaign identity hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'document_id': self.document_id,
            'search_spec_id': self.search_spec_id,
            'search_spec_sha256': self.search_spec_sha256,
            'candidate_set_sha256': self.candidate_set_sha256,
            'model_id': self.model_id,
            'model_version': self.model_version,
            'requested_band_hz': list(self.requested_band_hz),
            'max_holdout_rms_db': self.max_holdout_rms_db,
            'candidates': [item.model_dump(mode='json') for item in self.candidates],
            'objective_ids': list(self.objective_ids),
            'target_response': self.target_response.model_dump(mode='json'),
            'reference_band_hz': (
                None if self.reference_band_hz is None else list(self.reference_band_hz)
            ),
            'excluded_bands': [list(item) for item in self.excluded_bands],
            'objective_evaluation_spec': json.loads(self.objective_evaluation_spec_json),
            'objective_evaluation_spec_sha256': self.objective_evaluation_spec_sha256,
            'trend_tolerance_by_objective': dict(sorted(self.trend_tolerance_by_objective.items())),
            'trend_min_comparable_pairs': self.trend_min_comparable_pairs,
            'trend_min_agreement_ratio': self.trend_min_agreement_ratio,
            'sensitivity': [item.model_dump(mode='json') for item in self.sensitivity],
            'repeatability': [item.model_dump(mode='json') for item in self.repeatability],
            'separation': [item.model_dump(mode='json') for item in self.separation],
            'required_applicability_codes': list(self.required_applicability_codes),
            'created_at_utc': self.created_at_utc,
        }


def build_validation_campaign(
    *,
    document_id: str,
    search_spec_id: str,
    search_spec_sha256: str,
    candidate_set_sha256: str,
    model_id: str,
    model_version: str,
    requested_band_hz: tuple[float, float],
    max_holdout_rms_db: float,
    candidates: Sequence[CadValidationCampaignCandidate],
    objective_ids: Sequence[str],
    target_response: CadValidationTargetResponse,
    reference_band_hz: tuple[float, float] | None = None,
    excluded_bands: Sequence[tuple[float, float]] = (),
    trend_tolerance_by_objective: Mapping[str, float] | None = None,
    trend_min_comparable_pairs: int = 1,
    trend_min_agreement_ratio: float = 0.75,
    sensitivity: Sequence[CadValidationCampaignSensitivity] = (),
    repeatability: Sequence[CadValidationCampaignRepeatability] = (),
    separation: Sequence[CadValidationCampaignSeparation] = (),
    required_applicability_codes: Sequence[str] = (),
) -> CadValidationCampaign:
    objective_evaluation_spec = _evaluation_spec(
        objective_ids=objective_ids,
        requested_band_hz=requested_band_hz,
        target_response=target_response,
        reference_band_hz=reference_band_hz,
        excluded_bands=excluded_bands,
    )
    spec_json = canonical_objective_json(objective_evaluation_spec)
    spec_sha = canonical_objective_sha256(objective_evaluation_spec)
    payload = {
        'document_id': document_id,
        'search_spec_id': search_spec_id,
        'search_spec_sha256': search_spec_sha256,
        'candidate_set_sha256': candidate_set_sha256,
        'model_id': model_id,
        'model_version': model_version,
        'requested_band_hz': tuple(float(value) for value in requested_band_hz),
        'max_holdout_rms_db': float(max_holdout_rms_db),
        'candidates': tuple(candidates),
        'objective_ids': tuple(objective_ids),
        'target_response': target_response,
        'reference_band_hz': reference_band_hz,
        'excluded_bands': tuple(excluded_bands),
        'objective_evaluation_spec_json': spec_json,
        'objective_evaluation_spec_sha256': spec_sha,
        'trend_tolerance_by_objective': dict(trend_tolerance_by_objective or {}),
        'trend_min_comparable_pairs': trend_min_comparable_pairs,
        'trend_min_agreement_ratio': trend_min_agreement_ratio,
        'sensitivity': tuple(sensitivity),
        'repeatability': tuple(repeatability),
        'separation': tuple(separation),
        'required_applicability_codes': tuple(required_applicability_codes),
    }
    provisional = CadValidationCampaign.model_construct(
        campaign_id=str(uuid4()),
        created_at_utc=_utc_now(),
        campaign_sha256='0' * 64,
        **payload,
    )
    return CadValidationCampaign(
        **provisional.model_dump(exclude={'campaign_sha256'}),
        campaign_sha256=canonical_objective_sha256(provisional.identity_payload()),
    )


class CadValidationCampaignRegistration(BaseModel):
    """Durable repository-attested preregistration for one exact campaign.

    `registered_at_utc` is generated by `CadValidationCampaignRepository`
    inside the campaign commit transaction after the no-qualifying-evidence
    check passes. It is the only timestamp that downstream captured-time and
    model-validation gates may treat as durable preregistration; the caller's
    `CadValidationCampaign.created_at_utc` remains identity data only.
    """

    model_config = ConfigDict(frozen=True)

    authority_version: Literal['o60-validation-campaign-registration-1'] = (
        O60_CAMPAIGN_REGISTRATION_AUTHORITY_VERSION
    )
    registration_id: str = Field(
        pattern=r'^o60-validation-campaign-registration:[0-9a-f]{64}$'
    )
    registration_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    campaign_id: str = Field(min_length=1)
    campaign_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    registered_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_identity(self) -> 'CadValidationCampaignRegistration':
        _parse_timestamp(
            self.registered_at_utc,
            'campaign registration registered_at_utc',
        )
        expected = canonical_objective_sha256(self.semantic_payload())
        if self.registration_sha256 != expected:
            raise ValueError(
                'validation campaign registration semantic hash mismatch'
            )
        if self.registration_id != f'o60-validation-campaign-registration:{expected}':
            raise ValueError('validation campaign registration id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'registration_id', 'registration_sha256'},
        )


def build_validation_campaign_registration(
    *,
    campaign: CadValidationCampaign,
    registered_at_utc: str,
) -> CadValidationCampaignRegistration:
    payload = {
        'authority_version': O60_CAMPAIGN_REGISTRATION_AUTHORITY_VERSION,
        'campaign_id': campaign.campaign_id,
        'campaign_sha256': campaign.campaign_sha256,
        'registered_at_utc': registered_at_utc,
    }
    digest = canonical_objective_sha256(payload)
    return CadValidationCampaignRegistration(
        registration_id=f'o60-validation-campaign-registration:{digest}',
        registration_sha256=digest,
        **payload,
    )


def exact_campaign_registration(
    campaign: CadValidationCampaign,
    registration: CadValidationCampaignRegistration,
) -> CadValidationCampaignRegistration:
    """Require that a registration attests this exact campaign.

    The durable registration time must not predate the caller-claimed
    `created_at_utc` embedded in the campaign identity.
    """
    if (
        registration.campaign_id != campaign.campaign_id
        or registration.campaign_sha256 != campaign.campaign_sha256
    ):
        raise ValueError('validation campaign registration authority mismatch')
    registered = _parse_timestamp(
        registration.registered_at_utc,
        'campaign registration registered_at_utc',
    )
    claimed = _parse_timestamp(
        campaign.created_at_utc,
        'validation campaign created_at_utc',
    )
    if registered < claimed:
        raise ValueError('campaign registration cannot predate claimed preregistration')
    return registration
