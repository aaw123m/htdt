from __future__ import annotations

from hashlib import sha256
import json
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_measurement_repository import CadMeasurementRepository
from .cad_prediction_provider import PredictionProviderBinding
from .cad_repository import SceneRepository
from .cad_search_repository import CadSearchRepository
from .cad_search import candidate_preview_document, iter_cad_candidate_pages
from .cad_scene import scene_content_hash
from .canonical_json import canonical_sha256


class CadMeasurementPlan(BaseModel):
    """Immutable O50 link from one generated candidate to the revision that was physically applied."""

    model_config = ConfigDict(frozen=True)

    plan_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    search_spec_id: str = Field(min_length=1)
    search_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_id: str = Field(min_length=1)
    candidate_set_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    applied_scene_revision_id: str = Field(min_length=1)
    applied_scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    status: Literal['planned', 'measured'] = 'planned'
    measurement_ids: tuple[str, ...] = ()
    prediction_provider_binding_id: str | None = Field(default=None, min_length=1)
    prediction_provider_binding_sha256: str | None = Field(
        default=None,
        pattern=r'^[0-9a-f]{64}$',
    )
    # Exact persisted predecessor this version claims; None marks a first-ever
    # version. Omitted from the identity payload when unset so legacy payloads
    # keep their original plan_sha256.
    supersedes_plan_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    plan_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def validate_status(self) -> 'CadMeasurementPlan':
        if self.status == 'planned' and self.measurement_ids:
            raise ValueError('planned measurement plan cannot contain measurements')
        if self.status == 'measured' and not self.measurement_ids:
            raise ValueError('measured measurement plan requires measurements')
        if len(self.measurement_ids) != len(set(self.measurement_ids)):
            raise ValueError('measurement ids must be unique')
        if (self.prediction_provider_binding_id is None) != (
            self.prediction_provider_binding_sha256 is None
        ):
            raise ValueError('prediction provider binding id/hash must be supplied together')
        if self.plan_sha256 != _hash(self.identity_payload()):
            raise ValueError('measurement plan identity hash mismatch')
        return self

    def identity_payload(self) -> dict:
        payload = {
            'document_id': self.document_id,
            'search_spec_id': self.search_spec_id,
            'search_spec_sha256': self.search_spec_sha256,
            'candidate_id': self.candidate_id,
            'candidate_set_sha256': self.candidate_set_sha256,
            'applied_scene_revision_id': self.applied_scene_revision_id,
            'applied_scene_content_hash': self.applied_scene_content_hash,
            'status': self.status,
            'measurement_ids': list(self.measurement_ids),
        }
        if self.prediction_provider_binding_id is not None:
            payload['prediction_provider_binding_id'] = self.prediction_provider_binding_id
            payload['prediction_provider_binding_sha256'] = (
                self.prediction_provider_binding_sha256
            )
        if self.supersedes_plan_sha256 is not None:
            payload['supersedes_plan_sha256'] = self.supersedes_plan_sha256
        return payload


def _hash(value: object) -> str:
    return canonical_sha256(value)


def _resolve_candidate(scene_repository: SceneRepository, spec, candidate_id: str):
    for page in iter_cad_candidate_pages(scene_repository, spec):
        for candidate in page.candidates:
            if candidate.candidate_id == candidate_id:
                return candidate, page.candidate_set_sha256
    raise ValueError('candidate does not belong to SearchSpec candidate set')


