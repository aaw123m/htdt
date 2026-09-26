"""Append-only persistence for the source usable-output authority (#817).

* ``cad_usable_output_profiles`` — immutable versioned profiles keyed by
  ``(document_id, profile_id, version)`` with ``profile_sha256`` indexed.
* ``cad_usable_output_selections`` — append-only log of explicit
  current-profile selections, scoped to ``equipment_definition_id`` so
  each source equipment definition has its own current profile.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import sqlite3

from pydantic import BaseModel, ConfigDict, Field

from .cad_repository import SceneRepository
from .cad_schema import require_native_tables
from .cad_usable_output import SourceUsableOutputProfile


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class UsableOutputConflictError(ValueError):
    """A usable-output save violated append-only identity rules."""


class UsableOutputIntegrityError(ValueError):
    """A stored row disagreed with its payload or resolution contract."""


class UsableOutputSelection(BaseModel):
    """One recorded current-profile selection for an equipment definition."""

    model_config = ConfigDict(frozen=True)

    document_id: str = Field(min_length=1)
    equipment_definition_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    profile_sha256: str = Field(min_length=16)
    selected_at_utc: str = Field(min_length=1)


class CadUsableOutputRepository:
    """Native storage for usable-output profiles and scoped selections."""

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
                'cad_usable_output_profiles',
                'cad_usable_output_selections',
            )

    # ------------------------------------------------------------------
    # Profiles

    def save_profile(
        self,
        profile: SourceUsableOutputProfile,
        document_id: str,
    ) -> None:
        existing = self.get_profile(
            document_id, profile.profile_id, profile.version
        )
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise UsableOutputConflictError(
                'usable output profile (document_id, profile_id, version) '
                'is append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_usable_output_profiles (
                    document_id, profile_id, version, profile_sha256,
                    equipment_definition_id, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    profile.profile_id,
                    profile.version,
                    profile.profile_sha256,
                    profile.equipment_definition_id,
                    _utc_now(),
                    profile.model_dump_json(),
                ),
            )

    def get_profile(
        self,
        document_id: str,
        profile_id: str,
        version: str,
    ) -> SourceUsableOutputProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT profile_id, version, profile_sha256,
                       equipment_definition_id, payload_json
                FROM cad_usable_output_profiles
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
    ) -> SourceUsableOutputProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT profile_id, version, profile_sha256,
                       equipment_definition_id, payload_json
                FROM cad_usable_output_profiles
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
        equipment_definition_id: str | None = None,
    ) -> tuple[SourceUsableOutputProfile, ...]:
        with closing(self._connect()) as connection:
            if equipment_definition_id is None:
                rows = connection.execute(
                    """
                    SELECT profile_id, version, profile_sha256,
                           equipment_definition_id, payload_json
                    FROM cad_usable_output_profiles
                    WHERE document_id=?
                    ORDER BY created_at_utc, profile_id, version
                    """,
                    (document_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT profile_id, version, profile_sha256,
                           equipment_definition_id, payload_json
                    FROM cad_usable_output_profiles
                    WHERE document_id=? AND equipment_definition_id=?
                    ORDER BY created_at_utc, profile_id, version
                    """,
                    (document_id, equipment_definition_id),
                ).fetchall()
        return tuple(self._profile_from_row(row) for row in rows)

    def _profile_from_row(self, row: sqlite3.Row) -> SourceUsableOutputProfile:
        profile = SourceUsableOutputProfile.model_validate_json(
            row['payload_json']
        )
        if (
            profile.profile_id != row['profile_id']
            or profile.version != row['version']
            or profile.profile_sha256 != row['profile_sha256']
            or profile.equipment_definition_id != row['equipment_definition_id']
        ):
            raise UsableOutputIntegrityError(
                'usable output profile row disagrees with its payload'
            )
        return profile

    # ------------------------------------------------------------------
    # Scoped selections

    def select_profile(
        self,
        document_id: str,
        equipment_definition_id: str,
        profile: SourceUsableOutputProfile,
        selected_at_utc: str | None = None,
    ) -> UsableOutputSelection:
        persisted = self.get_profile(
            document_id, profile.profile_id, profile.version
        )
        if (
            persisted is None
            or persisted.profile_sha256 != profile.profile_sha256
        ):
            raise UsableOutputIntegrityError(
                'selection must reference a persisted usable output profile'
            )
        selection = UsableOutputSelection(
            document_id=document_id,
            equipment_definition_id=equipment_definition_id,
            profile_id=profile.profile_id,
            version=profile.version,
            profile_sha256=profile.profile_sha256,
            selected_at_utc=selected_at_utc or _utc_now(),
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_usable_output_selections (
                    document_id, equipment_definition_id, profile_id,
                    version, profile_sha256, selected_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    selection.document_id,
                    selection.equipment_definition_id,
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
        equipment_definition_id: str,
    ) -> UsableOutputSelection | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT document_id, equipment_definition_id, profile_id,
                       version, profile_sha256, selected_at_utc, payload_json
                FROM cad_usable_output_selections
                WHERE document_id=? AND equipment_definition_id=?
                ORDER BY selection_seq DESC
                LIMIT 1
                """,
                (document_id, equipment_definition_id),
            ).fetchone()
        if row is None:
            return None
        return self._selection_from_row(row)

    def list_selections(
        self,
        document_id: str,
        equipment_definition_id: str,
    ) -> tuple[UsableOutputSelection, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT document_id, equipment_definition_id, profile_id,
                       version, profile_sha256, selected_at_utc, payload_json
                FROM cad_usable_output_selections
                WHERE document_id=? AND equipment_definition_id=?
                ORDER BY selection_seq
                """,
                (document_id, equipment_definition_id),
            ).fetchall()
        return tuple(self._selection_from_row(row) for row in rows)

    def current_profile(
        self,
        document_id: str,
        equipment_definition_id: str,
    ) -> SourceUsableOutputProfile | None:
        selection = self.current_selection(document_id, equipment_definition_id)
        if selection is None:
            return None
        profile = self.get_profile(
            document_id, selection.profile_id, selection.version
        )
        if profile is None or profile.profile_sha256 != selection.profile_sha256:
            raise UsableOutputIntegrityError(
                'current selection does not resolve to its persisted profile'
            )
        return profile

    def _selection_from_row(self, row: sqlite3.Row) -> UsableOutputSelection:
        selection = UsableOutputSelection.model_validate_json(
            row['payload_json']
        )
        if (
            selection.document_id != row['document_id']
            or selection.equipment_definition_id != row['equipment_definition_id']
            or selection.profile_id != row['profile_id']
            or selection.version != row['version']
            or selection.profile_sha256 != row['profile_sha256']
            or selection.selected_at_utc != row['selected_at_utc']
        ):
            raise UsableOutputIntegrityError(
                'usable output selection row disagrees with its payload'
            )
        return selection
