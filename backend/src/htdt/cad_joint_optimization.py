from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite
from typing import Any, Literal, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_calibration import (
    CadCalibrationPlan,
    CadDeviceCapabilityConstraints,
    evaluate_calibration_support,
)
from .cad_extended_search import (
    CadExtendedSearchSpec,
    aim_horizontal_yaw_deg,
    aim_pitch_deg,
    body_horizontal_yaw_deg,
)
from .cad_measurement_quality import (
    CadMeasurementQualityReport,
    gate_measurement_claim,
)
from .cad_repository import SceneRevision
from .cad_scene import SceneDocument
from .cad_search_models import CadSearchSpec
from .cad_system_variant import SystemVariant, materialize_system_variant
from .optimization_objectives import (
    ObjectiveDefinition,
    ObjectiveMetric,
    ObjectiveVector,
)
from .pareto import ParetoEmptyError, ParetoResult, pareto_front


JOINT_OPTIMIZATION_SCHEMA_VERSION = 1
JOINT_OPTIMIZATION_AUTHORITY_VERSION = 'issue174-joint-optimization-1'
JOINT_CANDIDATE_AUTHORITY_VERSION = 'issue174-joint-candidate-1'
JOINT_EVALUATION_AUTHORITY_VERSION = 'issue174-joint-evaluation-1'
JOINT_SELECTION_AUTHORITY_VERSION = 'issue174-joint-selection-1'

PhysicalParameter = Literal[
    'x_m',
    'y_m',
    'z_m',
    'aim_yaw_deg',
    'aim_pitch_deg',
    'body_yaw_deg',
]
DspParameter = Literal[
    'gain_db',
    'peq_frequency_hz',
    'peq_q',
    'peq_gain_db',
    'crossover_frequency_hz',
    'crossover_order',
    'delay_s',
    'polarity',
]
JointCandidateClass = Literal['position_only', 'dsp_only', 'joint']
JointEligibilityState = Literal['ELIGIBLE', 'BLOCKED']


def canonical_joint_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def canonical_joint_sha256(value: Any) -> str:
    return sha256(canonical_joint_json(value).encode('utf-8')).hexdigest()


def device_capability_sha256(
    constraints: CadDeviceCapabilityConstraints,
) -> str:
    return canonical_joint_sha256(constraints.model_dump(mode='json'))


def routing_snapshot_sha256(plan: CadCalibrationPlan) -> str:
    payload = [
        {
            'channel_id': channel.channel_id,
            'physical_output_id': channel.physical_output_id,
            'routing': list(channel.routing),
        }
        for channel in plan.channels
    ]
    return canonical_joint_sha256(payload)


class JointPhysicalVariableRef(BaseModel):
    """Exact reference to a variable already owned by base/extended placement search."""

    model_config = ConfigDict(frozen=True)

    variable_id: str = Field(min_length=1)
    authority_kind: Literal['base_search', 'extended_search']
    authority_id: str = Field(min_length=1)
    authority_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    entity_id: str = Field(min_length=1)
    parameter: PhysicalParameter
    unit: Literal['m', 'deg']
    minimum: float
    maximum: float
    step: float = Field(gt=0.0)

    @model_validator(mode='after')
    def valid_variable(self) -> 'JointPhysicalVariableRef':
        values = (self.minimum, self.maximum, self.step)
        if not all(isfinite(float(value)) for value in values):
            raise ValueError('physical decision variable bounds must be finite')
        if self.maximum < self.minimum:
            raise ValueError('physical decision variable maximum must be >= minimum')
        expected_unit = 'm' if self.parameter.endswith('_m') else 'deg'
        if self.unit != expected_unit:
            raise ValueError(
                f'physical parameter {self.parameter} requires unit {expected_unit}'
            )
        return self


class JointDspVariable(BaseModel):
    """Search-domain declaration only; CalibrationPlan remains DSP settings authority."""

    model_config = ConfigDict(frozen=True)

    variable_id: str = Field(min_length=1)
    channel_id: str = Field(min_length=1)
    parameter: DspParameter
    filter_id: str | None = Field(default=None, min_length=1)
    crossover_index: int | None = Field(default=None, ge=0)
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = Field(default=None, gt=0.0)
    allowed_values: tuple[str, ...] = ()
    required_measurement_claim: Literal[
        'magnitude_response',
        'common_timing',
        'polarity',
    ]
    required_band_hz: tuple[float, float] | None = None

    @model_validator(mode='after')
    def valid_variable(self) -> 'JointDspVariable':
        expected_claim = (
            'common_timing'
            if self.parameter == 'delay_s'
            else 'polarity'
            if self.parameter == 'polarity'
            else 'magnitude_response'
        )
        if self.required_measurement_claim != expected_claim:
            raise ValueError(
                f'{self.parameter} requires {expected_claim} measurement authority'
            )
        if expected_claim == 'magnitude_response':
            if self.required_band_hz is None:
                raise ValueError(
                    f'{self.parameter} requires an explicit magnitude capability band'
                )
            low, high = self.required_band_hz
            if (
                not isfinite(float(low))
                or not isfinite(float(high))
                or low <= 0.0
                or high <= low
            ):
                raise ValueError('DSP required magnitude band is invalid')
        elif self.required_band_hz is not None:
            raise ValueError(
                'timing/polarity DSP variables must not carry a magnitude band'
            )

        if self.parameter.startswith('peq_'):
            if self.filter_id is None:
                raise ValueError('PEQ decision variable requires filter_id')
        elif self.filter_id is not None:
            raise ValueError('filter_id is valid only for PEQ decision variables')

        if self.parameter.startswith('crossover_'):
            if self.crossover_index is None:
                raise ValueError('crossover decision variable requires crossover_index')
        elif self.crossover_index is not None:
            raise ValueError(
                'crossover_index is valid only for crossover decision variables'
            )

        if self.parameter == 'polarity':
            if self.minimum is not None or self.maximum is not None or self.step is not None:
                raise ValueError('polarity decision variable is categorical')
            if self.allowed_values != ('normal', 'inverted'):
                raise ValueError(
                    'polarity decision variable must explicitly allow normal/inverted'
                )
        else:
            if self.allowed_values:
                raise ValueError('numeric DSP decision variable cannot carry allowed_values')
            if self.minimum is None or self.maximum is None:
                raise ValueError('numeric DSP decision variable requires explicit bounds')
            if not isfinite(float(self.minimum)) or not isfinite(float(self.maximum)):
                raise ValueError('DSP decision variable bounds must be finite')
            if self.maximum < self.minimum:
                raise ValueError('DSP decision variable maximum must be >= minimum')
        return self


class JointDspAuthorityRef(BaseModel):
    """Exact #173 source/quality/device authority reused by joint optimization."""

    model_config = ConfigDict(frozen=True)

    base_calibration_plan_id: str = Field(min_length=1)
    base_calibration_plan_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_measurement_id: str = Field(min_length=1)
    source_measurement_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_dataset_id: str = Field(min_length=1)
    source_dataset_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    measurement_quality_report_id: str = Field(min_length=1)
    measurement_quality_report_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    device_capability_id: str = Field(min_length=1)
    device_capability_version: str = Field(min_length=1)
    device_capability_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    routing_snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    phase_bearing_correction_policy: Literal[
        'blocked_without_explicit_authority'
    ] = 'blocked_without_explicit_authority'
    routing_rewrite_policy: Literal[
        'forbidden_in_initial_slice'
    ] = 'forbidden_in_initial_slice'


class JointHardConstraintRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    constraint_kind: Literal[
        'physical_search_workspace',
        'device_capability',
        'external',
    ]
    authority_id: str = Field(min_length=1)
    authority_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class JointRobustnessVariableMapping(BaseModel):
    model_config = ConfigDict(frozen=True)

    joint_variable_id: str = Field(min_length=1)
    o90_axis_id: str = Field(min_length=1)


class JointRobustnessSpecRef(BaseModel):
    """Reference only. This slice does not alter O90 or infer DSP probability."""

    model_config = ConfigDict(frozen=True)

    robustness_spec_id: str = Field(min_length=1)
    robustness_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    variable_mapping: tuple[JointRobustnessVariableMapping, ...] = Field(min_length=1)
    dsp_perturbation_policy: Literal['none'] = 'none'

    @model_validator(mode='after')
    def unique_mapping(self) -> 'JointRobustnessSpecRef':
        joint_ids = [item.joint_variable_id for item in self.variable_mapping]
        o90_ids = [item.o90_axis_id for item in self.variable_mapping]
        if len(joint_ids) != len(set(joint_ids)):
            raise ValueError('O90 joint variable mapping must be unique')
        if len(o90_ids) != len(set(o90_ids)):
            raise ValueError('O90 axis mapping must be unique')
        return self


class JointEvaluatorIdentity(BaseModel):
    model_config = ConfigDict(frozen=True)

    evaluator_id: str = Field(min_length=1)
    evaluator_version: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    fidelity: str = Field(min_length=1)
    evidence_scope: Literal['owned_room', 'validated_model', 'fixture_only']
    fixture_only: bool = False
    synthetic: bool = False
    production_eligible: bool = False

    @model_validator(mode='after')
    def valid_scope(self) -> 'JointEvaluatorIdentity':
        if self.fixture_only:
            if self.evidence_scope != 'fixture_only' or not self.synthetic:
                raise ValueError('fixture-only evaluator must be explicitly synthetic')
            if self.production_eligible:
                raise ValueError('fixture-only evaluator cannot be production eligible')
        if self.synthetic and self.production_eligible:
            raise ValueError('synthetic evaluator cannot be production eligible')
        if self.evidence_scope == 'fixture_only' and not self.fixture_only:
            raise ValueError('fixture_only evidence scope requires fixture_only=true')
        return self


class JointCandidateSamplingRule(BaseModel):
    """No new optimizer: candidates must be supplied by existing authorities."""

    model_config = ConfigDict(frozen=True)

    rule: Literal['explicit_authority_candidates'] = 'explicit_authority_candidates'
    ordering: Literal[
        'canonical_decision_vector'
    ] = 'canonical_decision_vector'
    seed: None = None


