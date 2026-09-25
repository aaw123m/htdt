"""Application-scope destinations for the workflow shell (UX160 IA v2).

These pages are read-mostly surfaces over existing persisted authorities —
the project library lists real documents, the inbox lists real capture items,
activity lists real revisions, the library wraps EquipmentLibraryService, and
support reports the real diagnostics/version data. None of them fabricate
project state.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .build_info import version_string
from .cad_repository import SceneRepository
from .navigation_target import NavigationTarget, NavigationTargetKind
from .native_diagnostics import diagnostics_dir
from .ui_theme import TypographyRole, set_typography_role
from .workflow_navigation import WorkspaceDeepLink, WorkspaceId
from .workflow_shell import TargetFocusResult


@dataclass(frozen=True, slots=True)
class ProjectEntry:
    document_id: str
    head_revision_id: str
    created_at_utc: str
    revision_count: int


class ProjectLibraryService:
    """Lists persisted project documents — read-only over scene authority."""

    def __init__(self, repository: SceneRepository) -> None:
        self.path = Path(repository.path)

    def list_projects(self) -> tuple[ProjectEntry, ...]:
        if not self.path.is_file():
            return ()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            try:
                rows = connection.execute(
                    """
                    SELECT h.document_id AS document_id,
                           h.head_revision_id AS head_revision_id,
                           r.created_at_utc AS created_at_utc,
                           (SELECT COUNT(*) FROM scene_revisions s
                            WHERE s.document_id = h.document_id) AS revisions
                    FROM scene_document_heads h
                    JOIN scene_revisions r ON r.revision_id = h.head_revision_id
                    ORDER BY r.created_at_utc DESC
                    """
                ).fetchall()
            except sqlite3.Error:
                return ()
        return tuple(
            ProjectEntry(
                document_id=str(row["document_id"]),
                head_revision_id=str(row["head_revision_id"]),
                created_at_utc=str(row["created_at_utc"]),
                revision_count=int(row["revisions"]),
            )
            for row in rows
        )


def _page_layout(page: QWidget, title: str, hint: str | None = None) -> QVBoxLayout:
    layout = QVBoxLayout(page)
    layout.setContentsMargins(28, 24, 28, 24)
    layout.setSpacing(10)
    heading = QLabel(title)
    set_typography_role(heading, TypographyRole.WORKSPACE_TITLE)
    layout.addWidget(heading)
    if hint:
        label = QLabel(hint)
        set_typography_role(label, TypographyRole.SECONDARY)
        label.setWordWrap(True)
        layout.addWidget(label)
    return layout


class ProjectLibraryPage(QWidget):
    """Project library: open/switch persisted documents, start a new one."""

    project_open_requested = Signal(str)
    commission_requested = Signal()

    def __init__(
        self,
        service: ProjectLibraryService,
        current_document_id: Callable[[], str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self._current_document_id = current_document_id
        layout = _page_layout(
            self,
            "プロジェクト",
            "保存済みのプロジェクトです。開くとそのプロジェクトに切り替わります。",
        )
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ("プロジェクト", "作成日時", "リビジョン数", "現在")
        )
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._sync_buttons)
        layout.addWidget(self.table, 1)

        self.open_button = QPushButton("開く")
        self.open_button.clicked.connect(self._open_selected)
        layout.addWidget(self.open_button)
        self.new_button = QPushButton("新規プロジェクト…")
        self.new_button.clicked.connect(lambda: self.commission_requested.emit())
        layout.addWidget(self.new_button)
        self.refresh()

    def refresh(self) -> None:
        entries = self.service.list_projects()
        current = self._current_document_id()
        self.table.setRowCount(0)
        for entry in entries:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = (
                entry.document_id,
                entry.created_at_utc,
                str(entry.revision_count),
                "●" if entry.document_id == current else "",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, entry.document_id)
                self.table.setItem(row, column, item)
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.open_button.setEnabled(bool(self.table.selectedItems()))

    def _selected_document_id(self) -> str | None:
        items = self.table.selectedItems()
        for item in items:
            value = item.data(Qt.ItemDataRole.UserRole)
            if value:
                return str(value)
        return None

    def _open_selected(self) -> None:
        document_id = self._selected_document_id()
        if document_id:
            self.project_open_requested.emit(document_id)


class CaptureInboxPage(QWidget):
    """Capture Inbox: uningested capture deliveries awaiting triage."""

    def __init__(
        self,
        list_items: Callable[[], tuple],
        on_navigate: Callable[[WorkspaceDeepLink], bool],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._list_items = list_items
        self._on_navigate = on_navigate
        layout = _page_layout(
            self,
            "取り込み",
            "取得済みのCapture配送です。昇格・紐付けは測定ワークスペースで行います。",
        )
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ("スコープ", "シリーズ", "分類", "状態", "到着数")
        )
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, 1)
        link = QPushButton("測定ワークスペースを開く")
        link.clicked.connect(
            lambda: self._on_navigate(
                WorkspaceDeepLink(WorkspaceId.MEASUREMENT, "import")
            )
        )
        layout.addWidget(link)
        self.refresh()

    def refresh(self) -> None:
        items = self._list_items()
        self.table.setRowCount(0)
        for item in items:
            row = self.table.rowCount()
            self.table.insertRow(row)
            for column, value in enumerate(
                (
                    item.scope,
                    item.capture_series_id,
                    item.primary_classification,
                    item.disposition,
                    str(item.arrival_count),
                )
            ):
                cell = QTableWidgetItem(str(value))
                if column == 0:
                    cell.setData(Qt.ItemDataRole.UserRole, item.inbox_item_id)
                self.table.setItem(row, column, cell)


class ActivityPage(QWidget):
    """Activity: recent persisted scene revisions across projects."""

    def __init__(
        self,
        list_revisions: Callable[[int], tuple],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._list_revisions = list_revisions
        layout = _page_layout(
            self,
            "アクティビティ",
            "保存・昇格などの永続化された記録（最新順）です。",
        )
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(("時刻", "プロジェクト", "リビジョン"))
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, 1)
        self.refresh()

    def refresh(self) -> None:
        self.table.setRowCount(0)
        for created_at, document_id, revision_id in self._list_revisions(50):
            row = self.table.rowCount()
            self.table.insertRow(row)
            for column, value in enumerate((created_at, document_id, revision_id)):
                cell = QTableWidgetItem(str(value))
                if column == 2:
                    cell.setData(Qt.ItemDataRole.UserRole, revision_id)
                self.table.setItem(row, column, cell)


def list_recent_revisions(repository: SceneRepository, limit: int = 50) -> tuple:
    """(created_at_utc, document_id, revision_id) rows — read-only."""

    path = Path(repository.path)
    if not path.is_file():
        return ()
    try:
        with closing(sqlite3.connect(path)) as connection:
            rows = connection.execute(
                """
                SELECT created_at_utc, document_id, revision_id
                FROM scene_revisions WHERE detached = 0 ORDER BY seq DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
    except sqlite3.Error:
        return ()
    return tuple((str(a), str(b), str(c)) for a, b, c in rows)


