"""Append-only persistence for the measurement-chain linearity /
overload authority (#695, REV58-MEASCHAIN).

Three tables:

* ``cad_measchain_linearity_profiles`` — sealed acquisition-chain
  linearity profiles.
* ``cad_measchain_overload_observations`` — sealed per-capture overload
  observations.
* ``cad_measchain_qualifications`` — sealed fail-closed capability
  verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_measchain_linearity import (
    CadAcquisitionOverloadObservation,
    CadMeasChainLinearityProfile,
    CadMeasChainQualification,
)


class MeasChainAuthorityConflictError(ValueError):
    """A measchain-authority save violated append-only identity rules."""


class MeasChainAuthorityIntegrityError(ValueError):
    """A stored measchain row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise MeasChainAuthorityIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise MeasChainAuthorityIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadMeasChainLinearityRepository:
    """Native storage for the #695 measurement-chain authority records."""

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
                'cad_measchain_linearity_profiles',
                'cad_measchain_overload_observations',
                'cad_measchain_qualifications',
            )

    # ------------------------------------------------------------------
    # Linearity profiles

    def save_profile(self, profile: CadMeasChainLinearityProfile) -> None:
        _assert_sealed(profile, 'profile_sha256', 'profile_id')
        existing = self.get_profile(profile.profile_id)
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise MeasChainAuthorityConflictError(
                'measchain linearity profiles are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_measchain_linearity_profiles (
                    profile_id, profile_sha256, document_id,
                    chain_label, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.profile_sha256,
                    profile.document_id,
                    profile.chain_label,
                    profile.declared_at_utc,
                    profile.model_dump_json(),
                ),
            )

    def get_profile(
        self, profile_id: str
    ) -> CadMeasChainLinearityProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_measchain_linearity_profiles '
                'WHERE profile_id=?',
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        profile = CadMeasChainLinearityProfile.model_validate_json(
            row['payload_json']
        )
        if (
            profile.profile_id != row['profile_id']
            or profile.profile_sha256 != row['profile_sha256']
            or profile.document_id != row['document_id']
            or profile.chain_label != row['chain_label']
            or profile.declared_at_utc != row['declared_at_utc']
        ):
            raise MeasChainAuthorityIntegrityError(
                'measchain profile row disagrees with payload'
            )
        return profile

    def list_profiles(
        self, document_id: str
    ) -> tuple[CadMeasChainLinearityProfile, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_measchain_linearity_profiles '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadMeasChainLinearityProfile.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Overload observations

    def save_observation(
        self, observation: CadAcquisitionOverloadObservation
    ) -> None:
        _assert_sealed(
            observation, 'observation_sha256', 'observation_id'
        )
        existing = self.get_observation(observation.observation_id)
        if existing is not None:
            if existing.observation_sha256 == observation.observation_sha256:
                return
            raise MeasChainAuthorityConflictError(
                'overload observations are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_measchain_overload_observations (
                    observation_id, observation_sha256, document_id,
                    chain_ref_id, overload_mechanism,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.observation_sha256,
                    observation.document_id,
                    observation.chain_ref.ref_id,
                    observation.overload_mechanism,
                    observation.declared_at_utc,
                    observation.model_dump_json(),
                ),
            )

    def get_observation(
        self, observation_id: str
    ) -> CadAcquisitionOverloadObservation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_measchain_overload_observations '
                'WHERE observation_id=?',
                (observation_id,),
            ).fetchone()
        if row is None:
            return None
        observation = (
            CadAcquisitionOverloadObservation.model_validate_json(
                row['payload_json']
            )
        )
        if (
            observation.observation_id != row['observation_id']
            or observation.observation_sha256 != row['observation_sha256']
            or observation.document_id != row['document_id']
            or observation.chain_ref.ref_id != row['chain_ref_id']
            or observation.overload_mechanism
            != row['overload_mechanism']
            or observation.declared_at_utc != row['declared_at_utc']
        ):
            raise MeasChainAuthorityIntegrityError(
                'overload observation row disagrees with payload'
            )
        return observation

    def list_observations(
        self, document_id: str
    ) -> tuple[CadAcquisitionOverloadObservation, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_measchain_overload_observations '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadAcquisitionOverloadObservation.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Qualifications

    def save_qualification(
        self, qualification: CadMeasChainQualification
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
            raise MeasChainAuthorityConflictError(
                'measchain qualifications are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_measchain_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    chain_ref_id, state, requested_class,
                    evaluation_version, evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.chain_ref.ref_id,
                    qualification.state,
                    qualification.requested_class,
                    qualification.evaluation_version,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> CadMeasChainQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_measchain_qualifications '
                'WHERE qualification_id=?',
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = CadMeasChainQualification.model_validate_json(
            row['payload_json']
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.chain_ref.ref_id != row['chain_ref_id']
            or qualification.state != row['state']
            or qualification.requested_class != row['requested_class']
            or qualification.evaluation_version
            != row['evaluation_version']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise MeasChainAuthorityIntegrityError(
                'measchain qualification row disagrees with payload'
            )
        return qualification

    def list_qualifications(
        self, document_id: str
    ) -> tuple[CadMeasChainQualification, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_measchain_qualifications '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadMeasChainQualification.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )


__all__ = [
    'CadMeasChainLinearityRepository',
    'MeasChainAuthorityConflictError',
    'MeasChainAuthorityIntegrityError',
]
