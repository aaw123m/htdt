"""Round-22 internal contract-drift regression tests.

Covers three verified seams where a callee's declared error contract had
drifted from what its callers actually catch:

- ``ManagedAssetStore`` raised plain ``ValueError`` for content-address
  violations while the module's own contract says managed-asset failures
  surface as ``ManagedAssetError`` (a ``ValueError`` subclass);
- ``evaluate_measurement_readiness`` crashed on a stored attachment row
  whose ``filename`` is empty — ``_basename('')`` returns ``None`` and
  the generator then called ``.casefold()`` on it;
- geometry edit paths that can legitimately raise ``EditStateError`` (a
  stale ``recovery_candidate`` mid-gesture) were caught only for
  ``ValueError``/``WallTopologyError``, leaking a raw exception through
  the input controller and the panel's operation runner.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication, QFrame

from htdt.cad_document import EditStateError
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    F1_DOCUMENT_ID,
    RoomVertex,
    make_f1_scene,
    room_vertices,
)
from htdt.managed_assets import ManagedAssetError, ManagedAssetStore
from htdt.readiness import evaluate_measurement_readiness
from htdt.room_geometry_input import RoomGeometryInputController
from htdt.room_geometry_panel import RoomGeometryPanel
from htdt.room_workspace import RoomWorkspace


# -- managed assets: typed content-address failures ----------------------------


def test_read_verified_corrupt_bytes_fail_closed_as_managed_asset_error(
    tmp_path: Path,
) -> None:
    store = ManagedAssetStore(tmp_path / 'assets')
    raw = b'calibration payload'
    digest = hashlib.sha256(raw).hexdigest()
    store.ensure_installed(digest, raw)

    store.asset_path(digest).write_bytes(b'corrupted bytes')

    with pytest.raises(ManagedAssetError, match='content address'):
        store.read_verified(digest)
    # ManagedAssetError is a ValueError, so legacy `except ValueError`
    # callers keep working — but catching the typed error now distinguishes
    # asset corruption from unrelated validation failures.
    assert issubclass(ManagedAssetError, ValueError)


def test_ensure_installed_mismatched_existing_bytes_fail_closed(
    tmp_path: Path,
) -> None:
    store = ManagedAssetStore(tmp_path / 'assets')
    raw = b'original payload'
    digest = hashlib.sha256(raw).hexdigest()
    store.ensure_installed(digest, raw)

    store.asset_path(digest).write_bytes(b'other bytes')

    with pytest.raises(ManagedAssetError, match='hash collision'):
        store.ensure_installed(digest, raw)


# -- readiness: empty attachment filename --------------------------------------


def _context_payload() -> dict:
    return {
        'room': {'width_m': 4.0, 'depth_m': 5.0, 'height_m': 2.4},
        'speakers': [],
        'measurement_point': {
            'point_id': 'MLP', 'label': 'MLP',
            'position': {'x_m': 2.0, 'y_m': 3.0, 'z_m': 1.0},
            'aim_xyz': [0.0, 0.0, 1.0],
        },
        'microphone': {
            'manufacturer': 'miniDSP', 'model': 'UMIK-1', 'serial': '7000001',
            'connection': 'usb', 'sample_rate_hz': 48000,
            'calibration_profile': '90deg',
            'calibration_filename': '7000001_90deg.txt',
        },
        'avr': {'manufacturer': 'Yamaha', 'model': 'RX-A4A'},
    }


def _rew_preflight(cal_path: str) -> dict:
    return {
        'read_only': True, 'audio_ready': True, 'driver': 'Java',
        'sample_rate_hz': 48000.0,
        'java': {
            'input_device': 'UMIK-1', 'input_endpoint_ready': True,
            'input_cal_file': cal_path,
            'multichannel_ready': True,
        },
    }


def test_empty_attachment_filename_does_not_crash_readiness() -> None:
    """A stored RawAsset row with ``filename: ''`` is ignored, not fatal."""
    result = evaluate_measurement_readiness(
        _context_payload(),
        _rew_preflight(r'C:\cal\7000001_90deg.txt'),
        [{'kind': 'microphone_calibration', 'context_id': 'ctx',
          'filename': '', 'asset_sha256': 'ab' * 32}],
        context_id='ctx',
    )
    assert result['machine_ready'] is False
    assert 'calibration_raw_asset_attached' in result['failed_check_keys']


# -- geometry editing: EditStateError seam coverage -----------------------------


class _FakePlotter:
    def __init__(self) -> None:
        self.reset_bounds = []

    def remove_actor(self, *_args, **_kwargs) -> None:
        pass

    def render(self) -> None:
        pass

    def add_mesh(self, *_args, **_kwargs):
        return None

    def view_xy(self, **_kwargs) -> None:
        pass

    def enable_parallel_projection(self, *_args, **_kwargs) -> None:
        pass

    def reset_camera(self, render=True, bounds=None) -> None:
        self.reset_bounds.append(bounds)


class _FakeRoomViewport(QFrame):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.interactor = self
        self.plotter = _FakePlotter()
        self.render_calls = []
        self.focused: list[str] = []
        self.focused_ids: list[tuple] = []
        self.fit_count = 0
        self._last_pos = QPointF(10.0, 10.0)
        self.pick_result = (1.0, -2.0, 0.0)

    def render_document(self, document, **kwargs) -> None:
        self.render_calls.append(kwargs)

    def fit_scene(self) -> None:
        self.fit_count += 1

    def focus_entity(self, entity_id: str) -> None:
        self.focused.append(entity_id)

    def focus_entities(self, entity_ids) -> None:
        self.focused_ids.append(tuple(entity_ids))

    def _last_display_position(self) -> QPointF:
        return QPointF(self._last_pos)

    def pick_world_position(self, _position):
        return self.pick_result


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _workspace(tmp_path):
    app = _app()
    repository = SceneRepository(tmp_path / 'scenes.sqlite3')
    repository.save(make_f1_scene(), parent_revision_id=None)
    workspace = RoomWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: _FakeRoomViewport(parent),
    )
    return app, workspace


def _teardown(app, workspace, geometry=None) -> None:
    if geometry is not None:
        geometry.dispose()
    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_close_sketch_surfaces_edit_state_error_as_status(tmp_path) -> None:
    """A stale recovery candidate mid-sketch fails closed via status text."""
    app, workspace = _workspace(tmp_path)
    geometry = RoomGeometryInputController(workspace, workspace.viewport)
    workspace.attach_geometry_input(geometry)
    geometry.start_sketch()
    geometry._sketch = [
        RoomVertex(vertex_id='a', x_m=0.0, y_m=0.0),
        RoomVertex(vertex_id='b', x_m=4.0, y_m=0.0),
        RoomVertex(vertex_id='c', x_m=4.0, y_m=5.0),
    ]
    # Recovery can arrive after the sketch starts (auto-save of an in-flight
    # dirty state) — replace_room then raises EditStateError.
    workspace.controller.recovery_candidate = object()

    assert geometry._close_sketch() is False

    _teardown(app, workspace, geometry)


def test_commit_vertex_drag_surfaces_edit_state_error_as_status(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    geometry = RoomGeometryInputController(workspace, workspace.viewport)
    workspace.attach_geometry_input(geometry)
    geometry.mode = 'edit'
    room = workspace.controller.committed_document.room
    assert room is not None
    vertices = [
        RoomVertex(vertex_id=v.vertex_id, x_m=v.x_m, y_m=v.y_m)
        for v in room_vertices(room)
    ]
    vertices[0] = RoomVertex(
        vertex_id=vertices[0].vertex_id, x_m=vertices[0].x_m + 0.5,
        y_m=vertices[0].y_m,
    )
    geometry._drag_index = 0
    geometry._drag_preview = tuple(vertices)
    workspace.controller.recovery_candidate = object()

    geometry._commit_vertex_drag()  # must not raise

    _teardown(app, workspace, geometry)


def test_geometry_panel_run_converts_edit_state_error(tmp_path) -> None:
    app, workspace = _workspace(tmp_path)
    geometry = RoomGeometryInputController(workspace, workspace.viewport)
    panel = RoomGeometryPanel(geometry)

    def _stale_operation():
        raise EditStateError('stale edit state')

    panel._run(_stale_operation, 'ok')
    assert panel.notice.text()

    panel.deleteLater()
    _teardown(app, workspace, geometry)
