from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from typing import Any, Callable, Literal, Sequence
from uuid import uuid4

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_model_validation import CadModelValidationRecord
from .cad_objective_models import CadObjectiveEvaluation
from .cad_search_models import CadCandidate, CadSearchSpec
from .canonical_json import canonical_json as _canonical, canonical_sha256 as _digest


ADAPTIVE_SCHEMA_VERSION = 1
ADAPTIVE_ALGORITHM_VERSION = 'adaptive-residual-gp-1'
AdaptiveExecutionScope = Literal['development_synthetic', 'production_owned_room']






def adaptive_timestamp_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def development_validation_ready(record: CadModelValidationRecord) -> bool:
    return (
        record.evidence_scope == 'synthetic_fixture'
        and record.recommendation_gate == 'disabled'
        and record.gate_reasons == (
            'automatic recommendation requires owned-room evidence',
        )
    )


def production_validation_ready(record: CadModelValidationRecord) -> bool:
    return (
        record.evidence_scope == 'owned_room'
        and record.recommendation_gate == 'eligible'
        and not record.gate_reasons
        and record.campaign_id is not None
        and record.campaign_sha256 is not None
    )


def require_validation_scope(
    record: CadModelValidationRecord,
    execution_scope: AdaptiveExecutionScope,
) -> None:
    if execution_scope == 'development_synthetic':
        if not development_validation_ready(record):
            raise ValueError(
                'development adaptive planning requires a fully-passing synthetic O60 '
                'record whose only stop reason is owned-room evidence'
            )
        return
    if not production_validation_ready(record):
        raise ValueError(
            'production adaptive planning requires a campaign-backed owned-room '
            'eligible O60 ValidationRecord'
        )


class CadAdaptiveObjectiveEstimate(BaseModel):
    model_config = ConfigDict(frozen=True)

    objective_id: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    predicted_value: float
    corrected_mean: float
    residual_uncertainty: float = Field(ge=0.0)
    uncertainty_ratio: float = Field(ge=0.0)
    training_count: int = Field(ge=1)

    @model_validator(mode='after')
    def finite_values(self) -> 'CadAdaptiveObjectiveEstimate':
        values = (
            self.predicted_value,
            self.corrected_mean,
            self.residual_uncertainty,
            self.uncertainty_ratio,
        )
        if not all(isfinite(float(value)) for value in values):
            raise ValueError('adaptive objective estimate values must be finite')
        return self


