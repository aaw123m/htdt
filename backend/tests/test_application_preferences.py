"""#591 application preferences tests."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from htdt.application_preferences import (
    ApplicationPreferences,
    ApplicationPreferenceStore,
    IncompatiblePreferencesError,
    PREFERENCES_SCHEMA_VERSION,
    PREFERENCE_DEFINITIONS,
    PreferenceChange,
    PreferenceLoadState,
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


def _write_payload(path, schema_version: int, values: dict) -> None:
    path.write_text(
        json.dumps(
            {
                'schema_version': schema_version,
                'authority': 'htdt-application-preferences',
                'values': values,
            }
        ),
        encoding='utf-8',
    )


def test_unknown_future_key_preserved_on_rewrite(tmp_path) -> None:
    path = tmp_path / 'prefs.json'
    _write_payload(
        path, PREFERENCES_SCHEMA_VERSION, {'future.new_feature': True}
    )
    store = ApplicationPreferenceStore(path)

    assert store.load_state == PreferenceLoadState.OK
    # The future key is never applied or exposed as an effective preference.
    assert 'future.new_feature' not in store.snapshot().values
    assert store.get('display_input.theme') == 'system'

    store.set('display_input.theme', 'dark')
    persisted = json.loads(path.read_text(encoding='utf-8'))
    assert persisted['values']['future.new_feature'] is True
    assert persisted['values']['display_input.theme'] == 'dark'
    # ...and a reload still never applies it.
    assert 'future.new_feature' not in ApplicationPreferenceStore(path).snapshot().values


def test_unserializable_opaque_key_dropped_not_poisoned(tmp_path) -> None:
    # json.loads admits bare NaN/Infinity, which the strict canonical writer
    # later refuses. A poisoned unknown key must not make every save raise.
    path = tmp_path / 'prefs.json'
    path.write_text(
        '{"schema_version": %d, "authority": "htdt-application-preferences",'
        ' "values": {"future.good": 1, "future.bad": NaN}}'
        % PREFERENCES_SCHEMA_VERSION,
        encoding='utf-8',
    )
    store = ApplicationPreferenceStore(path)

    assert store.load_state == PreferenceLoadState.PARTIAL_INVALID_VALUE
    assert store.load_error is not None
    assert store.write_allowed
    # Persisting drops the unserializable key and keeps the healthy opaque one.
    store.set('display_input.theme', 'dark')
    persisted = path.read_text(encoding='utf-8')
    assert 'future.bad' not in persisted
    persisted_payload = json.loads(persisted)
    assert persisted_payload['values']['future.good'] == 1
    assert persisted_payload['values']['display_input.theme'] == 'dark'


def test_newer_schema_blocks_durable_writes(tmp_path) -> None:
    path = tmp_path / 'prefs.json'
    _write_payload(path, PREFERENCES_SCHEMA_VERSION + 1, {'display_input.theme': 'dark'})
    store = ApplicationPreferenceStore(path)

    # Safe startup fallback: defaults, typed state, no destruction.
    assert store.load_state == PreferenceLoadState.INCOMPATIBLE_NEWER_SCHEMA
    assert store.load_error is not None
    assert store.is_default('display_input.theme')
    assert not store.write_allowed
    before = path.read_text(encoding='utf-8')
    with pytest.raises(IncompatiblePreferencesError):
        store.set('display_input.theme', 'light')
    with pytest.raises(IncompatiblePreferencesError):
        store.reset('display_input.theme')
    with pytest.raises(IncompatiblePreferencesError):
        store.update({'display_input.theme': 'light'})
    assert path.read_text(encoding='utf-8') == before


def test_reset_persisted_file_preserves_newer_file(tmp_path) -> None:
    path = tmp_path / 'prefs.json'
    _write_payload(path, PREFERENCES_SCHEMA_VERSION + 1, {'future.key': 7})
    store = ApplicationPreferenceStore(path)

    recovery = store.reset_persisted_file()
    assert recovery is not None and recovery.exists()
    preserved = json.loads(recovery.read_text(encoding='utf-8'))
    assert preserved['schema_version'] == PREFERENCES_SCHEMA_VERSION + 1
    assert preserved['values'] == {'future.key': 7}

    fresh = json.loads(path.read_text(encoding='utf-8'))
    assert fresh['schema_version'] == PREFERENCES_SCHEMA_VERSION
    assert store.load_state == PreferenceLoadState.MISSING
    store.set('display_input.theme', 'dark')  # writes resume after reset
    assert json.loads(path.read_text(encoding='utf-8'))['values'][
        'display_input.theme'
    ] == 'dark'


def test_corrupt_file_refuses_silent_overwrite(tmp_path) -> None:
    path = tmp_path / 'prefs.json'
    path.write_text('{ not json', encoding='utf-8')
    store = ApplicationPreferenceStore(path)
    assert store.load_state == PreferenceLoadState.CORRUPT
    before = path.read_text(encoding='utf-8')
    with pytest.raises(IncompatiblePreferencesError):
        store.set('display_input.theme', 'dark')
    assert path.read_text(encoding='utf-8') == before

    recovery = store.reset_persisted_file()
    assert recovery is not None and recovery.read_text(encoding='utf-8') == before


def test_partial_invalid_value_stays_writable(tmp_path) -> None:
    path = tmp_path / 'prefs.json'
    _write_payload(
        path,
        PREFERENCES_SCHEMA_VERSION,
        {'integrations.rew_port': 'not-a-port', 'future.key': 'x'},
    )
    store = ApplicationPreferenceStore(path)
    assert store.load_state == PreferenceLoadState.PARTIAL_INVALID_VALUE
    assert store.write_allowed
    store.set('display_input.theme', 'dark')
    persisted = json.loads(path.read_text(encoding='utf-8'))
    assert persisted['values']['display_input.theme'] == 'dark'
    # The invalid known value is dropped; the opaque future key survives.
    assert 'integrations.rew_port' not in persisted['values']
    assert persisted['values']['future.key'] == 'x'


def test_failed_single_key_persist_keeps_memory_and_disk_aligned(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / 'prefs.json'
    store = ApplicationPreferenceStore(path)
    store.set('display_input.theme', 'dark')
    before = path.read_text(encoding='utf-8')

    def boom(self) -> None:
        raise OSError('disk full')

    monkeypatch.setattr(
        ApplicationPreferenceStore, '_persist', boom
    )
    with pytest.raises(OSError):
        store.set('display_input.theme', 'light')
    monkeypatch.undo()
    # Memory fell back to the on-disk state — no split.
    assert store.get('display_input.theme') == 'dark'
    assert path.read_text(encoding='utf-8') == before


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
        store.set('integrations.rew_port', 9000)
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


def test_deeply_nested_file_falls_back_to_corrupt_state(tmp_path) -> None:
    # json.loads reports deeply nested input as RecursionError, which
    # previously aborted startup instead of degrading to CORRUPT (#REV28).
    path = tmp_path / 'prefs.json'
    path.write_text('[' * 3000 + ']' * 3000, encoding='utf-8')
    store = ApplicationPreferenceStore(path)
    assert store.load_state == PreferenceLoadState.CORRUPT
    assert store.is_default('general.language')
