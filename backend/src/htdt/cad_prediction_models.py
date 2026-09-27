from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_scene import Position3
from .canonical_json import canonical_json as canonical_prediction_json, canonical_sha256 as prediction_result_sha256


PredictionResultKind = Literal[
    'geometry_modes',
    'geometry_reflections',
    'scalar_field',
    # #938: a provider lane run persists the exact stored R170A/R170B
    # frequency response — never a re-simulated payload.
    'provider_frequency_response',
]
PredictionGeometryCompatibility = Literal[
    'exact_for_model_geometry',
    'rectangular_approximation',
    'unsupported',
    # #938: provider-lane results bind persisted solver evidence; room
    # geometry compatibility belongs to the provider's own pinned snapshot.
    'persisted_provider_evidence',
]
PredictionRunStatus = Literal['completed']
PredictionModeClass = Literal['axial', 'tangential', 'oblique']




def prediction_input_hash(input_snapshot_json: str) -> str:
    return sha256(input_snapshot_json.encode('utf-8')).hexdigest()




class CadPredictedRoomMode(BaseModel):
    model_config = ConfigDict(frozen=True)

    n_x: int = Field(ge=0)
    n_y: int = Field(ge=0)
    n_z: int = Field(ge=0)
    frequency_hz: float = Field(gt=0.0)
    mode_class: PredictionModeClass

    @model_validator(mode='after')
    def valid_mode(self) -> 'CadPredictedRoomMode':
        if self.n_x == self.n_y == self.n_z == 0:
            raise ValueError('room mode indices must not all be zero')
        if not isfinite(float(self.frequency_hz)):
            raise ValueError('mode frequency must be finite')
        return self


class CadPredictedReflection(BaseModel):
    model_config = ConfigDict(frozen=True)

    speaker_entity_id: str = Field(min_length=1)
    speaker_role: str = Field(min_length=1)
    surface_key: str = Field(min_length=1)
    surface_identity: str = Field(min_length=1)
    reflection_position: Position3
    source_position: Position3
    receiver_position: Position3
    direct_length_m: float = Field(ge=0.0)
    reflected_length_m: float = Field(ge=0.0)
    excess_length_m: float = Field(ge=0.0)
    excess_delay_ms: float = Field(ge=0.0)
    first_destructive_hz: float | None = Field(default=None, gt=0.0)

    @model_validator(mode='after')
    def finite_metrics(self) -> 'CadPredictedReflection':
        values = (
            self.direct_length_m,
            self.reflected_length_m,
            self.excess_length_m,
            self.excess_delay_ms,
        )
        if any(not isfinite(float(value)) for value in values):
            raise ValueError('reflection metrics must be finite')
        if self.reflected_length_m + 1e-9 < self.direct_length_m:
            raise ValueError('reflected path must not be shorter than the direct path')
        if self.first_destructive_hz is not None and not isfinite(float(self.first_destructive_hz)):
            raise ValueError('candidate destructive frequency must be finite')
        return self


class CadPredictedProviderResponse(BaseModel):
    """Persisted R170A/R170B provider frequency response for one receiver (#938).

    The provider lane never re-simulates: this payload is the exact stored
    output of the provider authority embedded in the run's input snapshot,
    rebound to the receiver entity the run was requested for.
    """

    model_config = ConfigDict(frozen=True)

    provider_id: str = Field(min_length=1)
    provider_semantic_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    provider_adapter_id: str = Field(min_length=1)
    provider_adapter_version: str = Field(min_length=1)
    provider_evidence_state: str = Field(min_length=1)
    provider_evidence_scope: str = Field(min_length=1)
    receiver_entity_id: str = Field(min_length=1)
    frequency_hz: tuple[float, ...] = Field(min_length=2)
    level_db: tuple[float, ...] = Field(min_length=2)

    @model_validator(mode='after')
    def valid_response(self) -> 'CadPredictedProviderResponse':
        if len(self.frequency_hz) != len(self.level_db):
            raise ValueError('provider response arrays must share one length')
        if any(
            not isfinite(float(value)) or float(value) <= 0.0
            for value in self.frequency_hz
        ):
            raise ValueError('provider response frequencies must be positive finite')
        if any(
            not isfinite(float(value)) for value in self.level_db
        ):
            raise ValueError('provider response levels must be finite')
        return self


