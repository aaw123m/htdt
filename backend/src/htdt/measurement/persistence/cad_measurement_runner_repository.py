"""Append-only persistence and transition rules for the campaign runner."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Literal
from uuid import uuid4

from ..services.cad_measurement_effective import CadEffectiveMeasurementResolver
from ..domain.cad_measurement_quality import dataset_sha256, measurement_sha256
from .cad_measurement_quality_repository import CadMeasurementQualityRepository
from ..domain.cad_measurement_runner import (
    MeasurementRunnerPlan,
    MeasurementRunnerRun,
    RunnerCellEvent,
    RunnerCellState,
    RunnerCellStatus,
    next_incomplete_cell,
    resolve_cell_states,

)
from ...cad_scene import is_measurement_target_eligible

from ...cad_schema import connect_sqlite, require_native_tables
from ...clock import utc_now_iso as _utc_now


class RunnerError(ValueError):
    pass


class CadMeasurementRunnerRepository:
    """Runner plans, runs, and per-cell transition events.

    Plans pin ``document_id`` + the exact scene revision (#853): writes
    re-validate the bound scene and cell entities, commits resolve the
    effective measurement binding (disposition + correction, #509/#844)
    and derive cell status from the latest replay-validated quality
    report — a caller cannot assert a quality verdict the authority has
    not itself verified.
    """

    def __init__(
        self,
        scene_repository,
        measurement_repository=None,
        quality_repository=None,
    ) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        self.measurement_repository = measurement_repository
        if quality_repository is None and measurement_repository is not None:
            quality_repository = CadMeasurementQualityRepository(
                measurement_repository
            )
        self.quality_repository = quality_repository
        self._effective = (
            CadEffectiveMeasurementResolver(
                measurement_repository, quality_repository
            )
            if measurement_repository is not None
            else None
        )
        self._initialize()

    def _connect(self):
        return closing(connect_sqlite(self.path))

    def _initialize(self) -> None:
        with self._connect() as connection, connection:
            require_native_tables(connection, 'cad_measurement_runner_plans', 'cad_measurement_runner_runs', 'cad_measurement_runner_events')

    def _validate_plan_scene_binding(
        self, plan: MeasurementRunnerPlan
    ) -> None:
        """The plan's scene revision must exist and bind the plan's document.

        Called on writes and authoritative reads alike so a runner plan
        cannot silently bind a foreign project's scene or a tampered
        content hash (#853).
        """
        revision = self.scene_repository.get(plan.scene_revision_id)
        if revision is None:
            raise RunnerError('runner plan scene revision is not persisted')
        if revision.document_id != plan.document_id:
            raise RunnerError('runner plan binds a foreign project scene')
        if revision.content_hash != plan.scene_content_hash:
            raise RunnerError('runner plan scene content hash mismatch')
        entities = {entity.entity_id: entity for entity in revision.document.entities}
        for cell in plan.cells:
            for speaker_id in cell.source_speaker_ids:
                speaker = entities.get(speaker_id)
                if speaker is None or speaker.kind != 'speaker':
                    raise RunnerError(
                        f'runner cell source is not a scene speaker: {speaker_id}'
                    )
            target = entities.get(cell.target_entity_id)
            if target is None or not is_measurement_target_eligible(target):
                raise RunnerError(
                    'runner cell target is not an eligible measurement point: '
                    f'{cell.target_entity_id}'
                )

    def save_plan(self, plan: MeasurementRunnerPlan) -> None:
        if self.get_plan(plan.plan_id) is not None:
            raise RunnerError('runner plans are append-only')
        self._validate_plan_scene_binding(plan)
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
        if row is None:
            return None
        plan = MeasurementRunnerPlan.model_validate_json(row['payload_json'])
        self._validate_plan_scene_binding(plan)
        return plan

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

    def list_plans(self, document_id: str) -> tuple[MeasurementRunnerPlan, ...]:
        """Plans of one project only — never a global list (#853)."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM cad_measurement_runner_plans "
                "WHERE json_extract(payload_json, '$.document_id')=? "
                "ORDER BY created_at_utc, plan_id",
                (document_id,),
            ).fetchall()
        plans = tuple(
            MeasurementRunnerPlan.model_validate_json(r['payload_json'])
            for r in rows
        )
        return tuple(plan for plan in plans if plan.document_id == document_id)

    def list_plan_created_at_utc(self, document_id: str) -> dict[str, str]:
        """``plan_id -> created_at_utc`` for one project's plans (#578).

        The persisted timestamp lives on the row, not the plan payload —
        surfaces showing a human ``保存`` label for an unnamed plan read it
        here in one pass.
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT plan_id, created_at_utc "
                "FROM cad_measurement_runner_plans "
                "WHERE json_extract(payload_json, '$.document_id')=? "
                "ORDER BY created_at_utc, plan_id",
                (document_id,),
            ).fetchall()
        return {
            str(row['plan_id']): str(row['created_at_utc'])
            for row in rows
        }

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
                'SELECT payload_json FROM cad_measurement_runner_events '
                'WHERE run_id=? ORDER BY rowid',
                (run_id,),
            ).fetchall()
        return tuple(RunnerCellEvent.model_validate_json(r['payload_json']) for r in rows)

    def cell_states(self, run_id: str) -> dict[int, RunnerCellState]:
        plan = self._plan_for_run(run_id)
        events = self.list_events(run_id)
        # Read-side revalidation (#853): every persisted event must bind a
        # planned cell of this run's plan; a foreign/corrupt row fails
        # closed rather than silently skewing resolution.
        for event in events:
            if event.cell_index >= len(plan.cells):
                raise RunnerError('runner event binds an unplanned cell')
        return resolve_cell_states(plan, events)

    def progress_for_run(
        self, run_id: str
    ) -> tuple[MeasurementRunnerPlan, dict[int, RunnerCellState]]:
        """One progress read: run, bound plan, and resolved cell states.

        ``get_run`` + ``get_plan`` + ``cell_states`` walks the run→plan
        indirection twice and re-validates the plan's scene binding on each
        ``get_plan`` call; this path reads each authority once while keeping
        the same fail-closed checks (run persisted, plan bound, events
        planned).
        """
        run = self.get_run(run_id)
        if run is None:
            raise RunnerError('runner run is not persisted')
        plan = self.get_plan(run.plan_id)
        if plan is None or plan.plan_sha256 != run.plan_sha256:
            raise RunnerError('runner run is bound to a different plan revision')
        events = self.list_events(run_id)
        for event in events:
            if event.cell_index >= len(plan.cells):
                raise RunnerError('runner event binds an unplanned cell')
        return plan, resolve_cell_states(plan, events)

    def _plan_for_run(self, run_id: str) -> MeasurementRunnerPlan:
        run = self.get_run(run_id)
        if run is None:
            raise RunnerError('runner run is not persisted')
        plan = self.get_plan(run.plan_id)
        if plan is None or plan.plan_sha256 != run.plan_sha256:
            raise RunnerError('runner run is bound to a different plan revision')
        return plan

    def _cell_for(self, plan: MeasurementRunnerPlan, cell_index: int):
        try:
            return plan.cell(cell_index)
        except IndexError:
            raise RunnerError('runner cell index is not in the plan') from None

    def _require_open_cell(
        self, run_id: str, cell_index: int, action: str
    ) -> RunnerCellState:
        """Transition legality (#853): terminal cells accept nothing, a
        retake-required cell only accepts a fresh commit."""
        state = self.cell_states(run_id)[cell_index]
        if state.status in ('completed', 'skipped'):
            raise RunnerError('runner cell is terminal')
        if state.status == 'retake_required' and action != 'commit':
            raise RunnerError(
                'retake_required cells only accept a new measurement commit'
            )
        return state

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
        self._cell_for(plan, cell_index)
        self._require_open_cell(run_id, cell_index, 'mark')
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
        created_at: str | None = None,
        reason: str = '',
    ) -> RunnerCellEvent:
        """Commit an exact measurement to the planned cell.

        The runner canonically derives the resulting state itself (#853):
        the evidence must be currently eligible (disposition/correction),
        its effective binding must match the planned cell's entity/channel/
        source set and the plan's scene revision, and the resulting status
        comes from the latest replay-validated quality report — no report
        or an unknown verdict leaves the cell ``quality_pending``, RETAKE
        marks it ``retake_required``, a clean report completes it. Callers
        never assert a verdict.
        """
        plan = self._plan_for_run(run_id)
        cell = self._cell_for(plan, cell_index)
        current = self._require_open_cell(run_id, cell_index, 'commit')
        if self._effective is not None:
            try:
                evidence = self._effective.require_normal_use(
                    measurement_id, purpose='measurement runner cell evidence'
                )
            except ValueError as exc:
                raise RunnerError(str(exc)) from exc
            record = evidence.measurement
            dataset = self.measurement_repository.get_dataset(dataset_id)
            if (
                dataset is None
                or dataset.measurement_id != measurement_id
                or dataset.dataset_sha256 != dataset_sha256
            ):
                raise RunnerError('committed dataset hash does not match persisted evidence')
            if (
                record.document_id != plan.document_id
                or record.scene_revision_id != plan.scene_revision_id
                or record.scene_content_hash != plan.scene_content_hash
            ):
                raise RunnerError('committed measurement binds a different scene/document')
            if (
                evidence.measurement_entity_id != cell.target_entity_id
                or evidence.channel_role != cell.channel_role
                or tuple(sorted(evidence.source_speaker_ids))
                != tuple(sorted(cell.source_speaker_ids))
            ):
                raise RunnerError('committed measurement does not bind the planned cell')
        superseded = None
        if current.status == 'retake_required':
            superseded = current.measurement_id
        status = self._derived_commit_status(measurement_id)
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

    def _derived_commit_status(self, measurement_id: str) -> RunnerCellStatus:
        """Cell outcome from the measurement's latest quality authority (#853)."""
        if self._effective is None or self.quality_repository is None:
            return 'quality_pending'
        record = self.measurement_repository.get_measurement(measurement_id)
        dataset = self.measurement_repository.dataset_for_measurement(measurement_id)
        report = self.quality_repository.latest_report(measurement_id)
        if record is None or dataset is None or report is None:
            return 'quality_pending'
        if report.measurement_sha256 != measurement_sha256(record):
            raise RunnerError('measurement quality report measurement hash mismatch')
        if report.dataset_sha256 != dataset_sha256(dataset):
            raise RunnerError('measurement quality report dataset hash mismatch')
        if report.retake_recommendation == 'RETAKE':
            return 'retake_required'
        if report.retake_recommendation == 'NOT_NEEDED':
            return 'completed'
        return 'quality_pending'

    def skip_cell(
        self,
        run_id: str,
        cell_index: int,
        *,
        created_at: str | None = None,
        reason: str = '',
    ) -> RunnerCellEvent:
        plan = self._plan_for_run(run_id)
        cell = self._cell_for(plan, cell_index)
        self._require_open_cell(run_id, cell_index, 'skip')
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
