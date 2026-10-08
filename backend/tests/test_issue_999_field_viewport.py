"""#999: SpatialFieldResult -> CAD viewport overlay (REV73).

Covers the pure display adapter (axes/flip/clim/decimate/fail-closed),
the staleness fix (currency vs CURRENT head, not the pinned revision),
and the offscreen viewport overlay lifecycle (named actors, no leaks).
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from htdt.cad_field_explorer import (
    build_mode_field_explorer_session,
    field_explorer_session_currency,
)
from htdt.cad_field_explorer_repository import CadFieldExplorerRepository
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
from htdt.field_volume_visual_adapter import (
    FieldDisplayBlocked,
    build_field_display_view,
    build_slice_item,
    field_overlay_currency,
    iso_values_for,
    nearest_slice_index,
)
from htdt.room_field_overlay import (
    FieldOverlay3DRequest,
    RoomFieldOverlayController,
)


def _scene(document_id: str = 'field-3d') -> SceneDocument:
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


def _saved(tmp_path: Path) -> tuple[SceneRepository, object]:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    revision = repository.save(_scene(), parent_revision_id=None).revision
    return repository, revision


def _session(tmp_path: Path, *, stride_m: float = 0.2):
    repository, revision = _saved(tmp_path)
    modes, _ = analyze_native_rectangular_geometry(
        revision, 'point-mlp', max_mode_hz=150.0
    )
    mode = modes.modes[0]
    session = build_mode_field_explorer_session(
        revision=revision,
        modes_result=modes,
        mode_indices=(mode.n_x, mode.n_y, mode.n_z),
        stride_m=stride_m,
    )
    return repository, revision, modes, session


def _canonical_value(session, ix: int, iy: int, iz: int) -> float:
    """Flat canonical sample at (ix, iy, iz) — x fastest."""

    result = session.result
    nx = result.axes[0].count
    ny = result.axes[1].count
    flat = (iz * ny + iy) * nx + ix
    if result.is_complex:
        return float(
            np.hypot(result.pressure_real[flat], result.pressure_imag[flat])
        )
    return float(result.pressure_magnitude_pa[flat])


def _head(repository: SceneRepository, document_id: str):
    return repository.current_head(document_id)


def _mutated(document: SceneDocument) -> SceneDocument:
    """A real content change — identical saves share the same revision id."""

    return document.model_copy(
        update={
            'entities': tuple(
                entity.model_copy(update={'name': 'FL2'})
                if entity.entity_id == 'speaker-fl'
                else entity
                for entity in document.entities
            )
        }
    )


# -- currency ---------------------------------------------------------------


def test_currency_uses_current_head_not_pinned_revision(tmp_path: Path) -> None:
    repository, revision, _modes, session = _session(tmp_path)
    head = _head(repository, session.document_id)

    assert field_overlay_currency(session, head).state == 'CURRENT'

    # Advance the head — the session's pinned revision is now non-current.
    repository.save(_mutated(revision.document), parent_revision_id=revision.revision_id)
    new_head = _head(repository, session.document_id)
    assert new_head.revision_id != session.scene_revision_id

    currency = field_overlay_currency(session, new_head)
    assert currency.state == 'STALE'
    assert 'scene revision superseded' in currency.reasons


def test_currency_unknown_when_head_missing(tmp_path: Path) -> None:
    _repository, _revision, _modes, session = _session(tmp_path)
    assert field_overlay_currency(session, None).state == 'UNKNOWN'


# -- display view -----------------------------------------------------------


def test_display_view_axes_flip_and_values(tmp_path: Path) -> None:
    repository, _revision, _modes, session = _session(tmp_path, stride_m=0.5)
    head = _head(repository, session.document_id)
    view = build_field_display_view(
        session, quantity='pressure_magnitude_pa', head=head
    )

    result = session.result
    nx, ny, nz = (axis.count for axis in result.axes)
    assert view.dims == (nx, ny, nz)
    assert view.sample_state == 'exact'
    # Render origin: x/z unchanged, y starts at the far end (flip).
    assert view.render_origin[0] == pytest.approx(result.axes[0].origin_m)
    assert view.render_origin[2] == pytest.approx(result.axes[2].origin_m)
    assert view.render_origin[1] == pytest.approx(
        -(result.axes[1].origin_m + (ny - 1) * result.axes[1].spacing_m)
    )
    # Positive spacing in all render axes.
    assert all(s > 0 for s in view.render_spacing)
    # Every render-grid sample equals the canonical sample at the Y-flipped
    # index — exact correspondence, no resampling at stride 1.
    for ix in range(nx):
        for iy in range(ny):
            for iz in range(nz):
                assert view.scalars_xyz[ix, iy, iz] == pytest.approx(
                    _canonical_value(session, ix, ny - 1 - iy, iz), rel=1e-6
                )
    assert view.clim[0] < view.clim[1]
    assert 'Pa' in view.scalar_bar_title
    assert view.masked_count == 0


def test_display_view_blocked_on_stale_and_unknown(tmp_path: Path) -> None:
    repository, revision, _modes, session = _session(tmp_path)
    repository.save(_mutated(revision.document), parent_revision_id=revision.revision_id)
    stale_head = _head(repository, session.document_id)

    with pytest.raises(FieldDisplayBlocked):
        build_field_display_view(
            session, quantity='pressure_magnitude_pa', head=stale_head
        )
    with pytest.raises(FieldDisplayBlocked):
        build_field_display_view(
            session, quantity='pressure_magnitude_pa', head=None
        )


def test_display_view_blocked_on_unsupported_quantity(tmp_path: Path) -> None:
    repository, _revision, _modes, session = _session(tmp_path)
    head = _head(repository, session.document_id)
    supported, _reason = session.result.supports_quantity('spl_db')
    if not supported:
        with pytest.raises(FieldDisplayBlocked):
            build_field_display_view(session, quantity='spl_db', head=head)


def test_display_view_phase_gate(tmp_path: Path) -> None:
    repository, _revision, _modes, session = _session(tmp_path)
    head = _head(repository, session.document_id)
    if session.result.is_complex:
        view = build_field_display_view(session, quantity='phase_deg', head=head)
        assert view.cyclic_colormap
        assert np.nanmin(view.scalars_xyz) >= -180.0 - 1e-3
        assert np.nanmax(view.scalars_xyz) <= 180.0 + 1e-3
    else:
        with pytest.raises(FieldDisplayBlocked):
            build_field_display_view(session, quantity='phase_deg', head=head)


def test_display_view_decimation_keeps_full_res_clim(tmp_path: Path) -> None:
    repository, _revision, _modes, session = _session(tmp_path)
    head = _head(repository, session.document_id)
    full = build_field_display_view(
        session, quantity='pressure_magnitude_pa', head=head
    )
    bounded = build_field_display_view(
        session,
        quantity='pressure_magnitude_pa',
        head=head,
        point_budget=64,
    )
    assert bounded.display_stride > 1
    assert bounded.sample_state == 'decimated'
    assert (
        bounded.dims[0] * bounded.dims[1] * bounded.dims[2] <= 64 * 8
    )  # stride rounding slack
    # clim comes from the full-resolution field, not the strided subset.
    assert bounded.clim == pytest.approx(full.clim)
    assert '間引き' in bounded.scalar_bar_title
    # Strided samples still equal canonical samples at the strided indices.
    nx, ny, nz = full.dims
    stride = bounded.display_stride
    for ix in range(bounded.dims[0]):
        for iy in range(bounded.dims[1]):
            assert bounded.scalars_xyz[ix, iy, 0] == pytest.approx(
                full.scalars_xyz[ix * stride, iy * stride, 0], rel=1e-5
            )


def test_display_view_budget_fail_closed(tmp_path: Path) -> None:
    repository, _revision, _modes, session = _session(tmp_path)
    head = _head(repository, session.document_id)
    with pytest.raises(FieldDisplayBlocked):
        build_field_display_view(
            session,
            quantity='pressure_magnitude_pa',
            head=head,
            point_budget=1,
        )


# -- slice items ------------------------------------------------------------


def test_slice_items_match_canonical_samples(tmp_path: Path) -> None:
    repository, _revision, _modes, session = _session(tmp_path, stride_m=0.5)
    head = _head(repository, session.document_id)
    view = build_field_display_view(
        session, quantity='pressure_magnitude_pa', head=head
    )
    result = session.result
    nx, ny, nz = (axis.count for axis in result.axes)

    # XY at z index 1.
    item = build_slice_item(view, 'xy', 1)
    assert item.dims == (nx, ny, 1)
    assert item.render_origin[2] == pytest.approx(
        view.render_origin[2] + view.render_spacing[2]
    )
    assert item.scalars[0, 0, 0] == pytest.approx(
        _canonical_value(session, 0, ny - 1, 1), rel=1e-6
    )

    # XZ at render-y index 0 == canonical y index ny-1.
    item = build_slice_item(view, 'xz', 0)
    assert item.dims == (nx, 1, nz)
    assert item.scalars[0, 0, 0] == pytest.approx(
        _canonical_value(session, 0, ny - 1, 0), rel=1e-6
    )

    # YZ at x index 0.
    item = build_slice_item(view, 'yz', 0)
    assert item.dims == (1, ny, nz)
    assert item.scalars[0, 0, 0] == pytest.approx(
        _canonical_value(session, 0, ny - 1, 0), rel=1e-6
    )

    with pytest.raises(FieldDisplayBlocked):
        build_slice_item(view, 'xy', nz + 5)


def test_nearest_slice_index_snaps_to_grid(tmp_path: Path) -> None:
    repository, _revision, _modes, session = _session(tmp_path, stride_m=0.5)
    head = _head(repository, session.document_id)
    view = build_field_display_view(
        session, quantity='pressure_magnitude_pa', head=head
    )
    z_axis = session.result.axes[2]
    index = nearest_slice_index(view, 'xy', z_axis.coordinate(1) + 1e-4)
    assert index == 1
    index = nearest_slice_index(view, 'xy', z_axis.coordinate(0) - 50.0)
    assert index == 0


def test_iso_values_fraction_and_phase_block(tmp_path: Path) -> None:
    repository, _revision, _modes, session = _session(tmp_path)
    head = _head(repository, session.document_id)
    view = build_field_display_view(
        session, quantity='pressure_magnitude_pa', head=head
    )
    values = iso_values_for(view, 0.5)
    assert len(values) == 1
    assert values[0] == pytest.approx((view.clim[0] + view.clim[1]) / 2)
    if view.cyclic_colormap or session.result.is_complex:
        phase_view = build_field_display_view(
            session, quantity='phase_deg', head=head
        )
        with pytest.raises(FieldDisplayBlocked):
            iso_values_for(phase_view, 0.5)


# -- controller lifecycle ---------------------------------------------------


def _request(session, **overrides) -> FieldOverlay3DRequest:
    base = dict(
        session_id=session.session_id,
        quantity='pressure_magnitude_pa',
        axis_plane='xy',
        coordinate_m=1.0,
    )
    base.update(overrides)
    return FieldOverlay3DRequest(**base)


def test_controller_resolve_scene_and_clear(tmp_path: Path) -> None:
    repository, _revision, _modes, session = _session(tmp_path)
    controller = RoomFieldOverlayController(repository)
    CadFieldExplorerRepository(repository).save(session)

    assert controller.resolve().scene is None  # nothing armed
    controller.set_request(_request(session))
    resolution = controller.resolve()
    assert resolution.scene is not None
    assert resolution.scene.slices[0].axis_plane == 'xy'
    assert any('CURRENT' in line for line in resolution.scene.status_lines)
    assert any('規格化' in line for line in resolution.scene.status_lines)

    controller.clear()
    assert controller.resolve().scene is None


def test_controller_blocks_stale_and_missing_session(tmp_path: Path) -> None:
    repository, revision, _modes, session = _session(tmp_path)
    controller = RoomFieldOverlayController(repository)
    CadFieldExplorerRepository(repository).save(session)
    controller.set_request(_request(session))

    # Advance the head -> the armed overlay must block, not paint stale data.
    repository.save(
        _mutated(revision.document), parent_revision_id=revision.revision_id
    )
    resolution = controller.resolve()
    assert resolution.scene is None
    assert 'STALE' in (resolution.blocked_reason or '')

    controller.set_request(_request(session, session_id='missing-session'))
    resolution = controller.resolve()
    assert resolution.scene is None
    assert '見つかりません' in (resolution.blocked_reason or '')


def test_controller_cross_document_session_blocked(tmp_path: Path) -> None:
    repository, _revision, _modes, session = _session(tmp_path)
    other_repo = SceneRepository(tmp_path / 'other.sqlite3')
    other_repo.save(_scene(document_id='other-doc'), parent_revision_id=None)
    # Arm a controller for a repo where the session never existed.
    controller = RoomFieldOverlayController(other_repo)
    controller.set_request(_request(session))
    resolution = controller.resolve()
    assert resolution.scene is None


def test_controller_probe_world_exact_samples(tmp_path: Path) -> None:
    repository, _revision, _modes, session = _session(tmp_path, stride_m=0.5)
    CadFieldExplorerRepository(repository).save(session)
    controller = RoomFieldOverlayController(repository)
    controller.set_request(_request(session, probe_enabled=True))

    axis = session.result.axes[1]
    y_render = -axis.coordinate(0)
    readout = controller.probe_world(
        (session.result.axes[0].coordinate(0), y_render, session.result.axes[2].coordinate(0))
    )
    assert 'Pa' in readout
    resolution = controller.resolve()
    assert resolution.scene.probe is not None
    assert resolution.probe_readout is not None

    # Off-domain fails closed with an honest message.
    readout = controller.probe_world((1e6, 0.0, 0.0))
    assert '範囲外' in readout

    controller.disarm_probe()
    assert not controller.probe_armed


# -- panel + viewport (offscreen) -------------------------------------------


def test_panel_3d_toggle_follows_head_currency(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QApplication

    from htdt.cad_prediction_repository import CadPredictionRepository
    from htdt.field_explorer_panel import FieldExplorerPanel

    QApplication.instance() or QApplication(['htdt-test'])
    repository, revision, _modes, session = _session(tmp_path)
    CadFieldExplorerRepository(repository).save(session)
    panel = FieldExplorerPanel(
        repository,
        CadPredictionRepository(repository),
        session.document_id,
    )
    panel.refresh_sessions()
    index = panel.session_combo.findData(session.session_id)
    panel.session_combo.setCurrentIndex(index)
    assert panel._session is not None
    assert panel.field3d_toggle.isEnabled()

    # Advance the head -> toggle disables and the row reads 古い.
    repository.save(
        _mutated(revision.document), parent_revision_id=revision.revision_id
    )
    panel.refresh_sessions()
    panel.session_combo.setCurrentIndex(
        panel.session_combo.findData(session.session_id)
    )
    assert not panel.field3d_toggle.isEnabled()


def test_viewport_overlay_actors_and_cleanup(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QApplication

    from htdt.room_viewport import RoomViewport3D

    QApplication.instance() or QApplication(['htdt-test'])
    repository, _revision, _modes, session = _session(tmp_path, stride_m=0.5)
    CadFieldExplorerRepository(repository).save(session)
    controller = RoomFieldOverlayController(repository)
    controller.set_request(_request(session, iso_enabled=True))
    scene = controller.resolve().scene
    assert scene is not None

    viewport = RoomViewport3D()
    with viewport.deferred_render():
        viewport.render_field_overlay(scene)
    names = [
        name
        for name in viewport.plotter.renderer.actors
        if isinstance(name, str) and name.startswith('acoustic-field-')
    ]
    assert any('acoustic-field-slice-xy-' in n for n in names)
    assert 'acoustic-field-status' in names
    assert 'acoustic-field-iso' in names
    # Overlay actors are non-pickable.
    for name in names:
        actor = viewport.plotter.renderer.actors[name]
        assert not actor.GetPickable()
    # Scalar bar registered for honest cleanup.
    assert viewport._field_scalar_bars

    viewport.clear_field_overlay()
    remaining = [
        name
        for name in viewport.plotter.renderer.actors
        if isinstance(name, str) and name.startswith('acoustic-field-')
    ]
    assert remaining == []
    assert not viewport._field_scalar_bars
    assert not viewport._field_image_cache


def test_viewport_overlay_prefix_swept_by_overlay_removal(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QApplication

    from htdt.room_viewport import RoomViewport3D

    QApplication.instance() or QApplication(['htdt-test'])
    repository, _revision, _modes, session = _session(tmp_path, stride_m=0.5)
    CadFieldExplorerRepository(repository).save(session)
    controller = RoomFieldOverlayController(repository)
    controller.set_request(_request(session))
    scene = controller.resolve().scene
    viewport = RoomViewport3D()
    viewport.render_field_overlay(scene)
    assert any(
        name.startswith('acoustic-field-')
        for name in viewport.plotter.renderer.actors
    )
    viewport._remove_overlay_actors()
    assert not any(
        name.startswith('acoustic-field-')
        for name in viewport.plotter.renderer.actors
    )


def test_field_explorer_currency_helper_still_used_for_pinned_checks() -> None:
    # The existing helper compares a session to ONE supplied revision — the
    # overlay passes the current head instead of the pinned revision.
    assert callable(field_explorer_session_currency)
