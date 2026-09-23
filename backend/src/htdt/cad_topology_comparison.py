from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_amplifier_headroom import (
    OBJECTIVE_COMPARISON_MODEL_ID as AMPLIFIER_OBJECTIVE_COMPARISON_MODEL_ID,
    PlaybackChainEvaluation,
)
from .cad_coverage import CoverageEvaluation
from .cad_direct_level import DirectLevelEvaluation
from .cad_repository import SceneRevision
from .cad_standards import StandardsEvaluation, StandardsProfile
from .cad_system_variant import SystemVariant
from .optimization_objectives import (
    ObjectiveDefinition,
    ObjectiveMetric,
    ObjectiveVector,
)
from .pareto import ParetoResult, pareto_front


TOPOLOGY_COMPARISON_SCHEMA_VERSION = 1
TOPOLOGY_COMPARISON_AUTHORITY_VERSION = 'o100d-topology-comparison-1'
TOPOLOGY_BUNDLE_AUTHORITY_VERSION = 'o100d-topology-variant-bundle-1'
TOPOLOGY_EVALUATION_AUTHORITY_VERSION = 'o100d-topology-comparison-evaluation-1'
TOPOLOGY_SELECTION_AUTHORITY_VERSION = 'o100d-topology-comparison-selection-1'

VariantRole = Literal['current', 'proposed', 'as_built']
EligibilityState = Literal['ELIGIBLE', 'INELIGIBLE']


def canonical_topology_comparison_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def canonical_topology_comparison_sha256(value: Any) -> str:
    return sha256(canonical_topology_comparison_json(value).encode('utf-8')).hexdigest()


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


class ExactAuthorityRef(BaseModel):
    """Exact opaque authority handle for extension points and evidence binding."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    authority_kind: str = Field(min_length=1)
    authority_id: str = Field(min_length=1)
    authority_version: str | None = Field(default=None, min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    evaluator_id: str | None = Field(default=None, min_length=1)
    evaluator_version: str | None = Field(default=None, min_length=1)
    model_id: str | None = Field(default=None, min_length=1)
    model_version: str | None = Field(default=None, min_length=1)
    fidelity: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def paired_identity_fields(self) -> 'ExactAuthorityRef':
        if (self.evaluator_id is None) != (self.evaluator_version is None):
            raise ValueError('evaluator id/version must be supplied together')
        if (self.model_id is None) != (self.model_version is None):
            raise ValueError('model id/version must be supplied together')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json')


class StandardsProfileRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    profile_id: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    profile_semantic_hash: str = Field(pattern=r'^[0-9a-f]{64}$')


class ComparisonCompatibilityPolicy(BaseModel):
    """No implicit normalization is permitted at the comparison boundary."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    objective_definition: Literal['exact'] = 'exact'
    evaluator_identity: Literal['exact_when_declared'] = 'exact_when_declared'
    model_identity: Literal['exact_when_declared'] = 'exact_when_declared'
    fidelity: Literal['exact_when_declared'] = 'exact_when_declared'
    missing_signature: Literal[
        'allow_when_objective_definition_is_exact'
    ] = 'allow_when_objective_definition_is_exact'


class MissingUnsupportedPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    required_missing: Literal['ineligible'] = 'ineligible'
    required_unsupported: Literal['ineligible'] = 'ineligible'
    optional_missing: Literal['allowed'] = 'allowed'
    optional_unsupported: Literal['allowed'] = 'allowed'
    substitute_numeric_value: Literal[False] = False


