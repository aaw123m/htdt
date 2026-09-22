"""Criterion-observation evidence authority for standards evaluations (#410).

A ``CriterionObservation`` is never caller authority on its own: every
``CriterionEvidenceRef`` it carries is a typed reference that must re-resolve
against a registered evidence source at the persistence boundary and on every
authoritative read. The resolver projects the exact source record into the
canonical fields the observation claims — observed value/unit, evidence basis,
provided inputs, capabilities, entity and Scene/SystemVariant binding — and
the observation must match that canonical projection before
``evaluate_standards_profile`` runs.

The built-in ``standards_manual_observation`` kind resolves against
``StandardsObservationAuthority`` records retained by
``CadStandardsRepository``: typed, immutable, content-addressed observations
for quantities whose honest source is physical/manual inspection. Integrations
with richer sources (measurement/dataset/quality authorities, prediction and
video-geometry evaluations, equipment/directivity authorities) register their
own resolvers through ``CadStandardsRepository(evidence_resolvers=...)``; a
kind without a registered resolver fails closed.
"""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Literal,
    Mapping,
    NamedTuple,
    Sequence,
)

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cad_repository import SceneRepository, SceneRevision
from .cad_scene import SceneDocument
from .cad_standards import (
    CriterionDefinition,
    CriterionEvidenceRef,
    CriterionObservation,
    EvidenceBasis,
    ObservedScalar,
    StandardsEvaluation,
    _decimal,
    _digest,
    _finite_number,
)
from .cad_system_variant import SystemVariant


if TYPE_CHECKING:
    from .cad_standards_repository import CadStandardsRepository


STANDARDS_OBSERVATION_AUTHORITY_ID_PREFIX = 'standards-observation'
STANDARDS_OBSERVATION_AUTHORITY_VERSION = '1'
STANDARDS_MANUAL_OBSERVATION_KIND = 'standards_manual_observation'


class StandardsObservationAuthority(BaseModel):
    """Typed immutable observation authority bound to one exact standards target.

    Where physical/manual inspection is the honest evidence source, the claim
    is recorded as one exact typed authority — not an arbitrary string ref.
    The authority pins the exact Standards target binding (document,
    SceneRevision + content hash, optional SystemVariant + hash, entity ids)
    and the canonical projection an observation may claim: the observed
    ``quantity``/``unit``/``observed_value``, its ``evidence_basis``, and the
    input/capability labels the observation can honestly support.
    ``authority_id`` and ``semantic_hash_sha256`` are derived from the
    semantic payload, so any change to the binding or the attested value is a
    different authority.
    """

    model_config = ConfigDict(frozen=True)

    @field_validator('entity_ids', 'provided_inputs', 'capabilities')
    @classmethod
    def canonical_string_set(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(values))

    authority_id: str = Field(
        pattern=rf'^{STANDARDS_OBSERVATION_AUTHORITY_ID_PREFIX}:[0-9a-f]{{64}}$'
    )
    authority_version: Literal['1'] = STANDARDS_OBSERVATION_AUTHORITY_VERSION
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    system_variant_id: str | None = Field(default=None, min_length=1)
    system_variant_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    quantity: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    observed_value: ObservedScalar
    evidence_basis: EvidenceBasis
    entity_ids: tuple[str, ...] = ()
    provided_inputs: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    observed_at_utc: str = Field(min_length=1)
    observer: str | None = Field(default=None, min_length=1)
    method: str | None = Field(default=None, min_length=1)
    note: str | None = Field(default=None, min_length=1)
    semantic_hash_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_authority(self) -> 'StandardsObservationAuthority':
        if (self.system_variant_id is None) != (self.system_variant_sha256 is None):
            raise ValueError('SystemVariant id/hash must be supplied together')
        for label, values in (
            ('entity ids', self.entity_ids),
            ('provided inputs', self.provided_inputs),
            ('capabilities', self.capabilities),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f'observation authority {label} must be unique')
            if any(not value for value in values):
                raise ValueError(
                    f'observation authority {label} must not contain empty values'
                )
        if _finite_number(self.observed_value):
            _decimal(self.observed_value)
        expected = _digest(self.semantic_payload())
        if self.semantic_hash_sha256 != expected:
            raise ValueError('StandardsObservationAuthority semantic hash mismatch')
        expected_id = f'{STANDARDS_OBSERVATION_AUTHORITY_ID_PREFIX}:{expected}'
        if self.authority_id != expected_id:
            raise ValueError('StandardsObservationAuthority id mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='json',
            exclude={'authority_id', 'semantic_hash_sha256'},
        )

    def ref(self) -> CriterionEvidenceRef:
        return CriterionEvidenceRef(
            kind=STANDARDS_MANUAL_OBSERVATION_KIND,
            evidence_id=self.authority_id,
            evidence_sha256=self.semantic_hash_sha256,
        )


