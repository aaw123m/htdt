"""資格情報ヴォールト operator panel (#951).

Operator surface over the #890 ``CredentialVaultService`` authority:
metadata-only credential list (``CredentialReference`` rows), store /
rotate / revoke / delete mutations, a reference *picker* that returns
credential ids (never secret material) for adapter ``credential_ref``
bindings, and the sealed lifecycle event log as the audit surface.

Honesty rules (mirroring the vault's fail-closed contract):

* secret material never reaches a widget — entry fields are
  password-echoed, handed to the service as ``SecretMaterial`` and
  cleared via ``take_secret``; copy/export actions only ever emit
  ids/metadata (``export_manifest``);
* the platform vault's real state drives enablement — ``locked`` /
  ``unavailable`` render honestly and disable material mutations while
  reference/audit metadata stays readable;
* store requires the document's recorded consent — the store dialog
  surfaces an explicit 同意 checkbox that seals a ``consent_recorded``
  lifecycle event BEFORE the store call;
* credential store/selection is NOT the #946 one-shot device-apply
  authorization — these dialogs seal vault lifecycle events, never a
  commissioning ``deploy_apply`` authorization;
* the picker surfaces scope drift and retrieval errors as actionable
  messages (revoked/deleted rows are not selectable for binding).
"""

from __future__ import annotations

import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .cad_credential_vault import (
    CredentialReference,
    CredentialVaultService,
    SecretMaterial,
)
from .ui_theme import SemanticState, set_semantic_state
from .user_facing_error import operation_error_message


_SCOPE_KIND_LABELS = {
    'provider': 'プロバイダー',
    'device': 'デバイス',
    'service': 'サービス',
}
_CREDENTIAL_TYPE_LABELS = {
    'password': 'パスワード',
    'api_token': 'APIトークン',
    'certificate': '証明書',
    'device_key': 'デバイスキー',
    'pairing_secret': 'ペアリングシークレット',
    'account': 'アカウント',
}
_VAULT_SCOPE_LABELS = {
    'user': 'ユーザー',
    'machine': 'マシン (このホストの全アカウント)',
}
_REFERENCE_STATE_LABELS = {
    'active': '有効',
    'revoked': '失効済み',
    'deleted': '削除済み',
}
_EVENT_KIND_LABELS = {
    'consent_recorded': '同意記録',
    'stored': '登録',
    'rotated': 'ローテーション',
    'revoked': '失効',
    'deleted': '削除',
    'used': '利用',
    'retrieve_denied': '取得拒否',
}


def _scope_text(reference: CredentialReference) -> str:
    kind = _SCOPE_KIND_LABELS.get(
        reference.scope_kind, reference.scope_kind)
    return f'{kind}: {reference.scope_ref}'


def _reference_credential_id(item: QTreeWidgetItem) -> str | None:
    value = item.data(0, Qt.ItemDataRole.UserRole)
    return str(value) if value else None


def _current_references(
    references,
) -> dict[str, CredentialReference]:
    """Latest row per ``credential_id`` — reference rows are append-only
    and tombstones bump ``version``, so the max version wins."""

    current: dict[str, CredentialReference] = {}
    for reference in references:
        existing = current.get(reference.credential_id)
        if existing is None or reference.version >= existing.version:
            current[reference.credential_id] = reference
    return current


