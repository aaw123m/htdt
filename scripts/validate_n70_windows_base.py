from __future__ import annotations

import argparse
import gc
import json
from pathlib import Path
import sys
import tempfile
from threading import Event
import time

from PySide6.QtCore import QThread, QTimer
from PySide6.QtWidgets import QApplication, QDockWidget, QPushButton, QTabBar

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend' / 'src'))

import htdt.prediction_workspace as prediction_workspace_module
from htdt.cad_prediction_repository import CadPredictionRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Offset3,
    Position3,
    RoomPrism,
    RoomVertex,
    SceneDocument,
    SceneEntity,
    Size3,
    make_polygon_room,
)
from htdt.native_cad import TheaterEditorWindow
from htdt.prediction_workspace import PredictionWorkspaceWindow

from validate_n40_windows import click_action, click_global, drag_selected_x, foreground, pump, wait_until

if sys.platform != 'win32':
    raise SystemExit('This acceptance harness requires Windows.')

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(errors='backslashreplace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(errors='backslashreplace')

FIXTURE_ID = 'fixture-a13-n70'
OTHER_DOCUMENT_ID = 'fixture-a13-n70-other'
L_FIXTURE_ID = 'fixture-a14-n70'


def _speaker(entity_id: str, x_m: float, y_m: float, role: str = 'FL') -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='speaker',
        name=entity_id,
        position=Position3(x_m=x_m, y_m=y_m, z_m=1.0),
        size_m=Size3(x_m=0.22, y_m=0.28, z_m=0.42),
        acoustic_reference_offset_m=Offset3(),
        speaker_role=role,
    )


def rectangular_scene(document_id: str) -> SceneDocument:
    return SceneDocument(
        document_id=document_id,
        schema_version=2,
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=(
            _speaker('speaker-fl', 1.3, 0.8, 'FL'),
            _speaker('speaker-fr', 4.7, 0.8, 'FR'),
            SceneEntity(
                entity_id='point-mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=3.0, y_m=3.0, z_m=1.1),
            ),
        ),
    )


def l_scene(document_id: str) -> SceneDocument:
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
    return SceneDocument(
        document_id=document_id,
        schema_version=2,
        room=room,
        entities=(
            _speaker('speaker-fl', 1.0, 0.8, 'FL'),
            SceneEntity(
                entity_id='point-mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=1.0, y_m=1.5, z_m=1.1),
            ),
        ),
    )


def _prediction_dock(window: PredictionWorkspaceWindow) -> QDockWidget | None:
    return next(
        (candidate for candidate in window.findChildren(QDockWidget) if candidate.windowTitle() == '予測'),
        None,
    )


def activate_prediction_tab(window: PredictionWorkspaceWindow, app: QApplication) -> bool:
    foreground(window, app)
    for tab_bar in window.findChildren(QTabBar):
        for index in range(tab_bar.count()):
            if tab_bar.tabText(index) != '予測':
                continue
            rect = tab_bar.tabRect(index)
            if rect.isEmpty():
                continue
            click_global(tab_bar.mapToGlobal(rect.center()), app)
            dock = _prediction_dock(window)
            if dock is None:
                return False
            return wait_until(app, lambda: not dock.visibleRegion().isEmpty(), 0.8)
    return False


def find_button(window: PredictionWorkspaceWindow, text: str) -> QPushButton:
    for button in window.findChildren(QPushButton):
        if button.text() == text:
            return button
    raise AssertionError(f'button not found: {text}')


def click_prediction_button(window: PredictionWorkspaceWindow, text: str, app: QApplication) -> bool:
    if not activate_prediction_tab(window, app):
        return False
    button = find_button(window, text)
    if not button.isEnabled() or button.visibleRegion().isEmpty():
        return False
    click_global(button.mapToGlobal(button.rect().center()), app)
    return True


def click_scene_entity(window: PredictionWorkspaceWindow, entity_id: str, app: QApplication) -> bool:
    item = window.items.get(entity_id)
    if item is None:
        return False
    tree = window.tree
    rect = tree.visualItemRect(item)
    if rect.isEmpty():
        tree.scrollToItem(item)
        pump(app, 0.05)
        rect = tree.visualItemRect(item)
    foreground(window, app)
    click_global(tree.viewport().mapToGlobal(rect.center()), app)
    return wait_until(app, lambda: window.selected_id == entity_id, 0.8)


def select_receiver(window: PredictionWorkspaceWindow, entity_id: str) -> bool:
    combo = window.prediction_receiver_combo
    if combo is None:
        return False
    index = combo.findData(entity_id)
    if index < 0:
        return False
    combo.setCurrentIndex(index)
    return combo.currentData() == entity_id


