"""Shared-library EquipmentDefinition upgrade workflow (#608).

Library definitions are immutable: a shared library refresh lands as a NEW
``EquipmentDefinition`` (same ``definition_id``, new version). This module
provides the explicit adoption path instead of silently rewriting projects:

- ``diff_equipment_definitions`` — a deterministic per-field comparison
  between two exact definitions of the same ``definition_id``, producing an
  ``EquipmentDefinitionUpgrade`` record naming every added/removed/changed
  authority slot.
- ``EquipmentDefinitionUsage`` / usage scanning — inventories which persisted
  authorities bind the OLD definition exactly (variants, binding semantics,
  installation contexts, R110 compiled sources, installed instances,
  topologies), so an upgrade reports its full impact surface.
- ``UpgradeAdoptionRecord`` — the append-only per-document decision:
  ``adopted`` (with the new authorities that rebind the upgrade) or
  ``skipped`` (with an explicit rationale). No project is silently rewritten;
  adoption is always an explicit recorded act.
- ``rebase_equipment_bindings`` — rebuilds variant equipment binding refs at
  the new definition for callers constructing a new SystemVariant; the old
  variant stays exactly as persisted.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_amplifier_headroom import AuthorityRef
from .cad_equipment import (
    EquipmentDataProvenance,
    EquipmentDefinition,
)


LIBRARY_UPGRADE_SCHEMA_VERSION = 1
LIBRARY_UPGRADE_AUTHORITY_VERSION = 'library-upgrade-1'

UsageKind = Literal[
    'system_variant_equipment',
    'binding_semantics',
    'installation_context',
    'r110_source_model',
    'installed_instance',
    'current_topology',
    'directivity_dataset',
]
UpgradeDecision = Literal['adopted', 'skipped']

# EquipmentDefinition slots whose payload identity is compared slot-by-slot.
_EQUIPMENT_AUTHORITY_SLOTS: tuple[str, ...] = (
    'identity_kind',
    'manufacturer',
    'model',
    'user_label',
    'equipment_class',
    'acoustic_reference_point_m',
    'cabinet_envelope_m',
    'sensitivity',
    'spl_capability',
    'directivity',
    'power_handling',
    'mounting',
    'port',
    'clearance',
    'provenance',
)


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


class DefinitionFieldChange(BaseModel):
    """One slot-level difference between two exact definitions."""

    model_config = ConfigDict(frozen=True)

    field: str = Field(min_length=1)
    change: Literal['added', 'removed', 'changed']
    before_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    after_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )

    @model_validator(mode='after')
    def valid_change(self) -> 'DefinitionFieldChange':
        if self.change == 'added' and self.before_sha256 is not None:
            raise ValueError('added field has no before value')
        if self.change == 'removed' and self.after_sha256 is not None:
            raise ValueError('removed field has no after value')
        if self.change == 'changed' and (
            self.before_sha256 is None or self.after_sha256 is None
        ):
            raise ValueError('changed field requires before and after hashes')
        if self.before_sha256 is not None and self.before_sha256 == self.after_sha256:
            raise ValueError('changed field hashes must differ')
        return self


class EquipmentDefinitionUsage(BaseModel):
    """One persisted authority that binds the old definition exactly."""

    model_config = ConfigDict(frozen=True)

    binding_kind: UsageKind
    authority_id: str = Field(min_length=1)
    authority_sha256: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$'
    )
    document_id: str | None = Field(default=None, min_length=1)
    entity_id: str | None = Field(default=None, min_length=1)


class EquipmentDefinitionUpgrade(BaseModel):
    """Immutable from-version→to-version upgrade record for one definition."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = LIBRARY_UPGRADE_SCHEMA_VERSION
    authority_version: Literal[
        'library-upgrade-1'
    ] = LIBRARY_UPGRADE_AUTHORITY_VERSION
    definition_id: str = Field(min_length=1)
    from_version: AuthorityRef
    to_version: AuthorityRef
    changes: tuple[DefinitionFieldChange, ...]
    impacted_fields: tuple[str, ...]
    provenance: tuple[EquipmentDataProvenance, ...] = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    upgrade_id: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_upgrade(self) -> 'EquipmentDefinitionUpgrade':
        if self.from_version.authority_id != self.definition_id:
            raise ValueError('upgrade from-version must reference the definition')
        if self.to_version.authority_id != self.definition_id:
            raise ValueError('upgrade to-version must reference the definition')
        if (
            self.from_version.version == self.to_version.version
            and self.from_version.semantic_sha256
            == self.to_version.semantic_sha256
        ):
            raise ValueError('upgrade requires distinct exact definitions')
        fields = [item.field for item in self.changes]
        if len(fields) != len(set(fields)):
            raise ValueError('upgrade field changes must be unique')
        if tuple(fields) != self.impacted_fields:
            raise ValueError('impacted_fields must match the diff order')
        digest = _digest(self.semantic_payload())
        if self.semantic_sha256 != digest:
            raise ValueError('EquipmentDefinitionUpgrade semantic hash mismatch')
        if self.upgrade_id != _semantic_id('library-upgrade', digest):
            raise ValueError('upgrade id does not match semantic hash')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'definition_id': self.definition_id,
            'from_version': self.from_version.model_dump(mode='json'),
            'to_version': self.to_version.model_dump(mode='json'),
            'changes': [item.model_dump(mode='json') for item in self.changes],
            'provenance': [item.model_dump(mode='json') for item in self.provenance],
            'created_at_utc': self.created_at_utc,
        }

    @property
    def is_noop(self) -> bool:
        return not self.changes


