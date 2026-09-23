from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .comparison import FrequencyResponse
from .rew_roomsim_batch import (
    ROOMSIM_BATCH_ADAPTER_VERSION,
    ROOMSIM_MODEL_ID,
    RewRoomSimPositionBatchResult,
)


CAD_ROOMSIM_BATCH_SCHEMA_VERSION = 1
CAD_ROOMSIM_ATTEMPT_SCHEMA_VERSION = 2
CAD_ROOMSIM_EXECUTION_SCHEMA_VERSION = 1


def canonical_roomsim_result_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def canonical_roomsim_result_sha256(value: Any) -> str:
    return sha256(canonical_roomsim_result_json(value).encode('utf-8')).hexdigest()


def roomsim_result_timestamp_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class CadRoomSimCandidateRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate_id: str = Field(min_length=1)
    raw_index: int = Field(ge=0)
    feasible_index: int = Field(ge=0)
    request_json: str = Field(min_length=2)
    request_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_request(self) -> 'CadRoomSimCandidateRequest':
        try:
            payload = json.loads(self.request_json)
        except json.JSONDecodeError as exc:
            raise ValueError('Room Simulator request_json must contain JSON') from exc
        if canonical_roomsim_result_json(payload) != self.request_json:
            raise ValueError('Room Simulator request_json must be canonical JSON')
        if canonical_roomsim_result_sha256(payload) != self.request_sha256:
            raise ValueError('Room Simulator request hash mismatch')
        if payload.get('candidate_id') != self.candidate_id:
            raise ValueError('Room Simulator request candidate_id mismatch')
        return self