class ReferenceLibraryPage(QWidget):
    """Reference library: equipment/source definitions shared across projects."""

    manage_requested = Signal()

    def __init__(
        self,
        list_definitions: Callable[[], tuple],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._list_definitions = list_definitions
        layout = _page_layout(
            self,
            "ライブラリ",
            "機材・ソース定義のライブラリです（プロジェクト共通）。",
        )
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(("メーカー", "モデル", "バージョン"))
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.table, 1)
        manage = QPushButton("機材ライブラリを管理…")
        manage.clicked.connect(lambda: self.manage_requested.emit())
        layout.addWidget(manage)
        self.refresh()

    def refresh(self) -> None:
        self.table.setRowCount(0)
        for definition in self._list_definitions():
            row = self.table.rowCount()
            self.table.insertRow(row)
            for column, value in enumerate(
                (
                    getattr(definition, "manufacturer", "") or "",
                    getattr(definition, "model", "")
                    or getattr(definition, "user_label", "")
                    or getattr(definition, "definition_id", ""),
                    str(getattr(definition, "version", "")),
                )
            ):
                self.table.setItem(row, column, QTableWidgetItem(str(value)))

    def focus_definition(self, definition_id: str) -> TargetFocusResult:
        for row in range(self.table.rowCount()):
            model = self.table.item(row, 1)
            if model is not None and model.text() == definition_id:
                self.table.selectRow(row)
                return TargetFocusResult(focused=True)
        return TargetFocusResult(
            focused=False,
            message="ライブラリ内に該当の定義が見つかりません",
        )


class SupportPage(QWidget):
    """Support: diagnostics locations and version — the support owner page."""

    def __init__(
        self,
        data_dir: Path,
        status_provider: Callable[[], tuple[str, ...]] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._status_provider = status_provider
        layout = _page_layout(
            self,
            "サポート",
            "問題が発生したときの確認情報です。",
        )
        for label_text in (
            f"バージョン: {version_string()}",
            f"データフォルダ: {data_dir}",
            f"診断ログ: {diagnostics_dir(data_dir)}",
        ):
            label = QLabel(label_text)
            label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            label.setWordWrap(True)
            layout.addWidget(label)
        self._status_layout = layout
        self._status_labels: list[QLabel] = []
        note = QLabel(
            "起動に失敗した場合は診断ログをサポートに共有してください。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)
        self.refresh()

    def refresh(self) -> None:
        """Re-render live status lines (e.g. effective receiver state)."""
        for label in self._status_labels:
            self._status_layout.removeWidget(label)
            label.deleteLater()
        self._status_labels.clear()
        if self._status_provider is None:
            return
        for text in self._status_provider():
            label = QLabel(text)
            label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            label.setWordWrap(True)
            self._status_layout.insertWidget(
                self._status_layout.count() - 2, label
            )
            self._status_labels.append(label)


def inbox_focus(page: CaptureInboxPage, target: NavigationTarget) -> TargetFocusResult:
    item_id = target.primary_id
    if item_id is None:
        return TargetFocusResult(focused=True)
    for row in range(page.table.rowCount()):
        cell = page.table.item(row, 0)
        if cell is not None and cell.data(Qt.ItemDataRole.UserRole) == item_id:
            page.table.selectRow(row)
            return TargetFocusResult(focused=True)
    return TargetFocusResult(
        focused=False,
        message="取り込み一覧に該当の項目がありません",
    )


def activity_focus(page: ActivityPage, target: NavigationTarget) -> TargetFocusResult:
    for row in range(page.table.rowCount()):
        cell = page.table.item(row, 2)
        if (
            cell is not None
            and target.primary_id is not None
            and cell.data(Qt.ItemDataRole.UserRole) in target.object_ids
        ):
            page.table.selectRow(row)
            return TargetFocusResult(focused=True)
    return TargetFocusResult(
        focused=False,
        message="アクティビティ一覧に該当の記録がありません",
    )


__all__ = [
    "ActivityPage",
    "CaptureInboxPage",
    "ProjectEntry",
    "ProjectLibraryPage",
    "ProjectLibraryService",
    "ReferenceLibraryPage",
    "SupportPage",
    "activity_focus",
    "inbox_focus",
    "list_recent_revisions",
]