class ComparedSystemVariant(BaseModel):
    """Named comparison identity. The label is not inferred physical truth."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    variant_id: str = Field(min_length=1)
    variant_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    document_id: str = Field(min_length=1)
    baseline_revision_id: str = Field(min_length=1)
    baseline_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    role: VariantRole
    comparison_label: str = Field(min_length=1)
    layout_profile_ref: ExactAuthorityRef | None = None
    as_built_authority_ref: ExactAuthorityRef | None = None

    @model_validator(mode='after')
    def valid_role_authority(self) -> 'ComparedSystemVariant':
        if self.role == 'as_built' and self.as_built_authority_ref is None:
            raise ValueError('as_built comparison role requires exact as-built authority')
        if self.role != 'as_built' and self.as_built_authority_ref is not None:
            raise ValueError('as-built authority is only valid for as_built comparison role')
        return self


def compared_system_variant(
    variant: SystemVariant,
    *,
    role: VariantRole,
    comparison_label: str,
    layout_profile_ref: ExactAuthorityRef | None = None,
    as_built_authority_ref: ExactAuthorityRef | None = None,
) -> ComparedSystemVariant:
    return ComparedSystemVariant(
        variant_id=variant.variant_id,
        variant_sha256=variant.variant_sha256,
        document_id=variant.document_id,
        baseline_revision_id=variant.baseline_revision_id,
        baseline_content_hash=variant.baseline_content_hash,
        role=role,
        comparison_label=comparison_label,
        layout_profile_ref=layout_profile_ref,
        as_built_authority_ref=as_built_authority_ref,
    )


def standards_profile_ref(profile: StandardsProfile) -> StandardsProfileRef:
    return StandardsProfileRef(
        profile_id=profile.profile_id,
        profile_version=profile.version,
        profile_semantic_hash=profile.profile_semantic_hash,
    )


class SystemTopologyComparisonSpec(BaseModel):
    """Immutable named topology comparison contract over existing authorities."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = TOPOLOGY_COMPARISON_SCHEMA_VERSION
    authority_version: Literal[
        'o100d-topology-comparison-1'
    ] = TOPOLOGY_COMPARISON_AUTHORITY_VERSION
    comparison_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    baseline_scene_revision_id: str = Field(min_length=1)
    baseline_scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_variants: tuple[ComparedSystemVariant, ...] = Field(min_length=2)
    required_objectives: tuple[ObjectiveDefinition, ...] = Field(min_length=1)
    optional_objectives: tuple[ObjectiveDefinition, ...] = ()
    standards_profile: StandardsProfileRef
    compatibility_policy: ComparisonCompatibilityPolicy = ComparisonCompatibilityPolicy()
    missing_unsupported_policy: MissingUnsupportedPolicy = MissingUnsupportedPolicy()
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_spec(self) -> 'SystemTopologyComparisonSpec':
        variant_ids = [item.variant_id for item in self.candidate_variants]
        if len(variant_ids) != len(set(variant_ids)):
            raise ValueError('comparison SystemVariant ids must be unique')
        if sum(item.role == 'current' for item in self.candidate_variants) > 1:
            raise ValueError('named topology comparison permits at most one current variant')
        for item in self.candidate_variants:
            if (
                item.document_id != self.document_id
                or item.baseline_revision_id != self.baseline_scene_revision_id
                or item.baseline_content_hash != self.baseline_scene_content_hash
            ):
                raise ValueError('comparison candidate baseline SceneRevision mismatch')

        required_ids = [item.objective_id for item in self.required_objectives]
        optional_ids = [item.objective_id for item in self.optional_objectives]
        if len(required_ids) != len(set(required_ids)):
            raise ValueError('required objective ids must be unique')
        if len(optional_ids) != len(set(optional_ids)):
            raise ValueError('optional objective ids must be unique')
        overlap = set(required_ids).intersection(optional_ids)
        if overlap:
            raise ValueError(f'objective cannot be required and optional: {sorted(overlap)}')

        digest = canonical_topology_comparison_sha256(self.semantic_payload())
        if self.semantic_sha256 != digest:
            raise ValueError('SystemTopologyComparisonSpec semantic hash mismatch')
        if self.comparison_id != _semantic_id('topology-comparison', digest):
            raise ValueError('SystemTopologyComparisonSpec deterministic id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'name': self.name,
            'document_id': self.document_id,
            'baseline_scene_revision_id': self.baseline_scene_revision_id,
            'baseline_scene_content_hash': self.baseline_scene_content_hash,
            'candidate_variants': [
                item.model_dump(mode='json')
                for item in self.candidate_variants
            ],
            'required_objectives': [
                item.identity_payload()
                for item in self.required_objectives
            ],
            'optional_objectives': [
                item.identity_payload()
                for item in self.optional_objectives
            ],
            'standards_profile': self.standards_profile.model_dump(mode='json'),
            'compatibility_policy': self.compatibility_policy.model_dump(mode='json'),
            'missing_unsupported_policy': self.missing_unsupported_policy.model_dump(
                mode='json'
            ),
        }

    def objective_definition(self, objective_id: str) -> ObjectiveDefinition:
        for item in (*self.required_objectives, *self.optional_objectives):
            if item.objective_id == objective_id:
                return item
        raise KeyError(objective_id)

    def candidate(self, variant_id: str) -> ComparedSystemVariant:
        for item in self.candidate_variants:
            if item.variant_id == variant_id:
                return item
        raise KeyError(variant_id)


def build_system_topology_comparison_spec(
    *,
    name: str,
    baseline: SceneRevision,
    candidate_variants: Sequence[ComparedSystemVariant],
    required_objectives: Sequence[ObjectiveDefinition],
    optional_objectives: Sequence[ObjectiveDefinition],
    standards_profile: StandardsProfile,
    compatibility_policy: ComparisonCompatibilityPolicy | None = None,
    missing_unsupported_policy: MissingUnsupportedPolicy | None = None,
) -> SystemTopologyComparisonSpec:
    candidates = tuple(candidate_variants)
    required = tuple(required_objectives)
    optional = tuple(optional_objectives)
    policy = compatibility_policy or ComparisonCompatibilityPolicy()
    missing_policy = missing_unsupported_policy or MissingUnsupportedPolicy()
    profile_ref = standards_profile_ref(standards_profile)
    payload = {
        'schema_version': TOPOLOGY_COMPARISON_SCHEMA_VERSION,
        'authority_version': TOPOLOGY_COMPARISON_AUTHORITY_VERSION,
        'name': name,
        'document_id': baseline.document_id,
        'baseline_scene_revision_id': baseline.revision_id,
        'baseline_scene_content_hash': baseline.content_hash,
        'candidate_variants': [item.model_dump(mode='json') for item in candidates],
        'required_objectives': [item.identity_payload() for item in required],
        'optional_objectives': [item.identity_payload() for item in optional],
        'standards_profile': profile_ref.model_dump(mode='json'),
        'compatibility_policy': policy.model_dump(mode='json'),
        'missing_unsupported_policy': missing_policy.model_dump(mode='json'),
    }
    digest = canonical_topology_comparison_sha256(payload)
    return SystemTopologyComparisonSpec(
        comparison_id=_semantic_id('topology-comparison', digest),
        name=name,
        document_id=baseline.document_id,
        baseline_scene_revision_id=baseline.revision_id,
        baseline_scene_content_hash=baseline.content_hash,
        candidate_variants=candidates,
        required_objectives=required,
        optional_objectives=optional,
        standards_profile=profile_ref,
        compatibility_policy=policy,
        missing_unsupported_policy=missing_policy,
        semantic_sha256=digest,
    )


