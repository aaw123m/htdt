"""Settings → Capture panel: receiver on/off, port, pairing via QR (#926).

The panel owns no policy of its own — the requested state lives in the
``integrations.capture_receiver_enabled`` ApplicationPreference, applied
through :class:`CaptureReceiverController`, while durable receiver
identity/network config stays in ``capture_receiver_config``.
"""

from __future__ import annotations

import json

import segno
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .accessible_labels import announce_status, wire_label_buddies
from .capture_receiver import ReceiverPairing, ReceiverPairingPayload
from .capture_receiver_controller import CaptureReceiverController
from .user_facing_error import operation_error_message


def _qr_pixmap(text: str, *, module: int = 6, quiet: int = 4) -> QPixmap:
    qr = segno.make(text, error='m', micro=False)
    matrix = qr.matrix
    n = len(matrix)
    size = (n + quiet * 2) * module
    image = QImage(size, size, QImage.Format.Format_RGB32)
    image.fill(QColor('#ffffff'))
    dark = QColor('#000000').rgb()
    for row_index, row in enumerate(matrix):
        for col_index, cell in enumerate(row):
            if not cell:
                continue
            for dy in range(module):
                for dx in range(module):
                    image.setPixel(
                        (quiet + col_index) * module + dx,
                        (quiet + row_index) * module + dy,
                        dark,
                    )
    return QPixmap.fromImage(image)


_PAIRING_SCOPE_THIS_PROJECT = 'project'
_PAIRING_SCOPE_UNASSIGNED = 'unassigned'


