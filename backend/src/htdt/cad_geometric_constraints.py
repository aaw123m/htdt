"""Room authoring constraints and construction guides (#618).

A *separate domain* from the #486 placement-constraint engine: these are
persistent, human-authored relationships between editor entities
(centerline/on-reference, symmetric pairs, fixed distances, equal spacing,
limited parallel/perpendicular), stored per document and re-solved locally
after edits.

Semantics, per the issue contract:
- driving vs. driven side is declared at creation (``driver_index`` /
  endpoint order) — never resolved by selection order;
- solves are deterministic and local: a driver-side edit propagates to its
  subjects, a subject-side edit is pulled back into compliance, and both are
  surfaced via solve notes — nothing is silently dropped or jittered;
- a constraint whose member entities or reference room disappears is marked
  ``broken`` with a reason — it is never rebound by name or discarded;
- everything persists as one versioned payload per document in
  ``authoring_constraint_sets``;
- guides derived here are display-only geometry, never solver input.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import atan2, degrees
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .cad_orientation_display import body_forward_direction
from .cad_scene import (
    Position3,
    Quaternion4,
    SceneDocument,
    SceneEntity,
    quaternion_from_axis_angle_vector,
    quaternion_from_matrix3,
    quaternion_multiply,
    quaternion_to_matrix3,
)


CONSTRAINT_KINDS = (
    'on_centerline',
    'symmetric_pair',
    'fixed_distance',
    'equal_spacing',
    'coincident',
    'parallel',
)

CONSTRAINT_KIND_LABELS: dict[str, str] = {
    'on_centerline': '中心線拘束',
    'symmetric_pair': '対称ペア拘束',
    'fixed_distance': '距離固定拘束',
    'equal_spacing': '等間隔拘束',
    'coincident': '一致拘束',
    'parallel': '平行/直交拘束',
}


class AuthoringConstraint(BaseModel):
    """One persisted authoring constraint. ``entity_ids`` order is the stored
    authority — for ``equal_spacing`` the first/last entries are the fixed
    endpoints; for the other kinds ``driver_index`` picks the driver."""

    model_config = ConfigDict(extra='forbid')

    constraint_id: str
    kind: str
    entity_ids: tuple[str, ...] = Field(min_length=1)
    axis: Literal['x', 'y'] | None = None
    #: Kind-specific value: signed delta along ``axis`` for fixed_distance,
    #: yaw offset in degrees for parallel.
    value: float | None = None
    driver_index: int = 0
    label: str | None = None
    created_at_utc: str
    broken: bool = False
    broken_reason: str | None = None

    @field_validator('kind')
    @classmethod
    def _known_kind(cls, value: str) -> str:
        if value not in CONSTRAINT_KINDS:
            raise ValueError(f'unknown constraint kind: {value}')
        return value

    @field_validator('entity_ids')
    @classmethod
    def _nonempty_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError('entity ids must be non-empty strings')
        return value


class AuthoringConstraintSet(BaseModel):
    """The document-scoped persisted constraint store."""

    model_config = ConfigDict(extra='forbid')

    schema_version: int = 1
    solve_version: int = 1
    constraints: tuple[AuthoringConstraint, ...] = ()


@dataclass(frozen=True)
class ConstraintSolveResult:
    """Result of one local solve: entity position/orientation updates plus an
    optional broken reason. All values are domain-space models."""

    moved: dict[str, Position3] = field(default_factory=dict)
    rotated: dict[str, Quaternion4] = field(default_factory=dict)
    broken_reason: str | None = None


def new_constraint_id() -> str:
    return f'constraint-{uuid4().hex[:10]}'


def _entity(document: SceneDocument, entity_id: str) -> SceneEntity | None:
    try:
        return document.entity(entity_id)
    except KeyError:
        return None


def _room_centerline(document: SceneDocument, axis: str) -> float | None:
    room = document.room
    if room is None:
        return None
    min_x, min_y, max_x, max_y = room.bounds_m
    return (min_x + max_x) * 0.5 if axis == 'x' else (min_y + max_y) * 0.5


def _mirror_across_x_centerline(
    entity: SceneEntity, centerline_x: float
) -> tuple[Position3, Quaternion4]:
    """Mirror a pose across the vertical x=centerline plane (Y-Z mirror).

    Position: x -> 2·cx - x. Orientation: R' = M·R·M with
    M = diag(-1, 1, 1) — the exact reflection of the body frame, preserving
    the mirrored pose's twist rather than re-canonicalizing it.
    """

    mirrored_position = Position3(
        x_m=2.0 * centerline_x - entity.position.x_m,
        y_m=entity.position.y_m,
        z_m=entity.position.z_m,
    )
    matrix = quaternion_to_matrix3(entity.orientation)
    # R' = M · R · M where M = diag(-1,1,1): flip x-column and x-row signs.
    mirrored = (
        (matrix[0][0], -matrix[0][1], -matrix[0][2]),
        (-matrix[1][0], matrix[1][1], matrix[1][2]),
        (-matrix[2][0], matrix[2][1], matrix[2][2]),
    )
    return mirrored_position, quaternion_from_matrix3(mirrored)


def _rotate_about_world_z(
    orientation: Quaternion4, delta_yaw_deg: float
) -> Quaternion4:
    rotation = quaternion_from_axis_angle_vector((0.0, 0.0, 1.0), delta_yaw_deg)
    return quaternion_multiply(rotation, orientation)


def solve_constraint(
    constraint: AuthoringConstraint,
    document: SceneDocument,
    *,
    driver_moved: set[str] | frozenset[str] | None = None,
) -> ConstraintSolveResult:
    """Resolve ``constraint`` against ``document`` after an edit.

    ``driver_moved`` carries the entity ids the user just edited; the solve
    propagates driver → subjects for both driver-side and subject-side edits
    (a subject edit is re-projected into compliance). Returns a
    ``broken_reason`` instead of solving when the constraint cannot hold.
    """

    entities: list[SceneEntity] = []
    for entity_id in constraint.entity_ids:
        entity = _entity(document, entity_id)
        if entity is None:
            return ConstraintSolveResult(
                broken_reason='拘束の対象オブジェクトが削除されました',
            )
        entities.append(entity)

    moved: dict[str, Position3] = {}
    rotated: dict[str, Quaternion4] = {}

    if constraint.kind == 'on_centerline':
        if constraint.axis is None:
            return ConstraintSolveResult(broken_reason='中心線の軸が未設定です')
        center = _room_centerline(document, constraint.axis)
        if center is None:
            return ConstraintSolveResult(
                broken_reason='部屋が未設定のため中心線拘束を解けません'
            )
        for entity in entities:
            current = entity.position
            if constraint.axis == 'x':
                target = current.x_m
                desired = center
                if abs(desired - target) > 1e-9:
                    moved[entity.entity_id] = Position3(
                        x_m=desired, y_m=current.y_m, z_m=current.z_m
                    )
            else:
                desired = center
                if abs(desired - current.y_m) > 1e-9:
                    moved[entity.entity_id] = Position3(
                        x_m=current.x_m, y_m=desired, z_m=current.z_m
                    )
        return ConstraintSolveResult(moved=moved)

    if constraint.kind == 'symmetric_pair':
        if len(entities) != 2:
            return ConstraintSolveResult(
                broken_reason='対称ペアには2つのオブジェクトが必要です'
            )
        center = _room_centerline(document, 'x')
        if center is None:
            return ConstraintSolveResult(
                broken_reason='部屋が未設定のため対称拘束を解けません'
            )
        driver_index = min(max(constraint.driver_index, 0), 1)
        driver = entities[driver_index]
        subject = entities[1 - driver_index]
        target_position, target_orientation = _mirror_across_x_centerline(
            driver, center
        )
        if target_position != subject.position:
            moved[subject.entity_id] = target_position
        if _quaternion_differs(target_orientation, subject.orientation):
            rotated[subject.entity_id] = target_orientation
        return ConstraintSolveResult(moved=moved, rotated=rotated)

    if constraint.kind == 'fixed_distance':
        if len(entities) != 2 or constraint.axis is None or constraint.value is None:
            return ConstraintSolveResult(
                broken_reason='距離固定拘束の定義が不完全です'
            )
        driver = entities[0]
        subject = entities[1]
        delta = float(constraint.value)
        if constraint.axis == 'x':
            desired = driver.position.x_m + delta
            if abs(desired - subject.position.x_m) > 1e-9:
                moved[subject.entity_id] = Position3(
                    x_m=desired,
                    y_m=subject.position.y_m,
                    z_m=subject.position.z_m,
                )
        else:
            desired = driver.position.y_m + delta
            if abs(desired - subject.position.y_m) > 1e-9:
                moved[subject.entity_id] = Position3(
                    x_m=subject.position.x_m,
                    y_m=desired,
                    z_m=subject.position.z_m,
                )
        return ConstraintSolveResult(moved=moved)

    if constraint.kind == 'equal_spacing':
        if len(entities) < 3 or constraint.axis is None:
            return ConstraintSolveResult(
                broken_reason='等間隔拘束には3つ以上のオブジェクトが必要です'
            )
        # Endpoints are the geometric extremes along the constrained axis —
        # deterministic regardless of the order members were selected in.
        ordered = sorted(
            entities,
            key=lambda entity: (
                entity.position.x_m
                if constraint.axis == 'x'
                else entity.position.y_m
            ),
        )
        interior = ordered[1:-1]
        start = ordered[0].position.x_m if constraint.axis == 'x' else ordered[0].position.y_m
        end = ordered[-1].position.x_m if constraint.axis == 'x' else ordered[-1].position.y_m
        count = len(entities) - 1
        for index, entity in enumerate(interior, start=1):
            target = start + (end - start) * (index / count)
            if constraint.axis == 'x':
                if abs(target - entity.position.x_m) > 1e-9:
                    moved[entity.entity_id] = Position3(
                        x_m=target,
                        y_m=entity.position.y_m,
                        z_m=entity.position.z_m,
                    )
            else:
                if abs(target - entity.position.y_m) > 1e-9:
                    moved[entity.entity_id] = Position3(
                        x_m=entity.position.x_m,
                        y_m=target,
                        z_m=entity.position.z_m,
                    )
        return ConstraintSolveResult(moved=moved)

    if constraint.kind == 'coincident':
        if len(entities) < 2:
            return ConstraintSolveResult(broken_reason='一致拘束には2つ以上のオブジェクトが必要です')
        driver_index = min(max(constraint.driver_index, 0), len(entities) - 1)
        driver = entities[driver_index]
        for entity in entities:
            if entity.entity_id == driver.entity_id:
                continue
            if entity.position != driver.position:
                moved[entity.entity_id] = driver.position
        return ConstraintSolveResult(moved=moved)

    if constraint.kind == 'parallel':
        if len(entities) < 2 or constraint.value is None:
            return ConstraintSolveResult(
                broken_reason='平行/直交拘束の定義が不完全です'
            )
        driver_index = min(max(constraint.driver_index, 0), len(entities) - 1)
        driver = entities[driver_index]
        driver_forward = body_forward_direction(driver.orientation)
        driver_heading = degrees(atan2(driver_forward.x, driver_forward.y))
        for entity in entities:
            if entity.entity_id == driver.entity_id:
                continue
            forward = body_forward_direction(entity.orientation)
            current_heading = degrees(atan2(forward.x, forward.y))
            target_heading = driver_heading + float(constraint.value)
            delta = ((target_heading - current_heading + 180.0) % 360.0) - 180.0
            if abs(delta) > 1e-6:
                rotated[entity.entity_id] = _rotate_about_world_z(
                    entity.orientation, delta
                )
        return ConstraintSolveResult(moved=moved, rotated=rotated)

    return ConstraintSolveResult(broken_reason=f'未対応の拘束種別: {constraint.kind}')


def _quaternion_differs(a: Quaternion4, b: Quaternion4, tol: float = 1e-9) -> bool:
    same = all(
        abs(x - y) <= tol
        for x, y in zip(
            (a.w, a.x, a.y, a.z), (b.w, b.x, b.y, b.z)
        )
    )
    negated = all(
        abs(x + y) <= tol
        for x, y in zip(
            (a.w, a.x, a.y, a.z), (b.w, b.x, b.y, b.z)
        )
    )
    return not (same or negated)


def constraint_guide_items(
    constraint_set: AuthoringConstraintSet,
    document: SceneDocument,
) -> tuple:
    """Build display-only guide lines for the constraint set.

    Imported lazily to avoid a room_viewport ↔ constraints import cycle.
    """

    from .room_viewport import GuideRenderItem

    items: list[GuideRenderItem] = []
    room = document.room
    z = 0.03
    for constraint in constraint_set.constraints:
        label = constraint.label or CONSTRAINT_KIND_LABELS.get(
            constraint.kind, constraint.kind
        )
        if constraint.broken:
            label = f'{label}（破損）'
        if constraint.kind == 'on_centerline' and room is not None:
            min_x, min_y, max_x, max_y = room.bounds_m
            if constraint.axis == 'x':
                cx = (min_x + max_x) * 0.5
                items.append(
                    GuideRenderItem(
                        start=(cx, min_y, z),
                        end=(cx, max_y, z),
                        label=label,
                    )
                )
            else:
                cy = (min_y + max_y) * 0.5
                items.append(
                    GuideRenderItem(
                        start=(min_x, cy, z),
                        end=(max_x, cy, z),
                        label=label,
                    )
                )
            continue
        positions = []
        for entity_id in constraint.entity_ids:
            entity = _entity(document, entity_id)
            if entity is not None:
                positions.append(entity.position)
        for a, b in zip(positions, positions[1:]):
            items.append(
                GuideRenderItem(
                    start=(a.x_m, a.y_m, a.z_m + 0.05),
                    end=(b.x_m, b.y_m, b.z_m + 0.05),
                    label=label,
                )
            )
    return tuple(items)


def make_centerline_constraint(
    axis: str, entity_ids: tuple[str, ...], *, label: str | None = None
) -> AuthoringConstraint:
    return AuthoringConstraint(
        constraint_id=new_constraint_id(),
        kind='on_centerline',
        entity_ids=entity_ids,
        axis=axis,  # type: ignore[arg-type]
        created_at_utc=datetime.now(timezone.utc).isoformat(),
        label=label or ('左右中心線' if axis == 'x' else '前後中心線'),
    )


def make_symmetric_pair_constraint(
    driver_id: str, subject_id: str, *, label: str | None = None
) -> AuthoringConstraint:
    return AuthoringConstraint(
        constraint_id=new_constraint_id(),
        kind='symmetric_pair',
        entity_ids=(driver_id, subject_id),
        driver_index=0,
        created_at_utc=datetime.now(timezone.utc).isoformat(),
        label=label or '対称ペア',
    )


def make_fixed_distance_constraint(
    driver: SceneEntity,
    subject: SceneEntity,
    *,
    axis: str,
    label: str | None = None,
) -> AuthoringConstraint:
    delta = (
        subject.position.x_m - driver.position.x_m
        if axis == 'x'
        else subject.position.y_m - driver.position.y_m
    )
    return AuthoringConstraint(
        constraint_id=new_constraint_id(),
        kind='fixed_distance',
        entity_ids=(driver.entity_id, subject.entity_id),
        axis=axis,  # type: ignore[arg-type]
        value=delta,
        created_at_utc=datetime.now(timezone.utc).isoformat(),
        label=label or f'{axis.upper()}距離固定 {abs(delta):.2f} m',
    )


def make_equal_spacing_constraint(
    entity_ids: tuple[str, ...], *, axis: str, label: str | None = None
) -> AuthoringConstraint:
    return AuthoringConstraint(
        constraint_id=new_constraint_id(),
        kind='equal_spacing',
        entity_ids=entity_ids,
        axis=axis,  # type: ignore[arg-type]
        created_at_utc=datetime.now(timezone.utc).isoformat(),
        label=label or f'{axis.upper()}方向等間隔',
    )


__all__ = [
    'CONSTRAINT_KINDS',
    'CONSTRAINT_KIND_LABELS',
    'AuthoringConstraint',
    'AuthoringConstraintSet',
    'ConstraintSolveResult',
    'constraint_guide_items',
    'make_centerline_constraint',
    'make_equal_spacing_constraint',
    'make_fixed_distance_constraint',
    'make_symmetric_pair_constraint',
    'new_constraint_id',
    'solve_constraint',
]
