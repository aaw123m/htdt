from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from typing import Any, Literal, NamedTuple, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_constraint_models import CadConstraintResult, CadConstraintSet
from .cad_constraints import evaluate_cad_constraints
from .cad_orientation_constraints import orientation_constraint_rejections
from .cad_repository import SceneRepository, SceneRevision
from .cad_scene import SceneDocument, scene_content_hash
from .cad_schema import (
    ensure_native_schema,
    require_native_tables,
    connect_sqlite,

)
from .cad_system_variant import SystemVariant, materialize_system_variant
from .cad_system_variant_repository import CadSystemVariantRepository
from .cad_topology_comparison import (
    ExactAuthorityRef,
    VariantEvaluationBundle,
)
from .cad_topology_search import (
    TopologyPlacementCandidate,
    TopologyPlacementSearchSpec,
    topology_candidate_document,
)
from .cad_topology_search_repository import CadTopologySearchRepository
from .optimization_objectives import ObjectiveMetric, ObjectiveVector
from .optimization_robustness import (
    ROBUSTNESS_ALGORITHM_VERSION,
    ROBUSTNESS_MULTIDIMENSIONAL_ALGORITHM_VERSION,
    ROBUSTNESS_SCHEMA_VERSION,
    LinkedPerturbationGroup,
    LocalPerturbation,
    RobustnessEvaluation,
    UncertaintyAxis,
    _axis_value,
    apply_local_perturbation,
    build_local_stencil,
    build_robustness_evaluations,
    canonical_robustness_json,
    canonical_robustness_sha256,
)
from .optimization_robustness_multidimensional import (
    MULTIDIMENSIONAL_SAMPLING_STRATEGY,
    build_multidimensional_evaluations_from_provenance,
    build_multidimensional_sampling_plan,
)
from .clock import utc_now_iso as _utc_now


PROPOSAL_ROBUSTNESS_AUTHORITY_VERSION = 'o100f-proposal-robustness-1'
PROPOSAL_MULTIDIMENSIONAL_AUTHORITY_VERSION = (
    'o100f-proposal-robustness-multidimensional-1'
)
PROPOSAL_ROBUSTNESS_SAMPLE_AUTHORITY_VERSION = 'o100f-proposal-sample-1'


AuthorityResolver = Callable[[str], ExactAuthorityRef | None]


class VariantBundleResolver(Protocol):
    path: Path

    def get_bundle(self, bundle_id: str) -> VariantEvaluationBundle | None:
        ...


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}:{digest}'


def _validate_sha256(value: str, *, label: str) -> None:
    if len(value) != 64 or any(ch not in '0123456789abcdef' for ch in value):
        raise ValueError(f'{label} must be a lowercase SHA-256')


def _variant_provenance(variant: SystemVariant) -> dict[str, str]:
    return {item.key: item.value for item in variant.provenance}


def _selected_nominal_vector(
    bundle: VariantEvaluationBundle,
    objective_ids: Sequence[str],
    *,
    candidate_id: str | None = None,
) -> ObjectiveVector:
    metrics = tuple(bundle.objective_vector.metric(item) for item in objective_ids)
    for metric in metrics:
        metric.comparison_value()
        if metric.definition is None:
            raise ValueError(
                f'proposal robustness requires exact ObjectiveDefinition: '
                f'{metric.objective_id}'
            )
    return ObjectiveVector(
        candidate_id=bundle.variant_id if candidate_id is None else candidate_id,
        metrics=metrics,
    )


def _objective_contract_payload(
    bundle: VariantEvaluationBundle,
    objective_ids: Sequence[str],
) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for objective_id in objective_ids:
        metric = bundle.objective_vector.metric(objective_id)
        metric.comparison_value()
        if metric.definition is None:
            raise ValueError(
                f'proposal robustness requires exact ObjectiveDefinition: '
                f'{objective_id}'
            )
        source = bundle.evidence_source(objective_id)
        payload.append(
            {
                'objective_id': objective_id,
                'objective_definition': metric.definition.model_dump(mode='json'),
                'source_signature': {
                    'authority_kind': source.authority_kind,
                    'authority_version': source.authority_version,
                    'evaluator_id': source.evaluator_id,
                    'evaluator_version': source.evaluator_version,
                    'model_id': source.model_id,
                    'model_version': source.model_version,
                    'fidelity': source.fidelity,
                },
            }
        )
    return payload


def _nominal_evidence(
    bundle: VariantEvaluationBundle,
    objective_ids: Sequence[str],
) -> tuple['ProposalObjectiveEvidenceBinding', ...]:
    return tuple(
        ProposalObjectiveEvidenceBinding(
            objective_id=objective_id,
            result_ref=bundle.evidence_source(objective_id),
        )
        for objective_id in objective_ids
    )


class ProposalObjectiveEvidenceBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    objective_id: str = Field(min_length=1)
    result_ref: ExactAuthorityRef


class ProposalPerturbationObjectiveResult(BaseModel):
    """Exact perturbed objective output supplied by existing O100D evaluators."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    objective_vector: ObjectiveVector
    objective_evidence: tuple[ProposalObjectiveEvidenceBinding, ...] = Field(
        min_length=1
    )

    @model_validator(mode='after')
    def validate_evidence(self) -> 'ProposalPerturbationObjectiveResult':
        metric_ids = [item.objective_id for item in self.objective_vector.metrics]
        evidence_ids = [item.objective_id for item in self.objective_evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError('proposal objective evidence IDs must be unique')
        if metric_ids != evidence_ids:
            raise ValueError(
                'proposal objective evidence order must match objective vector'
            )
        return self


class ProposalObjectiveResultAuthority(BaseModel):
    """Replayable binding for one exact perturbed objective result.

    Resolving an ``ExactAuthorityRef`` alone proves only that a result exists;
    it does not prove that the submitted ``ObjectiveVector`` metric derives
    from that result or that the result belongs to this exact perturbed
    Scene/sample. This richer immutable result authority records the perturbed
    input identity plus the canonical metric payload so persistence can replay
    exact objective output on save and on every authoritative read.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    result_ref: ExactAuthorityRef
    robustness_spec_id: str = Field(
        pattern=r'^proposal-robustness:[0-9a-f]{64}$'
    )
    robustness_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    sample_id: str = Field(min_length=1)
    perturbed_scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    metric: ObjectiveMetric


ObjectiveResultResolver = Callable[
    [ProposalObjectiveEvidenceBinding],
    ProposalObjectiveResultAuthority | None,
]


class _ExpectedSampleEvidence(NamedTuple):
    """Deterministic perturbed-scene evidence replayed for one plan item."""

    perturbed_scene_content_hash: str
    feasible: bool
    g10_results: tuple[CadConstraintResult, ...]
    o80_rejection_ids: tuple[str, ...]
    domain_rejection_ids: tuple[str, ...]
    perturbation_failure_reason: str | None