def start_prediction(window: PredictionWorkspaceWindow, app: QApplication) -> str | None:
    if not select_receiver(window, 'point-mlp'):
        return None
    if not click_prediction_button(window, '矩形幾何予測を実行', app):
        return None
    if not wait_until(app, lambda: window._current_prediction_token_id is not None, 0.8):
        return None
    return window._current_prediction_token_id


def wait_jobs_empty(window: PredictionWorkspaceWindow, app: QApplication, seconds: float = 2.5) -> bool:
    return wait_until(app, lambda: not window._prediction_tasks, seconds)


def run_a13(app: QApplication, root: Path) -> bool:
    root.mkdir(parents=True, exist_ok=True)
    repository = SceneRepository(root / 'scene.sqlite3')
    revision_a = repository.save(rectangular_scene(FIXTURE_ID), parent_revision_id=None).revision
    repository.save(rectangular_scene(OTHER_DOCUMENT_ID), parent_revision_id=None)
    prediction_repository = CadPredictionRepository(repository)

    window = TheaterEditorWindow(repository, FIXTURE_ID)
    foreground(window, app)
    closed = False
    heartbeat = {'count': 0}
    timer = QTimer(window)
    timer.setInterval(25)
    timer.timeout.connect(lambda: heartbeat.__setitem__('count', heartbeat['count'] + 1))
    timer.start()
    original_analyze = prediction_workspace_module.analyze_native_rectangular_geometry
    try:
        product_ok = isinstance(window, PredictionWorkspaceWindow) and _prediction_dock(window) is not None
        print('A13_N70_PRODUCT_COMPOSITION', product_ok, flush=True)
        if not product_ok:
            return False

        def delayed_analyze(*args, **kwargs):
            time.sleep(0.80)
            return original_analyze(*args, **kwargs)

        prediction_workspace_module.analyze_native_rectangular_geometry = delayed_analyze
        token_id = start_prediction(window, app)
        if token_id is None:
            print('A13_N70_START_STALE_JOB', False, flush=True)
            return False
        if not click_scene_entity(window, 'speaker-fl', app):
            print('A13_N70_MOUSE_SCENE_SELECT', False, flush=True)
            return False
        moved = drag_selected_x(window, app, scale=1.05)
        click_action(window, window.save_action, app)
        revision_b = repository.current_head(FIXTURE_ID)
        stale_finished = wait_jobs_empty(window, app, 2.8)
        stale_not_saved = (
            moved
            and revision_b is not None
            and revision_b.revision_id != revision_a.revision_id
            and stale_finished
            and len(prediction_repository.list_results(FIXTURE_ID)) == 0
        )
        print('A13_N70_EDIT_MAKES_RESULT_STALE', stale_not_saved, flush=True)
        if not stale_not_saved:
            return False

        responsive = heartbeat['count'] >= 10
        print('A13_N70_UI_RESPONSIVE', responsive, heartbeat['count'], flush=True)
        if not responsive:
            return False

        started = Event()
        release = Event()

        def blocked_analyze(*args, **kwargs):
            started.set()
            if not release.wait(5.0):
                raise TimeoutError('A13 N70 controlled prediction was not released')
            return original_analyze(*args, **kwargs)

        prediction_workspace_module.analyze_native_rectangular_geometry = blocked_analyze
        cancel_token_id = start_prediction(window, app)
        if cancel_token_id is None:
            release.set()
            print('A13_N70_START_CANCEL_JOB', False, flush=True)
            return False
        worker_started = wait_until(app, started.is_set, 0.8)
        print('A13_N70_CANCEL_WORKER_STARTED', worker_started, flush=True)
        if not worker_started:
            release.set()
            return False
        cancel_clicked = click_prediction_button(window, 'キャンセル', app)
        if not cancel_clicked:
            release.set()
            print('A13_N70_CANCEL_BUTTON_CLICKABLE', False, flush=True)
            return False
        cancel_token = window._prediction_tokens.get(cancel_token_id)
        cancel_registered = (
            cancel_token is not None
            and window.prediction_job_guard.is_cancelled(cancel_token)
            and window._current_prediction_token_id is None
        )
        release.set()
        cancelled = wait_jobs_empty(window, app, 1.8)
        cancel_ok = (
            cancel_registered
            and cancelled
            and len(prediction_repository.list_results(FIXTURE_ID)) == 0
        )
        print('A13_N70_CANCELLED_RESULT_NOT_APPLIED', cancel_ok, flush=True)
        if not cancel_ok:
            return False

        prediction_workspace_module.analyze_native_rectangular_geometry = delayed_analyze
        document_token_id = start_prediction(window, app)
        if document_token_id is None:
            print('A13_N70_START_DOCUMENT_JOB', False, flush=True)
            return False
        window.document_id = OTHER_DOCUMENT_ID
        document_finished = wait_jobs_empty(window, app, 2.0)
        document_switch_ok = (
            document_finished
            and len(prediction_repository.list_results(FIXTURE_ID)) == 0
            and len(prediction_repository.list_results(OTHER_DOCUMENT_ID)) == 0
        )
        print('A13_N70_DOCUMENT_SWITCH_RESULT_NOT_APPLIED', document_switch_ok, flush=True)
        if not document_switch_ok:
            return False
        window.document_id = FIXTURE_ID

        def close_delayed_analyze(*args, **kwargs):
            time.sleep(0.45)
            return original_analyze(*args, **kwargs)

        prediction_workspace_module.analyze_native_rectangular_geometry = close_delayed_analyze
        close_token_id = start_prediction(window, app)
        if close_token_id is None:
            print('A13_N70_START_CLOSE_JOB', False, flush=True)
            return False
        timer.stop()
        window.close()
        closed = True
        pump(app, 0.75)
        live_threads = [thread for thread in window.findChildren(QThread) if thread.isRunning()]
        close_ok = (
            not live_threads
            and len(prediction_repository.list_results(FIXTURE_ID)) == 0
        )
        print('A13_N70_CLEAN_EXIT_NO_WORKER', close_ok, len(live_threads), flush=True)
        return close_ok
    finally:
        prediction_workspace_module.analyze_native_rectangular_geometry = original_analyze
        timer.stop()
        if not closed:
            window.close()
            pump(app, 0.70)
        window.deleteLater()
        gc.collect()
        pump(app, 0.08)


