"""Registration dialogs for the measurement quality authorities (REV44).

The authority families persisted through
``CadMeasurementQualityRepository`` (timing references, excitation
assets, stimulus profiles, acoustic level calibrations, dataset level
references, routing profiles) previously had no production write path —
they could only be constructed in tests. Each dialog below collects the
operator's declaration for one family, builds the sealed record through
its ``build_*`` constructor (the model validators + repository save-time
checks keep failing closed), and hands it back for the workspace to
persist. An exception leaves ``record`` unset so a rejected or invalid
dialog never persists anything.

Surface-type decision per family:

- ``CadMeasurementExcitationAsset`` is external evidence — a raw
  stimulus file — so it follows the calibration-file pick-button
  pattern: 「ファイルを選択…」 → ``read_file_bounded`` →
  ``build_excitation_asset`` → ``save_excitation_asset`` (bytes land in
  the content-addressed managed-asset store immediately, like the mic
  calibration-file retain, because the record is only evidence once the
  bytes are).
- ``CadMeasurementStimulusProfile`` / ``CadMeasurementTimingReference`` /
  ``CadAcousticLevelCalibration`` / ``CadRoutingProfile`` are operator
  declarations of acquisition conditions — typed registration forms.
- ``CadDatasetLevelReference`` is a derived binding to an existing
  dataset — the dialog chooses the declared level semantics (and the
  persisted calibration authority for ``absolute_spl``) and the record
  seals the already-persisted dataset hash.
"""

from __future__ import annotations

from hashlib import sha256
from math import isfinite
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

import wave

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import file_dialog_memory
from .cad_measurement_authorities import (
    CadTimingDelayCorrection,
    absolute_spl_evidence_gaps,
    build_acoustic_level_calibration,
    build_dataset_level_reference,
    build_routing_profile,
    build_timing_reference,
)
from .cad_measurement_stimulus import (
    CadMeasurementExcitationAsset,
    build_excitation_asset,
    build_stimulus_profile,
)
from .ingress import read_file_bounded
from .limits import MAX_ATTACHMENT_BYTES
from .ui_theme import SemanticState, set_semantic_state
from .user_facing_error import operation_error_message

if TYPE_CHECKING:
    from .cad_measurement_models import CadFrequencyResponseDataset
    from .measurement_workflow import (
        MeasurementView,
        MeasurementWorkflowController,
        SpeakerTarget,
    )


# --- Shared label vocabularies (JA) -----------------------------------------


TIMING_METHOD_LABELS: tuple[tuple[str, str], ...] = (
    ('音響基準信号（acoustic_reference）', 'acoustic_reference'),
    ('ループバック（loopback）', 'loopback'),
    ('共有クロック（shared_clock）', 'shared_clock'),
    ('外部同期（external_sync）', 'external_sync'),
    ('外部取り込み（imported · 共通タイミング権威にはなりません）', 'imported'),
    ('手動申告（manual · 共通タイミング権威にはなりません）', 'manual'),
)


def timing_method_label(method: str) -> str:
    for label, value in TIMING_METHOD_LABELS:
        if value == method:
            return label
    return method or '不明'


_TIMING_T0_LABELS: tuple[tuple[str, str], ...] = (
    ('音響基準信号のエッジ', 'acoustic_reference_signal'),
    ('ループバックのエッジ', 'loopback_edge'),
    ('スイープ開始', 'sweep_start'),
    ('外部（取り込み）', 'imported'),
    ('手動申告', 'manual'),
    ('未確認', 'unknown'),
)

_CORRECTION_KIND_LABELS: tuple[tuple[str, str], ...] = (
    ('ループバック経路', 'loopback_path'),
    ('出力バッファ', 'output_buffer'),
    ('入力バッファ', 'input_buffer'),
    ('ドライバ遅延', 'driver_latency'),
    ('音響距離', 'acoustic_distance'),
    ('外部（取り込み）', 'imported'),
    ('手動申告', 'manual'),
    ('未確認', 'unknown'),
)

_CORRECTION_SIGN_LABELS: tuple[tuple[str, str], ...] = (
    ('到達時刻から減算', 'subtract_from_arrival'),
    ('t0 に加算', 'add_to_t0'),
    ('生成側で移動済み', 'producer_shifted'),
    ('未確認', 'unknown'),
)

_CORRECTION_STATE_LABELS: tuple[tuple[str, str], ...] = (
    ('メタデータのみ（未適用）', 'metadata_only'),
    ('データセットに適用済み', 'applied_to_dataset'),
    ('生成側で適用済み', 'producer_applied'),
    ('未確認', 'unknown'),
)

LEVEL_METHOD_LABELS: tuple[tuple[str, str], ...] = (
    ('音響校正器（acoustic_calibrator）', 'acoustic_calibrator'),
    ('REW SPLセッション（rew_spl_session）', 'rew_spl_session'),
    ('基準メーター転送（reference_meter_transfer）', 'reference_meter_transfer'),
    (
        'メーカー公称感度（manufacturer_sensitivity · 絶対SPL権威にはなりません）',
        'manufacturer_sensitivity',
    ),
    ('外部取り込み（imported · 同上）', 'imported'),
    ('手動申告（manual · 同上）', 'manual'),
)


def level_method_label(method: str) -> str:
    for label, value in LEVEL_METHOD_LABELS:
        if value == method:
            return label
    return method or '不明'


_LEVEL_METHOD_HINTS = {
    'acoustic_calibrator': (
        '音響校正器で絶対SPLを根拠づけるには: 基準レベル・基準周波数・'
        '入力チェーン識別（機器識別子・入力デバイス・入力チャンネル・'
        '入力パス識別子のいずれか）・感度(V/Pa)またはセッション記録が必要です。'
    ),
    'rew_spl_session': (
        'REW SPLセッションで絶対SPLを根拠づけるには: 取得セッション識別子・'
        'セッション記録（機器プロファイルまたはセッションID）・入力チェーン識別・'
        '校正結果（感度または基準レベル）が必要です。'
    ),
    'reference_meter_transfer': (
        '基準メーター転送で絶対SPLを根拠づけるには: 基準器の機器識別子・'
        '対象入力チェーン（デバイスまたはチャンネル）・観測結果（基準レベル'
        'または感度）・転送セッション記録が必要です。'
    ),
    'manufacturer_sensitivity': (
        'メーカー公称感度は記録されますが、単体では絶対SPLを根拠づけられません。'
    ),
    'imported': '取り込み済み校正は記録されますが、絶対SPL権威にはなりません。',
    'manual': '手動申告は記録されますが、絶対SPL権威にはなりません。',
}

