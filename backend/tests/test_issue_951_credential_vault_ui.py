"""#951 credential vault operator UI — metadata-only list, consent-gated
store, rotate/revoke/delete, reference picker (IDs only), audit surface,
honest locked/unavailable states, and secret-material non-leakage."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest


DOC = 'doc-ui-1'
SECRET = 's3cr3t-ui-p@ssw0rd!'


@pytest.fixture()
def repo(tmp_path: Path):
    from htdt.cad_credential_vault_repository import (
        CadCredentialVaultRepository,
    )
    from htdt.cad_repository import SceneRepository
    return CadCredentialVaultRepository(
        SceneRepository(tmp_path / 'cad-scenes.sqlite3'))


@pytest.fixture()
def vault():
    from htdt.cad_credential_vault import MemoryCredentialVault
    return MemoryCredentialVault()


@pytest.fixture()
def service(repo, vault):
    from htdt.cad_credential_vault import CredentialVaultService
    return CredentialVaultService(repo, vault)


def _app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _panel(service, document_id=DOC):
    from htdt.credential_vault_panel import CredentialVaultPanel
    _app()
    return CredentialVaultPanel(service, document_id)


def _store(service, doc=DOC, **kwargs):
    service.record_consent(doc, 'operator')
    return service.store_credential(
        doc,
        scope_kind='device',
        scope_ref='avr-1',
        credential_type='password',
        material=SECRET,
        actor='operator',
        **kwargs,
    )


def _tree_text(tree) -> str:
    parts = []
    for i in range(tree.topLevelItemCount()):
        item = tree.topLevelItem(i)
        parts.extend(
            item.text(c) for c in range(tree.columnCount()))
    return '\n'.join(parts)


# ----------------------------------------------------------------------
# store dialog — consent gating + secret handling


class TestStoreDialog:
    def test_ok_requires_all_fields(self) -> None:
        from htdt.credential_vault_panel import CredentialStoreDialog
        _app()
        dialog = CredentialStoreDialog(consent_needed=True)
        ok = dialog._buttons.button(
            dialog._buttons.StandardButton.Ok)
        assert not ok.isEnabled()
        dialog.scope_ref_edit.setText('avr-1')
        assert not ok.isEnabled()
        dialog.secret_edit.setText(SECRET)
        assert not ok.isEnabled()
        dialog.operator_edit.setText('op-1')
        assert not ok.isEnabled()  # consent still unticked
        dialog.consent_check.setChecked(True)
        assert ok.isEnabled()

    def test_no_consent_checkbox_when_already_recorded(self) -> None:
        from htdt.credential_vault_panel import CredentialStoreDialog
        _app()
        dialog = CredentialStoreDialog(consent_needed=False)
        assert not dialog.consent_check.isVisible()
        dialog.scope_ref_edit.setText('avr-1')
        dialog.secret_edit.setText(SECRET)
        dialog.operator_edit.setText('op-1')
        ok = dialog._buttons.button(
            dialog._buttons.StandardButton.Ok)
        assert ok.isEnabled()

    def test_secret_echo_and_take_secret_clears(self) -> None:
        from PySide6.QtWidgets import QLineEdit
        from htdt.credential_vault_panel import CredentialStoreDialog
        _app()
        dialog = CredentialStoreDialog(consent_needed=False)
        assert dialog.secret_edit.echoMode() == (
            QLineEdit.EchoMode.Password)
        dialog.secret_edit.setText(SECRET)
        assert dialog.secret_edit.text() == SECRET
        assert dialog.take_secret() == SECRET
        assert dialog.secret_edit.text() == ''

    def test_secret_never_renders_in_dialog_text(self) -> None:
        from htdt.credential_vault_panel import CredentialStoreDialog
        _app()
        dialog = CredentialStoreDialog(consent_needed=True)
        for label in dialog.findChildren(type(dialog.consent_check)):
            assert SECRET not in label.text()
        for child in dialog.findChildren(
                type(dialog.scope_ref_edit)):
            if child is dialog.secret_edit:
                continue
            assert SECRET not in child.text()


# ----------------------------------------------------------------------
# panel — list, consent banner, store flow


class TestPanel:
    def test_empty_document_renders_honestly(self, service) -> None:
        panel = _panel(service)
        assert panel.reference_tree.topLevelItemCount() == 0
        assert '未記録' in panel.consent_label.text()
        assert '利用可能' in panel.vault_label.text()
        assert panel.store_button.isEnabled()

    def test_store_dialog_records_consent_and_stores(
            self, service, repo, monkeypatch) -> None:
        from PySide6.QtWidgets import QDialog
        from htdt import credential_vault_panel as panel_mod

        panel = _panel(service)
        captured = {}

        class _Store:
            DialogCode = QDialog.DialogCode

            def __init__(self, *, consent_needed, parent=None):
                captured['consent_needed'] = consent_needed

            def exec(self):
                return QDialog.DialogCode.Accepted

            scope_kind = 'device'
            scope_ref = 'avr-1'
            credential_type = 'password'
            vault_scope = 'user'
            identity_hint = 'AVR login'
            operator_id = 'op-9'
            consent_requested = True

            def take_secret(self):
                return SECRET

        monkeypatch.setattr(panel_mod, 'CredentialStoreDialog', _Store)
        panel.store_button.click()
        assert captured['consent_needed'] is True
        references = service.list_references(DOC)
        assert len(references) == 1
        assert references[0].credential_id.startswith('crid-')
        # consent sealed BEFORE the store event
        kinds = [e.event_kind for e in service.list_events(DOC)]
        assert kinds == ['consent_recorded', 'stored']
        assert '登録しました' in panel.result_label.text()

    def test_list_shows_metadata_only(self, service) -> None:
        reference = _store(service)
        panel = _panel(service)
        assert panel.reference_tree.topLevelItemCount() == 1
        text = _tree_text(panel.reference_tree)
        assert reference.credential_id in text
        assert 'avr-1' in text
        assert SECRET not in text
        assert SECRET not in _tree_text(panel.audit_tree)

    def test_second_store_skips_consent(
            self, service, monkeypatch) -> None:
        from PySide6.QtWidgets import QDialog
        from htdt import credential_vault_panel as panel_mod
        _store(service)
        panel = _panel(service)
        captured = {}

        class _Store:
            DialogCode = QDialog.DialogCode

            def __init__(self, *, consent_needed, parent=None):
                captured['consent_needed'] = consent_needed

            def exec(self):
                return QDialog.DialogCode.Rejected

        monkeypatch.setattr(panel_mod, 'CredentialStoreDialog', _Store)
        panel.store_button.click()
        assert captured['consent_needed'] is False

    def test_rotate_dialog_updates_version(
            self, service, monkeypatch) -> None:
        from PySide6.QtWidgets import QDialog
        from htdt import credential_vault_panel as panel_mod
        reference = _store(service)
        panel = _panel(service)
        panel.reference_tree.topLevelItem(0).setSelected(True)
        panel.reference_tree.setCurrentItem(
            panel.reference_tree.topLevelItem(0))

        class _Rotate:
            DialogCode = QDialog.DialogCode

            def __init__(self, ref, parent=None):
                self.ref = ref

            def exec(self):
                return QDialog.DialogCode.Accepted

            operator_id = 'op-2'

            def take_secret(self):
                return 'n3w-s3cr3t!'

        monkeypatch.setattr(
            panel_mod, 'CredentialRotateDialog', _Rotate)
        panel.rotate_button.click()
        current = repo_current(service, reference.credential_id)
        assert current.version == 2
        assert current.state == 'active'
        assert '版 2' in panel.result_label.text()
        # new material retrievable under the same credential_id
        assert service.retrieve(
            reference.credential_id).reveal() == 'n3w-s3cr3t!'

    def test_revoke_and_delete_confirm_dialogs(
            self, service, monkeypatch) -> None:
        from PySide6.QtWidgets import QDialog
        from htdt import credential_vault_panel as panel_mod
        from htdt.cad_credential_vault import CredentialAuthRequiredError
        reference = _store(service)
        panel = _panel(service)

        class _Confirm:
            DialogCode = QDialog.DialogCode
            seen_actions = []

            def __init__(self, ref, *, action, parent=None):
                self.action = action
                _Confirm.seen_actions.append(action)

            def exec(self):
                return QDialog.DialogCode.Accepted

            operator_id = 'op-3'
            reason = 'rotate-out'

        monkeypatch.setattr(
            panel_mod, 'CredentialConfirmDialog', _Confirm)
        panel.reference_tree.setCurrentItem(
            panel.reference_tree.topLevelItem(0))
        panel.revoke_button.click()
        current = repo_current(service, reference.credential_id)
        assert current.state == 'revoked'
        with pytest.raises(CredentialAuthRequiredError):
            service.retrieve(reference.credential_id)
        assert '失効' in panel.result_label.text()

        panel.refresh()
        panel.reference_tree.setCurrentItem(
            panel.reference_tree.topLevelItem(0))
        panel.delete_button.click()
        current = repo_current(service, reference.credential_id)
        assert current.state == 'deleted'
        assert _Confirm.seen_actions == ['revoke', 'delete']

    def test_no_selection_disables_mutations(self, service) -> None:
        _store(service)
        panel = _panel(service)
        assert not panel.rotate_button.isEnabled()
        assert not panel.revoke_button.isEnabled()
        assert not panel.delete_button.isEnabled()
        assert not panel.verify_button.isEnabled()


def repo_current(service, credential_id):
    # reference rows are append-only per credential_id; the current row
    # is the highest version
    matches = [
        reference for reference in service.list_references(DOC)
        if reference.credential_id == credential_id
    ]
    assert matches, 'reference not found'
    return max(matches, key=lambda r: r.version)


# ----------------------------------------------------------------------
# picker — returns IDs, honours scope drift


class TestPicker:
    def test_picker_returns_credential_id_only(self, service) -> None:
        from htdt.credential_vault_panel import (
            CredentialReferencePickerDialog,
        )
        reference = _store(service)
        _app()
        dialog = CredentialReferencePickerDialog(service, DOC)
        assert dialog.tree.topLevelItemCount() == 1
        ok = dialog._buttons.button(
            dialog._buttons.StandardButton.Ok)
        assert not ok.isEnabled()
        dialog.tree.setCurrentItem(dialog.tree.topLevelItem(0))
        assert ok.isEnabled()
        dialog.accept()
        assert dialog.selected_credential_id == (
            reference.credential_id)
        assert SECRET not in _tree_text(dialog.tree)

    def test_picker_hides_revoked_and_deleted(self, service) -> None:
        from htdt.credential_vault_panel import (
            CredentialReferencePickerDialog,
        )
        first = _store(service)
        service.store_credential(
            DOC,
            scope_kind='device', scope_ref='avr-2',
            credential_type='api_token',
            material='other-secret', actor='operator',
        )
        service.revoke(first.credential_id, actor='operator')
        _app()
        dialog = CredentialReferencePickerDialog(service, DOC)
        assert dialog.tree.topLevelItemCount() == 1
        text = _tree_text(dialog.tree)
        assert first.credential_id not in text
        assert 'avr-2' in text

    def test_picker_blocks_scope_drift(self, service) -> None:
        from htdt.credential_vault_panel import (
            CredentialReferencePickerDialog,
        )
        _store(service)
        _app()
        dialog = CredentialReferencePickerDialog(
            service, DOC,
            expected_scope_kind='device',
            expected_scope_ref='display-1',
        )
        dialog.tree.setCurrentItem(dialog.tree.topLevelItem(0))
        ok = dialog._buttons.button(
            dialog._buttons.StandardButton.Ok)
        assert not ok.isEnabled()
        assert 'スコープが一致しません' in dialog.status_label.text()
        dialog.accept()  # blocked — stays None
        assert dialog.selected_credential_id is None

    def test_picker_accepts_matching_scope(self, service) -> None:
        from htdt.credential_vault_panel import (
            CredentialReferencePickerDialog,
        )
        reference = _store(service)
        _app()
        dialog = CredentialReferencePickerDialog(
            service, DOC,
            expected_scope_kind='device',
            expected_scope_ref='avr-1',
        )
        dialog.tree.setCurrentItem(dialog.tree.topLevelItem(0))
        ok = dialog._buttons.button(
            dialog._buttons.StandardButton.Ok)
        assert ok.isEnabled()
        dialog.accept()
        assert dialog.selected_credential_id == (
            reference.credential_id)


# ----------------------------------------------------------------------
# honest vault states


class TestVaultStates:
    def test_locked_vault_disables_material_ops(
            self, service, vault) -> None:
        _store(service)
        vault.set_locked(True)
        panel = _panel(service)
        assert 'ロック' in panel.vault_label.text()
        assert not panel.store_button.isEnabled()
        panel.reference_tree.setCurrentItem(
            panel.reference_tree.topLevelItem(0))
        panel._sync_mutation_enablement()
        assert not panel.rotate_button.isEnabled()
        assert not panel.revoke_button.isEnabled()
        assert not panel.delete_button.isEnabled()
        # metadata list + audit still readable
        assert panel.reference_tree.topLevelItemCount() == 1
        assert panel.audit_tree.topLevelItemCount() >= 2

    def test_verify_reports_actionable_error_on_locked(
            self, service, vault) -> None:
        reference = _store(service)
        vault.set_locked(True)
        panel = _panel(service)
        panel.reference_tree.setCurrentItem(
            panel.reference_tree.topLevelItem(0))
        panel.verify_button.click()
        assert panel.result_label.text()
        assert SECRET not in panel.result_label.text()
        vault.set_locked(False)
        panel.verify_button.click()
        assert '取得可能' in panel.result_label.text()

    def test_unavailable_vault_honest_stub(self, repo) -> None:
        from htdt.cad_credential_vault import (
            CredentialVaultService,
            UnavailableCredentialVault,
        )
        service = CredentialVaultService(
            repo, UnavailableCredentialVault())
        panel = _panel(service)
        assert '利用不可' in panel.vault_label.text()
        assert not panel.store_button.isEnabled()
        assert not panel.rotate_button.isEnabled()


# ----------------------------------------------------------------------
# audit surface + redaction guarantees


class TestAuditAndRedaction:
    def test_audit_tree_lists_lifecycle_events(self, service) -> None:
        reference = _store(service)
        service.retrieve(reference.credential_id, actor='adapter')
        panel = _panel(service)
        kinds = [
            panel.audit_tree.topLevelItem(i).text(1)
            for i in range(panel.audit_tree.topLevelItemCount())
        ]
        assert '同意記録' in kinds
        assert '登録' in kinds
        assert '利用' in kinds
        assert SECRET not in _tree_text(panel.audit_tree)

    def test_copy_id_puts_only_the_id_on_clipboard(
            self, service) -> None:
        from PySide6.QtWidgets import QApplication
        reference = _store(service)
        panel = _panel(service)
        panel.reference_tree.setCurrentItem(
            panel.reference_tree.topLevelItem(0))
        panel.copy_id_button.click()
        clip = QApplication.clipboard().text()
        assert clip == reference.credential_id
        assert SECRET not in clip

    def test_manifest_copy_has_no_secret_material(
            self, service) -> None:
        from PySide6.QtWidgets import QApplication
        _store(service)
        panel = _panel(service)
        panel.copy_manifest_button.click()
        clip = QApplication.clipboard().text()
        assert SECRET not in clip
        payload = json.loads(clip)
        assert payload and payload[0]['credential_id']
        # vault_key is intentionally excluded from the manifest
        assert 'vault_key' not in payload[0]

    def test_database_never_persists_material(
            self, service, repo) -> None:
        _store(service)
        with sqlite3.connect(repo.path) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_credential_references'
            ).fetchall()
            assert rows
            for (payload,) in rows:
                assert SECRET not in payload
            events = connection.execute(
                'SELECT payload_json '
                'FROM cad_credential_lifecycle_events'
            ).fetchall()
            for (payload,) in events:
                assert SECRET not in payload

    def test_export_manifest_repr_has_no_secret(
            self, service) -> None:
        _store(service)
        manifest = service.export_manifest(DOC)
        assert SECRET not in repr(manifest)


# ----------------------------------------------------------------------
# compat: resolver + old-project migration


class TestCompat:
    def test_resolver_accepts_picked_credential_id(
            self, service) -> None:
        from htdt.credential_vault_panel import (
            CredentialReferencePickerDialog,
        )
        reference = _store(service)
        _app()
        dialog = CredentialReferencePickerDialog(service, DOC)
        dialog.tree.setCurrentItem(dialog.tree.topLevelItem(0))
        dialog.accept()
        resolve = service.resolver(actor='adapter')
        material = resolve(dialog.selected_credential_id)
        assert material.reveal() == SECRET

    def test_old_project_without_credentials_renders_empty(
            self, service) -> None:
        # A document created before the vault feature has no consent,
        # references or events — the panel renders an honest empty
        # state and the first store still requires consent.
        panel = _panel(service, document_id='legacy-doc')
        assert panel.reference_tree.topLevelItemCount() == 0
        assert panel.audit_tree.topLevelItemCount() == 0
        assert '未記録' in panel.consent_label.text()
        assert panel.store_button.isEnabled()
