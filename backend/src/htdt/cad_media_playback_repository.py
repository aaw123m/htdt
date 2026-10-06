"""Append-only persistence for the media-playback capability authority
(#632, REV57-AUD).

Five tables:

* ``cad_playback_stack_identities`` — sealed exact playback-stack
  fingerprints.
* ``cad_media_profile_requirements`` — sealed exact media profiles.
* ``cad_playback_capability_records`` — sealed (stack × media) capability
  evidence.
* ``cad_playback_operation_runs`` — sealed scenario-bounded operation
  tests.
* ``cad_playback_qualifications`` — sealed fail-closed verdicts.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_media_playback_authority import (
    CadMediaProfileRequirement,
    CadPlaybackCapabilityRecord,
    CadPlaybackOperationRun,
    CadPlaybackQualification,
    CadPlaybackStackIdentity,
)


class MediaPlaybackConflictError(ValueError):
    """A media-playback save violated append-only identity rules."""


class MediaPlaybackIntegrityError(ValueError):
    """A stored media-playback row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise MediaPlaybackIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise MediaPlaybackIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadMediaPlaybackRepository:
    """Native storage for the #632 media-playback authority."""

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
                'cad_playback_stack_identities',
                'cad_media_profile_requirements',
                'cad_playback_capability_records',
                'cad_playback_operation_runs',
                'cad_playback_qualifications',
            )

    # ------------------------------------------------------------------
    # Stack identities

    def save_stack(self, stack: CadPlaybackStackIdentity) -> None:
        _assert_sealed(stack, 'stack_sha256', 'stack_id')
        existing = self.get_stack(stack.stack_id)
        if existing is not None:
            if existing.stack_sha256 == stack.stack_sha256:
                return
            raise MediaPlaybackConflictError(
                'playback stack identities are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_playback_stack_identities (
                    stack_id, stack_sha256, document_id, source_class,
                    device_identity, app_name, app_version, os_version,
                    firmware, observed_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stack.stack_id,
                    stack.stack_sha256,
                    stack.document_id,
                    stack.source_class,
                    stack.device_identity,
                    stack.app_name,
                    stack.app_version,
                    stack.os_version,
                    stack.firmware,
                    stack.observed_at_utc,
                    stack.model_dump_json(),
                ),
            )

    def get_stack(
        self, stack_id: str
    ) -> CadPlaybackStackIdentity | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_playback_stack_identities '
                'WHERE stack_id=?',
                (stack_id,),
            ).fetchone()
        if row is None:
            return None
        stack = CadPlaybackStackIdentity.model_validate_json(
            row['payload_json']
        )
        if (
            stack.stack_id != row['stack_id']
            or stack.stack_sha256 != row['stack_sha256']
            or stack.document_id != row['document_id']
            or stack.source_class != row['source_class']
            or stack.device_identity != row['device_identity']
            or stack.app_name != row['app_name']
            or stack.app_version != row['app_version']
            or stack.os_version != row['os_version']
            or stack.firmware != row['firmware']
            or stack.observed_at_utc != row['observed_at_utc']
        ):
            raise MediaPlaybackIntegrityError(
                'stored stack row disagrees with its payload'
            )
        return stack

    def list_stacks(
        self, document_id: str | None = None
    ) -> tuple[CadPlaybackStackIdentity, ...]:
        query = 'SELECT payload_json FROM cad_playback_stack_identities'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            CadPlaybackStackIdentity.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # Media requirements

    def save_requirement(
        self, requirement: CadMediaProfileRequirement
    ) -> None:
        _assert_sealed(
            requirement, 'requirement_sha256', 'requirement_id'
        )
        existing = self.get_requirement(requirement.requirement_id)
        if existing is not None:
            if existing.requirement_sha256 == (
                requirement.requirement_sha256
            ):
                return
            raise MediaPlaybackConflictError(
                'media profile requirements are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_media_profile_requirements (
                    requirement_id, requirement_sha256, document_id,
                    label, delivery_class, video_codec, audio_codec,
                    encryption_requirement, declared_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    requirement.requirement_id,
                    requirement.requirement_sha256,
                    requirement.document_id,
                    requirement.label,
                    requirement.delivery_class,
                    requirement.video_codec,
                    requirement.audio_codec,
                    requirement.encryption_requirement,
                    requirement.declared_at_utc,
                    requirement.model_dump_json(),
                ),
            )

    def get_requirement(
        self, requirement_id: str
    ) -> CadMediaProfileRequirement | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_media_profile_requirements '
                'WHERE requirement_id=?',
                (requirement_id,),
            ).fetchone()
        if row is None:
            return None
        requirement = CadMediaProfileRequirement.model_validate_json(
            row['payload_json']
        )
        if (
            requirement.requirement_id != row['requirement_id']
            or requirement.requirement_sha256
            != row['requirement_sha256']
            or requirement.document_id != row['document_id']
            or requirement.label != row['label']
            or requirement.delivery_class != row['delivery_class']
            or requirement.video_codec != row['video_codec']
            or requirement.audio_codec != row['audio_codec']
            or requirement.encryption_requirement
            != row['encryption_requirement']
            or requirement.declared_at_utc != row['declared_at_utc']
        ):
            raise MediaPlaybackIntegrityError(
                'stored media requirement row disagrees with its payload'
            )
        return requirement

    def list_requirements(
        self, document_id: str | None = None
    ) -> tuple[CadMediaProfileRequirement, ...]:
        query = (
            'SELECT payload_json FROM cad_media_profile_requirements'
        )
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            CadMediaProfileRequirement.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # Capability records

    def save_record(self, record: CadPlaybackCapabilityRecord) -> None:
        _assert_sealed(record, 'record_sha256', 'record_id')
        existing = self.get_record(record.record_id)
        if existing is not None:
            if existing.record_sha256 == record.record_sha256:
                return
            raise MediaPlaybackConflictError(
                'playback capability records are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_playback_capability_records (
                    record_id, record_sha256, document_id, stack_ref_id,
                    media_ref_id, capability_class, evidence_class,
                    output_state, failure_attribution, observed_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.record_id,
                    record.record_sha256,
                    record.document_id,
                    record.stack_ref.ref_id,
                    record.media_ref.ref_id,
                    record.capability_class,
                    record.evidence_class,
                    (
                        record.observation.output_state
                        if record.observation is not None
                        else None
                    ),
                    record.failure_attribution,
                    record.observed_at_utc,
                    record.model_dump_json(),
                ),
            )

    def get_record(
        self, record_id: str
    ) -> CadPlaybackCapabilityRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_playback_capability_records '
                'WHERE record_id=?',
                (record_id,),
            ).fetchone()
        if row is None:
            return None
        record = CadPlaybackCapabilityRecord.model_validate_json(
            row['payload_json']
        )
        observed_state = (
            record.observation.output_state
            if record.observation is not None
            else None
        )
        if (
            record.record_id != row['record_id']
            or record.record_sha256 != row['record_sha256']
            or record.document_id != row['document_id']
            or record.stack_ref.ref_id != row['stack_ref_id']
            or record.media_ref.ref_id != row['media_ref_id']
            or record.capability_class != row['capability_class']
            or record.evidence_class != row['evidence_class']
            or observed_state != row['output_state']
            or record.failure_attribution != row['failure_attribution']
            or record.observed_at_utc != row['observed_at_utc']
        ):
            raise MediaPlaybackIntegrityError(
                'stored capability record row disagrees with its payload'
            )
        return record

    def list_records(
        self, document_id: str | None = None
    ) -> tuple[CadPlaybackCapabilityRecord, ...]:
        query = 'SELECT payload_json FROM cad_playback_capability_records'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            CadPlaybackCapabilityRecord.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # Operation runs

    def save_run(self, run: CadPlaybackOperationRun) -> None:
        _assert_sealed(run, 'run_sha256', 'run_id')
        existing = self.get_run(run.run_id)
        if existing is not None:
            if existing.run_sha256 == run.run_sha256:
                return
            raise MediaPlaybackConflictError(
                'playback operation runs are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_playback_operation_runs (
                    run_id, run_sha256, document_id, stack_ref_id,
                    media_ref_id, scenario, duration_s,
                    operation_count, started_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.run_sha256,
                    run.document_id,
                    run.stack_ref.ref_id,
                    run.media_ref.ref_id,
                    run.scenario,
                    run.duration_s,
                    len(run.operations),
                    run.started_at_utc,
                    run.model_dump_json(),
                ),
            )

    def get_run(
        self, run_id: str
    ) -> CadPlaybackOperationRun | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_playback_operation_runs WHERE run_id=?',
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        run = CadPlaybackOperationRun.model_validate_json(
            row['payload_json']
        )
        if (
            run.run_id != row['run_id']
            or run.run_sha256 != row['run_sha256']
            or run.document_id != row['document_id']
            or run.stack_ref.ref_id != row['stack_ref_id']
            or run.media_ref.ref_id != row['media_ref_id']
            or run.scenario != row['scenario']
            or run.duration_s != row['duration_s']
            or len(run.operations) != row['operation_count']
            or run.started_at_utc != row['started_at_utc']
        ):
            raise MediaPlaybackIntegrityError(
                'stored operation run row disagrees with its payload'
            )
        return run

    def list_runs(
        self, document_id: str | None = None
    ) -> tuple[CadPlaybackOperationRun, ...]:
        query = 'SELECT payload_json FROM cad_playback_operation_runs'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            CadPlaybackOperationRun.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # Qualifications

    def save_qualification(
        self, qualification: CadPlaybackQualification
    ) -> None:
        _assert_sealed(
            qualification, 'qualification_sha256', 'qualification_id'
        )
        existing = self.get_qualification(qualification.qualification_id)
        if existing is not None:
            if existing.qualification_sha256 == (
                qualification.qualification_sha256
            ):
                return
            raise MediaPlaybackConflictError(
                'playback qualifications are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_playback_qualifications (
                    qualification_id, qualification_sha256, document_id,
                    stack_ref_id, media_ref_id, verdict,
                    evaluation_version, evaluated_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qualification.qualification_id,
                    qualification.qualification_sha256,
                    qualification.document_id,
                    qualification.stack_ref.ref_id,
                    qualification.media_ref.ref_id,
                    qualification.verdict,
                    qualification.evaluation_version,
                    qualification.evaluated_at_utc,
                    qualification.model_dump_json(),
                ),
            )

    def get_qualification(
        self, qualification_id: str
    ) -> CadPlaybackQualification | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT * FROM cad_playback_qualifications '
                'WHERE qualification_id=?',
                (qualification_id,),
            ).fetchone()
        if row is None:
            return None
        qualification = CadPlaybackQualification.model_validate_json(
            row['payload_json']
        )
        if (
            qualification.qualification_id != row['qualification_id']
            or qualification.qualification_sha256
            != row['qualification_sha256']
            or qualification.document_id != row['document_id']
            or qualification.stack_ref.ref_id != row['stack_ref_id']
            or qualification.media_ref.ref_id != row['media_ref_id']
            or qualification.verdict != row['verdict']
            or qualification.evaluation_version
            != row['evaluation_version']
            or qualification.evaluated_at_utc != row['evaluated_at_utc']
        ):
            raise MediaPlaybackIntegrityError(
                'stored qualification row disagrees with its payload'
            )
        return qualification

    def list_qualifications(
        self, document_id: str | None = None
    ) -> tuple[CadPlaybackQualification, ...]:
        query = 'SELECT payload_json FROM cad_playback_qualifications'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            CadPlaybackQualification.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )
