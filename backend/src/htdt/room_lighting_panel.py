"""Lighting-scene preview panel for the Room/Video surface (#1013).

Read-only companion to :mod:`room_lighting_preview`: a 「照明シーン」
preview toggle, the current scene identity, per-stage legend honesty
text, counts (pinned / 3D未配置 / unresolved), and the ``3D未配置`` list
that keeps control-only and dangling-binding fixtures visible.

The panel never issues a device command — 「適用」「実機読出し」 stay on
the approved device-action path; preview toggling only repaints the 3-D
explanation overlay.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .cad_lighting import LightingScene
from .room_lighting_preview import (
    LIGHTING_PREVIEW_DISCLAIMER,
    LIGHTING_ZONE_VOCAB,
    LightingScenePreview,
    stage_bead_label,
)
from .ui_theme import TypographyRole, set_typography_role


class RoomLightingPreviewPanel(QWidget):
    """Read-only lighting-scene preview controls for Room/Video."""

    previewToggled = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName('roomLightingPreviewPanel')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        heading = QLabel('照明シーン（読み取り専用プレビュー）')
        heading.setWordWrap(True)
        set_typography_role(heading, TypographyRole.SECTION_TITLE)
        layout.addWidget(heading)

        self.preview_toggle = QCheckBox('照明シーンを3Dに表示')
        self.preview_toggle.setObjectName('lightingPreviewToggle')
        self.preview_toggle.setToolTip(
            '現在の照明シーンの目標・送信・応答・実測の各状態を'
            '部屋3D上に説明記号として重ねます（機器へは送信しません）'
        )
        self.preview_toggle.toggled.connect(self.previewToggled)
        layout.addWidget(self.preview_toggle)

        self.scene_summary = QLabel('照明シーン未選択')
        self.scene_summary.setObjectName('lightingSceneSummary')
        self.scene_summary.setWordWrap(True)
        self.scene_summary.setToolTip('プレビュー対象の照明シーン権威の識別情報')
        set_typography_role(self.scene_summary, TypographyRole.SECONDARY)
        layout.addWidget(self.scene_summary)

        self.counts_label = QLabel()
        self.counts_label.setObjectName('lightingPreviewCounts')
        self.counts_label.setWordWrap(True)
        self.counts_label.setToolTip(
            'ピン表示・3D未配置・未解決参照・無視した他シーン記録の件数'
        )
        set_typography_role(self.counts_label, TypographyRole.SECONDARY)
        layout.addWidget(self.counts_label)

        unplaced_caption = QLabel('3D未配置・ゾーン')
        unplaced_caption.setWordWrap(True)
        set_typography_role(unplaced_caption, TypographyRole.SECONDARY)
        layout.addWidget(unplaced_caption)
        self.unplaced_list = QListWidget()
        self.unplaced_list.setObjectName('lightingUnplacedList')
        self.unplaced_list.setAccessibleName('3D未配置の照明器具とゾーン')
        self.unplaced_list.setToolTip(
            'シーン参照だが部屋物体にピンできない照明器具と'
            'シーン内ゾーンの一覧（制御専用・バインド未解決）'
        )
        # Long reason/state text wraps inside the narrow panel instead of
        # clipping behind a horizontal scrollbar.
        self.unplaced_list.setWordWrap(True)
        self.unplaced_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.unplaced_list.setMaximumHeight(140)
        layout.addWidget(self.unplaced_list)

        self.disclaimer = QLabel(LIGHTING_PREVIEW_DISCLAIMER)
        self.disclaimer.setObjectName('lightingPreviewDisclaimer')
        self.disclaimer.setWordWrap(True)
        set_typography_role(self.disclaimer, TypographyRole.SECONDARY)
        layout.addWidget(self.disclaimer)

        self._scene: LightingScene | None = None
        self._preview: LightingScenePreview | None = None
        self._sync_text()

    @property
    def preview_enabled(self) -> bool:
        return self.preview_toggle.isChecked()

    def show_scene(self, scene: LightingScene | None) -> None:
        """Update the scene-identity line (independent of the toggle)."""
        self._scene = scene
        self._sync_text()

    def show_preview(self, preview: LightingScenePreview | None) -> None:
        """Update counts + 3D未配置 list from the rendered preview."""
        self._preview = preview
        self._sync_text()

    def _sync_text(self) -> None:
        if self._scene is None:
            self.scene_summary.setText('照明シーン未選択')
        else:
            self.scene_summary.setText(
                f'{self._scene.label} '
                f'({self._scene.scene_id} v{self._scene.version})'
            )
        preview = self._preview
        self.unplaced_list.clear()
        if preview is None:
            self.counts_label.setText('')
            return
        counts = (
            f'ピン{len(preview.pins)}件 / '
            f'3D未配置{len(preview.unplaced)}件 / '
            f'未解決参照{len(preview.missing_refs)}件'
        )
        if preview.ignored_records:
            counts += f' / 他シーン記録{preview.ignored_records}件は無視'
        self.counts_label.setText(counts)
        for row in preview.zone_rows:
            desired = row.states[0]
            desired_text = (
                f'{desired.level_percent:g}%'
                if desired.known
                else '不明'
            )
            item = QListWidgetItem(
                f'【ゾーン】{row.zone_label or row.zone_id} '
                f'（{row.role}・{len(row.member_fixture_ids)}件）'
                f' 目標{desired_text}'
            )
            item.setToolTip(
                'ゾーンの状態はゾーン単位の記録です — '
                'メンバー器具個別の証拠ではありません'
            )
            self.unplaced_list.addItem(item)
        for item_data in preview.unplaced:
            item = QListWidgetItem(
                f'{item_data.fixture_label or item_data.fixture_id} — '
                f'{item_data.detail} · {stage_bead_label(item_data)}'
            )
            tooltip_parts = [
                item_data.detail,
                'fixture_id: ' + item_data.fixture_id,
            ]
            if item_data.zone_roles:
                tooltip_parts.append(
                    'ゾーン: '
                    + ', '.join(
                        LIGHTING_ZONE_VOCAB.get(role, (role, ''))[0]
                        for role in item_data.zone_roles
                    )
                )
            stale = [
                s.stage for s in item_data.states if s.stale
            ]
            if stale:
                tooltip_parts.append(
                    '観測時刻不明（stale）: ' + ', '.join(stale)
                )
            item.setToolTip('\n'.join(tooltip_parts))
            self.unplaced_list.addItem(item)
