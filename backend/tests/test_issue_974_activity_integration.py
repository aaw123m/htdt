"""#974: Activity Center integration — progress/cancel/results fixtures.

The registry (`ActivityCenter`) already provides the transitions; these
fixtures pin the *integration surface* the issue asks for:

* the shell strip keeps running counts, latest failure and navigation
  blockers visible from every workspace;
* every registered op carries a `WorkspaceDeepLink` back to its origin;
* cancel is wired to the cooperative executor callback, never a UI-only
  completion, and cancel during drain still ends CANCELLED;
* historical-input results are never mistaken for current/verified;
* a restarted app shows no phantom running workers;
* retry dispatch produces a fresh registered attempt with live cancel
  wiring, while unsafe re-runs need explicit re-authorization;
* state transitions are announced politely to assistive technology.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QMessageBox,
    QPushButton,
)

from htdt.activity_center import (
    ActivityCenter,
    Cancellability,
    NavigationPolicy,
    OperationClass,
    OperationProgress,
    OperationRetryRequest,
    OperationState,
    OperationTransitionError,
    ProgressKind,
    RetryPolicy,
    operation_state_label,
)
from htdt.workflow_navigation import (
    ApplicationDestinationId,
    WorkspaceDeepLink,
    WorkspaceId,
)


@pytest.fixture()
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _submit(center: ActivityCenter, **over) -> str:
    kwargs = {
        'operation_kind': 'prediction.run',
        'operation_class': OperationClass.COMPUTE,
        'title': '部屋の音響予測',
        'project_ref': 'doc-alpha',
        'document_ref': 'doc-alpha',
        'deep_link': WorkspaceDeepLink(WorkspaceId.ROOM, 'acoustics'),
    }
    kwargs.update(over)
    return center.submit(**kwargs)


def _running(center: ActivityCenter, **over) -> str:
    op_id = _submit(center, **over)
    center.mark_running(op_id)
    return op_id


# ---------------------------------------------------------------------------
# Registry-level integration contract
# ---------------------------------------------------------------------------


def test_failure_records_error_summary() -> None:
    center = ActivityCenter()
    op_id = _running(center)
    center.fail(op_id, error_summary='ソルバーが応答しませんでした')
    failed = center.require(op_id)
    assert failed.state == OperationState.FAILED
    assert failed.error_summary == 'ソルバーが応答しませんでした'
    # A failed op must surface in the recent list — the strip reads it.
    assert center.recent(1)[0].operation_id == op_id


def test_cancel_invokes_cooperative_callback() -> None:
    calls: list[str] = []
    center = ActivityCenter()
    op_id = _running(
        center,
        cancellability=Cancellability.CANCELLABLE,
        cancel_callback=lambda: calls.append('called'),
    )
    assert center.request_cancel(op_id)
    assert calls == ['called']  # the real executor callback ran
    assert center.require(op_id).state == OperationState.CANCELLATION_REQUESTED
    center.confirm_cancelled(op_id)
    assert center.require(op_id).state == OperationState.CANCELLED
    # 1 op = 1 record: the same id, now terminal — not a second row.
    assert len(center.recent()) == 1


def test_timeout_surfaces_as_failure_not_cancel() -> None:
    center = ActivityCenter()
    op_id = _running(center)
    center.fail(op_id, error_summary='時間切れ: 応答がありませんでした')
    op = center.require(op_id)
    assert op.state == OperationState.FAILED
    assert '時間切れ' in (op.error_summary or '')


def test_old_revision_result_is_historical_not_current() -> None:
    center = ActivityCenter()
    op_id = _running(
        center, input_authority_refs=('scene-revision:rev-1',)
    )
    center.complete(op_id, result_summary='予測を保存しました（3件）')
    assert center.require(op_id).state == OperationState.COMPLETED
    assert center.require(op_id).current_for_input

    # The caller reports WHICH authorities changed — rev-1 was superseded.
    center.note_authorities_changed({'scene-revision:rev-1'})

    op = center.require(op_id)
    assert op.state == OperationState.COMPLETED_FOR_HISTORICAL_INPUT
    assert not op.current_for_input
    # The label is honest — 旧入力, never current/verified.
    assert operation_state_label(op.state) == '完了（旧入力）'


def test_detached_drain_confirms_cancelled() -> None:
    """A worker outliving the drain still ends CANCELLED — its late
    completion is dropped by the released-task contract, so 'running
    forever' would be the dishonest record."""
    calls: list[str] = []
    center = ActivityCenter()
    op_id = _running(
        center,
        cancellability=Cancellability.CANCELLABLE,
        cancel_callback=lambda: calls.append('called'),
    )
    # Drain path (stop_busy / dispose): request then confirm.
    assert center.request_cancel(op_id)
    center.confirm_cancelled(op_id)
    assert calls == ['called']
    assert center.require(op_id).state == OperationState.CANCELLED
    assert center.active() == ()


