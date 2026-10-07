"""Native sealed storage for the #883 session-recovery authority.

Three append-only tables (native schema v100):

- ``session_recovery_journals`` — one sealed detection record per
  crashed session journal: the envelope facts plus head/envelope sha, so
  the record outlives file retention as tamper-evidence.
- ``session_recovery_decisions`` — operator decisions (restore /
  discard / defer / reject / retention) carrying the restoring
  session's lineage.
- ``session_recovery_reconciliations`` — verdicts on uncertain external
  effects (device apply, sweep acquisition, file writes).
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from pathlib import Path
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .session_recovery import (
    SessionReconciliationRecord,
    SessionRecoveryDecision,
    SessionRecoveryJournalRecord,
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
            if value is None:
                return None
        return value

    def _verify(self, record: Any) -> None:
        from .canonical_json import canonical_sha256

        if canonical_sha256(record.identity_payload()) != getattr(
            record, self.sha_field
        ):
            raise SessionRecoveryIntegrityError(
                f'{self.table} record is not sealed'
            )

    def save(self, record: Any) -> None:
        self._verify(record)
        rid = getattr(record, self.id_field)
        existing = self.get(rid)
        if existing is not None:
            if getattr(existing, self.sha_field) == getattr(
                record, self.sha_field
            ):
                return
            raise SessionRecoveryConflictError(
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
            raise SessionRecoveryIntegrityError(
                f'stored {self.table} id disagrees with its payload'
            )
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise SessionRecoveryIntegrityError(
                f'stored {self.table} sha disagrees with its payload'
            )
        for column, path in self.columns:
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise SessionRecoveryIntegrityError(
                    f'stored {self.table}.{column} disagrees '
                    'with its payload'
                )
        return record

    def list(self, document_id: str | None = None) -> tuple[Any, ...]:
        query = f'SELECT * FROM {self.table}'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        records = []
        for row in rows:
            record = self.model.model_validate_json(row['payload_json'])
            if record.document_id != row['document_id']:
                raise SessionRecoveryIntegrityError(
                    f'stored {self.table}.document_id disagrees '
                    'with its payload'
                )
            records.append(record)
        return tuple(records)

    def list_for_session(
        self, session_id: str
    ) -> tuple[Any, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f'SELECT * FROM {self.table} WHERE session_id=? '
                'ORDER BY seq ASC',
                (session_id,),
            ).fetchall()
        return tuple(
            self.model.model_validate_json(row['payload_json'])
            for row in rows
        )


class SessionRecoveryIntegrityError(RuntimeError):
    pass


class SessionRecoveryConflictError(RuntimeError):
    pass


class SessionRecoveryRepository:
    """Native storage for the #883 session-recovery authority."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'session_recovery_journals',
                'session_recovery_decisions',
                'session_recovery_reconciliations',
            )
        self.journal_records = _SealedStore(
            self._connect, 'session_recovery_journals',
            SessionRecoveryJournalRecord, 'journal_id',
            'journal_sha256',
            (
                ('document_id', '__document_id__'),
                ('session_id', 'session_id'),
                ('ending', 'ending'),
                ('integrity', 'integrity'),
                ('entry_count', 'entry_count'),
                ('head_entry_sha256', 'head_entry_sha256'),
                ('envelope_sha256', 'envelope_sha256'),
                ('detected_at_utc', 'detected_at_utc'),
            ),
        )
        self.decisions = _SealedStore(
            self._connect, 'session_recovery_decisions',
            SessionRecoveryDecision, 'decision_id',
            'decision_sha256',
            (
                ('document_id', '__document_id__'),
                ('session_id', 'session_id'),
                ('scope_kind', 'scope_kind'),
                ('scope_ref_id', 'scope_ref_id'),
                ('action', 'action'),
                ('actor', 'actor'),
                ('decided_at_utc', 'decided_at_utc'),
            ),
        )
        self.reconciliations = _SealedStore(
            self._connect, 'session_recovery_reconciliations',
            SessionReconciliationRecord, 'reconciliation_id',
            'reconciliation_sha256',
            (
                ('document_id', '__document_id__'),
                ('session_id', 'session_id'),
                ('operation_kind', 'operation_kind'),
                ('operation_ref_id', 'operation_ref_id'),
                ('verdict', 'verdict'),
                ('recorded_at_utc', 'recorded_at_utc'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    # detection records ------------------------------------------------

    def save_journal_record(
        self, record: SessionRecoveryJournalRecord
    ) -> None:
        self.journal_records.save(record)

    def get_journal_record(
        self, journal_id: str
    ) -> SessionRecoveryJournalRecord | None:
        return self.journal_records.get(journal_id)

    def list_journal_records(
        self, document_id: str | None = None
    ) -> tuple[SessionRecoveryJournalRecord, ...]:
        return self.journal_records.list(document_id)

    def journal_record_for_session(
        self, session_id: str
    ) -> SessionRecoveryJournalRecord | None:
        rows = self.journal_records.list_for_session(session_id)
        return rows[-1] if rows else None

    # decisions --------------------------------------------------------

    def save_decision(self, record: SessionRecoveryDecision) -> None:
        self.decisions.save(record)

    def get_decision(
        self, decision_id: str
    ) -> SessionRecoveryDecision | None:
        return self.decisions.get(decision_id)

    def list_decisions(
        self, document_id: str | None = None
    ) -> tuple[SessionRecoveryDecision, ...]:
        return self.decisions.list(document_id)

    def decisions_for_session(
        self, session_id: str
    ) -> tuple[SessionRecoveryDecision, ...]:
        return self.decisions.list_for_session(session_id)

    def session_is_resolved(self, session_id: str) -> bool:
        from .session_recovery import TERMINAL_DECISION_ACTIONS

        return any(
            decision.scope_kind == 'session'
            and decision.action in TERMINAL_DECISION_ACTIONS
            for decision in self.decisions_for_session(session_id)
        )

    # reconciliations --------------------------------------------------

    def save_reconciliation(
        self, record: SessionReconciliationRecord
    ) -> None:
        self.reconciliations.save(record)

    def get_reconciliation(
        self, reconciliation_id: str
    ) -> SessionReconciliationRecord | None:
        return self.reconciliations.get(reconciliation_id)

    def list_reconciliations(
        self, document_id: str | None = None
    ) -> tuple[SessionReconciliationRecord, ...]:
        return self.reconciliations.list(document_id)

    def reconciliations_for_session(
        self, session_id: str
    ) -> tuple[SessionReconciliationRecord, ...]:
        return self.reconciliations.list_for_session(session_id)


__all__ = [
    'SessionRecoveryConflictError',
    'SessionRecoveryIntegrityError',
    'SessionRecoveryRepository',
]
