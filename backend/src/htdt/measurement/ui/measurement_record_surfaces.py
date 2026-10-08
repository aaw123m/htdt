"""REV44-HEALTHSYNC — record-entry surfaces for AV sync and system health.

Two authority families had complete models + repositories + consumers but no
production writers (rev43 finding 7): ``CadAVSyncRepository`` saves and the
``CadSystemHealthRepository`` baseline/plan/run chain were unreachable from
the app, so the 稼働状況 overview domain could only ever read 未チェック and
the ``av_sync_recorded``/``health_baseline_created`` activity kinds could
never fire.

This module hosts the minimal honest record-entry dialogs, launched from
cards on the measurement workspace's 品質 context — the same context
``AV_SYNC_CONDITION`` deep links already route to:

* :class:`AVSyncRecordDialog` — register an exact playback-chain
  ``AVSyncCondition`` (scene/variant/operating-state pins auto-derived from
  the persisted document state; chain fields declared by the operator), then
  record ``AVLatencyMeasurement`` rows through the lifecycle chain the
  repository enforces — a chain head is always ``measured`` and every
  successor pins the current head (monotonic, no branching).
* :class:`HealthCheckDialog` — register a ``SystemHealthBaseline`` (name +
  optional operating-preset pick + user-authored metric pins whose evidence
  picks come from real persisted authorities), build+save a
  ``HealthCheckPlan`` from user-authored check items, then record a
  ``HealthCheckRun`` (per-check observations: evidence pick from persisted
  authorities + observed values + context) through ``run_health_check`` +
  ``save_run``.

Neither dialog holds authority: every write goes through the repositories'
verification, every read re-lists persisted rows, and invalid input fails
closed with a JA status message instead of a guess.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import NamedTuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...cad_av_sync import (
    AVLatencyMeasurement,
    AVSyncCondition,
    advance_av_latency_measurement,
    build_av_latency_measurement,
    build_av_sync_condition,
)
from ...cad_av_sync_repository import CadAVSyncRepository
from ..domain.cad_measurement_quality import measurement_sha256
from ..persistence.cad_measurement_repository import CadMeasurementRepository
from ...cad_operating_preset import (
    PresetProvenanceItem,
    build_operating_preset,
    record_applied_preset_state,
)
from ...cad_operating_preset_repository import CadOperatingPresetRepository
from ...cad_prediction_repository import CadPredictionRepository
from ...cad_repository import SceneRepository
from ...cad_room_operating_state_repository import (
    CadRoomOperatingStateRepository,
)
from ...cad_system_health import (
    HealthAuthorityRef,
    HealthCheckItem,
    HealthMetricPin,
    HealthObservation,
    build_health_baseline,
    build_health_check_plan,
    run_health_check,
)
from ...cad_system_health_repository import CadSystemHealthRepository
from ...cad_system_variant_repository import CadSystemVariantRepository
from ...ui_theme import TypographyRole, set_typography_role
from ...error_boundary import EXPECTED_OPERATION_ERRORS
from ...user_facing_error import operation_error_message


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _scroll_wrap(page: QWidget) -> QScrollArea:
    """Wrap a tab/dialog page so every control stays reachable.

    A plain page sized beyond the display drops its lower controls off
    screen with no scrollbar — measured 933px (HealthCheck) and 1150px
    (AV sync) on a 768px-tall box. QScrollArea keeps the natural hint
    small and scrolls instead.
    """

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QScrollArea.Shape.NoFrame)
    scroll.setWidget(page)
    return scroll


def _section_title(text: str, parent: QWidget) -> QLabel:
    label = QLabel(text, parent)
    set_typography_role(label, TypographyRole.SECONDARY)
    return label


def _short(text: str, length: int = 10) -> str:
    return text if len(text) <= length else text[:length] + '…'


def _parse_float(text: str) -> float | None:
    """Strict float parse — returns None for blanks, raises on junk."""
    stripped = text.strip()
    if not stripped:
        return None
    return float(stripped)


def _parse_required_float(text: str) -> float:
    value = _parse_float(text)
    if value is None:
        raise ValueError('blank')
    return value


# ---------------------------------------------------------------------------
# Shared authority picks
# ---------------------------------------------------------------------------


class _AuthorityOption(NamedTuple):
    """One persisted-authority pick rendered in an evidence combo."""

    text: str
    ref: HealthAuthorityRef


# Ref kinds the health baseline can honestly include in ``source_refs`` —
# every one resolves through the canonical registry, so a saved baseline
# stays verifiable by ``verify_persisted_baseline``.
_PERSISTED_REF_KINDS = frozenset(
    {
        'measurement',
        'prediction',
        'scene_revision',
        'operating_preset',
        'system_variant',
    }
)


def _persisted_authority_options(
    *,
    document_id: str,
    scene_repository: SceneRepository,
    measurement_repository: CadMeasurementRepository,
    prediction_repository: CadPredictionRepository,
    preset_repository: CadOperatingPresetRepository,
    variant_repository: CadSystemVariantRepository,
) -> list[_AuthorityOption]:
    """Evidence picks: every entry is a real persisted authority of this
    document, carrying the exact semantic hash its owner exposes."""
    options: list[_AuthorityOption] = []
    for record in measurement_repository.list_measurements(document_id):
        options.append(
            _AuthorityOption(
                f'測定 {_short(record.measurement_id)}',
                HealthAuthorityRef(
                    kind='measurement',
                    ref_id=record.measurement_id,
                    ref_sha256=measurement_sha256(record),
                ),
            )
        )
    for result in prediction_repository.list_results(document_id):
        options.append(
            _AuthorityOption(
                f'予測 {_short(result.prediction_id)}',
                HealthAuthorityRef(
                    kind='prediction',
                    ref_id=result.prediction_id,
                    ref_sha256=result.result_sha256,
                ),
            )
        )
    head = scene_repository.current_head(document_id)
    if head is not None:
        options.append(
            _AuthorityOption(
                f'シーンリビジョン {_short(head.revision_id)}（現在）',
                HealthAuthorityRef(
                    kind='scene_revision',
                    ref_id=head.revision_id,
                    ref_sha256=head.content_hash,
                ),
            )
        )
    for preset in preset_repository.list_presets(document_id):
        options.append(
            _AuthorityOption(
                f'プリセット {preset.name}',
                HealthAuthorityRef(
                    kind='operating_preset',
                    ref_id=preset.preset_id,
                    ref_sha256=preset.preset_sha256,
                    label=preset.name,
                ),
            )
        )
    for variant in variant_repository.list_variants(document_id):
        options.append(
            _AuthorityOption(
                f'バリアント {variant.name}',
                HealthAuthorityRef(
                    kind='system_variant',
                    ref_id=variant.variant_id,
                    ref_sha256=variant.variant_sha256,
                    label=variant.name,
                ),
            )
        )
    return options


# ---------------------------------------------------------------------------
# AV sync
# ---------------------------------------------------------------------------

_AV_STAGE_JA: dict[str, str] = {
    'measured': '実測',
    'correction_requested': '補正要求',
    'setting_applied': '設定適用',
    'residual_verified': '残差検証',
}

_AV_STAGE_ORDER = (
    'measured',
    'correction_requested',
    'setting_applied',
    'residual_verified',
)

_AV_METHOD_JA = (
    ('外部シンクテスト（手動）', 'manual_external_sync_test'),
    ('自動測定', 'auto'),
    ('不明', 'unknown'),
)

_AV_EVIDENCE_JA = (
    ('操作者が確認', 'user_confirmed'),
    ('機器の読み戻し', 'device_read_back'),
    ('インポート', 'imported'),
    ('不明', 'unknown'),
)


def _av_ms(value: float | None) -> str:
    return '—' if value is None else f'{value:g} ms'


class AVSyncRecordDialog(QDialog):
    """Record AV-sync conditions and their measurement lifecycle chains."""

    def __init__(
        self,
        *,
        scene_repository: SceneRepository,
        document_id: str,
        av_sync_repository: CadAVSyncRepository | None = None,
        variant_repository: CadSystemVariantRepository | None = None,
        operating_state_repository: CadRoomOperatingStateRepository | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.scene_repository = scene_repository
        self.document_id = document_id
        self.av_sync_repository = av_sync_repository or CadAVSyncRepository(
            scene_repository
        )
        self.variant_repository = (
            variant_repository or CadSystemVariantRepository(scene_repository)
        )
        self.operating_state_repository = (
            operating_state_repository
            or CadRoomOperatingStateRepository(scene_repository)
        )
        self._head = None
        self._operating_state = None

        self.setWindowTitle('AV同期の記録')
        self.setMinimumWidth(640)
        content = QWidget(self)
        layout = QVBoxLayout(content)
        scroll = _scroll_wrap(content)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        # Content is ~1150px tall — open bounded inside a 768px screen.
        self.resize(720, 700)

        layout.addWidget(_section_title('登録済みのAV同期条件', self))
        self.conditions = QTreeWidget(self)
        self.conditions.setHeaderLabels(
            ['表示機器', '映像モード', '音声経路', 'リップシンク', '登録日時']
        )
        self.conditions.setRootIsDecorated(False)
        self.conditions.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.conditions.setMinimumHeight(120)
        self.conditions.itemSelectionChanged.connect(self._refresh_measurements)
        layout.addWidget(self.conditions)

        layout.addWidget(_section_title('条件を登録', self))
        pin_form = QFormLayout()
        self.scene_pin_label = QLabel(pin_form.parentWidget())
        self.scene_pin_label.setWordWrap(True)
        pin_form.addRow('シーン', self.scene_pin_label)
        self.variant_combo = QComboBox(pin_form.parentWidget())
        pin_form.addRow('システムバリアント', self.variant_combo)
        self.state_pin_label = QLabel(pin_form.parentWidget())
        self.state_pin_label.setWordWrap(True)
        pin_form.addRow('運用状態', self.state_pin_label)
        layout.addLayout(pin_form)

        chain_form = QFormLayout()
        self.source_device_edit = QLineEdit()
        self.source_device_edit.setPlaceholderText('例: avr-1、player-1')
        chain_form.addRow('ソース機器', self.source_device_edit)
        self.source_input_edit = QLineEdit()
        self.source_input_edit.setPlaceholderText('例: hdmi1')
        chain_form.addRow('ソース入力経路', self.source_input_edit)
        self.display_device_edit = QLineEdit()
        self.display_device_edit.setPlaceholderText('例: projector-x')
        chain_form.addRow('表示機器（必須）', self.display_device_edit)
        self.video_mode_edit = QLineEdit()
        self.video_mode_edit.setPlaceholderText('例: game、movie')
        chain_form.addRow('映像モード', self.video_mode_edit)
        self.refresh_rate_edit = QLineEdit()
        self.refresh_rate_edit.setPlaceholderText('例: 120')
        chain_form.addRow('リフレッシュレート (Hz)', self.refresh_rate_edit)
        self.frame_rate_edit = QLineEdit()
        self.frame_rate_edit.setPlaceholderText('例: 60')
        chain_form.addRow('フレームレート (Hz)', self.frame_rate_edit)
        self.video_processing_edit = QLineEdit()
        self.video_processing_edit.setPlaceholderText('例: low_latency')
        chain_form.addRow('映像処理モード', self.video_processing_edit)
        self.audio_processing_edit = QLineEdit()
        self.audio_processing_edit.setPlaceholderText('例: direct')
        chain_form.addRow('音声処理モード', self.audio_processing_edit)
        self.audio_path_edit = QLineEdit()
        self.audio_path_edit.setPlaceholderText('例: earc、analog')
        chain_form.addRow('音声経路', self.audio_path_edit)
        self.lip_sync_edit = QLineEdit()
        self.lip_sync_edit.setPlaceholderText('例: 15（機器側の補正値）')
        chain_form.addRow('リップシンク補正 (ms)', self.lip_sync_edit)
        layout.addLayout(chain_form)

        register_row = QHBoxLayout()
        register_row.addStretch(1)
        self.condition_button = QPushButton('条件を登録', self)
        self.condition_button.setToolTip('測定時の条件（取得コンテキスト）を権威として登録します')
        self.condition_button.clicked.connect(self._register_condition)
        register_row.addWidget(self.condition_button)
        layout.addLayout(register_row)

        layout.addWidget(_section_title('測定チェーン', self))
        self.measurements = QTreeWidget(self)
        self.measurements.setHeaderLabels(
            ['段階', '測定オフセット', '要求補正', '適用設定', '残差', '記録日時']
        )
        self.measurements.setRootIsDecorated(False)
        self.measurements.setMinimumHeight(110)
        layout.addWidget(self.measurements)

        measure_form = QFormLayout()
        self.chain_combo = QComboBox()
        self.chain_combo.currentIndexChanged.connect(
            self._refresh_stage_options
        )
        measure_form.addRow('チェーン', self.chain_combo)
        self.stage_combo = QComboBox()
        self.stage_combo.currentIndexChanged.connect(self._stage_changed)
        measure_form.addRow('記録する段階', self.stage_combo)
        self.measure_value_edit = QLineEdit()
        self.measure_value_edit.setPlaceholderText('例: 42')
        self.measure_value_label = QLabel('測定オフセット (ms)')
        measure_form.addRow(self.measure_value_label, self.measure_value_edit)
        self.uncertainty_edit = QLineEdit()
        self.uncertainty_edit.setPlaceholderText('任意（例: 8）')
        self.uncertainty_label = QLabel('不確かさ (ms)')
        measure_form.addRow(self.uncertainty_label, self.uncertainty_edit)
        self.method_combo = QComboBox()
        for label, value in _AV_METHOD_JA:
            self.method_combo.addItem(label, value)
        self.method_label = QLabel('測定方法')
        measure_form.addRow(self.method_label, self.method_combo)
        self.evidence_combo = QComboBox()
        for label, value in _AV_EVIDENCE_JA:
            self.evidence_combo.addItem(label, value)
        self.evidence_label = QLabel('適用の証拠')
        measure_form.addRow(self.evidence_label, self.evidence_combo)
        self.measure_note_edit = QLineEdit()
        self.measure_note_edit.setPlaceholderText('任意のメモ')
        measure_form.addRow('メモ', self.measure_note_edit)
        layout.addLayout(measure_form)

        sign_note = QLabel(
            'オフセットは音声−映像 (ms)。正の値 = 音声が映像より遅れています。'
        )
        sign_note.setWordWrap(True)
        set_typography_role(sign_note, TypographyRole.SECONDARY)
        layout.addWidget(sign_note)

        record_row = QHBoxLayout()
        record_row.addStretch(1)
        self.measure_button = QPushButton('測定を記録', self)
        self.measure_button.setToolTip('AV同期測定の値を権威として記録します')
        self.measure_button.clicked.connect(self._record_measurement)
        record_row.addWidget(self.measure_button)
        layout.addLayout(record_row)

        self.status_label = QLabel('')
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.reload()

    # -- persisted listing -------------------------------------------------

    def reload(self) -> None:
        """Re-list persisted conditions + refresh the pinned-context pick."""
        self._refresh_pins()
        self.conditions.clear()
        for condition in self.av_sync_repository.list_conditions(self.document_id):
            item = QTreeWidgetItem(
                [
                    condition.display_device_id,
                    condition.video_mode,
                    condition.audio_path,
                    _av_ms(condition.lip_sync_offset_ms),
                    condition.created_at[:19],
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, condition)
            self.conditions.addTopLevelItem(item)
        if self.conditions.topLevelItemCount() == 0:
            empty = QTreeWidgetItem(['（登録なし）', '', '', '', ''])
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.conditions.addTopLevelItem(empty)
        self._refresh_measurements()

    def _refresh_pins(self) -> None:
        """Auto-derive scene/variant/operating-state pins from persisted state."""
        head = self.scene_repository.current_head(self.document_id)
        self._head = head
        if head is None:
            self.scene_pin_label.setText('（保存済みの部屋がありません）')
            self.condition_button.setEnabled(False)
        else:
            self.scene_pin_label.setText(
                f'{_short(head.revision_id)} · ハッシュ {_short(head.content_hash)}'
            )
            self.condition_button.setEnabled(True)

        current_index = 0
        self.variant_combo.clear()
        self.variant_combo.addItem('（なし）', None)
        for index, variant in enumerate(
            self.variant_repository.list_variants(self.document_id), start=1
        ):
            self.variant_combo.addItem(
                f'{variant.name}（{_short(variant.variant_id)}）', variant
            )
            # A variant proposed from the current head is the operating
            # default; the operator can still pick any persisted variant.
            if head is not None and variant.baseline_revision_id == head.revision_id:
                current_index = index
        self.variant_combo.setCurrentIndex(current_index)

        states = self.operating_state_repository.list_states(self.document_id)
        self._operating_state = states[-1] if states else None
        if self._operating_state is None:
            self.state_pin_label.setText('（記録なし）')
        else:
            self.state_pin_label.setText(
                f'{self._operating_state.name} · v{self._operating_state.version}'
            )

    def _refresh_measurements(self) -> None:
        condition = self._selected_condition()
        self.measurements.clear()
        self.chain_combo.clear()
        if condition is None:
            self.stage_combo.clear()
            self.measure_button.setEnabled(False)
            return
        self.measure_button.setEnabled(True)
        stages = self.av_sync_repository.list_measurements(condition.condition_id)
        heads: dict[str, AVLatencyMeasurement] = {}
        for stage in stages:
            chain = stage.chain_id or stage.measurement_id
            if (
                chain not in heads
                or _AV_STAGE_ORDER.index(stage.status)
                > _AV_STAGE_ORDER.index(heads[chain].status)
            ):
                heads[chain] = stage
            item = QTreeWidgetItem(
                [
                    _AV_STAGE_JA.get(stage.status, stage.status),
                    _av_ms(stage.measured_offset_ms),
                    _av_ms(stage.requested_correction_ms),
                    _av_ms(stage.applied_setting_ms),
                    _av_ms(stage.residual_offset_ms),
                    stage.captured_at[:19],
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, stage)
            self.measurements.addTopLevelItem(item)
        if self.measurements.topLevelItemCount() == 0:
            empty = QTreeWidgetItem(['（記録なし）', '', '', '', '', ''])
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.measurements.addTopLevelItem(empty)

        self.chain_combo.blockSignals(True)
        self.chain_combo.addItem('新しいチェーンを開始', None)
        for chain_id, head in heads.items():
            self.chain_combo.addItem(
                f'チェーン {_short(chain_id, 8)} · 現在: '
                f'{_AV_STAGE_JA.get(head.status, head.status)}',
                head,
            )
        # Subsequent records pin the current head by default — starting a
        # new parallel chain is the explicit exception, not the default.
        self.chain_combo.setCurrentIndex(1 if heads else 0)
        self.chain_combo.blockSignals(False)
        self._refresh_stage_options()

    def _selected_condition(self) -> AVSyncCondition | None:
        selected = self.conditions.selectedItems()
        if not selected:
            return None
        return selected[0].data(0, Qt.ItemDataRole.UserRole)

    def _refresh_stage_options(self) -> None:
        head = self.chain_combo.currentData()
        self.stage_combo.clear()
        if head is None:
            self.stage_combo.addItem('初期測定', 'measured')
        else:
            next_index = _AV_STAGE_ORDER.index(head.status) + 1
            for status in _AV_STAGE_ORDER[next_index:]:
                self.stage_combo.addItem(
                    f'次の段階: {_AV_STAGE_JA[status]}', status
                )
        self.stage_combo.setEnabled(self.stage_combo.count() > 0)
        self._stage_changed()

    def _stage_changed(self) -> None:
        stage = self.stage_combo.currentData()
        if stage == 'measured':
            self.measure_value_label.setText('測定オフセット (ms)')
        elif stage == 'correction_requested':
            self.measure_value_label.setText('要求する補正 (ms)')
        elif stage == 'setting_applied':
            self.measure_value_label.setText('適用した設定値 (ms)')
        else:
            self.measure_value_label.setText('残差オフセット (ms)')
        measured = stage == 'measured'
        self.uncertainty_label.setVisible(measured)
        self.uncertainty_edit.setVisible(measured)
        self.method_label.setVisible(measured)
        self.method_combo.setVisible(measured)
        applied = stage == 'setting_applied'
        self.evidence_label.setVisible(applied)
        self.evidence_combo.setVisible(applied)

    # -- writes --------------------------------------------------------------

    def _register_condition(self) -> None:
        if self._head is None:
            self.status_label.setText('先に部屋を保存してください。')
            return
        display_device = self.display_device_edit.text().strip()
        if not display_device:
            self.status_label.setText('表示機器を入力してください。')
            return
        try:
            refresh_rate = _parse_float(self.refresh_rate_edit.text())
            frame_rate = _parse_float(self.frame_rate_edit.text())
            lip_sync = _parse_float(self.lip_sync_edit.text())
        except ValueError:
            self.status_label.setText('Hz/ms の値は数値で入力してください。')
            return
        variant = self.variant_combo.currentData()
        state = self._operating_state
        try:
            condition = build_av_sync_condition(
                document_id=self.document_id,
                scene_revision_id=self._head.revision_id,
                scene_revision_sha256=self._head.content_hash,
                system_variant_id=(
                    variant.variant_id if variant is not None else None
                ),
                system_variant_sha256=(
                    variant.variant_sha256 if variant is not None else None
                ),
                source_device_id=self.source_device_edit.text().strip() or 'unknown',
                source_input_path=self.source_input_edit.text().strip() or 'unknown',
                display_device_id=display_device,
                video_mode=self.video_mode_edit.text().strip() or 'unknown',
                refresh_rate_hz=refresh_rate,
                frame_rate_hz=frame_rate,
                video_processing_mode=(
                    self.video_processing_edit.text().strip() or 'unknown'
                ),
                audio_processing_mode=(
                    self.audio_processing_edit.text().strip() or 'unknown'
                ),
                audio_path=self.audio_path_edit.text().strip() or 'unknown',
                lip_sync_offset_ms=lip_sync if lip_sync is not None else 0.0,
                operating_state_id=(
                    state.state_id if state is not None else None
                ),
                operating_state_sha256=(
                    state.semantic_sha256 if state is not None else None
                ),
                created_at=_utc_now(),
            )
            self.av_sync_repository.save_condition(condition)
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: condition save accept — expected validation/store errors surface verbatim; unexpected errors propagate to diagnostics
            self.status_label.setText(f'登録できませんでした: {operation_error_message(exc)}')
            return
        self.status_label.setText(
            f'条件を登録しました（{display_device}）。'
        )
        self.reload()

    def _record_measurement(self) -> None:
        condition = self._selected_condition()
        if condition is None:
            self.status_label.setText('条件を選択してください。')
            return
        head = self.chain_combo.currentData()
        stage = self.stage_combo.currentData()
        if stage is None:
            self.status_label.setText('記録できる段階がありません。')
            return
        if (head is None) != (stage == 'measured'):
            self.status_label.setText('チェーンと段階の組み合わせが不正です。')
            return
        try:
            value = _parse_float(self.measure_value_edit.text())
            uncertainty = _parse_float(self.uncertainty_edit.text())
        except ValueError:
            self.status_label.setText('ms の値は数値で入力してください。')
            return
        notes = (
            (self.measure_note_edit.text().strip(),)
            if self.measure_note_edit.text().strip()
            else ()
        )
        try:
            if head is None:
                measurement = build_av_latency_measurement(
                    condition,
                    method=self.method_combo.currentData(),
                    status='measured',
                    measured_offset_ms=value,
                    uncertainty_ms=uncertainty,
                    captured_at=_utc_now(),
                    source_kind='user_measured',
                    notes=notes,
                )
            else:
                kwargs: dict = {
                    'status': stage,
                    'captured_at': _utc_now(),
                    'notes': notes,
                }
                if stage == 'correction_requested':
                    kwargs['requested_correction_ms'] = value
                elif stage == 'setting_applied':
                    kwargs['applied_setting_ms'] = value
                    kwargs['applied_evidence'] = self.evidence_combo.currentData()
                elif stage == 'residual_verified':
                    kwargs['residual_offset_ms'] = value
                measurement = advance_av_latency_measurement(head, **kwargs)
            self.av_sync_repository.save_measurement(measurement)
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: latency record accept — expected conflict/validation errors surface verbatim; unexpected errors propagate to diagnostics
            self.status_label.setText(f'記録できませんでした: {operation_error_message(exc)}')
            return
        self.status_label.setText(
            f'{_AV_STAGE_JA.get(stage, stage)} を記録しました。'
        )
        self.measure_value_edit.clear()
        self.uncertainty_edit.clear()
        self.measure_note_edit.clear()
        self._refresh_measurements()


# ---------------------------------------------------------------------------
# System health
# ---------------------------------------------------------------------------

_HEALTH_DOMAIN_JA = (
    ('音響', 'acoustic'),
    ('設定', 'settings'),
    ('物理', 'physical'),
    ('映像', 'video'),
    ('ノイズ', 'noise'),
)

_HEALTH_UNIT_JA = (
    ('dB', 'db'),
    ('Hz', 'hz'),
    ('m', 'm'),
    ('単位なし', 'unitless'),
)

_UNIT_VALUE_KEY = {
    'db': 'value_db',
    'hz': 'value_hz',
    'm': 'value_m',
    'unitless': 'value',
}

_UNIT_TOLERANCE_KEY = {
    'db': 'tolerance_db',
    'hz': 'tolerance_hz',
    'm': 'tolerance_m',
}

_HEALTH_STATE_JA = {
    'within_baseline': 'ベースライン内',
    'changed': '変化あり',
    'indeterminate': '判定不能',
    'not_comparable': '比較不可',
    'not_run': '未実行',
}

_HEALTH_TRIGGER_JA = (
    ('手動', 'manual'),
    ('機材変更', 'equipment_change'),
    ('部屋変更', 'room_change'),
    ('定期チェック', 'periodic_reminder'),
)


def _repr_text(unit: str, value: float) -> str:
    return json.dumps(
        {_UNIT_VALUE_KEY[unit]: value}, separators=(',', ':'), sort_keys=True
    )


def _repr_unit(expected_repr: str) -> str | None:
    """Unit token of a pin's ``expected_repr``, without importing privates."""
    try:
        parsed = json.loads(expected_repr)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return 'unitless' if isinstance(parsed, (int, float)) else None
    for key, unit in (
        ('value_db', 'db'),
        ('level_db', 'db'),
        ('value_hz', 'hz'),
        ('frequency_hz', 'hz'),
        ('value_m', 'm'),
        ('position_m', 'm'),
        ('value', 'unitless'),
    ):
        if isinstance(parsed.get(key), (int, float)) and not isinstance(
            parsed.get(key), bool
        ):
            return unit
    return None


