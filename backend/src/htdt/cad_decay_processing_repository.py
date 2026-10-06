"""Append-only persistence for the decay-curve noise / truncation
processing authority (#676, REV58-DSPDECAY).

Five tables:

* ``cad_decay_processing_profiles`` — sealed processing identities.
* ``cad_decay_noise_estimates`` — sealed background-noise estimates.
* ``cad_decay_truncation_decisions`` — sealed RIR/EDC truncation
  decisions.
* ``cad_decay_edc_artifacts`` — sealed retained EDC artifacts (raw vs
  corrected lineage).
* ``cad_decay_fit_records`` — sealed scalar decay metrics with
  eligibility verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_decay_processing import (
    CadDecayEdcArtifact,
    CadDecayFitRecord,
    CadDecayNoiseEstimate,
    CadDecayProcessingProfile,
    CadRirTruncationDecision,
)


class DecayProcessingConflictError(ValueError):
    """A decay-processing save violated append-only identity rules."""


class DecayProcessingIntegrityError(ValueError):
    """A stored decay-processing row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise DecayProcessingIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise DecayProcessingIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadDecayProcessingRepository:
    """Native storage for the #676 decay-processing authority records."""

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
                'cad_decay_processing_profiles',
                'cad_decay_noise_estimates',
                'cad_decay_truncation_decisions',
                'cad_decay_edc_artifacts',
                'cad_decay_fit_records',
            )

    # ------------------------------------------------------------------
    # Processing profiles

    def save_profile(
        self, profile: CadDecayProcessingProfile
    ) -> None:
        _assert_sealed(profile, 'profile_sha256', 'profile_id')
        existing = self.get_profile(profile.profile_id)
        if existing is not None:
            if existing.profile_sha256 == profile.profile_sha256:
                return
            raise DecayProcessingConflictError(
                'decay processing profiles are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_decay_processing_profiles (
                    profile_id, profile_sha256, document_id,
                    profile_label, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_id,
                    profile.profile_sha256,
                    profile.document_id,
                    profile.profile_label,
                    profile.declared_at_utc,
                    profile.model_dump_json(),
                ),
            )

    def get_profile(
        self, profile_id: str
    ) -> CadDecayProcessingProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_decay_processing_profiles '
                'WHERE profile_id=?',
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        profile = CadDecayProcessingProfile.model_validate_json(
            row['payload_json']
        )
        if (
            profile.profile_id != row['profile_id']
            or profile.profile_sha256 != row['profile_sha256']
            or profile.document_id != row['document_id']
            or profile.profile_label != row['profile_label']
            or profile.declared_at_utc != row['declared_at_utc']
        ):
            raise DecayProcessingIntegrityError(
                'decay profile row disagrees with payload'
            )
        return profile

    def list_profiles(
        self, document_id: str
    ) -> tuple[CadDecayProcessingProfile, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_decay_processing_profiles '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadDecayProcessingProfile.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Noise estimates

    def save_noise_estimate(
        self, estimate: CadDecayNoiseEstimate
    ) -> None:
        _assert_sealed(estimate, 'estimate_sha256', 'estimate_id')
        existing = self.get_noise_estimate(estimate.estimate_id)
        if existing is not None:
            if existing.estimate_sha256 == estimate.estimate_sha256:
                return
            raise DecayProcessingConflictError(
                'decay noise estimates are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_decay_noise_estimates (
                    estimate_id, estimate_sha256, document_id,
                    rir_ref_id, method, stationarity, level_db,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    estimate.estimate_id,
                    estimate.estimate_sha256,
                    estimate.document_id,
                    estimate.rir_ref.ref_id,
                    estimate.method,
                    estimate.stationarity,
                    estimate.level_db,
                    estimate.declared_at_utc,
                    estimate.model_dump_json(),
                ),
            )

    def get_noise_estimate(
        self, estimate_id: str
    ) -> CadDecayNoiseEstimate | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_decay_noise_estimates '
                'WHERE estimate_id=?',
                (estimate_id,),
            ).fetchone()
        if row is None:
            return None
        estimate = CadDecayNoiseEstimate.model_validate_json(
            row['payload_json']
        )
        if (
            estimate.estimate_id != row['estimate_id']
            or estimate.estimate_sha256 != row['estimate_sha256']
            or estimate.document_id != row['document_id']
            or estimate.rir_ref.ref_id != row['rir_ref_id']
            or estimate.method != row['method']
            or estimate.stationarity != row['stationarity']
            or estimate.level_db != row['level_db']
            or estimate.declared_at_utc != row['declared_at_utc']
        ):
            raise DecayProcessingIntegrityError(
                'decay noise estimate row disagrees with payload'
            )
        return estimate

    def list_noise_estimates(
        self, document_id: str
    ) -> tuple[CadDecayNoiseEstimate, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_decay_noise_estimates '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadDecayNoiseEstimate.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Truncation decisions

    def save_truncation_decision(
        self, decision: CadRirTruncationDecision
    ) -> None:
        _assert_sealed(decision, 'decision_sha256', 'decision_id')
        existing = self.get_truncation_decision(decision.decision_id)
        if existing is not None:
            if existing.decision_sha256 == decision.decision_sha256:
                return
            raise DecayProcessingConflictError(
                'rir truncation decisions are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_decay_truncation_decisions (
                    decision_id, decision_sha256, document_id,
                    rir_ref_id, truncation_time_s, reason,
                    capture_truncated, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.decision_id,
                    decision.decision_sha256,
                    decision.document_id,
                    decision.rir_ref.ref_id,
                    decision.truncation_time_s,
                    decision.reason,
                    1 if decision.capture_truncated else 0,
                    decision.declared_at_utc,
                    decision.model_dump_json(),
                ),
            )

    def get_truncation_decision(
        self, decision_id: str
    ) -> CadRirTruncationDecision | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_decay_truncation_decisions '
                'WHERE decision_id=?',
                (decision_id,),
            ).fetchone()
        if row is None:
            return None
        decision = CadRirTruncationDecision.model_validate_json(
            row['payload_json']
        )
        if (
            decision.decision_id != row['decision_id']
            or decision.decision_sha256 != row['decision_sha256']
            or decision.document_id != row['document_id']
            or decision.rir_ref.ref_id != row['rir_ref_id']
            or decision.truncation_time_s != row['truncation_time_s']
            or decision.reason != row['reason']
            or (1 if decision.capture_truncated else 0)
            != row['capture_truncated']
            or decision.declared_at_utc != row['declared_at_utc']
        ):
            raise DecayProcessingIntegrityError(
                'truncation decision row disagrees with payload'
            )
        return decision

    def list_truncation_decisions(
        self, document_id: str
    ) -> tuple[CadRirTruncationDecision, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_decay_truncation_decisions '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadRirTruncationDecision.model_validate_json(
                r['payload_json']
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # EDC artifacts

    def save_edc_artifact(self, artifact: CadDecayEdcArtifact) -> None:
        _assert_sealed(artifact, 'artifact_sha256', 'artifact_id')
        existing = self.get_edc_artifact(artifact.artifact_id)
        if existing is not None:
            if existing.artifact_sha256 == artifact.artifact_sha256:
                return
            raise DecayProcessingConflictError(
                'decay edc artifacts are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_decay_edc_artifacts (
                    artifact_id, artifact_sha256, document_id,
                    rir_ref_id, edc_kind, content_sha256,
                    declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.artifact_id,
                    artifact.artifact_sha256,
                    artifact.document_id,
                    artifact.rir_ref.ref_id,
                    artifact.edc_kind,
                    artifact.content_sha256,
                    artifact.declared_at_utc,
                    artifact.model_dump_json(),
                ),
            )

    def get_edc_artifact(
        self, artifact_id: str
    ) -> CadDecayEdcArtifact | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_decay_edc_artifacts '
                'WHERE artifact_id=?',
                (artifact_id,),
            ).fetchone()
        if row is None:
            return None
        artifact = CadDecayEdcArtifact.model_validate_json(
            row['payload_json']
        )
        if (
            artifact.artifact_id != row['artifact_id']
            or artifact.artifact_sha256 != row['artifact_sha256']
            or artifact.document_id != row['document_id']
            or artifact.rir_ref.ref_id != row['rir_ref_id']
            or artifact.edc_kind != row['edc_kind']
            or artifact.content_sha256 != row['content_sha256']
            or artifact.declared_at_utc != row['declared_at_utc']
        ):
            raise DecayProcessingIntegrityError(
                'edc artifact row disagrees with payload'
            )
        return artifact

    def list_edc_artifacts(
        self, document_id: str
    ) -> tuple[CadDecayEdcArtifact, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_decay_edc_artifacts '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadDecayEdcArtifact.model_validate_json(r['payload_json'])
            for r in rows
        )

    # ------------------------------------------------------------------
    # Fit records

    def save_fit_record(self, record: CadDecayFitRecord) -> None:
        _assert_sealed(record, 'record_sha256', 'record_id')
        existing = self.get_fit_record(record.record_id)
        if existing is not None:
            if existing.record_sha256 == record.record_sha256:
                return
            raise DecayProcessingConflictError(
                'decay fit records are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_decay_fit_records (
                    record_id, record_sha256, document_id,
                    rir_ref_id, metric, value_s, eligibility,
                    evaluation_version, evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.record_id,
                    record.record_sha256,
                    record.document_id,
                    record.rir_ref.ref_id,
                    record.metric,
                    record.value_s,
                    record.eligibility,
                    record.evaluation_version,
                    record.evaluated_at_utc,
                    record.model_dump_json(),
                ),
            )

    def get_fit_record(
        self, record_id: str
    ) -> CadDecayFitRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_decay_fit_records WHERE record_id=?',
                (record_id,),
            ).fetchone()
        if row is None:
            return None
        record = CadDecayFitRecord.model_validate_json(
            row['payload_json']
        )
        if (
            record.record_id != row['record_id']
            or record.record_sha256 != row['record_sha256']
            or record.document_id != row['document_id']
            or record.rir_ref.ref_id != row['rir_ref_id']
            or record.metric != row['metric']
            or record.value_s != row['value_s']
            or record.eligibility != row['eligibility']
            or record.evaluation_version != row['evaluation_version']
            or record.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise DecayProcessingIntegrityError(
                'decay fit record row disagrees with payload'
            )
        return record

    def list_fit_records(
        self, document_id: str
    ) -> tuple[CadDecayFitRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT payload_json FROM cad_decay_fit_records '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(
            CadDecayFitRecord.model_validate_json(r['payload_json'])
            for r in rows
        )


__all__ = [
    'CadDecayProcessingRepository',
    'DecayProcessingConflictError',
    'DecayProcessingIntegrityError',
]
