"""Intervention Planner MVP (#519).

Orchestration layer over existing canonical evaluators: an
``InterventionStudySpec`` pins the baseline revision/variant, the exact
finding or explicit problem region, the allowed intervention families, the
target region of interest AND an independent guardrail domain, plus the
objective/metric contract to evaluate against. ``InterventionAlternative``
records are immutable counterfactual evaluations bound to generated
authority ids (SearchSpec / JointOptimizationSpec / treatment plan …) —
never scene truth and never collapsed into one hidden score.

Family rollout follows the issue's recommended order: geometry and
calibration(joint) studies first; treatment studies require explicit
capability evidence and are otherwise rejected rather than fabricated.
"""

from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_repository import SceneRevision
from .cad_system_variant import SystemVariant
from .optimization_objectives import ObjectiveDefinition


INTERVENTION_SCHEMA_VERSION = 1
INTERVENTION_AUTHORITY_VERSION = 'intervention-study-1'

InterventionFamily = Literal[
    'geometry',
    'calibration',
    'treatment',
    'topology',
]

FindingSourceKind = Literal[
    'prediction_result',
    'coverage_evaluation',
    'direct_level_evaluation',
    'measurement',
    'explicit_user_region',
]

EvidenceState = Literal[
    'exploratory',
    'predicted_unvalidated',
    'predicted_validated',
    'measured_verified',
]

ComparabilityState = Literal['comparable', 'incompatible_fidelity']


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


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


class InterventionFinding(BaseModel):
    """Structured problem ROI the study evaluates alternatives against.

    The finding binds an exact evidence authority where one exists
    (a prediction result, an evaluation, a measurement) or is an explicit
    user-declared region. It never asserts a causal label beyond the
    observable itself.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    source_kind: FindingSourceKind
    source_authority_id: str | None = Field(default=None, min_length=1)
    source_authority_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    observable: str = Field(min_length=1)
    target_band_hz: tuple[float, float] | None = None
    target_seat_entity_ids: tuple[str, ...] = ()
    detail: str | None = None

    @model_validator(mode='after')
    def valid_finding(self) -> 'InterventionFinding':
        if self.source_kind == 'explicit_user_region':
            if self.source_authority_id is not None or self.source_authority_sha256 is not None:
                raise ValueError(
                    'explicit user region must not carry an authority binding'
                )
        elif not (self.source_authority_id and self.source_authority_sha256):
            raise ValueError(
                'evidence-bound findings require exact authority id + sha256'
            )
        if self.target_band_hz is not None:
            low, high = self.target_band_hz
            if (
                not isfinite(float(low))
                or not isfinite(float(high))
                or low <= 0.0
                or high <= low
            ):
                raise ValueError('finding target band must be a finite ordered range')
        return self


class GuardrailDomain(BaseModel):
    """Independent regression observables — never penalties inside a score."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    guardrail_bands_hz: tuple[tuple[float, float], ...] = ()
    guardrail_seat_entity_ids: tuple[str, ...] = ()
    guardrail_observables: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid_domain(self) -> 'GuardrailDomain':
        for band in self.guardrail_bands_hz:
            low, high = band
            if (
                not isfinite(float(low))
                or not isfinite(float(high))
                or low <= 0.0
                or high <= low
            ):
                raise ValueError('guardrail bands must be finite ordered ranges')
        return self


