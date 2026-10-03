"""REV34: numbered journey guidance over the room workspace.

The evaluator must express the canonical first-build order (shape →
walls/openings → speakers/equipment → seats → acoustic surfaces) against
persisted state only — committed head revision plus document sidecars —
and the strip must route each numbered step at the context that owns it.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QApplication, QFrame

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import F1_DOCUMENT_ID, make_f1_scene
from htdt.room_journey import (
    current_journey_step,
    evaluate_room_journey,
)
from htdt.room_viewport import RoomOverlayState
from htdt.room_workspace import RoomWorkspace
from htdt.workflow_navigation import WorkspaceDeepLink, WorkspaceId


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _evaluate(**overrides):
    signals = {
        "room_saved": True,
        "vertex_count": 4,
        "wall_count": 0,
        "opening_count": 0,
        "speaker_count": 0,
        "unassigned_speaker_count": 0,
        "duplicate_role_count": 0,
        "equipment_count": 0,
        "seat_count": 0,
        "pose_count": 0,
        "material_count": 0,
        "treatment_count": 0,
        "prediction_count": 0,
    }
    signals.update(overrides)
    return evaluate_room_journey(**signals)


def _status(steps, key: str) -> str:
    return next(step.status for step in steps if step.key == key)


def _step(steps, key: str):
    return next(step for step in steps if step.key == key)


def test_unsaved_room_blocks_everything_downstream() -> None:
    steps = _evaluate(room_saved=False, vertex_count=0)
    assert _status(steps, "shape") == "current"
    assert current_journey_step(steps).key == "shape"
    for key in ("openings", "speakers", "seat", "acoustics"):
        step = _step(steps, key)
        assert step.status == "blocked"
        assert "保存" in step.detail


def test_fresh_room_guides_to_openings() -> None:
    steps = _evaluate()
    assert _status(steps, "shape") == "done"
    step = _step(steps, "openings")
    assert step.status == "current"
    assert step.context_id == "geometry"
    for key in ("speakers", "seat", "acoustics"):
        assert _status(steps, key) == "pending"


def test_walls_materialized_completes_openings() -> None:
    steps = _evaluate(wall_count=6, opening_count=2)
    step = _step(steps, "openings")
    assert step.status == "done"
    assert "壁 6 本" in step.detail
    assert "開口 2 件" in step.detail
    # No speakers yet — the step deep-links to the object palette context.
    speaker_step = _step(steps, "speakers")
    assert speaker_step.status == "current"
    assert speaker_step.context_id == "objects"


def test_walls_without_openings_still_complete() -> None:
    """A sealed theater legitimately never adds openings — done says so."""
    steps = _evaluate(wall_count=6, opening_count=0)
    step = _step(steps, "openings")
    assert step.status == "done"
    assert "開口なし" in step.detail


def test_unassigned_roles_keep_speakers_open() -> None:
    steps = _evaluate(
        wall_count=6, speaker_count=3, unassigned_speaker_count=1
    )
    step = _step(steps, "speakers")
    assert step.status == "current"
    assert "未設定" in step.detail
    # Speakers exist — the fix lives in the placement context.
    assert step.context_id == "placement"


def test_duplicate_roles_keep_speakers_open() -> None:
    steps = _evaluate(
        wall_count=6, speaker_count=3, duplicate_role_count=1
    )
    step = _step(steps, "speakers")
    assert step.status == "current"
    assert "役割" in step.detail


def test_ready_speakers_advance_to_seat() -> None:
    steps = _evaluate(wall_count=6, speaker_count=3, equipment_count=1)
    step = _step(steps, "speakers")
    assert step.status == "done"
    assert "スピーカー 3 本" in step.detail
    assert "機器 1 件" in step.detail
    seat = _step(steps, "seat")
    assert seat.status == "current"
    assert seat.context_id == "placement"


def test_seats_done_advance_to_acoustics() -> None:
    steps = _evaluate(
        wall_count=6, speaker_count=3, seat_count=2, pose_count=1
    )
    step = _step(steps, "seat")
    assert step.status == "done"
    assert "座席 2 席" in step.detail
    assert "リスナーポーズ 1 件" in step.detail
    acoustics = _step(steps, "acoustics")
    assert acoustics.status == "current"
    assert acoustics.context_id == "acoustics"


def test_materials_complete_the_journey() -> None:
    steps = _evaluate(
        wall_count=6,
        opening_count=1,
        speaker_count=3,
        seat_count=2,
        material_count=6,
        treatment_count=2,
        prediction_count=1,
    )
    assert all(step.status == "done" for step in steps)
    assert current_journey_step(steps) is None
    assert "面材質 6 面" in _step(steps, "acoustics").detail
    assert "処理 2 件" in _step(steps, "acoustics").detail


def test_out_of_order_done_states_stay_done() -> None:
    """Users can build out of order — done steps never un-complete."""
    steps = _evaluate(speaker_count=3, seat_count=1, material_count=4)
    assert _status(steps, "openings") == "current"
    assert _status(steps, "speakers") == "done"
    assert _status(steps, "seat") == "done"
    assert _status(steps, "acoustics") == "done"


class _FakeRoomViewport(QFrame):
    entitySelected = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.render_calls: list = []

    def render_document(
        self,
        document,
        *,
        selected_id: str | None,
        selected_ids=(),
        hidden_ids=frozenset(),
        locked_ids=frozenset(),
        overlays: RoomOverlayState = RoomOverlayState(),
        reset_camera: bool = False,
    ) -> None:
        self.render_calls.append((selected_id, overlays, reset_camera))

    def fit_scene(self) -> None:
        pass

    def focus_entity(self, entity_id: str) -> None:
        pass


def _f1_repository(tmp_path: Path) -> SceneRepository:
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(make_f1_scene(), parent_revision_id=None)
    return repository


def _workspace(tmp_path: Path, on_navigate=None) -> RoomWorkspace:
    return RoomWorkspace(
        _f1_repository(tmp_path),
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: _FakeRoomViewport(parent),
        on_navigate=on_navigate,
    )


def test_journey_strip_renders_and_routes_steps(tmp_path: Path) -> None:
    app = _app()
    workspace = _workspace(tmp_path)
    try:
        assert set(workspace._journey_buttons) == {
            "shape", "openings", "speakers", "seat", "acoustics",
        }
        # F1 fixture: room saved + speakers assigned, no walls/seat/materials.
        assert workspace.journey_progress.text() == "2/5"
        assert "壁と開口" in workspace.journey_hint.text()
        workspace._open_journey_step("seat")
        assert workspace.current_context == "placement"
        workspace._open_journey_step("openings")
        assert workspace.current_context == "geometry"
        workspace._open_journey_step("acoustics")
        assert workspace.current_context == "acoustics"
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()


def test_journey_steps_prefer_deep_links(tmp_path: Path) -> None:
    """Routed through the shell, step clicks keep the context bar in sync."""
    app = _app()
    targets: list[WorkspaceDeepLink] = []
    workspace = _workspace(tmp_path, on_navigate=targets.append)
    try:
        workspace._open_journey_step("seat")
        assert len(targets) == 1
        assert targets[0].workspace == WorkspaceId.ROOM
        assert targets[0].section == "placement"
        # The shell drives the actual context switch through its router —
        # the workspace does not jump ahead of it.
        assert workspace.current_context == "geometry"
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()


def test_journey_re_evaluates_on_save(tmp_path: Path) -> None:
    """Steps read persisted state: a draft seat does not count until save."""
    app = _app()
    workspace = _workspace(tmp_path)
    try:
        workspace.controller.add_object("seat")
        workspace.refresh()
        assert workspace.journey_progress.text() == "2/5"
        assert workspace.controller.save() is True
        workspace.refresh()
        assert workspace.journey_progress.text() == "3/5"
        # An unassigned speaker rolls the speakers step back to open.
        workspace.controller.add_object("speaker")
        workspace.controller.save()
        workspace.refresh()
        assert workspace.journey_progress.text() == "2/5"
        speakers_step = next(
            step for step in workspace._journey_steps if step.key == "speakers"
        )
        assert speakers_step.status == "pending"
        assert "未設定" in speakers_step.detail
    finally:
        workspace.close()
        workspace.deleteLater()
        app.processEvents()
