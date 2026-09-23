from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from math import asin, cos, degrees, isfinite, radians, sin
from typing import Any, Callable, Literal, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_constraint_models import CadConstraintResult, CadConstraintSet
from .cad_constraints import evaluate_cad_constraints
from .cad_extended_search import (
    CadExtendedCandidate,
    aim_horizontal_yaw_deg,
    body_horizontal_yaw_deg,
    direction_with_horizontal_yaw,
    extended_candidate_preview_document,
)
from .cad_objective_models import CadObjectiveEvaluation
from .cad_orientation_constraints import orientation_constraint_rejections
from .cad_repository import SceneRevision
from .cad_scene import Direction3, Position3, SceneDocument, scene_content_hash
from .cad_search import candidate_preview_document
from .cad_search_models import (
    CadCandidate,
    CadSearchSpec,
    constraint_workspace_snapshot,
)
from .optimization_objectives import (
    ObjectiveDefinition,
    ObjectiveError,
    ObjectiveMetric,
    ObjectiveVector,
)


ROBUSTNESS_SCHEMA_VERSION = 1
ROBUSTNESS_ALGORITHM_VERSION = 'o90a-local-stencil-1'
ROBUSTNESS_MULTIDIMENSIONAL_ALGORITHM_VERSION = 'o90b-bounded-design-1'
ROBUSTNESS_UNCERTAINTY_ALGORITHM_VERSION = 'o90b-uncertainty-design-1'
RobustnessCandidate = CadCandidate | CadExtendedCandidate
RobustnessCandidateKind = Literal['cad_candidate', 'extended_candidate']
RobustnessAxisParameter = Literal[
    'speaker_x_m',
    'speaker_y_m',
    'speaker_z_m',
    'listener_x_m',
    'listener_y_m',
    'listener_z_m',
    'aim_yaw_deg',
    'aim_pitch_deg',
    'body_yaw_deg',
]


def canonical_robustness_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def canonical_robustness_sha256(value: Any) -> str:
    return sha256(canonical_robustness_json(value).encode('utf-8')).hexdigest()


def robustness_timestamp_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_constraint_workspace_authority(
    search_spec: CadSearchSpec,
    constraint_set: CadConstraintSet,
) -> None:
    """Require the exact immutable G10/O80 constraint workspace frozen by SearchSpec."""

    if constraint_set.document_id != search_spec.document_id:
        raise ValueError('robustness constraint workspace belongs to another document')
    snapshot_json, workspace_hash = constraint_workspace_snapshot(constraint_set)
    if (
        workspace_hash != search_spec.constraint_workspace_hash
        or snapshot_json != search_spec.constraint_snapshot_json
    ):
        raise ValueError('robustness constraint workspace authority mismatch')


class UncertaintyAxis(BaseModel):
    """One explicit bounded tolerance axis for the O90A local stencil.

    O90A deliberately supports bounded intervals only. A +/- tolerance carries no
    probability meaning and is not converted to a distribution.
    """

    model_config = ConfigDict(frozen=True)

    axis_id: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    parameter: RobustnessAxisParameter
    unit: Literal['m', 'deg']
    nominal_value: float
    minus_delta: float = Field(gt=0.0)
    plus_delta: float = Field(gt=0.0)
    uncertainty_model: Literal['bounded_interval'] = 'bounded_interval'
    allowed_min: float | None = None
    allowed_max: float | None = None

    @model_validator(mode='after')
    def valid_axis(self) -> 'UncertaintyAxis':
        values = (
            self.nominal_value,
            self.minus_delta,
            self.plus_delta,
            self.allowed_min,
            self.allowed_max,
        )
        if any(value is not None and not isfinite(float(value)) for value in values):
            raise ValueError('robustness axis values must be finite')
        expected_unit = 'm' if self.parameter.endswith('_m') else 'deg'
        if self.unit != expected_unit:
            raise ValueError(
                f'robustness axis {self.parameter} requires unit {expected_unit}'
            )
        if (
            self.allowed_min is not None
            and self.allowed_max is not None
            and self.allowed_max < self.allowed_min
        ):
            raise ValueError('robustness allowed_max must be >= allowed_min')
        if (
            self.allowed_min is not None
            and self.nominal_value < self.allowed_min
        ):
            raise ValueError('robustness nominal value is below allowed_min')
        if (
            self.allowed_max is not None
            and self.nominal_value > self.allowed_max
        ):
            raise ValueError('robustness nominal value is above allowed_max')
        return self


class LinkedPerturbationGroup(BaseModel):
    """Explicit deterministic linkage between bounded O90 input axes.

    The shared normalized coordinate is a sampling-design relationship only.
    It is not a probability distribution or a statistical correlation estimate.
    """

    model_config = ConfigDict(frozen=True)

    group_id: str = Field(min_length=1)
    axis_multipliers: dict[str, float] = Field(min_length=2)
    relationship: Literal['shared_normalized_coordinate'] = (
        'shared_normalized_coordinate'
    )

    @model_validator(mode='after')
    def valid_group(self) -> 'LinkedPerturbationGroup':
        if len(self.axis_multipliers) < 2:
            raise ValueError('linked perturbation group requires at least two axes')
        for axis_id, multiplier in self.axis_multipliers.items():
            if not axis_id:
                raise ValueError('linked perturbation axis IDs must be non-empty')
            value = float(multiplier)
            if not isfinite(value) or value == 0.0 or abs(value) > 1.0:
                raise ValueError(
                    'linked perturbation multipliers must be finite, non-zero, '
                    'and within [-1, 1]'
                )
        return self


class DistributionAxisUncertainty(BaseModel):
    """Explicit probability distribution for one O90 input axis."""

    model_config = ConfigDict(frozen=True)

    axis_id: str = Field(min_length=1)
    distribution: Literal['uniform', 'normal']
    min_delta: float | None = None
    max_delta: float | None = None
    mean_delta: float = 0.0
    stddev: float | None = Field(default=None, gt=0.0)

    @model_validator(mode='after')
    def valid_distribution(self) -> 'DistributionAxisUncertainty':
        values = (self.min_delta, self.max_delta, self.mean_delta, self.stddev)
        if any(value is not None and not isfinite(float(value)) for value in values):
            raise ValueError('distribution uncertainty values must be finite')
        if self.distribution == 'uniform':
            if self.min_delta is None or self.max_delta is None:
                raise ValueError('uniform uncertainty requires explicit min/max deltas')
            if self.max_delta <= self.min_delta:
                raise ValueError('uniform uncertainty max_delta must exceed min_delta')
            if self.stddev is not None or self.mean_delta != 0.0:
                raise ValueError('uniform uncertainty uses only explicit min/max deltas')
        else:
            if self.stddev is None:
                raise ValueError('normal uncertainty requires explicit stddev')
            if (self.min_delta is None) != (self.max_delta is None):
                raise ValueError(
                    'truncated normal uncertainty requires both min/max deltas'
                )
            if (
                self.min_delta is not None
                and self.max_delta is not None
                and self.max_delta <= self.min_delta
            ):
                raise ValueError('truncated normal max_delta must exceed min_delta')
        return self


