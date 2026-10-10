"""Export preflight dialog — human review before anything leaves (#989).

Shows, per outgoing element: kind, size, sensitivity classification,
redistribution rights, risk flags and the send/no-send selection —
split into 完全再現用（非公開保存） and 外部レビュー用（最小限の項目）.

* Private scope ships the full content: the review is informational
  (the operator still sees what sensitive data the archive carries).
* External scope builds an allowlist: elements whose classification or
  rights are unconfirmed, secret-bearing or license-restricted stay
  excluded and cannot be toggled in — the only way to send an element
  is to confirm its class and rights here, which the caller persists
  as a sealed ``ProjectDataClassification``. Exclusion is never silent:
  excluded elements list their reason and land in the manifest's
  ``excluded_refs`` plus the bundle's ``omissions``.
* 「保留」 defers without persisting anything; 「キャンセル」 likewise —
  the source data is never mutated by closing this dialog.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QHBoxLayout,
)

from .export_preflight import (
    PreflightElement,
    PreflightPlan,
    PreflightScope,
    class_label,
    export_kind_label,
    lock_ineligible,
    risk_label,
    rights_label,
    scope_label,
)
from .ui_theme import SemanticState, TypographyRole, set_semantic_state, set_typography_role


_CLASS_CHOICES: tuple[str, ...] = (
    'public_shareable',
    'internal_project',
    'client_confidential',
    'personal_pii',
    'security_sensitive',
    'proprietary_license_restricted',
    'safety_engineering_restricted',
    'credential_or_secret',
    'unknown_classification',
)

_RIGHTS_CHOICES: tuple[str, ...] = (
    'undeclared',
    'open',
    'reference_only',
    'proprietary',
    'licensed_no_redistribution',
)

_COLS = ('種類', '要素', 'サイズ', 'リスク', 'データ区分', '権利', '判定', '送付')


def _bytes_label(count: int) -> str:
    value = float(count)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if value < 1024.0 or unit == 'GB':
            return f'{value:.1f} {unit}' if unit != 'B' else f'{count} B'
        value /= 1024.0
    return f'{count} B'


class _ElementRow:
    """Widgets bound to one ``PreflightElement`` row."""

    def __init__(
        self,
        element: PreflightElement,
        dialog: 'ExportPreflightDialog',
    ) -> None:
        self.element = element
        self.dialog = dialog

        self.class_combo = QComboBox()
        self.class_combo.setAccessibleName(f'データ区分 {element.element_id}')
        for data_class in _CLASS_CHOICES:
            self.class_combo.addItem(class_label(data_class), data_class)
        initial_class = (
            element.stored.data_class
            if element.stored is not None
            else element.proposed_class
        )
        index = self.class_combo.findData(initial_class)
        self.class_combo.setCurrentIndex(max(index, 0))
        self.class_combo.currentIndexChanged.connect(dialog._refresh_verdicts)

        self.rights_combo = QComboBox()
        self.rights_combo.setAccessibleName(f'権利 {element.element_id}')
        for rights in _RIGHTS_CHOICES:
            self.rights_combo.addItem(rights_label(rights), rights)
        initial_rights = (
            element.stored.rights_class
            if element.stored is not None
            else 'undeclared'
        )
        index = self.rights_combo.findData(initial_rights)
        self.rights_combo.setCurrentIndex(max(index, 0))
        self.rights_combo.currentIndexChanged.connect(dialog._refresh_verdicts)

        self.include_check = QCheckBox()
        self.include_check.setAccessibleName(f'送付 {element.element_id}')
        self.include_check.setChecked(element.include)
        self.include_check.stateChanged.connect(dialog._refresh_counts)


class ExportPreflightDialog(QDialog):
    """Review per-element sensitivity and send scope before export."""

    def __init__(
        self,
        plan: PreflightPlan,
        *,
        title: str,
        default_scope: PreflightScope = 'external_review',
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.plan = plan
        self._rows: list[_ElementRow] = []

        self.setWindowTitle('出力前の機密データ検査')
        self.resize(980, 620)
        self.setMinimumSize(640, 420)
        layout = QVBoxLayout(self)

        heading = QLabel(f'{title} — {export_kind_label(plan.export_kind)}')
        set_typography_role(heading, TypographyRole.SECTION_TITLE)
        layout.addWidget(heading)

        pin_parts = []
        if plan.source_revision_id:
            pin_parts.append(f'ソースリビジョン: {plan.source_revision_id[:12]}')
        if plan.source_sha256:
            pin_parts.append(f'SHA: {plan.source_sha256[:16]}…')
        self._pin_label = QLabel(' / '.join(pin_parts) or 'ソース権威: なし')
        set_typography_role(self._pin_label, TypographyRole.SECONDARY)
        layout.addWidget(self._pin_label)

        # --- Purpose selection -------------------------------------------
        purpose_row = QHBoxLayout()
        purpose_row.addWidget(QLabel('用途:'))
        self._scope_private = QRadioButton(scope_label('private_archive'))
        self._scope_external = QRadioButton(scope_label('external_review'))
        self._scope_private.setAccessibleName('完全再現用（非公開保存）')
        self._scope_external.setAccessibleName('外部レビュー用（最小限の項目）')
        (
            self._scope_external if default_scope == 'external_review'
            else self._scope_private
        ).setChecked(True)
        self._scope_private.toggled.connect(self._refresh_verdicts)
        self._scope_external.toggled.connect(self._refresh_verdicts)
        purpose_row.addWidget(self._scope_private)
        purpose_row.addWidget(self._scope_external)
        purpose_row.addStretch(1)
        layout.addLayout(purpose_row)

        self._scope_hint = QLabel()
        self._scope_hint.setWordWrap(True)
        set_typography_role(self._scope_hint, TypographyRole.SECONDARY)
        layout.addWidget(self._scope_hint)

        # --- Element table -------------------------------------------------
        self._table = QTableWidget(len(plan.elements), len(_COLS))
        self._table.setHorizontalHeaderLabels(_COLS)
        self._table.verticalHeader().setVisible(False)
        self._table.setAccessibleName('出力要素の検査一覧')
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        for row_index, element in enumerate(plan.elements):
            row = _ElementRow(element, self)
            self._rows.append(row)
            self._fill_row(row_index, row)
        self._table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        for column in (0, 2, 3, 4, 5, 6, 7):
            self._table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        self._table.setMinimumHeight(240)
        layout.addWidget(self._table, 1)

        # --- Footer: counts + degraded warning + buttons --------------------
        self._counts_label = QLabel()
        self._counts_label.setAccessibleName('出力件数・容量')
        layout.addWidget(self._counts_label)

        self._degraded_label = QLabel()
        self._degraded_label.setWordWrap(True)
        self._degraded_label.setAccessibleName('再現性の劣化警告')
        set_semantic_state(self._degraded_label, SemanticState.WARNING)
        self._degraded_label.hide()
        layout.addWidget(self._degraded_label)

        self._blocked_label = QLabel()
        self._blocked_label.setWordWrap(True)
        self._blocked_label.setAccessibleName('出力不可の理由')
        set_semantic_state(self._blocked_label, SemanticState.WARNING)
        self._blocked_label.hide()
        layout.addWidget(self._blocked_label)

        buttons = QDialogButtonBox(parent=self)
        self._export_button = buttons.addButton(
            '出力する', QDialogButtonBox.ButtonRole.AcceptRole
        )
        self._export_button.setAccessibleName('この内容で出力する')
        hold_button = buttons.addButton(
            '保留', QDialogButtonBox.ButtonRole.RejectRole
        )
        hold_button.setAccessibleName('保留（何も保存しない）')
        cancel_button = buttons.addButton(
            'キャンセル', QDialogButtonBox.ButtonRole.RejectRole
        )
        cancel_button.setAccessibleName('キャンセル（元データ不変）')
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._refresh_verdicts()

    # -- helpers -----------------------------------------------------------

    def _fill_row(self, row_index: int, row: _ElementRow) -> None:
        element = row.element
        table = self._table

        kind_item = QTableWidgetItem(element.label)
        kind_item.setToolTip(element.element_id)
        table.setItem(row_index, 0, kind_item)

        detail_item = QTableWidgetItem(element.detail)
        detail_item.setToolTip(element.element_id)
        table.setItem(row_index, 1, detail_item)

        size_item = QTableWidgetItem(_bytes_label(element.size_bytes))
        size_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        table.setItem(row_index, 2, size_item)

        risk_text = '、'.join(risk_label(flag) for flag in element.risk_flags) or '—'
        risk_item = QTableWidgetItem(risk_text)
        table.setItem(row_index, 3, risk_item)

        table.setCellWidget(row_index, 4, row.class_combo)
        table.setCellWidget(row_index, 5, row.rights_combo)

        verdict_item = QTableWidgetItem('')
        table.setItem(row_index, 6, verdict_item)

        table.setCellWidget(row_index, 7, row.include_check)

    def scope(self) -> PreflightScope:
        return (
            'external_review'
            if self._scope_external.isChecked()
            else 'private_archive'
        )

    def _refresh_verdicts(self) -> None:
        """Re-evaluate every row against the current scope + combos."""
        external = self.scope() == 'external_review'
        self._scope_hint.setText(
            '外部送付を意図した出力です — 各要素の区分・権利を確認し、'
            '送付する項目だけを選択してください。未確定・秘密・再配布不可'
            'の項目は送付できません。'
            if external
            else '非公開保存用の完全出力です — 内容はすべて含まれます。'
            '含まれる機密データの内訳を確認してください。'
        )
        for row_index, row in enumerate(self._rows):
            element = row.element
            element.confirmed_class = row.class_combo.currentData()
            element.confirmed_rights = row.rights_combo.currentData()
            lock = lock_ineligible(element) if external else None
            element.locked = external and lock is not None
            element.lock_reason = lock
            if external:
                row.include_check.setEnabled(not element.locked)
                if element.locked:
                    row.include_check.setChecked(False)
                elif not row.include_check.isChecked() and lock is None:
                    # Keep user choice; default stays as it was.
                    pass
                verdict = (
                    f'除外: {lock}'
                    if element.locked
                    else '送付可'
                )
            else:
                row.include_check.setEnabled(False)
                row.include_check.setChecked(True)
                verdict = '含む'
            element.include = row.include_check.isChecked()
            verdict_item = self._table.item(row_index, 6)
            if verdict_item is not None:
                verdict_item.setText(verdict)
                verdict_item.setToolTip(element.lock_reason or '')
        self._refresh_counts()

    def _refresh_counts(self) -> None:
        included = [
            row.element
            for row in self._rows
            if row.include_check.isChecked()
        ]
        excluded = [
            row.element
            for row in self._rows
            if not row.include_check.isChecked()
        ]
        total = sum(element.size_bytes for element in included)
        self._counts_label.setText(
            f'送付 {len(included)} 件 / 除外 {len(excluded)} 件 '
            f'— 合計 {_bytes_label(total)} '
            f'（全 {len(self._rows)} 件）'
        )
        self.plan.elements = [row.element for row in self._rows]
        for row in self._rows:
            row.element.include = row.include_check.isChecked()

        external = self.scope() == 'external_review'
        if external and excluded:
            self._degraded_label.setText(
                '除外項目があるため、この出力では再現性が低下します'
                '（incomplete/degraded — 除外理由はマニフェストと'
                '検査レコードに記録されます）。'
            )
            self._degraded_label.show()
        else:
            self._degraded_label.hide()

        blocked_reasons = []
        if external and not included:
            blocked_reasons.append(
                '外部送付できる項目がありません — 区分・権利を確認するか、'
                '完全再現用（非公開保存）を選んでください。'
            )
        locked = [row.element for row in self._rows if row.element.locked]
        if external and locked:
            blocked_reasons.append(
                f'{len(locked)} 件は未確定・秘密・再配布不可のため'
                '送付できません（除外として記録されます）。'
            )
        if blocked_reasons:
            self._blocked_label.setText('\n'.join(blocked_reasons))
            self._blocked_label.show()
        else:
            self._blocked_label.hide()
        self._export_button.setEnabled(bool(included))

    def _accept(self) -> None:
        for row in self._rows:
            row.element.include = row.include_check.isChecked()
            row.element.confirmed_class = row.class_combo.currentData()
            row.element.confirmed_rights = row.rights_combo.currentData()
        self.accept()


__all__ = ['ExportPreflightDialog']
