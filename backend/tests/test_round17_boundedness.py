"""Round 17: boundedness / memory & resource growth.

Regression probes for the leaks and unbounded structures this round fixed:

* ``ActivityCenter._records`` never evicted — every completed operation
  pinned its ``cancel_callback``/``domain_payload``/domain models for the
  lifetime of the (composition-scoped, effectively session-lifetime) center.
* ``WorkflowApplicationComposition._spawned_compositions`` accumulated one
  dead composition per respawn project switch; every intermediate hop was
  also pinned through the newest leaf.
* The activity mount's ``page.destroyed``-capturing closure re-subscribed on
  each mount without ever unsubscribing — one dead listener per remount.
* ``PredictionExecutionController._progress/_submitted/_cancelling`` grew by
  task id forever; ``MeasurementJobGuard``/``PredictionJobGuard._cancelled``
  grew by cancelled job id forever.
* ``cad_schema``'s two signature memos grew by database path forever.
* The storage health probe and the profile import dialog left an open
  sqlite connection / file handle on the success path.
"""

from __future__ import annotations

import gc
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
import weakref

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QCoreApplication
from PySide6.QtWidgets import QApplication

from htdt import cad_schema, support_diagnostics
from htdt.activity_center import (
    ActivityCenter,
    Cancellability,
    OperationClass,
    OperationState,
    OperationTransitionError,
    RetryPolicy,
)
from htdt.cad_measurement_jobs import (
    CANCELLED_JOB_LIMIT as MEASUREMENT_CANCELLED_JOB_LIMIT,
    MeasurementJobGuard,
)
from htdt.cad_prediction_jobs import (
    CANCELLED_JOB_LIMIT as PREDICTION_CANCELLED_JOB_LIMIT,
    PredictionJobGuard,
)
from htdt.cad_prediction_execution import (
    EXECUTION_PROGRESS_CACHE_LIMIT,
    PredictionExecutionController,
)
from htdt.cad_repository import SceneRepository
from htdt.workflow_application import WorkflowApplicationComposition


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _submit(center: ActivityCenter, **over) -> str:
    kwargs = {
        'operation_kind': 'prediction.run',
        'operation_class': OperationClass.COMPUTE,
        'title': 'Prediction run',
    }
    kwargs.update(over)
    return center.submit(**kwargs)


def _complete(center: ActivityCenter, operation_id: str) -> None:
    center.mark_running(operation_id)
    center.complete(operation_id)


def test_completed_operation_records_are_bounded() -> None:
    center = ActivityCenter(history_limit=8, record_limit=3)
    for index in range(8):
        operation_id = _submit(center)
        _complete(center, operation_id)
        assert len(center._records) <= 3, index
    assert len(center._history) <= 8


def test_get_and_history_reclassify_survive_record_eviction() -> None:
    center = ActivityCenter(history_limit=8, record_limit=3)
    ids = [
        _submit(center, input_authority_refs=('scene-rev:1',))
        for _ in range(8)
    ]
    for operation_id in ids:
        _complete(center, operation_id)

    evicted_record = ids[4]
    assert evicted_record not in center._records
    snapshot = center.get(evicted_record)
    assert snapshot is not None
    assert snapshot.state == OperationState.COMPLETED

    center.note_authorities_changed({'scene-rev:1'})
    snapshot = center.get(evicted_record)
    assert snapshot is not None
    assert snapshot.current_for_input is False
    assert snapshot.state == OperationState.COMPLETED_FOR_HISTORICAL_INPUT


def test_retry_of_evicted_operation_uses_history() -> None:
    center = ActivityCenter(history_limit=8, record_limit=3)
    original = _submit(center, retry_policy=RetryPolicy.SAFE_NEW_ATTEMPT)
    center.mark_running(original)
    center.fail(original, error_summary='boom')
    for _ in range(6):
        operation_id = _submit(center)
        _complete(center, operation_id)
    assert original not in center._records

    attempt2 = center.retry(original)
    assert attempt2 != original
    assert center.require(attempt2).attempt == 2
    child_retry = _submit(center, retry_of=original, attempt=2)
    assert center.require(child_retry).attempt == 2


def test_submit_rejects_duplicate_ids_evicted_to_history() -> None:
    center = ActivityCenter(history_limit=8, record_limit=3)
    original = _submit(center, operation_id='op-dup')
    _complete(center, original)
    for _ in range(6):
        operation_id = _submit(center)
        _complete(center, operation_id)
    assert original not in center._records

    with pytest.raises(OperationTransitionError):
        _submit(center, operation_id='op-dup')


