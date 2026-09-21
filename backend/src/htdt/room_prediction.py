from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Event

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .cad_constraint_repository import CadConstraintRepository
from .cad_prediction_jobs import PredictionJobApplyContext, PredictionJobGuard, PredictionJobToken
from .cad_prediction_models import CadPredictionResult
from .cad_prediction_repository import CadPredictionRepository
from .cad_prediction_request import RectangularGeometryRequestIdentity, rectangular_geometry_request_identity
from .cad_predictions import analyze_native_rectangular_geometry
from .cad_repository import SceneRepository, SceneRevision
from .cad_scene import acoustic_reference_position, scene_content_hash
from .cad_search_models import constraint_workspace_snapshot
from .native_worker import WORKER_CANCELLED, NativeWorker, NativeWorkerPool
from .room_workspace import RoomWorkspaceController
from .ui_theme import (
    SemanticState,
    SurfaceRole,
    TypographyRole,
    set_semantic_state,
    set_surface_role,
    set_typography_role,
)


@dataclass(frozen=True, slots=True)
class RoomPredictionRunSpec:
    revision: SceneRevision
    receiver_entity_id: str
    max_mode_hz: float
    sound_speed_m_s: float
    constraint_workspace_hash: str
    identity: RectangularGeometryRequestIdentity
    token: PredictionJobToken


@dataclass(frozen=True, slots=True)
class RoomPredictionRunState:
    busy: bool
    message: str
    error: bool = False


