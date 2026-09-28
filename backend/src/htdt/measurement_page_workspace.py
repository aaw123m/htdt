from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from typing import Any
from uuid import uuid4

import pyqtgraph as pg
from pyqtgraph.exporters import ImageExporter
from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import file_dialog_memory
from .analysis_export import (
    AnalysisExportMeta,
    build_analysis_export,
    comparison_metadata_entries,
    comparison_side_series,
    render_analysis_csv,
    series_from_comparison,
)
from .cad_display_labels import saved_label
from .cad_measurement_models import (
    MEASUREMENT_ATTACHMENT_KINDS,
    CadMeasurementComparison,
)
from .cad_measurement_quality import (
    CadMeasurementCapability,
    CadMicrophoneCapture,
    CadPlaybackCapture,
)
from .cad_repository import SceneRepository
from .cad_scene import Direction3
from .export_io import write_text_atomic
from .ingress import read_file_bounded
from .limits import MAX_NATIVE_REW_TEXT_FILE_BYTES
from .measurement_analysis import (
    DISPLAY_SMOOTHING_FRACTIONS,
    phase_trace,
    processing_summary,
    smoothed_level_trace,
    trace_label,
)
from .measurement_instrument_onboarding import (
    InstrumentStep,
    evaluate_instrument_onboarding,
)
from .measurement_workflow import (
    AcquisitionCapture,
    AssignmentCorrection,
    BatchImportItem,
    MeasurementAssignment,
    MeasurementView,
    MeasurementWorkflowController,
    PendingMeasurementImport,
    RewReadSource,
)
from .native_worker import WORKER_CANCELLED, NativeWorkerPool
from .scientific_plot_style import (
    PlotCursor,
    TraceSemantic,
    add_reference_line,
    add_scientific_legend,
    apply_scientific_appearance,
    link_x_axis,
    show_plot_state,
    trace_pen,
)
from .user_facing_error import (
    RETRYABLE_ERROR_CODES,
    log_operation_error,
    operation_error_message,
    to_user_facing_error,
    warn_user,
)
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
from .workspace_dirty_state import DirtyResolutionAction, WorkspaceDirtyState


_CONTEXT_IDS = (
    "import",
    "assignment",
    "campaign",
    "quality",
    "comparison",
    "calibration",
)

_CELL_STATUS_LABELS = {
    "not_started": "未着手",
    "staged": "取込済",
    "assignment_incomplete": "割当未完了",
    "quality_pending": "品質確認待ち",
    "retake_required": "要再測定",
    "completed": "完了",
    "skipped": "スキップ",
}
_VARIANT_PURPOSE_LABELS = {
    "measurement": "測定",
    "calibration": "キャリブレーション",
    "holdout": "ホールドアウト",
    "diagnostic": "診断",
    "validation": "検証",
}
_USER_ROLE = int(Qt.ItemDataRole.UserRole)


def _evidence_label(value: str) -> str:
    return {
        "measured": "実測",
        "derived": "派生",
        "predicted": "予測",
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
        "synthetic_fixture": "合成フィクスチャ",
    }.get(value, value)


def _disposition_label(value: str | None) -> str:
    """Lifecycle disposition (#509); None is the normal active state."""
    return {
        None: "有効",
        "active": "有効",
        "corrected": "訂正済み",
        "misassigned": "誤割当",
        "excluded_from_normal_use": "通常利用から除外",
        "test_only": "テスト測定",
        "duplicate_import": "重複取り込み",
    }.get(value, str(value))


def _duplicate_kind_label(value: str) -> str:
    return {
        "exact_duplicate": "完全な重複",
        "same_acquisition": "同一取得の再エクスポート",
        "new": "新規",
    }.get(value, value)


def _resolution_label(value: str) -> str:
    return {
        "reuse_existing": "既存の測定を利用",
        "import_as_new": "別の測定として保存",
    }.get(value, value)


def _batch_status_label(value: str) -> str:
    return {
        "staged": "保留中",
        "committed": "保存済み",
        "reused": "既存へ解決",
        "failed": "失敗",
    }.get(value, value)


def _mismatch_label(code: str) -> str:
    """Advisory A/B semantic-difference codes (#483/#852)."""
    return {
        "evidence_type": "証拠種別が異なります",
        "channel_role": "入力役割が異なります",
        "target": "測定位置が異なります",
        "source_speakers": "音源スピーカーが異なります",
        "scene_revision": "測定時の部屋状態が異なります",
        "smoothing": "入力スムージング処理が異なります",
        "acquisition_context": "取得条件が異なります",
        "routing_profile": "ルーティングプロファイルが異なります",
        "level_reference": "レベル基準が異なります",
        "timing_reference": "タイミング基準が異なります",
        "radiation_scope": "放射範囲が異なります",
    }.get(code, code)


def _level_compatibility_label(value: str | None) -> str:
    """Persisted level-compat verdict of a saved comparison (#852)."""
    return {
        "absolute_level_comparable": "絶対レベルで比較可能",
        "normalized_shape_comparable": "形状比較（参照帯域でレベル正規化）",
        "diagnostic_only": "診断のみ（レベル差は絶対的な音圧差ではありません）",
        None: "—",
    }.get(value, str(value))


def _spatial_change_label(change_kind: str) -> str:
    return {
        "moved": "移動",
        "removed": "削除済み",
        "added": "追加",
        "changed": "変更",
    }.get(change_kind, change_kind)


def _attachment_kind_label(value: str) -> str:
    return {
        "mdat": ".mdat",
        "calibration": "校正ファイル",
        "notes": "設定ノート",
        "other": "その他",
    }.get(value, value)


def _trace_color(view: MeasurementView) -> Any:
    tokens = DARK_THEME.scientific
    return tokens.predicted if view.evidence_type == "predicted" else tokens.measured


def _trace_semantic(view: MeasurementView) -> TraceSemantic:
    return (
        TraceSemantic.PREDICTED
        if view.evidence_type == "predicted"
        else TraceSemantic.MEASURED
    )


def _format_band(band: tuple[float, float] | None) -> str:
    if band is None:
        return "—"
    return f"{band[0]:.1f}–{band[1]:.1f} Hz"


