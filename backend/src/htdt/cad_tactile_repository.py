"""Append-only persistence for the tactile authority (#817).

Three authorities live here:

* ``cad_tactile_actuator_definitions`` — immutable actuator definitions
  keyed by ``(document_id, definition_id, version)``.
* ``cad_tactile_processing_profiles`` — immutable processing profiles
  keyed by ``(document_id, profile_id, version)``.
* ``cad_tactile_profile_selections`` — append-only log of the project's
  explicit current-processing-profile selections.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from pydantic import BaseModel, ConfigDict, Field

from .cad_repository import SceneRepository
from .cad_schema import require_native_tables, connect_sqlite
from .cad_tactile import (
    TactileActuatorDefinition,
    TactileProcessingProfile,
)
from .clock import utc_now_iso as _utc_now


class TactileConflictError(ValueError):
    """A tactile save violated append-only identity rules."""


class TactileIntegrityError(ValueError):
    """A stored row disagreed with its payload or resolution contract."""


class TactileProfileSelection(BaseModel):
    """One recorded current-profile selection for a document."""

    model_config = ConfigDict(frozen=True)

    document_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    profile_sha256: str = Field(min_length=16)
    selected_at_utc: str = Field(min_length=1)


class CadTactileRepository:
    """Native storage for tactile actuator definitions, processing
    profiles, and current-profile selections."""

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
                'cad_tactile_actuator_definitions',
                'cad_tactile_processing_profiles',
                'cad_tactile_profile_selections',
            )

    # ------------------------------------------------------------------
    # Actuator definitions

    def save_actuator_definition(
        self,
        definition: TactileActuatorDefinition,
        document_id: str,
    ) -> None:
        existing = self.get_actuator_definition(
            document_id, definition.definition_id, definition.version
        )
        if existing is not None:
            if existing.definition_sha256 == definition.definition_sha256:
                return
            raise TactileConflictError(
                'tactile actuator definition (document_id, definition_id, '
                'version) is append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_tactile_actuator_definitions (
                    document_id, definition_id, version, definition_sha256,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    definition.definition_id,
                    definition.version,
                    definition.definition_sha256,
                    _utc_now(),
                    definition.model_dump_json(),
                ),
            )

    def get_actuator_definition(
        self,
        document_id: str,
        definition_id: str,
        version: str,
    ) -> TactileActuatorDefinition | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT definition_id, version, definition_sha256, payload_json
                FROM cad_tactile_actuator_definitions
                WHERE document_id=? AND definition_id=? AND version=?
                """,
                (document_id, definition_id, version),
            ).fetchone()
        if row is None:
            return None
        return self._definition_from_row(row)

    def list_actuator_definitions(
        self,
        document_id: str,
    ) -> tuple[TactileActuatorDefinition, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT definition_id, version, definition_sha256, payload_json
                FROM cad_tactile_actuator_definitions
                WHERE document_id=?
                ORDER BY created_at_utc, definition_id, version
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._definition_from_row(row) for row in rows)

    def _definition_from_row(
        self, row: sqlite3.Row
    ) -> TactileActuatorDefinition:
        definition = TactileActuatorDefinition.model_validate_json(
            row['payload_json']
        )
        if (
            definition.definition_id != row['definition_id']
            or definition.version != row['version']
            or definition.definition_sha256 != row['definition_sha256']
        ):
            raise TactileIntegrityError(
                'tactile actuator definition row disagrees with its payload'
            )
        return definition

    # ------------------------------------------------------------------
    # Processing profiles

    def save_profile(
        self,
        profile: TactileProcessingProfile,
        document_id: str,
    ) -> None:
        existing = self.get_profile(
            document_id, profile.profile_id, profile.version
        )
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise TactileConflictError(
                'tactile processing profile (document_id, profile_id, '
                'version) is append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_tactile_processing_profiles (
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
    ) -> TactileProcessingProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT profile_id, version, profile_sha256, payload_json
                FROM cad_tactile_processing_profiles
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
    ) -> TactileProcessingProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT profile_id, version, profile_sha256, payload_json
                FROM cad_tactile_processing_profiles
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
    ) -> tuple[TactileProcessingProfile, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT profile_id, version, profile_sha256, payload_json
                FROM cad_tactile_processing_profiles
                WHERE document_id=?
                ORDER BY created_at_utc, profile_id, version
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._profile_from_row(row) for row in rows)

    def _profile_from_row(
        self, row: sqlite3.Row
    ) -> TactileProcessingProfile:
        profile = TactileProcessingProfile.model_validate_json(
            row['payload_json']
        )
        if (
            profile.profile_id != row['profile_id']
            or profile.version != row['version']
            or profile.profile_sha256 != row['profile_sha256']
        ):
            raise TactileIntegrityError(
                'tactile processing profile row disagrees with its payload'
            )
        return profile

    # ------------------------------------------------------------------
    # Current selections

    def select_profile(
        self,
        document_id: str,
        profile: TactileProcessingProfile,
        selected_at_utc: str | None = None,
    ) -> TactileProfileSelection:
        persisted = self.get_profile(
            document_id, profile.profile_id, profile.version
        )
        if (
            persisted is None
            or persisted.profile_sha256 != profile.profile_sha256
        ):
            raise TactileIntegrityError(
                'selection must reference a persisted tactile processing '
                'profile'
            )
        selection = TactileProfileSelection(
            document_id=document_id,
            profile_id=profile.profile_id,
            version=profile.version,
            profile_sha256=profile.profile_sha256,
            selected_at_utc=selected_at_utc or _utc_now(),
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_tactile_profile_selections (
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
    ) -> TactileProfileSelection | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT document_id, profile_id, version, profile_sha256,
                       selected_at_utc, payload_json
                FROM cad_tactile_profile_selections
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
    ) -> tuple[TactileProfileSelection, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT document_id, profile_id, version, profile_sha256,
                       selected_at_utc, payload_json
                FROM cad_tactile_profile_selections
                WHERE document_id=?
                ORDER BY selection_seq
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._selection_from_row(row) for row in rows)

    def current_profile(
        self,
        document_id: str,
    ) -> TactileProcessingProfile | None:
        selection = self.current_selection(document_id)
        if selection is None:
            return None
        profile = self.get_profile(
            document_id, selection.profile_id, selection.version
        )
        if profile is None or profile.profile_sha256 != selection.profile_sha256:
            raise TactileIntegrityError(
                'current selection does not resolve to its persisted profile'
            )
        return profile

    def _selection_from_row(self, row: sqlite3.Row) -> TactileProfileSelection:
        selection = TactileProfileSelection.model_validate_json(
            row['payload_json']
        )
        if (
            selection.document_id != row['document_id']
            or selection.profile_id != row['profile_id']
            or selection.version != row['version']
            or selection.profile_sha256 != row['profile_sha256']
            or selection.selected_at_utc != row['selected_at_utc']
        ):
            raise TactileIntegrityError(
                'tactile profile selection row disagrees with its payload'
            )
        return selection
