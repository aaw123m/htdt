from __future__ import annotations

import argparse
import gc
from pathlib import Path
import sys
import tempfile
from threading import Event
import time

from PySide6.QtCore import QThread, QTimer
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend' / 'src'))

import validate_n70_windows_base as base


def run_a13_latched(app: QApplication, root: Path) -> bool:
    """A13 with deterministic release points around real mouse/VTK operations."""

    root.mkdir(parents=True, exist_ok=True)
    repository = base.SceneRepository(root / 'scene.sqlite3')
    revision_a = repository.save(
        base.rectangular_scene(base.FIXTURE_ID), parent_revision_id=None
    ).revision
    repository.save(base.rectangular_scene(base.OTHER_DOCUMENT_ID), parent_revision_id=None)
    prediction_repository = base.CadPredictionRepository(repository)

    window = base.TheaterEditorWindow(repository, base.FIXTURE_ID)
    base.foreground(window, app)
    closed = False
    heartbeat = {'count': 0}
    timer = QTimer(window)
    timer.setInterval(25)
    timer.timeout.connect(lambda: heartbeat.__setitem__('count', heartbeat['count'] + 1))
    timer.start()
    original_analyze = base.prediction_workspace_module.analyze_native_rectangular_geometry
    release_events: list[Event] = []
    try:
        product_ok = isinstance(window, base.PredictionWorkspaceWindow) and base._prediction_dock(window) is not None
        print('A13_N70_PRODUCT_COMPOSITION', product_ok, flush=True)
        if not product_ok:
            return False

        stale_started = Event()
        stale_release = Event()
        release_events.append(stale_release)

        def stale_blocked_analyze(*args, **kwargs):
            stale_started.set()
            if not stale_release.wait(8.0):
                raise TimeoutError('A13 N70 stale prediction was not released')
            return original_analyze(*args, **kwargs)

        base.prediction_workspace_module.analyze_native_rectangular_geometry = stale_blocked_analyze
        token_id = base.start_prediction(window, app)
        if token_id is None:
            stale_release.set()
            print('A13_N70_START_STALE_JOB', False, flush=True)
            return False
        worker_started = base.wait_until(app, stale_started.is_set, 1.0)
        print('A13_N70_STALE_WORKER_STARTED', worker_started, flush=True)
        if not worker_started:
            stale_release.set()
            return False

        selected = base.click_scene_entity(window, 'speaker-fl', app)
        print('A13_N70_MOUSE_SCENE_SELECT', selected, flush=True)
        if not selected:
            stale_release.set()
            return False
        moved = base.drag_selected_x(window, app, scale=1.05)
        print('A13_N70_MOUSE_DRAG', moved, flush=True)
        if not moved:
            stale_release.set()
            return False
        base.click_action(window, window.save_action, app)
        revision_b = repository.current_head(base.FIXTURE_ID)
        save_changed_revision = revision_b is not None and revision_b.revision_id != revision_a.revision_id
        print('A13_N70_SAVE_CHANGED_REVISION', save_changed_revision, flush=True)
        stale_release.set()
        stale_finished = base.wait_jobs_empty(window, app, 2.8)
        stale_not_saved = (
            save_changed_revision
            and stale_finished
            and len(prediction_repository.list_results(base.FIXTURE_ID)) == 0
        )
        print('A13_N70_EDIT_MAKES_RESULT_STALE', stale_not_saved, flush=True)
        if not stale_not_saved:
            return False

        responsive = heartbeat['count'] >= 10
        print('A13_N70_UI_RESPONSIVE', responsive, heartbeat['count'], flush=True)
        if not responsive:
            return False

        cancel_started = Event()
        cancel_release = Event()
        release_events.append(cancel_release)

        def cancel_blocked_analyze(*args, **kwargs):
            cancel_started.set()
            if not cancel_release.wait(8.0):
                raise TimeoutError('A13 N70 cancelled prediction was not released')
            return original_analyze(*args, **kwargs)

        base.prediction_workspace_module.analyze_native_rectangular_geometry = cancel_blocked_analyze
        cancel_token_id = base.start_prediction(window, app)
        if cancel_token_id is None:
            cancel_release.set()
            print('A13_N70_START_CANCEL_JOB', False, flush=True)
            return False
        cancel_worker_started = base.wait_until(app, cancel_started.is_set, 1.0)
        print('A13_N70_CANCEL_WORKER_STARTED', cancel_worker_started, flush=True)
        if not cancel_worker_started:
            cancel_release.set()
            return False
        cancel_clicked = base.click_prediction_button(window, 'キャンセル', app)
        if not cancel_clicked:
            cancel_release.set()
            print('A13_N70_CANCEL_BUTTON_CLICKABLE', False, flush=True)
            return False
        cancel_token = window._prediction_tokens.get(cancel_token_id)
        cancel_registered = (
            cancel_token is not None
            and window.prediction_job_guard.is_cancelled(cancel_token)
            and window._current_prediction_token_id is None
        )
        print('A13_N70_CANCEL_REGISTERED', cancel_registered, flush=True)
        cancel_release.set()
        cancelled = base.wait_jobs_empty(window, app, 1.8)
        cancel_ok = (
            cancel_registered
            and cancelled
            and len(prediction_repository.list_results(base.FIXTURE_ID)) == 0
        )
        print('A13_N70_CANCELLED_RESULT_NOT_APPLIED', cancel_ok, flush=True)
        if not cancel_ok:
            return False

        document_started = Event()
        document_release = Event()
        release_events.append(document_release)

        def document_blocked_analyze(*args, **kwargs):
            document_started.set()
            if not document_release.wait(8.0):
                raise TimeoutError('A13 N70 document-switch prediction was not released')
            return original_analyze(*args, **kwargs)

        base.prediction_workspace_module.analyze_native_rectangular_geometry = document_blocked_analyze
        document_token_id = base.start_prediction(window, app)
        if document_token_id is None:
            document_release.set()
            print('A13_N70_START_DOCUMENT_JOB', False, flush=True)
            return False
        document_worker_started = base.wait_until(app, document_started.is_set, 1.0)
        print('A13_N70_DOCUMENT_WORKER_STARTED', document_worker_started, flush=True)
        if not document_worker_started:
            document_release.set()
            return False
        window.document_id = base.OTHER_DOCUMENT_ID
        document_release.set()
        document_finished = base.wait_jobs_empty(window, app, 2.0)
        document_switch_ok = (
            document_finished
            and len(prediction_repository.list_results(base.FIXTURE_ID)) == 0
            and len(prediction_repository.list_results(base.OTHER_DOCUMENT_ID)) == 0
        )
        print('A13_N70_DOCUMENT_SWITCH_RESULT_NOT_APPLIED', document_switch_ok, flush=True)
        if not document_switch_ok:
            return False
        window.document_id = base.FIXTURE_ID

        def close_delayed_analyze(*args, **kwargs):
            time.sleep(0.45)
            return original_analyze(*args, **kwargs)

        base.prediction_workspace_module.analyze_native_rectangular_geometry = close_delayed_analyze
        close_token_id = base.start_prediction(window, app)
        if close_token_id is None:
            print('A13_N70_START_CLOSE_JOB', False, flush=True)
            return False
        timer.stop()
        window.close()
        closed = True
        base.pump(app, 0.75)
        live_threads = [thread for thread in window.findChildren(QThread) if thread.isRunning()]
        close_ok = (
            not live_threads
            and len(prediction_repository.list_results(base.FIXTURE_ID)) == 0
        )
        print('A13_N70_CLEAN_EXIT_NO_WORKER', close_ok, len(live_threads), flush=True)
        return close_ok
    finally:
        for release in release_events:
            release.set()
        base.prediction_workspace_module.analyze_native_rectangular_geometry = original_analyze
        timer.stop()
        if not closed:
            window.close()
            base.pump(app, 0.70)
        window.deleteLater()
        gc.collect()
        base.pump(app, 0.08)


