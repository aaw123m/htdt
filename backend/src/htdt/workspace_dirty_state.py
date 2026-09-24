"""Dirty-state resolution contract for workspace deactivation (#610/#678).

Instead of hard-blocking navigation/exit/project-switch on unsaved state,
each workspace mount exposes its current dirty-state classification plus a
resolver that performs the operator's explicit choice. The shell presents
the state's choices and re-checks the deactivation guard afterwards:

- ``dirty_recoverable`` — unsaved edits: save, discard, or keep as a
  restorable recovery draft (discard is destructive).
- ``preview_active`` — an in-flight preview must be explicitly committed
  or cancelled; it is never silently committed.
- ``recovery_candidate_pending`` — a stored draft must be recovered or
  discarded.
- ``pending_import`` — a staged (uncommitted) measurement import must be
  kept or discarded.
- ``busy`` — background work blocks unconditionally; only cancel applies.

A failed action (e.g. save failure) keeps the current context: the router
stays on the blocked workspace and surfaces the resolver's message.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

WorkspaceDirtyState: TypeAlias = Literal[
    "clean",
    "preview_active",
    "dirty_recoverable",
    "recovery_candidate_pending",
    "pending_import",
    "busy",
]

DirtyResolutionAction: TypeAlias = Literal[
    "save",
    "discard",
    "keep_draft",
    "commit_preview",
    "cancel_preview",
    "recover_draft",
    "discard_recovery",
    "discard_pending",
]

DeactivationContext: TypeAlias = Literal[
    "navigate",
    "exit",
    "project_switch",
    "dispose",
]


@dataclass(frozen=True, slots=True)
class DirtyStateChoice:
    """One explicit resolution offered for a dirty state."""

    action: DirtyResolutionAction
    label: str
    destructive: bool = False


@dataclass(frozen=True, slots=True)
class DirtyStatePrompt:
    """What the shell should ask the operator for a blocked deactivation."""

    state: WorkspaceDirtyState
    context: DeactivationContext
    title: str
    message: str
    choices: tuple[DirtyStateChoice, ...]
    resolvable: bool = True


_CONTEXT_PREFIX: dict[DeactivationContext, str] = {
    "navigate": "画面を切り替える前に",
    "exit": "終了する前に",
    "project_switch": "プロジェクトを切り替える前に",
    "dispose": "データ領域を再読み込みする前に",
}

_CONTEXT_TITLE: dict[DeactivationContext, str] = {
    "navigate": "画面の切り替え",
    "exit": "アプリケーションの終了",
    "project_switch": "プロジェクトの切り替え",
    "dispose": "データの再読み込み",
}


def dirty_state_prompt(
    state: WorkspaceDirtyState,
    context: DeactivationContext,
) -> DirtyStatePrompt | None:
    """Build the operator prompt for a dirty state, or ``None`` when clean."""

    if state == "clean":
        return None
    prefix = _CONTEXT_PREFIX[context]
    title = _CONTEXT_TITLE[context]
    if state == "preview_active":
        return DirtyStatePrompt(
            state=state,
            context=context,
            title=title,
            message=f"{prefix}、操作中のプレビューを確定またはキャンセルしてください。",
            choices=(
                DirtyStateChoice("commit_preview", "プレビューを確定して続行"),
                DirtyStateChoice("cancel_preview", "プレビューを破棄して続行", destructive=True),
            ),
        )
    if state == "dirty_recoverable":
        return DirtyStatePrompt(
            state=state,
            context=context,
            title=title,
            message=f"{prefix}、未保存の変更をどうするか選択してください。",
            choices=(
                DirtyStateChoice("save", "保存して続行"),
                DirtyStateChoice("discard", "変更を破棄して続行", destructive=True),
                DirtyStateChoice("keep_draft", "下書きとして残して続行"),
            ),
        )
    if state == "recovery_candidate_pending":
        return DirtyStatePrompt(
            state=state,
            context=context,
            title=title,
            message=f"{prefix}、復旧可能な下書きを処理してください。",
            choices=(
                DirtyStateChoice("recover_draft", "下書きを復旧して続行"),
                DirtyStateChoice("discard_recovery", "復旧データを破棄して続行", destructive=True),
            ),
        )
    if state == "pending_import":
        if context == "navigate":
            # Same-shell navigation keeps the mounted workspace alive, so an
            # exact-identity acknowledgement is sufficient here (#796). For
            # every other context the mount may be destroyed, and a kept
            # staged import is only honest if it is durably recoverable —
            # until staged-import drafts persist, Keep is not offered.
            choices: tuple[DirtyStateChoice, ...] = (
                DirtyStateChoice("keep_draft", "そのまま残して続行"),
                DirtyStateChoice("discard_pending", "取り込みを破棄して続行", destructive=True),
            )
            message = f"{prefix}、取り込み途中の測定データをどうするか選択してください。"
        else:
            choices = (
                DirtyStateChoice("discard_pending", "取り込みを破棄して続行", destructive=True),
            )
            message = (
                f"{prefix}、取り込み途中の測定データは保存できません。"
                "取り込みを完了させるか、破棄してください。"
            )
        return DirtyStatePrompt(
            state=state,
            context=context,
            title=title,
            message=message,
            choices=choices,
        )
    return DirtyStatePrompt(
        state="busy",
        context=context,
        title=title,
        message=f"{prefix}、実行中の処理が完了するのを待ってください。",
        choices=(),
        resolvable=False,
    )


__all__ = [
    "DeactivationContext",
    "DirtyResolutionAction",
    "DirtyStateChoice",
    "DirtyStatePrompt",
    "WorkspaceDirtyState",
    "dirty_state_prompt",
]
