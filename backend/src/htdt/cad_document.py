from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from .cad_attachment_models import EntityAttachment
from .cad_scene import (
    Position3, Quaternion4, SceneDocument, SceneEntity,
    rotate_orientation_world, rotate_position_world, scene_content_hash,
)
from .physical_attachment import apply_attachments, attached_world_position


class EditStateError(RuntimeError):
    pass


def _detach_attachment_orphans(
    document: SceneDocument,
    removed_ids: frozenset[str],
) -> tuple[tuple[EntityAttachment, ...] | None, dict[str, Position3]]:
    """Drop attachment edges touching removed entities; orphans land at their
    derived world pose resolved on the pre-delete document (#661).

    Returns (kept attachments — None when empty, per the document invariant —
    and ``child_id -> derived world Position3`` landings).
    """

    edges = document.attachments or ()
    kept = tuple(
        edge for edge in edges
        if edge.parent_entity_id not in removed_ids
        and edge.child_entity_id not in removed_ids
    )
    if len(kept) == len(edges):
        return document.attachments, {}
    landings = {
        edge.child_entity_id: attached_world_position(document, edge)
        for edge in edges
        if edge.parent_entity_id in removed_ids
        and edge.child_entity_id not in removed_ids
    }
    return kept or None, landings


def _replace_entity(document: SceneDocument, replacement: SceneEntity) -> SceneDocument:
    document.entity(replacement.entity_id)
    entities = tuple(replacement if item.entity_id == replacement.entity_id else item for item in document.entities)
    return document.model_copy(update={'entities': entities})


def _replace_entities(document: SceneDocument, replacements: tuple[SceneEntity, ...]) -> SceneDocument:
    mapping = {entity.entity_id: entity for entity in replacements}
    if len(mapping) != len(replacements):
        raise EditStateError('replacement entity ids must be unique')
    existing = {entity.entity_id for entity in document.entities}
    missing = set(mapping) - existing
    if missing:
        raise EditStateError(f'cannot replace unknown entities: {sorted(missing)}')
    entities = tuple(mapping.get(item.entity_id, item) for item in document.entities)
    return document.model_copy(update={'entities': entities})


def _move(document: SceneDocument, entity_id: str, position: Position3) -> SceneDocument:
    entity = document.entity(entity_id)
    return _replace_entity(document, entity.model_copy(update={'position': position}))


def _rotate(document: SceneDocument, entity_id: str, orientation: Quaternion4) -> SceneDocument:
    entity = document.entity(entity_id)
    return _replace_entity(document, entity.model_copy(update={'orientation': orientation}))


def _delete(document: SceneDocument, entity_id: str) -> SceneDocument:
    document.entity(entity_id)
    return document.model_copy(update={'entities': tuple(item for item in document.entities if item.entity_id != entity_id)})


def _insert(document: SceneDocument, index: int, entity: SceneEntity) -> SceneDocument:
    if any(item.entity_id == entity.entity_id for item in document.entities):
        raise EditStateError(f'entity already exists: {entity.entity_id}')
    if not 0 <= index <= len(document.entities):
        raise EditStateError(f'entity insertion index out of range: {index}')
    entities = list(document.entities)
    entities.insert(index, entity)
    return document.model_copy(update={'entities': tuple(entities)})


@dataclass(frozen=True, slots=True)
class CommandPresentation:
    """Human-facing description of an EditCommand for undo/redo labels.

    Set at the call site that knows the semantic intent (a plain dataclass
    attribute, never derived from the command class name). ``label`` is an
    optional complete localized phrase for operations whose vocabulary is not
    covered by ``action`` (e.g. layout tools); when absent the label is
    composed from ``action`` + subjects.
    """

    action: str
    subject_names: tuple[str, ...] = ()
    detail: str | None = None
    label: str | None = None


@dataclass(frozen=True, slots=True)
class CommandHistoryEntry:
    """Read-only view over one command in the bounded working-edit history."""

    index: int
    label: str
    applied: bool


_COMMAND_SUBJECT_LIMIT = 2

_UPDATE_FIELD_LABELS: tuple[tuple[str, str], ...] = (
    ('name', '名前変更'),
    ('position', '位置変更'),
    ('size_m', '寸法変更'),
    ('orientation', '向き変更'),
    ('aim_xyz', '音響方向変更'),
    ('speaker_role', '役割変更'),
    ('body_geometry', '形状変更'),
    ('acoustic_reference_offset_m', '基準点変更'),
)


def _update_detail(before: SceneEntity, after: SceneEntity) -> str:
    """Single-field summary for inspector edits; generic label on mixed edits."""

    changed = [
        label
        for field_name, label in _UPDATE_FIELD_LABELS
        if getattr(before, field_name) != getattr(after, field_name)
    ]
    return changed[0] if len(changed) == 1 else '編集'


def _presentation_of(command: EditCommand) -> CommandPresentation | None:
    return getattr(command, 'presentation', None)


