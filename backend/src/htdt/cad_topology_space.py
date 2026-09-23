from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_repository import SceneRevision
from .cad_system_variant import (
    SystemVariant,
    VariantEntityDiff,
    materialize_system_variant,
)


TOPOLOGY_SPACE_SCHEMA_VERSION = 1
TOPOLOGY_SPACE_AUTHORITY_VERSION = 'o100b-topology-space-1'


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


class TopologyOperation(BaseModel):
    """Explicit O100A diff admitted by one topology option."""

    model_config = ConfigDict(frozen=True)

    kind: Literal['add', 'remove', 'replace']
    entity_id: str = Field(min_length=1)
    role_id: str = Field(min_length=1)
    proposed_spec_id: str | None = Field(default=None, min_length=1)
    optional_role: bool = False
    diff: VariantEntityDiff

    @model_validator(mode='after')
    def valid_operation(self) -> 'TopologyOperation':
        if self.diff.kind != self.kind or self.diff.entity_id != self.entity_id:
            raise ValueError('topology operation must exactly mirror O100A variant diff')
        if self.kind in {'add', 'replace'} and self.proposed_spec_id is None:
            raise ValueError('add/replace topology operation requires ProposedEntitySpec identity')
        if self.kind == 'remove' and self.proposed_spec_id is not None:
            raise ValueError('remove topology operation cannot reference ProposedEntitySpec')
        entity = (
            self.diff.after_entity
            if self.kind in {'add', 'replace'}
            else self.diff.before_entity
        )
        if entity is None or entity.kind != 'speaker':
            raise ValueError('O100B topology operations currently support speakers only')
        if entity.speaker_role != self.role_id:
            raise ValueError('topology operation role does not match exact O100A diff')
        return self


class TopologyVariantOption(BaseModel):
    """One exact O100A SystemVariant admitted by TopologySearchSpec."""

    model_config = ConfigDict(frozen=True)

    option_id: str = Field(min_length=1)
    template_variant_id: str = Field(min_length=1)
    template_variant_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    final_role_ids: tuple[str, ...]
    operations: tuple[TopologyOperation, ...]

    @model_validator(mode='after')
    def valid_option(self) -> 'TopologyVariantOption':
        if self.option_id != 'topo-' + self.template_variant_sha256[:20]:
            raise ValueError('topology option_id is not deterministic')
        if len(self.final_role_ids) != len(set(self.final_role_ids)):
            raise ValueError('topology option final roles must be unique')
        entity_ids = [item.entity_id for item in self.operations]
        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError('topology option operations must target unique entities')
        return self