def coverage_evaluation_ref(evaluation: CoverageEvaluation) -> ExactAuthorityRef:
    return ExactAuthorityRef(
        authority_kind='coverage_evaluation',
        authority_id=evaluation.evaluation_id,
        authority_version=evaluation.authority_version,
        semantic_sha256=evaluation.evaluation_sha256,
        evaluator_id='o100d-coverage-evaluator',
        evaluator_version=evaluation.authority_version,
        model_id='o100d-coverage-directivity-objective',
        model_version=evaluation.scenario.scenario_sha256,
        fidelity='exact-directivity-dataset',
    )


def direct_level_evaluation_ref(
    evaluation: DirectLevelEvaluation,
) -> ExactAuthorityRef:
    return ExactAuthorityRef(
        authority_kind='direct_level_evaluation',
        authority_id=evaluation.evaluation_id,
        authority_version=evaluation.authority_version,
        semantic_sha256=evaluation.evaluation_sha256,
        evaluator_id='o100d-direct-level-evaluator',
        evaluator_version=evaluation.authority_version,
        model_id='o100d-direct-equipment-derived-objective',
        model_version=evaluation.scenario.scenario_sha256,
        fidelity='direct-equipment-derived',
    )


def amplifier_headroom_evaluation_ref(
    evaluation: PlaybackChainEvaluation,
) -> ExactAuthorityRef:
    """Return the exact topology-comparison handle for playback-chain headroom.

    The evaluation hash binds the complete embedded PlaybackChainScenario.  The
    declared model identity uses the scenario comparison hash because that is
    the existing authority that defines quantity/load/duration/channel/routing
    comparability across different SystemVariant instances.  No separate
    fidelity claim is added because PlaybackChainEvaluation does not define one.
    """

    return ExactAuthorityRef(
        authority_kind='amplifier_headroom_evaluation',
        authority_id=evaluation.evaluation_id,
        authority_version=evaluation.authority_version,
        semantic_sha256=evaluation.evaluation_sha256,
        model_id=AMPLIFIER_OBJECTIVE_COMPARISON_MODEL_ID,
        model_version=evaluation.scenario.comparison_sha256,
    )


def standards_evaluation_ref(evaluation: StandardsEvaluation) -> ExactAuthorityRef:
    return ExactAuthorityRef(
        authority_kind='standards_evaluation',
        authority_id=evaluation.evaluation_id,
        authority_version=evaluation.authority_version,
        semantic_sha256=evaluation.evaluation_sha256,
        evaluator_id='standards-evaluator',
        evaluator_version=evaluation.evaluator_version,
        fidelity='criterion-level-evidence',
    )


class ObjectiveEvidenceBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    objective_id: str = Field(min_length=1)
    source_authority_kind: str = Field(min_length=1)
    source_authority_id: str = Field(min_length=1)
    source_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class ResolvedObjectiveAuthority(BaseModel):
    """Resolver result for an objective-bearing extension authority.

    An opaque ``ExactAuthorityRef`` proves only that an authority exists; it
    does not prove that a bundle's bound objective metric is the authority's
    actual output. Resolvers for authorities that contribute bundle objective
    metrics must return the resolved ref together with the canonical
    ``ObjectiveVector`` that authority produced for the bundle's
    SystemVariant.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    ref: ExactAuthorityRef
    objective_vector: ObjectiveVector


class VariantEvaluationBundle(BaseModel):
    """Exact per-variant references plus an already-computed ObjectiveVector."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = TOPOLOGY_COMPARISON_SCHEMA_VERSION
    authority_version: Literal[
        'o100d-topology-variant-bundle-1'
    ] = TOPOLOGY_BUNDLE_AUTHORITY_VERSION
    bundle_id: str = Field(min_length=1)
    comparison_id: str = Field(min_length=1)
    comparison_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    variant_id: str = Field(min_length=1)
    variant_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    coverage_evaluation: ExactAuthorityRef | None = None
    direct_level_evaluation: ExactAuthorityRef | None = None
    amplifier_headroom_evaluation: ExactAuthorityRef | None = None
    standards_evaluation: ExactAuthorityRef | None = None
    fr_prediction_refs: tuple[ExactAuthorityRef, ...] = ()
    reflection_prediction_refs: tuple[ExactAuthorityRef, ...] = ()
    installation_evidence_refs: tuple[ExactAuthorityRef, ...] = ()

    objective_vector: ObjectiveVector
    objective_vector_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    objective_evidence: tuple[ObjectiveEvidenceBinding, ...] = Field(min_length=1)
    bundle_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_bundle(self) -> 'VariantEvaluationBundle':
        expected_kinds = (
            ('coverage_evaluation', self.coverage_evaluation),
            ('direct_level_evaluation', self.direct_level_evaluation),
            ('amplifier_headroom_evaluation', self.amplifier_headroom_evaluation),
            ('standards_evaluation', self.standards_evaluation),
        )
        for expected, ref in expected_kinds:
            if ref is not None and ref.authority_kind != expected:
                raise ValueError(f'{expected} ref has wrong authority_kind')

        if self.objective_vector.candidate_id != self.variant_id:
            raise ValueError('bundle ObjectiveVector candidate_id must equal SystemVariant id')
        vector_digest = canonical_topology_comparison_sha256(
            self.objective_vector.identity_payload()
        )
        if self.objective_vector_sha256 != vector_digest:
            raise ValueError('bundle ObjectiveVector identity hash mismatch')

        metric_ids = [item.objective_id for item in self.objective_vector.metrics]
        evidence_ids = [item.objective_id for item in self.objective_evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError('bundle objective evidence bindings must be unique')
        if set(metric_ids) != set(evidence_ids):
            raise ValueError('every bundle objective metric requires one exact evidence binding')

        source_refs = self.all_evaluation_refs(include_standards=False)
        source_keys = {
            (ref.authority_kind, ref.authority_id, ref.semantic_sha256)
            for ref in source_refs
        }
        for binding in self.objective_evidence:
            key = (
                binding.source_authority_kind,
                binding.source_authority_id,
                binding.source_semantic_sha256,
            )
            if key not in source_keys:
                raise ValueError(
                    f'objective {binding.objective_id} evidence does not resolve to '
                    'a non-standards bundle authority'
                )

        digest = canonical_topology_comparison_sha256(self.semantic_payload())
        if self.bundle_sha256 != digest:
            raise ValueError('VariantEvaluationBundle semantic hash mismatch')
        if self.bundle_id != _semantic_id('topology-bundle', digest):
            raise ValueError('VariantEvaluationBundle deterministic id mismatch')
        return self

    def all_evaluation_refs(
        self,
        *,
        include_standards: bool = True,
    ) -> tuple[ExactAuthorityRef, ...]:
        fixed = tuple(
            ref
            for ref in (
                self.coverage_evaluation,
                self.direct_level_evaluation,
                self.amplifier_headroom_evaluation,
                self.standards_evaluation if include_standards else None,
            )
            if ref is not None
        )
        return (
            *fixed,
            *self.fr_prediction_refs,
            *self.reflection_prediction_refs,
            *self.installation_evidence_refs,
        )

    def evidence_source(self, objective_id: str) -> ExactAuthorityRef:
        binding = next(
            item for item in self.objective_evidence
            if item.objective_id == objective_id
        )
        for ref in self.all_evaluation_refs(include_standards=False):
            if (
                ref.authority_kind == binding.source_authority_kind
                and ref.authority_id == binding.source_authority_id
                and ref.semantic_sha256 == binding.source_semantic_sha256
            ):
                return ref
        raise KeyError(objective_id)

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'comparison_id': self.comparison_id,
            'comparison_semantic_sha256': self.comparison_semantic_sha256,
            'variant_id': self.variant_id,
            'variant_sha256': self.variant_sha256,
            'coverage_evaluation': (
                None
                if self.coverage_evaluation is None
                else self.coverage_evaluation.identity_payload()
            ),
            'direct_level_evaluation': (
                None
                if self.direct_level_evaluation is None
                else self.direct_level_evaluation.identity_payload()
            ),
            'amplifier_headroom_evaluation': (
                None
                if self.amplifier_headroom_evaluation is None
                else self.amplifier_headroom_evaluation.identity_payload()
            ),
            'standards_evaluation': (
                None
                if self.standards_evaluation is None
                else self.standards_evaluation.identity_payload()
            ),
            'fr_prediction_refs': [
                item.identity_payload()
                for item in self.fr_prediction_refs
            ],
            'reflection_prediction_refs': [
                item.identity_payload()
                for item in self.reflection_prediction_refs
            ],
            'installation_evidence_refs': [
                item.identity_payload()
                for item in self.installation_evidence_refs
            ],
            'objective_vector': self.objective_vector.identity_payload(),
            'objective_vector_sha256': self.objective_vector_sha256,
            'objective_evidence': [
                item.model_dump(mode='json')
                for item in self.objective_evidence
            ],
        }


