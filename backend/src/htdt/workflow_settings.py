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

from .modal_transient import exec_transient
from .application_preferences import (
    PENDING_PREFERENCE_KEYS,
    PREFERENCES_RECOVERY_SUFFIX,
    ApplicationPreferenceStore,
    PreferenceCategory,
    PreferenceChange,
    PreferenceDefinition,
    PreferenceError,
    PreferenceLoadState,
    PreferenceNotificationError,
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
    PreferenceCategory.MAINTENANCE: "メンテナンス",
}


#: User-facing Japanese name per registered preference key — the row label
#: must never render the internal dotted identifier. A key added to
#: PREFERENCE_DEFINITIONS without an entry here falls back to the raw key
#: so nothing silently lies about what it edits.
_PREFERENCE_LABELS: dict[str, str] = {
    'general.language': '表示言語',
    'general.startup_destination': '起動時に表示する画面',
    'general.reopen_last_project': '前回のプロジェクトを再開する',
    'display_input.length_unit': '長さの表示単位',
    'display_input.numeric_precision': '数値の小数点以下桁数',
    'display_input.angle_unit': '角度の表示単位',
    'display_input.theme': '外観テーマ',
    'display_input.reduced_motion': '視覚効果を減らす',
    'display_input.high_contrast': 'ハイコントラスト表示',
    'display_input.reflection_guidance_overlay': '反射ガイダンスのオーバーレイ',
    'integrations.rew_host': 'REW APIホスト',
    'integrations.rew_port': 'REW APIポート',
    'integrations.rew_auto_launch': 'REWの起動ボタンを表示する',
    'integrations.rew_auto_ingest': '新しいREW測定を自動で読み込む',
    'integrations.rew_watch_dir': 'REWテキストの監視フォルダー',
    'integrations.rew_install_path': 'REWのインストール場所',
    'integrations.capture_receiver_enabled': 'キャプチャレシーバーを有効化',
    'integrations.capture_watch_dir': 'キャプチャバンドルの監視フォルダー',
    'compute.preferred_backend': '実行バックエンドの優先順位',
    'compute.max_concurrency': '長時間ジョブの最大並行数',
    'compute.scratch_dir': '作業フォルダー',
    'compute.storage_ceiling_mb': '作業領域の上限 (MiB)',
    'files.export_dir': 'エクスポート先フォルダー',
    'files.portable_bundle_include_libraries': 'バンドルにライブラリ定義を含める',
    'diagnostics.include_project_ids': '診断パッケージにプロジェクト識別子を含める',
    'maintenance.storage_watch_enabled': 'ストレージの定期スキャン',
}


#: One-line Japanese description per key — shown as the row/tooltip hint
#: instead of the definition's English authoring note.
_PREFERENCE_DESCRIPTIONS: dict[str, str] = {
    'general.language': 'ユーザーインターフェースの表示言語です。',
    'general.startup_destination': '起動時に最初に表示する画面です。',
    'general.reopen_last_project': '起動時に最近使ったプロジェクトを提案・再開します。',
    'display_input.length_unit': '長さの表示単位です（内部のSI保存値は変わりません）。',
    'display_input.numeric_precision': '表示する小数点以下の桁数です。',
    'display_input.angle_unit': '角度の表示単位です（現在は度のみ）。',
    'display_input.theme': '外観テーマのポリシーです。',
    'display_input.reduced_motion': 'OS設定で不十分な場合にアニメーション等を抑えます。',
    'display_input.high_contrast': 'OS設定で不十分な場合にコントラストを上げます。',
    'display_input.reflection_guidance_overlay': '確定的パス権威が実証した一次反射ゾーンと音源再配置マーカーを3Dビューに表示するかどうかです。「自動」は音響コンテキストを開いている間だけ表示します。',
    'integrations.rew_host': 'REW APIの接続先ホストです（ループバックのみ）。',
    'integrations.rew_port': 'REW APIの接続ポートです。',
    'integrations.rew_auto_launch': 'REWが起動していないとき、測定ページにワンクリック起動ボタンを表示します。',
    'integrations.rew_auto_ingest': 'REW APIで検出した新しい測定を読み込みキューへ自動追加します。',
    'integrations.rew_watch_dir': 'このフォルダーに保存されたREWテキスト(.txt/.frd/.mdat)を読み込みキューへ自動追加します（空欄 = 監視しません）。',
    'integrations.rew_install_path': 'REW実行ファイルまたはアプリの場所です（空欄 = 標準のインストール場所を探します）。',
    'integrations.capture_receiver_enabled': 'ネイティブキャプチャレシーバーを有効にします。',
    'integrations.capture_watch_dir': 'このフォルダーに新しく保存されたキャプチャバンドル(.htdtcapture)を受信ボックスへ自動ステージします（空欄 = 監視しません）。証拠には昇格しません。',
    'compute.preferred_backend': '検証済みの実行バックエンドの中から優先順位を選びます。',
    'compute.max_concurrency': '長時間ジョブのローカル並行数の上限です。',
    'compute.scratch_dir': '作業・キャッシュ領域のルートです（空欄 = データフォルダー既定値）。',
    'compute.storage_ceiling_mb': '作業・キャッシュ領域の上限MiBです（0 = 管理しません）。',
    'files.export_dir': 'エクスポート・レポートの既定フォルダーです（空欄 = システムのドキュメント）。',
    'files.portable_bundle_include_libraries': 'ポータブルバンドルに再利用可能なライブラリ定義を同梱します。',
    'diagnostics.include_project_ids': '診断パッケージにプロジェクト識別子・ハッシュを含めます。',
    'maintenance.storage_watch_enabled': '起動中に定期的にデータフォルダーを読み取り専用でスキャンし、参照切れや回収可能な未参照ファイルを検出したときだけ通知します（削除はしません）。',
}


