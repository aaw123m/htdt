"""Append-only persistence for the A/V latency (lip-sync) authority
(#582).

Four tables:

* ``cad_av_latency_profiles`` — sealed perceptual/standards profiles
  (ITU-R BT.1359-1, ATSC IS-191, project custom).
* ``cad_av_latency_paths`` — sealed end-to-end latency path
  declarations bound to an ``AVSignalPath`` revision.
* ``cad_av_latency_path_measurements`` — sealed physical/protocol sync
  measurements bound to one path revision.
* ``cad_av_latency_qualifications`` — sealed closed-loop verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_av_latency import (
    AVLatencyPath,
    AVLatencyPathMeasurement,
    AVLatencyProfile,
    AVLatencyQualification,
)


class AVLatencyConflictError(ValueError):
    """An AV-sync save violated append-only identity rules."""


class AVLatencyIntegrityError(ValueError):
    """A stored AV-sync row disagreed with its payload or references."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha or getattr(record, id_field) != (
        getattr(record, id_field).split(':')[0] + ':' + sha
    ):
        raise AVLatencyIntegrityError(
            'record payload does not match its sealed identity'
        )


def _assert_sha(record: object, sha_field: str) -> None:
    """Seal check for records whose id is a versioned label, not a
    ``prefix:sha`` digest (latency paths carry caller-named path_id)."""
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise AVLatencyIntegrityError(
            'record payload does not match its sealed sha256'
        )


