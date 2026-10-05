"""Append-only persistence for the HDMI design & verification
authority (#583).

Seven tables:

* ``cad_hdmi_signal_profiles`` — sealed required-mode verification
  contracts.
* ``cad_hdmi_edid_artifacts`` — sealed raw-EDID artifact records with
  versioned interpretation.
* ``cad_hdmi_hdcp_observations`` — sealed observable HDCP link states.
* ``cad_hdmi_link_observations`` — sealed link mode/rate/error
  observations.
* ``cad_hdmi_verification_records`` — sealed field-verification
  records binding route + required profile + evidence.
* ``cad_hdmi_qualifications`` — sealed system-level verdicts keeping
  theoretical support distinct from field verification.
* ``cad_rp28_profiles`` — sealed CEDIA/CTA RP28 profile references.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_hdmi_verification import (
    EDIDArtifact,
    HDCPStateObservation,
    HDMIQualification,
    HDMISignalProfile,
    HDMIVerificationRecord,
    LinkStateObservation,
    Rp28VerificationProfile,
)


class HDMIVerificationConflictError(ValueError):
    """An HDMI-verification save violated append-only identity rules."""


class HDMIVerificationIntegrityError(ValueError):
    """A stored HDMI-verification row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha or getattr(record, id_field) != (
        getattr(record, id_field).split(':')[0] + ':' + sha
    ):
        raise HDMIVerificationIntegrityError(
            'record payload does not match its sealed identity'
        )