class CadPredictionResult(BaseModel):
    """Immutable model output bound to one exact native SceneRevision and model input."""

    model_config = ConfigDict(frozen=True)

    prediction_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    constraint_workspace_hash: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')

    model_id: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    result_kind: PredictionResultKind
    geometry_compatibility: PredictionGeometryCompatibility
    parameters_json: str
    input_snapshot_json: str
    input_hash: str = Field(pattern=r'^[0-9a-f]{64}$')

    submitted_at_utc: str = Field(min_length=1)
    completed_at_utc: str = Field(min_length=1)
    status: PredictionRunStatus = 'completed'
    assumptions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    modes: tuple[CadPredictedRoomMode, ...] = ()
    reflections: tuple[CadPredictedReflection, ...] = ()
    provider_response: CadPredictedProviderResponse | None = None

    result_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    def result_identity_payload(self) -> dict[str, object]:
        """Versioned semantic identity of the complete prediction output.

        Covers every field that affects interpretation of the stored result:
        the source binding (document, exact SceneRevision and content hash,
        and the constraint workspace the run was issued under), the pinned
        model identity, the canonical request (``parameters_json`` and the
        exact ``input_hash`` committing to ``input_snapshot_json``), result
        kind, geometry classification, status, assumptions/warnings and the
        result-kind payload itself. Only non-semantic storage identities and
        timestamps (``prediction_id``, ``run_id``, ``submitted_at_utc``,
        ``completed_at_utc``) are excluded, so re-running the same model on
        the same input yields the same ``result_sha256``.
        """
        return {
            'result_identity_version': 1,
            'document_id': self.document_id,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'constraint_workspace_hash': self.constraint_workspace_hash,
            'model_id': self.model_id,
            'model_version': self.model_version,
            'result_kind': self.result_kind,
            'geometry_compatibility': self.geometry_compatibility,
            'parameters_json': self.parameters_json,
            'input_hash': self.input_hash,
            'status': self.status,
            'assumptions': list(self.assumptions),
            'warnings': list(self.warnings),
            'modes': [item.model_dump(mode='json') for item in self.modes],
            'reflections': [item.model_dump(mode='json') for item in self.reflections],
            'provider_response': (
                None
                if self.provider_response is None
                else self.provider_response.model_dump(mode='json')
            ),
        }

    @model_validator(mode='after')
    def valid_result(self) -> 'CadPredictionResult':
        for field_name, raw in (
            ('parameters_json', self.parameters_json),
            ('input_snapshot_json', self.input_snapshot_json),
        ):
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f'{field_name} must contain JSON') from exc
            if canonical_prediction_json(decoded) != raw:
                raise ValueError(f'{field_name} must be canonical JSON')
        if prediction_input_hash(self.input_snapshot_json) != self.input_hash:
            raise ValueError('input_hash does not match input_snapshot_json')
        if len(self.assumptions) != len(set(self.assumptions)):
            raise ValueError('assumptions must be unique')
        if len(self.warnings) != len(set(self.warnings)):
            raise ValueError('warnings must be unique')
        if self.result_kind == 'geometry_modes' and self.reflections:
            raise ValueError('geometry_modes result must not contain reflections')
        if self.result_kind == 'geometry_reflections' and self.modes:
            raise ValueError('geometry_reflections result must not contain modes')
        if self.result_kind == 'scalar_field' and (self.modes or self.reflections):
            raise ValueError('scalar_field payload is stored separately from geometry payloads')
        if self.result_kind == 'provider_frequency_response':
            if self.provider_response is None:
                raise ValueError(
                    'provider_frequency_response result requires a provider payload'
                )
            if self.modes or self.reflections:
                raise ValueError(
                    'provider_frequency_response must not contain geometry payloads'
                )
        elif self.provider_response is not None:
            raise ValueError(
                'geometry result kinds must not carry a provider response'
            )
        if self.geometry_compatibility == 'unsupported' and (
            self.modes or self.reflections or self.provider_response is not None
        ):
            raise ValueError('unsupported model geometry must not publish prediction payloads')
        if self.result_sha256 != prediction_result_sha256(self.result_identity_payload()):
            raise ValueError('result_sha256 does not match the canonical result identity payload')
        return self