class RoomPredictionController(QObject):
    """N70 application adapter for the UX120 Room workspace.

    Identity, stale/cancel semantics and immutable persistence are delegated to the
    existing N70 request identity, PredictionJobGuard and CadPredictionRepository.
    """

    stateChanged = Signal(object)
    resultsChanged = Signal()
    runSelected = Signal(object)

    def __init__(
        self,
        scene_repository: SceneRepository,
        room_controller: RoomWorkspaceController,
        *,
        parent: QObject | None = None,
        operation: Callable[
            [RoomPredictionRunSpec, Event],
            tuple[CadPredictionResult, ...] | None,
        ] | None = None,
    ) -> None:
        super().__init__(parent)
        self.scene_repository = scene_repository
        self.room_controller = room_controller
        self.document_id = room_controller.document_id
        self.prediction_repository = CadPredictionRepository(scene_repository)
        self.constraint_repository = CadConstraintRepository(scene_repository.path)
        self.job_guard = PredictionJobGuard()
        self._operation = operation or self._analyze
        self._tokens: dict[str, PredictionJobToken] = {}
        self._specs: dict[str, RoomPredictionRunSpec] = {}
        self._pool = NativeWorkerPool(self)
        self._completion_states: dict[str, RoomPredictionRunState] = {}
        self._current_job_id: str | None = None
        self._selected_run_id: str | None = None
        self._disposed = False

    @property
    def _tasks(self) -> dict[str, tuple[QThread, NativeWorker]]:
        """Live worker records owned by ``self._pool`` (kept for tests)."""
        return self._pool.tasks

    @property
    def active_worker_count(self) -> int:
        return self._pool.active_count

    @property
    def is_busy(self) -> bool:
        return self._current_job_id is not None or self.active_worker_count > 0

    @property
    def selected_run_id(self) -> str | None:
        return self._selected_run_id

    def receiver_options(self) -> tuple[tuple[str, str], ...]:
        document = self.room_controller.committed_document
        return tuple(
            (entity.entity_id, f"{entity.name} · {entity.kind}")
            for entity in document.entities
            if acoustic_reference_position(entity) is not None
        )

    def _constraint_hash(self) -> str:
        constraint_set = self.constraint_repository.load(self.document_id)
        _snapshot, digest = constraint_workspace_snapshot(constraint_set)
        return digest

    def _saved_target(self, receiver_entity_id: str) -> SceneRevision:
        working = self.room_controller.working
        if working.source_revision_id is None:
            raise ValueError("保存済みSceneRevisionが必要です")
        if working.has_preview:
            raise ValueError("編集中の操作を確定またはキャンセルしてください")
        if working.is_dirty:
            raise ValueError("予測の前に現在の配置を保存してください")
        revision = self.scene_repository.get(working.source_revision_id)
        if revision is None:
            raise ValueError("現在のSceneRevisionを読み込めません")
        if revision.content_hash != scene_content_hash(working.committed_document):
            raise ValueError("現在のSceneRevisionと編集状態が一致しません")
        entity = revision.document.entity(receiver_entity_id)
        if acoustic_reference_position(entity) is None:
            raise ValueError("選択した受音点に音響基準点がありません")
        return revision

    def prepare_run(
        self,
        receiver_entity_id: str,
        *,
        max_mode_hz: float = 300.0,
        sound_speed_m_s: float = 343.0,
    ) -> RoomPredictionRunSpec:
        revision = self._saved_target(receiver_entity_id)
        identity = rectangular_geometry_request_identity(
            revision,
            receiver_entity_id,
            max_mode_hz=max_mode_hz,
            sound_speed_m_s=sound_speed_m_s,
        )
        constraint_hash = self._constraint_hash()
        token = self.job_guard.submit(
            revision,
            model_id=identity.model_id,
            model_version=identity.model_version,
            parameters_json=identity.parameters_json,
            input_hash=identity.input_hash,
            constraint_workspace_hash=constraint_hash,
        )
        return RoomPredictionRunSpec(
            revision=revision,
            receiver_entity_id=receiver_entity_id,
            max_mode_hz=float(max_mode_hz),
            sound_speed_m_s=float(sound_speed_m_s),
            constraint_workspace_hash=constraint_hash,
            identity=identity,
            token=token,
        )

    @staticmethod
    def _analyze(
        spec: RoomPredictionRunSpec,
        cancel_event: Event,
    ) -> tuple[CadPredictionResult, ...] | None:
        if cancel_event.is_set():
            return None
        return analyze_native_rectangular_geometry(
            spec.revision,
            spec.receiver_entity_id,
            max_mode_hz=spec.max_mode_hz,
            sound_speed_m_s=spec.sound_speed_m_s,
            constraint_workspace_hash=spec.constraint_workspace_hash,
        )

    def start(
        self,
        receiver_entity_id: str,
        *,
        max_mode_hz: float = 300.0,
        sound_speed_m_s: float = 343.0,
    ) -> bool:
        if self._disposed:
            return False
        if self.is_busy:
            self.stateChanged.emit(RoomPredictionRunState(True, "予測を実行中です"))
            return False
        try:
            spec = self.prepare_run(
                receiver_entity_id,
                max_mode_hz=max_mode_hz,
                sound_speed_m_s=sound_speed_m_s,
            )
        except Exception as exc:
            self.stateChanged.emit(
                RoomPredictionRunState(False, f"予測を開始できません · {exc}", error=True)
            )
            return False

        self._tokens[spec.token.job_id] = spec.token
        self._specs[spec.token.job_id] = spec
        self._current_job_id = spec.token.job_id
        self.stateChanged.emit(RoomPredictionRunState(True, "予測を計算しています…"))
        self._pool.start(
            spec.token.job_id,
            lambda cancel_event: self._operation(spec, cancel_event),
            self._task_completed,
            on_finished=self._task_thread_finished,
        )
        return True

    def cancel(self) -> bool:
        job_id = self._current_job_id
        if job_id is None:
            return False
        token = self._tokens.get(job_id)
        if token is not None:
            self.job_guard.cancel(token)
        self._pool.cancel(job_id)
        self._current_job_id = None
        self.stateChanged.emit(
            RoomPredictionRunState(
                True,
                "キャンセル処理中です。遅延結果は保存・適用しません",
            )
        )
        return True

    def _current_apply_context(self) -> PredictionJobApplyContext | None:
        working = self.room_controller.working
        if working.source_revision_id is None:
            return None
        return PredictionJobApplyContext(
            document_id=self.document_id,
            scene_revision_id=working.source_revision_id,
            scene_content_hash=scene_content_hash(working.committed_document),
            constraint_workspace_hash=self._constraint_hash(),
        )

    @staticmethod
    def _result_matches_token(
        result: CadPredictionResult,
        token: PredictionJobToken,
    ) -> bool:
        return (
            result.input_hash == token.input_hash
            and result.model_id == token.model_id
            and result.model_version == token.model_version
            and result.scene_revision_id == token.scene_revision_id
            and result.scene_content_hash == token.scene_content_hash
            and result.constraint_workspace_hash == token.constraint_workspace_hash
        )

    def accept_results(
        self,
        spec: RoomPredictionRunSpec,
        results: object,
    ) -> tuple[CadPredictionResult, ...] | None:
        token = spec.token
        if self.job_guard.is_cancelled(token):
            return None
        if (
            not isinstance(results, tuple)
            or not results
            or not all(isinstance(item, CadPredictionResult) for item in results)
        ):
            raise ValueError("prediction result contract mismatch")
        typed = results
        if any(not self._result_matches_token(item, token) for item in typed):
            raise ValueError("prediction immutable input identity mismatch")
        context = self._current_apply_context()
        if context is None or not self.job_guard.can_apply(token, context):
            return None
        for item in typed:
            self.prediction_repository.save(item)
        self._selected_run_id = typed[0].run_id
        return typed

    @Slot(object, object, object)
    def _task_completed(self, key: object, result: object, error: object) -> None:
        if self._disposed:
            return
        job_id = str(key)
        token = self._tokens.pop(job_id, None)
        spec = self._specs.pop(job_id, None)
        if self._current_job_id == job_id:
            self._current_job_id = None
        if token is None or spec is None:
            self._completion_states[job_id] = RoomPredictionRunState(
                False,
                "予測処理を終了しました",
            )
            return

        final_state: RoomPredictionRunState
        if self.job_guard.is_cancelled(token) or error == WORKER_CANCELLED:
            final_state = RoomPredictionRunState(False, "予測はキャンセルされました")
        elif error is not None:
            final_state = RoomPredictionRunState(
                False,
                f"予測に失敗しました · {error}",
                error=True,
            )
        else:
            try:
                accepted = self.accept_results(spec, result)
            except ValueError as exc:
                final_state = RoomPredictionRunState(
                    False,
                    f"予測結果を拒否しました · {exc}",
                    error=True,
                )
            else:
                if accepted is None:
                    final_state = RoomPredictionRunState(
                        False,
                        "条件が変更されたため古い予測結果を破棄しました",
                    )
                else:
                    self.resultsChanged.emit()
                    self.runSelected.emit(accepted)
                    compatibility = accepted[0].geometry_compatibility
                    final_state = RoomPredictionRunState(
                        False,
                        (
                            "現在の部屋形状は矩形幾何modelの対象外です"
                            if compatibility == "unsupported"
                            else "予測を保存しました"
                        ),
                    )

        # Keep the worker visible as busy until QThread has actually emitted
        # finished. This prevents workspace disposal/restore/new-run races in the
        # short interval after the worker result signal but before thread teardown.
        self._completion_states[job_id] = final_state
        self.stateChanged.emit(
            RoomPredictionRunState(True, "予測処理を終了しています…")
        )

    def _task_thread_finished(self, job_id: str) -> None:
        """Emit the stashed final state once the worker thread has stopped."""
        if self._disposed:
            return
        final_state = self._completion_states.pop(
            job_id,
            RoomPredictionRunState(False, "予測処理を終了しました"),
        )
        self.stateChanged.emit(final_state)


    def list_run_ids(self) -> tuple[str, ...]:
        run_ids: list[str] = []
        for result in self.prediction_repository.list_results(self.document_id):
            if result.run_id not in run_ids:
                run_ids.append(result.run_id)
        return tuple(run_ids)

    def results_for_run(self, run_id: str) -> tuple[CadPredictionResult, ...]:
        return self.prediction_repository.list_run(run_id)

    def result_is_current(self, result: CadPredictionResult) -> bool:
        working = self.room_controller.working
        return bool(
            working.source_revision_id == result.scene_revision_id
            and scene_content_hash(working.committed_document) == result.scene_content_hash
            and self._constraint_hash() == result.constraint_workspace_hash
        )

    def select_run(self, run_id: str | None) -> tuple[CadPredictionResult, ...]:
        self._selected_run_id = run_id
        results = () if run_id is None else self.results_for_run(run_id)
        self.runSelected.emit(results)
        return results

    def refresh_selection(self) -> tuple[CadPredictionResult, ...]:
        run_ids = self.list_run_ids()
        if self._selected_run_id not in run_ids:
            current_run = None
            for run_id in reversed(run_ids):
                results = self.results_for_run(run_id)
                if results and self.result_is_current(results[0]):
                    current_run = run_id
                    break
            self._selected_run_id = current_run or (run_ids[-1] if run_ids else None)
        return self.select_run(self._selected_run_id)

    def before_deactivate(self) -> tuple[bool, str | None]:
        if self.is_busy:
            return False, "予測の完了またはキャンセル後に画面を切り替えてください"
        return True, None

    def dispose(self) -> None:
        self._disposed = True
        for token in tuple(self._tokens.values()):
            self.job_guard.cancel(token)
        report = self._pool.shutdown()
        if not report.all_stopped:
            self.stateChanged.emit(
                RoomPredictionRunState(
                    False,
                    "予測処理の停止が遅延しています · 遅延結果は保存・適用しません",
                )
            )
        self._tokens.clear()
        self._specs.clear()
        self._completion_states.clear()
        self._current_job_id = None


