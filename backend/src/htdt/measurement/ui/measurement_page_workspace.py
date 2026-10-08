from __future__ import annotations

from collections.abc import Callable, MutableMapping
from datetime import datetime, timezone
from hashlib import sha256
import json
from math import isfinite
import os
from pathlib import Path
import tempfile
from threading import Event
from time import monotonic
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse
from uuid import uuid4

if TYPE_CHECKING:
    from ...application_preferences import ApplicationPreferenceStore
    from ...rew_auto import RewInstall

import pyqtgraph as pg
from pyqtgraph.exporters import ImageExporter
from PySide6.QtCore import QEvent, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHeaderView,
    QHBoxLayout,
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

from ... import file_dialog_memory
from ...analysis_export import (
    build_analysis_export,
    comparison_export_parts,
    render_analysis_csv,
)
from ...cad_display_labels import (
    acquisition_source_kind_label,
    measurement_claim_label,
    measurement_reason_label,
    saved_label,
)
from ..domain.cad_measurement_models import (
    MEASUREMENT_ATTACHMENT_KINDS,
    CadMeasurementComparison,
)
from ..domain.cad_measurement_authorities import (
    timing_reference_supports_common_timing,
)
from ..domain.cad_measurement_quality import (
    CadMeasurementCapability,
    CadMicrophoneCapture,
    CadPlaybackCapture,
)
from ...cad_repository import SceneRepository
from ...cad_scene import Direction3
from ...export_io import write_text_atomic
from ...ingress import read_file_bounded
from ...limits import MAX_ATTACHMENT_BYTES, MAX_NATIVE_REW_TEXT_FILE_BYTES
from ..domain.measurement_analysis import (
    DISPLAY_SMOOTHING_FRACTIONS,
    phase_trace,
    processing_summary,
    smoothed_level_trace,
    trace_label,
)
from .measurement_authority_dialogs import (
    DatasetLevelReferenceDialog,
    LevelCalibrationDialog,
    RoutingProfileDialog,
    StimulusProfileDialog,
    TimingReferenceDialog,
    level_reference_kind_label,
    timing_method_label,
)
from ..domain.measurement_instrument_onboarding import (
    InstrumentStep,
    evaluate_instrument_onboarding,
)
from ..domain.measurement_journey import (
    MeasurementJourneyStep,
    current_journey_step,
    evaluate_measurement_journey,
)
from .measurement_explanations import (
    apply_explanation,
    explain_combo_items,
    explain_form_row,
    explain_table_columns,
    metric_explanation,
    status_explanation,
)
from ...error_boundary import (
    EXPECTED_OPERATION_ERRORS,
    is_authority_failure,
    report_boundary_failure,
)
from ..services.measurement_workflow import (
    AcquisitionCapture,
    AssignmentCorrection,
    BatchImportItem,
    MeasurementAssignment,
    MeasurementView,
    MeasurementWorkflowController,
    MeasurementWorkflowError,
    PendingMeasurementImport,
    RewReadSource,
)
from ...native_worker import WORKER_CANCELLED, NativeWorkerPool
from ...rew_auto import (
    find_rew_install,
    launch_rew,
    scan_rew_watch_dir,
)
from ...scientific_plot_style import (
    PlotCursor,
    TraceSemantic,
    add_reference_line,
    add_scientific_legend,
    apply_scientific_appearance,
    link_x_axis,
    show_plot_state,
    trace_pen,
)
from ...user_facing_error import (
    RETRYABLE_ERROR_CODES,
    log_operation_error,
    operation_error_message,
    to_user_facing_error,
    warn_user,
)
from ...ui_theme import (
    DARK_THEME,
    SemanticState,
    SurfaceRole,
    TypographyRole,
    set_primary_action,
    set_semantic_state,
    set_surface_role,
    set_typography_role,
)
from ...workflow_navigation import WorkspaceDeepLink, WorkspaceId
from ...workflow_shell import WorkspaceFactory, WorkspaceMount
from ...workspace_dirty_state import DirtyResolutionAction, WorkspaceDirtyState


_CONTEXT_IDS = (
    "import",
    "assignment",
    "campaign",
    "quality",
    "comparison",
    "calibration",
    "acquisition",
    # #877 guided calibration wizard — reached from the calibration page,
    # not the context rail (same pattern as the acquisition page).
    "calibration_wizard",
)

_CELL_STATUS_LABELS = {
    "not_started": "未着手",
    "staged": "取り込み済",
    "assignment_incomplete": "割り当て未完了",
    "quality_pending": "品質確認待ち",
    "retake_required": "要再測定",
    "completed": "完了",
    "skipped": "スキップ",
}
_VARIANT_PURPOSE_LABELS = {
    "measurement": "測定",
    "calibration": "校正",
    "holdout": "ホールドアウト",
    "diagnostic": "診断",
    "validation": "検証",
}
_USER_ROLE = int(Qt.ItemDataRole.UserRole)

# #969: below this VIEWPORT width the quality table and its selection
# detail stack vertically instead of sharing a horizontal split — the
# detail must stay readable without any horizontal scrolling. The value
# covers the side-by-side minimum (~1160 px: both cards' minimum widths
# plus margins) so horizontal mode never needs an h-scrollbar of its own.
_QUALITY_DETAIL_STACK_MIN_WIDTH = 1180
# Verdict-filter sentinel matching rows whose 品質 cell shows 検証エラー
# (a dataset authoritative-read failure, which is not a quality_status).
_QUALITY_VERDICT_DATASET_ERROR = "__dataset_error__"

# Background automation retries are transient-failure paths, not permanent
# ones: a REW fetch or watch-file stage that fails this many times in a row
# is treated as wedged — skipped with one report instead of re-running on
# every poll tick.
_REW_AUTO_MAX_ATTEMPTS = 3


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
        "calibration": "校正",
        "repeatability": "繰り返し精度",
    }.get(check, check)


def _retake_reason_label(reason: str) -> str:
    """JA gloss for one stored ``retake_reasons`` entry (``check: reason``)."""
    check, separator, detail = reason.partition(': ')
    if separator:
        return f"{_check_label(check)}: {measurement_reason_label(detail)}"
    return measurement_reason_label(reason)


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
    return measurement_claim_label(claim)


