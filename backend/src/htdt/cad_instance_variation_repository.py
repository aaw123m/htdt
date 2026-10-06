"""Append-only persistence for the installed-instance variation authority
(#628, REV57-AUD).

Four tables:

* ``cad_instance_acoustic_evidence`` — sealed per-instance evidence
  (evidence level/domain/observables).
* ``cad_model_instance_deltas`` — sealed derived model↔instance
  comparison artifacts.
* ``cad_matched_set_declarations`` — sealed matched-set groupings.
* ``cad_matched_set_qualifications`` — sealed matched-set verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_instance_variation_authority import (
    CadInstanceAcousticEvidence,
    CadMatchedSetDeclaration,
    CadMatchedSetQualification,
    CadModelToInstanceDelta,
)


class InstanceVariationConflictError(ValueError):
    """An instance-variation save violated append-only identity rules."""


class InstanceVariationIntegrityError(ValueError):
    """A stored instance-variation row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise InstanceVariationIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise InstanceVariationIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadInstanceVariationRepository:
    """Native storage for the #628 instance-variation authority."""

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
                'cad_instance_acoustic_evidence',
                'cad_model_instance_deltas',
                'cad_matched_set_declarations',
                'cad_matched_set_qualifications',
            )

    # ------------------------------------------------------------------
    # Instance evidence

    def save_evidence(
        self, evidence: CadInstanceAcousticEvidence
    ) -> None:
        _assert_sealed(evidence, 'evidence_sha256', 'evidence_id')
        existing = self.get_evidence(evidence.evidence_id)
        if existing is not None:
            if existing.evidence_sha256 == evidence.evidence_sha256:
                return
            raise InstanceVariationConflictError(
                'instance acoustic evidence is append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_instance_acoustic_evidence (
                    evidence_id, evidence_sha256, document_id,
                    instance_ref_id, evidence_level, evidence_source,
                    measurement_domain, measured_at_utc, declared_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence.evidence_id,
                    evidence.evidence_sha256,
                    evidence.document_id,
                    (
                        evidence.instance_ref.ref_id
                        if evidence.instance_ref is not None
                        else None
                    ),
                    evidence.evidence_level,
                    evidence.evidence_source,
                    evidence.measurement_domain,
                    evidence.measured_at_utc,
                    evidence.declared_at_utc,
                    evidence.model_dump_json(),
                ),
            )

    def get_evidence(
        self, evidence_id: str
    ) -> CadInstanceAcousticEvidence | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_instance_acoustic_evidence '
                'WHERE evidence_id=?',
                (evidence_id,),
            ).fetchone()
        if row is None:
            return None
        evidence = CadInstanceAcousticEvidence.model_validate_json(
            row['payload_json']
        )
        if (
            evidence.evidence_id != row['evidence_id']
            or evidence.evidence_sha256 != row['evidence_sha256']
            or evidence.document_id != row['document_id']
            or (
                evidence.instance_ref.ref_id
                if evidence.instance_ref is not None
                else None
            )
            != row['instance_ref_id']
            or evidence.evidence_level != row['evidence_level']
            or evidence.evidence_source != row['evidence_source']
            or evidence.measurement_domain != row['measurement_domain']
            or evidence.measured_at_utc != row['measured_at_utc']
            or evidence.declared_at_utc != row['declared_at_utc']
        ):
            raise InstanceVariationIntegrityError(
                'stored instance evidence row disagrees with its payload'
            )
        return evidence

    def list_evidence(
        self, document_id: str | None = None
    ) -> tuple[CadInstanceAcousticEvidence, ...]:
        query = 'SELECT payload_json FROM cad_instance_acoustic_evidence'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            CadInstanceAcousticEvidence.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # Model→instance deltas

    def save_delta(self, delta: CadModelToInstanceDelta) -> None:
        _assert_sealed(delta, 'delta_sha256', 'delta_id')
        existing = self.get_delta(delta.delta_id)
        if existing is not None:
            if existing.delta_sha256 == delta.delta_sha256:
                return
            raise InstanceVariationConflictError(
                'model-to-instance deltas are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_model_instance_deltas (
                    delta_id, delta_sha256, document_id,
                    reference_evidence_ref_id, instance_evidence_ref_id,
                    quantity, max_delta_db, algorithm,
                    derived_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    delta.delta_id,
                    delta.delta_sha256,
                    delta.document_id,
                    delta.reference_evidence_ref.ref_id,
                    delta.instance_evidence_ref.ref_id,
                    delta.quantity,
                    delta.max_delta_db,
                    delta.algorithm,
                    delta.derived_at_utc,
                    delta.model_dump_json(),
                ),
            )

    def get_delta(
        self, delta_id: str
    ) -> CadModelToInstanceDelta | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_model_instance_deltas WHERE delta_id=?',
                (delta_id,),
            ).fetchone()
        if row is None:
            return None
        delta = CadModelToInstanceDelta.model_validate_json(
            row['payload_json']
        )
        if (
            delta.delta_id != row['delta_id']
            or delta.delta_sha256 != row['delta_sha256']
            or delta.document_id != row['document_id']
            or delta.reference_evidence_ref.ref_id
            != row['reference_evidence_ref_id']
            or delta.instance_evidence_ref.ref_id
            != row['instance_evidence_ref_id']
            or delta.quantity != row['quantity']
            or delta.max_delta_db != row['max_delta_db']
            or delta.algorithm != row['algorithm']
            or delta.derived_at_utc != row['derived_at_utc']
        ):
            raise InstanceVariationIntegrityError(
                'stored delta row disagrees with its payload'
            )
        return delta

    def list_deltas(
        self, document_id: str | None = None
    ) -> tuple[CadModelToInstanceDelta, ...]:
        query = 'SELECT payload_json FROM cad_model_instance_deltas'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            CadModelToInstanceDelta.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Matched-set declarations

    def save_matched_set(
        self, declaration: CadMatchedSetDeclaration
    ) -> None:
        _assert_sealed(declaration, 'set_sha256', 'set_id')
        existing = self.get_matched_set(declaration.set_id)
        if existing is not None:
            if existing.set_sha256 == declaration.set_sha256:
                return
            raise InstanceVariationConflictError(
                'matched-set declarations are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_matched_set_declarations (
                    set_id, set_sha256, document_id, role,
                    member_count, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    declaration.set_id,
                    declaration.set_sha256,
                    declaration.document_id,
                    declaration.role,
                    len(declaration.member_instance_refs),
                    declaration.declared_at_utc,
                    declaration.model_dump_json(),
                ),
            )

    def get_matched_set(
        self, set_id: str
    ) -> CadMatchedSetDeclaration | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_matched_set_declarations '
                'WHERE set_id=?',
                (set_id,),
            ).fetchone()
        if row is None:
            return None
        declaration = CadMatchedSetDeclaration.model_validate_json(
            row['payload_json']
        )
        if (
            declaration.set_id != row['set_id']
            or declaration.set_sha256 != row['set_sha256']
            or declaration.document_id != row['document_id']
            or declaration.role != row['role']
            or len(declaration.member_instance_refs)
            != row['member_count']
            or declaration.declared_at_utc != row['declared_at_utc']
        ):
            raise InstanceVariationIntegrityError(
                'stored matched set row disagrees with its payload'
            )
        return declaration

    def list_matched_sets(
        self, document_id: str | None = None
    ) -> tuple[CadMatchedSetDeclaration, ...]:
        query = 'SELECT payload_json FROM cad_matched_set_declarations'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            CadMatchedSetDeclaration.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # Qualifications

    def save_qualification(
        self, qualification: CadMatchedSetQualification
    ) -> None:
        _assert_sealed(
            qualification, 'qualification_sha256', 'qualification_id'
        )
        existing = self.get_qualification(qualification.qualification_id)
        if existing is not None:
            if existing.qualification_sha256 == (
                qualification.qualification_sha256
            ):
                return
            raise InstanceVariationConflictError(
                'matched-set qualifications are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_matched_set_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    set_ref_id, verdict, evaluation_version,
                    evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.set_ref.ref_id,
                    qualification.verdict,
                    qualification.evaluation_version,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> CadMatchedSetQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_matched_set_qualifications '
                'WHERE qualification_id=?',
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = CadMatchedSetQualification.model_validate_json(
            row['payload_json']
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.set_ref.ref_id != row['set_ref_id']
            or qualification.verdict != row['verdict']
            or qualification.evaluation_version
            != row['evaluation_version']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise InstanceVariationIntegrityError(
                'stored qualification row disagrees with its payload'
            )
        return qualification

    def list_qualifications(
        self, document_id: str | None = None
    ) -> tuple[CadMatchedSetQualification, ...]:
        query = 'SELECT payload_json FROM cad_matched_set_qualifications'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            CadMatchedSetQualification.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )
