"""#953: native 3D acoustic field explorer session authority and workflow."""

from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from htdt.cad_field_explorer import (
    build_mode_field_explorer_session,
    explorer_plane_coordinates,
    explorer_probe,
    explorer_quantities,
    explorer_slice,
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


def _scene(document_id: str = 'field-explorer') -> SceneDocument:
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


def _session(tmp_path: Path):
    repository, revision = _saved(tmp_path)
    modes, _ = analyze_native_rectangular_geometry(
        revision, 'point-mlp', max_mode_hz=150.0
    )
    mode = modes.modes[0]
    session = build_mode_field_explorer_session(
        revision=revision,
        modes_result=modes,
        mode_indices=(mode.n_x, mode.n_y, mode.n_z),
        stride_m=0.2,
    )
    return repository, revision, modes, session


def test_session_binds_run_revision_and_field(tmp_path: Path) -> None:
    _, revision, modes, session = _session(tmp_path)

    assert session.session_id.startswith('field-explorer-session:')
    assert session.scene_revision_id == revision.revision_id
    assert session.scene_content_hash == revision.content_hash
    assert session.prediction_run_id == modes.run_id
    assert session.prediction_id == modes.prediction_id
    assert session.request.semantic_sha256 == session.result.request_semantic_sha256
    # Analytical normalized field: SPL must stay gated off.
    supported, reason = session.result.supports_quantity('spl_db')
    assert supported is False
    assert 'absolute pressure reference' in reason
    quantities = dict(
        (quantity, supported) for quantity, supported, _ in explorer_quantities(session)
    )
    assert quantities['pressure_magnitude_pa'] is True
    assert quantities['phase_deg'] is True
    assert quantities['spl_db'] is False


def test_session_rejects_non_exact_run(tmp_path: Path) -> None:
    repository = SceneRepository(tmp_path / 'cad.sqlite3')
    room = make_polygon_room(
        (
            RoomVertex(vertex_id='a', x_m=0.0, y_m=0.0),
            RoomVertex(vertex_id='b', x_m=6.0, y_m=0.0),
            RoomVertex(vertex_id='c', x_m=6.0, y_m=4.0),
            RoomVertex(vertex_id='d', x_m=4.0, y_m=4.0),
            RoomVertex(vertex_id='e', x_m=4.0, y_m=2.0),
            RoomVertex(vertex_id='f', x_m=2.0, y_m=2.0),
            RoomVertex(vertex_id='g', x_m=2.0, y_m=4.0),
            RoomVertex(vertex_id='h', x_m=0.0, y_m=4.0),
        ),
        height_m=2.4,
    )
    scene = SceneDocument(
        document_id='field-explorer-l',
        schema_version=2,
        room=room,
        entities=(
            SceneEntity(
                entity_id='point-mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=3.0, y_m=1.0, z_m=1.1),
            ),
        ),
    )
    revision = repository.save(scene, parent_revision_id=None).revision
    modes, _ = analyze_native_rectangular_geometry(
        revision, 'point-mlp', max_mode_hz=150.0
    )
    assert modes.geometry_compatibility != 'exact_for_model_geometry'
    with pytest.raises(ValueError, match='exact rectangular model'):
        build_mode_field_explorer_session(
            revision=revision,
            modes_result=modes,
            mode_indices=(1, 0, 0),
            stride_m=0.2,
        )


def test_session_rejects_mode_outside_pinned_result(tmp_path: Path) -> None:
    _, revision, modes, _ = _session(tmp_path)
    with pytest.raises(ValueError, match='not in the pinned'):
        build_mode_field_explorer_session(
            revision=revision,
            modes_result=modes,
            mode_indices=(7, 7, 7),
            stride_m=0.2,
        )


def test_session_rejects_stale_run_binding(tmp_path: Path) -> None:
    repository, revision = _saved(tmp_path)
    modes, _ = analyze_native_rectangular_geometry(
        revision, 'point-mlp', max_mode_hz=150.0
    )
    other_revision = repository.save(
        _scene('field-explorer-other'), parent_revision_id=None
    ).revision
    with pytest.raises(ValueError, match='does not pin this SceneRevision'):
        build_mode_field_explorer_session(
            revision=other_revision,
            modes_result=modes,
            mode_indices=(
                modes.modes[0].n_x, modes.modes[0].n_y, modes.modes[0].n_z
            ),
            stride_m=0.2,
        )


def test_slice_is_exact_grid_locked(tmp_path: Path) -> None:
    _, _, _, session = _session(tmp_path)
    z_coords = explorer_plane_coordinates(session, 'xy')
    assert z_coords
    view = explorer_slice(
        session,
        axis_plane='xy',
        coordinate_m=z_coords[0],
        quantity='pressure_magnitude_pa',
    )
    assert view.sample_state == 'exact'
    assert view.row_axis == 'x_m'
    assert view.column_axis == 'y_m'
    with pytest.raises(ValueError):
        explorer_slice(
            session,
            axis_plane='xy',
            coordinate_m=z_coords[0] + 0.031,
            quantity='pressure_magnitude_pa',
        )


def test_probe_reports_exact_nearest_and_interpolated(tmp_path: Path) -> None:
    _, _, _, session = _session(tmp_path)
    result = session.result
    x_axis, y_axis, z_axis = result.axes
    on_grid = Position3(
        x_m=x_axis.coordinate(0),
        y_m=y_axis.coordinate(0),
        z_m=z_axis.coordinate(0),
    )
    probed = explorer_probe(
        session, position=on_grid, quantity='pressure_magnitude_pa'
    )
    assert probed.sample_state == 'exact'

    off_grid = Position3(
        x_m=x_axis.coordinate(0) + 0.03,
        y_m=y_axis.coordinate(0) + 0.03,
        z_m=z_axis.coordinate(0) + 0.03,
    )
    probed = explorer_probe(
        session,
        position=off_grid,
        quantity='pressure_magnitude_pa',
        interpolation='exact_samples',
    )
    assert probed.sample_state == 'nearest_sample'
    assert probed.distance_m > 0.0

    probed = explorer_probe(
        session,
        position=off_grid,
        quantity='pressure_magnitude_pa',
        interpolation='trilinear',
    )
    assert probed.sample_state == 'interpolated'


def test_session_goes_stale_when_scene_moves(tmp_path: Path) -> None:
    repository, revision, _, session = _session(tmp_path)
    currency = field_explorer_session_currency(session, revision)
    assert currency.state == 'CURRENT'

    moved = _scene()
    moved = moved.model_copy(
        update={
            'entities': moved.entities
            + (
                SceneEntity(
                    entity_id='speaker-new',
                    kind='speaker',
                    name='NEW',
                    position=Position3(x_m=2.0, y_m=3.0, z_m=1.0),
                    size_m=Size3(x_m=0.2, y_m=0.25, z_m=0.4),
                    acoustic_reference_offset_m=Offset3(),
                    speaker_role='FR',
                ),
            )
        }
    )
    new_revision = repository.save(
        moved, parent_revision_id=revision.revision_id
    ).revision
    currency = field_explorer_session_currency(session, new_revision)
    assert currency.state == 'STALE'


def test_repository_round_trip_and_replay_validation(tmp_path: Path) -> None:
    repository, _, modes, session = _session(tmp_path)
    store = CadFieldExplorerRepository(repository)

    store.save(session)
    # Idempotent identical re-save.
    store.save(session)

    loaded = store.get(session.session_id)
    assert loaded is not None
    assert loaded == session
    assert store.list_sessions(session.document_id) == (session,)
    assert store.list_for_run(modes.run_id) == (session,)

    # A row whose bytes were rewritten fails closed on read.
    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            'UPDATE cad_field_explorer_sessions SET payload_json = '
            'replace(payload_json, ?, ?) WHERE session_id = ?',
            (
                session.scene_revision_id,
                'tampered-revision',
                session.session_id,
            ),
        )
    with pytest.raises(ValueError):
        store.get(session.session_id)

    # A session pointing at another document fails closed on save.
    with pytest.raises(ValueError):
        store.save(
            session.model_copy(update={'document_id': 'other-document'})
        )


