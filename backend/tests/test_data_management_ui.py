from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QMessageBox, QScrollArea

from htdt.data_management import (
    BackupCreateResult,
    BackupMetadata,
    DataOperationFailure,
    DataOperationKind,
    DataOperationPhase,
    DataOperationProgress,
    RestoreResult,
)
from htdt.data_management_ui import build_data_management_component
from htdt.data_relocation import plan_data_relocation


class _FakeController(QObject):
    busy_changed = Signal(bool)
    progress_changed = Signal(object)
    backup_created = Signal(object)
    restore_preview_ready = Signal(object)
    restore_completed = Signal(object)
    restore_drill_completed = Signal(object)
    relocation_completed = Signal(object)
    storage_scan_completed = Signal(object)
    storage_gc_completed = Signal(object)
    operation_failed = Signal(object)
    operation_cancelled = Signal(object)

    def __init__(self, data_dir: Path) -> None:
        super().__init__()
        self.backend = SimpleNamespace(
            data_dir=data_dir,
            current_native_schema_version=lambda: 5,
            plan_relocation=lambda destination: plan_data_relocation(
                data_dir, Path(destination)
            ),
        )
        self.lifecycle = SimpleNamespace(restart_required=False)
        self._busy = False
        self.backup_requests: list[Path] = []
        self.preview_requests: list[Path] = []
        self.restore_requests: list[object] = []
        self.relocate_requests: list[Path] = []
        self.drill_requests: list[tuple[Path, Path]] = []
        self.storage_scan_requests = 0
        self.storage_gc_requests = 0
        self.revalidate_calls = 0

    @property
    def is_busy(self) -> bool:
        return self._busy

    @property
    def can_close_application(self) -> bool:
        return not self._busy

    def create_backup(
        self, destination: Path, *, allow_stale: bool = False
    ) -> str:
        self.backup_requests.append(Path(destination))
        return "backup-op"

    def preview_restore(self, backup_path: Path) -> str:
        self.preview_requests.append(Path(backup_path))
        return "preview-op"

    def restore(self, preview: object) -> str:
        self.restore_requests.append(preview)
        return "restore-op"

    def relocate(self, destination: Path) -> str:
        self.relocate_requests.append(Path(destination))
        return "relocate-op"

    def restore_drill(self, backup_path: Path, sandbox: Path) -> str:
        self.drill_requests.append((Path(backup_path), Path(sandbox)))
        return "drill-op"

    def scan_storage(self) -> str:
        self.storage_scan_requests += 1
        return "storage-scan-op"

    def gc_storage(self) -> str:
        self.storage_gc_requests += 1
        return "storage-gc-op"

    def revalidate(self):
        self.revalidate_calls += 1
        return SimpleNamespace(summary_ja=lambda: "summary-ja-stub")

    def request_cancel(self) -> bool:
        return False

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.busy_changed.emit(busy)


class _Dialogs:
    def __init__(
        self,
        *,
        backup_path: Path | None = None,
        restore_path: Path | None = None,
        relocation_path: Path | None = None,
        drill_sandbox: Path | None = None,
    ) -> None:
        self.backup_path = backup_path
        self.restore_path = restore_path
        self.relocation_path = relocation_path
        self.drill_sandbox = drill_sandbox
        self.suggested_names: list[str] = []

    def choose_backup_destination(
        self,
        parent,
        *,
        suggested_name: str,
    ) -> Path | None:
        self.suggested_names.append(suggested_name)
        return self.backup_path

    def choose_restore_file(self, parent) -> Path | None:
        return self.restore_path

    def choose_relocation_destination(self, parent) -> Path | None:
        return self.relocation_path

    def choose_drill_sandbox(self, parent) -> Path | None:
        return self.drill_sandbox


@pytest.fixture(scope="module")
def app() -> QApplication:
    instance = QApplication.instance()
    if instance is None:
        instance = QApplication([])
    assert isinstance(instance, QApplication)
    return instance


def _metadata(path: Path) -> BackupMetadata:
    return BackupMetadata(
        backup_path=path,
        created_at_utc="2026-09-18T15:00:00+00:00",
        application_version="0.1.0",
        backup_schema_version=1,
        native_schema_version=5,
        supported_native_schema_version=5,
        native_schema_compatibility="current",
        archive_size_bytes=12 * 1024 * 1024,
        database_size_bytes=4 * 1024 * 1024,
        measurement_asset_count=3,
        measurement_asset_size_bytes=8 * 1024 * 1024,
        managed_size_bytes=12 * 1024 * 1024,
        file_count=4,
        manifest_sha256="a" * 64,
    )


def _preview(path: Path):
    return SimpleNamespace(metadata=_metadata(path), manifest=object())


