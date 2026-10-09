"""Operational-clearance 3D layer presentation model (#1010).

Read-only derivation of *display symbols* from the persisted
``SceneEntity.operational_zones`` authority
(:mod:`htdt.cad_operational_geometry`). Doors swing, recliners extend,
racks slide out for service — this module turns each declared zone into a
planar footprint plus the conflict records that
:func:`operational_clearance_conflicts` returns. It performs no new
geometry math of its own and is the layer's only bridge into the
authority functions:

- every zone footprint is the world-space XY envelope polygon produced
  by :func:`operational_zone_footprint` — the renderer draws exactly the
  declared projection, never an invented 3-D swept solid;
- :func:`operational_clearance_conflicts` is the ONLY conflict source:
  both sides of each record (owning zone + conflicting entity, other
  zone, or the room boundary) are highlighted;
- a zone that cannot be evaluated to a usable footprint is an UNKNOWN
  zone (``未評価``) — it is never presented as "no interference";
- physical entities that declare no zones are 未宣言 — clearance is
  *undetermined* for them, distinct from a verified "no interference";
- zones carry optional height declarations; a zone without height data
  renders flat only and earns no "clear in height" claim anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass

from shapely.geometry.base import BaseGeometry

from .cad_operational_geometry import (
    operational_clearance_conflicts,
    operational_zone_footprint,
)
from .cad_orientation_constraints import entity_horizontal_footprint
from .cad_scene import (
    PHYSICAL_ENTITY_KINDS,
    SceneDocument,
    SceneEntity,
    room_vertices,
)
from .geometry import polygon_from_vertices


#: Zone-kind vocabulary — Japanese legend/filter label + display color.
#: Colors are explanation symbols only; they never encode severity.
OPERATIONAL_ZONE_KIND_VOCAB: dict[str, tuple[str, str]] = {
    'door_swing': ('開閉', '#ffb340'),
    'recline': ('リクライニング', '#59d98c'),
    'slide_out': ('引出し', '#4da3ff'),
    'service_access': ('サービス', '#e06ee0'),
    'rotate': ('回転', '#ffd166'),
}

#: Filter/display order for the five zone kinds.
OPERATIONAL_ZONE_KINDS: tuple[str, ...] = tuple(OPERATIONAL_ZONE_KIND_VOCAB)

#: Conflict highlight color (both sides of every conflict record).
OPERATIONAL_CONFLICT_COLOR = '#e05555'

#: Dim marker colors — UNKNOWN zone (unusable footprint) and undeclared
#: entity (no zones authored, clearance undetermined).
OPERATIONAL_UNKNOWN_ZONE_COLOR = '#4c5361'
OPERATIONAL_UNDECLARED_COLOR = '#8a93a3'

OPERATIONAL_CONFLICT_KIND_VOCAB: dict[str, tuple[str, str]] = {
    'intersects_entity': ('物体と交差', '#e05555'),
    'leaves_room': ('部屋外にはみ出し', '#e05555'),
    'overlaps_zone': ('他ゾーンと重複', '#e05555'),
}

OPERATIONAL_UNDECLARED_LABEL = '未宣言（判定不能）'
OPERATIONAL_UNKNOWN_ZONE_LABEL = '未評価ゾーン（判定不能）'

#: The honesty contract rendered next to the layer: this is an XY
#: projection, never an exact 3-D swept-solid collision test.
OPERATIONAL_CLEARANCE_DISCLAIMER = (
    '運用クリアランス: 床面へのXY投影のみを表示します — '
    '3Dスイープ立体による正確な衝突判定ではありません。'
    '高さ情報のないゾーンは高さ方向のクリアを意味しません。'
    'ゾーン未宣言の物体は「干渉なし」ではなく判定不能です。'
)

#: Below this area a footprint cannot be meaningfully drawn or swept —
#: the zone is presented as UNKNOWN rather than silently clear.
_MIN_USABLE_ZONE_AREA = 1e-9


@dataclass(frozen=True, slots=True)
class OperationalZoneRenderItem:
    """One declared operational zone resolved for display.

    ``footprint`` is the world-space XY envelope from
    :func:`operational_zone_footprint`; ``None`` marks an UNKNOWN zone
    that could not be evaluated — it renders as a dim marker, never as a
    clear (conflict-free) zone.
    """

    entity_id: str
    entity_name: str
    zone_id: str
    kind: str
    label: str
    footprint: BaseGeometry | None
    height_min_m: float
    height_max_m: float | None
    in_conflict: bool
    #: (x, y, top z) marker anchor in domain coordinates — where the
    #: UNKNOWN glyph floats for unevaluatable zones.
    marker_position: tuple[float, float, float] | None = None


@dataclass(frozen=True, slots=True)
class OperationalConflictRenderItem:
    """One ``operational_clearance_conflicts`` record resolved for display.

    ``region`` is the planar overlap/outside-room geometry to flood-fill;
    ``boundary_geometry`` the room-boundary line the zone covers for
    ``leaves_room``. ``other_footprint`` / ``other_zone_footprint`` carry
    the conflicting side's own geometry so BOTH parties highlight.
    """

    entity_id: str
    zone_id: str
    conflict_kind: str
    other_entity_id: str | None = None
    other_zone_id: str | None = None
    region: BaseGeometry | None = None
    boundary_geometry: BaseGeometry | None = None
    other_footprint: BaseGeometry | None = None
    other_zone_footprint: BaseGeometry | None = None


@dataclass(frozen=True, slots=True)
class OperationalUndeclaredMarker:
    """A physical entity with no operational zones — clearance undetermined."""

    entity_id: str
    entity_name: str
    position: tuple[float, float, float]
    top_z_m: float


@dataclass(frozen=True, slots=True)
class OperationalClearancePreview:
    """Everything the viewport/panel need to draw the clearance layer."""

    zones: tuple[OperationalZoneRenderItem, ...]
    #: Zones whose footprints could not be evaluated — never "clear".
    undefined_zones: tuple[OperationalZoneRenderItem, ...]
    undeclared: tuple[OperationalUndeclaredMarker, ...]
    #: Conflict records whose owning zone is currently displayed.
    conflicts: tuple[OperationalConflictRenderItem, ...]
    #: Zones filtered out by the kind filter (still evaluated, not shown).
    filtered_zone_count: int
    #: Conflict records hidden only because the owning zone kind is off.
    hidden_conflict_count: int
    total_zone_count: int
    total_conflict_count: int
    kind_legend: tuple[tuple[str, str], ...]
    summary: str
    disclaimer: str


def _usable_footprint(
    entity: SceneEntity, zone
) -> BaseGeometry | None:
    """The zone's world-XY footprint, or None when it cannot be evaluated.

    A footprint that degenerates to a point/line or evaluates empty is
    unusable for display AND for clearance claims — the zone is UNKNOWN.
    """

    try:
        footprint = operational_zone_footprint(entity, zone)
    except (AssertionError, ValueError):
        return None
    if (
        footprint is None
        or footprint.is_empty
        or not hasattr(footprint, 'area')
        or footprint.area <= _MIN_USABLE_ZONE_AREA
    ):
        return None
    return footprint


def _safe_region(a: BaseGeometry | None, b: BaseGeometry | None) -> BaseGeometry | None:
    """``a ∩ b`` guarded against degenerate inputs."""

    if a is None or b is None or a.is_empty or b.is_empty:
        return None
    try:
        region = a.intersection(b)
    except (ValueError, TypeError):
        return None
    if region is None or region.is_empty:
        return None
    return region


def _safe_difference(a: BaseGeometry | None, b: BaseGeometry | None) -> BaseGeometry | None:
    if a is None or b is None or a.is_empty or b.is_empty:
        return None
    try:
        region = a.difference(b)
    except (ValueError, TypeError):
        return None
    if region is None or region.is_empty:
        return None
    return region


def _safe_boundary(inside: BaseGeometry | None, boundary: BaseGeometry | None) -> BaseGeometry | None:
    """Room-boundary portion covered by the zone (where it crosses out)."""

    if inside is None or boundary is None or inside.is_empty:
        return None
    try:
        covered = inside.intersection(boundary)
    except (ValueError, TypeError):
        return None
    if covered is None or covered.is_empty:
        return None
    return covered


def _entity_marker(entity: SceneEntity) -> tuple[tuple[float, float, float], float]:
    """(domain position, top z) marker anchor for UNKNOWN/undeclared glyphs."""

    position = (
        float(entity.position.x_m),
        float(entity.position.y_m),
        float(entity.position.z_m),
    )
    top = float(entity.position.z_m) + (
        float(entity.size_m.z_m) * 0.5 if entity.size_m is not None else 0.0
    )
    return position, top


def build_operational_clearance_preview(
    *,
    document: SceneDocument,
    enabled_kinds=None,
) -> OperationalClearancePreview:
    """Derive the read-only layer model for the CURRENT document.

    Called on every render refresh with ``controller.document`` — which
    already reflects any active transform preview — so results can never
    be stale-revision: the footprints and the conflict records always
    come from the same live document.
    """

    enabled = (
        set(enabled_kinds)
        if enabled_kinds is not None
        else set(OPERATIONAL_ZONE_KIND_VOCAB)
    )
    entities = {entity.entity_id: entity for entity in document.entities}
    room_polygon = (
        polygon_from_vertices(
            [(float(v.x_m), float(v.y_m)) for v in room_vertices(document.room)]
        )
        if document.room is not None
        else None
    )

    # Footprints for EVERY declared zone (filtered-out kinds included) —
    # the conflict records reference them by (entity_id, zone_id).
    zone_footprints: dict[tuple[str, str], BaseGeometry | None] = {}
    zone_by_key: dict[tuple[str, str], object] = {}
    for entity in document.entities:
        for zone in entity.operational_zones or ():
            zone_footprints[(entity.entity_id, zone.zone_id)] = _usable_footprint(
                entity, zone
            )
            zone_by_key[(entity.entity_id, zone.zone_id)] = zone

    # The ONLY conflict source.
    all_conflicts = operational_clearance_conflicts(document)
    conflicted_zone_keys: set[tuple[str, str]] = set()
    for conflict in all_conflicts:
        conflicted_zone_keys.add((conflict.entity_id, conflict.zone_id))
        if conflict.other_zone_id is not None and conflict.other_entity_id is not None:
            conflicted_zone_keys.add((conflict.other_entity_id, conflict.other_zone_id))

    zones: list[OperationalZoneRenderItem] = []
    undefined_zones: list[OperationalZoneRenderItem] = []
    filtered_zone_count = 0
    for entity in document.entities:
        position, top_z = _entity_marker(entity)
        for zone in entity.operational_zones or ():
            footprint = zone_footprints[(entity.entity_id, zone.zone_id)]
            item = OperationalZoneRenderItem(
                entity_id=entity.entity_id,
                entity_name=entity.name,
                zone_id=zone.zone_id,
                kind=zone.kind,
                label=zone.label or zone.zone_id,
                footprint=footprint,
                height_min_m=float(zone.height_min_m),
                height_max_m=(
                    float(zone.height_max_m)
                    if zone.height_max_m is not None
                    else None
                ),
                in_conflict=(entity.entity_id, zone.zone_id) in conflicted_zone_keys,
                marker_position=(position[0], position[1], top_z),
            )
            if footprint is None:
                # UNKNOWN zone — unusable footprint; visually distinct
                # and never counted as clear.
                undefined_zones.append(item)
            elif zone.kind in enabled:
                zones.append(item)
            else:
                filtered_zone_count += 1

    conflicts: list[OperationalConflictRenderItem] = []
    hidden_conflict_count = 0
    for conflict in all_conflicts:
        zone_footprint = zone_footprints.get(
            (conflict.entity_id, conflict.zone_id)
        )
        owner_zone = zone_by_key.get((conflict.entity_id, conflict.zone_id))
        if owner_zone is not None and owner_zone.kind not in enabled:
            # The owning zone is filtered out — the conflict stays in the
            # honest count but is not drawn.
            hidden_conflict_count += 1
            continue
        region = None
        boundary_geometry = None
        other_footprint = None
        other_zone_footprint = None
        if conflict.conflict_kind == 'intersects_entity' and conflict.other_entity_id:
            other = entities.get(conflict.other_entity_id)
            if other is not None:
                other_footprint = entity_horizontal_footprint(other)
                region = _safe_region(zone_footprint, other_footprint)
        elif conflict.conflict_kind == 'overlaps_zone' and (
            conflict.other_entity_id and conflict.other_zone_id
        ):
            other_zone_footprint = zone_footprints.get(
                (conflict.other_entity_id, conflict.other_zone_id)
            )
            region = _safe_region(zone_footprint, other_zone_footprint)
        elif conflict.conflict_kind == 'leaves_room' and room_polygon is not None:
            region = _safe_difference(zone_footprint, room_polygon)
            boundary_geometry = _safe_boundary(zone_footprint, room_polygon.boundary)
        conflicts.append(
            OperationalConflictRenderItem(
                entity_id=conflict.entity_id,
                zone_id=conflict.zone_id,
                conflict_kind=conflict.conflict_kind,
                other_entity_id=conflict.other_entity_id,
                other_zone_id=conflict.other_zone_id,
                region=region,
                boundary_geometry=boundary_geometry,
                other_footprint=other_footprint,
                other_zone_footprint=other_zone_footprint,
            )
        )

    undeclared = [
        OperationalUndeclaredMarker(
            entity_id=entity.entity_id,
            entity_name=entity.name,
            position=_entity_marker(entity)[0],
            top_z_m=_entity_marker(entity)[1],
        )
        for entity in document.entities
        if entity.kind in PHYSICAL_ENTITY_KINDS
        and not entity.operational_zones
    ]

    used_kinds = [kind for kind in OPERATIONAL_ZONE_KIND_VOCAB if kind in enabled and any(
        zone.kind == kind for zone in zones
    )]
    kind_legend = tuple(OPERATIONAL_ZONE_KIND_VOCAB[kind] for kind in used_kinds)
    if conflicts:
        kind_legend += (('干渉あり', OPERATIONAL_CONFLICT_COLOR),)
    if undeclared:
        kind_legend += ((OPERATIONAL_UNDECLARED_LABEL, OPERATIONAL_UNDECLARED_COLOR),)
    if undefined_zones:
        kind_legend += ((OPERATIONAL_UNKNOWN_ZONE_LABEL, OPERATIONAL_UNKNOWN_ZONE_COLOR),)

    summary = (
        f'運用クリアランス · ゾーン{len(zones)}件'
        f' · 干渉{len(conflicts)}件'
        f' · 未宣言{len(undeclared)}物体（判定不能）'
    )
    if undefined_zones:
        summary += f' · 未評価ゾーン{len(undefined_zones)}件'
    if filtered_zone_count or hidden_conflict_count:
        summary += (
            f'（フィルタ外 ゾーン{filtered_zone_count}件'
            f'・干渉{hidden_conflict_count}件）'
        )

    return OperationalClearancePreview(
        zones=tuple(zones),
        undefined_zones=tuple(undefined_zones),
        undeclared=tuple(undeclared),
        conflicts=tuple(conflicts),
        filtered_zone_count=filtered_zone_count,
        hidden_conflict_count=hidden_conflict_count,
        total_zone_count=len(zones) + filtered_zone_count + len(undefined_zones),
        total_conflict_count=len(all_conflicts),
        kind_legend=kind_legend,
        summary=summary,
        disclaimer=OPERATIONAL_CLEARANCE_DISCLAIMER,
    )
