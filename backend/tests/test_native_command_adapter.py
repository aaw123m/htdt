"""Tests for the native command adapter (#776 family).

``native_command_adapter`` maps workspace window state to command-registry
availability — a misrouted binding here silently disables the undo/save/
import entry points for every UI surface (palette, menu, shortcuts) at once.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication

from htdt.native_command_adapter import (
    bind_active_editor_commands,
    bind_native_workspace_commands,
    build_native_command_registry,
    unbind_active_editor_commands,
)
from htdt.workflow_navigation import WorkspaceId


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _window(**overrides):
    calls = []

    def _record(name):
        def _fn(*args):
            calls.append((name, args))
        return _fn

    window = SimpleNamespace(
        save=_record('save'),
        undo=_record('undo'),
        redo=_record('redo'),
        start_room_sketch=_record('start_room_sketch'),
        add_object=_record('add_object'),
        import_rew_text_dialog=_record('import_rew_text_dialog'),
        run_rectangular_geometry_prediction_async=_record('run_prediction'),
        refresh_pareto_comparison=_record('refresh_pareto'),
        save_action=QAction(),
        undo_action=QAction(),
        redo_action=QAction(),
        draw_room_action=QAction(),
        calls=calls,
    )
    for name, value in overrides.items():
        setattr(window, name, value)
    return window


def _reason_code(registry, command_id: str) -> str | None:
    availability = registry.availability(command_id)
    return availability.reason.code if availability.reason else None


def test_registry_exposes_native_commands() -> None:
    _app()
    registry = build_native_command_registry(_window())
    ids = {definition.command_id for definition in registry.definitions()}
    assert {
        'project.save',
        'edit.undo',
        'edit.redo',
        'room.draw',
        'room.add_speaker',
        'measurements.import_rew',
        'prediction.run',
        'optimization.compare_candidates',
    } <= ids


def test_execute_dispatches_to_window_callable() -> None:
    _app()
    window = _window()
    window.undo_action.setEnabled(True)
    registry = build_native_command_registry(window)
    assert registry.execute('edit.undo') is True
    assert ('undo', ()) in window.calls


def test_action_availability_tracks_qaction_enabled() -> None:
    _app()
    window = _window()
    window.undo_action.setEnabled(False)
    registry = build_native_command_registry(window)
    assert registry.availability('edit.undo').enabled is False
    assert _reason_code(registry, 'edit.undo') == (
        'command.blocked.nothing_to_undo'
    )
    assert registry.execute('edit.undo') is False
    assert window.calls == []

    window.undo_action.setEnabled(True)
    assert registry.availability('edit.undo').enabled is True


def test_missing_action_attribute_blocks_command() -> None:
    _app()
    window = _window()
    del window.save_action
    registry = build_native_command_registry(window)
    assert registry.availability('project.save').enabled is False
    assert _reason_code(registry, 'project.save') == (
        'project.save.unavailable_or_editing'
    )


def test_measurement_import_availability_ladder() -> None:
    _app()
    window = _window()
    registry = build_native_command_registry(window)
    # No saved measurement target → unbound workspace.
    assert _reason_code(registry, 'measurements.import_rew') == (
        'measurement.import.workspace_unbound'
    )

    # A target that raises means no saved scene/point pair exists yet.
    def _raising():
        raise RuntimeError('no saved scene')

    window._saved_measurement_target = _raising
    assert _reason_code(registry, 'measurements.import_rew') == (
        'measurement.import.requires_saved_scene_and_point'
    )

    window._saved_measurement_target = lambda: True
    assert registry.availability('measurements.import_rew').enabled is True


def test_prediction_availability_ladder() -> None:
    _app()
    window = _window()
    registry = build_native_command_registry(window)

    window._current_prediction_token_id = 'tok-1'
    assert _reason_code(registry, 'prediction.run') == (
        'prediction.run.running'
    )

    window._current_prediction_token_id = None
    assert _reason_code(registry, 'prediction.run') == (
        'prediction.run.workspace_unbound'
    )

    def _raising():
        raise RuntimeError('nothing saved')

    window._saved_prediction_target = _raising
    assert _reason_code(registry, 'prediction.run') == (
        'prediction.run.requires_saved_scene_and_receiver'
    )

    window._saved_prediction_target = lambda: True
    assert registry.availability('prediction.run').enabled is True


def test_candidate_compare_availability() -> None:
    _app()
    window = _window()
    registry = build_native_command_registry(window)
    command_id = 'optimization.compare_candidates'
    assert _reason_code(registry, command_id) == (
        'optimization.compare.requires_spec_selection'
    )

    window.search_selected_spec_id = 'spec-1'
    assert _reason_code(registry, command_id) == (
        'optimization.compare.workspace_unbound'
    )

    window.objective_repository = object()
    assert registry.availability(command_id).enabled is True


def test_add_speaker_uses_callable_availability() -> None:
    _app()
    window = _window()
    registry = build_native_command_registry(window)
    # No _object_edit_available attribute → blocked.
    assert _reason_code(registry, 'room.add_speaker') == (
        'room.add_speaker.requires_finished_room'
    )
    window._object_edit_available = lambda: True
    assert registry.availability('room.add_speaker').enabled is True
    assert registry.execute('room.add_speaker') is True
    assert ('add_object', ('speaker',)) in window.calls


def test_workspace_scoped_binding_applies_per_workspace_ladder() -> None:
    _app()
    window = _window()
    registry = build_native_command_registry(window)
    bind_native_workspace_commands(registry, window, WorkspaceId.MEASUREMENT)
    # measurement import is blocked while no saved target exists.
    assert registry.execute('measurements.import_rew') is False
    window._saved_measurement_target = lambda: True
    assert registry.execute('measurements.import_rew') is True
    assert ('import_rew_text_dialog', ()) in window.calls

    # Rebinding for the room workspace restores room.draw's executor.
    window.draw_room_action.setEnabled(True)
    bind_native_workspace_commands(registry, window, WorkspaceId.ROOM)
    assert registry.execute('room.draw') is True
    assert ('start_room_sketch', ()) in window.calls


def test_unbind_returns_commands_to_context_blocked() -> None:
    _app()
    window = _window()
    window.undo_action.setEnabled(True)
    registry = build_native_command_registry(window)
    unbind_active_editor_commands(registry)
    availability = registry.availability('edit.undo')
    assert availability.enabled is False
    assert _reason_code(registry, 'edit.undo') == (
        'command.blocked.unavailable_in_context'
    )


def test_bind_active_editor_commands_rebinds_after_unbind() -> None:
    _app()
    window = _window()
    window.undo_action.setEnabled(True)
    registry = build_native_command_registry(window)
    unbind_active_editor_commands(registry)
    bind_active_editor_commands(registry, window)
    assert registry.execute('edit.undo') is True
    assert ('undo', ()) in window.calls
