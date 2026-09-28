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
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .build_info import version_string
from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite
from .navigation_target import (
    NavigationTarget,
    NavigationTargetKind,
    navigation_target_from_uri,
)
from .project_library_repository import ProjectLibraryRepository
from .native_diagnostics import diagnostics_dir
from .ui_theme import TypographyRole, set_typography_role
from .user_facing_error import operation_error_message, warn_user
from .workflow_navigation import WorkspaceDeepLink, WorkspaceId
from .workflow_shell import TargetFocusResult


@dataclass(frozen=True, slots=True)
class ProjectEntry:
    """One canonical project row for the Projects listing (#919).

    ``project_id`` is the stable semantic identity used to open a project;
    ``display_name`` is presentation only.
    """

    project_id: str
    document_id: str
    display_name: str
    head_revision_id: str | None
    created_at_utc: str
    revision_count: int


class ProjectLibraryService:
    """Lists canonical projects — read-only over the library authority.

    ``ProjectLibraryRepository`` auto-registers every document that already
    carries scene content, so canonical ``htdt_project_documents`` rows cover
    legacy and unregistered documents alike (#919).
    """

    def __init__(
        self,
        repository: SceneRepository,
        project_library: ProjectLibraryRepository | None = None,
    ) -> None:
        self.path = Path(repository.path)
        self._project_library = project_library or ProjectLibraryRepository(
            repository
        )

    def list_projects(self) -> tuple[ProjectEntry, ...]:
        heads = self._document_heads()
        return tuple(
            ProjectEntry(
                project_id=entry.project_id,
                document_id=entry.document_id,
                display_name=entry.display_name,
                head_revision_id=(
                    None
                    if entry.document_id not in heads
                    else heads[entry.document_id][0]
                ),
                created_at_utc=entry.created_at_utc,
                revision_count=(
                    0
                    if entry.document_id not in heads
                    else heads[entry.document_id][1]
                ),
            )
            for entry in self._project_library.list_projects()
        )

    def _document_heads(self) -> dict[str, tuple[str, int]]:
        if not self.path.is_file():
            return {}
        try:
            with closing(connect_sqlite(self.path)) as connection:
                rows = connection.execute(
                    """
                    SELECT h.document_id AS document_id,
                           h.head_revision_id AS head_revision_id,
                           (SELECT COUNT(*) FROM scene_revisions s
                            WHERE s.document_id = h.document_id) AS revisions
                    FROM scene_document_heads h
                    """
                ).fetchall()
        except sqlite3.Error:
            return {}
        return {
            str(document_id): (str(head_revision_id), int(revisions))
            for document_id, head_revision_id, revisions in rows
        }


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

        self.empty_label = QLabel(
            "まだプロジェクトはありません。"
            "「新規プロジェクト…」から作成できます。"
        )
        set_typography_role(self.empty_label, TypographyRole.SECONDARY)
        self.empty_label.setWordWrap(True)
        self.empty_label.setVisible(False)
        layout.addWidget(self.empty_label)

        self.open_button = QPushButton("開く")
        # Tooltip stays live while disabled so the gating is discoverable.
        self.open_button.setToolTip("一覧からプロジェクトを選択すると開けます")
        self.open_button.setAttribute(
            Qt.WidgetAttribute.WA_AlwaysShowToolTips, True
        )
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
        self.empty_label.setVisible(not entries)
        for entry in entries:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = (
                entry.display_name,
                entry.created_at_utc,
                str(entry.revision_count),
                "●" if entry.document_id == current else "",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, entry.project_id)
                self.table.setItem(row, column, item)
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.open_button.setEnabled(bool(self.table.selectedItems()))

    def _selected_project_id(self) -> str | None:
        items = self.table.selectedItems()
        for item in items:
            value = item.data(Qt.ItemDataRole.UserRole)
            if value:
                return str(value)
        return None

    def _open_selected(self) -> None:
        # Open routes through the stable canonical project_id (#919).
        project_id = self._selected_project_id()
        if project_id:
            self.project_open_requested.emit(project_id)


_INBOX_LINEAGE_ROLE = int(Qt.ItemDataRole.UserRole) + 1

_INBOX_GATE_LABELS = {
    "bundle_validation": "バンドル検証",
    "dependency_state": "依存関係",
    "alignment_state": "整列状態",
    "evidence_conflict_state": "証拠競合",
}
_INBOX_DISPOSITION_LABELS = {
    "pending": "保留中",
    "deferred": "延期",
    "partially_promoted": "一部昇格",
    "promoted": "昇格済",
    "rejected": "却下",
    "superseded": "置換済",
}
_INBOX_PROMOTABILITY_LABELS = {
    "promotable": "昇格可能",
    "partially_promotable": "一部昇格可能",
    "blocked": "昇格不可",
    "complete": "昇格完了",
}


