"""Native 3D acoustic field explorer dock (#953 / #517).

The UI half of the explorer workflow: it consumes the sealed
``FieldExplorerSession`` authorities built by ``cad_field_explorer`` —
never solver internals — and renders only what the field contract
supports. The produced field is labeled for what it is: an analytical
rectangular-room mode field bound to one saved prediction run, not a
solver-validated or measured field.
"""

from __future__ import annotations

from math import isfinite

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
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
from .user_facing_error import operation_error_message

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

    masked = set(view.masked_positions)
    lo: float | None = None
    hi: float | None = None
    hidden = 0
    for row_i, row in enumerate(view.rows):
        for col_i, value in enumerate(row):
            if (row_i, col_i) in masked or not isfinite(value):
                hidden += 1
                continue
            lo = value if lo is None else min(lo, value)
            hi = value if hi is None else max(hi, value)
    return (lo if lo is not None else 0.0, hi if hi is not None else 1.0, hidden)


def _slice_pixmap(view: FieldSliceView) -> QPixmap:
    """Render a slice view to an image — derived display product only.

    Orientation convention: x_m runs right; z_m runs up (vertical sections
    read upright, not sideways); for the horizontal plan y_m runs top→down
    so the room front stays at the image top — matching the 3D viewport's
    plan orientation.
    """

    n_rows = len(view.rows)
    n_cols = len(view.column_coordinates_m)
    horizontal = 'x_m' if 'x_m' in (view.row_axis, view.column_axis) else 'y_m'
    vertical = 'z_m' if 'z_m' in (view.row_axis, view.column_axis) else (
        'y_m' if horizontal == 'x_m' else 'x_m'
    )
    flip_vertical = vertical == 'z_m'
    width = n_rows if view.row_axis == horizontal else n_cols
    height = n_cols if view.row_axis == horizontal else n_rows
    lo, hi, _hidden = _slice_stats(view)
    span = hi - lo if hi > lo else 1.0
    masked = set(view.masked_positions)
    image = QImage(width, height, QImage.Format.Format_RGB32)
    for row_i, row in enumerate(view.rows):
        for col_i, value in enumerate(row):
            px = row_i if view.row_axis == horizontal else col_i
            py = col_i if view.row_axis == horizontal else row_i
            if flip_vertical:
                py = height - 1 - py
            if (row_i, col_i) in masked or not isfinite(value):
                image.setPixel(px, py, 0xFF404040)
                continue
            t = (value - lo) / span
            r, g, b = _ramp_rgb(t)
            image.setPixel(px, py, (0xFF << 24) | (r << 16) | (g << 8) | b)
    pixmap = QPixmap.fromImage(image).scaled(
        max(240, width * 8),
        max(240, height * 8),
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.FastTransformation,
    )
    return pixmap


def _scale_bar_pixmap(width: int = 240, height: int = 12) -> QPixmap:
    """Horizontal ramp strip matching ``_slice_pixmap``'s normalization range."""

    image = QImage(width, height, QImage.Format.Format_RGB32)
    for x in range(width):
        r, g, b = _ramp_rgb(x / max(1, width - 1))
        for y in range(height):
            image.setPixel(x, y, (0xFF << 24) | (r << 16) | (g << 8) | b)
    return QPixmap.fromImage(image)


