"""Settings > 保持管理 — capture retention surface (#620, #1025).

A thin presentation layer over :class:`CaptureRetentionService`: it lists the
persisted capture revisions in a searchable/groupable tree, runs the dry-run
``plan_capture_revision_purge`` on demand, and only then offers the actual
purge behind an explicit confirmation. All deletion semantics (reference
safety, transaction, blob GC) stay in ``capture_retention``; this widget
never touches SQLite itself.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...modal_transient import exec_transient
from ..services.capture_retention import (
    CapturePurgePlan,
    CaptureRetentionError,
    CaptureRetentionService,
    CaptureRevisionListing,
)
from ...error_boundary import (
    EXPECTED_OPERATION_ERRORS,
    is_authority_failure,
    report_boundary_failure,
)
from ...tree_item_role import ROLE as _REVISION_ROLE
from ...ui_theme import (
    ControlSize,
    SemanticState,
    SurfaceRole,
    TypographyRole,
    set_control_size,
    set_semantic_state,
    set_surface_role,
    set_typography_role,
)


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024.0 or unit == "GiB":
            return f"{size:,.1f} {unit}" if unit != "B" else f"{int(size):,} B"
        size /= 1024.0
    return f"{value:,} B"


def _plan_status_label(plan: CapturePurgePlan) -> str:
    return {
        "ready": "削除可能",
        "blocked": "依存関係があり削除できません",
        "absent": "このリビジョンは存在しません",
    }[plan.status]


_DEPENDENT_KIND_LABELS = {
    "semantic_promotion": "意味昇格",
    "mesh_composition": "メッシュ合成",
}


def _plan_summary_lines(plan: CapturePurgePlan) -> list[str]:
    """Itemized dry-run report — every category the plan computes is shown
    explicitly so a blocked/shared/referenced revision can never be mistaken
    for a clean 'safe to delete' candidate."""
    lines = [
        f"結果: {_plan_status_label(plan)}",
        f"対象リビジョン: {plan.capture_revision_id}",
        "リンクされた証拠: "
        f"削除対象 {len(plan.deletable_source_evidence_ids)} 件 / "
        f"共有のため保持 {len(plan.retained_source_evidence_ids)} 件",
        "削除対象レコード: "
        f"メッシュバインディング {len(plan.deletable_mesh_binding_ids)} 件 / "
        f"権威レコード {len(plan.deletable_authority_record_ids)} 件 / "
        f"座標権威 {len(plan.deletable_coordinate_authority_ids)} 件 / "
        f"ルームプラン {plan.roomplan_record_count} 件",
        "保持義務（他の取り込みと共有）: "
        f"バインディング {len(plan.retained_mesh_binding_ids)} 件 / "
        f"権威 {len(plan.retained_authority_record_ids)} 件 / "
        f"座標 {len(plan.retained_coordinate_authority_ids)} 件",
        f"回収可能: {_format_bytes(plan.reclaimable_bytes)}"
        f"（コンテンツブロブ {len(plan.reclaimed_blob_sha256)} 件）",
    ]
    if plan.blocking_dependents:
        lines.append("参照・再利用先（削除を妨げている参照）:")
        for dependent in plan.blocking_dependents[:8]:
            lines.append(
                f"・{_DEPENDENT_KIND_LABELS.get(dependent.kind, dependent.kind)}"
                f" {dependent.identifier}（{dependent.detail}）"
            )
        remaining = len(plan.blocking_dependents) - 8
        if remaining > 0:
            lines.append(f"…ほか {remaining} 件")
    else:
        lines.append("参照・再利用先: なし")
    return lines


_SORT_KEYS = {
    "latest": lambda revision: (
        revision.latest_recorded_at_utc,
        revision.capture_revision_id,
    ),
    "revision_id": lambda revision: (revision.capture_revision_id,),
    "series": lambda revision: (
        revision.capture_series_id,
        revision.latest_recorded_at_utc,
        revision.capture_revision_id,
    ),
    "count": lambda revision: (
        revision.ingestion_run_count,
        revision.latest_recorded_at_utc,
        revision.capture_revision_id,
    ),
    "bytes": lambda revision: (
        revision.linked_payload_bytes,
        revision.latest_recorded_at_utc,
        revision.capture_revision_id,
    ),
}

_UNASSIGNED_GROUP = "（プロジェクト未割当）"


class RetentionPolicyWidget(QWidget):
    """Retention picker + dry-run + confirmed purge for capture revisions.

    Candidates are shown in a searchable/sortable/groupable tree keyed by
    ``capture_revision_id`` — the id stays pinned across filter, reorder,
    and refresh operations, and an item that falls out of the current scope
    is reported instead of silently moving the selection (#1025).

    ``is_busy`` (optional) lets the host disable purges while a backup,
    restore, or relocation holds the data lock.
    """

    def __init__(
        self,
        service: CaptureRetentionService,
        *,
        is_busy: Callable[[], bool] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("captureRetentionWidget")
        set_surface_role(self, SurfaceRole.BASE)
        self._service = service
        self._is_busy = is_busy or (lambda: False)
        self._revisions: list[CaptureRevisionListing] = []
        # The purge button arms only for the plan computed against the
        # currently-selected revision, so a stale 'ready' can never leak
        # into a different selection.
        self._planned_revision_id: str | None = None
        # Selection is pinned by capture_revision_id — never by row index —
        # so re-sorting/filtering/refreshing can never silently move it.
        self._pinned_revision_id: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 32)
        layout.setSpacing(16)

        title = QLabel("キャプチャデータの保持管理", self)
        set_typography_role(title, TypographyRole.WORKSPACE_TITLE)
        layout.addWidget(title)

        intro = QLabel(
            "取り込んだキャプチャリビジョンごとに、HTDT内データの削除内容を"
            "事前確認（ドライラン）してから削除できます。元の "
            ".htdtcapture バンドルは外部ファイルのため対象外です。",
            self,
        )
        intro.setWordWrap(True)
        set_typography_role(intro, TypographyRole.BODY)
        layout.addWidget(intro)

        card = QFrame(self)
        card.setObjectName("captureRetentionCard")
        set_surface_role(card, SurfaceRole.RAISED)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(18, 16, 18, 16)
        card_layout.setSpacing(12)

        self.inventory_label = QLabel(card)
        self.inventory_label.setWordWrap(True)
        set_typography_role(self.inventory_label, TypographyRole.SECONDARY)
        card_layout.addWidget(self.inventory_label)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        self.search_edit = QLineEdit(card)
        self.search_edit.setObjectName("captureRetentionSearchEdit")
        self.search_edit.setPlaceholderText(
            "リビジョンID・シリーズ・プロジェクトで検索"
        )
        self.search_edit.setAccessibleName(
            "リビジョンID・シリーズ・プロジェクトで検索"
        )
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._on_scope_changed)
        filter_row.addWidget(self.search_edit, 1)
        self.group_combo = QComboBox(card)
        self.group_combo.setObjectName("captureRetentionGroupCombo")
        self.group_combo.setAccessibleName("グループ化")
        for label, key in (
            ("グループなし", "none"),
            ("シリーズ別", "series"),
            ("プロジェクト別", "project"),
            ("取り込み日別", "date"),
        ):
            self.group_combo.addItem(label, key)
        self.group_combo.currentIndexChanged.connect(self._on_order_changed)
        filter_row.addWidget(self.group_combo)
        self.sort_combo = QComboBox(card)
        self.sort_combo.setObjectName("captureRetentionSortCombo")
        self.sort_combo.setAccessibleName("並び順")
        for label, key in (
            ("取り込み日時（新しい順）", "latest"),
            ("リビジョンID", "revision_id"),
            ("シリーズ", "series"),
            ("取り込み回数", "count"),
            ("サイズ", "bytes"),
        ):
            self.sort_combo.addItem(label, key)
        self.sort_combo.currentIndexChanged.connect(self._on_order_changed)
        filter_row.addWidget(self.sort_combo)
        card_layout.addLayout(filter_row)

        self.count_label = QLabel(card)
        self.count_label.setAccessibleName("表示件数")
        set_typography_role(self.count_label, TypographyRole.SECONDARY)
        card_layout.addWidget(self.count_label)

        self.revision_tree = QTreeWidget(card)
        self.revision_tree.setObjectName("captureRetentionRevisionTree")
        self.revision_tree.setAccessibleName("削除候補のキャプチャリビジョン一覧")
        self.revision_tree.setHeaderLabels(
            (
                "リビジョン",
                "シリーズ",
                "プロジェクト",
                "取り込み",
                "最終取り込み（UTC）",
                "証拠・サイズ",
            )
        )
        header = self.revision_tree.header()
        for column in range(self.revision_tree.columnCount() - 1):
            header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        header.setSectionResizeMode(
            self.revision_tree.columnCount() - 1,
            QHeaderView.ResizeMode.Stretch,
        )
        self.revision_tree.setRootIsDecorated(True)
        self.revision_tree.setUniformRowHeights(True)
        self.revision_tree.setMinimumHeight(160)
        self.revision_tree.currentItemChanged.connect(
            self._on_current_item_changed
        )
        card_layout.addWidget(self.revision_tree, 1)

        self.selection_note = QLabel(card)
        self.selection_note.setObjectName("captureRetentionSelectionNote")
        self.selection_note.setWordWrap(True)
        self.selection_note.setAccessibleName("選択状態メモ")
        set_typography_role(self.selection_note, TypographyRole.SECONDARY)
        self.selection_note.hide()
        card_layout.addWidget(self.selection_note)

        picker_row = QHBoxLayout()
        picker_row.setSpacing(10)
        self.plan_button = QPushButton("削除内容を確認", card)
        self.plan_button.setObjectName("captureRetentionPlanButton")
        set_control_size(self.plan_button, ControlSize.STANDARD)
        self.plan_button.clicked.connect(self._run_dry_run)
        picker_row.addWidget(self.plan_button)
        self.purge_button = QPushButton("削除を実行", card)
        self.purge_button.setObjectName("captureRetentionPurgeButton")
        set_control_size(self.purge_button, ControlSize.STANDARD)
        self.purge_button.clicked.connect(self._confirm_and_purge)
        picker_row.addWidget(self.purge_button)
        picker_row.addStretch(1)
        card_layout.addLayout(picker_row)

        self.plan_label = QLabel(card)
        self.plan_label.setObjectName("captureRetentionPlanLabel")
        self.plan_label.setWordWrap(True)
        self.plan_label.setAccessibleName("削除内容の確認結果")
        set_typography_role(self.plan_label, TypographyRole.BODY)
        self.plan_label.hide()
        card_layout.addWidget(self.plan_label)

        layout.addWidget(card)
        layout.addStretch(1)

        self.refresh()

    # ---- inventory / listing ---------------------------------------------

    def refresh(self) -> None:
        """Reload inventory counts and the revision list.

        A refresh re-reads the persisted state — the underlying reference
        set may have changed (promotion added, purge ran, project switched),
        so any computed plan is invalidated unconditionally.
        """
        try:
            inventory = self._service.inventory()
            self._revisions = list(self._service.list_capture_revisions())
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: inventory refresh — expected failures surface on the inventory label; sealed-store failures and bugs propagate to diagnostics
            if is_authority_failure(exc):
                raise
            report_boundary_failure(exc, operation='キャプチャ棚卸しの読み込み')
            self.inventory_label.setText(
                "キャプチャ情報を読み込めませんでした。"
            )
            self._revisions = []
        else:
            self.inventory_label.setText(
                f"キャプチャ: リビジョン {inventory.capture_revision_count} 件 / "
                f"取り込み {inventory.ingestion_run_count} 回 / "
                f"証拠 {inventory.source_evidence_count} 件 "
                f"({_format_bytes(inventory.source_payload_bytes)}) / "
                f"コンテンツブロブ {inventory.content_blob_count} 件 "
                f"({_format_bytes(inventory.content_blob_bytes)})"
            )

        self._disarm_plan()
        self._rebuild_list()

    def _rebuild_list(self) -> None:
        """Re-create the visible item set from the current filter/sort/group
        controls, then re-pin the previous selection by id (never by row)."""
        needle = self.search_edit.text().strip().lower()
        shown = [
            revision
            for revision in self._revisions
            if not needle or needle in self._haystack(revision)
        ]
        sort_key = str(self.sort_combo.currentData() or "latest")
        key_fn = _SORT_KEYS.get(sort_key, _SORT_KEYS["latest"])
        shown.sort(
            key=key_fn, reverse=sort_key in ("latest", "count", "bytes")
        )
        group_key = str(self.group_combo.currentData() or "none")

        self.revision_tree.blockSignals(True)
        self.revision_tree.clear()
        restore_item: QTreeWidgetItem | None = None
        if group_key == "none":
            for revision in shown:
                item = self._revision_item(revision)
                self.revision_tree.addTopLevelItem(item)
                if revision.capture_revision_id == self._pinned_revision_id:
                    restore_item = item
        else:
            groups: dict[str, QTreeWidgetItem] = {}
            for revision in shown:
                # A revision promoted into several documents appears under
                # each of them — selection stays unique because it pins the
                # revision id, not the row.
                for name in self._group_names(revision, group_key):
                    parent = groups.get(name)
                    if parent is None:
                        parent = QTreeWidgetItem([name])
                        parent.setFlags(
                            parent.flags() & ~Qt.ItemFlag.ItemIsSelectable
                        )
                        parent.setToolTip(0, name)
                        groups[name] = parent
                        self.revision_tree.addTopLevelItem(parent)
                    item = self._revision_item(revision)
                    parent.addChild(item)
                    if (
                        revision.capture_revision_id
                        == self._pinned_revision_id
                    ):
                        restore_item = item
            for parent in groups.values():
                parent.setText(0, f"{parent.text(0)}（{parent.childCount()} 件）")
                parent.setExpanded(True)
        if restore_item is not None:
            self.revision_tree.setCurrentItem(restore_item)
            self.revision_tree.scrollToItem(restore_item)
        self.revision_tree.blockSignals(False)

        self.count_label.setText(
            f"表示 {len(shown)} 件 / 全 {len(self._revisions)} 件"
        )
        self._update_selection_note(shown)
        self._refresh_actions()

    @staticmethod
    def _haystack(revision: CaptureRevisionListing) -> str:
        return " ".join(
            (
                revision.capture_revision_id,
                revision.capture_series_id,
                " ".join(revision.assigned_document_ids),
                revision.latest_recorded_at_utc,
                str(revision.ingestion_run_count),
                str(revision.linked_evidence_count),
                _format_bytes(revision.linked_payload_bytes),
            )
        ).lower()

    @staticmethod
    def _group_names(
        revision: CaptureRevisionListing, group_key: str
    ) -> tuple[str, ...]:
        if group_key == "series":
            return (revision.capture_series_id,)
        if group_key == "project":
            return revision.assigned_document_ids or (_UNASSIGNED_GROUP,)
        if group_key == "date":
            return (revision.latest_recorded_at_utc[:10] or "不明",)
        return ()

    def _revision_item(self, revision) -> QTreeWidgetItem:
        project = (
            "、".join(revision.assigned_document_ids)
            if revision.assigned_document_ids
            else "未割当"
        )
        item = QTreeWidgetItem(
            [
                revision.capture_revision_id,
                revision.capture_series_id,
                project,
                f"{revision.ingestion_run_count} 回",
                revision.latest_recorded_at_utc,
                f"証拠 {revision.linked_evidence_count} 件 · "
                f"{_format_bytes(revision.linked_payload_bytes)}",
            ]
        )
        item.setData(0, _REVISION_ROLE, revision.capture_revision_id)
        item.setToolTip(0, revision.capture_revision_id)
        item.setToolTip(1, revision.capture_series_id)
        item.setToolTip(2, project)
        return item

    def _update_selection_note(self, shown) -> None:
        """#1025: when the pinned selection cannot be shown, say why instead
        of silently pointing at a different revision."""
        pinned = self._pinned_revision_id
        if pinned is None:
            self.selection_note.hide()
            return
        if self.revision_tree.currentItem() is not None:
            self.selection_note.hide()
            return
        by_id = {
            revision.capture_revision_id: revision
            for revision in self._revisions
        }
        if pinned not in by_id:
            self._pinned_revision_id = None
            self.selection_note.setText(
                "選択していたリビジョンは一覧にありません"
                "（削除済みまたは別プロジェクト）。"
            )
            self.selection_note.show()
            return
        if all(
            revision.capture_revision_id != pinned for revision in shown
        ):
            self.selection_note.setText(
                "選択していたリビジョンは絞り込み条件で非表示です。"
                "解除すると選択に戻ります。"
            )
            self.selection_note.show()
            return
        self.selection_note.hide()

    # ---- selection / plan arming ------------------------------------------

    def select_revision(self, revision_id: str) -> bool:
        """Focus the row for ``revision_id`` — navigation/test entry point.
        Only an id that is actually listed can be selected."""
        for index in range(self.revision_tree.topLevelItemCount()):
            top = self.revision_tree.topLevelItem(index)
            if top.data(0, _REVISION_ROLE) == revision_id:
                self.revision_tree.setCurrentItem(top)
                self.revision_tree.scrollToItem(top)
                return True
            for child_index in range(top.childCount()):
                child = top.child(child_index)
                if child.data(0, _REVISION_ROLE) == revision_id:
                    self.revision_tree.setCurrentItem(child)
                    self.revision_tree.scrollToItem(child)
                    return True
        return False

    def listed_revision_ids(self) -> tuple[str, ...]:
        """Revision ids currently present in the tree (post-filter order)."""
        ids: list[str] = []
        for index in range(self.revision_tree.topLevelItemCount()):
            top = self.revision_tree.topLevelItem(index)
            data = top.data(0, _REVISION_ROLE)
            if data:
                ids.append(str(data))
            for child_index in range(top.childCount()):
                data = top.child(child_index).data(0, _REVISION_ROLE)
                if data:
                    ids.append(str(data))
        return tuple(ids)

    def _selected_revision_id(self) -> str | None:
        item = self.revision_tree.currentItem()
        if item is None:
            return None
        data = item.data(0, _REVISION_ROLE)
        return str(data) if data else None

    def _on_current_item_changed(self, current, _previous) -> None:
        new_id = None
        if current is not None:
            data = current.data(0, _REVISION_ROLE)
            new_id = str(data) if data else None
        if new_id != self._pinned_revision_id:
            self._pinned_revision_id = new_id
            self._disarm_plan()
        self._refresh_actions()

    def _on_scope_changed(self, *_args) -> None:
        """Filter edits change which revisions can be selected — the plan
        was computed for the old scope, so it is invalidated."""
        self._disarm_plan()
        self._rebuild_list()

    def _on_order_changed(self, *_args) -> None:
        """Sort/group reorder does not change the candidate scope; the
        selection stays pinned by id and an existing plan stays valid."""
        self._rebuild_list()

    def _disarm_plan(self) -> None:
        self._planned_revision_id = None
        self.plan_label.hide()
        self._refresh_actions()

    def _refresh_actions(self) -> None:
        selected = self._selected_revision_id() is not None
        available = selected and not self._is_busy()
        self.plan_button.setEnabled(available)
        self.purge_button.setEnabled(
            available
            and self._planned_revision_id is not None
            and self._planned_revision_id == self._selected_revision_id()
        )

    def _run_dry_run(self) -> None:
        revision_id = self._selected_revision_id()
        if revision_id is None:
            return
        try:
            plan = self._service.plan_capture_revision_purge(revision_id)
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: purge plan — expected failures surface on the error label; sealed-store failures and bugs propagate to diagnostics
            if is_authority_failure(exc):
                raise
            report_boundary_failure(exc, operation='削除内容の確認')
            self._show_error("削除内容の確認に失敗しました。")
            return
        self._planned_revision_id = (
            revision_id if plan.status == "ready" else None
        )
        self.plan_label.setText("\n".join(_plan_summary_lines(plan)))
        set_semantic_state(
            self.plan_label,
            SemanticState.SUCCESS
            if plan.status == "ready"
            else SemanticState.WARNING,
        )
        self.plan_label.show()
        self._refresh_actions()

    def _confirm_and_purge(self) -> None:
        revision_id = self._selected_revision_id()
        if (
            revision_id is None
            or self._is_busy()
            or revision_id != self._planned_revision_id
        ):
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("キャプチャデータの削除")
        box.setText(
            f"キャプチャリビジョン {revision_id} のHTDT内データを削除します。"
        )
        box.setInformativeText(
            "外部の .htdtcapture バンドルは削除されません。"
            "実行直前に参照可否を再検証し、"
            "削除できない場合は何も変更しません。実行しますか？"
        )
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if exec_transient(box) != QMessageBox.StandardButton.Yes:
            return
        try:
            plan = self._service.purge_capture_revision(revision_id)
        except CaptureRetentionError:
            self.refresh()
            self._show_error(
                "参照関係があるため削除できませんでした。"
                "削除内容の確認で詳細を確認してください。"
            )
            return
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: purge execute — expected failures surface on the error label after refresh; sealed-store failures and bugs propagate to diagnostics
            if is_authority_failure(exc):
                raise
            report_boundary_failure(exc, operation='キャプチャ削除の実行')
            self.refresh()
            self._show_error(
                "削除に失敗しました。データは変更されていません。"
            )
            return
        self.refresh()
        self.plan_label.setText(
            "削除しました: "
            f"証拠 {len(plan.deletable_source_evidence_ids)} 件 / "
            f"{_format_bytes(plan.reclaimable_bytes)} を回収"
        )
        set_semantic_state(self.plan_label, SemanticState.SUCCESS)
        self.plan_label.show()

    def _show_error(self, message: str) -> None:
        self.plan_label.setText(message)
        set_semantic_state(self.plan_label, SemanticState.ERROR)
        self.plan_label.show()
        self._refresh_actions()


__all__ = ["RetentionPolicyWidget"]