def build_variant_evaluation_bundle(
    *,
    spec: SystemTopologyComparisonSpec,
    variant: SystemVariant,
    objective_vector: ObjectiveVector,
    objective_evidence: Sequence[ObjectiveEvidenceBinding],
    coverage_evaluation: ExactAuthorityRef | None = None,
    direct_level_evaluation: ExactAuthorityRef | None = None,
    amplifier_headroom_evaluation: ExactAuthorityRef | None = None,
    standards_evaluation: ExactAuthorityRef | None = None,
    fr_prediction_refs: Sequence[ExactAuthorityRef] = (),
    reflection_prediction_refs: Sequence[ExactAuthorityRef] = (),
    installation_evidence_refs: Sequence[ExactAuthorityRef] = (),
) -> VariantEvaluationBundle:
    candidate = spec.candidate(variant.variant_id)
    if candidate.variant_sha256 != variant.variant_sha256:
        raise ValueError('bundle SystemVariant hash does not match comparison spec')
    if (
        variant.document_id != spec.document_id
        or variant.baseline_revision_id != spec.baseline_scene_revision_id
        or variant.baseline_content_hash != spec.baseline_scene_content_hash
    ):
        raise ValueError('bundle SystemVariant baseline authority mismatch')
    vector_digest = canonical_topology_comparison_sha256(
        objective_vector.identity_payload()
    )
    base = {
        'schema_version': TOPOLOGY_COMPARISON_SCHEMA_VERSION,
        'authority_version': TOPOLOGY_BUNDLE_AUTHORITY_VERSION,
        'comparison_id': spec.comparison_id,
        'comparison_semantic_sha256': spec.semantic_sha256,
        'variant_id': variant.variant_id,
        'variant_sha256': variant.variant_sha256,
        'coverage_evaluation': (
            None if coverage_evaluation is None else coverage_evaluation.identity_payload()
        ),
        'direct_level_evaluation': (
            None if direct_level_evaluation is None else direct_level_evaluation.identity_payload()
        ),
        'amplifier_headroom_evaluation': (
            None
            if amplifier_headroom_evaluation is None
            else amplifier_headroom_evaluation.identity_payload()
        ),
        'standards_evaluation': (
            None if standards_evaluation is None else standards_evaluation.identity_payload()
        ),
        'fr_prediction_refs': [item.identity_payload() for item in fr_prediction_refs],
        'reflection_prediction_refs': [
            item.identity_payload()
            for item in reflection_prediction_refs
        ],
        'installation_evidence_refs': [
            item.identity_payload()
            for item in installation_evidence_refs
        ],
        'objective_vector': objective_vector.identity_payload(),
        'objective_vector_sha256': vector_digest,
        'objective_evidence': [
            item.model_dump(mode='json')
            for item in objective_evidence
        ],
    }
    digest = canonical_topology_comparison_sha256(base)
    return VariantEvaluationBundle(
        bundle_id=_semantic_id('topology-bundle', digest),
        comparison_id=spec.comparison_id,
        comparison_semantic_sha256=spec.semantic_sha256,
        variant_id=variant.variant_id,
        variant_sha256=variant.variant_sha256,
        coverage_evaluation=coverage_evaluation,
        direct_level_evaluation=direct_level_evaluation,
        amplifier_headroom_evaluation=amplifier_headroom_evaluation,
        standards_evaluation=standards_evaluation,
        fr_prediction_refs=tuple(fr_prediction_refs),
        reflection_prediction_refs=tuple(reflection_prediction_refs),
        installation_evidence_refs=tuple(installation_evidence_refs),
        objective_vector=objective_vector,
        objective_vector_sha256=vector_digest,
        objective_evidence=tuple(objective_evidence),
        bundle_sha256=digest,
    )


