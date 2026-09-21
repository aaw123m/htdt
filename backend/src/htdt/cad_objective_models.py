from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .optimization_objectives import ObjectiveVector
from .pareto import ParetoResult


CAD_OBJECTIVE_SCHEMA_VERSION = 1
CAD_PARETO_SCHEMA_VERSION = 1


def canonical_objective_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def canonical_objective_sha256(value: Any) -> str:
    return sha256(canonical_objective_json(value).encode('utf-8')).hexdigest()


def objective_timestamp_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class CadObjectiveInputRef(BaseModel):
    """One declared O30 input authority.

    ``source_sha256`` is the exact semantic hash the upstream evidence
    authority exposes for the referenced record (e.g. a RoomSim attempt
    response SHA-256, a measurement dataset SHA-256, or a provider semantic
    SHA-256). Optional so refs can be declared before the source identity is
    known, but when it is set the O30 authority replay must reproduce exactly
    this hash — a ref that names a different persisted record fails closed.
    ``hypothesis`` inputs are explicit caller-attested claims: they never
    resolve to persisted evidence and therefore must not carry a semantic
    hash.
    """

    model_config = ConfigDict(frozen=True)

    evidence_class: Literal['measured', 'derived', 'predicted', 'hypothesis']
    source_kind: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_provenance(self) -> 'CadObjectiveInputRef':
        if self.evidence_class == 'hypothesis' and self.source_sha256 is not None:
            raise ValueError(
                'hypothesis objective inputs are explicit claims and cannot '
                'carry verified source authority'
            )
        return self


class CadObjectiveEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = CAD_OBJECTIVE_SCHEMA_VERSION
    evaluation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    search_spec_id: str = Field(min_length=1)
    search_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_id: str = Field(min_length=1)
    input_refs: tuple[CadObjectiveInputRef, ...] = Field(min_length=1)
    evaluation_spec_json: str = Field(min_length=2)
    evaluation_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    vector: ObjectiveVector
    evaluation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    created_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_identity(self) -> 'CadObjectiveEvaluation':
        if self.vector.candidate_id != self.candidate_id:
            raise ValueError('objective vector candidate_id mismatch')
        keys = [
            (ref.evidence_class, ref.source_kind, ref.source_id)
            for ref in self.input_refs
        ]
        if len(keys) != len(set(keys)):
            raise ValueError('objective input refs must be unique')
        try:
            decoded = json.loads(self.evaluation_spec_json)
        except json.JSONDecodeError as exc:
            raise ValueError('evaluation_spec_json must contain JSON') from exc
        if canonical_objective_json(decoded) != self.evaluation_spec_json:
            raise ValueError('evaluation_spec_json must be canonical JSON')
        if canonical_objective_sha256(decoded) != self.evaluation_spec_sha256:
            raise ValueError('evaluation spec hash mismatch')
        if canonical_objective_sha256(self.identity_payload()) != self.evaluation_sha256:
            raise ValueError('objective evaluation identity hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'document_id': self.document_id,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'search_spec_id': self.search_spec_id,
            'search_spec_sha256': self.search_spec_sha256,
            'candidate_id': self.candidate_id,
            # ``exclude_none`` keeps the historical ref/hash shape for
            # unpinned (legacy) refs while pinning exact source identity on
            # new evidence-derived evaluations.
            'input_refs': [
                ref.model_dump(mode='json', exclude_none=True)
                for ref in self.input_refs
            ],
            'evaluation_spec': json.loads(self.evaluation_spec_json),
            'vector': self.vector.identity_payload(),
        }


class CadParetoEvaluationRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    evaluation_id: str = Field(min_length=1)
    evaluation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_id: str = Field(min_length=1)


class CadParetoSet(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = CAD_PARETO_SCHEMA_VERSION
    pareto_set_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    search_spec_id: str = Field(min_length=1)
    search_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    evaluations: tuple[CadParetoEvaluationRef, ...] = Field(min_length=1)
    objective_ids: tuple[str, ...] = Field(min_length=1)
    result: ParetoResult
    pareto_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    created_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_identity(self) -> 'CadParetoSet':
        evaluation_ids = [ref.evaluation_id for ref in self.evaluations]
        candidate_ids = [ref.candidate_id for ref in self.evaluations]
        if len(evaluation_ids) != len(set(evaluation_ids)):
            raise ValueError('Pareto evaluation ids must be unique')
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError('Pareto candidate ids must be unique')
        if len(self.objective_ids) != len(set(self.objective_ids)):
            raise ValueError('Pareto objective ids must be unique')
        if self.result.objective_ids != self.objective_ids:
            raise ValueError('Pareto result objective ids mismatch')
        if set(self.result.dominated_by) != set(candidate_ids):
            raise ValueError('Pareto result candidate set mismatch')
        if not set(self.result.non_dominated_candidate_ids).issubset(candidate_ids):
            raise ValueError('Pareto result contains an unknown candidate')
        if canonical_objective_sha256(self.identity_payload()) != self.pareto_sha256:
            raise ValueError('Pareto set identity hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'document_id': self.document_id,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'search_spec_id': self.search_spec_id,
            'search_spec_sha256': self.search_spec_sha256,
            'evaluations': [ref.model_dump(mode='json') for ref in self.evaluations],
            'objective_ids': list(self.objective_ids),
            'result': self.result.model_dump(mode='json'),
        }


def new_evaluation_id() -> str:
    return str(uuid4())


def new_pareto_set_id() -> str:
    return str(uuid4())
