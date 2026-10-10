"""Native 3D acoustic field explorer dock (#953 / #517).

The UI half of the explorer workflow: it consumes the sealed
``FieldExplorerSession`` authorities built by ``cad_field_explorer`` —
never solver internals — and renders only what the field contract
supports. The produced field is labeled for what it is: an analytical
rectangular-room mode field bound to one saved prediction run, not a
solver-validated or measured field.

#995 bounds the display side of that pipeline: slice rasters are built
on a ``NativeWorkerPool`` thread behind a debounce + request-epoch gate
(the control edits that used to repaint synchronously on the GUI
thread), the QImage raster and painted QPixmap each carry explicit
cell/pixel budgets with an honestly-labelled uniform decimation, and
every invalid / failed / deselected selection clears the previous
heatmap instead of leaving it painted as if current (#994).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .cad_display_labels import mode_class_label
from .cad_display_units import (
    LengthDisplayPolicy,
    display_length_policy,
    format_length_m,
)
from .cad_field_explorer import (
    FieldExplorerSession,
    build_mode_field_explorer_session,
    explorer_plane_coordinates,
    explorer_probe,
    explorer_quantities,
    explorer_slice,
    field_explorer_session_currency,
)
from .cad_field_explorer_repository import CadFieldExplorerRepository
from .cad_prediction_models import CadPredictionResult
from .cad_prediction_repository import CadPredictionRepository
from .cad_repository import SceneRepository
from .cad_scene import Position3
from .cad_spatial_field import FieldSliceView
from .field_volume_visual_adapter import field_overlay_currency
from .native_worker import WORKER_CANCELLED, NativeWorkerPool
from .room_field_overlay import FieldOverlay3DRequest
from .length_spinbox import MetricSpinBox
from .user_facing_error import operation_error_message
from .error_boundary import (
    EXPECTED_OPERATION_ERRORS,
    is_authority_failure,
    report_boundary_failure,
)

_ROLE = Qt.ItemDataRole.UserRole

_QUANTITY_LABELS = {
    'pressure_magnitude_pa': '圧力振幅 Pa',
    'spl_db': 'SPL dB',
    'phase_deg': '位相 deg',
}
_PLANE_LABELS = {'xy': '水平 XY', 'xz': '垂直 XZ', 'yz': '垂直 YZ'}
_SAMPLE_STATE_LABELS = {
    'exact': '厳密グリッドサンプル',
    'nearest_sample': '最近傍サンプル (補間なし)',
    'interpolated': '3次元線形補間値',
}


def _ramp_rgb(t: float) -> tuple[int, int, int]:
    """Sequential blue->cyan->yellow->red ramp shared by slice + scale bar."""

    t = min(1.0, max(0.0, t))
    r = int(255 * min(1.0, max(0.0, 2 * t - 0.5)))
    g = int(255 * min(1.0, max(0.0, 2 * t if t < 0.5 else 2 * (1 - t))))
    b = int(255 * min(1.0, max(0.0, 1.5 - 2 * t)))
    return r, g, b


def _slice_stats(view: FieldSliceView) -> tuple[float, float, int]:
    """(min, max, non-rendered count) over the slice's finite samples."""

    values = np.asarray(view.rows, dtype=np.float64)
    masked = np.zeros(values.shape, dtype=bool)
    if view.masked_positions:
        indices = np.asarray(view.masked_positions, dtype=np.int64)
        masked[indices[:, 0], indices[:, 1]] = True
    masked |= ~np.isfinite(values)
    finite = ~masked
    if not finite.any():
        return 0.0, 1.0, int(masked.sum())
    return (
        float(values[finite].min()),
        float(values[finite].max()),
        int(masked.sum()),
    )


#: Debounce window for slice re-render requests (#995): rapid control edits
#: (plane / coordinate / quantity / session) coalesce into ONE worker
#: dispatch, so a burst of edits can never queue a raster per keystroke.
_SLICE_DEBOUNCE_MS = 120

#: Display-side raster budget — independent of the 3D sample cap
#: ``MAX_FIELD_EXPLORER_SAMPLES`` (a data-authority bound, not a render
#: bound). The painted image is a derived display product: canonical
#: samples are never thinned; the raster is subsampled by a uniform
#: integer stride and the decimation is labelled in the status line.
FIELD_SLICE_MAX_RASTER_CELLS = 1_000_000

#: Device-pixel caps for the painted QPixmap: the fixed ``*8`` upscale it
#: replaces could allocate ~164 MiB for one 800x800 slice; 2048x2048 RGB32
#: stays ~16.8 MiB and the pixel cap holds at every DPI.
FIELD_SLICE_MAX_PIXMAP_EDGE = 2048
FIELD_SLICE_MAX_PIXMAP_PIXELS = 4_194_304  # 2048 * 2048

_SLICE_TASK_KEY = 'field-explorer-slice'
_BUILD_TASK_KEY = 'field-explorer-build'


def _slice_display_stride(n_rows: int, n_cols: int, budget: int) -> int:
    """Smallest uniform integer stride keeping the raster within budget.

    Monotonic predicate + binary search, so even a hostile degenerate
    slice (e.g. 1x10^8 cells from a fabricated view) resolves in ~30
    iterations instead of walking strides one at a time.
    """

    if n_rows <= 0 or n_cols <= 0 or n_rows * n_cols <= budget:
        return 1

    def _fits(stride: int) -> bool:
        return ((n_rows + stride - 1) // stride) * (
            (n_cols + stride - 1) // stride
        ) <= budget

    lo, hi = 1, max(n_rows, n_cols)  # hi collapses to 1x1 — always fits
    while lo < hi:
        mid = (lo + hi) // 2
        if _fits(mid):
            hi = mid
        else:
            lo = mid + 1
    return lo


