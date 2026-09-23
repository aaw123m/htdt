from __future__ import annotations

from .cad_document import EditStateError, TransformEntitiesCommand
from .cad_room import RoomWorkingDocument
from .cad_scene import SceneEntity, scene_content_hash


class TheaterWorkingDocument(RoomWorkingDocument):
    """Room-capable working document with atomic N40 multi-entity property edits."""

    def replace_entities(self, replacements: tuple[SceneEntity, ...]) -> bool:
        if self.has_preview:
            raise EditStateError('cannot replace entities while a preview is active')
        if not replacements:
            return False

        validated = tuple(
            SceneEntity.model_validate(entity.model_dump(mode='python'))
            for entity in replacements
        )
        ids = [entity.entity_id for entity in validated]
        if len(ids) != len(set(ids)):
            raise EditStateError('replacement entity ids must be unique')

        before = tuple(self._document.entity(entity_id) for entity_id in ids)
        command = TransformEntitiesCommand(before=before, after=validated)
        before_hash = scene_content_hash(self._document)
        self._document = self._history.push(command, self._document)
        return scene_content_hash(self._document) != before_hash
