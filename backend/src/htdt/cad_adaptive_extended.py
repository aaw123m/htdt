from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from typing import Any, Literal, Sequence
from uuid import uuid4

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_adaptive_planner import (
    AdaptiveExecutionScope,
    CadAdaptiveObjectiveEstimate,
    CadAdaptiveProposal,
    require_validation_scope,
)
from .cad_extended_search import (
    CadExtendedCandidate,
    CadExtendedSearchSpec,
)
from .cad_model_validation import CadModelValidationRecord
from .cad_search_models import CadSearchSpec
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _digest


ADAPTIVE_EXTENDED_SCHEMA_VERSION = 1
ADAPTIVE_EXTENDED_ALGORITHM_VERSION = 'adaptive-extended-residual-gp-1'
ExtendedObservationScope = Literal['synthetic_fixture', 'owned_room']
ExtendedFeatureSource = Literal['base_position', 'extended_parameter']
ExtendedFeatureCoordinate = Literal[
    'x',
    'y',
    'z',
    'aim_yaw_deg',
    'body_yaw_deg',
]






def adaptive_extended_timestamp_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


AdaptiveObservationSourceKind = Literal[
    'objective_evaluation',
    'synthetic_fixture',
]


class CadAdaptiveObservationSourceRef(BaseModel):
    """Typed exact source ref for one O80A observation value (#382).

    ``objective_evaluation`` names a persisted O30
    ``CadObjectiveEvaluation`` by id plus its exact ``evaluation_sha256``;
    the repository resolves it and requires the referenced metric to
    reproduce the observation's candidate/objective/unit/value and to carry
    an input ref of the required evidence class (``predicted`` for the
    prediction source, ``measured`` for the measurement source — the latter
    transitively binds the dataset/provenance authority O30 already
    replays).

    ``synthetic_fixture`` is a declared-only development ref: it never
    resolves to persisted production evidence and is rejected outside
    ``synthetic_fixture`` observation scope, so an arbitrary source string
    can no longer be labeled owned-room.
    """

    model_config = ConfigDict(frozen=True)

    kind: AdaptiveObservationSourceKind
    source_id: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


def build_observation_source_ref(
    *,
    kind: AdaptiveObservationSourceKind,
    source_id: str,
    source_sha256: str,
) -> CadAdaptiveObservationSourceRef:
    return CadAdaptiveObservationSourceRef(
        kind=kind,
        source_id=source_id,
        source_sha256=source_sha256,
    )


def synthetic_observation_source_ref(
    source_id: str,
    descriptor: Any,
) -> CadAdaptiveObservationSourceRef:
    """Declared-only development ref pinned to a deterministic descriptor."""

    return CadAdaptiveObservationSourceRef(
        kind='synthetic_fixture',
        source_id=source_id,
        source_sha256=_digest(descriptor),
    )


class CadAdaptiveExtendedObservation(BaseModel):
    """Immutable objective evidence for one exact extended candidate."""

    model_config = ConfigDict(frozen=True)

    observation_id: str = Field(min_length=1)
    extended_search_id: str = Field(min_length=1)
    extended_search_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_set_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_id: str = Field(min_length=1)
    evidence_scope: ExtendedObservationScope
    objective_id: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    predicted_value: float
    measured_value: float | None = None
    prediction_source: CadAdaptiveObservationSourceRef
    measurement_source: CadAdaptiveObservationSourceRef | None = None
    supersedes_observation_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    observation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    created_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_observation(self) -> 'CadAdaptiveExtendedObservation':
        if not isfinite(float(self.predicted_value)):
            raise ValueError('extended adaptive predicted value must be finite')
        if self.measured_value is not None and not isfinite(float(self.measured_value)):
            raise ValueError('extended adaptive measured value must be finite')
        if self.measured_value is None and self.measurement_source is not None:
            raise ValueError('predicted-only extended evidence must not claim measurement source')
        if self.measured_value is not None and self.measurement_source is None:
            raise ValueError('measured extended evidence requires measurement source')
        if self.observation_sha256 != _digest(self.identity_payload()):
            raise ValueError('extended adaptive observation identity hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'extended_search_id': self.extended_search_id,
            'extended_search_sha256': self.extended_search_sha256,
            'candidate_set_sha256': self.candidate_set_sha256,
            'candidate_id': self.candidate_id,
            'evidence_scope': self.evidence_scope,
            'objective_id': self.objective_id,
            'unit': self.unit,
            'predicted_value': self.predicted_value,
            'measured_value': self.measured_value,
            'prediction_source': self.prediction_source.model_dump(mode='json'),
            'measurement_source': (
                None
                if self.measurement_source is None
                else self.measurement_source.model_dump(mode='json')
            ),
            'supersedes_observation_sha256': self.supersedes_observation_sha256,
        }


