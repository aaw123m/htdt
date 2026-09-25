"""Application-aware current equipment authority (#984).

Applying a SystemVariant promotes the proposal's materialized
``SceneDocument`` into a new current ``SceneRevision`` plus a
``SystemVariantApplication`` lineage record, but the variant's exact
``EquipmentDefinition`` bindings live inside the proposal record only.
This module defines the canonical promotion rule (the issue's Option B):
current-state equipment authority derives from the revision graph —

    SceneRevision
    -> nearest SystemVariantApplication ancestry
    -> exact originating SystemVariant binding
    -> exact EquipmentDefinition

— so no EquipmentDefinition payload is duplicated into ``SceneEntity`` and
historical replays recover the binding effective at that exact revision.

Resolution rule for ``(revision R, speaker entity E)``:

- If the nearest applied-variant ancestor bound ``E``, that binding wins
  (effective at the application's ``selected_at_utc``).
- If the ancestor left ``E`` unchanged (``E`` exists in the applied
  revision but the variant bound nothing for it), the binding resolves
  recursively at the variant's baseline revision — unchanged speakers
  inherit their prior current binding.
- An explicit ``EquipmentBindingSemantics`` record visible at ``R``
  (``created_at_utc <= R.created_at_utc``) overrides the variant-derived
  binding only when strictly newer — that is an explicit later rebind.
- ``E`` added after the applied revision resolves explicit bindings only;
  an entity removed from ``R`` (or no longer a speaker) resolves nothing.

Apply never writes InstalledEquipment/as-built evidence: this resolver is
read-only and deterministically replayable over persisted state.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .cad_equipment_binding_repository import CadEquipmentBindingRepository
from .cad_repository import SceneRepository, SceneRevision
from .cad_system_variant import (
    ChannelRoleBinding,
    EquipmentBindingRef,
    SystemVariant,
    VariantProvenanceItem,
    build_system_variant,
)
from .cad_system_variant_repository import CadSystemVariantRepository


class ResolvedCurrentEquipmentBinding(BaseModel):
    """Exact equipment authority effective for one speaker at one revision."""

    model_config = ConfigDict(frozen=True)

    document_id: str = Field(min_length=1)
    revision_id: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    equipment_definition_id: str = Field(min_length=1)
    equipment_definition_version: str = Field(min_length=1)
    equipment_definition_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    resolution: str = Field(pattern=r'^(variant_application|explicit_binding)$')
    effective_at_utc: str = Field(min_length=1)
    variant_id: str | None = None
    variant_sha256: str | None = None
    application_id: str | None = None
    applied_revision_id: str | None = None
    binding_id: str | None = None
    binding_semantics_sha256: str | None = None


def _variant_chain_binding(
    revision: SceneRevision,
    entity_id: str,
    *,
    scene_repository: SceneRepository,
    variant_repository: CadSystemVariantRepository,
) -> ResolvedCurrentEquipmentBinding | None:
    """Nearest applied-ancestor binding, recursing through baselines."""

    lineage = variant_repository.proposal_lineage_for_revision(
        revision.revision_id
    )
    if lineage is None:
        return None
    application, variant = lineage
    applied = scene_repository.get(application.applied_revision_id)
    if applied is None:
        raise ValueError(
            'SystemVariantApplication applied SceneRevision is missing'
        )
    if entity_id not in {
        item.entity_id for item in applied.document.entities
    }:
        return None
    bound = next(
        (
            item
            for item in variant.equipment_bindings
            if item.entity_id == entity_id
        ),
        None,
    )
    if bound is not None:
        return ResolvedCurrentEquipmentBinding(
            document_id=revision.document_id,
            revision_id=revision.revision_id,
            entity_id=entity_id,
            equipment_definition_id=bound.equipment_definition_id,
            equipment_definition_version=bound.equipment_definition_version,
            equipment_definition_sha256=bound.equipment_definition_sha256,
            resolution='variant_application',
            effective_at_utc=application.selected_at_utc,
            variant_id=variant.variant_id,
            variant_sha256=variant.variant_sha256,
            application_id=application.application_id,
            applied_revision_id=application.applied_revision_id,
        )
    # The variant left this speaker unchanged: inherit the binding that was
    # current at the variant's baseline revision.
    baseline = scene_repository.get(variant.baseline_revision_id)
    if baseline is None:
        raise ValueError('SystemVariant baseline SceneRevision is missing')
    return _variant_chain_binding(
        baseline,
        entity_id,
        scene_repository=scene_repository,
        variant_repository=variant_repository,
    )


def resolve_current_equipment_binding(
    *,
    scene_repository: SceneRepository,
    variant_repository: CadSystemVariantRepository,
    revision_id: str,
    entity_id: str,
    binding_repository: CadEquipmentBindingRepository | None = None,
) -> ResolvedCurrentEquipmentBinding | None:
    """Resolve the equipment authority effective at an exact revision.

    Returns ``None`` when the entity is not a speaker in that revision or no
    equipment authority applies.
    """

    revision = scene_repository.get(revision_id)
    if revision is None:
        raise ValueError(f'SceneRevision {revision_id} is not persisted')
    entity = next(
        (
            item
            for item in revision.document.entities
            if item.entity_id == entity_id
        ),
        None,
    )
    if entity is None or entity.kind != 'speaker':
        return None

    candidates: list[ResolvedCurrentEquipmentBinding] = []
    variant_candidate = _variant_chain_binding(
        revision,
        entity_id,
        scene_repository=scene_repository,
        variant_repository=variant_repository,
    )
    if variant_candidate is not None:
        candidates.append(variant_candidate)
    if binding_repository is not None:
        semantics = binding_repository.latest_binding_for_entity_as_of(
            revision.document_id,
            entity_id,
            revision.created_at_utc,
        )
        if semantics is not None:
            candidates.append(
                ResolvedCurrentEquipmentBinding(
                    document_id=revision.document_id,
                    revision_id=revision.revision_id,
                    entity_id=entity_id,
                    equipment_definition_id=semantics.equipment.authority_id,
                    equipment_definition_version=semantics.equipment.version,
                    equipment_definition_sha256=(
                        semantics.equipment.semantic_sha256
                    ),
                    resolution='explicit_binding',
                    effective_at_utc=semantics.created_at_utc,
                    binding_id=semantics.binding_id,
                    binding_semantics_sha256=semantics.semantic_sha256,
                )
            )
    if not candidates:
        return None
    # A strictly newer effective time wins; a tie keeps the applied-variant
    # promotion over an explicit record (the apply is the promotion act).
    return max(
        candidates,
        key=lambda item: (
            item.effective_at_utc,
            item.resolution == 'variant_application',
        ),
    )


def current_equipment_projection(
    *,
    scene_repository: SceneRepository,
    variant_repository: CadSystemVariantRepository,
    revision_id: str,
    binding_repository: CadEquipmentBindingRepository | None = None,
) -> SystemVariant:
    """In-memory identity variant carrying the revision's resolved equipment.

    The projection is an empty-diff ``SystemVariant`` whose baseline is the
    given revision itself and whose ``equipment_bindings`` are the resolved
    current authorities — so ``compile_r110_source_model`` can compile the
    applied current state without pretending the applied revision is the
    old variant baseline. It is deterministic: ``variant_id`` is pinned to
    the revision and the semantic hash derives from resolved bindings.
    """

    revision = scene_repository.get(revision_id)
    if revision is None:
        raise ValueError(f'SceneRevision {revision_id} is not persisted')

    bindings: list[EquipmentBindingRef] = []
    roles: list[str] = []
    for entity in revision.document.entities:
        if entity.kind != 'speaker':
            continue
        if entity.speaker_role is not None and entity.speaker_role not in roles:
            roles.append(entity.speaker_role)
        resolved = resolve_current_equipment_binding(
            scene_repository=scene_repository,
            variant_repository=variant_repository,
            revision_id=revision_id,
            entity_id=entity.entity_id,
            binding_repository=binding_repository,
        )
        if resolved is None:
            continue
        bindings.append(
            EquipmentBindingRef(
                entity_id=entity.entity_id,
                equipment_definition_id=resolved.equipment_definition_id,
                equipment_definition_version=(
                    resolved.equipment_definition_version
                ),
                equipment_definition_sha256=(
                    resolved.equipment_definition_sha256
                ),
            )
        )

    variant = build_system_variant(
        baseline=revision,
        name='Current equipment state',
        role_bindings=tuple(
            ChannelRoleBinding(role_id=role, display_name=role)
            for role in roles
        ),
        proposed_entities=(),
        equipment_bindings=tuple(bindings),
        provenance=(
            VariantProvenanceItem(
                key='projection',
                value='current-equipment-resolution',
            ),
        ),
        created_at_utc=revision.created_at_utc,
    )
    return variant.model_copy(
        update={'variant_id': f'current-state:{revision.revision_id}'}
    )


__all__ = [
    'ResolvedCurrentEquipmentBinding',
    'resolve_current_equipment_binding',
    'current_equipment_projection',
]
