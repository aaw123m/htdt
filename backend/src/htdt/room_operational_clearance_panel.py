"""運用クリアランス layer panel for the Room placement surface (#1010).

Read-only companion to :mod:`room_operational_clearance`: the 「運用クリアランス」
preview toggle, the five zone-kind filters (開閉/リクライニング/引出し/
サービス/回転), honest counts (shown zones / conflicts / 未宣言 /
filtered-out), and the XY-projection disclaimer text.

The panel never authors or edits zones — it only repaints the 3-D
explanation layer; zone authoring stays on the entity-edit path.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from .room_operational_clearance import (
    OPERATIONAL_CLEARANCE_DISCLAIMER,
    OPERATIONAL_ZONE_KIND_VOCAB,
    OPERATIONAL_ZONE_KINDS,
    OperationalClearancePreview,
)
from .ui_theme import TypographyRole, set_typography_role


class RoomOperationalClearancePanel(QWidget):
    """Read-only 運用クリアランス layer controls for Room placement."""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName('roomOperationalClearancePanel')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        heading = QLabel('運用クリアランス（読み取り専用レイヤー）')
        heading.setWordWrap(True)
        set_typography_role(heading, TypographyRole.SECTION_TITLE)
        layout.addWidget(heading)

        self.preview_toggle = QCheckBox('運用クリアランスを3Dに表示')
        self.preview_toggle.setObjectName('opclearPreviewToggle')
        self.preview_toggle.setToolTip(
            '宣言された運用ゾーン（開閉・リクライニング・引出し・'
            'サービス・回転）のXY投影と干渉箇所を部屋3D上に重ねます'
        )
        self.preview_toggle.toggled.connect(lambda _c=False: self.changed.emit())
        layout.addWidget(self.preview_toggle)

        filter_caption = QLabel('ゾーン種類フィルタ')
        filter_caption.setWordWrap(True)
        set_typography_role(filter_caption, TypographyRole.SECONDARY)
        layout.addWidget(filter_caption)

        # 3-column grid so all five kind filters stay reachable on narrow
        # columns — a single HBox clips the 回転 checkbox at ~1024 px.
        kinds_grid = QGridLayout()
        kinds_grid.setContentsMargins(0, 0, 0, 0)
        kinds_grid.setHorizontalSpacing(6)
        kinds_grid.setVerticalSpacing(2)
        self.kind_toggles: dict[str, QCheckBox] = {}
        for index, kind in enumerate(OPERATIONAL_ZONE_KINDS):
            label, _color = OPERATIONAL_ZONE_KIND_VOCAB[kind]
            toggle = QCheckBox(label)
            toggle.setObjectName(f'opclearKind-{kind}')
            toggle.setChecked(True)
            toggle.setToolTip(f'{label}ゾーンの表示を切り替えます')
            toggle.toggled.connect(lambda _c=False: self.changed.emit())
            kinds_grid.addWidget(toggle, index // 3, index % 3)
            self.kind_toggles[kind] = toggle
        kinds_grid.setColumnStretch(3, 1)
        kinds_host = QWidget()
        kinds_host.setLayout(kinds_grid)
        layout.addWidget(kinds_host)

        self.counts_label = QLabel()
        self.counts_label.setObjectName('opclearCounts')
        self.counts_label.setWordWrap(True)
        self.counts_label.setToolTip(
            '表示ゾーン・干渉・未宣言物体・フィルタ外の件数'
            '（未宣言は「干渉なし」ではなく判定不能です）'
        )
        set_typography_role(self.counts_label, TypographyRole.SECONDARY)
        layout.addWidget(self.counts_label)

        self.disclaimer = QLabel(OPERATIONAL_CLEARANCE_DISCLAIMER)
        self.disclaimer.setObjectName('opclearDisclaimer')
        self.disclaimer.setWordWrap(True)
        set_typography_role(self.disclaimer, TypographyRole.SECONDARY)
        layout.addWidget(self.disclaimer)

        self._preview: OperationalClearancePreview | None = None
        self._sync_text()

    @property
    def preview_enabled(self) -> bool:
        return self.preview_toggle.isChecked()

    @property
    def enabled_kinds(self) -> frozenset[str]:
        return frozenset(
            kind
            for kind, toggle in self.kind_toggles.items()
            if toggle.isChecked()
        )

    def show_preview(self, preview: OperationalClearancePreview | None) -> None:
        """Update honest counts from the rendered preview (or None)."""
        self._preview = preview
        self._sync_text()

    def _sync_text(self) -> None:
        preview = self._preview
        if preview is None:
            self.counts_label.setText('')
            return
        text = (
            f'ゾーン表示{len(preview.zones)}件/'
            f'{preview.total_zone_count}件 · '
            f'干渉{len(preview.conflicts)}件/'
            f'{preview.total_conflict_count}件 · '
            f'未宣言{len(preview.undeclared)}物体（判定不能）'
        )
        if preview.undefined_zones:
            text += f' · 未評価ゾーン{len(preview.undefined_zones)}件'
        if preview.filtered_zone_count or preview.hidden_conflict_count:
            text += (
                f' · フィルタ外 ゾーン{preview.filtered_zone_count}件'
                f'/干渉{preview.hidden_conflict_count}件'
            )
        self.counts_label.setText(text)
