from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import Any, Literal

import numpy as np
import pyvista as pv
from PySide6.QtCore import QEvent, QItemSelectionModel, QSignalBlocker, QTimer, Qt
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QCursor, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QDockWidget,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QWidget,
)
from pyvistaqt import QtInteractor
from vtkmodules.vtkRenderingCore import vtkCellPicker

from .cad_document import EditorViewState, WorkingDocument
from .cad_gizmo import RotationWidget3D, TranslationWidget3D
from .cad_repository import RecoverySnapshot, SceneRepository
from .cad_scene import (
    F1_DOCUMENT_ID,
    Position3,
    Quaternion4,
    SceneEntity,
    domain_pose_to_render_matrix,
    domain_to_render,
    make_empty_scene,
    quaternion_from_euler_deg,
    quaternion_to_euler_deg,
    render_delta_to_domain,
    rotate_orientation_world,
)
from .cad_snap import SnapCandidate, SnapSelector, generate_snap_candidates, snap_angle_deg, snap_position_axis
from .room_viewport import _entity_mesh as _scene_entity_mesh
from .command_palette import flush_focused_text_editor, focused_text_editor

ROLE = int(Qt.ItemDataRole.UserRole)
AXIS_NAMES: tuple[Literal['x', 'y', 'z'], ...] = ('x', 'y', 'z')
STATIC_SNAP_KINDS = frozenset({'vertex', 'midpoint', 'alignment'})
EDGE_SNAP_KINDS = frozenset({'edge'})


def default_data_dir() -> Path:
    base = os.environ.get('LOCALAPPDATA')
    return Path(base) / 'HomeTheaterDigitalTwin' if base else Path.home() / '.home-theater-digital-twin'