def test_evicted_record_releases_domain_payload_and_callbacks() -> None:
    center = ActivityCenter(history_limit=8, record_limit=2)

    class _Token:
        pass

    payload = _Token()
    callback = _Token()
    payload_ref = weakref.ref(payload)
    callback_ref = weakref.ref(callback)

    original = _submit(
        center,
        domain_payload=payload,
        cancellability=Cancellability.CANCELLABLE,
        cancel_callback=lambda: callback,
    )
    _complete(center, original)
    for _ in range(4):
        operation_id = _submit(center)
        _complete(center, operation_id)
    assert original not in center._records

    del payload, callback
    gc.collect()
    assert payload_ref() is None
    assert callback_ref() is None


def test_active_records_are_never_evicted() -> None:
    center = ActivityCenter(history_limit=4, record_limit=2)
    active = _submit(center)
    for _ in range(6):
        operation_id = _submit(center)
        _complete(center, operation_id)
    assert active in center._records
    # total stays under the budget — the terminal tail yields to actives
    assert len(center._records) == 2


def test_unsubscribe_detaches_listener_idempotently() -> None:
    center = ActivityCenter()
    seen: list[str] = []
    other: list[str] = []
    center.subscribe(lambda snapshot: seen.append(snapshot.operation_id))
    center.subscribe(lambda snapshot: other.append(snapshot.operation_id))
    _submit(center)
    assert seen == other

    center.unsubscribe(center._listeners[0])
    _submit(center)
    assert len(seen) == 1
    assert len(other) == 2
    # idempotent — a stale unsubscribe must not remove other listeners
    center.unsubscribe(lambda snapshot: None)
    assert len(center._listeners) == 1


def _composition(tmp_path: Path, **kwargs) -> WorkflowApplicationComposition:
    repository = SceneRepository(tmp_path / "cad-scenes.sqlite3")
    return WorkflowApplicationComposition(repository, "document-1", **kwargs)


def _flush_deferred_delete() -> None:
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QCoreApplication.sendPostedEvents()


def _open_project(composition: WorkflowApplicationComposition, document_id: str):
    composition.project_library.open_project = lambda project_id: SimpleNamespace(
        document_id=document_id, project_id=project_id
    )
    composition._switch_to_project(
        SimpleNamespace(document_id=document_id, project_id=f"proj-{document_id}")
    )


def test_respawned_composition_chain_is_flattened_and_collectable(tmp_path: Path) -> None:
    app = _app()
    first = _composition(tmp_path)

    _open_project(first, 'doc-2')
    second = first._spawned_compositions[0]
    second_ref = weakref.ref(second)
    assert second.live_composition() is second

    _open_project(second, 'doc-3')
    third = second._spawned_compositions[0]

    # Every ancestor now points straight at the newest leaf — the dead
    # intermediate keeps no strong path to or from the chain.
    assert first._spawned_compositions == [third]
    assert second._spawned_compositions == [third]
    assert first.live_composition() is third

    del second
    _flush_deferred_delete()
    gc.collect()
    app.processEvents()
    assert second_ref() is None


def test_activity_mount_listener_released_on_dispose(tmp_path: Path) -> None:
    app = _app()
    composition = _composition(tmp_path)
    center = composition.activity_center
    baseline = len(center._listeners)

    mount = composition._make_activity()
    assert len(center._listeners) == baseline + 1

    mount.widget.deleteLater()
    _flush_deferred_delete()
    app.processEvents()
    assert len(center._listeners) == baseline

    # remount after dispose — same path as in-place project switch
    mount2 = composition._make_activity()
    assert len(center._listeners) == baseline + 1
    mount2.widget.deleteLater()
    _flush_deferred_delete()
    app.processEvents()
    assert len(center._listeners) == baseline


def test_job_guard_cancelled_registries_are_bounded(tmp_path: Path) -> None:
    revision = SimpleNamespace(
        document_id='doc', revision_id='rev-1', content_hash='h1'
    )
    measurement = SimpleNamespace(
        document_id='doc',
        scene_revision_id='rev-1',
        scene_content_hash='h1',
        measurement_entity_id='entity-1',
        measurement_position=SimpleNamespace(
            model_dump_json=lambda: '{}'
        ),
    )
    measurement_guard = MeasurementJobGuard()
    prediction_guard = PredictionJobGuard()

    for _ in range(MEASUREMENT_CANCELLED_JOB_LIMIT + 50):
        token = measurement_guard.submit(
            measurement, external_source_id='rew', query={}
        )
        measurement_guard.cancel(token)
    assert len(measurement_guard._cancelled) == MEASUREMENT_CANCELLED_JOB_LIMIT

    for _ in range(PREDICTION_CANCELLED_JOB_LIMIT + 50):
        token = prediction_guard.submit(
            revision, model_id='m', model_version='1',
            parameters_json='{}', input_hash='h',
        )
        prediction_guard.cancel(token)
    assert len(prediction_guard._cancelled) == PREDICTION_CANCELLED_JOB_LIMIT

    # the most recent cancel is still honoured
    token = prediction_guard.submit(
        revision, model_id='m', model_version='1',
        parameters_json='{}', input_hash='h',
    )
    prediction_guard.cancel(token)
    assert prediction_guard.is_cancelled(token)


