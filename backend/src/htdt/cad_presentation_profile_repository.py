"""Append-only persistence for the video presentation-profile authority (#818).

Two authorities live here:

* ``cad_video_presentation_profiles`` — immutable versioned profiles, keyed by
  ``(document_id, profile_id, version)`` with the semantic hash carried as an
  indexed column. One physical screen may retain several immutable profiles;
  saves are append-only (identical re-save is a no-op, divergent hash is a
  conflict).
* ``cad_video_presentation_selections`` — an append-only log of the project's
  explicit current-profile selections, each bound to a screen entity.
  ``current_selection`` returns the newest recorded selection: "current" is an
  explicit persisted record, never a latest-created inference, and switching
  profiles leaves prior selections (and their evaluations) untouched.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import sqlite3

from pydantic import BaseModel, ConfigDict, Field

from .cad_presentation_profile import VideoPresentationProfile
from .cad_repository import SceneRepository
from .cad_schema import require_native_tables


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PresentationProfileConflictError(ValueError):
    """A presentation-profile save violated append-only identity rules."""


class PresentationProfileIntegrityError(ValueError):
    """A stored row disagreed with its payload or resolution contract."""


class PresentationProfileSelection(BaseModel):
    """One recorded current-profile selection for a screen in a document."""

    model_config = ConfigDict(frozen=True)

    document_id: str = Field(min_length=1)
    screen_entity_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    profile_sha256: str = Field(min_length=16)
    selected_at_utc: str = Field(min_length=1)


class CadPresentationProfileRepository:
    """Native storage for presentation profiles and current selections."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_video_presentation_profiles',
                'cad_video_presentation_selections',
            )

    # ------------------------------------------------------------------
    # Profiles

    def save_profile(
        self,
        profile: VideoPresentationProfile,
        document_id: str,
    ) -> None:
        existing = self.get_profile(
            document_id, profile.profile_id, profile.version
        )
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise PresentationProfileConflictError(
                'presentation profile (document_id, profile_id, version) is append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_video_presentation_profiles (
                    document_id, profile_id, version, profile_sha256,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    profile.profile_id,
                    profile.version,
                    profile.profile_sha256,
                    _utc_now(),
                    profile.model_dump_json(),
                ),
            )

    def get_profile(
        self,
        document_id: str,
        profile_id: str,
        version: str,
    ) -> VideoPresentationProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT profile_id, version, profile_sha256, payload_json
                FROM cad_video_presentation_profiles
                WHERE document_id=? AND profile_id=? AND version=?
                """,
                (document_id, profile_id, version),
            ).fetchone()
        if row is None:
            return None
        return self._profile_from_row(row)

    def get_profile_by_hash(
        self,
        profile_sha256: str,
    ) -> VideoPresentationProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT profile_id, version, profile_sha256, payload_json
                FROM cad_video_presentation_profiles
                WHERE profile_sha256=?
                """,
                (profile_sha256,),
            ).fetchone()
        if row is None:
            return None
        return self._profile_from_row(row)

    def list_profiles(
        self,
        document_id: str,
    ) -> tuple[VideoPresentationProfile, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT profile_id, version, profile_sha256, payload_json
                FROM cad_video_presentation_profiles
                WHERE document_id=?
                ORDER BY created_at_utc, profile_id, version
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._profile_from_row(row) for row in rows)

    def _profile_from_row(self, row: sqlite3.Row) -> VideoPresentationProfile:
        profile = VideoPresentationProfile.model_validate_json(
            row['payload_json']
        )
        if (
            profile.profile_id != row['profile_id']
            or profile.version != row['version']
            or profile.profile_sha256 != row['profile_sha256']
        ):
            raise PresentationProfileIntegrityError(
                'presentation profile row disagrees with its payload'
            )
        return profile

    # ------------------------------------------------------------------
    # Current selections

    def select_profile(
        self,
        document_id: str,
        screen_entity_id: str,
        profile: VideoPresentationProfile,
        selected_at_utc: str | None = None,
    ) -> PresentationProfileSelection:
        persisted = self.get_profile(
            document_id, profile.profile_id, profile.version
        )
        if persisted is None or persisted.profile_sha256 != profile.profile_sha256:
            raise PresentationProfileIntegrityError(
                'selection must reference a persisted presentation profile'
            )
        selection = PresentationProfileSelection(
            document_id=document_id,
            screen_entity_id=screen_entity_id,
            profile_id=profile.profile_id,
            version=profile.version,
            profile_sha256=profile.profile_sha256,
            selected_at_utc=selected_at_utc or _utc_now(),
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_video_presentation_selections (
                    document_id, screen_entity_id, profile_id, version,
                    profile_sha256, selected_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    selection.document_id,
                    selection.screen_entity_id,
                    selection.profile_id,
                    selection.version,
                    selection.profile_sha256,
                    selection.selected_at_utc,
                    selection.model_dump_json(),
                ),
            )
        return selection

    def current_selection(
        self,
        document_id: str,
    ) -> PresentationProfileSelection | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT document_id, screen_entity_id, profile_id, version,
                       profile_sha256, selected_at_utc, payload_json
                FROM cad_video_presentation_selections
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
    ) -> tuple[PresentationProfileSelection, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT document_id, screen_entity_id, profile_id, version,
                       profile_sha256, selected_at_utc, payload_json
                FROM cad_video_presentation_selections
                WHERE document_id=?
                ORDER BY selection_seq
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._selection_from_row(row) for row in rows)

    def current_profile(
        self,
        document_id: str,
    ) -> VideoPresentationProfile | None:
        selection = self.current_selection(document_id)
        if selection is None:
            return None
        profile = self.get_profile(
            document_id, selection.profile_id, selection.version
        )
        if profile is None or profile.profile_sha256 != selection.profile_sha256:
            raise PresentationProfileIntegrityError(
                'current selection does not resolve to its persisted profile'
            )
        return profile

    def _selection_from_row(
        self, row: sqlite3.Row
    ) -> PresentationProfileSelection:
        selection = PresentationProfileSelection.model_validate_json(
            row['payload_json']
        )
        if (
            selection.document_id != row['document_id']
            or selection.screen_entity_id != row['screen_entity_id']
            or selection.profile_id != row['profile_id']
            or selection.version != row['version']
            or selection.profile_sha256 != row['profile_sha256']
            or selection.selected_at_utc != row['selected_at_utc']
        ):
            raise PresentationProfileIntegrityError(
                'presentation profile selection row disagrees with its payload'
            )
        return selection