class FieldExplorerPanel(QWidget):
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
        self._build_widgets()
        self.refresh_sessions()

    # -- widget scaffolding -------------------------------------------------

    def _build_widgets(self) -> None:
        layout = QVBoxLayout(self)

        identity = QLabel(
            '音場エクスプローラ — 解析的な矩形ルームモード音場を表示します。\n'
            'ソルバー検証済み・実測音場ではありません。'
        )
        identity.setWordWrap(True)
        layout.addWidget(identity)

        session_row = QHBoxLayout()
        self.session_combo = QComboBox()
        self.session_combo.setMinimumContentsLength(24)
        self.session_combo.currentIndexChanged.connect(
            self._session_combo_changed
        )
        session_row.addWidget(self.session_combo, 1)
        self.session_reload_button = QPushButton('再読込')
        self.session_reload_button.clicked.connect(self.refresh_sessions)
        session_row.addWidget(self.session_reload_button)
        layout.addLayout(session_row)

        form = QFormLayout()
        self.mode_combo = QComboBox()
        self.mode_combo.setMinimumContentsLength(18)
        form.addRow('モード', self.mode_combo)

        self.stride_field = QDoubleSpinBox()
        self.stride_field.setRange(0.02, 2.0)
        self.stride_field.setSingleStep(0.05)
        self.stride_field.setDecimals(3)
        self.stride_field.setValue(0.1)
        self.stride_field.setSuffix(' m')
        form.addRow('グリッド間隔', self.stride_field)

        self.build_button = QPushButton('選択モードの音場を生成')
        self.build_button.clicked.connect(self._build_session)
        form.addRow(self.build_button)
        layout.addLayout(form)

        view_form = QFormLayout()
        self.plane_combo = QComboBox()
        for plane in ('xy', 'xz', 'yz'):
            self.plane_combo.addItem(_PLANE_LABELS[plane], plane)
        self.plane_combo.currentIndexChanged.connect(self._refresh_view)
        view_form.addRow('断面', self.plane_combo)

        self.coordinate_combo = QComboBox()
        self.coordinate_combo.currentIndexChanged.connect(self._refresh_view)
        view_form.addRow('断面位置', self.coordinate_combo)

        self.quantity_combo = QComboBox()
        self.quantity_combo.currentIndexChanged.connect(self._refresh_view)
        view_form.addRow('表示量', self.quantity_combo)
        layout.addLayout(view_form)

        self.field_image_label = QLabel('音場なし')
        self.field_image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.field_image_label.setMinimumHeight(220)
        layout.addWidget(self.field_image_label)

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

        probe_form = QFormLayout()
        probe_row = QHBoxLayout()
        self.probe_x = QDoubleSpinBox()
        self.probe_y = QDoubleSpinBox()
        self.probe_z = QDoubleSpinBox()
        for axis_spin in (self.probe_x, self.probe_y, self.probe_z):
            axis_spin.setRange(-1000.0, 1000.0)
            axis_spin.setDecimals(3)
            axis_spin.setSingleStep(0.05)
            probe_row.addWidget(axis_spin)
        probe_form.addRow('プローブ位置 (x,y,z)', probe_row)

        self.interpolate_combo = QComboBox()
        self.interpolate_combo.addItem('最近傍サンプル (補間なし)', 'exact_samples')
        self.interpolate_combo.addItem('3次元線形補間', 'trilinear')
        probe_form.addRow('プローブ補間', self.interpolate_combo)

        self.probe_button = QPushButton('プローブ')
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
        revisions: dict[str, object] = {}
        for session in self.field_repository.list_sessions(self.document_id):
            if session.scene_revision_id not in revisions:
                revisions[session.scene_revision_id] = self.scene_repository.get(
                    session.scene_revision_id
                )
            revision = revisions[session.scene_revision_id]
            currency = (
                '古い'
                if revision is None
                else {
                    'CURRENT': '最新',
                    'STALE': '古い',
                }.get(
                    field_explorer_session_currency(
                        session, revision
                    ).state,
                    field_explorer_session_currency(
                        session, revision
                    ).state.lower(),
                )
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
                f'{mode.frequency_hz:.1f} Hz · {mode.mode_class}',
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
            self.field_status_label.setText('予測元のSceneRevisionがありません')
            return
        try:
            session = build_mode_field_explorer_session(
                revision=revision,
                modes_result=self._modes_result,
                mode_indices=tuple(mode),
                stride_m=float(self.stride_field.value()),
            )
        except ValueError as exc:
            self.field_status_label.setText(f'音場を生成できません · {operation_error_message(exc)}')
            return
        self.field_repository.save(session)
        self._session = session
        self.refresh_sessions()
        index = self.session_combo.findData(session.session_id)
        if index >= 0:
            self.session_combo.setCurrentIndex(index)
        self._load_session(session)

    def _session_combo_changed(self) -> None:
        session_id = self.session_combo.currentData()
        if session_id is None:
            return
        session = self.field_repository.get(str(session_id))
        if session is not None:
            self._load_session(session)

    def _load_session(self, session: FieldExplorerSession) -> None:
        self._session = session
        revision = self.scene_repository.get(session.scene_revision_id)
        currency = (
            None
            if revision is None
            else field_explorer_session_currency(session, revision)
        )
        state = (
            '古い — 現在のシーンとは一致しません'
            if currency is None or currency.state != 'CURRENT'
            else '最新'
        )
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
        for quantity, supported, reason in explorer_quantities(self._session):
            label = _QUANTITY_LABELS[quantity]
            if supported:
                self.quantity_combo.addItem(label, quantity)
            else:
                self.quantity_combo.addItem(
                    f'{label} — 非対応 ({reason})', None
                )
        if current is not None:
            index = self.quantity_combo.findData(current)
            if index >= 0:
                self.quantity_combo.setCurrentIndex(index)
        self.quantity_combo.blockSignals(False)

    def _refresh_coordinates(self) -> None:
        if self._session is None:
            return
        plane = self.plane_combo.currentData() or 'xy'
        self.coordinate_combo.blockSignals(True)
        self.coordinate_combo.clear()
        for coordinate in explorer_plane_coordinates(self._session, plane):
            self.coordinate_combo.addItem(f'{coordinate:.3f} m', coordinate)
        self.coordinate_combo.blockSignals(False)

    def _refresh_view(self) -> None:
        session = self._session
        if session is None:
            return
        plane = self.plane_combo.currentData()
        coordinate = self.coordinate_combo.currentData()
        quantity = self.quantity_combo.currentData()
        if plane is None or coordinate is None or quantity is None:
            return
        try:
            view = explorer_slice(
                session,
                axis_plane=plane,
                coordinate_m=float(coordinate),
                quantity=quantity,
                phase_mask_min_magnitude_pa=(
                    1.0e-9 if quantity == 'phase_deg' else None
                ),
            )
        except ValueError as exc:
            self.field_status_label.setText(f'断面を表示できません · {operation_error_message(exc)}')
            return
        self.field_image_label.setPixmap(_slice_pixmap(view))
        lo, hi, hidden = _slice_stats(view)
        horizontal = 'x_m' if 'x_m' in (view.row_axis, view.column_axis) else 'y_m'
        vertical = 'z_m' if 'z_m' in (view.row_axis, view.column_axis) else (
            'y_m' if horizontal == 'x_m' else 'x_m'
        )
        axes = f'{horizontal} → / {vertical} ↑'
        self.field_scale_bar.setPixmap(_scale_bar_pixmap())
        self.field_scale_bar.show()
        self.field_scale_lo.setText(f'{lo:.4g}')
        self.field_scale_hi.setText(f'{hi:.4g} {view.unit}')
        masked_note = f' · masked {hidden}' if hidden else ''
        self.field_status_label.setText(
            f'{_QUANTITY_LABELS.get(quantity, quantity)} · {axes} · '
            f'{lo:.4g}…{hi:.4g} {view.unit} · '
            f'{len(view.column_coordinates_m)}x{len(view.row_coordinates_m)} '
            f'サンプル · {view.sample_state}{masked_note}'
        )

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
            x_m=float(self.probe_x.value()),
            y_m=float(self.probe_y.value()),
            z_m=float(self.probe_z.value()),
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
                    f'Δ={probed.distance_m:.3f} m'
                )
            )
        )