def test_restart_shows_no_phantom_running(tmp_path: Path) -> None:
    """Active ops persisted at shutdown are diagnostics — the next
    session's registry must not display them as running."""
    center = ActivityCenter()
    calls: list[str] = []
    op_id = _running(
        center,
        cancellability=Cancellability.CANCELLABLE,
        cancel_callback=lambda: calls.append('called'),
    )
    history = tmp_path / 'activity-history.json'
    report = center.prepare_shutdown()
    assert op_id in {op.operation_id for op in report.detached_lingering}
    assert calls == ['called']  # cooperative cancel was requested
    assert center.require(op_id).state == OperationState.CANCELLATION_REQUESTED
    center.persist_history(history)

    # The next session: nothing is running. Persisted actives are
    # last-known diagnostics, never re-registered as live work.
    next_center = ActivityCenter()
    assert next_center.active() == ()
    assert next_center.navigation_blockers() == ()
    last_known = ActivityCenter.load_active_operations(history)
    assert op_id in {op.operation_id for op in last_known}


def test_exclusive_op_reports_navigation_block_reason() -> None:
    center = ActivityCenter()
    op_id = _running(
        center,
        operation_kind='measurement.batch_commit',
        title='バッチ測定の保存',
        navigation_policy=NavigationPolicy.EXCLUSIVE,
        navigation_block_reason='保存・適用を伴うため画面を切り替えられません',
    )
    blockers = center.navigation_blockers()
    assert [b.operation_id for b in blockers] == [op_id]
    assert blockers[0].navigation_block_reason
    # Backgroundable ops never block.
    _running(center, operation_kind='optimization.candidate_search')
    assert len(center.navigation_blockers()) == 1


def test_retry_dispatches_fresh_attempt_with_live_cancel() -> None:
    cancels: list[str] = []
    center = ActivityCenter()
    op_id = _running(
        center,
        cancellability=Cancellability.CANCELLABLE,
        cancel_callback=lambda: cancels.append('old'),
        retry_policy=RetryPolicy.SAFE_NEW_ATTEMPT,
        domain_payload={
            'retry': lambda op, new_id: OperationRetryRequest(
                cancel_callback=lambda: cancels.append('new'),
                domain_payload={'retry': lambda _o, _n: None},
            )
        },
    )
    center.fail(op_id, error_summary='タイムアウト')
    new_id = center.retry(
        op_id,
        retry_factory=lambda op: center.domain_payload_of(op.operation_id)[
            'retry'
        ](op, 'op-retry-1'),
        new_operation_id='op-retry-1',
    )
    assert new_id == 'op-retry-1'
    new_op = center.require(new_id)
    assert new_op.retry_of == op_id
    assert new_op.attempt == 2
    # The new attempt carries fresh wiring — cancel touches it, not the
    # old attempt's (cleared at its terminal transition).
    assert center.request_cancel(new_id)
    assert cancels == ['new']


def test_progress_is_honest_items_not_fake_percent() -> None:
    center = ActivityCenter()
    op_id = _running(center)
    center.update_progress(
        op_id,
        OperationProgress(
            kind=ProgressKind.ITEMS,
            done_units=3,
            total_units=10,
            unit_label='件',
        ),
    )
    op = center.require(op_id)
    assert op.progress.known_fraction() == pytest.approx(0.3)
    # Indeterminate progress must never fabricate a fraction.
    op2 = _running(center)
    assert center.require(op2).progress.known_fraction() is None


