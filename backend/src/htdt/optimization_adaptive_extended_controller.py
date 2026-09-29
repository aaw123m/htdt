from __future__ import annotations

from PySide6.QtWidgets import QTreeWidgetItem

from .cad_adaptive_extended_service import CadAdaptiveExtendedPlannerService
from .developer_mode import developer_mode_enabled
from .native_editor import ROLE
from .native_worker import WORKER_CANCELLED
from .user_facing_error import operation_error_message


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
            'production_owned_room'
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
        extended_search_id = extended_spec.extended_search_id
        validation_id = validation.validation_id
        self.statusBar().showMessage('Adaptive Extended Planを計算しています…')
        self._refresh_adaptive_run_state()
        self._adaptive_pool.start(
            'adaptive_extended:build',
            lambda cancel_event: self.adaptive_extended_service.build_and_save(
                extended_search_id=extended_search_id,
                validation_id=validation_id,
                execution_scope=scope,
                length_scale_normalized=length_scale,
                proposal_limit=proposal_limit,
                is_cancelled=cancel_event.is_set,
            ),
            self._adaptive_extended_build_completed,
            on_finished=lambda _key: self._refresh_adaptive_run_state(),
        )

    def _adaptive_extended_build_completed(self, key, result, error) -> None:
        if error == WORKER_CANCELLED:
            self.statusBar().showMessage(
                'Adaptive Extended Plan計算を中止しました'
            )
            return
        if error is not None:
            self.statusBar().showMessage(
                f'Adaptive Extended Planを作成できません · '
                f'{operation_error_message(error)}'
            )
            return
        plan = result
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

    def cancel_adaptive_extended_build(self) -> None:
        self._adaptive_pool.cancel('adaptive_extended:build')

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
                f'Adaptive Extended Planを読めません · {operation_error_message(exc)}'
            )
            return

        selected_item = None
        # #901: historical synthetic plans stay stored and readable but are
        # hidden from the production list unless developer mode is on.
        show_synthetic = developer_mode_enabled()
        for plan in reversed(plans):
            if (
                not show_synthetic
                and plan.execution_scope == 'development_synthetic'
            ):
                continue
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
            f'スコープ {plan.execution_scope} · ソース {plan.source_evidence_scope}',
            f'ベースモデル {plan.base_model_id}/{plan.base_model_version}',
            f'拡張モデル {plan.extended_model_id}/{plan.extended_model_version}',
            f'特徴 {feature_text}',
            f'正規化GP長さスケール {plan.length_scale_normalized:g}',
            f'学習 {len(plan.training_candidate_ids)} · 実測除外 '
            f'{len(plan.excluded_measured_candidate_ids)}',
            f'候補プール {plan.candidate_pool_count} · '
            f'提案 {len(plan.proposals)}',
            f'次の拡張候補 {plan.selected_candidate_id[:12]}',
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
                    f'候補 {candidate_id[:12]} · 獲得 '
                    f'{proposal.acquisition_score:.4f}'
                )
                for estimate in proposal.objectives:
                    lines.append(
                        f'{estimate.objective_id}: 予測 '
                        f'{estimate.predicted_value:.4g} → 補正 '
                        f'{estimate.corrected_mean:.4g} {estimate.unit} · '
                        f'不確かさ {estimate.residual_uncertainty:.3g} '
                        f'{estimate.unit}'
                    )
                self.extended_selected_candidate_id = candidate_id
                self._refresh_extended_candidate_tree()
                self._refresh_extended_binding_state()
                self._render_extended_overlay()
        label.setText('\n'.join(lines))