class ComparisonEligibilityIssue(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    code: str = Field(min_length=1)
    objective_id: str | None = Field(default=None, min_length=1)
    detail: str = Field(min_length=1)


class VariantComparisonEligibility(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    variant_id: str = Field(min_length=1)
    state: EligibilityState
    issues: tuple[ComparisonEligibilityIssue, ...] = ()

    @model_validator(mode='after')
    def state_matches_issues(self) -> 'VariantComparisonEligibility':
        if (self.state == 'ELIGIBLE') != (not self.issues):
            raise ValueError('comparison eligibility state/issues mismatch')
        return self


class VariantBundleRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    bundle_id: str = Field(min_length=1)
    bundle_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    variant_id: str = Field(min_length=1)
    variant_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class TopologyComparisonEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = TOPOLOGY_COMPARISON_SCHEMA_VERSION
    authority_version: Literal[
        'o100d-topology-comparison-evaluation-1'
    ] = TOPOLOGY_EVALUATION_AUTHORITY_VERSION
    evaluation_id: str = Field(min_length=1)
    comparison_id: str = Field(min_length=1)
    comparison_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    bundles: tuple[VariantBundleRef, ...]
    eligibility: tuple[VariantComparisonEligibility, ...] = Field(min_length=2)
    pareto_objective_ids: tuple[str, ...]
    pareto_result: ParetoResult | None = None
    created_at_utc: str = Field(min_length=1)
    evaluation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_evaluation(self) -> 'TopologyComparisonEvaluation':
        bundle_ids = [item.bundle_id for item in self.bundles]
        if len(bundle_ids) != len(set(bundle_ids)):
            raise ValueError('comparison bundle refs must be unique')
        variant_ids = [item.variant_id for item in self.eligibility]
        if len(variant_ids) != len(set(variant_ids)):
            raise ValueError('comparison eligibility variants must be unique')
        eligible = {
            item.variant_id
            for item in self.eligibility
            if item.state == 'ELIGIBLE'
        }
        if self.pareto_result is None:
            if eligible:
                raise ValueError('eligible comparison candidates require Pareto result')
            if self.pareto_objective_ids:
                raise ValueError('comparison without eligible candidates has no Pareto axes')
        else:
            if self.pareto_result.objective_ids != self.pareto_objective_ids:
                raise ValueError('comparison Pareto objective ids mismatch')
            if set(self.pareto_result.dominated_by) != eligible:
                raise ValueError('comparison Pareto candidate set must equal eligible candidates')
        digest = canonical_topology_comparison_sha256(self.semantic_payload())
        if self.evaluation_sha256 != digest:
            raise ValueError('TopologyComparisonEvaluation semantic hash mismatch')
        if self.evaluation_id != _semantic_id('topology-evaluation', digest):
            raise ValueError('TopologyComparisonEvaluation deterministic id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'comparison_id': self.comparison_id,
            'comparison_semantic_sha256': self.comparison_semantic_sha256,
            'bundles': [item.model_dump(mode='json') for item in self.bundles],
            'eligibility': [item.model_dump(mode='json') for item in self.eligibility],
            'pareto_objective_ids': list(self.pareto_objective_ids),
            'pareto_result': (
                None
                if self.pareto_result is None
                else self.pareto_result.model_dump(mode='json')
            ),
            'created_at_utc': self.created_at_utc,
        }


def _definition_issues(
    *,
    expected: ObjectiveDefinition,
    metric: ObjectiveMetric,
) -> tuple[ComparisonEligibilityIssue, ...]:
    actual = metric.definition
    if actual is None:
        return (
            ComparisonEligibilityIssue(
                code='objective_definition_missing',
                objective_id=expected.objective_id,
                detail='comparison requires explicit ObjectiveDefinition authority',
            ),
        )
    fields = (
        ('objective_id', 'objective_id'),
        ('quantity', 'quantity'),
        ('unit', 'unit'),
        ('direction', 'direction'),
        ('valid_domain', 'valid_domain'),
        ('comparison_model_id', 'comparison_model_id'),
        ('comparison_model_version', 'comparison_model_version'),
    )
    issues: list[ComparisonEligibilityIssue] = []
    for field_name, code_suffix in fields:
        if getattr(actual, field_name) != getattr(expected, field_name):
            issues.append(
                ComparisonEligibilityIssue(
                    code=f'incompatible_{code_suffix}',
                    objective_id=expected.objective_id,
                    detail=(
                        f'{field_name} differs from exact comparison definition'
                    ),
                )
            )
    return tuple(issues)


def _source_definition_issues(
    *,
    expected: ObjectiveDefinition,
    bundle: VariantEvaluationBundle,
) -> tuple[ComparisonEligibilityIssue, ...]:
    source = bundle.evidence_source(expected.objective_id)
    issues: list[ComparisonEligibilityIssue] = []
    if source.model_id is not None and (
        source.model_id != expected.comparison_model_id
        or source.model_version != expected.comparison_model_version
    ):
        issues.append(
            ComparisonEligibilityIssue(
                code='incompatible_source_model',
                objective_id=expected.objective_id,
                detail=(
                    'objective evidence source model id/version differs from '
                    'the exact ObjectiveDefinition comparison model'
                ),
            )
        )
    return tuple(issues)


def _metric_for(
    bundle: VariantEvaluationBundle,
    objective_id: str,
) -> ObjectiveMetric | None:
    try:
        return bundle.objective_vector.metric(objective_id)
    except KeyError:
        return None


def _declared_signature_issue_codes(
    bundles: Sequence[VariantEvaluationBundle],
    objective_id: str,
) -> set[str]:
    refs: list[ExactAuthorityRef] = []
    for bundle in bundles:
        metric = _metric_for(bundle, objective_id)
        if metric is None or metric.state != 'available':
            continue
        refs.append(bundle.evidence_source(objective_id))

    signatures = {
        'incompatible_evaluator_identity': {
            (ref.evaluator_id, ref.evaluator_version)
            for ref in refs
            if ref.evaluator_id is not None
        },
        'incompatible_source_model': {
            (ref.model_id, ref.model_version)
            for ref in refs
            if ref.model_id is not None
        },
        'incompatible_fidelity': {
            ref.fidelity
            for ref in refs
            if ref.fidelity is not None
        },
    }
    return {
        code
        for code, values in signatures.items()
        if len(values) > 1
    }


def _bundle_required_issues(
    *,
    spec: SystemTopologyComparisonSpec,
    bundle: VariantEvaluationBundle,
) -> tuple[ComparisonEligibilityIssue, ...]:
    issues: list[ComparisonEligibilityIssue] = []
    for definition in spec.required_objectives:
        metric = _metric_for(bundle, definition.objective_id)
        if metric is None:
            issues.append(
                ComparisonEligibilityIssue(
                    code='required_objective_missing',
                    objective_id=definition.objective_id,
                    detail='required objective is absent from ObjectiveVector',
                )
            )
            continue
        if metric.state == 'missing':
            issues.append(
                ComparisonEligibilityIssue(
                    code='required_objective_missing',
                    objective_id=definition.objective_id,
                    detail='required objective state is missing',
                )
            )
            continue
        if metric.state == 'unsupported':
            issues.append(
                ComparisonEligibilityIssue(
                    code='required_objective_unsupported',
                    objective_id=definition.objective_id,
                    detail='required objective state is unsupported',
                )
            )
            continue
        definition_issues = _definition_issues(
            expected=definition,
            metric=metric,
        )
        issues.extend(definition_issues)
        if not definition_issues:
            issues.extend(
                _source_definition_issues(
                    expected=definition,
                    bundle=bundle,
                )
            )
    return tuple(issues)


def _optional_is_common_and_compatible(
    *,
    spec: SystemTopologyComparisonSpec,
    bundles: Sequence[VariantEvaluationBundle],
    definition: ObjectiveDefinition,
) -> bool:
    for bundle in bundles:
        metric = _metric_for(bundle, definition.objective_id)
        if metric is None or metric.state != 'available':
            return False
        if _definition_issues(expected=definition, metric=metric):
            return False
        if _source_definition_issues(expected=definition, bundle=bundle):
            return False
    if _declared_signature_issue_codes(bundles, definition.objective_id):
        return False
    return True


def evaluate_topology_comparison(
    *,
    spec: SystemTopologyComparisonSpec,
    bundles: Sequence[VariantEvaluationBundle],
    created_at_utc: str,
) -> TopologyComparisonEvaluation:
    bundle_by_variant = {item.variant_id: item for item in bundles}
    if len(bundle_by_variant) != len(tuple(bundles)):
        raise ValueError('comparison permits at most one bundle per SystemVariant')
    for bundle in bundle_by_variant.values():
        if (
            bundle.comparison_id != spec.comparison_id
            or bundle.comparison_semantic_sha256 != spec.semantic_sha256
        ):
            raise ValueError('VariantEvaluationBundle belongs to another comparison spec')
        candidate = spec.candidate(bundle.variant_id)
        if candidate.variant_sha256 != bundle.variant_sha256:
            raise ValueError('VariantEvaluationBundle SystemVariant hash mismatch')

    initial_issues: dict[str, list[ComparisonEligibilityIssue]] = {}
    for candidate in spec.candidate_variants:
        bundle = bundle_by_variant.get(candidate.variant_id)
        if bundle is None:
            initial_issues[candidate.variant_id] = [
                ComparisonEligibilityIssue(
                    code='variant_bundle_missing',
                    detail='candidate has no VariantEvaluationBundle',
                )
            ]
        else:
            initial_issues[candidate.variant_id] = list(
                _bundle_required_issues(spec=spec, bundle=bundle)
            )

    ordered_bundles = tuple(
        bundle_by_variant[item.variant_id]
        for item in spec.candidate_variants
        if item.variant_id in bundle_by_variant
    )

    provisionally_eligible = [
        bundle_by_variant[item.variant_id]
        for item in spec.candidate_variants
        if not initial_issues[item.variant_id]
    ]
    for definition in spec.required_objectives:
        codes = _declared_signature_issue_codes(
            provisionally_eligible,
            definition.objective_id,
        )
        if not codes:
            continue
        for bundle in provisionally_eligible:
            for code in sorted(codes):
                initial_issues[bundle.variant_id].append(
                    ComparisonEligibilityIssue(
                        code=code,
                        objective_id=definition.objective_id,
                        detail=(
                            'required objective evidence uses incompatible declared '
                            'evaluator/model/fidelity authority'
                        ),
                    )
                )

    eligibility = tuple(
        VariantComparisonEligibility(
            variant_id=candidate.variant_id,
            state=(
                'ELIGIBLE'
                if not initial_issues[candidate.variant_id]
                else 'INELIGIBLE'
            ),
            issues=tuple(initial_issues[candidate.variant_id]),
        )
        for candidate in spec.candidate_variants
    )
    eligible_ids = {
        item.variant_id
        for item in eligibility
        if item.state == 'ELIGIBLE'
    }
    eligible_bundles = [
        bundle_by_variant[item.variant_id]
        for item in spec.candidate_variants
        if item.variant_id in eligible_ids
    ]

    if eligible_bundles:
        optional_ids = tuple(
            definition.objective_id
            for definition in spec.optional_objectives
            if _optional_is_common_and_compatible(
                spec=spec,
                bundles=eligible_bundles,
                definition=definition,
            )
        )
        pareto_objective_ids = (
            *(item.objective_id for item in spec.required_objectives),
            *optional_ids,
        )
        reduced_vectors = tuple(
            ObjectiveVector(
                candidate_id=bundle.variant_id,
                metrics=tuple(
                    bundle.objective_vector.metric(objective_id)
                    for objective_id in pareto_objective_ids
                ),
            )
            for bundle in eligible_bundles
        )
        pareto_result = pareto_front(
            reduced_vectors,
            pareto_objective_ids,
        )
    else:
        pareto_objective_ids = ()
        pareto_result = None

    bundle_refs = tuple(
        VariantBundleRef(
            bundle_id=item.bundle_id,
            bundle_sha256=item.bundle_sha256,
            variant_id=item.variant_id,
            variant_sha256=item.variant_sha256,
        )
        for item in ordered_bundles
    )
    payload = {
        'schema_version': TOPOLOGY_COMPARISON_SCHEMA_VERSION,
        'authority_version': TOPOLOGY_EVALUATION_AUTHORITY_VERSION,
        'comparison_id': spec.comparison_id,
        'comparison_semantic_sha256': spec.semantic_sha256,
        'bundles': [item.model_dump(mode='json') for item in bundle_refs],
        'eligibility': [item.model_dump(mode='json') for item in eligibility],
        'pareto_objective_ids': list(pareto_objective_ids),
        'pareto_result': (
            None if pareto_result is None else pareto_result.model_dump(mode='json')
        ),
        'created_at_utc': created_at_utc,
    }
    digest = canonical_topology_comparison_sha256(payload)
    return TopologyComparisonEvaluation(
        evaluation_id=_semantic_id('topology-evaluation', digest),
        comparison_id=spec.comparison_id,
        comparison_semantic_sha256=spec.semantic_sha256,
        bundles=bundle_refs,
        eligibility=eligibility,
        pareto_objective_ids=pareto_objective_ids,
        pareto_result=pareto_result,
        created_at_utc=created_at_utc,
        evaluation_sha256=digest,
    )


class TopologyComparisonSelection(BaseModel):
    """Decision record only. It never mutates deployment lifecycle state."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = TOPOLOGY_COMPARISON_SCHEMA_VERSION
    authority_version: Literal[
        'o100d-topology-comparison-selection-1'
    ] = TOPOLOGY_SELECTION_AUTHORITY_VERSION
    selection_id: str = Field(min_length=1)
    comparison_evaluation_id: str = Field(min_length=1)
    comparison_evaluation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    selected_variant_id: str = Field(min_length=1)
    selected_variant_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    selected_by: str = Field(min_length=1)
    rationale: str | None = Field(default=None, min_length=1)
    selected_at_utc: str = Field(min_length=1)
    selection_state: Literal['selected_only'] = 'selected_only'
    applied: Literal[False] = False
    installed: Literal[False] = False
    measured: Literal[False] = False
    selection_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_selection(self) -> 'TopologyComparisonSelection':
        digest = canonical_topology_comparison_sha256(self.semantic_payload())
        if self.selection_sha256 != digest:
            raise ValueError('TopologyComparisonSelection semantic hash mismatch')
        if self.selection_id != _semantic_id('topology-selection', digest):
            raise ValueError('TopologyComparisonSelection deterministic id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'comparison_evaluation_id': self.comparison_evaluation_id,
            'comparison_evaluation_sha256': self.comparison_evaluation_sha256,
            'selected_variant_id': self.selected_variant_id,
            'selected_variant_sha256': self.selected_variant_sha256,
            'selected_by': self.selected_by,
            'rationale': self.rationale,
            'selected_at_utc': self.selected_at_utc,
            'selection_state': self.selection_state,
            'applied': self.applied,
            'installed': self.installed,
            'measured': self.measured,
        }


def build_topology_comparison_selection(
    *,
    spec: SystemTopologyComparisonSpec,
    evaluation: TopologyComparisonEvaluation,
    selected_variant_id: str,
    selected_by: str,
    selected_at_utc: str,
    rationale: str | None = None,
) -> TopologyComparisonSelection:
    if (
        evaluation.comparison_id != spec.comparison_id
        or evaluation.comparison_semantic_sha256 != spec.semantic_sha256
    ):
        raise ValueError('selection evaluation/spec authority mismatch')
    state = next(
        (
            item
            for item in evaluation.eligibility
            if item.variant_id == selected_variant_id
        ),
        None,
    )
    if state is None or state.state != 'ELIGIBLE':
        raise ValueError('selection requires a comparison-eligible SystemVariant')
    candidate = spec.candidate(selected_variant_id)
    payload = {
        'schema_version': TOPOLOGY_COMPARISON_SCHEMA_VERSION,
        'authority_version': TOPOLOGY_SELECTION_AUTHORITY_VERSION,
        'comparison_evaluation_id': evaluation.evaluation_id,
        'comparison_evaluation_sha256': evaluation.evaluation_sha256,
        'selected_variant_id': candidate.variant_id,
        'selected_variant_sha256': candidate.variant_sha256,
        'selected_by': selected_by,
        'rationale': rationale,
        'selected_at_utc': selected_at_utc,
        'selection_state': 'selected_only',
        'applied': False,
        'installed': False,
        'measured': False,
    }
    digest = canonical_topology_comparison_sha256(payload)
    return TopologyComparisonSelection(
        selection_id=_semantic_id('topology-selection', digest),
        comparison_evaluation_id=evaluation.evaluation_id,
        comparison_evaluation_sha256=evaluation.evaluation_sha256,
        selected_variant_id=candidate.variant_id,
        selected_variant_sha256=candidate.variant_sha256,
        selected_by=selected_by,
        rationale=rationale,
        selected_at_utc=selected_at_utc,
        selection_sha256=digest,
    )
