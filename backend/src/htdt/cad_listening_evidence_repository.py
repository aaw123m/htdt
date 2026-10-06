"""Append-only persistence for REV59-LISTENEXP authorities.

Nine tables in one repository — listening experiments (#696),
assistive listening (#726), dynamic binaural (#727).
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .canonical_json import canonical_sha256
from .cad_listening_experiment import (
    ListeningExperimentPlan,
    ListenerQualification,
    SubjectiveInferenceRecord,
)
from .cad_assistive_listening import (
    ALSQualification,
    AssistiveListeningPath,
    ReceiverCompatibilityEvidence,
)
from .cad_binaural_dynamic import (
    BinauralQualification,
    DynamicBinauralSession,
    PoseTrackingEvidence,
)


class ListeningEvidenceConflictError(ValueError):
    """A LISTENEXP save violated append-only identity rules."""


class ListeningEvidenceIntegrityError(ValueError):
    """A stored LISTENEXP row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise ListeningEvidenceIntegrityError(
            'record payload does not match its sealed sha256'
        )
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise ListeningEvidenceIntegrityError(
            'record id does not match its sealed sha256'
        )


class _SealedStore:
    """Generic append-only store for one sealed record type."""

    def __init__(
        self,
        connection_factory: Any,
        table: str,
        model: type,
        id_field: str,
        sha_field: str,
        columns: tuple[tuple[str, str], ...],
    ) -> None:
        self._connect = connection_factory
        self.table = table
        self.model = model
        self.id_field = id_field
        self.sha_field = sha_field
        self.columns = columns

    def _column_value(self, record: Any, path: str) -> Any:
        if path == '__document_id__':
            return record.document_id
        value: Any = record
        for part in path.split('.'):
            value = getattr(value, part)
            if value is None:
                return None
        return value

    def save(self, record: Any) -> None:
        _assert_sealed(record, self.sha_field, self.id_field)
        rid = getattr(record, self.id_field)
        existing = self.get(rid)
        if existing is not None:
            if getattr(existing, self.sha_field) == getattr(
                record, self.sha_field
            ):
                return
            raise ListeningEvidenceConflictError(
                f'{self.table} records are append-only'
            )
        cols = ', '.join(
            [self.id_field, self.sha_field]
            + [c[0] for c in self.columns]
            + ['payload_json']
        )
        placeholders = ', '.join(['?'] * (2 + len(self.columns) + 1))
        values = (
            rid,
            getattr(record, self.sha_field),
            *(
                self._column_value(record, path)
                for _, path in self.columns
            ),
            record.model_dump_json(),
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                f'INSERT INTO {self.table} ({cols}) '
                f'VALUES ({placeholders})',
                values,
            )

    def get(self, rid: str) -> Any | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                f'SELECT * FROM {self.table} WHERE {self.id_field}=?',
                (rid,),
            ).fetchone()
        if row is None:
            return None
        record = self.model.model_validate_json(row['payload_json'])
        if getattr(record, self.id_field) != row[self.id_field]:
            raise ListeningEvidenceIntegrityError(
                f'stored {self.table} id disagrees with its payload'
            )
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise ListeningEvidenceIntegrityError(
                f'stored {self.table} sha disagrees with its payload'
            )
        for column, path in self.columns:
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise ListeningEvidenceIntegrityError(
                    f'stored {self.table}.{column} disagrees '
                    'with its payload'
                )
        return record

    def list(self, document_id: str | None = None) -> tuple[Any, ...]:
        query = f'SELECT payload_json FROM {self.table}'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(
            self.model.model_validate_json(r['payload_json'])
            for r in rows
        )


def _ref(column: str, path: str) -> tuple[str, str]:
    return (column, f'{path}.ref_id')