class CredentialStoreDialog(QDialog):
    """新規資格情報の登録フォーム (#951).

    The secret field is password-echoed; the caller reads it via
    :meth:`take_secret`, which clears the edit. ``identity_hint`` stays
    non-secret (the sealed model rejects secret-shaped values). When the
    document has no recorded consent yet, the explicit consent checkbox
    must be ticked before OK unlocks — the panel then seals a
    ``consent_recorded`` event before calling ``store_credential``.
    """

    def __init__(
        self,
        *,
        consent_needed: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setModal(True)
        self.setWindowTitle('資格情報の登録')
        self._consent_needed = consent_needed

        layout = QVBoxLayout(self)
        note = QLabel(
            '登録するのは資格情報の「参照」とプラットフォームの安全な'
            '保管庫への値です。プロジェクトデータには秘密の値は'
            '保存されません。', self)
        note.setWordWrap(True)
        layout.addWidget(note)

        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)

        self.scope_kind_combo = QComboBox(self)
        self.scope_kind_combo.setAccessibleName('スコープ種別')
        for kind, label in _SCOPE_KIND_LABELS.items():
            self.scope_kind_combo.addItem(label, kind)
        form.addRow('スコープ種別', self.scope_kind_combo)

        self.scope_ref_edit = QLineEdit(self)
        self.scope_ref_edit.setAccessibleName('スコープ対象')
        self.scope_ref_edit.setPlaceholderText(
            '対象の名前（例: avr-1, rew-api, minidsp-2x4）')
        form.addRow('スコープ対象（必須）', self.scope_ref_edit)

        self.type_combo = QComboBox(self)
        self.type_combo.setAccessibleName('資格情報の種別')
        for kind, label in _CREDENTIAL_TYPE_LABELS.items():
            self.type_combo.addItem(label, kind)
        form.addRow('種別', self.type_combo)

        self.vault_scope_combo = QComboBox(self)
        self.vault_scope_combo.setAccessibleName('保管範囲')
        for scope, label in _VAULT_SCOPE_LABELS.items():
            self.vault_scope_combo.addItem(label, scope)
        form.addRow('保管範囲', self.vault_scope_combo)

        self.hint_edit = QLineEdit(self)
        self.hint_edit.setAccessibleName('識別ヒント')
        self.hint_edit.setPlaceholderText(
            'アカウント名や用途メモなど、非シークレット情報のみ')
        form.addRow('識別ヒント（任意・非シークレット）', self.hint_edit)

        self.secret_edit = QLineEdit(self)
        self.secret_edit.setAccessibleName('秘密の値')
        self.secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret_edit.setPlaceholderText('この値は画面に表示されません')
        form.addRow('秘密の値（必須）', self.secret_edit)

        self.operator_edit = QLineEdit(self)
        self.operator_edit.setAccessibleName('オペレーターID')
        self.operator_edit.setPlaceholderText('オペレーターID（必須）')
        form.addRow('オペレーターID', self.operator_edit)
        layout.addLayout(form)

        self.consent_check = QCheckBox(
            'このプロジェクトでの資格情報保管に同意します', self)
        self.consent_check.setAccessibleName('保管への同意')
        self.consent_check.setToolTip(
            '同意は封緘されたライフサイクルイベントとして記録されます。')
        if consent_needed:
            layout.addWidget(self.consent_check)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, parent=self)
        self._buttons.button(
            QDialogButtonBox.StandardButton.Ok).setText('登録')
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

        for edit in (
            self.scope_ref_edit, self.secret_edit, self.operator_edit,
        ):
            edit.textChanged.connect(self._sync_ok)
        self.consent_check.toggled.connect(self._sync_ok)
        self._sync_ok()
        self.scope_ref_edit.setFocus()

    def _sync_ok(self) -> None:
        ok = (
            bool(self.scope_ref_edit.text().strip())
            and bool(self.secret_edit.text())
            and bool(self.operator_edit.text().strip())
        )
        if self._consent_needed:
            ok = ok and self.consent_check.isChecked()
        self._buttons.button(
            QDialogButtonBox.StandardButton.Ok).setEnabled(ok)

    # -- read-out ---------------------------------------------------------

    @property
    def scope_kind(self) -> str:
        return self.scope_kind_combo.currentData()

    @property
    def scope_ref(self) -> str:
        return self.scope_ref_edit.text().strip()

    @property
    def credential_type(self) -> str:
        return self.type_combo.currentData()

    @property
    def vault_scope(self) -> str:
        return self.vault_scope_combo.currentData()

    @property
    def identity_hint(self) -> str | None:
        text = self.hint_edit.text().strip()
        return text or None

    @property
    def operator_id(self) -> str:
        return self.operator_edit.text().strip()

    @property
    def consent_requested(self) -> bool:
        return self._consent_needed and self.consent_check.isChecked()

    def take_secret(self) -> str:
        """Return the entered secret once, then clear the field — the
        dialog never keeps the value reachable afterwards."""
        value = self.secret_edit.text()
        self.secret_edit.clear()
        return value


