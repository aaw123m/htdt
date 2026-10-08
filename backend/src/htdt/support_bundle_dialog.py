"""Preview dialog for the #884 diagnostic support bundle.

Shows the exact export content — every member with its privacy
classification, byte size, sha256 and redaction count — BEFORE the user
chooses a destination. The preview is produced by the builder's staging
pass, so what is shown is byte-identical to what will be written; nothing
leaves the machine until the user confirms.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .support_diagnostics import (
    BundlePreview,
    FieldClassification,
    PackageCategory,
)

_CLASSIFICATION_JA = {
    FieldClassification.SAFE_DIAGNOSTIC: '安全(診断用)',
    FieldClassification.PROJECT_METADATA: 'プロジェクト情報(要同意)',
    FieldClassification.SENSITIVE_PROJECT_DATA: '機微データ(既定で除外)',
    FieldClassification.SECRET: '秘密情報(出力不可)',
}

_CATEGORY_JA = {
    PackageCategory.LOGS: 'ログ',
    PackageCategory.BUILD_IDENTITY: 'ビルド識別情報',
    PackageCategory.SCHEMA_SUMMARY: 'スキーマ概要',
    PackageCategory.HEALTH_RESULTS: 'ヘルスチェック結果',
    PackageCategory.OPERATION_FAILURES: '失敗した操作',
    PackageCategory.CAPABILITY_INVENTORY: '機能提供一覧',
    PackageCategory.PREFERENCES_SUMMARY: '設定サマリ',
    PackageCategory.PROJECT_IDS: 'プロジェクトID',
    PackageCategory.LAUNCH_METADATA: '起動履歴',
    PackageCategory.RUNTIME_CONTEXT: 'ランタイム環境',
    PackageCategory.GPU_CONTEXT: 'GPU/グラフィックス',
    PackageCategory.AUDIO_CONTEXT: 'オーディオバックエンド',
    PackageCategory.ADAPTER_CAPABILITY: 'デバイスアダプタ能力',
    PackageCategory.WORKFLOW_STATE: 'ワークフロー状態',
    PackageCategory.RELEASE_EVIDENCE: 'リリース検証証跡',
    PackageCategory.MEASUREMENT_ENGINE_STATUS: '測定エンジン状態',
    PackageCategory.DEVICE_TRANSACTION_STATUS: 'デバイストランザクション',
}


def _fmt_bytes(value: int) -> str:
    if value >= 1 << 20:
        return f'{value / (1 << 20):.1f} MiB'
    if value >= 1 << 10:
        return f'{value / (1 << 10):.1f} KiB'
    return f'{value} B'


class SupportBundlePreviewDialog(QDialog):
    """Exact-content preview before export (#884 acceptance criterion)."""

    def __init__(
        self,
        preview: BundlePreview,
        *,
        include_project_ids: bool,
        project_ids_available: bool,
        restage: Callable[[bool], BundlePreview] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle('診断バンドルのプレビュー')
        self.setModal(True)
        self.resize(720, 560)
        self._preview = preview
        self._restage = restage

        layout = QVBoxLayout(self)

        summary = QLabel(
            'エクスポートされる内容を確認してください。'
            '一覧は実際に書き出される内容と完全に一致します。'
            '機微なデータは既定で除外され、すべての項目は分類されています。'
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)

        self._tree = QTreeWidget(self)
        self._tree.setColumnCount(4)
        self._tree.setHeaderLabels(['内容', '分類', 'サイズ', '状態'])
        self._tree.setRootIsDecorated(False)
        self._tree.setAccessibleName('診断バンドル内容プレビュー')
        layout.addWidget(self._tree, stretch=1)
        self._populate_members(preview)

        self._excluded_label = QLabel()
        self._excluded_label.setWordWrap(True)
        layout.addWidget(self._excluded_label)
        self._populate_excluded(preview)

        always_excluded = QLabel(
            '常に除外: 生測定音声・部屋ジオメトリ・シリアル番号・'
            'ユーザーメモ・資格情報/トークン・秘密鍵'
        )
        always_excluded.setWordWrap(True)
        layout.addWidget(always_excluded)

        self._errors_label = QLabel()
        self._errors_label.setWordWrap(True)
        layout.addWidget(self._errors_label)
        self._populate_errors(preview)

        self._project_ids_check = QCheckBox(
            'プロジェクト ID・表示名を含める(サポート依頼時のみ)', self
        )
        self._project_ids_check.setChecked(include_project_ids)
        self._project_ids_check.setEnabled(project_ids_available)
        if not project_ids_available:
            self._project_ids_check.setToolTip('プロジェクトが存在しません')
        self._project_ids_check.setAccessibleName(
            'プロジェクトIDを含めるオプション'
        )
        # The preview must stay byte-exact: toggling re-stages the bundle.
        self._project_ids_check.toggled.connect(self._on_project_ids_toggled)
        layout.addWidget(self._project_ids_check)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            'エクスポート…'
        )
        self._buttons.button(
            QDialogButtonBox.StandardButton.Cancel
        ).setText('キャンセル')
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        for button in self._buttons.buttons():
            button.setAccessibleName(button.text())
        layout.addWidget(self._buttons)

    @property
    def include_project_ids(self) -> bool:
        return self._project_ids_check.isChecked()

    @property
    def preview(self) -> BundlePreview:
        """The staged preview — byte-identical to what will be written."""
        return self._preview

    def _on_project_ids_toggled(self, checked: bool) -> None:
        if self._restage is None:
            return
        self._preview = self._restage(checked)
        self._populate_members(self._preview)
        self._populate_excluded(self._preview)
        self._populate_errors(self._preview)

    def _populate_members(self, preview: BundlePreview) -> None:
        self._tree.clear()
        for member in preview.members:
            redactions = sum(member.redactions.values())
            state = member.status
            if redactions:
                state += f' · 秘匿 {redactions} 箇所'
            item = QTreeWidgetItem(
                [
                    member.name,
                    _CLASSIFICATION_JA.get(
                        member.classification, member.classification.value
                    ),
                    _fmt_bytes(member.included_bytes),
                    state,
                ]
            )
            if member.sha256:
                item.setToolTip(0, f'sha256: {member.sha256}')
            self._tree.addTopLevelItem(item)
        for column in range(4):
            self._tree.resizeColumnToContents(column)

    def _populate_excluded(self, preview: BundlePreview) -> None:
        if preview.excluded_categories:
            self._excluded_label.setText(
                '除外されるカテゴリ: '
                + '、'.join(
                    _CATEGORY_JA.get(c, c.value)
                    for c in preview.excluded_categories
                )
            )
            self._excluded_label.show()
        else:
            self._excluded_label.hide()

    def _populate_errors(self, preview: BundlePreview) -> None:
        if preview.collection_errors:
            self._errors_label.setText(
                '収集エラー(バンドルには理由のみ記録): '
                + '、'.join(preview.collection_errors)
            )
            self._errors_label.show()
        else:
            self._errors_label.hide()


__all__ = ['SupportBundlePreviewDialog']
