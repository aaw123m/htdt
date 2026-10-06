"""Append-only persistence for the as-built acoustic-treatment
qualification authority (#631).

Four tables:

* ``cad_treatment_install_specs`` — sealed
  ``CadTreatmentInstallSpec`` design intents.
* ``cad_treatment_asbuilt_observations`` — sealed
  ``CadTreatmentAsBuiltObservation`` per-parameter field states; the
  spec must persist first.
* ``cad_treatment_inspections`` — sealed ``CadTreatmentInspection``
  field-inspection records.
* ``cad_treatment_qualifications`` — sealed
  ``CadTreatmentQualification`` design-vs-as-built verdicts; the spec
  must persist.

Re-saving an identical row is a no-op; a divergent hash under the same
id is a conflict. Records are sealed before insert — a ``model_copy``
forgery is rejected at the repository boundary.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_treatment_asbuilt_authority import (
    CadTreatmentAsBuiltObservation,
    CadTreatmentInspection,
    CadTreatmentInstallSpec,
    CadTreatmentQualification,
)


class TreatmentAsBuiltConflictError(ValueError):
    """An as-built save violated append-only identity rules."""


class TreatmentAsBuiltIntegrityError(ValueError):
    """A stored as-built row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise TreatmentAsBuiltIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise TreatmentAsBuiltIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadTreatmentAsBuiltRepository:
    """Native storage for the #631 as-built treatment records."""

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
                'cad_treatment_install_specs',
                'cad_treatment_asbuilt_observations',
                'cad_treatment_inspections',
                'cad_treatment_qualifications',
            )

    # ------------------------------------------------------------------
    # Install specs

    def save_spec(self, spec: CadTreatmentInstallSpec) -> None:
        _assert_sealed(spec, 'spec_sha256', 'spec_id')
        existing = self.get_spec(spec.spec_id)
        if existing is not None:
            if existing.spec_sha256 == spec.spec_sha256:
                return
            raise TreatmentAsBuiltConflictError(
                'treatment install specs are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_treatment_install_specs (
                    spec_id, spec_sha256, document_id,
                    treatment_class, acoustic_role,
                    lab_evidence_class, product_identity,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    spec.spec_id,
                    spec.spec_sha256,
                    spec.document_id,
                    spec.treatment_class,
                    spec.acoustic_role,
                    spec.lab_evidence_class,
                    spec.product_identity,
                    spec.declared_at_utc,
                    spec.model_dump_json(),
                ),
            )

    def get_spec(
        self, spec_id: str
    ) -> CadTreatmentInstallSpec | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_treatment_install_specs '
                'WHERE spec_id=?',
                (spec_id,),
            ).fetchone()
        if row is None:
            return None
        spec = CadTreatmentInstallSpec.model_validate_json(
            row['payload_json']
        )
        if (
            spec.spec_id != row['spec_id']
            or spec.spec_sha256 != row['spec_sha256']
            or spec.document_id != row['document_id']
            or spec.treatment_class != row['treatment_class']
            or spec.acoustic_role != row['acoustic_role']
            or spec.lab_evidence_class != row['lab_evidence_class']
            or spec.product_identity != row['product_identity']
            or spec.declared_at_utc != row['declared_at_utc']
        ):
            raise TreatmentAsBuiltIntegrityError(
                'install spec row disagrees with payload'
            )
        return spec

    def list_specs(
        self, document_id: str
    ) -> tuple[CadTreatmentInstallSpec, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_treatment_install_specs '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadTreatmentInstallSpec.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # As-built observations

    def save_observation(
        self, observation: CadTreatmentAsBuiltObservation
    ) -> None:
        _assert_sealed(
            observation, 'observation_sha256', 'observation_id'
        )
        existing = self.get_observation(observation.observation_id)
        if existing is not None:
            if existing.observation_sha256 == observation.observation_sha256:
                return
            raise TreatmentAsBuiltConflictError(
                'as-built observations are append-only'
            )
        if self.get_spec(observation.spec_ref.ref_id) is None:
            raise TreatmentAsBuiltConflictError(
                'the bound spec must persist before observations'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_treatment_asbuilt_observations (
                    observation_id, observation_sha256, document_id,
                    spec_ref_id, substituted, observed_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.observation_sha256,
                    observation.document_id,
                    observation.spec_ref.ref_id,
                    (
                        None if observation.substituted is None
                        else int(observation.substituted)
                    ),
                    observation.observed_at_utc,
                    observation.model_dump_json(),
                ),
            )

    def get_observation(
        self, observation_id: str
    ) -> CadTreatmentAsBuiltObservation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_treatment_asbuilt_observations '
                'WHERE observation_id=?',
                (observation_id,),
            ).fetchone()
        if row is None:
            return None
        observation = (
            CadTreatmentAsBuiltObservation.model_validate_json(
                row['payload_json']
            )
        )
        row_substituted = row['substituted']
        if (
            observation.observation_id != row['observation_id']
            or observation.observation_sha256
            != row['observation_sha256']
            or observation.document_id != row['document_id']
            or observation.spec_ref.ref_id != row['spec_ref_id']
            or (
                None if observation.substituted is None
                else bool(observation.substituted)
            ) != (
                None if row_substituted is None
                else bool(row_substituted)
            )
            or observation.observed_at_utc != row['observed_at_utc']
        ):
            raise TreatmentAsBuiltIntegrityError(
                'as-built observation row disagrees with payload'
            )
        return observation

    def list_observations(
        self, document_id: str
    ) -> tuple[CadTreatmentAsBuiltObservation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_treatment_asbuilt_observations '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadTreatmentAsBuiltObservation.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Inspections

    def save_inspection(
        self, inspection: CadTreatmentInspection
    ) -> None:
        _assert_sealed(
            inspection, 'inspection_sha256', 'inspection_id'
        )
        existing = self.get_inspection(inspection.inspection_id)
        if existing is not None:
            if existing.inspection_sha256 == inspection.inspection_sha256:
                return
            raise TreatmentAsBuiltConflictError(
                'treatment inspections are append-only'
            )
        for ref in inspection.observation_refs:
            if self.get_observation(ref.ref_id) is None:
                raise TreatmentAsBuiltConflictError(
                    'inspection references an unpersisted observation'
                )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_treatment_inspections (
                    inspection_id, inspection_sha256, document_id,
                    operator, inspected_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    inspection.inspection_id,
                    inspection.inspection_sha256,
                    inspection.document_id,
                    inspection.operator,
                    inspection.inspected_at_utc,
                    inspection.model_dump_json(),
                ),
            )

    def get_inspection(
        self, inspection_id: str
    ) -> CadTreatmentInspection | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_treatment_inspections '
                'WHERE inspection_id=?',
                (inspection_id,),
            ).fetchone()
        if row is None:
            return None
        inspection = CadTreatmentInspection.model_validate_json(
            row['payload_json']
        )
        if (
            inspection.inspection_id != row['inspection_id']
            or inspection.inspection_sha256 != row['inspection_sha256']
            or inspection.document_id != row['document_id']
            or inspection.operator != row['operator']
            or inspection.inspected_at_utc != row['inspected_at_utc']
        ):
            raise TreatmentAsBuiltIntegrityError(
                'inspection row disagrees with payload'
            )
        return inspection

    def list_inspections(
        self, document_id: str
    ) -> tuple[CadTreatmentInspection, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_treatment_inspections '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadTreatmentInspection.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Qualifications

    def save_qualification(
        self, qualification: CadTreatmentQualification
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
            raise TreatmentAsBuiltConflictError(
                'treatment qualifications are append-only'
            )
        if self.get_spec(qualification.spec_ref.ref_id) is None:
            raise TreatmentAsBuiltConflictError(
                'the bound spec must persist before qualifications'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_treatment_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    spec_ref_id, verdict, prediction_validity,
                    before_after_result, evaluation_version,
                    evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.spec_ref.ref_id,
                    qualification.verdict,
                    qualification.prediction_validity,
                    qualification.before_after_result,
                    qualification.evaluation_version,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> CadTreatmentQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_treatment_qualifications '
                'WHERE qualification_id=?',
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = (
            CadTreatmentQualification.model_validate_json(
                row['payload_json']
            )
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.spec_ref.ref_id != row['spec_ref_id']
            or qualification.verdict != row['verdict']
            or qualification.prediction_validity
            != row['prediction_validity']
            or qualification.before_after_result
            != row['before_after_result']
            or qualification.evaluation_version
            != row['evaluation_version']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise TreatmentAsBuiltIntegrityError(
                'treatment qualification row disagrees with payload'
            )
        return qualification

    def list_qualifications(
        self, document_id: str
    ) -> tuple[CadTreatmentQualification, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_treatment_qualifications '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadTreatmentQualification.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )


__all__ = [
    'CadTreatmentAsBuiltRepository',
    'TreatmentAsBuiltConflictError',
    'TreatmentAsBuiltIntegrityError',
]
