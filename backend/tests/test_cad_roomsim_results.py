from __future__ import annotations

from copy import deepcopy
import json
import sqlite3

import pytest

from htdt.cad_constraint_models import CadConstraintSet
from htdt.cad_repository import SceneRepository
from htdt.cad_roomsim import CadRoomSimBinding, CadRoomSimSourceBinding
from htdt.cad_roomsim_batch_runner import build_cad_roomsim_batch_spec, run_cad_roomsim_batch
from htdt.cad_roomsim_repository import CadRoomSimRepository
from htdt.cad_roomsim_results import (
    CAD_ROOMSIM_ATTEMPT_SCHEMA_VERSION,
    CAD_ROOMSIM_EXECUTION_SCHEMA_VERSION,
    CadRoomSimCandidateAttempt,
    CadRoomSimExecutionResult,
    canonical_roomsim_result_json,
    canonical_roomsim_result_sha256,
    new_roomsim_attempt_id,
    roomsim_attempt_frequency_response,
    roomsim_result_timestamp_utc,
)
from htdt.cad_scene import (
    Offset3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.cad_search import build_cad_search_spec
from htdt.cad_search_models import (
    CadCandidate,
    CadSearchAxis,
    CadSearchSpec,
)
from htdt.cad_search_repository import CadSearchRepository
from htdt.rew_api import RewRoomSimFrequencyResponse, RewRoomSimSnapshot
from htdt.rew_roomsim_batch import (
    ROOMSIM_BATCH_ADAPTER_VERSION,
    ROOMSIM_MODEL_ID,
    roomsim_state_sha256,
)


def _scene() -> SceneDocument:
    return SceneDocument(
        document_id='roomsim-batch-fixture',
        schema_version=2,
        room=RoomPrism(
            room_id='room',
            width_m=4.0,
            depth_m=5.0,
            height_m=2.4,
        ),
        entities=(
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='FL',
                speaker_role='FL',
                position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
                size_m=Size3(x_m=0.2, y_m=0.2, z_m=0.4),
                acoustic_reference_offset_m=Offset3(),
            ),
            SceneEntity(
                entity_id='point-mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=2.0, y_m=3.0, z_m=1.0),
            ),
        ),
    )


def _spec(revision) -> CadSearchSpec:
    constraint = CadConstraintSet(document_id=revision.document_id, constraints=())
    spec, _estimate = build_cad_search_spec(
        revision,
        constraint,
        (
            CadSearchAxis(
                entity_id='speaker-fl',
                axis='x',
                min_m=1.0,
                max_m=1.5,
                step_m=0.25,
            ),
        ),
        candidate_limit=10,
    )
    return spec.model_copy(update={
        'search_spec_id': 'roomsim-search',
        'created_at_utc': '2026-09-18T00:00:00+00:00',
    })


def _candidates() -> tuple[CadCandidate, CadCandidate]:
    return (
        CadCandidate(
            candidate_id='candidate-a',
            raw_index=0,
            feasible_index=0,
            positions={'speaker-fl': {'x_m': 1.25, 'y_m': 1.0, 'z_m': 1.0}},
        ),
        CadCandidate(
            candidate_id='candidate-b',
            raw_index=1,
            feasible_index=1,
            positions={'speaker-fl': {'x_m': 1.5, 'y_m': 1.0, 'z_m': 1.0}},
        ),
    )


def _binding() -> CadRoomSimBinding:
    return CadRoomSimBinding(
        receiver_entity_id='point-mlp',
        sources=(
            CadRoomSimSourceBinding(entity_id='speaker-fl', rew_source_name='Left'),
        ),
        response_source_name='Left',
    )


def _roomsim_snapshot() -> RewRoomSimSnapshot:
    return RewRoomSimSnapshot(
        rew_version='5.40 Beta 135 API 0.9.8',
        room_size={'unit': 'metres', 'width': 4.0, 'length': 5.0, 'height': 2.4},
        room_is_sealed=False,
        absorptions={'front': 0.1},
        options={'crossoverFrequencyHz': 80},
        head_position_rew={
            'unit': 'metres',
            'fromRear': 2.0,
            'fromLeft': 2.0,
            'fromFloor': 1.0,
        },
        head_position_htdt={'x_m': 2.0, 'y_m': 3.0, 'z_m': 1.0},
        mic_position_offsets={'unit': 'metres'},
        active_sources=('Left',),
        recognized_sources=('Left',),
        mic_positions=('Main',),
        sources={
            'Left': {
                'position_rew': {
                    'unit': 'metres',
                    'fromRear': 4.0,
                    'fromLeft': 1.0,
                    'fromFloor': 1.0,
                },
                'position_htdt': {'x_m': 1.0, 'y_m': 1.0, 'z_m': 1.0},
                'configuration': {'delayms': 0},
            },
        },
    )


