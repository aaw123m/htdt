from __future__ import annotations

import argparse
import gc
from pathlib import Path
import sys
import tempfile
from threading import Event
import time

from PySide6.QtCore import QThread, QTimer
from PySide6.QtWidgets import QApplication, QDockWidget, QPushButton, QScrollArea, QTabBar

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend' / 'src'))

import htdt.optimization_workspace as optimization_workspace_module
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import (
    Offset3,
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
    Size3,
    scene_content_hash,
)
from htdt.native_cad import TheaterEditorWindow
from htdt.native_editor import ROLE
from htdt.optimization_workspace import OptimizationWorkspaceWindow

from validate_n40_windows import click_action, click_global, drag_selected_x, foreground, pump, wait_until

if sys.platform != 'win32':
    raise SystemExit('This acceptance harness requires Windows.')

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(errors='backslashreplace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(errors='backslashreplace')

FIXTURE_ID = 'fixture-a13-a14-n80'
OTHER_DOCUMENT_ID = 'fixture-a13-n80-other'


def _speaker(entity_id: str, x_m: float, y_m: float, role: str) -> SceneEntity:
    return SceneEntity(
        entity_id=entity_id,
        kind='speaker',
        name=entity_id,
        position=Position3(x_m=x_m, y_m=y_m, z_m=1.0),
        size_m=Size3(x_m=0.22, y_m=0.28, z_m=0.42),
        acoustic_reference_offset_m=Offset3(),
        speaker_role=role,
    )


def fixture_scene(document_id: str) -> SceneDocument:
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


def _optimization_dock(window: OptimizationWorkspaceWindow) -> QDockWidget | None:
    return next(
        (candidate for candidate in window.findChildren(QDockWidget) if candidate.windowTitle() == '最適化'),
        None,
    )


def activate_optimization_tab(window: OptimizationWorkspaceWindow, app: QApplication) -> bool:
    foreground(window, app)
    for tab_bar in window.findChildren(QTabBar):
        for index in range(tab_bar.count()):
            if tab_bar.tabText(index) != '最適化':
                continue
            rect = tab_bar.tabRect(index)
            if rect.isEmpty():
                continue
            click_global(tab_bar.mapToGlobal(rect.center()), app)
            dock = _optimization_dock(window)
            if dock is None:
                return False
            return wait_until(app, lambda: not dock.visibleRegion().isEmpty(), 0.8)
    return False


def find_button(window: OptimizationWorkspaceWindow, text: str) -> QPushButton:
    for button in window.findChildren(QPushButton):
        if button.text() == text:
            return button
    raise AssertionError(f'button not found: {text}')


def click_optimization_button(
    window: OptimizationWorkspaceWindow,
    text: str,
    app: QApplication,
) -> bool:
    if not activate_optimization_tab(window, app):
        return False
    button = find_button(window, text)
    dock = _optimization_dock(window)
    if dock is not None and isinstance(dock.widget(), QScrollArea):
        dock.widget().ensureWidgetVisible(button, 30, 30)
        pump(app, 0.08)
    if not button.isEnabled() or button.visibleRegion().isEmpty():
        return False
    click_global(button.mapToGlobal(button.rect().center()), app)
    return True


def select_scene_entity(
    window: OptimizationWorkspaceWindow,
    entity_id: str,
    app: QApplication,
) -> bool:
    item = window.items.get(entity_id)
    if item is None:
        return False
    tree = window.tree
    tree.scrollToItem(item)
    pump(app, 0.05)
    rect = tree.visualItemRect(item)
    if rect.isEmpty():
        return False
    foreground(window, app)
    click_global(tree.viewport().mapToGlobal(rect.center()), app)
    return wait_until(app, lambda: window.selected_id == entity_id, 0.8)


def configure_and_save_search(
    window: OptimizationWorkspaceWindow,
    app: QApplication,
    *,
    suffix: str,
) -> str | None:
    if not activate_optimization_tab(window, app):
        return None
    combo = window.search_entity_combo
    axis_combo = window.search_axis_combo
    if (
        combo is None
        or axis_combo is None
        or window.search_min_field is None
        or window.search_max_field is None
        or window.search_step_field is None
        or window.search_limit_field is None
        or window.search_name_field is None
    ):
        return None
    index = combo.findData('speaker-fl')
    if index < 0:
        return None
    combo.setCurrentIndex(index)
    axis_index = axis_combo.findData('x')
    if axis_index < 0:
        return None
    axis_combo.setCurrentIndex(axis_index)
    window.search_min_field.setValue(1.50)
    window.search_max_field.setValue(1.70)
    window.search_step_field.setValue(0.10)
    window.search_limit_field.setValue(20)
    window.search_name_field.setText(f'N80 acceptance {suffix}')
    if not click_optimization_button(window, '軸を追加 / 更新', app):
        return None
    before = len(window.search_repository.list_specs(window.document_id))
    if not click_optimization_button(window, '探索仕様を保存', app):
        return None
    if not wait_until(
        app,
        lambda: len(window.search_repository.list_specs(window.document_id)) == before + 1,
        0.8,
    ):
        return None
    return window.search_selected_spec_id


def start_search(window: OptimizationWorkspaceWindow, app: QApplication) -> str | None:
    if not click_optimization_button(window, '候補を生成', app):
        return None
    if wait_until(app, lambda: window._current_search_task_id is not None, 0.35):
        return window._current_search_task_id
    # A tiny feasible set can complete and clear the task id before the harness
    # observes it. A completed page is stronger evidence than a transient token.
    if window.search_candidate_page is not None and not window._search_tasks:
        print('A14_N80_FAST_GENERATION_OBSERVED', True, flush=True)
        return 'completed-before-task-observation'
    return None


def wait_search_empty(
    window: OptimizationWorkspaceWindow,
    app: QApplication,
    seconds: float = 2.5,
) -> bool:
    return wait_until(app, lambda: not window._search_tasks, seconds)


def select_last_candidate(window: OptimizationWorkspaceWindow, app: QApplication) -> bool:
    tree = window.search_candidate_tree
    if tree is None or tree.topLevelItemCount() < 1:
        return False
    if not activate_optimization_tab(window, app):
        return False
    dock = _optimization_dock(window)
    if dock is not None and isinstance(dock.widget(), QScrollArea):
        dock.widget().ensureWidgetVisible(tree, 30, 30)
        pump(app, 0.08)
    item = tree.topLevelItem(tree.topLevelItemCount() - 1)
    tree.scrollToItem(item)
    pump(app, 0.05)
    rect = tree.visualItemRect(item)
    if rect.isEmpty():
        return False
    click_global(tree.viewport().mapToGlobal(rect.center()), app)
    expected = item.data(0, ROLE)
    return wait_until(
        app,
        lambda: window.search_selected_candidate_id == str(expected),
        0.8,
    )


def run_a13(app: QApplication, root: Path) -> bool:
    root.mkdir(parents=True, exist_ok=True)
    repository = SceneRepository(root / 'scene.sqlite3')
    revision_a = repository.save(fixture_scene(FIXTURE_ID), parent_revision_id=None).revision
    repository.save(fixture_scene(OTHER_DOCUMENT_ID), parent_revision_id=None)

    window = TheaterEditorWindow(repository, FIXTURE_ID)
    foreground(window, app)
    closed = False
    heartbeat = {'count': 0}
    timer = QTimer(window)
    timer.setInterval(25)
    timer.timeout.connect(lambda: heartbeat.__setitem__('count', heartbeat['count'] + 1))
    timer.start()
    original_generate = optimization_workspace_module.generate_cad_candidates
    release_events: list[Event] = []
    try:
        product_ok = (
            isinstance(window, OptimizationWorkspaceWindow)
            and _optimization_dock(window) is not None
        )
        print('A13_N80_PRODUCT_COMPOSITION', product_ok, flush=True)
        if not product_ok:
            return False

        if configure_and_save_search(window, app, suffix='stale') is None:
            print('A13_N80_SEARCHSPEC_CREATED', False, flush=True)
            return False

        stale_started = Event()
        stale_release = Event()
        release_events.append(stale_release)

        def stale_blocked_generate(*args, **kwargs):
            stale_started.set()
            if not stale_release.wait(8.0):
                raise TimeoutError('A13 N80 stale search was not released')
            return original_generate(*args, **kwargs)

        optimization_workspace_module.generate_cad_candidates = stale_blocked_generate
        if start_search(window, app) is None:
            stale_release.set()
            print('A13_N80_START_STALE_JOB', False, flush=True)
            return False
        worker_started = wait_until(app, stale_started.is_set, 1.0)
        print('A13_N80_STALE_WORKER_STARTED', worker_started, flush=True)
        if not worker_started:
            stale_release.set()
            return False

        selected = select_scene_entity(window, 'speaker-fl', app)
        print('A13_N80_MOUSE_SCENE_SELECT', selected, flush=True)
        if not selected:
            stale_release.set()
            return False
        moved = drag_selected_x(window, app, scale=1.05)
        print('A13_N80_MOUSE_DRAG', moved, flush=True)
        if not moved:
            stale_release.set()
            return False
        click_action(window, window.save_action, app)
        revision_b = repository.current_head(FIXTURE_ID)
        save_changed_revision = revision_b is not None and revision_b.revision_id != revision_a.revision_id
        print('A13_N80_SAVE_CHANGED_REVISION', save_changed_revision, flush=True)
        stale_release.set()
        stale_finished = wait_search_empty(window, app, 2.8)
        stale_discarded = (
            save_changed_revision
            and stale_finished
            and window.search_candidate_page is None
        )
        print('A13_N80_EDIT_MAKES_RESULT_STALE', stale_discarded, flush=True)
        if not stale_discarded:
            return False

        responsive = heartbeat['count'] >= 10
        print('A13_N80_UI_RESPONSIVE', responsive, heartbeat['count'], flush=True)
        if not responsive:
            return False

        if configure_and_save_search(window, app, suffix='current') is None:
            print('A13_N80_CURRENT_SEARCHSPEC_CREATED', False, flush=True)
            return False

        cancel_started = Event()
        cancel_release = Event()
        release_events.append(cancel_release)

        def cancel_blocked_generate(*args, **kwargs):
            cancel_started.set()
            cancelled = kwargs.get('cancelled')
            deadline = time.perf_counter() + 8.0
            while time.perf_counter() < deadline:
                if callable(cancelled) and cancelled():
                    raise RuntimeError('controlled N80 cancellation')
                if cancel_release.wait(0.01):
                    return original_generate(*args, **kwargs)
            raise TimeoutError('A13 N80 cancel search was not released')

        optimization_workspace_module.generate_cad_candidates = cancel_blocked_generate
        if start_search(window, app) is None:
            cancel_release.set()
            print('A13_N80_START_CANCEL_JOB', False, flush=True)
            return False
        cancel_worker_started = wait_until(app, cancel_started.is_set, 1.0)
        print('A13_N80_CANCEL_WORKER_STARTED', cancel_worker_started, flush=True)
        if not cancel_worker_started:
            cancel_release.set()
            return False
        cancel_clicked = click_optimization_button(window, '生成をキャンセル', app)
        if not cancel_clicked:
            cancel_release.set()
            print('A13_N80_CANCEL_BUTTON_CLICKABLE', False, flush=True)
            return False
        cancelled = wait_search_empty(window, app, 2.0)
        cancel_ok = cancelled and window.search_candidate_page is None
        print('A13_N80_CANCELLED_RESULT_NOT_APPLIED', cancel_ok, flush=True)
        if not cancel_ok:
            return False

        document_started = Event()
        document_release = Event()
        release_events.append(document_release)

        def document_blocked_generate(*args, **kwargs):
            document_started.set()
            if not document_release.wait(8.0):
                raise TimeoutError('A13 N80 document search was not released')
            return original_generate(*args, **kwargs)

        optimization_workspace_module.generate_cad_candidates = document_blocked_generate
        if start_search(window, app) is None:
            document_release.set()
            print('A13_N80_START_DOCUMENT_JOB', False, flush=True)
            return False
        document_worker_started = wait_until(app, document_started.is_set, 1.0)
        print('A13_N80_DOCUMENT_WORKER_STARTED', document_worker_started, flush=True)
        if not document_worker_started:
            document_release.set()
            return False
        window.document_id = OTHER_DOCUMENT_ID
        document_release.set()
        document_finished = wait_search_empty(window, app, 2.0)
        document_ok = document_finished and window.search_candidate_page is None
        print('A13_N80_DOCUMENT_SWITCH_RESULT_NOT_APPLIED', document_ok, flush=True)
        if not document_ok:
            return False
        window.document_id = FIXTURE_ID
        window._refresh_search_binding_state()
        pump(app, 0.08)

        def close_delayed_generate(*args, **kwargs):
            time.sleep(0.45)
            return original_generate(*args, **kwargs)

        optimization_workspace_module.generate_cad_candidates = close_delayed_generate
        if start_search(window, app) is None:
            print('A13_N80_START_CLOSE_JOB', False, flush=True)
            return False
        timer.stop()
        window.close()
        closed = True
        pump(app, 0.75)
        live_threads = [thread for thread in window.findChildren(QThread) if thread.isRunning()]
        close_ok = not live_threads
        print('A13_N80_CLEAN_EXIT_NO_WORKER', close_ok, len(live_threads), flush=True)
        return close_ok
    finally:
        for release in release_events:
            release.set()
        optimization_workspace_module.generate_cad_candidates = original_generate
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
    repository.save(fixture_scene(FIXTURE_ID), parent_revision_id=None)
    window = TheaterEditorWindow(repository, FIXTURE_ID)
    foreground(window, app)
    try:
        product_ok = (
            isinstance(window, OptimizationWorkspaceWindow)
            and _optimization_dock(window) is not None
        )
        print('A14_N80_PRODUCT_COMPOSITION', product_ok, flush=True)
        if not product_ok:
            return False

        spec_created = configure_and_save_search(window, app, suffix='apply') is not None
        print('A14_N80_SEARCHSPEC_CREATED', spec_created, flush=True)
        if not spec_created:
            return False

        if start_search(window, app) is None:
            print('A14_N80_GENERATE_BUTTON', False, flush=True)
            return False
        generated = wait_search_empty(window, app, 2.5) and window.search_candidate_page is not None
        print('A14_N80_CANDIDATES_GENERATED', generated, flush=True)
        if not generated:
            return False

        page = window.search_candidate_page
        deterministic_counts = (
            page is not None
            and page.raw_candidate_count == 3
            and page.feasible_candidate_count == 3
            and page.rejected_candidate_count == 0
            and page.duplicate_candidate_count == 0
        )
        print('A14_N80_DETERMINISTIC_COUNTS', deterministic_counts, flush=True)
        if not deterministic_counts:
            return False

        selected = select_last_candidate(window, app)
        print('A14_N80_MOUSE_SELECT_CANDIDATE', selected, flush=True)
        if not selected:
            return False
        candidate = window._selected_search_candidate()
        if candidate is None:
            return False

        before_hash = scene_content_hash(window.working.committed_document)
        before_history = window.working.history_length
        before_x = window.working.committed_document.entity('speaker-fl').position.x_m

        preview_clicked = click_optimization_button(window, '候補をpreview', app)
        preview_ok = (
            preview_clicked
            and window.search_preview_candidate_id == candidate.candidate_id
            and scene_content_hash(window.working.committed_document) == before_hash
            and window.working.history_length == before_history
            and not window.working.is_dirty
            and any(name.startswith('search-preview-') for name in window._search_actor_names)
        )
        print('A14_N80_PREVIEW_NON_AUTHORITATIVE', preview_ok, flush=True)
        if not preview_ok:
            return False

        apply_clicked = click_optimization_button(window, '候補を適用', app)
        target_x = float(candidate.positions['speaker-fl']['x_m'])
        moved_to_candidate = wait_until(
            app,
            lambda: abs(
                window.working.committed_document.entity('speaker-fl').position.x_m - target_x
            ) < 1e-9,
            0.8,
        )
        apply_ok = (
            apply_clicked
            and moved_to_candidate
            and window.working.history_length == before_history + 1
            and window.working.is_dirty
            and window.search_preview_candidate_id is None
        )
        print('A14_N80_APPLY_ONE_COMMAND', apply_ok, flush=True)
        if not apply_ok:
            return False

        click_action(window, window.undo_action, app)
        restored = wait_until(
            app,
            lambda: abs(
                window.working.committed_document.entity('speaker-fl').position.x_m - before_x
            ) < 1e-9,
            0.8,
        )
        undo_ok = (
            restored
            and scene_content_hash(window.working.committed_document) == before_hash
            and not window.working.is_dirty
        )
        print('A14_N80_UNDO_RESTORES_EXACT_SCENE', undo_ok, flush=True)
        return undo_ok
    finally:
        window.close()
        pump(app, 0.70)
        window.deleteLater()
        gc.collect()
        pump(app, 0.08)


def main() -> int:
    parser = argparse.ArgumentParser(description='Run N80 A13/A14 Windows optimization acceptance.')
    parser.add_argument('--keep-data', type=Path, default=None)
    args = parser.parse_args()
    app = QApplication.instance() or QApplication([sys.argv[0]])

    if args.keep_data is not None:
        base = args.keep_data
        base.mkdir(parents=True, exist_ok=True)
        a13 = run_a13(app, base / 'a13')
        a14 = run_a14(app, base / 'a14')
    else:
        with tempfile.TemporaryDirectory(prefix='htdt-n80-', ignore_cleanup_errors=True) as temp:
            base = Path(temp)
            a13 = run_a13(app, base / 'a13')
            a14 = run_a14(app, base / 'a14')

    print('A13_N80_RESULT', 'PASS' if a13 else 'FAIL', flush=True)
    print('A14_N80_RESULT', 'PASS' if a14 else 'FAIL', flush=True)
    passed = a13 and a14
    print('N80_A13_A14_RESULT', 'PASS' if passed else 'FAIL', flush=True)
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
