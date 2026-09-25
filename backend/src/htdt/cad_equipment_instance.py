"""Installed equipment instance authority (#569).

A reusable ``EquipmentDefinition`` describes a model; the physical unit that is
actually installed in a room is a distinct durable authority. This module keeps
the two explicitly separate:

- ``InstalledEquipmentInstance`` — the physical device (AVR, projector, speaker,
  DSP, ...) bound to a project document, optionally resolved to an exact
  catalog definition, optionally bound to a scene entity placeholder.
- ``InstalledDefinitionBinding`` — append-only catalog-resolution history; a
  later binding supersedes earlier ones for current resolution but never
  rewrites them, so definition upgrades (#608) preserve evidence lineage.
- ``InstalledDeviceObservation`` — append-only mutable-state evidence
  (firmware/software/settings/service notes) bound to the instance.
- ``InstalledEquipmentReplacement`` — physical replacement lineage; replacing a
  device creates a new instance and links old -> new.

Nothing here mutates an ``EquipmentDefinition`` or a scene entity; the instance
record is the project-owned truth for the installed unit.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_equipment import EquipmentDataProvenance, EquipmentDefinition


INSTALLED_EQUIPMENT_SCHEMA_VERSION = 1
INSTALLED_INSTANCE_AUTHORITY_VERSION = 'installed-equipment-instance-1'
INSTALLED_BINDING_AUTHORITY_VERSION = 'installed-equipment-definition-binding-1'
INSTALLED_OBSERVATION_AUTHORITY_VERSION = 'installed-device-observation-1'
INSTALLED_REPLACEMENT_AUTHORITY_VERSION = 'installed-equipment-replacement-1'

InstalledEquipmentClass = Literal[
    'avr',
    'processor',
    'amplifier',
    'speaker',
    'subwoofer',
    'projector',
    'display',
    'source_device',
    'dsp',
    'other',
]
InstalledInstanceState = Literal['current', 'removed']
ObservationKind = Literal[
    'firmware',
    'software',
    'settings',
    'service',
    'note',
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


class InstalledDefinitionRef(BaseModel):
    """Exact catalog definition resolution for an installed unit.

    Presence of this ref is a resolution *claim*; whether an exact persisted
    ``EquipmentDefinition`` actually resolves is decided by
    ``CadInstalledEquipmentRepository.resolve_instance_definition`` (#819).
    """

    model_config = ConfigDict(frozen=True)

    equipment_definition_id: str = Field(min_length=1)
    equipment_definition_version: str = Field(min_length=1)
    equipment_definition_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @classmethod
    def from_definition(
        cls,
        definition: EquipmentDefinition,
    ) -> 'InstalledDefinitionRef':
        return cls(
            equipment_definition_id=definition.definition_id,
            equipment_definition_version=definition.version,
            equipment_definition_sha256=definition.semantic_sha256,
        )


class ExternalEquipmentIdentity(BaseModel):
    """Observed equipment identity evidence that is NOT a resolved local
    ``EquipmentDefinition`` (#819).

    Carries the id/version/hash triple an external observation (e.g. a
    Capture ``equipment-identity`` record) attested on the physical unit.
    It is evidence of *what was seen*, never a claim that a local catalog
    definition exists for it — a same-looking id/version string must not
    count as adoption of a local definition without reconciliation.
    """

    model_config = ConfigDict(frozen=True)

    equipment_id: str = Field(min_length=1)
    equipment_version: str = Field(min_length=1)
    equipment_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    evidence_source: str | None = Field(default=None, min_length=1)


DefinitionResolutionState = Literal[
    'RESOLVED_EXACT',
    'EXTERNAL_UNRESOLVED',
    'MISSING_LOCAL_DEFINITION',
    'CONFLICT',
    'UNRESOLVED',
]
"""Typed read-side resolution result (#819):

- ``RESOLVED_EXACT``: the in-effect ref resolves to a persisted
  ``EquipmentDefinition`` with id + version + semantic hash all equal.
- ``EXTERNAL_UNRESOLVED``: only external/observed identity evidence exists;
  no exact local definition binding is in effect.
- ``MISSING_LOCAL_DEFINITION``: a resolution claim exists but no persisted
  definition carries its semantic hash.
- ``CONFLICT``: a definition with the claimed hash persists but its
  id/version differ from the claim.
- ``UNRESOLVED``: neither a resolution claim nor external identity
  evidence exists.
"""


class InstanceDefinitionResolution(BaseModel):
    """Read-side resolution state for one installed unit (#819)."""

    model_config = ConfigDict(frozen=True)

    instance_id: str = Field(min_length=1)
    status: DefinitionResolutionState
    definition: EquipmentDefinition | None = None
    binding: 'InstalledDefinitionBinding | None' = None
    detail: str | None = None


class InstalledEquipmentInstance(BaseModel):
    """Durable physical-unit record, separate from the reusable definition."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = INSTALLED_EQUIPMENT_SCHEMA_VERSION
    authority_version: Literal[
        'installed-equipment-instance-1'
    ] = INSTALLED_INSTANCE_AUTHORITY_VERSION
    instance_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    equipment_class: InstalledEquipmentClass
    definition_ref: InstalledDefinitionRef | None = None
    external_identity: ExternalEquipmentIdentity | None = None
    manufacturer: str | None = Field(default=None, min_length=1)
    model: str | None = Field(default=None, min_length=1)
    user_label: str | None = Field(default=None, min_length=1)
    serial_number: str | None = None
    asset_tag: str | None = None
    scene_entity_id: str | None = Field(default=None, min_length=1)
    state: InstalledInstanceState = 'current'
    installed_at_utc: str | None = Field(default=None, min_length=1)
    provenance: tuple[EquipmentDataProvenance, ...] = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_instance(self) -> 'InstalledEquipmentInstance':
        if self.definition_ref is None and (
            self.manufacturer is None or self.model is None
        ) and self.user_label is None and self.external_identity is None:
            raise ValueError(
                'unresolved installed equipment requires manufacturer/model '
                'or a user label'
            )
        if self.serial_number is not None and not self.serial_number.strip():
            raise ValueError('serial_number must be non-empty when supplied')
        if self.asset_tag is not None and not self.asset_tag.strip():
            raise ValueError('asset_tag must be non-empty when supplied')
        if self.semantic_sha256 != _digest(self.semantic_payload()):
            raise ValueError('InstalledEquipmentInstance semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        payload = {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'instance_id': self.instance_id,
            'document_id': self.document_id,
            'equipment_class': self.equipment_class,
            'definition_ref': (
                None
                if self.definition_ref is None
                else self.definition_ref.model_dump(mode='json')
            ),
            'manufacturer': self.manufacturer,
            'model': self.model,
            'user_label': self.user_label,
            'serial_number': self.serial_number,
            'asset_tag': self.asset_tag,
            'scene_entity_id': self.scene_entity_id,
            'state': self.state,
            'installed_at_utc': self.installed_at_utc,
            'provenance': [item.model_dump(mode='json') for item in self.provenance],
            'created_at_utc': self.created_at_utc,
        }
        # Absent key keeps persisted pre-#819 digests valid; only a
        # recorded identity enters the hash.
        if self.external_identity is not None:
            payload['external_identity'] = self.external_identity.model_dump(
                mode='json'
            )
        return payload

    @property
    def is_catalog_resolved(self) -> bool:
        """Whether the record carries an exact catalog-resolution *claim*.

        Persistence only admits such a claim after resolving it exactly
        against the equipment catalog; authoritative truth for a stored
        instance is
        ``CadInstalledEquipmentRepository.resolve_instance_definition``
        (#819). External observed identity alone never sets this.
        """
        return self.definition_ref is not None

    @property
    def has_observed_identity(self) -> bool:
        """Whether external equipment-identity evidence is attached."""
        return self.external_identity is not None


def build_installed_equipment_instance(
    *,
    instance_id: str,
    document_id: str,
    equipment_class: InstalledEquipmentClass,
    provenance: Sequence[EquipmentDataProvenance],
    created_at_utc: str,
    equipment_definition: EquipmentDefinition | None = None,
    external_identity: ExternalEquipmentIdentity | None = None,
    manufacturer: str | None = None,
    model: str | None = None,
    user_label: str | None = None,
    serial_number: str | None = None,
    asset_tag: str | None = None,
    scene_entity_id: str | None = None,
    installed_at_utc: str | None = None,
) -> InstalledEquipmentInstance:
    """Create one installed-unit record.

    ``equipment_definition`` supplies the exact catalog ref and its identity
    fields; ``external_identity`` records observed (e.g. Capture) identity
    evidence that is NOT a local catalog resolution; when both are omitted
    the unit must still carry manual identity evidence.
    """
    definition_ref = (
        None
        if equipment_definition is None
        else InstalledDefinitionRef.from_definition(equipment_definition)
    )
    if equipment_definition is not None:
        manufacturer = manufacturer or equipment_definition.manufacturer
        model = model or equipment_definition.model
        user_label = user_label or equipment_definition.user_label
    provenance_items = tuple(provenance)
    payload = {
        'schema_version': INSTALLED_EQUIPMENT_SCHEMA_VERSION,
        'authority_version': INSTALLED_INSTANCE_AUTHORITY_VERSION,
        'instance_id': instance_id,
        'document_id': document_id,
        'equipment_class': equipment_class,
        'definition_ref': (
            None
            if definition_ref is None
            else definition_ref.model_dump(mode='json')
        ),
        'manufacturer': manufacturer,
        'model': model,
        'user_label': user_label,
        'serial_number': serial_number,
        'asset_tag': asset_tag,
        'scene_entity_id': scene_entity_id,
        'state': 'current',
        'installed_at_utc': installed_at_utc,
        'provenance': [item.model_dump(mode='json') for item in provenance_items],
        'created_at_utc': created_at_utc,
    }
    if external_identity is not None:
        payload['external_identity'] = external_identity.model_dump(
            mode='json'
        )
    return InstalledEquipmentInstance(
        instance_id=instance_id,
        document_id=document_id,
        equipment_class=equipment_class,
        definition_ref=definition_ref,
        external_identity=external_identity,
        manufacturer=manufacturer,
        model=model,
        user_label=user_label,
        serial_number=serial_number,
        asset_tag=asset_tag,
        scene_entity_id=scene_entity_id,
        state='current',
        installed_at_utc=installed_at_utc,
        provenance=provenance_items,
        created_at_utc=created_at_utc,
        semantic_sha256=_digest(payload),
    )


class InstalledDefinitionBinding(BaseModel):
    """Append-only catalog-resolution record for an installed unit.

    The latest binding (by persistence order) is the current resolution;
    earlier bindings stay readable as history and are never rewritten.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = INSTALLED_EQUIPMENT_SCHEMA_VERSION
    authority_version: Literal[
        'installed-equipment-definition-binding-1'
    ] = INSTALLED_BINDING_AUTHORITY_VERSION
    binding_id: str = Field(min_length=1)
    instance_id: str = Field(min_length=1)
    definition_ref: InstalledDefinitionRef
    bound_at_utc: str = Field(min_length=1)
    provenance: tuple[EquipmentDataProvenance, ...] = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_binding(self) -> 'InstalledDefinitionBinding':
        if self.semantic_sha256 != _digest(self.semantic_payload()):
            raise ValueError('InstalledDefinitionBinding semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'binding_id': self.binding_id,
            'instance_id': self.instance_id,
            'definition_ref': self.definition_ref.model_dump(mode='json'),
            'bound_at_utc': self.bound_at_utc,
            'provenance': [item.model_dump(mode='json') for item in self.provenance],
        }


def build_installed_definition_binding(
    *,
    binding_id: str,
    instance_id: str,
    equipment_definition: EquipmentDefinition,
    bound_at_utc: str,
    provenance: Sequence[EquipmentDataProvenance],
) -> InstalledDefinitionBinding:
    provenance_items = tuple(provenance)
    definition_ref = InstalledDefinitionRef.from_definition(equipment_definition)
    payload = {
        'schema_version': INSTALLED_EQUIPMENT_SCHEMA_VERSION,
        'authority_version': INSTALLED_BINDING_AUTHORITY_VERSION,
        'binding_id': binding_id,
        'instance_id': instance_id,
        'definition_ref': definition_ref.model_dump(mode='json'),
        'bound_at_utc': bound_at_utc,
        'provenance': [item.model_dump(mode='json') for item in provenance_items],
    }
    return InstalledDefinitionBinding(
        binding_id=binding_id,
        instance_id=instance_id,
        definition_ref=definition_ref,
        bound_at_utc=bound_at_utc,
        provenance=provenance_items,
        semantic_sha256=_digest(payload),
    )


class InstalledDeviceObservation(BaseModel):
    """Append-only mutable-state observation for an installed unit."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = INSTALLED_EQUIPMENT_SCHEMA_VERSION
    authority_version: Literal[
        'installed-device-observation-1'
    ] = INSTALLED_OBSERVATION_AUTHORITY_VERSION
    observation_id: str = Field(min_length=1)
    instance_id: str = Field(min_length=1)
    observation_kind: ObservationKind
    version: str | None = Field(default=None, min_length=1)
    detail: str | None = Field(default=None, min_length=1)
    observed_at_utc: str = Field(min_length=1)
    provenance: tuple[EquipmentDataProvenance, ...] = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_observation(self) -> 'InstalledDeviceObservation':
        if self.version is None and self.detail is None:
            raise ValueError(
                'device observation requires a version or detail payload'
            )
        if self.semantic_sha256 != _digest(self.semantic_payload()):
            raise ValueError('InstalledDeviceObservation semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'observation_id': self.observation_id,
            'instance_id': self.instance_id,
            'observation_kind': self.observation_kind,
            'version': self.version,
            'detail': self.detail,
            'observed_at_utc': self.observed_at_utc,
            'provenance': [item.model_dump(mode='json') for item in self.provenance],
        }


def build_installed_device_observation(
    *,
    observation_id: str,
    instance_id: str,
    observation_kind: ObservationKind,
    observed_at_utc: str,
    provenance: Sequence[EquipmentDataProvenance],
    version: str | None = None,
    detail: str | None = None,
) -> InstalledDeviceObservation:
    provenance_items = tuple(provenance)
    payload = {
        'schema_version': INSTALLED_EQUIPMENT_SCHEMA_VERSION,
        'authority_version': INSTALLED_OBSERVATION_AUTHORITY_VERSION,
        'observation_id': observation_id,
        'instance_id': instance_id,
        'observation_kind': observation_kind,
        'version': version,
        'detail': detail,
        'observed_at_utc': observed_at_utc,
        'provenance': [item.model_dump(mode='json') for item in provenance_items],
    }
    return InstalledDeviceObservation(
        observation_id=observation_id,
        instance_id=instance_id,
        observation_kind=observation_kind,
        version=version,
        detail=detail,
        observed_at_utc=observed_at_utc,
        provenance=provenance_items,
        semantic_sha256=_digest(payload),
    )


class InstalledEquipmentReplacement(BaseModel):
    """Physical replacement lineage: old unit out, new unit in."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = INSTALLED_EQUIPMENT_SCHEMA_VERSION
    authority_version: Literal[
        'installed-equipment-replacement-1'
    ] = INSTALLED_REPLACEMENT_AUTHORITY_VERSION
    replacement_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    removed_instance_id: str = Field(min_length=1)
    installed_instance_id: str = Field(min_length=1)
    replaced_at_utc: str = Field(min_length=1)
    rationale: str | None = Field(default=None, min_length=1)
    provenance: tuple[EquipmentDataProvenance, ...] = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_replacement(self) -> 'InstalledEquipmentReplacement':
        if self.removed_instance_id == self.installed_instance_id:
            raise ValueError('replacement must reference a different instance')
        if self.semantic_sha256 != _digest(self.semantic_payload()):
            raise ValueError('InstalledEquipmentReplacement semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'replacement_id': self.replacement_id,
            'document_id': self.document_id,
            'removed_instance_id': self.removed_instance_id,
            'installed_instance_id': self.installed_instance_id,
            'replaced_at_utc': self.replaced_at_utc,
            'rationale': self.rationale,
            'provenance': [item.model_dump(mode='json') for item in self.provenance],
        }


def build_installed_equipment_replacement(
    *,
    replacement_id: str,
    document_id: str,
    removed_instance_id: str,
    installed_instance_id: str,
    replaced_at_utc: str,
    provenance: Sequence[EquipmentDataProvenance],
    rationale: str | None = None,
) -> InstalledEquipmentReplacement:
    provenance_items = tuple(provenance)
    payload = {
        'schema_version': INSTALLED_EQUIPMENT_SCHEMA_VERSION,
        'authority_version': INSTALLED_REPLACEMENT_AUTHORITY_VERSION,
        'replacement_id': replacement_id,
        'document_id': document_id,
        'removed_instance_id': removed_instance_id,
        'installed_instance_id': installed_instance_id,
        'replaced_at_utc': replaced_at_utc,
        'rationale': rationale,
        'provenance': [item.model_dump(mode='json') for item in provenance_items],
    }
    return InstalledEquipmentReplacement(
        replacement_id=replacement_id,
        document_id=document_id,
        removed_instance_id=removed_instance_id,
        installed_instance_id=installed_instance_id,
        replaced_at_utc=replaced_at_utc,
        rationale=rationale,
        provenance=provenance_items,
        semantic_sha256=_digest(payload),
    )


def installed_instance_from_capture_identity(
    *,
    instance_id: str,
    document_id: str,
    entity_id: str,
    equipment_id: str,
    equipment_version: str,
    equipment_hash: str,
    serial_or_asset_tag: str | None,
    recorded_at_utc: str,
    equipment_class: InstalledEquipmentClass = 'other',
    provenance: Sequence[EquipmentDataProvenance],
    created_at_utc: str,
    equipment_definition: EquipmentDefinition | None = None,
) -> InstalledEquipmentInstance:
    """Map one Capture ``equipment-identity`` record onto an installed unit.

    The capture record attests a physical match between a scene entity and an
    exact equipment identity, plus an optional serial/asset tag; that evidence
    is preserved verbatim on the instance. When a catalog definition is
    supplied its exact ref is bound as well — otherwise the capture identity
    fields still stand as unresolved evidence.
    """
    if not equipment_id.strip() or not equipment_version.strip():
        raise ValueError('capture equipment identity requires id and version')
    if len(equipment_hash) != 64:
        raise ValueError('capture equipment_hash must be a sha256 hex digest')
    # The observed identity is always preserved as external evidence; only a
    # verified catalog match also produces a resolution claim.
    external_identity = ExternalEquipmentIdentity(
        equipment_id=equipment_id,
        equipment_version=equipment_version,
        equipment_sha256=equipment_hash,
        evidence_source='capture:equipment-identity',
    )
    if equipment_definition is not None:
        if (
            equipment_definition.definition_id != equipment_id
            or equipment_definition.version != equipment_version
            or equipment_definition.semantic_sha256 != equipment_hash
        ):
            raise ValueError(
                'capture equipment identity does not match the supplied '
                'catalog definition exactly'
            )
        definition_ref: InstalledDefinitionRef | None = (
            InstalledDefinitionRef.from_definition(equipment_definition)
        )
    else:
        definition_ref = None
    serial = None
    asset_tag = None
    if serial_or_asset_tag is not None and serial_or_asset_tag.strip():
        serial = serial_or_asset_tag
    provenance_items = tuple(provenance)
    payload = {
        'schema_version': INSTALLED_EQUIPMENT_SCHEMA_VERSION,
        'authority_version': INSTALLED_INSTANCE_AUTHORITY_VERSION,
        'instance_id': instance_id,
        'document_id': document_id,
        'equipment_class': equipment_class,
        'definition_ref': (
            None
            if definition_ref is None
            else definition_ref.model_dump(mode='json')
        ),
        'external_identity': external_identity.model_dump(mode='json'),
        'manufacturer': None,
        'model': None,
        'user_label': equipment_id,
        'serial_number': serial,
        'asset_tag': asset_tag,
        'scene_entity_id': entity_id,
        'state': 'current',
        'installed_at_utc': recorded_at_utc,
        'provenance': [item.model_dump(mode='json') for item in provenance_items],
        'created_at_utc': created_at_utc,
    }
    return InstalledEquipmentInstance(
        instance_id=instance_id,
        document_id=document_id,
        equipment_class=equipment_class,
        definition_ref=definition_ref,
        external_identity=external_identity,
        manufacturer=None,
        model=None,
        user_label=equipment_id,
        serial_number=serial,
        asset_tag=asset_tag,
        scene_entity_id=entity_id,
        state='current',
        installed_at_utc=recorded_at_utc,
        provenance=provenance_items,
        created_at_utc=created_at_utc,
        semantic_sha256=_digest(payload),
    )


__all__ = [
    'INSTALLED_BINDING_AUTHORITY_VERSION',
    'INSTALLED_EQUIPMENT_SCHEMA_VERSION',
    'INSTALLED_INSTANCE_AUTHORITY_VERSION',
    'INSTALLED_OBSERVATION_AUTHORITY_VERSION',
    'INSTALLED_REPLACEMENT_AUTHORITY_VERSION',
    'DefinitionResolutionState',
    'ExternalEquipmentIdentity',
    'InstanceDefinitionResolution',
    'InstalledDefinitionBinding',
    'InstalledDefinitionRef',
    'InstalledDeviceObservation',
    'InstalledEquipmentClass',
    'InstalledEquipmentInstance',
    'InstalledEquipmentReplacement',
    'InstalledInstanceState',
    'ObservationKind',
    'build_installed_definition_binding',
    'build_installed_device_observation',
    'build_installed_equipment_instance',
    'build_installed_equipment_replacement',
    'installed_instance_from_capture_identity',
]
