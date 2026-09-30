"""SceneRevision history panel for the workflow-first Room (#485).

Browse the document's recorded lineage (mainline + detached), attach labels
and notes without mutating history, preview any revision as a read-only ghost
overlay over the current scene, inspect a structured diff, and restore a
historical state as a NEW revision descending from the latest head — never a
rewind, so every save keeps a valid audit trail.
"""

from __future__ import annotations

import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .cad_display_labels import revision_display_label
from .cad_scene_history import diff_summary_lines, summarize_revision
from .ui_theme import TypographyRole, set_typography_role

_REVISION_ROLE = Qt.ItemDataRole.UserRole


class RoomHistoryPanel(QWidget):
    """Browse / label / diff / preview / restore SceneRevisions."""

    previewRequested = Signal(object)  # revision_id or None (clear)
    restoreRequested = Signal(str)
    labelRequested = Signal(str, str, str)  # revision_id, label, note
    diffRequested = Signal(object)  # revision_id or None → diff vs current

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        heading = QLabel("シーン履歴")
        set_typography_role(heading, TypographyRole.SECTION_TITLE)
        layout.addWidget(heading)

        self.summary = QLabel("リビジョンなし")
        set_typography_role(self.summary, TypographyRole.SECONDARY)
        layout.addWidget(self.summary)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(("時刻", "ラベル", "内容"))
        self.tree.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.tree.header().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.currentItemChanged.connect(self._on_current_changed)
        layout.addWidget(self.tree, stretch=2)

        label_row = QHBoxLayout()
        label_row.setSpacing(4)
        self.label_field = QLineEdit()
        self.label_field.setPlaceholderText("ラベル（例: v2 候補）")
        self.label_field.setAccessibleName("ラベル")
        self.note_field = QLineEdit()
        self.note_field.setPlaceholderText("メモ（任意）")
        self.note_field.setAccessibleName("メモ")
        self.label_button = QPushButton("ラベル保存")
        label_row.addWidget(self.label_field, stretch=1)
        label_row.addWidget(self.note_field, stretch=1)
        label_row.addWidget(self.label_button)
        layout.addLayout(label_row)

        button_row = QHBoxLayout()
        button_row.setSpacing(4)
        self.preview_button = QPushButton("3Dプレビュー")
        self.preview_button.setCheckable(True)
        self.diff_button = QPushButton("差分を表示")
        self.restore_button = QPushButton("この版に復元")
        for button in (self.preview_button, self.diff_button, self.restore_button):
            button_row.addWidget(button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setPlaceholderText("リビジョンを選択すると詳細と差分を表示します")
        self.detail.setAccessibleName("リビジョン詳細")
        layout.addWidget(self.detail, stretch=1)

        self.preview_button.toggled.connect(self._on_preview_toggled)
        self.diff_button.clicked.connect(self._on_diff)
        self.restore_button.clicked.connect(self._on_restore)
        self.label_button.clicked.connect(self._on_label)

        self._selected_revision_id: str | None = None
        self._previewing = False
        self._labels: dict = {}

    def selected_revision_id(self) -> str | None:
        return self._selected_revision_id

    def select_revision(self, revision_id: str) -> bool:
        """Focus the row for ``revision_id`` — navigation deep-link entry."""
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            if item.data(0, _REVISION_ROLE) == revision_id:
                self.tree.setCurrentItem(item)
                self.tree.scrollToItem(item)
                return True
        return False

    def _on_current_changed(self, current: QTreeWidgetItem | None, _previous) -> None:
        self._selected_revision_id = None if current is None else str(current.data(0, _REVISION_ROLE))
        stored = self._labels.get(self._selected_revision_id) if self._selected_revision_id else None
        self.label_field.setText(stored.label if stored is not None else '')
        self.note_field.setText(stored.note if stored is not None else '')
        if self._previewing:
            self._on_preview_toggled(True)
        self._emit_detail()

    def _on_preview_toggled(self, checked: bool) -> None:
        self._previewing = checked and self._selected_revision_id is not None
        if self._previewing:
            self.previewRequested.emit(self._selected_revision_id)
        else:
            self.previewRequested.emit(None)

    def _on_diff(self) -> None:
        if self._selected_revision_id is not None:
            self.diffRequested.emit(self._selected_revision_id)

    def _on_restore(self) -> None:
        if self._selected_revision_id is not None:
            self.restoreRequested.emit(self._selected_revision_id)

    def _on_label(self) -> None:
        if self._selected_revision_id is None:
            return
        label = self.label_field.text().strip()
        if not label:
            return
        self.labelRequested.emit(self._selected_revision_id, label, self.note_field.text().strip())

    def _emit_detail(self) -> None:
        self.diffRequested.emit(self._selected_revision_id)

    def show_detail(self, text: str) -> None:
        self.detail.setPlainText(text)

    def set_label_fields(self, label: str, note: str) -> None:
        self.label_field.setText(label)
        self.note_field.setText(note)

    def sync_revisions(
        self,
        revisions: tuple,
        *,
        head_revision_id: str | None,
        labels: dict[str, object],
        current_summary_resolver=None,
    ) -> None:
        """Refresh rows; ``labels`` maps revision_id → RevisionLabel."""

        self._labels = dict(labels)
        self.tree.blockSignals(True)
        self.tree.clear()
        head_id = head_revision_id
        by_id = {revision.revision_id: revision for revision in revisions}
        keep_current: QTreeWidgetItem | None = None
        for revision in revisions:
            summary = summarize_revision(revision.document)
            marker = ' ●HEAD' if revision.revision_id == head_id else ''
            detached = ''
            if revision.detached:
                # A detached row's only visible lineage is its parent pointer —
                # name the branch point so the flat list doesn't hide the fork.
                parent = by_id.get(revision.parent_revision_id)
                branch = (
                    f' ←{revision_display_label(parent, labels)}'
                    if parent is not None
                    else ''
                )
                detached = ' ◇detached' + branch
            label = labels.get(revision.revision_id)
            label_text = label.label if label is not None else ''
            kinds = ' '.join(f'{name}×{count}' for name, count in summary.kind_counts)
            # Drop sub-second precision but keep the zone suffix — a bare
            # 'YYYY-MM-DD HH:MM:SS' cell reads as local wall time while the
            # stored instant is UTC.
            stamp = re.sub(r'\.\d+', '', revision.created_at_utc).replace('T', ' ')
            item = QTreeWidgetItem(
                [
                    stamp + marker + detached,
                    label_text,
                    f'{summary.entity_count}項目 · {kinds}' + ('' if summary.has_room else ' · 部屋なし'),
                ]
            )
            item.setData(0, _REVISION_ROLE, revision.revision_id)
            item.setToolTip(0, revision.revision_id)
            self.tree.addTopLevelItem(item)
            if revision.revision_id == self._selected_revision_id:
                keep_current = item
        if keep_current is not None:
            self.tree.setCurrentItem(keep_current)
        self.tree.blockSignals(False)
        self.summary.setText(
            f'{len(revisions)} リビジョン · ラベル {len(labels)} 件'
        )