class FakeControl:
    def __init__(self) -> None:
        self.state = _roomsim_snapshot()
        self.response_reads = 0
        self.fail_response_reads: set[int] = set()

    def get_roomsim_snapshot(self) -> RewRoomSimSnapshot:
        return deepcopy(self.state)

    def set_roomsim_head_position(self, position_rew) -> None:
        payload = self.state.__dict__.copy()
        payload['head_position_rew'] = dict(position_rew)
        payload['head_position_htdt'] = {
            'x_m': float(position_rew['fromLeft']),
            'y_m': float(self.state.room_size['length']) - float(position_rew['fromRear']),
            'z_m': float(position_rew['fromFloor']),
        }
        self.state = RewRoomSimSnapshot(**payload)

    def set_roomsim_source_position(self, source_name: str, position_rew) -> None:
        sources = deepcopy(self.state.sources)
        sources[source_name]['position_rew'] = dict(position_rew)
        sources[source_name]['position_htdt'] = {
            'x_m': float(position_rew['fromLeft']),
            'y_m': float(self.state.room_size['length']) - float(position_rew['fromRear']),
            'z_m': float(position_rew['fromFloor']),
        }
        payload = self.state.__dict__.copy()
        payload['sources'] = sources
        self.state = RewRoomSimSnapshot(**payload)

    def get_roomsim_frequency_response(self, *, mic_position='Main', source_name=None):
        self.response_reads += 1
        if self.response_reads in self.fail_response_reads:
            raise RuntimeError('simulated REW response failure')
        return RewRoomSimFrequencyResponse(
            source_name=source_name,
            mic_position=mic_position,
            message='fixture',
            unit='SPL',
            smoothing='None',
            start_frequency_hz=20.0,
            points_per_octave=96.0,
            frequency_step_hz=None,
            frequency_hz=(20.0, 40.0, 80.0),
            magnitude=(80.0, 81.0, 79.0),
            phase_deg=(0.0, 1.0, 2.0),
        )


def _repositories(tmp_path):
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(_scene(), parent_revision_id=None).revision
    search_repository = CadSearchRepository(scene_repository)
    spec = _spec(revision)
    search_repository.save(spec)
    roomsim_repository = CadRoomSimRepository(scene_repository, search_repository)
    return revision, spec, roomsim_repository


def test_roomsim_batch_cancel_then_resume_skips_completed_candidate(tmp_path) -> None:
    revision, spec, repository = _repositories(tmp_path)
    batch = build_cad_roomsim_batch_spec(
        revision,
        spec,
        candidate_set_sha256='a' * 64,
        candidates=_candidates(),
        binding=_binding(),
    )
    control = FakeControl()
    baseline_hash = roomsim_state_sha256(control.get_roomsim_snapshot())
    cancellation_checks = 0

    def cancelled() -> bool:
        nonlocal cancellation_checks
        cancellation_checks += 1
        return cancellation_checks > 1

    first = run_cad_roomsim_batch(repository, control, batch, cancelled=cancelled)

    assert first.completed_candidate_ids == ('candidate-a',)
    assert first.skipped_completed_candidate_ids == ()
    assert first.cancelled is True
    assert first.failed_candidate_id is None
    assert control.response_reads == 1
    assert roomsim_state_sha256(control.get_roomsim_snapshot()) == baseline_hash
    assert repository.get_batch_spec(batch.batch_run_id) == batch

    resumed = run_cad_roomsim_batch(repository, control, batch)

    assert resumed.completed_candidate_ids == ('candidate-b',)
    assert resumed.skipped_completed_candidate_ids == ('candidate-a',)
    assert resumed.cancelled is False
    assert resumed.failed_candidate_id is None
    assert control.response_reads == 2
    attempts = repository.list_attempts(batch.batch_run_id)
    assert [(item.candidate_id, item.attempt_index, item.status) for item in attempts] == [
        ('candidate-a', 1, 'completed'),
        ('candidate-b', 1, 'completed'),
    ]
    response = roomsim_attempt_frequency_response(attempts[0])
    assert response.frequency_hz == (20.0, 40.0, 80.0)
    assert response.level_db == (80.0, 81.0, 79.0)