def _unit_label(unit: str | None) -> str:
    return dict(_HEALTH_UNIT_JA).get(unit or '', '') or (unit or '?')


def _domain_label(domain: str) -> str:
    return dict(_HEALTH_DOMAIN_JA).get(domain, domain)


class HealthCheckDialog(QDialog):
    """Record a system-health baseline, check plan and check runs."""

    def __init__(
        self,
        *,
        scene_repository: SceneRepository,
        document_id: str,
        health_repository: CadSystemHealthRepository | None = None,
        preset_repository: CadOperatingPresetRepository | None = None,
        measurement_repository: CadMeasurementRepository | None = None,
        prediction_repository: CadPredictionRepository | None = None,
        variant_repository: CadSystemVariantRepository | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.scene_repository = scene_repository
        self.document_id = document_id
        self.preset_repository = (
            preset_repository or CadOperatingPresetRepository(scene_repository)
        )
        self.health_repository = health_repository or CadSystemHealthRepository(
            scene_repository, preset_repository=self.preset_repository
        )
        self.measurement_repository = (
            measurement_repository
            or CadMeasurementRepository(scene_repository)
        )
        self.prediction_repository = (
            prediction_repository
            or CadPredictionRepository(scene_repository)
        )
        self.variant_repository = (
            variant_repository or CadSystemVariantRepository(scene_repository)
        )
        self._pins: list[HealthMetricPin] = []
        self._pending_checks: list[HealthCheckItem] = []
        self._run_rows: list[dict] = []

        self.setWindowTitle('健全性チェックの記録')
        self.setMinimumWidth(720)
        layout = QVBoxLayout(self)

        self.tabs = QTabWidget(self)
        layout.addWidget(self.tabs)
        self.tabs.addTab(
            _scroll_wrap(self._build_baseline_tab()), 'ベースライン'
        )
        self.tabs.addTab(
            _scroll_wrap(self._build_plan_tab()), 'チェック計画'
        )
        self.tabs.addTab(
            _scroll_wrap(self._build_run_tab()), 'チェック実行'
        )

        self.status_label = QLabel('')
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        # Tab content is ~930px tall — open bounded inside a 768px screen.
        self.resize(760, 680)
        self.reload()

    # -- tab: baseline -------------------------------------------------------

    def _build_baseline_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)

        layout.addWidget(_section_title('登録済みベースライン', page))
        self.baselines = QTreeWidget(page)
        self.baselines.setHeaderLabels(
            ['名前', '作成日時', 'ピン数', 'プリセット']
        )
        self.baselines.setRootIsDecorated(False)
        self.baselines.setMinimumHeight(90)
        layout.addWidget(self.baselines)

        layout.addWidget(_section_title('ベースラインを登録', page))
        form = QFormLayout()
        self.baseline_name_edit = QLineEdit()
        self.baseline_name_edit.setPlaceholderText('例: コミッショニング時')
        form.addRow('名前（必須）', self.baseline_name_edit)
        self.baseline_preset_combo = QComboBox()
        form.addRow('運用プリセット', self.baseline_preset_combo)
        layout.addLayout(form)

        layout.addWidget(_section_title('メトリクスピン', page))
        pin_form = QFormLayout()
        self.pin_domain_combo = QComboBox()
        for label, value in _HEALTH_DOMAIN_JA:
            self.pin_domain_combo.addItem(label, value)
        pin_form.addRow('ドメイン', self.pin_domain_combo)
        self.pin_key_edit = QLineEdit()
        self.pin_key_edit.setPlaceholderText('例: mlp_level_db')
        pin_form.addRow('メトリクスキー', self.pin_key_edit)
        self.pin_unit_combo = QComboBox()
        for label, value in _HEALTH_UNIT_JA:
            self.pin_unit_combo.addItem(label, value)
        pin_form.addRow('単位', self.pin_unit_combo)
        self.pin_expected_edit = QLineEdit()
        self.pin_expected_edit.setPlaceholderText('例: 82.1')
        pin_form.addRow('期待値（必須）', self.pin_expected_edit)
        self.pin_tolerance_edit = QLineEdit()
        self.pin_tolerance_edit.setPlaceholderText('任意（例: 1.5）')
        pin_form.addRow('許容差', self.pin_tolerance_edit)
        self.pin_evidence_combo = QComboBox()
        pin_form.addRow('証拠', self.pin_evidence_combo)
        self.pin_memo_edit = QLineEdit()
        self.pin_memo_edit.setPlaceholderText('手入力の出典メモ（任意）')
        pin_form.addRow('出典メモ', self.pin_memo_edit)
        layout.addLayout(pin_form)

        pin_row = QHBoxLayout()
        pin_row.addStretch(1)
        self.pin_remove_button = QPushButton('ピンを削除', page)
        self.pin_remove_button.setToolTip('選択したメトリクスピンをベースライン定義から外します')
        self.pin_remove_button.clicked.connect(self._remove_pin)
        pin_row.addWidget(self.pin_remove_button)
        self.pin_add_button = QPushButton('ピンを追加', page)
        self.pin_add_button.setToolTip('ベースラインに検証対象のメトリクスピンを追加します')
        self.pin_add_button.clicked.connect(self._add_pin)
        pin_row.addWidget(self.pin_add_button)
        layout.addLayout(pin_row)

        self.pins_tree = QTreeWidget(page)
        self.pins_tree.setHeaderLabels(
            ['ドメイン', 'キー', '期待値', '許容差', '証拠']
        )
        self.pins_tree.setRootIsDecorated(False)
        self.pins_tree.setMinimumHeight(90)
        layout.addWidget(self.pins_tree)

        register_row = QHBoxLayout()
        register_row.addStretch(1)
        self.baseline_button = QPushButton('ベースラインを登録', page)
        self.baseline_button.setToolTip('現在のピン定義をベースライン権威として保存します')
        self.baseline_button.clicked.connect(self._register_baseline)
        register_row.addWidget(self.baseline_button)
        layout.addLayout(register_row)
        return page

    def _build_plan_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)

        form = QFormLayout()
        self.plan_baseline_combo = QComboBox()
        self.plan_baseline_combo.currentIndexChanged.connect(
            self._refresh_check_pin_options
        )
        form.addRow('ベースライン', self.plan_baseline_combo)
        layout.addLayout(form)

        layout.addWidget(_section_title('登録済みチェック計画', page))
        self.plans = QTreeWidget(page)
        self.plans.setHeaderLabels(['計画', '作成日時', 'チェック数'])
        self.plans.setRootIsDecorated(False)
        self.plans.setMinimumHeight(80)
        layout.addWidget(self.plans)

        layout.addWidget(_section_title('チェック項目を追加', page))
        check_form = QFormLayout()
        self.check_description_edit = QLineEdit()
        self.check_description_edit.setPlaceholderText(
            '例: MLP のFLレベルを測定'
        )
        check_form.addRow('説明（必須）', self.check_description_edit)
        self.check_domain_combo = QComboBox()
        for label, value in _HEALTH_DOMAIN_JA:
            self.check_domain_combo.addItem(label, value)
        check_form.addRow('ドメイン', self.check_domain_combo)
        self.check_preset_check = QCheckBox(
            'ベースラインの運用プリセットを前提にする'
        )
        check_form.addRow('前提条件', self.check_preset_check)
        layout.addLayout(check_form)

        layout.addWidget(_section_title('関連ピン（ベースライン内）', page))
        self.check_pins_tree = QTreeWidget(page)
        self.check_pins_tree.setHeaderLabels(['ピン', '期待値'])
        self.check_pins_tree.setRootIsDecorated(False)
        self.check_pins_tree.setMinimumHeight(70)
        layout.addWidget(self.check_pins_tree)

        check_row = QHBoxLayout()
        check_row.addStretch(1)
        self.check_add_button = QPushButton('項目を追加', page)
        self.check_add_button.setToolTip('チェック計画に検証項目を追加します')
        self.check_add_button.clicked.connect(self._add_check_item)
        check_row.addWidget(self.check_add_button)
        layout.addLayout(check_row)

        layout.addWidget(_section_title('この計画に含める項目', page))
        self.pending_checks = QTreeWidget(page)
        self.pending_checks.setHeaderLabels(
            ['説明', 'ドメイン', 'ピン数', '前提プリセット']
        )
        self.pending_checks.setRootIsDecorated(False)
        self.pending_checks.setMinimumHeight(70)
        layout.addWidget(self.pending_checks)

        plan_row = QHBoxLayout()
        self.check_remove_button = QPushButton('項目を削除', page)
        self.check_remove_button.setToolTip('選択した検証項目を計画から外します')
        self.check_remove_button.clicked.connect(self._remove_check_item)
        plan_row.addWidget(self.check_remove_button)
        plan_row.addStretch(1)
        self.plan_button = QPushButton('計画を保存', page)
        self.plan_button.setToolTip('現在の検証項目をベースラインに紐付く計画として保存します')
        self.plan_button.clicked.connect(self._save_plan)
        plan_row.addWidget(self.plan_button)
        layout.addLayout(plan_row)
        return page

    def _build_run_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)

        form = QFormLayout()
        self.run_plan_combo = QComboBox()
        self.run_plan_combo.currentIndexChanged.connect(self._build_run_rows)
        form.addRow('チェック計画', self.run_plan_combo)
        self.run_context_combo = QComboBox()
        form.addRow('観測時の運用プリセット', self.run_context_combo)
        self.run_repeatability_check = QCheckBox(
            '繰り返し・不確かさの証拠を記録した'
        )
        form.addRow('証拠', self.run_repeatability_check)
        self.run_trigger_combo = QComboBox()
        for label, value in _HEALTH_TRIGGER_JA:
            self.run_trigger_combo.addItem(label, value)
        form.addRow('契機', self.run_trigger_combo)
        layout.addLayout(form)

        layout.addWidget(
            _section_title('チェックごとの観測（証拠を選ぶと記録されます）', page)
        )
        self.run_checks_host = QVBoxLayout()
        layout.addLayout(self.run_checks_host)

        run_row = QHBoxLayout()
        run_row.addStretch(1)
        self.run_button = QPushButton('チェックを実行して記録', page)
        self.run_button.setToolTip('選択した計画を実行し、結果を実行権威として記録します')
        self.run_button.clicked.connect(self._record_run)
        run_row.addWidget(self.run_button)
        layout.addLayout(run_row)

        layout.addWidget(_section_title('最新の結果', page))
        self.run_results = QTreeWidget(page)
        self.run_results.setHeaderLabels(['チェック', '状態', '理由'])
        self.run_results.setRootIsDecorated(False)
        self.run_results.setMinimumHeight(80)
        layout.addWidget(self.run_results)
        return page

    # -- shared helpers -------------------------------------------------------

    def _authority_options(self) -> list[_AuthorityOption]:
        return _persisted_authority_options(
            document_id=self.document_id,
            scene_repository=self.scene_repository,
            measurement_repository=self.measurement_repository,
            prediction_repository=self.prediction_repository,
            preset_repository=self.preset_repository,
            variant_repository=self.variant_repository,
        )

    def _load_catalog(
        self,
    ) -> tuple[
        tuple,
        tuple,
        dict[str, tuple],
        tuple,
    ]:
        """One catalog snapshot — baselines, plans (document-wide), presets.

        Refresh passes used to re-issue ``list_baselines`` four times and
        ``list_plans``/``list_presets`` twice per pass (plus a per-plan
        baseline re-verify inside the repository). The dialog needs one
        consistent view, so every surface reuses this snapshot.
        """

        baselines = self.health_repository.list_baselines(self.document_id)
        plans = self.health_repository.list_plans_for_document(
            self.document_id
        )
        plans_by_baseline: dict[str, list] = {}
        for plan in plans:
            plans_by_baseline.setdefault(plan.baseline_id, []).append(plan)
        presets = self.preset_repository.list_presets(self.document_id)
        return baselines, plans, plans_by_baseline, presets

    def reload(self) -> None:
        """Re-list every persisted object the dialog surfaces."""
        catalog = self._load_catalog()
        self._refresh_baselines(*catalog)
        self._refresh_plan_pick(*catalog)
        self._refresh_evidence_options(catalog[3])

    def _refresh_baselines(
        self,
        baselines: tuple | None = None,
        _plans: tuple | None = None,
        plans_by_baseline: dict | None = None,
        presets: tuple | None = None,
    ) -> None:
        if baselines is None or plans_by_baseline is None or presets is None:
            baselines, _plans, plans_by_baseline, presets = (
                self._load_catalog()
            )
        preset_names = {preset.preset_id: preset.name for preset in presets}
        self.baselines.clear()
        for baseline in baselines:
            preset_name = ''
            if baseline.operating_preset_id is not None:
                preset_name = preset_names.get(
                    baseline.operating_preset_id, '解決不能'
                )
            item = QTreeWidgetItem(
                [
                    baseline.name,
                    baseline.created_at_utc[:19],
                    str(len(baseline.metric_pins)),
                    preset_name,
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, baseline)
            self.baselines.addTopLevelItem(item)
        if self.baselines.topLevelItemCount() == 0:
            empty = QTreeWidgetItem(['（登録なし）', '', '', ''])
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.baselines.addTopLevelItem(empty)
        self._refresh_plan_baseline_combo(baselines, plans_by_baseline)

    def _refresh_plan_baseline_combo(
        self,
        baselines: tuple,
        plans_by_baseline: dict,
    ) -> None:
        current = self.plan_baseline_combo.currentData()
        self.plan_baseline_combo.blockSignals(True)
        self.plan_baseline_combo.clear()
        for baseline in baselines:
            self.plan_baseline_combo.addItem(baseline.name, baseline)
        index = self.plan_baseline_combo.findData(current)
        if index >= 0:
            self.plan_baseline_combo.setCurrentIndex(index)
        self.plan_baseline_combo.blockSignals(False)
        self._refresh_check_pin_options()
        self._refresh_plans(baselines, plans_by_baseline)

    def _refresh_plans(
        self,
        baselines: tuple | None = None,
        plans_by_baseline: dict | None = None,
    ) -> None:
        if baselines is None or plans_by_baseline is None:
            baselines, _plans, plans_by_baseline, _presets = (
                self._load_catalog()
            )
        self.plans.clear()
        for baseline in baselines:
            for plan in plans_by_baseline.get(baseline.baseline_id, ()):
                item = QTreeWidgetItem(
                    [
                        _short(plan.plan_id),
                        plan.created_at_utc[:19],
                        str(len(plan.checks)),
                    ]
                )
                item.setData(0, Qt.ItemDataRole.UserRole, plan)
                self.plans.addTopLevelItem(item)
        if self.plans.topLevelItemCount() == 0:
            empty = QTreeWidgetItem(['（登録なし）', '', ''])
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.plans.addTopLevelItem(empty)
        self._refresh_plan_pick(baselines, None, plans_by_baseline, None)

    def _refresh_plan_pick(
        self,
        baselines: tuple | None = None,
        _plans: tuple | None = None,
        plans_by_baseline: dict | None = None,
        _presets: tuple | None = None,
    ) -> None:
        if baselines is None or plans_by_baseline is None:
            baselines, _plans, plans_by_baseline, _presets = (
                self._load_catalog()
            )
        current = self.run_plan_combo.currentData()
        self.run_plan_combo.blockSignals(True)
        self.run_plan_combo.clear()
        for baseline in baselines:
            for plan in plans_by_baseline.get(baseline.baseline_id, ()):
                self.run_plan_combo.addItem(
                    f'{baseline.name} · {_short(plan.plan_id)}', plan
                )
        index = self.run_plan_combo.findData(current)
        if index >= 0:
            self.run_plan_combo.setCurrentIndex(index)
        self.run_plan_combo.blockSignals(False)
        self._build_run_rows()

    def _refresh_evidence_options(
        self,
        presets: tuple | None = None,
    ) -> None:
        if presets is None:
            presets = self.preset_repository.list_presets(self.document_id)
        # Pin evidence allows the honest declared-provenance entry;
        # observation evidence is persisted-authority picks only.
        manual_current = self.pin_evidence_combo.currentData()
        self.pin_evidence_combo.clear()
        self.pin_evidence_combo.addItem('手入力（オペレーター入力の値）', 'manual')
        for option in self._authority_options():
            self.pin_evidence_combo.addItem(option.text, option)
        index = self.pin_evidence_combo.findData(manual_current)
        if index >= 0:
            self.pin_evidence_combo.setCurrentIndex(index)

        current_preset = self.run_context_combo.currentData()
        self.run_context_combo.clear()
        self.run_context_combo.addItem('（なし）', None)
        for preset in presets:
            self.run_context_combo.addItem(
                preset.name,
                HealthAuthorityRef(
                    kind='operating_preset',
                    ref_id=preset.preset_id,
                    ref_sha256=preset.preset_sha256,
                    label=preset.name,
                ),
            )
        index = self.run_context_combo.findData(current_preset)
        if index >= 0:
            self.run_context_combo.setCurrentIndex(index)

        current_bp = self.baseline_preset_combo.currentData()
        self.baseline_preset_combo.clear()
        self.baseline_preset_combo.addItem('（なし）', None)
        for preset in presets:
            self.baseline_preset_combo.addItem(preset.name, preset)
        index = self.baseline_preset_combo.findData(current_bp)
        if index >= 0:
            self.baseline_preset_combo.setCurrentIndex(index)
        self._build_run_rows()

    def _selected_plan_baseline(self):
        return self.plan_baseline_combo.currentData()

    def _refresh_check_pin_options(self) -> None:
        baseline = self._selected_plan_baseline()
        self.check_pins_tree.clear()
        has_preset = (
            baseline is not None and baseline.operating_preset_id is not None
        )
        self.check_preset_check.setEnabled(has_preset)
        self.check_preset_check.setChecked(has_preset)
        self.check_preset_check.setText(
            'ベースラインの運用プリセットを前提にする'
            if has_preset
            else '前提条件なし（ベースラインにプリセットがありません）'
        )
        if baseline is None:
            return
        for pin in baseline.metric_pins:
            item = QTreeWidgetItem(
                [
                    f'{_domain_label(pin.domain)} · {pin.metric_key}',
                    pin.expected_repr,
                ]
            )
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Checked)
            item.setData(0, Qt.ItemDataRole.UserRole, pin)
            self.check_pins_tree.addTopLevelItem(item)

    def _build_run_rows(self) -> None:
        while self.run_checks_host.count():
            item = self.run_checks_host.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            elif item.layout() is not None:
                child = item.layout()
                while child.count():
                    sub = child.takeAt(0)
                    if sub.widget() is not None:
                        sub.widget().deleteLater()
        self._run_rows = []
        plan = self.run_plan_combo.currentData()
        if plan is None:
            self.run_button.setEnabled(False)
            return
        self.run_button.setEnabled(True)
        baseline = self.health_repository.get_baseline(plan.baseline_id)
        pins_by_id = {
            pin.pin_id: pin for pin in (baseline.metric_pins if baseline else ())
        }
        for check in plan.checks:
            frame_layout = QVBoxLayout()
            title = QLabel(
                f'{check.description}（{_domain_label(check.domain)}）'
            )
            frame_layout.addWidget(title)
            form = QFormLayout()
            evidence = QComboBox()
            evidence.addItem('（観測なし — このチェックをスキップ）', None)
            for option in self._authority_options():
                evidence.addItem(option.text, option)
            evidence.setCurrentIndex(0)
            form.addRow('証拠', evidence)
            pin_fields: list[tuple[HealthMetricPin, QLineEdit]] = []
            for pin_id in check.related_pin_ids:
                pin = pins_by_id.get(pin_id)
                if pin is None:
                    continue
                edit = QLineEdit()
                unit = _repr_unit(pin.expected_repr)
                try:
                    expected = json.loads(pin.expected_repr)
                    numeric = next(
                        (
                            value
                            for value in expected.values()
                            if isinstance(value, (int, float))
                        ),
                        None,
                    )
                except (ValueError, AttributeError):
                    numeric = None
                edit.setPlaceholderText(
                    f'期待: {numeric} {_unit_label(unit)}'
                    if numeric is not None
                    else '観測値'
                )
                form.addRow(
                    f'{pin.metric_key}（{_unit_label(unit)}）', edit
                )
                pin_fields.append((pin, edit))
            frame_layout.addLayout(form)
            self.run_checks_host.addLayout(frame_layout)
            self._run_rows.append(
                {
                    'check': check,
                    'evidence': evidence,
                    'pins': pin_fields,
                }
            )

    # -- writes ---------------------------------------------------------------

    def _add_pin(self) -> None:
        key = self.pin_key_edit.text().strip()
        if not key:
            self.status_label.setText('メトリクスキーを入力してください。')
            return
        try:
            expected = _parse_required_float(self.pin_expected_edit.text())
        except ValueError:
            self.status_label.setText('期待値は数値で入力してください。')
            return
        try:
            tolerance = _parse_float(self.pin_tolerance_edit.text())
        except ValueError:
            self.status_label.setText('許容差は数値で入力してください。')
            return
        if tolerance is not None and tolerance <= 0:
            self.status_label.setText('許容差は正の数値で入力してください。')
            return
        unit = self.pin_unit_combo.currentData()
        if unit == 'unitless' and tolerance is not None:
            self.status_label.setText(
                '単位なしのピンには許容差を付けられません。'
            )
            return
        evidence_data = self.pin_evidence_combo.currentData()
        if evidence_data == 'manual':
            memo = self.pin_memo_edit.text().strip()
            evidence_ref = HealthAuthorityRef(
                kind='operator_entry',
                ref_id='operator-entry',
                label=memo or 'オペレーター入力',
            )
        elif isinstance(evidence_data, _AuthorityOption):
            evidence_ref = evidence_data.ref
        else:
            self.status_label.setText('証拠の選択が不正です。')
            return
        pin = HealthMetricPin(
            pin_id=f'pin-{len(self._pins) + 1}',
            domain=self.pin_domain_combo.currentData(),
            metric_key=key,
            evidence_ref=evidence_ref,
            expected_repr=_repr_text(unit, expected),
            tolerance_repr=(
                json.dumps(
                    {_UNIT_TOLERANCE_KEY[unit]: tolerance},
                    separators=(',', ':'),
                    sort_keys=True,
                )
                if tolerance is not None
                else None
            ),
        )
        if any(existing.metric_key == key for existing in self._pins):
            self.status_label.setText('同じキーのピンがすでにあります。')
            return
        self._pins.append(pin)
        evidence_text = (
            pin.evidence_ref.label
            or f'{pin.evidence_ref.kind}:{_short(pin.evidence_ref.ref_id)}'
        )
        item = QTreeWidgetItem(
            [
                _domain_label(pin.domain),
                pin.metric_key,
                pin.expected_repr,
                pin.tolerance_repr or '—',
                evidence_text,
            ]
        )
        item.setData(0, Qt.ItemDataRole.UserRole, pin)
        self.pins_tree.addTopLevelItem(item)
        self.pin_key_edit.clear()
        self.pin_expected_edit.clear()
        self.pin_tolerance_edit.clear()
        self.pin_memo_edit.clear()

    def _remove_pin(self) -> None:
        for item in self.pins_tree.selectedItems():
            pin = item.data(0, Qt.ItemDataRole.UserRole)
            self._pins = [candidate for candidate in self._pins if candidate is not pin]
            index = self.pins_tree.indexOfTopLevelItem(item)
            if index >= 0:
                self.pins_tree.takeTopLevelItem(index)

    def _register_baseline(self) -> None:
        name = self.baseline_name_edit.text().strip()
        if not name:
            self.status_label.setText('ベースライン名を入力してください。')
            return
        if not self._pins:
            self.status_label.setText(
                '少なくとも1つのメトリクスピンを追加してください。'
            )
            return
        head = self.scene_repository.current_head(self.document_id)
        if head is None:
            self.status_label.setText('先に部屋を保存してください。')
            return
        preset = self.baseline_preset_combo.currentData()
        source_refs: list[HealthAuthorityRef] = []
        seen: set[tuple[str, str]] = set()
        for pin in self._pins:
            ref = pin.evidence_ref
            if ref.kind in _PERSISTED_REF_KINDS and (ref.kind, ref.ref_id) not in seen:
                seen.add((ref.kind, ref.ref_id))
                source_refs.append(ref)
        try:
            baseline = build_health_baseline(
                document_id=self.document_id,
                name=name,
                scene_revision_id=head.revision_id,
                scene_content_hash=head.content_hash,
                operating_preset_id=(
                    preset.preset_id if preset is not None else None
                ),
                operating_preset_sha256=(
                    preset.preset_sha256 if preset is not None else None
                ),
                source_refs=source_refs,
                metric_pins=tuple(self._pins),
                created_at_utc=_utc_now(),
            )
            self.health_repository.save_baseline(baseline)
            self.health_repository.verify_persisted_baseline(
                baseline.baseline_id
            )
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: baseline save accept — expected validation/store errors surface verbatim; unexpected errors propagate to diagnostics
            self.status_label.setText(f'登録できませんでした: {operation_error_message(exc)}')
            return
        self.status_label.setText(f'ベースライン「{name}」を登録しました。')
        self._pins = []
        self.pins_tree.clear()
        self.baseline_name_edit.clear()
        self.reload()

    def _add_check_item(self) -> None:
        baseline = self._selected_plan_baseline()
        if baseline is None:
            self.status_label.setText('ベースラインを選択してください。')
            return
        description = self.check_description_edit.text().strip()
        if not description:
            self.status_label.setText('チェックの説明を入力してください。')
            return
        related = tuple(
            self.check_pins_tree.topLevelItem(index)
            .data(0, Qt.ItemDataRole.UserRole)
            .pin_id
            for index in range(self.check_pins_tree.topLevelItemCount())
            if self.check_pins_tree.topLevelItem(index).checkState(0)
            == Qt.CheckState.Checked
        )
        required_context: tuple[HealthAuthorityRef, ...] = ()
        if (
            self.check_preset_check.isChecked()
            and baseline.operating_preset_id is not None
        ):
            required_context = (
                HealthAuthorityRef(
                    kind='operating_preset',
                    ref_id=baseline.operating_preset_id,
                    ref_sha256=baseline.operating_preset_sha256,
                ),
            )
        item = HealthCheckItem(
            check_id=f'check-{len(self._pending_checks) + 1}',
            domain=self.check_domain_combo.currentData(),
            description=description,
            related_pin_ids=related,
            required_context=required_context,
        )
        self._pending_checks.append(item)
        row = QTreeWidgetItem(
            [
                description,
                _domain_label(item.domain),
                str(len(related)),
                'あり' if required_context else 'なし',
            ]
        )
        row.setData(0, Qt.ItemDataRole.UserRole, item)
        self.pending_checks.addTopLevelItem(row)
        self.check_description_edit.clear()

    def _remove_check_item(self) -> None:
        for item in self.pending_checks.selectedItems():
            check = item.data(0, Qt.ItemDataRole.UserRole)
            self._pending_checks = [
                candidate
                for candidate in self._pending_checks
                if candidate is not check
            ]
            index = self.pending_checks.indexOfTopLevelItem(item)
            if index >= 0:
                self.pending_checks.takeTopLevelItem(index)

    def _save_plan(self) -> None:
        baseline = self._selected_plan_baseline()
        if baseline is None:
            self.status_label.setText('ベースラインを選択してください。')
            return
        if not self._pending_checks:
            self.status_label.setText(
                '少なくとも1つのチェック項目を追加してください。'
            )
            return
        try:
            plan = build_health_check_plan(
                baseline,
                checks=tuple(self._pending_checks),
                created_at_utc=_utc_now(),
            )
            self.health_repository.save_plan(plan)
            self.health_repository.verify_persisted_plan(plan.plan_id)
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: plan save accept — expected validation/store errors surface verbatim; unexpected errors propagate to diagnostics
            self.status_label.setText(f'計画を保存できませんでした: {operation_error_message(exc)}')
            return
        self.status_label.setText('チェック計画を保存しました。')
        self._pending_checks = []
        self.pending_checks.clear()
        self._refresh_plans()

    def _record_run(self) -> None:
        plan = self.run_plan_combo.currentData()
        if plan is None:
            self.status_label.setText('チェック計画を選択してください。')
            return
        baseline = self.health_repository.get_baseline(plan.baseline_id)
        if baseline is None:
            self.status_label.setText('ベースラインが解決できません。')
            return
        context_refs: tuple[HealthAuthorityRef, ...] = ()
        context = self.run_context_combo.currentData()
        if isinstance(context, HealthAuthorityRef):
            context_refs = (context,)
        observations: list[HealthObservation] = []
        for row in self._run_rows:
            evidence_data = row['evidence'].currentData()
            if not isinstance(evidence_data, _AuthorityOption):
                continue  # honestly skipped — the check reports not_run
            pin_map: dict = {}
            for pin, edit in row['pins']:
                text = edit.text().strip()
                if not text:
                    continue
                try:
                    value = float(text)
                except ValueError:
                    self.status_label.setText(
                        f'「{row["check"].description}」の観測値は数値で'
                        '入力してください。'
                    )
                    return
                unit = _repr_unit(pin.expected_repr) or 'unitless'
                pin_map[pin.pin_id] = {_UNIT_VALUE_KEY[unit]: value}
            observed_repr = (
                json.dumps(pin_map, separators=(',', ':'), sort_keys=True)
                if pin_map
                else None
            )
            observations.append(
                HealthObservation(
                    check_id=row['check'].check_id,
                    evidence_ref=evidence_data.ref,
                    context_refs=context_refs,
                    observed_repr=observed_repr,
                    has_repeatability_evidence=(
                        self.run_repeatability_check.isChecked()
                    ),
                )
            )
        try:
            run = run_health_check(
                plan,
                baseline,
                observations=tuple(observations),
                trigger=self.run_trigger_combo.currentData(),
                created_at_utc=_utc_now(),
            )
            self.health_repository.save_run(run)
            self.health_repository.verify_persisted_run(run.run_id)
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: run record accept — expected validation/store errors surface verbatim; unexpected errors propagate to diagnostics
            self.status_label.setText(f'チェックを記録できませんでした: {operation_error_message(exc)}')
            return
        self.status_label.setText('チェック結果を記録しました。')
        self.run_results.clear()
        descriptions = {
            check.check_id: check.description for check in plan.checks
        }
        for assessment in run.assessments:
            item = QTreeWidgetItem(
                [
                    descriptions.get(assessment.check_id, assessment.check_id),
                    _HEALTH_STATE_JA.get(assessment.state, assessment.state),
                    assessment.reason,
                ]
            )
            self.run_results.addTopLevelItem(item)


