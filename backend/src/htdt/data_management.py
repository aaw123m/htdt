from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable
from uuid import uuid4

from PySide6.QtCore import QObject, QThread, Signal, Slot

from .native_backup import (
    BackupManifest,
    create_backup as native_create_backup,
    restore_backup as native_restore_backup,
    validate_backup as native_validate_backup,
)


class DataManagementBusyError(RuntimeError):
    pass


class RestorePreviewStaleError(RuntimeError):
    pass


class DataLifecycleState(str, Enum):
    ACTIVE = 'active'
    QUIESCED = 'quiesced'
    RESTART_REQUIRED = 'restart_required'


class DataOperationKind(str, Enum):
    CREATE_BACKUP = 'create_backup'
    VALIDATE_RESTORE = 'validate_restore'
    RESTORE = 'restore'


class DataOperationPhase(str, Enum):
    PREPARING = 'preparing'
    BACKING_UP = 'backing_up'
    VALIDATING = 'validating'
    RESTORING = 'restoring'
    RELOADING = 'reloading'


@dataclass(frozen=True)
class BackupMetadata:
    backup_path: Path
    created_at_utc: str
    application_version: str
    backup_schema_version: int
    archive_size_bytes: int
    database_size_bytes: int
    measurement_asset_count: int
    measurement_asset_size_bytes: int
    managed_size_bytes: int
    file_count: int
    manifest_sha256: str


@dataclass(frozen=True)
class BackupCreateResult:
    manifest: BackupManifest
    metadata: BackupMetadata


@dataclass(frozen=True)
class RestorePreview:
    manifest: BackupManifest
    metadata: BackupMetadata


@dataclass(frozen=True)
class RestoreResult:
    manifest: BackupManifest
    metadata: BackupMetadata
    pre_restore_backup: Path | None


@dataclass(frozen=True)
class DataOperationProgress:
    operation_id: str
    kind: DataOperationKind
    phase: DataOperationPhase
    message_ja: str
    fraction: float | None = None
    can_cancel: bool = False


@dataclass(frozen=True)
class DataOperationFailure:
    operation_id: str
    kind: DataOperationKind
    phase: DataOperationPhase
    message_ja: str
    detail: str
    exception_type: str
    restart_required: bool = False
    data_restored: bool = False


def _metadata_from_manifest(backup_path: Path, manifest: BackupManifest) -> BackupMetadata:
    database = next(entry for entry in manifest.files if entry.kind == 'database')
    assets = tuple(entry for entry in manifest.files if entry.kind == 'measurement_asset')
    return BackupMetadata(
        backup_path=Path(backup_path),
        created_at_utc=manifest.created_at_utc,
        application_version=manifest.application_version,
        backup_schema_version=manifest.schema_version,
        archive_size_bytes=Path(backup_path).stat().st_size,
        database_size_bytes=database.size_bytes,
        measurement_asset_count=len(assets),
        measurement_asset_size_bytes=sum(entry.size_bytes for entry in assets),
        managed_size_bytes=sum(entry.size_bytes for entry in manifest.files),
        file_count=len(manifest.files),
        manifest_sha256=manifest.manifest_sha256,
    )


