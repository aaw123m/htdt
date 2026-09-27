"""Scene object list for the workflow-first Room workspace (#482).

A lightweight always-visible object browser that stays selection-synced with
the 3D viewport, exposes hide/show + lock/unlock per entity and in batch, and
supports Undo-safe delete. Hidden objects remain listed (visibility toggled
off ≠ deleted); locked objects are inspectable but not editable.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .cad_scene import SceneDocument
from .ui_theme import set_typography_role, TypographyRole

_ENTITY_ROLE = Qt.ItemDataRole.UserRole


class RoomObjectsPanel(QWidget):
    """Object list + visibility/lock/delete actions for the Room workspace."""

    selectionRequested = Signal(tuple, object)  # (ordered entity ids, primary id | None)
    hideRequested = Signal(tuple, bool)
    lockRequested = Signal(tuple, bool)
    deleteRequested = Signal(tuple)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.summary = QLabel("オブジェクト一覧")
        set_typography_role(self.summary, TypographyRole.SECONDARY)
        layout.addWidget(self.summary)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(("名前", "種類", "表示", "ロック"))
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.itemSelectionChanged.connect(self._emit_selection)
        layout.addWidget(self.tree, stretch=1)

        button_row = QHBoxLayout()
        button_row.setSpacing(4)
        self.hide_button = QPushButton("隠す")
        self.show_button = QPushButton("表示")
        self.lock_button = QPushButton("ロック")
        self.unlock_button = QPushButton("解除")
        self.delete_button = QPushButton("削除")
        for button in (
            self.hide_button,
            self.show_button,
            self.lock_button,
            self.unlock_button,
            self.delete_button,
        ):
            button_row.addWidget(button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        self.hide_button.clicked.connect(lambda: self.hideRequested.emit(self.selected_entity_ids(), True))
        self.show_button.clicked.connect(lambda: self.hideRequested.emit(self.selected_entity_ids(), False))
        self.lock_button.clicked.connect(lambda: self.lockRequested.emit(self.selected_entity_ids(), True))
        self.unlock_button.clicked.connect(lambda: self.lockRequested.emit(self.selected_entity_ids(), False))
        self.delete_button.clicked.connect(lambda: self.deleteRequested.emit(self.selected_entity_ids()))

        self._syncing = False

    def selected_entity_ids(self) -> tuple[str, ...]:
        return tuple(
            str(item.data(0, _ENTITY_ROLE))
            for item in self.tree.selectedItems()
            if item.data(0, _ENTITY_ROLE) is not None
        )

    def _emit_selection(self) -> None:
        if self._syncing:
            return
        ids = self.selected_entity_ids()
        primary = ids[-1] if ids else None
        self.selectionRequested.emit(ids, primary)

    def sync_document(
        self,
        document: SceneDocument,
        *,
        selected_ids: tuple[str, ...],
        primary_id: str | None,
        hidden_ids: set[str] | frozenset[str],
        locked_ids: set[str] | frozenset[str],
        kind_labels: dict[str, str],
    ) -> None:
        """Refresh rows from document + view state (call on every refresh)."""

        self._syncing = True
        try:
            selected = set(selected_ids)
            self.tree.clear()
            hidden_count = 0
            locked_count = 0
            current_item: QTreeWidgetItem | None = None
            for entity in document.entities:
                hidden = entity.entity_id in hidden_ids
                locked = entity.entity_id in locked_ids
                hidden_count += 1 if hidden else 0
                locked_count += 1 if locked else 0
                item = QTreeWidgetItem(
                    [
                        entity.name,
                        kind_labels.get(entity.kind, entity.kind),
                        "—" if hidden else "●",
                        "🔒" if locked else "",
                    ]
                )
                item.setData(0, _ENTITY_ROLE, entity.entity_id)
                if hidden:
                    item.setForeground(
                        0,
                        self.palette().color(
                            QPalette.ColorGroup.Disabled,
                            QPalette.ColorRole.WindowText,
                        ),
                    )
                if entity.entity_id in selected:
                    item.setSelected(True)
                if entity.entity_id == primary_id:
                    current_item = item
                    # Primary gets a distinct marker in the name column.
                    item.setText(0, f"▶ {entity.name}")
                self.tree.addTopLevelItem(item)
            if current_item is not None:
                self.tree.setCurrentItem(current_item, 0)
                self.tree.scrollToItem(current_item)
            self.summary.setText(
                f"オブジェクト {len(document.entities)} 件 · 選択 {len(selected)} 件"
                + (f" · 非表示 {hidden_count}" if hidden_count else "")
                + (f" · ロック {locked_count}" if locked_count else "")
            )
        finally:
            self._syncing = False
