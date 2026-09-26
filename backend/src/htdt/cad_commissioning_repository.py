"""Append-only persistence for commissioning verification (#520).

Persistence reproduces every claim: plans resolve their pinned
SceneRevision/SystemVariant/profile inside the same document and resolve
each check's subject against the pinned design, and ``save_run`` replays
``build_commissioning_run`` from the stored plan/profile plus the run's own
observations — a caller-supplied result that does not match the canonical
replay is rejected, so a forged PASS can never persist.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Mapping

from .cad_authority_resolver import (
    ExactAuthorityResolver,
    KindResolver,
    ResolvedAuthority,
)
from .cad_commissioning import (
    CommissioningCheck,
    CommissioningPlan,
    CommissioningRun,
    ToleranceProfile,
    build_commissioning_run,
)
from .cad_repository import SceneRepository
from .cad_schema import require_native_tables
from .cad_system_variant_repository import CadSystemVariantRepository


class CommissioningConflictError(ValueError):
    """A commissioning save violated append-only identity rules."""


class CadCommissioningRepository:
    """Native storage for tolerance profiles, plans and runs.

    All three record types are immutable and append-only: a plan pins the
    tolerance profile's hash plus the exact scene/variant identity, a run
    pins the plan's hash, and none can be edited in place — later
    verification work appends new records.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        system_variant_repository: CadSystemVariantRepository | None = None,
        design_decision_repository=None,
        kind_resolvers: Mapping[str, KindResolver] | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.system_variant_repository = system_variant_repository
        resolvers: dict[str, KindResolver] = dict(kind_resolvers or {})
        if design_decision_repository is not None:
            def _resolve_design_decision(ref_id: str):
                decision = design_decision_repository.get_decision(ref_id)
                if decision is None:
                    return None
                return ResolvedAuthority(
                    kind='design_decision',
                    ref_id=ref_id,
                    document_id=decision.document_id,
                    semantic_sha256=decision.decision_sha256,
                )

            resolvers.setdefault('design_decision', _resolve_design_decision)
        self.resolver = ExactAuthorityResolver(
            scene_repository,
            system_variant_repository=system_variant_repository,
            kind_resolvers=resolvers,
        )
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        # #767: persistent schema is owned by the migration authority;
        # repositories verify the migrated contract, never converge it.
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_tolerance_profiles', 'cad_commissioning_plans', 'cad_commissioning_runs')

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

    def _resolve_plan_authority(self, plan: CommissioningPlan) -> None:
        """Resolve every authority pin a plan claims.

        The pinned SceneRevision must exist inside ``plan.document_id`` and
        match the stored content hash; a pinned SystemVariant must exist,
        belong to the same document, match its pinned semantic hash and be
        based on exactly the pinned revision; the tolerance profile must
        belong to the same document; and every check subject must resolve
        against the pinned design — a check naming a nonexistent entity,
        channel or foreign authority fails the save.
        """

        revision = self.scene_repository.get(plan.scene_revision_id)
        if revision is None:
            raise ValueError('plan references a missing SceneRevision')
        if revision.document_id != plan.document_id:
            raise ValueError(
                'plan SceneRevision belongs to a different document'
            )
        if revision.content_hash != plan.scene_content_hash:
            raise ValueError('plan SceneRevision content hash mismatch')
        if plan.system_variant_id is not None:
            repository = self.system_variant_repository
            if repository is None:
                raise ValueError(
                    'plan pins a SystemVariant but no variant repository '
                    'is bound'
                )
            variant = repository.get_variant(plan.system_variant_id)
            if variant is None:
                raise ValueError('plan references a missing SystemVariant')
            if variant.document_id != plan.document_id:
                raise ValueError(
                    'plan SystemVariant belongs to a different document'
                )
            if variant.variant_sha256 != plan.system_variant_sha256:
                raise ValueError('plan SystemVariant hash mismatch')
            if variant.baseline_revision_id != plan.scene_revision_id:
                raise ValueError(
                    'plan SystemVariant is not based on the pinned '
                    'SceneRevision'
                )
        profile = self.get_tolerance_profile(plan.tolerance_profile_id)
        if profile is None:
            raise ValueError('plan references unknown tolerance profile')
        if profile.document_id != plan.document_id:
            raise ValueError(
                'tolerance profile belongs to a different document'
            )
        if profile.profile_sha256 != plan.tolerance_profile_sha256:
            raise ValueError('plan tolerance profile hash mismatch')
        for check in plan.checks:
            self._resolve_check_subject(plan, check)

    def _resolve_check_subject(
        self,
        plan: CommissioningPlan,
        check: CommissioningCheck,
    ) -> None:
        """Resolve a check's subject against the plan's pinned design."""

        subject = check.subject
        if subject.scene_entity_id is not None:
            self.resolver.resolve_scene_entity(
                plan.scene_revision_id,
                subject.scene_entity_id,
                document_id=plan.document_id,
            )
        elif subject.channel_role_id is not None:
            variant = (
                self.system_variant_repository.get_variant(
                    plan.system_variant_id
                )
                if plan.system_variant_id is not None
                and self.system_variant_repository is not None
                else None
            )
            if variant is None:
                raise ValueError(
                    f'check {check.check_id} names a channel role but the '
                    'plan pins no SystemVariant to resolve it against'
                )
            role_ids = {
                binding.role_id for binding in variant.role_bindings
            }
            if subject.channel_role_id not in role_ids:
                raise ValueError(
                    f'check {check.check_id} channel role '
                    f'{subject.channel_role_id} is not bound in the '
                    'pinned SystemVariant'
                )
        else:
            assert subject.authority_ref is not None
            self.resolver.resolve(
                subject.authority_ref, document_id=plan.document_id
            )

    def save_plan(self, plan: CommissioningPlan) -> None:
        if self.get_plan(plan.plan_id) is not None:
            raise CommissioningConflictError(
                'CommissioningPlan ids are append-only'
            )
        self._resolve_plan_authority(plan)
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
        """Persist a run only when it replays exactly.

        The stored plan's profile is reloaded and the run is rebuilt from
        canonical plan + profile + the run's own observations and accepted
        deviations; the caller-supplied ``results`` must reproduce the
        replayed results field-for-field. Observation evidence refs are
        resolved against the same document — a manual observation simply
        carries no ``evidence_ref``.
        """

        if self.get_run(run.run_id) is not None:
            raise CommissioningConflictError(
                'CommissioningRun ids are append-only'
            )
        plan = self.get_plan(run.plan_id)
        if plan is None:
            raise ValueError('run references unknown commissioning plan')
        if plan.plan_sha256 != run.plan_sha256:
            raise ValueError('run plan hash differs from the stored plan')
        if run.document_id != plan.document_id:
            raise ValueError('run belongs to a different document')
        profile = self.get_tolerance_profile(plan.tolerance_profile_id)
        assert profile is not None  # plan save enforced this
        for observation in run.observations:
            if observation.evidence_ref is not None:
                self.resolver.resolve(
                    observation.evidence_ref,
                    document_id=run.document_id,
                )
        for deviation in run.accepted_deviations:
            if deviation.decision_ref is not None:
                # The acceptance authority must resolve to the exact pinned
                # design-decision record — a self-hashed or unrelated
                # authority can never stand in for it (#872).
                self.resolver.resolve(
                    deviation.decision_ref,
                    document_id=run.document_id,
                )
        # Replay also enforces the run-level input contract (#872): unique
        # observation ids, one observation per check, unique deviation ids,
        # one deviation per check, and deviation timing that can never
        # predate the evidence it accepts.
        replayed = build_commissioning_run(
            plan=plan,
            tolerance_profile=profile,
            observations=run.observations,
            accepted_deviations=run.accepted_deviations,
            created_at_utc=run.created_at_utc,
            run_id=run.run_id,
        )
        if replayed.semantic_payload() != run.semantic_payload():
            raise ValueError(
                'run results do not reproduce the canonical evaluation '
                'of the stored plan/profile/observations'
            )
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


__all__ = [
    'CadCommissioningRepository',
    'CommissioningConflictError',
]
