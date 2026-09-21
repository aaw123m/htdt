from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

from .cad_measurement_models import CadFrequencyResponseDataset, CadMeasurementRecord
from .cad_measurement_quality import (
    CadMeasurementLineageRecord,
    CadMeasurementQualityReport,
    dataset_sha256,
    measurement_sha256,
    replay_measurement_quality_report,
)
from .cad_measurement_repository import CadMeasurementRepository
from .cad_schema import check_native_schema_compatibility


class CadMeasurementQualityRepository:
    """Append-only quality and retake evidence over the native N60 measurement authority."""

    def __init__(self, measurement_repository: CadMeasurementRepository) -> None:
        self.measurement_repository = measurement_repository
        self.path = Path(measurement_repository.path)
        check_native_schema_compatibility(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    def _initialize(self) -> None:
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                '''
                CREATE TABLE IF NOT EXISTS cad_measurement_quality_reports (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_id TEXT NOT NULL UNIQUE,
                    measurement_id TEXT NOT NULL REFERENCES cad_measurements(measurement_id),
                    dataset_id TEXT NOT NULL REFERENCES cad_frequency_responses(dataset_id),
                    raw_asset_sha256 TEXT NOT NULL REFERENCES cad_measurement_assets(sha256),
                    report_sha256 TEXT NOT NULL,
                    profile_sha256 TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_measurement_quality_measurement_seq
                    ON cad_measurement_quality_reports(measurement_id, seq ASC);
                CREATE INDEX IF NOT EXISTS idx_measurement_quality_dataset_seq
                    ON cad_measurement_quality_reports(dataset_id, seq ASC);

                CREATE TABLE IF NOT EXISTS cad_measurement_lineage (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    lineage_id TEXT NOT NULL UNIQUE,
                    document_id TEXT NOT NULL,
                    measurement_id TEXT NOT NULL REFERENCES cad_measurements(measurement_id),
                    supersedes_measurement_id TEXT NOT NULL REFERENCES cad_measurements(measurement_id),
                    selected_measurement_id TEXT NOT NULL REFERENCES cad_measurements(measurement_id),
                    lineage_sha256 TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_measurement_lineage_document_seq
                    ON cad_measurement_lineage(document_id, seq ASC);
                CREATE INDEX IF NOT EXISTS idx_measurement_lineage_measurement
                    ON cad_measurement_lineage(measurement_id, seq ASC);
                '''
            )

    def _validate_report_bindings(
        self,
        report: CadMeasurementQualityReport,
    ) -> tuple[CadMeasurementRecord, CadFrequencyResponseDataset]:
        measurement = self.measurement_repository.get_measurement(report.measurement_id)
        if measurement is None:
            raise ValueError(f'quality report references unknown measurement: {report.measurement_id}')
        dataset = self.measurement_repository.get_dataset(report.dataset_id)
        if dataset is None:
            raise ValueError(f'quality report references unknown dataset: {report.dataset_id}')
        if dataset.measurement_id != measurement.measurement_id:
            raise ValueError('quality report dataset/measurement binding mismatch')
        if report.measurement_sha256 != measurement_sha256(measurement):
            raise ValueError('quality report measurement hash mismatch')
        if report.dataset_sha256 != dataset_sha256(dataset):
            raise ValueError('quality report dataset hash mismatch')
        if report.raw_asset_sha256 != dataset.source_sha256:
            raise ValueError('quality report raw asset hash mismatch')
        if (
            report.document_id != measurement.document_id
            or report.scene_revision_id != measurement.scene_revision_id
            or report.scene_content_hash != measurement.scene_content_hash
            or report.measurement_entity_id != measurement.measurement_entity_id
            or report.measurement_position != measurement.measurement_position
        ):
            raise ValueError('quality report SceneRevision/entity/measurement-point binding mismatch')

        for repeat_id in report.evidence.repeat_measurement_ids:
            repeat = self.measurement_repository.get_measurement(repeat_id)
            if repeat is None:
                raise ValueError(f'quality report references unknown repeat measurement: {repeat_id}')
            if (
                repeat.document_id != measurement.document_id
                or repeat.scene_revision_id != measurement.scene_revision_id
                or repeat.scene_content_hash != measurement.scene_content_hash
                or repeat.measurement_entity_id != measurement.measurement_entity_id
                or repeat.measurement_position != measurement.measurement_position
                or repeat.channel_role != measurement.channel_role
                or repeat.source_speaker_ids != measurement.source_speaker_ids
                or repeat.radiation_scope != measurement.radiation_scope
            ):
                raise ValueError('repeatability measurement binding mismatch')
        return measurement, dataset

    def _validate_current_report(self, report: CadMeasurementQualityReport) -> None:
        measurement, dataset = self._validate_report_bindings(report)
        rebuilt = replay_measurement_quality_report(
            report,
            measurement=measurement,
            dataset=dataset,
        )
        if rebuilt != report:
            raise ValueError('quality report does not match canonical quality algorithm output')

    def save_report(self, report: CadMeasurementQualityReport) -> None:
        self._validate_current_report(report)
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            if connection.execute(
                'SELECT 1 FROM cad_measurement_quality_reports WHERE report_id=?',
                (report.report_id,),
            ).fetchone() is not None:
                raise ValueError(f'quality report already exists: {report.report_id}')
            connection.execute(
                '''
                INSERT INTO cad_measurement_quality_reports(
                    report_id, measurement_id, dataset_id, raw_asset_sha256,
                    report_sha256, profile_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    report.report_id,
                    report.measurement_id,
                    report.dataset_id,
                    report.raw_asset_sha256,
                    report.report_sha256,
                    report.profile.profile_sha256,
                    report.created_at_utc,
                    report.model_dump_json(),
                ),
            )

    def get_report(self, report_id: str) -> CadMeasurementQualityReport | None:
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_measurement_quality_reports WHERE report_id=?',
                (report_id,),
            ).fetchone()
        if row is None:
            return None
        report = CadMeasurementQualityReport.model_validate_json(row['payload_json'])
        self._validate_current_report(report)
        return report

    def list_reports(self, measurement_id: str) -> tuple[CadMeasurementQualityReport, ...]:
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                '''
                SELECT payload_json
                FROM cad_measurement_quality_reports
                WHERE measurement_id=?
                ORDER BY seq ASC
                ''',
                (measurement_id,),
            ).fetchall()
        reports = tuple(
            CadMeasurementQualityReport.model_validate_json(row['payload_json'])
            for row in rows
        )
        for report in reports:
            self._validate_current_report(report)
        return reports

    def latest_report(self, measurement_id: str) -> CadMeasurementQualityReport | None:
        reports = self.list_reports(measurement_id)
        return reports[-1] if reports else None

    def save_lineage(self, lineage: CadMeasurementLineageRecord) -> None:
        current = self.measurement_repository.get_measurement(lineage.measurement_id)
        previous = self.measurement_repository.get_measurement(lineage.supersedes_measurement_id)
        selected = self.measurement_repository.get_measurement(lineage.selected_measurement_id)
        if current is None or previous is None or selected is None:
            raise ValueError('measurement lineage references unknown measurement evidence')
        if not (
            current.document_id == previous.document_id == selected.document_id == lineage.document_id
        ):
            raise ValueError('measurement lineage must stay within one document')
        if (
            current.scene_revision_id != previous.scene_revision_id
            or current.scene_content_hash != previous.scene_content_hash
            or current.measurement_entity_id != previous.measurement_entity_id
            or current.measurement_position != previous.measurement_position
            or current.channel_role != previous.channel_role
            or current.source_speaker_ids != previous.source_speaker_ids
            or current.radiation_scope != previous.radiation_scope
        ):
            raise ValueError('retake must preserve the measurement binding it supersedes')

        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            if connection.execute(
                'SELECT 1 FROM cad_measurement_lineage WHERE lineage_id=?',
                (lineage.lineage_id,),
            ).fetchone() is not None:
                raise ValueError(f'measurement lineage already exists: {lineage.lineage_id}')
            connection.execute(
                '''
                INSERT INTO cad_measurement_lineage(
                    lineage_id, document_id, measurement_id, supersedes_measurement_id,
                    selected_measurement_id, lineage_sha256, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    lineage.lineage_id,
                    lineage.document_id,
                    lineage.measurement_id,
                    lineage.supersedes_measurement_id,
                    lineage.selected_measurement_id,
                    lineage.lineage_sha256,
                    lineage.created_at_utc,
                    lineage.model_dump_json(),
                ),
            )

    def list_lineage(self, document_id: str) -> tuple[CadMeasurementLineageRecord, ...]:
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                '''
                SELECT payload_json
                FROM cad_measurement_lineage
                WHERE document_id=?
                ORDER BY seq ASC
                ''',
                (document_id,),
            ).fetchall()
        return tuple(
            CadMeasurementLineageRecord.model_validate_json(row['payload_json'])
            for row in rows
        )

    def selected_measurement_for_lineage(self, measurement_id: str) -> str:
        measurement = self.measurement_repository.get_measurement(measurement_id)
        if measurement is None:
            raise KeyError(measurement_id)
        events = self.list_lineage(measurement.document_id)
        connected = {measurement_id}
        changed = True
        while changed:
            changed = False
            for event in events:
                if (
                    event.measurement_id in connected
                    or event.supersedes_measurement_id in connected
                ):
                    before = len(connected)
                    connected.add(event.measurement_id)
                    connected.add(event.supersedes_measurement_id)
                    changed = changed or len(connected) != before
        relevant = [
            event
            for event in events
            if (
                event.measurement_id in connected
                and event.supersedes_measurement_id in connected
            )
        ]
        if not relevant:
            return measurement_id
        return relevant[-1].selected_measurement_id
