"""Append-only persistence for the active multi-way crossover
calibration authority (#665, REV58-MEASELEC).

Four tables:

* ``cad_multiway_speaker_definitions`` — sealed loudspeaker-as-multiway
  source identities (ways + physical drivers + origin pin).
* ``cad_active_crossover_plans`` — sealed crossover plans: per-way filter
  topology, mandatory protection, alignment and manufacturer envelopes.
* ``cad_driver_alignment_measurements`` — sealed per-way measured
  alignment evidence.
* ``cad_active_crossover_qualifications`` — sealed fail-closed verdicts
  gating room correction on loudspeaker coherence.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_active_crossover import (
    CadActiveCrossoverPlan,
    CadActiveCrossoverQualification,
    CadDriverAlignmentMeasurement,
    CadMultiwaySpeakerDefinition,
)


class ActiveCrossoverAuthorityConflictError(ValueError):
    """An active-crossover save violated append-only identity rules."""


class ActiveCrossoverAuthorityIntegrityError(ValueError):
    """A stored active-crossover row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise ActiveCrossoverAuthorityIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise ActiveCrossoverAuthorityIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadActiveCrossoverRepository:
    """Native storage for the #665 active-crossover authority records."""

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
                'cad_multiway_speaker_definitions',
                'cad_active_crossover_plans',
                'cad_driver_alignment_measurements',
                'cad_active_crossover_qualifications',
            )

    # ------------------------------------------------------------------
    # Multiway speaker definitions

    def save_definition(
        self, definition: CadMultiwaySpeakerDefinition
    ) -> None:
        _assert_sealed(definition, 'definition_sha256', 'definition_id')
        existing = self.get_definition(definition.definition_id)
        if existing is not None:
            if existing.definition_sha256 == definition.definition_sha256:
                return
            raise ActiveCrossoverAuthorityConflictError(
                'speaker definitions are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_multiway_speaker_definitions (
                    definition_id, definition_sha256, document_id,
                    speaker_instance, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    definition.definition_id,
                    definition.definition_sha256,
                    definition.document_id,
                    definition.speaker_instance,
                    definition.declared_at_utc,
                    definition.model_dump_json(),
                ),
            )

    def get_definition(
        self, definition_id: str
    ) -> CadMultiwaySpeakerDefinition | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_multiway_speaker_definitions '
                'WHERE definition_id=?',
                (definition_id,),
            ).fetchone()
        if row is None:
            return None
        definition = CadMultiwaySpeakerDefinition.model_validate_json(
            row['payload_json']
        )
        if (
            definition.definition_id != row['definition_id']
            or definition.definition_sha256 != row['definition_sha256']
            or definition.document_id != row['document_id']
            or definition.speaker_instance != row['speaker_instance']
            or definition.declared_at_utc != row['declared_at_utc']
        ):
            raise ActiveCrossoverAuthorityIntegrityError(
                'speaker definition row disagrees with payload'
            )
        return definition

    def list_definitions(
        self, document_id: str
    ) -> tuple[CadMultiwaySpeakerDefinition, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_multiway_speaker_definitions '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadMultiwaySpeakerDefinition.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Crossover plans

    def save_plan(self, plan: CadActiveCrossoverPlan) -> None:
        _assert_sealed(plan, 'plan_sha256', 'plan_id')
        existing = self.get_plan(plan.plan_id)
        if existing is not None:
            if existing.plan_sha256 == plan.plan_sha256:
                return
            raise ActiveCrossoverAuthorityConflictError(
                'crossover plans are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_active_crossover_plans (
                    plan_id, plan_sha256, document_id,
                    speaker_ref_id, dsp_device, declared_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.plan_id,
                    plan.plan_sha256,
                    plan.document_id,
                    plan.speaker_ref.ref_id,
                    plan.dsp_device,
                    plan.declared_at_utc,
                    plan.model_dump_json(),
                ),
            )

    def get_plan(
        self, plan_id: str
    ) -> CadActiveCrossoverPlan | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_active_crossover_plans '
                'WHERE plan_id=?',
                (plan_id,),
            ).fetchone()
        if row is None:
            return None
        plan = CadActiveCrossoverPlan.model_validate_json(
            row['payload_json']
        )
        if (
            plan.plan_id != row['plan_id']
            or plan.plan_sha256 != row['plan_sha256']
            or plan.document_id != row['document_id']
            or plan.speaker_ref.ref_id != row['speaker_ref_id']
            or plan.dsp_device != row['dsp_device']
            or plan.declared_at_utc != row['declared_at_utc']
        ):
            raise ActiveCrossoverAuthorityIntegrityError(
                'crossover plan row disagrees with payload'
            )
        return plan

    def list_plans(
        self, document_id: str
    ) -> tuple[CadActiveCrossoverPlan, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_active_crossover_plans '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadActiveCrossoverPlan.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Driver alignment measurements

    def save_alignment_measurement(
        self, measurement: CadDriverAlignmentMeasurement
    ) -> None:
        _assert_sealed(
            measurement, 'measurement_sha256', 'measurement_id'
        )
        existing = self.get_alignment_measurement(
            measurement.measurement_id
        )
        if existing is not None:
            if existing.measurement_sha256 == measurement.measurement_sha256:
                return
            raise ActiveCrossoverAuthorityConflictError(
                'alignment measurements are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_driver_alignment_measurements (
                    measurement_id, measurement_sha256, document_id,
                    speaker_ref_id, way_label, acoustic_polarity,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    measurement.measurement_id,
                    measurement.measurement_sha256,
                    measurement.document_id,
                    measurement.speaker_ref.ref_id,
                    measurement.way_label,
                    measurement.acoustic_polarity,
                    measurement.declared_at_utc,
                    measurement.model_dump_json(),
                ),
            )

    def get_alignment_measurement(
        self, measurement_id: str
    ) -> CadDriverAlignmentMeasurement | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_driver_alignment_measurements '
                'WHERE measurement_id=?',
                (measurement_id,),
            ).fetchone()
        if row is None:
            return None
        measurement = CadDriverAlignmentMeasurement.model_validate_json(
            row['payload_json']
        )
        if (
            measurement.measurement_id != row['measurement_id']
            or measurement.measurement_sha256
            != row['measurement_sha256']
            or measurement.document_id != row['document_id']
            or measurement.speaker_ref.ref_id != row['speaker_ref_id']
            or measurement.way_label != row['way_label']
            or measurement.acoustic_polarity != row['acoustic_polarity']
            or measurement.declared_at_utc != row['declared_at_utc']
        ):
            raise ActiveCrossoverAuthorityIntegrityError(
                'alignment measurement row disagrees with payload'
            )
        return measurement

    def list_alignment_measurements(
        self, document_id: str
    ) -> tuple[CadDriverAlignmentMeasurement, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_driver_alignment_measurements '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadDriverAlignmentMeasurement.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Crossover qualifications

    def save_qualification(
        self, qualification: CadActiveCrossoverQualification
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
            raise ActiveCrossoverAuthorityConflictError(
                'crossover qualifications are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_active_crossover_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    speaker_ref_id, state, evaluation_version,
                    evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.speaker_ref.ref_id,
                    qualification.state,
                    qualification.evaluation_version,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> CadActiveCrossoverQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_active_crossover_qualifications '
                'WHERE qualification_id=?',
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = (
            CadActiveCrossoverQualification.model_validate_json(
                row['payload_json']
            )
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.speaker_ref.ref_id != row['speaker_ref_id']
            or qualification.state != row['state']
            or qualification.evaluation_version
            != row['evaluation_version']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise ActiveCrossoverAuthorityIntegrityError(
                'crossover qualification row disagrees with payload'
            )
        return qualification

    def list_qualifications(
        self, document_id: str
    ) -> tuple[CadActiveCrossoverQualification, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_active_crossover_qualifications '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadActiveCrossoverQualification.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )


__all__ = [
    'CadActiveCrossoverRepository',
    'ActiveCrossoverAuthorityConflictError',
    'ActiveCrossoverAuthorityIntegrityError',
]
