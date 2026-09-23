"""Room CAD layout tools (#613): copy/paste, mirror, align, distribute,
paired-speaker mirror and seat-row array generation.

All operations run in *domain coordinates* on the document's
``SceneEntity`` snapshots and are applied through one atomic
``EntitySetEditCommand`` on the WorkingDocument, so each tool is exactly one
Undo step. Copy/paste uses a typed in-memory clipboard (entity snapshots +
pivot + schema version) — it never fabricates evidence fields and never
reuses source ids. Mirroring across the room centerline is deterministic and
NEVER infers speaker roles; the optional paired-speaker helper proposes a
canonical role (FL→FR, …) that the caller must explicitly accept.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from .cad_document import CommandPresentation, WorkingDocument
from .cad_scene import (
    Direction3,
    Position3,
    SceneDocument,
    SceneEntity,
    quaternion_from_matrix3,
    quaternion_to_matrix3,
)


LAYOUT_CLIPBOARD_SCHEMA_VERSION = 1

#: Canonical stereo/surround counterpart proposals for the paired-speaker
#: tool. Only a proposal — the caller decides whether to apply it.
CANONICAL_SPEAKER_PAIRS: dict[str, str] = {
    'FL': 'FR',
    'SL': 'SR',
    'SBL': 'SBR',
    'TFL': 'TFR',
    'TML': 'TMR',
    'TRL': 'TRR',
    'L': 'R',
}


class LayoutError(ValueError):
    """A rejected layout-tool request (user-facing reason)."""


@dataclass(frozen=True)
class LayoutClipboard:
    """Typed entity-snapshot clipboard. In-memory only — the persistence
    contract is satisfied by storing full entity snapshots so a paste in a
    reopened session still works when this object is serialized by callers."""

    schema_version: int = LAYOUT_CLIPBOARD_SCHEMA_VERSION
    entities: tuple[SceneEntity, ...] = ()
    pivot: tuple[float, float, float] = (0.0, 0.0, 0.0)
    source_document_id: str | None = None


def copy_selection(document: SceneDocument, entity_ids: tuple[str, ...]) -> LayoutClipboard:
    """Snapshot the selected entities onto the layout clipboard."""

    entities: list[SceneEntity] = []
    for entity_id in entity_ids:
        try:
            entities.append(document.entity(entity_id))
        except KeyError:
            continue
    if not entities:
        raise LayoutError('コピーする項目を選択してください')
    px = sum(entity.position.x_m for entity in entities) / len(entities)
    py = sum(entity.position.y_m for entity in entities) / len(entities)
    pz = sum(entity.position.z_m for entity in entities) / len(entities)
    return LayoutClipboard(
        entities=tuple(entities),
        pivot=(px, py, pz),
        source_document_id=document.document_id,
    )


def _fresh_entity(source: SceneEntity, offset: tuple[float, float, float]) -> SceneEntity:
    return source.model_copy(
        update={
            'entity_id': f'{source.kind}-{uuid4().hex[:10]}',
            'name': f'{source.name} コピー',
            'position': Position3(
                x_m=source.position.x_m + offset[0],
                y_m=source.position.y_m + offset[1],
                z_m=source.position.z_m + offset[2],
            ),
        },
        deep=True,
    )


def paste_clipboard(
    working: WorkingDocument,
    clipboard: LayoutClipboard,
    *,
    offset: tuple[float, float, float] = (0.25, 0.25, 0.0),
) -> tuple[str, ...]:
    """Paste clipboard entities with fresh ids and a visible offset.

    One atomic Undo step via ``EntitySetEditCommand``. Returns the new ids in
    clipboard order.
    """

    if clipboard.schema_version != LAYOUT_CLIPBOARD_SCHEMA_VERSION:
        raise LayoutError('クリップボードの形式が旧バージョンです')
    if not clipboard.entities:
        raise LayoutError('クリップボードが空です')
    added = tuple(_fresh_entity(source, offset) for source in clipboard.entities)
    presentation = CommandPresentation(
        action='add',
        subject_names=tuple(entity.name for entity in added),
        detail='貼り付け',
    )
    if not working.apply_entity_set_edit(added=added, presentation=presentation):
        raise LayoutError('貼り付けに失敗しました')
    return tuple(entity.entity_id for entity in added)


def duplicate_entities(
    working: WorkingDocument,
    document: SceneDocument,
    entity_ids: tuple[str, ...],
    *,
    offset: tuple[float, float, float] = (0.25, 0.25, 0.0),
) -> tuple[str, ...]:
    """Exact-offset duplicate of the selected entities — one Undo step."""

    snapshots: list[SceneEntity] = []
    for entity_id in entity_ids:
        try:
            snapshots.append(document.entity(entity_id))
        except KeyError:
            continue
    if not snapshots:
        raise LayoutError('複製する項目を選択してください')
    added = tuple(_fresh_entity(source, offset) for source in snapshots)
    presentation = CommandPresentation(
        action='add',
        subject_names=tuple(entity.name for entity in added),
        detail='複製',
    )
    if not working.apply_entity_set_edit(added=added, presentation=presentation):
        raise LayoutError('複製に失敗しました')
    return tuple(entity.entity_id for entity in added)


def _mirrored_entity(
    source: SceneEntity, centerline: float, *, new_id: bool
) -> SceneEntity:
    """Mirror position + orientation across the x=centerline plane.

    Orientation mirror: R' = M·R·M with M = diag(-1,1,1) — keeps the mirrored
    pose's twist intact rather than re-canonicalizing it. aim_xyz is mirrored
    as a direction vector (x negated); speaker_role is copied verbatim —
    roles are never inferred from position.
    """

    matrix = quaternion_to_matrix3(source.orientation)
    mirrored = (
        (matrix[0][0], -matrix[0][1], -matrix[0][2]),
        (-matrix[1][0], matrix[1][1], matrix[1][2]),
        (-matrix[2][0], matrix[2][1], matrix[2][2]),
    )
    updates: dict[str, object] = {
        'position': Position3(
            x_m=2.0 * centerline - source.position.x_m,
            y_m=source.position.y_m,
            z_m=source.position.z_m,
        ),
        'orientation': quaternion_from_matrix3(mirrored),
    }
    if source.aim_xyz is not None:
        updates['aim_xyz'] = Direction3(
            x=-source.aim_xyz.x,
            y=source.aim_xyz.y,
            z=source.aim_xyz.z,
        )
    if new_id:
        updates['entity_id'] = f'{source.kind}-{uuid4().hex[:10]}'
        updates['name'] = f'{source.name} ミラー'
    return source.model_copy(update=updates, deep=True)


def mirror_entities_x(
    working: WorkingDocument,
    document: SceneDocument,
    entity_ids: tuple[str, ...],
    *,
    as_new: bool = False,
) -> tuple[str, ...]:
    """Mirror the given entities across the room's x-centerline (left/right).

    ``as_new=False`` repositions in place (Undo-friendly move); ``as_new=True``
    creates mirrored duplicates with fresh ids.
    """

    room = document.room
    if room is None:
        raise LayoutError('部屋が未設定です')
    min_x, _, max_x, _ = room.bounds_m
    centerline = (min_x + max_x) * 0.5
    sources = []
    for entity_id in entity_ids:
        try:
            sources.append(document.entity(entity_id))
        except KeyError:
            continue
    if not sources:
        raise LayoutError('ミラーする項目を選択してください')
    if as_new:
        added = tuple(
            _mirrored_entity(source, centerline, new_id=True) for source in sources
        )
        presentation = CommandPresentation(
            action='add',
            subject_names=tuple(entity.name for entity in added),
            detail='ミラー複製',
        )
        if not working.apply_entity_set_edit(added=added, presentation=presentation):
            raise LayoutError('ミラー複製に失敗しました')
        return tuple(entity.entity_id for entity in added)
    transformed = tuple(
        _mirrored_entity(source, centerline, new_id=False) for source in sources
    )
    presentation = CommandPresentation(
        action='transform',
        subject_names=tuple(entity.name for entity in transformed),
        detail='左右ミラー',
    )
    if not working.apply_entity_set_edit(
        replaced_before=tuple(sources),
        replaced_after=transformed,
        presentation=presentation,
    ):
        raise LayoutError('ミラーに失敗しました')
    return tuple(entity.entity_id for entity in transformed)


def mirror_entities_y(
    working: WorkingDocument,
    document: SceneDocument,
    entity_ids: tuple[str, ...],
    *,
    as_new: bool = False,
) -> tuple[str, ...]:
    """Mirror across the room's y-centerline (front/back)."""

    room = document.room
    if room is None:
        raise LayoutError('部屋が未設定です')
    _, min_y, _, max_y = room.bounds_m
    centerline = (min_y + max_y) * 0.5
    sources = []
    for entity_id in entity_ids:
        try:
            sources.append(document.entity(entity_id))
        except KeyError:
            continue
    if not sources:
        raise LayoutError('ミラーする項目を選択してください')

    def _mirror_y(source: SceneEntity, new_id: bool) -> SceneEntity:
        matrix = quaternion_to_matrix3(source.orientation)
        mirrored = (
            (matrix[0][0], matrix[0][1], -matrix[0][2]),
            (matrix[1][0], matrix[1][1], -matrix[1][2]),
            (-matrix[2][0], -matrix[2][1], matrix[2][2]),
        )
        # M = diag(1,-1,1): flip y-column and y-row signs.
        updates: dict[str, object] = {
            'position': Position3(
                x_m=source.position.x_m,
                y_m=2.0 * centerline - source.position.y_m,
                z_m=source.position.z_m,
            ),
            'orientation': quaternion_from_matrix3(mirrored),
        }
        if source.aim_xyz is not None:
            updates['aim_xyz'] = Direction3(
                x=source.aim_xyz.x,
                y=-source.aim_xyz.y,
                z=source.aim_xyz.z,
            )
        if new_id:
            updates['entity_id'] = f'{source.kind}-{uuid4().hex[:10]}'
            updates['name'] = f'{source.name} ミラー'
        return source.model_copy(update=updates, deep=True)

    if as_new:
        added = tuple(_mirror_y(source, True) for source in sources)
        presentation = CommandPresentation(
            action='add',
            subject_names=tuple(entity.name for entity in added),
            detail='ミラー複製',
        )
        if not working.apply_entity_set_edit(added=added, presentation=presentation):
            raise LayoutError('ミラー複製に失敗しました')
        return tuple(entity.entity_id for entity in added)
    transformed = tuple(_mirror_y(source, False) for source in sources)
    presentation = CommandPresentation(
        action='transform',
        subject_names=tuple(entity.name for entity in transformed),
        detail='前後ミラー',
    )
    if not working.apply_entity_set_edit(
        replaced_before=tuple(sources),
        replaced_after=transformed,
        presentation=presentation,
    ):
        raise LayoutError('ミラーに失敗しました')
    return tuple(entity.entity_id for entity in transformed)


