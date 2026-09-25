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
  measured evidence stay distinct — evaluated as separate basis records,
  never collapsed by observation ordering — and there is deliberately no
  overall room-quality score;
- the criterion's declared ``aggregation`` and ``population_entity_ids``
  govern how band evidence folds into a verdict: ``spatial_mean``,
  ``spatial_worst``, ``per_position`` and ``single_listener`` aggregate
  per-population-member evidence, while ``population_envelope`` is
  explicitly ``UNSUPPORTED`` until its semantics are defined;
- observation eligibility is enforced: ``provided_capability`` must equal
  the criterion's ``required_capability``, and the observation unit must
  match the criterion unit or convert through the shared units authority
  (conversion provenance is retained); missing population coverage, a
  capability mismatch, or incompatible units leave the criterion
  ``UNKNOWN`` — never a substitute for "compliant".
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
from .cad_units import convert_unit, units_convertible


ACOUSTIC_TARGET_SCHEMA_VERSION = 1
ACOUSTIC_TARGET_AUTHORITY_VERSION = 'acoustic-performance-target-1'
ACOUSTIC_TARGET_EVALUATION_SCHEMA_VERSION = 1
ACOUSTIC_TARGET_EVALUATION_AUTHORITY_VERSION = 'acoustic-target-evaluation-1'
ACOUSTIC_TARGET_EVALUATOR_VERSION = 'acoustic-target-evaluator-2'


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


class AcousticTargetMemberResult(BaseModel):
    """Per-population-member evidence inside one band/basis evaluation.

    ``observed_value`` is always expressed in the criterion unit; the raw
    ``source_value``/``source_unit`` plus ``converted`` keep conversion
    provenance intact. A member is ``missing`` when no observation binds
    to it and ``ambiguous`` when conflicting observations do — both
    prevent the band from being decided.
    """

    model_config = ConfigDict(frozen=True)

    entity_id: str = Field(min_length=1)
    status: Literal['evaluated', 'missing', 'ambiguous']
    observed_value: float | None = None
    source_value: float | None = None
    source_unit: str | None = Field(default=None, min_length=1)
    converted: bool = False
    verdict: TargetVerdict = 'NOT_EVALUATED'
    provided_capability: str | None = Field(default=None, min_length=1)
    observation_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    evidence_refs: tuple[CriterionEvidenceRef, ...] = ()


