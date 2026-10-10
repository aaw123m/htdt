"""#995/#994: bounded field-explorer slice rendering.

#995 — slice compute runs off the UI thread on ``NativeWorkerPool``,
debounced, epoch-gated, and display-side rasters are decimated under a
declared cell budget with the decimation labelled (canonical samples are
never thinned).

#994 — an invalid/failed/deselected selection must clear the painted
heatmap instead of leaving the last slice behind.
"""

from __future__ import annotations

from pathlib import Path
import time

import numpy as np
import pytest

from htdt.cad_field_explorer import (
    build_mode_field_explorer_session,
    explorer_slice,
)
from htdt.cad_predictions import analyze_native_rectangular_geometry
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Offset3,
    Position3,
    RoomVertex,
    SceneDocument,
    SceneEntity,
    Size3,
    make_polygon_room,
)
from htdt.cad_spatial_field import FieldPlaneRequest, FieldSliceView


def _view(axis_plane: str, rows, *, masked=()) -> FieldSliceView:
    axes = {
        'xy': ('x_m', 'y_m', 'z_m'),
        'xz': ('x_m', 'z_m', 'y_m'),
        'yz': ('y_m', 'z_m', 'x_m'),
    }[axis_plane]
    row_axis, col_axis, _fixed = axes
    return FieldSliceView(
        result_semantic_sha256='a' * 64,
        plane=FieldPlaneRequest(axis_plane=axis_plane, coordinate_m=0.0),
        quantity='spl_db',
        unit='dB SPL',
        rows=tuple(tuple(row) for row in rows),
        row_axis=row_axis,
        column_axis=col_axis,
        row_coordinates_m=tuple(0.5 * i for i in range(len(rows))),
        column_coordinates_m=tuple(0.5 * i for i in range(len(rows[0]))),
        sample_state='exact',
        masked_positions=tuple(masked),
        cache_key_sha256='b' * 64,
    )


# -- pure raster helpers -----------------------------------------------------


def test_slice_display_stride_never_decimates_within_budget() -> None:
    from htdt.field_explorer_panel import (
        FIELD_SLICE_MAX_RASTER_CELLS,
        _slice_display_stride,
    )

    assert _slice_display_stride(100, 100, FIELD_SLICE_MAX_RASTER_CELLS) == 1
    # Exactly at budget still fits.
    assert _slice_display_stride(1000, 1000, 1_000_000) == 1


def test_slice_display_stride_bounds_oversize_grids() -> None:
    from htdt.field_explorer_panel import _slice_display_stride

    budget = 1_000_000
    # Fake-large grids resolve to the smallest stride that fits.
    assert _slice_display_stride(2000, 2000, budget) == 2
    stride = _slice_display_stride(4000, 3000, budget)
    rows = (4000 + stride - 1) // stride
    cols = (3000 + stride - 1) // stride
    assert rows * cols <= budget
    # And it is minimal — one step smaller would overflow.
    prev_rows = (4000 + stride - 2) // (stride - 1)
    prev_cols = (3000 + stride - 2) // (stride - 1)
    assert prev_rows * prev_cols > budget
    # Degenerate shapes collapse instead of hanging.
    assert _slice_display_stride(0, 5, budget) == 1
    assert _slice_display_stride(1, 10**8, budget) == 100


def test_slice_raster_decimates_only_display_side() -> None:
    from htdt.field_explorer_panel import (
        _slice_raster,
        _slice_display_stride,
    )

    rng = np.random.RandomState(0)
    rows = rng.rand(1200, 1000).tolist()
    view = _view('xy', rows)
    raster = _slice_raster(view)
    assert raster.display_stride == _slice_display_stride(1200, 1000, 1_000_000)
    assert raster.display_stride > 1
    assert raster.display_rows * raster.display_cols <= 1_000_000
    # Source extent + stats stay full-resolution — decimation is display-only.
    assert (raster.source_rows, raster.source_cols) == (1200, 1000)
    assert raster.lo == pytest.approx(float(np.asarray(rows).min()), abs=1e-6)
    assert raster.hi == pytest.approx(float(np.asarray(rows).max()), abs=1e-6)
    assert raster.hidden == 0


def test_slice_ramp_u8_matches_scalar_ramp() -> None:
    from htdt.field_explorer_panel import _ramp_rgb, _slice_ramp_u8

    t = np.linspace(0.0, 1.0, 257)
    r, g, b = _slice_ramp_u8(t)
    for i, ti in enumerate(t):
        assert (int(r[i]), int(g[i]), int(b[i])) == _ramp_rgb(float(ti))