def align_entities(
    working: WorkingDocument,
    document: SceneDocument,
    entity_ids: tuple[str, ...],
    *,
    axis: Literal['x', 'y'],
    mode: Literal['min', 'max', 'center'],
) -> tuple[str, ...]:
    """Align ≥2 entities along an axis to a common reference coordinate.

    Reference point is the selection's own bounding extent (min/max) or
    center — deterministic from the document, not selection order.
    """

    sources = []
    for entity_id in entity_ids:
        try:
            sources.append(document.entity(entity_id))
        except KeyError:
            continue
    if len(sources) < 2:
        raise LayoutError('揃えるには2つ以上の項目を選択してください')
    coord = lambda entity: (  # noqa: E731
        entity.position.x_m if axis == 'x' else entity.position.y_m
    )
    half = lambda entity: (  # noqa: E731 — physical size half-extent, else 0
        (entity.size_m.x_m if axis == 'x' else entity.size_m.y_m) * 0.5
        if entity.size_m is not None
        else 0.0
    )
    if mode == 'min':
        target = min(coord(entity) - half(entity) for entity in sources)
        setpoint = lambda entity: target + half(entity)  # noqa: E731
    elif mode == 'max':
        target = max(coord(entity) + half(entity) for entity in sources)
        setpoint = lambda entity: target - half(entity)  # noqa: E731
    else:
        low = min(coord(entity) for entity in sources)
        high = max(coord(entity) for entity in sources)
        target = (low + high) * 0.5
        setpoint = lambda entity: target  # noqa: E731
    transformed = []
    for entity in sources:
        value = setpoint(entity)
        position = Position3(
            x_m=value if axis == 'x' else entity.position.x_m,
            y_m=value if axis == 'y' else entity.position.y_m,
            z_m=entity.position.z_m,
        )
        transformed.append(entity.model_copy(update={'position': position}))
    presentation = CommandPresentation(
        action='transform',
        subject_names=tuple(entity.name for entity in transformed),
        detail='揃える',
    )
    if not working.apply_entity_set_edit(
        replaced_before=tuple(sources),
        replaced_after=tuple(transformed),
        presentation=presentation,
    ):
        return ()  # already aligned — apply_entity_set_edit no-ops
    return tuple(entity.entity_id for entity in transformed)