# ----------------------------------------------------------------------
# REV44-STAGED: operating-preset record entry
#
# ``save_preset`` / ``save_applied_state`` had live readers (the health
# dialog's preset picks, the ``operating_preset_created`` /
# ``operating_preset_applied`` activity events, preset pins inside health
# baselines) but no production writer — every list stayed empty. This
# dialog records only operator-declared state: a preset is an exact named
# configuration pinned to the current scene head, and an applied state is
# the operator's confirmation that the real devices matched it at a time.

_PRESET_CATEGORY_JA = {
    'movie': '映画',
    'music': '音楽',
    'game': 'ゲーム',
    'night': '夜間',
    'custom': 'カスタム',
}

_DECLARED_INPUT_JA = {
    'stereo_pcm': 'ステレオPCM',
    'channel_5_1': '5.1ch',
    'channel_7_1': '7.1ch',
    'object_audio': 'オブジェクトオーディオ',
    'game_low_latency': 'ゲーム低遅延',
    'unknown': '不明',
}


class OperatingPresetRecordDialog(QDialog):
    """Register ``TheaterOperatingPreset`` rows and record applied states."""

    def __init__(
        self,
        *,
        scene_repository: SceneRepository,
        document_id: str,
        preset_repository: CadOperatingPresetRepository | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.scene_repository = scene_repository
        self.document_id = document_id
        self.preset_repository = (
            preset_repository or CadOperatingPresetRepository(scene_repository)
        )
        self._head = None

        self.setWindowTitle('運用プリセットの記録')
        self.setMinimumWidth(680)
        layout = QVBoxLayout(self)

        self.tabs = QTabWidget(self)
        layout.addWidget(self.tabs)
        self.tabs.addTab(
            _scroll_wrap(self._build_register_tab()), 'プリセット登録'
        )
        self.tabs.addTab(
            _scroll_wrap(self._build_apply_tab()), '適用の記録'
        )

        self.status_label = QLabel('')
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.reload()

    # -- tab: register ------------------------------------------------------

    def _build_register_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)

        layout.addWidget(_section_title('登録済みプリセット', page))
        self.presets = QTreeWidget(page)
        self.presets.setHeaderLabels(
            ['名前', 'カテゴリ', '入力', '機器モード', '登録日時']
        )
        self.presets.setRootIsDecorated(False)
        self.presets.setMinimumHeight(110)
        layout.addWidget(self.presets)

        layout.addWidget(_section_title('プリセットを登録', page))
        pin_form = QFormLayout()
        self.scene_pin_label = QLabel(pin_form.parentWidget())
        self.scene_pin_label.setWordWrap(True)
        pin_form.addRow('シーン', self.scene_pin_label)
        layout.addLayout(pin_form)

        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText('例: 映画（夜間）')
        form.addRow('名前（必須）', self.name_edit)
        self.category_combo = QComboBox()
        for value, label in _PRESET_CATEGORY_JA.items():
            self.category_combo.addItem(label, value)
        form.addRow('カテゴリ', self.category_combo)
        self.input_combo = QComboBox()
        for value, label in _DECLARED_INPUT_JA.items():
            self.input_combo.addItem(label, value)
        self.input_combo.setCurrentIndex(len(_DECLARED_INPUT_JA) - 1)
        form.addRow('再生入力', self.input_combo)
        self.device_mode_edit = QLineEdit()
        self.device_mode_edit.setPlaceholderText('例: Movie、Pure Direct')
        form.addRow('機器モード', self.device_mode_edit)
        level_row = QHBoxLayout()
        self.level_enabled_check = QCheckBox('再生レベルを指定', page)
        self.level_spin = QDoubleSpinBox()
        self.level_spin.setRange(-80.0, 20.0)
        self.level_spin.setDecimals(1)
        self.level_spin.setValue(-20.0)
        self.level_spin.setSuffix(' dB')
        self.level_spin.setEnabled(False)
        self.level_enabled_check.toggled.connect(self.level_spin.setEnabled)
        level_row.addWidget(self.level_enabled_check)
        level_row.addWidget(self.level_spin)
        level_row.addStretch(1)
        level_host = QWidget()
        level_host.setLayout(level_row)
        form.addRow('公称再生レベル', level_host)
        self.purpose_edit = QLineEdit()
        self.purpose_edit.setPlaceholderText('例: 深夜は低レベルで映画を観る')
        form.addRow('用途メモ', self.purpose_edit)
        layout.addLayout(form)

        self.register_button = QPushButton('プリセットを登録', page)
        self.register_button.setToolTip('現在の偏差・条件を運用プリセットとして登録します')
        self.register_button.clicked.connect(self._register_preset)
        layout.addWidget(self.register_button)
        layout.addStretch(1)
        return page

    # -- tab: applied state -------------------------------------------------

    def _build_apply_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)

        layout.addWidget(_section_title('適用の記録済み一覧', page))
        self.applied_states = QTreeWidget(page)
        self.applied_states.setHeaderLabels(
            ['プリセット', '確認日時', '機器状態', 'メモ']
        )
        self.applied_states.setRootIsDecorated(False)
        self.applied_states.setMinimumHeight(110)
        layout.addWidget(self.applied_states)

        layout.addWidget(_section_title('適用を記録', page))
        form = QFormLayout()
        self.apply_preset_combo = QComboBox()
        form.addRow('プリセット', self.apply_preset_combo)
        self.device_context_edit = QLineEdit()
        self.device_context_edit.setPlaceholderText(
            '例: AVR=Movieモード、サブウーファーON'
        )
        form.addRow('確認時の機器状態', self.device_context_edit)
        self.deviation_edit = QLineEdit()
        self.deviation_edit.setPlaceholderText(
            '例: センターレベルを+1dBに変更（任意）'
        )
        form.addRow('プリセットとの差異', self.deviation_edit)
        self.note_edit = QLineEdit()
        form.addRow('メモ', self.note_edit)
        layout.addLayout(form)

        self.apply_button = QPushButton('適用を記録', page)
        self.apply_button.setToolTip('選択したプリセットの適用を権威として記録します')
        self.apply_button.clicked.connect(self._record_applied_state)
        layout.addWidget(self.apply_button)
        layout.addStretch(1)
        return page

    # -- refresh -----------------------------------------------------------

    def reload(self) -> None:
        """Re-list persisted presets/applied states + refresh the scene pin."""
        head = self.scene_repository.current_head(self.document_id)
        self._head = head
        if head is None:
            self.scene_pin_label.setText('（保存済みの部屋がありません）')
            self.register_button.setEnabled(False)
        else:
            self.scene_pin_label.setText(
                f'{_short(head.revision_id)} · ハッシュ {_short(head.content_hash)}'
            )
            self.register_button.setEnabled(True)

        self.presets.clear()
        presets = self.preset_repository.list_presets(self.document_id)
        for preset in presets:
            item = QTreeWidgetItem(
                [
                    preset.name,
                    _PRESET_CATEGORY_JA.get(preset.category, preset.category),
                    _DECLARED_INPUT_JA.get(
                        preset.declared_input, preset.declared_input
                    ),
                    preset.declared_device_mode or '—',
                    preset.created_at_utc[:19],
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, preset)
            self.presets.addTopLevelItem(item)
        if self.presets.topLevelItemCount() == 0:
            empty = QTreeWidgetItem(['（登録なし）', '', '', '', ''])
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.presets.addTopLevelItem(empty)

        names = {preset.preset_id: preset.name for preset in presets}
        self.applied_states.clear()
        applied_rows = [
            applied
            for preset in presets
            for applied in self.preset_repository.list_applied_states(
                preset.preset_id
            )
        ]
        for applied in applied_rows:
            deviation = next(
                (item.value for item in applied.deviations), ''
            )
            item = QTreeWidgetItem(
                [
                    names.get(applied.preset_id, _short(applied.preset_id)),
                    applied.confirmed_at_utc[:19],
                    applied.device_context or '—',
                    deviation or applied.note or '—',
                ]
            )
            self.applied_states.addTopLevelItem(item)
        if self.applied_states.topLevelItemCount() == 0:
            empty = QTreeWidgetItem(['（記録なし）', '', '', ''])
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.applied_states.addTopLevelItem(empty)

        self.apply_preset_combo.clear()
        for preset in presets:
            self.apply_preset_combo.addItem(preset.name, preset)
        self.apply_button.setEnabled(bool(presets))
        if not presets:
            self.apply_preset_combo.addItem('（プリセット未登録）', None)

    # -- writes -------------------------------------------------------------

    def _register_preset(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            self.status_label.setText('プリセット名を入力してください。')
            return
        if self._head is None:
            self.status_label.setText(
                '保存済みの部屋がないため、プリセットを登録できません。'
            )
            return
        device_mode = self.device_mode_edit.text().strip() or None
        purpose = self.purpose_edit.text().strip() or None
        level = (
            self.level_spin.value() if self.level_enabled_check.isChecked()
            else None
        )
        try:
            preset = build_operating_preset(
                document_id=self.document_id,
                name=name,
                scene_revision_id=self._head.revision_id,
                scene_content_hash=self._head.content_hash,
                category=self.category_combo.currentData(),
                purpose_note=purpose,
                declared_input=self.input_combo.currentData(),
                declared_device_mode=device_mode,
                nominal_playback_level_db=level,
                provenance=(
                    PresetProvenanceItem(
                        key='recorded_via',
                        value='運用プリセット記録ダイアログ',
                    ),
                ),
                created_at_utc=_utc_now(),
            )
            self.preset_repository.save_preset(preset)
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: preset save accept — expected validation/store errors surface verbatim; unexpected errors propagate to diagnostics
            self.status_label.setText(
                f'プリセットを登録できませんでした: {operation_error_message(exc)}'
            )
            return
        self.status_label.setText(f'プリセット「{name}」を登録しました。')
        self.name_edit.clear()
        self.reload()

    def _record_applied_state(self) -> None:
        preset = self.apply_preset_combo.currentData()
        if preset is None:
            self.status_label.setText(
                '適用を記録するには、先にプリセットを登録してください。'
            )
            return
        deviations = ()
        deviation_text = self.deviation_edit.text().strip()
        if deviation_text:
            deviations = (
                PresetProvenanceItem(
                    key='operator_deviation', value=deviation_text
                ),
            )
        try:
            applied = record_applied_preset_state(
                preset,
                confirmed_at_utc=_utc_now(),
                device_context=self.device_context_edit.text().strip() or None,
                deviations=deviations,
                note=self.note_edit.text().strip() or None,
            )
            self.preset_repository.save_applied_state(applied)
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: applied-state record accept — expected validation/store errors surface verbatim; unexpected errors propagate to diagnostics
            self.status_label.setText(f'適用を記録できませんでした: {operation_error_message(exc)}')
            return
        self.status_label.setText(
            f'「{preset.name}」の適用を記録しました。'
        )
        self.deviation_edit.clear()
        self.note_edit.clear()
        self.reload()


__all__ = [
    'AVSyncRecordDialog',
    'HealthCheckDialog',
    'OperatingPresetRecordDialog',
]
