"""Acoustic performance target authority (#558).

HTDT already owns TargetCurveProfile direction for frequency-response/EQ
targets (#508) and :class:`~htdt.cad_standards.StandardsProfile` for traceable
compliance criteria (#170). Neither carries the user's *acoustic performance
intent* for time-domain/decay/spatial goals: desired EDT/T20/T30 by band,
early-energy intent, clarity targets, seat-to-seat consistency, or a
background-noise goal reference (#532).

:class:`AcousticPerformanceTargetProfile` is that missing immutable,
versioned, project-scoped authority. Contract properties:

- EDT/T20/T30 are distinct metric kinds, never interchangeable labels;
- every criterion pins an explicit frequency band set, a desired range/target
  (``CriterionRule``), aggregation/population semantics, a required
  capability token, source provenance, and whether it is a hard constraint,
  an optimization objective, or informational;
- three origins stay distinct: ``standard`` / ``research`` / ``user_goal`` —
  a research-population mean is not a compliance criterion, and an ISO 3382
  *measurement method* is never silently turned into a target number;
- a ProjectDesignBrief (#555) can reference the profile through its existing
  ``acoustic_performance_target`` goal kind, and Optimize/treatment
  workflows pin ``target_semantic_hash`` so changing the profile stales —
  never rewrites — bound results;
- evaluation reports per criterion an explicit evaluability state
  (``AVAILABLE``/``UNKNOWN``/``UNSUPPORTED``/``BLOCKED``) with reasons and a
  separate ``MET``/``UNMET``/``NOT_EVALUATED`` verdict; predicted and
  measured evidence stay distinct, and there is deliberately no overall
  room-quality score.
"""

from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_equipment import FrequencyDomain
from .cad_standards import (
    CriterionEvidenceRef,
    CriterionRule,
    CriterionSource,
    EvidenceBasis,
)


ACOUSTIC_TARGET_SCHEMA_VERSION = 1
ACOUSTIC_TARGET_AUTHORITY_VERSION = 'acoustic-performance-target-1'
ACOUSTIC_TARGET_EVALUATION_SCHEMA_VERSION = 1
ACOUSTIC_TARGET_EVALUATION_AUTHORITY_VERSION = 'acoustic-target-evaluation-1'
ACOUSTIC_TARGET_EVALUATOR_VERSION = 'acoustic-target-evaluator-1'


#: Metric kinds are intentionally fine-grained. ``decay_edt``/``decay_t20``/
#: ``decay_t30`` carry different estimator semantics and are never coalesced;
#: ``clarity_c50``/``clarity_c80`` are early/late energy ratios, not decay
#: times; ``seat_spread`` is a population spread across positions;
#: ``noise_criterion`` binds an exact background-noise criterion profile
#: (#532) rather than a generic "NC/NCB target".
AcousticTargetMetricKind = Literal[
    'decay_edt',
    'decay_t20',
    'decay_t30',
    'clarity_c50',
    'clarity_c80',
    'early_energy',
    'seat_spread',
    'noise_criterion',
    'custom',
]

#: Where the target value comes from. A ``research`` origin is published
#: preference/population data — never a compliance criterion.
TargetOrigin = Literal['standard', 'research', 'user_goal']

#: How a criterion participates downstream. A hard constraint can gate a
#: candidate only when explicitly selected there; objectives score; the
#: informational role records intent without gating.
CriterionRole = Literal['hard_constraint', 'objective', 'informational']

#: How per-position/per-band results aggregate before comparison. Every
#: criterion must declare one — there is no implicit mean.
PopulationAggregation = Literal[
    'per_position',
    'spatial_mean',
    'spatial_worst',
    'population_envelope',
    'single_listener',
]

#: Evaluability of one criterion under the declared capability/evidence set.
TargetEvaluability = Literal['AVAILABLE', 'UNKNOWN', 'UNSUPPORTED', 'BLOCKED']

#: Whether an observed result satisfies the target rule — separate from
#: evaluability so an evaluable criterion with no observation never
#: fabricates a verdict.
TargetVerdict = Literal['MET', 'UNMET', 'NOT_EVALUATED']


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


def _finite(value: object, *, field_name: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not isfinite(float(value))
    ):
        raise ValueError(f'{field_name} must be a finite number')
    return float(value)


class AcousticTargetBand(BaseModel):
    """One explicit frequency band a criterion is defined over."""

    model_config = ConfigDict(frozen=True)

    band_id: str = Field(min_length=1)
    frequency: FrequencyDomain