def build_standards_observation_authority(
    *,
    document_id: str,
    scene_revision_id: str,
    scene_content_hash: str,
    quantity: str,
    unit: str,
    observed_value: ObservedScalar,
    evidence_basis: EvidenceBasis,
    observed_at_utc: str,
    system_variant_id: str | None = None,
    system_variant_sha256: str | None = None,
    entity_ids: Sequence[str] = (),
    provided_inputs: Sequence[str] = (),
    capabilities: Sequence[str] = (),
    observer: str | None = None,
    method: str | None = None,
    note: str | None = None,
) -> StandardsObservationAuthority:
    payload = {
        'authority_version': STANDARDS_OBSERVATION_AUTHORITY_VERSION,
        'document_id': document_id,
        'scene_revision_id': scene_revision_id,
        'scene_content_hash': scene_content_hash,
        'system_variant_id': system_variant_id,
        'system_variant_sha256': system_variant_sha256,
        'quantity': quantity,
        'unit': unit,
        'observed_value': observed_value,
        'evidence_basis': evidence_basis,
        'entity_ids': sorted(set(entity_ids)),
        'provided_inputs': sorted(set(provided_inputs)),
        'capabilities': sorted(set(capabilities)),
        'observed_at_utc': observed_at_utc,
        'observer': observer,
        'method': method,
        'note': note,
    }
    digest = _digest(payload)
    return StandardsObservationAuthority(
        authority_id=f'{STANDARDS_OBSERVATION_AUTHORITY_ID_PREFIX}:{digest}',
        document_id=document_id,
        scene_revision_id=scene_revision_id,
        scene_content_hash=scene_content_hash,
        system_variant_id=system_variant_id,
        system_variant_sha256=system_variant_sha256,
        quantity=quantity,
        unit=unit,
        observed_value=observed_value,
        evidence_basis=evidence_basis,
        entity_ids=tuple(sorted(set(entity_ids))),
        provided_inputs=tuple(sorted(set(provided_inputs))),
        capabilities=tuple(sorted(set(capabilities))),
        observed_at_utc=observed_at_utc,
        observer=observer,
        method=method,
        note=note,
        semantic_hash_sha256=digest,
    )


class ResolvedCriterionEvidence(NamedTuple):
    """One criterion-observation evidence ref resolved against exact authority.

    The resolver projects the source record into the canonical fields a
    ``CriterionObservation`` may claim. ``observed_value``/``unit`` are the
    exact quantity the evidence attests for the criterion, or ``None`` when
    the ref only supports inputs/capabilities. ``document_id``,
    ``scene_revision_id``, ``scene_content_hash``, ``system_variant_id`` and
    ``system_variant_sha256`` declare the exact binding the resolved authority
    carries; declared values must equal the evaluation target, and
    scene-bound evidence must bind the target SystemVariant exactly
    (``None`` fields mark scene-independent authorities such as equipment
    specifications).
    """

    ref: CriterionEvidenceRef
    # Semantic hash of the exact evidence consumed; required whenever the ref
    # pins ``evidence_sha256``.
    source_sha256: str | None
    # The strongest evidence basis this ref honestly supports.
    evidence_basis: EvidenceBasis
    observed_value: ObservedScalar | None = None
    unit: str | None = None
    provided_inputs: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    entity_ids: tuple[str, ...] = ()
    document_id: str | None = None
    scene_revision_id: str | None = None
    scene_content_hash: str | None = None
    system_variant_id: str | None = None
    system_variant_sha256: str | None = None
    # The resolved upstream record for callers that need more than the
    # projected fields.
    authority: Any = None


class CriterionEvidenceContext(NamedTuple):
    """Everything a criterion-observation evidence resolver may rely on."""

    evaluation: StandardsEvaluation
    criterion: CriterionDefinition
    observation: CriterionObservation
    scene_revision: SceneRevision
    # The exact target document: the baseline scene, or the materialized
    # SystemVariant when the evaluation is variant-bound.
    document: SceneDocument
    system_variant: SystemVariant | None
    scene_repository: SceneRepository
    standards_repository: 'CadStandardsRepository'

    @property
    def target(self):
        return self.evaluation.target