class InterventionStudySpec(BaseModel):
    """Immutable counterfactual study contract bound to exact authorities."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = INTERVENTION_SCHEMA_VERSION
    authority_version: Literal[
        'intervention-study-1'
    ] = INTERVENTION_AUTHORITY_VERSION

    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    base_system_variant_id: str = Field(min_length=1)
    base_system_variant_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    finding: InterventionFinding
    guardrail_domain: GuardrailDomain = GuardrailDomain()
    allowed_families: tuple[InterventionFamily, ...] = Field(min_length=1)
    treatment_capability_authority_id: str | None = Field(
        default=None, min_length=1
    )
    treatment_capability_authority_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )

    metric_definitions: tuple[ObjectiveDefinition, ...] = Field(min_length=1)
    fidelity_label: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    provider_version: str = Field(min_length=1)
    robustness_policy: Literal['none', 'o90_bounded'] = 'none'
    evidence_state_floor: EvidenceState = 'exploratory'
    candidate_budget: int = Field(ge=1, le=50_000)
    algorithm_version: str = Field(min_length=1)

    spec_id: str = Field(min_length=1)
    spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    created_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_spec(self) -> 'InterventionStudySpec':
        if len(set(self.allowed_families)) != len(self.allowed_families):
            raise ValueError('intervention families must be unique')
        has_treatment_authority = (
            self.treatment_capability_authority_id is not None
            and self.treatment_capability_authority_sha256 is not None
        )
        if 'treatment' in self.allowed_families and not has_treatment_authority:
            raise ValueError(
                'treatment family requires explicit capability authority '
                '(#451/#516); it is never fabricated'
            )
        if 'treatment' not in self.allowed_families and has_treatment_authority:
            raise ValueError(
                'treatment capability authority requires the treatment family'
            )
        objective_ids = [item.objective_id for item in self.metric_definitions]
        if len(objective_ids) != len(set(objective_ids)):
            raise ValueError('metric definitions must be unique by objective_id')
        if self.spec_sha256 != _digest(self.identity_payload()):
            raise ValueError('intervention study spec hash mismatch')
        if self.spec_id != _semantic_id('intervention-study', self.spec_sha256):
            raise ValueError('intervention study spec ID mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'document_id': self.document_id,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'base_system_variant_id': self.base_system_variant_id,
            'base_system_variant_sha256': self.base_system_variant_sha256,
            'finding': self.finding.model_dump(mode='json'),
            'guardrail_domain': self.guardrail_domain.model_dump(mode='json'),
            'allowed_families': list(self.allowed_families),
            'treatment_capability_authority_id': self.treatment_capability_authority_id,
            'treatment_capability_authority_sha256': self.treatment_capability_authority_sha256,
            'metric_definitions': [
                item.model_dump(mode='json')
                for item in self.metric_definitions
            ],
            'fidelity_label': self.fidelity_label,
            'provider_id': self.provider_id,
            'provider_version': self.provider_version,
            'robustness_policy': self.robustness_policy,
            'evidence_state_floor': self.evidence_state_floor,
            'candidate_budget': self.candidate_budget,
            'algorithm_version': self.algorithm_version,
            'created_at_utc': self.created_at_utc,
        }


def build_intervention_study_spec(
    *,
    scene_revision: SceneRevision,
    base_variant: SystemVariant,
    finding: InterventionFinding,
    allowed_families: Sequence[InterventionFamily],
    metric_definitions: Sequence[ObjectiveDefinition],
    fidelity_label: str,
    provider_id: str,
    provider_version: str,
    algorithm_version: str,
    candidate_budget: int,
    created_at_utc: str,
    guardrail_domain: GuardrailDomain | None = None,
    treatment_capability_authority_id: str | None = None,
    treatment_capability_authority_sha256: str | None = None,
    robustness_policy: Literal['none', 'o90_bounded'] = 'none',
    evidence_state_floor: EvidenceState = 'exploratory',
) -> InterventionStudySpec:
    if (
        base_variant.document_id != scene_revision.document_id
        or base_variant.baseline_revision_id != scene_revision.revision_id
        or base_variant.baseline_content_hash != scene_revision.content_hash
    ):
        raise ValueError(
            'intervention study baseline variant must bind the exact SceneRevision'
        )
    identity: dict[str, Any] = {
        'schema_version': INTERVENTION_SCHEMA_VERSION,
        'authority_version': INTERVENTION_AUTHORITY_VERSION,
        'document_id': scene_revision.document_id,
        'scene_revision_id': scene_revision.revision_id,
        'scene_content_hash': scene_revision.content_hash,
        'base_system_variant_id': base_variant.variant_id,
        'base_system_variant_sha256': base_variant.variant_sha256,
        'finding': finding.model_dump(mode='json'),
        'guardrail_domain': (
            guardrail_domain or GuardrailDomain()
        ).model_dump(mode='json'),
        'allowed_families': list(allowed_families),
        'treatment_capability_authority_id': treatment_capability_authority_id,
        'treatment_capability_authority_sha256': treatment_capability_authority_sha256,
        'metric_definitions': [
            item.model_dump(mode='json') for item in metric_definitions
        ],
        'fidelity_label': fidelity_label,
        'provider_id': provider_id,
        'provider_version': provider_version,
        'robustness_policy': robustness_policy,
        'evidence_state_floor': evidence_state_floor,
        'candidate_budget': candidate_budget,
        'algorithm_version': algorithm_version,
        'created_at_utc': created_at_utc,
    }
    digest = _digest(identity)
    return InterventionStudySpec(
        document_id=scene_revision.document_id,
        scene_revision_id=scene_revision.revision_id,
        scene_content_hash=scene_revision.content_hash,
        base_system_variant_id=base_variant.variant_id,
        base_system_variant_sha256=base_variant.variant_sha256,
        finding=finding,
        guardrail_domain=guardrail_domain or GuardrailDomain(),
        allowed_families=tuple(allowed_families),
        treatment_capability_authority_id=treatment_capability_authority_id,
        treatment_capability_authority_sha256=treatment_capability_authority_sha256,
        metric_definitions=tuple(metric_definitions),
        fidelity_label=fidelity_label,
        provider_id=provider_id,
        provider_version=provider_version,
        robustness_policy=robustness_policy,
        evidence_state_floor=evidence_state_floor,
        candidate_budget=candidate_budget,
        algorithm_version=algorithm_version,
        spec_id=_semantic_id('intervention-study', digest),
        spec_sha256=digest,
        created_at_utc=created_at_utc,
    )


class InterventionMetric(BaseModel):
    """One independent observable value — never folded into a score."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    objective_id: str = Field(min_length=1)
    value: float | None = None
    state: Literal['available', 'missing', 'unsupported'] = 'available'
    domain: Literal['target_roi', 'guardrail'] = 'target_roi'

    @model_validator(mode='after')
    def valid_metric(self) -> 'InterventionMetric':
        if self.state == 'available':
            if self.value is None or not isfinite(float(self.value)):
                raise ValueError('available metric requires a finite value')
        elif self.value is not None:
            raise ValueError('missing/unsupported metric must not carry a value')
        return self