class TopologySearchSpec(BaseModel):
    """Immutable declared topology search space over exact O100A variants."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = TOPOLOGY_SPACE_SCHEMA_VERSION
    authority_version: Literal['o100b-topology-space-1'] = TOPOLOGY_SPACE_AUTHORITY_VERSION
    topology_search_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    baseline_revision_id: str = Field(min_length=1)
    baseline_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    include_baseline: bool = True
    optional_role_ids: tuple[str, ...] = ()
    options: tuple[TopologyVariantOption, ...] = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    topology_search_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_identity(self) -> 'TopologySearchSpec':
        if len(self.optional_role_ids) != len(set(self.optional_role_ids)):
            raise ValueError('TopologySearchSpec optional roles must be unique')
        option_ids = [item.option_id for item in self.options]
        if len(option_ids) != len(set(option_ids)):
            raise ValueError('TopologySearchSpec option IDs must be unique')
        variant_ids = [item.template_variant_id for item in self.options]
        if len(variant_ids) != len(set(variant_ids)):
            raise ValueError('TopologySearchSpec template variants must be unique')
        variant_hashes = [item.template_variant_sha256 for item in self.options]
        if len(variant_hashes) != len(set(variant_hashes)):
            raise ValueError('TopologySearchSpec semantic template variants must be unique')
        used_optional = {
            operation.role_id
            for option in self.options
            for operation in option.operations
            if operation.optional_role
        }
        if used_optional != set(self.optional_role_ids):
            raise ValueError(
                'TopologySearchSpec optional roles must exactly match optional operations'
            )
        if self.topology_search_sha256 != _digest(self.identity_payload()):
            raise ValueError('TopologySearchSpec identity hash mismatch')
        if self.topology_search_id != 'ts-' + self.topology_search_sha256[:20]:
            raise ValueError('TopologySearchSpec ID is not deterministic')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'document_id': self.document_id,
            'baseline_revision_id': self.baseline_revision_id,
            'baseline_content_hash': self.baseline_content_hash,
            'include_baseline': self.include_baseline,
            'optional_role_ids': list(self.optional_role_ids),
            'options': [
                item.model_dump(mode='json')
                for item in self.options
            ],
        }

    def option(self, option_id: str) -> TopologyVariantOption:
        for item in self.options:
            if item.option_id == option_id:
                return item
        raise KeyError(option_id)


def _validate_variant_baseline(
    baseline: SceneRevision,
    variant: SystemVariant,
):
    if (
        variant.document_id != baseline.document_id
        or variant.baseline_revision_id != baseline.revision_id
        or variant.baseline_content_hash != baseline.content_hash
    ):
        raise ValueError(
            'TopologySearchSpec template SystemVariant baseline authority mismatch'
        )
    return materialize_system_variant(baseline, variant)


def _option_from_variant(
    *,
    baseline: SceneRevision,
    variant: SystemVariant,
    optional_role_ids: set[str],
) -> TopologyVariantOption:
    final_scene = _validate_variant_baseline(baseline, variant)
    proposed_by_entity = {
        item.entity.entity_id: item
        for item in variant.proposed_entities
    }
    operations: list[TopologyOperation] = []
    for diff in variant.diff:
        entity = (
            diff.after_entity
            if diff.kind in {'add', 'replace'}
            else diff.before_entity
        )
        if entity is None or entity.kind != 'speaker' or entity.speaker_role is None:
            raise ValueError('O100B topology search supports speaker diffs only')
        proposed = proposed_by_entity.get(diff.entity_id)
        operations.append(TopologyOperation(
            kind=diff.kind,
            entity_id=diff.entity_id,
            role_id=entity.speaker_role,
            proposed_spec_id=(
                None if proposed is None else proposed.spec_id
            ),
            optional_role=entity.speaker_role in optional_role_ids,
            diff=diff,
        ))

    final_roles = tuple(
        entity.speaker_role
        for entity in final_scene.entities
        if entity.kind == 'speaker' and entity.speaker_role is not None
    )
    return TopologyVariantOption(
        option_id='topo-' + variant.variant_sha256[:20],
        template_variant_id=variant.variant_id,
        template_variant_sha256=variant.variant_sha256,
        final_role_ids=final_roles,
        operations=tuple(operations),
    )


def build_topology_search_spec(
    *,
    baseline: SceneRevision,
    template_variants: Sequence[SystemVariant],
    optional_role_ids: Sequence[str] = (),
    include_baseline: bool = True,
    created_at_utc: str,
) -> TopologySearchSpec:
    """Declare exact topology alternatives without changing the baseline scene."""

    variants = tuple(template_variants)
    if not variants:
        raise ValueError('TopologySearchSpec requires at least one SystemVariant option')
    optional_roles = tuple(sorted(set(optional_role_ids)))
    optional_set = set(optional_roles)
    options = tuple(sorted(
        (
            _option_from_variant(
                baseline=baseline,
                variant=variant,
                optional_role_ids=optional_set,
            )
            for variant in variants
        ),
        key=lambda item: (
            item.template_variant_sha256,
            item.template_variant_id,
        ),
    ))
    used_roles = {
        operation.role_id
        for option in options
        for operation in option.operations
    }
    unknown_optional = optional_set - used_roles
    if unknown_optional:
        raise ValueError(
            'optional topology roles are not present in declared operations: '
            + ', '.join(sorted(unknown_optional))
        )

    identity = {
        'schema_version': TOPOLOGY_SPACE_SCHEMA_VERSION,
        'authority_version': TOPOLOGY_SPACE_AUTHORITY_VERSION,
        'document_id': baseline.document_id,
        'baseline_revision_id': baseline.revision_id,
        'baseline_content_hash': baseline.content_hash,
        'include_baseline': bool(include_baseline),
        'optional_role_ids': list(optional_roles),
        'options': [
            item.model_dump(mode='json')
            for item in options
        ],
    }
    search_sha = _digest(identity)
    return TopologySearchSpec(
        topology_search_id='ts-' + search_sha[:20],
        document_id=baseline.document_id,
        baseline_revision_id=baseline.revision_id,
        baseline_content_hash=baseline.content_hash,
        include_baseline=bool(include_baseline),
        optional_role_ids=optional_roles,
        options=options,
        created_at_utc=created_at_utc,
        topology_search_sha256=search_sha,
    )


def require_topology_option(
    *,
    baseline: SceneRevision,
    topology_spec: TopologySearchSpec,
    template_variant: SystemVariant,
    option_id: str,
) -> TopologyVariantOption:
    if (
        topology_spec.document_id != baseline.document_id
        or topology_spec.baseline_revision_id != baseline.revision_id
        or topology_spec.baseline_content_hash != baseline.content_hash
    ):
        raise ValueError('TopologySearchSpec baseline SceneRevision authority mismatch')
    option = topology_spec.option(option_id)
    if (
        option.template_variant_id != template_variant.variant_id
        or option.template_variant_sha256 != template_variant.variant_sha256
    ):
        raise ValueError('topology option does not match exact template SystemVariant')
    _validate_variant_baseline(baseline, template_variant)
    return option