def _set_plot_appearance(plot: pg.PlotWidget) -> None:
    """Standard scientific canvas — shared grammar lives in
    ``scientific_plot_style`` (#579); this stays as the local call-site shim."""

    apply_scientific_appearance(plot)


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
    # Emitted from the batch-commit worker thread; Qt queues it to the UI
    # thread, so the notice can show live progress without polling.
    batch_commit_progress = Signal(int, int)
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
        self.batch_commit_progress.connect(self._on_batch_commit_progress)
        self._job_handlers: dict[
            str,
            tuple[Callable[[object], None], str, Callable[[], None] | None],
        ] = {}
        # Latest-wins guard for jobs that can overlap (REW list/read share the
        # pool): only the most recent submission of a purpose applies its
        # completion — an earlier late result would stage stale data.
        self._job_purpose: dict[str, str] = {}
        self._latest_job_key: dict[str, str] = {}
        self._disposed = False
        self._rew_rows: list[dict[str, Any]] = []
        self._quality_views: tuple[MeasurementView, ...] = ()
        self._commit_job_key: str | None = None
        self._last_comparison: CadMeasurementComparison | None = None
        self._saved_comparisons: tuple[CadMeasurementComparison, ...] = ()
        self._retake_source_id: str | None = None
        self._correction_target_id: str | None = None
        self._spatial_context: Any = None
        self._spatial_viewport: Any = None
        self._spatial_viewport_failed = False
        # Semantic identity of the staged import acknowledged via keep_draft
        # (#610/#796); a re-staged or re-created import re-blocks.
        self._pending_release: str | None = None

        self.setObjectName("measurementPageWorkspace")
        set_surface_role(self, SurfaceRole.BASE)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Persistent selection context (#586): the active measurement or
        # comparison identity stays visually tied to the detail/plot below.
        self.context_label = QLabel(self)
        self.context_label.setObjectName("measurementContextLabel")
        self.context_label.setContentsMargins(24, 8, 24, 4)
        self.context_label.setText("測定未選択")
        root.addWidget(self.context_label)

        self._last_operation_error_detail: str | None = None
        # Notice + optional next-step action: success text alone still leaves
        # the flow dead-ended, so import/commit notices can carry the button
        # that continues the workflow (round-13).
        self.notice_row = QWidget(self)
        notice_row_layout = QHBoxLayout(self.notice_row)
        notice_row_layout.setContentsMargins(24, 8, 24, 8)
        notice_row_layout.setSpacing(8)
        self.notice = QLabel(self.notice_row)
        self.notice.setObjectName("measurementWorkspaceNotice")
        self.notice.setWordWrap(True)
        notice_row_layout.addWidget(self.notice, 1)
        self.notice_action = QPushButton(self.notice_row)
        self.notice_action.setObjectName("measurementWorkspaceNoticeAction")
        self.notice_action.setVisible(False)
        notice_row_layout.addWidget(self.notice_action)
        self.notice_row.setVisible(False)
        root.addWidget(self.notice_row)

        self.pages = QStackedWidget(self)
        self.pages.setObjectName("measurementPageStack")
        root.addWidget(self.pages, 1)

        self._build_import_page()
        self._build_assignment_page()
        self._build_campaign_page()
        self._build_quality_page()
        self._build_comparison_page()
        self._build_calibration_page()
        self.refresh()

    # ------------------------------------------------------------------
    # Shell interface

    def set_context(self, context_id: str) -> None:
        if context_id not in _CONTEXT_IDS:
            raise ValueError(f"unknown measurement context: {context_id}")
        self.current_context_id = context_id
        self.pages.setCurrentIndex(_CONTEXT_IDS.index(context_id))
        self.refresh()
        self._update_context_label()

    def focus_entity(self, entity_id: str) -> None:
        def _row_index() -> int | None:
            for index, row in enumerate(self._quality_views):
                if (
                    row.measurement_id == entity_id
                    or row.target_entity_id == entity_id
                ):
                    return index
            return None

        row_index = _row_index()
        if row_index is None:
            return
        if self.current_context_id != "quality":
            # The selection only exists on the quality page — show it before
            # selecting, otherwise the row changes invisibly behind the
            # section the link specified.
            self.set_context("quality")
            row_index = _row_index()
            if row_index is None:
                return
        self.quality_table.selectRow(row_index)
        self._show_quality_row(row_index)

    def select_measurement_id(self, measurement_id: str) -> bool:
        """Deep-link/palette focus port: select the quality row for
        ``measurement_id``; returns False when the record is absent."""
        self.refresh()
        for index, row in enumerate(self._quality_views):
            if row.measurement_id != measurement_id:
                continue
            if self.current_context_id != "quality":
                self.set_context("quality")
            self.quality_table.selectRow(index)
            self._show_quality_row(index)
            return True
        return False

    def refresh(self) -> None:
        # One authoritative listing per refresh: ``measurement_views``
        # re-verifies every bound dataset (asset hash + importer replay), so
        # the batch labels, campaign combo, quality table and comparison
        # pickers must share the result rather than each paying the pass.
        views = self.controller.measurement_views()
        batch_items = self.controller.batch_items()
        self._refresh_pending()
        self._refresh_batch(batch_items, views)
        self._refresh_assignment_options(batch_items)
        self._refresh_campaign(views)
        self._refresh_quality(views)
        self._refresh_comparison_choices(views)
        self._refresh_onboarding()

    def import_rew_text_dialog(self) -> None:
        path, _ = file_dialog_memory.get_open_file_name(
            self,
            "REWテキストを選択",
            'measurement.rew_text',
            "REWテキスト (*.txt *.frd);;すべてのファイル (*)",
        )
        if not path:
            return
        try:
            file_path = Path(path)
            raw = read_file_bounded(
                file_path,
                MAX_NATIVE_REW_TEXT_FILE_BYTES,
                label="REWテキストファイル",
            )
            self.controller.stage_rew_text(raw, file_path.name)
        except Exception as exc:
            self._operation_error_notice("読み込みに失敗しました", exc)
            return
        self._set_notice(
            "読み込みました。次に「割り当て」で測定点と入力役割を確認してください。",
            SemanticState.SUCCESS,
            action=("割り当てへ進む", lambda: self.set_context("assignment")),
        )
        self.refresh()

    def import_rew_batch_dialog(self) -> None:
        """Stage one or many REW text exports into the batch queue (#446)."""
        paths, _ = file_dialog_memory.get_open_file_names(
            self,
            "REWテキストを追加（複数選択可）",
            'measurement.rew_text',
            "REWテキスト (*.txt *.frd);;すべてのファイル (*)",
        )
        if not paths:
            return
        files: list[tuple[bytes, str]] = []
        for path in paths:
            try:
                file_path = Path(path)
                files.append(
                    (
                        read_file_bounded(
                            file_path,
                            MAX_NATIVE_REW_TEXT_FILE_BYTES,
                            label="REWテキストファイル",
                        ),
                        file_path.name,
                    )
                )
            except Exception as exc:
                self._operation_error_notice(
                    f"読み込みに失敗しました · {file_path.name}", exc
                )
                return
        try:
            items = self.controller.stage_rew_text_files(files)
        except Exception as exc:
            self._operation_error_notice("読み込みに失敗しました", exc)
            return
        self._set_notice(
            f"{len(items)} 件を読み込みました。「割り当て」で項目の意味付けと保存を行ってください。",
            SemanticState.SUCCESS,
            action=("割り当てへ進む", lambda: self.set_context("assignment")),
        )
        self.refresh()

    def _attach_to_selected_batch_item(self) -> None:
        selected = self.batch_table.selectedItems()
        if not selected:
            self._set_notice(
                "添付を追加するバッチ項目を選択してください。",
                SemanticState.WARNING,
            )
            return
        item_id = selected[0].data(Qt.ItemDataRole.UserRole)
        if not isinstance(item_id, str):
            return
        path, _ = file_dialog_memory.get_open_file_name(
            self,
            "添付ファイルを選択",
            'measurement.attach',
            "すべてのファイル (*)",
        )
        if not path:
            return
        kind = str(self.batch_attach_kind_combo.currentData())
        try:
            file_path = Path(path)
            raw = read_file_bounded(
                file_path,
                MAX_NATIVE_REW_TEXT_FILE_BYTES,
                label="ソース添付",
            )
            self.controller.attach_to_batch_item(
                item_id,
                filename=file_path.name,
                raw_bytes=raw,
                kind=kind,
            )
        except Exception as exc:
            self._operation_error_notice("添付に失敗しました", exc)
            return
        self._set_notice(
            "添付を項目に紐付けました。保存時に測定の証拠として登録されます。",
            SemanticState.SUCCESS,
        )
        self.refresh()

    def _clear_committed_batch(self) -> None:
        self.controller.discard_batch_committed()
        self.refresh()

    def _batch_resolution_changed(self, item_id: str, value: int) -> None:
        combo = self.batch_table.cellWidget(
            self._batch_table_row(item_id), 5
        )
        if combo is None:
            return
        try:
            self.controller.set_batch_resolution(
                item_id, str(combo.itemData(value))
            )
        except Exception as exc:
            self._operation_error_notice("解決方法を変更できませんでした", exc)

    def _batch_table_row(self, item_id: str) -> int:
        for row in range(self.batch_table.rowCount()):
            item = self.batch_table.item(row, 0)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == item_id:
                return row
        return -1

    def _refresh_batch(
        self,
        items: tuple[BatchImportItem, ...],
        views: tuple[MeasurementView, ...],
    ) -> None:
        measurement_labels = self._measurement_display_labels(views)
        self.batch_table.setRowCount(len(items))
        for row_index, item in enumerate(items):
            band = _format_band(item.frequency_band_hz)
            phase = (
                "—"
                if item.has_phase_samples is None
                else ("あり" if item.has_phase_samples else "なし")
            )
            duplicate = _duplicate_kind_label(item.duplicate_kind)
            if item.duplicate_of_name:
                duplicate += f" → {item.duplicate_of_name}"
            if item.status == "failed" and item.error:
                status = f"失敗: {item.error}"
            else:
                status = _batch_status_label(item.status)
            committed_to = (
                item.duplicate_of_name
                or (
                    measurement_labels.get(
                        item.committed_measurement_id,
                        item.committed_measurement_id,
                    )
                    if item.committed_measurement_id
                    else "—"
                )
                if item.status in ("committed", "reused")
                else ("保留中" if item.status == "staged" else "—")
            )
            attachment_note = (
                f"{item.attachment_count} 件" if item.attachment_count else "—"
            )
            resolution_text = (
                "—" if item.duplicate_kind == "new" else _resolution_label(item.resolution)
            )
            values = (
                item.filename,
                status,
                band,
                phase,
                duplicate,
                resolution_text,
                f"{committed_to} · 添付 {attachment_note}",
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setData(Qt.ItemDataRole.UserRole, item.item_id)
                self.batch_table.setItem(row_index, column, cell)
            if (
                item.status == "staged"
                and item.duplicate_kind in ("exact_duplicate", "same_acquisition")
            ):
                combo = QComboBox(self.batch_table)
                combo.addItem("既存の測定を利用", "reuse_existing")
                combo.addItem("別の測定として保存", "import_as_new")
                index = combo.findData(item.resolution)
                if index >= 0:
                    combo.setCurrentIndex(index)
                combo.currentIndexChanged.connect(
                    lambda value, item_id=item.item_id: self._batch_resolution_changed(
                        item_id, value
                    )
                )
                self.batch_table.setCellWidget(row_index, 5, combo)

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

        batch_card, batch_layout = _card("バッチ取り込み（キャンペーン）", host)
        batch_note = QLabel(
            "複数のREWテキストを一度に読み込み、保存前に各項目の状態を確認します。"
            "重複は自動で統合せず、項目ごとの解決方法を選んでください。",
            batch_card,
        )
        batch_note.setWordWrap(True)
        set_typography_role(batch_note, TypographyRole.SECONDARY)
        batch_layout.addWidget(batch_note)

        batch_buttons = QHBoxLayout()
        self.batch_add_button = QPushButton("REWテキストを追加（複数可）", batch_card)
        self.batch_add_button.clicked.connect(self.import_rew_batch_dialog)
        batch_buttons.addWidget(self.batch_add_button)
        self.batch_attach_kind_combo = QComboBox(batch_card)
        for kind in MEASUREMENT_ATTACHMENT_KINDS:
            self.batch_attach_kind_combo.addItem(_attachment_kind_label(kind), kind)
        batch_buttons.addWidget(self.batch_attach_kind_combo)
        self.batch_attach_button = QPushButton("選択項目に添付を追加", batch_card)
        self.batch_attach_button.clicked.connect(self._attach_to_selected_batch_item)
        batch_buttons.addWidget(self.batch_attach_button)
        self.batch_clear_button = QPushButton("保存済みをクリア", batch_card)
        self.batch_clear_button.clicked.connect(self._clear_committed_batch)
        batch_buttons.addWidget(self.batch_clear_button)
        self.batch_cancel_button = QPushButton("保存をキャンセル", batch_card)
        self.batch_cancel_button.clicked.connect(self._cancel_batch_commit)
        self.batch_cancel_button.setVisible(False)
        batch_buttons.addWidget(self.batch_cancel_button)
        batch_buttons.addStretch(1)
        batch_layout.addLayout(batch_buttons)

        self.batch_table = QTableWidget(0, 7, batch_card)
        self.batch_table.setHorizontalHeaderLabels(
            ["ファイル", "状態", "帯域", "位相", "重複", "解決", "保存先"]
        )
        self.batch_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.batch_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.batch_table.verticalHeader().setVisible(False)
        self.batch_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.batch_table.horizontalHeader().setStretchLastSection(True)
        self.batch_table.setMinimumHeight(160)
        batch_layout.addWidget(self.batch_table)
        layout.addWidget(batch_card)
        layout.addStretch(1)
        self.pages.addWidget(page)

    def _refresh_rew_async(self) -> None:
        self._set_notice("REW測定一覧を読み込み中です。", None)
        self._start_job(
            self.controller.list_rew_measurements,
            self._apply_rew_list,
            "REW一覧の読み込みに失敗しました",
            on_retry=self._refresh_rew_async,
            purpose="rew_list",
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
            on_retry=self._read_rew_async,
            purpose="rew_read",
        )

    def _stage_rew_snapshot(self, value: object) -> None:
        try:
            self.controller.stage_rew_snapshot(value)  # type: ignore[arg-type]
        except Exception as exc:
            self._operation_error_notice("REW測定の確認に失敗しました", exc)
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
        duplicate_note = ""
        if pending.duplicate_of_measurement_id is not None:
            duplicate_name = next(
                (
                    view.effective_target_name
                    for view in getattr(self, "_quality_views", ())
                    if view.measurement_id == pending.duplicate_of_measurement_id
                ),
                pending.duplicate_of_measurement_id,
            )
            duplicate_note = (
                f"\n注意: {_duplicate_kind_label(pending.duplicate_kind)}の測定が"
                f"既に保存されています（{duplicate_name}）。"
            )
        self.pending_import_label.setText(
            f"{pending.source_label}\n"
            f"{pending.sample_count:,} 点 · {_format_band(pending.frequency_band_hz)} · {phase}\n"
            "保存前の一時データです。意味付けは「割り当て」で確定します。"
            + duplicate_note
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

        self.assignment_scope_combo = QComboBox(assign_card)
        form.addRow("対象", self.assignment_scope_combo)

        # Grouped by operator meaning (#586): identity, evidence class, then
        # source/routing — instead of one visually flat form.
        identity_label = QLabel("測定の識別", assign_card)
        set_typography_role(identity_label, TypographyRole.SECONDARY)
        form.addRow(identity_label)

        self.target_combo = QComboBox(assign_card)
        form.addRow("測定位置", self.target_combo)

        evidence_label = QLabel("証拠クラス", assign_card)
        set_typography_role(evidence_label, TypographyRole.SECONDARY)
        form.addRow(evidence_label)

        self.evidence_combo = QComboBox(assign_card)
        for label, value in (
            ("実測", "measured"),
            ("予測", "predicted"),
            ("派生", "derived"),
            ("未確認", "unknown"),
        ):
            self.evidence_combo.addItem(label, value)
        form.addRow("証拠種別", self.evidence_combo)

        # Typed human labels (#586): the raw role token stays searchable via
        # the editable combo, but normal presentation is the localized label.
        self.channel_combo = QComboBox(assign_card)
        self.channel_combo.setEditable(True)
        for label, value in (
            ("不明", "unknown"),
            ("フロント左 (FL)", "front_left"),
            ("センター (C)", "center"),
            ("フロント右 (FR)", "front_right"),
            ("サブウーファー (LFE/Sub)", "subwoofer"),
        ):
            self.channel_combo.addItem(label, value)
        form.addRow("入力役割", self.channel_combo)

        self.radiation_combo = QComboBox(assign_card)
        for label, value in (
            ("未確認", "unknown"),
            ("単一音源", "single"),
            ("バスマネジメント", "bass_managed"),
            ("混在", "mixed"),
        ):
            self.radiation_combo.addItem(label, value)
        form.addRow("放射範囲", self.radiation_combo)

        routing_label = QLabel("音源 / ルーティング", assign_card)
        set_typography_role(routing_label, TypographyRole.SECONDARY)
        form.addRow(routing_label)

        self.routing_combo = QComboBox(assign_card)
        for label, value in (
            ("未確認", "unknown"),
            ("検証済み", "verified"),
            ("手動指定", "manual"),
            ("推定", "inferred"),
        ):
            self.routing_combo.addItem(label, value)
        form.addRow("ルーティング根拠", self.routing_combo)

        # #659: the SceneRevision the measurement was acquired against. The
        # current head is only the proposal; the user can explicitly bind a
        # past revision so delayed imports never silently join the newest
        # layout.
        self.acquisition_revision_combo = QComboBox(assign_card)
        self.acquisition_revision_combo.setMinimumContentsLength(24)
        self.acquisition_revision_combo.activated.connect(
            self._acquisition_revision_changed
        )
        form.addRow("取得時の配置", self.acquisition_revision_combo)

        # #473: a persisted verified channel-map can be selected; its entry
        # for the channel role supplies the observed speakers when nothing
        # is checked manually.
        self.routing_profile_combo = QComboBox(assign_card)
        self.routing_profile_combo.setMinimumContentsLength(24)
        form.addRow("ルーティングプロファイル", self.routing_profile_combo)
        assign_layout.addLayout(form)

        # #471: acquisition conditions — microphone identity/direction and
        # playback/AVR state, persisted as a CadAcquisitionContext authority
        # bound to the new measurement. An empty section means no context is
        # persisted (missing evidence, never a fabricated one); a saved
        # context can be re-applied as a preset.
        acquisition_card, acquisition_layout = _card("取得条件", assign_card)
        preset_row = QHBoxLayout()
        self.acquisition_preset_combo = QComboBox(acquisition_card)
        self.acquisition_preset_combo.setMinimumContentsLength(24)
        preset_row.addWidget(self.acquisition_preset_combo, 1)
        self.acquisition_preset_button = QPushButton("プリセット適用", acquisition_card)
        self.acquisition_preset_button.clicked.connect(self._apply_acquisition_preset)
        preset_row.addWidget(self.acquisition_preset_button)
        acquisition_layout.addLayout(preset_row)

        acquisition_form = QFormLayout()
        self.mic_orientation_combo = QComboBox(acquisition_card)
        for label, value in (
            ("未確認", "unknown"),
            ("上向き（0°プロファイル）", "0deg"),
            ("前方（90°プロファイル）", "90deg"),
        ):
            self.mic_orientation_combo.addItem(label, value)
        acquisition_form.addRow("マイク方向", self.mic_orientation_combo)

        self.mic_manufacturer_edit = QLineEdit(acquisition_card)
        acquisition_form.addRow("マイク メーカー", self.mic_manufacturer_edit)
        self.mic_model_edit = QLineEdit(acquisition_card)
        acquisition_form.addRow("マイク モデル", self.mic_model_edit)
        self.mic_serial_edit = QLineEdit(acquisition_card)
        acquisition_form.addRow("マイク シリアル", self.mic_serial_edit)
        self.mic_sample_rate_edit = QLineEdit(acquisition_card)
        self.mic_sample_rate_edit.setPlaceholderText("例: 48000")
        acquisition_form.addRow("マイク サンプルレート", self.mic_sample_rate_edit)
        self.mic_cal_file_edit = QLineEdit(acquisition_card)
        acquisition_form.addRow("校正ファイル名", self.mic_cal_file_edit)
        self.mic_cal_sha_edit = QLineEdit(acquisition_card)
        self.mic_cal_sha_edit.setPlaceholderText("SHA-256（64桁hex）")
        acquisition_form.addRow("校正ファイルSHA-256", self.mic_cal_sha_edit)

        self.output_device_edit = QLineEdit(acquisition_card)
        acquisition_form.addRow("出力デバイス", self.output_device_edit)
        self.avr_model_edit = QLineEdit(acquisition_card)
        acquisition_form.addRow("AVR モデル", self.avr_model_edit)
        self.avr_volume_edit = QLineEdit(acquisition_card)
        self.avr_volume_edit.setPlaceholderText("例: -15.0")
        acquisition_form.addRow("AVR ボリューム(dB)", self.avr_volume_edit)
        self.avr_processing_edit = QLineEdit(acquisition_card)
        acquisition_form.addRow("AVR 処理モード", self.avr_processing_edit)
        self.avr_peq_edit = QLineEdit(acquisition_card)
        acquisition_form.addRow("AVR PEQモード", self.avr_peq_edit)
        acquisition_layout.addLayout(acquisition_form)
        assign_layout.addWidget(acquisition_card)

        speaker_label = QLabel("音源スピーカー", assign_card)
        set_typography_role(speaker_label, TypographyRole.SECONDARY)
        assign_layout.addWidget(speaker_label)
        self.source_speaker_list = QListWidget(assign_card)
        self.source_speaker_list.setMaximumHeight(150)
        assign_layout.addWidget(self.source_speaker_list)

        # Compact pre-save review (#586 §4): the exact semantic assignment
        # in human terms before commit — no separate confirmation dialog.
        self.assignment_summary_label = QLabel("", assign_card)
        self.assignment_summary_label.setWordWrap(True)
        assign_layout.addWidget(self.assignment_summary_label)

        save_row = QHBoxLayout()
        save_row.addStretch(1)
        self.assignment_save_button = QPushButton("測定を保存", assign_card)
        set_primary_action(self.assignment_save_button)
        self.assignment_save_button.clicked.connect(self._commit_assignment)
        save_row.addWidget(self.assignment_save_button)
        assign_layout.addLayout(save_row)
        layout.addWidget(assign_card)

        self.target_combo.currentIndexChanged.connect(self._update_assignment_summary)
        self.evidence_combo.currentIndexChanged.connect(self._update_assignment_summary)
        self.channel_combo.currentIndexChanged.connect(self._update_assignment_summary)
        self.radiation_combo.currentIndexChanged.connect(self._update_assignment_summary)
        self.routing_combo.currentIndexChanged.connect(self._update_assignment_summary)
        self.source_speaker_list.itemChanged.connect(self._update_assignment_summary)
        layout.addStretch(1)
        self.pages.addWidget(page)

    def _scope_index(self, scope: tuple) -> int:
        for index in range(self.assignment_scope_combo.count()):
            if self.assignment_scope_combo.itemData(index) == scope:
                return index
        return -1

    def _refresh_assignment_options(
        self,
        items: tuple[BatchImportItem, ...],
    ) -> None:
        pending = self.controller.pending_import
        batch_items = [
            item
            for item in items
            if item.status in ("staged", "failed") and item.error is None
        ]
        batch_open = [item for item in batch_items if item.status == "staged"]

        previous_scope = self.assignment_scope_combo.currentData()
        self.assignment_scope_combo.blockSignals(True)
        self.assignment_scope_combo.clear()
        correction_armed = self._correction_target_id is not None
        correction_view = next(
            (
                view
                for view in self._quality_views
                if view.measurement_id == self._correction_target_id
            ),
            None,
        )
        if correction_view is not None:
            self.assignment_scope_combo.addItem(
                f"訂正: {correction_view.effective_target_name} の割り当て",
                ("correction", correction_view.measurement_id),
            )
        if pending is not None:
            self.assignment_scope_combo.addItem("現在の読み込み", ("pending", None))
        if batch_open:
            self.assignment_scope_combo.addItem(
                "バッチ全項目（共通割り当て）", ("batch_all", None)
            )
            for item in batch_open:
                self.assignment_scope_combo.addItem(
                    f"バッチ: {item.filename}", ("batch_item", item.item_id)
                )
        if correction_armed and correction_view is not None:
            self.assignment_scope_combo.setCurrentIndex(0)
        elif self._retake_source_id is not None and pending is not None:
            index = self._scope_index(("pending", None))
            if index >= 0:
                self.assignment_scope_combo.setCurrentIndex(index)
        elif isinstance(previous_scope, tuple) and previous_scope:
            index = self._scope_index(previous_scope)
            if index >= 0:
                self.assignment_scope_combo.setCurrentIndex(index)
        self.assignment_scope_combo.blockSignals(False)

        scope = self.assignment_scope_combo.currentData()
        has_target = isinstance(scope, tuple) and scope is not None
        self.assignment_save_button.setEnabled(has_target)
        if pending is None:
            if self._correction_target_id is not None:
                self.assignment_pending_label.setText(
                    "既存測定の割り当て訂正です。元の測定データは変更されません。"
                )
            elif batch_open:
                self.assignment_pending_label.setText(
                    f"{len(batch_open)} 件のバッチ項目が割り当て待ちです。"
                )
            else:
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
        except Exception as exc:
            targets = ()
            speakers = ()
            self._operation_error_notice(
                "割り当て対象を読み込めませんでした",
                exc,
                effect=None,
                severity=SemanticState.WARNING,
            )

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

        # #659 acquisition-time revision picker: head proposal plus history.
        self.acquisition_revision_combo.blockSignals(True)
        self.acquisition_revision_combo.clear()
        try:
            revisions = self.controller.revision_options()
        except Exception as exc:
            revisions = ()
            self._operation_error_notice(
                "履歴候補を読み込めませんでした",
                exc,
                effect=None,
                severity=SemanticState.WARNING,
            )
        for revision in revisions:
            self.acquisition_revision_combo.addItem(
                revision.created_at_utc, revision.revision_id
            )
        if pending is not None:
            index = self.acquisition_revision_combo.findData(
                pending.scene_revision_id
            )
            if index >= 0:
                self.acquisition_revision_combo.setCurrentIndex(index)
        self.acquisition_revision_combo.blockSignals(False)
        self.acquisition_revision_combo.setEnabled(pending is not None)

        # #473 routing profile selection (verified channel-map authority).
        previous_profile = self.routing_profile_combo.currentData()
        self.routing_profile_combo.clear()
        self.routing_profile_combo.addItem("（未選択）", None)
        try:
            # #858: only profiles scoped to this exact document are
            # selectable — an unscoped or foreign-project channel map can
            # never stand in for this project's routing authority.
            profiles = self.controller.quality_repository.list_routing_profiles(
                document_id=self.controller.document_id
            )
        except Exception as exc:
            profiles = ()
            self._operation_error_notice(
                "ルーティングプロファイルを読み込めませんでした",
                exc,
                effect=None,
                severity=SemanticState.WARNING,
            )
        for profile in profiles:
            self.routing_profile_combo.addItem(
                f"{profile.profile_name} · {profile.created_at_utc}",
                profile.routing_profile_id,
            )
        if previous_profile is not None:
            index = self.routing_profile_combo.findData(previous_profile)
            if index >= 0:
                self.routing_profile_combo.setCurrentIndex(index)

        # #471 persisted contexts offered as reusable presets.
        self.acquisition_preset_combo.clear()
        self.acquisition_preset_combo.addItem("（プリセットなし）", None)
        try:
            contexts = self.controller.acquisition_contexts()
        except Exception as exc:
            contexts = ()
            self._operation_error_notice(
                "プリセットを読み込めませんでした",
                exc,
                effect=None,
                severity=SemanticState.WARNING,
            )
        for context in contexts:
            self.acquisition_preset_combo.addItem(
                f"{context.source_kind} · {context.created_at_utc}",
                context.acquisition_context_id,
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
            channel_index = self.channel_combo.findData(retake_view.channel_role)
            if channel_index >= 0:
                self.channel_combo.setCurrentIndex(channel_index)
            else:
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
        elif correction_view is not None:
            # Correction pre-fills the *effective* binding so only the wrong
            # fields need changing; unchanged fields stay out of the record.
            index = self.target_combo.findData(correction_view.effective_target_entity_id)
            if index >= 0:
                self.target_combo.setCurrentIndex(index)
            channel_index = self.channel_combo.findData(
                correction_view.effective_channel_role
            )
            if channel_index >= 0:
                self.channel_combo.setCurrentIndex(channel_index)
            else:
                self.channel_combo.setCurrentText(
                    correction_view.effective_channel_role
                )
            index = self.radiation_combo.findData(correction_view.effective_radiation_scope)
            if index >= 0:
                self.radiation_combo.setCurrentIndex(index)
            index = self.routing_combo.findData(correction_view.effective_routing_evidence)
            if index >= 0:
                self.routing_combo.setCurrentIndex(index)
            wanted_sources = set(correction_view.effective_source_speaker_ids)
            for index in range(self.source_speaker_list.count()):
                item = self.source_speaker_list.item(index)
                item.setCheckState(
                    Qt.CheckState.Checked
                    if item.data(Qt.ItemDataRole.UserRole) in wanted_sources
                    else Qt.CheckState.Unchecked
                )
        self._update_assignment_summary()

    def _update_assignment_summary(self, *_args: object) -> None:
        if not hasattr(self, "assignment_summary_label"):
            return
        target = self.target_combo.currentText() or "—"
        channel_data = self.channel_combo.currentData()
        channel = (
            _channel_role_label(str(channel_data))
            if isinstance(channel_data, str) and channel_data
            else (self.channel_combo.currentText() or "不明")
        )
        evidence = self.evidence_combo.currentText() or "—"
        routing = self.routing_combo.currentText() or "—"
        sources = [
            self.source_speaker_list.item(index).text()
            for index in range(self.source_speaker_list.count())
            if self.source_speaker_list.item(index).checkState() == Qt.CheckState.Checked
        ]
        source_text = " / ".join(sources) if sources else "未指定"
        self.assignment_summary_label.setText(
            f"保存内容: {target} · {channel} · {evidence} · 音源 {source_text} · ルーティング {routing}"
        )

    def _acquisition_revision_changed(self) -> None:
        revision_id = self.acquisition_revision_combo.currentData()
        if not isinstance(revision_id, str) or not revision_id:
            return
        try:
            self.controller.select_pending_revision(revision_id)
        except Exception as exc:
            self._operation_error_notice(
                "取得時の配置を切り替えられませんでした",
                exc,
                effect=None,
                severity=SemanticState.WARNING,
            )

    def _apply_acquisition_preset(self) -> None:
        """Seed the capture fields from a persisted context (preset, #471)."""
        context_id = self.acquisition_preset_combo.currentData()
        if not isinstance(context_id, str) or not context_id:
            return
        context = self.controller.quality_repository.get_acquisition_context(
            context_id
        )
        if context is None:
            self._set_notice("プリセットを確認できませんでした。", SemanticState.WARNING)
            return
        mic = context.microphone
        playback = context.playback
        if mic is not None:
            index = self.mic_orientation_combo.findData(
                mic.calibration_profile or "unknown"
            )
            if index >= 0:
                self.mic_orientation_combo.setCurrentIndex(index)
            self.mic_manufacturer_edit.setText(mic.manufacturer or "")
            self.mic_model_edit.setText(mic.model or "")
            self.mic_serial_edit.setText(mic.serial or "")
            self.mic_sample_rate_edit.setText(
                "" if mic.sample_rate_hz is None else str(mic.sample_rate_hz)
            )
            self.mic_cal_file_edit.setText(mic.calibration_filename or "")
            self.mic_cal_sha_edit.setText(mic.calibration_sha256 or "")
        if playback is not None:
            self.output_device_edit.setText(playback.output_device_label or "")
            self.avr_model_edit.setText(
                " ".join(
                    part
                    for part in (playback.avr_manufacturer, playback.avr_model)
                    if part
                )
            )
            self.avr_volume_edit.setText(
                "" if playback.avr_volume_db is None else str(playback.avr_volume_db)
            )
            self.avr_processing_edit.setText(playback.avr_processing_mode or "")
            self.avr_peq_edit.setText(playback.avr_peq_mode or "")

    @staticmethod
    def _text_or_none(edit: QLineEdit) -> str | None:
        text = edit.text().strip()
        return text or None

    def _collect_acquisition_capture(self) -> tuple[AcquisitionCapture | None, Direction3 | None, str | None]:
        """Read the capture form into an AcquisitionCapture + mic direction.

        Returns ``(capture, direction, error)``. An entirely empty form is
        ``(None, None, None)`` — no acquisition context is persisted, which
        stays honest missing evidence rather than a fabricated record.
        """
        orientation = str(self.mic_orientation_combo.currentData())
        direction: Direction3 | None = None
        if orientation == "0deg":
            direction = Direction3(x=0.0, y=0.0, z=1.0)
        elif orientation == "90deg":
            direction = Direction3(x=0.0, y=-1.0, z=0.0)
        mic_rate_text = self.mic_sample_rate_edit.text().strip()
        mic_rate: int | None = None
        if mic_rate_text:
            try:
                mic_rate = int(mic_rate_text)
            except ValueError:
                return None, None, "マイクのサンプルレートは整数で入力してください"
        cal_sha = self._text_or_none(self.mic_cal_sha_edit)
        if cal_sha is not None and (
            len(cal_sha) != 64
            or any(char not in "0123456789abcdef" for char in cal_sha.lower())
        ):
            return None, None, "校正ファイルSHA-256は64桁の16進数で入力してください"
        volume_text = self.avr_volume_edit.text().strip()
        volume_db: float | None = None
        if volume_text:
            try:
                volume_db = float(volume_text)
            except ValueError:
                return None, None, "AVRボリュームは数値（dB）で入力してください"

        microphone = CadMicrophoneCapture(
            manufacturer=self._text_or_none(self.mic_manufacturer_edit),
            model=self._text_or_none(self.mic_model_edit),
            serial=self._text_or_none(self.mic_serial_edit),
            sample_rate_hz=mic_rate,
            calibration_profile=None if orientation == "unknown" else orientation,  # type: ignore[arg-type]
            calibration_filename=self._text_or_none(self.mic_cal_file_edit),
            calibration_sha256=cal_sha,
            direction=direction,
        )
        avr_model_text = self._text_or_none(self.avr_model_edit)
        playback = CadPlaybackCapture(
            output_device_label=self._text_or_none(self.output_device_edit),
            avr_model=avr_model_text,
            avr_volume_db=volume_db,
            avr_processing_mode=self._text_or_none(self.avr_processing_edit),
            avr_peq_mode=self._text_or_none(self.avr_peq_edit),
        )
        has_mic = any(
            (
                microphone.manufacturer,
                microphone.model,
                microphone.serial,
                microphone.sample_rate_hz,
                microphone.calibration_profile not in (None, "unknown"),
                microphone.calibration_filename,
                microphone.calibration_sha256,
                microphone.direction,
            )
        )
        has_playback = any(
            (
                playback.output_device_label,
                playback.avr_model,
                playback.avr_volume_db is not None,
                playback.avr_processing_mode,
                playback.avr_peq_mode,
            )
        )
        if not has_mic and not has_playback:
            return None, direction, None
        return (
            AcquisitionCapture(
                source_kind="manual",
                microphone=microphone if has_mic else None,
                playback=playback if has_playback else None,
                sample_rate_hz=mic_rate,
            ),
            direction,
            None,
        )

    def _commit_assignment(self) -> None:
        scope = self.assignment_scope_combo.currentData()
        if not isinstance(scope, tuple) or not scope:
            self._set_notice(
                "割り当て対象がありません。先にREWデータを読み込んでください。",
                SemanticState.WARNING,
            )
            return
        kind, ref = scope
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
        channel_data = self.channel_combo.currentData()
        channel_role = (
            str(channel_data)
            if isinstance(channel_data, str) and channel_data
            else (self.channel_combo.currentText().strip() or "unknown")
        )
        acquisition, direction, capture_error = self._collect_acquisition_capture()
        if capture_error is not None:
            self._set_notice(capture_error, SemanticState.WARNING)
            return
        routing_profile_id = self.routing_profile_combo.currentData()
        assignment = MeasurementAssignment(
            measurement_entity_id=target_id,
            evidence_type=str(self.evidence_combo.currentData()),  # type: ignore[arg-type]
            channel_role=channel_role,
            source_speaker_ids=source_ids,
            radiation_scope=str(self.radiation_combo.currentData()),  # type: ignore[arg-type]
            routing_evidence=str(self.routing_combo.currentData()),  # type: ignore[arg-type]
            measurement_direction=direction,
            routing_profile_id=(
                routing_profile_id if isinstance(routing_profile_id, str) else None
            ),
            acquisition=acquisition,
        )
        if kind == "correction":
            self._commit_correction(str(ref), assignment)
            return
        if kind in ("batch_all", "batch_item"):
            self._commit_batch_assignment(kind, ref, assignment)
            return
        # #659: when the layout moved since staging, the user chooses how to
        # resolve the divergence — commit to the acquired (historical)
        # revision, re-choose the current head, or keep the import pending.
        on_divergence = "reject"
        if self.controller.pending_divergence():
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("配置が変更されています")
            box.setText(
                "読み込み後に部屋の保存状態が変更されています。\n"
                "この測定をどの配置として保存するか選んでください。"
            )
            historical_button = box.addButton(
                "取得時の配置として保存", QMessageBox.ButtonRole.AcceptRole
            )
            current_button = box.addButton(
                "現在の配置として保存", QMessageBox.ButtonRole.DestructiveRole
            )
            box.addButton("保留にする", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            clicked = box.clickedButton()
            if clicked is historical_button:
                on_divergence = "historical"
            elif clicked is current_button:
                on_divergence = "accept_current"
            else:
                self._set_notice(
                    "保存を保留しました。取得時の配置を選び直すか、後で保存してください。",
                    SemanticState.WARNING,
                )
                return
        pending = self.controller.pending_import
        if (
            pending is not None
            and pending.duplicate_of_measurement_id is not None
        ):
            duplicate_name = next(
                (
                    view.effective_target_name
                    for view in getattr(self, "_quality_views", ())
                    if view.measurement_id == pending.duplicate_of_measurement_id
                ),
                pending.duplicate_of_measurement_id,
            )
            answer = QMessageBox.question(
                self,
                "同じ測定が既に保存されています",
                f"{_duplicate_kind_label(pending.duplicate_kind)}の測定が既に"
                f"保存されています（{duplicate_name}）。\n"
                "新しい測定としてもう一度保存しますか？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self._set_notice(
                    "保存をキャンセルしました。既存の測定はそのままです。",
                    SemanticState.WARNING,
                )
                return
        try:
            record = self.controller.commit_pending(
                assignment,
                on_divergence=on_divergence,  # type: ignore[arg-type]
            )
        except Exception as exc:
            self._operation_error_notice("測定を保存できませんでした", exc)
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
                    reason="測定品質ページからのユーザー指定再測定",
                )
            except Exception as exc:
                # Partial commit: the measurement persisted; the retake
                # lineage did not. Keep the exact mutation outcome (#903).
                self._set_notice(
                    "測定は保存されましたが、再測定の系譜を記録できませんでした"
                    "（保存時と同じ測定点・役割・音源が必要です）"
                    f" · {operation_error_message(exc)}",
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

    def _commit_batch_assignment(
        self,
        kind: str,
        ref: object,
        assignment: MeasurementAssignment,
    ) -> None:
        """Apply this assignment to batch items and save them explicitly (#446).

        The commit runs on the worker pool: hundreds of staged files each pay
        a write-time evidence verification, which would freeze the UI thread
        for tens of seconds. Progress arrives via ``batch_commit_progress``;
        cancellation is cooperative (each finished item's commit stays).
        """
        if self._commit_job_key is not None:
            self._set_notice("保存処理が進行中です。", SemanticState.WARNING)
            return
        try:
            if kind == "batch_all":
                applied = self.controller.apply_batch_assignment(assignment)
                if applied == 0:
                    self._set_notice(
                        "割り当てを適用できるバッチ項目がありません。",
                        SemanticState.WARNING,
                    )
                    return
                item_ids: list[str] | None = None
            else:
                self.controller.set_batch_item_assignment(str(ref), assignment)
                item_ids = [str(ref)]
        except Exception as exc:
            self._operation_error_notice("バッチを保存できませんでした", exc)
            return
        self._set_batch_committing(True)
        self._set_notice("バッチを保存しています…", None)
        key = uuid4().hex
        self._commit_job_key = key
        self._job_handlers[key] = (
            self._batch_commit_finished,
            "バッチを保存できませんでした",
            None,
        )
        self._job_pool.start(
            key,
            lambda cancel_event: self.controller.commit_batch(
                item_ids,
                cancel_event=cancel_event,
                progress=self.batch_commit_progress.emit,
            ),
            self._job_completed,
        )

    def _set_batch_committing(self, running: bool) -> None:
        self.batch_add_button.setEnabled(not running)
        self.batch_attach_button.setEnabled(not running)
        self.batch_clear_button.setEnabled(not running)
        self.batch_cancel_button.setVisible(running)
        self.batch_cancel_button.setEnabled(running)
        if running:
            # The assignment save path launches the commit — block a second
            # one while this job is live (refresh restores the right state).
            self.assignment_save_button.setEnabled(False)

    def _cancel_batch_commit(self) -> None:
        key = self._commit_job_key
        if key is not None:
            self._set_notice("保存をキャンセルしています…", None)
            self._job_pool.cancel(key)

    @Slot(int, int)
    def _on_batch_commit_progress(self, done: int, total: int) -> None:
        self._set_notice(f"バッチを保存しています… {done}/{total}", None)

    def _batch_commit_finished(self, value: object) -> None:
        outcomes = value if isinstance(value, tuple) else ()
        committed = sum(1 for o in outcomes if o.outcome == "committed")
        reused = sum(1 for o in outcomes if o.outcome == "reused")
        failed = sum(1 for o in outcomes if o.outcome == "failed")
        skipped = sum(1 for o in outcomes if o.outcome == "skipped")
        parts = [f"{committed} 件を保存"]
        if reused:
            parts.append(f"{reused} 件は既存測定を利用")
        if failed:
            parts.append(f"{failed} 件失敗")
        if skipped:
            parts.append(f"{skipped} 件未保存")
        self._set_notice(
            "、".join(parts) + "。失敗・未保存の項目は一覧に残っています。",
            SemanticState.SUCCESS if not failed else SemanticState.WARNING,
        )

    def _commit_correction(
        self,
        measurement_id: str,
        assignment: MeasurementAssignment,
    ) -> None:
        """Append an assignment correction to a persisted measurement (#509)."""
        view = next(
            (
                view
                for view in self._quality_views
                if view.measurement_id == measurement_id
            ),
            None,
        )
        if view is None:
            self._set_notice("訂正対象の測定が見つかりません", SemanticState.ERROR)
            self._correction_target_id = None
            self.refresh()
            return
        corrected = AssignmentCorrection(
            measurement_entity_id=(
                assignment.measurement_entity_id
                if assignment.measurement_entity_id != view.effective_target_entity_id
                else None
            ),
            channel_role=(
                assignment.channel_role
                if assignment.channel_role != view.effective_channel_role
                else None
            ),
            source_speaker_ids=(
                assignment.source_speaker_ids
                if assignment.source_speaker_ids != view.effective_source_speaker_ids
                else None
            ),
            radiation_scope=(
                assignment.radiation_scope
                if assignment.radiation_scope != view.effective_radiation_scope
                else None
            ),
            routing_evidence=(
                assignment.routing_evidence
                if assignment.routing_evidence != view.effective_routing_evidence
                else None
            ),
        )
        if all(
            value is None
            for value in (
                corrected.measurement_entity_id,
                corrected.channel_role,
                corrected.source_speaker_ids,
                corrected.radiation_scope,
                corrected.routing_evidence,
            )
        ):
            self._set_notice(
                "割り当てが現在の内容と同じです。訂正する項目を変更してください。",
                SemanticState.WARNING,
            )
            return
        reason, ok = QInputDialog.getText(
            self,
            "割り当ての訂正",
            "訂正の理由を記録してください（監査ログに残ります）:",
        )
        if not ok or not reason.strip():
            self._set_notice("訂正の理由が必要です", SemanticState.WARNING)
            return
        try:
            self.controller.correct_assignment(measurement_id, corrected, reason)
        except Exception as exc:
            self._operation_error_notice("訂正を記録できませんでした", exc)
            return
        self._correction_target_id = None
        self._set_notice(
            "割り当ての訂正を記録しました。元の測定データは変更されていません。",
            SemanticState.SUCCESS,
        )
        self.refresh()

    # ------------------------------------------------------------------
    # Campaign runner page (#529)

    def _build_campaign_page(self) -> None:
        page, host, layout = _page(
            "測定キャンペーン",
            "音源×測定位置×リピートの計画をガイド付きで実行します。REWが取得エンジンです。",
        )
        page.setObjectName("measurementCampaignPage")

        plan_card, plan_layout = _card("計画", host)
        plan_row = QHBoxLayout()
        self.campaign_plan_combo = QComboBox(plan_card)
        plan_row.addWidget(self.campaign_plan_combo, 1)
        self.campaign_open_button = QPushButton("実行を開始 / 再開", plan_card)
        set_primary_action(self.campaign_open_button)
        self.campaign_open_button.clicked.connect(self._open_campaign_run)
        plan_row.addWidget(self.campaign_open_button)
        plan_layout.addLayout(plan_row)

        builder_row = QHBoxLayout()
        sources_column = QVBoxLayout()
        sources_column.addWidget(QLabel("音源", plan_card))
        self.campaign_source_list = QListWidget(plan_card)
        self.campaign_source_list.setMinimumHeight(110)
        self.campaign_source_list.setMaximumHeight(170)
        self.campaign_source_list.itemChanged.connect(
            self._update_campaign_preview
        )
        sources_column.addWidget(self.campaign_source_list)
        builder_row.addLayout(sources_column, 1)
        targets_column = QVBoxLayout()
        targets_column.addWidget(QLabel("測定位置", plan_card))
        self.campaign_target_list = QListWidget(plan_card)
        self.campaign_target_list.setMinimumHeight(110)
        self.campaign_target_list.setMaximumHeight(170)
        self.campaign_target_list.itemChanged.connect(
            self._update_campaign_preview
        )
        targets_column.addWidget(self.campaign_target_list)
        builder_row.addLayout(targets_column, 1)
        plan_layout.addLayout(builder_row)

        option_row = QHBoxLayout()
        option_row.addWidget(QLabel("目的", plan_card))
        self.campaign_purpose_combo = QComboBox(plan_card)
        for value, label in (
            ('measurement', "測定"),
            ('calibration', "キャリブレーション"),
            ('holdout', "ホールドアウト"),
            ('diagnostic', "診断"),
        ):
            self.campaign_purpose_combo.addItem(label, value)
        self.campaign_purpose_combo.currentIndexChanged.connect(
            lambda _: self._update_campaign_preview()
        )
        option_row.addWidget(self.campaign_purpose_combo)
        self.campaign_repeat = QSpinBox(plan_card)
        self.campaign_repeat.setRange(1, 16)
        self.campaign_repeat.setValue(1)
        self.campaign_repeat.setPrefix("リピート ")
        self.campaign_repeat.valueChanged.connect(
            lambda _: self._update_campaign_preview()
        )
        option_row.addWidget(self.campaign_repeat)
        option_row.addWidget(QLabel("ターゲットパターン", plan_card))
        self.campaign_pattern_combo = QComboBox(plan_card)
        option_row.addWidget(self.campaign_pattern_combo, 1)
        self.campaign_apply_pattern_button = QPushButton(
            "パターンを適用", plan_card
        )
        self.campaign_apply_pattern_button.clicked.connect(
            self._apply_campaign_pattern
        )
        option_row.addWidget(self.campaign_apply_pattern_button)
        plan_layout.addLayout(option_row)

        action_row = QHBoxLayout()
        self.campaign_all_button = QPushButton(
            "全スピーカー×全測定位置", plan_card
        )
        self.campaign_all_button.clicked.connect(
            self._select_all_campaign_sources_targets
        )
        action_row.addWidget(self.campaign_all_button)
        self.campaign_preview_label = QLabel("", plan_card)
        action_row.addWidget(self.campaign_preview_label, 1)
        self.campaign_create_button = QPushButton("計画を作成", plan_card)
        self.campaign_create_button.clicked.connect(self._create_campaign_plan)
        action_row.addWidget(self.campaign_create_button)
        plan_layout.addLayout(action_row)
        layout.addWidget(plan_card)

        variant_card, variant_layout = _card("登録済みの詳細計画", host)
        variant_hint = QLabel(
            "SystemVariant/検証ワークフローが登録した測定計画を、"
            "そのまま実行用セルとして開きます（新しい全×全計画は作りません）。",
            variant_card,
        )
        variant_hint.setWordWrap(True)
        variant_layout.addWidget(variant_hint)
        variant_row = QHBoxLayout()
        self.campaign_variant_combo = QComboBox(variant_card)
        variant_row.addWidget(self.campaign_variant_combo, 1)
        self.campaign_variant_button = QPushButton(
            "この計画を実行", variant_card
        )
        self.campaign_variant_button.clicked.connect(self._open_variant_plan)
        variant_row.addWidget(self.campaign_variant_button)
        variant_layout.addLayout(variant_row)
        layout.addWidget(variant_card)

        matrix_card, matrix_layout = _card("計画セル", host)
        self.campaign_table = QTableWidget(0, 5, matrix_card)
        self.campaign_table.setHorizontalHeaderLabels(
            ["入力役割", "測定位置", "リピート", "状態", "測定"]
        )
        self.campaign_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.campaign_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.campaign_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.campaign_table.verticalHeader().setVisible(False)
        self.campaign_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.campaign_table.horizontalHeader().setStretchLastSection(True)
        self.campaign_table.itemSelectionChanged.connect(
            self._campaign_selection_changed
        )
        self.campaign_table.setMinimumHeight(220)
        matrix_layout.addWidget(self.campaign_table)
        layout.addWidget(matrix_card)

        step_card, step_layout = _card("現在のステップ", host)
        self.campaign_step_label = QLabel("キャンペーンを開いてください", step_card)
        self.campaign_step_label.setWordWrap(True)
        step_layout.addWidget(self.campaign_step_label)
        self.campaign_progress_label = QLabel("", step_card)
        self.campaign_progress_label.setWordWrap(True)
        step_layout.addWidget(self.campaign_progress_label)

        commit_row = QHBoxLayout()
        self.campaign_measurement_combo = QComboBox(step_card)
        commit_row.addWidget(self.campaign_measurement_combo, 1)
        self.campaign_commit_button = QPushButton("このセルに登録", step_card)
        self.campaign_commit_button.clicked.connect(self._commit_campaign_cell)
        commit_row.addWidget(self.campaign_commit_button)
        self.campaign_skip_button = QPushButton("スキップ", step_card)
        self.campaign_skip_button.clicked.connect(self._skip_campaign_cell)
        commit_row.addWidget(self.campaign_skip_button)
        step_layout.addLayout(commit_row)

        next_row = QHBoxLayout()
        next_row.addStretch(1)
        self.campaign_next_button = QPushButton("次の未完了セルへ", step_card)
        self.campaign_next_button.clicked.connect(self._select_next_campaign_cell)
        next_row.addWidget(self.campaign_next_button)
        step_layout.addLayout(next_row)
        layout.addWidget(step_card)
        layout.addStretch(1)
        self.pages.addWidget(page)

        self._campaign_run_id: str | None = None

    def _campaign_target_names(self) -> dict[str, str]:
        try:
            return {
                target.entity_id: target.name
                for target in self.controller.assignment_targets()
            }
        except Exception as exc:
            log_operation_error(
                to_user_facing_error(exc, title="対象名を読み込めませんでした"),
                exc,
            )
            return {}

    def _campaign_speaker_labels(self) -> dict[str, str]:
        """entity_id -> human speaker label (#578).

        Sources are shown as `name（role）`; a speaker with no human name
        keeps its entity id — the only remaining non-human identity.
        """

        try:
            labels: dict[str, str] = {}
            for speaker in self.controller.source_speakers():
                name = speaker.name or speaker.entity_id
                if speaker.role and speaker.role != speaker.name:
                    labels[speaker.entity_id] = f'{name}（{speaker.role}）'
                else:
                    labels[speaker.entity_id] = name
            return labels
        except Exception:
            return {}

    def _measurement_display_labels(
        self,
        views: tuple[MeasurementView, ...],
    ) -> dict[str, str]:
        """measurement_id -> human label for the campaign table (#578)."""

        labels: dict[str, str] = {}
        for row in views:
            labels[row.measurement_id] = (
                f"{_channel_role_label(row.channel_role)} · {row.target_name} · "
                f"{_evidence_label(row.evidence_type)}"
            )
        return labels

    def _refresh_campaign(
        self,
        views: tuple[MeasurementView, ...] | None = None,
    ) -> None:
        if views is None:
            views = self.controller.measurement_views()
        plans = self.controller.runner_plans()
        plan_created = self.controller.runner_plan_created_at_utc()
        previous = self.campaign_plan_combo.currentData()
        self.campaign_plan_combo.blockSignals(True)
        self.campaign_plan_combo.clear()
        for plan in plans:
            created = plan_created.get(plan.plan_id)
            plan_label = (
                saved_label(created) if created else '測定計画'
            )
            self.campaign_plan_combo.addItem(
                f"{self.controller.runner_plan_summary(plan)}（{plan_label}）",
                plan.plan_id,
            )
        if previous is not None:
            index = self.campaign_plan_combo.findData(previous)
            if index >= 0:
                self.campaign_plan_combo.setCurrentIndex(index)
        self.campaign_plan_combo.blockSignals(False)

        self._refresh_campaign_builder()

        names = self._campaign_target_names()
        measurement_labels = self._measurement_display_labels(views)
        self.campaign_measurement_combo.clear()
        for row in views:
            if row.dataset_id is None:
                continue
            self.campaign_measurement_combo.addItem(
                measurement_labels[row.measurement_id],
                row.measurement_id,
            )

        if self._campaign_run_id is None or self.controller.runner_repository.get_run(
            self._campaign_run_id
        ) is None:
            self.campaign_table.setRowCount(0)
            self.campaign_step_label.setText("キャンペーンを開いてください")
            self.campaign_progress_label.setText("")
            return

        plan = self.controller.runner_plan_for_run(self._campaign_run_id)
        states = self.controller.runner_cell_states(self._campaign_run_id)
        self.campaign_table.setRowCount(len(plan.cells))
        for cell in plan.cells:
            state = states[cell.cell_index]
            values = (
                _channel_role_label(cell.channel_role),
                names.get(cell.target_entity_id, cell.target_entity_id),
                str(cell.repeat_index + 1),
                _CELL_STATUS_LABELS[state.status],
                (
                    measurement_labels.get(state.measurement_id)
                    if state.measurement_id
                    else None
                )
                or "—",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, cell.cell_index)
                self.campaign_table.setItem(cell.cell_index, column, item)

        progress = self.controller.runner_progress(self._campaign_run_id)
        self.campaign_progress_label.setText(
            f"完了 {progress.completed} / 必要 {progress.total} · "
            f"未着手 {progress.not_started} · 品質確認待ち {progress.quality_pending} · "
            f"要再測定 {progress.retake_required} · スキップ {progress.skipped}"
        )
        next_index = self.controller.runner_next_incomplete(self._campaign_run_id)
        step = self.controller.runner_guided_step(self._campaign_run_id)
        speaker_labels = self._campaign_speaker_labels()
        if step is None:
            self.campaign_step_label.setText("計画された全セルが完了しました")
        else:
            self.campaign_step_label.setText(
                f"次: {_channel_role_label(step.channel_role)} · "
                f"{names.get(step.target_entity_id, step.target_entity_id)} · "
                f"リピート {step.repeat_index + 1} · 音源 "
                + ' / '.join(
                    speaker_labels.get(speaker_id, speaker_id)
                    for speaker_id in step.source_speaker_ids
                )
            )
        if next_index is not None:
            self.campaign_table.selectRow(next_index)

    def _checked_campaign_sources(
        self,
    ) -> tuple[tuple[str, tuple[str, ...]], ...]:
        sources = []
        for index in range(self.campaign_source_list.count()):
            item = self.campaign_source_list.item(index)
            option = item.data(Qt.ItemDataRole.UserRole)
            if (
                option is not None
                and item.checkState() == Qt.CheckState.Checked
            ):
                sources.append((option.channel_role, option.speaker_entity_ids))
        return tuple(sources)

    def _checked_campaign_targets(self) -> tuple[str, ...]:
        entity_ids = []
        for index in range(self.campaign_target_list.count()):
            item = self.campaign_target_list.item(index)
            entity_id = item.data(Qt.ItemDataRole.UserRole)
            if (
                entity_id is not None
                and item.checkState() == Qt.CheckState.Checked
            ):
                entity_ids.append(entity_id)
        return tuple(entity_ids)

    def _selected_campaign_purposes(self) -> tuple:
        purpose = self.campaign_purpose_combo.currentData()
        return (purpose,) if purpose else ('measurement',)

    def _update_campaign_preview(self) -> None:
        try:
            preview = self.controller.preview_runner_plan(
                sources=self._checked_campaign_sources() or None,
                target_entity_ids=self._checked_campaign_targets() or None,
                repeat_count=int(self.campaign_repeat.value()),
                purposes=self._selected_campaign_purposes(),
            )
        except Exception as exc:
            self.campaign_preview_label.setText(operation_error_message(exc))
            return
        self.campaign_preview_label.setText(
            f"{preview.cell_count} セル = 音源 {preview.source_count} × "
            f"測定位置 {preview.target_count} × {preview.repeat_count} 回"
            + (
                f" × 目的 {len(preview.purposes)}"
                if len(preview.purposes) > 1
                else ""
            )
        )

    def _rebuild_campaign_checklist(
        self,
        list_widget: QListWidget,
        entries: tuple[tuple[str, Any], ...],
    ) -> None:
        """Repopulate a checkable list, keeping the user's check states."""
        checked = {
            list_widget.item(index).data(Qt.ItemDataRole.UserRole)
            for index in range(list_widget.count())
            if list_widget.item(index).checkState() == Qt.CheckState.Checked
        }
        list_widget.blockSignals(True)
        list_widget.clear()
        for label, payload in entries:
            item = QListWidgetItem(label, list_widget)
            item.setData(Qt.ItemDataRole.UserRole, payload)
            item.setFlags(
                item.flags()
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsEnabled
            )
            item.setCheckState(
                Qt.CheckState.Checked
                if payload in checked
                else Qt.CheckState.Unchecked
            )
        list_widget.blockSignals(False)

    def _refresh_campaign_builder(self) -> None:
        speaker_labels = self._campaign_speaker_labels()
        try:
            options = self.controller.runner_source_options()
        except Exception:
            options = ()
        source_entries = []
        for option in options:
            names = [
                speaker_labels.get(entity_id, entity_id)
                for entity_id in option.speaker_entity_ids
            ]
            if option.grouped:
                label = (
                    f"{_channel_role_label(option.channel_role)} グループ"
                    f"（{' / '.join(names)}）"
                )
            else:
                label = names[0] if names else option.speaker_entity_ids[0]
            source_entries.append((label, option))
        first_population = self.campaign_source_list.count() == 0
        self._rebuild_campaign_checklist(
            self.campaign_source_list, tuple(source_entries)
        )
        # First population defaults to the simple preset: every speaker.
        if first_population:
            self._select_all_campaign_sources_targets()

        try:
            targets = self.controller.assignment_targets()
        except Exception:
            targets = ()
        first_population = self.campaign_target_list.count() == 0
        self._rebuild_campaign_checklist(
            self.campaign_target_list,
            tuple(
                (target.name or target.entity_id, target.entity_id)
                for target in targets
            ),
        )
        if first_population:
            for index in range(self.campaign_target_list.count()):
                self.campaign_target_list.item(index).setCheckState(
                    Qt.CheckState.Checked
                )

        try:
            patterns = self.controller.target_patterns()
        except Exception:
            patterns = ()
        target_names = self._campaign_target_names()
        self.campaign_pattern_combo.clear()
        for pattern in patterns:
            anchor_name = target_names.get(
                pattern.anchor_entity_id, "明示位置"
            )
            self.campaign_pattern_combo.addItem(
                f"{anchor_name} 起点 · {len(pattern.offsets)} 点"
                f" · v{pattern.pattern_version}",
                pattern.pattern_id,
            )
        self.campaign_apply_pattern_button.setEnabled(
            self.campaign_pattern_combo.count() > 0
        )

        try:
            variant_plans = self.controller.variant_measurement_plans()
        except Exception:
            variant_plans = ()
        previous_variant = self.campaign_variant_combo.currentData()
        self.campaign_variant_combo.blockSignals(True)
        self.campaign_variant_combo.clear()
        for plan in variant_plans:
            purpose_label = _VARIANT_PURPOSE_LABELS.get(
                plan.purpose, plan.purpose or '測定計画'
            )
            self.campaign_variant_combo.addItem(
                f"{purpose_label} · {len(plan.targets)} 対象"
                f" · {saved_label(plan.created_at_utc)}",
                plan.plan_id,
            )
        if previous_variant is not None:
            index = self.campaign_variant_combo.findData(previous_variant)
            if index >= 0:
                self.campaign_variant_combo.setCurrentIndex(index)
        self.campaign_variant_combo.blockSignals(False)
        self.campaign_variant_button.setEnabled(
            self.campaign_variant_combo.count() > 0
        )
        self._update_campaign_preview()

    def _select_all_campaign_sources_targets(self) -> None:
        """Explicit convenience preset: every speaker x every target."""
        self.campaign_source_list.blockSignals(True)
        for index in range(self.campaign_source_list.count()):
            item = self.campaign_source_list.item(index)
            option = item.data(Qt.ItemDataRole.UserRole)
            item.setCheckState(
                Qt.CheckState.Checked
                if option is not None and not option.grouped
                else Qt.CheckState.Unchecked
            )
        self.campaign_source_list.blockSignals(False)
        self.campaign_target_list.blockSignals(True)
        for index in range(self.campaign_target_list.count()):
            self.campaign_target_list.item(index).setCheckState(
                Qt.CheckState.Checked
            )
        self.campaign_target_list.blockSignals(False)
        self._update_campaign_preview()

    def _apply_campaign_pattern(self) -> None:
        pattern_id = self.campaign_pattern_combo.currentData()
        if not isinstance(pattern_id, str) or not pattern_id:
            return
        try:
            entity_ids = set(
                self.controller.target_pattern_entity_ids(pattern_id)
            )
        except Exception as exc:
            self._set_notice(
                f"パターンを適用できませんでした · {operation_error_message(exc)}",
                SemanticState.ERROR,
            )
            return
        if not entity_ids:
            self._set_notice(
                "パターンの測定点は現在の部屋に存在しません",
                SemanticState.WARNING,
            )
            return
        self.campaign_target_list.blockSignals(True)
        for index in range(self.campaign_target_list.count()):
            item = self.campaign_target_list.item(index)
            entity_id = item.data(Qt.ItemDataRole.UserRole)
            if entity_id in entity_ids:
                item.setCheckState(Qt.CheckState.Checked)
        self.campaign_target_list.blockSignals(False)
        self._update_campaign_preview()

    def _open_variant_plan(self) -> None:
        plan_id = self.campaign_variant_combo.currentData()
        if not isinstance(plan_id, str) or not plan_id:
            self._set_notice(
                "実行する登録済み計画を選択してください。",
                SemanticState.WARNING,
            )
            return
        try:
            plan = self.controller.create_runner_plan_from_variant_plan(
                plan_id
            )
        except Exception as exc:
            self._set_notice(
                f"計画を開けませんでした · {operation_error_message(exc)}", SemanticState.ERROR
            )
            return
        self._set_notice(
            f"{len(plan.cells)} セルの計画を用意しました。"
            "「実行を開始 / 再開」で開始します。",
            SemanticState.SUCCESS,
        )
        self.refresh()
        index = self.campaign_plan_combo.findData(plan.plan_id)
        if index >= 0:
            self.campaign_plan_combo.setCurrentIndex(index)

    def _create_campaign_plan(self) -> None:
        try:
            plan = self.controller.create_runner_plan(
                sources=self._checked_campaign_sources() or None,
                target_entity_ids=self._checked_campaign_targets() or None,
                repeat_count=int(self.campaign_repeat.value()),
                purposes=self._selected_campaign_purposes(),
            )
        except Exception as exc:
            self._operation_error_notice("計画を作成できませんでした", exc)
            return
        self._set_notice(
            f"{len(plan.cells)} セルの計画を作成しました。「実行を開始 / 再開」で開始します。",
            SemanticState.SUCCESS,
        )
        self.refresh()

    def _open_campaign_run(self) -> None:
        plan_id = self.campaign_plan_combo.currentData()
        if not isinstance(plan_id, str) or not plan_id:
            self._set_notice("先に計画を作成してください。", SemanticState.WARNING)
            return
        run = self.controller.open_runner(plan_id)
        self._campaign_run_id = run.run_id
        self._set_notice(
            "キャンペーンを開きました。次の未完了セルに従ってREWで取得してください。",
            None,
        )
        self._refresh_campaign()

    def _campaign_selection_changed(self) -> None:
        row_index = self.campaign_table.currentRow()
        if row_index < 0 or self._campaign_run_id is None:
            return
        cell_index_item = self.campaign_table.item(row_index, 0)
        if cell_index_item is None:
            return
        cell_index = cell_index_item.data(Qt.ItemDataRole.UserRole)
        try:
            step = self.controller.runner_guided_step(
                self._campaign_run_id, int(cell_index)
            )
        except Exception as exc:
            log_operation_error(
                to_user_facing_error(exc, title="計画ステップを読み込めませんでした"),
                exc,
            )
            return
        if step is None:
            return
        names = self._campaign_target_names()
        speaker_labels = self._campaign_speaker_labels()
        self.campaign_step_label.setText(
            f"選択: {_channel_role_label(step.channel_role)} · "
            f"{names.get(step.target_entity_id, step.target_entity_id)} · "
            f"リピート {step.repeat_index + 1} · 音源 "
            + ' / '.join(
                speaker_labels.get(speaker_id, speaker_id)
                for speaker_id in step.source_speaker_ids
            )
            + (f" · {step.notes}" if step.notes else "")
        )

    def _selected_campaign_cell(self) -> int | None:
        row_index = self.campaign_table.currentRow()
        if row_index < 0:
            return None
        item = self.campaign_table.item(row_index, 0)
        if item is None:
            return None
        value = item.data(Qt.ItemDataRole.UserRole)
        return int(value) if value is not None else None

    def _commit_campaign_cell(self) -> None:
        cell_index = self._selected_campaign_cell()
        measurement_id = self.campaign_measurement_combo.currentData()
        if self._campaign_run_id is None or cell_index is None:
            self._set_notice("先にセルを選択してください。", SemanticState.WARNING)
            return
        if not isinstance(measurement_id, str) or not measurement_id:
            self._set_notice(
                "登録する保存済み測定を選択してください。", SemanticState.WARNING
            )
            return
        try:
            self.controller.runner_commit_cell(
                self._campaign_run_id, cell_index, measurement_id
            )
        except Exception as exc:
            self._operation_error_notice("セルへ登録できませんでした", exc)
            return
        self._set_notice("計画セルに測定を登録しました。", SemanticState.SUCCESS)
        self._refresh_campaign()

    def _skip_campaign_cell(self) -> None:
        cell_index = self._selected_campaign_cell()
        if self._campaign_run_id is None or cell_index is None:
            self._set_notice("先にセルを選択してください。", SemanticState.WARNING)
            return
        try:
            self.controller.runner_skip_cell(self._campaign_run_id, cell_index)
        except Exception as exc:
            self._operation_error_notice("スキップできませんでした", exc)
            return
        self._refresh_campaign()

    def _select_next_campaign_cell(self) -> None:
        if self._campaign_run_id is None:
            return
        next_index = self.controller.runner_next_incomplete(self._campaign_run_id)
        if next_index is None:
            self._set_notice("未完了のセルはありません。", SemanticState.SUCCESS)
            return
        self.campaign_table.selectRow(next_index)

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
        self.quality_table = QTableWidget(0, 10, table_card)
        self.quality_table.setHorizontalHeaderLabels(
            ["入力", "証拠", "測定位置", "品質", "位相", "共通タイミング", "配置", "帯域", "状態", "再測定"]
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

        detail_card, detail_layout = _card("選択した測定", host)
        self.quality_detail = QLabel("測定を選択してください", detail_card)
        self.quality_detail.setWordWrap(True)
        detail_layout.addWidget(self.quality_detail)

        analysis_row = QHBoxLayout()
        smoothing_label = QLabel("表示スムージング", detail_card)
        set_typography_role(smoothing_label, TypographyRole.SECONDARY)
        analysis_row.addWidget(smoothing_label)
        self.quality_smoothing_combo = QComboBox(detail_card)
        for label, fraction in DISPLAY_SMOOTHING_FRACTIONS:
            self.quality_smoothing_combo.addItem(label, fraction)
        self.quality_smoothing_combo.currentIndexChanged.connect(
            lambda _i: self._refresh_quality_plot()
        )
        analysis_row.addWidget(self.quality_smoothing_combo)
        target_label = QLabel("ターゲットカーブ", detail_card)
        set_typography_role(target_label, TypographyRole.SECONDARY)
        analysis_row.addWidget(target_label)
        self.quality_target_combo = QComboBox(detail_card)
        self.quality_target_combo.addItem("オーバーレイなし", None)
        self.quality_target_combo.currentIndexChanged.connect(
            lambda _i: self._refresh_quality_plot()
        )
        analysis_row.addWidget(self.quality_target_combo)
        analysis_row.addStretch(1)
        detail_layout.addLayout(analysis_row)

        self.provenance_label = QLabel("", detail_card)
        self.provenance_label.setWordWrap(True)
        set_typography_role(self.provenance_label, TypographyRole.SECONDARY)
        detail_layout.addWidget(self.provenance_label)

        self.quality_plot = pg.PlotWidget(detail_card)
        self.quality_plot.setMinimumHeight(260)
        self.quality_plot.setLabel("bottom", "周波数", units="Hz")
        self.quality_plot.setLabel("left", "レベル", units="dB")
        _set_plot_appearance(self.quality_plot)
        add_scientific_legend(self.quality_plot)
        detail_layout.addWidget(self.quality_plot)

        phase_row = QHBoxLayout()
        self.phase_state_label = QLabel("", detail_card)
        self.phase_state_label.setWordWrap(True)
        phase_row.addWidget(self.phase_state_label, 1)
        self.phase_unwrap_check = QCheckBox("位相をアンラップ表示", detail_card)
        self.phase_unwrap_check.toggled.connect(
            lambda _v: self._refresh_quality_plot()
        )
        phase_row.addWidget(self.phase_unwrap_check)
        detail_layout.addLayout(phase_row)

        self.phase_plot = pg.PlotWidget(detail_card)
        self.phase_plot.setMinimumHeight(180)
        self.phase_plot.setLabel("bottom", "周波数", units="Hz")
        self.phase_plot.setLabel("left", "位相", units="deg")
        _set_plot_appearance(self.phase_plot)
        # Magnitude/phase share frequency navigation (#579 §8).
        link_x_axis(self.quality_plot, self.phase_plot)
        detail_layout.addWidget(self.phase_plot)

        # Synchronized table + detail split (#586): row selection and its
        # plot/detail stay adjacent instead of separated by a long scroll.
        quality_split = QSplitter(Qt.Orientation.Horizontal, host)
        quality_split.addWidget(table_card)
        quality_split.addWidget(detail_card)
        quality_split.setStretchFactor(0, 1)
        quality_split.setStretchFactor(1, 1)
        layout.addWidget(quality_split)

        spatial_card, spatial_layout = _card("空間コンテキスト", host)
        self.spatial_summary = QLabel("測定を選択してください", spatial_card)
        self.spatial_summary.setWordWrap(True)
        spatial_layout.addWidget(self.spatial_summary)
        spatial_row = QHBoxLayout()
        self.spatial_mode_combo = QComboBox(spatial_card)
        self.spatial_mode_combo.addItem("測定時の配置", "bound")
        self.spatial_mode_combo.addItem("現在との差分", "diff")
        self.spatial_mode_combo.currentIndexChanged.connect(
            lambda _i: self._render_spatial()
        )
        spatial_row.addWidget(self.spatial_mode_combo)
        spatial_row.addStretch(1)
        self.open_bound_room_button = QPushButton("測定時の部屋を開く", spatial_card)
        self.open_bound_room_button.clicked.connect(self._open_bound_room)
        spatial_row.addWidget(self.open_bound_room_button)
        spatial_layout.addLayout(spatial_row)
        self.spatial_viewport_holder = QVBoxLayout()
        spatial_layout.addLayout(self.spatial_viewport_holder)
        self.spatial_fallback_label = QLabel("", spatial_card)
        self.spatial_fallback_label.setWordWrap(True)
        set_typography_role(self.spatial_fallback_label, TypographyRole.SECONDARY)
        spatial_layout.addWidget(self.spatial_fallback_label)
        layout.addWidget(spatial_card)

        lifecycle_card, lifecycle_layout = _card("ライフサイクルと添付", host)
        self.disposition_label = QLabel("測定を選択してください", lifecycle_card)
        self.disposition_label.setWordWrap(True)
        lifecycle_layout.addWidget(self.disposition_label)
        disposition_row = QHBoxLayout()
        self.disposition_combo = QComboBox(lifecycle_card)
        for label, value in (
            ("有効に戻す", "active"),
            ("通常利用から除外", "excluded_from_normal_use"),
            ("誤割当として記録", "misassigned"),
            ("テスト測定として記録", "test_only"),
            ("重複取り込みとして記録", "duplicate_import"),
        ):
            self.disposition_combo.addItem(label, value)
        disposition_row.addWidget(self.disposition_combo)
        self.disposition_apply_button = QPushButton("状態を記録", lifecycle_card)
        self.disposition_apply_button.clicked.connect(self._apply_disposition)
        disposition_row.addWidget(self.disposition_apply_button)
        self.correct_button = QPushButton("割り当てを訂正…", lifecycle_card)
        self.correct_button.clicked.connect(self._start_correction)
        disposition_row.addWidget(self.correct_button)
        disposition_row.addStretch(1)
        lifecycle_layout.addLayout(disposition_row)
        attach_row = QHBoxLayout()
        self.quality_attach_kind_combo = QComboBox(lifecycle_card)
        for kind in MEASUREMENT_ATTACHMENT_KINDS:
            self.quality_attach_kind_combo.addItem(_attachment_kind_label(kind), kind)
        attach_row.addWidget(self.quality_attach_kind_combo)
        self.attach_button = QPushButton("ソース添付を追加", lifecycle_card)
        self.attach_button.clicked.connect(self._attach_to_measurement)
        attach_row.addWidget(self.attach_button)
        attach_row.addStretch(1)
        lifecycle_layout.addLayout(attach_row)
        self.attachments_label = QLabel("", lifecycle_card)
        self.attachments_label.setWordWrap(True)
        set_typography_role(self.attachments_label, TypographyRole.SECONDARY)
        lifecycle_layout.addWidget(self.attachments_label)
        layout.addWidget(lifecycle_card)

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

    def _refresh_quality(
        self,
        views: tuple[MeasurementView, ...] | None = None,
    ) -> None:
        if views is None:
            views = self.controller.measurement_views()
        self._quality_views = views
        selected_id = None
        selected_items = self.quality_table.selectedItems()
        if selected_items:
            selected_id = selected_items[0].data(Qt.ItemDataRole.UserRole)

        self._quality_views = views
        self.quality_table.setRowCount(len(views))
        for row_index, row in enumerate(views):
            values = (
                _channel_role_label(row.effective_channel_role),
                _evidence_label(row.evidence_type),
                row.effective_target_name,
                "検証エラー" if row.dataset_error else _quality_label(row.quality_status),
                _phase_label(row.phase_status),
                _capability_decision_label(row.common_timing_capability),
                "現在の配置" if row.scene_matches_current else "測定時の配置",
                _format_band(row.frequency_band_hz),
                _disposition_label(row.disposition),
                _retake_recommendation_label(row.retake_recommendation),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, row.measurement_id)
                self.quality_table.setItem(row_index, column, item)
            if selected_id == row.measurement_id:
                self.quality_table.selectRow(row_index)
                self._show_quality_row(row_index)

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
            self._update_context_label()
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
            " / ".join(row.effective_source_speaker_ids)
            if row.effective_source_speaker_ids
            else "未指定"
        )
        detail_lines: list[str] = []
        if row.dataset_error:
            detail_lines.append(
                "この測定の周波数応答データを検証できませんでした"
                f"（{row.dataset_error}）。比較・解析からは除外されています。"
            )
        detail_lines.append(
            f"{row.effective_target_name} · {_evidence_label(row.evidence_type)} · "
            f"{_channel_role_label(row.effective_channel_role)}\n"
            f"品質: {_quality_label(row.quality_status)} · {reasons}\n"
            f"{_phase_label(row.phase_status)} · "
            f"位相応答: {_capability_decision_label(row.phase_response_capability)} · "
            f"共通タイミング: {_capability_decision_label(row.common_timing_capability)} · "
            f"{scene}\n"
            f"音源: {source_speakers} · {captured}"
        )
        self.quality_detail.setText("\n".join(detail_lines))
        set_semantic_state(
            self.quality_detail,
            SemanticState.ERROR if row.dataset_error else None,
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

        # Lifecycle state + source attachments (#509, #446).
        disposition_parts = [
            f"状態: {_disposition_label(row.disposition)}"
            + (f" — {row.disposition_reason}" if row.disposition_reason else "")
        ]
        if row.is_corrected:
            disposition_parts.append(
                "割り当て訂正済み — 下記は有効な割り当てです"
                "（元の測定データは変更されていません）。"
            )
        if not row.is_normally_eligible:
            disposition_parts.append(
                "この測定は比較・分析の通常対象から除外されています。"
            )
        self.disposition_label.setText("\n".join(disposition_parts))
        attachments = self.controller.list_source_attachments(row.measurement_id)
        self.attachments_label.setText(
            "ソース添付: "
            + "、".join(
                f"{_attachment_kind_label(a.kind)} {a.filename}"
                for a in attachments
            )
            if attachments
            else "ソース添付: なし"
        )
        self.disposition_apply_button.setEnabled(True)
        self.correct_button.setEnabled(True)
        self.attach_button.setEnabled(True)

        self._refresh_target_curve_choices()
        self._refresh_spatial(row)
        self._refresh_quality_plot()

    def _selected_quality_view(self) -> MeasurementView | None:
        row_index = self.quality_table.currentRow()
        if not (0 <= row_index < len(self._quality_views)):
            return None
        return self._quality_views[row_index]

    def _refresh_quality_plot(self) -> None:
        """Redraw the FR analysis view: stored trace, optional deterministic
        display smoothing, target-curve overlay, and stored phase (#489, #503).
        Never mutates the persisted dataset."""
        row = self._selected_quality_view()
        self.quality_plot.clear()
        self.phase_plot.clear()
        if row is None or row.dataset_id is None:
            self.provenance_label.setText("")
            self.phase_state_label.setText("")
            if row is not None and row.dataset_error:
                show_plot_state(
                    self.quality_plot,
                    "この測定のデータを検証できませんでした",
                    detail="保存時の検証に失敗したため、このデータは利用できません。",
                )
                return
            # Designed no-data state instead of an empty dark graph (#579).
            show_plot_state(
                self.quality_plot,
                "測定がありません",
                detail="測定を読み込むと周波数応答を表示できます。",
            )
            return
        dataset = self.controller.dataset(row.dataset_id)
        summary = processing_summary(dataset)
        self.provenance_label.setText(
            f"処理情報: {trace_label(dataset, self.quality_smoothing_combo.currentData())}"
            f" · {summary}"
        )

        fraction = self.quality_smoothing_combo.currentData()
        trace = _trace_color(row)
        if isinstance(fraction, int) and fraction:
            derived = smoothed_level_trace(dataset, fraction)
            # The exact stored samples stay visible underneath as a dimmed
            # baseline; the smoothed view is a derived overlay, clearly
            # labelled as such (#579 §1).
            self.quality_plot.plot(
                dataset.frequency_hz,
                dataset.level_db,
                pen=trace_pen(
                    TraceSemantic.BASELINE, color=trace.hex, dimmed=True
                ),
                name="保存データ",
            )
            self.quality_plot.plot(
                derived.frequency_hz,
                derived.level_db,
                pen=trace_pen(_trace_semantic(row), color=trace.hex),
                name=trace_label(dataset, fraction),
            )
        else:
            self.quality_plot.plot(
                dataset.frequency_hz,
                dataset.level_db,
                pen=trace_pen(_trace_semantic(row), color=trace.hex),
                name="保存データ",
            )
        target = self.quality_target_combo.currentData()
        if target is not None:
            # Target reads as a fine dotted low-saturation accent, always
            # named — never confused with evidence (#579 §11). Overlay drawn
            # at declared absolute levels — no renormalization is ever
            # applied silently to either trace.
            self.quality_plot.plot(
                [point.frequency_hz for point in target.points],
                [point.level_db for point in target.points],
                pen=trace_pen(TraceSemantic.TARGET),
                name=f"ターゲット（{target.normalization.method}）",
            )

        # Stored phase response — an independent axis from common timing.
        trace_phase = phase_trace(dataset, unwrap=self.phase_unwrap_check.isChecked())
        timing = _capability_decision_label(row.common_timing_capability)
        if trace_phase is None:
            self.phase_state_label.setText(
                f"{_phase_label(row.phase_status)} · 共通タイミング: {timing}"
            )
            self.phase_plot.setVisible(False)
            return
        self.phase_plot.setVisible(True)
        self.phase_state_label.setText(
            f"{_phase_label(row.phase_status)} · 共通タイミング: {timing} — "
            "この位相表示だけでは測定間の共通時間基準は成立しません"
        )
        self.phase_plot.plot(
            trace_phase.frequency_hz,
            trace_phase.phase_deg,
            pen=trace_pen(_trace_semantic(row), color=trace.hex),
            name=(
                "位相（アンラップ表示）"
                if trace_phase.unwrapped
                else "位相（保存値）"
            ),
        )

    def _refresh_target_curve_choices(self) -> None:
        selected_index = self.quality_target_combo.currentIndex()
        self.quality_target_combo.blockSignals(True)
        self.quality_target_combo.clear()
        self.quality_target_combo.addItem("オーバーレイなし", None)
        try:
            curves = self.controller.target_curves()
        except Exception as exc:
            curves = ()
            self._operation_error_notice(
                "ターゲットカーブを読み込めませんでした",
                exc,
                effect=None,
                severity=SemanticState.WARNING,
            )
        for plan_id, curve in curves:
            self.quality_target_combo.addItem(
                f"{plan_id} · {curve.normalization.method}", curve
            )
        if selected_index > 0 and selected_index < self.quality_target_combo.count():
            self.quality_target_combo.setCurrentIndex(selected_index)
        self.quality_target_combo.blockSignals(False)

    def _refresh_spatial(self, row: MeasurementView) -> None:
        # #903: a failed load must read differently from "no context".
        try:
            self._spatial_context = self.controller.spatial_context(row.measurement_id)
        except Exception as exc:
            self._spatial_context = None
            self.spatial_summary.setText("空間コンテキストを読み込めませんでした")
            self.spatial_fallback_label.setText("")
            log_operation_error(
                to_user_facing_error(
                    exc, title="空間コンテキストを読み込めませんでした"
                ),
                exc,
            )
            return
        context = self._spatial_context
        if context is None or context.bound_revision is None:
            self.spatial_summary.setText("この測定の部屋コンテキストはありません")
            self.spatial_fallback_label.setText("")
            self.open_bound_room_button.setEnabled(False)
            return
        self.open_bound_room_button.setEnabled(True)
        bound = context.bound_revision
        lines = [
            f"測定時の部屋: {bound.created_at_utc}"
            f"（{len(bound.document.entities)} エンティティ）"
            + (" — 現在の部屋と一致" if context.bound_is_current else " — 履歴上の配置"),
        ]
        if context.measurement_position is not None:
            p = context.measurement_position
            lines.append(
                f"測定位置: ({p.x_m:.2f}, {p.y_m:.2f}, {p.z_m:.2f}) m"
            )
        if context.measurement_direction is not None:
            d = context.measurement_direction
            lines.append(
                f"測定方向: ({d[0]:.2f}, {d[1]:.2f}, {d[2]:.2f})"
            )
        if context.room_changed:
            lines.append("現在の部屋との差分:")
            for change in context.changes:
                extra = f" +{change.distance_m:.2f}m" if change.distance_m else ""
                lines.append(
                    f"  {_spatial_change_label(change.kind)}: {change.name}{extra}"
                )
        else:
            lines.append("現在の部屋と同じ配置です。")
        self.spatial_summary.setText("\n".join(lines))
        self._render_spatial()

    def _render_spatial(self) -> None:
        context = self._spatial_context
        if context is None or context.bound_revision is None:
            return
        mode = self.spatial_mode_combo.currentData()
        self.spatial_fallback_label.setText("")
        if self._spatial_viewport is None and not self._spatial_viewport_failed:
            try:
                from .room_viewport import RoomOverlayState, RoomViewport3D

                self._spatial_viewport = RoomViewport3D(self)
                self._spatial_viewport.setMinimumHeight(320)
                self._spatial_overlays = RoomOverlayState(grid=True, labels=True)
                self.spatial_viewport_holder.addWidget(self._spatial_viewport)
            except Exception:
                self._spatial_viewport_failed = True
                self.spatial_fallback_label.setText(
                    "この環境では3D表示を利用できません。"
                    "上の差分一覧で確認してください。"
                )
                return
        if self._spatial_viewport is None:
            return
        try:
            # One draw for the whole composite (document + ghosts + overlay).
            with self._spatial_viewport.deferred_render():
                if mode == "diff" and context.current_revision is not None:
                    # Current scene is authority; measurement-time entities render
                    # as wireframe ghosts so stale vs current stays visible.
                    self._spatial_viewport.render_document(
                        context.current_revision.document,
                        selected_id=None,
                        overlays=self._spatial_overlays,
                        reset_camera=True,
                    )
                    self._spatial_viewport.render_proposed_entities(
                        context.bound_revision.document.entities,
                        selected_id=context.effective_entity_id,
                        label="測定時の配置 ghost · current Sceneは変更しません",
                    )
                else:
                    self._spatial_viewport.render_document(
                        context.bound_revision.document,
                        selected_id=context.effective_entity_id,
                        overlays=self._spatial_overlays,
                        reset_camera=True,
                    )
                self._spatial_viewport.render_measurement_overlay(
                    position=context.measurement_position,
                    direction=context.measurement_direction,
                )
        except Exception:
            self._spatial_viewport_failed = True
            self.spatial_fallback_label.setText(
                "この環境では3D表示を利用できません。上の差分一覧で確認してください。"
            )

    def _open_bound_room(self) -> None:
        # #485 の履歴ワークスペースが存在しないため、測定時の部屋をこのページ内の
        # 3Dビューで読み取り専用に開く。
        index = self.spatial_mode_combo.findData("bound")
        if index >= 0:
            self.spatial_mode_combo.setCurrentIndex(index)

    def _apply_disposition(self) -> None:
        row = self._selected_quality_view()
        if row is None:
            return
        disposition = str(self.disposition_combo.currentData())
        reason, ok = QInputDialog.getText(
            self,
            "測定の状態を記録",
            "この状態を記録する理由（監査ログに残ります）:",
        )
        if not ok or not reason.strip():
            self._set_notice("状態を記録する理由が必要です", SemanticState.WARNING)
            return
        try:
            self.controller.set_disposition(
                row.measurement_id, disposition, reason.strip()
            )
        except Exception as exc:
            self._operation_error_notice("状態を記録できませんでした", exc)
            return
        self._set_notice("測定の状態を記録しました", SemanticState.SUCCESS)
        self.refresh()

    def _start_correction(self) -> None:
        row = self._selected_quality_view()
        if row is None:
            return
        self._correction_target_id = row.measurement_id
        self._set_notice(
            "「割り当て」で訂正する項目を直して保存してください。"
            "元の測定データは変更されません。",
            None,
        )
        self.refresh()

    def _attach_to_measurement(self) -> None:
        row = self._selected_quality_view()
        if row is None:
            return
        path, _ = file_dialog_memory.get_open_file_name(
            self, "添付ファイルを選択", 'measurement.attach', "すべてのファイル (*)"
        )
        if not path:
            return
        kind = str(self.quality_attach_kind_combo.currentData())
        try:
            file_path = Path(path)
            raw = read_file_bounded(
                file_path,
                MAX_NATIVE_REW_TEXT_FILE_BYTES,
                label="ソース添付",
            )
            self.controller.save_source_attachment(
                row.measurement_id,
                kind=kind,
                filename=file_path.name,
                raw_bytes=raw,
            )
        except Exception as exc:
            self._operation_error_notice("添付に失敗しました", exc)
            return
        self._set_notice("ソース添付を保存しました", SemanticState.SUCCESS)
        self.refresh()
        self.quality_plot.enableAutoRange()
        self._update_context_label()

    def _start_retake(self) -> None:
        row_index = self.quality_table.currentRow()
        if not (0 <= row_index < len(self._quality_views)):
            return
        row = self._quality_views[row_index]
        self._retake_source_id = row.measurement_id
        self._correction_target_id = None
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
            "データセットを比較する",
            "保存済みの周波数応答を A/B で比較します。実測・予測・シート間・前後比較に使えます。",
        )
        page.setObjectName("measurementComparisonPage")

        setup_card, setup_layout = _card("比較条件", host)
        form = QFormLayout()
        self.preset_combo = QComboBox(setup_card)
        for label, value in (
            ("任意 A/B", "any"),
            ("実測 vs 予測", "measured_vs_predicted"),
            ("実測 vs 実測（シート間・前後）", "measured_pair"),
            ("再測定系譜のみ", "retake_lineage"),
        ):
            self.preset_combo.addItem(label, value)
        self.preset_combo.currentIndexChanged.connect(
            lambda _i: self._refresh_comparison_choices()
        )
        form.addRow("プリセット", self.preset_combo)

        self.measured_combo = QComboBox(setup_card)
        self.predicted_combo = QComboBox(setup_card)
        form.addRow("データセット A", self.measured_combo)
        form.addRow("データセット B", self.predicted_combo)

        # One coherent band control (#586): low–high range plus presets
        # rather than two disconnected form rows.
        band_widget = QWidget(setup_card)
        band_row = QHBoxLayout(band_widget)
        band_row.setContentsMargins(0, 0, 0, 0)
        self.compare_low = QDoubleSpinBox(band_widget)
        self.compare_low.setRange(1.0, 100000.0)
        self.compare_low.setValue(20.0)
        self.compare_low.setSuffix(" Hz")
        band_row.addWidget(self.compare_low)
        band_row.addWidget(QLabel("–", band_widget))
        self.compare_high = QDoubleSpinBox(band_widget)
        self.compare_high.setRange(1.0, 100000.0)
        self.compare_high.setValue(20000.0)
        self.compare_high.setSuffix(" Hz")
        band_row.addWidget(self.compare_high)
        band_full = QPushButton("全帯域", band_widget)
        band_full.clicked.connect(
            lambda: (self.compare_low.setValue(20.0), self.compare_high.setValue(20000.0))
        )
        band_row.addWidget(band_full)
        band_lf = QPushButton("低域", band_widget)
        band_lf.clicked.connect(
            lambda: (self.compare_low.setValue(20.0), self.compare_high.setValue(300.0))
        )
        band_row.addWidget(band_lf)
        band_row.addStretch(1)
        form.addRow("比較帯域", band_widget)

        ref_row = QHBoxLayout()
        self.ref_band_check = QCheckBox("参照帯域でレベル合わせ", setup_card)
        ref_row.addWidget(self.ref_band_check)
        self.ref_low = QDoubleSpinBox(setup_card)
        self.ref_low.setRange(1.0, 100000.0)
        self.ref_low.setValue(20.0)
        self.ref_low.setSuffix(" Hz")
        self.ref_high = QDoubleSpinBox(setup_card)
        self.ref_high.setRange(1.0, 100000.0)
        self.ref_high.setValue(120.0)
        self.ref_high.setSuffix(" Hz")
        ref_row.addWidget(self.ref_low)
        ref_row.addWidget(self.ref_high)
        ref_row.addStretch(1)
        form.addRow("レベル参照", ref_row)

        setup_layout.addLayout(form)

        exclude_row = QHBoxLayout()
        exclude_row.addWidget(QLabel("除外帯域:", setup_card))
        self.excluded_low = QDoubleSpinBox(setup_card)
        self.excluded_low.setRange(1.0, 100000.0)
        self.excluded_low.setValue(45.0)
        self.excluded_low.setSuffix(" Hz")
        self.excluded_high = QDoubleSpinBox(setup_card)
        self.excluded_high.setRange(1.0, 100000.0)
        self.excluded_high.setValue(65.0)
        self.excluded_high.setSuffix(" Hz")
        exclude_row.addWidget(self.excluded_low)
        exclude_row.addWidget(self.excluded_high)
        self.add_exclusion_button = QPushButton("追加", setup_card)
        self.add_exclusion_button.clicked.connect(self._add_exclusion_band)
        exclude_row.addWidget(self.add_exclusion_button)
        self.remove_exclusion_button = QPushButton("選択を削除", setup_card)
        self.remove_exclusion_button.clicked.connect(self._remove_exclusion_band)
        exclude_row.addWidget(self.remove_exclusion_button)
        exclude_row.addStretch(1)
        setup_layout.addLayout(exclude_row)

        self.excluded_table = QTableWidget(0, 2, setup_card)
        self.excluded_table.setHorizontalHeaderLabels(["下限", "上限"])
        self.excluded_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.excluded_table.verticalHeader().setVisible(False)
        self.excluded_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.excluded_table.setMaximumHeight(110)
        setup_layout.addWidget(self.excluded_table)

        smooth_row = QHBoxLayout()
        smooth_row.addWidget(QLabel("表示スムージング:", setup_card))
        self.smooth_a_combo = QComboBox(setup_card)
        self.smooth_b_combo = QComboBox(setup_card)
        for label, fraction in DISPLAY_SMOOTHING_FRACTIONS:
            self.smooth_a_combo.addItem(f"A: {label}", fraction)
            self.smooth_b_combo.addItem(f"B: {label}", fraction)
        self.smooth_a_combo.currentIndexChanged.connect(
            lambda _i: self._preview_comparison_pair()
        )
        self.smooth_b_combo.currentIndexChanged.connect(
            lambda _i: self._preview_comparison_pair()
        )
        smooth_row.addWidget(self.smooth_a_combo)
        smooth_row.addWidget(self.smooth_b_combo)
        smooth_row.addStretch(1)
        setup_layout.addLayout(smooth_row)

        self.mismatch_label = QLabel("", setup_card)
        self.mismatch_label.setWordWrap(True)
        setup_layout.addWidget(self.mismatch_label)

        compare_row = QHBoxLayout()
        self.comparison_availability = QLabel("", setup_card)
        self.comparison_availability.setWordWrap(True)
        compare_row.addWidget(self.comparison_availability, 1)
        self.compare_button = QPushButton("比較結果を保存", setup_card)
        set_primary_action(self.compare_button)
        self.compare_button.clicked.connect(self._run_comparison)
        compare_row.addWidget(self.compare_button)
        setup_layout.addLayout(compare_row)
        layout.addWidget(setup_card)

        plot_card, plot_layout = _card("周波数応答", host)
        cursor_row = QHBoxLayout()
        self.comparison_cursor_check = QCheckBox("カーソル", plot_card)
        self.comparison_cursor_check.toggled.connect(
            self._toggle_comparison_cursor
        )
        cursor_row.addWidget(self.comparison_cursor_check)
        self.comparison_cursor_readout = QLabel("", plot_card)
        set_typography_role(
            self.comparison_cursor_readout, TypographyRole.SECONDARY
        )
        cursor_row.addWidget(self.comparison_cursor_readout, 1)
        plot_layout.addLayout(cursor_row)

        self.comparison_plot = pg.PlotWidget(plot_card)
        self.comparison_plot.setMinimumHeight(280)
        self.comparison_plot.setLabel("bottom", "周波数", units="Hz")
        self.comparison_plot.setLabel("left", "レベル", units="dB")
        _set_plot_appearance(self.comparison_plot)
        add_scientific_legend(self.comparison_plot)
        plot_layout.addWidget(self.comparison_plot)
        # Presentation-only probe shared by the stacked comparison plots:
        # one X cursor keeps frequency navigation aligned (#579 §7-8).
        self._comparison_cursor = PlotCursor(self.comparison_plot)
        self._comparison_cursor.line.sigPositionChanged.connect(
            lambda _line: self._update_comparison_cursor_readout()
        )
        self._cursor_traces: list[tuple[str, list[float], list[float]]] = []

        self.difference_plot = pg.PlotWidget(plot_card)
        self.difference_plot.setMinimumHeight(180)
        self.difference_plot.setLabel("bottom", "周波数", units="Hz")
        self.difference_plot.setLabel("left", "A − B", units="dB")
        _set_plot_appearance(self.difference_plot)
        # Explicit 0 dB reference — a difference graph without a zero line
        # hides which side is louder (#579 §6).
        add_reference_line(self.difference_plot, 0.0)
        # Frequency navigation stays shared between FR and difference.
        link_x_axis(self.comparison_plot, self.difference_plot)
        plot_layout.addWidget(self.difference_plot)

        self.phase_compare_plot = pg.PlotWidget(plot_card)
        self.phase_compare_plot.setMinimumHeight(170)
        self.phase_compare_plot.setLabel("bottom", "周波数", units="Hz")
        self.phase_compare_plot.setLabel("left", "位相", units="deg")
        _set_plot_appearance(self.phase_compare_plot)
        add_scientific_legend(self.phase_compare_plot)
        link_x_axis(self.comparison_plot, self.phase_compare_plot)
        plot_layout.addWidget(self.phase_compare_plot)
        layout.addWidget(plot_card)

        result_card, result_layout = _card("比較結果", host)
        self.comparison_state_label = QLabel("プレビュー（未保存）", result_card)
        set_typography_role(self.comparison_state_label, TypographyRole.SECONDARY)
        result_layout.addWidget(self.comparison_state_label)
        # Aligned metric rows (#586): scannable name/value pairs rather than
        # a prose sentence, with no generic score.
        self.comparison_metrics = QTableWidget(0, 2, result_card)
        self.comparison_metrics.setHorizontalHeaderLabels(["指標", "値"])
        self.comparison_metrics.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.comparison_metrics.verticalHeader().setVisible(False)
        self.comparison_metrics.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.comparison_metrics.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.comparison_metrics.setMaximumHeight(150)
        result_layout.addWidget(self.comparison_metrics)
        self.comparison_result = QLabel("比較未実行", result_card)
        self.comparison_result.setWordWrap(True)
        result_layout.addWidget(self.comparison_result)

        comparison_export_row = QHBoxLayout()
        self.comparison_export_csv_button = QPushButton(
            "この比較をCSVで保存…", result_card
        )
        self.comparison_export_csv_button.setToolTip(
            "保存済み比較の差分・両側レベルと判定メタデータを"
            "解析エクスポート形式のCSVで書き出します。"
        )
        self.comparison_export_csv_button.setEnabled(False)
        self.comparison_export_csv_button.clicked.connect(
            self._export_saved_comparison
        )
        comparison_export_row.addWidget(self.comparison_export_csv_button)
        self.comparison_export_png_button = QPushButton(
            "差分プロットをPNGで保存…", result_card
        )
        self.comparison_export_png_button.setToolTip(
            "現在表示している差分プロットを画像として書き出します。"
        )
        self.comparison_export_png_button.setEnabled(False)
        self.comparison_export_png_button.clicked.connect(
            self._export_difference_plot_png
        )
        comparison_export_row.addWidget(self.comparison_export_png_button)
        comparison_export_row.addStretch(1)
        result_layout.addLayout(comparison_export_row)

        self.comparison_history = QTableWidget(0, 7, result_card)
        self.comparison_history.setHorizontalHeaderLabels(
            ["作成時刻", "帯域", "参照帯域", "除外", "RMS差", "レベル差", "形状RMS"]
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

        self.measured_combo.currentIndexChanged.connect(
            self._dataset_a_changed
        )
        self.predicted_combo.currentIndexChanged.connect(
            self._comparison_selection_changed
        )
        self.comparison_history.itemSelectionChanged.connect(
            self._history_selection_changed
        )

    # ------------------------------------------------------------------
    # Calibration page — instrument onboarding (#1061)

    def _build_calibration_page(self) -> None:
        """UMIK-1 onboarding: calibration, orientation, SPL readiness, REW."""
        self._onboarding_context_id: str | None = None
        self._onboarding_steps: tuple[InstrumentStep, ...] = ()
        page, _host, layout = _page(
            "機器の準備（キャリブレーション）",
            "計測機器の校正・向き・SPL準備とREWキャンペーン設定を順に確認します。"
            "項目をダブルクリックすると対象のページに移動します。",
        )

        guide_card, guide = _card("UMIK-1 の計測準備", page)
        guide_text = QLabel(
            "・校正ファイル: 90deg基準（上向き・天井）は「_90deg」を含むファイル、"
            "0deg基準（正面）は含まないファイルを記録\n"
            "・向き: 90deg = マイクを天井へ向ける [0,0,1]（ホームシアター標準）/ "
            "0deg = 正面へ向ける [0,-1,0]（単一スピーカー計測時）\n"
            "・サンプルレート: 48 kHz\n"
            "・絶対SPL: UMIK-1単体では相対レベルのみ — 音響校正器・REW SPLセッション・"
            "基準メーター転送のいずれかのレベル校正記録が必要\n"
            "・REW: 「REW -api」で起動し、入力デバイスはJavaでUMIK-1を選択、"
            "校正ファイルを適用してからキャンペーンを計測",
            guide_card,
        )
        guide_text.setWordWrap(True)
        set_typography_role(guide_text, TypographyRole.SECONDARY)
        guide.addWidget(guide_text)
        layout.addWidget(guide_card)

        check_card, check = _card("準備チェック", page)
        self.onboarding_table = QTableWidget(0, 3, check_card)
        self.onboarding_table.setHorizontalHeaderLabels(
            ["ステップ", "状態", "確認内容"]
        )
        self.onboarding_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.onboarding_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.onboarding_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.onboarding_table.verticalHeader().setVisible(False)
        self.onboarding_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self.onboarding_table.setMinimumHeight(200)
        self.onboarding_table.itemActivated.connect(
            self._onboarding_step_activated
        )
        check.addWidget(self.onboarding_table)

        jump_row = QHBoxLayout()
        open_assignment = QPushButton("割り当てを開く", check_card)
        open_assignment.clicked.connect(
            lambda: self.set_context("assignment")
        )
        jump_row.addWidget(open_assignment)
        open_campaign = QPushButton("キャンペーンを開く", check_card)
        open_campaign.clicked.connect(
            lambda: self.set_context("campaign")
        )
        jump_row.addWidget(open_campaign)
        jump_row.addStretch(1)
        check.addLayout(jump_row)
        layout.addWidget(check_card)

        layout.addStretch(1)
        self.pages.addWidget(page)

    def _onboarding_step_activated(self, item: QTableWidgetItem) -> None:
        if item is None:
            return
        link = item.data(_USER_ROLE)
        if isinstance(link, str) and link in _CONTEXT_IDS:
            self.set_context(link)

    def _refresh_onboarding(self) -> None:
        contexts = self.controller.quality_repository.list_acquisition_contexts()
        context = next(
            (entry for entry in contexts if entry.microphone is not None),
            contexts[0] if contexts else None,
        )
        self._onboarding_context_id = (
            context.acquisition_context_id if context is not None else None
        )
        steps = evaluate_instrument_onboarding(
            context=context,
            level_calibrations=(
                self.controller.quality_repository.list_level_calibrations()
            ),
            plan_count=len(self.controller.runner_plans()),
        )
        self._onboarding_steps: tuple[InstrumentStep, ...] = steps
        status_labels = {
            "ready": "準備完了",
            "action": "要対応",
            "manual": "要確認",
        }
        self.onboarding_table.setRowCount(len(steps))
        for row, step in enumerate(steps):
            title_item = QTableWidgetItem(step.title)
            title_item.setData(_USER_ROLE, step.link)
            status_item = QTableWidgetItem(status_labels[step.status])
            detail_item = QTableWidgetItem(step.detail)
            detail_item.setToolTip(step.detail)
            self.onboarding_table.setItem(row, 0, title_item)
            self.onboarding_table.setItem(row, 1, status_item)
            self.onboarding_table.setItem(row, 2, detail_item)
        if self.current_context_id == "calibration":
            self._update_context_label()

    def _exclusion_bands(self) -> tuple[tuple[float, float], ...]:
        bands: list[tuple[float, float]] = []
        for row in range(self.excluded_table.rowCount()):
            low = self.excluded_table.item(row, 0)
            high = self.excluded_table.item(row, 1)
            if low is None or high is None:
                continue
            bands.append((float(low.text()), float(high.text())))
        return tuple(bands)

    def _add_exclusion_band(self) -> None:
        low = float(self.excluded_low.value())
        high = float(self.excluded_high.value())
        if not (low > 0.0 and high > low):
            self._set_notice(
                "除外帯域は 下限 < 上限 で指定してください。",
                SemanticState.WARNING,
            )
            return
        row = self.excluded_table.rowCount()
        self.excluded_table.insertRow(row)
        self.excluded_table.setItem(row, 0, QTableWidgetItem(f"{low:.3f}"))
        self.excluded_table.setItem(row, 1, QTableWidgetItem(f"{high:.3f}"))

    def _remove_exclusion_band(self) -> None:
        rows = sorted(
            {item.row() for item in self.excluded_table.selectedItems()},
            reverse=True,
        )
        for row in rows:
            self.excluded_table.removeRow(row)

    def _comparison_candidate_groups(
        self,
        views: tuple[MeasurementView, ...] | None = None,
    ) -> tuple[tuple[MeasurementView, ...], tuple[MeasurementView, ...]]:
        if views is None:
            views = self.controller.measurement_views()
        """A/B candidate lists for the selected preset (#483).

        ``any`` exposes every normally-eligible dataset on both sides;
        ``measured_vs_predicted`` keeps the classic measured-A/predicted-B
        split; ``measured_pair`` and ``retake_lineage`` both list measured
        datasets on each side (seat-to-seat / before-after / retake).
        """
        preset = str(self.preset_combo.currentData())
        all_candidates = self.controller.comparison_candidates(views=views)
        if preset == "measured_vs_predicted":
            return (
                self.controller.comparison_candidates(
                    "measured", views=views
                ),
                self.controller.comparison_candidates(
                    "predicted", views=views
                ),
            )
        if preset == "measured_pair":
            measured = self.controller.comparison_candidates(
                "measured", views=views
            )
            return measured, measured
        if preset == "retake_lineage":
            lineage = tuple(
                row
                for row in all_candidates
                if row.supersedes_measurement_id is not None
                or row.superseded_by_measurement_id is not None
            )
            return lineage, lineage
        return all_candidates, all_candidates

    def _refresh_comparison_choices(
        self,
        views: tuple[MeasurementView, ...] | None = None,
    ) -> None:
        if views is None:
            views = self.controller.measurement_views()
        candidates_a, candidates_b = self._comparison_candidate_groups(views)
        self._fill_dataset_combo(self.measured_combo, candidates_a, "A")
        self._fill_dataset_combo(
            self.predicted_combo,
            candidates_b,
            "B",
            exclude_dataset_id=self.measured_combo.currentData(),
        )

        a_id = self.measured_combo.currentData()
        b_id = self.predicted_combo.currentData()
        ready = bool(
            isinstance(a_id, str)
            and isinstance(b_id, str)
            and a_id
            and b_id
        )
        self.compare_button.setEnabled(ready)
        if ready:
            self.comparison_availability.setText(
                "比較結果は部屋状態と測定データに紐付けて保存します。"
            )
            set_semantic_state(self.comparison_availability, None)
        elif not candidates_a and not candidates_b:
            self.comparison_availability.setText(
                "比較できる周波数応答がありません。先にREWを読み込み、証拠種別を割り当ててください。"
            )
            set_semantic_state(self.comparison_availability, SemanticState.UNSUPPORTED)
        else:
            self.comparison_availability.setText(
                "A と B にデータセットを選択してください。"
            )
            set_semantic_state(self.comparison_availability, SemanticState.UNSUPPORTED)

        self._preview_comparison_pair(views)
        try:
            comparisons = self.controller.saved_comparisons()
        except Exception as exc:
            # One row that fails comparison re-verification must not take
            # the workspace down: the history reads stay empty and the
            # failure is surfaced instead of an unhandled exception.
            comparisons = ()
            self._set_notice(
                "比較履歴を読み込めませんでした · "
                + operation_error_message(exc),
                SemanticState.ERROR,
            )
        self._saved_comparisons = comparisons
        self.comparison_history.setRowCount(len(comparisons))
        for row_index, comparison in enumerate(comparisons):
            rms = "—" if comparison.rms_difference_db is None else f"{comparison.rms_difference_db:.3f} dB"
            shape = "—" if comparison.shape_rms_db is None else f"{comparison.shape_rms_db:.3f} dB"
            offset = (
                "—"
                if comparison.level_offset_db is None
                else f"{comparison.level_offset_db:+.3f} dB"
            )
            ref = (
                "なし"
                if comparison.reference_band_hz is None
                else _format_band(comparison.reference_band_hz)
            )
            excluded = (
                "なし"
                if not comparison.excluded_bands
                else " / ".join(
                    _format_band(band) for band in comparison.excluded_bands
                )
            )
            values = (
                comparison.created_at,
                _format_band(comparison.actual_band_hz),
                ref,
                excluded,
                rms,
                offset,
                shape,
            )
            for column, value in enumerate(values):
                self.comparison_history.setItem(row_index, column, QTableWidgetItem(value))

    @staticmethod
    def _fill_dataset_combo(
        combo: QComboBox,
        rows: tuple[MeasurementView, ...],
        side: str,
        exclude_dataset_id: str | None = None,
    ) -> None:
        previous = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        for row in rows:
            if row.dataset_id is None:
                continue
            scene = "現在" if row.scene_matches_current else "測定時配置"
            combo.addItem(
                f"{_evidence_label(row.evidence_type)} · "
                f"{_channel_role_label(row.effective_channel_role)} · "
                f"{row.effective_target_name} · {scene}",
                row.dataset_id,
            )
        if previous is not None and previous != exclude_dataset_id:
            index = combo.findData(previous)
            if index >= 0:
                combo.setCurrentIndex(index)
        if (
            exclude_dataset_id is not None
            and combo.currentData() == exclude_dataset_id
        ):
            for index in range(combo.count()):
                if combo.itemData(index) != exclude_dataset_id:
                    combo.setCurrentIndex(index)
                    break
            else:
                combo.setCurrentIndex(-1)
        combo.blockSignals(False)

    def _history_selection_changed(self) -> None:
        """Selecting a saved comparison reloads its exact analysis (#586)."""
        row_index = self.comparison_history.currentRow()
        comparisons = getattr(self, "_saved_comparisons", ())
        if not (0 <= row_index < len(comparisons)):
            return
        saved = comparisons[row_index]
        self._last_comparison = saved
        self.comparison_state_label.setText("保存済み比較")
        self._bind_saved_pair(saved)
        self._show_comparison(saved)

    def _bind_saved_pair(self, saved: CadMeasurementComparison) -> None:
        """Re-seat the dataset selectors on the saved A/B pair.

        History reload must reopen the comparison it names — the FR/phase
        overlay, cursor readout and mismatch row all follow the persisted
        pair instead of whatever preview happened to be selected. When a
        side has since left the eligible set (disposition, retired
        evidence) the live overlay is cleared rather than left showing an
        unrelated pair; the saved labels and metrics still render from
        the record itself.
        """
        a_index = self.measured_combo.findData(saved.dataset_a_id)
        b_index = self.predicted_combo.findData(saved.dataset_b_id)
        if a_index < 0 or b_index < 0:
            # The persisted pair may have been compared under a different
            # preset — retry against the superset 'any' candidate lists
            # before concluding a side left the eligible set.
            any_index = self.preset_combo.findData("any")
            if any_index >= 0 and self.preset_combo.currentIndex() != any_index:
                self.preset_combo.blockSignals(True)
                self.preset_combo.setCurrentIndex(any_index)
                self.preset_combo.blockSignals(False)
                candidates_a, candidates_b = self._comparison_candidate_groups()
                self._fill_dataset_combo(self.measured_combo, candidates_a, "A")
                self._fill_dataset_combo(
                    self.predicted_combo,
                    candidates_b,
                    "B",
                    exclude_dataset_id=saved.dataset_a_id,
                )
                a_index = self.measured_combo.findData(saved.dataset_a_id)
                b_index = self.predicted_combo.findData(saved.dataset_b_id)
        if a_index < 0 or b_index < 0:
            for combo in (self.measured_combo, self.predicted_combo):
                combo.blockSignals(True)
                combo.setCurrentIndex(-1)
                combo.blockSignals(False)
            self.comparison_plot.clear()
            self.comparison_plot.addItem(
                self._comparison_cursor.line, ignoreBounds=True
            )
            self.phase_compare_plot.clear()
            self.phase_compare_plot.setVisible(False)
            self._cursor_traces = []
            self.mismatch_label.setText("")
            show_plot_state(
                self.comparison_plot,
                "この保存済み比較の測定データは現在の比較候補にありません",
            )
            return
        self.measured_combo.blockSignals(True)
        self.measured_combo.setCurrentIndex(a_index)
        self.measured_combo.blockSignals(False)
        self.predicted_combo.blockSignals(True)
        self.predicted_combo.setCurrentIndex(b_index)
        self.predicted_combo.blockSignals(False)
        self._preview_comparison_pair()

    def _dataset_a_changed(self) -> None:
        a_id = self.measured_combo.currentData()
        if (
            isinstance(a_id, str)
            and self.predicted_combo.currentData() == a_id
        ):
            for index in range(self.predicted_combo.count()):
                if self.predicted_combo.itemData(index) != a_id:
                    self.predicted_combo.setCurrentIndex(index)
                    break
        self._comparison_selection_changed()

    def _comparison_selection_changed(self) -> None:
        # Selector-driven preview is visibly distinct from a persisted
        # comparison (#586): preview never poses as saved evidence.
        self._last_comparison = None
        self.comparison_state_label.setText("プレビュー（未保存）")
        self.comparison_export_csv_button.setEnabled(False)
        self.comparison_export_png_button.setEnabled(False)
        self._update_context_label()
        self._preview_comparison_pair()

    def _plot_dataset_trace(
        self,
        dataset_id: str,
        side: str,
        smoothing_fraction: int,
        color_hex: str,
        semantic: TraceSemantic,
    ) -> None:
        dataset = self.controller.dataset(dataset_id)
        label = f"{side}: {trace_label(dataset, smoothing_fraction)}"
        if smoothing_fraction:
            derived = smoothed_level_trace(dataset, smoothing_fraction)
            # Stored samples stay underneath as a dim baseline — never the
            # same pen grammar as evidence traces (#579 §1).
            self.comparison_plot.plot(
                dataset.frequency_hz,
                dataset.level_db,
                pen=trace_pen(
                    TraceSemantic.BASELINE, color=color_hex, dimmed=True
                ),
                name=f"{side}: 保存データ",
            )
            self.comparison_plot.plot(
                derived.frequency_hz,
                derived.level_db,
                pen=trace_pen(semantic, color=color_hex),
                name=label,
            )
            self._cursor_traces.append(
                (label, list(derived.frequency_hz), list(derived.level_db))
            )
        else:
            self.comparison_plot.plot(
                dataset.frequency_hz,
                dataset.level_db,
                pen=trace_pen(semantic, color=color_hex),
                name=label,
            )
            self._cursor_traces.append(
                (label, list(dataset.frequency_hz), list(dataset.level_db))
            )
        self._update_comparison_cursor_readout()

    def _preview_comparison_pair(
        self,
        views: tuple[MeasurementView, ...] | None = None,
    ) -> None:
        if views is None:
            views = self.controller.measurement_views()
        self.comparison_plot.clear()
        # clear() drops non-curve items — re-seat the shared probe (#579).
        self.comparison_plot.addItem(
            self._comparison_cursor.line, ignoreBounds=True
        )
        self.phase_compare_plot.clear()
        self.phase_compare_plot.setVisible(False)
        a_id = self.measured_combo.currentData()
        b_id = self.predicted_combo.currentData()
        tokens = DARK_THEME.scientific
        self._cursor_traces = []
        # Pen grammar encodes the dataset's evidence type, not its slot —
        # under the 任意 A/B preset a predicted dataset may sit in slot A
        # and must still draw dashed (#579 §1).
        semantics_by_dataset = {
            row.dataset_id: _trace_semantic(row)
            for row in self.controller.comparison_candidates(views=views)
            if row.dataset_id
        }
        semantic_of = lambda dataset_id: semantics_by_dataset.get(
            dataset_id, TraceSemantic.MEASURED
        )
        if isinstance(a_id, str) and a_id:
            self._plot_dataset_trace(
                a_id, "A", int(self.smooth_a_combo.currentData()),
                tokens.primary_trace.hex, semantic_of(a_id),
            )
        if isinstance(b_id, str) and b_id:
            self._plot_dataset_trace(
                b_id, "B", int(self.smooth_b_combo.currentData()),
                tokens.secondary_trace.hex, semantic_of(b_id),
            )
        if a_id or b_id:
            self.comparison_plot.enableAutoRange()
        else:
            # Designed empty state rather than a bare dark canvas (#579 §13).
            show_plot_state(
                self.comparison_plot,
                "A と B にデータセットを選択すると比較を表示します。",
            )

        # Stored phase for eligible A/B (#489): phase availability stays a
        # separate axis from common timing and is never implied by it.
        phases_plotted = 0
        for dataset_id, side, color_hex, semantic in (
            (a_id, "A", tokens.primary_trace.hex, semantic_of(a_id)),
            (b_id, "B", tokens.secondary_trace.hex, semantic_of(b_id)),
        ):
            if not (isinstance(dataset_id, str) and dataset_id):
                continue
            trace = phase_trace(self.controller.dataset(dataset_id))
            if trace is None:
                continue
            self.phase_compare_plot.plot(
                trace.frequency_hz,
                trace.phase_deg,
                pen=trace_pen(semantic, color=color_hex),
                name=f"{side}: 位相",
            )
            phases_plotted += 1
        self.phase_compare_plot.setVisible(phases_plotted > 0)

        # Semantic-mismatch advisory before interpreting the result (#483).
        if isinstance(a_id, str) and isinstance(b_id, str) and a_id and b_id:
            codes = self.controller.comparison_mismatches(
                a_id, b_id, views=views
            )
            self.mismatch_label.setText(
                "注意: " + "、".join(_mismatch_label(code) for code in codes)
                if codes
                else ""
            )
        else:
            self.mismatch_label.setText("")

    def _run_comparison(self) -> None:
        a_id = self.measured_combo.currentData()
        b_id = self.predicted_combo.currentData()
        if not isinstance(a_id, str) or not isinstance(b_id, str) or not a_id or not b_id:
            self._set_notice("A と B にデータセットを選択してください。", SemanticState.WARNING)
            return
        reference_band = None
        if self.ref_band_check.isChecked():
            low = float(self.ref_low.value())
            high = float(self.ref_high.value())
            if not (low > 0.0 and high > low):
                self._set_notice(
                    "参照帯域は 下限 < 上限 で指定してください。",
                    SemanticState.WARNING,
                )
                return
            reference_band = (low, high)
        try:
            saved = self.controller.compare_datasets(
                a_id,
                b_id,
                low_hz=float(self.compare_low.value()),
                high_hz=float(self.compare_high.value()),
                reference_band_hz=reference_band,
                excluded_bands=self._exclusion_bands(),
            )
        except Exception as exc:
            self._operation_error_notice("比較できませんでした", exc)
            return
        self._last_comparison = saved
        self.comparison_state_label.setText("保存済み比較")
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
        offset = (
            "利用不可"
            if saved.reference_band_hz is not None and saved.level_offset_db is None
            else ("—" if saved.level_offset_db is None else f"{saved.level_offset_db:+.3f} dB")
        )
        spec_lines = [f"実帯域 {_format_band(saved.actual_band_hz)}"]
        if saved.reference_band_hz is not None:
            spec_lines.append(f"参照帯域 {_format_band(saved.reference_band_hz)}")
        if saved.excluded_bands:
            spec_lines.append(
                "除外帯域 "
                + " / ".join(_format_band(band) for band in saved.excluded_bands)
            )
        metrics = (
            ("RMS差", rms),
            ("形状RMS", shape),
            ("平均差", mean),
            ("有効点", f"{saved.valid_points:,} / {saved.total_grid_points:,}"),
            ("実帯域", _format_band(saved.actual_band_hz)),
        )
        self.comparison_metrics.setRowCount(len(metrics))
        for row_index, (name, value) in enumerate(metrics):
            self.comparison_metrics.setItem(row_index, 0, QTableWidgetItem(name))
            value_item = QTableWidgetItem(value)
            value_item.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            self.comparison_metrics.setItem(row_index, 1, value_item)
        identity_lines: list[str] = []
        if saved.label_a is not None or saved.label_b is not None:
            identity_lines.append(
                f"A: {saved.label_a or '—'}　→　B: {saved.label_b or '—'}"
            )
        if saved.level_compatibility is not None:
            identity_lines.append(
                "レベル互換性: "
                + _level_compatibility_label(saved.level_compatibility)
            )
        if saved.semantics_json:
            # The persisted semantics are the record's verdict — show them
            # as stored, not recomputed against the live project (#852).
            try:
                saved_mismatches = json.loads(saved.semantics_json).get(
                    "mismatches"
                ) or ()
            except (TypeError, ValueError):
                saved_mismatches = ()
            if saved_mismatches:
                identity_lines.append(
                    "保存時の注意: "
                    + "、".join(
                        _mismatch_label(code) for code in saved_mismatches
                    )
                )
        self.comparison_result.setText(
            ("\n".join(identity_lines) + "\n" if identity_lines else "")
            + f"有効点 {saved.valid_points:,} / {saved.total_grid_points:,} · "
            f"平均差 {mean} · RMS差 {rms} · レベル差 {offset} · 形状RMS {shape}\n"
            + " · ".join(spec_lines)
        )
        self.difference_plot.clear()
        # Zero reference is recreated after clear (clear() removes items too).
        add_reference_line(self.difference_plot, 0.0)
        self.difference_plot.plot(
            saved.grid_hz,
            saved.difference_db,
            pen=trace_pen(
                TraceSemantic.DERIVED,
                color=DARK_THEME.scientific.primary_trace.hex,
            ),
            name="A − B",
        )
        self.difference_plot.enableAutoRange()
        self.comparison_export_csv_button.setEnabled(True)
        self.comparison_export_png_button.setEnabled(True)
        self._update_context_label()

    def _export_saved_comparison(self) -> None:
        """Write the shown saved comparison as an analysis CSV (#R9).

        The export carries all three curves the workspace renders — side A
        levels, side B levels and the A−B difference — plus the verdict
        metadata, keyed by the immutable ``comparison_id``. Like every
        comparison surface it reads the persisted record, never a
        recomputed preview.
        """
        saved = self._last_comparison
        if saved is None:
            return
        head = self.controller.scene_repository.current_head(
            self.controller.document_id
        )
        current_revision_id = head.revision_id if head is not None else None
        export = build_analysis_export(
            document_id=self.controller.document_id,
            title=f'比較 {saved.comparison_id}',
            generated_at_utc=datetime.now(timezone.utc).isoformat(),
            series=(
                comparison_side_series(
                    saved, 'a', current_scene_revision_id=current_revision_id
                ),
                comparison_side_series(
                    saved, 'b', current_scene_revision_id=current_revision_id
                ),
                series_from_comparison(
                    saved,
                    current_scene_revision_id=current_revision_id,
                ),
            ),
            metadata=comparison_metadata_entries(saved)
            + (
                AnalysisExportMeta(
                    key=(
                        f'comparison.{saved.comparison_id}'
                        '.semantics_json'
                    ),
                    value=saved.semantics_json,
                ),
            )
            if saved.semantics_json
            else comparison_metadata_entries(saved),
        )
        selected, _selected_filter = file_dialog_memory.get_save_file_name(
            self,
            "比較結果の保存先",
            'measurement.export_comparison',
            "CSV (*.csv)",
            suggested_name=f'htdt-comparison-{saved.comparison_id[:8]}.csv',
        )
        if not selected:
            return
        if not selected.lower().endswith('.csv'):
            selected += '.csv'
        try:
            write_text_atomic(Path(selected), render_analysis_csv(export))
        except OSError as exc:
            self._operation_error_notice(
                "比較をエクスポートできませんでした", exc
            )
            return
        self._set_notice(
            f"比較をCSVで書き出しました: {selected}", SemanticState.SUCCESS
        )

    def _export_difference_plot_png(self) -> None:
        """Snapshot the difference plot to PNG (#R9).

        ``ImageExporter`` renders what is on screen, so the PNG inherits
        the live axis state — zoom/pan included; the menu's リセット gives
        the canonical full view first.
        """
        if self._last_comparison is None:
            return
        selected, _selected_filter = file_dialog_memory.get_save_file_name(
            self,
            "差分プロットの保存先",
            'measurement.export_comparison_png',
            "PNG (*.png)",
            suggested_name=(
                'htdt-comparison-'
                f'{self._last_comparison.comparison_id[:8]}.png'
            ),
        )
        if not selected:
            return
        if not selected.lower().endswith('.png'):
            selected += '.png'
        target = Path(selected)
        exporter = ImageExporter(self.difference_plot.plotItem)
        exporter.parameters()['width'] = 1280
        staging: Path | None = None
        try:
            # ImageExporter only reports write failure via its return value
            # (QImage.save is False, never an exception); publish via a
            # sibling temp so a torn write never masquerades as the export.
            descriptor, staging_name = tempfile.mkstemp(
                prefix=f'.{target.name}.', suffix='.png', dir=target.parent
            )
            os.close(descriptor)
            staging = Path(staging_name)
            if not exporter.export(str(staging)):
                raise OSError(f'PNGの書き込みに失敗しました: {selected}')
            os.replace(staging, target)
        except (OSError, ValueError) as exc:
            if staging is not None:
                staging.unlink(missing_ok=True)
            self._operation_error_notice(
                "比較プロットを保存できませんでした", exc
            )
            return
        self._set_notice(
            f"差分プロットをPNGで書き出しました: {selected}",
            SemanticState.SUCCESS,
        )

    def _toggle_comparison_cursor(self, checked: bool) -> None:
        self._comparison_cursor.set_active(checked)
        self._update_comparison_cursor_readout()

    def _update_comparison_cursor_readout(self) -> None:
        if not self._comparison_cursor.active:
            self.comparison_cursor_readout.setText("")
            return
        self.comparison_cursor_readout.setText(
            self._comparison_cursor.nearest_readout(
                self._cursor_traces, value_unit="dB"
            ).replace("\n", "   ")
        )

    def _update_context_label(self) -> None:
        """Keep the persistent context header bound to the active selection."""
        if self.current_context_id == "calibration":
            context_id = self._onboarding_context_id
            self.context_label.setText(
                f"機器の準備 · 取得条件 {context_id}" if context_id else "機器の準備 · 取得条件なし"
            )
            return
        if self.current_context_id == "comparison":
            saved = self._last_comparison
            if saved is None:
                self.context_label.setText("比較: プレビュー（未保存）")
                return
            self.context_label.setText(
                f"保存済み比較 · {_format_band(saved.actual_band_hz)} · {saved.created_at}"
            )
            return
        if self.current_context_id == "quality":
            row_index = self.quality_table.currentRow()
            if 0 <= row_index < len(self._quality_views):
                row = self._quality_views[row_index]
                self.context_label.setText(
                    f"{_channel_role_label(row.channel_role)} · {row.target_name} · "
                    f"{_evidence_label(row.evidence_type)} · "
                    f"{'現在の配置' if row.scene_matches_current else '測定時の配置'} · "
                    f"品質: {_quality_label(row.quality_status)}"
                )
                return
        if self.current_context_id == "import":
            pending = self.controller.pending_import
            self.context_label.setText(
                f"読み込み中: {pending.source_label}" if pending is not None else "測定未選択"
            )
            return
        self.context_label.setText("測定未選択")

    # ------------------------------------------------------------------

    def _start_job(
        self,
        call: Callable[[], object],
        on_success: Callable[[object], None],
        error_prefix: str,
        *,
        on_retry: Callable[[], None] | None = None,
        purpose: str | None = None,
    ) -> None:
        if self._disposed:
            return
        key = uuid4().hex
        self._job_handlers[key] = (on_success, error_prefix, on_retry)
        if purpose is not None:
            self._job_purpose[key] = purpose
            self._latest_job_key[purpose] = key
        self._job_pool.start(
            key,
            lambda _cancel_event: call(),
            self._job_completed,
        )

    @Slot(object, object, object)
    def _job_completed(self, key: object, result: object, error: object) -> None:
        key_str = str(key)
        handler = self._job_handlers.pop(key_str, None)
        purpose = self._job_purpose.pop(key_str, None)
        if handler is None or self._disposed:
            return
        if purpose is not None and key_str != self._latest_job_key.get(purpose):
            # Superseded by a newer request on the same purpose — the late
            # result must never overwrite what the newer job will stage.
            return
        on_success, error_prefix, on_retry = handler
        if str(key) == self._commit_job_key:
            self._commit_job_key = None
            self._set_batch_committing(False)
            # Already-persisted items stay committed — refresh surfaces them
            # whatever the completion state was.
            self.refresh()
            if error == WORKER_CANCELLED:
                self._set_notice(
                    "保存をキャンセルしました。保存済みの項目はそのまま残っています。",
                    SemanticState.WARNING,
                )
                return
        if error is not None:
            if error != WORKER_CANCELLED:
                self._set_notice(
                    f"{error_prefix} · {operation_error_message(error)}",
                    SemanticState.ERROR,
                )
                # Retryable transport/concurrency failures also offer the
                # modal retry affordance — the notice stays so the record
                # survives a dismissed dialog.
                if (
                    on_retry is not None
                    and to_user_facing_error(error).code in RETRYABLE_ERROR_CODES
                ):
                    warn_user(self, error_prefix, error, on_retry=on_retry)
            return
        on_success(result)

    def _set_notice(
        self,
        message: str,
        state: SemanticState | None,
        *,
        action: tuple[str, Callable[[], None]] | None = None,
    ) -> None:
        self.notice.setText(message)
        if self.notice_action.receivers("clicked()"):
            self.notice_action.clicked.disconnect()
        self.notice_action.setVisible(False)
        if action is not None:
            label, callback = action
            self.notice_action.setText(label)
            self.notice_action.clicked.connect(callback)
            self.notice_action.setVisible(True)
        self.notice_row.setVisible(bool(message))
        set_semantic_state(self.notice, state)

    def _operation_error_notice(
        self,
        title: str,
        exc: BaseException,
        *,
        effect: str | None = '変更は保存されていません',
        severity: SemanticState = SemanticState.ERROR,
    ) -> None:
        """#903: notice = actionable mapped message; raw detail stays in the
        diagnostics path (``last_operation_error_detail`` + log)."""
        error = to_user_facing_error(
            exc, title=title, effect=effect, severity=severity
        )
        self._last_operation_error_detail = error.technical_detail
        log_operation_error(error, exc)
        self._set_notice(error.notice_text(), error.severity)

    @property
    def last_operation_error_detail(self) -> str | None:
        """Technical detail of the last operation failure (diagnostics path)."""
        return self._last_operation_error_detail

    @staticmethod
    def _pending_token(pending: PendingMeasurementImport) -> str:
        """Exact identity of the staged import (#796).

        A semantic hash of the staged content — source, scene pins, sample
        payload and scalar stage fields — not the Python object id, so a
        re-created equivalent pending object cannot silently defeat the
        acknowledgement.
        """

        payload = {
            'source_kind': pending.source_kind,
            'source_label': pending.source_label,
            'scene_revision_id': pending.scene_revision_id,
            'scene_content_hash': pending.scene_content_hash,
            'frequency_hz': pending.frequency_hz,
            'level_db': pending.level_db,
            'has_phase_samples': pending.has_phase_samples,
            'scene_revision_explicit': pending.scene_revision_explicit,
            'raw_filename': pending.raw_filename,
            'raw_text_head': (
                None if pending.raw_text is None
                else pending.raw_text[:4000].decode('utf-8', 'replace')
            ),
        }
        blob = json.dumps(
            payload, sort_keys=True, separators=(',', ':'),
            ensure_ascii=False, allow_nan=False,
        ).encode('utf-8')
        return sha256(blob).hexdigest()

    def before_deactivate(self) -> tuple[bool, str | None]:
        if self._job_pool.active_count:
            return False, "バックグラウンド処理が完了してから画面を切り替えてください"
        pending = self.controller.pending_import
        if pending is not None and self._pending_token(pending) != self._pending_release:
            return False, "取り込み途中の測定データを確定または破棄してから画面を切り替えてください"
        return True, None

    def dirty_state(self) -> WorkspaceDirtyState:
        """#610: the staged import joins the deactivation contract."""
        if self._job_pool.active_count:
            return 'busy'
        pending = self.controller.pending_import
        if pending is not None and self._pending_token(pending) != self._pending_release:
            return 'pending_import'
        return 'clean'

    def resolve_dirty_state(
        self, action: DirtyResolutionAction
    ) -> tuple[bool, str | None]:
        pending = self.controller.pending_import
        if action == 'keep_draft':
            if pending is None:
                return False, '取り込み途中のデータがありません'
            # Acknowledge this exact staged import; a new stage re-blocks.
            self._pending_release = self._pending_token(pending)
            return True, '取り込み途中のデータを残しました'
        if action == 'discard_pending':
            self.controller.clear_pending()
            self._pending_release = None
            self.refresh()
            return True, '取り込み途中のデータを破棄しました'
        return False, 'この状態では実行できない操作です'

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._disposed = True
        report = self._job_pool.shutdown()
        self._job_handlers.clear()
        self._job_purpose.clear()
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
        dirty_state=workspace.dirty_state,
        resolve_dirty_state=workspace.resolve_dirty_state,
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
