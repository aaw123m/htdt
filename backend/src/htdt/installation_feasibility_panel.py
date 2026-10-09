"""Installation feasibility inspection panel (Issue #1005).

Read-only surface over :mod:`installation_feasibility_viewmodel`: the
toggle controls the 3D overlay, the item list binds each mounting record
to its exact entity + construction element + verdict, and the detail
block shows every authority check reason plus the on-site construction
confirmations required for UNKNOWN items. UNKNOWN (灰色・不明) is an
evidence gap — never a red failure. The disclaimer states this is not a
construction permit, building-code, or load-rating certification.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .installation_feasibility_viewmodel import (
    FEASIBILITY_VERDICT_VOCAB,
    InstallationFeasibilityItem,
    InstallationFeasibilityPreview,
)
from .ui_theme import TypographyRole, set_typography_role


class RoomInstallationFeasibilityPanel(QWidget):
    """Read-only 設置実現性検査 controls for Room placement."""

    changed = Signal()
    itemSelected = Signal(object)  # InstallationFeasibilityItem | None

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName('roomInstallationFeasibilityPanel')
        self._preview: InstallationFeasibilityPreview | None = None
        self._items_by_row: dict[int, InstallationFeasibilityItem] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        heading = QLabel('設置実現性検査（読み取り専用レイヤー）')
        heading.setWordWrap(True)
        set_typography_role(heading, TypographyRole.SECTION_TITLE)
        layout.addWidget(heading)

        self.preview_toggle = QCheckBox('設置実現性を3Dに表示')
        self.preview_toggle.setObjectName('installFeasibilityPreviewToggle')
        self.preview_toggle.setToolTip(
            '取付記録ごとの実現性判定（基材・荷重・下地・開口深さ・保守空間）'
            'を部屋3D上に重ねます。不明は不適合ではなく現場確認項目です'
        )
        self.preview_toggle.toggled.connect(lambda _c=False: self.changed.emit())
        layout.addWidget(self.preview_toggle)

        self.counts_label = QLabel()
        self.counts_label.setObjectName('installFeasibilityCounts')
        self.counts_label.setWordWrap(True)
        self.counts_label.setToolTip(
            '記録数と判定内訳（不明は不適合ではなく現場確認項目です）'
        )
        set_typography_role(self.counts_label, TypographyRole.SECONDARY)
        layout.addWidget(self.counts_label)

        items_caption = QLabel('取付記録（選択で判定根拠を表示）')
        items_caption.setWordWrap(True)
        set_typography_role(items_caption, TypographyRole.SECONDARY)
        layout.addWidget(items_caption)

        self.items_list = QListWidget()
        self.items_list.setObjectName('installFeasibilityItems')
        self.items_list.setMinimumHeight(72)
        self.items_list.setMaximumHeight(132)
        self.items_list.setAlternatingRowColors(True)
        self.items_list.currentRowChanged.connect(self._on_row_changed)
        layout.addWidget(self.items_list)

        detail_caption = QLabel('判定根拠（authorityチェック原文）')
        detail_caption.setWordWrap(True)
        set_typography_role(detail_caption, TypographyRole.SECONDARY)
        layout.addWidget(detail_caption)

        self.detail_label = QLabel('—')
        self.detail_label.setObjectName('installFeasibilityDetail')
        self.detail_label.setWordWrap(True)
        self.detail_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.detail_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.detail_label)

        confirm_caption = QLabel('現場で確認すべき事項')
        confirm_caption.setWordWrap(True)
        set_typography_role(confirm_caption, TypographyRole.SECONDARY)
        layout.addWidget(confirm_caption)

        self.confirm_label = QLabel('—')
        self.confirm_label.setObjectName('installFeasibilityConfirmations')
        self.confirm_label.setWordWrap(True)
        self.confirm_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.confirm_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.confirm_label)

        legend_host = QWidget()
        legend_layout = QHBoxLayout(legend_host)
        legend_layout.setContentsMargins(0, 0, 0, 0)
        legend_layout.setSpacing(8)
        for verdict, (label, color) in FEASIBILITY_VERDICT_VOCAB.items():
            chip = QLabel(f'■{label}')
            chip.setStyleSheet(f'color: {color};')
            chip.setToolTip(f'verdict: {verdict}')
            legend_layout.addWidget(chip)
        legend_layout.addStretch(1)
        layout.addWidget(legend_host)

        self.disclaimer = QLabel()
        self.disclaimer.setObjectName('installFeasibilityDisclaimer')
        self.disclaimer.setWordWrap(True)
        set_typography_role(self.disclaimer, TypographyRole.SECONDARY)
        layout.addWidget(self.disclaimer)

        self._sync_text()
        self._update_detail(None)

    @property
    def preview_enabled(self) -> bool:
        return self.preview_toggle.isChecked()

    def selected_item(self) -> InstallationFeasibilityItem | None:
        return self._items_by_row.get(self.items_list.currentRow())

    def show_preview(
        self, preview: InstallationFeasibilityPreview | None
    ) -> None:
        """Bind the freshly evaluated preview — called every render pass."""

        self._preview = preview
        previous_entity = (
            item.entity_id
            if (item := self.selected_item()) is not None
            else None
        )
        self.items_list.blockSignals(True)
        try:
            self.items_list.clear()
            self._items_by_row.clear()
            items = preview.items if preview is not None else ()
            restore_row = -1
            for index, item in enumerate(items):
                label = item.entity_name or item.entity_id
                row = QListWidgetItem(
                    f'{label} — {item.overall_label}'
                )
                row.setToolTip(
                    f'{item.entity_id} · {item.mounting_label} · '
                    f'{item.element_label} · {item.overall_label}'
                )
                row.setData(Qt.ItemDataRole.UserRole, item.entity_id)
                row.setForeground(QBrush(QColor(self._verdict_color(item))))
                self.items_list.addItem(row)
                self._items_by_row[index] = item
                if item.entity_id == previous_entity:
                    restore_row = index
            if restore_row >= 0:
                self.items_list.setCurrentRow(restore_row)
        finally:
            self.items_list.blockSignals(False)

        self._sync_text()
        if self._items_by_row and self.items_list.currentRow() < 0:
            self.items_list.setCurrentRow(0)
        self._update_detail(self.selected_item())

    def _verdict_color(self, item: InstallationFeasibilityItem) -> str:
        if item.overall is not None:
            return FEASIBILITY_VERDICT_VOCAB[item.overall][1]
        # Unevaluated items share the UNKNOWN gray — never red.
        return FEASIBILITY_VERDICT_VOCAB['unknown'][1]

    def _sync_text(self) -> None:
        preview = self._preview
        if preview is None or not preview.items:
            self.counts_label.setText('取付記録がありません')
            self.disclaimer.setText(
                preview.disclaimer if preview is not None else ''
            )
            return
        self.counts_label.setText(preview.summary)
        self.disclaimer.setText(preview.disclaimer)

    def _on_row_changed(self, _row: int) -> None:
        self._update_detail(self.selected_item())
        self.itemSelected.emit(self.selected_item())

    def _update_detail(
        self, item: InstallationFeasibilityItem | None
    ) -> None:
        if item is None:
            self.detail_label.setText('—')
            self.confirm_label.setText('—')
            return
        lines: list[str] = [
            f'{item.entity_name or item.entity_id} · '
            f'{item.mounting_label} · 対象: {item.element_label}'
        ]
        if item.state != 'evaluated':
            lines.append(f'状態: {item.state_label}（authority判定なし）')
        else:
            assert item.report is not None
            lines.append(f'総合: {item.overall_label}')
            for row in item.checks:
                lines.append(
                    f'· {row.label}：{row.verdict_label} — {row.detail}'
                )
            if item.request is not None:
                req = item.request
                bits = [f'荷重 {req.payload_kg:.1f}kg']
                if req.cutout_required:
                    bits.append(
                        f'掘込深さ {float(req.cutout_depth_m or 0.0):.3f}m'
                    )
                if float(req.service_clearance_m) > 0.0:
                    bits.append(
                        f'保守空間 {float(req.service_clearance_m):.3f}m'
                    )
                lines.append(f'要求: {" · ".join(bits)}')
        self.detail_label.setText('\n'.join(lines))

        if item.confirmations:
            self.confirm_label.setText(
                '\n'.join(f'· {c}' for c in item.confirmations)
            )
        elif item.state == 'evaluated':
            self.confirm_label.setText('（追加の現場確認項目なし）')
        else:
            self.confirm_label.setText('—')
