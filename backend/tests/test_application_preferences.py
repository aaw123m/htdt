"""#591 application preferences tests."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from htdt.application_preferences import (
    ApplicationPreferences,
    ApplicationPreferenceStore,
    PREFERENCE_DEFINITIONS,
    PreferenceChange,
    PreferenceNotificationError,
    PreferenceValueError,
    UnknownPreferenceKey,
)


def test_all_keys_are_namespaced() -> None:
    for key in PREFERENCE_DEFINITIONS:
        category, _, _ = key.partition('.')
        assert category and '.' in key


def test_store_loads_defaults_when_missing(tmp_path) -> None:
    store = ApplicationPreferenceStore(tmp_path / 'prefs.json')
    snap = store.snapshot()
    # The snapshot model merges registered defaults for presentation.
    assert snap.values == {k: d.default for k, d in PREFERENCE_DEFINITIONS.items()}
    assert store.is_default('general.language')
    assert store.load_error is None


def test_set_persist_and_reload(tmp_path) -> None:
    path = tmp_path / 'prefs.json'
    store = ApplicationPreferenceStore(path)
    store.set('display_input.length_unit', 'cm')
    store.set('integrations.rew_port', 9000)

    reloaded = ApplicationPreferenceStore(path)
    assert reloaded.get('display_input.length_unit') == 'cm'
    assert reloaded.get('integrations.rew_port') == 9000
    assert reloaded.rew_api_base_url() == 'http://127.0.0.1:9000'


def test_unknown_and_invalid_values_rejected(tmp_path) -> None:
    store = ApplicationPreferenceStore(tmp_path / 'prefs.json')
    with pytest.raises(UnknownPreferenceKey):
        store.set('no.such.key', 1)
    with pytest.raises(PreferenceValueError):
        store.set('general.language', 'de')
    with pytest.raises(PreferenceValueError):
        store.set('integrations.rew_port', 70000)
    with pytest.raises(PreferenceValueError):
        store.set('display_input.numeric_precision', 9)


def test_reset_and_subscribe(tmp_path) -> None:
    store = ApplicationPreferenceStore(tmp_path / 'prefs.json')
    seen: list[PreferenceChange] = []
    store.subscribe(seen.append)
    store.set('display_input.theme', 'dark')
    assert [(c.key, c.new) for c in seen] == [('display_input.theme', 'dark')]

    store.reset('display_input.theme')
    assert store.is_default('display_input.theme')
    assert len(seen) == 2
    assert seen[-1].new == PREFERENCE_DEFINITIONS['display_input.theme'].default


def test_corrupt_file_falls_back_to_defaults(tmp_path) -> None:
    path = tmp_path / 'prefs.json'
    path.write_text('{ not json', encoding='utf-8')
    store = ApplicationPreferenceStore(path)
    assert store.load_error is not None
    assert store.is_default('display_input.theme')


def test_foreign_keys_ignored_on_load(tmp_path) -> None:
    path = tmp_path / 'prefs.json'
    path.write_text(
        json.dumps(
            {
                'schema_version': 1,
                'values': {
                    'display_input.theme': 'light',
                    'evil.key': True,
                    'integrations.rew_port': 'not-a-port',
                },
            }
        ),
        encoding='utf-8',
    )
    store = ApplicationPreferenceStore(path)
    assert store.get('display_input.theme') == 'light'
    assert 'evil.key' not in store.snapshot().values
    assert store.is_default('integrations.rew_port')
    assert store.load_error is not None  # invalid stored value flagged


def test_update_is_atomic_on_validation(tmp_path) -> None:
    store = ApplicationPreferenceStore(tmp_path / 'prefs.json')
    with pytest.raises(PreferenceValueError):
        store.update({'display_input.theme': 'dark', 'general.language': 'xx'})
    assert store.is_default('display_input.theme')


def test_language_change_requires_restart() -> None:
    assert PREFERENCE_DEFINITIONS['general.language'].restart_required
    assert not PREFERENCE_DEFINITIONS['display_input.theme'].restart_required


def test_preferences_model_is_frozen_and_validates() -> None:
    prefs = ApplicationPreferences(values={'display_input.theme': 'dark'})
    assert prefs.values['display_input.theme'] == 'dark'
    with pytest.raises(ValidationError):
        ApplicationPreferences(values={'rogue.key': 1})
    with pytest.raises(ValidationError):
        ApplicationPreferences(values={'integrations.rew_port': -1})


def test_update_commits_one_atomic_write(tmp_path) -> None:
    """#742: a multi-key Apply performs exactly one durable write."""
    path = tmp_path / 'prefs.json'
    store = ApplicationPreferenceStore(path)
    writes: list[dict] = []
    original = store._persist_values
    def counting(values):
        writes.append(dict(values))
        return original(values)
    store._persist_values = counting  # type: ignore[assignment]

    seen: list[PreferenceChange] = []
    store.subscribe(seen.append)
    changes = store.update({
        'display_input.theme': 'dark',
        'integrations.rew_port': 9000,
        'display_input.length_unit': 'cm',
    })

    assert len(writes) == 1
    assert writes[0] == {
        'display_input.theme': 'dark',
        'integrations.rew_port': 9000,
        'display_input.length_unit': 'cm',
    }
    assert {c.key for c in changes} == set(writes[0])
    assert {c.key for c in seen} == set(writes[0])
    reloaded = ApplicationPreferenceStore(path)
    assert reloaded.get('display_input.theme') == 'dark'
    assert reloaded.get('integrations.rew_port') == 9000


