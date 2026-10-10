"""Support-page health-check runner (#1018).

``run_health_checks`` is read-only but not free — the storage probes open
the project database and the semantic audit replays every persisted
authority on a throwaway clone. Running it inside a button slot would
freeze the UI, so this runner executes the check on a
``NativeWorkerPool`` thread and publishes its lifecycle to the
application ``ActivityCenter`` like every other async lane:

* one in-flight run at a time — a second ``start`` while busy is refused
  instead of stacking workers or misreporting whose result landed;
* cancellation is the cooperative contract: the registered cancel
  callback hits the worker flag, the audit's ``is_cancelled`` poll
  unwinds, and the completed-cancelled emission lands as ``cancelled``,
  never as a result;
* the submitted operation carries the managed-data fingerprint as its
  input authority, so a project switch or restore reclassifies the run
  ``COMPLETED_FOR_HISTORICAL_INPUT`` instead of leaving an old data
  dir's verdict labelled current;
* the job factory is invoked on the UI thread — Qt-facing state
  (preferences, receiver snapshots) is captured there and frozen into
  the job before any worker code runs.

The runner owns no validator of its own: whatever job the composition
hands it is the whole report.
"""

from __future__ import annotations

import logging
from pathlib import Path
from threading import Event
from typing import Callable

from PySide6.QtCore import QObject, Signal

from .activity_center import (
    ActivityCenter,
    Cancellability,
    OperationClass,
    OperationTransitionError,
    NavigationPolicy,
    RetryPolicy,
)
from .automatic_backup import managed_data_fingerprint
from .native_worker import NativeWorkerPool, WORKER_CANCELLED
from .support_diagnostics import HealthReport, HealthStatus
from .user_facing_error import operation_error_message


_LOGGER = logging.getLogger(__name__)

_TASK_KEY = 'support.health-check'


class SupportHealthRunner(QObject):
    """Run one health check at a time; report on the UI thread."""

    #: A run was registered + dispatched.
    check_started = Signal()
    #: (HealthReport) — emitted only for a run that finished and was not
    #: cancelled; delivered on this object's thread.
    report_ready = Signal(object)
    #: (error payload) — the job raised; the activity record is FAILED.
    run_failed = Signal(object)
    #: The run was cancelled before producing a report.
    run_cancelled = Signal()
    #: Terminal housekeeping — always emitted once a started run ends,
    #: whatever the outcome, so UI can re-arm.
    run_finished = Signal()

    def __init__(
        self,
        data_dir: Path,
        activity_center: ActivityCenter,
        job_factory: Callable[[], Callable[[Event], HealthReport]],
        *,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._data_dir = Path(data_dir)
        self._activity_center = activity_center
        # Called on the UI thread inside ``start``; returns the worker
        # callable ``cancel_event -> HealthReport``. Keeping the two-step
        # shape makes the thread boundary explicit: Qt-facing snapshots
        # are taken in the factory, not inside the worker.
        self._job_factory = job_factory
        self._pool = NativeWorkerPool(parent=self)
        self._operation_id: str | None = None
        self._closed = False
        #: Fingerprint stamped when the last run was dispatched — lets the
        #: page show which data state a still-visible report belongs to.
        self.last_fingerprint: str = ''

    @property
    def busy(self) -> bool:
        return self._operation_id is not None

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    def start(self) -> bool:
        """Dispatch one run. False while a run is already in flight."""
        if self._closed or self._operation_id is not None:
            return False
        try:
            fingerprint = managed_data_fingerprint(self._data_dir)
        except Exception:  # error-boundary: fingerprint is best-effort metadata
            fingerprint = 'unavailable'
        self.last_fingerprint = fingerprint
        try:
            operation_id = self._activity_center.submit(
                operation_kind='support_health_check',
                operation_class=OperationClass.DATA_MANAGEMENT,
                title='アプリとプロジェクトの状態診断',
                input_authority_refs=(
                    f'managed-data:{fingerprint}',
                ),
                cancellability=Cancellability.CANCELLABLE,
                cancel_callback=lambda: self._pool.cancel(_TASK_KEY),
                retry_policy=RetryPolicy.SAFE_NEW_ATTEMPT,
                navigation_policy=NavigationPolicy.BACKGROUNDABLE,
            )
        except OperationTransitionError:
            _LOGGER.exception('health check operation could not register')
            return False
        job = self._job_factory()
        self._operation_id = operation_id
        try:
            self._activity_center.mark_running(operation_id)
        except OperationTransitionError:
            self._operation_id = None
            return False
        try:
            self._pool.start(
                _TASK_KEY,
                job,
                self._on_completed,
                on_finished=self._job_finished,
            )
        except Exception:  # error-boundary: dispatch boundary — any pool-start failure clears busy/operation state and fails the record so a run never looks in-flight (noqa: BLE001)
            # A dispatch failure must not wedge ``busy`` or strand a
            # RUNNING operation — fail the record and let the caller see
            # the error instead of pretending the run is in flight.
            self._operation_id = None
            try:
                self._activity_center.fail(
                    operation_id,
                    error_summary='診断を開始できませんでした',
                )
            except OperationTransitionError:
                pass
            raise
        self.check_started.emit()
        return True

    def request_cancel(self) -> bool:
        """Cooperative cancel through the registry; False when not live."""
        if self._operation_id is None:
            return False
        return self._activity_center.request_cancel(self._operation_id)

    def _on_completed(
        self, _key: object, result: object, error: object
    ) -> None:
        operation_id = self._operation_id
        if self._closed or operation_id is None:
            # Late delivery after shutdown — the record was already
            # cancelled; emitting into a torn-down page is how ghost
            # surfaces happen.
            return
        if error == WORKER_CANCELLED:
            try:
                self._activity_center.confirm_cancelled(operation_id)
            except OperationTransitionError:
                pass
            self.run_cancelled.emit()
            return
        if error is not None:
            _LOGGER.warning('support health check failed: %s', error)
            try:
                self._activity_center.fail(
                    operation_id,
                    error_summary=operation_error_message(error),
                )
            except OperationTransitionError:
                pass
            self.run_failed.emit(error)
            return
        report = result
        assert isinstance(report, HealthReport)
        overall = report.overall
        summary = {
            HealthStatus.PASS: '全項目正常',
            HealthStatus.ATTENTION: '要注意項目あり',
            HealthStatus.FAIL: '失敗項目あり',
        }.get(overall, '判定不明')
        failed = len(report.failed)
        if failed:
            summary += f'（失敗 {failed} 項目）'
        try:
            self._activity_center.complete(
                operation_id, result_summary=summary
            )
        except OperationTransitionError:
            pass
        self.report_ready.emit(report)

    def _job_finished(self, _key: object) -> None:
        self._operation_id = None
        self.run_finished.emit()

    def shutdown(self) -> None:
        """Close hook: cancel any live run and drain the pool.

        The operation record always lands in a terminal state — a check
        left RUNNING forever would keep telling the activity history a
        diagnosis is in progress after the page is gone.
        """
        self._closed = True
        operation_id = self._operation_id
        if operation_id is not None:
            self._activity_center.request_cancel(operation_id)
            try:
                self._activity_center.confirm_cancelled(operation_id)
            except OperationTransitionError:
                pass
            self._operation_id = None
        self._pool.shutdown()


__all__ = ['SupportHealthRunner']
