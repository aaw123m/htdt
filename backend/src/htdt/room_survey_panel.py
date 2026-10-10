"""Qt panel for the as-built survey overlay (#1004 / REV73).

The 3D actors stay ``pickable=False`` (overlay convention), so this dock
panel is the click target the issue requires: one click on an element row
reaches the full authority bundle — campaigns, instruments (kind /
capability / app+version / calibration refs), sealed evaluation entry,
source hashes, blocked task verdicts with their reasons, reconciliations,
re-check candidates, and downstream invalidated artifacts. Blocked-task
reasons are one click under ブロック中のタスク.

The panel only renders what ``resolve_survey_overlay`` already resolved —
it never re-derives state, so a stale authority can never be shown next
to a current viewport.

``modeChanged`` carries 'off' or one of ``SURVEY_OVERLAY_MODES``; the
workspace mirrors it into the OverlayControls 測量 group.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    QTextBrowser,
)

from .room_survey_overlay import (
    MODE_LABELS,
    SURVEY_OVERLAY_MODES,
    UNMAPPED_REASON_LABELS,
    VERIFICATION_LABELS,
)
from .cad_geometry_survey import (
    REASON_LABELS,
    TASK_VERDICT_LABELS,
)
from .ui_theme import TypographyRole, set_typography_role


_CONTROL_BUCKET_LABELS = {
    'pass': '検査合格 (独立)',
    'fail': '検査不合格',
    'evidence_only': '証跡のみ (許容差なし)',
    'reg_used': '位置合わせ使用 — 検証合格ではありません',
    'unmapped': '未配置',
}

_MODE_ORDER = ('off', *SURVEY_OVERLAY_MODES)
_MODE_COMBO_LABELS = {'off': '非表示', **MODE_LABELS}


class RoomSurveyPanel(QWidget):
    """Geometry-dock survey surface: mode select, element/control lists,
    and the authority detail of the selected row."""

    modeChanged = Signal(str)

    def __init__(
        self,
        survey_controller=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName('roomSurveyPanel')
        self._controller = survey_controller
        self._scene = None
        self._syncing = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        header = QLabel('竣工測量オーバーレイ (#1004)')
        set_typography_role(header, TypographyRole.SECTION_TITLE)
        header.setToolTip(
            '測量権威の証跡・不確かさ・検証状態をCAD面に重ねます。\n'
            '読み取り専用 — 適合判定や測量記録は変更されません。'
        )
        header.setWordWrap(True)
        layout.addWidget(header)

        mode_form = QFormLayout()
        self.mode_combo = QComboBox()
        for mode in _MODE_ORDER:
            self.mode_combo.addItem(_MODE_COMBO_LABELS[mode], mode)
        self.mode_combo.setToolTip(
            'ビューポートの測量オーバーレイ表示モード'
        )
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        mode_form.addRow('表示モード', self.mode_combo)
        layout.addLayout(mode_form)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        set_typography_role(self.summary, TypographyRole.SECONDARY)
        layout.addWidget(self.summary)

        self.elements = QTreeWidget()
        self.elements.setAccessibleName('測量要素一覧')
        self.elements.setHeaderLabels(('要素 / コントロール', '状態'))
        tree_header = self.elements.headerItem()
        if tree_header is not None:
            tree_header.setToolTip(0, '測量要素・コントロール・ブロック中のタスク')
            tree_header.setToolTip(1, '現行リビジョンに対する解決状態')
        self.elements.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.elements, stretch=1)

        detail_header = QLabel('権威詳細 (クリックで表示)')
        set_typography_role(detail_header, TypographyRole.SECONDARY)
        layout.addWidget(detail_header)
        self.detail = QTextBrowser()
        self.detail.setAccessibleName('測量権威詳細')
        self.detail.setToolTip(
            'キャンペーン・機器・校正証跡・ハッシュ・ブロック理由・'
            '下流失効成果物 — 要素行を選択すると表示されます'
        )
        self.detail.setMinimumHeight(120)
        layout.addWidget(self.detail)

        self._show_empty('測量オーバーレイはオフです')

    # -- scene → widgets ----------------------------------------------------

    def show_scene(self, scene) -> None:
        """Render the resolved overlay scene (None = overlay off / no head)."""

        self._scene = scene
        self._syncing = True
        try:
            if scene is None:
                self.mode_combo.setCurrentIndex(0)
            else:
                self.mode_combo.setCurrentIndex(
                    _MODE_ORDER.index(scene.mode)
                )
        finally:
            self._syncing = False
        if scene is None:
            self._show_empty('測量オーバーレイはオフです')
            return

        self.summary.setText(
            f'リビジョン {scene.revision_id[:8]} · モード '
            f'{MODE_LABELS[scene.mode]} · マップ済 {len(scene.elements)} · '
            f'未マップ {len(scene.unmapped_elements)}'
        )

        self.elements.clear()
        mapped_root = QTreeWidgetItem(self.elements, ('要素', ''))
        mapped_root.setExpanded(True)
        for item in scene.elements:
            element = item.element
            entry = item.state_entry
            unc = (
                'n/a'
                if item.achieved_uncertainty_mm is None
                else f'±{item.achieved_uncertainty_mm:.0f}mm'
            )
            state_text = VERIFICATION_LABELS.get(
                item.verification, item.verification
            )
            tier = 'T?' if item.tier is None else f'T{item.tier}'
            row = QTreeWidgetItem(
                mapped_root,
                (f'{element.element_key}', f'{state_text} · {tier} · {unc}'),
            )
            row.setData(0, Qt.ItemDataRole.UserRole, element.element_id)
            row.setToolTip(
                0, f'{element.element_id} — クリックで権威詳細'
            )
        unmapped_root = QTreeWidgetItem(self.elements, ('マッピング不可', ''))
        unmapped_root.setExpanded(True)
        for item in scene.unmapped_elements:
            row = QTreeWidgetItem(
                unmapped_root,
                (
                    item.element.element_key,
                    UNMAPPED_REASON_LABELS.get(
                        item.unmapped_reason, str(item.unmapped_reason)
                    ),
                ),
            )
            row.setData(
                0, Qt.ItemDataRole.UserRole, item.element.element_id
            )
            row.setToolTip(
                0,
                '近傍の面へ推測で貼り付けることはしません — '
                '安定IDが現行リビジョンに無いため描画されません',
            )
        controls_root = QTreeWidgetItem(
            self.elements, ('独立コントロール', '')
        )
        controls_root.setExpanded(True)
        for item in (*scene.controls, *scene.unmapped_controls):
            control = item.control
            residual = (
                '宣言値なし'
                if control.residual_mm is None
                else f'残差 {control.residual_mm:.1f}mm'
            )
            tol = (
                '許容差なし'
                if control.tolerance_mm is None
                else f'許容差 {control.tolerance_mm:.0f}mm'
            )
            row = QTreeWidgetItem(
                controls_root,
                (
                    f'{control.control_id}',
                    f'{_CONTROL_BUCKET_LABELS[item.bucket]} · '
                    f'{residual} · {tol}',
                ),
            )
            row.setData(0, Qt.ItemDataRole.UserRole, f'control:{control.control_id}')
            row.setToolTip(0, item.label_ascii)
        tasks_root = QTreeWidgetItem(
            self.elements, ('ブロック中のタスク', '')
        )
        tasks_root.setExpanded(True)
        blocked = self._blocked_task_rows()
        for task_id, verdict, reasons, blockers in blocked:
            reason_text = ' / '.join(
                REASON_LABELS.get(r, r) for r in reasons
            )
            row = QTreeWidgetItem(
                tasks_root,
                (
                    task_id,
                    f'{TASK_VERDICT_LABELS.get(verdict, verdict)} — '
                    f'{reason_text}',
                ),
            )
            row.setData(0, Qt.ItemDataRole.UserRole, f'task:{task_id}')
            row.setToolTip(
                0,
                f'ブロック要素: {", ".join(blockers) or "なし"} — '
                '理由を1クリックで確認できます',
            )
        if not blocked:
            QTreeWidgetItem(tasks_root, ('(なし)', ''))

    def _blocked_task_rows(self) -> list:
        """(task_id, verdict, reasons, blocking ids) — 1-2 ops reach.

        Task verdicts are not on the scene items (keeps the resolve
        payload small); pull them from the detail provider.
        """

        rows: list = []
        controller = self._controller
        scene = self._scene
        if controller is None or scene is None:
            return rows
        seen: set[str] = set()
        for item in (*scene.elements, *scene.unmapped_elements):
            detail = controller.describe_element(item.element.element_id)
            if detail is None:
                continue
            for task_id, verdict, reasons in detail.blocking_task_verdicts:
                if task_id in seen:
                    continue
                seen.add(task_id)
                rows.append((task_id, verdict, reasons, (detail.element_id,)))
        return rows

    def _show_empty(self, text: str) -> None:
        self.summary.setText(text)
        self.elements.clear()
        self.detail.setPlainText('')

    # -- selection → authority detail ---------------------------------------

    def _selection_changed(self) -> None:
        items = self.elements.selectedItems()
        if not items:
            return
        key = items[0].data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(key, str):
            return
        controller = self._controller
        if controller is None:
            return
        if key.startswith('control:'):
            control = controller.survey_repository.get_control(
                key.split(':', 1)[1]
            )
            if control is None:
                self.detail.setPlainText('コントロールが見つかりません')
                return
            lines = [
                f'{control.control_id} — {control.kind}',
                f'計測 {control.measured_value_mm:.1f}mm'
                + (
                    f' / 宣言 {control.declared_value_mm:.1f}mm'
                    if control.declared_value_mm is not None
                    else ' / 宣言値なし'
                )
                + (
                    f' / 許容差 {control.tolerance_mm:.1f}mm'
                    if control.tolerance_mm is not None
                    else ' / 許容差なし — 残差は証跡表示のみ'
                ),
                f'残差: {"n/a" if control.residual_mm is None else f"{control.residual_mm:.2f}mm"}',
                (
                    '位置合わせに使用 — 検証合格とは表示しません'
                    if control.used_for_registration
                    else '独立コントロール'
                ),
                f'対象: {", ".join(control.element_keys) or "なし"}',
                f'hash: {control.control_sha256[:16]}…',
            ]
            self.detail.setPlainText('\n'.join(lines))
            return
        if key.startswith('task:'):
            # Task rows expose their blockers via the element detail —
            # selecting the row jumps to the first blocking element's
            # bundle so the reason chain stays one hop.
            task_id = key.split(':', 1)[1]
            self.detail.setPlainText(
                f'タスク {task_id}: ブロック理由は上の行に表示 — '
                'ブロック要素の行を選択すると権威詳細が開きます'
            )
            return
        detail = controller.describe_element(key)
        if detail is None:
            self.detail.setPlainText('要素の権威レコードが見つかりません')
            return
        self.detail.setPlainText('\n'.join(detail.detail_lines_ja))

    def _mode_changed(self, index: int) -> None:
        if self._syncing:
            return
        mode = self.mode_combo.itemData(index)
        if isinstance(mode, str):
            self.modeChanged.emit(mode)


__all__ = ['RoomSurveyPanel']