def test_roomsim_batch_failure_is_persisted_and_resume_creates_new_attempt(tmp_path) -> None:
    revision, spec, repository = _repositories(tmp_path)
    batch = build_cad_roomsim_batch_spec(
        revision,
        spec,
        candidate_set_sha256='b' * 64,
        candidates=_candidates(),
        binding=_binding(),
    )
    control = FakeControl()
    baseline_hash = roomsim_state_sha256(control.get_roomsim_snapshot())
    control.fail_response_reads.add(1)

    failed = run_cad_roomsim_batch(repository, control, batch)

    assert failed.completed_candidate_ids == ()
    assert failed.failed_candidate_id == 'candidate-a'
    assert 'simulated REW response failure' in str(failed.failure_message)
    assert roomsim_state_sha256(control.get_roomsim_snapshot()) == baseline_hash
    attempts = repository.list_candidate_attempts(batch.batch_run_id, 'candidate-a')
    assert len(attempts) == 1
    assert attempts[0].attempt_index == 1
    assert attempts[0].status == 'failed'
    assert attempts[0].result is None

    control.fail_response_reads.clear()
    resumed = run_cad_roomsim_batch(repository, control, batch)

    assert resumed.completed_candidate_ids == ('candidate-a', 'candidate-b')
    assert resumed.skipped_completed_candidate_ids == ()
    attempts = repository.list_candidate_attempts(batch.batch_run_id, 'candidate-a')
    assert [(item.attempt_index, item.status) for item in attempts] == [
        (1, 'failed'),
        (2, 'completed'),
    ]
    assert repository.completed_candidate_ids(batch.batch_run_id) == {
        'candidate-a',
        'candidate-b',
    }


def test_roomsim_repository_rejects_batch_bound_to_unsaved_search_spec(tmp_path) -> None:
    scene_repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = scene_repository.save(_scene(), parent_revision_id=None).revision
    search_repository = CadSearchRepository(scene_repository)
    repository = CadRoomSimRepository(scene_repository, search_repository)
    spec = _spec(revision)
    batch = build_cad_roomsim_batch_spec(
        revision,
        spec,
        candidate_set_sha256='c' * 64,
        candidates=(_candidates()[0],),
        binding=_binding(),
    )

    with pytest.raises(ValueError, match='SearchSpec does not exist'):
        repository.save_batch_spec(batch)


def test_roomsim_batch_spec_freezes_exact_candidate_request(tmp_path) -> None:
    revision, spec, _ = _repositories(tmp_path)
    candidate = _candidates()[0]
    batch = build_cad_roomsim_batch_spec(
        revision,
        spec,
        candidate_set_sha256='d' * 64,
        candidates=(candidate,),
        binding=_binding(),
    )

    frozen = batch.requests[0]
    payload = json.loads(frozen.request_json)
    assert frozen.candidate_id == candidate.candidate_id
    assert payload['candidate_id'] == candidate.candidate_id
    assert payload['source_positions_htdt']['Left'] == {
        'x_m': 1.25,
        'y_m': 1.0,
        'z_m': 1.0,
    }


def test_roomsim_batch_identity_is_reproducible_for_same_exact_inputs(tmp_path) -> None:
    revision, spec, _ = _repositories(tmp_path)
    first = build_cad_roomsim_batch_spec(
        revision,
        spec,
        candidate_set_sha256='e' * 64,
        candidates=_candidates(),
        binding=_binding(),
    )
    second = build_cad_roomsim_batch_spec(
        revision,
        spec,
        candidate_set_sha256='e' * 64,
        candidates=_candidates(),
        binding=_binding(),
    )

    assert first.batch_run_id != second.batch_run_id
    assert first.created_at_utc != ''
    assert second.created_at_utc != ''
    assert first.batch_spec_sha256 == second.batch_spec_sha256
    assert first.binding_sha256 == second.binding_sha256
    assert first.requests == second.requests


def _response_payload(**overrides) -> dict:
    payload = {
        'source_name': 'Left',
        'mic_position': 'Main',
        'message': 'fixture',
        'unit': 'SPL',
        'smoothing': 'None',
        'start_frequency_hz': 20.0,
        'points_per_octave': 96.0,
        'frequency_step_hz': None,
        'frequency_hz': [20.0, 40.0, 80.0],
        'magnitude': [80.0, 81.0, 79.0],
        'phase_deg': [0.0, 1.0, 2.0],
    }
    payload.update(overrides)
    return payload