class CaptureInboxPage(QWidget):
    """Capture Inbox: staged deliveries awaiting review (#770).

    The listing stays compact; selecting a row opens the exact item and
    project context (identity, scope, gate facets, promotability) in a
    detail pane so review, defer/reject, and scope assignment never operate
    on a bare list row.
    """

    def __init__(
        self,
        list_items: Callable[[], tuple],
        on_navigate: Callable[[WorkspaceDeepLink], bool],
        *,
        inspect_item: Callable[[str], object] | None = None,
        defer_item: Callable[[str, str], object] | None = None,
        reject_item: Callable[[str, str], object] | None = None,
        resume_item: Callable[[str], object] | None = None,
        list_projects: Callable[[], tuple] | None = None,
        assign_scope: Callable[[str, str], object] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._list_items = list_items
        self._on_navigate = on_navigate
        self._inspect_item = inspect_item
        self._defer_item = defer_item
        self._reject_item = reject_item
        self._resume_item = resume_item
        self._list_projects = list_projects
        self._assign_scope = assign_scope
        layout = _page_layout(
            self,
            "取り込み",
            "取得済みのCapture配送です。項目を選ぶと内容と判断材料を確認できます。",
        )
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ("スコープ", "シリーズ", "分類", "状態", "到着数")
        )
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.table.itemSelectionChanged.connect(self._sync_detail)
        splitter.addWidget(self.table)

        detail_panel = QWidget()
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.setContentsMargins(0, 4, 0, 0)
        self.detail = QLabel("一覧から項目を選択すると詳細を表示します。")
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        set_typography_role(self.detail, TypographyRole.SECONDARY)
        detail_layout.addWidget(self.detail, 1)
        actions = QHBoxLayout()
        self.defer_button = QPushButton("延期…")
        self.defer_button.clicked.connect(lambda: self._dispose("defer"))
        actions.addWidget(self.defer_button)
        self.reject_button = QPushButton("却下…")
        self.reject_button.clicked.connect(lambda: self._dispose("reject"))
        actions.addWidget(self.reject_button)
        self.resume_button = QPushButton("再開")
        self.resume_button.clicked.connect(lambda: self._dispose("resume"))
        actions.addWidget(self.resume_button)
        self.scope_combo = QComboBox()
        actions.addWidget(QLabel("プロジェクト:"))
        actions.addWidget(self.scope_combo, 1)
        self.scope_button = QPushButton("割り当て")
        self.scope_button.clicked.connect(self._apply_scope)
        actions.addWidget(self.scope_button)
        link = QPushButton("測定ワークスペースを開く")
        link.clicked.connect(
            lambda: self._on_navigate(
                WorkspaceDeepLink(WorkspaceId.MEASUREMENT, "import")
            )
        )
        actions.addWidget(link)
        detail_layout.addLayout(actions)
        splitter.addWidget(detail_panel)
        splitter.setStretchFactor(0, 1)
        layout.addWidget(splitter, 1)
        self.refresh()

    def _selected_row_data(self) -> tuple[str, str] | None:
        items = self.table.selectedItems()
        for item in items:
            if item.column() != 0:
                continue
            item_id = item.data(Qt.ItemDataRole.UserRole)
            digest = item.data(_INBOX_LINEAGE_ROLE)
            if item_id and digest:
                return str(item_id), str(digest)
        return None

    def _selected_digest(self) -> str | None:
        data = self._selected_row_data()
        return data[1] if data else None

    def _sync_detail(self) -> None:
        digest = self._selected_digest()
        if digest is None:
            if self.table.rowCount() == 0:
                self.detail.setText(
                    "取り込み待ちの配送はありません。"
                    "配送が到着するとここに表示されます。"
                )
            else:
                self.detail.setText("一覧から項目を選択すると詳細を表示します。")
            self._sync_actions(None)
            return
        inspection = (
            self._inspect_item(digest) if self._inspect_item is not None else None
        )
        if inspection is None:
            self.detail.setText(f"項目を確認できません: {digest[:12]}…")
            self._sync_actions(None)
            return
        self._populate_detail(inspection)
        self._sync_actions(inspection.item.disposition)

    def _populate_detail(self, inspection) -> None:
        item = inspection.item
        flags = (
            "・".join(item.classification_flags)
            if item.classification_flags
            else item.primary_classification
        )
        scope = (
            "（未割り当て）"
            if item.scope == "capture-inbox-unassigned"
            else item.scope
        )
        lines = [
            f"スコープ: {scope}",
            f"項目: {item.inbox_item_id.split(':', 1)[-1][:16]}…"
            f" / 系列 {item.capture_series_id} / リビジョン {item.capture_revision_id}",
            f"分類: {item.primary_classification}（{flags}）",
            f"到着: {item.arrival_source} ×{item.arrival_count}（{item.first_arrived_at_utc}）",
            "ゲート: "
            + " / ".join(
                f"{_INBOX_GATE_LABELS[key]}={getattr(item, key)}"
                + (
                    f"（{getattr(item, key[:-5] + '_detail')}）"
                    if getattr(item, key[:-5] + '_detail', '')
                    else ""
                )
                for key in _INBOX_GATE_LABELS
            ),
            f"昇格可能性: {_INBOX_PROMOTABILITY_LABELS.get(inspection.promotability, inspection.promotability)}"
            + (
                f"（昇格対象: {'・'.join(inspection.available_authority_kinds) or 'なし'}）"
            ),
            (
                "昇格済: "
                + ("・".join(inspection.promoted_authority_kinds) or "なし")
                + " / 不可: "
                + ("・".join(inspection.blocked_authority_kinds) or "なし")
            ),
            f"内容: 証拠{inspection.source_evidence_count} / "
            f"間取り{inspection.roomplan_record_count} / "
            f"メッシュ{inspection.raw_mesh_count} / "
            f"権威{inspection.authority_record_count}",
            f"状態: {_INBOX_DISPOSITION_LABELS.get(item.disposition, item.disposition)}"
            + (f" — {item.disposition_reason}" if item.disposition_reason else ""),
        ]
        if item.operator_notes:
            lines.append(f"メモ: {item.operator_notes}")
        self.detail.setText("\n".join(lines))
        self._populate_scope_combo(item.scope)

    def _populate_scope_combo(self, current_scope: str) -> None:
        self.scope_combo.clear()
        self.scope_combo.addItem("（未割り当て）", "capture-inbox-unassigned")
        if self._list_projects is None:
            return
        current_index = 0
        for entry in self._list_projects():
            label = getattr(entry, "display_name", "") or getattr(
                entry, "project_id", ""
            )
            document_id = getattr(entry, "document_id", "")
            if not document_id:
                continue
            self.scope_combo.addItem(label, document_id)
            if document_id == current_scope:
                current_index = self.scope_combo.count() - 1
        self.scope_combo.setCurrentIndex(current_index)

    def _sync_actions(self, disposition: str | None) -> None:
        self.defer_button.setEnabled(
            self._defer_item is not None and disposition == "pending"
        )
        self.reject_button.setEnabled(
            self._reject_item is not None
            and disposition in ("pending", "deferred")
        )
        self.resume_button.setEnabled(
            self._resume_item is not None
            and disposition in ("deferred", "rejected")
        )
        self.scope_button.setEnabled(
            self._assign_scope is not None
            and disposition in ("pending", "deferred")
        )

    def _dispose(self, action: str) -> None:
        digest = self._selected_digest()
        if digest is None:
            return
        handler = {
            "defer": self._defer_item,
            "reject": self._reject_item,
            "resume": self._resume_item,
        }[action]
        if handler is None:
            return
        reason = ""
        if action in ("defer", "reject"):
            title = {"defer": "延期", "reject": "却下"}[action]
            reason, ok = QInputDialog.getText(
                self, title, f"{title}の理由を入力してください。"
            )
            if not ok or not reason.strip():
                return
            reason = reason.strip()
        try:
            handler(digest, reason) if reason else handler(digest)
        except Exception as exc:
            warn_user(self, "取り込みできませんでした", exc)
            return
        self._refresh_keep_selection()

    def _apply_scope(self) -> None:
        digest = self._selected_digest()
        scope = self.scope_combo.currentData()
        if digest is None or not scope or self._assign_scope is None:
            return
        try:
            self._assign_scope(digest, str(scope))
        except Exception as exc:
            warn_user(self, "プロジェクト領域を割り当てできませんでした", exc)
            return
        self._refresh_keep_selection()

    def _refresh_keep_selection(self) -> None:
        selected = self._selected_row_data()
        self.refresh()
        if selected is None:
            return
        for row in range(self.table.rowCount()):
            cell = self.table.item(row, 0)
            if (
                cell is not None
                and cell.data(Qt.ItemDataRole.UserRole) == selected[0]
            ):
                self.table.selectRow(row)
                break
        self._sync_detail()

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
                    cell.setData(_INBOX_LINEAGE_ROLE, item.lineage_digest)
                self.table.setItem(row, column, cell)
        self._sync_detail()


_OPERATION_STATE_LABELS = {
    "queued": "待機中",
    "preflighting": "準備中",
    "running": "実行中",
    "cancellation_requested": "キャンセル要求中",
    "cancelled": "キャンセル",
    "completed": "完了",
    "failed": "失敗",
    "completed_for_historical_input": "完了（旧入力）",
    "result_stale": "結果が古い",
}


class ActivityPage(QWidget):
    """Activity: app operations, the projected project timeline, revisions.

    Sections (all read-mostly):

    * ``operations`` — live/recent :class:`ApplicationOperation` rows from the
      application-scoped ActivityCenter (data-management jobs, etc.),
    * ``timeline`` — human-readable project events projected by
      :class:`CadProjectActivityService` (variants, captures, measurements,
      checkpoints, notes); a row's nav URI deep link opens on double-click,
    * ``revisions`` — the raw persisted scene-revision ledger (latest first).
    """

    def __init__(
        self,
        list_revisions: Callable[[int], tuple],
        list_operations: Callable[[], tuple] | None = None,
        list_events: Callable[[int], tuple] | None = None,
        open_link: Callable[[str], bool] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._list_revisions = list_revisions
        self._list_operations = list_operations
        self._list_events = list_events
        self._open_link = open_link
        layout = _page_layout(
            self,
            "アクティビティ",
            "実行中の操作・プロジェクトの記録（最新順）です。",
        )
        if self._list_operations is not None:
            operations_heading = QLabel("操作")
            set_typography_role(
                operations_heading, TypographyRole.SECTION_TITLE
            )
            layout.addWidget(operations_heading)
            self.operations_table = QTableWidget(0, 3)
            self.operations_table.setHorizontalHeaderLabels(
                ("状態", "操作", "更新時刻")
            )
            self.operations_table.horizontalHeader().setSectionResizeMode(
                1, QHeaderView.ResizeMode.Stretch
            )
            self.operations_table.setEditTriggers(
                QTableWidget.EditTrigger.NoEditTriggers
            )
            layout.addWidget(self.operations_table, 1)
        else:
            self.operations_table = None
        if self._list_events is not None:
            timeline_heading = QLabel("プロジェクトタイムライン")
            set_typography_role(
                timeline_heading, TypographyRole.SECTION_TITLE
            )
            layout.addWidget(timeline_heading)
            self.events_table = QTableWidget(0, 3)
            self.events_table.setHorizontalHeaderLabels(
                ("時刻", "内容", "詳細")
            )
            self.events_table.horizontalHeader().setSectionResizeMode(
                1, QHeaderView.ResizeMode.Stretch
            )
            self.events_table.setEditTriggers(
                QTableWidget.EditTrigger.NoEditTriggers
            )
            self.events_table.itemActivated.connect(self._activate_event)
            self.events_table.itemDoubleClicked.connect(self._activate_event)
            layout.addWidget(self.events_table, 1)
        else:
            self.events_table = None
        revisions_heading = QLabel("リビジョン履歴")
        set_typography_role(revisions_heading, TypographyRole.SECTION_TITLE)
        layout.addWidget(revisions_heading)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(("時刻", "プロジェクト", "リビジョン"))
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, 1)
        self.empty_label = QLabel(
            "まだ記録はありません。保存や昇格を行うとここに表示されます。"
        )
        set_typography_role(self.empty_label, TypographyRole.SECONDARY)
        self.empty_label.setWordWrap(True)
        self.empty_label.setVisible(False)
        layout.addWidget(self.empty_label)
        self.refresh()

    def _activate_event(self, item: QTableWidgetItem) -> None:
        anchor = self.events_table.item(item.row(), 0)
        link = anchor.data(Qt.ItemDataRole.UserRole) if anchor is not None else None
        if link and self._open_link is not None:
            self._open_link(str(link))

    def refresh(self) -> None:
        if self.operations_table is not None and self._list_operations is not None:
            self.operations_table.setRowCount(0)
            for operation in self._list_operations():
                row = self.operations_table.rowCount()
                self.operations_table.insertRow(row)
                state = getattr(operation.state, "value", operation.state)
                detail = (
                    operation.error_summary
                    or operation.result_summary
                    or operation.operation_kind
                )
                for column, value in enumerate(
                    (
                        _OPERATION_STATE_LABELS.get(state, str(state)),
                        f"{operation.title} — {detail}",
                        operation.updated_at,
                    )
                ):
                    cell = QTableWidgetItem(str(value))
                    if column == 0:
                        cell.setData(
                            Qt.ItemDataRole.UserRole, operation.operation_id
                        )
                    self.operations_table.setItem(row, column, cell)
        if self.events_table is not None and self._list_events is not None:
            self.events_table.setRowCount(0)
            for event in self._list_events(50):
                row = self.events_table.rowCount()
                self.events_table.insertRow(row)
                for column, value in enumerate(
                    (event.occurred_at_utc, event.title, event.detail or "")
                ):
                    cell = QTableWidgetItem(str(value))
                    if column == 0 and event.deep_link is not None:
                        cell.setData(Qt.ItemDataRole.UserRole, event.deep_link)
                    self.events_table.setItem(row, column, cell)
        self.table.setRowCount(0)
        rows = self._list_revisions(50)
        self.empty_label.setVisible(not rows)
        for created_at, document_id, revision_id in rows:
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
        with closing(connect_sqlite(path)) as connection:
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