class RoomPredictionPanel(QWidget):
    """Dark-first prediction controls for the Room acoustics context."""

    runRequested = Signal()
    cancelRequested = Signal()

    def __init__(
        self,
        controller: RoomPredictionController,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setMinimumWidth(300)
        self.setMaximumWidth(390)
        set_surface_role(self, SurfaceRole.RAISED)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        title = QLabel("予測")
        set_typography_role(title, TypographyRole.SECTION_TITLE)
        layout.addWidget(title)

        description = QLabel(
            "矩形幾何モデルのルームモードと一次反射候補です。実測FRやSPL音場ではありません。"
        )
        description.setWordWrap(True)
        set_typography_role(description, TypographyRole.SECONDARY)
        layout.addWidget(description)

        form = QFormLayout()
        self.receiver = QComboBox()
        form.addRow("受音点", self.receiver)
        self.max_mode = QDoubleSpinBox()
        self.max_mode.setRange(20.0, 1000.0)
        self.max_mode.setValue(300.0)
        self.max_mode.setSuffix(" Hz")
        form.addRow("モード上限", self.max_mode)
        self.sound_speed = QDoubleSpinBox()
        self.sound_speed.setRange(250.0, 400.0)
        self.sound_speed.setDecimals(2)
        self.sound_speed.setValue(343.0)
        self.sound_speed.setSuffix(" m/s")
        form.addRow("音速", self.sound_speed)
        layout.addLayout(form)

        action_row = QHBoxLayout()
        self.run_button = QPushButton("予測実行")
        self.cancel_button = QPushButton("キャンセル")
        self.cancel_button.setEnabled(False)
        self.run_button.clicked.connect(self.run_prediction)
        self.cancel_button.clicked.connect(controller.cancel)
        action_row.addWidget(self.run_button)
        action_row.addWidget(self.cancel_button)
        layout.addLayout(action_row)

        self.state = QLabel("保存済み予測なし")
        self.state.setWordWrap(True)
        layout.addWidget(self.state)

        self.runs = QTreeWidget()
        self.runs.setHeaderLabels(["予測", "状態"])
        self.runs.setMinimumHeight(150)
        self.runs.itemSelectionChanged.connect(self._selected)
        layout.addWidget(self.runs)

        self.detail = QLabel()
        self.detail.setWordWrap(True)
        set_typography_role(self.detail, TypographyRole.SECONDARY)
        layout.addWidget(self.detail)
        layout.addStretch(1)

        controller.stateChanged.connect(self._state_changed)
        controller.resultsChanged.connect(self.refresh)
        controller.runSelected.connect(self.show_selected_results)
        self.refresh()

    def refresh(self) -> None:
        previous = self.controller.selected_run_id
        options = self.controller.receiver_options()
        previous_receiver = self.receiver.currentData()
        self.receiver.clear()
        for entity_id, label in options:
            self.receiver.addItem(label, entity_id)
        preferred = self.controller.room_controller.selected_id or previous_receiver
        if preferred is not None:
            index = self.receiver.findData(preferred)
            if index >= 0:
                self.receiver.setCurrentIndex(index)

        self.runs.clear()
        selected_item = None
        run_ids = self.controller.list_run_ids()
        for index, run_id in enumerate(run_ids, start=1):
            results = self.controller.results_for_run(run_id)
            if not results:
                continue
            first = results[0]
            current = self.controller.result_is_current(first)
            compatibility = {
                "exact_for_model_geometry": "矩形model対応",
                "rectangular_approximation": "近似",
                "unsupported": "非対応",
            }.get(first.geometry_compatibility, first.geometry_compatibility)
            item = QTreeWidgetItem(
                [f"予測 {index}", "現在" if current else "要再計算"]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, run_id)
            item.setToolTip(0, compatibility)
            self.runs.addTopLevelItem(item)
            if run_id == previous:
                selected_item = item

        if selected_item is None and self.runs.topLevelItemCount():
            selected_item = self.runs.topLevelItem(self.runs.topLevelItemCount() - 1)
        if selected_item is not None:
            self.runs.setCurrentItem(selected_item)
        else:
            self.controller.select_run(None)
            self._show_results(())

    def run_prediction(self) -> bool:
        receiver = self.receiver.currentData()
        if receiver is None:
            self._state_changed(
                RoomPredictionRunState(False, "受音点を選択してください", error=True)
            )
            return False
        return self.controller.start(
            str(receiver),
            max_mode_hz=float(self.max_mode.value()),
            sound_speed_m_s=float(self.sound_speed.value()),
        )

    def _selected(self) -> None:
        item = self.runs.currentItem()
        run_id = (
            None
            if item is None
            else item.data(0, Qt.ItemDataRole.UserRole)
        )
        results = self.controller.select_run(None if run_id is None else str(run_id))
        self._show_results(results)

    def show_selected_results(self, results: object) -> None:
        if not isinstance(results, tuple):
            return
        self._show_results(results)

    def _show_results(self, results: tuple[CadPredictionResult, ...]) -> None:
        if not results:
            self.detail.setText("予測結果を選択してください")
            return
        first = results[0]
        current = self.controller.result_is_current(first)
        modes = next((item for item in results if item.result_kind == "geometry_modes"), None)
        reflections = next(
            (item for item in results if item.result_kind == "geometry_reflections"),
            None,
        )
        mode_count = 0 if modes is None else len(modes.modes)
        reflection_count = 0 if reflections is None else len(reflections.reflections)
        state = "現在の条件に一致" if current else "条件が変更されています。再計算してください"
        warning = " / ".join(first.warnings[:3]) or "なし"
        self.detail.setText(
            f"{state}\n"
            f"model: {first.model_id} / {first.model_version}\n"
            f"room mode: {mode_count} · 一次反射候補: {reflection_count}\n"
            f"warning: {warning}"
        )
        set_semantic_state(
            self.detail,
            None if current else SemanticState.STALE,
        )

    def _state_changed(self, state: RoomPredictionRunState) -> None:
        self.state.setText(state.message)
        set_semantic_state(
            self.state,
            SemanticState.ERROR if state.error else None,
        )
        self.run_button.setEnabled(not state.busy)
        self.cancel_button.setEnabled(state.busy)
        if not state.busy:
            self.refresh()


__all__ = [
    "RoomPredictionController",
    "RoomPredictionPanel",
    "RoomPredictionRunSpec",
    "RoomPredictionRunState",
]
