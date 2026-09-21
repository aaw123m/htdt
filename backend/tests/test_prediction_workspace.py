from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from htdt.cad_prediction_jobs import PredictionJobApplyContext, PredictionJobGuard
from htdt.cad_prediction_repository import CadPredictionRepository
from htdt.cad_prediction_request import rectangular_geometry_request_identity
from htdt.cad_predictions import analyze_native_rectangular_geometry
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import acoustic_reference_position, make_f1_scene
from htdt.prediction_workspace import PredictionWorkspaceWindow


class _StatusBar:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def showMessage(self, message: str) -> None:
        self.messages.append(message)


def _prepared_run(tmp_path: Path):
    """One completed rectangular run plus the token/context the window expects."""

    repository = SceneRepository(tmp_path / 'scenes.sqlite3')
    revision = repository.save(make_f1_scene(), parent_revision_id=None).revision
    receiver = next(
        entity.entity_id
        for entity in revision.document.entities
        if acoustic_reference_position(entity) is not None
    )
    prediction_repository = CadPredictionRepository(repository)
    constraint_hash = '1' * 64
    results = analyze_native_rectangular_geometry(
        revision,
        receiver,
        constraint_workspace_hash=constraint_hash,
    )
    identity = rectangular_geometry_request_identity(revision, receiver)
    guard = PredictionJobGuard()
    token = guard.submit(
        revision,
        model_id=identity.model_id,
        model_version=identity.model_version,
        parameters_json=identity.parameters_json,
        input_hash=identity.input_hash,
        constraint_workspace_hash=constraint_hash,
    )
    context = PredictionJobApplyContext(
        document_id=revision.document_id,
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        constraint_workspace_hash=constraint_hash,
    )
    return prediction_repository, revision, results, guard, token, context


def _window_double(
    prediction_repository: CadPredictionRepository,
    guard: PredictionJobGuard,
    token,
    context: PredictionJobApplyContext,
    status_bar: _StatusBar,
) -> SimpleNamespace:
    """Minimal stand-in driving the real ``_prediction_task_completed`` slot."""

    return SimpleNamespace(
        _disposed=False,
        _prediction_tokens={token.job_id: token},
        _current_prediction_token_id=token.job_id,
        prediction_run_button=None,
        prediction_job_guard=guard,
        prediction_repository=prediction_repository,
        prediction_selected_run_id=None,
        _current_prediction_context=lambda: context,
        _refresh_prediction_results=lambda: None,
        _rebuild=lambda: None,
        statusBar=lambda: status_bar,
    )


def test_workspace_persists_completed_run_through_one_atomic_save_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prediction_repository, _revision, results, guard, token, context = _prepared_run(tmp_path)
    status_bar = _StatusBar()
    window = _window_double(prediction_repository, guard, token, context, status_bar)

    calls = []
    original = prediction_repository.save_run

    def recording_save_run(items):
        calls.append(tuple(items))
        return original(items)

    monkeypatch.setattr(prediction_repository, 'save_run', recording_save_run)

    PredictionWorkspaceWindow._prediction_task_completed(window, token.job_id, results, None)

    assert calls == [results]
    assert prediction_repository.list_run(results[0].run_id) == results
    assert window.prediction_selected_run_id == results[0].run_id
    assert status_bar.messages
    assert '予測を保存しました' in status_bar.messages[-1]


def test_workspace_leaves_no_partial_run_when_second_result_commit_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prediction_repository, revision, results, guard, token, context = _prepared_run(tmp_path)
    status_bar = _StatusBar()
    window = _window_double(prediction_repository, guard, token, context, status_bar)

    original = CadPredictionRepository._save_result_in_transaction
    attempts: list[str] = []

    def fail_on_second_insert(self, connection, result) -> None:
        attempts.append(result.prediction_id)
        if len(attempts) == 2:
            raise sqlite3.OperationalError('injected mid-commit failure')
        return original(self, connection, result)

    monkeypatch.setattr(
        CadPredictionRepository,
        '_save_result_in_transaction',
        fail_on_second_insert,
    )

    PredictionWorkspaceWindow._prediction_task_completed(window, token.job_id, results, None)

    # Both inserts ran inside one transaction, so the rolled-back first result
    # is never visible as a partial run; the failure is surfaced instead.
    assert attempts == [results[0].prediction_id, results[1].prediction_id]
    assert prediction_repository.list_run(results[0].run_id) == ()
    assert prediction_repository.list_results(revision.document_id) == ()
    assert window.prediction_selected_run_id is None
    assert status_bar.messages
    assert '予測を保存できませんでした' in status_bar.messages[-1]


def test_workspace_rejected_run_batch_persists_nothing(tmp_path: Path) -> None:
    prediction_repository, revision, results, guard, token, context = _prepared_run(tmp_path)
    status_bar = _StatusBar()
    window = _window_double(prediction_repository, guard, token, context, status_bar)

    # A run whose records disagree on run_id is rejected by the repository's
    # batch contract before any row is written.
    forged = (results[0], results[1].model_copy(update={'run_id': 'foreign-run'}))

    PredictionWorkspaceWindow._prediction_task_completed(window, token.job_id, forged, None)

    assert prediction_repository.list_results(revision.document_id) == ()
    assert prediction_repository.list_run(results[0].run_id) == ()
    assert window.prediction_selected_run_id is None
    assert status_bar.messages
    assert '拒否しました' in status_bar.messages[-1]