def _execution_result(
    batch,
    candidate_id: str,
    *,
    request_sha256: str | None = None,
    response: dict | None = None,
    model_id: str = ROOMSIM_MODEL_ID,
    model_version: str = 'fixture-rew-1',
    adapter_version: str = ROOMSIM_BATCH_ADAPTER_VERSION,
) -> CadRoomSimExecutionResult:
    if request_sha256 is None:
        request_sha256 = next(
            item.request_sha256
            for item in batch.requests
            if item.candidate_id == candidate_id
        )
    if response is None:
        response = _response_payload()
    response_json = canonical_roomsim_result_json(response)
    response_sha256 = canonical_roomsim_result_sha256(response)
    identity = {
        'schema_version': CAD_ROOMSIM_EXECUTION_SCHEMA_VERSION,
        'request_sha256': request_sha256,
        'candidate_id': candidate_id,
        'model_id': model_id,
        'model_version': model_version,
        'adapter_version': adapter_version,
        'pre_state_sha256': 'a' * 64,
        'applied_state_sha256': 'b' * 64,
        'restored_state_sha256': 'a' * 64,
        'response': response,
        'response_sha256': response_sha256,
    }
    return CadRoomSimExecutionResult(
        request_sha256=request_sha256,
        candidate_id=candidate_id,
        model_id=model_id,
        model_version=model_version,
        adapter_version=adapter_version,
        pre_state_sha256='a' * 64,
        applied_state_sha256='b' * 64,
        restored_state_sha256='a' * 64,
        response_json=response_json,
        response_sha256=response_sha256,
        result_sha256=canonical_roomsim_result_sha256(identity),
    )


def _completed_attempt(batch, candidate_id: str, *, result) -> CadRoomSimCandidateAttempt:
    started = roomsim_result_timestamp_utc()
    completed = roomsim_result_timestamp_utc()
    identity = {
        'schema_version': CAD_ROOMSIM_ATTEMPT_SCHEMA_VERSION,
        'batch_run_id': batch.batch_run_id,
        'candidate_id': candidate_id,
        'attempt_index': 1,
        'status': 'completed',
        'result': None if result is None else result.model_dump(mode='json'),
        'result_sha256': None if result is None else result.result_sha256,
        'error_type': None,
        'error_message': None,
        'started_at_utc': started,
        'completed_at_utc': completed,
    }
    return CadRoomSimCandidateAttempt(
        attempt_id=new_roomsim_attempt_id(),
        batch_run_id=batch.batch_run_id,
        candidate_id=candidate_id,
        attempt_index=1,
        status='completed',
        result=result,
        result_sha256=None if result is None else result.result_sha256,
        started_at_utc=started,
        completed_at_utc=completed,
        attempt_sha256=canonical_roomsim_result_sha256(identity),
    )


def _saved_batch(tmp_path):
    revision, spec, repository = _repositories(tmp_path)
    batch = build_cad_roomsim_batch_spec(
        revision,
        spec,
        candidate_set_sha256='f' * 64,
        candidates=_candidates(),
        binding=_binding(),
    )
    repository.save_batch_spec(batch)
    return repository, batch


def test_roomsim_completed_attempt_binds_exact_request_and_round_trips(tmp_path) -> None:
    repository, batch = _saved_batch(tmp_path)

    result = _execution_result(batch, 'candidate-a')
    attempt = _completed_attempt(batch, 'candidate-a', result=result)
    repository.save_attempt(attempt)

    stored = repository.get_attempt(attempt.attempt_id)
    assert stored == attempt
    assert stored.result is not None
    assert stored.result.request_sha256 == batch.requests[0].request_sha256
    assert stored.result.model_id == ROOMSIM_MODEL_ID
    assert stored.result.adapter_version == ROOMSIM_BATCH_ADAPTER_VERSION
    response = roomsim_attempt_frequency_response(stored)
    assert response.frequency_hz == (20.0, 40.0, 80.0)
    assert response.level_db == (80.0, 81.0, 79.0)

    listed = repository.list_candidate_attempts(batch.batch_run_id, 'candidate-a')
    assert listed == (attempt,)
    assert repository.completed_candidate_ids(batch.batch_run_id) == {'candidate-a'}


