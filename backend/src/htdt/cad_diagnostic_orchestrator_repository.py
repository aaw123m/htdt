"""Append-only persistence for the #885 diagnostic orchestrator.

Eight tables in one repository — sessions (``cad_diagnostic_sessions``),
the sealed stage-transition log
(``cad_diagnostic_session_transitions``), session hypothesis entries
(``cad_diagnostic_session_hypotheses``), discriminating test plans
(``cad_diagnostic_test_plans``), observations
(``cad_diagnostic_observations``), evidence updates
(``cad_diagnostic_evidence_updates``), resolutions
(``cad_diagnostic_resolutions``) and operator authorizations
(``cad_diagnostic_authorizations``). Shares the #806 ``_SealedStore``
machinery: save-time seal re-verification, read-time column-vs-payload
checks. The sealed transition log is what makes a session resumable —
``derive_session_state`` folds it deterministically.
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
from .cad_diagnostic_orchestrator import (
    DiagnosticEvidenceUpdate,
    DiagnosticHypothesisEntry,
    DiagnosticObservationRecord,
    DiagnosticOperatorAuthorization,
    DiagnosticResolutionRecord,
    DiagnosticSessionRecord,
    DiagnosticStageTransition,
    DiagnosticTestPlan,
)


class CadDiagnosticOrchestratorRepository:
    """Native storage for the diagnostic-orchestration authority."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_diagnostic_sessions',
                'cad_diagnostic_session_transitions',
                'cad_diagnostic_session_hypotheses',
                'cad_diagnostic_test_plans',
                'cad_diagnostic_observations',
                'cad_diagnostic_evidence_updates',
                'cad_diagnostic_resolutions',
                'cad_diagnostic_authorizations',
            )
        self.sessions = _SealedStore(
            self._connect,
            'cad_diagnostic_sessions',
            DiagnosticSessionRecord, 'session_id',
            'session_sha256',
            (
                ('document_id', '__document_id__'),
                ('fault_tree_id', 'fault_tree_id'),
                _ref('case_ref_id', 'case_ref'),
                _ref('symptom_ref_id', 'symptom_ref'),
            ),
        )
        self.transitions = _SealedStore(
            self._connect,
            'cad_diagnostic_session_transitions',
            DiagnosticStageTransition, 'transition_id',
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
        self.hypothesis_entries = _SealedStore(
            self._connect,
            'cad_diagnostic_session_hypotheses',
            DiagnosticHypothesisEntry, 'entry_id',
            'entry_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('session_ref_id', 'session_ref'),
                ('hypothesis_key', 'hypothesis_key'),
                _ref('hypothesis_ref_id', 'hypothesis_ref'),
                ('cause_family', 'cause_family'),
            ),
        )
        self.test_plans = _SealedStore(
            self._connect,
            'cad_diagnostic_test_plans',
            DiagnosticTestPlan, 'plan_id',
            'plan_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('session_ref_id', 'session_ref'),
                ('template_id', 'template_id'),
                ('plan_seq', 'plan_seq'),
                ('mechanism', 'mechanism'),
                ('safety_class', 'safety_class'),
            ),
        )
        self.observations = _SealedStore(
            self._connect,
            'cad_diagnostic_observations',
            DiagnosticObservationRecord, 'observation_id',
            'observation_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('session_ref_id', 'session_ref'),
                _ref('plan_ref_id', 'plan_ref'),
                ('mechanism', 'mechanism'),
            ),
        )
        self.evidence_updates = _SealedStore(
            self._connect,
            'cad_diagnostic_evidence_updates',
            DiagnosticEvidenceUpdate, 'update_id',
            'update_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('session_ref_id', 'session_ref'),
                _ref('observation_ref_id', 'observation_ref'),
            ),
        )
        self.resolutions = _SealedStore(
            self._connect,
            'cad_diagnostic_resolutions',
            DiagnosticResolutionRecord, 'resolution_id',
            'resolution_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('session_ref_id', 'session_ref'),
                ('verdict', 'verdict'),
                _ref('verdict_ref_id', 'verdict_ref'),
            ),
        )
        self.authorizations = _SealedStore(
            self._connect,
            'cad_diagnostic_authorizations',
            DiagnosticOperatorAuthorization, 'authorization_id',
            'authorization_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('session_ref_id', 'session_ref'),
                _ref('plan_ref_id', 'plan_ref'),
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

    # sessions -------------------------------------------------------

    def save_session(self, record: DiagnosticSessionRecord) -> None:
        self.sessions.save(record)

    def get_session(
        self, session_id: str,
    ) -> DiagnosticSessionRecord | None:
        return self.sessions.get(session_id)

    def list_sessions(
        self, document_id: str | None = None,
    ) -> tuple[DiagnosticSessionRecord, ...]:
        return self.sessions.list(document_id)

    # transitions -----------------------------------------------------

    def save_transition(
        self, record: DiagnosticStageTransition,
    ) -> None:
        self.transitions.save(record)

    def get_transition(
        self, transition_id: str,
    ) -> DiagnosticStageTransition | None:
        return self.transitions.get(transition_id)

    def list_transitions(
        self, session_id: str,
    ) -> tuple[DiagnosticStageTransition, ...]:
        return self._list_for_session(self.transitions, session_id)

    # hypothesis entries ------------------------------------------------

    def save_hypothesis_entry(
        self, record: DiagnosticHypothesisEntry,
    ) -> None:
        self.hypothesis_entries.save(record)

    def get_hypothesis_entry(
        self, entry_id: str,
    ) -> DiagnosticHypothesisEntry | None:
        return self.hypothesis_entries.get(entry_id)

    def list_hypothesis_entries(
        self, session_id: str,
    ) -> tuple[DiagnosticHypothesisEntry, ...]:
        return self._list_for_session(
            self.hypothesis_entries, session_id)

    # test plans ----------------------------------------------------------

    def save_test_plan(self, record: DiagnosticTestPlan) -> None:
        self.test_plans.save(record)

    def get_test_plan(
        self, plan_id: str,
    ) -> DiagnosticTestPlan | None:
        return self.test_plans.get(plan_id)

    def list_test_plans(
        self, session_id: str,
    ) -> tuple[DiagnosticTestPlan, ...]:
        return self._list_for_session(self.test_plans, session_id)

    # observations ----------------------------------------------------------

    def save_observation(
        self, record: DiagnosticObservationRecord,
    ) -> None:
        self.observations.save(record)

    def get_observation(
        self, observation_id: str,
    ) -> DiagnosticObservationRecord | None:
        return self.observations.get(observation_id)

    def list_observations(
        self, session_id: str,
    ) -> tuple[DiagnosticObservationRecord, ...]:
        return self._list_for_session(self.observations, session_id)

    # evidence updates --------------------------------------------------------

    def save_evidence_update(
        self, record: DiagnosticEvidenceUpdate,
    ) -> None:
        self.evidence_updates.save(record)

    def get_evidence_update(
        self, update_id: str,
    ) -> DiagnosticEvidenceUpdate | None:
        return self.evidence_updates.get(update_id)

    def list_evidence_updates(
        self, session_id: str,
    ) -> tuple[DiagnosticEvidenceUpdate, ...]:
        return self._list_for_session(
            self.evidence_updates, session_id)

    # resolutions ----------------------------------------------------------------

    def save_resolution(
        self, record: DiagnosticResolutionRecord,
    ) -> None:
        self.resolutions.save(record)

    def get_resolution(
        self, resolution_id: str,
    ) -> DiagnosticResolutionRecord | None:
        return self.resolutions.get(resolution_id)

    def list_resolutions(
        self, session_id: str,
    ) -> tuple[DiagnosticResolutionRecord, ...]:
        return self._list_for_session(self.resolutions, session_id)

    # operator authorizations ------------------------------------------------------

    def save_authorization(
        self, record: DiagnosticOperatorAuthorization,
    ) -> None:
        self.authorizations.save(record)

    def get_authorization(
        self, authorization_id: str,
    ) -> DiagnosticOperatorAuthorization | None:
        return self.authorizations.get(authorization_id)

    def list_authorizations(
        self, session_id: str,
    ) -> tuple[DiagnosticOperatorAuthorization, ...]:
        return self._list_for_session(self.authorizations, session_id)


__all__ = [
    'CadDiagnosticOrchestratorRepository',
    'DeploymentConflictError',
    'DeploymentIntegrityError',
]
