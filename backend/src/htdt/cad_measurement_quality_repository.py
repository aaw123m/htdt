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


class MeasurementLineageConflictError(ValueError):
    """A retake-lineage save violated the single-head supersession contract."""


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
                CREATE INDEX IF NOT EXISTS idx_measurement_lineage_supersedes
                    ON cad_measurement_lineage(supersedes_measurement_id, seq ASC);
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

    def _validate_lineage_bindings(self, lineage: CadMeasurementLineageRecord) -> None:
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

    def save_lineage(self, lineage: CadMeasurementLineageRecord) -> None:
        """Append one retake record extending the single head of its chain.

        The retake history of one measurement binding is an append-only chain
        of ``supersedes_measurement_id -> measurement_id`` edges whose
        topology is enforced under one ``BEGIN IMMEDIATE`` transaction:

        - the superseded measurement must be the current head of its chain —
          a measurement may be superseded at most once, so a second retake
          claiming the same predecessor is rejected as a stale-head fork;
        - the retake measurement must carry no existing lineage edge — it may
          not already supersede a predecessor (which would merge two chains)
          nor already be superseded (which would close a cycle, e.g. B -> A
          after A -> B), so every chain stays a simple path from root to head;
        - ``selected_measurement_id`` stays a per-record decision between the
          two sides of the retake, resolved authoritatively from the chain
          head on read rather than by insertion order.

        Two writers racing to retake the same head cannot both advance it:
        the head check runs inside the write transaction, so the loser sees
        the moved head and fails with ``MeasurementLineageConflictError``.
        The record's measurement bindings are revalidated on every save
        before the lock is taken, so the builder remains a convenience and
        not the only integrity boundary.
        """
        self._validate_lineage_bindings(lineage)
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            # BEGIN IMMEDIATE holds the write lock across the duplicate
            # recheck, the head/topology checks and the insert: concurrent
            # writers cannot both observe the same head.
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute(
                'SELECT 1 FROM cad_measurement_lineage WHERE lineage_id=?',
                (lineage.lineage_id,),
            ).fetchone() is not None:
                raise ValueError(f'measurement lineage already exists: {lineage.lineage_id}')
            prior_child = connection.execute(
                '''
                SELECT measurement_id
                FROM cad_measurement_lineage
                WHERE supersedes_measurement_id=?
                ''',
                (lineage.supersedes_measurement_id,),
            ).fetchone()
            if prior_child is not None:
                raise MeasurementLineageConflictError(
                    f'measurement lineage {lineage.lineage_id} rejected: '
                    f'{lineage.supersedes_measurement_id} is already superseded by '
                    f'{prior_child["measurement_id"]}; a retake must supersede the '
                    'current lineage head'
                )
            bound = connection.execute(
                '''
                SELECT measurement_id, supersedes_measurement_id
                FROM cad_measurement_lineage
                WHERE measurement_id=? OR supersedes_measurement_id=?
                ''',
                (lineage.measurement_id, lineage.measurement_id),
            ).fetchone()
            if bound is not None:
                if bound['measurement_id'] == lineage.measurement_id:
                    reason = (
                        f'already records a retake superseding '
                        f'{bound["supersedes_measurement_id"]}; a measurement may '
                        'supersede at most one predecessor'
                    )
                else:
                    reason = (
                        'is already superseded; reusing it as a retake would '
                        'merge chains or close a lineage cycle'
                    )
                raise MeasurementLineageConflictError(
                    f'measurement lineage {lineage.lineage_id} rejected: '
                    f'{lineage.measurement_id} {reason}'
                )
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
        """Return the document's retake history as validated single-head chains.

        Rows are replayed in insertion order; every stored column must agree
        with its payload and every record must still bind to its exact
        measurement evidence. Each measurement may supersede at most one
        predecessor and be superseded at most once, so valid history is a set
        of disjoint ``root -> head`` paths. A persisted fork, merge or cycle —
        rows written before the topology contract existed or injected past
        ``save_lineage`` — is surfaced as ``ValueError`` rather than silently
        relying on insertion order.
        """
        check_native_schema_compatibility(self.path)
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                '''
                SELECT *
                FROM cad_measurement_lineage
                WHERE document_id=?
                ORDER BY seq ASC
                ''',
                (document_id,),
            ).fetchall()
        records: list[CadMeasurementLineageRecord] = []
        children: dict[str, CadMeasurementLineageRecord] = {}
        parents: dict[str, CadMeasurementLineageRecord] = {}
        for row in rows:
            record = CadMeasurementLineageRecord.model_validate_json(row['payload_json'])
            if (
                row['lineage_id'] != record.lineage_id
                or row['document_id'] != record.document_id
                or row['measurement_id'] != record.measurement_id
                or row['supersedes_measurement_id'] != record.supersedes_measurement_id
                or row['selected_measurement_id'] != record.selected_measurement_id
                or row['lineage_sha256'] != record.lineage_sha256
                or row['created_at_utc'] != record.created_at_utc
            ):
                raise ValueError(
                    'persisted measurement lineage row disagrees with its payload'
                )
            self._validate_lineage_bindings(record)
            if record.supersedes_measurement_id in children:
                raise ValueError(
                    'measurement lineage history is not a single-head chain: '
                    f'{record.supersedes_measurement_id} is superseded more than once'
                )
            if record.measurement_id in parents:
                raise ValueError(
                    'measurement lineage history is not a single-head chain: '
                    f'{record.measurement_id} supersedes more than one predecessor'
                )
            children[record.supersedes_measurement_id] = record
            parents[record.measurement_id] = record
            records.append(record)
        # With at most one edge in each direction every component is a simple
        # path that must terminate at an un-superseded head; a component that
        # never reaches one is a persisted cycle.
        for record in records:
            seen = {record.supersedes_measurement_id}
            node = record.measurement_id
            while node in children:
                if node in seen:
                    raise ValueError(
                        'measurement lineage history is not a single-head chain: '
                        f'supersession cycle reaches {node} again'
                    )
                seen.add(node)
                node = children[node].measurement_id
        return tuple(records)

    def selected_measurement_for_lineage(self, measurement_id: str) -> str:
        """Resolve the selected evidence for the retake chain holding *measurement_id*.

        The selection is derived from validated topology, not insertion order:
        ``list_lineage`` proves the component is a single-head chain, the head
        is the unique measurement that was never superseded, and the record
        that produced the head declares the current selection (a retake may
        deliberately keep the superseded side selected). A measurement with
        no lineage resolves to itself.
        """
        measurement = self.measurement_repository.get_measurement(measurement_id)
        if measurement is None:
            raise KeyError(measurement_id)
        events = self.list_lineage(measurement.document_id)
        children = {event.supersedes_measurement_id: event for event in events}
        parents = {event.measurement_id: event for event in events}
        if measurement_id not in children and measurement_id not in parents:
            return measurement_id
        head = measurement_id
        while head in children:
            head = children[head].measurement_id
        return parents[head].selected_measurement_id
