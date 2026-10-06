"""Append-only persistence for the DSP filter-realization authority
(#679, REV58-DSPDECAY).

Four tables:

* ``cad_dsp_realization_profiles`` — sealed device/DSP realization
  profiles.
* ``cad_dsp_stage_records`` — sealed design→realization stage records.
* ``cad_dsp_parameter_mappings`` — sealed deterministic parameter
  mappings.
* ``cad_dsp_realization_qualifications`` — sealed fail-closed verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_dsp_realization import (
    CadDspParameterMapping,
    CadDspRealizationProfile,
    CadDspRealizationQualification,
    CadDspStageRecord,
)


class DspRealizationConflictError(ValueError):
    """A dsp-realization save violated append-only identity rules."""


class DspRealizationIntegrityError(ValueError):
    """A stored dsp-realization row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise DspRealizationIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise DspRealizationIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadDspRealizationRepository:
    """Native storage for the #679 DSP realization authority records."""

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
                'cad_dsp_realization_profiles',
                'cad_dsp_stage_records',
                'cad_dsp_parameter_mappings',
                'cad_dsp_realization_qualifications',
            )

    # ------------------------------------------------------------------
    # Realization profiles

    def save_profile(self, profile: CadDspRealizationProfile) -> None:
        _assert_sealed(profile, 'profile_sha256', 'profile_id')
        existing = self.get_profile(profile.profile_id)
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise DspRealizationConflictError(
                'dsp realization profiles are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_dsp_realization_profiles (
                    profile_id, profile_sha256, document_id,
                    device_identity, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.profile_sha256,
                    profile.document_id,
                    profile.device_identity,
                    profile.declared_at_utc,
                    profile.model_dump_json(),
                ),
            )

    def get_profile(
        self, profile_id: str
    ) -> CadDspRealizationProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_dsp_realization_profiles '
                'WHERE profile_id=?',
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        profile = CadDspRealizationProfile.model_validate_json(
            row['payload_json']
        )
        if (
            profile.profile_id != row['profile_id']
            or profile.profile_sha256 != row['profile_sha256']
            or profile.document_id != row['document_id']
            or profile.device_identity != row['device_identity']
            or profile.declared_at_utc != row['declared_at_utc']
        ):
            raise DspRealizationIntegrityError(
                'dsp realization profile row disagrees with payload'
            )
        return profile

    def list_profiles(
        self, document_id: str
    ) -> tuple[CadDspRealizationProfile, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_dsp_realization_profiles '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadDspRealizationProfile.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Stage records

    def save_stage(self, stage: CadDspStageRecord) -> None:
        _assert_sealed(stage, 'stage_sha256', 'stage_id')
        existing = self.get_stage(stage.stage_id)
        if existing is not None:
            if existing.stage_sha256 == stage.stage_sha256:
                return
            raise DspRealizationConflictError(
                'dsp stage records are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_dsp_stage_records (
                    stage_id, stage_sha256, document_id,
                    stage_kind, bank_label, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stage.stage_id,
                    stage.stage_sha256,
                    stage.document_id,
                    stage.stage_kind,
                    stage.bank_label,
                    stage.declared_at_utc,
                    stage.model_dump_json(),
                ),
            )

    def get_stage(self, stage_id: str) -> CadDspStageRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_dsp_stage_records WHERE stage_id=?',
                (stage_id,),
            ).fetchone()
        if row is None:
            return None
        stage = CadDspStageRecord.model_validate_json(row['payload_json'])
        if (
            stage.stage_id != row['stage_id']
            or stage.stage_sha256 != row['stage_sha256']
            or stage.document_id != row['document_id']
            or stage.stage_kind != row['stage_kind']
            or stage.bank_label != row['bank_label']
            or stage.declared_at_utc != row['declared_at_utc']
        ):
            raise DspRealizationIntegrityError(
                'dsp stage row disagrees with payload'
            )
        return stage

    def list_stages(
        self, document_id: str
    ) -> tuple[CadDspStageRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_dsp_stage_records '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadDspStageRecord.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Parameter mappings

    def save_mapping(self, mapping: CadDspParameterMapping) -> None:
        _assert_sealed(mapping, 'mapping_sha256', 'mapping_id')
        existing = self.get_mapping(mapping.mapping_id)
        if existing is not None:
            if existing.mapping_sha256 == mapping.mapping_sha256:
                return
            raise DspRealizationConflictError(
                'dsp parameter mappings are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_dsp_parameter_mappings (
                    mapping_id, mapping_sha256, document_id,
                    profile_ref_id, bank_label, state,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mapping.mapping_id,
                    mapping.mapping_sha256,
                    mapping.document_id,
                    mapping.profile_ref.ref_id,
                    mapping.bank_label,
                    mapping.state,
                    mapping.declared_at_utc,
                    mapping.model_dump_json(),
                ),
            )

    def get_mapping(
        self, mapping_id: str
    ) -> CadDspParameterMapping | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_dsp_parameter_mappings '
                'WHERE mapping_id=?',
                (mapping_id,),
            ).fetchone()
        if row is None:
            return None
        mapping = CadDspParameterMapping.model_validate_json(
            row['payload_json']
        )
        if (
            mapping.mapping_id != row['mapping_id']
            or mapping.mapping_sha256 != row['mapping_sha256']
            or mapping.document_id != row['document_id']
            or mapping.profile_ref.ref_id != row['profile_ref_id']
            or mapping.bank_label != row['bank_label']
            or mapping.state != row['state']
            or mapping.declared_at_utc != row['declared_at_utc']
        ):
            raise DspRealizationIntegrityError(
                'dsp mapping row disagrees with payload'
            )
        return mapping

    def list_mappings(
        self, document_id: str
    ) -> tuple[CadDspParameterMapping, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_dsp_parameter_mappings '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadDspParameterMapping.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Qualifications

    def save_qualification(
        self, qualification: CadDspRealizationQualification
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
            raise DspRealizationConflictError(
                'dsp realization qualifications are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_dsp_realization_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    profile_ref_id, state, transfer_verification,
                    evaluation_version, evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.profile_ref.ref_id,
                    qualification.state,
                    qualification.transfer_verification,
                    qualification.evaluation_version,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> CadDspRealizationQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_dsp_realization_qualifications '
                'WHERE qualification_id=?',
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = (
            CadDspRealizationQualification.model_validate_json(
                row['payload_json']
            )
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.profile_ref.ref_id != row['profile_ref_id']
            or qualification.state != row['state']
            or qualification.transfer_verification
            != row['transfer_verification']
            or qualification.evaluation_version
            != row['evaluation_version']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise DspRealizationIntegrityError(
                'dsp qualification row disagrees with payload'
            )
        return qualification

    def list_qualifications(
        self, document_id: str
    ) -> tuple[CadDspRealizationQualification, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM '
                'cad_dsp_realization_qualifications '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadDspRealizationQualification.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )


__all__ = [
    'CadDspRealizationRepository',
    'DspRealizationConflictError',
    'DspRealizationIntegrityError',
]
