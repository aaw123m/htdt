"""Append-only persistence for the device snapshot/restore authority
(#592).

Six authorities live here:

* ``cad_device_config_snapshots`` — sealed configuration snapshots keyed
  by ``snapshot_id``; re-saving an identical row is a no-op, a divergent
  row for the same id is a conflict. Corrections are new snapshots
  naming the old one as parent — the DAG is append-only.
* ``cad_device_known_good_baselines`` — immutable known-good references.
* ``cad_device_firmware_transitions`` — firmware change records;
  historical commissioning firmware identity is never rewritten.
* ``cad_device_restore_records`` — restore events with verdicts.
* ``cad_device_backup_artifacts`` — persisted identity rows for the
  #1055 ``DeviceConfigurationBackupArtifact`` model (raw bytes stay
  wherever the artifact lives; the row keeps hash/size/source).
* ``cad_device_replacement_assessments`` — portability assessments for
  replacement hardware.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_device_backup import DeviceConfigurationBackupArtifact
from .cad_device_snapshot import (
    ConfigurationRestoreRecord,
    DeviceConfigurationSnapshot,
    DeviceFirmwareTransition,
    DeviceKnownGoodBaseline,
    ReplacementDeviceAssessment,
)
from .cad_schema import require_native_tables, connect_sqlite
from .cad_repository import SceneRepository
from .canonical_json import canonical_sha256


class DeviceSnapshotConflictError(ValueError):
    """A save violated append-only identity rules."""


class DeviceSnapshotIntegrityError(ValueError):
    """A stored row disagreed with its payload or references."""


def _assert_sealed(
    record: object, sha_field: str, id_field: str | None
) -> None:
    """Re-verify the seal fields a save path persists (REV61).

    A ``model_copy(update=...)`` record keeps digest fields its payload
    never earned — the mismatch only surfaces when a read re-validates,
    so the write boundary is the last place that can still refuse. The
    stored sha must be the canonical digest of the semantic payload, and
    a digest-derived id must equal ``<prefix>-<sha[:24]>``.
    """

    sha = canonical_sha256(record.semantic_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise DeviceSnapshotIntegrityError(
            'record payload does not match its sealed sha256'
        )
    if id_field is None:
        return
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise DeviceSnapshotIntegrityError(
            'record id does not match its sealed sha256'
        )


class CadDeviceSnapshotRepository:
    """Native storage for device configuration snapshots, baselines,
    firmware transitions, restore records, backup artifacts and
    replacement assessments."""

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
                'cad_device_config_snapshots',
                'cad_device_known_good_baselines',
                'cad_device_firmware_transitions',
                'cad_device_restore_records',
                'cad_device_backup_artifacts',
                'cad_device_replacement_assessments',
            )

    # ------------------------------------------------------------------
    # Snapshots

    def save_snapshot(self, snapshot: DeviceConfigurationSnapshot) -> None:
        _assert_sealed(snapshot, 'snapshot_sha256', 'snapshot_id')
        existing = self.get_snapshot(snapshot.snapshot_id)
        if existing is not None:
            if existing.snapshot_sha256 == snapshot.snapshot_sha256:
                return
            raise DeviceSnapshotConflictError(
                'device snapshots are append-only — a corrected state is '
                'a new snapshot naming this one as parent'
            )
        for parent_id in snapshot.parent_snapshot_ids:
            if self.get_snapshot(parent_id) is None:
                raise DeviceSnapshotIntegrityError(
                    'a snapshot parent must reference a persisted '
                    f'snapshot — {parent_id} is not stored'
                )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_device_config_snapshots (
                    snapshot_id, snapshot_sha256, document_id,
                    instance_id, evidence_class, transition_kind,
                    firmware_version, state_content_sha256,
                    captured_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.snapshot_id,
                    snapshot.snapshot_sha256,
                    snapshot.document_id,
                    snapshot.instance_id,
                    snapshot.evidence_class,
                    snapshot.transition_kind,
                    snapshot.firmware_version,
                    snapshot.state_content_sha256,
                    snapshot.captured_at_utc,
                    snapshot.model_dump_json(),
                ),
            )

    def get_snapshot(
        self, snapshot_id: str
    ) -> DeviceConfigurationSnapshot | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT snapshot_id, snapshot_sha256, document_id,
                       instance_id, evidence_class, firmware_version,
                       payload_json
                FROM cad_device_config_snapshots
                WHERE snapshot_id=?
                """,
                (snapshot_id,),
            ).fetchone()
        if row is None:
            return None
        return self._snapshot_from_row(row)

    def get_snapshot_by_hash(
        self, snapshot_sha256: str
    ) -> DeviceConfigurationSnapshot | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT snapshot_id, snapshot_sha256, document_id,
                       instance_id, evidence_class, firmware_version,
                       payload_json
                FROM cad_device_config_snapshots
                WHERE snapshot_sha256=?
                """,
                (snapshot_sha256,),
            ).fetchone()
        if row is None:
            return None
        return self._snapshot_from_row(row)

    def list_snapshots(
        self,
        document_id: str | None = None,
        instance_id: str | None = None,
    ) -> tuple[DeviceConfigurationSnapshot, ...]:
        sql = (
            'SELECT snapshot_id, snapshot_sha256, document_id, '
            'instance_id, evidence_class, firmware_version, payload_json '
            'FROM cad_device_config_snapshots'
        )
        clauses: list[str] = []
        params: list[str] = []
        if document_id is not None:
            clauses.append('document_id=?')
            params.append(document_id)
        if instance_id is not None:
            clauses.append('instance_id=?')
            params.append(instance_id)
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY captured_at_utc, snapshot_id'
        with closing(self._connect()) as connection:
            rows = connection.execute(sql, tuple(params)).fetchall()
        return tuple(self._snapshot_from_row(row) for row in rows)

    def snapshot_children(
        self, snapshot_id: str
    ) -> tuple[DeviceConfigurationSnapshot, ...]:
        """Descendants naming this snapshot as parent — the DAG edges."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT snapshot_id, snapshot_sha256, document_id,
                       instance_id, evidence_class, firmware_version,
                       payload_json
                FROM cad_device_config_snapshots
                ORDER BY captured_at_utc, snapshot_id
                """,
            ).fetchall()
        return tuple(
            self._snapshot_from_row(row)
            for row in rows
            if snapshot_id
            in DeviceConfigurationSnapshot.model_validate_json(
                row['payload_json']
            ).parent_snapshot_ids
        )

    def _snapshot_from_row(
        self, row: sqlite3.Row
    ) -> DeviceConfigurationSnapshot:
        snapshot = DeviceConfigurationSnapshot.model_validate_json(
            row['payload_json']
        )
        if (
            snapshot.snapshot_id != row['snapshot_id']
            or snapshot.snapshot_sha256 != row['snapshot_sha256']
            or snapshot.document_id != row['document_id']
            or snapshot.instance_id != row['instance_id']
            or snapshot.evidence_class != row['evidence_class']
            or snapshot.firmware_version != row['firmware_version']
        ):
            raise DeviceSnapshotIntegrityError(
                'device snapshot row disagrees with its payload'
            )
        return snapshot

    # ------------------------------------------------------------------
    # Known-good baselines

    def save_baseline(self, baseline: DeviceKnownGoodBaseline) -> None:
        _assert_sealed(baseline, 'baseline_sha256', 'baseline_id')
        existing = self.get_baseline(baseline.baseline_id)
        if existing is not None:
            if existing.baseline_sha256 == baseline.baseline_sha256:
                return
            raise DeviceSnapshotConflictError(
                'known-good baselines are immutable — promotion is a new '
                'record, never an edit'
            )
        if self.get_snapshot_by_hash(baseline.snapshot_sha256) is None:
            raise DeviceSnapshotIntegrityError(
                'a baseline must reference a persisted snapshot — '
                'promoting an unstored state is not allowed'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_device_known_good_baselines (
                    baseline_id, baseline_sha256, document_id,
                    instance_id, snapshot_sha256, promoted_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    baseline.baseline_id,
                    baseline.baseline_sha256,
                    baseline.document_id,
                    baseline.instance_id,
                    baseline.snapshot_sha256,
                    baseline.promoted_at_utc,
                    baseline.model_dump_json(),
                ),
            )

    def get_baseline(
        self, baseline_id: str
    ) -> DeviceKnownGoodBaseline | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT baseline_id, baseline_sha256, document_id,
                       instance_id, snapshot_sha256, payload_json
                FROM cad_device_known_good_baselines
                WHERE baseline_id=?
                """,
                (baseline_id,),
            ).fetchone()
        if row is None:
            return None
        return self._baseline_from_row(row)

    def baselines_for_instance(
        self, instance_id: str
    ) -> tuple[DeviceKnownGoodBaseline, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT baseline_id, baseline_sha256, document_id,
                       instance_id, snapshot_sha256, payload_json
                FROM cad_device_known_good_baselines
                WHERE instance_id=?
                ORDER BY promoted_at_utc, baseline_id
                """,
                (instance_id,),
            ).fetchall()
        return tuple(self._baseline_from_row(row) for row in rows)

    def _baseline_from_row(
        self, row: sqlite3.Row
    ) -> DeviceKnownGoodBaseline:
        baseline = DeviceKnownGoodBaseline.model_validate_json(
            row['payload_json']
        )
        if (
            baseline.baseline_id != row['baseline_id']
            or baseline.baseline_sha256 != row['baseline_sha256']
            or baseline.document_id != row['document_id']
            or baseline.instance_id != row['instance_id']
            or baseline.snapshot_sha256 != row['snapshot_sha256']
        ):
            raise DeviceSnapshotIntegrityError(
                'known-good baseline row disagrees with its payload'
            )
        return baseline

    # ------------------------------------------------------------------
    # Firmware transitions

    def save_firmware_transition(
        self, transition: DeviceFirmwareTransition
    ) -> None:
        _assert_sealed(transition, 'transition_sha256', 'transition_id')
        existing = self.get_firmware_transition(transition.transition_id)
        if existing is not None:
            if existing.transition_sha256 == transition.transition_sha256:
                return
            raise DeviceSnapshotConflictError(
                'firmware transitions are append-only'
            )
        if transition.pre_snapshot_sha256 is not None and (
            self.get_snapshot_by_hash(transition.pre_snapshot_sha256)
            is None
        ):
            raise DeviceSnapshotIntegrityError(
                'a transition pre-snapshot must reference a persisted '
                'snapshot'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_device_firmware_transitions (
                    transition_id, transition_sha256, document_id,
                    instance_id, from_firmware, to_firmware,
                    migration_result, rollback_status, updated_at_utc,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transition.transition_id,
                    transition.transition_sha256,
                    transition.document_id,
                    transition.instance_id,
                    transition.from_firmware,
                    transition.to_firmware,
                    transition.migration_result,
                    transition.rollback_status,
                    transition.updated_at_utc,
                    transition.model_dump_json(),
                ),
            )

    def get_firmware_transition(
        self, transition_id: str
    ) -> DeviceFirmwareTransition | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT transition_id, transition_sha256, document_id,
                       instance_id, from_firmware, to_firmware,
                       payload_json
                FROM cad_device_firmware_transitions
                WHERE transition_id=?
                """,
                (transition_id,),
            ).fetchone()
        if row is None:
            return None
        return self._transition_from_row(row)

    def transitions_for_instance(
        self, instance_id: str
    ) -> tuple[DeviceFirmwareTransition, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT transition_id, transition_sha256, document_id,
                       instance_id, from_firmware, to_firmware,
                       payload_json
                FROM cad_device_firmware_transitions
                WHERE instance_id=?
                ORDER BY updated_at_utc, transition_id
                """,
                (instance_id,),
            ).fetchall()
        return tuple(self._transition_from_row(row) for row in rows)

    def _transition_from_row(
        self, row: sqlite3.Row
    ) -> DeviceFirmwareTransition:
        transition = DeviceFirmwareTransition.model_validate_json(
            row['payload_json']
        )
        if (
            transition.transition_id != row['transition_id']
            or transition.transition_sha256 != row['transition_sha256']
            or transition.document_id != row['document_id']
            or transition.instance_id != row['instance_id']
            or transition.from_firmware != row['from_firmware']
            or transition.to_firmware != row['to_firmware']
        ):
            raise DeviceSnapshotIntegrityError(
                'firmware transition row disagrees with its payload'
            )
        return transition

    # ------------------------------------------------------------------
    # Restore records

    def save_restore_record(
        self, record: ConfigurationRestoreRecord
    ) -> None:
        _assert_sealed(record, 'record_sha256', 'restore_id')
        existing = self.get_restore_record(record.restore_id)
        if existing is not None:
            if existing.record_sha256 == record.record_sha256:
                return
            raise DeviceSnapshotConflictError(
                'restore records are append-only'
            )
        if record.source_snapshot_sha256 is not None and (
            self.get_snapshot_by_hash(record.source_snapshot_sha256)
            is None
        ):
            raise DeviceSnapshotIntegrityError(
                'a restore source snapshot must reference a persisted '
                'snapshot'
            )
        if record.artifact_sha256 is not None and (
            self.get_backup_artifact(record.artifact_sha256) is None
        ):
            raise DeviceSnapshotIntegrityError(
                'a restore must name a persisted backup artifact — '
                'restoring from an unregistered artifact is not allowed'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_device_restore_records (
                    restore_id, record_sha256, document_id,
                    target_instance_id, artifact_sha256,
                    source_snapshot_sha256, result_status, verdict,
                    restored_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.restore_id,
                    record.record_sha256,
                    record.document_id,
                    record.target_instance_id,
                    record.artifact_sha256,
                    record.source_snapshot_sha256,
                    record.result_status,
                    record.verdict,
                    record.restored_at_utc,
                    record.model_dump_json(),
                ),
            )

    def get_restore_record(
        self, restore_id: str
    ) -> ConfigurationRestoreRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT restore_id, record_sha256, document_id,
                       target_instance_id, result_status, verdict,
                       payload_json
                FROM cad_device_restore_records
                WHERE restore_id=?
                """,
                (restore_id,),
            ).fetchone()
        if row is None:
            return None
        return self._restore_from_row(row)

    def restore_records_for_instance(
        self, instance_id: str
    ) -> tuple[ConfigurationRestoreRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT restore_id, record_sha256, document_id,
                       target_instance_id, result_status, verdict,
                       payload_json
                FROM cad_device_restore_records
                WHERE target_instance_id=?
                ORDER BY restored_at_utc, restore_id
                """,
                (instance_id,),
            ).fetchall()
        return tuple(self._restore_from_row(row) for row in rows)

    def _restore_from_row(
        self, row: sqlite3.Row
    ) -> ConfigurationRestoreRecord:
        record = ConfigurationRestoreRecord.model_validate_json(
            row['payload_json']
        )
        if (
            record.restore_id != row['restore_id']
            or record.record_sha256 != row['record_sha256']
            or record.document_id != row['document_id']
            or record.target_instance_id != row['target_instance_id']
            or record.result_status != row['result_status']
            or record.verdict != row['verdict']
        ):
            raise DeviceSnapshotIntegrityError(
                'restore record row disagrees with its payload'
            )
        return record

    # ------------------------------------------------------------------
    # Backup artifacts (#1055 model, persisted here)

    def save_backup_artifact(
        self, artifact: DeviceConfigurationBackupArtifact
    ) -> None:
        _assert_sealed(artifact, 'artifact_sha256', None)
        existing = self.get_backup_artifact(artifact.content_sha256)
        if existing is not None:
            if existing.artifact_sha256 == artifact.artifact_sha256:
                return
            raise DeviceSnapshotConflictError(
                'backup artifacts are content-addressed — the same '
                'content hash cannot carry different identity'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_device_backup_artifacts (
                    artifact_id, artifact_sha256, content_sha256,
                    device_equipment_id, manufacturer, model,
                    firmware_version, backup_format, privacy_class,
                    captured_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.artifact_id,
                    artifact.artifact_sha256,
                    artifact.content_sha256,
                    artifact.device_equipment_id,
                    artifact.manufacturer,
                    artifact.model,
                    artifact.firmware_version,
                    artifact.backup_format,
                    artifact.privacy_class,
                    artifact.captured_at_utc,
                    artifact.model_dump_json(),
                ),
            )

    def get_backup_artifact(
        self, content_sha256: str
    ) -> DeviceConfigurationBackupArtifact | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT artifact_id, artifact_sha256, content_sha256,
                       device_equipment_id, payload_json
                FROM cad_device_backup_artifacts
                WHERE content_sha256=?
                """,
                (content_sha256,),
            ).fetchone()
        if row is None:
            return None
        return self._artifact_from_row(row)

    def get_backup_artifact_by_id(
        self, artifact_id: str
    ) -> DeviceConfigurationBackupArtifact | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT artifact_id, artifact_sha256, content_sha256,
                       device_equipment_id, payload_json
                FROM cad_device_backup_artifacts
                WHERE artifact_id=?
                """,
                (artifact_id,),
            ).fetchone()
        if row is None:
            return None
        return self._artifact_from_row(row)

    def list_backup_artifacts(
        self, device_equipment_id: str
    ) -> tuple[DeviceConfigurationBackupArtifact, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT artifact_id, artifact_sha256, content_sha256,
                       device_equipment_id, payload_json
                FROM cad_device_backup_artifacts
                WHERE device_equipment_id=?
                ORDER BY captured_at_utc, artifact_id
                """,
                (device_equipment_id,),
            ).fetchall()
        return tuple(self._artifact_from_row(row) for row in rows)

    def _artifact_from_row(
        self, row: sqlite3.Row
    ) -> DeviceConfigurationBackupArtifact:
        artifact = DeviceConfigurationBackupArtifact.model_validate_json(
            row['payload_json']
        )
        if (
            artifact.artifact_id != row['artifact_id']
            or artifact.artifact_sha256 != row['artifact_sha256']
            or artifact.content_sha256 != row['content_sha256']
            or artifact.device_equipment_id != row['device_equipment_id']
        ):
            raise DeviceSnapshotIntegrityError(
                'backup artifact row disagrees with its payload'
            )
        return artifact

    # ------------------------------------------------------------------
    # Replacement assessments

    def save_replacement_assessment(
        self, assessment: ReplacementDeviceAssessment
    ) -> None:
        _assert_sealed(assessment, 'assessment_sha256', 'assessment_id')
        existing = self.get_replacement_assessment(
            assessment.assessment_id
        )
        if existing is not None:
            if existing.assessment_sha256 == assessment.assessment_sha256:
                return
            raise DeviceSnapshotConflictError(
                'replacement assessments are append-only'
            )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_device_replacement_assessments (
                    assessment_id, assessment_sha256, document_id,
                    source_instance_id, target_instance_id,
                    assessed_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assessment.assessment_id,
                    assessment.assessment_sha256,
                    assessment.document_id,
                    assessment.source_instance_id,
                    assessment.target_instance_id,
                    assessment.assessed_at_utc,
                    assessment.model_dump_json(),
                ),
            )

    def get_replacement_assessment(
        self, assessment_id: str
    ) -> ReplacementDeviceAssessment | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT assessment_id, assessment_sha256, document_id,
                       source_instance_id, target_instance_id,
                       payload_json
                FROM cad_device_replacement_assessments
                WHERE assessment_id=?
                """,
                (assessment_id,),
            ).fetchone()
        if row is None:
            return None
        return self._assessment_from_row(row)

    def replacement_assessments_for(
        self, instance_id: str
    ) -> tuple[ReplacementDeviceAssessment, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT assessment_id, assessment_sha256, document_id,
                       source_instance_id, target_instance_id,
                       payload_json
                FROM cad_device_replacement_assessments
                WHERE source_instance_id=? OR target_instance_id=?
                ORDER BY assessed_at_utc, assessment_id
                """,
                (instance_id, instance_id),
            ).fetchall()
        return tuple(self._assessment_from_row(row) for row in rows)

    def _assessment_from_row(
        self, row: sqlite3.Row
    ) -> ReplacementDeviceAssessment:
        assessment = ReplacementDeviceAssessment.model_validate_json(
            row['payload_json']
        )
        if (
            assessment.assessment_id != row['assessment_id']
            or assessment.assessment_sha256 != row['assessment_sha256']
            or assessment.document_id != row['document_id']
            or assessment.source_instance_id != row['source_instance_id']
            or assessment.target_instance_id != row['target_instance_id']
        ):
            raise DeviceSnapshotIntegrityError(
                'replacement assessment row disagrees with its payload'
            )
        return assessment


__all__ = [
    'CadDeviceSnapshotRepository',
    'DeviceSnapshotConflictError',
    'DeviceSnapshotIntegrityError',
]
