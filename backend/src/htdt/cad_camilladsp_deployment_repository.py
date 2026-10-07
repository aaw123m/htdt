"""Append-only persistence for the #838 CamillaDSP deploy evidence.

Three tables in one repository — deployment sessions
(``cad_camilladsp_deployment_sessions``), runtime telemetry observations
(``cad_camilladsp_runtime_observations``) and rollback evidence
(``cad_camilladsp_rollback_evidence``). Shares the #806 ``_SealedStore``
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
    _ref,
)
from .cad_camilladsp_deploy import (
    CamillaDSPDeploymentSession,
    CamillaDSPRollbackEvidence,
    CamillaDSPRuntimeObservation,
)


class CadCamillaDSPDeploymentRepository:
    """Native storage for the #838 CamillaDSP deploy authority."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_camilladsp_deployment_sessions',
                'cad_camilladsp_runtime_observations',
                'cad_camilladsp_rollback_evidence',
            )
        self.sessions = _SealedStore(
            self._connect,
            'cad_camilladsp_deployment_sessions',
            CamillaDSPDeploymentSession, 'session_id',
            'session_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('deployment_ref_id', 'deployment_ref'),
                ('binding_sha256', 'binding_sha256'),
                ('candidate_config_sha256', 'candidate_config_sha256'),
            ),
        )
        self.runtime_observations = _SealedStore(
            self._connect,
            'cad_camilladsp_runtime_observations',
            CamillaDSPRuntimeObservation, 'observation_id',
            'observation_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('deployment_ref_id', 'deployment_ref'),
                ('binding_sha256', 'binding_sha256'),
                ('processing_state', 'processing_state'),
            ),
        )
        self.rollback_evidence = _SealedStore(
            self._connect,
            'cad_camilladsp_rollback_evidence',
            CamillaDSPRollbackEvidence, 'evidence_id',
            'evidence_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('deployment_ref_id', 'deployment_ref'),
                ('outcome', 'outcome'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    # deployment sessions -------------------------------------------

    def save_deployment_session(
        self, record: CamillaDSPDeploymentSession,
    ) -> None:
        self.sessions.save(record)

    def get_deployment_session(
        self, session_id: str,
    ) -> CamillaDSPDeploymentSession | None:
        return self.sessions.get(session_id)

    def list_deployment_sessions(
        self, document_id: str | None = None,
    ) -> tuple[CamillaDSPDeploymentSession, ...]:
        return self.sessions.list(document_id)

    # runtime observations -------------------------------------------

    def save_runtime_observation(
        self, record: CamillaDSPRuntimeObservation,
    ) -> None:
        self.runtime_observations.save(record)

    def get_runtime_observation(
        self, observation_id: str,
    ) -> CamillaDSPRuntimeObservation | None:
        return self.runtime_observations.get(observation_id)

    def list_runtime_observations(
        self, document_id: str | None = None,
    ) -> tuple[CamillaDSPRuntimeObservation, ...]:
        return self.runtime_observations.list(document_id)

    # rollback evidence ----------------------------------------------

    def save_rollback_evidence(
        self, record: CamillaDSPRollbackEvidence,
    ) -> None:
        self.rollback_evidence.save(record)

    def get_rollback_evidence(
        self, evidence_id: str,
    ) -> CamillaDSPRollbackEvidence | None:
        return self.rollback_evidence.get(evidence_id)

    def list_rollback_evidence(
        self, document_id: str | None = None,
    ) -> tuple[CamillaDSPRollbackEvidence, ...]:
        return self.rollback_evidence.list(document_id)


__all__ = [
    'CadCamillaDSPDeploymentRepository',
    'DeploymentConflictError',
    'DeploymentIntegrityError',
]