class InterventionSemanticDiff(BaseModel):
    """Exact machine-readable summary of what the alternative changes."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    changed_entity_ids: tuple[str, ...] = ()
    dsp_parameters: tuple[str, ...] = ()
    treatment_item_ids: tuple[str, ...] = ()
    topology_changes: tuple[str, ...] = ()
    summary: str = Field(min_length=1)


class InterventionAlternative(BaseModel):
    """One evaluated counterfactual bound to its generated authorities."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    schema_version: Literal[1] = INTERVENTION_SCHEMA_VERSION
    study_spec_id: str = Field(min_length=1)
    study_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    family: InterventionFamily
    semantic_diff: InterventionSemanticDiff
    generated_authority_ids: tuple[str, ...] = ()
    evidence_state: EvidenceState
    fidelity_label: str = Field(min_length=1)
    comparability: ComparabilityState
    incomparability_reason: str | None = None
    metrics: tuple[InterventionMetric, ...] = ()
    regressions: tuple[str, ...] = ()
    apply_instructions: str | None = None

    alternative_id: str = Field(min_length=1)
    alternative_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_alternative(self) -> 'InterventionAlternative':
        if self.comparability == 'comparable':
            if self.incomparability_reason is not None:
                raise ValueError(
                    'comparable alternative must not carry an incomparability reason'
                )
        elif not self.incomparability_reason:
            raise ValueError(
                'incompatible_fidelity alternative requires an explicit reason'
            )
        if self.alternative_sha256 != _digest(self.identity_payload()):
            raise ValueError('intervention alternative hash mismatch')
        if self.alternative_id != _semantic_id(
            'intervention-alt', self.alternative_sha256
        ):
            raise ValueError('intervention alternative ID mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'study_spec_id': self.study_spec_id,
            'study_spec_sha256': self.study_spec_sha256,
            'family': self.family,
            'semantic_diff': self.semantic_diff.model_dump(mode='json'),
            'generated_authority_ids': list(self.generated_authority_ids),
            'evidence_state': self.evidence_state,
            'fidelity_label': self.fidelity_label,
            'comparability': self.comparability,
            'incomparability_reason': self.incomparability_reason,
            'metrics': [item.model_dump(mode='json') for item in self.metrics],
            'regressions': list(self.regressions),
        }


