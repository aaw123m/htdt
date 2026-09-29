"""Physical attachment and assembly authority (Issue #661).

Stands, wall mounts, racks, and stacked equipment are real spatial
relationships, not just co-located boxes: a display mount on a stand, an AV
receiver in rack unit 4, a speaker on a riser. This module models them as a
per-scene attachment graph — ``SceneDocument.attachments`` — where each edge
binds a child entity to a named anchor on a parent, with a child-local
offset.

Contract:

- The graph is a forest: ``validate_attachment_graph`` enforces that each
  child attaches to exactly one parent, all endpoints exist as physical
  entities, and the parent relation is acyclic. Violations fail closed at
  document validation, never at render time.
- ``attached_world_position`` derives the child's authoritative world pose:
  the parent-local offset (anchor face plus ``child_anchor_offset_m``) rotated
  by the parent's ``orientation`` and translated to the parent's resolved
  world position, composed transitively up the parent chain. A rack
  attachment places the child at the front face at the rack-unit's height.
- ``resolve_attached_positions`` returns every attached entity's derived
  world position so moved parents carry their children deterministically.
- Placement kinds stay deliberately coarse (stand-on, mount-to, rack-in,
  place-inside, stack-on): enough authority for clearance and semantics,
  nowhere near mechanical-CAD mating.
"""

from __future__ import annotations

from typing import Mapping

from pydantic import BaseModel, ConfigDict, Field

from .cad_attachment_models import EntityAttachment
from .cad_scene import (
    PHYSICAL_ENTITY_KINDS,
    Position3,
    Quaternion4,
    SceneDocument,
    SceneEntity,
    quaternion_to_matrix3,
)


class AttachmentGraphError(ValueError):
    """Attachment graph is malformed (unknown endpoint, cycle, duplicate)."""


def attachment_graph(
    document: SceneDocument,
) -> dict[str, EntityAttachment]:
    """child_entity_id → attachment map, validated."""

    attachments = document.attachments or ()
    entities = {entity.entity_id: entity for entity in document.entities}
    graph: dict[str, EntityAttachment] = {}
    for attachment in attachments:
        if attachment.child_entity_id in graph:
            raise AttachmentGraphError(
                f'entity {attachment.child_entity_id} attaches to more than one parent'
            )
        for endpoint in (attachment.child_entity_id, attachment.parent_entity_id):
            entity = entities.get(endpoint)
            if entity is None:
                raise AttachmentGraphError(
                    f'attachment {attachment.attachment_id} references unknown entity: {endpoint}'
                )
            if entity.kind not in PHYSICAL_ENTITY_KINDS:
                raise AttachmentGraphError(
                    f'attachment {attachment.attachment_id} binds non-physical entity: {endpoint}'
                )
        graph[attachment.child_entity_id] = attachment
    # Acyclicity: walk each child to a root; a revisited node means a cycle.
    for child_id in graph:
        seen: set[str] = set()
        node = child_id
        while node in graph:
            if node in seen:
                raise AttachmentGraphError(
                    f'attachment graph contains a cycle involving {node}'
                )
            seen.add(node)
            node = graph[node].parent_entity_id
    return graph


def validate_attachment_graph(document: SceneDocument) -> None:
    """Fail closed on any malformed attachment graph in the document."""

    attachment_graph(document)


def _rotate_local(
    vector: tuple[float, float, float],
    orientation: Quaternion4,
) -> tuple[float, float, float]:
    rotation = quaternion_to_matrix3(orientation)
    return tuple(
        sum(rotation[row][col] * vector[col] for col in range(3))
        for row in range(3)
    )


def _parent_anchor_local(
    parent: SceneEntity,
    attachment: EntityAttachment,
) -> tuple[float, float, float]:
    """Parent-local offset of the point on the parent the child attaches to."""

    size = parent.size_m
    hx = float(size.x_m) * 0.5 if size else 0.0
    hy = float(size.y_m) * 0.5 if size else 0.0
    hz = float(size.z_m) * 0.5 if size else 0.0
    anchor = attachment.parent_anchor
    if anchor == 'top_surface':
        return (0.0, 0.0, hz)
    if anchor == 'front_face':
        return (0.0, -hy, 0.0)
    if anchor == 'rear_face':
        return (0.0, hy, 0.0)
    if anchor == 'left_face':
        return (-hx, 0.0, 0.0)
    if anchor == 'right_face':
        return (hx, 0.0, 0.0)
    # interior — for racked_in, the child's face sits at the rack front at
    # its rack-unit height (1U = 0.04445 m) above the rack base.
    if attachment.kind == 'racked_in' and attachment.rack_unit is not None:
        unit_height = 0.04445
        z = -hz + (attachment.rack_unit - 1) * unit_height + unit_height * 0.5
        return (0.0, -hy, z)
    return (0.0, 0.0, 0.0)


def attached_world_position(
    document: SceneDocument,
    attachment: EntityAttachment,
) -> Position3:
    """Authoritative world position of an attached child entity."""

    graph = attachment_graph(document)
    entities = {entity.entity_id: entity for entity in document.entities}

    def resolve(entity_id: str) -> tuple[float, float, float]:
        edge = graph.get(entity_id)
        entity = entities[entity_id]
        if edge is None:
            p = entity.position
            return (float(p.x_m), float(p.y_m), float(p.z_m))
        parent_point = resolve(edge.parent_entity_id)
        parent = entities[edge.parent_entity_id]
        # The child's world transform is parent ∘ local: the anchor face
        # offset and child_anchor_offset_m are parent-local axes, rotated
        # into world space by the parent's orientation.
        local = tuple(
            anchor + offset
            for anchor, offset in zip(
                _parent_anchor_local(parent, edge),
                (float(value) for value in edge.child_anchor_offset_m),
            )
        )
        delta = _rotate_local(local, parent.orientation)
        return (
            parent_point[0] + delta[0],
            parent_point[1] + delta[1],
            parent_point[2] + delta[2],
        )

    x, y, z = resolve(attachment.child_entity_id)
    return Position3(x_m=x, y_m=y, z_m=z)


def resolve_attached_positions(document: SceneDocument) -> dict[str, Position3]:
    """Derived world positions for every attached entity."""

    graph = attachment_graph(document)
    return {
        child_id: attached_world_position(document, attachment)
        for child_id, attachment in graph.items()
    }


def attachment_children(document: SceneDocument) -> dict[str, tuple[str, ...]]:
    """parent_entity_id → attached child ids (sorted for determinism)."""

    graph = attachment_graph(document)
    children: dict[str, list[str]] = {}
    for attachment in graph.values():
        children.setdefault(attachment.parent_entity_id, []).append(
            attachment.child_entity_id
        )
    return {
        parent: tuple(sorted(child_ids)) for parent, child_ids in children.items()
    }


def apply_attachments(document: SceneDocument) -> SceneDocument:
    """Return a document whose attached entities sit at derived positions.

    The stored ``position`` of an attached entity is a cached projection of
    the attachment authority — re-deriving keeps it consistent after the
    parent moves.
    """

    graph = attachment_graph(document)
    if not graph:
        return document
    positions = resolve_attached_positions(document)
    entities = tuple(
        entity.model_copy(update={'position': positions[entity.entity_id]})
        if entity.entity_id in positions
        else entity
        for entity in document.entities
    )
    return document.model_copy(update={'entities': entities})
