"""Append-only persistence for the viewing-environment authority (#633).

Four tables:

* ``cad_ve_observations`` — sealed ``ViewingEnvironmentObservation``
  environment captures keyed by ``observation_id``.
* ``cad_ve_geometry_observations`` — sealed
  ``ViewingGeometryObservation`` viewer/screen geometry; a bound
  environment observation must persist when declared.
* ``cad_ve_lighting_scenes`` — sealed ``LightingSceneRecord`` automation
  scenes; a bound observation must persist when declared.
* ``cad_ve_qualifications`` — sealed ``ViewingEnvironmentQualification``
  verdicts; the observation and every bound geometry must persist.

Re-saving an identical row is a no-op; a divergent hash under the same
id is a conflict.  Records are sealed before insert — a ``model_copy``
forgery is rejected at the repository boundary.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_viewing_environment import (
    LightingSceneRecord,
    ViewingEnvironmentObservation,
    ViewingEnvironmentQualification,
    ViewingGeometryObservation,
)
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256


class ViewingEnvironmentConflictError(ValueError):
    """A viewing-environment save violated append-only identity rules."""


class ViewingEnvironmentIntegrityError(ValueError):
    """A stored viewing-environment row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha or getattr(record, id_field) != (
        getattr(record, id_field).split(':')[0] + ':' + sha
    ):
        raise ViewingEnvironmentIntegrityError(
            'record payload does not match its sealed identity'
        )