class CadListeningEvidenceRepository:
    """Native storage for the #696/#726/#727 authorities."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_listening_experiment_plans',
                'cad_listener_qualifications',
                'cad_subjective_inference_records',
                'cad_assistive_listening_paths',
                'cad_als_qualifications',
                'cad_receiver_compatibility_evidence',
                'cad_dynamic_binaural_sessions',
                'cad_pose_tracking_evidence',
                'cad_binaural_qualifications',
            )

        self.listening_plans = _SealedStore(
            self._connect, 'cad_listening_experiment_plans',
            ListeningExperimentPlan, 'plan_id', 'plan_sha256',
            (
                ('document_id', '__document_id__'),
                ('method_kind', 'method_kind'),
                ('impairment_regime', 'impairment_regime'),
            ),
        )

        self.listener_qualifications = _SealedStore(
            self._connect, 'cad_listener_qualifications',
            ListenerQualification, 'qualification_id', 'qualification_sha256',
            (
                ('document_id', '__document_id__'),
                ('training_completed', 'training_completed'),
            ),
        )

        self.inference_records = _SealedStore(
            self._connect, 'cad_subjective_inference_records',
            SubjectiveInferenceRecord, 'record_id', 'record_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('plan_ref_id', 'plan_ref'),
                ('verdict', 'verdict'),
            ),
        )

        self.als_paths = _SealedStore(
            self._connect, 'cad_assistive_listening_paths',
            AssistiveListeningPath, 'path_id', 'path_sha256',
            (
                ('document_id', '__document_id__'),
                ('technology', 'technology'),
            ),
        )

        self.als_qualifications = _SealedStore(
            self._connect, 'cad_als_qualifications',
            ALSQualification, 'qualification_id', 'qualification_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('path_ref_id', 'path_ref'),
            ),
        )

        self.receiver_evidence = _SealedStore(
            self._connect, 'cad_receiver_compatibility_evidence',
            ReceiverCompatibilityEvidence, 'evidence_id', 'evidence_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('path_ref_id', 'path_ref'),
                ('compatible', 'compatible'),
            ),
        )

        self.binaural_sessions = _SealedStore(
            self._connect, 'cad_dynamic_binaural_sessions',
            DynamicBinauralSession, 'session_id', 'session_sha256',
            (
                ('document_id', '__document_id__'),
                ('hrtf_class', 'hrtf_class'),
            ),
        )

        self.pose_evidence = _SealedStore(
            self._connect, 'cad_pose_tracking_evidence',
            PoseTrackingEvidence, 'evidence_id', 'evidence_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('session_ref_id', 'session_ref'),
            ),
        )

        self.binaural_qualifications = _SealedStore(
            self._connect, 'cad_binaural_qualifications',
            BinauralQualification, 'qualification_id', 'qualification_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('session_ref_id', 'session_ref'),
                ('verdict', 'verdict'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def save_listening_plan(self, record: ListeningExperimentPlan) -> None:
        self.listening_plans.save(record)

    def get_listening_plan(self, rid: str) -> ListeningExperimentPlan | None:
        return self.listening_plans.get(rid)

    def save_listener_qualification(self, record: ListenerQualification) -> None:
        self.listener_qualifications.save(record)

    def get_listener_qualification(self, rid: str) -> ListenerQualification | None:
        return self.listener_qualifications.get(rid)

    def save_inference_record(self, record: SubjectiveInferenceRecord) -> None:
        self.inference_records.save(record)

    def get_inference_record(self, rid: str) -> SubjectiveInferenceRecord | None:
        return self.inference_records.get(rid)

    def save_als_path(self, record: AssistiveListeningPath) -> None:
        self.als_paths.save(record)

    def get_als_path(self, rid: str) -> AssistiveListeningPath | None:
        return self.als_paths.get(rid)

    def save_als_qualification(self, record: ALSQualification) -> None:
        self.als_qualifications.save(record)

    def get_als_qualification(self, rid: str) -> ALSQualification | None:
        return self.als_qualifications.get(rid)

    def save_receiver_evidence(self, record: ReceiverCompatibilityEvidence) -> None:
        self.receiver_evidence.save(record)

    def get_receiver_evidence(self, rid: str) -> ReceiverCompatibilityEvidence | None:
        return self.receiver_evidence.get(rid)

    def save_binaural_session(self, record: DynamicBinauralSession) -> None:
        self.binaural_sessions.save(record)

    def get_binaural_session(self, rid: str) -> DynamicBinauralSession | None:
        return self.binaural_sessions.get(rid)

    def save_pose_evidence(self, record: PoseTrackingEvidence) -> None:
        self.pose_evidence.save(record)

    def get_pose_evidence(self, rid: str) -> PoseTrackingEvidence | None:
        return self.pose_evidence.get(rid)

    def save_binaural_qualification(self, record: BinauralQualification) -> None:
        self.binaural_qualifications.save(record)

    def get_binaural_qualification(self, rid: str) -> BinauralQualification | None:
        return self.binaural_qualifications.get(rid)
