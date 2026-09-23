from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import pyqtgraph as pg
from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .cad_measurement_models import CadMeasurementComparison
from .cad_measurement_quality import CadMeasurementCapability
from .cad_repository import SceneRepository
from .ingress import read_file_bounded
from .limits import MAX_NATIVE_REW_TEXT_FILE_BYTES
from .measurement_workflow import (
    MeasurementAssignment,
    MeasurementView,
    MeasurementWorkflowController,
    PendingMeasurementImport,
    RewReadSource,
)
from .native_worker import WORKER_CANCELLED, NativeWorkerPool
from .ui_theme import (
    DARK_THEME,
    SemanticState,
    SurfaceRole,
    TypographyRole,
    set_primary_action,
    set_semantic_state,
    set_surface_role,
    set_typography_role,
)
from .workflow_shell import WorkspaceFactory, WorkspaceMount


_CONTEXT_IDS = ("import", "assignment", "quality", "comparison")
_USER_ROLE = int(Qt.ItemDataRole.UserRole)


def _evidence_label(value: str) -> str:
    return {
        "measured": "実測",
        "derived": "派生",
        "predicted": "予測",
        "unknown": "未確認",
    }.get(value, value)


def _source_label(value: str) -> str:
    return {
        "rew_api": "REW API",
        "rew_text": "REWテキスト",
        "unknown": "未確認",
    }.get(value, value)


def _channel_role_label(value: str) -> str:
    return {
        "front_left": "フロント左 (FL)",
        "center": "センター (C)",
        "front_right": "フロント右 (FR)",
        "subwoofer": "サブウーファー",
        "unknown": "未指定",
    }.get(value, value)


def _phase_label(value: str | None) -> str:
    """Raw dataset phase evidence state — says nothing about common timing."""
    return {
        "valid": "位相データ有効",
        "absent": "位相データなし",
        "unknown": "位相データ未確認",
        None: "データなし",
    }.get(value, str(value))


def _capability_decision_label(capability: CadMeasurementCapability | None) -> str:
    """User-facing label for one canonical capability claim decision."""
    if capability is None:
        return "データなし"
    return {
        "ALLOWED": "利用可能",
        "BLOCKED": "利用不可",
        "UNKNOWN": "未確認",
    }.get(capability.decision, str(capability.decision))


def _capability_decision(capability: CadMeasurementCapability | None) -> str:
    return "UNKNOWN" if capability is None else capability.decision


def _check_label(check: str) -> str:
    """User-facing label for one independent quality check."""
    return {
        "clipping": "クリッピング",
        "noise_snr": "ノイズ/SNR",
        "usable_frequency_band": "使用可能帯域",
        "timing_reference": "タイミング基準",
        "polarity": "極性",
        "ir_window": "インパルス応答窓",
        "calibration": "キャリブレーション",
        "repeatability": "繰り返し精度",
    }.get(check, check)


def _check_status_label(status: str) -> str:
    """PASS/FAIL/UNKNOWN/NOT_EVALUATED — missing evidence never shows as PASS."""
    return {
        "PASS": "合格",
        "FAIL": "不合格",
        "UNKNOWN": "未確認",
        "NOT_EVALUATED": "未評価",
    }.get(status, status)


def _claim_label(claim: str) -> str:
    """User-facing label for one downstream capability claim."""
    return {
        "magnitude_response": "振幅応答",
        "phase_response": "位相応答",
        "common_timing": "共通タイミング",
        "arrival_time": "到達時刻",
        "decay": "減衰特性",
        "calibrated_response": "校正済み応答",
        "repeatability": "繰り返し精度",
        "polarity": "極性",
    }.get(claim, claim)


def _report_state_label(state: str) -> str:
    return {
        "current": "最新",
        "stale": "旧データセットのレポート（再評価が必要です）",
        "missing": "レポートなし",
    }.get(state, state)


def _retake_recommendation_label(value: str | None) -> str:
    return {
        "RETAKE": "再測定を推奨",
        "NOT_NEEDED": "再測定不要",
        "UNKNOWN": "未確認（証拠不足）",
        None: "—",
    }.get(value, str(value))


def _missing_evidence_label(code: str) -> str:
    """Stable missing-evidence codes from MeasurementRetakeGuidance."""
    return {
        "clipping_metadata": "クリッピング有無の取得メタデータ",
        "snr_evidence": "SNR証拠",
        "usable_band_evidence": "使用可能帯域の証拠",
        "timing_reference_evidence": "タイミング基準（基準ID・クロック・サンプルレート・遅延補正）",
        "polarity_evidence": "極性証拠",
        "impulse_response": "インパルス応答",
        "ir_window_evidence": "IR窓・切り詰め証拠",
        "calibration_provenance": "マイク校正ファイルの来歴",
        "repeat_measurements": "同一条件の繰り返し測定",
        "acquisition_context": "取得コンテキストの紐付け",
    }.get(code, code)


def _remeasure_label(code: str) -> str:
    """Stable re-measure codes from MeasurementRetakeGuidance."""
    return {
        "same_binding": "同じ測定点・入力役割・音源で再測定",
        "timed_acquisition": "タイミング基準を記録できる取得コンテキストで再測定",
        "calibrated_microphone": "校正ファイルを適用したマイクで再測定",
        "repeat_measurement": "同一条件の測定を追加して繰り返し精度を評価",
        "impulse_response_capture": "インパルス応答を含む形式で再取得",
        "acquisition_metadata": "クリッピング/SNR/帯域/極性の取得メタデータを記録して再測定",
    }.get(code, code)


def _quality_label(value: str) -> str:
    return {
        "unknown": "未確認",
        "synthetic_fixture": "Synthetic fixture",
    }.get(value, value)


def _format_band(band: tuple[float, float] | None) -> str:
    if band is None:
        return "—"
    return f"{band[0]:.1f}–{band[1]:.1f} Hz"