def test_field_explorer_panel_builds_slice_and_probe(tmp_path: Path) -> None:
    import os

    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication

    from htdt.cad_prediction_repository import CadPredictionRepository
    from htdt.field_explorer_panel import FieldExplorerPanel

    app = QApplication.instance() or QApplication([])
    repository, revision = _saved(tmp_path)
    modes, reflections = analyze_native_rectangular_geometry(
        revision, 'point-mlp', max_mode_hz=150.0
    )
    prediction_repository = CadPredictionRepository(repository)
    prediction_repository.save_run((modes, reflections))

    panel = FieldExplorerPanel(
        repository, prediction_repository, revision.document_id
    )
    assert panel.open_for_run(modes.run_id) is True
    panel.mode_combo.setCurrentIndex(0)
    panel._build_session()
    assert panel._session is not None
    # The saved session is listed and reloadable.
    assert panel.session_combo.count() == 2
    # Slice view renders a pixmap.
    panel.coordinate_combo.setCurrentIndex(0)
    panel._refresh_view()
    assert panel.field_image_label.pixmap() is not None
    # SPL stays gated off for the normalized analytical field.
    index = panel.quantity_combo.findText(
        'SPL dB — 非対応 (SPL requires an absolute pressure reference '
        'authority)'
    )
    assert index < 0 or panel.quantity_combo.itemData(index) is None
    # Probe on a grid node reports 'exact'.
    axis = panel._session.result.axes[0]
    panel.probe_x.setValue(axis.coordinate(0))
    panel.probe_y.setValue(panel._session.result.axes[1].coordinate(0))
    panel.probe_z.setValue(panel._session.result.axes[2].coordinate(0))
    panel._run_probe()
    assert 'exact' in panel.probe_result_label.text()
    del app
