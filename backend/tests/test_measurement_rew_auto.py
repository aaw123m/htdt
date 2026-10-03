"""REV40-REWAUTO: workspace-level tests for the REW automation loop."""

from __future__ import annotations

import os
from pathlib import Path
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from htdt.application_preferences import ApplicationPreferenceStore
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.measurement_page_workspace import MeasurementPageWorkspace
from htdt.measurement_workflow import MeasurementWorkflowController
from htdt.rew_api import (
    RewApiUnavailable,
    RewFrequencyResponse,
    RewFrequencyResponseSnapshot,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _snapshot(uuid: str, title: str, measurement_id: str) -> RewFrequencyResponseSnapshot:
    return RewFrequencyResponseSnapshot(
        measurement_summary={'uuid': uuid, 'title': title},
        query={},
        raw_frequency_response={},
        decoded=RewFrequencyResponse(
            measurement_id=measurement_id,
            unit='SPL',
            smoothing=None,
            start_frequency_hz=20.0,
            points_per_octave=None,
            frequency_step_hz=10.0,
            frequency_hz=(20.0, 30.0, 40.0),
            magnitude=(70.0, 71.0, 72.0),
            phase_deg=None,
            requested_unit='SPL',
            requested_ppo=None,
            requested_smoothing=None,
        ),
    )


class _FakeRewClient:
    """RewReadSource-shaped fake driven by the test."""

    def __init__(self, rows=None, snapshots=None, error=None):
        self.rows = list(rows or [])
        self.snapshots = dict(snapshots or {})
        self.error = error
        self.base_url = 'http://127.0.0.1:4735'
        self.fetch_calls: list[str] = []
        self.list_calls = 0

    def list_measurements(self, *, is_cancelled=None):
        self.list_calls += 1
        if self.error is not None:
            raise self.error
        return list(self.rows)

    def get_frequency_response_snapshot(
        self, measurement_uuid, *, unit=None, ppo=None, smoothing=None,
        is_cancelled=None,
    ):
        self.fetch_calls.append(measurement_uuid)
        return self.snapshots[measurement_uuid]


class _FakeActivityCenter:
    def __init__(self):
        self.ops: list[dict] = []

    def submit(self, **kwargs):
        op_id = f'op-{len(self.ops)}'
        self.ops.append({'id': op_id, **kwargs})
        return op_id

    def mark_running(self, operation_id, progress=None):
        for op in self.ops:
            if op['id'] == operation_id:
                op['running'] = True

    def complete(self, operation_id, *, result_summary=None):
        for op in self.ops:
            if op['id'] == operation_id:
                op['summary'] = result_summary


def _workspace(
    tmp_path: Path,
    client=None,
    *,
    preferences: ApplicationPreferenceStore | None = None,
    activity_center=None,
    **kwargs,
):
    scene_repository = SceneRepository(tmp_path / "cad.sqlite3")
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    controller = MeasurementWorkflowController(
        scene_repository, revision.document_id, rew_client=client
    )
    return controller, MeasurementPageWorkspace(
        controller,
        preferences=preferences,
        activity_center=activity_center,
        **kwargs,
    )


def _drain(app: QApplication, workspace: MeasurementPageWorkspace, timeout=10.0) -> None:
    deadline = time.monotonic() + timeout
    while workspace._job_pool.active_count and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()


def _close(app: QApplication, workspace: MeasurementPageWorkspace) -> None:
    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_auto_connect_lists_and_baselines_without_staging(tmp_path: Path) -> None:
    app = _app()
    client = _FakeRewClient(rows=[{'uuid': 'u1', 'title': 'MLP base'}])
    controller, workspace = _workspace(tmp_path, client)
    try:
        workspace.mount_activated()
        _drain(app, workspace)
        assert workspace.rew_combo.count() == 1
        assert workspace.controller.batch_items() == ()
        assert '接続中' in workspace.rew_status_label.text()
        assert workspace.rew_launch_button.isHidden()
    finally:
        _close(app, workspace)


def test_auto_ingest_stages_new_measurement_and_assigns_target(
    tmp_path: Path,
) -> None:
    app = _app()
    center = _FakeActivityCenter()
    client = _FakeRewClient(
        rows=[{'uuid': 'u1', 'title': 'MLP base'}],
        snapshots={'u1': _snapshot('u1', 'MLP base', 'm-1')},
    )
    controller, workspace = _workspace(tmp_path, client, activity_center=center)
    try:
        workspace.mount_activated()
        _drain(app, workspace)
        assert workspace.controller.batch_items() == ()

        client.rows.append({'uuid': 'u2', 'title': 'MLP evening'})
        client.snapshots['u2'] = _snapshot('u2', 'MLP evening', 'm-2')
        workspace._rew_auto_tick()
        _drain(app, workspace)

        items = workspace.controller.batch_items()
        assert len(items) == 1
        assert items[0].status == 'staged'
        assert items[0].assignment is not None
        assert items[0].assignment.measurement_entity_id == 'point-mlp'
        assert '読み込みキュー' in workspace.notice.text()
        assert any(op['operation_kind'] == 'rew_auto_ingest' for op in center.ops)
    finally:
        _close(app, workspace)


def test_auto_ingest_off_still_lists_but_stages_nothing(tmp_path: Path) -> None:
    app = _app()
    prefs = ApplicationPreferenceStore(tmp_path / 'prefs.json')
    prefs.set('integrations.rew_auto_ingest', False)
    client = _FakeRewClient(rows=[{'uuid': 'u1', 'title': 'MLP base'}])
    controller, workspace = _workspace(tmp_path, client, preferences=prefs)
    try:
        workspace.mount_activated()
        _drain(app, workspace)
        client.rows.append({'uuid': 'u2', 'title': 'MLP evening'})
        workspace._rew_auto_tick()
        _drain(app, workspace)
        assert workspace.rew_combo.count() == 2
        assert workspace.controller.batch_items() == ()
        assert client.fetch_calls == []
    finally:
        _close(app, workspace)


def test_launch_button_appears_and_launch_flips_state(tmp_path: Path) -> None:
    app = _app()
    prefs = ApplicationPreferenceStore(tmp_path / 'prefs.json')
    exe = tmp_path / 'roomeqwizard.exe'
    exe.write_bytes(b'x')
    prefs.set('integrations.rew_install_path', str(exe))
    client = _FakeRewClient(error=RewApiUnavailable('down'))
    launches: list[tuple] = []
    controller, workspace = _workspace(
        tmp_path,
        client,
        preferences=prefs,
        rew_launcher=lambda install, port=None: launches.append(
            (install.path, port)
        ),
    )
    try:
        workspace.mount_activated()
        _drain(app, workspace)
        assert workspace._rew_state == 'launchable'
        # Offscreen: ancestors were never shown — assert the hidden flag,
        # not effective visibility (same convention as other workspace tests).
        assert not workspace.rew_launch_button.isHidden()

        workspace.rew_launch_button.click()
        assert workspace._rew_state == 'launching'
        assert launches == [(str(exe), 4735)]
        assert workspace.rew_launch_button.isHidden()
    finally:
        _close(app, workspace)


def test_launch_button_hidden_when_no_install_found(tmp_path: Path) -> None:
    app = _app()
    prefs = ApplicationPreferenceStore(tmp_path / 'prefs.json')
    client = _FakeRewClient(error=RewApiUnavailable('down'))
    controller, workspace = _workspace(tmp_path, client, preferences=prefs)
    try:
        workspace.mount_activated()
        _drain(app, workspace)
        assert workspace._rew_state == 'unavailable'
        assert workspace.rew_launch_button.isHidden()
        assert 'REWが見つかりません' in workspace.rew_status_label.text()
    finally:
        _close(app, workspace)


def test_auto_job_does_not_block_navigation(tmp_path: Path) -> None:
    app = _app()
    gate = threading.Event()

    class _BlockingClient(_FakeRewClient):
        def list_measurements(self, *, is_cancelled=None):
            gate.wait(10)
            return list(self.rows)

    client = _BlockingClient(rows=[])
    controller, workspace = _workspace(tmp_path, client)
    try:
        workspace.mount_activated()
        deadline = time.monotonic() + 5
        while not workspace._rew_auto_job_keys and time.monotonic() < deadline:
            time.sleep(0.01)
        assert workspace._job_pool.active_count == 1
        allowed, _reason = workspace.before_deactivate()
        assert allowed
        gate.set()
        _drain(app, workspace)
    finally:
        gate.set()
        _close(app, workspace)


def test_watch_dir_stages_dropped_file(tmp_path: Path) -> None:
    app = _app()
    watch = tmp_path / 'watch'
    watch.mkdir()
    prefs = ApplicationPreferenceStore(tmp_path / 'prefs.json')
    prefs.set('integrations.rew_watch_dir', str(watch))
    client = _FakeRewClient(rows=[])
    controller, workspace = _workspace(tmp_path, client, preferences=prefs)
    try:
        workspace.mount_activated()
        _drain(app, workspace)
        # Baseline pass: nothing staged.
        assert workspace.controller.batch_items() == ()

        (watch / 'drop.txt').write_text(
            '* Exported with REW\n20 70\n40 71\n', encoding='utf-8'
        )
        workspace._rew_auto_tick()
        _drain(app, workspace)
        items = workspace.controller.batch_items()
        assert len(items) == 1
        assert items[0].filename == 'drop.txt'
    finally:
        _close(app, workspace)


def test_deactivate_stops_timer_and_reactivate_resumes(tmp_path: Path) -> None:
    app = _app()
    client = _FakeRewClient(rows=[{'uuid': 'u1', 'title': 'x'}])
    controller, workspace = _workspace(tmp_path, client)
    try:
        workspace.mount_activated()
        _drain(app, workspace)
        assert workspace._rew_timer.isActive()
        workspace.deactivate_rew_auto()
        assert not workspace._rew_timer.isActive()
        workspace.mount_activated()
        _drain(app, workspace)
        assert workspace._rew_timer.isActive()
    finally:
        _close(app, workspace)


def _two_target_scene(tmp_path: Path):
    from htdt.cad_scene import (
        Position3,
        RoomPrism,
        SceneDocument,
        SceneEntity,
    )

    scene_repository = SceneRepository(tmp_path / "cad.sqlite3")
    document = SceneDocument(
        document_id='two-target-doc',
        room=RoomPrism(width_m=5.0, depth_m=4.0, height_m=2.5),
        entities=(
            SceneEntity(
                entity_id='seat-a-entity',
                kind='measurement_point',
                name='Seat A',
                position=Position3(x_m=1.0, y_m=1.0, z_m=1.0),
            ),
            SceneEntity(
                entity_id='seat-b-entity',
                kind='measurement_point',
                name='Seat B',
                position=Position3(x_m=2.0, y_m=1.0, z_m=1.0),
            ),
        ),
    )
    revision = scene_repository.save(document, parent_revision_id=None).revision
    return MeasurementWorkflowController(
        scene_repository, revision.document_id
    )


def test_auto_assign_batch_items_applies_only_unique_matches(
    tmp_path: Path,
) -> None:
    controller = _two_target_scene(tmp_path)
    controller.stage_rew_text_files(
        [
            (b'* rew\n20 70\n40 71\n', 'measurement seat a up.txt'),
            (b'* rew\n20 72\n40 73\n', 'seat a and seat b sweep.txt'),
            (b'* rew\n20 74\n40 75\n', 'no target here.txt'),
        ]
    )
    applied, unresolved = controller.auto_assign_batch_items()
    assert applied == 1
    assert unresolved == 2
    views = {item.filename: item for item in controller.batch_items()}
    assigned = views['measurement seat a up.txt'].assignment
    assert assigned is not None
    assert assigned.measurement_entity_id == 'seat-a-entity'
    assert views['seat a and seat b sweep.txt'].assignment is None
    assert views['no target here.txt'].assignment is None


def test_auto_assign_batch_items_never_overwrites_user_choice(
    tmp_path: Path,
) -> None:
    from htdt.measurement_workflow import MeasurementAssignment

    controller = _two_target_scene(tmp_path)
    items = controller.stage_rew_text_files(
        [(b'* rew\n20 70\n40 71\n', 'seat a first take.txt')]
    )
    controller.set_batch_item_assignment(
        items[0].item_id,
        MeasurementAssignment(measurement_entity_id='seat-b-entity'),
    )
    applied, _unresolved = controller.auto_assign_batch_items()
    assert applied == 0
    item = controller.batch_items()[0]
    assert item.assignment is not None
    assert item.assignment.measurement_entity_id == 'seat-b-entity'