def describe_command(command: EditCommand) -> str:
    """Localized one-line description of what apply/revert changes.

    Never a class name: labels come from CommandPresentation only, falling
    back to a generic phrase when the call site supplied no presentation.
    """

    presentation = _presentation_of(command)
    if presentation is None:
        return '編集'
    if presentation.label:
        return presentation.label
    names = presentation.subject_names
    count = len(names)
    if count == 1:
        subject = f'「{names[0]}」'
    elif count <= _COMMAND_SUBJECT_LIMIT and count:
        subject = '・'.join(f'「{name}」' for name in names)
    elif count:
        subject = f'{count}件'
    else:
        subject = ''
    verb = {
        'move': '移動',
        'rotate': '回転',
        'add': '追加',
        'delete': '削除',
        'duplicate': '複製',
        'edit': '編集',
        'transform': '変更',
        'replace_document': '設計を変更',
    }.get(presentation.action, presentation.action)
    label = f'{verb}{subject}'
    if presentation.detail:
        label = f'{presentation.detail}{subject}' if subject else presentation.detail
    return label


class EditCommand(Protocol):
    @property
    def is_noop(self) -> bool: ...

    def apply(self, document: SceneDocument) -> SceneDocument: ...

    def revert(self, document: SceneDocument) -> SceneDocument: ...


@dataclass(frozen=True)
class MoveEntityCommand:
    entity_id: str
    before: Position3
    after: Position3
    presentation: CommandPresentation | None = None

    @property
    def is_noop(self) -> bool:
        return self.before == self.after

    def apply(self, document: SceneDocument) -> SceneDocument:
        return _move(document, self.entity_id, self.after)

    def revert(self, document: SceneDocument) -> SceneDocument:
        return _move(document, self.entity_id, self.before)


@dataclass(frozen=True)
class RotateEntityCommand:
    entity_id: str
    before: Quaternion4
    after: Quaternion4
    presentation: CommandPresentation | None = None

    @property
    def is_noop(self) -> bool:
        return self.before == self.after

    def apply(self, document: SceneDocument) -> SceneDocument:
        return _rotate(document, self.entity_id, self.after)

    def revert(self, document: SceneDocument) -> SceneDocument:
        return _rotate(document, self.entity_id, self.before)


@dataclass(frozen=True)
class TransformEntitiesCommand:
    before: tuple[SceneEntity, ...]
    after: tuple[SceneEntity, ...]
    presentation: CommandPresentation | None = None

    @property
    def is_noop(self) -> bool:
        return self.before == self.after

    def apply(self, document: SceneDocument) -> SceneDocument:
        return _replace_entities(document, self.after)

    def revert(self, document: SceneDocument) -> SceneDocument:
        return _replace_entities(document, self.before)


@dataclass(frozen=True)
class AddEntitiesCommand:
    entities: tuple[SceneEntity, ...]
    index: int
    presentation: CommandPresentation | None = None

    @property
    def is_noop(self) -> bool:
        return not self.entities

    def apply(self, document: SceneDocument) -> SceneDocument:
        updated = document
        for offset, entity in enumerate(self.entities):
            updated = _insert(updated, self.index + offset, entity)
        return updated

    def revert(self, document: SceneDocument) -> SceneDocument:
        updated = document
        for entity in reversed(self.entities):
            updated = _delete(updated, entity.entity_id)
        return updated


@dataclass(frozen=True)
class ReplaceEntityCommand:
    before: SceneEntity
    after: SceneEntity
    presentation: CommandPresentation | None = None

    def __post_init__(self) -> None:
        if self.before.entity_id != self.after.entity_id:
            raise EditStateError('replace entity command must preserve entity_id')

    @property
    def is_noop(self) -> bool:
        return self.before == self.after

    def apply(self, document: SceneDocument) -> SceneDocument:
        return _replace_entity(document, self.after)

    def revert(self, document: SceneDocument) -> SceneDocument:
        return _replace_entity(document, self.before)


@dataclass(frozen=True)
class DeleteEntityCommand:
    entity: SceneEntity
    index: int
    presentation: CommandPresentation | None = None

    @property
    def is_noop(self) -> bool:
        return False

    def apply(self, document: SceneDocument) -> SceneDocument:
        return _delete(document, self.entity.entity_id)

    def revert(self, document: SceneDocument) -> SceneDocument:
        return _insert(document, self.index, self.entity)


def _remove_entities(document: SceneDocument, entity_ids: frozenset[str]) -> SceneDocument:
    missing = entity_ids - {entity.entity_id for entity in document.entities}
    if missing:
        raise EditStateError(f'cannot remove unknown entities: {sorted(missing)}')
    return document.model_copy(update={
        'entities': tuple(entity for entity in document.entities if entity.entity_id not in entity_ids),
    })


def _append_entities(document: SceneDocument, additions: tuple[SceneEntity, ...]) -> SceneDocument:
    existing = {entity.entity_id for entity in document.entities}
    duplicates = existing.intersection(entity.entity_id for entity in additions)
    if duplicates:
        raise EditStateError(f'entities already exist: {sorted(duplicates)}')
    return document.model_copy(update={'entities': document.entities + additions})


@dataclass(frozen=True)
class CompositeEditCommand:
    """One Undo step covering an entity edit plus external design state.

    The entity command applies/reverts on the document; the side effects
    capture and restore state that lives outside the document (e.g. the
    authoring-constraint set, #843) so geometry and design semantics roll
    back together.
    """

    inner: 'EditCommand | None'
    apply_side: Callable[[], None] | None = None
    revert_side: Callable[[], None] | None = None
    presentation: CommandPresentation | None = None

    @property
    def is_noop(self) -> bool:
        return (
            self.inner is None or self.inner.is_noop
        ) and self.apply_side is None and self.revert_side is None

    def apply(self, document: SceneDocument) -> SceneDocument:
        updated = self.inner.apply(document) if self.inner is not None else document
        if self.apply_side is not None:
            self.apply_side()
        return updated

    def revert(self, document: SceneDocument) -> SceneDocument:
        updated = self.inner.revert(document) if self.inner is not None else document
        if self.revert_side is not None:
            self.revert_side()
        return updated