class AcousticTargetCriterion(BaseModel):
    """One versioned acoustic performance intent.

    ``metric`` pins the quantity and ``metric_version`` the exact definition
    or measurement-method authority (for example an ISO 3382-1:2009 method
    reference or a declared user method) — a measurement standard is never a
    source of the *target* itself; ``origin`` keeps standard / research /
    user-goal values distinct.
    """

    model_config = ConfigDict(frozen=True)

    @field_validator('bands', 'population_entity_ids')
    @classmethod
    def unique_ordered(cls, values: tuple) -> tuple:
        return tuple(values)

    criterion_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    metric: AcousticTargetMetricKind
    metric_version: str = Field(min_length=1)
    origin: TargetOrigin
    source: CriterionSource | None = None
    bands: tuple[AcousticTargetBand, ...] = Field(min_length=1)
    rule: CriterionRule
    unit: str = Field(min_length=1)
    aggregation: PopulationAggregation
    #: Seats/positions the criterion applies to; empty means the whole
    #: declared evaluation population.
    population_entity_ids: tuple[str, ...] = ()
    #: Capability token the evaluating provider must declare (for example
    #: ``predicted_t30`` or ``measured_edt``). A target existing does not
    #: mean the current provider can evaluate it.
    required_capability: str = Field(min_length=1)
    role: CriterionRole = 'informational'
    #: Whether predicted evidence, measured evidence, or both may satisfy
    #: the criterion. Default keeps the two bases explicit but either is
    #: acceptable where the metric semantics allow it.
    allowed_basis: Literal['predicted_or_measured', 'measured_only'] = (
        'predicted_or_measured'
    )
    note: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def valid_criterion(self) -> 'AcousticTargetCriterion':
        band_ids = [band.band_id for band in self.bands]
        if len(band_ids) != len(set(band_ids)):
            raise ValueError('acoustic target band ids must be unique')
        if len(set(self.population_entity_ids)) != len(self.population_entity_ids):
            raise ValueError('population entity ids must be unique')
        if self.origin != 'user_goal' and self.source is None:
            raise ValueError(
                'standard/research origins require an exact criterion source'
            )
        return self


