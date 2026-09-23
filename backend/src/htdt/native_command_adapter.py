from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QWidget

from .command_palette import CommandPaletteController
from .command_registry import (
    CommandAvailability,
    CommandContext,
    CommandRegistry,
    DeepLinkHandler,
    register_default_commands,
)
from .workflow_navigation import WorkspaceId


def _action_availability(
    window: Any,
    action_name: str,
    *,
    disabled_reason: str,
) -> CommandAvailability:
    action = getattr(window, action_name, None)
    if isinstance(action, QAction) and action.isEnabled():
        return CommandAvailability.available()
    return CommandAvailability.unavailable(disabled_reason)


def _callable_availability(
    window: Any,
    method_name: str,
    *,
    disabled_reason: str,
) -> CommandAvailability:
    method = getattr(window, method_name, None)
    if method is None:
        return CommandAvailability.unavailable(disabled_reason)
    try:
        available = bool(method())
    except Exception:
        available = False
    if available:
        return CommandAvailability.available()
    return CommandAvailability.unavailable(disabled_reason)


def _measurement_import_availability(window: Any) -> CommandAvailability:
    target = getattr(window, '_saved_measurement_target', None)
    if target is None:
        return CommandAvailability.unavailable('測定ワークスペースが接続されていません')
    try:
        target()
    except Exception:
        return CommandAvailability.unavailable(
            '保存済みSceneと測定点を用意してからREWを読み込んでください'
        )
    return CommandAvailability.available()


def _prediction_availability(window: Any) -> CommandAvailability:
    if getattr(window, '_current_prediction_token_id', None) is not None:
        return CommandAvailability.unavailable('予測を実行中です')
    target = getattr(window, '_saved_prediction_target', None)
    if target is None:
        return CommandAvailability.unavailable('予測ワークスペースが接続されていません')
    try:
        target()
    except Exception:
        return CommandAvailability.unavailable(
            '保存済みSceneと受音点を用意してから予測を実行してください'
        )
    return CommandAvailability.available()


def _candidate_compare_availability(window: Any) -> CommandAvailability:
    if getattr(window, 'search_selected_spec_id', None) is None:
        return CommandAvailability.unavailable('比較する探索仕様を選択してください')
    if getattr(window, 'objective_repository', None) is None:
        return CommandAvailability.unavailable('候補比較ワークスペースが接続されていません')
    return CommandAvailability.available()


def build_native_command_registry(
    window: Any,
    *,
    deep_link_handler: DeepLinkHandler | None = None,
) -> CommandRegistry:
    """Adapt existing UI/domain authorities into central command metadata.

    The callbacks deliberately delegate to existing window methods. This module does
    not persist scenes, import measurements, run solvers, or compute Pareto results.
    """

    registry = CommandRegistry(deep_link_handler=deep_link_handler)
    bindings = {
        'project.save': (
            window.save,
            lambda: _action_availability(
                window,
                'save_action',
                disabled_reason='保存できる変更がないか、編集中の操作があります',
            ),
        ),
        'edit.undo': (
            window.undo,
            lambda: _action_availability(
                window,
                'undo_action',
                disabled_reason='元に戻せる操作はありません',
            ),
        ),
        'edit.redo': (
            window.redo,
            lambda: _action_availability(
                window,
                'redo_action',
                disabled_reason='やり直せる操作はありません',
            ),
        ),
        'room.draw': (
            window.start_room_sketch,
            lambda: _action_availability(
                window,
                'draw_room_action',
                disabled_reason='部屋の編集中または復旧確認中は新しい作図を開始できません',
            ),
        ),
        'room.add_speaker': (
            lambda: window.add_object('speaker'),
            lambda: _callable_availability(
                window,
                '_object_edit_available',
                disabled_reason='部屋を作成し、部屋・壁編集を完了してから追加してください',
            ),
        ),
        'measurements.import_rew': (
            window.import_rew_text_dialog,
            lambda: _measurement_import_availability(window),
        ),
        'prediction.run': (
            window.run_rectangular_geometry_prediction_async,
            lambda: _prediction_availability(window),
        ),
        'optimization.compare_candidates': (
            window.refresh_pareto_comparison,
            lambda: _candidate_compare_availability(window),
        ),
    }
    register_default_commands(registry, bindings=bindings)
    return registry


def bind_active_editor_commands(registry: CommandRegistry, window: Any) -> None:
    """Bind global edit commands to the currently active legacy editor only."""

    registry.bind(
        'project.save',
        execute=window.save,
        availability=lambda: _action_availability(
            window,
            'save_action',
            disabled_reason='保存できる変更がないか、編集中の操作があります',
        ),
    )
    registry.bind(
        'edit.undo',
        execute=window.undo,
        availability=lambda: _action_availability(
            window,
            'undo_action',
            disabled_reason='元に戻せる操作はありません',
        ),
    )
    registry.bind(
        'edit.redo',
        execute=window.redo,
        availability=lambda: _action_availability(
            window,
            'redo_action',
            disabled_reason='やり直せる操作はありません',
        ),
    )


def unbind_active_editor_commands(registry: CommandRegistry) -> None:
    for command_id in ('project.save', 'edit.undo', 'edit.redo'):
        registry.unbind(command_id)


def bind_native_workspace_commands(
    registry: CommandRegistry,
    window: Any,
    workspace: WorkspaceId,
) -> None:
    """Bind task commands to the legacy workspace that still owns their authority."""

    if workspace is WorkspaceId.ROOM:
        registry.bind(
            'room.draw',
            execute=window.start_room_sketch,
            availability=lambda: _action_availability(
                window,
                'draw_room_action',
                disabled_reason='部屋の編集中または復旧確認中は新しい作図を開始できません',
            ),
        )
        registry.bind(
            'room.add_speaker',
            execute=lambda: window.add_object('speaker'),
            availability=lambda: _callable_availability(
                window,
                '_object_edit_available',
                disabled_reason='部屋を作成し、部屋・壁編集を完了してから追加してください',
            ),
        )
        registry.bind(
            'prediction.run',
            execute=window.run_rectangular_geometry_prediction_async,
            availability=lambda: _prediction_availability(window),
        )
    elif workspace is WorkspaceId.MEASUREMENT:
        registry.bind(
            'measurements.import_rew',
            execute=window.import_rew_text_dialog,
            availability=lambda: _measurement_import_availability(window),
        )
    elif workspace is WorkspaceId.OPTIMIZATION:
        registry.bind(
            'optimization.compare_candidates',
            execute=window.refresh_pareto_comparison,
            availability=lambda: _candidate_compare_availability(window),
        )


def install_native_command_palette(
    window: QWidget,
    *,
    deep_link_handler: DeepLinkHandler | None = None,
    context_provider: Callable[[], CommandContext | None] | None = None,
) -> CommandPaletteController:
    registry = build_native_command_registry(
        window,
        deep_link_handler=deep_link_handler,
    )
    controller = CommandPaletteController(
        window,
        registry,
        context_provider=context_provider,
    )
    # QObject parenting keeps the controller alive, while the explicit attribute is
    # useful to Agent A for attaching a router after shell construction.
    setattr(window, 'command_palette_controller', controller)
    return controller
