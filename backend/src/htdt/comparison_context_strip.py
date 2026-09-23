"""Persistent comparison context strip for the Optimize workspace (#584).

A compact strip rendered above the Optimize page stack that keeps the
current engineering context visible across Candidates / Comparison /
Robustness / Validation: baseline, selected candidate, scene revision and
selection status. It is pure presentation over the shared controller
selection — it never stores or mutates authority.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel

from .ui_theme import SemanticState, set_semantic_state, set_typography_role, TypographyRole


class ComparisonContextStrip(QFrame):
    """'現在の比較: 基準 … | 選択 … | シーン … | 状態 …' persistent strip."""

    def __init__(
        self,
        parent=None,
        *,
        on_details: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName('comparisonContextStrip')
        self._on_details = on_details

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(16)

        self.baseline_value = QLabel('—')
        self.selected_value = QLabel('—')
        self.scene_value = QLabel('—')
        self.status_value = QLabel('—')
        for caption, value_label, name in (
            ('基準', self.baseline_value, 'baseline'),
            ('選択候補', self.selected_value, 'selected'),
            ('シーン', self.scene_value, 'scene'),
            ('状態', self.status_value, 'status'),
        ):
            caption_label = QLabel(f'{caption}:')
            caption_label.setObjectName(f'contextStripCaption:{name}')
            set_typography_role(caption_label, TypographyRole.SECONDARY)
            layout.addWidget(caption_label)
            value_label.setObjectName(f'contextStripValue:{name}')
            layout.addWidget(value_label)
        layout.addStretch(1)

    def set_context(
        self,
        *,
        baseline_label: str,
        selected_label: str | None,
        scene_label: str | None,
        status_label: str | None,
    ) -> None:
        self.baseline_value.setText(baseline_label)
        self.selected_value.setText(selected_label or '—')
        self.scene_value.setText(scene_label or '—')
        self.status_value.setText(status_label or '—')
        set_semantic_state(
            self.status_value,
            SemanticState.WARNING
            if status_label in ('プレビュー', '選択済み')
            else None,
        )