def run_a14(app: QApplication, root: Path) -> bool:
    root.mkdir(parents=True, exist_ok=True)
    repository = SceneRepository(root / 'scene.sqlite3')
    repository.save(l_scene(L_FIXTURE_ID), parent_revision_id=None)
    prediction_repository = CadPredictionRepository(repository)
    window = TheaterEditorWindow(repository, L_FIXTURE_ID)
    foreground(window, app)
    try:
        product_ok = isinstance(window, PredictionWorkspaceWindow)
        print('A14_PRODUCT_COMPOSITION', product_ok, flush=True)
        if not product_ok:
            return False
        token_id = start_prediction(window, app)
        if token_id is None:
            print('A14_RUN_BUTTON', False, flush=True)
            return False
        finished = wait_jobs_empty(window, app, 2.0)
        results = prediction_repository.list_results(L_FIXTURE_ID)
        unsupported = (
            finished
            and len(results) == 2
            and len({item.run_id for item in results}) == 1
            and all(item.geometry_compatibility == 'unsupported' for item in results)
            and all(not item.modes and not item.reflections for item in results)
        )
        print('A14_NONRECT_UNSUPPORTED', unsupported, flush=True)
        if not unsupported:
            return False

        snapshot = json.loads(results[0].input_snapshot_json)
        no_hidden_approximation = (
            snapshot.get('approximation_rule') is None
            and len(snapshot.get('room', {}).get('footprint_vertices', [])) == 8
        )
        print('A14_NO_SILENT_RECTANGULAR_APPROXIMATION', no_hidden_approximation, flush=True)
        if not no_hidden_approximation:
            return False

        no_overlay = not window._prediction_actor_names
        scalar_disabled = window.prediction_scalar_button is not None and not window.prediction_scalar_button.isEnabled()
        print('A14_UNSUPPORTED_HAS_NO_OVERLAY', no_overlay, flush=True)
        print('A14_SCALAR_FIELD_CONTROL_GATED', scalar_disabled, flush=True)
        return no_overlay and scalar_disabled
    finally:
        window.close()
        pump(app, 0.65)
        window.deleteLater()
        gc.collect()
        pump(app, 0.08)


def main() -> int:
    parser = argparse.ArgumentParser(description='Run N70 A13/A14 Windows prediction acceptance.')
    parser.add_argument('--keep-data', type=Path, default=None)
    args = parser.parse_args()
    app = QApplication.instance() or QApplication([sys.argv[0]])

    if args.keep_data is not None:
        base = args.keep_data
        base.mkdir(parents=True, exist_ok=True)
        a13 = run_a13(app, base / 'a13')
        a14 = run_a14(app, base / 'a14')
    else:
        with tempfile.TemporaryDirectory(prefix='htdt-n70-', ignore_cleanup_errors=True) as temp:
            base = Path(temp)
            a13 = run_a13(app, base / 'a13')
            a14 = run_a14(app, base / 'a14')

    print('A13_N70_RESULT', 'PASS' if a13 else 'FAIL', flush=True)
    print('A14_RESULT', 'PASS' if a14 else 'FAIL', flush=True)
    passed = a13 and a14
    print('N70_A13_A14_RESULT', 'PASS' if passed else 'FAIL', flush=True)
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