class NativeEditorWindow(QMainWindow):
    """N20b native CAD shell: rigid group transforms, snap, undo, recovery and view state."""

    def __init__(self, repository: SceneRepository, document_id: str = F1_DOCUMENT_ID) -> None:
        super().__init__()
        self.repository = repository
        self.document_id = document_id
        self.working: WorkingDocument | None = None
        self.view_state = EditorViewState()
        self.selected_id: str | None = None
        self.recovery_candidate: RecoverySnapshot | None = None
        self.actors: dict[str, pv.Actor] = {}
        self.actor_ids: dict[int, str] = {}
        self.scene_pick_cache: tuple[tuple[str, float, float, float, float, float], ...] = ()
        self.scene_pick_cache_signature: tuple[int, int, int, int] | None = None
        self.scene_press_consumed = False
        self.items: dict[str, QTreeWidgetItem] = {}
        self.gizmo: TranslationWidget3D | RotationWidget3D | None = None
        self.drag_base_position: Position3 | None = None
        self.drag_base_orientation: Quaternion4 | None = None
        self.drag_rotation_pivot: Position3 | None = None
        self.drag_static_snap_candidates: tuple[SnapCandidate, ...] = ()
        self.drag_snap_axis: Literal['x', 'y', 'z'] | None = None
        self.retained_snap_candidate: SnapCandidate | None = None
        self.snap_feedback_label: str | None = None
        self.snap_selector = SnapSelector()
        self.capture_watch = QTimer(self)
        self.capture_watch.setInterval(40)
        self.capture_watch.timeout.connect(self._check_mouse_capture)
        self.gizmo_rebuild_timer = QTimer(self)
        self.gizmo_rebuild_timer.setSingleShot(True)
        self.gizmo_rebuild_timer.setInterval(0)
        self.gizmo_rebuild_timer.timeout.connect(self._rebuild_gizmo_after_selection)
        self.view_state_save_timer = QTimer(self)
        self.view_state_save_timer.setSingleShot(True)
        self.view_state_save_timer.setInterval(250)
        self.view_state_save_timer.timeout.connect(self._persist_view_state)
        self.preview_inspect_timer = QTimer(self)
        self.preview_inspect_timer.setSingleShot(True)
        self.preview_inspect_timer.setInterval(33)
        self.preview_inspect_timer.timeout.connect(self._refresh_preview_inspector)
        self.resize(1440, 900)
        self.setWindowTitle('Home Theater Digital Twin — N20b エディター')

        self.viewport = QtInteractor(self)
        self.setCentralWidget(self.viewport.interactor)
        self.scene_picker = vtkCellPicker()
        self.scene_picker.PickFromListOn()
        self.scene_picker.SetTolerance(0.005)
        self.scene_pick_observer: int | None = None
        self.viewport.interactor.installEventFilter(self)
        self.viewport.add_key_event('Escape', self.cancel_preview)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.itemSelectionChanged.connect(self._tree_selected)
        left = QDockWidget('シーン', self)
        left.setWidget(self.tree)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, left)

        inspector = QWidget()
        form = QFormLayout(inspector)
        self.kind_label = QLabel('—')
        self.id_label = QLabel('—')
        self.state_label = QLabel('—')
        self.aim_label = QLabel('—')
        form.addRow('種類', self.kind_label)
        form.addRow('ID', self.id_label)
        form.addRow('状態', self.state_label)

        self.position_fields: dict[str, QDoubleSpinBox] = {}
        for axis in ('X', 'Y', 'Z'):
            field = QDoubleSpinBox()
            field.setRange(-1000.0, 1000.0)
            field.setDecimals(4)
            field.setSingleStep(0.01)
            field.setSuffix(' m')
            field.setKeyboardTracking(False)
            field.editingFinished.connect(self._numeric_position_edited)
            self.position_fields[axis] = field
            form.addRow(axis, field)

        axis_labels = {'Yaw': 'ヨー', 'Pitch': 'ピッチ', 'Roll': 'ロール'}
        self.orientation_fields: dict[str, QDoubleSpinBox] = {}
        for axis in ('Yaw', 'Pitch', 'Roll'):
            field = QDoubleSpinBox()
            field.setRange(-180.0, 180.0)
            field.setDecimals(2)
            field.setSingleStep(1.0)
            field.setSuffix('°')
            field.setKeyboardTracking(False)
            field.editingFinished.connect(self._numeric_orientation_edited)
            self.orientation_fields[axis] = field
            form.addRow(axis_labels[axis], field)
        form.addRow('向き', self.aim_label)

        right = QDockWidget('インスペクター', self)
        right.setWidget(inspector)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, right)

        toolbar = QToolBar('編集', self)
        self.addToolBar(toolbar)
        self.save_action = self._action('保存', QKeySequence.StandardKey.Save, self.save)
        self.undo_action = self._action('元に戻す', QKeySequence.StandardKey.Undo, self.undo)
        self.redo_action = self._action('やり直す', QKeySequence.StandardKey.Redo, self.redo)
        self.delete_action = self._action('削除', None, self.delete_selected)
        self.delete_action.setShortcut(QKeySequence('Delete'))
        toolbar.addActions((self.save_action, self.undo_action, self.redo_action, self.delete_action))
        toolbar.addSeparator()

        self.transform_group = QActionGroup(self)
        self.transform_group.setExclusive(True)
        self.move_action = self._action('移動', None, self._activate_move_mode)
        self.move_action.setCheckable(True)
        self.move_action.setShortcut(QKeySequence('W'))
        self.rotate_action = self._action('回転', None, self._activate_rotate_mode)
        self.rotate_action.setCheckable(True)
        self.rotate_action.setShortcut(QKeySequence('R'))
        self.transform_group.addAction(self.move_action)
        self.transform_group.addAction(self.rotate_action)
        self.move_action.setChecked(True)
        toolbar.addActions((self.move_action, self.rotate_action))

        self.object_snap_action = self._action('オブジェクトスナップ', None, self._object_snap_toggled)
        self.object_snap_action.setCheckable(True)
        self.object_snap_action.setChecked(True)
        self.grid_snap_action = self._action('グリッドスナップ', None, self._grid_snap_toggled)
        self.grid_snap_action.setCheckable(True)
        self.angle_snap_action = self._action('角度スナップ', None, self._angle_snap_toggled)
        self.angle_snap_action.setCheckable(True)
        toolbar.addActions((self.object_snap_action, self.grid_snap_action, self.angle_snap_action))

        self.grid_step_field = QDoubleSpinBox()
        self.grid_step_field.setRange(0.001, 10.0)
        self.grid_step_field.setDecimals(3)
        self.grid_step_field.setSingleStep(0.01)
        self.grid_step_field.setValue(self.view_state.grid_step_m)
        self.grid_step_field.setSuffix(' m グリッド')
        self.grid_step_field.valueChanged.connect(self._grid_step_changed)
        toolbar.addWidget(self.grid_step_field)

        self.angle_step_field = QDoubleSpinBox()
        self.angle_step_field.setRange(0.1, 180.0)
        self.angle_step_field.setDecimals(1)
        self.angle_step_field.setSingleStep(5.0)
        self.angle_step_field.setValue(self.view_state.angle_step_deg)
        self.angle_step_field.setSuffix('° 角度')
        self.angle_step_field.valueChanged.connect(self._angle_step_changed)
        toolbar.addWidget(self.angle_step_field)
        toolbar.addSeparator()

        self.hide_action = self._action('非表示', None, self._toggle_hidden)
        self.hide_action.setCheckable(True)
        self.lock_action = self._action('ロック', None, self._toggle_locked)
        self.lock_action.setCheckable(True)
        self.show_all_action = self._action('すべて表示', None, self.show_all)
        toolbar.addActions((self.hide_action, self.lock_action, self.show_all_action))
        toolbar.addSeparator()

        self.recover_action = self._action('下書きを復旧', None, self.recover_draft)
        self.discard_recovery_action = self._action('復旧データを破棄', None, self.discard_recovery)
        toolbar.addActions((self.recover_action, self.discard_recovery_action))
        toolbar.addSeparator()

        for label, callback in (
            ('上面', self._top),
            ('正面', self._front),
            ('右側面', self._right),
            ('透視', self._perspective),
            ('全体表示', self._fit),
        ):
            toolbar.addAction(self._action(label, None, callback))

        self._load_or_seed()
        app = QApplication.instance()
        if app is not None:
            # Save must stay reachable while an inspector field holds a pending
            # edit (Ctrl+S arrives before editingFinished commits the value).
            # A bound method keeps the receiver tracked: the connection is
            # dropped when this window is destroyed. A lambda would fire
            # _update_actions on the deleted C++ object for every later
            # focus change (RuntimeError).
            app.focusChanged.connect(self._on_app_focus_changed)

    def _on_app_focus_changed(self, _old: QWidget | None, _new: QWidget | None) -> None:
        self._update_actions()

    _KIND_LABELS = {
        'speaker': 'スピーカー',
        'seat': '座席',
        'screen': 'スクリーン',
        'display': 'ディスプレイ',
        'projector': 'プロジェクター',
        'riser': 'ライザー',
        'furniture': '家具',
        'av_equipment': 'AV機器',
        'measurement_point': '測定点',
    }

    @classmethod
    def _kind_label(cls, kind: str) -> str:
        return cls._KIND_LABELS.get(kind, kind)

    @staticmethod
    def _operation_label(kind: str) -> str:
        return {'move': '移動', 'rotate': '回転'}.get(kind, '変換')

    def _action(self, label: str, shortcut: QKeySequence.StandardKey | None, callback) -> QAction:
        action = QAction(label, self)
        if shortcut is not None:
            action.setShortcut(QKeySequence(shortcut))
        action.triggered.connect(callback)
        return action

    def _sync_transform_controls(self) -> None:
        with QSignalBlocker(self.move_action):
            self.move_action.setChecked(self.view_state.transform_mode == 'move')
        with QSignalBlocker(self.rotate_action):
            self.rotate_action.setChecked(self.view_state.transform_mode == 'rotate')
        with QSignalBlocker(self.object_snap_action):
            self.object_snap_action.setChecked(self.view_state.object_snap_enabled)
        with QSignalBlocker(self.grid_snap_action):
            self.grid_snap_action.setChecked(self.view_state.grid_snap_enabled)
        with QSignalBlocker(self.angle_snap_action):
            self.angle_snap_action.setChecked(self.view_state.angle_snap_enabled)
        with QSignalBlocker(self.grid_step_field):
            self.grid_step_field.setValue(self.view_state.grid_step_m)
        with QSignalBlocker(self.angle_step_field):
            self.angle_step_field.setValue(self.view_state.angle_step_deg)

    def _load_or_seed(self) -> None:
        revision = self.repository.current_head(self.document_id)
        if revision is None:
            # A previously unseen document opens as an empty scene under its own
            # identity. Even the legacy F1 default must not silently persist the
            # synthetic fixture; demo data requires the explicit seed command
            # (#627).
            revision = self.repository.save(
                make_empty_scene(self.document_id), parent_revision_id=None
            ).revision
        self.working = WorkingDocument(
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
                f'F1 · リビジョン {revision.revision_id[:8]} · 復旧データあり · 下書きを復旧または復旧データを破棄を選択'
            )
        else:
            self.statusBar().showMessage(f'F1 · リビジョン {revision.revision_id[:8]} · 保存済み')

    def _invalidate_scene_pick_cache(self) -> None:
        self.scene_pick_cache = ()
        self.scene_pick_cache_signature = None

    def _scene_pick_state(self) -> tuple[int, int, int, int]:
        renderer = self.viewport.renderer
        width, height = renderer.GetSize()
        return (int(renderer.GetActiveCamera().GetMTime()), int(width), int(height), len(self.actors))

    def _rebuild_scene_pick_cache(self) -> None:
        renderer = self.viewport.renderer
        dpr = max(float(self.viewport.interactor.devicePixelRatioF()), 1.0)
        padding = 3.0 * dpr
        entries: list[tuple[str, float, float, float, float, float]] = []
        for entity_id, actor in self.actors.items():
            bounds = actor.GetBounds()
            if bounds is None or len(bounds) != 6 or not all(np.isfinite(value) for value in bounds):
                continue
            xs: list[float] = []
            ys: list[float] = []
            depths: list[float] = []
            for world_x in (bounds[0], bounds[1]):
                for world_y in (bounds[2], bounds[3]):
                    for world_z in (bounds[4], bounds[5]):
                        renderer.SetWorldPoint(float(world_x), float(world_y), float(world_z), 1.0)
                        renderer.WorldToDisplay()
                        display_x, display_y, display_z = renderer.GetDisplayPoint()
                        if np.isfinite(display_x) and np.isfinite(display_y) and np.isfinite(display_z):
                            xs.append(float(display_x))
                            ys.append(float(display_y))
                            depths.append(float(display_z))
            if not xs:
                continue
            entries.append((
                entity_id,
                min(xs) - padding,
                max(xs) + padding,
                min(ys) - padding,
                max(ys) + padding,
                min(depths),
            ))
        self.scene_pick_cache = tuple(entries)
        self.scene_pick_cache_signature = self._scene_pick_state()

    def _screen_pick_entity(self, x: float, y: float) -> str | None:
        signature = self._scene_pick_state()
        if signature != self.scene_pick_cache_signature:
            self._rebuild_scene_pick_cache()
        hits: list[tuple[float, float, str]] = []
        for entity_id, min_x, max_x, min_y, max_y, depth in self.scene_pick_cache:
            if min_x <= x <= max_x and min_y <= y <= max_y:
                area = max((max_x - min_x) * (max_y - min_y), 0.0)
                hits.append((depth, area, entity_id))
        return None if not hits else min(hits)[2]

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
            self.tree.addTopLevelItem(QTreeWidgetItem(['部屋 · 未作成']))
        else:
            room_item = QTreeWidgetItem(['部屋 · F1 6×4×2.4 m'])
            self.tree.addTopLevelItem(room_item)
            room_actor = self.viewport.add_mesh(
                pv.Box(bounds=(0.0, document.room.width_m, -document.room.depth_m, 0.0, 0.0, document.room.height_m)),
                style='wireframe',
                line_width=2,
                pickable=False,
            )
            room_actor.prop.opacity = 0.45

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
        self._update_actions()

    def _add_entity(self, parent: QTreeWidgetItem, entity: SceneEntity) -> None:
        flags: list[str] = []
        if self.view_state.is_hidden(entity.entity_id):
            flags.append('非表示')
        if self.view_state.is_locked(entity.entity_id):
            flags.append('ロック')
        suffix = f" · {', '.join(flags)}" if flags else ''
        item = QTreeWidgetItem([f'{entity.name}{suffix}'])
        item.setData(0, ROLE, entity.entity_id)
        parent.addChild(item)
        self.items[entity.entity_id] = item

        if self.view_state.is_hidden(entity.entity_id):
            return
        if entity.kind in {'speaker', 'furniture'} and entity.size_m is None:
            raise ValueError(f'{entity.entity_id} requires size_m for rendering')
        # Issue #464: reuse the canonical body-aware mesh builder so this
        # legacy viewport renders cylinders/polygon extrusions/mesh assets
        # instead of always pv.Cube.
        mesh = _scene_entity_mesh(entity)
        actor = self.viewport.add_mesh(mesh, name=f'entity:{entity.entity_id}', pickable=True)
        self.actors[entity.entity_id] = actor
        self.actor_ids[id(actor)] = entity.entity_id
        self.scene_picker.AddPickList(actor)

    def _scene_left_press(self, interactor: Any, _event: str) -> None:
        if self.gizmo is not None and getattr(self.gizmo, 'pressing', False):
            return
        x, y = interactor.GetEventPosition()
        entity_id = self._screen_pick_entity(float(x), float(y))
        if entity_id is None:
            if self.view_state.selection:
                self._select(None)
            return
        actor = self.actors.get(entity_id)
        if actor is not None:
            self._picked(actor)

    def _picked(self, actor: Any) -> None:
        entity_id = self.actor_ids.get(id(actor))
        if entity_id is None:
            entity_id = next(
                (candidate_id for candidate_id, candidate in self.actors.items() if candidate == actor),
                None,
            )
        if entity_id is None:
            return
        additive = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ControlModifier)
        if not additive and entity_id in self.view_state.selection:
            return
        if additive:
            selected = list(self.view_state.selection)
            if entity_id in selected:
                selected.remove(entity_id)
            else:
                selected.append(entity_id)
            primary = entity_id if entity_id in selected else (selected[-1] if selected else None)
            self._set_selection(tuple(selected), primary_id=primary)
        else:
            self._select(entity_id)

    def _tree_selected(self) -> None:
        selected_from_tree = [
            item.data(0, ROLE)
            for item in self.tree.selectedItems()
            if isinstance(item.data(0, ROLE), str)
        ]
        selected_set = set(selected_from_tree)
        ordered = [entity_id for entity_id in self.view_state.selection if entity_id in selected_set]
        ordered.extend(entity_id for entity_id in selected_from_tree if entity_id not in ordered)
        current = self.tree.currentItem()
        current_id = current.data(0, ROLE) if current is not None else None
        primary = current_id if isinstance(current_id, str) and current_id in selected_set else (ordered[-1] if ordered else None)
        self._set_selection(tuple(ordered), primary_id=primary)

    def _select(
        self,
        entity_id: str | None,
        *,
        cancel_preview: bool = True,
        persist: bool = True,
    ) -> None:
        self._set_selection(
            () if entity_id is None else (entity_id,),
            primary_id=entity_id,
            cancel_preview=cancel_preview,
            persist=persist,
        )

    def _set_selection(
        self,
        entity_ids: tuple[str, ...],
        *,
        primary_id: str | None = None,
        cancel_preview: bool = True,
        persist: bool = True,
    ) -> None:
        if cancel_preview and self.working and self.working.has_preview:
            self.cancel_preview()
        self.gizmo_rebuild_timer.stop()
        valid: list[str] = []
        if self.working is not None:
            known = {entity.entity_id for entity in self.working.committed_document.entities}
            valid = [entity_id for entity_id in entity_ids if entity_id in known]
        self.view_state.set_selection(valid, primary_id=primary_id)
        self.selected_id = self.view_state.selected_id
        if persist:
            self._schedule_view_state_persist()
            self._schedule_gizmo_rebuild()
            return
        self._rebuild_gizmo_after_selection()

    def _schedule_gizmo_rebuild(self) -> None:
        self.gizmo_rebuild_timer.start()

    def _rebuild_gizmo_after_selection(self) -> None:
        if self.working is None or self.working.has_preview:
            return
        self._remove_gizmo()
        with QSignalBlocker(self.tree):
            self.tree.clearSelection()
            for entity_id in self.view_state.selection:
                item = self.items.get(entity_id)
                if item is not None:
                    item.setSelected(True)
            primary_item = self.items.get(self.selected_id) if self.selected_id is not None else None
            if primary_item is not None:
                self.tree.setCurrentItem(primary_item, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
        selected_set = set(self.view_state.selection)
        for key, actor in self.actors.items():
            actor.prop.show_edges = key in selected_set
            actor.prop.line_width = 4 if key == self.selected_id else (2 if key in selected_set else 1)
        self._inspect(self.selected_id)
        self._update_actions()
        self._create_gizmo(self.selected_id)
        self.viewport.render()
    def _schedule_view_state_persist(self) -> None:
        self.view_state_save_timer.start()

    def _selection_pivot(self) -> Position3 | None:
        if self.working is None or not self.view_state.selection:
            return None
        entities = tuple(self.working.committed_document.entity(entity_id) for entity_id in self.view_state.selection)
        count = float(len(entities))
        return Position3(
            x_m=sum(entity.position.x_m for entity in entities) / count,
            y_m=sum(entity.position.y_m for entity in entities) / count,
            z_m=sum(entity.position.z_m for entity in entities) / count,
        )

    def _project_domain_to_dip(self, position: Position3) -> tuple[float, float]:
        x, y, z = domain_to_render(position)
        renderer = self.viewport.renderer
        renderer.SetWorldPoint(float(x), float(y), float(z), 1.0)
        renderer.WorldToDisplay()
        display_x, display_y, _ = renderer.GetDisplayPoint()
        dpr = max(float(self.viewport.interactor.devicePixelRatioF()), 1e-9)
        return (float(display_x) / dpr, float(display_y) / dpr)

    def _clear_snap_feedback(self) -> None:
        if self.snap_feedback_label is None:
            return
        self.viewport.remove_actor('snap-feedback', reset_camera=False, render=False)
        self.snap_feedback_label = None

    def _show_snap_feedback(self, label: str) -> None:
        if label == self.snap_feedback_label:
            return
        self.viewport.add_text(
            label,
            position='lower_left',
            font_size=10,
            name='snap-feedback',
            render=False,
        )
        self.snap_feedback_label = label

    def _reset_drag_snap_state(self) -> None:
        self.snap_selector.reset()
        self.drag_static_snap_candidates = ()
        self.drag_snap_axis = None
        self.retained_snap_candidate = None

    def _select_object_snap(
        self,
        selection: tuple[str, ...],
        axis: Literal['x', 'y', 'z'],
        probe: Position3,
    ):
        if self.working is None:
            return None
        if self.drag_snap_axis != axis:
            self._reset_drag_snap_state()
            self.drag_snap_axis = axis
            self.drag_static_snap_candidates = generate_snap_candidates(
                self.working.committed_document,
                exclude_ids=set(selection),
                axis=axis,
                probe=probe,
                kinds=STATIC_SNAP_KINDS,
            )

        retained = self.retained_snap_candidate
        if retained is not None and retained.kind != 'edge':
            selected = self.snap_selector.select((retained,), probe, self._project_domain_to_dip)
            if selected is not None:
                self.retained_snap_candidate = selected.candidate
                return selected
            self.retained_snap_candidate = None

        edges = generate_snap_candidates(
            self.working.committed_document,
            exclude_ids=set(selection),
            axis=axis,
            probe=probe,
            kinds=EDGE_SNAP_KINDS,
        )
        selected = self.snap_selector.select(
            self.drag_static_snap_candidates + edges,
            probe,
            self._project_domain_to_dip,
        )
        self.retained_snap_candidate = None if selected is None else selected.candidate
        return selected

    def _schedule_preview_inspector(self) -> None:
        # The committed Inspector is refreshed on release. Avoid Qt form updates in
        # the mouse-move hot path so dense F4 snapping stays within the frame budget.
        return

    def _refresh_preview_inspector(self) -> None:
        if self.working is not None and self.working.has_preview and self.selected_id is not None:
            self._inspect(self.selected_id, use_preview=True)

    def _create_gizmo(self, entity_id: str | None) -> None:
        if entity_id is None or self.working is None or self.recovery_candidate is not None:
            return
        selection = self.view_state.selection
        if not selection or entity_id not in selection or entity_id not in self.actors:
            return
        if any(self.view_state.is_locked(selected_id) for selected_id in selection):
            self.statusBar().showMessage('選択にロックされたオブジェクトが含まれます · 変換を無効化')
            return
        entities = tuple(self.working.committed_document.entity(selected_id) for selected_id in selection)
        pivot = self._selection_pivot()
        if pivot is None:
            return
        origin = domain_to_render(pivot)
        if self.view_state.transform_mode == 'move':
            self.gizmo = TranslationWidget3D(
                self.viewport,
                self.actors[entity_id],
                interact_callback=self._translation_interact,
                release_callback=self._translation_release,
                cancel_callback=self.cancel_preview,
                origin=origin,
            )
        elif all(entity.kind in {'speaker', 'furniture'} for entity in entities):
            self.gizmo = RotationWidget3D(
                self.viewport,
                self.actors[entity_id],
                interact_callback=self._rotation_interact,
                release_callback=self._rotation_release,
                cancel_callback=self.cancel_preview,
                origin=origin,
            )

    def _inspect(self, entity_id: str | None, *, use_preview: bool = False) -> None:
        self.kind_label.setText('—')
        self.id_label.setText('—')
        self.state_label.setText('—')
        self.aim_label.setText('—')
        for field in (*self.position_fields.values(), *self.orientation_fields.values()):
            field.setEnabled(False)
        if entity_id is None or self.working is None:
            return
        document = self.working.document if use_preview else self.working.committed_document
        entity = document.entity(entity_id)
        self.kind_label.setText(self._kind_label(entity.kind))
        self.id_label.setText(entity.entity_id)
        states = ['非表示' if self.view_state.is_hidden(entity_id) else '表示']
        if self.view_state.is_locked(entity_id):
            states.append('ロック')
        if len(self.view_state.selection) > 1:
            states.append(f'{len(self.view_state.selection)} 件選択')
        if self.recovery_candidate is not None:
            states.append('復旧保留')
        self.state_label.setText(' · '.join(states))
        self.aim_label.setText(
            '未設定'
            if entity.kind == 'speaker' and entity.aim_xyz is None
            else ('設定済み' if entity.kind == 'speaker' else '—')
        )
        selection = self.view_state.selection or (entity_id,)
        editable = self.recovery_candidate is None and not any(self.view_state.is_locked(selected_id) for selected_id in selection)
        position_values = (entity.position.x_m, entity.position.y_m, entity.position.z_m)
        position_blockers = [QSignalBlocker(field) for field in self.position_fields.values()]
        try:
            for field, value in zip(self.position_fields.values(), position_values, strict=True):
                field.setValue(value)
                field.setEnabled(editable)
        finally:
            del position_blockers

        yaw, pitch, roll = quaternion_to_euler_deg(entity.orientation)
        orientation_blockers = [QSignalBlocker(field) for field in self.orientation_fields.values()]
        try:
            for field, value in zip(self.orientation_fields.values(), (yaw, pitch, roll), strict=True):
                field.setValue(value)
                field.setEnabled(editable and len(selection) == 1 and entity.kind in {'speaker', 'furniture'})
        finally:
            del orientation_blockers

    def _numeric_position_edited(self) -> None:
        if self.selected_id is None or self.working is None or self.working.has_preview:
            return
        selection = self.view_state.selection or (self.selected_id,)
        if self.recovery_candidate is not None or any(self.view_state.is_locked(entity_id) for entity_id in selection):
            return
        position = Position3(
            x_m=self.position_fields['X'].value(),
            y_m=self.position_fields['Y'].value(),
            z_m=self.position_fields['Z'].value(),
        )
        selection = self.view_state.selection or (self.selected_id,)
        base = self.working.committed_document.entity(self.selected_id).position
        delta = (position.x_m - base.x_m, position.y_m - base.y_m, position.z_m - base.z_m)
        if len(selection) == 1:
            changed = self.working.move_entity(self.selected_id, position)
        else:
            self.working.begin_group_move(selection)
            self.working.preview_group_move(delta)
            changed = self.working.commit_preview()
        if changed:
            self._sync_recovery()
            self._rebuild()
        self._set_dirty_status()

    def _numeric_orientation_edited(self) -> None:
        if self.selected_id is None or self.working is None or self.working.has_preview:
            return
        selection = self.view_state.selection or (self.selected_id,)
        if self.recovery_candidate is not None or len(selection) != 1 or self.view_state.is_locked(self.selected_id):
            return
        entity = self.working.committed_document.entity(self.selected_id)
        if entity.kind not in {'speaker', 'furniture'}:
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

    def _translation_interact(self, matrix: np.ndarray) -> None:
        if self.selected_id is None or self.working is None or self.recovery_candidate is not None:
            return
        selection = self.view_state.selection or (self.selected_id,)
        if any(self.view_state.is_locked(entity_id) for entity_id in selection):
            return
        if not isinstance(self.gizmo, TranslationWidget3D):
            return
        if not self.working.has_preview:
            self.drag_base_position = self._selection_pivot()
            self.working.begin_group_move(selection)
            self._reset_drag_snap_state()
            self.capture_watch.start()
        if self.drag_base_position is None:
            return
        candidate = render_delta_to_domain(
            tuple(float(value) for value in matrix[:3, 3]),
            self.drag_base_position,
        )
        axis_index = self.gizmo.active_axis_index
        bypass_snap = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.AltModifier)
        snapped = False
        if axis_index is not None and not bypass_snap:
            axis = AXIS_NAMES[axis_index]
            if self.view_state.object_snap_enabled:
                selected = self._select_object_snap(selection, axis, candidate)
                if selected is not None:
                    candidate = selected.candidate.target
                    self._show_snap_feedback(selected.candidate.label)
                    snapped = True
            if not snapped and self.view_state.grid_snap_enabled:
                candidate = snap_position_axis(candidate, axis, self.view_state.grid_step_m)
                value = {'x': candidate.x_m, 'y': candidate.y_m, 'z': candidate.z_m}[axis]
                self._show_snap_feedback(f'グリッド · {axis.upper()}={value:.3f} m')
                snapped = True
        if bypass_snap or axis_index is None or not snapped:
            if bypass_snap:
                self._reset_drag_snap_state()
            self._clear_snap_feedback()
        delta = (
            candidate.x_m - self.drag_base_position.x_m,
            candidate.y_m - self.drag_base_position.y_m,
            candidate.z_m - self.drag_base_position.z_m,
        )
        display_delta = (delta[0], -delta[1], delta[2])
        self.gizmo.set_display_delta(display_delta)
        for entity_id in selection:
            actor = self.actors.get(entity_id)
            if actor is not None and actor is not self.gizmo.actor:
                actor.user_matrix = self.gizmo.matrix.copy()
        self.working.preview_group_move(delta)
        self._schedule_preview_inspector()

    def _commit_active_preview(self, expected_kind: Literal['move', 'rotate'] | None = None) -> bool:
        self.capture_watch.stop()
        self.preview_inspect_timer.stop()
        if self.working is None or not self.working.has_preview:
            return False
        kind = self.working.preview_kind
        if kind is None or (expected_kind is not None and kind != expected_kind):
            return False
        self.working.commit_preview()
        self.drag_base_position = None
        self.drag_base_orientation = None
        self.drag_rotation_pivot = None
        self._reset_drag_snap_state()
        self._clear_snap_feedback()
        self._sync_recovery()
        self._rebuild()
        self._set_dirty_status()
        return True

    def _translation_release(self, matrix: np.ndarray) -> None:
        del matrix
        self._commit_active_preview('move')

    def _rotation_interact(self, axis_index: int, angle_deg: float) -> None:
        if self.selected_id is None or self.working is None or self.recovery_candidate is not None:
            return
        selection = self.view_state.selection or (self.selected_id,)
        if any(self.view_state.is_locked(entity_id) for entity_id in selection):
            return
        if not isinstance(self.gizmo, RotationWidget3D):
            return
        if not self.working.has_preview:
            self.drag_rotation_pivot = self._selection_pivot()
            if self.drag_rotation_pivot is None:
                return
            self.working.begin_group_rotate(selection)
            self._reset_drag_snap_state()
            self._clear_snap_feedback()
            self.capture_watch.start()
        if self.drag_rotation_pivot is None:
            return
        effective_angle = float(angle_deg)
        if self.view_state.angle_snap_enabled:
            effective_angle = snap_angle_deg(effective_angle, self.view_state.angle_step_deg)
        self.gizmo.set_display_angle(effective_angle)
        for entity_id in selection:
            actor = self.actors.get(entity_id)
            if actor is not None and actor is not self.gizmo.actor:
                actor.user_matrix = self.gizmo.matrix.copy()
        self.working.preview_group_rotate(
            AXIS_NAMES[axis_index],
            effective_angle,
            self.drag_rotation_pivot,
        )
        self._schedule_preview_inspector()

    def _rotation_release(self, axis_index: int, angle_deg: float) -> None:
        del axis_index, angle_deg
        self._commit_active_preview('rotate')

    def cancel_preview(self) -> None:
        self.capture_watch.stop()
        self.preview_inspect_timer.stop()
        if self.working is None:
            return
        had_preview = self.working.has_preview
        kind = self.working.preview_kind or 'transform'
        if had_preview:
            self.working.cancel_preview()
        self.drag_base_position = None
        self.drag_base_orientation = None
        self.drag_rotation_pivot = None
        self._reset_drag_snap_state()
        self._clear_snap_feedback()
        if self.gizmo:
            self.gizmo.cancel()
        if had_preview:
            self._rebuild()
            self.statusBar().showMessage(f'{self._operation_label(kind)}をキャンセル · 履歴は変更なし')
        else:
            self._update_actions()

    def _activate_move_mode(self, checked: bool = False) -> None:
        del checked
        self._set_transform_mode('move')

    def _activate_rotate_mode(self, checked: bool = False) -> None:
        del checked
        self._set_transform_mode('rotate')

    def _set_transform_mode(self, mode: Literal['move', 'rotate']) -> None:
        if self.working and self.working.has_preview:
            self.cancel_preview()
        if self.view_state.transform_mode == mode:
            return
        self.view_state.transform_mode = mode
        self._sync_transform_controls()
        self._reset_drag_snap_state()
        self.gizmo_rebuild_timer.stop()
        self._remove_gizmo()
        self._create_gizmo(self.selected_id)
        self.statusBar().showMessage(f'{self._operation_label(mode)}ツール · ワールド軸')
        self._update_actions()
        self.viewport.render()

    def _object_snap_toggled(self, checked: bool) -> None:
        if self.working and self.working.has_preview:
            self.cancel_preview()
        self.view_state.object_snap_enabled = bool(checked)
        self._reset_drag_snap_state()
        self._clear_snap_feedback()
        self.statusBar().showMessage(f"オブジェクトスナップ {'オン' if checked else 'オフ'} · 8/12 DIP")

    def _grid_snap_toggled(self, checked: bool) -> None:
        if self.working and self.working.has_preview:
            self.cancel_preview()
        self.view_state.grid_snap_enabled = bool(checked)
        self.statusBar().showMessage(
            f"グリッドスナップ {'オン' if checked else 'オフ'} · {self.view_state.grid_step_m:g} m"
        )

    def _angle_snap_toggled(self, checked: bool) -> None:
        if self.working and self.working.has_preview:
            self.cancel_preview()
        self.view_state.angle_snap_enabled = bool(checked)
        self.statusBar().showMessage(
            f"角度スナップ {'オン' if checked else 'オフ'} · {self.view_state.angle_step_deg:g}°"
        )

    def _grid_step_changed(self, value: float) -> None:
        if self.working and self.working.has_preview:
            self.cancel_preview()
        self.view_state.grid_step_m = float(value)

    def _angle_step_changed(self, value: float) -> None:
        if self.working and self.working.has_preview:
            self.cancel_preview()
        self.view_state.angle_step_deg = float(value)

    def undo(self) -> None:
        if self.working is None or self.recovery_candidate is not None:
            return
        if self.working.has_preview:
            self.cancel_preview()
            return
        if self.working.undo():
            self._sync_recovery()
            self._rebuild()
            self._set_dirty_status()

    def redo(self) -> None:
        if self.working is None or self.recovery_candidate is not None:
            return
        if self.working.redo():
            self._sync_recovery()
            self._rebuild()
            self._set_dirty_status()

    def delete_selected(self) -> None:
        if self.selected_id is None or self.working is None or self.recovery_candidate is not None:
            return
        if self.view_state.is_locked(self.selected_id):
            self.statusBar().showMessage('ロックされたオブジェクトは削除できません')
            return
        entity_id = self.selected_id
        if self.working.delete_entity(entity_id):
            remaining = [selected for selected in self.view_state.selection if selected != entity_id]
            self.view_state.set_selection(remaining)
            self.selected_id = self.view_state.selected_id
            self._sync_recovery()
            self._persist_view_state()
            self._rebuild()
            self._set_dirty_status()

    def _toggle_hidden(self, checked: bool) -> None:
        if self.selected_id is None:
            return
        if self.working and self.working.has_preview:
            self.cancel_preview()
        self.view_state.set_hidden(self.selected_id, checked)
        self._persist_view_state()
        self._rebuild()

    def _toggle_locked(self, checked: bool) -> None:
        if self.selected_id is None:
            return
        if self.working and self.working.has_preview:
            self.cancel_preview()
        self.view_state.set_locked(self.selected_id, checked)
        self._persist_view_state()
        self._rebuild()

    def show_all(self) -> None:
        if not self.view_state.hidden_ids:
            return
        if self.working and self.working.has_preview:
            self.cancel_preview()
        self.view_state.hidden_ids.clear()
        self._persist_view_state()
        self._rebuild()

    def has_focused_text_editor(self) -> bool:
        """True while an inspector/room text or numeric field inside this window owns focus."""

        return focused_text_editor(self) is not None

    def commit_pending_editor(self) -> None:
        """Commit the focused inspector/room field so Save stores its visible value.

        Ctrl+S arrives while the field still owns focus — before editingFinished
        delivers the pending value to the WorkingDocument. A synchronous focus
        transfer runs the field's commit handler first; invalid input reverts via
        the field's existing commit handler instead of silently saving the stale
        document value.
        """

        flush_focused_text_editor(self)

    def save(self) -> None:
        if self.working is None or self.recovery_candidate is not None:
            return
        if self.working.has_preview:
            self.statusBar().showMessage('保存前に実行中の変換を完了またはキャンセルしてください')
            return
        self.commit_pending_editor()
        try:
            result = self.repository.save(
                self.working.committed_document,
                parent_revision_id=self.working.source_revision_id,
            )
            self.working.mark_saved(result.revision.revision_id, result.revision.content_hash)
        except Exception as exc:
            self.statusBar().showMessage(f'保存に失敗しました · 下書きは保持 · {exc}')
            QMessageBox.critical(self, '保存に失敗しました', f'{exc}\n\n下書きと復旧スナップショットは保持されました。')
            return
        verb = '保存しました' if result.created else '変更なし'
        self.statusBar().showMessage(f'リビジョン {result.revision.revision_id[:8]} · {verb} · 保存済み')
        self._update_actions()

    def recover_draft(self) -> None:
        if self.recovery_candidate is None:
            return
        source_id = self.recovery_candidate.source_revision_id
        source = self.repository.get(source_id) if source_id is not None else None
        if source is None:
            self.statusBar().showMessage('復旧元のリビジョンが見つからないため復旧データを開けません')
            return
        self.working = WorkingDocument(
            self.recovery_candidate.document,
            source_revision_id=source.revision_id,
            saved_content_hash=source.content_hash,
        )
        self.view_state.sanitize(self.working.committed_document)
        self.selected_id = self.view_state.selected_id
        self.recovery_candidate = None
        self._rebuild(reset_camera=True)
        self.statusBar().showMessage(f'リビジョン {source.revision_id[:8]} から下書きを復旧 · 未保存')

    def discard_recovery(self) -> None:
        if self.recovery_candidate is None:
            return
        self.repository.clear_recovery(self.document_id)
        self.recovery_candidate = None
        self._rebuild()
        self.statusBar().showMessage('復旧データを破棄しました · 正式リビジョンは変更なし')

    def _sync_recovery(self) -> None:
        if self.working is None or self.working.has_preview or self.recovery_candidate is not None:
            return
        try:
            if self.working.is_dirty:
                self.repository.save_recovery(
                    self.working.committed_document,
                    source_revision_id=self.working.source_revision_id,
                )
            else:
                self.repository.clear_recovery(self.document_id)
        except Exception as exc:
            self.statusBar().showMessage(f'復旧スナップショットに失敗しました · {exc}')

    def _persist_view_state(self) -> None:
        self.view_state_save_timer.stop()
        try:
            self.repository.save_view_state(
                self.document_id,
                selected_id=self.view_state.selected_id,
                selected_ids=self.view_state.selection,
                hidden_ids=self.view_state.hidden_ids,
                locked_ids=self.view_state.locked_ids,
            )
        except Exception as exc:
            self.statusBar().showMessage(f'表示状態の保存に失敗しました · {exc}')

    def _set_dirty_status(self) -> None:
        if self.working is None:
            return
        self.statusBar().showMessage('未保存' if self.working.is_dirty else '保存済み')
        self._update_actions()

    def _update_actions(self) -> None:
        if self.working is None:
            return
        recovery_block = self.recovery_candidate is not None
        selected = self.selected_id is not None
        locked = bool(selected and self.view_state.is_locked(self.selected_id))
        self.save_action.setEnabled(
            not recovery_block
            and not self.working.has_preview
            and (self.working.is_dirty or self.has_focused_text_editor())
        )
        self.undo_action.setEnabled(not recovery_block and (self.working.can_undo or self.working.has_preview))
        self.redo_action.setEnabled(not recovery_block and self.working.can_redo and not self.working.has_preview)
        self.delete_action.setEnabled(not recovery_block and selected and not locked and not self.working.has_preview)
        self.show_all_action.setEnabled(bool(self.view_state.hidden_ids))
        self.recover_action.setEnabled(recovery_block and not self.working.is_dirty)
        self.discard_recovery_action.setEnabled(recovery_block and not self.working.is_dirty)
        with QSignalBlocker(self.hide_action):
            self.hide_action.setEnabled(selected)
            self.hide_action.setChecked(bool(selected and self.view_state.is_hidden(self.selected_id)))
        with QSignalBlocker(self.lock_action):
            self.lock_action.setEnabled(selected)
            self.lock_action.setChecked(locked)

    def _remove_gizmo(self) -> None:
        if self.gizmo:
            self.gizmo.remove()
            self.gizmo = None

    def _cancel_before_view_change(self) -> None:
        if self.working and self.working.has_preview:
            self.cancel_preview()

    def _top(self) -> None:
        self._cancel_before_view_change()
        self.viewport.view_xy(negative=True)
        self.viewport.enable_parallel_projection()
        self.viewport.reset_camera()
        self.viewport.render()

    def _front(self) -> None:
        self._cancel_before_view_change()
        self.viewport.view_xz(negative=False)
        self.viewport.enable_parallel_projection()
        self.viewport.reset_camera()
        self.viewport.render()

    def _right(self) -> None:
        self._cancel_before_view_change()
        self.viewport.view_yz(negative=True)
        self.viewport.enable_parallel_projection()
        self.viewport.reset_camera()
        self.viewport.render()

    def _perspective(self) -> None:
        self._cancel_before_view_change()
        self.viewport.disable_parallel_projection()
        self.viewport.view_isometric()
        self.viewport.reset_camera()
        self.viewport.render()

    def _fit(self) -> None:
        self._cancel_before_view_change()
        self.viewport.reset_camera()
        self.viewport.render()

    def _check_mouse_capture(self) -> None:
        if self.working is None or not self.working.has_preview:
            self.capture_watch.stop()
            return
        if not (QApplication.mouseButtons() & Qt.MouseButton.LeftButton):
            local = self.viewport.interactor.mapFromGlobal(QCursor.pos())
            if self.viewport.interactor.rect().contains(local):
                kind = self.working.preview_kind
                if self._commit_active_preview(kind):
                    self.statusBar().showMessage(f'{self._operation_label(kind)}を確定しました（マウス解放を復旧）')
            else:
                self.cancel_preview()
            return
        if QWidget.mouseGrabber() is not self.viewport.interactor:
            self.cancel_preview()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if watched is self.viewport.interactor:
            event_type = event.type()
            if event_type == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                dpr = max(float(self.viewport.interactor.devicePixelRatioF()), 1.0)
                _, render_height = self.viewport.render_window.GetSize()
                position = event.position()
                x = float(position.x()) * dpr
                y = float(render_height) - float(position.y()) * dpr
                renderer = self.viewport.renderer
                if self.gizmo is not None and self.gizmo.hit_test_display(x, y, renderer):
                    self.scene_press_consumed = False
                    return False
                entity_id = self._screen_pick_entity(x, y)
                if entity_id is None:
                    self.scene_press_consumed = False
                    if self.view_state.selection:
                        self._select(None)
                        self._remove_gizmo()
                    return False
                actor = self.actors.get(entity_id)
                if actor is not None:
                    self._picked(actor)
                    self.scene_press_consumed = True
                    return True
            elif (
                event_type == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.LeftButton
                and self.scene_press_consumed
            ):
                self.scene_press_consumed = False
                return True
        return super().eventFilter(watched, event)

    def event(self, event) -> bool:
        if (
            event.type() == QEvent.Type.WindowDeactivate
            and self.working is not None
            and self.working.has_preview
        ):
            self.cancel_preview()
        return super().event(event)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self.capture_watch.stop()
        self.gizmo_rebuild_timer.stop()
        self.view_state_save_timer.stop()
        self.preview_inspect_timer.stop()
        if self.working and self.working.has_preview:
            self.working.cancel_preview()
        self._sync_recovery()
        self._persist_view_state()
        self._remove_gizmo()
        if self.scene_pick_observer is not None:
            self.viewport.iren.interactor.RemoveObserver(self.scene_pick_observer)
            self.scene_pick_observer = None
        self.viewport.close()
        event.accept()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='HTDT ネイティブCADエディター N20b シェルを起動')
    parser.add_argument('--data-dir', type=Path, default=default_data_dir())
    parser.add_argument('--document-id', default=F1_DOCUMENT_ID)
    args = parser.parse_args(argv)
    app = QApplication([sys.argv[0]])
    repository = SceneRepository(args.data_dir / 'cad-scenes.sqlite3')
    window = NativeEditorWindow(repository, args.document_id)
    window.show()
    return int(app.exec())


if __name__ == '__main__':
    raise SystemExit(main())
