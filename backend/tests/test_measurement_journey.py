"""REV32: numbered journey guidance over the measurement workspace.

The evaluator must express the canonical first-run order (room → instrument
→ plan → run → evidence → review) against persisted state only — the same
fail-closed rules the controller enforces — and the strip must route each
numbered step at the page that owns it.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import make_f1_scene
from htdt.measurement_instrument_onboarding import InstrumentStep
from htdt.measurement_journey import (
    current_journey_step,
    evaluate_measurement_journey,
)
from htdt.measurement_page_workspace import MeasurementPageWorkspace
from htdt.measurement_workflow import MeasurementWorkflowController
from htdt.workflow_navigation import WorkspaceDeepLink, WorkspaceId


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _instrument(
    statuses: tuple[str, ...],
) -> tuple[InstrumentStep, ...]:
    return tuple(
        InstrumentStep(
            key=f"step-{index}",
            title=f"項目{index}",
            status=status,  # type: ignore[arg-type]
            detail="",
            link=None,
        )
        for index, status in enumerate(statuses)
    )


def _evaluate(**overrides):
    signals = {
        "scene_saved": True,
        "instrument_steps": _instrument(("ready",)),
        "plan_count": 0,
        "cells_completed": 0,
        "cells_remaining": 0,
        "measurement_count": 0,
        "staged_pending": False,
    }
    signals.update(overrides)
    return evaluate_measurement_journey(**signals)


def _status(steps, key: str) -> str:
    return next(step.status for step in steps if step.key == key)


def _step(steps, key: str):
    return next(step for step in steps if step.key == key)


def test_unsaved_room_blocks_everything_downstream() -> None:
    steps = _evaluate(scene_saved=False)
    assert _status(steps, "room") == "current"
    assert current_journey_step(steps).key == "room"
    for key in ("instrument", "plan", "run", "evidence", "review"):
        assert _status(steps, key) == "blocked"


def test_fresh_project_guides_to_instrument() -> None:
    steps = _evaluate(
        instrument_steps=_instrument(("action", "action", "manual"))
    )
    assert _status(steps, "room") == "done"
    assert _status(steps, "instrument") == "current"
    assert "要対応 2 項目" in _step(steps, "instrument").detail
    for key in ("plan", "run", "evidence", "review"):
        assert _status(steps, key) == "pending"


def test_manual_only_instrument_items_do_not_block() -> None:
    """Physical confirmations cannot be proven by records — tolerated."""
    steps = _evaluate(instrument_steps=_instrument(("ready", "manual")))
    assert _status(steps, "instrument") == "done"


def test_plan_exists_advances_to_run() -> None:
    steps = _evaluate(plan_count=2)
    assert _status(steps, "plan") == "done"
    assert _status(steps, "run") == "current"


def test_partial_run_keeps_run_step_current() -> None:
    steps = _evaluate(
        plan_count=1, cells_completed=2, cells_remaining=3,
        measurement_count=2,
    )
    step = _step(steps, "run")
    assert step.status == "current"
    assert "残り 3 セル" in step.detail


def test_completed_run_marks_run_done() -> None:
    steps = _evaluate(
        plan_count=1, cells_completed=4, cells_remaining=0,
        measurement_count=4,
    )
    assert _status(steps, "run") == "done"


def test_external_import_path_completes_run_without_cells() -> None:
    """REW-imported evidence without a runner plan is a legitimate path."""
    steps = _evaluate(measurement_count=3)
    assert _status(steps, "run") == "done"
    assert _status(steps, "evidence") == "done"


def test_staged_import_steers_evidence_step_to_assignment() -> None:
    steps = _evaluate(plan_count=1, staged_pending=True)
    step = _step(steps, "evidence")
    assert step.context_id == "assignment"
    assert "割り当て" in step.detail


def test_staged_import_keeps_evidence_step_open() -> None:
    """Committed evidence plus a still-staged import is unfinished work."""
    steps = _evaluate(
        plan_count=1, cells_completed=4, cells_remaining=0,
        measurement_count=4, staged_pending=True,
    )
    assert _status(steps, "evidence") == "current"


def test_fully_finished_journey_has_no_current() -> None:
    steps = _evaluate(
        plan_count=1, cells_completed=4, cells_remaining=0,
        measurement_count=4,
    )
    assert all(step.status == "done" for step in steps)
    assert current_journey_step(steps) is None


def _workspace(tmp_path: Path):
    scene_repository = SceneRepository(tmp_path / "cad.sqlite3")
    revision = scene_repository.save(
        make_f1_scene(), parent_revision_id=None
    ).revision
    controller = MeasurementWorkflowController(
        scene_repository, revision.document_id
    )
    return controller, scene_repository, revision


def test_journey_strip_opens_the_owning_context(tmp_path: Path) -> None:
    app = _app()
    controller, _, _ = _workspace(tmp_path)
    workspace = MeasurementPageWorkspace(controller)
    try:
        # Fresh project: instruments/preparation is the current step.
        assert {
            key for key in workspace._journey_buttons
        } == {"room", "instrument", "plan", "run", "evidence", "review"}
        workspace._open_journey_step("plan")
        assert workspace.current_context_id == "campaign"
        workspace._open_journey_step("evidence")
        # No staged import → the evidence step points at 読み込み.
        assert workspace.current_context_id == "import"
        workspace._open_journey_step("review")
        assert workspace.current_context_id == "quality"
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()


def test_journey_room_step_uses_on_navigate(tmp_path: Path) -> None:
    app = _app()
    controller, _, _ = _workspace(tmp_path)
    targets: list[WorkspaceDeepLink] = []
    workspace = MeasurementPageWorkspace(
        controller, on_navigate=targets.append
    )
    try:
        workspace._open_journey_step("room")
        assert len(targets) == 1
        assert targets[0].workspace == WorkspaceId.ROOM
        assert targets[0].section == "geometry"
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()


def test_journey_context_steps_prefer_deep_links(tmp_path: Path) -> None:
    """Routed through the shell, step clicks keep the context bar in sync."""
    app = _app()
    controller, _, _ = _workspace(tmp_path)
    targets: list[WorkspaceDeepLink] = []
    workspace = MeasurementPageWorkspace(
        controller, on_navigate=targets.append
    )
    try:
        workspace._open_journey_step("plan")
        assert len(targets) == 1
        assert targets[0].workspace == WorkspaceId.MEASUREMENT
        assert targets[0].section == "campaign"
        # The shell drives the actual context switch through its router —
        # the workspace does not jump ahead of it.
        assert workspace.current_context_id == "import"
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()
