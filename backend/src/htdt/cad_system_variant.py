from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Literal, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_document import WorkingDocument
from .cad_repository import SceneRevision
from .cad_scene import PHYSICAL_ENTITY_KINDS, SceneDocument, SceneEntity, scene_content_hash


SYSTEM_VARIANT_SCHEMA_VERSION = 1
SYSTEM_VARIANT_AUTHORITY_VERSION = 'o100a-system-variant-2'
SYSTEM_VARIANT_EQUIPMENT_AUTHORITY_VERSION = 'o100a-system-variant-3'
SystemVariantAuthorityVersion = Literal[
    'o100a-system-variant-1',
    'o100a-system-variant-2',
    'o100a-system-variant-3',
]

LifecycleState = Literal['current', 'proposed', 'as_built', 'measured']
VariantDiffKind = Literal['add', 'remove', 'replace']
ProposalEvidenceKind = Literal[
    'prediction',
    'objective',
    'robustness',
    'comparison',
    'user_decision',
]


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


class VariantProvenanceItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str = Field(min_length=1)
    value: str = Field(min_length=1)


class ChannelRoleBinding(BaseModel):
    """Versioned role identity without imposing a fixed speaker-role schema."""

    model_config = ConfigDict(frozen=True)

    role_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    layout_profile_id: str | None = Field(default=None, min_length=1)
    layout_profile_version: str | None = Field(default=None, min_length=1)
    paired_role_id: str | None = Field(default=None, min_length=1)
    provenance: tuple[VariantProvenanceItem, ...] = ()

    @model_validator(mode='after')
    def valid_role(self) -> 'ChannelRoleBinding':
        if self.paired_role_id == self.role_id:
            raise ValueError('channel role cannot be paired with itself')
        if (self.layout_profile_id is None) != (self.layout_profile_version is None):
            raise ValueError('layout profile id/version must be supplied together')
        keys = [item.key for item in self.provenance]
        if len(keys) != len(set(keys)):
            raise ValueError('channel role provenance keys must be unique')
        return self


class EquipmentBindingRef(BaseModel):
    """Exact source-entity reference to immutable O100C equipment authority."""

    model_config = ConfigDict(frozen=True)

    entity_id: str = Field(min_length=1)
    equipment_definition_id: str = Field(min_length=1)
    equipment_definition_version: str = Field(min_length=1)
    equipment_definition_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProposedEntitySpec(BaseModel):
    """Exact hypothetical physical entity payload without measurement evidence."""

    model_config = ConfigDict(frozen=True)

    spec_id: str = Field(min_length=1)
    entity: SceneEntity
    role_binding_id: str | None = Field(default=None, min_length=1)
    lifecycle: Literal['proposed'] = 'proposed'
    provenance: tuple[VariantProvenanceItem, ...] = ()

    @model_validator(mode='after')
    def valid_proposed_entity(self) -> 'ProposedEntitySpec':
        if self.entity.kind not in PHYSICAL_ENTITY_KINDS:
            raise ValueError('ProposedEntitySpec requires a physical SceneEntity')
        if self.entity.kind == 'speaker':
            if self.role_binding_id is None:
                raise ValueError('proposed speaker requires a ChannelRoleBinding')
            if self.entity.speaker_role != self.role_binding_id:
                raise ValueError('proposed speaker role must match ChannelRoleBinding role_id')
        elif self.role_binding_id is not None:
            raise ValueError('non-speaker proposed entities must not use speaker role bindings')
        keys = [item.key for item in self.provenance]
        if len(keys) != len(set(keys)):
            raise ValueError('proposed entity provenance keys must be unique')
        return self


class ProposalEvidenceRef(BaseModel):
    """Reference to proposal evidence; physical measurement is intentionally not a valid kind."""

    model_config = ConfigDict(frozen=True)

    evidence_kind: ProposalEvidenceKind
    evidence_id: str = Field(min_length=1)
    evidence_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    detail: str | None = Field(default=None, min_length=1)


class EntityLifecycleBinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    entity_id: str = Field(min_length=1)
    state: LifecycleState
    measurement_ids: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid_measurement_state(self) -> 'EntityLifecycleBinding':
        if len(self.measurement_ids) != len(set(self.measurement_ids)):
            raise ValueError('measurement ids must be unique')
        if self.state == 'measured' and not self.measurement_ids:
            raise ValueError('measured lifecycle state requires measurement evidence')
        if self.state != 'measured' and self.measurement_ids:
            raise ValueError('measurement evidence is valid only for measured lifecycle state')
        return self


