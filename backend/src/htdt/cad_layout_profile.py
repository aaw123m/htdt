"""Versioned LayoutProfile + CurrentSystemTopology authority (#505).

A ``LayoutProfile`` is the persisted, versioned channel vocabulary for a
project: declared speaker roles (``LayoutRole``) with stable role_ids, display
names, optional stereo pairing, and an optional deterministic channel order.
Nothing about it is guessed from free-form ``speaker_role`` strings.

A ``CurrentSystemTopology`` is the immutable authority binding one exact
scene revision's as-built speaker set to one exact LayoutProfile plus a
physical-source map: each bound speaker records its profile role and the exact
equipment authority (and, when registered, the exact InstalledEquipmentInstance
from #569) representing the physical unit. Speakers without a declared role
must be explicitly recorded as unassigned — the map is complete or invalid.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_amplifier_headroom import AuthorityRef
from .cad_equipment import EquipmentDataProvenance
from .cad_scene import SceneDocument, is_unassigned_speaker_role


LAYOUT_PROFILE_SCHEMA_VERSION = 1
LAYOUT_PROFILE_AUTHORITY_VERSION = 'layout-profile-1'
CURRENT_TOPOLOGY_AUTHORITY_VERSION = 'current-system-topology-1'


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


class LayoutRole(BaseModel):
    """One declared channel role inside a LayoutProfile."""

    model_config = ConfigDict(frozen=True)

    role_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    paired_role_id: str | None = Field(default=None, min_length=1)
    optional: bool = False
    notes: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def valid_role(self) -> 'LayoutRole':
        if is_unassigned_speaker_role(self.role_id):
            raise ValueError(
                'LayoutRole role_id cannot be an unassigned placeholder'
            )
        if self.paired_role_id == self.role_id:
            raise ValueError('LayoutRole cannot be paired with itself')
        return self


class LayoutProfile(BaseModel):
    """Immutable versioned channel-vocabulary authority."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = LAYOUT_PROFILE_SCHEMA_VERSION
    authority_version: Literal[
        'layout-profile-1'
    ] = LAYOUT_PROFILE_AUTHORITY_VERSION
    profile_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str | None = Field(default=None, min_length=1)
    roles: tuple[LayoutRole, ...] = Field(min_length=1)
    channel_order: tuple[str, ...] = ()
    provenance: tuple[EquipmentDataProvenance, ...] = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_profile(self) -> 'LayoutProfile':
        role_ids = [role.role_id for role in self.roles]
        if len(role_ids) != len(set(role_ids)):
            raise ValueError('LayoutProfile role ids must be unique')
        role_set = set(role_ids)
        for role in self.roles:
            if role.paired_role_id is not None and role.paired_role_id not in role_set:
                raise ValueError(
                    'LayoutRole paired_role_id is not present in the profile'
                )
        order = list(self.channel_order)
        if len(order) != len(set(order)):
            raise ValueError('LayoutProfile channel_order must be unique')
        if not set(order).issubset(role_set):
            raise ValueError(
                'LayoutProfile channel_order must reference declared roles'
            )
        if self.semantic_sha256 != _digest(self.semantic_payload()):
            raise ValueError('LayoutProfile semantic hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'profile_id': self.profile_id,
            'version': self.version,
            'name': self.name,
            'description': self.description,
            'roles': [item.model_dump(mode='json') for item in self.roles],
            'channel_order': list(self.channel_order),
            'provenance': [item.model_dump(mode='json') for item in self.provenance],
        }

    @property
    def role_ids(self) -> tuple[str, ...]:
        return tuple(role.role_id for role in self.roles)

    def role(self, role_id: str) -> LayoutRole | None:
        return next(
            (role for role in self.roles if role.role_id == role_id),
            None,
        )


def build_layout_profile(
    *,
    profile_id: str,
    version: str,
    name: str,
    roles: Sequence[LayoutRole],
    provenance: Sequence[EquipmentDataProvenance],
    description: str | None = None,
    channel_order: Sequence[str] = (),
) -> LayoutProfile:
    provenance_items = tuple(provenance)
    role_items = tuple(roles)
    order = tuple(channel_order)
    payload = {
        'schema_version': LAYOUT_PROFILE_SCHEMA_VERSION,
        'authority_version': LAYOUT_PROFILE_AUTHORITY_VERSION,
        'profile_id': profile_id,
        'version': version,
        'name': name,
        'description': description,
        'roles': [item.model_dump(mode='json') for item in role_items],
        'channel_order': list(order),
        'provenance': [item.model_dump(mode='json') for item in provenance_items],
    }
    return LayoutProfile(
        profile_id=profile_id,
        version=version,
        name=name,
        description=description,
        roles=role_items,
        channel_order=order,
        provenance=provenance_items,
        semantic_sha256=_digest(payload),
    )


class TopologyBindingItem(BaseModel):
    """One as-built speaker → profile role → physical source binding."""

    model_config = ConfigDict(frozen=True)

    entity_id: str = Field(min_length=1)
    role_id: str = Field(min_length=1)
    equipment: AuthorityRef | None = None
    installed_instance_id: str | None = Field(default=None, min_length=1)
    installed_instance_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    physical_source_label: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def valid_binding(self) -> 'TopologyBindingItem':
        if (self.installed_instance_id is None) != (
            self.installed_instance_sha256 is None
        ):
            raise ValueError(
                'installed instance id/sha must be supplied together'
            )
        if (
            self.equipment is None
            and self.installed_instance_id is None
            and self.physical_source_label is None
        ):
            raise ValueError(
                'topology binding requires a physical source: exact equipment '
                'authority, installed instance, or an explicit label'
            )
        if is_unassigned_speaker_role(self.role_id):
            raise ValueError(
                'topology bindings cannot use an unassigned placeholder role'
            )
        return self


