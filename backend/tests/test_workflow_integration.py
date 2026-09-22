from __future__ import annotations

from types import SimpleNamespace

from htdt.command_registry import (
    CommandRegistry,
    WorkspaceDeepLink as RegistryDeepLink,
    WorkspaceId as RegistryWorkspaceId,
    register_default_commands,
)
from htdt.workflow_legacy_bridge import (
    legacy_editor_deactivation_guard,
    refresh_legacy_editor_revision,
)
from htdt.workflow_navigation import WorkspaceDeepLink, WorkspaceId


def test_navigation_contract_is_shared_across_command_and_shell_layers() -> None:
    assert RegistryWorkspaceId is WorkspaceId
    assert RegistryDeepLink is WorkspaceDeepLink
    target = WorkspaceDeepLink("room", section="placement", entity_id="speaker-1")
    assert target.workspace is WorkspaceId.ROOM
    assert target.section == "placement"
    assert target.subsection == "placement"
    assert target.as_uri().startswith("htdt://workspace/room/placement")


def test_command_execution_stops_when_shell_navigation_is_blocked() -> None:
    registry = CommandRegistry(deep_link_handler=lambda _target: False)
    register_default_commands(registry)

    assert registry.execute("navigation.room") is False


def test_legacy_bridge_blocks_dirty_or_preview_state() -> None:
    dirty = SimpleNamespace(
        working=SimpleNamespace(is_dirty=True, has_preview=False),
        recovery_candidate=None,
        findChildren=lambda _kind: [],
    )
    allowed, reason = legacy_editor_deactivation_guard(dirty)
    assert allowed is False
    assert reason is not None and "未保存" in reason

    preview = SimpleNamespace(
        working=SimpleNamespace(is_dirty=False, has_preview=True),
        recovery_candidate=None,
        findChildren=lambda _kind: [],
    )
    allowed, reason = legacy_editor_deactivation_guard(preview)
    assert allowed is False
    assert reason is not None and "編集中" in reason


def test_clean_stale_legacy_workspace_reloads_latest_revision() -> None:
    latest = SimpleNamespace(revision_id="revision-new")
    repository = SimpleNamespace(current_head=lambda _document_id: latest)
    calls: list[str] = []
    window = SimpleNamespace(
        working=SimpleNamespace(
            is_dirty=False,
            has_preview=False,
            source_revision_id="revision-old",
        ),
        recovery_candidate=None,
        repository=repository,
        document_id="project-1",
        findChildren=lambda _kind: [],
        _load_or_seed=lambda: calls.append("reload"),
    )

    assert refresh_legacy_editor_revision(window) is True
    assert calls == ["reload"]
