"""Append-only persistence and transition rules for the campaign runner."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
import json
from typing import Literal
from uuid import uuid4

from .cad_measurement_runner import (
    MeasurementRunnerPlan,
    MeasurementRunnerRun,
    RunnerCellEvent,
    RunnerCellState,
    RunnerCellStatus,
    next_incomplete_cell,
    resolve_cell_states,

)

from .cad_schema import require_native_tables


class RunnerError(ValueError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CadMeasurementRunnerRepository:
    """Runner plans, runs, and per-cell transition events.

    When ``measurement_repository`` is provided, committed measurements are
    validated to exist and to bind the exact dataset sha before the cell
    transition is accepted.
    """

    def __init__(self, scene_repository, measurement_repository=None) -> None:
        self.path = scene_repository.path
        self.measurement_repository = measurement_repository
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys = ON')
        return closing(connection)

    def _initialize(self) -> None:
        with self._connect() as connection, connection:
            require_native_tables(connection, 'cad_measurement_runner_plans', 'cad_measurement_runner_runs', 'cad_measurement_runner_events')

    def save_plan(self, plan: MeasurementRunnerPlan) -> None:
        if self.get_plan(plan.plan_id) is not None:
            raise RunnerError('runner plans are append-only')
        with self._connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_measurement_runner_plans (
                    plan_id, scene_revision_id, plan_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (plan.plan_id, plan.scene_revision_id, plan.plan_sha256, _utc_now(), plan.model_dump_json()),
            )

    def get_plan(self, plan_id: str) -> MeasurementRunnerPlan | None:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_measurement_runner_plans WHERE plan_id=?',
                (plan_id,),
            ).fetchone()
        return None if row is None else MeasurementRunnerPlan.model_validate_json(row['payload_json'])

    def start_run(self, plan_id: str, *, started_at: str | None = None) -> MeasurementRunnerRun:
        plan = self.get_plan(plan_id)
        if plan is None:
            raise RunnerError('runner plan is not persisted')
        run = MeasurementRunnerRun(
            run_id=str(uuid4()),
            plan_id=plan.plan_id,
            plan_sha256=plan.plan_sha256,
            started_at=started_at or _utc_now(),
        )
        with self._connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_measurement_runner_runs (
                    run_id, plan_id, plan_sha256, started_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (run.run_id, run.plan_id, run.plan_sha256, run.started_at, run.model_dump_json()),
            )
        return run

    def get_run(self, run_id: str) -> MeasurementRunnerRun | None:
        with self._connect() as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_measurement_runner_runs WHERE run_id=?',
                (run_id,),
            ).fetchone()
        return None if row is None else MeasurementRunnerRun.model_validate_json(row['payload_json'])

    def list_plans(self) -> tuple[MeasurementRunnerPlan, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_measurement_runner_plans ORDER BY created_at_utc, plan_id'
            ).fetchall()
        return tuple(MeasurementRunnerPlan.model_validate_json(r['payload_json']) for r in rows)

    def list_runs(self, plan_id: str) -> tuple[MeasurementRunnerRun, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_measurement_runner_runs WHERE plan_id=? ORDER BY started_at_utc, run_id',
                (plan_id,),
            ).fetchall()
        return tuple(MeasurementRunnerRun.model_validate_json(r['payload_json']) for r in rows)

    def _append_event(self, event: RunnerCellEvent) -> None:
        with self._connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_measurement_runner_events (
                    event_id, run_id, cell_index, status, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.run_id,
                    event.cell_index,
                    event.status,
                    event.created_at,
                    event.model_dump_json(),
                ),
            )

    def list_events(self, run_id: str) -> tuple[RunnerCellEvent, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_measurement_runner_events WHERE run_id=? ORDER BY created_at_utc, event_id',
                (run_id,),
            ).fetchall()
        return tuple(RunnerCellEvent.model_validate_json(r['payload_json']) for r in rows)

    def cell_states(self, run_id: str) -> dict[int, RunnerCellState]:
        run = self.get_run(run_id)
        if run is None:
            raise RunnerError('runner run is not persisted')
        plan = self.get_plan(run.plan_id)
        return resolve_cell_states(plan, self.list_events(run_id))

    def _plan_for_run(self, run_id: str) -> MeasurementRunnerPlan:
        run = self.get_run(run_id)
        if run is None:
            raise RunnerError('runner run is not persisted')
        plan = self.get_plan(run.plan_id)
        if plan.plan_sha256 != run.plan_sha256:
            raise RunnerError('runner run is bound to a different plan revision')
        return plan

    def mark_cell(
        self,
        run_id: str,
        cell_index: int,
        status: Literal['staged', 'assignment_incomplete', 'quality_pending'],
        *,
        created_at: str | None = None,
        reason: str = '',
    ) -> RunnerCellEvent:
        plan = self._plan_for_run(run_id)
        plan.cell(cell_index)
        event = RunnerCellEvent(
            event_id=str(uuid4()),
            run_id=run_id,
            cell_index=cell_index,
            status=status,
            reason=reason,
            created_at=created_at or _utc_now(),
        )
        self._append_event(event)
        return event

    def commit_cell(
        self,
        run_id: str,
        cell_index: int,
        *,
        measurement_id: str,
        dataset_id: str,
        dataset_sha256: str,
        quality_decision: Literal['passed', 'blocked', 'pending'] = 'pending',
        created_at: str | None = None,
        reason: str = '',
    ) -> RunnerCellEvent:
        """Commit an exact measurement to the planned cell.

        Quality verdicts decide the resulting state: 'passed' completes the
        cell, 'blocked' marks it retake_required (never completed), and
        'pending' leaves it quality_pending until a report lands.
        """
        plan = self._plan_for_run(run_id)
        plan.cell(cell_index)
        if self.measurement_repository is not None:
            record = self.measurement_repository.get_measurement(measurement_id)
            if record is None:
                raise RunnerError('committed measurement is not persisted')
            dataset = self.measurement_repository.get_dataset(dataset_id)
            if dataset is None or dataset.dataset_sha256 != dataset_sha256:
                raise RunnerError('committed dataset hash does not match persisted evidence')
        states = self.cell_states(run_id)
        superseded = None
        current = states[cell_index]
        if current.status == 'retake_required':
            superseded = current.measurement_id
        status: RunnerCellStatus = {
            'passed': 'completed',
            'blocked': 'retake_required',
            'pending': 'quality_pending',
        }[quality_decision]
        event = RunnerCellEvent(
            event_id=str(uuid4()),
            run_id=run_id,
            cell_index=cell_index,
            status=status,
            measurement_id=measurement_id,
            dataset_id=dataset_id,
            dataset_sha256=dataset_sha256,
            supersedes_measurement_id=superseded,
            reason=reason,
            created_at=created_at or _utc_now(),
        )
        self._append_event(event)
        return event

    def skip_cell(
        self,
        run_id: str,
        cell_index: int,
        *,
        created_at: str | None = None,
        reason: str = '',
    ) -> RunnerCellEvent:
        plan = self._plan_for_run(run_id)
        cell = plan.cell(cell_index)
        if not cell.allow_skip:
            raise RunnerError('plan does not allow skipping this cell')
        event = RunnerCellEvent(
            event_id=str(uuid4()),
            run_id=run_id,
            cell_index=cell_index,
            status='skipped',
            reason=reason,
            created_at=created_at or _utc_now(),
        )
        self._append_event(event)
        return event

    def next_incomplete(self, run_id: str) -> int | None:
        plan = self._plan_for_run(run_id)
        return next_incomplete_cell(plan, self.cell_states(run_id))