def build_measurement_plan(scene_repository: SceneRepository, search_repository: CadSearchRepository, *,
    search_spec_id: str, candidate_id: str, applied_scene_revision_id: str) -> CadMeasurementPlan:
    spec = search_repository.get(search_spec_id)
    if spec is None:
        raise ValueError('SearchSpec does not exist')
    source = scene_repository.get(spec.scene_revision_id)
    if source is None or source.document_id != spec.document_id or source.content_hash != spec.scene_content_hash:
        raise ValueError('SearchSpec source revision authority is unavailable or changed')
    candidate, candidate_set_sha256 = _resolve_candidate(scene_repository, spec, candidate_id)
    revision = scene_repository.get(applied_scene_revision_id)
    if revision is None:
        raise ValueError('applied SceneRevision does not exist')
    if revision.document_id != spec.document_id:
        raise ValueError('applied SceneRevision belongs to another document')
    if revision.parent_revision_id != source.revision_id:
        raise ValueError('applied SceneRevision must directly descend from the SearchSpec source revision')
    expected = candidate_preview_document(source.document, candidate)
    if scene_content_hash(expected) != revision.content_hash:
        raise ValueError('applied SceneRevision does not exactly match the selected candidate placement')
    payload = {
        'document_id': spec.document_id, 'search_spec_id': spec.search_spec_id,
        'search_spec_sha256': spec.search_spec_sha256, 'candidate_id': candidate_id,
        'candidate_set_sha256': candidate_set_sha256,
        'applied_scene_revision_id': revision.revision_id,
        'applied_scene_content_hash': revision.content_hash, 'status': 'planned', 'measurement_ids': [],
    }
    return CadMeasurementPlan(plan_id=str(uuid4()), plan_sha256=_hash(payload), **payload)


def bind_measurement_plan_prediction(
    plan: CadMeasurementPlan,
    binding: PredictionProviderBinding,
) -> CadMeasurementPlan:
    """Bind O50 planning to one exact R170A prediction authority."""

    if plan.status != 'planned':
        raise ValueError('prediction authority must be bound before measurement completion')
    if binding.consumer_kind != 'O50_MEASUREMENT_PLAN':
        raise ValueError('measurement plan requires an O50 prediction-provider binding')
    if binding.consumer_id != plan.plan_id:
        raise ValueError('prediction-provider binding references another measurement plan')
    if 'frequency_response_magnitude' not in binding.required_observables:
        raise ValueError('measurement plan prediction binding requires FR magnitude capability')
    payload = plan.identity_payload()
    payload['prediction_provider_binding_id'] = binding.binding_id
    payload['prediction_provider_binding_sha256'] = binding.semantic_sha256
    # A binding update is a new planned version that must supersede the exact
    # plan snapshot it was derived from.
    payload['supersedes_plan_sha256'] = plan.plan_sha256
    base = plan.model_dump(
        exclude={
            'prediction_provider_binding_id',
            'prediction_provider_binding_sha256',
            'supersedes_plan_sha256',
            'plan_sha256',
        }
    )
    return CadMeasurementPlan(
        **base,
        prediction_provider_binding_id=binding.binding_id,
        prediction_provider_binding_sha256=binding.semantic_sha256,
        supersedes_plan_sha256=plan.plan_sha256,
        plan_sha256=_hash(payload),
    )


def complete_measurement_plan(plan: CadMeasurementPlan, measurement_repository: CadMeasurementRepository,
    measurement_ids: tuple[str, ...]) -> CadMeasurementPlan:
    if plan.status != 'planned':
        raise ValueError('measurement plan is already completed')
    if not measurement_ids:
        raise ValueError('at least one measurement is required')
    for measurement_id in measurement_ids:
        record = measurement_repository.get_measurement(measurement_id)
        if record is None:
            raise ValueError(f'measurement does not exist: {measurement_id}')
        if record.document_id != plan.document_id or record.scene_revision_id != plan.applied_scene_revision_id:
            raise ValueError('measurement is not bound to the applied candidate SceneRevision')
        if record.scene_content_hash != plan.applied_scene_content_hash:
            raise ValueError('measurement content hash does not match applied candidate revision')
        if record.evidence_type != 'measured':
            raise ValueError('measurement plan accepts measured evidence only')
    payload = plan.identity_payload()
    payload['status'] = 'measured'
    payload['measurement_ids'] = list(measurement_ids)
    # Completion is a lifecycle transition of the exact claimed head, so two
    # completions built from the same planned snapshot can never both persist.
    payload['supersedes_plan_sha256'] = plan.plan_sha256
    base = plan.model_dump(
        exclude={'status', 'measurement_ids', 'supersedes_plan_sha256', 'plan_sha256'}
    )
    return CadMeasurementPlan(
        **base,
        status='measured',
        measurement_ids=measurement_ids,
        supersedes_plan_sha256=plan.plan_sha256,
        plan_sha256=_hash(payload),
    )
