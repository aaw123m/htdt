from __future__ import annotations

import argparse
from math import hypot
from pathlib import Path
import sys
from uuid import uuid4

import numpy as np
import pyvista as pv
from PySide6.QtCore import QEvent, QSignalBlocker, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QToolBar,
    QWidget,
)

from .cad_repository import SceneRepository
from .cad_scene import F1_DOCUMENT_ID, RoomPrism, RoomVertex, room_vertices
from .cad_wall_models import WallConstraintBinding, WallOpening, WallSegment, WallTopology
from .cad_walls import (
    WallTopologyError,
    add_constraint_binding,
    add_opening,
    delete_wall,
    make_wall_topology,
    merge_walls,
    move_wall,
    split_wall,
    validate_wall_topology,
    wall_length,
)
from .native_editor import default_data_dir
from .room_editor import RoomEditorWindow


def _wall_prism(room: RoomPrism, wall: WallSegment) -> pv.PolyData:
    vertices = room_vertices(room)
    by_id = {vertex.vertex_id: vertex for vertex in vertices}
    start = by_id[wall.from_vertex_id]
    end = by_id[wall.to_vertex_id]
    dx = end.x_m - start.x_m
    dy = end.y_m - start.y_m
    length = hypot(dx, dy)
    if length <= 1e-12:
        return pv.PolyData()

    signed_area = sum(
        vertex.x_m * vertices[(index + 1) % len(vertices)].y_m
        - vertices[(index + 1) % len(vertices)].x_m * vertex.y_m
        for index, vertex in enumerate(vertices)
    )
    if signed_area >= 0.0:
        nx, ny = dy / length, -dx / length
    else:
        nx, ny = -dy / length, dx / length

    sx2 = start.x_m + nx * wall.thickness_m
    sy2 = start.y_m + ny * wall.thickness_m
    ex2 = end.x_m + nx * wall.thickness_m
    ey2 = end.y_m + ny * wall.thickness_m
    h = room.height_m
    points = np.asarray(
        [
            (start.x_m, -start.y_m, 0.0),
            (end.x_m, -end.y_m, 0.0),
            (ex2, -ey2, 0.0),
            (sx2, -sy2, 0.0),
            (start.x_m, -start.y_m, h),
            (end.x_m, -end.y_m, h),
            (ex2, -ey2, h),
            (sx2, -sy2, h),
        ],
        dtype=float,
    )
    faces = np.asarray(
        [
            4, 0, 1, 2, 3,
            4, 4, 7, 6, 5,
            4, 0, 4, 5, 1,
            4, 1, 5, 6, 2,
            4, 2, 6, 7, 3,
            4, 3, 7, 4, 0,
        ],
        dtype=np.int64,
    )
    return pv.PolyData(points, faces)