def test_deep_link_points_back_to_origin_workspace() -> None:
    center = ActivityCenter()
    op_id = _running(center)
    link = center.require(op_id).deep_link
    assert link is not None
    assert link.workspace == WorkspaceId.ROOM
    uri = link.as_uri()
    assert uri.startswith('htdt://')


# ---------------------------------------------------------------------------
# Shell strip (offscreen)
# ---------------------------------------------------------------------------


def _strip(qapp):
    from htdt.workflow_shell import ActivityStatusStrip

    strip = ActivityStatusStrip()
    strip.show()
    return strip


def _pump() -> None:
    QApplication.processEvents()
    QApplication.processEvents()


def test_strip_shows_running_count_and_states(qapp) -> None:
    from htdt.workflow_shell import ActivityStatusStrip

    strip = _strip(qapp)
    center = ActivityCenter()
    strip.bind(center)
    _pump()
    status = strip.findChild(type(strip._status_label), 'activityStatusText')
    assert status is not None and status.text() == '実行中の処理はありません'

    _running(center, title='探索候補の生成')
    _running(
        center,
        title='バッチ測定の保存',
        navigation_policy=NavigationPolicy.EXCLUSIVE,
        navigation_block_reason='保存を伴うため画面を切り替えられません',
    )
    cancelable = _running(
        center,
        cancellability=Cancellability.CANCELLABLE,
        cancel_callback=lambda: None,
    )
    center.request_cancel(cancelable)
    _pump()
    text = status.text()
    assert '実行中 2件' in text
    assert '中止要求中 1件' in text


def test_strip_shows_latest_failure_and_block_reason(qapp) -> None:
    strip = _strip(qapp)
    center = ActivityCenter()
    strip.bind(center)
    _pump()
    status = strip.findChild(
        type(strip._status_label), 'activityStatusText'
    )
    block = strip.findChild(type(strip._status_label), 'activityBlockText')
    assert status is not None and block is not None

    op_id = _running(
        center,
        title='バッチ測定の保存',
        navigation_policy=NavigationPolicy.EXCLUSIVE,
        navigation_block_reason='保存を伴うため画面を切り替えられません',
    )
    _pump()
    assert '画面を移動できません' in block.text()
    assert 'バッチ測定の保存' in block.text()

    center.fail(op_id, error_summary='書き込みが拒否されました')
    _pump()
    assert '直近: バッチ測定の保存' in status.text()
    assert '失敗' in status.text()
    assert '書き込みが拒否されました' in status.text()
    assert block.text() == ''


def test_strip_announces_state_changes_once(qapp, monkeypatch) -> None:
    import htdt.workflow_shell as shell_module

    announced: list[str] = []
    monkeypatch.setattr(
        shell_module, 'announce_status',
        lambda _w, message, **kw: announced.append(message),
    )
    strip = _strip(qapp)
    center = ActivityCenter()
    strip.bind(center)
    _pump()
    op_id = _running(center)
    _pump()
    center.complete(op_id, result_summary='done')
    _pump()
    # One announcement per *change* — a second refresh repeats nothing.
    center.update_progress(
        _running(center),
        OperationProgress(
            kind=ProgressKind.ITEMS,
            done_units=1,
            total_units=2,
            unit_label='件',
        ),
    )
    _pump()
    assert len(announced) == 1
    assert '実行中 → 完了' in announced[0]


def test_strip_open_button_requests_activity_page(qapp) -> None:
    strip = _strip(qapp)
    fired: list[bool] = []
    strip.openRequested.connect(lambda: fired.append(True))
    button = strip.findChild(QPushButton, 'activityOpenButton')
    assert button is not None
    button.click()
    assert fired == [True]


# ---------------------------------------------------------------------------
# Activity page rows + actions (offscreen)
# ---------------------------------------------------------------------------


def _activity_page(qapp, center: ActivityCenter, **kwargs):
    from htdt.application_pages import ActivityPage

    return ActivityPage(
        lambda doc_id, limit, after: ((), None),
        count_revisions=lambda doc_id: 0,
        list_operations=lambda: (
            *center.active(), *reversed(center.recent(50))
        ),
        document_id='doc-alpha',
        **kwargs,
    )


