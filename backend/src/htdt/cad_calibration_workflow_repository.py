"""Applied-settings record persistence for the calibration workflow (#452).

``CadCalibrationRepository`` already stores plans/exports/verification —
this side table records what the user *actually applied* to the device,
including structured deviations, so "effective applied settings" is always
reconstructible as export ⊕ deviations and export ≠ applied stays explicit.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from .cad_calibration_repository import CadCalibrationRepository
from .cad_calibration_workflow import CadAppliedSettingsRecord
from .cad_repository import SceneRepository


class CadAppliedSettingsRepository:
    """Append-only store of effective applied-settings records."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.database_path = scene_repository.path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cad_applied_settings (
                    applied_id TEXT NOT NULL UNIQUE,
                    document_id TEXT NOT NULL,
                    calibration_plan_id TEXT NOT NULL,
                    applied_sha256 TEXT NOT NULL,
                    applied_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_cad_applied_settings_plan
                ON cad_applied_settings(calibration_plan_id)
                """
            )

    def save_applied(
        self,
        record: CadAppliedSettingsRecord,
        *,
        calibration_repository: CadCalibrationRepository | None = None,
    ) -> CadAppliedSettingsRecord:
        self._validate(record, calibration_repository)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO cad_applied_settings(
                    applied_id, document_id, calibration_plan_id,
                    applied_sha256, applied_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.applied_id,
                    record.document_id,
                    record.calibration_plan_id,
                    record.applied_sha256,
                    record.applied_at_utc,
                    record.model_dump_json(),
                ),
            )
        return record

    def get_applied(self, applied_id: str) -> CadAppliedSettingsRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT payload_json FROM cad_applied_settings WHERE applied_id=?',
                (applied_id,),
            ).fetchone()
        if row is None:
            return None
        return self._validate(
            CadAppliedSettingsRecord.model_validate_json(row['payload_json'])
        )

    def list_applied(
        self, plan_id: str
    ) -> tuple[CadAppliedSettingsRecord, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM cad_applied_settings
                WHERE calibration_plan_id=? ORDER BY applied_at_utc, applied_id
                """,
                (plan_id,),
            ).fetchall()
        return tuple(
            self._validate(
                CadAppliedSettingsRecord.model_validate_json(row['payload_json'])
            )
            for row in rows
        )

    def _validate(
        self,
        record: CadAppliedSettingsRecord,
        calibration_repository: CadCalibrationRepository | None = None,
    ) -> CadAppliedSettingsRecord:
        if calibration_repository is None:
            head = self.scene_repository.current_head(record.document_id)
            if head is None:
                raise ValueError(
                    f'applied settings {record.applied_id} references an unknown document'
                )
            return record
        plan = calibration_repository.get_plan(record.calibration_plan_id)
        if plan is None:
            raise ValueError(
                f'applied settings {record.applied_id} references an unknown plan'
            )
        if plan.plan_semantic_sha256 != record.calibration_plan_semantic_sha256:
            raise ValueError(
                'applied settings pinned to a superseded calibration plan'
            )
        snapshot = calibration_repository.get_export(
            record.exported_settings_id
        )
        if snapshot is None:
            raise ValueError(
                f'applied settings {record.applied_id} references an unknown export'
            )
        if (
            snapshot.exported_settings_semantic_sha256
            != record.exported_settings_semantic_sha256
        ):
            raise ValueError('applied settings export hash mismatch')
        return record
