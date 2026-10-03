from __future__ import annotations

import argparse
from math import hypot
import os
from pathlib import Path
import sys
from typing import Literal
from uuid import uuid4

import numpy as np
import pyvista as pv
from PySide6.QtCore import QEvent, QSignalBlocker, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDockWidget,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QToolBar,
    QTreeWidgetItem,
    QWidget,
)

from .acoustic_treatment_service import (
    TREATMENT_TYPES,
    AcousticTreatmentService,
)
from .cad_document import EditorViewState
from .cad_repository import SceneRepository
from .cad_room import RoomWorkingDocument
from .cad_scene import (
    F1_DOCUMENT_ID,
    Position3,
    RoomPrism,
    RoomVertex,
    make_empty_scene,
    make_polygon_room,
    room_vertices,
)
from .native_editor import NativeEditorWindow, ROLE, default_data_dir
from .user_facing_error import operation_error_message


RoomMode = Literal['idle', 'sketch', 'edit']


def _room_wireframe(room: RoomPrism) -> pv.PolyData:
    vertices = room_vertices(room)
    count = len(vertices)
    points = np.asarray(
        [(vertex.x_m, -vertex.y_m, 0.0) for vertex in vertices]
        + [(vertex.x_m, -vertex.y_m, room.height_m) for vertex in vertices],
        dtype=float,
    )
    lines: list[int] = []
    for offset in (0, count):
        for index in range(count):
            lines.extend((2, offset + index, offset + ((index + 1) % count)))
    for index in range(count):
        lines.extend((2, index, count + index))
    mesh = pv.PolyData(points)
    mesh.lines = np.asarray(lines, dtype=np.int64)
    return mesh


def _polyline(vertices: tuple[RoomVertex, ...], *, close: bool) -> pv.PolyData | None:
    if len(vertices) < 2:
        return None
    points = [(vertex.x_m, -vertex.y_m, 0.0) for vertex in vertices]
    if close:
        points.append(points[0])
    return pv.lines_from_points(np.asarray(points, dtype=float), close=False)


