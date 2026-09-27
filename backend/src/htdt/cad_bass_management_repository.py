"""Append-only persistence for the bass-management profile authority (#817).

Two authorities live here:

* ``cad_bass_management_profiles`` — immutable versioned profiles, keyed by
  ``(document_id, profile_id, version)`` with the semantic hash carried as an
  indexed column. Saves are append-only: re-saving an identical row is a
  no-op, a divergent hash for the same key is a conflict.
* ``cad_bass_management_selections`` — an append-only log of the project's
  explicit current-profile selections. ``current_selection`` returns the
  newest recorded selection; "current" is therefore an explicit persisted
  record, never a latest-created inference.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import sqlite3

from pydantic import BaseModel, ConfigDict, Field

from .cad_bass_management import BassManagementProfile
from .cad_repository import SceneRepository
from .cad_schema import require_native_tables, connect_sqlite


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BassManagementConflictError(ValueError):
    """A bass-management save violated append-only identity rules."""


class BassManagementIntegrityError(ValueError):
    """A stored row disagreed with its payload or resolution contract."""


class BassManagementSelection(BaseModel):
    """One recorded current-profile selection for a document."""

    model_config = ConfigDict(frozen=True)

    document_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    profile_sha256: str = Field(min_length=16)
    selected_at_utc: str = Field(min_length=1)


class CadBassManagementRepository:
    """Native storage for bass-management profiles and current selections."""

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
                'cad_bass_management_profiles',
                'cad_bass_management_selections',
            )

    # ------------------------------------------------------------------
    # Profiles

    def save_profile(
        self,
        profile: BassManagementProfile,
        document_id: str,
    ) -> None:
        existing = self.get_profile(
            document_id, profile.profile_id, profile.version
        )
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise BassManagementConflictError(
                'bass management profile (document_id, profile_id, version) is append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_bass_management_profiles (
                    document_id, profile_id, version, profile_sha256,
                    lifecycle, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    profile.profile_id,
                    profile.version,
                    profile.profile_sha256,
                    profile.lifecycle,
                    _utc_now(),
                    profile.model_dump_json(),
                ),
            )

    def get_profile(
        self,
        document_id: str,
        profile_id: str,
        version: str,
    ) -> BassManagementProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT profile_id, version, profile_sha256, lifecycle, payload_json
                FROM cad_bass_management_profiles
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
    ) -> BassManagementProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT profile_id, version, profile_sha256, lifecycle, payload_json
                FROM cad_bass_management_profiles
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
    ) -> tuple[BassManagementProfile, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT profile_id, version, profile_sha256, lifecycle, payload_json
                FROM cad_bass_management_profiles
                WHERE document_id=?
                ORDER BY created_at_utc, profile_id, version
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._profile_from_row(row) for row in rows)

    def _profile_from_row(self, row: sqlite3.Row) -> BassManagementProfile:
        profile = BassManagementProfile.model_validate_json(row['payload_json'])
        if (
            profile.profile_id != row['profile_id']
            or profile.version != row['version']
            or profile.profile_sha256 != row['profile_sha256']
            or profile.lifecycle != row['lifecycle']
        ):
            raise BassManagementIntegrityError(
                'bass management profile row disagrees with its payload'
            )
        return profile

    # ------------------------------------------------------------------
    # Current selections

    def select_profile(
        self,
        document_id: str,
        profile: BassManagementProfile,
        selected_at_utc: str | None = None,
    ) -> BassManagementSelection:
        persisted = self.get_profile(
            document_id, profile.profile_id, profile.version
        )
        if persisted is None or persisted.profile_sha256 != profile.profile_sha256:
            raise BassManagementIntegrityError(
                'selection must reference a persisted bass management profile'
            )
        selection = BassManagementSelection(
            document_id=document_id,
            profile_id=profile.profile_id,
            version=profile.version,
            profile_sha256=profile.profile_sha256,
            selected_at_utc=selected_at_utc or _utc_now(),
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_bass_management_selections (
                    document_id, profile_id, version, profile_sha256,
                    selected_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    selection.document_id,
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
    ) -> BassManagementSelection | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT document_id, profile_id, version, profile_sha256,
                       selected_at_utc, payload_json
                FROM cad_bass_management_selections
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
    ) -> tuple[BassManagementSelection, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT document_id, profile_id, version, profile_sha256,
                       selected_at_utc, payload_json
                FROM cad_bass_management_selections
                WHERE document_id=?
                ORDER BY selection_seq
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._selection_from_row(row) for row in rows)

    def current_profile(
        self,
        document_id: str,
    ) -> BassManagementProfile | None:
        selection = self.current_selection(document_id)
        if selection is None:
            return None
        profile = self.get_profile(
            document_id, selection.profile_id, selection.version
        )
        if profile is None or profile.profile_sha256 != selection.profile_sha256:
            raise BassManagementIntegrityError(
                'current selection does not resolve to its persisted profile'
            )
        return profile

    def _selection_from_row(self, row: sqlite3.Row) -> BassManagementSelection:
        selection = BassManagementSelection.model_validate_json(
            row['payload_json']
        )
        if (
            selection.document_id != row['document_id']
            or selection.profile_id != row['profile_id']
            or selection.version != row['version']
            or selection.profile_sha256 != row['profile_sha256']
            or selection.selected_at_utc != row['selected_at_utc']
        ):
            raise BassManagementIntegrityError(
                'bass management selection row disagrees with its payload'
            )
        return selection