_LIBRARY_FAMILY_TITLES = {
    "equipment": "機材・スピーカー定義",
    "treatment": "吸音・処理材",
    "target_curve": "目標カーブ",
    "standard_profile": "基準プロファイル",
    "instrument": "測定機器",
    "material": "音響材料",
    "operating_profile": "動作プロファイル",
}

_LIBRARY_SCOPE_LABELS = {
    "builtin": "同梱",
    "user_library": "ユーザーライブラリ",
    "project_local": "プロジェクト",
    "imported_dependency": "依存として取り込み",
    "historical": "履歴",
}


class ReferenceLibraryPage(QWidget):
    """Reference library: equipment/source definitions shared across projects.

    ``library_index`` (the #630 hub read model) renders one additional
    read-only section per registered authority family — speakers, materials,
    standards profiles — so shared authorities are discoverable in one place.
    """

    manage_requested = Signal()

    def __init__(
        self,
        list_definitions: Callable[[], tuple],
        library_index=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._list_definitions = list_definitions
        self._library_index = library_index
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

        self._family_frames: dict[str, tuple[QLabel, QTableWidget]] = {}
        if library_index is not None:
            for family in library_index.families():
                header = QLabel(
                    _LIBRARY_FAMILY_TITLES.get(str(family), str(family)),
                    self,
                )
                set_typography_role(header, TypographyRole.SECTION_TITLE)
                table = QTableWidget(0, 4, self)
                table.setHorizontalHeaderLabels(
                    ("名前", "区分", "スコープ", "バージョン")
                )
                table.horizontalHeader().setSectionResizeMode(
                    0, QHeaderView.ResizeMode.Stretch
                )
                table.setEditTriggers(
                    QTableWidget.EditTrigger.NoEditTriggers
                )
                table.setSelectionBehavior(
                    QTableWidget.SelectionBehavior.SelectRows
                )
                header.hide()
                table.hide()
                layout.addWidget(header)
                layout.addWidget(table)
                self._family_frames[str(family)] = (header, table)
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
                item = QTableWidgetItem(str(value))
                if column == 1:
                    item.setData(
                        Qt.ItemDataRole.UserRole,
                        getattr(definition, "definition_id", ""),
                    )
                self.table.setItem(row, column, item)
        self._refresh_family_sections()

    def _refresh_family_sections(self) -> None:
        if self._library_index is None:
            return
        for family, (header, table) in self._family_frames.items():
            try:
                entries = self._library_index.entries(family=family)
            except Exception:  # noqa: BLE001 - a broken provider must not blank the page
                entries = ()
            header.setVisible(bool(entries))
            table.setVisible(bool(entries))
            table.setRowCount(0)
            for entry in entries:
                row = table.rowCount()
                table.insertRow(row)
                for column, value in enumerate(
                    (
                        entry.display_name,
                        entry.capability_summary
                        or entry.source_summary
                        or "",
                        _LIBRARY_SCOPE_LABELS.get(
                            str(entry.scope), str(entry.scope)
                        ),
                        entry.version,
                    )
                ):
                    cell = QTableWidgetItem(str(value))
                    if column == 0:
                        cell.setData(
                            Qt.ItemDataRole.UserRole, entry.semantic_key
                        )
                    table.setItem(row, column, cell)

    def focus_definition(self, definition_id: str) -> TargetFocusResult:
        for row in range(self.table.rowCount()):
            model = self.table.item(row, 1)
            if (
                model is not None
                and model.data(Qt.ItemDataRole.UserRole) == definition_id
            ):
                self.table.selectRow(row)
                return TargetFocusResult(focused=True)
        return TargetFocusResult(
            focused=False,
            message="ライブラリ内に該当の定義が見つかりません",
        )


class SupportPage(QWidget):
    """Support: diagnostics locations, version, and package export (#604)."""

    def __init__(
        self,
        data_dir: Path,
        status_provider: Callable[[], tuple[str, ...]] | None = None,
        export_diagnostics: Callable[[QWidget], str | None] | None = None,
        open_authority_graph: Callable[[QWidget], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._status_provider = status_provider
        self._export_diagnostics = export_diagnostics
        self._open_authority_graph = open_authority_graph
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
        if self._open_authority_graph is not None:
            self.authority_button = QPushButton("権威グラフを開く", self)
            self.authority_button.setObjectName("supportOpenAuthorityGraph")
            self.authority_button.clicked.connect(
                lambda: self._open_authority_graph(self)
            )
            layout.addWidget(self.authority_button)
        else:
            self.authority_button = None
        if self._export_diagnostics is not None:
            self.export_button = QPushButton("診断パッケージをエクスポート", self)
            self.export_button.setObjectName("supportExportDiagnostics")
            self.export_button.clicked.connect(self._run_export)
            layout.addWidget(self.export_button)
            self.export_status = QLabel(self)
            self.export_status.setObjectName("supportExportStatus")
            self.export_status.setWordWrap(True)
            layout.addWidget(self.export_status)
        else:
            self.export_button = None
            self.export_status = None
        layout.addStretch(1)
        self.refresh()

    def _run_export(self) -> None:
        try:
            path = self._export_diagnostics(self)
        except Exception as exc:
            self.export_status.setText(
                "診断パッケージを作成できませんでした: "
                f"{operation_error_message(exc)}"
            )
            return
        if path is not None:
            self.export_status.setText(f"保存しました: {path}")

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


def projects_focus(
    page: ProjectLibraryPage, target: NavigationTarget
) -> TargetFocusResult:
    project_id = target.primary_id
    if project_id is None:
        return TargetFocusResult(focused=True)
    for row in range(page.table.rowCount()):
        cell = page.table.item(row, 0)
        if cell is not None and cell.data(Qt.ItemDataRole.UserRole) == project_id:
            page.table.selectRow(row)
            return TargetFocusResult(focused=True)
    return TargetFocusResult(
        focused=False,
        message="プロジェクト一覧に該当の項目がありません",
    )


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


def _event_row_matches(cell: QTableWidgetItem, target: NavigationTarget) -> bool:
    """A timeline row matches when its stored nav URI targets the same id."""

    link = cell.data(Qt.ItemDataRole.UserRole)
    if not isinstance(link, str):
        return False
    try:
        linked = navigation_target_from_uri(link)
    except ValueError:
        return False
    if linked.kind is not target.kind:
        return False
    return bool(set(linked.object_ids) & set(target.object_ids))


def activity_focus(page: ActivityPage, target: NavigationTarget) -> TargetFocusResult:
    if target.primary_id is None:
        return TargetFocusResult(focused=True)
    if page.events_table is not None:
        for row in range(page.events_table.rowCount()):
            cell = page.events_table.item(row, 0)
            if cell is not None and _event_row_matches(cell, target):
                page.events_table.selectRow(row)
                page.events_table.scrollToItem(cell)
                return TargetFocusResult(focused=True)
    if page.operations_table is not None:
        for row in range(page.operations_table.rowCount()):
            cell = page.operations_table.item(row, 0)
            if (
                cell is not None
                and cell.data(Qt.ItemDataRole.UserRole) in target.object_ids
            ):
                page.operations_table.selectRow(row)
                page.operations_table.scrollToItem(cell)
                return TargetFocusResult(focused=True)
    for row in range(page.table.rowCount()):
        cell = page.table.item(row, 2)
        if (
            cell is not None
            and cell.data(Qt.ItemDataRole.UserRole) in target.object_ids
        ):
            page.table.selectRow(row)
            page.table.scrollToItem(cell)
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
    "projects_focus",
    "list_recent_revisions",
]
