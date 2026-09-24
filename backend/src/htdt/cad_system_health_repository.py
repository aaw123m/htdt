"""Append-only persistence for system-health baselines/checks (#568)."""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import require_native_tables
from .cad_system_health import (
    HealthCheckPlan,
    HealthCheckRun,
    SystemHealthBaseline,
)


class SystemHealthConflictError(ValueError):
    """A baseline/plan/run save violated append-only identity rules."""


class CadSystemHealthRepository:
    """Native storage for health baselines, check plans and check runs.

    All three tables are append-only: repeated verifications form history,
    and a new run never rewrites the baseline or earlier runs.
    """

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            require_native_tables(
                connection,
                'cad_health_baselines',
                'cad_health_check_plans',
                'cad_health_check_runs',
            )


    def save_baseline(self, baseline: SystemHealthBaseline) -> None:
        if self.get_baseline(baseline.baseline_id) is not None:
            raise SystemHealthConflictError(
                'SystemHealthBaseline ids are append-only'
            )
        revision = self.scene_repository.get(baseline.scene_revision_id)
        if revision is None:
            raise ValueError('baseline pins a SceneRevision that is not persisted')
        if revision.content_hash != baseline.scene_content_hash:
            raise ValueError('baseline SceneRevision content hash mismatch')
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_health_baselines (
                    baseline_id, document_id, baseline_sha256,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    baseline.baseline_id,
                    baseline.document_id,
                    baseline.baseline_sha256,
                    baseline.created_at_utc,
                    baseline.model_dump_json(),
                ),
            )

    def get_baseline(self, baseline_id: str) -> SystemHealthBaseline | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_health_baselines WHERE baseline_id=?',
                (baseline_id,),
            ).fetchone()
        if row is None:
            return None
        return SystemHealthBaseline.model_validate_json(row['payload_json'])

    def list_baselines(
        self,
        document_id: str,
    ) -> tuple[SystemHealthBaseline, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_health_baselines
                WHERE document_id=?
                ORDER BY created_at_utc, baseline_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            SystemHealthBaseline.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Plans

    def save_plan(self, plan: HealthCheckPlan) -> None:
        if self.get_plan(plan.plan_id) is not None:
            raise SystemHealthConflictError('HealthCheckPlan ids are append-only')
        baseline = self.get_baseline(plan.baseline_id)
        if baseline is None:
            raise ValueError('health check plan requires a persisted baseline')
        if baseline.baseline_sha256 != plan.baseline_sha256:
            raise ValueError('health check plan is bound to a different baseline')
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_health_check_plans (
                    plan_id, document_id, baseline_id, baseline_sha256,
                    plan_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.plan_id,
                    plan.document_id,
                    plan.baseline_id,
                    plan.baseline_sha256,
                    plan.plan_sha256,
                    plan.created_at_utc,
                    plan.model_dump_json(),
                ),
            )

    def get_plan(self, plan_id: str) -> HealthCheckPlan | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_health_check_plans WHERE plan_id=?',
                (plan_id,),
            ).fetchone()
        if row is None:
            return None
        return HealthCheckPlan.model_validate_json(row['payload_json'])

    def list_plans(
        self,
        baseline_id: str,
    ) -> tuple[HealthCheckPlan, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_health_check_plans
                WHERE baseline_id=?
                ORDER BY created_at_utc, plan_id
                """,
                (baseline_id,),
            ).fetchall()
        return tuple(
            HealthCheckPlan.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Runs

    def save_run(self, run: HealthCheckRun) -> None:
        if self.get_run(run.run_id) is not None:
            raise SystemHealthConflictError('HealthCheckRun ids are append-only')
        plan = self.get_plan(run.plan_id)
        if plan is None:
            raise ValueError('health run requires a persisted check plan')
        if plan.plan_sha256 != run.plan_sha256:
            raise ValueError('health run is bound to a different plan')
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_health_check_runs (
                    run_id, document_id, plan_id, run_sha256,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.document_id,
                    run.plan_id,
                    run.run_sha256,
                    run.created_at_utc,
                    run.model_dump_json(),
                ),
            )

    def get_run(self, run_id: str) -> HealthCheckRun | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_health_check_runs WHERE run_id=?',
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return HealthCheckRun.model_validate_json(row['payload_json'])

    def list_runs(
        self,
        plan_id: str,
    ) -> tuple[HealthCheckRun, ...]:
        """All runs for one plan, oldest first — append-only history."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_health_check_runs
                WHERE plan_id=?
                ORDER BY created_at_utc, run_id
                """,
                (plan_id,),
            ).fetchall()
        return tuple(
            HealthCheckRun.model_validate_json(row['payload_json'])
            for row in rows
        )

    def list_document_runs(
        self,
        document_id: str,
    ) -> tuple[HealthCheckRun, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_health_check_runs
                WHERE document_id=?
                ORDER BY created_at_utc, run_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            HealthCheckRun.model_validate_json(row['payload_json'])
            for row in rows
        )
