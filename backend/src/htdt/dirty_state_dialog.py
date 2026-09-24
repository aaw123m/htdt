"""Modal decision dialog for workspace dirty-state resolution (#610/#678).

``resolve_mount_dirty_state`` is the single entry the shell calls whenever a
deactivation guard blocks: it reads the mount's dirty state, presents the
state's explicit choices (Save / Discard / Recover Draft / keep / cancel),
executes the chosen resolution through the mount, and re-checks the
deactivation guard. The shell then either proceeds or stays put with the
mount's message — a failed resolution never loses the operator's context.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import QMessageBox, QWidget

from .workspace_dirty_state import (
    DeactivationContext,
    DirtyResolutionAction,
    dirty_state_prompt,
)

if TYPE_CHECKING:
    from .workflow_shell import WorkspaceMount


def resolve_mount_dirty_state(
    mount: "WorkspaceMount",
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
    for choice in prompt.choices:
        role = (
            QMessageBox.ButtonRole.DestructiveRole
            if choice.destructive
            else QMessageBox.ButtonRole.AcceptRole
        )
        button = box.addButton(choice.label, role)
        buttons[button] = choice.action
    box.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
    box.exec()
    return buttons.get(box.clickedButton())


def _apply(
    mount: "WorkspaceMount",
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
            f"処理を完了できませんでした · {error}",
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


__all__ = ["resolve_mount_dirty_state"]