class CadAVLatencyRepository:
    """Native storage for A/V latency paths, profiles, measurements
    and qualifications."""

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
                'cad_av_latency_profiles',
                'cad_av_latency_paths',
                'cad_av_latency_path_measurements',
                'cad_av_latency_qualifications',
            )

    # ------------------------------------------------------------------
    # Profiles

    def save_profile(self, profile: AVLatencyProfile) -> None:
        _assert_sealed(profile, 'profile_sha256', 'profile_id')
        existing = self.get_profile(profile.profile_id)
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise AVLatencyConflictError(
                'av sync profiles are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_av_latency_profiles (
                    profile_id, profile_sha256, document_id,
                    profile_kind, label, standard_ref,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.profile_sha256,
                    profile.document_id,
                    profile.profile_kind,
                    profile.label,
                    profile.standard_ref,
                    profile.created_at_utc,
                    profile.model_dump_json(),
                ),
            )

    def get_profile(self, profile_id: str) -> AVLatencyProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT profile_id, profile_sha256, document_id,
                       profile_kind, label, standard_ref,
                       created_at_utc, payload_json
                FROM cad_av_latency_profiles
                WHERE profile_id=?
                """,
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        profile = AVLatencyProfile.model_validate_json(row['payload_json'])
        if (
            profile.profile_id != row['profile_id']
            or profile.profile_sha256 != row['profile_sha256']
            or profile.document_id != row['document_id']
            or profile.profile_kind != row['profile_kind']
            or profile.label != row['label']
            or profile.standard_ref != row['standard_ref']
            or profile.created_at_utc != row['created_at_utc']
        ):
            raise AVLatencyIntegrityError(
                'av sync profile row disagrees with payload'
            )
        return profile

    def list_profiles(
        self, document_id: str
    ) -> tuple[AVLatencyProfile, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM cad_av_latency_profiles
                WHERE document_id=?
                ORDER BY created_at_utc, profile_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            AVLatencyProfile.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Latency paths

    def save_path(self, path: AVLatencyPath) -> None:
        _assert_sha(path, 'path_sha256')
        existing = self.get_path(path.path_id, path.version)
        if existing is not None:
            if existing.path_sha256 == path.path_sha256:
                return
            raise AVLatencyConflictError(
                'av latency paths are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_av_latency_paths (
                    path_id, version, path_sha256, document_id,
                    signal_path_id, signal_path_version,
                    signal_path_sha256, display_picture_mode,
                    audio_route, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    path.path_id,
                    path.version,
                    path.path_sha256,
                    path.document_id,
                    path.signal_path_id,
                    path.signal_path_version,
                    path.signal_path_sha256,
                    path.display_picture_mode,
                    path.audio_route,
                    path.created_at_utc,
                    path.model_dump_json(),
                ),
            )

    def get_path(
        self, path_id: str, version: str
    ) -> AVLatencyPath | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT path_id, version, path_sha256, document_id,
                       signal_path_id, signal_path_version,
                       signal_path_sha256, display_picture_mode,
                       audio_route, created_at_utc, payload_json
                FROM cad_av_latency_paths
                WHERE path_id=? AND version=?
                """,
                (path_id, version),
            ).fetchone()
        if row is None:
            return None
        return self._path_from_row(row)

    def get_path_revision(
        self, path_id: str, path_sha256: str
    ) -> AVLatencyPath | None:
        """Fetch the exact sealed revision a measurement/qualification
        binds — staled evidence always stays readable."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT path_id, version, path_sha256, document_id,
                       signal_path_id, signal_path_version,
                       signal_path_sha256, display_picture_mode,
                       audio_route, created_at_utc, payload_json
                FROM cad_av_latency_paths
                WHERE path_id=? AND path_sha256=?
                """,
                (path_id, path_sha256),
            ).fetchone()
        if row is None:
            return None
        return self._path_from_row(row)

    def list_paths(
        self, document_id: str
    ) -> tuple[AVLatencyPath, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT path_id, version, path_sha256, document_id,
                       signal_path_id, signal_path_version,
                       signal_path_sha256, display_picture_mode,
                       audio_route, created_at_utc, payload_json
                FROM cad_av_latency_paths
                WHERE document_id=?
                ORDER BY created_at_utc, path_id, version
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._path_from_row(row) for row in rows)

    def _path_from_row(self, row: sqlite3.Row) -> AVLatencyPath:
        path = AVLatencyPath.model_validate_json(row['payload_json'])
        if (
            path.path_id != row['path_id']
            or path.version != row['version']
            or path.path_sha256 != row['path_sha256']
            or path.document_id != row['document_id']
            or path.signal_path_id != row['signal_path_id']
            or path.signal_path_version != row['signal_path_version']
            or path.signal_path_sha256 != row['signal_path_sha256']
            or path.display_picture_mode != row['display_picture_mode']
            or path.audio_route != row['audio_route']
            or path.created_at_utc != row['created_at_utc']
        ):
            raise AVLatencyIntegrityError(
                'av latency path row disagrees with payload'
            )
        return path

    # ------------------------------------------------------------------
    # Measurements

    def save_measurement(self, measurement: AVLatencyPathMeasurement) -> None:
        _assert_sealed(
            measurement, 'measurement_sha256', 'measurement_id'
        )
        existing = self.get_measurement(measurement.measurement_id)
        if existing is not None:
            if (
                existing.measurement_sha256
                == measurement.measurement_sha256
            ):
                return
            raise AVLatencyConflictError(
                'av sync measurements are append-only'
            )
        if (
            self.get_path_revision(
                measurement.path_id, measurement.path_sha256
            )
            is None
        ):
            raise AVLatencyIntegrityError(
                'an av sync measurement must reference a persisted '
                'path revision'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_av_latency_path_measurements (
                    measurement_id, measurement_sha256, document_id,
                    path_id, path_version, path_sha256, method,
                    measured_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    measurement.measurement_id,
                    measurement.measurement_sha256,
                    measurement.document_id,
                    measurement.path_id,
                    measurement.path_version,
                    measurement.path_sha256,
                    measurement.method,
                    measurement.measured_at_utc,
                    measurement.model_dump_json(),
                ),
            )

    def get_measurement(
        self, measurement_id: str
    ) -> AVLatencyPathMeasurement | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT measurement_id, measurement_sha256, document_id,
                       path_id, path_version, path_sha256, method,
                       measured_at_utc, payload_json
                FROM cad_av_latency_path_measurements
                WHERE measurement_id=?
                """,
                (measurement_id,),
            ).fetchone()
        if row is None:
            return None
        return self._measurement_from_row(row)

    def list_measurements(
        self, document_id: str
    ) -> tuple[AVLatencyPathMeasurement, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT measurement_id, measurement_sha256, document_id,
                       path_id, path_version, path_sha256, method,
                       measured_at_utc, payload_json
                FROM cad_av_latency_path_measurements
                WHERE document_id=?
                ORDER BY measured_at_utc, measurement_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._measurement_from_row(row) for row in rows)

    def _measurement_from_row(
        self, row: sqlite3.Row
    ) -> AVLatencyPathMeasurement:
        measurement = AVLatencyPathMeasurement.model_validate_json(
            row['payload_json']
        )
        if (
            measurement.measurement_id != row['measurement_id']
            or measurement.measurement_sha256
            != row['measurement_sha256']
            or measurement.document_id != row['document_id']
            or measurement.path_id != row['path_id']
            or measurement.path_version != row['path_version']
            or measurement.path_sha256 != row['path_sha256']
            or measurement.method != row['method']
            or measurement.measured_at_utc != row['measured_at_utc']
        ):
            raise AVLatencyIntegrityError(
                'av sync measurement row disagrees with payload'
            )
        return measurement

    # ------------------------------------------------------------------
    # Qualifications

    def save_qualification(
        self, qualification: AVLatencyQualification
    ) -> None:
        _assert_sealed(
            qualification, 'qualification_sha256', 'qualification_id'
        )
        existing = self.get_qualification(
            qualification.qualification_id
        )
        if existing is not None:
            if (
                existing.qualification_sha256
                == qualification.qualification_sha256
            ):
                return
            raise AVLatencyConflictError(
                'av sync qualifications are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_av_latency_qualifications (
                    qualification_id, qualification_sha256,
                    document_id, path_id, path_version, path_sha256,
                    profile_id,
                    verdict, evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.path_id,
                    qualification.path_version,
                    qualification.path_sha256,
                    qualification.profile_id,
                    qualification.verdict,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> AVLatencyQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT qualification_id, qualification_sha256,
                       document_id, path_id, path_version, path_sha256,
                       profile_id,
                       verdict, evaluated_at_utc, payload_json
                FROM cad_av_latency_qualifications
                WHERE qualification_id=?
                """,
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        return self._qualification_from_row(row)

    def list_qualifications(
        self, document_id: str
    ) -> tuple[AVLatencyQualification, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT qualification_id, qualification_sha256,
                       document_id, path_id, path_version, path_sha256,
                       profile_id,
                       verdict, evaluated_at_utc, payload_json
                FROM cad_av_latency_qualifications
                WHERE document_id=?
                ORDER BY evaluated_at_utc, qualification_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            self._qualification_from_row(row) for row in rows
        )

    def _qualification_from_row(
        self, row: sqlite3.Row
    ) -> AVLatencyQualification:
        qualification = AVLatencyQualification.model_validate_json(
            row['payload_json']
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.path_id != row['path_id']
            or qualification.path_version != row['path_version']
            or qualification.path_sha256 != row['path_sha256']
            or qualification.profile_id != row['profile_id']
            or qualification.verdict != row['verdict']
            or qualification.evaluated_at_utc
            != row['evaluated_at_utc']
        ):
            raise AVLatencyIntegrityError(
                'av sync qualification row disagrees with payload'
            )
        return qualification


__all__ = [
    'AVLatencyConflictError',
    'AVLatencyIntegrityError',
    'CadAVLatencyRepository',
]
