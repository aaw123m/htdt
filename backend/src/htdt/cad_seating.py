"""Seating layout authoring (#546): generated seat rows with spacing, aisles,
stagger and optional riser-linked seating height.

A ``SeatingLayoutSpec`` is a versioned, persisted authoring record (stored in
the ``seating_layout_specs`` payload table) that deterministically generates
``seat`` entities. Generated seats keep the same listener/eye/head semantics
as hand-authored seats (``acoustic_reference_offset_m`` ear-height offset,
screen-facing orientation) and get stable, spec-scoped ids — so a
regeneration can report each seat as added / moved / removed while
hand-authored seats stay untouched.

Contract details honored here:
- riser binding is an explicit reference: a missing riser blocks generation
  for that row with a surfaced error — a riser is never silently created;
- regeneration is a diff, not a rewrite: seats whose slots vanished are
  reported as ``removed`` candidates and only actually deleted when the
  caller opts in (``remove_orphaned=True``) — nothing is silently dropped;
- everything lands through one ``EntitySetEditCommand`` → one Undo step.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import cos, radians, sin
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .cad_document import CommandPresentation, WorkingDocument
from .cad_scene import (
    Offset3,
    Position3,
    SceneDocument,
    SceneEntity,
    Size3,
    quaternion_from_euler_deg,
)


SEAT_SPEC_SCHEMA_VERSION = 1
DEFAULT_SEAT_SIZE = Size3(x_m=0.70, y_m=0.80, z_m=0.90)
DEFAULT_SEAT_REFERENCE = Offset3(z_m=0.65)


class SeatingLayoutError(ValueError):
    """A rejected seating-layout request (user-facing reason)."""


class AisleSpec(BaseModel):
    """One explicit aisle: a gap of ``width_m`` after seat index
    ``after_index`` (0-based; the aisle sits between seats
    ``after_index`` and ``after_index + 1`` of every row)."""

    model_config = ConfigDict(extra='forbid')

    after_index: int = Field(ge=0)
    width_m: float = Field(gt=0.0, le=3.0)


class SeatRowSpec(BaseModel):
    """One authored row within a seating layout."""

    model_config = ConfigDict(extra='forbid')

    row_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=64)
    count: int = Field(ge=1, le=40)
    #: Center-to-center seat spacing along the row axis.
    spacing_m: float = Field(gt=0.2, le=3.0)
    #: Per-row depth offset from the previous row's anchor along +Y (rear).
    row_spacing_m: float = Field(gt=0.5, le=5.0, default=1.0)
    #: Alternate-row half-spacing offset along the row axis (stagger).
    stagger: bool = False
    #: Optional riser entity the row's seats bind to (seat height follows the
    #: riser top). Explicit reference — the generator never creates risers.
    riser_entity_id: str | None = None


class SeatingLayoutSpec(BaseModel):
    """Versioned persisted seating-layout authoring record."""

    model_config = ConfigDict(extra='forbid')

    schema_version: int = SEAT_SPEC_SCHEMA_VERSION
    spec_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=64)
    #: Anchor of the first row's first seat, in room coordinates.
    anchor_x_m: float
    anchor_y_m: float
    #: Row axis direction in plan, degrees counterclockwise from +X.
    row_direction_deg: float = 0.0
    #: Direction successive rows advance toward; 'rear' = +Y (away from the
    #: front wall at -Y... see note: +Y is the rear of the room).
    row_advance: Literal['rear', 'front'] = 'rear'
    rows: tuple[SeatRowSpec, ...] = Field(min_length=1)
    aisles: tuple[AisleSpec, ...] = ()
    #: Seat facing convention: 'front' faces -Y (toward the screen wall),
    #: matching the hand-authored seat convention (yaw 180).
    facing: Literal['front', 'rear'] = 'front'
    seat_width_m: float = 0.70
    seat_depth_m: float = 0.80
    seat_height_m: float = 0.90
    ear_height_m: float = 0.65

    @field_validator('rows')
    @classmethod
    def _unique_row_ids(cls, rows: tuple[SeatRowSpec, ...]) -> tuple[SeatRowSpec, ...]:
        ids = [row.row_id for row in rows]
        if len(ids) != len(set(ids)):
            raise ValueError('row ids must be unique')
        return rows

    @field_validator('aisles')
    @classmethod
    def _bounded_aisles(cls, aisles: tuple[AisleSpec, ...]) -> tuple[AisleSpec, ...]:
        if len(aisles) > 8:
            raise ValueError('aisles are limited to 8')
        indexes = [aisle.after_index for aisle in aisles]
        if len(indexes) != len(set(indexes)):
            raise ValueError('duplicate aisle positions')
        return aisles


def new_seating_spec_id() -> str:
    return f'seatlayout-{uuid4().hex[:10]}'


def spec_seat_entity_id(spec_id: str, row_id: str, seat_index: int) -> str:
    """Stable id for a generated seat — same slot, same id across regens."""

    return f'seat-{spec_id}-{row_id}-{seat_index}'


def spec_owns_entity(spec: SeatingLayoutSpec, entity_id: str) -> bool:
    return entity_id.startswith(f'seat-{spec.spec_id}-')


@dataclass(frozen=True)
class SeatSlot:
    """One planned seat placement in the layout."""

    row_index: int
    seat_index: int
    entity_id: str
    name: str
    position: Position3
    riser_entity_id: str | None


@dataclass(frozen=True)
class LayoutPlan:
    """Result of resolving a spec: slots plus per-row diagnostics."""

    slots: tuple[SeatSlot, ...]
    warnings: tuple[str, ...]


def _riser_top(document: SceneDocument, riser_id: str) -> float:
    """Riser top surface z. Missing riser blocks the row — never created."""

    try:
        riser = document.entity(riser_id)
    except KeyError as exc:
        raise SeatingLayoutError(
            f'段差参照のラック/ライザー「{riser_id}」が見つかりません。'
            'ライザーは自動作成されません'
        ) from exc
    if riser.kind != 'riser':
        raise SeatingLayoutError(
            f'指定された「{riser.name}」はライザーではありません'
        )
    top = riser.position.z_m
    if riser.size_m is not None:
        top = riser.position.z_m + riser.size_m.z_m * 0.5
    return top


def plan_seating(spec: SeatingLayoutSpec, document: SceneDocument) -> LayoutPlan:
    """Deterministically resolve a spec into seat slots (no document writes)."""

    slots: list[SeatSlot] = []
    warnings: list[str] = []
    direction = radians(spec.row_direction_deg)
    ux, uy = cos(direction), sin(direction)
    advance_sign = 1.0 if spec.row_advance == 'rear' else -1.0
    seat_size = Size3(
        x_m=spec.seat_width_m,
        y_m=spec.seat_depth_m,
        z_m=spec.seat_height_m,
    )
    base_z = seat_size.z_m * 0.5

    cursor_y = spec.anchor_y_m
    for row_index, row in enumerate(spec.rows):
        riser_z: float | None = None
        if row.riser_entity_id is not None:
            riser_z = _riser_top(document, row.riser_entity_id)
        z_m = base_z if riser_z is None else riser_z + seat_size.z_m * 0.5
        stagger_offset = (
            (row.spacing_m * 0.5) if row.stagger and row_index % 2 == 1 else 0.0
        )
        cursor = stagger_offset
        for seat_index in range(row.count):
            if seat_index > 0:
                cursor += row.spacing_m
                # An aisle adds its explicit width on top of normal spacing.
                for aisle in spec.aisles:
                    if aisle.after_index == seat_index - 1:
                        cursor += aisle.width_m
            x = spec.anchor_x_m + ux * cursor
            y = cursor_y + uy * cursor
            slots.append(
                SeatSlot(
                    row_index=row_index,
                    seat_index=seat_index,
                    entity_id=spec_seat_entity_id(spec.spec_id, row.row_id, seat_index),
                    name=f'{row.name}-{seat_index + 1}',
                    position=Position3(x_m=x, y_m=y, z_m=z_m),
                    riser_entity_id=row.riser_entity_id,
                )
            )
        if row_index < len(spec.rows) - 1:
            cursor_y += advance_sign * row.row_spacing_m
    return LayoutPlan(slots=tuple(slots), warnings=tuple(warnings))


def _seat_entity(spec: SeatingLayoutSpec, slot: SeatSlot) -> SceneEntity:
    yaw = 180.0 if spec.facing == 'front' else 0.0
    return SceneEntity(
        entity_id=slot.entity_id,
        kind='seat',
        name=slot.name,
        position=slot.position,
        orientation=quaternion_from_euler_deg(
            yaw_deg=yaw, pitch_deg=0.0, roll_deg=0.0
        ),
        size_m=Size3(
            x_m=spec.seat_width_m,
            y_m=spec.seat_depth_m,
            z_m=spec.seat_height_m,
        ),
        acoustic_reference_offset_m=Offset3(z_m=spec.ear_height_m),
    )


@dataclass(frozen=True)
class SeatingRegenerationDiff:
    """Preview diff for a regeneration: added / moved / removed / kept."""

    added: tuple[SceneEntity, ...]
    moved_before: tuple[SceneEntity, ...]
    moved_after: tuple[SceneEntity, ...]
    removed: tuple[SceneEntity, ...]


def plan_regeneration(
    spec: SeatingLayoutSpec, document: SceneDocument
) -> SeatingRegenerationDiff:
    """Diff the spec's planned seats against its existing generated seats."""

    plan = plan_seating(spec, document)
    slot_by_id = {slot.entity_id: slot for slot in plan.slots}
    existing = {
        entity.entity_id: entity
        for entity in document.entities
        if spec_owns_entity(spec, entity.entity_id)
    }
    added: list[SceneEntity] = []
    moved_before: list[SceneEntity] = []
    moved_after: list[SceneEntity] = []
    for slot in plan.slots:
        candidate = _seat_entity(spec, slot)
        current = existing.get(slot.entity_id)
        if current is None:
            added.append(candidate)
        elif current != candidate:
            moved_before.append(current)
            moved_after.append(candidate)
    removed = [
        entity for entity_id, entity in existing.items() if entity_id not in slot_by_id
    ]
    return SeatingRegenerationDiff(
        added=tuple(added),
        moved_before=tuple(moved_before),
        moved_after=tuple(moved_after),
        removed=tuple(removed),
    )


