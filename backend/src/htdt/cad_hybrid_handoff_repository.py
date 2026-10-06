"""Append-only persistence for the wave↔geometrical hybrid-handoff
authority (#687, REV58-NUMERIC).

Two tables:

* ``cad_hybrid_composition_profiles`` — sealed component-pin +
  normalization + transition + filter + phenomenon declarations.
* ``cad_hybrid_transition_qualifications`` — sealed fail-closed
  handoff verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_hybrid_handoff_authority import (
    HybridCompositionProfile,
    HybridTransitionQualification,
)


class HybridHandoffConflictError(ValueError):
    """A hybrid-handoff save violated append-only identity rules."""


class HybridHandoffIntegrityError(ValueError):
    """A stored hybrid-handoff row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise HybridHandoffIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise HybridHandoffIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadHybridHandoffRepository:
    """Native storage for the #687 hybrid-handoff authority."""

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
                'cad_hybrid_composition_profiles',
                'cad_hybrid_transition_qualifications',
            )

    # ------------------------------------------------------------------
    # Composition profiles

    def save_profile(
        self, profile: HybridCompositionProfile
    ) -> None:
        _assert_sealed(profile, 'profile_sha256', 'profile_id')
        existing = self.get_profile(profile.profile_id)
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise HybridHandoffConflictError(
                'hybrid composition profiles are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_hybrid_composition_profiles (
                    profile_id, profile_sha256, document_id,
                    wave_prediction_ref_id, ga_prediction_ref_id,
                    transition_kind, output_capability, declared_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.profile_sha256,
                    profile.document_id,
                    profile.wave_component.prediction_ref.ref_id,
                    profile.ga_component.prediction_ref.ref_id,
                    profile.transition.kind,
                    profile.output_capability,
                    profile.declared_at_utc,
                    profile.model_dump_json(),
                ),
            )

    def get_profile(
        self, profile_id: str
    ) -> HybridCompositionProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_hybrid_composition_profiles '
                'WHERE profile_id=?',
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        profile = HybridCompositionProfile.model_validate_json(
            row['payload_json']
        )
        if (
            profile.profile_id != row['profile_id']
            or profile.profile_sha256 != row['profile_sha256']
            or profile.document_id != row['document_id']
            or profile.wave_component.prediction_ref.ref_id
            != row['wave_prediction_ref_id']
            or profile.ga_component.prediction_ref.ref_id
            != row['ga_prediction_ref_id']
            or profile.transition.kind != row['transition_kind']
            or profile.output_capability != row['output_capability']
            or profile.declared_at_utc != row['declared_at_utc']
        ):
            raise HybridHandoffIntegrityError(
                'stored hybrid composition profile disagrees with its '
                'payload'
            )
        return profile

    def list_profiles(
        self, document_id: str | None = None
    ) -> tuple[HybridCompositionProfile, ...]:
        query = (
            'SELECT payload_json FROM cad_hybrid_composition_profiles'
        )
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            HybridCompositionProfile.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # Transition qualifications

    def save_qualification(
        self, qualification: HybridTransitionQualification
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
            raise HybridHandoffConflictError(
                'hybrid transition qualifications are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_hybrid_transition_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    profile_ref_id, handoff_state, gap_low_hz,
                    gap_high_hz, evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.profile_ref.ref_id,
                    qualification.handoff_state,
                    (
                        qualification.gap_band_hz[0]
                        if qualification.gap_band_hz is not None
                        else None
                    ),
                    (
                        qualification.gap_band_hz[1]
                        if qualification.gap_band_hz is not None
                        else None
                    ),
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> HybridTransitionQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_hybrid_transition_qualifications '
                'WHERE qualification_id=?',
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = (
            HybridTransitionQualification.model_validate_json(
                row['payload_json']
            )
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.profile_ref.ref_id != row['profile_ref_id']
            or qualification.handoff_state != row['handoff_state']
            or (
                qualification.gap_band_hz[0]
                if qualification.gap_band_hz is not None
                else None
            ) != row['gap_low_hz']
            or (
                qualification.gap_band_hz[1]
                if qualification.gap_band_hz is not None
                else None
            ) != row['gap_high_hz']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise HybridHandoffIntegrityError(
                'stored hybrid transition qualification disagrees with '
                'its payload'
            )
        return qualification

    def list_qualifications(
        self, document_id: str | None = None
    ) -> tuple[HybridTransitionQualification, ...]:
        query = (
            'SELECT payload_json FROM '
            'cad_hybrid_transition_qualifications'
        )
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            HybridTransitionQualification.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )
