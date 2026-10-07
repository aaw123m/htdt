"""Append-only persistence for the #868 commissioning orchestrator.

Six tables in one repository — orchestration runs
(``cad_commissioning_orch_runs``), stage transitions
(``cad_commissioning_orch_transitions``), operator authorizations
(``cad_commissioning_orch_authorizations``), rollback records
(``cad_commissioning_orch_rollbacks``), before/after comparisons
(``cad_commissioning_orch_before_after``) and acceptance verdicts
(``cad_commissioning_orch_verdicts``). Shares the #806 ``_SealedStore``
machinery: save-time seal re-verification, read-time column-vs-payload
checks. The sealed transition log is what makes a run resumable after
an app restart — ``derive_run_state`` folds it deterministically.
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
    _ref,
)
from .cad_commissioning_orchestrator import (
    CommissioningAcceptanceVerdict,
    CommissioningBeforeAfter,
    CommissioningOperatorAuthorization,
    CommissioningOrchestrationRun,
    CommissioningRollback,
    CommissioningStageTransition,
)


class CadCommissioningOrchestratorRepository:
    """Native storage for the commissioning-orchestration authority."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_commissioning_orch_runs',
                'cad_commissioning_orch_transitions',
                'cad_commissioning_orch_authorizations',
                'cad_commissioning_orch_rollbacks',
                'cad_commissioning_orch_before_after',
                'cad_commissioning_orch_verdicts',
            )
        self.runs = _SealedStore(
            self._connect,
            'cad_commissioning_orch_runs',
            CommissioningOrchestrationRun, 'run_id',
            'run_sha256',
            (
                ('document_id', '__document_id__'),
                ('scene_revision_id', 'scene_revision_id'),
                _ref('provider_manifest_ref_id', 'provider_manifest_ref'),
                ('device_binding_sha256', 'device_binding_sha256'),
            ),
        )
        self.transitions = _SealedStore(
            self._connect,
            'cad_commissioning_orch_transitions',
            CommissioningStageTransition, 'transition_id',
            'transition_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('run_ref_id', 'run_ref'),
                ('seq_no', 'seq'),
                ('event_kind', 'event_kind'),
                ('outcome', 'outcome'),
                ('to_stage', 'to_stage'),
            ),
        )
        self.authorizations = _SealedStore(
            self._connect,
            'cad_commissioning_orch_authorizations',
            CommissioningOperatorAuthorization, 'authorization_id',
            'authorization_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('run_ref_id', 'run_ref'),
                ('scope', 'scope'),
            ),
        )
        self.rollbacks = _SealedStore(
            self._connect,
            'cad_commissioning_orch_rollbacks',
            CommissioningRollback, 'rollback_id',
            'rollback_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('run_ref_id', 'run_ref'),
                _ref('deployment_ref_id', 'deployment_ref'),
                ('outcome', 'outcome'),
            ),
        )
        self.before_after = _SealedStore(
            self._connect,
            'cad_commissioning_orch_before_after',
            CommissioningBeforeAfter, 'comparison_id',
            'comparison_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('run_ref_id', 'run_ref'),
                _ref('deployment_ref_id', 'deployment_ref'),
                ('verdict', 'verdict'),
            ),
        )
        self.verdicts = _SealedStore(
            self._connect,
            'cad_commissioning_orch_verdicts',
            CommissioningAcceptanceVerdict, 'verdict_id',
            'verdict_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('run_ref_id', 'run_ref'),
                ('verdict', 'verdict'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    # runs ----------------------------------------------------------

    def save_run(self, record: CommissioningOrchestrationRun) -> None:
        self.runs.save(record)

    def get_run(
        self, run_id: str,
    ) -> CommissioningOrchestrationRun | None:
        return self.runs.get(run_id)

    def list_runs(
        self, document_id: str | None = None,
    ) -> tuple[CommissioningOrchestrationRun, ...]:
        return self.runs.list(document_id)

    # transitions ----------------------------------------------------

    def save_transition(
        self, record: CommissioningStageTransition,
    ) -> None:
        self.transitions.save(record)

    def get_transition(
        self, transition_id: str,
    ) -> CommissioningStageTransition | None:
        return self.transitions.get(transition_id)

    def list_transitions(
        self, document_id: str | None = None,
    ) -> tuple[CommissioningStageTransition, ...]:
        return self.transitions.list(document_id)

    # operator authorizations -----------------------------------------

    def save_authorization(
        self, record: CommissioningOperatorAuthorization,
    ) -> None:
        self.authorizations.save(record)

    def get_authorization(
        self, authorization_id: str,
    ) -> CommissioningOperatorAuthorization | None:
        return self.authorizations.get(authorization_id)

    def list_authorizations(
        self, document_id: str | None = None,
    ) -> tuple[CommissioningOperatorAuthorization, ...]:
        return self.authorizations.list(document_id)

    # rollbacks ---------------------------------------------------------

    def save_rollback(self, record: CommissioningRollback) -> None:
        self.rollbacks.save(record)

    def get_rollback(
        self, rollback_id: str,
    ) -> CommissioningRollback | None:
        return self.rollbacks.get(rollback_id)

    def list_rollbacks(
        self, document_id: str | None = None,
    ) -> tuple[CommissioningRollback, ...]:
        return self.rollbacks.list(document_id)

    # before/after --------------------------------------------------------

    def save_before_after(self, record: CommissioningBeforeAfter) -> None:
        self.before_after.save(record)

    def get_before_after(
        self, comparison_id: str,
    ) -> CommissioningBeforeAfter | None:
        return self.before_after.get(comparison_id)

    def list_before_after(
        self, document_id: str | None = None,
    ) -> tuple[CommissioningBeforeAfter, ...]:
        return self.before_after.list(document_id)

    # verdicts -------------------------------------------------------------

    def save_verdict(self, record: CommissioningAcceptanceVerdict) -> None:
        self.verdicts.save(record)

    def get_verdict(
        self, verdict_id: str,
    ) -> CommissioningAcceptanceVerdict | None:
        return self.verdicts.get(verdict_id)

    def list_verdicts(
        self, document_id: str | None = None,
    ) -> tuple[CommissioningAcceptanceVerdict, ...]:
        return self.verdicts.list(document_id)


__all__ = [
    'CadCommissioningOrchestratorRepository',
    'DeploymentConflictError',
    'DeploymentIntegrityError',
]