def test_slice_raster_masks_and_nonfinite_cells() -> None:
    from htdt.field_explorer_panel import _slice_raster

    rows = [
        [1.0, float('nan'), 3.0],
        [float('inf'), 2.0, 4.0],
    ]
    view = _view('xy', rows, masked=((0, 2),))
    raster = _slice_raster(view)
    # finite values: {1, 2, 3, 4}; hidden: masked(0,2) + nan(0,1) + inf(1,0)
    assert raster.hidden == 3
    assert (raster.lo, raster.hi) == (1.0, 4.0)
    # Masked/non-finite cells paint the neutral alpha cell colour.
    image = raster.pixels  # (height=y, width=x, BGRA)
    # xy: image rows index y, cols index x; (row=x, col=y) source.
    # masked source cell (x=0, y=2) -> image row 2 col 0.
    px = tuple(int(v) for v in image[2, 0])
    assert px == (0x40, 0x40, 0x40, 0xFF)


def test_slice_raster_flat_and_fully_masked_fields() -> None:
    from htdt.field_explorer_panel import _slice_raster

    flat = _view('xy', [[7.5] * 4] * 4)
    raster = _slice_raster(flat)
    assert raster.lo == raster.hi == 7.5
    # Uniform colour (t=0.5 midpoint) — no NaN bleed-through.
    image = raster.pixels
    assert (image[..., 3] == 0xFF).all()
    assert np.unique(image[..., 0]).size == 1
    assert np.unique(image[..., 2]).size == 1

    masked_all = _view(
        'xy', [[1.0, 2.0], [3.0, 4.0]], masked=((0, 0), (0, 1), (1, 0), (1, 1))
    )
    raster = _slice_raster(masked_all)
    assert raster.hidden == 4
    assert (raster.pixels[..., :3] == 0x40).all()


def test_target_pixmap_size_scales_by_dpr_and_stays_bounded() -> None:
    from htdt.field_explorer_panel import (
        FIELD_SLICE_MAX_PIXMAP_EDGE,
        FIELD_SLICE_MAX_PIXMAP_PIXELS,
        _target_pixmap_size,
    )

    for dpr in (1.0, 1.25, 1.5, 2.0):
        w, h = _target_pixmap_size(500, 300, dpr)
        assert w == max(240, round(500 * dpr))
        assert h == max(240, round(300 * dpr))
    # A degenerate label size still gets the 240px floor.
    assert _target_pixmap_size(0, 10, 1.0) == (240, 240)
    # Hostile DPR clamps to the pixel budget, never an unbounded raster.
    w, h = _target_pixmap_size(4000, 4000, 8.0)
    assert w <= FIELD_SLICE_MAX_PIXMAP_EDGE
    assert w * h <= FIELD_SLICE_MAX_PIXMAP_PIXELS


# -- panel integration --------------------------------------------------------


def _scene(document_id: str = 'issue-995') -> SceneDocument:
    room = make_polygon_room(
        (
            RoomVertex(vertex_id='a', x_m=1.0, y_m=2.0),
            RoomVertex(vertex_id='b', x_m=5.0, y_m=2.0),
            RoomVertex(vertex_id='c', x_m=5.0, y_m=5.0),
            RoomVertex(vertex_id='d', x_m=1.0, y_m=5.0),
        ),
        height_m=2.5,
    )
    return SceneDocument(
        document_id=document_id,
        schema_version=2,
        room=room,
        entities=(
            SceneEntity(
                entity_id='speaker-fl',
                kind='speaker',
                name='FL',
                position=Position3(x_m=1.4, y_m=2.6, z_m=1.05),
                size_m=Size3(x_m=0.2, y_m=0.25, z_m=0.4),
                acoustic_reference_offset_m=Offset3(),
                speaker_role='FL',
            ),
            SceneEntity(
                entity_id='point-mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=3.0, y_m=4.0, z_m=1.1),
            ),
        ),
    )


def _panel(tmp_path: Path):
    import os

    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication

    from htdt.cad_prediction_repository import CadPredictionRepository
    from htdt.field_explorer_panel import FieldExplorerPanel

    app = QApplication.instance() or QApplication([])
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = repository.save(_scene(), parent_revision_id=None).revision
    modes, reflections = analyze_native_rectangular_geometry(
        revision, 'point-mlp', max_mode_hz=150.0
    )
    prediction_repository = CadPredictionRepository(repository)
    prediction_repository.save_run((modes, reflections))
    panel = FieldExplorerPanel(
        repository, prediction_repository, revision.document_id
    )
    session = build_mode_field_explorer_session(
        revision=revision,
        modes_result=modes,
        mode_indices=(
            modes.modes[0].n_x,
            modes.modes[0].n_y,
            modes.modes[0].n_z,
        ),
        stride_m=0.2,
    )
    panel.field_repository.save(session)
    panel.refresh_sessions()
    index = panel.session_combo.findData(session.session_id)
    assert index >= 0
    panel.session_combo.setCurrentIndex(index)
    return app, panel, repository, modes, session


