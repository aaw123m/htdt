from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .activity_center import (
    ActivityCenter,
    NavigationPolicy,
    OperationClass,
    OperationProgress,
    OperationTransitionError,
    ProgressKind,
)
from .data_relocation import (
    DataRelocationBlockedError,
    ManagedDataRelocationPlan,
    execute_data_relocation,
    plan_data_relocation,
)

from PySide6.QtCore import QObject, QThread, Signal, Slot

from .automatic_backup import AutomaticBackupScheduler
from .cad_schema import (
    NATIVE_SCHEMA_VERSION,
    NativeSchemaCompatibility,
    native_schema_compatibility,
    read_native_schema_version,
)
from .native_backup import (
    DATABASE_NAME,
    BackupManifest,
    create_backup as native_create_backup,
    inspect_backup as native_inspect_backup,
    restore_backup as native_restore_backup,
    validate_backup as native_validate_backup,
)
from .native_upgrade import UpgradeEvent, execute_native_upgrade
from .persisted_data import backup_excluded_names
from .storage_maintenance import (
    StorageGcResult,
    StorageReport,
    plan_storage_gc,
    run_storage_gc,
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
    RELOCATE = 'relocate'
    SCAN_STORAGE = 'scan_storage'
    GC_STORAGE = 'gc_storage'


class DataOperationPhase(str, Enum):
    PREPARING = 'preparing'
    BACKING_UP = 'backing_up'
    VALIDATING = 'validating'
    RESTORING = 'restoring'
    RELOCATING = 'relocating'
    SCANNING = 'scanning'
    COLLECTING = 'collecting'
    RELOADING = 'reloading'


_OPERATION_TITLES: dict[DataOperationKind, str] = {
    DataOperationKind.CREATE_BACKUP: 'バックアップの作成',
    DataOperationKind.VALIDATE_RESTORE: 'バックアップの検証',
    DataOperationKind.RESTORE: 'バックアップからの復元',
    DataOperationKind.RELOCATE: 'データフォルダの移動',
    DataOperationKind.SCAN_STORAGE: 'ストレージのスキャン',
    DataOperationKind.GC_STORAGE: '未参照アセットの削除',
}


def _format_bytes(value: int) -> str:
    size = float(value)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(size):,} {unit}"
            return f"{size:,.1f} {unit}"
        size /= 1024.0
    return f"{value:,} B"


@dataclass(frozen=True)
class BackupMetadata:
    backup_path: Path
    created_at_utc: str
    application_version: str
    backup_schema_version: int
    # The native DB schema actually stored in the snapshotted/staged
    # database — a different authority from the archive-format
    # ``backup_schema_version``. ``0`` means a pre-versioning database.
    native_schema_version: int
    supported_native_schema_version: int
    native_schema_compatibility: NativeSchemaCompatibility
    archive_size_bytes: int
    database_size_bytes: int
    measurement_asset_count: int
    measurement_asset_size_bytes: int
    managed_size_bytes: int
    file_count: int
    manifest_sha256: str
    # Auxiliary registry components the archive carries (e.g.
    # auxiliary/commissioning-plans.json) and the persisted-data categories
    # the backup contract deliberately excludes — shown so 'whole-data
    # backup' is a declared contract, not whatever was enumerated (#769).
    auxiliary_components: tuple[str, ...] = ()
    excluded_categories: tuple[str, ...] = ()


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
    # #756: truthful restore outcome — the archive's own schema version,
    # the live version after any post-restore migration, and the #606
    # upgrade event that performed it (None when no migration was needed).
    restored_native_schema_version: int
    final_native_schema_version: int
    migration_performed: bool
    upgrade_event_id: str | None


@dataclass(frozen=True)
class RelocationResult:
    """Outcome of a managed data relocation (#621)."""

    plan: ManagedDataRelocationPlan
    destination_dir: Path
    parked_dir: Path


