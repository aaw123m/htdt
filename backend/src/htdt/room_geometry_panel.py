from __future__ import annotations

from math import hypot
from typing import TYPE_CHECKING
from uuid import uuid4

from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .cad_document import EditStateError
from .cad_scene import room_vertices
from .cad_wall_models import WallConstraintBinding, WallOpening
from .cad_walls import (
    WallTopologyError,
    add_constraint_binding,
    add_opening,
    delete_constraint_binding,
    delete_opening,
    update_constraint_binding,
    update_opening,
    update_wall_thickness,
    wall_length,
)
from .ui_theme import (
    SemanticState,
    SurfaceRole,
    TypographyRole,
    set_semantic_state,
    set_surface_role,
    set_typography_role,
)
from .user_facing_error import operation_error_message

if TYPE_CHECKING:
    from .room_geometry_input import RoomGeometryInputController


class _TooltipForwardingSpinBox(QDoubleSpinBox):
    """Spin box whose tooltip also shows over the embedded line edit.

    Qt does not propagate a spin box's tooltip to its internal QLineEdit —
    the cursor sits on the line edit, so the panel's tooltips would never
    appear over the text area without this mirror.
    """

    def setToolTip(self, text: str) -> None:
        super().setToolTip(text)
        self.lineEdit().setToolTip(text)


def _spin_value_changed(field: QDoubleSpinBox, stored: float) -> bool:
    """Whether the field holds a user-meaningful change from ``stored``.

    QDoubleSpinBox.value() is rounded to the field's decimals, so a stored
    value with finer precision (sketched floats, imported documents) would
    otherwise commit display-rounding noise as a silent edit on focus-out.
    """
    tolerance = 0.5 * 10 ** -field.decimals()
    return abs(float(field.value()) - stored) > tolerance


class RoomGeometryPanel(QFrame):
    """Context-only geometry inspector backed by existing N30a/N30b authority."""

    def __init__(
        self,
        geometry: RoomGeometryInputController,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.geometry = geometry
        self.controller = geometry.workspace.controller
        self.setObjectName("roomGeometryPanel")
        self.setMinimumWidth(248)
        self.setMaximumWidth(560)
        set_surface_role(self, SurfaceRole.RAISED)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        title = QLabel("部屋形状")
        set_typography_role(title, TypographyRole.SECTION_TITLE)
        root.addWidget(title)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        set_typography_role(self.summary, TypographyRole.SECONDARY)
        root.addWidget(self.summary)

        mode_row = QHBoxLayout()
        self.edit_button = QPushButton("形状編集")
        self.edit_button.setToolTip(
            "編集モードに入り、3D上で頂点・辺・壁を選んで調整できるようにします"
        )
        self.finish_button = QPushButton("編集終了")
        self.finish_button.setToolTip("形状編集モードを終了します")
        self.edit_button.clicked.connect(self.geometry.start_edit)
        self.finish_button.clicked.connect(self.geometry.cancel)
        mode_row.addWidget(self.edit_button)
        mode_row.addWidget(self.finish_button)
        root.addLayout(mode_row)

        room_form = QFormLayout()
        self.height = self._metric_field(0.1, 20.0, decimals=3, step=0.05)
        self.height.setToolTip("床から天井までの高さ（m）· すべての壁に共通です")
        self.height.editingFinished.connect(self._height_edited)
        room_form.addRow("天井高", self.height)
        height_label = room_form.labelForField(self.height)
        if height_label is not None:
            height_label.setToolTip(self.height.toolTip())
        root.addLayout(room_form)

        self.selection_title = QLabel("選択: なし")
        set_typography_role(self.selection_title, TypographyRole.BODY)
        root.addWidget(self.selection_title)

        self.vertex_host = QWidget()
        vertex_layout = QFormLayout(self.vertex_host)
        vertex_layout.setContentsMargins(0, 0, 0, 0)
        self.vertex_x = self._metric_field(-1000.0, 1000.0, decimals=4)
        self.vertex_y = self._metric_field(-1000.0, 1000.0, decimals=4)
        vertex_hint = "選択中の頂点の部屋座標（m）· +X=部屋右、+Y=部屋奥"
        self.vertex_x.setToolTip(vertex_hint)
        self.vertex_y.setToolTip(vertex_hint)
        self.vertex_x.editingFinished.connect(self._vertex_edited)
        self.vertex_y.editingFinished.connect(self._vertex_edited)
        vertex_layout.addRow("頂点 X", self.vertex_x)
        vertex_layout.addRow("頂点 Y", self.vertex_y)
        for field in (self.vertex_x, self.vertex_y):
            label = vertex_layout.labelForField(field)
            if label is not None:
                label.setToolTip(vertex_hint)
        self.delete_vertex_button = QPushButton("頂点を削除")
        self.delete_vertex_button.setToolTip(
            "選択中の頂点を削除します（両隣の頂点が直線で結ばれます）"
        )
        self.delete_vertex_button.clicked.connect(self._delete_vertex)
        vertex_layout.addRow("", self.delete_vertex_button)
        root.addWidget(self.vertex_host)

        self.edge_host = QWidget()
        edge_layout = QFormLayout(self.edge_host)
        edge_layout.setContentsMargins(0, 0, 0, 0)
        self.edge_length = self._metric_field(0.001, 1000.0, decimals=4)
        edge_hint = "選択中の辺の長さ（m）· 変更すると終点側の頂点が移動します"
        self.edge_length.setToolTip(edge_hint)
        self.edge_length.editingFinished.connect(self._edge_length_edited)
        edge_layout.addRow("辺の長さ", self.edge_length)
        edge_label = edge_layout.labelForField(self.edge_length)
        if edge_label is not None:
            edge_label.setToolTip(edge_hint)
        self.split_offset = self._metric_field(0.001, 1000.0, decimals=3)
        split_offset_hint = "辺の始点からの分割位置（m）· 中点以外に頂点を挿入したい場合に変更"
        self.split_offset.setToolTip(split_offset_hint)
        edge_layout.addRow("分割位置", self.split_offset)
        split_offset_label = edge_layout.labelForField(self.split_offset)
        if split_offset_label is not None:
            split_offset_label.setToolTip(split_offset_hint)
        edge_actions = QHBoxLayout()
        self.insert_midpoint_button = QPushButton("中点に頂点追加")
        self.insert_midpoint_button.setToolTip(
            "選択中の辺の中点に新しい頂点を挿入します（L字・凹凸を作れます）"
        )
        self.insert_midpoint_button.clicked.connect(self._insert_midpoint)
        self.insert_at_offset_button = QPushButton("指定位置に頂点追加")
        self.insert_at_offset_button.setToolTip(
            "「分割位置」で指定した地点に新しい頂点を挿入します"
        )
        self.insert_at_offset_button.clicked.connect(self._insert_at_offset)
        self.ensure_walls_button = QPushButton("壁編集を有効化")
        self.ensure_walls_button.setToolTip(
            "辺から壁オブジェクトを生成し、厚さ・開口を編集できるようにします"
        )
        self.ensure_walls_button.clicked.connect(self._ensure_topology)
        edge_actions.addWidget(self.insert_midpoint_button)
        edge_actions.addWidget(self.ensure_walls_button)
        edge_layout.addRow("", edge_actions)
        offset_actions = QHBoxLayout()
        offset_actions.addWidget(self.insert_at_offset_button)
        edge_layout.addRow("", offset_actions)
        root.addWidget(self.edge_host)

        wall_label = QLabel("壁")
        set_typography_role(wall_label, TypographyRole.SECTION_TITLE)
        wall_label.setToolTip("部屋の外周を構成する壁 · 「形状編集」モードで辺/壁を選ぶと編集できます")
        root.addWidget(wall_label)

        self.wall_hint = QLabel(
            "「形状編集」モードで3D上の辺・壁を選ぶと、ここに厚さ・結合/削除・クリアランス参照の設定が表示されます"
        )
        self.wall_hint.setWordWrap(True)
        set_typography_role(self.wall_hint, TypographyRole.SECONDARY)
        root.addWidget(self.wall_hint)

        self.wall_host = QWidget()
        wall_form = QFormLayout(self.wall_host)
        wall_form.setContentsMargins(0, 0, 0, 0)
        self.wall_id = QLabel("—")
        self.wall_id.setToolTip("選択中の壁の番号（部屋の外周を時計回りに採番）")
        self.wall_length = QLabel("—")
        self.wall_length.setToolTip("選択中の壁の長さ（m）· 読み取り専用")
        self.wall_thickness = self._metric_field(0.001, 5.0, decimals=3)
        wall_thickness_hint = "壁の厚さ（m）· 遮音・構造の表現に使われます"
        self.wall_thickness.setToolTip(wall_thickness_hint)
        self.wall_thickness.editingFinished.connect(self._wall_thickness_edited)
        wall_form.addRow("壁", self.wall_id)
        wall_form.addRow("長さ", self.wall_length)
        wall_form.addRow("厚さ", self.wall_thickness)
        thickness_label = wall_form.labelForField(self.wall_thickness)
        if thickness_label is not None:
            thickness_label.setToolTip(wall_thickness_hint)
        wall_actions = QHBoxLayout()
        self.merge_wall_button = QPushButton("次の壁と結合")
        self._merge_wall_tooltips = (
            "この壁と次（時計回り）の壁を1本にまとめます",
            "この壁と先頭の壁を1本にまとめます（境目をまたぐ結合）",
        )
        self.merge_wall_button.setToolTip(self._merge_wall_tooltips[0])
        self.delete_wall_button = QPushButton("壁を削除")
        self.delete_wall_button.setToolTip("選択中の壁を削除します")
        self.merge_wall_button.clicked.connect(self._merge_wall)
        self.delete_wall_button.clicked.connect(self._delete_wall)
        wall_actions.addWidget(self.merge_wall_button)
        wall_actions.addWidget(self.delete_wall_button)
        wall_form.addRow("", wall_actions)
        clearance_hint = (
            "選択中の壁に対するクリアランス参照（自動配置との境界条件）"
        )
        self.wall_clearance_count = QLabel("—")
        self.wall_clearance_count.setToolTip("この壁に紐づくクリアランス参照の件数")
        self.binding_selector = QComboBox()
        self.binding_selector.setToolTip(
            "編集するクリアランス参照を選択 ·「新規 / 未選択」では追加モードになります"
        )
        self.binding_selector.currentIndexChanged.connect(self._binding_selected)
        self.clearance_value = self._metric_field(0.0, 10.0, decimals=3, step=0.05)
        self.clearance_value.setValue(0.30)
        self.clearance_value.setToolTip("クリアランスの距離（m）")
        self.add_clearance_button = QPushButton("追加")
        self.add_clearance_button.setToolTip(
            "選択中の壁へクリアランス参照を追加します"
        )
        self.apply_clearance_button = QPushButton("適用")
        self.apply_clearance_button.setToolTip(
            "選択中のクリアランス参照に入力値を反映します"
        )
        self.delete_clearance_button = QPushButton("削除")
        self.delete_clearance_button.setToolTip(
            "選択中のクリアランス参照を削除します"
        )
        self.add_clearance_button.clicked.connect(self._add_clearance_binding)
        self.apply_clearance_button.clicked.connect(self._apply_clearance_binding)
        self.delete_clearance_button.clicked.connect(self._delete_clearance_binding)
        clearance_actions = QHBoxLayout()
        clearance_actions.addWidget(self.add_clearance_button)
        clearance_actions.addWidget(self.apply_clearance_button)
        clearance_actions.addWidget(self.delete_clearance_button)
        wall_form.addRow("クリアランス参照", self.wall_clearance_count)
        wall_form.addRow("参照の選択", self.binding_selector)
        wall_form.addRow("クリアランス", self.clearance_value)
        wall_form.addRow("", clearance_actions)
        clearance_label = wall_form.labelForField(self.wall_clearance_count)
        if clearance_label is not None:
            clearance_label.setToolTip(clearance_hint)
        for field, hint in (
            (self.binding_selector, self.binding_selector.toolTip()),
            (self.clearance_value, self.clearance_value.toolTip()),
        ):
            label = wall_form.labelForField(field)
            if label is not None:
                label.setToolTip(hint)
        root.addWidget(self.wall_host)

        opening_label = QLabel("開口")
        set_typography_role(opening_label, TypographyRole.SECTION_TITLE)
        opening_label.setToolTip("壁の開いた部分（ドア・窓・通路）")
        root.addWidget(opening_label)

        self.opening_hint = QLabel(
            "壁を選択すると開口の追加・編集ができます"
        )
        self.opening_hint.setWordWrap(True)
        set_typography_role(self.opening_hint, TypographyRole.SECONDARY)
        root.addWidget(self.opening_hint)

        self.opening_host = QWidget()
        opening_form = QFormLayout(self.opening_host)
        opening_form.setContentsMargins(0, 0, 0, 0)
        self.opening_selector = QComboBox()
        self.opening_selector.setToolTip(
            "編集する開口を選択 ·「新規 / 未選択」の状態で「追加」を押すと新しい開口を作成します"
        )
        self.opening_selector.currentIndexChanged.connect(self._opening_selected)
        self.opening_kind = QComboBox()
        for value, label in (
            ("door", "ドア"),
            ("window", "窓"),
            ("passage", "通路"),
            ("other", "その他"),
        ):
            self.opening_kind.addItem(label, value)
        self.opening_kind.setToolTip(
            "開口の種類 · 通路は常に開放扱い、ドア・窓は閉じた開口として音響計算されます"
        )
        self.opening_offset = self._metric_field(0.0, 1000.0, decimals=3)
        self.opening_width = self._metric_field(0.001, 1000.0, decimals=3)
        self.opening_sill = self._metric_field(0.0, 20.0, decimals=3)
        self.opening_height = self._metric_field(0.001, 20.0, decimals=3)
        self.opening_open = QCheckBox("開放として扱う")
        opening_hints = {
            "開口": "編集する開口を選択 ·「新規 / 未選択」では追加モードになります",
            "種類": self.opening_kind.toolTip(),
            "開始位置": "壁の始点から開口の開始端までの距離（m）",
            "幅": "開口の幅（m）",
            "床から": "床から開口下端までの高さ（m）· 窓の場合は腰壁の高さ",
            "高さ": "開口の高さ（m）",
        }
        self.opening_offset.setToolTip(opening_hints["開始位置"])
        self.opening_width.setToolTip(opening_hints["幅"])
        self.opening_sill.setToolTip(opening_hints["床から"])
        self.opening_height.setToolTip(opening_hints["高さ"])
        self.opening_open.setToolTip(
            "扉・仕切りのない常時開いた開口として扱います（通路と同じ扱い）"
        )
        opening_form.addRow("開口", self.opening_selector)
        opening_form.addRow("種類", self.opening_kind)
        opening_form.addRow("開始位置", self.opening_offset)
        opening_form.addRow("幅", self.opening_width)
        opening_form.addRow("床から", self.opening_sill)
        opening_form.addRow("高さ", self.opening_height)
        opening_form.addRow("", self.opening_open)
        for field, hint in (
            (self.opening_selector, opening_hints["開口"]),
            (self.opening_kind, opening_hints["種類"]),
            (self.opening_offset, opening_hints["開始位置"]),
            (self.opening_width, opening_hints["幅"]),
            (self.opening_sill, opening_hints["床から"]),
            (self.opening_height, opening_hints["高さ"]),
        ):
            label = opening_form.labelForField(field)
            if label is not None:
                label.setToolTip(hint)

        opening_actions = QHBoxLayout()
        self.add_opening_button = QPushButton("追加")
        self.add_opening_button.setToolTip(
            "入力した種類・寸法でこの壁に新しい開口を作成します"
        )
        self.apply_opening_button = QPushButton("適用")
        self.apply_opening_button.setToolTip("選択中の開口に入力値を反映します")
        self.delete_opening_button = QPushButton("削除")
        self.delete_opening_button.setToolTip("選択中の開口を削除します")
        self.add_opening_button.clicked.connect(self._add_opening)
        self.apply_opening_button.clicked.connect(self._apply_opening)
        self.delete_opening_button.clicked.connect(self._delete_opening)
        opening_actions.addWidget(self.add_opening_button)
        opening_actions.addWidget(self.apply_opening_button)
        opening_actions.addWidget(self.delete_opening_button)
        opening_form.addRow("", opening_actions)
        root.addWidget(self.opening_host)

        self.notice = QLabel()
        self.notice.setWordWrap(True)
        set_typography_role(self.notice, TypographyRole.SECONDARY)
        root.addWidget(self.notice)
        root.addStretch(1)

        geometry.selectionChanged.connect(self.refresh)
        self.refresh()

    @staticmethod
    def _metric_field(
        minimum: float,
        maximum: float,
        *,
        decimals: int,
        step: float = 0.01,
    ) -> QDoubleSpinBox:
        field = _TooltipForwardingSpinBox()
        field.setRange(minimum, maximum)
        field.setDecimals(decimals)
        field.setSingleStep(step)
        field.setSuffix(" m")
        field.setKeyboardTracking(False)
        return field

    def refresh(self) -> None:
        room = self.geometry.room
        editable = (
            room is not None
            and self.controller.recovery_candidate is None
            and not self.controller.working.has_preview
        )
        self.edit_button.setEnabled(editable and self.geometry.mode == "idle")
        self.finish_button.setEnabled(self.geometry.mode != "idle")
        self.height.setEnabled(editable)

        if room is None:
            self.summary.setText("部屋がありません。まず「部屋を描く」で形状を作成してください。")
            self.vertex_host.hide()
            self.edge_host.hide()
            self.wall_host.hide()
            self.opening_host.hide()
            self.wall_hint.hide()
            self.opening_hint.hide()
            return

        min_x, min_y, max_x, max_y = room.bounds_m
        self.summary.setText(
            f"{len(room_vertices(room))}頂点 · "
            f"X {min_x:.2f}–{max_x:.2f} m · Y {min_y:.2f}–{max_y:.2f} m"
        )
        with QSignalBlocker(self.height):
            self.height.setValue(room.height_m)

        vertex = self.geometry.selected_vertex
        edge_index = self.geometry.selected_edge_index
        self.vertex_host.setVisible(vertex is not None)
        self.edge_host.setVisible(edge_index is not None)

        if vertex is not None:
            self.selection_title.setText("選択: 頂点")
            with QSignalBlocker(self.vertex_x), QSignalBlocker(self.vertex_y):
                self.vertex_x.setValue(vertex.x_m)
                self.vertex_y.setValue(vertex.y_m)
            self.vertex_x.setEnabled(editable)
            self.vertex_y.setEnabled(editable)
            self.delete_vertex_button.setEnabled(editable)
        elif edge_index is not None:
            vertices = tuple(room_vertices(room))
            start = vertices[edge_index % len(vertices)]
            end = vertices[(edge_index + 1) % len(vertices)]
            self.selection_title.setText("選択: 辺 / 壁")
            with QSignalBlocker(self.edge_length):
                self.edge_length.setValue(hypot(end.x_m - start.x_m, end.y_m - start.y_m))
            self.edge_length.setEnabled(editable)
            edge_length_value = hypot(end.x_m - start.x_m, end.y_m - start.y_m)
            with QSignalBlocker(self.split_offset):
                self.split_offset.setMaximum(max(edge_length_value - 0.001, 0.001))
                self.split_offset.setValue(
                    min(edge_length_value * 0.5, self.split_offset.maximum())
                )
            self.split_offset.setEnabled(editable)
            self.insert_midpoint_button.setEnabled(editable)
            self.insert_at_offset_button.setEnabled(editable)
        else:
            self.selection_title.setText("選択: なし")
            self.vertex_host.hide()
            self.edge_host.hide()

        topology = self.geometry.topology
        self.ensure_walls_button.setVisible(topology is None)
        wall = self.geometry.selected_wall
        wall_ready = editable and wall is not None and topology is not None
        self.wall_host.setVisible(wall is not None)
        self.opening_host.setVisible(wall is not None)
        self.wall_hint.setVisible(wall is None)
        self.opening_hint.setVisible(wall is None)

        if wall is None or topology is None:
            return

        wall_index = topology.walls.index(wall)
        self.wall_id.setText(f"壁 {wall_index + 1}")
        self.wall_length.setText(f"{wall_length(room, wall):.3f} m")
        with QSignalBlocker(self.wall_thickness):
            self.wall_thickness.setValue(wall.thickness_m)
        self.wall_thickness.setEnabled(wall_ready)
        is_last_wall = wall_index == len(topology.walls) - 1
        self.merge_wall_button.setText(
            "先頭の壁と結合" if is_last_wall else "次の壁と結合"
        )
        self.merge_wall_button.setToolTip(
            self._merge_wall_tooltips[1 if is_last_wall else 0]
        )
        self.merge_wall_button.setEnabled(wall_ready)
        self.delete_wall_button.setEnabled(wall_ready)

        bindings = tuple(
            item
            for item in topology.constraint_bindings
            if wall.wall_id in item.wall_ids
        )
        self.wall_clearance_count.setText(f"{len(bindings)} 件")
        self.wall_clearance_count.setToolTip(
            "この壁に紐づくクリアランス参照"
            + (
                "（" + ", ".join(f"{item.clearance_m:.2f} m" for item in bindings) + "）"
                if bindings
                else "はありません"
            )
        )
        previous_binding = self.binding_selector.currentData()
        with QSignalBlocker(self.binding_selector):
            self.binding_selector.clear()
            self.binding_selector.addItem("新規 / 未選択", None)
            for item in bindings:
                label = f"{item.clearance_m:.2f} m"
                if len(item.wall_ids) > 1:
                    label += f"（{len(item.wall_ids)}壁）"
                self.binding_selector.addItem(label, item.binding_id)
            if previous_binding is not None:
                index = self.binding_selector.findData(previous_binding)
                if index >= 0:
                    self.binding_selector.setCurrentIndex(index)
        self.binding_selector.setEnabled(wall_ready)
        self.clearance_value.setEnabled(wall_ready)
        self.add_clearance_button.setEnabled(wall_ready)
        self._binding_selected()

        previous = self.opening_selector.currentData()
        openings = tuple(item for item in topology.openings if item.wall_id == wall.wall_id)
        with QSignalBlocker(self.opening_selector):
            self.opening_selector.clear()
            self.opening_selector.addItem("新規 / 未選択", None)
            for item in openings:
                self.opening_selector.addItem(
                    f"{self._kind_label(item.kind)} · {item.offset_m:.2f} m",
                    item.opening_id,
                )
            if previous is not None:
                index = self.opening_selector.findData(previous)
                if index >= 0:
                    self.opening_selector.setCurrentIndex(index)
        self.add_opening_button.setEnabled(wall_ready)
        self._opening_selected()

    @staticmethod
    def _kind_label(kind: str) -> str:
        return {
            "door": "ドア",
            "window": "窓",
            "passage": "通路",
            "other": "その他",
        }.get(kind, kind)

    def _selected_opening(self) -> WallOpening | None:
        topology = self.geometry.topology
        opening_id = self.opening_selector.currentData()
        if topology is None or not isinstance(opening_id, str):
            return None
        return next(
            (item for item in topology.openings if item.opening_id == opening_id),
            None,
        )

    def _opening_selected(self) -> None:
        opening = self._selected_opening()
        enabled = opening is not None
        self.opening_kind.setEnabled(self.add_opening_button.isEnabled())
        for field in (
            self.opening_offset,
            self.opening_width,
            self.opening_sill,
            self.opening_height,
            self.opening_open,
        ):
            field.setEnabled(enabled)
        self.apply_opening_button.setEnabled(enabled)
        self.delete_opening_button.setEnabled(enabled)
        if opening is None:
            return
        blockers = [
            QSignalBlocker(self.opening_kind),
            QSignalBlocker(self.opening_offset),
            QSignalBlocker(self.opening_width),
            QSignalBlocker(self.opening_sill),
            QSignalBlocker(self.opening_height),
            QSignalBlocker(self.opening_open),
        ]
        try:
            kind_index = self.opening_kind.findData(opening.kind)
            if kind_index >= 0:
                self.opening_kind.setCurrentIndex(kind_index)
            self.opening_offset.setValue(opening.offset_m)
            self.opening_width.setValue(opening.width_m)
            self.opening_sill.setValue(opening.sill_m)
            self.opening_height.setValue(opening.height_m)
            self.opening_open.setChecked(opening.is_open)
        finally:
            del blockers

    def _run(self, operation, success: str) -> None:
        try:
            changed = bool(operation())
        except (EditStateError, ValueError, WallTopologyError) as exc:
            self.geometry.workspace.mark_pending_editor_rejected()
            self.notice.setText(operation_error_message(exc))
            set_semantic_state(self.notice, SemanticState.ERROR)
            self.refresh()
            return
        if changed:
            self.notice.setText(success)
            set_semantic_state(self.notice, SemanticState.SUCCESS)
        else:
            self.notice.setText("変更はありません")
            set_semantic_state(self.notice, None)
        self.geometry.workspace.refresh()
        self.refresh()

    def _height_edited(self) -> None:
        room = self.geometry.room
        if room is None:
            return
        if not _spin_value_changed(self.height, room.height_m):
            self.refresh()
            return
        self._run(
            lambda: self.geometry.set_room_height(self.height.value()),
            "天井高を更新しました",
        )

    def _vertex_edited(self) -> None:
        selected = self.geometry.selected_vertex
        if selected is None:
            return
        if not _spin_value_changed(
            self.vertex_x, selected.x_m
        ) and not _spin_value_changed(self.vertex_y, selected.y_m):
            self.refresh()
            return
        self._run(
            lambda: self.geometry.set_selected_vertex_coordinates(
                x_m=self.vertex_x.value(),
                y_m=self.vertex_y.value(),
            ),
            "頂点座標を更新しました",
        )

    def _edge_length_edited(self) -> None:
        room = self.geometry.room
        if room is None or self.geometry.selected_edge_index is None:
            return
        vertices = list(room_vertices(room))
        index = self.geometry.selected_edge_index % len(vertices)
        start = vertices[index]
        end = vertices[(index + 1) % len(vertices)]
        current_length = hypot(end.x_m - start.x_m, end.y_m - start.y_m)
        if not _spin_value_changed(self.edge_length, current_length):
            self.refresh()
            return
        self._run(
            lambda: self.geometry.set_selected_edge_length(self.edge_length.value()),
            "辺の長さを更新しました",
        )

    def _insert_midpoint(self) -> None:
        self._run(self.geometry.insert_selected_edge_midpoint, "頂点を追加しました")

    def _insert_at_offset(self) -> None:
        self._run(
            lambda: self.geometry.insert_selected_edge_vertex(
                self.split_offset.value()
            ),
            "頂点を追加しました",
        )

    def _delete_vertex(self) -> None:
        self._run(self.geometry.delete_selected_vertex, "頂点を削除しました")

    def _ensure_topology(self) -> None:
        self._run(self.geometry.ensure_wall_topology, "壁編集を有効にしました")

    def _merge_wall(self) -> None:
        self._run(self.geometry.merge_selected_wall_with_next, "壁を結合しました")

    def _delete_wall(self) -> None:
        self._run(self.geometry.delete_selected_wall, "壁を削除しました")

    def _selected_binding(self) -> WallConstraintBinding | None:
        topology = self.geometry.topology
        binding_id = self.binding_selector.currentData()
        if topology is None or not isinstance(binding_id, str):
            return None
        return next(
            (
                item
                for item in topology.constraint_bindings
                if item.binding_id == binding_id
            ),
            None,
        )

    def _binding_selected(self) -> None:
        binding = self._selected_binding()
        enabled = binding is not None
        self.apply_clearance_button.setEnabled(enabled)
        self.delete_clearance_button.setEnabled(enabled)
        if binding is None:
            return
        with QSignalBlocker(self.clearance_value):
            self.clearance_value.setValue(binding.clearance_m)

    def _apply_clearance_binding(self) -> None:
        room = self.geometry.room
        topology = self.geometry.topology
        current = self._selected_binding()
        if room is None or topology is None or current is None:
            return
        replacement = WallConstraintBinding(
            binding_id=current.binding_id,
            wall_ids=current.wall_ids,
            clearance_m=float(self.clearance_value.value()),
        )

        def operation() -> bool:
            candidate = update_constraint_binding(room, topology, replacement)
            return self.controller.replace_room_topology(room, candidate)

        self._run(operation, "クリアランス参照を更新しました")

    def _delete_clearance_binding(self) -> None:
        room = self.geometry.room
        topology = self.geometry.topology
        current = self._selected_binding()
        if room is None or topology is None or current is None:
            return

        def operation() -> bool:
            candidate = delete_constraint_binding(room, topology, current.binding_id)
            return self.controller.replace_room_topology(room, candidate)

        self._run(operation, "クリアランス参照を削除しました")

    def _add_clearance_binding(self) -> None:
        room = self.geometry.room
        topology = self.geometry.topology
        wall = self.geometry.selected_wall
        if room is None or topology is None or wall is None:
            return
        binding = WallConstraintBinding(
            binding_id=f"clearance-{uuid4().hex[:10]}",
            wall_ids=(wall.wall_id,),
            clearance_m=float(self.clearance_value.value()),
        )

        def operation() -> bool:
            candidate = add_constraint_binding(room, topology, binding)
            return self.controller.replace_room_topology(room, candidate)

        self._run(operation, "クリアランス参照を追加しました")

    def _wall_thickness_edited(self) -> None:
        room = self.geometry.room
        topology = self.geometry.topology
        wall = self.geometry.selected_wall
        if room is None or topology is None or wall is None:
            return
        if not _spin_value_changed(self.wall_thickness, wall.thickness_m):
            self.refresh()
            return

        def operation() -> bool:
            changed = update_wall_thickness(
                room,
                topology,
                wall.wall_id,
                thickness_m=self.wall_thickness.value(),
            )
            return self.controller.replace_room_topology(room, changed)

        self._run(operation, "壁厚を更新しました")

    def _add_opening(self) -> None:
        room = self.geometry.room
        topology = self.geometry.topology
        wall = self.geometry.selected_wall
        if room is None or wall is None:
            return
        if topology is None:
            self.geometry.ensure_wall_topology()
            topology = self.geometry.topology
            wall = self.geometry.selected_wall
        if topology is None or wall is None:
            return
        length = wall_length(room, wall)
        if length <= 0.12:
            self.notice.setText("壁が短すぎるため開口を追加できません")
            set_semantic_state(self.notice, SemanticState.ERROR)
            return
        width = min(0.9, max(0.10, length - 0.10))
        kind = str(self.opening_kind.currentData() or "door")
        opening = WallOpening(
            opening_id=f"opening-{uuid4().hex[:10]}",
            wall_id=wall.wall_id,
            offset_m=max((length - width) * 0.5, 0.0),
            width_m=width,
            sill_m=0.8 if kind == "window" else 0.0,
            height_m=min(1.0 if kind == "window" else 2.0, room.height_m),
            kind=kind,
            is_open=kind == "passage",
        )

        def operation() -> bool:
            candidate = add_opening(room, topology, opening)
            return self.controller.replace_room_topology(room, candidate)

        self._run(operation, "開口を追加しました")
        index = self.opening_selector.findData(opening.opening_id)
        if index >= 0:
            self.opening_selector.setCurrentIndex(index)

    def _apply_opening(self) -> None:
        room = self.geometry.room
        topology = self.geometry.topology
        wall = self.geometry.selected_wall
        current = self._selected_opening()
        if room is None or topology is None or wall is None or current is None:
            return
        replacement = WallOpening(
            opening_id=current.opening_id,
            wall_id=wall.wall_id,
            offset_m=self.opening_offset.value(),
            width_m=self.opening_width.value(),
            sill_m=self.opening_sill.value(),
            height_m=self.opening_height.value(),
            kind=str(self.opening_kind.currentData() or current.kind),
            is_open=self.opening_open.isChecked(),
        )

        def operation() -> bool:
            candidate = update_opening(room, topology, replacement)
            return self.controller.replace_room_topology(room, candidate)

        self._run(operation, "開口を更新しました")

    def _delete_opening(self) -> None:
        room = self.geometry.room
        topology = self.geometry.topology
        current = self._selected_opening()
        if room is None or topology is None or current is None:
            return

        def operation() -> bool:
            candidate = delete_opening(room, topology, current.opening_id)
            return self.controller.replace_room_topology(room, candidate)

        self._run(operation, "開口を削除しました")


__all__ = ["RoomGeometryPanel"]
