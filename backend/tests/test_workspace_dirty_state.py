from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PySide6.QtWidgets import QApplication, QLabel

from htdt.cad_repository import SceneRepository
from htdt.cad_scene import F1_DOCUMENT_ID, make_f1_scene
from htdt.room_workspace import RoomWorkspaceController
from htdt.workspace_dirty_state import dirty_state_prompt
from htdt.workflow_navigation import WorkspaceId
from htdt.workflow_shell import (
    WorkflowShellWindow,
    WorkspaceMount,
    build_canonical_workspace_registrations,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _registrations(factory):
    return build_canonical_workspace_registrations(
        {workspace_id: factory(workspace_id) for workspace_id in WorkspaceId}
    )


# --- prompt mapping ----------------------------------------------------------


@pytest.mark.parametrize(
    "state,expected",
    [
        ("dirty_recoverable", ("save", "discard", "keep_draft")),
        ("preview_active", ("commit_preview", "cancel_preview")),
        ("recovery_candidate_pending", ("recover_draft", "discard_recovery")),
    ],
)
def test_dirty_state_prompt_offers_explicit_choices(state, expected) -> None:
    for context in ("navigate", "exit", "project_switch", "dispose"):
        prompt = dirty_state_prompt(state, context)
        assert prompt is not None
        assert prompt.state == state
        assert prompt.context == context
        assert prompt.resolvable
        assert tuple(choice.action for choice in prompt.choices) == expected
        # every destructive transition is marked so the dialog can style it
        assert any(choice.destructive for choice in prompt.choices)


def test_pending_import_keep_only_for_navigate() -> None:
    """#796: Keep Draft is offered only where the mount survives.

    Navigation keeps the workspace mounted, so an acknowledged staged
    import can honestly stay. Exit / project switch / dispose may destroy
    the mount, and an unrecoverable staged import must not look kept.
    """

    navigate = dirty_state_prompt("pending_import", "navigate")
    assert navigate is not None
    assert tuple(choice.action for choice in navigate.choices) == (
        "keep_draft",
        "discard_pending",
    )
    for context in ("exit", "project_switch", "dispose"):
        prompt = dirty_state_prompt("pending_import", context)
        assert prompt is not None
        assert prompt.resolvable
        assert tuple(choice.action for choice in prompt.choices) == (
            "discard_pending",
        )
        assert prompt.choices[0].destructive
        assert "破棄" in prompt.message


def test_dirty_state_prompt_clean_and_busy() -> None:
    assert dirty_state_prompt("clean", "navigate") is None
    prompt = dirty_state_prompt("busy", "navigate")
    assert prompt is not None
    assert prompt.resolvable
    assert [choice.action for choice in prompt.choices] == ["stop_busy"]
    assert prompt.choices[0].destructive


# --- RoomWorkspaceController resolution --------------------------------------


def _controller(tmp_path):
    repository = SceneRepository(tmp_path / "scenes.sqlite3")
    repository.save(make_f1_scene(), parent_revision_id=None)
    controller = RoomWorkspaceController(repository, F1_DOCUMENT_ID)
    return repository, controller


def _dirty(controller: RoomWorkspaceController) -> None:
    controller.delete_entities(("speaker-fl",))


def test_dirty_save_resolution(tmp_path) -> None:
    repository, controller = _controller(tmp_path)
    _dirty(controller)
    assert controller.dirty_state() == "dirty_recoverable"
    allowed, _ = controller.before_deactivate()
    assert not allowed

    resolved, message = controller.resolve_dirty_state("save")
    assert resolved, message
    assert controller.dirty_state() == "clean"
    allowed, _ = controller.before_deactivate()
    assert allowed
    assert repository.recovery(F1_DOCUMENT_ID) is None


def test_dirty_discard_resolution_restores_saved_head(tmp_path) -> None:
    repository, controller = _controller(tmp_path)
    _dirty(controller)
    head = repository.latest(F1_DOCUMENT_ID)

    resolved, _ = controller.resolve_dirty_state("discard")
    assert resolved
    assert controller.dirty_state() == "clean"
    assert controller.committed_document == head.document
    assert repository.recovery(F1_DOCUMENT_ID) is None


def test_keep_draft_releases_then_reblocks_on_new_edit(tmp_path) -> None:
    repository, controller = _controller(tmp_path)
    _dirty(controller)

    resolved, _ = controller.resolve_dirty_state("keep_draft")
    assert resolved
    # The transition proceeds while the draft is persisted.
    allowed, _ = controller.before_deactivate()
    assert allowed
    assert controller.dirty_state() == "clean"
    assert repository.recovery(F1_DOCUMENT_ID) is not None
    # The working document still carries the unsaved edits.
    assert controller.is_dirty

    # A fresh edit invalidates the acknowledgement and blocks again.
    controller.delete_entities(("speaker-fr",))
    assert controller.dirty_state() == "dirty_recoverable"
    allowed, _ = controller.before_deactivate()
    assert not allowed


def test_recovery_candidate_resolution(tmp_path) -> None:
    repository, controller = _controller(tmp_path)
    _dirty(controller)
    controller.keep_draft()

    # Reopening the document surfaces the stored draft as a candidate.
    reopened = RoomWorkspaceController(repository, F1_DOCUMENT_ID)
    assert reopened.recovery_candidate is not None
    assert reopened.dirty_state() == "recovery_candidate_pending"
    allowed, _ = reopened.before_deactivate()
    assert not allowed

    resolved, message = reopened.resolve_dirty_state("recover_draft")
    assert resolved, message
    # Recovering the draft yields unsaved edits to decide about.
    assert reopened.dirty_state() == "dirty_recoverable"

    resolved, _ = reopened.resolve_dirty_state("discard")
    assert resolved
    assert reopened.dirty_state() == "clean"


def test_discard_recovery_resolution(tmp_path) -> None:
    repository, controller = _controller(tmp_path)
    _dirty(controller)
    controller.keep_draft()
    reopened = RoomWorkspaceController(repository, F1_DOCUMENT_ID)
    assert reopened.dirty_state() == "recovery_candidate_pending"

    resolved, _ = reopened.resolve_dirty_state("discard_recovery")
    assert resolved
    assert reopened.dirty_state() == "clean"
    assert repository.recovery("f1") is None


def test_unknown_resolution_action_is_rejected(tmp_path) -> None:
    _, controller = _controller(tmp_path)
    _dirty(controller)
    resolved, message = controller.resolve_dirty_state("bogus")  # type: ignore[arg-type]
    assert not resolved
    assert message


# --- router integration --------------------------------------------------------


def test_navigate_offers_resolution_instead_of_hard_block(monkeypatch) -> None:
    app = _app()
    state = {"dirty": True}
    resolved_actions: list[str] = []

    def factory(workspace_id: WorkspaceId):
        def build() -> WorkspaceMount:
            if workspace_id is not WorkspaceId.ROOM:
                return WorkspaceMount.from_widget(QLabel(workspace_id.value))

            def guard():
                return (
                    (False, "未保存の変更があります")
                    if state["dirty"]
                    else (True, None)
                )

            def dirty_state():
                return "dirty_recoverable" if state["dirty"] else "clean"

            def resolve(action):
                resolved_actions.append(action)
                if action == "save":
                    state["dirty"] = False
                    return True, "保存しました"
                return False, "残りました"

            return WorkspaceMount.from_widget(
                QLabel(workspace_id.value),
                before_deactivate=guard,
                dirty_state=dirty_state,
                resolve_dirty_state=resolve,
            )

        return build

    # The dialog is operator-facing; tests drive the resolution directly.
    import htdt.dirty_state_dialog as dialog

    def auto_save(mount, context, parent):
        assert context == "navigate"
        assert mount.dirty_state() == "dirty_recoverable"
        mount.resolve_dirty_state("save")
        return True

    monkeypatch.setattr(dialog, "resolve_mount_dirty_state", auto_save)

    window = WorkflowShellWindow(_registrations(factory))
    assert window.navigate(WorkspaceId.ROOM)

    assert window.navigate(WorkspaceId.MEASUREMENT)
    assert resolved_actions == ["save"]
    assert window.current_workspace_id is WorkspaceId.MEASUREMENT

    window.close()
    window.deleteLater()
    app.processEvents()


def test_navigate_still_blocks_when_operator_cancels(monkeypatch) -> None:
    app = _app()

    def factory(workspace_id: WorkspaceId):
        def build() -> WorkspaceMount:
            if workspace_id is not WorkspaceId.ROOM:
                return WorkspaceMount.from_widget(QLabel(workspace_id.value))
            return WorkspaceMount.from_widget(
                QLabel(workspace_id.value),
                before_deactivate=lambda: (False, "未保存の変更があります"),
                dirty_state=lambda: "dirty_recoverable",
                resolve_dirty_state=lambda action: (False, None),
            )

        return build

    import htdt.dirty_state_dialog as dialog

    monkeypatch.setattr(
        dialog, "resolve_mount_dirty_state", lambda mount, context, parent: False
    )

    window = WorkflowShellWindow(_registrations(factory))
    assert window.navigate(WorkspaceId.ROOM)
    assert window.navigate(WorkspaceId.MEASUREMENT) is False
    assert window.current_workspace_id is WorkspaceId.ROOM

    window.close()
    window.deleteLater()
    app.processEvents()


def test_dispose_all_offers_resolution_per_mount(monkeypatch) -> None:
    app = _app()
    state = {"dirty": True}

    def factory(workspace_id: WorkspaceId):
        def build() -> WorkspaceMount:
            if workspace_id is not WorkspaceId.MEASUREMENT:
                return WorkspaceMount.from_widget(QLabel(workspace_id.value))
            return WorkspaceMount.from_widget(
                QLabel(workspace_id.value),
                before_deactivate=lambda: (
                    (False, "取り込み途中です") if state["dirty"] else (True, None)
                ),
                dirty_state=lambda: (
                    "pending_import" if state["dirty"] else "clean"
                ),
                resolve_dirty_state=lambda action: (
                    (state.update(dirty=False) or (True, "破棄しました"))
                    if action == "discard_pending"
                    else (False, None)
                ),
            )

        return build

    import htdt.dirty_state_dialog as dialog

    def auto_discard(mount, context, parent):
        assert context == "dispose"
        mount.resolve_dirty_state("discard_pending")
        return True

    monkeypatch.setattr(dialog, "resolve_mount_dirty_state", auto_discard)

    window = WorkflowShellWindow(_registrations(factory))
    assert window.navigate(WorkspaceId.MEASUREMENT)
    # The non-current mount's blocked state must not silently block the
    # dispose once the operator resolves it.
    window.dispose_data_workspaces()
    assert state["dirty"] is False
    assert window.router.current_workspace_id is None

    window.deleteLater()
    app.processEvents()