class AcousticTargetBandResult(BaseModel):
    """Per-band outcome inside a criterion verdict.

    ``observed_value`` is the aggregate value in the criterion unit used
    for the comparison, ``basis`` records whether it came from predicted
    or measured evidence, and ``member_results`` keeps per-population-
    member evidence instead of dropping it. Predicted and measured
    evidence are evaluated as separate basis records — a basis whose
    record is ``NOT_EVALUATED`` (incomplete or ambiguous population
    coverage) prevents the criterion verdict from being MET.
    """

    model_config = ConfigDict(frozen=True)

    band_id: str = Field(min_length=1)
    observed_value: float | None = None
    verdict: TargetVerdict
    basis: EvidenceBasis | None = None
    aggregation: PopulationAggregation | None = None
    limiting_entity_id: str | None = Field(default=None, min_length=1)
    member_results: tuple[AcousticTargetMemberResult, ...] = ()
    reason: str | None = Field(default=None, min_length=1)


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
    evaluator_version: Literal['acoustic-target-evaluator-2'] = (
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


def _observation_digest(observation: AcousticTargetObservation) -> str:
    return _digest(observation.model_dump(mode='json'))


def _convert_observation_value(
    observation: AcousticTargetObservation,
    criterion: AcousticTargetCriterion,
) -> tuple[float, bool] | None:
    """Return ``(value_in_criterion_unit, converted)`` or ``None``.

    An exact unit match needs no conversion; a known conversion is applied
    and flagged; incompatible units make the observation ineligible rather
    than silently comparing raw numbers.
    """
    if observation.unit == criterion.unit:
        return observation.observed_value, False
    if not units_convertible(observation.unit, criterion.unit):
        return None
    return (
        convert_unit(
            observation.observed_value,
            observation.unit,
            criterion.unit,
        ),
        True,
    )


def _rule_worst_score(value: float, rule: CriterionRule) -> float:
    """Adversity score for spatial-worst selection — larger is worse."""
    if rule.operator == 'min':
        return float(rule.minimum) - value
    if rule.operator == 'max':
        return value - float(rule.maximum)
    if rule.operator == 'range':
        return max(
            float(rule.minimum) - value,
            value - float(rule.maximum),
        )
    # equals: binary — any mismatch is equally adverse, a match is not
    return 0.0 if _rule_met(value, rule) else 1.0


def _member_statuses(
    criterion: AcousticTargetCriterion,
    obs_items: Sequence[
        tuple[AcousticTargetObservation, tuple[float, bool]]
    ],
) -> list[tuple[str, list[tuple[AcousticTargetObservation, tuple[float, bool]]]]]:
    """Map population members to their candidate observations.

    Returns ``(member_id, obs_list)`` pairs. A declared
    ``population_entity_ids`` fixes the member order and makes an
    observation with no usable entity binding ineligible for coverage; an
    undeclared population is the union of observed entity ids plus one
    anonymous member per distinct unattributed observation. An
    observation covering multiple entities counts for each of them.
    """
    declared = list(criterion.population_entity_ids)
    unattributed: list[
        tuple[AcousticTargetObservation, tuple[float, bool]]
    ] = []
    member_map: dict[
        str, list[tuple[AcousticTargetObservation, tuple[float, bool]]]
    ] = {}
    for observation, converted in obs_items:
        if not observation.entity_ids:
            unattributed.append((observation, converted))
            continue
        for entity_id in observation.entity_ids:
            member_map.setdefault(entity_id, []).append(
                (observation, converted)
            )
    if declared:
        return [
            (entity_id, member_map.get(entity_id, []))
            for entity_id in declared
        ]
    members = [
        (entity_id, member_map[entity_id])
        for entity_id in sorted(member_map)
    ]
    # No declared population to bind to: each distinct unattributed
    # observation is one anonymous member — never silently reassigned.
    anonymous: dict[str, int] = {}
    for observation, converted in unattributed:
        digest = _observation_digest(observation)
        index = anonymous.setdefault(digest, len(anonymous))
        members.append((f'(unattributed:{index})', [(observation, converted)]))
    return members


def _evaluate_band_basis(
    criterion: AcousticTargetCriterion,
    band: AcousticTargetBand,
    basis: EvidenceBasis,
    obs_items: Sequence[
        tuple[AcousticTargetObservation, tuple[float, bool]]
    ],
) -> AcousticTargetBandResult:
    """Aggregate one band under one evidence basis.

    Any member that is missing or ambiguous makes the band
    ``NOT_EVALUATED`` — its member records still carry their evidence.
    """
    member_results: list[AcousticTargetMemberResult] = []
    evaluated: list[tuple[str, float, AcousticTargetMemberResult]] = []
    for member_id, candidates in _member_statuses(criterion, obs_items):
        distinct = {
            _observation_digest(observation)
            for observation, _converted in candidates
        }
        if not candidates:
            member_results.append(AcousticTargetMemberResult(
                entity_id=member_id,
                status='missing',
            ))
            continue
        if len(distinct) > 1:
            member_results.append(AcousticTargetMemberResult(
                entity_id=member_id,
                status='ambiguous',
            ))
            continue
        observation, (value, converted) = sorted(
            candidates,
            key=lambda item: _observation_digest(item[0]),
        )[0]
        member = AcousticTargetMemberResult(
            entity_id=member_id,
            status='evaluated',
            observed_value=value,
            source_value=observation.observed_value,
            source_unit=observation.unit,
            converted=converted,
            verdict=(
                'MET'
                if _rule_met(value, criterion.rule)
                else 'UNMET'
            ),
            provided_capability=observation.provided_capability,
            observation_sha256=_observation_digest(observation),
            evidence_refs=observation.evidence_refs,
        )
        member_results.append(member)
        evaluated.append((member_id, value, member))

    if criterion.aggregation == 'single_listener':
        if len(member_results) != 1 or not evaluated:
            return AcousticTargetBandResult(
                band_id=band.band_id,
                verdict='NOT_EVALUATED',
                basis=basis,
                aggregation=criterion.aggregation,
                member_results=tuple(member_results),
                reason='single_listener requires exactly one evaluated member',
            )
        member_id, value, member = evaluated[0]
        return AcousticTargetBandResult(
            band_id=band.band_id,
            observed_value=value,
            verdict=member.verdict,
            basis=basis,
            aggregation=criterion.aggregation,
            limiting_entity_id=member_id,
            member_results=tuple(member_results),
        )

    if len(evaluated) != len(member_results):
        return AcousticTargetBandResult(
            band_id=band.band_id,
            verdict='NOT_EVALUATED',
            basis=basis,
            aggregation=criterion.aggregation,
            member_results=tuple(member_results),
            reason='declared population coverage is incomplete or ambiguous',
        )
    if not evaluated:
        return AcousticTargetBandResult(
            band_id=band.band_id,
            verdict='NOT_EVALUATED',
            basis=basis,
            aggregation=criterion.aggregation,
            reason='no members in the evaluated population',
        )

    worst = max(
        evaluated,
        key=lambda item: (
            _rule_worst_score(item[1], criterion.rule),
            item[0],
        ),
    )
    if criterion.aggregation == 'spatial_mean':
        value = sum(item[1] for item in evaluated) / len(evaluated)
        return AcousticTargetBandResult(
            band_id=band.band_id,
            observed_value=value,
            verdict=(
                'MET' if _rule_met(value, criterion.rule) else 'UNMET'
            ),
            basis=basis,
            aggregation=criterion.aggregation,
            limiting_entity_id=worst[0],
            member_results=tuple(member_results),
        )
    if criterion.aggregation == 'spatial_worst':
        return AcousticTargetBandResult(
            band_id=band.band_id,
            observed_value=worst[1],
            verdict=worst[2].verdict,
            basis=basis,
            aggregation=criterion.aggregation,
            limiting_entity_id=worst[0],
            member_results=tuple(member_results),
        )
    # per_position: every evaluated member must satisfy the rule
    verdict: TargetVerdict = (
        'MET'
        if all(item[2].verdict == 'MET' for item in evaluated)
        else 'UNMET'
    )
    return AcousticTargetBandResult(
        band_id=band.band_id,
        verdict=verdict,
        basis=basis,
        aggregation=criterion.aggregation,
        limiting_entity_id=worst[0],
        member_results=tuple(member_results),
    )


def evaluate_acoustic_targets(
    *,
    profile: AcousticPerformanceTargetProfile,
    observations: Sequence[AcousticTargetObservation],
    available_capabilities: Sequence[str],
    created_at_utc: str,
) -> AcousticTargetEvaluation:
    """Fail-closed per-criterion evaluation.

    A criterion is ``UNSUPPORTED`` when no provider declares its required
    capability (or when its aggregation semantics are not defined by the
    target schema), ``BLOCKED`` when a provider capability exists but every
    band's observations carry a basis the criterion does not allow,
    ``UNKNOWN`` when evaluable in principle but band observations are
    missing, carry the wrong provided capability/units, or do not cover the
    declared population, and ``AVAILABLE`` with a ``MET``/``UNMET`` verdict
    once every declared band aggregates to a verdict for every allowed
    evidence basis that carries eligible observations. Predicted and
    measured evidence never collapse into one record; per-member evidence
    (with observation identity and unit-conversion provenance) is retained
    and bound into the evaluation hash. No overall score is produced.
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
        if criterion.aggregation == 'population_envelope':
            results.append(AcousticTargetCriterionResult(
                criterion_id=criterion.criterion_id,
                criterion_sha256=criterion_hash,
                evaluability='UNSUPPORTED',
                verdict='NOT_EVALUATED',
                reason=(
                    'population_envelope aggregation semantics are not '
                    'defined by the target schema'
                ),
                role=criterion.role,
            ))
            continue

        band_results: list[AcousticTargetBandResult] = []
        blocked = False
        all_disallowed = True
        unknown_reason: str | None = None
        for band in criterion.bands:
            candidates = by_key.get(
                (criterion.criterion_id, band.band_id), []
            )
            allowed = [
                item
                for item in candidates
                if criterion.allowed_basis != 'measured_only'
                or item.evidence_basis == 'measured'
            ]
            if allowed:
                all_disallowed = False
            elif candidates:
                blocked = True
                band_results.append(AcousticTargetBandResult(
                    band_id=band.band_id,
                    verdict='NOT_EVALUATED',
                    reason=(
                        'band observations carry an evidence basis the '
                        'criterion does not allow'
                    ),
                ))
                continue
            else:
                if unknown_reason is None:
                    unknown_reason = (
                        'no observations exist for the declared band'
                    )
                band_results.append(AcousticTargetBandResult(
                    band_id=band.band_id,
                    verdict='NOT_EVALUATED',
                    reason='no observations exist for the declared band',
                ))
                continue

            # Eligibility: the observation's declared provider capability
            # must match the required capability, and its unit must match
            # or convert into the criterion unit.
            eligible: list[
                tuple[AcousticTargetObservation, tuple[float, bool]]
            ] = []
            saw_capability_mismatch = False
            saw_unit_mismatch = False
            for item in allowed:
                if item.provided_capability != criterion.required_capability:
                    saw_capability_mismatch = True
                    continue
                converted = _convert_observation_value(item, criterion)
                if converted is None:
                    saw_unit_mismatch = True
                    continue
                eligible.append((item, converted))
            if not eligible:
                if unknown_reason is None:
                    if saw_capability_mismatch:
                        unknown_reason = (
                            'no observation declares the required '
                            f'capability {criterion.required_capability!r}'
                        )
                    elif saw_unit_mismatch:
                        unknown_reason = (
                            'observation units do not match or convert to '
                            'the criterion unit'
                        )
                band_results.append(AcousticTargetBandResult(
                    band_id=band.band_id,
                    verdict='NOT_EVALUATED',
                    reason=(
                        'observation declares a different provider '
                        'capability'
                        if saw_capability_mismatch
                        else 'observation unit does not match or convert to '
                        'the criterion unit'
                    ),
                ))
                continue

            # Per-basis evaluation: predicted and measured evidence never
            # collapse into one record via ordering.
            by_basis: dict[
                EvidenceBasis,
                list[tuple[AcousticTargetObservation, tuple[float, bool]]],
            ] = {}
            for item in eligible:
                by_basis.setdefault(item[0].evidence_basis, []).append(item)
            for basis in sorted(by_basis):
                result = _evaluate_band_basis(
                    criterion, band, basis, by_basis[basis]
                )
                if (
                    result.verdict == 'NOT_EVALUATED'
                    and unknown_reason is None
                    and result.reason is not None
                ):
                    unknown_reason = result.reason
                band_results.append(result)

        if blocked and all_disallowed:
            evaluability: TargetEvaluability = 'BLOCKED'
            reason = (
                'observations exist only under an evidence basis the '
                'criterion does not allow'
            )
            verdict: TargetVerdict = 'NOT_EVALUATED'
        elif any(
            item.verdict == 'NOT_EVALUATED' for item in band_results
        ):
            evaluability = 'UNKNOWN'
            reason = (
                unknown_reason
                if unknown_reason is not None
                else 'band observations cannot decide the declared band set'
            )
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
    'AcousticTargetMemberResult',
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