CriterionEvidenceResolver = Callable[
    [CriterionEvidenceContext, CriterionEvidenceRef],
    ResolvedCriterionEvidence,
]


def _scalars_equal(left: object, right: object) -> bool:
    if _finite_number(left) and _finite_number(right):
        return _decimal(left) == _decimal(right)
    return left == right


def validate_observation_evidence(
    observation: CriterionObservation,
    resolved: Sequence[ResolvedCriterionEvidence],
) -> None:
    """Require an observation to match the canonical resolved projection.

    Every value-bearing ref must agree on the exact observed value/unit; a
    claimed ``observed_value`` must be attested by that consensus, and the
    claimed ``evidence_basis`` must be honestly supported by the
    value-attesting refs (so a ``measured`` claim needs measured evidence).
    Claimed ``provided_inputs``/``capabilities`` must not exceed the union the
    resolved evidence supports, and every claimed ``entity_ids`` member must
    be covered by the value-attesting evidence (or by any resolved ref when
    no value is claimed). Anything else is fabricated or unbacked caller
    authority and is rejected.
    """

    criterion_id = observation.criterion_id
    valued = tuple(item for item in resolved if item.observed_value is not None)
    first = valued[0] if valued else None
    for item in valued[1:]:
        if (
            not _scalars_equal(item.observed_value, first.observed_value)
            or item.unit != first.unit
        ):
            raise ValueError(
                f'criterion {criterion_id} resolved evidence disagrees on the '
                'observed quantity'
            )

    if observation.observed_value is not None:
        if first is None:
            raise ValueError(
                f'criterion {criterion_id} resolved evidence does not attest '
                'the claimed observed value'
            )
        if not _scalars_equal(first.observed_value, observation.observed_value):
            raise ValueError(
                f'criterion {criterion_id} observed value is inconsistent with '
                'resolved evidence'
            )
        if first.unit != observation.unit:
            raise ValueError(
                f'criterion {criterion_id} observed unit is inconsistent with '
                'resolved evidence'
            )
        if observation.evidence_basis is not None and not any(
            item.evidence_basis == observation.evidence_basis for item in valued
        ):
            raise ValueError(
                f'criterion {criterion_id} evidence basis '
                f'{observation.evidence_basis} is not supported by the '
                'value-attesting evidence'
            )
    elif observation.evidence_basis is not None and not any(
        item.evidence_basis == observation.evidence_basis for item in resolved
    ):
        raise ValueError(
            f'criterion {criterion_id} evidence basis '
            f'{observation.evidence_basis} is not supported by resolved evidence'
        )

    provided = {label for item in resolved for label in item.provided_inputs}
    capabilities = {label for item in resolved for label in item.capabilities}
    excess_inputs = set(observation.provided_inputs) - provided
    excess_capabilities = set(observation.capabilities) - capabilities
    if excess_inputs or excess_capabilities:
        raise ValueError(
            f'criterion {criterion_id} claims inputs/capabilities beyond '
            'resolved evidence: '
            f'inputs={sorted(excess_inputs)} '
            f'capabilities={sorted(excess_capabilities)}'
        )

    coverage_pool = valued if observation.observed_value is not None else resolved
    covered = {entity for item in coverage_pool for entity in item.entity_ids}
    uncovered = set(observation.entity_ids) - covered
    if uncovered:
        raise ValueError(
            f'criterion {criterion_id} entity binding is not covered by '
            f'resolved evidence: {sorted(uncovered)}'
        )


