from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
from threading import Event

import numpy as np
import pyqtgraph as pg
import pyvista as pv
from PySide6.QtCore import QThread, Qt, Slot
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
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import file_dialog_memory
from .cad_measurement_jobs import MeasurementJobApplyContext, MeasurementJobGuard, MeasurementJobToken
from .cad_measurement_models import CadFrequencyResponseDataset, CadMeasurementRecord
from .cad_measurement_repository import CadMeasurementRepository
from .measurement_target_service import (
    TARGET_PATTERN_PRESETS,
    MeasurementTargetService,
)
from .cad_measurements import measurement_record_for_revision, normalize_rew_api_snapshot, normalize_rew_text
from .cad_repository import SceneRepository, SceneRevision
from .cad_scene import (
    F1_DOCUMENT_ID,
    acoustic_reference_position,
    domain_pose_to_render_matrix,
    domain_to_render,
    scene_content_hash,
)
from .cad_search_models import constraint_workspace_snapshot
from .comparison import FrequencyResponse, compare_frequency_responses
from .constraint_editor import ConstraintEditorWindow
from .ingress import read_file_bounded
from .limits import MAX_NATIVE_REW_TEXT_FILE_BYTES
from .native_editor import ROLE
from .native_worker import WORKER_CANCELLED, NativeWorker, NativeWorkerPool
from .rew_api import RewApiClient
from .user_facing_error import operation_error_message


def measurement_is_synthetic(record: CadMeasurementRecord) -> bool:
    try:
        provenance = json.loads(record.provenance_json)
    except (TypeError, json.JSONDecodeError):
        provenance = {}
    return bool(
        provenance.get('validation_scope') == 'synthetic_fixture'
        or provenance.get('synthetic_fixture') is True
        or record.quality_status == 'synthetic_fixture'
    )


def measurement_evidence_label(record: CadMeasurementRecord) -> str:
    if measurement_is_synthetic(record):
        return '合成'
    return {
        'measured': '実測',
        'derived': '派生',
        'predicted': '予測',
        'unknown': '不明',
    }.get(record.evidence_type, record.evidence_type)