def build_intervention_alternative(
    *,
    spec: InterventionStudySpec,
    family: InterventionFamily,
    semantic_diff: InterventionSemanticDiff,
    evidence_state: EvidenceState,
    fidelity_label: str,
    metrics: Sequence[InterventionMetric],
    generated_authority_ids: Sequence[str] = (),
    regressions: Sequence[str] = (),
    apply_instructions: str | None = None,
) -> InterventionAlternative:
    """Record one evaluated alternative against its study contract.

    Fail-closed semantics: the family must be allowed by the spec; metric ids
    must be declared by the spec's metric definitions; cross-family fidelity
    must match the spec label or the alternative is recorded
    ``incompatible_fidelity`` with an explicit reason.
    """
    if family not in spec.allowed_families:
        raise ValueError(
            f'intervention family {family} is not allowed by this study spec'
        )
    declared_ids = {item.objective_id for item in spec.metric_definitions}
    unknown = [
        item.objective_id
        for item in metrics
        if item.objective_id not in declared_ids
    ]
    if unknown:
        raise ValueError(
            f'alternative metrics are not declared by the study: {sorted(set(unknown))}'
        )
    if fidelity_label == spec.fidelity_label:
        comparability: ComparabilityState = 'comparable'
        incomparability_reason = None
    else:
        comparability = 'incompatible_fidelity'
        incomparability_reason = (
            f'alternative fidelity {fidelity_label!r} does not match study '
            f'fidelity {spec.fidelity_label!r} — quantitative cross-family '
            'comparison is not established'
        )

    identity: dict[str, Any] = {
        'schema_version': INTERVENTION_SCHEMA_VERSION,
        'study_spec_id': spec.spec_id,
        'study_spec_sha256': spec.spec_sha256,
        'family': family,
        'semantic_diff': semantic_diff.model_dump(mode='json'),
        'generated_authority_ids': list(generated_authority_ids),
        'evidence_state': evidence_state,
        'fidelity_label': fidelity_label,
        'comparability': comparability,
        'incomparability_reason': incomparability_reason,
        'metrics': [item.model_dump(mode='json') for item in metrics],
        'regressions': list(regressions),
    }
    digest = _digest(identity)
    return InterventionAlternative(
        study_spec_id=spec.spec_id,
        study_spec_sha256=spec.spec_sha256,
        family=family,
        semantic_diff=semantic_diff,
        generated_authority_ids=tuple(generated_authority_ids),
        evidence_state=evidence_state,
        fidelity_label=fidelity_label,
        comparability=comparability,
        incomparability_reason=incomparability_reason,
        metrics=tuple(metrics),
        regressions=tuple(regressions),
        apply_instructions=apply_instructions,
        alternative_id=_semantic_id('intervention-alt', digest),
        alternative_sha256=digest,
    )
