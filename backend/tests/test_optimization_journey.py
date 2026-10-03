"""REV33: numbered journey guidance over the optimization workspace.

The evaluator must express the canonical first-run order (scene → spec →
apply → evidence → conditions → review) against persisted state only — the
same fail-closed rules the controller enforces — and the strip must route
each numbered step at the page that owns it.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import F1_DOCUMENT_ID, make_f1_scene
from htdt.optimization_journey import (
    current_journey_step,
    evaluate_optimization_journey,
)
from htdt.optimization_workflow_workspace import OptimizationWorkflowWorkspace
from htdt.workflow_navigation import WorkspaceDeepLink, WorkspaceId


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


class _FakePlotter:
    def add_mesh(self, *_args, **_kwargs):
        return object()

    def remove_actor(self, *_args, **_kwargs) -> None:
        pass

    def add_text(self, *_args, **_kwargs) -> None:
        pass

    def render(self) -> None:
        pass


class _FakeViewport(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.plotter = _FakePlotter()

    def render_document(self, *_args, **_kwargs) -> None:
        pass


def _evaluate(**overrides):
    signals = {
        "scene_saved": True,
        "spec_count": 0,
        "spec_current_count": 0,
        "applied": False,
        "plan_count": 0,
        "plans_measured": 0,
        "campaign_count": 0,
        "evaluation_count": 0,
        "pareto_set_count": 0,
        "validation_count": 0,
    }
    signals.update(overrides)
    return evaluate_optimization_journey(**signals)


def _status(steps, key: str) -> str:
    return next(step.status for step in steps if step.key == key)


def _step(steps, key: str):
    return next(step for step in steps if step.key == key)


def test_unsaved_room_blocks_everything_downstream() -> None:
    steps = _evaluate(scene_saved=False)
    assert _status(steps, "scene") == "current"
    assert current_journey_step(steps).key == "scene"
    for key in ("spec", "apply", "evidence", "conditions", "review"):
        assert _status(steps, key) == "blocked"


def test_fresh_project_guides_to_spec() -> None:
    steps = _evaluate()
    assert _status(steps, "scene") == "done"
    assert _status(steps, "spec") == "current"
    assert _step(steps, "spec").context_id == "setup"
    for key in ("apply", "evidence", "conditions", "review"):
        assert _status(steps, key) == "pending"


def test_stale_only_specs_keep_spec_step_open() -> None:
    """A saved-but-stale spec cannot generate candidates — re-author first."""
    steps = _evaluate(spec_count=2, spec_current_count=0)
    step = _step(steps, "spec")
    assert step.status == "current"
    assert "再" in step.detail


def test_current_spec_advances_to_apply() -> None:
    steps = _evaluate(spec_count=1, spec_current_count=1)
    assert _status(steps, "spec") == "done"
    assert _status(steps, "apply") == "current"
    assert _step(steps, "apply").context_id == "candidates"


def test_applied_candidate_advances_to_evidence() -> None:
    steps = _evaluate(spec_count=1, spec_current_count=1, applied=True)
    assert _status(steps, "apply") == "done"
    assert _status(steps, "evidence") == "current"
    assert _step(steps, "evidence").context_id == "validation"


def test_unmeasured_plans_still_noted_once_bar_met() -> None:
    """One measured plan completes the step, but unmeasured plans surface."""
    steps = _evaluate(
        spec_count=1, spec_current_count=1, applied=True,
        plan_count=2, plans_measured=1,
    )
    step = _step(steps, "evidence")
    assert step.status == "done"
    assert "未計測 1 件" in step.detail


def test_measured_plans_advance_to_conditions() -> None:
    steps = _evaluate(
        spec_count=1, spec_current_count=1, applied=True,
        plan_count=1, plans_measured=1,
    )
    assert _status(steps, "evidence") == "done"
    assert _status(steps, "conditions") == "current"


def test_campaign_without_evaluations_guides_to_materialize() -> None:
    steps = _evaluate(
        spec_count=1, spec_current_count=1, applied=True,
        plan_count=1, plans_measured=1, campaign_count=1,
    )
    assert _status(steps, "conditions") == "done"
    assert "根拠データ" in _step(steps, "conditions").detail
    assert _status(steps, "review") == "current"


def test_evaluations_steer_review_to_comparison() -> None:
    steps = _evaluate(
        spec_count=1, spec_current_count=1, applied=True,
        plan_count=1, plans_measured=1, campaign_count=1,
        evaluation_count=3,
    )
    step = _step(steps, "review")
    assert step.status == "current"
    assert step.context_id == "comparison"
    assert "比較指標 3 件" in step.detail


def test_pareto_set_completes_review() -> None:
    steps = _evaluate(
        spec_count=1, spec_current_count=1, applied=True,
        plan_count=1, plans_measured=1, campaign_count=1,
        evaluation_count=3, pareto_set_count=1,
    )
    assert _status(steps, "review") == "done"
    assert current_journey_step(steps) is None


def test_saved_validation_completes_review() -> None:
    steps = _evaluate(
        spec_count=1, spec_current_count=1, applied=True,
        plan_count=1, plans_measured=1, campaign_count=1,
        evaluation_count=3, validation_count=1,
    )
    assert _status(steps, "review") == "done"
    assert all(step.status == "done" for step in steps)


def _workspace(tmp_path: Path, on_navigate=None) -> OptimizationWorkflowWorkspace:
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(make_f1_scene(), parent_revision_id=None)
    return OptimizationWorkflowWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: _FakeViewport(parent),
        on_navigate=on_navigate,
    )


def test_journey_strip_renders_and_routes_steps(tmp_path: Path) -> None:
    app = _app()
    workspace = _workspace(tmp_path)
    try:
        assert set(workspace._journey_buttons) == {
            "scene", "spec", "apply", "evidence", "conditions", "review",
        }
        # Saved room, nothing else → 1/6 with 探索設定 spotlighted.
        assert workspace.journey_progress.text() == "1/6"
        assert "探索設定" in workspace.journey_hint.text()
        workspace._open_journey_step("spec")
        assert workspace.current_page_id == "setup"
        workspace._open_journey_step("apply")
        assert workspace.current_page_id == "candidates"
        workspace._open_journey_step("conditions")
        assert workspace.current_page_id == "validation"
        workspace._open_journey_step("review")
        assert workspace.current_page_id == "comparison"
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()


def test_journey_context_steps_prefer_deep_links(tmp_path: Path) -> None:
    """Routed through the shell, step clicks keep the context bar in sync."""
    app = _app()
    targets: list[WorkspaceDeepLink] = []
    workspace = _workspace(tmp_path, on_navigate=targets.append)
    try:
        workspace._open_journey_step("apply")
        assert len(targets) == 1
        assert targets[0].workspace == WorkspaceId.OPTIMIZATION
        assert targets[0].section == "candidates"
        # The shell drives the actual page switch through its router —
        # the workspace does not jump ahead of it.
        assert workspace.current_page_id == "setup"
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()


def test_journey_scene_step_uses_on_navigate(tmp_path: Path) -> None:
    app = _app()
    targets: list[WorkspaceDeepLink] = []
    workspace = _workspace(tmp_path, on_navigate=targets.append)
    try:
        workspace._open_journey_step("scene")
        assert len(targets) == 1
        assert targets[0].workspace == WorkspaceId.ROOM
        assert targets[0].section == "geometry"
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()
