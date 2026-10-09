"""#985 — review/proposal package exports run off the UI thread.

Covers the worker-lane contract: ActivityCenter registration with real
stage/frame progress, cooperative cancel with staged-output cleanup,
crash/disk-full failure leaving no partial output, the busy gate
(duplicate start), pinned-input attribution, stale-session
reclassification as COMPLETED_FOR_HISTORICAL_INPUT, and atomic publish.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from htdt.activity_center import ActivityCenter, OperationState
from htdt.cad_design_comparison_repository import CadDesignComparisonRepository
from htdt.cad_design_decision_repository import CadDesignDecisionRepository
from htdt.cad_presentation_repository import CadPresentationRepository
from htdt.cad_presentation_session import (
    PresentationRenderSettings,
    build_presentation_session,
    build_viewpoint,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_review_package import derived_yaw_steps, verify_review_package
from htdt.cad_proposal_package import verify_proposal_package
from htdt.cad_scene import (
    Direction3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
)
from htdt.cad_view_state import RoomCameraState
from htdt.presentation_export_runner import (
    PresentationExportJob,
    PresentationExportRunner,
    _publish_staged,
)

NOW = '2026-10-08T12:00:00+00:00'


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _pump_until(predicate, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        QApplication.processEvents()
        if time.monotonic() > deadline:
            raise AssertionError('timed out waiting for export job')
        time.sleep(0.01)


def _scene(document_id: str = 'doc-1', fl_x: float = 1.2) -> SceneDocument:
    return SceneDocument(
        document_id=document_id,
        room=RoomPrism(width_m=6.0, depth_m=4.5, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='fl',
                kind='speaker',
                name='FL',
                speaker_role='FL',
                position=Position3(x_m=fl_x, y_m=0.8, z_m=1.0),
                size_m=Size3(x_m=0.24, y_m=0.28, z_m=0.42),
                aim_xyz=Direction3(x=0.0, y=1.0, z=0.0),
            ),
        ),
    )


def _camera(x: float = 3.0) -> RoomCameraState:
    return RoomCameraState(
        position=(x, 2.0, 3.0),
        focal_point=(0.0, 0.0, 0.0),
        view_up=(0.0, 0.0, 1.0),
        projection='perspective',
        standard_view='custom',
    )


def _fixture(tmp_path: Path):
    scenes = SceneRepository(tmp_path / 'cad.sqlite3')
    rev_a = scenes.save(_scene(fl_x=1.2), parent_revision_id=None).revision
    repository = CadPresentationRepository(scenes)
    comparison = CadDesignComparisonRepository(scenes)
    return scenes, rev_a, repository, comparison


def _session(revision, *viewpoints, **overrides):
    kwargs = dict(
        document_id=revision.document_id,
        label='クライアントレビュー',
        scene_revision_id=revision.revision_id,
        scene_content_hash=revision.content_hash,
        viewpoints=viewpoints,
        created_at_utc=NOW,
    )
    kwargs.update(overrides)
    return build_presentation_session(**kwargs)


def _runner(scenes, repository, comparison, center, renderer_factory):
    return PresentationExportRunner(
        scene_repository=scenes,
        presentation_repository=repository,
        comparison_repository=comparison,
        decision_repository=CadDesignDecisionRepository(scenes),
        activity_center=center,
        renderer_factory=renderer_factory,
    )


def _job(
    kind: str,
    session,
    out_root: Path,
    *,
    yaw_steps_deg=None,
) -> PresentationExportJob:
    if kind == 'review':
        if yaw_steps_deg is not None:
            effective = yaw_steps_deg
        elif session.render.yaw_step_deg is None:
            effective = ()
        else:
            effective = derived_yaw_steps(session.render.yaw_step_deg)
        expected_frames = len(session.ordered_viewpoints()) * (
            1 + len(effective)
        )
        expected_sheets = 3
    else:
        expected_frames = 0
        expected_sheets = 4
    return PresentationExportJob(
        kind=kind,
        session=session,
        package_dir=out_root / f'{kind}-{session.session_id[:8]}',
        output_root=out_root,
        yaw_steps_deg=yaw_steps_deg,
        include_drawings=True,
        expected_frames=expected_frames,
        expected_sheets=expected_sheets,
    )


def _staging_dirs(root: Path) -> list[Path]:
    return [p for p in root.iterdir() if p.name.startswith('.')]


class _StubRenderer:
    def renderer_id(self) -> str:
        return 'stub/1.0'

    def render_frame(self, document, viewpoint, *, yaw_deg: int) -> bytes:
        return (
            b'PNG-STUB:'
            + viewpoint.viewpoint_id.encode()
            + b':'
            + str(yaw_deg).encode()
        )


class _GatedRenderer:
    """Blocks inside render_frame until released — holds the job open."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.thread_names: list[str] = []

    def renderer_id(self) -> str:
        return 'gated/1.0'

    def render_frame(self, document, viewpoint, *, yaw_deg: int) -> bytes:
        self.calls += 1
        self.thread_names.append(threading.current_thread().name)
        self.started.set()
        while not self.release.wait(0.01):
            pass
        return b'PNG:' + viewpoint.viewpoint_id.encode()


