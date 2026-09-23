from __future__ import annotations

import argparse
import gc
from pathlib import Path
import sys
import tempfile

from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend' / 'src'))

from htdt.cad_constraint_models import (
    CadConstraintPoint2D,
    CadConstraintSet,
    CadExclusionRegionConstraint,
    CadWallClearanceConstraint,
)
from htdt.cad_constraint_repository import CadConstraintRepository
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import Position3, RoomPrism, SceneDocument, SceneEntity, Size3
from htdt.cad_walls import make_wall_topology
from htdt.native_cad import ConstraintEditorWindow, TheaterEditorWindow
from htdt.native_editor import ROLE

from validate_n40_windows import click_actor, click_global, drag_selected_x, foreground, pump, wait_until

if sys.platform != 'win32':
    raise SystemExit('This acceptance harness requires Windows.')

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(errors='backslashreplace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(errors='backslashreplace')

FIXTURE_ID = 'fixture-a11-n50'
WALL_CONSTRAINT_ID = 'right-wall-clearance'
WALKWAY_CONSTRAINT_ID = 'walkway-main'
RIGHT_WALL_ID = 'wall:front-right->rear-right'


def make_scene() -> SceneDocument:
    room = RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4)
    return SceneDocument(
        document_id=FIXTURE_ID,
        schema_version=3,
        room=room,
        wall_topology=make_wall_topology(room),
        entities=(
            SceneEntity(
                entity_id='speaker-right',
                kind='speaker',
                name='右スピーカー',
                speaker_role='FR',
                position=Position3(x_m=4.6, y_m=1.0, z_m=1.0),
                size_m=Size3(x_m=0.30, y_m=0.30, z_m=0.50),
            ),
            SceneEntity(
                entity_id='seat-main',
                kind='seat',
                name='主座席',
                position=Position3(x_m=1.5, y_m=2.0, z_m=0.45),
                size_m=Size3(x_m=0.70, y_m=0.80, z_m=0.90),
            ),
            SceneEntity(
                entity_id='free-furniture',
                kind='furniture',
                name='自由家具',
                position=Position3(x_m=3.0, y_m=3.1, z_m=0.40),
                size_m=Size3(x_m=0.60, y_m=0.40, z_m=0.80),
            ),
        ),
    )


def make_constraints() -> CadConstraintSet:
    return CadConstraintSet(
        document_id=FIXTURE_ID,
        constraints=(
            CadWallClearanceConstraint(
                constraint_id=WALL_CONSTRAINT_ID,
                name='右壁離隔',
                entity_ids=('speaker-right',),
                wall_id=RIGHT_WALL_ID,
                min_m=1.10,
            ),
            CadExclusionRegionConstraint(
                constraint_id=WALKWAY_CONSTRAINT_ID,
                name='主通路',
                entity_ids=('seat-main',),
                region_role='walkway',
                vertices=(
                    CadConstraintPoint2D(x_m=2.05, y_m=1.0),
                    CadConstraintPoint2D(x_m=3.50, y_m=1.0),
                    CadConstraintPoint2D(x_m=3.50, y_m=3.0),
                    CadConstraintPoint2D(x_m=2.05, y_m=3.0),
                ),
            ),
        ),
    )


def click_constraint_reason(window: ConstraintEditorWindow, constraint_id: str, app: QApplication) -> bool:
    tree = window.constraint_tree
    if tree is None:
        return False
    for index in range(tree.topLevelItemCount()):
        item = tree.topLevelItem(index)
        if str(item.data(0, ROLE + 1)) != constraint_id:
            continue
        rect = tree.visualItemRect(item)
        if rect.isEmpty():
            tree.scrollToItem(item)
            pump(app, 0.05)
            rect = tree.visualItemRect(item)
        click_global(tree.viewport().mapToGlobal(rect.center()), app)
        return wait_until(
            app,
            lambda: (
                tree.currentItem() is item
                and window.constraint_selected_result_id == str(item.data(0, ROLE))
            ),
            0.75,
        )
    return False


def quality_wording_absent(window: ConstraintEditorWindow) -> bool:
    text = '\n'.join(
        filter(
            None,
            (
                window.windowTitle(),
                '' if window.constraint_summary_label is None else window.constraint_summary_label.text(),
                '' if window.constraint_detail_label is None else window.constraint_detail_label.text(),
            ),
        )
    )
    forbidden = ('音質が良い', '音質が悪い', '最適配置', 'おすすめ配置', '高音質', '低音質')
    return not any(word in text for word in forbidden)