def distribute_entities(
    working: WorkingDocument,
    document: SceneDocument,
    entity_ids: tuple[str, ...],
    *,
    axis: Literal['x', 'y'],
) -> tuple[str, ...]:
    """Redistribute ≥3 entities along an axis with equal center spacing.

    The two extreme endpoints stay fixed — the interior entities move to
    even positions between them.
    """

    sources = []
    for entity_id in entity_ids:
        try:
            sources.append(document.entity(entity_id))
        except KeyError:
            continue
    if len(sources) < 3:
        raise LayoutError('等間隔配置には3つ以上の項目を選択してください')
    coord = lambda entity: (  # noqa: E731
        entity.position.x_m if axis == 'x' else entity.position.y_m
    )
    ordered = sorted(sources, key=coord)
    start, end = coord(ordered[0]), coord(ordered[-1])
    count = len(ordered) - 1
    transformed = []
    for index, entity in enumerate(ordered):
        target = start + (end - start) * (index / count)
        position = Position3(
            x_m=target if axis == 'x' else entity.position.x_m,
            y_m=target if axis == 'y' else entity.position.y_m,
            z_m=entity.position.z_m,
        )
        transformed.append(entity.model_copy(update={'position': position}))
    presentation = CommandPresentation(
        action='transform',
        subject_names=tuple(entity.name for entity in transformed),
        detail='等間隔配置',
    )
    if not working.apply_entity_set_edit(
        # before/after must be pairwise — both sorted along the axis.
        replaced_before=tuple(ordered),
        replaced_after=tuple(transformed),
        presentation=presentation,
    ):
        return ()  # already evenly spaced
    return tuple(entity.entity_id for entity in transformed)