def test_roomsim_attempt_rejects_result_bound_to_foreign_request_sha(tmp_path) -> None:
    repository, batch = _saved_batch(tmp_path)

    # A self-consistent fabricated result whose request sha matches nothing persisted.
    forged = _completed_attempt(
        batch,
        'candidate-a',
        result=_execution_result(batch, 'candidate-a', request_sha256='0' * 64),
    )
    with pytest.raises(ValueError, match='not bound to the exact candidate request'):
        repository.save_attempt(forged)


def test_roomsim_attempt_cannot_be_rebound_to_another_candidate_request(tmp_path) -> None:
    repository, batch = _saved_batch(tmp_path)
    request_b = next(
        item for item in batch.requests if item.candidate_id == 'candidate-b'
    )

    forged = _completed_attempt(
        batch,
        'candidate-a',
        result=_execution_result(
            batch,
            'candidate-a',
            request_sha256=request_b.request_sha256,
        ),
    )
    with pytest.raises(ValueError, match='not bound to the exact candidate request'):
        repository.save_attempt(forged)


def test_roomsim_attempt_rejects_response_selector_mismatch(tmp_path) -> None:
    repository, batch = _saved_batch(tmp_path)

    wrong_mic = _completed_attempt(
        batch,
        'candidate-a',
        result=_execution_result(
            batch,
            'candidate-a',
            response=_response_payload(mic_position='Side'),
        ),
    )
    with pytest.raises(ValueError, match='does not match the exact candidate request'):
        repository.save_attempt(wrong_mic)

    wrong_source = _completed_attempt(
        batch,
        'candidate-a',
        result=_execution_result(
            batch,
            'candidate-a',
            response=_response_payload(source_name=None),
        ),
    )
    with pytest.raises(ValueError, match='does not match the exact candidate request'):
        repository.save_attempt(wrong_source)


def test_roomsim_execution_result_rejects_schema_invalid_response(tmp_path) -> None:
    _repository, batch = _saved_batch(tmp_path)

    # Arbitrary canonical JSON is not Room Simulator response evidence.
    with pytest.raises(ValueError):
        _execution_result(
            batch,
            'candidate-a',
            response={'frequency_hz': [20.0], 'magnitude': [80.0]},
        )
    # Aligned frequency/magnitude axes are required.
    with pytest.raises(ValueError):
        _execution_result(
            batch,
            'candidate-a',
            response=_response_payload(magnitude=[80.0]),
        )
    # Unknown payload keys are rejected.
    with pytest.raises(ValueError):
        _execution_result(
            batch,
            'candidate-a',
            response=_response_payload(unexpected='extra'),
        )


def test_roomsim_execution_result_rejects_wrong_model_or_adapter(tmp_path) -> None:
    _repository, batch = _saved_batch(tmp_path)

    with pytest.raises(ValueError, match='model_id mismatch'):
        _execution_result(batch, 'candidate-a', model_id='other-model')
    with pytest.raises(ValueError, match='adapter_version mismatch'):
        _execution_result(batch, 'candidate-a', adapter_version='other-adapter')


def test_roomsim_completed_attempt_requires_execution_result(tmp_path) -> None:
    _repository, batch = _saved_batch(tmp_path)

    with pytest.raises(ValueError, match='requires the execution result'):
        _completed_attempt(batch, 'candidate-a', result=None)


def test_roomsim_attempt_reads_fail_closed_on_payload_swap(tmp_path) -> None:
    repository, batch = _saved_batch(tmp_path)
    attempt_a = _completed_attempt(
        batch,
        'candidate-a',
        result=_execution_result(batch, 'candidate-a'),
    )
    attempt_b = _completed_attempt(
        batch,
        'candidate-b',
        result=_execution_result(batch, 'candidate-b'),
    )
    repository.save_attempt(attempt_a)
    repository.save_attempt(attempt_b)

    with sqlite3.connect(repository.path) as connection:
        payload_b = connection.execute(
            'SELECT payload_json FROM cad_roomsim_candidate_attempts WHERE attempt_id=?',
            (attempt_b.attempt_id,),
        ).fetchone()[0]
        connection.execute(
            'UPDATE cad_roomsim_candidate_attempts SET payload_json=? WHERE attempt_id=?',
            (payload_b, attempt_a.attempt_id),
        )

    # attempt-a's row now carries a payload bound to candidate-b's exact request.
    with pytest.raises(ValueError):
        repository.get_attempt(attempt_a.attempt_id)
    with pytest.raises(ValueError):
        repository.list_candidate_attempts(batch.batch_run_id, 'candidate-a')
    with pytest.raises(ValueError):
        repository.list_attempts(batch.batch_run_id)
