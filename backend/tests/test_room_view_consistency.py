"""REV16-CONSIST: multi-view agreement regression tests.

The same entity/value must read identically in every view that shows it:
the inspector, the objects tree, the placement dock's video bindings, the
history detail pane, and the persisted sidecar authorities (listener pose /
screen transfer / video workspace) must never present or write a stale
snapshot of a renamed, edited, or deleted entity.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QApplication, QFrame

from htdt.cad_document import EditStateError
from htdt.cad_listener_pose import build_listener_pose
from htdt.cad_repository import SceneRepository
from htdt.cad_scene import F1_DOCUMENT_ID, Offset3, make_f1_scene
from htdt.cad_video_workspace import VideoGeometryWorkspace
from htdt.room_workspace import RoomWorkspace, RoomWorkspaceController


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _f1_repository(tmp_path) -> SceneRepository:
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(make_f1_scene(), parent_revision_id=None)
    return repository


class FakeRoomViewport(QFrame):
    entitySelected = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.render_calls: list[tuple] = []
        self.focused: list[str] = []
        self.fit_count = 0

    def render_document(
        self,
        document,
        *,
        selected_id=None,
        selected_ids=(),
        hidden_ids=frozenset(),
        locked_ids=frozenset(),
        overlays=None,
        reset_camera=False,
    ) -> None:
        self.render_calls.append((selected_id, overlays, reset_camera))

    def fit_scene(self) -> None:
        self.fit_count += 1

    def focus_entity(self, entity_id) -> None:
        self.focused.append(entity_id)


def _workspace(tmp_path):
    app = _app()
    repository = _f1_repository(tmp_path)
    workspace = RoomWorkspace(
        repository,
        F1_DOCUMENT_ID,
        viewport_factory=lambda parent: FakeRoomViewport(parent),
    )
    workspace.resize(1100, 700)
    workspace.show()
    app.processEvents()
    return app, workspace


def _close(app, workspace) -> None:
    workspace.close()
    workspace.deleteLater()
    app.processEvents()


def test_inspector_orientation_field_resyncs_after_commit_and_undo(tmp_path) -> None:
    """A committed orientation edit must clear the field's dirty mark so a
    later undo/align actually repaints the spin — otherwise the inspector
    keeps showing (and recommitting) the stale angle."""
    app, workspace = _workspace(tmp_path)
    try:
        controller = workspace.controller
        inspector = workspace.inspector
        controller.set_selection("speaker-fl")
        app.processEvents()
        heading = inspector.orientation_fields["heading"]
        original = heading.value()
        target = original + 30.0 if original < 150.0 else original - 30.0
        heading.setValue(target)
        assert heading in inspector._dirty_widgets
        workspace._commit_inspector()
        app.processEvents()
        # After a successful commit the widget matches the fresh baseline —
        # the in-progress-edit mark must clear like every other field.
        assert heading not in inspector._dirty_widgets
        assert workspace.undo()
        app.processEvents()
        assert abs(heading.value() - original) <= 1e-6
    finally:
        _close(app, workspace)


def test_restore_revision_requires_clean_sidecars(tmp_path) -> None:
    """Restoring a revision rebinds the scene document — the dirty gate must
    cover the sidecar stores too, or unsaved video/pose edits would sit on
    top of a rolled-back scene."""
    repository = _f1_repository(tmp_path)
    controller = RoomWorkspaceController(repository, F1_DOCUMENT_ID)
    head_a = repository.current_head(F1_DOCUMENT_ID)
    controller.set_selection("speaker-fl")
    entity = controller.document.entity("speaker-fl")
    controller.update_selected(
        name="Front Left renamed",
        position=entity.position,
        size_m=entity.size_m,
        speaker_role=entity.speaker_role,
    )
    controller.save()
    head_b = repository.current_head(F1_DOCUMENT_ID)
    assert head_b.revision_id != head_a.revision_id
    assert not controller.is_dirty
    controller.save_video_workspace(
        VideoGeometryWorkspace(
            document_id=F1_DOCUMENT_ID,
            projector_entity_id="projector-unsaved",
        )
    )
    assert controller.is_dirty
    assert not controller.working.is_dirty
    with pytest.raises(EditStateError):
        controller.restore_revision(head_a.revision_id)
    # A clean bind (baseline captured with the sidecar already persisted)
    # must still be allowed to restore.
    reopened = RoomWorkspaceController(repository, F1_DOCUMENT_ID)
    assert not reopened.is_dirty
    restored = reopened.restore_revision(head_a.revision_id)
    assert restored.revision_id != head_b.revision_id


def test_video_panel_tracks_deletes_in_placement_context(tmp_path) -> None:
    """Deleting an entity while the placement dock is open must drop its
    video-binding row — the panel can otherwise keep writing bindings for a
    screen/seat the document no longer contains."""
    app, workspace = _workspace(tmp_path)
    try:
        workspace.set_context("placement")
        app.processEvents()
        seat = workspace.controller.add_object("seat")
        screen = workspace.controller.add_object("screen")
        workspace._refresh()
        app.processEvents()
        panel = workspace.video_panel
        assert seat.entity_id in panel._seat_widgets
        assert panel._screen_entity_id == screen.entity_id
        workspace.delete_selection()  # screen is the current selection
        app.processEvents()
        assert panel._screen_entity_id is None
        assert "バインド未設定" in panel.screen_heading.text()
        workspace.controller.set_selection(seat.entity_id)
        workspace.delete_selection()
        app.processEvents()
        assert seat.entity_id not in panel._seat_widgets
    finally:
        _close(app, workspace)


def test_video_panel_seat_card_follows_entity_rename(tmp_path) -> None:
    """A rename keeps the entity id — the seat card/ combo must rebuild on
    the (id, name) signature, not the id list alone."""
    app, workspace = _workspace(tmp_path)
    try:
        workspace.set_context("placement")
        app.processEvents()
        seat = workspace.controller.add_object("seat")
        workspace._refresh()
        app.processEvents()
        combo = workspace.video_panel.seat_view_combo
        assert combo.count() == 1
        assert combo.itemText(0) == "座席"
        workspace.controller.set_selection(seat.entity_id)
        entity = workspace.controller.document.entity(seat.entity_id)
        workspace.controller.update_selected(
            name="リヤ左",
            position=entity.position,
            size_m=entity.size_m,
            speaker_role=entity.speaker_role,
        )
        workspace._refresh()
        app.processEvents()
        assert workspace.video_panel.seat_view_combo.itemText(0) == "リヤ左"
    finally:
        _close(app, workspace)


def test_stale_seat_pose_write_is_guarded(tmp_path) -> None:
    """A pose write for a seat deleted from the draft must be a no-op even
    while the saved head still contains the seat (the repository validates
    against head, the workspace must guard against the draft)."""
    app, workspace = _workspace(tmp_path)
    try:
        controller = workspace.controller
        seat = controller.add_object("seat")
        controller.save()
        assert controller.delete_entities((seat.entity_id,))
        pose = build_listener_pose(
            seat_entity_id=seat.entity_id,
            label="P",
            head_center_offset_local_m=Offset3(z_m=1.15),
            eye_reference_offset_local_m=Offset3(z_m=1.10),
            acoustic_reference_offset_local_m=Offset3(z_m=0.65),
            provenance="test",
        )
        workspace.listener_pose_repository.save_pose(pose)
        workspace._seat_pose_changed(seat.entity_id, pose.pose_id)
        selections = workspace.listener_pose_repository.selections_for_document(
            controller.document_id
        )
        assert seat.entity_id not in selections
    finally:
        _close(app, workspace)


def test_history_detail_recomputes_open_diff_on_refresh(tmp_path) -> None:
    """With a revision selected, a refresh must recompute its diff against
    the current HEAD — not reset the detail pane to the generic summary."""
    app, workspace = _workspace(tmp_path)
    try:
        controller = workspace.controller
        controller.set_selection("speaker-fl")
        entity = controller.document.entity("speaker-fl")
        controller.update_selected(
            name="Front Left renamed",
            position=entity.position,
            size_m=entity.size_m,
            speaker_role=entity.speaker_role,
        )
        controller.save()
        workspace.set_context("history")
        app.processEvents()
        head = controller.repository.current_head(controller.document_id)
        older = next(
            revision
            for revision in controller.list_revisions()
            if revision.revision_id != head.revision_id
        )
        assert workspace.history_panel.select_revision(older.revision_id)
        app.processEvents()
        assert "差分" in workspace.history_panel.detail.toPlainText()
        # A mutation + refresh in history context must keep the live diff.
        controller.set_selection("speaker-c")
        entity = controller.document.entity("speaker-c")
        controller.update_selected(
            name="Center renamed",
            position=entity.position,
            size_m=entity.size_m,
            speaker_role=entity.speaker_role,
        )
        workspace._refresh()
        app.processEvents()
        assert "差分" in workspace.history_panel.detail.toPlainText()
    finally:
        _close(app, workspace)