class VariantEntityDiff(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: VariantDiffKind
    entity_id: str = Field(min_length=1)
    before_entity: SceneEntity | None = None
    after_entity: SceneEntity | None = None
    insertion_index: int = Field(ge=0)

    @model_validator(mode='after')
    def valid_diff(self) -> 'VariantEntityDiff':
        before = self.before_entity
        after = self.after_entity
        if self.kind == 'add':
            if before is not None or after is None:
                raise ValueError('add diff requires only after_entity')
        elif self.kind == 'remove':
            if before is None or after is not None:
                raise ValueError('remove diff requires only before_entity')
        else:
            if before is None or after is None:
                raise ValueError('replace diff requires before_entity and after_entity')
        for entity in (before, after):
            if entity is not None and entity.entity_id != self.entity_id:
                raise ValueError('variant diff entity_id mismatch')
        return self


class SystemVariant(BaseModel):
    """Immutable O100A system proposal bound to one exact baseline SceneRevision."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = SYSTEM_VARIANT_SCHEMA_VERSION
    authority_version: SystemVariantAuthorityVersion = SYSTEM_VARIANT_AUTHORITY_VERSION
    variant_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    baseline_revision_id: str = Field(min_length=1)
    baseline_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    parent_variant_id: str | None = Field(default=None, min_length=1)
    role_bindings: tuple[ChannelRoleBinding, ...]
    proposed_entities: tuple[ProposedEntitySpec, ...]
    diff: tuple[VariantEntityDiff, ...]
    entity_lifecycle: tuple[EntityLifecycleBinding, ...]
    equipment_bindings: tuple[EquipmentBindingRef, ...] = ()
    proposal_evidence: tuple[ProposalEvidenceRef, ...] = ()
    provenance: tuple[VariantProvenanceItem, ...] = ()
    created_at_utc: str = Field(min_length=1)
    variant_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_variant(self) -> 'SystemVariant':
        role_ids = [item.role_id for item in self.role_bindings]
        if len(role_ids) != len(set(role_ids)):
            raise ValueError('SystemVariant role bindings must be unique')
        spec_ids = [item.spec_id for item in self.proposed_entities]
        if len(spec_ids) != len(set(spec_ids)):
            raise ValueError('SystemVariant proposed entity spec ids must be unique')
        proposed_entity_ids = [item.entity.entity_id for item in self.proposed_entities]
        if len(proposed_entity_ids) != len(set(proposed_entity_ids)):
            raise ValueError('SystemVariant proposed entity ids must be unique')
        role_set = set(role_ids)
        for item in self.role_bindings:
            if item.paired_role_id is not None and item.paired_role_id not in role_set:
                raise ValueError('ChannelRoleBinding paired role is not present in the variant')
        for item in self.proposed_entities:
            if item.role_binding_id is not None and item.role_binding_id not in role_set:
                raise ValueError('proposed speaker references unknown ChannelRoleBinding')
        diff_ids = [item.entity_id for item in self.diff]
        if len(diff_ids) != len(set(diff_ids)):
            raise ValueError('SystemVariant diff entity ids must be unique')
        proposed_set = set(proposed_entity_ids)
        changed_proposed_set = {
            item.entity_id
            for item in self.diff
            if item.kind in {'add', 'replace'}
        }
        if changed_proposed_set != proposed_set:
            raise ValueError('SystemVariant add/replace diff must exactly match ProposedEntitySpec set')
        lifecycle_ids = [item.entity_id for item in self.entity_lifecycle]
        if len(lifecycle_ids) != len(set(lifecycle_ids)):
            raise ValueError('SystemVariant lifecycle bindings must be unique')
        lifecycle_proposed_set = {
            item.entity_id
            for item in self.entity_lifecycle
            if item.state == 'proposed'
        }
        if lifecycle_proposed_set != proposed_set:
            raise ValueError('proposed lifecycle bindings must exactly match ProposedEntitySpec set')
        equipment_entity_ids = [item.entity_id for item in self.equipment_bindings]
        if len(equipment_entity_ids) != len(set(equipment_entity_ids)):
            raise ValueError('SystemVariant equipment bindings must be unique per entity')
        if self.authority_version != 'o100a-system-variant-3' and self.equipment_bindings:
            raise ValueError(
                'legacy SystemVariant authority cannot carry equipment bindings'
            )
        if not set(equipment_entity_ids).issubset(set(lifecycle_ids)):
            raise ValueError(
                'SystemVariant equipment binding references an untracked entity'
            )
        provenance_keys = [item.key for item in self.provenance]
        if len(provenance_keys) != len(set(provenance_keys)):
            raise ValueError('SystemVariant provenance keys must be unique')
        if self.variant_sha256 != _digest(self.identity_payload()):
            raise ValueError('SystemVariant identity hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload = {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'name': self.name,
            'document_id': self.document_id,
            'baseline_revision_id': self.baseline_revision_id,
            'baseline_content_hash': self.baseline_content_hash,
            'parent_variant_id': self.parent_variant_id,
            'role_bindings': [item.model_dump(mode='json') for item in self.role_bindings],
            'proposed_entities': [item.model_dump(mode='json') for item in self.proposed_entities],
            'diff': [item.model_dump(mode='json') for item in self.diff],
            'entity_lifecycle': [item.model_dump(mode='json') for item in self.entity_lifecycle],
            'proposal_evidence': [item.model_dump(mode='json') for item in self.proposal_evidence],
            'provenance': [item.model_dump(mode='json') for item in self.provenance],
        }
        if self.authority_version == 'o100a-system-variant-3':
            payload['equipment_bindings'] = [
                item.model_dump(mode='json')
                for item in self.equipment_bindings
            ]
        return payload


def _validate_baseline(variant: SystemVariant, baseline: SceneRevision) -> None:
    if baseline.revision_id != variant.baseline_revision_id:
        raise ValueError('SystemVariant baseline revision mismatch')
    if baseline.document_id != variant.document_id:
        raise ValueError('SystemVariant baseline document mismatch')
    if baseline.content_hash != variant.baseline_content_hash:
        raise ValueError('SystemVariant baseline content hash mismatch')


def materialize_system_variant(
    baseline: SceneRevision,
    variant: SystemVariant,
) -> SceneDocument:
    """Derive the exact proposed scene without mutating the baseline revision."""

    _validate_baseline(variant, baseline)
    baseline_by_id = {
        entity.entity_id: entity
        for entity in baseline.document.entities
    }
    baseline_index = {
        entity.entity_id: index
        for index, entity in enumerate(baseline.document.entities)
    }
    removed: set[str] = set()
    replacements: dict[str, SceneEntity] = {}

    for change in variant.diff:
        if change.kind == 'add':
            continue
        existing = baseline_by_id.get(change.entity_id)
        if existing is None:
            raise ValueError(f'variant diff references missing baseline entity: {change.entity_id}')
        if existing != change.before_entity:
            raise ValueError(f'variant diff before_entity mismatch: {change.entity_id}')
        if baseline_index[change.entity_id] != change.insertion_index:
            raise ValueError(f'variant diff baseline index mismatch: {change.entity_id}')
        if change.kind == 'remove':
            removed.add(change.entity_id)
        else:
            assert change.after_entity is not None
            replacements[change.entity_id] = change.after_entity

    entities = [
        replacements.get(entity.entity_id, entity)
        for entity in baseline.document.entities
        if entity.entity_id not in removed
    ]
    final_ids = {entity.entity_id for entity in entities}

    for change in variant.diff:
        if change.kind != 'add':
            continue
        if change.entity_id in final_ids:
            raise ValueError(f'variant add duplicates entity_id: {change.entity_id}')
        if change.after_entity is None:
            raise ValueError('variant add is missing after_entity')
        if change.insertion_index != len(entities):
            raise ValueError('variant add insertion index no longer matches exact derivation')
        entities.append(change.after_entity)
        final_ids.add(change.entity_id)

    proposed_by_id = {
        item.entity.entity_id: item.entity
        for item in variant.proposed_entities
    }
    final_by_id = {entity.entity_id: entity for entity in entities}
    for entity_id, proposed in proposed_by_id.items():
        if final_by_id.get(entity_id) != proposed:
            raise ValueError(f'proposed entity does not match exact variant diff: {entity_id}')

    final_entity_ids = {entity.entity_id for entity in entities}
    final_speaker_ids = {
        entity.entity_id
        for entity in entities
        if entity.kind == 'speaker'
    }
    proposed_entity_ids = {
        item.entity.entity_id
        for item in variant.proposed_entities
    }
    lifecycle_ids = {item.entity_id for item in variant.entity_lifecycle}
    if not final_speaker_ids.issubset(lifecycle_ids):
        raise ValueError('SystemVariant lifecycle bindings must cover the exact final speaker set')
    if not proposed_entity_ids.issubset(lifecycle_ids):
        raise ValueError('SystemVariant lifecycle bindings must cover every proposed entity')
    if not lifecycle_ids.issubset(final_entity_ids):
        raise ValueError('SystemVariant lifecycle binding references a missing final entity')

    return baseline.document.model_copy(update={'entities': tuple(entities)})

def build_system_variant(
    *,
    baseline: SceneRevision,
    name: str,
    role_bindings: Sequence[ChannelRoleBinding],
    proposed_entities: Sequence[ProposedEntitySpec],
    remove_entity_ids: Sequence[str] = (),
    lifecycle_overrides: Sequence[EntityLifecycleBinding] = (),
    equipment_bindings: Sequence[EquipmentBindingRef] = (),
    proposal_evidence: Sequence[ProposalEvidenceRef] = (),
    provenance: Sequence[VariantProvenanceItem] = (),
    parent_variant_id: str | None = None,
    created_at_utc: str,
) -> SystemVariant:
    """Create an exact add/remove/replace proposal from a fixed SceneRevision."""

    roles = tuple(role_bindings)
    proposals = tuple(proposed_entities)
    removals = tuple(dict.fromkeys(remove_entity_ids))
    binding_items = tuple(equipment_bindings)
    evidence = tuple(proposal_evidence)
    provenance_items = tuple(provenance)

    baseline_entities = {entity.entity_id: entity for entity in baseline.document.entities}
    proposal_by_id = {item.entity.entity_id: item for item in proposals}
    if len(proposal_by_id) != len(proposals):
        raise ValueError('proposed entity ids must be unique')
    overlap = set(removals).intersection(proposal_by_id)
    if overlap:
        raise ValueError(f'entity cannot be both removed and proposed: {sorted(overlap)}')
    missing_removals = set(removals) - set(baseline_entities)
    if missing_removals:
        raise ValueError(f'cannot remove missing baseline entities: {sorted(missing_removals)}')
    invalid_removals = {
        entity_id
        for entity_id in removals
        if baseline_entities[entity_id].kind not in PHYSICAL_ENTITY_KINDS
    }
    if invalid_removals:
        raise ValueError(
            'SystemVariant remove operations require physical entities: '
            f'{sorted(invalid_removals)}'
        )
    invalid_replacements = {
        entity_id
        for entity_id, proposal in proposal_by_id.items()
        if entity_id in baseline_entities
        and (
            baseline_entities[entity_id].kind not in PHYSICAL_ENTITY_KINDS
            or baseline_entities[entity_id].kind != proposal.entity.kind
        )
    }
    if invalid_replacements:
        raise ValueError(
            'SystemVariant replacements must preserve physical entity kind: '
            f'{sorted(invalid_replacements)}'
        )

    role_by_id = {item.role_id: item for item in roles}
    if len(role_by_id) != len(roles):
        raise ValueError('ChannelRoleBinding role ids must be unique')
    for proposal in proposals:
        if proposal.role_binding_id is not None and proposal.role_binding_id not in role_by_id:
            raise ValueError('proposed speaker references unknown ChannelRoleBinding')

    diff: list[VariantEntityDiff] = []
    for index, entity in enumerate(baseline.document.entities):
        if entity.entity_id in removals:
            diff.append(VariantEntityDiff(
                kind='remove',
                entity_id=entity.entity_id,
                before_entity=entity,
                insertion_index=index,
            ))
            continue
        proposal = proposal_by_id.get(entity.entity_id)
        if proposal is not None:
            diff.append(VariantEntityDiff(
                kind='replace',
                entity_id=entity.entity_id,
                before_entity=entity,
                after_entity=proposal.entity,
                insertion_index=index,
            ))

    final_count_before_add = len(baseline.document.entities) - len(removals)
    add_offset = 0
    for proposal in proposals:
        if proposal.entity.entity_id in baseline_entities:
            continue
        diff.append(VariantEntityDiff(
            kind='add',
            entity_id=proposal.entity.entity_id,
            after_entity=proposal.entity,
            insertion_index=final_count_before_add + add_offset,
        ))
        add_offset += 1

    overrides = {item.entity_id: item for item in lifecycle_overrides}
    if len(overrides) != len(tuple(lifecycle_overrides)):
        raise ValueError('lifecycle override entity ids must be unique')
    if set(overrides).intersection(proposal_by_id):
        raise ValueError('proposed entity lifecycle cannot be overridden')
    valid_final_ids = (
        (set(baseline_entities) - set(removals))
        | set(proposal_by_id)
    )
    unknown_overrides = set(overrides) - valid_final_ids
    if unknown_overrides:
        raise ValueError(f'lifecycle override references missing final entity: {sorted(unknown_overrides)}')

    lifecycle: list[EntityLifecycleBinding] = []
    final_entities: list[SceneEntity] = []
    for entity in baseline.document.entities:
        if entity.entity_id in removals:
            continue
        replacement = proposal_by_id.get(entity.entity_id)
        final_entities.append(replacement.entity if replacement is not None else entity)
    final_entities.extend(
        item.entity
        for item in proposals
        if item.entity.entity_id not in baseline_entities
    )
    final_by_id = {entity.entity_id: entity for entity in final_entities}
    invalid_overrides = {
        entity_id
        for entity_id in overrides
        if final_by_id[entity_id].kind not in PHYSICAL_ENTITY_KINDS
    }
    if invalid_overrides:
        raise ValueError(
            'lifecycle overrides require physical entities: '
            f'{sorted(invalid_overrides)}'
        )
    tracked_ids: list[str] = []
    for entity in final_entities:
        if (
            entity.kind == 'speaker'
            or entity.entity_id in proposal_by_id
            or entity.entity_id in overrides
        ):
            tracked_ids.append(entity.entity_id)
    for entity_id in tracked_ids:
        if entity_id in proposal_by_id:
            lifecycle.append(EntityLifecycleBinding(entity_id=entity_id, state='proposed'))
        else:
            lifecycle.append(overrides.get(
                entity_id,
                EntityLifecycleBinding(entity_id=entity_id, state='current'),
            ))

    binding_entity_ids = [item.entity_id for item in binding_items]
    if len(binding_entity_ids) != len(set(binding_entity_ids)):
        raise ValueError('equipment bindings must be unique per source entity')
    invalid_binding_targets = {
        item.entity_id
        for item in binding_items
        if item.entity_id not in final_by_id
        or final_by_id[item.entity_id].kind != 'speaker'
    }
    if invalid_binding_targets:
        raise ValueError(
            'equipment bindings require final speaker source entities: '
            f'{sorted(invalid_binding_targets)}'
        )

    authority_version: SystemVariantAuthorityVersion = (
        SYSTEM_VARIANT_EQUIPMENT_AUTHORITY_VERSION
        if binding_items
        else SYSTEM_VARIANT_AUTHORITY_VERSION
    )
    identity = {
        'schema_version': SYSTEM_VARIANT_SCHEMA_VERSION,
        'authority_version': authority_version,
        'name': name,
        'document_id': baseline.document_id,
        'baseline_revision_id': baseline.revision_id,
        'baseline_content_hash': baseline.content_hash,
        'parent_variant_id': parent_variant_id,
        'role_bindings': [item.model_dump(mode='json') for item in roles],
        'proposed_entities': [item.model_dump(mode='json') for item in proposals],
        'diff': [item.model_dump(mode='json') for item in diff],
        'entity_lifecycle': [item.model_dump(mode='json') for item in lifecycle],
        'proposal_evidence': [item.model_dump(mode='json') for item in evidence],
        'provenance': [item.model_dump(mode='json') for item in provenance_items],
    }
    if binding_items:
        identity['equipment_bindings'] = [
            item.model_dump(mode='json')
            for item in binding_items
        ]
    variant = SystemVariant(
        authority_version=authority_version,
        variant_id=str(uuid4()),
        name=name,
        document_id=baseline.document_id,
        baseline_revision_id=baseline.revision_id,
        baseline_content_hash=baseline.content_hash,
        parent_variant_id=parent_variant_id,
        role_bindings=roles,
        proposed_entities=proposals,
        diff=tuple(diff),
        entity_lifecycle=tuple(lifecycle),
        equipment_bindings=binding_items,
        proposal_evidence=evidence,
        provenance=provenance_items,
        created_at_utc=created_at_utc,
        variant_sha256=_digest(identity),
    )
    # A builder result must already be fully derivable, even before persistence.
    materialize_system_variant(baseline, variant)
    return variant


def apply_system_variant_to_working_document(
    working: WorkingDocument,
    *,
    baseline: SceneRevision,
    variant: SystemVariant,
) -> bool:
    """Apply a proposal to WorkingDocument as one undoable command, without saving it."""

    _validate_baseline(variant, baseline)
    if working.source_revision_id != baseline.revision_id:
        raise ValueError('working document is not based on the SystemVariant baseline')
    if scene_content_hash(working.committed_document) != baseline.content_hash:
        raise ValueError('working document has diverged from the SystemVariant baseline')
    proposed = materialize_system_variant(baseline, variant)
    return working.replace_document(proposed)
