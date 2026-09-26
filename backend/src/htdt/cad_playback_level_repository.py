"""Append-only persistence for playback-level authorities (#733)."""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import TYPE_CHECKING

from .cad_playback_level import PlaybackLevelCondition, ReferencePlaybackProfile
from .cad_repository import SceneRepository
from .cad_schema import require_native_tables

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .cad_measurement_quality_repository import CadMeasurementQualityRepository
    from .cad_measurement_repository import CadMeasurementRepository


class PlaybackLevelConflictError(ValueError):
    """A condition/profile save violated append-only identity rules."""


class CadPlaybackLevelRepository:
    """Native storage for PlaybackLevelCondition + ReferencePlaybackProfile.

    Both authorities are append-only: a condition records one exact
    composition of program/device/acoustic evidence; a profile records one
    versioned reference target. Neither is ever updated in place.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        *,
        measurement_repository: 'CadMeasurementRepository | None' = None,
        measurement_quality_repository: 'CadMeasurementQualityRepository | None' = None,
    ) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        self._measurement_repository = measurement_repository
        self._measurement_quality_repository = measurement_quality_repository
        self._initialize()

    def _measurement_repositories(
        self,
    ) -> tuple['CadMeasurementRepository', 'CadMeasurementQualityRepository']:
        if self._measurement_repository is None:
            from .cad_measurement_repository import CadMeasurementRepository

            self._measurement_repository = CadMeasurementRepository(
                self.scene_repository
            )
        if self._measurement_quality_repository is None:
            from .cad_measurement_quality_repository import (
                CadMeasurementQualityRepository,
            )

            self._measurement_quality_repository = CadMeasurementQualityRepository(
                self._measurement_repository
            )
        return self._measurement_repository, self._measurement_quality_repository

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        # #767: persistent schema is owned by the migration authority;
        # repositories verify the migrated contract, never converge it.
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_playback_level_conditions', 'cad_reference_playback_profiles')

    def save_profile(self, profile: ReferencePlaybackProfile) -> None:
        if self.get_profile(profile.profile_id, profile.version) is not None:
            raise PlaybackLevelConflictError(
                'ReferencePlaybackProfile (profile_id, version) is append-only'
            )
        if (
            profile.document_id is not None
            and self.scene_repository.latest(profile.document_id) is None
        ):
            raise ValueError(
                'profile pins a document with no persisted SceneRevision'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_reference_playback_profiles (
                    profile_id, version, document_id, semantic_sha256,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.version,
                    profile.document_id,
                    profile.semantic_sha256,
                    profile.created_at_utc,
                    profile.model_dump_json(),
                ),
            )

    def get_profile(
        self, profile_id: str, version: str
    ) -> ReferencePlaybackProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_reference_playback_profiles
                WHERE profile_id=? AND version=?
                """,
                (profile_id, version),
            ).fetchone()
        if row is None:
            return None
        return ReferencePlaybackProfile.model_validate_json(row['payload_json'])

    def get_profile_by_hash(
        self, semantic_sha256: str
    ) -> ReferencePlaybackProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_reference_playback_profiles
                WHERE semantic_sha256=?
                """,
                (semantic_sha256,),
            ).fetchone()
        if row is None:
            return None
        return ReferencePlaybackProfile.model_validate_json(row['payload_json'])

    def list_profile_versions(
        self, profile_id: str
    ) -> tuple[ReferencePlaybackProfile, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM cad_reference_playback_profiles
                WHERE profile_id=? ORDER BY seq ASC
                """,
                (profile_id,),
            ).fetchall()
        return tuple(
            ReferencePlaybackProfile.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Conditions

    def _validate_condition(self, condition: PlaybackLevelCondition) -> None:
        revision = self.scene_repository.get(condition.scene_revision_id)
        if revision is None:
            raise ValueError('condition pins a SceneRevision that is not persisted')
        if revision.content_hash != condition.scene_content_hash:
            raise ValueError('condition SceneRevision content hash mismatch')
        if revision.document_id != condition.document_id:
            raise ValueError('condition SceneRevision belongs to another document')
        if condition.reference_profile is not None:
            profile = self.get_profile_by_hash(
                condition.reference_profile.semantic_sha256
            )
            if profile is None:
                raise ValueError(
                    'condition pins a ReferencePlaybackProfile that is not '
                    'persisted'
                )
            if (
                profile.profile_id != condition.reference_profile.profile_id
                or profile.version != condition.reference_profile.version
            ):
                raise ValueError(
                    'reference profile hash resolves to a different '
                    'profile/version'
                )
        if condition.acoustic_evidence:
            from .cad_measurement_disposition import (
                MEASUREMENT_ELIGIBLE_DISPOSITIONS,
            )

            measurements, quality = self._measurement_repositories()
            for evidence in condition.acoustic_evidence:
                measurement = measurements.get_measurement(
                    evidence.measurement_id
                )
                if measurement is None:
                    raise ValueError(
                        'acoustic evidence references unknown measurement '
                        f'{evidence.measurement_id}'
                    )
                if measurement.document_id != condition.document_id:
                    raise ValueError(
                        'acoustic evidence measurement belongs to another '
                        'document'
                    )
                if measurement.scene_revision_id != condition.scene_revision_id:
                    raise ValueError(
                        'acoustic evidence measurement was captured on a '
                        'different SceneRevision'
                    )
                disposition = quality.latest_disposition(
                    evidence.measurement_id
                )
                if (
                    disposition is not None
                    and disposition.disposition
                    not in MEASUREMENT_ELIGIBLE_DISPOSITIONS
                ):
                    raise ValueError(
                        f'measurement {evidence.measurement_id} is not '
                        'eligible acoustic-level evidence'
                    )

    def save_condition(self, condition: PlaybackLevelCondition) -> None:
        if self.get_condition_by_hash(condition.condition_sha256) is not None:
            raise PlaybackLevelConflictError(
                'PlaybackLevelCondition content is append-only'
            )
        self._validate_condition(condition)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_playback_level_conditions (
                    condition_id, document_id, scene_revision_id,
                    condition_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    condition.condition_id,
                    condition.document_id,
                    condition.scene_revision_id,
                    condition.condition_sha256,
                    condition.created_at_utc,
                    condition.model_dump_json(),
                ),
            )

    def get_condition(self, condition_id: str) -> PlaybackLevelCondition | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_playback_level_conditions
                WHERE condition_id=? ORDER BY seq DESC LIMIT 1
                """,
                (condition_id,),
            ).fetchone()
        if row is None:
            return None
        return PlaybackLevelCondition.model_validate_json(row['payload_json'])

    def get_condition_by_hash(
        self, condition_sha256: str
    ) -> PlaybackLevelCondition | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_playback_level_conditions
                WHERE condition_sha256=?
                """,
                (condition_sha256,),
            ).fetchone()
        if row is None:
            return None
        return PlaybackLevelCondition.model_validate_json(row['payload_json'])

    def list_conditions(
        self, document_id: str
    ) -> tuple[PlaybackLevelCondition, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM cad_playback_level_conditions
                WHERE document_id=? ORDER BY created_at_utc, condition_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            PlaybackLevelCondition.model_validate_json(row['payload_json'])
            for row in rows
        )