def diff_equipment_definitions(
    old: EquipmentDefinition,
    new: EquipmentDefinition,
    *,
    provenance: Sequence[EquipmentDataProvenance],
    created_at_utc: str,
) -> EquipmentDefinitionUpgrade:
    """Compute the slot-level upgrade between two exact definitions."""
    if old.definition_id != new.definition_id:
        raise ValueError(
            'upgrades must stay within one definition_id lineage; '
            'a different id is a new definition, not an upgrade'
        )
    if old.semantic_sha256 == new.semantic_sha256:
        raise ValueError('upgrade requires distinct definition semantics')

    old_dump = old.model_dump(mode='json')
    new_dump = new.model_dump(mode='json')
    changes: list[DefinitionFieldChange] = []
    for slot in _EQUIPMENT_AUTHORITY_SLOTS:
        before = old_dump.get(slot)
        after = new_dump.get(slot)
        if before == after:
            continue
        before_sha = None if before is None else _digest(before)
        after_sha = None if after is None else _digest(after)
        if before is None:
            change: Literal['added', 'removed', 'changed'] = 'added'
        elif after is None:
            change = 'removed'
        else:
            change = 'changed'
        changes.append(
            DefinitionFieldChange(
                field=slot,
                change=change,
                before_sha256=before_sha,
                after_sha256=after_sha,
            )
        )

    provenance_items = tuple(provenance)
    from_ref = AuthorityRef(
        authority_id=old.definition_id,
        version=old.version,
        semantic_sha256=old.semantic_sha256,
    )
    to_ref = AuthorityRef(
        authority_id=new.definition_id,
        version=new.version,
        semantic_sha256=new.semantic_sha256,
    )
    payload = {
        'schema_version': LIBRARY_UPGRADE_SCHEMA_VERSION,
        'authority_version': LIBRARY_UPGRADE_AUTHORITY_VERSION,
        'definition_id': old.definition_id,
        'from_version': from_ref.model_dump(mode='json'),
        'to_version': to_ref.model_dump(mode='json'),
        'changes': [item.model_dump(mode='json') for item in changes],
        'provenance': [item.model_dump(mode='json') for item in provenance_items],
        'created_at_utc': created_at_utc,
    }
    digest = _digest(payload)
    return EquipmentDefinitionUpgrade(
        definition_id=old.definition_id,
        from_version=from_ref,
        to_version=to_ref,
        changes=tuple(changes),
        impacted_fields=tuple(item.field for item in changes),
        provenance=provenance_items,
        created_at_utc=created_at_utc,
        upgrade_id=_semantic_id('library-upgrade', digest),
        semantic_sha256=digest,
    )