def _validate_evidence_target_binding(
    context: CriterionEvidenceContext,
    resolved: ResolvedCriterionEvidence,
) -> None:
    """Prove resolved evidence belongs to the exact evaluation target.

    A resolver declares which target coordinates its authority is bound to.
    Scene-bound evidence must declare the full SceneRevision identity and the
    exact SystemVariant binding of the target (both ``None`` for a baseline
    target): scene evidence for another revision, document, content hash, or
    variant is never evidence for this evaluation. Fully scene-independent
    authorities (equipment specifications and similar) declare no scene or
    variant binding at all; a variant binding without a scene binding is
    malformed and rejected. Scene-bound entity ids must exist in the exact
    target document.
    """

    criterion_id = context.criterion.criterion_id
    ref = resolved.ref
    if (
        ref.evidence_sha256 is not None
        and resolved.source_sha256 != ref.evidence_sha256
    ):
        raise ValueError(
            f'criterion {criterion_id} evidence semantic hash mismatch: '
            f'{ref.kind}:{ref.evidence_id}'
        )

    target = context.target
    scene_fields = (
        resolved.document_id,
        resolved.scene_revision_id,
        resolved.scene_content_hash,
    )
    if not any(field is not None for field in scene_fields):
        if (
            resolved.system_variant_id is not None
            or resolved.system_variant_sha256 is not None
        ):
            raise ValueError(
                f'criterion {criterion_id} evidence binds a SystemVariant '
                'without binding the SceneRevision'
            )
        return
    if not all(field is not None for field in scene_fields):
        raise ValueError(
            f'criterion {criterion_id} evidence scene binding is incomplete'
        )
    if scene_fields != (
        target.document_id,
        target.scene_revision_id,
        target.scene_content_hash,
    ):
        raise ValueError(
            f'criterion {criterion_id} evidence is bound to another '
            'SceneRevision'
        )
    if (resolved.system_variant_id, resolved.system_variant_sha256) != (
        target.system_variant_id,
        target.system_variant_sha256,
    ):
        raise ValueError(
            f'criterion {criterion_id} evidence is bound to another '
            'SystemVariant'
        )
    known_entities = {
        entity.entity_id for entity in context.document.entities
    }
    unknown = set(resolved.entity_ids) - known_entities
    if unknown:
        raise ValueError(
            f'criterion {criterion_id} evidence binds entities outside the '
            f'target document: {sorted(unknown)}'
        )


def resolve_criterion_observation_evidence(
    context: CriterionEvidenceContext,
    resolvers: Mapping[str, CriterionEvidenceResolver],
) -> tuple[ResolvedCriterionEvidence, ...]:
    """Resolve every evidence ref on the observation against typed authority.

    Each ``CriterionEvidenceRef.kind`` selects the registered resolver; a kind
    without a resolver fails closed. Resolved evidence must carry the exact
    semantic hash the ref pins and must prove it belongs to the exact
    Scene/SystemVariant/entity scope of the evaluation target.
    """

    resolved_items: list[ResolvedCriterionEvidence] = []
    for ref in context.observation.evidence_refs:
        resolver = resolvers.get(ref.kind)
        if resolver is None:
            raise ValueError(
                f'criterion {context.criterion.criterion_id} evidence kind '
                f'has no resolver: {ref.kind}'
            )
        resolved = resolver(context, ref)
        if resolved.ref != ref:
            raise ValueError(
                f'criterion {context.criterion.criterion_id} evidence '
                'resolver returned a mismatched ref'
            )
        _validate_evidence_target_binding(context, resolved)
        resolved_items.append(resolved)
    return tuple(resolved_items)


def resolve_standards_manual_observation(
    context: CriterionEvidenceContext,
    ref: CriterionEvidenceRef,
) -> ResolvedCriterionEvidence:
    """Resolve one retained ``StandardsObservationAuthority`` exactly."""

    authority = context.standards_repository.get_observation_authority(
        ref.evidence_id
    )
    if authority is None:
        raise ValueError(
            f'criterion {context.criterion.criterion_id} evidence ref does not '
            f'resolve: {ref.kind}:{ref.evidence_id}'
        )
    if authority.quantity != context.criterion.quantity:
        raise ValueError(
            f'criterion {context.criterion.criterion_id} observation authority '
            'does not attest the criterion quantity'
        )
    if authority.unit != context.criterion.unit:
        raise ValueError(
            f'criterion {context.criterion.criterion_id} observation authority '
            'unit does not match the criterion'
        )
    return ResolvedCriterionEvidence(
        ref=ref,
        source_sha256=authority.semantic_hash_sha256,
        evidence_basis=authority.evidence_basis,
        observed_value=authority.observed_value,
        unit=authority.unit,
        provided_inputs=tuple(
            sorted(set(authority.provided_inputs) | {authority.quantity})
        ),
        capabilities=authority.capabilities,
        entity_ids=authority.entity_ids,
        document_id=authority.document_id,
        scene_revision_id=authority.scene_revision_id,
        scene_content_hash=authority.scene_content_hash,
        system_variant_id=authority.system_variant_id,
        system_variant_sha256=authority.system_variant_sha256,
        authority=authority,
    )


STANDARDS_EVIDENCE_RESOLVERS: dict[str, CriterionEvidenceResolver] = {
    STANDARDS_MANUAL_OBSERVATION_KIND: resolve_standards_manual_observation,
}
