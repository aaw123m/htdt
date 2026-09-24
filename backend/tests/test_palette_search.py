"""Composable palette providers and fused search (#585)."""

from __future__ import annotations

from types import SimpleNamespace

from htdt.command_registry import (
    CommandAvailability,
    CommandContext,
    CommandDefinition,
    CommandRegistry,
)
from htdt.palette_search import (
    CommandPaletteProvider,
    PaletteResultKind,
    PaletteSearchService,
    SceneEntityPaletteProvider,
    StaticPaletteProvider,
    help_destinations,
    settings_destinations,
)
from htdt.workflow_navigation import WorkspaceDeepLink, WorkspaceId


def _registry() -> CommandRegistry:
    registry = CommandRegistry()
    registry.set_deep_link_handler(lambda link: True)
    registry.register(
        CommandDefinition(
            command_id='workspace.navigate.room',
            display_name='部屋を開く',
            contexts=(CommandContext.GLOBAL,),
            shortcut='Ctrl+2',
            keywords=('room', '画面'),
            deep_link=WorkspaceDeepLink(WorkspaceId.ROOM),
        ),
        availability=lambda: CommandAvailability.available(),
    )
    registry.register(
        CommandDefinition(
            command_id='project.save',
            display_name='保存',
            contexts=(CommandContext.GLOBAL,),
            shortcut='Ctrl+S',
            keywords=('save', '保存'),
        ),
        availability=lambda: CommandAvailability.available(),
    )
    return registry


def _entities() -> tuple:
    return (
        SimpleNamespace(
            entity_id='sp-fl',
            kind='speaker',
            name='Front Left',
            speaker_role='FL',
        ),
        SimpleNamespace(
            entity_id='seat-1',
            kind='seat',
            name='メインシート',
            speaker_role=None,
        ),
    )


def _service(**overrides) -> PaletteSearchService:
    links: list[WorkspaceDeepLink] = []
    return PaletteSearchService(
        (
            CommandPaletteProvider(_registry()),
            SceneEntityPaletteProvider(
                _entities,
                lambda entity: WorkspaceDeepLink(
                    WorkspaceId.ROOM, 'placement', entity_id=entity.entity_id
                ),
            ),
            StaticPaletteProvider(
                'settings',
                PaletteResultKind.SETTINGS,
                settings_destinations(),
                lambda destination_id: True,
            ),
            StaticPaletteProvider(
                'help',
                PaletteResultKind.HELP,
                help_destinations(),
                lambda destination_id: True,
            ),
        ),
        on_deep_link=lambda link: links.append(link) or True,
    )


def test_command_provider_marks_navigation_results() -> None:
    service = _service()
    results = service.search('部屋')
    navigation = [r for r in results if r.kind == PaletteResultKind.NAVIGATION]
    assert any(r.command_id == 'workspace.navigate.room' for r in navigation)
    room = next(r for r in navigation if r.command_id == 'workspace.navigate.room')
    assert room.shortcut == 'Ctrl+2'
    assert room.deep_link == WorkspaceDeepLink(WorkspaceId.ROOM)


def test_scene_entities_searchable_by_name_role_and_alias() -> None:
    service = _service()
    by_name = service.search('Front')
    assert any(r.result_id == 'entity:sp-fl' for r in by_name)
    by_role = service.search('FL')
    assert any(r.result_id == 'entity:sp-fl' for r in by_role)
    by_alias = service.search('speaker')
    assert any(r.result_id == 'entity:sp-fl' for r in by_alias)


def test_results_deduped_by_stable_result_id() -> None:
    service = _service()
    results = service.search('保存')
    ids = [r.result_id for r in results]
    assert len(ids) == len(set(ids))
    assert all(id_.count(':') == 1 for id_ in ids)


def test_empty_query_groups_recent_and_navigation() -> None:
    service = _service()
    groups = service.suggested(context=CommandContext.GLOBAL)
    assert groups
    assert all(r.group in ('最近使用', 'ナビゲーション') for r in groups)
    navigation_only = {r.group for r in groups}
    assert 'ナビゲーション' in navigation_only


def test_activation_routes_deep_links_and_commands() -> None:
    links: list[WorkspaceDeepLink] = []
    service = PaletteSearchService(
        (
            CommandPaletteProvider(_registry()),
            SceneEntityPaletteProvider(
                _entities,
                lambda entity: WorkspaceDeepLink(
                    WorkspaceId.ROOM, 'placement', entity_id=entity.entity_id
                ),
            ),
        ),
        on_deep_link=lambda link: links.append(link) or True,
    )
    # command activation dispatches through the registry itself
    command = next(
        r for r in service.search('部屋') if r.command_id == 'workspace.navigate.room'
    )
    assert service.activate(command)

    # entity results activate via the service-level deep-link route
    entity = next(r for r in service.search('Front') if r.command_id is None)
    assert service.activate(entity)
    assert links == [
        WorkspaceDeepLink(WorkspaceId.ROOM, 'placement', entity_id='sp-fl')
    ]

    # activated results move into the Recent empty-query group
    suggested = service.suggested(context=CommandContext.GLOBAL)
    assert any(r.group == '最近使用' for r in suggested)


def test_unavailable_result_shows_reason_and_refuses_activation() -> None:
    service = PaletteSearchService(
        (CommandPaletteProvider(_registry()),),
    )
    provider_result = next(
        (r for r in service.search('speaker') if r.available is False), None
    )
    # entities provider not present in this service; check a disabled command
    registry = CommandRegistry()
    registry.register(
        CommandDefinition(
            command_id='mutate.thing',
            display_name='変更する',
            contexts=(CommandContext.GLOBAL,),
        ),
        availability=lambda: CommandAvailability.unavailable('凍結中です'),
    )
    service = PaletteSearchService((CommandPaletteProvider(registry),))
    result = next(r for r in service.search('変更') if not r.available)
    assert result.disabled_reason == '凍結中です'
    assert service.activate(result) is False


def test_settings_and_help_destinations_present() -> None:
    service = _service()
    assert any(
        r.result_id == 'settings:settings.data'
        for r in service.search('バックアップ')
    )
    assert any(
        r.result_id == 'help:help.shortcuts' for r in service.search('shortcut')
    )
