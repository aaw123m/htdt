"""Structured SceneRevision history services (#485).

The repository already persists an immutable single-head revision lineage.
This module adds the read-side authority the history UX needs: a structured
``SceneDiff`` between two revisions plus a human-readable summary, so the
history panel can explain "what changed" without re-parsing payloads in UI
code. Nothing here mutates revisions, labels, or the document head.
"""

from __future__ import annotations

from dataclasses import dataclass

from .cad_scene import SceneDocument, scene_content_hash


# Display order and Japanese labels mirror room_workspace.KIND_LABELS so the
# history summary reads identically to the object palette.
ENTITY_FIELD_LABELS: dict[str, str] = {
    'name': '名称',
    'kind': '種別',
    'position': '位置',
    'orientation': '向き',
    'size_m': 'サイズ',
    'acoustic_reference_offset_m': '音響基準位置',
    'speaker_role': 'スピーカーロール',
    'aim_xyz': '指向',
    'body_geometry': '形状',
    'semantic_bindings': '機能割当',
    'operational_zones': '運用クリアランス',
}


@dataclass(frozen=True)
class EntityFieldChange:
    entity_id: str
    entity_name: str
    kind: str
    fields: tuple[str, ...]


@dataclass(frozen=True)
class SceneDiff:
    """Structured comparison between two scene documents (same document_id)."""

    added_entity_ids: tuple[str, ...]
    removed_entity_ids: tuple[str, ...]
    entity_changes: tuple[EntityFieldChange, ...]
    room_changed: bool
    wall_topology_changed: bool
    semantic_geometry_changed: bool
    attachments_changed: bool = False
    construction_assemblies_changed: bool = False
    #: Entity ORDER is persisted content (it feeds the content hash): a
    #: reorder-only revision changes the document and must not diff empty
    #: even though no entity's fields moved (#REV21).
    entity_order_changed: bool = False

    @property
    def is_empty(self) -> bool:
        return (
            not self.added_entity_ids
            and not self.removed_entity_ids
            and not self.entity_changes
            and not self.room_changed
            and not self.wall_topology_changed
            and not self.semantic_geometry_changed
            and not self.attachments_changed
            and not self.construction_assemblies_changed
            and not self.entity_order_changed
        )


_ENTITY_DIFF_FIELDS = (
    'name',
    'kind',
    'position',
    'orientation',
    'size_m',
    'acoustic_reference_offset_m',
    'speaker_role',
    'aim_xyz',
    'body_geometry',
    'semantic_bindings',
    'operational_zones',
)


def diff_scene_documents(before: SceneDocument, after: SceneDocument) -> SceneDiff:
    """Compare two scene documents and report a structured diff.

    The comparison is field-accurate — it reports which persisted fields of
    which entity changed — so history UI can explain a revision without
    guessing. Documents must share a document_id; cross-document diffs are a
    caller error because lineage is per-document.
    """

    if before.document_id != after.document_id:
        raise ValueError('scene diff requires both revisions of the same document')

    before_entities = {entity.entity_id: entity for entity in before.entities}
    after_entities = {entity.entity_id: entity for entity in after.entities}
    added = tuple(
        entity_id for entity_id in after_entities if entity_id not in before_entities
    )
    removed = tuple(
        entity_id for entity_id in before_entities if entity_id not in after_entities
    )
    changes: list[EntityFieldChange] = []
    for entity_id in before_entities.keys() & after_entities.keys():
        source = before_entities[entity_id]
        target = after_entities[entity_id]
        if source == target:
            continue
        fields = tuple(
            field
            for field in _ENTITY_DIFF_FIELDS
            if getattr(source, field) != getattr(target, field)
        )
        changes.append(
            EntityFieldChange(
                entity_id=entity_id,
                entity_name=target.name,
                kind=target.kind,
                fields=fields,
            )
        )
    before_ids = tuple(entity.entity_id for entity in before.entities)
    after_ids = tuple(entity.entity_id for entity in after.entities)
    return SceneDiff(
        added_entity_ids=added,
        removed_entity_ids=removed,
        entity_changes=tuple(changes),
        room_changed=before.room != after.room,
        wall_topology_changed=before.wall_topology != after.wall_topology,
        semantic_geometry_changed=(
            before.r120_semantic_geometry != after.r120_semantic_geometry
        ),
        attachments_changed=before.attachments != after.attachments,
        construction_assemblies_changed=(
            before.construction_assemblies != after.construction_assemblies
        ),
        # Only a same-set reorder is reported as reorder — with additions or
        # removals a changed order is expected and not a separate fact.
        entity_order_changed=(
            not added and not removed and before_ids != after_ids
        ),
    )


def diff_summary_lines(
    diff: SceneDiff,
    document: SceneDocument,
    *,
    fallback_document: SceneDocument | None = None,
) -> tuple[str, ...]:
    """Japanese summary lines describing a diff, anchored to names in ``document``.

    ``document`` is whichever side the reader is anchored to (normally the
    newer revision) so entity names resolve to familiar labels.
    ``fallback_document`` supplies names for entities missing from the anchor
    (removed entities only exist on the older side) — without it their raw
    ids are shown.
    """

    entities = {entity.entity_id: entity for entity in document.entities}
    fallback_entities = (
        {}
        if fallback_document is None
        else {
            entity.entity_id: entity for entity in fallback_document.entities
        }
    )

    def _label(entity_id: str) -> str:
        entity = entities.get(entity_id) or fallback_entities.get(entity_id)
        return entity.name if entity is not None else entity_id

    lines: list[str] = []
    if diff.room_changed:
        lines.append('部屋形状を変更')
    if diff.wall_topology_changed:
        lines.append('壁構造（壁・開口部）を変更')
    if diff.semantic_geometry_changed:
        lines.append('意味ジオメトリを変更')
    if diff.attachments_changed:
        lines.append('取付・マウント関係を変更')
    if diff.construction_assemblies_changed:
        lines.append('構造アセンブリを変更')
    if diff.entity_order_changed:
        lines.append('オブジェクトの順序を変更')
    for entity_id in diff.added_entity_ids:
        lines.append(f'{_label(entity_id)} を追加')
    for entity_id in diff.removed_entity_ids:
        lines.append(f'{_label(entity_id)} を削除')
    for change in diff.entity_changes:
        field_labels = '、'.join(
            ENTITY_FIELD_LABELS.get(field, field) for field in change.fields
        )
        lines.append(f'{change.entity_name}: {field_labels}を変更')
    if not lines:
        lines.append('変更なし（同一内容）')
    return tuple(lines)


@dataclass(frozen=True)
class RevisionSummary:
    """Compact per-revision display data for history lists."""

    entity_count: int
    kind_counts: tuple[tuple[str, int], ...]
    has_room: bool
    wall_count: int
    content_hash: str


def summarize_revision(document: SceneDocument) -> RevisionSummary:
    """Summarize one revision's document for list/preview rows."""

    counts: dict[str, int] = {}
    for entity in document.entities:
        counts[entity.kind] = counts.get(entity.kind, 0) + 1
    return RevisionSummary(
        entity_count=len(document.entities),
        kind_counts=tuple(sorted(counts.items())),
        has_room=document.room is not None,
        wall_count=len(document.wall_topology.walls) if document.wall_topology else 0,
        content_hash=scene_content_hash(document),
    )
