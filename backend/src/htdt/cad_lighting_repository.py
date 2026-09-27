"""Append-only persistence for the lighting scene authority (#817).

* ``cad_lighting_scenes`` — immutable versioned scenes keyed by
  ``(document_id, scene_id, version)`` with ``scene_sha256`` indexed.
* ``cad_lighting_scene_selections`` — append-only log of the project's
  explicit current-scene selections.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from pydantic import BaseModel, ConfigDict, Field

from .cad_repository import SceneRepository
from .cad_schema import require_native_tables, connect_sqlite
from .cad_lighting import LightingScene
from .clock import utc_now_iso as _utc_now


class LightingConflictError(ValueError):
    """A lighting-scene save violated append-only identity rules."""


class LightingIntegrityError(ValueError):
    """A stored row disagreed with its payload or resolution contract."""


class LightingSceneSelection(BaseModel):
    """One recorded current-scene selection for a document."""

    model_config = ConfigDict(frozen=True)

    document_id: str = Field(min_length=1)
    scene_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    scene_sha256: str = Field(min_length=16)
    selected_at_utc: str = Field(min_length=1)


class CadLightingRepository:
    """Native storage for lighting scenes and current-scene selections."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_lighting_scenes',
                'cad_lighting_scene_selections',
            )

    # ------------------------------------------------------------------
    # Lighting scenes

    def save_scene(
        self,
        scene: LightingScene,
        document_id: str,
    ) -> None:
        existing = self.get_scene(document_id, scene.scene_id, scene.version)
        if existing is not None:
            if existing.scene_sha256 == scene.scene_sha256:
                return
            raise LightingConflictError(
                'lighting scene (document_id, scene_id, version) is append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_lighting_scenes (
                    document_id, scene_id, version, scene_sha256,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    scene.scene_id,
                    scene.version,
                    scene.scene_sha256,
                    _utc_now(),
                    scene.model_dump_json(),
                ),
            )

    def get_scene(
        self,
        document_id: str,
        scene_id: str,
        version: str,
    ) -> LightingScene | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT scene_id, version, scene_sha256, payload_json
                FROM cad_lighting_scenes
                WHERE document_id=? AND scene_id=? AND version=?
                """,
                (document_id, scene_id, version),
            ).fetchone()
        if row is None:
            return None
        return self._scene_from_row(row)

    def get_scene_by_hash(
        self,
        scene_sha256: str,
    ) -> LightingScene | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT scene_id, version, scene_sha256, payload_json
                FROM cad_lighting_scenes
                WHERE scene_sha256=?
                """,
                (scene_sha256,),
            ).fetchone()
        if row is None:
            return None
        return self._scene_from_row(row)

    def list_scenes(
        self,
        document_id: str,
    ) -> tuple[LightingScene, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT scene_id, version, scene_sha256, payload_json
                FROM cad_lighting_scenes
                WHERE document_id=?
                ORDER BY created_at_utc, scene_id, version
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._scene_from_row(row) for row in rows)

    def _scene_from_row(self, row: sqlite3.Row) -> LightingScene:
        scene = LightingScene.model_validate_json(row['payload_json'])
        if (
            scene.scene_id != row['scene_id']
            or scene.version != row['version']
            or scene.scene_sha256 != row['scene_sha256']
        ):
            raise LightingIntegrityError(
                'lighting scene row disagrees with its payload'
            )
        return scene

    # ------------------------------------------------------------------
    # Current selections

    def select_scene(
        self,
        document_id: str,
        scene: LightingScene,
        selected_at_utc: str | None = None,
    ) -> LightingSceneSelection:
        persisted = self.get_scene(document_id, scene.scene_id, scene.version)
        if persisted is None or persisted.scene_sha256 != scene.scene_sha256:
            raise LightingIntegrityError(
                'selection must reference a persisted lighting scene'
            )
        selection = LightingSceneSelection(
            document_id=document_id,
            scene_id=scene.scene_id,
            version=scene.version,
            scene_sha256=scene.scene_sha256,
            selected_at_utc=selected_at_utc or _utc_now(),
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_lighting_scene_selections (
                    document_id, scene_id, version, scene_sha256,
                    selected_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    selection.document_id,
                    selection.scene_id,
                    selection.version,
                    selection.scene_sha256,
                    selection.selected_at_utc,
                    selection.model_dump_json(),
                ),
            )
        return selection

    def current_selection(
        self,
        document_id: str,
    ) -> LightingSceneSelection | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT document_id, scene_id, version, scene_sha256,
                       selected_at_utc, payload_json
                FROM cad_lighting_scene_selections
                WHERE document_id=?
                ORDER BY selection_seq DESC
                LIMIT 1
                """,
                (document_id,),
            ).fetchone()
        if row is None:
            return None
        return self._selection_from_row(row)

    def list_selections(
        self,
        document_id: str,
    ) -> tuple[LightingSceneSelection, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT document_id, scene_id, version, scene_sha256,
                       selected_at_utc, payload_json
                FROM cad_lighting_scene_selections
                WHERE document_id=?
                ORDER BY selection_seq
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._selection_from_row(row) for row in rows)

    def current_scene(
        self,
        document_id: str,
    ) -> LightingScene | None:
        selection = self.current_selection(document_id)
        if selection is None:
            return None
        scene = self.get_scene(
            document_id, selection.scene_id, selection.version
        )
        if scene is None or scene.scene_sha256 != selection.scene_sha256:
            raise LightingIntegrityError(
                'current selection does not resolve to its persisted scene'
            )
        return scene

    def _selection_from_row(self, row: sqlite3.Row) -> LightingSceneSelection:
        selection = LightingSceneSelection.model_validate_json(
            row['payload_json']
        )
        if (
            selection.document_id != row['document_id']
            or selection.scene_id != row['scene_id']
            or selection.version != row['version']
            or selection.scene_sha256 != row['scene_sha256']
            or selection.selected_at_utc != row['selected_at_utc']
        ):
            raise LightingIntegrityError(
                'lighting scene selection row disagrees with its payload'
            )
        return selection
