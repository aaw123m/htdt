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
from .user_facing_error import operation_error_message


class MeasurementPlanControllerMixin:
    def create_measurement_plan_for_selected_candidate(self) -> None:
        if self.search_selected_spec_id is None or self.search_selected_candidate_id is None:
            self.statusBar().showMessage('探索設定と候補を選択してください')
            return
        latest = self.repository.current_head(self.document_id)
        if latest is None or self.working is None or self.working.is_dirty:
            self.statusBar().showMessage('候補を適用した部屋を保存してから実測候補を記録してください')
            return
        try:
            plan = build_measurement_plan(
                self.repository, self.search_repository,
                search_spec_id=self.search_selected_spec_id,
                candidate_id=self.search_selected_candidate_id,
                applied_scene_revision_id=latest.revision_id,
            )
            self.measurement_repository.save_measurement_plan(plan)
        except Exception as exc:
            self.statusBar().showMessage(f'実測候補を記録できません · {operation_error_message(exc)}')
            return
        if self.measurement_plan_label is not None:
            self.measurement_plan_label.setText(
                '実測候補を記録しました · 対応する保存状態を固定しています'
            )
        self.refresh_measurement_plans()
        self.statusBar().showMessage('実測候補を保存しました · 実際の配置変更と測定は人が行います')

    def refresh_measurement_plans(self) -> None:
        tree = self.measurement_plan_tree
        if tree is None:
            return
        tree.clear()
        spec_id = self.search_selected_spec_id
        if spec_id is None:
            if self.measurement_match_list is not None:
                self.measurement_match_list.clear()
            return
        plans = self.measurement_repository.latest_measurement_plans(spec_id)
        for index, plan in enumerate(plans, start=1):
            status_text = '測定済み' if plan.status == 'measured' else '測定待ち'
            item = QTreeWidgetItem([
                f'実測候補 {index}',
                status_text,
                '固定済み',
                str(len(plan.measurement_ids)),
            ])
            item.setData(0, ROLE, plan.plan_id)
            tree.addTopLevelItem(item)
        if self.measurement_plan_label is not None:
            planned = sum(plan.status == 'planned' for plan in plans)
            measured = sum(plan.status == 'measured' for plan in plans)
            self.measurement_plan_label.setText(
                f'実測候補 {len(plans)}件 · 測定待ち {planned} · 測定済み {measured}'
            )

    def _selected_measurement_plan(self):
        tree = self.measurement_plan_tree
        spec_id = self.search_selected_spec_id
        if tree is None or spec_id is None:
            return None
        item = tree.currentItem()
        if item is None:
            return None
        plan_id = item.data(0, ROLE)
        if not isinstance(plan_id, str):
            return None
        return next(
            (plan for plan in self.measurement_repository.latest_measurement_plans(spec_id)
             if plan.plan_id == plan_id),
            None,
        )

    def _measurement_plan_selected(self) -> None:
        plan = self._selected_measurement_plan()
        matches = self.measurement_match_list
        if matches is None:
            return
        matches.clear()
        if plan is None:
            if self.measurement_complete_button is not None:
                self.measurement_complete_button.setEnabled(False)
            return
        records = self.measurement_repository.list_measurements(plan.document_id)
        visible_index = 0
        for record in records:
            if (
                record.scene_revision_id != plan.applied_scene_revision_id
                or record.scene_content_hash != plan.applied_scene_content_hash
                or record.evidence_type != 'measured'
            ):
                continue
            visible_index += 1
            source_label = {
                'rew_text': 'REWテキスト',
                'rew_api': 'REW',
                'unknown': '取込データ',
            }.get(record.source_kind, '取込データ')
            role_label = record.channel_role or '役割未設定'
            item = QListWidgetItem(
                f'実測 {visible_index} · {role_label} · {source_label}'
            )
            item.setData(Qt.ItemDataRole.UserRole, record.measurement_id)
            matches.addItem(item)
            if record.measurement_id in plan.measurement_ids:
                item.setSelected(True)
        if self.measurement_complete_button is not None:
            self.measurement_complete_button.setEnabled(plan.status == 'planned' and matches.count() > 0)

    def complete_selected_measurement_plan(self) -> None:
        plan = self._selected_measurement_plan()
        matches = self.measurement_match_list
        if plan is None or matches is None:
            return
        measurement_ids = tuple(
            str(item.data(Qt.ItemDataRole.UserRole))
            for item in matches.selectedItems()
        )
        if not measurement_ids:
            self.statusBar().showMessage('関連付ける実測を選択してください')
            return
        try:
            completed = complete_measurement_plan(
                plan,
                self.measurement_repository,
                measurement_ids,
            )
            self.measurement_repository.save_measurement_plan(completed)
        except Exception as exc:
            self.statusBar().showMessage(f'実測を関連付けできません · {operation_error_message(exc)}')
            return
        self.refresh_measurement_plans()
        self.refresh_validation_campaigns()
        self.statusBar().showMessage(
            f'実測候補をcompletedにしました · measured evidence {len(completed.measurement_ids)}件'
        )