def build_adaptive_extended_observation(
    *,
    extended_spec: CadExtendedSearchSpec,
    candidate_set_sha256: str,
    candidate_id: str,
    evidence_scope: ExtendedObservationScope,
    objective_id: str,
    unit: str,
    predicted_value: float,
    prediction_source: CadAdaptiveObservationSourceRef,
    measured_value: float | None = None,
    measurement_source: CadAdaptiveObservationSourceRef | None = None,
    supersedes_observation_sha256: str | None = None,
    created_at_utc: str | None = None,
) -> CadAdaptiveExtendedObservation:
    identity = {
        'extended_search_id': extended_spec.extended_search_id,
        'extended_search_sha256': extended_spec.extended_search_sha256,
        'candidate_set_sha256': candidate_set_sha256,
        'candidate_id': candidate_id,
        'evidence_scope': evidence_scope,
        'objective_id': objective_id,
        'unit': unit,
        'predicted_value': float(predicted_value),
        'measured_value': None if measured_value is None else float(measured_value),
        'prediction_source': prediction_source.model_dump(mode='json'),
        'measurement_source': (
            None
            if measurement_source is None
            else measurement_source.model_dump(mode='json')
        ),
        'supersedes_observation_sha256': supersedes_observation_sha256,
    }
    return CadAdaptiveExtendedObservation(
        observation_id=str(uuid4()),
        **identity,
        observation_sha256=_digest(identity),
        created_at_utc=created_at_utc or adaptive_extended_timestamp_utc(),
    )


class CadAdaptiveExtendedFeature(BaseModel):
    model_config = ConfigDict(frozen=True)

    feature_id: str = Field(min_length=1)
    source: ExtendedFeatureSource
    entity_id: str = Field(min_length=1)
    coordinate: ExtendedFeatureCoordinate
    unit: Literal['m', 'deg']
    min_value: float
    max_value: float
    scale: float = Field(gt=0.0)

    @model_validator(mode='after')
    def finite_values(self) -> 'CadAdaptiveExtendedFeature':
        if not all(
            isfinite(float(value))
            for value in (self.min_value, self.max_value, self.scale)
        ):
            raise ValueError('adaptive extended feature values must be finite')
        if self.max_value < self.min_value:
            raise ValueError('adaptive extended feature max must be >= min')
        return self


def build_adaptive_extended_features(
    base_spec: CadSearchSpec,
    extended_spec: CadExtendedSearchSpec,
) -> tuple[CadAdaptiveExtendedFeature, ...]:
    features: list[CadAdaptiveExtendedFeature] = []
    for axis in base_spec.axes:
        span = float(axis.max_m) - float(axis.min_m)
        features.append(CadAdaptiveExtendedFeature(
            feature_id=f'base:{axis.entity_id}:{axis.axis}',
            source='base_position',
            entity_id=axis.entity_id,
            coordinate=axis.axis,
            unit='m',
            min_value=float(axis.min_m),
            max_value=float(axis.max_m),
            scale=span if span > 1e-12 else 1.0,
        ))
    for axis in extended_spec.axes:
        span = float(axis.max_value) - float(axis.min_value)
        features.append(CadAdaptiveExtendedFeature(
            feature_id=f'extended:{axis.entity_id}:{axis.parameter}',
            source='extended_parameter',
            entity_id=axis.entity_id,
            coordinate=axis.parameter,
            unit='deg',
            min_value=float(axis.min_value),
            max_value=float(axis.max_value),
            scale=span if span > 1e-12 else 1.0,
        ))
    ids = [feature.feature_id for feature in features]
    if len(ids) != len(set(ids)):
        raise ValueError('adaptive extended feature IDs must be unique')
    if not features:
        raise ValueError('adaptive extended planning requires features')
    return tuple(features)


