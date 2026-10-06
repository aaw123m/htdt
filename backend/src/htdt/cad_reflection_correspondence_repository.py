"""Append-only persistence for the reflection correspondence authority
(#677, REV58-VALIDMETH).

Three tables:

* ``cad_reflection_pairings`` — sealed predicted↔observed reflection
  associations.
* ``cad_reflection_correspondence_sets`` — sealed per source↔receiver
  correspondence sets (direct-path registration pinned).
* ``cad_reflection_correspondence_verdicts`` — sealed set-level
  verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_reflection_correspondence import (
    ReflectionCorrespondencePairing,
    ReflectionCorrespondenceSet,
    ReflectionCorrespondenceVerdict,
)


class ReflectionCorrespondenceConflictError(ValueError):
    """A reflection-correspondence save violated append-only identity."""


class ReflectionCorrespondenceIntegrityError(ValueError):
    """A stored reflection-correspondence row disagreed with its
    payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise ReflectionCorrespondenceIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise ReflectionCorrespondenceIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadReflectionCorrespondenceRepository:
    """Native storage for the #677 reflection-correspondence records."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_reflection_pairings',
                'cad_reflection_correspondence_sets',
                'cad_reflection_correspondence_verdicts',
            )

    def save_pairing(
        self, pairing: ReflectionCorrespondencePairing
    ) -> None:
        _assert_sealed(pairing, 'pairing_sha256', 'pairing_id')
        existing = self.get_pairing(pairing.pairing_id)
        if existing is not None:
            if existing.pairing_sha256 == pairing.pairing_sha256:
                return
            raise ReflectionCorrespondenceConflictError(
                'reflection pairings are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_reflection_pairings (
                    pairing_id, pairing_sha256, document_id,
                    correspondence_state, matching_algorithm,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pairing.pairing_id,
                    pairing.pairing_sha256,
                    pairing.document_id,
                    pairing.correspondence_state,
                    pairing.matching_algorithm,
                    pairing.declared_at_utc,
                    pairing.model_dump_json(),
                ),
            )

    def get_pairing(
        self, pairing_id: str
    ) -> ReflectionCorrespondencePairing | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_reflection_pairings '
                'WHERE pairing_id=?',
                (pairing_id,),
            ).fetchone()
        if row is None:
            return None
        pairing = ReflectionCorrespondencePairing.model_validate_json(
            row['payload_json']
        )
        if (
            pairing.pairing_id != row['pairing_id']
            or pairing.pairing_sha256 != row['pairing_sha256']
            or pairing.document_id != row['document_id']
            or pairing.correspondence_state
            != row['correspondence_state']
            or pairing.matching_algorithm != row['matching_algorithm']
            or pairing.declared_at_utc != row['declared_at_utc']
        ):
            raise ReflectionCorrespondenceIntegrityError(
                'reflection pairing row disagrees with payload'
            )
        return pairing

    def list_pairings(
        self, document_id: str
    ) -> tuple[ReflectionCorrespondencePairing, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_reflection_pairings '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            ReflectionCorrespondencePairing.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    def save_set(self, record: ReflectionCorrespondenceSet) -> None:
        _assert_sealed(record, 'set_sha256', 'set_id')
        existing = self.get_set(record.set_id)
        if existing is not None:
            if existing.set_sha256 == record.set_sha256:
                return
            raise ReflectionCorrespondenceConflictError(
                'correspondence sets are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_reflection_correspondence_sets (
                    set_id, set_sha256, document_id,
                    registration_ref_id, pairing_count, declared_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.set_id,
                    record.set_sha256,
                    record.document_id,
                    record.registration_ref.ref_id,
                    len(record.pairing_refs),
                    record.declared_at_utc,
                    record.model_dump_json(),
                ),
            )

    def get_set(
        self, set_id: str
    ) -> ReflectionCorrespondenceSet | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_reflection_correspondence_sets '
                'WHERE set_id=?',
                (set_id,),
            ).fetchone()
        if row is None:
            return None
        record = ReflectionCorrespondenceSet.model_validate_json(
            row['payload_json']
        )
        if (
            record.set_id != row['set_id']
            or record.set_sha256 != row['set_sha256']
            or record.document_id != row['document_id']
            or record.registration_ref.ref_id
            != row['registration_ref_id']
            or len(record.pairing_refs) != row['pairing_count']
            or record.declared_at_utc != row['declared_at_utc']
        ):
            raise ReflectionCorrespondenceIntegrityError(
                'correspondence set row disagrees with payload'
            )
        return record

    def list_sets(
        self, document_id: str
    ) -> tuple[ReflectionCorrespondenceSet, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_reflection_correspondence_sets '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            ReflectionCorrespondenceSet.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    def save_verdict(
        self, verdict: ReflectionCorrespondenceVerdict
    ) -> None:
        _assert_sealed(verdict, 'verdict_sha256', 'verdict_id')
        existing = self.get_verdict(verdict.verdict_id)
        if existing is not None:
            if existing.verdict_sha256 == verdict.verdict_sha256:
                return
            raise ReflectionCorrespondenceConflictError(
                'correspondence verdicts are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_reflection_correspondence_verdicts (
                    verdict_id, verdict_sha256, document_id,
                    set_ref_id, state, matched_pair_count,
                    evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    verdict.verdict_id,
                    verdict.verdict_sha256,
                    verdict.document_id,
                    verdict.set_ref.ref_id,
                    verdict.state,
                    verdict.matched_pair_count,
                    verdict.evaluated_at_utc,
                    verdict.model_dump_json(),
                ),
            )

    def get_verdict(
        self, verdict_id: str
    ) -> ReflectionCorrespondenceVerdict | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_reflection_correspondence_verdicts '
                'WHERE verdict_id=?',
                (verdict_id,),
            ).fetchone()
        if row is None:
            return None
        verdict = ReflectionCorrespondenceVerdict.model_validate_json(
            row['payload_json']
        )
        if (
            verdict.verdict_id != row['verdict_id']
            or verdict.verdict_sha256 != row['verdict_sha256']
            or verdict.document_id != row['document_id']
            or verdict.set_ref.ref_id != row['set_ref_id']
            or verdict.state != row['state']
            or verdict.matched_pair_count != row['matched_pair_count']
            or verdict.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise ReflectionCorrespondenceIntegrityError(
                'correspondence verdict row disagrees with payload'
            )
        return verdict

    def list_verdicts(
        self, document_id: str
    ) -> tuple[ReflectionCorrespondenceVerdict, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_reflection_correspondence_verdicts '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            ReflectionCorrespondenceVerdict.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )


__all__ = [
    'CadReflectionCorrespondenceRepository',
    'ReflectionCorrespondenceConflictError',
    'ReflectionCorrespondenceIntegrityError',
]
