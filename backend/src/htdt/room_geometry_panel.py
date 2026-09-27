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

from .cad_scene import room_vertices
from .cad_wall_models import WallOpening
from .cad_walls import (
    WallTopologyError,
    add_opening,
    delete_opening,
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
        self.setMaximumWidth(320)
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
        self.finish_button = QPushButton("編集終了")
        self.edit_button.clicked.connect(self.geometry.start_edit)
        self.finish_button.clicked.connect(self.geometry.cancel)
        mode_row.addWidget(self.edit_button)
        mode_row.addWidget(self.finish_button)
        root.addLayout(mode_row)

        room_form = QFormLayout()
        self.height = self._metric_field(0.1, 20.0, decimals=3, step=0.05)
        self.height.editingFinished.connect(self._height_edited)
        room_form.addRow("天井高", self.height)
        root.addLayout(room_form)

        self.selection_title = QLabel("選択: なし")
        set_typography_role(self.selection_title, TypographyRole.BODY)
        root.addWidget(self.selection_title)

        self.vertex_host = QWidget()
        vertex_layout = QFormLayout(self.vertex_host)
        vertex_layout.setContentsMargins(0, 0, 0, 0)
        self.vertex_x = self._metric_field(-1000.0, 1000.0, decimals=4)
        self.vertex_y = self._metric_field(-1000.0, 1000.0, decimals=4)
        self.vertex_x.editingFinished.connect(self._vertex_edited)
        self.vertex_y.editingFinished.connect(self._vertex_edited)
        vertex_layout.addRow("頂点 X", self.vertex_x)
        vertex_layout.addRow("頂点 Y", self.vertex_y)
        self.delete_vertex_button = QPushButton("頂点を削除")
        self.delete_vertex_button.clicked.connect(self._delete_vertex)
        vertex_layout.addRow("", self.delete_vertex_button)
        root.addWidget(self.vertex_host)

        self.edge_host = QWidget()
        edge_layout = QFormLayout(self.edge_host)
        edge_layout.setContentsMargins(0, 0, 0, 0)
        self.edge_length = self._metric_field(0.001, 1000.0, decimals=4)
        self.edge_length.editingFinished.connect(self._edge_length_edited)
        edge_layout.addRow("辺の長さ", self.edge_length)
        edge_actions = QHBoxLayout()
        self.insert_midpoint_button = QPushButton("中点に頂点追加")
        self.insert_midpoint_button.clicked.connect(self._insert_midpoint)
        self.ensure_walls_button = QPushButton("壁編集を有効化")
        self.ensure_walls_button.clicked.connect(self._ensure_topology)
        edge_actions.addWidget(self.insert_midpoint_button)
        edge_actions.addWidget(self.ensure_walls_button)
        edge_layout.addRow("", edge_actions)
        root.addWidget(self.edge_host)

        wall_label = QLabel("壁")
        set_typography_role(wall_label, TypographyRole.SECTION_TITLE)
        root.addWidget(wall_label)

        self.wall_host = QWidget()
        wall_form = QFormLayout(self.wall_host)
        wall_form.setContentsMargins(0, 0, 0, 0)
        self.wall_id = QLabel("—")
        self.wall_length = QLabel("—")
        self.wall_thickness = self._metric_field(0.001, 5.0, decimals=3)
        self.wall_thickness.editingFinished.connect(self._wall_thickness_edited)
        wall_form.addRow("壁", self.wall_id)
        wall_form.addRow("長さ", self.wall_length)
        wall_form.addRow("厚さ", self.wall_thickness)
        wall_actions = QHBoxLayout()
        self.merge_wall_button = QPushButton("次の壁と結合")
        self.delete_wall_button = QPushButton("壁を削除")
        self.merge_wall_button.clicked.connect(self._merge_wall)
        self.delete_wall_button.clicked.connect(self._delete_wall)
        wall_actions.addWidget(self.merge_wall_button)
        wall_actions.addWidget(self.delete_wall_button)
        wall_form.addRow("", wall_actions)
        root.addWidget(self.wall_host)

        opening_label = QLabel("開口")
        set_typography_role(opening_label, TypographyRole.SECTION_TITLE)
        root.addWidget(opening_label)

        self.opening_host = QWidget()
        opening_form = QFormLayout(self.opening_host)
        opening_form.setContentsMargins(0, 0, 0, 0)
        self.opening_selector = QComboBox()
        self.opening_selector.currentIndexChanged.connect(self._opening_selected)
        self.opening_kind = QComboBox()
        for value, label in (
            ("door", "ドア"),
            ("window", "窓"),
            ("passage", "通路"),
            ("other", "その他"),
        ):
            self.opening_kind.addItem(label, value)
        self.opening_offset = self._metric_field(0.0, 1000.0, decimals=3)
        self.opening_width = self._metric_field(0.001, 1000.0, decimals=3)
        self.opening_sill = self._metric_field(0.0, 20.0, decimals=3)
        self.opening_height = self._metric_field(0.001, 20.0, decimals=3)
        self.opening_open = QCheckBox("開放として扱う")
        opening_form.addRow("開口", self.opening_selector)
        opening_form.addRow("種類", self.opening_kind)
        opening_form.addRow("開始位置", self.opening_offset)
        opening_form.addRow("幅", self.opening_width)
        opening_form.addRow("床から", self.opening_sill)
        opening_form.addRow("高さ", self.opening_height)
        opening_form.addRow("", self.opening_open)

        opening_actions = QHBoxLayout()
        self.add_opening_button = QPushButton("追加")
        self.apply_opening_button = QPushButton("適用")
        self.delete_opening_button = QPushButton("削除")
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
        field = QDoubleSpinBox()
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
            self.insert_midpoint_button.setEnabled(editable)
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

        if wall is None or topology is None:
            return

        wall_index = topology.walls.index(wall)
        self.wall_id.setText(f"壁 {wall_index + 1}")
        self.wall_length.setText(f"{wall_length(room, wall):.3f} m")
        with QSignalBlocker(self.wall_thickness):
            self.wall_thickness.setValue(wall.thickness_m)
        self.wall_thickness.setEnabled(wall_ready)
        self.merge_wall_button.setEnabled(wall_ready)
        self.delete_wall_button.setEnabled(wall_ready)

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
        except (ValueError, WallTopologyError) as exc:
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
        self._run(
            lambda: self.geometry.set_room_height(self.height.value()),
            "天井高を更新しました",
        )

    def _vertex_edited(self) -> None:
        self._run(
            lambda: self.geometry.set_selected_vertex_coordinates(
                x_m=self.vertex_x.value(),
                y_m=self.vertex_y.value(),
            ),
            "頂点座標を更新しました",
        )

    def _edge_length_edited(self) -> None:
        self._run(
            lambda: self.geometry.set_selected_edge_length(self.edge_length.value()),
            "辺の長さを更新しました",
        )

    def _insert_midpoint(self) -> None:
        self._run(self.geometry.insert_selected_edge_midpoint, "頂点を追加しました")

    def _delete_vertex(self) -> None:
        self._run(self.geometry.delete_selected_vertex, "頂点を削除しました")

    def _ensure_topology(self) -> None:
        self._run(self.geometry.ensure_wall_topology, "壁編集を有効にしました")

    def _merge_wall(self) -> None:
        self._run(self.geometry.merge_selected_wall_with_next, "壁を結合しました")

    def _delete_wall(self) -> None:
        self._run(self.geometry.delete_selected_wall, "壁を削除しました")

    def _wall_thickness_edited(self) -> None:
        room = self.geometry.room
        topology = self.geometry.topology
        wall = self.geometry.selected_wall
        if room is None or topology is None or wall is None:
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