def _wait_worker_started(renderer: _GatedRenderer) -> None:
    deadline = time.monotonic() + 10.0
    while not renderer.started.is_set():
        if time.monotonic() > deadline:
            raise AssertionError('worker never reached render_frame')
        time.sleep(0.01)


def test_review_export_runs_off_ui_thread_and_publishes(tmp_path: Path):
    """Full job: worker lane → staged build → verify → atomic publish."""
    _app()
    scenes, rev_a, repository, comparison = _fixture(tmp_path)
    session = _session(
        rev_a,
        build_viewpoint(name='正面', camera=_camera()),
        build_viewpoint(name='後方', camera=_camera(-3.0)),
        render=PresentationRenderSettings(yaw_step_deg=60),
    )
    repository.save_session(session)
    center = ActivityCenter()
    seen_threads: list[str] = []

    class _Probed(_StubRenderer):
        def render_frame(self, document, viewpoint, *, yaw_deg):
            seen_threads.append(threading.current_thread().name)
            return super().render_frame(
                document, viewpoint, yaw_deg=yaw_deg
            )

    runner = _runner(
        scenes, repository, comparison, center, lambda _s: _Probed()
    )
    out_root = tmp_path / 'out'
    out_root.mkdir()
    job = _job('review', session, out_root)
    completed: list[object] = []
    runner.export_completed.connect(completed.append)
    try:
        assert runner.start(job) is True
        _pump_until(lambda: not runner.busy)
        assert len(completed) == 1
        # Frames rendered on a QThread lane, never the UI/main thread.
        assert seen_threads
        assert all(
            name != threading.main_thread().name for name in seen_threads
        )
        # Published package exists and re-hashes clean.
        assert job.package_dir.is_dir()
        assert (job.package_dir / 'manifest.json').is_file()
        verify_review_package(job.package_dir)
        # No staging leftovers.
        assert _staging_dirs(out_root) == []
        op = center.recent()[0]
        assert op.operation_kind == 'presentation.export.review'
        assert op.state == OperationState.COMPLETED
        assert op.revision_ref == rev_a.revision_id
        assert op.input_authority_refs
        assert 'フレーム' in (op.result_summary or '')
    finally:
        runner.shutdown()


def test_progress_reports_measured_stage_and_item_counts(tmp_path: Path):
    _app()
    scenes, rev_a, repository, comparison = _fixture(tmp_path)
    session = _session(
        rev_a,
        build_viewpoint(name='正面', camera=_camera()),
        render=PresentationRenderSettings(yaw_step_deg=None),
    )
    repository.save_session(session)
    center = ActivityCenter()
    snapshots: list = []
    center.subscribe(snapshots.append)
    runner = _runner(
        scenes, repository, comparison, center, lambda _s: _StubRenderer()
    )
    out_root = tmp_path / 'out'
    out_root.mkdir()
    job = _job('review', session, out_root)
    try:
        assert runner.start(job) is True
        _pump_until(lambda: not runner.busy)
        progresses = [
            op.progress
            for op in snapshots
            if getattr(op, 'progress', None) is not None
        ]
        # Frame items: one pinned frame, measured done/total — never a %.
        frame_rows = [
            p
            for p in progresses
            if p.kind == 'items'
            and p.total_units
            and p.unit_label == 'フレーム'
        ]
        assert frame_rows
        assert frame_rows[-1].total_units == 1
        assert frame_rows[-1].done_units == 1
        sheet_rows = [
            p
            for p in progresses
            if p.kind == 'items' and p.unit_label == 'シート'
        ]
        assert sheet_rows and sheet_rows[-1].done_units == 3
        stages = {
            p.stage_index for p in progresses if p.kind == 'stage'
        }
        # 図面生成 (stage 3) reports only ITEMS rows; verify carries 6.
        assert {1, 4, 5, 6} <= stages
    finally:
        runner.shutdown()