def test_persist_failure_leaves_memory_and_disk_unchanged(tmp_path) -> None:
    """#742: injected persistence failure must not half-apply."""
    path = tmp_path / 'prefs.json'
    store = ApplicationPreferenceStore(path)
    store.set('display_input.theme', 'dark')
    prior_disk = path.read_text(encoding='utf-8')

    seen: list[PreferenceChange] = []
    store.subscribe(seen.append)

    def broken(values):
        raise OSError('disk full')

    store._persist_values = broken  # type: ignore[assignment]
    with pytest.raises(OSError):
        store.update({
            'display_input.theme': 'light',
            'integrations.rew_port': 9000,
        })
    assert store.get('display_input.theme') == 'dark'
    assert store.get('integrations.rew_port') == 4735
    assert seen == []
    assert path.read_text(encoding='utf-8') == prior_disk

    # Single-key set() shares the same atomic path: no mutation before
    # durable replace succeeds.
    with pytest.raises(OSError):
        store.set('integrations.rew_port', 1)
    assert store.get('integrations.rew_port') == 4735
    assert seen == []


def test_listener_failure_cannot_create_partial_state(tmp_path) -> None:
    """#742: a post-commit listener error keeps the durable commit honest."""
    path = tmp_path / 'prefs.json'
    store = ApplicationPreferenceStore(path)

    def boom(change):
        raise RuntimeError('observer failed')

    seen: list[PreferenceChange] = []
    store.subscribe(boom)
    store.subscribe(seen.append)
    with pytest.raises(PreferenceNotificationError):
        store.update({'display_input.theme': 'dark', 'integrations.rew_port': 9000})
    # Commit was still durable and every listener ran.
    assert {c.key for c in seen} == {'display_input.theme', 'integrations.rew_port'}
    reloaded = ApplicationPreferenceStore(path)
    assert reloaded.get('display_input.theme') == 'dark'
    assert reloaded.get('integrations.rew_port') == 9000


def test_update_removes_defaults_in_same_atomic_write(tmp_path) -> None:
    path = tmp_path / 'prefs.json'
    store = ApplicationPreferenceStore(path)
    store.set('display_input.theme', 'dark')
    store.set('integrations.rew_port', 9000)

    changes = store.update({
        'display_input.theme': 'system',
        'display_input.reduced_motion': True,
    })
    assert {c.key for c in changes} == {
        'display_input.theme',
        'display_input.reduced_motion',
    }
    assert store.is_default('display_input.theme')
    payload = json.loads(path.read_text(encoding='utf-8'))
    assert 'display_input.theme' not in payload['values']
    assert payload['values']['display_input.reduced_motion'] is True