class CredentialRotateDialog(QDialog):
    """ローテーション — replaces the secret under the same
    ``credential_id`` (a new sealed reference row, version + 1)."""

    def __init__(
        self,
        reference: CredentialReference,
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setModal(True)
        self.setWindowTitle('資格情報のローテーション')
        layout = QVBoxLayout(self)

        note = QLabel(
            '新しい値に置き換えます。資格情報IDは変わらないため、'
            'この資格情報を参照する設定・証跡はそのまま使えます。', self)
        note.setWordWrap(True)
        layout.addWidget(note)

        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        for label, value in (
            ('資格情報ID', reference.credential_id),
            ('スコープ', _scope_text(reference)),
            ('種別', _CREDENTIAL_TYPE_LABELS.get(
                reference.credential_type, reference.credential_type)),
            ('現在の版', str(reference.version)),
        ):
            value_label = QLabel(value, self)
            value_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
            value_label.setWordWrap(True)
            form.addRow(label, value_label)

        self.secret_edit = QLineEdit(self)
        self.secret_edit.setAccessibleName('新しい秘密の値')
        self.secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret_edit.setPlaceholderText('この値は画面に表示されません')
        form.addRow('新しい値（必須）', self.secret_edit)

        self.operator_edit = QLineEdit(self)
        self.operator_edit.setAccessibleName('オペレーターID')
        self.operator_edit.setPlaceholderText('オペレーターID（必須）')
        form.addRow('オペレーターID', self.operator_edit)
        layout.addLayout(form)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, parent=self)
        self._buttons.button(
            QDialogButtonBox.StandardButton.Ok).setText('ローテーション')
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

        self.secret_edit.textChanged.connect(self._sync_ok)
        self.operator_edit.textChanged.connect(self._sync_ok)
        self._sync_ok()
        self.secret_edit.setFocus()

    def _sync_ok(self) -> None:
        self._buttons.button(
            QDialogButtonBox.StandardButton.Ok).setEnabled(
            bool(self.secret_edit.text())
            and bool(self.operator_edit.text().strip()))

    @property
    def operator_id(self) -> str:
        return self.operator_edit.text().strip()

    def take_secret(self) -> str:
        value = self.secret_edit.text()
        self.secret_edit.clear()
        return value


class CredentialConfirmDialog(QDialog):
    """失効 / 削除の確認 — the operator confirms which metadata row the
    tombstone applies to. Distinct from the #946 one-shot device-apply
    authorization: this seals a vault lifecycle event only."""

    def __init__(
        self,
        reference: CredentialReference,
        *,
        action: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if action not in ('revoke', 'delete'):
            raise ValueError(f'unknown confirm action {action!r}')
        self._action = action
        self.setModal(True)
        self.setWindowTitle(
            '資格情報の失効' if action == 'revoke' else '資格情報の削除')
        layout = QVBoxLayout(self)

        consequence = QLabel(
            '失効後はこの資格情報を取得できなくなります。'
            '利用側は「失効」として失敗し、参照は監査用に残ります。'
            if action == 'revoke' else
            '削除は保管庫の値自体も破棄し、以後の取得は全て失敗します。'
            '参照行は tombstone として監査用に残り、元に戻せません。',
            self)
        consequence.setWordWrap(True)
        layout.addWidget(consequence)

        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        for label, value in (
            ('資格情報ID', reference.credential_id),
            ('スコープ', _scope_text(reference)),
            ('種別', _CREDENTIAL_TYPE_LABELS.get(
                reference.credential_type, reference.credential_type)),
            ('版', str(reference.version)),
        ):
            value_label = QLabel(value, self)
            value_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
            value_label.setWordWrap(True)
            form.addRow(label, value_label)

        self.operator_edit = QLineEdit(self)
        self.operator_edit.setAccessibleName('オペレーターID')
        self.operator_edit.setPlaceholderText('オペレーターID（必須）')
        form.addRow('オペレーターID', self.operator_edit)

        self.reason_edit = QLineEdit(self)
        self.reason_edit.setAccessibleName('理由')
        self.reason_edit.setPlaceholderText('理由（任意・非シークレット）')
        form.addRow('理由', self.reason_edit)
        layout.addLayout(form)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, parent=self)
        self._buttons.button(
            QDialogButtonBox.StandardButton.Ok).setText(
            '失効する' if action == 'revoke' else '削除する')
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)
        self.operator_edit.textChanged.connect(self._sync_ok)
        self._sync_ok()
        self.operator_edit.setFocus()

    def _sync_ok(self) -> None:
        self._buttons.button(
            QDialogButtonBox.StandardButton.Ok).setEnabled(
            bool(self.operator_edit.text().strip()))

    @property
    def operator_id(self) -> str:
        return self.operator_edit.text().strip()

    @property
    def reason(self) -> str | None:
        text = self.reason_edit.text().strip()
        return text or None


