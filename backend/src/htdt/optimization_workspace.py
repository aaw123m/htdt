from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from uuid import uuid4

import numpy as np
import pyvista as pv
from PySide6.QtCore import QSignalBlocker, QThread, Qt
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
from .cad_adaptive_extended_repository import CadAdaptiveExtendedRepository
from .cad_adaptive_extended_service import CadAdaptiveExtendedPlannerService
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
from .developer_mode import developer_mode_enabled
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
from .native_worker import NativeWorker, NativeWorkerPool
from .prediction_workspace import PredictionWorkspaceWindow

from .optimization_adaptive_controller import AdaptiveControllerMixin
from .optimization_adaptive_extended_controller import AdaptiveExtendedControllerMixin
from .optimization_extended_controller import ExtendedSearchControllerMixin
from .optimization_measurement_controller import MeasurementPlanControllerMixin
from .optimization_search_controller import SearchControllerMixin, candidate_cloud_points
from .optimization_validation_controller import ValidationControllerMixin
from .user_facing_error import operation_error_message
class OptimizationWorkspaceWindow(
    ValidationControllerMixin,
    MeasurementPlanControllerMixin,
    AdaptiveControllerMixin,
    AdaptiveExtendedControllerMixin,
    ExtendedSearchControllerMixin,
    SearchControllerMixin,
    PredictionWorkspaceWindow,
):
    """N80a product layer: immutable SearchSpec, candidate preview and explicit apply."""

    search_page_limit = 250

    def __init__(self, repository: SceneRepository, document_id: str = F1_DOCUMENT_ID) -> None:
        self.search_repository = CadSearchRepository(repository)
        self.measurement_repository = CadMeasurementRepository(repository)
        self.roomsim_repository = CadRoomSimRepository(repository, self.search_repository)
        self.objective_repository = CadObjectiveRepository(
            repository,
            self.search_repository,
            measurement_repository=self.measurement_repository,
            roomsim_repository=self.roomsim_repository,
        )
        self.validation_repository = CadModelValidationRepository(
            self.search_repository,
            self.roomsim_repository,
            self.measurement_repository,
            self.objective_repository,
        )
        self.validation_service = CadModelValidationService(
            self.search_repository,
            self.roomsim_repository,
            self.measurement_repository,
            self.objective_repository,
        )
        self.adaptive_repository = CadAdaptivePlanRepository(
            self.search_repository,
            self.validation_repository,
            self.objective_repository,
        )
        self.adaptive_service = CadAdaptivePlannerService(
            self.search_repository,
            self.objective_repository,
            self.validation_repository,
            self.adaptive_repository,
        )
        self.extended_repository = CadExtendedSearchRepository(
            self.search_repository,
            self.validation_repository,
        )
        self.adaptive_extended_repository = CadAdaptiveExtendedRepository(
            self.extended_repository,
            self.validation_repository,
            self.objective_repository,
        )
        self.adaptive_extended_service = CadAdaptiveExtendedPlannerService(
            self.extended_repository,
            self.validation_repository,
            self.adaptive_extended_repository,
        )
        self.campaign_repository = CadValidationCampaignRepository(
            self.search_repository,
            self.measurement_repository,
        )
        self.campaign_service = CadValidationCampaignService(
            self.campaign_repository,
            self.roomsim_repository,
            self.measurement_repository,
            self.objective_repository,
            self.validation_service,
        )
        self.search_selected_spec_id: str | None = None
        self.search_selected_candidate_id: str | None = None
        self.search_preview_candidate_id: str | None = None
        self.search_candidate_page: CadCandidateSetPage | None = None
        self.search_binding_label: QLabel | None = None
        self.search_name_field: QLineEdit | None = None
        self.search_entity_combo: QComboBox | None = None
        self.search_axis_combo: QComboBox | None = None
        self.search_min_field: QDoubleSpinBox | None = None
        self.search_max_field: QDoubleSpinBox | None = None
        self.search_step_field: QDoubleSpinBox | None = None
        self.search_limit_field: QSpinBox | None = None
        self.search_axis_tree: QTreeWidget | None = None
        self.search_linked_tree: QTreeWidget | None = None
        self.linked_master_combo: QComboBox | None = None
        self.linked_slave_combo: QComboBox | None = None
        self.linked_relation_combo: QComboBox | None = None
        self.linked_mirror_field: QDoubleSpinBox | None = None
        self.search_spec_tree: QTreeWidget | None = None
        self.search_candidate_tree: QTreeWidget | None = None
        self.search_summary_label: QLabel | None = None
        self.search_save_button: QPushButton | None = None
        self.search_generate_button: QPushButton | None = None
        self.search_cancel_button: QPushButton | None = None
        self.search_reauthor_button: QPushButton | None = None
        self.search_generate_reason_label: QLabel | None = None
        self.search_candidate_filter_field: QLineEdit | None = None
        self.search_prev_button: QPushButton | None = None
        self.search_next_button: QPushButton | None = None
        self.search_preview_button: QPushButton | None = None
        self.search_clear_preview_button: QPushButton | None = None
        self.search_apply_button: QPushButton | None = None
        # Created only by OptimizationWorkflowWorkspace's search dock; the
        # shared SearchControllerMixin refresh guards on them being None here.
        self.search_reauthor_button: QPushButton | None = None
        self.search_generate_reason_label: QLabel | None = None
        self.measurement_plan_button: QPushButton | None = None
        self.measurement_plan_label: QLabel | None = None
        self.measurement_plan_tree: QTreeWidget | None = None
        self.measurement_match_list: QListWidget | None = None
        self.measurement_complete_button: QPushButton | None = None
        self.objective_list: QListWidget | None = None
        self.pareto_tree: QTreeWidget | None = None
        self.pareto_summary_label: QLabel | None = None
        self.pareto_refresh_button: QPushButton | None = None
        self.validation_tree: QTreeWidget | None = None
        self.validation_detail_label: QLabel | None = None
        self.validation_refresh_button: QPushButton | None = None
        self.adaptive_scope_combo: QComboBox | None = None
        self.adaptive_length_scale_field: QDoubleSpinBox | None = None
        self.adaptive_proposal_limit_field: QSpinBox | None = None
        self.adaptive_build_button: QPushButton | None = None
        self.adaptive_tree: QTreeWidget | None = None
        self.adaptive_detail_label: QLabel | None = None
        self.adaptive_extended_length_scale_field: QDoubleSpinBox | None = None
        self.adaptive_extended_proposal_limit_field: QSpinBox | None = None
        self.adaptive_extended_build_button: QPushButton | None = None
        self.adaptive_extended_tree: QTreeWidget | None = None
        self.adaptive_extended_detail_label: QLabel | None = None
        self.extended_capability_combo: QComboBox | None = None
        self.extended_entity_combo: QComboBox | None = None
        self.extended_parameter_combo: QComboBox | None = None
        self.extended_min_field: QDoubleSpinBox | None = None
        self.extended_max_field: QDoubleSpinBox | None = None
        self.extended_step_field: QDoubleSpinBox | None = None
        self.extended_limit_field: QSpinBox | None = None
        self.extended_axis_tree: QTreeWidget | None = None
        self.extended_axes: dict[tuple[str, str], CadExtendedSearchAxis] = {}
        self.extended_spec_tree: QTreeWidget | None = None
        self.extended_candidate_tree: QTreeWidget | None = None
        self.extended_summary_label: QLabel | None = None
        self.extended_generate_button: QPushButton | None = None
        self.extended_cancel_button: QPushButton | None = None
        self.extended_prev_button: QPushButton | None = None
        self.extended_next_button: QPushButton | None = None
        self.extended_preview_button: QPushButton | None = None
        self.extended_clear_preview_button: QPushButton | None = None
        self.extended_apply_button: QPushButton | None = None
        self.extended_selected_spec_id: str | None = None
        self.extended_selected_candidate_id: str | None = None
        self.extended_preview_candidate_id: str | None = None
        self.extended_candidate_page: CadExtendedCandidateSetPage | None = None
        self.campaign_assignment_tree: QTreeWidget | None = None
        self.campaign_tree: QTreeWidget | None = None
        self.campaign_detail_label: QLabel | None = None
        self.campaign_model_version_field: QLineEdit | None = None
        self.campaign_low_field: QDoubleSpinBox | None = None
        self.campaign_high_field: QDoubleSpinBox | None = None
        self.campaign_residual_field: QDoubleSpinBox | None = None
        self.campaign_sensitivity_field: QDoubleSpinBox | None = None
        self.campaign_sensitivity_error_field: QDoubleSpinBox | None = None
        self.campaign_separation_field: QDoubleSpinBox | None = None
        self.campaign_applicability_state: dict[str, QComboBox] = {}
        self.campaign_applicability_detail: dict[str, QLineEdit] = {}
        self.campaign_assignments: dict[str, str] = {}
        self._search_actor_names: set[str] = set()
        # Parentless until self is a QObject; reparented right after super().
        self._search_pool = NativeWorkerPool()
        self._search_task_spec_ids: dict[str, str] = {}
        self._current_search_task_id: str | None = None
        self._extended_actor_names: set[str] = set()
        self._extended_pool = NativeWorkerPool()
        self._extended_task_spec_ids: dict[str, tuple[str, str]] = {}
        self._current_extended_task_id: str | None = None
        super().__init__(repository, document_id)
        self._search_pool.setParent(self)
        self._extended_pool.setParent(self)
        self.setWindowTitle('Home Theater Digital Twin — 最適化CAD')
        self._create_search_dock()
        self._refresh_search_entities()
        self._refresh_search_specs()
        self._refresh_extended_entities()
        self._refresh_extended_capabilities()
        self._refresh_extended_specs()
        self.refresh_adaptive_extended_plans()

    def _create_search_dock(self) -> None:
        panel = QWidget()
        layout = QVBoxLayout(panel)

        self.search_binding_label = QLabel('保存済みSceneRevisionから探索仕様を作成します')
        self.search_binding_label.setWordWrap(True)
        layout.addWidget(self.search_binding_label)

        semantics = QLabel(
            '候補はhard constraintを満たす幾何配置です。順位・推奨・音質評価ではありません。'
        )
        semantics.setWordWrap(True)
        layout.addWidget(semantics)

        form = QFormLayout()
        self.search_name_field = QLineEdit()
        self.search_name_field.setPlaceholderText('例: FL 前後 sweep')
        form.addRow('名前', self.search_name_field)

        self.search_entity_combo = QComboBox()
        self.search_entity_combo.setMinimumContentsLength(18)
        self.search_entity_combo.currentIndexChanged.connect(self._seed_search_axis_range)
        form.addRow('可動物体', self.search_entity_combo)

        self.search_axis_combo = QComboBox()
        self.search_axis_combo.addItem('X', 'x')
        self.search_axis_combo.addItem('Y', 'y')
        self.search_axis_combo.addItem('Z', 'z')
        self.search_axis_combo.currentIndexChanged.connect(self._seed_search_axis_range)
        form.addRow('軸', self.search_axis_combo)

        self.search_min_field = self._search_distance_field()
        self.search_max_field = self._search_distance_field()
        self.search_step_field = self._search_distance_field(minimum=0.001, value=0.10)
        form.addRow('最小', self.search_min_field)
        form.addRow('最大', self.search_max_field)
        form.addRow('刻み', self.search_step_field)

        self.search_limit_field = QSpinBox()
        self.search_limit_field.setRange(1, 50_000)
        self.search_limit_field.setValue(10_000)
        form.addRow('raw候補上限', self.search_limit_field)
        layout.addLayout(form)

        axis_actions = QHBoxLayout()
        add_axis = QPushButton('軸を追加 / 更新')
        add_axis.clicked.connect(self.add_search_axis)
        axis_actions.addWidget(add_axis)
        remove_axis = QPushButton('選択軸を削除')
        remove_axis.clicked.connect(self.remove_selected_search_axis)
        axis_actions.addWidget(remove_axis)
        layout.addLayout(axis_actions)

        self.search_axis_tree = QTreeWidget()
        self.search_axis_tree.setHeaderLabels(['物体', '軸', '最小', '最大', '刻み'])
        self.search_axis_tree.setMinimumHeight(120)
        layout.addWidget(self.search_axis_tree)

        linked_label = QLabel(
            '連動探索変数: 1つの可動軸が従属物体を派生します。'
            'mirror_x は部屋中心線を仮定せず鏡面xを必須指定します。'
        )
        linked_label.setWordWrap(True)
        layout.addWidget(linked_label)

        linked_form = QFormLayout()
        self.linked_master_combo = QComboBox()
        self.linked_master_combo.setMinimumContentsLength(18)
        linked_form.addRow('マスター', self.linked_master_combo)
        self.linked_slave_combo = QComboBox()
        self.linked_slave_combo.setMinimumContentsLength(18)
        linked_form.addRow('スレーブ', self.linked_slave_combo)
        self.linked_relation_combo = QComboBox()
        for label, value in (
            ('鏡像 X', 'mirror_x'),
            ('X一致', 'equal_x'),
            ('Y一致', 'equal_y'),
            ('Z一致', 'equal_z'),
            ('X同一変位', 'equal_delta_x'),
            ('Y同一変位', 'equal_delta_y'),
            ('Z同一変位', 'equal_delta_z'),
        ):
            self.linked_relation_combo.addItem(label, value)
        linked_form.addRow('関係', self.linked_relation_combo)
        self.linked_mirror_field = self._search_distance_field(minimum=0.0)
        linked_form.addRow('鏡面 x', self.linked_mirror_field)
        layout.addLayout(linked_form)

        linked_actions = QHBoxLayout()
        add_link = QPushButton('連動を追加 / 更新')
        add_link.clicked.connect(self.add_linked_search_variable)
        linked_actions.addWidget(add_link)
        remove_link = QPushButton('選択連動を削除')
        remove_link.clicked.connect(self.remove_selected_linked_variable)
        linked_actions.addWidget(remove_link)
        layout.addLayout(linked_actions)

        self.search_linked_tree = QTreeWidget()
        self.search_linked_tree.setHeaderLabels(['マスター', 'スレーブ', '関係', '鏡面x'])
        self.search_linked_tree.setMinimumHeight(90)
        layout.addWidget(self.search_linked_tree)

        self.search_save_button = QPushButton('探索仕様を保存')
        self.search_save_button.setToolTip(
            '現在の保存済みSceneRevisionとconstraint workspace hashへimmutable bindingします'
        )
        self.search_save_button.clicked.connect(self.save_search_spec)
        layout.addWidget(self.search_save_button)

        self.search_spec_tree = QTreeWidget()
        self.search_spec_tree.setHeaderLabels(['探索仕様', '入力版', '状態'])
        self.search_spec_tree.setMinimumHeight(150)
        self.search_spec_tree.itemSelectionChanged.connect(self._search_spec_selected)
        layout.addWidget(self.search_spec_tree)

        generation = QHBoxLayout()
        self.search_generate_button = QPushButton('候補を生成')
        self.search_generate_button.clicked.connect(self.generate_search_candidates_async)
        generation.addWidget(self.search_generate_button)
        self.search_cancel_button = QPushButton('生成をキャンセル')
        self.search_cancel_button.clicked.connect(self.cancel_search_generation)
        generation.addWidget(self.search_cancel_button)
        layout.addLayout(generation)

        paging = QHBoxLayout()
        self.search_prev_button = QPushButton('前の候補')
        self.search_prev_button.clicked.connect(self.previous_search_page)
        paging.addWidget(self.search_prev_button)
        self.search_next_button = QPushButton('次の候補')
        self.search_next_button.clicked.connect(self.next_search_page)
        paging.addWidget(self.search_next_button)
        layout.addLayout(paging)

        self.search_summary_label = QLabel('候補未生成')
        self.search_summary_label.setWordWrap(True)
        layout.addWidget(self.search_summary_label)

        self.search_candidate_tree = QTreeWidget()
        self.search_candidate_tree.setHeaderLabels(['候補', 'index', '位置'])
        self.search_candidate_tree.setMinimumHeight(180)
        self.search_candidate_tree.itemSelectionChanged.connect(self._search_candidate_selected)
        layout.addWidget(self.search_candidate_tree)

        candidate_actions = QHBoxLayout()
        self.search_preview_button = QPushButton('候補をpreview')
        self.search_preview_button.clicked.connect(self.preview_selected_candidate)
        candidate_actions.addWidget(self.search_preview_button)
        self.search_clear_preview_button = QPushButton('preview解除')
        self.search_clear_preview_button.clicked.connect(self.clear_candidate_preview)
        candidate_actions.addWidget(self.search_clear_preview_button)
        self.search_apply_button = QPushButton('候補を適用')
        self.search_apply_button.setToolTip('明示適用だけがSceneを変更し、1回のUndoで全位置を戻します')
        self.search_apply_button.clicked.connect(self.apply_selected_candidate)
        candidate_actions.addWidget(self.search_apply_button)
        layout.addLayout(candidate_actions)

        self.measurement_plan_button = QPushButton('現在の保存版を実測候補として記録')
        self.measurement_plan_button.setToolTip('候補適用後にSceneを保存してから、候補とその正確なSceneRevisionをimmutableに結びます')
        self.measurement_plan_button.clicked.connect(self.create_measurement_plan_for_selected_candidate)
        layout.addWidget(self.measurement_plan_button)
        self.measurement_plan_label = QLabel('実測候補未登録')
        self.measurement_plan_label.setWordWrap(True)
        layout.addWidget(self.measurement_plan_label)

        self.measurement_plan_tree = QTreeWidget()
        self.measurement_plan_tree.setHeaderLabels(['実測候補', '状態', 'シーン', '測定'])
        self.measurement_plan_tree.setMinimumHeight(120)
        self.measurement_plan_tree.itemSelectionChanged.connect(self._measurement_plan_selected)
        layout.addWidget(self.measurement_plan_tree)

        self.measurement_match_list = QListWidget()
        self.measurement_match_list.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        self.measurement_match_list.setMinimumHeight(90)
        layout.addWidget(self.measurement_match_list)

        self.measurement_complete_button = QPushButton('選択したN60実測を候補へ関連付け')
        self.measurement_complete_button.setToolTip(
            '候補適用時と完全一致するSceneRevision/content hashのmeasured evidenceだけを関連付けます'
        )
        self.measurement_complete_button.clicked.connect(self.complete_selected_measurement_plan)
        layout.addWidget(self.measurement_complete_button)

        comparison_label = QLabel('Pareto比較 · objectiveは独立指標のまま保持します')
        comparison_label.setWordWrap(True)
        layout.addWidget(comparison_label)

        self.objective_list = QListWidget()
        self.objective_list.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        self.objective_list.setMinimumHeight(100)
        layout.addWidget(self.objective_list)

        self.pareto_refresh_button = QPushButton('Pareto集合を再計算・保存')
        self.pareto_refresh_button.clicked.connect(self.refresh_pareto_comparison)
        layout.addWidget(self.pareto_refresh_button)

        self.pareto_summary_label = QLabel('objective evaluation未読込')
        self.pareto_summary_label.setWordWrap(True)
        layout.addWidget(self.pareto_summary_label)

        self.pareto_tree = QTreeWidget()
        self.pareto_tree.setHeaderLabels(['候補', 'Pareto', '根拠', '指標'])
        self.pareto_tree.setMinimumHeight(180)
        self.pareto_tree.itemSelectionChanged.connect(self._pareto_candidate_selected)
        layout.addWidget(self.pareto_tree)

        campaign_label = QLabel(
            '実室Validation Campaign · 測定前にcalibration/holdoutと閾値を固定'
        )
        campaign_label.setWordWrap(True)
        layout.addWidget(campaign_label)

        assignment_actions = QHBoxLayout()
        campaign_calibration = QPushButton('選択候補→calibration')
        campaign_calibration.clicked.connect(
            lambda: self.assign_selected_candidate_to_campaign('calibration')
        )
        assignment_actions.addWidget(campaign_calibration)
        campaign_holdout = QPushButton('選択候補→holdout')
        campaign_holdout.clicked.connect(
            lambda: self.assign_selected_candidate_to_campaign('holdout')
        )
        assignment_actions.addWidget(campaign_holdout)
        campaign_remove = QPushButton('campaignから外す')
        campaign_remove.clicked.connect(self.remove_selected_campaign_assignment)
        assignment_actions.addWidget(campaign_remove)
        layout.addLayout(assignment_actions)

        self.campaign_assignment_tree = QTreeWidget()
        self.campaign_assignment_tree.setHeaderLabels(['候補', '役割'])
        self.campaign_assignment_tree.setMinimumHeight(100)
        layout.addWidget(self.campaign_assignment_tree)

        campaign_form = QFormLayout()
        self.campaign_model_version_field = QLineEdit()
        self.campaign_model_version_field.setPlaceholderText('例: 5.40 Beta 135 API 0.9.8')
        campaign_form.addRow('モデルバージョン', self.campaign_model_version_field)

        self.campaign_low_field = QDoubleSpinBox()
        self.campaign_low_field.setRange(1.0, 20000.0)
        self.campaign_low_field.setDecimals(1)
        self.campaign_low_field.setValue(20.0)
        campaign_form.addRow('検証帯域 low Hz', self.campaign_low_field)

        self.campaign_high_field = QDoubleSpinBox()
        self.campaign_high_field.setRange(1.0, 20000.0)
        self.campaign_high_field.setDecimals(1)
        self.campaign_high_field.setValue(160.0)
        campaign_form.addRow('検証帯域 high Hz', self.campaign_high_field)

        self.campaign_residual_field = QDoubleSpinBox()
        self.campaign_residual_field.setRange(0.0, 100.0)
        self.campaign_residual_field.setDecimals(2)
        self.campaign_residual_field.setValue(0.0)
        self.campaign_residual_field.setSpecialValueText('要設定')
        campaign_form.addRow('holdout RMS上限 dB', self.campaign_residual_field)

        self.campaign_sensitivity_field = QDoubleSpinBox()
        self.campaign_sensitivity_field.setRange(0.0, 1000.0)
        self.campaign_sensitivity_field.setDecimals(2)
        self.campaign_sensitivity_field.setValue(0.0)
        self.campaign_sensitivity_field.setSpecialValueText('要設定')
        campaign_form.addRow('感度上限 dB/m', self.campaign_sensitivity_field)

        self.campaign_sensitivity_error_field = QDoubleSpinBox()
        self.campaign_sensitivity_error_field.setRange(0.0, 1000.0)
        self.campaign_sensitivity_error_field.setDecimals(2)
        self.campaign_sensitivity_error_field.setValue(0.0)
        self.campaign_sensitivity_error_field.setSpecialValueText('要設定')
        campaign_form.addRow('感度誤差上限 dB/m', self.campaign_sensitivity_error_field)

        self.campaign_separation_field = QDoubleSpinBox()
        self.campaign_separation_field.setRange(0.0, 100.0)
        self.campaign_separation_field.setDecimals(2)
        self.campaign_separation_field.setValue(0.0)
        self.campaign_separation_field.setSpecialValueText('要設定')
        campaign_form.addRow('候補差 / repeatability', self.campaign_separation_field)
        layout.addLayout(campaign_form)

        campaign_save = QPushButton('Campaignを測定前にimmutable保存')
        campaign_save.setToolTip(
            'holdoutを実測結果から選び直せないよう、この時点の役割・target・閾値を固定します'
        )
        campaign_save.clicked.connect(self.save_validation_campaign)
        layout.addWidget(campaign_save)

        self.campaign_tree = QTreeWidget()
        self.campaign_tree.setHeaderLabels(['campaign', 'model', '候補', 'readiness'])
        self.campaign_tree.setMinimumHeight(130)
        self.campaign_tree.itemSelectionChanged.connect(self._campaign_selected)
        layout.addWidget(self.campaign_tree)

        campaign_actions = QHBoxLayout()
        campaign_refresh = QPushButton('readiness更新')
        campaign_refresh.clicked.connect(self.refresh_validation_campaigns)
        campaign_actions.addWidget(campaign_refresh)
        campaign_materialize = QPushButton('O30 objective evidence生成')
        campaign_materialize.clicked.connect(self.materialize_selected_campaign_objectives)
        campaign_actions.addWidget(campaign_materialize)
        campaign_rew_read = QPushButton('選択REW→Campaign実測')
        campaign_rew_read.setToolTip(
            '選択中のCampaign・planned Measurement Plan・現在SceneRevisionが一致する場合だけ、'
            'REW API測定をowned-room campaign evidenceとして読み込みます'
        )
        campaign_rew_read.clicked.connect(self.read_selected_rew_for_campaign_async)
        campaign_actions.addWidget(campaign_rew_read)
        layout.addLayout(campaign_actions)

        self.campaign_detail_label = QLabel('Campaign未選択')
        self.campaign_detail_label.setWordWrap(True)
        layout.addWidget(self.campaign_detail_label)

        applicability_label = QLabel(
            '適用条件 · 自動評価は保存済みの証拠権威から判定を再計算します。'
            '手動証跡は登録済みattestation IDを根拠欄に入力します。'
            '未確認はgateを開きません'
        )
        applicability_label.setWordWrap(True)
        layout.addWidget(applicability_label)

        applicability_form = QFormLayout()
        for code, label_text in (
            ('geometry', 'geometry'),
            ('band', 'band'),
            ('routing', 'routing'),
        ):
            state = QComboBox()
            state.addItem('未確認', 'unverified')
            state.addItem('自動評価', 'auto')
            state.addItem('手動証跡', 'manual')
            self.campaign_applicability_state[code] = state
            applicability_form.addRow(f'{label_text} 判定', state)

            detail = QLineEdit()
            detail.setPlaceholderText('確認メモ / attestation ID')
            self.campaign_applicability_detail[code] = detail
            applicability_form.addRow(f'{label_text} 根拠', detail)
        layout.addLayout(applicability_form)

        campaign_build_validation = QPushButton('CampaignからValidationRecordを構築・保存')
        campaign_build_validation.setToolTip(
            'readinessが揃ったcampaignだけをO60検証し、applicabilityを含むimmutable recordとして保存します'
        )
        campaign_build_validation.clicked.connect(
            self.build_and_save_selected_campaign_validation
        )
        layout.addWidget(campaign_build_validation)

        validation_label = QLabel(
            'モデル検証 · residual / trend / sensitivity / repeatabilityを独立表示'
        )
        validation_label.setWordWrap(True)
        layout.addWidget(validation_label)

        self.validation_refresh_button = QPushButton('保存済みValidationRecordを更新')
        self.validation_refresh_button.clicked.connect(self.refresh_model_validations)
        layout.addWidget(self.validation_refresh_button)

        self.validation_tree = QTreeWidget()
        self.validation_tree.setHeaderLabels([
            'validation', 'scope', 'residual', 'trend', 'sensitivity', 'repeatability', 'gate'
        ])
        self.validation_tree.setMinimumHeight(150)
        self.validation_tree.itemSelectionChanged.connect(self._validation_selected)
        layout.addWidget(self.validation_tree)

        self.validation_detail_label = QLabel('ValidationRecord未選択')
        self.validation_detail_label.setWordWrap(True)
        layout.addWidget(self.validation_detail_label)

        adaptive_label = QLabel(
            'Adaptive Planner · synthetic開発とowned-room本番を明示分離します'
        )
        adaptive_label.setWordWrap(True)
        layout.addWidget(adaptive_label)

        adaptive_form = QFormLayout()
        self.adaptive_scope_combo = QComboBox()
        # #901: production owned-room first and only by default; the
        # synthetic development lane is developer-mode-only.
        self.adaptive_scope_combo.addItem(
            '実部屋 production',
            'production_owned_room',
        )
        if developer_mode_enabled():
            self.adaptive_scope_combo.addItem(
                '合成 development',
                'development_synthetic',
            )
        adaptive_form.addRow('実行scope', self.adaptive_scope_combo)

        self.adaptive_length_scale_field = QDoubleSpinBox()
        self.adaptive_length_scale_field.setRange(0.01, 20.0)
        self.adaptive_length_scale_field.setDecimals(3)
        self.adaptive_length_scale_field.setSingleStep(0.05)
        self.adaptive_length_scale_field.setValue(0.5)
        self.adaptive_length_scale_field.setSuffix(' m')
        adaptive_form.addRow('GP長さスケール', self.adaptive_length_scale_field)

        self.adaptive_proposal_limit_field = QSpinBox()
        self.adaptive_proposal_limit_field.setRange(1, 100)
        self.adaptive_proposal_limit_field.setValue(20)
        adaptive_form.addRow('表示proposal上限', self.adaptive_proposal_limit_field)
        layout.addLayout(adaptive_form)

        self.adaptive_build_button = QPushButton('次の測定候補を計算・immutable保存')
        self.adaptive_build_button.setToolTip(
            'synthetic scopeは開発検証専用です。owned-room productionは'
            'current campaign-backed eligible O60 ValidationRecordだけを受け付けます'
        )
        self.adaptive_build_button.clicked.connect(self.build_selected_adaptive_plan)
        layout.addWidget(self.adaptive_build_button)

        self.adaptive_tree = QTreeWidget()
        self.adaptive_tree.setHeaderLabels([
            'プラン / 候補', 'スコープ', '獲得', '補正objective'
        ])
        self.adaptive_tree.setMinimumHeight(180)
        self.adaptive_tree.itemSelectionChanged.connect(self._adaptive_selected)
        layout.addWidget(self.adaptive_tree)

        self.adaptive_detail_label = QLabel('Adaptive Plan未選択')
        self.adaptive_detail_label.setWordWrap(True)
        layout.addWidget(self.adaptive_detail_label)

        extended_label = QLabel(
            'Extended Search · acoustic aim / physical cabinet toe-inは明示model capabilityがある場合だけ探索します'
        )
        extended_label.setWordWrap(True)
        layout.addWidget(extended_label)

        capability_actions = QHBoxLayout()
        self.extended_capability_combo = QComboBox()
        self.extended_capability_combo.setMinimumContentsLength(24)
        capability_actions.addWidget(self.extended_capability_combo)
        synthetic_capability = QPushButton('Synthetic capability作成')
        synthetic_capability.setToolTip(
            'ソフトウェア受入専用のdirectional model capabilityです。'
            'owned-room validationへ昇格しません'
        )
        synthetic_capability.clicked.connect(
            self.create_synthetic_extended_capability
        )
        capability_actions.addWidget(synthetic_capability)
        owned_capability = QPushButton('選択O60→本番capability')
        owned_capability.setToolTip(
            'owned-room eligible ValidationRecordがspeaker directionを扱うmodelの場合だけ保存できます。'
            'REW Room Simulatorはdirectional aim/body yaw非対応なので拒否されます'
        )
        owned_capability.clicked.connect(
            self.create_owned_room_extended_capability
        )
        capability_actions.addWidget(owned_capability)
        layout.addLayout(capability_actions)

        extended_form = QFormLayout()
        self.extended_parameter_combo = QComboBox()
        self.extended_parameter_combo.addItem(
            '音響照準 yaw',
            'aim_yaw_deg',
        )
        self.extended_parameter_combo.addItem(
            '音響照準 pitch',
            'aim_pitch_deg',
        )
        self.extended_parameter_combo.addItem(
            '物理キャビネット toe-in (ボディyaw)',
            'body_yaw_deg',
        )
        self.extended_parameter_combo.currentIndexChanged.connect(
            self._seed_extended_aim_range
        )
        extended_form.addRow('parameter', self.extended_parameter_combo)

        self.extended_entity_combo = QComboBox()
        self.extended_entity_combo.currentIndexChanged.connect(
            self._seed_extended_aim_range
        )
        extended_form.addRow('speaker', self.extended_entity_combo)

        self.extended_min_field = QDoubleSpinBox()
        self.extended_min_field.setRange(-180.0, 180.0)
        self.extended_min_field.setDecimals(1)
        self.extended_min_field.setSingleStep(1.0)
        self.extended_min_field.setSuffix('°')
        extended_form.addRow('yaw最小', self.extended_min_field)

        self.extended_max_field = QDoubleSpinBox()
        self.extended_max_field.setRange(-180.0, 180.0)
        self.extended_max_field.setDecimals(1)
        self.extended_max_field.setSingleStep(1.0)
        self.extended_max_field.setSuffix('°')
        extended_form.addRow('yaw最大', self.extended_max_field)

        self.extended_step_field = QDoubleSpinBox()
        self.extended_step_field.setRange(0.1, 180.0)
        self.extended_step_field.setDecimals(1)
        self.extended_step_field.setSingleStep(1.0)
        self.extended_step_field.setValue(5.0)
        self.extended_step_field.setSuffix('°')
        extended_form.addRow('yaw刻み', self.extended_step_field)

        self.extended_limit_field = QSpinBox()
        self.extended_limit_field.setRange(1, 50_000)
        self.extended_limit_field.setValue(10_000)
        extended_form.addRow('extended候補上限', self.extended_limit_field)
        layout.addLayout(extended_form)

        extended_axis_actions = QHBoxLayout()
        add_extended_axis = QPushButton('extended軸を追加 / 更新')
        add_extended_axis.clicked.connect(self.add_or_update_extended_axis)
        extended_axis_actions.addWidget(add_extended_axis)
        remove_extended_axis = QPushButton('選択extended軸を削除')
        remove_extended_axis.clicked.connect(self.remove_selected_extended_axis)
        extended_axis_actions.addWidget(remove_extended_axis)
        layout.addLayout(extended_axis_actions)

        self.extended_axis_tree = QTreeWidget()
        self.extended_axis_tree.setHeaderLabels([
            'speaker', 'parameter', '最小', '最大', '刻み'
        ])
        self.extended_axis_tree.setMinimumHeight(105)
        layout.addWidget(self.extended_axis_tree)

        save_extended = QPushButton('Extended SearchSpecをimmutable保存')
        save_extended.clicked.connect(self.save_extended_search_spec)
        layout.addWidget(save_extended)

        self.extended_spec_tree = QTreeWidget()
        self.extended_spec_tree.setHeaderLabels([
            'extended', 'model', 'parameter', '状態'
        ])
        self.extended_spec_tree.setMinimumHeight(120)
        self.extended_spec_tree.itemSelectionChanged.connect(
            self._extended_spec_selected
        )
        layout.addWidget(self.extended_spec_tree)

        extended_generation = QHBoxLayout()
        self.extended_generate_button = QPushButton('extended候補を生成')
        self.extended_generate_button.clicked.connect(
            self.generate_extended_candidates_async
        )
        extended_generation.addWidget(self.extended_generate_button)
        self.extended_cancel_button = QPushButton('生成をキャンセル')
        self.extended_cancel_button.clicked.connect(
            self.cancel_extended_generation
        )
        extended_generation.addWidget(self.extended_cancel_button)
        layout.addLayout(extended_generation)

        extended_paging = QHBoxLayout()
        self.extended_prev_button = QPushButton('前のextended候補')
        self.extended_prev_button.clicked.connect(self.previous_extended_page)
        extended_paging.addWidget(self.extended_prev_button)
        self.extended_next_button = QPushButton('次のextended候補')
        self.extended_next_button.clicked.connect(self.next_extended_page)
        extended_paging.addWidget(self.extended_next_button)
        layout.addLayout(extended_paging)

        self.extended_summary_label = QLabel('Extended候補未生成')
        self.extended_summary_label.setWordWrap(True)
        layout.addWidget(self.extended_summary_label)

        self.extended_candidate_tree = QTreeWidget()
        self.extended_candidate_tree.setHeaderLabels([
            '候補', 'ベース', '位置', '音響yaw', 'ボディyaw'
        ])
        self.extended_candidate_tree.setMinimumHeight(170)
        self.extended_candidate_tree.itemSelectionChanged.connect(
            self._extended_candidate_selected
        )
        layout.addWidget(self.extended_candidate_tree)

        extended_candidate_actions = QHBoxLayout()
        self.extended_preview_button = QPushButton('extended候補をpreview')
        self.extended_preview_button.clicked.connect(
            self.preview_selected_extended_candidate
        )
        extended_candidate_actions.addWidget(self.extended_preview_button)
        self.extended_clear_preview_button = QPushButton('preview解除')
        self.extended_clear_preview_button.clicked.connect(
            self.clear_extended_preview
        )
        extended_candidate_actions.addWidget(self.extended_clear_preview_button)
        self.extended_apply_button = QPushButton('extended候補を適用')
        self.extended_apply_button.setToolTip(
            '位置・body orientation・aimを1 commandで適用し、1回のUndoで復元します'
        )
        self.extended_apply_button.clicked.connect(
            self.apply_selected_extended_candidate
        )
        extended_candidate_actions.addWidget(self.extended_apply_button)
        layout.addLayout(extended_candidate_actions)

        adaptive_extended_label = QLabel(
            'Adaptive Extended · base XYZ + O80 parameterをscale正規化して'
            '次のextended測定候補を選びます'
        )
        adaptive_extended_label.setWordWrap(True)
        layout.addWidget(adaptive_extended_label)

        adaptive_extended_form = QFormLayout()
        self.adaptive_extended_length_scale_field = QDoubleSpinBox()
        self.adaptive_extended_length_scale_field.setRange(0.01, 20.0)
        self.adaptive_extended_length_scale_field.setDecimals(3)
        self.adaptive_extended_length_scale_field.setSingleStep(0.05)
        self.adaptive_extended_length_scale_field.setValue(0.5)
        adaptive_extended_form.addRow(
            '正規化GP長さスケール',
            self.adaptive_extended_length_scale_field,
        )
        self.adaptive_extended_proposal_limit_field = QSpinBox()
        self.adaptive_extended_proposal_limit_field.setRange(1, 100)
        self.adaptive_extended_proposal_limit_field.setValue(20)
        adaptive_extended_form.addRow(
            'extended proposal上限',
            self.adaptive_extended_proposal_limit_field,
        )
        layout.addLayout(adaptive_extended_form)

        self.adaptive_extended_build_button = QPushButton(
            'Adaptive Extended候補を計算・immutable保存'
        )
        self.adaptive_extended_build_button.setToolTip(
            '選択Extended SearchSpecにpersist済みobjective observationが必要です。'
            'productionはcurrent owned-room O60 + owned-room capabilityを要求します'
        )
        self.adaptive_extended_build_button.clicked.connect(
            self.build_selected_adaptive_extended_plan
        )
        layout.addWidget(self.adaptive_extended_build_button)

        self.adaptive_extended_tree = QTreeWidget()
        self.adaptive_extended_tree.setHeaderLabels([
            'プラン / 候補', 'スコープ', '獲得', '特徴 / 指標'
        ])
        self.adaptive_extended_tree.setMinimumHeight(180)
        self.adaptive_extended_tree.itemSelectionChanged.connect(
            self._adaptive_extended_selected
        )
        layout.addWidget(self.adaptive_extended_tree)

        self.adaptive_extended_detail_label = QLabel(
            'Adaptive Extended Plan未選択'
        )
        self.adaptive_extended_detail_label.setWordWrap(True)
        layout.addWidget(self.adaptive_extended_detail_label)

        layout.addStretch(1)

        dock = QDockWidget('最適化', self)
        scroll = _MeasurementScrollArea(panel, dock)
        dock.setWidget(scroll)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        self._unify_right_context_docks(dock)
        dock.raise_()

    def refresh_pareto_comparison(self) -> None:
        spec_id = self.search_selected_spec_id
        if spec_id is None or self.objective_list is None or self.pareto_tree is None:
            return
        spec = self.search_repository.get(spec_id)
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
            self.pareto_tree.clear()
            if self.pareto_summary_label is not None:
                self.pareto_summary_label.setText('staleなSearchSpecではPareto集合を更新できません')
            self.statusBar().showMessage('Pareto比較を拒否しました · SearchSpec/Scene/constraint authorityがstaleです')
            return

        # One shared authority memo for this refresh: the candidate listing,
        # the persisted-set resolution and the save-time replay each re-validate
        # the same immutable evaluations, so a sealed (id, sha) pair replays
        # its authority once instead of once per lane.
        validated: dict = {}
        evaluations = self.objective_repository.latest_evaluations_by_candidate(
            spec_id, validated=validated
        )
        if not evaluations:
            self.objective_list.clear()
            self.pareto_tree.clear()
            if self.pareto_summary_label is not None:
                self.pareto_summary_label.setText('この探索仕様にはobjective evaluationがありません')
            return

        available = tuple(metric.objective_id for metric in evaluations[0].vector.metrics)
        expected_ids = set(available)
        expected_units = {metric.objective_id: metric.unit for metric in evaluations[0].vector.metrics}
        for evaluation in evaluations[1:]:
            metric_map = {metric.objective_id: metric for metric in evaluation.vector.metrics}
            if set(metric_map) != expected_ids:
                self.pareto_tree.clear()
                self.pareto_summary_label.setText('objective集合が候補間で一致しません · Pareto比較を中止')
                self.statusBar().showMessage('Pareto比較を拒否しました · objective集合不一致')
                return
            if any(metric_map[objective_id].unit != expected_units[objective_id] for objective_id in available):
                self.pareto_tree.clear()
                self.pareto_summary_label.setText('objective単位が候補間で一致しません · Pareto比較を中止')
                self.statusBar().showMessage('Pareto比較を拒否しました · objective単位不一致')
                return

        previous = {item.data(Qt.ItemDataRole.UserRole) for item in self.objective_list.selectedItems()}
        self.objective_list.clear()
        for objective_id in available:
            item = QListWidgetItem(objective_id)
            item.setData(Qt.ItemDataRole.UserRole, objective_id)
            self.objective_list.addItem(item)
            if not previous or objective_id in previous:
                item.setSelected(True)
        selected = tuple(
            str(item.data(Qt.ItemDataRole.UserRole))
            for item in self.objective_list.selectedItems()
        )
        if not selected:
            selected = available

        try:
            built = build_pareto_set(evaluations, selected)
            existing = self.objective_repository.find_pareto_set_by_sha(
                spec_id, built.pareto_sha256, validated=validated
            )
            pareto_set = existing or built
            if existing is None:
                self.objective_repository.save_pareto_set(
                    pareto_set, validated=validated
                )
        except Exception as exc:
            self.pareto_tree.clear()
            if self.pareto_summary_label is not None:
                self.pareto_summary_label.setText(f'Pareto比較を作成できません · {operation_error_message(exc)}')
            self.statusBar().showMessage(f'Pareto比較を拒否しました · {operation_error_message(exc)}')
            return

        non_dominated = set(pareto_set.result.non_dominated_candidate_ids)
        self.pareto_tree.clear()
        for evaluation in evaluations:
            metric_map = {metric.objective_id: metric for metric in evaluation.vector.metrics}
            provenance = ', '.join(
                f'{ref.evidence_class}:{ref.source_kind}:{ref.source_id[:12]}'
                for ref in evaluation.input_refs
            )
            values = '; '.join(
                f'{objective_id}={metric_map[objective_id].value:.4g} {metric_map[objective_id].unit}'
                for objective_id in selected
            )
            item = QTreeWidgetItem([
                evaluation.candidate_id[:12],
                '非劣' if evaluation.candidate_id in non_dominated else '支配あり',
                provenance,
                values,
            ])
            item.setData(0, ROLE, evaluation.candidate_id)
            self.pareto_tree.addTopLevelItem(item)
        if self.pareto_summary_label is not None:
            reused = ' · 既存snapshot' if existing is not None else ''
            self.pareto_summary_label.setText(
                f'{len(evaluations)}候補 · 非劣 {len(non_dominated)} · '
                f'指標 {len(selected)} · {pareto_set.pareto_set_id[:8]}{reused}'
            )

    def _pareto_candidate_selected(self) -> None:
        if self.pareto_tree is None or self.search_candidate_tree is None:
            return
        item = self.pareto_tree.currentItem()
        if item is None:
            return
        candidate_id = item.data(0, ROLE)
        if not isinstance(candidate_id, str):
            return
        self.search_selected_candidate_id = candidate_id
        found = False
        for index in range(self.search_candidate_tree.topLevelItemCount()):
            candidate_item = self.search_candidate_tree.topLevelItem(index)
            payload = candidate_item.data(0, ROLE)
            if payload == candidate_id:
                self.search_candidate_tree.setCurrentItem(candidate_item)
                found = True
                break
        if not found:
            self.statusBar().showMessage(
                'Pareto候補は現在のcandidate page外です · candidate pageを移動してからpreview/applyしてください'
            )

    def _replace_constraint_set(self, constraints: tuple, *, message: str) -> bool:
        changed = super()._replace_constraint_set(constraints, message=message)
        if changed and self.search_spec_tree is not None:
            self.search_preview_candidate_id = None
            self.extended_preview_candidate_id = None
            self._refresh_search_specs()
            self._refresh_extended_specs()
            self._render_search_overlay()
            self._render_extended_overlay()
        return changed

    def save(self) -> None:
        super().save()
        if self.search_spec_tree is not None:
            self._refresh_search_entities()
            self._refresh_search_specs()
            self._refresh_extended_entities()
            self._refresh_extended_capabilities()
            self._refresh_extended_specs()
            self._render_search_overlay()
            self._render_extended_overlay()

    def _rebuild(self, *, reset_camera: bool = False) -> None:
        super()._rebuild(reset_camera=reset_camera)
        if self.search_spec_tree is not None:
            self._refresh_search_binding_state()
            self._refresh_extended_binding_state()
            self._render_search_overlay()
            self._render_extended_overlay()

    @property
    def _search_tasks(self) -> dict[str, tuple[QThread, NativeWorker]]:
        """Live search worker records owned by ``self._search_pool``."""
        return self._search_pool.tasks

    @property
    def _extended_tasks(self) -> dict[str, tuple[QThread, NativeWorker]]:
        """Live extended worker records owned by ``self._extended_pool``."""
        return self._extended_pool.tasks

    def active_search_worker_count(self) -> int:
        return self._search_pool.active_count

    def active_extended_worker_count(self) -> int:
        return self._extended_pool.active_count

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self._disposed = True
        reports = (
            self._search_pool.shutdown(),
            self._extended_pool.shutdown(),
        )
        if any(not report.all_stopped for report in reports):
            self.statusBar().showMessage(
                '候補生成の停止が遅延しています · 遅延結果は適用しません'
            )
        super().closeEvent(event)