def _set_plot_appearance(plot: pg.PlotWidget) -> None:
    tokens = DARK_THEME
    plot.setBackground(tokens.surfaces.canvas.hex)
    plot.showGrid(x=True, y=True, alpha=0.18)
    plot.setLogMode(x=True, y=False)
    item = plot.getPlotItem()
    item.setContentsMargins(10, 8, 10, 10)
    item.setDownsampling(auto=True, mode="peak")
    item.setClipToView(True)
    item.getViewBox().setDefaultPadding(0.03)
    for axis_name in ("bottom", "left"):
        axis = plot.getAxis(axis_name)
        axis.setPen(tokens.surfaces.border_strong.hex)
        axis.setTextPen(tokens.text.secondary.hex)
        axis.setStyle(tickTextOffset=8, autoExpandTextSpace=True)


def _card(title: str, parent: QWidget | None = None) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame(parent)
    set_surface_role(frame, SurfaceRole.RAISED)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(10)
    heading = QLabel(title, frame)
    set_typography_role(heading, TypographyRole.SECTION_TITLE)
    layout.addWidget(heading)
    return frame, layout


def _page(title: str, subtitle: str) -> tuple[QScrollArea, QWidget, QVBoxLayout]:
    scroll = QScrollArea()
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setWidgetResizable(True)
    set_surface_role(scroll, SurfaceRole.BASE)

    host = QWidget()
    set_surface_role(host, SurfaceRole.BASE)
    layout = QVBoxLayout(host)
    layout.setContentsMargins(24, 22, 24, 28)
    layout.setSpacing(16)

    heading = QLabel(title, host)
    set_typography_role(heading, TypographyRole.WORKSPACE_TITLE)
    layout.addWidget(heading)
    lead = QLabel(subtitle, host)
    lead.setWordWrap(True)
    set_typography_role(lead, TypographyRole.SECONDARY)
    layout.addWidget(lead)
    scroll.setWidget(host)
    return scroll, host, layout


