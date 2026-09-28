from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from . import file_dialog_memory
from .data_management import (
    BackupCreateResult,
    BackupMetadata,
    DataManagementController,
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
            "データの移動先フォルダを選択",
            'data.relocate',
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
        "元の場所は移動後も退避フォルダとして残ります。"
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
    box.setText("現在のHTDTデータを、このバックアップの内容に置き換えます。")
    box.setInformativeText(
        "現在のデータは復元前バックアップとして自動保存されます。"
        "\n復元を開始しますか？"
    )
    box.setStandardButtons(
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
    )
    box.setDefaultButton(QMessageBox.StandardButton.Cancel)
    return box.exec() == QMessageBox.StandardButton.Yes


def _default_backup_name(now: datetime | None = None) -> str:
    stamp = (now or datetime.now()).strftime("%Y-%m-%d-%H%M")
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
        return parsed.astimezone().strftime("%Y/%m/%d %H:%M")
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
            ("schema_version", "バックアップschema"),
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

        self.validation_status.setVisible(validated)
        if validated:
            self.validation_status.setText(
                "復元前検証: manifest / SHA-256 / SQLite整合性 / 外部キー / "
                f"DB schema互換性（v{metadata.native_schema_version}） / "
                "測定アセットを検証済み"
            )
            set_semantic_state(self.validation_status, SemanticState.SUCCESS)


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

        self.progress_card, self.progress_label, self.progress_bar = self._build_progress_card(content)
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
        set_control_size(self.migration_export_button, ControlSize.PROMINENT)
        set_primary_action(self.migration_export_button)
        self.migration_export_button.clicked.connect(self._choose_backup_destination)
        migration_actions.addWidget(self.migration_export_button)

        self.migration_import_button = QPushButton(
            "以前のPCの移行ファイルを読み込む",
            migration_card,
        )
        self.migration_import_button.setObjectName("dataManagementMigrationImportButton")
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
            "バックアップ作成後は同じnative authorityで検証されます。"
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
            "HTDTが管理するデータベースと測定アセットを別のドライブやフォルダへ"
            "移動します。コピーと検証が完了するまで元の場所は変更されません。",
            relocation_card,
        )
        relocation_text.setWordWrap(True)
        set_typography_role(relocation_text, TypographyRole.BODY)
        relocation_layout.addWidget(relocation_text)

        self.relocate_button = QPushButton(
            "移動先フォルダを選択", relocation_card
        )
        self.relocate_button.setObjectName("dataManagementRelocateButton")
        set_control_size(self.relocate_button, ControlSize.STANDARD)
        self.relocate_button.clicked.connect(self._choose_relocation_destination)
        relocation_layout.addWidget(self.relocate_button)
        layout.addWidget(relocation_card)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.backup_button = QPushButton("バックアップを作成", operations_card)
        self.backup_button.setObjectName("dataManagementBackupButton")
        set_control_size(self.backup_button, ControlSize.STANDARD)
        self.backup_button.clicked.connect(self._choose_backup_destination)
        actions.addWidget(self.backup_button)

        self.select_restore_button = QPushButton("復元ファイルを選択", operations_card)
        self.select_restore_button.setObjectName("dataManagementSelectRestoreButton")
        set_control_size(self.select_restore_button, ControlSize.STANDARD)
        self.select_restore_button.clicked.connect(self._choose_restore_file)
        actions.addWidget(self.select_restore_button)
        actions.addStretch(1)
        operations_layout.addLayout(actions)

        # Round9 audit: saved generations were invisible — restore required
        # remembering where a backup file lived. List every restorable
        # archive the app knows about (automatic generations + pre-upgrade
        # recovery copies) so the user can pick one directly.
        self.generations_row = QWidget(operations_card)
        generations_layout = QHBoxLayout(self.generations_row)
        generations_layout.setContentsMargins(0, 0, 0, 0)
        generations_layout.setSpacing(10)
        generations_label = QLabel(
            "保存済みバックアップ:", self.generations_row
        )
        generations_layout.addWidget(generations_label)
        self.generations_combo = QComboBox(self.generations_row)
        self.generations_combo.setObjectName("dataManagementGenerationsCombo")
        generations_layout.addWidget(self.generations_combo, 1)
        self.generation_restore_button = QPushButton(
            "このバックアップを検証して復元…", self.generations_row
        )
        self.generation_restore_button.setObjectName(
            "dataManagementGenerationRestoreButton"
        )
        set_control_size(
            self.generation_restore_button, ControlSize.STANDARD
        )
        self.generation_restore_button.clicked.connect(
            self._preview_selected_generation
        )
        generations_layout.addWidget(self.generation_restore_button)
        operations_layout.addWidget(self.generations_row)
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
        set_control_size(self.backup_dir_button, ControlSize.STANDARD)
        self.backup_dir_button.clicked.connect(
            self._choose_backup_policy_dir
        )
        dir_layout.addWidget(self.backup_dir_button)
        self.backup_dir_reset_button = QPushButton("既定に戻す", dir_row)
        self.backup_dir_reset_button.setObjectName(
            "dataManagementBackupDirResetButton"
        )
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

        storage_title = QLabel("ストレージとエビデンス管理", storage_card)
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
        controller.relocation_completed.connect(self._on_relocation_completed)
        controller.storage_scan_completed.connect(self._on_storage_scan_completed)
        controller.storage_gc_completed.connect(self._on_storage_gc_completed)
        controller.operation_failed.connect(self._on_operation_failed)

        self._refresh_generations()
        if self._restart_required:
            self._show_restart_required(
                "データを安全に読み直せませんでした。HTDTを再起動してください。"
            )
        else:
            self._refresh_actions()

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

    def _build_progress_card(self, parent: QWidget) -> tuple[QFrame, QLabel, QProgressBar]:
        card = QFrame(parent)
        card.setObjectName("dataManagementProgressCard")
        set_surface_role(card, SurfaceRole.RAISED)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)
        label = QLabel("処理を開始しています", card)
        set_typography_role(label, TypographyRole.BODY)
        layout.addWidget(label)
        progress = QProgressBar(card)
        progress.setObjectName("dataManagementProgress")
        progress.setRange(0, 0)
        progress.setTextVisible(False)
        layout.addWidget(progress)
        card.hide()
        return card, label, progress

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
        self.controller.create_backup(destination)

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
        except Exception:  # noqa: BLE001 - listing must never break the page
            generations = ()
        self.generations_combo.clear()
        for path in generations:
            self.generations_combo.addItem(path.name, str(path))
        self.generations_row.setVisible(bool(generations))
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
        self._refresh_actions()

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
        except Exception:  # noqa: BLE001 - warning must never break the page
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
                "レガシーデータベースを読み取れませんでした: "
                f"{report.detail}"
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
            "自動バックアップの保存先フォルダを選択",
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
        self.migration_export_button.setEnabled(available)
        self.migration_import_button.setEnabled(available)
        self.relocate_button.setEnabled(available)
        self.storage_button.setEnabled(available)
        self.generations_combo.setEnabled(available)
        self.generation_restore_button.setEnabled(available)
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
