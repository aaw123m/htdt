"""Append-only persistence for the guided video commissioning journey (#541).

Every record the journey produces is immutable evidence keyed by its
content-derived id:

* ``cad_video_commissioning_sessions`` — session binding snapshots
  (``vcs-``). A rebind is a new row that supersedes the old one.
* ``cad_video_commissioning_status_events`` — the only mutable-appearing
  surface: an append-only log of ``open → completed|abandoned``
  transitions. Sessions stay immutable; status is derived.
* ``cad_video_readiness_reports`` — ``vrr-`` readiness verdicts.
* ``cad_video_diagnoses`` — ``vcd-`` diagnosis records.
* ``cad_video_action_proposals`` — ``vcp-`` corrective-action proposals.
* ``cad_video_operator_adjustments`` — ``voa-`` iteration records
  (adjustment + optional follow-up measurement).
* ``cad_video_before_after_comparisons`` — ``vbc-`` comparison verdicts.
* ``cad_video_import_batches`` — ``vib-`` import provenance linking a
  session to the measurement sets imported under it.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .cad_video_commissioning import (
    GuidedVideoCommissioningSession,
    VideoBeforeAfterComparison,
    VideoCommissioningDiagnosis,
    VideoCommissioningProposal,
    VideoCommissioningStatus,
    VideoOperatorAdjustment,
    VideoReadinessReport,
    VideoReadinessState,
)
from .cad_video_measure_import import VideoMeasurementImport
from .clock import utc_now_iso as _utc_now


class VideoCommissioningConflictError(ValueError):
    """A save violated the append-only identity of a commissioning row."""


class VideoCommissioningIntegrityError(ValueError):
    """A stored row disagreed with its payload or resolution contract."""


class VideoSessionStatusEvent(BaseModel):
    """One recorded session-status transition — append-only."""

    model_config = ConfigDict(frozen=True)

    document_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    from_status: VideoCommissioningStatus
    to_status: VideoCommissioningStatus
    event_at_utc: str = Field(min_length=1)


_TRANSITIONS: dict[str, tuple[str, ...]] = {
    'open': ('completed', 'abandoned'),
    'completed': (),
    'abandoned': (),
}


class VideoSessionJourneyState(BaseModel):
    """Aggregated persisted evidence for the journey surface."""

    model_config = ConfigDict(frozen=True)

    session: GuidedVideoCommissioningSession | None = None
    current_status: VideoCommissioningStatus = 'open'
    readiness_state: VideoReadinessState | None = None
    measurement_set_ids: tuple[str, ...] = ()
    diagnosis_ids: tuple[str, ...] = ()
    adjustment_ids: tuple[str, ...] = ()
    comparison_ids: tuple[str, ...] = ()
    import_batch_ids: tuple[str, ...] = ()


class CadVideoCommissioningRepository:
    """Native storage for guided video commissioning records."""

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
                'cad_video_commissioning_sessions',
                'cad_video_commissioning_status_events',
                'cad_video_readiness_reports',
                'cad_video_diagnoses',
                'cad_video_action_proposals',
                'cad_video_operator_adjustments',
                'cad_video_before_after_comparisons',
                'cad_video_import_batches',
            )

    # ------------------------------------------------------------------
    # Sessions

    def save_session(
        self,
        session: GuidedVideoCommissioningSession,
    ) -> None:
        """Persist a session snapshot. ``status`` must be ``'open'`` —
        later transitions are appended as status events."""
        if session.status != 'open':
            raise VideoCommissioningConflictError(
                'sessions are persisted open; transitions are '
                'status events'
            )
        existing = self.get_session(session.document_id, session.session_id)
        if existing is not None:
            if existing.session_sha256 == session.session_sha256:
                return
            raise VideoCommissioningConflictError(
                'commissioning session (document_id, session_id) '
                'is append-only'
            )
        supersedes = session.supersedes_session_id
        if supersedes is not None:
            parent = self.get_session(session.document_id, supersedes)
            if parent is None:
                raise VideoCommissioningIntegrityError(
                    'a superseding session must reference a persisted '
                    'session'
                )
            if self.current_status(session.document_id, supersedes) != 'open':
                raise VideoCommissioningConflictError(
                    'a completed or abandoned session cannot be '
                    'superseded'
                )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_video_commissioning_sessions (
                    document_id, session_id, surface_entity_id, mode,
                    session_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.document_id,
                    session.session_id,
                    session.surface_entity_id,
                    session.mode,
                    session.session_sha256,
                    _utc_now(),
                    session.model_dump_json(),
                ),
            )

    def get_session(
        self,
        document_id: str,
        session_id: str,
    ) -> GuidedVideoCommissioningSession | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT session_id, surface_entity_id, session_sha256,
                       payload_json
                FROM cad_video_commissioning_sessions
                WHERE document_id=? AND session_id=?
                """,
                (document_id, session_id),
            ).fetchone()
        if row is None:
            return None
        return self._session_from_row(row)

    def list_sessions(
        self,
        document_id: str,
        surface_entity_id: str | None = None,
    ) -> tuple[GuidedVideoCommissioningSession, ...]:
        with closing(self._connect()) as connection:
            if surface_entity_id is None:
                rows = connection.execute(
                    """
                    SELECT session_id, surface_entity_id, session_sha256,
                           payload_json
                    FROM cad_video_commissioning_sessions
                    WHERE document_id=?
                    ORDER BY created_at_utc, session_id
                    """,
                    (document_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT session_id, surface_entity_id, session_sha256,
                           payload_json
                    FROM cad_video_commissioning_sessions
                    WHERE document_id=? AND surface_entity_id=?
                    ORDER BY created_at_utc, session_id
                    """,
                    (document_id, surface_entity_id),
                ).fetchall()
        return tuple(self._session_from_row(row) for row in rows)

    def _session_from_row(
        self, row: sqlite3.Row
    ) -> GuidedVideoCommissioningSession:
        session = GuidedVideoCommissioningSession.model_validate_json(
            row['payload_json']
        )
        if (
            session.session_id != row['session_id']
            or session.surface_entity_id != row['surface_entity_id']
            or session.session_sha256 != row['session_sha256']
        ):
            raise VideoCommissioningIntegrityError(
                'commissioning session row disagrees with its payload'
            )
        return session

    # ------------------------------------------------------------------
    # Status transitions — append-only

    def record_status_event(
        self,
        document_id: str,
        session_id: str,
        to_status: Literal['completed', 'abandoned'],
        event_at_utc: str | None = None,
    ) -> VideoSessionStatusEvent:
        session = self.get_session(document_id, session_id)
        if session is None:
            raise VideoCommissioningIntegrityError(
                'a status event must reference a persisted session'
            )
        from_status = self.current_status(document_id, session_id)
        if to_status not in _TRANSITIONS[from_status]:
            raise VideoCommissioningConflictError(
                f'session {session_id} cannot transition '
                f'{from_status} → {to_status}'
            )
        event = VideoSessionStatusEvent(
            document_id=document_id,
            session_id=session_id,
            from_status=from_status,
            to_status=to_status,
            event_at_utc=event_at_utc or _utc_now(),
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_video_commissioning_status_events (
                    document_id, session_id, from_status, to_status,
                    event_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.document_id,
                    event.session_id,
                    event.from_status,
                    event.to_status,
                    event.event_at_utc,
                    event.model_dump_json(),
                ),
            )
        return event

    def current_status(
        self,
        document_id: str,
        session_id: str,
    ) -> VideoCommissioningStatus:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT to_status
                FROM cad_video_commissioning_status_events
                WHERE document_id=? AND session_id=?
                ORDER BY seq DESC
                LIMIT 1
                """,
                (document_id, session_id),
            ).fetchone()
        return row['to_status'] if row is not None else 'open'

    def list_status_events(
        self,
        document_id: str,
        session_id: str,
    ) -> tuple[VideoSessionStatusEvent, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM cad_video_commissioning_status_events
                WHERE document_id=? AND session_id=?
                ORDER BY seq
                """,
                (document_id, session_id),
            ).fetchall()
        return tuple(
            VideoSessionStatusEvent.model_validate_json(row['payload_json'])
            for row in rows
        )

    # ------------------------------------------------------------------
    # Readiness reports

    def save_readiness_report(
        self,
        report: VideoReadinessReport,
        document_id: str,
    ) -> None:
        self._require_session(document_id, report.session_id)
        existing = self.get_readiness_report(document_id, report.report_id)
        if existing is not None:
            if existing.report_sha256 == report.report_sha256:
                return
            raise VideoCommissioningConflictError(
                'readiness report (document_id, report_id) is append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_video_readiness_reports (
                    document_id, report_id, session_id, state,
                    report_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    report.report_id,
                    report.session_id,
                    report.state,
                    report.report_sha256,
                    _utc_now(),
                    report.model_dump_json(),
                ),
            )

    def get_readiness_report(
        self,
        document_id: str,
        report_id: str,
    ) -> VideoReadinessReport | None:
        row = self._fetch_one(
            'cad_video_readiness_reports',
            'report_id',
            document_id,
            report_id,
        )
        return (
            self._row_model(row, 'cad_video_readiness_reports',
                            VideoReadinessReport, 'report_id',
                            'report_sha256')
            if row is not None
            else None
        )

    def latest_readiness_report(
        self,
        document_id: str,
        session_id: str,
    ) -> VideoReadinessReport | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM cad_video_readiness_reports
                WHERE document_id=? AND session_id=?
                ORDER BY created_at_utc DESC, report_id DESC
                LIMIT 1
                """,
                (document_id, session_id),
            ).fetchone()
        return (
            self._row_model(row, 'cad_video_readiness_reports',
                            VideoReadinessReport, 'report_id',
                            'report_sha256')
            if row is not None
            else None
        )

    def list_readiness_reports(
        self,
        document_id: str,
        session_id: str | None = None,
    ) -> tuple[VideoReadinessReport, ...]:
        return self._list_by_session(
            'cad_video_readiness_reports',
            document_id,
            session_id,
            VideoReadinessReport,
            'report_id',
            'report_sha256',
        )

    # ------------------------------------------------------------------
    # Diagnoses

    def save_diagnosis(
        self,
        diagnosis: VideoCommissioningDiagnosis,
        document_id: str,
    ) -> None:
        self._require_session(document_id, diagnosis.session_id)
        existing = self.get_diagnosis(document_id, diagnosis.diagnosis_id)
        if existing is not None:
            if existing.diagnosis_sha256 == diagnosis.diagnosis_sha256:
                return
            raise VideoCommissioningConflictError(
                'diagnosis (document_id, diagnosis_id) is append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_video_diagnoses (
                    document_id, diagnosis_id, session_id,
                    measurement_set_id, overall_status,
                    diagnosis_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    diagnosis.diagnosis_id,
                    diagnosis.session_id,
                    diagnosis.measurement_set_id,
                    diagnosis.overall,
                    diagnosis.diagnosis_sha256,
                    _utc_now(),
                    diagnosis.model_dump_json(),
                ),
            )

    def get_diagnosis(
        self,
        document_id: str,
        diagnosis_id: str,
    ) -> VideoCommissioningDiagnosis | None:
        row = self._fetch_one(
            'cad_video_diagnoses',
            'diagnosis_id',
            document_id,
            diagnosis_id,
        )
        return (
            self._row_model(
                row, 'cad_video_diagnoses', VideoCommissioningDiagnosis,
                'diagnosis_id', 'diagnosis_sha256',
            )
            if row is not None
            else None
        )

    def list_diagnoses(
        self,
        document_id: str,
        session_id: str | None = None,
    ) -> tuple[VideoCommissioningDiagnosis, ...]:
        return self._list_by_session(
            'cad_video_diagnoses',
            document_id,
            session_id,
            VideoCommissioningDiagnosis,
            'diagnosis_id',
            'diagnosis_sha256',
        )

    # ------------------------------------------------------------------
    # Corrective-action proposals

    def save_proposal(
        self,
        proposal: VideoCommissioningProposal,
        document_id: str,
    ) -> None:
        existing = self.get_proposal(document_id, proposal.proposal_id)
        if existing is not None:
            if existing.proposal_sha256 == proposal.proposal_sha256:
                return
            raise VideoCommissioningConflictError(
                'action proposal (document_id, proposal_id) is append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_video_action_proposals (
                    document_id, proposal_id, diagnosis_id,
                    proposal_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    proposal.proposal_id,
                    proposal.diagnosis_id,
                    proposal.proposal_sha256,
                    _utc_now(),
                    proposal.model_dump_json(),
                ),
            )

    def get_proposal(
        self,
        document_id: str,
        proposal_id: str,
    ) -> VideoCommissioningProposal | None:
        row = self._fetch_one(
            'cad_video_action_proposals',
            'proposal_id',
            document_id,
            proposal_id,
        )
        return (
            self._row_model(
                row, 'cad_video_action_proposals',
                VideoCommissioningProposal,
                'proposal_id', 'proposal_sha256',
            )
            if row is not None
            else None
        )

    def list_proposals(
        self,
        document_id: str,
        diagnosis_id: str | None = None,
    ) -> tuple[VideoCommissioningProposal, ...]:
        with closing(self._connect()) as connection:
            if diagnosis_id is None:
                rows = connection.execute(
                    """
                    SELECT * FROM cad_video_action_proposals
                    WHERE document_id=?
                    ORDER BY created_at_utc, proposal_id
                    """,
                    (document_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM cad_video_action_proposals
                    WHERE document_id=? AND diagnosis_id=?
                    ORDER BY created_at_utc, proposal_id
                    """,
                    (document_id, diagnosis_id),
                ).fetchall()
        return tuple(
            self._row_model(
                row, 'cad_video_action_proposals',
                VideoCommissioningProposal,
                'proposal_id', 'proposal_sha256',
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # Operator adjustments

    def save_adjustment(
        self,
        adjustment: VideoOperatorAdjustment,
        document_id: str,
    ) -> None:
        self._require_session(document_id, adjustment.session_id)
        existing = self.get_adjustment(document_id, adjustment.adjustment_id)
        if existing is not None:
            if existing.adjustment_sha256 == adjustment.adjustment_sha256:
                return
            raise VideoCommissioningConflictError(
                'operator adjustment (document_id, adjustment_id) '
                'is append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_video_operator_adjustments (
                    document_id, adjustment_id, session_id,
                    iteration_index, adjustment_sha256,
                    created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    adjustment.adjustment_id,
                    adjustment.session_id,
                    adjustment.iteration_index,
                    adjustment.adjustment_sha256,
                    _utc_now(),
                    adjustment.model_dump_json(),
                ),
            )

    def get_adjustment(
        self,
        document_id: str,
        adjustment_id: str,
    ) -> VideoOperatorAdjustment | None:
        row = self._fetch_one(
            'cad_video_operator_adjustments',
            'adjustment_id',
            document_id,
            adjustment_id,
        )
        return (
            self._row_model(
                row, 'cad_video_operator_adjustments',
                VideoOperatorAdjustment,
                'adjustment_id', 'adjustment_sha256',
            )
            if row is not None
            else None
        )

    def list_adjustments(
        self,
        document_id: str,
        session_id: str | None = None,
    ) -> tuple[VideoOperatorAdjustment, ...]:
        return self._list_by_session(
            'cad_video_operator_adjustments',
            document_id,
            session_id,
            VideoOperatorAdjustment,
            'adjustment_id',
            'adjustment_sha256',
        )

    # ------------------------------------------------------------------
    # Before/after comparisons

    def save_comparison(
        self,
        comparison: VideoBeforeAfterComparison,
        document_id: str,
    ) -> None:
        self._require_session(document_id, comparison.session_id)
        existing = self.get_comparison(
            document_id, comparison.comparison_id
        )
        if existing is not None:
            if existing.comparison_sha256 == comparison.comparison_sha256:
                return
            raise VideoCommissioningConflictError(
                'comparison (document_id, comparison_id) is append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_video_before_after_comparisons (
                    document_id, comparison_id, session_id,
                    iteration_index, comparison_status, overall_direction,
                    comparison_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    comparison.comparison_id,
                    comparison.session_id,
                    comparison.iteration_index,
                    comparison.status,
                    comparison.overall,
                    comparison.comparison_sha256,
                    _utc_now(),
                    comparison.model_dump_json(),
                ),
            )

    def get_comparison(
        self,
        document_id: str,
        comparison_id: str,
    ) -> VideoBeforeAfterComparison | None:
        row = self._fetch_one(
            'cad_video_before_after_comparisons',
            'comparison_id',
            document_id,
            comparison_id,
        )
        return (
            self._row_model(
                row, 'cad_video_before_after_comparisons',
                VideoBeforeAfterComparison,
                'comparison_id', 'comparison_sha256',
            )
            if row is not None
            else None
        )

    def list_comparisons(
        self,
        document_id: str,
        session_id: str | None = None,
    ) -> tuple[VideoBeforeAfterComparison, ...]:
        return self._list_by_session(
            'cad_video_before_after_comparisons',
            document_id,
            session_id,
            VideoBeforeAfterComparison,
            'comparison_id',
            'comparison_sha256',
        )

    # ------------------------------------------------------------------
    # Import batches

    def save_import_batch(
        self,
        batch: VideoMeasurementImport,
        document_id: str,
    ) -> None:
        if batch.session_id is not None:
            self._require_session(document_id, batch.session_id)
        existing = self.get_import_batch(document_id, batch.batch_id)
        if existing is not None:
            if existing.batch_sha256 == batch.batch_sha256:
                return
            raise VideoCommissioningConflictError(
                'import batch (document_id, batch_id) is append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_video_import_batches (
                    document_id, batch_id, session_id,
                    measurement_set_id, format_id,
                    batch_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    batch.batch_id,
                    batch.session_id,
                    batch.measurement_set_id,
                    batch.format_id,
                    batch.batch_sha256,
                    _utc_now(),
                    batch.model_dump_json(),
                ),
            )

    def get_import_batch(
        self,
        document_id: str,
        batch_id: str,
    ) -> VideoMeasurementImport | None:
        row = self._fetch_one(
            'cad_video_import_batches',
            'batch_id',
            document_id,
            batch_id,
        )
        return (
            self._row_model(
                row, 'cad_video_import_batches', VideoMeasurementImport,
                'batch_id', 'batch_sha256',
            )
            if row is not None
            else None
        )

    def list_import_batches(
        self,
        document_id: str,
        session_id: str | None = None,
    ) -> tuple[VideoMeasurementImport, ...]:
        return self._list_by_session(
            'cad_video_import_batches',
            document_id,
            session_id,
            VideoMeasurementImport,
            'batch_id',
            'batch_sha256',
        )

    def import_batches_for_set(
        self,
        document_id: str,
        measurement_set_id: str,
    ) -> tuple[VideoMeasurementImport, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM cad_video_import_batches
                WHERE document_id=? AND measurement_set_id=?
                ORDER BY created_at_utc, batch_id
                """,
                (document_id, measurement_set_id),
            ).fetchall()
        return tuple(
            self._row_model(
                row, 'cad_video_import_batches', VideoMeasurementImport,
                'batch_id', 'batch_sha256',
            )
            for row in rows
        )

    # ------------------------------------------------------------------
    # Journey aggregation — one read for the surface's step strip

    def journey_state(
        self,
        document_id: str,
        session_id: str,
    ) -> VideoSessionJourneyState:
        session = self.get_session(document_id, session_id)
        if session is None:
            return VideoSessionJourneyState()
        latest_readiness = self.latest_readiness_report(
            document_id, session_id
        )
        return VideoSessionJourneyState(
            session=session,
            current_status=self.current_status(document_id, session_id),
            readiness_state=(
                latest_readiness.state
                if latest_readiness is not None
                else None
            ),
            measurement_set_ids=tuple(
                b.measurement_set_id
                for b in self.list_import_batches(document_id, session_id)
            ),
            diagnosis_ids=tuple(
                d.diagnosis_id
                for d in self.list_diagnoses(document_id, session_id)
            ),
            adjustment_ids=tuple(
                a.adjustment_id
                for a in self.list_adjustments(document_id, session_id)
            ),
            comparison_ids=tuple(
                c.comparison_id
                for c in self.list_comparisons(document_id, session_id)
            ),
            import_batch_ids=tuple(
                b.batch_id
                for b in self.list_import_batches(document_id, session_id)
            ),
        )

    # ------------------------------------------------------------------
    # Internals

    def _require_session(
        self, document_id: str, session_id: str
    ) -> None:
        if self.get_session(document_id, session_id) is None:
            raise VideoCommissioningIntegrityError(
                'record must reference a persisted commissioning session'
            )

    def _fetch_one(
        self,
        table: str,
        key_column: str,
        document_id: str,
        key: str,
    ) -> sqlite3.Row | None:
        with closing(self._connect()) as connection:
            return connection.execute(
                f'SELECT * FROM {table} '
                f'WHERE document_id=? AND {key_column}=?',
                (document_id, key),
            ).fetchone()

    def _list_by_session(
        self,
        table: str,
        document_id: str,
        session_id: str | None,
        model: type[BaseModel],
        id_column: str,
        sha_column: str,
    ) -> tuple:
        with closing(self._connect()) as connection:
            if session_id is None:
                rows = connection.execute(
                    f'SELECT * FROM {table} WHERE document_id=? '
                    f'ORDER BY created_at_utc, {id_column}',
                    (document_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    f'SELECT * FROM {table} '
                    f'WHERE document_id=? AND session_id=? '
                    f'ORDER BY created_at_utc, {id_column}',
                    (document_id, session_id),
                ).fetchall()
        return tuple(
            self._row_model(row, table, model, id_column, sha_column)
            for row in rows
        )

    @staticmethod
    def _row_model(
        row: sqlite3.Row,
        table: str,
        model: type[BaseModel],
        id_column: str,
        sha_column: str,
    ):
        record = model.model_validate_json(row['payload_json'])
        if (
            getattr(record, id_column) != row[id_column]
            or getattr(record, sha_column) != row[sha_column]
        ):
            raise VideoCommissioningIntegrityError(
                f'{table} row disagrees with its payload'
            )
        return record


__all__ = [
    'CadVideoCommissioningRepository',
    'VideoCommissioningConflictError',
    'VideoCommissioningIntegrityError',
    'VideoSessionJourneyState',
    'VideoSessionStatusEvent',
]
