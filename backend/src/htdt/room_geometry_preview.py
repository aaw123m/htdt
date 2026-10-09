"""Pre-apply impact previews for dangerous room-geometry edits (#982).

Dangerous inspector operations (wall merge/delete, vertex delete, opening
update/delete, wall thickness) no longer commit on click. They first build
an uncommitted candidate through the existing ``cad_walls`` authority, diff
it against the committed state, and hand the panel a
:class:`GeometryChangePreview` the user must explicitly apply or discard.

Honesty contract:

* A preview never mutates the document — it only carries a candidate.
* The preview is bound to ``(source_revision_id, scene_content_hash)`` of
  the committed document it was computed from; :meth:`is_stale` lets the
  apply path reject delayed results that target a moved SceneRevision.
* Side-effects that cannot be computed are listed as ``不明`` (unknown),
  never silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import hypot
from typing import Callable
from uuid import uuid4

from .cad_room_authoring import validate_room_authoring_model
from .cad_scene import (
    RoomAuthoringModel,
    RoomPrism,
    make_polygon_room,
    room_vertices,
    scene_content_hash,
)
from .cad_wall_models import WallOpening, WallTopology
from .cad_walls import (
    WallTopologyError,
    delete_opening,
    delete_wall,
    merge_walls,
    update_opening,
    update_wall_thickness,
    wall_length,
)

FormatM = Callable[[float], str]


@dataclass(frozen=True)
class GeometryChangePreview:
    """One armed, uncommitted geometry change plus its human impact report.

    ``room``/``topology`` hold the candidate to commit on apply (``topology``
    ``None`` means the change is room-only). ``feasible`` is False when the
    authority refused to build a candidate — the panel renders the report
    but disables apply, so the affected IDs are still visible to the user.
    """

    kind: str
    title: str
    target_id: str
    source_revision_id: str | None
    content_hash: str
    room: RoomPrism | None = None
    topology: WallTopology | None = None
    room_only: bool = False
    feasible: bool = True
    lines: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    # Selection fixups applied after a successful commit.
    select_vertex_id: str | None = None
    select_edge_index: int | None = None
    select_opening_id: str | None = None
    clear_selection: bool = False

    def is_stale(self, document, source_revision_id: str | None) -> bool:
        """True when the committed document moved since this preview was built."""

        return (
            self.source_revision_id != source_revision_id
            or self.content_hash != scene_content_hash(document)
        )


def _base_key(document, source_revision_id: str | None) -> tuple[str | None, str]:
    return source_revision_id, scene_content_hash(document)


def _wall_label(topology: WallTopology, wall_id: str) -> str:
    for index, wall in enumerate(topology.walls):
        if wall.wall_id == wall_id:
            return f"壁 {index + 1}（{wall_id}）"
    return wall_id


def _opening_label(opening: WallOpening) -> str:
    kinds = {"door": "ドア", "window": "窓", "passage": "通路", "other": "その他"}
    return f"{opening.opening_id}（{kinds.get(opening.kind, opening.kind)}）"


def _wall_refs(topology: WallTopology, wall_ids: set[str]) -> tuple[list, list]:
    """Openings / constraint bindings that reference any of ``wall_ids``."""

    openings = [
        opening for opening in topology.openings if opening.wall_id in wall_ids
    ]
    bindings = [
        binding
        for binding in topology.constraint_bindings
        if any(item in wall_ids for item in binding.wall_ids)
    ]
    return openings, bindings


def _diff_openings(
    before: WallTopology,
    after: WallTopology,
    *,
    fmt: FormatM,
) -> tuple[list[str], list[str], list[str]]:
    """Classify openings across a topology change: lost / reassigned / moved."""

    lost: list[str] = []
    reassigned: list[str] = []
    moved: list[str] = []
    after_by_id = {item.opening_id: item for item in after.openings}
    for opening in before.openings:
        new = after_by_id.get(opening.opening_id)
        if new is None:
            lost.append(_opening_label(opening))
            continue
        if new.wall_id != opening.wall_id:
            reassigned.append(
                f"{_opening_label(opening)}: 壁 {opening.wall_id} → {new.wall_id}"
                f"（開始位置 {fmt(opening.offset_m)} → {fmt(new.offset_m)}）"
            )
            continue
        changes: list[str] = []
        if abs(new.offset_m - opening.offset_m) > 1e-9:
            changes.append(
                f"開始位置 {fmt(opening.offset_m)} → {fmt(new.offset_m)}"
            )
        if abs(new.width_m - opening.width_m) > 1e-9:
            changes.append(f"幅 {fmt(opening.width_m)} → {fmt(new.width_m)}")
        if abs(new.sill_m - opening.sill_m) > 1e-9:
            changes.append(f"床から {fmt(opening.sill_m)} → {fmt(new.sill_m)}")
        if abs(new.height_m - opening.height_m) > 1e-9:
            changes.append(f"高さ {fmt(opening.height_m)} → {fmt(new.height_m)}")
        if new.kind != opening.kind:
            changes.append(f"種類 {_opening_label(opening).split('（')[-1].rstrip('）')} → {_opening_label(new).split('（')[-1].rstrip('）')}")
        if new.is_open != opening.is_open:
            changes.append("開放扱い " + ("オン → オフ" if opening.is_open else "オフ → オン"))
        if changes:
            moved.append(f"{_opening_label(opening)}: " + "、".join(changes))
    for opening in after.openings:
        if opening.opening_id not in {item.opening_id for item in before.openings}:
            moved.append(f"新しい開口: {_opening_label(opening)}")
    return lost, reassigned, moved


def _diff_bindings(
    before: WallTopology,
    after: WallTopology,
    *,
    fmt: FormatM,
) -> list[str]:
    """Constraint bindings whose wall set or clearance changed."""

    lines: list[str] = []
    after_by_id = {item.binding_id: item for item in after.constraint_bindings}
    for binding in before.constraint_bindings:
        new = after_by_id.get(binding.binding_id)
        if new is None:
            lines.append(f"クリアランス参照 {binding.binding_id}: 削除されます")
            continue
        changes: list[str] = []
        if new.wall_ids != binding.wall_ids:
            changes.append(
                f"対象壁 {', '.join(binding.wall_ids)} → {', '.join(new.wall_ids)}"
            )
        if abs(new.clearance_m - binding.clearance_m) > 1e-9:
            changes.append(
                f"距離 {fmt(binding.clearance_m)} → {fmt(new.clearance_m)}"
            )
        if changes:
            lines.append(f"クリアランス参照 {binding.binding_id}: " + "、".join(changes))
    return lines


def _authoring_issues(
    authoring: RoomAuthoringModel | None,
    topology: WallTopology | None,
) -> list[str]:
    """Advisory re-validation of the 高度な形状 model against the candidate."""

    if authoring is None:
        return []
    try:
        issues = validate_room_authoring_model(authoring, wall_topology=topology)
    except Exception:
        return ["高度な形状への影響: 不明"]
    lines: list[str] = []
    for issue in issues:
        label = "エラー" if issue.severity == "error" else "注意"
        lines.append(f"高度な形状 {label}: {issue.message}")
        if len(lines) >= 3:
            lines.append(f"…ほか {len(issues) - 3} 件")
            break
    return lines


def _readiness_line() -> str:
    """Solver/optimization readiness impact — we cannot compute it here."""

    return "最適化・solver readiness への影響: 不明（実行時に再評価されます）"


def preview_wall_delete(
    room: RoomPrism,
    topology: WallTopology,
    wall_id: str,
    *,
    document,
    source_revision_id: str | None,
    authoring: RoomAuthoringModel | None,
    fmt: FormatM,
) -> GeometryChangePreview:
    revision_id, content_hash = _base_key(document, source_revision_id)
    label = _wall_label(topology, wall_id)
    walls = list(topology.walls)
    index = next(i for i, wall in enumerate(walls) if wall.wall_id == wall_id)
    successor = walls[(index + 1) % len(walls)]
    wall = walls[index]
    openings, bindings = _wall_refs(topology, {wall_id, successor.wall_id})
    lines: list[str] = [
        f"{label}（長さ {fmt(wall_length(room, wall))}）を削除し、"
        f"{_wall_label(topology, successor.wall_id)} と1本にまとめます",
    ]
    if openings:
        lines.append(
            "影響する開口: " + "、".join(_opening_label(o) for o in openings)
            + " — 再割当・削除しない限り削除できません"
        )
    if bindings:
        lines.append(
            "影響するクリアランス参照: "
            + "、".join(b.binding_id for b in bindings)
        )
    thickness_mismatch = abs(wall.thickness_m - successor.thickness_m) > 1e-9
    if thickness_mismatch:
        lines.append(
            f"対象壁（{fmt(wall.thickness_m)}）と次の壁"
            f"（{fmt(successor.thickness_m)}）の厚さが異なります"
        )
    try:
        new_room, new_topology = delete_wall(
            room, topology, wall_id,
            replacement_wall_id=f"wall-replacement-{uuid4().hex[:10]}",
        )
    except WallTopologyError as exc:
        lines.append(_readiness_line())
        return GeometryChangePreview(
            kind="wall_delete",
            title=f"{label} の削除",
            target_id=wall_id,
            source_revision_id=revision_id,
            content_hash=content_hash,
            feasible=False,
            lines=tuple(lines),
            blockers=(str(exc),),
        )
    lines.append(
        f"外周の壁: {len(topology.walls)} 本 → {len(new_topology.walls)} 本"
    )
    lines.extend(_diff_bindings(topology, new_topology, fmt=fmt))
    lines.extend(_authoring_issues(authoring, new_topology))
    lines.append(_readiness_line())
    return GeometryChangePreview(
        kind="wall_delete",
        title=f"{label} の削除",
        target_id=wall_id,
        source_revision_id=revision_id,
        content_hash=content_hash,
        room=new_room,
        topology=new_topology,
        clear_selection=True,
        lines=tuple(lines),
    )


def preview_wall_merge(
    room: RoomPrism,
    topology: WallTopology,
    wall_id: str,
    *,
    document,
    source_revision_id: str | None,
    authoring: RoomAuthoringModel | None,
    fmt: FormatM,
) -> GeometryChangePreview:
    revision_id, content_hash = _base_key(document, source_revision_id)
    walls = list(topology.walls)
    index = next(i for i, wall in enumerate(walls) if wall.wall_id == wall_id)
    first = walls[index]
    second = walls[(index + 1) % len(walls)]
    label = _wall_label(topology, wall_id)
    merged_id = f"wall-merged-{uuid4().hex[:10]}"
    lines: list[str] = [
        f"{label} と {_wall_label(topology, second.wall_id)} を1本に結合します",
        f"結合後の長さ: {fmt(wall_length(room, first) + wall_length(room, second))}",
    ]
    openings, bindings = _wall_refs(topology, {first.wall_id, second.wall_id})
    if openings:
        lines.append(
            "再割当される開口: " + "、".join(_opening_label(o) for o in openings)
        )
    if bindings:
        lines.append(
            "再割当されるクリアランス参照: "
            + "、".join(b.binding_id for b in bindings)
        )
    try:
        new_room, new_topology = merge_walls(
            room, topology, first.wall_id, second.wall_id,
            merged_wall_id=merged_id,
        )
    except WallTopologyError as exc:
        lines.append(_readiness_line())
        return GeometryChangePreview(
            kind="wall_merge",
            title=f"{label} の結合",
            target_id=wall_id,
            source_revision_id=revision_id,
            content_hash=content_hash,
            feasible=False,
            lines=tuple(lines),
            blockers=(str(exc),),
        )
    lost, reassigned, moved = _diff_openings(topology, new_topology, fmt=fmt)
    lines.extend(f"消失する開口: {item}" for item in lost)
    lines.extend(f"開口の再割当: {item}" for item in reassigned)
    lines.extend(f"開口の変更: {item}" for item in moved)
    lines.extend(_diff_bindings(topology, new_topology, fmt=fmt))
    lines.extend(_authoring_issues(authoring, new_topology))
    lines.append(_readiness_line())
    # The merged wall lands on the edge index of `first` (or last for the
    # wrap-around pair) — keep the selection on the merged wall.
    edge_index = index if index < len(walls) - 1 else len(new_topology.walls) - 1
    return GeometryChangePreview(
        kind="wall_merge",
        title=f"{label} の結合",
        target_id=wall_id,
        source_revision_id=revision_id,
        content_hash=content_hash,
        room=new_room,
        topology=new_topology,
        select_edge_index=edge_index,
        lines=tuple(lines),
    )


def preview_vertex_delete(
    room: RoomPrism,
    topology: WallTopology | None,
    vertex_id: str,
    *,
    document,
    source_revision_id: str | None,
    authoring: RoomAuthoringModel | None,
    fmt: FormatM,
) -> GeometryChangePreview:
    """Vertex deletion routes through the same paths as delete_selected_vertex."""

    revision_id, content_hash = _base_key(document, source_revision_id)
    vertices = tuple(room_vertices(room))
    index = next(i for i, v in enumerate(vertices) if v.vertex_id == vertex_id)
    lines: list[str] = [f"頂点 {vertex_id} を削除します（両隣が直線で結ばれます）"]
    if len(vertices) <= 3:
        lines.append("部屋には3頂点以上が必要です")
        return GeometryChangePreview(
            kind="vertex_delete",
            title=f"頂点 {vertex_id} の削除",
            target_id=vertex_id,
            source_revision_id=revision_id,
            content_hash=content_hash,
            feasible=False,
            lines=tuple(lines),
            blockers=("部屋には3頂点以上が必要です",),
        )
    if topology is None:
        try:
            new_room = make_polygon_room(
                tuple(v for v in vertices if v.vertex_id != vertex_id),
                height_m=room.height_m,
                room_id=room.room_id,
            )
        except ValueError as exc:
            lines.append(_readiness_line())
            return GeometryChangePreview(
                kind="vertex_delete",
                title=f"頂点 {vertex_id} の削除",
                target_id=vertex_id,
                source_revision_id=revision_id,
                content_hash=content_hash,
                feasible=False,
                lines=tuple(lines),
                blockers=(str(exc),),
            )
        lines.append(f"頂点数: {len(vertices)} → {len(vertices) - 1}")
        lines.extend(_authoring_issues(authoring, None))
        lines.append(_readiness_line())
        return GeometryChangePreview(
            kind="vertex_delete",
            title=f"頂点 {vertex_id} の削除",
            target_id=vertex_id,
            source_revision_id=revision_id,
            content_hash=content_hash,
            room=new_room,
            room_only=True,
            clear_selection=True,
            lines=tuple(lines),
        )
    predecessor_edge = (index - 1) % len(vertices)
    pair = (
        vertices[predecessor_edge].vertex_id,
        vertices[(predecessor_edge + 1) % len(vertices)].vertex_id,
    )
    predecessor = next(
        (
            wall
            for wall in topology.walls
            if (wall.from_vertex_id, wall.to_vertex_id) == pair
        ),
        None,
    )
    if predecessor is None:
        return GeometryChangePreview(
            kind="vertex_delete",
            title=f"頂点 {vertex_id} の削除",
            target_id=vertex_id,
            source_revision_id=revision_id,
            content_hash=content_hash,
            feasible=False,
            lines=tuple(lines),
            blockers=("削除対象の辺に対応する壁が見つかりません",),
        )
    preview = preview_wall_delete(
        room,
        topology,
        predecessor.wall_id,
        document=document,
        source_revision_id=source_revision_id,
        authoring=authoring,
        fmt=fmt,
    )
    return GeometryChangePreview(
        kind="vertex_delete",
        title=f"頂点 {vertex_id} の削除",
        target_id=vertex_id,
        source_revision_id=revision_id,
        content_hash=content_hash,
        room=preview.room,
        topology=preview.topology,
        feasible=preview.feasible,
        lines=(lines[0], *preview.lines),
        blockers=preview.blockers,
        clear_selection=True,
    )


def preview_wall_thickness(
    room: RoomPrism,
    topology: WallTopology,
    wall_id: str,
    thickness_m: float,
    *,
    document,
    source_revision_id: str | None,
    authoring: RoomAuthoringModel | None,
    fmt: FormatM,
) -> GeometryChangePreview:
    revision_id, content_hash = _base_key(document, source_revision_id)
    label = _wall_label(topology, wall_id)
    wall = next(w for w in topology.walls if w.wall_id == wall_id)
    lines: list[str] = [
        f"{label} の厚さ: {fmt(wall.thickness_m)} → {fmt(thickness_m)}",
    ]
    walls = list(topology.walls)
    index = walls.index(wall)
    for neighbor in (walls[(index - 1) % len(walls)], walls[(index + 1) % len(walls)]):
        if abs(neighbor.thickness_m - thickness_m) > 1e-9:
            lines.append(
                f"{_wall_label(topology, neighbor.wall_id)}"
                f"（厚さ {fmt(neighbor.thickness_m)}）との結合・削除は"
                "厚さが揃うまで実行できなくなります"
            )
    try:
        new_topology = update_wall_thickness(
            room, topology, wall_id, thickness_m=thickness_m
        )
    except WallTopologyError as exc:
        lines.append(_readiness_line())
        return GeometryChangePreview(
            kind="wall_thickness",
            title=f"{label} の厚さ変更",
            target_id=wall_id,
            source_revision_id=revision_id,
            content_hash=content_hash,
            feasible=False,
            lines=tuple(lines),
            blockers=(str(exc),),
        )
    lines.extend(_authoring_issues(authoring, new_topology))
    lines.append(_readiness_line())
    return GeometryChangePreview(
        kind="wall_thickness",
        title=f"{label} の厚さ変更",
        target_id=wall_id,
        source_revision_id=revision_id,
        content_hash=content_hash,
        room=room,
        topology=new_topology,
        lines=tuple(lines),
    )


def preview_opening_update(
    room: RoomPrism,
    topology: WallTopology,
    replacement: WallOpening,
    *,
    document,
    source_revision_id: str | None,
    authoring: RoomAuthoringModel | None,
    fmt: FormatM,
) -> GeometryChangePreview:
    revision_id, content_hash = _base_key(document, source_revision_id)
    current = next(
        (
            item
            for item in topology.openings
            if item.opening_id == replacement.opening_id
        ),
        None,
    )
    label = _opening_label(replacement)
    lines: list[str] = [f"{label} を更新します"]
    try:
        new_topology = update_opening(room, topology, replacement)
    except WallTopologyError as exc:
        if current is not None:
            wall = next(
                (w for w in topology.walls if w.wall_id == current.wall_id), None
            )
            if wall is not None:
                lines.append(
                    f"壁の長さ {fmt(wall_length(room, wall))} の範囲内に"
                    "開口が収まる必要があります"
                )
        lines.append(_readiness_line())
        return GeometryChangePreview(
            kind="opening_update",
            title=f"{label} の更新",
            target_id=replacement.opening_id,
            source_revision_id=revision_id,
            content_hash=content_hash,
            feasible=False,
            lines=tuple(lines),
            blockers=(str(exc),),
        )
    lost, reassigned, moved = _diff_openings(topology, new_topology, fmt=fmt)
    lines.extend(f"開口の変更: {item}" for item in moved)
    if not moved:
        lines.append("寸法・種類に変更はありません")
    lines.extend(_authoring_issues(authoring, new_topology))
    lines.append(_readiness_line())
    return GeometryChangePreview(
        kind="opening_update",
        title=f"{label} の更新",
        target_id=replacement.opening_id,
        source_revision_id=revision_id,
        content_hash=content_hash,
        room=room,
        topology=new_topology,
        select_opening_id=replacement.opening_id,
        lines=tuple(lines),
    )


def preview_opening_delete(
    room: RoomPrism,
    topology: WallTopology,
    opening_id: str,
    *,
    document,
    source_revision_id: str | None,
    authoring: RoomAuthoringModel | None,
    fmt: FormatM,
) -> GeometryChangePreview:
    revision_id, content_hash = _base_key(document, source_revision_id)
    opening = next(
        (item for item in topology.openings if item.opening_id == opening_id),
        None,
    )
    if opening is None:
        return GeometryChangePreview(
            kind="opening_delete",
            title=f"開口 {opening_id} の削除",
            target_id=opening_id,
            source_revision_id=revision_id,
            content_hash=content_hash,
            feasible=False,
            lines=(),
            blockers=("開口が見つかりません",),
        )
    label = _opening_label(opening)
    wall = next((w for w in topology.walls if w.wall_id == opening.wall_id), None)
    lines: list[str] = [
        f"{label}（{_wall_label(topology, opening.wall_id)} 上、"
        f"開始 {fmt(opening.offset_m)}・幅 {fmt(opening.width_m)}）を削除します",
        "壁自体は残り、開口だけが閉じられます",
    ]
    if wall is not None:
        lines.append(f"削除後の壁は穴のない壁（長さ {fmt(wall_length(room, wall))}）になります")
    try:
        new_topology = delete_opening(room, topology, opening_id)
    except WallTopologyError as exc:
        lines.append(_readiness_line())
        return GeometryChangePreview(
            kind="opening_delete",
            title=f"{label} の削除",
            target_id=opening_id,
            source_revision_id=revision_id,
            content_hash=content_hash,
            feasible=False,
            lines=tuple(lines),
            blockers=(str(exc),),
        )
    lines.extend(_authoring_issues(authoring, new_topology))
    lines.append(_readiness_line())
    return GeometryChangePreview(
        kind="opening_delete",
        title=f"{label} の削除",
        target_id=opening_id,
        source_revision_id=revision_id,
        content_hash=content_hash,
        room=room,
        topology=new_topology,
        select_opening_id=None,
        lines=tuple(lines),
    )


__all__ = [
    "GeometryChangePreview",
    "preview_opening_delete",
    "preview_opening_update",
    "preview_vertex_delete",
    "preview_wall_delete",
    "preview_wall_merge",
    "preview_wall_thickness",
]