class CadHDMIVerificationRepository:
    """Native storage for HDMI profiles, observations, verification
    records, qualifications and RP28 profiles."""

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
                'cad_hdmi_signal_profiles',
                'cad_hdmi_edid_artifacts',
                'cad_hdmi_hdcp_observations',
                'cad_hdmi_link_observations',
                'cad_hdmi_verification_records',
                'cad_hdmi_qualifications',
                'cad_rp28_profiles',
            )

    # ------------------------------------------------------------------
    # Signal profiles

    def save_signal_profile(self, profile: HDMISignalProfile) -> None:
        _assert_sealed(profile, 'profile_sha256', 'profile_id')
        existing = self.get_signal_profile(profile.profile_id)
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise HDMIVerificationConflictError(
                'hdmi signal profiles are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_hdmi_signal_profiles (
                    profile_id, profile_sha256, document_id, label,
                    width_px, height_px, refresh_hz, hdcp_required,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.profile_sha256,
                    profile.document_id,
                    profile.label,
                    profile.width_px,
                    profile.height_px,
                    profile.refresh_hz,
                    profile.hdcp_required,
                    profile.created_at_utc,
                    profile.model_dump_json(),
                ),
            )

    def get_signal_profile(
        self, profile_id: str
    ) -> HDMISignalProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM cad_hdmi_signal_profiles
                WHERE profile_id=?
                """,
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        profile = HDMISignalProfile.model_validate_json(
            row['payload_json']
        )
        if (
            profile.profile_id != row['profile_id']
            or profile.profile_sha256 != row['profile_sha256']
            or profile.document_id != row['document_id']
            or profile.label != row['label']
            or profile.width_px != row['width_px']
            or profile.height_px != row['height_px']
            or profile.refresh_hz != row['refresh_hz']
            or profile.hdcp_required != row['hdcp_required']
            or profile.created_at_utc != row['created_at_utc']
        ):
            raise HDMIVerificationIntegrityError(
                'hdmi signal profile row disagrees with payload'
            )
        return profile

    # ------------------------------------------------------------------
    # EDID artifacts

    def save_edid_artifact(self, artifact: EDIDArtifact) -> None:
        _assert_sealed(artifact, 'artifact_sha256', 'artifact_id')
        existing = self.get_edid_artifact(artifact.artifact_id)
        if existing is not None:
            if existing.artifact_sha256 == artifact.artifact_sha256:
                return
            raise HDMIVerificationConflictError(
                'edid artifacts are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_hdmi_edid_artifacts (
                    artifact_id, artifact_sha256, document_id,
                    signal_path_id, signal_path_version,
                    signal_path_sha256, interception_kind,
                    captured_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.artifact_id,
                    artifact.artifact_sha256,
                    artifact.document_id,
                    artifact.signal_path_id,
                    artifact.signal_path_version,
                    artifact.signal_path_sha256,
                    artifact.interception_kind,
                    artifact.captured_at_utc,
                    artifact.model_dump_json(),
                ),
            )

    def get_edid_artifact(
        self, artifact_id: str
    ) -> EDIDArtifact | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM cad_hdmi_edid_artifacts
                WHERE artifact_id=?
                """,
                (artifact_id,),
            ).fetchone()
        if row is None:
            return None
        artifact = EDIDArtifact.model_validate_json(row['payload_json'])
        if (
            artifact.artifact_id != row['artifact_id']
            or artifact.artifact_sha256 != row['artifact_sha256']
            or artifact.document_id != row['document_id']
            or artifact.signal_path_id != row['signal_path_id']
            or artifact.signal_path_version
            != row['signal_path_version']
            or artifact.signal_path_sha256 != row['signal_path_sha256']
            or artifact.interception_kind != row['interception_kind']
            or artifact.captured_at_utc != row['captured_at_utc']
        ):
            raise HDMIVerificationIntegrityError(
                'edid artifact row disagrees with payload'
            )
        return artifact

    def list_edid_artifacts(
        self, signal_path_id: str, signal_path_version: str
    ) -> tuple[EDIDArtifact, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM cad_hdmi_edid_artifacts
                WHERE signal_path_id=? AND signal_path_version=?
                ORDER BY captured_at_utc, artifact_id
                """,
                (signal_path_id, signal_path_version),
            ).fetchall()
        return tuple(
            EDIDArtifact.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # HDCP observations

    def save_hdcp_observation(
        self, observation: HDCPStateObservation
    ) -> None:
        _assert_sealed(
            observation, 'observation_sha256', 'observation_id'
        )
        existing = self.get_hdcp_observation(observation.observation_id)
        if existing is not None:
            if existing.observation_sha256 == observation.observation_sha256:
                return
            raise HDMIVerificationConflictError(
                'hdcp observations are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_hdmi_hdcp_observations (
                    observation_id, observation_sha256, document_id,
                    signal_path_id, signal_path_version,
                    signal_path_sha256, negotiated_version, auth_state,
                    observed_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.observation_sha256,
                    observation.document_id,
                    observation.signal_path_id,
                    observation.signal_path_version,
                    observation.signal_path_sha256,
                    observation.negotiated_version,
                    observation.auth_state,
                    observation.observed_at_utc,
                    observation.model_dump_json(),
                ),
            )

    def get_hdcp_observation(
        self, observation_id: str
    ) -> HDCPStateObservation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM cad_hdmi_hdcp_observations
                WHERE observation_id=?
                """,
                (observation_id,),
            ).fetchone()
        if row is None:
            return None
        observation = HDCPStateObservation.model_validate_json(
            row['payload_json']
        )
        if (
            observation.observation_id != row['observation_id']
            or observation.observation_sha256
            != row['observation_sha256']
            or observation.document_id != row['document_id']
            or observation.signal_path_sha256
            != row['signal_path_sha256']
            or observation.negotiated_version
            != row['negotiated_version']
            or observation.auth_state != row['auth_state']
            or observation.observed_at_utc != row['observed_at_utc']
        ):
            raise HDMIVerificationIntegrityError(
                'hdcp observation row disagrees with payload'
            )
        return observation

    # ------------------------------------------------------------------
    # Link observations

    def save_link_observation(
        self, observation: LinkStateObservation
    ) -> None:
        _assert_sealed(
            observation, 'observation_sha256', 'observation_id'
        )
        existing = self.get_link_observation(observation.observation_id)
        if existing is not None:
            if existing.observation_sha256 == observation.observation_sha256:
                return
            raise HDMIVerificationConflictError(
                'link observations are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_hdmi_link_observations (
                    observation_id, observation_sha256, document_id,
                    signal_path_id, signal_path_version,
                    signal_path_sha256, link_mode,
                    negotiated_rate_gbps, observed_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.observation_sha256,
                    observation.document_id,
                    observation.signal_path_id,
                    observation.signal_path_version,
                    observation.signal_path_sha256,
                    observation.link_mode,
                    observation.negotiated_rate_gbps,
                    observation.observed_at_utc,
                    observation.model_dump_json(),
                ),
            )

    def get_link_observation(
        self, observation_id: str
    ) -> LinkStateObservation | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM cad_hdmi_link_observations
                WHERE observation_id=?
                """,
                (observation_id,),
            ).fetchone()
        if row is None:
            return None
        observation = LinkStateObservation.model_validate_json(
            row['payload_json']
        )
        if (
            observation.observation_id != row['observation_id']
            or observation.observation_sha256
            != row['observation_sha256']
            or observation.document_id != row['document_id']
            or observation.signal_path_sha256
            != row['signal_path_sha256']
            or observation.link_mode != row['link_mode']
            or observation.negotiated_rate_gbps
            != row['negotiated_rate_gbps']
            or observation.observed_at_utc != row['observed_at_utc']
        ):
            raise HDMIVerificationIntegrityError(
                'link observation row disagrees with payload'
            )
        return observation

    # ------------------------------------------------------------------
    # Verification records

    def save_verification_record(
        self, record: HDMIVerificationRecord
    ) -> None:
        _assert_sealed(record, 'record_sha256', 'record_id')
        existing = self.get_verification_record(record.record_id)
        if existing is not None:
            if existing.record_sha256 == record.record_sha256:
                return
            raise HDMIVerificationConflictError(
                'hdmi verification records are append-only'
            )
        if (
            self.get_signal_profile(record.required_profile_id) is None
            or self.get_signal_profile(
                record.required_profile_id
            ).profile_sha256  # type: ignore[union-attr]
            != record.required_profile_sha256
        ):
            raise HDMIVerificationIntegrityError(
                'a verification record must reference a persisted '
                'signal profile revision'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_hdmi_verification_records (
                    record_id, record_sha256, document_id,
                    signal_path_id, signal_path_version,
                    signal_path_sha256, required_profile_id, verdict,
                    observed_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.record_id,
                    record.record_sha256,
                    record.document_id,
                    record.signal_path_id,
                    record.signal_path_version,
                    record.signal_path_sha256,
                    record.required_profile_id,
                    record.verdict,
                    record.observed_at_utc,
                    record.model_dump_json(),
                ),
            )

    def get_verification_record(
        self, record_id: str
    ) -> HDMIVerificationRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM cad_hdmi_verification_records
                WHERE record_id=?
                """,
                (record_id,),
            ).fetchone()
        if row is None:
            return None
        record = HDMIVerificationRecord.model_validate_json(
            row['payload_json']
        )
        if (
            record.record_id != row['record_id']
            or record.record_sha256 != row['record_sha256']
            or record.document_id != row['document_id']
            or record.signal_path_sha256 != row['signal_path_sha256']
            or record.required_profile_id != row['required_profile_id']
            or record.verdict != row['verdict']
            or record.observed_at_utc != row['observed_at_utc']
        ):
            raise HDMIVerificationIntegrityError(
                'hdmi verification record row disagrees with payload'
            )
        return record

    # ------------------------------------------------------------------
    # Qualifications

    def save_qualification(
        self, qualification: HDMIQualification
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
            raise HDMIVerificationConflictError(
                'hdmi qualifications are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_hdmi_qualifications (
                    qualification_id, qualification_sha256,
                    document_id, signal_path_id, signal_path_version,
                    signal_path_sha256,
                    required_profile_id, theoretical_status, verdict,
                    evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.signal_path_id,
                    qualification.signal_path_version,
                    qualification.signal_path_sha256,
                    qualification.required_profile_id,
                    qualification.theoretical_status,
                    qualification.verdict,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> HDMIQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM cad_hdmi_qualifications
                WHERE qualification_id=?
                """,
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = HDMIQualification.model_validate_json(
            row['payload_json']
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.signal_path_sha256
            != row['signal_path_sha256']
            or qualification.required_profile_id
            != row['required_profile_id']
            or qualification.theoretical_status
            != row['theoretical_status']
            or qualification.verdict != row['verdict']
            or qualification.evaluated_at_utc
            != row['evaluated_at_utc']
        ):
            raise HDMIVerificationIntegrityError(
                'hdmi qualification row disagrees with payload'
            )
        return qualification

    # ------------------------------------------------------------------
    # RP28 profiles

    def save_rp28_profile(
        self, profile: Rp28VerificationProfile
    ) -> None:
        _assert_sealed(profile, 'profile_sha256', 'profile_id')
        existing = self.get_rp28_profile(profile.profile_id)
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise HDMIVerificationConflictError(
                'rp28 profiles are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_rp28_profiles (
                    profile_id, profile_sha256, document_id, label,
                    standard_id, edition, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.profile_sha256,
                    profile.document_id,
                    profile.label,
                    profile.standard_id,
                    profile.edition,
                    profile.created_at_utc,
                    profile.model_dump_json(),
                ),
            )

    def get_rp28_profile(
        self, profile_id: str
    ) -> Rp28VerificationProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM cad_rp28_profiles
                WHERE profile_id=?
                """,
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        profile = Rp28VerificationProfile.model_validate_json(
            row['payload_json']
        )
        if (
            profile.profile_id != row['profile_id']
            or profile.profile_sha256 != row['profile_sha256']
            or profile.document_id != row['document_id']
            or profile.label != row['label']
            or profile.standard_id != row['standard_id']
            or profile.edition != row['edition']
            or profile.created_at_utc != row['created_at_utc']
        ):
            raise HDMIVerificationIntegrityError(
                'rp28 profile row disagrees with payload'
            )
        return profile


__all__ = [
    'CadHDMIVerificationRepository',
    'HDMIVerificationConflictError',
    'HDMIVerificationIntegrityError',
]
