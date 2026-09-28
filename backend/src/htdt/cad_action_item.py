"""Project action items and spatial notes (#667).

Small actionable follow-ups ("move the sofa", "re-measure after fixing the
sweep delay", "confirm the legacy amp can feed the height channels") that
belong to the project but are *not* part of the authority chain.
:class:`ProjectActionItem` is lightweight workflow metadata: it points at
canonical evidence through exact typed references, never becomes a Scene
entity, a measurement point, or evidence truth.

Authority boundary (per the issue contract):

- creating/updating an item never mutates Scene content, calibration state,
  measurements or installed state;
- ``subject_refs``/``spatial_anchor`` are typed pointers — completion of an
  action never resolves a gap or marks a step PASS by itself;
- an item may pin a position in the room — a visualization anchor only,
  never a ``SceneEntity`` or an implicit measurement point;
- ``completed_at_utc``/``status`` are the only mutable workflow fields;
  the content hash pins every other field;
- archive (``archived=True``) hides finished items without deleting the
  record — the audit trail stays in the project database.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .canonical_json import canonical_json as _canonical_json, canonical_sha256 as _hash, canonicalize_payload


ACTION_ITEM_SCHEMA_VERSION = 1
ACTION_ITEM_AUTHORITY_VERSION = 'project-action-item-1'

ActionItemStatus = Literal['open', 'in_progress', 'waiting', 'done', 'dismissed']
ACTION_ITEM_STATUSES: frozenset[str] = frozenset(
    {'open', 'in_progress', 'waiting', 'done', 'dismissed'}
)

ActionItemPriority = Literal['low', 'normal', 'important']
ACTION_ITEM_PRIORITIES: frozenset[str] = frozenset(
    {'low', 'normal', 'important'}
)

#: Typed subject an action references — exact ids only, never names.
ActionSubjectKind = Literal[
    'scene_entity',
    'room_surface',
    'room_geometry',
    'equipment_instance',
    'measurement',
    'prediction',
    'system_variant',
    'cable_run',
    'construction_assembly',
    'capture_inbox_item',
    'evidence_gap',
    'assumption_decision',
    'design_decision',
    'commissioning_check',
    'measurement_plan',
    'calibration_plan',
    'scene_revision',
    'workflow_task',
    'project',
    'other',
]
ACTION_SUBJECT_KINDS: frozenset[str] = frozenset(
    {
        'scene_entity',
        'room_surface',
        'room_geometry',
        'equipment_instance',
        'measurement',
        'prediction',
        'system_variant',
        'cable_run',
        'construction_assembly',
        'capture_inbox_item',
        'evidence_gap',
        'assumption_decision',
        'design_decision',
        'commissioning_check',
        'measurement_plan',
        'calibration_plan',
        'scene_revision',
        'workflow_task',
        'project',
        'other',
    }
)

#: Where the item's physical context lives — the anchor is a presentation
#: hint for the 3D scene, never a SceneEntity or a measurement point.
SpatialAnchorKind = Literal['position', 'surface_ref', 'entity_ref']






class ActionSubjectRef(BaseModel):
    """Exact pointer to one canonical object an action is about."""

    model_config = ConfigDict(frozen=True)

    kind: ActionSubjectKind
    ref_id: str = Field(min_length=1)
    ref_sha256: str | None = Field(default=None, min_length=8)
    label: str | None = Field(default=None, min_length=1)


class ActionSpatialAnchor(BaseModel):
    """Optional room-space anchor for a spatial note/follow-up.

    ``scene_revision_id`` pins the revision the anchor was placed against —
    when the room later changes, the anchor is still interpretable.
    """

    model_config = ConfigDict(frozen=True)

    kind: SpatialAnchorKind
    position_m: tuple[float, float, float] | None = None
    ref_id: str | None = Field(default=None, min_length=1)
    scene_revision_id: str | None = Field(default=None, min_length=1)
    note: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def valid_anchor(self) -> 'ActionSpatialAnchor':
        if self.kind == 'position' and self.position_m is None:
            raise ValueError('position anchor requires position_m')
        if self.kind == 'position' and self.ref_id is not None:
            raise ValueError('position anchor must not carry a ref_id')
        if self.kind != 'position' and self.ref_id is None:
            raise ValueError(f'{self.kind} anchor requires ref_id')
        if self.kind != 'position' and self.position_m is not None:
            raise ValueError(f'{self.kind} anchor must not carry position_m')
        if self.position_m is not None and any(
            not (isinstance(value, float) or isinstance(value, int))
            or value != value  # NaN
            or abs(value) == float('inf')
            for value in self.position_m
        ):
            raise ValueError('position_m must be finite')
        return self


class ProjectActionItem(BaseModel):
    """One follow-up task bound to exact subject refs and an optional pin."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = ACTION_ITEM_SCHEMA_VERSION
    authority_version: Literal['project-action-item-1'] = (
        ACTION_ITEM_AUTHORITY_VERSION
    )
    action_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    detail: str | None = None
    status: ActionItemStatus = 'open'
    priority: ActionItemPriority = 'normal'
    created_at_utc: str = Field(min_length=1)
    updated_at_utc: str = Field(min_length=1)
    completed_at_utc: str | None = None
    #: Optional grouping — e.g. a site visit, a measuring session.
    visit_group: str | None = Field(default=None, min_length=1)
    subject_refs: tuple[ActionSubjectRef, ...] = ()
    spatial_anchor: ActionSpatialAnchor | None = None
    #: Which surface/record spawned this item — provenance, not authority.
    source_context: str | None = Field(default=None, min_length=1)
    #: Exact authority that closed this item when the fix landed (e.g. a
    #: scene_revision id). Optional — closing without a change is allowed
    #: (e.g. dismissed), and never fakes a resolution.
    resolution_ref: ActionSubjectRef | None = None
    archived: bool = False
    action_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def valid_action(self) -> 'ProjectActionItem':
        keys = {(ref.kind, ref.ref_id) for ref in self.subject_refs}
        if len(keys) != len(self.subject_refs):
            raise ValueError('subject_refs must be unique per kind/ref')
        if self.status in ('done', 'dismissed') and self.completed_at_utc is None:
            raise ValueError(f'{self.status} requires completed_at_utc')
        if self.status in ('open', 'in_progress', 'waiting'):
            if self.completed_at_utc is not None:
                raise ValueError(
                    f'{self.status} must not carry completed_at_utc'
                )
            if self.resolution_ref is not None:
                raise ValueError(
                    'resolution_ref is only valid for done/dismissed items'
                )
        if self.action_sha256 != _hash(self.semantic_payload()):
            raise ValueError('ProjectActionItem hash mismatch')
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            'schema_version': self.schema_version,
            'authority_version': self.authority_version,
            'action_id': self.action_id,
            'document_id': self.document_id,
            'title': self.title,
            'detail': self.detail,
            'status': self.status,
            'priority': self.priority,
            'created_at_utc': self.created_at_utc,
            'updated_at_utc': self.updated_at_utc,
            'completed_at_utc': self.completed_at_utc,
            'visit_group': self.visit_group,
            'subject_refs': [ref.model_dump(mode='json') for ref in self.subject_refs],
            'spatial_anchor': (
                None
                if self.spatial_anchor is None
                else self.spatial_anchor.model_dump(mode='json')
            ),
            'source_context': self.source_context,
            'resolution_ref': (
                None
                if self.resolution_ref is None
                else self.resolution_ref.model_dump(mode='json')
            ),
            'archived': self.archived,
        }


