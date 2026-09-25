"""Append-only persistence for system-health baselines/checks (#568).

Project-scope integrity (#744): the repository is the authority boundary,
so every write verifies the pinned chain stays inside one document —
``run.document_id == plan.document_id == baseline.document_id ==
revision.document_id`` — and runs must reproduce canonically from their
recorded inputs rather than merely carrying a self-consistent hash.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import TYPE_CHECKING, Callable

from .cad_repository import SceneRepository
from .cad_schema import require_native_tables
from .cad_system_health import (
    HealthAuthorityRef,
    HealthCheckPlan,
    HealthCheckRun,
    SystemHealthBaseline,
    run_health_check,
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

    ``preset_repository`` resolves ``operating_preset_id`` bindings so a
    baseline cannot pin a preset from another document. ``source_resolver``
    maps a project-scoped ``HealthAuthorityRef`` to its owning document id;
    refs the resolver reports as foreign are rejected, and unresolvable
    refs are rejected when a resolver is configured (they cannot silently
    contribute to a commissioned baseline).
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        ref_resolver: 'AuthorityRefResolver | None' = None,
        preset_repository: object | None = None,
        source_resolver: Callable[[HealthAuthorityRef], str | None] | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        if ref_resolver is None:
            from .cad_authority_refs import CanonicalAuthorityRefResolver

            ref_resolver = CanonicalAuthorityRefResolver(scene_repository)
        self.ref_resolver = ref_resolver
        self.preset_repository = preset_repository
        self.source_resolver = source_resolver
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
        # The pinned revision must belong to this document — a foreign
        # revision can never become this project's commissioned state.
        if revision.document_id != baseline.document_id:
            raise ValueError(
                'baseline SceneRevision belongs to another document'
            )
        if revision.content_hash != baseline.scene_content_hash:
            raise ValueError('baseline SceneRevision content hash mismatch')
        if baseline.operating_preset_id is not None and (
            self.preset_repository is not None
        ):
            preset = self.preset_repository.get_preset(
                baseline.operating_preset_id
            )
            if preset is None:
                raise ValueError(
                    'baseline pins an operating preset that is not persisted'
                )
            if preset.document_id != baseline.document_id:
                raise ValueError(
                    'baseline operating preset belongs to another document'
                )
            if preset.preset_sha256 != baseline.operating_preset_sha256:
                raise ValueError(
                    'baseline operating preset hash mismatch'
                )
        if self.source_resolver is not None:
            foreign: list[str] = []
            for ref in baseline.source_refs:
                owner = self.source_resolver(ref)
                if owner is not None and owner != baseline.document_id:
                    foreign.append(f'{ref.kind}:{ref.ref_id}')
            if foreign:
                raise ValueError(
                    'baseline pins source refs owned by another document: '
                    + ', '.join(sorted(foreign))
                )
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

    def _baseline_from_row(
        self, row: sqlite3.Row
    ) -> SystemHealthBaseline | None:
        """Fail-closed read: inconsistent legacy/corrupt rows never project.

        The persisted payload must agree with the row's document column
        and pin a same-document SceneRevision (#744).
        """

        try:
            baseline = SystemHealthBaseline.model_validate_json(
                row['payload_json']
            )
        except ValueError:
            return None
        if baseline.document_id != row['document_id']:
            return None
        revision = self.scene_repository.get(baseline.scene_revision_id)
        if (
            revision is None
            or revision.document_id != baseline.document_id
            or revision.content_hash != baseline.scene_content_hash
        ):
            return None
        return baseline

    def get_baseline(self, baseline_id: str) -> SystemHealthBaseline | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT document_id, payload_json'
                ' FROM cad_health_baselines WHERE baseline_id=?',
                (baseline_id,),
            ).fetchone()
        if row is None:
            return None
        return self._baseline_from_row(row)

    def list_baselines(
        self,
        document_id: str,
    ) -> tuple[SystemHealthBaseline, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT document_id, payload_json
                FROM cad_health_baselines
                WHERE document_id=?
                ORDER BY created_at_utc, baseline_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            baseline
            for baseline in (self._baseline_from_row(row) for row in rows)
            if baseline is not None
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
        if plan.document_id != baseline.document_id:
            raise ValueError(
                'health check plan belongs to a different document than its'
                ' baseline'
            )
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

    def _plan_from_row(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> HealthCheckPlan | None:
        """Fail-closed read: plan must agree with its row and its baseline."""

        try:
            plan = HealthCheckPlan.model_validate_json(row['payload_json'])
        except ValueError:
            return None
        if (
            plan.document_id != row['document_id']
            or plan.baseline_id != row['baseline_id']
            or plan.baseline_sha256 != row['baseline_sha256']
        ):
            return None
        baseline_row = connection.execute(
            'SELECT document_id, baseline_sha256'
            ' FROM cad_health_baselines WHERE baseline_id=?',
            (plan.baseline_id,),
        ).fetchone()
        if (
            baseline_row is None
            or baseline_row['document_id'] != plan.document_id
            or baseline_row['baseline_sha256'] != plan.baseline_sha256
        ):
            return None
        return plan

    def get_plan(self, plan_id: str) -> HealthCheckPlan | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT document_id, baseline_id, baseline_sha256,'
                ' payload_json'
                ' FROM cad_health_check_plans WHERE plan_id=?',
                (plan_id,),
            ).fetchone()
            if row is None:
                return None
            return self._plan_from_row(connection, row)

    def list_plans(
        self,
        baseline_id: str,
    ) -> tuple[HealthCheckPlan, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT document_id, baseline_id, baseline_sha256, payload_json
                FROM cad_health_check_plans
                WHERE baseline_id=?
                ORDER BY created_at_utc, plan_id
                """,
                (baseline_id,),
            ).fetchall()
            return tuple(
                plan
                for plan in (
                    self._plan_from_row(connection, row) for row in rows
                )
                if plan is not None
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
        if run.document_id != plan.document_id:
            raise ValueError(
                'health run belongs to a different document than its plan'
            )
        baseline = self.get_baseline(plan.baseline_id)
        if (
            baseline is None
            or baseline.baseline_sha256 != plan.baseline_sha256
            or baseline.document_id != plan.document_id
        ):
            raise ValueError(
                'health run baseline lineage is not resolvable within this'
                ' document'
            )
        # A persisted run is derived authority: its assessments must
        # reproduce exactly from the persisted plan + baseline + recorded
        # observations, not merely carry a self-consistent hash (#744).
        check_ids = {item.check_id for item in plan.checks}
        unknown = [
            item.check_id
            for item in run.observations
            if item.check_id not in check_ids
        ]
        if unknown:
            raise ValueError(
                'health run records observations for checks outside the'
                f' plan: {sorted(unknown)}'
            )
        expected = run_health_check(
            plan,
            baseline,
            observations=run.observations,
            trigger=run.trigger,
            cause_hypothesis=run.cause_hypothesis,
            created_at_utc=run.created_at_utc,
            run_id=run.run_id,
        )
        if (
            expected.assessments != run.assessments
            or expected.run_sha256 != run.run_sha256
        ):
            raise ValueError(
                'health run assessments do not reproduce from canonical'
                ' inputs'
            )
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

    def _run_from_row(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> HealthCheckRun | None:
        """Fail-closed read: run must agree with its row and its plan."""

        try:
            run = HealthCheckRun.model_validate_json(row['payload_json'])
        except ValueError:
            return None
        if (
            run.document_id != row['document_id']
            or run.plan_id != row['plan_id']
        ):
            return None
        plan_row = connection.execute(
            'SELECT document_id, plan_sha256'
            ' FROM cad_health_check_plans WHERE plan_id=?',
            (run.plan_id,),
        ).fetchone()
        if (
            plan_row is None
            or plan_row['document_id'] != run.document_id
            or plan_row['plan_sha256'] != run.plan_sha256
        ):
            return None
        return run

    def get_run(self, run_id: str) -> HealthCheckRun | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT document_id, plan_id, payload_json'
                ' FROM cad_health_check_runs WHERE run_id=?',
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            return self._run_from_row(connection, row)

    def list_runs(
        self,
        plan_id: str,
    ) -> tuple[HealthCheckRun, ...]:
        """All runs for one plan, oldest first — append-only history."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT document_id, plan_id, payload_json
                FROM cad_health_check_runs
                WHERE plan_id=?
                ORDER BY created_at_utc, run_id
                """,
                (plan_id,),
            ).fetchall()
            return tuple(
                run
                for run in (
                    self._run_from_row(connection, row) for row in rows
                )
                if run is not None
            )

    def list_document_runs(
        self,
        document_id: str,
    ) -> tuple[HealthCheckRun, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT document_id, plan_id, payload_json
                FROM cad_health_check_runs
                WHERE document_id=?
                ORDER BY created_at_utc, run_id
                """,
                (document_id,),
            ).fetchall()
            return tuple(
                run
                for run in (
                    self._run_from_row(connection, row) for row in rows
                )
                if run is not None
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