def run_a11(app: QApplication, root: Path) -> bool:
    database = root / 'scene.sqlite3'
    repository = SceneRepository(database)
    repository.save(make_scene(), parent_revision_id=None)
    CadConstraintRepository(database).save(make_constraints())

    window = TheaterEditorWindow(repository, FIXTURE_ID)
    foreground(window, app)
    try:
        product_ok = isinstance(window, ConstraintEditorWindow) and window.constraint_tree is not None
        initial_ok = (
            product_ok
            and window.constraint_evaluation.constraints_satisfied
            and window.constraint_summary_label is not None
            and window.constraint_summary_label.text() == '制約を満たす'
            and quality_wording_absent(window)
        )
        print('A11_PRODUCT_COMPOSITION', product_ok, flush=True)
        print('A11_INITIAL_FEASIBLE', initial_ok, flush=True)
        if not initial_ok:
            return False

        # Real mouse: move the speaker toward the right wall. The preview violates
        # 1.10 m clearance but stays inside the room prism, so the move must reject.
        click_actor(window, 'speaker-right', app)
        before_wall = window.working.committed_document.entity('speaker-right').position
        before_wall_history = window.working.history_length
        wall_drag_completed = drag_selected_x(window, app, scale=1.05)
        after_wall = window.working.committed_document.entity('speaker-right').position
        wall_violation = next(
            (item for item in (window.constraint_last_rejected.violations if window.constraint_last_rejected else ())
             if item.constraint_id == WALL_CONSTRAINT_ID),
            None,
        )
        wall_rejected = (
            wall_drag_completed
            and after_wall == before_wall
            and window.working.history_length == before_wall_history
            and wall_violation is not None
            and wall_violation.actual_m is not None
            and wall_violation.actual_m < 1.10
        )
        print('A11_MOUSE_WALL_REJECT', wall_rejected, flush=True)
        if not wall_rejected:
            return False

        wall_reason_clicked = click_constraint_reason(window, WALL_CONSTRAINT_ID, app)
        wall_detail = '' if window.constraint_detail_label is None else window.constraint_detail_label.text()
        wall_reason_ok = (
            wall_reason_clicked
            and window.selected_id == 'speaker-right'
            and '右壁離隔' in wall_detail
            and '実測距離:' in wall_detail
            and '必要条件: 1.100 m 以上' in wall_detail
            and 'constraint-selected-wall' in window._constraint_actor_names
            and 'constraint-distance-segment' in window._constraint_actor_names
            and 'constraint-rejected-candidate' in window._constraint_actor_names
            and quality_wording_absent(window)
        )
        print('A11_MOUSE_WALL_REASON', wall_reason_ok, flush=True)
        if not wall_reason_ok:
            return False

        # Real mouse: move the seat into the pre-defined walkway. This is a new
        # exclusion violation, so the candidate must reject without history change.
        click_actor(window, 'seat-main', app)
        before_walkway = window.working.committed_document.entity('seat-main').position
        before_walkway_history = window.working.history_length
        walkway_drag_completed = drag_selected_x(window, app, scale=1.05)
        after_walkway = window.working.committed_document.entity('seat-main').position
        walkway_violation = next(
            (item for item in (window.constraint_last_rejected.violations if window.constraint_last_rejected else ())
             if item.constraint_id == WALKWAY_CONSTRAINT_ID),
            None,
        )
        walkway_rejected = (
            walkway_drag_completed
            and after_walkway == before_walkway
            and window.working.history_length == before_walkway_history
            and walkway_violation is not None
            and walkway_violation.region_role == 'walkway'
        )
        print('A11_MOUSE_WALKWAY_REJECT', walkway_rejected, flush=True)
        if not walkway_rejected:
            return False

        walkway_reason_clicked = click_constraint_reason(window, WALKWAY_CONSTRAINT_ID, app)
        walkway_detail = '' if window.constraint_detail_label is None else window.constraint_detail_label.text()
        walkway_reason_ok = (
            walkway_reason_clicked
            and window.selected_id == 'seat-main'
            and '主通路' in walkway_detail
            and '通路に干渉しています' in walkway_detail
            and '領域: 通路' in walkway_detail
            and f'constraint-region:{WALKWAY_CONSTRAINT_ID}' in window._constraint_actor_names
            and 'constraint-rejected-candidate' in window._constraint_actor_names
            and quality_wording_absent(window)
        )
        print('A11_MOUSE_WALKWAY_REASON', walkway_reason_ok, flush=True)
        if not walkway_reason_ok:
            return False

        # Rejection must not leave the normal transform path wedged.
        click_actor(window, 'free-furniture', app)
        free_before = window.working.committed_document.entity('free-furniture').position
        free_history = window.working.history_length
        free_drag_completed = drag_selected_x(window, app, scale=1.05)
        free_after = window.working.committed_document.entity('free-furniture').position
        normal_edit_ok = (
            free_drag_completed
            and free_after != free_before
            and window.working.history_length == free_history + 1
            and window.constraint_last_rejected is None
            and window.constraint_evaluation.constraints_satisfied
        )
        print('A11_EDIT_AFTER_REJECT', normal_edit_ok, flush=True)
        if not normal_edit_ok:
            return False

        persisted = CadConstraintRepository(database).load(FIXTURE_ID) == make_constraints()
        print('A11_CONSTRAINT_PERSISTENCE', persisted, flush=True)
        print('A11_QUALITY_WORDING_ABSENT', quality_wording_absent(window), flush=True)
        return persisted and quality_wording_absent(window)
    finally:
        window.close()
        pump(app, 0.10)
        window.deleteLater()
        gc.collect()
        pump(app, 0.08)


def main() -> int:
    parser = argparse.ArgumentParser(description='Run N50 A11 Windows acceptance with real OS mouse input.')
    parser.add_argument('--keep-data', type=Path, default=None)
    args = parser.parse_args()
    app = QApplication.instance() or QApplication([sys.argv[0]])
    if args.keep_data is not None:
        args.keep_data.mkdir(parents=True, exist_ok=True)
        passed = run_a11(app, args.keep_data)
    else:
        with tempfile.TemporaryDirectory(prefix='htdt-a11-', ignore_cleanup_errors=True) as temp:
            passed = run_a11(app, Path(temp))
    print('A11_RESULT', 'PASS' if passed else 'FAIL', flush=True)
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
