"""Append-only persistence for REV59-CADREF authorities.

Two tables — cadence delivery (#777), reference room profile (#778).
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_cadence_reference import (
    CadenceDeliveryEvidence,
    ReferenceRoomProfile,
)


class CadRefAuthorityConflictError(ValueError):
    """A CADREF save violated append-only identity rules."""


class CadRefAuthorityIntegrityError(ValueError):
    """A stored CADREF row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise CadRefAuthorityIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise CadRefAuthorityIntegrityError(
            'record id does not match its sealed sha256'
        )


class _SealedStore:
    """Generic append-only store for one sealed record type."""

    def __init__(
        self,
        connection_factory: Any,
        table: str,
        model: type,
        id_field: str,
        sha_field: str,
        columns: tuple[tuple[str, str], ...],
    ) -> None:
        self._connect = connection_factory
        self.table = table
        self.model = model
        self.id_field = id_field
        self.sha_field = sha_field
        self.columns = columns

    def _column_value(self, record: Any, path: str) -> Any:
        if path == '__document_id__':
            return record.document_id
        value: Any = record
        for part in path.split('.'):
            value = getattr(value, part)
        return value

    def save(self, record: Any) -> None:
        _assert_sealed(record, self.sha_field, self.id_field)
        rid = getattr(record, self.id_field)
        existing = self.get(rid)
        if existing is not None:
            if getattr(existing, self.sha_field) == getattr(
                record, self.sha_field
            ):
                return
            raise CadRefAuthorityConflictError(
                f'{self.table} records are append-only'
            )
        cols = ', '.join(
            [self.id_field, self.sha_field]
            + [c[0] for c in self.columns]
            + ['payload_json']
        )
        placeholders = ', '.join(['?'] * (2 + len(self.columns) + 1))
        values = (
            rid,
            getattr(record, self.sha_field),
            *(
                self._column_value(record, path)
                for _, path in self.columns
            ),
            record.model_dump_json(),
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                f'INSERT INTO {self.table} ({cols}) '
                f'VALUES ({placeholders})',
                values,
            )

    def get(self, rid: str) -> Any | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                f'SELECT * FROM {self.table} WHERE {self.id_field}=?',
                (rid,),
            ).fetchone()
        if row is None:
            return None
        record = self.model.model_validate_json(row['payload_json'])
        if getattr(record, self.id_field) != row[self.id_field]:
            raise CadRefAuthorityIntegrityError(
                f'stored {self.table} id disagrees with its payload'
            )
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise CadRefAuthorityIntegrityError(
                f'stored {self.table} sha disagrees with its payload'
            )
        for column, path in self.columns:
            if record.__dict__.get(path.split('.')[0]) is None:
                continue
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise CadRefAuthorityIntegrityError(
                    f'stored {self.table}.{column} disagrees '
                    'with its payload'
                )
        return record

    def list(self, document_id: str | None = None) -> tuple[Any, ...]:
        query = f'SELECT payload_json FROM {self.table}'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            self.model.model_validate_json(r['payload_json'])
            for r in rows
        )


def _ref(column: str, path: str) -> tuple[str, str]:
    return (column, f'{path}.ref_id')


class CadCadRefRepository:
    """Native storage for the #777/#778 authorities."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_cadence_delivery_evidence',
                'cad_reference_room_profiles',
            )

        self.cadence_evidence = _SealedStore(
            self._connect, 'cad_cadence_delivery_evidence',
            CadenceDeliveryEvidence, 'evidence_id', 'evidence_sha256',
            (
                ('document_id', '__document_id__'),
                ('content_cadence_kind', 'content_cadence_kind'),
                ('refresh_relationship', 'refresh_relationship'),
            ),
        )

        self.reference_profiles = _SealedStore(
            self._connect, 'cad_reference_room_profiles',
            ReferenceRoomProfile, 'profile_id', 'profile_sha256',
            (
                ('document_id', '__document_id__'),
                ('framework', 'framework'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def save_cadence_evidence(self, record: CadenceDeliveryEvidence) -> None:
        self.cadence_evidence.save(record)

    def get_cadence_evidence(self, rid: str) -> CadenceDeliveryEvidence | None:
        return self.cadence_evidence.get(rid)

    def save_reference_profile(self, record: ReferenceRoomProfile) -> None:
        self.reference_profiles.save(record)

    def get_reference_profile(self, rid: str) -> ReferenceRoomProfile | None:
        return self.reference_profiles.get(rid)
