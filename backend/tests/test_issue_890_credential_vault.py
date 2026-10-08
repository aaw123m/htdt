"""#890 credential vault authority — lifecycle, fail-closed vault states,
redaction, rotation-stability and export safety."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import sys

import pytest

from htdt.cad_credential_vault import (
    CredentialAuthRequiredError,
    CredentialConsentRequiredError,
    CredentialLifecycleEvent,
    CredentialReference,
    CredentialReferenceError,
    CredentialVaultService,
    DpapiCredentialVault,
    MemoryCredentialVault,
    SecretMaterial,
    UnavailableCredentialVault,
    platform_vault,
)
from htdt.cad_credential_vault_repository import (
    CadCredentialVaultRepository,
)
from htdt.cad_calibration_deployment_repository import (
    DeploymentIntegrityError,
)
from htdt.cad_repository import SceneRepository


@pytest.fixture()
def repo(tmp_path: Path) -> CadCredentialVaultRepository:
    return CadCredentialVaultRepository(
        SceneRepository(tmp_path / 'cad-scenes.sqlite3')
    )


@pytest.fixture()
def service(repo):
    return CredentialVaultService(repo, MemoryCredentialVault())


def _consent_and_store(
    service: CredentialVaultService, doc: str = 'doc-1',
    **kwargs,
) -> CredentialReference:
    service.record_consent(doc, 'operator')
    return service.store_credential(
        doc,
        scope_kind='device',
        scope_ref='avr-1',
        credential_type='password',
        material='s3cr3t-p@ssw0rd!',
        actor='operator',
        **kwargs,
    )


def test_secret_material_is_redacted_by_default() -> None:
    material = SecretMaterial('hunter2')
    assert 'hunter2' not in repr(material)
    assert 'hunter2' not in str(material)
    assert material.reveal() == 'hunter2'
    assert material.use(lambda v: v.upper()) == 'HUNTER2'
    assert len(material.fingerprint()) == 64


def test_store_requires_prior_consent(service) -> None:
    with pytest.raises(CredentialConsentRequiredError):
        service.store_credential(
            'doc-1',
            scope_kind='device',
            scope_ref='avr-1',
            credential_type='password',
            material='x',
            actor='operator',
        )


def test_store_retrieve_roundtrip(service) -> None:
    reference = _consent_and_store(service)
    assert reference.state == 'active'
    assert reference.version == 1
    material = service.retrieve(reference.credential_id, actor='avr-adapter')
    assert material.reveal() == 's3cr3t-p@ssw0rd!'
    kinds = [e.event_kind for e in service._repo.list_events('doc-1')]
    assert kinds == ['consent_recorded', 'stored', 'used']


def test_no_secret_in_database(
    service, repo, tmp_path: Path,
) -> None:
    _consent_and_store(service)
    with sqlite3.connect(repo.path) as connection:
        for table in (
            'cad_credential_references',
            'cad_credential_lifecycle_events',
        ):
            for row in connection.execute(
                f'SELECT payload_json FROM {table}'
            ):
                assert 's3cr3t-p@ssw0rd!' not in row[0]


def test_locked_vault_fails_closed(service, repo) -> None:
    service._vault.set_locked(True)
    service.record_consent('doc-1', 'operator')
    with pytest.raises(CredentialAuthRequiredError, match='locked'):
        service.store_credential(
            'doc-1',
            scope_kind='device',
            scope_ref='avr-1',
            credential_type='password',
            material='x',
            actor='operator',
        )


def test_unavailable_vault_fails_closed(repo) -> None:
    service = CredentialVaultService(repo, UnavailableCredentialVault())
    service.record_consent('doc-1', 'operator')
    with pytest.raises(CredentialAuthRequiredError, match='unavailable'):
        service.store_credential(
            'doc-1',
            scope_kind='device',
            scope_ref='avr-1',
            credential_type='password',
            material='x',
            actor='operator',
        )


def test_missing_credential_is_actionable(service) -> None:
    with pytest.raises(CredentialAuthRequiredError, match='not stored'):
        service.retrieve('crid-nonexistent')


def test_rotation_keeps_credential_id_stable(service) -> None:
    reference = _consent_and_store(service)
    rotated = service.rotate(
        reference.credential_id, material='n3w-s3cr3t!', actor='operator',
    )
    assert rotated.credential_id == reference.credential_id
    assert rotated.version == 2
    assert rotated.supersedes_ref is not None
    assert rotated.supersedes_ref.ref_id == reference.reference_id
    assert service.retrieve(reference.credential_id).reveal() == 'n3w-s3cr3t!'
    # The original sealed reference still resolves (append-only).
    repo_ref = service._repo.get_reference(reference.reference_id)
    assert repo_ref is not None
    assert repo_ref.version == 1


def test_revoked_credential_denies_retrieval(service) -> None:
    reference = _consent_and_store(service)
    service.revoke(reference.credential_id, actor='operator')
    with pytest.raises(CredentialAuthRequiredError, match='revoked'):
        service.retrieve(reference.credential_id)
    kinds = [
        e.event_kind
        for e in service._repo.list_events(
            'doc-1', credential_id=reference.credential_id,
        )
    ]
    assert 'retrieve_denied' in kinds


def test_deleted_credential_denies_retrieval(service) -> None:
    reference = _consent_and_store(service)
    tombstone = service.delete(reference.credential_id, actor='operator')
    assert tombstone.state == 'deleted'
    assert not service._vault.contains(reference.vault_key)
    with pytest.raises(CredentialAuthRequiredError, match='deleted'):
        service.retrieve(reference.credential_id)


def test_rotate_revoked_rejected(service) -> None:
    reference = _consent_and_store(service)
    service.revoke(reference.credential_id, actor='operator')
    with pytest.raises(CredentialReferenceError, match='revoked'):
        service.rotate(reference.credential_id, material='x', actor='op')


def test_resolver_matches_transport_convention(service) -> None:
    reference = _consent_and_store(service)
    resolver = service.resolver(actor='hue-transport')
    # credential_id, reference_id and vault_key all resolve (never a value).
    assert resolver(reference.credential_id).reveal() == 's3cr3t-p@ssw0rd!'
    assert resolver(reference.reference_id).reveal() == 's3cr3t-p@ssw0rd!'
    assert resolver(reference.vault_key).reveal() == 's3cr3t-p@ssw0rd!'
    with pytest.raises(CredentialAuthRequiredError, match='does not resolve'):
        resolver('definitely-not-a-credential')


def test_redactor_strips_material(service) -> None:
    _consent_and_store(service)
    redactor = service.build_redactor('doc-1')
    transcript = (
        'PJLink auth handshake sent password s3cr3t-p@ssw0rd! to avr-1'
    )
    scrubbed = redactor.redact(transcript)
    assert 's3cr3t-p@ssw0rd!' not in scrubbed
    assert '«redacted:crid-' in scrubbed


def test_export_manifest_has_no_material(service) -> None:
    reference = _consent_and_store(service)
    manifest = service.export_manifest('doc-1')
    assert len(manifest) == 1
    entry = manifest[0]
    assert entry['credential_id'] == reference.credential_id
    assert 's3cr3t-p@ssw0rd!' not in repr(manifest)
    assert 'vault_key' not in entry  # opaque key stays out of exports


def test_identity_hint_rejects_secret_markers(service) -> None:
    service.record_consent('doc-1', 'operator')
    with pytest.raises(Exception, match='secret'):
        service.store_credential(
            'doc-1',
            scope_kind='device',
            scope_ref='avr-1',
            credential_type='password',
            material='x',
            actor='operator',
            identity_hint='password=abc123',
        )


def test_event_details_reject_secret_shaped_keys(service, repo) -> None:
    with pytest.raises(Exception, match='secret'):
        service._append_event(
            'doc-1', 'crid-x', 'used', 'actor',
            {'secret_value': 'nope'},
        )


def test_reference_seal_tamper_detected(service, repo) -> None:
    reference = _consent_and_store(service)
    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            'UPDATE cad_credential_references SET scope_ref=? '
            'WHERE reference_id=?',
            ('tampered', reference.reference_id),
        )
        connection.commit()
    with pytest.raises(DeploymentIntegrityError):
        repo.get_reference(reference.reference_id)


def test_machine_scope_on_memory_vault_rejected(service) -> None:
    service.record_consent('doc-1', 'operator')
    with pytest.raises(Exception, match='machine scope'):
        service.store_credential(
            'doc-1',
            scope_kind='device',
            scope_ref='avr-1',
            credential_type='password',
            material='x',
            actor='operator',
            vault_scope='machine',
        )


@pytest.mark.skipif(sys.platform != 'win32', reason='DPAPI is Windows-only')
def test_dpapi_vault_roundtrip(tmp_path: Path) -> None:
    vault = DpapiCredentialVault(tmp_path / 'vault')
    if vault.state() == 'unavailable':
        pytest.skip('DPAPI unusable in this environment')
    service = CredentialVaultService(
        CadCredentialVaultRepository(
            SceneRepository(tmp_path / 'cad-scenes.sqlite3')
        ),
        vault,
    )
    reference = _consent_and_store(service)
    assert service.retrieve(
        reference.credential_id,
    ).reveal() == 's3cr3t-p@ssw0rd!'
    # Blob on disk is protected — raw file must not contain the secret.
    blob = next((tmp_path / 'vault' / 'user').glob('*.bin'))
    assert b's3cr3t' not in blob.read_bytes()


def test_platform_vault_factory(tmp_path: Path) -> None:
    vault = platform_vault(tmp_path / 'v')
    assert vault.state() in ('unlocked', 'unavailable')
