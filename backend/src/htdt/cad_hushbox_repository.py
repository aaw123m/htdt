"""Append-only persistence for the projector hush-box / enclosure
co-design authority (#624).

Five tables:

* ``cad_projector_install_constraints`` — sealed manufacturer
  installation/ventilation envelopes.
* ``cad_projector_enclosure_plans`` — sealed enclosure plans /
  as-builts.
* ``cad_enclosure_operating_observations`` — sealed sustained thermal
  observations.
* ``cad_enclosure_acoustic_observations`` — sealed before/after
  acoustic observations.
* ``cad_enclosure_qualifications`` — sealed fail-closed joint verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_hushbox_authority import (
    CadEnclosureAcousticObservation,
    CadEnclosureOperatingObservation,
    CadEnclosureQualification,
    CadProjectorEnclosurePlan,
    CadProjectorInstallConstraints,
)


class HushboxConflictError(ValueError):
    """A hushbox-authority save violated append-only identity rules."""


class HushboxIntegrityError(ValueError):
    """A stored hushbox row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise HushboxIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise HushboxIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadHushboxRepository:
    """Native storage for the #624 hush-box-authority records."""

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
                'cad_projector_install_constraints',
                'cad_projector_enclosure_plans',
                'cad_enclosure_operating_observations',
                'cad_enclosure_acoustic_observations',
                'cad_enclosure_qualifications',
            )

    # ------------------------------------------------------------------
    # Install constraints

    def save_constraints(
        self, constraints: CadProjectorInstallConstraints
    ) -> None:
        _assert_sealed(
            constraints, 'constraint_sha256', 'constraint_id'
        )
        existing = self.get_constraints(constraints.constraint_id)
        if existing is not None:
            if existing.constraint_sha256 == constraints.constraint_sha256:
                return
            raise HushboxConflictError(
                'projector install constraints are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_projector_install_constraints (
                    constraint_id, constraint_sha256, document_id,
                    manufacturer, model, source_document,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    constraints.constraint_id,
                    constraints.constraint_sha256,
                    constraints.document_id,
                    constraints.manufacturer,
                    constraints.model,
                    constraints.source_document,
                    constraints.declared_at_utc,
                    constraints.model_dump_json(),
                ),
            )

    def get_constraints(
        self, constraint_id: str
    ) -> CadProjectorInstallConstraints | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_projector_install_constraints '
                'WHERE constraint_id=?',
                (constraint_id,),
            ).fetchone()
        if row is None:
            return None
        constraints = CadProjectorInstallConstraints.model_validate_json(
            row['payload_json']
        )
        if (
            constraints.constraint_id != row['constraint_id']
            or constraints.constraint_sha256 != row['constraint_sha256']
            or constraints.document_id != row['document_id']
            or constraints.manufacturer != row['manufacturer']
            or constraints.model != row['model']
            or constraints.source_document != row['source_document']
            or constraints.declared_at_utc != row['declared_at_utc']
        ):
            raise HushboxIntegrityError(
                'install constraints row disagrees with payload'
            )
        return constraints

    def list_constraints(
        self, document_id: str
    ) -> tuple[CadProjectorInstallConstraints, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_projector_install_constraints '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadProjectorInstallConstraints.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Enclosure plans

    def save_plan(self, plan: CadProjectorEnclosurePlan) -> None:
        _assert_sealed(plan, 'plan_sha256', 'plan_id')
        existing = self.get_plan(plan.plan_id)
        if existing is not None:
            if existing.plan_sha256 == plan.plan_sha256:
                return
            raise HushboxConflictError(
                'enclosure plans are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_projector_enclosure_plans (
                    plan_id, plan_sha256, document_id,
                    constraint_ref_id, remote_projection,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.plan_id,
                    plan.plan_sha256,
                    plan.document_id,
                    (
                        None if plan.constraint_ref is None
                        else plan.constraint_ref.ref_id
                    ),
                    int(plan.remote_projection),
                    plan.declared_at_utc,
                    plan.model_dump_json(),
                ),
            )

    def get_plan(
        self, plan_id: str
    ) -> CadProjectorEnclosurePlan | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_projector_enclosure_plans '
                'WHERE plan_id=?',
                (plan_id,),
            ).fetchone()
        if row is None:
            return None
        plan = CadProjectorEnclosurePlan.model_validate_json(
            row['payload_json']
        )
        if (
            plan.plan_id != row['plan_id']
            or plan.plan_sha256 != row['plan_sha256']
            or plan.document_id != row['document_id']
            or (
                None if plan.constraint_ref is None
                else plan.constraint_ref.ref_id
            ) != row['constraint_ref_id']
            or plan.remote_projection != bool(row['remote_projection'])
            or plan.declared_at_utc != row['declared_at_utc']
        ):
            raise HushboxIntegrityError(
                'enclosure plan row disagrees with payload'
            )
        return plan

    def list_plans(
        self, document_id: str
    ) -> tuple[CadProjectorEnclosurePlan, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_projector_enclosure_plans '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadProjectorEnclosurePlan.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Operating observations

    def save_operating_observation(
        self, observation: CadEnclosureOperatingObservation
    ) -> None:
        _assert_sealed(
            observation, 'observation_sha256', 'observation_id'
        )
        existing = self.get_operating_observation(
            observation.observation_id
        )
        if existing is not None:
            if (
                existing.observation_sha256
                == observation.observation_sha256
            ):
                return
            raise HushboxConflictError(
                'enclosure operating observations are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_enclosure_operating_observations (
                    observation_id, observation_sha256, document_id,
                    plan_ref_id, scenario, duration_s,
                    projector_fan_state, protection_event,
                    measured_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.observation_sha256,
                    observation.document_id,
                    observation.plan_ref.ref_id,
                    observation.scenario,
                    observation.duration_s,
                    observation.projector_fan_state,
                    observation.protection_event,
                    observation.measured_at_utc,
                    observation.model_dump_json(),
                ),
            )

    def get_operating_observation(
        self, observation_id: str
    ) -> CadEnclosureOperatingObservation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_enclosure_operating_observations '
                'WHERE observation_id=?',
                (observation_id,),
            ).fetchone()
        if row is None:
            return None
        observation = (
            CadEnclosureOperatingObservation.model_validate_json(
                row['payload_json']
            )
        )
        if (
            observation.observation_id != row['observation_id']
            or observation.observation_sha256
            != row['observation_sha256']
            or observation.document_id != row['document_id']
            or observation.plan_ref.ref_id != row['plan_ref_id']
            or observation.scenario != row['scenario']
            or observation.duration_s != row['duration_s']
            or observation.projector_fan_state
            != row['projector_fan_state']
            or observation.protection_event != row['protection_event']
            or observation.measured_at_utc != row['measured_at_utc']
        ):
            raise HushboxIntegrityError(
                'operating observation row disagrees with payload'
            )
        return observation

    def list_operating_observations(
        self, document_id: str
    ) -> tuple[CadEnclosureOperatingObservation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_enclosure_operating_observations '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadEnclosureOperatingObservation.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Acoustic observations

    def save_acoustic_observation(
        self, acoustic: CadEnclosureAcousticObservation
    ) -> None:
        _assert_sealed(acoustic, 'acoustic_sha256', 'acoustic_id')
        existing = self.get_acoustic_observation(acoustic.acoustic_id)
        if existing is not None:
            if existing.acoustic_sha256 == acoustic.acoustic_sha256:
                return
            raise HushboxConflictError(
                'enclosure acoustic observations are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_enclosure_acoustic_observations (
                    acoustic_id, acoustic_sha256, document_id,
                    plan_ref_id, comparability, pre_spl_db, post_spl_db,
                    measured_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    acoustic.acoustic_id,
                    acoustic.acoustic_sha256,
                    acoustic.document_id,
                    acoustic.plan_ref.ref_id,
                    acoustic.comparability,
                    acoustic.pre_spl_db,
                    acoustic.post_spl_db,
                    acoustic.measured_at_utc,
                    acoustic.model_dump_json(),
                ),
            )

    def get_acoustic_observation(
        self, acoustic_id: str
    ) -> CadEnclosureAcousticObservation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_enclosure_acoustic_observations '
                'WHERE acoustic_id=?',
                (acoustic_id,),
            ).fetchone()
        if row is None:
            return None
        acoustic = CadEnclosureAcousticObservation.model_validate_json(
            row['payload_json']
        )
        if (
            acoustic.acoustic_id != row['acoustic_id']
            or acoustic.acoustic_sha256 != row['acoustic_sha256']
            or acoustic.document_id != row['document_id']
            or acoustic.plan_ref.ref_id != row['plan_ref_id']
            or acoustic.comparability != row['comparability']
            or acoustic.pre_spl_db != row['pre_spl_db']
            or acoustic.post_spl_db != row['post_spl_db']
            or acoustic.measured_at_utc != row['measured_at_utc']
        ):
            raise HushboxIntegrityError(
                'acoustic observation row disagrees with payload'
            )
        return acoustic

    def list_acoustic_observations(
        self, document_id: str
    ) -> tuple[CadEnclosureAcousticObservation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_enclosure_acoustic_observations '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadEnclosureAcousticObservation.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Qualifications

    def save_qualification(
        self, qualification: CadEnclosureQualification
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
            raise HushboxConflictError(
                'enclosure qualifications are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_enclosure_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    plan_ref_id, verdict, thermal_state, acoustic_state,
                    optical_state, evaluation_version, evaluated_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.plan_ref.ref_id,
                    qualification.verdict,
                    qualification.thermal_state,
                    qualification.acoustic_state,
                    qualification.optical_state,
                    qualification.evaluation_version,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> CadEnclosureQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_enclosure_qualifications '
                'WHERE qualification_id=?',
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = CadEnclosureQualification.model_validate_json(
            row['payload_json']
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.plan_ref.ref_id != row['plan_ref_id']
            or qualification.verdict != row['verdict']
            or qualification.thermal_state != row['thermal_state']
            or qualification.acoustic_state != row['acoustic_state']
            or qualification.optical_state != row['optical_state']
            or qualification.evaluation_version
            != row['evaluation_version']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise HushboxIntegrityError(
                'enclosure qualification row disagrees with payload'
            )
        return qualification

    def list_qualifications(
        self, document_id: str
    ) -> tuple[CadEnclosureQualification, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_enclosure_qualifications '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadEnclosureQualification.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )


__all__ = [
    'CadHushboxRepository',
    'HushboxConflictError',
    'HushboxIntegrityError',
]