@dataclass(frozen=True)
class EntitySetEditCommand:
    """Atomic mixed edit: removed ids + replaced ids -> replaced + appended entities.

    One semantic batch (e.g. a seating-layout regeneration that moves some
    seats, adds some and removes others) stays a single undo step.
    """

    removed: tuple[SceneEntity, ...]
    replaced_before: tuple[SceneEntity, ...]
    replaced_after: tuple[SceneEntity, ...]
    added: tuple[SceneEntity, ...]
    presentation: CommandPresentation | None = None
    # Parallel to ``removed``: each entity's index in the document before the
    # edit, so revert restores the exact entity order (entity order is part of
    # the content hash, like DeleteEntitiesCommand).
    removed_indices: tuple[int, ...] | None = None
    # Attachment state before the edit; removed parents drop their edges and
    # surviving children land at their derived world pose (#661).
    before_attachments: tuple[EntityAttachment, ...] | None = None
    orphaned_positions: tuple[tuple[str, Position3], ...] = ()

    @property
    def is_noop(self) -> bool:
        return not self.removed and not self.added and self.replaced_before == self.replaced_after

    def apply(self, document: SceneDocument) -> SceneDocument:
        removed_ids = frozenset(entity.entity_id for entity in self.removed)
        updated = _remove_entities(document, removed_ids)
        kept_edges, landings = _detach_attachment_orphans(document, removed_ids)
        if landings:
            updated = updated.model_copy(update={'entities': tuple(
                entity.model_copy(update={'position': landings[entity.entity_id]})
                if entity.entity_id in landings else entity
                for entity in updated.entities
            )})
        updated = updated.model_copy(update={'attachments': kept_edges})
        if self.replaced_after:
            updated = _replace_entities(updated, self.replaced_after)
        if self.added:
            updated = _append_entities(updated, self.added)
        return updated

    def revert(self, document: SceneDocument) -> SceneDocument:
        updated = _remove_entities(document, frozenset(entity.entity_id for entity in self.added))
        if self.replaced_before:
            updated = _replace_entities(updated, self.replaced_before)
        if not self.removed:
            return updated.model_copy(update={'attachments': self.before_attachments})
        if (
            self.removed_indices is None
            or len(self.removed_indices) != len(self.removed)
            or any(index < 0 for index in self.removed_indices)
        ):
            raise EditStateError('entity set edit does not record removal positions')
        entities = list(updated.entities)
        inserted: set[str] = set()
        pairs = sorted(zip(self.removed_indices, self.removed), key=lambda pair: pair[0])
        for index, entity in pairs:
            if entity.entity_id in inserted:
                continue
            inserted.add(entity.entity_id)
            entities.insert(min(index, len(entities)), entity)
        orphan_positions = dict(self.orphaned_positions)
        entities = [
            entity.model_copy(update={'position': orphan_positions[entity.entity_id]})
            if entity.entity_id in orphan_positions else entity
            for entity in entities
        ]
        return updated.model_copy(update={
            'entities': tuple(entities),
            'attachments': self.before_attachments,
        })


@dataclass(frozen=True)
class DeleteEntitiesCommand:
    """Atomic delete of several entities: one Undo step restores the exact set."""

    removed: tuple[tuple[int, SceneEntity], ...]
    presentation: CommandPresentation | None = None
    # Prior attachment edges and pre-delete orphan positions so revert
    # restores the exact relationship state, not just the entity list.
    before_attachments: tuple[EntityAttachment, ...] | None = None
    orphaned_positions: tuple[tuple[str, Position3], ...] = ()

    @property
    def is_noop(self) -> bool:
        return not self.removed

    def apply(self, document: SceneDocument) -> SceneDocument:
        removed_ids = {entity.entity_id for _, entity in self.removed}
        kept_edges, landings = _detach_attachment_orphans(document, frozenset(removed_ids))
        remaining = tuple(
            entity.model_copy(update={'position': landings[entity.entity_id]})
            if entity.entity_id in landings else entity
            for entity in document.entities if entity.entity_id not in removed_ids
        )
        if len(remaining) + len(self.removed) != len(document.entities):
            raise EditStateError('document does not contain every entity scheduled for deletion')
        return document.model_copy(update={'entities': remaining, 'attachments': kept_edges})

    def revert(self, document: SceneDocument) -> SceneDocument:
        orphan_positions = dict(self.orphaned_positions)
        entities = [
            entity.model_copy(update={'position': orphan_positions[entity.entity_id]})
            if entity.entity_id in orphan_positions else entity
            for entity in document.entities
        ]
        for index, entity in self.removed:
            entities.insert(index, entity)
        return document.model_copy(update={
            'entities': tuple(entities),
            'attachments': self.before_attachments,
        })