class CredentialReferencePickerDialog(QDialog):
    """アダプタ設定向けの資格情報「参照」ピッカー (#951).

    Returns the selected ``credential_id`` — an ID, never secret
    material — so callers can place it into a ``credential_ref``
    binding field. Only ``active`` references are listed: revoked and
    deleted credentials cannot be bound. When ``expected_scope_kind`` /
    ``expected_scope_ref`` are given, a selection that does not match
    the target scope is rejected with an actionable drift message.
    """

    def __init__(
        self,
        service: CredentialVaultService,
        document_id: str,
        *,
        expected_scope_kind: str | None = None,
        expected_scope_ref: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._document_id = document_id
        self._expected_scope_kind = expected_scope_kind
        self._expected_scope_ref = expected_scope_ref
        self._selected_credential_id: str | None = None
        self.setModal(True)
        self.setWindowTitle('資格情報の選択')

        layout = QVBoxLayout(self)
        note = QLabel(
            '選択で返るのは資格情報のID（参照名）だけです。'
            '秘密の値はここでは表示・コピーされません。', self)
        note.setWordWrap(True)
        layout.addWidget(note)

        self.tree = QTreeWidget(self)
        self.tree.setAccessibleName('選択可能な資格情報')
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(
            ('資格情報ID', 'スコープ', '種別', 'ヒント'))
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(
            QTreeWidget.SelectionMode.SingleSelection)
        layout.addWidget(self.tree)

        self.status_label = QLabel(self)
        self.status_label.setWordWrap(True)
        self.status_label.setAccessibleName('選択状態')
        layout.addWidget(self.status_label)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, parent=self)
        self._buttons.button(
            QDialogButtonBox.StandardButton.Ok).setText('この参照を使う')
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

        self.tree.itemSelectionChanged.connect(self._sync_selection)
        self.tree.itemDoubleClicked.connect(
            lambda _item, _col: self._accept_if_valid())
        self._reload()
        self._sync_selection()

    def _reload(self) -> None:
        self.tree.clear()
        try:
            references = self._service.list_references(self._document_id)
        except Exception as exc:  # error-boundary: authority read
            self.status_label.setText(
                '資格情報の一覧を読めません: '
                + operation_error_message(exc))
            set_semantic_state(self.status_label, SemanticState.ERROR)
            return
        current = _current_references(references)
        for reference in current.values():
            if reference.state != 'active':
                continue  # revoked/deleted credentials are never bindable
            item = QTreeWidgetItem((
                reference.credential_id,
                _scope_text(reference),
                _CREDENTIAL_TYPE_LABELS.get(
                    reference.credential_type, reference.credential_type),
                reference.identity_hint or '',
            ))
            item.setData(
                0, Qt.ItemDataRole.UserRole, reference.credential_id)
            self.tree.addTopLevelItem(item)
        for col in range(self.tree.columnCount()):
            self.tree.resizeColumnToContents(col)

    def _selected_reference(self) -> CredentialReference | None:
        items = self.tree.selectedItems()
        if not items:
            return None
        credential_id = _reference_credential_id(items[0])
        if credential_id is None:
            return None
        return _current_references(
            self._service.list_references(self._document_id),
        ).get(credential_id)

    def _sync_selection(self) -> None:
        ok_button = self._buttons.button(
            QDialogButtonBox.StandardButton.Ok)
        reference = self._selected_reference()
        if reference is None:
            ok_button.setEnabled(False)
            self.status_label.setText('資格情報を選択してください')
            set_semantic_state(
                self.status_label, SemanticState.UNSUPPORTED)
            return
        if (
            self._expected_scope_kind is not None
            or self._expected_scope_ref is not None
        ):
            matches = (
                (
                    self._expected_scope_kind is None
                    or reference.scope_kind == self._expected_scope_kind
                )
                and (
                    self._expected_scope_ref is None
                    or reference.scope_ref == self._expected_scope_ref
                )
            )
            if not matches:
                expected = (
                    f'{self._expected_scope_kind or "*"}:'
                    f'{self._expected_scope_ref or "*"}')
                actual = (
                    f'{reference.scope_kind}:{reference.scope_ref}')
                ok_button.setEnabled(False)
                self.status_label.setText(
                    'スコープが一致しません — この対象には使えません'
                    f'（必要: {expected} / 選択: {actual}）。'
                    '対象に合う資格情報を選んでください')
                set_semantic_state(self.status_label, SemanticState.ERROR)
                return
        ok_button.setEnabled(True)
        self.status_label.setText(
            f'選択中: {reference.credential_id}')
        set_semantic_state(
            self.status_label, SemanticState.UNSUPPORTED)

    def _accept_if_valid(self) -> None:
        if self._buttons.button(
                QDialogButtonBox.StandardButton.Ok).isEnabled():
            self.accept()

    def accept(self) -> None:
        # Fail closed: a disabled OK means the selection is empty or the
        # scope drifted — never resolve to a credential_id anyway.
        ok_button = self._buttons.button(
            QDialogButtonBox.StandardButton.Ok)
        if not ok_button.isEnabled():
            return
        reference = self._selected_reference()
        if reference is None:
            return
        self._selected_credential_id = reference.credential_id
        super().accept()

    @property
    def selected_credential_id(self) -> str | None:
        """The picked credential ID — metadata only, never material."""
        return self._selected_credential_id


