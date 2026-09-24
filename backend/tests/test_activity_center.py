"""#603 application activity center tests."""

from __future__ import annotations

import pytest

from htdt.activity_center import (
    ActivityCenter,
    Cancellability,
    NavigationPolicy,
    OperationClass,
    OperationProgress,
    OperationState,
    OperationTransitionError,
    ProgressKind,
    RetryPolicy,
)


def _submit(center: ActivityCenter, **over) -> str:
    kwargs = {
        'operation_kind': 'prediction.run',
        'operation_class': OperationClass.COMPUTE,
        'title': 'Prediction run',
    }
    kwargs.update(over)
    return center.submit(**kwargs)


def test_lifecycle_transitions() -> None:
    center = ActivityCenter()
    op_id = _submit(center)
    assert center.require(op_id).state == OperationState.QUEUED

    center.mark_preflighting(op_id)
    center.mark_running(op_id)
    running = center.require(op_id)
    assert running.state == OperationState.RUNNING
    assert running.started_at is not None

    done = center.complete(op_id, result_summary='200 candidates')
    assert done.state == OperationState.COMPLETED
    assert done.finished_at is not None
    assert center.active() == ()
    assert center.recent() == (done,)


def test_illegal_transition_rejected() -> None:
    center = ActivityCenter()
    op_id = _submit(center)
    center.mark_running(op_id)
    center.complete(op_id)
    with pytest.raises(OperationTransitionError):
        center.mark_running(op_id)
    with pytest.raises(OperationTransitionError):
        center.mark_preflighting(op_id)


def test_cancellation_contract() -> None:
    center = ActivityCenter()
    cancelled: list[str] = []

    not_cancellable = _submit(center)
    center.mark_running(not_cancellable)
    assert center.request_cancel(not_cancellable) is False

    cancellable = _submit(
        center,
        cancellability=Cancellability.CANCELLABLE,
        cancel_callback=lambda: cancelled.append('hit'),
    )
    center.mark_running(cancellable)
    assert center.request_cancel(cancellable) is True
    assert cancelled == ['hit']
    assert center.require(cancellable).state == OperationState.CANCELLATION_REQUESTED
    center.confirm_cancelled(cancellable)
    assert center.require(cancellable).state == OperationState.CANCELLED

    # A non-cancellable op cannot smuggle a cancel callback in.
    with pytest.raises(OperationTransitionError):
        _submit(center, cancel_callback=lambda: None)


def test_commit_point_blocks_cancel() -> None:
    center = ActivityCenter()
    op_id = _submit(
        center,
        cancellability=Cancellability.CANCEL_UNTIL_COMMIT,
    )
    center.mark_running(op_id)
    center.mark_commit_point(op_id)
    assert center.request_cancel(op_id) is False
    center.complete(op_id)


def test_navigation_blockers() -> None:
    center = ActivityCenter()
    _submit(center)
    assert not center.navigation_blocked()
    exclusive = _submit(
        center,
        navigation_policy=NavigationPolicy.EXCLUSIVE,
        navigation_block_reason='restore in progress',
    )
    assert center.navigation_blocked()
    blockers = center.navigation_blockers()
    assert [b.operation_id for b in blockers] == [exclusive]


def test_authorities_changed_marks_historical() -> None:
    center = ActivityCenter()
    completed = _submit(center, input_authority_refs=('scene-rev:9',))
    center.mark_running(completed)
    center.complete(completed)

    running = _submit(center, input_authority_refs=('scene-rev:9',))
    center.mark_running(running)

    center.note_authorities_changed({'scene-rev:9', 'scene-rev:10'})

    stale_completed = center.require(completed)
    assert stale_completed.state == OperationState.COMPLETED_FOR_HISTORICAL_INPUT
    assert stale_completed.current_for_input is False
    assert center.require(running).current_for_input is False


def test_retry_creates_new_attempt() -> None:
    center = ActivityCenter()
    op_id = _submit(center, retry_policy=RetryPolicy.SAFE_NEW_ATTEMPT)
    center.mark_running(op_id)
    center.fail(op_id, error_summary='REW timed out', diagnostic_id='abcd')

    attempt2 = center.retry(op_id)
    retry_op = center.require(attempt2)
    assert retry_op.attempt == 2
    assert retry_op.retry_of == op_id
    assert retry_op.state == OperationState.QUEUED

    # An active op cannot be retried.
    with pytest.raises(OperationTransitionError):
        center.retry(attempt2)
    # UNSAFE/NONE policies cannot retry.
    plain = _submit(center)
    center.mark_running(plain)
    center.fail(plain, error_summary='boom')
    with pytest.raises(OperationTransitionError):
        center.retry(plain)


def test_prepare_shutdown_accounts_for_active() -> None:
    center = ActivityCenter()
    cancellable = _submit(center, cancellability=Cancellability.CANCELLABLE)
    center.mark_running(cancellable)
    stuck = _submit(center)
    center.mark_running(stuck)

    report = center.prepare_shutdown()
    assert {op.operation_id for op in report.active_at_exit} == {cancellable, stuck}
    assert report.cancellation_requested == (cancellable,)
    assert report.detached_lingering == (cancellable,)


def test_progress_model_honest() -> None:
    with pytest.raises(ValueError):
        OperationProgress(kind=ProgressKind.DETERMINATE)
    with pytest.raises(ValueError):
        OperationProgress(kind=ProgressKind.INDETERMINATE, fraction=0.5)
    with pytest.raises(ValueError):
        OperationProgress(kind=ProgressKind.ITEMS, done_units=3)

    indeterminate = OperationProgress(kind=ProgressKind.INDETERMINATE)
    assert indeterminate.known_fraction() is None
    staged = OperationProgress(
        kind=ProgressKind.STAGE, stage_index=2, stage_count=4
    )
    assert staged.known_fraction() == 0.5
    items = OperationProgress(
        kind=ProgressKind.ITEMS, done_units=50, total_units=200
    )
    assert items.known_fraction() == 0.25


def test_history_persist_load_roundtrip(tmp_path) -> None:
    center = ActivityCenter()
    op_id = _submit(center, project_ref='demo')
    center.mark_running(op_id)
    center.complete(op_id)
    path = tmp_path / 'activity_history.json'
    center.persist_history(path)

    restored = ActivityCenter.load_history(path)
    assert len(restored) == 1
    assert restored[0].state == OperationState.COMPLETED
    assert restored[0].project_ref == 'demo'
    assert ActivityCenter.load_history(tmp_path / 'missing.json') == ()


def test_history_is_bounded() -> None:
    center = ActivityCenter(history_limit=5)
    for i in range(9):
        op_id = _submit(center, title=f'op {i}')
        center.mark_running(op_id)
        center.complete(op_id)
    recent = center.recent(50)
    assert len(recent) == 5
    assert recent[-1].title == 'op 8'
