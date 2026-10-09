"""Scene object list for the workflow-first Room workspace (#482, #978).

A lightweight always-visible object browser that stays selection-synced with
the 3D viewport, exposes hide/show + lock/unlock per entity and in batch, and
supports Undo-safe delete. Hidden objects remain listed (visibility toggled
off ≠ deleted); locked objects are inspectable but not editable.

#978 adds mass-room triage on top of the same surface:

- Instant search across name / kind label / speaker role / stable entity ID
  plus state filters (選択中のみ・非表示・ロック中・問題あり). A zero-match
  state names the active condition and offers a reset path.
- Entities are grouped by kind (取込メッシュ is its own group so imported
  mesh bodies stay findable) with expand/collapse headers. Group expansion
  state survives refreshes; every row edit still resolves by stable entity
  ID — never by row index.
- 「選択のみ表示」/「隔離解除」 reuse the workspace's existing
  ``isolate_selection``/``clear_isolation`` — isolation is a *temporary*
  display state (隔離中), explicitly distinct from 隠す/ロック which are
  persistent view-state edits.
- List → 3D focus (フォーカス button / double-click) and 3D pick → list
  auto-scroll + highlight stay synchronized. Selected entities hidden by
  the active filter are reported as ``フィルタ外の選択 N件`` instead of
  silently disappearing.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPalette
from shapely.geometry import Point
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .cad_scene import (
    SceneDocument,
    SceneEntity,
    duplicated_speaker_roles,
    is_unassigned_speaker_role,
)
from .geometry import polygon_from_vertices
from .ui_theme import (
    ControlSize,
    set_control_size,
    set_typography_role,
    TypographyRole,
)

_ENTITY_ROLE = Qt.ItemDataRole.UserRole
_GROUP_ROLE = Qt.ItemDataRole.UserRole + 1

OBJECT_FILTER_ALL = 'all'
OBJECT_FILTER_SELECTED = 'selected'
OBJECT_FILTER_HIDDEN = 'hidden'
OBJECT_FILTER_LOCKED = 'locked'
OBJECT_FILTER_PROBLEM = 'problem'

_OBJECT_FILTER_ITEMS: tuple[tuple[str, str], ...] = (
    ('全て', OBJECT_FILTER_ALL),
    ('選択中のみ', OBJECT_FILTER_SELECTED),
    ('非表示', OBJECT_FILTER_HIDDEN),
    ('ロック中', OBJECT_FILTER_LOCKED),
    ('問題あり', OBJECT_FILTER_PROBLEM),
)
_OBJECT_FILTER_LABELS = {key: label for label, key in _OBJECT_FILTER_ITEMS}

_IMPORTED_MESH_GROUP = 'imported_mesh'

#: Group display order. Kinds not listed here (future EntityKind values)
#: append at the end in first-seen document order.
_OBJECT_GROUP_ORDER: tuple[str, ...] = (
    'speaker',
    'seat',
    'screen',
    'display',
    'projector',
    'av_equipment',
    'riser',
    'furniture',
    _IMPORTED_MESH_GROUP,
    'measurement_point',
)
_IMPORTED_MESH_GROUP_LABEL = '取込メッシュ'


def entity_group_key(entity: SceneEntity) -> str:
    """Group bucket for one entity; imported mesh bodies get their own."""

    body = getattr(entity, 'body_geometry', None)
    if getattr(body, 'kind', None) == 'mesh_asset':
        return _IMPORTED_MESH_GROUP
    return entity.kind


def entity_problem_reasons(document: SceneDocument) -> dict[str, tuple[str, ...]]:
    """Per-entity attention reasons — honest, document-local signals only.

    Mirrors the room-journey quality gates (unassigned channel role,
    duplicated channel role) plus a geometry check (entity centre outside
    the room footprint/height). No new scene truth is introduced.
    """

    entities = document.entities
    duplicated_roles = set(duplicated_speaker_roles(entities))
    reasons: dict[str, list[str]] = {}

    room = document.room
    footprint = None
    if room is not None and room.footprint_vertices:
        footprint = polygon_from_vertices(
            [(vertex.x_m, vertex.y_m) for vertex in room.footprint_vertices]
        )
    bounds = room.bounds_m if room is not None else None
    height_m = float(room.height_m) if room is not None else None

    for entity in entities:
        problems: list[str] = []
        if entity.kind == 'speaker':
            if is_unassigned_speaker_role(entity.speaker_role):
                problems.append('チャンネルロールが未割当です')
            elif entity.speaker_role in duplicated_roles:
                problems.append(f'ロール「{entity.speaker_role}」が重複しています')
        if bounds is not None and height_m is not None:
            inside_xy = (
                footprint.covers(
                    Point(float(entity.position.x_m), float(entity.position.y_m))
                )
                if footprint is not None
                else (
                    bounds[0] <= entity.position.x_m <= bounds[2]
                    and bounds[1] <= entity.position.y_m <= bounds[3]
                )
            )
            if not inside_xy or not (0.0 <= entity.position.z_m <= height_m):
                problems.append('位置が部屋の範囲外です')
        if problems:
            reasons[entity.entity_id] = tuple(problems)
    return reasons


def _entity_search_haystack(entity: SceneEntity, kind_labels: dict[str, str]) -> str:
    return ' '.join(
        part
        for part in (
            entity.name,
            entity.kind,
            kind_labels.get(entity.kind, ''),
            entity.speaker_role or '',
            entity.entity_id,
        )
        if part
    ).casefold()


class RoomObjectsPanel(QWidget):
    """Object list + search/filter/groups + visibility/lock/delete/isolation."""

    selectionRequested = Signal(tuple, object)  # (ordered entity ids, primary id | None)
    hideRequested = Signal(tuple, bool)
    lockRequested = Signal(tuple, bool)
    deleteRequested = Signal(tuple)
    isolationRequested = Signal()
    isolationClearRequested = Signal()
    focusRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.summary = QLabel("オブジェクト一覧")
        self.summary.setWordWrap(True)
        set_typography_role(self.summary, TypographyRole.SECONDARY)
        layout.addWidget(self.summary)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(4)
        self.search_edit = QLineEdit(self)
        self.search_edit.setObjectName("objectsSearch")
        self.search_edit.setPlaceholderText("名前・種類・ロール・IDで検索…")
        self.search_edit.setAccessibleName("オブジェクトを検索")
        self.search_edit.setToolTip(
            "名前・種類・スピーカーロール・IDの部分一致で一覧を絞り込みます"
        )
        self.search_edit.textChanged.connect(self._refilter)
        filter_row.addWidget(self.search_edit, 1)
        self.filter_combo = QComboBox(self)
        self.filter_combo.setObjectName("objectsFilter")
        for label, key in _OBJECT_FILTER_ITEMS:
            self.filter_combo.addItem(label, key)
        self.filter_combo.setAccessibleName("表示フィルタ")
        self.filter_combo.setToolTip(
            "選択中・非表示・ロック中・問題ありの状態で絞り込みます"
        )
        self.filter_combo.currentIndexChanged.connect(lambda _i: self._refilter())
        filter_row.addWidget(self.filter_combo)
        self.reset_filter_button = QPushButton("リセット", self)
        self.reset_filter_button.setObjectName("objectsFilterReset")
        self.reset_filter_button.setAccessibleName("絞り込み条件をリセット")
        self.reset_filter_button.setToolTip(
            "検索とフィルタを解除して全項目を表示します"
        )
        set_control_size(self.reset_filter_button, ControlSize.COMPACT)
        self.reset_filter_button.clicked.connect(self.reset_filters)
        filter_row.addWidget(self.reset_filter_button)
        layout.addLayout(filter_row)

        view_row = QHBoxLayout()
        view_row.setSpacing(4)
        self.isolate_button = QPushButton("選択のみ表示", self)
        self.isolate_button.setObjectName("objectsIsolate")
        self.isolate_button.setAccessibleName("選択した物体だけを表示")
        self.isolate_button.setToolTip(
            "選択した物体だけを3D上に表示します（一時的な表示切替で、"
            "「隠す」とは別の操作です）"
        )
        self.clear_isolation_button = QPushButton("隔離解除", self)
        self.clear_isolation_button.setObjectName("objectsClearIsolation")
        self.clear_isolation_button.setAccessibleName("一時表示を解除")
        self.clear_isolation_button.setToolTip(
            "一時的な表示切替を解除して、隔離前の表示状態に戻します"
        )
        self.focus_button = QPushButton("フォーカス", self)
        self.focus_button.setObjectName("objectsFocus")
        self.focus_button.setAccessibleName("選択物を3Dでフォーカス")
        self.focus_button.setToolTip(
            "選択した物体に3Dカメラを合わせます（ダブルクリックでも同じ操作です）"
        )
        for button in (
            self.isolate_button,
            self.clear_isolation_button,
            self.focus_button,
        ):
            set_control_size(button, ControlSize.COMPACT)
            view_row.addWidget(button)
        view_row.addStretch(1)
        layout.addLayout(view_row)

        self.isolate_button.clicked.connect(self.isolationRequested)
        self.clear_isolation_button.clicked.connect(self.isolationClearRequested)
        self.focus_button.clicked.connect(self.focusRequested)

        self.tree = QTreeWidget()
        self.tree.setAccessibleName("オブジェクト一覧")
        self.tree.setHeaderLabels(("名前", "種類", "表示", "ロック"))
        header = self.tree.headerItem()
        if header is not None:
            header.setToolTip(0, "物体の表示名 · ▶は選択中の主対象 · ⚠は要対応")
            header.setToolTip(1, "物体の種類（スピーカー・座席・家具など）")
            header.setToolTip(2, "3D上の表示状態 · ●=表示中、—=非表示（削除ではありません）")
            header.setToolTip(3, "ロック中は誤って移動・編集できないよう保護されます")
        self.tree.setToolTip(
            "部屋に置いた物体の一覧 · クリックで選択、Ctrl+クリックで複数選択、"
            "ダブルクリックで3Dフォーカス"
        )
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # Name column takes all slack — the ~265px dock leaves only ~65px
        # otherwise; kind/state markers shrink to their short content.
        self.tree.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        for _col in (1, 2, 3):
            self.tree.header().setSectionResizeMode(
                _col, QHeaderView.ResizeMode.ResizeToContents
            )
        # Kind groups are real tree parents — the header keeps its expander.
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.itemSelectionChanged.connect(self._emit_selection)
        self.tree.itemExpanded.connect(self._group_expanded)
        self.tree.itemCollapsed.connect(self._group_collapsed)
        self.tree.itemDoubleClicked.connect(self._item_double_clicked)
        layout.addWidget(self.tree, stretch=1)

        button_row = QHBoxLayout()
        button_row.setSpacing(4)
        self.hide_button = QPushButton("隠す")
        self.hide_button.setToolTip(
            "選択した物体を3D上で非表示にします（一覧には残ります）"
        )
        self.show_button = QPushButton("表示")
        self.show_button.setToolTip("非表示の物体を3D上に再表示します")
        self.lock_button = QPushButton("ロック")
        self.lock_button.setToolTip(
            "選択した物体をロックし、誤った移動・編集を防ぎます"
        )
        self.unlock_button = QPushButton("解除")
        self.unlock_button.setToolTip("ロックを解除して編集可能に戻します")
        self.delete_button = QPushButton("削除")
        self.delete_button.setToolTip(
            "選択した物体を削除します（Ctrl+Zで元に戻せます）"
        )
        for button in (
            self.hide_button,
            self.show_button,
            self.lock_button,
            self.unlock_button,
            self.delete_button,
        ):
            # Compact padding keeps the 5-button row inside the 260-320px dock.
            set_control_size(button, ControlSize.COMPACT)
            button_row.addWidget(button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        self.hide_button.clicked.connect(lambda: self.hideRequested.emit(self.selected_entity_ids(), True))
        self.show_button.clicked.connect(lambda: self.hideRequested.emit(self.selected_entity_ids(), False))
        self.lock_button.clicked.connect(lambda: self.lockRequested.emit(self.selected_entity_ids(), True))
        self.unlock_button.clicked.connect(lambda: self.lockRequested.emit(self.selected_entity_ids(), False))
        self.delete_button.clicked.connect(lambda: self.deleteRequested.emit(self.selected_entity_ids()))

        self._syncing = False
        self._collapsed_groups: set[str] = set()
        self._entity_items: dict[str, QTreeWidgetItem] = {}
        self._last_document: SceneDocument | None = None
        self._last_state: dict[str, object] = {}
        self._filter_active = False

    # -- public helpers ----------------------------------------------------

    def selected_entity_ids(self) -> tuple[str, ...]:
        return tuple(
            str(item.data(0, _ENTITY_ROLE))
            for item in self.tree.selectedItems()
            if item.data(0, _ENTITY_ROLE) is not None
        )

    def item_for_entity(self, entity_id: str) -> QTreeWidgetItem | None:
        """Row for one stable entity ID, or None when filtered out."""

        return self._entity_items.get(entity_id)

    def reset_filters(self) -> None:
        """Clear search text and restore the all-items filter."""

        self._syncing = True
        try:
            self.search_edit.clear()
            self.filter_combo.setCurrentIndex(0)
        finally:
            self._syncing = False
        self._refilter()

    # -- internal ----------------------------------------------------------

    def _emit_selection(self) -> None:
        if self._syncing:
            return
        ids = self.selected_entity_ids()
        primary = ids[-1] if ids else None
        self.selectionRequested.emit(ids, primary)

    def _group_key(self, item: QTreeWidgetItem) -> str | None:
        value = item.data(0, _GROUP_ROLE)
        return str(value) if value is not None else None

    def _group_expanded(self, item: QTreeWidgetItem) -> None:
        key = self._group_key(item)
        if key is not None:
            self._collapsed_groups.discard(key)

    def _group_collapsed(self, item: QTreeWidgetItem) -> None:
        key = self._group_key(item)
        if key is not None:
            self._collapsed_groups.add(key)

    def _item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        if item.data(0, _ENTITY_ROLE) is not None:
            self.focusRequested.emit()

    def _filters(self) -> tuple[str, str]:
        return (
            self.search_edit.text().strip().casefold(),
            str(self.filter_combo.currentData() or OBJECT_FILTER_ALL),
        )

    def _refilter(self) -> None:
        if self._syncing or self._last_document is None:
            return
        self.sync_document(self._last_document, **self._last_state)

    def _matches(
        self,
        entity: SceneEntity,
        *,
        needle: str,
        filter_key: str,
        selected: frozenset[str],
        hidden: frozenset[str],
        locked: frozenset[str],
        problem_ids: frozenset[str],
        kind_labels: dict[str, str],
    ) -> bool:
        entity_id = entity.entity_id
        if filter_key == OBJECT_FILTER_SELECTED and entity_id not in selected:
            return False
        if filter_key == OBJECT_FILTER_HIDDEN and entity_id not in hidden:
            return False
        if filter_key == OBJECT_FILTER_LOCKED and entity_id not in locked:
            return False
        if filter_key == OBJECT_FILTER_PROBLEM and entity_id not in problem_ids:
            return False
        if needle and needle not in _entity_search_haystack(entity, kind_labels):
            return False
        return True

    def sync_document(
        self,
        document: SceneDocument,
        *,
        selected_ids: tuple[str, ...],
        primary_id: str | None,
        hidden_ids: set[str] | frozenset[str],
        locked_ids: set[str] | frozenset[str],
        kind_labels: dict[str, str],
        isolation_active: bool = False,
    ) -> None:
        """Refresh rows from document + view state (call on every refresh)."""

        self._last_document = document
        self._last_state = dict(
            selected_ids=selected_ids,
            primary_id=primary_id,
            hidden_ids=hidden_ids,
            locked_ids=locked_ids,
            kind_labels=kind_labels,
            isolation_active=isolation_active,
        )

        self._syncing = True
        try:
            selected = frozenset(selected_ids)
            hidden = frozenset(hidden_ids)
            locked = frozenset(locked_ids)
            problems = entity_problem_reasons(document)
            problem_ids = frozenset(problems)
            needle, filter_key = self._filters()
            self._filter_active = bool(needle) or filter_key != OBJECT_FILTER_ALL
            self.reset_filter_button.setEnabled(self._filter_active)

            self.tree.clear()
            self._entity_items = {}
            hidden_count = 0
            locked_count = 0
            shown_count = 0
            filtered_out_selected = 0
            current_item: QTreeWidgetItem | None = None
            primary_group_key: str | None = None

            visible: dict[str, list[SceneEntity]] = {}
            group_order: list[str] = []
            for entity in document.entities:
                if entity.entity_id in hidden:
                    hidden_count += 1
                if entity.entity_id in locked:
                    locked_count += 1
                if not self._matches(
                    entity,
                    needle=needle,
                    filter_key=filter_key,
                    selected=selected,
                    hidden=hidden,
                    locked=locked,
                    problem_ids=problem_ids,
                    kind_labels=kind_labels,
                ):
                    if entity.entity_id in selected:
                        filtered_out_selected += 1
                    continue
                shown_count += 1
                key = entity_group_key(entity)
                if key not in visible:
                    visible[key] = []
                    group_order.append(key)
                visible[key].append(entity)
                if entity.entity_id == primary_id:
                    primary_group_key = key

            ordered_keys = [
                key for key in _OBJECT_GROUP_ORDER if key in visible
            ] + [
                key for key in group_order if key not in _OBJECT_GROUP_ORDER
            ]
            for key in ordered_keys:
                group_label = (
                    _IMPORTED_MESH_GROUP_LABEL
                    if key == _IMPORTED_MESH_GROUP
                    else kind_labels.get(key, key)
                )
                group_item = QTreeWidgetItem([f"{group_label}（{len(visible[key])}）"])
                group_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                group_item.setData(0, _GROUP_ROLE, key)
                group_item.setToolTip(
                    0,
                    f"{group_label}のグループ · クリックで展開/折りたたみ",
                )
                self.tree.addTopLevelItem(group_item)
                for entity in visible[key]:
                    entity_id = entity.entity_id
                    name = entity.name
                    if entity_id == primary_id:
                        name = f"▶ {name}"
                    if entity_id in problems:
                        name = f"{name} ⚠"
                    item = QTreeWidgetItem(
                        [
                            name,
                            kind_labels.get(entity.kind, entity.kind),
                            "—" if entity_id in hidden else "●",
                            "🔒" if entity_id in locked else "",
                        ]
                    )
                    item.setData(0, _ENTITY_ROLE, entity_id)
                    if entity_id in problems:
                        item.setToolTip(
                            0,
                            f"{entity.name} — 要対応: "
                            + "、".join(problems[entity_id]),
                        )
                    else:
                        # Long names elide in the narrow dock — keep the
                        # full name reachable on hover.
                        item.setToolTip(0, entity.name)
                    if entity_id in hidden:
                        item.setForeground(
                            0,
                            self.palette().color(
                                QPalette.ColorGroup.Disabled,
                                QPalette.ColorRole.WindowText,
                            ),
                        )
                    if entity_id in selected:
                        item.setSelected(True)
                    if entity_id == primary_id:
                        current_item = item
                    group_item.addChild(item)
                    self._entity_items[entity_id] = item
                expanded = (
                    key == primary_group_key or key not in self._collapsed_groups
                )
                group_item.setExpanded(expanded)

            if not ordered_keys and document.entities:
                zero_item = QTreeWidgetItem(
                    [
                        "条件に一致する項目はありません —"
                        " リセットで全表示に戻せます"
                    ]
                )
                zero_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                zero_item.setToolTip(
                    0,
                    self._active_filter_description(needle, filter_key),
                )
                self.tree.addTopLevelItem(zero_item)

            if current_item is not None:
                self.tree.setCurrentItem(current_item, 0)
                self.tree.scrollToItem(current_item)

            summary = (
                f"オブジェクト {len(document.entities)} 件 · 選択 {len(selected)} 件"
                + (f" · 非表示 {hidden_count}" if hidden_count else "")
                + (f" · ロック {locked_count}" if locked_count else "")
                + (
                    f" · 表示 {shown_count}/{len(document.entities)}"
                    if self._filter_active
                    else ""
                )
                + (
                    f" · フィルタ外の選択 {filtered_out_selected} 件"
                    if filtered_out_selected
                    else ""
                )
                + (" · 隔離中" if isolation_active else "")
            )
            self.summary.setText(summary)
            self.isolate_button.setEnabled(bool(selected))
            self.clear_isolation_button.setEnabled(isolation_active)
            self.focus_button.setEnabled(bool(selected))
        finally:
            self._syncing = False

    def _active_filter_description(self, needle: str, filter_key: str) -> str:
        parts = []
        if needle:
            parts.append(f"検索「{self.search_edit.text().strip()}」")
        if filter_key != OBJECT_FILTER_ALL:
            parts.append(f"フィルタ「{_OBJECT_FILTER_LABELS.get(filter_key, filter_key)}」")
        return "絞り込み条件: " + " + ".join(parts)