class UpgradeAdoptionRecord(BaseModel):
    """Append-only per-document adoption decision for one upgrade."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = LIBRARY_UPGRADE_SCHEMA_VERSION
    authority_version: Literal[
        'library-upgrade-1'
    ] = LIBRARY_UPGRADE_AUTHORITY_VERSION
    adoption_id: str = Field(min_length=1)
    upgrade_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    document_id: str = Field(min_length=1)
    decision: UpgradeDecision
    rationale: str = Field(min_length=1)
    rebased_authority_sha256: tuple[str, ...] = ()
    provenance: tuple[EquipmentDataProvenance, ...] = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_adoption(self) -> 'UpgradeAdoptionRecord':
        if self.decision == 'skipped' and self.rebased_authority_sha256:
            raise ValueError(
                'a skipped adoption cannot record rebased authorities'
            )
        digest = _digest(self.semantic_payload())
        if self.semantic_sha256 != digest:
            raise ValueError('UpgradeAdoptionRecord semantic hash mismatch')
        if self.adoption_id != _semantic_id('upgrade-adoption', digest):
            raise ValueError('adoption id does not match semantic hash')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'upgrade_sha256': self.upgrade_sha256,
            'document_id': self.document_id,
            'decision': self.decision,
            'rationale': self.rationale,
            'rebased_authority_sha256': list(self.rebased_authority_sha256),
            'provenance': [item.model_dump(mode='json') for item in self.provenance],
            'created_at_utc': self.created_at_utc,
        }


def build_upgrade_adoption(
    *,
    upgrade: EquipmentDefinitionUpgrade,
    document_id: str,
    decision: UpgradeDecision,
    rationale: str,
    provenance: Sequence[EquipmentDataProvenance],
    created_at_utc: str,
    rebased_authority_sha256: Sequence[str] = (),
) -> UpgradeAdoptionRecord:
    provenance_items = tuple(provenance)
    rebased = tuple(rebased_authority_sha256)
    payload = {
        'schema_version': LIBRARY_UPGRADE_SCHEMA_VERSION,
        'authority_version': LIBRARY_UPGRADE_AUTHORITY_VERSION,
        'upgrade_sha256': upgrade.semantic_sha256,
        'document_id': document_id,
        'decision': decision,
        'rationale': rationale,
        'rebased_authority_sha256': list(rebased),
        'provenance': [item.model_dump(mode='json') for item in provenance_items],
        'created_at_utc': created_at_utc,
    }
    digest = _digest(payload)
    return UpgradeAdoptionRecord(
        adoption_id=_semantic_id('upgrade-adoption', digest),
        upgrade_sha256=upgrade.semantic_sha256,
        document_id=document_id,
        decision=decision,
        rationale=rationale,
        rebased_authority_sha256=rebased,
        provenance=provenance_items,
        created_at_utc=created_at_utc,
        semantic_sha256=digest,
    )


def rebase_equipment_bindings(
    bindings: Sequence['EquipmentBindingRef'],
    upgrade: EquipmentDefinitionUpgrade,
) -> tuple['EquipmentBindingRef', ...]:
    """Retarget variant equipment bindings at the upgrade's new version.

    The returned tuple is for building a NEW SystemVariant; the original
    variant's bindings are never mutated in place.
    """
    from .cad_system_variant import EquipmentBindingRef

    rebased: list[EquipmentBindingRef] = []
    for binding in bindings:
        if (
            binding.equipment_definition_id == upgrade.definition_id
            and binding.equipment_definition_sha256
            == upgrade.from_version.semantic_sha256
        ):
            rebased.append(
                EquipmentBindingRef(
                    entity_id=binding.entity_id,
                    equipment_definition_id=upgrade.to_version.authority_id,
                    equipment_definition_version=upgrade.to_version.version,
                    equipment_definition_sha256=(
                        upgrade.to_version.semantic_sha256
                    ),
                )
            )
        else:
            rebased.append(binding)
    return tuple(rebased)


def adoption_required_fields(
    upgrade: EquipmentDefinitionUpgrade,
) -> tuple[str, ...]:
    """Fields whose change forces downstream authorities to be re-derived.

    Identity/display-only changes (labels) do not affect modeled semantics.
    """
    display_only = {'user_label', 'manufacturer', 'model'}
    return tuple(
        item.field
        for item in upgrade.changes
        if item.field not in display_only
    )


__all__ = [
    'DefinitionFieldChange',
    'EquipmentDefinitionUpgrade',
    'EquipmentDefinitionUsage',
    'LIBRARY_UPGRADE_AUTHORITY_VERSION',
    'LIBRARY_UPGRADE_SCHEMA_VERSION',
    'UpgradeAdoptionRecord',
    'UpgradeDecision',
    'UsageKind',
    'adoption_required_fields',
    'build_upgrade_adoption',
    'diff_equipment_definitions',
    'rebase_equipment_bindings',
]
