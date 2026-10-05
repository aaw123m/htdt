from __future__ import annotations

import logging
from collections.abc import Callable
from threading import Event
from typing import TYPE_CHECKING
from uuid import uuid4

from PySide6.QtCore import QObject, QSignalBlocker, QThread, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QComboBox,
    QDoubleSpinBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QWidget,
)

from .cad_adaptive_extended_repository import CadAdaptiveExtendedRepository
from .cad_adaptive_extended_service import CadAdaptiveExtendedPlannerService
from .cad_adaptive_repository import CadAdaptivePlanRepository
from .cad_adaptive_service import CadAdaptivePlannerService
from .developer_mode import developer_mode_enabled
from .cad_constraint_repository import CadConstraintRepository
from .cad_extended_search import CadExtendedCandidateSetPage, CadExtendedSearchAxis
from .cad_extended_search_repository import CadExtendedSearchRepository
from .cad_measurement_jobs import (
    MeasurementJobApplyContext,
    MeasurementJobGuard,
    MeasurementJobToken,
)
from .cad_measurement_models import CadMeasurementRecord
from .cad_measurement_quality_producer import CadMeasurementQualityProducer
from .cad_measurement_quality_repository import CadMeasurementQualityRepository
from .cad_measurement_repository import CadMeasurementRepository
from .cad_measurements import measurement_record_for_revision, normalize_rew_api_snapshot
from .cad_decision_rule import (
    DecisionSubject,
    VERDICT_LABELS,
    UncertaintyCompositionManifest,
    UncertaintySourceRef,
    build_decision_rule_spec,
    evaluate_pairwise_preference,
)
from .cad_decision_rule_repository import CadDecisionRuleRepository
from .cad_model_validation_repository import CadModelValidationRepository
from .cad_model_validation_service import CadModelValidationService
from .cad_objective_repository import CadObjectiveRepository
from .cad_objectives import build_pareto_set
from .cad_repository import SceneRepository
from .cad_robustness_repository import CadRobustnessRepository
from .cad_roomsim_repository import CadRoomSimRepository
from .cad_scene import scene_content_hash
from .cad_search import search_spec_current_working
from .cad_search_models import CadCandidateSetPage, constraint_workspace_snapshot
from .cad_search_repository import CadSearchRepository
from .cad_validation_campaign_repository import CadValidationCampaignRepository
from .cad_validation_campaign_service import CadValidationCampaignService
from .tree_item_role import ROLE
from .native_worker import WORKER_CANCELLED, NativeWorker, NativeWorkerPool
from .optimization_adaptive_controller import AdaptiveControllerMixin
from .optimization_adaptive_extended_controller import AdaptiveExtendedControllerMixin
from .optimization_extended_controller import ExtendedSearchControllerMixin
from .optimization_measurement_controller import MeasurementPlanControllerMixin
from .optimization_robustness_controller import RobustnessControllerMixin
from .optimization_search_controller import SearchControllerMixin
from .optimization_validation_controller import ValidationControllerMixin
from .rew_api import RewApiClient
from .room_workspace import RoomWorkspaceController

if TYPE_CHECKING:
    from .measurement_workflow import RewReadSource
from .workspace_dirty_state import DirtyResolutionAction, WorkspaceDirtyState
from .user_facing_error import operation_error_message


class _StatusProxy:
    def __init__(self, callback: Callable[[str], None]) -> None:
        self._callback = callback

    def showMessage(self, message: str) -> None:  # noqa: N802
        self._callback(str(message))


_OBJECTIVE_LABELS = {
    "response.rms_difference_db": "応答差 RMS",
    "response.peak_excess_db": "ピーク超過",
    "response.dip_deficit_db": "ディップ不足",
    "response.shape_rms_db": "応答形状 RMS",
    "pair.rms_difference_db": "ペア応答差 RMS",
    "pair.shape_rms_db": "ペア形状 RMS",
    "seat.pairwise_rms_difference_max_db": "座席間差最大",
    "seat.pairwise_rms_difference_rms_db": "座席間差 RMS",
    "seat.pairwise_shape_max_db": "座席間形状差最大",
    "seat.pairwise_shape_rms_db": "座席間形状差 RMS",
    "movement.total_m": "総移動量",
    "movement.max_m": "最大移動量",
}
_EVIDENCE_CLASS_LABELS = {
    "measured": "実測",
    "predicted": "予測",
    "derived": "派生",
    "hypothesis": "仮説",
}
_DECISION_REASON_LABELS = {
    'no_declared_uncertainty_resolution': '宣言された不確かさ証拠がありません',
    'separation_below_evidence_resolution': '差が結合分解能以下です',
    'separation_exceeds_composed_resolution': '差が結合分解能を超えています',
    'separation_within_practical_equivalence': '実用同等しきい値内です',
    'declared_limitations_apply': '宣言された制約付きです',
    'nominal_leader_display_only': '名目順位のみ表示',
}


def _objective_display_name(objective_id: str) -> str:
    known = _OBJECTIVE_LABELS.get(objective_id)
    if known is not None:
        return known
    tail = objective_id.rsplit(".", 1)[-1]
    return tail.replace("_", " ")


def _evidence_display(input_refs) -> str:
    labels = []
    for ref in input_refs:
        label = _EVIDENCE_CLASS_LABELS.get(ref.evidence_class, "根拠データ")
        if label not in labels:
            labels.append(label)
    return " / ".join(labels) if labels else "—"


