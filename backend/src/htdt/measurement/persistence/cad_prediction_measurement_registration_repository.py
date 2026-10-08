"""Append-only persistence for prediction<->measurement registration (#564).

Registrations and sealed residual reports are durable project records stored
by exact semantic identity. Persisted rows are re-verified on read: the
registration's semantic hash and its comparability verdict are recomputed,
so a row whose payload no longer matches its own declared verdict fails
closed instead of silently authorizing a contaminated comparison.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from ..domain.cad_prediction_measurement_registration import (
    PredictionMeasurementRegistration,
    PredictionMeasurementResidualReport,
    evaluate_comparability,
    evaluate_registration_freshness,
)
from ...cad_repository import SceneRepository
from ...cad_schema import require_native_tables, connect_sqlite
from ...clock import utc_now_iso as _utc_now


class RegistrationConflictError(ValueError):
    """A registration/report authority was saved twice with different content."""


class CadPredictionMeasurementRegistrationRepository:
    """Durable store for #564 registration + residual evidence."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _initialize(self) -> None:
        # #767: persistent schema is owned by the migration authority;
        # repositories verify the migrated contract, never converge it.
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_prediction_measurement_registrations',
                'cad_prediction_measurement_residual_reports',
            )

    # -- registrations --------------------------------------------------

    def save(self, registration: PredictionMeasurementRegistration) -> None:
        # Re-verify before persisting: a record whose sealed verdict no
        # longer matches evaluation must not enter the store.
        if evaluate_comparability(registration) != registration.comparability:
            raise ValueError('registration verdict does not match evaluation')
        with closing(self._connect()) as connection:
            measurement_row = connection.execute(
                'SELECT 1 FROM cad_measurements WHERE measurement_id=?',
                (registration.measurement.measurement_id,),
            ).fetchone()
        if measurement_row is None:
            raise ValueError(
                'registration pins a measurement that is not persisted: '
                f'{registration.measurement.measurement_id}'
            )
        revision = self.scene_repository.get(registration.scene_revision_id)
        if revision is None or revision.document_id != registration.document_id:
            raise ValueError('registration pins a scene revision that is not persisted')
        self._insert_once(
            table='cad_prediction_measurement_registrations',
            key_column='registration_id',
            key=registration.registration_id,
            columns=(
                'registration_id',
                'semantic_sha256',
                'document_id',
                'scene_revision_id',
                'measurement_id',
                'comparability_state',
                'partition',
                'payload_json',
                'recorded_at_utc',
            ),
            values=(
                registration.registration_id,
                registration.semantic_sha256,
                registration.document_id,
                registration.scene_revision_id,
                registration.measurement.measurement_id,
                registration.comparability.state,
                registration.partition,
                registration.model_dump_json(),
                _utc_now(),
            ),
            payload=registration.model_dump_json(),
        )

    def get(self, registration_id: str) -> PredictionMeasurementRegistration | None:
        row = self._select_row(
            'cad_prediction_measurement_registrations',
            'registration_id',
            registration_id,
        )
        if row is None:
            return None
        return self._row_to_registration(row)

    def list_for_document(
        self, document_id: str
    ) -> tuple[PredictionMeasurementRegistration, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT * FROM cad_prediction_measurement_registrations '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(self._row_to_registration(row) for row in rows)

    def list_for_measurement(
        self, measurement_id: str
    ) -> tuple[PredictionMeasurementRegistration, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT * FROM cad_prediction_measurement_registrations '
                'WHERE measurement_id=? ORDER BY seq ASC',
                (measurement_id,),
            ).fetchall()
        return tuple(self._row_to_registration(row) for row in rows)

    def _row_to_registration(
        self, row: sqlite3.Row
    ) -> PredictionMeasurementRegistration:
        registration = PredictionMeasurementRegistration.model_validate_json(
            row['payload_json']
        )
        if (
            row['registration_id'] != registration.registration_id
            or row['semantic_sha256'] != registration.semantic_sha256
            or row['document_id'] != registration.document_id
            or row['measurement_id'] != registration.measurement.measurement_id
            or row['comparability_state'] != registration.comparability.state
            or row['partition'] != registration.partition
        ):
            raise ValueError(
                'persisted registration row disagrees with its payload'
            )
        return registration

    def evaluate_freshness(
        self, registration: PredictionMeasurementRegistration
    ) -> str:
        """Live staleness: 'current' or 'stale_geometry_revision' (#564)."""

        revision = self.scene_repository.current_head(registration.document_id)
        if revision is None:
            return 'current'
        return evaluate_registration_freshness(
            registration,
            current_scene_revision_id=revision.revision_id,
            current_scene_content_hash=revision.content_hash,
        )

    # -- residual reports ------------------------------------------------

    def save_report(self, report: PredictionMeasurementResidualReport) -> None:
        if self.get(report.registration_id) is None:
            raise ValueError(
                'residual report pins a registration that is not persisted'
            )
        self._insert_once(
            table='cad_prediction_measurement_residual_reports',
            key_column='report_id',
            key=report.report_id,
            columns=(
                'report_id',
                'semantic_sha256',
                'registration_id',
                'document_id',
                'partition',
                'payload_json',
                'recorded_at_utc',
            ),
            values=(
                report.report_id,
                report.semantic_sha256,
                report.registration_id,
                report.document_id,
                report.partition,
                report.model_dump_json(),
                _utc_now(),
            ),
            payload=report.model_dump_json(),
        )

    def get_report(
        self, report_id: str
    ) -> PredictionMeasurementResidualReport | None:
        row = self._select_row(
            'cad_prediction_measurement_residual_reports', 'report_id', report_id
        )
        if row is None:
            return None
        return self._row_to_report(row)

    def list_reports(
        self, registration_id: str
    ) -> tuple[PredictionMeasurementResidualReport, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT * FROM cad_prediction_measurement_residual_reports '
                'WHERE registration_id=? ORDER BY seq ASC',
                (registration_id,),
            ).fetchall()
        return tuple(self._row_to_report(row) for row in rows)

    def list_reports_for_document(
        self, document_id: str
    ) -> tuple[PredictionMeasurementResidualReport, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT * FROM cad_prediction_measurement_residual_reports '
                'WHERE document_id=? ORDER BY seq ASC',
                (document_id,),
            ).fetchall()
        return tuple(self._row_to_report(row) for row in rows)

    def latest_report(
        self, registration_id: str
    ) -> PredictionMeasurementResidualReport | None:
        reports = self.list_reports(registration_id)
        return reports[-1] if reports else None

    def _row_to_report(
        self, row: sqlite3.Row
    ) -> PredictionMeasurementResidualReport:
        report = PredictionMeasurementResidualReport.model_validate_json(
            row['payload_json']
        )
        if (
            row['report_id'] != report.report_id
            or row['semantic_sha256'] != report.semantic_sha256
            or row['registration_id'] != report.registration_id
            or row['document_id'] != report.document_id
            or row['partition'] != report.partition
        ):
            raise ValueError('persisted residual report row disagrees with its payload')
        return report

    # -- low-level helpers -------------------------------------------------

    def _select(
        self, table: str, key_column: str, key: str
    ) -> str | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                f'SELECT payload_json FROM {table} WHERE {key_column}=?',
                (key,),
            ).fetchone()
        return None if row is None else str(row[0])

    def _select_row(
        self, table: str, key_column: str, key: str
    ) -> sqlite3.Row | None:
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            return connection.execute(
                f'SELECT * FROM {table} WHERE {key_column}=?',
                (key,),
            ).fetchone()

    def _insert_once(
        self,
        *,
        table: str,
        key_column: str,
        key: str,
        columns: tuple[str, ...],
        values: tuple[object, ...],
        payload: str,
    ) -> None:
        existing = self._select(table, key_column, key)
        if existing is not None:
            if existing != payload:
                raise RegistrationConflictError(
                    f'{key_column} {key} is persisted with different content'
                )
            return
        placeholders = ', '.join('?' for _ in values)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                f'INSERT INTO {table} ({", ".join(columns)}) '
                f'VALUES ({placeholders})',
                values,
            )
