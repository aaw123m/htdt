from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from math import isfinite
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_constraint_models import CadConstraintSet


# Schema v2 binds the executable O10/constraint-engine payloads into
# search_spec_sha256; persisted v1 specs are a prior schema version and fail
# closed on read instead of silently reinterpreting their hashes.
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
        return {
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
