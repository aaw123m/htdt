"""Measurement plan value model — domain rank (#807 boundary refactor).

``CadMeasurementPlan`` is a frozen pydantic authority record; it was defined
inside ``services/cad_measurement_loop`` which forced the persistence layer
to import upward into services just to validate plans on save. The model
lives at domain rank now; the services module imports it back.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...canonical_json import canonical_sha256


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

__all__ = ['CadMeasurementPlan']