def apply_seating_layout(
    working: WorkingDocument,
    document: SceneDocument,
    spec: SeatingLayoutSpec,
    *,
    remove_orphaned: bool = False,
) -> SeatingRegenerationDiff:
    """Apply the planned layout atomically (one Undo step).

    ``remove_orphaned`` defaults to False — seats the new layout no longer
    references stay in place (and are still reported in the returned diff's
    ``removed`` list so the caller can surface them as stale candidates).
    """

    diff = plan_regeneration(spec, document)
    removed = diff.removed if remove_orphaned else ()
    presentation = CommandPresentation(
        action='layout',
        subject_names=(
            f'{spec.name}: +{len(diff.added)} / 移動 {len(diff.moved_after)}'
            f' / 解除 {len(removed)}'
        ),
        detail='座席レイアウト生成',
    )
    if not working.apply_entity_set_edit(
        removed=removed,
        replaced_before=diff.moved_before,
        replaced_after=diff.moved_after,
        added=diff.added,
        presentation=presentation,
    ):
        if not (diff.added or diff.moved_after or removed):
            raise SeatingLayoutError('レイアウトに変更はありません')
        raise SeatingLayoutError('座席レイアウトの適用に失敗しました')
    return diff


__all__ = [
    'AisleSpec',
    'DEFAULT_SEAT_REFERENCE',
    'DEFAULT_SEAT_SIZE',
    'LayoutPlan',
    'SeatRowSpec',
    'SeatSlot',
    'SeatingLayoutError',
    'SeatingLayoutSpec',
    'SeatingRegenerationDiff',
    'apply_seating_layout',
    'new_seating_spec_id',
    'plan_regeneration',
    'plan_seating',
    'spec_owns_entity',
    'spec_seat_entity_id',
]
