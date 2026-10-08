"""#879 safe device discovery + capability handshake authority.

The commissioning targets consumed by #868 (orchestrator), #806
(deployment) and #878 (``DeploymentPipelineService``) used to require the
operator to type endpoints, identify devices, pick adapter profiles and
confirm firmware/capability compatibility by hand. This module automates
the *non-destructive* front of that setup as one sealed ladder::

    DISCOVER -> IDENTIFY -> CAPABILITY PROBE -> USER BIND
      -> SAVE TRUSTED ENDPOINT -> USE ADAPTER

Fail-closed rules enforced here:

* discovery is **read-only** — a :class:`DiscoveryBackend` observes and
  reports; nothing in this module mutates device state, and no path
  auto-deploys to a discovered endpoint;
* every run requires an operator-approved :class:`DiscoveryScanScope` —
  no unrestricted silent LAN sweep; backends that cannot honour the
  declared scope fail closed with ``outcome='scope_rejected'``;
* ``discovered`` is not ``identified`` is not ``probed`` is not
  ``bound`` is not ``trusted`` — each rung is a distinct sealed record;
  a binding built on insufficient identity stays ``unverifiable`` and
  can never resolve to a deployable target;
* ambiguous duplicates (same advertised identity on several endpoints
  with no stable discriminator) require an explicit operator
  disambiguation basis — the service never picks one;
* identity drift (device replacement on the saved endpoint, firmware or
  capability change) produces a sealed :class:`DeviceIdentityDriftReport`
  and demotes the binding chain to ``invalidated_drift`` — a stale
  binding is never silently re-used;
* credentials never enter discovery evidence — a binding carries only a
  secret *name* (``credential_ref``), matching the #726 convention;
* network presence is never treated as proof a device is safe or
  authorized to modify — ``trusted`` still requires the operator's
  explicit bind decision.

Records:

* :class:`DiscoveryRunRecord` — one discovery pass over one scope.
* :class:`DiscoveredDeviceRecord` — one endpoint observation (identity +
  advertised capabilities + ambiguity state).
* :class:`CapabilityProbeRecord` — capability negotiation for one
  device/adapter pair; the snapshot sha a binding pins.
* :class:`TrustedDeviceBinding` — the explicit operator bind decision;
  a chain of records carries state transitions (``trusted`` →
  ``invalidated_drift``/``revoked``/``superseded``).
* :class:`DeviceIdentityDriftReport` — the sealed drift verdict.
* :class:`RebindingDecision` — the operator's response to drift.
"""

from __future__ import annotations

from typing import Any, Literal, Mapping, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cad_authority_resolver import AuthorityRef
from .cad_device_adapter import (
    AdapterCapabilityReport,
    AdapterDeviceBinding,
    build_device_binding,
)
from .canonical_json import (
    canonical_sha256 as _hash,
    canonicalize_payload,
)


_SHA256 = r'^[0-9a-f]{64}$'

#: Credential-name guard shared with #726: a handle names a secret, it is
#: never the secret. Values bearing these markers are rejected.
_CREDENTIAL_VALUE_MARKERS = ('pass', 'token', 'secret_value', 'key=')


def _semantic_id(prefix: str, digest: str) -> str:
    return f'{prefix}-{digest[:24]}'


def _seal_id(
    model_cls: type[BaseModel],
    prefix: str,
    payload: dict[str, Any],
) -> tuple[str, str]:
    probe = model_cls.model_construct(
        **canonicalize_payload(model_cls, dict(payload))
    )
    digest = _hash(probe.identity_payload())
    return _semantic_id(prefix, digest), digest


def _ref(kind: str, record_id: str, sha: str) -> AuthorityRef:
    return AuthorityRef(kind=kind, ref_id=record_id, ref_sha256=sha)


# ---------------------------------------------------------------------------
# Errors


class DiscoveryError(Exception):
    """Base for discovery-authority failures (fail-closed surfaces)."""


class DiscoveryScopeError(DiscoveryError):
    """The requested scan exceeded the operator-approved scope."""


class DiscoveryBackendUnavailableError(DiscoveryError):
    """The discovery backend cannot run on this build/environment."""


class AmbiguousDeviceError(DiscoveryError):
    """A bind was requested on a device that is not uniquely identified."""


class DiscoveryBindingError(DiscoveryError):
    """A bind/resolution request failed the evidence gates."""


class StaleBindingError(DiscoveryError):
    """A binding chain was invalidated by drift or revocation."""


class AdapterProbeRefusedError(DiscoveryError):
    """The endpoint answered but refused capability negotiation."""


# ---------------------------------------------------------------------------
# Vocabulary

DiscoveryMechanism = Literal[
    'mdns',
    'ssdp',
    'vendor_documented',
    'configured_endpoint_scan',
    'manual_entry',
]

DiscoveryRunOutcome = Literal[
    'completed',
    'unavailable',
    'scope_rejected',
    'failed',
]

#: Identification state of one discovered endpoint. ``identified`` means
#: manufacturer + model (+ optionally a stable identity) were observed;
#: ``partial`` when only some identity fields were observable;
#: ``unidentified`` when the endpoint answered nothing identifying;
#: ``ambiguous`` when identical advertised identity covers several
#: endpoints with no stable discriminator; ``manual_entry`` marks the
#: operator-declared fallback path (``evidence_basis='operator_declared'``
#: records how identity was established).
DeviceIdentityState = Literal[
    'identified', 'partial', 'unidentified', 'ambiguous', 'manual_entry',
]

DeviceEvidenceBasis = Literal['advertised', 'operator_declared']

CapabilityProbeOutcome = Literal[
    'probed', 'unreachable', 'refused', 'insufficient', 'unsupported_adapter',
]

TrustedBindingState = Literal[
    'trusted', 'unverifiable', 'invalidated_drift', 'revoked', 'superseded',
]

IdentityBasis = Literal['device_stable_id', 'endpoint_only', 'operator_declared']

DriftKind = Literal[
    'identity_replaced',
    'firmware_changed',
    'capability_changed',
    'endpoint_unreachable',
    'identity_unverifiable',
]

DriftVerdict = Literal[
    'replacement_suspect', 'drift_invalidates', 'unchanged', 'unverifiable',
]

DriftRecommendation = Literal['rebind_required', 'reprobe_required', 'none']

RebindingAction = Literal[
    'rebound_same_device', 'bound_replacement', 'kept_invalidated', 'revoked',
]


# ---------------------------------------------------------------------------
# Scan scope — operator-approved discovery boundary (input, not sealed).

class DiscoveryScanScope(BaseModel):
    """The operator-approved boundary a discovery run may touch.

    At least one of ``approved_endpoints`` / ``approved_service_types`` /
    ``approved_networks`` must be non-empty — an empty scope is an
    unrestricted sweep request and fails closed.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    scope_id: str = Field(min_length=1)
    approved_endpoints: tuple[str, ...] = ()
    approved_service_types: tuple[str, ...] = ()
    approved_networks: tuple[str, ...] = ()
    approved_by: str = Field(min_length=1)
    approved_at_utc: str = Field(min_length=1)
    #: Hard bound on endpoints one run may contact.
    max_endpoints: int = Field(default=32, ge=1, le=1024)

    @model_validator(mode='after')
    def valid_scope(self) -> 'DiscoveryScanScope':
        if not (
            self.approved_endpoints
            or self.approved_service_types
            or self.approved_networks
        ):
            raise ValueError(
                'discovery scope is empty — an unrestricted silent LAN '
                'sweep is never the default'
            )
        return self

    def scope_sha256(self) -> str:
        return _hash(self.model_dump(mode='json'))

    def allows_endpoint(self, endpoint: str) -> bool:
        """Endpoint-level admission for scoped scans."""
        if endpoint in self.approved_endpoints:
            return True
        host = endpoint.rsplit(':', 1)[0].strip('[]').lower()
        approved_hosts = {
            e.rsplit(':', 1)[0].strip('[]').lower()
            for e in self.approved_endpoints
        }
        return host in approved_hosts


def build_scan_scope(
    *,
    approved_by: str,
    approved_at_utc: str,
    approved_endpoints: tuple[str, ...] = (),
    approved_service_types: tuple[str, ...] = (),
    approved_networks: tuple[str, ...] = (),
    max_endpoints: int = 32,
    scope_id: str | None = None,
) -> DiscoveryScanScope:
    return DiscoveryScanScope(
        scope_id=scope_id or f'scope-{uuid4().hex[:16]}',
        approved_endpoints=tuple(approved_endpoints),
        approved_service_types=tuple(approved_service_types),
        approved_networks=tuple(approved_networks),
        approved_by=approved_by,
        approved_at_utc=approved_at_utc,
        max_endpoints=max_endpoints,
    )


# ---------------------------------------------------------------------------
# Backend-facing observation (raw, unsealed — the service seals records).

class DiscoveryObservation(BaseModel):
    """One endpoint observation reported by a discovery backend.

    Any field the backend could not observe stays ``None`` — never
    inferred. ``stable_identity`` is the device-stable discriminator
    (serial/MAC-stable id) when the mechanism exposes one.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    endpoint: str = Field(min_length=1)
    manufacturer: str | None = None
    model: str | None = None
    device_name: str | None = None
    stable_identity: str | None = None
    firmware_version: str | None = None
    software_version: str | None = None
    advertised_capabilities: tuple[str, ...] = ()
    #: Adapter profile the advertisement nominates, when known.
    suggested_adapter_id: str | None = None