class CadViewingEnvironmentRepository:
    """Native storage for viewing-environment authority records."""

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
                'cad_ve_observations',
                'cad_ve_geometry_observations',
                'cad_ve_lighting_scenes',
                'cad_ve_qualifications',
            )

    # ------------------------------------------------------------------
    # Environment observations

    def save_observation(
        self, observation: ViewingEnvironmentObservation
    ) -> None:
        _assert_sealed(
            observation, 'observation_sha256', 'observation_id'
        )
        existing = self.get_observation(observation.observation_id)
        if existing is not None:
            if existing.observation_sha256 == observation.observation_sha256:
                return
            raise ViewingEnvironmentConflictError(
                'environment observations are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_ve_observations (
                    observation_id, observation_sha256, document_id,
                    room_ref, captured_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.observation_sha256,
                    observation.document_id,
                    observation.room_ref,
                    observation.captured_at_utc,
                    observation.model_dump_json(),
                ),
            )

    def get_observation(
        self, observation_id: str
    ) -> ViewingEnvironmentObservation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT observation_id, observation_sha256, document_id,
                       room_ref, captured_at_utc, payload_json
                FROM cad_ve_observations
                WHERE observation_id=?
                """,
                (observation_id,),
            ).fetchone()
        if row is None:
            return None
        return self._observation_from_row(row)

    def list_observations(
        self, document_id: str
    ) -> tuple[ViewingEnvironmentObservation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT observation_id, observation_sha256, document_id,
                       room_ref, captured_at_utc, payload_json
                FROM cad_ve_observations
                WHERE document_id=?
                ORDER BY captured_at_utc, observation_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            self._observation_from_row(row) for row in rows
        )

    def _observation_from_row(
        self, row: sqlite3.Row
    ) -> ViewingEnvironmentObservation:
        observation = ViewingEnvironmentObservation.model_validate_json(
            row['payload_json']
        )
        if (
            observation.observation_id != row['observation_id']
            or observation.observation_sha256 != row['observation_sha256']
            or observation.document_id != row['document_id']
            or observation.room_ref != row['room_ref']
            or observation.captured_at_utc != row['captured_at_utc']
        ):
            raise ViewingEnvironmentIntegrityError(
                'environment observation row disagrees with its payload'
            )
        return observation

    # ------------------------------------------------------------------
    # Geometry observations

    def save_geometry(
        self, geometry: ViewingGeometryObservation
    ) -> None:
        _assert_sealed(geometry, 'geometry_sha256', 'geometry_id')
        existing = self.get_geometry(geometry.geometry_id)
        if existing is not None:
            if existing.geometry_sha256 == geometry.geometry_sha256:
                return
            raise ViewingEnvironmentConflictError(
                'geometry observations are append-only'
            )
        if geometry.observation_id is not None:
            bound = self.get_observation(geometry.observation_id)
            if bound is None:
                raise ViewingEnvironmentIntegrityError(
                    'a bound environment observation must persist'
                )
            if bound.observation_sha256 != geometry.observation_sha256:
                raise ViewingEnvironmentIntegrityError(
                    'bound observation hash mismatch'
                )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_ve_geometry_observations (
                    geometry_id, geometry_sha256, document_id,
                    observation_id, observation_sha256, seat_ref,
                    measured_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    geometry.geometry_id,
                    geometry.geometry_sha256,
                    geometry.document_id,
                    geometry.observation_id,
                    geometry.observation_sha256,
                    geometry.seat_ref,
                    geometry.measured_at_utc,
                    geometry.model_dump_json(),
                ),
            )

    def get_geometry(
        self, geometry_id: str
    ) -> ViewingGeometryObservation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT geometry_id, geometry_sha256, document_id,
                       observation_id, observation_sha256, seat_ref,
                       measured_at_utc, payload_json
                FROM cad_ve_geometry_observations
                WHERE geometry_id=?
                """,
                (geometry_id,),
            ).fetchone()
        if row is None:
            return None
        return self._geometry_from_row(row)

    def list_geometry(
        self, document_id: str
    ) -> tuple[ViewingGeometryObservation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT geometry_id, geometry_sha256, document_id,
                       observation_id, observation_sha256, seat_ref,
                       measured_at_utc, payload_json
                FROM cad_ve_geometry_observations
                WHERE document_id=?
                ORDER BY measured_at_utc, geometry_id
                """,
                (document_id,),
            ).fetchall()
        return tuple(self._geometry_from_row(row) for row in rows)

    def _geometry_from_row(
        self, row: sqlite3.Row
    ) -> ViewingGeometryObservation:
        geometry = ViewingGeometryObservation.model_validate_json(
            row['payload_json']
        )
        if (
            geometry.geometry_id != row['geometry_id']
            or geometry.geometry_sha256 != row['geometry_sha256']
            or geometry.document_id != row['document_id']
            or geometry.observation_id != row['observation_id']
            or geometry.observation_sha256 != row['observation_sha256']
            or geometry.seat_ref != row['seat_ref']
            or geometry.measured_at_utc != row['measured_at_utc']
        ):
            raise ViewingEnvironmentIntegrityError(
                'geometry observation row disagrees with its payload'
            )
        return geometry

    # ------------------------------------------------------------------
    # Lighting scenes

    def save_scene(self, scene: LightingSceneRecord) -> None:
        _assert_sealed(scene, 'scene_sha256', 'scene_id')
        existing = self.get_scene(scene.scene_id)
        if existing is not None:
            if existing.scene_sha256 == scene.scene_sha256:
                return
            raise ViewingEnvironmentConflictError(
                'lighting scenes are append-only'
            )
        if scene.bound_observation_id is not None:
            bound = self.get_observation(scene.bound_observation_id)
            if bound is None:
                raise ViewingEnvironmentIntegrityError(
                    'a bound environment observation must persist'
                )
            if bound.observation_sha256 != scene.bound_observation_sha256:
                raise ViewingEnvironmentIntegrityError(
                    'bound observation hash mismatch'
                )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_ve_lighting_scenes (
                    scene_id, scene_sha256, document_id, name, kind,
                    bound_observation_id, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scene.scene_id,
                    scene.scene_sha256,
                    scene.document_id,
                    scene.name,
                    scene.kind,
                    scene.bound_observation_id,
                    scene.declared_at_utc,
                    scene.model_dump_json(),
                ),
            )

    def get_scene(
        self, scene_id: str
    ) -> LightingSceneRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT scene_id, scene_sha256, document_id, name, kind,
                       bound_observation_id, declared_at_utc, payload_json
                FROM cad_ve_lighting_scenes
                WHERE scene_id=?
                """,
                (scene_id,),
            ).fetchone()
        if row is None:
            return None
        scene = LightingSceneRecord.model_validate_json(
            row['payload_json']
        )
        if (
            scene.scene_id != row['scene_id']
            or scene.scene_sha256 != row['scene_sha256']
            or scene.document_id != row['document_id']
            or scene.name != row['name']
            or scene.kind != row['kind']
            or scene.bound_observation_id != row['bound_observation_id']
            or scene.declared_at_utc != row['declared_at_utc']
        ):
            raise ViewingEnvironmentIntegrityError(
                'lighting scene row disagrees with its payload'
            )
        return scene

    # ------------------------------------------------------------------
    # Qualifications

    def save_qualification(
        self, qualification: ViewingEnvironmentQualification
    ) -> None:
        _assert_sealed(
            qualification, 'qualification_sha256', 'qualification_id'
        )
        existing = self.get_qualification(qualification.qualification_id)
        if existing is not None:
            if (
                existing.qualification_sha256
                == qualification.qualification_sha256
            ):
                return
            raise ViewingEnvironmentConflictError(
                'qualifications are append-only'
            )
        bound = self.get_observation(qualification.observation_id)
        if bound is None:
            raise ViewingEnvironmentIntegrityError(
                'a qualification must reference a persisted observation'
            )
        if bound.observation_sha256 != qualification.observation_sha256:
            raise ViewingEnvironmentIntegrityError(
                'bound observation hash mismatch'
            )
        for geometry_id in qualification.geometry_ids:
            if self.get_geometry(geometry_id) is None:
                raise ViewingEnvironmentIntegrityError(
                    'a bound geometry observation must persist'
                )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_ve_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    observation_id, observation_sha256, profile_kind,
                    profile_scope, state, evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.observation_id,
                    qualification.observation_sha256,
                    qualification.profile_kind,
                    qualification.profile_scope,
                    qualification.state,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> ViewingEnvironmentQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT qualification_id, qualification_sha256,
                       document_id, observation_id, observation_sha256,
                       profile_kind, profile_scope, state,
                       evaluated_at_utc, payload_json
                FROM cad_ve_qualifications
                WHERE qualification_id=?
                """,
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        return self._qualification_from_row(row)

    def list_qualifications(
        self, document_id: str
    ) -> tuple[ViewingEnvironmentQualification, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT qualification_id, qualification_sha256,
                       document_id, observation_id, observation_sha256,
                       profile_kind, profile_scope, state,
                       evaluated_at_utc, payload_json
                FROM cad_ve_qualifications
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
    ) -> ViewingEnvironmentQualification:
        qualification = ViewingEnvironmentQualification.model_validate_json(
            row['payload_json']
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.observation_id != row['observation_id']
            or qualification.observation_sha256
            != row['observation_sha256']
            or qualification.profile_kind != row['profile_kind']
            or qualification.profile_scope != row['profile_scope']
            or qualification.state != row['state']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise ViewingEnvironmentIntegrityError(
                'qualification row disagrees with its payload'
            )
        return qualification
