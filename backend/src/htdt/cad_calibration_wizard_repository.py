"""Append-only persistence for the #877 calibration-wizard authority.

Four sealed stores in one repository:

- ``cad_calibration_wizard_runs`` — the sealed lane/pin commitments,
- ``cad_calibration_wizard_transitions`` — the sealed event log that is
  the sole resume authority for run state,
- ``cad_spl_check_acceptance_profiles`` — pinned SPL/reference-check
  acceptance criteria for Lane B and Lane C check specs,
- ``cad_campaign_check_plans`` — the sealed before/after check
  requirements bound to a campaign.

The stores only persist sealed records: every save re-derives the
record's sha256 from its payload and rejects a mismatch, every read
verifies id/sha/material columns against the stored payload, and an
existing id with a different sha raises — append-only, fail closed.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .cad_calibration_wizard import (
    CadCalibrationWizardRun,
    CadCalibrationWizardTransition,
    CadCampaignCheckPlan,
    CadSplCheckAcceptanceProfile,
)


class CalibrationWizardConflictError(ValueError):
    """A calibration-wizard save violated append-only identity rules."""


class CalibrationWizardIntegrityError(ValueError):
    """A stored calibration-wizard row disagreed with its payload."""


def _assert_sealed(record: object, sha_field: str, id_field: str) -> None:
    from .canonical_json import canonical_sha256

    sha = canonical_sha256(record.identity_payload())  # type: ignore[attr-defined]
    if getattr(record, sha_field) != sha:
        raise CalibrationWizardIntegrityError(
            'record payload does not match its sealed sha256')
    rid = getattr(record, id_field)
    prefix = rid.rsplit('-', 1)[0]
    if rid != f'{prefix}-{sha[:24]}':
        raise CalibrationWizardIntegrityError(
            'record id does not match its sealed sha256')


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

    def save(self, record: Any, connection: Any | None = None) -> None:
        if connection is not None:
            self._save_in(connection, record)
            return
        with closing(self._connect()) as owned, owned:
            self._save_in(owned, record)

    def _save_in(self, connection: Any, record: Any) -> None:
        _assert_sealed(record, self.sha_field, self.id_field)
        rid = getattr(record, self.id_field)
        existing = self._get_in(connection, rid)
        if existing is not None:
            if getattr(existing, self.sha_field) == getattr(
                    record, self.sha_field):
                return
            raise CalibrationWizardConflictError(
                f'{self.table} records are append-only')
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
        connection.execute(
            f'INSERT INTO {self.table} ({cols}) '
            f'VALUES ({placeholders})',
            values,
        )

    def get(self, rid: str) -> Any | None:
        with closing(self._connect()) as connection:
            return self._get_in(connection, rid)

    def _get_in(self, connection: Any, rid: str) -> Any | None:
        row = connection.execute(
            f'SELECT * FROM {self.table} WHERE {self.id_field}=?',
            (rid,),
        ).fetchone()
        if row is None:
            return None
        record = self.model.model_validate_json(row['payload_json'])
        if getattr(record, self.id_field) != row[self.id_field]:
            raise CalibrationWizardIntegrityError(
                f'stored {self.table} id disagrees with its payload')
        if getattr(record, self.sha_field) != row[self.sha_field]:
            raise CalibrationWizardIntegrityError(
                f'stored {self.table} sha disagrees with its payload')
        for column, path in self.columns:
            expected = self._column_value(record, path)
            if isinstance(expected, bool):
                expected = int(expected)
            if row[column] != expected:
                raise CalibrationWizardIntegrityError(
                    f'stored {self.table}.{column} disagrees '
                    'with its payload')
        return record

    def list(self, document_id: str | None = None) -> tuple[Any, ...]:
        query = f'SELECT * FROM {self.table}'
        params: tuple[str, ...] = ()
        if document_id is not None:
            query += ' WHERE document_id=?'
            params = (document_id,)
        query += ' ORDER BY seq ASC'
        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()
        records = []
        for row in rows:
            record = self.model.model_validate_json(row['payload_json'])
            if record.document_id != row['document_id']:
                raise CalibrationWizardIntegrityError(
                    f'stored {self.table}.document_id disagrees '
                    'with its payload')
            records.append(record)
        return tuple(records)


def _ref(column: str, path: str) -> tuple[str, str]:
    return (column, f'{path}.ref_id')


class CadCalibrationWizardRepository:
    """Native storage for the #877 calibration-wizard authority."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_calibration_wizard_runs',
                'cad_calibration_wizard_transitions',
                'cad_spl_check_acceptance_profiles',
                'cad_campaign_check_plans',
            )
        self.runs = _SealedStore(
            self._connect, 'cad_calibration_wizard_runs',
            CadCalibrationWizardRun, 'run_id', 'run_sha256',
            (
                ('document_id', '__document_id__'),
                ('lane', 'lane'),
                ('wizard_version', 'wizard_version'),
                ('engine_version', 'engine_version'),
                ('derivation_version', 'derivation_version'),
                ('check_evaluation_version', 'check_evaluation_version'),
                _ref('instrument_ref_id', 'instrument_ref'),
                _ref('calibrator_ref_id', 'calibrator_ref'),
                _ref('acceptance_profile_ref_id', 'acceptance_profile_ref'),
                _ref('campaign_ref_id', 'campaign_ref'),
                _ref('check_plan_ref_id', 'check_plan_ref'),
                ('check_kind', 'check_kind'),
                ('created_at_utc', 'created_at_utc'),
                ('created_by', 'created_by'),
            ),
        )
        self.transitions = _SealedStore(
            self._connect, 'cad_calibration_wizard_transitions',
            CadCalibrationWizardTransition, 'transition_id',
            'transition_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('run_ref_id', 'run_ref'),
                ('run_seq', 'seq'),
                ('event_kind', 'event_kind'),
                ('outcome', 'outcome'),
                ('actor', 'actor'),
                ('from_stage', 'from_stage'),
                ('to_stage', 'to_stage'),
                ('event_succeeded', 'event_succeeded'),
                ('result_tag', 'result_tag'),
                ('recorded_at_utc', 'recorded_at_utc'),
            ),
        )
        self.profiles = _SealedStore(
            self._connect, 'cad_spl_check_acceptance_profiles',
            CadSplCheckAcceptanceProfile, 'profile_id', 'profile_sha256',
            (
                ('document_id', '__document_id__'),
                ('name', 'name'),
                ('reference_level_db', 'reference_level_db'),
                ('reference_frequency_hz', 'reference_frequency_hz'),
                ('expected_measured_level_db', 'expected_measured_level_db'),
                ('level_tolerance_db', 'level_tolerance_db'),
                ('min_snr_db', 'min_snr_db'),
                ('declared_at_utc', 'declared_at_utc'),
                ('declared_by', 'declared_by'),
            ),
        )
        self.check_plans = _SealedStore(
            self._connect, 'cad_campaign_check_plans',
            CadCampaignCheckPlan, 'plan_id', 'plan_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('campaign_ref_id', 'campaign_ref'),
                ('blocked_on_failure', 'blocked_on_failure'),
                ('declared_at_utc', 'declared_at_utc'),
                ('declared_by', 'declared_by'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    # runs ------------------------------------------------------------

    def save_run(self, record: CadCalibrationWizardRun) -> None:
        self.runs.save(record)

    def get_run(self, run_id: str) -> CadCalibrationWizardRun | None:
        return self.runs.get(run_id)

    def list_runs(
        self, document_id: str | None = None,
    ) -> tuple[CadCalibrationWizardRun, ...]:
        return self.runs.list(document_id)

    # transitions -----------------------------------------------------

    def save_transition(
        self, record: CadCalibrationWizardTransition,
    ) -> None:
        self.transitions.save(record)

    def get_transition(
        self, transition_id: str,
    ) -> CadCalibrationWizardTransition | None:
        return self.transitions.get(transition_id)

    def list_transitions(
        self, run_id: str,
    ) -> tuple[CadCalibrationWizardTransition, ...]:
        """A run's sealed log, ordered by the run's own seq."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT * FROM cad_calibration_wizard_transitions '
                'WHERE run_ref_id=? ORDER BY run_seq ASC', (run_id,),
            ).fetchall()
        records = []
        for row in rows:
            record = CadCalibrationWizardTransition.model_validate_json(
                row['payload_json'])
            for column, path in self.transitions.columns:
                expected = self.transitions._column_value(record, path)
                if isinstance(expected, bool):
                    expected = int(expected)
                if row[column] != expected:
                    raise CalibrationWizardIntegrityError(
                        'stored transition column disagrees with payload')
            records.append(record)
        return tuple(records)

    # acceptance profiles -----------------------------------------------

    def save_profile(
        self, record: CadSplCheckAcceptanceProfile,
    ) -> None:
        self.profiles.save(record)

    def get_profile(
        self, profile_id: str,
    ) -> CadSplCheckAcceptanceProfile | None:
        return self.profiles.get(profile_id)

    def list_profiles(
        self, document_id: str | None = None,
    ) -> tuple[CadSplCheckAcceptanceProfile, ...]:
        return self.profiles.list(document_id)

    # campaign check plans -----------------------------------------------

    def save_check_plan(self, record: CadCampaignCheckPlan) -> None:
        self.check_plans.save(record)

    def get_check_plan(
        self, plan_id: str,
    ) -> CadCampaignCheckPlan | None:
        return self.check_plans.get(plan_id)

    def list_check_plans(
        self, document_id: str | None = None,
    ) -> tuple[CadCampaignCheckPlan, ...]:
        return self.check_plans.list(document_id)


__all__ = [
    'CadCalibrationWizardRepository',
    'CalibrationWizardConflictError',
    'CalibrationWizardIntegrityError',
]
