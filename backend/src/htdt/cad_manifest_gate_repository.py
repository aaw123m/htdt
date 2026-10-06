"""Append-only persistence for manifest-verification authorities.

Two tables — manifest gates, gate run results.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_manifest_verification import (
    GateRunResult,
    ManifestGate,
)


class ManifestGateConflictError(ValueError):
    """A MANIFESTGATE save violated append-only identity rules."""


class ManifestGateIntegrityError(ValueError):
    """A stored MANIFESTGATE row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise ManifestGateIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise ManifestGateIntegrityError(
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
            raise ManifestGateConflictError(
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
            raise ManifestGateIntegrityError(
                f'stored {self.table} id disagrees with its payload'
            )
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise ManifestGateIntegrityError(
                f'stored {self.table} sha disagrees with its payload'
            )
        for column, path in self.columns:
            if record.__dict__.get(path.split('.')[0]) is None:
                continue
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise ManifestGateIntegrityError(
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


class CadManifestGateRepository:
    """Native storage for the manifest-verification authorities."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_manifest_gates',
                'cad_gate_run_results',
            )

        self.manifest_gates = _SealedStore(
            self._connect, 'cad_manifest_gates',
            ManifestGate, 'gate_id', 'gate_sha256',
            (
                ('document_id', '__document_id__'),
                ('issue_ref', 'issue_ref'),
                ('check_kind', 'check_kind'),
            ),
        )

        self.gate_run_results = _SealedStore(
            self._connect, 'cad_gate_run_results',
            GateRunResult, 'result_id', 'result_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('gate_ref_id', 'gate_ref'),
                ('outcome', 'outcome'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def save_manifest_gate(self, record: ManifestGate) -> None:
        self.manifest_gates.save(record)

    def get_manifest_gate(self, rid: str) -> ManifestGate | None:
        return self.manifest_gates.get(rid)

    def list_manifest_gates(
        self, document_id: str | None = None
    ) -> tuple[ManifestGate, ...]:
        return self.manifest_gates.list(document_id)

    def save_gate_run_result(self, record: GateRunResult) -> None:
        self.gate_run_results.save(record)

    def get_gate_run_result(self, rid: str) -> GateRunResult | None:
        return self.gate_run_results.get(rid)

    def list_gate_run_results(
        self, document_id: str | None = None
    ) -> tuple[GateRunResult, ...]:
        return self.gate_run_results.list(document_id)