class DataManagementBackend:
    """Thin application facade over the native backup authority.

    This class never inspects archive contents directly and never reproduces
    validation, integrity, hashing, pre-restore backup, or rollback semantics.
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)

    def create_backup(self, destination: Path) -> BackupCreateResult:
        destination = Path(destination)
        manifest = native_create_backup(self.data_dir, destination)
        return BackupCreateResult(
            manifest=manifest,
            metadata=_metadata_from_manifest(destination, manifest),
        )

    def preview_restore(self, backup_path: Path) -> RestorePreview:
        backup_path = Path(backup_path)
        manifest = native_validate_backup(backup_path)
        return RestorePreview(
            manifest=manifest,
            metadata=_metadata_from_manifest(backup_path, manifest),
        )

    def restore(
        self,
        preview: RestorePreview,
        *,
        on_phase: Callable[[DataOperationPhase, str], None] | None = None,
    ) -> RestoreResult:
        backup_path = preview.metadata.backup_path

        if on_phase is not None:
            on_phase(DataOperationPhase.VALIDATING, '復元前にバックアップを再検証しています')
        current_manifest = native_validate_backup(backup_path)
        if current_manifest != preview.manifest:
            raise RestorePreviewStaleError(
                'backup archive changed after the restore preview was created'
            )

        if on_phase is not None:
            on_phase(DataOperationPhase.RESTORING, '現在のデータを退避して復元しています')
        manifest, pre_restore_backup = native_restore_backup(
            self.data_dir,
            backup_path,
        )
        return RestoreResult(
            manifest=manifest,
            metadata=_metadata_from_manifest(backup_path, manifest),
            pre_restore_backup=pre_restore_backup,
        )


class ApplicationDataLifecycle:
    """Application-level data handle lifecycle for destructive restore.

    The shell supplies callbacks that disable mutations, release every live
    workspace/repository/Scene reference, and rebuild them from the restored
    data directory. Old handles are never reattached after a restore attempt.
    """

    def __init__(
        self,
        *,
        freeze_mutations: Callable[[], None],
        release_data_handles: Callable[[], None],
        reopen_data_handles: Callable[[], None],
        thaw_mutations: Callable[[], None],
    ) -> None:
        self._freeze_mutations = freeze_mutations
        self._release_data_handles = release_data_handles
        self._reopen_data_handles = reopen_data_handles
        self._thaw_mutations = thaw_mutations
        self._state = DataLifecycleState.ACTIVE
        self._generation = 0
        self._mutations_frozen = False

    @property
    def state(self) -> DataLifecycleState:
        return self._state

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def restart_required(self) -> bool:
        return self._state is DataLifecycleState.RESTART_REQUIRED

    def _require_active(self) -> None:
        if self._state is not DataLifecycleState.ACTIVE or self._mutations_frozen:
            raise RuntimeError(f'data lifecycle is not active: {self._state.value}')

    def begin_backup(self) -> None:
        self._require_active()
        self._freeze_mutations()
        self._mutations_frozen = True

    def finish_backup(self) -> None:
        if not self._mutations_frozen or self._state is not DataLifecycleState.ACTIVE:
            return
        self._thaw_mutations()
        self._mutations_frozen = False

    def begin_restore(self) -> None:
        self._require_active()
        self._freeze_mutations()
        self._mutations_frozen = True
        try:
            self._release_data_handles()
        except Exception:
            self._thaw_mutations()
            self._mutations_frozen = False
            raise
        self._state = DataLifecycleState.QUIESCED

    def resume_after_restore_attempt(self) -> None:
        if self._state is not DataLifecycleState.QUIESCED:
            raise RuntimeError(f'data lifecycle is not quiesced: {self._state.value}')
        try:
            self._reopen_data_handles()
        except Exception:
            self._state = DataLifecycleState.RESTART_REQUIRED
            raise

        self._generation += 1
        self._state = DataLifecycleState.ACTIVE
        self._thaw_mutations()
        self._mutations_frozen = False


@dataclass
class _ActiveOperation:
    operation_id: str
    kind: DataOperationKind
    thread: QThread
    worker: '_OperationWorker'
    lifecycle_mode: str
    result: object | None = None
    failure_payload: tuple[DataOperationPhase, Exception] | None = None


class _OperationWorker(QObject):
    progress = Signal(object)
    succeeded = Signal(object)
    failed = Signal(object)
    finished = Signal()

    def __init__(
        self,
        *,
        operation_id: str,
        kind: DataOperationKind,
        job: Callable[[Callable[[DataOperationPhase, str], None]], object],
    ) -> None:
        super().__init__()
        self._operation_id = operation_id
        self._kind = kind
        self._job = job
        self._phase = DataOperationPhase.PREPARING

    def _emit_phase(self, phase: DataOperationPhase, message_ja: str) -> None:
        self._phase = phase
        self.progress.emit(
            DataOperationProgress(
                operation_id=self._operation_id,
                kind=self._kind,
                phase=phase,
                message_ja=message_ja,
            )
        )

    @Slot()
    def run(self) -> None:
        try:
            result = self._job(self._emit_phase)
        except Exception as exc:
            self.failed.emit((self._phase, exc))
        else:
            self.succeeded.emit(result)
        finally:
            self.finished.emit()


class DataManagementController(QObject):
    """Qt controller intended for Settings > Data Management in the UX110 shell.

    Public methods must be called on the controller's owning Qt thread. All
    archive/database work is executed on a dedicated worker QThread. Progress
    is intentionally phase-based and indeterminate because native_backup is
    the sole authority and does not expose byte-level progress callbacks.
    """

    busy_changed = Signal(bool)
    progress_changed = Signal(object)
    backup_created = Signal(object)
    restore_preview_ready = Signal(object)
    restore_completed = Signal(object)
    operation_failed = Signal(object)

    def __init__(
        self,
        backend: DataManagementBackend,
        lifecycle: ApplicationDataLifecycle,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.backend = backend
        self.lifecycle = lifecycle
        self._active: _ActiveOperation | None = None

    @property
    def is_busy(self) -> bool:
        return self._active is not None

    @property
    def can_close_application(self) -> bool:
        return self._active is None

    def _assert_owner_thread(self) -> None:
        if QThread.currentThread() is not self.thread():
            raise RuntimeError('data management controller must be called on its owning Qt thread')

    def _assert_idle(self) -> None:
        if self._active is not None:
            raise DataManagementBusyError(
                f'data management operation already running: {self._active.kind.value}'
            )

    def create_backup(self, destination: Path) -> str:
        self._assert_owner_thread()
        self._assert_idle()
        operation_id = uuid4().hex
        try:
            self.lifecycle.begin_backup()
        except Exception as exc:
            self._emit_immediate_failure(
                operation_id,
                DataOperationKind.CREATE_BACKUP,
                DataOperationPhase.PREPARING,
                'バックアップを開始できませんでした',
                exc,
            )
            return operation_id

        def job(emit: Callable[[DataOperationPhase, str], None]) -> BackupCreateResult:
            emit(DataOperationPhase.BACKING_UP, 'バックアップを作成・検証しています')
            return self.backend.create_backup(destination)

        return self._start(
            operation_id=operation_id,
            kind=DataOperationKind.CREATE_BACKUP,
            job=job,
            lifecycle_mode='backup',
        )

    def preview_restore(self, backup_path: Path) -> str:
        self._assert_owner_thread()
        self._assert_idle()
        operation_id = uuid4().hex

        def job(emit: Callable[[DataOperationPhase, str], None]) -> RestorePreview:
            emit(DataOperationPhase.VALIDATING, 'バックアップの整合性と互換性を検証しています')
            return self.backend.preview_restore(backup_path)

        return self._start(
            operation_id=operation_id,
            kind=DataOperationKind.VALIDATE_RESTORE,
            job=job,
            lifecycle_mode='none',
        )

    def restore(self, preview: RestorePreview) -> str:
        self._assert_owner_thread()
        self._assert_idle()
        operation_id = uuid4().hex
        try:
            self.lifecycle.begin_restore()
        except Exception as exc:
            self._emit_immediate_failure(
                operation_id,
                DataOperationKind.RESTORE,
                DataOperationPhase.PREPARING,
                '復元のために現在のデータを閉じられませんでした',
                exc,
            )
            return operation_id

        def job(emit: Callable[[DataOperationPhase, str], None]) -> RestoreResult:
            return self.backend.restore(preview, on_phase=emit)

        return self._start(
            operation_id=operation_id,
            kind=DataOperationKind.RESTORE,
            job=job,
            lifecycle_mode='restore',
        )

    def _start(
        self,
        *,
        operation_id: str,
        kind: DataOperationKind,
        job: Callable[[Callable[[DataOperationPhase, str], None]], object],
        lifecycle_mode: str,
    ) -> str:
        thread = QThread(self)
        worker = _OperationWorker(
            operation_id=operation_id,
            kind=kind,
            job=job,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self.progress_changed)
        worker.succeeded.connect(self._capture_success)
        worker.failed.connect(self._capture_failure)
        worker.finished.connect(thread.quit)
        thread.finished.connect(self._thread_finished)
        # NOTE: no ``finished -> worker.deleteLater``. Deleting a
        # moved-to-thread QObject while its QThread emits ``finished``
        # races native thread teardown (PySide6/Windows: sporadic access
        # violation / abort). ``_active.worker`` keeps the Python-owned C++
        # object alive until ``_thread_finished`` runs on this thread.
        thread.finished.connect(thread.deleteLater)

        self._active = _ActiveOperation(
            operation_id=operation_id,
            kind=kind,
            thread=thread,
            worker=worker,
            lifecycle_mode=lifecycle_mode,
        )
        self.busy_changed.emit(True)
        thread.start()
        return operation_id

    @Slot(object)
    def _capture_success(self, result: object) -> None:
        active = self._active
        if active is not None:
            active.result = result

    @Slot(object)
    def _capture_failure(self, payload: object) -> None:
        active = self._active
        if active is None:
            return
        phase, exc = payload
        active.failure_payload = (phase, exc)

    @Slot()
    def _thread_finished(self) -> None:
        active = self._active
        if active is None:
            return
        if active.failure_payload is not None:
            self._complete_failure(active)
        else:
            self._complete_success(active)

    def _complete_success(self, active: _ActiveOperation) -> None:
        lifecycle_error: Exception | None = None
        try:
            if active.lifecycle_mode == 'backup':
                self.lifecycle.finish_backup()
            elif active.lifecycle_mode == 'restore':
                self.progress_changed.emit(
                    DataOperationProgress(
                        operation_id=active.operation_id,
                        kind=active.kind,
                        phase=DataOperationPhase.RELOADING,
                        message_ja='復元後のデータを読み直しています',
                    )
                )
                self.lifecycle.resume_after_restore_attempt()
        except Exception as exc:
            lifecycle_error = exc

        result = active.result
        self._finish_active()
        if lifecycle_error is not None:
            message = (
                'データは復元されましたが、画面の再読み込みに失敗しました'
                if active.kind is DataOperationKind.RESTORE
                else 'バックアップは作成されましたが、編集状態の復帰に失敗しました'
            )
            self.operation_failed.emit(
                DataOperationFailure(
                    operation_id=active.operation_id,
                    kind=active.kind,
                    phase=DataOperationPhase.RELOADING,
                    message_ja=message,
                    detail=str(lifecycle_error),
                    exception_type=type(lifecycle_error).__name__,
                    restart_required=self.lifecycle.restart_required,
                    data_restored=active.kind is DataOperationKind.RESTORE,
                )
            )
            return

        if active.kind is DataOperationKind.CREATE_BACKUP:
            self.backup_created.emit(result)
        elif active.kind is DataOperationKind.VALIDATE_RESTORE:
            self.restore_preview_ready.emit(result)
        else:
            self.restore_completed.emit(result)

    def _complete_failure(self, active: _ActiveOperation) -> None:
        phase, exc = active.failure_payload or (
            DataOperationPhase.PREPARING,
            RuntimeError('data management worker stopped without a result'),
        )
        restart_required = False
        lifecycle_detail = ''
        try:
            if active.lifecycle_mode == 'backup':
                self.lifecycle.finish_backup()
            elif active.lifecycle_mode == 'restore':
                self.lifecycle.resume_after_restore_attempt()
        except Exception as lifecycle_exc:
            restart_required = self.lifecycle.restart_required
            lifecycle_detail = f' / reload failed: {lifecycle_exc}'

        self._finish_active()
        self.operation_failed.emit(
            DataOperationFailure(
                operation_id=active.operation_id,
                kind=active.kind,
                phase=phase,
                message_ja=self._failure_message(active.kind),
                detail=f'{exc}{lifecycle_detail}',
                exception_type=type(exc).__name__,
                restart_required=restart_required,
                data_restored=False,
            )
        )

    def _finish_active(self) -> None:
        self._active = None
        self.busy_changed.emit(False)

    def _emit_immediate_failure(
        self,
        operation_id: str,
        kind: DataOperationKind,
        phase: DataOperationPhase,
        message_ja: str,
        exc: Exception,
    ) -> None:
        self.operation_failed.emit(
            DataOperationFailure(
                operation_id=operation_id,
                kind=kind,
                phase=phase,
                message_ja=message_ja,
                detail=str(exc),
                exception_type=type(exc).__name__,
                restart_required=self.lifecycle.restart_required,
            )
        )

    @staticmethod
    def _failure_message(kind: DataOperationKind) -> str:
        if kind is DataOperationKind.CREATE_BACKUP:
            return 'バックアップを作成できませんでした'
        if kind is DataOperationKind.VALIDATE_RESTORE:
            return 'バックアップを検証できませんでした'
        return 'バックアップから復元できませんでした'
