"""Typed navigation targets, resolver, history, and URI compat (#650)."""

from __future__ import annotations

from htdt.navigation_target import (
    NavigationHistory,
    NavigationIntent,
    NavigationResolver,
    NavigationTarget,
    NavigationTargetKind,
    navigation_target_from_uri,
)
from htdt.workflow_navigation import (
    ApplicationDestinationId,
    WorkspaceDeepLink,
    WorkspaceId,
)


def test_target_as_uri_roundtrip() -> None:
    target = NavigationTarget(
        kind=NavigationTargetKind.SCENE_ENTITY,
        object_ids=('speaker-1',),
        project_id='doc-1',
        revision_id='rev-1',
        system_variant_id='var-1',
        preferred_destination=WorkspaceId.ROOM,
        preferred_section='placement',
        intent=NavigationIntent.EDIT,
        referrer=WorkspaceId.OVERVIEW.value,
    )
    uri = target.as_uri()
    assert uri.startswith('htdt://nav/v1/scene_entity')

    parsed = navigation_target_from_uri(uri)
    assert parsed.kind == NavigationTargetKind.SCENE_ENTITY
    assert parsed.object_ids == ('speaker-1',)
    assert parsed.project_id == 'doc-1'
    assert parsed.revision_id == 'rev-1'
    assert parsed.system_variant_id == 'var-1'
    assert parsed.preferred_destination == WorkspaceId.ROOM
    assert parsed.preferred_section == 'placement'
    assert parsed.intent == NavigationIntent.EDIT
    assert parsed.referrer == WorkspaceId.OVERVIEW.value


def test_workspace_uri_compat_produces_typed_target() -> None:
    parsed = navigation_target_from_uri(
        'htdt://workspace/room/placement?entity=speaker-1'
    )
    assert parsed.kind == NavigationTargetKind.SCENE_ENTITY
    assert parsed.object_ids == ('speaker-1',)
    assert parsed.preferred_destination == WorkspaceId.ROOM
    assert parsed.preferred_section == 'placement'


def test_workspace_uri_without_entity_is_workspace_target() -> None:
    parsed = navigation_target_from_uri('htdt://workspace/optimization')
    assert parsed.kind == NavigationTargetKind.WORKSPACE
    assert parsed.preferred_destination == WorkspaceId.OPTIMIZATION


def test_app_scope_uri() -> None:
    parsed = navigation_target_from_uri('htdt://app/inbox')
    assert parsed.preferred_destination == ApplicationDestinationId.INBOX


def test_resolver_focused_status_when_surface_declares_kind() -> None:
    resolver = NavigationResolver()
    target = NavigationTarget(
        kind=NavigationTargetKind.SCENE_ENTITY,
        object_ids=('speaker-1',),
        preferred_destination=WorkspaceId.ROOM,
    )
    resolution = resolver.resolve(
        target,
        registered={WorkspaceId.ROOM},
        capabilities={
            WorkspaceId.ROOM: frozenset({NavigationTargetKind.SCENE_ENTITY})
        },
    )
    assert resolution.status == 'focused'
    assert resolution.ok
    assert resolution.link is not None
    assert resolution.link.entity_id == 'speaker-1'


def test_resolver_navigated_when_kind_not_declared() -> None:
    resolver = NavigationResolver()
    target = NavigationTarget(
        kind=NavigationTargetKind.SYSTEM_VARIANT,
        object_ids=('v1',),
        preferred_destination=WorkspaceId.MEASUREMENT,
    )
    resolution = resolver.resolve(
        target,
        registered={WorkspaceId.MEASUREMENT},
        capabilities={WorkspaceId.MEASUREMENT: frozenset()},
    )
    assert resolution.status == 'navigated'


def test_resolver_rejects_unregistered_destination() -> None:
    resolver = NavigationResolver()
    target = NavigationTarget(
        kind=NavigationTargetKind.HELP_TOPIC,
        object_ids=('shortcuts',),
    )
    resolution = resolver.resolve(
        target,
        registered=set(),
        capabilities={},
    )
    assert resolution.status == 'unsupported'
    assert resolution.message


def test_history_back_forward_replays_entries() -> None:
    history = NavigationHistory()
    t1 = NavigationTarget(
        kind=NavigationTargetKind.WORKSPACE,
        preferred_destination=WorkspaceId.ROOM,
    )
    t2 = NavigationTarget(
        kind=NavigationTargetKind.WORKSPACE,
        preferred_destination=WorkspaceId.MEASUREMENT,
    )
    history.record(t1, WorkspaceDeepLink(WorkspaceId.ROOM))
    history.record(t2, WorkspaceDeepLink(WorkspaceId.MEASUREMENT))
    assert history.current.target.preferred_destination == WorkspaceId.MEASUREMENT
    back = history.back()
    assert back.target.preferred_destination == WorkspaceId.ROOM
    assert history.can_go_forward
    forward = history.forward()
    assert forward.target.preferred_destination == WorkspaceId.MEASUREMENT


def test_history_bounded_capacity() -> None:
    history = NavigationHistory()
    for index in range(NavigationHistory.CAPACITY + 10):
        history.record(
            NavigationTarget(
                kind=NavigationTargetKind.WORKSPACE,
                preferred_destination=WorkspaceId.ROOM,
                preferred_section=str(index),
            ),
            WorkspaceDeepLink(WorkspaceId.ROOM),
        )
    assert len(history.entries()) <= NavigationHistory.CAPACITY


def test_workspace_deep_link_roundtrip_app_scope() -> None:
    link = WorkspaceDeepLink(ApplicationDestinationId.INBOX, 'all')
    uri = link.as_uri()
    assert uri.startswith('htdt://app/inbox')
    parsed = WorkspaceDeepLink.from_uri(uri)
    assert parsed.workspace == ApplicationDestinationId.INBOX