def test_component_is_mountable_and_restore_is_fail_closed_until_preview(
    app: QApplication,
    tmp_path: Path,
) -> None:
    controller = _FakeController(tmp_path / "data")
    dialogs = _Dialogs(restore_path=tmp_path / "portable.htdt-backup")
    component = build_data_management_component(
        controller,
        dialogs=dialogs,
        confirm_restore=lambda _parent, _preview: True,
    )
    widget = component.widget

    assert widget.parent() is None
    assert widget.objectName() == "dataManagementWidget"
    assert not widget.restore_button.isEnabled()
    assert component.can_close_application
    assert component.before_deactivate() == (True, None)

    widget.select_restore_button.click()

    assert controller.preview_requests == [tmp_path / "portable.htdt-backup"]
    assert not widget.restore_button.isEnabled()
    assert widget.preview_metadata.isHidden()

    invalid = DataOperationFailure(
        operation_id="preview-op",
        kind=DataOperationKind.VALIDATE_RESTORE,
        phase=DataOperationPhase.VALIDATING,
        message_ja="バックアップを検証できませんでした",
        detail="injected invalid archive",
        exception_type="ValueError",
    )
    controller.operation_failed.emit(invalid)

    assert not widget.restore_button.isEnabled()
    assert not widget.status_card.isHidden()
    assert "検証できませんでした" in widget.status_title.text()

    component.close()


def test_backup_restore_and_pc_migration_entries_delegate_to_controller(
    app: QApplication,
    tmp_path: Path,
) -> None:
    backup_path = tmp_path / "migration.htdt-backup"
    restore_path = tmp_path / "from-old-pc.htdt-backup"
    controller = _FakeController(tmp_path / "data")
    dialogs = _Dialogs(backup_path=backup_path, restore_path=restore_path)
    confirmations: list[object] = []

    def confirm(_parent, preview) -> bool:
        confirmations.append(preview)
        return True

    component = build_data_management_component(
        controller,
        dialogs=dialogs,
        confirm_restore=confirm,
    )
    widget = component.widget

    widget.migration_export_button.click()
    widget.backup_button.click()

    assert controller.backup_requests == [backup_path, backup_path]
    assert all(name.endswith(".htdt-backup") for name in dialogs.suggested_names)

    backup_result = SimpleNamespace(metadata=_metadata(backup_path))
    controller.backup_created.emit(backup_result)
    assert not widget.result_metadata.isHidden()
    assert "バックアップを作成しました" == widget.status_title.text()

    widget.migration_import_button.click()
    assert controller.preview_requests == [restore_path]
    assert not widget.restore_button.isEnabled()

    preview = _preview(restore_path)
    controller.restore_preview_ready.emit(preview)

    assert widget.restore_button.isEnabled()
    assert not widget.preview_metadata.isHidden()
    assert "SQLite整合性" in widget.preview_metadata.validation_status.text()
    assert widget.preview_metadata._values["measurement_assets"].text().startswith("3 件")

    widget.restore_button.click()

    assert confirmations == [preview]
    assert controller.restore_requests == [preview]

    pre_restore = tmp_path / "data-pre-restore.htdt-backup"
    result = SimpleNamespace(
        metadata=_metadata(restore_path),
        pre_restore_backup=pre_restore,
    )
    controller.restore_completed.emit(result)

    assert not widget.result_metadata.isHidden()
    assert widget.preview_metadata.isHidden()
    assert pre_restore.name in widget.pre_restore_label.text()
    assert widget.status_title.text() == "復元が完了しました"

    component.close()


def test_busy_progress_failure_and_restart_required_are_presented(
    app: QApplication,
    tmp_path: Path,
) -> None:
    controller = _FakeController(tmp_path / "data")
    dialogs = _Dialogs(restore_path=tmp_path / "restore.htdt-backup")
    component = build_data_management_component(
        controller,
        dialogs=dialogs,
        confirm_restore=lambda _parent, _preview: True,
    )
    widget = component.widget

    widget.select_restore_button.click()
    controller.restore_preview_ready.emit(_preview(tmp_path / "restore.htdt-backup"))
    assert widget.restore_button.isEnabled()

    controller.set_busy(True)
    controller.progress_changed.emit(
        DataOperationProgress(
            operation_id="restore-op",
            kind=DataOperationKind.RESTORE,
            phase=DataOperationPhase.RESTORING,
            message_ja="現在のデータを退避して復元しています",
        )
    )

    assert not component.can_close_application
    assert component.before_deactivate()[0] is False
    assert not widget.progress_card.isHidden()
    assert "退避して復元" in widget.progress_label.text()
    assert not widget.backup_button.isEnabled()
    assert not widget.restore_button.isEnabled()

    controller.set_busy(False)
    controller.operation_failed.emit(
        DataOperationFailure(
            operation_id="restore-op",
            kind=DataOperationKind.RESTORE,
            phase=DataOperationPhase.RELOADING,
            message_ja="データは復元されましたが、画面の再読み込みに失敗しました",
            detail="injected reopen failure",
            exception_type="RuntimeError",
            restart_required=True,
            data_restored=True,
        )
    )

    assert component.can_close_application
    assert component.restart_required
    assert component.before_deactivate()[0] is False
    assert not widget.restart_card.isHidden()
    assert "再起動" in widget.restart_detail.text()
    assert not widget.backup_button.isEnabled()
    assert not widget.select_restore_button.isEnabled()
    assert not widget.restore_button.isEnabled()
    assert widget.preview_metadata.isHidden()

    component.close()


