"""Append-only persistence for the #878 deployment pipeline authority.

Five tables in one repository — staged pipeline records
(``cad_deployment_pipeline_records``), scoped one-shot operator
authorizations (``cad_deployment_operator_authorizations``), assisted
instruction manifests (``cad_assisted_instruction_manifests``), assisted
attestations (``cad_assisted_deployment_attestations``) and APO
installed-identity records (``cad_apo_install_records``). Shares the
#806 ``_SealedStore`` machinery: save-time seal re-verification,
read-time column-vs-payload checks.
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
from .cad_deployment_pipeline import (
    ApoInstallRecord,
    AssistedDeploymentAttestation,
    AssistedInstructionManifest,
    DeploymentOperatorAuthorization,
    DeploymentPipelineRecord,
)


class CadDeploymentPipelineRepository:
    """Native storage for the #878 deployment pipeline authority."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_deployment_pipeline_records',
                'cad_deployment_operator_authorizations',
                'cad_assisted_instruction_manifests',
                'cad_assisted_deployment_attestations',
                'cad_apo_install_records',
            )
        self.pipeline_records = _SealedStore(
            self._connect,
            'cad_deployment_pipeline_records',
            DeploymentPipelineRecord, 'record_id',
            'record_sha256',
            (
                ('document_id', '__document_id__'),
                ('pipeline_id', 'pipeline_id'),
                ('binding_sha256', 'binding_sha256'),
                ('adapter_id', 'adapter_id'),
                ('target_ref', 'target_ref'),
                ('stage', 'stage'),
                ('evidence_strength', 'evidence_strength'),
                ('readback_verdict', 'readback_verdict'),
                ('partial_write', 'partial_write'),
            ),
        )
        self.operator_authorizations = _SealedStore(
            self._connect,
            'cad_deployment_operator_authorizations',
            DeploymentOperatorAuthorization, 'authorization_id',
            'authorization_sha256',
            (
                ('document_id', '__document_id__'),
                ('pipeline_id', 'pipeline_id'),
                ('scope', 'scope'),
                ('operator_id', 'operator_id'),
                ('consumed', 'consumed'),
            ),
        )
        self.assisted_manifests = _SealedStore(
            self._connect,
            'cad_assisted_instruction_manifests',
            AssistedInstructionManifest, 'manifest_id',
            'manifest_sha256',
            (
                ('document_id', '__document_id__'),
                ('target_ref', 'target_ref'),
            ),
        )
        self.assisted_attestations = _SealedStore(
            self._connect,
            'cad_assisted_deployment_attestations',
            AssistedDeploymentAttestation, 'attestation_id',
            'attestation_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('manifest_ref_id', 'manifest_ref'),
                ('pipeline_id', 'pipeline_id'),
                ('outcome', 'outcome'),
            ),
        )
        self.apo_installs = _SealedStore(
            self._connect,
            'cad_apo_install_records',
            ApoInstallRecord, 'record_id',
            'record_sha256',
            (
                ('document_id', '__document_id__'),
                ('target_path_repr', 'target_path_repr'),
                ('verification', 'verification'),
                ('evidence_strength', 'evidence_strength'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    # pipeline records ----------------------------------------------

    def save_pipeline_record(self, record: DeploymentPipelineRecord) -> None:
        self.pipeline_records.save(record)

    def get_pipeline_record(
        self, record_id: str,
    ) -> DeploymentPipelineRecord | None:
        return self.pipeline_records.get(record_id)

    def list_pipeline_records(
        self, document_id: str | None = None,
    ) -> tuple[DeploymentPipelineRecord, ...]:
        return self.pipeline_records.list(document_id)

    def list_pipeline_records_for_pipeline(
        self, pipeline_id: str,
    ) -> tuple[DeploymentPipelineRecord, ...]:
        return tuple(
            record for record in self.pipeline_records.list(None)
            if record.pipeline_id == pipeline_id
        )

    # operator authorizations ----------------------------------------

    def save_operator_authorization(
        self, record: DeploymentOperatorAuthorization,
    ) -> None:
        self.operator_authorizations.save(record)

    def get_operator_authorization(
        self, authorization_id: str,
    ) -> DeploymentOperatorAuthorization | None:
        return self.operator_authorizations.get(authorization_id)

    def list_operator_authorizations(
        self, document_id: str | None = None,
    ) -> tuple[DeploymentOperatorAuthorization, ...]:
        return self.operator_authorizations.list(document_id)

    # assisted manifests ----------------------------------------------

    def save_assisted_manifest(
        self, record: AssistedInstructionManifest,
    ) -> None:
        self.assisted_manifests.save(record)

    def get_assisted_manifest(
        self, manifest_id: str,
    ) -> AssistedInstructionManifest | None:
        return self.assisted_manifests.get(manifest_id)

    def list_assisted_manifests(
        self, document_id: str | None = None,
    ) -> tuple[AssistedInstructionManifest, ...]:
        return self.assisted_manifests.list(document_id)

    # assisted attestations -------------------------------------------

    def save_assisted_attestation(
        self, record: AssistedDeploymentAttestation,
    ) -> None:
        self.assisted_attestations.save(record)

    def get_assisted_attestation(
        self, attestation_id: str,
    ) -> AssistedDeploymentAttestation | None:
        return self.assisted_attestations.get(attestation_id)

    def list_assisted_attestations(
        self, document_id: str | None = None,
    ) -> tuple[AssistedDeploymentAttestation, ...]:
        return self.assisted_attestations.list(document_id)

    # apo install records ----------------------------------------------

    def save_apo_install(self, record: ApoInstallRecord) -> None:
        self.apo_installs.save(record)

    def get_apo_install(self, record_id: str) -> ApoInstallRecord | None:
        return self.apo_installs.get(record_id)

    def list_apo_installs(
        self, document_id: str | None = None,
    ) -> tuple[ApoInstallRecord, ...]:
        return self.apo_installs.list(document_id)


__all__ = [
    'CadDeploymentPipelineRepository',
    'DeploymentConflictError',
    'DeploymentIntegrityError',
]