def _report_state_label(state: str) -> str:
    return {
        "current": "最新",
        "stale": "旧データセットのレポート（再評価が必要です）",
        "missing": "レポートなし",
        "error": "検証エラー",
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
        "misassigned": "誤割り当て",
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


def _comparability_state_label(state: str) -> str:
    """JA labels for the #564 comparability gate verdict."""
    return {
        "comparable": "比較可能",
        "comparable_with_limitations": "条件付き比較可能",
        "incomparable": "比較不可",
        "insufficient_evidence": "証拠不足",
    }.get(state, state)


def _timing_method_label(method: str) -> str:
    """JA labels for the #564 time-reference method vocabulary."""
    return {
        "exact_reference": "正確な基準",
        "acoustic_timing_reference": "音響基準",
        "known_hardware_latency": "既知のハード遅延",
        "estimated_from_direct_arrival": "直接波推定",
        "estimated_by_correlation": "相関推定",
        "unknown": "不明",
    }.get(method, method)


def _partition_label(partition: str) -> str:
    return {
        "calibration": "キャリブレーション",
        "holdout": "ホールドアウト",
        "repeatability": "再現性",
        "unassigned": "未割当",
    }.get(partition, partition)


def _observable_label(observable: str) -> str:
    return {
        "magnitude_db": "帯域レベル残差",
        "phase_deg": "位相残差",
        "direct_arrival_time": "直接波到達時刻",
        "early_reflection_time": "初期反射到達時刻",
        "modal_peak_frequency": "モードピーク周波数",
        "decay_time": "減衰時間",
    }.get(observable, observable)


def _observable_state_label(state: str) -> str:
    return {
        "computed": "算出",
        "unsupported": "対象外",
        "insufficient_data": "データ不足",
    }.get(state, state)


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


class _NativeCampaignPreflightDialog(QDialog):
    """Read-only pre-arm report for the native campaign run (#956).

    Every routing, level, position and policy the run will execute is
    displayed before any audio output; the explicit "アーム承認" click is
    the only path that arms the runner — spec or saved settings never
    substitute for it. A failed PRECHECK disables arming (fail-closed).
    """

    def __init__(self, report, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("ネイティブ自動実行 — 事前確認")
        self.setModal(True)
        self.armed = False
        self.report = report

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        if report.simulated_backend:
            banner = QLabel(
                "SIMULATED バックエンド — 実行結果はテスト証跡であり、"
                "実測定の合格証拠にはなりません。", self)
            banner.setWordWrap(True)
            set_semantic_state(banner, SemanticState.WARNING)
            layout.addWidget(banner)

        lines = [
            f"実行計画: {report.plan_id}",
            f"再生デバイス: {', '.join(report.playback_devices) or '（なし）'}",
            f"録音デバイス: {', '.join(report.capture_devices) or '（なし）'}",
            f"レベル: {report.level_dbfs:.1f} dBFS "
            f"（ポリシー上限 {report.max_output_level_dbfs:.1f} dBFS）",
            f"校正状態: {report.calibration_state}",
            "",
            "ルーティング:",
            *(f"  {line}" for line in report.routing_lines),
            "",
            "測定位置（各位置への移動時に確認を求めます）:",
            *(f"  {line}" for line in report.position_lines),
        ]
        body = QLabel("\n".join(lines), self)
        body.setWordWrap(True)
        layout.addWidget(body)

        if report.blocked_reasons:
            blocked = QLabel(
                "事前チェックでブロックされています:\n"
                + "\n".join(f"  {r}" for r in report.blocked_reasons),
                self)
            blocked.setWordWrap(True)
            set_semantic_state(blocked, SemanticState.WARNING)
            layout.addWidget(blocked)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel_button = QPushButton("キャンセル", self)
        cancel_button.clicked.connect(self.reject)
        buttons.addWidget(cancel_button)
        self.arm_button = QPushButton(
            "アーム承認 — この内容で出力を開始", self)
        set_primary_action(self.arm_button)
        self.arm_button.setEnabled(report.ok)
        if not report.ok:
            self.arm_button.setToolTip(
                "事前チェックでブロックされているためアームできません")
        self.arm_button.clicked.connect(self._arm)
        buttons.addWidget(self.arm_button)
        layout.addLayout(buttons)

    def _arm(self) -> None:
        self.armed = True
        self.accept()


class MeasurementPageWorkspace(QWidget):
    # Emitted from the batch-commit worker thread; Qt queues it to the UI
    # thread, so the notice can show live progress without polling.
    batch_commit_progress = Signal(int, int)
    """UX130 document-like measurement workspace mounted directly by the shell."""

    def __init__(
        self,
        controller: MeasurementWorkflowController,
        parent: QWidget | None = None,
        *,
        on_navigate: Callable[[WorkspaceDeepLink], bool] | None = None,
        help_registry=None,
        open_help: Callable[[str], bool] | None = None,
        preferences: 'ApplicationPreferenceStore | None' = None,
        activity_center=None,
        rew_launcher: Callable[..., Any] | None = None,
        rew_auto_interval_ms: int = 15_000,
        rew_launch_timeout_s: float = 90.0,
        acquisition_backend=None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        # #869: the sweep-acquisition backend — WASAPI stub by default (it
        # reports backend_unavailable honestly on this build); tests inject
        # the deterministic fake.
        if acquisition_backend is None:
            from ...cad_sweep_acquisition import WasapiAudioBackend

            acquisition_backend = WasapiAudioBackend()
        self._acq_backend = acquisition_backend
        self._acq_engine = None
        self._acq_stimulus_definition = None
        self._acq_run_record = None
        self._on_navigate = on_navigate
        # REV32-TERMS: help surfaces — the registry drives the glossary
        # dialog and error→topic resolution; ``open_help`` routes to the
        # composition's topic opener when mounted inside the shell.
        self._help_registry = help_registry
        self._open_help = open_help
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
        # REV56-MEASEV: lazily opened measurement-evidence repository for
        # the quality-detail panel (#572/#573).
        self._measurement_evidence_repository = None
        self._stimulus_registry_repository = None
        self._spatial_campaign_repository = None
        # REV40-REWAUTO: REW automation state. Auto jobs share the worker
        # pool but are excluded from the deactivation/dirty busy gate via
        # ``_rew_auto_job_keys`` — a background poll must never trap the
        # operator on this page.
        self._preferences = preferences
        self._activity_center = activity_center
        self._rew_launcher = rew_launcher if rew_launcher is not None else launch_rew
        self._rew_auto_interval_ms = rew_auto_interval_ms
        self._rew_launch_timeout_s = rew_launch_timeout_s
        self._rew_state = 'connecting'
        self._rew_seen_uuids: set[str] = set()
        self._rew_uuids_known = False
        self._rew_watch_seen: MutableMapping[str, tuple[int, int]] = {}
        # One-scan-old watch candidates: a dropped file is staged only once
        # its signature survives a second scan (never mid-write).
        self._rew_watch_pending: MutableMapping[str, tuple[int, int]] = {}
        # Per-measurement fetch / per-file stage failure counts — a
        # deterministically failing fetch or file stops retrying after
        # ``_REW_AUTO_MAX_ATTEMPTS`` instead of re-running every poll tick.
        self._rew_fetch_failures: dict[str, int] = {}
        self._rew_watch_failures: dict[str, int] = {}
        # The root the watch markers belong to — a preference change re-
        # baselines instead of letting another directory's markers stage
        # or dedupe files (REV43; mirrors CaptureWatchRunner's epoch rule).
        self._rew_watch_root: Path | None = None
        # Uncommitted batch item ids the operator acknowledged via
        # keep_draft (REV42): released rows no longer block deactivation,
        # while a newly staged row re-arms the gate.
        self._batch_released_ids: set[str] = set()
        # Auto notices that already fired — a persistent condition (missing
        # watch dir, no saved scene) must not re-shout over user notices
        # every poll tick; cleared when the condition resolves.
        self._rew_auto_notice_keys: set[str] = set()
        self._rew_install: 'RewInstall | None' = None
        self._rew_launch_deadline: float | None = None
        self._rew_auto_job_keys: set[str] = set()
        self._rew_timer = QTimer(self)
        self._rew_timer.setInterval(rew_auto_interval_ms)
        self._rew_timer.timeout.connect(self._rew_auto_tick)
        self._rew_rows: list[dict[str, Any]] = []
        self._quality_views: tuple[MeasurementView, ...] = ()
        # #969: the table is filtered and sortable, so a visual row index
        # can never be trusted — every row→view lookup goes through the
        # measurement_id stored on each item's UserRole.
        self._quality_views_by_id: dict[str, MeasurementView] = {}
        self._filtered_quality_views: tuple[MeasurementView, ...] = ()
        self._quality_page_viewport: QWidget | None = None
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
        self._journey_steps: tuple[MeasurementJourneyStep, ...] = ()
        self._journey_buttons: dict[str, QPushButton] = {}

        self.setObjectName("measurementPageWorkspace")
        set_surface_role(self, SurfaceRole.BASE)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Persistent selection context (#586): the active measurement or
        # comparison identity stays visually tied to the detail/plot below.
        context_row = QHBoxLayout()
        context_row.setContentsMargins(24, 8, 24, 4)
        self.context_label = QLabel(self)
        self.context_label.setObjectName("measurementContextLabel")
        self.context_label.setText("測定未選択")
        context_row.addWidget(self.context_label, 1)
        # REV32-TERMS: a persistent help affordance — the TermId-driven
        # glossary and topic help are one click away from every page.
        self.glossary_button = QPushButton("用語集・ヘルプ", self)
        self.glossary_button.setObjectName("measurementGlossaryButton")
        self.glossary_button.setToolTip(
            "フィールドや状態表示の意味を用語集とヘルプで確認できます"
        )
        self.glossary_button.clicked.connect(self._show_glossary)
        self.glossary_button.setVisible(
            self._help_registry is not None or self._open_help is not None
        )
        context_row.addWidget(self.glossary_button)
        root.addLayout(context_row)

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
        self.notice_action.setToolTip("この通知に対応する画面・操作へ移動します。")
        self.notice_action.setWhatsThis("この通知に対応する画面・操作へ移動します。")
        self.notice_action.setObjectName("measurementWorkspaceNoticeAction")
        self.notice_action.setVisible(False)
        notice_row_layout.addWidget(self.notice_action)
        self.notice_row.setVisible(False)
        root.addWidget(self.notice_row)

        # Numbered journey strip (REV32): the six context tabs are flat
        # siblings that never express the canonical first-run order, so a
        # state-driven step list stays pinned above the page stack — each
        # number opens the page that owns the step.
        self.journey_card = QFrame(self)
        self.journey_card.setObjectName("measurementJourneyCard")
        set_surface_role(self.journey_card, SurfaceRole.RAISED)
        journey_layout = QVBoxLayout(self.journey_card)
        journey_layout.setContentsMargins(16, 10, 16, 10)
        journey_layout.setSpacing(6)
        journey_head = QHBoxLayout()
        journey_head.setSpacing(8)
        journey_title = QLabel("測定の手順", self.journey_card)
        set_typography_role(journey_title, TypographyRole.SECTION_TITLE)
        journey_head.addWidget(journey_title)
        journey_head.addStretch(1)
        self.journey_progress = QLabel(self.journey_card)
        self.journey_progress.setObjectName("measurementJourneyProgress")
        set_typography_role(self.journey_progress, TypographyRole.SECONDARY)
        journey_head.addWidget(self.journey_progress)
        journey_layout.addLayout(journey_head)
        self.journey_steps_row = QHBoxLayout()
        self.journey_steps_row.setSpacing(6)
        journey_layout.addLayout(self.journey_steps_row)
        journey_hint_row = QHBoxLayout()
        journey_hint_row.setSpacing(8)
        self.journey_hint = QLabel(self.journey_card)
        self.journey_hint.setObjectName("measurementJourneyHint")
        self.journey_hint.setWordWrap(True)
        set_typography_role(self.journey_hint, TypographyRole.SECONDARY)
        journey_hint_row.addWidget(self.journey_hint, 1)
        self.journey_open = QPushButton("現在の手順を開く", self.journey_card)
        self.journey_open.setToolTip("手順の進行状態に応じた、次に開くべきページへ移動します。")
        self.journey_open.setWhatsThis("手順の進行状態に応じた、次に開くべきページへ移動します。")
        self.journey_open.setObjectName("measurementJourneyOpen")
        self.journey_open.clicked.connect(self._open_current_journey_step)
        journey_hint_row.addWidget(self.journey_open)
        journey_layout.addLayout(journey_hint_row)
        root.addWidget(self.journey_card)

        self.pages = QStackedWidget(self)
        self.pages.setObjectName("measurementPageStack")
        root.addWidget(self.pages, 1)

        self._build_import_page()
        self._build_assignment_page()
        self._build_campaign_page()
        self._build_quality_page()
        self._build_comparison_page()
        self._build_calibration_page()
        self._build_acquisition_page()
        self._build_calibration_wizard_page()
        self._wire_explanations()
        self.refresh()

    # ------------------------------------------------------------------
    # Shell interface

    def set_context(self, context_id: str) -> None:
        if context_id not in _CONTEXT_IDS:
            raise ValueError(f"unknown measurement context: {context_id}")
        if context_id == self.current_context_id:
            # The shell re-issues the route context on every activation even
            # when it did not change; a same-context no-op keeps activate()'s
            # own refresh as the single reload instead of doubling it.
            return
        self.current_context_id = context_id
        self.pages.setCurrentIndex(_CONTEXT_IDS.index(context_id))
        self.refresh()
        self._update_context_label()

    def focus_entity(self, entity_id: str) -> None:
        def _match() -> MeasurementView | None:
            for row in self._quality_views:
                if (
                    row.measurement_id == entity_id
                    or row.target_entity_id == entity_id
                ):
                    return row
            return None

        view = _match()
        if view is None:
            return
        if self.current_context_id != "quality":
            # The selection only exists on the quality page — show it before
            # selecting, otherwise the row changes invisibly behind the
            # section the link specified.
            self.set_context("quality")
            view = _match()
            if view is None:
                return
        row_index = self._quality_row_index_for_id(view.measurement_id)
        if row_index is None:
            # #969: a deep link must reach its row — when the active filters
            # hide it, reset them instead of selecting invisibly.
            self._clear_quality_filters()
            row_index = self._quality_row_index_for_id(view.measurement_id)
            if row_index is None:
                return
        self.quality_table.selectRow(row_index)
        self._show_quality_row(row_index)

    def select_measurement_id(self, measurement_id: str) -> bool:
        """Deep-link/palette focus port: select the quality row for
        ``measurement_id``; returns False when the record is absent."""
        self.refresh()
        for row in self._quality_views:
            if row.measurement_id != measurement_id:
                continue
            if self.current_context_id != "quality":
                self.set_context("quality")
            row_index = self._quality_row_index_for_id(measurement_id)
            if row_index is None:
                self._clear_quality_filters()
                row_index = self._quality_row_index_for_id(measurement_id)
            if row_index is None:
                return False
            self.quality_table.selectRow(row_index)
            self._show_quality_row(row_index)
            return True
        return False

    # ------------------------------------------------------------------
    # REV32-TERMS — explanations, glossary and help plumbing

    def _wire_explanations(self) -> None:
        """Tooltip/WhatsThis on every field, table header and status combo.

        Content lives in ``measurement_explanations`` — this method only
        binds registry keys to widgets, so a missing key is a no-op rather
        than a build error.
        """
        for widget, key in (
            (self.rew_combo, 'import.rew_measurement'),
            (self.batch_attach_kind_combo, 'import.attachment_kind'),
            (self.assignment_scope_combo, 'assignment.scope'),
            (self.target_combo, 'assignment.target'),
            (self.evidence_combo, 'assignment.evidence_type'),
            (self.channel_combo, 'assignment.channel_role'),
            (self.radiation_combo, 'assignment.radiation_scope'),
            (self.routing_combo, 'assignment.routing_evidence'),
            (
                self.acquisition_revision_combo,
                'assignment.acquisition_revision',
            ),
            (self.routing_profile_combo, 'assignment.routing_profile'),
            (self.source_speaker_list, 'assignment.source_speakers'),
            (self.acquisition_preset_combo, 'acquisition.preset'),
            (
                self.mic_orientation_combo,
                'acquisition.mic_orientation',
            ),
            (
                self.mic_manufacturer_edit,
                'acquisition.mic_manufacturer',
            ),
            (self.mic_model_edit, 'acquisition.mic_model'),
            (
                self.mic_serial_edit,
                'acquisition.mic_serial',
            ),
            (
                self.mic_sample_rate_edit,
                'acquisition.sample_rate',
            ),
            (
                self.mic_cal_file_edit,
                'acquisition.calibration_file',
            ),
            (
                self.mic_cal_sha_edit,
                'acquisition.calibration_sha',
            ),
            (
                self.output_device_edit,
                'acquisition.output_device',
            ),
            (self.avr_model_edit, 'acquisition.avr_model'),
            (
                self.avr_volume_edit,
                'acquisition.avr_volume',
            ),
            (
                self.avr_processing_edit,
                'acquisition.avr_processing',
            ),
            (self.avr_peq_edit, 'acquisition.avr_peq'),
            (self.campaign_plan_combo, 'campaign.plan'),
            (self.campaign_source_list, 'campaign.sources'),
            (self.campaign_target_list, 'campaign.targets'),
            (self.campaign_purpose_combo, 'campaign.purpose'),
            (self.campaign_repeat, 'campaign.repeat'),
            (self.campaign_pattern_combo, 'campaign.pattern'),
            (self.campaign_variant_combo, 'campaign.variant_plan'),
            (self.campaign_measurement_combo, 'campaign.measurement'),
            (self.quality_verdict_combo, 'quality.filter.verdict'),
            (self.quality_state_combo, 'quality.filter.state'),
            (self.quality_channel_filter, 'quality.filter.channel'),
            (self.quality_position_filter, 'quality.filter.position'),
            (self.quality_search_edit, 'quality.filter.search'),
            (self.quality_filter_clear, 'quality.filter.clear'),
            (self.quality_smoothing_combo, 'quality.smoothing'),
            (self.quality_target_combo, 'quality.target'),
            (self.phase_unwrap_check, 'quality.phase_unwrap'),
            (self.spatial_mode_combo, 'quality.spatial_mode'),
            (self.disposition_combo, 'quality.disposition'),
            (self.quality_attach_kind_combo, 'quality.attach_kind'),
            (self.preset_combo, 'comparison.preset'),
            (self.measured_combo, 'comparison.dataset_a'),
            (self.predicted_combo, 'comparison.dataset_b'),
            (self.compare_low, 'comparison.band_low'),
            (self.compare_high, 'comparison.band_high'),
            (self.ref_band_check, 'comparison.reference_band'),
            (self.ref_low, 'comparison.reference_band'),
            (self.ref_high, 'comparison.reference_band'),
            (self.excluded_low, 'comparison.excluded_band'),
            (self.excluded_high, 'comparison.excluded_band'),
            (self.smooth_a_combo, 'comparison.smoothing'),
            (self.smooth_b_combo, 'comparison.smoothing'),
        ):
            self._explain_field(widget, key)

        explain_table_columns(
            self.batch_table,
            {
                0: 'import.batch_table.file',
                1: 'import.batch_table.status',
                2: 'import.batch_table.band',
                3: 'import.batch_table.phase',
                4: 'import.batch_table.duplicate',
                5: 'import.batch_table.resolution',
                6: 'import.batch_table.saved',
            },
        )
        explain_table_columns(
            self.campaign_table,
            {
                0: 'campaign.table.channel_role',
                1: 'campaign.table.target',
                2: 'campaign.table.repeat',
                3: 'campaign.table.status',
                4: 'campaign.table.measurement',
            },
        )
        explain_table_columns(
            self.quality_table,
            {
                0: 'quality.table.channel',
                1: 'quality.table.evidence',
                2: 'quality.table.target',
                3: 'quality.table.quality',
                4: 'quality.table.phase',
                5: 'quality.table.timing',
                6: 'quality.table.placement',
                7: 'quality.table.band',
                8: 'quality.table.disposition',
                9: 'quality.table.retake',
            },
        )
        explain_table_columns(
            self.comparison_metrics,
            {0: 'comparison.metrics.name', 1: 'comparison.metrics.value'},
        )
        explain_table_columns(
            self.comparison_history,
            {
                0: 'comparison.history.created',
                1: 'comparison.history.band',
                2: 'comparison.history.reference',
                3: 'comparison.history.excluded',
                4: 'comparison.history.rms',
                5: 'comparison.history.offset',
                6: 'comparison.history.shape',
            },
        )
        explain_table_columns(
            self.excluded_table,
            {
                0: 'comparison.excluded_band',
                1: 'comparison.excluded_band',
            },
        )
        explain_table_columns(
            self.onboarding_table,
            {
                0: 'calibration.table.step',
                1: 'calibration.table.status',
                2: 'calibration.table.check',
            },
        )

        # Dropdown items that carry a status code explain themselves too.
        # Attach-kind, evidence and purpose vocabularies collide with
        # other surfaces ('calibration', 'unknown') — pass their domain.
        explain_combo_items(self.evidence_combo, domain='evidence')
        explain_combo_items(self.quality_attach_kind_combo, domain='attach')
        explain_combo_items(self.batch_attach_kind_combo, domain='attach')
        explain_combo_items(self.campaign_purpose_combo, domain='purpose')
        explain_combo_items(self.disposition_combo)

    def _explain_field(self, widget: QWidget, key: str) -> None:
        """Explain a field — the input AND its QFormLayout row label."""
        parent = widget.parentWidget()
        if parent is not None:
            for form in parent.findChildren(QFormLayout):
                if form.labelForField(widget) is not None:
                    explain_form_row(form, widget, key)
                    return
        apply_explanation(widget, key)

    def _help_available(self) -> bool:
        return self._help_registry is not None or self._open_help is not None

    def _show_help_topic(self, topic_id: str) -> None:
        """Open one help topic — composition route first, registry fallback."""
        if self._open_help is not None and self._open_help(topic_id):
            return
        if self._help_registry is None:
            return
        topic = self._help_registry.get(topic_id)
        if topic is None:
            return
        from ...localization import PresentationLocale
        from ...workflow_help import HelpDialog

        HelpDialog.topic(
            topic,
            locale=PresentationLocale.JAPANESE,
            command_registry=None,
            help_registry=self._help_registry,
            parent=self,
        ).exec()

    def _show_glossary(self) -> None:
        """The TermId-registry-driven glossary surface (REV32-TERMS)."""
        if self._open_help is not None and self._open_help('help.glossary'):
            return
        if self._help_registry is None:
            return
        from ...localization import PresentationLocale
        from ...workflow_help import GlossaryDialog

        GlossaryDialog(
            self._help_registry,
            locale=PresentationLocale.JAPANESE,
            parent=self,
        ).exec()

    def _open_error_help(self, code: str) -> None:
        """Resolve an error code to its bound topic (fallback: the generic
        operation-error topic) and show it."""
        topic_id = 'trouble.operation_error'
        if self._help_registry is not None:
            bound = self._help_registry.topic_for_reason(code)
            if bound is not None:
                topic_id = bound.topic_id
        self._show_help_topic(topic_id)

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
        # Runner plans are listed once per refresh: the campaign combo,
        # onboarding steps and journey strip previously paid a full
        # document-scoped listing each.
        runner_plans = self.controller.runner_plans()
        runner_plan_created = self.controller.runner_plan_created_at_utc()
        self._refresh_campaign(
            views, plans=runner_plans, plan_created=runner_plan_created
        )
        self._refresh_quality(views)
        self._refresh_comparison_choices(views)
        self._refresh_onboarding(plans=runner_plans)
        self._refresh_authority_inventory()
        self._refresh_journey(batch_items, views, plans=runner_plans)
        self._refresh_record_surfaces()

    def _refresh_journey(
        self,
        batch_items: list[BatchImportItem] | tuple[BatchImportItem, ...],
        views: tuple[MeasurementView, ...] | list[MeasurementView],
        *,
        plans=None,
    ) -> None:
        """Re-evaluate the numbered journey strip from persisted state.

        Runs after ``_refresh_onboarding`` so the instrument table's
        evaluated steps drive the 機器の準備 entry without a second pass.
        Every controller call degrades to an empty signal: a guide that
        silently drops to step 1 is annoying but a guide that kills the
        whole workspace refresh is a regression.
        """
        try:
            self.controller.latest_revision()
            scene_saved = True
        except MeasurementWorkflowError:
            # Genuinely unsaved → step 1. Store/integrity failures must
            # not masquerade as 'no scene' — they propagate to the
            # uncaught-exception diagnostics boundary instead (#815).
            scene_saved = False
        plan_count = 0
        cells_completed = 0
        cells_remaining = 0
        if scene_saved:
            try:
                plans = (
                    self.controller.runner_plans() if plans is None else plans
                )
                plan_count = len(plans)
                for plan in plans:
                    runs = self.controller.runner_repository.list_runs(
                        plan.plan_id
                    )
                    if not runs:
                        continue
                    progress = self.controller.runner_progress(runs[-1].run_id)
                    cells_completed += progress.completed
                    cells_remaining += (
                        progress.total - progress.completed - progress.skipped
                    )
            except EXPECTED_OPERATION_ERRORS as exc:
                if is_authority_failure(exc):
                    raise  # sealed-store failures never degrade to step-1
                report_boundary_failure(exc, operation='測定計画の状態確認')
        try:
            pending_import = self.controller.pending_import
        except EXPECTED_OPERATION_ERRORS as exc:
            if is_authority_failure(exc):
                raise
            report_boundary_failure(exc, operation='取り込み予定の確認')
            pending_import = None
        staged_pending = pending_import is not None or any(
            item.status in ("staged", "failed") for item in batch_items
        )
        steps = evaluate_measurement_journey(
            scene_saved=scene_saved,
            instrument_steps=tuple(self._onboarding_steps or ()),
            plan_count=plan_count,
            cells_completed=cells_completed,
            cells_remaining=cells_remaining,
            measurement_count=len(views),
            staged_pending=staged_pending,
        )
        self._journey_steps = steps

        while self.journey_steps_row.count():
            item = self.journey_steps_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._journey_buttons.clear()
        done_count = sum(1 for step in steps if step.status == "done")
        self.journey_progress.setText(f"{done_count}/6")
        for step in steps:
            label = f"{step.number} {step.title}"
            if step.status == "done":
                label += " ✓"
            button = QPushButton(label, self.journey_card)
            button.setObjectName(f"measurementJourneyStep_{step.key}")
            button.setFlat(True)
            button.setToolTip(step.detail)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            if step.status == "current":
                set_primary_action(button)
            elif step.status == "done":
                set_semantic_state(button, SemanticState.SUCCESS)
            button.clicked.connect(
                lambda checked=False, key=step.key: self._open_journey_step(key)
            )
            self.journey_steps_row.addWidget(button)
            self._journey_buttons[step.key] = button
        self.journey_steps_row.addStretch(1)

        current = current_journey_step(steps)
        if current is None:
            self.journey_hint.setText(
                "すべての手順が完了しています。品質の確認や別条件との比較に進めます。"
            )
            self.journey_open.setVisible(False)
        else:
            self.journey_hint.setText(
                f"次にやること — {current.number} {current.title}: {current.detail}"
            )
            self.journey_open.setVisible(True)

    def _open_journey_step(self, key: str) -> None:
        step = next(
            (step for step in self._journey_steps if step.key == key), None
        )
        if step is None:
            return
        # Deep links keep the shell's context bar and router in sync with
        # the page switch; bare set_context is the standalone/test fallback.
        if step.context_id is not None:
            if self._on_navigate is not None:
                self._on_navigate(
                    WorkspaceDeepLink(
                        WorkspaceId.MEASUREMENT, step.context_id
                    )
                )
            else:
                self.set_context(step.context_id)
            return
        if step.workspace == "room" and self._on_navigate is not None:
            self._on_navigate(
                WorkspaceDeepLink(WorkspaceId.ROOM, "geometry")
            )

    def _open_current_journey_step(self) -> None:
        step = current_journey_step(self._journey_steps)
        if step is not None:
            self._open_journey_step(step.key)

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
        except EXPECTED_OPERATION_ERRORS as exc:
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
            except EXPECTED_OPERATION_ERRORS as exc:
                self._operation_error_notice(
                    f"読み込みに失敗しました · {file_path.name}", exc
                )
                return
        try:
            items = self.controller.stage_rew_text_files(files)
        except EXPECTED_OPERATION_ERRORS as exc:
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
                MAX_ATTACHMENT_BYTES,
                label="ソース添付",
            )
            self.controller.attach_to_batch_item(
                item_id,
                filename=file_path.name,
                raw_bytes=raw,
                kind=kind,
            )
        except EXPECTED_OPERATION_ERRORS as exc:
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

    def _discard_selected_batch_items(self) -> None:
        """Drop the selected uncommitted queue rows (REV42).

        The queue is also fed by the REW automation — a parse-failed or
        unwanted row could previously never be removed (it fails commit
        forever and only ``discard_batch_committed`` existed). Every
        cell carries the row's item id in ``UserRole``.
        """
        item_ids = {
            item.data(Qt.ItemDataRole.UserRole)
            for item in self.batch_table.selectedItems()
            if item.data(Qt.ItemDataRole.UserRole)
        }
        if not item_ids:
            self._set_notice(
                "削除する行を一覧で選択してください。",
                SemanticState.WARNING,
            )
            return
        removed = self.controller.discard_batch_items(item_ids)
        self._set_notice(
            f"読み込みキューの項目を {removed} 件取り除きました。",
            SemanticState.SUCCESS,
        )
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
        except EXPECTED_OPERATION_ERRORS as exc:
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
            batch_tooltips = (
                None,
                status_explanation(item.status),
                None,
                None,
                status_explanation(item.duplicate_kind),
                status_explanation(item.resolution),
                None,
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setData(Qt.ItemDataRole.UserRole, item.item_id)
                tip = batch_tooltips[column]
                if tip:
                    cell.setToolTip(tip)
                self.batch_table.setItem(row_index, column, cell)
            if (
                item.status == "staged"
                and item.duplicate_kind in ("exact_duplicate", "same_acquisition")
            ):
                combo = QComboBox(self.batch_table)
                combo.addItem("既存の測定を利用", "reuse_existing")
                combo.addItem("別の測定として保存", "import_as_new")
                explain_combo_items(combo)
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
        self.text_import_button.setToolTip("PC上のREW周波数応答テキスト (.txt/.mdat) を選んで一時領域へ読み込みます。")
        self.text_import_button.setWhatsThis("PC上のREW周波数応答テキスト (.txt/.mdat) を選んで一時領域へ読み込みます。")
        self.text_import_button.clicked.connect(self.import_rew_text_dialog)
        button_row.addWidget(self.text_import_button)

        self.rew_refresh_button = QPushButton("REW一覧を更新", source_card)
        self.rew_refresh_button.setToolTip("REWアプリが公開している測定一覧を最新状態に読み直します。REW側で測定を追加した直後に押してください。")
        self.rew_refresh_button.setWhatsThis("REWアプリが公開している測定一覧を最新状態に読み直します。REW側で測定を追加した直後に押してください。")
        self.rew_refresh_button.clicked.connect(self._refresh_rew_async)
        button_row.addWidget(self.rew_refresh_button)
        button_row.addStretch(1)
        source_layout.addLayout(button_row)

        rew_row = QHBoxLayout()
        self.rew_combo = QComboBox(source_card)
        self.rew_combo.setMinimumContentsLength(32)
        self.rew_combo.setToolTip("REWアプリが公開している測定の一覧です。読み込むものを選んでください。")
        self.rew_combo.setWhatsThis("REWアプリが公開している測定の一覧です。読み込むものを選んでください。")
        rew_row.addWidget(self.rew_combo, 1)
        self.rew_read_button = QPushButton("選択したREWを読み込む", source_card)
        self.rew_read_button.setToolTip("左の一覧で選択中のREW測定を一時領域へ読み込みます。プレビューで内容を確認できます。")
        self.rew_read_button.setWhatsThis("左の一覧で選択中のREW測定を一時領域へ読み込みます。プレビューで内容を確認できます。")
        self.rew_read_button.clicked.connect(self._read_rew_async)
        rew_row.addWidget(self.rew_read_button)
        source_layout.addLayout(rew_row)

        # REV40-REWAUTO: live connection state + one-click launch — the
        # automation status line replaces the old implicit "press 更新 and
        # hope" contract; the launch button only appears when the API is
        # unreachable AND a local REW install was found.
        rew_status_row = QHBoxLayout()
        self.rew_status_label = QLabel(
            "REWとの接続を確認しています…", source_card
        )
        self.rew_status_label.setObjectName("rewAutoStatusLabel")
        set_typography_role(self.rew_status_label, TypographyRole.SECONDARY)
        self.rew_status_label.setWordWrap(True)
        rew_status_row.addWidget(self.rew_status_label, 1)
        self.rew_launch_button = QPushButton("REWを起動して接続", source_card)
        self.rew_launch_button.setObjectName("rewLaunchButton")
        self.rew_launch_button.setToolTip(
            "このPCのREWをAPI有効で起動します。起動後は新しい測定を自動で読み込みます。"
        )
        self.rew_launch_button.setWhatsThis(
            "このPCのREWをAPI有効で起動します。起動後は新しい測定を自動で読み込みます。"
        )
        self.rew_launch_button.clicked.connect(self._launch_rew_clicked)
        self.rew_launch_button.setVisible(False)
        rew_status_row.addWidget(self.rew_launch_button)
        source_layout.addLayout(rew_status_row)
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
        self.batch_add_button.setToolTip("複数のREWテキストファイルを一度に選んで読み込みキューへ追加します。保存前に各行の状態を確認できます。")
        self.batch_add_button.setWhatsThis("複数のREWテキストファイルを一度に選んで読み込みキューへ追加します。保存前に各行の状態を確認できます。")
        self.batch_add_button.clicked.connect(self.import_rew_batch_dialog)
        batch_buttons.addWidget(self.batch_add_button)
        self.batch_attach_kind_combo = QComboBox(batch_card)
        self.batch_attach_kind_combo.setAccessibleName("添付種別")
        self.batch_attach_kind_combo.setToolTip("「選択項目に添付を追加」で付けるファイルの種別を選びます。")
        self.batch_attach_kind_combo.setWhatsThis("「選択項目に添付を追加」で付けるファイルの種別を選びます。")
        for kind in MEASUREMENT_ATTACHMENT_KINDS:
            self.batch_attach_kind_combo.addItem(_attachment_kind_label(kind), kind)
        batch_buttons.addWidget(self.batch_attach_kind_combo)
        self.batch_attach_button = QPushButton("選択項目に添付を追加", batch_card)
        self.batch_attach_button.setToolTip("一覧で選択中の行に、左の種別の添付ファイル（写真・メモなど）を追加します。")
        self.batch_attach_button.setWhatsThis("一覧で選択中の行に、左の種別の添付ファイル（写真・メモなど）を追加します。")
        self.batch_attach_button.clicked.connect(self._attach_to_selected_batch_item)
        batch_buttons.addWidget(self.batch_attach_button)
        self.batch_discard_button = QPushButton("選択項目を削除", batch_card)
        self.batch_discard_button.setToolTip("一覧で選択中の未保存の行を読み込みキューから取り除きます（保存済みの行は取り除けません）。")
        self.batch_discard_button.setWhatsThis("一覧で選択中の未保存の行を読み込みキューから取り除きます（保存済みの行は取り除けません）。")
        self.batch_discard_button.clicked.connect(self._discard_selected_batch_items)
        batch_buttons.addWidget(self.batch_discard_button)
        self.batch_clear_button = QPushButton("保存済みをクリア", batch_card)
        self.batch_clear_button.setToolTip("すでに保存済みとして確定した行を一覧から取り除きます（保存したデータ自体は削除されません）。")
        self.batch_clear_button.setWhatsThis("すでに保存済みとして確定した行を一覧から取り除きます（保存したデータ自体は削除されません）。")
        self.batch_clear_button.clicked.connect(self._clear_committed_batch)
        batch_buttons.addWidget(self.batch_clear_button)
        self.batch_cancel_button = QPushButton("保存をキャンセル", batch_card)
        self.batch_cancel_button.clicked.connect(self._cancel_batch_commit)
        self.batch_cancel_button.setVisible(False)
        batch_buttons.addWidget(self.batch_cancel_button)
        batch_buttons.addStretch(1)
        batch_layout.addLayout(batch_buttons)

        self.batch_table = QTableWidget(0, 7, batch_card)
        self.batch_table.setToolTip(
            "読み込みキューの一覧です。列の見出しにカーソルを合わせると各列の説明が表示されます。"
        )
        self.batch_table.setHorizontalHeaderLabels(
            ["ファイル", "状態", "帯域", "位相", "重複", "解決", "保存先"]
        )
        for _col, _tip in enumerate((
            "読み込んだREWファイル名",
            "行の状態（読み込み済み/エラー/保存済みなど）",
            "測定の周波数帯域（Hz）",
            "位相情報の有無",
            "保存済みデータとの重複の有無",
            "重複時の扱い（上書き・別名保存・スキップ）を選びます",
            "割り当て先の測定点・座席",
        )):
            self.batch_table.horizontalHeaderItem(_col).setToolTip(_tip)
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
            lambda cancel_event: self.controller.list_rew_measurements(
                cancel_event=cancel_event
            ),
            self._apply_rew_list,
            "REW一覧の読み込みに失敗しました",
            on_retry=self._refresh_rew_async,
            purpose="rew_list",
        )

    def _apply_rew_list(self, value: object, *, announce: bool = True) -> None:
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
        if announce:
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
            lambda cancel_event: self.controller.fetch_rew_snapshot(
                measurement_uuid, cancel_event=cancel_event
            ),
            self._stage_rew_snapshot,
            "REW測定の読み込みに失敗しました",
            on_retry=self._read_rew_async,
            purpose="rew_read",
        )

    def _stage_rew_snapshot(self, value: object) -> None:
        try:
            self.controller.stage_rew_snapshot(value)  # type: ignore[arg-type]
        except EXPECTED_OPERATION_ERRORS as exc:
            self._operation_error_notice("REW測定の確認に失敗しました", exc)
            return
        self._set_notice(
            "読み込みました。次に「割り当て」で測定点と入力役割を確認してください。",
            SemanticState.SUCCESS,
        )
        self.refresh()

    # -- REV40-REWAUTO: REW automation loop ------------------------------
    #
    # The loop polls the REW API while this page is mounted and automates
    # the steps the operator used to click by hand: detecting that REW is
    # up (status line), offering a one-click API-enabled launch, staging
    # new measurements into the batch queue, and name-matching a target
    # when exactly one fits. Staged items go through the exact same
    # stage→evidence promotion gate as manual imports — automation covers
    # the clicking, not the authority.

    def mount_activated(self) -> None:
        """Mount on_activate: refresh + (re)start the REW automation loop."""
        self.refresh()
        self._start_rew_auto()

    def deactivate_rew_auto(self) -> None:
        """Mount on_deactivate: stop polling — the page is not visible."""
        self._rew_timer.stop()

    def _pref(self, key: str, default: object) -> object:
        store = self._preferences
        if store is None:
            return default
        try:
            return store.get(key)
        except EXPECTED_OPERATION_ERRORS:
            # Preference values are presentation state: an invalid stored
            # value falls back to the design default by contract, so this
            # degrade is silent on purpose (#815).
            return default

    def _rew_port(self) -> int:
        client = getattr(self.controller, 'rew_client', None)
        base_url = getattr(client, 'base_url', None)
        if isinstance(base_url, str):
            port = urlparse(base_url).port
            if isinstance(port, int):
                return port
        pref_port = self._pref('integrations.rew_port', 4735)
        return pref_port if isinstance(pref_port, int) else 4735

    def _start_rew_auto(self) -> None:
        if self._disposed or getattr(self.controller, 'rew_client', None) is None:
            self._set_rew_state('unavailable')
            return
        if self._rew_state not in ('launching',):
            self._set_rew_state('connecting')
        if not self._rew_timer.isActive():
            self._rew_timer.start()
        self._rew_auto_tick()

    def _resolve_rew_install(self) -> 'RewInstall | None':
        self._rew_install = find_rew_install(
            install_path=str(
                self._pref('integrations.rew_install_path', '') or ''
            )
        )
        return self._rew_install

    def _set_rew_state(self, state: str) -> None:
        self._rew_state = state
        auto_ingest = bool(self._pref('integrations.rew_auto_ingest', True))
        if state == 'connected':
            text = (
                "REW接続中 — 新しい測定は自動で読み込みキューへ追加します"
                if auto_ingest
                else "REW接続中 — 自動読み込みは設定でオフです"
            )
            self.rew_status_label.setText(text)
            set_semantic_state(self.rew_status_label, SemanticState.SUCCESS)
            self.rew_launch_button.setVisible(False)
        elif state == 'launching':
            self.rew_status_label.setText(
                "REWを起動しました — 接続を確認しています…"
            )
            set_semantic_state(self.rew_status_label, None)
            self.rew_launch_button.setVisible(False)
        elif state == 'connecting':
            self.rew_status_label.setText("REWとの接続を確認しています…")
            set_semantic_state(self.rew_status_label, None)
            self.rew_launch_button.setVisible(False)
        elif state == 'launchable':
            install_name = (
                self._rew_install.display if self._rew_install else 'REW'
            )
            self.rew_status_label.setText(
                f"REWが起動していません — 「{self.rew_launch_button.text()}」で"
                f"{install_name}をAPI有効で起動できます"
            )
            set_semantic_state(self.rew_status_label, SemanticState.WARNING)
            self.rew_launch_button.setVisible(True)
        else:  # 'unavailable'
            self.rew_status_label.setText(
                "REWが見つかりません — REWを起動してAPIを有効にするか、"
                "設定の「REWのインストール場所」で場所を指定してください"
            )
            set_semantic_state(self.rew_status_label, SemanticState.WARNING)
            self.rew_launch_button.setVisible(False)

    def _launch_rew_clicked(self) -> None:
        install = self._rew_install or self._resolve_rew_install()
        if install is None:
            self._set_notice(
                "REWのインストール場所が見つかりません。設定の「REWのインストール場所」で指定してください。",
                SemanticState.WARNING,
            )
            return
        try:
            self._rew_launcher(install, port=self._rew_port())
        except EXPECTED_OPERATION_ERRORS as exc:
            self._operation_error_notice("REWの起動に失敗しました", exc)
            return
        self._rew_launch_deadline = monotonic() + self._rew_launch_timeout_s
        self._set_rew_state('launching')
        # Poll faster while waiting for the freshly-launched API to come up.
        self._rew_timer.setInterval(1500)
        if not self._rew_timer.isActive():
            self._rew_timer.start()
        self._rew_auto_tick()

    def _start_rew_auto_job(self, call: Callable[[Event], object]) -> None:
        """Register a background auto job — same pool, excluded from the
        navigation busy gate, errors folded into the result dict so the
        generic retry-modal path never fires for a background poll."""
        if self._disposed:
            return
        key = uuid4().hex
        self._rew_auto_job_keys.add(key)
        self._job_handlers[key] = (
            self._apply_rew_auto_result, 'REW自動処理に失敗しました', None
        )
        self._job_purpose[key] = 'rew_auto'
        self._latest_job_key['rew_auto'] = key
        try:
            self._job_pool.start(
                key,
                lambda cancel_event: call(cancel_event),
                self._job_completed,
            )
        except Exception:  # error-boundary: teardown — clear leaked job key, re-raise
            # A failed start must not leak the key — a phantom auto job
            # would undercount _user_busy_count forever and wedge
            # navigation with a "background processing" gate that never
            # clears.
            self._rew_auto_job_keys.discard(key)
            self._job_handlers.pop(key, None)
            self._job_purpose.pop(key, None)
            if self._latest_job_key.get('rew_auto') == key:
                self._latest_job_key.pop('rew_auto', None)
            raise

    def _rew_auto_tick(self) -> None:
        if self._disposed or getattr(self.controller, 'rew_client', None) is None:
            return
        if self._rew_auto_job_keys:
            return  # one auto poll in flight at a time
        if (
            self._rew_state == 'launching'
            and self._rew_launch_deadline is not None
            and monotonic() > self._rew_launch_deadline
        ):
            # Launch never produced a reachable API — back to the offer.
            self._rew_launch_deadline = None
            self._rew_timer.setInterval(self._rew_auto_interval_ms)
            self._set_rew_state('launchable')
        auto_ingest = bool(self._pref('integrations.rew_auto_ingest', True))
        watch_dir = str(self._pref('integrations.rew_watch_dir', '') or '').strip()
        if watch_dir:
            watch_root = Path(watch_dir)
            if watch_root != self._rew_watch_root:
                # A changed watch path is a new watch epoch: everything in
                # the directory pre-dates it, so re-baseline instead of
                # letting another directory's markers stage or dedupe
                # files (opt-in contract — files present when watching
                # starts are never staged).
                self._rew_watch_seen.clear()
                self._rew_watch_pending.clear()
                self._rew_watch_failures.clear()
                self._rew_watch_root = watch_root
        else:
            self._rew_watch_root = None
        seen_uuids = self._rew_seen_uuids
        uuids_known = self._rew_uuids_known
        watch_seen = self._rew_watch_seen
        watch_pending = self._rew_watch_pending
        fetch_failures = self._rew_fetch_failures
        controller = self.controller

        def work(cancel_event: Event) -> dict[str, Any]:
            result: dict[str, Any] = {
                'rows': None,
                'error': None,
                'snapshots': (),
                'fetched_uuids': (),
                'watch_files': (),
                'watch_skipped': (),
                'needs_scene': False,
                'watch_dir_missing': False,
            }
            if watch_dir:
                try:
                    files, skipped = scan_rew_watch_dir(
                        watch_dir, watch_seen, watch_pending
                    )
                except OSError:
                    result['watch_dir_missing'] = True
                else:
                    result['watch_files'] = files
                    result['watch_skipped'] = skipped
            try:
                rows = controller.list_rew_measurements(
                    cancel_event=cancel_event
                )
            except EXPECTED_OPERATION_ERRORS as exc:
                result['error'] = exc
                return result
            result['rows'] = rows
            if auto_ingest and uuids_known:
                new_uuids = [
                    str(row.get('uuid'))
                    for row in rows
                    if isinstance(row, dict)
                    and isinstance(row.get('uuid'), str)
                    and row.get('uuid')
                    and str(row.get('uuid')) not in seen_uuids
                ]
                if new_uuids:
                    try:
                        controller.latest_revision()
                    except MeasurementWorkflowError:
                        # Staging pins the current scene head — without a
                        # saved room there is nothing honest to stage into.
                        # Store/adapter failures propagate to the job-error
                        # path instead of reading as 'no scene' (#815).
                        result['needs_scene'] = True
                    else:
                        snapshots = []
                        fetched: list[str] = []
                        fetch_failed: list[str] = []
                        for measurement_uuid in new_uuids:
                            if cancel_event.is_set():
                                break
                            try:
                                snapshots.append(
                                    controller.fetch_rew_snapshot(
                                        measurement_uuid,
                                        cancel_event=cancel_event,
                                    )
                                )
                                fetched.append(measurement_uuid)
                                fetch_failures.pop(measurement_uuid, None)
                            except EXPECTED_OPERATION_ERRORS:
                                # Transient failures retry next tick, but a
                                # fetch that keeps failing (undecodable or
                                # removed mid-poll) must not re-request every
                                # tick forever — mark it seen like a wedged
                                # watch file and report it once.
                                attempts = (
                                    fetch_failures.get(measurement_uuid, 0) + 1
                                )
                                fetch_failures[measurement_uuid] = attempts
                                if attempts >= _REW_AUTO_MAX_ATTEMPTS:
                                    seen_uuids.add(measurement_uuid)
                                    fetch_failed.append(measurement_uuid)
                                continue
                        result['snapshots'] = tuple(snapshots)
                        result['fetched_uuids'] = tuple(fetched)
                        result['fetch_failed_uuids'] = tuple(fetch_failed)
            return result

        self._start_rew_auto_job(work)

    def _apply_rew_auto_result(self, value: object) -> None:
        result = value if isinstance(value, dict) else {}
        rows = result.get('rows')
        error = result.get('error')
        if rows is not None:
            first_list = not self._rew_uuids_known
            self._rew_uuids_known = True
            listed = {
                str(row.get('uuid'))
                for row in rows
                if isinstance(row, dict)
                and isinstance(row.get('uuid'), str)
                and row.get('uuid')
            }
            if first_list:
                # Baseline: only measurements appearing *after* the watch
                # started are 'new' — the existing REW library must never
                # silently flood the queue.
                self._rew_seen_uuids.update(listed)
            # Failure counts for uuids no longer listed are dead weight —
            # the map stays bounded by the current library.
            for stale_uuid in [
                uuid
                for uuid in self._rew_fetch_failures
                if uuid not in listed
            ]:
                del self._rew_fetch_failures[stale_uuid]
            self._apply_rew_list(rows, announce=False)
            self._set_rew_state('connected')
            self._rew_timer.setInterval(self._rew_auto_interval_ms)
        elif error is not None:
            if self._rew_state == 'launching':
                # Deadline enforcement happens at tick start; still waiting.
                pass
            elif (
                bool(self._pref('integrations.rew_auto_launch', True))
                and self._resolve_rew_install() is not None
            ):
                self._set_rew_state('launchable')
            else:
                self._set_rew_state('unavailable')

        staged_labels: list[str] = []
        applied_count = 0
        unresolved_count = 0
        needs_scene = bool(result.get('needs_scene'))
        snapshots = result.get('snapshots') or ()
        fetch_failed = [
            str(measurement_uuid)
            for measurement_uuid in (result.get('fetch_failed_uuids') or ())
        ]
        # A stage failure this tick is not a 'quiet' tick: its dedupe key
        # must survive past the notice branches below or the next failing
        # tick would re-shout the same warning.
        stage_failed_this_tick = False
        if snapshots:
            try:
                items = self.controller.stage_rew_snapshots(snapshots)
            except EXPECTED_OPERATION_ERRORS as exc:
                # Fetched uuids stay unseen so the next poll retries —
                # marking them seen here would silently drop the
                # measurements the user was told would auto-load. The
                # notice dedupes: a persistent failure must not clobber
                # the operator's notice area every poll tick.
                stage_failed_this_tick = True
                if 'stage_failed' not in self._rew_auto_notice_keys:
                    self._rew_auto_notice_keys.add('stage_failed')
                    self._operation_error_notice(
                        "REW測定の自動読み込みに失敗しました", exc
                    )
            else:
                self._rew_seen_uuids.update(
                    result.get('fetched_uuids') or ()
                )
                applied, unresolved = self._auto_assign_safely(items)
                applied_count += applied
                unresolved_count += unresolved
                staged_labels.extend(
                    f"REW「{item.filename}」" for item in items
                )
        watch_files = result.get('watch_files') or ()
        if watch_files:
            watch_dir = str(
                self._pref('integrations.rew_watch_dir', '') or ''
            ).strip()
            try:
                self.controller.latest_revision()
            except MeasurementWorkflowError:
                # No saved scene yet — the snapshot path already reports
                # this via needs_scene; watch files re-queue and stage
                # once a scene exists instead of dropping silently.
                # Store/adapter failures stay loud via the job error path.
                needs_scene = True
                self._unmark_watch_files(watch_dir, watch_files)
            else:
                try:
                    items = self.controller.stage_rew_text_files(watch_files)
                except EXPECTED_OPERATION_ERRORS as exc:
                    stage_failed_this_tick = True
                    wedged = self._fail_watch_files(watch_dir, watch_files)
                    if wedged:
                        self._auto_notice_once(
                            'watch_wedged:' + ','.join(sorted(wedged)),
                            f"監視フォルダーのREWテキスト {len(wedged)} 件は"
                            f"繰り返し読み込みに失敗したためスキップします"
                            f"（{wedged[0]} など）。ファイルを修正すると"
                            "再度読み込みます。",
                        )
                    elif 'watch_stage_failed' not in self._rew_auto_notice_keys:
                        self._rew_auto_notice_keys.add('watch_stage_failed')
                        self._operation_error_notice(
                            "監視フォルダーのREWテキスト読み込みに失敗しました",
                            exc,
                        )
                else:
                    for _raw, filename in watch_files:
                        self._rew_watch_failures.pop(
                            str(Path(watch_dir) / filename), None
                        )
                    applied, unresolved = self._auto_assign_safely(items)
                    applied_count += applied
                    unresolved_count += unresolved
                    staged_labels.extend(item.filename for item in items)

        watch_skipped = [
            str(name) for name in (result.get('watch_skipped') or ())
        ]

        if staged_labels:
            self._rew_auto_notice_keys.clear()
            detail = f"新しい測定 {len(staged_labels)} 件を読み込みキューへ追加しました"
            if applied_count:
                detail += f"（{applied_count} 件は測定点を自動割り当て）"
            if unresolved_count:
                detail += f"・{unresolved_count} 件は「割り当て」で測定点を確認してください"
            if watch_skipped:
                detail += f"・{len(watch_skipped)} 件は読み込めませんでした"
            if fetch_failed:
                detail += (
                    f"・{len(fetch_failed)} 件は繰り返し取得に失敗したため"
                    "スキップしました"
                )
            self._set_notice(detail + "。", SemanticState.SUCCESS)
            self._report_auto_ingest(
                f"REW自動取り込み: {len(staged_labels)} 件",
                detail,
            )
            self.refresh()
        elif fetch_failed:
            labels = self._rew_row_labels(rows, fetch_failed)
            self._auto_notice_once(
                'fetch_failed:' + ','.join(sorted(fetch_failed)),
                f"REW測定 {len(fetch_failed)} 件を繰り返し取得できませんでした"
                f"（{labels[0]} など）ためスキップします。REW側の測定データを"
                "確認してください。",
            )
        elif needs_scene:
            self._auto_notice_once(
                'needs_scene',
                "新しいREW測定を検出しましたが、保存済みの部屋がありません。"
                "部屋を保存すると次回の確認で自動で読み込みます。",
            )
        elif watch_skipped:
            self._auto_notice_once(
                'watch_skipped:' + ','.join(sorted(watch_skipped)),
                f"監視フォルダーのREWテキスト {len(watch_skipped)} 件を読み込めませんでした"
                f"（{watch_skipped[0]} など）。サイズ上限または読み取りエラーを確認してください。",
            )
        elif result.get('watch_dir_missing'):
            self._auto_notice_once(
                'watch_dir_missing',
                "REW監視フォルダーが見つかりません。設定の「REWテキストの監視フォルダー」を確認してください。",
            )
        elif stage_failed_this_tick:
            # A failure notice already fired (or deduped) this tick — its
            # key must survive so the next failing tick stays deduped.
            pass
        else:
            # A resolved or merely quiet tick clears every key except
            # 'needs_scene' while no scene exists: a re-queued watch file
            # alternates delivery/non-delivery ticks, and clearing on the
            # quiet tick re-shouts the warning every other poll (REV41
            # e2e finding). Other keys re-arm the next time their
            # condition actually appears.
            try:
                self.controller.latest_revision()
            except MeasurementWorkflowError:
                self._rew_auto_notice_keys.intersection_update(
                    {'needs_scene'}
                )
            else:
                self._rew_auto_notice_keys.clear()

    def _auto_notice_once(self, key: str, message: str) -> None:
        """Show a persistent auto-poll warning once per condition run.

        Without the key guard a missing watch dir re-writes the same
        warning every poll tick, clobbering whatever notice the operator
        was reading. The key clears when the condition resolves.
        """
        if key in self._rew_auto_notice_keys:
            return
        self._rew_auto_notice_keys.add(key)
        self._set_notice(message, SemanticState.WARNING)

    def _auto_assign_safely(
        self, items: tuple
    ) -> tuple[int, int]:
        """Run name-match auto-assign without letting its failure abort
        the apply — targets derive from the scene revision, so a scene
        read error would otherwise escape to ``sys.excepthook`` every
        poll tick while the staged rows sat unnoticed."""
        try:
            return self.controller.auto_assign_batch_items(
                item_ids=[item.item_id for item in items]
            )
        except EXPECTED_OPERATION_ERRORS as exc:
            self._operation_error_notice(
                "REW測定点の自動割り当てに失敗しました", exc
            )
            # Untried rows need a manual assignment check — report them as
            # unresolved so the success detail still points at 「割り当て」.
            return 0, len(items)

    def _unmark_watch_files(
        self, watch_dir: str, watch_files: tuple
    ) -> None:
        """Re-queue delivered watch files whose staging had to wait.

        ``scan_rew_watch_dir`` marks a file seen on delivery; dropping the
        marker re-enters it as a candidate so the next scans retry instead
        of silently losing the drop. Used when staging is temporarily
        impossible (no saved scene yet) — a stage *failure* goes through
        ``_fail_watch_files`` so a deterministically bad file stops
        retrying after ``_REW_AUTO_MAX_ATTEMPTS``.
        """
        root = Path(watch_dir)
        for _raw, filename in watch_files:
            self._rew_watch_seen.pop(str(root / filename), None)

    def _fail_watch_files(
        self, watch_dir: str, watch_files: tuple
    ) -> list[str]:
        """Re-queue watch files whose staging failed, with a retry cap.

        Each failed stage re-enters the file unless it has already failed
        ``_REW_AUTO_MAX_ATTEMPTS`` times; a wedged file keeps its seen
        marker so it stops cycling through scan→stage→fail every other
        poll, and a later rewrite still re-delivers it (the signature
        change reads as new). Returns the names that hit the cap.
        """
        root = Path(watch_dir)
        wedged: list[str] = []
        for _raw, filename in watch_files:
            key = str(root / filename)
            attempts = self._rew_watch_failures.get(key, 0) + 1
            self._rew_watch_failures[key] = attempts
            if attempts >= _REW_AUTO_MAX_ATTEMPTS:
                wedged.append(filename)
            else:
                self._rew_watch_seen.pop(key, None)
        return wedged

    def _rew_row_labels(
        self, rows: object, uuids: list[str]
    ) -> list[str]:
        """Display names for measurement uuids from the last listed rows."""
        titles: dict[str, str] = {}
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                uuid = row.get('uuid')
                title = row.get('title')
                if isinstance(uuid, str) and uuid:
                    titles[uuid] = (
                        str(title).strip()
                        if isinstance(title, str) and title.strip()
                        else uuid
                    )
        return [titles.get(uuid, uuid) for uuid in uuids]

    def _report_auto_ingest(self, title: str, summary: str) -> None:
        """Surface the automated staging in the activity center (#activity).

        Best-effort: the batch rows themselves are the durable surface, so
        a missing or rejecting center never blocks the ingest.
        """
        center = self._activity_center
        if center is None:
            return
        try:
            from ...activity_center import OperationClass

            operation_id = center.submit(
                operation_kind='rew_auto_ingest',
                operation_class=OperationClass.EXTERNAL_IO,
                title=title,
                deep_link=WorkspaceDeepLink(WorkspaceId.MEASUREMENT, 'import'),
            )
            center.mark_running(operation_id)
            center.complete(operation_id, result_summary=summary)
        except EXPECTED_OPERATION_ERRORS as exc:
            # Best-effort reporting surface — degrade, never silently.
            report_boundary_failure(exc, operation='アクティビティ記録の登録')

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
        # is checked manually. REV44: the 登録 button is the production
        # write path that puts rows into cad_routing_profiles at all.
        routing_profile_row = QHBoxLayout()
        routing_profile_row.setContentsMargins(0, 0, 0, 0)
        self.routing_profile_combo = QComboBox(assign_card)
        self.routing_profile_combo.setMinimumContentsLength(24)
        routing_profile_row.addWidget(self.routing_profile_combo, 1)
        self.routing_profile_register_button = QPushButton("登録…", assign_card)
        self.routing_profile_register_button.setToolTip(
            "検証済みチャンネルマップを登録します。"
        )
        self.routing_profile_register_button.setWhatsThis(
            "検証済みチャンネルマップを登録します。"
        )
        self.routing_profile_register_button.clicked.connect(
            self._register_routing_profile
        )
        routing_profile_row.addWidget(self.routing_profile_register_button)
        form.addRow("ルーティングプロファイル", routing_profile_row)
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
        acquisition_form.addRow("マイクメーカー", self.mic_manufacturer_edit)
        self.mic_model_edit = QLineEdit(acquisition_card)
        acquisition_form.addRow("マイクモデル", self.mic_model_edit)
        self.mic_serial_edit = QLineEdit(acquisition_card)
        acquisition_form.addRow("マイクシリアル", self.mic_serial_edit)
        self.mic_sample_rate_edit = QLineEdit(acquisition_card)
        self.mic_sample_rate_edit.setPlaceholderText("例: 48000")
        acquisition_form.addRow("マイクサンプルレート", self.mic_sample_rate_edit)
        self.mic_cal_file_edit = QLineEdit(acquisition_card)
        cal_file_row = QHBoxLayout()
        cal_file_row.setContentsMargins(0, 0, 0, 0)
        cal_file_row.addWidget(self.mic_cal_file_edit, 1)
        self.cal_file_pick_button = QPushButton("ファイルを選択…", acquisition_card)
        self.cal_file_pick_button.setToolTip("マイク校正ファイルを選び、バイト列を証拠として添付します。ファイル名とSHA-256が自動で入力されます。")
        self.cal_file_pick_button.setWhatsThis("マイク校正ファイルを選び、バイト列を証拠として添付します。ファイル名とSHA-256が自動で入力されます。")
        self.cal_file_pick_button.clicked.connect(
            self._retain_calibration_file_dialog
        )
        cal_file_row.addWidget(self.cal_file_pick_button)
        acquisition_form.addRow("校正ファイル名", cal_file_row)
        self.mic_cal_sha_edit = QLineEdit(acquisition_card)
        self.mic_cal_sha_edit.setPlaceholderText("SHA-256（64桁16進数）")
        acquisition_form.addRow("校正ファイルSHA-256", self.mic_cal_sha_edit)

        # REV44: a persisted timing-reference authority can be bound to this
        # capture; the 登録 button is its production write path. Selecting a
        # reference that can authorize common timing derives the flat context
        # fields (clock identity, declared sample rate, typed delay sum) the
        # repository enforces consistency against.
        timing_reference_row = QHBoxLayout()
        timing_reference_row.setContentsMargins(0, 0, 0, 0)
        self.timing_reference_combo = QComboBox(acquisition_card)
        self.timing_reference_combo.setMinimumContentsLength(24)
        self.timing_reference_combo.setToolTip(
            "共通タイミングの権威となるタイミング基準を選択します。"
        )
        self.timing_reference_combo.activated.connect(
            self._timing_reference_changed
        )
        timing_reference_row.addWidget(self.timing_reference_combo, 1)
        self.timing_reference_button = QPushButton("登録…", acquisition_card)
        self.timing_reference_button.setToolTip(
            "新しいタイミング基準を登録します。"
        )
        self.timing_reference_button.setWhatsThis(
            "新しいタイミング基準を登録します。"
        )
        self.timing_reference_button.clicked.connect(
            self._register_timing_reference
        )
        timing_reference_row.addWidget(self.timing_reference_button)
        acquisition_form.addRow("タイミング基準", timing_reference_row)

        # REV44: acquisition-scope identities. The session id groups captures
        # of one sitting; the signal-path fingerprint is what a persistent
        # timing reference scopes to; the input-path identity is what an
        # instrument-scope level calibration scopes to. Leaving them empty is
        # honest — the context then simply cannot match a scoped authority.
        self.acquisition_session_edit = QLineEdit(acquisition_card)
        self.acquisition_session_edit.setPlaceholderText("例: sess-2026-10-04")
        acquisition_form.addRow("取得セッション識別子", self.acquisition_session_edit)
        self.signal_path_edit = QLineEdit(acquisition_card)
        self.signal_path_edit.setPlaceholderText(
            "例: HDMI-AVR→UMIK-1/USB（デバイス・経路・クロック構成の指紋）"
        )
        acquisition_form.addRow("信号パス識別子", self.signal_path_edit)
        self.input_path_edit = QLineEdit(acquisition_card)
        self.input_path_edit.setPlaceholderText("例: UMIK-1/ch1")
        acquisition_form.addRow("入力パス識別子", self.input_path_edit)

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
        except EXPECTED_OPERATION_ERRORS as exc:
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
        except EXPECTED_OPERATION_ERRORS as exc:
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
        except EXPECTED_OPERATION_ERRORS as exc:
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

        # REV44: persisted timing-reference authorities offered for binding.
        previous_timing = self.timing_reference_combo.currentData()
        self.timing_reference_combo.clear()
        self.timing_reference_combo.addItem("（未選択）", None)
        try:
            timing_references = (
                self.controller.quality_repository.list_timing_references()
            )
        except EXPECTED_OPERATION_ERRORS as exc:
            timing_references = ()
            self._operation_error_notice(
                "タイミング基準を読み込めませんでした",
                exc,
                effect=None,
                severity=SemanticState.WARNING,
            )
        for reference in timing_references:
            self.timing_reference_combo.addItem(
                f"{timing_method_label(reference.method)} "
                f"· {reference.created_at_utc}",
                reference.timing_reference_id,
            )
        if previous_timing is not None:
            index = self.timing_reference_combo.findData(previous_timing)
            if index >= 0:
                self.timing_reference_combo.setCurrentIndex(index)

        # #471 persisted contexts offered as reusable presets.
        self.acquisition_preset_combo.clear()
        self.acquisition_preset_combo.addItem("（プリセットなし）", None)
        try:
            contexts = self.controller.acquisition_contexts()
        except EXPECTED_OPERATION_ERRORS as exc:
            contexts = ()
            self._operation_error_notice(
                "プリセットを読み込めませんでした",
                exc,
                effect=None,
                severity=SemanticState.WARNING,
            )
        for context in contexts:
            self.acquisition_preset_combo.addItem(
                f"{acquisition_source_kind_label(context.source_kind)} "
                f"· {context.created_at_utc}",
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
        except EXPECTED_OPERATION_ERRORS as exc:
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
        # REV44: reuse the bound timing authority and the scope identities —
        # a preset that silently dropped them would produce a weaker context.
        if context.timing_reference_id:
            index = self.timing_reference_combo.findData(
                context.timing_reference_id
            )
            if index >= 0:
                self.timing_reference_combo.setCurrentIndex(index)
        self.acquisition_session_edit.setText(
            context.acquisition_session_id or ""
        )
        self.signal_path_edit.setText(context.signal_path_identity or "")
        self.input_path_edit.setText(context.input_path_identity or "")

    def _timing_reference_changed(self) -> None:
        """Auto-fill scope fields the selected reference declares (REV44).

        A persistent reference's signal-path fingerprint and a declared
        session/rate are copied into the form so the persisted context
        actually matches the authority's scope; a field the user already
        filled is never clobbered.
        """
        reference_id = self.timing_reference_combo.currentData()
        if not isinstance(reference_id, str) or not reference_id:
            return
        try:
            reference = self.controller.quality_repository.get_timing_reference(
                reference_id
            )
        except EXPECTED_OPERATION_ERRORS as exc:
            if is_authority_failure(exc):
                raise
            # Autofill convenience — the form keeps what the user typed,
            # and the failure is logged rather than silent.
            report_boundary_failure(exc, operation='タイミング基準の確認')
            return
        if reference is None:
            return
        if (
            reference.signal_path_identity
            and not self.signal_path_edit.text().strip()
        ):
            self.signal_path_edit.setText(reference.signal_path_identity)
        if (
            reference.acquisition_session_id
            and not self.acquisition_session_edit.text().strip()
        ):
            self.acquisition_session_edit.setText(
                reference.acquisition_session_id
            )
        if (
            reference.sample_rate_hz is not None
            and not self.mic_sample_rate_edit.text().strip()
        ):
            self.mic_sample_rate_edit.setText(
                str(int(reference.sample_rate_hz))
            )

    def _register_timing_reference(self) -> None:
        dialog = TimingReferenceDialog(self, default_signal_path=(
            self.signal_path_edit.text().strip() or None
        ))
        if not dialog.exec():
            return
        record = dialog.record
        assert record is not None
        try:
            self.controller.quality_repository.save_timing_reference(record)
        except EXPECTED_OPERATION_ERRORS as exc:
            self._operation_error_notice(
                "タイミング基準を登録できませんでした", exc
            )
            return
        self._set_notice(
            "タイミング基準を登録しました — 「取得条件」で選択できます",
            SemanticState.SUCCESS,
        )
        self.refresh()

    def _register_routing_profile(self) -> None:
        dialog = RoutingProfileDialog(self.controller, self)
        if not dialog.exec():
            return
        record = dialog.record
        assert record is not None
        try:
            self.controller.quality_repository.save_routing_profile(record)
        except EXPECTED_OPERATION_ERRORS as exc:
            self._operation_error_notice(
                "ルーティングプロファイルを登録できませんでした", exc
            )
            return
        self._set_notice(
            "ルーティングプロファイルを登録しました — "
            "「ルーティングプロファイル」から選択できます",
            SemanticState.SUCCESS,
        )
        self.refresh()

    def _register_level_calibration(self) -> None:
        try:
            contexts = (
                self.controller.quality_repository.list_acquisition_contexts()
            )
        except EXPECTED_OPERATION_ERRORS as exc:
            contexts = ()
            self._operation_error_notice(
                "取得コンテキストを読み込めませんでした",
                exc,
                effect=None,
                severity=SemanticState.WARNING,
            )
        dialog = LevelCalibrationDialog(
            self._quality_views, contexts, self
        )
        if not dialog.exec():
            return
        record = dialog.record
        assert record is not None
        try:
            self.controller.quality_repository.save_level_calibration(record)
        except EXPECTED_OPERATION_ERRORS as exc:
            self._operation_error_notice(
                "レベル校正を登録できませんでした", exc
            )
            return
        self._set_notice(
            "レベル校正を登録しました — 絶対SPLを根拠づける校正は機器の準備で"
            "確認できます",
            SemanticState.SUCCESS,
        )
        self.refresh()

    def _register_stimulus_profile(self) -> None:
        dialog = StimulusProfileDialog(
            self.controller, self._quality_views, self
        )
        if not dialog.exec():
            return
        record = dialog.record
        assert record is not None
        try:
            self.controller.quality_repository.save_stimulus_profile(record)
        except EXPECTED_OPERATION_ERRORS as exc:
            self._operation_error_notice(
                "刺激プロファイルを登録できませんでした", exc
            )
            return
        self._set_notice(
            "刺激プロファイルを登録しました", SemanticState.SUCCESS
        )
        self.refresh()

    def _register_dataset_level_reference(self) -> None:
        row = self._selected_quality_view()
        if row is None or row.dataset_id is None:
            self._set_notice(
                "データセットのある測定を選んでください",
                SemanticState.WARNING,
            )
            return
        try:
            dataset = (
                self.controller.measurement_repository.dataset_for_measurement(
                    row.measurement_id
                )
            )
        except EXPECTED_OPERATION_ERRORS as exc:
            self._operation_error_notice(
                "データセットを確認できませんでした", exc
            )
            return
        if dataset is None:
            self._set_notice(
                "この測定のデータセットを確認できませんでした",
                SemanticState.WARNING,
            )
            return
        try:
            calibrations = (
                self.controller.quality_repository.list_level_calibrations()
            )
        except EXPECTED_OPERATION_ERRORS as exc:
            calibrations = ()
            self._operation_error_notice(
                "レベル校正を読み込めませんでした",
                exc,
                effect=None,
                severity=SemanticState.WARNING,
            )
        dialog = DatasetLevelReferenceDialog(row, dataset, calibrations, self)
        if not dialog.exec():
            return
        record = dialog.record
        assert record is not None
        try:
            self.controller.quality_repository.save_dataset_level_reference(
                record
            )
            # The pinned level authority changes the produced report's epoch:
            # re-derive so the report seals the new reference and the
            # absolute-SPL claim resolves against it.
            self.controller.reproduce_quality_report(row.measurement_id)
        except EXPECTED_OPERATION_ERRORS as exc:
            self._operation_error_notice(
                "レベル基準を登録できませんでした", exc
            )
            return
        self._set_notice(
            f"レベル基準を登録しました（"
            f"{level_reference_kind_label(record.level_reference_kind)}）",
            SemanticState.SUCCESS,
        )
        self.refresh()

    def _retain_calibration_file_dialog(self) -> None:
        """Attach a mic calibration file's bytes and bind its content hash.

        The onboarding checklist tells the user to record the filename
        *and attach the bytes*; this button is the attach affordance. The
        bytes land in the content-addressed managed-asset store through
        ``save_calibration_file`` and the returned SHA-256 fills the hash
        field, so the declared digest can actually resolve through
        ``validate_calibration_file`` (and therefore the quality
        producer's calibration claim) instead of pointing at nothing.
        """
        path, _ = file_dialog_memory.get_open_file_name(
            self,
            "マイク校正ファイルを選択",
            'measurement.calibration_file',
            "校正ファイル (*.txt *.csv *.frd *.cal);;すべてのファイル (*)",
        )
        if not path:
            return
        try:
            file_path = Path(path)
            raw = read_file_bounded(
                file_path,
                MAX_ATTACHMENT_BYTES,
                label="校正ファイル",
            )
            digest = self.controller.quality_repository.save_calibration_file(
                filename=file_path.name,
                raw_bytes=raw,
            )
        except EXPECTED_OPERATION_ERRORS as exc:
            self._operation_error_notice(
                "校正ファイルの保存に失敗しました", exc
            )
            return
        self.mic_cal_file_edit.setText(file_path.name)
        self.mic_cal_sha_edit.setText(digest)
        self._set_notice(
            f"校正ファイルを添付しました · {file_path.name} — 「測定を保存」で取得条件に含めてください",
            SemanticState.SUCCESS,
        )

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
            if mic_rate <= 0:
                return None, None, "マイクのサンプルレートは正の整数で入力してください"
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
            if not isfinite(volume_db):
                return None, None, "AVRボリュームは有限の数値（dB）で入力してください"

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

        # REV44: a bound timing-reference authority derives the flat context
        # fields the repository then cross-checks — the context may only
        # repeat what the sealed reference attests (declared clock identity,
        # declared rate, typed delay sum). ``timing_reference_valid`` is True
        # only when the bound method can support common timing at all; a
        # manual/imported reference binds evidence without upgrading it.
        timing_reference_id = self.timing_reference_combo.currentData()
        timing_kwargs: dict = {}
        timing_reference = None
        if isinstance(timing_reference_id, str) and timing_reference_id:
            try:
                timing_reference = (
                    self.controller.quality_repository.get_timing_reference(
                        timing_reference_id
                    )
                )
            except EXPECTED_OPERATION_ERRORS:
                return (
                    None,
                    None,
                    "選択したタイミング基準を確認できませんでした — 保存されていません",
                )
            if timing_reference is None:
                return (
                    None,
                    None,
                    "選択したタイミング基準が見つかりません — "
                    "保存されていません",
                )
            delay_total = (
                sum(
                    correction.value_s
                    for correction in timing_reference.delay_corrections
                )
                if timing_reference.delay_corrections
                else None
            )
            timing_kwargs = {
                "timing_reference_valid": (
                    True
                    if timing_reference_supports_common_timing(timing_reference)
                    else None
                ),
                "timing_reference_id": timing_reference.timing_reference_id,
                "timing_reference_sha256": timing_reference.timing_reference_sha256,
                "clock_source": (
                    timing_reference.input_clock_identity
                    or timing_reference.output_clock_identity
                    or timing_reference.reference_channel
                ),
                "delay_correction_s": delay_total,
            }
        acquisition_session_id = self._text_or_none(self.acquisition_session_edit)
        signal_path_identity = self._text_or_none(self.signal_path_edit)
        input_path_identity = self._text_or_none(self.input_path_edit)
        has_scope = any(
            (
                acquisition_session_id,
                signal_path_identity,
                input_path_identity,
                timing_reference is not None,
            )
        )
        if not has_mic and not has_playback and not has_scope:
            return None, direction, None
        return (
            AcquisitionCapture(
                source_kind="manual",
                microphone=microphone if has_mic else None,
                playback=playback if has_playback else None,
                # A bound authority dictates the context's declared rate —
                # the repository consistency check only allows repeating
                # what the sealed reference attests (#642).
                sample_rate_hz=(
                    int(timing_reference.sample_rate_hz)
                    if (
                        timing_reference is not None
                        and timing_reference.sample_rate_hz is not None
                    )
                    else mic_rate
                ),
                acquisition_session_id=acquisition_session_id,
                signal_path_identity=signal_path_identity,
                input_path_identity=input_path_identity,
                **timing_kwargs,
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
            divergence_help = box.addButton(
                "ヘルプ", QMessageBox.ButtonRole.HelpRole
            )
            while True:
                box.exec()
                if (
                    box.clickedButton() is divergence_help
                    and self._help_available()
                ):
                    self._show_help_topic('concept.scene_vs_revision')
                    continue
                break
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
            duplicate_box = QMessageBox(self)
            duplicate_box.setIcon(QMessageBox.Icon.Question)
            duplicate_box.setWindowTitle("同じ測定が既に保存されています")
            duplicate_box.setText(
                f"{_duplicate_kind_label(pending.duplicate_kind)}の測定が既に"
                f"保存されています（{duplicate_name}）。\n"
                "新しい測定としてもう一度保存しますか？"
            )
            duplicate_box.addButton(QMessageBox.StandardButton.Yes)
            duplicate_box.addButton(QMessageBox.StandardButton.No)
            duplicate_help = duplicate_box.addButton(
                "ヘルプ", QMessageBox.ButtonRole.HelpRole
            )
            duplicate_box.setDefaultButton(QMessageBox.StandardButton.No)
            while True:
                duplicate_result = duplicate_box.exec()
                if (
                    duplicate_box.clickedButton() is duplicate_help
                    and self._help_available()
                ):
                    # ヘルプを読んだあとで選び直せるよう、ボックスを再表示する。
                    self._show_help_topic('workflow.measurements')
                    continue
                break
            if duplicate_result != QMessageBox.StandardButton.Yes:
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
        except EXPECTED_OPERATION_ERRORS as exc:
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
            except EXPECTED_OPERATION_ERRORS as exc:
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
        if self._disposed:
            return
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
        except EXPECTED_OPERATION_ERRORS as exc:
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
        # The worker thread mutates shared _BatchEntry state while it runs —
        # the per-row resolution combo and item selection are GUI-side write
        # paths onto the same entries, so the whole table is inert during a
        # commit instead of racing the worker mid-flight.
        self.batch_table.setEnabled(not running)
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
        from ...workflow_help import ReasonTextDialog

        reason_dialog = ReasonTextDialog(
            self,
            "割り当ての訂正",
            "訂正の理由を記録してください（監査ログに残ります）:",
            on_help=(
                (lambda: self._show_help_topic('workflow.measurements'))
                if self._help_available()
                else None
            ),
        )
        ok = bool(reason_dialog.exec())
        reason = reason_dialog.text()
        if not ok or not reason.strip():
            self._set_notice("訂正の理由が必要です", SemanticState.WARNING)
            return
        try:
            self.controller.correct_assignment(measurement_id, corrected, reason)
        except EXPECTED_OPERATION_ERRORS as exc:
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
            ('calibration', "校正"),
            ('holdout', "ホールドアウト"),
            ('diagnostic', "診断"),
        ):
            self.campaign_purpose_combo.addItem(label, value)
        self.campaign_purpose_combo.currentIndexChanged.connect(
            lambda _: self._update_campaign_preview()
        )
        option_row.addWidget(self.campaign_purpose_combo)
        self.campaign_repeat = QSpinBox(plan_card)
        self.campaign_repeat.setAccessibleName("リピート回数")
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

        native_card, native_layout = _card(
            "HTDTネイティブ自動実行", host)
        native_hint = QLabel(
            "選択した計画を HTDT ネイティブ掃引エンジンで自動実行します。"
            "出力前にルーティング・レベル・位置を確認してアーム承認し、"
            "マイク位置の移動時のみ確認を求めます。",
            native_card,
        )
        native_hint.setWordWrap(True)
        set_typography_role(native_hint, TypographyRole.SECONDARY)
        native_layout.addWidget(native_hint)
        native_row = QHBoxLayout()
        self.campaign_native_button = QPushButton(
            "自動実行を準備…", native_card)
        self.campaign_native_button.clicked.connect(
            self._native_campaign_prepare)
        native_row.addWidget(self.campaign_native_button)
        self.campaign_native_pause_button = QPushButton(
            "一時停止", native_card)
        self.campaign_native_pause_button.clicked.connect(
            self._native_campaign_pause)
        native_row.addWidget(self.campaign_native_pause_button)
        self.campaign_native_resume_button = QPushButton(
            "再開", native_card)
        self.campaign_native_resume_button.clicked.connect(
            self._native_campaign_resume)
        native_row.addWidget(self.campaign_native_resume_button)
        self.campaign_native_cancel_button = QPushButton(
            "中止", native_card)
        self.campaign_native_cancel_button.clicked.connect(
            self._native_campaign_cancel)
        native_row.addWidget(self.campaign_native_cancel_button)
        native_row.addStretch(1)
        # Enabled only while a drive exists — inert buttons are dishonest.
        self.campaign_native_pause_button.setEnabled(False)
        self.campaign_native_resume_button.setEnabled(False)
        self.campaign_native_cancel_button.setEnabled(False)
        native_layout.addLayout(native_row)
        self.campaign_native_status_label = QLabel("", native_card)
        self.campaign_native_status_label.setWordWrap(True)
        native_layout.addWidget(self.campaign_native_status_label)
        self.campaign_native_simulated_label = QLabel("", native_card)
        self.campaign_native_simulated_label.setWordWrap(True)
        set_semantic_state(
            self.campaign_native_simulated_label, SemanticState.WARNING)
        native_layout.addWidget(self.campaign_native_simulated_label)
        position_row = QHBoxLayout()
        self.campaign_native_position_label = QLabel("", native_card)
        self.campaign_native_position_label.setWordWrap(True)
        position_row.addWidget(self.campaign_native_position_label, 1)
        self.campaign_native_confirm_button = QPushButton(
            "位置を確認して続行", native_card)
        self.campaign_native_confirm_button.clicked.connect(
            self._native_confirm_position)
        self.campaign_native_confirm_button.setEnabled(False)
        position_row.addWidget(self.campaign_native_confirm_button)
        native_layout.addLayout(position_row)
        layout.addWidget(native_card)

        variant_card, variant_layout = _card("登録済みの詳細計画", host)
        variant_hint = QLabel(
            "システムバリアント/検証ワークフローが登録した測定計画を、"
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
        self.campaign_table = QTableWidget(0, 6, matrix_card)
        self.campaign_table.setHorizontalHeaderLabels(
            ["入力役割", "測定位置", "リピート", "状態", "測定",
             "ネイティブ実行"]
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
        self.campaign_step_label = QLabel(
            "「実行を開始 / 再開」で計画を開くと、次に測るセルがここに表示されます",
            step_card,
        )
        self.campaign_step_label.setWordWrap(True)
        step_layout.addWidget(self.campaign_step_label)
        self.campaign_progress_label = QLabel("", step_card)
        self.campaign_progress_label.setWordWrap(True)
        step_layout.addWidget(self.campaign_progress_label)

        commit_row = QHBoxLayout()
        self.campaign_measurement_combo = QComboBox(step_card)
        self.campaign_measurement_combo.setAccessibleName("登録する測定")
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
        self._native_drive = None
        self._native_plan = None
        self._native_runner_plan = None
        self._native_assignments = None
        self._native_labels: dict[int, str] = {}

    # -- native automated campaign execution (#956) --------------------

    _NATIVE_OUTCOME_LABELS = {
        'in_progress': '実行中',
        'awaiting_position': '位置確認待ち',
        'paused': '一時停止中',
        'blocked': 'ブロック',
        'completed': '完了',
        'completed_with_failures': '完了（失敗あり）',
        'failed': '失敗',
        'cancelled': '中止',
    }

    def _native_stimulus(self):
        from ...cad_sweep_acquisition import SweepStimulusSpec

        return SweepStimulusSpec(
            start_frequency_hz=self.acq_start_freq.value(),
            end_frequency_hz=self.acq_end_freq.value(),
            duration_s=self.acq_duration.value(),
            level_dbfs=self.acq_level.value(),
            sample_rate_hz=int(self.acq_sample_rate.currentData()),
            repetitions=self.acq_repetitions.value(),
        )

    def _native_default_routings(self, runner_plan) -> dict:
        """Per-binding default routing from the acquisition surface's
        device selections — always displayed in preflight for explicit
        arm approval, never silently applied."""
        from ...cad_campaign_native import channel_entity_id_for
        from ...cad_sweep_acquisition import ChannelRouting

        try:
            devices = tuple(self._acq_backend.enumerate_devices() or ())
        except EXPECTED_OPERATION_ERRORS:
            devices = ()
        playback_dev = next(
            (d for d in devices
             if d.direction in ('playback', 'duplex')), None)
        capture_dev = next(
            (d for d in devices
             if d.direction in ('capture', 'duplex')), None)
        playback_id = str(
            self.acq_playback_device.currentData()
            or (playback_dev.device_id if playback_dev else ''))
        capture_id = str(
            self.acq_capture_device.currentData()
            or (capture_dev.device_id if capture_dev else ''))
        loopback = (
            1 if capture_dev is not None
            and capture_dev.max_input_channels > 1
            else None
        )
        routings: dict = {}
        for cell in runner_plan.cells:
            channel_id = channel_entity_id_for(
                cell.channel_role, cell.source_speaker_ids)
            if channel_id in routings:
                continue
            # One logical channel per physical output — declared,
            # displayed, and approved in preflight.
            routings[channel_id] = ChannelRouting(
                playback_device_id=playback_id,
                playback_channel=len(routings),
                capture_device_id=capture_id,
                capture_channel=0,
                loopback_input_channel=loopback,
            )
        return routings

    def _native_arm_provider(self, approved_routings: dict,
                           level_dbfs: float):
        """Arm echoes exactly the routing + level the operator approved —
        an engine presenting different parameters is refused."""
        from ...cad_sweep_acquisition import ArmConfirmation

        def provider(entry, request):
            expected = approved_routings.get(entry.channel_entity_id)
            r = request.routing
            if expected is None or (
                r.playback_device_id != expected.playback_device_id
                or r.playback_channel != expected.playback_channel
                or r.capture_device_id != expected.capture_device_id
                or r.capture_channel != expected.capture_channel
                or r.loopback_input_channel
                != expected.loopback_input_channel
                or request.stimulus.level_dbfs != level_dbfs
            ):
                return None
            return ArmConfirmation(
                acknowledged_playback_device_id=r.playback_device_id,
                acknowledged_playback_channel=r.playback_channel,
                acknowledged_capture_device_id=r.capture_device_id,
                acknowledged_capture_channel=r.capture_channel,
                acknowledged_level_dbfs=request.stimulus.level_dbfs,
            )

        return provider

    def _native_campaign_prepare(self) -> None:
        from ...cad_sweep_acquisition import MeasurementAcquisitionEngine

        plan_id = self.campaign_plan_combo.currentData()
        if not plan_id:
            self._set_notice("先に実行する計画を選択してください",
                             SemanticState.WARNING)
            return
        runner_plan = self.controller.runner_repository.get_plan(plan_id)
        if runner_plan is None:
            self._set_notice("計画が見つかりません", SemanticState.WARNING)
            return
        routings = self._native_default_routings(runner_plan)
        stimulus = self._native_stimulus()
        try:
            materialization = self.controller.materialize_native_campaign(
                plan_id, routings=routings,
                stimulus_template=stimulus)
        except MeasurementWorkflowError as exc:
            self._operation_error_notice(
                "自動実行の正準計画に変換できませんでした", exc)
            return
        plan = materialization.plan
        report = self.controller.native_preflight(
            plan, backend=self._acq_backend)
        dialog = _NativeCampaignPreflightDialog(report, self)
        if (dialog.exec() != QDialog.DialogCode.Accepted
                or not dialog.armed):
            self._set_notice(
                "自動実行は開始されていません — アーム承認がありません",
                SemanticState.WARNING)
            return
        self._native_runner_plan = runner_plan
        self._native_plan = plan
        self._native_assignments = materialization.assignments
        self._native_drive = self.controller.native_campaign_drive(
            plan,
            engine_factory=lambda: MeasurementAcquisitionEngine(
                self._acq_backend),
            arm_confirmation_provider=self._native_arm_provider(
                routings, stimulus.level_dbfs),
        )
        try:
            run = self.controller.open_runner(plan_id)
            self._campaign_run_id = run.run_id
        except EXPECTED_OPERATION_ERRORS as exc:
            self._operation_error_notice(
                "セル表を開けませんでした", exc)
        self._native_drive_run()

    def _native_drive_run(self) -> None:
        drive = self._native_drive
        if drive is None:
            return
        try:
            drive.advance()
        except EXPECTED_OPERATION_ERRORS as exc:
            # error-boundary: expected — journal/state failures surface
            # honestly; the campaign stays recoverable via 再開.
            self._operation_error_notice("自動実行に失敗しました", exc)
        self._native_sync_ui()

    def _native_confirm_position(self) -> None:
        drive = self._native_drive
        if drive is None:
            return
        try:
            awaiting = drive.state().awaiting_position_id
            if awaiting:
                drive.confirm_position(awaiting)
        except EXPECTED_OPERATION_ERRORS as exc:
            self._operation_error_notice("位置を確認できませんでした", exc)
            self._native_sync_ui()
            return
        self._native_drive_run()

    def _native_campaign_pause(self) -> None:
        drive = self._native_drive
        if drive is None:
            return
        try:
            drive.pause('operator pause')
        except EXPECTED_OPERATION_ERRORS as exc:
            self._operation_error_notice("一時停止できませんでした", exc)
        self._native_sync_ui()

    def _native_campaign_resume(self) -> None:
        drive = self._native_drive
        if drive is None:
            return
        try:
            drive.resume()
        except EXPECTED_OPERATION_ERRORS as exc:
            self._operation_error_notice("再開できませんでした", exc)
            self._native_sync_ui()
            return
        self._native_drive_run()

    def _native_campaign_cancel(self) -> None:
        drive = self._native_drive
        if drive is None:
            return
        try:
            drive.cancel('operator cancelled')
        except EXPECTED_OPERATION_ERRORS as exc:
            self._operation_error_notice("中止できませんでした", exc)
        self._native_sync_ui()

    def _native_sync_ui(self) -> None:
        from ...cad_campaign_native import cell_native_labels

        plan = self._native_plan
        drive = self._native_drive
        if plan is None or drive is None:
            self.campaign_native_status_label.setText("")
            self.campaign_native_simulated_label.setText("")
            self.campaign_native_position_label.setText("")
            self.campaign_native_confirm_button.setEnabled(False)
            self.campaign_native_pause_button.setEnabled(False)
            self.campaign_native_resume_button.setEnabled(False)
            self.campaign_native_cancel_button.setEnabled(False)
            return
        state, assignments, records = self.controller.native_campaign_status(
            plan, self._native_runner_plan)
        self._native_assignments = assignments
        progress = state.progress
        outcome = self._NATIVE_OUTCOME_LABELS.get(
            state.outcome, state.outcome)
        lines = [
            f"{outcome} · 完了 {progress.completed}/{progress.total} · "
            f"残り {progress.remaining} · 失敗 {progress.failed} · "
            f"位置確認待ち {progress.awaiting_position} · "
            f"ブロック {progress.blocked}"
        ]
        if state.blocked_reason:
            lines.append(f"ブロック理由: {state.blocked_reason}")
        missing = progress.total - progress.completed - progress.waived
        if missing and state.outcome in (
                'completed_with_failures', 'failed', 'cancelled'):
            lines.append(
                f"証跡のないセルが {missing} 件残っています — "
                "「実行を開始 / 再開」でジャーナルから続行できます")
        self.campaign_native_status_label.setText(" / ".join(lines))

        simulated = (
            getattr(self._acq_backend, 'backend_id', '') == 'fake-audio-io'
            or any(r.backend_is_simulated for r in records))
        self.campaign_native_simulated_label.setText(
            "SIMULATED バックエンド — 記録はテスト証跡であり、"
            "実測定の合格証拠ではありません"
            if simulated else "")

        terminal = state.outcome in (
            'completed', 'completed_with_failures', 'failed', 'cancelled')
        # A terminal campaign never asks for a mic move — the prompt is
        # meaningful only while the runner can still advance.
        awaiting = state.awaiting_position_id if not terminal else None
        if awaiting:
            names = self._campaign_target_names()
            label = names.get(awaiting, awaiting)
            spec = plan.position(awaiting)
            coords = (
                f" ({spec.declared_position.x_m:.2f}, "
                f"{spec.declared_position.y_m:.2f}, "
                f"{spec.declared_position.z_m:.2f}) m"
                if spec is not None and spec.declared_position is not None
                else "")
            self.campaign_native_position_label.setText(
                f"マイクを「{label}」{coords} に移動してください。\n"
                "位置を確認するまで実行は進みません。")
            self.campaign_native_confirm_button.setEnabled(True)
        else:
            self.campaign_native_position_label.setText("")
            self.campaign_native_confirm_button.setEnabled(False)

        self.campaign_native_pause_button.setEnabled(
            not terminal and not state.paused)
        self.campaign_native_resume_button.setEnabled(
            not terminal and (state.paused or bool(state.blocked_reason)))
        self.campaign_native_cancel_button.setEnabled(not terminal)

        self._native_labels = cell_native_labels(
            runner_plan=self._native_runner_plan,
            assignments=assignments,
            state=state,
            run_records=records,
        )
        self._refresh_campaign()

    def _campaign_target_names(self) -> dict[str, str]:
        try:
            return {
                target.entity_id: target.name
                for target in self.controller.assignment_targets()
            }
        except EXPECTED_OPERATION_ERRORS as exc:
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
        except EXPECTED_OPERATION_ERRORS as exc:
            if is_authority_failure(exc):
                raise
            report_boundary_failure(exc, operation='音源名の読み込み')
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
        *,
        plans=None,
        plan_created=None,
    ) -> None:
        if views is None:
            views = self.controller.measurement_views()
        if plans is None:
            plans = self.controller.runner_plans()
        if plan_created is None:
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
            self.campaign_step_label.setText(
                "計画がまだありません — 上の「計画を作成」で条件を選んでください"
            )
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
                (
                    self._native_labels.get(cell.cell_index, '—')
                    if self._native_runner_plan is not None
                    and self._native_runner_plan.plan_id == plan.plan_id
                    else '—'
                ),
            )
            column_native = (
                self._native_runner_plan is not None
                and self._native_runner_plan.plan_id == plan.plan_id
            )
            cell_tooltips = (
                None,
                None,
                None,
                status_explanation(state.status),
                None,
                (
                    'HTDTネイティブ自動実行の最新の実行状態です'
                    if column_native
                    else None
                ),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, cell.cell_index)
                tip = cell_tooltips[column]
                if tip:
                    item.setToolTip(tip)
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
        except EXPECTED_OPERATION_ERRORS as exc:
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
        except EXPECTED_OPERATION_ERRORS as exc:
            if is_authority_failure(exc):
                raise
            report_boundary_failure(exc, operation='キャンペーン音源候補の読み込み')
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
        except EXPECTED_OPERATION_ERRORS as exc:
            if is_authority_failure(exc):
                raise
            report_boundary_failure(exc, operation='割り当て対象の読み込み')
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
        except EXPECTED_OPERATION_ERRORS as exc:
            if is_authority_failure(exc):
                raise
            report_boundary_failure(exc, operation='パターン候補の読み込み')
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
        except EXPECTED_OPERATION_ERRORS as exc:
            if is_authority_failure(exc):
                raise
            report_boundary_failure(exc, operation='バリアント計画の読み込み')
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
        except EXPECTED_OPERATION_ERRORS as exc:
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
        except EXPECTED_OPERATION_ERRORS as exc:
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
        except EXPECTED_OPERATION_ERRORS as exc:
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
        except EXPECTED_OPERATION_ERRORS as exc:
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
        except EXPECTED_OPERATION_ERRORS as exc:
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
        except EXPECTED_OPERATION_ERRORS as exc:
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

        # #969: combinable filters over the saved-measurement listing —
        # quality verdict, attention/status flag, channel role, measurement
        # position and free text all intersect; the count stays honest.
        filter_row = QHBoxLayout()
        self.quality_verdict_combo = QComboBox(table_card)
        self.quality_verdict_combo.addItem("品質: すべて", None)
        self.quality_verdict_combo.setAccessibleName("品質で絞り込み")
        self.quality_verdict_combo.currentIndexChanged.connect(
            lambda _i: self._apply_quality_filters()
        )
        filter_row.addWidget(self.quality_verdict_combo)
        self.quality_state_combo = QComboBox(table_card)
        for label, value in (
            ("状態: すべて", None),
            ("要対応のみ", "attention"),
            ("要再測定", "retake"),
            ("レポート再評価", "stale_report"),
            ("校正が未確認/不合格", "calibration"),
            ("検証エラー", "verification_error"),
            ("通常利用から除外", "excluded"),
        ):
            self.quality_state_combo.addItem(label, value)
        self.quality_state_combo.setAccessibleName("状態で絞り込み")
        self.quality_state_combo.currentIndexChanged.connect(
            lambda _i: self._apply_quality_filters()
        )
        filter_row.addWidget(self.quality_state_combo)
        self.quality_channel_filter = QComboBox(table_card)
        self.quality_channel_filter.addItem("入力: すべて", None)
        self.quality_channel_filter.setAccessibleName("入力役割で絞り込み")
        self.quality_channel_filter.currentIndexChanged.connect(
            lambda _i: self._apply_quality_filters()
        )
        filter_row.addWidget(self.quality_channel_filter)
        self.quality_position_filter = QComboBox(table_card)
        self.quality_position_filter.addItem("測定位置: すべて", None)
        self.quality_position_filter.setAccessibleName("測定位置で絞り込み")
        self.quality_position_filter.currentIndexChanged.connect(
            lambda _i: self._apply_quality_filters()
        )
        filter_row.addWidget(self.quality_position_filter)
        filter_row.addStretch(1)
        table_layout.addLayout(filter_row)

        search_row = QHBoxLayout()
        self.quality_search_edit = QLineEdit(table_card)
        self.quality_search_edit.setPlaceholderText(
            "測定ID・測定位置・入力役割で検索"
        )
        self.quality_search_edit.setAccessibleName(
            "測定ID・測定位置・入力役割で検索"
        )
        self.quality_search_edit.setClearButtonEnabled(True)
        self.quality_search_edit.textChanged.connect(
            lambda _t: self._apply_quality_filters()
        )
        search_row.addWidget(self.quality_search_edit, 1)
        self.quality_filter_clear = QPushButton("絞り込みをクリア", table_card)
        self.quality_filter_clear.setAccessibleName("絞り込みをクリア")
        self.quality_filter_clear.clicked.connect(self._clear_quality_filters)
        search_row.addWidget(self.quality_filter_clear)
        self.quality_count_label = QLabel("", table_card)
        self.quality_count_label.setAccessibleName("表示件数")
        set_typography_role(self.quality_count_label, TypographyRole.SECONDARY)
        search_row.addWidget(self.quality_count_label)
        table_layout.addLayout(search_row)

        self.quality_selection_note = QLabel("", table_card)
        self.quality_selection_note.setWordWrap(True)
        set_typography_role(
            self.quality_selection_note, TypographyRole.SECONDARY
        )
        table_layout.addWidget(self.quality_selection_note)

        self.quality_table = QTableWidget(0, 10, table_card)
        self.quality_table.setAccessibleName("保存済み測定一覧")
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
        # #969: column sorting is allowed — every row→record lookup resolves
        # through the measurement_id on UserRole, never the visual index.
        self.quality_table.setSortingEnabled(True)
        self.quality_table.setMinimumHeight(220)
        table_layout.addWidget(self.quality_table)

        detail_card, detail_layout = _card("選択した測定", host)
        self.quality_detail = QLabel("測定を選択してください", detail_card)
        self.quality_detail.setAccessibleName("選択した測定の詳細")
        self.quality_detail.setWordWrap(True)
        # #969: keyboard users must reach and copy the detail text too.
        self.quality_detail.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.quality_detail.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
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
        # #969: below _QUALITY_DETAIL_STACK_MIN_WIDTH the same splitter
        # flips to a vertical stack — detail under the table, no horizontal
        # scroll needed to read it. The flip keys on the scroll area's
        # VIEWPORT width: the host itself cannot shrink below its own
        # minimum size, so its width never signals "narrow".
        self.quality_split = QSplitter(Qt.Orientation.Horizontal, host)
        self.quality_split.addWidget(table_card)
        self.quality_split.addWidget(detail_card)
        self.quality_split.setStretchFactor(0, 1)
        self.quality_split.setStretchFactor(1, 1)
        layout.addWidget(self.quality_split)
        self._quality_page_viewport = page.viewport()
        self._quality_page_viewport.installEventFilter(self)
        self._relayout_quality_split()

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
            ("誤割り当てとして記録", "misassigned"),
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
        self.quality_attach_kind_combo.setAccessibleName("添付種別")
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

        # REV44: the dataset level-reference authority for the selected
        # measurement — a derived binding the producer reads to decide what
        # the persisted level_db values mean (absolute SPL vs uncalibrated).
        level_ref_row = QHBoxLayout()
        self.level_reference_label = QLabel("", lifecycle_card)
        self.level_reference_label.setWordWrap(True)
        set_typography_role(
            self.level_reference_label, TypographyRole.SECONDARY
        )
        level_ref_row.addWidget(self.level_reference_label, 1)
        self.level_reference_button = QPushButton(
            "レベル基準を登録…", lifecycle_card
        )
        self.level_reference_button.setToolTip(
            "この測定のデータセットにレベルの意味（絶対SPL 等）を記録します。"
        )
        self.level_reference_button.setWhatsThis(
            "この測定のデータセットにレベルの意味（絶対SPL 等）を記録します。"
        )
        self.level_reference_button.clicked.connect(
            self._register_dataset_level_reference
        )
        level_ref_row.addWidget(self.level_reference_button)
        lifecycle_layout.addLayout(level_ref_row)
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

        # REV44-HEALTHSYNC: the record-entry surfaces for the two previously
        # write-only authorities — AV-sync conditions (this context is where
        # AV_SYNC_CONDITION deep links already route) and health baselines /
        # check plans / runs (the 概要 稼働状況 domain deeplinks here).
        record_row = QHBoxLayout()

        av_sync_card, av_sync_layout = _card("AV同期", host)
        self.av_sync_summary = QLabel("条件なし", av_sync_card)
        self.av_sync_summary.setWordWrap(True)
        av_sync_layout.addWidget(self.av_sync_summary)
        self.av_sync_button = QPushButton("AV同期を記録…", av_sync_card)
        self.av_sync_button.clicked.connect(self._open_av_sync_dialog)
        av_sync_layout.addWidget(self.av_sync_button)
        record_row.addWidget(av_sync_card, 1)

        health_card, health_layout = _card("健全性チェック", host)
        self.health_summary = QLabel("ベースラインなし", health_card)
        self.health_summary.setWordWrap(True)
        health_layout.addWidget(self.health_summary)
        self.health_button = QPushButton("健全性チェックを記録…", health_card)
        self.health_button.clicked.connect(self._open_health_dialog)
        health_layout.addWidget(self.health_button)
        record_row.addWidget(health_card, 1)

        preset_card, preset_layout = _card("運用プリセット", host)
        self.preset_summary = QLabel("プリセットなし", preset_card)
        self.preset_summary.setWordWrap(True)
        preset_layout.addWidget(self.preset_summary)
        self.preset_button = QPushButton("プリセットを記録…", preset_card)
        self.preset_button.clicked.connect(self._open_preset_dialog)
        preset_layout.addWidget(self.preset_button)
        record_row.addWidget(preset_card, 1)

        layout.addLayout(record_row)
        layout.addStretch(1)
        self.pages.addWidget(page)

    def _refresh_quality(
        self,
        views: tuple[MeasurementView, ...] | None = None,
    ) -> None:
        if views is None:
            views = self.controller.measurement_views()
        self._quality_views = views
        self._quality_views_by_id = {
            row.measurement_id: row for row in views
        }
        self._rebuild_quality_filter_choices()
        selected_id = None
        selected_items = self.quality_table.selectedItems()
        if selected_items:
            selected_id = selected_items[0].data(Qt.ItemDataRole.UserRole)

        # #969: the combinable filters decide which rows the table shows;
        # the listing itself keeps every measurement. Sorting is disabled
        # while rows are inserted, then re-enabled — all row→record lookups
        # resolve the measurement_id on UserRole, never the visual index.
        filtered = self._quality_filtered_views()
        self._filtered_quality_views = filtered
        self.quality_table.setSortingEnabled(False)
        self.quality_table.setRowCount(len(filtered))
        for row_index, row in enumerate(filtered):
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
            row_tooltips = (
                None,
                status_explanation(row.evidence_type, domain='evidence'),
                None,
                (
                    'データセットの検証に失敗しました'
                    if row.dataset_error
                    else status_explanation(row.quality_status, domain='quality')
                ),
                status_explanation(row.phase_status, domain='phase'),
                status_explanation(
                    _capability_decision(row.common_timing_capability),
                    domain='capability',
                ),
                (
                    '現在のシーンと同じ配置です'
                    if row.scene_matches_current
                    else '測定時の配置と現在の配置が異なります'
                ),
                None,
                status_explanation(row.disposition),
                status_explanation(row.retake_recommendation),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, row.measurement_id)
                tip = row_tooltips[column]
                if tip:
                    item.setToolTip(tip)
                self.quality_table.setItem(row_index, column, item)
        self.quality_table.setSortingEnabled(True)
        # Re-apply the header's sort indicator over the freshly inserted
        # rows: rows must always match the indicator, otherwise the first
        # user click only moves the indicator while the order lags a click
        # behind. Row identity is unaffected — it lives on UserRole.
        header = self.quality_table.horizontalHeader()
        self.quality_table.sortItems(
            header.sortIndicatorSection(), header.sortIndicatorOrder()
        )

        # #969: honest count — hidden rows are stated, never silently dropped.
        self.quality_count_label.setText(
            f"{len(views)}件中{len(filtered)}件を表示"
        )
        self._update_quality_selection_note(selected_id)

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
            return
        # Selection persists keyed by measurement_id (#969). A vanished or
        # filter-hidden selection stays cleared — the note above explains
        # why; an untouched first fill keeps the historic row-0 default.
        target_row = (
            self._quality_row_index_for_id(selected_id)
            if selected_id is not None
            else None
        )
        if target_row is not None:
            self.quality_table.selectRow(target_row)
        elif selected_id is None and filtered and not self.quality_table.selectedItems():
            self.quality_table.selectRow(0)
        if self.quality_table.selectedItems():
            self._show_quality_row(self.quality_table.currentRow())
        elif filtered:
            self.quality_detail.setText("測定を選択してください")
        else:
            self.quality_detail.setText(
                "絞り込み条件に一致する測定はありません"
            )

    def _quality_row_index_for_id(self, measurement_id: str) -> int | None:
        """#969: resolve a measurement_id to its current VISUAL table row.

        Filtering and sorting decouple the visual index from
        ``_quality_views`` order — only the UserRole binding is stable."""
        for row_index in range(self.quality_table.rowCount()):
            item = self.quality_table.item(row_index, 0)
            if (
                item is not None
                and item.data(Qt.ItemDataRole.UserRole) == measurement_id
            ):
                return row_index
        return None

    def _quality_view_for_row(self, row_index: int) -> MeasurementView | None:
        """#969: map a visual table row back to its record by id."""
        item = self.quality_table.item(row_index, 0)
        if item is None:
            return None
        measurement_id = item.data(Qt.ItemDataRole.UserRole)
        if measurement_id is None:
            return None
        return self._quality_views_by_id.get(measurement_id)

    # -- #969 combinable filters ------------------------------------------

    @staticmethod
    def _refill_filter_combo(combo: QComboBox, entries) -> None:
        """Rebuild a filter combo's choices, preserving the current value
        when it still exists (filters survive refresh keyed on data)."""
        current = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        for label, data in entries:
            combo.addItem(label, data)
        index = combo.findData(current)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(False)

    def _rebuild_quality_filter_choices(self) -> None:
        """#969: verdict/channel/position choices reflect the current
        listing — a value that disappeared unselects itself honestly."""
        statuses: list[str] = []
        roles: list[str] = []
        positions: list[str] = []
        has_dataset_error = False
        for row in self._quality_views:
            if row.quality_status not in statuses:
                statuses.append(row.quality_status)
            if row.effective_channel_role not in roles:
                roles.append(row.effective_channel_role)
            if row.effective_target_name not in positions:
                positions.append(row.effective_target_name)
            has_dataset_error = has_dataset_error or bool(row.dataset_error)
        verdict_entries: list[tuple[str, object]] = [("品質: すべて", None)]
        if has_dataset_error:
            verdict_entries.append(
                ("品質: 検証エラー", _QUALITY_VERDICT_DATASET_ERROR)
            )
        verdict_entries.extend(
            (f"品質: {_quality_label(status)}", status)
            for status in sorted(statuses)
        )
        self._refill_filter_combo(self.quality_verdict_combo, verdict_entries)
        self._refill_filter_combo(
            self.quality_channel_filter,
            [("入力: すべて", None)]
            + [
                (f"入力: {_channel_role_label(role)}", role)
                for role in sorted(roles)
            ],
        )
        self._refill_filter_combo(
            self.quality_position_filter,
            [("測定位置: すべて", None)]
            + [(f"測定位置: {name}", name) for name in sorted(positions)],
        )

    def _quality_attention_reasons(self, row: MeasurementView) -> list[str]:
        """#969 「要対応」 facets — each reason is stated, never implied."""
        reasons: list[str] = []
        if (
            row.dataset_error
            or row.report_error
            or row.quality_report_state == "error"
        ):
            reasons.append("検証エラー")
        if row.quality_status == "invalid":
            reasons.append("品質 invalid")
        if row.retake_recommendation == "RETAKE":
            reasons.append("要再測定")
        if row.quality_report_state == "stale":
            reasons.append("レポート再評価")
        if any(item.status == "FAIL" for item in row.quality_checks):
            reasons.append("チェック不合格")
        if not row.is_normally_eligible:
            reasons.append("通常利用から除外")
        return reasons

    def _quality_state_matches(self, row: MeasurementView, state: str) -> bool:
        if state == "attention":
            return bool(self._quality_attention_reasons(row))
        if state == "retake":
            return row.retake_recommendation == "RETAKE"
        if state == "stale_report":
            return row.quality_report_state in ("stale", "error")
        if state == "calibration":
            # Honest reading: only an evaluated calibration check that did
            # not pass counts — missing evidence never claims uncalibrated.
            return any(
                item.check == "calibration" and item.status != "PASS"
                for item in row.quality_checks
            )
        if state == "verification_error":
            return bool(
                row.dataset_error
                or row.report_error
                or row.quality_report_state == "error"
            )
        if state == "excluded":
            return not row.is_normally_eligible
        return True

    def _quality_filtered_views(self) -> tuple[MeasurementView, ...]:
        verdict = self.quality_verdict_combo.currentData()
        state = self.quality_state_combo.currentData()
        channel = self.quality_channel_filter.currentData()
        position = self.quality_position_filter.currentData()
        needle = self.quality_search_edit.text().strip().lower()
        filtered: list[MeasurementView] = []
        for row in self._quality_views:
            if verdict is not None:
                if verdict == _QUALITY_VERDICT_DATASET_ERROR:
                    if not row.dataset_error:
                        continue
                elif row.dataset_error or row.quality_status != verdict:
                    continue
            if state is not None and not self._quality_state_matches(
                row, state
            ):
                continue
            if (
                channel is not None
                and row.effective_channel_role != channel
            ):
                continue
            if (
                position is not None
                and row.effective_target_name != position
            ):
                continue
            if needle:
                haystack = " ".join(
                    (
                        row.measurement_id,
                        row.effective_target_name,
                        row.target_name,
                        row.effective_target_entity_id,
                        row.effective_channel_role,
                        _channel_role_label(row.effective_channel_role),
                        row.dataset_id or "",
                    )
                ).lower()
                if needle not in haystack:
                    continue
            filtered.append(row)
        return tuple(filtered)

    def _update_quality_selection_note(self, selected_id) -> None:
        """#969: when the prior selection can no longer be shown, say why
        instead of silently pointing at a different measurement."""
        if selected_id is None:
            self.quality_selection_note.setText("")
            return
        row = self._quality_views_by_id.get(selected_id)
        if row is None:
            self.quality_selection_note.setText(
                "選択していた測定は現在の一覧にありません"
                "（別のリビジョンまたは削除済み）。"
            )
            return
        if row not in self._filtered_quality_views:
            self.quality_selection_note.setText(
                "選択していた測定は絞り込み条件で非表示です。"
            )
            return
        self.quality_selection_note.setText("")

    def _apply_quality_filters(self) -> None:
        """Re-run the listing through the current filter controls — reuses
        the fetched views so a keystroke never re-verifies the store."""
        self._refresh_quality(self._quality_views)

    def _clear_quality_filters(self) -> None:
        for combo in (
            self.quality_verdict_combo,
            self.quality_state_combo,
            self.quality_channel_filter,
            self.quality_position_filter,
        ):
            combo.blockSignals(True)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)
        self.quality_search_edit.blockSignals(True)
        self.quality_search_edit.clear()
        self.quality_search_edit.blockSignals(False)
        self.quality_selection_note.setText("")
        self._apply_quality_filters()

    def _relayout_quality_split(self) -> None:
        """#969 adaptive layout: wide pages keep table‖detail side-by-side;
        narrow/high-DPI pages stack detail under the table."""
        viewport = self._quality_page_viewport
        if viewport is None:
            return
        target = (
            Qt.Orientation.Horizontal
            if viewport.width() >= _QUALITY_DETAIL_STACK_MIN_WIDTH
            else Qt.Orientation.Vertical
        )
        if self.quality_split.orientation() != target:
            self.quality_split.setOrientation(target)

    def eventFilter(self, watched, event) -> bool:
        if (
            watched is self._quality_page_viewport
            and event.type() == QEvent.Type.Resize
        ):
            self._relayout_quality_split()
        return super().eventFilter(watched, event)

    def _quality_selection_changed(self) -> None:
        row_index = self.quality_table.currentRow()
        if row_index >= 0:
            self._show_quality_row(row_index)

    def _measurement_evidence_lines(self, measurement_id: str) -> list[str]:
        """REV56-MEASEV (#572/#573): uncertainty-budget and state-stability
        evidence bound to the selected measurement. The repository is
        opened lazily and failures degrade to an honest unavailable line
        rather than breaking the quality detail panel."""
        try:
            if self._measurement_evidence_repository is None:
                from ..persistence.cad_measurement_evidence_repository import (
                    CadMeasurementEvidenceRepository,
                )

                self._measurement_evidence_repository = (
                    CadMeasurementEvidenceRepository(
                        self.controller.scene_repository
                    )
                )
            from ..domain.measurement_evidence_display import (
                quality_evidence_detail_lines,
            )

            lines = quality_evidence_detail_lines(
                measurement_id,
                repository=self._measurement_evidence_repository,
            )
            lines.extend(
                self._stimulus_pin_lines(measurement_id)
            )
            lines.extend(
                self._spatial_campaign_lines(measurement_id)
            )
            return lines
        except EXPECTED_OPERATION_ERRORS as exc:
            if is_authority_failure(exc):
                raise
            report_boundary_failure(exc, operation='証拠レコードの読み込み')
            return ["不確かさ/状態安定性: 証拠レコードを読み込めませんでした"]

    def _stimulus_pin_lines(self, measurement_id: str) -> list[str]:
        """REV56-BASSSTIM (#608): stimulus pins bound to the selected
        measurement. An unpinned measurement is honest — pinned is shown
        with the registry identity it resolves to; a lookup failure
        degrades to an unavailable line, never a silent claim."""
        try:
            if self._stimulus_registry_repository is None:
                from ...cad_stimulus_registry_repository import (
                    CadStimulusRegistryRepository,
                )

                self._stimulus_registry_repository = (
                    CadStimulusRegistryRepository(
                        self.controller.scene_repository
                    )
                )
            pins = self._stimulus_registry_repository.pins_for_measurement(
                measurement_id
            )
            if not pins:
                return [
                    '刺激ピン: 未登録'
                    '（この測定に使った刺激は登録簿に結ばれていません）'
                ]
            from ..domain.measurement_evidence_display import stimulus_pin_line

            return [stimulus_pin_line(pin) for pin in pins]
        except EXPECTED_OPERATION_ERRORS as exc:
            if is_authority_failure(exc):
                raise
            report_boundary_failure(exc, operation='刺激ピン登録簿の読み込み')
            return ['刺激ピン: 登録簿を読み込めませんでした']

    def _spatial_campaign_lines(self, measurement_id: str) -> list[str]:
        """REV56-CAMPPROFILE (#581): campaign-point bindings for the
        selected measurement. An unbound measurement is honest — bound is
        shown with the declared point role and observed-position
        deviation; a lookup failure degrades to an unavailable line,
        never a silent claim."""
        try:
            if self._spatial_campaign_repository is None:
                from ...cad_spatial_campaign_repository import (
                    CadSpatialCampaignRepository,
                )

                self._spatial_campaign_repository = (
                    CadSpatialCampaignRepository(
                        self.controller.scene_repository
                    )
                )
            bindings = (
                self._spatial_campaign_repository.bindings_for_measurement(
                    measurement_id
                )
            )
            if not bindings:
                return [
                    '空間キャンペーン: 未束縛'
                    '（この測定は宣言済み測定点計画に結ばれていません）'
                ]
            from ..domain.measurement_evidence_display import (
                point_role_label,
                spatial_binding_line,
            )

            lines: list[str] = []
            for binding in bindings:
                design = self._spatial_campaign_repository.get_design(
                    binding.design_id
                )
                point = (
                    design.point(binding.point_id) if design else None
                )
                line = spatial_binding_line(binding)
                if point is not None:
                    roles = '、'.join(
                        point_role_label(role) for role in point.roles
                    )
                    line = f'{line} — 役割: {roles}'
                lines.append(line)
            return lines
        except EXPECTED_OPERATION_ERRORS as exc:
            if is_authority_failure(exc):
                raise
            report_boundary_failure(exc, operation='空間キャンペーン束縛の読み込み')
            return [
                '空間キャンペーン: 束縛レコードを読み込めませんでした'
            ]

    def _show_quality_row(self, row_index: int) -> None:
        # #969: the visual row is never an index into _quality_views —
        # resolve the record through the item's measurement_id binding.
        row = self._quality_view_for_row(row_index)
        if row is None:
            return
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
            f"測定ID: {row.measurement_id}\n"
            f"{row.effective_target_name} · {_evidence_label(row.evidence_type)} · "
            f"{_channel_role_label(row.effective_channel_role)}\n"
            f"品質: {_quality_label(row.quality_status)} · {reasons}\n"
            f"{_phase_label(row.phase_status)} · "
            f"位相応答: {_capability_decision_label(row.phase_response_capability)} · "
            f"共通タイミング: {_capability_decision_label(row.common_timing_capability)} · "
            f"{scene}\n"
            f"音源: {source_speakers} · {captured}"
        )
        detail_lines.extend(
            self._measurement_evidence_lines(row.measurement_id)
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
                f" — {measurement_reason_label(item.reason)}"
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
                f" — {' / '.join(measurement_reason_label(r) for r in item.reasons)}"
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
                retake_lines.append(
                    "理由: "
                    + " / ".join(
                        _retake_reason_label(reason)
                        for reason in row.retake_reasons
                    )
                )
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
        self._update_level_reference_row(row)

        self._refresh_target_curve_choices()
        self._refresh_spatial(row)
        self._refresh_quality_plot()

    def _update_level_reference_row(self, row: MeasurementView) -> None:
        """Bound level-reference state + register affordance (REV44).

        The button is only live when the selected measurement has a
        verifiable dataset without a reference — one dataset can carry
        exactly one pinned level reference, so a bound dataset disables it.
        """
        if row.dataset_id is None or row.dataset_error:
            self.level_reference_label.setText(
                "レベル基準: 検証可能なデータセットがありません"
            )
            self.level_reference_button.setEnabled(False)
            return
        try:
            reference = (
                self.controller.quality_repository.get_dataset_level_reference(
                    row.dataset_id
                )
            )
        except EXPECTED_OPERATION_ERRORS as exc:
            if is_authority_failure(exc):
                raise
            report_boundary_failure(exc, operation='レベル基準の検証')
            self.level_reference_label.setText(
                "レベル基準: 検証できませんでした（保存データを確認してください）"
            )
            self.level_reference_button.setEnabled(False)
            return
        if reference is None:
            self.level_reference_label.setText(
                "レベル基準: 未登録 — レベルの意味が未宣言のままです"
            )
            self.level_reference_button.setEnabled(True)
            return
        self.level_reference_label.setText(
            f"レベル基準: "
            f"{level_reference_kind_label(reference.level_reference_kind)} "
            f"· 登録 {reference.created_at_utc}"
        )
        self.level_reference_button.setEnabled(False)

    def _selected_quality_view(self) -> MeasurementView | None:
        return self._quality_view_for_row(self.quality_table.currentRow())

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
        except EXPECTED_OPERATION_ERRORS as exc:
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
        except EXPECTED_OPERATION_ERRORS as exc:
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
                from ...room_viewport import RoomOverlayState, RoomViewport3D

                self._spatial_viewport = RoomViewport3D(self)
                self._spatial_viewport.setMinimumHeight(320)
                self._spatial_overlays = RoomOverlayState(grid=True, labels=True)
                self.spatial_viewport_holder.addWidget(self._spatial_viewport)
            except EXPECTED_OPERATION_ERRORS as exc:
                if is_authority_failure(exc):
                    raise
                report_boundary_failure(exc, operation='空間ビューの初期化')
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
                        label="測定時の配置ゴースト · 現在シーンは変更しません",
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
        except EXPECTED_OPERATION_ERRORS as exc:
            if is_authority_failure(exc):
                raise
            report_boundary_failure(exc, operation='空間ビューの描画')
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
        from ...workflow_help import ReasonTextDialog

        reason_dialog = ReasonTextDialog(
            self,
            "測定の状態を記録",
            "この状態を記録する理由（監査ログに残ります）:",
            on_help=(
                (lambda: self._show_help_topic('concept.lifecycle_states'))
                if self._help_available()
                else None
            ),
        )
        ok = bool(reason_dialog.exec())
        reason = reason_dialog.text()
        if not ok or not reason.strip():
            self._set_notice("状態を記録する理由が必要です", SemanticState.WARNING)
            return
        try:
            self.controller.set_disposition(
                row.measurement_id, disposition, reason.strip()
            )
        except EXPECTED_OPERATION_ERRORS as exc:
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
                MAX_ATTACHMENT_BYTES,
                label="ソース添付",
            )
            self.controller.save_source_attachment(
                row.measurement_id,
                kind=kind,
                filename=file_path.name,
                raw_bytes=raw,
            )
        except EXPECTED_OPERATION_ERRORS as exc:
            self._operation_error_notice("添付に失敗しました", exc)
            return
        self._set_notice("ソース添付を保存しました", SemanticState.SUCCESS)
        self.refresh()
        self.quality_plot.enableAutoRange()
        self._update_context_label()

    def _start_retake(self) -> None:
        row = self._selected_quality_view()
        if row is None:
            return
        self._retake_source_id = row.measurement_id
        self._correction_target_id = None
        self._set_notice(
            f"{row.target_name} の再測定です。REWから再取得し、"
            "「割り当て」で同じ測定点・入力役割・音源を選んでください。",
            None,
        )
        self.set_context("import")

    # ------------------------------------------------------------------
    # REV44-HEALTHSYNC: AV-sync + system-health record entry
    #
    # Both authority families persist through their own repositories; the
    # dialogs own the writes and this page only reflects persisted counts.

    def _open_av_sync_dialog(self) -> None:
        from .measurement_record_surfaces import AVSyncRecordDialog

        dialog = AVSyncRecordDialog(
            scene_repository=self.controller.scene_repository,
            document_id=self.controller.document_id,
            parent=self,
        )
        dialog.exec()
        self._refresh_record_surfaces()

    def _open_health_dialog(self) -> None:
        from .measurement_record_surfaces import HealthCheckDialog

        dialog = HealthCheckDialog(
            scene_repository=self.controller.scene_repository,
            document_id=self.controller.document_id,
            parent=self,
        )
        dialog.exec()
        self._refresh_record_surfaces()

    def _open_preset_dialog(self) -> None:
        from .measurement_record_surfaces import OperatingPresetRecordDialog

        dialog = OperatingPresetRecordDialog(
            scene_repository=self.controller.scene_repository,
            document_id=self.controller.document_id,
            parent=self,
        )
        dialog.exec()
        self._refresh_record_surfaces()

    def _refresh_record_surfaces(self) -> None:
        """Summarize persisted record-entry state on the quality cards."""
        from ...cad_av_sync_repository import CadAVSyncRepository
        from ...cad_operating_preset_repository import (
            CadOperatingPresetRepository,
        )
        from ...cad_system_health_repository import CadSystemHealthRepository

        try:
            av_sync = CadAVSyncRepository(self.controller.scene_repository)
            conditions = av_sync.list_conditions(self.controller.document_id)
            stages = sum(
                len(av_sync.list_measurements(condition.condition_id))
                for condition in conditions
            )
            if conditions:
                self.av_sync_summary.setText(
                    f"条件 {len(conditions)}件 · 測定 {stages}件"
                )
            else:
                self.av_sync_summary.setText("条件なし")
        except EXPECTED_OPERATION_ERRORS as exc:
            if is_authority_failure(exc):
                raise  # integrity failures never read as a normal summary
            report_boundary_failure(exc, operation='AV同期サマリーの読み込み')
            self.av_sync_summary.setText("確認できません")
        try:
            health = CadSystemHealthRepository(self.controller.scene_repository)
            baselines = health.list_baselines(self.controller.document_id)
            runs = health.list_document_runs(self.controller.document_id)
            if runs:
                latest = runs[-1]
                changed = sum(
                    1
                    for item in latest.assessments
                    if item.state == 'changed'
                )
                state = "変化あり" if changed else "ベースライン内"
                self.health_summary.setText(
                    f"ベースライン {len(baselines)}件 · 最新: {state}"
                )
            elif baselines:
                self.health_summary.setText(
                    f"ベースライン {len(baselines)}件 · チェック未実行"
                )
            else:
                self.health_summary.setText("ベースラインなし")
        except EXPECTED_OPERATION_ERRORS as exc:
            if is_authority_failure(exc):
                raise  # integrity failures never read as a normal summary
            report_boundary_failure(exc, operation='ヘルスサマリーの読み込み')
            self.health_summary.setText("確認できません")
        try:
            presets = CadOperatingPresetRepository(
                self.controller.scene_repository
            )
            preset_rows = presets.list_presets(self.controller.document_id)
            applied = [
                row
                for preset in preset_rows
                for row in presets.list_applied_states(preset.preset_id)
            ]
            if preset_rows:
                self.preset_summary.setText(
                    f"プリセット {len(preset_rows)}件 · 適用記録 {len(applied)}件"
                )
            else:
                self.preset_summary.setText("プリセットなし")
        except EXPECTED_OPERATION_ERRORS as exc:
            if is_authority_failure(exc):
                raise  # integrity failures never read as a normal summary
            report_boundary_failure(exc, operation='プリセットサマリーの読み込み')
            self.preset_summary.setText("確認できません")

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
        self.compare_high.setAccessibleName("比較帯域上限")
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
        self.ref_low.setAccessibleName("レベル参照帯域下限")
        self.ref_low.setRange(1.0, 100000.0)
        self.ref_low.setValue(20.0)
        self.ref_low.setSuffix(" Hz")
        self.ref_high = QDoubleSpinBox(setup_card)
        self.ref_high.setAccessibleName("レベル参照帯域上限")
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
        self.excluded_high.setAccessibleName("除外帯域上限")
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
        self.smooth_b_combo.setAccessibleName("表示スムージング B")
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

        registration_card, registration_layout = _card(
            "予測↔実測 登録（#564）", host
        )
        registration_card.setObjectName("pmRegistrationCard")
        self.registration_state_label = QLabel(
            "実測×予測の比較には登録レコードが必要です。", registration_card
        )
        self.registration_state_label.setWordWrap(True)
        registration_layout.addWidget(self.registration_state_label)

        registration_actions = QHBoxLayout()
        self.register_pair_button = QPushButton(
            "登録レコードを作成", registration_card
        )
        self.register_pair_button.setToolTip(
            "選択中の実測×予測ペアを座標・時間・レベル基準つきで登録します。"
        )
        self.register_pair_button.clicked.connect(self._create_registration)
        registration_actions.addWidget(self.register_pair_button)
        self.residual_compute_button = QPushButton(
            "残差を計算", registration_card
        )
        self.residual_compute_button.setToolTip(
            "登録済みペアの残差レポートを計算して保存します。"
        )
        self.residual_compute_button.clicked.connect(self._compute_residuals)
        registration_actions.addWidget(self.residual_compute_button)
        registration_actions.addStretch(1)
        registration_layout.addLayout(registration_actions)

        self.residual_summary_label = QLabel("", registration_card)
        self.residual_summary_label.setWordWrap(True)
        registration_layout.addWidget(self.residual_summary_label)

        self.registration_table = QTableWidget(0, 6, registration_card)
        self.registration_table.setAccessibleName('予測↔実測の登録レコード一覧')
        self.registration_table.setHorizontalHeaderLabels(
            ["測定", "比較可否", "鮮度", "位置差", "時間方式", "パーティション"]
        )
        self.registration_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.registration_table.verticalHeader().setVisible(False)
        self.registration_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.registration_table.setMaximumHeight(140)
        registration_layout.addWidget(self.registration_table)
        layout.addWidget(registration_card)

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
            "機器の準備（校正）",
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
            "・REW: 「REW -API」で起動し、入力デバイスはJavaでUMIK-1を選択、"
            "校正ファイルを適用してからキャンペーンを計測",
            guide_card,
        )
        guide_text.setWordWrap(True)
        set_typography_role(guide_text, TypographyRole.SECONDARY)
        guide.addWidget(guide_text)
        wizard_row = QHBoxLayout()
        open_wizard = QPushButton("ガイド付き校正を開く", guide_card)
        open_wizard.setAccessibleName("ガイド付き校正ウィザードを開く")
        open_wizard.setToolTip(
            "ループバック校正・SPLリファレンスチェック・"
            "キャンペーン前後チェックをガイド手順で実行します。")
        open_wizard.setWhatsThis(open_wizard.toolTip())
        open_wizard.clicked.connect(
            lambda: self.set_context("calibration_wizard"))
        wizard_row.addWidget(open_wizard)
        wizard_row.addStretch(1)
        guide.addLayout(wizard_row)
        layout.addWidget(guide_card)

        # REV44: the production write path for the quality-authority
        # families — each registered authority is then consumed by the
        # onboarding checklist, the acquisition binding combos, the routing
        # provenance and the produced quality reports.
        authority_card, authority = _card("計測の権威データ", page)
        authority_note = QLabel(
            "測定の証拠となる権威レコードを登録します。登録済みの権威は取得条件・"
            "品質レポート・準備チェックから参照されます。",
            authority_card,
        )
        authority_note.setWordWrap(True)
        set_typography_role(authority_note, TypographyRole.SECONDARY)
        authority.addWidget(authority_note)

        def _authority_row(
            title: str,
            button_text: str,
            slot,
            tooltip: str,
        ) -> QLabel:
            row = QHBoxLayout()
            title_label = QLabel(title, authority_card)
            row.addWidget(title_label)
            count_label = QLabel("0 件", authority_card)
            set_typography_role(count_label, TypographyRole.SECONDARY)
            row.addStretch(1)
            row.addWidget(count_label)
            button = QPushButton(button_text, authority_card)
            button.setToolTip(tooltip)
            button.setWhatsThis(tooltip)
            button.clicked.connect(slot)
            row.addWidget(button)
            authority.addLayout(row)
            return count_label

        self.authority_level_calibration_count = _authority_row(
            "レベル校正",
            "登録…",
            self._register_level_calibration,
            "絶対SPLを根拠づけるレベル校正権威を登録します。",
        )
        self.authority_timing_reference_count = _authority_row(
            "タイミング基準",
            "登録…",
            self._register_timing_reference,
            "共通タイミングの権威となるタイミング基準を登録します。",
        )
        self.authority_stimulus_profile_count = _authority_row(
            "刺激プロファイル",
            "登録…",
            self._register_stimulus_profile,
            "再生した刺激信号の権威記録を登録します。",
        )
        self.authority_routing_profile_count = _authority_row(
            "ルーティングプロファイル",
            "登録…",
            self._register_routing_profile,
            "検証済みチャンネルマップの権威を登録します。",
        )
        layout.addWidget(authority_card)

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
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.onboarding_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
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

    def _refresh_onboarding(self, *, plans=None) -> None:
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
            plan_count=(
                len(plans)
                if plans is not None
                else len(self.controller.runner_plans())
            ),
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
            status_tip = status_explanation(step.status)
            if status_tip:
                status_item.setToolTip(status_tip)
            detail_item = QTableWidgetItem(step.detail)
            detail_item.setToolTip(step.detail)
            self.onboarding_table.setItem(row, 0, title_item)
            self.onboarding_table.setItem(row, 1, status_item)
            self.onboarding_table.setItem(row, 2, detail_item)
        if self.current_context_id == "calibration":
            self._update_context_label()

    def _refresh_authority_inventory(self) -> None:
        """Persisted-authority counts on the calibration page (REV44).

        A listing failure is shown as a load error count and one warning —
        the registration buttons still run so the operator can fix the
        store by registering valid records or reporting the corruption.
        """
        repository = self.controller.quality_repository
        failures: list[str] = []

        def _count(kind: str, probe) -> str:
            try:
                value = probe()
                # probes may return a formatted composite label
                # (e.g. profiles + assets) or a plain collection
                return value if isinstance(value, str) else f"{len(value)} 件"
            except EXPECTED_OPERATION_ERRORS as exc:
                if is_authority_failure(exc):
                    raise
                report_boundary_failure(exc, operation='権威データ件数の確認')
                failures.append(kind)
                return "読み込み失敗"

        level_count = _count(
            'レベル校正', lambda: repository.list_level_calibrations()
        )
        self.authority_level_calibration_count.setText(level_count)
        timing_count = _count(
            'タイミング基準', lambda: repository.list_timing_references()
        )
        self.authority_timing_reference_count.setText(timing_count)

        def _stimulus_counts() -> str:
            profiles = repository.list_stimulus_profiles(
                self.controller.document_id
            )
            assets = repository.list_excitation_assets(
                self.controller.document_id
            )
            return f"{len(profiles)} 件（励振 {len(assets)}）"

        self.authority_stimulus_profile_count.setText(
            _count('刺激プロファイル', _stimulus_counts)
        )
        routing_count = _count(
            'ルーティングプロファイル',
            lambda: repository.list_routing_profiles(
                document_id=self.controller.document_id
            ),
        )
        self.authority_routing_profile_count.setText(routing_count)
        if failures:
            self._set_notice(
                f"権威データを読み込めません: {'、'.join(failures)}",
                SemanticState.WARNING,
            )

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
                "除外帯域は下限 < 上限で指定してください。",
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
        except EXPECTED_OPERATION_ERRORS as exc:
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
        self._refresh_registration_panel(views)

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
        self._refresh_registration_panel()

    def _registration_service(self):
        """Lazily build the #564 registration service over the workspace repos."""
        try:
            from ..services.cad_prediction_measurement_service import (
                PredictionMeasurementService,
            )

            return PredictionMeasurementService(
                self.controller.scene_repository,
                self.controller.document_id,
                measurement_repository=self.controller.measurement_repository,
                quality_repository=getattr(
                    self.controller, 'quality_repository', None
                ),
            )
        except EXPECTED_OPERATION_ERRORS as exc:
            if is_authority_failure(exc):
                raise
            report_boundary_failure(exc, operation='登録サービスの初期化')
            return None
            return None

    def _selected_pair_views(self) -> tuple:
        a_id = self.measured_combo.currentData()
        b_id = self.predicted_combo.currentData()
        views = self.controller.measurement_views()
        a_view = next((v for v in views if v.dataset_id == a_id), None)
        b_view = next((v for v in views if v.dataset_id == b_id), None)
        return a_view, b_view

    def _selected_measured_predicted(self) -> tuple:
        """Return (measured_view, predicted_view) or (None, None) (#564)."""
        a_view, b_view = self._selected_pair_views()
        if a_view is None or b_view is None:
            return None, None
        measured, predicted = a_view, b_view
        if a_view.evidence_type == 'predicted':
            measured, predicted = b_view, a_view
        if measured.evidence_type != 'measured' or predicted.evidence_type != 'predicted':
            return None, None
        return measured, predicted

    def _pair_registration(self):
        measured, predicted = self._selected_measured_predicted()
        if measured is None or predicted is None or predicted.dataset_id is None:
            return None
        service = self._registration_service()
        if service is None:
            return None
        for registration in service.list_registrations():
            if (
                registration.measurement.measurement_id == measured.measurement_id
                and registration.prediction.kind == 'imported_prediction_dataset'
                and registration.prediction.prediction_id == predicted.dataset_id
            ):
                return registration
        return None

    def _refresh_registration_panel(self, views=None) -> None:
        if getattr(self, 'registration_table', None) is None:
            return
        service = self._registration_service()
        try:
            registrations = (
                service.list_registrations() if service is not None else ()
            )
        except EXPECTED_OPERATION_ERRORS as exc:
            if is_authority_failure(exc):
                raise
            report_boundary_failure(exc, operation='登録レコードの読み込み')
            registrations = ()
        freshness = {}
        for registration in registrations:
            try:
                freshness[registration.registration_id] = (
                    service.registration_freshness(registration)
                )
            except EXPECTED_OPERATION_ERRORS as exc:
                if is_authority_failure(exc):
                    raise
                # Unreadable freshness is 'unverified' — never 'current':
                # defaulting to current would let a stale pair pass the
                # residual gate (#815).
                report_boundary_failure(exc, operation='登録の新旧判定')
                freshness[registration.registration_id] = 'unverified'
        self.registration_table.setRowCount(len(registrations))
        for row, registration in enumerate(registrations):
            delta = (
                '—'
                if registration.receiver.position_delta_m is None
                else f'{registration.receiver.position_delta_m * 1000:.0f} mm'
            )
            stale = freshness.get(registration.registration_id, 'unverified')
            values = (
                registration.measurement.measurement_id[:10],
                _comparability_state_label(registration.comparability.state),
                (
                    '最新' if stale == 'current'
                    else '確認不可' if stale == 'unverified'
                    else '古い配置'
                ),
                delta,
                _timing_method_label(registration.timing.method),
                _partition_label(registration.partition),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, registration.registration_id)
                self.registration_table.setItem(row, column, item)

        measured, predicted = self._selected_measured_predicted()
        mixed = measured is not None
        registration = self._pair_registration() if mixed else None
        self._current_pair_registration = registration
        self.register_pair_button.setEnabled(
            bool(mixed and service is not None)
        )
        can_compute = bool(
            registration is not None
            and registration.comparability.state
            in ('comparable', 'comparable_with_limitations')
            and freshness.get(registration.registration_id, 'unverified') == 'current'
        )
        self.residual_compute_button.setEnabled(can_compute)
        if not mixed:
            self.registration_state_label.setText(
                '実測×予測ペアを選択すると登録できます。'
            )
        elif registration is None:
            self.registration_state_label.setText(
                '未登録 — 比較する前に登録レコードを作成してください。'
            )
        else:
            verdict = registration.comparability
            parts = [_comparability_state_label(verdict.state)]
            if verdict.comparable_band_hz is not None:
                parts.append(_format_band(verdict.comparable_band_hz))
            if verdict.limitations:
                parts.append(f'制限 {len(verdict.limitations)} 件')
            if freshness.get(registration.registration_id) != 'current':
                parts.append('古い配置（stale）')
            self.registration_state_label.setText('登録済み: ' + ' · '.join(parts))

    def _create_registration(self) -> None:
        measured, predicted = self._selected_measured_predicted()
        if measured is None or predicted is None or predicted.dataset_id is None:
            self._set_notice(
                '実測×予測ペアを選択してください。', SemanticState.WARNING
            )
            return
        service = self._registration_service()
        if service is None:
            self._set_notice('登録サービスを初期化できません。', SemanticState.ERROR)
            return
        try:
            registration = service.register_pair(
                measured.measurement_id,
                prediction_dataset_id=predicted.dataset_id,
                spatial_method='exact_scene_xyz',
                spatial_provenance='manual',
            )
        except EXPECTED_OPERATION_ERRORS as exc:
            self._operation_error_notice('登録できませんでした', exc)
            return
        state = _comparability_state_label(registration.comparability.state)
        self._set_notice(f'登録レコードを作成しました（{state}）。', SemanticState.SUCCESS)
        self._refresh_registration_panel()

    def _compute_residuals(self) -> None:
        registration = getattr(self, '_current_pair_registration', None)
        if registration is None:
            self._set_notice(
                '残差を計算する登録レコードがありません。',
                SemanticState.WARNING,
            )
            return
        service = self._registration_service()
        if service is None:
            self._set_notice('登録サービスを初期化できません。', SemanticState.ERROR)
            return
        try:
            report = service.compute_and_persist_residual_report(
                registration.registration_id
            )
        except EXPECTED_OPERATION_ERRORS as exc:
            self._operation_error_notice('残差を計算できませんでした', exc)
            return
        lines: list[str] = []
        for observable in report.observables:
            if observable.state != 'computed':
                lines.append(
                    f'{_observable_label(observable.observable)}: '
                    f'{_observable_state_label(observable.state)}'
                    + (f'（{observable.reason}）' if observable.reason else '')
                )
                continue
            if observable.observable == 'magnitude_db' and observable.magnitude_bands:
                rms_values = [
                    band.rms_difference_db
                    for band in observable.magnitude_bands
                    if band.rms_difference_db is not None
                ]
                summary = (
                    f'{max(rms_values):.2f} dB（最大帯域RMS）'
                    if rms_values
                    else '算出'
                )
                lines.append(f'帯域レベル残差: {summary}')
            elif observable.observable == 'phase_deg' and observable.phase_bands:
                worst = max(
                    band.mean_abs_difference_deg
                    for band in observable.phase_bands
                    if band.mean_abs_difference_deg is not None
                )
                lines.append(f'位相残差: {worst:.1f} deg（最大帯域平均絶対差）')
            elif observable.observable == 'direct_arrival_time' and observable.direct_arrival is not None:
                arrival = observable.direct_arrival
                lines.append(
                    '到達時刻誤差: '
                    + (
                        f'{arrival.error_s * 1000:.2f} ms'
                        if arrival.error_s is not None
                        else '予測遅延なし'
                    )
                )
            else:
                lines.append(f'{_observable_label(observable.observable)}: 算出')
        self.residual_summary_label.setText(
            '残差レポートを保存しました。\n' + '\n'.join(lines)
        )
        self._set_notice('残差レポートを保存しました。', SemanticState.SUCCESS)

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
        # #564: a measured-vs-predicted comparison requires an explicit
        # registration record — residuals computed without a registered
        # coordinate/timing/level basis would be silently contaminated.
        measured_view, predicted_view = self._selected_measured_predicted()
        if measured_view is not None:
            registration = self._pair_registration()
            if registration is None:
                self._set_notice(
                    '実測×予測の比較には登録レコードが必要です。'
                    '「登録レコードを作成」を実行してください。',
                    SemanticState.WARNING,
                )
                return
            freshness_ok = True
            service = self._registration_service()
            if service is not None:
                freshness_ok = (
                    service.registration_freshness(registration) == 'current'
                )
            if registration.comparability.state not in (
                'comparable',
                'comparable_with_limitations',
            ) or not freshness_ok:
                self._set_notice(
                    'この登録ペアは比較できません: '
                    + _comparability_state_label(registration.comparability.state)
                    + ('（古い配置）' if not freshness_ok else ''),
                    SemanticState.ERROR,
                )
                return
        reference_band = None
        if self.ref_band_check.isChecked():
            low = float(self.ref_low.value())
            high = float(self.ref_high.value())
            if not (low > 0.0 and high > low):
                self._set_notice(
                    "参照帯域は下限 < 上限で指定してください。",
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
        except EXPECTED_OPERATION_ERRORS as exc:
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
            name_item = QTableWidgetItem(name)
            metric_tip = metric_explanation(name)
            if metric_tip:
                name_item.setToolTip(metric_tip)
            self.comparison_metrics.setItem(row_index, 0, name_item)
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
        series, metadata = comparison_export_parts(
            saved, current_scene_revision_id=current_revision_id
        )
        export = build_analysis_export(
            document_id=self.controller.document_id,
            title=f'比較 {saved.comparison_id}',
            generated_at_utc=datetime.now(timezone.utc).isoformat(),
            series=series,
            metadata=metadata,
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
            write_text_atomic(
                Path(selected), render_analysis_csv(export), encoding='utf-8-sig'
            )
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
            row = self._selected_quality_view()
            if row is not None:
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
        call: Callable[[Event], object],
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
            lambda cancel_event: call(cancel_event),
            self._job_completed,
        )

    @Slot(object, object, object)
    def _job_completed(self, key: object, result: object, error: object) -> None:
        key_str = str(key)
        self._rew_auto_job_keys.discard(key_str)
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
                    and to_user_facing_error(error, title=error_prefix).code in RETRYABLE_ERROR_CODES
                ):
                    warn_user(
                        self,
                        error_prefix,
                        error,
                        on_retry=on_retry,
                        on_help=self._open_error_help,
                    )
            return
        on_success(result)

    # ------------------------------------------------------------------
    # #869 HTDT-native sweep acquisition context (REV66)

    def _build_acquisition_page(self) -> None:
        """Minimal honest surface for the HTDT-native sweep engine.

        Shows backend/device truth (empty enumeration and an unavailable
        backend are both legal, honest states), the exact armed
        device/channel/level the operator must confirm, the current stage
        with its blocked reason and next permitted action, and the sealed
        evidence identity once a run terminates. No real audio path is
        wired here — the default WASAPI stub reports unavailable.
        """
        from ...cad_sweep_acquisition import (
            LevelSafetyPolicy,
            _WASAPI_BACKEND_ID,
            _FAKE_BACKEND_ID,
        )
        from ...cad_sweep_acquisition_evidence import (
            SWEEP_ACQUISITION_STAGE_LABELS,
        )

        page, _host, layout = _page(
            "HTDTネイティブ掃引測定",
            "REW等の外部ツールを介さず、掃引信号の生成・再生・録音と"
            "インパルス応答の導出をHTDT自身で行います。"
            "実デバイスでの取得は、バックエンドが利用可能な環境でのみ実行できます。",
        )
        self._acq_stage_labels = SWEEP_ACQUISITION_STAGE_LABELS

        # -- backend + device binding ------------------------------------
        device_card, device = _card("バックエンド・デバイス", page)
        backend_id = getattr(self._acq_backend, 'backend_id', 'unknown')
        if backend_id == _WASAPI_BACKEND_ID:
            if getattr(self._acq_backend, 'available', lambda: False)():
                backend_text = (
                    f"バックエンド: {backend_id}（実デバイスI/O — "
                    "利用可能）")
            else:
                _reason = getattr(
                    self._acq_backend, 'unavailable_reason',
                    lambda: '利用不可')()
                backend_text = (
                    f"バックエンド: {backend_id}（利用不可 — "
                    f"{_reason}）")
        elif backend_id == _FAKE_BACKEND_ID:
            backend_text = (
                f"バックエンド: {backend_id}（シミュレーション — "
                "テスト証跡のみ）")
        else:
            backend_text = f"バックエンド: {backend_id}"
        self.acq_backend_label = QLabel(backend_text, device_card)
        self.acq_backend_label.setWordWrap(True)
        self.acq_backend_label.setAccessibleName("オーディオバックエンド")
        set_typography_role(self.acq_backend_label, TypographyRole.SECONDARY)
        device.addWidget(self.acq_backend_label)

        form = QFormLayout()
        self.acq_playback_device = QComboBox(device_card)
        self.acq_playback_device.setAccessibleName("再生デバイス")
        form.addRow("再生デバイス", self.acq_playback_device)
        self.acq_playback_channel = QSpinBox(device_card)
        self.acq_playback_channel.setRange(0, 31)
        self.acq_playback_channel.setAccessibleName("再生チャンネル")
        form.addRow("再生チャンネル", self.acq_playback_channel)
        self.acq_capture_device = QComboBox(device_card)
        self.acq_capture_device.setAccessibleName("録音デバイス")
        form.addRow("録音デバイス", self.acq_capture_device)
        self.acq_capture_channel = QSpinBox(device_card)
        self.acq_capture_channel.setRange(0, 31)
        self.acq_capture_channel.setAccessibleName("録音チャンネル")
        form.addRow("録音チャンネル", self.acq_capture_channel)
        self.acq_loopback_channel = QComboBox(device_card)
        self.acq_loopback_channel.setAccessibleName("ループバック基準チャンネル")
        self.acq_loopback_channel.addItem("なし（タイミングは限定的）", None)
        for ch in range(8):
            self.acq_loopback_channel.addItem(f"入力 {ch}", ch)
        form.addRow("ループバック基準", self.acq_loopback_channel)
        device.addLayout(form)

        self.acq_devices_note = QLabel("", device_card)
        self.acq_devices_note.setWordWrap(True)
        set_typography_role(self.acq_devices_note, TypographyRole.SECONDARY)
        device.addWidget(self.acq_devices_note)
        refresh_row = QHBoxLayout()
        self.acq_refresh_button = QPushButton("デバイスを更新", device_card)
        self.acq_refresh_button.setAccessibleName("オーディオデバイスを更新")
        self.acq_refresh_button.clicked.connect(self._acq_refresh_devices)
        refresh_row.addWidget(self.acq_refresh_button)
        refresh_row.addStretch(1)
        device.addLayout(refresh_row)
        layout.addWidget(device_card)

        # -- sweep parameters + level safety ------------------------------
        params_card, params = _card("掃引パラメータとレベル安全", page)
        pform = QFormLayout()
        self.acq_start_freq = QDoubleSpinBox(params_card)
        self.acq_start_freq.setRange(1.0, 24000.0)
        self.acq_start_freq.setValue(100.0)
        self.acq_start_freq.setSuffix(" Hz")
        self.acq_start_freq.setAccessibleName("掃引開始周波数")
        pform.addRow("開始周波数", self.acq_start_freq)
        self.acq_end_freq = QDoubleSpinBox(params_card)
        self.acq_end_freq.setRange(2.0, 24000.0)
        self.acq_end_freq.setValue(8000.0)
        self.acq_end_freq.setSuffix(" Hz")
        self.acq_end_freq.setAccessibleName("掃引終了周波数")
        pform.addRow("終了周波数", self.acq_end_freq)
        self.acq_duration = QDoubleSpinBox(params_card)
        self.acq_duration.setRange(0.02, 120.0)
        self.acq_duration.setValue(0.5)
        self.acq_duration.setSuffix(" s")
        self.acq_duration.setAccessibleName("掃引長")
        pform.addRow("掃引長", self.acq_duration)
        self.acq_level = QDoubleSpinBox(params_card)
        self.acq_level.setRange(-80.0, 0.0)
        self.acq_level.setDecimals(1)
        self.acq_level.setValue(-12.0)
        self.acq_level.setSuffix(" dBFS")
        self.acq_level.setAccessibleName("出力レベル dBFS")
        pform.addRow("出力レベル", self.acq_level)
        self.acq_sample_rate = QComboBox(params_card)
        self.acq_sample_rate.setAccessibleName("サンプルレート")
        for rate in (44100, 48000, 96000):
            self.acq_sample_rate.addItem(f"{rate} Hz", rate)
        self.acq_sample_rate.setCurrentIndex(1)
        pform.addRow("サンプルレート", self.acq_sample_rate)
        self.acq_repetitions = QSpinBox(params_card)
        self.acq_repetitions.setRange(1, 16)
        self.acq_repetitions.setValue(2)
        self.acq_repetitions.setAccessibleName("リピート回数")
        pform.addRow("リピート", self.acq_repetitions)
        self.acq_require_absolute = QCheckBox(
            "絶対レベル（SPL）の校正が必要", params_card)
        self.acq_require_absolute.setAccessibleName(
            "絶対レベル校正の要求")
        pform.addRow("", self.acq_require_absolute)
        params.addLayout(pform)
        self._acq_level_policy = LevelSafetyPolicy()
        self.acq_policy_label = QLabel(
            f"ポリシー上限: {self._acq_level_policy.max_output_level_dbfs} "
            "dBFS — 超過した要求はアームを拒否します。",
            params_card,
        )
        self.acq_policy_label.setWordWrap(True)
        set_typography_role(self.acq_policy_label, TypographyRole.SECONDARY)
        params.addWidget(self.acq_policy_label)
        layout.addWidget(params_card)

        # -- run controls --------------------------------------------------
        run_card, run = _card("測定の実行", page)
        self.acq_stage_label = QLabel(
            f"ステージ: {self._acq_stage_labels['precheck']}", run_card)
        self.acq_stage_label.setAccessibleName("取得ステージ")
        run.addWidget(self.acq_stage_label)
        self.acq_blocked_label = QLabel("", run_card)
        self.acq_blocked_label.setWordWrap(True)
        self.acq_blocked_label.setAccessibleName("ブロック理由")
        set_typography_role(self.acq_blocked_label, TypographyRole.SECONDARY)
        run.addWidget(self.acq_blocked_label)
        self.acq_next_action_label = QLabel("", run_card)
        self.acq_next_action_label.setWordWrap(True)
        self.acq_next_action_label.setAccessibleName("次の許可された操作")
        set_typography_role(self.acq_next_action_label, TypographyRole.SECONDARY)
        run.addWidget(self.acq_next_action_label)
        self.acq_progress_label = QLabel("", run_card)
        self.acq_progress_label.setAccessibleName("取得進捗")
        set_typography_role(self.acq_progress_label, TypographyRole.SECONDARY)
        run.addWidget(self.acq_progress_label)

        buttons = QHBoxLayout()
        self.acq_configure_button = QPushButton("事前チェック", run_card)
        self.acq_configure_button.setAccessibleName("取得の事前チェック")
        self.acq_configure_button.clicked.connect(self._acq_configure)
        buttons.addWidget(self.acq_configure_button)
        self.acq_arm_button = QPushButton("アーム", run_card)
        self.acq_arm_button.setAccessibleName("出力をアーム")
        self.acq_arm_button.setToolTip(
            "表示中のデバイス・チャンネル・レベルを確認して出力をアームします。")
        self.acq_arm_button.setWhatsThis(self.acq_arm_button.toolTip())
        self.acq_arm_button.clicked.connect(self._acq_arm)
        buttons.addWidget(self.acq_arm_button)
        self.acq_start_button = QPushButton("測定開始", run_card)
        self.acq_start_button.setAccessibleName("掃引測定を開始")
        self.acq_start_button.clicked.connect(self._acq_start)
        buttons.addWidget(self.acq_start_button)
        self.acq_cancel_button = QPushButton("中止", run_card)
        self.acq_cancel_button.setAccessibleName("掃引測定を中止")
        self.acq_cancel_button.clicked.connect(self._acq_cancel)
        buttons.addWidget(self.acq_cancel_button)
        buttons.addStretch(1)
        run.addLayout(buttons)
        layout.addWidget(run_card)

        # -- result / evidence --------------------------------------------
        result_card, result = _card("結果・証跡", page)
        self.acq_outcome_label = QLabel("結果: —", result_card)
        self.acq_outcome_label.setAccessibleName("取得結果")
        result.addWidget(self.acq_outcome_label)
        self.acq_quality_label = QLabel("品質: —", result_card)
        self.acq_quality_label.setWordWrap(True)
        self.acq_quality_label.setAccessibleName("品質判定")
        result.addWidget(self.acq_quality_label)
        self.acq_measured_label = QLabel("", result_card)
        self.acq_measured_label.setWordWrap(True)
        self.acq_measured_label.setAccessibleName("測定値")
        set_typography_role(self.acq_measured_label, TypographyRole.SECONDARY)
        result.addWidget(self.acq_measured_label)
        self.acq_evidence_label = QLabel("", result_card)
        self.acq_evidence_label.setWordWrap(True)
        self.acq_evidence_label.setAccessibleName("証跡アイデンティティ")
        set_typography_role(self.acq_evidence_label, TypographyRole.SECONDARY)
        result.addWidget(self.acq_evidence_label)
        save_row = QHBoxLayout()
        self.acq_save_button = QPushButton("証跡を保存", result_card)
        self.acq_save_button.setAccessibleName("測定証跡を保存")
        self.acq_save_button.setToolTip(
            "掃引定義・実行レコード・ステージ遷移を封緘レコードとして保存します。")
        self.acq_save_button.setWhatsThis(self.acq_save_button.toolTip())
        self.acq_save_button.clicked.connect(self._acq_save_evidence)
        save_row.addWidget(self.acq_save_button)
        save_row.addStretch(1)
        result.addLayout(save_row)
        layout.addWidget(result_card)

        layout.addStretch(1)
        self.pages.addWidget(page)
        self._acq_refresh_devices()
        self._acq_sync_stage_ui()

    # -- acquisition handlers ---------------------------------------------

    def _acq_refresh_devices(self) -> None:
        """Enumerate devices honestly — an empty list is a legal state."""
        self.acq_playback_device.clear()
        self.acq_capture_device.clear()
        try:
            devices = self._acq_backend.enumerate_devices()
        except EXPECTED_OPERATION_ERRORS:
            devices = ()
        playback = [d for d in devices
                    if d.direction in ('playback', 'duplex')]
        capture = [d for d in devices
                   if d.direction in ('capture', 'duplex')]
        for dev in playback:
            self.acq_playback_device.addItem(
                f"{dev.display_name} ({dev.device_id})", dev.device_id)
        for dev in capture:
            self.acq_capture_device.addItem(
                f"{dev.display_name} ({dev.device_id})", dev.device_id)
        if not devices:
            self.acq_devices_note.setText(
                "列挙されたデバイスがありません — "
                "バックエンドが利用できないか、デバイスが接続されていません。")
        else:
            self.acq_devices_note.setText(
                f"{len(devices)} 台のデバイスを検出しました。")
        # Enumeration changed → any armed state is stale.
        if self._acq_engine is not None:
            self._acq_engine.notify_configuration_changed()
            self._acq_sync_stage_ui()

    def _acq_request(self):
        from ...cad_sweep_acquisition import (
            AcquisitionRequest,
            ChannelRouting,
            SweepStimulusSpec,
        )

        spec = SweepStimulusSpec(
            start_frequency_hz=self.acq_start_freq.value(),
            end_frequency_hz=self.acq_end_freq.value(),
            duration_s=self.acq_duration.value(),
            level_dbfs=self.acq_level.value(),
            sample_rate_hz=int(self.acq_sample_rate.currentData()),
            repetitions=self.acq_repetitions.value(),
        )
        routing = ChannelRouting(
            playback_device_id=str(
                self.acq_playback_device.currentData() or ''),
            playback_channel=self.acq_playback_channel.value(),
            capture_device_id=str(
                self.acq_capture_device.currentData() or ''),
            capture_channel=self.acq_capture_channel.value(),
            loopback_input_channel=self.acq_loopback_channel.currentData(),
        )
        return AcquisitionRequest(
            stimulus=spec,
            routing=routing,
            level_policy=self._acq_level_policy,
            requires_absolute_level=(
                self.acq_require_absolute.isChecked()),
            # Honest: this surface does not resolve a bound calibration —
            # the recorded state stays 'unknown' unless the authority is
            # wired by a later slice.
            calibration_state='unknown',
            declared_synchronized=False,
        )

    def _acq_configure(self) -> None:
        from ...cad_sweep_acquisition import MeasurementAcquisitionEngine

        try:
            request = self._acq_request()
            self._acq_engine = MeasurementAcquisitionEngine(
                self._acq_backend)
            report = self._acq_engine.configure(request)
            if report.ok:
                self._set_notice("事前チェックに合格しました", SemanticState.SUCCESS)
            else:
                self._set_notice(
                    "事前チェックでブロックされました: "
                    + " / ".join(report.blocked_reasons),
                    SemanticState.WARNING,
                )
        except EXPECTED_OPERATION_ERRORS as exc:
            # error-boundary: expected — invalid sweep/device params.
            self._operation_error_notice("事前チェックに失敗しました", exc)
        self._acq_sync_stage_ui()

    def _acq_arm(self) -> None:
        from ...cad_sweep_acquisition import ArmConfirmation

        engine = self._acq_engine
        if engine is None:
            self._set_notice("先に事前チェックを実行してください",
                             SemanticState.WARNING)
            return
        # The level-safety contract: the operator confirms exactly the
        # device/channel/level shown on this surface.
        try:
            engine.arm(ArmConfirmation(
                acknowledged_playback_device_id=str(
                    self.acq_playback_device.currentData() or ''),
                acknowledged_playback_channel=(
                    self.acq_playback_channel.value()),
                acknowledged_capture_device_id=str(
                    self.acq_capture_device.currentData() or ''),
                acknowledged_capture_channel=(
                    self.acq_capture_channel.value()),
                acknowledged_level_dbfs=self.acq_level.value(),
            ))
            self._set_notice("出力をアームしました — 測定開始できます",
                             SemanticState.SUCCESS)
        except EXPECTED_OPERATION_ERRORS as exc:
            # error-boundary: expected — arm gate refusal is operator-facing.
            self._operation_error_notice("アームに失敗しました", exc)
        self._acq_sync_stage_ui()

    def _acq_start(self) -> None:
        engine = self._acq_engine
        if engine is None:
            self._set_notice("先に事前チェックを実行してください",
                             SemanticState.WARNING)
            return
        try:
            # REV66 note: the engine is synchronous; with a real backend a
            # run belongs on the worker pool. The fake backend and the
            # WASAPI stub both return immediately/deterministically.
            engine.start()
        except EXPECTED_OPERATION_ERRORS as exc:
            # error-boundary: expected — backend open/state failures.
            self._operation_error_notice("測定を開始できませんでした", exc)
        self._acq_sync_stage_ui()
        self._acq_sync_result_ui()

    def _acq_cancel(self) -> None:
        engine = self._acq_engine
        if engine is None:
            return
        engine.cancel()
        self._acq_sync_stage_ui()
        self._acq_sync_result_ui()

    def _acq_save_evidence(self) -> None:
        from ...cad_authority_resolver import AuthorityRef
        from ...cad_sweep_acquisition_evidence import (
            build_stimulus_definition,
        )
        from ...cad_sweep_acquisition_repository import (
            CadSweepAcquisitionRepository,
        )
        from ...clock import utc_now_iso

        engine = self._acq_engine
        if engine is None or engine.result.stimulus is None:
            self._set_notice("保存できる証跡がありません — 先に測定を実行してください",
                             SemanticState.WARNING)
            return
        try:
            repo = CadSweepAcquisitionRepository(
                self.controller.scene_repository)
            if self._acq_stimulus_definition is None:
                self._acq_stimulus_definition = build_stimulus_definition(
                    document_id=self.controller.document_id,
                    stimulus=engine.result.stimulus,
                    created_at_utc=utc_now_iso(),
                )
                repo.save_stimulus_definition(
                    self._acq_stimulus_definition)
            stimulus_ref = AuthorityRef(
                kind='sweep_stimulus_definition',
                ref_id=(
                    self._acq_stimulus_definition.stimulus_definition_id),
                ref_sha256=(
                    self._acq_stimulus_definition.stimulus_sha256),
            )
            self._acq_run_record = repo.record_run(
                document_id=self.controller.document_id,
                engine=engine,
                result=engine.result,
                stimulus_ref=stimulus_ref,
            )
            self._set_notice(
                f"証跡を保存しました: {self._acq_run_record.acquisition_id}",
                SemanticState.SUCCESS)
        except EXPECTED_OPERATION_ERRORS as exc:
            # error-boundary: expected — sealed-store rejection stays
            # operator-facing.
            self._operation_error_notice("証跡の保存に失敗しました", exc)
        self._acq_sync_result_ui()

    def _acq_sync_stage_ui(self) -> None:
        engine = self._acq_engine
        stage = engine.stage if engine is not None else 'precheck'
        self.acq_stage_label.setText(
            f"ステージ: {self._acq_stage_labels.get(stage, stage)}")
        blocked = engine.blocked_reasons if engine is not None else ()
        self.acq_blocked_label.setText(
            ("ブロック理由: " + " / ".join(blocked)) if blocked else "")
        next_action = (
            engine.next_permitted_action() if engine is not None
            else 'configure() — resolve devices and routing')
        self.acq_next_action_label.setText(f"次の操作: {next_action}")
        done, total = engine.progress if engine is not None else (0, 0)
        self.acq_progress_label.setText(
            f"進捗: {done}/{total} フレーム" if total else "")
        is_ready = engine is not None and engine.stage == 'ready'
        is_armed = engine is not None and engine.stage == 'armed'
        terminal = engine is not None and engine.stage in (
            'completed', 'failed', 'cancelled')
        self.acq_arm_button.setEnabled(is_ready)
        self.acq_start_button.setEnabled(is_armed)
        self.acq_cancel_button.setEnabled(
            engine is not None and engine.stage == 'armed')
        self.acq_save_button.setEnabled(bool(terminal))
        # Arm button shows exactly what the operator confirms.
        if is_ready:
            self.acq_arm_button.setText(
                f"アーム: "
                f"{self.acq_playback_device.currentData() or '?'}:"
                f"{self.acq_playback_channel.value()} @ "
                f"{self.acq_level.value()} dBFS")
        else:
            self.acq_arm_button.setText("アーム")

    def _acq_sync_result_ui(self) -> None:
        from ...cad_sweep_acquisition_evidence import (
            SWEEP_ACQUISITION_QUALITY_LABELS,
            SWEEP_ACQUISITION_REASON_LABELS,
            SWEEP_TIMING_QUALITY_LABELS,
        )

        engine = self._acq_engine
        result = engine.result if engine is not None else None
        if result is None or engine is None or engine.stage == 'precheck':
            return
        stage = engine.stage
        outcome_text = self._acq_stage_labels.get(stage, stage)
        self.acq_outcome_label.setText(f"結果: {outcome_text}")
        quality = result.quality
        if quality is None:
            self.acq_quality_label.setText("品質: 未評価")
            self.acq_measured_label.setText("")
            self.acq_evidence_label.setText("")
            return
        verdict = SWEEP_ACQUISITION_QUALITY_LABELS.get(
            quality.verdict, quality.verdict)
        reasons = "、".join(
            SWEEP_ACQUISITION_REASON_LABELS.get(r, r)
            for r in quality.reasons)
        warnings = "、".join(quality.warnings)
        parts = [f"品質: {verdict}"]
        if reasons:
            parts.append(f"理由: {reasons}")
        if warnings:
            parts.append(f"警告: {warnings}")
        self.acq_quality_label.setText(" / ".join(parts))
        measured = quality.measured
        bits = []
        if measured.get('snr_db') is not None:
            bits.append(f"SNR {measured['snr_db']:.1f} dB")
        if measured.get('noise_floor_dbfs') is not None:
            bits.append(
                f"ノイズフロア {measured['noise_floor_dbfs']:.1f} dBFS")
        if measured.get('clipped_samples'):
            bits.append(f"クリップ {measured['clipped_samples']} サンプル")
        if measured.get('xrun_count'):
            bits.append(f"xrun {measured['xrun_count']} 回")
        if result.timing is not None:
            tq = SWEEP_TIMING_QUALITY_LABELS.get(
                result.timing.quality, result.timing.quality)
            bits.append(f"タイミング: {tq}")
        self.acq_measured_label.setText(" · ".join(bits))
        evidence = []
        if result.impulse_response is not None and (
                result.impulse_response.ir_sha256):
            evidence.append(
                f"IR: {result.impulse_response.ir_sha256[:16]}…")
        if self._acq_run_record is not None:
            evidence.append(
                f"記録: {self._acq_run_record.acquisition_id}")
            if self._acq_run_record.raw_audio_sha256:
                evidence.append(
                    f"生録音: "
                    f"{self._acq_run_record.raw_audio_sha256[:16]}…")
        self.acq_evidence_label.setText(" / ".join(evidence))

    # -- guided calibration wizard (#877) -------------------------------------

    def _build_calibration_wizard_page(self) -> None:
        """Guided measurement-chain calibration (#877).

        Three lanes — interface loopback calibration, microphone/SPL
        reference check, and campaign pre/post checks — all driving the
        #869 acquisition engine. Software-controlled stages run
        automatically; the only prompts are the physical instructions the
        machine cannot perform. Every step seals a transition so the run
        survives restarts without inventing completion.
        """
        from ...cad_calibration_wizard import (
            CALIBRATION_WIZARD_INSTRUCTION_LABELS,
            CALIBRATION_WIZARD_LANE_LABELS,
            CALIBRATION_WIZARD_STAGE_LABELS,
        )
        from ...cad_sweep_acquisition import (
            _WASAPI_BACKEND_ID,
            _FAKE_BACKEND_ID,
        )

        page, _host, layout = _page(
            "ガイド付き測定チェーン校正",
            "ソフトウェアで実行できる作業は自動化し、ケーブル接続・"
            "校正器の取り付けなど物理的な操作だけを明示的に指示します。"
            "すべてのステップは封緘記録として保存され、中断・再起動しても"
            "完了状態を偽りません。",
        )
        self._wiz_stage_labels = CALIBRATION_WIZARD_STAGE_LABELS
        self._wiz_instruction_labels = CALIBRATION_WIZARD_INSTRUCTION_LABELS
        self._wiz_lane_labels = CALIBRATION_WIZARD_LANE_LABELS
        self._wiz = None
        self._wiz_instruments: dict = {}
        self._wiz_profiles: dict = {}
        self._wiz_campaigns: dict = {}

        # -- lane + backend --------------------------------------------------
        lane_card, lane_box = _card("校正レーン", page)
        backend_id = getattr(self._acq_backend, 'backend_id', 'unknown')
        if backend_id == _WASAPI_BACKEND_ID:
            if getattr(self._acq_backend, 'available', lambda: False)():
                wiz_backend_text = (
                    f"バックエンド: {backend_id}（実デバイスI/O — "
                    "利用可能）")
            else:
                _wiz_reason = getattr(
                    self._acq_backend, 'unavailable_reason',
                    lambda: '利用不可')()
                wiz_backend_text = (
                    f"バックエンド: {backend_id}（利用不可 — "
                    f"{_wiz_reason}）")
        elif backend_id == _FAKE_BACKEND_ID:
            wiz_backend_text = (
                f"バックエンド: {backend_id}（シミュレーション — "
                "テスト証跡のみ）")
        else:
            wiz_backend_text = f"バックエンド: {backend_id}"
        self.wiz_backend_label = QLabel(wiz_backend_text, lane_card)
        self.wiz_backend_label.setWordWrap(True)
        self.wiz_backend_label.setAccessibleName("オーディオバックエンド")
        set_typography_role(self.wiz_backend_label, TypographyRole.SECONDARY)
        lane_box.addWidget(self.wiz_backend_label)

        lane_form = QFormLayout()
        self.wiz_lane_combo = QComboBox(lane_card)
        self.wiz_lane_combo.setAccessibleName("校正レーン")
        for lane_id, label in CALIBRATION_WIZARD_LANE_LABELS.items():
            self.wiz_lane_combo.addItem(label, lane_id)
        self.wiz_lane_combo.currentIndexChanged.connect(
            lambda _i: self.wiz_setup_stack.setCurrentIndex(
                self.wiz_lane_combo.currentIndex()))
        lane_form.addRow("レーン", self.wiz_lane_combo)
        lane_box.addLayout(lane_form)
        self.wiz_persistence_note = QLabel("", lane_card)
        self.wiz_persistence_note.setWordWrap(True)
        set_typography_role(
            self.wiz_persistence_note, TypographyRole.SECONDARY)
        lane_box.addWidget(self.wiz_persistence_note)
        layout.addWidget(lane_card)

        # -- per-lane setup ---------------------------------------------------
        self.wiz_setup_stack = QStackedWidget(page)

        # Lane A: interface loopback — devices, routing, sweep spec.
        lane_a = QWidget()
        a_form = QFormLayout(lane_a)
        self.wiz_playback_device = QComboBox(lane_a)
        self.wiz_playback_device.setAccessibleName("再生デバイス")
        a_form.addRow("再生デバイス", self.wiz_playback_device)
        self.wiz_playback_channel = QSpinBox(lane_a)
        self.wiz_playback_channel.setRange(0, 31)
        self.wiz_playback_channel.setAccessibleName("再生チャンネル")
        a_form.addRow("再生チャンネル", self.wiz_playback_channel)
        self.wiz_capture_device = QComboBox(lane_a)
        self.wiz_capture_device.setAccessibleName("録音デバイス")
        a_form.addRow("録音デバイス", self.wiz_capture_device)
        self.wiz_capture_channel = QSpinBox(lane_a)
        self.wiz_capture_channel.setRange(0, 31)
        self.wiz_capture_channel.setAccessibleName("録音チャンネル")
        a_form.addRow("録音チャンネル", self.wiz_capture_channel)
        self.wiz_loopback_channel = QComboBox(lane_a)
        self.wiz_loopback_channel.setAccessibleName("ループバック基準")
        for ch in range(8):
            self.wiz_loopback_channel.addItem(f"入力 {ch}", ch)
        self.wiz_loopback_channel.setCurrentIndex(1)
        a_form.addRow("ループバック基準", self.wiz_loopback_channel)
        self.wiz_sample_rate = QComboBox(lane_a)
        self.wiz_sample_rate.setAccessibleName("サンプルレート")
        for rate in (44100, 48000, 96000):
            self.wiz_sample_rate.addItem(f"{rate} Hz", rate)
        self.wiz_sample_rate.setCurrentIndex(1)
        a_form.addRow("サンプルレート", self.wiz_sample_rate)
        self.wiz_start_freq = QDoubleSpinBox(lane_a)
        self.wiz_start_freq.setRange(1.0, 24000.0)
        self.wiz_start_freq.setValue(100.0)
        self.wiz_start_freq.setSuffix(" Hz")
        self.wiz_start_freq.setAccessibleName("掃引開始周波数")
        a_form.addRow("開始周波数", self.wiz_start_freq)
        self.wiz_end_freq = QDoubleSpinBox(lane_a)
        self.wiz_end_freq.setRange(2.0, 24000.0)
        self.wiz_end_freq.setValue(8000.0)
        self.wiz_end_freq.setSuffix(" Hz")
        self.wiz_end_freq.setAccessibleName("掃引終了周波数")
        a_form.addRow("終了周波数", self.wiz_end_freq)
        self.wiz_duration = QDoubleSpinBox(lane_a)
        self.wiz_duration.setRange(0.02, 120.0)
        self.wiz_duration.setValue(0.5)
        self.wiz_duration.setSuffix(" s")
        self.wiz_duration.setAccessibleName("掃引長")
        a_form.addRow("掃引長", self.wiz_duration)
        self.wiz_level = QDoubleSpinBox(lane_a)
        self.wiz_level.setRange(-80.0, -6.0)
        self.wiz_level.setDecimals(1)
        self.wiz_level.setValue(-12.0)
        self.wiz_level.setSuffix(" dBFS")
        self.wiz_level.setAccessibleName("出力レベル dBFS")
        a_form.addRow("出力レベル", self.wiz_level)
        lane_a_note = QLabel(
            "出力→入力のループバックケーブルを指示されたら接続します。"
            "DAC+ADCの結合校正として記録されます。",
            lane_a)
        lane_a_note.setWordWrap(True)
        set_typography_role(lane_a_note, TypographyRole.SECONDARY)
        a_form.addRow(lane_a_note)
        self.wiz_setup_stack.addWidget(lane_a)

        # Lane B: instrument + acceptance profile + check kind.
        lane_b = QWidget()
        b_form = QFormLayout(lane_b)
        self.wiz_instrument_combo = QComboBox(lane_b)
        self.wiz_instrument_combo.setAccessibleName("対象機器")
        b_form.addRow("対象機器", self.wiz_instrument_combo)
        new_box = QHBoxLayout()
        self.wiz_inst_manufacturer = QLineEdit(lane_b)
        self.wiz_inst_manufacturer.setPlaceholderText("メーカー")
        self.wiz_inst_manufacturer.setAccessibleName("機器メーカー")
        self.wiz_inst_model = QLineEdit(lane_b)
        self.wiz_inst_model.setPlaceholderText("型番")
        self.wiz_inst_model.setAccessibleName("機器型番")
        self.wiz_inst_serial = QLineEdit(lane_b)
        self.wiz_inst_serial.setPlaceholderText("シリアル/資産ID *")
        self.wiz_inst_serial.setAccessibleName("機器シリアルまたは資産ID")
        for w in (self.wiz_inst_manufacturer, self.wiz_inst_model,
                  self.wiz_inst_serial):
            new_box.addWidget(w)
        b_form.addRow("新規機器", new_box)
        self.wiz_check_kind = QComboBox(lane_b)
        self.wiz_check_kind.setAccessibleName("チェック種別")
        for kind, label in (
            ('interim_check', '中間チェック'),
            ('pre_use_field_check', '使用前フィールドチェック'),
            ('post_use_field_check', '使用後フィールドチェック'),
            ('periodic_test', '定期試験'),
        ):
            self.wiz_check_kind.addItem(label, kind)
        b_form.addRow("チェック種別", self.wiz_check_kind)
        self.wiz_profile_name = QLineEdit(lane_b)
        self.wiz_profile_name.setText("field-check-94dB-1k")
        self.wiz_profile_name.setAccessibleName("合格基準プロファイル名")
        b_form.addRow("プロファイル名", self.wiz_profile_name)
        self.wiz_ref_level = QDoubleSpinBox(lane_b)
        self.wiz_ref_level.setRange(30.0, 150.0)
        self.wiz_ref_level.setValue(94.0)
        self.wiz_ref_level.setSuffix(" dB")
        self.wiz_ref_level.setAccessibleName("校正器の公称レベル")
        b_form.addRow("公称レベル", self.wiz_ref_level)
        self.wiz_ref_freq = QDoubleSpinBox(lane_b)
        self.wiz_ref_freq.setRange(10.0, 24000.0)
        self.wiz_ref_freq.setValue(1000.0)
        self.wiz_ref_freq.setSuffix(" Hz")
        self.wiz_ref_freq.setAccessibleName("基準周波数")
        b_form.addRow("基準周波数", self.wiz_ref_freq)
        self.wiz_expected_level = QDoubleSpinBox(lane_b)
        self.wiz_expected_level.setRange(-120.0, 0.0)
        self.wiz_expected_level.setDecimals(1)
        self.wiz_expected_level.setValue(-20.0)
        self.wiz_expected_level.setSuffix(" dBFS")
        self.wiz_expected_level.setAccessibleName("期待測定レベル")
        b_form.addRow("期待レベル", self.wiz_expected_level)
        self.wiz_level_tol = QDoubleSpinBox(lane_b)
        self.wiz_level_tol.setRange(0.05, 10.0)
        self.wiz_level_tol.setValue(1.0)
        self.wiz_level_tol.setSuffix(" dB")
        self.wiz_level_tol.setAccessibleName("レベル許容差")
        b_form.addRow("レベル許容差", self.wiz_level_tol)
        self.wiz_freq_tol = QDoubleSpinBox(lane_b)
        self.wiz_freq_tol.setRange(0.5, 500.0)
        self.wiz_freq_tol.setValue(10.0)
        self.wiz_freq_tol.setSuffix(" Hz")
        self.wiz_freq_tol.setAccessibleName("周波数許容差")
        b_form.addRow("周波数許容差", self.wiz_freq_tol)
        self.wiz_stability_tol = QDoubleSpinBox(lane_b)
        self.wiz_stability_tol.setRange(0.05, 10.0)
        self.wiz_stability_tol.setValue(0.5)
        self.wiz_stability_tol.setSuffix(" dB")
        self.wiz_stability_tol.setAccessibleName("安定性許容差")
        b_form.addRow("安定性", self.wiz_stability_tol)
        self.wiz_min_snr = QDoubleSpinBox(lane_b)
        self.wiz_min_snr.setRange(3.0, 120.0)
        self.wiz_min_snr.setValue(20.0)
        self.wiz_min_snr.setSuffix(" dB")
        self.wiz_min_snr.setAccessibleName("最低SNR")
        b_form.addRow("最低SNR", self.wiz_min_snr)
        self.wiz_check_duration = QDoubleSpinBox(lane_b)
        self.wiz_check_duration.setRange(0.1, 60.0)
        self.wiz_check_duration.setValue(0.5)
        self.wiz_check_duration.setSuffix(" s")
        self.wiz_check_duration.setAccessibleName("録音窓の長さ")
        b_form.addRow("録音窓", self.wiz_check_duration)
        lane_b_note = QLabel(
            "校正器・基準ソースを対象機器に取り付け、安定させてから"
            "セットアップを確認します。野外チェックは試験所校正では"
            "ありません — 機器適合性の更新として記録されます。",
            lane_b)
        lane_b_note.setWordWrap(True)
        set_typography_role(lane_b_note, TypographyRole.SECONDARY)
        b_form.addRow(lane_b_note)
        self.wiz_setup_stack.addWidget(lane_b)

        # Lane C: campaign + check plan (sealed before the run).
        lane_c = QWidget()
        c_form = QFormLayout(lane_c)
        self.wiz_campaign_combo = QComboBox(lane_c)
        self.wiz_campaign_combo.setAccessibleName("対象キャンペーン")
        c_form.addRow("キャンペーン", self.wiz_campaign_combo)
        self.wiz_c_instrument_combo = QComboBox(lane_c)
        self.wiz_c_instrument_combo.setAccessibleName("チェック対象機器")
        c_form.addRow("対象機器", self.wiz_c_instrument_combo)
        self.wiz_c_profile_combo = QComboBox(lane_c)
        self.wiz_c_profile_combo.setAccessibleName("合格基準プロファイル")
        c_form.addRow("合格基準", self.wiz_c_profile_combo)
        checks_row = QHBoxLayout()
        self.wiz_c_pre = QCheckBox("使用前チェック", lane_c)
        self.wiz_c_pre.setChecked(True)
        self.wiz_c_pre.setAccessibleName("使用前フィールドチェックを要求")
        self.wiz_c_post = QCheckBox("使用後チェック", lane_c)
        self.wiz_c_post.setChecked(True)
        self.wiz_c_post.setAccessibleName("使用後フィールドチェックを要求")
        checks_row.addWidget(self.wiz_c_pre)
        checks_row.addWidget(self.wiz_c_post)
        checks_row.addStretch(1)
        c_form.addRow("要求チェック", checks_row)
        self.wiz_c_blocking = QCheckBox(
            "失敗時はキャンペーン証跡をブロック", lane_c)
        self.wiz_c_blocking.setChecked(True)
        self.wiz_c_blocking.setAccessibleName("失敗時ブロックポリシー")
        c_form.addRow("ポリシー", self.wiz_c_blocking)
        lane_c_note = QLabel(
            "計画は実行前に封緘されます — 要求されたチェックを"
            "後から弱めることはできません。機器とプロファイルは"
            "登録済みレコードから選びます。",
            lane_c)
        lane_c_note.setWordWrap(True)
        set_typography_role(lane_c_note, TypographyRole.SECONDARY)
        c_form.addRow(lane_c_note)
        self.wiz_setup_stack.addWidget(lane_c)
        layout.addWidget(self.wiz_setup_stack)

        devices_row = QHBoxLayout()
        self.wiz_refresh_button = QPushButton(
            "デバイス・レコードを更新", page)
        self.wiz_refresh_button.setAccessibleName(
            "デバイスと機器レコードを更新")
        self.wiz_refresh_button.clicked.connect(
            self._wiz_refresh_devices)
        devices_row.addWidget(self.wiz_refresh_button)
        devices_row.addStretch(1)
        layout.addLayout(devices_row)

        # -- guidance: the physical instruction -------------------------------
        guide_card, guide = _card("ガイダンス", page)
        self.wiz_stage_label = QLabel("ステージ: —", guide_card)
        self.wiz_stage_label.setAccessibleName("校正ウィザードのステージ")
        guide.addWidget(self.wiz_stage_label)
        self.wiz_instruction_label = QLabel("", guide_card)
        self.wiz_instruction_label.setWordWrap(True)
        self.wiz_instruction_label.setAccessibleName("物理操作の指示")
        guide.addWidget(self.wiz_instruction_label)
        self.wiz_blocked_label = QLabel("", guide_card)
        self.wiz_blocked_label.setWordWrap(True)
        self.wiz_blocked_label.setAccessibleName("ブロック理由")
        set_typography_role(self.wiz_blocked_label, TypographyRole.SECONDARY)
        guide.addWidget(self.wiz_blocked_label)
        self.wiz_next_label = QLabel("", guide_card)
        self.wiz_next_label.setWordWrap(True)
        self.wiz_next_label.setAccessibleName("次に許可されたイベント")
        set_typography_role(self.wiz_next_label, TypographyRole.SECONDARY)
        guide.addWidget(self.wiz_next_label)
        layout.addWidget(guide_card)

        # -- controls ----------------------------------------------------------
        ctrl_card, ctrl = _card("操作", page)
        buttons = QHBoxLayout()
        self.wiz_begin_button = QPushButton("開始", ctrl_card)
        self.wiz_begin_button.setAccessibleName("校正ウィザードを開始")
        self.wiz_begin_button.setToolTip(
            "選択したレーンの実行を封緘して開始します。")
        self.wiz_begin_button.setWhatsThis(self.wiz_begin_button.toolTip())
        self.wiz_begin_button.clicked.connect(self._wiz_begin)
        buttons.addWidget(self.wiz_begin_button)
        self.wiz_confirm_button = QPushButton("セットアップを確認", ctrl_card)
        self.wiz_confirm_button.setAccessibleName("物理セットアップを確認")
        self.wiz_confirm_button.setToolTip(
            "指示された物理操作を完了したことを記録します。")
        self.wiz_confirm_button.setWhatsThis(
            self.wiz_confirm_button.toolTip())
        self.wiz_confirm_button.clicked.connect(self._wiz_confirm)
        buttons.addWidget(self.wiz_confirm_button)
        self.wiz_campaign_done_button = QPushButton(
            "キャンペーン完了を記録", ctrl_card)
        self.wiz_campaign_done_button.setAccessibleName(
            "キャンペーン測定の完了を記録")
        self.wiz_campaign_done_button.clicked.connect(
            self._wiz_campaign_done)
        buttons.addWidget(self.wiz_campaign_done_button)
        self.wiz_retry_button = QPushButton("再試行", ctrl_card)
        self.wiz_retry_button.setAccessibleName("ブロックされたステップを再試行")
        self.wiz_retry_button.setToolTip(
            "ブロックされたステップへ戻り、ソフトウェア処理をやり直します。")
        self.wiz_retry_button.setWhatsThis(self.wiz_retry_button.toolTip())
        self.wiz_retry_button.clicked.connect(self._wiz_retry)
        buttons.addWidget(self.wiz_retry_button)
        self.wiz_cancel_button = QPushButton("中止", ctrl_card)
        self.wiz_cancel_button.setAccessibleName("校正ウィザードを中止")
        self.wiz_cancel_button.clicked.connect(self._wiz_cancel)
        buttons.addWidget(self.wiz_cancel_button)
        buttons.addStretch(1)
        ctrl.addLayout(buttons)
        layout.addWidget(ctrl_card)

        # -- evidence ------------------------------------------------------------
        ev_card, ev = _card("証跡", page)
        self.wiz_run_label = QLabel("実行: —", ev_card)
        self.wiz_run_label.setAccessibleName("校正ウィザード実行ID")
        ev.addWidget(self.wiz_run_label)
        self.wiz_artifacts_label = QLabel("", ev_card)
        self.wiz_artifacts_label.setWordWrap(True)
        self.wiz_artifacts_label.setAccessibleName("封緘された証跡")
        set_typography_role(
            self.wiz_artifacts_label, TypographyRole.SECONDARY)
        ev.addWidget(self.wiz_artifacts_label)
        self.wiz_log = QListWidget(ev_card)
        self.wiz_log.setAccessibleName("遷移ログ")
        self.wiz_log.setMinimumHeight(140)
        ev.addWidget(self.wiz_log)
        layout.addWidget(ev_card)

        layout.addStretch(1)
        self.pages.addWidget(page)
        self._wiz_refresh_devices()
        self._wiz_sync_ui()

    # -- wizard helpers -------------------------------------------------------

    def _wiz_stores(self):
        """Real repositories when the scene has a database, else in-memory.

        Without a scene repository nothing can persist — the page states
        that honestly instead of implying evidence was saved.
        """
        from ...cad_calibration_wizard import WizardStores

        scene_repo = getattr(self.controller, 'scene_repository', None)
        if scene_repo is None:
            return WizardStores(), False
        from ...cad_calibration_wizard_repository import (
            CadCalibrationWizardRepository,
        )
        from ...cad_calibration_lifecycle_repository import (
            CadCalibrationLifecycleRepository,
        )
        from ...cad_interface_loopback_repository import (
            CadInterfaceLoopbackRepository,
        )
        from ...cad_sweep_acquisition_repository import (
            CadSweepAcquisitionRepository,
        )
        try:
            return WizardStores(
                wizard=CadCalibrationWizardRepository(scene_repo),
                sweep=CadSweepAcquisitionRepository(scene_repo),
                interface=CadInterfaceLoopbackRepository(scene_repo),
                lifecycle=CadCalibrationLifecycleRepository(scene_repo),
            ), True
        except EXPECTED_OPERATION_ERRORS:
            # error-boundary: expected — DB unavailable/locked means
            # in-memory evidence, not a fake persisted one.
            return WizardStores(), False

    def _wiz_refresh_devices(self) -> None:
        """Enumerate devices + registered records honestly."""
        from ...cad_sweep_acquisition import ChannelRouting  # noqa: F401

        self.wiz_playback_device.clear()
        self.wiz_capture_device.clear()
        try:
            devices = self._acq_backend.enumerate_devices()
        except EXPECTED_OPERATION_ERRORS:
            devices = ()
        playback = [d for d in devices
                    if d.direction in ('playback', 'duplex')]
        capture = [d for d in devices
                   if d.direction in ('capture', 'duplex')]
        for dev in playback:
            self.wiz_playback_device.addItem(
                f"{dev.display_name} ({dev.device_id})", dev.device_id)
        for dev in capture:
            self.wiz_capture_device.addItem(
                f"{dev.display_name} ({dev.device_id})", dev.device_id)
        stores, persisted = self._wiz_stores()
        self.wiz_persistence_note.setText(
            "" if persisted else
            "注: シーンデータベースが開かれていないため、証跡は"
            "このセッション内でのみ保持されます。")
        self._wiz_instruments.clear()
        self._wiz_profiles.clear()
        self._wiz_campaigns.clear()
        self.wiz_instrument_combo.clear()
        self.wiz_c_instrument_combo.clear()
        self.wiz_c_profile_combo.clear()
        self.wiz_campaign_combo.clear()
        if stores.lifecycle is not None:
            for inst in stores.lifecycle.list_instruments(
                    getattr(self.controller, 'document_id', None)):
                self._wiz_instruments[inst.instrument_id] = inst
                label = inst.serial_or_instance_id
                if inst.model:
                    label += f" ({inst.model})"
                self.wiz_instrument_combo.addItem(label, inst.instrument_id)
                self.wiz_c_instrument_combo.addItem(
                    label, inst.instrument_id)
        self.wiz_instrument_combo.addItem(
            "（新規登録 — 下の欄を入力）", '__new__')
        if stores.wizard is not None:
            for prof in stores.wizard.list_profiles(
                    getattr(self.controller, 'document_id', None)):
                self._wiz_profiles[prof.profile_id] = prof
                self.wiz_c_profile_combo.addItem(
                    f"{prof.name} ({prof.profile_id[:12]}…)",
                    prof.profile_id)
        if persisted:
            from ...cad_owned_room_campaign_repository import (
                CadOwnedRoomCampaignRepository,
            )
            try:
                campaign_repo = CadOwnedRoomCampaignRepository(
                    self.controller.scene_repository)
                for prereg in campaign_repo.preregistrations.list(
                        getattr(self.controller, 'document_id', None)):
                    self._wiz_campaigns[
                        prereg.preregistration_id] = prereg
                    self.wiz_campaign_combo.addItem(
                        prereg.preregistration_id,
                        prereg.preregistration_id)
            except EXPECTED_OPERATION_ERRORS:
                # error-boundary: expected — no campaign tables yet.
                pass
        if self.wiz_campaign_combo.count() == 0:
            self.wiz_campaign_combo.addItem("（登録済みキャンペーンなし）", None)
        if self.wiz_c_profile_combo.count() == 0:
            self.wiz_c_profile_combo.addItem("（登録済みプロファイルなし）", None)

    def _wiz_selected_instrument(self, combo) -> tuple | None:
        """(instrument, needs_save) — a registered record or a new one
        built from the entry fields. Never invents an identity."""
        from ...cad_calibration_lifecycle import build_instrument_instance
        from ...clock import utc_now_iso

        data = combo.currentData()
        if data and data != '__new__':
            return self._wiz_instruments.get(data), False
        serial = self.wiz_inst_serial.text().strip()
        if not serial:
            return None, False
        inst = build_instrument_instance(
            document_id=self.controller.document_id,
            category='measurement_microphone',
            manufacturer=self.wiz_inst_manufacturer.text().strip() or None,
            model=self.wiz_inst_model.text().strip() or None,
            serial_or_instance_id=serial,
            declared_at_utc=utc_now_iso(),
        )
        return inst, True

    def _wiz_routing(self):
        from ...cad_sweep_acquisition import (
            ChannelRouting, SweepStimulusSpec,
        )

        spec = SweepStimulusSpec(
            start_frequency_hz=self.wiz_start_freq.value(),
            end_frequency_hz=self.wiz_end_freq.value(),
            duration_s=self.wiz_duration.value(),
            level_dbfs=self.wiz_level.value(),
            sample_rate_hz=int(self.wiz_sample_rate.currentData()),
            repetitions=2,
        )
        routing = ChannelRouting(
            playback_device_id=str(
                self.wiz_playback_device.currentData() or ''),
            playback_channel=self.wiz_playback_channel.value(),
            capture_device_id=str(
                self.wiz_capture_device.currentData() or ''),
            capture_channel=self.wiz_capture_channel.value(),
            loopback_input_channel=self.wiz_loopback_channel.currentData(),
        )
        return spec, routing

    def _wiz_check_routing(self):
        """Record-window spec for check lanes — a short near-silent
        playback while the physical calibrator supplies the tone."""
        from ...cad_sweep_acquisition import (
            ChannelRouting, SweepStimulusSpec,
        )

        spec = SweepStimulusSpec(
            start_frequency_hz=100.0,
            end_frequency_hz=8000.0,
            duration_s=self.wiz_check_duration.value(),
            level_dbfs=-60.0,
            sample_rate_hz=int(self.wiz_sample_rate.currentData()),
            repetitions=1,
        )
        routing = ChannelRouting(
            playback_device_id=str(
                self.wiz_playback_device.currentData() or ''),
            playback_channel=self.wiz_playback_channel.value(),
            capture_device_id=str(
                self.wiz_capture_device.currentData() or ''),
            capture_channel=self.wiz_capture_channel.value(),
            loopback_input_channel=self.wiz_loopback_channel.currentData(),
        )
        return spec, routing

    def _wiz_profile_from_fields(self):
        """Seal the acceptance profile exactly as the operator set it."""
        from ...cad_calibration_wizard import CadSplCheckAcceptanceProfile
        from ...clock import utc_now_iso

        return CadSplCheckAcceptanceProfile.create(
            document_id=self.controller.document_id,
            name=self.wiz_profile_name.text().strip()
            or f"profile-{utc_now_iso()}",
            reference_level_db=self.wiz_ref_level.value(),
            reference_frequency_hz=self.wiz_ref_freq.value(),
            expected_measured_level_db=self.wiz_expected_level.value(),
            level_tolerance_db=self.wiz_level_tol.value(),
            frequency_tolerance_hz=self.wiz_freq_tol.value(),
            max_level_deviation_db=self.wiz_stability_tol.value(),
            min_snr_db=self.wiz_min_snr.value(),
            min_duration_s=max(0.05, self.wiz_check_duration.value() * 0.6),
            declared_at_utc=utc_now_iso(),
            declared_by='operator',
        )

    def _wiz_begin(self) -> None:
        from ...cad_authority_resolver import AuthorityRef
        from ...cad_calibration_lifecycle import instrument_binding
        from ...cad_calibration_wizard import (
            CalibrationWizard, CadCampaignCheckPlan,
            RequiredCheckSpec, spl_profile_binding,
        )
        from ...cad_interface_loopback import CadInterfaceIoPath
        from ...clock import utc_now_iso

        lane = self.wiz_lane_combo.currentData()
        stores, persisted = self._wiz_stores()
        self._wiz = CalibrationWizard(
            self.controller.document_id, self._acq_backend,
            stores=stores)
        try:
            if lane == 'interface_loopback':
                spec, routing = self._wiz_routing()
                if not routing.playback_device_id \
                        or not routing.capture_device_id:
                    self._set_notice(
                        "再生・録音デバイスを選択してください",
                        SemanticState.WARNING)
                    self._wiz = None
                    return
                backend_id = getattr(
                    self._acq_backend, 'backend_id', 'unknown')
                io_path = CadInterfaceIoPath(
                    output_device=routing.playback_device_id,
                    output_port=str(routing.playback_channel),
                    output_channel=str(routing.playback_channel),
                    input_device=routing.capture_device_id,
                    input_port=str(routing.capture_channel),
                    input_channel=str(routing.capture_channel),
                    sample_rate_hz=float(spec.sample_rate_hz),
                    loopback_path_kind='analog',
                    acquisition_class='analog_interface',
                    driver_backend=backend_id,
                )
                self._wiz.begin_loopback(
                    io_path=io_path, routing=routing, stimulus_spec=spec,
                    created_by='operator', at_utc=utc_now_iso())
            elif lane == 'spl_reference_check':
                instrument, needs_save = self._wiz_selected_instrument(
                    self.wiz_instrument_combo)
                if instrument is None:
                    self._set_notice(
                        "対象機器を選ぶか、新規機器を登録してください",
                        SemanticState.WARNING)
                    self._wiz = None
                    return
                if needs_save and stores.lifecycle is not None:
                    stores.lifecycle.save_instrument(instrument)
                profile = self._wiz_profile_from_fields()
                spec, routing = self._wiz_check_routing()
                self._wiz.begin_reference_check(
                    instrument=instrument, acceptance_profile=profile,
                    routing=routing, stimulus_spec=spec,
                    check_kind=self.wiz_check_kind.currentData(),
                    created_by='operator', at_utc=utc_now_iso())
            else:  # campaign_checks
                campaign_id = self.wiz_campaign_combo.currentData()
                instrument, needs_save = self._wiz_selected_instrument(
                    self.wiz_c_instrument_combo)
                profile_id = self.wiz_c_profile_combo.currentData()
                if not campaign_id or instrument is None \
                        or not profile_id:
                    self._set_notice(
                        "キャンペーン・機器・合格基準を選択してください",
                        SemanticState.WARNING)
                    self._wiz = None
                    return
                if needs_save and stores.lifecycle is not None:
                    stores.lifecycle.save_instrument(instrument)
                prereg = self._wiz_campaigns.get(campaign_id)
                campaign_ref = AuthorityRef(
                    kind='campaign_preregistration',
                    ref_id=prereg.preregistration_id,
                    ref_sha256=prereg.preregistration_sha256,
                )
                profile = self._wiz_profiles[profile_id]
                spec_routing = self._wiz_check_routing()
                inst_ref = instrument_binding(instrument)
                prof_ref = spl_profile_binding(profile)
                specs = []
                if self.wiz_c_pre.isChecked():
                    specs.append(RequiredCheckSpec(
                        instrument_ref=inst_ref,
                        check_kind='pre_use_field_check',
                        acceptance_profile_ref=prof_ref))
                if self.wiz_c_post.isChecked():
                    specs.append(RequiredCheckSpec(
                        instrument_ref=inst_ref,
                        check_kind='post_use_field_check',
                        acceptance_profile_ref=prof_ref))
                if not specs:
                    self._set_notice(
                        "少なくとも1つのチェックを要求してください",
                        SemanticState.WARNING)
                    self._wiz = None
                    return
                plan = CadCampaignCheckPlan.create(
                    document_id=self.controller.document_id,
                    campaign_ref=campaign_ref,
                    required_checks=tuple(specs),
                    blocked_on_failure=self.wiz_c_blocking.isChecked(),
                    declared_at_utc=utc_now_iso(), declared_by='operator')
                self._wiz.begin_campaign_checks(
                    campaign_ref=campaign_ref, plan=plan,
                    routing=spec_routing[1], stimulus_spec=spec_routing[0],
                    created_by='operator', at_utc=utc_now_iso())
        except EXPECTED_OPERATION_ERRORS as exc:
            # error-boundary: expected — invalid inputs reject before any
            # run is sealed.
            self._operation_error_notice("校正の開始に失敗しました", exc)
            self._wiz = None
        except ValueError as exc:
            self._operation_error_notice("校正の開始に失敗しました", exc)
            self._wiz = None
        self._wiz_sync_ui()

    def _wiz_confirm(self) -> None:
        """Physical step done → machine records it, software resumes."""
        if self._wiz is None:
            return
        try:
            self._wiz.confirm_hardware()
            self._wiz.run_automatic()
        except EXPECTED_OPERATION_ERRORS as exc:
            # error-boundary: expected — backend open/capture failures.
            self._operation_error_notice("ステップの実行に失敗しました", exc)
        self._wiz_sync_ui()

    def _wiz_campaign_done(self) -> None:
        if self._wiz is None:
            return
        try:
            self._wiz.record_campaign_completed()
        except EXPECTED_OPERATION_ERRORS as exc:
            self._operation_error_notice("記録に失敗しました", exc)
        self._wiz_sync_ui()

    def _wiz_retry(self) -> None:
        if self._wiz is None:
            return
        try:
            self._wiz.request_retry()
            self._wiz.run_automatic()
        except EXPECTED_OPERATION_ERRORS as exc:
            self._operation_error_notice("再試行に失敗しました", exc)
        self._wiz_sync_ui()

    def _wiz_cancel(self) -> None:
        if self._wiz is None:
            return
        try:
            self._wiz.cancel()
        except EXPECTED_OPERATION_ERRORS as exc:
            self._operation_error_notice("中止に失敗しました", exc)
        self._wiz_sync_ui()

    def _wiz_sync_ui(self) -> None:
        from ...cad_calibration_wizard import next_permitted_events

        wiz = self._wiz
        state = wiz.state if wiz is not None else None
        stage = state.current_stage if state is not None else None
        self.wiz_stage_label.setText(
            "ステージ: "
            + (self._wiz_stage_labels.get(stage, stage)
               if stage else '—'))
        instruction = wiz.pending_instruction if wiz is not None else None
        if instruction is not None:
            self.wiz_instruction_label.setText(
                "物理操作: "
                + self._wiz_instruction_labels.get(instruction, instruction))
        else:
            self.wiz_instruction_label.setText("")
        blocked = (
            state.blocked_reason if state is not None else None)
        self.wiz_blocked_label.setText(
            f"ブロック理由: {blocked}" if blocked else "")
        if state is not None:
            permitted = ', '.join(next_permitted_events(state) or ('—',))
            self.wiz_next_label.setText(f"次のイベント: {permitted}")
        else:
            self.wiz_next_label.setText("")

        terminal = state is not None and (
            state.completed or state.cancelled or state.failed)
        can_begin = wiz is None or terminal
        self.wiz_begin_button.setEnabled(can_begin)
        self.wiz_confirm_button.setEnabled(
            instruction in (
                'connect_loopback_cable', 'position_calibrator')
            and not state.device_disconnected)
        self.wiz_campaign_done_button.setEnabled(
            stage == 'await_campaign')
        self.wiz_retry_button.setEnabled(
            state is not None and state.blocked_reason is not None
            and not state.device_disconnected)
        self.wiz_cancel_button.setEnabled(
            state is not None and not terminal)

        if wiz is not None and wiz.run is not None:
            self.wiz_run_label.setText(
                f"実行: {wiz.run.run_id} "
                f"({self._wiz_lane_labels.get(wiz.run.lane, wiz.run.lane)})")
            artifacts = []
            for kind in sorted(state.evidence):
                ref = state.evidence[kind]
                artifacts.append(f"{kind}: {ref.ref_id[:18]}…")
            self.wiz_artifacts_label.setText(
                " / ".join(artifacts) if artifacts else "")
        else:
            self.wiz_run_label.setText("実行: —")
            self.wiz_artifacts_label.setText("")
        self.wiz_log.clear()
        if wiz is not None:
            for t in wiz.transitions:
                self.wiz_log.addItem(
                    f"[{t.seq}] {t.event_kind} → {t.outcome} "
                    f"({self._wiz_stage_labels.get(t.to_stage, t.to_stage)})")
            self.wiz_log.scrollToBottom()

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
        # REV32-TERMS: an error notice carries its own meaning+recovery —
        # the action opens the help topic bound to this error code.
        action = None
        if self._help_available():
            action = (
                "意味と対処",
                lambda code=error.code: self._open_error_help(code),
            )
        self._set_notice(error.notice_text(), error.severity, action=action)

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

    def _user_busy_count(self) -> int:
        """In-flight jobs excluding REW automation polls (REV40-REWAUTO).

        A background poll must never hold the operator on this page — only
        user-initiated work gates navigation. Clamped at zero: a finished
        auto job drops ``active_count`` before its key is discarded on the
        next event pass, and a negative count would still read as 'busy'.
        """
        return max(
            0,
            self._job_pool.active_count - len(self._rew_auto_job_keys),
        )

    def _pending_batch_ids(self) -> set[str]:
        """Uncommitted batch rows the operator has not acknowledged.

        The batch queue is in-memory and (REV40+) also fed by the REW
        automation, so uncommitted rows are unsaved work exactly like a
        staged pending import — deactivation must not silently drop them.
        keep_draft releases the ids current at acknowledgement; a newly
        staged row re-arms the gate.
        """
        return set(
            self.controller.uncommitted_batch_item_ids()
        ) - self._batch_released_ids

    def before_deactivate(self) -> tuple[bool, str | None]:
        if self._user_busy_count():
            return False, "バックグラウンド処理が完了してから画面を切り替えてください"
        pending = self.controller.pending_import
        if pending is not None and self._pending_token(pending) != self._pending_release:
            return False, "取り込み途中の測定データを確定または破棄してから画面を切り替えてください"
        if self._pending_batch_ids():
            return False, "読み込みキューの項目を保存または破棄してから画面を切り替えてください"
        return True, None

    def dirty_state(self) -> WorkspaceDirtyState:
        """#610: the staged import joins the deactivation contract."""
        if self._user_busy_count():
            return 'busy'
        pending = self.controller.pending_import
        if pending is not None and self._pending_token(pending) != self._pending_release:
            return 'pending_import'
        if self._pending_batch_ids():
            return 'pending_import'
        return 'clean'

    def resolve_dirty_state(
        self, action: DirtyResolutionAction
    ) -> tuple[bool, str | None]:
        pending = self.controller.pending_import
        batch_ids = set(self.controller.uncommitted_batch_item_ids())
        if action == 'keep_draft':
            if pending is None and not batch_ids:
                return False, '取り込み途中のデータがありません'
            # Acknowledge the exact staged import and every current queue
            # row; a re-staged import or a newly staged row re-blocks.
            if pending is not None:
                self._pending_release = self._pending_token(pending)
            self._batch_released_ids |= batch_ids
            return True, '取り込み途中のデータを残しました'
        if action == 'discard_pending':
            self.controller.clear_pending()
            self._pending_release = None
            self.controller.discard_batch_items(batch_ids)
            self._batch_released_ids -= batch_ids
            self.refresh()
            return True, '取り込み途中のデータを破棄しました'
        if action == 'stop_busy':
            # D1/#REV19: the operator explicitly abandoned in-flight work —
            # drain the pool exactly like closeEvent does, but the pool and
            # the workspace stay usable for the next job. Detached workers'
            # completions were disconnected inside stop_all, so late
            # results can never apply.
            report = self._job_pool.stop_all()
            self._job_handlers.clear()
            self._job_purpose.clear()
            self._latest_job_key.clear()
            self._rew_auto_job_keys.clear()
            self._commit_job_key = None
            self._set_batch_committing(False)
            self.refresh()
            if report.all_stopped:
                return True, '実行中の処理を中止しました'
            return True, '実行中の処理を中止しました · 停止が遅延している処理の結果は適用されません'
        return False, 'この状態では実行できない操作です'

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._disposed = True
        self._rew_timer.stop()
        report = self._job_pool.shutdown()
        self._job_handlers.clear()
        self._job_purpose.clear()
        self._rew_auto_job_keys.clear()
        if not report.all_stopped:
            self._set_notice(
                "バックグラウンド処理の停止が遅延しています · 遅延結果は適用しません",
                None,
            )
        super().closeEvent(event)


def build_measurement_workspace_mount(
    controller: MeasurementWorkflowController,
    *,
    on_navigate: Callable[[WorkspaceDeepLink], bool] | None = None,
    help_registry=None,
    open_help: Callable[[str], bool] | None = None,
    preferences: 'ApplicationPreferenceStore | None' = None,
    activity_center=None,
) -> WorkspaceMount:
    """Build the shell-owned mount without a legacy QMainWindow/QDockWidget bridge."""

    workspace = MeasurementPageWorkspace(
        controller,
        on_navigate=on_navigate,
        help_registry=help_registry,
        open_help=open_help,
        preferences=preferences,
        activity_center=activity_center,
    )
    return WorkspaceMount.from_widget(
        workspace,
        on_activate=workspace.mount_activated,
        on_deactivate=workspace.deactivate_rew_auto,
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
    on_navigate: Callable[[WorkspaceDeepLink], bool] | None = None,
    help_registry=None,
    open_help: Callable[[str], bool] | None = None,
    preferences: 'ApplicationPreferenceStore | None' = None,
    activity_center=None,
) -> WorkspaceFactory:
    """Return the lazy factory consumed by build_canonical_workspace_registrations."""

    def build() -> WorkspaceMount:
        controller = MeasurementWorkflowController(
            scene_repository,
            document_id,
            rew_client=rew_client,
        )
        return build_measurement_workspace_mount(
            controller,
            on_navigate=on_navigate,
            help_registry=help_registry,
            open_help=open_help,
            preferences=preferences,
            activity_center=activity_center,
        )

    return build


__all__ = [
    "MeasurementPageWorkspace",
    "build_measurement_workspace_mount",
    "create_measurement_workspace_factory",
]
