"""Append-only persistence for evidence reconciliation (#602).

Reconciliation decisions are replayed, not trusted: ``save_decision``
reloads the stored subject plus the persisted observation set pinned by
the decision's ``observation_ids`` and re-derives the canonical outcome.
A caller-supplied decision that does not match the replay — a forged
``consistent`` over conflicting evidence — is rejected before commit.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Mapping

from .cad_authority_resolver import (
    AuthorityRef,
    ExactAuthorityResolver,
    KindResolver,
)
from .cad_evidence_reconciliation import (
    EvidenceObservation,
    EvidenceSubject,
    ReconciliationDecision,
    reconcile_subject,
)
from .cad_repository import SceneRepository
from .cad_system_variant_repository import CadSystemVariantRepository


class ReconciliationConflictError(ValueError):
    """A reconciliation save violated append-only identity rules."""


class CadEvidenceReconciliationRepository:
    """Native storage for subjects, observations and decisions.

    All three record types are append-only: re-reconciling a subject appends
    a new :class:`ReconciliationDecision`, preserving the full audit trail of
    which sources agreed or conflicted at each point in time.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        system_variant_repository: CadSystemVariantRepository | None = None,
        kind_resolvers: Mapping[str, KindResolver] | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.system_variant_repository = system_variant_repository
        self.resolver = ExactAuthorityResolver(
            scene_repository,
            system_variant_repository=system_variant_repository,
            kind_resolvers=kind_resolvers,
        )
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
                    target_json TEXT NOT NULL,
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
        # The reconciled target is an exact typed authority inside the
        # subject's own document — it must resolve before commit.
        self.resolver.resolve(subject.target, document_id=subject.document_id)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_evidence_subjects (
                    subject_id, document_id, subject_kind, target_json,
                    attribute, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    subject.subject_id,
                    subject.document_id,
                    subject.subject_kind,
                    subject.target.model_dump_json(),
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
        subject = self.get_subject(observation.subject_id)
        if subject is None:
            raise ValueError('observation references unknown subject')
        if observation.source_ref is not None:
            # Non-manual observations claim a producing authority; resolve
            # the exact ``source``-kind ref inside the subject's document so
            # provenance cannot be asserted for a foreign or fabricated
            # source. Manual observations (``source_ref`` absent) are
            # explicit operator provenance and need no authority.
            self.resolver.resolve(
                AuthorityRef(
                    kind=observation.source,
                    ref_id=observation.source_ref,
                    ref_sha256=observation.source_sha256,
                ),
                document_id=subject.document_id,
            )
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
        """Persist a decision only when it replays exactly.

        The stored subject and the persisted observation set pinned by the
        decision are reloaded, ``reconcile_subject`` is re-run and the
        caller-supplied outcome must reproduce the canonical one. A
        decision that claims ``consistent`` over conflicting or missing
        evidence cannot be recorded.
        """

        if self.get_decision(decision.decision_id) is not None:
            raise ReconciliationConflictError(
                'ReconciliationDecision ids are append-only'
            )
        subject = self.get_subject(decision.subject_id)
        if subject is None:
            raise ValueError('decision references unknown subject')
        if decision.document_id != subject.document_id:
            raise ValueError(
                'decision belongs to a different document than its subject'
            )
        persisted = {
            o.observation_id: o
            for o in self.list_observations(subject.subject_id)
        }
        try:
            observations = tuple(
                persisted[oid] for oid in decision.observation_ids
            )
        except KeyError as exc:
            raise ValueError(
                'decision pins an observation that is not persisted'
            ) from exc
        replayed = reconcile_subject(
            subject,
            observations,
            tolerance=decision.tolerance,
            tolerance_unit=decision.tolerance_unit,
            decided_at_utc=decision.decided_at_utc,
            decided_by=decision.decided_by,
            decision_id=decision.decision_id,
        )
        if replayed.semantic_payload() != decision.semantic_payload():
            raise ValueError(
                'decision does not reproduce the canonical reconciliation '
                'of the pinned persisted observations'
            )
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