def _slice_ramp_u8(
    t: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Vectorized twin of ``_ramp_rgb`` — identical piecewise-linear ramp."""

    t = np.clip(t, 0.0, 1.0)
    r = np.clip(2.0 * t - 0.5, 0.0, 1.0)
    g = np.clip(np.where(t < 0.5, 2.0 * t, 2.0 * (1.0 - t)), 0.0, 1.0)
    b = np.clip(1.5 - 2.0 * t, 0.0, 1.0)
    return (
        (r * 255).astype(np.uint8),
        (g * 255).astype(np.uint8),
        (b * 255).astype(np.uint8),
    )


@dataclass(frozen=True, slots=True)
class SliceRaster:
    """Bounded display raster for one slice — orientation already applied.

    ``pixels`` is a ``(height, width, 4)`` uint8 BGRA buffer matching
    ``QImage.Format_RGB32`` byte order. ``lo``/``hi``/``hidden`` are
    measured on the FULL-resolution slice so display decimation can
    never hide an extremum (same contract as the 3D viewport adapter).
    """

    pixels: np.ndarray
    lo: float
    hi: float
    hidden: int
    display_stride: int
    source_rows: int
    source_cols: int
    display_rows: int
    display_cols: int
    horizontal: str
    vertical: str

    @property
    def width(self) -> int:
        return int(self.pixels.shape[1])

    @property
    def height(self) -> int:
        return int(self.pixels.shape[0])


def _slice_raster(
    view: FieldSliceView,
    cell_budget: int = FIELD_SLICE_MAX_RASTER_CELLS,
) -> SliceRaster:
    """Rasterize one exact slice into a bounded display grid (#995).

    Source cells are subsampled by a uniform integer stride — a
    nearest-sample view, never interpolated: coordinate extent, axis
    direction, phase mask and colour scale keep their meaning, and the
    canonical samples are never touched (display product only).

    Orientation convention: x_m runs right; z_m runs up (vertical
    sections read upright, not sideways); for the horizontal plan y_m
    runs top→down so the room front stays at the image top — matching
    the 3D viewport's plan orientation.
    """

    n_rows = len(view.rows)
    n_cols = len(view.column_coordinates_m)
    values = np.asarray(view.rows, dtype=np.float64)
    masked = np.zeros((n_rows, n_cols), dtype=bool)
    if view.masked_positions:
        indices = np.asarray(view.masked_positions, dtype=np.int64)
        masked[indices[:, 0], indices[:, 1]] = True
    masked |= ~np.isfinite(values)
    hidden = int(masked.sum())
    finite = ~masked
    if finite.any():
        lo = float(values[finite].min())
        hi = float(values[finite].max())
    else:
        lo, hi = 0.0, 1.0

    stride = _slice_display_stride(n_rows, n_cols, cell_budget)
    shown = values[::stride, ::stride]
    shown_masked = masked[::stride, ::stride]
    span = hi - lo if hi > lo else 1.0
    t = np.where(
        np.isfinite(shown), np.clip((shown - lo) / span, 0.0, 1.0), 0.0
    )
    r, g, b = _slice_ramp_u8(t)
    cells = np.empty((shown.shape[0], shown.shape[1], 4), dtype=np.uint8)
    cells[..., 0] = b  # Format_RGB32 byte order is B,G,R,A little-endian
    cells[..., 1] = g
    cells[..., 2] = r
    cells[..., 3] = 0xFF
    cells[shown_masked] = (0x40, 0x40, 0x40, 0xFF)

    horizontal = 'x_m' if 'x_m' in (view.row_axis, view.column_axis) else 'y_m'
    vertical = 'z_m' if 'z_m' in (view.row_axis, view.column_axis) else (
        'y_m' if horizontal == 'x_m' else 'x_m'
    )
    image = cells.transpose(1, 0, 2) if view.row_axis == horizontal else cells
    if vertical == 'z_m':
        image = image[::-1]
    return SliceRaster(
        pixels=np.ascontiguousarray(image),
        lo=lo,
        hi=hi,
        hidden=hidden,
        display_stride=stride,
        source_rows=n_rows,
        source_cols=n_cols,
        display_rows=shown.shape[0],
        display_cols=shown.shape[1],
        horizontal=horizontal,
        vertical=vertical,
    )


def _slice_qimage(raster: SliceRaster) -> QImage:
    """QImage over the raster buffer — safe to build off the GUI thread."""

    image = QImage(
        raster.pixels.data,
        raster.width,
        raster.height,
        raster.width * 4,
        QImage.Format.Format_RGB32,
    )
    # Detach from the numpy buffer so the image owns its pixels.
    return image.copy()


def _slice_pixmap(view: FieldSliceView) -> QPixmap:
    """Bounded raster pixmap (no upscale) — kept for r9-era tests."""

    return QPixmap.fromImage(_slice_qimage(_slice_raster(view)))


def _target_pixmap_size(
    width_dip: float, height_dip: float, device_pixel_ratio: float
) -> tuple[int, int]:
    """Paint target in device pixels: widget DIP x DPR, clamped to budget."""

    width = max(
        240,
        int(
            round(
                max(1.0, float(width_dip)) * max(1.0, device_pixel_ratio)
            )
        ),
    )
    height = max(
        240,
        int(
            round(
                max(1.0, float(height_dip)) * max(1.0, device_pixel_ratio)
            )
        ),
    )
    scale = min(
        1.0,
        FIELD_SLICE_MAX_PIXMAP_EDGE / max(width, height),
        (FIELD_SLICE_MAX_PIXMAP_PIXELS / (width * height)) ** 0.5,
    )
    return max(1, int(width * scale)), max(1, int(height * scale))


@dataclass(frozen=True, slots=True)
class _SliceRequest:
    """One queued slice render — its epoch is the request identity (#994)."""

    request_id: int
    session_id: str
    plane: str
    coordinate_m: float
    quantity: str


@dataclass(frozen=True, slots=True)
class _SliceJobResult:
    """Worker → GUI payload: a rendered image + stats, or an honest error."""

    request: _SliceRequest
    image: QImage | None
    raster: SliceRaster | None
    unit: str
    sample_state: str
    error: str | None


def _scale_bar_pixmap(width: int = 240, height: int = 12) -> QPixmap:
    """Horizontal ramp strip matching ``_slice_raster``'s normalization range."""

    image = QImage(width, height, QImage.Format.Format_RGB32)
    for x in range(width):
        r, g, b = _ramp_rgb(x / max(1, width - 1))
        for y in range(height):
            image.setPixel(x, y, (0xFF << 24) | (r << 16) | (g << 8) | b)
    return QPixmap.fromImage(image)


class FieldExplorerPanel(QWidget):
    """2D field explorer + the '音場を3D表示' deep-link into the CAD viewport (#999)."""

    #: Emitted with a FieldOverlay3DRequest whenever the armed overlay state
    #: changes (toggle on, or plane/quantity/coordinate/iso/volume/probe edit).
    field3DRequested = Signal(object)
    #: Emitted when the overlay is toggled off or the session stops being CURRENT.
    field3DCleared = Signal()

    """Explorer dock body: session list, slice controls, probe readout."""

    def __init__(
        self,
        repository: SceneRepository,
        prediction_repository: CadPredictionRepository,
        document_id: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.scene_repository = repository
        self.prediction_repository = prediction_repository
        self.field_repository = CadFieldExplorerRepository(repository)
        self.document_id = document_id
        self._session: FieldExplorerSession | None = None
        self._modes_result: CadPredictionResult | None = None
        self._length_policy = display_length_policy('m')
        self._suppress_3d_signals = False
        self._build_widgets()
        # #995: slice rasters + session builds run on one worker pool so
        # dense grids can never freeze the GUI thread. The epoch is the
        # render-request identity (#994): every control edit bumps it,
        # and only a result stamped with the live epoch may paint.
        self._pool = NativeWorkerPool(self)
        self._slice_timer = QTimer(self)
        self._slice_timer.setSingleShot(True)
        self._slice_timer.setInterval(_SLICE_DEBOUNCE_MS)
        self._slice_timer.timeout.connect(self._start_slice_request)
        self._slice_epoch = 0
        self._build_epoch = 0
        self._pending_slice: _SliceRequest | None = None
        self._quantity_unsupported: dict[int, str] = {}
        self._probe_context: tuple[str, str] | None = None
        self._slice_busy = False
        self._build_busy = False
        self._disposed = False
        #: Device-pixel size of the pixmap currently applied — repaints
        #: only run when the label's real geometry diverges from it.
        self._applied_target: tuple[int, int] | None = None
        self.field_image_label.installEventFilter(self)
        self.refresh_sessions()

    # -- widget scaffolding -------------------------------------------------

    def _build_widgets(self) -> None:
        layout = QVBoxLayout(self)

        identity = QLabel(
            '音場エクスプローラー — 解析的な矩形ルームモード音場を表示します。\n'
            'ソルバー検証済み・実測音場ではありません。'
        )
        identity.setWordWrap(True)
        layout.addWidget(identity)

        session_row = QHBoxLayout()
        self.session_combo = QComboBox()
        self.session_combo.setMinimumContentsLength(12)
        self.session_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.session_combo.setToolTip(
            '表示する予測実行（音場セッション）を選びます。'
        )
        self.session_combo.currentIndexChanged.connect(
            self._session_combo_changed
        )
        session_row.addWidget(self.session_combo, 1)
        self.session_reload_button = QPushButton('再読み込み')
        self.session_reload_button.setToolTip(
            '予測実行の一覧を読み込み直します。'
        )
        self.session_reload_button.clicked.connect(self.refresh_sessions)
        session_row.addWidget(self.session_reload_button)
        layout.addLayout(session_row)

        form = QFormLayout()
        self.mode_combo = QComboBox()
        self.mode_combo.setMinimumContentsLength(18)
        self.mode_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.mode_combo.setToolTip(
            '音場を計算する室モード（部屋の固有振動の次数）を選びます。'
        )
        form.addRow('モード', self.mode_combo)

        self.stride_field = MetricSpinBox(minimum_m=0.02, maximum_m=2.0)
        self.stride_field.set_value_m(0.1)
        self.stride_field.setToolTip(
            '音場を計算する格子点の間隔です。'
            '小さいほど細かく描けますが生成に時間がかかります。'
        )
        form.addRow('グリッド間隔', self.stride_field)

        self.build_button = QPushButton('選択モードの音場を生成')
        self.build_button.setToolTip(
            '選択したモードの音場を格子点で計算して表示します。'
        )
        self.build_button.clicked.connect(self._build_session)
        form.addRow(self.build_button)
        layout.addLayout(form)

        view_form = QFormLayout()
        self.plane_combo = QComboBox()
        self.plane_combo.setMinimumContentsLength(12)
        self.plane_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        for plane in ('xy', 'xz', 'yz'):
            self.plane_combo.addItem(_PLANE_LABELS[plane], plane)
        self.plane_combo.setToolTip(
            '音場を表示する切断面です。XY=水平面（上から見た図）、XZ・YZ=垂直断面です。'
        )
        self.plane_combo.currentIndexChanged.connect(self._plane_changed)
        view_form.addRow('断面', self.plane_combo)

        self.coordinate_combo = QComboBox()
        self.coordinate_combo.setMinimumContentsLength(12)
        self.coordinate_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.coordinate_combo.setToolTip(
            '断面を切る高さ・位置の座標です。'
        )
        self.coordinate_combo.currentIndexChanged.connect(self._refresh_view)
        view_form.addRow('断面位置', self.coordinate_combo)

        self.quantity_combo = QComboBox()
        self.quantity_combo.setMinimumContentsLength(12)
        self.quantity_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.quantity_combo.setToolTip(
            '断面に表示する物理量（音圧レベルなど）です。'
        )
        self.quantity_combo.currentIndexChanged.connect(self._refresh_view)
        view_form.addRow('表示量', self.quantity_combo)
        layout.addLayout(view_form)

        self.field_image_label = QLabel('音場なし')
        self.field_image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.field_image_label.setMinimumHeight(220)
        # Ignored size policy is load-bearing (#995): the label's own
        # geometry must never depend on its contents. If a pixmap/text
        # swap changed the label's sizeHint, layout churn would emit
        # Resize, which re-rendered, which resized — an unbounded
        # paint->clear->repaint loop on the real GUI.
        self.field_image_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored
        )
        layout.addWidget(self.field_image_label)

        # Fixed-height busy row — showing/hiding the affordance must not
        # move the image label either (same feedback loop otherwise).
        self.busy_row = QWidget()
        self.busy_row.setFixedHeight(22)
        busy_row = QHBoxLayout(self.busy_row)
        busy_row.setContentsMargins(0, 0, 0, 0)
        self.field_progress = QProgressBar()
        self.field_progress.setRange(0, 0)
        self.field_progress.setMaximumHeight(14)
        self.field_progress.setToolTip(
            '断面・音場をワーカーで計算中です。'
        )
        busy_row.addWidget(self.field_progress, 1)
        self.field_cancel_button = QPushButton('キャンセル')
        self.field_cancel_button.setToolTip(
            '進行中の計算を取り消します。'
            '表示条件を変更すると再計算します。'
        )
        self.field_cancel_button.clicked.connect(self._cancel_render)
        busy_row.addWidget(self.field_cancel_button)
        layout.addWidget(self.busy_row)
        self.field_progress.hide()
        self.field_cancel_button.hide()

        scale_row = QHBoxLayout()
        self.field_scale_lo = QLabel('')
        self.field_scale_lo.setStyleSheet('font-size: 10px;')
        scale_row.addWidget(self.field_scale_lo)
        self.field_scale_bar = QLabel()
        self.field_scale_bar.setFixedHeight(12)
        scale_row.addWidget(self.field_scale_bar, 1)
        self.field_scale_hi = QLabel('')
        self.field_scale_hi.setStyleSheet('font-size: 10px;')
        scale_row.addWidget(self.field_scale_hi)
        self.field_scale_bar.hide()
        layout.addLayout(scale_row)

        self.field_status_label = QLabel('')
        self.field_status_label.setWordWrap(True)
        layout.addWidget(self.field_status_label)

        field3d_group = QGroupBox('3D CAD表示')
        field3d_layout = QVBoxLayout(field3d_group)
        self.field3d_toggle = QCheckBox('音場を3D CADに重ねる')
        self.field3d_toggle.setToolTip(
            '部屋の3Dビューに断面・等値面としてこの音場を重ねます\n'
            '(シーンが最新の場合のみ表示できます)'
        )
        self.field3d_toggle.toggled.connect(self._field3d_changed)
        self.field3d_toggle.setEnabled(False)
        field3d_layout.addWidget(self.field3d_toggle)
        self.field3d_iso = QCheckBox('等値面を追加')
        self.field3d_iso.toggled.connect(self._field3d_changed)
        field3d_layout.addWidget(self.field3d_iso)
        self.field3d_volume = QCheckBox('半透明ボリュームを追加 (GPU依存)')
        self.field3d_volume.toggled.connect(self._field3d_changed)
        field3d_layout.addWidget(self.field3d_volume)
        self.field3d_probe = QCheckBox('3Dプローブ (ビューをクリック / Esc解除)')
        self.field3d_probe.toggled.connect(self._field3d_changed)
        field3d_layout.addWidget(self.field3d_probe)
        self.field3d_status = QLabel('')
        self.field3d_status.setWordWrap(True)
        field3d_layout.addWidget(self.field3d_status)
        layout.addWidget(field3d_group)

        probe_form = QFormLayout()
        probe_row = QHBoxLayout()
        self.probe_x = MetricSpinBox(minimum_m=-1000.0, maximum_m=1000.0)
        self.probe_y = MetricSpinBox(minimum_m=-1000.0, maximum_m=1000.0)
        self.probe_z = MetricSpinBox(minimum_m=-1000.0, maximum_m=1000.0)
        _axis_tips = (
            'プローブ位置のX座標です（幅方向）。',
            'プローブ位置のY座標です（奥行き方向）。',
            'プローブ位置のZ座標です（高さ方向）。',
        )
        self.probe_x.setAccessibleName('プローブ位置 X座標')
        self.probe_y.setAccessibleName('プローブ位置 Y座標')
        self.probe_z.setAccessibleName('プローブ位置 Z座標')
        for axis_spin, _tip in zip(
            (self.probe_x, self.probe_y, self.probe_z), _axis_tips
        ):
            axis_spin.setToolTip(_tip)
            probe_row.addWidget(axis_spin)
        probe_form.addRow('プローブ位置 (x,y,z)', probe_row)

        self.interpolate_combo = QComboBox()
        self.interpolate_combo.setMinimumContentsLength(12)
        self.interpolate_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.interpolate_combo.addItem('最近傍サンプル (補間なし)', 'exact_samples')
        self.interpolate_combo.addItem('3次元線形補間', 'trilinear')
        self.interpolate_combo.setToolTip(
            '格子点の間の値の読み方です。最近傍=計算した格子点そのまま、'
            '3次元線形補間=周囲の格子点から滑らかに推定します。'
        )
        probe_form.addRow('プローブ補間', self.interpolate_combo)

        self.probe_button = QPushButton('プローブ')
        self.probe_button.setToolTip(
            '指定した位置での音場の値を読み出します。'
        )
        self.probe_button.clicked.connect(self._run_probe)
        probe_form.addRow(self.probe_button)
        layout.addLayout(probe_form)

        self.probe_result_label = QLabel('')
        self.probe_result_label.setWordWrap(True)
        layout.addWidget(self.probe_result_label)
        layout.addStretch(1)

    # -- session lifecycle --------------------------------------------------

    def refresh_sessions(self) -> None:
        current = (
            None if self._session is None else self._session.session_id
        )
        self.session_combo.blockSignals(True)
        self.session_combo.clear()
        self.session_combo.addItem('(セッションを選択)', None)
        # Sessions commonly share one scene revision; resolve each unique
        # revision once per refresh instead of re-validating it per row.
        # Currency is judged against the document's CURRENT head (#999):
        # comparing a session to its own pinned revision can never go STALE.
        head = self.scene_repository.current_head(self.document_id)
        for session in self.field_repository.list_sessions(self.document_id):
            currency = {
                'CURRENT': '最新',
                'STALE': '古い',
                'UNKNOWN': '不明',
            }.get(
                field_overlay_currency(session, head).state,
                '不明',
            )
            label = (
                f'モード ({session.mode_n_x},{session.mode_n_y},'
                f'{session.mode_n_z}) · {session.result.frequency_hz:.1f} Hz '
                f'· 実行 {session.prediction_run_id[:8]} · {currency}'
            )
            self.session_combo.addItem(label, session.session_id)
        if current is not None:
            index = self.session_combo.findData(current)
            if index >= 0:
                self.session_combo.setCurrentIndex(index)
        self.session_combo.blockSignals(False)
        # Currency can move without a session switch (the scene head advanced
        # while the panel was open) — re-validate the loaded session too.
        if self._session is not None:
            fresh = self.field_repository.get(self._session.session_id)
            if fresh is not None:
                self._load_session(fresh)

    def open_for_run(self, run_id: str) -> bool:
        """Prepare the build form for one saved prediction run."""

        results = self.prediction_repository.list_run(run_id)
        modes = next(
            (
                item
                for item in results
                if item.result_kind == 'geometry_modes'
            ),
            None,
        )
        if modes is None or modes.geometry_compatibility != (
            'exact_for_model_geometry'
        ):
            self._modes_result = None
            self.mode_combo.clear()
            self.field_status_label.setText(
                'この実行は厳密矩形モデル結果を持たないため音場を生成できません'
            )
            return False
        self._modes_result = modes
        self.mode_combo.clear()
        for mode in modes.modes:
            self.mode_combo.addItem(
                f'({mode.n_x},{mode.n_y},{mode.n_z}) · '
                f'{mode.frequency_hz:.1f} Hz · {mode_class_label(mode.mode_class)}',
                (mode.n_x, mode.n_y, mode.n_z),
            )
        self.field_status_label.setText(
            f'実行 {run_id[:8]} · {len(modes.modes)} モード候補'
        )
        return True

    def _build_session(self) -> None:
        if self._modes_result is None:
            self.field_status_label.setText('予測実行を選択してください')
            return
        mode = self.mode_combo.currentData()
        if mode is None:
            self.field_status_label.setText('モードを選択してください')
            return
        revision = self.scene_repository.get(
            self._modes_result.scene_revision_id
        )
        if revision is None:
            self.field_status_label.setText('予測元のシーンリビジョンがありません')
            return
        # Dense grids make build + repository save take seconds — run both
        # on the worker pool so the GUI never blocks (#995). Qt widgets
        # are read on this thread first (stride/mode are plain values).
        modes_result = self._modes_result
        mode_indices = tuple(mode)
        stride_m = float(self.stride_field.value_m())
        field_repository = self.field_repository
        self._build_epoch += 1
        epoch = self._build_epoch
        self._set_build_busy(True)
        self.field_status_label.setText('音場を生成中…')

        def work(cancel_event):
            try:
                built = build_mode_field_explorer_session(
                    revision=revision,
                    modes_result=modes_result,
                    mode_indices=mode_indices,
                    stride_m=stride_m,
                )
                if cancel_event.is_set():
                    return (epoch, None, None)
                field_repository.save(built)
                return (epoch, built, None)
            except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: worker envelope — expected failures return their reason verbatim in the result envelope; unexpected errors propagate to diagnostics
                return (epoch, None, operation_error_message(exc))

        try:
            self._pool.start(_BUILD_TASK_KEY, work, self._build_job_completed)
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: op surface — an expected pool-start failure shows a verbatim error; unexpected errors propagate to diagnostics
            self._set_build_busy(False)
            self.field_status_label.setText(
                f'音場を生成できません · {operation_error_message(exc)}'
            )

    def _build_job_completed(self, key, result, error) -> None:
        """GUI-thread slot for the build task (epoch-gated like slices)."""

        if error is not None:
            if error == WORKER_CANCELLED:
                return  # whoever cancelled already owns the visible state
            if self._build_busy:
                self._set_build_busy(False)
                self.field_status_label.setText(
                    '音場を生成できません · '
                    f'{operation_error_message(error)}'
                )
            return
        epoch, session, reason = result
        if epoch != self._build_epoch:
            return  # superseded or cancelled while the worker ran
        self._set_build_busy(False)
        if reason is not None:
            self.field_status_label.setText(
                f'音場を生成できません · {reason}'
            )
            return
        if session is None:
            return  # cancelled inside work() — cancel path owns the UI
        self._session = session
        self.refresh_sessions()
        index = self.session_combo.findData(session.session_id)
        if index >= 0:
            self.session_combo.setCurrentIndex(index)
        self._load_session(session)

    def set_length_policy(self, policy: LengthDisplayPolicy) -> None:
        """Apply the #496 display-unit policy to fields and labels.

        SI metres stay authoritative — only display formatting changes.
        Re-populating the coordinate combo re-renders its labels; the last
        probe result re-renders on the next probe.
        """

        self._length_policy = policy
        for field in (
            self.stride_field,
            self.probe_x,
            self.probe_y,
            self.probe_z,
        ):
            field.set_display_unit(policy.unit, decimals=policy.decimals)
        self._refresh_coordinates()

    def _session_combo_changed(self) -> None:
        session_id = self.session_combo.currentData()
        if session_id is None:
            # Deselection must not keep the previous session painted as if
            # it were the current selection (#994).
            self._unload_session('音場セッションを選択してください')
            return
        session = self.field_repository.get(str(session_id))
        if session is None:
            self._unload_session(
                'セッションを読み込めません · 一覧を再読み込みしてください'
            )
            return
        self._load_session(session)

    def _unload_session(self, reason: str) -> None:
        """Drop the loaded session + every painted artifact (#994)."""

        self._session = None
        self._slice_epoch += 1
        self._pool.cancel(_SLICE_TASK_KEY)
        if self.field3d_toggle.isChecked():
            self.field3d_toggle.setChecked(False)  # emits field3DCleared
        self.field3d_toggle.setEnabled(False)
        for combo in (self.coordinate_combo, self.quantity_combo):
            combo.blockSignals(True)
            combo.clear()
            combo.blockSignals(False)
        self._quantity_unsupported.clear()
        self._clear_field_view(reason)

    def _load_session(self, session: FieldExplorerSession) -> None:
        self._session = session
        head = self.scene_repository.current_head(session.document_id)
        currency = field_overlay_currency(session, head)
        state = (
            '最新'
            if currency.state == 'CURRENT'
            else (
                '古い — 現在のシーンとは一致しません'
                if currency.state == 'STALE'
                else '不明 — 現在のシーンを確認できません'
            )
        )
        if currency.state != 'CURRENT' and self.field3d_toggle.isChecked():
            self.field3d_toggle.setChecked(False)
        self.field3d_toggle.setEnabled(currency.state == 'CURRENT')
        self.field_status_label.setText(
            f'{session.producer} · モード ({session.mode_n_x},'
            f'{session.mode_n_y},{session.mode_n_z}) · '
            f'{session.result.frequency_hz:.1f} Hz · {state}'
        )
        self._refresh_quantity_options()
        self._refresh_coordinates()
        self._refresh_view()

    # -- view refresh -------------------------------------------------------

    def _refresh_quantity_options(self) -> None:
        if self._session is None:
            return
        current = self.quantity_combo.currentData()
        self.quantity_combo.blockSignals(True)
        self.quantity_combo.clear()
        self._quantity_unsupported.clear()
        for quantity, supported, reason in explorer_quantities(self._session):
            label = _QUANTITY_LABELS[quantity]
            if supported:
                self.quantity_combo.addItem(label, quantity)
            else:
                self._quantity_unsupported[
                    self.quantity_combo.count()
                ] = reason or '非対応'
                self.quantity_combo.addItem(
                    f'{label} — 非対応 ({reason})', None
                )
        if current is not None:
            index = self.quantity_combo.findData(current)
            if index >= 0:
                self.quantity_combo.setCurrentIndex(index)
        self.quantity_combo.blockSignals(False)

    def _plane_changed(self) -> None:
        # 断面位置 options are bound to the plane's fixed axis — repopulate
        # them before rendering so a stale coordinate never drives the slice.
        self._refresh_coordinates()
        self._refresh_view()

    def _refresh_coordinates(self) -> None:
        if self._session is None:
            return
        plane = self.plane_combo.currentData() or 'xy'
        self.coordinate_combo.blockSignals(True)
        self.coordinate_combo.clear()
        for coordinate in explorer_plane_coordinates(self._session, plane):
            self.coordinate_combo.addItem(
                format_length_m(coordinate, self._length_policy), coordinate
            )
        self.coordinate_combo.blockSignals(False)

    def _refresh_view(self) -> None:
        """Selection-driven refresh — clear the old paint, then queue."""

        self._queue_slice_render(clear_paint=True)

    def _request_repaint(self) -> None:
        """Display-driven repaint of the SAME selection (resize / DPI move).

        Unlike ``_refresh_view`` this keeps the current paint + status
        until the new raster lands — repaints must never blank the view
        or the layout churn they answer to would loop forever.
        """

        self._queue_slice_render(clear_paint=False)

    def _queue_slice_render(self, clear_paint: bool) -> None:
        """Queue a bounded, worker-side slice render (#995).

        Every call bumps the epoch — the render-request identity (#994) —
        and cancels the in-flight raster, so only the latest selection's
        result can ever paint. Invalid / deselected selections clear the
        old heatmap immediately instead of leaving it behind.
        """

        self._slice_epoch += 1
        self._pending_slice = None
        self._pool.cancel(_SLICE_TASK_KEY)
        session = self._session
        if session is None:
            self._clear_field_view('音場セッションを選択してください')
            return
        plane = self.plane_combo.currentData()
        coordinate = self.coordinate_combo.currentData()
        quantity = self.quantity_combo.currentData()
        if plane is None:
            self._clear_field_view('断面を選択してください')
            return
        if coordinate is None:
            self._clear_field_view('断面位置を選択してください')
            return
        if quantity is None:
            reason = self._quantity_unsupported.get(
                self.quantity_combo.currentIndex()
            )
            self._clear_field_view(
                'この表示量はこの音場では計算できません'
                + (f'（{reason}）' if reason else '')
                + ' · 対応する表示量を選び直してください'
            )
            return
        self._sync_probe_context()
        self._pending_slice = _SliceRequest(
            request_id=self._slice_epoch,
            session_id=session.session_id,
            plane=str(plane),
            coordinate_m=float(coordinate),
            quantity=str(quantity),
        )
        if clear_paint:
            self._clear_paint_only()
            self.field_status_label.setText('断面を計算中…')
        self._set_slice_busy(True)
        self._slice_timer.start()

    def _start_slice_request(self) -> None:
        """Debounce timer fired — dispatch the pending render to the pool."""

        request = self._pending_slice
        self._pending_slice = None
        if request is None or request.request_id != self._slice_epoch:
            self._set_slice_busy(False)
            return
        session = self._session
        if session is None or session.session_id != request.session_id:
            self._set_slice_busy(False)
            return
        self._pool.cancel(_SLICE_TASK_KEY)  # supersede an in-flight raster

        def work(cancel_event) -> _SliceJobResult:
            try:
                view = explorer_slice(
                    session,
                    axis_plane=request.plane,
                    coordinate_m=request.coordinate_m,
                    quantity=request.quantity,
                    phase_mask_min_magnitude_pa=(
                        1.0e-9 if request.quantity == 'phase_deg' else None
                    ),
                )
                if cancel_event.is_set():
                    return _SliceJobResult(request, None, None, '', '', None)
                raster = _slice_raster(view)
                if cancel_event.is_set():
                    return _SliceJobResult(request, None, None, '', '', None)
                # QImage (never QPixmap/QWidget) is safe to build here;
                # conversion + painting happen on the GUI thread.
                image = _slice_qimage(raster)
                return _SliceJobResult(
                    request=request,
                    image=image,
                    raster=raster,
                    unit=view.unit,
                    sample_state=view.sample_state,
                    error=None,
                )
            except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: worker envelope — expected failures return their reason verbatim in the result envelope; unexpected errors propagate to diagnostics
                return _SliceJobResult(
                    request=request,
                    image=None,
                    raster=None,
                    unit='',
                    sample_state='',
                    error=operation_error_message(exc),
                )

        try:
            self._pool.start(_SLICE_TASK_KEY, work, self._slice_job_completed)
        except EXPECTED_OPERATION_ERRORS as exc:  # error-boundary: op surface — an expected pool-start failure shows a verbatim error; unexpected errors propagate to diagnostics
            self._set_slice_busy(False)
            self._clear_field_view(
                f'断面を表示できません · {operation_error_message(exc)}'
            )

    def _slice_job_completed(self, key, result, error) -> None:
        """GUI-thread slot: apply only the live epoch's payload (#994)."""

        if error is not None:
            if error == WORKER_CANCELLED:
                # Whoever cancelled (edit/cancel button/hide) already owns
                # the visible state — nothing to restore here.
                pass
            elif self._slice_busy or self._pending_slice is not None:
                self._clear_field_view(
                    '断面を表示できません · '
                    f'{operation_error_message(error)}'
                )
            self._set_slice_busy(self._pending_slice is not None)
            return
        if not isinstance(result, _SliceJobResult):
            self._set_slice_busy(self._pending_slice is not None)
            return
        request = result.request
        if request.request_id != self._slice_epoch:
            return  # superseded while the worker ran — leave the state
        self._set_slice_busy(False)
        if result.error is not None:
            self._clear_field_view(
                f'断面を表示できません · {result.error}'
            )
            return
        if result.image is None or result.raster is None:
            return  # cancelled inside work() — cancel path owns the UI
        self._apply_slice_result(result)

    def _apply_slice_result(self, result: _SliceJobResult) -> None:
        """Paint the rendered request — only when it still IS the request."""

        request = result.request
        session = self._session
        if session is None or session.session_id != request.session_id:
            return
        # The controls must still describe this render — a repaint is
        # only ever the paint of the CURRENT selection (#994).
        plane = self.plane_combo.currentData()
        quantity = self.quantity_combo.currentData()
        coordinate = self.coordinate_combo.currentData()
        if (
            plane != request.plane
            or quantity != request.quantity
            or coordinate is None
            or float(coordinate) != request.coordinate_m
        ):
            return
        raster = result.raster
        if raster is None or result.image is None:
            return
        target_w, target_h = _target_pixmap_size(
            self.field_image_label.width(),
            self.field_image_label.height(),
            self.devicePixelRatioF(),
        )
        pixmap = QPixmap.fromImage(result.image).scaled(
            target_w,
            target_h,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        self.field_image_label.setPixmap(pixmap)
        self._applied_target = (target_w, target_h)
        self.field_scale_bar.setPixmap(_scale_bar_pixmap())
        self.field_scale_bar.show()
        self.field_scale_lo.setText(f'{raster.lo:.4g}')
        self.field_scale_hi.setText(f'{raster.hi:.4g} {result.unit}')
        masked_note = f' · マスク {raster.hidden}' if raster.hidden else ''
        decimate_note = (
            f' · 表示 {raster.display_cols}x{raster.display_rows}'
            f' (間引き x{raster.display_stride} · サンプル本体は不変)'
            if raster.display_stride > 1
            else ''
        )
        axes = f'{raster.horizontal} → / {raster.vertical} ↑'
        self.field_status_label.setText(
            f'{_QUANTITY_LABELS.get(request.quantity, request.quantity)}'
            f' · {axes} · '
            f'{raster.lo:.4g}…{raster.hi:.4g} {result.unit} · '
            f'{raster.source_cols}x{raster.source_rows} サンプル · '
            f'{result.sample_state}{masked_note}{decimate_note}'
        )
        self._emit_3d_if_armed()

    # -- bounded render plumbing (#995) + stale-paint clearing (#994) -------

    def _clear_paint_only(self) -> None:
        """Drop the painted raster + legend — no status/probe touch."""

        self._applied_target = None
        self.field_image_label.clear()
        self.field_image_label.setText('音場なし')
        self.field_scale_bar.hide()
        self.field_scale_lo.setText('')
        self.field_scale_hi.setText('')

    def _clear_field_view(self, reason: str) -> None:
        """Clear all painted field state and show an honest reason (#994)."""

        self._pending_slice = None
        self._slice_timer.stop()
        self._set_slice_busy(False)
        self._clear_paint_only()
        self.field_status_label.setText(reason)
        self._sync_probe_context()
        # An armed overlay whose request just became impossible is stale
        # in the same sense — uncheck it so the viewport clears too.
        if self.field3d_toggle.isChecked() and (
            self._current_3d_request() is None
        ):
            self.field3d_toggle.setChecked(False)  # emits field3DCleared

    def _set_slice_busy(self, busy: bool) -> None:
        self._slice_busy = busy
        self._update_busy_ui()

    def _set_build_busy(self, busy: bool) -> None:
        self._build_busy = busy
        self._update_busy_ui()

    def _update_busy_ui(self) -> None:
        busy = self._slice_busy or self._build_busy
        self.field_progress.setVisible(busy)
        self.field_cancel_button.setVisible(busy)
        self.build_button.setEnabled(not self._build_busy)
        self.build_button.setText(
            '音場を生成中…' if self._build_busy else '選択モードの音場を生成'
        )

    def _cancel_render(self) -> None:
        """Cancel button: abandon pending + in-flight compute honestly."""

        if self._slice_busy or self._pending_slice is not None:
            self._slice_epoch += 1  # in-flight results now drop as stale
            self._pending_slice = None
            self._slice_timer.stop()
            self._pool.cancel(_SLICE_TASK_KEY)
            self._set_slice_busy(False)
            self._clear_paint_only()
            self.field_status_label.setText(
                '断面計算をキャンセルしました · '
                '表示条件を変更すると再計算します'
            )
        if self._build_busy:
            self._build_epoch += 1
            self._pool.cancel(_BUILD_TASK_KEY)
            self._set_build_busy(False)
            self.field_status_label.setText(
                '音場の生成をキャンセルしました'
            )

    def _sync_probe_context(self) -> None:
        """Probe readout belongs to (session, quantity) — clear it off context."""

        current = (
            None if self._session is None else self._session.session_id,
            self.quantity_combo.currentData(),
        )
        if self._probe_context is not None and self._probe_context != current:
            self.probe_result_label.setText('')
            self._probe_context = None

    def eventFilter(self, watched, event) -> bool:
        # Repaint when the image area's real device-pixel target changed
        # (dialog resize, DPI/screen move). Guarded by _applied_target +
        # a dead-band so layout churn can never feed back into renders —
        # content swaps are sizeHint-ignored, so any Resize seen here is
        # a genuine geometry change, but cheap insurance is cheap.
        if watched is not self.field_image_label or self._session is None:
            return super().eventFilter(watched, event)
        if event.type() in (
            QEvent.Type.Resize,
            QEvent.Type.ScreenChangeInternal,
        ) and self._applied_target is not None:
            target = _target_pixmap_size(
                self.field_image_label.width(),
                self.field_image_label.height(),
                self.devicePixelRatioF(),
            )
            old_w, old_h = self._applied_target
            if (
                abs(target[0] - old_w) > 12
                or abs(target[1] - old_h) > 12
            ):
                self._request_repaint()
        return super().eventFilter(watched, event)

    def hideEvent(self, event) -> None:
        # The explorer lives in a non-modal dialog: hidden work is wasted
        # work. stop_all drains it; the pool stays usable for reopening.
        self.stop_workers()
        super().hideEvent(event)

    def stop_workers(self):
        """Bounded stop that keeps the pool usable (dialog hide path).

        Detaches immediately (zero join): a worker inside an
        uninterruptible sealed computation would otherwise stall the
        dialog's hide on its remaining runtime. The lingerer is detached
        to module ownership and its late completion is dropped by the
        epoch gate.
        """

        self._slice_epoch += 1
        self._build_epoch += 1
        self._pending_slice = None
        self._slice_timer.stop()
        self._set_slice_busy(False)
        self._set_build_busy(False)
        return self._pool.stop_all(timeout_ms=0)

    def dispose(self):
        """Real teardown: physical shutdown of slice + build workers."""

        self._disposed = True
        self._slice_timer.stop()
        return self._pool.shutdown()

    # -- 3D viewport overlay (#999) ------------------------------------------

    def _current_3d_request(self) -> FieldOverlay3DRequest | None:
        session = self._session
        quantity = self.quantity_combo.currentData()
        plane = self.plane_combo.currentData()
        coordinate = self.coordinate_combo.currentData()
        if session is None or quantity is None or plane is None or coordinate is None:
            return None
        head = self.scene_repository.current_head(session.document_id)
        if field_overlay_currency(session, head).state != 'CURRENT':
            return None
        return FieldOverlay3DRequest(
            session_id=session.session_id,
            quantity=quantity,
            axis_plane=plane,
            coordinate_m=float(coordinate),
            iso_enabled=self.field3d_iso.isChecked(),
            volume_enabled=self.field3d_volume.isChecked(),
            probe_enabled=self.field3d_probe.isChecked(),
        )

    def _field3d_changed(self) -> None:
        if self._suppress_3d_signals:
            return
        if not self.field3d_toggle.isChecked():
            self.field3DCleared.emit()
            self.field3d_status.setText('')
            return
        request = self._current_3d_request()
        if request is None:
            self._suppress_3d_signals = True
            self.field3d_toggle.setChecked(False)
            self._suppress_3d_signals = False
            self.field3d_status.setText(
                '最新の音場セッションを読み込んでください'
            )
            return
        self.field3DRequested.emit(request)

    def _emit_3d_if_armed(self) -> None:
        if self.field3d_toggle.isChecked() and not self._suppress_3d_signals:
            request = self._current_3d_request()
            if request is None:
                self.field3d_toggle.setChecked(False)
            else:
                self.field3DRequested.emit(request)

    def set_3d_probe_off(self) -> None:
        """Workspace Esc disarm → uncheck the probe checkbox (re-emits)."""

        if self.field3d_probe.isChecked():
            self.field3d_probe.setChecked(False)

    def _run_probe(self) -> None:
        session = self._session
        if session is None:
            self.probe_result_label.setText('音場セッションがありません')
            return
        quantity = self.quantity_combo.currentData()
        if quantity is None:
            self.probe_result_label.setText('表示量を選択してください')
            return
        position = Position3(
            x_m=float(self.probe_x.value_m()),
            y_m=float(self.probe_y.value_m()),
            z_m=float(self.probe_z.value_m()),
        )
        try:
            probed = explorer_probe(
                session,
                position=position,
                quantity=quantity,
                interpolation=self.interpolate_combo.currentData(),
            )
        except ValueError as exc:
            self.probe_result_label.setText(f'プローブできません · {operation_error_message(exc)}')
            return
        self._probe_context = (session.session_id, str(quantity))
        self.probe_result_label.setText(
            f'{probed.value:.4g} {probed.unit} · '
            f'{_SAMPLE_STATE_LABELS[probed.sample_state]}'
            + (
                ''
                if probed.sample_state == 'exact'
                else (
                    f' · サンプリング位置 ({probed.sampled_position.x_m:.3f}, '
                    f'{probed.sampled_position.y_m:.3f}, '
                    f'{probed.sampled_position.z_m:.3f}) '
                    f'Δ={format_length_m(probed.distance_m, self._length_policy)}'
                )
            )
        )
