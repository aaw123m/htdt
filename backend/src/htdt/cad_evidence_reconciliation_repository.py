"""Append-only persistence for evidence reconciliation (#602)."""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_evidence_reconciliation import (
    EvidenceObservation,
    EvidenceSubject,
    ReconciliationDecision,
)
from .cad_repository import SceneRepository


class ReconciliationConflictError(ValueError):
    """A reconciliation save violated append-only identity rules."""


class CadEvidenceReconciliationRepository:
    """Native storage for subjects, observations and decisions.

    All three record types are append-only: re-reconciling a subject appends
    a new :class:`ReconciliationDecision`, preserving the full audit trail of
    which sources agreed or conflicted at each point in time.
    """

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_evidence_subjects (
                    subject_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    subject_kind TEXT NOT NULL,
                    target_ref TEXT NOT NULL,
                    attribute TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_evidence_observations (
                    observation_id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_ref TEXT,
                    captured_at_utc TEXT,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY (subject_id)
                        REFERENCES cad_evidence_subjects (subject_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_cad_evidence_observations_subject
                ON cad_evidence_observations (subject_id)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_reconciliation_decisions (
                    decision_id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    decision_sha256 TEXT NOT NULL,
                    decided_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY (subject_id)
                        REFERENCES cad_evidence_subjects (subject_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_cad_reconciliation_subject
                ON cad_reconciliation_decisions (subject_id, decided_at_utc)
                """
            )

    def save_subject(self, subject: EvidenceSubject) -> None:
        if self.get_subject(subject.subject_id) is not None:
            raise ReconciliationConflictError(
                'EvidenceSubject ids are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_evidence_subjects (
                    subject_id, document_id, subject_kind, target_ref,
                    attribute, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    subject.subject_id,
                    subject.document_id,
                    subject.subject_kind,
                    subject.target_ref,
                    subject.attribute,
                    subject.model_dump_json(),
                ),
            )

    def get_subject(self, subject_id: str) -> EvidenceSubject | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_evidence_subjects
                WHERE subject_id=?
                """,
                (subject_id,),
            ).fetchone()
        if row is None:
            return None
        return EvidenceSubject.model_validate_json(row['payload_json'])

    def list_subjects(
        self, document_id: str
    ) -> tuple[EvidenceSubject, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_evidence_subjects
                WHERE document_id=?
                ORDER BY subject_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            EvidenceSubject.model_validate_json(row['payload_json'])
            for row in rows
        )

    def save_observation(self, observation: EvidenceObservation) -> None:
        if self.get_observation(observation.observation_id) is not None:
            raise ReconciliationConflictError(
                'EvidenceObservation ids are append-only'
            )
        if self.get_subject(observation.subject_id) is None:
            raise ValueError('observation references unknown subject')
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_evidence_observations (
                    observation_id, subject_id, source, source_ref,
                    captured_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.subject_id,
                    observation.source,
                    observation.source_ref,
                    observation.captured_at_utc,
                    observation.model_dump_json(),
                ),
            )

    def get_observation(
        self, observation_id: str
    ) -> EvidenceObservation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_evidence_observations
                WHERE observation_id=?
                """,
                (observation_id,),
            ).fetchone()
        if row is None:
            return None
        return EvidenceObservation.model_validate_json(row['payload_json'])

    def list_observations(
        self, subject_id: str
    ) -> tuple[EvidenceObservation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_evidence_observations
                WHERE subject_id=?
                ORDER BY observation_id
                """,
                (subject_id,),
            ).fetchall()
        return tuple(
            EvidenceObservation.model_validate_json(row['payload_json'])
            for row in rows
        )

    def save_decision(self, decision: ReconciliationDecision) -> None:
        if self.get_decision(decision.decision_id) is not None:
            raise ReconciliationConflictError(
                'ReconciliationDecision ids are append-only'
            )
        if self.get_subject(decision.subject_id) is None:
            raise ValueError('decision references unknown subject')
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_reconciliation_decisions (
                    decision_id, subject_id, document_id, outcome,
                    decision_sha256, decided_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.decision_id,
                    decision.subject_id,
                    decision.document_id,
                    decision.outcome,
                    decision.decision_sha256,
                    decision.decided_at_utc,
                    decision.model_dump_json(),
                ),
            )

    def get_decision(
        self, decision_id: str
    ) -> ReconciliationDecision | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_reconciliation_decisions
                WHERE decision_id=?
                """,
                (decision_id,),
            ).fetchone()
        if row is None:
            return None
        return ReconciliationDecision.model_validate_json(
            row['payload_json']
        )

    def list_decisions(
        self, subject_id: str
    ) -> tuple[ReconciliationDecision, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_reconciliation_decisions
                WHERE subject_id=?
                ORDER BY decided_at_utc, decision_id
                """,
                (subject_id,),
            ).fetchall()
        return tuple(
            ReconciliationDecision.model_validate_json(row['payload_json'])
            for row in rows
        )

    def latest_decision(
        self, subject_id: str
    ) -> ReconciliationDecision | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM cad_reconciliation_decisions
                WHERE subject_id=?
                ORDER BY decided_at_utc DESC, decision_id DESC
                LIMIT 1
                """,
                (subject_id,),
            ).fetchone()
        if row is None:
            return None
        return ReconciliationDecision.model_validate_json(
            row['payload_json']
        )


__all__ = [
    'CadEvidenceReconciliationRepository',
    'ReconciliationConflictError',
]
