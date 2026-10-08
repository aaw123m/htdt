"""Append-only persistence for the #887 adapter-SDK authority.

Two tables share the #806 ``_SealedStore`` machinery — save-time seal
re-verification, read-time column-vs-payload checks, append-only
conflict on id+sha divergence:

- ``cad_adapter_sdk_descriptors`` — sealed
  :class:`~htdt.cad_adapter_sdk.AdapterSdkContract` records (``asd-``);
- ``cad_adapter_conformance_results`` — sealed
  :class:`~htdt.cad_adapter_conformance.ConformanceResultRecord`
  verdicts (``acr-``) pinned to descriptor sha + suite version.

``latest_result_for`` is the query downstream gates use before
:func:`~htdt.cad_adapter_sdk.assert_conforming`: the freshest sealed
result for one descriptor sha, so stale-descriptor evidence is never
silently reused.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

from .cad_adapter_conformance import ConformanceResultRecord
from .cad_adapter_sdk import AdapterSdkContract
from .cad_repository import SceneRepository
from .cad_schema import connect_sqlite, require_native_tables
from .cad_calibration_deployment_repository import (
    DeploymentConflictError,
    DeploymentIntegrityError,
    _SealedStore,
)


class CadAdapterSdkRepository:
    """Native storage for adapter SDK descriptors + conformance results."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_adapter_sdk_descriptors',
                'cad_adapter_conformance_results',
            )
        self.descriptors = _SealedStore(
            self._connect,
            'cad_adapter_sdk_descriptors',
            AdapterSdkContract, 'descriptor_id',
            'descriptor_sha256',
            (
                ('document_id', '__document_id__'),
                ('adapter_id', 'adapter_id'),
                ('adapter_version', 'adapter_version'),
                ('adapter_kind', 'adapter_kind'),
                ('device_family', 'device_family'),
                ('sdk_version', 'sdk_version'),
                ('contract_status', 'contract_status'),
                ('declared_at_utc', 'declared_at_utc'),
            ),
        )
        self.results = _SealedStore(
            self._connect,
            'cad_adapter_conformance_results',
            ConformanceResultRecord, 'result_id',
            'result_sha256',
            (
                ('document_id', '__document_id__'),
                ('descriptor_sha256', 'descriptor_ref.ref_sha256'),
                ('adapter_id', 'adapter_id'),
                ('adapter_version', 'adapter_version'),
                ('suite_version', 'suite_version'),
                ('contract_version', 'contract_version'),
                ('verdict', 'verdict'),
                ('issued_at_utc', 'issued_at_utc'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    # -- descriptors ----------------------------------------------------

    def save_descriptor(self, descriptor: AdapterSdkContract) -> None:
        self.descriptors.save(descriptor)

    def get_descriptor(
        self, descriptor_id: str,
    ) -> AdapterSdkContract | None:
        return self.descriptors.get(descriptor_id)

    def list_descriptors(
        self, document_id: str | None = None,
    ) -> tuple[AdapterSdkContract, ...]:
        return self.descriptors.list(document_id)

    # -- conformance results ---------------------------------------------

    def save_result(self, result: ConformanceResultRecord) -> None:
        self.results.save(result)

    def get_result(
        self, result_id: str,
    ) -> ConformanceResultRecord | None:
        return self.results.get(result_id)

    def list_results(
        self, document_id: str | None = None,
    ) -> tuple[ConformanceResultRecord, ...]:
        return self.results.list(document_id)

    def latest_result_for(
        self, descriptor_sha256: str,
    ) -> ConformanceResultRecord | None:
        """Freshest sealed result pinned to this descriptor sha.

        Results for other descriptor generations are never returned —
        a caller asking "is *this* adapter conforming?" can only ever
        get evidence minted against the exact descriptor it holds.
        """
        matches = [
            record for record in self.results.list(None)
            if record.descriptor_ref.ref_sha256 == descriptor_sha256
        ]
        if not matches:
            return None
        return max(matches, key=lambda record: record.issued_at_utc)


__all__ = [
    'CadAdapterSdkRepository',
    'DeploymentConflictError',
    'DeploymentIntegrityError',
]
