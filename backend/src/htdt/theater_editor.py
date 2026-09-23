from __future__ import annotations

from uuid import uuid4

import numpy as np
import pyvista as pv
from pydantic import ValidationError
from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QDockWidget,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QToolBar,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .cad_composition import CadEditorWindow
from .cad_document import EditStateError
from .cad_gizmo import RotationWidget3D
from .cad_repository import SceneRepository
from .cad_scene import (
    F1_DOCUMENT_ID,
    PHYSICAL_ENTITY_KINDS,
    Offset3,
    Position3,
    SceneEntity,
    Size3,
    acoustic_reference_position,
    domain_pose_to_render_matrix,
    domain_to_render,
    is_unassigned_speaker_role,
    next_unassigned_speaker_role,
    quaternion_from_euler_deg,
)
from .native_editor import ROLE
from .room_editor import _room_wireframe
from .room_viewport import _entity_local_mesh


class TheaterEditorWindow(CadEditorWindow):
    """N40 theater-object layer on the accepted room/wall product composition."""

    def __init__(self, repository: SceneRepository, document_id: str = F1_DOCUMENT_ID) -> None:
        # Base constructors call virtual rebuild/inspect/action methods.
        self.object_actions: list[QAction] = []
        self.object_buttons: list[QPushButton] = []
        super().__init__(repository, document_id)
        self.setWindowTitle('Home Theater Digital Twin — シアターCAD')

        self._create_object_palette()
        self._create_object_inspector()
        self._create_object_toolbar()
        self._localize_scene_dock()
        self._inspect(self.selected_id)
        self._update_actions()

    def _localize_scene_dock(self) -> None:
        for dock in self.findChildren(QDockWidget):
            if dock.windowTitle() == 'Scene':
                dock.setWindowTitle('シーン')

    def _create_object_palette(self) -> None:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.addWidget(QLabel('追加'))
        grid = QGridLayout()
        layout.addLayout(grid)

        entries = (
            ('▰  スピーカー', 'speaker', 'スピーカーを追加して移動ツールで配置します'),
            ('▱  座席', 'seat', '座席本体と耳位置の音響基準点を追加します'),
            ('▭  スクリーン', 'screen', '薄いスクリーン面を追加します'),
            ('▮  ディスプレイ', 'display', 'TV・ダイレクトビューディスプレイを追加します'),
            ('□  家具', 'furniture', '家具の外形ボックスを追加します'),
            ('▦  AV機器', 'av_equipment', 'AVラック・機器の外形を追加します'),
            ('⊕  測定点', 'measurement_point', '物理寸法を持たない測定基準点を追加します'),
        )
        for index, (label, kind, tooltip) in enumerate(entries):
            button = QPushButton(label)
            button.setMinimumHeight(38)
            button.setToolTip(tooltip)
            button.clicked.connect(lambda checked=False, object_kind=kind: self.add_object(object_kind))
            grid.addWidget(button, index // 2, index % 2)
            self.object_buttons.append(button)

        template = QPushButton('3.0.2  スピーカーセット')
        template.setMinimumHeight(42)
        template.setToolTip('FL / C / FR / TFL / TFR を一括追加します。固定規格ではなく編集可能なテンプレートです')
        template.clicked.connect(self.add_302_template)
        layout.addWidget(template)
        self.object_buttons.append(template)
        layout.addStretch(1)

        dock = QDockWidget('オブジェクト', self)
        dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)

    def _create_object_toolbar(self) -> None:
        toolbar = QToolBar('オブジェクト', self)
        self.addToolBar(toolbar)
        self.duplicate_action = QAction('複製', self)
        self.duplicate_action.setShortcut(QKeySequence('Ctrl+D'))
        self.duplicate_action.setToolTip('選択オブジェクトを少しずらして複製')
        self.duplicate_action.triggered.connect(self.duplicate_selected_object)
        toolbar.addAction(self.duplicate_action)
        self.object_actions.append(self.duplicate_action)

    @staticmethod
    def _metric_field(*, minimum: float, maximum: float, step: float = 0.01) -> QDoubleSpinBox:
        field = QDoubleSpinBox()
        field.setRange(minimum, maximum)
        field.setDecimals(4)
        field.setSingleStep(step)
        field.setSuffix(' m')
        field.setKeyboardTracking(False)
        return field

    def _create_object_inspector(self) -> None:
        panel = QWidget()
        form = QFormLayout(panel)
        self.object_kind_label = QLabel('—')
        form.addRow('オブジェクト', self.object_kind_label)

        self.size_fields: dict[str, QDoubleSpinBox] = {}
        for axis in ('X', 'Y', 'Z'):
            field = self._metric_field(minimum=0.001, maximum=100.0)
            field.editingFinished.connect(self._numeric_size_edited)
            self.size_fields[axis] = field
            form.addRow(f'寸法 {axis}', field)

        self.speaker_role_field = QLineEdit()
        self.speaker_role_field.setPlaceholderText('未設定 · 例: FL / C / TFL')
        self.speaker_role_field.editingFinished.connect(self._speaker_role_edited)
        form.addRow('スピーカー役割', self.speaker_role_field)

        self.reference_enabled = QCheckBox('本体に追従する基準点')
        self.reference_enabled.toggled.connect(self._reference_enabled_toggled)
        form.addRow('音響基準点', self.reference_enabled)

        self.reference_fields: dict[str, QDoubleSpinBox] = {}
        for axis in ('X', 'Y', 'Z'):
            field = self._metric_field(minimum=-100.0, maximum=100.0)
            field.editingFinished.connect(self._numeric_reference_edited)
            self.reference_fields[axis] = field
            form.addRow(f'基準オフセット {axis}', field)

        hint = QLabel('位置・回転は上のインスペクターで編集 · 寸法と基準点は本体とは別に保持')
        hint.setWordWrap(True)
        form.addRow(hint)

        dock = QDockWidget('オブジェクト詳細', self)
        dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)

    def _rebuild(self, *, reset_camera: bool = False) -> None:
        """RoomEditor rebuild extended for all N40 entity groups, then wall overlays."""

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
            vertices = self._room_vertices()
            room_item = QTreeWidgetItem([
                f'部屋 · {len(vertices)} 頂点 · {document.room.height_m:.3g} m 高さ'
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
            ('seat', '座席'),
            ('screen', 'スクリーン'),
            ('display', 'ディスプレイ'),
            ('furniture', '家具'),
            ('av_equipment', 'AV機器'),
            ('measurement_point', 'リスニング・測定点'),
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
        self._localize_tree()
        self._render_wall_overlay()
        self._refresh_wall_inspector()
        self._refresh_object_inspector()
        self._update_actions()
        self.viewport.render()

    @staticmethod
    def _physical_mesh(entity: SceneEntity) -> pv.PolyData:
        size = entity.size_m
        if size is None:
            raise ValueError(f'{entity.entity_id} requires size_m for rendering')
        # Issue #464: an explicitly authored body shape (cylinder, polygon
        # extrusion, mesh asset) overrides the stylized per-kind placeholder.
        authored = _entity_local_mesh(entity)
        if authored is not None:
            return authored
        if entity.kind == 'speaker':
            body = pv.Cube(x_length=size.x_m, y_length=size.y_m, z_length=size.z_m)
            marker = pv.Cone(
                center=(0.0, -size.y_m * 0.62, 0.0),
                direction=(0.0, -1.0, 0.0),
                height=max(size.y_m * 0.24, 0.04),
                radius=max(min(size.x_m, size.z_m) * 0.16, 0.025),
                resolution=12,
            )
            return body.merge(marker, merge_points=False)
        if entity.kind == 'seat':
            seat_height = max(size.z_m * 0.34, 0.05)
            base = pv.Cube(
                center=(0.0, 0.0, -size.z_m * 0.24),
                x_length=size.x_m,
                y_length=size.y_m,
                z_length=seat_height,
            )
            back = pv.Cube(
                center=(0.0, size.y_m * 0.42, size.z_m * 0.08),
                x_length=size.x_m,
                y_length=max(size.y_m * 0.16, 0.05),
                z_length=max(size.z_m * 0.78, 0.08),
            )
            return base.merge(back, merge_points=False)
        if entity.kind == 'screen':
            return pv.Cube(x_length=size.x_m, y_length=size.y_m, z_length=size.z_m)
        if entity.kind == 'av_equipment':
            body = pv.Cube(x_length=size.x_m, y_length=size.y_m, z_length=size.z_m)
            shelf_height = max(size.z_m * 0.025, 0.01)
            meshes = [body]
            for fraction in (-0.22, 0.0, 0.22):
                meshes.append(pv.Cube(
                    center=(0.0, -size.y_m * 0.51, size.z_m * fraction),
                    x_length=size.x_m * 0.84,
                    y_length=max(size.y_m * 0.04, 0.01),
                    z_length=shelf_height,
                ))
            merged = meshes[0]
            for mesh in meshes[1:]:
                merged = merged.merge(mesh, merge_points=False)
            return merged
        return pv.Cube(x_length=size.x_m, y_length=size.y_m, z_length=size.z_m)

    def _add_entity(self, parent: QTreeWidgetItem, entity: SceneEntity) -> None:
        flags: list[str] = []
        if self.view_state.is_hidden(entity.entity_id):
            flags.append('非表示')
        if self.view_state.is_locked(entity.entity_id):
            flags.append('ロック')
        if entity.kind == 'speaker':
            role = entity.speaker_role
            flags.append(
                '役割 未設定' if is_unassigned_speaker_role(role) else f'役割 {role}'
            )
            flags.append('向き未設定' if entity.aim_xyz is None else '向き設定済み')
        suffix = f" · {' · '.join(flags)}" if flags else ''
        item = QTreeWidgetItem([f'{entity.name}{suffix}'])
        item.setData(0, ROLE, entity.entity_id)
        parent.addChild(item)
        self.items[entity.entity_id] = item

        if self.view_state.is_hidden(entity.entity_id):
            return
        if entity.kind == 'measurement_point':
            mesh = pv.Sphere(radius=0.08, center=(0.0, 0.0, 0.0))
        else:
            mesh = self._physical_mesh(entity)
        mesh.transform(np.asarray(domain_pose_to_render_matrix(entity.position, entity.orientation)), inplace=True)
        actor = self.viewport.add_mesh(mesh, name=f'entity:{entity.entity_id}', pickable=True, render=False)
        self.actors[entity.entity_id] = actor
        self.actor_ids[id(actor)] = entity.entity_id
        self.scene_picker.AddPickList(actor)

        reference = acoustic_reference_position(entity)
        if reference is not None and entity.kind != 'measurement_point':
            self.viewport.add_mesh(
                pv.Sphere(radius=0.05, center=domain_to_render(reference)),
                name=f'acoustic-reference:{entity.entity_id}',
                pickable=False,
                render=False,
            )
        if entity.kind == 'speaker' and entity.aim_xyz is not None:
            start = reference or entity.position
            start_render = domain_to_render(start)
            direction = (entity.aim_xyz.x, -entity.aim_xyz.y, entity.aim_xyz.z)
            end = tuple(start_render[index] + 0.6 * direction[index] for index in range(3))
            self.viewport.add_mesh(
                pv.Line(start_render, end),
                line_width=4,
                name=f'aim:{entity.entity_id}',
                pickable=False,
                render=False,
            )

    def _object_position(self, kind: str, size: Size3 | None = None) -> Position3:
        room = self._current_room()
        if room is None:
            return Position3(x_m=0.0, y_m=0.0, z_m=0.0)
        min_x, min_y, max_x, max_y = room.bounds_m
        width = max_x - min_x
        depth = max_y - min_y
        center_x = min_x + width * 0.5
        if kind in {'speaker', 'screen', 'display'}:
            y_m = min_y + min(depth * 0.18, 0.75)
        else:
            y_m = min_y + min(depth * 0.42, 1.65)
        if kind in {'seat', 'furniture', 'av_equipment'} and size is not None:
            z_m = size.z_m * 0.5
        elif kind in {'screen', 'display'}:
            z_m = min(max(room.height_m * 0.55, 0.5), max(room.height_m - 0.1, 0.1))
        elif kind == 'measurement_point':
            z_m = min(1.1, max(room.height_m - 0.1, 0.1))
        else:
            z_m = min(1.0, max(room.height_m - 0.1, 0.1))
        return Position3(x_m=center_x, y_m=y_m, z_m=z_m)

    def _make_object(self, kind: str) -> SceneEntity:
        token = uuid4().hex[:10]
        if kind == 'speaker':
            size = Size3(x_m=0.24, y_m=0.28, z_m=0.42)
            existing_roles = (
                ()
                if self.working is None
                else (
                    entity.speaker_role
                    for entity in self.working.committed_document.entities
                    if entity.kind == 'speaker'
                )
            )
            return SceneEntity(
                entity_id=f'speaker-{token}',
                kind='speaker',
                name='スピーカー',
                speaker_role=next_unassigned_speaker_role(existing_roles),
                position=self._object_position(kind, size),
                size_m=size,
                acoustic_reference_offset_m=Offset3(y_m=size.y_m * 0.5),
                aim_xyz=None,
            )
        if kind == 'seat':
            size = Size3(x_m=0.70, y_m=0.80, z_m=0.90)
            return SceneEntity(
                entity_id=f'seat-{token}',
                kind='seat',
                name='座席',
                position=self._object_position(kind, size),
                orientation=quaternion_from_euler_deg(yaw_deg=180.0, pitch_deg=0.0, roll_deg=0.0),
                size_m=size,
                acoustic_reference_offset_m=Offset3(z_m=0.65),
            )
        if kind == 'screen':
            size = Size3(x_m=2.60, y_m=0.04, z_m=1.46)
            return SceneEntity(
                entity_id=f'screen-{token}',
                kind='screen',
                name='スクリーン',
                position=self._object_position(kind, size),
                size_m=size,
            )
        if kind == 'display':
            size = Size3(x_m=1.50, y_m=0.06, z_m=0.85)
            return SceneEntity(
                entity_id=f'display-{token}',
                kind='display',
                name='ディスプレイ',
                position=self._object_position(kind, size),
                size_m=size,
            )
        if kind == 'furniture':
            size = Size3(x_m=1.20, y_m=0.55, z_m=0.90)
            return SceneEntity(
                entity_id=f'furniture-{token}',
                kind='furniture',
                name='家具',
                position=self._object_position(kind, size),
                size_m=size,
            )
        if kind == 'av_equipment':
            size = Size3(x_m=0.60, y_m=0.55, z_m=1.30)
            return SceneEntity(
                entity_id=f'av-{token}',
                kind='av_equipment',
                name='AV機器',
                position=self._object_position(kind, size),
                size_m=size,
            )
        if kind == 'measurement_point':
            return SceneEntity(
                entity_id=f'point-{token}',
                kind='measurement_point',
                name='測定点',
                position=self._object_position(kind),
            )
        raise ValueError(f'unsupported object kind: {kind}')

    def _object_edit_available(self) -> bool:
        return (
            self.working is not None
            and self.recovery_candidate is None
            and self._current_room() is not None
            and self.room_mode == 'idle'
            and not self.wall_edit_active
            and not self.working.has_preview
        )

    def add_object(self, kind: str) -> None:
        if not self._object_edit_available():
            self.statusBar().showMessage('オブジェクト追加は部屋を作成し、部屋・壁編集を終了してから使用できます')
            return
        entity = self._make_object(kind)
        if self.working.add_entity(entity):
            self.view_state.set_selection((entity.entity_id,), primary_id=entity.entity_id)
            self.selected_id = entity.entity_id
            self.view_state.transform_mode = 'move'
            self._sync_transform_controls()
            self._sync_recovery()
            self._persist_view_state()
            self._rebuild()
            self.statusBar().showMessage(f'{entity.name}を追加しました · 矢印ハンドルで配置、数値は精密化に使用します')

    def _make_302_entities(self) -> tuple[SceneEntity, ...]:
        room = self._current_room()
        if room is None:
            return ()
        min_x, min_y, max_x, max_y = room.bounds_m
        width = max_x - min_x
        depth = max_y - min_y
        center_x = min_x + width * 0.5
        span = min(max(width * 0.20, 0.45), 1.20)
        front_y = min_y + min(depth * 0.18, 0.75)
        top_y = min_y + min(depth * 0.34, 1.35)
        floor_z = min(1.0, max(room.height_m - 0.1, 0.1))
        top_z = max(0.1, min(room.height_m - 0.15, room.height_m * 0.86))
        bed_size = Size3(x_m=0.24, y_m=0.28, z_m=0.42)
        top_size = Size3(x_m=0.18, y_m=0.18, z_m=0.14)
        definitions = (
            ('FL', 'フロント左', center_x - span, front_y, floor_z, bed_size),
            ('C', 'センター', center_x, front_y, floor_z, Size3(x_m=0.50, y_m=0.28, z_m=0.20)),
            ('FR', 'フロント右', center_x + span, front_y, floor_z, bed_size),
            ('TFL', 'トップフロント左', center_x - span * 0.75, top_y, top_z, top_size),
            ('TFR', 'トップフロント右', center_x + span * 0.75, top_y, top_z, top_size),
        )
        entities: list[SceneEntity] = []
        for role, name, x_m, y_m, z_m, size in definitions:
            entities.append(SceneEntity(
                entity_id=f'speaker-{role.lower()}-{uuid4().hex[:8]}',
                kind='speaker',
                name=name,
                speaker_role=role,
                position=Position3(x_m=x_m, y_m=y_m, z_m=z_m),
                size_m=size,
                acoustic_reference_offset_m=Offset3(y_m=size.y_m * 0.5),
                aim_xyz=None,
            ))
        return tuple(entities)

    def add_302_template(self) -> None:
        if not self._object_edit_available():
            self.statusBar().showMessage('3.0.2追加は部屋を作成し、部屋・壁編集を終了してから使用できます')
            return
        entities = self._make_302_entities()
        if not entities:
            return
        if self.working.add_entities(entities):
            last = entities[-1]
            self.view_state.set_selection(tuple(entity.entity_id for entity in entities), primary_id=last.entity_id)
            self.selected_id = last.entity_id
            self.view_state.transform_mode = 'move'
            self._sync_transform_controls()
            self._sync_recovery()
            self._persist_view_state()
            self._rebuild()
            self.statusBar().showMessage(
                '3.0.2を1操作で追加しました · 5台を選択中 · 役割はテンプレート後も自由に編集できます'
            )

    def duplicate_selected_object(self) -> None:
        if not self._object_edit_available() or self.selected_id is None:
            return
        if self.view_state.is_locked(self.selected_id):
            self.statusBar().showMessage('ロック中のオブジェクトは複製できません')
            return
        source = self.working.committed_document.entity(self.selected_id)
        new_id = f'{source.kind}-{uuid4().hex[:10]}'
        position = Position3(
            x_m=source.position.x_m + 0.25,
            y_m=source.position.y_m + 0.25,
            z_m=source.position.z_m,
        )
        if self.working.duplicate_entity(
            source.entity_id,
            new_entity_id=new_id,
            name=f'{source.name} コピー',
            position=position,
        ):
            self.view_state.set_selection((new_id,), primary_id=new_id)
            self.selected_id = new_id
            self._sync_recovery()
            self._persist_view_state()
            self._rebuild()
            self.statusBar().showMessage('複製しました · 元の寸法・役割・向き・音響基準点を保持しています')

    def _inspect(self, entity_id: str | None, *, use_preview: bool = False) -> None:
        super()._inspect(entity_id, use_preview=use_preview)
        if not hasattr(self, 'size_fields'):
            return
        self._refresh_object_inspector(entity_id=entity_id, use_preview=use_preview)

    def _refresh_object_inspector(self, *, entity_id: str | None = None, use_preview: bool = False) -> None:
        if not hasattr(self, 'size_fields'):
            return
        target_id = self.selected_id if entity_id is None else entity_id
        self.object_kind_label.setText('—')
        for field in self.size_fields.values():
            field.setEnabled(False)
        self.speaker_role_field.setEnabled(False)
        with QSignalBlocker(self.speaker_role_field):
            self.speaker_role_field.setText('')
        with QSignalBlocker(self.reference_enabled):
            self.reference_enabled.setChecked(False)
        self.reference_enabled.setEnabled(False)
        for field in self.reference_fields.values():
            field.setEnabled(False)
            with QSignalBlocker(field):
                field.setValue(0.0)
        if target_id is None or self.working is None:
            return
        document = self.working.document if use_preview else self.working.committed_document
        entity = document.entity(target_id)
        kind_labels = {
            'speaker': 'スピーカー',
            'seat': '座席',
            'screen': 'スクリーン',
            'display': 'ディスプレイ',
            'furniture': '家具',
            'av_equipment': 'AV機器',
            'measurement_point': '測定点',
        }
        self.object_kind_label.setText(kind_labels[entity.kind])
        selection = self.view_state.selection or (target_id,)
        editable = (
            self.recovery_candidate is None
            and len(selection) == 1
            and not self.view_state.is_locked(target_id)
            and not self.wall_edit_active
            and self.room_mode == 'idle'
        )
        if entity.size_m is not None:
            values = (entity.size_m.x_m, entity.size_m.y_m, entity.size_m.z_m)
            blockers = [QSignalBlocker(field) for field in self.size_fields.values()]
            try:
                for field, value in zip(self.size_fields.values(), values, strict=True):
                    field.setValue(value)
                    field.setEnabled(editable)
            finally:
                del blockers
        if entity.kind == 'speaker':
            with QSignalBlocker(self.speaker_role_field):
                role = entity.speaker_role
                self.speaker_role_field.setText(
                    '' if is_unassigned_speaker_role(role) else (role or '')
                )
            self.speaker_role_field.setEnabled(editable)
        if entity.kind != 'measurement_point':
            has_reference = entity.acoustic_reference_offset_m is not None
            with QSignalBlocker(self.reference_enabled):
                self.reference_enabled.setChecked(has_reference)
            self.reference_enabled.setEnabled(editable)
            if has_reference:
                offset = entity.acoustic_reference_offset_m
                assert offset is not None
                blockers = [QSignalBlocker(field) for field in self.reference_fields.values()]
                try:
                    for field, value in zip(
                        self.reference_fields.values(),
                        (offset.x_m, offset.y_m, offset.z_m),
                        strict=True,
                    ):
                        field.setValue(value)
                        field.setEnabled(editable)
                finally:
                    del blockers

        # Base N20 enables rotation only for its original kinds. N40 physical bodies share the same pose contract.
        if entity.kind in PHYSICAL_ENTITY_KINDS and editable:
            for field in self.orientation_fields.values():
                field.setEnabled(True)

    def _numeric_size_edited(self) -> None:
        if self.selected_id is None or self.working is None or not self._object_edit_available():
            return
        if self.view_state.is_locked(self.selected_id):
            return
        entity = self.working.committed_document.entity(self.selected_id)
        if entity.kind not in PHYSICAL_ENTITY_KINDS:
            return
        try:
            size = Size3(
                x_m=self.size_fields['X'].value(),
                y_m=self.size_fields['Y'].value(),
                z_m=self.size_fields['Z'].value(),
            )
            changed = self.working.update_entity(self.selected_id, size_m=size)
        except (ValidationError, ValueError, EditStateError) as exc:
            self._refresh_object_inspector()
            self.statusBar().showMessage(f'寸法を変更できません · {exc}')
            return
        if changed:
            self._sync_recovery()
            self._rebuild()
            self.statusBar().showMessage('物理寸法を変更しました · 音響基準点は本体ローカル位置を保持します')

    def _speaker_role_edited(self) -> None:
        if self.selected_id is None or self.working is None or not self._object_edit_available():
            return
        entity = self.working.committed_document.entity(self.selected_id)
        if entity.kind != 'speaker' or self.view_state.is_locked(self.selected_id):
            return
        role = self.speaker_role_field.text().strip()
        if is_unassigned_speaker_role(role):
            # An emptied field means "unassigned", not a literal placeholder
            # string: store a fresh unique reserved token instead.
            role = next_unassigned_speaker_role(
                item.speaker_role
                for item in self.working.committed_document.entities
                if item.kind == 'speaker' and item.entity_id != self.selected_id
            )
        try:
            changed = self.working.update_entity(self.selected_id, speaker_role=role)
        except (ValidationError, ValueError, EditStateError) as exc:
            self._refresh_object_inspector()
            self.statusBar().showMessage(f'役割を変更できません · {exc}')
            return
        if changed:
            self._sync_recovery()
            self._rebuild()
            label = '未設定' if is_unassigned_speaker_role(role) else role
            self.statusBar().showMessage(f'スピーカー役割を {label} に変更しました')

    def _reference_enabled_toggled(self, checked: bool) -> None:
        if self.selected_id is None or self.working is None or not self._object_edit_available():
            return
        entity = self.working.committed_document.entity(self.selected_id)
        if entity.kind == 'measurement_point' or self.view_state.is_locked(self.selected_id):
            return
        offset = entity.acoustic_reference_offset_m
        if checked and offset is None:
            offset = Offset3()
        elif not checked:
            offset = None
        try:
            changed = self.working.update_entity(self.selected_id, acoustic_reference_offset_m=offset)
        except (ValidationError, ValueError, EditStateError) as exc:
            self._refresh_object_inspector()
            self.statusBar().showMessage(f'音響基準点を変更できません · {exc}')
            return
        if changed:
            self._sync_recovery()
            self._rebuild()
            self.statusBar().showMessage('音響基準点を更新しました · 本体位置とは別のローカル参照です')

    def _numeric_reference_edited(self) -> None:
        if self.selected_id is None or self.working is None or not self._object_edit_available():
            return
        entity = self.working.committed_document.entity(self.selected_id)
        if entity.kind == 'measurement_point' or self.view_state.is_locked(self.selected_id):
            return
        if entity.acoustic_reference_offset_m is None:
            return
        try:
            offset = Offset3(
                x_m=self.reference_fields['X'].value(),
                y_m=self.reference_fields['Y'].value(),
                z_m=self.reference_fields['Z'].value(),
            )
            changed = self.working.update_entity(self.selected_id, acoustic_reference_offset_m=offset)
        except (ValidationError, ValueError, EditStateError) as exc:
            self._refresh_object_inspector()
            self.statusBar().showMessage(f'音響基準点を変更できません · {exc}')
            return
        if changed:
            self._sync_recovery()
            self._rebuild()
            self.statusBar().showMessage('音響基準点オフセットを変更しました')

    def _numeric_orientation_edited(self) -> None:
        if self.selected_id is None or self.working is None or self.working.has_preview:
            return
        selection = self.view_state.selection or (self.selected_id,)
        if self.recovery_candidate is not None or len(selection) != 1 or self.view_state.is_locked(self.selected_id):
            return
        entity = self.working.committed_document.entity(self.selected_id)
        if entity.kind not in PHYSICAL_ENTITY_KINDS:
            return
        orientation = quaternion_from_euler_deg(
            yaw_deg=self.orientation_fields['Yaw'].value(),
            pitch_deg=self.orientation_fields['Pitch'].value(),
            roll_deg=self.orientation_fields['Roll'].value(),
        )
        if self.working.rotate_entity(self.selected_id, orientation):
            self._sync_recovery()
            self._rebuild()
        self._set_dirty_status()

    def _create_gizmo(self, entity_id: str | None) -> None:
        if self.room_mode != 'idle' or self.wall_edit_active:
            return
        if self.view_state.transform_mode == 'move':
            super()._create_gizmo(entity_id)
            return
        if entity_id is None or self.working is None or self.recovery_candidate is not None:
            return
        selection = self.view_state.selection
        if not selection or entity_id not in selection or entity_id not in self.actors:
            return
        if any(self.view_state.is_locked(selected_id) for selected_id in selection):
            self.statusBar().showMessage('選択にロック中のオブジェクトがあります · 回転できません')
            return
        entities = tuple(self.working.committed_document.entity(selected_id) for selected_id in selection)
        if not all(entity.kind in PHYSICAL_ENTITY_KINDS for entity in entities):
            return
        pivot = self._selection_pivot()
        if pivot is None:
            return
        self.gizmo = RotationWidget3D(
            self.viewport,
            self.actors[entity_id],
            interact_callback=self._rotation_interact,
            release_callback=self._rotation_release,
            cancel_callback=self.cancel_preview,
            origin=domain_to_render(pivot),
        )

    def _update_actions(self) -> None:
        super()._update_actions()
        if not hasattr(self, 'object_buttons'):
            return
        can_add = self._object_edit_available()
        for button in self.object_buttons:
            button.setEnabled(can_add)
        if not hasattr(self, 'duplicate_action'):
            return
        selected = self.selected_id is not None
        locked = bool(selected and self.view_state.is_locked(self.selected_id))
        self.duplicate_action.setEnabled(can_add and selected and not locked)
