"""Round 14 — notification & status-message truth regressions.

Covers the findings fixed this round:

* measure tool — the workspace status must not keep instructing the user
  to "click endpoints" after the result is computed (later clicks only
  pad ``_endpoints``) or after ``cancel()`` ends the interaction;
* Activity Center — the persisted ``result_summary`` for data-management
  operations carries the real outcome (destination, counts, recovery
  anchors), not a generic "finished" line;
* Activity page — the ops table follows live op mutations (queued via the
  same ``QTimer.singleShot(0, page, page.refresh)`` mechanism the app wires
  through ``activity_center.subscribe``).
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QTimer
from PySide6.QtWidgets import QApplication

from htdt.activity_center import ActivityCenter, OperationClass
from htdt.application_pages import ActivityPage
from htdt.data_management import (
    BackupCreateResult,
    BackupMetadata,
    DataOperationKind,
    RelocationResult,
    RestorePreview,
    RestoreResult,
    _result_summary,
)
from htdt.room_measure_input import RoomMeasureController
from htdt.storage_maintenance import (
    ManagedFileReport,
    StorageGcResult,
    StorageReport,
)


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _StatusWorkspace(QObject):
    """Minimal stand-in for RoomWorkspace's status sink."""

    def __init__(self) -> None:
        super().__init__()
        self.status_text = ""

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self.status_text = text

    def refresh(self) -> None:
        pass


class _Viewport:
    def pick_world_position(self, _point: QPointF):
        return (1.0, 0.0, 0.0)


def test_measure_status_tracks_result_and_cancel() -> None:
    _app()
    workspace = _StatusWorkspace()
    controller = RoomMeasureController(workspace, _Viewport())

    controller.begin()
    assert "端点をクリック" in workspace.status_text

    controller.pick_free_point(QPointF(0, 0))
    controller.pick_free_point(QPointF(1, 0))
    assert controller.result is not None
    # Result computed: the prompt must stop asking for endpoints — later
    # clicks only pad _endpoints and silently no-op.
    assert "端点をクリック" not in workspace.status_text
    assert "計測開始" in workspace.status_text

    controller.cancel()
    assert not controller.is_active
    assert workspace.status_text == "計測を中止しました"

    # Second Esc: idle no-op keeps the honest terminal text.
    controller.cancel()
    assert workspace.status_text == "計測を中止しました"


def _metadata() -> BackupMetadata:
    return BackupMetadata(
        backup_path=Path("D:/data/HTDT-backup.htdtb"),
        created_at_utc="2026-09-28T00:00:00Z",
        application_version="test",
        backup_schema_version=1,
        native_schema_version=3,
        supported_native_schema_version=3,
        native_schema_compatibility="current",
        archive_size_bytes=10,
        database_size_bytes=5,
        measurement_asset_count=0,
        measurement_asset_size_bytes=0,
        managed_size_bytes=10,
        file_count=7,
        manifest_sha256="0" * 64,
    )


def test_result_summary_carries_real_outcomes() -> None:
    metadata = _metadata()

    backup = _result_summary(
        DataOperationKind.CREATE_BACKUP,
        BackupCreateResult(manifest=None, metadata=metadata),
    )
    assert "HTDT-backup.htdtb" in backup

    preview = _result_summary(
        DataOperationKind.VALIDATE_RESTORE,
        RestorePreview(manifest=None, metadata=metadata),
    )
    assert "HTDT-backup.htdtb" in preview
    assert "7" in preview  # real file count

    restore = _result_summary(
        DataOperationKind.RESTORE,
        RestoreResult(
            manifest=None,
            metadata=metadata,
            pre_restore_backup=Path("D:/data/pre.htdtb"),
            restored_native_schema_version=2,
            final_native_schema_version=3,
            migration_performed=True,
            upgrade_event_id="evt",
        ),
    )
    assert "pre.htdtb" in restore
    assert "v2" in restore and "v3" in restore

    relocate = _result_summary(
        DataOperationKind.RELOCATE,
        RelocationResult(
            plan=None,
            destination_dir=Path("D:/new-data"),
            parked_dir=Path("D:/old-data.parked"),
        ),
    )
    assert "new-data" in relocate and "parked" in relocate

    orphan = ManagedFileReport(
        path=Path("f"),
        digest="d",
        size_bytes=4096,
        classification="unreferenced",
    )
    scan = _result_summary(
        DataOperationKind.SCAN_STORAGE,
        StorageReport(
            data_dir=Path("x"),
            database_bytes=0,
            diagnostics_bytes=0,
            categories=(),
            referenced_files=(),
            orphan_candidates=(orphan, orphan),
            temporary_files=(),
            unmanaged_files=(),
            missing_referenced=(),
        ),
    )
    assert "2 件" in scan and "8.0 KiB" in scan

    gc = _result_summary(
        DataOperationKind.GC_STORAGE,
        StorageGcResult(
            deleted_files=5,
            deleted_registry_rows=5,
            freed_bytes=2048,
            skipped_digests=("d1",),
        ),
    )
    assert "5 件" in gc and "2.0 KiB" in gc and "スキップ 1 件" in gc


def test_activity_page_ops_table_follows_op_mutations() -> None:
    """Mirrors the app's wiring: subscribe → queued refresh → live rows."""
    app = _app()
    center = ActivityCenter()

    def operations() -> tuple:
        return (*center.active(), *reversed(center.recent(30)))

    page = ActivityPage(
        lambda _doc, _limit, _offset: (),
        list_operations=operations,
    )
    try:
        QTimer.singleShot(0, page, page.refresh)  # no-op warm-up
        # Unscoped ops land in the app-global section (#1023).
        assert page.other_operations_table.rowCount() == 0

        center.subscribe(
            lambda _op: QTimer.singleShot(0, page, page.refresh)
        )
        op_id = center.submit(
            operation_kind="backup.create",
            operation_class=OperationClass.DATA_MANAGEMENT,
            title="バックアップの作成",
        )
        app.processEvents()
        assert page.other_operations_table.rowCount() == 1

        center.mark_running(op_id)
        center.complete(op_id, result_summary="done")
        app.processEvents()
        assert page.other_operations_table.rowCount() == 1
        assert "完了" in page.other_operations_table.item(0, 0).text()
    finally:
        page.deleteLater()
        app.processEvents()
