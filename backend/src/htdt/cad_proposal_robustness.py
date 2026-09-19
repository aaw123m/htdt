from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_constraint_models import CadConstraintResult, CadConstraintSet
from .cad_constraints import evaluate_cad_constraints
from .cad_orientation_constraints import orientation_constraint_rejections
from .cad_repository import SceneRepository, SceneRevision
from .cad_scene import SceneDocument, scene_content_hash
from .cad_schema import ensure_native_schema
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
from .optimization_objectives import ObjectiveVector
from .optimization_robustness import (
    ROBUSTNESS_ALGORITHM_VERSION,
    ROBUSTNESS_SCHEMA_VERSION,
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


PROPOSAL_ROBUSTNESS_AUTHORITY_VERSION = 'o100f-proposal-robustness-1'
PROPOSAL_ROBUSTNESS_SAMPLE_AUTHORITY_VERSION = 'o100f-proposal-sample-1'


AuthorityResolver = Callable[[str], ExactAuthorityRef | None]


class VariantBundleResolver(Protocol):
    path: Path

    def get_bundle(self, bundle_id: str) -> VariantEvaluationBundle | None:
        ...


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    software_version: str = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)

    @property
    def candidate_id(self) -> str:
        return self.candidate_variant_id

    @property
    def sampling_strategy(self) -> str:
        return 'deterministic_local_stencil'

    @property
    def algorithm_version(self) -> str:
        return ROBUSTNESS_ALGORITHM_VERSION

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
            exclude={'robustness_spec_id', 'robustness_spec_sha256'},
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
    step: Literal['nominal', 'minus', 'plus']
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
        'software_version': software_version,
        'created_at_utc': created_at_utc,
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


class CadProposalRobustnessRepository:
    """Append-only O100F local-robustness persistence.

    Existing O90 tables and identities are intentionally not modified.
    """

    def __init__(
        self,
        *,
        scene_repository: SceneRepository,
        variant_repository: CadSystemVariantRepository,
        topology_repository: CadTopologySearchRepository,
        bundle_resolver: VariantBundleResolver,
        external_resolvers: Mapping[str, AuthorityResolver] | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.variant_repository = variant_repository
        self.topology_repository = topology_repository
        self.bundle_resolver = bundle_resolver
        self.external_resolvers = dict(external_resolvers or {})
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
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cad_proposal_robustness_specs (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    robustness_spec_id TEXT NOT NULL UNIQUE,
                    robustness_spec_sha256 TEXT NOT NULL UNIQUE,
                    document_id TEXT NOT NULL,
                    scene_revision_id TEXT NOT NULL,
                    candidate_variant_id TEXT NOT NULL,
                    topology_candidate_id TEXT NOT NULL,
                    nominal_bundle_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_proposal_robustness_variant_seq
                    ON cad_proposal_robustness_specs(
                        candidate_variant_id,
                        seq ASC
                    );

                CREATE TABLE IF NOT EXISTS cad_proposal_perturbation_samples (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    sample_id TEXT NOT NULL UNIQUE,
                    sample_sha256 TEXT NOT NULL UNIQUE,
                    robustness_spec_id TEXT NOT NULL,
                    sample_index INTEGER NOT NULL,
                    feasible INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL,
                    UNIQUE(robustness_spec_id, sample_index),
                    FOREIGN KEY(robustness_spec_id)
                        REFERENCES cad_proposal_robustness_specs(robustness_spec_id)
                );

                CREATE TABLE IF NOT EXISTS cad_proposal_robustness_evaluations (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    evaluation_id TEXT NOT NULL UNIQUE,
                    evaluation_sha256 TEXT NOT NULL UNIQUE,
                    robustness_spec_id TEXT NOT NULL,
                    objective_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at_utc TEXT NOT NULL,
                    UNIQUE(robustness_spec_id, objective_id),
                    FOREIGN KEY(robustness_spec_id)
                        REFERENCES cad_proposal_robustness_specs(robustness_spec_id)
                );
                """
            )

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

    def _validate_spec(
        self,
        spec: ProposalRobustnessSpec,
    ) -> ProposalRobustnessSpec:
        spec = ProposalRobustnessSpec.model_validate(spec.model_dump(mode='python'))
        (
            baseline,
            template,
            candidate_variant,
            topology_spec,
            candidate,
            bundle,
        ) = self._resolve_spec_authorities(spec)
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
        spec: ProposalRobustnessSpec,
    ) -> ProposalRobustnessSpec:
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
                persisted = ProposalRobustnessSpec.model_validate_json(
                    row['payload_json']
                )
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
    ) -> ProposalRobustnessSpec | None:
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
        return self._validate_spec(
            ProposalRobustnessSpec.model_validate_json(row['payload_json'])
        )

    def _validate_sample(
        self,
        sample: ProposalPerturbationSample,
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

        plans = build_local_stencil(spec)
        if sample.sample_index >= len(plans) or plans[sample.sample_index] != LocalPerturbation(
            sample_id=sample.sample_id,
            sample_index=sample.sample_index,
            axis_id=sample.axis_id,
            step=sample.step,
            parameter_deltas=sample.parameter_deltas,
        ):
            raise ValueError('proposal sample does not match deterministic local stencil')

        *_, bundle = self._resolve_spec_authorities(spec)
        if sample.objective_vector is not None:
            expected_signature = _objective_signature(
                _selected_nominal_vector(bundle, spec.objective_ids)
            )
            if _objective_signature(sample.objective_vector) != expected_signature:
                raise ValueError('proposal sample objective schema mismatch')
            if sample.step == 'nominal':
                expected_evidence = _nominal_evidence(bundle, spec.objective_ids)
                if sample.objective_evidence != expected_evidence:
                    raise ValueError(
                        'proposal nominal sample evidence differs from exact bundle'
                    )
            else:
                for binding in sample.objective_evidence:
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
        return sample

    def save_sample(
        self,
        sample: ProposalPerturbationSample,
    ) -> ProposalPerturbationSample:
        sample = self._validate_sample(sample)
        with closing(self._connect()) as connection, connection:
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
                return self._validate_sample(persisted)
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

    def _validate_evaluation(
        self,
        evaluation: RobustnessEvaluation,
    ) -> RobustnessEvaluation:
        evaluation = RobustnessEvaluation.model_validate(
            evaluation.model_dump(mode='python')
        )
        spec = self.get_spec(evaluation.robustness_spec_id)
        if spec is None:
            raise ValueError(
                'proposal robustness evaluation references missing spec'
            )
        if (
            evaluation.robustness_spec_sha256 != spec.robustness_spec_sha256
            or evaluation.candidate_id != spec.candidate_id
        ):
            raise ValueError(
                'proposal robustness evaluation authority mismatch'
            )
        samples = self.list_samples(spec.robustness_spec_id)
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
        evaluation = self._validate_evaluation(evaluation)
        with closing(self._connect()) as connection, connection:
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
                return self._validate_evaluation(persisted)
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
        return tuple(self.save_evaluation(item) for item in evaluations)

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
