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

from pydantic import ValidationError

from .cad_authority_resolver import (
    AuthorityRef,
    ExactAuthorityResolver,
    KindResolver,
)
from .cad_evidence_reconciliation import (
    EvidenceObservation,
    EvidenceSubject,
    ReconciliationDecision,
    evidence_observation_sha256,
    evidence_subject_sha256,
    reconcile_subject,
)
from .cad_repository import SceneRepository
from .cad_schema import require_native_tables, connect_sqlite
from .cad_system_variant_repository import CadSystemVariantRepository


class ReconciliationConflictError(ValueError):
    """A reconciliation save violated append-only identity rules."""


class ReconciliationIntegrityError(ValueError):
    """Persisted reconciliation authority failed verification (#873).

    Row/payload drift, a tampered input under an unchanged id, or a
    decision that no longer replays from its pinned inputs all fail closed
    here instead of returning silently-shifted evidence.
    """


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
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        # #767: persistent schema is owned by the migration authority;
        # repositories verify the migrated contract, never converge it.
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_evidence_subjects', 'cad_evidence_observations', 'cad_reconciliation_decisions')

    def save_subject(self, subject: EvidenceSubject) -> None:
        # #873: new subjects must carry their immutable semantic identity —
        # legacy ID-only payloads are readable, never newly writable.
        if subject.subject_sha256 is None:
            raise ReconciliationIntegrityError(
                'EvidenceSubject requires subject_sha256'
            )
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
                    attribute, subject_sha256, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    subject.subject_id,
                    subject.document_id,
                    subject.subject_kind,
                    subject.target.model_dump_json(),
                    subject.attribute,
                    subject.subject_sha256,
                    subject.model_dump_json(),
                ),
            )

    def _row_to_subject(self, row: sqlite3.Row) -> EvidenceSubject:
        """Authoritative read: row columns, payload hash and the exact
        target ref must agree (#873)."""

        try:
            subject = EvidenceSubject.model_validate_json(row['payload_json'])
        except ValidationError as exc:
            raise ReconciliationIntegrityError(
                f'evidence subject payload corrupt: {row["subject_id"]}'
            ) from exc
        if (
            subject.subject_id != row['subject_id']
            or subject.document_id != row['document_id']
            or subject.subject_kind != row['subject_kind']
            or subject.attribute != row['attribute']
            or subject.target != AuthorityRef.model_validate_json(
                row['target_json']
            )
        ):
            raise ReconciliationIntegrityError(
                f'evidence subject row/payload mismatch: {row["subject_id"]}'
            )
        stored_hash = row['subject_sha256']
        if stored_hash is not None and stored_hash != (
            subject.subject_sha256
            if subject.subject_sha256 is not None
            else evidence_subject_sha256(subject)
        ):
            raise ReconciliationIntegrityError(
                f'evidence subject hash drift: {row["subject_id"]}'
            )
        # The exact target authority must still resolve — the pinned hash
        # is checked against the canonical owner, not "current state".
        self.resolver.resolve(subject.target, document_id=subject.document_id)
        return subject

    def get_subject(self, subject_id: str) -> EvidenceSubject | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM cad_evidence_subjects
                WHERE subject_id=?
                """,
                (subject_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_subject(row)

    def list_subjects(
        self, document_id: str
    ) -> tuple[EvidenceSubject, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM cad_evidence_subjects
                WHERE document_id=?
                ORDER BY subject_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._row_to_subject(row) for row in rows)

    def save_observation(self, observation: EvidenceObservation) -> None:
        # #873: new observations must carry their immutable semantic
        # identity; the bound subject's hash is pinned implicitly by the
        # observation payload's ``subject_id`` -> stored subject row.
        if observation.observation_sha256 is None:
            raise ReconciliationIntegrityError(
                'EvidenceObservation requires observation_sha256'
            )
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
                    captured_at_utc, observation_sha256, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.subject_id,
                    observation.source,
                    observation.source_ref,
                    observation.captured_at_utc,
                    observation.observation_sha256,
                    observation.model_dump_json(),
                ),
            )

    def _row_to_observation(self, row: sqlite3.Row) -> EvidenceObservation:
        """Authoritative read: row columns, payload hash, bound subject and
        source authority must all agree (#873)."""

        try:
            observation = EvidenceObservation.model_validate_json(
                row['payload_json']
            )
        except ValidationError as exc:
            raise ReconciliationIntegrityError(
                'evidence observation payload corrupt: '
                f'{row["observation_id"]}'
            ) from exc
        if (
            observation.observation_id != row['observation_id']
            or observation.subject_id != row['subject_id']
            or observation.source != row['source']
            or observation.source_ref != row['source_ref']
            or observation.captured_at_utc != row['captured_at_utc']
        ):
            raise ReconciliationIntegrityError(
                'evidence observation row/payload mismatch: '
                f'{row["observation_id"]}'
            )
        stored_hash = row['observation_sha256']
        if stored_hash is not None and stored_hash != (
            observation.observation_sha256
            if observation.observation_sha256 is not None
            else evidence_observation_sha256(observation)
        ):
            raise ReconciliationIntegrityError(
                f'evidence observation hash drift: {row["observation_id"]}'
            )
        # The bound subject must still exist (its exact identity is what
        # makes the observation's subject pin meaningful), and the
        # non-manual source authority must still resolve exactly.
        subject = self.get_subject(observation.subject_id)
        if subject is None:
            raise ReconciliationIntegrityError(
                'evidence observation references missing subject: '
                f'{row["observation_id"]}'
            )
        if observation.source_ref is not None:
            self.resolver.resolve(
                AuthorityRef(
                    kind=observation.source,
                    ref_id=observation.source_ref,
                    ref_sha256=observation.source_sha256,
                ),
                document_id=subject.document_id,
            )
        return observation

    def get_observation(
        self, observation_id: str
    ) -> EvidenceObservation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM cad_evidence_observations
                WHERE observation_id=?
                """,
                (observation_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_observation(row)

    def list_observations(
        self, subject_id: str
    ) -> tuple[EvidenceObservation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM cad_evidence_observations
                WHERE subject_id=?
                ORDER BY observation_id
                """,
                (subject_id,),
            ).fetchall()
        return tuple(self._row_to_observation(row) for row in rows)

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
        # #873: new decisions must pin the exact input semantic hashes —
        # an ID-only decision is a legacy format, never newly writable.
        if not decision.pins_exact_inputs:
            raise ReconciliationIntegrityError(
                'new decisions must pin exact subject/observation hashes'
            )
        subject = self.get_subject(decision.subject_id)
        if subject is None:
            raise ValueError('decision references unknown subject')
        if decision.document_id != subject.document_id:
            raise ValueError(
                'decision belongs to a different document than its subject'
            )
        self._assert_input_pins(decision, subject)
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

    def _assert_input_pins(
        self,
        decision: ReconciliationDecision,
        subject: EvidenceSubject,
    ) -> None:
        """Verify the decision's pinned input hashes against stored
        authority (#873).

        The stored subject/observation rows carry — or recompute — their
        semantic hashes; a pin that no longer matches means the input
        drifted under an unchanged id and the read/save must fail closed.
        """

        actual_subject_hash = (
            subject.subject_sha256 or evidence_subject_sha256(subject)
        )
        if (
            decision.subject_sha256 is not None
            and decision.subject_sha256 != actual_subject_hash
        ):
            raise ReconciliationIntegrityError(
                f'decision {decision.decision_id} pins subject hash '
                f'{decision.subject_sha256} but the stored subject hashes '
                f'to {actual_subject_hash}'
            )
        if decision.observation_refs is None:
            return
        for ref in decision.observation_refs:
            observation = self.get_observation(ref.observation_id)
            if observation is None:
                raise ReconciliationIntegrityError(
                    f'decision {decision.decision_id} pins missing '
                    f'observation {ref.observation_id}'
                )
            actual = (
                observation.observation_sha256
                or evidence_observation_sha256(observation)
            )
            if ref.observation_sha256 != actual:
                raise ReconciliationIntegrityError(
                    f'decision {decision.decision_id} pins observation '
                    f'{ref.observation_id} hash {ref.observation_sha256} '
                    f'but the stored row hashes to {actual}'
                )

    def _row_to_decision(self, row: sqlite3.Row) -> ReconciliationDecision:
        """Authoritative read: row/payload agree, input pins hold, and the
        stored decision replays exactly from the pinned inputs (#873).

        Legacy ID-only decisions replay on the fields they have; their
        missing pins are reported by ``pins_exact_inputs`` — callers must
        not present them as exact-input authorities.
        """

        try:
            decision = ReconciliationDecision.model_validate_json(
                row['payload_json']
            )
        except ValidationError as exc:
            raise ReconciliationIntegrityError(
                'reconciliation decision payload corrupt: '
                f'{row["decision_id"]}'
            ) from exc
        if (
            decision.decision_id != row['decision_id']
            or decision.subject_id != row['subject_id']
            or decision.document_id != row['document_id']
            or decision.outcome != row['outcome']
            or decision.decision_sha256 != row['decision_sha256']
            or decision.decided_at_utc != row['decided_at_utc']
        ):
            raise ReconciliationIntegrityError(
                'reconciliation decision row/payload mismatch: '
                f'{row["decision_id"]}'
            )
        subject = self.get_subject(decision.subject_id)
        if subject is None:
            raise ReconciliationIntegrityError(
                'reconciliation decision references missing subject: '
                f'{row["decision_id"]}'
            )
        self._assert_input_pins(decision, subject)
        persisted = {
            o.observation_id: o
            for o in self.list_observations(subject.subject_id)
        }
        try:
            observations = tuple(
                persisted[oid] for oid in decision.observation_ids
            )
        except KeyError as exc:
            raise ReconciliationIntegrityError(
                f'decision {decision.decision_id} pins an observation that '
                'is not persisted'
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
        stored_payload = decision.semantic_payload()
        replayed_payload = replayed.semantic_payload()
        if decision.pins_exact_inputs:
            matches = replayed_payload == stored_payload
        else:
            # Legacy ID-only decision: it never recorded the pin fields, so
            # replay compares only the semantic surface it actually had.
            matches = all(
                replayed_payload.get(key) == value
                for key, value in stored_payload.items()
            )
        if not matches:
            raise ReconciliationIntegrityError(
                f'decision {decision.decision_id} does not reproduce the '
                'canonical reconciliation of its pinned inputs'
            )
        return decision

    def get_decision(
        self, decision_id: str
    ) -> ReconciliationDecision | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM cad_reconciliation_decisions
                WHERE decision_id=?
                """,
                (decision_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_decision(row)

    def list_decisions(
        self, subject_id: str
    ) -> tuple[ReconciliationDecision, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM cad_reconciliation_decisions
                WHERE subject_id=?
                ORDER BY decided_at_utc, decision_id
                """,
                (subject_id,),
            ).fetchall()
        return tuple(self._row_to_decision(row) for row in rows)

    def latest_decision(
        self, subject_id: str
    ) -> ReconciliationDecision | None:
        """The most recently recorded reconciliation for the subject.

        Decisions are independent reconciliations over pinned input sets —
        this ordering is presentation provenance over validated
        timestamps, never a supersession or current-authority chain (#873).
        """

        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT *
                FROM cad_reconciliation_decisions
                WHERE subject_id=?
                ORDER BY decided_at_utc DESC, decision_id DESC
                LIMIT 1
                """,
                (subject_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_decision(row)


__all__ = [
    'CadEvidenceReconciliationRepository',
    'ReconciliationConflictError',
    'ReconciliationIntegrityError',
]
