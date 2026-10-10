from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import file_dialog_memory
from .data_management import (
    BackupCreateResult,
    BackupMetadata,
    DataManagementController,
    DataOperationCancellation,
    DataOperationFailure,
    DataOperationKind,
    DataOperationProgress,
    RelocationResult,
    RestorePreview,
    RestoreResult,
)
from .automatic_backup import (
    AutomaticBackupPolicy,
    AutomaticBackupScheduler,
    backups_dir,
    list_restorable_backups,
)
from .data_relocation import ManagedDataRelocationPlan
from .legacy_data import inspect_legacy_store
from .restore_drill import RestoreDrillResult, latest_drill_result
from .storage_maintenance import (
    StorageGcResult,
    StorageReport,
)
from .ui_theme import (
    ControlSize,
    SemanticState,
    SurfaceRole,
    TypographyRole,
    set_control_size,
    set_primary_action,
    set_semantic_state,
    set_surface_role,
    set_typography_role,
)
from .user_facing_error import operation_error_message
from .error_boundary import (
    EXPECTED_OPERATION_ERRORS,
    is_authority_failure,
    report_boundary_failure,
)


_BACKUP_SUFFIX = ".htdt-backup"


class DataManagementDialogProvider(Protocol):
    def choose_backup_destination(
        self,
        parent: QWidget,
        *,
        suggested_name: str,
    ) -> Path | None: ...

    def choose_restore_file(self, parent: QWidget) -> Path | None: ...

    def choose_relocation_destination(self, parent: QWidget) -> Path | None: ...

    def choose_drill_sandbox(self, parent: QWidget) -> Path | None: ...


class QtDataManagementDialogProvider:
    """Native file-dialog adapter kept outside backup/restore semantics."""

    def choose_backup_destination(
        self,
        parent: QWidget,
        *,
        suggested_name: str,
    ) -> Path | None:
        selected, _filter = file_dialog_memory.get_save_file_name(
            parent,
            "バックアップの保存先",
            'data.backup.save',
            "HTDTバックアップ (*.htdt-backup)",
            suggested_name=suggested_name,
            default_dir=str(Path.home()),
        )
        if not selected:
            return None
        path = Path(selected)
        if not str(path).lower().endswith(_BACKUP_SUFFIX):
            path = Path(f"{path}{_BACKUP_SUFFIX}")
        return path

    def choose_restore_file(self, parent: QWidget) -> Path | None:
        selected, _filter = file_dialog_memory.get_open_file_name(
            parent,
            "復元するバックアップを選択",
            'data.backup.restore',
            "HTDTバックアップ (*.htdt-backup)",
            default_dir=str(Path.home()),
        )
        return None if not selected else Path(selected)

    def choose_relocation_destination(self, parent: QWidget) -> Path | None:
        selected = file_dialog_memory.get_existing_directory(
            parent,
            "データの移動先フォルダーを選択",
            'data.relocate',
            default_dir=str(Path.home()),
        )
        return None if not selected else Path(selected)

    def choose_drill_sandbox(self, parent: QWidget) -> Path | None:
        selected = file_dialog_memory.get_existing_directory(
            parent,
            "復元テスト用の隔離フォルダーを選択（本番データは変更されません）",
            'data.drill_sandbox',
            default_dir=str(Path.home()),
        )
        return None if not selected else Path(selected)


RestoreConfirmation = Callable[[QWidget, RestorePreview], bool]
RelocationConfirmation = Callable[[QWidget, ManagedDataRelocationPlan], bool]


def _default_relocate_confirmation(
    parent: QWidget, plan: ManagedDataRelocationPlan
) -> bool:
    box = QMessageBox(parent)
    box.setWindowTitle("データ保存場所の移動")
    box.setIcon(QMessageBox.Icon.Warning)
    box.setText(
        f"HTDTデータを {plan.destination_dir} へ移動します。"
    )
    box.setInformativeText(
        f"移動量: {_format_bytes(plan.total_bytes)}"
        f"（DB {_format_bytes(plan.database_bytes)} / "
        f"アセット {plan.asset_count} 件 {_format_bytes(plan.asset_bytes)}）\n"
        "元の場所は移動後も退避フォルダーとして残ります。"
        "完了後はHTDTの再起動が必要です。移動を開始しますか？"
    )
    box.setStandardButtons(
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
    )
    box.setDefaultButton(QMessageBox.StandardButton.Cancel)
    return box.exec() == QMessageBox.StandardButton.Yes


def _default_restore_confirmation(parent: QWidget, preview: RestorePreview) -> bool:
    box = QMessageBox(parent)
    box.setWindowTitle("バックアップから復元")
    box.setIcon(QMessageBox.Icon.Warning)
    box.setText(
        f"現在のHTDTデータを、バックアップ"
        f"「{preview.metadata.backup_path.name}」の内容に置き換えます。"
    )
    box.setInformativeText(
        f"バックアップ作成日時: {preview.metadata.created_at_utc}\n"
        "現在のデータは復元前バックアップとして自動保存されます。"
        "\n復元を開始しますか？"
    )
    box.setStandardButtons(
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
    )
    box.setDefaultButton(QMessageBox.StandardButton.Cancel)
    return box.exec() == QMessageBox.StandardButton.Yes


def _default_backup_name(now: datetime | None = None) -> str:
    # UTC + 'Z' marker: every persisted timestamp in the app is UTC, and an
    # unlabeled local stamp in the filename reads as a different instant.
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d-%H%MZ")
    return f"HTDT-backup-{stamp}{_BACKUP_SUFFIX}"


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


def _format_created_at(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).strftime("%Y/%m/%d %H:%M UTC")
    except ValueError:
        return value


#: Localized labels for the persisted-data registry names shown in the
#: backup contract's excluded list — the registry ids are internal
#: snake_case identifiers, not display text.
_EXCLUDED_COMPONENT_LABELS = {
    'application_preferences': 'アプリケーション設定',
    'reference_library_meta': '参照ライブラリ情報',
    'window_state_global': 'ウィンドウ状態（全体）',
    'window_state_projects': 'ウィンドウ状態（プロジェクト別）',
    'file_dialog_memory': 'ファイルダイアログの記憶',
    'activity_history': 'アクティビティ履歴',
    'automatic_backup_policy': '自動バックアップポリシー',
    'automatic_backup_state': '自動バックアップ状態',
    'upgrade_events': 'アップグレードイベント',
    'upgrade_state_marker': 'アップグレード状態マーカー',
    'upgrade_recovery': 'アップグレード復旧データ',
    'legacy_migration_journal': 'レガシー移行ジャーナル',
    'legacy_database': 'レガシーデータベース',
    'legacy_database_archives': 'レガシーデータベースアーカイブ',
    'legacy_assets': 'レガシーアセット',
    'legacy_assets_archives': 'レガシーアセットアーカイブ',
    'capture_receiver_state': 'キャプチャ受信状態',
    'diagnostics': '診断データ',
    'runtime_state': '実行時状態',
    'instance_lock': 'インスタンスロック',
    'htdt_instance_lock': 'インスタンスロック',
    'launch_intents_queue': '起動インテントキュー',
}

#: Storage inventory category ids -> operator-facing labels.
_STORAGE_CATEGORY_LABELS = {
    'native-database': 'ネイティブデータベース',
    'managed-assets': '管理対象アセット',
    'diagnostics': '診断データ',
}


def _excluded_component_label(name: str) -> str:
    return _EXCLUDED_COMPONENT_LABELS.get(name, name)


def _format_native_schema(metadata: BackupMetadata) -> str:
    state = metadata.native_schema_compatibility
    version = metadata.native_schema_version
    supported = metadata.supported_native_schema_version
    if state == "current":
        return f"v{version}（このアプリと同一）"
    if state == "migration_required":
        return f"v{version}（移行が必要 — 復元時にv{supported}へ更新）"
    if state == "incompatible_newer":
        return f"v{version}（このアプリでは非対応 — 対応はv{supported}まで）"
    return f"バージョン情報なし（従来形式 — 復元時にv{supported}へ移行）"