class EndpointProber(Protocol):
    """Read-only per-endpoint probe used by scoped scans."""

    def probe(self, endpoint: str) -> DiscoveryObservation | None: ...


class DiscoveryBackend:
    """The discovery path contract — observe only, never mutate.

    Rules: enumerate honestly (empty is a legal answer), observe only the
    operator-approved scope, never infer identity fields it did not see.
    """

    backend_id = 'abstract'
    backend_version = 'abstract-0'
    mechanism: DiscoveryMechanism = 'vendor_documented'
    backend_is_simulated = False

    def available(self) -> bool:
        raise NotImplementedError

    def unavailable_reason(self) -> str:
        return 'discovery backend is not implemented on this build'

    def discover(
        self, scope: DiscoveryScanScope,
    ) -> tuple[DiscoveryObservation, ...]:
        raise NotImplementedError

    def probe_identity(
        self, endpoint: str, scope: DiscoveryScanScope,
    ) -> DiscoveryObservation | None:
        """Re-observe one endpoint — the drift/verification path."""
        raise NotImplementedError


class MdnsDiscoveryBackend(DiscoveryBackend):
    """mDNS/Bonjour service discovery — stubbed, fails closed.

    Real multicast enumeration is not testable on this build; the backend
    reports itself unavailable so a run records ``unavailable`` honestly
    instead of pretending to have swept.
    """

    backend_id = 'htdt-mdns'
    backend_version = 'stub-0'
    mechanism: DiscoveryMechanism = 'mdns'

    def available(self) -> bool:
        return False

    def unavailable_reason(self) -> str:
        return 'mdns/bonjour discovery is not implemented on this build'

    def discover(
        self, scope: DiscoveryScanScope,
    ) -> tuple[DiscoveryObservation, ...]:
        raise DiscoveryBackendUnavailableError(self.unavailable_reason())

    def probe_identity(
        self, endpoint: str, scope: DiscoveryScanScope,
    ) -> DiscoveryObservation | None:
        raise DiscoveryBackendUnavailableError(self.unavailable_reason())


class SsdpDiscoveryBackend(DiscoveryBackend):
    """SSDP/UPnP discovery — stubbed, fails closed like mDNS."""

    backend_id = 'htdt-ssdp'
    backend_version = 'stub-0'
    mechanism: DiscoveryMechanism = 'ssdp'

    def available(self) -> bool:
        return False

    def unavailable_reason(self) -> str:
        return 'ssdp/upnp discovery is not implemented on this build'

    def discover(
        self, scope: DiscoveryScanScope,
    ) -> tuple[DiscoveryObservation, ...]:
        raise DiscoveryBackendUnavailableError(self.unavailable_reason())

    def probe_identity(
        self, endpoint: str, scope: DiscoveryScanScope,
    ) -> DiscoveryObservation | None:
        raise DiscoveryBackendUnavailableError(self.unavailable_reason())


class VendorDiscoveryBackend(DiscoveryBackend):
    """Vendor-documented discovery channel — stubbed, fails closed."""

    backend_id = 'htdt-vendor-discovery'
    backend_version = 'stub-0'
    mechanism: DiscoveryMechanism = 'vendor_documented'

    def available(self) -> bool:
        return False

    def unavailable_reason(self) -> str:
        return 'vendor-documented discovery is not wired on this build'

    def discover(
        self, scope: DiscoveryScanScope,
    ) -> tuple[DiscoveryObservation, ...]:
        raise DiscoveryBackendUnavailableError(self.unavailable_reason())

    def probe_identity(
        self, endpoint: str, scope: DiscoveryScanScope,
    ) -> DiscoveryObservation | None:
        raise DiscoveryBackendUnavailableError(self.unavailable_reason())


class ConfiguredEndpointScanBackend(DiscoveryBackend):
    """Scan of explicitly approved endpoints only — the bounded lane.

    Contacts exactly ``scope.approved_endpoints`` (capped by
    ``scope.max_endpoints``) through the injected read-only prober.
    Anything outside the approved set is refused with
    :class:`DiscoveryScopeError` — there is no ambient sweep.
    """

    backend_id = 'htdt-configured-endpoint-scan'
    backend_version = '1'
    mechanism: DiscoveryMechanism = 'configured_endpoint_scan'

    def __init__(self, prober: EndpointProber) -> None:
        self._prober = prober

    def available(self) -> bool:
        return True

    def _check_scope(self, scope: DiscoveryScanScope) -> tuple[str, ...]:
        if not scope.approved_endpoints:
            raise DiscoveryScopeError(
                'configured endpoint scan requires approved_endpoints'
            )
        if len(scope.approved_endpoints) > scope.max_endpoints:
            raise DiscoveryScopeError(
                f'approved endpoint count {len(scope.approved_endpoints)} '
                f'exceeds scope max_endpoints {scope.max_endpoints}'
            )
        return scope.approved_endpoints

    def discover(
        self, scope: DiscoveryScanScope,
    ) -> tuple[DiscoveryObservation, ...]:
        endpoints = self._check_scope(scope)
        observations: list[DiscoveryObservation] = []
        for endpoint in endpoints:
            try:
                observed = self._prober.probe(endpoint)
            except Exception:  # error-boundary: probe is best-effort
                continue
            if observed is not None:
                observations.append(observed)
        return tuple(observations)

    def probe_identity(
        self, endpoint: str, scope: DiscoveryScanScope,
    ) -> DiscoveryObservation | None:
        if not scope.allows_endpoint(endpoint):
            raise DiscoveryScopeError(
                f'endpoint {endpoint} is outside the approved scope'
            )
        try:
            return self._prober.probe(endpoint)
        except Exception:  # error-boundary: unreachable reads as absent
            return None


class StaticEndpointProber:
    """Read-only prober backed by a fixed endpoint->observation map.

    Used by the configured-endpoint scan in tests and by deployments that
    stage observations through a documented probe file — it performs no
    network I/O itself.
    """

    def __init__(
        self, observations: Mapping[str, DiscoveryObservation],
    ) -> None:
        self._map = dict(observations)

    def probe(self, endpoint: str) -> DiscoveryObservation | None:
        return self._map.get(endpoint)


# ---------------------------------------------------------------------------
# Fake discovery backend — deterministic simulation of every named case.

class FakeDiscoveryDevice(BaseModel):
    """One fake endpoint the backend will report.

    ``reachable=False`` marks a stale endpoint (was once seen, now gone);
    ``stable_identity`` differentiates identity-replacement scenarios.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    endpoint: str = Field(min_length=1)
    manufacturer: str | None = 'FakeAudio'
    model: str | None = 'FAKE-AVR-1'
    device_name: str | None = None
    stable_identity: str | None = None
    firmware_version: str | None = '1.0.0'
    software_version: str | None = None
    advertised_capabilities: tuple[str, ...] = ()
    suggested_adapter_id: str | None = None
    reachable: bool = True


class FakeDiscoveryScenario(BaseModel):
    """Everything the fake backend simulates — all deterministic.

    Named cases the issue requires: ``devices=()`` is *no device*; one
    entry is *one device*; two entries sharing device_name+model with no
    stable identities is *duplicates*; ``reachable=False`` is *stale
    endpoint*; changing ``stable_identity`` on the same endpoint is
    *identity replacement*; changing ``firmware_version`` is *firmware
    change*.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    devices: tuple[FakeDiscoveryDevice, ...] = ()


