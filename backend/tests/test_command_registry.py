from __future__ import annotations

import pytest

from htdt.command_registry import (
    DATA_MUTATIONS_FROZEN_REASON,
    CommandAvailability,
    CommandDefinition,
    CommandRegistry,
    ShortcutBehavior,
    WorkspaceDeepLink,
    WorkspaceId,
    command_shortcut_allowed,
    default_command_definitions,
    register_default_commands,
)


REQUIRED_COMMAND_IDS = {
    'navigation.overview',
    'navigation.room',
    'navigation.measurements',
    'navigation.optimization',
    'project.save',
    'edit.undo',
    'edit.redo',
    'room.draw',
    'room.add_speaker',
    'measurements.import_rew',
    'prediction.run',
    'optimization.compare_candidates',
}


def _registry_with_defaults() -> CommandRegistry:
    registry = CommandRegistry()
    register_default_commands(registry)
    return registry


def test_default_registry_covers_issue_118_minimum_commands() -> None:
    definitions = {item.command_id: item for item in default_command_definitions()}

    assert REQUIRED_COMMAND_IDS <= definitions.keys()
    assert definitions['navigation.overview'].display_name == '概要'
    assert definitions['navigation.room'].display_name == '部屋'
    assert definitions['navigation.measurements'].display_name == '測定'
    assert definitions['navigation.optimization'].display_name == '最適化'
    assert definitions['project.save'].shortcut == 'Ctrl+S'
    assert definitions['edit.undo'].shortcut == 'Ctrl+Z'
    assert definitions['edit.redo'].shortcut_aliases == ('Ctrl+Shift+Z',)
    assert definitions['navigation.measurements'].deep_link == WorkspaceDeepLink(
        WorkspaceId.MEASUREMENT
    )
    assert definitions['room.add_speaker'].deep_link == WorkspaceDeepLink(
        WorkspaceId.ROOM, 'placement'
    )


def test_navigation_and_scene_commands_share_one_search_entry() -> None:
    registry = _registry_with_defaults()

    ids = {result.definition.command_id for result in registry.search('部屋')}

    assert 'navigation.room' in ids
    assert 'room.draw' in ids


def test_disabled_reason_is_returned_and_execution_is_blocked() -> None:
    calls: list[str] = []
    registry = CommandRegistry()
    registry.register(
        CommandDefinition(command_id='test.blocked', display_name='利用不可テスト'),
        execute=lambda: calls.append('executed'),
        availability=lambda: CommandAvailability.unavailable('前提条件が不足しています'),
    )

    result = registry.search('利用不可')[0]

    assert result.availability.enabled is False
    assert result.availability.disabled_reason == '前提条件が不足しています'
    assert registry.execute('test.blocked') is False
    assert calls == []


def test_deep_link_command_enables_when_shell_router_is_attached() -> None:
    visited: list[WorkspaceDeepLink] = []
    registry = CommandRegistry()
    definition = CommandDefinition(
        command_id='navigation.test',
        display_name='テスト画面',
        deep_link=WorkspaceDeepLink(WorkspaceId.ROOM, 'geometry'),
    )
    registry.register(definition)

    assert registry.availability(definition.command_id).enabled is False

    registry.set_deep_link_handler(visited.append)

    assert registry.execute(definition.command_id) is True
    assert visited == [WorkspaceDeepLink(WorkspaceId.ROOM, 'geometry')]


def test_lazy_shell_binding_can_attach_executor_during_navigation() -> None:
    events: list[str] = []
    registry = CommandRegistry()
    definition = CommandDefinition(
        command_id='room.lazy',
        display_name='遅延接続',
        deep_link=WorkspaceDeepLink(WorkspaceId.ROOM, 'geometry'),
    )
    registry.register(definition)

    def navigate(_deep_link: WorkspaceDeepLink) -> None:
        events.append('navigate')
        registry.bind(
            definition.command_id,
            execute=lambda: events.append('execute'),
        )

    registry.set_deep_link_handler(navigate)

    assert registry.execute(definition.command_id) is True
    assert events == ['navigate', 'execute']