class BackupMetadataView(QFrame):
    """Presentation-only view of metadata already produced by the controller."""

    def __init__(self, *, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("dataManagementMetadata")
        set_surface_role(self, SurfaceRole.RAISED)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(10)

        self.title = QLabel(title, self)
        set_typography_role(self.title, TypographyRole.SECTION_TITLE)
        outer.addWidget(self.title)

        self.validation_status = QLabel(self)
        self.validation_status.setWordWrap(True)
        self.validation_status.hide()
        outer.addWidget(self.validation_status)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(20)
        form.setVerticalSpacing(8)
        outer.addLayout(form)

        self._values: dict[str, QLabel] = {}
        for key, label in (
            ("created_at", "作成日時"),
            ("application_version", "HTDTバージョン"),
            ("schema_version", "バックアップスキーマ"),
            ("native_schema", "DBスキーマ"),
            ("archive_size", "アーカイブ"),
            ("database_size", "データベース"),
            ("measurement_assets", "測定アセット"),
            ("auxiliary", "補助データ"),
            ("excluded", "バックアップ対象外"),
            ("managed_size", "管理対象合計"),
            ("file_count", "保存ファイル"),
            ("path", "ファイル"),
        ):
            value = QLabel("—", self)
            value.setTextInteractionFlags(value.textInteractionFlags())
            value.setWordWrap(key == "path")
            set_typography_role(value, TypographyRole.BODY)
            form.addRow(label, value)
            self._values[key] = value

    def set_metadata(self, metadata: BackupMetadata, *, validated: bool = False) -> None:
        self._values["created_at"].setText(_format_created_at(metadata.created_at_utc))
        self._values["application_version"].setText(metadata.application_version)
        self._values["schema_version"].setText(str(metadata.backup_schema_version))
        self._values["native_schema"].setText(_format_native_schema(metadata))
        self._values["archive_size"].setText(_format_bytes(metadata.archive_size_bytes))
        self._values["database_size"].setText(_format_bytes(metadata.database_size_bytes))
        self._values["measurement_assets"].setText(
            f"{metadata.measurement_asset_count:,} 件 / "
            f"{_format_bytes(metadata.measurement_asset_size_bytes)}"
        )
        # #769: 'whole-data' is a declared contract — show which registry
        # components the archive carries and which are deliberately out.
        auxiliary = metadata.auxiliary_components
        self._values["auxiliary"].setText(
            ", ".join(auxiliary) if auxiliary else "なし"
        )
        excluded = metadata.excluded_categories
        self._values["excluded"].setText(
            ", ".join(_excluded_component_label(name) for name in excluded)
            if excluded else "なし"
        )
        self._values["managed_size"].setText(_format_bytes(metadata.managed_size_bytes))
        self._values["file_count"].setText(f"{metadata.file_count:,} 件")
        self._values["path"].setText(str(metadata.backup_path))

        status_lines: list[str] = []
        if validated:
            status_lines.append(
                "復元前検証: マニフェスト / SHA-256 / SQLite整合性 / 外部キー / "
                f"DB スキーマ互換性（v{metadata.native_schema_version}） / "
                "測定アセットを検証済み"
            )
        # Round 14: a degraded archive must never present as clean — the
        # declared stale count is part of the archive's contract.
        if metadata.stale_authority_count > 0:
            status_lines.append(
                f"検証を通過しなかった記録を "
                f"{metadata.stale_authority_count} 件含みます"
                "（マニフェストに明記。復元後に「記録を再検証」で"
                "再導出できます）"
            )
        self.validation_status.setVisible(bool(status_lines))
        self.validation_status.setText("\n".join(status_lines))
        if status_lines:
            set_semantic_state(
                self.validation_status,
                SemanticState.WARNING
                if metadata.stale_authority_count > 0
                else SemanticState.SUCCESS,
            )


class _GenerationsRow(QWidget):
    """Saved-backup picker row that folds its secondary action into 操作 ▾.

    Same adaptive pattern as CaptureInboxPage._sync_action_layout: the
    fold trigger is the row's required width vs the width it is actually
    given (high-DPI shrinks logical width — exactly the crowded case).
    Hidden secondary widgets stop counting toward minimumSizeHint, so a
    seeded fold keeps the page inside narrow windows; the overflow menu
    runs the exact same slots the folded buttons do.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._collapsed_actions: bool | None = None
        self._secondary_widgets: list[QWidget] = []
        self.actions_menu = QMenu(self)
        self.actions_overflow = QToolButton(self)
        self.actions_overflow.setText("操作 ▾")
        self.actions_overflow.setToolTip(
            "幅が狭いときのバックアップ世代操作の一覧です"
        )
        self.actions_overflow.setAccessibleName(
            "保存済みバックアップのその他の操作"
        )
        self.actions_overflow.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.actions_overflow.setMenu(self.actions_menu)
        self.actions_overflow.setVisible(False)
        self.row_layout = QHBoxLayout(self)
        self.row_layout.setContentsMargins(0, 0, 0, 0)
        self.row_layout.setSpacing(10)

    def add_secondary_action(
        self,
        widget: QWidget,
        label: str,
        slot,
    ) -> QAction:
        """Register a foldable widget plus its overflow-menu mirror.

        The menu action runs the exact same slot, so folded actions stay
        reachable — mis-taps and dead ends cannot happen on narrow rows.
        """
        self._secondary_widgets.append(widget)
        action = self.actions_menu.addAction(label)
        action.triggered.connect(slot)
        return action

    def finish_actions(self) -> None:
        """Append 操作 ▾ and seed the fold state.

        Hidden secondary widgets don't count toward minimumSizeHint, so
        seeding keeps the page's minimum width inside the scroll viewport
        until the first real resizeEvent recomputes and unfolds when it
        fits.
        """
        self.row_layout.addWidget(self.actions_overflow)
        self._sync_action_layout()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync_action_layout()

    def _actions_required_width(self) -> int:
        """Width the row needs to show every control unclipped.

        Computed from child size hints so it stays correct whether the
        row is currently folded or not (hidden widgets keep their hints);
        the stretch-factor generations combo contributes only its
        minimum — folding guards the fixed controls, not its slack.
        """
        total = 0
        visible = 0
        for index in range(self.row_layout.count()):
            item = self.row_layout.itemAt(index)
            widget = item.widget()
            if widget is self.actions_overflow:
                continue  # replaces the secondary set, never coexists
            if widget is not None and self.row_layout.stretch(index) > 0:
                total += widget.minimumSizeHint().width()
                visible += 1
            elif widget is not None:
                total += widget.sizeHint().width()
                visible += 1
            else:
                total += item.sizeHint().width()
        if visible > 1:
            total += self.row_layout.spacing() * (visible - 1)
        return total

    def _available_width(self) -> int:
        """Width the scroll viewport can actually grant this row.

        The row's own width() is granted from its minimumSizeHint, so a
        too-wide row never sees the shortage — the real constraint is the
        enclosing QScrollArea's viewport minus the fixed chrome stacked
        between the viewport and this row (content/card margins).
        """
        margins = self.row_layout.contentsMargins()
        chrome = margins.left() + margins.right()
        scroll_area: QScrollArea | None = None
        node = self.parentWidget()
        while node is not None:
            if isinstance(node, QScrollArea):
                scroll_area = node
                break
            node = node.parentWidget()
        if scroll_area is None:
            return self.width() - chrome
        viewport_widget = scroll_area.viewport()
        node = self.parentWidget()
        while node is not None and node is not viewport_widget:
            layout = node.layout()
            if layout is not None:
                edge = layout.contentsMargins()
                chrome += edge.left() + edge.right()
            node = node.parentWidget()
        return scroll_area.viewport().width() - chrome

    def _sync_action_layout(self) -> None:
        """Fold secondary actions into 操作 ▾ when the row cannot fit.

        Trigger = required row width vs the width the row is given — a
        fixed pixel threshold can sit below the row's own minimum and
        never fire. The primary restore action never collapses, the
        same rule CaptureInboxPage applies to its primary triage ops.
        """
        available = self._available_width()
        required = self._actions_required_width()
        if self._collapsed_actions is True:
            # Small hysteresis so a borderline resize doesn't flap open.
            collapse = required > available - 24
        else:
            collapse = required > available
        if self._collapsed_actions == collapse:
            return
        self._collapsed_actions = collapse
        for widget in self._secondary_widgets:
            widget.setVisible(not collapse)
        self.actions_overflow.setVisible(collapse)


class DataManagementWidget(QWidget):
    """Mountable Settings > Data Management surface.

    The widget owns presentation and file/confirmation dialogs only. Backup,
    validation, restore, rollback, and application-data lifecycle semantics stay
    in DataManagementController and native_backup.
    """

    def __init__(
        self,
        controller: DataManagementController,
        *,
        dialogs: DataManagementDialogProvider | None = None,
        confirm_restore: RestoreConfirmation | None = None,
        confirm_relocate: RelocationConfirmation | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("dataManagementWidget")
        set_surface_role(self, SurfaceRole.BASE)

        self.controller = controller
        self.dialogs = dialogs or QtDataManagementDialogProvider()
        self.confirm_restore = confirm_restore or _default_restore_confirmation
        self.confirm_relocate = confirm_relocate or _default_relocate_confirmation
        self._restore_preview: RestorePreview | None = None
        self._busy = controller.is_busy
        self._restart_required = controller.lifecycle.restart_required

        page_layout = QVBoxLayout(self)
        page_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea(self)
        scroll.setObjectName("dataManagementScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        page_layout.addWidget(scroll)

        content = QWidget(scroll)
        set_surface_role(content, SurfaceRole.BASE)
        scroll.setWidget(content)

        layout = QVBoxLayout(content)
        layout.setContentsMargins(32, 28, 32, 32)
        layout.setSpacing(16)

        title = QLabel("データ管理", content)
        set_typography_role(title, TypographyRole.WORKSPACE_TITLE)
        layout.addWidget(title)

        intro = QLabel(
            "バックアップ、復元、別PCへの移行をここで行います。"
            "HTDTが管理する部屋・測定・予測・最適化データが対象です。",
            content,
        )
        intro.setWordWrap(True)
        set_typography_role(intro, TypographyRole.BODY)
        layout.addWidget(intro)

        current_schema_version = controller.backend.current_native_schema_version()
        current_schema = (
            f"v{current_schema_version}"
            if current_schema_version > 0
            else "バージョン情報なし（従来形式または未作成）"
        )
        data_dir = QLabel(
            f"現在のデータ保存場所: {controller.backend.data_dir}"
            f" / DBスキーマ: {current_schema}",
            content,
        )
        data_dir.setWordWrap(True)
        set_typography_role(data_dir, TypographyRole.SECONDARY)
        layout.addWidget(data_dir)

        self.restart_card = self._build_restart_card(content)
        layout.addWidget(self.restart_card)

        self.status_card, self.status_title, self.status_detail = self._build_status_card(content)
        layout.addWidget(self.status_card)

        self.progress_card, self.progress_label, self.progress_bar, self.cancel_button = self._build_progress_card(content)
        layout.addWidget(self.progress_card)

        migration_card = QFrame(content)
        migration_card.setObjectName("dataManagementMigrationCard")
        set_surface_role(migration_card, SurfaceRole.RAISED)
        migration_layout = QVBoxLayout(migration_card)
        migration_layout.setContentsMargins(18, 16, 18, 16)
        migration_layout.setSpacing(12)

        migration_title = QLabel("別のPCへ移行", migration_card)
        set_typography_role(migration_title, TypographyRole.SECTION_TITLE)
        migration_layout.addWidget(migration_title)

        migration_intro = QLabel(
            "1ファイルを作成して旧PCから新PCへ渡し、新PCで検証して復元します。",
            migration_card,
        )
        migration_intro.setWordWrap(True)
        set_typography_role(migration_intro, TypographyRole.BODY)
        migration_layout.addWidget(migration_intro)

        migration_actions = QHBoxLayout()
        migration_actions.setSpacing(10)
        self.migration_export_button = QPushButton("移行ファイルを作成", migration_card)
        self.migration_export_button.setObjectName("dataManagementMigrationExportButton")
        self.migration_export_button.setToolTip('現在のデータを別PCへ移すための移行ファイルを作成します')
        set_control_size(self.migration_export_button, ControlSize.PROMINENT)
        set_primary_action(self.migration_export_button)
        self.migration_export_button.clicked.connect(self._choose_backup_destination)
        migration_actions.addWidget(self.migration_export_button)

        self.migration_import_button = QPushButton(
            "旧PCの移行ファイルを読み込む",
            migration_card,
        )
        self.migration_import_button.setObjectName("dataManagementMigrationImportButton")
        self.migration_import_button.setToolTip('旧PCで作成した移行ファイルを選んでこのPCへ復元します')
        set_control_size(self.migration_import_button, ControlSize.PROMINENT)
        self.migration_import_button.clicked.connect(self._choose_restore_file)
        migration_actions.addWidget(self.migration_import_button)
        migration_layout.addLayout(migration_actions)
        layout.addWidget(migration_card)

        operations_card = QFrame(content)
        operations_card.setObjectName("dataManagementOperationsCard")
        set_surface_role(operations_card, SurfaceRole.RAISED)
        operations_layout = QVBoxLayout(operations_card)
        operations_layout.setContentsMargins(18, 16, 18, 16)
        operations_layout.setSpacing(12)

        operations_title = QLabel("バックアップと復元", operations_card)
        set_typography_role(operations_title, TypographyRole.SECTION_TITLE)
        operations_layout.addWidget(operations_title)

        operations_text = QLabel(
            "バックアップ作成後は同じネイティブ権威で検証されます。"
            "復元はファイル選択直後には実行されず、先に検証結果と内容を表示します。",
            operations_card,
        )
        operations_text.setWordWrap(True)
        set_typography_role(operations_text, TypographyRole.BODY)
        operations_layout.addWidget(operations_text)

        relocation_card = QFrame(content)
        relocation_card.setObjectName("dataManagementRelocationCard")
        set_surface_role(relocation_card, SurfaceRole.RAISED)
        relocation_layout = QVBoxLayout(relocation_card)
        relocation_layout.setContentsMargins(18, 16, 18, 16)
        relocation_layout.setSpacing(12)

        relocation_title = QLabel("データ保存場所の移動", relocation_card)
        set_typography_role(relocation_title, TypographyRole.SECTION_TITLE)
        relocation_layout.addWidget(relocation_title)

        relocation_text = QLabel(
            "HTDTが管理するデータベースと測定アセットを別のドライブやフォルダーへ"
            "移動します。コピーと検証が完了するまで元の場所は変更されません。",
            relocation_card,
        )
        relocation_text.setWordWrap(True)
        set_typography_role(relocation_text, TypographyRole.BODY)
        relocation_layout.addWidget(relocation_text)

        self.relocate_button = QPushButton(
            "移動先フォルダーを選択", relocation_card
        )
        self.relocate_button.setObjectName("dataManagementRelocateButton")
        self.relocate_button.setToolTip('データ保存先を別のフォルダーへ移動します')
        set_control_size(self.relocate_button, ControlSize.STANDARD)
        self.relocate_button.clicked.connect(self._choose_relocation_destination)
        relocation_layout.addWidget(self.relocate_button)
        layout.addWidget(relocation_card)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.backup_button = QPushButton("バックアップを作成", operations_card)
        self.backup_button.setObjectName("dataManagementBackupButton")
        self.backup_button.setToolTip('全データのバックアップファイルを作成します')
        set_control_size(self.backup_button, ControlSize.STANDARD)
        self.backup_button.clicked.connect(self._choose_backup_destination)
        actions.addWidget(self.backup_button)

        self.select_restore_button = QPushButton("復元ファイルを選択", operations_card)
        self.select_restore_button.setObjectName("dataManagementSelectRestoreButton")
        self.select_restore_button.setToolTip('復元するバックアップファイルを選択します')
        set_control_size(self.select_restore_button, ControlSize.STANDARD)
        self.select_restore_button.clicked.connect(self._choose_restore_file)
        actions.addWidget(self.select_restore_button)

        # Round 14: the post-update revalidation lane — records stranded
        # by a re-keyed build are re-derived here on demand, not only at
        # the launch prompt.
        self.revalidate_button = QPushButton(
            "記録を再検証", operations_card
        )
        self.revalidate_button.setObjectName(
            "dataManagementRevalidateButton"
        )
        self.revalidate_button.setToolTip('アップデート後に取り残された記録を再導出・再検証します')
        set_control_size(self.revalidate_button, ControlSize.STANDARD)
        self.revalidate_button.clicked.connect(self._run_revalidation)
        actions.addWidget(self.revalidate_button)
        actions.addStretch(1)
        operations_layout.addLayout(actions)

        # Round 14: when evidence is stale only because the build
        # re-keyed it, a plain backup refuses — this opt-in writes a
        # degraded archive declaring every failing row in the manifest,
        # so the user's only copy is never un-exportable.
        self.backup_allow_stale_checkbox = QCheckBox(
            "検証を通過しない記録を含めてバックアップする"
            "（対象はマニフェストに明記されます）",
            operations_card,
        )
        self.backup_allow_stale_checkbox.setObjectName(
            "dataManagementBackupAllowStale"
        )
        operations_layout.addWidget(self.backup_allow_stale_checkbox)

        # Round9 audit: saved generations were invisible — restore required
        # remembering where a backup file lived. List every restorable
        # archive the app knows about (automatic generations + pre-upgrade
        # recovery copies) so the user can pick one directly.
        self.generations_row = _GenerationsRow(operations_card)
        generations_layout = self.generations_row.row_layout
        generations_label = QLabel(
            "保存済みバックアップ:", self.generations_row
        )
        generations_layout.addWidget(generations_label)
        self.generations_combo = QComboBox(self.generations_row)
        self.generations_combo.setObjectName("dataManagementGenerationsCombo")
        # Auto-backup names run ~55 chars — AdjustToContents would pin the
        # row's minimum width to the longest archive name and defeat the
        # scroll viewport. The popup list still shows full names; the
        # closed combo just clips the selected text when it must shrink.
        self.generations_combo.setMinimumContentsLength(12)
        self.generations_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        generations_layout.addWidget(self.generations_combo, 1)
        self.generation_restore_button = QPushButton(
            "このバックアップを検証して復元…", self.generations_row
        )
        self.generation_restore_button.setObjectName(
            "dataManagementGenerationRestoreButton"
        )
        self.generation_restore_button.setToolTip('選択した世代のバックアップを検証して復元します')
        set_control_size(
            self.generation_restore_button, ControlSize.STANDARD
        )
        self.generation_restore_button.clicked.connect(
            self._preview_selected_generation
        )
        generations_layout.addWidget(self.generation_restore_button)
        self.drill_button = QPushButton(
            "このバックアップを復元テスト…", self.generations_row
        )
        self.drill_button.setObjectName("dataManagementDrillButton")
        self.drill_button.setToolTip(
            '選択したバックアップを隔離フォルダーへ実際に復元して検証します'
            '（本番データは変更されません）'
        )
        set_control_size(self.drill_button, ControlSize.STANDARD)
        self.drill_button.clicked.connect(self._run_restore_drill)
        generations_layout.addWidget(self.drill_button)
        # #992: the drill action is secondary — it folds into 操作 ▾ when
        # the row cannot fit, while the primary restore action never
        # collapses. The menu action mirrors the same slot.
        self._drill_menu_action = self.generations_row.add_secondary_action(
            self.drill_button,
            "このバックアップを復元テスト…",
            self._run_restore_drill,
        )
        self.generations_row.finish_actions()
        operations_layout.addWidget(self.generations_row)

        self.last_drill_label = QLabel(operations_card)
        self.last_drill_label.setObjectName("dataManagementLastDrillLabel")
        self.last_drill_label.setWordWrap(True)
        self.last_drill_label.setAccessibleName("前回の復元テスト結果")
        set_typography_role(self.last_drill_label, TypographyRole.SECONDARY)
        self.last_drill_label.hide()
        operations_layout.addWidget(self.last_drill_label)

        self.drill_result_card = QFrame(operations_card)
        self.drill_result_card.setObjectName("dataManagementDrillResultCard")
        set_surface_role(self.drill_result_card, SurfaceRole.RAISED)
        drill_layout = QVBoxLayout(self.drill_result_card)
        drill_layout.setContentsMargins(14, 12, 14, 12)
        drill_layout.setSpacing(8)
        self.drill_verdict_label = QLabel(self.drill_result_card)
        self.drill_verdict_label.setObjectName(
            "dataManagementDrillVerdict"
        )
        self.drill_verdict_label.setWordWrap(True)
        set_typography_role(
            self.drill_verdict_label, TypographyRole.SECTION_TITLE
        )
        drill_layout.addWidget(self.drill_verdict_label)
        self.drill_checks_label = QLabel(self.drill_result_card)
        self.drill_checks_label.setObjectName(
            "dataManagementDrillChecks"
        )
        self.drill_checks_label.setWordWrap(True)
        self.drill_checks_label.setAccessibleName(
            "復元テストの検証項目"
        )
        set_typography_role(self.drill_checks_label, TypographyRole.BODY)
        drill_layout.addWidget(self.drill_checks_label)
        self.drill_claims_label = QLabel(self.drill_result_card)
        self.drill_claims_label.setObjectName(
            "dataManagementDrillClaims"
        )
        self.drill_claims_label.setWordWrap(True)
        set_typography_role(
            self.drill_claims_label, TypographyRole.SECONDARY
        )
        drill_layout.addWidget(self.drill_claims_label)
        self.drill_result_card.hide()
        operations_layout.addWidget(self.drill_result_card)
        layout.addWidget(operations_card)

        # Round9-prefs deferred item: the automatic-backup policy already
        # exists (AutomaticBackupPolicy, persisted next to the data root) but
        # had no UI — expose it here, commit-on-change. The runner builds a
        # fresh scheduler per launch tick, so edits apply from the next
        # launch check; the label says so rather than pretending live-apply.
        policy_card = QFrame(content)
        policy_card.setObjectName("dataManagementBackupPolicyCard")
        set_surface_role(policy_card, SurfaceRole.RAISED)
        policy_layout = QVBoxLayout(policy_card)
        policy_layout.setContentsMargins(18, 16, 18, 16)
        policy_layout.setSpacing(12)

        policy_title = QLabel("自動バックアップ", policy_card)
        set_typography_role(policy_title, TypographyRole.SECTION_TITLE)
        policy_layout.addWidget(policy_title)

        policy_intro = QLabel(
            "起動時に前回のバックアップからの経過時間を確認し、期限が"
            "過ぎていれば自動で世代を作成します。変更は次回の起動時"
            "チェックから適用されます。",
            policy_card,
        )
        policy_intro.setWordWrap(True)
        set_typography_role(policy_intro, TypographyRole.BODY)
        policy_layout.addWidget(policy_intro)

        self._backup_scheduler = AutomaticBackupScheduler(
            self.controller.backend.data_dir
        )
        self._backup_policy = self._backup_scheduler.load_policy()
        self._backup_policy_dir = self._backup_policy.backup_dir

        self.backup_policy_enabled = QCheckBox(
            "自動バックアップを有効にする", policy_card
        )
        self.backup_policy_enabled.setObjectName(
            "dataManagementBackupPolicyEnabled"
        )
        self.backup_policy_enabled.setChecked(self._backup_policy.enabled)
        self.backup_policy_enabled.toggled.connect(
            self._save_backup_policy
        )
        policy_layout.addWidget(self.backup_policy_enabled)

        policy_form = QFormLayout()
        policy_form.setSpacing(8)
        self.backup_interval_spin = QDoubleSpinBox(policy_card)
        self.backup_interval_spin.setObjectName(
            "dataManagementBackupIntervalSpin"
        )
        self.backup_interval_spin.setRange(0.5, 2160.0)
        self.backup_interval_spin.setDecimals(1)
        self.backup_interval_spin.setSingleStep(1.0)
        self.backup_interval_spin.setSuffix(" 時間ごと")
        self.backup_interval_spin.setValue(self._backup_policy.interval_hours)
        self.backup_interval_spin.valueChanged.connect(
            self._save_backup_policy
        )
        policy_form.addRow("作成間隔", self.backup_interval_spin)

        self.backup_keep_spin = QSpinBox(policy_card)
        self.backup_keep_spin.setObjectName("dataManagementBackupKeepSpin")
        self.backup_keep_spin.setRange(1, 100)
        self.backup_keep_spin.setSuffix(" 世代")
        self.backup_keep_spin.setValue(self._backup_policy.keep_generations)
        self.backup_keep_spin.valueChanged.connect(self._save_backup_policy)
        policy_form.addRow("最新世代の保持数", self.backup_keep_spin)

        self.backup_keep_daily_spin = QSpinBox(policy_card)
        self.backup_keep_daily_spin.setObjectName(
            "dataManagementBackupKeepDailySpin"
        )
        self.backup_keep_daily_spin.setRange(0, 366)
        self.backup_keep_daily_spin.setSuffix(" 日分")
        self.backup_keep_daily_spin.setValue(
            self._backup_policy.keep_daily_generations
        )
        self.backup_keep_daily_spin.valueChanged.connect(
            self._save_backup_policy
        )
        policy_form.addRow("日次バックアップの保持", self.backup_keep_daily_spin)
        policy_layout.addLayout(policy_form)

        dir_row = QWidget(policy_card)
        dir_layout = QHBoxLayout(dir_row)
        dir_layout.setContentsMargins(0, 0, 0, 0)
        dir_layout.setSpacing(10)
        self.backup_dir_label = QLabel(dir_row)
        self.backup_dir_label.setObjectName("dataManagementBackupDirLabel")
        self.backup_dir_label.setWordWrap(True)
        dir_layout.addWidget(self.backup_dir_label, 1)
        self.backup_dir_button = QPushButton("変更…", dir_row)
        self.backup_dir_button.setObjectName(
            "dataManagementBackupDirButton"
        )
        self.backup_dir_button.setToolTip('自動バックアップの保存先フォルダーを変更します')
        set_control_size(self.backup_dir_button, ControlSize.STANDARD)
        self.backup_dir_button.clicked.connect(
            self._choose_backup_policy_dir
        )
        dir_layout.addWidget(self.backup_dir_button)
        self.backup_dir_reset_button = QPushButton("既定に戻す", dir_row)
        self.backup_dir_reset_button.setObjectName(
            "dataManagementBackupDirResetButton"
        )
        self.backup_dir_reset_button.setToolTip('自動バックアップの保存先を既定へ戻します')
        set_control_size(self.backup_dir_reset_button, ControlSize.STANDARD)
        self.backup_dir_reset_button.clicked.connect(
            self._reset_backup_policy_dir
        )
        dir_layout.addWidget(self.backup_dir_reset_button)
        policy_layout.addWidget(dir_row)
        self._refresh_backup_dir_label()
        layout.addWidget(policy_card)

        storage_card = QFrame(content)
        storage_card.setObjectName("dataManagementStorageCard")
        set_surface_role(storage_card, SurfaceRole.RAISED)
        storage_layout = QVBoxLayout(storage_card)
        storage_layout.setContentsMargins(18, 16, 18, 16)
        storage_layout.setSpacing(12)

        storage_title = QLabel("ストレージと証拠管理", storage_card)
        set_typography_role(storage_title, TypographyRole.SECTION_TITLE)
        storage_layout.addWidget(storage_title)

        storage_text = QLabel(
            "管理対象ストレージの内訳を確認し、どの権威からも参照されない"
            "孤立アセットを削除候補として表示します。"
            "削除は確認後にのみ実行されます。",
            storage_card,
        )
        storage_text.setWordWrap(True)
        set_typography_role(storage_text, TypographyRole.BODY)
        storage_layout.addWidget(storage_text)

        storage_actions = QHBoxLayout()
        storage_actions.setSpacing(10)
        self.storage_button = QPushButton(
            "ストレージを確認", storage_card
        )
        self.storage_button.setObjectName("dataManagementStorageButton")
        self.storage_button.setToolTip('データ保存先の内容と容量を確認します')
        set_control_size(self.storage_button, ControlSize.STANDARD)
        self.storage_button.clicked.connect(self._show_storage_inventory)
        storage_actions.addWidget(self.storage_button)
        storage_actions.addStretch(1)
        storage_layout.addLayout(storage_actions)

        self.legacy_label = QLabel("", storage_card)
        self.legacy_label.setWordWrap(True)
        set_typography_role(self.legacy_label, TypographyRole.BODY)
        set_semantic_state(self.legacy_label, SemanticState.WARNING)
        storage_layout.addWidget(self.legacy_label)
        self._refresh_legacy_warning()
        layout.addWidget(storage_card)

        self.preview_metadata = BackupMetadataView(title="復元前の確認", parent=content)
        self.preview_metadata.setObjectName("dataManagementRestorePreview")
        self.preview_metadata.hide()
        layout.addWidget(self.preview_metadata)

        self.restore_button = QPushButton("このバックアップから復元", content)
        self.restore_button.setObjectName("dataManagementRestoreButton")
        self.restore_button.setToolTip('選択したバックアップの内容で現在のデータを置き換えます')
        set_control_size(self.restore_button, ControlSize.PROMINENT)
        set_primary_action(self.restore_button)
        self.restore_button.clicked.connect(self._confirm_and_restore)
        layout.addWidget(self.restore_button)

        self.result_metadata = BackupMetadataView(title="処理結果", parent=content)
        self.result_metadata.setObjectName("dataManagementResultMetadata")
        self.result_metadata.hide()
        layout.addWidget(self.result_metadata)

        self.pre_restore_label = QLabel(content)
        self.pre_restore_label.setObjectName("dataManagementPreRestoreBackup")
        self.pre_restore_label.setWordWrap(True)
        set_typography_role(self.pre_restore_label, TypographyRole.SECONDARY)
        self.pre_restore_label.hide()
        layout.addWidget(self.pre_restore_label)

        scope_note = QLabel(
            "移行対象はHTDTが管理するデータです。REW本体の設定、Windowsの音声設定、"
            "AVR本体設定、HTDTへ取り込んでいない外部ファイルは含まれません。",
            content,
        )
        scope_note.setWordWrap(True)
        set_typography_role(scope_note, TypographyRole.SECONDARY)
        layout.addWidget(scope_note)
        layout.addStretch(1)

        controller.busy_changed.connect(self._on_busy_changed)
        controller.progress_changed.connect(self._on_progress_changed)
        controller.backup_created.connect(self._on_backup_created)
        controller.restore_preview_ready.connect(self._on_restore_preview_ready)
        controller.restore_completed.connect(self._on_restore_completed)
        controller.restore_drill_completed.connect(
            self._on_drill_completed
        )
        controller.relocation_completed.connect(self._on_relocation_completed)
        controller.storage_scan_completed.connect(self._on_storage_scan_completed)
        controller.storage_gc_completed.connect(self._on_storage_gc_completed)
        controller.operation_failed.connect(self._on_operation_failed)
        controller.operation_cancelled.connect(self._on_operation_cancelled)

        self._refresh_generations()
        self._refresh_last_drill()
        if self._restart_required:
            self._show_restart_required(
                "データを安全に読み直せませんでした。HTDTを再起動してください。"
            )
        else:
            # A widget mounted while an op is already running never saw the
            # busy_changed(True) signal — reflect the busy state it read in
            # __init__ or it would show enabled actions with no progress
            # card while the operation runs underneath it (#REV18).
            self._on_busy_changed(self._busy)

    @property
    def restart_required(self) -> bool:
        return self._restart_required

    @property
    def can_close_application(self) -> bool:
        return self.controller.can_close_application

    def before_deactivate(self) -> tuple[bool, str | None]:
        if self._busy:
            return False, "データ処理が完了してから画面を切り替えてください"
        if self._restart_required:
            return False, "データを再読み込みできないため、HTDTを再起動してください"
        return True, None

    def _build_restart_card(self, parent: QWidget) -> QFrame:
        card = QFrame(parent)
        card.setObjectName("dataManagementRestartCard")
        set_surface_role(card, SurfaceRole.RAISED)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)
        heading = QLabel("再起動が必要です", card)
        set_typography_role(heading, TypographyRole.SECTION_TITLE)
        set_semantic_state(heading, SemanticState.ERROR)
        layout.addWidget(heading)
        self.restart_detail = QLabel(card)
        self.restart_detail.setWordWrap(True)
        set_typography_role(self.restart_detail, TypographyRole.BODY)
        layout.addWidget(self.restart_detail)
        card.hide()
        return card

    def _build_status_card(self, parent: QWidget) -> tuple[QFrame, QLabel, QLabel]:
        card = QFrame(parent)
        card.setObjectName("dataManagementStatusCard")
        set_surface_role(card, SurfaceRole.RAISED)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(4)
        title = QLabel(card)
        set_typography_role(title, TypographyRole.SECTION_TITLE)
        layout.addWidget(title)
        detail = QLabel(card)
        detail.setWordWrap(True)
        set_typography_role(detail, TypographyRole.SECONDARY)
        layout.addWidget(detail)
        card.hide()
        return card, title, detail

    def _build_progress_card(self, parent: QWidget) -> tuple[QFrame, QLabel, QProgressBar, QPushButton]:
        card = QFrame(parent)
        card.setObjectName("dataManagementProgressCard")
        set_surface_role(card, SurfaceRole.RAISED)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)
        label = QLabel("処理を開始しています", card)
        set_typography_role(label, TypographyRole.BODY)
        layout.addWidget(label)
        progress_row = QHBoxLayout()
        progress = QProgressBar(card)
        progress.setObjectName("dataManagementProgress")
        progress.setRange(0, 0)
        progress.setTextVisible(False)
        progress_row.addWidget(progress, 1)
        cancel_button = QPushButton("中止", card)
        cancel_button.setObjectName("dataManagementCancelButton")
        cancel_button.setToolTip('実行中の処理を中止します')
        cancel_button.clicked.connect(self._request_cancel)
        progress_row.addWidget(cancel_button)
        layout.addLayout(progress_row)
        card.hide()
        return card, label, progress, cancel_button

    def _choose_backup_destination(self) -> None:
        if self._busy or self._restart_required:
            return
        destination = self.dialogs.choose_backup_destination(
            self,
            suggested_name=_default_backup_name(),
        )
        if destination is None:
            return
        self._hide_status()
        self.result_metadata.hide()
        self.pre_restore_label.hide()
        self.controller.create_backup(
            destination,
            allow_stale=self.backup_allow_stale_checkbox.isChecked(),
        )

    def _run_revalidation(self) -> None:
        """Round 14: re-derive stale authority under the current build."""
        if self._busy or self._restart_required:
            return
        try:
            report = self.controller.revalidate()
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: op surface — expected failures show a verbatim error state; unexpected errors propagate to diagnostics
            self._show_status(
                "再検証を完了できませんでした",
                operation_error_message(exc),
                SemanticState.ERROR,
            )
            return
        QMessageBox.information(self, "HTDT 再検証", report.summary_ja())
        self._refresh_generations()

    def _choose_restore_file(self) -> None:
        if self._busy or self._restart_required:
            return
        backup_path = self.dialogs.choose_restore_file(self)
        if backup_path is None:
            return
        self._clear_restore_preview()
        self._hide_status()
        self.result_metadata.hide()
        self.pre_restore_label.hide()
        self.controller.preview_restore(backup_path)

    def _refresh_generations(self) -> None:
        """Re-list restorable archives — cheap filename listing only."""

        try:
            generations = list_restorable_backups(
                self.controller.backend.data_dir
            )
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: listing read — expected failures report and degrade to no rows; sealed-store failures propagate
            if is_authority_failure(exc):
                raise
            report_boundary_failure(exc, operation='バックアップ一覧の読み取り')
            generations = ()
        self.generations_combo.clear()
        for path in generations:
            self.generations_combo.addItem(path.name, str(path))
        self.generations_row.setVisible(bool(generations))
        # Longest-name content changed — the fold trigger depends on the
        # row's required width, so resync while the viewport keeps its size.
        self.generations_row._sync_action_layout()
        self._refresh_actions()

    def _preview_selected_generation(self) -> None:
        if self._busy or self._restart_required:
            return
        selected = self.generations_combo.currentData()
        if not selected:
            return
        self._clear_restore_preview()
        self._hide_status()
        self.result_metadata.hide()
        self.pre_restore_label.hide()
        self.controller.preview_restore(Path(selected))

    def _run_restore_drill(self) -> None:
        """#992: start an isolated restore rehearsal for the selected
        backup — the drill only ever writes inside the chosen sandbox."""
        if self._busy or self._restart_required:
            return
        selected = self.generations_combo.currentData()
        if not selected:
            return
        sandbox = self.dialogs.choose_drill_sandbox(self)
        if sandbox is None:
            return
        self._hide_status()
        self.drill_result_card.hide()
        self.controller.restore_drill(Path(selected), Path(sandbox))

    def _on_drill_completed(self, result: RestoreDrillResult) -> None:
        verdict_titles = {
            'restorable': 'このバックアップは復元可能です',
            'restorable_with_conditions': '条件付きで復元可能です',
            'failed': 'このバックアップは復元できません',
            'not_verifiable': '復元可否を検証できませんでした',
        }
        verdict_states = {
            'restorable': SemanticState.SUCCESS,
            'restorable_with_conditions': SemanticState.WARNING,
            'failed': SemanticState.ERROR,
            'not_verifiable': SemanticState.WARNING,
        }
        self.drill_verdict_label.setText(
            f"{verdict_titles.get(result.verdict, result.verdict)} — "
            f"{result.backup_name}"
        )
        set_semantic_state(
            self.drill_verdict_label,
            verdict_states.get(result.verdict, SemanticState.WARNING),
        )
        status_marks = {
            'passed': '✓',
            'failed': '✗',
            'conditional': '△',
            'unknown': '?',
            'skipped': '—',
        }
        lines = [
            f"{status_marks.get(check.status, '?')} {check.detail_ja}"
            for check in result.checks
        ]
        self.drill_checks_label.setText('\n'.join(lines))
        claims_ja = {
            'archive_verified': 'アーカイブ検証済み',
            'isolated_restore_succeeded': '隔離復元の成功',
            'same_machine_opened': '同一環境でのオープン成功',
        }
        non_claims_ja = {
            'other_pc_migration': '別PCへの移行可否',
            'physical_disaster_recovery': '物理障害からの復旧',
            'operator_acceptance': '運用者による受け入れ',
        }
        proven = '、'.join(
            claims_ja.get(c, c) for c in result.claims
        ) or 'なし'
        unproven = '、'.join(
            non_claims_ja.get(c, c) for c in result.non_claims
        )
        self.drill_claims_label.setText(
            f'証明済み: {proven}\n'
            f'このテストでは証明していません: {unproven}'
        )
        self.drill_result_card.show()
        self._show_status(
            verdict_titles.get(result.verdict, result.verdict),
            f'演習ログ: {Path(result.sandbox_dir).parent} / '
            f'結果はデータ管理の履歴にも記録されました',
            verdict_states.get(result.verdict, SemanticState.WARNING),
        )
        self._refresh_last_drill()

    def _refresh_last_drill(self) -> None:
        try:
            latest = latest_drill_result(self.controller.backend.data_dir)
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: journal read — expected failures report and degrade to 'no drill' honestly; sealed-store failures propagate
            if is_authority_failure(exc):
                raise
            report_boundary_failure(exc, operation='ドリル結果の読み取り')
            latest = None
        if latest is None:
            self.last_drill_label.hide()
            return
        verdict_ja = {
            'restorable': '復元可能',
            'restorable_with_conditions': '条件付きで復元可能',
            'failed': '復元不可',
            'not_verifiable': '検証不可',
        }.get(latest.verdict, latest.verdict)
        self.last_drill_label.setText(
            f'前回の復元テスト: {verdict_ja} — '
            f'{latest.backup_name}（{latest.finished_at_utc}）'
        )
        self.last_drill_label.show()

    def showEvent(self, event) -> None:  # noqa: N802
        # Generations created while the dialog was hidden appear on show.
        self._refresh_generations()
        super().showEvent(event)

    def _confirm_and_restore(self) -> None:
        preview = self._restore_preview
        if (
            preview is None
            or self._busy
            or self._restart_required
            or not self.confirm_restore(self, preview)
        ):
            return
        self._hide_status()
        self.controller.restore(preview)

    def _choose_relocation_destination(self) -> None:
        if self._busy or self._restart_required:
            return
        destination = self.dialogs.choose_relocation_destination(self)
        if destination is None:
            return
        plan = self.controller.backend.plan_relocation(destination)
        if not plan.executable:
            blockers = "\n".join(
                f"・{blocker.detail}" for blocker in plan.blockers
            )
            self._show_status(
                "この移動先は利用できません",
                blockers,
                SemanticState.ERROR,
            )
            return
        if not self.confirm_relocate(self, plan):
            return
        self._hide_status()
        self.result_metadata.hide()
        self.pre_restore_label.hide()
        self.controller.relocate(destination)

    def _on_relocation_completed(self, result: RelocationResult) -> None:
        self._show_status(
            "データ保存場所を移動しました",
            f"移動先: {result.destination_dir} / "
            f"退避した旧データ: {result.parked_dir}",
            SemanticState.SUCCESS,
        )
        self._show_restart_required(
            "データの移動が完了しました。HTDTを終了して再起動すると、"
            "新しい場所で開きます。"
        )

    def _on_busy_changed(self, busy: bool) -> None:
        self._busy = busy
        self.progress_card.setVisible(busy)
        if busy:
            self.progress_label.setText("処理を開始しています")
            self.progress_bar.setRange(0, 0)
            self.progress_bar.setTextVisible(False)
            self.cancel_button.setEnabled(True)
            self.cancel_button.setText("中止")
        self._refresh_actions()

    def _request_cancel(self) -> None:
        """REV19/D2: cooperative cancel against the running operation.

        A refused request (committed restore/relocate, or nothing to
        cancel) is surfaced honestly instead of disabling the button
        up-front — the activity snapshot decides.
        """
        if self.controller.request_cancel():
            self.cancel_button.setEnabled(False)
            self.cancel_button.setText("中止を要求しています…")
            self.progress_label.setText("処理を中止しています…")
        else:
            self.progress_label.setText(
                "この処理は安全に中止できる段階を過ぎています"
            )

    def _on_operation_cancelled(
        self, cancellation: DataOperationCancellation
    ) -> None:
        self._show_status(
            "処理を中止しました",
            "途中までの結果は適用されていません",
            SemanticState.WARNING,
        )
        if cancellation.restart_required:
            self._show_restart_required(
                "現在のデータ状態を安全に再読み込みできませんでした。"
                " HTDTを終了して再起動してください。"
            )

    def _on_progress_changed(self, progress: DataOperationProgress) -> None:
        self.progress_card.show()
        self.progress_label.setText(progress.message_ja)
        if progress.fraction is None:
            self.progress_bar.setRange(0, 0)
            self.progress_bar.setTextVisible(False)
        else:
            value = max(0, min(100, round(progress.fraction * 100)))
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(value)
            self.progress_bar.setTextVisible(True)

    def _refresh_legacy_warning(self) -> None:
        """#598: surface the retired store so users can archive it.

        The legacy database is outside the canonical native backup, so its
        presence is a durability warning, not just an inventory fact.
        """

        try:
            report = inspect_legacy_store(self.controller.backend.data_dir)
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: legacy probe — expected failures report and hide the warning honestly; sealed-store failures propagate
            if is_authority_failure(exc):
                raise
            report_boundary_failure(exc, operation='レガシーストアの検査')
            self.legacy_label.hide()
            return
        if report.state == 'populated':
            self.legacy_label.setText(
                "レガシーブラウザデータ (htdt.sqlite3) を検出しました。"
                "このデータはバックアップ対象外のため、"
                "htdt-native --migrate-legacy-data で移行してください。"
            )
            self.legacy_label.show()
        elif report.state == 'unreadable':
            self.legacy_label.setText(
                "レガシーデータベース (htdt.sqlite3) を読み取れませんでした。"
                "ファイルが破損している可能性があります — "
                "バックアップからの復元またはサポートへの共有を検討してください。"
            )
            self.legacy_label.show()
        else:
            self.legacy_label.hide()

    def _show_storage_inventory(self) -> None:
        """#501/#760: kick the async inventory — see ``_on_storage_scan_completed``."""

        self.controller.scan_storage()

    def _on_storage_scan_completed(self, report: StorageReport) -> None:
        """Inventory -> candidates -> explicit confirm -> async GC."""

        lines: list[str] = []
        for category in report.categories:
            lines.append(
                f"{_STORAGE_CATEGORY_LABELS.get(category.category, category.category)}: "
                f"{category.file_count} 件 / "
                f"物理 {_format_bytes(category.physical_unique_bytes)}"
                f"（論理参照 {_format_bytes(category.logical_referenced_bytes)}"
                f"・未参照 {_format_bytes(category.unreferenced_bytes)}）"
            )
        if report.missing_referenced:
            lines.append("")
            lines.append(
                "整合性エラー: 参照されているアセットが見つかりません:"
            )
            for missing in report.missing_referenced[:8]:
                lines.append(f"・{missing.digest} ({missing.relative_path})")
        if report.orphan_candidates:
            lines.append("")
            lines.append(
                f"削除候補: {len(report.orphan_candidates)} 件 / "
                f"{_format_bytes(report.reclaimable_bytes)} を回収可能"
            )
        else:
            lines.append("")
            lines.append("削除候補はありません。")

        if not report.orphan_candidates:
            QMessageBox.information(
                self, "ストレージの確認結果", "\n".join(lines)
            )
            return

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("孤立アセットの削除")
        box.setText("\n".join(lines))
        box.setInformativeText(
            "削除対象はどの権威からも参照されていないアセットのみです。"
            "実行前に各候補の参照可否を再検証します。"
        )
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        box.button(QMessageBox.StandardButton.Yes).setText("削除を実行")
        box.setDefaultButton(QMessageBox.StandardButton.No)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        self.controller.gc_storage()

    def _on_storage_gc_completed(self, result: StorageGcResult) -> None:
        QMessageBox.information(
            self,
            "ストレージを整理しました",
            f"{result.deleted_files} 件の孤立アセットを削除し、"
            f"{_format_bytes(result.freed_bytes)} を回収しました。"
            + (
                f"\nスキップ: {len(result.skipped_digests)} 件"
                if result.skipped_digests
                else ""
            ),
        )
        self._refresh_legacy_warning()

    def _on_backup_created(self, result: BackupCreateResult) -> None:
        self.result_metadata.title.setText("作成したバックアップ")
        self.result_metadata.set_metadata(result.metadata, validated=True)
        self.result_metadata.show()
        self.pre_restore_label.hide()
        if result.metadata.stale_authority_count > 0:
            # Degraded archive (round 14): backup succeeded, but say
            # plainly that it carries unverified records and where the
            # revalidation lane lives — never a silent success.
            self._show_status(
                "バックアップを作成しました（未検証の記録を含む）",
                f"{result.metadata.backup_path}\n"
                f"検証を通過しなかった記録: "
                f"{result.metadata.stale_authority_count} 件"
                "（マニフェストに明記されています）。"
                "「記録を再検証」で再導出できます。",
                SemanticState.WARNING,
            )
            return
        self._show_status(
            "バックアップを作成しました",
            str(result.metadata.backup_path),
            SemanticState.SUCCESS,
        )

    def _on_restore_preview_ready(self, preview: RestorePreview) -> None:
        self._restore_preview = preview
        self.preview_metadata.set_metadata(preview.metadata, validated=True)
        self.preview_metadata.show()
        self._show_status(
            "バックアップを復元できます",
            "内容を確認してから復元を開始してください。",
            SemanticState.SUCCESS,
        )
        self._refresh_actions()

    def _on_restore_completed(self, result: RestoreResult) -> None:
        self._clear_restore_preview()
        self.result_metadata.title.setText("復元したバックアップ")
        self.result_metadata.set_metadata(result.metadata, validated=True)
        self.result_metadata.show()
        if result.pre_restore_backup is None:
            self.pre_restore_label.hide()
        else:
            self.pre_restore_label.setText(
                f"復元前バックアップ: {result.pre_restore_backup}"
            )
            self.pre_restore_label.show()
        self._show_status(
            "復元が完了しました",
            "復元後のデータを読み直しました。",
            SemanticState.SUCCESS,
        )

    def _on_operation_failed(self, failure: DataOperationFailure) -> None:
        if failure.kind in {
            DataOperationKind.VALIDATE_RESTORE,
            DataOperationKind.RESTORE,
        }:
            self._clear_restore_preview()

        detail = failure.detail.strip()
        self._show_status(
            failure.message_ja,
            f"詳細: {detail}" if detail else "",
            SemanticState.ERROR,
        )

        if failure.restart_required:
            suffix = (
                "データの復元自体は完了しています。"
                if failure.data_restored
                else "現在のデータ状態を安全に再読み込みできませんでした。"
            )
            self._show_restart_required(
                f"{suffix} HTDTを終了して再起動してください。"
            )

    # ---- automatic backup policy (round10) ------------------------------

    def _save_backup_policy(self, *_args: object) -> None:
        policy = AutomaticBackupPolicy(
            enabled=self.backup_policy_enabled.isChecked(),
            interval_hours=float(self.backup_interval_spin.value()),
            keep_generations=int(self.backup_keep_spin.value()),
            keep_daily_generations=int(
                self.backup_keep_daily_spin.value()
            ),
            backup_dir=self._backup_policy_dir,
        )
        try:
            self._backup_scheduler.save_policy(policy)
        except (OSError, ValueError) as exc:
            self._show_status(
                "自動バックアップ設定を保存できませんでした",
                operation_error_message(exc),
                SemanticState.ERROR,
            )
            return
        self._backup_policy = policy
        self._backup_scheduler.policy = policy
        self._show_status(
            "自動バックアップ設定を更新しました",
            "次回の起動時チェックから適用されます。",
            SemanticState.SUCCESS,
        )

    def _choose_backup_policy_dir(self) -> None:
        selected = file_dialog_memory.get_existing_directory(
            self,
            "自動バックアップの保存先フォルダーを選択",
            'backup.policy_dir',
            default_dir=str(
                backups_dir(
                    self.controller.backend.data_dir, self._backup_policy
                )
            ),
        )
        if not selected:
            return
        self._backup_policy_dir = selected
        self._refresh_backup_dir_label()
        self._save_backup_policy()

    def _reset_backup_policy_dir(self) -> None:
        self._backup_policy_dir = None
        self._refresh_backup_dir_label()
        self._save_backup_policy()

    def _refresh_backup_dir_label(self) -> None:
        effective = backups_dir(
            self.controller.backend.data_dir,
            self._backup_policy.model_copy(
                update={'backup_dir': self._backup_policy_dir}
            ),
        )
        suffix = "（既定）" if self._backup_policy_dir is None else ""
        self.backup_dir_label.setText(f"保存先: {effective}{suffix}")
        self.backup_dir_reset_button.setVisible(
            self._backup_policy_dir is not None
        )

    def _show_status(
        self,
        title: str,
        detail: str,
        state: SemanticState,
    ) -> None:
        self.status_title.setText(title)
        set_semantic_state(self.status_title, state)
        self.status_detail.setText(detail)
        self.status_detail.setVisible(bool(detail))
        self.status_card.show()

    def _hide_status(self) -> None:
        self.status_card.hide()

    def _show_restart_required(self, detail: str) -> None:
        self._restart_required = True
        self.restart_detail.setText(detail)
        self.restart_card.show()
        self._refresh_actions()

    def _clear_restore_preview(self) -> None:
        self._restore_preview = None
        self.preview_metadata.hide()
        self._refresh_actions()

    def _refresh_actions(self) -> None:
        available = not self._busy and not self._restart_required
        self.backup_button.setEnabled(available)
        self.select_restore_button.setEnabled(available)
        self.revalidate_button.setEnabled(available)
        self.migration_export_button.setEnabled(available)
        self.migration_import_button.setEnabled(available)
        self.relocate_button.setEnabled(available)
        self.storage_button.setEnabled(available)
        self.generations_combo.setEnabled(available)
        self.generation_restore_button.setEnabled(available)
        self.drill_button.setEnabled(available)
        self._drill_menu_action.setEnabled(available)
        for control in (
            self.backup_policy_enabled,
            self.backup_interval_spin,
            self.backup_keep_spin,
            self.backup_keep_daily_spin,
            self.backup_dir_button,
            self.backup_dir_reset_button,
        ):
            control.setEnabled(available)
        self.restore_button.setEnabled(
            available and self._restore_preview is not None
        )


@dataclass(slots=True)
class DataManagementComponent:
    """Shell-facing mount contract without coupling to workflow_shell.py."""

    widget: DataManagementWidget

    @property
    def can_close_application(self) -> bool:
        return self.widget.can_close_application

    @property
    def restart_required(self) -> bool:
        return self.widget.restart_required

    def before_deactivate(self) -> tuple[bool, str | None]:
        return self.widget.before_deactivate()

    def close(self) -> None:
        self.widget.close()


def build_data_management_component(
    controller: DataManagementController,
    *,
    dialogs: DataManagementDialogProvider | None = None,
    confirm_restore: RestoreConfirmation | None = None,
    confirm_relocate: RelocationConfirmation | None = None,
) -> DataManagementComponent:
    """Create an unparented component that a future Settings route can mount."""

    widget = DataManagementWidget(
        controller,
        dialogs=dialogs,
        confirm_restore=confirm_restore,
        confirm_relocate=confirm_relocate,
    )
    widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    return DataManagementComponent(widget=widget)


__all__ = [
    "BackupMetadataView",
    "DataManagementComponent",
    "DataManagementDialogProvider",
    "DataManagementWidget",
    "QtDataManagementDialogProvider",
    "RelocationConfirmation",
    "RestoreConfirmation",
    "build_data_management_component",
]