def default_fake_devices() -> tuple[FakeDiscoveryDevice, ...]:
    return (
        FakeDiscoveryDevice(
            endpoint='avr-lan://192.0.2.10:23',
            manufacturer='Denon',
            model='AVR-X3800H',
            device_name='Living Room AVR',
            stable_identity='serial-abc-123',
            firmware_version='1.58.0',
            advertised_capabilities=('gain',),
            suggested_adapter_id='htdt-avr-lan',
        ),
    )


def default_fake_scenario(
    **overrides: Any,
) -> FakeDiscoveryScenario:
    devices = overrides.pop('devices', default_fake_devices())
    return FakeDiscoveryScenario(devices=tuple(devices))


class FakeDiscoveryBackend(DiscoveryBackend):
    """Deterministic in-process discovery backend — test evidence only.

    Its observations are flagged ``backend_is_simulated`` on every record
    so they can never read as real network evidence.
    """

    backend_id = 'htdt-fake-discovery'
    backend_version = '1'
    mechanism: DiscoveryMechanism = 'configured_endpoint_scan'
    backend_is_simulated = True

    def __init__(
        self,
        scenario: FakeDiscoveryScenario | None = None,
        *,
        mechanism: DiscoveryMechanism = 'configured_endpoint_scan',
        honour_scope: bool = True,
    ) -> None:
        self.scenario = scenario or default_fake_scenario()
        self.mechanism = mechanism
        self.honour_scope = honour_scope
        self.probe_calls: list[str] = []

    def available(self) -> bool:
        return True

    def _observe(self, device: FakeDiscoveryDevice) -> DiscoveryObservation:
        return DiscoveryObservation(
            endpoint=device.endpoint,
            manufacturer=device.manufacturer,
            model=device.model,
            device_name=device.device_name,
            stable_identity=device.stable_identity,
            firmware_version=device.firmware_version,
            software_version=device.software_version,
            advertised_capabilities=device.advertised_capabilities,
            suggested_adapter_id=device.suggested_adapter_id,
        )

    def _endpoints_for(self, scope: DiscoveryScanScope) -> set[str] | None:
        if not self.honour_scope:
            return None
        if self.mechanism == 'configured_endpoint_scan':
            return set(scope.approved_endpoints)
        return None

    def discover(
        self, scope: DiscoveryScanScope,
    ) -> tuple[DiscoveryObservation, ...]:
        allowed = self._endpoints_for(scope)
        result: list[DiscoveryObservation] = []
        for device in self.scenario.devices:
            if allowed is not None and device.endpoint not in allowed:
                continue
            if not device.reachable:
                continue
            result.append(self._observe(device))
        return tuple(result)

    def probe_identity(
        self, endpoint: str, scope: DiscoveryScanScope,
    ) -> DiscoveryObservation | None:
        self.probe_calls.append(endpoint)
        allowed = self._endpoints_for(scope)
        if allowed is not None and endpoint not in allowed:
            raise DiscoveryScopeError(
                f'endpoint {endpoint} is outside the approved scope'
            )
        for device in self.scenario.devices:
            if device.endpoint == endpoint:
                if not device.reachable:
                    return None
                return self._observe(device)
        return None


# ---------------------------------------------------------------------------
# Capability prober — the PROBE rung of the handshake.

class CapabilityProbeResult(BaseModel):
    """What a capability negotiation observed on one endpoint.

    ``report=None`` means the endpoint answered but no usable manifest
    could be negotiated — the probe outcome reads ``insufficient``.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    report: AdapterCapabilityReport | None = None
    firmware_version: str | None = None
    notes: tuple[str, ...] = ()


class CapabilityProber(Protocol):
    """Capability negotiation contract — read-only, never applies."""

    prober_id: str

    def probe_capability(self, endpoint: str) -> CapabilityProbeResult: ...


class AdapterCapabilityProber:
    """Publishes a calibration adapter's own capability manifest.

    The manifest is the adapter's declared surface (#878); when the
    adapter exposes a ``firmware_version(endpoint)`` read hook it is
    consumed, otherwise the probe's firmware stays ``None`` — never
    inferred.
    """

    def __init__(
        self,
        adapter: Any,
        *,
        prober_id: str | None = None,
    ) -> None:
        self._adapter = adapter
        self.prober_id = prober_id or getattr(
            adapter, 'adapter_id', 'adapter-capability-prober',
        )

    def probe_capability(self, endpoint: str) -> CapabilityProbeResult:
        report: AdapterCapabilityReport = self._adapter.capability()
        firmware_probe = getattr(self._adapter, 'firmware_version', None)
        firmware: str | None = None
        if callable(firmware_probe):
            try:
                firmware = firmware_probe(endpoint)
            except Exception:  # error-boundary: version read best-effort
                firmware = None
        return CapabilityProbeResult(report=report, firmware_version=firmware)


class FakeCapabilityProber:
    """Deterministic capability prober for tests.

    ``results`` maps endpoint to :class:`CapabilityProbeResult`, an
    exception instance (raised — a refused/failed negotiation) or ``None``
    (endpoint unreachable).
    """

    prober_id = 'htdt-fake-capability-prober'

    def __init__(
        self, results: Mapping[str, CapabilityProbeResult | Exception | None],
    ) -> None:
        self._results = dict(results)
        self.probed: list[str] = []

    def probe_capability(self, endpoint: str) -> CapabilityProbeResult:
        self.probed.append(endpoint)
        result = self._results.get(endpoint)
        if result is None:
            raise DiscoveryBackendUnavailableError(
                f'fake prober: endpoint {endpoint} unreachable'
            )
        if isinstance(result, Exception):
            raise result
        return result


def capability_snapshot_sha256(report: AdapterCapabilityReport) -> str:
    """Canonical digest of one negotiated capability manifest."""
    return _hash(report.model_dump(mode='json'))


# ---------------------------------------------------------------------------
# Sealed records

class DiscoveryRunRecord(BaseModel):
    """One sealed discovery pass over one operator-approved scope."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    run_id: str = Field(min_length=1)
    run_sha256: str = Field(pattern=_SHA256)
    document_id: str = Field(min_length=1)
    backend_id: str = Field(min_length=1)
    backend_version: str = Field(min_length=1)
    mechanism: DiscoveryMechanism
    backend_is_simulated: bool
    scope_sha256: str = Field(pattern=_SHA256)
    scope_approved_by: str = Field(min_length=1)
    started_at_utc: str = Field(min_length=1)
    finished_at_utc: str = Field(min_length=1)
    outcome: DiscoveryRunOutcome
    device_count: int = Field(ge=0)
    ambiguous_count: int = Field(ge=0)
    failure_reason: str | None = None
    notes: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid_run(self) -> 'DiscoveryRunRecord':
        if self.ambiguous_count > self.device_count:
            raise ValueError('ambiguous_count exceeds device_count')
        if self.outcome == 'completed' and self.failure_reason is not None:
            raise ValueError('completed run cannot carry a failure reason')
        if (
            self.outcome != 'completed'
            and (self.device_count or self.ambiguous_count)
        ):
            raise ValueError('non-completed run cannot emit devices')
        if self.run_sha256 != _hash(self.identity_payload()):
            raise ValueError('DiscoveryRunRecord hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'run_id', 'run_sha256'},
        )