class AcousticPerformanceTargetProfile(BaseModel):
    """Immutable versioned profile of non-FR acoustic performance goals.

    ``target_semantic_hash`` is the deterministic identity downstream
    workflows bind; changing the profile produces a different hash and a new
    ``profile_version`` — historical evaluations that pinned the old hash are
    stale, never rewritten.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = ACOUSTIC_TARGET_SCHEMA_VERSION
    authority_version: Literal['acoustic-performance-target-1'] = (
        ACOUSTIC_TARGET_AUTHORITY_VERSION
    )
    profile_id: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    name: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    criteria: tuple[AcousticTargetCriterion, ...]
    #: Optional pin of the background-noise authority a ``noise_criterion``
    #: criterion resolves against (#532); kept as a ref so the noise profile
    #: stays independent authority.
    noise_authority_ref: str | None = Field(default=None, min_length=1)
    supersedes_profile_id: str | None = Field(default=None, min_length=1)
    created_at_utc: str = Field(min_length=1)
    target_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_profile(self) -> 'AcousticPerformanceTargetProfile':
        criterion_ids = [item.criterion_id for item in self.criteria]
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError('acoustic target criterion ids must be unique')
        if not self.criteria:
            raise ValueError('acoustic target profile requires at least one criterion')
        if self.target_semantic_hash != _digest(self.semantic_payload()):
            raise ValueError('AcousticPerformanceTargetProfile semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'profile_id': self.profile_id,
            'profile_version': self.profile_version,
            'name': self.name,
            'document_id': self.document_id,
            'criteria': [item.model_dump(mode='json') for item in self.criteria],
            'noise_authority_ref': self.noise_authority_ref,
            'supersedes_profile_id': self.supersedes_profile_id,
            'created_at_utc': self.created_at_utc,
        }

    def criterion(self, criterion_id: str) -> AcousticTargetCriterion | None:
        return next(
            (item for item in self.criteria if item.criterion_id == criterion_id),
            None,
        )


def build_acoustic_target_profile(
    *,
    profile_id: str,
    profile_version: str,
    name: str,
    document_id: str,
    criteria: Sequence[AcousticTargetCriterion],
    created_at_utc: str,
    noise_authority_ref: str | None = None,
    supersedes_profile_id: str | None = None,
) -> AcousticPerformanceTargetProfile:
    payload: dict[str, Any] = {
        'schema_version': ACOUSTIC_TARGET_SCHEMA_VERSION,
        'authority_version': ACOUSTIC_TARGET_AUTHORITY_VERSION,
        'profile_id': profile_id,
        'profile_version': profile_version,
        'name': name,
        'document_id': document_id,
        'criteria': [item.model_dump(mode='json') for item in criteria],
        'noise_authority_ref': noise_authority_ref,
        'supersedes_profile_id': supersedes_profile_id,
        'created_at_utc': created_at_utc,
    }
    return AcousticPerformanceTargetProfile(
        profile_id=profile_id,
        profile_version=profile_version,
        name=name,
        document_id=document_id,
        criteria=tuple(criteria),
        noise_authority_ref=noise_authority_ref,
        supersedes_profile_id=supersedes_profile_id,
        created_at_utc=created_at_utc,
        target_semantic_hash=_digest(payload),
    )


class AcousticTargetObservation(BaseModel):
    """One observed/predicted value for one criterion band at one population.

    ``evidence_basis`` is required so magnitude-only or FR-only evidence can
    never masquerade as a time-domain metric; ``provided_capability`` names
    the exact provider capability that produced the value.
    """

    model_config = ConfigDict(frozen=True)

    criterion_id: str = Field(min_length=1)
    band_id: str = Field(min_length=1)
    observed_value: float
    unit: str = Field(min_length=1)
    evidence_basis: EvidenceBasis
    provided_capability: str = Field(min_length=1)
    entity_ids: tuple[str, ...] = ()
    evidence_refs: tuple[CriterionEvidenceRef, ...] = ()

    @field_validator('observed_value')
    @classmethod
    def finite_value(cls, value: float) -> float:
        return _finite(value, field_name='observed value')

    @model_validator(mode='after')
    def valid_observation(self) -> 'AcousticTargetObservation':
        if len(set(self.entity_ids)) != len(self.entity_ids):
            raise ValueError('observation entity ids must be unique')
        return self


class AcousticTargetBandResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    band_id: str = Field(min_length=1)
    observed_value: float | None = None
    verdict: TargetVerdict
    basis: EvidenceBasis | None = None


class AcousticTargetCriterionResult(BaseModel):
    """Evaluability plus verdict for one criterion — no score, ever."""

    model_config = ConfigDict(frozen=True)

    criterion_id: str = Field(min_length=1)
    criterion_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    evaluability: TargetEvaluability
    verdict: TargetVerdict
    reason: str = Field(min_length=1)
    band_results: tuple[AcousticTargetBandResult, ...] = ()
    role: CriterionRole


class AcousticTargetEvaluation(BaseModel):
    """Immutable per-criterion evaluation bound to an exact profile hash."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = ACOUSTIC_TARGET_EVALUATION_SCHEMA_VERSION
    authority_version: Literal['acoustic-target-evaluation-1'] = (
        ACOUSTIC_TARGET_EVALUATION_AUTHORITY_VERSION
    )
    evaluator_version: Literal['acoustic-target-evaluator-1'] = (
        ACOUSTIC_TARGET_EVALUATOR_VERSION
    )
    evaluation_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    target_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    results: tuple[AcousticTargetCriterionResult, ...]
    created_at_utc: str = Field(min_length=1)
    evaluation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_evaluation(self) -> 'AcousticTargetEvaluation':
        ids = [item.criterion_id for item in self.results]
        if len(ids) != len(set(ids)):
            raise ValueError('acoustic target evaluation result ids must be unique')
        if self.evaluation_sha256 != _digest(self.semantic_payload()):
            raise ValueError('AcousticTargetEvaluation semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'evaluator_version': self.evaluator_version,
            'evaluation_id': self.evaluation_id,
            'profile_id': self.profile_id,
            'profile_version': self.profile_version,
            'target_semantic_hash': self.target_semantic_hash,
            'results': [item.model_dump(mode='json') for item in self.results],
            'created_at_utc': self.created_at_utc,
        }

    def is_bound_to(self, profile: AcousticPerformanceTargetProfile) -> bool:
        return self.target_semantic_hash == profile.target_semantic_hash


def _rule_met(value: float, rule: CriterionRule) -> bool:
    from .cad_standards import _compare  # shared exact comparison semantics

    return _compare(value, rule)