def run_a14_fast_completion_tolerant(app: QApplication, root: Path) -> bool:
    """Run base A14 without requiring a fast worker token to remain observable."""

    original_start_prediction = base.start_prediction

    def tolerant_start_prediction(window, qt_app):
        if not base.select_receiver(window, 'point-mlp'):
            return None
        if not base.click_prediction_button(window, '矩形幾何予測を実行', qt_app):
            return None
        token_id = window._current_prediction_token_id
        if token_id is not None:
            return token_id
        base.pump(qt_app, 0.05)
        token_id = window._current_prediction_token_id
        if token_id is not None:
            return token_id
        # Unsupported geometry can finish and clear the token before the harness
        # observes it. Base A14 subsequently requires the persisted two-result
        # unsupported contract, so this sentinel cannot turn a failed run into PASS.
        if not window._prediction_tasks:
            print('A14_FAST_COMPLETION_OBSERVED', True, flush=True)
            return 'completed-before-token-observation'
        return next(iter(window._prediction_tasks), 'prediction-started')

    base.start_prediction = tolerant_start_prediction
    try:
        return base.run_a14(app, root)
    finally:
        base.start_prediction = original_start_prediction


def main() -> int:
    parser = argparse.ArgumentParser(description='Run deterministic N70 A13/A14 Windows acceptance.')
    parser.add_argument('--keep-data', type=Path, default=None)
    args = parser.parse_args()
    app = QApplication.instance() or QApplication([sys.argv[0]])

    if args.keep_data is not None:
        base_dir = args.keep_data
        base_dir.mkdir(parents=True, exist_ok=True)
        a13 = run_a13_latched(app, base_dir / 'a13')
        a14 = run_a14_fast_completion_tolerant(app, base_dir / 'a14')
    else:
        with tempfile.TemporaryDirectory(prefix='htdt-n70-gate-', ignore_cleanup_errors=True) as temp:
            base_dir = Path(temp)
            a13 = run_a13_latched(app, base_dir / 'a13')
            a14 = run_a14_fast_completion_tolerant(app, base_dir / 'a14')

    print('A13_N70_RESULT', 'PASS' if a13 else 'FAIL', flush=True)
    print('A14_RESULT', 'PASS' if a14 else 'FAIL', flush=True)
    passed = a13 and a14
    print('N70_A13_A14_RESULT', 'PASS' if passed else 'FAIL', flush=True)
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