class CadRoomSimBatchSpec(BaseModel):
    """Immutable O20 batch input bound to exact native authority and candidate requests.

    ``requests`` is the explicit ordered subset-selection authority: each
    member must resolve to a canonical SearchSpec candidate and reproduce
    ``build_cad_roomsim_candidate_request`` exactly, in canonical
    enumeration order. The self-hash alone is not authority — persistence
    replays the batch-input compiler on save and on every read.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = CAD_ROOMSIM_BATCH_SCHEMA_VERSION
    batch_run_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    scene_revision_id: str = Field(min_length=1)
    scene_content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    search_spec_id: str = Field(min_length=1)
    search_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_set_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    binding_json: str = Field(min_length=2)
    binding_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    model_id: str = ROOMSIM_MODEL_ID
    adapter_version: str = ROOMSIM_BATCH_ADAPTER_VERSION
    requests: tuple[CadRoomSimCandidateRequest, ...] = Field(min_length=1)
    created_at_utc: str = Field(min_length=1)
    batch_spec_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_identity(self) -> 'CadRoomSimBatchSpec':
        if self.model_id != ROOMSIM_MODEL_ID:
            raise ValueError('Room Simulator batch model_id mismatch')
        if self.adapter_version != ROOMSIM_BATCH_ADAPTER_VERSION:
            raise ValueError('Room Simulator batch adapter_version mismatch')
        candidate_ids = [item.candidate_id for item in self.requests]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError('Room Simulator batch candidate ids must be unique')
        try:
            binding = json.loads(self.binding_json)
        except json.JSONDecodeError as exc:
            raise ValueError('Room Simulator binding_json must contain JSON') from exc
        if canonical_roomsim_result_json(binding) != self.binding_json:
            raise ValueError('Room Simulator binding_json must be canonical JSON')
        if canonical_roomsim_result_sha256(binding) != self.binding_sha256:
            raise ValueError('Room Simulator binding hash mismatch')
        if canonical_roomsim_result_sha256(self.identity_payload()) != self.batch_spec_sha256:
            raise ValueError('Room Simulator batch spec identity hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'document_id': self.document_id,
            'scene_revision_id': self.scene_revision_id,
            'scene_content_hash': self.scene_content_hash,
            'search_spec_id': self.search_spec_id,
            'search_spec_sha256': self.search_spec_sha256,
            'candidate_set_sha256': self.candidate_set_sha256,
            'binding': json.loads(self.binding_json),
            'model_id': self.model_id,
            'adapter_version': self.adapter_version,
            'requests': [item.model_dump(mode='json') for item in self.requests],
        }


class CadRoomSimFrequencyResponse(BaseModel):
    """Canonical typed schema for the exact RewRoomSimFrequencyResponse payload."""

    model_config = ConfigDict(frozen=True, extra='forbid', allow_inf_nan=False)

    source_name: str | None
    mic_position: str = Field(min_length=1)
    message: str | None
    unit: str | None
    smoothing: str | None
    start_frequency_hz: float = Field(gt=0)
    points_per_octave: float | None = Field(gt=0)
    frequency_step_hz: float | None = Field(gt=0)
    frequency_hz: tuple[float, ...] = Field(min_length=1)
    magnitude: tuple[float, ...] = Field(min_length=1)
    phase_deg: tuple[float, ...] | None

    @model_validator(mode='after')
    def valid_response(self) -> 'CadRoomSimFrequencyResponse':
        if self.points_per_octave is None and self.frequency_step_hz is None:
            raise ValueError(
                'Room Simulator response requires points_per_octave or frequency_step_hz'
            )
        if len(self.frequency_hz) != len(self.magnitude):
            raise ValueError('Room Simulator response frequency/magnitude length mismatch')
        if self.phase_deg is not None and len(self.phase_deg) != len(self.magnitude):
            raise ValueError('Room Simulator response phase/magnitude length mismatch')
        if any(value <= 0 for value in self.frequency_hz):
            raise ValueError('Room Simulator response frequencies must be positive')
        return self


class CadRoomSimExecutionResult(BaseModel):
    """Immutable O20 execution output bound to the exact persisted candidate request.

    Built directly from ``RewRoomSimPositionBatchResult`` at transaction time so
    the persisted record carries the exact request SHA, model/adapter identity,
    transaction state hashes and the canonical typed response authority rather
    than an arbitrary self-hashed payload.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = CAD_ROOMSIM_EXECUTION_SCHEMA_VERSION
    request_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_id: str = Field(min_length=1)
    model_id: str = ROOMSIM_MODEL_ID
    model_version: str = Field(min_length=1)
    adapter_version: str = ROOMSIM_BATCH_ADAPTER_VERSION
    pre_state_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    applied_state_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    restored_state_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    response_json: str = Field(min_length=2)
    response_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    result_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_result(self) -> 'CadRoomSimExecutionResult':
        if self.model_id != ROOMSIM_MODEL_ID:
            raise ValueError('Room Simulator result model_id mismatch')
        if self.adapter_version != ROOMSIM_BATCH_ADAPTER_VERSION:
            raise ValueError('Room Simulator result adapter_version mismatch')
        if self.pre_state_sha256 != self.restored_state_sha256:
            raise ValueError('completed Room Simulator result requires exact pre-state restoration')
        roomsim_result_response(self)
        if canonical_roomsim_result_sha256(json.loads(self.response_json)) != self.response_sha256:
            raise ValueError('Room Simulator response hash mismatch')
        if canonical_roomsim_result_sha256(self.identity_payload()) != self.result_sha256:
            raise ValueError('Room Simulator execution result identity hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'request_sha256': self.request_sha256,
            'candidate_id': self.candidate_id,
            'model_id': self.model_id,
            'model_version': self.model_version,
            'adapter_version': self.adapter_version,
            'pre_state_sha256': self.pre_state_sha256,
            'applied_state_sha256': self.applied_state_sha256,
            'restored_state_sha256': self.restored_state_sha256,
            'response': json.loads(self.response_json),
            'response_sha256': self.response_sha256,
        }


def roomsim_result_response(result: CadRoomSimExecutionResult) -> CadRoomSimFrequencyResponse:
    """Parse the immutable exact response payload through the canonical schema."""
    try:
        payload = json.loads(result.response_json)
    except json.JSONDecodeError as exc:
        raise ValueError('Room Simulator response_json must contain JSON') from exc
    if canonical_roomsim_result_json(payload) != result.response_json:
        raise ValueError('Room Simulator response_json must be canonical JSON')
    return CadRoomSimFrequencyResponse.model_validate(payload)


