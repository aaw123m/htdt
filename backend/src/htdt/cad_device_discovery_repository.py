"""Append-only persistence for the #879 device-discovery authority.

Six tables in one repository — discovery runs (``cad_discovery_runs``),
discovered devices (``cad_discovered_devices``), capability probe
records (``cad_capability_probe_records``), trusted device bindings
(``cad_trusted_device_bindings``), identity drift reports
(``cad_device_identity_drift_reports``) and rebinding decisions
(``cad_device_rebinding_decisions``). Shares the #806 ``_SealedStore``
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
from .cad_device_discovery import (
    CapabilityProbeRecord,
    DeviceIdentityDriftReport,
    DiscoveryRunRecord,
    DiscoveredDeviceRecord,
    RebindingDecision,
    TrustedDeviceBinding,
)


class CadDeviceDiscoveryRepository:
    """Native storage for the #879 device-discovery authority."""

    def __init__(self, scene_repository: SceneRepository) -> None:
        self.scene_repository = scene_repository
        self.path = scene_repository.path
        with closing(self._connect()) as connection, connection:
            require_native_tables(
                connection,
                'cad_discovery_runs',
                'cad_discovered_devices',
                'cad_capability_probe_records',
                'cad_trusted_device_bindings',
                'cad_device_identity_drift_reports',
                'cad_device_rebinding_decisions',
            )
        self.runs = _SealedStore(
            self._connect,
            'cad_discovery_runs',
            DiscoveryRunRecord, 'run_id',
            'run_sha256',
            (
                ('document_id', '__document_id__'),
                ('backend_id', 'backend_id'),
                ('mechanism', 'mechanism'),
                ('outcome', 'outcome'),
                ('device_count', 'device_count'),
            ),
        )
        self.devices = _SealedStore(
            self._connect,
            'cad_discovered_devices',
            DiscoveredDeviceRecord, 'device_id',
            'device_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('run_ref_id', 'run_ref'),
                ('endpoint', 'endpoint'),
                ('identity_state', 'identity_state'),
                ('ambiguity_group', 'ambiguity_group'),
            ),
        )
        self.probes = _SealedStore(
            self._connect,
            'cad_capability_probe_records',
            CapabilityProbeRecord, 'probe_id',
            'probe_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('device_ref_id', 'device_ref'),
                ('adapter_id', 'adapter_id'),
                ('outcome', 'outcome'),
                (
                    'capability_snapshot_sha256',
                    'capability_snapshot_sha256',
                ),
            ),
        )
        self.bindings = _SealedStore(
            self._connect,
            'cad_trusted_device_bindings',
            TrustedDeviceBinding, 'binding_record_id',
            'binding_record_sha256',
            (
                ('document_id', '__document_id__'),
                ('binding_id', 'binding_id'),
                _ref('device_ref_id', 'device_ref'),
                ('adapter_id', 'adapter_id'),
                ('endpoint', 'endpoint'),
                ('trust_state', 'trust_state'),
                ('identity_basis', 'identity_basis'),
            ),
        )
        self.drift_reports = _SealedStore(
            self._connect,
            'cad_device_identity_drift_reports',
            DeviceIdentityDriftReport, 'report_id',
            'report_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('binding_ref_id', 'binding_ref'),
                ('drift_kind', 'drift_kind'),
                ('verdict', 'verdict'),
            ),
        )
        self.rebinding_decisions = _SealedStore(
            self._connect,
            'cad_device_rebinding_decisions',
            RebindingDecision, 'decision_id',
            'decision_sha256',
            (
                ('document_id', '__document_id__'),
                _ref('previous_binding_ref_id', 'previous_binding_ref'),
                ('action', 'action'),
                _ref('new_binding_ref_id', 'new_binding_ref'),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    # discovery runs --------------------------------------------------

    def save_run(self, record: DiscoveryRunRecord) -> None:
        self.runs.save(record)

    def get_run(self, run_id: str) -> DiscoveryRunRecord | None:
        return self.runs.get(run_id)

    def list_runs(
        self, document_id: str | None = None,
    ) -> tuple[DiscoveryRunRecord, ...]:
        return self.runs.list(document_id)

    # discovered devices ------------------------------------------------

    def save_device(self, record: DiscoveredDeviceRecord) -> None:
        self.devices.save(record)

    def get_device(self, device_id: str) -> DiscoveredDeviceRecord | None:
        return self.devices.get(device_id)

    def list_devices(
        self, document_id: str | None = None,
    ) -> tuple[DiscoveredDeviceRecord, ...]:
        return self.devices.list(document_id)

    def list_devices_for_run(
        self, run_id: str,
    ) -> tuple[DiscoveredDeviceRecord, ...]:
        return tuple(
            record for record in self.devices.list(None)
            if record.run_ref is not None and record.run_ref.ref_id == run_id
        )

    # capability probes -------------------------------------------------

    def save_probe(self, record: CapabilityProbeRecord) -> None:
        self.probes.save(record)

    def get_probe(self, probe_id: str) -> CapabilityProbeRecord | None:
        return self.probes.get(probe_id)

    def list_probes(
        self, document_id: str | None = None,
    ) -> tuple[CapabilityProbeRecord, ...]:
        return self.probes.list(document_id)

    # trusted device bindings --------------------------------------------

    def save_binding(self, record: TrustedDeviceBinding) -> None:
        self.bindings.save(record)

    def get_binding_record(
        self, binding_record_id: str,
    ) -> TrustedDeviceBinding | None:
        return self.bindings.get(binding_record_id)

    def list_binding_records(
        self, document_id: str | None = None,
    ) -> tuple[TrustedDeviceBinding, ...]:
        return self.bindings.list(document_id)

    def list_binding_chain(
        self, binding_id: str,
    ) -> tuple[TrustedDeviceBinding, ...]:
        """All records of one binding chain, oldest → newest."""
        return tuple(
            record for record in self.bindings.list(None)
            if record.binding_id == binding_id
        )

    # identity drift reports ----------------------------------------------

    def save_drift_report(self, record: DeviceIdentityDriftReport) -> None:
        self.drift_reports.save(record)

    def get_drift_report(
        self, report_id: str,
    ) -> DeviceIdentityDriftReport | None:
        return self.drift_reports.get(report_id)

    def list_drift_reports(
        self, document_id: str | None = None,
    ) -> tuple[DeviceIdentityDriftReport, ...]:
        return self.drift_reports.list(document_id)

    # rebinding decisions ---------------------------------------------------

    def save_rebinding_decision(self, record: RebindingDecision) -> None:
        self.rebinding_decisions.save(record)

    def get_rebinding_decision(
        self, decision_id: str,
    ) -> RebindingDecision | None:
        return self.rebinding_decisions.get(decision_id)

    def list_rebinding_decisions(
        self, document_id: str | None = None,
    ) -> tuple[RebindingDecision, ...]:
        return self.rebinding_decisions.list(document_id)


__all__ = [
    'CadDeviceDiscoveryRepository',
    'DeploymentConflictError',
    'DeploymentIntegrityError',
]
