"""Append-only persistence for the lifecycle health/drift authority
(#595).

Eight sealed record families live here — every save re-verifies the
payload's content-derived hash (``_assert_sealed``) so a forged
``model_copy`` with a stale digest raises rather than merging:

* ``cad_monitoring_declarations`` — per-device observability +
  collection authorization;
* ``cad_lifecycle_observations`` — telemetry/manual/measurement
  observations (retained forever, including resolved failures);
* ``cad_change_events`` — auditable system changes;
* ``cad_trend_assessments`` — descriptive trend verdicts;
* ``cad_symptom_episodes`` — correlated fault episodes;
* ``cad_drift_assessments`` — per-subject drift verdicts;
* ``cad_reverification_triggers`` — targeted re-verification outputs;
* ``cad_restore_confirmations`` — restore-and-confirm results.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_health_drift import (
    ChangeEventRecord,
    DeviceMonitoringDeclaration,
    DriftAssessment,
    LifecycleObservation,
    RestoreConfirmation,
    ReverificationTrigger,
    SymptomEpisode,
    TrendAssessment,
)
from .cad_schema import require_native_tables, connect_sqlite
from .cad_repository import SceneRepository
from .canonical_json import canonical_sha256 as _hash


class HealthDriftConflictError(ValueError):
    """A save violated append-only identity rules."""


class HealthDriftIntegrityError(ValueError):
    """A stored or incoming payload disagreed with its sealed hash."""


def _assert_sealed(record, *, sha_field: str, payload_fn: str) -> None:
    """Fail-closed re-verification of a sealed record before persist."""

    digest = _hash(getattr(record, payload_fn)())
    if getattr(record, sha_field) != digest:
        raise HealthDriftIntegrityError(
            f'{type(record).__name__} payload diverges from its sealed '
            'hash — re-derive the record instead of mutating a copy'
        )


class CadHealthDriftRepository:
    """Native storage for the #595 lifecycle health/drift records."""

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
                'cad_monitoring_declarations',
                'cad_lifecycle_observations',
                'cad_change_events',
                'cad_trend_assessments',
                'cad_symptom_episodes',
                'cad_drift_assessments',
                'cad_reverification_triggers',
                'cad_restore_confirmations',
            )

    # ------------------------------------------------------------------
    # Generic append-only save/get/list helpers

    def _save(
        self,
        table: str,
        id_column: str,
        record_id: str,
        columns: dict[str, object],
        payload_json: str,
        existing: object | None,
        existing_sha: str | None,
        record_sha: str,
    ) -> None:
        if existing is not None:
            if existing_sha == record_sha:
                return
            raise HealthDriftConflictError(
                f'{table} rows are append-only — a correction is a new '
                'record, never an update in place'
            )
        cols = ', '.join([id_column, *columns.keys(), 'payload_json'])
        marks = ', '.join(['?'] * (len(columns) + 2))
        with closing(self._connect()) as connection, connection:
            connection.execute(
                f'INSERT INTO {table} ({cols}) VALUES ({marks})',
                (record_id, *columns.values(), payload_json),
            )

    @staticmethod
    def _fetch_one(connection, table: str, where: str, params: tuple):
        return connection.execute(
            f'SELECT payload_json FROM {table} WHERE {where} LIMIT 1',
            params,
        ).fetchone()

    @staticmethod
    def _fetch_all(connection, table: str, where: str, params: tuple):
        return connection.execute(
            f'SELECT payload_json FROM {table} WHERE {where} '
            'ORDER BY seq ASC',
            params,
        ).fetchall()

    # ------------------------------------------------------------------
    # Monitoring declarations

    def save_declaration(
        self, declaration: DeviceMonitoringDeclaration
    ) -> None:
        _assert_sealed(
            declaration,
            sha_field='declaration_sha256',
            payload_fn='semantic_payload',
        )
        existing = self.get_declaration(declaration.declaration_id)
        self._save(
            'cad_monitoring_declarations',
            'declaration_id',
            declaration.declaration_id,
            {
                'document_id': declaration.document_id,
                'subject_kind': declaration.subject_ref.kind,
                'subject_ref_id': declaration.subject_ref.ref_id,
                'capability_repr': ','.join(declaration.capabilities),
                'remote_allowed': int(
                    declaration.authorization is not None
                    and declaration.authorization.remote_collection_allowed
                ),
                'declaration_sha256': declaration.declaration_sha256,
                'declared_at_utc': declaration.declared_at_utc,
            },
            declaration.model_dump_json(),
            existing,
            None if existing is None else existing.declaration_sha256,
            declaration.declaration_sha256,
        )

    def get_declaration(
        self, declaration_id: str
    ) -> DeviceMonitoringDeclaration | None:
        with closing(self._connect()) as connection:
            row = self._fetch_one(
                connection,
                'cad_monitoring_declarations',
                'declaration_id=?',
                (declaration_id,),
            )
        if row is None:
            return None
        return DeviceMonitoringDeclaration.model_validate_json(
            row['payload_json']
        )

    def list_declarations(
        self, document_id: str
    ) -> tuple[DeviceMonitoringDeclaration, ...]:
        with closing(self._connect()) as connection:
            rows = self._fetch_all(
                connection,
                'cad_monitoring_declarations',
                'document_id=?',
                (document_id,),
            )
        return tuple(
            DeviceMonitoringDeclaration.model_validate_json(
                row['payload_json']
            )
            for row in rows
        )

    def declaration_for_subject(
        self, document_id: str, kind: str, ref_id: str
    ) -> DeviceMonitoringDeclaration | None:
        """Latest declaration for one subject (declarations append; the
        newest wins as the declared observability state)."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM cad_monitoring_declarations
                WHERE document_id=? AND subject_kind=? AND subject_ref_id=?
                ORDER BY seq DESC LIMIT 1
                """,
                (document_id, kind, ref_id),
            ).fetchone()
        if row is None:
            return None
        return DeviceMonitoringDeclaration.model_validate_json(
            row['payload_json']
        )

    # ------------------------------------------------------------------
    # Observations

    def save_observation(self, observation: LifecycleObservation) -> None:
        _assert_sealed(
            observation,
            sha_field='observation_sha256',
            payload_fn='semantic_payload',
        )
        existing = self.get_observation(observation.observation_id)
        self._save(
            'cad_lifecycle_observations',
            'observation_id',
            observation.observation_id,
            {
                'document_id': observation.document_id,
                'subject_kind': observation.subject_ref.kind,
                'subject_ref_id': observation.subject_ref.ref_id,
                'domain': observation.domain,
                'kind': observation.kind,
                'collection_mode': observation.collection_mode,
                'observation_sha256': observation.observation_sha256,
                'observed_at_utc': observation.observed_at_utc,
            },
            observation.model_dump_json(),
            existing,
            None if existing is None else existing.observation_sha256,
            observation.observation_sha256,
        )

    def get_observation(
        self, observation_id: str
    ) -> LifecycleObservation | None:
        with closing(self._connect()) as connection:
            row = self._fetch_one(
                connection,
                'cad_lifecycle_observations',
                'observation_id=?',
                (observation_id,),
            )
        if row is None:
            return None
        return LifecycleObservation.model_validate_json(row['payload_json'])

    def list_observations(
        self,
        document_id: str,
        *,
        subject_kind: str | None = None,
        subject_ref_id: str | None = None,
        domain: str | None = None,
        kind: str | None = None,
    ) -> tuple[LifecycleObservation, ...]:
        where = ['document_id=?']
        params: list[str] = [document_id]
        if subject_kind is not None:
            where.append('subject_kind=?')
            params.append(subject_kind)
        if subject_ref_id is not None:
            where.append('subject_ref_id=?')
            params.append(subject_ref_id)
        if domain is not None:
            where.append('domain=?')
            params.append(domain)
        if kind is not None:
            where.append('kind=?')
            params.append(kind)
        with closing(self._connect()) as connection:
            rows = self._fetch_all(
                connection,
                'cad_lifecycle_observations',
                ' AND '.join(where),
                tuple(params),
            )
        return tuple(
            LifecycleObservation.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Change events

    def save_change_event(self, event: ChangeEventRecord) -> None:
        _assert_sealed(
            event, sha_field='event_sha256', payload_fn='semantic_payload'
        )
        existing = self.get_change_event(event.event_id)
        self._save(
            'cad_change_events',
            'event_id',
            event.event_id,
            {
                'document_id': event.document_id,
                'kind': event.kind,
                'event_sha256': event.event_sha256,
                'occurred_at_utc': event.occurred_at_utc,
            },
            event.model_dump_json(),
            existing,
            None if existing is None else existing.event_sha256,
            event.event_sha256,
        )

    def get_change_event(self, event_id: str) -> ChangeEventRecord | None:
        with closing(self._connect()) as connection:
            row = self._fetch_one(
                connection,
                'cad_change_events',
                'event_id=?',
                (event_id,),
            )
        if row is None:
            return None
        return ChangeEventRecord.model_validate_json(row['payload_json'])

    def list_change_events(
        self, document_id: str
    ) -> tuple[ChangeEventRecord, ...]:
        with closing(self._connect()) as connection:
            rows = self._fetch_all(
                connection,
                'cad_change_events',
                'document_id=?',
                (document_id,),
            )
        return tuple(
            ChangeEventRecord.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Trend assessments

    def save_trend_assessment(self, assessment: TrendAssessment) -> None:
        _assert_sealed(
            assessment,
            sha_field='assessment_sha256',
            payload_fn='semantic_payload',
        )
        existing = self.get_trend_assessment(assessment.assessment_id)
        self._save(
            'cad_trend_assessments',
            'assessment_id',
            assessment.assessment_id,
            {
                'document_id': assessment.document_id,
                'subject_kind': assessment.subject_ref.kind,
                'subject_ref_id': assessment.subject_ref.ref_id,
                'metric_key': assessment.spec.metric_key,
                'state': assessment.state,
                'assessment_sha256': assessment.assessment_sha256,
                'assessed_at_utc': assessment.assessed_at_utc,
            },
            assessment.model_dump_json(),
            existing,
            None if existing is None else existing.assessment_sha256,
            assessment.assessment_sha256,
        )

    def get_trend_assessment(
        self, assessment_id: str
    ) -> TrendAssessment | None:
        with closing(self._connect()) as connection:
            row = self._fetch_one(
                connection,
                'cad_trend_assessments',
                'assessment_id=?',
                (assessment_id,),
            )
        if row is None:
            return None
        return TrendAssessment.model_validate_json(row['payload_json'])

    def list_trend_assessments(
        self, document_id: str
    ) -> tuple[TrendAssessment, ...]:
        with closing(self._connect()) as connection:
            rows = self._fetch_all(
                connection,
                'cad_trend_assessments',
                'document_id=?',
                (document_id,),
            )
        return tuple(
            TrendAssessment.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Symptom episodes

    def save_symptom_episode(self, episode: SymptomEpisode) -> None:
        _assert_sealed(
            episode,
            sha_field='episode_sha256',
            payload_fn='semantic_payload',
        )
        existing = self.get_symptom_episode(episode.episode_id)
        self._save(
            'cad_symptom_episodes',
            'episode_id',
            episode.episode_id,
            {
                'document_id': episode.document_id,
                'reason_state': episode.reason_state,
                'resolved': int(episode.resolved),
                'episode_sha256': episode.episode_sha256,
                'recorded_at_utc': episode.recorded_at_utc,
            },
            episode.model_dump_json(),
            existing,
            None if existing is None else existing.episode_sha256,
            episode.episode_sha256,
        )

    def get_symptom_episode(self, episode_id: str) -> SymptomEpisode | None:
        with closing(self._connect()) as connection:
            row = self._fetch_one(
                connection,
                'cad_symptom_episodes',
                'episode_id=?',
                (episode_id,),
            )
        if row is None:
            return None
        return SymptomEpisode.model_validate_json(row['payload_json'])

    def list_symptom_episodes(
        self, document_id: str
    ) -> tuple[SymptomEpisode, ...]:
        with closing(self._connect()) as connection:
            rows = self._fetch_all(
                connection,
                'cad_symptom_episodes',
                'document_id=?',
                (document_id,),
            )
        return tuple(
            SymptomEpisode.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Drift assessments

    def save_drift_assessment(self, assessment: DriftAssessment) -> None:
        _assert_sealed(
            assessment,
            sha_field='assessment_sha256',
            payload_fn='semantic_payload',
        )
        existing = self.get_drift_assessment(assessment.assessment_id)
        self._save(
            'cad_drift_assessments',
            'assessment_id',
            assessment.assessment_id,
            {
                'document_id': assessment.document_id,
                'subject_kind': assessment.subject_ref.kind,
                'subject_ref_id': assessment.subject_ref.ref_id,
                'dependency_status': assessment.dependency_status,
                'operational_severity': assessment.operational_severity,
                'evidence_certainty': assessment.evidence_certainty,
                'assessment_sha256': assessment.assessment_sha256,
                'assessed_at_utc': assessment.assessed_at_utc,
            },
            assessment.model_dump_json(),
            existing,
            None if existing is None else existing.assessment_sha256,
            assessment.assessment_sha256,
        )

    def get_drift_assessment(
        self, assessment_id: str
    ) -> DriftAssessment | None:
        with closing(self._connect()) as connection:
            row = self._fetch_one(
                connection,
                'cad_drift_assessments',
                'assessment_id=?',
                (assessment_id,),
            )
        if row is None:
            return None
        return DriftAssessment.model_validate_json(row['payload_json'])

    def list_drift_assessments(
        self,
        document_id: str,
        *,
        subject_kind: str | None = None,
        subject_ref_id: str | None = None,
    ) -> tuple[DriftAssessment, ...]:
        where = ['document_id=?']
        params: list[str] = [document_id]
        if subject_kind is not None:
            where.append('subject_kind=?')
            params.append(subject_kind)
        if subject_ref_id is not None:
            where.append('subject_ref_id=?')
            params.append(subject_ref_id)
        with closing(self._connect()) as connection:
            rows = self._fetch_all(
                connection,
                'cad_drift_assessments',
                ' AND '.join(where),
                tuple(params),
            )
        return tuple(
            DriftAssessment.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Reverification triggers

    def save_trigger(self, trigger: ReverificationTrigger) -> None:
        _assert_sealed(
            trigger,
            sha_field='trigger_sha256',
            payload_fn='semantic_payload',
        )
        existing = self.get_trigger(trigger.trigger_id)
        self._save(
            'cad_reverification_triggers',
            'trigger_id',
            trigger.trigger_id,
            {
                'document_id': trigger.document_id,
                'assessment_id': trigger.assessment_id,
                'action': trigger.action,
                'trigger_sha256': trigger.trigger_sha256,
                'decided_at_utc': trigger.decided_at_utc,
            },
            trigger.model_dump_json(),
            existing,
            None if existing is None else existing.trigger_sha256,
            trigger.trigger_sha256,
        )

    def get_trigger(self, trigger_id: str) -> ReverificationTrigger | None:
        with closing(self._connect()) as connection:
            row = self._fetch_one(
                connection,
                'cad_reverification_triggers',
                'trigger_id=?',
                (trigger_id,),
            )
        if row is None:
            return None
        return ReverificationTrigger.model_validate_json(row['payload_json'])

    def list_triggers(
        self, document_id: str, *, assessment_id: str | None = None
    ) -> tuple[ReverificationTrigger, ...]:
        where = 'document_id=?'
        params: tuple[str, ...] = (document_id,)
        if assessment_id is not None:
            where += ' AND assessment_id=?'
            params = (document_id, assessment_id)
        with closing(self._connect()) as connection:
            rows = self._fetch_all(
                connection, 'cad_reverification_triggers', where, params
            )
        return tuple(
            ReverificationTrigger.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Restore confirmations

    def save_restore_confirmation(
        self, confirmation: RestoreConfirmation
    ) -> None:
        _assert_sealed(
            confirmation,
            sha_field='confirmation_sha256',
            payload_fn='semantic_payload',
        )
        existing = self.get_restore_confirmation(confirmation.confirmation_id)
        self._save(
            'cad_restore_confirmations',
            'confirmation_id',
            confirmation.confirmation_id,
            {
                'document_id': confirmation.document_id,
                'restore_ref_kind': confirmation.restore_ref.kind,
                'restore_ref_id': confirmation.restore_ref.ref_id,
                'verdict': confirmation.verdict,
                'confirmation_sha256': confirmation.confirmation_sha256,
                'confirmed_at_utc': confirmation.confirmed_at_utc,
            },
            confirmation.model_dump_json(),
            existing,
            None if existing is None else existing.confirmation_sha256,
            confirmation.confirmation_sha256,
        )

    def get_restore_confirmation(
        self, confirmation_id: str
    ) -> RestoreConfirmation | None:
        with closing(self._connect()) as connection:
            row = self._fetch_one(
                connection,
                'cad_restore_confirmations',
                'confirmation_id=?',
                (confirmation_id,),
            )
        if row is None:
            return None
        return RestoreConfirmation.model_validate_json(row['payload_json'])

    def list_restore_confirmations(
        self, document_id: str
    ) -> tuple[RestoreConfirmation, ...]:
        with closing(self._connect()) as connection:
            rows = self._fetch_all(
                connection,
                'cad_restore_confirmations',
                'document_id=?',
                (document_id,),
            )
        return tuple(
            RestoreConfirmation.model_validate_json(row['payload_json'])
            for row in rows
        )


__all__ = [
    'CadHealthDriftRepository',
    'HealthDriftConflictError',
    'HealthDriftIntegrityError',
]
