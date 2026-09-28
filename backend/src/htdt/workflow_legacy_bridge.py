from __future__ import annotations

from typing import Any

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QDockWidget

from .workflow_navigation import WorkspaceId


def legacy_editor_deactivation_guard(window: Any) -> tuple[bool, str | None]:
    """Fail closed while the temporary legacy workspace bridge owns mutable state.

    Each legacy QMainWindow still owns a WorkingDocument. Until UX120-UX140 replace
    those windows with shared-document workspace views, navigation is blocked while
    a draft, preview, recovery decision, or background worker is live. This prevents
    two mounted workspaces from silently saving divergent descendants of one scene
    revision.
    """

    working = getattr(window, "working", None)
    if working is not None:
        if bool(getattr(working, "has_preview", False)):
            return False, "編集中の操作を確定または取り消してから画面を切り替えてください"
        if bool(getattr(working, "is_dirty", False)):
            return False, "未保存の変更を保存または元に戻してから画面を切り替えてください"

    if getattr(window, "recovery_candidate", None) is not None:
        return False, "復旧データを復元または破棄してから画面を切り替えてください"

    find_children = getattr(window, "findChildren", None)
    if callable(find_children):
        try:
            if any(thread.isRunning() for thread in find_children(QThread)):
                return False, "処理の完了またはキャンセル後に画面を切り替えてください"
        except RuntimeError:
            return False, "現在の処理状態を確認してから画面を切り替えてください"

    return True, None


def refresh_legacy_editor_revision(window: Any) -> bool:
    """Reload a clean legacy editor when another workspace saved a newer revision."""

    working = getattr(window, "working", None)
    repository = getattr(window, "repository", None)
    document_id = getattr(window, "document_id", None)
    if working is None or repository is None or document_id is None:
        return False

    latest = repository.current_head(document_id)
    if latest is None or latest.revision_id == getattr(working, "source_revision_id", None):
        return False

    allowed, reason = legacy_editor_deactivation_guard(window)
    if not allowed:
        raise RuntimeError(reason or "stale workspace cannot be reloaded safely")

    reload_method = getattr(window, "_load_or_seed", None)
    if not callable(reload_method):
        raise RuntimeError("legacy workspace cannot reload the current SceneRevision")
    reload_method()
    return True


_CONTEXT_DOCK_TITLES: dict[WorkspaceId, dict[str, tuple[str, ...]]] = {
    WorkspaceId.ROOM: {
        "geometry": ("部屋", "壁・開口"),
        "objects": ("オブジェクト詳細", "インスペクター"),
        "placement": ("オブジェクト詳細", "制約", "インスペクター"),
        "acoustics": ("予測", "制約"),
    },
    WorkspaceId.MEASUREMENT: {
        "import": ("実測",),
        "assignment": ("実測",),
        "quality": ("実測",),
        "comparison": ("実測",),
    },
    WorkspaceId.OPTIMIZATION: {
        "setup": ("最適化", "制約"),
        "candidates": ("最適化",),
        "objectives": ("最適化",),
        "measurement-plan": ("最適化", "実測"),
        "validation": ("最適化",),
    },
}


def raise_legacy_context(window: Any, workspace: WorkspaceId, context_id: str) -> None:
    """Best-effort context projection for the temporary legacy-window bridge."""

    titles = _CONTEXT_DOCK_TITLES.get(workspace, {}).get(context_id, ())
    if not titles:
        return
    docks = {
        dock.windowTitle(): dock
        for dock in window.findChildren(QDockWidget)
    }
    for title in titles:
        dock = docks.get(title)
        if dock is not None:
            dock.show()
            dock.raise_()
            return


def select_legacy_entity(window: Any, entity_id: str) -> None:
    """Forward entity deep-links through the workspace-owned selection API."""

    items = getattr(window, "items", None)
    setter = getattr(window, "_set_selection", None)
    if not isinstance(items, dict) or entity_id not in items or not callable(setter):
        return
    setter((entity_id,), primary_id=entity_id, cancel_preview=True, persist=True)


__all__ = [
    "legacy_editor_deactivation_guard",
    "raise_legacy_context",
    "refresh_legacy_editor_revision",
    "select_legacy_entity",
]
