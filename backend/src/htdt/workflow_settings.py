from __future__ import annotations

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .application_preferences import (
    ApplicationPreferenceStore,
    PreferenceCategory,
    PreferenceDefinition,
    PreferenceError,
    PreferenceValueType,
)
from .data_management_ui import DataManagementComponent
from .ui_theme import (
    SemanticState,
    TypographyRole,
    set_semantic_state,
    set_typography_role,
)
from .user_facing_error import operation_error_message


_CATEGORY_LABELS: dict[PreferenceCategory, str] = {
    PreferenceCategory.GENERAL: "一般",
    PreferenceCategory.DISPLAY_INPUT: "表示と入力",
    PreferenceCategory.INTEGRATIONS: "連携",
    PreferenceCategory.COMPUTE: "計算",
    PreferenceCategory.FILES_EXPORT: "ファイルと出力",
    PreferenceCategory.DIAGNOSTICS: "診断",
}


class PreferencesWidget(QWidget):
    """Settings surface over :class:`ApplicationPreferenceStore` (#740).

    Every control is bound to one registered preference definition and
    writes straight through to the durable store, so there is no draft
    state that can drift from what is persisted. When the on-disk file is
    in a write-refused load state (corrupt or written by a newer schema)
    the editors disable fail-closed — the widget can never overwrite a
    document this build did not fully consume.
    """

    def __init__(
        self,
        store: ApplicationPreferenceStore,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("preferencesWidget")
        self._store = store
        self._editors: dict[str, QWidget] = {}
        self._loading = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        self.status = QLabel(self)
        self.status.setObjectName("preferencesStatus")
        self.status.setWordWrap(True)
        set_typography_role(self.status, TypographyRole.SECONDARY)
        layout.addWidget(self.status)

        self.reset_button = QPushButton("すべて既定値に戻す", self)
        self.reset_button.setObjectName("preferencesResetAll")
        self.reset_button.clicked.connect(self._reset_all)
        layout.addWidget(self.reset_button)

        scroll = QScrollArea(self)
        scroll.setObjectName("preferencesScroll")
        scroll.setWidgetResizable(True)
        host = QWidget()
        self._form_host_layout = QVBoxLayout(host)
        self._form_host_layout.setContentsMargins(0, 0, 0, 0)
        self._form_host_layout.setSpacing(12)
        scroll.setWidget(host)
        layout.addWidget(scroll, 1)

        for category in PreferenceCategory:
            definitions = store.definitions(category)
            if not definitions:
                continue
            group = QGroupBox(
                _CATEGORY_LABELS.get(category, category.value), host
            )
            form = QFormLayout(group)
            for definition in definitions:
                form.addRow(
                    self._row_label(definition),
                    self._build_editor(definition),
                )
            self._form_host_layout.addWidget(group)
        self._form_host_layout.addStretch(1)

        self.reload()

    @staticmethod
    def _row_label(definition: PreferenceDefinition) -> QLabel:
        text = definition.key
        if definition.restart_required:
            text += "（再起動が必要）"
        label = QLabel(text)
        if definition.description:
            label.setToolTip(definition.description)
        return label

    def _build_editor(self, definition: PreferenceDefinition) -> QWidget:
        key = definition.key
        if definition.value_type == PreferenceValueType.BOOLEAN:
            editor: QWidget = QCheckBox()
            editor.toggled.connect(  # type: ignore[attr-defined]
                lambda checked, k=key: self._commit(k, bool(checked))
            )
        elif definition.value_type == PreferenceValueType.ENUM:
            combo = QComboBox()
            for value in definition.allowed_values or ():
                combo.addItem(str(value), userData=value)
            combo.currentIndexChanged.connect(
                lambda _index, k=key, c=combo: self._commit(k, c.currentData())
            )
            editor = combo
        elif definition.value_type == PreferenceValueType.INTEGER:
            spin = QSpinBox()
            spin.setMinimum(
                int(definition.min_value) if definition.min_value is not None else -2_147_483_648
            )
            spin.setMaximum(
                int(definition.max_value) if definition.max_value is not None else 2_147_483_647
            )
            spin.valueChanged.connect(
                lambda value, k=key: self._commit(k, int(value))
            )
            editor = spin
        elif definition.value_type == PreferenceValueType.NUMBER:
            spinf = QDoubleSpinBox()
            spinf.setMinimum(
                float(definition.min_value) if definition.min_value is not None else -1e12
            )
            spinf.setMaximum(
                float(definition.max_value) if definition.max_value is not None else 1e12
            )
            spinf.valueChanged.connect(
                lambda value, k=key: self._commit(k, float(value))
            )
            editor = spinf
        else:  # STRING / PATH
            line = QLineEdit()
            line.editingFinished.connect(
                lambda k=key, e=line: self._commit(k, e.text())
            )
            editor = line
        editor.setObjectName(f"preferenceEditor:{key}")
        if definition.description:
            editor.setToolTip(definition.description)
        self._editors[key] = editor
        return editor

    def _commit(self, key: str, value: object) -> None:
        if self._loading:
            return
        try:
            self._store.set(key, value)
        except PreferenceError as exc:
            self.status.setText(f"保存できませんでした: {operation_error_message(exc)}")
            set_semantic_state(self.status, SemanticState.ERROR)
            self.reload()
            return
        self._sync_editor(key)

    def _reset_all(self) -> None:
        try:
            if self._store.write_allowed:
                self._store.update(
                    {d.key: d.default for d in self._store.definitions()}
                )
            else:
                # The only sanctioned recovery for a corrupt/newer-schema
                # file; the old document is preserved under .recovery first.
                self._store.reset_persisted_file()
        except PreferenceError as exc:
            self.status.setText(f"既定値への復元に失敗しました: {operation_error_message(exc)}")
            set_semantic_state(self.status, SemanticState.ERROR)
        self.reload()

    def _sync_editor(self, key: str) -> None:
        self._loading = True
        try:
            self._set_editor_value(self._editors[key], self._store.get(key))
        finally:
            self._loading = False

    @staticmethod
    def _set_editor_value(editor: QWidget, value: object) -> None:
        if isinstance(editor, QCheckBox):
            editor.setChecked(bool(value))
        elif isinstance(editor, QComboBox):
            index = editor.findData(value)
            editor.setCurrentIndex(index if index >= 0 else 0)
        elif isinstance(editor, (QSpinBox, QDoubleSpinBox)):
            editor.setValue(value)  # type: ignore[arg-type]
        elif isinstance(editor, QLineEdit):
            editor.setText(str(value))

    def reload(self) -> None:
        """Re-pull every control from the store and refresh the banner."""
        self._loading = True
        try:
            for key, editor in self._editors.items():
                self._set_editor_value(editor, self._store.get(key))
        finally:
            self._loading = False
        writable = self._store.write_allowed
        for editor in self._editors.values():
            editor.setEnabled(writable)
        if not writable:
            detail = self._store.load_error or "unknown"
            self.status.setText(
                "設定ファイルを上書きできません"
                f"（{self._store.load_state.value}: {detail}）。"
                "リセットすると以前のファイルを保存した上で既定値に戻ります。"
            )
            set_semantic_state(self.status, SemanticState.WARNING)
        elif self._store.load_error:
            self.status.setText(f"一部の設定を読み込めませんでした: {self._store.load_error}")
            set_semantic_state(self.status, SemanticState.WARNING)
        else:
            self.status.setText("")
            set_semantic_state(self.status, None)


class DataManagementDialog(QDialog):
    """Settings host that keeps destructive data operations fail-closed."""

    def __init__(
        self,
        component: DataManagementComponent,
        parent: QWidget | None = None,
        capture_panel: QWidget | None = None,
        preferences_panel: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.component = component
        self.setWindowTitle("設定")
        self.setModal(False)
        self.resize(860, 720)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        component.widget.setParent(self)
        self._capture_panel = capture_panel
        self._preferences_panel = preferences_panel
        if capture_panel is None and preferences_panel is None:
            layout.addWidget(component.widget)
            self._tabs = None
        else:
            self._tabs = QTabWidget(self)
            self._tabs.addTab(component.widget, "データ管理")
            if preferences_panel is not None:
                self._tabs.addTab(preferences_panel, "環境設定")
            if capture_panel is not None:
                self._tabs.addTab(capture_panel, "キャプチャ")
            layout.addWidget(self._tabs)

    def open_settings(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def open_preferences(self) -> None:
        if self._tabs is not None and self._preferences_panel is not None:
            self._tabs.setCurrentWidget(self._preferences_panel)
        self.open_settings()

    def open_capture_settings(self) -> None:
        if self._tabs is not None and self._capture_panel is not None:
            self._tabs.setCurrentWidget(self._capture_panel)
        self.open_settings()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        allowed, reason = self.component.before_deactivate()
        if not allowed:
            # QDialog has no statusBar; surface the refusal reason instead of
            # silently swallowing the close.
            if reason:
                QMessageBox.information(self, self.windowTitle(), reason)
            event.ignore()
            return
        event.accept()


__all__ = ["DataManagementDialog", "PreferencesWidget"]
