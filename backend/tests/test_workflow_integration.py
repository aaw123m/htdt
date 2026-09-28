from __future__ import annotations

from htdt.command_registry import (
    CommandRegistry,
    WorkspaceDeepLink as RegistryDeepLink,
    WorkspaceId as RegistryWorkspaceId,
    register_default_commands,
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