def roomsim_execution_result(
    result: RewRoomSimPositionBatchResult,
    *,
    request_sha256: str,
) -> CadRoomSimExecutionResult:
    """Freeze exact runtime transaction output into immutable result authority."""
    response_payload = asdict(result.response)
    response_json = canonical_roomsim_result_json(response_payload)
    response_sha256 = canonical_roomsim_result_sha256(response_payload)
    identity = {
        'schema_version': CAD_ROOMSIM_EXECUTION_SCHEMA_VERSION,
        'request_sha256': request_sha256,
        'candidate_id': result.candidate_id,
        'model_id': result.model_id,
        'model_version': result.model_version,
        'adapter_version': result.adapter_version,
        'pre_state_sha256': result.pre_state_sha256,
        'applied_state_sha256': result.applied_state_sha256,
        'restored_state_sha256': result.restored_state_sha256,
        'response': response_payload,
        'response_sha256': response_sha256,
    }
    return CadRoomSimExecutionResult(
        request_sha256=request_sha256,
        candidate_id=result.candidate_id,
        model_id=result.model_id,
        model_version=result.model_version,
        adapter_version=result.adapter_version,
        pre_state_sha256=result.pre_state_sha256,
        applied_state_sha256=result.applied_state_sha256,
        restored_state_sha256=result.restored_state_sha256,
        response_json=response_json,
        response_sha256=response_sha256,
        result_sha256=canonical_roomsim_result_sha256(identity),
    )


class CadRoomSimCandidateAttempt(BaseModel):
    """Immutable attempt envelope referencing the exact execution result authority."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[2] = CAD_ROOMSIM_ATTEMPT_SCHEMA_VERSION
    attempt_id: str = Field(min_length=1)
    batch_run_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    attempt_index: int = Field(ge=1)
    status: Literal['completed', 'failed']
    result: CadRoomSimExecutionResult | None = None
    result_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    error_type: str | None = None
    error_message: str | None = None
    started_at_utc: str = Field(min_length=1)
    completed_at_utc: str = Field(min_length=1)
    attempt_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_attempt(self) -> 'CadRoomSimCandidateAttempt':
        if self.status == 'completed':
            if self.result is None or self.result_sha256 is None:
                raise ValueError('completed Room Simulator attempt requires the execution result')
            if self.result_sha256 != self.result.result_sha256:
                raise ValueError('Room Simulator attempt execution result hash mismatch')
            if self.result.candidate_id != self.candidate_id:
                raise ValueError('Room Simulator attempt execution result candidate mismatch')
            if self.error_type is not None or self.error_message is not None:
                raise ValueError('completed Room Simulator attempt must not contain an error')
        else:
            if self.result is not None or self.result_sha256 is not None:
                raise ValueError('failed Room Simulator attempt must not publish an execution result')
            if not self.error_type or not self.error_message:
                raise ValueError('failed Room Simulator attempt requires error_type and error_message')

        if canonical_roomsim_result_sha256(self.identity_payload()) != self.attempt_sha256:
            raise ValueError('Room Simulator attempt identity hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'batch_run_id': self.batch_run_id,
            'candidate_id': self.candidate_id,
            'attempt_index': self.attempt_index,
            'status': self.status,
            'result': None if self.result is None else self.result.model_dump(mode='json'),
            'result_sha256': self.result_sha256,
            'error_type': self.error_type,
            'error_message': self.error_message,
            'started_at_utc': self.started_at_utc,
            'completed_at_utc': self.completed_at_utc,
        }


def new_roomsim_batch_run_id() -> str:
    return str(uuid4())


def new_roomsim_attempt_id() -> str:
    return str(uuid4())


def roomsim_attempt_frequency_response(attempt: CadRoomSimCandidateAttempt) -> FrequencyResponse:
    if attempt.status != 'completed' or attempt.result is None:
        raise ValueError('Room Simulator attempt has no completed frequency response')
    response = roomsim_result_response(attempt.result)
    return FrequencyResponse(
        frequency_hz=response.frequency_hz,
        level_db=response.magnitude,
    )