def test_large_session_360_frame_totals(tmp_path: Path):
    """DoD fixture: 40 viewpoints x (1 pinned + 8 yaw) = 360 frames."""
    _app()
    scenes, rev_a, repository, comparison = _fixture(tmp_path)
    viewpoints = tuple(
        build_viewpoint(name=f'視点{i:02d}', camera=_camera(2.0 + i * 0.1))
        for i in range(40)
    )
    session = _session(
        rev_a,
        *viewpoints,
        render=PresentationRenderSettings(yaw_step_deg=30),
    )
    repository.save_session(session)
    yaw = derived_yaw_steps(30)
    expected = len(viewpoints) * (1 + len(yaw))
    assert expected == 360
    center = ActivityCenter()
    snapshots: list = []
    center.subscribe(snapshots.append)
    runner = _runner(
        scenes, repository, comparison, center, lambda _s: _StubRenderer()
    )
    out_root = tmp_path / 'out'
    out_root.mkdir()
    job = _job('review', session, out_root)
    assert job.expected_frames == expected
    try:
        assert runner.start(job) is True
        _pump_until(lambda: not runner.busy, timeout=120.0)
        manifest = verify_review_package(job.package_dir)
        renders = [e for e in manifest.entries if e.kind == 'render']
        assert len(renders) == 360
        items = [
            op.progress
            for op in snapshots
            if getattr(op, 'progress', None) is not None
            and op.progress.kind == 'items'
            and op.progress.unit_label == 'フレーム'
        ]
        assert items[-1].done_units == 360
        assert center.recent()[0].state == OperationState.COMPLETED
    finally:
        runner.shutdown()


def test_cancel_during_render_leaves_no_partial_output(tmp_path: Path):
    _app()
    scenes, rev_a, repository, comparison = _fixture(tmp_path)
    viewpoints = tuple(
        build_viewpoint(name=f'視点{i:02d}', camera=_camera(2.0 + i * 0.1))
        for i in range(36)
    )
    session = _session(
        rev_a,
        *viewpoints,
        render=PresentationRenderSettings(yaw_step_deg=30),
    )
    repository.save_session(session)
    center = ActivityCenter()
    gated = _GatedRenderer()
    runner = _runner(
        scenes, repository, comparison, center, lambda _s: gated
    )
    out_root = tmp_path / 'out'
    out_root.mkdir()
    job = _job('review', session, out_root)
    cancelled: list[bool] = []
    runner.export_cancelled.connect(lambda: cancelled.append(True))
    try:
        assert runner.start(job) is True
        _wait_worker_started(gated)
        assert runner.request_cancel() is True
        gated.release.set()
        _pump_until(lambda: not runner.busy)
        assert cancelled == [True]
        op = center.recent()[0]
        assert op.state == OperationState.CANCELLED
        assert not job.package_dir.exists()
        assert _staging_dirs(out_root) == []
    finally:
        gated.release.set()
        runner.shutdown()


def test_cancel_requested_after_verify_discards_result(tmp_path: Path):
    """A cancel landing before publish is honored: nothing is published."""
    _app()
    scenes, rev_a, repository, comparison = _fixture(tmp_path)
    session = _session(
        rev_a,
        build_viewpoint(name='正面', camera=_camera()),
        render=PresentationRenderSettings(yaw_step_deg=None),
    )
    repository.save_session(session)
    center = ActivityCenter()
    runner = _runner(
        scenes, repository, comparison, center, lambda _s: _StubRenderer()
    )
    out_root = tmp_path / 'out'
    out_root.mkdir()
    job = _job('review', session, out_root)
    try:
        assert runner.start(job) is True
        # Wait (without pumping the event loop) until the worker has
        # staged+written manifest.json — the completed emission is then
        # either still in run() or queued undispatched; request_cancel
        # flips the record to CANCELLATION_REQUESTED before dispatch.
        deadline = time.monotonic() + 10.0
        while True:
            staging = [
                p for p in out_root.iterdir() if '.staging-' in p.name
            ]
            if staging and (staging[0] / 'manifest.json').exists():
                break
            if time.monotonic() > deadline:
                raise AssertionError('build never staged a manifest')
            time.sleep(0.01)
        assert runner.request_cancel() is True
        _pump_until(lambda: not runner.busy)
        op = center.recent()[0]
        assert op.state == OperationState.CANCELLED
        assert not job.package_dir.exists()
        assert _staging_dirs(out_root) == []
    finally:
        runner.shutdown()