class ProposalRobustnessSpec(BaseModel):
    """O100F local-robustness authority for an un-applied SystemVariant proposal."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = ROBUSTNESS_SCHEMA_VERSION
    authority_version: Literal[
        'o100f-proposal-robustness-1'
    ] = PROPOSAL_ROBUSTNESS_AUTHORITY_VERSION

    robustness_spec_id: str = Field(
        pattern=r'^proposal-robustness:[0-9a-f]{64}$'
    )
    robustness_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')

    template_variant_id: str = Field(min_length=1)
    template_variant_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_variant_id: str = Field(min_length=1)
    candidate_variant_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    topology_search_id: str = Field(min_length=1)
    topology_search_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    topology_candidate_id: str = Field(min_length=1)
    topology_candidate_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_set_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    materialized_scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    constraint_snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    g10_constraint_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    nominal_bundle_id: str = Field(min_length=1)
    nominal_bundle_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    objective_ids: tuple[str, ...] = Field(min_length=1)
    objective_contract_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    axes: tuple[UncertaintyAxis, ...] = Field(min_length=1)
    sampling_strategy: Literal[
        'deterministic_local_stencil'
    ] = 'deterministic_local_stencil'
    algorithm_version: Literal[
        'o90a-local-stencil-1'
    ] = ROBUSTNESS_ALGORITHM_VERSION
    software_version: str = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)

    @property
    def candidate_id(self) -> str:
        return self.candidate_variant_id

    @model_validator(mode='after')
    def validate_identity(self) -> 'ProposalRobustnessSpec':
        if len(self.objective_ids) != len(set(self.objective_ids)):
            raise ValueError('proposal robustness objective IDs must be unique')
        axis_ids = [item.axis_id for item in self.axes]
        axis_targets = [(item.entity_id, item.parameter) for item in self.axes]
        if len(axis_ids) != len(set(axis_ids)):
            raise ValueError('proposal robustness axis IDs must be unique')
        if len(axis_targets) != len(set(axis_targets)):
            raise ValueError(
                'proposal robustness axes must be unique by entity + parameter'
            )
        expected = canonical_robustness_sha256(self.semantic_payload())
        if self.robustness_spec_sha256 != expected:
            raise ValueError('ProposalRobustnessSpec semantic hash mismatch')
        if self.robustness_spec_id != _semantic_id('proposal-robustness', expected):
            raise ValueError('ProposalRobustnessSpec id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={
                'robustness_spec_id',
                'robustness_spec_sha256',
                'created_at_utc',
            },
        )


class ProposalMultidimensionalRobustnessSpec(ProposalRobustnessSpec):
    """O100F bounded multidimensional authority derived from one exact local spec."""

    authority_version: Literal[
        'o100f-proposal-robustness-multidimensional-1'
    ] = PROPOSAL_MULTIDIMENSIONAL_AUTHORITY_VERSION
    sampling_strategy: Literal[
        'deterministic_multidimensional_bounded'
    ] = MULTIDIMENSIONAL_SAMPLING_STRATEGY
    algorithm_version: Literal[
        'o90b-bounded-design-1'
    ] = ROBUSTNESS_MULTIDIMENSIONAL_ALGORITHM_VERSION
    sampling_seed: int
    sample_count: int = Field(ge=3)
    linked_groups: tuple[LinkedPerturbationGroup, ...] = ()
    parent_robustness_spec_id: str = Field(
        pattern=r'^proposal-robustness:[0-9a-f]{64}$'
    )
    parent_robustness_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def validate_multidimensional(self) -> 'ProposalMultidimensionalRobustnessSpec':
        if self.parent_robustness_spec_id == self.robustness_spec_id:
            raise ValueError('proposal multidimensional spec cannot parent itself')
        axis_ids = {item.axis_id for item in self.axes}
        linked_axis_ids: set[str] = set()
        group_ids: set[str] = set()
        for group in self.linked_groups:
            if group.group_id in group_ids:
                raise ValueError(
                    'proposal multidimensional linked group IDs must be unique'
                )
            group_ids.add(group.group_id)
            unknown = set(group.axis_multipliers) - axis_ids
            if unknown:
                raise ValueError(
                    'proposal multidimensional linked group references unknown axes: '
                    f'{sorted(unknown)}'
                )
            overlap = linked_axis_ids.intersection(group.axis_multipliers)
            if overlap:
                raise ValueError(
                    'proposal robustness axis may belong to only one linked group: '
                    f'{sorted(overlap)}'
                )
            linked_axis_ids.update(group.axis_multipliers)
        return self


ProposalRobustnessAuthority = (
    ProposalRobustnessSpec | ProposalMultidimensionalRobustnessSpec
)


def derive_proposal_multidimensional_robustness_spec(
    base_spec: ProposalRobustnessSpec,
    *,
    sample_count: int,
    seed: int,
    linked_groups: Sequence[LinkedPerturbationGroup] = (),
    created_at_utc: str | None = None,
) -> ProposalMultidimensionalRobustnessSpec:
    if type(base_spec) is not ProposalRobustnessSpec:
        raise ValueError(
            'proposal multidimensional derivation requires exact local parent spec'
        )
    if sample_count < 3:
        raise ValueError(
            'proposal multidimensional robustness requires at least three samples'
        )
    ordered_groups = tuple(sorted(linked_groups, key=lambda item: item.group_id))
    payload = base_spec.model_dump(mode='json')
    payload.update(
        {
            'authority_version': PROPOSAL_MULTIDIMENSIONAL_AUTHORITY_VERSION,
            'sampling_strategy': MULTIDIMENSIONAL_SAMPLING_STRATEGY,
            'algorithm_version': ROBUSTNESS_MULTIDIMENSIONAL_ALGORITHM_VERSION,
            'sampling_seed': int(seed),
            'sample_count': int(sample_count),
            'linked_groups': [
                item.model_dump(mode='json') for item in ordered_groups
            ],
            'parent_robustness_spec_id': base_spec.robustness_spec_id,
            'parent_robustness_spec_sha256': base_spec.robustness_spec_sha256,
            'created_at_utc': created_at_utc or _utc_now(),
        }
    )
    payload.pop('robustness_spec_id', None)
    payload.pop('robustness_spec_sha256', None)
    semantic = {
        key: value
        for key, value in payload.items()
        if key != 'created_at_utc'
    }
    digest = canonical_robustness_sha256(semantic)
    return ProposalMultidimensionalRobustnessSpec(
        **payload,
        robustness_spec_id=_semantic_id('proposal-robustness', digest),
        robustness_spec_sha256=digest,
    )


class ProposalPerturbationSample(BaseModel):
    """Append-only O90 local sample over one exact un-applied proposal."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = ROBUSTNESS_SCHEMA_VERSION
    authority_version: Literal[
        'o100f-proposal-sample-1'
    ] = PROPOSAL_ROBUSTNESS_SAMPLE_AUTHORITY_VERSION

    sample_id: str = Field(min_length=1)
    robustness_spec_id: str = Field(
        pattern=r'^proposal-robustness:[0-9a-f]{64}$'
    )
    robustness_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_id: str = Field(min_length=1)

    sample_index: int = Field(ge=0)
    axis_id: str | None = None
    step: Literal['nominal', 'minus', 'plus', 'multidimensional']
    parameter_deltas: dict[str, float]

    perturbed_scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    feasible: bool
    g10_results: tuple[CadConstraintResult, ...] = ()
    o80_rejection_ids: tuple[str, ...] = ()
    domain_rejection_ids: tuple[str, ...] = ()

    objective_contract_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    objective_evidence: tuple[ProposalObjectiveEvidenceBinding, ...] = ()
    objective_vector: ObjectiveVector | None = None
    failure_reason: str | None = None

    sample_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    created_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def validate_identity(self) -> 'ProposalPerturbationSample':
        LocalPerturbation(
            sample_id=self.sample_id,
            sample_index=self.sample_index,
            axis_id=self.axis_id,
            step=self.step,
            parameter_deltas=self.parameter_deltas,
        )
        evidence_ids = [item.objective_id for item in self.objective_evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError('proposal sample objective evidence IDs must be unique')
        if self.objective_vector is not None:
            if self.objective_vector.candidate_id != self.sample_id:
                raise ValueError(
                    'proposal sample ObjectiveVector candidate_id must equal sample_id'
                )
            metric_ids = [item.objective_id for item in self.objective_vector.metrics]
            if metric_ids != evidence_ids:
                raise ValueError(
                    'proposal sample evidence order must match objective vector'
                )
        elif self.objective_evidence:
            raise ValueError(
                'proposal sample cannot carry objective evidence without a vector'
            )
        if not self.feasible and self.objective_vector is not None:
            raise ValueError('infeasible proposal perturbations must remain unscored')
        expected = canonical_robustness_sha256(self.identity_payload())
        if self.sample_sha256 != expected:
            raise ValueError('ProposalPerturbationSample semantic hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
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
            'objective_contract_sha256': self.objective_contract_sha256,
            'objective_evidence': [
                item.model_dump(mode='json') for item in self.objective_evidence
            ],
            'objective_vector': (
                None
                if self.objective_vector is None
                else self.objective_vector.identity_payload()
            ),
            'failure_reason': self.failure_reason,
        }


def _validate_lineage(
    *,
    baseline: SceneRevision,
    template_variant: SystemVariant,
    candidate_variant: SystemVariant,
    topology_spec: TopologyPlacementSearchSpec,
    topology_candidate: TopologyPlacementCandidate,
    candidate_set_sha256: str,
) -> SceneDocument:
    _validate_sha256(candidate_set_sha256, label='candidate_set_sha256')
    if (
        template_variant.document_id != baseline.document_id
        or template_variant.baseline_revision_id != baseline.revision_id
        or template_variant.baseline_content_hash != baseline.content_hash
    ):
        raise ValueError('proposal robustness template variant baseline mismatch')
    if (
        candidate_variant.document_id != baseline.document_id
        or candidate_variant.baseline_revision_id != baseline.revision_id
        or candidate_variant.baseline_content_hash != baseline.content_hash
    ):
        raise ValueError('proposal robustness candidate variant baseline mismatch')
    if candidate_variant.parent_variant_id != template_variant.variant_id:
        raise ValueError(
            'proposal robustness candidate variant must descend from template'
        )
    if (
        topology_spec.document_id != baseline.document_id
        or topology_spec.baseline_revision_id != baseline.revision_id
        or topology_spec.baseline_content_hash != baseline.content_hash
        or topology_spec.template_variant_id != template_variant.variant_id
        or topology_spec.template_variant_sha256 != template_variant.variant_sha256
    ):
        raise ValueError('proposal robustness topology search authority mismatch')
    if (
        topology_candidate.search_id != topology_spec.search_id
        or topology_candidate.search_sha256 != topology_spec.search_sha256
        or topology_candidate.template_variant_sha256
        != topology_spec.template_variant_sha256
        or topology_candidate.topology_search_id != topology_spec.topology_search_id
        or topology_candidate.topology_search_sha256
        != topology_spec.topology_search_sha256
        or topology_candidate.topology_option_id != topology_spec.topology_option_id
    ):
        raise ValueError('proposal robustness topology candidate authority mismatch')

    provenance = _variant_provenance(candidate_variant)
    expected_provenance = {
        'o100b.topology_search_sha256': topology_spec.topology_search_sha256,
        'o100b.topology_option_id': topology_spec.topology_option_id,
        'o100b.search_sha256': topology_spec.search_sha256,
        'o100b.algorithm_version': topology_spec.algorithm_version,
        'o100b.candidate_id': topology_candidate.candidate_id,
        'o100b.candidate_sha256': topology_candidate.candidate_sha256,
    }
    for key, value in expected_provenance.items():
        if provenance.get(key) != value:
            raise ValueError(
                f'proposal robustness candidate variant provenance mismatch: {key}'
            )

    topology_scene = topology_candidate_document(
        baseline=baseline,
        template_variant=template_variant,
        spec=topology_spec,
        candidate=topology_candidate,
    )
    variant_scene = materialize_system_variant(baseline, candidate_variant)
    topology_hash = scene_content_hash(topology_scene)
    variant_hash = scene_content_hash(variant_scene)
    if topology_hash != variant_hash:
        raise ValueError(
            'proposal robustness candidate variant scene differs from O100B candidate'
        )
    return variant_scene


def build_proposal_robustness_spec(
    *,
    baseline: SceneRevision,
    template_variant: SystemVariant,
    candidate_variant: SystemVariant,
    topology_spec: TopologyPlacementSearchSpec,
    topology_candidate: TopologyPlacementCandidate,
    candidate_set_sha256: str,
    nominal_bundle: VariantEvaluationBundle,
    objective_ids: Sequence[str],
    axes: Sequence[UncertaintyAxis],
    software_version: str,
    created_at_utc: str,
) -> ProposalRobustnessSpec:
    nominal_scene = _validate_lineage(
        baseline=baseline,
        template_variant=template_variant,
        candidate_variant=candidate_variant,
        topology_spec=topology_spec,
        topology_candidate=topology_candidate,
        candidate_set_sha256=candidate_set_sha256,
    )
    if (
        nominal_bundle.variant_id != candidate_variant.variant_id
        or nominal_bundle.variant_sha256 != candidate_variant.variant_sha256
    ):
        raise ValueError(
            'proposal robustness nominal bundle SystemVariant identity mismatch'
        )

    objectives = tuple(objective_ids)
    if not objectives or len(objectives) != len(set(objectives)):
        raise ValueError(
            'proposal robustness requires unique non-empty objective IDs'
        )
    _selected_nominal_vector(nominal_bundle, objectives)
    objective_contract = _objective_contract_payload(nominal_bundle, objectives)
    contract_sha = canonical_robustness_sha256(objective_contract)

    ordered_axes = tuple(sorted(axes, key=lambda item: item.axis_id))
    if not ordered_axes:
        raise ValueError('proposal robustness requires at least one uncertainty axis')
    for axis in ordered_axes:
        actual = _axis_value(nominal_scene, axis)
        if abs(actual - float(axis.nominal_value)) > 1e-9:
            raise ValueError(
                f'proposal robustness axis {axis.axis_id} nominal value '
                f'{axis.nominal_value} does not match exact proposal value {actual}'
            )

    core = {
        'schema_version': ROBUSTNESS_SCHEMA_VERSION,
        'authority_version': PROPOSAL_ROBUSTNESS_AUTHORITY_VERSION,
        'document_id': baseline.document_id,
        'scene_revision_id': baseline.revision_id,
        'scene_content_hash': baseline.content_hash,
        'template_variant_id': template_variant.variant_id,
        'template_variant_sha256': template_variant.variant_sha256,
        'candidate_variant_id': candidate_variant.variant_id,
        'candidate_variant_sha256': candidate_variant.variant_sha256,
        'topology_search_id': topology_spec.search_id,
        'topology_search_sha256': topology_spec.search_sha256,
        'topology_candidate_id': topology_candidate.candidate_id,
        'topology_candidate_sha256': topology_candidate.candidate_sha256,
        'candidate_set_sha256': candidate_set_sha256,
        'materialized_scene_content_hash': scene_content_hash(nominal_scene),
        'constraint_snapshot_sha256': topology_spec.constraint_snapshot_sha256,
        'g10_constraint_spec_sha256': topology_spec.g10_constraint_spec_sha256,
        'nominal_bundle_id': nominal_bundle.bundle_id,
        'nominal_bundle_sha256': nominal_bundle.bundle_sha256,
        'objective_ids': list(objectives),
        'objective_contract_sha256': contract_sha,
        'axes': [item.model_dump(mode='json') for item in ordered_axes],
        'sampling_strategy': 'deterministic_local_stencil',
        'algorithm_version': ROBUSTNESS_ALGORITHM_VERSION,
        'software_version': software_version,
    }
    digest = canonical_robustness_sha256(core)
    return ProposalRobustnessSpec(
        robustness_spec_id=_semantic_id('proposal-robustness', digest),
        robustness_spec_sha256=digest,
        document_id=baseline.document_id,
        scene_revision_id=baseline.revision_id,
        scene_content_hash=baseline.content_hash,
        template_variant_id=template_variant.variant_id,
        template_variant_sha256=template_variant.variant_sha256,
        candidate_variant_id=candidate_variant.variant_id,
        candidate_variant_sha256=candidate_variant.variant_sha256,
        topology_search_id=topology_spec.search_id,
        topology_search_sha256=topology_spec.search_sha256,
        topology_candidate_id=topology_candidate.candidate_id,
        topology_candidate_sha256=topology_candidate.candidate_sha256,
        candidate_set_sha256=candidate_set_sha256,
        materialized_scene_content_hash=scene_content_hash(nominal_scene),
        constraint_snapshot_sha256=topology_spec.constraint_snapshot_sha256,
        g10_constraint_spec_sha256=topology_spec.g10_constraint_spec_sha256,
        nominal_bundle_id=nominal_bundle.bundle_id,
        nominal_bundle_sha256=nominal_bundle.bundle_sha256,
        objective_ids=objectives,
        objective_contract_sha256=contract_sha,
        axes=ordered_axes,
        sampling_strategy='deterministic_local_stencil',
        algorithm_version=ROBUSTNESS_ALGORITHM_VERSION,
        software_version=software_version,
        created_at_utc=created_at_utc,
    )


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


def _objective_signature(vector: ObjectiveVector) -> tuple[tuple[str, str, str, str], ...]:
    return tuple(
        (
            item.objective_id,
            item.definition_id,
            item.unit,
            item.direction,
        )
        for item in vector.metrics
    )


def _validate_result_contract(
    *,
    nominal_bundle: VariantEvaluationBundle,
    spec: ProposalRobustnessSpec,
    result: ProposalPerturbationObjectiveResult,
    sample_id: str,
) -> ProposalPerturbationObjectiveResult:
    if result.objective_vector.candidate_id != sample_id:
        raise ValueError(
            'proposal perturbed ObjectiveVector candidate_id must equal sample_id'
        )
    nominal = _selected_nominal_vector(nominal_bundle, spec.objective_ids)
    if _objective_signature(result.objective_vector) != _objective_signature(nominal):
        raise ValueError(
            'proposal perturbed objective schema does not match nominal bundle'
        )

    evidence_by_id = {
        item.objective_id: item.result_ref
        for item in result.objective_evidence
    }
    if tuple(evidence_by_id) != spec.objective_ids:
        raise ValueError(
            'proposal perturbed objective evidence order does not match spec'
        )
    for objective_id in spec.objective_ids:
        nominal_ref = nominal_bundle.evidence_source(objective_id)
        actual_ref = evidence_by_id[objective_id]
        signature_fields = (
            'authority_kind',
            'authority_version',
            'evaluator_id',
            'evaluator_version',
            'model_id',
            'model_version',
            'fidelity',
        )
        if any(
            getattr(actual_ref, field) != getattr(nominal_ref, field)
            for field in signature_fields
        ):
            raise ValueError(
                f'proposal perturbed objective evidence signature mismatch: '
                f'{objective_id}'
            )
    return result


def _make_proposal_sample(
    *,
    spec: ProposalRobustnessSpec,
    plan: LocalPerturbation,
    document: SceneDocument,
    constraint_set: CadConstraintSet,
    changed_entity_ids: Sequence[str],
    result: ProposalPerturbationObjectiveResult | None,
    failure_reason: str | None,
    domain_rejection_ids: tuple[str, ...],
    created_at_utc: str,
) -> ProposalPerturbationSample:
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
        result = None
        failure_reason = failure_reason or 'hard_constraint_violation'

    payload = {
        'schema_version': ROBUSTNESS_SCHEMA_VERSION,
        'authority_version': PROPOSAL_ROBUSTNESS_SAMPLE_AUTHORITY_VERSION,
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
        'objective_contract_sha256': spec.objective_contract_sha256,
        'objective_evidence': (
            []
            if result is None
            else [
                item.model_dump(mode='json')
                for item in result.objective_evidence
            ]
        ),
        'objective_vector': (
            None
            if result is None
            else result.objective_vector.identity_payload()
        ),
        'failure_reason': failure_reason,
    }
    return ProposalPerturbationSample(
        **payload,
        sample_sha256=canonical_robustness_sha256(payload),
        created_at_utc=created_at_utc,
    )


def evaluate_proposal_local_robustness(
    *,
    baseline: SceneRevision,
    template_variant: SystemVariant,
    candidate_variant: SystemVariant,
    topology_spec: TopologyPlacementSearchSpec,
    topology_candidate: TopologyPlacementCandidate,
    candidate_set_sha256: str,
    spec: ProposalRobustnessSpec,
    constraint_set: CadConstraintSet,
    nominal_bundle: VariantEvaluationBundle,
    evaluator: Callable[
        [SceneDocument, str],
        ProposalPerturbationObjectiveResult,
    ],
    created_at_utc: str | None = None,
) -> tuple[
    tuple[ProposalPerturbationSample, ...],
    tuple[RobustnessEvaluation, ...],
]:
    rebuilt = build_proposal_robustness_spec(
        baseline=baseline,
        template_variant=template_variant,
        candidate_variant=candidate_variant,
        topology_spec=topology_spec,
        topology_candidate=topology_candidate,
        candidate_set_sha256=candidate_set_sha256,
        nominal_bundle=nominal_bundle,
        objective_ids=spec.objective_ids,
        axes=spec.axes,
        software_version=spec.software_version,
        created_at_utc=spec.created_at_utc,
    )
    if rebuilt != spec:
        raise ValueError(
            'proposal robustness spec does not reproduce from exact authorities'
        )
    constraint_payload = json.loads(topology_spec.constraint_snapshot_json)
    if CadConstraintSet.model_validate(constraint_payload) != constraint_set:
        raise ValueError(
            'proposal robustness constraint set differs from O100B snapshot'
        )
    if (
        canonical_robustness_sha256(constraint_payload)
        != spec.constraint_snapshot_sha256
    ):
        raise ValueError('proposal robustness constraint snapshot hash mismatch')

    nominal_document = materialize_system_variant(baseline, candidate_variant)
    if scene_content_hash(nominal_document) != spec.materialized_scene_content_hash:
        raise ValueError('proposal robustness nominal scene hash mismatch')

    nominal_vector = _selected_nominal_vector(
        nominal_bundle,
        spec.objective_ids,
    )
    nominal_evidence = _nominal_evidence(nominal_bundle, spec.objective_ids)
    plans = build_local_stencil(spec)
    axis_by_id = {item.axis_id: item for item in spec.axes}
    timestamp = created_at_utc or _utc_now()
    samples: list[ProposalPerturbationSample] = []

    for plan in plans:
        document = nominal_document
        result: ProposalPerturbationObjectiveResult | None = None
        failure_reason: str | None = None
        domain_rejections: tuple[str, ...]
        changed_ids: tuple[str, ...]

        if plan.axis_id is None:
            changed_ids = tuple(sorted({item.entity_id for item in spec.axes}))
            domain_rejections = ()
            result = ProposalPerturbationObjectiveResult(
                objective_vector=nominal_vector.model_copy(
                    update={'candidate_id': plan.sample_id}
                ),
                objective_evidence=nominal_evidence,
            )
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
        if feasible and plan.axis_id is not None:
            try:
                result = _validate_result_contract(
                    nominal_bundle=nominal_bundle,
                    spec=spec,
                    result=evaluator(document, plan.sample_id),
                    sample_id=plan.sample_id,
                )
            except Exception as exc:
                result = None
                failure_reason = f'objective_evaluation_failed:{exc}'

        samples.append(
            _make_proposal_sample(
                spec=spec,
                plan=plan,
                document=document,
                constraint_set=constraint_set,
                changed_entity_ids=changed_ids,
                result=result,
                failure_reason=failure_reason,
                domain_rejection_ids=domain_rejections,
                created_at_utc=timestamp,
            )
        )

    evaluations = build_robustness_evaluations(
        spec,
        tuple(samples),
        created_at_utc=timestamp,
    )
    return tuple(samples), evaluations


def _proposal_multidimensional_provenance(
    spec: ProposalMultidimensionalRobustnessSpec,
    samples: Sequence[ProposalPerturbationSample],
) -> dict[str, Any]:
    return {
        'robustness_spec_id': spec.robustness_spec_id,
        'robustness_spec_sha256': spec.robustness_spec_sha256,
        'parent_robustness_spec_id': spec.parent_robustness_spec_id,
        'parent_robustness_spec_sha256': spec.parent_robustness_spec_sha256,
        'sampling_strategy': spec.sampling_strategy,
        'algorithm_version': spec.algorithm_version,
        'sampling_seed': spec.sampling_seed,
        'sample_count': spec.sample_count,
        'linked_groups': [
            item.model_dump(mode='json') for item in spec.linked_groups
        ],
        'sample_ids': [item.sample_id for item in samples],
        'candidate_variant_id': spec.candidate_variant_id,
        'candidate_variant_sha256': spec.candidate_variant_sha256,
        'topology_search_id': spec.topology_search_id,
        'topology_search_sha256': spec.topology_search_sha256,
        'topology_candidate_id': spec.topology_candidate_id,
        'topology_candidate_sha256': spec.topology_candidate_sha256,
        'candidate_set_sha256': spec.candidate_set_sha256,
        'nominal_bundle_id': spec.nominal_bundle_id,
        'nominal_bundle_sha256': spec.nominal_bundle_sha256,
        'objective_contract_sha256': spec.objective_contract_sha256,
    }


def evaluate_proposal_multidimensional_robustness(
    *,
    baseline: SceneRevision,
    template_variant: SystemVariant,
    candidate_variant: SystemVariant,
    topology_spec: TopologyPlacementSearchSpec,
    topology_candidate: TopologyPlacementCandidate,
    candidate_set_sha256: str,
    spec: ProposalMultidimensionalRobustnessSpec,
    parent_spec: ProposalRobustnessSpec,
    constraint_set: CadConstraintSet,
    nominal_bundle: VariantEvaluationBundle,
    evaluator: Callable[
        [SceneDocument, str],
        ProposalPerturbationObjectiveResult,
    ],
    created_at_utc: str | None = None,
) -> tuple[
    tuple[ProposalPerturbationSample, ...],
    tuple[RobustnessEvaluation, ...],
]:
    rebuilt_parent = build_proposal_robustness_spec(
        baseline=baseline,
        template_variant=template_variant,
        candidate_variant=candidate_variant,
        topology_spec=topology_spec,
        topology_candidate=topology_candidate,
        candidate_set_sha256=candidate_set_sha256,
        nominal_bundle=nominal_bundle,
        objective_ids=parent_spec.objective_ids,
        axes=parent_spec.axes,
        software_version=parent_spec.software_version,
        created_at_utc=parent_spec.created_at_utc,
    )
    if rebuilt_parent != parent_spec:
        raise ValueError(
            'proposal multidimensional parent does not reproduce from authorities'
        )
    rebuilt = derive_proposal_multidimensional_robustness_spec(
        parent_spec,
        sample_count=spec.sample_count,
        seed=spec.sampling_seed,
        linked_groups=spec.linked_groups,
        created_at_utc=spec.created_at_utc,
    )
    if rebuilt != spec:
        raise ValueError(
            'proposal multidimensional spec does not reproduce from parent authority'
        )

    constraint_payload = json.loads(topology_spec.constraint_snapshot_json)
    if CadConstraintSet.model_validate(constraint_payload) != constraint_set:
        raise ValueError(
            'proposal multidimensional constraint set differs from O100B snapshot'
        )
    if (
        canonical_robustness_sha256(constraint_payload)
        != spec.constraint_snapshot_sha256
    ):
        raise ValueError(
            'proposal multidimensional constraint snapshot hash mismatch'
        )

    nominal_document = materialize_system_variant(baseline, candidate_variant)
    if scene_content_hash(nominal_document) != spec.materialized_scene_content_hash:
        raise ValueError('proposal multidimensional nominal scene hash mismatch')

    nominal_vector = _selected_nominal_vector(
        nominal_bundle,
        spec.objective_ids,
    )
    nominal_evidence = _nominal_evidence(nominal_bundle, spec.objective_ids)
    axis_by_id = {item.axis_id: item for item in spec.axes}
    plans = build_multidimensional_sampling_plan(spec)
    timestamp = created_at_utc or _utc_now()
    samples: list[ProposalPerturbationSample] = []

    for plan in plans:
        document = nominal_document
        changed_ids: set[str] = set()
        domain_rejections: list[str] = []
        failure_reason: str | None = None
        result: ProposalPerturbationObjectiveResult | None = None

        if plan.step == 'nominal':
            changed_ids.update(item.entity_id for item in spec.axes)
            result = ProposalPerturbationObjectiveResult(
                objective_vector=nominal_vector.model_copy(
                    update={'candidate_id': plan.sample_id}
                ),
                objective_evidence=nominal_evidence,
            )
        else:
            for axis_id in sorted(plan.parameter_deltas):
                axis = axis_by_id[axis_id]
                delta = float(plan.parameter_deltas[axis_id])
                changed_ids.add(axis.entity_id)
                domain_rejections.extend(_domain_rejections(axis, delta))
                try:
                    document = apply_local_perturbation(document, axis, delta)
                except Exception as exc:
                    domain_rejections.append(
                        f'__perturbation_unsupported__:{axis.axis_id}'
                    )
                    failure_reason = f'perturbation_failed:{exc}'

        g10 = evaluate_cad_constraints(document, constraint_set)
        o80 = orientation_constraint_rejections(
            document,
            constraint_set,
            changed_entity_ids=tuple(sorted(changed_ids)),
        )
        feasible = (
            g10.constraints_satisfied
            and not o80
            and not domain_rejections
        )
        if feasible and plan.step != 'nominal':
            try:
                result = _validate_result_contract(
                    nominal_bundle=nominal_bundle,
                    spec=spec,
                    result=evaluator(document, plan.sample_id),
                    sample_id=plan.sample_id,
                )
            except Exception as exc:
                result = None
                failure_reason = f'objective_evaluation_failed:{exc}'

        samples.append(
            _make_proposal_sample(
                spec=spec,
                plan=plan,
                document=document,
                constraint_set=constraint_set,
                changed_entity_ids=tuple(sorted(changed_ids)),
                result=result,
                failure_reason=failure_reason,
                domain_rejection_ids=tuple(sorted(set(domain_rejections))),
                created_at_utc=timestamp,
            )
        )

    provenance = _proposal_multidimensional_provenance(spec, samples)
    evaluations = build_multidimensional_evaluations_from_provenance(
        robustness_spec_id=spec.robustness_spec_id,
        robustness_spec_sha256=spec.robustness_spec_sha256,
        candidate_id=spec.candidate_id,
        expected_sample_ids=tuple(item.sample_id for item in plans),
        samples=tuple(samples),
        sampling_provenance=provenance,
        created_at_utc=timestamp,
    )
    return tuple(samples), evaluations


class CadProposalRobustnessRepository:
    """Append-only O100F local-robustness persistence.

    Existing O90 tables and identities are intentionally not modified.

    Pydantic validity and self-hashes are necessary but not sufficient for a
    persisted O100F row. Every write and every authoritative read replays the
    canonical perturbation authority of ``evaluate_proposal_local_robustness``
    and its multidimensional equivalent:

    * ``save_spec``/``get_spec`` re-resolve the exact baseline SceneRevision,
      SystemVariants, O100B authorities and nominal VariantEvaluationBundle,
      then rebuild the spec (multidimensional rows additionally re-derive from
      their persisted local parent) and require exact equality;
    * ``save_sample``/``list_samples`` rebuild the deterministic sampling plan
      from the validated spec, require each stored row to equal the plan
      member at its ``sample_index``, rematerialize the exact proposal Scene,
      re-apply the exact perturbation, and require the persisted
      ``perturbed_scene_content_hash``, ``g10_results``,
      ``o80_rejection_ids``, ``domain_rejection_ids``, ``feasible`` and
      ``failure_reason`` to equal the replayed canonical output;
    * scored non-nominal samples additionally resolve every objective result
      ref: a typed ``objective_result_resolvers`` entry replays the richer
      ``ProposalObjectiveResultAuthority`` (exact perturbed Scene/sample
      binding plus canonical metric payload), and ``external_resolvers``
      proves exact ref identity whenever no typed resolver covers the
      authority kind; the repository's own persisted result authorities —
      recorded on save — must reproduce the same record so a ref cannot
      migrate across perturbations and an ``ObjectiveVector`` value cannot
      drift from the recorded output;
    * ``save_evaluation``/``list_evaluations`` regenerate each stored
      evaluation from the validated samples and require exact equality.

    ``schema_version``/``authority_version``/``algorithm_version`` are part of
    every spec and sample identity, are pinned to the supported literals, and
    are re-verified under the same canonical semantics on every read; unknown
    evaluator versions fail closed through the exact objective-contract
    signature instead of being silently trusted.
    """

    def __init__(
        self,
        *,
        scene_repository: SceneRepository,
        variant_repository: CadSystemVariantRepository,
        topology_repository: CadTopologySearchRepository,
        bundle_resolver: VariantBundleResolver,
        external_resolvers: Mapping[str, AuthorityResolver] | None = None,
        objective_result_resolvers: (
            Mapping[str, ObjectiveResultResolver] | None
        ) = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.variant_repository = variant_repository
        self.topology_repository = topology_repository
        self.bundle_resolver = bundle_resolver
        self.external_resolvers = dict(external_resolvers or {})
        self.objective_result_resolvers = dict(objective_result_resolvers or {})
        self.path = Path(scene_repository.path)

        repositories = (
            ('SystemVariant', variant_repository),
            ('TopologySearch', topology_repository),
            ('VariantBundle', bundle_resolver),
        )
        for label, repository in repositories:
            repo_path = Path(
                getattr(repository, 'path', getattr(repository, 'db_path', self.path))
            )
            if repo_path != self.path:
                raise ValueError(
                    f'proposal robustness and {label} repositories must share '
                    'one native CAD database'
                )
        ensure_native_schema(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        ensure_native_schema(self.path)
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_proposal_robustness_specs', 'cad_proposal_perturbation_samples', 'cad_proposal_objective_result_authorities', 'cad_proposal_robustness_evaluations')

    def _resolve_external(self, ref: ExactAuthorityRef) -> None:
        resolver = self.external_resolvers.get(ref.authority_kind)
        if resolver is None:
            raise ValueError(
                f'no proposal robustness resolver for authority_kind='
                f'{ref.authority_kind}'
            )
        resolved = resolver(ref.authority_id)
        if resolved is None:
            raise ValueError(
                f'proposal robustness external authority does not exist: '
                f'{ref.authority_kind}:{ref.authority_id}'
            )
        if resolved != ref:
            raise ValueError(
                f'proposal robustness external authority mismatch: '
                f'{ref.authority_kind}:{ref.authority_id}'
            )

    def _resolve_spec_authorities(
        self,
        spec: ProposalRobustnessSpec,
    ) -> tuple[
        SceneRevision,
        SystemVariant,
        SystemVariant,
        TopologyPlacementSearchSpec,
        TopologyPlacementCandidate,
        VariantEvaluationBundle,
    ]:
        baseline = self.scene_repository.get(spec.scene_revision_id)
        if baseline is None:
            raise ValueError('proposal robustness baseline SceneRevision disappeared')
        if (
            baseline.document_id != spec.document_id
            or baseline.content_hash != spec.scene_content_hash
        ):
            raise ValueError('proposal robustness baseline SceneRevision mismatch')

        template = self.variant_repository.get_variant(spec.template_variant_id)
        candidate_variant = self.variant_repository.get_variant(
            spec.candidate_variant_id
        )
        if template is None or candidate_variant is None:
            raise ValueError('proposal robustness SystemVariant authority disappeared')
        if (
            template.variant_sha256 != spec.template_variant_sha256
            or candidate_variant.variant_sha256 != spec.candidate_variant_sha256
        ):
            raise ValueError('proposal robustness SystemVariant hash mismatch')

        topology_spec = self.topology_repository.get_spec(spec.topology_search_id)
        candidate = self.topology_repository.get_candidate(
            spec.topology_candidate_id
        )
        if topology_spec is None or candidate is None:
            raise ValueError('proposal robustness O100B authority disappeared')
        if (
            topology_spec.search_sha256 != spec.topology_search_sha256
            or candidate.candidate_sha256 != spec.topology_candidate_sha256
        ):
            raise ValueError('proposal robustness O100B authority hash mismatch')
        comparison_ref = self.topology_repository.comparison_ref(
            candidate.candidate_id
        )
        if (
            comparison_ref.candidate_set_sha256 != spec.candidate_set_sha256
            or comparison_ref.variant_id != candidate_variant.variant_id
            or comparison_ref.variant_sha256 != candidate_variant.variant_sha256
            or comparison_ref.proposed_content_hash
            != spec.materialized_scene_content_hash
        ):
            raise ValueError(
                'proposal robustness topology comparison lineage mismatch'
            )

        bundle = self.bundle_resolver.get_bundle(spec.nominal_bundle_id)
        if bundle is None:
            raise ValueError(
                'proposal robustness nominal VariantEvaluationBundle disappeared'
            )
        if bundle.bundle_sha256 != spec.nominal_bundle_sha256:
            raise ValueError(
                'proposal robustness nominal VariantEvaluationBundle hash mismatch'
            )
        return baseline, template, candidate_variant, topology_spec, candidate, bundle

    @staticmethod
    def _parse_spec_json(payload_json: str) -> ProposalRobustnessAuthority:
        decoded = json.loads(payload_json)
        strategy = decoded.get('sampling_strategy')
        if strategy == MULTIDIMENSIONAL_SAMPLING_STRATEGY:
            return ProposalMultidimensionalRobustnessSpec.model_validate(decoded)
        return ProposalRobustnessSpec.model_validate(decoded)

    def _validate_spec(
        self,
        spec: ProposalRobustnessAuthority,
    ) -> ProposalRobustnessAuthority:
        if isinstance(spec, ProposalMultidimensionalRobustnessSpec):
            spec = ProposalMultidimensionalRobustnessSpec.model_validate(
                spec.model_dump(mode='python')
            )
        else:
            spec = ProposalRobustnessSpec.model_validate(
                spec.model_dump(mode='python')
            )

        (
            baseline,
            template,
            candidate_variant,
            topology_spec,
            candidate,
            bundle,
        ) = self._resolve_spec_authorities(spec)

        if isinstance(spec, ProposalMultidimensionalRobustnessSpec):
            parent = self.get_spec(spec.parent_robustness_spec_id)
            if type(parent) is not ProposalRobustnessSpec:
                raise ValueError(
                    'proposal multidimensional spec requires persisted local parent'
                )
            assert parent is not None
            if parent.robustness_spec_sha256 != spec.parent_robustness_spec_sha256:
                raise ValueError(
                    'proposal multidimensional parent robustness hash mismatch'
                )
            rebuilt = derive_proposal_multidimensional_robustness_spec(
                parent,
                sample_count=spec.sample_count,
                seed=spec.sampling_seed,
                linked_groups=spec.linked_groups,
                created_at_utc=spec.created_at_utc,
            )
        else:
            rebuilt = build_proposal_robustness_spec(
                baseline=baseline,
                template_variant=template,
                candidate_variant=candidate_variant,
                topology_spec=topology_spec,
                topology_candidate=candidate,
                candidate_set_sha256=spec.candidate_set_sha256,
                nominal_bundle=bundle,
                objective_ids=spec.objective_ids,
                axes=spec.axes,
                software_version=spec.software_version,
                created_at_utc=spec.created_at_utc,
            )
        if rebuilt != spec:
            raise ValueError(
                'proposal robustness spec does not reproduce from persisted authorities'
            )
        return spec

    def save_spec(
        self,
        spec: ProposalRobustnessAuthority,
    ) -> ProposalRobustnessAuthority:
        spec = self._validate_spec(spec)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_proposal_robustness_specs
                WHERE robustness_spec_id=?
                """,
                (spec.robustness_spec_id,),
            ).fetchone()
            if row is not None:
                persisted = self._parse_spec_json(row['payload_json'])
                if persisted != spec:
                    raise ValueError(
                        'ProposalRobustnessSpec id exists with different semantics'
                    )
                return self._validate_spec(persisted)
            connection.execute(
                """
                INSERT INTO cad_proposal_robustness_specs(
                    robustness_spec_id,
                    robustness_spec_sha256,
                    document_id,
                    scene_revision_id,
                    candidate_variant_id,
                    topology_candidate_id,
                    nominal_bundle_id,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    spec.robustness_spec_id,
                    spec.robustness_spec_sha256,
                    spec.document_id,
                    spec.scene_revision_id,
                    spec.candidate_variant_id,
                    spec.topology_candidate_id,
                    spec.nominal_bundle_id,
                    spec.model_dump_json(),
                    _utc_now(),
                ),
            )
        return spec

    def get_spec(
        self,
        robustness_spec_id: str,
    ) -> ProposalRobustnessAuthority | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_proposal_robustness_specs
                WHERE robustness_spec_id=?
                """,
                (robustness_spec_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate_spec(self._parse_spec_json(row['payload_json']))

    @staticmethod
    def _sampling_plan(
        spec: ProposalRobustnessAuthority,
    ) -> tuple[LocalPerturbation, ...]:
        if spec.sampling_strategy == 'deterministic_local_stencil':
            return build_local_stencil(spec)
        if spec.sampling_strategy == MULTIDIMENSIONAL_SAMPLING_STRATEGY:
            return build_multidimensional_sampling_plan(spec)
        raise ValueError(
            'unsupported proposal robustness sampling strategy: '
            f'{spec.sampling_strategy}'
        )

    def _expected_sample_evidence(
        self,
        *,
        spec: ProposalRobustnessAuthority,
        plan: LocalPerturbation,
        baseline: SceneRevision,
        candidate_variant: SystemVariant,
        topology_spec: TopologyPlacementSearchSpec,
    ) -> _ExpectedSampleEvidence:
        """Replay the canonical perturbed-scene evidence for one plan item.

        This mirrors ``evaluate_proposal_local_robustness`` and the O100F
        multidimensional equivalent exactly: the same perturbation application
        order, the same domain rejection bookkeeping, the same changed-entity
        set handed to the O80 orientation gate, and the same feasible
        computation over the persisted O100B constraint snapshot.
        """
        constraint_payload = json.loads(topology_spec.constraint_snapshot_json)
        if (
            canonical_robustness_sha256(constraint_payload)
            != spec.constraint_snapshot_sha256
        ):
            raise ValueError(
                'proposal robustness constraint snapshot hash mismatch'
            )
        constraint_set = CadConstraintSet.model_validate(constraint_payload)

        nominal_document = materialize_system_variant(baseline, candidate_variant)
        if (
            scene_content_hash(nominal_document)
            != spec.materialized_scene_content_hash
        ):
            raise ValueError('proposal robustness nominal scene hash mismatch')

        axis_by_id = {item.axis_id: item for item in spec.axes}
        document = nominal_document
        changed: set[str] = set()
        domain_rejections: list[str] = []
        perturbation_failure_reason: str | None = None

        if spec.sampling_strategy == 'deterministic_local_stencil':
            if plan.axis_id is None:
                changed.update(item.entity_id for item in spec.axes)
            else:
                axis = axis_by_id[plan.axis_id]
                delta = float(plan.parameter_deltas[plan.axis_id])
                changed.add(axis.entity_id)
                local_rejections = _domain_rejections(axis, delta)
                domain_rejections.extend(local_rejections)
                if not local_rejections:
                    try:
                        document = apply_local_perturbation(document, axis, delta)
                    except Exception as exc:
                        domain_rejections.append(
                            f'__perturbation_unsupported__:{axis.axis_id}'
                        )
                        perturbation_failure_reason = (
                            f'perturbation_failed:{exc}'
                        )
            expected_domain_ids = tuple(domain_rejections)
        elif spec.sampling_strategy == MULTIDIMENSIONAL_SAMPLING_STRATEGY:
            if plan.step == 'nominal':
                changed.update(item.entity_id for item in spec.axes)
            else:
                for axis_id in sorted(plan.parameter_deltas):
                    axis = axis_by_id[axis_id]
                    delta = float(plan.parameter_deltas[axis_id])
                    changed.add(axis.entity_id)
                    domain_rejections.extend(_domain_rejections(axis, delta))
                    try:
                        document = apply_local_perturbation(document, axis, delta)
                    except Exception as exc:
                        domain_rejections.append(
                            f'__perturbation_unsupported__:{axis.axis_id}'
                        )
                        perturbation_failure_reason = (
                            f'perturbation_failed:{exc}'
                        )
            expected_domain_ids = tuple(sorted(set(domain_rejections)))
        else:
            raise ValueError(
                'unsupported proposal robustness sampling strategy: '
                f'{spec.sampling_strategy}'
            )

        g10 = evaluate_cad_constraints(document, constraint_set)
        o80 = orientation_constraint_rejections(
            document,
            constraint_set,
            changed_entity_ids=tuple(sorted(changed)),
        )
        feasible = (
            g10.constraints_satisfied
            and not o80
            and not expected_domain_ids
        )
        return _ExpectedSampleEvidence(
            perturbed_scene_content_hash=scene_content_hash(document),
            feasible=feasible,
            g10_results=tuple(g10.results),
            o80_rejection_ids=tuple(o80),
            domain_rejection_ids=expected_domain_ids,
            perturbation_failure_reason=perturbation_failure_reason,
        )

    def _require_objective_result_authority(
        self,
        expected: ProposalObjectiveResultAuthority,
        *,
        connection: sqlite3.Connection | None,
    ) -> None:
        """Require exact perturbed-sample binding for one result ref.

        A typed ``ObjectiveResultResolver`` replays the external result
        authority directly. Without one, the repository still requires its own
        persisted richer result authority — recorded at save time — to
        reproduce the exact perturbed Scene/sample binding and canonical
        metric payload, so a ref cannot be reused across perturbations and a
        submitted ObjectiveVector value cannot drift from the recorded output.
        """
        ref = expected.result_ref
        resolver = self.objective_result_resolvers.get(ref.authority_kind)
        if resolver is not None:
            resolved = resolver(
                ProposalObjectiveEvidenceBinding(
                    objective_id=expected.metric.objective_id,
                    result_ref=ref,
                )
            )
            if resolved is None:
                raise ValueError(
                    'proposal objective result authority does not exist: '
                    f'{ref.authority_kind}:{ref.authority_id}'
                )
            resolved = ProposalObjectiveResultAuthority.model_validate(resolved)
            if resolved != expected:
                raise ValueError(
                    'proposal objective result authority does not reproduce '
                    'the exact perturbed sample binding'
                )

        if connection is not None:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_proposal_objective_result_authorities
                WHERE authority_kind=? AND authority_id=? AND objective_id=?
                """,
                (
                    ref.authority_kind,
                    ref.authority_id,
                    expected.metric.objective_id,
                ),
            ).fetchone()
        else:
            with closing(self._connect()) as local:
                row = local.execute(
                    """
                    SELECT payload_json
                    FROM cad_proposal_objective_result_authorities
                    WHERE authority_kind=? AND authority_id=? AND objective_id=?
                    """,
                    (
                        ref.authority_kind,
                        ref.authority_id,
                        expected.metric.objective_id,
                    ),
                ).fetchone()
        if row is not None:
            persisted = ProposalObjectiveResultAuthority.model_validate_json(
                row['payload_json']
            )
            if persisted != expected:
                raise ValueError(
                    'proposal objective result authority binds a different '
                    'perturbed sample or objective output'
                )
            return
        if connection is None:
            raise ValueError(
                'proposal objective result authority was never persisted for '
                f'{ref.authority_kind}:{ref.authority_id}'
            )
        connection.execute(
            """
            INSERT INTO cad_proposal_objective_result_authorities(
                authority_kind,
                authority_id,
                objective_id,
                sample_id,
                payload_json,
                recorded_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                ref.authority_kind,
                ref.authority_id,
                expected.metric.objective_id,
                expected.sample_id,
                expected.model_dump_json(),
                _utc_now(),
            ),
        )

    def _validate_sample(
        self,
        sample: ProposalPerturbationSample,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> ProposalPerturbationSample:
        sample = ProposalPerturbationSample.model_validate(
            sample.model_dump(mode='python')
        )
        spec = self.get_spec(sample.robustness_spec_id)
        if spec is None:
            raise ValueError('proposal sample references missing robustness spec')
        if (
            sample.robustness_spec_sha256 != spec.robustness_spec_sha256
            or sample.candidate_id != spec.candidate_id
            or sample.objective_contract_sha256 != spec.objective_contract_sha256
        ):
            raise ValueError('proposal sample robustness authority mismatch')

        plans = self._sampling_plan(spec)
        expected_plan = LocalPerturbation(
            sample_id=sample.sample_id,
            sample_index=sample.sample_index,
            axis_id=sample.axis_id,
            step=sample.step,
            parameter_deltas=sample.parameter_deltas,
        )
        if (
            sample.sample_index >= len(plans)
            or plans[sample.sample_index] != expected_plan
        ):
            raise ValueError(
                'proposal sample does not match deterministic sampling plan'
            )
        plan = plans[sample.sample_index]

        (
            baseline,
            _template_variant,
            candidate_variant,
            topology_spec,
            _topology_candidate,
            bundle,
        ) = self._resolve_spec_authorities(spec)
        expected = self._expected_sample_evidence(
            spec=spec,
            plan=plan,
            baseline=baseline,
            candidate_variant=candidate_variant,
            topology_spec=topology_spec,
        )
        if (
            sample.perturbed_scene_content_hash
            != expected.perturbed_scene_content_hash
            or sample.feasible != expected.feasible
            or tuple(sample.g10_results) != expected.g10_results
            or tuple(sample.o80_rejection_ids) != expected.o80_rejection_ids
            or tuple(sample.domain_rejection_ids)
            != expected.domain_rejection_ids
        ):
            raise ValueError(
                'proposal sample does not reproduce canonical perturbed '
                'scene, constraint and feasibility evidence'
            )

        if not expected.feasible:
            expected_failure = (
                expected.perturbation_failure_reason
                or 'hard_constraint_violation'
            )
            if sample.failure_reason != expected_failure:
                raise ValueError('proposal sample failure_reason mismatch')
            if (
                sample.objective_vector is not None
                or sample.objective_evidence
            ):
                raise ValueError(
                    'infeasible proposal sample must remain unscored'
                )
            return sample

        if sample.objective_vector is None:
            if plan.step == 'nominal':
                raise ValueError(
                    'feasible nominal proposal sample requires exact '
                    'bundle objective output'
                )
            if not (sample.failure_reason or '').startswith(
                'objective_evaluation_failed:'
            ):
                raise ValueError(
                    'feasible unscored proposal sample requires objective '
                    'evaluator failure provenance'
                )
            return sample

        if sample.failure_reason is not None:
            raise ValueError(
                'scored proposal sample cannot carry failure_reason'
            )
        expected_signature = _objective_signature(
            _selected_nominal_vector(bundle, spec.objective_ids)
        )
        if _objective_signature(sample.objective_vector) != expected_signature:
            raise ValueError('proposal sample objective schema mismatch')

        if plan.step == 'nominal':
            if sample.objective_evidence != _nominal_evidence(
                bundle,
                spec.objective_ids,
            ):
                raise ValueError(
                    'proposal nominal sample evidence differs from exact bundle'
                )
            if sample.objective_vector != _selected_nominal_vector(
                bundle,
                spec.objective_ids,
                candidate_id=sample.sample_id,
            ):
                raise ValueError(
                    'proposal nominal sample objective vector differs from '
                    'exact bundle'
                )
            return sample

        for binding in sample.objective_evidence:
            if (
                binding.result_ref.authority_kind
                not in self.objective_result_resolvers
            ):
                self._resolve_external(binding.result_ref)
        _validate_result_contract(
            nominal_bundle=bundle,
            spec=spec,
            result=ProposalPerturbationObjectiveResult(
                objective_vector=sample.objective_vector,
                objective_evidence=sample.objective_evidence,
            ),
            sample_id=sample.sample_id,
        )
        for binding in sample.objective_evidence:
            self._require_objective_result_authority(
                ProposalObjectiveResultAuthority(
                    result_ref=binding.result_ref,
                    robustness_spec_id=spec.robustness_spec_id,
                    robustness_spec_sha256=spec.robustness_spec_sha256,
                    sample_id=sample.sample_id,
                    perturbed_scene_content_hash=(
                        expected.perturbed_scene_content_hash
                    ),
                    metric=sample.objective_vector.metric(binding.objective_id),
                ),
                connection=connection,
            )
        return sample

    def save_sample(
        self,
        sample: ProposalPerturbationSample,
    ) -> ProposalPerturbationSample:
        with closing(self._connect()) as connection, connection:
            sample = self._validate_sample(sample, connection=connection)
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_proposal_perturbation_samples
                WHERE sample_id=?
                """,
                (sample.sample_id,),
            ).fetchone()
            if row is not None:
                persisted = ProposalPerturbationSample.model_validate_json(
                    row['payload_json']
                )
                if persisted != sample:
                    raise ValueError(
                        'ProposalPerturbationSample id exists with different semantics'
                    )
                return persisted
            connection.execute(
                """
                INSERT INTO cad_proposal_perturbation_samples(
                    sample_id,
                    sample_sha256,
                    robustness_spec_id,
                    sample_index,
                    feasible,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sample.sample_id,
                    sample.sample_sha256,
                    sample.robustness_spec_id,
                    sample.sample_index,
                    int(sample.feasible),
                    sample.model_dump_json(),
                    _utc_now(),
                ),
            )
        return sample

    def save_samples(
        self,
        samples: Sequence[ProposalPerturbationSample],
    ) -> tuple[ProposalPerturbationSample, ...]:
        persisted = tuple(self.save_sample(item) for item in samples)
        return persisted

    def list_samples(
        self,
        robustness_spec_id: str,
    ) -> tuple[ProposalPerturbationSample, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_proposal_perturbation_samples
                WHERE robustness_spec_id=?
                ORDER BY sample_index ASC
                """,
                (robustness_spec_id,),
            ).fetchall()
        return tuple(
            self._validate_sample(
                ProposalPerturbationSample.model_validate_json(row['payload_json'])
            )
            for row in rows
        )

    def _evidence_for_spec(
        self,
        robustness_spec_id: str,
        evidence: dict,
    ):
        """Spec + full sample listing, loaded once per spec per batch."""
        cached = evidence.get(robustness_spec_id)
        if cached is None:
            spec = self.get_spec(robustness_spec_id)
            if spec is None:
                raise ValueError(
                    'proposal robustness evaluation references missing spec'
                )
            cached = (spec, self.list_samples(spec.robustness_spec_id))
            evidence[robustness_spec_id] = cached
        return cached

    def _validate_evaluation(
        self,
        evaluation: RobustnessEvaluation,
        *,
        evidence: dict | None = None,
    ) -> RobustnessEvaluation:
        evaluation = RobustnessEvaluation.model_validate(
            evaluation.model_dump(mode='python')
        )
        spec, samples = self._evidence_for_spec(
            evaluation.robustness_spec_id,
            {} if evidence is None else evidence,
        )
        if (
            evaluation.robustness_spec_sha256 != spec.robustness_spec_sha256
            or evaluation.candidate_id != spec.candidate_id
        ):
            raise ValueError(
                'proposal robustness evaluation authority mismatch'
            )
        if isinstance(spec, ProposalMultidimensionalRobustnessSpec):
            plans = build_multidimensional_sampling_plan(spec)
            regenerated = build_multidimensional_evaluations_from_provenance(
                robustness_spec_id=spec.robustness_spec_id,
                robustness_spec_sha256=spec.robustness_spec_sha256,
                candidate_id=spec.candidate_id,
                expected_sample_ids=tuple(item.sample_id for item in plans),
                samples=samples,
                sampling_provenance=_proposal_multidimensional_provenance(
                    spec,
                    samples,
                ),
                created_at_utc=evaluation.created_at_utc,
            )
        else:
            regenerated = build_robustness_evaluations(
                spec,
                samples,
                created_at_utc=evaluation.created_at_utc,
            )
        match = next(
            (
                item
                for item in regenerated
                if item.objective_id == evaluation.objective_id
            ),
            None,
        )
        if match != evaluation:
            raise ValueError(
                'proposal robustness evaluation does not reproduce from samples'
            )
        return evaluation

    def save_evaluation(
        self,
        evaluation: RobustnessEvaluation,
    ) -> RobustnessEvaluation:
        with closing(self._connect()) as connection:
            return self._save_evaluation(connection, evaluation)

    def _save_evaluation(
        self,
        connection: sqlite3.Connection,
        evaluation: RobustnessEvaluation,
        *,
        evidence: dict | None = None,
    ) -> RobustnessEvaluation:
        evaluation = self._validate_evaluation(evaluation, evidence=evidence)
        with connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_proposal_robustness_evaluations
                WHERE evaluation_id=?
                """,
                (evaluation.evaluation_id,),
            ).fetchone()
            if row is not None:
                persisted = RobustnessEvaluation.model_validate_json(
                    row['payload_json']
                )
                if persisted != evaluation:
                    raise ValueError(
                        'proposal RobustnessEvaluation id exists with '
                        'different semantics'
                    )
                return self._validate_evaluation(persisted, evidence=evidence)
            connection.execute(
                """
                INSERT INTO cad_proposal_robustness_evaluations(
                    evaluation_id,
                    evaluation_sha256,
                    robustness_spec_id,
                    objective_id,
                    payload_json,
                    recorded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.evaluation_id,
                    evaluation.evaluation_sha256,
                    evaluation.robustness_spec_id,
                    evaluation.objective_id,
                    evaluation.model_dump_json(),
                    _utc_now(),
                ),
            )
        return evaluation

    def save_evaluations(
        self,
        evaluations: Sequence[RobustnessEvaluation],
    ) -> tuple[RobustnessEvaluation, ...]:
        """Persist a batch over one connection and one evidence replay.

        Each evaluation still commits independently — a mid-batch failure
        leaves earlier items persisted exactly as sequential
        ``save_evaluation`` calls would — but the batch shares one
        connection and one spec/sample evidence listing per spec instead
        of re-reading both per row.
        """
        evidence: dict = {}
        with closing(self._connect()) as connection:
            return tuple(
                self._save_evaluation(connection, item, evidence=evidence)
                for item in evaluations
            )

    def list_evaluations(
        self,
        robustness_spec_id: str,
    ) -> tuple[RobustnessEvaluation, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_proposal_robustness_evaluations
                WHERE robustness_spec_id=?
                ORDER BY objective_id ASC
                """,
                (robustness_spec_id,),
            ).fetchall()
        return tuple(
            self._validate_evaluation(
                RobustnessEvaluation.model_validate_json(row['payload_json'])
            )
            for row in rows
        )
