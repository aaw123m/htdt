"""Append-only persistence for the HVAC acoustic/airflow co-design
authority (#616).

Five tables:

* ``cad_hvac_ventilation_scenarios`` — sealed
  ``CadVentilationScenario`` records.
* ``cad_hvac_path_declarations`` — sealed ``CadHvacPath`` path graphs.
* ``cad_hvac_component_evidence`` — sealed
  ``CadHvacComponentEvidence`` records bound to exact operating
  conditions.
* ``cad_hvac_field_observations`` — sealed
  ``CadHvacFieldObservation`` commissioning records; the path must
  persist first.
* ``cad_hvac_qualifications`` — sealed ``CadHvacQualification``
  verdicts; the path must persist.

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
from .cad_hvac_authority import (
    CadHvacComponentEvidence,
    CadHvacFieldObservation,
    CadHvacPath,
    CadHvacQualification,
    CadVentilationScenario,
)


class HvacConflictError(ValueError):
    """An HVAC-authority save violated append-only identity rules."""


class HvacIntegrityError(ValueError):
    """A stored HVAC row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise HvacIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise HvacIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadHvacRepository:
    """Native storage for the #616 HVAC co-design records."""

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
                'cad_hvac_ventilation_scenarios',
                'cad_hvac_path_declarations',
                'cad_hvac_component_evidence',
                'cad_hvac_field_observations',
                'cad_hvac_qualifications',
            )

    # ------------------------------------------------------------------
    # Ventilation scenarios

    def save_scenario(self, scenario: CadVentilationScenario) -> None:
        _assert_sealed(scenario, 'scenario_sha256', 'scenario_id')
        existing = self.get_scenario(scenario.scenario_id)
        if existing is not None:
            if existing.scenario_sha256 == scenario.scenario_sha256:
                return
            raise HvacConflictError(
                'ventilation scenarios are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_hvac_ventilation_scenarios (
                    scenario_id, scenario_sha256, document_id,
                    operating_state, required_supply_flow_lps,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scenario.scenario_id,
                    scenario.scenario_sha256,
                    scenario.document_id,
                    scenario.operating_state,
                    scenario.required_supply_flow_lps,
                    scenario.declared_at_utc,
                    scenario.model_dump_json(),
                ),
            )

    def get_scenario(
        self, scenario_id: str
    ) -> CadVentilationScenario | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_hvac_ventilation_scenarios '
                'WHERE scenario_id=?',
                (scenario_id,),
            ).fetchone()
        if row is None:
            return None
        scenario = CadVentilationScenario.model_validate_json(
            row['payload_json']
        )
        if (
            scenario.scenario_id != row['scenario_id']
            or scenario.scenario_sha256 != row['scenario_sha256']
            or scenario.document_id != row['document_id']
            or scenario.operating_state != row['operating_state']
            or scenario.required_supply_flow_lps
            != row['required_supply_flow_lps']
            or scenario.declared_at_utc != row['declared_at_utc']
        ):
            raise HvacIntegrityError(
                'ventilation scenario row disagrees with payload'
            )
        return scenario

    def list_scenarios(
        self, document_id: str
    ) -> tuple[CadVentilationScenario, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_hvac_ventilation_scenarios '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadVentilationScenario.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Path declarations

    def save_path(self, path: CadHvacPath) -> None:
        _assert_sealed(path, 'path_sha256', 'path_id')
        existing = self.get_path(path.path_id)
        if existing is not None:
            if existing.path_sha256 == path.path_sha256:
                return
            raise HvacConflictError('HVAC paths are append-only')
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_hvac_path_declarations (
                    path_id, path_sha256, document_id,
                    path_kind, serves_room, flanking_role,
                    scenario_ref_id, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    path.path_id,
                    path.path_sha256,
                    path.document_id,
                    path.path_kind,
                    path.serves_room,
                    path.flanking_role,
                    (
                        None if path.scenario_ref is None
                        else path.scenario_ref.ref_id
                    ),
                    path.declared_at_utc,
                    path.model_dump_json(),
                ),
            )

    def get_path(self, path_id: str) -> CadHvacPath | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_hvac_path_declarations '
                'WHERE path_id=?',
                (path_id,),
            ).fetchone()
        if row is None:
            return None
        path = CadHvacPath.model_validate_json(row['payload_json'])
        if (
            path.path_id != row['path_id']
            or path.path_sha256 != row['path_sha256']
            or path.document_id != row['document_id']
            or path.path_kind != row['path_kind']
            or path.serves_room != row['serves_room']
            or path.flanking_role != row['flanking_role']
            or (
                None if path.scenario_ref is None
                else path.scenario_ref.ref_id
            ) != row['scenario_ref_id']
            or path.declared_at_utc != row['declared_at_utc']
        ):
            raise HvacIntegrityError(
                'HVAC path row disagrees with payload'
            )
        return path

    def list_paths(
        self, document_id: str
    ) -> tuple[CadHvacPath, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_hvac_path_declarations '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadHvacPath.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Component evidence

    def save_component_evidence(
        self, evidence: CadHvacComponentEvidence
    ) -> None:
        _assert_sealed(evidence, 'evidence_sha256', 'evidence_id')
        existing = self.get_component_evidence(evidence.evidence_id)
        if existing is not None:
            if existing.evidence_sha256 == evidence.evidence_sha256:
                return
            raise HvacConflictError(
                'HVAC component evidence is append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_hvac_component_evidence (
                    evidence_id, evidence_sha256, document_id,
                    component_kind, method, airflow_evidence_class,
                    flow_rate_lps, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence.evidence_id,
                    evidence.evidence_sha256,
                    evidence.document_id,
                    evidence.component_kind,
                    evidence.method,
                    evidence.airflow_evidence_class,
                    evidence.flow_rate_lps,
                    evidence.declared_at_utc,
                    evidence.model_dump_json(),
                ),
            )

    def get_component_evidence(
        self, evidence_id: str
    ) -> CadHvacComponentEvidence | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_hvac_component_evidence '
                'WHERE evidence_id=?',
                (evidence_id,),
            ).fetchone()
        if row is None:
            return None
        evidence = CadHvacComponentEvidence.model_validate_json(
            row['payload_json']
        )
        if (
            evidence.evidence_id != row['evidence_id']
            or evidence.evidence_sha256 != row['evidence_sha256']
            or evidence.document_id != row['document_id']
            or evidence.component_kind != row['component_kind']
            or evidence.method != row['method']
            or evidence.airflow_evidence_class
            != row['airflow_evidence_class']
            or evidence.flow_rate_lps != row['flow_rate_lps']
            or evidence.declared_at_utc != row['declared_at_utc']
        ):
            raise HvacIntegrityError(
                'component evidence row disagrees with payload'
            )
        return evidence

    def list_component_evidence(
        self, document_id: str
    ) -> tuple[CadHvacComponentEvidence, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_hvac_component_evidence '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadHvacComponentEvidence.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Field observations

    def save_observation(
        self, observation: CadHvacFieldObservation
    ) -> None:
        _assert_sealed(
            observation, 'observation_sha256', 'observation_id'
        )
        existing = self.get_observation(observation.observation_id)
        if existing is not None:
            if existing.observation_sha256 == observation.observation_sha256:
                return
            raise HvacConflictError(
                'HVAC field observations are append-only'
            )
        if self.get_path(observation.path_ref.ref_id) is None:
            raise HvacConflictError(
                'the bound HVAC path must persist before observations'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_hvac_field_observations (
                    observation_id, observation_sha256, document_id,
                    path_ref_id, operating_state, balancing_state,
                    room_noise_db, measured_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.observation_sha256,
                    observation.document_id,
                    observation.path_ref.ref_id,
                    observation.operating_state,
                    observation.balancing_state,
                    observation.room_noise_db,
                    observation.measured_at_utc,
                    observation.model_dump_json(),
                ),
            )

    def get_observation(
        self, observation_id: str
    ) -> CadHvacFieldObservation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_hvac_field_observations '
                'WHERE observation_id=?',
                (observation_id,),
            ).fetchone()
        if row is None:
            return None
        observation = CadHvacFieldObservation.model_validate_json(
            row['payload_json']
        )
        if (
            observation.observation_id != row['observation_id']
            or observation.observation_sha256
            != row['observation_sha256']
            or observation.document_id != row['document_id']
            or observation.path_ref.ref_id != row['path_ref_id']
            or observation.operating_state != row['operating_state']
            or observation.balancing_state != row['balancing_state']
            or observation.room_noise_db != row['room_noise_db']
            or observation.measured_at_utc != row['measured_at_utc']
        ):
            raise HvacIntegrityError(
                'field observation row disagrees with payload'
            )
        return observation

    def list_observations(
        self, document_id: str
    ) -> tuple[CadHvacFieldObservation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_hvac_field_observations '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadHvacFieldObservation.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Qualifications

    def save_qualification(
        self, qualification: CadHvacQualification
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
            raise HvacConflictError(
                'HVAC qualifications are append-only'
            )
        if self.get_path(qualification.path_ref.ref_id) is None:
            raise HvacConflictError(
                'the bound HVAC path must persist before qualifications'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_hvac_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    path_ref_id, verdict, airflow_eligibility,
                    acoustic_state, flanking_state, evaluation_version,
                    evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.path_ref.ref_id,
                    qualification.verdict,
                    qualification.airflow_eligibility,
                    qualification.acoustic_state,
                    qualification.flanking_state,
                    qualification.evaluation_version,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> CadHvacQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_hvac_qualifications '
                'WHERE qualification_id=?',
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = CadHvacQualification.model_validate_json(
            row['payload_json']
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.path_ref.ref_id != row['path_ref_id']
            or qualification.verdict != row['verdict']
            or qualification.airflow_eligibility
            != row['airflow_eligibility']
            or qualification.acoustic_state != row['acoustic_state']
            or qualification.flanking_state != row['flanking_state']
            or qualification.evaluation_version
            != row['evaluation_version']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise HvacIntegrityError(
                'HVAC qualification row disagrees with payload'
            )
        return qualification

    def list_qualifications(
        self, document_id: str
    ) -> tuple[CadHvacQualification, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_hvac_qualifications '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadHvacQualification.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )


__all__ = [
    'CadHvacRepository',
    'HvacConflictError',
    'HvacIntegrityError',
]