class OptimizationWorkflowController(
    QObject,
    ValidationControllerMixin,
    MeasurementPlanControllerMixin,
    RobustnessControllerMixin,
    AdaptiveControllerMixin,
    AdaptiveExtendedControllerMixin,
    ExtendedSearchControllerMixin,
    SearchControllerMixin,
):
    """QMainWindow-free UX140 controller over existing O10-O80 authorities."""

    search_page_limit = 250
    statusChanged = Signal(str)
    sceneChanged = Signal(bool)
    rewBusyChanged = Signal(bool)

    def __init__(
        self,
        repository: SceneRepository,
        document_id: str,
        *,
        rew_client: RewReadSource | None = None,
    ) -> None:
        QObject.__init__(self)
        self.repository = repository
        self.document_id = document_id
        self.scene = RoomWorkspaceController(repository, document_id)
        self._selected_id = self.scene.view_state.selected_id
        self.constraint_repository = CadConstraintRepository(repository.path)
        self.constraint_set = self.constraint_repository.load(document_id)

        self.search_repository = CadSearchRepository(repository)
        self.measurement_repository = CadMeasurementRepository(repository)
        self._quality_producer: CadMeasurementQualityProducer | None = None
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
        self.robustness_repository = CadRobustnessRepository(
            scene_repository=repository,
            search_repository=self.search_repository,
            objective_repository=self.objective_repository,
            extended_search_repository=self.extended_repository,
        )
        self.decision_repository = CadDecisionRuleRepository(repository)
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
        self.extended_axes: dict[tuple[str, str], CadExtendedSearchAxis] = {}
        self.extended_selected_spec_id: str | None = None
        self.extended_selected_candidate_id: str | None = None
        self.extended_preview_candidate_id: str | None = None
        self.extended_candidate_page: CadExtendedCandidateSetPage | None = None
        self.campaign_assignments: dict[str, str] = {}

        self._search_actor_names: set[str] = set()
        self._search_pool = NativeWorkerPool(self)
        self._search_task_spec_ids: dict[str, str] = {}
        self._current_search_task_id: str | None = None
        self._extended_actor_names: set[str] = set()
        self._extended_pool = NativeWorkerPool(self)
        self._extended_task_spec_ids: dict[str, tuple[str, str]] = {}
        self._current_extended_task_id: str | None = None
        self._adaptive_pool = NativeWorkerPool(self)
        self._disposed = False

        self.viewport = None
        self._render_scene: Callable[[bool], None] | None = None
        self._status_proxy = _StatusProxy(self.statusChanged.emit)

        self.rew_client = rew_client if rew_client is not None else RewApiClient()
        self.rew_job_guard = MeasurementJobGuard()
        self._rew_pool = NativeWorkerPool(self)
        self._rew_tokens: dict[str, MeasurementJobToken] = {}
        self._rew_semantics: dict[str, tuple[str, str, str | None, str | None]] = {}
        self._latest_rew_list_key: str | None = None
        self._rew_list_sequence = 0
        self._current_rew_token_id: str | None = None

        self._create_controls()
        self.refresh_from_authorities()

    @property
    def working(self):
        return self.scene.working

    @property
    def view_state(self):
        return self.scene.view_state

    @property
    def selected_id(self) -> str | None:
        return self._selected_id

    @selected_id.setter
    def selected_id(self, value: str | None) -> None:
        self._selected_id = value

    def statusBar(self) -> _StatusProxy:  # noqa: N802
        return self._status_proxy

    def bind_viewport(self, viewport, render_scene: Callable[[bool], None]) -> None:
        self.viewport = viewport
        self._render_scene = render_scene
        self._rebuild(reset_camera=True)

    def activate(self) -> None:
        changed = self.scene.reload_if_clean()
        self._selected_id = self.scene.view_state.selected_id
        self.constraint_set = self.constraint_repository.load(self.document_id)
        self.refresh_from_authorities()
        self._rebuild(reset_camera=changed)

    def before_deactivate(self) -> tuple[bool, str | None]:
        if self.active_search_worker_count() or self.active_extended_worker_count():
            return False, "候補生成が完了またはキャンセルされるまで画面を切り替えられません"
        if self._rew_tasks:
            return False, "REW読み込みが完了するまで画面を切り替えられません"
        if self._adaptive_pool.active_count:
            return False, "アダプティブ計画計算が完了またはキャンセルされるまで画面を切り替えられません"
        return self.scene.before_deactivate()

    def dirty_state(self) -> WorkspaceDirtyState:
        """#610: worker activity blocks outright; scene state resolves."""
        if self.active_search_worker_count() or self.active_extended_worker_count():
            return 'busy'
        if self._rew_tasks:
            return 'busy'
        if self._adaptive_pool.active_count:
            return 'busy'
        return self.scene.dirty_state()

    def resolve_dirty_state(
        self, action: DirtyResolutionAction
    ) -> tuple[bool, str | None]:
        if action == 'stop_busy':
            # D1/#REV19: the operator explicitly abandoned in-flight work —
            # drain every pool like dispose() does but keep the controller
            # usable. Detached workers' completions are disconnected inside
            # stop_all, so late results can never apply or be saved.
            for token in tuple(self._rew_tokens.values()):
                self.rew_job_guard.cancel(token)
            reports = (
                self._search_pool.stop_all(),
                self._extended_pool.stop_all(),
                self._rew_pool.stop_all(),
                self._adaptive_pool.stop_all(),
            )
            self._rew_tokens.clear()
            self._rew_semantics.clear()
            self._current_rew_token_id = None
            self.rewBusyChanged.emit(False)
            if any(not report.all_stopped for report in reports):
                return True, '実行中の処理を中止しました · 停止が遅延している処理の結果は適用されません'
            return True, '実行中の処理を中止しました'
        return self.scene.resolve_dirty_state(action)

    def refresh_from_authorities(self) -> None:
        self._refresh_search_entities()
        self._refresh_search_specs()
        self._refresh_extended_entities()
        self._refresh_extended_capabilities()
        self._refresh_extended_specs()
        self.refresh_measurement_plans()
        self.refresh_validation_campaigns()
        self.refresh_model_validations()
        self.refresh_adaptive_plans()
        self.refresh_adaptive_extended_plans()
        self._refresh_campaign_measurement_points()

    def save(self) -> bool:
        created = self.scene.save()
        self._selected_id = self.scene.view_state.selected_id
        self._refresh_search_entities()
        self._refresh_search_specs()
        self._refresh_extended_entities()
        self._refresh_extended_capabilities()
        self._refresh_extended_specs()
        self._rebuild()
        self._set_dirty_status()
        return created

    def undo(self) -> bool:
        changed = self.scene.undo()
        if changed:
            self._selected_id = self.scene.view_state.selected_id
            self._refresh_search_specs()
            self._refresh_extended_specs()
            self._rebuild()
            self._set_dirty_status()
        return changed

    def redo(self) -> bool:
        changed = self.scene.redo()
        if changed:
            self._selected_id = self.scene.view_state.selected_id
            self._refresh_search_specs()
            self._refresh_extended_specs()
            self._rebuild()
            self._set_dirty_status()
        return changed

    def _sync_recovery(self) -> None:
        self.scene._sync_recovery()

    def _persist_view_state(self) -> None:
        self.scene._persist_view_state()

    def _set_dirty_status(self) -> None:
        self.statusChanged.emit("未保存の変更があります" if self.working.is_dirty else "保存済み")

    def _rebuild(self, *, reset_camera: bool = False) -> None:
        if self._render_scene is not None:
            self._render_scene(bool(reset_camera))
        self._refresh_search_binding_state()
        self._refresh_extended_binding_state()
        if self.viewport is not None:
            self._render_search_overlay()
            self._render_extended_overlay()
        self.sceneChanged.emit(bool(reset_camera))

    @property
    def _search_tasks(self) -> dict[str, tuple[QThread, NativeWorker]]:
        """Live search worker records owned by ``self._search_pool``."""
        return self._search_pool.tasks

    @property
    def _extended_tasks(self) -> dict[str, tuple[QThread, NativeWorker]]:
        """Live extended worker records owned by ``self._extended_pool``."""
        return self._extended_pool.tasks

    @property
    def _rew_tasks(self) -> dict[str, tuple[QThread, NativeWorker]]:
        """Live REW worker records owned by ``self._rew_pool``."""
        return self._rew_pool.tasks

    def active_search_worker_count(self) -> int:
        return self._search_pool.active_count

    def active_extended_worker_count(self) -> int:
        return self._extended_pool.active_count

    def dispose(self) -> None:
        self._disposed = True
        for token in tuple(self._rew_tokens.values()):
            self.rew_job_guard.cancel(token)
        reports = (
            self._search_pool.shutdown(),
            self._extended_pool.shutdown(),
            self._rew_pool.shutdown(),
            self._adaptive_pool.shutdown(),
        )
        if any(not report.all_stopped for report in reports):
            self.statusChanged.emit(
                "バックグラウンド処理の停止が遅延しています · 遅延結果は適用しません"
            )
        self._rew_tokens.clear()
        self._rew_semantics.clear()
        self._current_rew_token_id = None
        self.scene.close()

    def refresh_pareto_comparison(self) -> None:
        spec_id = self.search_selected_spec_id
        if spec_id is None or self.objective_list is None or self.pareto_tree is None:
            return
        spec = self.search_repository.get(spec_id)
        if (
            spec is None
            or not search_spec_current_working(
                spec,
                self.working,
                self.constraint_set,
                current_document_id=self.document_id,
            )
        ):
            self.pareto_tree.clear()
            self.pareto_summary_label.setText(
                "部屋または制約が変更されたため、この探索設定ではPareto比較を更新できません"
            )
            if self.decision_verdict_label is not None:
                self.decision_verdict_label.setText("証拠判定は利用できません")
            self.statusChanged.emit(
                "Pareto比較を更新できません · 部屋または制約が変更されています"
            )
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
            self.pareto_summary_label.setText("この探索設定には比較できる指標データがありません")
            if self.decision_verdict_label is not None:
                self.decision_verdict_label.setText("証拠判定は利用できません")
            return

        available = tuple(metric.objective_id for metric in evaluations[0].vector.metrics)
        expected_ids = set(available)
        expected_units = {
            metric.objective_id: metric.unit for metric in evaluations[0].vector.metrics
        }
        for evaluation in evaluations[1:]:
            metric_map = {metric.objective_id: metric for metric in evaluation.vector.metrics}
            if set(metric_map) != expected_ids:
                self.pareto_tree.clear()
                self.pareto_summary_label.setText(
                    "候補間で比較指標が一致しないため、Pareto比較を中止しました"
                )
                if self.decision_verdict_label is not None:
                    self.decision_verdict_label.setText("証拠判定は利用できません")
                return
            if any(
                metric_map[objective_id].unit != expected_units[objective_id]
                for objective_id in available
            ):
                self.pareto_tree.clear()
                self.pareto_summary_label.setText(
                    "候補間で指標の単位が一致しないため、Pareto比較を中止しました"
                )
                if self.decision_verdict_label is not None:
                    self.decision_verdict_label.setText("証拠判定は利用できません")
                return

        previous = {
            item.data(Qt.ItemDataRole.UserRole) for item in self.objective_list.selectedItems()
        }
        self.objective_list.clear()
        for objective_id in available:
            list_item = QListWidgetItem(_objective_display_name(objective_id))
            list_item.setData(Qt.ItemDataRole.UserRole, objective_id)
            self.objective_list.addItem(list_item)
            if not previous or objective_id in previous:
                list_item.setSelected(True)
        selected = tuple(
            str(item.data(Qt.ItemDataRole.UserRole))
            for item in self.objective_list.selectedItems()
        ) or available

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
            self.pareto_summary_label.setText(f"Pareto比較を作成できません · {operation_error_message(exc)}")
            if self.decision_verdict_label is not None:
                self.decision_verdict_label.setText("証拠判定は利用できません")
            return

        non_dominated = set(pareto_set.result.non_dominated_candidate_ids)
        self.pareto_tree.clear()
        for candidate_number, evaluation in enumerate(evaluations, start=1):
            metric_map = {
                metric.objective_id: metric for metric in evaluation.vector.metrics
            }
            provenance = _evidence_display(evaluation.input_refs)
            values = "; ".join(
                f"{_objective_display_name(objective_id)} "
                f"{metric_map[objective_id].value:.4g} {metric_map[objective_id].unit}"
                for objective_id in selected
            )
            item = QTreeWidgetItem(
                [
                    f"候補 {candidate_number}",
                    "非劣" if evaluation.candidate_id in non_dominated else "支配あり",
                    provenance,
                    values,
                ]
            )
            item.setData(0, ROLE, evaluation.candidate_id)
            self.pareto_tree.addTopLevelItem(item)
        reused = " · 保存済み結果を再利用" if existing is not None else ""
        self.pareto_summary_label.setText(
            f"{len(evaluations)}候補 · 非劣 {len(non_dominated)} · "
            f"指標 {len(selected)}{reused}"
        )
        self._refresh_decision_verdicts(spec_id, evaluations, selected)

    # REV56 (#577): pairwise preference verdicts gated on the declared
    # uncertainty evidence — the comparison surface shows a rankable /
    # indistinguishable / insufficient-evidence verdict plus its reason,
    # never a bare numeric ordering.
    _DECISION_PAIR_CAP = 8

    def _refresh_decision_verdicts(
        self,
        spec_id: str,
        evaluations,
        selected: tuple[str, ...],
    ) -> None:
        if self.decision_verdict_label is None:
            return
        try:
            self._apply_decision_verdicts(spec_id, evaluations, selected)
        except Exception as exc:
            self.decision_verdict_label.setText(
                f"証拠判定を計算できません · {operation_error_message(exc)}"
            )

    def _apply_decision_verdicts(
        self,
        spec_id: str,
        evaluations,
        selected: tuple[str, ...],
    ) -> None:
        scene_revision_id = getattr(self.working, 'source_revision_id', None)
        envelopes: dict[tuple[str, str], object] = {}
        envelope_times: dict[str, str] = {}
        if scene_revision_id is not None:
            evaluation_ids = {
                evaluation.evaluation_id for evaluation in evaluations
            }
            latest_spec_by_candidate: dict[str, object] = {}
            for rspec in self.robustness_repository.list_specs_for_search(
                document_id=self.document_id,
                scene_revision_id=scene_revision_id,
                search_spec_id=spec_id,
            ):
                # the envelope is bound to the exact nominal evaluation it
                # perturbed — evidence for a stale evaluation is not reused
                if rspec.nominal_objective_evaluation_id in evaluation_ids:
                    latest_spec_by_candidate[rspec.candidate_id] = rspec
            for candidate_id, rspec in latest_spec_by_candidate.items():
                envelope_times[candidate_id] = rspec.created_at_utc
                for reval in self.robustness_repository.list_evaluations(
                    rspec.robustness_spec_id
                ):
                    envelopes[(candidate_id, reval.objective_id)] = reval

        compared = list(evaluations)[: self._DECISION_PAIR_CAP]
        lines: list[str] = []
        counts: dict[str, int] = {}
        for index_a in range(len(compared)):
            for index_b in range(index_a + 1, len(compared)):
                evaluation_a = compared[index_a]
                evaluation_b = compared[index_b]
                metrics_a = {
                    metric.objective_id: metric
                    for metric in evaluation_a.vector.metrics
                }
                metrics_b = {
                    metric.objective_id: metric
                    for metric in evaluation_b.vector.metrics
                }
                for objective_id in selected:
                    metric_a = metrics_a.get(objective_id)
                    metric_b = metrics_b.get(objective_id)
                    if (
                        metric_a is None
                        or metric_b is None
                        or metric_a.direction not in ('minimize', 'maximize')
                    ):
                        continue
                    verdict = self._pairwise_objective_verdict(
                        objective_id=objective_id,
                        metric_a=metric_a,
                        metric_b=metric_b,
                        evaluation_a=evaluation_a,
                        evaluation_b=evaluation_b,
                        envelopes=envelopes,
                        envelope_times=envelope_times,
                        spec_id=spec_id,
                    )
                    counts[verdict.verdict] = counts.get(verdict.verdict, 0) + 1
                    if verdict.verdict in (
                        'evidentially_indeterminate',
                        'insufficient_evidence',
                        'clearly_superior_within_declared_evidence',
                    ) and len(lines) < 8:
                        lines.append(
                            f"{evaluation_a.candidate_id[:12]}↔"
                            f"{evaluation_b.candidate_id[:12]} "
                            f"{_objective_display_name(objective_id)}: "
                            f"{VERDICT_LABELS[verdict.verdict]}"
                            + (
                                f" — {_DECISION_REASON_LABELS.get(code, code)}"
                                if (code := next(
                                    iter(verdict.reason_codes), ''))
                                else ''
                            )
                        )
        if not counts:
            self.decision_verdict_label.setText(
                "証拠判定できる候補ペアがありません"
            )
            return
        total = sum(counts.values())
        ordered = sorted(
            counts.items(), key=lambda pair: pair[0]
        )
        heading = ' · '.join(
            f"{VERDICT_LABELS[kind]} {count}" for kind, count in ordered
        )
        lines.insert(0, f"証拠判定 {total}件: {heading}")
        self.decision_verdict_label.setText('\n'.join(lines))

    def _pairwise_objective_verdict(
        self,
        *,
        objective_id: str,
        metric_a,
        metric_b,
        evaluation_a,
        evaluation_b,
        envelopes: dict[tuple[str, str], object],
        envelope_times: dict[str, str],
        spec_id: str,
    ):
        evidence_a = envelopes.get((evaluation_a.candidate_id, objective_id))
        evidence_b = envelopes.get((evaluation_b.candidate_id, objective_id))
        sources: list[UncertaintySourceRef] = []
        if evidence_a is not None and evidence_a.sampled_envelope is not None:
            half_width = (
                evidence_a.sampled_envelope.sampled_max
                - evidence_a.sampled_envelope.sampled_min
            ) / 2.0
            sources.append(
                UncertaintySourceRef(
                    source_id=f'o90-envelope:{evaluation_a.candidate_id}',
                    source_class='input_installation_variation',
                    representation='bounded_interval',
                    value=half_width,
                    unit=metric_a.unit,
                    scope='independent',
                    authority_ref=evidence_a.evaluation_id,
                )
            )
        if evidence_b is not None and evidence_b.sampled_envelope is not None:
            half_width = (
                evidence_b.sampled_envelope.sampled_max
                - evidence_b.sampled_envelope.sampled_min
            ) / 2.0
            sources.append(
                UncertaintySourceRef(
                    source_id=f'o90-envelope:{evaluation_b.candidate_id}',
                    source_class='input_installation_variation',
                    representation='bounded_interval',
                    value=half_width,
                    unit=metric_b.unit,
                    scope='independent',
                    authority_ref=evidence_b.evaluation_id,
                )
            )
        timestamps = [
            evaluation_a.created_at_utc,
            evaluation_b.created_at_utc,
        ]
        for candidate_id in (
            evaluation_a.candidate_id, evaluation_b.candidate_id
        ):
            if candidate_id in envelope_times:
                timestamps.append(envelope_times[candidate_id])
        rule = build_decision_rule_spec(
            document_id=self.document_id,
            rule_version_label='o90-envelope-pairwise-v1',
            decision_type='pairwise_candidate_preference',
            criterion_id=objective_id,
            criterion_unit=metric_a.unit,
            criterion_direction=metric_a.direction,
            uncertainty_manifest=UncertaintyCompositionManifest(
                sources=tuple(sources),
                combination_method='bounded_linear_sum',
                combination_justification=(
                    'O90幾何・設置ばらつきの評価サンプル包絡半幅を線形和で結合'
                    ' — 相関は未宣言のため共通モード相殺を仮定せず保守的に扱う'
                    if sources
                    else 'この指標・候補ペアには宣言された不確かさ証拠がない'
                ),
            ),
            risk_policy={
                'policy_id': 'balanced_design_exploration',
                'allow_nominal_rank_display': True,
            },
            declared_limitations=(
                '材料・測定・モデル不確かさはこの判定に未合成'
                ' — 結合分解能は幾何・設置ばらつきの下限',
            ) if sources else (),
            created_at_utc=max(timestamps),
        )
        verdict = evaluate_pairwise_preference(
            rule,
            candidate_a=DecisionSubject(
                candidate_id=evaluation_a.candidate_id,
                evaluation_id=evaluation_a.evaluation_id,
                evaluation_sha256=evaluation_a.evaluation_sha256,
            ),
            candidate_b=DecisionSubject(
                candidate_id=evaluation_b.candidate_id,
                evaluation_id=evaluation_b.evaluation_id,
                evaluation_sha256=evaluation_b.evaluation_sha256,
            ),
            value_a=float(metric_a.value),
            value_b=float(metric_b.value),
            search_spec_id=spec_id,
            limitations=rule.declared_limitations,
            created_at_utc=max(timestamps),
        )
        self.decision_repository.save_rule(rule)
        self.decision_repository.save_verdict(verdict)
        return verdict

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
        for index in range(self.search_candidate_tree.topLevelItemCount()):
            candidate_item = self.search_candidate_tree.topLevelItem(index)
            if candidate_item.data(0, ROLE) == candidate_id:
                self.search_candidate_tree.setCurrentItem(candidate_item)
                return
        self.statusChanged.emit(
            "対象候補は現在の候補ページ外です · ページを移動してからプレビューまたは適用してください"
        )

    def refresh_rew_list_async(self) -> None:
        self._rew_list_sequence += 1
        key = f"list:{self._rew_list_sequence}"
        self._latest_rew_list_key = key
        self._start_rew_task(
            key,
            lambda cancel_event: self.rew_client.list_measurements(
                is_cancelled=cancel_event.is_set
            ),
        )
        self.statusChanged.emit("REW測定一覧を読み込み中…")

    def _start_selected_rew_read(
        self,
        *,
        validation_scope: str | None,
        validation_campaign_id: str | None,
        evidence_type_override: str | None = None,
    ) -> None:
        external_id = self.rew_combo.currentData()
        entity_id = self.campaign_measurement_point_combo.currentData()
        plan = self._selected_measurement_plan()
        if not isinstance(external_id, str):
            raise ValueError("REW測定を選択してください")
        if not isinstance(entity_id, str):
            raise ValueError("測定点を選択してください")
        if plan is None:
            raise ValueError("測定計画を選択してください")
        revision = self.repository.get(plan.applied_scene_revision_id)
        if revision is None:
            raise ValueError("測定計画に対応する保存済みの部屋状態が見つかりません")
        provisional = measurement_record_for_revision(
            revision,
            entity_id,
            source_kind="rew_api",
            external_source_id=external_id,
        )
        token = self.rew_job_guard.submit(
            provisional,
            external_source_id=external_id,
            query={"unit": "SPL", "ppo": None, "smoothing": None},
            constraint_workspace_hash=self._constraint_workspace_hash(),
        )
        self._rew_tokens[token.job_id] = token
        evidence = evidence_type_override or "unknown"
        self._rew_semantics[token.job_id] = (
            evidence,
            self.rew_channel_role_field.text().strip() or "unknown",
            validation_scope,
            validation_campaign_id,
        )
        self._current_rew_token_id = token.job_id
        self._start_rew_task(
            token.job_id,
            lambda cancel_event: self.rew_client.get_frequency_response_snapshot(
                external_id,
                ppo=None,
                unit="SPL",
                smoothing=None,
                is_cancelled=cancel_event.is_set,
            ),
        )
        self.statusChanged.emit("REWを読み込んでいます…")

    def _start_rew_task(
        self, key: str, operation: Callable[[Event], object]
    ) -> None:
        if self._disposed:
            return
        self._rew_pool.start(
            key,
            lambda cancel_event: operation(cancel_event),
            self._rew_task_completed,
            on_finished=self._rew_task_finished,
        )
        self.rewBusyChanged.emit(True)

    @Slot(object, object, object)
    def _rew_task_completed(self, key: object, result: object, error: object) -> None:
        if self._disposed:
            return
        task_key = str(key)
        if task_key.startswith("list:"):
            if task_key != self._latest_rew_list_key or error == WORKER_CANCELLED:
                return
            if error is not None:
                self.statusChanged.emit(f"REW一覧取得失敗 · {operation_error_message(error)}")
                return
            summaries = result if isinstance(result, list) else []
            with QSignalBlocker(self.rew_combo):
                self.rew_combo.clear()
                visible_index = 0
                for summary in summaries:
                    if not isinstance(summary, dict) or not isinstance(summary.get("uuid"), str):
                        continue
                    visible_index += 1
                    title = summary.get("title")
                    label = (
                        title.strip()
                        if isinstance(title, str) and title.strip()
                        else f"REW測定 {visible_index}"
                    )
                    self.rew_combo.addItem(label, summary["uuid"])
            self.statusChanged.emit(f"REW測定 {len(summaries)} 件を確認しました")
            return

        token = self._rew_tokens.get(task_key)
        if token is None:
            return
        if error is not None:
            if error != WORKER_CANCELLED and not self.rew_job_guard.is_cancelled(token):
                self.statusChanged.emit(f"REW読み込み失敗 · {operation_error_message(error)}")
            return
        context = self._current_job_apply_context()
        if context is None or not self.rew_job_guard.can_apply(token, context):
            self.statusChanged.emit(
                "REWの遅延結果は現在の配置へ適用しません · 部屋の保存状態または制約が変更されています"
            )
            return
        revision = self.repository.get(token.scene_revision_id)
        if revision is None:
            self.statusChanged.emit("REW結果に対応する保存状態が見つかりません")
            return
        evidence, channel_role, validation_scope, validation_campaign_id = (
            self._rew_semantics.get(task_key, ("unknown", "unknown", None, None))
        )
        try:
            record, dataset, filename, raw = normalize_rew_api_snapshot(
                revision,
                token.measurement_entity_id,
                result,
                evidence_type=evidence,
                channel_role=channel_role,
                validation_scope=validation_scope,
                validation_campaign_id=validation_campaign_id,
            )
            self.measurement_repository.save(
                record,
                dataset,
                raw_filename=filename,
                raw_bytes=raw,
            )
            self._produce_quality_report(record.measurement_id)
        except Exception as exc:
            self.statusChanged.emit(f"REW結果保存失敗 · {operation_error_message(exc)}")
            return
        self.refresh_measurement_plans()
        self.refresh_validation_campaigns()
        self.statusChanged.emit("REW測定を保存しました")

    def _produce_quality_report(self, measurement_id: str) -> None:
        """Derive the committed measurement's quality report (#REV42-QUALITYPROD).

        Best-effort: a derivation failure leaves the measurement honestly
        report-less (quality_pending) and the next quality read retries —
        it must not fail a measurement save that already succeeded.
        """
        try:
            if self._quality_producer is None:
                self._quality_producer = CadMeasurementQualityProducer(
                    CadMeasurementQualityRepository(self.measurement_repository)
                )
            self._quality_producer.produce_report(measurement_id)
        except Exception:
            logging.getLogger(__name__).warning(
                'quality report production failed for %s',
                measurement_id,
                exc_info=True,
            )

    def _constraint_workspace_hash(self) -> str:
        """Digest of the constraint workspace a delayed REW read is bound to."""
        _snapshot, digest = constraint_workspace_snapshot(self.constraint_set)
        return digest

    def _current_job_apply_context(self) -> MeasurementJobApplyContext | None:
        if self.working.source_revision_id is None:
            return None
        return MeasurementJobApplyContext(
            document_id=self.document_id,
            scene_revision_id=self.working.source_revision_id,
            scene_content_hash=scene_content_hash(self.working.committed_document),
            constraint_workspace_hash=self._constraint_workspace_hash(),
        )

    def _rew_task_finished(self, key: str) -> None:
        self._rew_tasks.pop(key, None)
        if not key.startswith("list:"):
            self._rew_tokens.pop(key, None)
            self._rew_semantics.pop(key, None)
            if self._current_rew_token_id == key:
                self._current_rew_token_id = None
        self.rewBusyChanged.emit(bool(self._rew_tasks))

    def _refresh_campaign_measurement_points(self) -> None:
        plan = self._selected_measurement_plan()
        previous = self.campaign_measurement_point_combo.currentData()
        with QSignalBlocker(self.campaign_measurement_point_combo):
            self.campaign_measurement_point_combo.clear()
            if plan is None:
                return
            revision = self.repository.get(plan.applied_scene_revision_id)
            if revision is None:
                return
            for entity in revision.document.entities:
                if entity.kind == "measurement_point":
                    self.campaign_measurement_point_combo.addItem(
                        entity.name, entity.entity_id
                    )
            if previous is not None:
                index = self.campaign_measurement_point_combo.findData(previous)
                if index >= 0:
                    self.campaign_measurement_point_combo.setCurrentIndex(index)

    def _create_controls(self) -> None:
        self._create_robustness_controls()
        self.search_binding_label = QLabel("保存済みの部屋状態から探索設定を作成します")
        self.search_binding_label.setWordWrap(True)
        self.search_name_field = QLineEdit()
        self.search_name_field.setPlaceholderText("例: FL 前後移動")
        self.search_entity_combo = QComboBox()
        self.search_entity_combo.currentIndexChanged.connect(self._seed_search_axis_range)
        self.search_axis_combo = QComboBox()
        for label, value in (("X", "x"), ("Y", "y"), ("Z", "z")):
            self.search_axis_combo.addItem(label, value)
        self.search_axis_combo.currentIndexChanged.connect(self._seed_search_axis_range)
        self.search_min_field = self._search_distance_field()
        self.search_max_field = self._search_distance_field()
        self.search_step_field = self._search_distance_field(minimum=0.001, value=0.10)
        self.search_limit_field = QSpinBox()
        self.search_limit_field.setRange(1, 50_000)
        self.search_limit_field.setValue(10_000)
        # #1090: task-first range presets add sane axis ranges for the
        # selected entity without touching the low-level numeric form.
        self.search_preset_combo = QComboBox()
        self.search_preset_combo.setAccessibleName('探索プリセット')
        for key, label, _axes, _range, _step in self.SEARCH_RANGE_PRESETS:
            self.search_preset_combo.addItem(label, key)
        self.search_preset_apply_button = QPushButton("プリセットで軸を追加")
        self.search_preset_apply_button.clicked.connect(
            self.apply_search_range_preset
        )
        self.search_axis_tree = QTreeWidget()
        self.search_axis_tree.setAccessibleName('探索軸')
        self.search_axis_tree.setHeaderLabels(["物体", "軸", "最小", "最大", "刻み"])
        self.linked_master_combo = QComboBox()
        self.linked_slave_combo = QComboBox()
        self.linked_relation_combo = QComboBox()
        for label, value in (
            ("鏡像 X", "mirror_x"),
            ("X一致", "equal_x"),
            ("Y一致", "equal_y"),
            ("Z一致", "equal_z"),
            ("X同一変位", "equal_delta_x"),
            ("Y同一変位", "equal_delta_y"),
            ("Z同一変位", "equal_delta_z"),
        ):
            self.linked_relation_combo.addItem(label, value)
        self.linked_mirror_field = self._search_distance_field(minimum=0.0)
        self.linked_add_button = QPushButton("連動を追加 / 更新")
        self.linked_add_button.clicked.connect(self.add_linked_search_variable)
        self.linked_remove_button = QPushButton("選択連動を削除")
        self.linked_remove_button.clicked.connect(self.remove_selected_linked_variable)
        self.search_linked_tree = QTreeWidget()
        self.search_linked_tree.setAccessibleName('連動パラメーター')
        self.search_linked_tree.setHeaderLabels(["マスター", "スレーブ", "関係", "鏡面x"])
        self.search_save_button = QPushButton("探索設定を保存")
        self.search_save_button.clicked.connect(self.save_search_spec)
        self.search_spec_tree = QTreeWidget()
        self.search_spec_tree.setAccessibleName('保存済み探索設定')
        self.search_spec_tree.setHeaderLabels(["探索設定", "入力状態", "状態"])
        self.search_spec_tree.itemSelectionChanged.connect(self._search_spec_selected)
        self.search_reauthor_button = QPushButton("同じ条件で再探索")
        self.search_reauthor_button.setToolTip(
            "選択した探索設定の軸・候補上限・連動変数を現在の部屋へ再作成します"
        )
        self.search_reauthor_button.clicked.connect(
            self.reauthor_selected_search_spec
        )
        self.search_generate_button = QPushButton("候補を生成")
        self.search_generate_button.clicked.connect(self.generate_search_candidates_async)
        self.search_cancel_button = QPushButton("生成をキャンセル")
        self.search_cancel_button.clicked.connect(self.cancel_search_generation)
        self.search_summary_label = QLabel("候補未生成")
        self.search_generate_reason_label = QLabel()
        self.search_generate_reason_label.setWordWrap(True)
        self.search_generate_reason_label.hide()
        self.search_candidate_tree = QTreeWidget()
        self.search_candidate_tree.setAccessibleName('生成候補')
        self.search_candidate_tree.setHeaderLabels(["候補", "番号", "位置"])
        # #1088: header sorting (番号 column sorts numerically) plus a text
        # filter so large candidate pages stay triageable.
        self.search_candidate_tree.setSortingEnabled(True)
        self.search_candidate_tree.sortItems(1, Qt.SortOrder.AscendingOrder)
        self.search_candidate_tree.itemSelectionChanged.connect(self._search_candidate_selected)
        self.search_candidate_filter_field = QLineEdit()
        self.search_candidate_filter_field.setPlaceholderText("番号・位置で絞り込み")
        self.search_candidate_filter_field.setClearButtonEnabled(True)
        self.search_candidate_filter_field.textChanged.connect(
            self._apply_search_candidate_filter
        )
        self.search_candidate_filter_note = QLabel()
        self.search_candidate_filter_note.setWordWrap(True)
        self.search_candidate_filter_note.hide()
        self.search_prev_button = QPushButton("前の候補")
        self.search_prev_button.clicked.connect(self.previous_search_page)
        self.search_next_button = QPushButton("次の候補")
        self.search_next_button.clicked.connect(self.next_search_page)
        self.search_preview_button = QPushButton("候補をプレビュー")
        self.search_preview_button.clicked.connect(self.preview_selected_candidate)
        self.search_clear_preview_button = QPushButton("プレビュー解除")
        self.search_clear_preview_button.clicked.connect(self.clear_candidate_preview)
        self.search_apply_button = QPushButton("候補を適用")
        self.search_apply_button.clicked.connect(self.apply_selected_candidate)

        self.measurement_plan_button = QPushButton("現在の保存版を実測候補として記録")
        self.measurement_plan_button.clicked.connect(
            self.create_measurement_plan_for_selected_candidate
        )
        self.measurement_plan_label = QLabel("実測候補未登録")
        self.measurement_plan_tree = QTreeWidget()
        self.measurement_plan_tree.setAccessibleName('実測候補')
        self.measurement_plan_tree.setHeaderLabels(["実測候補", "状態", "保存状態", "測定"])
        self.measurement_plan_tree.itemSelectionChanged.connect(
            self._measurement_plan_selected
        )
        self.measurement_plan_tree.itemSelectionChanged.connect(
            self._refresh_campaign_measurement_points
        )
        self.measurement_match_list = QListWidget()
        self.measurement_match_list.setAccessibleName('関連実測候補')
        self.measurement_match_list.setSelectionMode(
            QListWidget.SelectionMode.MultiSelection
        )
        self.measurement_complete_button = QPushButton(
            "選択した実測を候補へ関連付け"
        )
        self.measurement_complete_button.clicked.connect(
            self.complete_selected_measurement_plan
        )

        self.objective_list = QListWidget()
        self.objective_list.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        self.pareto_refresh_button = QPushButton("Pareto集合を更新")
        self.pareto_refresh_button.clicked.connect(self.refresh_pareto_comparison)
        self.pareto_summary_label = QLabel("比較指標が未読み込みです")
        self.decision_verdict_label = QLabel("証拠判定は未評価です")
        self.decision_verdict_label.setAccessibleName('証拠判定')
        self.pareto_tree = QTreeWidget()
        self.pareto_tree.setAccessibleName('Pareto比較候補')
        self.pareto_tree.setHeaderLabels(["候補", "Pareto", "根拠", "指標"])
        self.pareto_tree.itemSelectionChanged.connect(self._pareto_candidate_selected)

        self.campaign_assignment_tree = QTreeWidget()
        self.campaign_assignment_tree.setAccessibleName('検証候補')
        self.campaign_assignment_tree.setHeaderLabels(["候補", "役割"])
        self.campaign_model_version_field = QLineEdit()
        self.campaign_low_field = self._number_field(1.0, 20_000.0, 20.0, 1)
        self.campaign_high_field = self._number_field(1.0, 20_000.0, 160.0, 1)
        self.campaign_residual_field = self._number_field(0.0, 100.0, 0.0, 2)
        self.campaign_sensitivity_field = self._number_field(0.0, 1000.0, 0.0, 2)
        self.campaign_sensitivity_error_field = self._number_field(
            0.0, 1000.0, 0.0, 2
        )
        self.campaign_separation_field = self._number_field(0.0, 100.0, 0.0, 2)
        for field in (
            self.campaign_residual_field,
            self.campaign_sensitivity_field,
            self.campaign_sensitivity_error_field,
            self.campaign_separation_field,
        ):
            field.setSpecialValueText("要設定")
        self.campaign_tree = QTreeWidget()
        self.campaign_tree.setAccessibleName('検証キャンペーン')
        self.campaign_tree.setHeaderLabels(["検証条件", "モデル", "候補", "準備状況"])
        self.campaign_tree.itemSelectionChanged.connect(self._campaign_selected)
        self.campaign_detail_label = QLabel("検証条件が未選択です")
        self.campaign_applicability_state: dict[str, QComboBox] = {}
        self.campaign_applicability_detail: dict[str, QLineEdit] = {}
        self.campaign_applicability_attest: dict[str, QPushButton] = {}
        for code in ("geometry", "band", "routing"):
            state = QComboBox()
            state.addItem("未確認", "unverified")
            state.addItem("自動評価", "auto")
            state.addItem("手動証拠", "manual")
            self.campaign_applicability_state[code] = state
            detail = QLineEdit()
            detail.setPlaceholderText("確認メモ / 証明 ID")
            self.campaign_applicability_detail[code] = detail
            attest = QPushButton("証明…")
            attest.setToolTip(
                "手動証拠として使う適用条件証明を登録・選択します"
            )
            attest.clicked.connect(
                lambda _checked=False, c=code: self.open_applicability_attestation(c)
            )
            self.campaign_applicability_attest[code] = attest

        self.validation_refresh_button = QPushButton("保存済み検証を更新")
        self.validation_refresh_button.clicked.connect(self.refresh_model_validations)
        self.validation_tree = QTreeWidget()
        self.validation_tree.setAccessibleName('保存済み検証')
        self.validation_tree.setHeaderLabels(
            ["検証", "範囲", "残差", "傾向", "感度", "再現性", "推奨可否"]
        )
        self.validation_tree.itemSelectionChanged.connect(self._validation_selected)
        self.validation_detail_label = QLabel("検証結果が未選択です")

        self.adaptive_scope_combo = QComboBox()
        # #901: the production owned-room scope is the default and the only
        # scope in a normal session — the synthetic development lane is
        # opt-in via developer mode and must never be the operator default.
        self.adaptive_scope_combo.addItem("実室データで本番検証", "production_owned_room")
        if developer_mode_enabled():
            self.adaptive_scope_combo.addItem("合成データで開発検証", "development_synthetic")
        self.adaptive_length_scale_field = self._number_field(0.01, 20.0, 0.5, 3)
        self.adaptive_length_scale_field.setSuffix(" m")
        self.adaptive_proposal_limit_field = QSpinBox()
        self.adaptive_proposal_limit_field.setRange(1, 100)
        self.adaptive_proposal_limit_field.setValue(20)
        self.adaptive_build_button = QPushButton("次の測定候補を計算・保存")
        self.adaptive_build_button.clicked.connect(self.build_selected_adaptive_plan)
        self.adaptive_cancel_button = QPushButton("計算を中止")
        self.adaptive_cancel_button.setEnabled(False)
        self.adaptive_cancel_button.clicked.connect(self.cancel_adaptive_build)
        self.adaptive_tree = QTreeWidget()
        self.adaptive_tree.setAccessibleName('アダプティブ計画')
        self.adaptive_tree.setHeaderLabels(["計画 / 候補", "範囲", "取得値", "補正指標"])
        self.adaptive_tree.itemSelectionChanged.connect(self._adaptive_selected)
        self.adaptive_detail_label = QLabel("次候補の計画が未選択です")

        self.extended_capability_combo = QComboBox()
        self.extended_capability_combo.setAccessibleName('拡張能力の検証結果')
        self.extended_parameter_combo = QComboBox()
        self.extended_parameter_combo.addItem("音響照準（ヨー）", "aim_yaw_deg")
        self.extended_parameter_combo.addItem("音響照準（ピッチ）", "aim_pitch_deg")
        self.extended_parameter_combo.addItem(
            "筐体ヨー（トーイン）", "body_yaw_deg"
        )
        self.extended_parameter_combo.currentIndexChanged.connect(
            self._seed_extended_aim_range
        )
        self.extended_entity_combo = QComboBox()
        self.extended_entity_combo.currentIndexChanged.connect(
            self._seed_extended_aim_range
        )
        self.extended_min_field = self._angle_field()
        self.extended_max_field = self._angle_field()
        self.extended_step_field = self._angle_field(minimum=0.1, value=5.0)
        self.extended_limit_field = QSpinBox()
        self.extended_limit_field.setRange(1, 50_000)
        self.extended_limit_field.setValue(10_000)
        self.extended_axis_tree = QTreeWidget()
        self.extended_axis_tree.setAccessibleName('拡張探索軸')
        self.extended_axis_tree.setHeaderLabels(
            ["スピーカー", "パラメーター", "最小", "最大", "刻み"]
        )
        self.extended_spec_tree = QTreeWidget()
        self.extended_spec_tree.setAccessibleName('拡張探索設定')
        self.extended_spec_tree.setHeaderLabels(
            ["拡張探索", "モデル", "パラメーター", "状態"]
        )
        self.extended_spec_tree.itemSelectionChanged.connect(
            self._extended_spec_selected
        )
        self.extended_generate_button = QPushButton("拡張候補を生成")
        self.extended_generate_button.clicked.connect(
            self.generate_extended_candidates_async
        )
        self.extended_cancel_button = QPushButton("生成をキャンセル")
        self.extended_cancel_button.clicked.connect(self.cancel_extended_generation)
        self.extended_summary_label = QLabel("拡張候補は未生成です")
        self.extended_candidate_tree = QTreeWidget()
        self.extended_candidate_tree.setAccessibleName('拡張生成候補')
        self.extended_candidate_tree.setHeaderLabels(
            ["候補", "元候補", "位置", "音響ヨー", "筐体ヨー"]
        )
        self.extended_candidate_tree.setSortingEnabled(True)
        # 候補 column carries the canonical enumeration index as its sort
        # key — pin ascending so the default view is enumeration order,
        # like the 番号-sorted search tree.
        self.extended_candidate_tree.sortItems(
            0, Qt.SortOrder.AscendingOrder
        )
        self.extended_candidate_tree.itemSelectionChanged.connect(
            self._extended_candidate_selected
        )
        self.extended_candidate_filter_field = QLineEdit()
        self.extended_candidate_filter_field.setPlaceholderText("候補・位置で絞り込み")
        self.extended_candidate_filter_field.setClearButtonEnabled(True)
        self.extended_candidate_filter_field.textChanged.connect(
            self._apply_extended_candidate_filter
        )
        self.extended_candidate_filter_note = QLabel()
        self.extended_candidate_filter_note.setWordWrap(True)
        self.extended_candidate_filter_note.hide()
        self.extended_prev_button = QPushButton("前の拡張候補")
        self.extended_prev_button.clicked.connect(self.previous_extended_page)
        self.extended_next_button = QPushButton("次の拡張候補")
        self.extended_next_button.clicked.connect(self.next_extended_page)
        self.extended_preview_button = QPushButton("拡張候補をプレビュー")
        self.extended_preview_button.clicked.connect(
            self.preview_selected_extended_candidate
        )
        self.extended_clear_preview_button = QPushButton("プレビュー解除")
        self.extended_clear_preview_button.clicked.connect(self.clear_extended_preview)
        self.extended_apply_button = QPushButton("拡張候補を適用")
        self.extended_apply_button.clicked.connect(
            self.apply_selected_extended_candidate
        )

        self.adaptive_extended_length_scale_field = self._number_field(
            0.01, 20.0, 0.5, 3
        )
        self.adaptive_extended_proposal_limit_field = QSpinBox()
        self.adaptive_extended_proposal_limit_field.setRange(1, 100)
        self.adaptive_extended_proposal_limit_field.setValue(20)
        self.adaptive_extended_build_button = QPushButton(
            "拡張した次候補を計算・保存"
        )
        self.adaptive_extended_build_button.clicked.connect(
            self.build_selected_adaptive_extended_plan
        )
        self.adaptive_extended_cancel_button = QPushButton("計算を中止")
        self.adaptive_extended_cancel_button.setEnabled(False)
        self.adaptive_extended_cancel_button.clicked.connect(
            self.cancel_adaptive_extended_build
        )
        self.adaptive_extended_tree = QTreeWidget()
        self.adaptive_extended_tree.setHeaderLabels(
            ["計画 / 候補", "範囲", "取得値", "特徴 / 指標"]
        )
        self.adaptive_extended_tree.itemSelectionChanged.connect(
            self._adaptive_extended_selected
        )
        self.adaptive_extended_detail_label = QLabel(
            "拡張した次候補の計画が未選択です"
        )

        self.rew_combo = QComboBox()
        self.rew_refresh_button = QPushButton("REW一覧更新")
        self.rew_refresh_button.clicked.connect(self.refresh_rew_list_async)
        self.rew_channel_role_field = QLineEdit("unknown")
        self.rew_channel_role_field.setPlaceholderText("例: FL / C / SUB")
        self.campaign_measurement_point_combo = QComboBox()

        self._apply_help_tooltips()

    def _apply_help_tooltips(self) -> None:
        """Explain every control and column in plain Japanese.

        The workspace forms are terse by design; the tooltip carries the
        field's meaning, its unit, and what the value affects so the panel
        stays readable without a manual.
        """
        tooltips: dict[QWidget, str] = {
            self.search_name_field: (
                "この探索設定の名前です。保存済み一覧にこの名前で表示されます。"
            ),
            self.search_entity_combo: (
                "動かす対象の物体（スピーカー・機器・測定点など）です。"
            ),
            self.search_axis_combo: (
                "物体を動かす方向です。X=部屋の幅、Y=奥行き、Z=高さです。"
            ),
            self.search_min_field: (
                "探索する範囲の下限です（m）。部屋の座標での絶対位置です。"
            ),
            self.search_max_field: (
                "探索する範囲の上限です（m）。部屋の座標での絶対位置です。"
            ),
            self.search_step_field: (
                "候補を生成する間隔です（m）。小さいほど細かくなり、候補数と計算時間が増えます。"
            ),
            self.search_limit_field: (
                "生成する候補数の上限です（1〜50000）。上限を超えた分の組み合わせは生成されません。"
            ),
            self.search_preset_combo: (
                "代表的な探索範囲のテンプレートです。選んで「プリセットで軸を追加」を押すと、"
                "下の詳細フォームを使わずに軸を登録できます。"
            ),
            self.search_preset_apply_button: (
                "選択したプリセットの軸・範囲・刻みを探索軸として追加します。"
            ),
            self.search_axis_tree: (
                "登録済みの探索軸の一覧です。「軸を追加 / 更新」で上のフォームの内容がここに反映されます。"
            ),
            self.linked_master_combo: (
                "連動の基準となる物体（マスター）です。スレーブはこの物体の位置から決まります。"
            ),
            self.linked_slave_combo: (
                "マスターの移動に追従する物体（スレーブ）です。"
            ),
            self.linked_relation_combo: (
                "スレーブの位置をマスターから決める関係です。"
                "鏡像=鏡面を挟んだ対称位置、一致=同じ座標値、同一変位=同じ移動量です。"
            ),
            self.linked_mirror_field: (
                "「鏡像 X」を選んだときの鏡面の X 座標です（m）。"
                "部屋の中心線は自動では仮定されないため、必ず値を指定します。"
            ),
            self.search_linked_tree: (
                "登録済みの連動ルールです。候補生成時にスレーブの位置はこのルールで自動的に決まります。"
            ),
            self.search_save_button: (
                "現在の軸・候補上限・連動ルールを探索設定として保存します。"
            ),
            self.search_spec_tree: (
                "保存済みの探索設定です。「入力状態」が古い設定は部屋や制約の変更で無効化されており、"
                "「同じ条件で再探索」で現在の部屋に作り直せます。"
            ),
            self.search_generate_button: (
                "登録した軸の組み合わせから、ハード制約を満たす配置候補を生成します。"
                "ここでは順位や推奨は決めません。"
            ),
            self.search_cancel_button: "実行中の候補生成を中断します。",
            self.search_candidate_tree: (
                "生成された候補の一覧です。選択するとプレビューや適用ができます。"
            ),
            self.search_candidate_filter_field: (
                "番号や位置の文字で候補一覧を絞り込みます。"
            ),
            self.search_prev_button: "候補一覧を前のページへ戻します。",
            self.search_next_button: "候補一覧を次のページへ進めます。",
            self.search_preview_button: (
                "選択した候補の配置を 3D プレビューに重ねて表示します。部屋はまだ変更されません。"
            ),
            self.search_clear_preview_button: "プレビューの重ね表示を解除します。",
            self.search_apply_button: (
                "選択した候補の配置を新しいリビジョンとして部屋に適用します。"
            ),
            self.measurement_plan_button: (
                "現在の保存版の配置を実測候補として記録します。"
                "後で実際の測定結果と突き合わせるための予定表です。"
            ),
            self.measurement_plan_tree: (
                "実測予定の候補一覧です。「測定」列が実測結果との突き合わせ状況を示します。"
            ),
            self.measurement_match_list: (
                "選択した候補に関連付ける実測結果を選びます（複数選択可）。"
            ),
            self.measurement_complete_button: (
                "選択した実測を候補に関連付けて、突き合わせを完了にします。"
            ),
            self.objective_list: (
                "候補の比較に使う評価指標を選びます（複数選択可）。"
            ),
            self.pareto_refresh_button: (
                "選択した指標で候補同士を比較します。Pareto最適とは、"
                "どれか1つの指標を良くしようとすると別の指標が必ず悪くなる候補のことです。"
            ),
            self.pareto_tree: (
                "Pareto比較の結果です。「Pareto」列が支配関係、"
                "「根拠」列がその判定の理由、「指標」列が各評価値です。"
            ),
            self.campaign_assignment_tree: (
                "検証対象の候補と、その検証内での役割（基準・比較など）です。"
            ),
            self.campaign_model_version_field: (
                "この検証に使う予測モデルのバージョン識別子です。"
            ),
            self.campaign_low_field: "検証する周波数帯の下限です（Hz）。",
            self.campaign_high_field: "検証する周波数帯の上限です（Hz）。",
            self.campaign_residual_field: (
                "検証に使う実測RMS誤差の許容上限です（dB）。"
                "予測と実測の差がこれを超えると検証は不合格です。"
            ),
            self.campaign_sensitivity_field: (
                "位置が1mずれたときの音圧変化（感度）の許容上限です（dB/m）。"
            ),
            self.campaign_sensitivity_error_field: (
                "感度の推定に許容する誤差の上限です（dB/m）。"
            ),
            self.campaign_separation_field: (
                "候補同士を区別できるとみなす最小の差（再現性の目安）です。"
            ),
            self.campaign_tree: (
                "保存済みの検証キャンペーンです。「準備状況」が実施に必要な条件の充足を示します。"
            ),
            self.validation_refresh_button: (
                "保存済みのモデル検証結果を読み込み直します。"
            ),
            self.validation_tree: (
                "モデル検証の結果一覧です。「推奨可否」は、この検証結果を配置の推奨根拠に"
                "使えるかどうかを示します。"
            ),
            self.adaptive_scope_combo: (
                "次候補の計算に使うデータの範囲です。通常は実室データのみです。"
            ),
            self.adaptive_length_scale_field: (
                "ガウス過程（GP）回帰の長さ尺度です（m）。"
                "大きいほど滑らかな変化を仮定し広い範囲から、小さいほど近傍から次候補を探します。"
            ),
            self.adaptive_proposal_limit_field: (
                "1回の計算で出す次候補の最大数です。"
            ),
            self.adaptive_build_button: (
                "取得済みの測定データから、次に測ると情報量が最も増える候補を計算して保存します。"
            ),
            self.adaptive_cancel_button: "実行中の次候補計算を中断します。",
            self.adaptive_tree: (
                "アダプティブ計画とその候補の一覧です。「補正指標」はモデルの不確かさの大きさを示します。"
            ),
            self.extended_capability_combo: (
                "拡張探索の前提となる検証済み能力（向き探索の検証結果）です。"
                "検証結果がない場合は「選択した検証結果から本番向け能力を作成」で作ります。"
            ),
            self.extended_parameter_combo: (
                "探索する角度パラメーターです。音響照準=音が向かう方向、"
                "筐体ヨー=スピーカー筐体の実際の向き（トーイン）です。"
            ),
            self.extended_entity_combo: "角度を変えるスピーカーです。",
            self.extended_min_field: "角度の下限です（°）。",
            self.extended_max_field: "角度の上限です（°）。",
            self.extended_step_field: (
                "角度の刻みです（°）。小さいほど細かく、候補数と計算時間が増えます。"
            ),
            self.extended_limit_field: (
                "生成する拡張候補数の上限です（1〜50000）。"
            ),
            self.extended_axis_tree: (
                "登録済みの拡張探索軸（角度範囲）の一覧です。"
            ),
            self.extended_spec_tree: (
                "保存済みの拡張探索設定と、対象モデル・パラメーター・状態です。"
            ),
            self.extended_generate_button: (
                "登録した角度範囲から拡張候補を生成します。"
            ),
            self.extended_cancel_button: "実行中の拡張候補生成を中断します。",
            self.extended_candidate_tree: (
                "生成された拡張候補の一覧です。元候補からの角度変化を確認できます。"
            ),
            self.extended_candidate_filter_field: (
                "候補名や位置の文字で拡張候補を絞り込みます。"
            ),
            self.extended_prev_button: "拡張候補一覧を前のページへ戻します。",
            self.extended_next_button: "拡張候補一覧を次のページへ進めます。",
            self.extended_preview_button: (
                "選択した拡張候補の角度を 3D プレビューで確認します。"
            ),
            self.extended_clear_preview_button: "拡張候補のプレビュー表示を解除します。",
            self.extended_apply_button: (
                "選択した拡張候補の角度を部屋に適用します。"
            ),
            self.adaptive_extended_length_scale_field: (
                "拡張した次候補を探すガウス過程の正規化長さ尺度です（m）。"
            ),
            self.adaptive_extended_proposal_limit_field: (
                "1回の計算で出す拡張次候補の最大数です。"
            ),
            self.adaptive_extended_build_button: (
                "拡張探索も含めて、次に測るべき候補を計算して保存します。"
            ),
            self.adaptive_extended_cancel_button: (
                "実行中の拡張次候補計算を中断します。"
            ),
            self.adaptive_extended_tree: (
                "拡張アダプティブ計画とその候補の一覧です。"
            ),
            self.rew_combo: "取り込む REW 測定を選びます。",
            self.rew_refresh_button: "REW API から測定一覧を更新します。",
            self.rew_channel_role_field: (
                "この測定のチャンネル役割です（例: FL / C / SUB）。実測と候補の突き合わせに使います。"
            ),
            self.campaign_measurement_point_combo: (
                "実測の採点位置（部屋内の測定点）です。"
            ),
        }
        for widget, text in tooltips.items():
            if widget is not None:
                widget.setToolTip(text)
                widget.setWhatsThis(text)
                # Qt does not propagate a spin box's tooltip to its embedded
                # line edit — the cursor sits on the line edit, so mirror the
                # text there or the text area shows nothing.
                line_edit = widget.lineEdit() if isinstance(widget, QAbstractSpinBox) else None
                if line_edit is not None:
                    line_edit.setToolTip(text)

        for code, combo in self.campaign_applicability_state.items():
            combo.setToolTip(
                "この検証領域の適用可否の確認方法です。"
                "未確認=まだ評価していない、自動評価=ソフトが判定、手動証拠=外部の証拠で確認済み。"
            )
        for field in self.campaign_applicability_detail.values():
            field.setToolTip("適用可否の根拠となる確認メモまたは証明IDです。")

        # Column meanings — the headers stay short, the tooltip carries the
        # definition so a first-time reader can decode the table.
        column_tooltips: tuple[tuple[QTreeWidget, dict[int, str]], ...] = (
            (self.search_axis_tree, {
                0: "探索対象の物体",
                1: "動かす方向（X=幅、Y=奥行き、Z=高さ）",
                2: "探索範囲の下限（m）",
                3: "探索範囲の上限（m）",
                4: "候補を生成する間隔（m）",
            }),
            (self.search_spec_tree, {
                0: "探索設定の名前",
                1: "設定作成時の部屋・制約との一致状態（古い場合は要再設定）",
                2: "この設定が現在使える状態かどうか",
            }),
            (self.search_candidate_tree, {
                0: "候補の名前",
                1: "生成順の番号",
                2: "物体の配置座標（m）",
            }),
            (self.measurement_plan_tree, {
                0: "実測予定の候補名",
                1: "計画の進行状態",
                2: "計画の保存状態",
                3: "実測結果との突き合わせ状況",
            }),
            (self.pareto_tree, {
                0: "候補の名前",
                1: "Pareto支配関係（他候補に全指標で負けていないか）",
                2: "支配関係の判定理由",
                3: "各評価指標の値",
            }),
            (self.campaign_assignment_tree, {
                0: "検証対象の候補名",
                1: "検証内での役割（基準・比較など）",
            }),
            (self.campaign_tree, {
                0: "検証条件の名前",
                1: "使用する予測モデルのバージョン",
                2: "対象の候補",
                3: "検証実施に必要な条件の充足状況",
            }),
            (self.validation_tree, {
                0: "検証の名前",
                1: "検証した周波数帯・範囲",
                2: "予測と実測の差（小さいほど一致）",
                3: "周波数変化への追従傾向",
                4: "入力変化に対する出力変化の大きさ",
                5: "同じ条件で測り直したときの一致度",
                6: "この検証結果を配置推奨の根拠に使えるか",
            }),
            (self.adaptive_tree, {
                0: "計画名または候補名",
                1: "対象の範囲",
                2: "既に取得済みの測定値",
                3: "モデル不確かさの補正指標",
            }),
            (self.extended_axis_tree, {
                0: "対象スピーカー",
                1: "角度パラメーター（音響照準・筐体ヨー）",
                2: "角度の下限（°）",
                3: "角度の上限（°）",
                4: "角度の刻み（°）",
            }),
            (self.extended_spec_tree, {
                0: "拡張探索設定の名前",
                1: "対象モデル",
                2: "探索する角度パラメーター",
                3: "この設定が現在使える状態かどうか",
            }),
            (self.extended_candidate_tree, {
                0: "拡張候補の名前",
                1: "派生元の候補",
                2: "物体の配置座標（m）",
                3: "音響照準のヨー角（°）",
                4: "筐体のヨー角（°）",
            }),
            (self.adaptive_extended_tree, {
                0: "計画名または候補名",
                1: "対象の範囲",
                2: "既に取得済みの測定値",
                3: "候補の特徴量・指標",
            }),
        )
        for tree, tips in column_tooltips:
            if tree is None:
                continue
            header = tree.headerItem()
            for column, text in tips.items():
                header.setToolTip(column, text)

    @staticmethod
    def _number_field(
        minimum: float,
        maximum: float,
        value: float,
        decimals: int,
    ) -> QDoubleSpinBox:
        field = QDoubleSpinBox()
        field.setRange(minimum, maximum)
        field.setDecimals(decimals)
        field.setValue(value)
        return field

    @staticmethod
    def _angle_field(
        *,
        minimum: float = -180.0,
        maximum: float = 180.0,
        value: float = 0.0,
    ) -> QDoubleSpinBox:
        field = QDoubleSpinBox()
        field.setRange(minimum, maximum)
        field.setDecimals(1)
        field.setSingleStep(1.0)
        field.setValue(value)
        field.setSuffix("°")
        return field


__all__ = ["OptimizationWorkflowController"]
