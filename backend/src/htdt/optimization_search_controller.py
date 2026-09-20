from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from threading import Event
from uuid import uuid4

import numpy as np
import pyvista as pv
from PySide6.QtCore import QObject, QSignalBlocker, QThread, Qt, Signal, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QComboBox,
    QDockWidget,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTreeWidget,
    QListWidget,
    QListWidgetItem,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .analysis_markers import render_analysis_marker_cloud
from .cad_adaptive_repository import CadAdaptivePlanRepository
from .cad_adaptive_service import CadAdaptivePlannerService
from .cad_extended_search import (
    CadExtendedCandidate,
    CadExtendedCandidateSetPage,
    CadExtendedSearchAxis,
    aim_horizontal_yaw_deg,
    apply_extended_candidate,
    build_extended_model_capability,
    build_extended_search_spec,
    extended_candidate_preview_document,
    generate_extended_candidates,
)
from .cad_extended_search_repository import CadExtendedSearchRepository
from .cad_repository import SceneRepository, SceneRevision
from .cad_objectives import build_pareto_set
from .cad_objective_repository import CadObjectiveRepository
from .cad_scene import F1_DOCUMENT_ID
from .cad_search import (
    apply_candidate_positions,
    build_cad_search_spec,
    generate_cad_candidates,
    search_spec_current_working,
)
from .cad_search_models import CadCandidate, CadCandidateSetPage, CadSearchAxis, CadSearchSpec
from .cad_search_repository import CadSearchRepository
from .measurement_workspace import _MeasurementScrollArea
from .cad_measurement_repository import CadMeasurementRepository
from .cad_model_validation_repository import CadModelValidationRepository
from .cad_model_validation_service import CadModelValidationService
from .cad_roomsim_repository import CadRoomSimRepository
from .cad_measurement_loop import build_measurement_plan, complete_measurement_plan
from .cad_validation_campaign import (
    CadValidationCampaignCandidate,
    CadValidationCampaignRepeatability,
    CadValidationCampaignSensitivity,
    CadValidationCampaignSeparation,
    CadValidationTargetResponse,
    build_validation_campaign,
)
from .cad_validation_campaign_repository import CadValidationCampaignRepository
from .cad_validation_campaign_service import CadValidationCampaignService
from .cad_validation_metrics import CadApplicabilityCheck
from .native_editor import ROLE

from .optimization_task import _SearchTask

def candidate_cloud_points(
    page: CadCandidateSetPage,
    primary_entity_id: str,
) -> np.ndarray:
    """Return one representative domain point per visible candidate."""

    points: list[tuple[float, float, float]] = []
    for candidate in page.candidates:
        position = candidate.positions.get(primary_entity_id)
        if position is None:
            continue
        points.append(
            (
                float(position['x_m']),
                float(position['y_m']),
                float(position['z_m']),
            )
        )
    if not points:
        return np.empty((0, 3), dtype=float)
    return np.asarray(points, dtype=float)