class PairingDialog(QDialog):
    """Pair-a-device dialog: QR offer + confirmation + revoke."""

    def __init__(
        self,
        controller: CaptureReceiverController,
        project_ref: str | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._project_ref = project_ref
        self._pairing: ReceiverPairing | None = None
        self._payload: ReceiverPairingPayload | None = None
        self.setWindowTitle("デバイスのペアリング")
        self.setModal(True)
        layout = QVBoxLayout(self)

        offer_form = QFormLayout()
        self.display_name_edit = QLineEdit(self)
        self.display_name_edit.setPlaceholderText("例: 測定用タブレット")
        offer_form.addRow("デバイス名", self.display_name_edit)
        self.scope_combo = QComboBox(self)
        self.scope_combo.addItem(
            "このプロジェクトへ割り当て", _PAIRING_SCOPE_THIS_PROJECT
        )
        self.scope_combo.addItem(
            "後で割り当て（受信ボックス未割当）", _PAIRING_SCOPE_UNASSIGNED
        )
        if project_ref is None:
            self.scope_combo.removeItem(0)
        offer_form.addRow("取り込み先", self.scope_combo)
        layout.addLayout(offer_form)

        offer_row = QHBoxLayout()
        self.offer_button = QPushButton("QRコードを発行", self)
        self.offer_button.clicked.connect(self._issue_offer)
        offer_row.addWidget(self.offer_button)
        offer_row.addStretch(1)
        layout.addLayout(offer_row)

        self.qr_label = QLabel(
            "Capture アプリでスキャンするQRコードを発行します。", self
        )
        self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_label.setMinimumHeight(240)
        layout.addWidget(self.qr_label, 1)

        self.confirm_row = QHBoxLayout()
        self.confirm_row.addWidget(QLabel("確認コード", self))
        self.confirm_code_edit = QLineEdit(self)
        self.confirm_code_edit.setPlaceholderText("アプリに表示されたコード")
        self.confirm_row.addWidget(self.confirm_code_edit, 1)
        self.confirm_button = QPushButton("確認", self)
        self.confirm_button.clicked.connect(self._confirm)
        self.confirm_row.addWidget(self.confirm_button)
        layout.addLayout(self.confirm_row)

        self.status_label = QLabel("", self)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        layout.addWidget(QLabel("ペアリング済みデバイス", self))
        self.pairing_list = QListWidget(self)
        self.pairing_list.setMinimumHeight(120)
        layout.addWidget(self.pairing_list)
        self.revoke_button = QPushButton("選択したデバイスを解除", self)
        self.revoke_button.clicked.connect(self._revoke_selected)
        layout.addWidget(self.revoke_button)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._refresh_pairings()
        wire_label_buddies(self)

    def _set_status(self, message: str) -> None:
        # The status line updates asynchronously on pairing actions —
        # announce it or screen readers never hear the outcome.
        self.status_label.setText(message)
        announce_status(self, message)

    # -- offer ---------------------------------------------------------------

    def _issue_offer(self) -> None:
        project_ref = (
            self._project_ref
            if self.scope_combo.currentData() == _PAIRING_SCOPE_THIS_PROJECT
            else None
        )
        display_name = self.display_name_edit.text().strip() or None
        try:
            pairing, payload = self._controller.service.begin_pairing(
                project_ref=project_ref,
                display_name=display_name,
            )
        except Exception as exc:
            QMessageBox.warning(
                self, "ペアリング",
                f"QRコードを発行できませんでした · {operation_error_message(exc)}"
            )
            return
        self._pairing = pairing
        self._payload = payload
        qr_text = json.dumps(
            payload.model_dump(mode='json', by_alias=True),
            separators=(',', ':'),
            sort_keys=True,
        )
        pixmap = _qr_pixmap(qr_text)
        self.qr_label.setPixmap(pixmap)
        self._set_status(
            f"Capture アプリでスキャンしてください。"
            f"期限: {payload.expires_at or '—'} · "
            f"確認コードをアプリ側と照合して「確認」を押します。"
        )
        self._refresh_pairings()

    def _confirm(self) -> None:
        if self._pairing is None:
            self._set_status("先にQRコードを発行してください。")
            return
        expected = self._pairing.confirmation_code
        entered = self.confirm_code_edit.text().strip()
        if expected:
            if not entered:
                self._set_status("確認コードを入力してください。")
                return
            if expected != entered:
                self._set_status("確認コードが一致しません。")
                return
        try:
            pairing = self._controller.service.confirm_pairing(
                self._pairing.pairing_id
            )
        except Exception as exc:
            self._set_status(f"確認できませんでした · {operation_error_message(exc)}")
            return
        self._pairing = pairing
        self._set_status("ペアリングを確定しました。")
        self._refresh_pairings()

    def _revoke_selected(self) -> None:
        item = self.pairing_list.currentItem()
        if item is None:
            self._set_status("解除するデバイスを選択してください。")
            return
        pairing_id = item.data(Qt.ItemDataRole.UserRole)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("ペアリングの解除")
        box.setText(f"「{item.text()}」のペアリングを解除します。")
        box.setInformativeText(
            "解除すると、このデバイスからの新しい取り込みは受け付けなくなります。"
        )
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        try:
            self._controller.service.revoke_pairing(pairing_id)
        except Exception as exc:
            self._set_status(f"解除できませんでした · {operation_error_message(exc)}")
            return
        self._set_status("デバイスのペアリングを解除しました。")
        self._refresh_pairings()

    def _refresh_pairings(self) -> None:
        self.pairing_list.clear()
        try:
            pairings = self._controller.service.list_pairings()
        except Exception:
            pairings = ()
        state_labels = {
            'offered': '発行済み（未確認）',
            'active': '有効',
            'revoked': '解除済み',
            'expired': '期限切れ',
        }
        for pairing in pairings:
            scope = pairing.project_ref or '受信ボックス（未割当）'
            label = (
                f"{pairing.receiver_instance_id[:8]}… · "
                f"{state_labels.get(pairing.state, pairing.state)} · {scope}"
            )
            item = QListWidgetItem(label, self.pairing_list)
            item.setData(Qt.ItemDataRole.UserRole, pairing.pairing_id)


class CaptureReceiverPanel(QWidget):
    """Settings → キャプチャ tab body: policy toggle, port, pairing, status."""

    def __init__(
        self,
        controller: CaptureReceiverController,
        project_ref_provider,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._project_ref_provider = project_ref_provider

        layout = QVBoxLayout(self)
        intro = QLabel(
            "HTDT Capture アプリからの測定パッケージをこのPCで受け取ります。"
            "受け取ったデータは取り込み前に必ず受信ボックスに入ります。",
            self,
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        toggle_row = QHBoxLayout()
        toggle_row.addWidget(QLabel("受信を有効にする", self))
        self.enabled_combo = QComboBox(self)
        self.enabled_combo.addItem("無効", False)
        self.enabled_combo.addItem("有効", True)
        toggle_row.addWidget(self.enabled_combo)
        self.apply_button = QPushButton("適用", self)
        self.apply_button.clicked.connect(self._apply_enabled)
        toggle_row.addWidget(self.apply_button)
        toggle_row.addStretch(1)
        layout.addLayout(toggle_row)

        port_row = QHBoxLayout()
        port_row.addWidget(QLabel("待受ポート", self))
        self.port_spin = QSpinBox(self)
        self.port_spin.setRange(1, 65535)
        port_row.addWidget(self.port_spin)
        self.port_button = QPushButton("ポートを変更", self)
        self.port_button.clicked.connect(self._apply_port)
        port_row.addWidget(self.port_button)
        port_row.addStretch(1)
        layout.addLayout(port_row)

        action_row = QHBoxLayout()
        self.pair_button = QPushButton("デバイスをペアリング…", self)
        self.pair_button.clicked.connect(self._open_pairing)
        action_row.addWidget(self.pair_button)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        self.status_label = QLabel("", self)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.error_label = QLabel("", self)
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)
        layout.addStretch(1)

        controller.changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        controller = self._controller
        index = self.enabled_combo.findData(controller.requested_enabled)
        if index >= 0:
            self.enabled_combo.blockSignals(True)
            self.enabled_combo.setCurrentIndex(index)
            self.enabled_combo.blockSignals(False)
        try:
            self.port_spin.setValue(controller.service.get_config().port)
        except Exception:
            pass
        lines = controller.status_lines()
        if lines:
            self.status_label.setText(' · '.join(lines))
        self.error_label.setText(
            "受信を開始できませんでした。ポートを変更するか、"
            f"受信を無効にしてください。詳細: {controller.last_error}"
            if controller.last_error
            else ""
        )

    def _apply_enabled(self) -> None:
        enabled = bool(self.enabled_combo.currentData())
        error = self._controller.set_enabled(enabled)
        if error:
            QMessageBox.warning(
                self,
                "キャプチャ受信",
                "受信を開始できませんでした。プロジェクトは引き続き使えます。\n"
                f"{error}",
            )
        self.refresh()

    def _apply_port(self) -> None:
        error = self._controller.set_port(int(self.port_spin.value()))
        if error:
            QMessageBox.warning(
                self,
                "キャプチャ受信",
                f"ポートを変更できませんでした。\n{error}",
            )
        self.refresh()

    def _open_pairing(self) -> None:
        try:
            project_ref = self._project_ref_provider()
        except Exception:
            project_ref = None
        dialog = PairingDialog(self._controller, project_ref, self)
        dialog.exec()
        self.refresh()