class JointOptimizationSpec(BaseModel):
    """Immutable solver-neutral orchestration contract for Issue #174 first slice."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = JOINT_OPTIMIZATION_SCHEMA_VERSION
    authority_version: Literal[
        'issue174-joint-optimization-1'
    ] = JOINT_OPTIMIZATION_AUTHORITY_VERSION
    spec_id: str = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)

    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    base_system_variant_id: str = Field(min_length=1)
    base_system_variant_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    physical_search_spec_id: str = Field(min_length=1)
    physical_search_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    extended_search_spec_id: str | None = Field(default=None, min_length=1)
    extended_search_spec_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    physical_variables: tuple[JointPhysicalVariableRef, ...] = Field(min_length=1)

    dsp_authority: JointDspAuthorityRef | None = None
    dsp_variables: tuple[JointDspVariable, ...] = ()

    objectives: tuple[ObjectiveDefinition, ...] = Field(min_length=1)
    hard_constraints: tuple[JointHardConstraintRef, ...] = Field(min_length=1)
    robustness: JointRobustnessSpecRef
    evaluator: JointEvaluatorIdentity
    candidate_budget: int = Field(ge=1, le=50_000)
    sampling_rule: JointCandidateSamplingRule = JointCandidateSamplingRule()
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_spec(self) -> 'JointOptimizationSpec':
        physical_ids = [item.variable_id for item in self.physical_variables]
        if len(physical_ids) != len(set(physical_ids)):
            raise ValueError('physical decision variable IDs must be unique')
        dsp_ids = [item.variable_id for item in self.dsp_variables]
        if len(dsp_ids) != len(set(dsp_ids)):
            raise ValueError('DSP decision variable IDs must be unique')
        if set(physical_ids).intersection(dsp_ids):
            raise ValueError('physical and DSP decision variable IDs must be disjoint')

        if bool(self.extended_search_spec_id) != bool(self.extended_search_spec_sha256):
            raise ValueError('extended search id/hash must be supplied together')
        if self.dsp_variables and self.dsp_authority is None:
            raise ValueError('DSP decision variables require exact CalibrationPlan authority')
        if not self.dsp_variables and self.dsp_authority is not None:
            raise ValueError('DSP authority must not be attached when DSP search is disabled')

        objective_ids = [item.objective_id for item in self.objectives]
        if len(objective_ids) != len(set(objective_ids)):
            raise ValueError('objective definitions must be unique by objective_id')
        constraint_keys = [
            (item.constraint_kind, item.authority_id, item.authority_sha256)
            for item in self.hard_constraints
        ]
        if len(constraint_keys) != len(set(constraint_keys)):
            raise ValueError('hard constraint references must be unique')

        known_variables = set(physical_ids) | set(dsp_ids)
        unknown_mappings = {
            item.joint_variable_id
            for item in self.robustness.variable_mapping
            if item.joint_variable_id not in known_variables
        }
        if unknown_mappings:
            raise ValueError(
                f'O90 mapping references unknown joint variables: {sorted(unknown_mappings)}'
            )

        if self.semantic_sha256 != canonical_joint_sha256(self.semantic_payload()):
            raise ValueError('JointOptimizationSpec semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'document_id': self.document_id,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'base_system_variant_id': self.base_system_variant_id,
            'base_system_variant_sha256': self.base_system_variant_sha256,
            'physical_search_spec_id': self.physical_search_spec_id,
            'physical_search_spec_sha256': self.physical_search_spec_sha256,
            'extended_search_spec_id': self.extended_search_spec_id,
            'extended_search_spec_sha256': self.extended_search_spec_sha256,
            'physical_variables': [
                item.model_dump(mode='json') for item in self.physical_variables
            ],
            'dsp_authority': (
                None
                if self.dsp_authority is None
                else self.dsp_authority.model_dump(mode='json')
            ),
            'dsp_variables': [
                item.model_dump(mode='json') for item in self.dsp_variables
            ],
            'objectives': [
                item.model_dump(mode='json') for item in self.objectives
            ],
            'hard_constraints': [
                item.model_dump(mode='json') for item in self.hard_constraints
            ],
            'robustness': self.robustness.model_dump(mode='json'),
            'evaluator': self.evaluator.model_dump(mode='json'),
            'candidate_budget': self.candidate_budget,
            'sampling_rule': self.sampling_rule.model_dump(mode='json'),
        }


def physical_variables_from_authority(
    search_spec: CadSearchSpec,
    extended_search_spec: CadExtendedSearchSpec | None,
) -> tuple[JointPhysicalVariableRef, ...]:
    variables: list[JointPhysicalVariableRef] = []
    for axis in search_spec.axes:
        parameter = f'{axis.axis}_m'
        variables.append(JointPhysicalVariableRef(
            variable_id=f'physical:{axis.entity_id}:{parameter}',
            authority_kind='base_search',
            authority_id=search_spec.search_spec_id,
            authority_sha256=search_spec.search_spec_sha256,
            entity_id=axis.entity_id,
            parameter=parameter,
            unit='m',
            minimum=axis.min_m,
            maximum=axis.max_m,
            step=axis.step_m,
        ))

    if extended_search_spec is not None:
        for axis in extended_search_spec.axes:
            variables.append(JointPhysicalVariableRef(
                variable_id=f'physical:{axis.entity_id}:{axis.parameter}',
                authority_kind='extended_search',
                authority_id=extended_search_spec.extended_search_id,
                authority_sha256=extended_search_spec.extended_search_sha256,
                entity_id=axis.entity_id,
                parameter=axis.parameter,
                unit='deg',
                minimum=axis.min_value,
                maximum=axis.max_value,
                step=axis.step,
            ))
    return tuple(variables)


def build_joint_optimization_spec(
    *,
    scene_revision: SceneRevision,
    base_system_variant: SystemVariant,
    physical_search_spec: CadSearchSpec,
    extended_search_spec: CadExtendedSearchSpec | None,
    base_calibration_plan: CadCalibrationPlan | None,
    measurement_quality_report: CadMeasurementQualityReport | None,
    dsp_variables: Sequence[JointDspVariable],
    objectives: Sequence[ObjectiveDefinition],
    robustness: JointRobustnessSpecRef,
    evaluator: JointEvaluatorIdentity,
    candidate_budget: int,
    created_at_utc: str,
    spec_id: str | None = None,
    hard_constraints: Sequence[JointHardConstraintRef] = (),
) -> JointOptimizationSpec:
    if (
        base_system_variant.document_id != scene_revision.document_id
        or base_system_variant.baseline_revision_id != scene_revision.revision_id
        or base_system_variant.baseline_content_hash != scene_revision.content_hash
    ):
        raise ValueError(
            'base SystemVariant must bind the exact baseline SceneRevision'
        )
    if (
        physical_search_spec.document_id != scene_revision.document_id
        or physical_search_spec.scene_revision_id != scene_revision.revision_id
        or physical_search_spec.scene_content_hash != scene_revision.content_hash
    ):
        raise ValueError(
            'physical search authority must bind the exact baseline SceneRevision'
        )

    if extended_search_spec is not None:
        if (
            extended_search_spec.document_id != scene_revision.document_id
            or extended_search_spec.base_search_spec_id
            != physical_search_spec.search_spec_id
            or extended_search_spec.base_search_spec_sha256
            != physical_search_spec.search_spec_sha256
        ):
            raise ValueError(
                'extended search authority must bind the exact base physical search'
            )

    dsp_items = tuple(dsp_variables)
    dsp_authority: JointDspAuthorityRef | None = None
    if dsp_items:
        if base_calibration_plan is None or measurement_quality_report is None:
            raise ValueError(
                'DSP search requires base CalibrationPlan and MeasurementQualityReport'
            )
        if (
            base_calibration_plan.document_id != scene_revision.document_id
            or base_calibration_plan.scene_revision_id != scene_revision.revision_id
            or base_calibration_plan.scene_content_hash != scene_revision.content_hash
        ):
            raise ValueError(
                'base CalibrationPlan must bind the exact baseline SceneRevision'
            )
        if (
            base_calibration_plan.system_variant_id != base_system_variant.variant_id
            or base_calibration_plan.system_variant_sha256
            != base_system_variant.variant_sha256
        ):
            raise ValueError(
                'base CalibrationPlan must bind the exact base SystemVariant'
            )
        if base_calibration_plan.support_state != 'SUPPORTED':
            raise ValueError(
                'joint DSP search requires a supported base CalibrationPlan'
            )
        if (
            measurement_quality_report.report_id
            != base_calibration_plan.measurement_quality_report_id
            or measurement_quality_report.report_sha256
            != base_calibration_plan.measurement_quality_report_sha256
            or measurement_quality_report.measurement_id
            != base_calibration_plan.source_measurement_id
            or measurement_quality_report.measurement_sha256
            != base_calibration_plan.source_measurement_sha256
            or measurement_quality_report.dataset_id
            != base_calibration_plan.source_dataset_id
            or measurement_quality_report.dataset_sha256
            != base_calibration_plan.source_dataset_sha256
        ):
            raise ValueError(
                'joint DSP search MeasurementQualityReport authority mismatch'
            )

        constraints = base_calibration_plan.device_constraints
        dsp_authority = JointDspAuthorityRef(
            base_calibration_plan_id=base_calibration_plan.plan_id,
            base_calibration_plan_sha256=base_calibration_plan.plan_semantic_sha256,
            source_measurement_id=base_calibration_plan.source_measurement_id,
            source_measurement_sha256=base_calibration_plan.source_measurement_sha256,
            source_dataset_id=base_calibration_plan.source_dataset_id,
            source_dataset_sha256=base_calibration_plan.source_dataset_sha256,
            measurement_quality_report_id=measurement_quality_report.report_id,
            measurement_quality_report_sha256=measurement_quality_report.report_sha256,
            device_capability_id=constraints.capability_id,
            device_capability_version=constraints.capability_version,
            device_capability_sha256=device_capability_sha256(constraints),
            routing_snapshot_sha256=routing_snapshot_sha256(base_calibration_plan),
        )
    elif base_calibration_plan is not None or measurement_quality_report is not None:
        raise ValueError(
            'base calibration/quality authority must not be attached without DSP variables'
        )

    physical_variables = physical_variables_from_authority(
        physical_search_spec,
        extended_search_spec,
    )

    constraints = list(hard_constraints)
    constraints.append(JointHardConstraintRef(
        constraint_kind='physical_search_workspace',
        authority_id=physical_search_spec.search_spec_id,
        authority_sha256=physical_search_spec.constraint_workspace_hash,
    ))
    if dsp_authority is not None:
        constraints.append(JointHardConstraintRef(
            constraint_kind='device_capability',
            authority_id=dsp_authority.device_capability_id,
            authority_sha256=dsp_authority.device_capability_sha256,
        ))
    unique_constraints = tuple(dict.fromkeys(
        (
            item.constraint_kind,
            item.authority_id,
            item.authority_sha256,
        )
        for item in constraints
    ))
    constraint_refs = tuple(
        JointHardConstraintRef(
            constraint_kind=kind,
            authority_id=authority_id,
            authority_sha256=authority_sha256,
        )
        for kind, authority_id, authority_sha256 in unique_constraints
    )

    payload: dict[str, Any] = {
        'schema_version': JOINT_OPTIMIZATION_SCHEMA_VERSION,
        'authority_version': JOINT_OPTIMIZATION_AUTHORITY_VERSION,
        'spec_id': spec_id or str(uuid4()),
        'created_at_utc': created_at_utc,
        'document_id': scene_revision.document_id,
        'scene_revision_id': scene_revision.revision_id,
        'scene_content_hash': scene_revision.content_hash,
        'base_system_variant_id': base_system_variant.variant_id,
        'base_system_variant_sha256': base_system_variant.variant_sha256,
        'physical_search_spec_id': physical_search_spec.search_spec_id,
        'physical_search_spec_sha256': physical_search_spec.search_spec_sha256,
        'extended_search_spec_id': (
            None
            if extended_search_spec is None
            else extended_search_spec.extended_search_id
        ),
        'extended_search_spec_sha256': (
            None
            if extended_search_spec is None
            else extended_search_spec.extended_search_sha256
        ),
        'physical_variables': physical_variables,
        'dsp_authority': dsp_authority,
        'dsp_variables': dsp_items,
        'objectives': tuple(objectives),
        'hard_constraints': constraint_refs,
        'robustness': robustness,
        'evaluator': evaluator,
        'candidate_budget': candidate_budget,
        'sampling_rule': JointCandidateSamplingRule(),
    }
    provisional = JointOptimizationSpec.model_construct(
        **payload,
        semantic_sha256='0' * 64,
    )
    return JointOptimizationSpec(
        **payload,
        semantic_sha256=canonical_joint_sha256(provisional.semantic_payload()),
    )


class JointDecisionValue(BaseModel):
    model_config = ConfigDict(frozen=True)

    domain: Literal['physical', 'dsp']
    variable_id: str = Field(min_length=1)
    value: float | int | str

    @model_validator(mode='after')
    def finite_value(self) -> 'JointDecisionValue':
        if isinstance(self.value, (float, int)):
            if not isfinite(float(self.value)):
                raise ValueError('joint decision values must be finite')
        elif not self.value:
            raise ValueError('categorical joint decision value must not be empty')
        return self


class JointCalibrationCandidateRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    plan_id: str = Field(min_length=1)
    plan_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    support_state: Literal['SUPPORTED', 'UNSUPPORTED']
    measurement_quality_report_id: str = Field(min_length=1)
    measurement_quality_report_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class JointObjectiveVectorRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    evaluation_binding_id: str = Field(min_length=1)
    evaluation_binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    objective_vector_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class JointCandidate(BaseModel):
    """Immutable composition of exact physical and DSP references."""

    model_config = ConfigDict(frozen=True)

    authority_version: Literal[
        'issue174-joint-candidate-1'
    ] = JOINT_CANDIDATE_AUTHORITY_VERSION
    candidate_id: str = Field(min_length=1)
    candidate_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    parent_spec_id: str = Field(min_length=1)
    parent_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    physical_system_variant_id: str = Field(min_length=1)
    physical_system_variant_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    calibration_candidate: JointCalibrationCandidateRef | None = None
    decision_vector: tuple[JointDecisionValue, ...] = Field(min_length=1)
    candidate_class: JointCandidateClass
    eligibility_state: JointEligibilityState
    blocked_reasons: tuple[str, ...] = ()
    evaluator: JointEvaluatorIdentity
    production_eligibility: Literal[
        'PRODUCTION_ELIGIBLE',
        'PRODUCTION_INELIGIBLE',
    ]
    objective_vector_ref: JointObjectiveVectorRef | None = None

    @model_validator(mode='after')
    def valid_candidate(self) -> 'JointCandidate':
        keys = [(item.domain, item.variable_id) for item in self.decision_vector]
        if len(keys) != len(set(keys)):
            raise ValueError('joint decision vector variable IDs must be unique')
        if keys != sorted(keys):
            raise ValueError('joint decision vector must use canonical ordering')
        physical = any(item.domain == 'physical' for item in self.decision_vector)
        dsp = any(item.domain == 'dsp' for item in self.decision_vector)
        expected_class: JointCandidateClass = (
            'joint' if physical and dsp
            else 'position_only' if physical
            else 'dsp_only'
        )
        if self.candidate_class != expected_class:
            raise ValueError('JointCandidate class does not match decision vector')
        if dsp != (self.calibration_candidate is not None):
            raise ValueError(
                'DSP decision vector requires exact CalibrationPlan candidate reference'
            )
        if self.eligibility_state == 'ELIGIBLE' and self.blocked_reasons:
            raise ValueError('eligible JointCandidate cannot carry blocked reasons')
        if self.eligibility_state == 'BLOCKED' and not self.blocked_reasons:
            raise ValueError('blocked JointCandidate requires explicit reasons')
        expected_production = (
            'PRODUCTION_ELIGIBLE'
            if self.evaluator.production_eligible
            else 'PRODUCTION_INELIGIBLE'
        )
        if self.production_eligibility != expected_production:
            raise ValueError('candidate production eligibility must match evaluator authority')
        if self.candidate_sha256 != canonical_joint_sha256(self.semantic_payload()):
            raise ValueError('JointCandidate semantic hash mismatch')
        if self.candidate_id != f'joint-candidate-{self.candidate_sha256[:24]}':
            raise ValueError('JointCandidate deterministic ID mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'parent_spec_id': self.parent_spec_id,
            'parent_spec_sha256': self.parent_spec_sha256,
            'physical_system_variant_id': self.physical_system_variant_id,
            'physical_system_variant_sha256': self.physical_system_variant_sha256,
            'calibration_candidate': (
                None
                if self.calibration_candidate is None
                else self.calibration_candidate.model_dump(mode='json')
            ),
            'decision_vector': [
                item.model_dump(mode='json') for item in self.decision_vector
            ],
            'candidate_class': self.candidate_class,
            'eligibility_state': self.eligibility_state,
            'blocked_reasons': list(self.blocked_reasons),
            'evaluator': self.evaluator.model_dump(mode='json'),
            'production_eligibility': self.production_eligibility,
            'objective_vector_ref': (
                None
                if self.objective_vector_ref is None
                else self.objective_vector_ref.model_dump(mode='json')
            ),
        }


def _validate_numeric_grid(
    *,
    value: float,
    minimum: float,
    maximum: float,
    step: float | None,
    variable_id: str,
) -> None:
    if value < minimum - 1e-12 or value > maximum + 1e-12:
        raise ValueError(f'{variable_id} is outside declared decision bounds')
    if step is None:
        return
    scaled = (value - minimum) / step
    if abs(scaled - round(scaled)) > 1e-9:
        raise ValueError(f'{variable_id} is not aligned to declared decision step')


def _validate_decision_vector(
    spec: JointOptimizationSpec,
    decisions: Sequence[JointDecisionValue],
) -> tuple[JointDecisionValue, ...]:
    ordered = tuple(sorted(decisions, key=lambda item: (item.domain, item.variable_id)))
    if not ordered:
        raise ValueError('JointCandidate requires at least one explicit decision value')
    keys = [(item.domain, item.variable_id) for item in ordered]
    if len(keys) != len(set(keys)):
        raise ValueError('JointCandidate decision values must be unique')

    physical_by_id = {item.variable_id: item for item in spec.physical_variables}
    dsp_by_id = {item.variable_id: item for item in spec.dsp_variables}
    for item in ordered:
        if item.domain == 'physical':
            definition = physical_by_id.get(item.variable_id)
            if definition is None:
                raise ValueError(
                    f'unknown physical decision variable: {item.variable_id}'
                )
            if not isinstance(item.value, (float, int)):
                raise ValueError('physical decision values must be numeric')
            _validate_numeric_grid(
                value=float(item.value),
                minimum=definition.minimum,
                maximum=definition.maximum,
                step=definition.step,
                variable_id=item.variable_id,
            )
        else:
            definition = dsp_by_id.get(item.variable_id)
            if definition is None:
                raise ValueError(f'unknown DSP decision variable: {item.variable_id}')
            if definition.parameter == 'polarity':
                if item.value not in definition.allowed_values:
                    raise ValueError(
                        f'{item.variable_id} is outside declared categorical domain'
                    )
            else:
                if not isinstance(item.value, (float, int)):
                    raise ValueError('numeric DSP decision values must be numeric')
                assert definition.minimum is not None
                assert definition.maximum is not None
                _validate_numeric_grid(
                    value=float(item.value),
                    minimum=definition.minimum,
                    maximum=definition.maximum,
                    step=definition.step,
                    variable_id=item.variable_id,
                )
    return ordered


def _aligned(value: float, resolution: float | None) -> bool:
    if resolution is None:
        return True
    scaled = float(value) / float(resolution)
    return abs(scaled - round(scaled)) <= 1e-9


def _device_resolution_reasons(plan: CadCalibrationPlan) -> tuple[str, ...]:
    constraints = plan.device_constraints
    reasons: list[str] = []
    for channel in plan.channels:
        if not _aligned(channel.gain_db, constraints.channel_gain_resolution_db):
            reasons.append(
                f'channel {channel.channel_id} gain is not aligned to device gain resolution'
            )
        if not _aligned(channel.delay_s, constraints.delay_resolution_s):
            reasons.append(
                f'channel {channel.channel_id} delay is not aligned to device delay resolution'
            )
        for item in channel.peq:
            if not _aligned(item.frequency_hz, constraints.frequency_resolution_hz):
                reasons.append(
                    f'filter {item.filter_id} frequency is not aligned to device resolution'
                )
            if not _aligned(item.q, constraints.q_resolution):
                reasons.append(
                    f'filter {item.filter_id} Q is not aligned to device resolution'
                )
            if not _aligned(item.gain_db, constraints.filter_gain_resolution_db):
                reasons.append(
                    f'filter {item.filter_id} gain is not aligned to device resolution'
                )
        for crossover in channel.crossovers:
            if not _aligned(
                crossover.frequency_hz,
                constraints.frequency_resolution_hz,
            ):
                reasons.append(
                    f'channel {channel.channel_id} crossover frequency is not aligned '
                    'to device resolution'
                )
    return tuple(dict.fromkeys(reasons))


def _decision_capability_reasons(
    *,
    spec: JointOptimizationSpec,
    decisions: Sequence[JointDecisionValue],
    quality_report: CadMeasurementQualityReport,
) -> tuple[str, ...]:
    dsp_variables = {item.variable_id: item for item in spec.dsp_variables}
    reasons: list[str] = []
    for decision in decisions:
        if decision.domain != 'dsp':
            continue
        variable = dsp_variables[decision.variable_id]
        result = gate_measurement_claim(
            quality_report,
            variable.required_measurement_claim,
            required_band_hz=(
                variable.required_band_hz
                if variable.required_measurement_claim == 'magnitude_response'
                else None
            ),
        )
        if result.decision != 'ALLOWED':
            reasons.append(
                f'{variable.variable_id}: {variable.required_measurement_claim}: '
                f'{result.decision}: ' + '; '.join(result.reasons)
            )
    return tuple(dict.fromkeys(reasons))


def _validate_dsp_candidate_authority(
    *,
    spec: JointOptimizationSpec,
    physical_variant: SystemVariant,
    calibration_plan: CadCalibrationPlan,
    quality_report: CadMeasurementQualityReport,
) -> tuple[str, ...]:
    authority = spec.dsp_authority
    if authority is None:
        raise ValueError('JointOptimizationSpec has no DSP authority')
    if (
        calibration_plan.document_id != spec.document_id
        or calibration_plan.scene_revision_id != spec.scene_revision_id
        or calibration_plan.scene_content_hash != spec.scene_content_hash
    ):
        raise ValueError('CalibrationPlan candidate SceneRevision authority mismatch')
    if (
        calibration_plan.system_variant_id != physical_variant.variant_id
        or calibration_plan.system_variant_sha256 != physical_variant.variant_sha256
    ):
        raise ValueError(
            'CalibrationPlan candidate must bind the exact physical SystemVariant'
        )
    if (
        calibration_plan.source_measurement_id != authority.source_measurement_id
        or calibration_plan.source_measurement_sha256
        != authority.source_measurement_sha256
        or calibration_plan.source_dataset_id != authority.source_dataset_id
        or calibration_plan.source_dataset_sha256 != authority.source_dataset_sha256
        or calibration_plan.measurement_quality_report_id
        != authority.measurement_quality_report_id
        or calibration_plan.measurement_quality_report_sha256
        != authority.measurement_quality_report_sha256
    ):
        raise ValueError(
            'CalibrationPlan candidate must reuse the exact DSP measurement authority'
        )
    if (
        quality_report.report_id != authority.measurement_quality_report_id
        or quality_report.report_sha256
        != authority.measurement_quality_report_sha256
    ):
        raise ValueError('candidate MeasurementQualityReport authority mismatch')
    if (
        device_capability_sha256(calibration_plan.device_constraints)
        != authority.device_capability_sha256
        or calibration_plan.device_constraints.capability_id
        != authority.device_capability_id
        or calibration_plan.device_constraints.capability_version
        != authority.device_capability_version
    ):
        raise ValueError('candidate device capability authority mismatch')

    support_state, support_reasons = evaluate_calibration_support(
        quality_report=quality_report,
        sample_rate_hz=calibration_plan.sample_rate_hz,
        channels=calibration_plan.channels,
        target_curve=calibration_plan.target_curve,
        max_boost_db=calibration_plan.max_boost_db,
        max_cut_db=calibration_plan.max_cut_db,
        device_constraints=calibration_plan.device_constraints,
    )
    if (
        support_state != calibration_plan.support_state
        or support_reasons != calibration_plan.unsupported_reasons
    ):
        raise ValueError(
            'CalibrationPlan candidate support state is not canonical'
        )

    reasons = list(support_reasons)
    reasons.extend(_device_resolution_reasons(calibration_plan))
    if routing_snapshot_sha256(calibration_plan) != authority.routing_snapshot_sha256:
        reasons.append(
            'routing/output mapping rewrite is forbidden in Issue #174 initial slice'
        )
    return tuple(dict.fromkeys(reasons))


def build_joint_candidate(
    *,
    spec: JointOptimizationSpec,
    physical_system_variant: SystemVariant,
    decisions: Sequence[JointDecisionValue],
    calibration_plan: CadCalibrationPlan | None = None,
    measurement_quality_report: CadMeasurementQualityReport | None = None,
) -> JointCandidate:
    if (
        physical_system_variant.document_id != spec.document_id
        or physical_system_variant.baseline_revision_id != spec.scene_revision_id
        or physical_system_variant.baseline_content_hash != spec.scene_content_hash
    ):
        raise ValueError(
            'physical SystemVariant must derive from the exact JointOptimizationSpec baseline'
        )

    ordered = _validate_decision_vector(spec, decisions)
    has_physical = any(item.domain == 'physical' for item in ordered)
    has_dsp = any(item.domain == 'dsp' for item in ordered)
    is_base_variant = (
        physical_system_variant.variant_id == spec.base_system_variant_id
        and physical_system_variant.variant_sha256
        == spec.base_system_variant_sha256
    )
    if has_physical and is_base_variant:
        raise ValueError('physical decision candidate cannot reuse unchanged base SystemVariant')
    if not has_physical and not is_base_variant:
        raise ValueError('DSP-only candidate must reuse the exact base SystemVariant')

    candidate_class: JointCandidateClass = (
        'joint' if has_physical and has_dsp
        else 'position_only' if has_physical
        else 'dsp_only'
    )

    blocked_reasons: tuple[str, ...] = ()
    calibration_ref: JointCalibrationCandidateRef | None = None
    if has_dsp:
        if calibration_plan is None or measurement_quality_report is None:
            raise ValueError(
                'DSP/joint candidate requires CalibrationPlan and MeasurementQualityReport'
            )
        assert spec.dsp_authority is not None
        if (
            calibration_plan.plan_id
            == spec.dsp_authority.base_calibration_plan_id
            and calibration_plan.plan_semantic_sha256
            == spec.dsp_authority.base_calibration_plan_sha256
        ):
            raise ValueError('DSP decision candidate cannot reuse unchanged base CalibrationPlan')
        support_reasons = list(_validate_dsp_candidate_authority(
            spec=spec,
            physical_variant=physical_system_variant,
            calibration_plan=calibration_plan,
            quality_report=measurement_quality_report,
        ))
        support_reasons.extend(_decision_capability_reasons(
            spec=spec,
            decisions=ordered,
            quality_report=measurement_quality_report,
        ))
        blocked_reasons = tuple(dict.fromkeys(support_reasons))
        calibration_ref = JointCalibrationCandidateRef(
            plan_id=calibration_plan.plan_id,
            plan_semantic_sha256=calibration_plan.plan_semantic_sha256,
            support_state=calibration_plan.support_state,
            measurement_quality_report_id=calibration_plan.measurement_quality_report_id,
            measurement_quality_report_sha256=(
                calibration_plan.measurement_quality_report_sha256
            ),
        )
    elif calibration_plan is not None or measurement_quality_report is not None:
        raise ValueError(
            'position-only candidate must not attach a DSP candidate authority'
        )

    eligibility: JointEligibilityState = (
        'BLOCKED' if blocked_reasons else 'ELIGIBLE'
    )
    production_eligibility = (
        'PRODUCTION_ELIGIBLE'
        if spec.evaluator.production_eligible
        else 'PRODUCTION_INELIGIBLE'
    )
    semantic_payload = {
        'authority_version': JOINT_CANDIDATE_AUTHORITY_VERSION,
        'parent_spec_id': spec.spec_id,
        'parent_spec_sha256': spec.semantic_sha256,
        'physical_system_variant_id': physical_system_variant.variant_id,
        'physical_system_variant_sha256': physical_system_variant.variant_sha256,
        'calibration_candidate': (
            None if calibration_ref is None else calibration_ref.model_dump(mode='json')
        ),
        'decision_vector': [
            item.model_dump(mode='json') for item in ordered
        ],
        'candidate_class': candidate_class,
        'eligibility_state': eligibility,
        'blocked_reasons': list(blocked_reasons),
        'evaluator': spec.evaluator.model_dump(mode='json'),
        'production_eligibility': production_eligibility,
        'objective_vector_ref': None,
    }
    digest = canonical_joint_sha256(semantic_payload)
    return JointCandidate(
        candidate_id=f'joint-candidate-{digest[:24]}',
        candidate_sha256=digest,
        parent_spec_id=spec.spec_id,
        parent_spec_sha256=spec.semantic_sha256,
        physical_system_variant_id=physical_system_variant.variant_id,
        physical_system_variant_sha256=physical_system_variant.variant_sha256,
        calibration_candidate=calibration_ref,
        decision_vector=ordered,
        candidate_class=candidate_class,
        eligibility_state=eligibility,
        blocked_reasons=blocked_reasons,
        evaluator=spec.evaluator,
        production_eligibility=production_eligibility,
        objective_vector_ref=None,
    )


_MATERIALIZED_VALUE_TOLERANCE = 1e-9


def _wrapped_yaw_delta_deg(target_deg: float, actual_deg: float) -> float:
    return (float(target_deg) - float(actual_deg) + 180.0) % 360.0 - 180.0


def _materialized_physical_value(
    variable: JointPhysicalVariableRef,
    document: SceneDocument,
) -> float:
    """Derive one physical decision value from the materialized scene."""

    try:
        entity = document.entity(variable.entity_id)
    except KeyError as exc:
        raise ValueError(
            f'physical decision variable {variable.variable_id} references an '
            'entity missing from the materialized SystemVariant scene'
        ) from exc
    if variable.parameter == 'x_m':
        return float(entity.position.x_m)
    if variable.parameter == 'y_m':
        return float(entity.position.y_m)
    if variable.parameter == 'z_m':
        return float(entity.position.z_m)
    if variable.parameter == 'aim_yaw_deg':
        if entity.aim_xyz is None:
            raise ValueError(
                f'physical decision variable {variable.variable_id} requires an '
                'explicit speaker aim in the materialized SystemVariant scene'
            )
        return float(aim_horizontal_yaw_deg(entity.aim_xyz))
    if variable.parameter == 'aim_pitch_deg':
        if entity.aim_xyz is None:
            raise ValueError(
                f'physical decision variable {variable.variable_id} requires an '
                'explicit speaker aim in the materialized SystemVariant scene'
            )
        return float(aim_pitch_deg(entity.aim_xyz))
    if variable.parameter == 'body_yaw_deg':
        return float(body_horizontal_yaw_deg(entity))
    raise ValueError(
        f'unsupported physical decision parameter: {variable.parameter}'
    )


def _materialized_dsp_value(
    variable: JointDspVariable,
    plan: CadCalibrationPlan,
) -> float | str:
    """Derive one DSP decision value from the exact candidate CalibrationPlan."""

    channel = next(
        (item for item in plan.channels if item.channel_id == variable.channel_id),
        None,
    )
    if channel is None:
        raise ValueError(
            f'DSP decision variable {variable.variable_id} references channel '
            f'{variable.channel_id} missing from the candidate CalibrationPlan'
        )
    if variable.parameter == 'gain_db':
        return float(channel.gain_db)
    if variable.parameter == 'delay_s':
        return float(channel.delay_s)
    if variable.parameter == 'polarity':
        return channel.polarity
    if variable.parameter.startswith('peq_'):
        peq = next(
            (item for item in channel.peq if item.filter_id == variable.filter_id),
            None,
        )
        if peq is None:
            raise ValueError(
                f'DSP decision variable {variable.variable_id} references filter '
                f'{variable.filter_id} missing from the candidate CalibrationPlan'
            )
        if variable.parameter == 'peq_frequency_hz':
            return float(peq.frequency_hz)
        if variable.parameter == 'peq_q':
            return float(peq.q)
        if variable.parameter == 'peq_gain_db':
            return float(peq.gain_db)
        raise ValueError(
            f'unsupported DSP decision parameter: {variable.parameter}'
        )
    if variable.parameter.startswith('crossover_'):
        index = variable.crossover_index
        if index is None or index >= len(channel.crossovers):
            raise ValueError(
                f'DSP decision variable {variable.variable_id} references a '
                'crossover index missing from the candidate CalibrationPlan'
            )
        crossover = channel.crossovers[index]
        if variable.parameter == 'crossover_frequency_hz':
            return float(crossover.frequency_hz)
        if variable.parameter == 'crossover_order':
            return float(crossover.filter_order)
    raise ValueError(f'unsupported DSP decision parameter: {variable.parameter}')


def require_joint_decision_materialization(
    *,
    spec: JointOptimizationSpec,
    baseline: SceneRevision,
    physical_system_variant: SystemVariant,
    calibration_plan: CadCalibrationPlan | None,
    decisions: Sequence[JointDecisionValue],
) -> None:
    """Re-derive every submitted decision value from exact materialized authorities.

    A persisted JointCandidate must not merely carry in-range values: each
    physical decision must equal the entity state the referenced SystemVariant
    actually materializes over the baseline SceneRevision, and each DSP
    decision must equal the gain/PEQ/crossover/delay/polarity setting in the
    exact candidate CalibrationPlan. Unknown variables, unresolvable
    materialization targets, and value mismatches all fail closed.
    """

    physical_by_id = {item.variable_id: item for item in spec.physical_variables}
    dsp_by_id = {item.variable_id: item for item in spec.dsp_variables}
    materialized: SceneDocument | None = None
    for decision in decisions:
        if decision.domain == 'physical':
            variable = physical_by_id.get(decision.variable_id)
            if variable is None:
                raise ValueError(
                    f'unknown physical decision variable: {decision.variable_id}'
                )
            if not isinstance(decision.value, (float, int)):
                raise ValueError('physical decision values must be numeric')
            if materialized is None:
                materialized = materialize_system_variant(
                    baseline,
                    physical_system_variant,
                )
            actual = _materialized_physical_value(variable, materialized)
            if variable.unit == 'deg':
                delta = abs(_wrapped_yaw_delta_deg(float(decision.value), actual))
            else:
                delta = abs(float(decision.value) - actual)
            if delta > _MATERIALIZED_VALUE_TOLERANCE:
                raise ValueError(
                    f'{decision.variable_id} does not match the materialized '
                    'SystemVariant scene'
                )
            continue
        variable = dsp_by_id.get(decision.variable_id)
        if variable is None:
            raise ValueError(
                f'unknown DSP decision variable: {decision.variable_id}'
            )
        if calibration_plan is None:
            raise ValueError(
                'DSP decision values require an exact candidate CalibrationPlan'
            )
        actual = _materialized_dsp_value(variable, calibration_plan)
        if variable.parameter == 'polarity':
            if str(decision.value) != actual:
                raise ValueError(
                    f'{decision.variable_id} does not match the candidate '
                    'CalibrationPlan materialization'
                )
            continue
        if not isinstance(decision.value, (float, int)):
            raise ValueError('numeric DSP decision values must be numeric')
        if (
            abs(float(decision.value) - float(actual))
            > _MATERIALIZED_VALUE_TOLERANCE
        ):
            raise ValueError(
                f'{decision.variable_id} does not match the candidate '
                'CalibrationPlan materialization'
            )


class JointEvaluationInputRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    evidence_class: Literal['measured', 'derived', 'predicted', 'hypothesis']
    source_kind: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class JointCandidateEvaluationBinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    authority_version: Literal[
        'issue174-joint-evaluation-1'
    ] = JOINT_EVALUATION_AUTHORITY_VERSION
    evaluation_binding_id: str = Field(min_length=1)
    evaluation_binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    parent_spec_id: str = Field(min_length=1)
    parent_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_id: str = Field(min_length=1)
    candidate_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    evaluator: JointEvaluatorIdentity
    input_refs: tuple[JointEvaluationInputRef, ...] = Field(min_length=1)
    objective_vector: ObjectiveVector
    objective_vector_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    created_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_binding(self) -> 'JointCandidateEvaluationBinding':
        if self.objective_vector.candidate_id != self.candidate_id:
            raise ValueError('joint objective vector candidate identity mismatch')
        keys = [
            (item.evidence_class, item.source_kind, item.source_id, item.source_sha256)
            for item in self.input_refs
        ]
        if len(keys) != len(set(keys)):
            raise ValueError('joint evaluation input references must be unique')
        if self.objective_vector_sha256 != canonical_joint_sha256(
            self.objective_vector.identity_payload()
        ):
            raise ValueError('joint objective vector hash mismatch')
        if self.evaluation_binding_sha256 != canonical_joint_sha256(
            self.semantic_payload()
        ):
            raise ValueError('joint evaluation binding hash mismatch')
        if self.evaluation_binding_id != (
            f'joint-evaluation-{self.evaluation_binding_sha256[:24]}'
        ):
            raise ValueError('joint evaluation deterministic ID mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'parent_spec_id': self.parent_spec_id,
            'parent_spec_sha256': self.parent_spec_sha256,
            'candidate_id': self.candidate_id,
            'candidate_sha256': self.candidate_sha256,
            'evaluator': self.evaluator.model_dump(mode='json'),
            'input_refs': [item.model_dump(mode='json') for item in self.input_refs],
            'objective_vector': self.objective_vector.identity_payload(),
            'created_at_utc': self.created_at_utc,
        }

    def objective_ref(self) -> JointObjectiveVectorRef:
        return JointObjectiveVectorRef(
            evaluation_binding_id=self.evaluation_binding_id,
            evaluation_binding_sha256=self.evaluation_binding_sha256,
            objective_vector_sha256=self.objective_vector_sha256,
        )


def unsupported_joint_objective_vector(
    *,
    spec: JointOptimizationSpec,
    candidate: JointCandidate,
) -> ObjectiveVector:
    """Explicit no-fake-prediction representation when a numeric evaluator is absent."""

    return ObjectiveVector(
        candidate_id=candidate.candidate_id,
        metrics=tuple(
            ObjectiveMetric(
                objective_id=definition.objective_id,
                value=None,
                unit=definition.unit,
                direction=definition.direction,
                state='unsupported',
                definition=definition,
            )
            for definition in spec.objectives
        ),
    )


def bind_joint_candidate_evaluation(
    *,
    spec: JointOptimizationSpec,
    candidate: JointCandidate,
    objective_vector: ObjectiveVector,
    input_refs: Sequence[JointEvaluationInputRef],
    created_at_utc: str,
) -> JointCandidateEvaluationBinding:
    if (
        candidate.parent_spec_id != spec.spec_id
        or candidate.parent_spec_sha256 != spec.semantic_sha256
    ):
        raise ValueError('JointCandidate does not belong to JointOptimizationSpec')
    if candidate.evaluator != spec.evaluator:
        raise ValueError('JointCandidate evaluator authority mismatch')
    if objective_vector.candidate_id != candidate.candidate_id:
        raise ValueError('objective vector candidate identity mismatch')

    expected_ids = tuple(item.objective_id for item in spec.objectives)
    actual_ids = tuple(item.objective_id for item in objective_vector.metrics)
    if actual_ids != expected_ids:
        raise ValueError(
            'objective vector must use the exact JointOptimizationSpec objective ordering'
        )
    for definition, metric in zip(
        spec.objectives,
        objective_vector.metrics,
        strict=True,
    ):
        if metric.definition is None:
            raise ValueError(
                'joint objective metrics require explicit ObjectiveDefinition authority'
            )
        if metric.definition.definition_id != definition.definition_id:
            raise ValueError(
                f'objective {definition.objective_id} definition/model/fidelity mismatch'
            )

    refs = tuple(sorted(
        input_refs,
        key=lambda item: (
            item.evidence_class,
            item.source_kind,
            item.source_id,
            item.source_sha256,
        ),
    ))
    if not refs:
        raise ValueError('joint objective evaluation requires exact input provenance')

    objective_sha = canonical_joint_sha256(objective_vector.identity_payload())
    payload = {
        'authority_version': JOINT_EVALUATION_AUTHORITY_VERSION,
        'parent_spec_id': spec.spec_id,
        'parent_spec_sha256': spec.semantic_sha256,
        'candidate_id': candidate.candidate_id,
        'candidate_sha256': candidate.candidate_sha256,
        'evaluator': spec.evaluator.model_dump(mode='json'),
        'input_refs': [item.model_dump(mode='json') for item in refs],
        'objective_vector': objective_vector.identity_payload(),
        'created_at_utc': created_at_utc,
    }
    digest = canonical_joint_sha256(payload)
    return JointCandidateEvaluationBinding(
        evaluation_binding_id=f'joint-evaluation-{digest[:24]}',
        evaluation_binding_sha256=digest,
        parent_spec_id=spec.spec_id,
        parent_spec_sha256=spec.semantic_sha256,
        candidate_id=candidate.candidate_id,
        candidate_sha256=candidate.candidate_sha256,
        evaluator=spec.evaluator,
        input_refs=refs,
        objective_vector=objective_vector,
        objective_vector_sha256=objective_sha,
        created_at_utc=created_at_utc,
    )


def joint_pareto_front(
    evaluations: Sequence[JointCandidateEvaluationBinding],
    candidates: Sequence[JointCandidate],
    objective_ids: Sequence[str] | None = None,
) -> ParetoResult:
    """Reuse existing direction-aware Pareto only across compatible authority."""

    evaluation_items = tuple(evaluations)
    if not evaluation_items:
        raise ParetoEmptyError('joint Pareto requires at least one evaluation')
    candidate_by_id = {item.candidate_id: item for item in candidates}
    if len(candidate_by_id) != len(tuple(candidates)):
        raise ValueError('joint Pareto candidate IDs must be unique')

    reference = evaluation_items[0]
    vectors: list[ObjectiveVector] = []
    for evaluation in evaluation_items:
        if (
            evaluation.parent_spec_id != reference.parent_spec_id
            or evaluation.parent_spec_sha256 != reference.parent_spec_sha256
        ):
            raise ValueError(
                'joint Pareto refuses incompatible optimization spec/model/fidelity authority'
            )
        if evaluation.evaluator != reference.evaluator:
            raise ValueError(
                'joint Pareto refuses incompatible evaluator/model/fidelity authority'
            )
        candidate = candidate_by_id.get(evaluation.candidate_id)
        if candidate is None:
            raise ValueError('joint Pareto evaluation references missing candidate')
        if candidate.candidate_sha256 != evaluation.candidate_sha256:
            raise ValueError('joint Pareto candidate hash mismatch')
        if candidate.eligibility_state != 'ELIGIBLE':
            raise ValueError('blocked JointCandidate is not Pareto comparison eligible')
        vector = evaluation.objective_vector
        # A binding whose vector is entirely unevaluated (e.g. the canonical
        # unsupported record) carries no comparable evidence: it must not
        # poison the front, while a partially populated vector still fails
        # closed inside pareto_front.
        if any(
            metric.state == 'available' and metric.value is not None
            for metric in vector.metrics
        ):
            vectors.append(vector)
    return pareto_front(vectors, objective_ids)


class JointCandidateSelection(BaseModel):
    """Selection reference only; never applies SceneRevision or exports DSP."""

    model_config = ConfigDict(frozen=True)

    authority_version: Literal[
        'issue174-joint-selection-1'
    ] = JOINT_SELECTION_AUTHORITY_VERSION
    selection_id: str = Field(min_length=1)
    parent_spec_id: str = Field(min_length=1)
    parent_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_id: str = Field(min_length=1)
    candidate_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    evaluation_binding_id: str | None = Field(default=None, min_length=1)
    evaluation_binding_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    state: Literal['selected_only'] = 'selected_only'
    scene_application_state: Literal['not_applied'] = 'not_applied'
    dsp_export_state: Literal['not_exported'] = 'not_exported'
    selected_at_utc: str = Field(min_length=1)
    selection_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_selection(self) -> 'JointCandidateSelection':
        if bool(self.evaluation_binding_id) != bool(self.evaluation_binding_sha256):
            raise ValueError('evaluation binding id/hash must be supplied together')
        if self.selection_sha256 != canonical_joint_sha256(self.semantic_payload()):
            raise ValueError('joint selection semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'authority_version': self.authority_version,
            'parent_spec_id': self.parent_spec_id,
            'parent_spec_sha256': self.parent_spec_sha256,
            'candidate_id': self.candidate_id,
            'candidate_sha256': self.candidate_sha256,
            'evaluation_binding_id': self.evaluation_binding_id,
            'evaluation_binding_sha256': self.evaluation_binding_sha256,
            'state': self.state,
            'scene_application_state': self.scene_application_state,
            'dsp_export_state': self.dsp_export_state,
            'selected_at_utc': self.selected_at_utc,
        }


def build_joint_candidate_selection(
    *,
    spec: JointOptimizationSpec,
    candidate: JointCandidate,
    evaluation: JointCandidateEvaluationBinding | None,
    selected_at_utc: str,
    selection_id: str | None = None,
) -> JointCandidateSelection:
    if (
        candidate.parent_spec_id != spec.spec_id
        or candidate.parent_spec_sha256 != spec.semantic_sha256
    ):
        raise ValueError('selected candidate does not belong to JointOptimizationSpec')
    if evaluation is not None:
        if (
            evaluation.candidate_id != candidate.candidate_id
            or evaluation.candidate_sha256 != candidate.candidate_sha256
            or evaluation.parent_spec_sha256 != spec.semantic_sha256
        ):
            raise ValueError('selection evaluation binding mismatch')

    payload = {
        'authority_version': JOINT_SELECTION_AUTHORITY_VERSION,
        'parent_spec_id': spec.spec_id,
        'parent_spec_sha256': spec.semantic_sha256,
        'candidate_id': candidate.candidate_id,
        'candidate_sha256': candidate.candidate_sha256,
        'evaluation_binding_id': (
            None if evaluation is None else evaluation.evaluation_binding_id
        ),
        'evaluation_binding_sha256': (
            None if evaluation is None else evaluation.evaluation_binding_sha256
        ),
        'state': 'selected_only',
        'scene_application_state': 'not_applied',
        'dsp_export_state': 'not_exported',
        'selected_at_utc': selected_at_utc,
    }
    return JointCandidateSelection(
        selection_id=selection_id or str(uuid4()),
        **payload,
        selection_sha256=canonical_joint_sha256(payload),
    )
