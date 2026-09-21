from __future__ import annotations

import sqlite3
from threading import Event

import pytest

from htdt.cad_constraint_models import CadConstraintSet, CadPairDistanceConstraint
from htdt.cad_constraint_repository import CadConstraintRepository
from htdt.cad_prediction_repository import CadPredictionRepository
from htdt.cad_scene import F1_DOCUMENT_ID, acoustic_reference_position
from htdt.room_prediction import RoomPredictionController
from htdt.room_workspace import RoomWorkspaceController
from htdt.cad_repository import SceneRepository


def _controller(tmp_path):
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    room = RoomWorkspaceController(repository, F1_DOCUMENT_ID)
    prediction = RoomPredictionController(repository, room)
    receiver = next(
        entity.entity_id
        for entity in room.committed_document.entities
        if acoustic_reference_position(entity) is not None
    )
    return repository, room, prediction, receiver


def test_room_prediction_reuses_n70_identity_guard_and_repository(tmp_path) -> None:
    repository, room, prediction, receiver = _controller(tmp_path)

    spec = prediction.prepare_run(receiver)
    results = prediction._analyze(spec, Event())

    assert results is not None
    accepted = prediction.accept_results(spec, results)
    assert accepted == results
    stored = prediction.prediction_repository.list_run(results[0].run_id)
    assert stored == results
    assert all(
        result.scene_revision_id == room.working.source_revision_id
        and result.input_hash == spec.identity.input_hash
        and result.constraint_workspace_hash == spec.constraint_workspace_hash
        for result in stored
    )
    assert repository.latest(F1_DOCUMENT_ID) is not None
    assert prediction.result_is_current(stored[0]) is True

    prediction.dispose()


def test_room_prediction_rejects_delayed_result_after_scene_becomes_dirty(tmp_path) -> None:
    _repository, room, prediction, receiver = _controller(tmp_path)

    spec = prediction.prepare_run(receiver)
    results = prediction._analyze(spec, Event())
    assert results is not None

    room.add_object("furniture")
    assert room.is_dirty

    assert prediction.accept_results(spec, results) is None
    assert prediction.prediction_repository.list_results(F1_DOCUMENT_ID) == ()

    prediction.dispose()


def test_saved_room_prediction_becomes_stale_after_local_scene_edit(tmp_path) -> None:
    _repository, room, prediction, receiver = _controller(tmp_path)

    spec = prediction.prepare_run(receiver)
    results = prediction._analyze(spec, Event())
    assert results is not None
    accepted = prediction.accept_results(spec, results)
    assert accepted is not None
    assert prediction.result_is_current(accepted[0])

    room.add_object("furniture")

    assert prediction.result_is_current(accepted[0]) is False

    prediction.dispose()


def test_cancelled_room_prediction_token_cannot_be_persisted(tmp_path) -> None:
    _repository, _room, prediction, receiver = _controller(tmp_path)

    spec = prediction.prepare_run(receiver)
    results = prediction._analyze(spec, Event())
    assert results is not None
    prediction.job_guard.cancel(spec.token)

    assert prediction.accept_results(spec, results) is None
    assert prediction.prediction_repository.list_results(F1_DOCUMENT_ID) == ()

    prediction.dispose()


def test_room_prediction_rejects_result_after_constraint_workspace_changes(tmp_path) -> None:
    repository, room, prediction, receiver = _controller(tmp_path)

    spec = prediction.prepare_run(receiver)
    results = prediction._analyze(spec, Event())
    assert results is not None

    entity_ids = tuple(entity.entity_id for entity in room.committed_document.entities)
    assert len(entity_ids) >= 2
    constraints = CadConstraintSet(
        document_id=F1_DOCUMENT_ID,
        constraints=(
            CadPairDistanceConstraint(
                constraint_id="test-pair-distance",
                name="テスト離隔",
                entity_a=entity_ids[0],
                entity_b=entity_ids[1],
                min_m=0.10,
            ),
        ),
    )
    CadConstraintRepository(repository.path).save(constraints)

    assert prediction.accept_results(spec, results) is None
    assert prediction.prediction_repository.list_results(F1_DOCUMENT_ID) == ()

    prediction.dispose()


def test_room_prediction_remains_busy_until_worker_thread_finishes(tmp_path) -> None:
    _repository, _room, prediction, _receiver = _controller(tmp_path)

    class _RunningThread:
        @staticmethod
        def isRunning() -> bool:
            return True

    prediction._current_job_id = None
    prediction._tasks["finishing-job"] = (_RunningThread(), object())  # type: ignore[assignment]

    assert prediction.is_busy is True
    allowed, reason = prediction.before_deactivate()
    assert allowed is False
    assert reason is not None and "予測" in reason

    prediction._tasks.clear()
    prediction.dispose()


def test_room_prediction_persists_run_through_single_atomic_save(tmp_path, monkeypatch) -> None:
    _repository, _room, prediction, receiver = _controller(tmp_path)

    spec = prediction.prepare_run(receiver)
    results = prediction._analyze(spec, Event())
    assert results is not None

    calls = []
    original = prediction.prediction_repository.save_run

    def recording_save_run(items):
        calls.append(tuple(items))
        return original(items)

    monkeypatch.setattr(prediction.prediction_repository, 'save_run', recording_save_run)

    assert prediction.accept_results(spec, results) == results
    assert calls == [results]
    assert prediction.prediction_repository.list_run(results[0].run_id) == results

    prediction.dispose()


def test_room_prediction_second_result_failure_leaves_no_partial_run(tmp_path, monkeypatch) -> None:
    _repository, _room, prediction, receiver = _controller(tmp_path)

    spec = prediction.prepare_run(receiver)
    results = prediction._analyze(spec, Event())
    assert results is not None

    original = CadPredictionRepository._save_result_in_transaction
    attempts = []

    def fail_on_second_insert(self, connection, result):
        attempts.append(result.prediction_id)
        if len(attempts) == 2:
            raise sqlite3.OperationalError('injected mid-commit failure')
        return original(self, connection, result)

    monkeypatch.setattr(
        CadPredictionRepository, '_save_result_in_transaction', fail_on_second_insert
    )

    with pytest.raises(sqlite3.OperationalError, match='injected mid-commit'):
        prediction.accept_results(spec, results)

    assert len(attempts) == 2
    assert prediction.prediction_repository.list_run(results[0].run_id) == ()
    assert prediction.prediction_repository.list_results(F1_DOCUMENT_ID) == ()

    prediction.dispose()


def test_room_prediction_reports_persistence_failure_as_error_state(tmp_path, monkeypatch) -> None:
    _repository, _room, prediction, receiver = _controller(tmp_path)

    spec = prediction.prepare_run(receiver)
    results = prediction._analyze(spec, Event())
    assert results is not None

    def failing_save_run(items):
        raise sqlite3.OperationalError('disk image is malformed')

    monkeypatch.setattr(prediction.prediction_repository, 'save_run', failing_save_run)

    states = []
    prediction.stateChanged.connect(states.append)
    prediction._tokens[spec.token.job_id] = spec.token
    prediction._specs[spec.token.job_id] = spec
    prediction._task_completed(spec.token.job_id, results, None)
    prediction._task_thread_finished(spec.token.job_id)

    assert prediction.prediction_repository.list_run(results[0].run_id) == ()
    assert states
    assert states[-1].error is True
    assert '保存できませんでした' in states[-1].message

    prediction.dispose()
