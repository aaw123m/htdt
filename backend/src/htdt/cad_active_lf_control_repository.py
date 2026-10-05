"""Append-only persistence for the active-LF control plan authority (#533).

Two authorities live here, mirroring the bass-management repository:

* ``cad_active_lf_control_plans`` — immutable sealed
  :class:`ActiveLowFrequencyControlPlan` payloads, keyed by
  ``(document_id, plan_id, plan_sha256)``. A plan's lifecycle advances by
  recording a *new sealed plan* — never by mutating a row.
* ``cad_active_lf_control_events`` — an append-only lifecycle journal
  binding design → exported → applied → read-back → verified
  transitions. Each event pins both endpoint plan hashes plus the
  evidence reference the transition claims, so a plan can never claim an
  applied/read-back state that was never observed.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_active_lf_control import (
    ActiveLowFrequencyControlPlan,
    ControlPlanLifecycle,
)
from .cad_repository import SceneRepository
from .cad_schema import require_native_tables, connect_sqlite
from .clock import utc_now_iso as _utc_now


class ActiveLfConflictError(ValueError):
    """A control-plan save violated append-only identity rules."""


class ActiveLfIntegrityError(ValueError):
    """A stored row disagreed with its payload or resolution contract."""


class LfLifecycleTransitionError(ValueError):
    """An illegal lifecycle transition was requested."""


LfLifecycleEventKind = Literal[
    'export',
    'apply',
    'read_back',
    'verify',
    'reject',
    'note',
]

#: Allowed (from_lifecycle, to_lifecycle, event_kind) triples. A plan's
#: lifecycle only advances through observed evidence; there is no direct
#: proposed→verified edge.
_ALLOWED_TRANSITIONS: tuple[tuple[str, str, str], ...] = (
    ('proposed', 'exported', 'export'),
    ('exported', 'applied', 'apply'),
    ('applied', 'read_back', 'read_back'),
    ('read_back', 'verified', 'verify'),
    ('proposed', 'rejected', 'reject'),
    ('exported', 'rejected', 'reject'),
    ('applied', 'rejected', 'reject'),
)

#: Transitions that must carry an evidence reference — "applied" without
#: observed device acknowledgement is not applied.
_EVIDENCE_REQUIRED_TARGETS = ('applied', 'read_back', 'verified')


class LfControlLifecycleEvent(BaseModel):
    """One recorded lifecycle transition between two persisted plans."""

    model_config = ConfigDict(frozen=True)

    document_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    from_plan_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    to_plan_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    from_lifecycle: ControlPlanLifecycle
    to_lifecycle: ControlPlanLifecycle
    event_kind: LfLifecycleEventKind
    evidence_ref: str | None = None
    actor: str | None = None
    note: str | None = None
    recorded_at_utc: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid_event(self) -> 'LfControlLifecycleEvent':
        if self.event_kind != 'note' and self.from_plan_sha256 == self.to_plan_sha256:
            raise ValueError(
                'a lifecycle transition must move between distinct plan payloads'
            )
        return self


def transition_plan(
    plan: ActiveLowFrequencyControlPlan,
    *,
    lifecycle: ControlPlanLifecycle,
) -> ActiveLowFrequencyControlPlan:
    """Reseal ``plan`` at a new lifecycle state.

    The plan's identity (scene/variant/groups/band/matrix) is unchanged —
    only the lifecycle field differs, producing a distinct sealed payload
    that the journal can bind to its predecessor.
    """
    from .cad_active_lf_control import build_control_plan

    payload = plan.model_dump(mode='python', exclude={'plan_sha256'})
    payload['lifecycle'] = lifecycle
    return build_control_plan(**payload)


class CadActiveLfControlRepository:
    """Native storage for active-LF control plans and lifecycle events."""

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
                'cad_active_lf_control_plans',
                'cad_active_lf_control_events',
            )

    # ------------------------------------------------------------------
    # Plans (append-only)

    def save_plan(
        self,
        plan: ActiveLowFrequencyControlPlan,
        document_id: str,
    ) -> None:
        if plan.document_id != document_id:
            raise ActiveLfIntegrityError(
                'plan.document_id disagrees with the repository document'
            )
        existing = self.get_plan(document_id, plan.plan_id, plan.plan_sha256)
        if existing is not None:
            return  # identical sealed payload — no-op
        conflict = self._conflicting_row(document_id, plan.plan_id, plan.plan_sha256)
        if conflict is not None:
            raise ActiveLfConflictError(
                'control plan (document_id, plan_id, plan_sha256) is append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_active_lf_control_plans (
                    document_id, plan_id, plan_sha256, representation,
                    lifecycle, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    plan.plan_id,
                    plan.plan_sha256,
                    plan.representation,
                    plan.lifecycle,
                    _utc_now(),
                    plan.model_dump_json(),
                ),
            )

    def _conflicting_row(
        self, document_id: str, plan_id: str, plan_sha256: str
    ) -> sqlite3.Row | None:
        with closing(self._connect()) as connection:
            return connection.execute(
                """
                SELECT plan_sha256 FROM cad_active_lf_control_plans
                WHERE document_id=? AND plan_id=? AND plan_sha256=?
                """,
                (document_id, plan_id, plan_sha256),
            ).fetchone()

    def get_plan(
        self,
        document_id: str,
        plan_id: str,
        plan_sha256: str,
    ) -> ActiveLowFrequencyControlPlan | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT plan_id, plan_sha256, representation, lifecycle, payload_json
                FROM cad_active_lf_control_plans
                WHERE document_id=? AND plan_id=? AND plan_sha256=?
                """,
                (document_id, plan_id, plan_sha256),
            ).fetchone()
        if row is None:
            return None
        return self._plan_from_row(row)

    def get_plan_by_hash(
        self, plan_sha256: str
    ) -> ActiveLowFrequencyControlPlan | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT plan_id, plan_sha256, representation, lifecycle, payload_json
                FROM cad_active_lf_control_plans
                WHERE plan_sha256=?
                """,
                (plan_sha256,),
            ).fetchone()
        if row is None:
            return None
        return self._plan_from_row(row)

    def plans_for(
        self,
        document_id: str,
        plan_id: str,
    ) -> tuple[ActiveLowFrequencyControlPlan, ...]:
        """Every recorded state of one plan id, oldest first."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT plan_id, plan_sha256, representation, lifecycle, payload_json
                FROM cad_active_lf_control_plans
                WHERE document_id=? AND plan_id=?
                ORDER BY seq
                """,
                (document_id, plan_id),
            ).fetchall()
        return tuple(self._plan_from_row(row) for row in rows)

    def current_plan(
        self,
        document_id: str,
        plan_id: str,
    ) -> ActiveLowFrequencyControlPlan | None:
        """The newest recorded state of one plan id."""
        plans = self.plans_for(document_id, plan_id)
        return plans[-1] if plans else None

    def list_plans(
        self, document_id: str
    ) -> tuple[ActiveLowFrequencyControlPlan, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT plan_id, plan_sha256, representation, lifecycle, payload_json
                FROM cad_active_lf_control_plans
                WHERE document_id=?
                ORDER BY seq
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._plan_from_row(row) for row in rows)

    def _plan_from_row(self, row: sqlite3.Row) -> ActiveLowFrequencyControlPlan:
        plan = ActiveLowFrequencyControlPlan.model_validate_json(row['payload_json'])
        if (
            plan.plan_id != row['plan_id']
            or plan.plan_sha256 != row['plan_sha256']
            or plan.representation != row['representation']
            or plan.lifecycle != row['lifecycle']
        ):
            raise ActiveLfIntegrityError(
                'control plan row disagrees with its payload'
            )
        return plan

    # ------------------------------------------------------------------
    # Lifecycle events (append-only journal)

    def record_transition(
        self,
        document_id: str,
        *,
        from_plan: ActiveLowFrequencyControlPlan,
        to_plan: ActiveLowFrequencyControlPlan,
        event_kind: LfLifecycleEventKind,
        evidence_ref: str | None = None,
        actor: str | None = None,
        note: str | None = None,
        recorded_at_utc: str | None = None,
    ) -> LfControlLifecycleEvent:
        """Record one lifecycle transition between two persisted plans.

        Fails closed: both plans must be persisted under this document and
        share ``plan_id``; the edge must be a legal lifecycle step; and
        applied/read-back/verified targets require an evidence reference —
        a plan cannot claim a deployed state it never observed.
        """
        if from_plan.document_id != document_id or to_plan.document_id != document_id:
            raise ActiveLfIntegrityError(
                'transition plans must belong to the repository document'
            )
        if from_plan.plan_id != to_plan.plan_id:
            raise LfLifecycleTransitionError(
                'a lifecycle transition must stay within one plan id'
            )
        if event_kind != 'note':
            if (
                from_plan.lifecycle,
                to_plan.lifecycle,
                event_kind,
            ) not in _ALLOWED_TRANSITIONS:
                raise LfLifecycleTransitionError(
                    'illegal lifecycle transition '
                    f'{from_plan.lifecycle}->{to_plan.lifecycle} ({event_kind})'
                )
            if (
                to_plan.lifecycle in _EVIDENCE_REQUIRED_TARGETS
                and not evidence_ref
            ):
                raise LfLifecycleTransitionError(
                    f'{to_plan.lifecycle} requires an evidence reference '
                    '(observed device state / verification record)'
                )
        elif note is None:
            raise LfLifecycleTransitionError(
                'a note event must carry a note'
            )
        for plan in (from_plan, to_plan):
            persisted = self.get_plan(document_id, plan.plan_id, plan.plan_sha256)
            if persisted is None:
                raise ActiveLfIntegrityError(
                    'transition endpoints must be persisted plans'
                )
        event = LfControlLifecycleEvent(
            document_id=document_id,
            plan_id=from_plan.plan_id,
            from_plan_sha256=from_plan.plan_sha256,
            to_plan_sha256=to_plan.plan_sha256,
            from_lifecycle=from_plan.lifecycle,
            to_lifecycle=to_plan.lifecycle,
            event_kind=event_kind,
            evidence_ref=evidence_ref,
            actor=actor,
            note=note,
            recorded_at_utc=recorded_at_utc or _utc_now(),
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_active_lf_control_events (
                    document_id, plan_id, from_plan_sha256, to_plan_sha256,
                    from_lifecycle, to_lifecycle, event_kind, evidence_ref,
                    actor, recorded_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.document_id,
                    event.plan_id,
                    event.from_plan_sha256,
                    event.to_plan_sha256,
                    event.from_lifecycle,
                    event.to_lifecycle,
                    event.event_kind,
                    event.evidence_ref,
                    event.actor,
                    event.recorded_at_utc,
                    event.model_dump_json(),
                ),
            )
        return event

    def lifecycle_events(
        self,
        document_id: str,
        plan_id: str,
    ) -> tuple[LfControlLifecycleEvent, ...]:
        """The recorded transition journal for one plan, oldest first."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT document_id, plan_id, from_plan_sha256, to_plan_sha256,
                       from_lifecycle, to_lifecycle, event_kind, evidence_ref,
                       actor, recorded_at_utc, payload_json
                FROM cad_active_lf_control_events
                WHERE document_id=? AND plan_id=?
                ORDER BY seq
                """,
                (document_id, plan_id),
            ).fetchall()
        return tuple(self._event_from_row(row) for row in rows)

    def _event_from_row(self, row: sqlite3.Row) -> LfControlLifecycleEvent:
        event = LfControlLifecycleEvent.model_validate_json(row['payload_json'])
        if (
            event.document_id != row['document_id']
            or event.plan_id != row['plan_id']
            or event.from_plan_sha256 != row['from_plan_sha256']
            or event.to_plan_sha256 != row['to_plan_sha256']
            or event.from_lifecycle != row['from_lifecycle']
            or event.to_lifecycle != row['to_lifecycle']
            or event.event_kind != row['event_kind']
            or event.recorded_at_utc != row['recorded_at_utc']
        ):
            raise ActiveLfIntegrityError(
                'control lifecycle event row disagrees with its payload'
            )
        return event
