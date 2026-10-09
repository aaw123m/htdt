"""Scheduled read-only storage-integrity scans (REV42).

The storage page's inventory is only produced when the user remembers to
open データ管理 and click the scan button — nothing ever surfaces
``missing_referenced`` (authority references whose files are gone) or
growing ``reclaimable_bytes`` by itself. That is exactly the class of
maintenance step the automation sweep covers: the inventory is read-only,
idempotent, and cheap enough to re-run on a timer.

This runner drives ``plan_storage_gc`` on a ``NativeWorkerPool`` thread on
a ``QTimer`` cadence while the session is alive. It is deliberately NOT
routed through ``DataManagementController.scan_storage``: that controller's
``storage_scan_completed`` signal is consumed by the UI to pop a
``QMessageBox`` every time, which would be a nagging dialog every interval.
Here only a *reportable* result surfaces anything — an Activity Center
entry (project scope, backgroundable) plus one statusbar line — and
deletion is never performed. ``run_storage_gc`` stays behind its manual
confirm dialog in the storage page.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from .error_boundary import (
    EXPECTED_OPERATION_ERRORS,
    is_authority_failure,
    report_boundary_failure,
)
from .native_worker import NativeWorkerPool, WORKER_CANCELLED
from .storage_maintenance import plan_storage_gc


_LOGGER = logging.getLogger(__name__)

_TASK_KEY = 'storage-watch.scan'

#: Default scan cadence. ``plan_storage_gc`` walks the data dir on a
#: worker thread; four hours keeps the check near-free while bounding how
#: stale the inventory can get between manual visits.
DEFAULT_INTERVAL_MS = 4 * 60 * 60 * 1000

#: Only reports of at least this much reclaimable space surface an
#: Activity entry — below the threshold the scan stays silent so the
#: cadence cannot spam the timeline for a handful of orphans.
REPORTABLE_RECLAIMABLE_BYTES = 64 * 1024 * 1024


class StorageWatchRunner(QObject):
    """Periodic read-only integrity scan; never deletes anything."""

    #: Emitted only for a reportable scan — ``missing_referenced > 0`` or
    #: ``reclaimable_bytes >= REPORTABLE_RECLAIMABLE_BYTES``. Carries the
    #: ``StorageReport`` itself; the app turns it into an activity entry.
    integrity_notice = Signal(object)
    #: (message) — scan failures surface once so a broken data dir is not
    #: silently un-checked forever.
    scan_failed = Signal(object)

    def __init__(
        self,
        data_dir: Path,
        preferences: object,
        parent: QObject | None = None,
        *,
        interval_ms: int = DEFAULT_INTERVAL_MS,
        reportable_bytes: int = REPORTABLE_RECLAIMABLE_BYTES,
    ) -> None:
        super().__init__(parent)
        self.data_dir = Path(data_dir)
        self._preferences = preferences
        self.reportable_bytes = max(0, int(reportable_bytes))
        self._pool = NativeWorkerPool(parent=self)
        self._timer = QTimer(self)
        self._timer.setInterval(max(1, int(interval_ms)))
        self._timer.timeout.connect(self._tick)
        self._in_flight = False
        self._closed = False

    def start(self) -> bool:
        """Arm the timer and run the first scan; False once closed."""

        if self._closed:
            return False
        self._timer.start()
        self._tick()
        return True

    def _enabled(self) -> bool:
        # Re-read every tick (same convention as the REW watch lane): a
        # settings toggle takes effect on the next poll with no restart,
        # and a store that cannot answer fails closed — off.
        try:
            return bool(
                self._preferences.get('maintenance.storage_watch_enabled')
            )
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: preference probe — expected store failures fail closed (off) and report; sealed-store failures and bugs propagate to diagnostics
            if is_authority_failure(exc):
                raise
            report_boundary_failure(exc, operation='ストレージ監視設定の読み取り')
            return False

    def _tick(self) -> None:
        # Absorbed while a scan is still running — the next interval
        # re-checks, so a reportable finding is delayed at most one period.
        if self._in_flight or self._closed or not self._enabled():
            return
        self._kick()

    def _kick(self) -> None:
        if self._in_flight or self._closed:
            return
        self._in_flight = True

        def job(cancel) -> object:
            return plan_storage_gc(
                self.data_dir, is_cancelled=cancel.is_set
            )

        try:
            self._pool.start(
                _TASK_KEY,
                job,
                self._on_completed,
                on_finished=self._job_finished,
            )
        except Exception:  # error-boundary: in-flight flag reset — a raise must not wedge ``_in_flight`` (later ticks would be absorbed forever); any failure type resets it before re-raising (noqa: BLE001)
            # A raise must not wedge ``_in_flight`` — later ticks would be
            # absorbed forever and the feature would silently stop.
            self._in_flight = False
            raise

    def _on_completed(self, _key: object, result: object, error: object) -> None:
        if self._closed:
            # Delivered late after shutdown() — drop it; emitting into a
            # torn-down app is how test-suite Qt crashes happen.
            return
        if error == WORKER_CANCELLED:
            return
        if error is not None:
            _LOGGER.warning('storage integrity scan failed: %s', error)
            self.scan_failed.emit(str(error))
            return
        report = result
        missing = len(getattr(report, 'missing_referenced', ()))
        reclaimable = int(getattr(report, 'reclaimable_bytes', 0))
        if missing or reclaimable >= self.reportable_bytes:
            self.integrity_notice.emit(report)

    def _job_finished(self, _key: object) -> None:
        self._in_flight = False

    def shutdown(self) -> None:
        self._closed = True
        self._timer.stop()
        self._pool.shutdown()