def test_prediction_execution_bookkeeping_is_bounded() -> None:
    executor = SimpleNamespace(
        progress_sink=None,
        cancel=lambda task_id: True,
        acquire_progress_sink=lambda sink: SimpleNamespace(
            release=lambda: None
        ),
    )
    controller = PredictionExecutionController(executor)

    def _task(index: int) -> str:
        return f'r140-execution-task:{index:064x}'

    for index in range(EXECUTION_PROGRESS_CACHE_LIMIT + 50):
        controller.mark_submitted(_task(index))
    assert len(controller._submitted) == EXECUTION_PROGRESS_CACHE_LIMIT
    assert len(controller._progress) == EXECUTION_PROGRESS_CACHE_LIMIT
    assert _task(0) not in controller._submitted

    for index in range(EXECUTION_PROGRESS_CACHE_LIMIT + 50):
        assert controller.request_cancellation(_task(index)) is True
    assert len(controller._cancelling) == EXECUTION_PROGRESS_CACHE_LIMIT

    view = controller.progress_view(_task(EXECUTION_PROGRESS_CACHE_LIMIT + 49))
    assert view.phase == 'CANCELLING'


def test_schema_signature_memos_are_bounded() -> None:
    ensured_backup = dict(cad_schema._ENSURED_SCHEMA_SIGNATURES)
    compat_backup = dict(cad_schema._COMPATIBLE_SCHEMA_SIGNATURES)
    try:
        cad_schema._ENSURED_SCHEMA_SIGNATURES.clear()
        cad_schema._COMPATIBLE_SCHEMA_SIGNATURES.clear()
        for index in range(cad_schema._SCHEMA_SIGNATURE_CACHE_LIMIT + 50):
            cad_schema._memoize_schema_signature(
                cad_schema._ENSURED_SCHEMA_SIGNATURES,
                f'/db/{index}',
                (1, 2, 3, 4, index),
            )
            cad_schema._memoize_schema_signature(
                cad_schema._COMPATIBLE_SCHEMA_SIGNATURES,
                f'/db/{index}',
                ((1, 2, 3, 4, index), 0),
            )
        assert (
            len(cad_schema._ENSURED_SCHEMA_SIGNATURES)
            == cad_schema._SCHEMA_SIGNATURE_CACHE_LIMIT
        )
        assert (
            len(cad_schema._COMPATIBLE_SCHEMA_SIGNATURES)
            == cad_schema._SCHEMA_SIGNATURE_CACHE_LIMIT
        )
        assert '/db/0' not in cad_schema._ENSURED_SCHEMA_SIGNATURES
        assert (
            f'/db/{cad_schema._SCHEMA_SIGNATURE_CACHE_LIMIT + 49}'
            in cad_schema._ENSURED_SCHEMA_SIGNATURES
        )
    finally:
        cad_schema._ENSURED_SCHEMA_SIGNATURES.clear()
        cad_schema._ENSURED_SCHEMA_SIGNATURES.update(ensured_backup)
        cad_schema._COMPATIBLE_SCHEMA_SIGNATURES.clear()
        cad_schema._COMPATIBLE_SCHEMA_SIGNATURES.update(compat_backup)


def test_database_health_check_closes_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / 'cad-scenes.sqlite3'
    connection = sqlite3.connect(database)
    connection.execute('CREATE TABLE seed(x)')
    connection.commit()
    connection.close()

    closed: list[bool] = []
    real_connect = sqlite3.connect

    class _TrackedConnection:
        def __init__(self, *args, **kwargs):
            self._inner = real_connect(*args, **kwargs)

        def execute(self, *args, **kwargs):
            return self._inner.execute(*args, **kwargs)

        def close(self):
            closed.append(True)
            self._inner.close()

    monkeypatch.setattr(
        support_diagnostics.sqlite3,
        'connect',
        lambda *args, **kwargs: _TrackedConnection(*args, **kwargs),
    )

    results = support_diagnostics._check_database(tmp_path)
    # every connection the probe opened was explicitly closed
    assert closed and all(closed)
    assert any(r.check_id == 'storage.sqlite_quick_check' for r in results)
