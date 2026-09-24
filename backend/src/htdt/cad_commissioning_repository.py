"""Append-only persistence for commissioning verification (#520)."""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_commissioning import (
    CommissioningPlan,
    CommissioningRun,
    ToleranceProfile,
)
from .cad_repository import SceneRepository


class CommissioningConflictError(ValueError):
    """A commissioning save violated append-only identity rules."""


class CadCommissioningRepository:
    """Native storage for tolerance profiles, plans and runs.

    All three record types are immutable and append-only: a plan pins the
    tolerance profile's hash, a run pins the plan's hash, and none can be
    edited in place — later verification work appends new records.
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
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_tolerance_profiles (
                    profile_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    profile_sha256 TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_commissioning_plans (
                    plan_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    scene_revision_id TEXT NOT NULL,
                    tolerance_profile_id TEXT NOT NULL,
                    plan_sha256 TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_commissioning_runs (
                    run_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    run_sha256 TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY (plan_id)
                        REFERENCES cad_commissioning_plans (plan_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_cad_commissioning_runs_plan
                ON cad_commissioning_runs (plan_id, created_at_utc)
                """
            )

    def save_tolerance_profile(self, profile: ToleranceProfile) -> None:
        if self.get_tolerance_profile(profile.profile_id) is not None:
            raise CommissioningConflictError(
                'ToleranceProfile ids are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_tolerance_profiles (
                    profile_id, document_id, name, version,
                    profile_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.document_id,
                    profile.name,
                    profile.version,
                    profile.profile_sha256,
                    profile.created_at_utc,
                    profile.model_dump_json(),
                ),
            )

    def get_tolerance_profile(
        self, profile_id: str
    ) -> ToleranceProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_tolerance_profiles
                WHERE profile_id=?
                """,
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        return ToleranceProfile.model_validate_json(row['payload_json'])

    def list_tolerance_profiles(
        self, document_id: str
    ) -> tuple[ToleranceProfile, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_tolerance_profiles
                WHERE document_id=?
                ORDER BY created_at_utc, profile_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            ToleranceProfile.model_validate_json(row['payload_json'])
            for row in rows
        )

    def save_plan(self, plan: CommissioningPlan) -> None:
        if self.get_plan(plan.plan_id) is not None:
            raise CommissioningConflictError(
                'CommissioningPlan ids are append-only'
            )
        profile = self.get_tolerance_profile(plan.tolerance_profile_id)
        if profile is None:
            raise ValueError('plan references unknown tolerance profile')
        if profile.profile_sha256 != plan.tolerance_profile_sha256:
            raise ValueError('plan tolerance profile hash mismatch')
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_commissioning_plans (
                    plan_id, document_id, scene_revision_id,
                    tolerance_profile_id, plan_sha256, created_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.plan_id,
                    plan.document_id,
                    plan.scene_revision_id,
                    plan.tolerance_profile_id,
                    plan.plan_sha256,
                    plan.created_at_utc,
                    plan.model_dump_json(),
                ),
            )

    def get_plan(self, plan_id: str) -> CommissioningPlan | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_commissioning_plans
                WHERE plan_id=?
                """,
                (plan_id,),
            ).fetchone()
        if row is None:
            return None
        return CommissioningPlan.model_validate_json(row['payload_json'])

    def list_plans(
        self, document_id: str
    ) -> tuple[CommissioningPlan, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_commissioning_plans
                WHERE document_id=?
                ORDER BY created_at_utc, plan_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            CommissioningPlan.model_validate_json(row['payload_json'])
            for row in rows
        )

    def save_run(self, run: CommissioningRun) -> None:
        if self.get_run(run.run_id) is not None:
            raise CommissioningConflictError(
                'CommissioningRun ids are append-only'
            )
        plan = self.get_plan(run.plan_id)
        if plan is None:
            raise ValueError('run references unknown commissioning plan')
        if plan.plan_sha256 != run.plan_sha256:
            raise ValueError('run plan hash differs from the stored plan')
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_commissioning_runs (
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

    def get_run(self, run_id: str) -> CommissioningRun | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_commissioning_runs
                WHERE run_id=?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return CommissioningRun.model_validate_json(row['payload_json'])

    def list_runs(
        self, plan_id: str
    ) -> tuple[CommissioningRun, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_commissioning_runs
                WHERE plan_id=?
                ORDER BY created_at_utc, run_id
                """,
                (plan_id,),
            ).fetchall()
        return tuple(
            CommissioningRun.model_validate_json(row['payload_json'])
            for row in rows
        )


__all__ = ['CadCommissioningRepository', 'CommissioningConflictError']