_LEVEL_SCOPE_LABELS: tuple[tuple[str, str], ...] = (
    ('この測定のみ（measurement）', 'measurement'),
    ('取得セッション全体（session）', 'session'),
    ('機器・入力パス（instrument）', 'instrument'),
    ('未指定（unknown · 絶対SPLには適用されません）', 'unknown'),
)

STIMULUS_KIND_LABELS: tuple[tuple[str, str], ...] = (
    ('ログスイープ', 'log_sweep'),
    ('固定サイン波', 'fixed_sine'),
    ('ピンクノイズ', 'pink_noise'),
    ('ホワイトノイズ', 'white_noise'),
    ('インパルス', 'impulse'),
    ('MLS', 'mls'),
    ('音声', 'speech'),
    ('プログラム素材', 'program_material'),
    ('トーンバースト', 'tone_burst'),
    ('チャンネル識別シーケンス', 'channel_ident_sequence'),
    ('AV同期テストメディア', 'av_sync_test_media'),
    ('外部信号（共有参照を証明できません）', 'external'),
    ('不明な外部信号（同上）', 'unknown_external'),
)


def stimulus_kind_label(kind: str) -> str:
    for label, value in STIMULUS_KIND_LABELS:
        if value == kind:
            return label
    return kind or '不明'


_STIMULUS_INTENT_LABELS: tuple[tuple[str, str], ...] = (
    ('計測', 'measurement'),
    ('検証', 'verification'),
    ('校正', 'calibration'),
    ('診断', 'diagnostic'),
    ('未指定', 'unknown'),
)

_DIGITAL_STIMULUS_KINDS = frozenset(
    {
        'log_sweep',
        'fixed_sine',
        'pink_noise',
        'white_noise',
        'impulse',
        'mls',
        'tone_burst',
        'channel_ident_sequence',
        'av_sync_test_media',
    }
)

LEVEL_REFERENCE_KIND_LABELS: tuple[tuple[str, str], ...] = (
    ('絶対SPL（absolute_spl · レベル校正権威に紐付け）', 'absolute_spl'),
    ('SPL（未校正 spl_uncalibrated）', 'spl_uncalibrated'),
    ('dBFS', 'dbfs'),
    ('Pa', 'pa'),
    ('相対レベル（relative）', 'relative'),
)


def level_reference_kind_label(kind: str) -> str:
    for label, value in LEVEL_REFERENCE_KIND_LABELS:
        if value == kind:
            return label
    return kind or '不明'


_ROUTING_VERIFICATION_LABELS: tuple[tuple[str, str], ...] = (
    ('検証済み', 'verified'),
    ('未検証', 'unverified'),
    ('混合', 'mixed'),
    ('検証不能', 'unavailable'),
)

_CHANNEL_ROLE_CHOICES: tuple[tuple[str, str], ...] = (
    ('不明', 'unknown'),
    ('フロント左 (FL)', 'front_left'),
    ('センター (C)', 'center'),
    ('フロント右 (FR)', 'front_right'),
    ('サラウンド左 (SL)', 'surround_left'),
    ('サラウンド右 (SR)', 'surround_right'),
    ('サブウーファー (LFE/Sub)', 'subwoofer'),
)


# --- Helpers -----------------------------------------------------------------


def _text_or_none(edit: QLineEdit | QComboBox) -> str | None:
    text = (
        edit.currentText() if isinstance(edit, QComboBox) else edit.text()
    ).strip()
    return text or None


def _combo_text_or_none(combo: QComboBox) -> str | None:
    text = combo.currentText().strip()
    return text or None


def _parse_optional_float(edit: QLineEdit, label: str) -> float | None:
    text = edit.text().strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError as exc:
        raise ValueError(f'{label}は数値で入力してください') from exc
    if not isfinite(value):
        raise ValueError(f'{label}は有限の数値で入力してください')
    return value


def _parse_optional_int(edit: QLineEdit, label: str) -> int | None:
    text = edit.text().strip()
    if not text:
        return None
    try:
        value = int(text)
    except ValueError as exc:
        raise ValueError(f'{label}は整数で入力してください') from exc
    return value


def _combo_with(items: Sequence[tuple[str, str]], parent: QWidget) -> QComboBox:
    combo = QComboBox(parent)
    for label, value in items:
        combo.addItem(label, value)
    return combo


def _dialog_buttons(dialog: QDialog) -> QDialogButtonBox:
    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Save
        | QDialogButtonBox.StandardButton.Cancel,
        dialog,
    )
    buttons.button(QDialogButtonBox.StandardButton.Save).setText('登録')
    buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('キャンセル')
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    return buttons


class _RecordDialog(QDialog):
    """Base: ``accept`` runs ``build_record``; build failures stay open."""

    def __init__(self, parent: QWidget | None, title: str) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.record: Any = None
        self.error_label = QLabel('', self)
        self.error_label.setWordWrap(True)
        self.error_label.setObjectName('authorityDialogError')
        set_semantic_state(self.error_label, SemanticState.ERROR)

    def build_record(self) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    def accept(self) -> None:
        try:
            self.record = self.build_record()
        except Exception as exc:
            self.error_label.setText(f'登録内容を確定できません: {operation_error_message(exc)}')
            return
        super().accept()


# --- Timing reference ---------------------------------------------------------