def _candidate_feature(
    candidate: CadExtendedCandidate,
    features: Sequence[CadAdaptiveExtendedFeature],
) -> np.ndarray:
    values: list[float] = []
    for feature in features:
        if feature.source == 'base_position':
            try:
                raw = candidate.positions[feature.entity_id][
                    f'{feature.coordinate}_m'
                ]
            except KeyError as exc:
                raise ValueError(
                    f'extended candidate misses base feature {feature.feature_id}'
                ) from exc
        elif feature.coordinate == 'aim_yaw_deg':
            try:
                raw = candidate.aim_yaw_deg[feature.entity_id]
            except KeyError as exc:
                raise ValueError(
                    f'extended candidate misses aim feature {feature.feature_id}'
                ) from exc
        else:
            try:
                raw = candidate.body_yaw_deg[feature.entity_id]
            except KeyError as exc:
                raise ValueError(
                    f'extended candidate misses body feature {feature.feature_id}'
                ) from exc
        normalized = (float(raw) - feature.min_value) / feature.scale
        values.append(normalized)
    return np.asarray(values, dtype=float)


def _gp_residual_estimate(
    train_x: np.ndarray,
    train_y: np.ndarray,
    query_x: np.ndarray,
    *,
    length_scale_normalized: float,
) -> tuple[float, float, float]:
    if train_x.ndim != 2 or query_x.ndim != 1:
        raise ValueError('adaptive extended GP feature shape is invalid')
    if train_x.shape[0] != train_y.shape[0] or not train_y.size:
        raise ValueError('adaptive extended GP training shape is invalid')
    scale = float(length_scale_normalized)
    if scale <= 0.0 or not isfinite(scale):
        raise ValueError('adaptive extended normalized length scale must be positive')

    scaled = train_x / scale
    query = query_x / scale
    pairwise = scaled[:, None, :] - scaled[None, :, :]
    kernel = np.exp(-0.5 * np.sum(pairwise * pairwise, axis=2))
    kernel = kernel + np.eye(kernel.shape[0], dtype=float) * 1e-8
    delta = scaled - query[None, :]
    k_star = np.exp(-0.5 * np.sum(delta * delta, axis=1))

    try:
        chol = np.linalg.cholesky(kernel)
        alpha = np.linalg.solve(chol.T, np.linalg.solve(chol, train_y))
        projection = np.linalg.solve(chol, k_star)
        variance = max(0.0, 1.0 - float(projection @ projection))
    except np.linalg.LinAlgError:
        inverse = np.linalg.pinv(kernel)
        alpha = inverse @ train_y
        variance = max(0.0, 1.0 - float(k_star @ inverse @ k_star))

    correction = float(k_star @ alpha)
    absolute = np.abs(train_y)
    residual_scale = max(
        float(np.std(train_y)) if train_y.size > 1 else 0.0,
        float(np.mean(absolute)),
        1e-6,
    )
    ratio = float(np.sqrt(variance))
    return correction, residual_scale * ratio, ratio


class CadAdaptiveExtendedPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = ADAPTIVE_EXTENDED_SCHEMA_VERSION
    plan_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    base_search_spec_id: str = Field(min_length=1)
    base_search_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    base_candidate_set_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    extended_search_id: str = Field(min_length=1)
    extended_search_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    extended_candidate_set_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    capability_id: str = Field(min_length=1)
    capability_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    validation_id: str = Field(min_length=1)
    validation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    execution_scope: AdaptiveExecutionScope
    source_evidence_scope: Literal['synthetic_fixture', 'owned_room']
    base_model_id: str = Field(min_length=1)
    base_model_version: str = Field(min_length=1)
    extended_model_id: str = Field(min_length=1)
    extended_model_version: str = Field(min_length=1)
    objective_ids: tuple[str, ...] = Field(min_length=1)
    features: tuple[CadAdaptiveExtendedFeature, ...] = Field(min_length=1)
    observation_sha256s: tuple[str, ...] = Field(min_length=1)
    training_candidate_ids: tuple[str, ...] = Field(min_length=1)
    excluded_measured_candidate_ids: tuple[str, ...] = Field(min_length=1)
    candidate_pool_count: int = Field(ge=1)
    length_scale_normalized: float = Field(gt=0.0)
    proposal_limit: int = Field(ge=1)
    seed: int = 0
    acquisition_function: Literal[
        'max_normalized_residual_uncertainty'
    ] = 'max_normalized_residual_uncertainty'
    algorithm_version: Literal[
        'adaptive-extended-residual-gp-1'
    ] = ADAPTIVE_EXTENDED_ALGORITHM_VERSION
    selected_candidate_id: str = Field(min_length=1)
    proposals: tuple[CadAdaptiveProposal, ...] = Field(min_length=1)
    adaptive_extended_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    created_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_plan(self) -> 'CadAdaptiveExtendedPlan':
        if self.execution_scope == 'development_synthetic':
            if self.source_evidence_scope != 'synthetic_fixture':
                raise ValueError('development extended adaptive plan must remain synthetic')
        elif self.source_evidence_scope != 'owned_room':
            raise ValueError('production extended adaptive plan must use owned-room evidence')
        for values, label in (
            (self.objective_ids, 'objective'),
            (tuple(item.feature_id for item in self.features), 'feature'),
            (self.observation_sha256s, 'observation'),
            (self.training_candidate_ids, 'training candidate'),
            (self.excluded_measured_candidate_ids, 'measured candidate'),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f'adaptive extended {label} values must be unique')
        proposal_ids = tuple(item.candidate_id for item in self.proposals)
        if len(proposal_ids) != len(set(proposal_ids)):
            raise ValueError('adaptive extended proposal IDs must be unique')
        if self.selected_candidate_id != proposal_ids[0]:
            raise ValueError('adaptive extended selected candidate must be first proposal')
        if set(proposal_ids) & set(self.excluded_measured_candidate_ids):
            raise ValueError('adaptive extended proposals must exclude measured candidates')
        if any(
            tuple(item.objective_id for item in proposal.objectives)
            != self.objective_ids
            for proposal in self.proposals
        ):
            raise ValueError('adaptive extended proposal objective ordering mismatch')
        if not isfinite(float(self.length_scale_normalized)):
            raise ValueError('adaptive extended length scale must be finite')
        if len(self.proposals) > self.proposal_limit:
            raise ValueError('adaptive extended proposals exceed proposal limit')
        if self.adaptive_extended_sha256 != _digest(self.identity_payload()):
            raise ValueError('adaptive extended plan identity hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'document_id': self.document_id,
            'base_search_spec_id': self.base_search_spec_id,
            'base_search_spec_sha256': self.base_search_spec_sha256,
            'base_candidate_set_sha256': self.base_candidate_set_sha256,
            'extended_search_id': self.extended_search_id,
            'extended_search_sha256': self.extended_search_sha256,
            'extended_candidate_set_sha256': self.extended_candidate_set_sha256,
            'capability_id': self.capability_id,
            'capability_sha256': self.capability_sha256,
            'validation_id': self.validation_id,
            'validation_sha256': self.validation_sha256,
            'execution_scope': self.execution_scope,
            'source_evidence_scope': self.source_evidence_scope,
            'base_model_id': self.base_model_id,
            'base_model_version': self.base_model_version,
            'extended_model_id': self.extended_model_id,
            'extended_model_version': self.extended_model_version,
            'objective_ids': list(self.objective_ids),
            'features': [item.model_dump(mode='json') for item in self.features],
            'observation_sha256s': list(self.observation_sha256s),
            'training_candidate_ids': list(self.training_candidate_ids),
            'excluded_measured_candidate_ids': list(
                self.excluded_measured_candidate_ids
            ),
            'candidate_pool_count': self.candidate_pool_count,
            'length_scale_normalized': self.length_scale_normalized,
            'proposal_limit': self.proposal_limit,
            'seed': self.seed,
            'acquisition_function': self.acquisition_function,
            'algorithm_version': self.algorithm_version,
            'selected_candidate_id': self.selected_candidate_id,
            'proposals': [
                proposal.model_dump(mode='json') for proposal in self.proposals
            ],
        }


