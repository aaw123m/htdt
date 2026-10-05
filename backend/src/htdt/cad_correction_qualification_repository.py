"""Append-only persistence for correction qualification records (#568).

Qualification records are durable project evidence pinned to the exact
correction identity they qualify (``subject_id`` + ``subject_sha256``).
Persisted rows are re-validated on read — the sealed semantic hash, the
gate structure, and the state/scope consistency are all re-checked by the
model — so a corrupted or tampered row fails closed instead of silently
authorizing a stale qualification.

Material-change invalidation is exact-hash based: a record is only
*current* for the correction whose subject sha256 it was sealed against.
Any change to the filter coefficients, target, device constraints, or the
bound authorities produces a new subject sha, and
``current_for_correction`` returns nothing — the new identity starts
honestly unqualified.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_correction_qualification import (
    CorrectionQualificationRecord,
    CorrectionQualificationScope,
    CorrectionQualificationState,
)
from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .clock import utc_now_iso as _utc_now


class QualificationConflictError(ValueError):
    """A qualification record was saved twice with different content."""


class CadCorrectionQualificationRepository:
    """Durable store for #568 correction qualification records."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        # #767: persistent schema is owned by the migration authority;
        # repositories verify the migrated contract, never converge it.
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_correction_qualifications')

    def save(self, record: CorrectionQualificationRecord) -> None:
        if record.document_id is None:
            raise ValueError(
                'qualification record requires document_id for persistence'
            )
        self._insert_once(
            record=record,
            payload=record.model_dump_json(),
        )

    def get(
        self, qualification_id: str
    ) -> CorrectionQualificationRecord | None:
        row = self._select_row('qualification_id', qualification_id)
        if row is None:
            return None
        return self._row_to_record(row)

    def list_for_document(
        self, document_id: str
    ) -> tuple[CorrectionQualificationRecord, ...]:
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                'SELECT * FROM cad_correction_qualifications '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(self._row_to_record(row) for row in rows)

    def list_for_correction(
        self, document_id: str, subject_id: str
    ) -> tuple[CorrectionQualificationRecord, ...]:
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                'SELECT * FROM cad_correction_qualifications '
                'WHERE document_id=? AND correction_subject_id=? '
                'ORDER BY seq ASC',
                (document_id, subject_id),
            ).fetchall()
        return tuple(self._row_to_record(row) for row in rows)

    def current_for_correction(
        self,
        document_id: str,
        subject_id: str,
        subject_sha256: str,
    ) -> CorrectionQualificationRecord | None:
        """Latest record that still qualifies this exact correction identity.

        A record sealed against a different subject sha — a re-exported
        plan, a regenerated FIR, a retargeted curve — is stale evidence and
        is not returned; the caller sees honest unqualified state.
        """

        records = self.list_for_correction(document_id, subject_id)
        for record in reversed(records):
            if record.subject.subject_sha256 == subject_sha256:
                return record
        return None

    def scope_labels(
        self, document_id: str
    ) -> dict[str, CorrectionQualificationScope]:
        """{subject_id: latest scope} for every qualified correction."""

        labels: dict[str, CorrectionQualificationScope] = {}
        for record in self.list_for_document(document_id):
            labels[record.subject.subject_id] = record.scope
        return labels

    def _row_to_record(
        self, row: sqlite3.Row
    ) -> CorrectionQualificationRecord:
        record = CorrectionQualificationRecord.model_validate_json(
            row['payload_json']
        )
        if (
            row['qualification_id'] != record.qualification_id
            or row['semantic_sha256'] != record.semantic_sha256
            or row['document_id'] != record.document_id
            or row['correction_subject_id'] != record.subject.subject_id
            or row['correction_subject_sha256'] != record.subject.subject_sha256
            or row['state'] != record.state
            or row['scope'] != record.scope
        ):
            raise ValueError(
                'persisted qualification row disagrees with its payload'
            )
        return record

    def _select_row(
        self, key_column: str, key: str
    ) -> sqlite3.Row | None:
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            return connection.execute(
                'SELECT * FROM cad_correction_qualifications '
                f'WHERE {key_column}=?',
                (key,),
            ).fetchone()

    def _insert_once(
        self,
        *,
        record: CorrectionQualificationRecord,
        payload: str,
    ) -> None:
        existing = self._select_row('qualification_id', record.qualification_id)
        if existing is not None:
            if existing['payload_json'] != payload:
                raise QualificationConflictError(
                    'qualification_id '
                    f'{record.qualification_id} is persisted with different content'
                )
            return
        with closing(self._connect()) as connection, connection:
            connection.execute(
                'INSERT INTO cad_correction_qualifications ('
                'qualification_id, semantic_sha256, document_id, '
                'correction_subject_id, correction_subject_sha256, '
                'state, scope, payload_json, recorded_at_utc'
                ') VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    record.qualification_id,
                    record.semantic_sha256,
                    record.document_id,
                    record.subject.subject_id,
                    record.subject.subject_sha256,
                    record.state,
                    record.scope,
                    payload,
                    _utc_now(),
                ),
            )


__all__ = [
    'CadCorrectionQualificationRepository',
    'CorrectionQualificationRecord',
    'CorrectionQualificationScope',
    'CorrectionQualificationState',
    'QualificationConflictError',
]