#: Japanese labels for enum option values, keyed per preference key. Values
#: missing from a key's map render raw — host addresses and unit symbols are
#: already the right display form.
_PREFERENCE_VALUE_LABELS: dict[str, dict[object, str]] = {
    'general.language': {
        'system_default': 'システムに従う',
        'ja': '日本語',
        'en': 'English',
    },
    'general.startup_destination': {
        'overview': '概要',
        'reopen_last_project': '前回のプロジェクトを再開',
    },
    'display_input.angle_unit': {
        'degree': '度',
    },
    'display_input.theme': {
        'system': 'システムに従う',
        'light': 'ライト',
        'dark': 'ダーク',
    },
    'compute.preferred_backend': {
        'auto': '自動',
        'cpu': 'CPU',
        'gpu_when_validated': 'GPU（検証済みのみ）',
    },
    'display_input.reflection_guidance_overlay': {
        'off': '表示しない',
        'auto': '自動（音響コンテキストのみ）',
        'on': '常に表示',
    },
}


#: The write-refused banner names the load state — the enum value is an
#: internal id, so map it to what the operator is actually looking at.
_LOAD_STATE_LABELS: dict[PreferenceLoadState, str] = {
    PreferenceLoadState.OK: '正常',
    PreferenceLoadState.MISSING: 'なし',
    PreferenceLoadState.PARTIAL_INVALID_VALUE: '一部の値が無効',
    PreferenceLoadState.CORRUPT: '破損',
    PreferenceLoadState.INCOMPATIBLE_NEWER_SCHEMA: '新しい形式のファイル',
}


def _reset_effect_label(definition: PreferenceDefinition) -> str:
    if definition.restart_required:
        return '再起動後に反映'
    if definition.category == PreferenceCategory.INTEGRATIONS:
        return '即時反映 · 連携設定が変わります'
    return '即時反映'


def _reset_value_text(value: object) -> str:
    if isinstance(value, bool):
        return '有効' if value else '無効'
    return str(value)