class TimingReferenceDialog(_RecordDialog):
    """Register a persisted measurement timing-reference authority.

    Scope is limited to the honestly bindable surfaces: ``persistent``
    (the signal-path fingerprint a later acquisition context can prove by
    declaring the same identity) and ``unknown`` (recorded evidence that
    can never authorize common timing). ``measurement``/``session``
    scopes require already-persisted subjects and cannot cover a
    measurement that is still being committed, so this dialog does not
    offer them.
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        default_signal_path: str | None = None,
    ) -> None:
        super().__init__(parent, 'タイミング基準を登録')
        self.resize(560, 520)
        layout = QVBoxLayout(self)

        note = QLabel(
            'タイミング基準は取得条件の共通タイミング根拠として記録されます。'
            '登録した基準は「割り当て」の取得条件で選択でき、'
            '共通タイミング権威になり得るのは機械的な同期証拠'
            '（音響基準信号・ループバック・共有クロック・外部同期）だけです。',
            self,
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        form = QFormLayout()
        self.method_combo = _combo_with(TIMING_METHOD_LABELS, self)
        form.addRow('方式', self.method_combo)
        self.reference_channel_edit = QLineEdit(self)
        self.reference_channel_edit.setPlaceholderText('例: loopback L / Mic ch2')
        form.addRow('基準チャネル', self.reference_channel_edit)
        self.input_clock_edit = QLineEdit(self)
        self.input_clock_edit.setPlaceholderText('例: USB-ADC 内蔵クロック')
        form.addRow('入力クロック識別子', self.input_clock_edit)
        self.output_clock_edit = QLineEdit(self)
        self.output_clock_edit.setPlaceholderText('例: HDMI-AVR DAC クロック')
        form.addRow('出力クロック識別子', self.output_clock_edit)
        self.sample_rate_edit = QLineEdit(self)
        self.sample_rate_edit.setPlaceholderText('例: 48000')
        form.addRow('サンプルレート (Hz)', self.sample_rate_edit)
        self.t0_combo = _combo_with(_TIMING_T0_LABELS, self)
        form.addRow('t0 規約', self.t0_combo)
        layout.addLayout(form)

        self.scope_combo = _combo_with(
            (
                ('同一信号パスで有効（persistent）', 'persistent'),
                ('未指定（unknown · 共通タイミングには使えません）', 'unknown'),
            ),
            self,
        )
        self.scope_combo.currentIndexChanged.connect(self._scope_changed)
        form2 = QFormLayout()
        form2.addRow('有効範囲', self.scope_combo)
        self.signal_path_edit = QLineEdit(self)
        self.signal_path_edit.setPlaceholderText(
            '例: HDMI-AVR→UMIK-1/USB（デバイス・経路・クロック構成の指紋）'
        )
        if default_signal_path:
            self.signal_path_edit.setText(default_signal_path)
        form2.addRow('信号パス識別子', self.signal_path_edit)
        layout.addLayout(form2)
        self._scope_changed()

        corrections_title = QLabel('遅延補正（任意・各行は型付きで記録されます）', self)
        layout.addWidget(corrections_title)
        self.corrections_table = QTableWidget(0, 4, self)
        self.corrections_table.setHorizontalHeaderLabels(
            ['種別', '値 (ms)', '符号', '適用状態']
        )
        self.corrections_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.corrections_table.verticalHeader().setVisible(False)
        self.corrections_table.setMaximumHeight(140)
        layout.addWidget(self.corrections_table)
        corrections_row = QHBoxLayout()
        add_button = QPushButton('補正を追加', self)
        add_button.clicked.connect(self._add_correction_row)
        corrections_row.addWidget(add_button)
        remove_button = QPushButton('選択行を削除', self)
        remove_button.clicked.connect(self._remove_correction_row)
        corrections_row.addWidget(remove_button)
        corrections_row.addStretch(1)
        layout.addLayout(corrections_row)

        layout.addWidget(self.error_label)
        layout.addWidget(_dialog_buttons(self))

    def _scope_changed(self) -> None:
        persistent = str(self.scope_combo.currentData()) == 'persistent'
        self.signal_path_edit.setEnabled(persistent)
        if not persistent:
            self.signal_path_edit.setToolTip(
                '有効範囲が「同一信号パス」のとき必須です'
            )

    def _add_correction_row(self) -> None:
        row = self.corrections_table.rowCount()
        self.corrections_table.insertRow(row)
        kind_combo = _combo_with(_CORRECTION_KIND_LABELS, self.corrections_table)
        sign_combo = _combo_with(_CORRECTION_SIGN_LABELS, self.corrections_table)
        state_combo = _combo_with(
            _CORRECTION_STATE_LABELS, self.corrections_table
        )
        self.corrections_table.setCellWidget(row, 0, kind_combo)
        self.corrections_table.setItem(row, 1, QTableWidgetItem(''))
        self.corrections_table.setCellWidget(row, 2, sign_combo)
        self.corrections_table.setCellWidget(row, 3, state_combo)

    def _remove_correction_row(self) -> None:
        rows = sorted(
            {item.row() for item in self.corrections_table.selectedItems()},
            reverse=True,
        )
        for row in rows:
            self.corrections_table.removeRow(row)

    def _collect_corrections(self) -> tuple[CadTimingDelayCorrection, ...]:
        corrections: list[CadTimingDelayCorrection] = []
        for row in range(self.corrections_table.rowCount()):
            kind_widget = self.corrections_table.cellWidget(row, 0)
            value_item = self.corrections_table.item(row, 1)
            sign_widget = self.corrections_table.cellWidget(row, 2)
            state_widget = self.corrections_table.cellWidget(row, 3)
            value_text = (value_item.text() if value_item else '').strip()
            if not value_text:
                raise ValueError(
                    f'遅延補正 {row + 1} 行目の値を入力してください'
                )
            try:
                value_s = float(value_text) / 1000.0
            except ValueError as exc:
                raise ValueError(
                    f'遅延補正 {row + 1} 行目の値は数値（ms）で入力してください'
                ) from exc
            if not isfinite(value_s):
                raise ValueError(
                    f'遅延補正 {row + 1} 行目の値は有限の数値で入力してください'
                )
            corrections.append(
                CadTimingDelayCorrection(
                    correction_kind=str(kind_widget.currentData()),  # type: ignore[union-attr]
                    value_s=value_s,
                    sign_convention=str(sign_widget.currentData()),  # type: ignore[union-attr]
                    application_state=str(state_widget.currentData()),  # type: ignore[union-attr]
                )
            )
        return tuple(corrections)

    def build_record(self) -> Any:
        scope = str(self.scope_combo.currentData())
        signal_path = _text_or_none(self.signal_path_edit)
        if scope == 'persistent' and not signal_path:
            raise ValueError(
                '「同一信号パスで有効」には信号パス識別子が必要です'
            )
        sample_rate = _parse_optional_int(self.sample_rate_edit, 'サンプルレート')
        if sample_rate is not None and sample_rate <= 0:
            raise ValueError('サンプルレートは正の整数で入力してください')
        method = str(self.method_combo.currentData())
        if method == 'shared_clock':
            if not (
                _text_or_none(self.input_clock_edit)
                and _text_or_none(self.output_clock_edit)
            ):
                raise ValueError(
                    '共有クロックには入力・出力両方のクロック識別子が必要です'
                )
        return build_timing_reference(
            method=method,  # type: ignore[arg-type]
            reference_channel=_text_or_none(self.reference_channel_edit),
            input_clock_identity=_text_or_none(self.input_clock_edit),
            output_clock_identity=_text_or_none(self.output_clock_edit),
            sample_rate_hz=sample_rate,
            t0_convention=str(self.t0_combo.currentData()),  # type: ignore[arg-type]
            delay_corrections=self._collect_corrections(),
            validity_scope=scope,  # type: ignore[arg-type]
            signal_path_identity=signal_path if scope == 'persistent' else None,
        )


# --- Acoustic level calibration ------------------------------------------------


class LevelCalibrationDialog(_RecordDialog):
    """Register an acoustic level-calibration authority.

    This is the declaration a SPL-authorizing ``absolute_spl`` claim and
    the SPL-readiness onboarding step read: only the three evidence-backed
    methods (acoustic calibrator / REW SPL session / reference-meter
    transfer) can authorize absolute SPL — imported/manual/datasheet
    methods persist as honest records that never promote a claim.
    """

    def __init__(
        self,
        views: Sequence['MeasurementView'],
        contexts: Sequence[Any],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, 'レベル校正を登録')
        self.resize(560, 620)
        layout = QVBoxLayout(self)

        note = QLabel(
            '絶対SPLを主張するには、その測定に適用できるレベル校正権威が必要です。'
            'ここでは校正の方式・機器・有効範囲を記録します。',
            self,
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        form = QFormLayout()
        self.method_combo = _combo_with(LEVEL_METHOD_LABELS, self)
        self.method_combo.currentIndexChanged.connect(self._method_changed)
        form.addRow('方式', self.method_combo)
        self.method_hint = QLabel('', self)
        self.method_hint.setWordWrap(True)
        form.addRow('', self.method_hint)
        self.instrument_identity_edit = QLineEdit(self)
        self.instrument_identity_edit.setPlaceholderText(
            '例: B&K 4231 / UMIK-1 S/N 123456'
        )
        form.addRow('機器識別子', self.instrument_identity_edit)
        self.instrument_profile_edit = QLineEdit(self)
        self.instrument_profile_edit.setPlaceholderText(
            '例: REW SPLセッション 2026-10-04（任意のセッション記録名）'
        )
        form.addRow('機器プロファイル/セッション記録', self.instrument_profile_edit)
        self.input_device_edit = QLineEdit(self)
        self.input_device_edit.setPlaceholderText('例: USB Audio (UMIK-1)')
        form.addRow('入力デバイス', self.input_device_edit)
        self.input_channel_edit = QLineEdit(self)
        self.input_channel_edit.setPlaceholderText('例: ch1')
        form.addRow('入力チャンネル', self.input_channel_edit)
        self.sensitivity_edit = QLineEdit(self)
        self.sensitivity_edit.setPlaceholderText('例: 12.6')
        form.addRow('感度 (mV/Pa)', self.sensitivity_edit)
        self.reference_level_edit = QLineEdit(self)
        self.reference_level_edit.setPlaceholderText('例: 94.0')
        form.addRow('基準レベル (dB SPL)', self.reference_level_edit)
        self.reference_frequency_edit = QLineEdit(self)
        self.reference_frequency_edit.setPlaceholderText('例: 1000')
        form.addRow('基準周波数 (Hz)', self.reference_frequency_edit)
        self.uncertainty_edit = QLineEdit(self)
        self.uncertainty_edit.setPlaceholderText('例: 0.3')
        form.addRow('不確かさ (dB)', self.uncertainty_edit)
        layout.addLayout(form)

        scope_form = QFormLayout()
        self.scope_combo = _combo_with(_LEVEL_SCOPE_LABELS, self)
        self.scope_combo.currentIndexChanged.connect(self._scope_changed)
        scope_form.addRow('有効範囲', self.scope_combo)

        self.measurement_combo = QComboBox(self)
        self._measurement_ids: list[str] = []
        for view in views:
            self.measurement_combo.addItem(
                f'{view.effective_target_name} · '
                f'{view.channel_role} · {view.imported_at}',
                view.measurement_id,
            )
            self._measurement_ids.append(view.measurement_id)
        scope_form.addRow('対象の測定', self.measurement_combo)

        self.session_combo = QComboBox(self)
        self.session_combo.setEditable(True)
        session_ids = sorted(
            {
                context.acquisition_session_id
                for context in contexts
                if getattr(context, 'acquisition_session_id', None)
            }
        )
        for session_id in session_ids:
            self.session_combo.addItem(session_id, session_id)
        self.session_combo.setEditText('')
        scope_form.addRow('取得セッション識別子', self.session_combo)

        self.input_path_combo = QComboBox(self)
        self.input_path_combo.setEditable(True)
        input_paths = sorted(
            {
                context.input_path_identity
                for context in contexts
                if getattr(context, 'input_path_identity', None)
            }
        )
        for path_id in input_paths:
            self.input_path_combo.addItem(path_id, path_id)
        self.input_path_combo.setEditText('')
        scope_form.addRow('入力パス識別子', self.input_path_combo)
        layout.addLayout(scope_form)
        self._scope_changed()
        self._method_changed()

        layout.addWidget(self.error_label)
        layout.addWidget(_dialog_buttons(self))

    def _method_changed(self) -> None:
        self.method_hint.setText(
            _LEVEL_METHOD_HINTS.get(str(self.method_combo.currentData()), '')
        )

    def _scope_changed(self) -> None:
        scope = str(self.scope_combo.currentData())
        self.measurement_combo.setEnabled(scope == 'measurement')
        self.session_combo.setEnabled(scope in ('session',))
        self.input_path_combo.setEnabled(scope in ('instrument', 'session'))

    def build_record(self) -> Any:
        scope = str(self.scope_combo.currentData())
        subject_id = (
            self.measurement_combo.currentData() if scope == 'measurement' else None
        )
        session_id = (
            _combo_text_or_none(self.session_combo)
            if scope in ('session',)
            else None
        )
        input_path = (
            _combo_text_or_none(self.input_path_combo)
            if scope in ('instrument', 'session')
            else None
        )
        if scope == 'measurement' and not subject_id:
            raise ValueError('有効範囲「この測定のみ」には対象の測定が必要です')
        if scope == 'session' and not session_id:
            raise ValueError(
                '有効範囲「取得セッション」には取得セッション識別子が必要です'
            )
        if scope == 'instrument' and not (
            _text_or_none(self.instrument_identity_edit) and input_path
        ):
            raise ValueError(
                '有効範囲「機器・入力パス」には機器識別子と入力パス識別子が必要です'
            )
        sensitivity = _parse_optional_float(self.sensitivity_edit, '感度')
        if sensitivity is not None and sensitivity <= 0:
            raise ValueError('感度は正の数値で入力してください')
        # UI takes millivolts per pascal; the authority stores V/Pa.
        sensitivity_v_per_pa = (
            sensitivity / 1000.0 if sensitivity is not None else None
        )
        return build_acoustic_level_calibration(
            method=str(self.method_combo.currentData()),  # type: ignore[arg-type]
            instrument_identity=_text_or_none(self.instrument_identity_edit),
            instrument_profile=_text_or_none(self.instrument_profile_edit),
            input_device_label=_text_or_none(self.input_device_edit),
            input_channel=_text_or_none(self.input_channel_edit),
            sensitivity_v_per_pa=sensitivity_v_per_pa,
            reference_level_db_spl=_parse_optional_float(
                self.reference_level_edit, '基準レベル'
            ),
            reference_frequency_hz=_parse_optional_float(
                self.reference_frequency_edit, '基準周波数'
            ),
            uncertainty_db=_parse_optional_float(self.uncertainty_edit, '不確かさ'),
            validity_scope=scope,  # type: ignore[arg-type]
            subject_measurement_id=(
                subject_id if isinstance(subject_id, str) else None
            ),
            acquisition_session_id=session_id,
            input_path_identity=input_path,
        )


# --- Stimulus profile (+ excitation asset upload) ------------------------------


def _probe_excitation_metadata(path: Path) -> dict[str, Any]:
    """Verified-only stimulus-file metadata for the sealed record.

    A WAV file's duration/rate/channels are read from the container header;
    anything else only contributes its extension as the declared format —
    unverifiable metadata is never invented.
    """
    metadata: dict[str, Any] = {'format': None}
    suffix = path.suffix.lower()
    if suffix:
        metadata['format'] = suffix.lstrip('.')
    if suffix == '.wav':
        try:
            with wave.open(str(path), 'rb') as wav:
                frames = wav.getnframes()
                rate = wav.getframerate()
                channels = wav.getnchannels()
        except (wave.Error, EOFError, OSError):
            return metadata
        if rate > 0:
            metadata['sample_rate_hz'] = float(rate)
            metadata['duration_s'] = frames / float(rate)
            metadata['channel_count'] = channels
    return metadata


class StimulusProfileDialog(_RecordDialog):
    """Register the stimulus authority a measurement or verification ran.

    The optional excitation-file pick uploads the bytes into the managed
    asset store on selection (same pattern as the mic calibration-file
    retain) so the profile can pin the exact content hash; the dataset
    binding names the persisted measurement dataset the stimulus derives
    from. Unbound profiles stay honest — they record declared conditions
    that can never prove a shared reference.
    """

    def __init__(
        self,
        controller: 'MeasurementWorkflowController',
        views: Sequence['MeasurementView'],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, '刺激プロファイルを登録')
        self.controller = controller
        self.resize(580, 560)
        layout = QVBoxLayout(self)

        note = QLabel(
            '刺激プロファイルは「再生した信号」の権威記録です。'
            '励振ファイルを添付すると内容のSHA-256で証明できます。',
            self,
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        form = QFormLayout()
        self.kind_combo = _combo_with(STIMULUS_KIND_LABELS, self)
        self.kind_combo.currentIndexChanged.connect(self._kind_changed)
        form.addRow('信号種別', self.kind_combo)
        self.intent_combo = _combo_with(_STIMULUS_INTENT_LABELS, self)
        form.addRow('意図', self.intent_combo)

        asset_row = QHBoxLayout()
        self.asset_combo = QComboBox(self)
        self.asset_combo.setMinimumContentsLength(28)
        asset_row.addWidget(self.asset_combo, 1)
        pick_button = QPushButton('ファイルを選択…', self)
        pick_button.setToolTip(
            '励振ファイルを選び、バイト列を証拠として登録します。'
            'ファイル名とSHA-256が自動で反映されます。'
        )
        pick_button.clicked.connect(self._pick_excitation_file)
        asset_row.addWidget(pick_button)
        form.addRow('励振ファイル', asset_row)

        self.dataset_combo = QComboBox(self)
        self.dataset_combo.setMinimumContentsLength(28)
        self.dataset_combo.addItem('（なし）', None)
        for view in views:
            if view.dataset_id is None:
                continue
            try:
                dataset = self.controller.measurement_repository.dataset_for_measurement(
                    view.measurement_id
                )
            except Exception:
                continue
            if dataset is None:
                continue
            self.dataset_combo.addItem(
                f'{view.effective_target_name} · {view.channel_role} · '
                f'{view.imported_at}',
                dataset.dataset_sha256,
            )
        form.addRow('由来データセット', self.dataset_combo)

        self.level_dbfs_edit = QLineEdit(self)
        self.level_dbfs_edit.setPlaceholderText('例: -20.0')
        form.addRow('レベル (dBFS)', self.level_dbfs_edit)
        self.peak_dbfs_edit = QLineEdit(self)
        self.peak_dbfs_edit.setPlaceholderText('例: -3.0')
        form.addRow('ピーク (dBFS)', self.peak_dbfs_edit)
        self.level_spl_edit = QLineEdit(self)
        self.level_spl_edit.setPlaceholderText('例: 75.0')
        form.addRow('音圧レベル (dB SPL)', self.level_spl_edit)
        self.duration_edit = QLineEdit(self)
        self.duration_edit.setPlaceholderText('例: 5.0')
        form.addRow('継続時間 (s)', self.duration_edit)
        self.sample_rate_edit = QLineEdit(self)
        self.sample_rate_edit.setPlaceholderText('例: 48000')
        form.addRow('サンプルレート (Hz)', self.sample_rate_edit)
        self.channel_count_edit = QLineEdit(self)
        self.channel_count_edit.setPlaceholderText('例: 1')
        form.addRow('チャンネル数', self.channel_count_edit)
        self.band_low_edit = QLineEdit(self)
        self.band_low_edit.setPlaceholderText('例: 20')
        form.addRow('帯域下限 (Hz)', self.band_low_edit)
        self.band_high_edit = QLineEdit(self)
        self.band_high_edit.setPlaceholderText('例: 20000')
        form.addRow('帯域上限 (Hz)', self.band_high_edit)
        layout.addLayout(form)

        layout.addWidget(self.error_label)
        layout.addWidget(_dialog_buttons(self))
        self._reload_assets()
        self._kind_changed()

    def _reload_assets(self) -> None:
        selected = self.asset_combo.currentData()
        self.asset_combo.clear()
        self.asset_combo.addItem('（なし）', None)
        for asset in self.controller.quality_repository.list_excitation_assets(
            self.controller.document_id
        ):
            self.asset_combo.addItem(
                f'{asset.filename} · {asset.sha256[:12]}…',
                asset.excitation_asset_id,
            )
        if selected is not None:
            index = self.asset_combo.findData(selected)
            if index >= 0:
                self.asset_combo.setCurrentIndex(index)

    def _kind_changed(self) -> None:
        digital = str(self.kind_combo.currentData()) in _DIGITAL_STIMULUS_KINDS
        self.level_spl_edit.setEnabled(not digital)
        if digital:
            self.level_spl_edit.setToolTip(
                'デジタル信号は音圧レベルを持ちません（SPLはレベル校正権威の領域）'
            )
            self.level_spl_edit.clear()
        else:
            self.level_spl_edit.setToolTip('')

    def _pick_excitation_file(self) -> None:
        """Upload the stimulus file's bytes and register its asset record."""
        path, _selected_filter = file_dialog_memory.get_open_file_name(
            self,
            '励振ファイルを選択',
            'measurement.excitation_asset',
            '音声ファイル (*.wav *.flac *.mp3 *.aif *.aiff);;すべてのファイル (*)',
        )
        if not path:
            return
        try:
            file_path = Path(path)
            raw = read_file_bounded(
                file_path,
                MAX_ATTACHMENT_BYTES,
                label='励振ファイル',
            )
            metadata = _probe_excitation_metadata(file_path)
            asset = build_excitation_asset(
                document_id=self.controller.document_id,
                filename=file_path.name,
                sha256=sha256(raw).hexdigest(),
                byte_length=len(raw),
                format=metadata.get('format'),
                duration_s=metadata.get('duration_s'),
                sample_rate_hz=metadata.get('sample_rate_hz'),
                channel_count=metadata.get('channel_count'),
            )
            self.controller.quality_repository.save_excitation_asset(
                asset, raw
            )
        except Exception as exc:
            self.error_label.setText(f'励振ファイルを登録できません: {operation_error_message(exc)}')
            return
        self.error_label.setText('')
        self._reload_assets()
        index = self.asset_combo.findData(asset.excitation_asset_id)
        if index >= 0:
            self.asset_combo.setCurrentIndex(index)

    def build_record(self) -> Any:
        asset_id = self.asset_combo.currentData()
        asset = None
        if isinstance(asset_id, str) and asset_id:
            asset = self.controller.quality_repository.get_excitation_asset(
                asset_id
            )
            if asset is None:
                raise ValueError('選択した励振ファイルを確認できません')
        band_low = _parse_optional_float(self.band_low_edit, '帯域下限')
        band_high = _parse_optional_float(self.band_high_edit, '帯域上限')
        channel_count = _parse_optional_int(self.channel_count_edit, 'チャンネル数')
        dataset_sha = self.dataset_combo.currentData()
        return build_stimulus_profile(
            document_id=self.controller.document_id,
            stimulus_kind=str(self.kind_combo.currentData()),  # type: ignore[arg-type]
            excitation_asset=asset,
            measurement_dataset_sha256=(
                dataset_sha if isinstance(dataset_sha, str) else None
            ),
            intent=str(self.intent_combo.currentData()),  # type: ignore[arg-type]
            level_dbfs=_parse_optional_float(self.level_dbfs_edit, 'レベル(dBFS)'),
            peak_dbfs=_parse_optional_float(self.peak_dbfs_edit, 'ピーク(dBFS)'),
            level_db_spl=_parse_optional_float(
                self.level_spl_edit, '音圧レベル(dB SPL)'
            ),
            duration_s=_parse_optional_float(self.duration_edit, '継続時間'),
            sample_rate_hz=_parse_optional_float(
                self.sample_rate_edit, 'サンプルレート'
            ),
            channel_count=channel_count,
            band_low_hz=band_low,
            band_high_hz=band_high,
        )


