from __future__ import annotations

from collections.abc import Callable
from uuid import uuid4

from PySide6.QtCore import QObject, QSignalBlocker, QThread, Qt, Signal, Slot
from PySide6.QtWidgets import (
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
)

from .cad_adaptive_extended_repository import CadAdaptiveExtendedRepository
from .cad_adaptive_extended_service import CadAdaptiveExtendedPlannerService
from .cad_adaptive_repository import CadAdaptivePlanRepository
from .cad_adaptive_service import CadAdaptivePlannerService
from .cad_constraint_repository import CadConstraintRepository
from .cad_extended_search import CadExtendedCandidateSetPage, CadExtendedSearchAxis
from .cad_extended_search_repository import CadExtendedSearchRepository
from .cad_measurement_jobs import (
    MeasurementJobApplyContext,
    MeasurementJobGuard,
    MeasurementJobToken,
)
from .cad_measurement_models import CadMeasurementRecord
from .cad_measurement_repository import CadMeasurementRepository
from .cad_measurements import measurement_record_for_revision, normalize_rew_api_snapshot
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
from .native_editor import ROLE
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
    "seat.pairwise_rms_difference_max_db": "座席間差 最大",
    "seat.pairwise_rms_difference_rms_db": "座席間差 RMS",
    "seat.pairwise_shape_max_db": "座席間形状差 最大",
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

    def __init__(self, repository: SceneRepository, document_id: str) -> None:
        QObject.__init__(self)
        self.repository = repository
        self.document_id = document_id
        self.scene = RoomWorkspaceController(repository, document_id)
        self._selected_id = self.scene.view_state.selected_id
        self.constraint_repository = CadConstraintRepository(repository.path)
        self.constraint_set = self.constraint_repository.load(document_id)

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
        self.robustness_repository = CadRobustnessRepository(
            scene_repository=repository,
            search_repository=self.search_repository,
            objective_repository=self.objective_repository,
            extended_search_repository=self.extended_repository,
        )
        self.adaptive_extended_repository = CadAdaptiveExtendedRepository(
            self.extended_repository,
            self.validation_repository,
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
        self._disposed = False

        self.viewport = None
        self._render_scene: Callable[[bool], None] | None = None
        self._status_proxy = _StatusProxy(self.statusChanged.emit)

        self.rew_client = RewApiClient()
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
            return False, "REW読込が完了するまで画面を切り替えられません"
        return self.scene.before_deactivate()

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
            self._rebuild()
            self._set_dirty_status()
        return changed

    def redo(self) -> bool:
        changed = self.scene.redo()
        if changed:
            self._selected_id = self.scene.view_state.selected_id
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
            self.statusChanged.emit(
                "Pareto比較を更新できません · 部屋または制約が変更されています"
            )
            return

        evaluations = self.objective_repository.latest_evaluations_by_candidate(spec_id)
        if not evaluations:
            self.objective_list.clear()
            self.pareto_tree.clear()
            self.pareto_summary_label.setText("この探索設定には比較できる指標データがありません")
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
                return
            if any(
                metric_map[objective_id].unit != expected_units[objective_id]
                for objective_id in available
            ):
                self.pareto_tree.clear()
                self.pareto_summary_label.setText(
                    "候補間で指標の単位が一致しないため、Pareto比較を中止しました"
                )
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
                spec_id, built.pareto_sha256
            )
            pareto_set = existing or built
            if existing is None:
                self.objective_repository.save_pareto_set(pareto_set)
        except Exception as exc:
            self.pareto_tree.clear()
            self.pareto_summary_label.setText(f"Pareto比較を作成できません · {exc}")
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
        self._start_rew_task(key, self.rew_client.list_measurements)
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
            raise ValueError("Measurement Planを選択してください")
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
            lambda: self.rew_client.get_frequency_response_snapshot(
                external_id, ppo=None, unit="SPL", smoothing=None
            ),
        )
        self.statusChanged.emit("REWを読み込んでいます…")

    def _start_rew_task(self, key: str, operation: Callable[[], object]) -> None:
        if self._disposed:
            return
        self._rew_pool.start(
            key,
            lambda _cancel_event: operation(),
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
                self.statusChanged.emit(f"REW一覧取得失敗 · {error}")
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
                self.statusChanged.emit(f"REW読込失敗 · {error}")
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
        except Exception as exc:
            self.statusChanged.emit(f"REW結果保存失敗 · {exc}")
            return
        self.refresh_measurement_plans()
        self.refresh_validation_campaigns()
        self.statusChanged.emit("REW測定を保存しました")

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
        self.search_axis_tree = QTreeWidget()
        self.search_axis_tree.setHeaderLabels(["物体", "軸", "最小", "最大", "刻み"])
        self.search_save_button = QPushButton("探索設定を保存")
        self.search_save_button.clicked.connect(self.save_search_spec)
        self.search_spec_tree = QTreeWidget()
        self.search_spec_tree.setHeaderLabels(["探索設定", "入力状態", "状態"])
        self.search_spec_tree.itemSelectionChanged.connect(self._search_spec_selected)
        self.search_generate_button = QPushButton("候補を生成")
        self.search_generate_button.clicked.connect(self.generate_search_candidates_async)
        self.search_cancel_button = QPushButton("生成をキャンセル")
        self.search_cancel_button.clicked.connect(self.cancel_search_generation)
        self.search_summary_label = QLabel("候補未生成")
        self.search_generate_reason_label = QLabel()
        self.search_generate_reason_label.setWordWrap(True)
        self.search_generate_reason_label.hide()
        self.search_candidate_tree = QTreeWidget()
        self.search_candidate_tree.setHeaderLabels(["候補", "番号", "位置"])
        self.search_candidate_tree.itemSelectionChanged.connect(self._search_candidate_selected)
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
        self.measurement_plan_tree.setHeaderLabels(["実測候補", "状態", "保存状態", "測定"])
        self.measurement_plan_tree.itemSelectionChanged.connect(
            self._measurement_plan_selected
        )
        self.measurement_plan_tree.itemSelectionChanged.connect(
            self._refresh_campaign_measurement_points
        )
        self.measurement_match_list = QListWidget()
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
        self.pareto_summary_label = QLabel("比較指標が未読込です")
        self.pareto_tree = QTreeWidget()
        self.pareto_tree.setHeaderLabels(["候補", "Pareto", "根拠", "指標"])
        self.pareto_tree.itemSelectionChanged.connect(self._pareto_candidate_selected)

        self.campaign_assignment_tree = QTreeWidget()
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
        self.campaign_tree.setHeaderLabels(["検証条件", "モデル", "候補", "準備状況"])
        self.campaign_tree.itemSelectionChanged.connect(self._campaign_selected)
        self.campaign_detail_label = QLabel("検証条件が未選択です")
        self.campaign_applicability_state: dict[str, QComboBox] = {}
        self.campaign_applicability_detail: dict[str, QLineEdit] = {}
        for code in ("geometry", "band", "routing"):
            state = QComboBox()
            state.addItem("未確認", "unverified")
            state.addItem("自動評価", "auto")
            state.addItem("手動証跡", "manual")
            self.campaign_applicability_state[code] = state
            detail = QLineEdit()
            detail.setPlaceholderText("確認メモ / attestation ID")
            self.campaign_applicability_detail[code] = detail

        self.validation_refresh_button = QPushButton("保存済み検証を更新")
        self.validation_refresh_button.clicked.connect(self.refresh_model_validations)
        self.validation_tree = QTreeWidget()
        self.validation_tree.setHeaderLabels(
            ["検証", "範囲", "残差", "傾向", "感度", "再現性", "推薦可否"]
        )
        self.validation_tree.itemSelectionChanged.connect(self._validation_selected)
        self.validation_detail_label = QLabel("検証結果が未選択です")

        self.adaptive_scope_combo = QComboBox()
        self.adaptive_scope_combo.addItem("合成データで開発検証", "development_synthetic")
        self.adaptive_scope_combo.addItem("実室データで本番検証", "production_owned_room")
        self.adaptive_length_scale_field = self._number_field(0.01, 20.0, 0.5, 3)
        self.adaptive_length_scale_field.setSuffix(" m")
        self.adaptive_proposal_limit_field = QSpinBox()
        self.adaptive_proposal_limit_field.setRange(1, 100)
        self.adaptive_proposal_limit_field.setValue(20)
        self.adaptive_build_button = QPushButton("次の測定候補を計算・保存")
        self.adaptive_build_button.clicked.connect(self.build_selected_adaptive_plan)
        self.adaptive_tree = QTreeWidget()
        self.adaptive_tree.setHeaderLabels(["計画 / 候補", "範囲", "取得値", "補正指標"])
        self.adaptive_tree.itemSelectionChanged.connect(self._adaptive_selected)
        self.adaptive_detail_label = QLabel("次候補の計画が未選択です")

        self.extended_capability_combo = QComboBox()
        self.extended_parameter_combo = QComboBox()
        self.extended_parameter_combo.addItem("音響の向き（yaw）", "aim_yaw_deg")
        self.extended_parameter_combo.addItem(
            "筐体の向き（toe-in）", "body_yaw_deg"
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
        self.extended_axis_tree.setHeaderLabels(
            ["スピーカー", "パラメータ", "最小", "最大", "刻み"]
        )
        self.extended_spec_tree = QTreeWidget()
        self.extended_spec_tree.setHeaderLabels(
            ["拡張探索", "モデル", "パラメータ", "状態"]
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
        self.extended_candidate_tree.setHeaderLabels(
            ["候補", "元候補", "位置", "音響 yaw", "筐体 yaw"]
        )
        self.extended_candidate_tree.itemSelectionChanged.connect(
            self._extended_candidate_selected
        )
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
