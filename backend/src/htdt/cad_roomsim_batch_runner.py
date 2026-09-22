from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Callable, Iterable

from .cad_repository import SceneRevision
from .cad_roomsim import (
    CadRoomSimBinding,
    build_cad_roomsim_candidate_request,
)
from .cad_roomsim_repository import CadRoomSimRepository
from .cad_roomsim_results import (
    CAD_ROOMSIM_ATTEMPT_SCHEMA_VERSION,
    CadRoomSimBatchSpec,
    CadRoomSimCandidateAttempt,
    CadRoomSimExecutionResult,
    canonical_roomsim_result_json,
    canonical_roomsim_result_sha256,
    new_roomsim_attempt_id,
    new_roomsim_batch_run_id,
    roomsim_execution_result,
    roomsim_result_timestamp_utc,
)
from .cad_search_models import CadCandidate, CadSearchSpec
from .rew_roomsim_batch import (
    ROOMSIM_BATCH_ADAPTER_VERSION,
    ROOMSIM_MODEL_ID,
    RewRoomSimPositionBatchRequest,
    RoomSimPositionControl,
    run_roomsim_position_batch,
)


@dataclass(frozen=True)
class CadRoomSimBatchRunOutcome:
    batch_run_id: str
    completed_candidate_ids: tuple[str, ...]
    skipped_completed_candidate_ids: tuple[str, ...]
    attempt_ids: tuple[str, ...]
    failed_candidate_id: str | None
    failure_message: str | None
    cancelled: bool


def build_cad_roomsim_batch_spec(
    revision: SceneRevision,
    spec: CadSearchSpec,
    *,
    candidate_set_sha256: str,
    candidates: Iterable[CadCandidate],
    binding: CadRoomSimBinding,
) -> CadRoomSimBatchSpec:
    candidate_tuple = tuple(candidates)
    if not candidate_tuple:
        raise ValueError('Room Simulator batch requires at least one candidate')

    binding_payload = binding.model_dump(mode='json')
    binding_json = canonical_roomsim_result_json(binding_payload)
    requests = tuple(
        build_cad_roomsim_candidate_request(revision, spec, candidate, binding)
        for candidate in candidate_tuple
    )
    identity = {
        'schema_version': 1,
        'document_id': revision.document_id,
        'scene_revision_id': revision.revision_id,
        'scene_content_hash': revision.content_hash,
        'search_spec_id': spec.search_spec_id,
        'search_spec_sha256': spec.search_spec_sha256,
        'candidate_set_sha256': candidate_set_sha256,
        'binding': binding_payload,
        'model_id': ROOMSIM_MODEL_ID,
        'adapter_version': ROOMSIM_BATCH_ADAPTER_VERSION,
        'requests': [item.model_dump(mode='json') for item in requests],
    }
    return CadRoomSimBatchSpec(
        batch_run_id=new_roomsim_batch_run_id(),
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        search_spec_id=spec.search_spec_id,
        search_spec_sha256=spec.search_spec_sha256,
        candidate_set_sha256=candidate_set_sha256,
        binding_json=binding_json,
        binding_sha256=canonical_roomsim_result_sha256(binding_payload),
        requests=requests,
        created_at_utc=roomsim_result_timestamp_utc(),
        batch_spec_sha256=canonical_roomsim_result_sha256(identity),
    )


def _attempt(
    *,
    batch_run_id: str,
    candidate_id: str,
    attempt_index: int,
    started_at_utc: str,
    status: str,
    result: CadRoomSimExecutionResult | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
) -> CadRoomSimCandidateAttempt:
    completed_at_utc = roomsim_result_timestamp_utc()
    identity = {
        'schema_version': CAD_ROOMSIM_ATTEMPT_SCHEMA_VERSION,
        'batch_run_id': batch_run_id,
        'candidate_id': candidate_id,
        'attempt_index': attempt_index,
        'status': status,
        'result': None if result is None else result.model_dump(mode='json'),
        'result_sha256': None if result is None else result.result_sha256,
        'error_type': error_type,
        'error_message': error_message,
        'started_at_utc': started_at_utc,
        'completed_at_utc': completed_at_utc,
    }
    return CadRoomSimCandidateAttempt(
        attempt_id=new_roomsim_attempt_id(),
        batch_run_id=batch_run_id,
        candidate_id=candidate_id,
        attempt_index=attempt_index,
        status=status,
        result=result,
        result_sha256=None if result is None else result.result_sha256,
        error_type=error_type,
        error_message=error_message,
        started_at_utc=started_at_utc,
        completed_at_utc=completed_at_utc,
        attempt_sha256=canonical_roomsim_result_sha256(identity),
    )


def run_cad_roomsim_batch(
    repository: CadRoomSimRepository,
    control: RoomSimPositionControl,
    batch_spec: CadRoomSimBatchSpec,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> CadRoomSimBatchRunOutcome:
    """Run/resume one immutable O20 batch.

    Cancellation is observed only between candidates. An active REW transaction
    is always allowed to finish its mandatory restore path before this function
    stops submitting more candidates.
    """

    stored = repository.get_batch_spec(batch_spec.batch_run_id)
    if stored is None:
        repository.save_batch_spec(batch_spec)
    elif stored != batch_spec:
        raise ValueError('stored Room Simulator batch differs from the supplied immutable spec')

    completed_now: list[str] = []
    skipped: list[str] = []
    attempt_ids: list[str] = []
    failed_candidate_id: str | None = None
    failure_message: str | None = None
    was_cancelled = False

    completed_before = set(repository.completed_candidate_ids(batch_spec.batch_run_id))
    for frozen in batch_spec.requests:
        if frozen.candidate_id in completed_before:
            skipped.append(frozen.candidate_id)
            continue
        if cancelled is not None and cancelled():
            was_cancelled = True
            break

        request = RewRoomSimPositionBatchRequest(**json.loads(frozen.request_json))
        attempt_index = repository.next_attempt_index(
            batch_spec.batch_run_id,
            frozen.candidate_id,
        )
        started_at_utc = roomsim_result_timestamp_utc()
        try:
            result = run_roomsim_position_batch(control, request)
        except Exception as exc:
            failed = _attempt(
                batch_run_id=batch_spec.batch_run_id,
                candidate_id=frozen.candidate_id,
                attempt_index=attempt_index,
                started_at_utc=started_at_utc,
                status='failed',
                error_type=type(exc).__name__,
                error_message=str(exc) or type(exc).__name__,
            )
            repository.save_attempt(failed)
            attempt_ids.append(failed.attempt_id)
            failed_candidate_id = frozen.candidate_id
            failure_message = f'{failed.error_type}: {failed.error_message}'
            break

        completed = _attempt(
            batch_run_id=batch_spec.batch_run_id,
            candidate_id=frozen.candidate_id,
            attempt_index=attempt_index,
            started_at_utc=started_at_utc,
            status='completed',
            result=roomsim_execution_result(
                result,
                request_sha256=frozen.request_sha256,
            ),
        )
        repository.save_attempt(completed)
        attempt_ids.append(completed.attempt_id)
        completed_now.append(frozen.candidate_id)
        completed_before.add(frozen.candidate_id)

    return CadRoomSimBatchRunOutcome(
        batch_run_id=batch_spec.batch_run_id,
        completed_candidate_ids=tuple(completed_now),
        skipped_completed_candidate_ids=tuple(skipped),
        attempt_ids=tuple(attempt_ids),
        failed_candidate_id=failed_candidate_id,
        failure_message=failure_message,
        cancelled=was_cancelled,
    )