class DistributionUncertaintyModel(BaseModel):
    """Explicit joint probability model with declared independence."""

    model_config = ConfigDict(frozen=True)

    model_kind: Literal['distribution'] = 'distribution'
    model_id: str = Field(min_length=1)
    dependence: Literal['independent'] = 'independent'
    axes: tuple[DistributionAxisUncertainty, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def unique_axes(self) -> 'DistributionUncertaintyModel':
        axis_ids = [axis.axis_id for axis in self.axes]
        if len(axis_ids) != len(set(axis_ids)):
            raise ValueError('distribution uncertainty axis IDs must be unique')
        return self


class ExplicitPerturbationState(BaseModel):
    """One supplied empirical/discrete joint perturbation state."""

    model_config = ConfigDict(frozen=True)

    state_id: str = Field(min_length=1)
    parameter_deltas: dict[str, float]
    probability_weight: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode='after')
    def finite_deltas(self) -> 'ExplicitPerturbationState':
        if any(not isfinite(float(value)) for value in self.parameter_deltas.values()):
            raise ValueError('explicit perturbation deltas must be finite')
        return self


def _validate_explicit_state_weights(
    states: Sequence[ExplicitPerturbationState],
    *,
    label: str,
) -> None:
    state_ids = [state.state_id for state in states]
    if len(state_ids) != len(set(state_ids)):
        raise ValueError(f'{label} state IDs must be unique')
    weighted = [state.probability_weight is not None for state in states]
    if any(weighted) and not all(weighted):
        raise ValueError(
            f'{label} probability weights must be supplied for every state or none'
        )
    if all(weighted):
        total = sum(float(state.probability_weight or 0.0) for state in states)
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f'{label} explicit probability weights must sum to 1')