def _result_summary(kind: DataOperationKind, result: object) -> str:
    """Real outcome for the persisted Activity Center record.

    Mirrors what the completion dialogs already show — destination, counts,
    and recovery anchors — so the record a user returns to says what
    actually happened, not just that something finished.
    """
    if isinstance(result, BackupCreateResult):
        return f'バックアップを作成しました · {result.metadata.backup_path}'
    if isinstance(result, RestoreResult):
        summary = f'バックアップから復元しました · {result.metadata.backup_path}'
        if result.migration_performed:
            summary += (
                f' · DB移行 v{result.restored_native_schema_version}'
                f'→v{result.final_native_schema_version}'
            )
        if result.pre_restore_backup is not None:
            summary += f' · 復元前バックアップ: {result.pre_restore_backup}'
        return summary
    if isinstance(result, RestorePreview):
        return (
            f'バックアップを検証しました · {result.metadata.backup_path}'
            f'（{result.metadata.file_count} ファイル）'
        )
    if isinstance(result, RelocationResult):
        return (
            f'データフォルダを {result.destination_dir} に移動しました'
            f'（退避先: {result.parked_dir}）'
        )
    if isinstance(result, StorageGcResult):
        summary = (
            f'{result.deleted_files} 件の孤立アセットを削除し、'
            f'{_format_bytes(result.freed_bytes)} を回収しました'
        )
        if result.skipped_digests:
            summary += f'（スキップ {len(result.skipped_digests)} 件）'
        return summary
    if isinstance(result, StorageReport):
        if result.orphan_candidates:
            return (
                f'削除候補 {len(result.orphan_candidates)} 件 / '
                f'{_format_bytes(result.reclaimable_bytes)} を回収可能'
            )
        return 'スキャンが完了しました · 削除候補はありません'
    return f'{_OPERATION_TITLES[kind]}が完了しました'


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


def _metadata_from_manifest(
    backup_path: Path,
    manifest: BackupManifest,
    *,
    native_schema_version: int,
) -> BackupMetadata:
    database = next(entry for entry in manifest.files if entry.kind == 'database')
    assets = tuple(entry for entry in manifest.files if entry.kind == 'measurement_asset')
    return BackupMetadata(
        backup_path=Path(backup_path),
        created_at_utc=manifest.created_at_utc,
        application_version=manifest.application_version,
        backup_schema_version=manifest.schema_version,
        native_schema_version=native_schema_version,
        supported_native_schema_version=NATIVE_SCHEMA_VERSION,
        native_schema_compatibility=native_schema_compatibility(native_schema_version),
        archive_size_bytes=Path(backup_path).stat().st_size,
        database_size_bytes=database.size_bytes,
        measurement_asset_count=len(assets),
        measurement_asset_size_bytes=sum(entry.size_bytes for entry in assets),
        managed_size_bytes=sum(entry.size_bytes for entry in manifest.files),
        file_count=len(manifest.files),
        manifest_sha256=manifest.manifest_sha256,
        auxiliary_components=tuple(
            entry.path for entry in manifest.files if entry.kind == 'auxiliary'
        ),
        excluded_categories=backup_excluded_names(),
    )