def evaluate_acoustic_targets(
    *,
    profile: AcousticPerformanceTargetProfile,
    observations: Sequence[AcousticTargetObservation],
    available_capabilities: Sequence[str],
    created_at_utc: str,
) -> AcousticTargetEvaluation:
    """Fail-closed per-criterion evaluation.

    A criterion is ``UNSUPPORTED`` when no provider declares its required
    capability, ``BLOCKED`` when a provider capability exists but the only
    observations carry a basis the criterion does not allow, ``UNKNOWN``
    when evaluable in principle but band observations are missing, and
    ``AVAILABLE`` with a ``MET``/``UNMET`` verdict once every declared band
    has an allowed observation. No overall score is produced.
    """

    capabilities = set(available_capabilities)
    by_key: dict[tuple[str, str], list[AcousticTargetObservation]] = {}
    for observation in observations:
        by_key.setdefault((observation.criterion_id, observation.band_id), []).append(
            observation
        )

    results: list[AcousticTargetCriterionResult] = []
    for criterion in profile.criteria:
        criterion_hash = _digest(criterion.model_dump(mode='json'))
        if criterion.required_capability not in capabilities:
            results.append(AcousticTargetCriterionResult(
                criterion_id=criterion.criterion_id,
                criterion_sha256=criterion_hash,
                evaluability='UNSUPPORTED',
                verdict='NOT_EVALUATED',
                reason=(
                    f'no provider declares required capability '
                    f'{criterion.required_capability!r}'
                ),
                role=criterion.role,
            ))
            continue

        band_results: list[AcousticTargetBandResult] = []
        blocked = False
        missing = False
        for band in criterion.bands:
            candidates = by_key.get((criterion.criterion_id, band.band_id), [])
            allowed = [
                item
                for item in candidates
                if criterion.allowed_basis != 'measured_only'
                or item.evidence_basis == 'measured'
            ]
            if candidates and not allowed:
                blocked = True
                band_results.append(AcousticTargetBandResult(
                    band_id=band.band_id,
                    verdict='NOT_EVALUATED',
                ))
                continue
            if not allowed:
                missing = True
                band_results.append(AcousticTargetBandResult(
                    band_id=band.band_id,
                    verdict='NOT_EVALUATED',
                ))
                continue
            # Deterministic aggregation: the first sorted-by-evidence-id
            # observation is the canonical candidate; callers keep per-
            # position values as separate observation entity_ids.
            observation = sorted(
                allowed,
                key=lambda item: (
                    item.evidence_basis,
                    tuple(sorted(item.entity_ids)),
                    tuple((ref.kind, ref.evidence_id) for ref in item.evidence_refs),
                ),
            )[0]
            band_results.append(AcousticTargetBandResult(
                band_id=band.band_id,
                observed_value=observation.observed_value,
                verdict=(
                    'MET'
                    if _rule_met(observation.observed_value, criterion.rule)
                    else 'UNMET'
                ),
                basis=observation.evidence_basis,
            ))

        if blocked:
            evaluability: TargetEvaluability = 'BLOCKED'
            reason = (
                'only observations carry an evidence basis the criterion '
                'does not allow'
            )
            verdict: TargetVerdict = 'NOT_EVALUATED'
        elif missing:
            evaluability = 'UNKNOWN'
            reason = 'band observations are missing for the declared band set'
            verdict = 'NOT_EVALUATED'
        else:
            evaluability = 'AVAILABLE'
            verdict = (
                'MET'
                if all(item.verdict == 'MET' for item in band_results)
                else 'UNMET'
            )
            reason = 'all declared bands evaluated'
        results.append(AcousticTargetCriterionResult(
            criterion_id=criterion.criterion_id,
            criterion_sha256=criterion_hash,
            evaluability=evaluability,
            verdict=verdict,
            reason=reason,
            band_results=tuple(band_results),
            role=criterion.role,
        ))

    identity: dict[str, Any] = {
        'schema_version': ACOUSTIC_TARGET_EVALUATION_SCHEMA_VERSION,
        'authority_version': ACOUSTIC_TARGET_EVALUATION_AUTHORITY_VERSION,
        'evaluator_version': ACOUSTIC_TARGET_EVALUATOR_VERSION,
        'profile_id': profile.profile_id,
        'profile_version': profile.profile_version,
        'target_semantic_hash': profile.target_semantic_hash,
        'results': [item.model_dump(mode='json') for item in results],
        'created_at_utc': created_at_utc,
    }
    evaluation_id = f'acoustic-target-eval-{_digest(identity)[:32]}'
    payload = {**identity, 'evaluation_id': evaluation_id}
    digest = _digest(payload)
    return AcousticTargetEvaluation(
        evaluation_id=evaluation_id,
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        target_semantic_hash=profile.target_semantic_hash,
        results=tuple(results),
        created_at_utc=created_at_utc,
        evaluation_sha256=digest,
    )


__all__ = [
    'ACOUSTIC_TARGET_AUTHORITY_VERSION',
    'ACOUSTIC_TARGET_EVALUATION_AUTHORITY_VERSION',
    'ACOUSTIC_TARGET_EVALUATION_SCHEMA_VERSION',
    'ACOUSTIC_TARGET_EVALUATOR_VERSION',
    'ACOUSTIC_TARGET_SCHEMA_VERSION',
    'AcousticPerformanceTargetProfile',
    'AcousticTargetBand',
    'AcousticTargetBandResult',
    'AcousticTargetCriterion',
    'AcousticTargetCriterionResult',
    'AcousticTargetEvaluation',
    'AcousticTargetMetricKind',
    'AcousticTargetObservation',
    'CriterionRole',
    'PopulationAggregation',
    'TargetEvaluability',
    'TargetOrigin',
    'TargetVerdict',
    'build_acoustic_target_profile',
    'evaluate_acoustic_targets',
]
