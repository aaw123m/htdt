from __future__ import annotations

from dataclasses import asdict
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_predictions import RectangularRoomFrame, exact_rectangular_room_frame
from .cad_repository import SceneRevision
from .cad_roomsim_results import (
    CadRoomSimCandidateRequest,
    canonical_roomsim_result_json,
    canonical_roomsim_result_sha256,
)
from .cad_scene import Position3, acoustic_reference_position
from .cad_search import candidate_preview_document
from .cad_search_models import CadCandidate, CadSearchSpec
from .rew_roomsim_batch import RewRoomSimPositionBatchRequest


class CadRoomSimSourceBinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    entity_id: str = Field(min_length=1)
    rew_source_name: str = Field(min_length=1)


class CadRoomSimBinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    receiver_entity_id: str = Field(min_length=1)
    sources: tuple[CadRoomSimSourceBinding, ...] = Field(min_length=1)
    mic_position: str = Field(default='Main', min_length=1)
    response_source_name: str | None = None
    geometry_mode: Literal['exact_rectangular'] = 'exact_rectangular'

    @model_validator(mode='after')
    def unique_bindings(self) -> 'CadRoomSimBinding':
        entity_ids = [item.entity_id for item in self.sources]
        source_names = [item.rew_source_name for item in self.sources]
        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError('Room Simulator source entity bindings must be unique')
        if len(source_names) != len(set(source_names)):
            raise ValueError('Room Simulator source names must be unique')
        if self.response_source_name is not None and self.response_source_name not in source_names:
            raise ValueError('source-specific Room Simulator response must reference a bound source')
        return self


def _local(position: Position3, frame: RectangularRoomFrame) -> dict[str, float]:
    return {
        'x_m': float(position.x_m - frame.origin_x_m),
        'y_m': float(position.y_m - frame.origin_y_m),
        'z_m': float(position.z_m),
    }


def _inside(position: Position3, frame: RectangularRoomFrame, *, tolerance: float = 1e-9) -> bool:
    local = _local(position, frame)
    return (
        -tolerance <= local['x_m'] <= frame.width_m + tolerance
        and -tolerance <= local['y_m'] <= frame.depth_m + tolerance
        and -tolerance <= local['z_m'] <= frame.height_m + tolerance
    )


def build_cad_roomsim_batch_request(
    revision: SceneRevision,
    spec: CadSearchSpec,
    candidate: CadCandidate,
    binding: CadRoomSimBinding,
) -> RewRoomSimPositionBatchRequest:
    if spec.document_id != revision.document_id:
        raise ValueError('Room Simulator SearchSpec belongs to another document')
    if spec.scene_revision_id != revision.revision_id:
        raise ValueError('Room Simulator SearchSpec belongs to another SceneRevision')
    if spec.scene_content_hash != revision.content_hash:
        raise ValueError('Room Simulator SearchSpec content hash mismatch')

    room = revision.document.room
    if room is None:
        raise ValueError('Room Simulator prediction requires a room')
    frame = exact_rectangular_room_frame(room)
    if frame is None:
        raise ValueError('Room Simulator exact model requires an axis-aligned rectangular room')

    preview = candidate_preview_document(revision.document, candidate)
    source_entity_ids = {item.entity_id for item in binding.sources}

    for entity_id in candidate.positions:
        entity = preview.entity(entity_id)
        if entity.kind == 'speaker' and entity_id not in source_entity_ids:
            raise ValueError(
                f'candidate moves an acoustic source without a Room Simulator binding: {entity_id}'
            )
        if entity.kind == 'measurement_point' and entity_id != binding.receiver_entity_id:
            raise ValueError(
                f'candidate moves an unmodelled measurement point: {entity_id}'
            )

    receiver_entity = preview.entity(binding.receiver_entity_id)
    if receiver_entity.kind != 'measurement_point':
        raise ValueError(
            f'Room Simulator receiver binding is not a measurement point: {binding.receiver_entity_id}'
        )
    receiver = acoustic_reference_position(receiver_entity)
    if receiver is None:
        raise ValueError('Room Simulator receiver has no acoustic reference position')
    if not _inside(receiver, frame):
        raise ValueError('Room Simulator receiver acoustic reference is outside the room')

    sources: dict[str, dict[str, float]] = {}
    for item in binding.sources:
        entity = preview.entity(item.entity_id)
        if entity.kind != 'speaker':
            raise ValueError(f'Room Simulator source binding is not a speaker: {item.entity_id}')
        reference = acoustic_reference_position(entity)
        if reference is None:
            raise ValueError(
                f'Room Simulator speaker has no explicit acoustic reference: {item.entity_id}'
            )
        if not _inside(reference, frame):
            raise ValueError(
                f'Room Simulator speaker acoustic reference is outside the room: {item.entity_id}'
            )
        sources[item.rew_source_name] = _local(reference, frame)

    return RewRoomSimPositionBatchRequest(
        candidate_id=candidate.candidate_id,
        room_width_m=frame.width_m,
        room_depth_m=frame.depth_m,
        room_height_m=frame.height_m,
        head_position_htdt=_local(receiver, frame),
        source_positions_htdt=sources,
        mic_position=binding.mic_position,
        source_name=binding.response_source_name,
    )


def build_cad_roomsim_candidate_request(
    revision: SceneRevision,
    spec: CadSearchSpec,
    candidate: CadCandidate,
    binding: CadRoomSimBinding,
) -> CadRoomSimCandidateRequest:
    """Freeze the canonical Room Simulator request for one exact candidate.

    The batch-input compiler and the repository authority replay share this
    builder, so a persisted ``CadRoomSimCandidateRequest`` is provably the
    canonical compilation of ``candidate`` under ``binding`` rather than a
    caller-supplied payload that merely hashes consistently.
    """
    request = build_cad_roomsim_batch_request(revision, spec, candidate, binding)
    payload = asdict(request)
    request_json = canonical_roomsim_result_json(payload)
    return CadRoomSimCandidateRequest(
        candidate_id=candidate.candidate_id,
        raw_index=candidate.raw_index,
        feasible_index=candidate.feasible_index,
        request_json=request_json,
        request_sha256=canonical_roomsim_result_sha256(payload),
    )