@dataclass(frozen=True)
class UpdateEntitiesCommand:
    """Atomic multi-entity field update; revert restores the exact prior tuple."""

    before: tuple[SceneEntity, ...]
    after: tuple[SceneEntity, ...]
    presentation: CommandPresentation | None = None

    def __post_init__(self) -> None:
        if len(self.before) != len(self.after):
            raise EditStateError('entity update requires matched before/after tuples')

    @property
    def is_noop(self) -> bool:
        return self.before == self.after

    def _replace_all(self, document: SceneDocument, entities: tuple[SceneEntity, ...]) -> SceneDocument:
        by_id = {entity.entity_id: entity for entity in entities}
        replaced = tuple(by_id.get(entity.entity_id, entity) for entity in document.entities)
        return document.model_copy(update={'entities': replaced})

    def apply(self, document: SceneDocument) -> SceneDocument:
        return self._replace_all(document, self.after)

    def revert(self, document: SceneDocument) -> SceneDocument:
        return self._replace_all(document, self.before)


@dataclass(frozen=True)
class ReplaceDocumentCommand:
    """Exact whole-scene replacement used when one semantic operation changes topology."""

    before: SceneDocument
    after: SceneDocument
    presentation: CommandPresentation | None = None

    def __post_init__(self) -> None:
        if self.before.document_id != self.after.document_id:
            raise EditStateError('document replacement must preserve document_id')

    @property
    def is_noop(self) -> bool:
        return self.before == self.after

    def apply(self, document: SceneDocument) -> SceneDocument:
        if document != self.before:
            raise EditStateError('document replacement before state does not match')
        return self.after

    def revert(self, document: SceneDocument) -> SceneDocument:
        if document != self.after:
            raise EditStateError('document replacement after state does not match')
        return self.before


@dataclass
class EditorViewState:
    """Non-physical editor state. Selection order is stable; selected_id is the primary item."""

    selected_id: str | None = None
    selected_ids: list[str] = field(default_factory=list)
    hidden_ids: set[str] = field(default_factory=set)
    locked_ids: set[str] = field(default_factory=set)
    transform_mode: Literal['move', 'rotate'] = 'move'
    object_snap_enabled: bool = True
    grid_snap_enabled: bool = False
    grid_step_m: float = 0.05
    angle_snap_enabled: bool = False
    angle_step_deg: float = 15.0

    def __post_init__(self) -> None:
        if self.selected_id is not None and self.selected_id not in self.selected_ids:
            self.selected_ids.append(self.selected_id)
        self._dedupe_selection()
        if self.selected_id is None and self.selected_ids:
            self.selected_id = self.selected_ids[-1]

    @property
    def selection(self) -> tuple[str, ...]:
        return tuple(self.selected_ids)

    def set_selection(self, entity_ids: tuple[str, ...] | list[str], *, primary_id: str | None = None) -> None:
        self.selected_ids = list(entity_ids)
        self._dedupe_selection()
        if not self.selected_ids:
            self.selected_id = None
            return
        self.selected_id = primary_id if primary_id in self.selected_ids else self.selected_ids[-1]

    def select_only(self, entity_id: str | None) -> None:
        self.set_selection(() if entity_id is None else (entity_id,), primary_id=entity_id)

    def toggle_selected(self, entity_id: str) -> None:
        if entity_id in self.selected_ids:
            self.selected_ids.remove(entity_id)
            self.selected_id = self.selected_ids[-1] if self.selected_ids else None
        else:
            self.selected_ids.append(entity_id)
            self.selected_id = entity_id

    def is_selected(self, entity_id: str) -> bool:
        return entity_id in self.selected_ids

    def is_hidden(self, entity_id: str) -> bool:
        return entity_id in self.hidden_ids

    def is_locked(self, entity_id: str) -> bool:
        return entity_id in self.locked_ids

    def set_hidden(self, entity_id: str, hidden: bool) -> None:
        if hidden:
            self.hidden_ids.add(entity_id)
        else:
            self.hidden_ids.discard(entity_id)

    def set_locked(self, entity_id: str, locked: bool) -> None:
        if locked:
            self.locked_ids.add(entity_id)
        else:
            self.locked_ids.discard(entity_id)

    def sanitize(self, document: SceneDocument) -> None:
        valid = {entity.entity_id for entity in document.entities}
        self.hidden_ids.intersection_update(valid)
        self.locked_ids.intersection_update(valid)
        self.selected_ids = [entity_id for entity_id in self.selected_ids if entity_id in valid]
        self._dedupe_selection()
        if self.selected_id not in self.selected_ids:
            self.selected_id = self.selected_ids[-1] if self.selected_ids else None

    def _dedupe_selection(self) -> None:
        seen: set[str] = set()
        ordered: list[str] = []
        for entity_id in self.selected_ids:
            if entity_id not in seen:
                seen.add(entity_id)
                ordered.append(entity_id)
        self.selected_ids = ordered


