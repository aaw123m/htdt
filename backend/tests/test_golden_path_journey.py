"""REV64 #804 Gate B: whole-project golden-path journey guidance.

The Overview must show the operator where they are in the full flow —
部屋 → 機材 → 測定 → 予測 → 比較 → 適用 → 再測定 → 出力 — derived from the
same persisted state the readiness service already aggregates. The strip is
guidance, never a gate: exactly one step is 'current', steps are clickable
navigation targets, and nothing downstream of a missing scene can silently
look 'done'.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from htdt.cad_repository import SceneRevision
from htdt.golden_path_journey import evaluate_golden_path_journey
from htdt.overview_readiness import OverviewReadinessService
from htdt.overview_workspace import OverviewWorkspace
from htdt.workflow_navigation import WorkspaceId


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _evaluate(**overrides):
    defaults = dict(
        scene_saved=True,
        room_complete=True,
        speakers_present=True,
        speaker_roles_ok=True,
        measurement_count=0,
        has_current_prediction=False,
        has_candidates=False,
        variant_stages=frozenset(),
    )
    return evaluate_golden_path_journey(**(defaults | overrides))


def test_empty_project_points_at_room_creation() -> None:
    steps = _evaluate(
        scene_saved=False,
        room_complete=False,
        speakers_present=False,
        speaker_roles_ok=False,
    )
    assert len(steps) == 8
    assert [step.key for step in steps] == [
        'room', 'equipment', 'measurement', 'prediction',
        'comparison', 'apply', 'verify', 'export',
    ]
    assert steps[0].status == 'current'
    assert steps[0].target is not None
    assert steps[0].target.workspace == WorkspaceId.ROOM
    # Downstream of a missing scene can never look done or current.
    assert all(step.status == 'blocked' for step in steps[1:])


def test_exactly_one_current_step_and_monotone_progress() -> None:
    steps = _evaluate()
    statuses = [step.status for step in steps]
    assert statuses[0] == 'done'  # room
    assert statuses[1] == 'done'  # equipment
    assert statuses[2] == 'current'  # measurement — first undone step
    assert statuses[3:] == ['pending'] * 5


def test_every_step_is_a_navigation_target() -> None:
    for step in _evaluate():
        assert step.target is not None, step.key
        assert step.target.as_uri().startswith('htdt://')


def test_mid_journey_state_marks_apply_current() -> None:
    steps = _evaluate(
        measurement_count=3,
        has_current_prediction=True,
        has_candidates=True,
    )
    assert [s.status for s in steps] == [
        'done', 'done', 'done', 'done', 'done', 'current', 'pending', 'pending',
    ]
    apply_step = steps[5]
    assert apply_step.key == 'apply'
    assert apply_step.target.workspace == WorkspaceId.OPTIMIZATION


def test_proposal_is_not_an_applied_change() -> None:
    steps = _evaluate(
        measurement_count=1,
        has_current_prediction=True,
        has_candidates=True,
        variant_stages=frozenset({'proposed'}),
    )
    assert steps[5].key == 'apply'
    assert steps[5].status == 'current'
    assert steps[6].status == 'pending'


def test_applied_variant_requires_remeasurement() -> None:
    steps = _evaluate(
        measurement_count=2,
        has_current_prediction=True,
        has_candidates=True,
        variant_stages=frozenset({'applied'}),
    )
    assert steps[5].status == 'done'
    assert steps[6].key == 'verify'
    assert steps[6].status == 'current'
    assert steps[6].target.workspace == WorkspaceId.MEASUREMENT


def test_fully_verified_journey_is_all_done() -> None:
    steps = _evaluate(
        measurement_count=4,
        has_current_prediction=True,
        has_candidates=True,
        variant_stages=frozenset({'measured_validated'}),
    )
    assert all(step.status == 'done' for step in steps)


# --- Overview wiring: the view model always carries the strip -----------


class _SceneSource:
    def __init__(self, revision: SceneRevision | None) -> None:
        self.revision = revision

    def current_head(self, document_id: str) -> SceneRevision | None:
        return self.revision


class _EmptySource:
    def list_measurements(self, document_id: str) -> tuple:
        return ()

    def dataset_for_measurement(self, measurement_id: str):
        return None

    def list_results(self, document_id: str) -> tuple:
        return ()

    def list_specs(self, document_id: str) -> tuple:
        return ()

    def inspect_for_search_spec(self, search_spec_id: str) -> tuple:
        return ()

    def latest_report(self, measurement_id: str):
        return None


def _revision() -> SceneRevision:
    document = SimpleNamespace(
        room=SimpleNamespace(room_id='room'),
        entities=(
            SimpleNamespace(
                entity_id='speaker-fl', kind='speaker', speaker_role='FL'
            ),
        ),
    )
    return SceneRevision(
        revision_id='revision-current',
        document_id='project-1',
        parent_revision_id=None,
        created_at_utc='2026-10-06T00:00:00+00:00',
        content_hash='a' * 64,
        document=document,
    )


def _service(revision: SceneRevision | None) -> OverviewReadinessService:
    empty = _EmptySource()
    return OverviewReadinessService(
        _SceneSource(revision),
        empty,
        empty,
        empty,
        empty,
        empty,
    )


def test_overview_without_scene_shows_journey_pointing_at_room() -> None:
    view = _service(None).read('project-1')
    steps = view.golden_path_steps
    assert len(steps) == 8
    assert steps[0].key == 'room'
    assert steps[0].status == 'current'
    assert all(step.status == 'blocked' for step in steps[1:])


def test_overview_with_empty_scene_starts_at_measurement() -> None:
    view = _service(_revision()).read('project-1')
    steps = {step.key: step.status for step in view.golden_path_steps}
    assert steps['room'] == 'done'
    assert steps['equipment'] == 'done'
    assert steps['measurement'] == 'current'
    assert steps['export'] == 'pending'


def test_overview_workspace_renders_strip_and_routes_steps() -> None:
    """The strip is visible, numbered, and every step navigates."""
    _app()
    navigated: list = []
    workspace = OverviewWorkspace(
        _service(_revision()),
        'project-1',
        navigate=lambda target: navigated.append(target) or True,
    )
    try:
        progress = workspace.findChild(QLabel, 'overviewJourneyProgress')
        assert progress is not None
        assert progress.text() == '使い方の流れ 2/8'
        buttons = [
            workspace.findChild(QPushButton, f'overviewJourneyStep_{key}')
            for key in (
                'room', 'equipment', 'measurement', 'prediction',
                'comparison', 'apply', 'verify', 'export',
            )
        ]
        assert all(button is not None for button in buttons)
        # Current step (measurement) navigates to the measurement workspace.
        buttons[2].click()
        assert len(navigated) == 1
        assert navigated[0].workspace == WorkspaceId.MEASUREMENT
        # A done step still navigates — guidance, never a gate.
        buttons[0].click()
        assert navigated[1].workspace == WorkspaceId.ROOM
    finally:
        workspace.deleteLater()
