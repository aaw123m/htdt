"""Append-only persistence for the #1030 lifecycle-gate runner.

Two sealed stores behind the shared ``_SealedStore`` machinery
(save-time seal re-verification, read-time column-vs-payload checks):

* ``cad_gate_operator_plans`` — deterministic minimal-step operator
  plans per lifecycle gate (same manifest + gate → same plan id, so
  re-planning is a no-op append).
* ``cad_gate_acceptance_runs`` — sealed per-execution acceptance
  records, including runs imported from other machines after full
  re-verification.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .cad_calibration_deployment_repository import _SealedStore
from .cad_lifecycle_gates import CadGateAcceptanceRun, GateOperatorPlan


class CadLifecycleGateRepository:
    """Native storage for the #1030 lifecycle-gate authority."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_gate_operator_plans',
                'cad_gate_acceptance_runs',
            )
        self.plans = _SealedStore(
            self._connect,
            'cad_gate_operator_plans',
            GateOperatorPlan,
            'plan_id',
            'plan_sha256',
            (
                ('document_id', '__document_id__'),
                ('issue', 'issue'),
                ('gate_index', 'gate_index'),
                ('gate_kind', 'gate_kind'),
                ('lifecycle', 'lifecycle'),
                ('manifest_sha256', 'manifest_sha256'),
            ),
        )
        self.runs = _SealedStore(
            self._connect,
            'cad_gate_acceptance_runs',
            CadGateAcceptanceRun,
            'run_id',
            'run_sha256',
            (
                ('document_id', '__document_id__'),
                ('issue', 'issue'),
                ('gate_index', 'gate_index'),
                ('gate_kind', 'gate_kind'),
                ('plan_ref_id', 'plan_ref.ref_id'),
                ('verdict', 'verdict'),
                ('started_at_utc', 'started_at_utc'),
                ('finished_at_utc', 'finished_at_utc'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    # -- writes ---------------------------------------------------------------

    def save_plan(self, plan: GateOperatorPlan) -> None:
        self.plans.save(plan)

    def save_run(self, run: CadGateAcceptanceRun) -> None:
        self.runs.save(run)

    # -- reads ----------------------------------------------------------------

    def get_plan(self, plan_id: str) -> GateOperatorPlan | None:
        return self.plans.get(plan_id)

    def list_plans(
        self,
        document_id: str | None = None,
        *,
        issue: int | None = None,
    ) -> list[GateOperatorPlan]:
        clauses: list[str] = []
        params: list[object] = []
        if document_id is not None:
            clauses.append('document_id = ?')
            params.append(document_id)
        if issue is not None:
            clauses.append('issue = ?')
            params.append(issue)
        sql = 'SELECT plan_id FROM cad_gate_operator_plans'
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(sql, tuple(params)).fetchall()
        plans: list[GateOperatorPlan] = []
        for row in rows:
            plan = self.get_plan(row[0])
            if plan is None:
                raise RuntimeError(  # error-boundary: sealed store read
                    f'gate operator plan {row[0]} unreadable')
            plans.append(plan)
        return plans

    def get_run(self, run_id: str) -> CadGateAcceptanceRun | None:
        return self.runs.get(run_id)

    def list_runs(
        self,
        document_id: str | None = None,
        *,
        issue: int | None = None,
        gate_index: int | None = None,
    ) -> list[CadGateAcceptanceRun]:
        clauses: list[str] = []
        params: list[object] = []
        if document_id is not None:
            clauses.append('document_id = ?')
            params.append(document_id)
        if issue is not None:
            clauses.append('issue = ?')
            params.append(issue)
        if gate_index is not None:
            clauses.append('gate_index = ?')
            params.append(gate_index)
        sql = 'SELECT run_id FROM cad_gate_acceptance_runs'
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(sql, tuple(params)).fetchall()
        runs: list[CadGateAcceptanceRun] = []
        for row in rows:
            run = self.get_run(row[0])
            if run is None:
                raise RuntimeError(  # error-boundary: sealed store read
                    f'gate acceptance run {row[0]} unreadable')
            runs.append(run)
        return runs


__all__ = ['CadLifecycleGateRepository']
