"""Append-only persistence for photometric/HDR authorities (#818).

* ``cad_photometric_profiles`` — immutable projector image performance
  profiles keyed by ``(document_id, profile_id, version)``.
* ``cad_photometric_profile_selections`` — append-only log of explicit
  current-photometric-profile selections per document.
* ``cad_screen_optical_profiles`` — immutable screen optical profiles.
* ``cad_screen_optical_selections`` — append-only current-profile log
  scoped to ``screen_entity_id``.
* ``cad_ambient_reflectance_profiles`` — immutable ambient reflectance
  profiles that pin the exact screen-optical triple they were derived
  against.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import sqlite3

from pydantic import BaseModel, ConfigDict, Field

from .cad_repository import SceneRepository
from .cad_schema import require_native_tables, connect_sqlite
from .cad_photometric import (
    AmbientReflectanceProfile,
    ProjectorImagePerformanceProfile,
    ScreenOpticalProfile,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PhotometricConflictError(ValueError):
    """A photometric save violated append-only identity rules."""


class PhotometricIntegrityError(ValueError):
    """A stored row disagreed with its payload or resolution contract."""


class PhotometricProfileSelection(BaseModel):
    """One recorded current-photometric-profile selection for a document."""

    model_config = ConfigDict(frozen=True)

    document_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    profile_sha256: str = Field(min_length=16)
    selected_at_utc: str = Field(min_length=1)


class ScreenOpticalSelection(BaseModel):
    """One recorded current screen-optical profile selection."""

    model_config = ConfigDict(frozen=True)

    document_id: str = Field(min_length=1)
    screen_entity_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    profile_sha256: str = Field(min_length=16)
    selected_at_utc: str = Field(min_length=1)


class CadPhotometricRepository:
    """Native storage for photometric/HDR profiles and selections."""

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
                'cad_photometric_profiles',
                'cad_photometric_profile_selections',
                'cad_screen_optical_profiles',
                'cad_screen_optical_selections',
                'cad_ambient_reflectance_profiles',
            )

    # ------------------------------------------------------------------
    # Projector image performance profiles

    def save_photometric_profile(
        self,
        profile: ProjectorImagePerformanceProfile,
        document_id: str,
    ) -> None:
        existing = self.get_photometric_profile(
            document_id, profile.profile_id, profile.version
        )
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise PhotometricConflictError(
                'photometric profile (document_id, profile_id, version) '
                'is append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_photometric_profiles (
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

    def get_photometric_profile(
        self,
        document_id: str,
        profile_id: str,
        version: str,
    ) -> ProjectorImagePerformanceProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT profile_id, version, profile_sha256, payload_json
                FROM cad_photometric_profiles
                WHERE document_id=? AND profile_id=? AND version=?
                """,
                (document_id, profile_id, version),
            ).fetchone()
        if row is None:
            return None
        return self._photometric_from_row(row)

    def get_photometric_profile_by_hash(
        self,
        profile_sha256: str,
    ) -> ProjectorImagePerformanceProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT profile_id, version, profile_sha256, payload_json
                FROM cad_photometric_profiles
                WHERE profile_sha256=?
                """,
                (profile_sha256,),
            ).fetchone()
        if row is None:
            return None
        return self._photometric_from_row(row)

    def list_photometric_profiles(
        self,
        document_id: str,
    ) -> tuple[ProjectorImagePerformanceProfile, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT profile_id, version, profile_sha256, payload_json
                FROM cad_photometric_profiles
                WHERE document_id=?
                ORDER BY created_at_utc, profile_id, version
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._photometric_from_row(row) for row in rows)

    def _photometric_from_row(
        self, row: sqlite3.Row
    ) -> ProjectorImagePerformanceProfile:
        profile = ProjectorImagePerformanceProfile.model_validate_json(
            row['payload_json']
        )
        if (
            profile.profile_id != row['profile_id']
            or profile.version != row['version']
            or profile.profile_sha256 != row['profile_sha256']
        ):
            raise PhotometricIntegrityError(
                'photometric profile row disagrees with its payload'
            )
        return profile

    # ------------------------------------------------------------------
    # Photometric selections

    def select_photometric_profile(
        self,
        document_id: str,
        profile: ProjectorImagePerformanceProfile,
        selected_at_utc: str | None = None,
    ) -> PhotometricProfileSelection:
        persisted = self.get_photometric_profile(
            document_id, profile.profile_id, profile.version
        )
        if (
            persisted is None
            or persisted.profile_sha256 != profile.profile_sha256
        ):
            raise PhotometricIntegrityError(
                'selection must reference a persisted photometric profile'
            )
        selection = PhotometricProfileSelection(
            document_id=document_id,
            profile_id=profile.profile_id,
            version=profile.version,
            profile_sha256=profile.profile_sha256,
            selected_at_utc=selected_at_utc or _utc_now(),
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_photometric_profile_selections (
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

    def current_photometric_selection(
        self,
        document_id: str,
    ) -> PhotometricProfileSelection | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT document_id, profile_id, version, profile_sha256,
                       selected_at_utc, payload_json
                FROM cad_photometric_profile_selections
                WHERE document_id=?
                ORDER BY selection_seq DESC
                LIMIT 1
                """,
                (document_id,),
            ).fetchone()
        if row is None:
            return None
        return self._photometric_selection_from_row(row)

    def list_photometric_selections(
        self,
        document_id: str,
    ) -> tuple[PhotometricProfileSelection, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT document_id, profile_id, version, profile_sha256,
                       selected_at_utc, payload_json
                FROM cad_photometric_profile_selections
                WHERE document_id=?
                ORDER BY selection_seq
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._photometric_selection_from_row(row) for row in rows)

    def current_photometric_profile(
        self,
        document_id: str,
    ) -> ProjectorImagePerformanceProfile | None:
        selection = self.current_photometric_selection(document_id)
        if selection is None:
            return None
        profile = self.get_photometric_profile(
            document_id, selection.profile_id, selection.version
        )
        if profile is None or profile.profile_sha256 != selection.profile_sha256:
            raise PhotometricIntegrityError(
                'current selection does not resolve to its persisted '
                'photometric profile'
            )
        return profile

    def _photometric_selection_from_row(
        self, row: sqlite3.Row
    ) -> PhotometricProfileSelection:
        selection = PhotometricProfileSelection.model_validate_json(
            row['payload_json']
        )
        if (
            selection.document_id != row['document_id']
            or selection.profile_id != row['profile_id']
            or selection.version != row['version']
            or selection.profile_sha256 != row['profile_sha256']
            or selection.selected_at_utc != row['selected_at_utc']
        ):
            raise PhotometricIntegrityError(
                'photometric profile selection row disagrees with its payload'
            )
        return selection

    # ------------------------------------------------------------------
    # Screen optical profiles

    def save_screen_optical_profile(
        self,
        profile: ScreenOpticalProfile,
        document_id: str,
    ) -> None:
        existing = self.get_screen_optical_profile(
            document_id, profile.profile_id, profile.version
        )
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise PhotometricConflictError(
                'screen optical profile (document_id, profile_id, version) '
                'is append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_screen_optical_profiles (
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

    def get_screen_optical_profile(
        self,
        document_id: str,
        profile_id: str,
        version: str,
    ) -> ScreenOpticalProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT profile_id, version, profile_sha256, payload_json
                FROM cad_screen_optical_profiles
                WHERE document_id=? AND profile_id=? AND version=?
                """,
                (document_id, profile_id, version),
            ).fetchone()
        if row is None:
            return None
        return self._screen_optical_from_row(row)

    def get_screen_optical_profile_by_hash(
        self,
        profile_sha256: str,
    ) -> ScreenOpticalProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT profile_id, version, profile_sha256, payload_json
                FROM cad_screen_optical_profiles
                WHERE profile_sha256=?
                """,
                (profile_sha256,),
            ).fetchone()
        if row is None:
            return None
        return self._screen_optical_from_row(row)

    def list_screen_optical_profiles(
        self,
        document_id: str,
    ) -> tuple[ScreenOpticalProfile, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT profile_id, version, profile_sha256, payload_json
                FROM cad_screen_optical_profiles
                WHERE document_id=?
                ORDER BY created_at_utc, profile_id, version
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._screen_optical_from_row(row) for row in rows)

    def _screen_optical_from_row(
        self, row: sqlite3.Row
    ) -> ScreenOpticalProfile:
        profile = ScreenOpticalProfile.model_validate_json(
            row['payload_json']
        )
        if (
            profile.profile_id != row['profile_id']
            or profile.version != row['version']
            or profile.profile_sha256 != row['profile_sha256']
        ):
            raise PhotometricIntegrityError(
                'screen optical profile row disagrees with its payload'
            )
        return profile

    # ------------------------------------------------------------------
    # Screen optical selections (scoped to the display surface)

    def select_screen_optical_profile(
        self,
        document_id: str,
        screen_entity_id: str,
        profile: ScreenOpticalProfile,
        selected_at_utc: str | None = None,
    ) -> ScreenOpticalSelection:
        persisted = self.get_screen_optical_profile(
            document_id, profile.profile_id, profile.version
        )
        if (
            persisted is None
            or persisted.profile_sha256 != profile.profile_sha256
        ):
            raise PhotometricIntegrityError(
                'selection must reference a persisted screen optical profile'
            )
        selection = ScreenOpticalSelection(
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
                INSERT INTO cad_screen_optical_selections (
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

    def current_screen_optical_selection(
        self,
        document_id: str,
        screen_entity_id: str,
    ) -> ScreenOpticalSelection | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT document_id, screen_entity_id, profile_id, version,
                       profile_sha256, selected_at_utc, payload_json
                FROM cad_screen_optical_selections
                WHERE document_id=? AND screen_entity_id=?
                ORDER BY selection_seq DESC
                LIMIT 1
                """,
                (document_id, screen_entity_id),
            ).fetchone()
        if row is None:
            return None
        return self._screen_optical_selection_from_row(row)

    def list_screen_optical_selections(
        self,
        document_id: str,
        screen_entity_id: str,
    ) -> tuple[ScreenOpticalSelection, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT document_id, screen_entity_id, profile_id, version,
                       profile_sha256, selected_at_utc, payload_json
                FROM cad_screen_optical_selections
                WHERE document_id=? AND screen_entity_id=?
                ORDER BY selection_seq
                """,
                (document_id, screen_entity_id),
            ).fetchall()
        return tuple(
            self._screen_optical_selection_from_row(row) for row in rows
        )

    def current_screen_optical_profile(
        self,
        document_id: str,
        screen_entity_id: str,
    ) -> ScreenOpticalProfile | None:
        selection = self.current_screen_optical_selection(
            document_id, screen_entity_id
        )
        if selection is None:
            return None
        profile = self.get_screen_optical_profile(
            document_id, selection.profile_id, selection.version
        )
        if profile is None or profile.profile_sha256 != selection.profile_sha256:
            raise PhotometricIntegrityError(
                'current selection does not resolve to its persisted '
                'screen optical profile'
            )
        return profile

    def _screen_optical_selection_from_row(
        self, row: sqlite3.Row
    ) -> ScreenOpticalSelection:
        selection = ScreenOpticalSelection.model_validate_json(
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
            raise PhotometricIntegrityError(
                'screen optical selection row disagrees with its payload'
            )
        return selection

    # ------------------------------------------------------------------
    # Ambient reflectance profiles

    def save_ambient_reflectance_profile(
        self,
        profile: AmbientReflectanceProfile,
        document_id: str,
    ) -> None:
        existing = self.get_ambient_reflectance_profile(
            document_id, profile.profile_id, profile.version
        )
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise PhotometricConflictError(
                'ambient reflectance profile (document_id, profile_id, '
                'version) is append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_ambient_reflectance_profiles (
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

    def get_ambient_reflectance_profile(
        self,
        document_id: str,
        profile_id: str,
        version: str,
    ) -> AmbientReflectanceProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT profile_id, version, profile_sha256, payload_json
                FROM cad_ambient_reflectance_profiles
                WHERE document_id=? AND profile_id=? AND version=?
                """,
                (document_id, profile_id, version),
            ).fetchone()
        if row is None:
            return None
        return self._ambient_reflectance_from_row(row)

    def list_ambient_reflectance_profiles(
        self,
        document_id: str,
    ) -> tuple[AmbientReflectanceProfile, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT profile_id, version, profile_sha256, payload_json
                FROM cad_ambient_reflectance_profiles
                WHERE document_id=?
                ORDER BY created_at_utc, profile_id, version
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            self._ambient_reflectance_from_row(row) for row in rows
        )

    def _ambient_reflectance_from_row(
        self, row: sqlite3.Row
    ) -> AmbientReflectanceProfile:
        profile = AmbientReflectanceProfile.model_validate_json(
            row['payload_json']
        )
        if (
            profile.profile_id != row['profile_id']
            or profile.version != row['version']
            or profile.profile_sha256 != row['profile_sha256']
        ):
            raise PhotometricIntegrityError(
                'ambient reflectance profile row disagrees with its payload'
            )
        return profile