class CadAdaptiveProposal(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate_id: str = Field(min_length=1)
    acquisition_score: float = Field(ge=0.0)
    objectives: tuple[CadAdaptiveObjectiveEstimate, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def unique_objectives(self) -> 'CadAdaptiveProposal':
        ids = [item.objective_id for item in self.objectives]
        if len(ids) != len(set(ids)):
            raise ValueError('adaptive proposal objective ids must be unique')
        if not isfinite(float(self.acquisition_score)):
            raise ValueError('adaptive acquisition score must be finite')
        return self


class CadAdaptiveEvaluationRef(BaseModel):
    """Identity binding to one exact predicted O30 evaluation consumed by O70."""

    model_config = ConfigDict(frozen=True)

    evaluation_id: str = Field(min_length=1)
    evaluation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_id: str = Field(min_length=1)


class CadAdaptivePlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = ADAPTIVE_SCHEMA_VERSION
    plan_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    search_spec_id: str = Field(min_length=1)
    search_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_set_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    validation_id: str = Field(min_length=1)
    validation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    execution_scope: AdaptiveExecutionScope
    source_evidence_scope: Literal['synthetic_fixture', 'owned_room']
    validation_recommendation_gate: Literal['disabled', 'eligible']
    validation_gate_reasons: tuple[str, ...]
    model_id: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    objective_ids: tuple[str, ...] = Field(min_length=1)
    predicted_evaluation_refs: tuple[CadAdaptiveEvaluationRef, ...] = Field(
        min_length=1
    )
    training_candidate_ids: tuple[str, ...] = Field(min_length=1)
    excluded_measured_candidate_ids: tuple[str, ...] = Field(min_length=1)
    candidate_pool_count: int = Field(ge=1)
    length_scale_m: float = Field(gt=0.0)
    proposal_limit: int = Field(ge=1)
    seed: int = 0
    acquisition_function: Literal['max_normalized_residual_uncertainty'] = (
        'max_normalized_residual_uncertainty'
    )
    algorithm_version: Literal['adaptive-residual-gp-1'] = ADAPTIVE_ALGORITHM_VERSION
    selected_candidate_id: str = Field(min_length=1)
    proposals: tuple[CadAdaptiveProposal, ...] = Field(min_length=1)
    adaptive_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    created_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_identity(self) -> 'CadAdaptivePlan':
        if self.execution_scope == 'development_synthetic':
            if self.source_evidence_scope != 'synthetic_fixture':
                raise ValueError('development adaptive plan must remain synthetic')
        elif self.source_evidence_scope != 'owned_room':
            raise ValueError('production adaptive plan must use owned-room evidence')

        if len(self.objective_ids) != len(set(self.objective_ids)):
            raise ValueError('adaptive objective ids must be unique')
        if len(self.training_candidate_ids) != len(set(self.training_candidate_ids)):
            raise ValueError('adaptive training candidate ids must be unique')
        if len(self.excluded_measured_candidate_ids) != len(
            set(self.excluded_measured_candidate_ids)
        ):
            raise ValueError('adaptive measured candidate ids must be unique')

        ref_ids = [ref.evaluation_id for ref in self.predicted_evaluation_refs]
        ref_shas = [ref.evaluation_sha256 for ref in self.predicted_evaluation_refs]
        ref_candidates = [ref.candidate_id for ref in self.predicted_evaluation_refs]
        if len(ref_ids) != len(set(ref_ids)) or len(ref_shas) != len(set(ref_shas)):
            raise ValueError('adaptive predicted evaluation refs must be unique')
        if len(ref_candidates) != len(set(ref_candidates)):
            raise ValueError(
                'adaptive predicted evaluation refs must be unique per candidate'
            )
        if set(ref_candidates) & set(self.excluded_measured_candidate_ids):
            raise ValueError(
                'adaptive predicted evidence must exclude measured candidates'
            )

        proposal_ids = [item.candidate_id for item in self.proposals]
        if len(proposal_ids) != len(set(proposal_ids)):
            raise ValueError('adaptive proposal candidate ids must be unique')
        if self.selected_candidate_id != self.proposals[0].candidate_id:
            raise ValueError('adaptive selected candidate must be the first proposal')
        if set(proposal_ids) & set(self.excluded_measured_candidate_ids):
            raise ValueError('adaptive proposals must exclude already measured candidates')
        if set(proposal_ids) - set(ref_candidates):
            raise ValueError(
                'adaptive proposals must bind their predicted objective evidence'
            )
        if len(self.predicted_evaluation_refs) != self.candidate_pool_count:
            raise ValueError(
                'adaptive predicted evidence must cover the candidate pool'
            )
        if any(
            tuple(item.objective_id for item in proposal.objectives)
            != self.objective_ids
            for proposal in self.proposals
        ):
            raise ValueError('adaptive proposals must share the plan objective ordering')
        if len(self.proposals) > self.proposal_limit:
            raise ValueError('adaptive proposals exceed proposal limit')

        if self.adaptive_sha256 != _digest(self.identity_payload()):
            raise ValueError('adaptive plan identity hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'document_id': self.document_id,
            'search_spec_id': self.search_spec_id,
            'search_spec_sha256': self.search_spec_sha256,
            'candidate_set_sha256': self.candidate_set_sha256,
            'validation_id': self.validation_id,
            'validation_sha256': self.validation_sha256,
            'execution_scope': self.execution_scope,
            'source_evidence_scope': self.source_evidence_scope,
            'validation_recommendation_gate': self.validation_recommendation_gate,
            'validation_gate_reasons': list(self.validation_gate_reasons),
            'model_id': self.model_id,
            'model_version': self.model_version,
            'objective_ids': list(self.objective_ids),
            'predicted_evaluation_refs': [
                ref.model_dump(mode='json')
                for ref in self.predicted_evaluation_refs
            ],
            'training_candidate_ids': list(self.training_candidate_ids),
            'excluded_measured_candidate_ids': list(
                self.excluded_measured_candidate_ids
            ),
            'candidate_pool_count': self.candidate_pool_count,
            'length_scale_m': self.length_scale_m,
            'proposal_limit': self.proposal_limit,
            'seed': self.seed,
            'acquisition_function': self.acquisition_function,
            'algorithm_version': self.algorithm_version,
            'selected_candidate_id': self.selected_candidate_id,
            'proposals': [
                proposal.model_dump(mode='json') for proposal in self.proposals
            ],
        }


def _candidate_feature(spec: CadSearchSpec, candidate: CadCandidate) -> np.ndarray:
    values: list[float] = []
    for axis in spec.axes:
        try:
            value = candidate.positions[axis.entity_id][f'{axis.axis}_m']
        except KeyError as exc:
            raise ValueError(
                f'adaptive candidate is missing search axis {axis.entity_id}/{axis.axis}'
            ) from exc
        values.append(float(value))
    return np.asarray(values, dtype=float)


def _gp_residual_estimate(
    train_x: np.ndarray,
    train_y: np.ndarray,
    query_x: np.ndarray,
    *,
    length_scale_m: float,
) -> tuple[float, float, float]:
    if train_x.ndim != 2 or query_x.ndim != 1:
        raise ValueError('adaptive GP feature shape is invalid')
    if train_x.shape[0] != train_y.shape[0] or not train_y.size:
        raise ValueError('adaptive GP training shape is invalid')

    scaled = train_x / float(length_scale_m)
    query = query_x / float(length_scale_m)
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

    mean = float(k_star @ alpha)
    absolute = np.abs(train_y)
    residual_scale = max(
        float(np.std(train_y)) if train_y.size > 1 else 0.0,
        float(np.mean(absolute)),
        1e-6,
    )
    uncertainty_ratio = float(np.sqrt(variance))
    return mean, residual_scale * uncertainty_ratio, uncertainty_ratio


def _predicted_evaluation(
    evaluations: Sequence[CadObjectiveEvaluation],
    candidate_id: str,
    objective_ids: tuple[str, ...],
) -> CadObjectiveEvaluation | None:
    selected = None
    for evaluation in evaluations:
        if evaluation.candidate_id != candidate_id:
            continue
        classes = {ref.evidence_class for ref in evaluation.input_refs}
        if 'predicted' not in classes or 'measured' in classes:
            continue
        try:
            for objective_id in objective_ids:
                evaluation.vector.metric(objective_id)
        except KeyError:
            continue
        selected = evaluation
    return selected


def select_predicted_evaluations(
    evaluations: Sequence[CadObjectiveEvaluation],
    objective_ids: Sequence[str],
) -> tuple[CadObjectiveEvaluation, ...]:
    """Canonical O30 evidence selection consumed by the adaptive planner.

    Keeps the latest predicted-only evaluation per candidate that carries
    every required objective, preserving evaluation order. Both the planner
    service and the repository replay use this selection so persisted plans
    stay bound to current exact evidence.
    """

    selected: dict[str, CadObjectiveEvaluation] = {}
    for evaluation in evaluations:
        classes = {ref.evidence_class for ref in evaluation.input_refs}
        if 'predicted' not in classes or 'measured' in classes:
            continue
        try:
            for objective_id in objective_ids:
                evaluation.vector.metric(objective_id)
        except KeyError:
            continue
        selected[evaluation.candidate_id] = evaluation
    return tuple(selected.values())


def build_adaptive_plan(
    *,
    spec: CadSearchSpec,
    candidate_set_sha256: str,
    validation: CadModelValidationRecord,
    candidates: Sequence[CadCandidate],
    predicted_evaluations: Sequence[CadObjectiveEvaluation],
    execution_scope: AdaptiveExecutionScope,
    length_scale_m: float = 0.5,
    proposal_limit: int = 20,
    is_cancelled: Callable[[], bool] | None = None,
) -> CadAdaptivePlan:
    require_validation_scope(validation, execution_scope)
    if validation.document_id != spec.document_id:
        raise ValueError('adaptive validation belongs to another document')
    if (
        validation.search_spec_id != spec.search_spec_id
        or validation.search_spec_sha256 != spec.search_spec_sha256
    ):
        raise ValueError('adaptive validation SearchSpec authority mismatch')
    if validation.candidate_set_sha256 != candidate_set_sha256:
        raise ValueError('adaptive candidate-set hash mismatch')
    if length_scale_m <= 0 or not isfinite(float(length_scale_m)):
        raise ValueError('adaptive length scale must be finite and positive')
    if proposal_limit < 1:
        raise ValueError('adaptive proposal limit must be positive')

    objective_ids = tuple(
        dict.fromkeys(sample.objective_id for sample in validation.objective_samples)
    )
    if not objective_ids:
        raise ValueError('adaptive planning requires O60 objective validation samples')

    candidate_by_id = {candidate.candidate_id: candidate for candidate in candidates}
    measured_ids = tuple(dict.fromkeys(pair.candidate_id for pair in validation.pairs))
    training_ids = tuple(
        dict.fromkeys(
            sample.candidate_id
            for sample in validation.objective_samples
            if sample.split == 'calibration'
        )
    )
    if not training_ids:
        raise ValueError('adaptive planning requires calibration objective samples')
    missing_training = set(training_ids) - set(candidate_by_id)
    if missing_training:
        raise ValueError(
            f'adaptive training candidates are missing from SearchSpec: {sorted(missing_training)}'
        )

    training_by_objective: dict[str, list] = {objective_id: [] for objective_id in objective_ids}
    units: dict[str, str] = {}
    for sample in validation.objective_samples:
        if sample.split != 'calibration':
            continue
        unit = units.setdefault(sample.objective_id, sample.unit)
        if unit != sample.unit:
            raise ValueError('adaptive objective units are inconsistent')
        training_by_objective[sample.objective_id].append(sample)

    training_arrays: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for objective_id in objective_ids:
        samples = training_by_objective[objective_id]
        if not samples:
            raise ValueError(
                f'adaptive objective lacks calibration training data: {objective_id}'
            )
        train_x = np.vstack(
            [_candidate_feature(spec, candidate_by_id[item.candidate_id]) for item in samples]
        )
        train_y = np.asarray(
            [item.measured_value - item.predicted_value for item in samples],
            dtype=float,
        )
        training_arrays[objective_id] = (train_x, train_y)

    proposals: list[CadAdaptiveProposal] = []
    evidence_refs: dict[str, CadAdaptiveEvaluationRef] = {}
    measured_set = set(measured_ids)
    for candidate in candidates:
        if is_cancelled is not None and is_cancelled():
            raise RuntimeError('adaptive plan build cancelled')
        if candidate.candidate_id in measured_set:
            continue
        evaluation = _predicted_evaluation(
            predicted_evaluations,
            candidate.candidate_id,
            objective_ids,
        )
        if evaluation is None:
            continue
        query_x = _candidate_feature(spec, candidate)
        estimates: list[CadAdaptiveObjectiveEstimate] = []
        ratios: list[float] = []
        for objective_id in objective_ids:
            metric = evaluation.vector.metric(objective_id)
            expected_unit = units[objective_id]
            if metric.unit != expected_unit:
                raise ValueError(
                    f'adaptive predicted objective unit mismatch: {objective_id}'
                )
            train_x, train_y = training_arrays[objective_id]
            correction, uncertainty, ratio = _gp_residual_estimate(
                train_x,
                train_y,
                query_x,
                length_scale_m=float(length_scale_m),
            )
            estimates.append(CadAdaptiveObjectiveEstimate(
                objective_id=objective_id,
                unit=metric.unit,
                predicted_value=float(metric.value),
                corrected_mean=float(metric.value) + correction,
                residual_uncertainty=uncertainty,
                uncertainty_ratio=ratio,
                training_count=int(train_y.size),
            ))
            ratios.append(ratio)
        score = max(ratios)
        proposals.append(CadAdaptiveProposal(
            candidate_id=candidate.candidate_id,
            acquisition_score=score,
            objectives=tuple(estimates),
        ))
        evidence_refs[candidate.candidate_id] = CadAdaptiveEvaluationRef(
            evaluation_id=evaluation.evaluation_id,
            evaluation_sha256=evaluation.evaluation_sha256,
            candidate_id=candidate.candidate_id,
        )

    if not proposals:
        raise ValueError(
            'adaptive planning has no unmeasured candidate with predicted objective evidence'
        )

    proposals.sort(key=lambda item: (-item.acquisition_score, item.candidate_id))
    candidate_pool_count = len(proposals)
    proposals = proposals[: min(int(proposal_limit), candidate_pool_count)]
    provisional = CadAdaptivePlan.model_construct(
        plan_id=str(uuid4()),
        document_id=spec.document_id,
        search_spec_id=spec.search_spec_id,
        search_spec_sha256=spec.search_spec_sha256,
        candidate_set_sha256=candidate_set_sha256,
        validation_id=validation.validation_id,
        validation_sha256=validation.validation_sha256,
        execution_scope=execution_scope,
        source_evidence_scope=validation.evidence_scope,
        validation_recommendation_gate=validation.recommendation_gate,
        validation_gate_reasons=validation.gate_reasons,
        model_id=validation.model_id,
        model_version=validation.model_version,
        objective_ids=objective_ids,
        predicted_evaluation_refs=tuple(
            evidence_refs[candidate_id]
            for candidate_id in sorted(evidence_refs)
        ),
        training_candidate_ids=training_ids,
        excluded_measured_candidate_ids=measured_ids,
        candidate_pool_count=candidate_pool_count,
        length_scale_m=float(length_scale_m),
        proposal_limit=int(proposal_limit),
        seed=0,
        acquisition_function='max_normalized_residual_uncertainty',
        algorithm_version=ADAPTIVE_ALGORITHM_VERSION,
        selected_candidate_id=proposals[0].candidate_id,
        proposals=tuple(proposals),
        adaptive_sha256='0' * 64,
        created_at_utc=adaptive_timestamp_utc(),
    )
    return CadAdaptivePlan(
        **provisional.model_dump(exclude={'adaptive_sha256'}),
        adaptive_sha256=_digest(provisional.identity_payload()),
    )
