"""Append-only persistence for target curve profiles (#508)."""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_calibration_repository import CadCalibrationRepository
from .cad_repository import SceneRepository
from .cad_schema import require_native_tables, connect_sqlite
from .cad_target_profile import (
    CadTargetCurveProfile,
    CalibrationPlanTargetBinding,
)


class TargetProfileConflictError(ValueError):
    """A profile/binding save violated append-only identity rules."""


class CadTargetProfileRepository:
    """Native storage for CadTargetCurveProfile + plan bindings.

    Profiles are immutable and versioned: ``(profile_id, version)`` may be
    saved exactly once; redefining a target means a new version row, never
    an UPDATE. Plan bindings are append-only facts pinned to both semantic
    hashes, so a saved plan and a saved profile version can always be audited
    to the exact curve that was intended.
    """

    def __init__(
        self,
        scene_repository: SceneRepository,
        calibration_repository: CadCalibrationRepository | None = None,
    ) -> None:
        self.scene_repository = scene_repository
        # CadCalibrationRepository aggregates several authorities, so it is
        # injected rather than constructed here; it is only required for
        # ``save_binding`` (to prove the pinned plan exists).
        self.calibration_repository = calibration_repository
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        # #767: persistent schema is owned by the migration authority;
        # repositories verify the migrated contract, never converge it.
        with closing(self._connect()) as connection, connection:
            require_native_tables(connection, 'cad_target_curve_profiles', 'cad_plan_target_bindings')

    def save_profile(self, profile: CadTargetCurveProfile) -> None:
        if self.get_profile(profile.profile_id, profile.version) is not None:
            raise TargetProfileConflictError(
                'CadTargetCurveProfile (profile_id, version) is append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_target_curve_profiles (
                    profile_id, version, document_id, semantic_sha256,
                    payload_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.version,
                    profile.document_id,
                    profile.semantic_sha256,
                    profile.model_dump_json(),
                    profile.created_at_utc,
                ),
            )

    def get_profile(
        self,
        profile_id: str,
        version: str,
    ) -> CadTargetCurveProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_target_curve_profiles
                WHERE profile_id=? AND version=?
                """,
                (profile_id, version),
            ).fetchone()
        if row is None:
            return None
        return CadTargetCurveProfile.model_validate_json(row['payload_json'])

    def get_profile_by_hash(
        self,
        semantic_sha256: str,
    ) -> CadTargetCurveProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_target_curve_profiles
                WHERE semantic_sha256=?
                """,
                (semantic_sha256,),
            ).fetchone()
        if row is None:
            return None
        return CadTargetCurveProfile.model_validate_json(row['payload_json'])

    def list_profiles(
        self,
        document_id: str,
    ) -> tuple[CadTargetCurveProfile, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_target_curve_profiles
                WHERE document_id=?
                ORDER BY seq ASC
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            CadTargetCurveProfile.model_validate_json(row['payload_json'])
            for row in rows
        )

    def list_profile_versions(
        self,
        profile_id: str,
    ) -> tuple[CadTargetCurveProfile, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_target_curve_profiles
                WHERE profile_id=?
                ORDER BY seq ASC
                """,
                (profile_id,),
            ).fetchall()
        return tuple(
            CadTargetCurveProfile.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Plan bindings

    def save_binding(self, binding: CalibrationPlanTargetBinding) -> None:
        if self.get_binding(binding.binding_id) is not None:
            raise TargetProfileConflictError(
                'CalibrationPlanTargetBinding ids are append-only'
            )
        profile = self.get_profile(
            binding.profile_id, binding.profile_version
        )
        if profile is None:
            raise ValueError('binding requires a persisted target profile')
        if profile.semantic_sha256 != binding.profile_semantic_sha256:
            raise ValueError('binding is pinned to a different profile payload')
        if self.calibration_repository is None:
            raise ValueError(
                'binding saves require a CadCalibrationRepository'
            )
        plan = self.calibration_repository.get_plan(binding.plan_id)
        if plan is None:
            raise ValueError('binding requires a persisted calibration plan')
        if plan.plan_semantic_sha256 != binding.plan_semantic_sha256:
            raise ValueError('binding is pinned to a different plan payload')
        if plan.document_id != binding.document_id:
            raise ValueError('binding document does not match plan')
        if profile.document_id != binding.document_id:
            raise ValueError('binding document does not match profile')
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_plan_target_bindings (
                    binding_id, document_id, plan_id, profile_id,
                    profile_version, binding_sha256, payload_json, bound_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    binding.binding_id,
                    binding.document_id,
                    binding.plan_id,
                    binding.profile_id,
                    binding.profile_version,
                    binding.binding_sha256,
                    binding.model_dump_json(),
                    binding.bound_at_utc,
                ),
            )

    def get_binding(
        self,
        binding_id: str,
    ) -> CalibrationPlanTargetBinding | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_plan_target_bindings
                WHERE binding_id=?
                """,
                (binding_id,),
            ).fetchone()
        if row is None:
            return None
        return CalibrationPlanTargetBinding.model_validate_json(
            row['payload_json']
        )

    def list_bindings_for_plan(
        self,
        plan_id: str,
    ) -> tuple[CalibrationPlanTargetBinding, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_plan_target_bindings
                WHERE plan_id=?
                ORDER BY seq ASC
                """,
                (plan_id,),
            ).fetchall()
        return tuple(
            CalibrationPlanTargetBinding.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    def list_bindings_for_profile(
        self,
        profile_id: str,
    ) -> tuple[CalibrationPlanTargetBinding, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_plan_target_bindings
                WHERE profile_id=?
                ORDER BY seq ASC
                """,
                (profile_id,),
            ).fetchall()
        return tuple(
            CalibrationPlanTargetBinding.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )


__all__ = [
    'CadTargetProfileRepository',
    'TargetProfileConflictError',
]