class DiscoveredDeviceRecord(BaseModel):
    """Sealed observation of one endpoint — the IDENTIFY rung."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    device_id: str = Field(min_length=1)
    device_sha256: str = Field(pattern=_SHA256)
    document_id: str = Field(min_length=1)
    run_ref: AuthorityRef | None = None
    discovery_mechanism: DiscoveryMechanism
    endpoint: str = Field(min_length=1)
    manufacturer: str | None = None
    model: str | None = None
    device_name: str | None = None
    stable_identity: str | None = None
    firmware_version: str | None = None
    software_version: str | None = None
    advertised_capabilities: tuple[str, ...] = ()
    suggested_adapter_id: str | None = None
    identity_state: DeviceIdentityState
    evidence_basis: DeviceEvidenceBasis
    #: Shared key for records in one ambiguity cluster.
    ambiguity_group: str | None = None
    observed_at_utc: str = Field(min_length=1)
    backend_id: str = Field(min_length=1)
    backend_is_simulated: bool

    @model_validator(mode='after')
    def valid_device(self) -> 'DiscoveredDeviceRecord':
        if self.identity_state == 'ambiguous' and not self.ambiguity_group:
            raise ValueError('ambiguous device needs an ambiguity_group')
        if (
            self.identity_state == 'identified'
            and not (self.manufacturer and self.model)
        ):
            raise ValueError(
                'identified requires manufacturer and model observed'
            )
        if (
            self.discovery_mechanism == 'manual_entry'
            and self.evidence_basis != 'operator_declared'
        ):
            raise ValueError('manual entry is operator-declared evidence')
        if (
            self.discovery_mechanism != 'manual_entry'
            and self.evidence_basis == 'operator_declared'
            and self.identity_state != 'manual_entry'
        ):
            raise ValueError(
                'operator-declared basis must carry manual_entry state'
            )
        if self.device_sha256 != _hash(self.identity_payload()):
            raise ValueError('DiscoveredDeviceRecord hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'device_id', 'device_sha256'},
        )


class CapabilityProbeRecord(BaseModel):
    """Sealed capability negotiation for one device/adapter pair."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    probe_id: str = Field(min_length=1)
    probe_sha256: str = Field(pattern=_SHA256)
    document_id: str = Field(min_length=1)
    device_ref: AuthorityRef
    adapter_id: str = Field(min_length=1)
    prober_id: str = Field(min_length=1)
    outcome: CapabilityProbeOutcome
    #: The negotiated manifest — present exactly when outcome='probed'.
    capability_report: AdapterCapabilityReport | None = None
    capability_snapshot_sha256: str | None = Field(
        default=None, pattern=_SHA256,
    )
    firmware_version: str | None = None
    probed_at_utc: str = Field(min_length=1)
    failure_reason: str | None = None
    notes: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid_probe(self) -> 'CapabilityProbeRecord':
        if self.outcome == 'probed':
            if self.capability_report is None:
                raise ValueError('probed outcome requires the manifest')
            if self.capability_snapshot_sha256 is None:
                raise ValueError('probed outcome requires the snapshot sha')
            if self.capability_report.adapter_id != self.adapter_id:
                raise ValueError(
                    'capability manifest belongs to a different adapter'
                )
        else:
            if self.capability_report is not None:
                raise ValueError(
                    'a failed probe cannot carry a capability manifest'
                )
            if self.capability_snapshot_sha256 is not None:
                raise ValueError(
                    'a failed probe cannot carry a snapshot sha'
                )
            if self.failure_reason is None:
                raise ValueError('a failed probe must record the reason')
        if self.probe_sha256 != _hash(self.identity_payload()):
            raise ValueError('CapabilityProbeRecord hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'probe_id', 'probe_sha256'},
        )


class TrustedDeviceBinding(BaseModel):
    """The explicit operator bind decision — the TRUSTED ENDPOINT rung.

    Pins the device identity, the negotiated capability snapshot sha and
    the adapter profile. One logical binding is a *chain* of records
    sharing ``binding_id``: state transitions emit a new record with
    ``supersedes_record_sha256`` so history is never rewritten.
    ``credential_ref`` is only the secret's *name* — credentials never
    enter project evidence.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    binding_record_id: str = Field(min_length=1)
    binding_record_sha256: str = Field(pattern=_SHA256)
    binding_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    device_ref: AuthorityRef
    probe_ref: AuthorityRef
    adapter_id: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    device_identity_key: str | None = None
    identity_basis: IdentityBasis
    capability_snapshot_sha256: str = Field(pattern=_SHA256)
    firmware_version: str | None = None
    trust_state: TrustedBindingState
    operator_id: str = Field(min_length=1)
    bound_at_utc: str = Field(min_length=1)
    credential_ref: str | None = None
    #: Operator's reason when binding an ambiguous device.
    disambiguation_basis: str | None = None
    supersedes_record_sha256: str | None = Field(
        default=None, pattern=_SHA256,
    )
    notes: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid_binding(self) -> 'TrustedDeviceBinding':
        if self.credential_ref is not None and any(
            marker in self.credential_ref.lower()
            for marker in _CREDENTIAL_VALUE_MARKERS
        ):
            raise ValueError(
                'credential_ref is a secret *name*, never a value'
            )
        if self.trust_state == 'trusted':
            if (
                self.device_identity_key is None
                and self.identity_basis == 'endpoint_only'
                and self.disambiguation_basis is None
            ):
                raise ValueError(
                    'trusted requires a stable identity, an operator-'
                    'declared basis or an explicit disambiguation — '
                    'endpoint-only identity stays unverifiable'
                )
        if self.binding_record_sha256 != _hash(self.identity_payload()):
            raise ValueError('TrustedDeviceBinding hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python',
            exclude={'binding_record_id', 'binding_record_sha256'},
        )


class DeviceIdentityDriftReport(BaseModel):
    """Sealed verdict of a binding re-verification against live identity."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    report_id: str = Field(min_length=1)
    report_sha256: str = Field(pattern=_SHA256)
    document_id: str = Field(min_length=1)
    binding_ref: AuthorityRef
    drift_kind: DriftKind | None = None
    verdict: DriftVerdict
    expected_identity_key: str | None = None
    observed_identity_key: str | None = None
    expected_firmware_version: str | None = None
    observed_firmware_version: str | None = None
    observed_capability_snapshot_sha256: str | None = Field(
        default=None, pattern=_SHA256,
    )
    recommendation: DriftRecommendation
    observed_at_utc: str = Field(min_length=1)
    notes: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid_report(self) -> 'DeviceIdentityDriftReport':
        if self.verdict == 'unchanged' and self.drift_kind is not None:
            raise ValueError('unchanged verdict carries no drift kind')
        if self.verdict != 'unchanged' and self.drift_kind is None:
            raise ValueError('a drift verdict needs its drift kind')
        if self.verdict in ('replacement_suspect', 'drift_invalidates'):
            if self.recommendation == 'none':
                raise ValueError(
                    'confirmed drift must recommend rebind/reprobe'
                )
        if self.report_sha256 != _hash(self.identity_payload()):
            raise ValueError('DeviceIdentityDriftReport hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'report_id', 'report_sha256'},
        )


class RebindingDecision(BaseModel):
    """The operator's sealed response to an identity-drift report."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    decision_id: str = Field(min_length=1)
    decision_sha256: str = Field(pattern=_SHA256)
    document_id: str = Field(min_length=1)
    previous_binding_ref: AuthorityRef
    drift_report_ref: AuthorityRef | None = None
    action: RebindingAction
    new_binding_ref: AuthorityRef | None = None
    operator_id: str = Field(min_length=1)
    decided_at_utc: str = Field(min_length=1)
    notes: tuple[str, ...] = ()

    @model_validator(mode='after')
    def valid_decision(self) -> 'RebindingDecision':
        needs_new = self.action in ('rebound_same_device', 'bound_replacement')
        if needs_new and self.new_binding_ref is None:
            raise ValueError(f'{self.action} requires the new binding ref')
        if not needs_new and self.new_binding_ref is not None:
            raise ValueError(
                f'{self.action} cannot carry a new binding ref'
            )
        if self.decision_sha256 != _hash(self.identity_payload()):
            raise ValueError('RebindingDecision hash mismatch')
        return self

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode='python', exclude={'decision_id', 'decision_sha256'},
        )


# ---------------------------------------------------------------------------
# Resolved target — what #868/#806/#878 consume.

class ResolvedTrustedTarget(BaseModel):
    """A trusted binding resolved into deployable target coordinates.

    Carries exactly what the deployment layers need: the adapter profile,
    the canonical ``target_ref`` (endpoint), a ready-made
    :class:`AdapterDeviceBinding`, the negotiated capability manifest and
    the credential *name* — never the credential.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    trusted_binding_record_id: str = Field(min_length=1)
    trusted_binding_sha256: str = Field(pattern=_SHA256)
    binding_id: str = Field(min_length=1)
    adapter_id: str = Field(min_length=1)
    target_ref: str = Field(min_length=1)
    adapter_binding: AdapterDeviceBinding
    capability: AdapterCapabilityReport
    identity_basis: IdentityBasis
    credential_ref: str | None = None
    warnings: tuple[str, ...] = ()


