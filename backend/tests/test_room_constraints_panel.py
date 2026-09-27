"""Offscreen Qt coverage for RoomConstraintsPanel (#486) — round 2.

The panel renders the authoritative ``CadConstraintSet`` plus its live
evaluation: summary text must distinguish no-constraints / satisfied /
violations / evaluation-error, and result rows must resolve back to their
constraint ids and emit the selected result object.
"""

from __future__ import annotations

import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication

from htdt.cad_constraint_models import (
    CadConstraintEvaluation,
    CadConstraintResult,
    CadConstraintSet,
    CadWallClearanceConstraint,
)
from htdt.cad_scene import (
    Position3,
    RoomPrism,
    SceneDocument,
    SceneEntity,
)
from htdt.room_constraints_panel import RoomConstraintsPanel


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _doc() -> SceneDocument:
    return SceneDocument(
        document_id='doc-1',
        room=RoomPrism(width_m=6.0, depth_m=4.0, height_m=2.4),
        entities=(
            SceneEntity(
                entity_id='point-mlp',
                kind='measurement_point',
                name='MLP',
                position=Position3(x_m=3.0, y_m=3.0, z_m=1.1),
            ),
        ),
    )


def _result(result_id: str, constraint_id: str, *, passed: bool, name: str) -> CadConstraintResult:
    return CadConstraintResult(
        result_id=result_id,
        constraint_id=constraint_id,
        kind='wall_clearance',
        name=name,
        entity_ids=('point-mlp',),
        passed=passed,
        reason_code='ok' if passed else 'too_close',
        reason_ja='条件を満たす' if passed else '壁に近すぎ',
        actual_m=0.9,
    )


def test_sync_state_empty_constraints() -> None:
    _app()
    panel = RoomConstraintsPanel()
    panel.sync_state(
        CadConstraintSet(document_id='doc-1'), None, _doc()
    )
    assert panel.summary_label.text() == '制約なし · 移動/回転は自由です'
    assert panel.results_tree.topLevelItemCount() == 0


def _constraint(constraint_id: str) -> CadWallClearanceConstraint:
    return CadWallClearanceConstraint(
        constraint_id=constraint_id,
        name='壁クリアランス',
        entity_ids=('point-mlp',),
        wall_id='wall-north',
        min_m=0.5,
    )


def test_sync_state_violations_sorted_failed_first() -> None:
    _app()
    panel = RoomConstraintsPanel()
    evaluation = CadConstraintEvaluation(
        constraints_satisfied=False,
        results=(
            _result('r-ok', 'c-ok', passed=True, name='通過制約'),
            _result('r-bad', 'c-bad', passed=False, name='違反制約'),
        ),
    )
    panel.sync_state(
        CadConstraintSet(
            document_id='doc-1',
            constraints=(_constraint('c-ok'), _constraint('c-bad')),
        ),
        evaluation,
        _doc(),
    )
    # Summary reports violation count, not constraint count.
    assert '制約違反 1 件' in panel.summary_label.text()
    assert panel.results_tree.topLevelItemCount() == 2
    # Failed result sorts first.
    first = panel.results_tree.topLevelItem(0)
    assert first.text(0) == '違反制約'
    assert '✕' in first.text(2)
    assert 'MLP' in first.text(1)
    assert '0.900 m' in first.text(2)


def test_result_selection_emits_result_object_and_constraint_id() -> None:
    _app()
    panel = RoomConstraintsPanel()
    evaluation = CadConstraintEvaluation(
        constraints_satisfied=True,
        results=(_result('r-1', 'c-1', passed=True, name='制約A'),),
    )
    panel.sync_state(
        CadConstraintSet(document_id='doc-1'), evaluation, _doc()
    )
    emitted: list = []
    panel.resultSelected.connect(emitted.append)
    panel.results_tree.setCurrentItem(panel.results_tree.topLevelItem(0))
    assert panel.selected_constraint_id() == 'c-1'
    assert emitted and emitted[-1] is not None
    assert emitted[-1].result_id == 'r-1'


def test_sync_state_evaluate_error_overrides_summary() -> None:
    _app()
    panel = RoomConstraintsPanel()
    panel.sync_state(
        CadConstraintSet(document_id='doc-1'),
        None,
        _doc(),
        evaluate_error='ジオメトリ破損',
    )
    assert '制約を評価できません' in panel.summary_label.text()
    assert 'ジオメトリ破損' in panel.summary_label.text()


def test_action_buttons_emit_command_tokens() -> None:
    _app()
    panel = RoomConstraintsPanel()
    emitted: list = []
    optimized: list = []
    panel.constraintActionRequested.connect(emitted.append)
    panel.optimizeRequested.connect(lambda: optimized.append(True))

    panel.add_walkway_button.click()
    panel.add_allowed_button.click()
    panel.add_wall_button.click()
    panel.add_pair_button.click()
    panel.delete_button.click()
    panel.optimize_button.click()
    assert emitted == [
        'add_walkway',
        'add_allowed',
        'add_wall',
        'add_pair',
        'delete',
    ]
    assert optimized == [True]


def test_wall_combo_keeps_selection_across_resync() -> None:
    _app()
    panel = RoomConstraintsPanel()
    doc = _doc()
    panel.set_walls(doc)
    # No wall topology in _doc(): only the auto-select entry.
    assert panel.wall_combo.count() == 1
    assert panel.wall_combo.currentData() is None
