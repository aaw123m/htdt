"""Modal decision dialog for workspace dirty-state resolution (#610/#678).

``resolve_mount_dirty_state`` is the single entry the shell calls whenever a
deactivation guard blocks: it reads the mount's dirty state, presents the
state's explicit choices (Save / Discard / Recover Draft / keep / cancel),
executes the chosen resolution through the mount, and re-checks the
deactivation guard. The shell then either proceeds or stays put with the
mount's message — a failed resolution never loses the operator's context.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal, Protocol

from PySide6.QtWidgets import QMessageBox, QWidget

from .user_facing_error import operation_error_message
from .workspace_dirty_state import (
    DeactivationContext,
    DirtyResolutionAction,
    WorkspaceDirtyState,
    dirty_state_prompt,
)


class _MountLike(Protocol):
    """Structural surface ``resolve_mount_dirty_state`` needs from a mount.

    ``workflow_shell.WorkspaceMount`` satisfies this shape; the dialog takes
    the protocol so the shell->dialog import is the only edge of the pair
    (#807: breaks the dirty_state_dialog <-> workflow_shell cycle).
    """

    dirty_state: Callable[[], WorkspaceDirtyState] | None
    resolve_dirty_state: (
        Callable[[DirtyResolutionAction], tuple[bool, str | None]] | None
    )
    before_deactivate: Callable[[], tuple[bool, str | None]] | None


def resolve_mount_dirty_state(
    mount: _MountLike,
    context: DeactivationContext,
    parent: QWidget | None,
) -> bool:
    """Offer explicit resolution for a blocked mount.

    Returns ``True`` only when the mount's ``before_deactivate`` guard now
    allows deactivation. Mounts without dirty-state hooks, a ``clean`` or
    unresolvable (``busy``) state, and the operator choosing cancel all
    return ``False``.
    """

    if mount.dirty_state is None or mount.resolve_dirty_state is None:
        return False
    prompt = dirty_state_prompt(mount.dirty_state(), context)
    if prompt is None:
        return False
    if not prompt.resolvable:
        QMessageBox.information(parent, prompt.title, prompt.message)
        return False
    action = _choose(prompt, parent)
    if action is None:
        return False
    return _apply(mount, action, prompt.title, parent)


def _choose(prompt, parent: QWidget | None) -> DirtyResolutionAction | None:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(prompt.title)
    box.setText(prompt.message)
    box.setStandardButtons(QMessageBox.StandardButton.NoButton)
    buttons: dict[object, DirtyResolutionAction] = {}
    default_button = None
    for choice in prompt.choices:
        role = (
            QMessageBox.ButtonRole.DestructiveRole
            if choice.destructive
            else QMessageBox.ButtonRole.AcceptRole
        )
        button = box.addButton(choice.label, role)
        buttons[button] = choice.action
        if default_button is None and not choice.destructive:
            default_button = button
    cancel_button = box.addButton(
        "キャンセル", QMessageBox.ButtonRole.RejectRole
    )
    # Enter must never pick a destructive resolution: default to the first
    # non-destructive choice (save/keep/open), or Cancel when every choice
    # is destructive (e.g. busy workspaces offering only stop_busy).
    box.setDefaultButton(default_button or cancel_button)
    box.exec()
    return buttons.get(box.clickedButton())


def _apply(
    mount: _MountLike,
    action: DirtyResolutionAction,
    title: str,
    parent: QWidget | None,
) -> bool:
    try:
        resolved, message = mount.resolve_dirty_state(action)
    except Exception as error:  # resolution must never lose the context
        QMessageBox.warning(
            parent,
            title,
            f"処理を完了できませんでした · {operation_error_message(error)}",
        )
        return False
    if not resolved:
        QMessageBox.warning(
            parent,
            title,
            message or "処理を完了できませんでした",
        )
        return False
    if mount.before_deactivate is None:
        return True
    allowed, _reason = mount.before_deactivate()
    # The caller surfaces the remaining block through its status message.
    return allowed


def choose_snapshot_action(
    action_label: str,
    parent: QWidget | None,
) -> Literal["save", "last_saved"] | None:
    """Pick which project generation an export/duplicate serializes.

    #918/#927: when mounted workspaces hold unsaved state, the operator
    explicitly chooses between saving first and serializing the last
    persisted state. ``last_saved`` never mutates the working copy, and
    cancelling must never produce an artifact.
    """

    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(f"未保存の変更 — {action_label}")
    box.setText(
        "現在のプロジェクトには未保存の変更があります。\n"
        f"{action_label}する対象の状態を選んでください。"
    )
    save_button = box.addButton(
        f"保存して{action_label}", QMessageBox.ButtonRole.AcceptRole
    )
    last_saved_button = box.addButton(
        f"保存済みの状態を{action_label}", QMessageBox.ButtonRole.DestructiveRole
    )
    box.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
    # Enter saves first — the non-destructive choice; Esc/Cancel aborts and
    # never produces an artifact.
    box.setDefaultButton(save_button)
    box.exec()
    clicked = box.clickedButton()
    if clicked is last_saved_button:
        return "last_saved"
    if clicked is save_button:
        return "save"
    return None


__all__ = ["choose_snapshot_action", "resolve_mount_dirty_state"]
