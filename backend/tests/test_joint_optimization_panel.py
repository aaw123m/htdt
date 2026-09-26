"""#945: joint optimization panel executes persisted specs (#945 regression:
the #524 surface stopped at spec authoring)."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from htdt.joint_optimization_context import JointOptimizationContext
from htdt.joint_optimization_panel import JointOptimizationPanel

from test_cad_joint_optimization import (  # noqa: E402  (shared fixtures)
    DOCUMENT_ID,
    _fixture,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _panel(fixture, tmp_path: Path, statuses: list[str]) -> JointOptimizationPanel:
    _app()
    context = JointOptimizationContext(
        fixture.scene_repository,
        DOCUMENT_ID,
        objective_repository=fixture.objective_repository,
    )
    return JointOptimizationPanel(
        context,
        on_status=statuses.append,
    )


def test_execute_button_runs_selected_spec(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    statuses: list[str] = []
    panel = _panel(fixture, tmp_path, statuses)
    baseline = panel.context.resolve_baseline()
    spec = panel.context.create_spec(
        baseline=baseline,
        mode='placement_only',
        dsp_variables=(),
        candidate_budget=8,
    )
    panel.refresh()

    assert panel.spec_tree.topLevelItemCount() == 1
    item = panel.spec_tree.topLevelItem(0)
    assert not panel.execute_button.isEnabled()

    item.setSelected(True)
    assert panel.execute_button.isEnabled()

    panel.execute_button.click()

    candidates = panel.context.joint_repository.list_candidates(spec.spec_id)
    assert len(candidates) == 2
    assert '実行完了' in panel.execution_label.text()
    assert any('ジョイント最適化を実行しました' in s for s in statuses)
    # Persisted state is reflected in the spec tree status column.
    assert '候補' in panel.spec_tree.topLevelItem(0).text(4)


def test_execute_disabled_when_spec_is_stale(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    statuses: list[str] = []
    panel = _panel(fixture, tmp_path, statuses)
    baseline = panel.context.resolve_baseline()
    spec = panel.context.create_spec(
        baseline=baseline,
        mode='placement_only',
        dsp_variables=(),
        candidate_budget=8,
    )

    moved = fixture.revision.document.model_copy(
        update={
            'room': fixture.revision.document.room.model_copy(
                update={'width_m': 7.0}
            )
        }
    )
    fixture.scene_repository.save(
        moved, parent_revision_id=fixture.revision.revision_id
    )
    panel.refresh()

    item = panel.spec_tree.topLevelItem(0)
    item.setSelected(True)
    assert not panel.execute_button.isEnabled()
    assert 'stale' in panel.spec_tree.topLevelItem(0).text(4)
    # The stale spec cannot have produced candidates.
    assert panel.context.joint_repository.list_candidates(spec.spec_id) == ()