class CommandHistory:
    def __init__(self) -> None:
        self._commands: list[EditCommand] = []
        self._index = 0

    @property
    def can_undo(self) -> bool:
        return self._index > 0

    @property
    def can_redo(self) -> bool:
        return self._index < len(self._commands)

    @property
    def length(self) -> int:
        return len(self._commands)

    @property
    def index(self) -> int:
        return self._index

    def undo_label(self) -> str | None:
        if not self.can_undo:
            return None
        return describe_command(self._commands[self._index - 1])

    def redo_label(self) -> str | None:
        if not self.can_redo:
            return None
        return describe_command(self._commands[self._index])

    def entries(self, *, limit: int | None = None) -> tuple[CommandHistoryEntry, ...]:
        """Bounded read-only history; index < current index means applied."""

        total = len(self._commands)
        start = 0 if limit is None else max(0, total - max(0, int(limit)))
        return tuple(
            CommandHistoryEntry(
                index=position,
                label=describe_command(self._commands[position]),
                applied=position < self._index,
            )
            for position in range(start, total)
        )

    @staticmethod
    def _resolved(document: SceneDocument) -> SceneDocument:
        """Project attachment authority into stored child positions (#661).

        A stored ``position`` on an attached entity is a derived cache, not
        free state: every history transition re-derives it so moved parents
        carry their children and no command can leave a stale projection.
        ``CommandHistory`` is exercised with duck-typed stand-ins too, so
        non-SceneDocument inputs pass through untouched.
        """

        if not isinstance(document, SceneDocument):
            return document
        return apply_attachments(document)

    def push(self, command: EditCommand, document: SceneDocument) -> SceneDocument:
        if command.is_noop:
            return document
        # Apply before mutating history: a command that fails its before-state
        # check must neither truncate the redo tail nor be recorded as applied.
        new_document = self._resolved(command.apply(document))
        self._commands = self._commands[: self._index]
        self._commands.append(command)
        self._index += 1
        return new_document

    def undo(self, document: SceneDocument) -> SceneDocument:
        if not self.can_undo:
            return document
        # Revert before moving the index: a failed revert must not consume
        # the command — it stays applied and undoable.
        new_document = self._resolved(self._commands[self._index - 1].revert(document))
        self._index -= 1
        return new_document

    def redo(self, document: SceneDocument) -> SceneDocument:
        if not self.can_redo:
            return document
        new_document = self._resolved(self._commands[self._index].apply(document))
        self._index += 1
        return new_document


class WorkingDocument:
    def __init__(self, document: SceneDocument, *, source_revision_id: str | None = None, saved_content_hash: str | None = None) -> None:
        self._document = document
        self._source_revision_id = source_revision_id
        self._saved_hash = saved_content_hash or self._content_hash()
        self._history = CommandHistory()
        self._preview_kind: Literal['move', 'rotate'] | None = None
        self._preview_entity_ids: tuple[str, ...] = ()
        self._preview_before_entities: tuple[SceneEntity, ...] = ()
        self._preview_document: SceneDocument | None = None

    @property
    def document(self) -> SceneDocument:
        return self._preview_document or self._document

    @property
    def committed_document(self) -> SceneDocument:
        return self._document

    @property
    def source_revision_id(self) -> str | None:
        return self._source_revision_id

    @property
    def saved_content_hash(self) -> str:
        return self._saved_hash

    @property
    def history_length(self) -> int:
        return self._history.length

    @property
    def can_undo(self) -> bool:
        return self._history.can_undo

    @property
    def can_redo(self) -> bool:
        return self._history.can_redo

    @property
    def has_preview(self) -> bool:
        return self._preview_document is not None

    @property
    def preview_kind(self) -> Literal['move', 'rotate'] | None:
        return self._preview_kind

    @property
    def is_dirty(self) -> bool:
        return self._content_hash() != self._saved_hash

    @property
    def _document(self) -> SceneDocument:
        return self._document_value

    @_document.setter
    def _document(self, document: SceneDocument) -> None:
        self._document_value = document
        self._content_hash_cache: str | None = None

    def _content_hash(self) -> str:
        """Canonical content hash of the committed document, memoized.

        ``SceneDocument`` is immutable and every mutation path reassigns
        ``self._document`` (which clears this cache via the setter), so the
        hash is computed at most once per committed state instead of on
        every ``is_dirty`` poll / edit boundary.
        """
        cached = self._content_hash_cache
        if cached is None:
            cached = scene_content_hash(self._document_value)
            self._content_hash_cache = cached
        return cached

    @property
    def undo_label(self) -> str | None:
        return self._history.undo_label()

    @property
    def redo_label(self) -> str | None:
        return self._history.redo_label()

    def history_entries(self, *, limit: int | None = None) -> tuple[CommandHistoryEntry, ...]:
        return self._history.entries(limit=limit)

    def _begin_preview(self, entity_ids: tuple[str, ...], kind: Literal['move', 'rotate']) -> tuple[SceneEntity, ...]:
        if self.has_preview:
            raise EditStateError('another preview is already active')
        if not entity_ids:
            raise EditStateError('preview requires at least one entity')
        if len(set(entity_ids)) != len(entity_ids):
            raise EditStateError('preview entity ids must be unique')
        entities = tuple(self._document.entity(entity_id) for entity_id in entity_ids)
        self._preview_kind = kind
        self._preview_entity_ids = entity_ids
        self._preview_before_entities = entities
        self._preview_document = self._document
        return entities

    def begin_move(self, entity_id: str) -> None:
        self._begin_preview((entity_id,), 'move')

    def begin_group_move(self, entity_ids: tuple[str, ...]) -> None:
        self._begin_preview(entity_ids, 'move')

    def preview_move(self, position: Position3) -> None:
        if not self.has_preview or self._preview_kind != 'move' or len(self._preview_before_entities) != 1:
            raise EditStateError('single-entity move preview has not started')
        replacement = self._preview_before_entities[0].model_copy(update={'position': position})
        # Attached children follow their parent in preview exactly as they
        # will on commit — preview must not lie about attachment authority.
        self._preview_document = apply_attachments(_replace_entities(self._document, (replacement,)))

    def preview_group_move(self, delta_xyz: tuple[float, float, float]) -> None:
        if not self.has_preview or self._preview_kind != 'move' or not self._preview_before_entities:
            raise EditStateError('group move preview has not started')
        dx, dy, dz = (float(value) for value in delta_xyz)
        replacements = tuple(
            entity.model_copy(update={'position': Position3(
                x_m=entity.position.x_m + dx,
                y_m=entity.position.y_m + dy,
                z_m=entity.position.z_m + dz,
            )})
            for entity in self._preview_before_entities
        )
        self._preview_document = apply_attachments(_replace_entities(self._document, replacements))

    def begin_rotate(self, entity_id: str) -> None:
        self._begin_preview((entity_id,), 'rotate')

    def begin_group_rotate(self, entity_ids: tuple[str, ...]) -> None:
        self._begin_preview(entity_ids, 'rotate')

    def preview_rotate(self, orientation: Quaternion4) -> None:
        if not self.has_preview or self._preview_kind != 'rotate' or len(self._preview_before_entities) != 1:
            raise EditStateError('single-entity rotate preview has not started')
        replacement = self._preview_before_entities[0].model_copy(update={'orientation': orientation})
        self._preview_document = apply_attachments(_replace_entities(self._document, (replacement,)))

    def preview_group_rotate(
        self,
        axis: Literal['x', 'y', 'z'],
        angle_deg: float,
        pivot: Position3,
    ) -> None:
        if not self.has_preview or self._preview_kind != 'rotate' or not self._preview_before_entities:
            raise EditStateError('group rotate preview has not started')
        replacements = tuple(
            entity.model_copy(update={
                'position': rotate_position_world(entity.position, pivot, axis, angle_deg),
                'orientation': rotate_orientation_world(entity.orientation, axis, angle_deg),
            })
            for entity in self._preview_before_entities
        )
        self._preview_document = apply_attachments(_replace_entities(self._document, replacements))

    def commit_preview(self) -> bool:
        if not self.has_preview or self._preview_document is None or not self._preview_before_entities:
            return False
        after = tuple(self._preview_document.entity(entity.entity_id) for entity in self._preview_before_entities)
        action = 'move' if self._preview_kind == 'move' else 'rotate'
        command: EditCommand = TransformEntitiesCommand(
            self._preview_before_entities,
            after,
            presentation=CommandPresentation(
                action=action,
                subject_names=tuple(entity.name for entity in self._preview_before_entities),
            ),
        )
        self._clear_preview()
        before_hash = self._content_hash()
        self._document = self._history.push(command, self._document)
        return self._content_hash() != before_hash

    def cancel_preview(self) -> bool:
        if not self.has_preview:
            return False
        self._clear_preview()
        return True

    def transform_entities(
        self,
        before: tuple[SceneEntity, ...],
        after: tuple[SceneEntity, ...],
        *,
        presentation: CommandPresentation | None = None,
    ) -> bool:
        """Apply an exact multi-entity replacement as one history command."""

        if self.has_preview:
            raise EditStateError('cannot transform entities while a preview is active')
        if not before:
            return False
        before_ids = tuple(entity.entity_id for entity in before)
        after_ids = tuple(entity.entity_id for entity in after)
        if len(set(before_ids)) != len(before_ids) or before_ids != after_ids:
            raise EditStateError('transform entities must preserve a unique ordered entity-id set')
        current = tuple(self._document.entity(entity_id) for entity_id in before_ids)
        if current != before:
            raise EditStateError('transform before state does not match the current document')
        command = TransformEntitiesCommand(
            before=before,
            after=after,
            presentation=presentation or CommandPresentation(
                action='transform',
                subject_names=tuple(entity.name for entity in before),
            ),
        )
        before_hash = self._content_hash()
        self._document = self._history.push(command, self._document)
        return self._content_hash() != before_hash

    def move_entity(self, entity_id: str, position: Position3) -> bool:
        if self.has_preview:
            raise EditStateError('cannot commit a numeric move while a preview is active')
        before_entity = self._document.entity(entity_id)
        before = before_entity.position
        command = MoveEntityCommand(
            entity_id,
            before,
            position,
            presentation=CommandPresentation(action='move', subject_names=(before_entity.name,)),
        )
        before_hash = self._content_hash()
        self._document = self._history.push(command, self._document)
        return self._content_hash() != before_hash

    def rotate_entity(self, entity_id: str, orientation: Quaternion4) -> bool:
        if self.has_preview:
            raise EditStateError('cannot commit a numeric rotation while a preview is active')
        before_entity = self._document.entity(entity_id)
        before = before_entity.orientation
        command = RotateEntityCommand(
            entity_id,
            before,
            orientation,
            presentation=CommandPresentation(action='rotate', subject_names=(before_entity.name,)),
        )
        before_hash = self._content_hash()
        self._document = self._history.push(command, self._document)
        return self._content_hash() != before_hash

    def add_entities(
        self,
        entities: tuple[SceneEntity, ...],
        *,
        index: int | None = None,
        presentation: CommandPresentation | None = None,
    ) -> bool:
        if self.has_preview:
            raise EditStateError('cannot add entities while a preview is active')
        if not entities:
            return False
        validated = tuple(SceneEntity.model_validate(entity.model_dump(mode='python')) for entity in entities)
        ids = [entity.entity_id for entity in validated]
        if len(ids) != len(set(ids)):
            raise EditStateError('added entity ids must be unique')
        existing = {entity.entity_id for entity in self._document.entities}
        duplicates = existing.intersection(ids)
        if duplicates:
            raise EditStateError(f'entities already exist: {sorted(duplicates)}')
        insertion_index = len(self._document.entities) if index is None else int(index)
        if not 0 <= insertion_index <= len(self._document.entities):
            raise EditStateError(f'entity insertion index out of range: {insertion_index}')
        before_hash = self._content_hash()
        self._document = self._history.push(
            AddEntitiesCommand(
                entities=validated,
                index=insertion_index,
                presentation=presentation or CommandPresentation(
                    action='add',
                    subject_names=tuple(entity.name for entity in validated),
                ),
            ),
            self._document,
        )
        return self._content_hash() != before_hash

    def add_entity(
        self,
        entity: SceneEntity,
        *,
        index: int | None = None,
        presentation: CommandPresentation | None = None,
    ) -> bool:
        return self.add_entities((entity,), index=index, presentation=presentation)

    def duplicate_entity(
        self,
        entity_id: str,
        *,
        new_entity_id: str,
        name: str | None = None,
        position: Position3 | None = None,
    ) -> bool:
        if self.has_preview:
            raise EditStateError('cannot duplicate an entity while a preview is active')
        source_index = next(index for index, entity in enumerate(self._document.entities) if entity.entity_id == entity_id)
        source = self._document.entities[source_index]
        payload = source.model_dump(mode='python')
        payload['entity_id'] = new_entity_id
        payload['name'] = name or f'{source.name} Copy'
        if position is not None:
            payload['position'] = position
        duplicate = SceneEntity.model_validate(payload)
        return self.add_entity(
            duplicate,
            index=source_index + 1,
            presentation=CommandPresentation(action='duplicate', subject_names=(source.name,)),
        )

    def update_entity(self, target_entity_id: str, **updates: Any) -> bool:
        if self.has_preview:
            raise EditStateError('cannot update an entity while a preview is active')
        if 'entity_id' in updates and updates['entity_id'] != target_entity_id:
            raise EditStateError('entity_id cannot be changed')
        before = self._document.entity(target_entity_id)
        payload = before.model_dump(mode='python')
        payload.update(updates)
        payload['entity_id'] = target_entity_id
        after = SceneEntity.model_validate(payload)
        before_hash = self._content_hash()
        self._document = self._history.push(
            ReplaceEntityCommand(
                before=before,
                after=after,
                presentation=CommandPresentation(
                    action='edit',
                    subject_names=(before.name,),
                    detail=_update_detail(before, after),
                ),
            ),
            self._document,
        )
        return self._content_hash() != before_hash

    def delete_entity(self, entity_id: str) -> bool:
        return self.delete_entities((entity_id,))

    def delete_entities(self, entity_ids: tuple[str, ...] | list[str]) -> bool:
        """Delete several entities as one Undo step, restoring exact index order.

        A group delete is one semantic operation: the batch records each removed
        entity with its original index so ``undo`` restores the exact prior
        entity list — the #480/#482 contract that batch delete remains Undo-safe.
        """
        if self.has_preview:
            raise EditStateError('cannot delete entities while a preview is active')
        unique_ids = tuple(dict.fromkeys(entity_ids))
        if not unique_ids:
            return False
        removed: list[tuple[int, SceneEntity]] = []
        for index, entity in enumerate(self._document.entities):
            if entity.entity_id in unique_ids:
                removed.append((index, entity))
        missing = set(unique_ids) - {entity.entity_id for _, entity in removed}
        if missing:
            raise EditStateError(f'entities not in the scene: {sorted(missing)}')
        # Removing a parent detaches its children: they land at their current
        # derived world pose instead of keeping a stale local projection, and
        # the edges are dropped so no dangling parent refs survive (#661).
        removed_id_set = frozenset(unique_ids)
        before_attachments = self._document.attachments
        orphaned_positions = tuple(
            (edge.child_entity_id, self._document.entity(edge.child_entity_id).position)
            for edge in (before_attachments or ())
            if edge.parent_entity_id in removed_id_set
            and edge.child_entity_id not in removed_id_set
        )
        before_hash = self._content_hash()
        self._document = self._history.push(
            DeleteEntitiesCommand(
                removed=tuple(removed),
                presentation=CommandPresentation(
                    action='delete',
                    subject_names=tuple(entity.name for _, entity in removed),
                ),
                before_attachments=before_attachments,
                orphaned_positions=orphaned_positions,
            ),
            self._document,
        )
        return self._content_hash() != before_hash

    def update_entities(self, updates: dict[str, dict[str, Any]]) -> bool:
        """Apply per-entity field updates as one Undo step (#480 batch edit).

        Only fields the caller deliberately allows are patched; every updated
        entity is re-validated, and the whole batch is atomic under one command.
        """
        if self.has_preview:
            raise EditStateError('cannot update entities while a preview is active')
        if not updates:
            return False
        before: list[SceneEntity] = []
        after: list[SceneEntity] = []
        for entity_id, entity_updates in updates.items():
            source = self._document.entity(entity_id)
            if 'entity_id' in entity_updates and entity_updates['entity_id'] != entity_id:
                raise EditStateError('entity_id cannot be changed')
            payload = source.model_dump(mode='python')
            payload.update(entity_updates)
            payload['entity_id'] = entity_id
            before.append(source)
            after.append(SceneEntity.model_validate(payload))
        before_hash = self._content_hash()
        self._document = self._history.push(
            UpdateEntitiesCommand(
                before=tuple(before),
                after=tuple(after),
                presentation=CommandPresentation(
                    action='edit',
                    subject_names=tuple(entity.name for entity in before),
                ),
            ),
            self._document,
        )
        return self._content_hash() != before_hash

    def replace_document(
        self,
        document: SceneDocument,
        *,
        presentation: CommandPresentation | None = None,
    ) -> bool:
        """Apply an exact same-document topology/state replacement as one Undo step."""

        if self.has_preview:
            raise EditStateError('cannot replace document while a preview is active')
        validated = SceneDocument.model_validate(document.model_dump(mode='python'))
        if validated.document_id != self._document.document_id:
            raise EditStateError('document replacement must preserve document_id')
        # Attached-child positions are derived state: store the resolved
        # projection so the command's exact-state checks stay consistent
        # with the normalized documents history produces.
        validated = apply_attachments(validated)
        before_hash = self._content_hash()
        self._document = self._history.push(
            ReplaceDocumentCommand(
                before=self._document,
                after=validated,
                presentation=presentation or CommandPresentation(action='replace_document'),
            ),
            self._document,
        )
        return self._content_hash() != before_hash

    def push_command(self, command: EditCommand) -> bool:
        """Push one arbitrary command through the shared undo history.

        Used by the workspace for composite commands that couple an entity
        edit with external design state (#843) — a state-only command
        reports success even when the document hash is unchanged.
        """

        if self.has_preview:
            raise EditStateError('cannot apply a command while a preview is active')
        if command.is_noop:
            return False
        self._document = self._history.push(command, self._document)
        return True

    def apply_entity_set_edit(
        self,
        *,
        removed: tuple[SceneEntity, ...] = (),
        replaced_before: tuple[SceneEntity, ...] = (),
        replaced_after: tuple[SceneEntity, ...] = (),
        added: tuple[SceneEntity, ...] = (),
        presentation: CommandPresentation | None = None,
        apply_side: Callable[[], None] | None = None,
        revert_side: Callable[[], None] | None = None,
    ) -> bool:
        """Apply one atomic mixed batch (removals + replacements + additions).

        ``apply_side``/``revert_side`` wrap the entity edit in a composite
        command so external design state joins the same Undo step (#843).
        """

        if self.has_preview:
            raise EditStateError('cannot apply a batched edit while a preview is active')
        index_of = {
            entity.entity_id: index for index, entity in enumerate(self._document.entities)
        }
        removed_id_set = frozenset(entity.entity_id for entity in removed)
        before_attachments = self._document.attachments
        orphaned_positions = tuple(
            (edge.child_entity_id, self._document.entity(edge.child_entity_id).position)
            for edge in (before_attachments or ())
            if edge.parent_entity_id in removed_id_set
            and edge.child_entity_id not in removed_id_set
            and edge.child_entity_id in index_of
        )
        inner = EntitySetEditCommand(
            removed=removed,
            replaced_before=replaced_before,
            replaced_after=replaced_after,
            added=added,
            presentation=presentation,
            removed_indices=tuple(index_of.get(entity.entity_id, -1) for entity in removed),
            before_attachments=before_attachments,
            orphaned_positions=orphaned_positions,
        )
        command: EditCommand = inner
        if apply_side is not None or revert_side is not None:
            command = CompositeEditCommand(
                inner=inner,
                apply_side=apply_side,
                revert_side=revert_side,
                presentation=presentation,
            )
        if command.is_noop:
            return False
        # Fail closed: every declared before-state must match the document.
        current = {entity.entity_id: entity for entity in self._document.entities}
        for snapshot in removed + replaced_before:
            if current.get(snapshot.entity_id) != snapshot:
                raise EditStateError('batched edit before state does not match the current document')
        overlapping = {entity.entity_id for entity in added} & current.keys()
        if overlapping:
            raise EditStateError(f'entities already exist: {sorted(overlapping)}')
        before_hash = self._content_hash()
        self._document = self._history.push(command, self._document)
        return (
            self._content_hash() != before_hash
            or command is not inner
        )

    def undo(self) -> bool:
        if self.has_preview:
            raise EditStateError('cancel the active preview before undo')
        # A consumed command may leave the document identical (constraint
        # state-only sidecar, #843) — report that the revert ran.
        index = self._history.index
        self._document = self._history.undo(self._document)
        return self._history.index != index

    def redo(self) -> bool:
        if self.has_preview:
            raise EditStateError('cancel the active preview before redo')
        index = self._history.index
        self._document = self._history.redo(self._document)
        return self._history.index != index

    def mark_saved(self, revision_id: str, content_hash: str) -> None:
        if self.has_preview:
            raise EditStateError('cannot mark a document saved while a preview is active')
        actual = self._content_hash()
        if actual != content_hash:
            raise EditStateError('saved content hash does not match the working document')
        self._source_revision_id = revision_id
        self._saved_hash = actual

    def _clear_preview(self) -> None:
        self._preview_kind = None
        self._preview_entity_ids = ()
        self._preview_before_entities = ()
        self._preview_document = None