# --- Routing profile ------------------------------------------------------------


class RoutingProfileDialog(_RecordDialog):
    """Register a verified channel-map authority.

    Each row is one output-path → physical-speaker binding: the output
    device label, REW channel label, optional hardware index, the logical
    role HTDT assigns, which speakers were expected vs observed during
    verification, and the verification state the operator asserts. The
    profile pins the current head revision so the speaker identities it
    attests stay provable.
    """

    _COL_DEVICE = 0
    _COL_CHANNEL = 1
    _COL_HW_INDEX = 2
    _COL_ROLE = 3
    _COL_VERIFICATION = 4

    def __init__(
        self,
        controller: 'MeasurementWorkflowController',
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, 'ルーティングプロファイルを登録')
        self.controller = controller
        self._speakers: tuple['SpeakerTarget', ...] = tuple(
            controller.source_speakers()
        )
        # Parallel to table rows: (expected ids, observed ids) per entry.
        self._row_speakers: list[tuple[list[str], list[str]]] = []
        self._syncing_speaker_lists = False
        self.resize(680, 640)
        layout = QVBoxLayout(self)

        note = QLabel(
            'ルーティングプロファイルは出力チャンネルから物理スピーカーへの'
            '検証済み対応付けです。各行は出力経路ごとの役割・期待/観測スピーカー・'
            '検証状態を記録します。',
            self,
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        form = QFormLayout()
        self.profile_name_edit = QLineEdit('routing', self)
        form.addRow('プロファイル名', self.profile_name_edit)
        layout.addLayout(form)

        self.entries_table = QTableWidget(0, 5, self)
        self.entries_table.setHorizontalHeaderLabels(
            ['出力デバイス', 'REWチャンネル', 'HW番号', '論理役割', '検証状態']
        )
        self.entries_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.entries_table.verticalHeader().setVisible(False)
        self.entries_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.entries_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.entries_table.itemSelectionChanged.connect(
            self._entry_selection_changed
        )
        self.entries_table.setMinimumHeight(160)
        layout.addWidget(self.entries_table)

        entry_buttons = QHBoxLayout()
        add_button = QPushButton('行を追加', self)
        add_button.clicked.connect(self._add_entry_row)
        entry_buttons.addWidget(add_button)
        derive_button = QPushButton('スピーカー役割から行を生成', self)
        derive_button.setToolTip(
            'シーンのスピーカーを役割ごとにまとめ、各行の期待/観測スピーカーに'
            '設定します（検証状態は未検証のまま — 実測確認後に変更してください）。'
        )
        derive_button.clicked.connect(self._derive_rows_from_speakers)
        entry_buttons.addWidget(derive_button)
        remove_button = QPushButton('選択行を削除', self)
        remove_button.clicked.connect(self._remove_entry_row)
        entry_buttons.addWidget(remove_button)
        entry_buttons.addStretch(1)
        layout.addLayout(entry_buttons)

        speakers_row = QHBoxLayout()
        for title, attr in (
            ('期待スピーカー（選択中の行）', 'expected_speaker_list'),
            ('観測スピーカー（選択中の行）', 'observed_speaker_list'),
        ):
            column = QVBoxLayout()
            column.addWidget(QLabel(title, self))
            list_widget = QListWidget(self)
            list_widget.setSelectionMode(
                QAbstractItemView.SelectionMode.NoSelection
            )
            for speaker in self._speakers:
                item = QListWidgetItem(
                    f'{speaker.name} · {speaker.role}', list_widget
                )
                item.setData(Qt.ItemDataRole.UserRole, speaker.entity_id)
                item.setFlags(
                    item.flags() | Qt.ItemFlag.ItemIsUserCheckable
                )
                item.setCheckState(Qt.CheckState.Unchecked)
            list_widget.itemChanged.connect(self._speaker_checks_changed)
            setattr(self, attr, list_widget)
            column.addWidget(list_widget)
            speakers_row.addLayout(column)
        layout.addLayout(speakers_row)

        layout.addWidget(self.error_label)
        layout.addWidget(_dialog_buttons(self))
        self._derive_rows_from_speakers()

    # -- row model --------------------------------------------------------------

    def _add_entry_row(self) -> None:
        row = self.entries_table.rowCount()
        self.entries_table.insertRow(row)
        self.entries_table.setItem(
            row, self._COL_DEVICE, QTableWidgetItem('')
        )
        self.entries_table.setItem(
            row, self._COL_CHANNEL, QTableWidgetItem('')
        )
        self.entries_table.setItem(
            row, self._COL_HW_INDEX, QTableWidgetItem('')
        )
        role_combo = _combo_with(_CHANNEL_ROLE_CHOICES, self.entries_table)
        role_combo.setEditable(True)
        self.entries_table.setCellWidget(row, self._COL_ROLE, role_combo)
        verification_combo = _combo_with(
            _ROUTING_VERIFICATION_LABELS, self.entries_table
        )
        # Asserting verified wiring is an explicit operator decision —
        # new rows always start unverified, never pre-claimed.
        verification_combo.setCurrentIndex(
            verification_combo.findData('unverified')
        )
        self.entries_table.setCellWidget(
            row, self._COL_VERIFICATION, verification_combo
        )
        self._row_speakers.append(([], []))

    def _remove_entry_row(self) -> None:
        rows = sorted(
            {item.row() for item in self.entries_table.selectedItems()},
            reverse=True,
        )
        for row in rows:
            self.entries_table.removeRow(row)
            if row < len(self._row_speakers):
                del self._row_speakers[row]

    def _derive_rows_from_speakers(self) -> None:
        """One entry row per speaker role, speakers pre-filled honestly.

        Expected and observed speakers both start as the role's members —
        the operator still reviews each row and marks verification, so a
        derived default never silently claims verified wiring.
        """
        if not self._speakers:
            return
        latest_label = self._latest_output_device_label()
        by_role: dict[str, list['SpeakerTarget']] = {}
        for speaker in self._speakers:
            role = speaker.role if speaker.role != '未設定' else 'unknown'
            by_role.setdefault(role, []).append(speaker)
        for role, members in sorted(by_role.items()):
            self._add_entry_row()
            row = self.entries_table.rowCount() - 1
            device_item = self.entries_table.item(row, self._COL_DEVICE)
            if device_item is not None and latest_label:
                device_item.setText(latest_label)
            channel_item = self.entries_table.item(row, self._COL_CHANNEL)
            if channel_item is not None:
                channel_item.setText(role)
            role_combo = self.entries_table.cellWidget(row, self._COL_ROLE)
            if role_combo is not None:
                role_index = role_combo.findData(role)
                if role_index >= 0:
                    role_combo.setCurrentIndex(role_index)
                else:
                    role_combo.setCurrentText(role)
            ids = [speaker.entity_id for speaker in members]
            self._row_speakers[row] = (list(ids), list(ids))
        if self.entries_table.rowCount():
            self.entries_table.selectRow(0)
            self._entry_selection_changed()

    def _latest_output_device_label(self) -> str | None:
        try:
            contexts = self.controller.quality_repository.list_acquisition_contexts()
        except Exception:
            return None
        for context in reversed(contexts):
            playback = getattr(context, 'playback', None)
            label = getattr(playback, 'output_device_label', None)
            if label:
                return str(label)
        return None

    # -- speaker checklists -------------------------------------------------------

    def _entry_selection_changed(self) -> None:
        row = self.entries_table.currentRow()
        self._syncing_speaker_lists = True
        try:
            expected, observed = (
                self._row_speakers[row] if 0 <= row < len(self._row_speakers)
                else ([], [])
            )
            for list_widget, selected in (
                (self.expected_speaker_list, set(expected)),
                (self.observed_speaker_list, set(observed)),
            ):
                for index in range(list_widget.count()):
                    item = list_widget.item(index)
                    item.setCheckState(
                        Qt.CheckState.Checked
                        if item.data(Qt.ItemDataRole.UserRole) in selected
                        else Qt.CheckState.Unchecked
                    )
                list_widget.setEnabled(row >= 0)
        finally:
            self._syncing_speaker_lists = False

    def _speaker_checks_changed(self) -> None:
        if self._syncing_speaker_lists:
            return
        row = self.entries_table.currentRow()
        if not (0 <= row < len(self._row_speakers)):
            return
        expected = [
            str(
                self.expected_speaker_list.item(index).data(
                    Qt.ItemDataRole.UserRole
                )
            )
            for index in range(self.expected_speaker_list.count())
            if self.expected_speaker_list.item(index).checkState()
            == Qt.CheckState.Checked
        ]
        observed = [
            str(
                self.observed_speaker_list.item(index).data(
                    Qt.ItemDataRole.UserRole
                )
            )
            for index in range(self.observed_speaker_list.count())
            if self.observed_speaker_list.item(index).checkState()
            == Qt.CheckState.Checked
        ]
        self._row_speakers[row] = (expected, observed)

    # -- build ----------------------------------------------------------------------

    def build_record(self) -> Any:
        name = self.profile_name_edit.text().strip()
        if not name:
            raise ValueError('プロファイル名を入力してください')
        if self.entries_table.rowCount() == 0:
            raise ValueError('チャンネルマップの行を1行以上追加してください')
        revision = self.controller.latest_revision()
        entries: list[dict[str, Any]] = []
        for row in range(self.entries_table.rowCount()):
            device_item = self.entries_table.item(row, self._COL_DEVICE)
            channel_item = self.entries_table.item(row, self._COL_CHANNEL)
            hw_item = self.entries_table.item(row, self._COL_HW_INDEX)
            device = (device_item.text() if device_item else '').strip()
            channel = (channel_item.text() if channel_item else '').strip()
            if not device or not channel:
                raise ValueError(
                    f'{row + 1} 行目: 出力デバイスとREWチャンネルは必須です'
                )
            hw_text = (hw_item.text() if hw_item else '').strip()
            hw_index: int | None = None
            if hw_text:
                try:
                    hw_index = int(hw_text)
                except ValueError as exc:
                    raise ValueError(
                        f'{row + 1} 行目: HW番号は整数で入力してください'
                    ) from exc
                if hw_index < 0:
                    raise ValueError(
                        f'{row + 1} 行目: HW番号は0以上で入力してください'
                    )
            role_widget = self.entries_table.cellWidget(row, self._COL_ROLE)
            verification_widget = self.entries_table.cellWidget(
                row, self._COL_VERIFICATION
            )
            role = (
                role_widget.currentText().strip()
                if role_widget is not None
                else ''
            ) or 'unknown'
            verification = (
                str(verification_widget.currentData())
                if verification_widget is not None
                else 'unverified'
            )
            expected: list[str] = []
            observed: list[str] = []
            if row < len(self._row_speakers):
                expected, observed = self._row_speakers[row]
            entries.append(
                {
                    'output_device_label': device,
                    'rew_channel_label': channel,
                    'hardware_channel_index': hw_index,
                    'logical_role': role,
                    'expected_speaker_ids': tuple(expected),
                    'observed_speaker_ids': tuple(observed),
                    'verification': verification,
                }
            )
        return build_routing_profile(
            entries=entries,
            profile_name=name,
            document_id=self.controller.document_id,
            scene_revision_id=revision.revision_id,
        )


# --- Dataset level reference ------------------------------------------------------


class DatasetLevelReferenceDialog(_RecordDialog):
    """Pin the level-semantics authority for the selected measurement's dataset.

    The record declares what the persisted ``level_db`` values mean;
    ``absolute_spl`` additionally binds a persisted acoustic level
    calibration — the repository refuses it unless that calibration both
    supports absolute SPL and its declared scope covers this exact
    measurement. Other kinds are honest non-absolute declarations that
    never carry a calibration pin.
    """

    def __init__(
        self,
        view: 'MeasurementView',
        dataset: 'CadFrequencyResponseDataset',
        calibrations: Sequence[Any],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, 'レベル基準を登録')
        self.dataset = dataset
        self.resize(520, 320)
        layout = QVBoxLayout(self)

        note = QLabel(
            f'{view.effective_target_name} · {view.channel_role} のデータセットに'
            'レベルの意味を記録します。絶対SPLには適用可能なレベル校正権威が必要です。',
            self,
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        form = QFormLayout()
        self.kind_combo = _combo_with(LEVEL_REFERENCE_KIND_LABELS, self)
        self.kind_combo.currentIndexChanged.connect(self._kind_changed)
        form.addRow('レベルの意味', self.kind_combo)

        self.calibration_combo = QComboBox(self)
        self.calibration_combo.setMinimumContentsLength(30)
        self._calibrations = tuple(calibrations)
        for calibration in self._calibrations:
            gaps = absolute_spl_evidence_gaps(calibration)
            suffix = '' if not gaps else f'（証拠不足: {", ".join(gaps)}）'
            self.calibration_combo.addItem(
                f'{level_method_label(calibration.method)} · '
                f'{calibration.instrument_identity or "機器未特定"} · '
                f'{calibration.calibrated_at_utc}{suffix}',
                calibration.calibration_id,
            )
        form.addRow('紐付けるレベル校正', self.calibration_combo)
        layout.addLayout(form)

        layout.addWidget(self.error_label)
        layout.addWidget(_dialog_buttons(self))
        self._kind_changed()

    def _kind_changed(self) -> None:
        absolute = str(self.kind_combo.currentData()) == 'absolute_spl'
        self.calibration_combo.setEnabled(absolute)

    def build_record(self) -> Any:
        kind = str(self.kind_combo.currentData())
        calibration = None
        if kind == 'absolute_spl':
            if self.calibration_combo.count() == 0:
                raise ValueError(
                    '絶対SPLには登録済みのレベル校正が必要です — '
                    '「機器の準備（校正）」の計測の権威データから先に登録してください'
                )
            index = self.calibration_combo.currentIndex()
            if not (0 <= index < len(self._calibrations)):
                raise ValueError('紐付けるレベル校正を選択してください')
            calibration = self._calibrations[index]
        return build_dataset_level_reference(
            measurement_id=self.dataset.measurement_id,
            dataset_id=self.dataset.dataset_id,
            dataset_sha256=self.dataset.dataset_sha256,
            level_reference_kind=kind,  # type: ignore[arg-type]
            calibration_id=(
                calibration.calibration_id if calibration is not None else None
            ),
            calibration_sha256=(
                calibration.calibration_sha256
                if calibration is not None
                else None
            ),
        )


__all__ = [
    'DatasetLevelReferenceDialog',
    'LevelCalibrationDialog',
    'RoutingProfileDialog',
    'StimulusProfileDialog',
    'TimingReferenceDialog',
    'level_method_label',
    'level_reference_kind_label',
    'stimulus_kind_label',
    'timing_method_label',
]
