"""Append-only persistence for the control/automation scenario
qualification authority (#601).

Four tables:

* ``cad_control_surfaces`` — sealed controller/automation surface
  declarations (family, program identity + version pin, controlled
  subjects).
* ``cad_control_scenarios`` — sealed expected-step-sequence
  declarations (steps, feedback expectations, per-step timeouts,
  failure-notification requirement).
* ``cad_control_scenario_runs`` — sealed execution evidence records
  (per-step observed outcome, elapsed time, observed feedback,
  notification outcome).
* ``cad_control_qualifications`` — sealed fail-closed qualification
  verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_control_scenario import (
    ControlScenario,
    ControlScenarioQualification,
    ControlScenarioRun,
    ControlSurfaceDeclaration,
)


class ControlScenarioConflictError(ValueError):
    """A control-scenario save violated append-only identity rules."""


class ControlScenarioIntegrityError(ValueError):
    """A stored control-scenario row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise ControlScenarioIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise ControlScenarioIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadControlScenarioRepository:
    """Native storage for the #601 control-scenario authority records."""

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
                'cad_control_surfaces',
                'cad_control_scenarios',
                'cad_control_scenario_runs',
                'cad_control_qualifications',
            )

    def _list(
        self,
        *,
        table: str,
        model,
        where: str,
        params: tuple[object, ...],
        order: str,
    ):
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f'SELECT payload_json FROM {table} WHERE {where} '
                f'ORDER BY {order}',
                params,
            ).fetchall()
        return tuple(
            model.model_validate_json(r['payload_json']) for r in rows
        )

    # ------------------------------------------------------------------
    # Control surfaces

    def save_surface(self, surface: ControlSurfaceDeclaration) -> None:
        _assert_sealed(surface, 'surface_sha256', 'surface_id')
        existing = self.get_surface(surface.surface_id)
        if existing is not None:
            if existing.surface_sha256 == surface.surface_sha256:
                return
            raise ControlScenarioConflictError(
                'control surfaces are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_control_surfaces (
                    surface_id, surface_sha256, document_id,
                    controller_ref_kind, controller_ref_id,
                    controller_family, program_identity,
                    program_version, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    surface.surface_id,
                    surface.surface_sha256,
                    surface.document_id,
                    surface.controller_ref.kind
                    if surface.controller_ref else None,
                    surface.controller_ref.ref_id
                    if surface.controller_ref else None,
                    surface.controller_family,
                    surface.program_identity,
                    surface.program_version,
                    surface.declared_at_utc,
                    surface.model_dump_json(),
                ),
            )

    def get_surface(
        self, surface_id: str
    ) -> ControlSurfaceDeclaration | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_control_surfaces WHERE surface_id=?',
                (surface_id,),
            ).fetchone()
        if row is None:
            return None
        surface = ControlSurfaceDeclaration.model_validate_json(
            row['payload_json']
        )
        if (
            surface.surface_id != row['surface_id']
            or surface.surface_sha256 != row['surface_sha256']
            or surface.document_id != row['document_id']
            or surface.controller_family != row['controller_family']
            or surface.program_identity != row['program_identity']
            or surface.program_version != row['program_version']
            or surface.declared_at_utc != row['declared_at_utc']
        ):
            raise ControlScenarioIntegrityError(
                'control surface row disagrees with payload'
            )
        return surface

    def list_surfaces(
        self, document_id: str
    ) -> tuple[ControlSurfaceDeclaration, ...]:
        return self._list(
            table='cad_control_surfaces',
            model=ControlSurfaceDeclaration,
            where='document_id=?',
            params=(document_id,),
            order='declared_at_utc, surface_id',
        )

    # ------------------------------------------------------------------
    # Scenarios

    def save_scenario(self, scenario: ControlScenario) -> None:
        _assert_sealed(scenario, 'scenario_sha256', 'scenario_id')
        existing = self.get_scenario(scenario.scenario_id)
        if existing is not None:
            if existing.scenario_sha256 == scenario.scenario_sha256:
                return
            raise ControlScenarioConflictError(
                'control scenarios are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_control_scenarios (
                    scenario_id, scenario_sha256, document_id,
                    surface_ref_id, kind, name, step_count,
                    failure_notification_required, declared_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scenario.scenario_id,
                    scenario.scenario_sha256,
                    scenario.document_id,
                    scenario.surface_ref.ref_id
                    if scenario.surface_ref else None,
                    scenario.kind,
                    scenario.name,
                    len(scenario.steps),
                    int(scenario.failure_notification_required),
                    scenario.declared_at_utc,
                    scenario.model_dump_json(),
                ),
            )

    def get_scenario(self, scenario_id: str) -> ControlScenario | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_control_scenarios WHERE scenario_id=?',
                (scenario_id,),
            ).fetchone()
        if row is None:
            return None
        scenario = ControlScenario.model_validate_json(
            row['payload_json']
        )
        if (
            scenario.scenario_id != row['scenario_id']
            or scenario.scenario_sha256 != row['scenario_sha256']
            or scenario.document_id != row['document_id']
            or scenario.kind != row['kind']
            or scenario.name != row['name']
            or len(scenario.steps) != row['step_count']
            or int(scenario.failure_notification_required)
            != row['failure_notification_required']
            or scenario.declared_at_utc != row['declared_at_utc']
        ):
            raise ControlScenarioIntegrityError(
                'control scenario row disagrees with payload'
            )
        return scenario

    def list_scenarios(
        self, document_id: str
    ) -> tuple[ControlScenario, ...]:
        return self._list(
            table='cad_control_scenarios',
            model=ControlScenario,
            where='document_id=?',
            params=(document_id,),
            order='declared_at_utc, scenario_id',
        )

    # ------------------------------------------------------------------
    # Runs

    def save_run(self, run: ControlScenarioRun) -> None:
        _assert_sealed(run, 'run_sha256', 'run_id')
        existing = self.get_run(run.run_id)
        if existing is not None:
            if existing.run_sha256 == run.run_sha256:
                return
            raise ControlScenarioConflictError(
                'scenario runs are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_control_scenario_runs (
                    run_id, run_sha256, document_id, scenario_ref_id,
                    scenario_sha256, outcome,
                    failure_notification_outcome, started_at_utc,
                    finished_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.run_sha256,
                    run.document_id,
                    run.scenario_ref.ref_id,
                    run.scenario_ref.ref_sha256,
                    run.outcome,
                    run.failure_notification_outcome,
                    run.started_at_utc,
                    run.finished_at_utc,
                    run.model_dump_json(),
                ),
            )

    def get_run(self, run_id: str) -> ControlScenarioRun | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_control_scenario_runs WHERE run_id=?',
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        run = ControlScenarioRun.model_validate_json(row['payload_json'])
        if (
            run.run_id != row['run_id']
            or run.run_sha256 != row['run_sha256']
            or run.document_id != row['document_id']
            or run.outcome != row['outcome']
            or run.failure_notification_outcome
            != row['failure_notification_outcome']
            or run.started_at_utc != row['started_at_utc']
            or run.finished_at_utc != row['finished_at_utc']
        ):
            raise ControlScenarioIntegrityError(
                'scenario run row disagrees with payload'
            )
        return run

    def list_runs(
        self, document_id: str
    ) -> tuple[ControlScenarioRun, ...]:
        return self._list(
            table='cad_control_scenario_runs',
            model=ControlScenarioRun,
            where='document_id=?',
            params=(document_id,),
            order='started_at_utc, run_id',
        )

    # ------------------------------------------------------------------
    # Qualifications

    def save_qualification(
        self, qualification: ControlScenarioQualification
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
            raise ControlScenarioConflictError(
                'scenario qualifications are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_control_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    scenario_ref_id, state, evaluation_version,
                    evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.scenario_ref.ref_id,
                    qualification.state,
                    qualification.evaluation_version,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> ControlScenarioQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_control_qualifications '
                'WHERE qualification_id=?',
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = ControlScenarioQualification.model_validate_json(
            row['payload_json']
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.state != row['state']
            or qualification.evaluation_version
            != row['evaluation_version']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise ControlScenarioIntegrityError(
                'qualification row disagrees with payload'
            )
        return qualification

    def list_qualifications(
        self, document_id: str
    ) -> tuple[ControlScenarioQualification, ...]:
        return self._list(
            table='cad_control_qualifications',
            model=ControlScenarioQualification,
            where='document_id=?',
            params=(document_id,),
            order='evaluated_at_utc, qualification_id',
        )


__all__ = [
    'CadControlScenarioRepository',
    'ControlScenarioConflictError',
    'ControlScenarioIntegrityError',
]
