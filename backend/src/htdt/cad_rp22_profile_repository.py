"""Append-only persistence for the RP22 standards profile (#579)."""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .cad_rp22_profile import RP22Evaluation, RP22StandardsProfile


class RP22ProfileConflictError(ValueError):
    """A profile/evaluation save violated append-only identity rules."""


class CadRP22ProfileRepository:
    """Native storage for RP22StandardsProfile + RP22Evaluation.

    The parameter-declaration profile is immutable and versioned by
    edition: ``(profile_id, edition)`` may be saved exactly once, so the
    declared limits behind any past conformance verdict stay auditably
    pinned. Evaluations are append-only facts that reference the profile
    hash — a re-evaluation is a new row, never an UPDATE.
    """

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        # #767: persistent schema is owned by the migration authority;
        # repositories verify the migrated contract, never converge it.
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_rp22_profiles',
                'cad_rp22_evaluations',
            )

    # ------------------------------------------------------------------
    # Profiles

    def save_profile(self, profile: RP22StandardsProfile) -> None:
        if (
            self.get_profile(profile.profile_id, profile.edition)
            is not None
        ):
            raise RP22ProfileConflictError(
                'RP22StandardsProfile (profile_id, edition) is '
                'append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_rp22_profiles (
                    profile_id, profile_version, profile_sha256,
                    registry_key, payload_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.edition,
                    profile.profile_sha256,
                    profile.registry_key,
                    profile.model_dump_json(),
                    profile.created_at_utc,
                ),
            )

    def get_profile(
        self,
        profile_id: str,
        edition: str,
    ) -> RP22StandardsProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_rp22_profiles
                WHERE profile_id=? AND profile_version=?
                """,
                (profile_id, edition),
            ).fetchone()
        if row is None:
            return None
        return RP22StandardsProfile.model_validate_json(row['payload_json'])

    def get_profile_by_hash(
        self,
        profile_sha256: str,
    ) -> RP22StandardsProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_rp22_profiles
                WHERE profile_sha256=?
                """,
                (profile_sha256,),
            ).fetchone()
        if row is None:
            return None
        return RP22StandardsProfile.model_validate_json(row['payload_json'])

    def list_profiles(
        self,
        profile_id: str,
    ) -> tuple[RP22StandardsProfile, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_rp22_profiles
                WHERE profile_id=?
                ORDER BY seq ASC
                """,
                (profile_id,),
            ).fetchall()
        return tuple(
            RP22StandardsProfile.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Evaluations

    def save_evaluation(self, evaluation: RP22Evaluation) -> None:
        if self.get_evaluation(evaluation.evaluation_id) is not None:
            raise RP22ProfileConflictError(
                'RP22Evaluation ids are append-only'
            )
        profile = self.get_profile_by_hash(evaluation.profile_sha256)
        if profile is None:
            raise ValueError(
                'an RP22 evaluation requires a persisted profile'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_rp22_evaluations (
                    evaluation_id, document_id, profile_id,
                    profile_version, evaluation_sha256, payload_json,
                    evaluated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.evaluation_id,
                    evaluation.document_id,
                    evaluation.profile_id,
                    profile.edition,
                    evaluation.evaluation_sha256,
                    evaluation.model_dump_json(),
                    evaluation.evaluated_at_utc,
                ),
            )

    def get_evaluation(
        self,
        evaluation_id: str,
    ) -> RP22Evaluation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_rp22_evaluations
                WHERE evaluation_id=?
                """,
                (evaluation_id,),
            ).fetchone()
        if row is None:
            return None
        return RP22Evaluation.model_validate_json(row['payload_json'])

    def list_evaluations(
        self,
        document_id: str,
    ) -> tuple[RP22Evaluation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_rp22_evaluations
                WHERE document_id=?
                ORDER BY seq ASC
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            RP22Evaluation.model_validate_json(row['payload_json'])
            for row in rows
        )


__all__ = [
    'CadRP22ProfileRepository',
    'RP22ProfileConflictError',
]