class SearchControllerMixin:
    @staticmethod
    def _search_distance_field(*, minimum: float = -1000.0, value: float = 0.0) -> QDoubleSpinBox:
        field = QDoubleSpinBox()
        field.setRange(minimum, 1000.0)
        field.setDecimals(3)
        field.setSingleStep(0.05)
        field.setValue(value)
        field.setSuffix(' m')
        return field

    def _refresh_search_entities(self) -> None:
        combo = self.search_entity_combo
        if combo is None or self.working is None:
            return
        previous = combo.currentData()
        with QSignalBlocker(combo):
            combo.clear()
            for entity in self.working.committed_document.entities:
                combo.addItem(f'{entity.name} · {entity.kind}', entity.entity_id)
            if previous is not None:
                index = combo.findData(previous)
                if index >= 0:
                    combo.setCurrentIndex(index)
        self._seed_search_axis_range()
        if self.extended_entity_combo is not None:
            self._refresh_extended_entities()

    def _seed_search_axis_range(self) -> None:
        if (
            self.working is None
            or self.search_entity_combo is None
            or self.search_axis_combo is None
            or self.search_min_field is None
            or self.search_max_field is None
        ):
            return
        entity_id = self.search_entity_combo.currentData()
        axis = self.search_axis_combo.currentData()
        if entity_id is None or axis not in ('x', 'y', 'z'):
            return
        try:
            entity = self.working.committed_document.entity(str(entity_id))
        except KeyError:
            return
        position = float(getattr(entity.position, f'{axis}_m'))
        room = self.working.committed_document.room
        upper = None
        if room is not None:
            upper = {'x': float(room.width_m), 'y': float(room.depth_m), 'z': float(room.height_m)}[axis]
        low = max(0.0, position - 0.50) if upper is not None else position - 0.50
        high = min(upper, position + 0.50) if upper is not None else position + 0.50
        self.search_min_field.setValue(low)
        self.search_max_field.setValue(max(low, high))

    def add_search_axis(self) -> None:
        if (
            self.search_axis_tree is None
            or self.search_entity_combo is None
            or self.search_axis_combo is None
            or self.search_min_field is None
            or self.search_max_field is None
            or self.search_step_field is None
        ):
            return
        entity_id = self.search_entity_combo.currentData()
        axis = self.search_axis_combo.currentData()
        if entity_id is None or axis not in ('x', 'y', 'z'):
            return
        try:
            item = CadSearchAxis(
                entity_id=str(entity_id),
                axis=axis,
                min_m=float(self.search_min_field.value()),
                max_m=float(self.search_max_field.value()),
                step_m=float(self.search_step_field.value()),
            )
        except Exception as exc:
            self.statusBar().showMessage(f'探索軸を追加できません · {exc}')
            return

        for index in range(self.search_axis_tree.topLevelItemCount()):
            current = self.search_axis_tree.topLevelItem(index)
            payload = current.data(0, ROLE)
            if isinstance(payload, dict) and (
                payload.get('entity_id'), payload.get('axis')
            ) == (item.entity_id, item.axis):
                self._set_axis_tree_item(current, item)
                return
        tree_item = QTreeWidgetItem()
        self._set_axis_tree_item(tree_item, item)
        self.search_axis_tree.addTopLevelItem(tree_item)

    def _entity_display_name(self, entity_id: str) -> str:
        if self.working is not None:
            try:
                return self.working.committed_document.entity(entity_id).name
            except KeyError:
                pass
        return "対象"

    def _set_axis_tree_item(self, tree_item: QTreeWidgetItem, axis: CadSearchAxis) -> None:
        tree_item.setText(0, self._entity_display_name(axis.entity_id))
        tree_item.setText(1, axis.axis.upper())
        tree_item.setText(2, f'{axis.min_m:.3f}')
        tree_item.setText(3, f'{axis.max_m:.3f}')
        tree_item.setText(4, f'{axis.step_m:.3f}')
        tree_item.setData(0, ROLE, axis.model_dump(mode='json'))

    def remove_selected_search_axis(self) -> None:
        tree = self.search_axis_tree
        if tree is None:
            return
        item = tree.currentItem()
        if item is None:
            return
        index = tree.indexOfTopLevelItem(item)
        if index >= 0:
            tree.takeTopLevelItem(index)

    def _draft_search_axes(self) -> tuple[CadSearchAxis, ...]:
        tree = self.search_axis_tree
        if tree is None:
            return ()
        result: list[CadSearchAxis] = []
        for index in range(tree.topLevelItemCount()):
            payload = tree.topLevelItem(index).data(0, ROLE)
            if isinstance(payload, dict):
                result.append(CadSearchAxis.model_validate(payload))
        return tuple(result)

    def _saved_search_revision(self) -> SceneRevision:
        if self.working is None or self.working.source_revision_id is None:
            raise ValueError('保存済みの部屋状態が必要です')
        if self.working.has_preview:
            raise ValueError('編集中の移動・回転を確定またはキャンセルしてください')
        if self.working.is_dirty:
            raise ValueError('探索設定を作る前に現在の配置を保存してください')
        revision = self.repository.get(self.working.source_revision_id)
        if revision is None:
            raise ValueError('現在の保存状態を読み込めません')
        return revision

    def save_search_spec(self) -> None:
        axes = self._draft_search_axes()
        if not axes:
            self.statusBar().showMessage('探索設定には少なくとも1つの可動軸が必要です')
            return
        try:
            revision = self._saved_search_revision()
            limit = 10_000 if self.search_limit_field is None else int(self.search_limit_field.value())
            name = None if self.search_name_field is None else self.search_name_field.text()
            spec, estimate = build_cad_search_spec(
                revision,
                self.constraint_set,
                axes,
                candidate_limit=limit,
                name=name,
            )
            self.search_repository.save(spec)
        except Exception as exc:
            self.statusBar().showMessage(f'探索設定を保存できません · {exc}')
            return

        self.search_selected_spec_id = spec.search_spec_id
        self.search_candidate_page = None
        self.search_selected_candidate_id = None
        self.search_preview_candidate_id = None
        self._refresh_search_specs()
        self._remove_search_overlays()
        self.statusBar().showMessage(
            f'探索設定を保存しました · 総候補 {estimate["raw_candidate_count"]}'
        )

    def _refresh_search_specs(self) -> None:
        tree = self.search_spec_tree
        if tree is None:
            return
        specs = self.search_repository.list_specs(self.document_id)
        if specs and self.search_selected_spec_id is None:
            self.search_selected_spec_id = specs[-1].search_spec_id

        selected_item: QTreeWidgetItem | None = None
        with QSignalBlocker(tree):
            tree.clear()
            for display_index, spec in enumerate(reversed(specs), start=1):
                current = (
                    self.working is not None
                    and search_spec_current_working(
                    spec,
                    self.working,
                    self.constraint_set,
                    current_document_id=self.document_id,
                )
                )
                item = QTreeWidgetItem(
                    [
                        spec.name or f'探索設定 {display_index}',
                        '現在の部屋' if current else '以前の部屋',
                        '利用可' if current else '再設定が必要',
                    ]
                )
                item.setData(0, ROLE, spec.search_spec_id)
                tree.addTopLevelItem(item)
                if spec.search_spec_id == self.search_selected_spec_id:
                    selected_item = item
            if selected_item is not None:
                tree.setCurrentItem(selected_item)
        self._refresh_search_binding_state()

    def _search_spec_selected(self) -> None:
        tree = self.search_spec_tree
        if tree is None:
            return
        item = tree.currentItem()
        spec_id = None if item is None else item.data(0, ROLE)
        normalized = None if spec_id is None else str(spec_id)
        if normalized != self.search_selected_spec_id:
            self.search_candidate_page = None
            self.search_selected_candidate_id = None
            self.search_preview_candidate_id = None
            self.extended_selected_spec_id = None
            self.extended_candidate_page = None
            self.extended_selected_candidate_id = None
            self.extended_preview_candidate_id = None
            self.extended_axes.clear()
            self._refresh_extended_axis_tree()
            self.campaign_assignments.clear()
            self._refresh_campaign_assignment_tree()
        self.search_selected_spec_id = normalized
        self.extended_candidate_page = None
        self.extended_selected_candidate_id = None
        self.extended_preview_candidate_id = None
        self._refresh_search_binding_state()
        self._refresh_search_candidate_tree()
        self.refresh_measurement_plans()
        self.refresh_validation_campaigns()
        self.refresh_model_validations()
        self.refresh_adaptive_plans()
        self._refresh_extended_entities()
        self._refresh_extended_capabilities()
        self._refresh_extended_specs()
        self._render_search_overlay()
        self._render_extended_overlay()

    def _selected_search_spec(self) -> CadSearchSpec | None:
        if self.search_selected_spec_id is None:
            return None
        return self.search_repository.get(self.search_selected_spec_id)

    def _selected_search_candidate(self) -> CadCandidate | None:
        page = self.search_candidate_page
        if page is None or self.search_selected_candidate_id is None:
            return None
        return next(
            (
                candidate
                for candidate in page.candidates
                if candidate.candidate_id == self.search_selected_candidate_id
            ),
            None,
        )

    def _refresh_search_binding_state(self) -> None:
        spec = self._selected_search_spec()
        current = (
            spec is not None
            and self.working is not None
            and search_spec_current_working(
                    spec,
                    self.working,
                    self.constraint_set,
                    current_document_id=self.document_id,
                )
        )
        if self.search_binding_label is not None:
            if self.working is None or self.working.source_revision_id is None:
                self.search_binding_label.setText('保存済みの部屋状態がありません')
            elif spec is None:
                self.search_binding_label.setText(
                    '現在の保存状態から探索設定を作成してください'
                )
            else:
                self.search_binding_label.setText(
                    '現在の部屋・制約に一致しています'
                    if current
                    else '部屋または制約が変更されています · 探索設定を更新してください'
                )
        search_busy = self._current_search_task_id is not None
        extended_busy = self._current_extended_task_id is not None
        busy = search_busy or extended_busy
        if self.search_save_button is not None:
            self.search_save_button.setEnabled(
                not busy
                and self.working is not None
                and self.working.source_revision_id is not None
                and not self.working.is_dirty
                and not self.working.has_preview
            )
        if self.search_generate_button is not None:
            self.search_generate_button.setEnabled(not busy and bool(current))
        if self.search_generate_reason_label is not None:
            if self.working is None or self.working.source_revision_id is None:
                reason = "部屋を保存してから候補を生成できます。"
            elif spec is None:
                reason = "先に「探索設定」で探索設定を保存・選択してください。"
            elif not current:
                reason = "部屋または制約が変更されています。探索設定を更新してください。"
            else:
                reason = ""
            self.search_generate_reason_label.setText(reason)
            self.search_generate_reason_label.setVisible(bool(reason))
        if self.search_cancel_button is not None:
            self.search_cancel_button.setEnabled(search_busy)
        has_candidate = self._selected_search_candidate() is not None and bool(current)
        if self.search_preview_button is not None:
            self.search_preview_button.setEnabled(has_candidate)
        if self.search_apply_button is not None:
            self.search_apply_button.setEnabled(has_candidate)
        if self.search_clear_preview_button is not None:
            self.search_clear_preview_button.setEnabled(self.search_preview_candidate_id is not None)
        page = self.search_candidate_page
        if self.search_prev_button is not None:
            self.search_prev_button.setEnabled(
                not busy and bool(current) and page is not None and page.offset > 0
            )
        if self.search_next_button is not None:
            self.search_next_button.setEnabled(
                not busy
                and bool(current)
                and page is not None
                and page.offset + len(page.candidates) < page.feasible_candidate_count
            )

    def generate_search_candidates_async(self, *, offset: int | None = None) -> None:
        if self._current_search_task_id is not None or self._current_extended_task_id is not None:
            return
        spec = self._selected_search_spec()
        if spec is None or self.working is None:
            self.statusBar().showMessage('生成する探索設定を選択してください')
            return
        if not search_spec_current_working(
            spec,
            self.working,
            self.constraint_set,
            current_document_id=self.document_id,
        ):
            self.statusBar().showMessage('部屋または制約が変更された探索設定からは候補を生成できません')
            self._refresh_search_binding_state()
            return

        page_offset = 0 if offset is None else max(0, int(offset))
        key = str(uuid4())
        self._current_search_task_id = key
        self._search_task_spec_ids[key] = spec.search_spec_id
        self._refresh_search_binding_state()
        self.statusBar().showMessage(
            f'候補を生成しています… · {page_offset + 1}件目から'
        )
        self._start_search_task(
            key,
            lambda cancel_event: generate_cad_candidates(
                self.repository,
                spec,
                offset=page_offset,
                limit=self.search_page_limit,
                cancelled=cancel_event.is_set,
            ),
        )

    def _start_search_task(self, key: str, operation: Callable[[Event], object]) -> None:
        thread = QThread(self)
        worker = _SearchTask(key, operation)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.completed.connect(self._search_task_completed)
        worker.completed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._search_tasks[key] = (thread, worker)
        thread.start()

    def cancel_search_generation(self) -> None:
        key = self._current_search_task_id
        if key is None:
            return
        record = self._search_tasks.get(key)
        if record is not None:
            record[1].cancel()
        self.statusBar().showMessage('候補生成をキャンセルしています…')

    @Slot(object, object, object)
    def _search_task_completed(self, key: object, result: object, error: object) -> None:
        task_id = str(key)
        spec_id = self._search_task_spec_ids.pop(task_id, None)
        self._search_tasks.pop(task_id, None)
        if self._current_search_task_id == task_id:
            self._current_search_task_id = None

        if error == 'cancelled':
            self.statusBar().showMessage('候補生成をキャンセルしました')
            self._refresh_search_binding_state()
            return
        if error is not None:
            self.statusBar().showMessage(f'候補生成に失敗しました · {error}')
            self._refresh_search_binding_state()
            return
        if not isinstance(result, CadCandidateSetPage):
            self.statusBar().showMessage('候補生成結果を利用できません · 結果形式が一致しません')
            self._refresh_search_binding_state()
            return
        spec = None if spec_id is None else self.search_repository.get(spec_id)
        if (
            spec is None
            or self.working is None
            or not search_spec_current_working(
                        spec,
                        self.working,
                        self.constraint_set,
                        current_document_id=self.document_id,
                    )
        ):
            self.statusBar().showMessage('古い候補生成結果を破棄しました · 部屋または制約が変更されています')
            self._refresh_search_binding_state()
            return
        if spec.search_spec_id != self.search_selected_spec_id:
            self.statusBar().showMessage('候補生成は完了しました · 別の探索設定が選択されています')
            self._refresh_search_binding_state()
            return

        self.search_candidate_page = result
        self.search_preview_candidate_id = None
        self.search_selected_candidate_id = (
            result.candidates[0].candidate_id if result.candidates else None
        )
        self._refresh_search_candidate_tree()
        self._refresh_search_binding_state()
        self._render_search_overlay()
        if self.search_summary_label is not None:
            self.search_summary_label.setText(
                f'総候補 {result.raw_candidate_count} · 有効 {result.feasible_candidate_count} · '
                f'除外 {result.rejected_candidate_count} · 重複 {result.duplicate_candidate_count} · '
                f'表示 {result.offset + 1 if result.candidates else 0}–'
                f'{result.offset + len(result.candidates)}'
            )
        self.statusBar().showMessage(
            f'候補を生成しました · 有効 {result.feasible_candidate_count}件'
        )

    def _refresh_search_candidate_tree(self) -> None:
        tree = self.search_candidate_tree
        if tree is None:
            return
        page = self.search_candidate_page
        selected_item: QTreeWidgetItem | None = None
        with QSignalBlocker(tree):
            tree.clear()
            if page is None:
                return
            for row_number, candidate in enumerate(page.candidates, start=1):
                position_text = ' · '.join(
                    f'{self._entity_display_name(entity_id)} '
                    f'({position["x_m"]:.2f}, {position["y_m"]:.2f}, {position["z_m"]:.2f})'
                    for entity_id, position in sorted(candidate.positions.items())
                )
                item = QTreeWidgetItem(
                    [
                        f'候補 {result_number}'
                        if (result_number := page.offset + row_number) > 0
                        else f'候補 {row_number}',
                        str(candidate.feasible_index + 1),
                        position_text,
                    ]
                )
                item.setData(0, ROLE, candidate.candidate_id)
                tree.addTopLevelItem(item)
                if candidate.candidate_id == self.search_selected_candidate_id:
                    selected_item = item
            if selected_item is not None:
                tree.setCurrentItem(selected_item)

    def _search_candidate_selected(self) -> None:
        tree = self.search_candidate_tree
        if tree is None:
            return
        item = tree.currentItem()
        candidate_id = None if item is None else item.data(0, ROLE)
        self.search_selected_candidate_id = None if candidate_id is None else str(candidate_id)
        self._refresh_search_binding_state()
        self._render_search_overlay()

    def previous_search_page(self) -> None:
        page = self.search_candidate_page
        if page is None:
            return
        self.generate_search_candidates_async(offset=max(0, page.offset - self.search_page_limit))

    def next_search_page(self) -> None:
        page = self.search_candidate_page
        if page is None:
            return
        next_offset = page.offset + self.search_page_limit
        if next_offset < page.feasible_candidate_count:
            self.generate_search_candidates_async(offset=next_offset)

    def preview_selected_candidate(self) -> None:
        candidate = self._selected_search_candidate()
        if candidate is None:
            return
        self.search_preview_candidate_id = candidate.candidate_id
        self._refresh_search_binding_state()
        self._render_search_overlay()
        self.statusBar().showMessage('候補preview · Scene/Undo履歴は変更していません')

    def clear_candidate_preview(self) -> None:
        if self.search_preview_candidate_id is None:
            return
        self.search_preview_candidate_id = None
        self._refresh_search_binding_state()
        self._render_search_overlay()
        self.statusBar().showMessage('候補previewを解除しました')

    def apply_selected_candidate(self) -> None:
        spec = self._selected_search_spec()
        candidate = self._selected_search_candidate()
        if spec is None or candidate is None or self.working is None:
            return
        try:
            changed = apply_candidate_positions(
                self.working,
                candidate,
                spec=spec,
                current_constraint_set=self.constraint_set,
                current_document_id=self.document_id,
            )
        except Exception as exc:
            self.statusBar().showMessage(f'候補を適用できません · {exc}')
            self._refresh_search_binding_state()
            return
        if not changed:
            self.statusBar().showMessage('候補は現在配置と同一です')
            return

        entity_ids = tuple(sorted(candidate.positions))
        self.view_state.set_selection(entity_ids, primary_id=entity_ids[-1] if entity_ids else None)
        self.selected_id = self.view_state.selected_id
        self.search_preview_candidate_id = None
        self._sync_recovery()
        self._persist_view_state()
        self._rebuild()
        self._set_dirty_status()
        self._refresh_search_specs()
        self.statusBar().showMessage('候補を1 commandで適用しました · Undoで全位置を復元できます')

    def _remove_search_overlays(self) -> None:
        for name in tuple(self._search_actor_names):
            try:
                self.viewport.remove_actor(name, reset_camera=False, render=False)
            except Exception:
                pass
        self._search_actor_names.clear()

    def _render_search_overlay(self) -> None:
        self._remove_search_overlays()
        page = self.search_candidate_page
        spec = self._selected_search_spec()
        if page is None or spec is None or self.working is None:
            self.viewport.render()
            return
        if not search_spec_current_working(
            spec,
            self.working,
            self.constraint_set,
            current_document_id=self.document_id,
        ):
            self.viewport.render()
            return

        primary_entity_id = spec.axes[0].entity_id
        points = candidate_cloud_points(page, primary_entity_id)
        if len(points):
            actor_name = 'search-candidate-cloud'
            render_analysis_marker_cloud(
                self.viewport,
                points,
                actor_name=actor_name,
                point_size=6.0,
                render=False,
            )
            self._search_actor_names.add(actor_name)

        selected = self._selected_search_candidate()
        if selected is not None:
            primary = selected.positions.get(primary_entity_id)
            if primary is not None:
                name = 'search-selected-candidate'
                self._search_actor_names.add(name)
                self.viewport.add_mesh(
                    pv.Sphere(
                        radius=0.07,
                        center=(
                            float(primary['x_m']),
                            -float(primary['y_m']),
                            float(primary['z_m']),
                        ),
                    ),
                    name=name,
                    style='wireframe',
                    line_width=4,
                    pickable=False,
                    render=False,
                )

        preview = next(
            (
                candidate
                for candidate in page.candidates
                if candidate.candidate_id == self.search_preview_candidate_id
            ),
            None,
        )
        if preview is not None:
            for index, (entity_id, position) in enumerate(sorted(preview.positions.items())):
                try:
                    current = self.working.committed_document.entity(entity_id).position
                except KeyError:
                    continue
                target = (
                    float(position['x_m']),
                    -float(position['y_m']),
                    float(position['z_m']),
                )
                source = (float(current.x_m), -float(current.y_m), float(current.z_m))
                line_name = f'search-preview-line:{index}'
                point_name = f'search-preview-point:{index}'
                self._search_actor_names.update((line_name, point_name))
                self.viewport.add_mesh(
                    pv.Line(source, target),
                    name=line_name,
                    line_width=3,
                    pickable=False,
                    render=False,
                )
                self.viewport.add_mesh(
                    pv.Sphere(radius=0.055, center=target),
                    name=point_name,
                    style='wireframe',
                    line_width=3,
                    pickable=False,
                    render=False,
                )
            label_name = 'search-preview-label'
            self._search_actor_names.add(label_name)
            self.viewport.add_text(
                '候補preview · Scene未変更',
                position='upper_right',
                font_size=9,
                name=label_name,
                render=False,
            )
        self.viewport.render()

