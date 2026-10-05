"""Append-only persistence for response-target profiles (#588)."""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_response_target import (
    ResponseTargetProfile,
    SpectralBalanceEvaluation,
)
from .cad_schema import connect_sqlite, require_native_tables


class ResponseTargetConflictError(ValueError):
    """A profile/evaluation save violated append-only identity rules."""


class CadResponseTargetRepository:
    """Native storage for ResponseTargetProfile + SpectralBalanceEvaluation.

    Targets are immutable and versioned: ``(profile_id, version)`` may be
    saved exactly once — redefining a target is a new version row, never
    an UPDATE, so a historical comparison always resolves to the exact
    curve/semantics it ran under. Evaluations are append-only facts
    pinned to ``target_sha256``.
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
                'cad_response_targets',
                'cad_spectral_balance_evaluations',
            )

    # ------------------------------------------------------------------
    # Target profiles

    def save_profile(self, profile: ResponseTargetProfile) -> None:
        if (
            self.get_profile(profile.profile_id, profile.version)
            is not None
        ):
            raise ResponseTargetConflictError(
                'ResponseTargetProfile (profile_id, version) is '
                'append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_response_targets (
                    profile_id, version, document_id, target_sha256,
                    kind, payload_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.version,
                    profile.document_id,
                    profile.target_sha256,
                    profile.kind,
                    profile.model_dump_json(),
                    profile.created_at_utc,
                ),
            )

    def get_profile(
        self,
        profile_id: str,
        version: str,
    ) -> ResponseTargetProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_response_targets
                WHERE profile_id=? AND version=?
                """,
                (profile_id, version),
            ).fetchone()
        if row is None:
            return None
        return ResponseTargetProfile.model_validate_json(
            row['payload_json']
        )

    def get_profile_by_hash(
        self,
        target_sha256: str,
    ) -> ResponseTargetProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_response_targets
                WHERE target_sha256=?
                """,
                (target_sha256,),
            ).fetchone()
        if row is None:
            return None
        return ResponseTargetProfile.model_validate_json(
            row['payload_json']
        )

    def list_profiles(
        self,
        document_id: str,
    ) -> tuple[ResponseTargetProfile, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_response_targets
                WHERE document_id=?
                ORDER BY seq ASC
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            ResponseTargetProfile.model_validate_json(row['payload_json'])
            for row in rows
        )

    def list_profile_versions(
        self,
        profile_id: str,
    ) -> tuple[ResponseTargetProfile, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_response_targets
                WHERE profile_id=?
                ORDER BY seq ASC
                """,
                (profile_id,),
            ).fetchall()
        return tuple(
            ResponseTargetProfile.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Spectral-balance evaluations

    def save_evaluation(
        self,
        evaluation: SpectralBalanceEvaluation,
    ) -> None:
        if self.get_evaluation(evaluation.evaluation_id) is not None:
            raise ResponseTargetConflictError(
                'SpectralBalanceEvaluation ids are append-only'
            )
        profile = self.get_profile(
            evaluation.profile_id, evaluation.profile_version
        )
        if profile is None:
            raise ValueError(
                'a spectral-balance evaluation requires a persisted '
                'target profile'
            )
        if profile.target_sha256 != evaluation.target_sha256:
            raise ValueError(
                'evaluation is pinned to a different target payload'
            )
        if profile.document_id != evaluation.document_id:
            raise ValueError(
                'evaluation document does not match the target profile'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_spectral_balance_evaluations (
                    evaluation_id, document_id, profile_id,
                    profile_version, evaluation_sha256, payload_json,
                    evaluated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.evaluation_id,
                    evaluation.document_id,
                    evaluation.profile_id,
                    evaluation.profile_version,
                    evaluation.evaluation_sha256,
                    evaluation.model_dump_json(),
                    evaluation.evaluated_at_utc,
                ),
            )

    def get_evaluation(
        self,
        evaluation_id: str,
    ) -> SpectralBalanceEvaluation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_spectral_balance_evaluations
                WHERE evaluation_id=?
                """,
                (evaluation_id,),
            ).fetchone()
        if row is None:
            return None
        return SpectralBalanceEvaluation.model_validate_json(
            row['payload_json']
        )

    def list_evaluations(
        self,
        document_id: str,
    ) -> tuple[SpectralBalanceEvaluation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_spectral_balance_evaluations
                WHERE document_id=?
                ORDER BY seq ASC
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            SpectralBalanceEvaluation.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )


__all__ = [
    'CadResponseTargetRepository',
    'ResponseTargetConflictError',
]