class MeasurementPageWorkspace(QWidget):
    """UX130 document-like measurement workspace mounted directly by the shell."""

    def __init__(
        self,
        controller: MeasurementWorkflowController,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.current_context_id = "import"
        self._job_pool = NativeWorkerPool(self)
        self._job_handlers: dict[
            str, tuple[Callable[[object], None], str]
        ] = {}
        self._disposed = False
        self._rew_rows: list[dict[str, Any]] = []
        self._quality_views: tuple[MeasurementView, ...] = ()
        self._last_comparison: CadMeasurementComparison | None = None
        self._retake_source_id: str | None = None

        self.setObjectName("measurementPageWorkspace")
        set_surface_role(self, SurfaceRole.BASE)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.notice = QLabel(self)
        self.notice.setObjectName("measurementWorkspaceNotice")
        self.notice.setWordWrap(True)
        self.notice.setVisible(False)
        self.notice.setContentsMargins(24, 8, 24, 8)
        root.addWidget(self.notice)

        self.pages = QStackedWidget(self)
        self.pages.setObjectName("measurementPageStack")
        root.addWidget(self.pages, 1)

        self._build_import_page()
        self._build_assignment_page()
        self._build_quality_page()
        self._build_comparison_page()
        self.refresh()

    # ------------------------------------------------------------------
    # Shell interface

    def set_context(self, context_id: str) -> None:
        if context_id not in _CONTEXT_IDS:
            raise ValueError(f"unknown measurement context: {context_id}")
        self.current_context_id = context_id
        self.pages.setCurrentIndex(_CONTEXT_IDS.index(context_id))
        self.refresh()

    def focus_entity(self, entity_id: str) -> None:
        for row_index, row in enumerate(self._quality_views):
            if row.measurement_id == entity_id or row.target_entity_id == entity_id:
                self.quality_table.selectRow(row_index)
                self._show_quality_row(row_index)
                return

    def refresh(self) -> None:
        self._refresh_pending()
        self._refresh_assignment_options()
        self._refresh_quality()
        self._refresh_comparison_choices()

    def import_rew_text_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "REWテキストを選択",
            "",
            "REW text (*.txt *.frd);;All files (*)",
        )
        if not path:
            return
        try:
            file_path = Path(path)
            raw = read_file_bounded(
                file_path,
                MAX_NATIVE_REW_TEXT_FILE_BYTES,
                label="REW text file",
            )
            self.controller.stage_rew_text(raw, file_path.name)
        except Exception as exc:
            self._set_notice(f"読み込みに失敗しました · {exc}", SemanticState.ERROR)
            return
        self._set_notice(
            "読み込みました。次に「割り当て」で測定点と入力役割を確認してください。",
            SemanticState.SUCCESS,
        )
        self.refresh()

    # ------------------------------------------------------------------
    # Import page

    def _build_import_page(self) -> None:
        page, host, layout = _page(
            "測定を読み込む",
            "REWの周波数応答を一時領域へ読み込みます。この段階では測定証拠として保存されません。",
        )
        page.setObjectName("measurementImportPage")

        source_card, source_layout = _card("読み込み元", host)
        button_row = QHBoxLayout()
        self.text_import_button = QPushButton("REWテキストを選ぶ", source_card)
        set_primary_action(self.text_import_button)
        self.text_import_button.clicked.connect(self.import_rew_text_dialog)
        button_row.addWidget(self.text_import_button)

        self.rew_refresh_button = QPushButton("REW一覧を更新", source_card)
        self.rew_refresh_button.clicked.connect(self._refresh_rew_async)
        button_row.addWidget(self.rew_refresh_button)
        button_row.addStretch(1)
        source_layout.addLayout(button_row)

        rew_row = QHBoxLayout()
        self.rew_combo = QComboBox(source_card)
        self.rew_combo.setMinimumContentsLength(32)
        rew_row.addWidget(self.rew_combo, 1)
        self.rew_read_button = QPushButton("選択したREWを読み込む", source_card)
        self.rew_read_button.clicked.connect(self._read_rew_async)
        rew_row.addWidget(self.rew_read_button)
        source_layout.addLayout(rew_row)
        layout.addWidget(source_card)

        pending_card, pending_layout = _card("読み込み内容", host)
        self.pending_import_label = QLabel("まだ読み込まれていません", pending_card)
        self.pending_import_label.setWordWrap(True)
        pending_layout.addWidget(self.pending_import_label)

        self.import_preview_plot = pg.PlotWidget(pending_card)
        self.import_preview_plot.setMinimumHeight(240)
        self.import_preview_plot.setLabel("bottom", "周波数", units="Hz")
        self.import_preview_plot.setLabel("left", "レベル", units="dB")
        _set_plot_appearance(self.import_preview_plot)
        pending_layout.addWidget(self.import_preview_plot)
        layout.addWidget(pending_card)
        layout.addStretch(1)
        self.pages.addWidget(page)

    def _refresh_rew_async(self) -> None:
        self._set_notice("REW測定一覧を読み込み中です。", None)
        self._start_job(
            self.controller.list_rew_measurements,
            self._apply_rew_list,
            "REW一覧の読み込みに失敗しました",
        )

    def _apply_rew_list(self, value: object) -> None:
        rows = value if isinstance(value, list) else []
        self._rew_rows = [item for item in rows if isinstance(item, dict)]
        previous = self.rew_combo.currentData()
        self.rew_combo.clear()
        for row in self._rew_rows:
            uuid = row.get("uuid")
            if not isinstance(uuid, str) or not uuid:
                continue
            title = row.get("title")
            date = row.get("date")
            label = str(title).strip() if isinstance(title, str) and title.strip() else "名称なし"
            if isinstance(date, str) and date.strip():
                label = f"{label} · {date.strip()}"
            self.rew_combo.addItem(label, uuid)
        if previous is not None:
            index = self.rew_combo.findData(previous)
            if index >= 0:
                self.rew_combo.setCurrentIndex(index)
        self._set_notice(
            f"REWから {self.rew_combo.count()} 件を確認しました。",
            SemanticState.SUCCESS,
        )

    def _read_rew_async(self) -> None:
        measurement_uuid = self.rew_combo.currentData()
        if not isinstance(measurement_uuid, str) or not measurement_uuid:
            self._set_notice("先にREW一覧を更新して測定を選択してください。", SemanticState.WARNING)
            return
        self._set_notice("選択したREW測定を読み込み中です。", None)
        self._start_job(
            lambda: self.controller.fetch_rew_snapshot(measurement_uuid),
            self._stage_rew_snapshot,
            "REW測定の読み込みに失敗しました",
        )

    def _stage_rew_snapshot(self, value: object) -> None:
        try:
            self.controller.stage_rew_snapshot(value)  # type: ignore[arg-type]
        except Exception as exc:
            self._set_notice(f"REW測定の確認に失敗しました · {exc}", SemanticState.ERROR)
            return
        self._set_notice(
            "読み込みました。次に「割り当て」で測定点と入力役割を確認してください。",
            SemanticState.SUCCESS,
        )
        self.refresh()

    def _refresh_pending(self) -> None:
        pending = self.controller.pending_import
        self.import_preview_plot.clear()
        if pending is None:
            self.pending_import_label.setText("まだ読み込まれていません")
            return
        phase = "位相サンプルあり" if pending.has_phase_samples else "位相サンプルなし"
        self.pending_import_label.setText(
            f"{pending.source_label}\n"
            f"{pending.sample_count:,} 点 · {_format_band(pending.frequency_band_hz)} · {phase}\n"
            "保存前の一時データです。意味付けは「割り当て」で確定します。"
        )
        self.import_preview_plot.plot(
            pending.frequency_hz,
            pending.level_db,
            pen=pg.mkPen(DARK_THEME.scientific.primary_trace.hex, width=2),
            name="読み込み",
        )
        self.import_preview_plot.enableAutoRange()

    # ------------------------------------------------------------------
    # Assignment page

    def _build_assignment_page(self) -> None:
        page, host, layout = _page(
            "測定を割り当てる",
            "読み込んだ応答を、保存済みの部屋状態にある測定点・入力役割・音源へ割り当ててから保存します。",
        )
        page.setObjectName("measurementAssignmentPage")

        pending_card, pending_layout = _card("保存待ち", host)
        self.assignment_pending_label = QLabel("読み込み待ち", pending_card)
        self.assignment_pending_label.setWordWrap(True)
        pending_layout.addWidget(self.assignment_pending_label)
        layout.addWidget(pending_card)

        assign_card, assign_layout = _card("割り当て", host)
        form = QFormLayout()

        self.target_combo = QComboBox(assign_card)
        form.addRow("測定位置", self.target_combo)

        self.evidence_combo = QComboBox(assign_card)
        for label, value in (
            ("実測", "measured"),
            ("予測", "predicted"),
            ("派生", "derived"),
            ("未確認", "unknown"),
        ):
            self.evidence_combo.addItem(label, value)
        form.addRow("証拠種別", self.evidence_combo)

        self.channel_combo = QComboBox(assign_card)
        self.channel_combo.setEditable(True)
        self.channel_combo.addItems(
            ["unknown", "front_left", "center", "front_right", "subwoofer"]
        )
        form.addRow("入力役割", self.channel_combo)

        self.radiation_combo = QComboBox(assign_card)
        for label, value in (
            ("未確認", "unknown"),
            ("単一音源", "single"),
            ("Bass management", "bass_managed"),
            ("混在", "mixed"),
        ):
            self.radiation_combo.addItem(label, value)
        form.addRow("放射範囲", self.radiation_combo)

        self.routing_combo = QComboBox(assign_card)
        for label, value in (
            ("未確認", "unknown"),
            ("検証済み", "verified"),
            ("手動指定", "manual"),
            ("推定", "inferred"),
        ):
            self.routing_combo.addItem(label, value)
        form.addRow("ルーティング根拠", self.routing_combo)
        assign_layout.addLayout(form)

        speaker_label = QLabel("音源スピーカー", assign_card)
        set_typography_role(speaker_label, TypographyRole.SECONDARY)
        assign_layout.addWidget(speaker_label)
        self.source_speaker_list = QListWidget(assign_card)
        self.source_speaker_list.setMaximumHeight(150)
        assign_layout.addWidget(self.source_speaker_list)

        save_row = QHBoxLayout()
        save_row.addStretch(1)
        self.assignment_save_button = QPushButton("割り当てて保存", assign_card)
        set_primary_action(self.assignment_save_button)
        self.assignment_save_button.clicked.connect(self._commit_assignment)
        save_row.addWidget(self.assignment_save_button)
        assign_layout.addLayout(save_row)
        layout.addWidget(assign_card)
        layout.addStretch(1)
        self.pages.addWidget(page)

    def _refresh_assignment_options(self) -> None:
        pending = self.controller.pending_import
        self.assignment_save_button.setEnabled(pending is not None)
        if pending is None:
            self.assignment_pending_label.setText(
                "「読み込み」でREWデータを選ぶと、ここで測定位置と意味付けを確定できます。"
            )
        else:
            retake_note = (
                "\nこの測定は再測定として記録されます（元の測定とレポートは保持されます）。"
                if self._retake_source_id
                else ""
            )
            self.assignment_pending_label.setText(
                f"{pending.source_label} · {pending.sample_count:,} 点 · "
                f"{_format_band(pending.frequency_band_hz)}{retake_note}"
            )

        previous_target = self.target_combo.currentData()
        previous_sources = {
            self.source_speaker_list.item(index).data(Qt.ItemDataRole.UserRole)
            for index in range(self.source_speaker_list.count())
            if self.source_speaker_list.item(index).checkState() == Qt.CheckState.Checked
        }
        self.target_combo.clear()
        self.source_speaker_list.clear()
        try:
            targets = self.controller.assignment_targets()
            speakers = self.controller.source_speakers()
        except Exception:
            targets = ()
            speakers = ()

        for target in targets:
            self.target_combo.addItem(target.name, target.entity_id)
        if previous_target is not None:
            index = self.target_combo.findData(previous_target)
            if index >= 0:
                self.target_combo.setCurrentIndex(index)

        for speaker in speakers:
            item = QListWidgetItem(
                f"{speaker.name} · {speaker.role}",
                self.source_speaker_list,
            )
            item.setData(Qt.ItemDataRole.UserRole, speaker.entity_id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if speaker.entity_id in previous_sources
                else Qt.CheckState.Unchecked
            )

        # A pending retake pre-fills the exact binding it supersedes so the
        # lineage record stays valid; the user can still adjust before saving.
        retake_view = next(
            (
                view
                for view in self._quality_views
                if view.measurement_id == self._retake_source_id
            ),
            None,
        )
        if retake_view is not None:
            index = self.target_combo.findData(retake_view.target_entity_id)
            if index >= 0:
                self.target_combo.setCurrentIndex(index)
            self.channel_combo.setCurrentText(retake_view.channel_role)
            index = self.radiation_combo.findData(retake_view.radiation_scope)
            if index >= 0:
                self.radiation_combo.setCurrentIndex(index)
            index = self.routing_combo.findData(retake_view.routing_evidence)
            if index >= 0:
                self.routing_combo.setCurrentIndex(index)
            wanted_sources = set(retake_view.source_speaker_ids)
            for index in range(self.source_speaker_list.count()):
                item = self.source_speaker_list.item(index)
                item.setCheckState(
                    Qt.CheckState.Checked
                    if item.data(Qt.ItemDataRole.UserRole) in wanted_sources
                    else Qt.CheckState.Unchecked
                )

    def _commit_assignment(self) -> None:
        target_id = self.target_combo.currentData()
        if not isinstance(target_id, str) or not target_id:
            self._set_notice(
                "測定位置として使える音響基準点がありません。先に部屋で測定点を作成してください。",
                SemanticState.WARNING,
            )
            return
        source_ids = tuple(
            str(self.source_speaker_list.item(index).data(Qt.ItemDataRole.UserRole))
            for index in range(self.source_speaker_list.count())
            if self.source_speaker_list.item(index).checkState() == Qt.CheckState.Checked
        )
        channel_role = self.channel_combo.currentText().strip() or "unknown"
        assignment = MeasurementAssignment(
            measurement_entity_id=target_id,
            evidence_type=str(self.evidence_combo.currentData()),  # type: ignore[arg-type]
            channel_role=channel_role,
            source_speaker_ids=source_ids,
            radiation_scope=str(self.radiation_combo.currentData()),  # type: ignore[arg-type]
            routing_evidence=str(self.routing_combo.currentData()),  # type: ignore[arg-type]
        )
        try:
            record = self.controller.commit_pending(assignment)
        except Exception as exc:
            self._set_notice(f"測定を保存できませんでした · {exc}", SemanticState.ERROR)
            return
        retake_source_id = self._retake_source_id
        self._retake_source_id = None
        if retake_source_id is not None:
            # Append-only lineage: the superseded measurement, its quality
            # reports and any calibration/holdout assignment stay untouched.
            try:
                self.controller.record_retake(
                    measurement_id=record.measurement_id,
                    supersedes_measurement_id=retake_source_id,
                    reason="user-initiated retake from the measurement quality page",
                )
            except Exception as exc:
                self._set_notice(
                    "測定は保存されましたが、再測定の系譜を記録できませんでした"
                    f"（保存時と同じ測定点・役割・音源が必要です） · {exc}",
                    SemanticState.WARNING,
                )
                self.refresh()
                return
            self._set_notice(
                "再測定を保存し、置き換えの系譜を記録しました。",
                SemanticState.SUCCESS,
            )
            self.refresh()
            return
        self._set_notice(
            f"{_evidence_label(record.evidence_type)}測定を保存しました。「品質」で内容を確認できます。",
            SemanticState.SUCCESS,
        )
        self.refresh()

    # ------------------------------------------------------------------
    # Quality page

    def _build_quality_page(self) -> None:
        page, host, layout = _page(
            "品質と利用可能な比較",
            "保存済みの品質・位相状態をそのまま表示します。未確認の情報をUI側で推測して補完しません。",
        )
        page.setObjectName("measurementQualityPage")

        capability_row = QHBoxLayout()
        magnitude_card, magnitude_layout = _card("振幅比較", host)
        self.magnitude_capability = QLabel("測定データなし", magnitude_card)
        self.magnitude_capability.setWordWrap(True)
        magnitude_layout.addWidget(self.magnitude_capability)
        capability_row.addWidget(magnitude_card, 1)

        phase_card, phase_layout = _card("位相応答", host)
        self.phase_capability = QLabel("測定データなし", phase_card)
        self.phase_capability.setWordWrap(True)
        phase_layout.addWidget(self.phase_capability)
        capability_row.addWidget(phase_card, 1)

        timing_card, timing_layout = _card("共通タイミング", host)
        self.timing_capability = QLabel("測定データなし", timing_card)
        self.timing_capability.setWordWrap(True)
        timing_layout.addWidget(self.timing_capability)
        capability_row.addWidget(timing_card, 1)
        layout.addLayout(capability_row)

        table_card, table_layout = _card("保存済み測定", host)
        self.quality_table = QTableWidget(0, 9, table_card)
        self.quality_table.setHorizontalHeaderLabels(
            ["入力", "証拠", "測定位置", "品質", "位相", "共通タイミング", "配置", "帯域", "再測定"]
        )
        self.quality_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.quality_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.quality_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.quality_table.verticalHeader().setVisible(False)
        self.quality_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.quality_table.horizontalHeader().setStretchLastSection(True)
        self.quality_table.itemSelectionChanged.connect(self._quality_selection_changed)
        self.quality_table.setMinimumHeight(220)
        table_layout.addWidget(self.quality_table)
        layout.addWidget(table_card)

        detail_card, detail_layout = _card("選択した測定", host)
        self.quality_detail = QLabel("測定を選択してください", detail_card)
        self.quality_detail.setWordWrap(True)
        detail_layout.addWidget(self.quality_detail)
        self.quality_plot = pg.PlotWidget(detail_card)
        self.quality_plot.setMinimumHeight(260)
        self.quality_plot.setLabel("bottom", "周波数", units="Hz")
        self.quality_plot.setLabel("left", "レベル", units="dB")
        _set_plot_appearance(self.quality_plot)
        detail_layout.addWidget(self.quality_plot)
        layout.addWidget(detail_card)

        report_card, report_layout = _card("品質レポート", host)
        self.quality_report_label = QLabel("測定を選択してください", report_card)
        self.quality_report_label.setWordWrap(True)
        report_layout.addWidget(self.quality_report_label)
        self.quality_checks_label = QLabel("", report_card)
        self.quality_checks_label.setWordWrap(True)
        report_layout.addWidget(self.quality_checks_label)
        self.quality_capabilities_label = QLabel("", report_card)
        self.quality_capabilities_label.setWordWrap(True)
        report_layout.addWidget(self.quality_capabilities_label)
        layout.addWidget(report_card)

        retake_card, retake_layout = _card("再測定ガイダンス", host)
        self.retake_label = QLabel("測定を選択してください", retake_card)
        self.retake_label.setWordWrap(True)
        retake_layout.addWidget(self.retake_label)
        retake_row = QHBoxLayout()
        retake_row.addStretch(1)
        self.retake_button = QPushButton("再測定（REWから再取得）", retake_card)
        self.retake_button.setEnabled(False)
        self.retake_button.clicked.connect(self._start_retake)
        retake_row.addWidget(self.retake_button)
        retake_layout.addLayout(retake_row)
        layout.addWidget(retake_card)
        layout.addStretch(1)
        self.pages.addWidget(page)

    def _refresh_quality(self) -> None:
        selected_id = None
        selected_items = self.quality_table.selectedItems()
        if selected_items:
            selected_id = selected_items[0].data(Qt.ItemDataRole.UserRole)

        views = self.controller.measurement_views()
        self._quality_views = views
        self.quality_table.setRowCount(len(views))
        for row_index, row in enumerate(views):
            values = (
                _channel_role_label(row.channel_role),
                _evidence_label(row.evidence_type),
                row.target_name,
                _quality_label(row.quality_status),
                _capability_decision_label(row.phase_response_capability),
                _capability_decision_label(row.common_timing_capability),
                "現在の配置" if row.scene_matches_current else "測定時の配置",
                _format_band(row.frequency_band_hz),
                _retake_recommendation_label(row.retake_recommendation),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, row.measurement_id)
                self.quality_table.setItem(row_index, column, item)
            if selected_id == row.measurement_id:
                self.quality_table.selectRow(row_index)

        dataset_count = sum(1 for row in views if row.dataset_id is not None)
        phase_count = sum(
            1
            for row in views
            if _capability_decision(row.phase_response_capability) == "ALLOWED"
        )
        timing_count = sum(
            1
            for row in views
            if _capability_decision(row.common_timing_capability) == "ALLOWED"
        )
        timing_blocked = sum(
            1
            for row in views
            if _capability_decision(row.common_timing_capability) == "BLOCKED"
        )
        self.magnitude_capability.setText(
            "周波数応答の比較に使用できます"
            if dataset_count
            else "比較できる周波数応答がありません"
        )
        set_semantic_state(
            self.magnitude_capability,
            SemanticState.SUCCESS if dataset_count else SemanticState.UNSUPPORTED,
        )
        self.phase_capability.setText(
            f"{phase_count} 件で位相応答を利用できます"
            if phase_count
            else "有効と確認された位相データがありません"
        )
        set_semantic_state(
            self.phase_capability,
            SemanticState.SUCCESS if phase_count else SemanticState.UNSUPPORTED,
        )
        if timing_count:
            timing_text = f"{timing_count} 件で共通タイミング基準が確立しています"
            timing_state = SemanticState.SUCCESS
        elif timing_blocked:
            timing_text = "共通タイミング基準が無効と報告された測定があります"
            timing_state = SemanticState.UNSUPPORTED
        else:
            timing_text = (
                "共通タイミング基準は未確認です。"
                "位相データの有効性だけでは測定間の共通時間基準は成立しません"
            )
            timing_state = SemanticState.UNSUPPORTED
        self.timing_capability.setText(timing_text)
        set_semantic_state(self.timing_capability, timing_state)

        if not views:
            self.quality_detail.setText("保存済み測定はありません")
            self.quality_plot.clear()
            self.quality_report_label.setText("保存済み測定はありません")
            self.quality_checks_label.setText("")
            self.quality_capabilities_label.setText("")
            self.retake_label.setText("保存済み測定はありません")
            self.retake_button.setEnabled(False)
        elif not self.quality_table.selectedItems():
            self.quality_table.selectRow(0)
            self._show_quality_row(0)

    def _quality_selection_changed(self) -> None:
        row_index = self.quality_table.currentRow()
        if row_index >= 0:
            self._show_quality_row(row_index)

    def _show_quality_row(self, row_index: int) -> None:
        if not (0 <= row_index < len(self._quality_views)):
            return
        row = self._quality_views[row_index]
        reasons = "、".join(row.quality_reasons) if row.quality_reasons else "理由情報なし"
        captured = row.captured_at or "取得時刻未記録"
        scene = "現在の配置と一致" if row.scene_matches_current else "測定時の配置を保持"
        source_speakers = (
            " / ".join(row.source_speaker_ids) if row.source_speaker_ids else "未指定"
        )
        self.quality_detail.setText(
            f"{row.target_name} · {_evidence_label(row.evidence_type)} · "
            f"{_channel_role_label(row.channel_role)}\n"
            f"品質: {_quality_label(row.quality_status)} · {reasons}\n"
            f"{_phase_label(row.phase_status)} · "
            f"位相応答: {_capability_decision_label(row.phase_response_capability)} · "
            f"共通タイミング: {_capability_decision_label(row.common_timing_capability)} · "
            f"{scene}\n"
            f"音源: {source_speakers} · {captured}"
        )

        # Replay-validated quality report summary (#468): profile name/version
        # and timestamp are shown, raw internal hashes stay out of the view.
        profile = row.quality_profile_version or "—"
        created = row.quality_report_created_at or "—"
        self.quality_report_label.setText(
            f"状態: {_report_state_label(row.quality_report_state)} · "
            f"プロファイル {profile} · 作成 {created}"
        )
        if row.quality_checks:
            check_lines = "\n".join(
                f"{_check_label(item.check)}: {_check_status_label(item.status)}"
                f" — {item.reason}"
                for item in row.quality_checks
            )
            self.quality_checks_label.setText(f"チェック:\n{check_lines}")
        else:
            self.quality_checks_label.setText(
                "チェック: この測定に現在の品質レポートはありません（未評価）。"
            )
        if row.capabilities:
            capability_lines = "\n".join(
                f"{_claim_label(item.claim)}: {_capability_decision_label(item)}"
                f" — {' / '.join(item.reasons)}"
                for item in row.capabilities
            )
            self.quality_capabilities_label.setText(
                f"この測定で使える主張:\n{capability_lines}"
            )
        else:
            self.quality_capabilities_label.setText("この測定で使える主張: データなし")

        # Retake guidance and lineage (#468).
        names = {view.measurement_id: view.target_name for view in self._quality_views}

        def _name(measurement_id: str) -> str:
            return names.get(measurement_id, "別の測定")

        retake_lines = [
            f"再測定: {_retake_recommendation_label(row.retake_recommendation)}"
        ]
        guidance = row.retake_guidance
        if guidance is not None:
            if row.retake_reasons:
                retake_lines.append("理由: " + " / ".join(row.retake_reasons))
            if guidance.missing_evidence:
                retake_lines.append(
                    "不足している証拠: "
                    + "、".join(
                        _missing_evidence_label(code)
                        for code in guidance.missing_evidence
                    )
                )
            if guidance.remeasure:
                retake_lines.append(
                    "再測定の指針: "
                    + "；".join(_remeasure_label(code) for code in guidance.remeasure)
                )
        elif row.quality_report_state != "current":
            retake_lines.append(
                "現在の品質レポートがないため、証拠に基づく再測定の判定はできません。"
            )
        if row.supersedes_measurement_id is not None:
            retake_lines.append(
                f"この測定は {_name(row.supersedes_measurement_id)} の再測定です"
            )
        if row.superseded_by_measurement_id is not None:
            retake_lines.append(
                f"この測定は {_name(row.superseded_by_measurement_id)} に置き換えられています"
            )
        if row.has_lineage:
            retake_lines.append(
                f"この系譜で選択中の測定: {_name(row.selected_measurement_id)}"
            )
        self.retake_label.setText("\n".join(retake_lines))
        self.retake_button.setEnabled(True)

        self.quality_plot.clear()
        if row.dataset_id is None:
            return
        dataset = self.controller.dataset(row.dataset_id)
        trace = (
            DARK_THEME.scientific.predicted
            if row.evidence_type == "predicted"
            else DARK_THEME.scientific.measured
        )
        style = (
            Qt.PenStyle.DashLine
            if row.evidence_type == "predicted"
            else Qt.PenStyle.SolidLine
        )
        self.quality_plot.plot(
            dataset.frequency_hz,
            dataset.level_db,
            pen=pg.mkPen(trace.hex, width=2, style=style),
            name=_evidence_label(row.evidence_type),
        )
        self.quality_plot.enableAutoRange()

    def _start_retake(self) -> None:
        row_index = self.quality_table.currentRow()
        if not (0 <= row_index < len(self._quality_views)):
            return
        row = self._quality_views[row_index]
        self._retake_source_id = row.measurement_id
        self._set_notice(
            f"{row.target_name} の再測定です。REWから再取得し、"
            "「割り当て」で同じ測定点・入力役割・音源を選んでください。",
            None,
        )
        self.set_context("import")

    # ------------------------------------------------------------------
    # Comparison page

    def _build_comparison_page(self) -> None:
        page, host, layout = _page(
            "予測と実測を比較する",
            "保存済みの実測と予測の周波数応答を比較します。",
        )
        page.setObjectName("measurementComparisonPage")

        setup_card, setup_layout = _card("比較条件", host)
        form = QFormLayout()
        self.measured_combo = QComboBox(setup_card)
        self.predicted_combo = QComboBox(setup_card)
        form.addRow("実測", self.measured_combo)
        form.addRow("予測", self.predicted_combo)

        self.compare_low = QDoubleSpinBox(setup_card)
        self.compare_low.setRange(1.0, 100000.0)
        self.compare_low.setValue(20.0)
        self.compare_low.setSuffix(" Hz")
        self.compare_high = QDoubleSpinBox(setup_card)
        self.compare_high.setRange(1.0, 100000.0)
        self.compare_high.setValue(20000.0)
        self.compare_high.setSuffix(" Hz")
        form.addRow("下限", self.compare_low)
        form.addRow("上限", self.compare_high)
        setup_layout.addLayout(form)

        compare_row = QHBoxLayout()
        self.comparison_availability = QLabel("", setup_card)
        self.comparison_availability.setWordWrap(True)
        compare_row.addWidget(self.comparison_availability, 1)
        self.compare_button = QPushButton("比較して保存", setup_card)
        set_primary_action(self.compare_button)
        self.compare_button.clicked.connect(self._run_comparison)
        compare_row.addWidget(self.compare_button)
        setup_layout.addLayout(compare_row)
        layout.addWidget(setup_card)

        plot_card, plot_layout = _card("周波数応答", host)
        self.comparison_plot = pg.PlotWidget(plot_card)
        self.comparison_plot.setMinimumHeight(280)
        self.comparison_plot.setLabel("bottom", "周波数", units="Hz")
        self.comparison_plot.setLabel("left", "レベル", units="dB")
        _set_plot_appearance(self.comparison_plot)
        self.comparison_plot.addLegend()
        plot_layout.addWidget(self.comparison_plot)

        self.difference_plot = pg.PlotWidget(plot_card)
        self.difference_plot.setMinimumHeight(180)
        self.difference_plot.setLabel("bottom", "周波数", units="Hz")
        self.difference_plot.setLabel("left", "実測 − 予測", units="dB")
        _set_plot_appearance(self.difference_plot)
        plot_layout.addWidget(self.difference_plot)
        layout.addWidget(plot_card)

        result_card, result_layout = _card("比較結果", host)
        self.comparison_result = QLabel("比較未実行", result_card)
        self.comparison_result.setWordWrap(True)
        result_layout.addWidget(self.comparison_result)

        self.comparison_history = QTableWidget(0, 4, result_card)
        self.comparison_history.setHorizontalHeaderLabels(
            ["作成時刻", "帯域", "RMS差", "形状RMS"]
        )
        self.comparison_history.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.comparison_history.verticalHeader().setVisible(False)
        self.comparison_history.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.comparison_history.setMaximumHeight(180)
        result_layout.addWidget(self.comparison_history)
        layout.addWidget(result_card)
        layout.addStretch(1)
        self.pages.addWidget(page)

        self.measured_combo.currentIndexChanged.connect(self._preview_comparison_pair)
        self.predicted_combo.currentIndexChanged.connect(self._preview_comparison_pair)

    def _refresh_comparison_choices(self) -> None:
        measured = self.controller.comparison_candidates("measured")
        predicted = self.controller.comparison_candidates("predicted")
        self._fill_dataset_combo(self.measured_combo, measured)
        self._fill_dataset_combo(self.predicted_combo, predicted)

        ready = bool(measured and predicted)
        self.compare_button.setEnabled(ready)
        if ready:
            self.comparison_availability.setText(
                "実測は実線、予測は破線で表示します。比較結果は部屋状態と測定データに紐付けて保存します。"
            )
            set_semantic_state(self.comparison_availability, None)
        elif not measured and not predicted:
            self.comparison_availability.setText(
                "実測と予測の周波数応答がありません。先にREWを読み込み、証拠種別を割り当ててください。"
            )
            set_semantic_state(self.comparison_availability, SemanticState.UNSUPPORTED)
        elif not predicted:
            self.comparison_availability.setText(
                "予測の周波数応答がありません。予測を保存すると比較できます。"
            )
            set_semantic_state(self.comparison_availability, SemanticState.UNSUPPORTED)
        else:
            self.comparison_availability.setText(
                "実測の周波数応答がありません。REW測定を読み込むと比較できます。"
            )
            set_semantic_state(self.comparison_availability, SemanticState.UNSUPPORTED)

        self._preview_comparison_pair()
        comparisons = self.controller.saved_comparisons()
        self.comparison_history.setRowCount(len(comparisons))
        for row_index, comparison in enumerate(comparisons):
            rms = "—" if comparison.rms_difference_db is None else f"{comparison.rms_difference_db:.3f} dB"
            shape = "—" if comparison.shape_rms_db is None else f"{comparison.shape_rms_db:.3f} dB"
            values = (
                comparison.created_at,
                _format_band(comparison.actual_band_hz),
                rms,
                shape,
            )
            for column, value in enumerate(values):
                self.comparison_history.setItem(row_index, column, QTableWidgetItem(value))

    @staticmethod
    def _fill_dataset_combo(
        combo: QComboBox,
        rows: tuple[MeasurementView, ...],
    ) -> None:
        previous = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        for row in rows:
            if row.dataset_id is None:
                continue
            scene = "現在" if row.scene_matches_current else "測定時配置"
            combo.addItem(
                f"{_channel_role_label(row.channel_role)} · {row.target_name} · {scene}",
                row.dataset_id,
            )
        if previous is not None:
            index = combo.findData(previous)
            if index >= 0:
                combo.setCurrentIndex(index)
        combo.blockSignals(False)

    def _preview_comparison_pair(self) -> None:
        self.comparison_plot.clear()
        measured_id = self.measured_combo.currentData()
        predicted_id = self.predicted_combo.currentData()
        if isinstance(measured_id, str) and measured_id:
            measured = self.controller.dataset(measured_id)
            self.comparison_plot.plot(
                measured.frequency_hz,
                measured.level_db,
                pen=pg.mkPen(
                    DARK_THEME.scientific.measured.hex,
                    width=2,
                    style=Qt.PenStyle.SolidLine,
                ),
                name="実測",
            )
        if isinstance(predicted_id, str) and predicted_id:
            predicted = self.controller.dataset(predicted_id)
            self.comparison_plot.plot(
                predicted.frequency_hz,
                predicted.level_db,
                pen=pg.mkPen(
                    DARK_THEME.scientific.predicted.hex,
                    width=2,
                    style=Qt.PenStyle.DashLine,
                ),
                name="予測",
            )
        if measured_id or predicted_id:
            self.comparison_plot.enableAutoRange()

    def _run_comparison(self) -> None:
        measured_id = self.measured_combo.currentData()
        predicted_id = self.predicted_combo.currentData()
        if not isinstance(measured_id, str) or not isinstance(predicted_id, str):
            self._set_notice("実測と予測を一つずつ選択してください。", SemanticState.WARNING)
            return
        try:
            saved = self.controller.compare_datasets(
                measured_id,
                predicted_id,
                low_hz=float(self.compare_low.value()),
                high_hz=float(self.compare_high.value()),
            )
        except Exception as exc:
            self._set_notice(f"比較できませんでした · {exc}", SemanticState.ERROR)
            return
        self._last_comparison = saved
        self._show_comparison(saved)
        self._set_notice(
            "比較結果を保存しました。",
            SemanticState.SUCCESS,
        )
        self._refresh_comparison_choices()

    def _show_comparison(self, saved: CadMeasurementComparison) -> None:
        rms = "—" if saved.rms_difference_db is None else f"{saved.rms_difference_db:.3f} dB"
        mean = "—" if saved.mean_difference_db is None else f"{saved.mean_difference_db:.3f} dB"
        shape = "—" if saved.shape_rms_db is None else f"{saved.shape_rms_db:.3f} dB"
        self.comparison_result.setText(
            f"有効点 {saved.valid_points:,} / {saved.total_grid_points:,} · "
            f"平均差 {mean} · RMS差 {rms} · 形状RMS {shape}\n"
            f"実帯域 {_format_band(saved.actual_band_hz)}"
        )
        self.difference_plot.clear()
        self.difference_plot.plot(
            saved.grid_hz,
            saved.difference_db,
            pen=pg.mkPen(
                DARK_THEME.scientific.primary_trace.hex,
                width=2,
                style=Qt.PenStyle.DotLine,
            ),
            name="実測 − 予測",
        )
        self.difference_plot.enableAutoRange()

    # ------------------------------------------------------------------

    def _start_job(
        self,
        call: Callable[[], object],
        on_success: Callable[[object], None],
        error_prefix: str,
    ) -> None:
        if self._disposed:
            return
        key = uuid4().hex
        self._job_handlers[key] = (on_success, error_prefix)
        self._job_pool.start(
            key,
            lambda _cancel_event: call(),
            self._job_completed,
        )

    @Slot(object, object, object)
    def _job_completed(self, key: object, result: object, error: object) -> None:
        handler = self._job_handlers.pop(str(key), None)
        if handler is None or self._disposed:
            return
        on_success, error_prefix = handler
        if error is not None:
            if error != WORKER_CANCELLED:
                self._set_notice(
                    f"{error_prefix} · {error}",
                    SemanticState.ERROR,
                )
            return
        on_success(result)

    def _set_notice(
        self,
        message: str,
        state: SemanticState | None,
    ) -> None:
        self.notice.setText(message)
        self.notice.setVisible(bool(message))
        set_semantic_state(self.notice, state)

    def before_deactivate(self) -> tuple[bool, str | None]:
        if self._job_pool.active_count:
            return False, "REWの読み込み処理が完了してから画面を切り替えてください"
        return True, None

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._disposed = True
        report = self._job_pool.shutdown()
        self._job_handlers.clear()
        if not report.all_stopped:
            self._set_notice(
                "バックグラウンド処理の停止が遅延しています · 遅延結果は適用しません",
                None,
            )
        super().closeEvent(event)


def build_measurement_workspace_mount(
    controller: MeasurementWorkflowController,
) -> WorkspaceMount:
    """Build the shell-owned mount without a legacy QMainWindow/QDockWidget bridge."""

    workspace = MeasurementPageWorkspace(controller)
    return WorkspaceMount.from_widget(
        workspace,
        on_activate=workspace.refresh,
        before_deactivate=workspace.before_deactivate,
        on_context_changed=workspace.set_context,
        on_entity_requested=workspace.focus_entity,
    )


def create_measurement_workspace_factory(
    scene_repository: SceneRepository,
    document_id: str,
    *,
    rew_client: RewReadSource | None = None,
) -> WorkspaceFactory:
    """Return the lazy factory consumed by build_canonical_workspace_registrations."""

    def build() -> WorkspaceMount:
        controller = MeasurementWorkflowController(
            scene_repository,
            document_id,
            rew_client=rew_client,
        )
        return build_measurement_workspace_mount(controller)

    return build


__all__ = [
    "MeasurementPageWorkspace",
    "build_measurement_workspace_mount",
    "create_measurement_workspace_factory",
]
