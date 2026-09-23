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


class AdaptiveControllerMixin:
    def build_selected_adaptive_plan(self) -> None:
        record = self._selected_validation_record()
        spec = self._selected_search_spec()
        if record is None or spec is None:
            self.statusBar().showMessage(
                'Adaptive PlannerにはSearchSpecとValidationRecordの選択が必要です'
            )
            return
        if (
            self.working is None
            or not search_spec_current_working(
                spec,
                self.working,
                self.constraint_set,
                current_document_id=self.document_id,
            )
        ):
            self.statusBar().showMessage(
                'staleなSearchSpec/Scene/constraintからAdaptive Planを作成できません'
            )
            return
        if record.search_spec_id != spec.search_spec_id:
            self.statusBar().showMessage(
                '選択ValidationRecordは現在のSearchSpecに属していません'
            )
            return

        scope = (
            'development_synthetic'
            if self.adaptive_scope_combo is None
            else str(self.adaptive_scope_combo.currentData())
        )
        length_scale = (
            0.5
            if self.adaptive_length_scale_field is None
            else float(self.adaptive_length_scale_field.value())
        )
        proposal_limit = (
            20
            if self.adaptive_proposal_limit_field is None
            else int(self.adaptive_proposal_limit_field.value())
        )
        try:
            plan = self.adaptive_service.build_and_save(
                validation_id=record.validation_id,
                execution_scope=scope,
                length_scale_m=length_scale,
                proposal_limit=proposal_limit,
            )
        except Exception as exc:
            self.statusBar().showMessage(f'Adaptive Planを作成できません · {exc}')
            return

        self.refresh_adaptive_plans(select_plan_id=plan.plan_id)
        mode = (
            'synthetic開発'
            if plan.execution_scope == 'development_synthetic'
            else 'owned-room本番'
        )
        self.statusBar().showMessage(
            f'O70 Adaptive Planを保存しました · {mode} · '
            f'次候補 {plan.selected_candidate_id[:12]}'
        )

    def refresh_adaptive_plans(
        self,
        *,
        select_plan_id: str | None = None,
    ) -> None:
        tree = self.adaptive_tree
        if tree is None:
            return
        tree.clear()
        if self.adaptive_detail_label is not None:
            self.adaptive_detail_label.setText('Adaptive Plan未選択')
        spec_id = self.search_selected_spec_id
        if spec_id is None:
            return
        try:
            plans = self.adaptive_repository.list_for_search_spec(spec_id)
        except Exception as exc:
            self.statusBar().showMessage(f'Adaptive Planを読めません · {exc}')
            return

        selected_item: QTreeWidgetItem | None = None
        for plan in reversed(plans):
            scope_text = (
                'synthetic'
                if plan.execution_scope == 'development_synthetic'
                else 'owned-room'
            )
            root = QTreeWidgetItem([
                f'plan {plan.plan_id[:8]}',
                scope_text,
                'selected ' + plan.selected_candidate_id[:12],
                ', '.join(plan.objective_ids),
            ])
            root.setData(0, ROLE, {'plan_id': plan.plan_id})
            tree.addTopLevelItem(root)
            if plan.plan_id == select_plan_id:
                selected_item = root
            for proposal in plan.proposals:
                objective_text = '; '.join(
                    f'{estimate.objective_id}: '
                    f'{estimate.corrected_mean:.4g}±{estimate.residual_uncertainty:.3g} '
                    f'{estimate.unit}'
                    for estimate in proposal.objectives
                )
                child = QTreeWidgetItem([
                    proposal.candidate_id[:12],
                    scope_text,
                    f'{proposal.acquisition_score:.4f}',
                    objective_text,
                ])
                child.setData(0, ROLE, {
                    'plan_id': plan.plan_id,
                    'candidate_id': proposal.candidate_id,
                })
                root.addChild(child)
            root.setExpanded(plan.plan_id == select_plan_id)

        if selected_item is None and tree.topLevelItemCount() > 0:
            selected_item = tree.topLevelItem(0)
        if selected_item is not None:
            tree.setCurrentItem(selected_item)
        else:
            self._adaptive_selected()

    def _adaptive_selected(self) -> None:
        tree = self.adaptive_tree
        label = self.adaptive_detail_label
        if tree is None or label is None:
            return
        item = tree.currentItem()
        payload = None if item is None else item.data(0, ROLE)
        if not isinstance(payload, dict):
            label.setText('Adaptive Plan未選択')
            return
        plan_id = payload.get('plan_id')
        if not isinstance(plan_id, str):
            label.setText('Adaptive Plan未選択')
            return
        plan = self.adaptive_repository.get(plan_id)
        if plan is None:
            label.setText('Adaptive Planが見つかりません')
            return

        lines = [
            f'scope {plan.execution_scope} · source {plan.source_evidence_scope}',
            f'validation {plan.validation_id[:8]} · {plan.model_id}/{plan.model_version}',
            f'algorithm {plan.algorithm_version} · acquisition {plan.acquisition_function}',
            f'length scale {plan.length_scale_m:g} m · training '
            f'{len(plan.training_candidate_ids)} · measured除外 '
            f'{len(plan.excluded_measured_candidate_ids)}',
            f'candidate pool {plan.candidate_pool_count} · proposals {len(plan.proposals)}',
            f'next candidate {plan.selected_candidate_id[:12]}',
        ]
        if plan.execution_scope == 'development_synthetic':
            lines.append(
                'synthetic development only · production recommendationは開きません'
            )
        candidate_id = payload.get('candidate_id')
        if isinstance(candidate_id, str):
            proposal = next(
                (
                    proposal
                    for proposal in plan.proposals
                    if proposal.candidate_id == candidate_id
                ),
                None,
            )
            if proposal is not None:
                lines.append(
                    f'candidate {candidate_id[:12]} · acquisition '
                    f'{proposal.acquisition_score:.4f}'
                )
                for estimate in proposal.objectives:
                    lines.append(
                        f'{estimate.objective_id}: predicted {estimate.predicted_value:.4g} '
                        f'→ corrected {estimate.corrected_mean:.4g} {estimate.unit} · '
                        f'uncertainty {estimate.residual_uncertainty:.3g} {estimate.unit}'
                    )
                self.search_selected_candidate_id = candidate_id
                if self.search_candidate_tree is not None:
                    found = False
                    for index in range(self.search_candidate_tree.topLevelItemCount()):
                        candidate_item = self.search_candidate_tree.topLevelItem(index)
                        candidate_payload = candidate_item.data(0, ROLE)
                        if candidate_payload == candidate_id:
                            self.search_candidate_tree.setCurrentItem(candidate_item)
                            found = True
                            break
                    if not found:
                        self.statusBar().showMessage(
                            'Adaptive候補は現在のcandidate page外です · '
                            'pageを移動してpreview/applyしてください'
                        )
        label.setText('\n'.join(lines))