def build_adaptive_extended_plan(
    *,
    base_spec: CadSearchSpec,
    base_candidate_set_sha256: str,
    extended_spec: CadExtendedSearchSpec,
    extended_candidate_set_sha256: str,
    capability_id: str,
    capability_sha256: str,
    extended_model_id: str,
    extended_model_version: str,
    validation: CadModelValidationRecord,
    candidates: Sequence[CadExtendedCandidate],
    observations: Sequence[CadAdaptiveExtendedObservation],
    execution_scope: AdaptiveExecutionScope,
    length_scale_normalized: float = 0.5,
    proposal_limit: int = 20,
) -> CadAdaptiveExtendedPlan:
    require_validation_scope(validation, execution_scope)
    if validation.document_id != base_spec.document_id:
        raise ValueError('extended adaptive validation belongs to another document')
    if (
        validation.search_spec_id != base_spec.search_spec_id
        or validation.search_spec_sha256 != base_spec.search_spec_sha256
        or validation.candidate_set_sha256 != base_candidate_set_sha256
    ):
        raise ValueError('extended adaptive base O60/SearchSpec authority mismatch')
    if (
        extended_spec.document_id != base_spec.document_id
        or extended_spec.base_search_spec_id != base_spec.search_spec_id
        or extended_spec.base_search_spec_sha256 != base_spec.search_spec_sha256
        or extended_spec.base_candidate_set_sha256 != base_candidate_set_sha256
    ):
        raise ValueError('extended adaptive Extended SearchSpec authority mismatch')
    if extended_spec.capability_id != capability_id:
        raise ValueError('extended adaptive capability ID mismatch')
    if extended_spec.capability_sha256 != capability_sha256:
        raise ValueError('extended adaptive capability hash mismatch')
    if length_scale_normalized <= 0.0 or not isfinite(float(length_scale_normalized)):
        raise ValueError('extended adaptive normalized length scale must be positive')
    if proposal_limit < 1:
        raise ValueError('extended adaptive proposal limit must be positive')

    candidate_by_id = {candidate.candidate_id: candidate for candidate in candidates}
    if len(candidate_by_id) != len(candidates):
        raise ValueError('extended adaptive candidate IDs must be unique')
    if not candidate_by_id:
        raise ValueError('extended adaptive candidate set is empty')

    for observation in observations:
        if (
            observation.extended_search_id != extended_spec.extended_search_id
            or observation.extended_search_sha256 != extended_spec.extended_search_sha256
            or observation.candidate_set_sha256 != extended_candidate_set_sha256
        ):
            raise ValueError('extended adaptive observation authority mismatch')
        if observation.candidate_id not in candidate_by_id:
            raise ValueError(
                f'extended adaptive observation candidate is outside set: '
                f'{observation.candidate_id}'
            )
        expected_scope = (
            'synthetic_fixture'
            if execution_scope == 'development_synthetic'
            else 'owned_room'
        )
        if observation.evidence_scope != expected_scope:
            raise ValueError('extended adaptive observation evidence scope mismatch')

    by_key: dict[tuple[str, str], CadAdaptiveExtendedObservation] = {}
    for observation in observations:
        key = (observation.candidate_id, observation.objective_id)
        if key in by_key:
            raise ValueError(
                'extended adaptive service must resolve observation supersession '
                f'before planning: {key[0]} / {key[1]}'
            )
        by_key[key] = observation
    objective_ids = tuple(sorted({item.objective_id for item in observations}))
    if not objective_ids:
        raise ValueError('extended adaptive planning requires objective evidence')

    units: dict[str, str] = {}
    for observation in observations:
        existing = units.setdefault(observation.objective_id, observation.unit)
        if existing != observation.unit:
            raise ValueError('extended adaptive objective units are inconsistent')

    measured_ids = tuple(
        candidate.candidate_id
        for candidate in candidates
        if any(
            (
                obs := by_key.get((candidate.candidate_id, objective_id))
            ) is not None
            and obs.measured_value is not None
            for objective_id in objective_ids
        )
    )
    training_ids = tuple(
        candidate.candidate_id
        for candidate in candidates
        if all(
            (
                obs := by_key.get((candidate.candidate_id, objective_id))
            ) is not None
            and obs.measured_value is not None
            for objective_id in objective_ids
        )
    )
    if not training_ids:
        raise ValueError('extended adaptive planning requires measured training candidates')

    features = build_adaptive_extended_features(base_spec, extended_spec)
    training_arrays: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for objective_id in objective_ids:
        train_x = np.vstack([
            _candidate_feature(candidate_by_id[candidate_id], features)
            for candidate_id in training_ids
        ])
        train_y = np.asarray([
            float(by_key[(candidate_id, objective_id)].measured_value)
            - float(by_key[(candidate_id, objective_id)].predicted_value)
            for candidate_id in training_ids
        ])
        training_arrays[objective_id] = (train_x, train_y)

    proposals: list[CadAdaptiveProposal] = []
    measured_set = set(measured_ids)
    used_observation_hashes: set[str] = set()
    for candidate in candidates:
        if candidate.candidate_id in measured_set:
            continue
        candidate_observations = [
            by_key.get((candidate.candidate_id, objective_id))
            for objective_id in objective_ids
        ]
        if any(item is None for item in candidate_observations):
            continue
        query_x = _candidate_feature(candidate, features)
        estimates: list[CadAdaptiveObjectiveEstimate] = []
        ratios: list[float] = []
        for objective_id, observation in zip(
            objective_ids,
            candidate_observations,
            strict=True,
        ):
            assert observation is not None
            train_x, train_y = training_arrays[objective_id]
            correction, uncertainty, ratio = _gp_residual_estimate(
                train_x,
                train_y,
                query_x,
                length_scale_normalized=float(length_scale_normalized),
            )
            estimates.append(CadAdaptiveObjectiveEstimate(
                objective_id=objective_id,
                unit=observation.unit,
                predicted_value=float(observation.predicted_value),
                corrected_mean=float(observation.predicted_value) + correction,
                residual_uncertainty=uncertainty,
                uncertainty_ratio=ratio,
                training_count=int(train_y.size),
            ))
            ratios.append(ratio)
            used_observation_hashes.add(observation.observation_sha256)
        proposals.append(CadAdaptiveProposal(
            candidate_id=candidate.candidate_id,
            acquisition_score=max(ratios),
            objectives=tuple(estimates),
        ))

    if not proposals:
        raise ValueError(
            'extended adaptive planning has no unmeasured candidate with '
            'complete predicted objective evidence'
        )

    for candidate_id in training_ids:
        for objective_id in objective_ids:
            used_observation_hashes.add(
                by_key[(candidate_id, objective_id)].observation_sha256
            )

    proposals.sort(key=lambda item: (-item.acquisition_score, item.candidate_id))
    candidate_pool_count = len(proposals)
    proposals = proposals[: min(int(proposal_limit), candidate_pool_count)]
    provisional = CadAdaptiveExtendedPlan.model_construct(
        plan_id=str(uuid4()),
        document_id=base_spec.document_id,
        base_search_spec_id=base_spec.search_spec_id,
        base_search_spec_sha256=base_spec.search_spec_sha256,
        base_candidate_set_sha256=base_candidate_set_sha256,
        extended_search_id=extended_spec.extended_search_id,
        extended_search_sha256=extended_spec.extended_search_sha256,
        extended_candidate_set_sha256=extended_candidate_set_sha256,
        capability_id=capability_id,
        capability_sha256=capability_sha256,
        validation_id=validation.validation_id,
        validation_sha256=validation.validation_sha256,
        execution_scope=execution_scope,
        source_evidence_scope=(
            'synthetic_fixture'
            if execution_scope == 'development_synthetic'
            else 'owned_room'
        ),
        base_model_id=validation.model_id,
        base_model_version=validation.model_version,
        extended_model_id=extended_model_id,
        extended_model_version=extended_model_version,
        objective_ids=objective_ids,
        features=features,
        observation_sha256s=tuple(sorted(used_observation_hashes)),
        training_candidate_ids=training_ids,
        excluded_measured_candidate_ids=measured_ids,
        candidate_pool_count=candidate_pool_count,
        length_scale_normalized=float(length_scale_normalized),
        proposal_limit=int(proposal_limit),
        seed=0,
        acquisition_function='max_normalized_residual_uncertainty',
        algorithm_version=ADAPTIVE_EXTENDED_ALGORITHM_VERSION,
        selected_candidate_id=proposals[0].candidate_id,
        proposals=tuple(proposals),
        adaptive_extended_sha256='0' * 64,
        created_at_utc=adaptive_extended_timestamp_utc(),
    )
    return CadAdaptiveExtendedPlan(
        **provisional.model_dump(exclude={'adaptive_extended_sha256'}),
        adaptive_extended_sha256=_digest(provisional.identity_payload()),
    )