class CurrentSystemTopology(BaseModel):
    """Immutable binding of one scene revision's speakers to a LayoutProfile."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = LAYOUT_PROFILE_SCHEMA_VERSION
    authority_version: Literal[
        'current-system-topology-1'
    ] = CURRENT_TOPOLOGY_AUTHORITY_VERSION
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    layout_profile: AuthorityRef
    bindings: tuple[TopologyBindingItem, ...]
    unassigned_entity_ids: tuple[str, ...] = ()
    provenance: tuple[EquipmentDataProvenance, ...] = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    topology_id: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_topology(self) -> 'CurrentSystemTopology':
        entity_ids = [item.entity_id for item in self.bindings]
        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError('topology bindings must be unique per entity')
        role_ids = [item.role_id for item in self.bindings]
        if len(role_ids) != len(set(role_ids)):
            raise ValueError('topology bindings must be unique per role')
        unassigned = list(self.unassigned_entity_ids)
        if len(unassigned) != len(set(unassigned)):
            raise ValueError('unassigned entity ids must be unique')
        if set(unassigned) & set(entity_ids):
            raise ValueError(
                'an entity cannot be both bound and unassigned'
            )
        digest = _digest(self.semantic_payload())
        if self.semantic_sha256 != digest:
            raise ValueError('CurrentSystemTopology semantic hash mismatch')
        if self.topology_id != _semantic_id('current-topology', digest):
            raise ValueError(
                'CurrentSystemTopology id does not match semantic hash'
            )
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'document_id': self.document_id,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'layout_profile': self.layout_profile.model_dump(mode='json'),
            'bindings': [
                item.model_dump(mode='json') for item in self.bindings
            ],
            'unassigned_entity_ids': list(self.unassigned_entity_ids),
            'provenance': [item.model_dump(mode='json') for item in self.provenance],
            'created_at_utc': self.created_at_utc,
        }


def build_current_system_topology(
    *,
    document: SceneDocument,
    scene_revision_id: str,
    scene_content_hash: str,
    layout_profile: LayoutProfile,
    bindings: Sequence[TopologyBindingItem],
    unassigned_entity_ids: Sequence[str] = (),
    provenance: Sequence[EquipmentDataProvenance],
    created_at_utc: str,
) -> CurrentSystemTopology:
    """Derive a topology binding that fails closed on any coverage gap.

    Every physical speaker in the revision must either be bound to a declared
    profile role or explicitly recorded as unassigned — there is no implicit
    'whatever is in the scene' fallback.
    """
    binding_items = tuple(bindings)
    unassigned = tuple(unassigned_entity_ids)
    profile = layout_profile
    role_set = set(profile.role_ids)
    for item in binding_items:
        if item.role_id not in role_set:
            raise ValueError(
                f'topology role {item.role_id} is not declared by '
                f'LayoutProfile {profile.profile_id} v{profile.version}'
            )
    speakers = {
        entity.entity_id: entity
        for entity in document.entities
        if entity.kind == 'speaker'
    }
    bound = {item.entity_id for item in binding_items}
    unassigned_set = set(unassigned)
    unknown = (bound | unassigned_set) - set(speakers)
    if unknown:
        raise ValueError(
            f'topology references non-speaker/missing entities: {sorted(unknown)}'
        )
    uncovered = set(speakers) - bound - unassigned_set
    if uncovered:
        raise ValueError(
            'every physical speaker must be bound to a LayoutProfile role or '
            f'explicitly unassigned; uncovered: {sorted(uncovered)}'
        )
    for entity_id in bound:
        speaker_role = speakers[entity_id].speaker_role
        role_id = next(
            item.role_id
            for item in binding_items
            if item.entity_id == entity_id
        )
        if speaker_role is not None and speaker_role != role_id:
            raise ValueError(
                f'entity {entity_id} scene role {speaker_role} does not '
                f'match declared topology role {role_id}'
            )

    provenance_items = tuple(provenance)
    profile_ref = AuthorityRef(
        authority_id=profile.profile_id,
        version=profile.version,
        semantic_sha256=profile.semantic_sha256,
    )
    payload = {
        'schema_version': LAYOUT_PROFILE_SCHEMA_VERSION,
        'authority_version': CURRENT_TOPOLOGY_AUTHORITY_VERSION,
        'document_id': document.document_id,
        'scene_revision_id': scene_revision_id,
        'scene_content_hash': scene_content_hash,
        'layout_profile': profile_ref.model_dump(mode='json'),
        'bindings': [item.model_dump(mode='json') for item in binding_items],
        'unassigned_entity_ids': list(unassigned),
        'provenance': [item.model_dump(mode='json') for item in provenance_items],
        'created_at_utc': created_at_utc,
    }
    digest = _digest(payload)
    return CurrentSystemTopology(
        document_id=document.document_id,
        scene_revision_id=scene_revision_id,
        scene_content_hash=scene_content_hash,
        layout_profile=profile_ref,
        bindings=binding_items,
        unassigned_entity_ids=unassigned,
        provenance=provenance_items,
        created_at_utc=created_at_utc,
        topology_id=_semantic_id('current-topology', digest),
        semantic_sha256=digest,
    )


__all__ = [
    'CURRENT_TOPOLOGY_AUTHORITY_VERSION',
    'LAYOUT_PROFILE_AUTHORITY_VERSION',
    'LAYOUT_PROFILE_SCHEMA_VERSION',
    'CurrentSystemTopology',
    'LayoutProfile',
    'LayoutRole',
    'TopologyBindingItem',
    'build_current_system_topology',
    'build_layout_profile',
]
