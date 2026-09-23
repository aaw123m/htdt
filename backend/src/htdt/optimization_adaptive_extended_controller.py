from __future__ import annotations

from PySide6.QtWidgets import QTreeWidgetItem

from .cad_adaptive_extended_service import CadAdaptiveExtendedPlannerService
from .native_editor import ROLE


class AdaptiveExtendedControllerMixin:
    def build_selected_adaptive_extended_plan(self) -> None:
        validation = self._selected_validation_record()
        extended_spec = self._selected_extended_spec()
        base_spec = self._selected_search_spec()
        if validation is None or extended_spec is None or base_spec is None:
            self.statusBar().showMessage(
                'Adaptive ExtendedにはSearchSpec / Extended SearchSpec / '
                'ValidationRecordの選択が必要です'
            )
            return
        if extended_spec.base_search_spec_id != base_spec.search_spec_id:
            self.statusBar().showMessage(
                '選択Extended SearchSpecは現在のbase SearchSpecに属していません'
            )
            return
        if validation.search_spec_id != base_spec.search_spec_id:
            self.statusBar().showMessage(
                '選択ValidationRecordは現在のbase SearchSpecに属していません'
            )
            return

        scope = (
            'development_synthetic'
            if self.adaptive_scope_combo is None
            else str(self.adaptive_scope_combo.currentData())
        )
        length_scale = (
            0.5
            if self.adaptive_extended_length_scale_field is None
            else float(self.adaptive_extended_length_scale_field.value())
        )
        proposal_limit = (
            20
            if self.adaptive_extended_proposal_limit_field is None
            else int(self.adaptive_extended_proposal_limit_field.value())
        )
        try:
            plan = self.adaptive_extended_service.build_and_save(
                extended_search_id=extended_spec.extended_search_id,
                validation_id=validation.validation_id,
                execution_scope=scope,
                length_scale_normalized=length_scale,
                proposal_limit=proposal_limit,
            )
        except Exception as exc:
            self.statusBar().showMessage(
                f'Adaptive Extended Planを作成できません · {exc}'
            )
            return

        self.refresh_adaptive_extended_plans(select_plan_id=plan.plan_id)
        mode = (
            'synthetic開発'
            if plan.execution_scope == 'development_synthetic'
            else 'owned-room本番'
        )
        self.statusBar().showMessage(
            f'O80A Adaptive Extended Planを保存しました · {mode} · '
            f'次候補 {plan.selected_candidate_id[:12]}'
        )

    def refresh_adaptive_extended_plans(
        self,
        *,
        select_plan_id: str | None = None,
    ) -> None:
        tree = self.adaptive_extended_tree
        if tree is None:
            return
        tree.clear()
        if self.adaptive_extended_detail_label is not None:
            self.adaptive_extended_detail_label.setText(
                'Adaptive Extended Plan未選択'
            )
        extended_search_id = self.extended_selected_spec_id
        if extended_search_id is None:
            return
        try:
            plans = self.adaptive_extended_repository.list_plans(
                extended_search_id
            )
        except Exception as exc:
            self.statusBar().showMessage(
                f'Adaptive Extended Planを読めません · {exc}'
            )
            return

        selected_item = None
        for plan in reversed(plans):
            scope_text = (
                'synthetic'
                if plan.execution_scope == 'development_synthetic'
                else 'owned-room'
            )
            feature_text = ', '.join(
                feature.feature_id for feature in plan.features
            )
            root = QTreeWidgetItem([
                f'plan {plan.plan_id[:8]}',
                scope_text,
                'selected ' + plan.selected_candidate_id[:12],
                feature_text,
            ])
            root.setData(0, ROLE, {'plan_id': plan.plan_id})
            tree.addTopLevelItem(root)
            if plan.plan_id == select_plan_id:
                selected_item = root
            for proposal in plan.proposals:
                objective_text = '; '.join(
                    f'{estimate.objective_id}: '
                    f'{estimate.corrected_mean:.4g}±'
                    f'{estimate.residual_uncertainty:.3g} '
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
            self._adaptive_extended_selected()

    def _adaptive_extended_selected(self) -> None:
        tree = self.adaptive_extended_tree
        label = self.adaptive_extended_detail_label
        if tree is None or label is None:
            return
        item = tree.currentItem()
        payload = None if item is None else item.data(0, ROLE)
        if not isinstance(payload, dict):
            label.setText('Adaptive Extended Plan未選択')
            return
        plan_id = payload.get('plan_id')
        if not isinstance(plan_id, str):
            label.setText('Adaptive Extended Plan未選択')
            return
        plan = self.adaptive_extended_repository.get_plan(plan_id)
        if plan is None:
            label.setText('Adaptive Extended Planが見つかりません')
            return

        feature_text = ', '.join(
            f'{feature.feature_id}[{feature.unit}]÷{feature.scale:g}'
            for feature in plan.features
        )
        lines = [
            f'scope {plan.execution_scope} · source {plan.source_evidence_scope}',
            f'base model {plan.base_model_id}/{plan.base_model_version}',
            f'extended model {plan.extended_model_id}/{plan.extended_model_version}',
            f'features {feature_text}',
            f'normalized GP length scale {plan.length_scale_normalized:g}',
            f'training {len(plan.training_candidate_ids)} · measured除外 '
            f'{len(plan.excluded_measured_candidate_ids)}',
            f'candidate pool {plan.candidate_pool_count} · '
            f'proposals {len(plan.proposals)}',
            f'next extended candidate {plan.selected_candidate_id[:12]}',
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
                        f'{estimate.objective_id}: predicted '
                        f'{estimate.predicted_value:.4g} → corrected '
                        f'{estimate.corrected_mean:.4g} {estimate.unit} · '
                        f'uncertainty {estimate.residual_uncertainty:.3g} '
                        f'{estimate.unit}'
                    )
                self.extended_selected_candidate_id = candidate_id
                self._refresh_extended_candidate_tree()
                self._refresh_extended_binding_state()
                self._render_extended_overlay()
        label.setText('\n'.join(lines))
