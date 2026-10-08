"""Append-only persistence for the #889 application updater.

Eight tables in one repository — packages
(``cad_update_packages``), sessions (``cad_update_sessions``), the sealed
stage-transition log (``cad_update_transitions``), preflight reports
(``cad_update_preflight_reports``), restore points
(``cad_update_restore_points``), health reports
(``cad_update_health_reports``), one-shot operator authorizations
(``cad_update_authorizations``) and terminal outcomes
(``cad_update_outcomes``). Shares the #806 ``_SealedStore`` machinery:
save-time seal re-verification, read-time column-vs-payload checks. The
sealed transition log is what makes an interrupted update resumable —
``derive_update_state`` folds it deterministically.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .cad_calibration_deployment_repository import (
    DeploymentConflictError,
    DeploymentIntegrityError,
    _SealedStore,
    _ref,
)
from .cad_application_update import (
    UpdateHealthReport,
    UpdateOperatorAuthorization,
    UpdateOutcomeRecord,
    UpdatePackageDescriptor,
    UpdatePreflightReport,
    UpdateRestorePoint,
    UpdateSessionRecord,
    UpdateStageTransition,
)


class CadApplicationUpdateRepository:
    """Native storage for the application-update authority."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_update_packages',
                'cad_update_sessions',
                'cad_update_transitions',
                'cad_update_preflight_reports',
                'cad_update_restore_points',
                'cad_update_health_reports',
                'cad_update_authorizations',
                'cad_update_outcomes',
            )
        self.packages = _SealedStore(
            self._connect,
            'cad_update_packages',
            UpdatePackageDescriptor, 'package_id',
            'package_sha256',
            (
                ('document_id', '__document_id__'),
                ('target_version', 'target_version'),
                ('channel', 'channel'),
                ('signature_status', 'signature.status'),
                ('declared_by', 'declared_by'),
            ),
        )
        self.sessions = _SealedStore(
            self._connect,
            'cad_update_sessions',
            UpdateSessionRecord, 'session_id',
            'session_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('package_ref_id', 'package_ref'),
                ('signature_policy', 'signature_policy'),
                ('opened_by', 'opened_by'),
            ),
        )
        self.transitions = _SealedStore(
            self._connect,
            'cad_update_transitions',
            UpdateStageTransition, 'transition_id',
            'transition_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('session_ref_id', 'session_ref'),
                ('seq_no', 'seq'),
                ('event_kind', 'event_kind'),
                ('outcome', 'outcome'),
                ('to_stage', 'to_stage'),
            ),
        )
        self.preflight_reports = _SealedStore(
            self._connect,
            'cad_update_preflight_reports',
            UpdatePreflightReport, 'report_id',
            'report_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('session_ref_id', 'session_ref'),
                _ref('package_ref_id', 'package_ref'),
                ('verdict', 'verdict'),
            ),
        )
        self.restore_points = _SealedStore(
            self._connect,
            'cad_update_restore_points',
            UpdateRestorePoint, 'restore_id',
            'restore_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('session_ref_id', 'session_ref'),
                ('data_backup_kind', 'data_backup_kind'),
                ('migration_boundary', 'migration_boundary'),
                ('rollback_scope_capable', 'rollback_scope_capable'),
            ),
        )
        self.health_reports = _SealedStore(
            self._connect,
            'cad_update_health_reports',
            UpdateHealthReport, 'report_id',
            'report_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('session_ref_id', 'session_ref'),
                ('verdict', 'verdict'),
            ),
        )
        self.authorizations = _SealedStore(
            self._connect,
            'cad_update_authorizations',
            UpdateOperatorAuthorization, 'authorization_id',
            'authorization_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('session_ref_id', 'session_ref'),
                ('scope', 'scope'),
                ('authorized_by', 'authorized_by'),
            ),
        )
        self.outcomes = _SealedStore(
            self._connect,
            'cad_update_outcomes',
            UpdateOutcomeRecord, 'outcome_id',
            'outcome_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('session_ref_id', 'session_ref'),
                ('verdict', 'verdict'),
                ('rollback_scope', 'rollback_scope'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def _list_for_session(
        self,
        store: _SealedStore,
        session_id: str,
    ) -> tuple[Any, ...]:
        """List rows pinned to one session with payload verification."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f'SELECT * FROM {store.table} '
                'WHERE session_ref_id=? ORDER BY seq ASC',
                (session_id,),
            ).fetchall()
        records = []
        for row in rows:
            record = store.model.model_validate_json(row['payload_json'])
            if getattr(record, store.id_field) != row[store.id_field]:
                raise DeploymentIntegrityError(
                    f'stored {store.table} id disagrees with its '
                    'payload')
            for column, path in store.columns:
                expected = store._column_value(record, path)
                if isinstance(expected, bool):
                    expected = int(expected)
                if row[column] != expected:
                    raise DeploymentIntegrityError(
                        f'stored {store.table}.{column} disagrees '
                        'with its payload')
            records.append(record)
        return tuple(records)

    # packages -------------------------------------------------------

    def save_package(self, record: UpdatePackageDescriptor) -> None:
        self.packages.save(record)

    def get_package(
        self, package_id: str,
    ) -> UpdatePackageDescriptor | None:
        return self.packages.get(package_id)

    def list_packages(
        self, document_id: str | None = None,
    ) -> tuple[UpdatePackageDescriptor, ...]:
        return self.packages.list(document_id)

    # sessions -------------------------------------------------------

    def save_session(self, record: UpdateSessionRecord) -> None:
        self.sessions.save(record)

    def get_session(
        self, session_id: str,
    ) -> UpdateSessionRecord | None:
        return self.sessions.get(session_id)

    def list_sessions(
        self, document_id: str | None = None,
    ) -> tuple[UpdateSessionRecord, ...]:
        return self.sessions.list(document_id)

    # transitions -------------------------------------------------------

    def save_transition(self, record: UpdateStageTransition) -> None:
        self.transitions.save(record)

    def get_transition(
        self, transition_id: str,
    ) -> UpdateStageTransition | None:
        return self.transitions.get(transition_id)

    def list_transitions(
        self, session_id: str,
    ) -> tuple[UpdateStageTransition, ...]:
        return self._list_for_session(self.transitions, session_id)

    # preflight reports -------------------------------------------------

    def save_preflight_report(self, record: UpdatePreflightReport) -> None:
        self.preflight_reports.save(record)

    def get_preflight_report(
        self, report_id: str,
    ) -> UpdatePreflightReport | None:
        return self.preflight_reports.get(report_id)

    def list_preflight_reports(
        self, session_id: str,
    ) -> tuple[UpdatePreflightReport, ...]:
        return self._list_for_session(self.preflight_reports, session_id)

    # restore points -------------------------------------------------------

    def save_restore_point(self, record: UpdateRestorePoint) -> None:
        self.restore_points.save(record)

    def get_restore_point(
        self, restore_id: str,
    ) -> UpdateRestorePoint | None:
        return self.restore_points.get(restore_id)

    def list_restore_points(
        self, session_id: str,
    ) -> tuple[UpdateRestorePoint, ...]:
        return self._list_for_session(self.restore_points, session_id)

    # health reports --------------------------------------------------------

    def save_health_report(self, record: UpdateHealthReport) -> None:
        self.health_reports.save(record)

    def get_health_report(
        self, report_id: str,
    ) -> UpdateHealthReport | None:
        return self.health_reports.get(report_id)

    def list_health_reports(
        self, session_id: str,
    ) -> tuple[UpdateHealthReport, ...]:
        return self._list_for_session(self.health_reports, session_id)

    # authorizations ----------------------------------------------------------

    def save_authorization(
        self, record: UpdateOperatorAuthorization,
    ) -> None:
        self.authorizations.save(record)

    def get_authorization(
        self, authorization_id: str,
    ) -> UpdateOperatorAuthorization | None:
        return self.authorizations.get(authorization_id)

    def list_authorizations(
        self, session_id: str,
    ) -> tuple[UpdateOperatorAuthorization, ...]:
        return self._list_for_session(self.authorizations, session_id)

    # outcomes -------------------------------------------------------------------

    def save_outcome(self, record: UpdateOutcomeRecord) -> None:
        self.outcomes.save(record)

    def get_outcome(
        self, outcome_id: str,
    ) -> UpdateOutcomeRecord | None:
        return self.outcomes.get(outcome_id)

    def list_outcomes(
        self, session_id: str,
    ) -> tuple[UpdateOutcomeRecord, ...]:
        return self._list_for_session(self.outcomes, session_id)


__all__ = [
    'CadApplicationUpdateRepository',
    'DeploymentConflictError',
    'DeploymentIntegrityError',
]