class PreferencesResetDialog(QDialog):
    """#987: diff-confirm dialog for すべて既定値に戻す.

    Shows only the keys that would actually change, grouped by category
    with current value / default / apply-effect. The operator may reset
    all categories or a checked subset; nothing reaches the store until
    one of the reset buttons is accepted.
    """

    def __init__(
        self,
        pending: tuple[tuple[PreferenceDefinition, object], ...],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName('preferencesResetDialog')
        self.setWindowTitle('既定値への復元の確認')
        self._chosen_keys: frozenset[str] | None = None
        self._category_checks: dict[PreferenceCategory, QCheckBox] = {}
        self._category_keys: dict[PreferenceCategory, tuple[str, ...]] = {}

        layout = QVBoxLayout(self)
        intro = QLabel(
            '以下の設定が既定値に戻ります。初期化するカテゴリを選んでください。',
            self,
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        host = QWidget()
        groups_layout = QVBoxLayout(host)
        groups_layout.setContentsMargins(0, 0, 0, 0)

        by_category: dict[PreferenceCategory, list[tuple[PreferenceDefinition, object]]] = {}
        for definition, current in pending:
            by_category.setdefault(definition.category, []).append(
                (definition, current)
            )

        for category in PreferenceCategory:
            rows = by_category.get(category)
            if not rows:
                continue
            self._category_keys[category] = tuple(
                definition.key for definition, _current in rows
            )
            check = QCheckBox(_CATEGORY_LABELS.get(category, category.value), host)
            check.setChecked(True)
            check.setAccessibleName(
                f'カテゴリ「{_CATEGORY_LABELS.get(category, category.value)}」を初期化する'
            )
            self._category_checks[category] = check
            groups_layout.addWidget(check)
            for definition, current in rows:
                row = QLabel(
                    '　'
                    + _PREFERENCE_LABELS.get(definition.key, definition.key)
                    + f': {_reset_value_text(current)}'
                    + f' → {_reset_value_text(definition.default)}'
                    + f'（{_reset_effect_label(definition)}）',
                    host,
                )
                row.setWordWrap(True)
                set_typography_role(row, TypographyRole.SECONDARY)
                groups_layout.addWidget(row)
        groups_layout.addStretch(1)
        scroll.setWidget(host)
        layout.addWidget(scroll, 1)

        buttons_row = QGroupBox(self)
        buttons_layout = QVBoxLayout(buttons_row)
        self.selected_button = QPushButton('選択カテゴリのみ初期化', buttons_row)
        self.selected_button.setToolTip(
            'チェックしたカテゴリの設定だけを既定値に戻します'
        )
        self.selected_button.clicked.connect(self._accept_selected)
        self.all_button = QPushButton('すべて初期化', buttons_row)
        self.all_button.setToolTip('一覧のすべての設定を既定値に戻します')
        self.all_button.clicked.connect(self._accept_all)
        cancel_button = QPushButton('キャンセル', buttons_row)
        cancel_button.clicked.connect(self.reject)
        buttons_layout.addWidget(self.selected_button)
        buttons_layout.addWidget(self.all_button)
        buttons_layout.addWidget(cancel_button)
        layout.addWidget(buttons_row)

    def _accept_selected(self) -> None:
        keys: list[str] = []
        for category, check in self._category_checks.items():
            if check.isChecked():
                keys.extend(self._category_keys.get(category, ()))
        if not keys:
            return
        self._chosen_keys = frozenset(keys)
        self.accept()

    def _accept_all(self) -> None:
        keys: list[str] = []
        for key_tuple in self._category_keys.values():
            keys.extend(key_tuple)
        self._chosen_keys = frozenset(keys)
        self.accept()

    def chosen_keys(self) -> frozenset[str] | None:
        return self._chosen_keys


class SanctionedResetDialog(QDialog):
    """#987: explicit warning before the destructive file reset.

    The corrupt/newer-schema persisted file can only be recovered via
    ``reset_persisted_file()``. This dialog names the ``.recovery``
    destination up front so the operator can cancel without discarding
    anything.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName('sanctionedResetDialog')
        self.setWindowTitle('設定ファイルの初期化')
        layout = QVBoxLayout(self)
        message = QLabel(
            '設定ファイルを読み込めないため、現在の値を編集できません。'
            f'初期化すると既存ファイルは «{PREFERENCES_RECOVERY_SUFFIX}» で'
            '終わる復旧用ファイルに保存され、すべて既定値で新規作成されます。',
            self,
        )
        message.setWordWrap(True)
        layout.addWidget(message)
        self.reset_button = QPushButton('初期化して既定値に戻す', self)
        self.reset_button.clicked.connect(self.accept)
        cancel_button = QPushButton('キャンセル', self)
        cancel_button.clicked.connect(self.reject)
        layout.addWidget(self.reset_button)
        layout.addWidget(cancel_button)


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
        self.reset_button.setToolTip(
            "変更される設定を確認してから初期化します"
        )
        self.reset_button.clicked.connect(self._reset_all)
        layout.addWidget(self.reset_button)

        self.rollback_button = QPushButton("元の設定に戻す", self)
        self.rollback_button.setObjectName("preferencesRollback")
        self.rollback_button.setToolTip(
            "直前の初期化で保存された値を復元します（外部機器の状態は戻りません）"
        )
        self.rollback_button.clicked.connect(self._rollback_reset)
        self.rollback_button.setVisible(False)
        layout.addWidget(self.rollback_button)
        self._rollback_snapshot: dict[str, object] | None = None

        scroll = QScrollArea(self)
        scroll.setObjectName("preferencesScroll")
        scroll.setWidgetResizable(True)
        host = QWidget()
        self._form_host_layout = QVBoxLayout(host)
        self._form_host_layout.setContentsMargins(0, 0, 0, 0)
        self._form_host_layout.setSpacing(12)
        scroll.setWidget(host)
        layout.addWidget(scroll, 1)

        # Other surfaces write the same keys (e.g. the キャプチャ tab
        # applies integrations.capture_receiver_enabled through its
        # controller) — keep the rendered controls in sync with every
        # committed change, not just this widget's own writes. The store
        # is app-scoped and outlives this widget's shell, so release the
        # subscription on destruction or an unrelated commit would keep
        # touching a dead editor tree.
        store.subscribe(self._on_external_change)
        # PySide6 silently never delivers ``destroyed`` to a bound method of
        # the object being destroyed (same caveat as data_management's
        # controller detach), so the release goes through a lambda.
        self.destroyed.connect(lambda: self.release())

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
        text = _PREFERENCE_LABELS.get(definition.key, definition.key)
        if definition.key in PENDING_PREFERENCE_KEYS:
            text += "（準備中）"
        if definition.restart_required:
            text += "（再起動が必要）"
        label = QLabel(text)
        if definition.key in PENDING_PREFERENCE_KEYS:
            hint = "この設定はまだ実装されていないため、現在は変更できません。"
        elif definition.description:
            hint = _PREFERENCE_DESCRIPTIONS.get(definition.key, definition.description)
        else:
            hint = ''
        if hint:
            label.setToolTip(hint)
            label.setWhatsThis(hint)
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
            value_labels = _PREFERENCE_VALUE_LABELS.get(definition.key, {})
            for value in definition.allowed_values or ():
                combo.addItem(
                    str(value_labels.get(value, value)), userData=value
                )
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
        if key in PENDING_PREFERENCE_KEYS:
            # Persisted value still loads, but no production consumer reads
            # it yet — show it, disabled, instead of a working-looking no-op.
            editor.setEnabled(False)
            hint = "この設定はまだ実装されていないため、現在は変更できません。"
            editor.setToolTip(hint)
            editor.setWhatsThis(hint)
        elif definition.description:
            hint = _PREFERENCE_DESCRIPTIONS.get(definition.key, definition.description)
            editor.setToolTip(hint)
            editor.setWhatsThis(hint)
        self._editors[key] = editor
        return editor

    def release(self) -> None:
        """Detach from the shared store; safe to call more than once."""

        self._store.unsubscribe(self._on_external_change)

    def _on_external_change(self, change: PreferenceChange) -> None:
        self._sync_editor(change.key)

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

    def _reset_candidates(
        self,
    ) -> tuple[tuple[PreferenceDefinition, object], ...]:
        """Definitions whose stored value differs from the default."""
        pending: list[tuple[PreferenceDefinition, object]] = []
        for definition in self._store.definitions():
            current = self._store.get(definition.key)
            if current != definition.default:
                pending.append((definition, current))
        return tuple(pending)

    def _confirm_reset(
        self,
        pending: tuple[tuple[PreferenceDefinition, object], ...],
    ) -> frozenset[str] | None:
        dialog = PreferencesResetDialog(pending, parent=self)
        if exec_transient(dialog) != QDialog.DialogCode.Accepted:
            return None
        return dialog.chosen_keys()

    def _confirm_sanctioned_reset(self) -> bool:
        dialog = SanctionedResetDialog(parent=self)
        return exec_transient(dialog) == QDialog.DialogCode.Accepted

    def _reset_all(self) -> None:
        if not self._store.write_allowed:
            # The only sanctioned recovery for a corrupt/newer-schema
            # file; warn first — the old document is preserved under
            # .recovery so nothing is silently discarded.
            if not self._confirm_sanctioned_reset():
                return
            try:
                recovery = self._store.reset_persisted_file()
            except PreferenceError as exc:
                self.reload()
                self.status.setText(
                    f"既定値への復元に失敗しました: {operation_error_message(exc)}"
                )
                set_semantic_state(self.status, SemanticState.ERROR)
            else:
                self.reload()
                if recovery is not None:
                    self.status.setText(
                        f"初期化しました。以前のファイルは {recovery} に保存されました。"
                    )
                else:
                    self.status.setText("初期化しました。")
            return

        pending = self._reset_candidates()
        if not pending:
            self.status.setText("すべて既定値のため変更はありません。")
            return
        keys = self._confirm_reset(pending)
        if not keys:
            self.status.setText("リセットはキャンセルされました。")
            return

        snapshot = {
            key: self._store.get(key)
            for key in keys
            if self._store.get(key) != self._store.definition(key).default
        }
        try:
            applied = self._store.update(
                {key: self._store.definition(key).default for key in keys}
            )
        except PreferenceNotificationError:
            # The durable commit already succeeded — only listener
            # notification failed. Never report this as rolled back.
            self._rollback_snapshot = snapshot
            self.rollback_button.setVisible(True)
            self.reload()
            self.status.setText(
                "設定は保存されましたが、一部の画面への反映に失敗しました。"
            )
            set_semantic_state(self.status, SemanticState.ERROR)
            return
        except PreferenceError as exc:
            self.reload()
            self.status.setText(
                f"既定値への復元に失敗しました: {operation_error_message(exc)}"
            )
            set_semantic_state(self.status, SemanticState.ERROR)
            return

        self._rollback_snapshot = snapshot
        self.rollback_button.setVisible(bool(snapshot))
        self.reload()
        self.status.setText(f"{len(applied)} 件を既定値に戻しました。")

    def _rollback_reset(self) -> None:
        snapshot = self._rollback_snapshot
        if not snapshot:
            return
        try:
            self._store.update(snapshot)
        except PreferenceError as exc:
            self.reload()
            self.status.setText(
                f"元の設定への復元に失敗しました: {operation_error_message(exc)}"
            )
            set_semantic_state(self.status, SemanticState.ERROR)
            return
        self._rollback_snapshot = None
        self.rollback_button.setVisible(False)
        self.reload()
        self.status.setText("元の設定に戻しました。")

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
        for key, editor in self._editors.items():
            editor.setEnabled(writable and key not in PENDING_PREFERENCE_KEYS)
        if not writable:
            detail = self._store.load_error or "unknown"
            load_state = _LOAD_STATE_LABELS.get(
                self._store.load_state, self._store.load_state.value
            )
            self.status.setText(
                "設定ファイルを上書きできません"
                f"（{load_state}: {detail}）。"
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
        retention_panel: QWidget | None = None,
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
        self._retention_panel = retention_panel
        if (
            capture_panel is None
            and preferences_panel is None
            and retention_panel is None
        ):
            layout.addWidget(component.widget)
            self._tabs = None
        else:
            self._tabs = QTabWidget(self)
            self._tabs.addTab(component.widget, "データ管理")
            self._tabs.setTabToolTip(
                self._tabs.indexOf(component.widget),
                "プロジェクトデータの管理（バックアップ・リセットなどの破壊的操作を含みます）",
            )
            if preferences_panel is not None:
                self._tabs.addTab(preferences_panel, "環境設定")
                self._tabs.setTabToolTip(
                    self._tabs.indexOf(preferences_panel),
                    "アプリケーションの表示・動作の環境設定",
                )
            if capture_panel is not None:
                self._tabs.addTab(capture_panel, "キャプチャ")
                self._tabs.setTabToolTip(
                    self._tabs.indexOf(capture_panel),
                    "HTDT Capture アプリからの測定パッケージ受信の設定",
                )
            if retention_panel is not None:
                self._tabs.addTab(retention_panel, "保持管理")
                self._tabs.setTabToolTip(
                    self._tabs.indexOf(retention_panel),
                    "保存データの保持期間と自動削除の設定",
                )
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

    def open_retention_settings(self) -> None:
        if self._tabs is not None and self._retention_panel is not None:
            self._tabs.setCurrentWidget(self._retention_panel)
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