def _drain(panel, timeout: float = 30.0) -> None:
    """Pump the event loop until slice/build work has fully settled."""

    app = panel._slice_timer.parent()  # QTimer's parent is the panel
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if (
            not panel._slice_timer.isActive()
            and panel._pending_slice is None
            and len(panel._pool) == 0
            and not panel._slice_busy
            and not panel._build_busy
        ):
            return
        time.sleep(0.005)
    raise AssertionError('field explorer render did not settle in time')


def test_slice_paints_on_worker_then_applies(tmp_path: Path) -> None:
    app, panel, _, _, _ = _panel(tmp_path)
    assert panel._session is not None
    # The paint is asynchronous: nothing appears until the worker result
    # comes back through the completion relay.
    _drain(panel)
    assert not panel.field_image_label.pixmap().isNull()
    assert 'サンプル' in panel.field_status_label.text()
    assert not panel.field_progress.isVisible()


def test_unsupported_quantity_clears_heatmap_994(tmp_path: Path) -> None:
    """#994: an unsupported quantity must not leave the last heatmap painted."""

    app, panel, _, _, _ = _panel(tmp_path)
    _drain(panel)
    assert not panel.field_image_label.pixmap().isNull()
    # SPL is unsupported for the normalized analytical field.
    blocked = None
    for index in range(panel.quantity_combo.count()):
        if panel.quantity_combo.itemData(index) is None:
            blocked = index
            break
    assert blocked is not None
    panel.quantity_combo.setCurrentIndex(blocked)
    _drain(panel)
    assert panel.field_image_label.pixmap().isNull()
    assert '非対応' in panel.field_status_label.text() or (
        '計算できません' in panel.field_status_label.text()
    )
    # Re-selecting a supported quantity repaints a fresh slice.
    for index in range(panel.quantity_combo.count()):
        if panel.quantity_combo.itemData(index) is not None:
            panel.quantity_combo.setCurrentIndex(index)
            break
    _drain(panel)
    assert not panel.field_image_label.pixmap().isNull()


def test_session_deselect_clears_heatmap_994(tmp_path: Path) -> None:
    app, panel, _, _, _ = _panel(tmp_path)
    _drain(panel)
    assert not panel.field_image_label.pixmap().isNull()
    panel.session_combo.setCurrentIndex(0)  # '(セッションを選択)'
    _drain(panel)
    assert panel._session is None
    assert panel.field_image_label.pixmap().isNull()
    assert '選択してください' in panel.field_status_label.text()


def test_slice_worker_failure_clears_heatmap_994(
    tmp_path: Path, monkeypatch
) -> None:
    """#994: a slice compute failure clears the stale paint + shows reason."""

    app, panel, _, _, _ = _panel(tmp_path)
    _drain(panel)
    assert not panel.field_image_label.pixmap().isNull()

    import htdt.field_explorer_panel as panel_module

    real = panel_module.explorer_slice

    def boom(*args, **kwargs):
        raise ValueError('boom-slice')

    monkeypatch.setattr(panel_module, 'explorer_slice', boom)
    panel.plane_combo.setCurrentIndex(1)
    _drain(panel)
    assert panel.field_image_label.pixmap().isNull()
    # The failure reason is shown via the user-facing error mapping —
    # honest state, never the stale paint.
    status = panel.field_status_label.text()
    assert status.startswith('断面を表示できません · ')
    assert len(status) > len('断面を表示できません · ')
    monkeypatch.setattr(panel_module, 'explorer_slice', real)
    panel.plane_combo.setCurrentIndex(0)
    _drain(panel)
    assert not panel.field_image_label.pixmap().isNull()


def test_stale_render_epoch_is_dropped(tmp_path: Path) -> None:
    """A worker result stamped with a superseded request-id never paints."""

    app, panel, _, _, _ = _panel(tmp_path)
    _drain(panel)
    assert not panel.field_image_label.pixmap().isNull()

    from htdt.field_explorer_panel import _SliceJobResult, _SliceRequest

    # Start a new render (clears the paint, queues epoch N), then inject a
    # result stamped with epoch N-1 — the gate must drop it untouched.
    panel._refresh_view()
    assert panel.field_image_label.pixmap().isNull()
    stale_request = _SliceRequest(
        request_id=panel._slice_epoch - 1,
        session_id=panel._session.session_id,
        plane='xy',
        coordinate_m=float(panel.coordinate_combo.currentData()),
        quantity=str(panel.quantity_combo.currentData()),
    )
    view = explorer_slice(
        panel._session,
        axis_plane='xy',
        coordinate_m=stale_request.coordinate_m,
        quantity=stale_request.quantity,
    )
    import htdt.field_explorer_panel as panel_module

    raster = panel_module._slice_raster(view)
    result = _SliceJobResult(
        request=stale_request,
        image=panel_module._slice_qimage(raster),
        raster=raster,
        unit=view.unit,
        sample_state=view.sample_state,
        error=None,
    )
    panel._slice_job_completed('field-explorer-slice', result, None)
    assert panel.field_image_label.pixmap().isNull()  # still cleared
    _drain(panel)  # the real request lands afterwards
    assert not panel.field_image_label.pixmap().isNull()