class CredentialVaultPanel(QWidget):
    """資格情報ヴォールトのオペレーターパネル (#951).

    Metadata-only list over ``CredentialReference`` rows plus the sealed
    lifecycle audit log. Mutations go through the dialogs above; every
    failure surfaces as an actionable message, and a locked/unavailable
    vault disables material operations while metadata stays readable.
    """

    def __init__(
        self,
        service: CredentialVaultService,
        document_id: str,
        *,
        on_status=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._document_id = document_id
        self._on_status = on_status

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.vault_label = QLabel(self)
        self.vault_label.setWordWrap(True)
        self.vault_label.setAccessibleName('ヴォールト状態')
        layout.addWidget(self.vault_label)

        self.consent_label = QLabel(self)
        self.consent_label.setWordWrap(True)
        self.consent_label.setAccessibleName('保管への同意')
        layout.addWidget(self.consent_label)

        self.reference_tree = QTreeWidget(self)
        self.reference_tree.setAccessibleName('資格情報一覧')
        self.reference_tree.setColumnCount(8)
        self.reference_tree.setHeaderLabels((
            '資格情報ID', 'スコープ', '種別', '保管範囲',
            '状態', '版', 'ヒント', '登録日時 (UTC)'))
        self.reference_tree.setToolTip(
            '登録済み資格情報の参照（非シークレットのメタデータ）です。'
            '秘密の値はここには表示されません。')
        self.reference_tree.setRootIsDecorated(False)
        self.reference_tree.setSelectionMode(
            QTreeWidget.SelectionMode.SingleSelection)
        layout.addWidget(self.reference_tree)

        mutations = QHBoxLayout()
        mutations.setSpacing(4)
        self.store_button = QPushButton('新規登録…', self)
        self.store_button.setAccessibleName('新規登録')
        self.store_button.clicked.connect(self._on_store)
        mutations.addWidget(self.store_button)

        self.rotate_button = QPushButton('ローテーション…', self)
        self.rotate_button.setAccessibleName('ローテーション')
        self.rotate_button.clicked.connect(self._on_rotate)
        mutations.addWidget(self.rotate_button)

        self.revoke_button = QPushButton('失効…', self)
        self.revoke_button.setAccessibleName('失効')
        self.revoke_button.clicked.connect(self._on_revoke)
        mutations.addWidget(self.revoke_button)

        self.delete_button = QPushButton('削除…', self)
        self.delete_button.setAccessibleName('削除')
        self.delete_button.clicked.connect(self._on_delete)
        mutations.addWidget(self.delete_button)
        mutations.addStretch(1)
        layout.addLayout(mutations)

        utilities = QHBoxLayout()
        utilities.setSpacing(4)
        self.verify_button = QPushButton('取得を検証', self)
        self.verify_button.setAccessibleName('取得を検証')
        self.verify_button.setToolTip(
            '選択した資格情報が今取得できるかを確認します。'
            '失効・削除・ロック中などは理由付きで表示されます。')
        self.verify_button.clicked.connect(self._on_verify)
        utilities.addWidget(self.verify_button)

        self.copy_id_button = QPushButton('参照IDをコピー', self)
        self.copy_id_button.setAccessibleName('参照IDをコピー')
        self.copy_id_button.setToolTip(
            '資格情報ID（参照名）をコピーします — 秘密の値ではありません。')
        self.copy_id_button.clicked.connect(self._on_copy_id)
        utilities.addWidget(self.copy_id_button)

        self.pick_button = QPushButton('参照を選択…', self)
        self.pick_button.setAccessibleName('参照を選択')
        self.pick_button.setToolTip(
            'アダプタ設定用に資格情報のIDを選択します（値は返しません）。')
        self.pick_button.clicked.connect(self._on_pick)
        utilities.addWidget(self.pick_button)

        self.copy_manifest_button = QPushButton(
            '監査マニフェストをコピー', self)
        self.copy_manifest_button.setAccessibleName('監査マニフェストをコピー')
        self.copy_manifest_button.setToolTip(
            '非シークレットのメタデータ一覧 (export_manifest) を'
            'JSONでコピーします。')
        self.copy_manifest_button.clicked.connect(self._on_copy_manifest)
        utilities.addWidget(self.copy_manifest_button)

        self.refresh_button = QPushButton('再読み込み', self)
        self.refresh_button.setAccessibleName('再読み込み')
        self.refresh_button.clicked.connect(self.refresh)
        utilities.addWidget(self.refresh_button)
        utilities.addStretch(1)
        layout.addLayout(utilities)

        self.result_label = QLabel(self)
        self.result_label.setWordWrap(True)
        self.result_label.setAccessibleName('操作結果')
        layout.addWidget(self.result_label)

        self.audit_tree = QTreeWidget(self)
        self.audit_tree.setAccessibleName('資格情報ライフサイクル監査ログ')
        self.audit_tree.setColumnCount(5)
        self.audit_tree.setHeaderLabels(
            ('記録時刻 (UTC)', 'イベント', 'アクター', '資格情報ID', '詳細'))
        self.audit_tree.setToolTip(
            '封緘されたライフサイクルイベント（同意・登録・回転・失効・'
            '削除・利用・取得拒否）です。詳細はメタデータのみで、'
            '秘密の値は記録されません。')
        self.audit_tree.setRootIsDecorated(False)
        layout.addWidget(self.audit_tree)

        self.reference_tree.itemSelectionChanged.connect(
            self._sync_mutation_enablement)
        self.refresh()

    # ------------------------------------------------------------------
    # reads

    def selected_credential_id(self) -> str | None:
        items = self.reference_tree.selectedItems()
        if not items:
            return None
        return _reference_credential_id(items[0])

    def _selected_reference(self) -> CredentialReference | None:
        credential_id = self.selected_credential_id()
        if credential_id is None:
            return None
        for reference in self._service.list_references(self._document_id):
            if reference.credential_id == credential_id:
                return reference
        return None

    def _vault_state(self) -> str:
        try:
            return self._service.vault_state()
        except Exception:  # error-boundary: backend probe
            return 'unavailable'

    def _current_reference(self) -> CredentialReference | None:
        """The selected credential's CURRENT row (max version) — stale
        active rows of a revoked/deleted credential must not enable
        mutations."""

        credential_id = self.selected_credential_id()
        if credential_id is None:
            return None
        return _current_references(
            self._service.list_references(self._document_id),
        ).get(credential_id)

    # ------------------------------------------------------------------
    # render

    def _sync_mutation_enablement(self) -> None:
        state = self._vault_state()
        unlocked = state == 'unlocked'
        reference = self._current_reference()
        active = reference is not None and reference.state == 'active'
        deletable = (
            reference is not None and reference.state != 'deleted')

        self.store_button.setEnabled(unlocked)
        self.rotate_button.setEnabled(unlocked and active)
        self.revoke_button.setEnabled(unlocked and active)
        self.delete_button.setEnabled(unlocked and deletable)
        self.verify_button.setEnabled(reference is not None)
        self.copy_id_button.setEnabled(reference is not None)

        if not unlocked:
            reason = (
                'ヴォールトがロック中です'
                if state == 'locked'
                else 'この環境では資格情報ヴォールトを利用できません')
            for button in (
                self.store_button, self.rotate_button,
                self.revoke_button, self.delete_button,
            ):
                button.setToolTip(reason)
        else:
            self.store_button.setToolTip(
                '新しい資格情報を登録します（初回は同意の記録が必要です）。')
            self.rotate_button.setToolTip(
                '選択した資格情報の値を新しい値に置き換えます。')
            self.revoke_button.setToolTip(
                '選択した資格情報を失効させます（以後は取得できません）。')
            self.delete_button.setToolTip(
                '選択した資格情報を削除します（保管庫の値も破棄します）。')

    def refresh(self) -> None:
        state = self._vault_state()
        if state == 'unlocked':
            self.vault_label.setText('ヴォールト状態: 利用可能')
            set_semantic_state(self.vault_label, SemanticState.SUCCESS)
        elif state == 'locked':
            self.vault_label.setText(
                'ヴォールト状態: ロック中 — OS の資格情報ストアが'
                'ロックされています。解除後に「再読み込み」してください。')
            set_semantic_state(self.vault_label, SemanticState.WARNING)
        else:
            self.vault_label.setText(
                'ヴォールト状態: この環境では利用不可 — 安全な資格情報'
                'ストア (Windows DPAPI 等) がないため、資格情報の保管・'
                '取得はできません。参照と監査ログは読み取りのみ可能です。')
            set_semantic_state(
                self.vault_label, SemanticState.UNSUPPORTED)

        try:
            has_consent = self._service.has_consent(self._document_id)
            references = self._service.list_references(self._document_id)
            events = self._service.list_events(self._document_id)
        except Exception as exc:  # error-boundary: authority read
            self.consent_label.setText('')
            self.reference_tree.clear()
            self.audit_tree.clear()
            self._report(
                '資格情報の一覧を読めません: '
                + operation_error_message(exc), ok=False)
            self._sync_mutation_enablement()
            return

        self.consent_label.setText(
            '保管への同意: 記録済み'
            if has_consent
            else '保管への同意: 未記録 — 初回の登録時に同意が必要です')
        set_semantic_state(
            self.consent_label,
            SemanticState.SUCCESS if has_consent
            else SemanticState.UNSUPPORTED)

        # Selection survives refresh — the credential the operator was
        # acting on stays selected (its newest row) after repopulating.
        previously_selected = self.selected_credential_id()
        self.reference_tree.clear()
        restored_item = None
        for reference in references:
            item = QTreeWidgetItem((
                reference.credential_id,
                _scope_text(reference),
                _CREDENTIAL_TYPE_LABELS.get(
                    reference.credential_type, reference.credential_type),
                _VAULT_SCOPE_LABELS.get(
                    reference.vault_scope, reference.vault_scope),
                _REFERENCE_STATE_LABELS.get(
                    reference.state, reference.state),
                str(reference.version),
                reference.identity_hint or '',
                reference.created_at_utc,
            ))
            item.setData(
                0, Qt.ItemDataRole.UserRole, reference.credential_id)
            item.setData(
                0, Qt.ItemDataRole.UserRole + 1,
                reference.reference_id)
            self.reference_tree.addTopLevelItem(item)
            if reference.credential_id == previously_selected:
                restored_item = item  # newest matching row wins
        for col in range(self.reference_tree.columnCount()):
            self.reference_tree.resizeColumnToContents(col)
        if restored_item is not None:
            self.reference_tree.setCurrentItem(restored_item)

        self.audit_tree.clear()
        for event in events:
            details = '、'.join(
                f'{key}={value}'
                for key, value in (event.details or {}).items())
            self.audit_tree.addTopLevelItem(QTreeWidgetItem((
                event.recorded_at_utc,
                _EVENT_KIND_LABELS.get(
                    event.event_kind, event.event_kind),
                event.actor,
                event.credential_id,
                details,
            )))
        for col in range(self.audit_tree.columnCount()):
            self.audit_tree.resizeColumnToContents(col)

        self._sync_mutation_enablement()

    # ------------------------------------------------------------------
    # mutations

    def _report(self, text: str, *, ok: bool) -> None:
        self.result_label.setText(text)
        set_semantic_state(
            self.result_label,
            SemanticState.SUCCESS if ok else SemanticState.ERROR)
        if self._on_status is not None:
            self._on_status(text)

    def _on_store(self) -> None:
        consent_needed = not self._service.has_consent(self._document_id)
        dialog = CredentialStoreDialog(
            consent_needed=consent_needed, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            if dialog.consent_requested:
                self._service.record_consent(
                    self._document_id, dialog.operator_id)
            reference = self._service.store_credential(
                self._document_id,
                scope_kind=dialog.scope_kind,
                scope_ref=dialog.scope_ref,
                credential_type=dialog.credential_type,
                material=SecretMaterial(dialog.take_secret()),
                actor=dialog.operator_id,
                vault_scope=dialog.vault_scope,
                identity_hint=dialog.identity_hint,
            )
        except Exception as exc:  # error-boundary: service call
            self._report(
                '資格情報を登録できませんでした: '
                + operation_error_message(exc), ok=False)
        else:
            self._report(
                f'資格情報 {reference.credential_id} を登録しました',
                ok=True)
        self.refresh()

    def _on_rotate(self) -> None:
        reference = self._current_reference()
        if reference is None:
            return
        dialog = CredentialRotateDialog(reference, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            rotated = self._service.rotate(
                reference.credential_id,
                material=SecretMaterial(dialog.take_secret()),
                actor=dialog.operator_id,
            )
        except Exception as exc:  # error-boundary: service call
            self._report(
                'ローテーションできませんでした: '
                + operation_error_message(exc), ok=False)
        else:
            self._report(
                f'{rotated.credential_id} を版 {rotated.version} '
                'に更新しました', ok=True)
        self.refresh()

    def _on_revoke(self) -> None:
        self._confirm_lifecycle('revoke')

    def _on_delete(self) -> None:
        self._confirm_lifecycle('delete')

    def _confirm_lifecycle(self, action: str) -> None:
        reference = self._current_reference()
        if reference is None:
            return
        dialog = CredentialConfirmDialog(
            reference, action=action, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            if action == 'revoke':
                outcome = self._service.revoke(
                    reference.credential_id, actor=dialog.operator_id)
                verb = '失効させました'
            else:
                outcome = self._service.delete(
                    reference.credential_id, actor=dialog.operator_id)
                verb = '削除しました'
        except Exception as exc:  # error-boundary: service call
            self._report(
                '操作できませんでした: '
                + operation_error_message(exc), ok=False)
        else:
            self._report(
                f'{outcome.credential_id} を{verb}', ok=True)
        self.refresh()

    # ------------------------------------------------------------------
    # verification / metadata copy-out (IDs and manifest only)

    def _on_verify(self) -> None:
        """Probe retrieval of the selected credential — success reports
        only the non-secret fingerprint; failures surface the vault's
        actionable message verbatim (missing/revoked/deleted/locked)."""
        credential_id = self.selected_credential_id()
        if credential_id is None:
            return
        try:
            material = self._service.retrieve(
                credential_id, actor='operator-ui')
        except Exception as exc:  # error-boundary: service call
            self._report(
                '取得できません: '
                + operation_error_message(exc), ok=False)
        else:
            self._report(
                f'{credential_id} は取得可能です '
                f'(fingerprint {material.fingerprint()[:12]}…)',
                ok=True)
        self.refresh()

    def _on_copy_id(self) -> None:
        credential_id = self.selected_credential_id()
        if credential_id is None:
            return
        QApplication.clipboard().setText(credential_id)
        self._report(
            f'参照ID {credential_id} をコピーしました', ok=True)

    def _on_pick(self) -> None:
        dialog = CredentialReferencePickerDialog(
            self._service, self._document_id, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._report(
            '選択した参照ID: '
            f'{dialog.selected_credential_id}', ok=True)

    def _on_copy_manifest(self) -> None:
        try:
            manifest = self._service.export_manifest(self._document_id)
        except Exception as exc:  # error-boundary: service call
            self._report(
                'マニフェストを生成できませんでした: '
                + operation_error_message(exc), ok=False)
            return
        QApplication.clipboard().setText(
            json.dumps(
                list(manifest), ensure_ascii=False, indent=2))
        self._report(
            f'監査マニフェスト ({len(manifest)} 件) をコピーしました',
            ok=True)


class CredentialVaultDialog(QDialog):
    """資格情報ヴォールトをモーダルで開くホスト (#951).

    The Support destination opens this for the current document — the
    vault surface lives inside the existing サポート context, not as a
    new top-level workspace.
    """

    def __init__(
        self,
        service: CredentialVaultService,
        document_id: str,
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setModal(True)
        self.setWindowTitle('資格情報ヴォールト')
        self.resize(860, 620)
        layout = QVBoxLayout(self)
        note = QLabel(
            'プロジェクトに紐付く資格情報の管理画面です。ここで表示・'
            'コピーされるのは参照IDとメタデータだけで、秘密の値は'
            '表示されません。', self)
        note.setWordWrap(True)
        layout.addWidget(note)
        self.panel = CredentialVaultPanel(
            service, document_id, parent=self)
        layout.addWidget(self.panel, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Close, parent=self)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


__all__ = [
    'CredentialConfirmDialog',
    'CredentialReferencePickerDialog',
    'CredentialRotateDialog',
    'CredentialStoreDialog',
    'CredentialVaultDialog',
    'CredentialVaultPanel',
]