class DataManagementBackend:
    """Thin application facade over the native backup authority.

    This class never inspects archive contents directly and never reproduces
    validation, integrity, hashing, pre-restore backup, or rollback semantics.
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)

    def current_native_schema_version(self) -> int:
        """Native DB schema version of the live data directory (0 if absent)."""

        return read_native_schema_version(self.data_dir / DATABASE_NAME)

    def create_backup(self, destination: Path) -> BackupCreateResult:
        destination = Path(destination)
        manifest = native_create_backup(self.data_dir, destination)
        # The manual generation already covers the current data, so the
        # automatic scheduler must not archive the same bytes again on its
        # next tick — best-effort mark, never gates the backup itself.
        try:
            AutomaticBackupScheduler(self.data_dir).record_external_generation()
        except Exception:  # noqa: BLE001
            pass
        # The live database is the snapshot source, so its stored version is
        # the version the archive actually contains — the manifest field was
        # verified against the staged snapshot during creation.
        return BackupCreateResult(
            manifest=manifest,
            metadata=_metadata_from_manifest(
                destination,
                manifest,
                native_schema_version=self.current_native_schema_version(),
            ),
        )

    def preview_restore(self, backup_path: Path) -> RestorePreview:
        backup_path = Path(backup_path)
        manifest, staged_schema_version = native_inspect_backup(backup_path)
        return RestorePreview(
            manifest=manifest,
            metadata=_metadata_from_manifest(
                backup_path,
                manifest,
                native_schema_version=staged_schema_version,
            ),
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

        # #756: a backup written by a newer build cannot be restored —
        # validate the archive's compatibility before the live cutover,
        # never discover it while reopening restored data. (native_restore
        # re-checks the staged bytes too; this fails fast on the preview's
        # own verdict.)
        if preview.metadata.native_schema_compatibility == 'incompatible_newer':
            raise ValueError(
                'backup was written by a newer HTDT data format '
                f'(v{preview.metadata.native_schema_version}); this build '
                f'supports up to v{NATIVE_SCHEMA_VERSION}'
            )

        if on_phase is not None:
            on_phase(DataOperationPhase.RESTORING, '現在のデータを退避して復元しています')
        manifest, pre_restore_backup = native_restore_backup(
            self.data_dir,
            backup_path,
        )

        # #756: an older-schema archive lands on the live root as-is. Route
        # the required data update through the journaled #606 upgrade
        # lifecycle — recovery snapshot, migration, verification, and a
        # persisted UpgradeEvent — instead of letting the next repository
        # open silently migrate the restored bytes.
        restored_version = self.current_native_schema_version()
        upgrade_event: UpgradeEvent | None = None
        if native_schema_compatibility(restored_version) in (
            'migration_required',
            'legacy_unversioned',
        ):
            if on_phase is not None:
                on_phase(
                    DataOperationPhase.VALIDATING,
                    '復元されたデータを現在の形式へ移行しています',
                )
            upgrade_event = execute_native_upgrade(self.data_dir)
        final_version = self.current_native_schema_version()

        return RestoreResult(
            manifest=manifest,
            metadata=_metadata_from_manifest(
                backup_path,
                manifest,
                # The archive's own stored schema — the same authority the
                # preview showed — not the post-upgrade live version.
                native_schema_version=preview.metadata.native_schema_version,
            ),
            pre_restore_backup=pre_restore_backup,
            restored_native_schema_version=restored_version,
            final_native_schema_version=final_version,
            migration_performed=upgrade_event is not None,
            upgrade_event_id=(
                upgrade_event.upgrade_id if upgrade_event is not None else None
            ),
        )

    def plan_relocation(
        self, destination_dir: Path
    ) -> ManagedDataRelocationPlan:
        """#621: preview moving the managed root to another location."""

        return plan_data_relocation(self.data_dir, Path(destination_dir))

    def relocate(self, destination_dir: Path) -> RelocationResult:
        """#621: copy → verify → cutover, then repoint the bootstrap config.

        The caller must have quiesced the data handle (no live repositories
        or running HTDT processes hold the root) before invoking this.
        """

        plan, parked = execute_data_relocation(
            self.data_dir, Path(destination_dir)
        )
        return RelocationResult(
            plan=plan,
            destination_dir=plan.destination_dir,
            parked_dir=parked,
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

    def mark_restart_required(self) -> None:
        """Transition a quiesced lifecycle to restart-required (#621).

        After a relocation succeeds the data root lives elsewhere; existing
        handles stay closed and the app must restart against the new root.
        """

        if self._state is not DataLifecycleState.QUIESCED:
            raise RuntimeError(
                f'data lifecycle is not quiesced: {self._state.value}'
            )
        self._state = DataLifecycleState.RESTART_REQUIRED


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


#: Operation threads that outlive their controller are re-owned here so a
#: destroyed controller never deletes a still-running QThread under itself —
#: the same contract as ``native_worker._LINGERING_THREADS``. The map also
#: pins each worker's Python reference until ``finished`` fires; the pair
#: cleans itself up through the already-connected ``finished -> quit ->
#: deleteLater`` chain once the job returns.
_LINGERING_OP_THREADS: dict[QThread, '_OperationWorker'] = {}


def lingering_op_thread_count() -> int:
    """Operation threads currently detached after their controller died."""
    return len(_LINGERING_OP_THREADS)


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
    relocation_completed = Signal(object)
    storage_scan_completed = Signal(object)
    storage_gc_completed = Signal(object)
    operation_failed = Signal(object)

    def __init__(
        self,
        backend: DataManagementBackend,
        lifecycle: ApplicationDataLifecycle,
        parent: QObject | None = None,
        *,
        activity_center: ActivityCenter | None = None,
    ) -> None:
        super().__init__(parent)
        self.backend = backend
        self.lifecycle = lifecycle
        # When wired, every operation is mirrored into the application
        # activity center (#603) so progress/history is visible app-wide.
        self.activity_center = activity_center
        if self.activity_center is not None:
            self.progress_changed.connect(self._mirror_progress)
        self._active: _ActiveOperation | None = None
        # destroy() must go through a plain callable: PySide6 silently never
        # delivers the signal to a bound method of the object being
        # destroyed (verified on PySide6 6.11), so a lambda keeps the detach
        # path live. Same shape as native_worker._detach_all.
        self.destroyed.connect(lambda: self._detach_active_thread())

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

    def relocate(self, destination_dir: Path) -> str:
        """#621: move the managed root after quiescing all data handles."""

        self._assert_owner_thread()
        self._assert_idle()
        operation_id = uuid4().hex
        try:
            self.lifecycle.begin_restore()
        except Exception as exc:
            self._emit_immediate_failure(
                operation_id,
                DataOperationKind.RELOCATE,
                DataOperationPhase.PREPARING,
                '移動のために現在のデータを閉じられませんでした',
                exc,
            )
            return operation_id

        def job(emit: Callable[[DataOperationPhase, str], None]) -> RelocationResult:
            emit(DataOperationPhase.RELOCATING, '移動先の空き容量と配置を確認しています')
            plan = self.backend.plan_relocation(destination_dir)
            if not plan.executable:
                raise DataRelocationBlockedError(
                    '; '.join(blocker.detail for blocker in plan.blockers)
                )
            emit(
                DataOperationPhase.RELOCATING,
                'データをコピーして検証しています',
            )
            return self.backend.relocate(destination_dir)

        return self._start(
            operation_id=operation_id,
            kind=DataOperationKind.RELOCATE,
            job=job,
            lifecycle_mode='relocate',
        )

    def scan_storage(self) -> str:
        """#501/#760: run the managed-assets inventory on the worker thread.

        ``plan_storage_gc`` walks every authority table, so it must not run
        on the UI thread for large stores.
        """

        self._assert_owner_thread()
        self._assert_idle()
        operation_id = uuid4().hex

        def job(emit: Callable[[DataOperationPhase, str], None]) -> StorageReport:
            emit(DataOperationPhase.SCANNING, '管理対象ストレージを確認しています')
            return plan_storage_gc(self.backend.data_dir)

        return self._start(
            operation_id=operation_id,
            kind=DataOperationKind.SCAN_STORAGE,
            job=job,
            lifecycle_mode='none',
        )

    def gc_storage(self) -> str:
        """Delete unreachable managed assets after re-proving unreachability."""

        self._assert_owner_thread()
        self._assert_idle()
        operation_id = uuid4().hex

        def job(emit: Callable[[DataOperationPhase, str], None]) -> StorageGcResult:
            emit(DataOperationPhase.COLLECTING, '未参照アセットを再検証して削除しています')
            return run_storage_gc(self.backend.data_dir)

        return self._start(
            operation_id=operation_id,
            kind=DataOperationKind.GC_STORAGE,
            job=job,
            lifecycle_mode='none',
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
        self._submit_operation(operation_id, kind, lifecycle_mode)
        self.busy_changed.emit(True)
        thread.start()
        return operation_id

    def _submit_operation(
        self,
        operation_id: str,
        kind: DataOperationKind,
        lifecycle_mode: str,
    ) -> None:
        center = self.activity_center
        if center is None:
            return
        exclusive = lifecycle_mode in ('backup', 'restore', 'relocate')
        center.submit(
            operation_id=operation_id,
            operation_kind=kind.value,
            operation_class=OperationClass.DATA_MANAGEMENT,
            title=_OPERATION_TITLES[kind],
            navigation_policy=(
                NavigationPolicy.EXCLUSIVE
                if exclusive
                else NavigationPolicy.BACKGROUNDABLE
            ),
            navigation_block_reason=(
                'データ管理操作の実行中はプロジェクトを切り替えられません'
                if exclusive
                else None
            ),
        )
        center.mark_running(operation_id)

    @Slot(object)
    def _mirror_progress(self, progress: object) -> None:
        center = self.activity_center
        if center is None:
            return
        operation_id = getattr(progress, 'operation_id', None)
        if operation_id is None:
            return
        snapshot = center.get(operation_id)
        if snapshot is None or not snapshot.is_active:
            return
        try:
            center.update_progress(
                operation_id,
                OperationProgress(
                    kind=ProgressKind.INDETERMINATE,
                    stage_label=progress.message_ja,
                ),
            )
        except OperationTransitionError:
            pass

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
            elif active.lifecycle_mode == 'relocate':
                # The data root lives at the destination now; existing
                # handles stay closed and the app must restart.
                self.lifecycle.mark_restart_required()
        except Exception as exc:
            lifecycle_error = exc

        result = active.result
        lifecycle_message = (
            (
                'データは復元されましたが、画面の再読み込みに失敗しました'
                if active.kind is DataOperationKind.RESTORE
                else 'バックアップは作成されましたが、編集状態の復帰に失敗しました'
            )
            if lifecycle_error is not None
            else None
        )
        self._finish_active()
        if self.activity_center is not None:
            try:
                # When the post-operation lifecycle step fails the data op
                # did complete, but recording a bare 'finished' claims a
                # clean outcome the operator never got — record the real
                # outcome text the failure card shows.
                self.activity_center.complete(
                    active.operation_id,
                    result_summary=lifecycle_message
                    or _result_summary(active.kind, result),
                )
            except OperationTransitionError:
                pass
        if lifecycle_error is not None:
            message = lifecycle_message
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
        elif active.kind is DataOperationKind.RELOCATE:
            self.relocation_completed.emit(result)
        elif active.kind is DataOperationKind.SCAN_STORAGE:
            self.storage_scan_completed.emit(result)
        elif active.kind is DataOperationKind.GC_STORAGE:
            self.storage_gc_completed.emit(result)
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
            elif active.lifecycle_mode in ('restore', 'relocate'):
                self.lifecycle.resume_after_restore_attempt()
        except Exception as lifecycle_exc:
            restart_required = self.lifecycle.restart_required
            lifecycle_detail = f' / reload failed: {lifecycle_exc}'

        self._finish_active()
        if self.activity_center is not None:
            try:
                self.activity_center.fail(
                    active.operation_id,
                    error_summary=self._failure_message(active.kind),
                )
            except OperationTransitionError:
                pass
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

    def _detach_active_thread(self) -> None:
        """Keep a running op thread alive if the controller is destroyed.

        The shell's close guard makes this latent in orderly shutdown, but if
        the controller is ever deleted mid-operation the still-running QThread
        — a child of this object — would be deleted under itself. The pair is
        re-owned at module scope until ``finished`` fires; result delivery and
        lifecycle completion belong to the dead controller and are dropped.
        """
        active = self._active
        if active is None:
            return
        self._active = None
        thread = active.thread
        # Pin the worker first: the record must never hold the last Python
        # reference while the thread may still be running.
        _LINGERING_OP_THREADS[thread] = active.worker
        thread.setParent(None)
        thread.finished.connect(
            lambda: _LINGERING_OP_THREADS.pop(thread, None)
        )
        if not thread.isRunning():
            # Finished between the last state check and the reparent.
            _LINGERING_OP_THREADS.pop(thread, None)

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
        # Lifecycle-begin failures never reach ``_start``; register the
        # operation then fail it so activity history still records it.
        if self.activity_center is not None:
            try:
                self._submit_operation(operation_id, kind, 'none')
            except OperationTransitionError:
                pass
            try:
                self.activity_center.fail(
                    operation_id, error_summary=message_ja
                )
            except OperationTransitionError:
                pass
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
        if kind is DataOperationKind.RELOCATE:
            return 'データ保存場所を移動できませんでした'
        if kind is DataOperationKind.SCAN_STORAGE:
            return 'ストレージを確認できませんでした'
        if kind is DataOperationKind.GC_STORAGE:
            return 'ストレージを整理できませんでした'
        return 'バックアップから復元できませんでした'
