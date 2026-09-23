from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from math import isfinite
from typing import Any, Iterable, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_constraint_models import CadConstraintSet


# Schema v2 binds the executable O10/constraint-engine payloads into
# search_spec_sha256; persisted v1 specs are a prior schema version and fail
# closed on read instead of silently reinterpreting their hashes.
# Linked/group search variables (issue #504) extend schema v2 additively: they
# enter the identity payload only when declared, so persisted v2 specs without
# linked variables keep bit-exact identity semantics and remain valid.
CAD_SEARCH_SCHEMA_VERSION = 2
CAD_SEARCH_ALGORITHM_VERSION = 'search-space-grid-1'


def canonical_search_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def canonical_search_sha256(value: Any) -> str:
    return sha256(canonical_search_json(value).encode('utf-8')).hexdigest()


def constraint_workspace_snapshot(constraint_set: CadConstraintSet) -> tuple[str, str]:
    payload = constraint_set.model_dump(mode='json')
    raw = canonical_search_json(payload)
    return raw, sha256(raw.encode('utf-8')).hexdigest()


class CadSearchAxis(BaseModel):
    model_config = ConfigDict(frozen=True)

    entity_id: str = Field(min_length=1)
    axis: Literal['x', 'y', 'z']
    min_m: float
    max_m: float
    step_m: float = Field(gt=0.0)

    @model_validator(mode='after')
    def valid_range(self) -> 'CadSearchAxis':
        values = (self.min_m, self.max_m, self.step_m)
        if not all(isfinite(float(value)) for value in values):
            raise ValueError('search axis values must be finite')
        if self.max_m < self.min_m:
            raise ValueError('search axis max_m must be >= min_m')
        return self


LinkRelation = Literal[
    'mirror_x',
    'equal_x',
    'equal_y',
    'equal_z',
    'equal_delta_x',
    'equal_delta_y',
    'equal_delta_z',
]

_LINK_RELATION_AXIS: dict[str, str] = {
    'mirror_x': 'x',
    'equal_x': 'x',
    'equal_delta_x': 'x',
    'equal_y': 'y',
    'equal_delta_y': 'y',
    'equal_z': 'z',
    'equal_delta_z': 'z',
}


class CadLinkedSearchVariable(BaseModel):
    """Explicit linked/group search relation between two existing-scene entities.

    One rule derives exactly one slave axis from the resolved master position,
    so a shared delta or a mirrored pair stays a single independent search
    degree of freedom instead of expanding per-member Cartesian dimensions.
    The rule is also injected into the compiled constraint-engine spec as a
    ``linked_placement`` constraint, converging with the O100
    ``LinkedPlacementRule`` semantics rather than redefining them.
    """

    model_config = ConfigDict(frozen=True)

    constraint_id: str = Field(min_length=1, max_length=100)
    master_entity_id: str = Field(min_length=1, max_length=100)
    slave_entity_id: str = Field(min_length=1, max_length=100)
    relation: LinkRelation
    mirror_axis_x_m: float | None = None
    tolerance_m: float = Field(default=1e-6, ge=0.0)

    @model_validator(mode='after')
    def valid_variable(self) -> 'CadLinkedSearchVariable':
        if self.master_entity_id == self.slave_entity_id:
            raise ValueError('linked search variable requires two different entities')
        if self.relation == 'mirror_x':
            # The mirror reference must be explicit: never silently assume the
            # room centerline (issue #504).
            if self.mirror_axis_x_m is None:
                raise ValueError('mirror_x requires an explicit mirror_axis_x_m reference')
        elif self.mirror_axis_x_m is not None:
            raise ValueError('mirror_axis_x_m is only valid for relation=mirror_x')
        if self.mirror_axis_x_m is not None and not isfinite(float(self.mirror_axis_x_m)):
            raise ValueError('linked search variable mirror axis must be finite')
        if not isfinite(float(self.tolerance_m)):
            raise ValueError('linked search variable tolerance must be finite')
        return self

    @property
    def linked_axis(self) -> str:
        return _LINK_RELATION_AXIS[self.relation]


def shared_delta_variables(
    master_entity_id: str,
    member_entity_ids: Iterable[str],
    axis: Literal['x', 'y', 'z'],
    *,
    constraint_id_prefix: str,
    tolerance_m: float = 1e-6,
) -> tuple[CadLinkedSearchVariable, ...]:
    """Fan one master-axis search variable out to a group on the same axis.

    Each member keeps its exact stored baseline offset from the master
    (``equal_delta_*``), so a seat row or paired subwoofers move together as
    one search degree of freedom while preserving relative geometry.
    """

    if not constraint_id_prefix:
        raise ValueError('shared delta variables require a constraint id prefix')
    members = [member for member in member_entity_ids if member != master_entity_id]
    if len(members) != len(set(members)):
        raise ValueError('shared delta member entity ids must be unique')
    relation: LinkRelation = f'equal_delta_{axis}'  # type: ignore[assignment]
    return tuple(
        CadLinkedSearchVariable(
            constraint_id=f'{constraint_id_prefix}-{index:02d}',
            master_entity_id=master_entity_id,
            slave_entity_id=member,
            relation=relation,
            tolerance_m=tolerance_m,
        )
        for index, member in enumerate(members, start=1)
    )


class CadSearchSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[2] = CAD_SEARCH_SCHEMA_VERSION
    search_spec_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(min_length=64, max_length=64)
    constraint_workspace_hash: str = Field(min_length=64, max_length=64)
    constraint_snapshot_json: str = Field(min_length=2)
    constraint_engine_spec_json: str = Field(min_length=2)
    constraint_engine_spec_sha256: str = Field(min_length=64, max_length=64)
    algorithm: Literal['deterministic_grid'] = 'deterministic_grid'
    algorithm_version: Literal['search-space-grid-1'] = CAD_SEARCH_ALGORITHM_VERSION
    axes: tuple[CadSearchAxis, ...] = Field(min_length=1)
    linked_variables: tuple[CadLinkedSearchVariable, ...] = ()
    candidate_limit: int = Field(ge=1, le=50_000)
    o10_spec_json: str = Field(min_length=2)
    search_spec_sha256: str = Field(min_length=64, max_length=64)
    name: str | None = None
    created_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def validate_identity(self) -> 'CadSearchSpec':
        axis_keys = [(item.entity_id, item.axis) for item in self.axes]
        if len(axis_keys) != len(set(axis_keys)):
            raise ValueError('search axes must be unique by entity_id + axis')
        link_ids = [item.constraint_id for item in self.linked_variables]
        if len(link_ids) != len(set(link_ids)):
            raise ValueError('linked search variable constraint ids must be unique')
        axis_key_set = set(axis_keys)
        linked_targets: set[tuple[str, str]] = set()
        for variable in self.linked_variables:
            target = (variable.slave_entity_id, variable.linked_axis)
            if target in axis_key_set:
                raise ValueError(
                    f'{variable.slave_entity_id}.{variable.linked_axis} cannot be '
                    'both a grid axis and a linked derivation target'
                )
            if target in linked_targets:
                raise ValueError(
                    f'multiple linked search variables target {variable.slave_entity_id}.{variable.linked_axis}'
                )
            linked_targets.add(target)
        for label, payload_json in (
            ('constraint snapshot', self.constraint_snapshot_json),
            ('constraint engine spec', self.constraint_engine_spec_json),
            ('o10 spec', self.o10_spec_json),
        ):
            if canonical_search_json(json.loads(payload_json)) != payload_json:
                raise ValueError(f'{label} must be canonical JSON')
        if canonical_search_sha256(json.loads(self.constraint_snapshot_json)) != self.constraint_workspace_hash:
            raise ValueError('constraint workspace hash mismatch')
        if canonical_search_sha256(json.loads(self.constraint_engine_spec_json)) != self.constraint_engine_spec_sha256:
            raise ValueError('constraint engine spec hash mismatch')
        if canonical_search_sha256(self.identity_payload()) != self.search_spec_sha256:
            raise ValueError('search spec identity hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'schema_version': self.schema_version,
            'document_id': self.document_id,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'constraint_workspace_hash': self.constraint_workspace_hash,
            'constraint_engine_spec_sha256': self.constraint_engine_spec_sha256,
            'o10_spec': json.loads(self.o10_spec_json),
            'algorithm': self.algorithm,
            'algorithm_version': self.algorithm_version,
            'axes': [item.model_dump(mode='json') for item in self.axes],
            'candidate_limit': self.candidate_limit,
        }
        if self.linked_variables:
            # Declared linked/group rules are bound into spec identity; specs
            # without linked variables keep the v2 payload layout bit-exact.
            payload['linked_variables'] = [
                item.model_dump(mode='json')
                for item in sorted(
                    self.linked_variables,
                    key=lambda item: (item.constraint_id, item.master_entity_id, item.slave_entity_id),
                )
            ]
        return payload


class CadCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate_id: str = Field(min_length=1)
    raw_index: int = Field(ge=0)
    feasible_index: int = Field(ge=0)
    positions: dict[str, dict[str, float]]

    @model_validator(mode='after')
    def finite_positions(self) -> 'CadCandidate':
        for entity_id, position in self.positions.items():
            if not entity_id:
                raise ValueError('candidate entity id must not be empty')
            if set(position) != {'x_m', 'y_m', 'z_m'}:
                raise ValueError('candidate positions require x_m/y_m/z_m')
            if not all(isfinite(float(value)) for value in position.values()):
                raise ValueError('candidate positions must be finite')
        return self


class CadCandidateSetPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    search_spec_id: str
    search_spec_sha256: str
    candidate_set_sha256: str
    raw_candidate_count: int
    feasible_candidate_count: int
    rejected_candidate_count: int
    duplicate_candidate_count: int
    rejection_counts: dict[str, int]
    offset: int
    limit: int
    candidates: tuple[CadCandidate, ...]


def new_search_spec_id() -> str:
    return str(uuid4())


def search_timestamp_utc() -> str:
    return datetime.now(timezone.utc).isoformat()
