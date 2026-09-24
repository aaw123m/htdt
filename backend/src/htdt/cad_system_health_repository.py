"""Append-only persistence for system-health baselines/checks (#568)."""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import TYPE_CHECKING

from .cad_repository import SceneRepository
from .cad_system_health import (
    HealthAuthorityRef,
    HealthCheckPlan,
    HealthCheckRun,
    SystemHealthBaseline,
)

if TYPE_CHECKING:
    from .cad_authority_refs import AuthorityRefResolver


class SystemHealthConflictError(ValueError):
    """A baseline/plan/run save violated append-only identity rules."""


#: HealthAuthorityRef kinds that name canonical persisted authorities. Other
#: kinds (external instruments, declared provenance) have no canonical table
#: and stay declared-only — absence is never fabricated as a failure.
_AUTHORITY_RESOLVER_KINDS: dict[str, str] = {
    'operating_preset': 'operating_preset',
    'system_variant': 'system_variant',
    'measurement': 'measurement',
    'design_checkpoint': 'design_checkpoint',
    'design_comparison_set': 'design_comparison_set',
    'prediction': 'prediction',
    'validation': 'validation',
    'standards': 'standards',
    'robustness': 'robustness',
    'scene_revision': 'scene_revision',
    'as_built': 'as_built',
    'measured_state': 'measured_state',
}


class CadSystemHealthRepository:
    """Native storage for health baselines, check plans and check runs.

    All three tables are append-only: repeated verifications form history,
    and a new run never rewrites the baseline or earlier runs.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        ref_resolver: 'AuthorityRefResolver | None' = None,
    ) -> None:
        self.scene_repository = scene_repository
        if ref_resolver is None:
            from .cad_authority_refs import CanonicalAuthorityRefResolver

            ref_resolver = CanonicalAuthorityRefResolver(scene_repository)
        self.ref_resolver = ref_resolver
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
                CREATE TABLE IF NOT EXISTS cad_health_baselines (
                    baseline_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    baseline_sha256 TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_health_check_plans (
                    plan_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    baseline_id TEXT NOT NULL,
                    baseline_sha256 TEXT NOT NULL,
                    plan_sha256 TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_health_check_runs (
                    run_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    run_sha256 TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )

    # ------------------------------------------------------------------
    # Baselines

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

    # ------------------------------------------------------------------
    # Persisted-record verification (#757 semantic audit)

    def verify_persisted_baseline(
        self, baseline_id: str
    ) -> SystemHealthBaseline:
        """Re-run baseline invariants on a persisted row.

        The scene pin must resolve, the document scope must match, the
        operating-preset binding (when pinned) must resolve to the exact
        preset revision, and every authority ref naming a canonical kind
        must resolve, stay in-project, and match its semantic hash.
        """
        baseline = self.get_baseline(baseline_id)
        if baseline is None:
            raise ValueError(f'health baseline {baseline_id} no longer resolves')
        revision = self.scene_repository.get(baseline.scene_revision_id)
        if revision is None:
            raise ValueError('baseline pins a SceneRevision that is not persisted')
        if revision.content_hash != baseline.scene_content_hash:
            raise ValueError('baseline SceneRevision content hash mismatch')
        if revision.document_id != baseline.document_id:
            raise ValueError('baseline SceneRevision belongs to another document')
        if baseline.operating_preset_id is not None:
            self._assert_resolves(
                'operating_preset',
                baseline.operating_preset_id,
                baseline.operating_preset_sha256,
                baseline.document_id,
            )
        for ref in baseline.source_refs + baseline.instrument_refs:
            self._assert_health_ref(ref, baseline.document_id)
        return baseline

    def verify_persisted_plan(self, plan_id: str) -> HealthCheckPlan:
        plan = self.get_plan(plan_id)
        if plan is None:
            raise ValueError(f'health check plan {plan_id} no longer resolves')
        baseline = self.get_baseline(plan.baseline_id)
        if baseline is None:
            raise ValueError('health check plan requires a persisted baseline')
        if baseline.baseline_sha256 != plan.baseline_sha256:
            raise ValueError('health check plan is bound to a different baseline')
        if baseline.document_id != plan.document_id:
            raise ValueError('health check plan belongs to another document')
        for check in plan.checks:
            for ref in check.required_context:
                self._assert_health_ref(ref, plan.document_id)
        return plan

    def verify_persisted_run(self, run_id: str) -> HealthCheckRun:
        run = self.get_run(run_id)
        if run is None:
            raise ValueError(f'health check run {run_id} no longer resolves')
        plan = self.get_plan(run.plan_id)
        if plan is None:
            raise ValueError('health run requires a persisted check plan')
        if plan.plan_sha256 != run.plan_sha256:
            raise ValueError('health run is bound to a different plan')
        if plan.document_id != run.document_id:
            raise ValueError('health run belongs to another document')
        return run

    def _assert_health_ref(
        self, ref: HealthAuthorityRef, document_id: str
    ) -> None:
        resolver_kind = _AUTHORITY_RESOLVER_KINDS.get(ref.kind)
        if resolver_kind is None:
            return
        self._assert_resolves(
            resolver_kind, ref.ref_id, ref.ref_sha256, document_id
        )

    def _assert_resolves(
        self,
        resolver_kind: str,
        ref_id: str,
        ref_sha256: str | None,
        document_id: str,
    ) -> None:
        resolved = self.ref_resolver.resolve(resolver_kind, ref_id, document_id)
        if resolved is None:
            raise ValueError(
                f'references a {resolver_kind} authority that does not resolve'
            )
        if (
            resolved.document_id is not None
            and resolved.document_id != document_id
        ):
            raise ValueError(
                f'{resolver_kind} authority belongs to another document'
            )
        if resolved.semantic_sha256 is not None:
            if ref_sha256 is None:
                raise ValueError(
                    f'ref must pin the {resolver_kind} semantic hash to claim '
                    'an exact reference'
                )
            if ref_sha256 != resolved.semantic_sha256:
                raise ValueError(
                    f'{resolver_kind} hash does not match the canonical authority'
                )
        elif ref_sha256 is not None:
            raise ValueError(
                f'ref supplies a hash the id-only {resolver_kind} authority '
                'does not expose'
            )