def test_cancel_abandons_pending_render(tmp_path: Path) -> None:
    app, panel, _, _, _ = _panel(tmp_path)
    _drain(panel)
    assert not panel.field_image_label.pixmap().isNull()
    panel._refresh_view()
    assert panel.field_cancel_button.isVisible() or panel._pending_slice
    panel._cancel_render()
    _drain(panel)
    assert panel.field_image_label.pixmap().isNull()
    assert 'キャンセル' in panel.field_status_label.text()


def test_rapid_selection_changes_paint_only_latest(tmp_path: Path) -> None:
    """100 rapid coordinate edits collapse into one bounded final render."""

    app, panel, _, _, _ = _panel(tmp_path)
    _drain(panel)
    count = panel.coordinate_combo.count()
    assert count > 1
    for i in range(100):
        panel.coordinate_combo.setCurrentIndex(i % count)
    _drain(panel)
    assert len(panel._pool) == 0
    assert not panel.field_image_label.pixmap().isNull()
    # Status reflects the CURRENT selection's slice, not an earlier one.
    assert 'サンプル' in panel.field_status_label.text()


def test_probe_readout_clears_with_context_change(tmp_path: Path) -> None:
    app, panel, _, _, _ = _panel(tmp_path)
    _drain(panel)
    session = panel._session
    panel.probe_x.setValue(session.result.axes[0].coordinate(0))
    panel.probe_y.setValue(session.result.axes[1].coordinate(0))
    panel.probe_z.setValue(session.result.axes[2].coordinate(0))
    panel._run_probe()
    assert panel.probe_result_label.text()
    # Switching the displayed quantity invalidates the probe context.
    blocked = next(
        (
            index
            for index in range(panel.quantity_combo.count())
            if panel.quantity_combo.itemData(index) is None
        ),
        None,
    )
    if blocked is not None:
        panel.quantity_combo.setCurrentIndex(blocked)
    else:
        panel.quantity_combo.setCurrentIndex(
            (panel.quantity_combo.currentIndex() + 1)
            % panel.quantity_combo.count()
        )
    _drain(panel)
    assert panel.probe_result_label.text() == ''


def test_build_session_runs_off_thread(tmp_path: Path) -> None:
    app, panel, repository, modes, _ = _panel(tmp_path)
    # Clear the loaded session, then drive the build path.
    panel.session_combo.setCurrentIndex(0)
    _drain(panel)
    assert panel._session is None
    assert panel.open_for_run(modes.run_id) is True
    panel.mode_combo.setCurrentIndex(0)
    panel._build_session()
    assert panel._session is None  # still building — GUI never blocked
    _drain(panel)
    assert panel._session is not None
    assert not panel.field_image_label.pixmap().isNull()


def test_build_session_failure_is_honest(
    tmp_path: Path, monkeypatch
) -> None:
    app, panel, repository, modes, _ = _panel(tmp_path)
    panel.session_combo.setCurrentIndex(0)
    _drain(panel)
    assert panel.open_for_run(modes.run_id) is True
    panel.mode_combo.setCurrentIndex(0)

    import htdt.field_explorer_panel as panel_module

    def boom(*args, **kwargs):
        raise ValueError('boom-build')

    monkeypatch.setattr(
        panel_module, 'build_mode_field_explorer_session', boom
    )
    panel._build_session()
    _drain(panel)
    assert panel._session is None
    status = panel.field_status_label.text()
    assert status.startswith('音場を生成できません · ')
    assert len(status) > len('音場を生成できません · ')


def test_stop_workers_and_dispose_are_bounded(tmp_path: Path) -> None:
    app, panel, _, _, _ = _panel(tmp_path)
    _drain(panel)
    panel._refresh_view()
    panel.stop_workers()
    assert len(panel._pool) == 0
    # Pool stays usable after a hide-style stop.
    panel._refresh_view()
    _drain(panel)
    assert not panel.field_image_label.pixmap().isNull()
    # dispose() is the hard teardown — later dispatches fail honestly.
    panel.dispose()
    panel._refresh_view()
    _drain(panel)
    assert '表示できません' in panel.field_status_label.text() or (
        panel.field_image_label.pixmap().isNull()
    )
