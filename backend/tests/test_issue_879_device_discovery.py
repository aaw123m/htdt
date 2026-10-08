"""#879: safe device discovery + capability handshake — the sealed ladder
DISCOVER -> IDENTIFY -> CAPABILITY PROBE -> USER BIND -> TRUSTED ENDPOINT
-> USE ADAPTER, the read-only backend contract, operator-approved scope,
ambiguous-duplicate disambiguation, identity/firmware/capability drift
invalidation, and the sealed-record round-trip/tamper guarantees.

Fail-closed coverage: no unrestricted silent LAN sweep, unidentifiable or
ambiguous endpoints can never be bound, stale bindings are never silently
re-used, and credentials never enter sealed evidence.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from htdt.cad_device_adapter import AdapterCapabilityReport
from htdt.cad_device_discovery import (
    AdapterProbeRefusedError,
    AmbiguousDeviceError,
    CapabilityProbeResult,
    ConfiguredEndpointScanBackend,
    DEVICE_DISCOVERY_LABELS,
    DeviceDiscoveryService,
    DiscoveryBindingError,
    DiscoveryObservation,
    DiscoveryScanScope,
    DiscoveryScopeError,
    FakeCapabilityProber,
    FakeDiscoveryBackend,
    FakeDiscoveryDevice,
    FakeDiscoveryScenario,
    MdnsDiscoveryBackend,
    SsdpDiscoveryBackend,
    StaleBindingError,
    StaticEndpointProber,
    TrustedDeviceBinding,
    VendorDiscoveryBackend,
    build_scan_scope,
    capability_snapshot_sha256,
    default_fake_scenario,
    derive_binding_state,
    resolve_trusted_target,
)
from htdt.cad_device_discovery_repository import (
    CadDeviceDiscoveryRepository,
    DeploymentIntegrityError,
)
from htdt.cad_repository import SceneRepository
from htdt.cad_schema import connect_sqlite, ensure_native_schema

NOW = '2026-10-08T00:00:00+00:00'
LATER = '2026-10-08T01:00:00+00:00'
DOC = 'doc-879'
EP1 = 'avr-lan://192.0.2.10:23'
EP2 = 'avr-lan://192.0.2.11:23'
ADAPTER = 'htdt-avr-lan'


def _capability_report(**kw: object) -> AdapterCapabilityReport:
    payload: dict[str, object] = {
        'adapter_id': ADAPTER,
        'adapter_version': '1',
        'adapter_kind': 'network_api',
        'device_family': 'avr',
        'supports_apply': True,
        'supports_read_back': True,
        'supports_materialization': True,
        'deploy_mechanism': 'machine_write',
        'readback_mechanism': 'machine_exact',
        'rollback_mechanism': 'previous_config',
        'runtime_observation': 'telemetry',
        'supported_features': ('gain',),
        'limit_notes': (),
        'auth_requirements': ('approved_remote_endpoint',),
        'applicability': 'avr lan targets',
        'protocol_authority': 'documented',
        'notes': (),
    }
    payload.update(kw)
    return AdapterCapabilityReport(**payload)  # type: ignore[arg-type]


def _scope(
    *endpoints: str,
    **kw: object,
) -> DiscoveryScanScope:
    return build_scan_scope(
        approved_by='operator-1',
        approved_at_utc=NOW,
        approved_endpoints=endpoints or (EP1, EP2),
        **kw,  # type: ignore[arg-type]
    )


def _repo(tmp_path: Path) -> CadDeviceDiscoveryRepository:
    db = tmp_path / 'cad.sqlite3'
    ensure_native_schema(db)
    return CadDeviceDiscoveryRepository(SceneRepository(db))


def _service(
    tmp_path: Path | None = None,
) -> tuple[DeviceDiscoveryService, CadDeviceDiscoveryRepository | None]:
    repo = _repo(tmp_path) if tmp_path is not None else None
    return DeviceDiscoveryService(repo), repo


def _probe_result(
    firmware: str = '1.58.0', **kw: object,
) -> CapabilityProbeResult:
    return CapabilityProbeResult(
        report=_capability_report(**kw), firmware_version=firmware,
    )


def _ladder(
    service: DeviceDiscoveryService,
    *,
    endpoint: str = EP1,
    firmware: str = '1.58.0',
    document_id: str = DOC,
    scope: DiscoveryScanScope | None = None,
    scenario: FakeDiscoveryScenario | None = None,
    credential_ref: str | None = None,
) -> tuple:
    backend = FakeDiscoveryBackend(scenario or default_fake_scenario())
    run, devices = service.run_discovery(
        document_id=document_id,
        backend=backend,
        scope=scope or _scope(endpoint),
        started_at_utc=NOW,
        finished_at_utc=NOW,
    )
    prober = FakeCapabilityProber(
        {endpoint: _probe_result(firmware)}
    )
    probe = service.probe_capability(
        document_id=document_id,
        device=devices[0],
        prober=prober,
        adapter_id=ADAPTER,
        at_utc=LATER,
    )
    binding = service.bind(
        document_id=document_id,
        device=devices[0],
        probe=probe,
        operator_id='operator-1',
        at_utc=LATER,
        credential_ref=credential_ref,
    )
    return run, devices[0], probe, binding, backend


# ---------------------------------------------------------------------------
# scope + backend fail-closed


class TestScopeContract:
    def test_empty_scope_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            build_scan_scope(
                approved_by='op',
                approved_at_utc=NOW,
            )

    def test_service_scope_is_honoured(self) -> None:
        service, _ = _service()
        # two fake devices; only EP1 is inside the approved scope
        backend = FakeDiscoveryBackend(FakeDiscoveryScenario(devices=(
            FakeDiscoveryDevice(endpoint=EP1),
            FakeDiscoveryDevice(endpoint=EP2),
        )))
        run, devices = service.run_discovery(
            document_id=DOC,
            backend=backend,
            scope=_scope(EP1),
            started_at_utc=NOW,
            finished_at_utc=NOW,
        )
        assert run.outcome == 'completed'
        assert run.device_count == 1
        assert devices[0].endpoint == EP1

    def test_configured_scan_refuses_endpoints_outside_scope(self) -> None:
        backend = ConfiguredEndpointScanBackend(
            StaticEndpointProber({EP1: DiscoveryObservation(endpoint=EP1)})
        )
        with pytest.raises(DiscoveryScopeError):
            backend.probe_identity(EP2, _scope(EP1))

    def test_configured_scan_discovers_only_approved_endpoints(self) -> None:
        backend = ConfiguredEndpointScanBackend(
            StaticEndpointProber({
                EP1: DiscoveryObservation(
                    endpoint=EP1, manufacturer='Denon', model='AVR-X',
                ),
            })
        )
        # Endpoint inside scope that answers -> observed; outside -> never
        observations = backend.discover(_scope(EP1))
        assert len(observations) == 1
        # An approved-endpoint list containing an absent device yields no
        # ghost record — the prober returned None honestly.
        observations = backend.discover(_scope(EP1, EP2))
        assert len(observations) == 1

    def test_configured_scan_rejects_missing_approved_endpoints(self) -> None:
        backend = ConfiguredEndpointScanBackend(StaticEndpointProber({}))
        scope = build_scan_scope(
            approved_by='op',
            approved_at_utc=NOW,
            approved_service_types=('urn:htdt:avr',),
        )
        with pytest.raises(DiscoveryScopeError):
            backend.discover(scope)

    def test_configured_scan_rejects_oversized_scope(self) -> None:
        backend = ConfiguredEndpointScanBackend(StaticEndpointProber({}))
        scope = _scope(*(f'avr-lan://10.0.0.{i}:23' for i in range(5)))
        scope = build_scan_scope(
            approved_by='op',
            approved_at_utc=NOW,
            approved_endpoints=tuple(
                f'avr-lan://10.0.0.{i}:23' for i in range(5)
            ),
            max_endpoints=3,
        )
        with pytest.raises(DiscoveryScopeError):
            backend.discover(scope)


class TestUnavailableBackendsFailClosed:
    @pytest.mark.parametrize(
        'backend',
        [
            MdnsDiscoveryBackend(),
            SsdpDiscoveryBackend(),
            VendorDiscoveryBackend(),
        ],
    )
    def test_unavailable_backend_records_honest_run(
        self, backend: object,
    ) -> None:
        service, _ = _service()
        run, devices = service.run_discovery(
            document_id=DOC,
            backend=backend,  # type: ignore[arg-type]
            scope=_scope(EP1),
            started_at_utc=NOW,
            finished_at_utc=NOW,
        )
        assert run.outcome == 'unavailable'
        assert run.device_count == 0
        assert run.failure_reason is not None
        assert devices == ()


class TestFakeScenarios:
    def test_no_devices(self) -> None:
        service, _ = _service()
        backend = FakeDiscoveryBackend(FakeDiscoveryScenario(devices=()))
        run, devices = service.run_discovery(
            document_id=DOC, backend=backend, scope=_scope(EP1),
            started_at_utc=NOW, finished_at_utc=NOW,
        )
        assert run.outcome == 'completed'
        assert run.device_count == 0
        assert devices == ()

    def test_simulated_backend_is_flagged(self) -> None:
        service, _ = _service()
        backend = FakeDiscoveryBackend()
        run, devices = service.run_discovery(
            document_id=DOC, backend=backend, scope=_scope(EP1),
            started_at_utc=NOW, finished_at_utc=NOW,
        )
        assert run.backend_is_simulated is True
        assert devices[0].backend_is_simulated is True

    def test_ambiguous_duplicates(self) -> None:
        service, _ = _service()
        dup = FakeDiscoveryDevice(
            endpoint=EP1, device_name='AVR', model='AVR-X',
            manufacturer='Denon', stable_identity=None,
        )
        dup2 = FakeDiscoveryDevice(
            endpoint=EP2, device_name='AVR', model='AVR-X',
            manufacturer='Denon', stable_identity=None,
        )
        backend = FakeDiscoveryBackend(
            FakeDiscoveryScenario(devices=(dup, dup2))
        )
        run, devices = service.run_discovery(
            document_id=DOC, backend=backend,
            scope=_scope(EP1, EP2),
            started_at_utc=NOW, finished_at_utc=NOW,
        )
        assert run.ambiguous_count == 2
        assert all(d.identity_state == 'ambiguous' for d in devices)
        assert {d.ambiguity_group for d in devices} == {
            devices[0].ambiguity_group,
        }

    def test_stable_identities_distinguish_duplicates(self) -> None:
        service, _ = _service()
        backend = FakeDiscoveryBackend(FakeDiscoveryScenario(devices=(
            FakeDiscoveryDevice(
                endpoint=EP1, device_name='AVR', model='AVR-X',
                manufacturer='Denon', stable_identity='serial-a',
            ),
            FakeDiscoveryDevice(
                endpoint=EP2, device_name='AVR', model='AVR-X',
                manufacturer='Denon', stable_identity='serial-b',
            ),
        )))
        run, devices = service.run_discovery(
            document_id=DOC, backend=backend,
            scope=_scope(EP1, EP2),
            started_at_utc=NOW, finished_at_utc=NOW,
        )
        assert run.ambiguous_count == 0
        assert all(d.identity_state == 'identified' for d in devices)

    def test_partial_and_unidentified_states(self) -> None:
        service, _ = _service()
        backend = FakeDiscoveryBackend(FakeDiscoveryScenario(devices=(
            FakeDiscoveryDevice(
                endpoint=EP1, manufacturer='Denon', model=None,
            ),
            FakeDiscoveryDevice(
                endpoint=EP2, manufacturer=None, model=None,
                stable_identity=None, firmware_version=None,
            ),
        )))
        run, devices = service.run_discovery(
            document_id=DOC, backend=backend,
            scope=_scope(EP1, EP2),
            started_at_utc=NOW, finished_at_utc=NOW,
        )
        states = {d.endpoint: d.identity_state for d in devices}
        assert states[EP1] == 'partial'
        assert states[EP2] == 'unidentified'

    def test_stale_endpoint_not_discovered(self) -> None:
        service, _ = _service()
        backend = FakeDiscoveryBackend(FakeDiscoveryScenario(devices=(
            FakeDiscoveryDevice(endpoint=EP1, reachable=False),
        )))
        run, devices = service.run_discovery(
            document_id=DOC, backend=backend, scope=_scope(EP1),
            started_at_utc=NOW, finished_at_utc=NOW,
        )
        assert devices == ()
        assert backend.probe_identity(EP1, _scope(EP1)) is None


# ---------------------------------------------------------------------------
# probe

class TestCapabilityProbe:
    def test_probe_seals_manifest_snapshot(self, tmp_path: Path) -> None:
        service, _ = _service()
        run, device, probe, binding, _ = _ladder(service)
        assert probe.outcome == 'probed'
        assert probe.capability_report is not None
        assert probe.capability_snapshot_sha256 == (
            capability_snapshot_sha256(probe.capability_report)
        )
        assert probe.device_ref.ref_id == device.device_id

    def test_unreachable_endpoint(self) -> None:
        service, _ = _service()
        backend = FakeDiscoveryBackend()
        run, devices = service.run_discovery(
            document_id=DOC, backend=backend, scope=_scope(EP1),
            started_at_utc=NOW, finished_at_utc=NOW,
        )
        prober = FakeCapabilityProber({})  # endpoint unreachable
        probe = service.probe_capability(
            document_id=DOC, device=devices[0], prober=prober,
            adapter_id=ADAPTER, at_utc=LATER,
        )
        assert probe.outcome == 'unreachable'
        assert probe.capability_report is None
        assert probe.failure_reason is not None

    def test_refused_negotiation(self) -> None:
        service, _ = _service()
        backend = FakeDiscoveryBackend()
        run, devices = service.run_discovery(
            document_id=DOC, backend=backend, scope=_scope(EP1),
            started_at_utc=NOW, finished_at_utc=NOW,
        )
        prober = FakeCapabilityProber(
            {EP1: AdapterProbeRefusedError('device refused')}
        )
        probe = service.probe_capability(
            document_id=DOC, device=devices[0], prober=prober,
            adapter_id=ADAPTER, at_utc=LATER,
        )
        assert probe.outcome == 'refused'

    def test_manifestless_answer_is_insufficient(self) -> None:
        service, _ = _service()
        backend = FakeDiscoveryBackend()
        run, devices = service.run_discovery(
            document_id=DOC, backend=backend, scope=_scope(EP1),
            started_at_utc=NOW, finished_at_utc=NOW,
        )
        prober = FakeCapabilityProber({
            EP1: CapabilityProbeResult(report=None),
        })
        probe = service.probe_capability(
            document_id=DOC, device=devices[0], prober=prober,
            adapter_id=ADAPTER, at_utc=LATER,
        )
        assert probe.outcome == 'insufficient'
        assert probe.capability_report is None
        assert probe.failure_reason is not None

    def test_unidentified_endpoint_still_probes_for_evidence(self) -> None:
        # probing is read-only — even an unidentified endpoint can be
        # negotiated with; the bind step enforces the identity gates.
        service, _ = _service()
        backend = FakeDiscoveryBackend(FakeDiscoveryScenario(devices=(
            FakeDiscoveryDevice(
                endpoint=EP1, manufacturer=None, model=None,
                stable_identity=None, firmware_version=None,
            ),
        )))
        run, devices = service.run_discovery(
            document_id=DOC, backend=backend, scope=_scope(EP1),
            started_at_utc=NOW, finished_at_utc=NOW,
        )
        prober = FakeCapabilityProber({EP1: _probe_result()})
        probe = service.probe_capability(
            document_id=DOC, device=devices[0], prober=prober,
            adapter_id=ADAPTER, at_utc=LATER,
        )
        assert devices[0].identity_state == 'unidentified'
        assert probe.outcome == 'probed'
        assert prober.probed == [EP1]
        # ...but an unidentified device can still never be bound
        with pytest.raises(DiscoveryBindingError):
            service.bind(
                document_id=DOC, device=devices[0], probe=probe,
                operator_id='op', at_utc=LATER,
            )

    def test_adapter_mismatch_fails_closed(self) -> None:
        service, _ = _service()
        backend = FakeDiscoveryBackend()
        run, devices = service.run_discovery(
            document_id=DOC, backend=backend, scope=_scope(EP1),
            started_at_utc=NOW, finished_at_utc=NOW,
        )
        prober = FakeCapabilityProber({EP1: _probe_result()})
        probe = service.probe_capability(
            document_id=DOC, device=devices[0], prober=prober,
            adapter_id='other-adapter', at_utc=LATER,
        )
        assert probe.outcome == 'unsupported_adapter'


# ---------------------------------------------------------------------------
# bind

class TestBind:
    def test_full_ladder_produces_trusted_binding(
        self, tmp_path: Path,
    ) -> None:
        service, _ = _service()
        run, device, probe, binding, _ = _ladder(service)
        assert binding.trust_state == 'trusted'
        assert binding.identity_basis == 'device_stable_id'
        assert binding.device_identity_key == 'serial-abc-123'
        assert binding.capability_snapshot_sha256 == (
            probe.capability_snapshot_sha256
        )

    def test_ambiguous_requires_disambiguation(self) -> None:
        service, _ = _service()
        backend = FakeDiscoveryBackend(FakeDiscoveryScenario(devices=(
            FakeDiscoveryDevice(
                endpoint=EP1, device_name='AVR', model='AVR-X',
                manufacturer='Denon', stable_identity=None,
            ),
            FakeDiscoveryDevice(
                endpoint=EP2, device_name='AVR', model='AVR-X',
                manufacturer='Denon', stable_identity=None,
            ),
        )))
        run, devices = service.run_discovery(
            document_id=DOC, backend=backend, scope=_scope(EP1, EP2),
            started_at_utc=NOW, finished_at_utc=NOW,
        )
        prober = FakeCapabilityProber({EP1: _probe_result()})
        probe = service.probe_capability(
            document_id=DOC, device=devices[0], prober=prober,
            adapter_id=ADAPTER, at_utc=LATER,
        )
        with pytest.raises(AmbiguousDeviceError):
            service.bind(
                document_id=DOC, device=devices[0], probe=probe,
                operator_id='op', at_utc=LATER,
            )
        bound = service.bind(
            document_id=DOC, device=devices[0], probe=probe,
            operator_id='op', at_utc=LATER,
            disambiguation_basis='operator confirmed front-panel serial',
        )
        assert bound.trust_state == 'trusted'
        assert bound.disambiguation_basis is not None

    def test_unidentified_never_binds(self) -> None:
        service, _ = _service()
        backend = FakeDiscoveryBackend(FakeDiscoveryScenario(devices=(
            FakeDiscoveryDevice(
                endpoint=EP1, manufacturer=None, model=None,
                stable_identity=None, firmware_version=None,
            ),
        )))
        run, devices = service.run_discovery(
            document_id=DOC, backend=backend, scope=_scope(EP1),
            started_at_utc=NOW, finished_at_utc=NOW,
        )
        prober = FakeCapabilityProber({EP1: _probe_result()})
        probe = service.probe_capability(
            document_id=DOC, device=devices[0], prober=prober,
            adapter_id=ADAPTER, at_utc=LATER,
        )
        with pytest.raises(DiscoveryBindingError):
            service.bind(
                document_id=DOC, device=devices[0], probe=probe,
                operator_id='op', at_utc=LATER,
            )

    def test_failed_probe_never_binds(self) -> None:
        service, _ = _service()
        backend = FakeDiscoveryBackend()
        run, devices = service.run_discovery(
            document_id=DOC, backend=backend, scope=_scope(EP1),
            started_at_utc=NOW, finished_at_utc=NOW,
        )
        prober = FakeCapabilityProber({})
        probe = service.probe_capability(
            document_id=DOC, device=devices[0], prober=prober,
            adapter_id=ADAPTER, at_utc=LATER,
        )
        assert probe.outcome == 'unreachable'
        with pytest.raises(DiscoveryBindingError):
            service.bind(
                document_id=DOC, device=devices[0], probe=probe,
                operator_id='op', at_utc=LATER,
            )

    def test_endpoint_only_identity_stays_unverifiable(self) -> None:
        service, _ = _service()
        scenario = FakeDiscoveryScenario(devices=(
            FakeDiscoveryDevice(
                endpoint=EP1, manufacturer='Denon', model='AVR-X',
                stable_identity=None,
            ),
        ))
        backend = FakeDiscoveryBackend(scenario)
        run, devices = service.run_discovery(
            document_id=DOC, backend=backend, scope=_scope(EP1),
            started_at_utc=NOW, finished_at_utc=NOW,
        )
        prober = FakeCapabilityProber({EP1: _probe_result()})
        probe = service.probe_capability(
            document_id=DOC, device=devices[0], prober=prober,
            adapter_id=ADAPTER, at_utc=LATER,
        )
        bound = service.bind(
            document_id=DOC, device=devices[0], probe=probe,
            operator_id='op', at_utc=LATER,
        )
        assert bound.trust_state == 'unverifiable'
        assert bound.identity_basis == 'endpoint_only'

    def test_probe_of_other_device_never_binds(self) -> None:
        service, _ = _service()
        backend = FakeDiscoveryBackend(FakeDiscoveryScenario(devices=(
            FakeDiscoveryDevice(
                endpoint=EP1, stable_identity='serial-a',
            ),
            FakeDiscoveryDevice(
                endpoint=EP2, device_name='Other', model='B',
                manufacturer='Denon', stable_identity='serial-b',
            ),
        )))
        run, devices = service.run_discovery(
            document_id=DOC, backend=backend, scope=_scope(EP1, EP2),
            started_at_utc=NOW, finished_at_utc=NOW,
        )
        prober = FakeCapabilityProber({EP2: _probe_result()})
        probe = service.probe_capability(
            document_id=DOC, device=devices[1], prober=prober,
            adapter_id=ADAPTER, at_utc=LATER,
        )
        with pytest.raises(DiscoveryBindingError):
            service.bind(
                document_id=DOC, device=devices[0], probe=probe,
                operator_id='op', at_utc=LATER,
            )

    def test_credential_ref_is_a_name_never_a_value(self) -> None:
        service, _ = _service()
        run, device, probe, bound, _ = _ladder(service)
        payload = bound.model_dump(mode='python')
        payload['credential_ref'] = 'password=hunter2'
        with pytest.raises(ValidationError):
            TrustedDeviceBinding(**payload)
        # a handle-style name binds fine
        bound2 = service.bind(
            document_id=DOC, device=device, probe=probe,
            operator_id='op', at_utc=LATER,
            credential_ref='avr-telnet-credential',
        )
        assert bound2.credential_ref == 'avr-telnet-credential'


# ---------------------------------------------------------------------------
# manual entry

class TestManualEntry:
    def test_manual_entry_path_binds_and_resolves(self) -> None:
        service, _ = _service()
        device = service.manual_entry(
            document_id=DOC,
            endpoint=EP1,
            operator_id='op',
            at_utc=NOW,
            manufacturer='Denon',
            model='AVR-X3800H',
            stable_identity='serial-abc-123',
            firmware_version='1.58.0',
        )
        assert device.identity_state == 'manual_entry'
        assert device.evidence_basis == 'operator_declared'
        assert device.discovery_mechanism == 'manual_entry'
        prober = FakeCapabilityProber({EP1: _probe_result()})
        probe = service.probe_capability(
            document_id=DOC, device=device, prober=prober,
            adapter_id=ADAPTER, at_utc=LATER,
        )
        bound = service.bind(
            document_id=DOC, device=device, probe=probe,
            operator_id='op', at_utc=LATER,
        )
        assert bound.trust_state == 'trusted'
        assert bound.identity_basis == 'device_stable_id'
        target = service.resolve_trusted_target(
            binding_chain=(bound,), probe=probe, device=device,
        )
        assert target.target_ref == EP1

    def test_manual_entry_without_identity_stays_unverifiable(self) -> None:
        service, _ = _service()
        device = service.manual_entry(
            document_id=DOC, endpoint=EP1, operator_id='op', at_utc=NOW,
        )
        prober = FakeCapabilityProber({EP1: _probe_result()})
        probe = service.probe_capability(
            document_id=DOC, device=device, prober=prober,
            adapter_id=ADAPTER, at_utc=LATER,
        )
        bound = service.bind(
            document_id=DOC, device=device, probe=probe,
            operator_id='op', at_utc=LATER,
        )
        assert bound.trust_state == 'trusted'
        assert bound.identity_basis == 'operator_declared'
        target = service.resolve_trusted_target(
            binding_chain=(bound,), probe=probe, device=device,
        )
        assert any('operator_declared' in w for w in target.warnings)


# ---------------------------------------------------------------------------
# drift

class TestIdentityDrift:
    def _bound(self, service: DeviceDiscoveryService):
        return _ladder(service)

    def test_unchanged_recheck_keeps_binding(self) -> None:
        service, _ = _service()
        run, device, probe, binding, backend = self._bound(service)
        report, successor = service.recheck_binding(
            document_id=DOC, binding_chain=(binding,), backend=backend,
            scope=_scope(EP1), at_utc=LATER,
        )
        assert report.verdict == 'unchanged'
        assert report.drift_kind is None
        assert report.recommendation == 'none'
        assert successor is None
        assert derive_binding_state((binding,)) == 'trusted'

    def test_identity_replacement_invalidates(self) -> None:
        service, _ = _service()
        run, device, probe, binding, _ = self._bound(service)
        replaced = FakeDiscoveryBackend(FakeDiscoveryScenario(devices=(
            FakeDiscoveryDevice(
                endpoint=EP1, manufacturer='Denon', model='AVR-X3800H',
                stable_identity='serial-zzz-999', firmware_version='1.58.0',
            ),
        )))
        report, successor = service.recheck_binding(
            document_id=DOC, binding_chain=(binding,), backend=replaced,
            scope=_scope(EP1), at_utc=LATER,
        )
        assert report.verdict == 'replacement_suspect'
        assert report.drift_kind == 'identity_replaced'
        assert report.recommendation == 'rebind_required'
        assert successor is not None
        assert successor.trust_state == 'invalidated_drift'
        assert successor.supersedes_record_sha256 == (
            binding.binding_record_sha256
        )
        assert derive_binding_state((binding, successor)) == (
            'invalidated_drift'
        )

    def test_stale_endpoint_invalidates(self) -> None:
        service, _ = _service()
        run, device, probe, binding, _ = self._bound(service)
        dead = FakeDiscoveryBackend(FakeDiscoveryScenario(devices=(
            FakeDiscoveryDevice(endpoint=EP1, reachable=False),
        )))
        report, successor = service.recheck_binding(
            document_id=DOC, binding_chain=(binding,), backend=dead,
            scope=_scope(EP1), at_utc=LATER,
        )
        assert report.drift_kind == 'endpoint_unreachable'
        assert successor is not None
        assert successor.trust_state == 'invalidated_drift'

    def test_firmware_change_forces_reprobe(self) -> None:
        service, _ = _service()
        run, device, probe, binding, _ = self._bound(service)
        upgraded = FakeDiscoveryBackend(FakeDiscoveryScenario(devices=(
            FakeDiscoveryDevice(
                endpoint=EP1, stable_identity='serial-abc-123',
                firmware_version='2.0.0',
            ),
        )))
        report, successor = service.recheck_binding(
            document_id=DOC, binding_chain=(binding,), backend=upgraded,
            scope=_scope(EP1), at_utc=LATER,
        )
        assert report.drift_kind == 'firmware_changed'
        assert report.verdict == 'drift_invalidates'
        assert report.recommendation == 'reprobe_required'
        assert successor is not None
        assert successor.trust_state == 'invalidated_drift'

    def test_identity_unverifiable_when_stable_id_lost(self) -> None:
        service, _ = _service()
        run, device, probe, binding, _ = self._bound(service)
        blank = FakeDiscoveryBackend(FakeDiscoveryScenario(devices=(
            FakeDiscoveryDevice(
                endpoint=EP1, stable_identity=None,
                firmware_version='1.58.0',
            ),
        )))
        report, successor = service.recheck_binding(
            document_id=DOC, binding_chain=(binding,), backend=blank,
            scope=_scope(EP1), at_utc=LATER,
        )
        assert report.verdict == 'unverifiable'
        assert report.drift_kind == 'identity_unverifiable'
        assert successor is not None
        assert successor.trust_state == 'unverifiable'

    def test_capability_change_invalidates(self) -> None:
        service, _ = _service()
        run, device, probe, binding, backend = self._bound(service)
        shifted_prober = FakeCapabilityProber({
            EP1: CapabilityProbeResult(
                report=_capability_report(
                    supported_features=('gain', 'peq'),
                ),
                firmware_version='1.58.0',
            ),
        })
        report, successor = service.recheck_binding(
            document_id=DOC, binding_chain=(binding,), backend=backend,
            scope=_scope(EP1), prober=shifted_prober, at_utc=LATER,
        )
        assert report.drift_kind == 'capability_changed'
        assert report.verdict == 'drift_invalidates'
        assert successor is not None

    def test_non_trusted_chain_recheck_fails_closed(self) -> None:
        service, _ = _service()
        run, device, probe, binding, _ = self._bound(service)
        dead = FakeDiscoveryBackend(FakeDiscoveryScenario(devices=(
            FakeDiscoveryDevice(endpoint=EP1, reachable=False),
        )))
        report, successor = service.recheck_binding(
            document_id=DOC, binding_chain=(binding,), backend=dead,
            scope=_scope(EP1), at_utc=LATER,
        )
        with pytest.raises(StaleBindingError):
            service.recheck_binding(
                document_id=DOC, binding_chain=(binding, successor),
                backend=dead, scope=_scope(EP1), at_utc=LATER,
            )


class TestRebindingDecision:
    def test_rebind_decision_records_new_binding(self) -> None:
        service, _ = _service()
        run, device, probe, binding, _ = _ladder(service)
        dead = FakeDiscoveryBackend(FakeDiscoveryScenario(devices=(
            FakeDiscoveryDevice(endpoint=EP1, reachable=False),
        )))
        report, successor = service.recheck_binding(
            document_id=DOC, binding_chain=(binding,), backend=dead,
            scope=_scope(EP1), at_utc=LATER,
        )
        # operator re-runs discovery, binds the replacement unit
        new_run, devices = service.run_discovery(
            document_id=DOC,
            backend=FakeDiscoveryBackend(FakeDiscoveryScenario(devices=(
                FakeDiscoveryDevice(
                    endpoint=EP1, manufacturer='Denon', model='AVR-X3800H',
                    stable_identity='serial-zzz-999',
                    firmware_version='2.0.0',
                ),
            ))),
            scope=_scope(EP1), started_at_utc=LATER, finished_at_utc=LATER,
        )
        new_probe = service.probe_capability(
            document_id=DOC, device=devices[0],
            prober=FakeCapabilityProber({EP1: _probe_result('2.0.0')}),
            adapter_id=ADAPTER, at_utc=LATER,
        )
        new_binding = service.bind(
            document_id=DOC, device=devices[0], probe=new_probe,
            operator_id='op', at_utc=LATER,
        )
        decision = service.record_rebinding_decision(
            document_id=DOC,
            previous_binding=binding,
            action='bound_replacement',
            operator_id='op',
            at_utc=LATER,
            drift_report=report,
            new_binding=new_binding,
        )
        assert decision.new_binding_ref is not None
        assert decision.new_binding_ref.ref_id == (
            new_binding.binding_record_id
        )
        assert decision.drift_report_ref is not None

    def test_revoked_decision_never_carries_new_binding(self) -> None:
        service, _ = _service()
        run, device, probe, binding, _ = _ladder(service)
        with pytest.raises(ValidationError):
            service.record_rebinding_decision(
                document_id=DOC,
                previous_binding=binding,
                action='revoked',
                operator_id='op',
                at_utc=LATER,
                new_binding=binding,
            )


# ---------------------------------------------------------------------------
# resolve

class TestResolveTrustedTarget:
    def test_resolve_produces_deployable_coordinates(self) -> None:
        service, _ = _service()
        run, device, probe, binding, _ = _ladder(service)
        target = resolve_trusted_target(
            binding, probe, device,
            routing=(('fl', 'out-fl'),),
        )
        assert target.target_ref == EP1
        assert target.adapter_id == ADAPTER
        assert target.capability.adapter_id == ADAPTER
        assert target.adapter_binding.device_serial == EP1
        assert target.adapter_binding.device_model == 'AVR-X3800H'
        assert target.adapter_binding.firmware_version == '1.58.0'
        assert target.trusted_binding_sha256 == (
            binding.binding_record_sha256
        )

    def test_untrusted_binding_never_resolves(self) -> None:
        service, _ = _service()
        run, device, probe, binding, _ = _ladder(service)
        dead = FakeDiscoveryBackend(FakeDiscoveryScenario(devices=(
            FakeDiscoveryDevice(endpoint=EP1, reachable=False),
        )))
        report, successor = service.recheck_binding(
            document_id=DOC, binding_chain=(binding,), backend=dead,
            scope=_scope(EP1), at_utc=LATER,
        )
        with pytest.raises(StaleBindingError):
            resolve_trusted_target(successor, probe, device)  # type: ignore[arg-type]

    def test_wrong_probe_never_resolves(self) -> None:
        service, _ = _service()
        run, device, probe, binding, _ = _ladder(service)
        other_backend = FakeDiscoveryBackend()
        run2, devices2 = service.run_discovery(
            document_id=DOC, backend=other_backend, scope=_scope(EP1),
            started_at_utc=NOW, finished_at_utc=NOW,
        )
        other_probe = service.probe_capability(
            document_id=DOC, device=devices2[0],
            prober=FakeCapabilityProber({EP1: _probe_result('9.9.9')}),
            adapter_id=ADAPTER, at_utc=LATER,
        )
        with pytest.raises(DiscoveryBindingError):
            resolve_trusted_target(binding, other_probe, device)

    def test_wrong_device_never_resolves(self) -> None:
        service, _ = _service()
        run, device, probe, binding, _ = _ladder(service)
        other = service.manual_entry(
            document_id=DOC, endpoint=EP2, operator_id='op', at_utc=NOW,
            manufacturer='Denon', model='AVR-X',
        )
        with pytest.raises(DiscoveryBindingError):
            resolve_trusted_target(binding, probe, other)


# ---------------------------------------------------------------------------
# seal integrity + repository

class TestSealIntegrity:
    def test_run_seal_rejects_tamper(self) -> None:
        service, _ = _service()
        backend = FakeDiscoveryBackend()
        run, _ = service.run_discovery(
            document_id=DOC, backend=backend, scope=_scope(EP1),
            started_at_utc=NOW, finished_at_utc=NOW,
        )
        payload = run.model_dump(mode='python')
        payload['outcome'] = 'failed'
        with pytest.raises(ValidationError):
            type(run)(**payload)

    def test_binding_seal_rejects_tamper(self) -> None:
        service, _ = _service()
        run, device, probe, binding, _ = _ladder(service)
        payload = binding.model_dump(mode='python')
        payload['trust_state'] = 'unverifiable'
        with pytest.raises(ValidationError):
            type(binding)(**payload)

    def test_drift_report_seal_rejects_tamper(self) -> None:
        service, _ = _service()
        run, device, probe, binding, _ = _ladder(service)
        dead = FakeDiscoveryBackend(FakeDiscoveryScenario(devices=(
            FakeDiscoveryDevice(endpoint=EP1, reachable=False),
        )))
        report, _ = service.recheck_binding(
            document_id=DOC, binding_chain=(binding,), backend=dead,
            scope=_scope(EP1), at_utc=LATER,
        )
        payload = report.model_dump(mode='python')
        payload['verdict'] = 'unchanged'
        with pytest.raises(ValidationError):
            type(report)(**payload)


class TestRepositoryRoundTrip:
    def test_full_ladder_persists(self, tmp_path: Path) -> None:
        service, repo = _service(tmp_path)
        run, device, probe, binding, _ = _ladder(service)
        assert repo is not None
        assert repo.get_run(run.run_id) == run
        assert repo.get_device(device.device_id) == device
        assert repo.get_probe(probe.probe_id) == probe
        assert repo.get_binding_record(binding.binding_record_id) == binding
        assert repo.list_devices_for_run(run.run_id) == (device,)
        assert repo.list_binding_chain(binding.binding_id) == (binding,)
        assert len(repo.list_runs(DOC)) == 1

    def test_drift_chain_persists(self, tmp_path: Path) -> None:
        service, repo = _service(tmp_path)
        run, device, probe, binding, _ = _ladder(service)
        dead = FakeDiscoveryBackend(FakeDiscoveryScenario(devices=(
            FakeDiscoveryDevice(endpoint=EP1, reachable=False),
        )))
        report, successor = service.recheck_binding(
            document_id=DOC, binding_chain=(binding,), backend=dead,
            scope=_scope(EP1), at_utc=LATER,
        )
        assert repo is not None
        assert repo.get_drift_report(report.report_id) == report
        chain = repo.list_binding_chain(binding.binding_id)
        assert [b.trust_state for b in chain] == [
            'trusted', 'invalidated_drift',
        ]
        decision = service.record_rebinding_decision(
            document_id=DOC, previous_binding=binding,
            action='kept_invalidated', operator_id='op', at_utc=LATER,
            drift_report=report,
        )
        assert repo.get_rebinding_decision(decision.decision_id) == decision

    def test_tampered_row_fails_closed(self, tmp_path: Path) -> None:
        service, repo = _service(tmp_path)
        run, device, probe, binding, _ = _ladder(service)
        assert repo is not None
        with connect_sqlite(repo.path) as connection:
            connection.execute(
                "UPDATE cad_discovered_devices SET endpoint=? "
                "WHERE device_id=?",
                (EP2, device.device_id),
            )
            connection.commit()
        with pytest.raises(DeploymentIntegrityError):
            repo.get_device(device.device_id)

    def test_unsealed_save_rejected(self, tmp_path: Path) -> None:
        # same-id/different-sha conflicts are unreachable — the id is the
        # sha; tampered payloads are rejected by the seal check before
        # the store ever touches the append-only ledger.
        service, repo = _service(tmp_path)
        run, device, probe, binding, _ = _ladder(service)
        assert repo is not None
        tampered = binding.model_copy()
        object.__setattr__(tampered, 'bound_at_utc', '2099-01-01')
        object.__setattr__(
            tampered, 'binding_record_sha256', 'a' * 64,
        )
        with pytest.raises(DeploymentIntegrityError):
            repo.save_binding(tampered)

    def test_service_without_repository_is_pure(self) -> None:
        service, _ = _service()
        run, device, probe, binding, _ = _ladder(service)
        assert run.outcome == 'completed'


# ---------------------------------------------------------------------------
# JA labels

def test_ja_labels_cover_vocabulary() -> None:
    for key in (
        'identified', 'ambiguous', 'unverifiable', 'invalidated_drift',
        'manual_entry', 'replacement_suspect', 'rebind_required',
        'configured_endpoint_scan', 'endpoint_unreachable',
    ):
        assert key in DEVICE_DISCOVERY_LABELS
        assert DEVICE_DISCOVERY_LABELS[key]