def test_activity_page_rows_show_states_and_progress(qapp) -> None:
    center = ActivityCenter()
    op_id = _running(center)
    center.update_progress(
        op_id,
        OperationProgress(
            kind=ProgressKind.ITEMS,
            done_units=4,
            total_units=10,
            unit_label='件',
        ),
    )
    canceled: list[str] = []
    page = _activity_page(
        qapp,
        center,
        cancel_operation=lambda oid: canceled.append(oid) or True,
    )
    page.show()
    _pump()
    table = page.operations_table
    assert table.rowCount() == 1
    state_cell = table.item(0, 0).text()
    progress_cell = table.item(0, 2).text()
    assert '実行中' in state_cell
    assert '4/10 件' in progress_cell
    # No ETA / invented %: the progress column only echoes real units.
    assert '%' not in progress_cell


def test_activity_page_cancel_button_uses_real_callback(qapp) -> None:
    calls: list[str] = []
    center = ActivityCenter()
    _running(
        center,
        cancellability=Cancellability.CANCELLABLE,
        cancel_callback=lambda: calls.append('cancelled'),
    )
    page = _activity_page(
        qapp,
        center,
        cancel_operation=lambda oid: center.request_cancel(oid),
    )
    page.show()
    _pump()
    table = page.operations_table
    actions = table.cellWidget(0, 4)
    cancel_btn = next(
        b for b in actions.findChildren(QPushButton) if b.text() == '中止'
    )
    cancel_btn.click()
    _pump()
    assert calls == ['cancelled']
    # The record moved through the real transition, not a UI-only flag.
    page._refresh_operations()
    _pump()
    assert 'キャンセル要求中' in table.item(0, 0).text()


def test_activity_page_unsafe_retry_requires_confirmation(qapp, monkeypatch) -> None:
    center = ActivityCenter()
    reran: list[str] = []
    op_id = _running(
        center,
        retry_policy=RetryPolicy.UNSAFE,
        domain_payload={
            'rerun': lambda _op: reran.append('rerun') or 'new-op-id'
        },
    )
    center.fail(op_id, error_summary='boom')
    # Wire like the app composition: _retry_activity_operation consults
    # the payload. Passed at construction so the row renders with the
    # action already available.
    def app_retry(oid: str) -> bool:
        snapshot = center.get(oid)
        if snapshot is None:
            return False
        payload = center.domain_payload_of(oid)
        if not isinstance(payload, dict):
            return False
        rerun = payload.get('rerun')
        if not callable(rerun):
            return False
        return rerun(snapshot) is not None

    page = _activity_page(qapp, center, retry_operation=app_retry)
    page.show()
    _pump()
    table = page.operations_table
    actions = table.cellWidget(0, 4)
    buttons = {
        b.text(): b for b in actions.findChildren(QPushButton)
    }
    assert '再試行（要確認）' in buttons
    accepted = {'value': False}
    monkeypatch.setattr(
        QMessageBox, 'question',
        staticmethod(lambda *a, **kw: (
            accepted.__setitem__('value', True),
            QMessageBox.StandardButton.Yes,
        )[1]),
    )
    buttons['再試行（要確認）'].click()
    _pump()
    assert accepted['value']  # confirmation was asked
    assert reran == ['rerun']  # fresh record, not the dead attempt


def test_activity_page_row_deep_link_navigation(qapp) -> None:
    opened: list[str] = []
    center = ActivityCenter()
    _running(
        center,
        deep_link=WorkspaceDeepLink(
            WorkspaceId.OPTIMIZATION, 'candidates'
        ),
    )
    page = _activity_page(
        qapp, center, open_link=lambda uri: opened.append(uri) or True
    )
    page.show()
    _pump()
    table = page.operations_table
    actions = table.cellWidget(0, 4)
    open_btn = next(
        b for b in actions.findChildren(QPushButton) if b.text() == '開く'
    )
    open_btn.click()
    assert opened and opened[0].startswith('htdt://')

    # Double-click activates the same link and moves keyboard focus.
    table.setFocus(Qt.FocusReason.OtherFocusReason)
    item = table.item(0, 1)
    table.itemDoubleClicked.emit(item)
    _pump()
    assert len(opened) == 2