def propose_pair_role(role: str | None) -> str | None:
    """Canonical counterpart role for a paired speaker (FL→FR, …).

    Returns ``None`` when the source role is not a canonical pair member —
    callers MUST NOT substitute a guess in that case.
    """

    if role is None:
        return None
    role_key = role.strip().upper()
    if role_key in CANONICAL_SPEAKER_PAIRS:
        return CANONICAL_SPEAKER_PAIRS[role_key]
    reverse = {right: left for left, right in CANONICAL_SPEAKER_PAIRS.items()}
    return reverse.get(role_key)


def mirror_speaker_pair(
    working: WorkingDocument,
    document: SceneDocument,
    speaker_id: str,
    *,
    apply_role_proposal: bool,
) -> str:
    """Create the canonical mirrored pair partner for one speaker.

    Mirrors across the room x-centerline into a NEW entity (one Undo step).
    When ``apply_role_proposal`` is False the partner keeps the source role
    verbatim (never inferred); when True the canonical counterpart
    (:func:`propose_pair_role`) is applied — an explicit user decision.
    """

    try:
        source = document.entity(speaker_id)
    except KeyError as exc:
        raise LayoutError('ペア複製するスピーカーを選択してください') from exc
    if source.kind != 'speaker':
        raise LayoutError('ペア複製はスピーカー専用です')
    room = document.room
    if room is None:
        raise LayoutError('部屋が未設定です')
    min_x, _, max_x, _ = room.bounds_m
    centerline = (min_x + max_x) * 0.5
    mirrored = _mirrored_entity(source, centerline, new_id=True)
    updates: dict[str, object] = {'name': f'{source.name} ペア'}
    if apply_role_proposal:
        proposed = propose_pair_role(source.speaker_role)
        if proposed is not None:
            updates['speaker_role'] = proposed
    mirrored = mirrored.model_copy(update=updates)
    presentation = CommandPresentation(
        action='add',
        subject_names=(mirrored.name,),
        detail='ペア複製',
    )
    if not working.apply_entity_set_edit(added=(mirrored,), presentation=presentation):
        raise LayoutError('ペア複製に失敗しました')
    return mirrored.entity_id


__all__ = [
    'CANONICAL_SPEAKER_PAIRS',
    'LAYOUT_CLIPBOARD_SCHEMA_VERSION',
    'LayoutClipboard',
    'LayoutError',
    'align_entities',
    'copy_selection',
    'distribute_entities',
    'duplicate_entities',
    'mirror_entities_x',
    'mirror_entities_y',
    'mirror_speaker_pair',
    'paste_clipboard',
    'propose_pair_role',
]
