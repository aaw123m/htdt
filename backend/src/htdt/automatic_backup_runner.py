"""In-app periodic automatic-backup trigger (#755).

The scheduler's ``'periodic'`` evaluation point was designed to be driven
while the UI is alive — but until now nothing in the app ever invoked it,
so a normal GUI session never produced an automatic backup (the
``--automatic-backup`` CLI flag only fires when an external scheduled task
calls it, which nothing registers).

This runner performs the check on a ``NativeWorkerPool`` thread after the
shell is up: ``evaluate('periodic')`` decides cheaply whether anything is
due; only a real backup run surfaces an Activity Center entry and a status
message. A skipped check stays silent — it must not nag every launch.
Failure is reported, never fatal: the app runs fine without the backup.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from .automatic_backup import AutomaticBackupScheduler
from .native_worker import NativeWorkerPool, WORKER_CANCELLED


_LOGGER = logging.getLogger(__name__)

_TASK_KEY = 'automatic-backup.periodic'


class AutomaticBackupRunner(QObject):
    """One-shot-per-launch due check + run for automatic backups."""

    #: Emitted (queued to the GUI thread) only when a backup actually ran.
    backup_started = Signal()
    #: (result, error): result is the ``(path, manifest)`` pair or None when
    #: no generation was due; error is the message text or None.
    backup_completed = Signal(object, object)

    def __init__(
        self,
        data_dir: Path,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.data_dir = Path(data_dir)
        self._pool = NativeWorkerPool(parent=self)
        self._attempted = False
        self._closed = False

    def start(self) -> bool:
        """Kick off the due check once per runner; False if already asked."""

        if self._attempted or self._closed:
            return False
        self._attempted = True

        def job(_cancel) -> object:
            scheduler = AutomaticBackupScheduler(self.data_dir)
            should, reason = scheduler.evaluate('periodic')
            if not should:
                _LOGGER.info('automatic backup not due: %s', reason)
                return None
            # Cheap cooperative-cancellation seam between the read-only
            # evaluate and the copy phase — a window closing during the scan
            # skips the backup entirely instead of starting it mid-shutdown.
            if _cancel.is_set():
                return None
            self.backup_started.emit()
            # Propagate the pool's cancel flag through the copy loop: a
            # window closing mid-archive must abort the write cooperatively
            # (BackupCancelledError → "cancelled" completion), never be
            # detached and killed mid-write past the shutdown budget.
            result = scheduler.run_due(
                'periodic', is_cancelled=_cancel.is_set
            )
            if result is None:
                # Re-evaluated as not due (e.g. a concurrent manual backup
                # satisfied the interval) — nothing to surface.
                return None
            return result

        self._pool.start(_TASK_KEY, job, self._on_completed)
        return True

    def _on_completed(self, _key: object, result: object, error: object) -> None:
        if self._closed:
            # A queued completion delivered after shutdown() must not
            # surface on the closing shell — the Activity Center entry and
            # statusbar write belong to a live composition.
            return
        if error == WORKER_CANCELLED:
            return
        self.backup_completed.emit(result, error)

    def shutdown(self) -> None:
        self._closed = True
        self._pool.shutdown()


__all__ = ['AutomaticBackupRunner']
