"""Append-only persistence for the eigenmode validation authority
(#674, REV58-VALIDMETH).

Two tables:

* ``cad_mode_pairings`` — sealed predicted↔measured mode associations.
* ``cad_eigenmode_verdicts`` — sealed per-mode validation verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_eigenmode_validation import (
    EigenmodeValidationVerdict,
    ModePairingRecord,
)


class EigenmodeValidationConflictError(ValueError):
    """An eigenmode-validation save violated append-only identity."""


class EigenmodeValidationIntegrityError(ValueError):
    """A stored eigenmode-validation row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise EigenmodeValidationIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise EigenmodeValidationIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadEigenmodeValidationRepository:
    """Native storage for the #674 eigenmode-validation records."""

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
                'cad_mode_pairings',
                'cad_eigenmode_verdicts',
            )

    def save_pairing(self, pairing: ModePairingRecord) -> None:
        _assert_sealed(pairing, 'pairing_sha256', 'pairing_id')
        existing = self.get_pairing(pairing.pairing_id)
        if existing is not None:
            if existing.pairing_sha256 == pairing.pairing_sha256:
                return
            raise EigenmodeValidationConflictError(
                'mode pairings are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_mode_pairings (
                    pairing_id, pairing_sha256, document_id,
                    pairing_state, pairing_algorithm, declared_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pairing.pairing_id,
                    pairing.pairing_sha256,
                    pairing.document_id,
                    pairing.pairing_state,
                    pairing.pairing_algorithm,
                    pairing.declared_at_utc,
                    pairing.model_dump_json(),
                ),
            )

    def get_pairing(
        self, pairing_id: str
    ) -> ModePairingRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_mode_pairings WHERE pairing_id=?',
                (pairing_id,),
            ).fetchone()
        if row is None:
            return None
        pairing = ModePairingRecord.model_validate_json(
            row['payload_json']
        )
        if (
            pairing.pairing_id != row['pairing_id']
            or pairing.pairing_sha256 != row['pairing_sha256']
            or pairing.document_id != row['document_id']
            or pairing.pairing_state != row['pairing_state']
            or pairing.pairing_algorithm != row['pairing_algorithm']
            or pairing.declared_at_utc != row['declared_at_utc']
        ):
            raise EigenmodeValidationIntegrityError(
                'mode pairing row disagrees with payload'
            )
        return pairing

    def list_pairings(
        self, document_id: str
    ) -> tuple[ModePairingRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_mode_pairings '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            ModePairingRecord.model_validate_json(r['payload_json'])
            for r in rows
        )

    def save_verdict(
        self, verdict: EigenmodeValidationVerdict
    ) -> None:
        _assert_sealed(verdict, 'verdict_sha256', 'verdict_id')
        existing = self.get_verdict(verdict.verdict_id)
        if existing is not None:
            if existing.verdict_sha256 == verdict.verdict_sha256:
                return
            raise EigenmodeValidationConflictError(
                'eigenmode verdicts are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_eigenmode_verdicts (
                    verdict_id, verdict_sha256, document_id,
                    pairing_ref_id, state, frequency_agreement,
                    shape_agreement, damping_agreement,
                    evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    verdict.verdict_id,
                    verdict.verdict_sha256,
                    verdict.document_id,
                    verdict.pairing_ref.ref_id,
                    verdict.state,
                    verdict.frequency_agreement,
                    verdict.shape_agreement,
                    verdict.damping_agreement,
                    verdict.evaluated_at_utc,
                    verdict.model_dump_json(),
                ),
            )

    def get_verdict(
        self, verdict_id: str
    ) -> EigenmodeValidationVerdict | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_eigenmode_verdicts '
                'WHERE verdict_id=?',
                (verdict_id,),
            ).fetchone()
        if row is None:
            return None
        verdict = EigenmodeValidationVerdict.model_validate_json(
            row['payload_json']
        )
        if (
            verdict.verdict_id != row['verdict_id']
            or verdict.verdict_sha256 != row['verdict_sha256']
            or verdict.document_id != row['document_id']
            or verdict.pairing_ref.ref_id != row['pairing_ref_id']
            or verdict.state != row['state']
            or verdict.frequency_agreement != row['frequency_agreement']
            or verdict.shape_agreement != row['shape_agreement']
            or verdict.damping_agreement != row['damping_agreement']
            or verdict.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise EigenmodeValidationIntegrityError(
                'eigenmode verdict row disagrees with payload'
            )
        return verdict

    def list_verdicts(
        self, document_id: str
    ) -> tuple[EigenmodeValidationVerdict, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_eigenmode_verdicts '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            EigenmodeValidationVerdict.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )


__all__ = [
    'CadEigenmodeValidationRepository',
    'EigenmodeValidationConflictError',
    'EigenmodeValidationIntegrityError',
]
