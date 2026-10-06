"""Append-only persistence for the authority dependency / staleness
graph (#729, REV59-DEPS).

Five tables:

* ``cad_dependency_edge_declarations`` — sealed typed dependency edges.
* ``cad_dependency_change_events`` — sealed semantic change events.
* ``cad_dependency_rule_profiles`` — sealed versioned invalidation
  rulesets.
* ``cad_staleness_assessments`` — sealed staleness verdicts.
* ``cad_revalidation_plans`` — sealed minimal revalidation work sets.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_authority_dependency import (
    DependencyEdgeDeclaration,
    DependencyRuleProfile,
    RevalidationPlan,
    SemanticChangeEvent,
    StalenessAssessment,
)


class DependencyGraphConflictError(ValueError):
    """A dependency-graph save violated append-only identity."""


class DependencyGraphIntegrityError(ValueError):
    """A stored dependency-graph row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise DependencyGraphIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise DependencyGraphIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadAuthorityDependencyRepository:
    """Native storage for the #729 dependency/staleness records."""

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
                'cad_dependency_edge_declarations',
                'cad_dependency_change_events',
                'cad_dependency_rule_profiles',
                'cad_staleness_assessments',
                'cad_revalidation_plans',
            )

    # ------------------------------------------------------------------
    # Dependency edges

    def save_edge(self, edge: DependencyEdgeDeclaration) -> None:
        _assert_sealed(edge, 'edge_sha256', 'edge_id')
        existing = self.get_edge(edge.edge_id)
        if existing is not None:
            if existing.edge_sha256 == edge.edge_sha256:
                return
            raise DependencyGraphConflictError(
                'dependency edges are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_dependency_edge_declarations (
                    edge_id, edge_sha256, document_id, subject_ref_id,
                    kind, target_ref_id, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    edge.edge_id,
                    edge.edge_sha256,
                    edge.document_id,
                    edge.subject_ref.ref_id,
                    edge.kind,
                    edge.target_ref.ref_id,
                    edge.declared_at_utc,
                    edge.model_dump_json(),
                ),
            )

    def get_edge(
        self, edge_id: str
    ) -> DependencyEdgeDeclaration | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_dependency_edge_declarations '
                'WHERE edge_id=?',
                (edge_id,),
            ).fetchone()
        if row is None:
            return None
        edge = DependencyEdgeDeclaration.model_validate_json(
            row['payload_json']
        )
        if (
            edge.edge_id != row['edge_id']
            or edge.edge_sha256 != row['edge_sha256']
            or edge.document_id != row['document_id']
            or edge.subject_ref.ref_id != row['subject_ref_id']
            or edge.kind != row['kind']
            or edge.target_ref.ref_id != row['target_ref_id']
            or edge.declared_at_utc != row['declared_at_utc']
        ):
            raise DependencyGraphIntegrityError(
                'dependency edge row disagrees with payload'
            )
        return edge

    def list_edges(
        self, document_id: str
    ) -> tuple[DependencyEdgeDeclaration, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_dependency_edge_declarations '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            DependencyEdgeDeclaration.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Change events

    def save_change_event(self, event: SemanticChangeEvent) -> None:
        _assert_sealed(event, 'event_sha256', 'event_id')
        existing = self.get_change_event(event.event_id)
        if existing is not None:
            if existing.event_sha256 == event.event_sha256:
                return
            raise DependencyGraphConflictError(
                'semantic change events are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_dependency_change_events (
                    event_id, event_sha256, document_id,
                    changed_ref_id, change_class, occurred_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.event_sha256,
                    event.document_id,
                    event.changed_ref.ref_id,
                    event.change_class,
                    event.occurred_at_utc,
                    event.model_dump_json(),
                ),
            )

    def get_change_event(
        self, event_id: str
    ) -> SemanticChangeEvent | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_dependency_change_events '
                'WHERE event_id=?',
                (event_id,),
            ).fetchone()
        if row is None:
            return None
        event = SemanticChangeEvent.model_validate_json(
            row['payload_json']
        )
        if (
            event.event_id != row['event_id']
            or event.event_sha256 != row['event_sha256']
            or event.document_id != row['document_id']
            or event.changed_ref.ref_id != row['changed_ref_id']
            or event.change_class != row['change_class']
            or event.occurred_at_utc != row['occurred_at_utc']
        ):
            raise DependencyGraphIntegrityError(
                'semantic change event row disagrees with payload'
            )
        return event

    def list_change_events(
        self, document_id: str
    ) -> tuple[SemanticChangeEvent, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_dependency_change_events '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            SemanticChangeEvent.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Rule profiles

    def save_rule_profile(self, profile: DependencyRuleProfile) -> None:
        _assert_sealed(profile, 'profile_sha256', 'profile_id')
        existing = self.get_rule_profile(profile.profile_id)
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise DependencyGraphConflictError(
                'dependency rule profiles are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_dependency_rule_profiles (
                    profile_id, profile_sha256, document_id,
                    ruleset_version, entry_count, declared_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.profile_sha256,
                    profile.document_id,
                    profile.ruleset_version,
                    len(profile.entries),
                    profile.declared_at_utc,
                    profile.model_dump_json(),
                ),
            )

    def get_rule_profile(
        self, profile_id: str
    ) -> DependencyRuleProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_dependency_rule_profiles '
                'WHERE profile_id=?',
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        profile = DependencyRuleProfile.model_validate_json(
            row['payload_json']
        )
        if (
            profile.profile_id != row['profile_id']
            or profile.profile_sha256 != row['profile_sha256']
            or profile.document_id != row['document_id']
            or profile.ruleset_version != row['ruleset_version']
            or len(profile.entries) != row['entry_count']
            or profile.declared_at_utc != row['declared_at_utc']
        ):
            raise DependencyGraphIntegrityError(
                'dependency rule profile row disagrees with payload'
            )
        return profile

    def list_rule_profiles(
        self, document_id: str
    ) -> tuple[DependencyRuleProfile, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_dependency_rule_profiles '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            DependencyRuleProfile.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Staleness assessments

    def save_assessment(self, assessment: StalenessAssessment) -> None:
        _assert_sealed(assessment, 'assessment_sha256', 'assessment_id')
        existing = self.get_assessment(assessment.assessment_id)
        if existing is not None:
            if existing.assessment_sha256 == assessment.assessment_sha256:
                return
            raise DependencyGraphConflictError(
                'staleness assessments are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_staleness_assessments (
                    assessment_id, assessment_sha256, document_id,
                    change_event_ref_id, entry_count, evaluated_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assessment.assessment_id,
                    assessment.assessment_sha256,
                    assessment.document_id,
                    assessment.change_event_ref.ref_id,
                    len(assessment.entries),
                    assessment.evaluated_at_utc,
                    assessment.model_dump_json(),
                ),
            )

    def get_assessment(
        self, assessment_id: str
    ) -> StalenessAssessment | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_staleness_assessments '
                'WHERE assessment_id=?',
                (assessment_id,),
            ).fetchone()
        if row is None:
            return None
        assessment = StalenessAssessment.model_validate_json(
            row['payload_json']
        )
        if (
            assessment.assessment_id != row['assessment_id']
            or assessment.assessment_sha256 != row['assessment_sha256']
            or assessment.document_id != row['document_id']
            or assessment.change_event_ref.ref_id
            != row['change_event_ref_id']
            or len(assessment.entries) != row['entry_count']
            or assessment.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise DependencyGraphIntegrityError(
                'staleness assessment row disagrees with payload'
            )
        return assessment

    def list_assessments(
        self, document_id: str
    ) -> tuple[StalenessAssessment, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_staleness_assessments '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            StalenessAssessment.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Revalidation plans

    def save_plan(self, plan: RevalidationPlan) -> None:
        _assert_sealed(plan, 'plan_sha256', 'plan_id')
        existing = self.get_plan(plan.plan_id)
        if existing is not None:
            if existing.plan_sha256 == plan.plan_sha256:
                return
            raise DependencyGraphConflictError(
                'revalidation plans are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_revalidation_plans (
                    plan_id, plan_sha256, document_id,
                    assessment_ref_id, action_count, planned_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.plan_id,
                    plan.plan_sha256,
                    plan.document_id,
                    plan.assessment_ref.ref_id,
                    len(plan.actions),
                    plan.planned_at_utc,
                    plan.model_dump_json(),
                ),
            )

    def get_plan(self, plan_id: str) -> RevalidationPlan | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_revalidation_plans WHERE plan_id=?',
                (plan_id,),
            ).fetchone()
        if row is None:
            return None
        plan = RevalidationPlan.model_validate_json(row['payload_json'])
        if (
            plan.plan_id != row['plan_id']
            or plan.plan_sha256 != row['plan_sha256']
            or plan.document_id != row['document_id']
            or plan.assessment_ref.ref_id != row['assessment_ref_id']
            or len(plan.actions) != row['action_count']
            or plan.planned_at_utc != row['planned_at_utc']
        ):
            raise DependencyGraphIntegrityError(
                'revalidation plan row disagrees with payload'
            )
        return plan

    def list_plans(
        self, document_id: str
    ) -> tuple[RevalidationPlan, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_revalidation_plans '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            RevalidationPlan.model_validate_json(r['payload_json'])
            for r in rows
        )


__all__ = [
    'CadAuthorityDependencyRepository',
    'DependencyGraphConflictError',
    'DependencyGraphIntegrityError',
]