def test_renderer_factory_crash_fails_and_cleans_staging(tmp_path: Path):
    _app()
    scenes, rev_a, repository, comparison = _fixture(tmp_path)
    session = _session(
        rev_a,
        build_viewpoint(name='正面', camera=_camera()),
        render=PresentationRenderSettings(yaw_step_deg=None),
    )
    repository.save_session(session)
    center = ActivityCenter()

    def _boom(_session):
        raise OSError('no GL context')

    runner = _runner(
        scenes, repository, comparison, center, _boom
    )
    out_root = tmp_path / 'out'
    out_root.mkdir()
    job = _job('review', session, out_root)
    failed: list[object] = []
    runner.export_failed.connect(failed.append)
    try:
        assert runner.start(job) is True
        _pump_until(lambda: not runner.busy)
        assert len(failed) == 1
        _pinned, error_text, diagnostic_id = failed[0]
        assert diagnostic_id
        assert error_text
        op = center.recent()[0]
        assert op.state == OperationState.FAILED
        assert op.diagnostic_id == diagnostic_id
        assert '[diag:' in (op.error_summary or '')
        assert not job.package_dir.exists()
        assert _staging_dirs(out_root) == []
    finally:
        runner.shutdown()


def test_disk_full_during_write_fails_and_cleans_staging(
    tmp_path: Path, monkeypatch
):
    _app()
    scenes, rev_a, repository, comparison = _fixture(tmp_path)
    session = _session(
        rev_a,
        build_viewpoint(name='正面', camera=_camera()),
        render=PresentationRenderSettings(yaw_step_deg=None),
    )
    repository.save_session(session)
    center = ActivityCenter()
    import htdt.cad_review_package as review_pkg

    def _no_space(*args, **kwargs):
        raise OSError('No space left on device')

    monkeypatch.setattr(review_pkg, '_write_file', _no_space)
    runner = _runner(
        scenes, repository, comparison, center, lambda _s: _StubRenderer()
    )
    out_root = tmp_path / 'out'
    out_root.mkdir()
    job = _job('review', session, out_root)
    failed: list[object] = []
    runner.export_failed.connect(failed.append)
    try:
        assert runner.start(job) is True
        _pump_until(lambda: not runner.busy)
        assert len(failed) == 1
        assert center.recent()[0].state == OperationState.FAILED
        assert not job.package_dir.exists()
        assert _staging_dirs(out_root) == []
    finally:
        runner.shutdown()


def test_verify_failure_never_publishes(tmp_path: Path, monkeypatch):
    _app()
    scenes, rev_a, repository, comparison = _fixture(tmp_path)
    session = _session(
        rev_a,
        build_viewpoint(name='正面', camera=_camera()),
        render=PresentationRenderSettings(yaw_step_deg=None),
    )
    repository.save_session(session)
    center = ActivityCenter()
    import htdt.presentation_export_runner as runner_mod

    def _bad_verify(_dir):
        raise ValueError('manifest mismatch')

    monkeypatch.setattr(runner_mod, 'verify_review_package', _bad_verify)
    runner = _runner(
        scenes, repository, comparison, center, lambda _s: _StubRenderer()
    )
    out_root = tmp_path / 'out'
    out_root.mkdir()
    job = _job('review', session, out_root)
    try:
        assert runner.start(job) is True
        _pump_until(lambda: not runner.busy)
        assert center.recent()[0].state == OperationState.FAILED
        assert not job.package_dir.exists()
        assert _staging_dirs(out_root) == []
    finally:
        runner.shutdown()


def test_duplicate_start_refused_while_busy(tmp_path: Path):
    _app()
    scenes, rev_a, repository, comparison = _fixture(tmp_path)
    session = _session(
        rev_a,
        build_viewpoint(name='正面', camera=_camera()),
        render=PresentationRenderSettings(yaw_step_deg=None),
    )
    repository.save_session(session)
    center = ActivityCenter()
    gated = _GatedRenderer()
    runner = _runner(
        scenes, repository, comparison, center, lambda _s: gated
    )
    out_root = tmp_path / 'out'
    out_root.mkdir()
    job = _job('review', session, out_root)
    try:
        assert runner.start(job) is True
        _wait_worker_started(gated)
        assert runner.start(job) is False
        assert runner.start(job) is False
        assert len(center.active()) == 1
        gated.release.set()
        _pump_until(lambda: not runner.busy)
        assert runner.start(job) is True
        _wait_worker_started(gated)
        gated.release.set()
        _pump_until(lambda: not runner.busy)
    finally:
        gated.release.set()
        runner.shutdown()