class RoomEditorWindow(NativeEditorWindow):
    """N30a room-sketch layer over the accepted N20b native editor shell."""

    def __init__(self, repository: SceneRepository, document_id: str = F1_DOCUMENT_ID) -> None:
        # NativeEditorWindow calls virtual _load_or_seed/_rebuild during construction,
        # so room-tool state must exist before super().__init__().
        self.room_mode: RoomMode = 'idle'
        self.room_sketch_vertices: list[RoomVertex] = []
        self.room_cursor_xy: tuple[float, float] | None = None
        self.room_drag_vertex_id: str | None = None
        self.room_drag_before: tuple[RoomVertex, ...] = ()
        self.room_drag_preview: tuple[RoomVertex, ...] = ()
        self.selected_room_vertex_id: str | None = None
        self.selected_room_edge_index: int | None = None
        self.treatment_service = AcousticTreatmentService(
            repository, document_id
        )
        self.treatment_surface_combo: QComboBox | None = None
        self.treatment_list_label: QLabel | None = None
        super().__init__(repository, document_id)
        self.setWindowTitle('Home Theater Digital Twin — N30a 部屋エディター')

        room_toolbar = QToolBar('部屋', self)
        self.addToolBar(room_toolbar)
        self.addToolBarBreak()
        self.draw_room_action = QAction('部屋を作図', self)
        self.draw_room_action.setToolTip('部屋の外形を3D上でクリックして描き始めます')
        self.draw_room_action.triggered.connect(self.start_room_sketch)
        self.edit_room_action = QAction('部屋を編集', self)
        self.edit_room_action.setToolTip('既存の部屋の頂点・辺を編集モードで調整します')
        self.edit_room_action.triggered.connect(self.start_room_edit)
        self.close_room_action = QAction('部屋を閉じる', self)
        self.close_room_action.setToolTip('描画中の輪郭を閉じて部屋を確定します')
        self.close_room_action.triggered.connect(self.close_room_sketch)
        self.done_room_action = QAction('部屋編集を終了', self)
        self.done_room_action.setToolTip('部屋の編集モードを終了します')
        self.done_room_action.triggered.connect(self.finish_room_edit)
        self.insert_vertex_action = QAction('頂点を挿入', self)
        self.insert_vertex_action.setToolTip('ONの間、辺をクリックするとその位置に頂点を挿入します')
        self.insert_vertex_action.setCheckable(True)
        self.insert_vertex_action.toggled.connect(self._insert_vertex_toggled)
        self.delete_vertex_action = QAction('頂点を削除', self)
        self.delete_vertex_action.setToolTip('選択中の頂点を削除します')
        self.delete_vertex_action.triggered.connect(self.delete_room_vertex)
        room_toolbar.addActions(
            (
                self.draw_room_action,
                self.edit_room_action,
                self.close_room_action,
                self.done_room_action,
                self.insert_vertex_action,
                self.delete_vertex_action,
            )
        )

        room_inspector = QWidget()
        room_form = QFormLayout(room_inspector)
        self.room_tool_label = QLabel('オブジェクト')
        self.room_bounds_label = QLabel('—')
        room_form.addRow('部屋ツール', self.room_tool_label)
        room_form.addRow('範囲', self.room_bounds_label)

        self.room_vertex_x = QDoubleSpinBox()
        self.room_vertex_x.setRange(-1000.0, 1000.0)
        self.room_vertex_x.setDecimals(4)
        self.room_vertex_x.setSingleStep(0.01)
        self.room_vertex_x.setSuffix(' m')
        self.room_vertex_x.setKeyboardTracking(False)
        self.room_vertex_x.editingFinished.connect(self._numeric_room_vertex_edited)
        room_form.addRow('頂点 X', self.room_vertex_x)

        self.room_vertex_y = QDoubleSpinBox()
        self.room_vertex_y.setRange(-1000.0, 1000.0)
        self.room_vertex_y.setDecimals(4)
        self.room_vertex_y.setSingleStep(0.01)
        self.room_vertex_y.setSuffix(' m')
        self.room_vertex_y.setKeyboardTracking(False)
        self.room_vertex_y.editingFinished.connect(self._numeric_room_vertex_edited)
        room_form.addRow('頂点 Y', self.room_vertex_y)

        self.room_edge_length = QDoubleSpinBox()
        self.room_edge_length.setRange(0.001, 1000.0)
        self.room_edge_length.setDecimals(4)
        self.room_edge_length.setSingleStep(0.01)
        self.room_edge_length.setSuffix(' m')
        self.room_edge_length.setKeyboardTracking(False)
        self.room_edge_length.editingFinished.connect(self._numeric_room_edge_edited)
        room_form.addRow('辺の長さ', self.room_edge_length)

        self.room_height = QDoubleSpinBox()
        self.room_height.setRange(0.1, 20.0)
        self.room_height.setDecimals(3)
        self.room_height.setSingleStep(0.05)
        self.room_height.setSuffix(' m')
        self.room_height.setKeyboardTracking(False)
        self.room_height.editingFinished.connect(self._numeric_room_height_edited)
        room_form.addRow('天井高', self.room_height)

        room_field_hints = {
            '頂点 X': '選択中の頂点のX座標（m、部屋座標）· +X=部屋右',
            '頂点 Y': '選択中の頂点のY座標（m、部屋座標）· +Y=部屋奥',
            '辺の長さ': '選択中の辺の長さ（m）· 変更すると終点側の頂点が移動します',
            '天井高': '床から天井までの高さ（m）· すべての壁に共通です',
        }
        for row_text, field in (
            ('頂点 X', self.room_vertex_x),
            ('頂点 Y', self.room_vertex_y),
            ('辺の長さ', self.room_edge_length),
            ('天井高', self.room_height),
        ):
            field.setToolTip(room_field_hints[row_text])
            label = room_form.labelForField(field)
            if label is not None:
                label.setToolTip(room_field_hints[row_text])

        room_form.addRow(QLabel('— 音響トリートメント —'))
        self.treatment_name_field = QLineEdit()
        self.treatment_name_field.setPlaceholderText('例: 60x120 吸音材')
        room_form.addRow('トリートメント名', self.treatment_name_field)
        self.treatment_type_combo = QComboBox()
        self.treatment_type_combo.addItems(TREATMENT_TYPES)
        room_form.addRow('種類', self.treatment_type_combo)
        self.treatment_width = QDoubleSpinBox()
        self.treatment_width.setRange(0.05, 10.0)
        self.treatment_width.setValue(0.60)
        self.treatment_width.setSuffix(' m')
        room_form.addRow('幅', self.treatment_width)
        self.treatment_height = QDoubleSpinBox()
        self.treatment_height.setRange(0.05, 10.0)
        self.treatment_height.setValue(1.20)
        self.treatment_height.setSuffix(' m')
        room_form.addRow('高さ', self.treatment_height)
        self.treatment_thickness = QDoubleSpinBox()
        self.treatment_thickness.setRange(0.005, 1.0)
        self.treatment_thickness.setValue(0.10)
        self.treatment_thickness.setDecimals(3)
        self.treatment_thickness.setSuffix(' m')
        room_form.addRow('厚さ', self.treatment_thickness)
        self.treatment_air_gap = QDoubleSpinBox()
        self.treatment_air_gap.setRange(0.0, 1.0)
        self.treatment_air_gap.setValue(0.0)
        self.treatment_air_gap.setDecimals(3)
        self.treatment_air_gap.setSuffix(' m')
        room_form.addRow('エアギャップ', self.treatment_air_gap)
        self.treatment_surface_combo = QComboBox()
        room_form.addRow('設置面', self.treatment_surface_combo)
        self.treatment_pos_x = QDoubleSpinBox()
        self.treatment_pos_x.setRange(-1000.0, 1000.0)
        self.treatment_pos_x.setValue(0.0)
        self.treatment_pos_x.setSuffix(' m')
        room_form.addRow('位置 X', self.treatment_pos_x)
        self.treatment_pos_y = QDoubleSpinBox()
        self.treatment_pos_y.setRange(-1000.0, 1000.0)
        self.treatment_pos_y.setValue(0.0)
        self.treatment_pos_y.setSuffix(' m')
        room_form.addRow('位置 Y', self.treatment_pos_y)
        self.treatment_pos_z = QDoubleSpinBox()
        self.treatment_pos_z.setRange(-100.0, 100.0)
        self.treatment_pos_z.setValue(1.2)
        self.treatment_pos_z.setSuffix(' m')
        room_form.addRow('位置 Z', self.treatment_pos_z)
        treatment_hints = {
            'トリートメント名': '吸音処理の表示名（例: 60x120 吸音材）',
            '種類': '吸音処理の構造種別（多孔質・バストラップ・拡散など）',
            '幅': 'パネルの幅（m）',
            '高さ': 'パネルの高さ（m）',
            '厚さ': '吸音材の厚さ（m）',
            'エアギャップ': 'パネル背面と壁の間の空気層（m）· 低音域の吸音に効きます',
            '設置面': '処理を取り付ける壁・天井の面',
            '位置 X': '設置位置のX座標（m、部屋座標）',
            '位置 Y': '設置位置のY座標（m、部屋座標）',
            '位置 Z': '設置位置の高さ（m、床基準）',
            '比較': 'A/B比較セットにつける名前（例: ベースライン vs A）',
        }
        treatment_fields = {
            'トリートメント名': self.treatment_name_field,
            '種類': self.treatment_type_combo,
            '幅': self.treatment_width,
            '高さ': self.treatment_height,
            '厚さ': self.treatment_thickness,
            'エアギャップ': self.treatment_air_gap,
            '設置面': self.treatment_surface_combo,
            '位置 X': self.treatment_pos_x,
            '位置 Y': self.treatment_pos_y,
            '位置 Z': self.treatment_pos_z,
        }
        for row_text, field in treatment_fields.items():
            field.setToolTip(treatment_hints[row_text])
            label = room_form.labelForField(field)
            if label is not None:
                label.setToolTip(treatment_hints[row_text])
        treatment_button = QPushButton('定義して配置')
        treatment_button.setToolTip('上の内容で吸音処理を定義し、指定位置に配置します')
        treatment_button.clicked.connect(self._create_and_place_treatment)
        room_form.addRow(treatment_button)
        self.treatment_list_label = QLabel('トリートメントなし')
        self.treatment_list_label.setWordWrap(True)
        room_form.addRow(self.treatment_list_label)
        self.treatment_compare_name = QLineEdit()
        self.treatment_compare_name.setPlaceholderText('例: ベースライン vs A')
        self.treatment_compare_name.setToolTip(treatment_hints['比較'])
        room_form.addRow('比較', self.treatment_compare_name)
        compare_button = QPushButton('A/B比較を作成')
        compare_button.setToolTip('現在の配置を名前つき比較候補として記録します')
        compare_button.clicked.connect(self._create_treatment_comparison)
        room_form.addRow(compare_button)

        room_dock = QDockWidget('部屋', self)
        room_dock.setWidget(room_inspector)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, room_dock)
        self._refresh_room_inspector()
        self._refresh_treatments()
        self._update_actions()

    def _refresh_treatments(self) -> None:
        """Refresh host-surface options and the placements list (#985)."""
        if self.treatment_surface_combo is not None:
            self.treatment_surface_combo.clear()
            self.treatment_surface_combo.addItem('(面なし)', None)
            for surface_id in self.treatment_service.host_surface_options():
                self.treatment_surface_combo.addItem(
                    surface_id, surface_id
                )
        if self.treatment_list_label is None:
            return
        placements = self.treatment_service.list_placements()
        if not placements:
            self.treatment_list_label.setText('トリートメントなし')
            return
        self.treatment_list_label.setText(
            '\n'.join(
                f"{item.instance_id[-8:]} {item.definition_id} "
                f"{item.lifecycle} wave={item.wave_capability}"
                for item in placements
            )
        )

    def _create_and_place_treatment(self) -> None:
        """Author an exact definition and place it (#985 §1/§2)."""
        try:
            definition = self.treatment_service.create_definition(
                name=(
                    self.treatment_name_field.text().strip()
                    or 'treatment'
                ),
                treatment_type=self.treatment_type_combo.currentText(),
                width_m=self.treatment_width.value(),
                height_m=self.treatment_height.value(),
                thickness_m=self.treatment_thickness.value(),
                air_gap_m=self.treatment_air_gap.value(),
            )
            placement = self.treatment_service.place_treatment(
                definition=definition,
                host_surface_id=(
                    self.treatment_surface_combo.currentData()
                ),
                position=Position3(
                    x_m=self.treatment_pos_x.value(),
                    y_m=self.treatment_pos_y.value(),
                    z_m=self.treatment_pos_z.value(),
                ),
            )
        except Exception as exc:
            self.statusBar().showMessage(f'トリートメントを配置できません · {operation_error_message(exc)}')
            return
        self.statusBar().showMessage(
            f'トリートメント配置 · {placement.instance_id[-8:]}'
        )
        self._refresh_treatments()

    def _create_treatment_comparison(self) -> None:
        """Named baseline + treatment design comparison (#985 §3)."""
        placements = self.treatment_service.list_placements()
        if not placements:
            self.statusBar().showMessage('比較するトリートメントがありません')
            return
        try:
            spec = self.treatment_service.create_comparison(
                name=(
                    self.treatment_compare_name.text().strip()
                    or 'ベースライン vs トリートメント'
                ),
                candidate_designs=(
                    (
                        'treatment A',
                        tuple(
                            item.instance_id for item in placements
                        ),
                    ),
                ),
            )
        except Exception as exc:
            self.statusBar().showMessage(f'比較を作成できません · {operation_error_message(exc)}')
            return
        self.statusBar().showMessage(
            f'比較を作成しました · {spec.name} '
            f'({len(spec.candidates)} 候補)'
        )

    def _load_or_seed(self) -> None:
        revision = self.repository.current_head(self.document_id)
        if revision is None:
            # Unknown document identities — including the legacy F1 default —
            # open as an empty scene. Synthetic demo content is only ever
            # persisted through the explicit development seed command (#627).
            revision = self.repository.save(
                make_empty_scene(self.document_id), parent_revision_id=None
            ).revision
        self.working = RoomWorkingDocument(
            revision.document,
            source_revision_id=revision.revision_id,
            saved_content_hash=revision.content_hash,
        )
        record = self.repository.view_state(self.document_id)
        if record is not None:
            self.view_state = EditorViewState(
                selected_id=record.selected_id,
                selected_ids=list(record.selected_ids),
                hidden_ids=set(record.hidden_ids),
                locked_ids=set(record.locked_ids),
            )
            self.view_state.sanitize(revision.document)
        self._sync_transform_controls()
        self.selected_id = self.view_state.selected_id
        self.recovery_candidate = self.repository.recovery(self.document_id)
        self._rebuild(reset_camera=True)
        if self.recovery_candidate is not None:
            self.statusBar().showMessage(
                f'リビジョン {revision.revision_id[:8]} · 復旧データあり · 下書きを復旧または復旧データを破棄を選択'
            )
        else:
            room_state = '部屋あり' if revision.document.room is not None else '空のシーン · 部屋を作図で開始'
            self.statusBar().showMessage(f'リビジョン {revision.revision_id[:8]} · {room_state} · 保存済み')

    def recover_draft(self) -> None:
        if self.recovery_candidate is None:
            return
        source_id = self.recovery_candidate.source_revision_id
        source = self.repository.get(source_id) if source_id is not None else None
        if source is None:
            self.statusBar().showMessage('復旧元のリビジョンが見つからないため復旧データを開けません')
            return
        self.working = RoomWorkingDocument(
            self.recovery_candidate.document,
            source_revision_id=source.revision_id,
            saved_content_hash=source.content_hash,
        )
        self.view_state.sanitize(self.working.committed_document)
        self.selected_id = self.view_state.selected_id
        self.recovery_candidate = None
        self._rebuild(reset_camera=True)
        self.statusBar().showMessage(f'リビジョン {source.revision_id[:8]} から下書きを復旧 · 未保存')

    def _rebuild(self, *, reset_camera: bool = False) -> None:
        if self.working is None:
            return
        selected_ids = self.view_state.selection
        primary_id = self.view_state.selected_id
        self.gizmo_rebuild_timer.stop()
        self.preview_inspect_timer.stop()
        self._reset_drag_snap_state()
        self._invalidate_scene_pick_cache()
        self._remove_gizmo()
        self.viewport.clear()
        self.viewport.add_axes()
        self.viewport.show_grid()
        self.tree.clear()
        self.actors.clear()
        self.actor_ids.clear()
        self.scene_picker.InitializePickList()
        self.items.clear()
        document = self.working.committed_document

        if document.room is None:
            room_item = QTreeWidgetItem(['部屋 · 未作成'])
        else:
            vertices = room_vertices(document.room)
            room_item = QTreeWidgetItem([
                f'部屋 · {len(vertices)} 頂点 · 高さ {document.room.height_m:.3g} m'
            ])
            room_actor = self.viewport.add_mesh(
                _room_wireframe(document.room),
                line_width=2,
                pickable=False,
                name='room-prism',
            )
            room_actor.prop.opacity = 0.55
        self.tree.addTopLevelItem(room_item)

        groups: dict[str, QTreeWidgetItem] = {}
        for key, label in (
            ('speaker', 'スピーカー'),
            ('measurement_point', 'リスニング・測定点'),
            ('furniture', '家具'),
        ):
            groups[key] = QTreeWidgetItem([label])
            self.tree.addTopLevelItem(groups[key])
        for entity in document.entities:
            self._add_entity(groups[entity.kind], entity)
        self.tree.expandAll()
        if reset_camera:
            self._perspective()
        valid_selected = tuple(entity_id for entity_id in selected_ids if entity_id in self.items)
        self._set_selection(valid_selected, primary_id=primary_id, cancel_preview=False, persist=False)
        if self.room_mode == 'edit':
            self._render_room_edit_handles()
        elif self.room_mode == 'sketch':
            self._render_room_sketch_overlay()
        self._refresh_room_inspector()
        self._update_actions()

    def _create_gizmo(self, entity_id: str | None) -> None:
        if self.room_mode != 'idle':
            return
        super()._create_gizmo(entity_id)

    def _current_room(self) -> RoomPrism | None:
        if self.working is None:
            return None
        return self.working.committed_document.room

    def _room_vertices(self) -> tuple[RoomVertex, ...]:
        room = self._current_room()
        if room is None:
            return ()
        return room_vertices(room)

    def _project_room_to_qt(self, vertex: RoomVertex) -> tuple[float, float]:
        renderer = self.viewport.renderer
        renderer.SetWorldPoint(float(vertex.x_m), -float(vertex.y_m), 0.0, 1.0)
        renderer.WorldToDisplay()
        display_x, display_y, _ = renderer.GetDisplayPoint()
        dpr = max(float(self.viewport.interactor.devicePixelRatioF()), 1e-9)
        _, render_height = self.viewport.render_window.GetSize()
        return (float(display_x) / dpr, (float(render_height) - float(display_y)) / dpr)

    def _screen_to_floor(self, qt_x: float, qt_y: float) -> tuple[float, float] | None:
        renderer = self.viewport.renderer
        dpr = max(float(self.viewport.interactor.devicePixelRatioF()), 1.0)
        _, render_height = self.viewport.render_window.GetSize()
        display_x = float(qt_x) * dpr
        display_y = float(render_height) - float(qt_y) * dpr

        def world(depth: float) -> np.ndarray | None:
            renderer.SetDisplayPoint(display_x, display_y, depth)
            renderer.DisplayToWorld()
            x, y, z, w = renderer.GetWorldPoint()
            if abs(float(w)) <= 1e-12:
                return None
            return np.asarray((x / w, y / w, z / w), dtype=float)

        near = world(0.0)
        far = world(1.0)
        if near is None or far is None:
            return None
        ray = far - near
        if abs(float(ray[2])) <= 1e-12:
            return None
        fraction = -float(near[2]) / float(ray[2])
        point = near + ray * fraction
        x_m = float(point[0])
        y_m = -float(point[1])
        if self.view_state.grid_snap_enabled:
            step = self.view_state.grid_step_m
            x_m = round(x_m / step) * step
            y_m = round(y_m / step) * step
        return (x_m, y_m)

    def _remove_room_tool_overlays(self) -> None:
        for name in (
            'room-sketch-line',
            'room-sketch-points',
            'room-sketch-cursor',
            'room-drag-line',
            'room-vertex-handles',
            'room-midpoint-handles',
        ):
            self.viewport.remove_actor(name, reset_camera=False, render=False)

    def _render_room_sketch_overlay(self) -> None:
        self._remove_room_tool_overlays()
        vertices = tuple(self.room_sketch_vertices)
        line = _polyline(vertices, close=False)
        if line is not None:
            self.viewport.add_mesh(line, line_width=3, pickable=False, name='room-sketch-line', render=False)
        if vertices:
            points = np.asarray([(v.x_m, -v.y_m, 0.0) for v in vertices], dtype=float)
            self.viewport.add_mesh(
                pv.PolyData(points),
                render_points_as_spheres=True,
                point_size=12,
                pickable=False,
                name='room-sketch-points',
                render=False,
            )
        if vertices and self.room_cursor_xy is not None:
            x_m, y_m = self.room_cursor_xy
            cursor_line = pv.Line(
                (vertices[-1].x_m, -vertices[-1].y_m, 0.0),
                (x_m, -y_m, 0.0),
            )
            self.viewport.add_mesh(
                cursor_line,
                line_width=2,
                pickable=False,
                name='room-sketch-cursor',
                render=False,
            )
        self.viewport.render()

    def _render_room_edit_handles(self) -> None:
        self._remove_room_tool_overlays()
        vertices = self.room_drag_preview or self._room_vertices()
        if not vertices:
            return
        if self.room_drag_preview:
            line = _polyline(vertices, close=True)
            if line is not None:
                self.viewport.add_mesh(line, line_width=3, pickable=False, name='room-drag-line', render=False)
        vertex_points = np.asarray([(v.x_m, -v.y_m, 0.0) for v in vertices], dtype=float)
        self.viewport.add_mesh(
            pv.PolyData(vertex_points),
            render_points_as_spheres=True,
            point_size=13,
            pickable=False,
            name='room-vertex-handles',
            render=False,
        )
        midpoint_points = []
        for index, start in enumerate(vertices):
            end = vertices[(index + 1) % len(vertices)]
            midpoint_points.append(((start.x_m + end.x_m) / 2.0, -(start.y_m + end.y_m) / 2.0, 0.0))
        self.viewport.add_mesh(
            pv.PolyData(np.asarray(midpoint_points, dtype=float)),
            render_points_as_spheres=True,
            point_size=8,
            pickable=False,
            name='room-midpoint-handles',
            render=False,
        )
        self.viewport.render()

    def _near_sketch_start(self, qt_x: float, qt_y: float) -> bool:
        if len(self.room_sketch_vertices) < 3:
            return False
        start_x, start_y = self._project_room_to_qt(self.room_sketch_vertices[0])
        return hypot(qt_x - start_x, qt_y - start_y) <= 12.0

    def _hit_room_handle(self, qt_x: float, qt_y: float) -> tuple[str, int] | None:
        vertices = self._room_vertices()
        if not vertices:
            return None
        hits: list[tuple[float, str, int]] = []
        for index, vertex in enumerate(vertices):
            x, y = self._project_room_to_qt(vertex)
            distance = hypot(qt_x - x, qt_y - y)
            if distance <= 12.0:
                hits.append((distance, 'vertex', index))
        for index, start in enumerate(vertices):
            end = vertices[(index + 1) % len(vertices)]
            midpoint = RoomVertex(
                vertex_id='midpoint',
                x_m=(start.x_m + end.x_m) / 2.0,
                y_m=(start.y_m + end.y_m) / 2.0,
            )
            x, y = self._project_room_to_qt(midpoint)
            distance = hypot(qt_x - x, qt_y - y)
            if distance <= 10.0:
                hits.append((distance, 'edge', index))
        if not hits:
            return None
        _, kind, index = min(hits, key=lambda item: item[0])
        return (kind, index)

    def start_room_sketch(self) -> None:
        if self.recovery_candidate is not None or self.working is None:
            return
        if self.working.has_preview:
            super().cancel_preview()
        self._select(None)
        self._remove_gizmo()
        self.room_mode = 'sketch'
        self.room_sketch_vertices = []
        self.room_cursor_xy = None
        self.room_drag_vertex_id = None
        self.room_drag_before = ()
        self.room_drag_preview = ()
        self.selected_room_vertex_id = None
        self.selected_room_edge_index = None
        self._top()
        self._rebuild()
        self.statusBar().showMessage('部屋を作図 · 頂点をクリック · 最初の頂点をクリックかEnterで閉合 · Escでキャンセル')

    def close_room_sketch(self) -> None:
        if self.room_mode != 'sketch':
            return
        if len(self.room_sketch_vertices) < 3:
            self.statusBar().showMessage('閉合には頂点が3つ以上必要です')
            return
        existing = self._current_room()
        height_m = existing.height_m if existing is not None else 2.4
        try:
            room = make_polygon_room(tuple(self.room_sketch_vertices), height_m=height_m)
        except ValueError as exc:
            self.statusBar().showMessage(f'部屋を閉合できません · {operation_error_message(exc)}')
            return
        changed = self._replace_room(room)
        self.room_mode = 'edit'
        self.room_sketch_vertices = []
        self.room_cursor_xy = None
        self.selected_room_vertex_id = room_vertices(room)[0].vertex_id
        self.selected_room_edge_index = None
        self._rebuild()
        verb = 'を作成しました' if changed else 'は変更なし'
        self.statusBar().showMessage(f'部屋{verb} · 頂点や寸法を編集できます · 部屋編集を終了でオブジェクトへ戻る')

    def start_room_edit(self) -> None:
        if self.recovery_candidate is not None or self._current_room() is None:
            self.statusBar().showMessage('部屋編集に入る前に部屋を作成してください')
            return
        if self.working is not None and self.working.has_preview:
            super().cancel_preview()
        self._select(None)
        self._remove_gizmo()
        self.room_mode = 'edit'
        self.room_sketch_vertices = []
        self.room_cursor_xy = None
        self.room_drag_vertex_id = None
        self.room_drag_before = ()
        self.room_drag_preview = ()
        self.selected_room_edge_index = None
        self._top()
        self._rebuild()
        self.statusBar().showMessage('部屋編集 · 頂点をドラッグ · 辺の中点を選択で辺の長さを編集 · 頂点を挿入+中点で頂点追加')

    def finish_room_edit(self) -> None:
        if self.room_mode == 'sketch':
            self.cancel_preview()
            return
        if self.room_mode != 'edit':
            return
        self._cancel_room_drag()
        self.room_mode = 'idle'
        self.selected_room_vertex_id = None
        self.selected_room_edge_index = None
        if hasattr(self, 'insert_vertex_action'):
            with QSignalBlocker(self.insert_vertex_action):
                self.insert_vertex_action.setChecked(False)
        self._rebuild()
        self.statusBar().showMessage('部屋編集を終了しました · オブジェクトツールが有効です')

    def _insert_vertex_toggled(self, checked: bool) -> None:
        if checked and self.room_mode != 'edit':
            self.start_room_edit()
        self._update_actions()

    def _insert_room_vertex(self, edge_index: int) -> None:
        room = self._current_room()
        if room is None:
            return
        vertices = list(room_vertices(room))
        start = vertices[edge_index]
        end = vertices[(edge_index + 1) % len(vertices)]
        inserted = RoomVertex(
            vertex_id=f'room-v-{uuid4().hex[:12]}',
            x_m=(start.x_m + end.x_m) / 2.0,
            y_m=(start.y_m + end.y_m) / 2.0,
        )
        vertices.insert(edge_index + 1, inserted)
        try:
            replacement = make_polygon_room(vertices, height_m=room.height_m, room_id=room.room_id)
        except ValueError as exc:
            self.statusBar().showMessage(f'頂点の挿入を拒否しました · {operation_error_message(exc)}')
            return
        if self._replace_room(replacement):
            self.selected_room_vertex_id = inserted.vertex_id
            self.selected_room_edge_index = None
            self._rebuild()
            self.statusBar().showMessage('頂点を挿入しました · 元に戻す1回で元の部屋に戻ります')

    def delete_room_vertex(self) -> None:
        room = self._current_room()
        if self.room_mode != 'edit' or room is None or self.selected_room_vertex_id is None:
            return
        vertices = list(room_vertices(room))
        if len(vertices) <= 3:
            self.statusBar().showMessage('部屋には頂点が3つ以上必要です')
            return
        vertices = [vertex for vertex in vertices if vertex.vertex_id != self.selected_room_vertex_id]
        try:
            replacement = make_polygon_room(vertices, height_m=room.height_m, room_id=room.room_id)
        except ValueError as exc:
            self.statusBar().showMessage(f'頂点の削除を拒否しました · {operation_error_message(exc)}')
            return
        if self._replace_room(replacement):
            self.selected_room_vertex_id = None
            self.selected_room_edge_index = None
            self._rebuild()
            self.statusBar().showMessage('頂点を削除しました · 元に戻す1回で復元できます')

    def _replace_room(self, room: RoomPrism) -> bool:
        if not isinstance(self.working, RoomWorkingDocument):
            return False
        changed = self.working.replace_room(room)
        if changed:
            self._sync_recovery()
        self._update_actions()
        return changed

    def _cancel_room_drag(self) -> None:
        if self.room_drag_vertex_id is None:
            return
        if QWidget.mouseGrabber() is self.viewport.interactor:
            self.viewport.interactor.releaseMouse()
        self.room_drag_vertex_id = None
        self.room_drag_before = ()
        self.room_drag_preview = ()
        self._rebuild()

    def _commit_room_drag(self) -> None:
        room = self._current_room()
        if room is None or self.room_drag_vertex_id is None:
            self._cancel_room_drag()
            return
        preview = self.room_drag_preview or self.room_drag_before
        vertex_id = self.room_drag_vertex_id
        if QWidget.mouseGrabber() is self.viewport.interactor:
            self.viewport.interactor.releaseMouse()
        self.room_drag_vertex_id = None
        self.room_drag_before = ()
        self.room_drag_preview = ()
        try:
            replacement = make_polygon_room(preview, height_m=room.height_m, room_id=room.room_id)
        except ValueError as exc:
            self._rebuild()
            self.statusBar().showMessage(f'頂点の移動を拒否しました · {operation_error_message(exc)}')
            return
        changed = self._replace_room(replacement)
        self.selected_room_vertex_id = vertex_id
        self._rebuild()
        self.statusBar().showMessage('頂点の移動を確定しました · 元に戻す1回' if changed else '頂点の移動は変更なし')

    def _numeric_room_vertex_edited(self) -> None:
        room = self._current_room()
        if self.room_mode != 'edit' or room is None or self.selected_room_vertex_id is None:
            return
        vertices = list(room_vertices(room))
        for index, vertex in enumerate(vertices):
            if vertex.vertex_id == self.selected_room_vertex_id:
                vertices[index] = RoomVertex(
                    vertex_id=vertex.vertex_id,
                    x_m=self.room_vertex_x.value(),
                    y_m=self.room_vertex_y.value(),
                )
                break
        try:
            replacement = make_polygon_room(vertices, height_m=room.height_m, room_id=room.room_id)
        except ValueError as exc:
            self._refresh_room_inspector()
            self.statusBar().showMessage(f'頂点座標を拒否しました · {operation_error_message(exc)}')
            return
        changed = self._replace_room(replacement)
        if changed:
            self._rebuild()
            self.statusBar().showMessage('頂点座標を確定しました · 範囲は自動更新')
        else:
            self._refresh_room_inspector()

    def _numeric_room_edge_edited(self) -> None:
        room = self._current_room()
        if self.room_mode != 'edit' or room is None or self.selected_room_edge_index is None:
            return
        vertices = list(room_vertices(room))
        index = self.selected_room_edge_index % len(vertices)
        start = vertices[index]
        end_index = (index + 1) % len(vertices)
        end = vertices[end_index]
        dx = end.x_m - start.x_m
        dy = end.y_m - start.y_m
        current_length = hypot(dx, dy)
        if current_length <= 1e-12:
            return
        requested = self.room_edge_length.value()
        scale = requested / current_length
        vertices[end_index] = RoomVertex(
            vertex_id=end.vertex_id,
            x_m=start.x_m + dx * scale,
            y_m=start.y_m + dy * scale,
        )
        try:
            replacement = make_polygon_room(vertices, height_m=room.height_m, room_id=room.room_id)
        except ValueError as exc:
            self._refresh_room_inspector()
            self.statusBar().showMessage(f'辺の寸法を拒否しました · {operation_error_message(exc)}')
            return
        changed = self._replace_room(replacement)
        if changed:
            self._rebuild()
            self.statusBar().showMessage('辺の寸法を確定しました · 端点と範囲を更新')
        else:
            self._refresh_room_inspector()

    def _numeric_room_height_edited(self) -> None:
        room = self._current_room()
        if self.room_mode != 'edit' or room is None:
            return
        replacement = room.model_copy(update={'height_m': float(self.room_height.value())})
        replacement = RoomPrism.model_validate(replacement.model_dump(mode='python'))
        changed = self._replace_room(replacement)
        if changed:
            self._rebuild()
            self.statusBar().showMessage('天井高を確定しました · 元に戻す1回')
        else:
            self._refresh_room_inspector()

    def _refresh_room_inspector(self) -> None:
        if not hasattr(self, 'room_bounds_label'):
            return
        room = self._current_room()
        self.room_tool_label.setText(
            {'idle': '待機', 'sketch': '作図', 'edit': '編集'}.get(
                self.room_mode, self.room_mode
            )
        )
        editable = self.room_mode == 'edit' and self.recovery_candidate is None and room is not None
        for field in (self.room_vertex_x, self.room_vertex_y, self.room_edge_length, self.room_height):
            field.setEnabled(False)
        if room is None:
            self.room_bounds_label.setText('—')
            return
        min_x, min_y, max_x, max_y = room.bounds_m
        self.room_bounds_label.setText(
            f'X {min_x:.3f}…{max_x:.3f} m · Y {min_y:.3f}…{max_y:.3f} m'
        )
        with QSignalBlocker(self.room_height):
            self.room_height.setValue(room.height_m)
        self.room_height.setEnabled(editable)

        vertices = room_vertices(room)
        selected_vertex = next(
            (vertex for vertex in vertices if vertex.vertex_id == self.selected_room_vertex_id),
            None,
        )
        if selected_vertex is not None:
            with QSignalBlocker(self.room_vertex_x), QSignalBlocker(self.room_vertex_y):
                self.room_vertex_x.setValue(selected_vertex.x_m)
                self.room_vertex_y.setValue(selected_vertex.y_m)
            self.room_vertex_x.setEnabled(editable)
            self.room_vertex_y.setEnabled(editable)
        if self.selected_room_edge_index is not None and vertices:
            index = self.selected_room_edge_index % len(vertices)
            start = vertices[index]
            end = vertices[(index + 1) % len(vertices)]
            with QSignalBlocker(self.room_edge_length):
                self.room_edge_length.setValue(hypot(end.x_m - start.x_m, end.y_m - start.y_m))
            self.room_edge_length.setEnabled(editable)

    def _update_actions(self) -> None:
        super()._update_actions()
        if not hasattr(self, 'draw_room_action'):
            return
        blocked = self.recovery_candidate is not None or self.working is None
        room_exists = self._current_room() is not None
        self.draw_room_action.setEnabled(not blocked and self.room_mode != 'sketch')
        self.edit_room_action.setEnabled(not blocked and room_exists and self.room_mode != 'sketch')
        self.close_room_action.setEnabled(not blocked and self.room_mode == 'sketch')
        self.done_room_action.setEnabled(not blocked and self.room_mode == 'edit')
        self.insert_vertex_action.setEnabled(not blocked and self.room_mode == 'edit' and room_exists)
        self.delete_vertex_action.setEnabled(
            not blocked and self.room_mode == 'edit' and self.selected_room_vertex_id is not None
        )
        room_busy = self.room_mode == 'sketch' or self.room_drag_vertex_id is not None
        self.save_action.setEnabled(self.save_action.isEnabled() and not room_busy)

    def save(self) -> None:
        if self.room_mode == 'sketch' or self.room_drag_vertex_id is not None:
            self.statusBar().showMessage('保存前に実行中の部屋操作を完了またはキャンセルしてください')
            return
        super().save()

    def cancel_preview(self) -> None:
        if self.room_drag_vertex_id is not None:
            self._cancel_room_drag()
            self.statusBar().showMessage('頂点の移動をキャンセル · 履歴は変更なし')
            return
        if self.room_mode == 'sketch':
            self.room_mode = 'idle'
            self.room_sketch_vertices = []
            self.room_cursor_xy = None
            self._rebuild()
            self.statusBar().showMessage('部屋の作図をキャンセル · 履歴は変更なし')
            return
        if hasattr(self, 'insert_vertex_action') and self.insert_vertex_action.isChecked():
            self.insert_vertex_action.setChecked(False)
            self.statusBar().showMessage('頂点の挿入をキャンセル')
            return
        super().cancel_preview()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if watched is self.viewport.interactor and self.room_mode in {'sketch', 'edit'}:
            event_type = event.type()
            if event_type == QEvent.Type.KeyPress:
                if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.room_mode == 'sketch':
                    self.close_room_sketch()
                    return True
                if event.key() == Qt.Key.Key_Escape:
                    self.cancel_preview()
                    return True

            position = getattr(event, 'position', lambda: None)()
            if position is not None:
                qt_x = float(position.x())
                qt_y = float(position.y())
            else:
                qt_x = qt_y = 0.0

            if self.room_mode == 'sketch':
                if event_type == QEvent.Type.MouseMove:
                    floor = self._screen_to_floor(qt_x, qt_y)
                    if floor is not None:
                        self.room_cursor_xy = floor
                        self._render_room_sketch_overlay()
                    return True
                if event_type == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                    if self._near_sketch_start(qt_x, qt_y):
                        self.close_room_sketch()
                        return True
                    floor = self._screen_to_floor(qt_x, qt_y)
                    if floor is None:
                        return True
                    x_m, y_m = floor
                    self.room_sketch_vertices.append(
                        RoomVertex(vertex_id=f'room-v-{uuid4().hex[:12]}', x_m=x_m, y_m=y_m)
                    )
                    self.room_cursor_xy = floor
                    self._render_room_sketch_overlay()
                    self.statusBar().showMessage(
                        f'部屋を作図 · {len(self.room_sketch_vertices)} 頂点 · 始点クリック/Enterで閉合 · Escでキャンセル'
                    )
                    return True
                if event_type in (QEvent.Type.MouseButtonRelease, QEvent.Type.MouseButtonDblClick):
                    return True

            if self.room_mode == 'edit':
                if event_type == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                    hit = self._hit_room_handle(qt_x, qt_y)
                    if hit is None:
                        self.selected_room_vertex_id = None
                        self.selected_room_edge_index = None
                        self._refresh_room_inspector()
                        self._update_actions()
                        return True
                    kind, index = hit
                    vertices = self._room_vertices()
                    if kind == 'edge':
                        self.selected_room_vertex_id = None
                        self.selected_room_edge_index = index
                        if hasattr(self, 'insert_vertex_action') and self.insert_vertex_action.isChecked():
                            self._insert_room_vertex(index)
                        else:
                            self._refresh_room_inspector()
                            self._update_actions()
                            self.statusBar().showMessage('辺を選択しました · 正確な長さを入力するか頂点を挿入を有効化')
                        return True
                    vertex = vertices[index]
                    self.selected_room_vertex_id = vertex.vertex_id
                    self.selected_room_edge_index = None
                    self.room_drag_vertex_id = vertex.vertex_id
                    self.room_drag_before = vertices
                    self.room_drag_preview = vertices
                    self.viewport.interactor.grabMouse()
                    self._refresh_room_inspector()
                    self._update_actions()
                    return True
                if event_type == QEvent.Type.MouseMove and self.room_drag_vertex_id is not None:
                    floor = self._screen_to_floor(qt_x, qt_y)
                    if floor is None:
                        return True
                    x_m, y_m = floor
                    preview = list(self.room_drag_before)
                    for index, vertex in enumerate(preview):
                        if vertex.vertex_id == self.room_drag_vertex_id:
                            preview[index] = RoomVertex(vertex_id=vertex.vertex_id, x_m=x_m, y_m=y_m)
                            break
                    self.room_drag_preview = tuple(preview)
                    self._render_room_edit_handles()
                    return True
                if (
                    event_type == QEvent.Type.MouseButtonRelease
                    and event.button() == Qt.MouseButton.LeftButton
                    and self.room_drag_vertex_id is not None
                ):
                    self._commit_room_drag()
                    return True
                if event_type == QEvent.Type.MouseButtonRelease:
                    return True
        return super().eventFilter(watched, event)

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.WindowDeactivate and self.room_drag_vertex_id is not None:
            self._cancel_room_drag()
        return super().event(event)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='HTDT ネイティブCADエディター N30a 部屋シェルを起動')
    parser.add_argument('--data-dir', type=Path, default=default_data_dir())
    parser.add_argument('--document-id', default=F1_DOCUMENT_ID)
    args = parser.parse_args(argv)
    app = QApplication([sys.argv[0]])
    repository = SceneRepository(args.data_dir / 'cad-scenes.sqlite3')
    window = RoomEditorWindow(repository, args.document_id)
    window.show()
    return int(app.exec())


if __name__ == '__main__':
    raise SystemExit(main())
