"""Append-only persistence for REV56-MEASEV measurement evidence (#572/#573/#575).

Six authority tables, one repository — mirroring the
``measurement_campaign`` multi-probe convention:

- ``cad_measurement_uncertainty_budgets`` — sealed uncertainty budgets
- ``cad_measurement_significance_assessments`` — residual/delta verdicts
- ``cad_measurement_state_policies`` — versioned state-control policies
- ``cad_measurement_state_snapshots`` — capture-time state records
- ``cad_measurement_state_verdicts`` — comparability/stationarity verdicts
- ``cad_measurement_transforms`` — transformation DAG nodes

Rows are re-validated on read through the sealed models; a tampered or
stale row fails closed. Persistence is owned by the migration authority —
this repository verifies the migrated contract, never converges it.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from .cad_measurement_state import (
    MeasurementStateSnapshot,
    StateComparabilityVerdict,
    StateControlPolicy,
)
from .cad_measurement_transform import MeasurementTransform
from .cad_measurement_uncertainty import (
    MeasurementUncertaintyBudget,
    ResidualSignificanceAssessment,
)
from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .clock import utc_now_iso as _utc_now


class MeasurementEvidenceConflictError(ValueError):
    """An evidence record was saved twice with different content."""


class CadMeasurementEvidenceRepository:
    """Durable store for measurement-evidence authorities."""

    _TABLES: tuple[str, ...] = (
        'cad_measurement_uncertainty_budgets',
        'cad_measurement_significance_assessments',
        'cad_measurement_state_policies',
        'cad_measurement_state_snapshots',
        'cad_measurement_state_verdicts',
        'cad_measurement_transforms',
    )

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
            require_native_tables(connection, *self._TABLES)

    # -- generic plumbing ---------------------------------------------------

    def _insert_once(
        self,
        *,
        table: str,
        columns: tuple[str, ...],
        values: tuple[Any, ...],
        id_column: str,
        record_id: str,
        payload: str,
    ) -> None:
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            existing = connection.execute(
                f'SELECT payload_json FROM {table} WHERE {id_column}=?',
                (record_id,),
            ).fetchone()
        if existing is not None:
            if existing['payload_json'] != payload:
                raise MeasurementEvidenceConflictError(
                    f'{id_column} {record_id} is persisted with different '
                    'content'
                )
            return
        insert_columns = (*columns, 'payload_json', 'recorded_at_utc')
        placeholders = ', '.join('?' for _ in insert_columns)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                f'INSERT INTO {table} ({", ".join(insert_columns)}) '
                f'VALUES ({placeholders})',
                (*values, payload, _utc_now()),
            )

    def _select_row(
        self, table: str, key_column: str, key: str
    ) -> sqlite3.Row | None:
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            return connection.execute(
                f'SELECT * FROM {table} WHERE {key_column}=?', (key,)
            ).fetchone()

    def _select_rows(
        self,
        table: str,
        *,
        where: str = '1=1',
        params: tuple[Any, ...] = (),
    ) -> list[sqlite3.Row]:
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            return connection.execute(
                f'SELECT * FROM {table} WHERE {where} ORDER BY seq ASC',
                params,
            ).fetchall()

    # -- uncertainty budgets (#572) -----------------------------------------

    def save_uncertainty_budget(
        self, budget: MeasurementUncertaintyBudget
    ) -> None:
        payload = budget.model_dump_json()
        self._insert_once(
            table='cad_measurement_uncertainty_budgets',
            columns=(
                'budget_id',
                'semantic_sha256',
                'document_id',
                'measurement_id',
                'dataset_id',
            ),
            values=(
                budget.budget_id,
                budget.semantic_sha256,
                budget.document_id,
                budget.measurement_id,
                budget.dataset_id,
            ),
            id_column='budget_id',
            record_id=budget.budget_id,
            payload=payload,
        )

    def get_uncertainty_budget(
        self, budget_id: str
    ) -> MeasurementUncertaintyBudget | None:
        row = self._select_row(
            'cad_measurement_uncertainty_budgets', 'budget_id', budget_id
        )
        if row is None:
            return None
        return self._budget_from_row(row)

    def _budget_from_row(self, row: sqlite3.Row) -> MeasurementUncertaintyBudget:
        return self._budget_checks(row)

    def _budget_checks(self, row: sqlite3.Row) -> MeasurementUncertaintyBudget:
        record = MeasurementUncertaintyBudget.model_validate_json(
            row['payload_json']
        )
        if (
            row['budget_id'] != record.budget_id
            or row['semantic_sha256'] != record.semantic_sha256
            or row['document_id'] != record.document_id
            or row['measurement_id'] != record.measurement_id
            or row['dataset_id'] != record.dataset_id
        ):
            raise ValueError(
                'persisted uncertainty budget row disagrees with its payload'
            )
        return record

    def list_uncertainty_budgets(
        self, document_id: str
    ) -> tuple[MeasurementUncertaintyBudget, ...]:
        rows = self._select_rows(
            'cad_measurement_uncertainty_budgets',
            where='document_id=?',
            params=(document_id,),
        )
        return tuple(self._budget_checks(row) for row in rows)

    def uncertainty_budgets_for_measurement(
        self, measurement_id: str
    ) -> tuple[MeasurementUncertaintyBudget, ...]:
        rows = self._select_rows(
            'cad_measurement_uncertainty_budgets',
            where='measurement_id=?',
            params=(measurement_id,),
        )
        return tuple(self._budget_checks(row) for row in rows)

    # -- significance assessments (#572) -------------------------------------

    def save_significance_assessment(
        self, assessment: ResidualSignificanceAssessment
    ) -> None:
        payload = assessment.model_dump_json()
        self._insert_once(
            table='cad_measurement_significance_assessments',
            columns=(
                'assessment_id',
                'semantic_sha256',
                'document_id',
                'subject_kind',
                'subject_ref_id',
                'budget_id',
            ),
            values=(
                assessment.assessment_id,
                assessment.semantic_sha256,
                assessment.document_id,
                assessment.subject_kind,
                assessment.subject_ref_id,
                assessment.budget_id,
            ),
            id_column='assessment_id',
            record_id=assessment.assessment_id,
            payload=payload,
        )

    def get_significance_assessment(
        self, assessment_id: str
    ) -> ResidualSignificanceAssessment | None:
        row = self._select_row(
            'cad_measurement_significance_assessments',
            'assessment_id',
            assessment_id,
        )
        if row is None:
            return None
        return self._assessment_from_row(row)

    def _assessment_from_row(
        self, row: sqlite3.Row
    ) -> ResidualSignificanceAssessment:
        record = ResidualSignificanceAssessment.model_validate_json(
            row['payload_json']
        )
        if (
            row['assessment_id'] != record.assessment_id
            or row['semantic_sha256'] != record.semantic_sha256
            or row['document_id'] != record.document_id
            or row['subject_kind'] != record.subject_kind
            or row['subject_ref_id'] != record.subject_ref_id
            or row['budget_id'] != record.budget_id
        ):
            raise ValueError(
                'persisted significance row disagrees with its payload'
            )
        return record

    def list_significance_assessments(
        self, document_id: str
    ) -> tuple[ResidualSignificanceAssessment, ...]:
        rows = self._select_rows(
            'cad_measurement_significance_assessments',
            where='document_id=?',
            params=(document_id,),
        )
        return tuple(self._assessment_from_row(row) for row in rows)

    # -- state control policies (#573) ----------------------------------------

    def save_state_policy(self, policy: StateControlPolicy) -> None:
        payload = policy.model_dump_json()
        self._insert_once(
            table='cad_measurement_state_policies',
            columns=(
                'policy_id',
                'semantic_sha256',
                'document_id',
                'name',
            ),
            values=(
                policy.policy_id,
                policy.semantic_sha256,
                policy.document_id,
                policy.name,
            ),
            id_column='policy_id',
            record_id=policy.policy_id,
            payload=payload,
        )

    def get_state_policy(
        self, policy_id: str
    ) -> StateControlPolicy | None:
        row = self._select_row(
            'cad_measurement_state_policies', 'policy_id', policy_id
        )
        if row is None:
            return None
        return self._policy_from_row(row)

    def _policy_from_row(self, row: sqlite3.Row) -> StateControlPolicy:
        record = StateControlPolicy.model_validate_json(row['payload_json'])
        if (
            row['policy_id'] != record.policy_id
            or row['semantic_sha256'] != record.semantic_sha256
            or row['document_id'] != record.document_id
            or row['name'] != record.name
        ):
            raise ValueError(
                'persisted state policy row disagrees with its payload'
            )
        return record

    def list_state_policies(
        self, document_id: str
    ) -> tuple[StateControlPolicy, ...]:
        rows = self._select_rows(
            'cad_measurement_state_policies',
            where='document_id=?',
            params=(document_id,),
        )
        return tuple(self._policy_from_row(row) for row in rows)

    # -- state snapshots (#573) ------------------------------------------------

    def save_state_snapshot(
        self, snapshot: MeasurementStateSnapshot
    ) -> None:
        payload = snapshot.model_dump_json()
        self._insert_once(
            table='cad_measurement_state_snapshots',
            columns=(
                'snapshot_id',
                'semantic_sha256',
                'document_id',
                'measurement_id',
                'observed_at_utc',
            ),
            values=(
                snapshot.snapshot_id,
                snapshot.semantic_sha256,
                snapshot.document_id,
                snapshot.measurement_id,
                snapshot.observed_at_utc,
            ),
            id_column='snapshot_id',
            record_id=snapshot.snapshot_id,
            payload=payload,
        )

    def get_state_snapshot(
        self, snapshot_id: str
    ) -> MeasurementStateSnapshot | None:
        row = self._select_row(
            'cad_measurement_state_snapshots', 'snapshot_id', snapshot_id
        )
        if row is None:
            return None
        return self._snapshot_from_row(row)

    def _snapshot_from_row(
        self, row: sqlite3.Row
    ) -> MeasurementStateSnapshot:
        record = MeasurementStateSnapshot.model_validate_json(
            row['payload_json']
        )
        if (
            row['snapshot_id'] != record.snapshot_id
            or row['semantic_sha256'] != record.semantic_sha256
            or row['document_id'] != record.document_id
            or row['measurement_id'] != record.measurement_id
            or row['observed_at_utc'] != record.observed_at_utc
        ):
            raise ValueError(
                'persisted state snapshot row disagrees with its payload'
            )
        return record

    def list_state_snapshots(
        self, document_id: str
    ) -> tuple[MeasurementStateSnapshot, ...]:
        rows = self._select_rows(
            'cad_measurement_state_snapshots',
            where='document_id=?',
            params=(document_id,),
        )
        return tuple(self._snapshot_from_row(row) for row in rows)

    def state_snapshots_for_measurement(
        self, measurement_id: str
    ) -> tuple[MeasurementStateSnapshot, ...]:
        rows = self._select_rows(
            'cad_measurement_state_snapshots',
            where='measurement_id=?',
            params=(measurement_id,),
        )
        return tuple(self._snapshot_from_row(row) for row in rows)

    # -- state verdicts (#573) -------------------------------------------------

    def save_state_verdict(
        self, verdict: StateComparabilityVerdict
    ) -> None:
        payload = verdict.model_dump_json()
        self._insert_once(
            table='cad_measurement_state_verdicts',
            columns=(
                'verdict_id',
                'semantic_sha256',
                'document_id',
                'subject_kind',
                'state',
                'policy_id',
            ),
            values=(
                verdict.verdict_id,
                verdict.semantic_sha256,
                verdict.document_id,
                verdict.subject_kind,
                verdict.state,
                verdict.policy_id,
            ),
            id_column='verdict_id',
            record_id=verdict.verdict_id,
            payload=payload,
        )

    def get_state_verdict(
        self, verdict_id: str
    ) -> StateComparabilityVerdict | None:
        row = self._select_row(
            'cad_measurement_state_verdicts', 'verdict_id', verdict_id
        )
        if row is None:
            return None
        return self._verdict_from_row(row)

    def _verdict_from_row(
        self, row: sqlite3.Row
    ) -> StateComparabilityVerdict:
        record = StateComparabilityVerdict.model_validate_json(
            row['payload_json']
        )
        if (
            row['verdict_id'] != record.verdict_id
            or row['semantic_sha256'] != record.semantic_sha256
            or row['document_id'] != record.document_id
            or row['subject_kind'] != record.subject_kind
            or row['state'] != record.state
            or row['policy_id'] != record.policy_id
        ):
            raise ValueError(
                'persisted state verdict row disagrees with its payload'
            )
        return record

    def list_state_verdicts(
        self, document_id: str
    ) -> tuple[StateComparabilityVerdict, ...]:
        rows = self._select_rows(
            'cad_measurement_state_verdicts',
            where='document_id=?',
            params=(document_id,),
        )
        return tuple(self._verdict_from_row(row) for row in rows)

    # -- measurement transforms (#575) ------------------------------------------

    def save_transform(self, transform: MeasurementTransform) -> None:
        payload = transform.model_dump_json()
        self._insert_once(
            table='cad_measurement_transforms',
            columns=(
                'transform_id',
                'semantic_sha256',
                'document_id',
                'kind',
            ),
            values=(
                transform.transform_id,
                transform.semantic_sha256,
                transform.document_id,
                transform.kind,
            ),
            id_column='transform_id',
            record_id=transform.transform_id,
            payload=payload,
        )

    def get_transform(
        self, transform_id: str
    ) -> MeasurementTransform | None:
        row = self._select_row(
            'cad_measurement_transforms', 'transform_id', transform_id
        )
        if row is None:
            return None
        return self._transform_from_row(row)

    def _transform_from_row(self, row: sqlite3.Row) -> MeasurementTransform:
        record = MeasurementTransform.model_validate_json(row['payload_json'])
        if (
            row['transform_id'] != record.transform_id
            or row['semantic_sha256'] != record.semantic_sha256
            or row['document_id'] != record.document_id
            or row['kind'] != record.kind
        ):
            raise ValueError(
                'persisted transform row disagrees with its payload'
            )
        return record

    def list_transforms(
        self, document_id: str
    ) -> tuple[MeasurementTransform, ...]:
        rows = self._select_rows(
            'cad_measurement_transforms',
            where='document_id=?',
            params=(document_id,),
        )
        return tuple(self._transform_from_row(row) for row in rows)

    def derived_for_document(
        self, document_id: str
    ) -> tuple[MeasurementTransform, ...]:
        """Transforms that produced a derived output — the derived-evidence
        view; derived values are never indistinguishable from measured."""
        return tuple(
            t
            for t in self.list_transforms(document_id)
            if t.derived_output is not None
        )


__all__ = [
    'CadMeasurementEvidenceRepository',
    'MeasurementEvidenceConflictError',
]