def resolve_trusted_target(
    binding: TrustedDeviceBinding,
    probe: CapabilityProbeRecord,
    device: DiscoveredDeviceRecord,
    *,
    routing: tuple[tuple[str, str], ...] = (),
) -> ResolvedTrustedTarget:
    """Resolve a *trusted* binding into deployable coordinates.

    Fail closed: anything but ``trust_state='trusted'`` raises, a probe
    or device that does not match the pinned refs raises, and the
    resolved adapter binding pins the same identity/firmware the
    operator approved.
    """
    if binding.trust_state != 'trusted':
        raise StaleBindingError(
            f'binding {binding.binding_id} is {binding.trust_state} — '
            'a stale or unverified binding is never silently re-used'
        )
    if probe.probe_id != binding.probe_ref.ref_id:
        raise DiscoveryBindingError(
            'probe record does not match the pinned probe ref'
        )
    if (
        probe.capability_snapshot_sha256
        != binding.capability_snapshot_sha256
    ):
        raise DiscoveryBindingError(
            'probe capability snapshot differs from the pinned snapshot'
        )
    if probe.capability_report is None:
        raise DiscoveryBindingError('probe record carries no manifest')
    if device.device_id != binding.device_ref.ref_id:
        raise DiscoveryBindingError(
            'device record does not match the pinned device ref'
        )
    if device.endpoint != binding.endpoint:
        raise DiscoveryBindingError(
            'device endpoint moved since the bind — rebind explicitly'
        )
    warnings: list[str] = []
    if binding.identity_basis != 'device_stable_id':
        warnings.append(
            f'identity basis is {binding.identity_basis} — device '
            'replacement cannot be machine-detected on re-check'
        )
    adapter_binding = build_device_binding(
        adapter_id=binding.adapter_id,
        device_family=probe.capability_report.device_family,
        device_model=device.model or 'unknown',
        device_serial=binding.endpoint,
        firmware_version=binding.firmware_version or 'unknown',
        routing=tuple(routing),
        bound_at_utc=binding.bound_at_utc,
        binding_id=binding.binding_id,
    )
    return ResolvedTrustedTarget(
        trusted_binding_record_id=binding.binding_record_id,
        trusted_binding_sha256=binding.binding_record_sha256,
        binding_id=binding.binding_id,
        adapter_id=binding.adapter_id,
        target_ref=binding.endpoint,
        adapter_binding=adapter_binding,
        capability=probe.capability_report,
        identity_basis=binding.identity_basis,
        credential_ref=binding.credential_ref,
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# Evaluation helpers

def _identity_state(observation: DiscoveryObservation) -> DeviceIdentityState:
    if observation.manufacturer and observation.model:
        return 'identified'
    if observation.manufacturer or observation.model or observation.stable_identity:
        return 'partial'
    return 'unidentified'


def _ambiguity_key(observation: DiscoveryObservation) -> str:
    return '|'.join(
        (part or '').strip().lower()
        for part in (
            observation.device_name,
            observation.model,
            observation.manufacturer,
        )
    )


def mark_ambiguity(
    observations: tuple[DiscoveryObservation, ...],
) -> dict[str, str]:
    """Endpoint -> ambiguity group key for ambiguous duplicates.

    Two or more endpoints advertising the *same* name/model/manufacturer
    with no pairwise-distinct stable identity are ambiguous — the service
    never picks one; the operator must disambiguate explicitly.
    """
    groups: dict[str, list[DiscoveryObservation]] = {}
    for observation in observations:
        groups.setdefault(_ambiguity_key(observation), []).append(observation)
    ambiguous: dict[str, str] = {}
    for key, members in groups.items():
        if len(members) < 2:
            continue
        identities = {m.stable_identity for m in members}
        if len(members) == len(identities) and None not in identities:
            # Stable identities fully discriminate the duplicates — each
            # is individually bindable.
            continue
        group_sha = _hash({'group': key, 'endpoints': sorted(
            m.endpoint for m in members)})
        group_id = _semantic_id('ambgrp', group_sha)
        for member in members:
            ambiguous[member.endpoint] = group_id
    return ambiguous


def derive_binding_state(
    records: tuple[TrustedDeviceBinding, ...],
) -> TrustedBindingState:
    """Current state of one binding chain — fail closed."""
    if not records:
        return 'unverifiable'
    return records[-1].trust_state


def evaluate_identity_drift(
    binding: TrustedDeviceBinding,
    observed: DiscoveryObservation | None,
) -> tuple[DriftKind | None, DriftVerdict, DriftRecommendation, str]:
    """Compare a trusted binding against a fresh identity observation.

    Returns ``(drift_kind, verdict, recommendation, detail)``. Pure —
    the service seals the report and the successor binding record.
    """
    if binding.trust_state != 'trusted':
        raise StaleBindingError(
            f'binding {binding.binding_id} is {binding.trust_state}; '
            'drift evaluation only re-verifies trusted bindings'
        )
    if observed is None:
        return (
            'endpoint_unreachable',
            'drift_invalidates',
            'rebind_required',
            'endpoint no longer answers — the bound device is absent',
        )
    if (
        binding.device_identity_key is not None
        and observed.stable_identity is not None
        and observed.stable_identity != binding.device_identity_key
    ):
        return (
            'identity_replaced',
            'replacement_suspect',
            'rebind_required',
            'the endpoint now reports a different stable identity — '
            'the bound unit was likely replaced',
        )
    if (
        binding.device_identity_key is not None
        and observed.stable_identity is None
    ):
        return (
            'identity_unverifiable',
            'unverifiable',
            'rebind_required',
            'the endpoint stopped reporting a stable identity — the '
            'bound unit can no longer be proven present',
        )
    if (
        binding.firmware_version is not None
        and observed.firmware_version is not None
        and observed.firmware_version != binding.firmware_version
    ):
        return (
            'firmware_changed',
            'drift_invalidates',
            'reprobe_required',
            'firmware/software changed — capability negotiation must '
            'be re-run before reuse',
        )
    if (
        binding.device_identity_key is None
        and binding.identity_basis == 'endpoint_only'
    ):
        return (
            'identity_unverifiable',
            'unverifiable',
            'rebind_required',
            'endpoint-only identity cannot prove the same device — '
            'explicit rebind required',
        )
    return (None, 'unchanged', 'none', 'bound identity re-observed')


def compare_capability_snapshot(
    binding: TrustedDeviceBinding,
    observed_sha256: str | None,
) -> tuple[DriftKind | None, DriftVerdict, DriftRecommendation, str] | None:
    """Capability drift after an identity-clean re-probe."""
    if observed_sha256 is None:
        return None
    if observed_sha256 != binding.capability_snapshot_sha256:
        return (
            'capability_changed',
            'drift_invalidates',
            'reprobe_required',
            'negotiated capability surface changed — adapter assumptions '
            'are stale',
        )
    return None


# ---------------------------------------------------------------------------
# Service — drives the ladder and seals every record.

class DeviceDiscoveryService:
    """The DISCOVER → … → USE ADAPTER authority.

    Every record is sealed before emission and persisted through
    ``repository`` when given. The service itself never mutates a device:
    the only device-touching calls are the backend's read-only
    ``discover``/``probe_identity`` and the prober's read-only
    ``probe_capability``.
    """

    def __init__(self, repository: Any | None = None) -> None:
        self._repository = repository

    # -- persistence --------------------------------------------------

    def _save(self, record: BaseModel) -> None:
        repo = self._repository
        if repo is None:
            return
        name = type(record).__name__
        method = {
            'DiscoveryRunRecord': 'save_run',
            'DiscoveredDeviceRecord': 'save_device',
            'CapabilityProbeRecord': 'save_probe',
            'TrustedDeviceBinding': 'save_binding',
            'DeviceIdentityDriftReport': 'save_drift_report',
            'RebindingDecision': 'save_rebinding_decision',
        }[name]
        getattr(repo, method)(record)

    # -- DISCOVER / IDENTIFY -------------------------------------------

    def run_discovery(
        self,
        *,
        document_id: str,
        backend: DiscoveryBackend,
        scope: DiscoveryScanScope,
        started_at_utc: str,
        finished_at_utc: str,
    ) -> tuple[DiscoveryRunRecord, tuple[DiscoveredDeviceRecord, ...]]:
        """Run one read-only discovery pass and seal the evidence."""
        observations: tuple[DiscoveryObservation, ...] = ()
        outcome: DiscoveryRunOutcome = 'completed'
        failure_reason: str | None = None
        if not backend.available():
            outcome = 'unavailable'
            failure_reason = backend.unavailable_reason()
        else:
            try:
                observations = backend.discover(scope)
            except DiscoveryScopeError as exc:
                outcome = 'scope_rejected'
                failure_reason = str(exc)
            except Exception as exc:  # error-boundary: backend read
                outcome = 'failed'
                failure_reason = str(exc)
        # Ambiguity marking is pure over observations, so the run's
        # counts are known before any device record is sealed — the run
        # is sealed first and devices pin the real run ref (a record may
        # only reference records sealed before it).
        ambiguous = (
            mark_ambiguity(observations) if outcome == 'completed' else {}
        )
        device_count = len(observations) if outcome == 'completed' else 0
        run = self._build_run(
            document_id=document_id,
            backend=backend,
            scope=scope,
            started_at_utc=started_at_utc,
            finished_at_utc=finished_at_utc,
            outcome=outcome,
            device_count=device_count,
            ambiguous_count=len(ambiguous),
            failure_reason=failure_reason,
        )
        self._save(run)
        devices: list[DiscoveredDeviceRecord] = []
        if outcome == 'completed':
            run_ref = _ref('discovery_run', run.run_id, run.run_sha256)
            for observation in observations:
                state = _identity_state(observation)
                group = ambiguous.get(observation.endpoint)
                if group is not None:
                    state = 'ambiguous'
                device = self._build_device(
                    document_id=document_id,
                    run_ref=run_ref,
                    observation=observation,
                    identity_state=state,
                    ambiguity_group=group,
                    observed_at_utc=finished_at_utc,
                    backend=backend,
                )
                devices.append(device)
                self._save(device)
        return run, tuple(devices)

    def _build_run(
        self,
        *,
        document_id: str,
        backend: DiscoveryBackend,
        scope: DiscoveryScanScope,
        started_at_utc: str,
        finished_at_utc: str,
        outcome: DiscoveryRunOutcome,
        device_count: int,
        ambiguous_count: int,
        failure_reason: str | None,
    ) -> DiscoveryRunRecord:
        payload: dict[str, Any] = {
            'document_id': document_id,
            'backend_id': backend.backend_id,
            'backend_version': backend.backend_version,
            'mechanism': backend.mechanism,
            'backend_is_simulated': backend.backend_is_simulated,
            'scope_sha256': scope.scope_sha256(),
            'scope_approved_by': scope.approved_by,
            'started_at_utc': started_at_utc,
            'finished_at_utc': finished_at_utc,
            'outcome': outcome,
            'device_count': device_count,
            'ambiguous_count': ambiguous_count,
            'failure_reason': failure_reason,
            'notes': (),
        }
        rid, digest = _seal_id(DiscoveryRunRecord, 'disc-run', payload)
        return DiscoveryRunRecord(run_id=rid, run_sha256=digest, **payload)

    def _build_device(
        self,
        *,
        document_id: str,
        run_ref: AuthorityRef | None,
        observation: DiscoveryObservation,
        identity_state: DeviceIdentityState,
        ambiguity_group: str | None,
        observed_at_utc: str,
        backend: DiscoveryBackend,
    ) -> DiscoveredDeviceRecord:
        payload: dict[str, Any] = {
            'document_id': document_id,
            'run_ref': run_ref,
            'discovery_mechanism': backend.mechanism,
            'endpoint': observation.endpoint,
            'manufacturer': observation.manufacturer,
            'model': observation.model,
            'device_name': observation.device_name,
            'stable_identity': observation.stable_identity,
            'firmware_version': observation.firmware_version,
            'software_version': observation.software_version,
            'advertised_capabilities': tuple(
                observation.advertised_capabilities
            ),
            'suggested_adapter_id': observation.suggested_adapter_id,
            'identity_state': identity_state,
            'evidence_basis': 'advertised',
            'ambiguity_group': ambiguity_group,
            'observed_at_utc': observed_at_utc,
            'backend_id': backend.backend_id,
            'backend_is_simulated': backend.backend_is_simulated,
        }
        rid, digest = _seal_id(DiscoveredDeviceRecord, 'disdev', payload)
        return DiscoveredDeviceRecord(
            device_id=rid, device_sha256=digest, **payload,
        )

    def manual_entry(
        self,
        *,
        document_id: str,
        endpoint: str,
        operator_id: str,
        at_utc: str,
        manufacturer: str | None = None,
        model: str | None = None,
        device_name: str | None = None,
        stable_identity: str | None = None,
        firmware_version: str | None = None,
        advertised_capabilities: tuple[str, ...] = (),
        suggested_adapter_id: str | None = None,
    ) -> DiscoveredDeviceRecord:
        """The fallback path — an operator-declared endpoint.

        Identity is what the operator declared, recorded as such
        (``evidence_basis='operator_declared'``); a manual entry still
        needs a capability probe and an explicit bind before use.
        """
        payload: dict[str, Any] = {
            'document_id': document_id,
            'run_ref': None,
            'discovery_mechanism': 'manual_entry',
            'endpoint': endpoint,
            'manufacturer': manufacturer,
            'model': model,
            'device_name': device_name,
            'stable_identity': stable_identity,
            'firmware_version': firmware_version,
            'software_version': None,
            'advertised_capabilities': tuple(advertised_capabilities),
            'suggested_adapter_id': suggested_adapter_id,
            'identity_state': 'manual_entry',
            'evidence_basis': 'operator_declared',
            'ambiguity_group': None,
            'observed_at_utc': at_utc,
            'backend_id': 'operator-manual-entry',
            'backend_is_simulated': False,
        }
        rid, digest = _seal_id(DiscoveredDeviceRecord, 'disdev', payload)
        record = DiscoveredDeviceRecord(
            device_id=rid, device_sha256=digest, **payload,
        )
        self._save(record)
        return record

    # -- CAPABILITY PROBE ----------------------------------------------

    def probe_capability(
        self,
        *,
        document_id: str,
        device: DiscoveredDeviceRecord,
        prober: CapabilityProber,
        adapter_id: str,
        at_utc: str,
    ) -> CapabilityProbeRecord:
        """Negotiate capabilities for one discovered device — read-only."""
        outcome: CapabilityProbeOutcome
        report: AdapterCapabilityReport | None = None
        snapshot: str | None = None
        firmware: str | None = None
        failure_reason: str | None = None
        notes: list[str] = []
        if device.document_id != document_id:
            raise DiscoveryBindingError(
                'probe device belongs to a different document'
            )
        # Probing is read-only negotiation — it runs on any identified or
        # partially identified endpoint (including ambiguous devices,
        # whose probe output may BE the operator's disambiguation
        # evidence). Identity gates live in bind(), not here.
        try:
            result = prober.probe_capability(device.endpoint)
        except DiscoveryBackendUnavailableError as exc:
            outcome = 'unreachable'
            failure_reason = str(exc)
        except AdapterProbeRefusedError as exc:
            outcome = 'refused'
            failure_reason = str(exc)
        except Exception as exc:  # error-boundary: prober read
            outcome = 'refused'
            failure_reason = str(exc)
        else:
            if result.report is None:
                outcome = 'insufficient'
                failure_reason = (
                    'endpoint answered but no capability manifest '
                    'could be negotiated'
                )
                firmware = result.firmware_version
                notes.extend(result.notes)
            elif result.report.adapter_id != adapter_id:
                outcome = 'unsupported_adapter'
                failure_reason = (
                    f'probed manifest is for adapter '
                    f'{result.report.adapter_id}, not {adapter_id}'
                )
            else:
                outcome = 'probed'
                report = result.report
                snapshot = capability_snapshot_sha256(report)
                firmware = result.firmware_version
                notes.extend(result.notes)
        record = self._build_probe(
            document_id=document_id,
            device=device,
            adapter_id=adapter_id,
            prober_id=prober.prober_id,
            outcome=outcome,
            report=report,
            snapshot=snapshot,
            firmware=firmware,
            at_utc=at_utc,
            failure_reason=failure_reason,
            notes=notes,
        )
        self._save(record)
        return record

    def _build_probe(
        self,
        *,
        document_id: str,
        device: DiscoveredDeviceRecord,
        adapter_id: str,
        prober_id: str,
        outcome: CapabilityProbeOutcome,
        report: AdapterCapabilityReport | None,
        snapshot: str | None,
        firmware: str | None,
        at_utc: str,
        failure_reason: str | None,
        notes: list[str],
    ) -> CapabilityProbeRecord:
        payload: dict[str, Any] = {
            'document_id': document_id,
            'device_ref': _ref(
                'discovered_device', device.device_id, device.device_sha256,
            ),
            'adapter_id': adapter_id,
            'prober_id': prober_id,
            'outcome': outcome,
            'capability_report': report,
            'capability_snapshot_sha256': snapshot,
            'firmware_version': firmware,
            'probed_at_utc': at_utc,
            'failure_reason': failure_reason,
            'notes': tuple(notes),
        }
        rid, digest = _seal_id(CapabilityProbeRecord, 'cprob', payload)
        return CapabilityProbeRecord(probe_id=rid, probe_sha256=digest, **payload)

    # -- USER BIND / TRUSTED ENDPOINT -----------------------------------

    def bind(
        self,
        *,
        document_id: str,
        device: DiscoveredDeviceRecord,
        probe: CapabilityProbeRecord,
        operator_id: str,
        at_utc: str,
        credential_ref: str | None = None,
        disambiguation_basis: str | None = None,
        binding_id: str | None = None,
        notes: tuple[str, ...] = (),
    ) -> TrustedDeviceBinding:
        """Record the operator's explicit bind decision.

        A bind is only ever as strong as its identity evidence: stable
        device identity or an explicit operator-declared/disambiguated
        basis reaches ``trusted``; endpoint-only identity stays
        ``unverifiable`` — usable by no deployment path.
        """
        if device.document_id != document_id:
            raise DiscoveryBindingError(
                'bind device belongs to a different document'
            )
        if probe.document_id != document_id:
            raise DiscoveryBindingError(
                'bind probe belongs to a different document'
            )
        if probe.device_ref.ref_id != device.device_id:
            raise DiscoveryBindingError(
                'probe does not belong to this device record'
            )
        if probe.outcome != 'probed':
            raise DiscoveryBindingError(
                f'cannot bind on probe outcome {probe.outcome}'
            )
        if device.identity_state == 'unidentified':
            raise DiscoveryBindingError(
                'an unidentified endpoint can never be bound'
            )
        if device.identity_state == 'ambiguous' and not disambiguation_basis:
            raise AmbiguousDeviceError(
                f'device {device.device_id} is ambiguous '
                f'({device.ambiguity_group}) — bind requires an explicit '
                'disambiguation basis'
            )
        identity_basis: IdentityBasis
        identity_key: str | None = device.stable_identity
        if device.stable_identity is not None:
            identity_basis = 'device_stable_id'
        elif device.evidence_basis == 'operator_declared':
            identity_basis = 'operator_declared'
            identity_key = (
                device.stable_identity
                or f'operator:{operator_id}:{device.endpoint}'
            )
        else:
            identity_basis = 'endpoint_only'
        trust_state: TrustedBindingState
        if (
            identity_basis == 'endpoint_only'
            and disambiguation_basis is None
        ):
            trust_state = 'unverifiable'
        else:
            trust_state = 'trusted'
        payload: dict[str, Any] = {
            'binding_id': binding_id or f'tdb-{uuid4().hex[:16]}',
            'document_id': document_id,
            'device_ref': _ref(
                'discovered_device', device.device_id, device.device_sha256,
            ),
            'probe_ref': _ref(
                'capability_probe', probe.probe_id, probe.probe_sha256,
            ),
            'adapter_id': probe.adapter_id,
            'endpoint': device.endpoint,
            'device_identity_key': identity_key,
            'identity_basis': identity_basis,
            'capability_snapshot_sha256': probe.capability_snapshot_sha256,
            'firmware_version': (
                probe.firmware_version or device.firmware_version
            ),
            'trust_state': trust_state,
            'operator_id': operator_id,
            'bound_at_utc': at_utc,
            'credential_ref': credential_ref,
            'disambiguation_basis': disambiguation_basis,
            'supersedes_record_sha256': None,
            'notes': tuple(notes),
        }
        rid, digest = _seal_id(TrustedDeviceBinding, 'tdbr', payload)
        record = TrustedDeviceBinding(
            binding_record_id=rid, binding_record_sha256=digest, **payload,
        )
        self._save(record)
        return record

    # -- drift / re-verification ----------------------------------------

    def recheck_binding(
        self,
        *,
        document_id: str,
        binding_chain: tuple[TrustedDeviceBinding, ...],
        backend: DiscoveryBackend,
        scope: DiscoveryScanScope,
        prober: CapabilityProber | None = None,
        at_utc: str,
    ) -> tuple[DeviceIdentityDriftReport, TrustedDeviceBinding | None]:
        """Re-verify the current binding record against the live endpoint.

        Confirmed drift emits a drift report *and* demotes the chain with
        a new ``invalidated_drift``/``unverifiable`` successor record — a
        stale binding is never silently re-used. An unchanged verdict
        emits only the report (the binding stays trusted).
        """
        if not binding_chain:
            raise DiscoveryBindingError('empty binding chain')
        current = binding_chain[-1]
        if current.document_id != document_id:
            raise DiscoveryBindingError(
                'binding belongs to a different document'
            )
        if current.trust_state != 'trusted':
            raise StaleBindingError(
                f'binding {current.binding_id} is {current.trust_state}'
            )
        try:
            observed = backend.probe_identity(current.endpoint, scope)
        except DiscoveryScopeError:
            raise
        except Exception:  # error-boundary: backend read
            observed = None
        kind, verdict, recommendation, detail = evaluate_identity_drift(
            current, observed,
        )
        observed_snapshot: str | None = None
        if (
            verdict == 'unchanged'
            and prober is not None
            and observed is not None
        ):
            try:
                result = prober.probe_capability(current.endpoint)
                observed_snapshot = capability_snapshot_sha256(
                    result.report,
                )
            except Exception:  # error-boundary: prober read
                observed_snapshot = None
            cap = compare_capability_snapshot(current, observed_snapshot)
            if cap is not None:
                kind, verdict, recommendation, detail = cap
        report = self._build_drift_report(
            document_id=document_id,
            binding=current,
            kind=kind,
            verdict=verdict,
            observed=observed,
            observed_snapshot=observed_snapshot,
            recommendation=recommendation,
            at_utc=at_utc,
            detail=detail,
        )
        self._save(report)
        successor: TrustedDeviceBinding | None = None
        if verdict in ('replacement_suspect', 'drift_invalidates'):
            successor = self._successor(current, 'invalidated_drift', at_utc, detail)
        elif verdict == 'unverifiable':
            successor = self._successor(current, 'unverifiable', at_utc, detail)
        if successor is not None:
            self._save(successor)
        return report, successor

    def _successor(
        self,
        current: TrustedDeviceBinding,
        state: TrustedBindingState,
        at_utc: str,
        detail: str,
    ) -> TrustedDeviceBinding:
        payload: dict[str, Any] = {
            'binding_id': current.binding_id,
            'document_id': current.document_id,
            'device_ref': current.device_ref,
            'probe_ref': current.probe_ref,
            'adapter_id': current.adapter_id,
            'endpoint': current.endpoint,
            'device_identity_key': current.device_identity_key,
            'identity_basis': current.identity_basis,
            'capability_snapshot_sha256': current.capability_snapshot_sha256,
            'firmware_version': current.firmware_version,
            'trust_state': state,
            'operator_id': current.operator_id,
            'bound_at_utc': at_utc,
            'credential_ref': current.credential_ref,
            'disambiguation_basis': current.disambiguation_basis,
            'supersedes_record_sha256': current.binding_record_sha256,
            'notes': (detail,),
        }
        rid, digest = _seal_id(TrustedDeviceBinding, 'tdbr', payload)
        return TrustedDeviceBinding(
            binding_record_id=rid, binding_record_sha256=digest, **payload,
        )

    def _build_drift_report(
        self,
        *,
        document_id: str,
        binding: TrustedDeviceBinding,
        kind: DriftKind | None,
        verdict: DriftVerdict,
        observed: DiscoveryObservation | None,
        observed_snapshot: str | None,
        recommendation: DriftRecommendation,
        at_utc: str,
        detail: str,
    ) -> DeviceIdentityDriftReport:
        payload: dict[str, Any] = {
            'document_id': document_id,
            'binding_ref': _ref(
                'trusted_device_binding',
                binding.binding_record_id, binding.binding_record_sha256,
            ),
            'drift_kind': kind,
            'verdict': verdict,
            'expected_identity_key': binding.device_identity_key,
            'observed_identity_key': (
                observed.stable_identity if observed is not None else None
            ),
            'expected_firmware_version': binding.firmware_version,
            'observed_firmware_version': (
                observed.firmware_version if observed is not None else None
            ),
            'observed_capability_snapshot_sha256': observed_snapshot,
            'recommendation': recommendation,
            'observed_at_utc': at_utc,
            'notes': (detail,),
        }
        rid, digest = _seal_id(DeviceIdentityDriftReport, 'didr', payload)
        return DeviceIdentityDriftReport(report_id=rid, report_sha256=digest, **payload)

    # -- rebinding decision ----------------------------------------------

    def record_rebinding_decision(
        self,
        *,
        document_id: str,
        previous_binding: TrustedDeviceBinding,
        action: RebindingAction,
        operator_id: str,
        at_utc: str,
        drift_report: DeviceIdentityDriftReport | None = None,
        new_binding: TrustedDeviceBinding | None = None,
        notes: tuple[str, ...] = (),
    ) -> RebindingDecision:
        """Seal the operator's response to a drift report."""
        if previous_binding.document_id != document_id:
            raise DiscoveryBindingError(
                'decision binding belongs to a different document'
            )
        if drift_report is not None and (
            drift_report.binding_ref.ref_id
            != previous_binding.binding_record_id
        ):
            raise DiscoveryBindingError(
                'drift report does not concern this binding'
            )
        if new_binding is not None and (
            new_binding.binding_id == previous_binding.binding_id
            and new_binding.trust_state != 'trusted'
        ):
            raise DiscoveryBindingError(
                'a rebind must carry a trusted successor binding'
            )
        payload: dict[str, Any] = {
            'document_id': document_id,
            'previous_binding_ref': _ref(
                'trusted_device_binding',
                previous_binding.binding_record_id,
                previous_binding.binding_record_sha256,
            ),
            'drift_report_ref': (
                _ref(
                    'device_identity_drift_report',
                    drift_report.report_id, drift_report.report_sha256,
                )
                if drift_report is not None else None
            ),
            'action': action,
            'new_binding_ref': (
                _ref(
                    'trusted_device_binding',
                    new_binding.binding_record_id,
                    new_binding.binding_record_sha256,
                )
                if new_binding is not None else None
            ),
            'operator_id': operator_id,
            'decided_at_utc': at_utc,
            'notes': tuple(notes),
        }
        rid, digest = _seal_id(RebindingDecision, 'rbd', payload)
        record = RebindingDecision(
            decision_id=rid, decision_sha256=digest, **payload,
        )
        self._save(record)
        return record

    # -- USE ADAPTER -----------------------------------------------------

    def resolve_trusted_target(
        self,
        *,
        binding_chain: tuple[TrustedDeviceBinding, ...],
        probe: CapabilityProbeRecord,
        device: DiscoveredDeviceRecord,
        routing: tuple[tuple[str, str], ...] = (),
    ) -> ResolvedTrustedTarget:
        """Resolve the chain head to deployable coordinates — fail closed."""
        current = binding_chain[-1] if binding_chain else None
        if current is None:
            raise StaleBindingError('empty binding chain')
        return resolve_trusted_target(
            current, probe, device, routing=routing,
        )


# ---------------------------------------------------------------------------
# JA labels

DEVICE_DISCOVERY_LABELS: dict[str, str] = {
    # mechanisms
    'mdns': 'mDNS/Bonjour',
    'ssdp': 'SSDP/UPnP',
    'vendor_documented': 'ベンダー公認探索',
    'configured_endpoint_scan': '承認済みエンドポイントスキャン',
    'manual_entry': '手動エンドポイント入力',
    # run outcomes
    'completed': '完了',
    'unavailable': '利用不可',
    'scope_rejected': 'スコープ拒否',
    'failed': '失敗',
    # identity states
    'identified': '識別済み',
    'partial': '一部識別',
    'unidentified': '未識別',
    'ambiguous': '曖昧',
    # evidence basis
    'advertised': '広告ベース',
    'operator_declared': '操作者申告',
    # probe outcomes
    'probed': 'プローブ済み',
    'unreachable': '到達不可',
    'refused': '拒否',
    'insufficient': '証跡不足',
    'unsupported_adapter': '非対応アダプタ',
    # trust states
    'trusted': '信頼済み',
    'unverifiable': '検証不可',
    'invalidated_drift': 'ドリフト無効化',
    'revoked': '失効',
    'superseded': '置き換え済み',
    # identity basis
    'device_stable_id': '機器固定ID',
    'endpoint_only': 'エンドポイントのみ',
    # drift kinds
    'identity_replaced': '識別子置き換え',
    'firmware_changed': 'ファームウェア変更',
    'capability_changed': '機能変更',
    'endpoint_unreachable': 'エンドポイント到達不可',
    'identity_unverifiable': '識別子検証不可',
    # drift verdicts
    'replacement_suspect': '置き換え疑い',
    'drift_invalidates': 'ドリフト無効',
    'unchanged': '変更なし',
    # recommendations / rebind actions
    'rebind_required': '再バインド必須',
    'reprobe_required': '再プローブ必須',
    'none': 'なし',
    'rebound_same_device': '同一機器へ再バインド',
    'bound_replacement': '交換機器へバインド',
    'kept_invalidated': '無効化のまま保持',
}


__all__ = [
    'AdapterCapabilityProber',
    'AdapterProbeRefusedError',
    'AmbiguousDeviceError',
    'CapabilityProbeOutcome',
    'CapabilityProbeRecord',
    'CapabilityProbeResult',
    'CapabilityProber',
    'ConfiguredEndpointScanBackend',
    'DEVICE_DISCOVERY_LABELS',
    'DeviceDiscoveryService',
    'DeviceEvidenceBasis',
    'DeviceIdentityDriftReport',
    'DeviceIdentityState',
    'DiscoveryBackend',
    'DiscoveryBackendUnavailableError',
    'DiscoveryBindingError',
    'DiscoveryError',
    'DiscoveryMechanism',
    'DiscoveryObservation',
    'DiscoveryRunOutcome',
    'DiscoveryRunRecord',
    'DiscoveryScanScope',
    'DiscoveredDeviceRecord',
    'DriftKind',
    'DriftRecommendation',
    'DriftVerdict',
    'EndpointProber',
    'FakeCapabilityProber',
    'FakeDiscoveryBackend',
    'FakeDiscoveryDevice',
    'FakeDiscoveryScenario',
    'IdentityBasis',
    'MdnsDiscoveryBackend',
    'RebindingAction',
    'RebindingDecision',
    'ResolvedTrustedTarget',
    'SsdpDiscoveryBackend',
    'StaleBindingError',
    'StaticEndpointProber',
    'TrustedBindingState',
    'TrustedDeviceBinding',
    'VendorDiscoveryBackend',
    'build_scan_scope',
    'capability_snapshot_sha256',
    'compare_capability_snapshot',
    'default_fake_devices',
    'default_fake_scenario',
    'derive_binding_state',
    'evaluate_identity_drift',
    'mark_ambiguity',
    'resolve_trusted_target',
]
