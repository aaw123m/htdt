from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
import sqlite3
from threading import Event

import pyvista as pv
from PySide6.QtCore import QThread, Qt, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDockWidget,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .cad_prediction_jobs import PredictionJobApplyContext, PredictionJobGuard, PredictionJobToken
from .cad_prediction_models import CadPredictionResult, canonical_prediction_json
from .cad_prediction_repository import CadPredictionRepository
from .field_explorer_panel import FieldExplorerPanel
from .prediction_matrix_service import PredictionMatrixService
from .cad_prediction_request import rectangular_geometry_request_identity
from .cad_predictions import analyze_native_rectangular_geometry
from .cad_repository import SceneRepository, SceneRevision
from .cad_scene import F1_DOCUMENT_ID, acoustic_reference_position, domain_to_render, scene_content_hash
from .measurement_workspace import MeasurementWorkspaceWindow
from .native_editor import ROLE
from .native_worker import WORKER_CANCELLED, NativeWorker, NativeWorkerPool
from .user_facing_error import operation_error_message


class PredictionWorkspaceWindow(MeasurementWorkspaceWindow):
    """N70 product layer: immutable prediction evidence and geometry visualization."""

    def __init__(self, repository: SceneRepository, document_id: str = F1_DOCUMENT_ID) -> None:
        self.prediction_repository = CadPredictionRepository(repository)
        self.matrix_service = PredictionMatrixService(repository, document_id)
        self.matrix_table: QTableWidget | None = None
        self.matrix_status_label: QLabel | None = None
        self.prediction_job_guard = PredictionJobGuard()
        self.prediction_selected_run_id: str | None = None
        self.prediction_tree: QTreeWidget | None = None
        self.prediction_summary_label: QLabel | None = None
        self.prediction_detail_label: QLabel | None = None
        self.prediction_receiver_combo: QComboBox | None = None
        self.prediction_max_mode_field: QDoubleSpinBox | None = None
        self.prediction_sound_speed_field: QDoubleSpinBox | None = None
        self.prediction_run_button: QPushButton | None = None
        self.prediction_cancel_button: QPushButton | None = None
        self.prediction_reflection_checkbox: QCheckBox | None = None
        self.prediction_scalar_button: QPushButton | None = None
        self.field_explorer_panel: FieldExplorerPanel | None = None
        self.field_explorer_dock: QDockWidget | None = None
        self._prediction_actor_names: set[str] = set()
        # Parentless until self is a QObject; reparented right after super().
        self._prediction_pool = NativeWorkerPool()
        self._prediction_tokens: dict[str, PredictionJobToken] = {}
        self._current_prediction_token_id: str | None = None
        super().__init__(repository, document_id)
        self._prediction_pool.setParent(self)
        self.setWindowTitle('Home Theater Digital Twin — 予測CAD')
        self._create_prediction_dock()
        self._create_field_explorer_dock()
        self._refresh_prediction_receivers()
        self._refresh_prediction_results()

    def _create_prediction_dock(self) -> None:
        panel = QWidget()
        layout = QVBoxLayout(panel)

        self.prediction_summary_label = QLabel('保存済み予測なし')
        self.prediction_summary_label.setWordWrap(True)
        layout.addWidget(self.prediction_summary_label)

        identity_label = QLabel(
            'モデル: htdt.rectangular_geometry / rect-room-geometry-1\n'
            '室モード周波数と一次反射の幾何候補のみ。実測FR・SPL音場ではありません。'
        )
        identity_label.setWordWrap(True)
        layout.addWidget(identity_label)

        form = QFormLayout()
        self.prediction_receiver_combo = QComboBox()
        self.prediction_receiver_combo.setMinimumContentsLength(18)
        form.addRow('受音点', self.prediction_receiver_combo)

        self.prediction_max_mode_field = QDoubleSpinBox()
        self.prediction_max_mode_field.setRange(20.0, 1000.0)
        self.prediction_max_mode_field.setDecimals(1)
        self.prediction_max_mode_field.setValue(300.0)
        self.prediction_max_mode_field.setSuffix(' Hz')
        form.addRow('モード上限', self.prediction_max_mode_field)

        self.prediction_sound_speed_field = QDoubleSpinBox()
        self.prediction_sound_speed_field.setRange(250.0, 400.0)
        self.prediction_sound_speed_field.setDecimals(2)
        self.prediction_sound_speed_field.setValue(343.0)
        self.prediction_sound_speed_field.setSuffix(' m/s')
        form.addRow('音速', self.prediction_sound_speed_field)
        layout.addLayout(form)

        action_row = QHBoxLayout()
        self.prediction_run_button = QPushButton('矩形幾何予測を実行')
        self.prediction_run_button.setToolTip(
            '保存済みシーンリビジョンとモデル入力ハッシュへ固定してGUI スレッド外で計算します'
        )
        self.prediction_run_button.clicked.connect(self.run_rectangular_geometry_prediction_async)
        action_row.addWidget(self.prediction_run_button)

        self.prediction_cancel_button = QPushButton('キャンセル')
        self.prediction_cancel_button.clicked.connect(self.cancel_prediction)
        action_row.addWidget(self.prediction_cancel_button)
        layout.addLayout(action_row)

        self.prediction_reflection_checkbox = QCheckBox('一次反射パスを3D表示')
        self.prediction_reflection_checkbox.setChecked(True)
        self.prediction_reflection_checkbox.toggled.connect(lambda _checked: self._rebuild())
        layout.addWidget(self.prediction_reflection_checkbox)

        self.prediction_scalar_button = QPushButton('音場ヒートマップ / スライス / ボリューム')
        self.prediction_scalar_button.setEnabled(False)
        self.prediction_scalar_button.setToolTip(
            '厳密矩形モデルのモード結果を持つ実行選択時に有効になります'
        )
        self.prediction_scalar_button.clicked.connect(self._open_field_explorer)
        layout.addWidget(self.prediction_scalar_button)

        self.prediction_tree = QTreeWidget()
        self.prediction_tree.setHeaderLabels(['予測結果', '入力版'])
        self.prediction_tree.setMinimumHeight(220)
        self.prediction_tree.itemSelectionChanged.connect(self._prediction_tree_selected)
        layout.addWidget(self.prediction_tree)

        self.prediction_detail_label = QLabel(
            '予測結果を選択するとモデル / 仮定 / 互換性 / 古い状態を表示します'
        )
        self.prediction_detail_label.setWordWrap(True)
        layout.addWidget(self.prediction_detail_label)

        self.matrix_status_label = QLabel('行列なし')
        self.matrix_status_label.setWordWrap(True)
        layout.addWidget(self.matrix_status_label)
        self.matrix_table = QTableWidget()
        self.matrix_table.setMinimumHeight(140)
        self.matrix_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        layout.addWidget(self.matrix_table)
        matrix_refresh_button = QPushButton('行列を再読み込み')
        matrix_refresh_button.clicked.connect(self.refresh_matrix_dock)
        layout.addWidget(matrix_refresh_button)
        self.refresh_matrix_dock()

        layout.addStretch(1)

        dock = QDockWidget('予測', self)
        dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        self._unify_right_context_docks(dock)
        dock.raise_()

    def refresh_matrix_dock(self) -> None:
        """O531: render the persisted source x receiver grid (#986)."""
        if self.matrix_table is None:
            return
        presentation = self.matrix_service.matrix_presentation()
        self.matrix_table.clear()
        if presentation.spec_id is None:
            self.matrix_table.setRowCount(0)
            self.matrix_table.setColumnCount(0)
            if self.matrix_status_label is not None:
                self.matrix_status_label.setText(
                    f"行列なし · {presentation.reason or ''}"
                )
            return
        sources = presentation.source_labels
        receivers = presentation.receiver_labels
        self.matrix_table.setColumnCount(len(sources))
        self.matrix_table.setRowCount(len(receivers))
        self.matrix_table.setHorizontalHeaderLabels(list(sources))
        self.matrix_table.setVerticalHeaderLabels(list(receivers))
        cells = {
            (cell.matrix_source_id, cell.matrix_receiver_id): cell
            for cell in presentation.cells
        }
        spec = self.matrix_service.repository.get_spec(presentation.spec_id)
        for row, receiver in enumerate(spec.receivers):
            for column, source in enumerate(spec.sources):
                cell = cells.get(
                    (source.matrix_source_id, receiver.matrix_receiver_id)
                )
                text = '' if cell is None else (
                    cell.state
                    + (f'·{cell.blocked_reason}' if cell.blocked_reason else '')
                )
                self.matrix_table.setItem(
                    row, column, QTableWidgetItem(text)
                )
        if self.matrix_status_label is not None:
            parts = [presentation.spec_name]
            if presentation.run_state is not None:
                parts.append(
                    f"実行 {presentation.run_attempt}: "
                    f"{presentation.run_state}"
                )
            if presentation.currency_state is not None:
                parts.append(f'鮮度 {presentation.currency_state}')
            self.matrix_status_label.setText(' · '.join(parts))

    def _constraint_workspace_hash(self) -> str:
        constraint_set = getattr(self, 'constraint_set', None)
        if constraint_set is None:
            payload = {'document_id': self.document_id, 'constraints': []}
        else:
            payload = constraint_set.model_dump(mode='json')
        canonical = canonical_prediction_json(payload)
        return sha256(canonical.encode('utf-8')).hexdigest()

    def _current_prediction_context(self) -> PredictionJobApplyContext | None:
        if self.working is None or self.working.source_revision_id is None:
            return None
        return PredictionJobApplyContext(
            document_id=self.document_id,
            scene_revision_id=self.working.source_revision_id,
            scene_content_hash=scene_content_hash(self.working.committed_document),
            constraint_workspace_hash=self._constraint_workspace_hash(),
        )

    def _saved_prediction_target(self) -> tuple[SceneRevision, str]:
        if self.working is None or self.working.source_revision_id is None:
            raise ValueError('保存済みシーンリビジョンが必要です')
        if self.working.is_dirty:
            raise ValueError('予測の前に現在の配置を保存してください')
        revision = self.repository.get(self.working.source_revision_id)
        if revision is None:
            raise ValueError('現在のシーンリビジョンを読み込めません')

        receiver_id = None
        if self.prediction_receiver_combo is not None:
            receiver_id = self.prediction_receiver_combo.currentData()
        if receiver_id is None and self.selected_id is not None:
            receiver_id = self.selected_id
        if receiver_id is None:
            raise ValueError('受音点を選択してください')
        entity = revision.document.entity(str(receiver_id))
        if acoustic_reference_position(entity) is None:
            raise ValueError('選択した受音点に音響基準点がありません')
        return revision, str(receiver_id)

    def _refresh_prediction_receivers(self) -> None:
        combo = self.prediction_receiver_combo
        if combo is None or self.working is None:
            return
        previous = combo.currentData()
        combo.clear()
        for entity in self.working.committed_document.entities:
            if acoustic_reference_position(entity) is None:
                continue
            label = f'{entity.name} · {entity.kind}'
            combo.addItem(label, entity.entity_id)
        preferred = self.selected_id if self.selected_id is not None else previous
        if preferred is not None:
            index = combo.findData(preferred)
            if index >= 0:
                combo.setCurrentIndex(index)

    def _inspect(self, entity_id: str | None, *, use_preview: bool = False) -> None:
        super()._inspect(entity_id, use_preview=use_preview)
        combo = self.prediction_receiver_combo
        if combo is None or entity_id is None or use_preview:
            return
        index = combo.findData(entity_id)
        if index >= 0:
            combo.setCurrentIndex(index)

    def save(self) -> None:
        super().save()
        self._refresh_prediction_receivers()
        self._refresh_prediction_results()

    def _rebuild(self, *, reset_camera: bool = False) -> None:
        super()._rebuild(reset_camera=reset_camera)
        if self.prediction_tree is not None:
            self._refresh_prediction_metadata()
            self._render_prediction_overlay()
        self.viewport.render()

    def run_rectangular_geometry_prediction_async(self) -> None:
        try:
            revision, receiver_id = self._saved_prediction_target()
            max_mode_hz = (
                300.0 if self.prediction_max_mode_field is None else float(self.prediction_max_mode_field.value())
            )
            sound_speed = (
                343.0
                if self.prediction_sound_speed_field is None
                else float(self.prediction_sound_speed_field.value())
            )
            identity = rectangular_geometry_request_identity(
                revision,
                receiver_id,
                max_mode_hz=max_mode_hz,
                sound_speed_m_s=sound_speed,
            )
            constraint_hash = self._constraint_workspace_hash()
            token = self.prediction_job_guard.submit(
                revision,
                model_id=identity.model_id,
                model_version=identity.model_version,
                parameters_json=identity.parameters_json,
                input_hash=identity.input_hash,
                constraint_workspace_hash=constraint_hash,
            )
        except Exception as exc:
            self.statusBar().showMessage(f'予測を開始できません · {operation_error_message(exc)}')
            return

        self._prediction_tokens[token.job_id] = token
        self._current_prediction_token_id = token.job_id
        if self.prediction_run_button is not None:
            self.prediction_run_button.setEnabled(False)
        self.statusBar().showMessage(
            f'予測中… リビジョン {revision.revision_id[:8]} · 入力 {identity.input_hash[:8]}'
        )
        self._start_prediction_task(
            token.job_id,
            lambda cancel_event: (
                None
                if cancel_event.is_set()
                else analyze_native_rectangular_geometry(
                    revision,
                    receiver_id,
                    max_mode_hz=max_mode_hz,
                    sound_speed_m_s=sound_speed,
                    constraint_workspace_hash=constraint_hash,
                )
            ),
        )

    def cancel_prediction(self) -> None:
        token_id = self._current_prediction_token_id
        if token_id is None:
            return
        token = self._prediction_tokens.get(token_id)
        if token is not None:
            self.prediction_job_guard.cancel(token)
        self._prediction_pool.cancel(token_id)
        self._current_prediction_token_id = None
        if self.prediction_run_button is not None:
            self.prediction_run_button.setEnabled(True)
        self.statusBar().showMessage('予測をキャンセルしました · 遅延結果は現在シーンへ適用しません')

    def _start_prediction_task(self, key: str, operation: Callable[[Event], object]) -> None:
        if self._disposed:
            return
        self._prediction_pool.start(
            key,
            operation,
            self._prediction_task_completed,
        )

    @property
    def _prediction_tasks(self) -> dict[str, tuple[QThread, NativeWorker]]:
        """Live worker records owned by ``self._prediction_pool``."""
        return self._prediction_pool.tasks

    @Slot(object, object, object)
    def _prediction_task_completed(self, key: object, result: object, error: object) -> None:
        if self._disposed:
            return
        token_id = str(key)
        token = self._prediction_tokens.pop(token_id, None)
        # The pool keeps the task record until QThread.finished so the worker
        # stays busy-visible during the completed -> finished interval.
        if self._current_prediction_token_id == token_id:
            self._current_prediction_token_id = None
            if self.prediction_run_button is not None:
                self.prediction_run_button.setEnabled(True)
        if token is None:
            return
        if self.prediction_job_guard.is_cancelled(token) or error == WORKER_CANCELLED:
            self.statusBar().showMessage('予測キャンセル済み · 結果は保存/適用しません')
            return
        if error is not None:
            self.statusBar().showMessage(f'予測失敗 · {operation_error_message(error)}')
            return
        if not isinstance(result, tuple) or not result or not all(
            isinstance(item, CadPredictionResult) for item in result
        ):
            self.statusBar().showMessage('予測結果を拒否しました · 結果の形式が一致しません')
            return
        if any(
            item.input_hash != token.input_hash
            or item.model_id != token.model_id
            or item.model_version != token.model_version
            or item.scene_revision_id != token.scene_revision_id
            or item.scene_content_hash != token.scene_content_hash
            or item.constraint_workspace_hash != token.constraint_workspace_hash
            for item in result
        ):
            self.statusBar().showMessage('予測結果を拒否しました · 実行時と入力が一致しません')
            return
        context = self._current_prediction_context()
        if context is None or not self.prediction_job_guard.can_apply(token, context):
            self.statusBar().showMessage('古い予測結果を破棄しました · シーン/制約が変更されています')
            return

        try:
            self.prediction_repository.save_run(result)
        except ValueError as exc:
            self.statusBar().showMessage(f'予測結果を拒否しました · {operation_error_message(exc)}')
            return
        except sqlite3.Error as exc:
            self.statusBar().showMessage(f'予測を保存できませんでした · {operation_error_message(exc)}')
            return
        self.prediction_selected_run_id = result[0].run_id
        self._refresh_prediction_results()
        self._rebuild()
        compatibility = result[0].geometry_compatibility
        if compatibility == 'unsupported':
            self.statusBar().showMessage('矩形幾何モデルは現在の部屋形状を非対応として記録しました')
        else:
            self.statusBar().showMessage(
                f'予測を保存しました · リビジョン {result[0].scene_revision_id[:8]} · 実行 {result[0].run_id[:8]}'
            )

    def _selected_prediction_results(self) -> tuple[CadPredictionResult, ...]:
        if self.prediction_selected_run_id is None:
            return ()
        return self.prediction_repository.list_run(self.prediction_selected_run_id)

    def _refresh_prediction_results(self) -> None:
        tree = self.prediction_tree
        if tree is None:
            return
        results = self.prediction_repository.list_results(self.document_id)
        if results and self.prediction_selected_run_id is None:
            self.prediction_selected_run_id = results[-1].run_id
        run_ids: list[str] = []
        for result in results:
            if result.run_id not in run_ids:
                run_ids.append(result.run_id)

        tree.clear()
        selected_item: QTreeWidgetItem | None = None
        for run_id in reversed(run_ids):
            run_results = tuple(item for item in results if item.run_id == run_id)
            first = run_results[0]
            compatibility = {
                'exact_for_model_geometry': '矩形モデル厳密',
                'rectangular_approximation': '矩形近似',
                'unsupported': '非対応',
            }.get(first.geometry_compatibility, first.geometry_compatibility)
            top = QTreeWidgetItem(
                [f'{first.model_id} · {compatibility}', first.scene_revision_id[:8]]
            )
            top.setData(0, ROLE, run_id)
            tree.addTopLevelItem(top)

            modes = next((item for item in run_results if item.result_kind == 'geometry_modes'), None)
            reflections = next(
                (item for item in run_results if item.result_kind == 'geometry_reflections'), None
            )
            if modes is not None:
                mode_group = QTreeWidgetItem([f'室モード候補 · {len(modes.modes)}件', '予測幾何'])
                mode_group.setData(0, ROLE, run_id)
                top.addChild(mode_group)
                for mode in modes.modes[:20]:
                    child = QTreeWidgetItem(
                        [
                            f'{mode.frequency_hz:.1f} Hz · {mode.mode_class} '
                            f'({mode.n_x},{mode.n_y},{mode.n_z})',
                            'FR/SPLではない',
                        ]
                    )
                    child.setData(0, ROLE, run_id)
                    mode_group.addChild(child)
            if reflections is not None:
                reflection_group = QTreeWidgetItem(
                    [f'一次反射幾何候補 · {len(reflections.reflections)}件', '予測幾何']
                )
                reflection_group.setData(0, ROLE, run_id)
                top.addChild(reflection_group)
                for reflection in reflections.reflections[:30]:
                    child = QTreeWidgetItem(
                        [
                            f'{reflection.speaker_role} → {reflection.surface_identity} · '
                            f'+{reflection.excess_delay_ms:.2f} ms',
                            '振幅/位相なし',
                        ]
                    )
                    child.setData(0, ROLE, run_id)
                    reflection_group.addChild(child)
            if run_id == self.prediction_selected_run_id:
                selected_item = top
        if selected_item is not None:
            tree.setCurrentItem(selected_item)
            selected_item.setSelected(True)
        self._refresh_prediction_metadata()
        self._render_prediction_overlay()

    def _create_field_explorer_dock(self) -> None:
        dock = QDockWidget('音場エクスプローラー', self)
        dock.setObjectName('field_explorer_dock')
        self.field_explorer_panel = FieldExplorerPanel(
            self.repository,
            self.prediction_repository,
            self.document_id,
        )
        dock.setWidget(self.field_explorer_panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        self._unify_right_context_docks(dock)
        self.field_explorer_dock = dock

    def _explorable_prediction_results(
        self,
    ) -> tuple[CadPredictionResult, ...] | None:
        results = self._selected_prediction_results()
        modes = next(
            (
                item
                for item in results
                if item.result_kind == 'geometry_modes'
                and item.geometry_compatibility == 'exact_for_model_geometry'
            ),
            None,
        )
        return results if modes is not None else None

    def _open_field_explorer(self) -> None:
        if self.field_explorer_panel is None or self.field_explorer_dock is None:
            return
        run_id = self.prediction_selected_run_id
        if run_id is None:
            return
        if self.field_explorer_panel.open_for_run(run_id):
            self.field_explorer_dock.show()
            self.field_explorer_dock.raise_()

    def _prediction_tree_selected(self) -> None:
        if self.prediction_tree is None:
            return
        items = self.prediction_tree.selectedItems()
        run_id = None if not items else items[0].data(0, ROLE)
        if run_id is not None:
            self.prediction_selected_run_id = str(run_id)
        self._refresh_prediction_metadata()
        self._render_prediction_overlay()
        self.viewport.render()

    def _refresh_prediction_metadata(self) -> None:
        if self.prediction_summary_label is None or self.prediction_detail_label is None:
            return
        results = self._selected_prediction_results()
        if not results:
            self.prediction_summary_label.setText('保存済み予測なし')
            self.prediction_detail_label.setText(
                '予測結果を選択するとモデル / 仮定 / 互換性 / 古い状態を表示します'
            )
            return
        first = results[0]
        current_hash = None if self.working is None else scene_content_hash(self.working.committed_document)
        historical = current_hash != first.scene_content_hash
        state = '過去入力の予測 · 現在シーンには重ねない' if historical else '現在シーンと入力版が一致'
        if self.prediction_scalar_button is not None:
            self.prediction_scalar_button.setEnabled(
                self._explorable_prediction_results() is not None
            )
        compatibility = {
            'exact_for_model_geometry': 'exact_for_model_geometry',
            'rectangular_approximation': 'rectangular_approximation',
            'unsupported': 'unsupported',
        }.get(first.geometry_compatibility, first.geometry_compatibility)
        self.prediction_summary_label.setText(
            f'{state}\nリビジョン {first.scene_revision_id[:8]} · 入力 {first.input_hash[:8]} · {compatibility}'
        )
        assumptions = ', '.join(first.assumptions[:5]) or '—'
        warnings = ', '.join(first.warnings[:5]) or 'なし'
        self.prediction_detail_label.setText(
            f'model: {first.model_id} / {first.model_version}\n'
            f'分類: 予測幾何 · 互換性: {compatibility}\n'
            f'前提: {assumptions}\n'
            f'警告: {warnings}'
        )

    def _remove_prediction_overlays(self) -> None:
        for name in tuple(self._prediction_actor_names):
            try:
                self.viewport.remove_actor(name, reset_camera=False, render=False)
            except Exception:
                pass
        self._prediction_actor_names.clear()
        try:
            self.viewport.remove_actor('prediction-overlay-label', reset_camera=False, render=False)
        except Exception:
            pass

    def _render_prediction_overlay(self) -> None:
        self._remove_prediction_overlays()
        if self.working is None:
            return
        results = self._selected_prediction_results()
        if not results:
            return
        first = results[0]
        if scene_content_hash(self.working.committed_document) != first.scene_content_hash:
            return
        if first.geometry_compatibility != 'exact_for_model_geometry':
            return
        if self.prediction_reflection_checkbox is not None and not self.prediction_reflection_checkbox.isChecked():
            return
        reflection_result = next(
            (item for item in results if item.result_kind == 'geometry_reflections'), None
        )
        if reflection_result is None:
            return

        direct_seen: set[str] = set()
        for index, reflection in enumerate(reflection_result.reflections):
            source = domain_to_render(reflection.source_position)
            point = domain_to_render(reflection.reflection_position)
            receiver = domain_to_render(reflection.receiver_position)
            if reflection.speaker_entity_id not in direct_seen:
                direct_name = f'prediction-direct:{reflection.speaker_entity_id}'
                direct_seen.add(reflection.speaker_entity_id)
                self._prediction_actor_names.add(direct_name)
                self.viewport.add_mesh(
                    pv.Line(source, receiver),
                    name=direct_name,
                    color='deepskyblue',
                    line_width=2,
                    opacity=0.65,
                    pickable=False,
                    render=False,
                )

            first_leg = f'prediction-reflection-a:{index}'
            second_leg = f'prediction-reflection-b:{index}'
            point_name = f'prediction-reflection-point:{index}'
            self._prediction_actor_names.update((first_leg, second_leg, point_name))
            self.viewport.add_mesh(
                pv.Line(source, point),
                name=first_leg,
                color='darkorange',
                line_width=2,
                opacity=0.8,
                pickable=False,
                render=False,
            )
            self.viewport.add_mesh(
                pv.Line(point, receiver),
                name=second_leg,
                color='darkorange',
                line_width=2,
                opacity=0.8,
                pickable=False,
                render=False,
            )
            self.viewport.add_mesh(
                pv.Sphere(radius=0.035, center=point),
                name=point_name,
                color='gold',
                pickable=False,
                render=False,
            )
        if reflection_result.reflections:
            self.viewport.add_text(
                '予測幾何 · 実測ではありません',
                name='prediction-overlay-label',
                position='lower_left',
                font_size=9,
            )

    def active_prediction_worker_count(self) -> int:
        return self._prediction_pool.active_count

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self._disposed = True
        for token in tuple(self._prediction_tokens.values()):
            self.prediction_job_guard.cancel(token)
        report = self._prediction_pool.shutdown()
        if not report.all_stopped:
            self.statusBar().showMessage(
                '予測処理の停止が遅延しています · 遅延結果は保存・適用しません'
            )
        super().closeEvent(event)
