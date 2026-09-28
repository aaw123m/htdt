"""Round 13 — settings & option effect truth.

Pins the findings this round fixes:

- ``integrations.capture_receiver_enabled`` written through the 環境設定
  checkbox persisted the policy but never applied it — the receiver kept
  its previous live state until restart and the キャプチャ tab showed a
  stale combo.
- ``integrations.rew_host`` / ``integrations.rew_port`` persisted but no
  production client read them (``rew_api_base_url`` was dead code; every
  ``RewApiClient`` hardcoded the loopback default). Both are now wired
  into the workflow compositions and rebind live.
- ``general.language`` was still marked 準備中 although the
  LocalizationService + help/palette surfaces already consume it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QComboBox, QWidget  # noqa: E402

from htdt.application_preferences import (  # noqa: E402
    PENDING_PREFERENCE_KEYS,
    PREFERENCES_SCHEMA_VERSION,
    ApplicationPreferenceStore,
    PreferenceValueError,
)
from htdt.cad_repository import SceneRepository  # noqa: E402
from htdt.capture_receiver_controller import (  # noqa: E402
    PREFERENCE_KEY,
    CaptureReceiverController,
)
from htdt.rew_api import DEFAULT_REW_API_URL, validate_rew_api_url  # noqa: E402
from htdt.workflow_application import (  # noqa: E402
    WorkflowApplicationComposition,
)
from htdt.workflow_navigation import WorkspaceId  # noqa: E402
from htdt.workflow_settings import PreferencesWidget  # noqa: E402


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _rig(tmp_path: Path):
    repository = SceneRepository(tmp_path / 'data' / 'cad-scenes.sqlite3')
    preferences = ApplicationPreferenceStore.for_data_dir(tmp_path / 'data')
    controller = CaptureReceiverController(repository, preferences)
    return repository, preferences, controller


# ---------------------------------------------------------------------------
# Cross-surface: the same logical setting in 環境設定 and キャプチャ
# ---------------------------------------------------------------------------


def test_environment_tab_write_applies_receiver_state(tmp_path: Path) -> None:
    """A raw ``store.set`` — what the 環境設定 checkbox commits — must
    start/stop the receiver live, not just persist a flag."""
    _app()
    _, preferences, controller = _rig(tmp_path)
    try:
        signals: list[str] = []
        controller.changed.connect(lambda: signals.append('changed'))

        preferences.set(PREFERENCE_KEY, True)
        assert controller.service.running is True
        assert signals  # panels refresh without a manual poll

        preferences.set(PREFERENCE_KEY, False)
        assert controller.service.running is False
        assert controller.service.get_config().enabled is False
    finally:
        controller.shutdown()


def test_set_enabled_still_returns_start_errors(tmp_path: Path) -> None:
    """The キャプチャ tab's apply path keeps its error contract through
    the new shared apply helper."""
    import socket

    _app()
    _, preferences, controller = _rig(tmp_path)
    blocker = socket.socket()
    blocker.bind(('0.0.0.0', 0))
    blocker.listen(1)
    controller.service.set_port(blocker.getsockname()[1])
    try:
        error = controller.set_enabled(True)
        assert error
        assert controller.last_error == error
        assert controller.service.running is False
        # The persisted policy still says enabled — the failure is
        # operational, not a silent rollback of the user's choice.
        assert preferences.get(PREFERENCE_KEY) is True
    finally:
        blocker.close()
        controller.shutdown()


def test_preferences_widget_tracks_external_writes(tmp_path: Path) -> None:
    """The 環境設定 checkbox mirrors a flip committed by another surface —
    the same sync gap in reverse."""
    _app()
    store = ApplicationPreferenceStore(tmp_path / 'p.json')
    widget = PreferencesWidget(store)
    editor = widget._editors[PREFERENCE_KEY]
    assert not editor.isChecked()
    # The write path the キャプチャ tab's 適用 button takes.
    store.set(PREFERENCE_KEY, True)
    assert editor.isChecked()
    store.set(PREFERENCE_KEY, False)
    assert not editor.isChecked()
    widget.deleteLater()


def test_shutdown_prevents_post_exit_restart(tmp_path: Path) -> None:
    _app()
    _, preferences, controller = _rig(tmp_path)
    preferences.set(PREFERENCE_KEY, True)
    assert controller.service.running is True
    controller.shutdown()
    assert controller.service.running is False
    # A late preference write after shutdown must not resurrect the socket.
    preferences.set(PREFERENCE_KEY, False)
    preferences.set(PREFERENCE_KEY, True)
    assert controller.service.running is False


# ---------------------------------------------------------------------------
# REW endpoint preferences reach the live clients
# ---------------------------------------------------------------------------


def test_wired_keys_left_the_pending_set() -> None:
    for key in (
        'general.language',
        'integrations.rew_host',
        'integrations.rew_port',
    ):
        assert key not in PENDING_PREFERENCE_KEYS


def test_preferences_widget_enables_newly_wired_editors(tmp_path: Path) -> None:
    _app()
    widget = PreferencesWidget(ApplicationPreferenceStore(tmp_path / 'p.json'))
    for key in (
        'general.language',
        'integrations.rew_host',
        'integrations.rew_port',
    ):
        editor = widget._editors[key]
        assert editor.isEnabled(), key
    assert isinstance(widget._editors['integrations.rew_host'], QComboBox)
    widget.deleteLater()


def test_measure_mount_uses_persisted_rew_endpoint(tmp_path: Path) -> None:
    _app()
    repository = SceneRepository(tmp_path / 'data' / 'cad-scenes.sqlite3')
    preferences = ApplicationPreferenceStore.for_data_dir(tmp_path / 'data')
    preferences.set('integrations.rew_port', 5000)
    composition = WorkflowApplicationComposition(
        repository, 'document-1', preferences=preferences
    )
    try:
        mount = composition._make_measurement()
        client = mount.widget.controller.rew_client
        assert client.base_url == 'http://127.0.0.1:5000'
        mount.widget.deleteLater()
    finally:
        composition.shell.close()
        composition.shell.deleteLater()


def test_rew_endpoint_change_rebinds_mounted_client(tmp_path: Path) -> None:
    _app()
    repository = SceneRepository(tmp_path / 'data' / 'cad-scenes.sqlite3')
    preferences = ApplicationPreferenceStore.for_data_dir(tmp_path / 'data')
    composition = WorkflowApplicationComposition(
        repository, 'document-1', preferences=preferences
    )
    try:
        mount = composition._make_measurement()
        composition.shell.router._mounts[WorkspaceId.MEASUREMENT] = mount
        client = mount.widget.controller.rew_client
        assert client.base_url == DEFAULT_REW_API_URL

        preferences.set('integrations.rew_port', 5001)
        assert client.base_url == 'http://127.0.0.1:5001'

        preferences.set('integrations.rew_host', 'localhost')
        assert client.base_url == 'http://localhost:5001'

        mount.widget.deleteLater()
    finally:
        composition.shell.close()
        composition.shell.deleteLater()


def test_optimization_mount_forwards_rew_client(tmp_path: Path) -> None:
    _app()
    repository = SceneRepository(tmp_path / 'data' / 'cad-scenes.sqlite3')
    injected = object()  # any non-None RewReadSource-shaped stand-in
    mount = __import__(
        'htdt.optimization_workflow_workspace',
        fromlist=['build_optimization_workspace_mount'],
    ).build_optimization_workspace_mount(
        repository, 'document-1', rew_client=injected
    )
    try:
        assert mount.widget.controller.rew_client is injected
    finally:
        mount.widget.deleteLater()


# ---------------------------------------------------------------------------
# Validation: the definitions match the consumer's contract
# ---------------------------------------------------------------------------


def test_rew_host_accepts_only_loopback(tmp_path: Path) -> None:
    store = ApplicationPreferenceStore(tmp_path / 'prefs.json')
    with pytest.raises(PreferenceValueError):
        store.set('integrations.rew_host', '192.168.1.5')
    with pytest.raises(ValueError):
        validate_rew_api_url('http://192.168.1.5:4735')


def test_rew_port_definition_matches_consumer_bounds(tmp_path: Path) -> None:
    store = ApplicationPreferenceStore(tmp_path / 'prefs.json')
    definition = store.definition('integrations.rew_port')
    with pytest.raises(PreferenceValueError):
        definition.validate(1023)
    with pytest.raises(ValueError):
        validate_rew_api_url('http://127.0.0.1:1023')


def test_legacy_persisted_rew_values_drop_honestly(tmp_path: Path) -> None:
    """A value persisted under the old unconstrained definitions is not
    silently used — the load drops it to default and reports it."""
    path = tmp_path / 'prefs.json'
    path.write_text(
        json.dumps(
            {
                'schema_version': PREFERENCES_SCHEMA_VERSION,
                'authority': 'htdt-application-preferences',
                'values': {
                    'integrations.rew_host': 'rew.local',
                    'integrations.rew_port': 80,
                },
            }
        ),
        encoding='utf-8',
    )
    store = ApplicationPreferenceStore(path)
    assert store.get('integrations.rew_host') == '127.0.0.1'
    assert store.get('integrations.rew_port') == 4735
    assert store.load_error