class MeasurementEditorWindow(ConstraintEditorWindow):
    """N60 product layer: immutable measurement evidence, FR and historical placement."""

    def __init__(self, repository: SceneRepository, document_id: str = F1_DOCUMENT_ID) -> None:
        self.measurement_repository = CadMeasurementRepository(repository)
        self.measurement_selected_id: str | None = None
        self.measurement_tree: QTreeWidget | None = None
        self.measurement_detail_label: QLabel | None = None
        self.measurement_scene_label: QLabel | None = None
        self.measurement_compare_label: QLabel | None = None
        self.fr_plot: pg.PlotWidget | None = None
        self.diff_plot: pg.PlotWidget | None = None
        self.evidence_combo: QComboBox | None = None
        self.channel_role_field: QLineEdit | None = None
        self.rew_combo: QComboBox | None = None
        self.compare_a_combo: QComboBox | None = None
        self.compare_b_combo: QComboBox | None = None
        self.compare_low_field: QDoubleSpinBox | None = None
        self.compare_high_field: QDoubleSpinBox | None = None
        self.rew_client = RewApiClient()
        self.target_service = MeasurementTargetService(repository, document_id)
        self.pattern_anchor_combo: QComboBox | None = None
        self.pattern_preset_combo: QComboBox | None = None
        self.pattern_spacing_field: QDoubleSpinBox | None = None
        self.pattern_list_label: QLabel | None = None
        self.rew_job_guard = MeasurementJobGuard()
        # Parentless until self is a QObject; reparented right after super().
        self._rew_pool = NativeWorkerPool()
        self._rew_tokens: dict[str, MeasurementJobToken] = {}
        self._rew_semantics: dict[str, tuple[str, str, str | None, str | None]] = {}
        self._latest_rew_list_key: str | None = None
        self._rew_list_sequence = 0
        self._current_rew_token_id: str | None = None
        self._disposed = False
        super().__init__(repository, document_id)
        self._rew_pool.setParent(self)
        self.setWindowTitle('Home Theater Digital Twin — 実測CAD')
        self._create_measurement_dock()
        self._refresh_measurement_list()

    def _create_measurement_dock(self) -> None:
        panel = QWidget()
        layout = QVBoxLayout(panel)

        self.measurement_scene_label = QLabel('保存済み測定なし')
        self.measurement_scene_label.setWordWrap(True)
        layout.addWidget(self.measurement_scene_label)

        self.measurement_tree = QTreeWidget()
        self.measurement_tree.setHeaderLabels(['測定', '配置版'])
        self.measurement_tree.setMinimumHeight(150)
        self.measurement_tree.itemSelectionChanged.connect(self._measurement_tree_selected)
        layout.addWidget(self.measurement_tree)

        form = QFormLayout()
        self.evidence_combo = QComboBox()
        for label, value in (
            ('不明', 'unknown'),
            ('実測', 'measured'),
            ('派生', 'derived'),
            ('予測', 'predicted'),
        ):
            self.evidence_combo.addItem(label, value)
        form.addRow('証拠種別', self.evidence_combo)

        self.channel_role_field = QLineEdit('unknown')
        self.channel_role_field.setPlaceholderText('例: front_left / center / 不明')
        form.addRow('入力役割', self.channel_role_field)
        layout.addLayout(form)

        import_row = QHBoxLayout()
        text_button = QPushButton('REWテキスト取り込み')
        text_button.setToolTip('選択中の音響基準点と、現在の保存済みシーンリビジョンへ固定して取り込みます')
        text_button.clicked.connect(self.import_rew_text_dialog)
        import_row.addWidget(text_button)
        refresh_button = QPushButton('REW一覧更新')
        refresh_button.setToolTip('localhostのREW測定一覧をGUI スレッド外で読み取ります')
        refresh_button.clicked.connect(self.refresh_rew_list_async)
        import_row.addWidget(refresh_button)
        layout.addLayout(import_row)

        rew_row = QHBoxLayout()
        self.rew_combo = QComboBox()
        self.rew_combo.setMinimumContentsLength(18)
        rew_row.addWidget(self.rew_combo, 1)
        read_button = QPushButton('選択REWを読み込み')
        read_button.clicked.connect(self.read_selected_rew_async)
        rew_row.addWidget(read_button)
        # Honest affordance (round10 cancel audit): the REW HTTP read cannot
        # be interrupted mid-call — cancelling marks the job so its late
        # result is discarded, it does not abort the request.
        cancel_button = QPushButton('読み込みの待機をやめる')
        cancel_button.setToolTip(
            '実行中のREW読み込みを中断せず、結果が届いても適用しないようにします'
        )
        cancel_button.clicked.connect(self.cancel_rew_read)
        rew_row.addWidget(cancel_button)
        layout.addLayout(rew_row)

        self.measurement_detail_label = QLabel('測定を選択すると出典 / ポイント / リビジョンを表示します')
        self.measurement_detail_label.setWordWrap(True)
        layout.addWidget(self.measurement_detail_label)

        self.fr_plot = pg.PlotWidget()
        self.fr_plot.setMinimumHeight(220)
        self.fr_plot.setLabel('bottom', '周波数', units='Hz')
        self.fr_plot.setLabel('left', 'レベル', units='dB')
        self.fr_plot.setLogMode(x=True, y=False)
        self.fr_plot.showGrid(x=True, y=True, alpha=0.25)
        layout.addWidget(self.fr_plot)

        compare_form = QFormLayout()
        self.compare_a_combo = QComboBox()
        self.compare_b_combo = QComboBox()
        compare_form.addRow('比較 A', self.compare_a_combo)
        compare_form.addRow('比較 B', self.compare_b_combo)
        self.compare_low_field = QDoubleSpinBox()
        self.compare_low_field.setRange(1.0, 100000.0)
        self.compare_low_field.setValue(20.0)
        self.compare_low_field.setSuffix(' Hz')
        self.compare_high_field = QDoubleSpinBox()
        self.compare_high_field.setRange(1.0, 100000.0)
        self.compare_high_field.setValue(20000.0)
        self.compare_high_field.setSuffix(' Hz')
        compare_form.addRow('比較下限', self.compare_low_field)
        compare_form.addRow('比較上限', self.compare_high_field)
        layout.addLayout(compare_form)

        compare_button = QPushButton('A/B比較を保存')
        compare_button.setToolTip('データセット A/Bと各測定時シーンリビジョンを固定して比較結果を保存します')
        compare_button.clicked.connect(self.compare_selected_measurements)
        layout.addWidget(compare_button)
        self.measurement_compare_label = QLabel('比較未実行')
        self.measurement_compare_label.setWordWrap(True)
        layout.addWidget(self.measurement_compare_label)

        self.diff_plot = pg.PlotWidget()
        self.diff_plot.setMinimumHeight(120)
        self.diff_plot.setLabel('bottom', '周波数', units='Hz')
        self.diff_plot.setLabel('left', 'A − B', units='dB')
        self.diff_plot.setLogMode(x=True, y=False)
        self.diff_plot.showGrid(x=True, y=True, alpha=0.25)
        layout.addWidget(self.diff_plot)

        pattern_form = QFormLayout()
        self.pattern_anchor_combo = QComboBox()
        self.pattern_anchor_combo.setMinimumContentsLength(18)
        pattern_form.addRow('ターゲット基準', self.pattern_anchor_combo)
        self.pattern_preset_combo = QComboBox()
        self.pattern_preset_combo.addItems(TARGET_PATTERN_PRESETS)
        pattern_form.addRow('パターン', self.pattern_preset_combo)
        self.pattern_spacing_field = QDoubleSpinBox()
        self.pattern_spacing_field.setRange(0.01, 1.0)
        self.pattern_spacing_field.setDecimals(3)
        self.pattern_spacing_field.setValue(0.10)
        self.pattern_spacing_field.setSuffix(' m')
        pattern_form.addRow('間隔', self.pattern_spacing_field)
        layout.addLayout(pattern_form)
        pattern_buttons = QHBoxLayout()
        pattern_create_button = QPushButton('ターゲット作成')
        pattern_create_button.clicked.connect(self._create_target_pattern)
        pattern_buttons.addWidget(pattern_create_button)
        pattern_materialize_button = QPushButton('ターゲット実体化')
        pattern_materialize_button.clicked.connect(self._materialize_target_pattern)
        pattern_buttons.addWidget(pattern_materialize_button)
        layout.addLayout(pattern_buttons)
        self.pattern_list_label = QLabel('ターゲットパターンなし')
        self.pattern_list_label.setWordWrap(True)
        layout.addWidget(self.pattern_list_label)

        dock = QDockWidget('実測', self)
        dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        constraint_dock = next(
            (candidate for candidate in self.findChildren(QDockWidget) if candidate.windowTitle() == '制約'),
            None,
        )
        if constraint_dock is not None:
            self.tabifyDockWidget(constraint_dock, dock)
        dock.raise_()
        self._refresh_target_patterns()

    def _refresh_target_patterns(self) -> None:
        """Populate anchor choices and the persisted pattern list (#987)."""
        if self.pattern_anchor_combo is not None:
            current = self.pattern_anchor_combo.currentData()
            self.pattern_anchor_combo.clear()
            for kind, entity_id in self.target_service.anchor_entity_options():
                self.pattern_anchor_combo.addItem(
                    f'{kind}: {entity_id}', (kind, entity_id)
                )
            index = self.pattern_anchor_combo.findData(current)
            if index >= 0:
                self.pattern_anchor_combo.setCurrentIndex(index)
        if self.pattern_list_label is None:
            return
        presentations = self.target_service.list_presentations()
        if not presentations:
            self.pattern_list_label.setText('ターゲットパターンなし')
            return
        lines = []
        for item in presentations[-6:]:
            state = '古い基準(要再基準化)' if item.stale else '最新'
            lines.append(
                f"{item.pattern_id.split(':')[-1][:8]} "
                f"{item.anchor_kind} v{item.pattern_version} "
                f"{item.materialized_count}/{item.point_count} {state}"
            )
        self.pattern_list_label.setText('\n'.join(lines))

    def _create_target_pattern(self) -> None:
        combo = self.pattern_anchor_combo
        if combo is None or combo.currentData() is None:
            self.statusBar().showMessage('ターゲット基準がありません')
            return
        kind, entity_id = combo.currentData()
        try:
            pattern = self.target_service.create_pattern(
                anchor_kind=kind,
                anchor_entity_id=entity_id,
                preset=self.pattern_preset_combo.currentText(),
                spacing_m=self.pattern_spacing_field.value(),
            )
        except Exception as exc:
            self.statusBar().showMessage(f'パターンを作成できません · {operation_error_message(exc)}')
            return
        preview = self.target_service.preview(pattern)
        self.statusBar().showMessage(
            f'パターン作成 · {len(preview.positions)}点 '
            f'(アンカー {pattern.anchor_revision_id[:8]}) — 実体化で確定'
        )
        self._refresh_target_patterns()

    def _materialize_target_pattern(self) -> None:
        presentations = self.target_service.list_presentations()
        if not presentations:
            self.statusBar().showMessage('実体化するパターンがありません')
            return
        # Materialize the newest un-materialized or stale-anchored pattern
        # via the canonical pinned-revision authority (#987).
        target = next(
            (
                item
                for item in reversed(presentations)
                if item.materialized_count < item.point_count
            ),
            None,
        )
        if target is None:
            target = presentations[-1]
            if target.stale:
                try:
                    rebased = self.target_service.rebase(target.pattern_id)
                except Exception as exc:
                    self.statusBar().showMessage(f'再基準化できません · {operation_error_message(exc)}')
                    return
                target_id = rebased.pattern_id
            else:
                self.statusBar().showMessage('すべてのパターンは実体化済みです')
                self._refresh_target_patterns()
                return
        else:
            target_id = target.pattern_id
            if target.stale:
                try:
                    rebased = self.target_service.rebase(target_id)
                    target_id = rebased.pattern_id
                except Exception as exc:
                    self.statusBar().showMessage(f'再基準化できません · {operation_error_message(exc)}')
                    return
        try:
            points = self.target_service.materialize(target_id)
        except Exception as exc:
            self.statusBar().showMessage(f'実体化できません · {operation_error_message(exc)}')
            return
        ids = ', '.join(point.measurement_point_entity_id for point in points)
        self.statusBar().showMessage(f'実体化しました · {ids}')
        self._refresh_target_patterns()
        self._rebuild()

    def _rebuild(self, *, reset_camera: bool = False) -> None:
        super()._rebuild(reset_camera=reset_camera)
        if self.measurement_selected_id is not None:
            self._render_measurement_ghost()
        if self.measurement_scene_label is not None:
            self._refresh_measurement_metadata()
        self.viewport.render()

    def save(self) -> None:
        super().save()
        if self.measurement_tree is not None:
            self._refresh_measurement_metadata()
            self._rebuild()

    def _saved_measurement_target(self) -> tuple[SceneRevision, str]:
        if self.working is None or self.working.source_revision_id is None:
            raise ValueError('保存済みシーンリビジョンが必要です')
        if self.working.is_dirty:
            raise ValueError('測定取り込みの前に現在の配置を保存してください')
        revision = self.repository.get(self.working.source_revision_id)
        if revision is None:
            raise ValueError('現在のシーンリビジョンを読み込めません')
        entity_id = self.selected_id
        if entity_id is None:
            raise ValueError('測定点または音響基準点を持つ物体を選択してください')
        entity = revision.document.entity(entity_id)
        if acoustic_reference_position(entity) is None:
            raise ValueError('選択物体に音響基準点がありません')
        return revision, entity_id

    def _evidence_type(self) -> str:
        return 'unknown' if self.evidence_combo is None else str(self.evidence_combo.currentData())

    def _channel_role(self) -> str:
        if self.channel_role_field is None:
            return 'unknown'
        value = self.channel_role_field.text().strip()
        return value or 'unknown'

    def import_rew_text_dialog(self) -> None:
        path, _ = file_dialog_memory.get_open_file_name(
            self,
            'REWテキストを選択',
            'measurement.rew_text',
            'テキスト (*.txt *.frd);;すべてのファイル (*)',
        )
        if not path:
            return
        try:
            file_path = Path(path)
            raw = read_file_bounded(
                file_path,
                MAX_NATIVE_REW_TEXT_FILE_BYTES,
                label='REWテキストファイル',
            )
            self.import_rew_text_bytes(raw, file_path.name)
        except Exception as exc:
            self.statusBar().showMessage(f'REWテキスト取り込み失敗 · {operation_error_message(exc)}')

    def import_rew_text_bytes(self, raw: bytes, filename: str) -> CadMeasurementRecord:
        revision, entity_id = self._saved_measurement_target()
        record, dataset, raw_filename, source = normalize_rew_text(
            revision,
            entity_id,
            raw,
            filename=filename,
            evidence_type=self._evidence_type(),
            channel_role=self._channel_role(),
        )
        self.measurement_repository.save(record, dataset, raw_filename=raw_filename, raw_bytes=source)
        self.measurement_selected_id = record.measurement_id
        self._refresh_measurement_list()
        self._rebuild()
        self.statusBar().showMessage(f'測定を保存しました · リビジョン {record.scene_revision_id[:8]}')
        return record

    def _refresh_measurement_list(self) -> None:
        if self.measurement_tree is None:
            return
        records = self.measurement_repository.list_measurements(self.document_id)
        self.measurement_tree.clear()
        for record in records:
            evidence = measurement_evidence_label(record)
            item = QTreeWidgetItem([
                f'{record.channel_role} · {evidence}',
                record.scene_revision_id[:8],
            ])
            item.setData(0, ROLE, record.measurement_id)
            self.measurement_tree.addTopLevelItem(item)
            if record.measurement_id == self.measurement_selected_id:
                item.setSelected(True)
                self.measurement_tree.setCurrentItem(item)
        self._refresh_comparison_choices(records)
        self._refresh_measurement_metadata()

    def _refresh_comparison_choices(self, records: tuple[CadMeasurementRecord, ...]) -> None:
        for combo in (self.compare_a_combo, self.compare_b_combo):
            if combo is None:
                continue
            previous = combo.currentData()
            combo.clear()
            for record in records:
                dataset = self.measurement_repository.dataset_for_measurement(record.measurement_id)
                if dataset is None:
                    continue
                combo.addItem(f'{record.channel_role} · {record.scene_revision_id[:8]}', dataset.dataset_id)
            if previous is not None:
                index = combo.findData(previous)
                if index >= 0:
                    combo.setCurrentIndex(index)
        if self.compare_b_combo is not None and self.compare_b_combo.count() > 1 and self.compare_b_combo.currentIndex() == 0:
            self.compare_b_combo.setCurrentIndex(1)

    def _measurement_tree_selected(self) -> None:
        if self.measurement_tree is None:
            return
        items = self.measurement_tree.selectedItems()
        self.measurement_selected_id = None if not items else items[0].data(0, ROLE)
        self._refresh_measurement_metadata()
        self._plot_selected_measurement()
        self._rebuild()

    def _selected_measurement(self) -> CadMeasurementRecord | None:
        if self.measurement_selected_id is None:
            return None
        return self.measurement_repository.get_measurement(self.measurement_selected_id)

    def _refresh_measurement_metadata(self) -> None:
        if self.measurement_detail_label is None or self.measurement_scene_label is None:
            return
        record = self._selected_measurement()
        if record is None:
            self.measurement_scene_label.setText('保存済み測定を選択してください')
            self.measurement_detail_label.setText('測定を選択すると出典 / ポイント / リビジョンを表示します')
            return
        current_hash = None if self.working is None else scene_content_hash(self.working.committed_document)
        historical = current_hash != record.scene_content_hash
        state = '測定時配置 · 現在配置と異なる' if historical else '測定時配置 · 現在配置と一致'
        self.measurement_scene_label.setText(
            f'{state}\nリビジョン {record.scene_revision_id[:8]} · 測定点 {record.measurement_entity_id}'
        )
        captured = record.captured_at or '不明'
        synthetic = measurement_is_synthetic(record)
        scope = (
            '合成フィクスチャ · 非物理測定'
            if synthetic
            else measurement_evidence_label(record)
        )
        self.measurement_detail_label.setText(
            f'証拠: {scope} ({record.evidence_type}) · '
            f'入力: {record.channel_role} · ソース: {record.source_kind}\n'
            f'取得時刻: {captured} · 品質: {record.quality_status}\n'
            f'位置: X {record.measurement_position.x_m:.3f} / '
            f'Y {record.measurement_position.y_m:.3f} / '
            f'Z {record.measurement_position.z_m:.3f} m'
        )

    def _plot_selected_measurement(self) -> None:
        if self.fr_plot is None:
            return
        self.fr_plot.clear()
        record = self._selected_measurement()
        if record is None:
            return
        dataset = self.measurement_repository.dataset_for_measurement(record.measurement_id)
        if dataset is None:
            return
        pen = pg.mkPen('#5b8ff9', width=2, style=Qt.PenStyle.SolidLine)
        self.fr_plot.plot(dataset.frequency_hz, dataset.level_db, pen=pen, name='選択測定')
        self.fr_plot.enableAutoRange()

    def _render_measurement_ghost(self) -> None:
        record = self._selected_measurement()
        if record is None or self.working is None:
            return
        if scene_content_hash(self.working.committed_document) == record.scene_content_hash:
            return
        try:
            revision = self.measurement_repository.source_revision(record.measurement_id)
        except Exception:
            return
        source_ids = set(record.source_speaker_ids)
        if not source_ids:
            source_ids.update(entity.entity_id for entity in revision.document.entities if entity.kind == 'speaker')
        source_ids.add(record.measurement_entity_id)
        for entity in revision.document.entities:
            if entity.entity_id not in source_ids:
                continue
            if entity.kind == 'measurement_point':
                mesh = pv.Sphere(radius=0.10, center=domain_to_render(entity.position))
            else:
                try:
                    mesh = self._physical_mesh(entity)
                except ValueError:
                    continue
                mesh.transform(np.asarray(domain_pose_to_render_matrix(entity.position, entity.orientation)), inplace=True)
            actor = self.viewport.add_mesh(
                mesh,
                name=f'measurement-ghost:{entity.entity_id}',
                style='wireframe',
                line_width=2,
                opacity=0.45,
                pickable=False,
                render=False,
            )
            actor.SetPickable(False)
        ghost_label = '合成配置' if measurement_is_synthetic(record) else '測定時配置'
        self.viewport.add_text(
            f'{ghost_label} · {record.scene_revision_id[:8]}',
            position='upper_left',
            font_size=9,
            name='measurement-ghost-label',
            render=False,
        )

    def compare_selected_measurements(self) -> None:
        if self.compare_a_combo is None or self.compare_b_combo is None:
            return
        dataset_a_id = self.compare_a_combo.currentData()
        dataset_b_id = self.compare_b_combo.currentData()
        if not dataset_a_id or not dataset_b_id or dataset_a_id == dataset_b_id:
            self.statusBar().showMessage('比較する別々の測定 A / B を選択してください')
            return
        dataset_a = self.measurement_repository.get_dataset(str(dataset_a_id))
        dataset_b = self.measurement_repository.get_dataset(str(dataset_b_id))
        if dataset_a is None or dataset_b is None:
            self.statusBar().showMessage('比較データセットを読み込めません')
            return
        low = 20.0 if self.compare_low_field is None else float(self.compare_low_field.value())
        high = 20000.0 if self.compare_high_field is None else float(self.compare_high_field.value())
        try:
            result = compare_frequency_responses(
                FrequencyResponse(dataset_a.frequency_hz, dataset_a.level_db),
                FrequencyResponse(dataset_b.frequency_hz, dataset_b.level_db),
                low,
                high,
            )
            saved = self.measurement_repository.save_comparison(str(dataset_a_id), str(dataset_b_id), result)
        except Exception as exc:
            self.statusBar().showMessage(f'A/B比較失敗 · {operation_error_message(exc)}')
            return
        self._plot_comparison(dataset_a, dataset_b, saved.difference_db, saved.grid_hz)
        if self.measurement_compare_label is not None:
            rms = '—' if saved.rms_difference_db is None else f'{saved.rms_difference_db:.3f} dB'
            self.measurement_compare_label.setText(
                f'比較 {saved.comparison_id[:8]} · A {saved.scene_revision_a_id[:8]} / B {saved.scene_revision_b_id[:8]}\n'
                f'有効点 {saved.valid_points} · RMS差 {rms} · {saved.algorithm_version}'
            )
        self.statusBar().showMessage('A/B比較をデータセット / リビジョン固定で保存しました')

    def _plot_comparison(
        self,
        dataset_a: CadFrequencyResponseDataset,
        dataset_b: CadFrequencyResponseDataset,
        difference_db: tuple[float, ...],
        grid_hz: tuple[float, ...],
    ) -> None:
        if self.fr_plot is not None:
            self.fr_plot.clear()
            self.fr_plot.plot(
                dataset_a.frequency_hz,
                dataset_a.level_db,
                pen=pg.mkPen('#5b8ff9', width=2, style=Qt.PenStyle.SolidLine),
                name='A',
            )
            self.fr_plot.plot(
                dataset_b.frequency_hz,
                dataset_b.level_db,
                pen=pg.mkPen('#f6bd16', width=2, style=Qt.PenStyle.DashLine),
                name='B',
            )
            if self.fr_plot.getPlotItem().legend is None:
                self.fr_plot.addLegend()
            self.fr_plot.enableAutoRange()
        if self.diff_plot is not None:
            self.diff_plot.clear()
            self.diff_plot.plot(
                grid_hz,
                difference_db,
                pen=pg.mkPen('#6f5ef9', width=2, style=Qt.PenStyle.DotLine),
                name='A − B',
            )
            self.diff_plot.enableAutoRange()

    def refresh_rew_list_async(self) -> None:
        self._rew_list_sequence += 1
        key = f'list:{self._rew_list_sequence}'
        self._latest_rew_list_key = key
        self._start_rew_task(
            key,
            lambda cancel_event: self.rew_client.list_measurements(
                is_cancelled=cancel_event.is_set
            ),
        )
        self.statusBar().showMessage('REW測定一覧を読み込み中…')

    def read_selected_rew_async(self) -> None:
        self._start_selected_rew_read(
            validation_scope=None,
            validation_campaign_id=None,
            evidence_type_override=None,
        )

    def _start_selected_rew_read(
        self,
        *,
        validation_scope: str | None,
        validation_campaign_id: str | None,
        evidence_type_override: str | None = None,
    ) -> None:
        if self.rew_combo is None:
            return
        external_id = self.rew_combo.currentData()
        if not external_id:
            self.statusBar().showMessage('先にREW一覧を更新して測定を選択してください')
            return
        if validation_scope is None and validation_campaign_id is not None:
            raise ValueError('validation campaign requires a validation scope')
        if validation_scope is not None and validation_scope != 'owned_room':
            raise ValueError('unsupported validation scope')
        if validation_scope == 'owned_room' and not validation_campaign_id:
            raise ValueError('owned-room REW read requires a campaign id')
        try:
            revision, entity_id = self._saved_measurement_target()
            provisional = measurement_record_for_revision(
                revision,
                entity_id,
                source_kind='rew_api',
                external_source_id=str(external_id),
            )
        except Exception as exc:
            self.statusBar().showMessage(operation_error_message(exc))
            return
        token = self.rew_job_guard.submit(
            provisional,
            external_source_id=str(external_id),
            query={'unit': 'SPL', 'ppo': None, 'smoothing': None},
            constraint_workspace_hash=self._constraint_workspace_hash(),
        )
        self._rew_tokens[token.job_id] = token
        evidence_type = (
            self._evidence_type()
            if evidence_type_override is None
            else evidence_type_override
        )
        self._rew_semantics[token.job_id] = (
            evidence_type,
            self._channel_role(),
            validation_scope,
            validation_campaign_id,
        )
        self._current_rew_token_id = token.job_id
        self._start_rew_task(
            token.job_id,
            lambda cancel_event: self.rew_client.get_frequency_response_snapshot(
                str(external_id),
                ppo=None,
                unit='SPL',
                smoothing=None,
                is_cancelled=cancel_event.is_set,
            ),
        )
        scope_message = (
            ''
            if validation_campaign_id is None
            else f' · キャンペーン {validation_campaign_id[:8]}'
        )
        self.statusBar().showMessage(
            f'REW読み込み中 · リビジョン {token.scene_revision_id[:8]}{scope_message}'
        )

    def cancel_rew_read(self) -> None:
        token_id = self._current_rew_token_id
        if token_id is None:
            return
        token = self._rew_tokens.get(token_id)
        if token is not None:
            self.rew_job_guard.cancel(token)
            # Logical-cancel the worker too: a still-queued read never runs
            # and a finishing read reports WORKER_CANCELLED instead of a
            # successful result that must be discarded downstream (#REV18).
            self._rew_pool.cancel(token_id)
            self.statusBar().showMessage('REW読み込みの待機をやめました · 遅延結果は適用しません')
        self._current_rew_token_id = None

    @property
    def _rew_tasks(self) -> dict[str, tuple[QThread, NativeWorker]]:
        """Live worker records owned by ``self._rew_pool``."""
        return self._rew_pool.tasks

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

    @Slot(object, object, object)
    def _rew_task_completed(self, key: object, result: object, error: object) -> None:
        if self._disposed:
            return
        key = str(key)
        if key.startswith('list:'):
            if key != self._latest_rew_list_key or error == WORKER_CANCELLED:
                return
            if error is not None:
                self.statusBar().showMessage(f'REW一覧取得失敗 · {operation_error_message(error)}')
                return
            summaries = result if isinstance(result, list) else []
            if self.rew_combo is not None:
                self.rew_combo.clear()
                for summary in summaries:
                    if not isinstance(summary, dict) or not isinstance(summary.get('uuid'), str):
                        continue
                    label = summary.get('title') if isinstance(summary.get('title'), str) else summary['uuid']
                    self.rew_combo.addItem(label, summary['uuid'])
            self.statusBar().showMessage(f'REW測定 {len(summaries)} 件を確認しました')
            return

        token = self._rew_tokens.get(key)
        if token is None:
            return
        if error is not None:
            if error != WORKER_CANCELLED and not self.rew_job_guard.is_cancelled(token):
                self.statusBar().showMessage(f'REW読み込み失敗 · {operation_error_message(error)}')
            return
        if self.rew_job_guard.is_cancelled(token):
            # The operator cancelled this read — the discard reason is the
            # cancel itself, not a revision/constraint change (#REV18).
            self.statusBar().showMessage('REW読み込みはキャンセルされました · 遅延結果は適用しません')
            return
        context = self._current_job_apply_context()
        if context is None or not self.rew_job_guard.can_apply(token, context):
            self.statusBar().showMessage('REW遅延結果は現在の配置へ適用しません · リビジョン/ドキュメント/制約が変更されています')
            return
        revision = self.repository.get(token.scene_revision_id)
        if revision is None:
            self.statusBar().showMessage('REW結果のソースリビジョンが見つかりません')
            return
        evidence, channel_role, validation_scope, validation_campaign_id = self._rew_semantics.get(
            token.job_id,
            ('unknown', 'unknown', None, None),
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
            self.measurement_repository.save(record, dataset, raw_filename=filename, raw_bytes=raw)
        except Exception as exc:
            self.statusBar().showMessage(f'REW結果保存失敗 · {operation_error_message(exc)}')
            return
        self.measurement_selected_id = record.measurement_id
        self._refresh_measurement_list()
        self._rebuild()
        self.statusBar().showMessage(f'REW測定を保存しました · リビジョン {record.scene_revision_id[:8]}')

    def _constraint_workspace_hash(self) -> str | None:
        """Digest of the constraint workspace a delayed REW read is bound to.

        ``None`` means no constraint workspace is loaded, so the read stays
        constraint-independent (matching PredictionJobGuard semantics).
        """
        constraint_set = getattr(self, 'constraint_set', None)
        if constraint_set is None:
            return None
        _snapshot, digest = constraint_workspace_snapshot(constraint_set)
        return digest

    def _current_job_apply_context(self) -> MeasurementJobApplyContext | None:
        if self.working is None or self.working.source_revision_id is None:
            return None
        return MeasurementJobApplyContext(
            document_id=self.document_id,
            scene_revision_id=self.working.source_revision_id,
            scene_content_hash=scene_content_hash(self.working.committed_document),
            constraint_workspace_hash=self._constraint_workspace_hash(),
        )

    def _rew_task_finished(self, key: str) -> None:
        self._rew_tasks.pop(key, None)
        if not key.startswith('list:'):
            self._rew_tokens.pop(key, None)
            self._rew_semantics.pop(key, None)
            if self._current_rew_token_id == key:
                self._current_rew_token_id = None

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self._disposed = True
        for token in tuple(self._rew_tokens.values()):
            self.rew_job_guard.cancel(token)
        report = self._rew_pool.shutdown()
        if not report.all_stopped:
            self.statusBar().showMessage(
                'REW処理の停止が遅延しています · 遅延結果は適用しません'
            )
        super().closeEvent(event)
