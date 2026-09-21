from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Literal, Mapping, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_validation_metrics import (
    CadApplicabilityCheck,
    CadCandidateSeparationCheck,
    CadObjectiveValidationSample,
    CadRepeatabilityCheck,
    CadSensitivityCheck,
    CadTrendCheck,
    build_trend_checks,
)
from .comparison import FrequencyResponse, compare_frequency_responses


VALIDATION_ALGORITHM_VERSION = 'model-validation-2'
EvidenceScope = Literal['synthetic_fixture', 'owned_room']


def _canon(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _hash(value: Any) -> str:
    return sha256(_canon(value).encode('utf-8')).hexdigest()


class CadValidationPair(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate_id: str = Field(min_length=1)
    split: Literal['calibration', 'holdout']
    prediction_source_id: str = Field(min_length=1)
    measurement_id: str = Field(min_length=1)
    rms_difference_db: float = Field(ge=0)
    shape_rms_db: float | None = Field(default=None, ge=0)


def _advanced_gate_reasons(
    *,
    evidence_scope: EvidenceScope,
    residual_gate: Literal['pass', 'fail', 'insufficient'],
    pairs: Sequence[CadValidationPair],
    objective_samples: Sequence[CadObjectiveValidationSample],
    trend_checks: Sequence[CadTrendCheck],
    sensitivity_checks: Sequence[CadSensitivityCheck],
    repeatability_checks: Sequence[CadRepeatabilityCheck],
    separation_checks: Sequence[CadCandidateSeparationCheck],
    applicability_checks: Sequence[CadApplicabilityCheck],
) -> tuple[str, ...]:
    reasons: list[str] = []
    if evidence_scope != 'owned_room':
        reasons.append('automatic recommendation requires owned-room evidence')
    calibration_candidates = {pair.candidate_id for pair in pairs if pair.split == 'calibration'}
    holdout_candidates = {pair.candidate_id for pair in pairs if pair.split == 'holdout'}
    if not calibration_candidates:
        reasons.append('independent calibration evidence is required')
    if not holdout_candidates:
        reasons.append('independent holdout evidence is required')
    if calibration_candidates & holdout_candidates:
        reasons.append('calibration and holdout candidate sets must be disjoint')
    if residual_gate != 'pass':
        reasons.append(f'holdout residual gate is {residual_gate}')

    holdout_by_objective: dict[str, set[str]] = {}
    for sample in objective_samples:
        if sample.split == 'holdout':
            holdout_by_objective.setdefault(sample.objective_id, set()).add(sample.candidate_id)
    if not holdout_by_objective:
        reasons.append('holdout objective trend evidence is required')
    trend_ids = {check.objective_id for check in trend_checks}
    for objective_id, candidate_ids in holdout_by_objective.items():
        if len(candidate_ids) < 2:
            reasons.append(f'objective trend {objective_id} has fewer than two holdout candidates')
        elif objective_id not in trend_ids:
            reasons.append(f'objective trend {objective_id} check is missing')
    for check in trend_checks:
        if check.gate != 'pass':
            reasons.append(f'objective trend {check.objective_id} is {check.gate}')

    if not sensitivity_checks:
        reasons.append('placement sensitivity evidence is required')
    for check in sensitivity_checks:
        if check.gate != 'pass':
            reasons.append(
                f'placement sensitivity {check.objective_id} '
                f'{check.candidate_a_id}/{check.candidate_b_id} failed'
            )

    if not repeatability_checks:
        reasons.append('same-condition repeatability evidence is required')

    if not separation_checks:
        reasons.append('candidate separation vs repeatability evidence is required')
    for check in separation_checks:
        if check.gate != 'pass':
            reasons.append(
                f'candidate separation {check.candidate_a_id}/{check.candidate_b_id} '
                'is not above repeatability floor'
            )

    if not applicability_checks:
        reasons.append('model applicability checks are required')
    for check in applicability_checks:
        if not check.passed:
            reasons.append(f'model applicability failed: {check.code}')

    return tuple(reasons)


class CadModelValidationRecord(BaseModel):
    """Immutable O60 validation bound to exact search/prediction/measurement authority."""

    model_config = ConfigDict(frozen=True)

    validation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    search_spec_id: str = Field(min_length=1)
    search_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_set_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    campaign_id: str | None = Field(default=None, min_length=1)
    campaign_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    campaign_registration_id: str | None = Field(
        default=None,
        pattern=r'^o60-validation-campaign-registration:[0-9a-f]{64}$',
    )
    campaign_registration_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    model_id: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    evidence_scope: EvidenceScope = 'synthetic_fixture'
    requested_band_hz: tuple[float, float]
    pairs: tuple[CadValidationPair, ...] = Field(min_length=1)
    holdout_rms_db: float | None = Field(default=None, ge=0)
    calibration_rms_db: float | None = Field(default=None, ge=0)
    max_holdout_rms_db: float = Field(gt=0)
    residual_gate: Literal['pass', 'fail', 'insufficient']

    objective_samples: tuple[CadObjectiveValidationSample, ...] = ()
    trend_checks: tuple[CadTrendCheck, ...] = ()
    sensitivity_checks: tuple[CadSensitivityCheck, ...] = ()
    repeatability_checks: tuple[CadRepeatabilityCheck, ...] = ()
    separation_checks: tuple[CadCandidateSeparationCheck, ...] = ()
    applicability_checks: tuple[CadApplicabilityCheck, ...] = ()

    recommendation_gate: Literal['disabled', 'eligible'] = 'disabled'
    gate_reasons: tuple[str, ...]
    algorithm_version: Literal['model-validation-2'] = VALIDATION_ALGORITHM_VERSION
    created_at_utc: str = Field(min_length=1)
    validation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_identity(self) -> 'CadModelValidationRecord':
        low_hz, high_hz = self.requested_band_hz
        if low_hz <= 0 or high_hz <= low_hz:
            raise ValueError('model validation frequency band is invalid')

        registration_fields = (
            self.campaign_registration_id,
            self.campaign_registration_sha256,
        )
        if self.evidence_scope == 'owned_room':
            if self.campaign_id is None or self.campaign_sha256 is None:
                raise ValueError('owned-room validation requires a preregistered campaign')
            if any(field is None for field in registration_fields):
                raise ValueError(
                    'owned-room validation requires a durable campaign registration'
                )
        else:
            if self.campaign_id is not None or self.campaign_sha256 is not None:
                raise ValueError('synthetic validation must not claim an owned-room campaign')
            if any(field is not None for field in registration_fields):
                raise ValueError(
                    'synthetic validation must not claim a campaign registration'
                )

        split_by_candidate: dict[str, str] = {}
        for pair in self.pairs:
            prior = split_by_candidate.setdefault(pair.candidate_id, pair.split)
            if prior != pair.split:
                raise ValueError('candidate cannot appear in both calibration and holdout splits')
        for sample in self.objective_samples:
            prior = split_by_candidate.setdefault(sample.candidate_id, sample.split)
            if prior != sample.split:
                raise ValueError('candidate cannot appear in both calibration and holdout splits')

        identities = {
            (pair.prediction_source_id, pair.measurement_id)
            for pair in self.pairs
        }
        if len(identities) != len(self.pairs):
            raise ValueError('validation prediction/measurement pairs must be unique')

        objective_identities = {
            (
                sample.candidate_id,
                sample.objective_id,
                sample.predicted_evaluation_id,
                sample.measured_evaluation_id,
            )
            for sample in self.objective_samples
        }
        if len(objective_identities) != len(self.objective_samples):
            raise ValueError('objective validation samples must be unique')

        trend_ids = [check.objective_id for check in self.trend_checks]
        if len(trend_ids) != len(set(trend_ids)):
            raise ValueError('trend checks must be unique by objective')
        sensitivity_keys = [
            (
                check.objective_id,
                tuple(sorted((check.candidate_a_id, check.candidate_b_id))),
            )
            for check in self.sensitivity_checks
        ]
        if len(sensitivity_keys) != len(set(sensitivity_keys)):
            raise ValueError('sensitivity checks must be unique by objective/candidate pair')
        repeatability_keys = [
            tuple(sorted(check.measurement_ids))
            for check in self.repeatability_checks
        ]
        if len(repeatability_keys) != len(set(repeatability_keys)):
            raise ValueError('repeatability groups must be unique by measurement set')
        separation_keys = [
            (
                tuple(sorted((check.candidate_a_id, check.candidate_b_id))),
                tuple(sorted((check.measurement_a_id, check.measurement_b_id))),
            )
            for check in self.separation_checks
        ]
        if len(separation_keys) != len(set(separation_keys)):
            raise ValueError('candidate separation checks must be unique')
        applicability_codes = [check.code for check in self.applicability_checks]
        if len(applicability_codes) != len(set(applicability_codes)):
            raise ValueError('model applicability check codes must be unique')

        for check in self.trend_checks:
            matching = tuple(
                sample for sample in self.objective_samples
                if sample.split == 'holdout' and sample.objective_id == check.objective_id
            )
            rebuilt = build_trend_checks(
                matching,
                tie_tolerance_by_objective={check.objective_id: check.tie_tolerance},
                min_comparable_pairs=check.min_comparable_pairs,
                min_agreement_ratio=check.min_agreement_ratio,
            )
            if len(rebuilt) != 1 or rebuilt[0] != check:
                raise ValueError('trend check does not match objective validation samples')

        if self.holdout_rms_db is None and self.residual_gate != 'insufficient':
            raise ValueError('missing holdout evidence requires residual_gate=insufficient')
        if self.holdout_rms_db is not None:
            expected = 'pass' if self.holdout_rms_db <= self.max_holdout_rms_db else 'fail'
            if self.residual_gate != expected:
                raise ValueError('residual gate does not match holdout RMS threshold')

        advanced_reasons = _advanced_gate_reasons(
            evidence_scope=self.evidence_scope,
            residual_gate=self.residual_gate,
            pairs=self.pairs,
            objective_samples=self.objective_samples,
            trend_checks=self.trend_checks,
            sensitivity_checks=self.sensitivity_checks,
            repeatability_checks=self.repeatability_checks,
            separation_checks=self.separation_checks,
            applicability_checks=self.applicability_checks,
        )
        expected_recommendation = 'eligible' if not advanced_reasons else 'disabled'
        if self.recommendation_gate != expected_recommendation:
            raise ValueError('recommendation gate does not match O60 evidence gates')
        if self.gate_reasons != advanced_reasons:
            raise ValueError('recommendation gate reasons do not match O60 evidence gates')

        if self.validation_sha256 != _hash(self.identity_payload()):
            raise ValueError('model validation identity hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'document_id': self.document_id,
            'search_spec_id': self.search_spec_id,
            'search_spec_sha256': self.search_spec_sha256,
            'candidate_set_sha256': self.candidate_set_sha256,
            'campaign_id': self.campaign_id,
            'campaign_sha256': self.campaign_sha256,
            'campaign_registration_id': self.campaign_registration_id,
            'campaign_registration_sha256': self.campaign_registration_sha256,
            'model_id': self.model_id,
            'model_version': self.model_version,
            'evidence_scope': self.evidence_scope,
            'requested_band_hz': list(self.requested_band_hz),
            'pairs': [pair.model_dump(mode='json') for pair in self.pairs],
            'holdout_rms_db': self.holdout_rms_db,
            'calibration_rms_db': self.calibration_rms_db,
            'max_holdout_rms_db': self.max_holdout_rms_db,
            'residual_gate': self.residual_gate,
            'objective_samples': [
                sample.model_dump(mode='json') for sample in self.objective_samples
            ],
            'trend_checks': [check.model_dump(mode='json') for check in self.trend_checks],
            'sensitivity_checks': [
                check.model_dump(mode='json') for check in self.sensitivity_checks
            ],
            'repeatability_checks': [
                check.model_dump(mode='json') for check in self.repeatability_checks
            ],
            'separation_checks': [
                check.model_dump(mode='json') for check in self.separation_checks
            ],
            'applicability_checks': [
                check.model_dump(mode='json') for check in self.applicability_checks
            ],
            'recommendation_gate': self.recommendation_gate,
            'gate_reasons': list(self.gate_reasons),
            'algorithm_version': self.algorithm_version,
        }


def _residual_payload(
    *,
    document_id: str,
    search_spec_id: str,
    search_spec_sha256: str,
    candidate_set_sha256: str,
    campaign_id: str | None,
    campaign_sha256: str | None,
    campaign_registration_id: str | None,
    campaign_registration_sha256: str | None,
    model_id: str,
    model_version: str,
    evidence_scope: EvidenceScope,
    samples: tuple[
        tuple[
            str,
            Literal['calibration', 'holdout'],
            str,
            str,
            FrequencyResponse,
            FrequencyResponse,
        ],
        ...,
    ],
    low_hz: float,
    high_hz: float,
    max_holdout_rms_db: float,
) -> dict[str, Any]:
    pairs: list[CadValidationPair] = []
    buckets: dict[str, list[float]] = {'calibration': [], 'holdout': []}

    for candidate_id, split, prediction_id, measurement_id, predicted, measured in samples:
        result = compare_frequency_responses(predicted, measured, low_hz, high_hz)
        if result.rms_difference_db is None:
            raise ValueError('validation pair has insufficient overlapping response data')
        pair = CadValidationPair(
            candidate_id=candidate_id,
            split=split,
            prediction_source_id=prediction_id,
            measurement_id=measurement_id,
            rms_difference_db=result.rms_difference_db,
            shape_rms_db=result.shape_rms_db,
        )
        pairs.append(pair)
        buckets[split].append(result.rms_difference_db)

    def aggregate_rms(values: list[float]) -> float | None:
        if not values:
            return None
        return (sum(value * value for value in values) / len(values)) ** 0.5

    calibration_rms = aggregate_rms(buckets['calibration'])
    holdout_rms = aggregate_rms(buckets['holdout'])
    if holdout_rms is None:
        residual_gate = 'insufficient'
    elif holdout_rms > max_holdout_rms_db:
        residual_gate = 'fail'
    else:
        residual_gate = 'pass'

    return {
        'document_id': document_id,
        'search_spec_id': search_spec_id,
        'search_spec_sha256': search_spec_sha256,
        'candidate_set_sha256': candidate_set_sha256,
        'campaign_id': campaign_id,
        'campaign_sha256': campaign_sha256,
        'campaign_registration_id': campaign_registration_id,
        'campaign_registration_sha256': campaign_registration_sha256,
        'model_id': model_id,
        'model_version': model_version,
        'evidence_scope': evidence_scope,
        'requested_band_hz': (float(low_hz), float(high_hz)),
        'pairs': tuple(pairs),
        'holdout_rms_db': holdout_rms,
        'calibration_rms_db': calibration_rms,
        'max_holdout_rms_db': float(max_holdout_rms_db),
        'residual_gate': residual_gate,
    }


def recompute_residual_payload(
    record: CadModelValidationRecord,
    samples: tuple[
        tuple[
            str,
            Literal['calibration', 'holdout'],
            str,
            str,
            FrequencyResponse,
            FrequencyResponse,
        ],
        ...,
    ],
) -> dict[str, Any]:
    """Rebuild a record's residual fields from exact evidence responses.

    This shares the canonical ``_residual_payload`` builder so persistence can
    require persisted pair residuals, aggregate RMS values, and the residual
    gate to equal the values derived from the referenced prediction and
    measurement evidence.
    """
    return _residual_payload(
        document_id=record.document_id,
        search_spec_id=record.search_spec_id,
        search_spec_sha256=record.search_spec_sha256,
        candidate_set_sha256=record.candidate_set_sha256,
        campaign_id=record.campaign_id,
        campaign_sha256=record.campaign_sha256,
        campaign_registration_id=record.campaign_registration_id,
        campaign_registration_sha256=record.campaign_registration_sha256,
        model_id=record.model_id,
        model_version=record.model_version,
        evidence_scope=record.evidence_scope,
        samples=samples,
        low_hz=record.requested_band_hz[0],
        high_hz=record.requested_band_hz[1],
        max_holdout_rms_db=record.max_holdout_rms_db,
    )


def _build_record(payload: dict[str, Any]) -> CadModelValidationRecord:
    reasons = _advanced_gate_reasons(
        evidence_scope=payload['evidence_scope'],
        residual_gate=payload['residual_gate'],
        pairs=payload['pairs'],
        objective_samples=payload.get('objective_samples', ()),
        trend_checks=payload.get('trend_checks', ()),
        sensitivity_checks=payload.get('sensitivity_checks', ()),
        repeatability_checks=payload.get('repeatability_checks', ()),
        separation_checks=payload.get('separation_checks', ()),
        applicability_checks=payload.get('applicability_checks', ()),
    )
    payload = {
        **payload,
        'recommendation_gate': 'eligible' if not reasons else 'disabled',
        'gate_reasons': reasons,
        'algorithm_version': VALIDATION_ALGORITHM_VERSION,
    }
    provisional = CadModelValidationRecord.model_construct(
        validation_id=str(uuid4()),
        created_at_utc=datetime.now(timezone.utc).isoformat(),
        validation_sha256='0' * 64,
        **payload,
    )
    return CadModelValidationRecord(
        **provisional.model_dump(exclude={'validation_sha256'}),
        validation_sha256=_hash(provisional.identity_payload()),
    )


def build_model_validation(
    *,
    document_id: str,
    search_spec_id: str,
    search_spec_sha256: str,
    candidate_set_sha256: str,
    model_id: str,
    model_version: str,
    samples: tuple[
        tuple[
            str,
            Literal['calibration', 'holdout'],
            str,
            str,
            FrequencyResponse,
            FrequencyResponse,
        ],
        ...,
    ],
    low_hz: float,
    high_hz: float,
    max_holdout_rms_db: float,
    evidence_scope: EvidenceScope = 'synthetic_fixture',
    campaign_id: str | None = None,
    campaign_sha256: str | None = None,
    campaign_registration_id: str | None = None,
    campaign_registration_sha256: str | None = None,
) -> CadModelValidationRecord:
    """Build the residual-only O60 baseline.

    It intentionally remains recommendation-disabled because trend, sensitivity,
    repeatability, separation and applicability evidence are not present.
    """

    return _build_record(_residual_payload(
        document_id=document_id,
        search_spec_id=search_spec_id,
        search_spec_sha256=search_spec_sha256,
        candidate_set_sha256=candidate_set_sha256,
        campaign_id=campaign_id,
        campaign_sha256=campaign_sha256,
        campaign_registration_id=campaign_registration_id,
        campaign_registration_sha256=campaign_registration_sha256,
        model_id=model_id,
        model_version=model_version,
        evidence_scope=evidence_scope,
        samples=samples,
        low_hz=low_hz,
        high_hz=high_hz,
        max_holdout_rms_db=max_holdout_rms_db,
    ))


def build_full_model_validation(
    *,
    document_id: str,
    search_spec_id: str,
    search_spec_sha256: str,
    candidate_set_sha256: str,
    model_id: str,
    model_version: str,
    response_samples: tuple[
        tuple[
            str,
            Literal['calibration', 'holdout'],
            str,
            str,
            FrequencyResponse,
            FrequencyResponse,
        ],
        ...,
    ],
    objective_samples: Sequence[CadObjectiveValidationSample],
    sensitivity_checks: Sequence[CadSensitivityCheck],
    repeatability_checks: Sequence[CadRepeatabilityCheck],
    separation_checks: Sequence[CadCandidateSeparationCheck],
    applicability_checks: Sequence[CadApplicabilityCheck],
    low_hz: float,
    high_hz: float,
    max_holdout_rms_db: float,
    evidence_scope: EvidenceScope,
    campaign_id: str | None = None,
    campaign_sha256: str | None = None,
    campaign_registration_id: str | None = None,
    campaign_registration_sha256: str | None = None,
    trend_tolerance_by_objective: Mapping[str, float] | None = None,
    trend_min_comparable_pairs: int = 1,
    trend_min_agreement_ratio: float = 0.75,
) -> CadModelValidationRecord:
    payload = _residual_payload(
        document_id=document_id,
        search_spec_id=search_spec_id,
        search_spec_sha256=search_spec_sha256,
        candidate_set_sha256=candidate_set_sha256,
        campaign_id=campaign_id,
        campaign_sha256=campaign_sha256,
        campaign_registration_id=campaign_registration_id,
        campaign_registration_sha256=campaign_registration_sha256,
        model_id=model_id,
        model_version=model_version,
        evidence_scope=evidence_scope,
        samples=response_samples,
        low_hz=low_hz,
        high_hz=high_hz,
        max_holdout_rms_db=max_holdout_rms_db,
    )
    ordered_samples = tuple(objective_samples)
    payload.update({
        'objective_samples': ordered_samples,
        'trend_checks': build_trend_checks(
            ordered_samples,
            tie_tolerance_by_objective=trend_tolerance_by_objective,
            min_comparable_pairs=trend_min_comparable_pairs,
            min_agreement_ratio=trend_min_agreement_ratio,
        ),
        'sensitivity_checks': tuple(sensitivity_checks),
        'repeatability_checks': tuple(repeatability_checks),
        'separation_checks': tuple(separation_checks),
        'applicability_checks': tuple(applicability_checks),
    })
    return _build_record(payload)
