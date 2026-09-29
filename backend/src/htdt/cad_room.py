from __future__ import annotations

from dataclasses import dataclass

from .cad_document import CommandPresentation, EditStateError, WorkingDocument
from .cad_scene import RoomPrism, SceneDocument
from .cad_wall_models import WallTopology
from .physical_attachment import apply_attachments


@dataclass(frozen=True)
class ReplaceRoomCommand:
    """Replace room-related state atomically while preserving the rest of the scene snapshot."""

    before: SceneDocument
    after: SceneDocument
    presentation: CommandPresentation | None = None

    @property
    def is_noop(self) -> bool:
        return self.before == self.after

    def apply(self, document: SceneDocument) -> SceneDocument:
        if document != self.before:
            raise EditStateError('room command base document changed before apply')
        return self.after

    def revert(self, document: SceneDocument) -> SceneDocument:
        if document != self.after:
            raise EditStateError('room command document changed before revert')
        return self.before


class RoomWorkingDocument(WorkingDocument):
    """WorkingDocument extension used by N30 room/wall tools.

    Provisional geometry stays in the active tool. Only a fully validated room
    snapshot, including wall topology when present, reaches command history.
    """

    def replace_room(self, room: RoomPrism | None) -> bool:
        if self.has_preview:
            raise EditStateError('cannot edit the room while an entity transform preview is active')
        before = self.committed_document
        after = before.model_copy(
            update={
                'schema_version': max(2, before.schema_version),
                'room': room,
            }
        )
        return self._commit_room_snapshot(before, after, label='部屋形状を変更')

    def replace_room_topology(self, room: RoomPrism, topology: WallTopology) -> bool:
        """Commit geometry + wall/opening references as one Undoable transaction."""

        if self.has_preview:
            raise EditStateError('cannot edit walls while an entity transform preview is active')
        before = self.committed_document
        after = before.model_copy(
            update={
                'schema_version': max(3, before.schema_version),
                'room': room,
                'wall_topology': topology,
            }
        )
        return self._commit_room_snapshot(before, after, label='壁・開口を編集')

    def _commit_room_snapshot(
        self,
        before: SceneDocument,
        after: SceneDocument,
        *,
        label: str,
    ) -> bool:
        # model_copy(update=...) does not revalidate by design; validate the exact
        # snapshot that will become part of history before it can be committed.
        # ValidationError propagates as ValueError — the documented fail-closed
        # surface callers already handle.
        validated = SceneDocument.model_validate(after.model_dump(mode='python'))
        # Attached-child positions are derived state: store the resolved
        # projection so the command's exact-state checks stay consistent.
        validated = apply_attachments(validated)
        before_hash = self._content_hash()
        self._document = self._history.push(
            ReplaceRoomCommand(
                before,
                validated,
                presentation=CommandPresentation(action='edit', label=label),
            ),
            self._document,
        )
        return self._content_hash() != before_hash