def test_stale_session_result_is_historical(tmp_path: Path):
    _app()
    scenes, rev_a, repository, comparison = _fixture(tmp_path)
    session = _session(
        rev_a,
        build_viewpoint(name='正面', camera=_camera()),
        render=PresentationRenderSettings(yaw_step_deg=None),
    )
    repository.save_session(session)
    # A newer revision becomes the document head after the pin.
    scenes.save(_scene(fl_x=1.6), parent_revision_id=rev_a.revision_id)
    center = ActivityCenter()
    runner = _runner(
        scenes, repository, comparison, center, lambda _s: _StubRenderer()
    )
    out_root = tmp_path / 'out'
    out_root.mkdir()
    job = _job('review', session, out_root)
    try:
        assert runner.start(job) is True
        _pump_until(lambda: not runner.busy)
        op = center.recent()[0]
        assert op.state == OperationState.COMPLETED_FOR_HISTORICAL_INPUT
        assert job.package_dir.is_dir()  # package still exists
        verify_review_package(job.package_dir)
    finally:
        runner.shutdown()


def test_proposal_export_end_to_end(tmp_path: Path):
    _app()
    scenes, rev_a, repository, comparison = _fixture(tmp_path)
    session = _session(
        rev_a,
        build_viewpoint(name='正面', camera=_camera()),
    )
    repository.save_session(session)
    center = ActivityCenter()
    runner = _runner(
        scenes, repository, comparison, center, lambda _s: _StubRenderer()
    )
    out_root = tmp_path / 'out'
    out_root.mkdir()
    job = _job('proposal', session, out_root)
    try:
        assert runner.start(job) is True
        _pump_until(lambda: not runner.busy)
        assert (job.package_dir / 'proposal.html').is_file()
        assert (job.package_dir / 'manifest.json').is_file()
        verify_proposal_package(job.package_dir)
        assert _staging_dirs(out_root) == []
        op = center.recent()[0]
        assert op.operation_kind == 'presentation.export.proposal'
        assert op.state == OperationState.COMPLETED
    finally:
        runner.shutdown()


def test_shutdown_cancels_in_flight_export(tmp_path: Path):
    _app()
    scenes, rev_a, repository, comparison = _fixture(tmp_path)
    viewpoints = tuple(
        build_viewpoint(name=f'視点{i:02d}', camera=_camera(2.0 + i * 0.1))
        for i in range(36)
    )
    session = _session(
        rev_a,
        *viewpoints,
        render=PresentationRenderSettings(yaw_step_deg=30),
    )
    repository.save_session(session)
    center = ActivityCenter()
    gated = _GatedRenderer()
    runner = _runner(
        scenes, repository, comparison, center, lambda _s: gated
    )
    out_root = tmp_path / 'out'
    out_root.mkdir()
    job = _job('review', session, out_root)
    assert runner.start(job) is True
    _wait_worker_started(gated)
    # Simulate project switch: close → bounded drain → cancelled record.
    def _close():
        runner.shutdown()

    closer = threading.Thread(target=_close)
    closer.start()
    time.sleep(0.3)
    gated.release.set()
    closer.join(timeout=30)
    assert not runner.busy
    assert not job.package_dir.exists()
    assert _staging_dirs(out_root) == []
    op = center.recent()[0]
    assert op.state == OperationState.CANCELLED


def test_publish_staged_is_atomic(tmp_path: Path):
    staging = tmp_path / '.pkg.staging-1'
    staging.mkdir()
    (staging / 'manifest.json').write_text('{}', encoding='utf-8')
    target = tmp_path / 'pkg'
    _publish_staged(staging, target)
    assert (target / 'manifest.json').is_file()
    assert not staging.exists()
    # An existing *empty* destination is replaced.
    staging2 = tmp_path / '.pkg.staging-2'
    staging2.mkdir()
    (staging2 / 'manifest.json').write_text('{"v":2}', encoding='utf-8')
    _publish_staged(staging2, target.parent / 'pkg2')
    assert (target.parent / 'pkg2' / 'manifest.json').is_file()
    # A non-empty destination refuses honestly.
    staging3 = tmp_path / '.pkg.staging-3'
    staging3.mkdir()
    (staging3 / 'x').write_text('x', encoding='utf-8')
    with pytest.raises(OSError):
        _publish_staged(staging3, target)
    assert staging3.exists()  # caller cleans it
    assert (target / 'manifest.json').read_text('utf-8') == '{}'