class EmpiricalUncertaintyModel(BaseModel):
    """Supplied empirical samples; repeated observations are not implicit weights."""

    model_config = ConfigDict(frozen=True)

    model_kind: Literal['empirical'] = 'empirical'
    model_id: str = Field(min_length=1)
    samples: tuple[ExplicitPerturbationState, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def valid_samples(self) -> 'EmpiricalUncertaintyModel':
        _validate_explicit_state_weights(self.samples, label='empirical uncertainty')
        return self


class DiscreteUncertaintyModel(BaseModel):
    """Finite alternatives, probabilistic only when all weights are explicit."""

    model_config = ConfigDict(frozen=True)

    model_kind: Literal['discrete'] = 'discrete'
    model_id: str = Field(min_length=1)
    states: tuple[ExplicitPerturbationState, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def valid_states(self) -> 'DiscreteUncertaintyModel':
        _validate_explicit_state_weights(self.states, label='discrete uncertainty')
        return self


ExplicitInputUncertaintyModel = (
    DistributionUncertaintyModel
    | EmpiricalUncertaintyModel
    | DiscreteUncertaintyModel
)


class RobustnessSpec(BaseModel):
    """Immutable O90A authority bound to existing Scene/Search/Objective evidence."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = ROBUSTNESS_SCHEMA_VERSION
    robustness_spec_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    search_spec_id: str = Field(min_length=1)
    search_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_set_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_kind: RobustnessCandidateKind
    candidate_id: str = Field(min_length=1)
    candidate_payload_json: str = Field(min_length=2)
    candidate_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    nominal_objective_evaluation_id: str = Field(min_length=1)
    nominal_objective_evaluation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    objective_evaluation_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    nominal_prediction_result_ref: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    prediction_provider_id: str = Field(min_length=1)
    fidelity: str = Field(min_length=1)
    axes: tuple[UncertaintyAxis, ...] = Field(min_length=1)
    sampling_strategy: Literal[
        'deterministic_local_stencil',
        'deterministic_multidimensional_bounded',
        'deterministic_multidimensional_uncertainty',
    ] = 'deterministic_local_stencil'
    algorithm_version: Literal[
        'o90a-local-stencil-1',
        'o90b-bounded-design-1',
        'o90b-uncertainty-design-1',
    ] = ROBUSTNESS_ALGORITHM_VERSION
    sampling_seed: int | None = None
    sample_count: int | None = Field(default=None, ge=1)
    linked_groups: tuple[LinkedPerturbationGroup, ...] = ()
    input_uncertainty_model: ExplicitInputUncertaintyModel | None = None
    parent_robustness_spec_id: str | None = Field(default=None, min_length=1)
    parent_robustness_spec_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    software_version: str = Field(min_length=1)
    robustness_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    created_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_identity(self) -> 'RobustnessSpec':
        axis_ids = [axis.axis_id for axis in self.axes]
        axis_targets = [(axis.entity_id, axis.parameter) for axis in self.axes]
        if len(axis_ids) != len(set(axis_ids)):
            raise ValueError('robustness axis IDs must be unique')
        if len(axis_targets) != len(set(axis_targets)):
            raise ValueError('robustness axes must be unique by entity + parameter')
        known_axis_ids = set(axis_ids)
        linked_axis_ids: set[str] = set()
        group_ids: set[str] = set()
        for group in self.linked_groups:
            if group.group_id in group_ids:
                raise ValueError('linked perturbation group IDs must be unique')
            group_ids.add(group.group_id)
            group_axis_ids = set(group.axis_multipliers)
            unknown = group_axis_ids - known_axis_ids
            if unknown:
                raise ValueError(
                    f'linked perturbation group references unknown axes: {sorted(unknown)}'
                )
            overlap = linked_axis_ids & group_axis_ids
            if overlap:
                raise ValueError(
                    f'uncertainty axes may belong to only one linked group: '
                    f'{sorted(overlap)}'
                )
            linked_axis_ids.update(group_axis_ids)
        if self.sampling_strategy == 'deterministic_local_stencil':
            if self.algorithm_version != ROBUSTNESS_ALGORITHM_VERSION:
                raise ValueError('local robustness spec requires O90A algorithm version')
            if (
                self.sampling_seed is not None
                or self.sample_count is not None
                or self.linked_groups
                or self.input_uncertainty_model is not None
                or self.parent_robustness_spec_id is not None
                or self.parent_robustness_spec_sha256 is not None
            ):
                raise ValueError('O90A local spec cannot carry O90B sampling metadata')
        elif self.sampling_strategy == 'deterministic_multidimensional_bounded':
            if (
                self.algorithm_version
                != ROBUSTNESS_MULTIDIMENSIONAL_ALGORITHM_VERSION
            ):
                raise ValueError(
                    'bounded multidimensional robustness requires O90B bounded version'
                )
            if self.sampling_seed is None or self.sample_count is None:
                raise ValueError(
                    'bounded multidimensional robustness requires seed and sample_count'
                )
            if self.sample_count < 3:
                raise ValueError(
                    'bounded multidimensional robustness requires at least three samples'
                )
            if self.input_uncertainty_model is not None:
                raise ValueError(
                    'bounded interval sampling cannot carry a probability model'
                )
            if (
                self.parent_robustness_spec_id is None
                or self.parent_robustness_spec_sha256 is None
            ):
                raise ValueError(
                    'multidimensional robustness spec requires exact O90A parent'
                )
        else:
            if self.algorithm_version != ROBUSTNESS_UNCERTAINTY_ALGORITHM_VERSION:
                raise ValueError(
                    'explicit uncertainty sampling requires O90B uncertainty version'
                )
            if self.sample_count is None or self.input_uncertainty_model is None:
                raise ValueError(
                    'explicit uncertainty sampling requires model and sample_count'
                )
            if self.linked_groups:
                raise ValueError(
                    'probabilistic/empirical/discrete models do not reuse bounded linked groups'
                )
            if (
                self.parent_robustness_spec_id is None
                or self.parent_robustness_spec_sha256 is None
            ):
                raise ValueError(
                    'explicit uncertainty spec requires exact O90A parent'
                )
            model = self.input_uncertainty_model
            if isinstance(model, DistributionUncertaintyModel):
                if self.sampling_seed is None:
                    raise ValueError(
                        'distribution uncertainty requires an explicit sampling seed'
                    )
                if self.sample_count < 3:
                    raise ValueError(
                        'distribution uncertainty requires nominal plus at least two samples'
                    )
                model_axis_ids = {axis.axis_id for axis in model.axes}
                if model_axis_ids != known_axis_ids:
                    raise ValueError(
                        'distribution uncertainty must define every robustness axis'
                    )
            else:
                if self.sampling_seed is not None:
                    raise ValueError(
                        'empirical/discrete enumeration has no sampling seed'
                    )
                states = (
                    model.samples
                    if isinstance(model, EmpiricalUncertaintyModel)
                    else model.states
                )
                if self.sample_count != 1 + len(states):
                    raise ValueError(
                        'empirical/discrete sample_count must equal nominal plus states'
                    )
                for state in states:
                    unknown = set(state.parameter_deltas) - known_axis_ids
                    if unknown:
                        raise ValueError(
                            'explicit uncertainty state references unknown axes: '
                            f'{sorted(unknown)}'
                        )
        try:
            candidate_payload = json.loads(self.candidate_payload_json)
        except json.JSONDecodeError as exc:
            raise ValueError('candidate_payload_json must contain JSON') from exc
        if canonical_robustness_json(candidate_payload) != self.candidate_payload_json:
            raise ValueError('candidate_payload_json must be canonical JSON')
        if canonical_robustness_sha256(candidate_payload) != self.candidate_sha256:
            raise ValueError('robustness candidate hash mismatch')
        candidate = _candidate_from_payload(self.candidate_kind, candidate_payload)
        if candidate.candidate_id != self.candidate_id:
            raise ValueError('robustness candidate ID does not match candidate payload')
        expected_hash = canonical_robustness_sha256(self.identity_payload())
        if expected_hash != self.robustness_spec_sha256:
            raise ValueError('robustness spec identity hash mismatch')
        if self.robustness_spec_id != _semantic_id('rob', expected_hash):
            raise ValueError('robustness spec ID does not match semantic hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = {
            'schema_version': self.schema_version,
            'document_id': self.document_id,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'search_spec_id': self.search_spec_id,
            'search_spec_sha256': self.search_spec_sha256,
            'candidate_set_sha256': self.candidate_set_sha256,
            'candidate_kind': self.candidate_kind,
            'candidate_id': self.candidate_id,
            'candidate': json.loads(self.candidate_payload_json),
            'candidate_sha256': self.candidate_sha256,
            'nominal_objective_evaluation_id': self.nominal_objective_evaluation_id,
            'nominal_objective_evaluation_sha256': (
                self.nominal_objective_evaluation_sha256
            ),
            'objective_evaluation_spec_sha256': (
                self.objective_evaluation_spec_sha256
            ),
            'nominal_prediction_result_ref': self.nominal_prediction_result_ref,
            'model_id': self.model_id,
            'model_version': self.model_version,
            'prediction_provider_id': self.prediction_provider_id,
            'fidelity': self.fidelity,
            'axes': [axis.model_dump(mode='json') for axis in self.axes],
            'sampling_strategy': self.sampling_strategy,
            'algorithm_version': self.algorithm_version,
            'software_version': self.software_version,
        }
        if self.sampling_strategy == 'deterministic_multidimensional_bounded':
            payload.update(
                {
                    'sampling_seed': self.sampling_seed,
                    'sample_count': self.sample_count,
                    'linked_groups': [
                        group.model_dump(mode='json') for group in self.linked_groups
                    ],
                    'parent_robustness_spec_id': self.parent_robustness_spec_id,
                    'parent_robustness_spec_sha256': (
                        self.parent_robustness_spec_sha256
                    ),
                }
            )
        elif self.sampling_strategy == 'deterministic_multidimensional_uncertainty':
            assert self.input_uncertainty_model is not None
            payload.update(
                {
                    'sampling_seed': self.sampling_seed,
                    'sample_count': self.sample_count,
                    'input_uncertainty_model': (
                        self.input_uncertainty_model.model_dump(mode='json')
                    ),
                    'parent_robustness_spec_id': self.parent_robustness_spec_id,
                    'parent_robustness_spec_sha256': (
                        self.parent_robustness_spec_sha256
                    ),
                }
            )
        return payload


class LocalPerturbation(BaseModel):
    model_config = ConfigDict(frozen=True)

    sample_id: str = Field(min_length=1)
    sample_index: int = Field(ge=0)
    axis_id: str | None = None
    step: Literal['nominal', 'minus', 'plus', 'multidimensional', 'uncertainty']
    parameter_deltas: dict[str, float]
    uncertainty_model_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    uncertainty_item_id: str | None = Field(default=None, min_length=1)
    probability_weight: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode='after')
    def valid_delta(self) -> 'LocalPerturbation':
        if self.step == 'nominal':
            if self.axis_id is not None or self.parameter_deltas:
                raise ValueError('nominal perturbation must not contain an axis delta')
        elif self.step in {'multidimensional', 'uncertainty'}:
            if self.axis_id is not None:
                raise ValueError(
                    'multi-axis perturbation must not name one local axis'
                )
            if self.step == 'multidimensional' and not self.parameter_deltas:
                raise ValueError(
                    'multidimensional perturbation requires parameter deltas'
                )
        else:
            if self.axis_id is None:
                raise ValueError('local perturbation requires axis_id')
            if set(self.parameter_deltas) != {self.axis_id}:
                raise ValueError('local perturbation must contain exactly its axis delta')
        if any(not isfinite(float(value)) for value in self.parameter_deltas.values()):
            raise ValueError('perturbation deltas must be finite')
        return self


class PerturbationObjectiveResult(BaseModel):
    """Result adapter supplied by the existing prediction/O30 evaluation path."""

    model_config = ConfigDict(frozen=True)

    prediction_result_ref: str = Field(min_length=1)
    objective_vector: ObjectiveVector


class PerturbationSample(BaseModel):
    """Append-only evidence for one deterministic O90A perturbation."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = ROBUSTNESS_SCHEMA_VERSION
    sample_id: str = Field(min_length=1)
    robustness_spec_id: str = Field(min_length=1)
    robustness_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_id: str = Field(min_length=1)
    sample_index: int = Field(ge=0)
    axis_id: str | None = None
    step: Literal['nominal', 'minus', 'plus', 'multidimensional', 'uncertainty']
    parameter_deltas: dict[str, float]
    uncertainty_model_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    uncertainty_item_id: str | None = Field(default=None, min_length=1)
    probability_weight: float | None = Field(default=None, ge=0.0, le=1.0)
    perturbed_scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    feasible: bool
    g10_results: tuple[CadConstraintResult, ...] = ()
    o80_rejection_ids: tuple[str, ...] = ()
    domain_rejection_ids: tuple[str, ...] = ()
    model_id: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    prediction_provider_id: str = Field(min_length=1)
    fidelity: str = Field(min_length=1)
    objective_evaluation_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    prediction_result_ref: str | None = Field(default=None, min_length=1)
    objective_vector: ObjectiveVector | None = None
    failure_reason: str | None = None
    sample_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    created_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_identity(self) -> 'PerturbationSample':
        LocalPerturbation(
            sample_id=self.sample_id,
            sample_index=self.sample_index,
            axis_id=self.axis_id,
            step=self.step,
            parameter_deltas=self.parameter_deltas,
        )
        if self.objective_vector is not None:
            if self.objective_vector.candidate_id != self.sample_id:
                raise ValueError(
                    'perturbation objective vector candidate_id must equal sample_id'
                )
            if self.prediction_result_ref is None:
                raise ValueError(
                    'perturbation objective vector requires prediction_result_ref'
                )
        if not self.feasible and self.objective_vector is not None:
            raise ValueError('infeasible perturbations must remain unscored evidence')
        uncertainty_values = (
            self.uncertainty_model_sha256,
            self.uncertainty_item_id,
            self.probability_weight,
        )
        if any(value is not None for value in uncertainty_values):
            if self.uncertainty_model_sha256 is None or self.uncertainty_item_id is None:
                raise ValueError(
                    'uncertainty sample provenance requires model hash and item ID'
                )
            if self.step not in {'nominal', 'uncertainty'}:
                raise ValueError(
                    'explicit uncertainty provenance requires uncertainty/nominal step'
                )
        expected = canonical_robustness_sha256(self.identity_payload())
        if expected != self.sample_sha256:
            raise ValueError('perturbation sample evidence hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = {
            'schema_version': self.schema_version,
            'sample_id': self.sample_id,
            'robustness_spec_id': self.robustness_spec_id,
            'robustness_spec_sha256': self.robustness_spec_sha256,
            'candidate_id': self.candidate_id,
            'sample_index': self.sample_index,
            'axis_id': self.axis_id,
            'step': self.step,
            'parameter_deltas': self.parameter_deltas,
            'perturbed_scene_content_hash': self.perturbed_scene_content_hash,
            'feasible': self.feasible,
            'g10_results': [
                item.model_dump(mode='json') for item in self.g10_results
            ],
            'o80_rejection_ids': list(self.o80_rejection_ids),
            'domain_rejection_ids': list(self.domain_rejection_ids),
            'model_id': self.model_id,
            'model_version': self.model_version,
            'prediction_provider_id': self.prediction_provider_id,
            'fidelity': self.fidelity,
            'objective_evaluation_spec_sha256': (
                self.objective_evaluation_spec_sha256
            ),
            'prediction_result_ref': self.prediction_result_ref,
            'objective_vector': (
                None
                if self.objective_vector is None
                else self.objective_vector.identity_payload()
            ),
            'failure_reason': self.failure_reason,
        }
        if self.uncertainty_model_sha256 is not None:
            payload.update(
                {
                    'uncertainty_model_sha256': self.uncertainty_model_sha256,
                    'uncertainty_item_id': self.uncertainty_item_id,
                    'probability_weight': self.probability_weight,
                }
            )
        return payload


class LocalSensitivity(BaseModel):
    """Finite-difference evidence for one objective and one declared axis."""

    model_config = ConfigDict(frozen=True)

    axis_id: str = Field(min_length=1)
    input_unit: Literal['m', 'deg']
    objective_unit: str = Field(min_length=1)
    minus_sample_id: str = Field(min_length=1)
    plus_sample_id: str = Field(min_length=1)
    minus_delta: float = Field(gt=0.0)
    plus_delta: float = Field(gt=0.0)
    minus_value: float | None
    nominal_value: float
    plus_value: float | None
    minus_slope_per_unit: float | None
    plus_slope_per_unit: float | None
    central_slope_per_unit: float | None
    state: Literal['available', 'infeasible_or_failed']


class SampledObjectiveEnvelope(BaseModel):
    """Observed finite-sample objective range; never an asserted true worst-case."""

    model_config = ConfigDict(frozen=True)

    sampled_min_sample_id: str = Field(min_length=1)
    sampled_min_value: float
    sampled_max_sample_id: str = Field(min_length=1)
    sampled_max_value: float
    percentile_values: dict[str, float] | None = None

    @model_validator(mode='after')
    def valid_envelope(self) -> 'SampledObjectiveEnvelope':
        if not all(
            isfinite(float(value))
            for value in (self.sampled_min_value, self.sampled_max_value)
        ):
            raise ValueError('sampled envelope values must be finite')
        if self.sampled_max_value < self.sampled_min_value:
            raise ValueError('sampled envelope max must be >= min')
        if self.percentile_values is not None and any(
            not isfinite(float(value)) for value in self.percentile_values.values()
        ):
            raise ValueError('sampled percentile values must be finite')
        return self


class RobustnessEvaluation(BaseModel):
    """Per-objective O90A result; never a scalar overall robustness score."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = ROBUSTNESS_SCHEMA_VERSION
    evaluation_id: str = Field(min_length=1)
    robustness_spec_id: str = Field(min_length=1)
    robustness_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_id: str = Field(min_length=1)
    objective_id: str = Field(min_length=1)
    objective_unit: str = Field(min_length=1)
    direction: Literal['minimize', 'maximize']
    objective_definition: ObjectiveDefinition | None = None
    nominal_sample_id: str = Field(min_length=1)
    nominal_value: float
    local_sensitivities: tuple[LocalSensitivity, ...]
    sampled_worst_semantics: Literal['sampled_worst'] = 'sampled_worst'
    sampled_worst_sample_id: str = Field(min_length=1)
    sampled_worst_value: float
    sample_ids: tuple[str, ...] = Field(min_length=1)
    infeasible_sample_ids: tuple[str, ...] = ()
    failed_sample_ids: tuple[str, ...] = ()
    sampled_envelope: SampledObjectiveEnvelope | None = None
    feasible_fraction: float | None = Field(default=None, ge=0.0, le=1.0)
    sampling_provenance_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    percentile_semantics: Literal[
        'not_available_bounded_interval',
        'not_available_empirical_unweighted',
        'not_available_discrete_unweighted',
        'explicit_probability_model',
    ] | None = None
    probability_semantics: Literal[
        'explicit_distribution',
        'explicit_empirical_weights',
        'explicit_discrete_weights',
    ] | None = None
    mean_value: float | None = None
    constraint_violation_probability: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
    )
    probability_sample_ids: tuple[str, ...] = ()
    evaluation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    created_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_identity(self) -> 'RobustnessEvaluation':
        numeric = (self.nominal_value, self.sampled_worst_value)
        if not all(isfinite(float(value)) for value in numeric):
            raise ValueError('robustness evaluation values must be finite')
        if self.objective_definition is not None:
            if (
                self.objective_definition.objective_id != self.objective_id
                or self.objective_definition.unit != self.objective_unit
                or self.objective_definition.direction != self.direction
            ):
                raise ValueError(
                    'robustness objective definition does not match id/unit/direction'
                )
        if len(self.sample_ids) != len(set(self.sample_ids)):
            raise ValueError('robustness evaluation sample IDs must be unique')
        if self.nominal_sample_id not in self.sample_ids:
            raise ValueError('nominal sample must be part of robustness evaluation')
        if self.sampled_worst_sample_id not in self.sample_ids:
            raise ValueError('sampled worst sample must be part of robustness evaluation')
        multidimensional_values = (
            self.sampled_envelope,
            self.feasible_fraction,
            self.sampling_provenance_sha256,
            self.percentile_semantics,
        )
        if any(value is not None for value in multidimensional_values):
            if any(value is None for value in multidimensional_values):
                raise ValueError(
                    'multidimensional robustness summary must be complete'
                )
            assert self.sampled_envelope is not None
            if (
                self.sampled_envelope.sampled_min_sample_id not in self.sample_ids
                or self.sampled_envelope.sampled_max_sample_id not in self.sample_ids
            ):
                raise ValueError('sampled envelope samples must belong to evaluation')
            unavailable = {
                'not_available_bounded_interval',
                'not_available_empirical_unweighted',
                'not_available_discrete_unweighted',
            }
            if self.percentile_semantics in unavailable:
                if self.sampled_envelope.percentile_values is not None:
                    raise ValueError(
                        'non-probabilistic uncertainty cannot expose percentiles'
                    )
                if (
                    self.probability_semantics is not None
                    or self.mean_value is not None
                    or self.constraint_violation_probability is not None
                    or self.probability_sample_ids
                ):
                    raise ValueError(
                        'non-probabilistic uncertainty cannot expose probability outputs'
                    )
            else:
                if self.sampled_envelope.percentile_values is None:
                    raise ValueError(
                        'explicit probability summary requires percentile values'
                    )
                if (
                    self.probability_semantics is None
                    or self.mean_value is None
                    or self.constraint_violation_probability is None
                    or not self.probability_sample_ids
                ):
                    raise ValueError(
                        'explicit probability summary requires complete probability outputs'
                    )
                if not set(self.probability_sample_ids).issubset(self.sample_ids):
                    raise ValueError(
                        'probability samples must belong to robustness evaluation'
                    )
        if self.mean_value is not None and not isfinite(float(self.mean_value)):
            raise ValueError('robustness mean value must be finite')
        expected = canonical_robustness_sha256(self.identity_payload())
        if expected != self.evaluation_sha256:
            raise ValueError('robustness evaluation identity hash mismatch')
        if self.evaluation_id != _semantic_id('re', expected):
            raise ValueError('robustness evaluation ID does not match semantic hash')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = {
            'schema_version': self.schema_version,
            'robustness_spec_id': self.robustness_spec_id,
            'robustness_spec_sha256': self.robustness_spec_sha256,
            'candidate_id': self.candidate_id,
            'objective_id': self.objective_id,
            'objective_unit': self.objective_unit,
            'direction': self.direction,
            'nominal_sample_id': self.nominal_sample_id,
            'nominal_value': self.nominal_value,
            'local_sensitivities': [
                item.model_dump(mode='json') for item in self.local_sensitivities
            ],
            'sampled_worst_semantics': self.sampled_worst_semantics,
            'sampled_worst_sample_id': self.sampled_worst_sample_id,
            'sampled_worst_value': self.sampled_worst_value,
            'sample_ids': list(self.sample_ids),
            'infeasible_sample_ids': list(self.infeasible_sample_ids),
            'failed_sample_ids': list(self.failed_sample_ids),
        }
        if self.objective_definition is not None:
            payload['objective_definition'] = self.objective_definition.model_dump(
                mode='json'
            )
        if self.sampled_envelope is not None:
            payload.update(
                {
                    'sampled_envelope': self.sampled_envelope.model_dump(mode='json'),
                    'feasible_fraction': self.feasible_fraction,
                    'sampling_provenance_sha256': self.sampling_provenance_sha256,
                    'percentile_semantics': self.percentile_semantics,
                }
            )
        if (
            self.probability_semantics is not None
            or self.mean_value is not None
            or self.constraint_violation_probability is not None
            or self.probability_sample_ids
        ):
            payload.update(
                {
                    'probability_semantics': self.probability_semantics,
                    'mean_value': self.mean_value,
                    'constraint_violation_probability': (
                        self.constraint_violation_probability
                    ),
                    'probability_sample_ids': list(self.probability_sample_ids),
                }
            )
        return payload


class RobustnessSampleCache(Protocol):
    """Append-only cache authority for exact O90 sample reuse."""

    def save_spec(self, spec: RobustnessSpec) -> RobustnessSpec: ...

    def list_reusable_samples(
        self,
        spec: RobustnessSpec,
    ) -> tuple[PerturbationSample, ...]: ...

    def save_sample(self, sample: PerturbationSample) -> PerturbationSample: ...

    def save_evaluations(
        self,
        evaluations: tuple[RobustnessEvaluation, ...],
    ) -> tuple[RobustnessEvaluation, ...]: ...


class RobustnessExecutionResult(BaseModel):
    """One resumable O90 execution attempt."""

    model_config = ConfigDict(frozen=True)

    status: Literal['completed', 'cancelled']
    robustness_spec_id: str = Field(min_length=1)
    robustness_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    samples: tuple[PerturbationSample, ...]
    evaluations: tuple[RobustnessEvaluation, ...] = ()
    reused_sample_ids: tuple[str, ...] = ()
    computed_sample_ids: tuple[str, ...] = ()



def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _candidate_kind(candidate: RobustnessCandidate) -> RobustnessCandidateKind:
    if isinstance(candidate, CadExtendedCandidate):
        return 'extended_candidate'
    return 'cad_candidate'


def _candidate_from_payload(
    kind: RobustnessCandidateKind,
    payload: dict[str, Any],
) -> RobustnessCandidate:
    if kind == 'extended_candidate':
        return CadExtendedCandidate.model_validate(payload)
    return CadCandidate.model_validate(payload)


def _candidate_document(
    revision: SceneRevision,
    candidate: RobustnessCandidate,
) -> SceneDocument:
    if isinstance(candidate, CadExtendedCandidate):
        return extended_candidate_preview_document(revision.document, candidate)
    return candidate_preview_document(revision.document, candidate)


def _axis_value(document: SceneDocument, axis: UncertaintyAxis) -> float:
    entity = document.entity(axis.entity_id)
    if axis.parameter.startswith('speaker_'):
        if entity.kind != 'speaker':
            raise ValueError(
                f'axis {axis.axis_id} requires a speaker: {axis.entity_id}'
            )
        coordinate = axis.parameter.removeprefix('speaker_').removesuffix('_m')
        return float(getattr(entity.position, f'{coordinate}_m'))
    if axis.parameter.startswith('listener_'):
        if entity.kind not in {'seat', 'measurement_point'}:
            raise ValueError(
                f'axis {axis.axis_id} requires a seat/listener point: {axis.entity_id}'
            )
        coordinate = axis.parameter.removeprefix('listener_').removesuffix('_m')
        return float(getattr(entity.position, f'{coordinate}_m'))
    if entity.kind != 'speaker' or entity.aim_xyz is None:
        raise ValueError(
            f'axis {axis.axis_id} requires a speaker with explicit acoustic aim'
        )
    if axis.parameter == 'aim_yaw_deg':
        return float(aim_horizontal_yaw_deg(entity.aim_xyz))
    if axis.parameter == 'aim_pitch_deg':
        return float(degrees(asin(max(-1.0, min(1.0, entity.aim_xyz.z)))))
    if axis.parameter == 'body_yaw_deg':
        return float(body_horizontal_yaw_deg(entity))
    raise ValueError(f'unsupported robustness axis: {axis.parameter}')


def _metric_schema(vector: ObjectiveVector) -> tuple[tuple[str, str], ...]:
    return tuple(
        (metric.objective_id, metric.definition_id)
        for metric in vector.metrics
    )


def build_robustness_spec(
    *,
    source_revision: SceneRevision,
    search_spec: CadSearchSpec,
    candidate: RobustnessCandidate,
    candidate_set_sha256: str,
    nominal_objective: CadObjectiveEvaluation,
    nominal_prediction_result_ref: str,
    model_id: str,
    model_version: str,
    prediction_provider_id: str,
    fidelity: str,
    axes: Sequence[UncertaintyAxis],
    software_version: str,
    created_at_utc: str | None = None,
) -> RobustnessSpec:
    if (
        search_spec.document_id != source_revision.document_id
        or search_spec.scene_revision_id != source_revision.revision_id
        or search_spec.scene_content_hash != source_revision.content_hash
    ):
        raise ValueError('robustness SearchSpec does not match exact SceneRevision')
    if (
        nominal_objective.document_id != source_revision.document_id
        or nominal_objective.scene_revision_id != source_revision.revision_id
        or nominal_objective.scene_content_hash != source_revision.content_hash
        or nominal_objective.search_spec_id != search_spec.search_spec_id
        or nominal_objective.search_spec_sha256 != search_spec.search_spec_sha256
    ):
        raise ValueError('robustness nominal objective authority mismatch')
    if nominal_objective.candidate_id != candidate.candidate_id:
        raise ValueError('robustness nominal objective candidate mismatch')
    for metric in nominal_objective.vector.metrics:
        try:
            metric.comparison_value()
        except ObjectiveError as exc:
            raise ValueError(
                f'robustness nominal objective is not comparison-eligible: {metric.objective_id}'
            ) from exc
    if not any(
        ref.source_id == nominal_prediction_result_ref
        for ref in nominal_objective.input_refs
    ):
        raise ValueError(
            'nominal prediction result ref must be present in O30 objective inputs'
        )
    ordered_axes = tuple(sorted(axes, key=lambda item: item.axis_id))
    if not ordered_axes:
        raise ValueError('robustness spec requires at least one uncertainty axis')

    candidate_document = _candidate_document(source_revision, candidate)
    tolerance = 1e-9
    for axis in ordered_axes:
        actual = _axis_value(candidate_document, axis)
        if abs(actual - float(axis.nominal_value)) > tolerance:
            raise ValueError(
                f'axis {axis.axis_id} nominal value {axis.nominal_value} does not '
                f'match exact candidate value {actual}'
            )

    if len(candidate_set_sha256) != 64 or any(
        ch not in '0123456789abcdef' for ch in candidate_set_sha256
    ):
        raise ValueError('candidate_set_sha256 must be a lowercase SHA-256')

    candidate_payload = candidate.model_dump(mode='json')
    candidate_payload_json = canonical_robustness_json(candidate_payload)
    candidate_sha = canonical_robustness_sha256(candidate_payload)
    identity = {
        'schema_version': ROBUSTNESS_SCHEMA_VERSION,
        'document_id': source_revision.document_id,
        'scene_revision_id': source_revision.revision_id,
        'scene_content_hash': source_revision.content_hash,
        'search_spec_id': search_spec.search_spec_id,
        'search_spec_sha256': search_spec.search_spec_sha256,
        'candidate_set_sha256': candidate_set_sha256,
        'candidate_kind': _candidate_kind(candidate),
        'candidate_id': candidate.candidate_id,
        'candidate': candidate_payload,
        'candidate_sha256': candidate_sha,
        'nominal_objective_evaluation_id': nominal_objective.evaluation_id,
        'nominal_objective_evaluation_sha256': nominal_objective.evaluation_sha256,
        'objective_evaluation_spec_sha256': nominal_objective.evaluation_spec_sha256,
        'nominal_prediction_result_ref': nominal_prediction_result_ref,
        'model_id': model_id,
        'model_version': model_version,
        'prediction_provider_id': prediction_provider_id,
        'fidelity': fidelity,
        'axes': [axis.model_dump(mode='json') for axis in ordered_axes],
        'sampling_strategy': 'deterministic_local_stencil',
        'algorithm_version': ROBUSTNESS_ALGORITHM_VERSION,
        'software_version': software_version,
    }
    digest = canonical_robustness_sha256(identity)
    return RobustnessSpec(
        robustness_spec_id=_semantic_id('rob', digest),
        document_id=source_revision.document_id,
        scene_revision_id=source_revision.revision_id,
        scene_content_hash=source_revision.content_hash,
        search_spec_id=search_spec.search_spec_id,
        search_spec_sha256=search_spec.search_spec_sha256,
        candidate_set_sha256=candidate_set_sha256,
        candidate_kind=_candidate_kind(candidate),
        candidate_id=candidate.candidate_id,
        candidate_payload_json=candidate_payload_json,
        candidate_sha256=candidate_sha,
        nominal_objective_evaluation_id=nominal_objective.evaluation_id,
        nominal_objective_evaluation_sha256=nominal_objective.evaluation_sha256,
        objective_evaluation_spec_sha256=nominal_objective.evaluation_spec_sha256,
        nominal_prediction_result_ref=nominal_prediction_result_ref,
        model_id=model_id,
        model_version=model_version,
        prediction_provider_id=prediction_provider_id,
        fidelity=fidelity,
        axes=ordered_axes,
        software_version=software_version,
        robustness_spec_sha256=digest,
        created_at_utc=created_at_utc or robustness_timestamp_utc(),
    )


def build_local_stencil(spec: RobustnessSpec) -> tuple[LocalPerturbation, ...]:
    if spec.sampling_strategy != 'deterministic_local_stencil':
        raise ValueError('local stencil requires an O90A local robustness spec')
    plans: list[LocalPerturbation] = []

    def add(
        *,
        axis_id: str | None,
        step: Literal['nominal', 'minus', 'plus', 'multidimensional'],
        deltas: dict[str, float],
    ) -> None:
        payload = {
            'robustness_spec_sha256': spec.robustness_spec_sha256,
            'candidate_id': spec.candidate_id,
            'axis_id': axis_id,
            'step': step,
            'parameter_deltas': deltas,
            'algorithm_version': spec.algorithm_version,
        }
        plans.append(
            LocalPerturbation(
                sample_id=_semantic_id('rp', canonical_robustness_sha256(payload)),
                sample_index=len(plans),
                axis_id=axis_id,
                step=step,
                parameter_deltas=deltas,
            )
        )

    add(axis_id=None, step='nominal', deltas={})
    for axis in spec.axes:
        add(
            axis_id=axis.axis_id,
            step='minus',
            deltas={axis.axis_id: -float(axis.minus_delta)},
        )
        add(
            axis_id=axis.axis_id,
            step='plus',
            deltas={axis.axis_id: float(axis.plus_delta)},
        )
    return tuple(plans)


def _replace_position(
    document: SceneDocument,
    axis: UncertaintyAxis,
    target_value: float,
) -> SceneDocument:
    entity = document.entity(axis.entity_id)
    coordinate = (
        axis.parameter.removeprefix('speaker_')
        if axis.parameter.startswith('speaker_')
        else axis.parameter.removeprefix('listener_')
    ).removesuffix('_m')
    position = entity.position.model_copy(update={f'{coordinate}_m': target_value})
    replacement = entity.model_copy(update={'position': position})
    return document.model_copy(
        update={
            'entities': tuple(
                replacement if item.entity_id == entity.entity_id else item
                for item in document.entities
            )
        }
    )


def _direction_with_pitch(direction: Direction3, pitch_deg: float) -> Direction3:
    yaw = radians(aim_horizontal_yaw_deg(direction))
    pitch = radians(float(pitch_deg))
    horizontal = cos(pitch)
    return Direction3(
        x=horizontal * sin(yaw),
        y=horizontal * cos(yaw),
        z=sin(pitch),
    )


def apply_local_perturbation(
    document: SceneDocument,
    axis: UncertaintyAxis,
    delta: float,
) -> SceneDocument:
    """Apply one O90A delta without mutating the source document."""

    if not isfinite(float(delta)):
        raise ValueError('robustness perturbation delta must be finite')
    current = _axis_value(document, axis)
    target = current + float(delta)

    if axis.parameter.endswith('_m'):
        return _replace_position(document, axis, target)

    entity = document.entity(axis.entity_id)
    if axis.parameter == 'aim_yaw_deg':
        replacement = entity.model_copy(
            update={'aim_xyz': direction_with_horizontal_yaw(entity.aim_xyz, target)}
        )
        return document.model_copy(
            update={
                'entities': tuple(
                    replacement if item.entity_id == entity.entity_id else item
                    for item in document.entities
                )
            }
        )
    if axis.parameter == 'aim_pitch_deg':
        if target < -90.0 or target > 90.0:
            raise ValueError('acoustic aim pitch must remain within [-90, 90] degrees')
        replacement = entity.model_copy(
            update={'aim_xyz': _direction_with_pitch(entity.aim_xyz, target)}
        )
        return document.model_copy(
            update={
                'entities': tuple(
                    replacement if item.entity_id == entity.entity_id else item
                    for item in document.entities
                )
            }
        )
    if axis.parameter == 'body_yaw_deg':
        overlay = CadExtendedCandidate(
            candidate_id=f'robust-overlay:{axis.axis_id}',
            base_candidate_id='robust-base',
            raw_index=0,
            feasible_index=0,
            positions={},
            body_yaw_deg={axis.entity_id: target},
        )
        return extended_candidate_preview_document(document, overlay)
    raise ValueError(f'unsupported robustness perturbation: {axis.parameter}')


def _domain_rejections(
    axis: UncertaintyAxis | None,
    delta: float,
) -> tuple[str, ...]:
    if axis is None:
        return ()
    target = float(axis.nominal_value) + float(delta)
    if axis.allowed_min is not None and target < axis.allowed_min:
        return (f'__uncertainty_bound__:{axis.axis_id}:min',)
    if axis.allowed_max is not None and target > axis.allowed_max:
        return (f'__uncertainty_bound__:{axis.axis_id}:max',)
    return ()


def _copy_nominal_vector(
    nominal: CadObjectiveEvaluation,
    sample_id: str,
) -> ObjectiveVector:
    return nominal.vector.model_copy(update={'candidate_id': sample_id})


def _make_sample(
    *,
    spec: RobustnessSpec,
    plan: LocalPerturbation,
    document: SceneDocument,
    constraint_set: CadConstraintSet,
    changed_entity_ids: Sequence[str],
    objective_result: PerturbationObjectiveResult | None,
    failure_reason: str | None,
    domain_rejection_ids: tuple[str, ...],
    created_at_utc: str,
) -> PerturbationSample:
    g10 = evaluate_cad_constraints(document, constraint_set)
    o80 = orientation_constraint_rejections(
        document,
        constraint_set,
        changed_entity_ids=changed_entity_ids,
    )
    feasible = (
        g10.constraints_satisfied
        and not o80
        and not domain_rejection_ids
    )
    if not feasible:
        objective_result = None
        failure_reason = failure_reason or 'hard_constraint_violation'

    payload = {
        'schema_version': ROBUSTNESS_SCHEMA_VERSION,
        'sample_id': plan.sample_id,
        'robustness_spec_id': spec.robustness_spec_id,
        'robustness_spec_sha256': spec.robustness_spec_sha256,
        'candidate_id': spec.candidate_id,
        'sample_index': plan.sample_index,
        'axis_id': plan.axis_id,
        'step': plan.step,
        'parameter_deltas': plan.parameter_deltas,
        'perturbed_scene_content_hash': scene_content_hash(document),
        'feasible': feasible,
        'g10_results': [item.model_dump(mode='json') for item in g10.results],
        'o80_rejection_ids': list(o80),
        'domain_rejection_ids': list(domain_rejection_ids),
        'model_id': spec.model_id,
        'model_version': spec.model_version,
        'prediction_provider_id': spec.prediction_provider_id,
        'fidelity': spec.fidelity,
        'objective_evaluation_spec_sha256': spec.objective_evaluation_spec_sha256,
        'prediction_result_ref': (
            None if objective_result is None else objective_result.prediction_result_ref
        ),
        'objective_vector': (
            None
            if objective_result is None
            else objective_result.objective_vector.identity_payload()
        ),
        'failure_reason': failure_reason,
    }
    return PerturbationSample(
        **payload,
        sample_sha256=canonical_robustness_sha256(payload),
        created_at_utc=created_at_utc,
    )


def evaluate_local_robustness(
    *,
    source_revision: SceneRevision,
    search_spec: CadSearchSpec,
    spec: RobustnessSpec,
    constraint_set: CadConstraintSet,
    nominal_objective: CadObjectiveEvaluation,
    evaluator: Callable[[SceneDocument, str], PerturbationObjectiveResult],
    created_at_utc: str | None = None,
) -> tuple[tuple[PerturbationSample, ...], tuple[RobustnessEvaluation, ...]]:
    """Evaluate nominal/+/- local evidence using existing G10/O30/O80 authorities."""

    if (
        source_revision.revision_id != spec.scene_revision_id
        or source_revision.content_hash != spec.scene_content_hash
        or source_revision.document_id != spec.document_id
    ):
        raise ValueError('robustness source SceneRevision authority mismatch')
    if (
        search_spec.search_spec_id != spec.search_spec_id
        or search_spec.search_spec_sha256 != spec.search_spec_sha256
    ):
        raise ValueError('robustness SearchSpec authority mismatch')
    if (
        nominal_objective.evaluation_id != spec.nominal_objective_evaluation_id
        or nominal_objective.evaluation_sha256
        != spec.nominal_objective_evaluation_sha256
        or nominal_objective.evaluation_spec_sha256
        != spec.objective_evaluation_spec_sha256
    ):
        raise ValueError('robustness O30 objective authority mismatch')
    _validate_constraint_workspace_authority(search_spec, constraint_set)

    candidate = _candidate_from_payload(
        spec.candidate_kind,
        json.loads(spec.candidate_payload_json),
    )
    nominal_document = _candidate_document(source_revision, candidate)
    plans = build_local_stencil(spec)
    axis_by_id = {axis.axis_id: axis for axis in spec.axes}
    objective_schema = _metric_schema(nominal_objective.vector)
    timestamp = created_at_utc or robustness_timestamp_utc()
    samples: list[PerturbationSample] = []

    for plan in plans:
        document = nominal_document
        changed_ids: tuple[str, ...]
        domain_rejections: tuple[str, ...]
        result: PerturbationObjectiveResult | None = None
        failure_reason: str | None = None

        if plan.axis_id is None:
            changed_ids = tuple(sorted({axis.entity_id for axis in spec.axes}))
            domain_rejections = ()
        else:
            axis = axis_by_id[plan.axis_id]
            delta = float(plan.parameter_deltas[plan.axis_id])
            changed_ids = (axis.entity_id,)
            domain_rejections = _domain_rejections(axis, delta)
            if not domain_rejections:
                try:
                    document = apply_local_perturbation(document, axis, delta)
                except Exception as exc:
                    domain_rejections = (
                        f'__perturbation_unsupported__:{axis.axis_id}',
                    )
                    failure_reason = f'perturbation_failed:{exc}'

        g10 = evaluate_cad_constraints(document, constraint_set)
        o80 = orientation_constraint_rejections(
            document,
            constraint_set,
            changed_entity_ids=changed_ids,
        )
        feasible = (
            g10.constraints_satisfied
            and not o80
            and not domain_rejections
        )

        if feasible:
            if plan.step == 'nominal':
                result = PerturbationObjectiveResult(
                    prediction_result_ref=spec.nominal_prediction_result_ref,
                    objective_vector=_copy_nominal_vector(
                        nominal_objective,
                        plan.sample_id,
                    ),
                )
            else:
                try:
                    result = evaluator(document, plan.sample_id)
                    if _metric_schema(result.objective_vector) != objective_schema:
                        raise ValueError(
                            'perturbed objective schema does not match nominal O30 vector'
                        )
                    if result.objective_vector.candidate_id != plan.sample_id:
                        raise ValueError(
                            'perturbed O30 vector candidate_id must equal sample_id'
                        )
                except Exception as exc:
                    result = None
                    failure_reason = f'objective_evaluation_failed:{exc}'

        payload = {
            'schema_version': ROBUSTNESS_SCHEMA_VERSION,
            'sample_id': plan.sample_id,
            'robustness_spec_id': spec.robustness_spec_id,
            'robustness_spec_sha256': spec.robustness_spec_sha256,
            'candidate_id': spec.candidate_id,
            'sample_index': plan.sample_index,
            'axis_id': plan.axis_id,
            'step': plan.step,
            'parameter_deltas': plan.parameter_deltas,
            'perturbed_scene_content_hash': scene_content_hash(document),
            'feasible': feasible,
            'g10_results': [
                item.model_dump(mode='json') for item in g10.results
            ],
            'o80_rejection_ids': list(o80),
            'domain_rejection_ids': list(domain_rejections),
            'model_id': spec.model_id,
            'model_version': spec.model_version,
            'prediction_provider_id': spec.prediction_provider_id,
            'fidelity': spec.fidelity,
            'objective_evaluation_spec_sha256': (
                spec.objective_evaluation_spec_sha256
            ),
            'prediction_result_ref': (
                None if result is None else result.prediction_result_ref
            ),
            'objective_vector': (
                None
                if result is None
                else result.objective_vector.identity_payload()
            ),
            'failure_reason': (
                failure_reason
                if failure_reason is not None
                else (None if feasible else 'hard_constraint_violation')
            ),
        }
        samples.append(
            PerturbationSample(
                **payload,
                sample_sha256=canonical_robustness_sha256(payload),
                created_at_utc=timestamp,
            )
        )

    return tuple(samples), build_robustness_evaluations(
        spec,
        tuple(samples),
        created_at_utc=timestamp,
    )


def _metric_for_sample(
    sample: PerturbationSample,
    objective_id: str,
) -> ObjectiveMetric | None:
    if sample.objective_vector is None:
        return None
    try:
        metric = sample.objective_vector.metric(objective_id)
    except KeyError:
        return None
    if metric.state != 'available' or metric.value is None:
        return None
    return metric


def build_robustness_evaluations(
    spec: RobustnessSpec,
    samples: Sequence[PerturbationSample],
    *,
    created_at_utc: str | None = None,
) -> tuple[RobustnessEvaluation, ...]:
    ordered = tuple(sorted(samples, key=lambda item: item.sample_index))
    expected_ids = tuple(item.sample_id for item in build_local_stencil(spec))
    if tuple(item.sample_id for item in ordered) != expected_ids:
        raise ValueError('robustness samples do not match the deterministic local stencil')
    if any(
        item.robustness_spec_id != spec.robustness_spec_id
        or item.robustness_spec_sha256 != spec.robustness_spec_sha256
        or item.candidate_id != spec.candidate_id
        for item in ordered
    ):
        raise ValueError('robustness samples belong to another authority')
    nominal = ordered[0]
    if nominal.step != 'nominal' or nominal.objective_vector is None:
        raise ValueError('robustness evaluation requires a scored nominal sample')

    timestamp = created_at_utc or robustness_timestamp_utc()
    by_axis_step = {
        (item.axis_id, item.step): item
        for item in ordered
        if item.axis_id is not None
    }
    evaluations: list[RobustnessEvaluation] = []
    for nominal_metric in nominal.objective_vector.metrics:
        sensitivities: list[LocalSensitivity] = []
        scored: list[tuple[PerturbationSample, ObjectiveMetric]] = []
        for sample in ordered:
            metric = _metric_for_sample(sample, nominal_metric.objective_id)
            if metric is not None:
                scored.append((sample, metric))

        for axis in spec.axes:
            minus = by_axis_step[(axis.axis_id, 'minus')]
            plus = by_axis_step[(axis.axis_id, 'plus')]
            minus_metric = _metric_for_sample(minus, nominal_metric.objective_id)
            plus_metric = _metric_for_sample(plus, nominal_metric.objective_id)
            minus_value = None if minus_metric is None else float(minus_metric.value)
            plus_value = None if plus_metric is None else float(plus_metric.value)
            nominal_value = float(nominal_metric.value)
            minus_slope = (
                None
                if minus_value is None
                else (nominal_value - minus_value) / float(axis.minus_delta)
            )
            plus_slope = (
                None
                if plus_value is None
                else (plus_value - nominal_value) / float(axis.plus_delta)
            )
            central = (
                None
                if minus_value is None or plus_value is None
                else (plus_value - minus_value)
                / (float(axis.plus_delta) + float(axis.minus_delta))
            )
            sensitivities.append(
                LocalSensitivity(
                    axis_id=axis.axis_id,
                    input_unit=axis.unit,
                    objective_unit=nominal_metric.unit,
                    minus_sample_id=minus.sample_id,
                    plus_sample_id=plus.sample_id,
                    minus_delta=axis.minus_delta,
                    plus_delta=axis.plus_delta,
                    minus_value=minus_value,
                    nominal_value=nominal_value,
                    plus_value=plus_value,
                    minus_slope_per_unit=minus_slope,
                    plus_slope_per_unit=plus_slope,
                    central_slope_per_unit=central,
                    state=(
                        'available'
                        if minus_value is not None and plus_value is not None
                        else 'infeasible_or_failed'
                    ),
                )
            )

        if not scored:
            raise ValueError(
                f'objective {nominal_metric.objective_id} has no scored robustness samples'
            )
        if nominal_metric.direction == 'minimize':
            worst_sample, worst_metric = max(
                scored,
                key=lambda pair: float(pair[1].value),
            )
        else:
            worst_sample, worst_metric = min(
                scored,
                key=lambda pair: float(pair[1].value),
            )

        identity = {
            'schema_version': ROBUSTNESS_SCHEMA_VERSION,
            'robustness_spec_id': spec.robustness_spec_id,
            'robustness_spec_sha256': spec.robustness_spec_sha256,
            'candidate_id': spec.candidate_id,
            'objective_id': nominal_metric.objective_id,
            'objective_unit': nominal_metric.unit,
            'direction': nominal_metric.direction,
            'nominal_sample_id': nominal.sample_id,
            'nominal_value': float(nominal_metric.value),
            'local_sensitivities': [
                item.model_dump(mode='json') for item in sensitivities
            ],
            'sampled_worst_semantics': 'sampled_worst',
            'sampled_worst_sample_id': worst_sample.sample_id,
            'sampled_worst_value': float(worst_metric.value),
            'sample_ids': [item.sample_id for item in ordered],
            'infeasible_sample_ids': [
                item.sample_id for item in ordered if not item.feasible
            ],
            'failed_sample_ids': [
                item.sample_id
                for item in ordered
                if item.feasible and item.objective_vector is None
            ],
        }
        if nominal_metric.definition is not None:
            identity['objective_definition'] = nominal_metric.definition.model_dump(
                mode='json'
            )
        digest = canonical_robustness_sha256(identity)
        evaluations.append(
            RobustnessEvaluation(
                **identity,
                evaluation_id=_semantic_id('re', digest),
                evaluation_sha256=digest,
                created_at_utc=timestamp,
            )
        )
    return tuple(evaluations)
