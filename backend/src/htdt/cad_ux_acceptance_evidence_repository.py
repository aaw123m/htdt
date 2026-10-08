"""Append-only persistence for the #880 UX acceptance evidence authority.

One table — ``cad_ux_acceptance_bundle_records`` — retains the sealed
record of each UX160 matrix-row evidence bundle produced by
``scripts/ux160_acceptance_run.py``. Shares the #806 ``_SealedStore``
machinery: save-time seal re-verification, read-time column-vs-payload
checks.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .cad_calibration_deployment_repository import (
    DeploymentConflictError,
    DeploymentIntegrityError,
    _SealedStore,
)
from .cad_ux_acceptance_evidence import CadUxAcceptanceBundleRecord


class CadUxAcceptanceEvidenceRepository:
    """Native storage for retained UX acceptance evidence bundles."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_ux_acceptance_bundle_records',
            )
        self.bundles = _SealedStore(
            self._connect,
            'cad_ux_acceptance_bundle_records',
            CadUxAcceptanceBundleRecord, 'bundle_id',
            'bundle_sha256',
            (
                ('document_id', '__document_id__'),
                ('matrix_id', 'matrix_id'),
                ('row_id', 'row_id'),
                ('run_attempt', 'run_attempt'),
                ('scenario', 'scenario'),
                ('scale_factor', 'scale_factor'),
                ('capture_mode', 'capture_mode'),
                ('verdict', 'verdict'),
                ('review_state', 'review_state'),
                ('checkpoints_total', 'checkpoints_total'),
                ('checkpoints_finding', 'checkpoints_finding'),
                ('manifest_sha256', 'manifest_sha256'),
                ('bundle_ref', 'bundle_ref'),
                ('finished_at_utc', 'finished_at_utc'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def save_bundle(self, record: CadUxAcceptanceBundleRecord) -> None:
        self.bundles.save(record)

    def get_bundle(
        self, bundle_id: str,
    ) -> CadUxAcceptanceBundleRecord | None:
        return self.bundles.get(bundle_id)

    def list_bundles(
        self, document_id: str | None = None,
    ) -> tuple[CadUxAcceptanceBundleRecord, ...]:
        return self.bundles.list(document_id)

    def list_bundles_for_row(
        self, matrix_id: str, row_id: str,
    ) -> tuple[CadUxAcceptanceBundleRecord, ...]:
        return tuple(
            record for record in self.bundles.list(None)
            if record.matrix_id == matrix_id and record.row_id == row_id
        )

    def next_run_attempt(self, matrix_id: str, row_id: str) -> int:
        """Next attempt number for a matrix row — 1 + the highest retained
        attempt. Evidence is append-only so attempts never reuse."""

        return 1 + max(
            (record.run_attempt
             for record in self.list_bundles_for_row(matrix_id, row_id)),
            default=0,
        )


__all__ = [
    'CadUxAcceptanceEvidenceRepository',
    'DeploymentConflictError',
    'DeploymentIntegrityError',
]
