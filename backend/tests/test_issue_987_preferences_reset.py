"""#987: すべて既定値に戻す must confirm a diff before mutating the store.

Pre-fix the button committed every registered default immediately — one
misclick rewrote integrations/compute/UI settings with no review. Now the
operator sees only the keys that would change (current → default, effect),
may reset a chosen category subset, cancel with zero writes, and can
restore the pre-reset values via 元の設定に戻す. The sanctioned recovery
for a corrupt/newer-schema file also warns before discarding.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip('PySide6')

from PySide6.QtWidgets import QApplication

from htdt.application_preferences import (
    PREFERENCES_SCHEMA_VERSION,
    ApplicationPreferenceStore,
    PreferenceCategory,
)
from htdt.workflow_settings import PreferencesWidget


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _changed_store(tmp_path: Path) -> ApplicationPreferenceStore:
    store = ApplicationPreferenceStore(tmp_path / 'prefs.json')
    store.set('display_input.reduced_motion', True)
    store.set('display_input.length_unit', 'cm')
    store.set('integrations.rew_port', 9000)
    return store


def test_reset_click_does_not_mutate_without_confirmation(tmp_path: Path) -> None:
    _app()
    store = _changed_store(tmp_path)
    widget = PreferencesWidget(store)
    try:
        # Without a confirmed choice the button must not write anything —
        # simulate the operator cancelling the diff dialog.
        widget._confirm_reset = lambda pending: None
        widget.reset_button.click()
        assert store.get('display_input.reduced_motion') is True
        assert store.get('display_input.length_unit') == 'cm'
        assert store.get('integrations.rew_port') == 9000
        assert 'キャンセル' in widget.status.text()
    finally:
        widget.deleteLater()


def test_reset_all_categories_via_confirmation(tmp_path: Path) -> None:
    _app()
    store = _changed_store(tmp_path)
    widget = PreferencesWidget(store)
    try:
        all_keys = frozenset(
            d.key
            for d, _current in widget._reset_candidates()
        )
        widget._confirm_reset = lambda pending: all_keys
        widget.reset_button.click()
        assert store.get('display_input.reduced_motion') is False
        assert store.get('display_input.length_unit') == 'mm'
        assert store.get('integrations.rew_port') == 4735
        assert widget.rollback_button.isVisibleTo(widget)
        assert '件を既定値に戻しました' in widget.status.text()
    finally:
        widget.deleteLater()


def test_reset_selected_category_only(tmp_path: Path) -> None:
    _app()
    store = _changed_store(tmp_path)
    widget = PreferencesWidget(store)
    try:
        display_keys = frozenset(
            d.key
            for d, _c in widget._reset_candidates()
            if d.category == PreferenceCategory.DISPLAY_INPUT
        )
        widget._confirm_reset = lambda pending: display_keys
        widget.reset_button.click()
        assert store.get('display_input.reduced_motion') is False
        assert store.get('display_input.length_unit') == 'mm'
        # Integrations untouched — selected categories only.
        assert store.get('integrations.rew_port') == 9000
    finally:
        widget.deleteLater()


def test_no_changes_reports_without_dialog(tmp_path: Path) -> None:
    _app()
    store = ApplicationPreferenceStore(tmp_path / 'prefs.json')
    widget = PreferencesWidget(store)
    try:
        called = []
        widget._confirm_reset = lambda pending: called.append(pending) or frozenset()
        widget.reset_button.click()
        assert called == []  # never opened the dialog
        assert 'すべて既定値' in widget.status.text()
    finally:
        widget.deleteLater()


def test_rollback_restores_prestored_values(tmp_path: Path) -> None:
    _app()
    store = _changed_store(tmp_path)
    widget = PreferencesWidget(store)
    try:
        widget._confirm_reset = (
            lambda pending: frozenset(d.key for d, _c in pending)
        )
        widget.reset_button.click()
        assert store.get('integrations.rew_port') == 4735

        widget.rollback_button.click()
        assert store.get('display_input.reduced_motion') is True
        assert store.get('display_input.length_unit') == 'cm'
        assert store.get('integrations.rew_port') == 9000
        assert '元の設定に戻しました' in widget.status.text()
    finally:
        widget.deleteLater()


def test_notification_failure_reports_saved_not_rolled_back(
    tmp_path: Path,
) -> None:
    _app()
    store = _changed_store(tmp_path)
    store.subscribe(lambda change: (_ for _ in ()).throw(RuntimeError('listener boom')))
    widget = PreferencesWidget(store)
    try:
        widget._confirm_reset = (
            lambda pending: frozenset(d.key for d, _c in pending)
        )
        widget.reset_button.click()
        # Durable commit succeeded — only listener notification failed.
        assert store.get('integrations.rew_port') == 4735
        assert '保存されましたが、一部の画面への反映に失敗しました' in widget.status.text()
        # Rollback still possible for the persisted doc.
        assert widget._rollback_snapshot is not None
    finally:
        widget.deleteLater()


def test_sanctioned_reset_requires_confirmation(tmp_path: Path) -> None:
    _app()
    path = tmp_path / 'prefs.json'
    path.write_text(
        json.dumps(
            {
                'schema_version': PREFERENCES_SCHEMA_VERSION + 1,
                'values': {'general.language': 'ja'},
            }
        ),
        encoding='utf-8',
    )
    store = ApplicationPreferenceStore(path)
    widget = PreferencesWidget(store)
    try:
        assert not store.write_allowed
        # Cancelled warning → no reset, no recovery file.
        widget._confirm_sanctioned_reset = lambda: False
        widget.reset_button.click()
        assert not store.write_allowed
        assert not path.with_name(path.name + '.recovery').exists()

        # Accepted → sanctioned reset preserves the old document.
        widget._confirm_sanctioned_reset = lambda: True
        widget.reset_button.click()
        assert store.write_allowed
        assert path.with_name(path.name + '.recovery').is_file()
        assert '復旧用' in widget.status.text() or '保存されました' in widget.status.text()
    finally:
        widget.deleteLater()


def test_reset_candidates_lists_only_changed_keys(tmp_path: Path) -> None:
    _app()
    store = _changed_store(tmp_path)
    widget = PreferencesWidget(store)
    try:
        pending = widget._reset_candidates()
        assert len(store.definitions()) > 3
        assert {d.key for d, _c in pending} == {
            'display_input.reduced_motion',
            'display_input.length_unit',
            'integrations.rew_port',
        }
    finally:
        widget.deleteLater()


def test_reset_dialog_category_selection(tmp_path: Path) -> None:
    _app()
    from htdt.workflow_settings import PreferencesResetDialog

    store = _changed_store(tmp_path)
    pending = tuple(
        (d, store.get(d.key))
        for d in store.definitions()
        if store.get(d.key) != d.default
    )
    assert len(pending) == 3
    dialog = PreferencesResetDialog(pending)
    try:
        # Uncheck integrations → chosen keys cover only display_input.
        check = dialog._category_checks[PreferenceCategory.INTEGRATIONS]
        check.setChecked(False)
        dialog._accept_selected()
        keys = dialog.chosen_keys()
        assert keys is not None
        assert 'integrations.rew_port' not in keys
        assert 'display_input.reduced_motion' in keys
        assert 'display_input.length_unit' in keys

        # accept_all covers everything regardless of checkbox state.
        dialog._accept_all()
        assert 'integrations.rew_port' in dialog.chosen_keys()
    finally:
        dialog.deleteLater()