def build_action_item(
    *,
    document_id: str,
    title: str,
    created_at_utc: str,
    detail: str | None = None,
    status: ActionItemStatus = 'open',
    priority: ActionItemPriority = 'normal',
    visit_group: str | None = None,
    subject_refs: tuple[ActionSubjectRef, ...] = (),
    spatial_anchor: ActionSpatialAnchor | None = None,
    source_context: str | None = None,
    action_id: str | None = None,
) -> ProjectActionItem:
    """Create a new action item (always starts unarchived, unfinished)."""

    payload: dict[str, Any] = {
        'action_id': action_id or str(uuid4()),
        'document_id': document_id,
        'title': title,
        'detail': detail,
        'status': status,
        'priority': priority,
        'created_at_utc': created_at_utc,
        'updated_at_utc': created_at_utc,
        'completed_at_utc': None,
        'visit_group': visit_group,
        'subject_refs': tuple(subject_refs),
        'spatial_anchor': spatial_anchor,
        'source_context': source_context,
        'resolution_ref': None,
        'archived': False,
    }
    provisional = ProjectActionItem.model_construct(
        **payload, action_sha256='0' * 64
    )
    return ProjectActionItem(
        **payload,
        action_sha256=_hash(provisional.semantic_payload()),
    )


def update_action_item(
    item: ProjectActionItem,
    *,
    updated_at_utc: str,
    title: str | None = None,
    detail: str | None = None,
    status: ActionItemStatus | None = None,
    priority: ActionItemPriority | None = None,
    visit_group: str | None = None,
    subject_refs: tuple[ActionSubjectRef, ...] | None = None,
    spatial_anchor: ActionSpatialAnchor | None = None,
    resolution_ref: ActionSubjectRef | None = None,
    archived: bool | None = None,
) -> ProjectActionItem:
    """Return a new immutable record with the workflow fields updated.

    Passing ``status='done'``/``'dismissed'`` stamps ``completed_at_utc``;
    reopening clears both the completion stamp and the resolution ref.
    """

    next_status = status if status is not None else item.status
    finished = next_status in ('done', 'dismissed')
    if resolution_ref is not None and not finished:
        raise ValueError(
            'resolution_ref is only valid for done/dismissed items'
        )
    payload: dict[str, Any] = {
        'action_id': item.action_id,
        'document_id': item.document_id,
        'title': title if title is not None else item.title,
        'detail': detail if detail is not None else item.detail,
        'status': next_status,
        'priority': priority if priority is not None else item.priority,
        'created_at_utc': item.created_at_utc,
        'updated_at_utc': updated_at_utc,
        'completed_at_utc': (
            item.completed_at_utc or updated_at_utc if finished else None
        ),
        'visit_group': (
            visit_group if visit_group is not None else item.visit_group
        ),
        'subject_refs': (
            tuple(subject_refs)
            if subject_refs is not None
            else item.subject_refs
        ),
        'spatial_anchor': (
            spatial_anchor
            if spatial_anchor is not None
            else item.spatial_anchor
        ),
        'source_context': item.source_context,
        'resolution_ref': (
            (resolution_ref if resolution_ref is not None else item.resolution_ref)
            if finished
            else None
        ),
        'archived': archived if archived is not None else item.archived,
    }
    provisional = ProjectActionItem.model_construct(**canonicalize_payload(ProjectActionItem, dict(
        **payload, action_sha256='0' * 64
    )))
    return ProjectActionItem(
        **payload,
        action_sha256=_hash(provisional.semantic_payload()),
    )


__all__ = [
    'ACTION_ITEM_AUTHORITY_VERSION',
    'ACTION_ITEM_PRIORITIES',
    'ACTION_ITEM_SCHEMA_VERSION',
    'ACTION_ITEM_STATUSES',
    'ACTION_SUBJECT_KINDS',
    'ActionItemPriority',
    'ActionItemStatus',
    'ActionSpatialAnchor',
    'ActionSubjectKind',
    'ActionSubjectRef',
    'ProjectActionItem',
    'SpatialAnchorKind',
    'build_action_item',
    'update_action_item',
]
