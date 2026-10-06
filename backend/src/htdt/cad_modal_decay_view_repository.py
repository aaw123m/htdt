"""Append-only persistence for the modal-decay view authority (#706,
REV58-VALIDMETH).

Two tables:

* ``cad_modal_decay_observations`` — sealed mode-candidate decay
  observations bound to their transform identities.
* ``cad_modal_decay_qualifications`` — sealed fail-closed
  qualifications.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_modal_decay_view import (
    ModalDecayObservation,
    ModalDecayQualification,
)


class ModalDecayConflictError(ValueError):
    """A modal-decay save violated append-only identity rules."""


class ModalDecayIntegrityError(ValueError):
    """A stored modal-decay row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise ModalDecayIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise ModalDecayIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadModalDecayViewRepository:
    """Native storage for the #706 modal-decay authority records."""

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
                'cad_modal_decay_observations',
                'cad_modal_decay_qualifications',
            )

    def save_observation(
        self, observation: ModalDecayObservation
    ) -> None:
        _assert_sealed(
            observation, 'observation_sha256', 'observation_id'
        )
        existing = self.get_observation(observation.observation_id)
        if existing is not None:
            if (
                existing.observation_sha256
                == observation.observation_sha256
            ):
                return
            raise ModalDecayConflictError(
                'modal-decay observations are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_modal_decay_observations (
                    observation_id, observation_sha256, document_id,
                    raw_evidence_ref_id, overlap_state, fit_model,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.observation_sha256,
                    observation.document_id,
                    observation.raw_evidence_ref.ref_id,
                    observation.overlap_state,
                    observation.fit_model,
                    observation.declared_at_utc,
                    observation.model_dump_json(),
                ),
            )

    def get_observation(
        self, observation_id: str
    ) -> ModalDecayObservation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_modal_decay_observations '
                'WHERE observation_id=?',
                (observation_id,),
            ).fetchone()
        if row is None:
            return None
        observation = ModalDecayObservation.model_validate_json(
            row['payload_json']
        )
        if (
            observation.observation_id != row['observation_id']
            or observation.observation_sha256
            != row['observation_sha256']
            or observation.document_id != row['document_id']
            or observation.raw_evidence_ref.ref_id
            != row['raw_evidence_ref_id']
            or observation.overlap_state != row['overlap_state']
            or observation.fit_model != row['fit_model']
            or observation.declared_at_utc != row['declared_at_utc']
        ):
            raise ModalDecayIntegrityError(
                'modal-decay observation row disagrees with payload'
            )
        return observation

    def list_observations(
        self, document_id: str
    ) -> tuple[ModalDecayObservation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_modal_decay_observations '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            ModalDecayObservation.model_validate_json(r['payload_json'])
            for r in rows
        )

    def save_qualification(
        self, qualification: ModalDecayQualification
    ) -> None:
        _assert_sealed(
            qualification, 'qualification_sha256', 'qualification_id'
        )
        existing = self.get_qualification(qualification.qualification_id)
        if existing is not None:
            if (
                existing.qualification_sha256
                == qualification.qualification_sha256
            ):
                return
            raise ModalDecayConflictError(
                'modal-decay qualifications are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_modal_decay_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    observation_ref_id, state, decay_trustworthy,
                    evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.observation_ref.ref_id,
                    qualification.state,
                    int(qualification.decay_trustworthy),
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> ModalDecayQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_modal_decay_qualifications '
                'WHERE qualification_id=?',
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = ModalDecayQualification.model_validate_json(
            row['payload_json']
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.observation_ref.ref_id
            != row['observation_ref_id']
            or qualification.state != row['state']
            or int(qualification.decay_trustworthy)
            != row['decay_trustworthy']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise ModalDecayIntegrityError(
                'modal-decay qualification row disagrees with payload'
            )
        return qualification

    def list_qualifications(
        self, document_id: str
    ) -> tuple[ModalDecayQualification, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_modal_decay_qualifications '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            ModalDecayQualification.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )


__all__ = [
    'CadModalDecayViewRepository',
    'ModalDecayConflictError',
    'ModalDecayIntegrityError',
]
