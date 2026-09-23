from __future__ import annotations

from math import hypot
from uuid import uuid4

import numpy as np
import pyvista as pv
from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import (
    QDockWidget,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from shapely.geometry import Point, Polygon, box

from .cad_constraint_models import (
    CadAllowedRegionConstraint,
    CadConstraintEvaluation,
    CadConstraintPoint2D,
    CadConstraintResult,
    CadConstraintSet,
    CadExclusionRegionConstraint,
    CadPairDistanceConstraint,
    CadWallClearanceConstraint,
)
from .cad_constraint_policy import blocking_candidate_violations
from .cad_constraint_repository import CadConstraintRepository
from .cad_constraints import CadConstraintAdapterError, evaluate_cad_constraints
from .cad_repository import SceneRepository
from .cad_scene import F1_DOCUMENT_ID, Position3, room_vertices
from .native_editor import ROLE
from .theater_workflow import TheaterWorkflowWindow


class ConstraintEditorWindow(TheaterWorkflowWindow):
    """N50 product layer: visible placement constraints and hard move rejection."""

    def __init__(self, repository: SceneRepository, document_id: str = F1_DOCUMENT_ID) -> None:
        # Base constructors call virtual rebuild/inspect methods.
        self.constraint_repository = CadConstraintRepository(repository.path)
        self.constraint_set = self.constraint_repository.load(document_id)
        self.constraint_evaluation = CadConstraintEvaluation(constraints_satisfied=True, results=())
        self.constraint_last_rejected: CadConstraintEvaluation | None = None
        self.constraint_last_rejected_positions: dict[str, Position3] = {}
        self.constraint_selected_result_id: str | None = None
        self.constraint_last_subject_id: str | None = None
        self.constraint_tree: QTreeWidget | None = None
        self.constraint_summary_label: QLabel | None = None
        self.constraint_detail_label: QLabel | None = None
        self.constraint_distance_field: QDoubleSpinBox | None = None
        self._constraint_actor_names: set[str] = set()
        super().__init__(repository, document_id)
        self.setWindowTitle('Home Theater Digital Twin — 制約CAD')
        self._create_constraint_dock()
        self._refresh_constraint_state()

    def _create_constraint_dock(self) -> None:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        self.constraint_summary_label = QLabel('制約なし')
        self.constraint_summary_label.setWordWrap(True)
        layout.addWidget(self.constraint_summary_label)

        self.constraint_tree = QTreeWidget()
        self.constraint_tree.setHeaderHidden(True)
        self.constraint_tree.setMinimumHeight(180)
        self.constraint_tree.itemSelectionChanged.connect(self._constraint_tree_selected)
        layout.addWidget(self.constraint_tree)

        form = QFormLayout()
        self.constraint_distance_field = QDoubleSpinBox()
        self.constraint_distance_field.setRange(0.0, 20.0)
        self.constraint_distance_field.setDecimals(3)
        self.constraint_distance_field.setSingleStep(0.05)
        self.constraint_distance_field.setValue(0.50)
        self.constraint_distance_field.setSuffix(' m')
        form.addRow('必要離隔', self.constraint_distance_field)
        layout.addLayout(form)

        for label, tooltip, callback in (
            ('通路を追加', '選択物体の現在位置を中心に通路（除外領域）を追加します', self.add_walkway_for_selected),
            ('許可領域を追加', '選択物体の現在位置を中心に許可領域を追加します', self.add_allowed_region_for_selected),
            ('壁離隔を追加', '選択物体と、選択中または最寄りの壁の離隔制約を追加します', self.add_wall_clearance_for_selected),
            ('物体間離隔を追加', '選択した2物体の水平離隔制約を追加します', self.add_pair_clearance_for_selection),
            ('選択制約を削除', '制約一覧で選択中の定義を削除します', self.delete_selected_constraint),
        ):
            button = QPushButton(label)
            button.setToolTip(tooltip)
            button.clicked.connect(callback)
            layout.addWidget(button)

        self.constraint_detail_label = QLabel('違反理由を選択すると、対象と actual / required を表示します')
        self.constraint_detail_label.setWordWrap(True)
        layout.addWidget(self.constraint_detail_label)
        layout.addStretch(1)

        dock = QDockWidget('制約', self)
        dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        object_detail = next(
            (candidate for candidate in self.findChildren(QDockWidget) if candidate.windowTitle() == 'オブジェクト詳細'),
            None,
        )
        if object_detail is not None:
            self.tabifyDockWidget(object_detail, dock)
        dock.raise_()

    def _inspect(self, entity_id: str | None, *, use_preview: bool = False) -> None:
        super()._inspect(entity_id, use_preview=use_preview)
        if entity_id is not None and self.working is not None:
            if any(entity.entity_id == entity_id for entity in self.working.committed_document.entities):
                self.constraint_last_subject_id = entity_id

    def _rebuild(self, *, reset_camera: bool = False) -> None:
        super()._rebuild(reset_camera=reset_camera)
        if self.constraint_tree is not None:
            self._refresh_constraint_state()

    def _clear_rejected_constraint_preview(self) -> None:
        self.constraint_last_rejected = None
        self.constraint_last_rejected_positions = {}

    def _active_constraint_evaluation(self) -> CadConstraintEvaluation:
        return self.constraint_last_rejected or self.constraint_evaluation

    def _constraint_subject_id(self) -> str | None:
        if self.working is None:
            return None
        known = {entity.entity_id for entity in self.working.committed_document.entities}
        if self.selected_id in known:
            return self.selected_id
        if self.constraint_last_subject_id in known:
            return self.constraint_last_subject_id
        return None

    def _replace_constraint_set(self, constraints: tuple, *, message: str) -> bool:
        candidate = CadConstraintSet(document_id=self.document_id, constraints=constraints)
        if self.working is None:
            return False
        try:
            evaluate_cad_constraints(self.working.committed_document, candidate)
        except (CadConstraintAdapterError, ValueError) as exc:
            self.statusBar().showMessage(f'制約を追加できません · {exc}')
            return False
        self.constraint_set = candidate
        self.constraint_repository.save(candidate)
        self._clear_rejected_constraint_preview()
        self.constraint_selected_result_id = None
        self._refresh_constraint_state()
        self.statusBar().showMessage(message)
        return True

    def _region_vertices_around_subject(
        self,
        entity_id: str,
        *,
        half_width_m: float,
        half_depth_m: float,
    ) -> tuple[CadConstraintPoint2D, ...]:
        if self.working is None or self.working.committed_document.room is None:
            raise ValueError('部屋が必要です')
        document = self.working.committed_document
        entity = document.entity(entity_id)
        room_points = [(vertex.x_m, vertex.y_m) for vertex in room_vertices(document.room)]
        room_polygon = Polygon(room_points)
        proposed = box(
            entity.position.x_m - half_width_m,
            entity.position.y_m - half_depth_m,
            entity.position.x_m + half_width_m,
            entity.position.y_m + half_depth_m,
        )
        clipped = room_polygon.intersection(proposed)
        if clipped.is_empty:
            raise ValueError('選択位置の周囲に領域を作れません')
        if clipped.geom_type == 'MultiPolygon':
            point = Point(entity.position.x_m, entity.position.y_m)
            polygons = list(clipped.geoms)
            containing = [polygon for polygon in polygons if polygon.covers(point)]
            clipped = max(containing or polygons, key=lambda polygon: polygon.area)
        if clipped.geom_type != 'Polygon' or clipped.area <= 1e-9:
            raise ValueError('有効な領域ポリゴンを作れません')
        coordinates = list(clipped.exterior.coords)[:-1]
        if len(coordinates) < 3:
            raise ValueError('領域には3点以上必要です')
        return tuple(CadConstraintPoint2D(x_m=float(x), y_m=float(y)) for x, y in coordinates)

    def add_walkway_for_selected(self) -> None:
        subject_id = self._constraint_subject_id()
        if subject_id is None:
            self.statusBar().showMessage('通路を追加する物体を先に選択してください')
            return
        try:
            vertices = self._region_vertices_around_subject(
                subject_id,
                half_width_m=0.45,
                half_depth_m=0.90,
            )
        except ValueError as exc:
            self.statusBar().showMessage(str(exc))
            return
        constraint = CadExclusionRegionConstraint(
            constraint_id=f'walkway-{uuid4().hex[:10]}',
            name='通路',
            entity_ids=(subject_id,),
            vertices=vertices,
            region_role='walkway',
        )
        self._replace_constraint_set(
            self.constraint_set.constraints + (constraint,),
            message='通路を追加しました · 現在位置との干渉を制約一覧で確認できます',
        )

    def add_allowed_region_for_selected(self) -> None:
        subject_id = self._constraint_subject_id()
        if subject_id is None:
            self.statusBar().showMessage('許可領域を追加する物体を先に選択してください')
            return
        try:
            vertices = self._region_vertices_around_subject(
                subject_id,
                half_width_m=1.00,
                half_depth_m=1.00,
            )
        except ValueError as exc:
            self.statusBar().showMessage(str(exc))
            return
        constraint = CadAllowedRegionConstraint(
            constraint_id=f'allowed-{uuid4().hex[:10]}',
            name='許可領域',
            entity_ids=(subject_id,),
            vertices=vertices,
        )
        self._replace_constraint_set(
            self.constraint_set.constraints + (constraint,),
            message='許可領域を追加しました',
        )

    def _wall_points(self, wall_id: str) -> tuple[tuple[float, float], tuple[float, float]]:
        if self.working is None:
            raise ValueError('scene is not open')
        document = self.working.committed_document
        if document.room is None or document.wall_topology is None:
            raise ValueError('壁トポロジーが必要です')
        wall = next((item for item in document.wall_topology.walls if item.wall_id == wall_id), None)
        if wall is None:
            raise ValueError(f'wall not found: {wall_id}')
        vertices = {vertex.vertex_id: vertex for vertex in room_vertices(document.room)}
        start = vertices[wall.from_vertex_id]
        end = vertices[wall.to_vertex_id]
        return ((float(start.x_m), float(start.y_m)), (float(end.x_m), float(end.y_m)))

    @staticmethod
    def _distance_to_segment(
        point: tuple[float, float],
        start: tuple[float, float],
        end: tuple[float, float],
    ) -> tuple[float, tuple[float, float]]:
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        length_sq = dx * dx + dy * dy
        if length_sq <= 1e-18:
            return (hypot(point[0] - start[0], point[1] - start[1]), start)
        ratio = ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length_sq
        ratio = max(0.0, min(1.0, ratio))
        closest = (start[0] + ratio * dx, start[1] + ratio * dy)
        return (hypot(point[0] - closest[0], point[1] - closest[1]), closest)

    def _wall_for_subject(self, entity_id: str) -> str | None:
        if self.working is None:
            return None
        document = self.working.committed_document
        topology = document.wall_topology
        if topology is None:
            return None
        known = {wall.wall_id for wall in topology.walls}
        if self.selected_wall_id in known:
            return self.selected_wall_id
        entity = document.entity(entity_id)
        point = (float(entity.position.x_m), float(entity.position.y_m))
        best: tuple[float, str] | None = None
        for wall in topology.walls:
            distance, _ = self._distance_to_segment(point, *self._wall_points(wall.wall_id))
            candidate = (distance, wall.wall_id)
            if best is None or candidate < best:
                best = candidate
        return None if best is None else best[1]

    def add_wall_clearance_for_selected(self) -> None:
        subject_id = self._constraint_subject_id()
        if subject_id is None:
            self.statusBar().showMessage('壁離隔を設定する物体を先に選択してください')
            return
        wall_id = self._wall_for_subject(subject_id)
        if wall_id is None:
            self.statusBar().showMessage('壁離隔には壁トポロジーが必要です')
            return
        minimum = self.constraint_distance_field.value() if self.constraint_distance_field is not None else 0.50
        constraint = CadWallClearanceConstraint(
            constraint_id=f'wall-clearance-{uuid4().hex[:10]}',
            name='壁離隔',
            entity_ids=(subject_id,),
            wall_id=wall_id,
            min_m=minimum,
        )
        self._replace_constraint_set(
            self.constraint_set.constraints + (constraint,),
            message=f'壁離隔を追加しました · {minimum:.3f} m 以上',
        )

    def add_pair_clearance_for_selection(self) -> None:
        if self.working is None:
            return
        selection = tuple(
            entity_id
            for entity_id in self.view_state.selection
            if any(entity.entity_id == entity_id for entity in self.working.committed_document.entities)
        )
        if len(selection) != 2:
            self.statusBar().showMessage('物体間離隔は2物体を選択してから追加してください')
            return
        minimum = self.constraint_distance_field.value() if self.constraint_distance_field is not None else 0.50
        constraint = CadPairDistanceConstraint(
            constraint_id=f'pair-clearance-{uuid4().hex[:10]}',
            name='物体間離隔',
            entity_a=selection[0],
            entity_b=selection[1],
            min_m=minimum,
            distance_mode='horizontal_xy',
            distance_reference='envelope_clearance',
        )
        self._replace_constraint_set(
            self.constraint_set.constraints + (constraint,),
            message=f'物体間離隔を追加しました · {minimum:.3f} m 以上',
        )

    def delete_selected_constraint(self) -> None:
        if self.constraint_tree is None:
            return
        item = self.constraint_tree.currentItem()
        if item is None:
            return
        constraint_id = item.data(0, ROLE + 1)
        if not constraint_id:
            return
        remaining = tuple(item for item in self.constraint_set.constraints if item.constraint_id != str(constraint_id))
        if len(remaining) == len(self.constraint_set.constraints):
            return
        self.constraint_set = CadConstraintSet(document_id=self.document_id, constraints=remaining)
        self.constraint_repository.save(self.constraint_set)
        self._clear_rejected_constraint_preview()
        self.constraint_selected_result_id = None
        self._refresh_constraint_state()
        self.statusBar().showMessage('制約を削除しました')

    def _find_result(self, result_id: str | None) -> CadConstraintResult | None:
        if result_id is None:
            return None
        return next((item for item in self._active_constraint_evaluation().results if item.result_id == result_id), None)

    @staticmethod
    def _requirement_text(result: CadConstraintResult) -> str:
        if result.required_min_m is not None and result.required_max_m is not None:
            return f'{result.required_min_m:.3f}–{result.required_max_m:.3f} m'
        if result.required_min_m is not None:
            return f'{result.required_min_m:.3f} m 以上'
        if result.required_max_m is not None:
            return f'{result.required_max_m:.3f} m 以下'
        return '領域条件'

    def _result_detail_text(self, result: CadConstraintResult | None) -> str:
        if result is None:
            return '違反理由を選択すると、対象と actual / required を表示します'
        actual = '—' if result.actual_m is None else f'{result.actual_m:.3f} m'
        entities = ', '.join(result.entity_ids) if result.entity_ids else '—'
        lines = [
            f'{result.name} · {result.reason_ja}',
            f'対象: {entities}',
            f'実測距離: {actual}',
            f'必要条件: {self._requirement_text(result)}',
        ]
        if result.wall_id is not None:
            lines.append(f'壁: {result.wall_id}')
        if result.region_role == 'walkway':
            lines.append('領域: 通路')
        elif result.region_role == 'allowed':
            lines.append('領域: 許可領域')
        elif result.region_role == 'exclusion':
            lines.append('領域: 除外領域')
        return '\n'.join(lines)

    def _refresh_constraint_state(self) -> None:
        if self.working is None or self.constraint_tree is None:
            return
        try:
            self.constraint_evaluation = evaluate_cad_constraints(
                self.working.committed_document,
                self.constraint_set,
            )
        except (CadConstraintAdapterError, ValueError) as exc:
            if self.constraint_summary_label is not None:
                self.constraint_summary_label.setText(f'制約を評価できません · {exc}')
            self.constraint_tree.clear()
            if self.constraint_detail_label is not None:
                self.constraint_detail_label.setText(str(exc))
            self._render_constraint_overlay(CadConstraintEvaluation(constraints_satisfied=False, results=()))
            return

        active = self._active_constraint_evaluation()
        violations = active.violations
        if self.constraint_summary_label is not None:
            if self.constraint_last_rejected is not None:
                self.constraint_summary_label.setText(f'直前の移動を拒否 · 制約違反 {len(violations)}件')
            elif not self.constraint_set.constraints:
                self.constraint_summary_label.setText('制約なし')
            elif active.constraints_satisfied:
                self.constraint_summary_label.setText('制約を満たす')
            else:
                self.constraint_summary_label.setText(f'制約違反 {len(violations)}件')

        with QSignalBlocker(self.constraint_tree):
            self.constraint_tree.clear()
            ordered = sorted(active.results, key=lambda item: (item.passed, item.name, item.result_id))
            selected_item: QTreeWidgetItem | None = None
            for result in ordered:
                marker = '✓' if result.passed else '⚠'
                measure = '' if result.actual_m is None else f' · {result.actual_m:.3f} m'
                tree_item = QTreeWidgetItem([f'{marker} {result.name} · {result.reason_ja}{measure}'])
                tree_item.setData(0, ROLE, result.result_id)
                tree_item.setData(0, ROLE + 1, result.constraint_id)
                self.constraint_tree.addTopLevelItem(tree_item)
                if result.result_id == self.constraint_selected_result_id:
                    selected_item = tree_item
            if selected_item is not None:
                self.constraint_tree.setCurrentItem(selected_item)
        selected = self._find_result(self.constraint_selected_result_id)
        if selected is None and violations:
            selected = violations[0]
            self.constraint_selected_result_id = selected.result_id
        if self.constraint_detail_label is not None:
            self.constraint_detail_label.setText(self._result_detail_text(selected))
        self._render_constraint_overlay(active)

    def _constraint_tree_selected(self) -> None:
        if self.constraint_tree is None:
            return
        item = self.constraint_tree.currentItem()
        result_id = None if item is None else item.data(0, ROLE)
        self.constraint_selected_result_id = None if result_id is None else str(result_id)
        result = self._find_result(self.constraint_selected_result_id)
        if result is not None and result.entity_ids:
            subject_id = result.entity_ids[0]
            if self.working is not None and any(
                entity.entity_id == subject_id for entity in self.working.committed_document.entities
            ):
                self._set_selection((subject_id,), primary_id=subject_id, cancel_preview=True, persist=True)
                self.constraint_last_subject_id = subject_id
        if self.constraint_detail_label is not None:
            self.constraint_detail_label.setText(self._result_detail_text(result))
        self._render_constraint_overlay(self._active_constraint_evaluation())

    def _remove_constraint_overlays(self) -> None:
        for name in tuple(self._constraint_actor_names):
            try:
                self.viewport.remove_actor(name, reset_camera=False, render=False)
            except Exception:
                pass
        self._constraint_actor_names.clear()
        try:
            self.viewport.remove_actor('constraint-detail-overlay', reset_camera=False, render=False)
        except Exception:
            pass

    @staticmethod
    def _region_mesh(vertices: tuple[CadConstraintPoint2D, ...], *, z_m: float) -> pv.PolyData:
        points = np.asarray([(item.x_m, -item.y_m, z_m) for item in vertices], dtype=float)
        faces = np.asarray([len(points), *range(len(points))], dtype=np.int64)
        return pv.PolyData(points, faces).triangulate()

    def _render_constraint_overlay(self, evaluation: CadConstraintEvaluation) -> None:
        if self.working is None:
            return
        self._remove_constraint_overlays()
        selected = self._find_result(self.constraint_selected_result_id)
        selected_constraint_id = None if selected is None else selected.constraint_id

        for constraint in self.constraint_set.constraints:
            if not isinstance(constraint, (CadAllowedRegionConstraint, CadExclusionRegionConstraint)):
                continue
            name = f'constraint-region:{constraint.constraint_id}'
            self._constraint_actor_names.add(name)
            is_selected = constraint.constraint_id == selected_constraint_id
            if isinstance(constraint, CadAllowedRegionConstraint):
                color = 'steelblue'
                opacity = 0.08
            elif constraint.region_role == 'walkway':
                color = 'darkorange'
                opacity = 0.16
            else:
                color = 'tomato'
                opacity = 0.14
            mesh = self._region_mesh(constraint.vertices, z_m=0.015)
            self.viewport.add_mesh(
                mesh,
                name=name,
                color=color,
                opacity=opacity,
                show_edges=True,
                line_width=5 if is_selected else 2,
                pickable=False,
                render=False,
            )
            if isinstance(constraint, CadExclusionRegionConstraint):
                points = constraint.vertices
                if len(points) >= 3:
                    cx = sum(item.x_m for item in points) / len(points)
                    cy = sum(item.y_m for item in points) / len(points)
                    for index, point in enumerate(points[::2]):
                        pattern_name = f'constraint-pattern:{constraint.constraint_id}:{index}'
                        self._constraint_actor_names.add(pattern_name)
                        self.viewport.add_mesh(
                            pv.Line((cx, -cy, 0.022), (point.x_m, -point.y_m, 0.022)),
                            name=pattern_name,
                            color=color,
                            line_width=3 if constraint.region_role == 'walkway' else 2,
                            pickable=False,
                            render=False,
                        )

        involved = set() if selected is None else set(selected.entity_ids)
        normal_selection = set(self.view_state.selection)
        for entity_id, actor in self.actors.items():
            actor.prop.show_edges = entity_id in normal_selection or entity_id in involved
            if entity_id in involved:
                actor.prop.line_width = 6
            elif entity_id == self.selected_id:
                actor.prop.line_width = 4
            elif entity_id in normal_selection:
                actor.prop.line_width = 2
            else:
                actor.prop.line_width = 1

        if selected is not None:
            self._render_selected_constraint_geometry(selected)
            text = self._result_detail_text(selected)
            self.viewport.add_text(
                text,
                position='lower_left',
                font_size=9,
                name='constraint-detail-overlay',
                render=False,
            )
        self.viewport.render()

    def _subject_position_for_result(self, result: CadConstraintResult) -> Position3 | None:
        if not result.entity_ids or self.working is None:
            return None
        entity_id = result.entity_ids[0]
        if entity_id in self.constraint_last_rejected_positions:
            return self.constraint_last_rejected_positions[entity_id]
        try:
            return self.working.committed_document.entity(entity_id).position
        except KeyError:
            return None

    def _render_selected_constraint_geometry(self, result: CadConstraintResult) -> None:
        if self.working is None:
            return
        document = self.working.committed_document
        subject = self._subject_position_for_result(result)
        if subject is None:
            return

        if result.entity_ids and result.entity_ids[0] in self.constraint_last_rejected_positions:
            marker_name = 'constraint-rejected-candidate'
            self._constraint_actor_names.add(marker_name)
            self.viewport.add_mesh(
                pv.Sphere(radius=0.10, center=(subject.x_m, -subject.y_m, subject.z_m)),
                name=marker_name,
                style='wireframe',
                line_width=4,
                color='red',
                pickable=False,
                render=False,
            )

        if result.wall_id is not None:
            try:
                start, end = self._wall_points(result.wall_id)
            except ValueError:
                return
            wall_name = 'constraint-selected-wall'
            self._constraint_actor_names.add(wall_name)
            self.viewport.add_mesh(
                pv.Line((start[0], -start[1], 0.04), (end[0], -end[1], 0.04)),
                name=wall_name,
                line_width=8,
                color='red',
                pickable=False,
                render=False,
            )
            _, closest = self._distance_to_segment((subject.x_m, subject.y_m), start, end)
            distance_name = 'constraint-distance-segment'
            self._constraint_actor_names.add(distance_name)
            self.viewport.add_mesh(
                pv.Line(
                    (subject.x_m, -subject.y_m, max(subject.z_m * 0.15, 0.05)),
                    (closest[0], -closest[1], max(subject.z_m * 0.15, 0.05)),
                ),
                name=distance_name,
                line_width=5,
                color='red',
                pickable=False,
                render=False,
            )
            return

        if result.kind == 'pair_distance' and len(result.entity_ids) == 2:
            try:
                other = document.entity(result.entity_ids[1]).position
            except KeyError:
                return
            pair_name = 'constraint-pair-distance'
            self._constraint_actor_names.add(pair_name)
            self.viewport.add_mesh(
                pv.Line(
                    (subject.x_m, -subject.y_m, max(subject.z_m, 0.05)),
                    (other.x_m, -other.y_m, max(other.z_m, 0.05)),
                ),
                name=pair_name,
                line_width=5,
                color='red',
                pickable=False,
                render=False,
            )

    def _preview_constraint_evaluations(
        self,
        selection: tuple[str, ...],
    ) -> tuple[CadConstraintEvaluation, CadConstraintEvaluation, dict[str, Position3]]:
        if self.working is None:
            raise CadConstraintAdapterError('scene is not open')
        committed = self.working.committed_document
        preview = self.working.document
        positions = {entity_id: preview.entity(entity_id).position for entity_id in selection}
        before = evaluate_cad_constraints(committed, self.constraint_set)
        candidate = evaluate_cad_constraints(
            committed,
            self.constraint_set,
            position_overrides=positions,
        )
        return before, candidate, positions

    def _commit_active_preview(self, expected_kind: str | None = None) -> bool:
        if (
            self.working is not None
            and self.working.has_preview
            and self.working.preview_kind == 'move'
            and self.constraint_set.constraints
        ):
            selection = self.view_state.selection or (() if self.selected_id is None else (self.selected_id,))
            try:
                before, candidate, positions = self._preview_constraint_evaluations(selection)
                blocked = blocking_candidate_violations(before, candidate, set(selection))
            except (CadConstraintAdapterError, ValueError) as exc:
                super().cancel_preview()
                self.statusBar().showMessage(f'移動を確定できません · 制約評価エラー · {exc}')
                return False
            if blocked:
                self.constraint_last_rejected = candidate
                self.constraint_last_rejected_positions = positions
                self.constraint_selected_result_id = blocked[0].result_id
                super().cancel_preview()
                self.statusBar().showMessage(
                    f'移動を拒否しました · {blocked[0].reason_ja} · 制約一覧で理由を確認できます'
                )
                return False
        self._clear_rejected_constraint_preview()
        return super()._commit_active_preview(expected_kind)  # type: ignore[arg-type]

    def _numeric_position_edited(self) -> None:
        if not self.constraint_set.constraints:
            self._clear_rejected_constraint_preview()
            super()._numeric_position_edited()
            return
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
        base = self.working.committed_document.entity(self.selected_id).position
        delta = (position.x_m - base.x_m, position.y_m - base.y_m, position.z_m - base.z_m)
        self.working.begin_group_move(selection)
        self.working.preview_group_move(delta)
        self._commit_active_preview('move')

    def _entity_constraint_ids(self, entity_id: str) -> tuple[str, ...]:
        result: list[str] = []
        for constraint in self.constraint_set.constraints:
            if isinstance(constraint, (CadAllowedRegionConstraint, CadExclusionRegionConstraint, CadWallClearanceConstraint)):
                entity_ids = constraint.entity_ids
            else:
                entity_ids = (constraint.entity_a, constraint.entity_b)
            if entity_id in entity_ids:
                result.append(constraint.constraint_id)
        return tuple(result)

    def delete_selected(self) -> None:
        if self.selected_id is not None:
            refs = self._entity_constraint_ids(self.selected_id)
            if refs:
                self.statusBar().showMessage('制約が参照している物体は削除できません · 先に制約を削除してください')
                return
        super().delete_selected()

    def _wall_constraint_ids(self, wall_id: str | None) -> tuple[str, ...]:
        if wall_id is None:
            return ()
        return tuple(
            item.constraint_id
            for item in self.constraint_set.constraints
            if isinstance(item, CadWallClearanceConstraint) and item.wall_id == wall_id
        )

    def split_selected_wall(self) -> None:
        if self._wall_constraint_ids(self.selected_wall_id):
            self.statusBar().showMessage('壁離隔制約がある壁は分割前に制約を削除または再設定してください')
            return
        super().split_selected_wall()

    def merge_selected_wall_with_next(self) -> None:
        if self.working is not None and self.selected_wall_id is not None:
            topology = self.working.committed_document.wall_topology
            if topology is not None:
                ids = [wall.wall_id for wall in topology.walls]
                if self.selected_wall_id in ids:
                    index = ids.index(self.selected_wall_id)
                    next_id = ids[(index + 1) % len(ids)]
                    if self._wall_constraint_ids(self.selected_wall_id) or self._wall_constraint_ids(next_id):
                        self.statusBar().showMessage('壁離隔制約がある壁は結合前に制約を削除または再設定してください')
                        return
        super().merge_selected_wall_with_next()

    def delete_selected_wall(self) -> None:
        if self.working is not None and self.selected_wall_id is not None:
            topology = self.working.committed_document.wall_topology
            if topology is not None:
                ids = [wall.wall_id for wall in topology.walls]
                if self.selected_wall_id in ids:
                    index = ids.index(self.selected_wall_id)
                    successor = ids[(index + 1) % len(ids)]
                    if self._wall_constraint_ids(self.selected_wall_id) or self._wall_constraint_ids(successor):
                        self.statusBar().showMessage('壁離隔制約がある壁は削除前に制約を削除または再設定してください')
                        return
        super().delete_selected_wall()
