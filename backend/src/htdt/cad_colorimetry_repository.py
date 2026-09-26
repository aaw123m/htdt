"""Append-only persistence for colorimetry evidence authorities (#818).

* ``cad_color_target_profiles`` — immutable versioned color target
  profiles keyed by ``(document_id, target_id, version)``.
* ``cad_color_target_selections`` — append-only log of explicit
  current-target-profile selections per document.
* ``cad_color_measurement_sets`` — immutable measurement evidence sets
  keyed by ``(document_id, measurement_set_id)`` and indexed by
  ``surface_entity_id`` so a display surface's evidence is looked up
  without payload parsing.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import sqlite3

from pydantic import BaseModel, ConfigDict, Field

from .cad_repository import SceneRepository
from .cad_schema import require_native_tables
from .cad_colorimetry import (
    VideoColorMeasurementSet,
    VideoColorTargetProfile,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ColorimetryConflictError(ValueError):
    """A colorimetry save violated append-only identity rules."""


class ColorimetryIntegrityError(ValueError):
    """A stored row disagreed with its payload or resolution contract."""


class ColorTargetSelection(BaseModel):
    """One recorded current-target-profile selection for a document."""

    model_config = ConfigDict(frozen=True)

    document_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    target_sha256: str = Field(min_length=16)
    selected_at_utc: str = Field(min_length=1)


class CadColorimetryRepository:
    """Native storage for color target profiles, selections, and
    measurement sets."""

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
                'cad_color_target_profiles',
                'cad_color_target_selections',
                'cad_color_measurement_sets',
            )

    # ------------------------------------------------------------------
    # Color target profiles

    def save_target_profile(
        self,
        profile: VideoColorTargetProfile,
        document_id: str,
    ) -> None:
        existing = self.get_target_profile(
            document_id, profile.target_id, profile.version
        )
        if existing is not None:
            if existing.target_sha256 == profile.target_sha256:
                return
            raise ColorimetryConflictError(
                'color target profile (document_id, target_id, version) '
                'is append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_color_target_profiles (
                    document_id, target_id, version, target_sha256,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    profile.target_id,
                    profile.version,
                    profile.target_sha256,
                    _utc_now(),
                    profile.model_dump_json(),
                ),
            )

    def get_target_profile(
        self,
        document_id: str,
        target_id: str,
        version: str,
    ) -> VideoColorTargetProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT target_id, version, target_sha256, payload_json
                FROM cad_color_target_profiles
                WHERE document_id=? AND target_id=? AND version=?
                """,
                (document_id, target_id, version),
            ).fetchone()
        if row is None:
            return None
        return self._target_from_row(row)

    def get_target_profile_by_hash(
        self,
        target_sha256: str,
    ) -> VideoColorTargetProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT target_id, version, target_sha256, payload_json
                FROM cad_color_target_profiles
                WHERE target_sha256=?
                """,
                (target_sha256,),
            ).fetchone()
        if row is None:
            return None
        return self._target_from_row(row)

    def list_target_profiles(
        self,
        document_id: str,
    ) -> tuple[VideoColorTargetProfile, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT target_id, version, target_sha256, payload_json
                FROM cad_color_target_profiles
                WHERE document_id=?
                ORDER BY created_at_utc, target_id, version
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._target_from_row(row) for row in rows)

    def _target_from_row(self, row: sqlite3.Row) -> VideoColorTargetProfile:
        profile = VideoColorTargetProfile.model_validate_json(
            row['payload_json']
        )
        if (
            profile.target_id != row['target_id']
            or profile.version != row['version']
            or profile.target_sha256 != row['target_sha256']
        ):
            raise ColorimetryIntegrityError(
                'color target profile row disagrees with its payload'
            )
        return profile

    # ------------------------------------------------------------------
    # Target selections

    def select_target_profile(
        self,
        document_id: str,
        profile: VideoColorTargetProfile,
        selected_at_utc: str | None = None,
    ) -> ColorTargetSelection:
        persisted = self.get_target_profile(
            document_id, profile.target_id, profile.version
        )
        if (
            persisted is None
            or persisted.target_sha256 != profile.target_sha256
        ):
            raise ColorimetryIntegrityError(
                'selection must reference a persisted color target profile'
            )
        selection = ColorTargetSelection(
            document_id=document_id,
            target_id=profile.target_id,
            version=profile.version,
            target_sha256=profile.target_sha256,
            selected_at_utc=selected_at_utc or _utc_now(),
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_color_target_selections (
                    document_id, target_id, version, target_sha256,
                    selected_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    selection.document_id,
                    selection.target_id,
                    selection.version,
                    selection.target_sha256,
                    selection.selected_at_utc,
                    selection.model_dump_json(),
                ),
            )
        return selection

    def current_selection(
        self,
        document_id: str,
    ) -> ColorTargetSelection | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT document_id, target_id, version, target_sha256,
                       selected_at_utc, payload_json
                FROM cad_color_target_selections
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
    ) -> tuple[ColorTargetSelection, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT document_id, target_id, version, target_sha256,
                       selected_at_utc, payload_json
                FROM cad_color_target_selections
                WHERE document_id=?
                ORDER BY selection_seq
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._selection_from_row(row) for row in rows)

    def current_target_profile(
        self,
        document_id: str,
    ) -> VideoColorTargetProfile | None:
        selection = self.current_selection(document_id)
        if selection is None:
            return None
        profile = self.get_target_profile(
            document_id, selection.target_id, selection.version
        )
        if profile is None or profile.target_sha256 != selection.target_sha256:
            raise ColorimetryIntegrityError(
                'current selection does not resolve to its persisted '
                'color target profile'
            )
        return profile

    def _selection_from_row(self, row: sqlite3.Row) -> ColorTargetSelection:
        selection = ColorTargetSelection.model_validate_json(
            row['payload_json']
        )
        if (
            selection.document_id != row['document_id']
            or selection.target_id != row['target_id']
            or selection.version != row['version']
            or selection.target_sha256 != row['target_sha256']
            or selection.selected_at_utc != row['selected_at_utc']
        ):
            raise ColorimetryIntegrityError(
                'color target selection row disagrees with its payload'
            )
        return selection

    # ------------------------------------------------------------------
    # Measurement sets

    def save_measurement_set(
        self,
        measurement_set: VideoColorMeasurementSet,
        document_id: str,
    ) -> None:
        existing = self.get_measurement_set(
            document_id, measurement_set.measurement_set_id
        )
        if existing is not None:
            if (
                existing.measurement_set_sha256
                == measurement_set.measurement_set_sha256
            ):
                return
            raise ColorimetryConflictError(
                'color measurement set (document_id, measurement_set_id) '
                'is append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_color_measurement_sets (
                    document_id, measurement_set_id, surface_entity_id,
                    measurement_set_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    measurement_set.measurement_set_id,
                    measurement_set.surface_entity_id,
                    measurement_set.measurement_set_sha256,
                    _utc_now(),
                    measurement_set.model_dump_json(),
                ),
            )

    def get_measurement_set(
        self,
        document_id: str,
        measurement_set_id: str,
    ) -> VideoColorMeasurementSet | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT measurement_set_id, surface_entity_id,
                       measurement_set_sha256, payload_json
                FROM cad_color_measurement_sets
                WHERE document_id=? AND measurement_set_id=?
                """,
                (document_id, measurement_set_id),
            ).fetchone()
        if row is None:
            return None
        return self._measurement_set_from_row(row)

    def list_measurement_sets(
        self,
        document_id: str,
        surface_entity_id: str | None = None,
    ) -> tuple[VideoColorMeasurementSet, ...]:
        with closing(self._connect()) as connection:
            if surface_entity_id is None:
                rows = connection.execute(
                    """
                    SELECT measurement_set_id, surface_entity_id,
                           measurement_set_sha256, payload_json
                    FROM cad_color_measurement_sets
                    WHERE document_id=?
                    ORDER BY created_at_utc, measurement_set_id
                    """,
                    (document_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT measurement_set_id, surface_entity_id,
                           measurement_set_sha256, payload_json
                    FROM cad_color_measurement_sets
                    WHERE document_id=? AND surface_entity_id=?
                    ORDER BY created_at_utc, measurement_set_id
                    """,
                    (document_id, surface_entity_id),
                ).fetchall()
        return tuple(self._measurement_set_from_row(row) for row in rows)

    def _measurement_set_from_row(
        self, row: sqlite3.Row
    ) -> VideoColorMeasurementSet:
        measurement_set = VideoColorMeasurementSet.model_validate_json(
            row['payload_json']
        )
        if (
            measurement_set.measurement_set_id != row['measurement_set_id']
            or measurement_set.surface_entity_id != row['surface_entity_id']
            or measurement_set.measurement_set_sha256
            != row['measurement_set_sha256']
        ):
            raise ColorimetryIntegrityError(
                'color measurement set row disagrees with its payload'
            )
        return measurement_set