def test_saved_generations_are_browsable_and_previewable(
    app: QApplication,
    tmp_path: Path,
) -> None:
    """Round9 audit: the data-management page lists restorable generations
    directly instead of making the user hunt for the archive file."""

    data_dir = tmp_path / "data"
    generations_dir = tmp_path / "data-backups"
    generations_dir.mkdir(parents=True)
    newest = generations_dir / (
        "htdt-backup-manual-20260901T000000Z-abcd1234.htdt-backup"
    )
    newest.write_bytes(b"newest")
    (generations_dir / "unrelated.txt").write_text("noise")

    controller = _FakeController(data_dir)
    component = build_data_management_component(controller)
    widget = component.widget

    assert not widget.generations_row.isHidden()
    assert widget.generations_combo.count() == 1
    assert widget.generations_combo.itemText(0) == newest.name

    widget.generation_restore_button.click()

    # The picked generation goes through the same preview->confirm->
    # restore pipeline as a file-picked archive.
    assert controller.preview_requests == [newest]
    assert widget.generation_restore_button.isEnabled()
    assert not widget.restore_button.isEnabled()

    controller.restore_preview_ready.emit(_preview(newest))
    assert widget.restore_button.isEnabled()

    component.close()


def test_generations_row_hides_when_nothing_is_saved(
    app: QApplication,
    tmp_path: Path,
) -> None:
    controller = _FakeController(tmp_path / "data")
    component = build_data_management_component(controller)
    widget = component.widget

    assert widget.generations_combo.count() == 0
    assert widget.generations_row.isHidden()

    component.close()


def test_generations_row_stays_inside_narrow_viewport(
    app: QApplication,
    tmp_path: Path,
) -> None:
    """#992: the generations row must keep its minimumSizeHint inside the
    scroll viewport on narrow windows — the drill action folds into the
    操作 ▾ overflow menu and still runs the exact same slot, while the
    primary restore action never collapses."""

    data_dir = tmp_path / "data"
    generations_dir = tmp_path / "data-backups"
    generations_dir.mkdir(parents=True)
    newest = generations_dir / (
        "htdt-backup-manual-20260901T000000Z-abcd1234.htdt-backup"
    )
    newest.write_bytes(b"newest")
    sandbox = tmp_path / "drill-sandbox"

    controller = _FakeController(data_dir)
    dialogs = _Dialogs(drill_sandbox=sandbox)
    component = build_data_management_component(controller, dialogs=dialogs)
    widget = component.widget
    scroll = widget.findChild(QScrollArea, "dataManagementScroll")
    row = widget.generations_row

    widget.resize(800, 600)
    widget.show()
    app.processEvents()
    app.processEvents()

    # The row's minimum must never force horizontal scrolling at ~800px.
    assert row.minimumSizeHint().width() <= scroll.viewport().width()

    # The secondary drill action folds into the overflow menu; the
    # primary restore action never collapses.
    assert widget.drill_button.isHidden()
    assert not row.actions_overflow.isHidden()
    assert not widget.generation_restore_button.isHidden()

    # The folded menu action runs the exact same slot as the button.
    menu_actions = row.actions_menu.actions()
    assert [action.text() for action in menu_actions] == [
        "このバックアップを復元テスト…"
    ]
    menu_actions[0].trigger()
    assert controller.drill_requests == [(newest, sandbox)]

    # Widening unfolds the row again — the button returns and the
    # overflow menu hides.
    widget.resize(1400, 900)
    app.processEvents()
    app.processEvents()
    assert not widget.drill_button.isHidden()
    assert row.actions_overflow.isHidden()

    component.close()


def test_revalidate_button_runs_revalidation_and_shows_report(
    app: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-14 regression guard: the revalidation button calls
    ``controller.revalidate()`` and surfaces the report summary. The test
    double must shadow that signature — without it the whole lane is
    untestable and diverges silently from the real controller."""
    controller = _FakeController(tmp_path / "data")
    component = build_data_management_component(controller)
    widget = component.widget

    info_calls: list[tuple] = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda *args, **kwargs: info_calls.append(args),
    )

    widget.revalidate_button.click()

    assert controller.revalidate_calls == 1
    assert len(info_calls) == 1
    assert "HTDT 再検証" in str(info_calls[0])
    assert "summary-ja-stub" in str(info_calls[0])

    component.close()
