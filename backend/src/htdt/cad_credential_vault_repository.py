"""Append-only persistence for the #890 credential-vault authority.

Two tables in one repository — credential references
(``cad_credential_references``) and lifecycle events
(``cad_credential_lifecycle_events``). Shares the #806 ``_SealedStore``
machinery: save-time seal re-verification, read-time column-vs-payload
checks.

Neither table ever stores secret material — ``vault_key`` is an opaque
``htdt-cred/<uuid>`` handle into the platform vault (DPAPI on Windows);
references pin the stable ``credential_id`` so rotation never rewrites
evidence.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .cad_calibration_deployment_repository import (
    _SealedStore,
)
from .cad_credential_vault import (
    CredentialLifecycleEvent,
    CredentialReference,
)


class CadCredentialVaultRepository:
    """Native storage for the #890 credential-vault authority."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_credential_references',
                'cad_credential_lifecycle_events',
            )
        self.references = _SealedStore(
            self._connect,
            'cad_credential_references',
            CredentialReference, 'reference_id',
            'reference_sha256',
            (
                ('document_id', '__document_id__'),
                ('credential_id', 'credential_id'),
                ('scope_kind', 'scope_kind'),
                ('scope_ref', 'scope_ref'),
                ('credential_type', 'credential_type'),
                ('vault_scope', 'vault_scope'),
                ('vault_key', 'vault_key'),
                ('state', 'state'),
                ('version', 'version'),
                ('created_at_utc', 'created_at_utc'),
            ),
        )
        self.events = _SealedStore(
            self._connect,
            'cad_credential_lifecycle_events',
            CredentialLifecycleEvent, 'event_id',
            'event_sha256',
            (
                ('document_id', '__document_id__'),
                ('credential_id', 'credential_id'),
                ('event_kind', 'event_kind'),
                ('actor', 'actor'),
                ('recorded_at_utc', 'recorded_at_utc'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    # -- writes -------------------------------------------------------------

    def append_reference(self, reference: CredentialReference) -> None:
        self.references.save(reference)

    def append_event(self, event: CredentialLifecycleEvent) -> None:
        self.events.save(event)

    # -- reads ----------------------------------------------------------------

    def get_reference(self, reference_id: str) -> CredentialReference | None:
        return self.references.get(reference_id)

    def get_event(self, event_id: str) -> CredentialLifecycleEvent | None:
        return self.events.get(event_id)

    def current_reference(
        self, credential_id: str,
    ) -> CredentialReference | None:
        """Latest reference row for the logical credential (max version)."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT reference_id FROM cad_credential_references '
                'WHERE credential_id = ? ORDER BY version DESC, seq DESC '
                'LIMIT 1',
                (credential_id,),
            ).fetchone()
        if row is None:
            return None
        return self.get_reference(row[0])

    def find_reference(self, handle: str) -> CredentialReference | None:
        """Resolve a credential *name* — ``credential_id``,
        ``reference_id`` or ``vault_key`` — to its current reference row."""

        reference = self.current_reference(handle)
        if reference is not None:
            return reference
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT credential_id FROM cad_credential_references '
                'WHERE reference_id = ? OR vault_key = ? '
                'ORDER BY version DESC LIMIT 1',
                (handle, handle),
            ).fetchone()
        if row is None:
            return None
        return self.current_reference(row[0])

    def list_references(
        self, document_id: str,
    ) -> tuple[CredentialReference, ...]:
        return self.references.list(document_id)

    def list_events(
        self,
        document_id: str,
        *,
        credential_id: str | None = None,
    ) -> tuple[CredentialLifecycleEvent, ...]:
        events = self.events.list(document_id)
        if credential_id is not None:
            events = tuple(
                event for event in events
                if event.credential_id == credential_id
            )
        return events

    def has_consent(self, document_id: str) -> bool:
        return any(
            event.event_kind == 'consent_recorded'
            for event in self.list_events(document_id)
        )