def test_unbound_global_command_has_disabled_reason_until_bound() -> None:
    registry = CommandRegistry()
    definition = CommandDefinition(command_id='project.lazy', display_name='遅延保存')
    registry.register(definition)

    availability = registry.availability(definition.command_id)
    assert availability.enabled is False
    assert availability.disabled_reason == 'この操作は現在の画面では利用できません'

    registry.bind(definition.command_id, execute=lambda: None)

    assert registry.availability(definition.command_id).enabled is True


def test_duplicate_command_id_is_rejected() -> None:
    registry = CommandRegistry()
    definition = CommandDefinition(command_id='test.duplicate', display_name='重複')
    registry.register(definition, execute=lambda: None)

    with pytest.raises(ValueError, match='duplicate command id'):
        registry.register(definition, execute=lambda: None)


def test_focus_safe_shortcuts_do_not_fire_in_text_input() -> None:
    undo = next(
        item for item in default_command_definitions() if item.command_id == 'edit.undo'
    )
    save = next(
        item for item in default_command_definitions() if item.command_id == 'project.save'
    )
    scene = CommandDefinition(
        command_id='scene.move',
        display_name='移動',
        shortcut='M',
        shortcut_behavior=ShortcutBehavior.FOCUS_SAFE,
    )

    assert command_shortcut_allowed(undo, text_input_focused=True) is False
    assert command_shortcut_allowed(scene, text_input_focused=True) is False
    assert command_shortcut_allowed(save, text_input_focused=True) is True
    assert command_shortcut_allowed(scene, text_input_focused=False) is True


def test_mutation_classification_is_fail_closed_for_data_commands() -> None:
    definitions = {item.command_id: item for item in default_command_definitions()}

    read_only = {
        command_id
        for command_id, definition in definitions.items()
        if not definition.mutates_managed_data
    }

    assert read_only == {
        'navigation.overview',
        'navigation.room',
        'navigation.measurements',
        'navigation.optimization',
        'room.view.fit_selection',
        'room.view.fit_all',
    }
    assert definitions['project.save'].mutates_managed_data is True
    assert definitions['edit.undo'].mutates_managed_data is True
    assert definitions['room.draw'].mutates_managed_data is True
    assert CommandDefinition(command_id='custom', display_name='任意').mutates_managed_data is True


def test_data_mutation_freeze_fails_closed_over_enabled_workspace_availability() -> None:
    calls: list[str] = []
    registry = _registry_with_defaults()
    registry.bind(
        'project.save',
        execute=lambda: calls.append('save'),
        availability=CommandAvailability.available,
    )

    assert registry.data_mutations_frozen is False
    assert registry.execute('project.save') is True
    assert calls == ['save']

    registry.freeze_data_mutations()

    assert registry.data_mutations_frozen is True
    availability = registry.availability('project.save')
    assert availability.enabled is False
    assert availability.disabled_reason == DATA_MUTATIONS_FROZEN_REASON
    assert registry.execute('project.save') is False
    assert calls == ['save']

    registry.thaw_data_mutations()

    assert registry.data_mutations_frozen is False
    assert registry.availability('project.save').enabled is True
    assert registry.execute('project.save') is True
    assert calls == ['save', 'save']


def test_data_mutation_freeze_keeps_read_only_commands_available() -> None:
    calls: list[str] = []
    navigated: list[WorkspaceDeepLink] = []
    registry = _registry_with_defaults()
    registry.set_deep_link_handler(lambda target: navigated.append(target))
    registry.bind(
        'room.view.fit_all',
        execute=lambda: calls.append('fit-all'),
        availability=CommandAvailability.available,
    )

    registry.freeze_data_mutations()

    assert registry.availability('navigation.room').enabled is True
    assert registry.execute('navigation.room') is True
    assert navigated == [WorkspaceDeepLink(WorkspaceId.ROOM)]
    assert registry.availability('room.view.fit_all').enabled is True
    assert registry.execute('room.view.fit_all') is True
    assert calls == ['fit-all']


def test_data_mutation_freeze_reason_surfaces_in_search_results() -> None:
    registry = _registry_with_defaults()
    registry.bind(
        'project.save',
        execute=lambda: None,
        availability=CommandAvailability.available,
    )

    registry.freeze_data_mutations()

    result = next(
        item
        for item in registry.search('保存')
        if item.definition.command_id == 'project.save'
    )
    assert result.availability.enabled is False
    assert result.availability.disabled_reason == DATA_MUTATIONS_FROZEN_REASON