class WallEditorWindow(RoomEditorWindow):
    """N30b wall/opening editor layered on the accepted N30a room editor."""

    def __init__(self, repository: SceneRepository, document_id: str = F1_DOCUMENT_ID) -> None:
        # RoomEditorWindow calls virtual methods during construction.
        self.wall_edit_active = False
        self.selected_wall_id: str | None = None
        self.wall_drag_wall_id: str | None = None
        self.wall_drag_start_xy: tuple[float, float] | None = None
        self.wall_drag_before_room: RoomPrism | None = None
        self.wall_drag_before_topology: WallTopology | None = None
        self.wall_drag_preview_room: RoomPrism | None = None
        self.wall_drag_preview_topology: WallTopology | None = None
        self._wall_actor_names: set[str] = set()
        super().__init__(repository, document_id)

        self.setWindowTitle('Home Theater Digital Twin — 壁・開口エディター')
        self._localize_inherited_ui()

        toolbar = QToolBar('壁', self)
        self.addToolBar(toolbar)
        self.start_wall_action = QAction('壁を編集', self)
        self.start_wall_action.triggered.connect(self.start_wall_edit)
        self.finish_wall_action = QAction('壁編集を終了', self)
        self.finish_wall_action.triggered.connect(self.finish_wall_edit)
        self.split_wall_action = QAction('壁を中央で分割', self)
        self.split_wall_action.triggered.connect(self.split_selected_wall)
        self.merge_wall_action = QAction('次の壁と結合', self)
        self.merge_wall_action.triggered.connect(self.merge_selected_wall_with_next)
        self.delete_wall_action = QAction('壁を削除', self)
        self.delete_wall_action.triggered.connect(self.delete_selected_wall)
        self.add_opening_action = QAction('ドア開口を追加', self)
        self.add_opening_action.triggered.connect(self.add_door_opening)
        self.add_clearance_action = QAction('クリアランス参照を追加', self)
        self.add_clearance_action.triggered.connect(self.add_clearance_binding)
        toolbar.addActions(
            (
                self.start_wall_action,
                self.finish_wall_action,
                self.split_wall_action,
                self.merge_wall_action,
                self.delete_wall_action,
                self.add_opening_action,
                self.add_clearance_action,
            )
        )

        inspector = QWidget()
        form = QFormLayout(inspector)
        self.wall_mode_label = QLabel('オブジェクト編集')
        self.wall_id_label = QLabel('—')
        self.wall_length_label = QLabel('—')
        self.wall_openings_label = QLabel('—')
        self.wall_constraints_label = QLabel('—')
        form.addRow('操作モード', self.wall_mode_label)
        form.addRow('選択中の壁', self.wall_id_label)
        form.addRow('壁の長さ', self.wall_length_label)
        form.addRow('開口数', self.wall_openings_label)
        form.addRow('クリアランス参照', self.wall_constraints_label)

        self.wall_thickness = QDoubleSpinBox()
        self.wall_thickness.setRange(0.001, 2.0)
        self.wall_thickness.setDecimals(3)
        self.wall_thickness.setSingleStep(0.01)
        self.wall_thickness.setSuffix(' m')
        self.wall_thickness.setKeyboardTracking(False)
        self.wall_thickness.editingFinished.connect(self._numeric_wall_thickness_edited)
        form.addRow('壁厚', self.wall_thickness)

        self.clearance_value = QDoubleSpinBox()
        self.clearance_value.setRange(0.0, 10.0)
        self.clearance_value.setDecimals(3)
        self.clearance_value.setSingleStep(0.05)
        self.clearance_value.setValue(0.30)
        self.clearance_value.setSuffix(' m')
        form.addRow('追加するクリアランス', self.clearance_value)

        dock = QDockWidget('壁・開口', self)
        dock.setWidget(inspector)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        self._refresh_wall_inspector()
        self._update_actions()

    def _localize_inherited_ui(self) -> None:
        action_labels = {
            'Save': '保存',
            'Undo': '元に戻す',
            'Redo': 'やり直す',
            'Delete': '削除',
            'Move': '移動',
            'Rotate': '回転',
            'Object Snap': 'オブジェクトスナップ',
            'Grid Snap': 'グリッドスナップ',
            'Angle Snap': '角度スナップ',
            'Hidden': '非表示',
            'Locked': 'ロック',
            'Show All': 'すべて表示',
            'Recover Draft': '下書きを復旧',
            'Discard Recovery': '復旧データを破棄',
            'Top': '上面',
            'Front': '正面',
            'Right': '右側面',
            'Perspective': '透視',
            'Fit': '全体表示',
        }
        for toolbar in self.findChildren(QToolBar):
            if toolbar.windowTitle() == 'Editor':
                toolbar.setWindowTitle('編集')
            elif toolbar.windowTitle() == 'Room':
                toolbar.setWindowTitle('部屋')
            for action in toolbar.actions():
                replacement = action_labels.get(action.text())
                if replacement is not None:
                    action.setText(replacement)

        self.draw_room_action.setText('部屋を作図')
        self.edit_room_action.setText('部屋を編集')
        self.close_room_action.setText('部屋を閉じる')
        self.done_room_action.setText('部屋編集を終了')
        self.insert_vertex_action.setText('頂点を挿入')
        self.delete_vertex_action.setText('頂点を削除')
        self.grid_step_field.setSuffix(' m グリッド')
        self.angle_step_field.setSuffix('° 角度')

        for dock in self.findChildren(QDockWidget):
            layout = dock.widget().layout() if dock.widget() is not None else None
            if dock.windowTitle() == 'Inspector':
                dock.setWindowTitle('インスペクター')
                if isinstance(layout, QFormLayout):
                    fields = (
                        (self.kind_label, '種類'),
                        (self.id_label, 'ID'),
                        (self.state_label, '状態'),
                        (self.position_fields['X'], 'X'),
                        (self.position_fields['Y'], 'Y'),
                        (self.position_fields['Z'], 'Z'),
                        (self.orientation_fields['Yaw'], 'ヨー'),
                        (self.orientation_fields['Pitch'], 'ピッチ'),
                        (self.orientation_fields['Roll'], 'ロール'),
                        (self.aim_label, '向き'),
                    )
                    for field, label in fields:
                        widget = layout.labelForField(field)
                        if isinstance(widget, QLabel):
                            widget.setText(label)
            elif dock.windowTitle() == 'Room':
                dock.setWindowTitle('部屋')
                if isinstance(layout, QFormLayout):
                    fields = (
                        (self.room_tool_label, '部屋ツール'),
                        (self.room_bounds_label, '範囲'),
                        (self.room_vertex_x, '頂点 X'),
                        (self.room_vertex_y, '頂点 Y'),
                        (self.room_edge_length, '辺の長さ'),
                        (self.room_height, '天井高'),
                    )
                    for field, label in fields:
                        widget = layout.labelForField(field)
                        if isinstance(widget, QLabel):
                            widget.setText(label)
        self._localize_tree()

    def _localize_tree(self) -> None:
        if not hasattr(self, 'tree'):
            return
        replacements = {
            'Speakers': 'スピーカー',
            'Listening / Measurement': 'リスニング・測定点',
            'Furniture': '家具',
        }
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            text = item.text(0)
            if text == 'Room · not created':
                item.setText(0, '部屋 · 未作成')
            elif text.startswith('Room · '):
                localized = text.replace('Room · ', '部屋 · ', 1)
                localized = localized.replace(' vertices · ', ' 頂点 · ')
                localized = localized.replace(' m high', ' m 高さ')
                item.setText(0, localized)
            elif text in replacements:
                item.setText(0, replacements[text])

    def _rebuild(self, *, reset_camera: bool = False) -> None:
        super()._rebuild(reset_camera=reset_camera)
        self._localize_tree()
        self._render_wall_overlay()
        self._refresh_wall_inspector()

    def _current_topology(self) -> WallTopology | None:
        if self.working is None:
            return None
        return self.working.committed_document.wall_topology

    def _ensure_wall_topology(self) -> bool:
        room = self._current_room()
        if room is None:
            self.statusBar().showMessage('先に部屋を作成してください')
            return False
        if self._current_topology() is not None:
            return True
        topology = make_wall_topology(room)
        if self.working.replace_room_topology(room, topology):
            self._sync_recovery()
            self._rebuild()
        return True

    def _remove_wall_overlay(self) -> None:
        for name in tuple(self._wall_actor_names):
            self.viewport.remove_actor(name, reset_camera=False, render=False)
        self._wall_actor_names.clear()

    def _render_wall_overlay(
        self,
        room: RoomPrism | None = None,
        topology: WallTopology | None = None,
    ) -> None:
        if not hasattr(self, 'viewport'):
            return
        self._remove_wall_overlay()
        room = room or self._current_room()
        topology = topology or self._current_topology()
        if room is None or topology is None:
            return

        by_id = {vertex.vertex_id: vertex for vertex in room_vertices(room)}
        for wall in topology.walls:
            prism_name = f'wall-prism:{wall.wall_id}'
            prism = _wall_prism(room, wall)
            if prism.n_points:
                self.viewport.add_mesh(
                    prism,
                    opacity=0.22,
                    pickable=False,
                    name=prism_name,
                    render=False,
                )
                self._wall_actor_names.add(prism_name)
            start = by_id[wall.from_vertex_id]
            end = by_id[wall.to_vertex_id]
            line_name = f'wall-line:{wall.wall_id}'
            self.viewport.add_mesh(
                pv.Line((start.x_m, -start.y_m, 0.03), (end.x_m, -end.y_m, 0.03)),
                line_width=6 if wall.wall_id == self.selected_wall_id else 3,
                pickable=False,
                name=line_name,
                render=False,
            )
            self._wall_actor_names.add(line_name)

        for opening in topology.openings:
            wall = next(wall for wall in topology.walls if wall.wall_id == opening.wall_id)
            start = by_id[wall.from_vertex_id]
            end = by_id[wall.to_vertex_id]
            length = wall_length(room, wall)
            ux = (end.x_m - start.x_m) / length
            uy = (end.y_m - start.y_m) / length
            x1 = start.x_m + ux * opening.offset_m
            y1 = start.y_m + uy * opening.offset_m
            x2 = start.x_m + ux * (opening.offset_m + opening.width_m)
            y2 = start.y_m + uy * (opening.offset_m + opening.width_m)
            name = f'wall-opening:{opening.opening_id}'
            self.viewport.add_mesh(
                pv.Line((x1, -y1, opening.sill_m + 0.02), (x2, -y2, opening.sill_m + 0.02)),
                line_width=9,
                pickable=False,
                name=name,
                render=False,
            )
            self._wall_actor_names.add(name)
        self.viewport.render()

    def _project_xy_to_qt(self, x_m: float, y_m: float) -> tuple[float, float]:
        return self._project_room_to_qt(RoomVertex(vertex_id='projection', x_m=x_m, y_m=y_m))

    @staticmethod
    def _point_segment_distance(
        px: float,
        py: float,
        ax: float,
        ay: float,
        bx: float,
        by: float,
    ) -> float:
        dx = bx - ax
        dy = by - ay
        denom = dx * dx + dy * dy
        if denom <= 1e-12:
            return hypot(px - ax, py - ay)
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denom))
        return hypot(px - (ax + t * dx), py - (ay + t * dy))

    def _hit_wall(self, qt_x: float, qt_y: float) -> str | None:
        room = self._current_room()
        topology = self._current_topology()
        if room is None or topology is None:
            return None
        by_id = {vertex.vertex_id: vertex for vertex in room_vertices(room)}
        hits: list[tuple[float, str]] = []
        for wall in topology.walls:
            start = by_id[wall.from_vertex_id]
            end = by_id[wall.to_vertex_id]
            ax, ay = self._project_xy_to_qt(start.x_m, start.y_m)
            bx, by = self._project_xy_to_qt(end.x_m, end.y_m)
            distance = self._point_segment_distance(qt_x, qt_y, ax, ay, bx, by)
            if distance <= 12.0:
                hits.append((distance, wall.wall_id))
        return min(hits, default=(0.0, None), key=lambda item: item[0])[1]

    def start_wall_edit(self) -> None:
        if self.recovery_candidate is not None or self.working is None:
            return
        if self.room_mode != 'idle':
            self.finish_room_edit()
        if not self._ensure_wall_topology():
            return
        self._select(None)
        self._remove_gizmo()
        self.wall_edit_active = True
        self.selected_wall_id = None
        self._top()
        self._rebuild()
        self.statusBar().showMessage('壁編集 · 壁をクリックして選択、ドラッグで移動 · Escで終了')

    def finish_wall_edit(self) -> None:
        self._cancel_wall_drag(rebuild=False)
        self.wall_edit_active = False
        self.selected_wall_id = None
        self._rebuild()
        self.statusBar().showMessage('壁編集を終了しました · オブジェクト編集が有効です')

    def _cancel_wall_drag(self, *, rebuild: bool = True) -> None:
        if QWidget.mouseGrabber() is self.viewport.interactor:
            self.viewport.interactor.releaseMouse()
        self.wall_drag_wall_id = None
        self.wall_drag_start_xy = None
        self.wall_drag_before_room = None
        self.wall_drag_before_topology = None
        self.wall_drag_preview_room = None
        self.wall_drag_preview_topology = None
        if rebuild:
            self._rebuild()

    def _commit_wall_drag(self) -> None:
        room = self.wall_drag_preview_room
        topology = self.wall_drag_preview_topology
        selected = self.wall_drag_wall_id
        if QWidget.mouseGrabber() is self.viewport.interactor:
            self.viewport.interactor.releaseMouse()
        self.wall_drag_wall_id = None
        self.wall_drag_start_xy = None
        self.wall_drag_before_room = None
        self.wall_drag_before_topology = None
        self.wall_drag_preview_room = None
        self.wall_drag_preview_topology = None
        if room is None or topology is None:
            self._rebuild()
            self.statusBar().showMessage('壁の移動は変更なしで終了しました')
            return
        changed = self.working.replace_room_topology(room, topology)
        if changed:
            self._sync_recovery()
        self.selected_wall_id = selected
        self._rebuild()
        self.statusBar().showMessage('壁を移動しました · 元に戻すで1操作を復元できます')

    def split_selected_wall(self) -> None:
        room = self._current_room()
        topology = self._current_topology()
        if not self.wall_edit_active or room is None or topology is None or self.selected_wall_id is None:
            return
        wall = next(wall for wall in topology.walls if wall.wall_id == self.selected_wall_id)
        token = uuid4().hex[:10]
        first_id = f'wall-{token}-a'
        second_id = f'wall-{token}-b'
        try:
            new_room, new_topology = split_wall(
                room,
                topology,
                wall.wall_id,
                offset_m=wall_length(room, wall) / 2.0,
                new_vertex_id=f'wall-v-{token}',
                first_wall_id=first_id,
                second_wall_id=second_id,
            )
        except WallTopologyError as exc:
            self.statusBar().showMessage(f'壁を分割できません · {exc}')
            return
        if self.working.replace_room_topology(new_room, new_topology):
            self._sync_recovery()
        self.selected_wall_id = first_id
        self._rebuild()
        self.statusBar().showMessage('壁を中央で分割しました · 開口とクリアランス参照は子壁へ追従します')

    def merge_selected_wall_with_next(self) -> None:
        room = self._current_room()
        topology = self._current_topology()
        if not self.wall_edit_active or room is None or topology is None or self.selected_wall_id is None:
            return
        index = next(
            (i for i, wall in enumerate(topology.walls) if wall.wall_id == self.selected_wall_id),
            None,
        )
        if index is None or index + 1 >= len(topology.walls):
            self.statusBar().showMessage('この壁には順方向の結合対象がありません')
            return
        merged_id = f'wall-{uuid4().hex[:10]}'
        try:
            new_room, new_topology = merge_walls(
                room,
                topology,
                self.selected_wall_id,
                topology.walls[index + 1].wall_id,
                merged_wall_id=merged_id,
            )
        except WallTopologyError as exc:
            self.statusBar().showMessage(f'壁を結合できません · {exc}')
            return
        if self.working.replace_room_topology(new_room, new_topology):
            self._sync_recovery()
        self.selected_wall_id = merged_id
        self._rebuild()
        self.statusBar().showMessage('壁を結合しました · 開口とクリアランス参照を1操作で更新しました')

    def delete_selected_wall(self) -> None:
        room = self._current_room()
        topology = self._current_topology()
        if not self.wall_edit_active or room is None or topology is None or self.selected_wall_id is None:
            return
        replacement_id = f'wall-{uuid4().hex[:10]}'
        try:
            new_room, new_topology = delete_wall(
                room,
                topology,
                self.selected_wall_id,
                replacement_wall_id=replacement_id,
            )
        except WallTopologyError as exc:
            self.statusBar().showMessage(f'壁を削除できません · {exc}')
            return
        if self.working.replace_room_topology(new_room, new_topology):
            self._sync_recovery()
        self.selected_wall_id = replacement_id
        self._rebuild()
        self.statusBar().showMessage('壁を削除しました · 参照切れがないことを確認して確定しました')

    def add_door_opening(self) -> None:
        room = self._current_room()
        topology = self._current_topology()
        if not self.wall_edit_active or room is None or topology is None or self.selected_wall_id is None:
            return
        wall = next(wall for wall in topology.walls if wall.wall_id == self.selected_wall_id)
        length = wall_length(room, wall)
        if length <= 0.25:
            self.statusBar().showMessage('壁が短すぎるため開口を追加できません')
            return
        width = min(0.9, max(0.1, length - 0.1))
        opening = WallOpening(
            opening_id=f'opening-{uuid4().hex[:10]}',
            wall_id=wall.wall_id,
            offset_m=(length - width) / 2.0,
            width_m=width,
            sill_m=0.0,
            height_m=min(2.0, room.height_m),
            kind='door',
            is_open=False,
        )
        try:
            new_topology = add_opening(room, topology, opening)
        except WallTopologyError as exc:
            self.statusBar().showMessage(f'開口を追加できません · {exc}')
            return
        if self.working.replace_room_topology(room, new_topology):
            self._sync_recovery()
        self._rebuild()
        self.statusBar().showMessage('ドア開口を追加しました · 壁ローカル位置で保持します')

    def add_clearance_binding(self) -> None:
        room = self._current_room()
        topology = self._current_topology()
        if not self.wall_edit_active or room is None or topology is None or self.selected_wall_id is None:
            return
        binding = WallConstraintBinding(
            binding_id=f'clearance-{uuid4().hex[:10]}',
            wall_ids=(self.selected_wall_id,),
            clearance_m=float(self.clearance_value.value()),
        )
        try:
            new_topology = add_constraint_binding(room, topology, binding)
        except (ValueError, WallTopologyError) as exc:
            self.statusBar().showMessage(f'クリアランス参照を追加できません · {exc}')
            return
        if self.working.replace_room_topology(room, new_topology):
            self._sync_recovery()
        self._rebuild()
        self.statusBar().showMessage('クリアランス参照を追加しました · 壁IDの変更にも追従します')

    def _numeric_wall_thickness_edited(self) -> None:
        room = self._current_room()
        topology = self._current_topology()
        if not self.wall_edit_active or room is None or topology is None or self.selected_wall_id is None:
            return
        walls = list(topology.walls)
        for index, wall in enumerate(walls):
            if wall.wall_id == self.selected_wall_id:
                walls[index] = wall.model_copy(update={'thickness_m': float(self.wall_thickness.value())})
                break
        candidate = WallTopology(
            walls=tuple(walls),
            openings=topology.openings,
            constraint_bindings=topology.constraint_bindings,
        )
        try:
            validate_wall_topology(room, candidate)
        except WallTopologyError as exc:
            self._refresh_wall_inspector()
            self.statusBar().showMessage(f'壁厚を変更できません · {exc}')
            return
        if self.working.replace_room_topology(room, candidate):
            self._sync_recovery()
            self._rebuild()
            self.statusBar().showMessage('壁厚を変更しました · 室内境界と参照は維持されています')
        else:
            self._refresh_wall_inspector()

    def _refresh_wall_inspector(self) -> None:
        if not hasattr(self, 'wall_mode_label'):
            return
        self.wall_mode_label.setText('壁編集' if self.wall_edit_active else 'オブジェクト編集')
        room = self._current_room()
        topology = self._current_topology()
        self.wall_thickness.setEnabled(False)
        self.clearance_value.setEnabled(self.wall_edit_active and self.recovery_candidate is None)
        if room is None or topology is None or self.selected_wall_id is None:
            self.wall_id_label.setText('—')
            self.wall_length_label.setText('—')
            self.wall_openings_label.setText('—')
            self.wall_constraints_label.setText('—')
            return
        wall = next((wall for wall in topology.walls if wall.wall_id == self.selected_wall_id), None)
        if wall is None:
            self.wall_id_label.setText('—')
            return
        self.wall_id_label.setText(wall.wall_id)
        self.wall_length_label.setText(f'{wall_length(room, wall):.3f} m')
        self.wall_openings_label.setText(
            str(sum(1 for opening in topology.openings if opening.wall_id == wall.wall_id))
        )
        self.wall_constraints_label.setText(
            str(sum(1 for binding in topology.constraint_bindings if wall.wall_id in binding.wall_ids))
        )
        with QSignalBlocker(self.wall_thickness):
            self.wall_thickness.setValue(wall.thickness_m)
        self.wall_thickness.setEnabled(self.wall_edit_active and self.recovery_candidate is None)

    def _update_actions(self) -> None:
        super()._update_actions()
        if not hasattr(self, 'start_wall_action'):
            return
        blocked = self.recovery_candidate is not None or self.working is None
        selected = self.wall_edit_active and self.selected_wall_id is not None
        self.start_wall_action.setEnabled(not blocked and self._current_room() is not None and not self.wall_edit_active)
        self.finish_wall_action.setEnabled(not blocked and self.wall_edit_active)
        self.split_wall_action.setEnabled(not blocked and selected)
        self.merge_wall_action.setEnabled(not blocked and selected)
        self.delete_wall_action.setEnabled(not blocked and selected)
        self.add_opening_action.setEnabled(not blocked and selected)
        self.add_clearance_action.setEnabled(not blocked and selected)
        if self.wall_edit_active:
            for action in (
                self.move_action,
                self.rotate_action,
                self.delete_action,
                self.draw_room_action,
                self.edit_room_action,
                self.close_room_action,
                self.done_room_action,
                self.insert_vertex_action,
                self.delete_vertex_action,
            ):
                action.setEnabled(False)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if watched is self.viewport.interactor and self.wall_edit_active:
            event_type = event.type()
            if event_type == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
                if self.wall_drag_wall_id is not None:
                    self._cancel_wall_drag()
                    self.statusBar().showMessage('壁の移動をキャンセルしました · 履歴は増えていません')
                else:
                    self.finish_wall_edit()
                return True

            position = getattr(event, 'position', lambda: None)()
            qt_x = float(position.x()) if position is not None else 0.0
            qt_y = float(position.y()) if position is not None else 0.0

            if event_type == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                wall_id = self._hit_wall(qt_x, qt_y)
                self.selected_wall_id = wall_id
                self._refresh_wall_inspector()
                self._update_actions()
                self._render_wall_overlay()
                if wall_id is None:
                    self.statusBar().showMessage('壁をクリックして選択してください')
                    return True
                floor = self._screen_to_floor(qt_x, qt_y)
                if floor is None:
                    return True
                self.wall_drag_wall_id = wall_id
                self.wall_drag_start_xy = floor
                self.wall_drag_before_room = self._current_room()
                self.wall_drag_before_topology = self._current_topology()
                self.wall_drag_preview_room = None
                self.wall_drag_preview_topology = None
                self.viewport.interactor.grabMouse()
                self.statusBar().showMessage('壁を選択しました · ドラッグで移動、離すと確定します')
                return True

            if event_type == QEvent.Type.MouseMove and self.wall_drag_wall_id is not None:
                floor = self._screen_to_floor(qt_x, qt_y)
                if floor is None or self.wall_drag_start_xy is None:
                    return True
                before_room = self.wall_drag_before_room
                before_topology = self.wall_drag_before_topology
                if before_room is None or before_topology is None:
                    return True
                dx = floor[0] - self.wall_drag_start_xy[0]
                dy = floor[1] - self.wall_drag_start_xy[1]
                try:
                    preview_room, preview_topology = move_wall(
                        before_room,
                        before_topology,
                        self.wall_drag_wall_id,
                        delta_x_m=dx,
                        delta_y_m=dy,
                    )
                except WallTopologyError as exc:
                    self.wall_drag_preview_room = None
                    self.wall_drag_preview_topology = None
                    self.statusBar().showMessage(f'この位置には移動できません · {exc}')
                    self._render_wall_overlay(before_room, before_topology)
                    return True
                self.wall_drag_preview_room = preview_room
                self.wall_drag_preview_topology = preview_topology
                self._render_wall_overlay(preview_room, preview_topology)
                return True

            if (
                event_type == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.LeftButton
                and self.wall_drag_wall_id is not None
            ):
                self._commit_wall_drag()
                return True
            if event_type == QEvent.Type.MouseButtonRelease:
                return True
        return super().eventFilter(watched, event)

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.WindowDeactivate and self.wall_drag_wall_id is not None:
            self._cancel_wall_drag()
        return super().event(event)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='HTDT native CAD N30b 壁・開口エディター')
    parser.add_argument('--data-dir', type=Path, default=default_data_dir())
    parser.add_argument('--document-id', default=F1_DOCUMENT_ID)
    args = parser.parse_args(argv)
    app = QApplication([sys.argv[0]])
    repository = SceneRepository(args.data_dir / 'cad-scenes.sqlite3')
    window = WallEditorWindow(repository, args.document_id)
    window.show()
    return int(app.exec())


if __name__ == '__main__':
    raise SystemExit(main())
