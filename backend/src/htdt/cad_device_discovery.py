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

import ipaddress
import select
import socket
import struct
import threading
import time
from typing import Any, Callable, Literal, Mapping, Protocol
from urllib.parse import urlsplit
from uuid import uuid4
from xml.etree import ElementTree

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


class DiscoveryTransportError(DiscoveryBackendUnavailableError):
    """The UDP transport failed mid-run — the environment cannot perform
    the exchange (firewall-blocked bind, missing multicast route, NIC
    down). Classified ``unavailable``, never ``failed`` — the backend
    itself did not break."""


class DiscoveryCancelledError(DiscoveryError):
    """The operator cancelled the discovery run before it finished."""


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
    'cancelled',
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
    #: The service type (mDNS PTR owner / SSDP ST·NT) that advertised
    #: this endpoint — observed, not inferred.
    service_type: str | None = None


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


# ---------------------------------------------------------------------------
# Multicast UDP transport — the only raw-network object in this module.
#
# One ``exchange`` sends each datagram once and collects ``(payload,
# source)`` pairs for a bounded window. It is injectable so the
# fail-closed surfaces (no multicast route, firewall-blocked bind,
# timeouts, cancellation) are testable without touching a LAN.


class MulticastQueryTransport(Protocol):
    """Bounded multicast query/response window for one (group, port) pair."""

    def check_availability(self) -> str | None:
        """``None`` when usable, else why this host cannot run it."""
        ...

    def exchange(
        self,
        packets: tuple[bytes, ...],
        *,
        listen_seconds: float,
        cancel_event: threading.Event | None = None,
    ) -> tuple[tuple[bytes, tuple[str, int]], ...]:
        """Send every packet once, then collect ``(payload, (host, port))``
        until the deadline, the cancel event fires or a datagram cap."""
        ...


class UdpMulticastTransport:
    """Real UDP multicast exchange over one IPv4 (group, port) pair.

    ``join_group=True`` binds the group port and joins the multicast
    group — mDNS style, where answers arrive multicast. ``False`` keeps
    an ephemeral port and hears unicast replies — SSDP M-SEARCH style.
    All waits are bounded; nothing here retries or re-transmits.
    """

    def __init__(
        self,
        *,
        group: str,
        group_port: int,
        join_group: bool,
        ttl: int,
        max_datagrams: int = 1024,
        socket_factory: Callable[..., Any] = socket.socket,
    ) -> None:
        self._group = group
        self._group_port = group_port
        self._join_group = join_group
        self._ttl = ttl
        self._max_datagrams = max_datagrams
        self._socket_factory = socket_factory

    def _open(self) -> Any:
        sock = self._socket_factory(
            socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP,
        )
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(
                socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, self._ttl,
            )
            if self._join_group:
                sock.bind(('', self._group_port))
                membership = (
                    socket.inet_aton(self._group)
                    + socket.inet_aton('0.0.0.0')
                )
                sock.setsockopt(
                    socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership,
                )
            else:
                sock.bind(('', 0))
        except Exception:
            sock.close()
            raise
        return sock

    def check_availability(self) -> str | None:
        try:
            sock = self._open()
        except OSError as exc:
            return (
                f'cannot open the multicast socket for '
                f'{self._group}:{self._group_port} ({exc}) — firewall, '
                'missing multicast route or disabled interface'
            )
        except Exception as exc:  # error-boundary: env probe
            return f'multicast transport probe failed ({exc})'
        try:
            sock.close()
        except Exception:
            pass
        return None

    def exchange(
        self,
        packets: tuple[bytes, ...],
        *,
        listen_seconds: float,
        cancel_event: threading.Event | None = None,
    ) -> tuple[tuple[bytes, tuple[str, int]], ...]:
        try:
            sock = self._open()
        except OSError as exc:
            raise DiscoveryTransportError(
                f'multicast transport unavailable at {self._group}:'
                f'{self._group_port} — {exc}'
            ) from exc
        received: list[tuple[bytes, tuple[str, int]]] = []
        try:
            for packet in packets:
                try:
                    sock.sendto(packet, (self._group, self._group_port))
                except OSError as exc:
                    raise DiscoveryTransportError(
                        f'multicast send to {self._group}:'
                        f'{self._group_port} failed — {exc}'
                    ) from exc
            deadline = time.monotonic() + max(listen_seconds, 0.0)
            while len(received) < self._max_datagrams:
                if cancel_event is not None and cancel_event.is_set():
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    readable, _, _ = select.select(
                        (sock,), (), (), remaining,
                    )
                except OSError as exc:
                    raise DiscoveryTransportError(
                        f'multicast listen failed — {exc}'
                    ) from exc
                if not readable:
                    break
                try:
                    payload, source = sock.recvfrom(65535)
                except OSError:
                    break
                received.append((payload, source))
        finally:
            try:
                sock.close()
            except Exception:
                pass
        return tuple(received)


# ---------------------------------------------------------------------------
# Scope admission for ambient (multicast) discovery.
#
# ``DiscoveryScanScope.allows_endpoint`` answers the approved-endpoint
# check; approved_networks adds a CIDR/address membership check for
# ambient enumeration, where the operator can approve a subnet without
# naming every address. A host name (not a literal IP) cannot prove
# network membership, so it is admitted only when no networks are
# configured — operators who care constrain by CIDR, not by name.


def _endpoint_host(endpoint: str) -> str:
    """Host part of an endpoint locator (``scheme://h:p`` or ``h:p``/``h``)."""
    rest = endpoint.split('://', 1)[-1].split('/', 1)[0]
    if rest.startswith('['):
        end = rest.find(']')
        return (rest[1:end] if end != -1 else rest).lower()
    if ':' in rest:
        return rest.rsplit(':', 1)[0].lower()
    return rest.lower()


def _approved_networks(
    scope: DiscoveryScanScope,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse ``approved_networks``; rejects bad entries before any packet
    leaves — scope is validated first, always."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in scope.approved_networks:
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError as exc:
            raise DiscoveryScopeError(
                f'approved_networks entry {entry!r} is not a valid '
                'network (CIDR or address expected)'
            ) from exc
    return tuple(networks)


def _host_in_networks(
    host: str,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    if not networks:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # A name cannot prove membership — fail closed, never guess.
        return False
    return any(address in network for network in networks)


def _ambient_endpoint_admitted(
    endpoint: str,
    scope: DiscoveryScanScope,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    host = _endpoint_host(endpoint)
    if scope.approved_endpoints:
        approved_hosts = {
            _endpoint_host(approved) for approved in scope.approved_endpoints
        }
        if host not in approved_hosts:
            return False
    return _host_in_networks(host, networks)


def _bracketed_host(host: str) -> str:
    return f'[{host}]' if ':' in host else host


# ---------------------------------------------------------------------------
# mDNS / DNS-SD wire format (RFC 6762 + RFC 6763) — read-only queries.
#
# Sent packets are legacy multicast DNS queries: a handful of PTR
# questions and nothing else. Parsed packets are responder answers;
# only PTR, SRV, TXT, A and AAAA records are consumed. Anything else —
# or anything malformed — is ignored and counted as a parse anomaly,
# never silently treated as an observation.


MDNS_GROUP = '224.0.0.251'
MDNS_PORT = 5353
DNS_SD_ENUMERATION_TYPE = '_services._dns-sd._udp.local'
_DNS_PTR = 12
_DNS_TXT = 16
_DNS_SRV = 33
_DNS_A = 1
_DNS_AAAA = 28
_DNS_CLASS_IN_FLUSH = 0x8001


def _dns_name_encode(name: str) -> bytes:
    out = bytearray()
    for label in name.rstrip('.').split('.'):
        encoded = label.encode('utf-8')
        if len(encoded) > 63:
            raise ValueError(f'dns label too long: {label!r}')
        out.append(len(encoded))
        out += encoded
    out.append(0)
    return bytes(out)


def _build_mdns_ptr_query(service_types: tuple[str, ...]) -> bytes:
    questions = b''.join(
        _dns_name_encode(service_type)
        + struct.pack('>HH', _DNS_PTR, _DNS_CLASS_IN_FLUSH)
        for service_type in service_types
    )
    header = struct.pack('>HHHHHH', 0, 0, len(service_types), 0, 0, 0)
    return header + questions


def _dns_name_decode(packet: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    jumped = False
    next_offset = offset
    jumps = 0
    while True:
        if offset >= len(packet):
            raise ValueError('truncated dns name')
        length = packet[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(packet):
                raise ValueError('truncated compression pointer')
            pointer = ((length & 0x3F) << 8) | packet[offset + 1]
            if pointer >= len(packet):
                raise ValueError('compression pointer out of range')
            if not jumped:
                next_offset = offset + 2
            jumped = True
            jumps += 1
            if jumps > 32:
                raise ValueError('compression pointer loop')
            offset = pointer
            continue
        if length & 0xC0:
            raise ValueError('reserved dns label bits set')
        offset += 1
        if length == 0:
            if not jumped:
                next_offset = offset
            break
        if offset + length > len(packet):
            raise ValueError('truncated dns label')
        labels.append(packet[offset:offset + length].decode('utf-8', 'replace'))
        offset += length
    return '.'.join(labels), next_offset


def _iter_dns_rrs(packet: bytes):
    """Yield ``(owner, rtype, ttl, rdata_offset, rdlen)`` for every RR."""
    if len(packet) < 12:
        raise ValueError('short dns header')
    _id, _flags, qd, an, ns, ar = struct.unpack('>HHHHHH', packet[:12])
    offset = 12
    for _ in range(qd):
        _, offset = _dns_name_decode(packet, offset)
        offset += 4
    if offset > len(packet):
        raise ValueError('truncated question section')
    for _ in range(an + ns + ar):
        owner, offset = _dns_name_decode(packet, offset)
        if offset + 10 > len(packet):
            raise ValueError('truncated resource record header')
        rtype, _rclass, ttl, rdlen = struct.unpack(
            '>HHIH', packet[offset:offset + 10],
        )
        offset += 10
        if offset + rdlen > len(packet):
            raise ValueError('truncated resource record data')
        yield owner, rtype, ttl, offset, rdlen
        offset += rdlen


def _dns_txt_map(packet: bytes, offset: int, rdlen: int) -> dict[str, str]:
    entries: dict[str, str] = {}
    end = offset + rdlen
    while offset < end:
        length = packet[offset]
        offset += 1
        if offset + length > end:
            break
        chunk = packet[offset:offset + length].decode('utf-8', 'replace')
        offset += length
        key, sep, value = chunk.partition('=')
        if sep and key.lower() not in entries:
            entries[key.lower()] = value
    return entries


#: DNS-SD TXT keys commonly documented by device vendors. Anything not
#: listed is preserved nowhere — identity fields are observed claims,
#: never inferred ones.
_MDNS_TXT_KEYS = {
    'manufacturer': ('manufacturer', 'mf', 'mfr', 'brand'),
    'model': ('model', 'md', 'modelname', 'modelid'),
    'stable_identity': (
        'serialnumber', 'serial', 'sn', 'mac', 'uid', 'uuid', 'id',
        'deviceid', 'pk', 'hw',
    ),
    'firmware_version': ('firmwareversion', 'firmware', 'fwversion', 'fw', 'fv'),
    'software_version': (
        'swversion', 'sw', 'softwareversion', 'version', 'vers', 'srcvers',
    ),
    'capabilities': ('features', 'caps', 'capabilities', 'ft', 'flags'),
}


def _first_txt(txt: Mapping[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = txt.get(key)
        if value:
            return value
    return None


def _mdns_instance_label(instance: str, service_type: str) -> str | None:
    suffix = f'.{service_type}'
    if instance.lower().endswith(suffix.lower()):
        label = instance[: -len(suffix)]
        return label or None
    return None


class _MdnsRecords:
    """Aggregated resource records across one discovery's responses."""

    def __init__(self) -> None:
        self.ptrs: list[tuple[str, str]] = []
        self.srvs: dict[str, tuple[int, str]] = {}
        self.txts: dict[str, dict[str, str]] = {}
        self.addrs: dict[str, list[str]] = {}
        self.malformed = 0

    def ingest(self, payload: bytes) -> None:
        try:
            records = list(_iter_dns_rrs(payload))
        except ValueError:
            self.malformed += 1
            return
        for owner, rtype, _ttl, rdata_offset, rdlen in records:
            try:
                if rtype == _DNS_PTR:
                    target, _ = _dns_name_decode(payload, rdata_offset)
                    self.ptrs.append((owner, target))
                elif rtype == _DNS_SRV:
                    port = struct.unpack(
                        '>H', payload[rdata_offset + 4:rdata_offset + 6],
                    )[0]
                    target, _ = _dns_name_decode(payload, rdata_offset + 6)
                    self.srvs[owner] = (port, target)
                elif rtype == _DNS_TXT:
                    self.txts[owner] = _dns_txt_map(
                        payload, rdata_offset, rdlen,
                    )
                elif rtype == _DNS_A and rdlen == 4:
                    self.addrs.setdefault(owner, []).append(
                        socket.inet_ntoa(payload[rdata_offset:rdata_offset + 4]),
                    )
                elif rtype == _DNS_AAAA and rdlen == 16:
                    self.addrs.setdefault(owner, []).append(
                        socket.inet_ntop(
                            socket.AF_INET6,
                            payload[rdata_offset:rdata_offset + 16],
                        ),
                    )
            except (ValueError, IndexError, struct.error, OSError):
                self.malformed += 1


# ---------------------------------------------------------------------------
# SSDP / UPnP wire format — M-SEARCH queries plus notification adverts.


SSDP_GROUP = '239.255.255.250'
SSDP_PORT = 1900


def _build_ssdp_msearch(st: str, mx_seconds: int) -> bytes:
    return (
        'M-SEARCH * HTTP/1.1\r\n'
        f'HOST:{SSDP_GROUP}:{SSDP_PORT}\r\n'
        'MAN:"ssdp:discover"\r\n'
        f'MX:{mx_seconds}\r\n'
        f'ST:{st}\r\n'
        '\r\n'
    ).encode('ascii')


def _parse_ssdp_datagram(data: bytes) -> tuple[dict[str, str], str | None]:
    """Parse one SSDP datagram → ``(headers, st_or_nt)``.

    Raises ``ValueError`` on anything that is not a response or NOTIFY.
    """
    text = data.decode('utf-8', 'replace')
    lines = text.split('\r\n')
    start = lines[0].strip() if lines else ''
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            break
        key, sep, value = line.partition(':')
        if sep:
            headers[key.strip().lower()] = value.strip()
    if '200 ok' in start.lower():
        return headers, headers.get('st')
    if start.upper().startswith('NOTIFY'):
        return headers, headers.get('nt')
    raise ValueError('not an ssdp response/notify datagram')


def _bounded_http_get(
    url: str,
    timeout_seconds: float,
    *,
    max_bytes: int = 65536,
) -> bytes | None:
    """Fetch an http URL with strict caps — used for UPnP description
    documents only. Returns ``None`` on any failure (a missing document
    is a partial observation, never a crash)."""
    parts = urlsplit(url)
    if parts.scheme != 'http' or not parts.hostname:
        return None
    port = parts.port or 80
    path = parts.path or '/'
    if parts.query:
        path = f'{path}?{parts.query}'
    sock: Any = None
    try:
        sock = socket.create_connection((parts.hostname, port), timeout_seconds)
        sock.settimeout(timeout_seconds)
        request = (
            f'GET {path} HTTP/1.0\r\n'
            f'Host: {parts.hostname}:{port}\r\n'
            'Accept: text/xml\r\n'
            '\r\n'
        ).encode('ascii')
        sock.sendall(request)
        chunks = bytearray()
        while len(chunks) < max_bytes:
            try:
                chunk = sock.recv(min(65536, max_bytes - len(chunks)))
            except socket.timeout:
                break
            if not chunk:
                break
            chunks += chunk
    except OSError:
        return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    if not chunks:
        return None
    head, sep, body = bytes(chunks).partition(b'\r\n\r\n')
    if not sep:
        return None
    status_line = head.split(b'\r\n', 1)[0].decode('ascii', 'replace')
    status_parts = status_line.split(' ', 2)
    if len(status_parts) < 2 or status_parts[1] != '200':
        return None
    return bytes(body[:max_bytes])


def _xml_local_name(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]


def _parse_upnp_description(document: bytes) -> dict[str, Any]:
    """Extract declared fields from a UPnP device description.

    All fields are declared-by-device metadata — they are stored as
    observed-vs-declared evidence, never trusted as ground truth.
    """
    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError:
        return {}
    device: ElementTree.Element | None = None
    for element in root.iter():
        if _xml_local_name(element.tag) == 'device':
            device = element
            break
    if device is None:
        return {}
    fields: dict[str, Any] = {'service_types': []}
    name_map = {
        'friendlyName': 'device_name',
        'manufacturer': 'manufacturer',
        'modelName': 'model',
        'serialNumber': 'serial_number',
        'UDN': 'udn',
    }
    for child in device.iter():
        local = _xml_local_name(child.tag)
        target = name_map.get(local)
        if target is not None and child.text and child.text.strip():
            fields[target] = child.text.strip()
        elif local == 'serviceType' and child.text and child.text.strip():
            fields['service_types'].append(child.text.strip())
    return fields


def _ssdp_usn_uuid(usn: str) -> str | None:
    if not usn.lower().startswith('uuid:'):
        return None
    return usn.split('::', 1)[0][len('uuid:'):].strip() or None


class MdnsDiscoveryBackend(DiscoveryBackend):
    """mDNS/Bonjour service discovery — real multicast, read-only.

    Sends PTR queries for the operator-approved service types and parses
    answers strictly from the wire. When ``approved_service_types`` is
    empty the run enumerates service types first (the DNS-SD
    ``_services._dns-sd._udp.local`` PTR) then queries each discovered
    type — still bounded by ``max_service_types`` and by the approved
    endpoint/network admission filters, so it never degenerates into an
    unrestricted sweep. Identity fields are TXT-advertised claims only.
    """

    backend_id = 'htdt-mdns'
    backend_version = '1'
    mechanism: DiscoveryMechanism = 'mdns'

    def __init__(
        self,
        transport: MulticastQueryTransport | None = None,
        *,
        query_seconds: float = 3.0,
        followup_seconds: float = 2.0,
        max_service_types: int = 32,
    ) -> None:
        self._transport = transport or UdpMulticastTransport(
            group=MDNS_GROUP, group_port=MDNS_PORT, join_group=True, ttl=255,
        )
        self._query_seconds = query_seconds
        self._followup_seconds = followup_seconds
        self._max_service_types = max_service_types
        self._run_notes: list[str] = []

    def available(self) -> bool:
        return self._transport.check_availability() is None

    def unavailable_reason(self) -> str:
        reason = self._transport.check_availability()
        return reason or 'mdns/bonjour transport is unavailable'

    def last_run_notes(self) -> tuple[str, ...]:
        """Parse anomalies and admission drops from the latest run —
        the operator-visible 'what was filtered and why' trail."""
        return tuple(self._run_notes)

    def _check_cancel(
        self, cancel_event: threading.Event | None,
    ) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise DiscoveryCancelledError(
                'operator cancelled the discovery run'
            )

    def _query_round(
        self,
        service_types: tuple[str, ...],
        listen_seconds: float,
        cancel_event: threading.Event | None,
        records: _MdnsRecords,
    ) -> None:
        self._check_cancel(cancel_event)
        query = _build_mdns_ptr_query(service_types)
        responses = self._transport.exchange(
            (query,),
            listen_seconds=listen_seconds,
            cancel_event=cancel_event,
        )
        for payload, _source in responses:
            records.ingest(payload)
        self._check_cancel(cancel_event)

    def _collect_records(
        self,
        scope: DiscoveryScanScope,
        cancel_event: threading.Event | None,
    ) -> tuple[_MdnsRecords, frozenset[str]]:
        approved_types = frozenset(
            service_type.lower()
            for service_type in scope.approved_service_types
        )
        if len(approved_types) > self._max_service_types:
            raise DiscoveryScopeError(
                f'approved service type count {len(approved_types)} '
                f'exceeds bound {self._max_service_types}'
            )
        records = _MdnsRecords()
        if approved_types:
            self._query_round(
                tuple(sorted(approved_types)),
                self._query_seconds,
                cancel_event,
                records,
            )
            return records, approved_types
        # Bounded enumeration: ask which service types exist, then ask
        # for instances of each type found.
        self._query_round(
            (DNS_SD_ENUMERATION_TYPE,), self._query_seconds, cancel_event,
            records,
        )
        enumerated = sorted(
            {target.lower() for owner, target in records.ptrs
             if owner.lower() == DNS_SD_ENUMERATION_TYPE}
        )
        queried = tuple(enumerated[: self._max_service_types])
        if len(enumerated) > self._max_service_types:
            self._run_notes.append(
                f'service-type enumeration hit the {self._max_service_types}'
                '-type bound; remaining types were not queried'
            )
        if queried:
            self._query_round(
                queried, self._followup_seconds, cancel_event, records,
            )
        return records, frozenset(enumerated)

    def _observations(
        self,
        records: _MdnsRecords,
        approved_types: frozenset[str],
        scope: DiscoveryScanScope,
        networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    ) -> tuple[DiscoveryObservation, ...]:
        observations: list[DiscoveryObservation] = []
        seen_endpoints: set[str] = set()
        dropped_no_locator = 0
        dropped_out_of_scope = 0
        for owner, target in records.ptrs:
            service_type = owner.lower()
            if service_type == DNS_SD_ENUMERATION_TYPE:
                continue
            if approved_types and service_type not in approved_types:
                continue
            srv = records.srvs.get(target)
            if srv is None:
                dropped_no_locator += 1
                continue
            port, host_target = srv
            candidates = records.addrs.get(host_target) or (host_target,)
            txt = records.txts.get(target) or {}
            device_name = _mdns_instance_label(target, owner)
            capability_text = _first_txt(txt, _MDNS_TXT_KEYS['capabilities'])
            for host in candidates:
                endpoint = f'mdns://{_bracketed_host(host)}:{port}'
                if endpoint in seen_endpoints:
                    continue
                seen_endpoints.add(endpoint)
                if not _ambient_endpoint_admitted(endpoint, scope, networks):
                    dropped_out_of_scope += 1
                    continue
                observations.append(DiscoveryObservation(
                    endpoint=endpoint,
                    manufacturer=_first_txt(txt, _MDNS_TXT_KEYS['manufacturer']),
                    model=_first_txt(txt, _MDNS_TXT_KEYS['model']),
                    device_name=device_name,
                    stable_identity=_first_txt(
                        txt, _MDNS_TXT_KEYS['stable_identity'],
                    ),
                    firmware_version=_first_txt(
                        txt, _MDNS_TXT_KEYS['firmware_version'],
                    ),
                    software_version=_first_txt(
                        txt, _MDNS_TXT_KEYS['software_version'],
                    ),
                    advertised_capabilities=tuple(
                        part.strip()
                        for part in (capability_text or '').split(',')
                        if part.strip()
                    ),
                    service_type=service_type,
                ))
        if records.malformed:
            self._run_notes.append(
                f'{records.malformed} malformed dns record(s) ignored'
            )
        if dropped_no_locator:
            self._run_notes.append(
                f'{dropped_no_locator} instance(s) advertised without an '
                'srv locator — not contactable, not recorded'
            )
        if dropped_out_of_scope:
            self._run_notes.append(
                f'{dropped_out_of_scope} advertised endpoint(s) outside '
                'the approved scope — dropped'
            )
        return tuple(observations)

    def discover(
        self,
        scope: DiscoveryScanScope,
        *,
        cancel_event: threading.Event | None = None,
    ) -> tuple[DiscoveryObservation, ...]:
        self._run_notes = []
        networks = _approved_networks(scope)
        self._check_cancel(cancel_event)
        down = self._transport.check_availability()
        if down is not None:
            raise DiscoveryTransportError(down)
        records, admitted_types = self._collect_records(scope, cancel_event)
        return self._observations(records, admitted_types, scope, networks)

    def probe_identity(
        self,
        endpoint: str,
        scope: DiscoveryScanScope,
        *,
        cancel_event: threading.Event | None = None,
    ) -> DiscoveryObservation | None:
        networks = _approved_networks(scope)
        if (
            scope.approved_endpoints or scope.approved_networks
        ) and not _ambient_endpoint_admitted(endpoint, scope, networks):
            raise DiscoveryScopeError(
                f'endpoint {endpoint} is outside the approved scope'
            )
        self._run_notes = []
        records, admitted_types = self._collect_records(scope, cancel_event)
        observations = self._observations(
            records, admitted_types, scope, networks,
        )
        for observation in observations:
            if observation.endpoint == endpoint:
                return observation
        return None


class SsdpDiscoveryBackend(DiscoveryBackend):
    """SSDP/UPnP discovery — real M-SEARCH, read-only.

    Sends one M-SEARCH per approved ST (default ``ssdp:all`` when the
    operator approved no service types — still bounded by the
    endpoint/network admission filters and the bounded listen window).
    LOCATION headers pointing off the responder's own host are not
    followed; a description document that fails to fetch or parse
    degrades the observation to ``partial``/``unidentified`` rather
    than dropping or fabricating it.
    """

    backend_id = 'htdt-ssdp'
    backend_version = '1'
    mechanism: DiscoveryMechanism = 'ssdp'

    def __init__(
        self,
        transport: MulticastQueryTransport | None = None,
        *,
        description_fetcher: Callable[..., bytes | None] | None = None,
        mx_seconds: int = 2,
        fetch_timeout_seconds: float = 2.0,
        max_service_types: int = 32,
    ) -> None:
        self._transport = transport or UdpMulticastTransport(
            group=SSDP_GROUP, group_port=SSDP_PORT, join_group=False, ttl=4,
        )
        self._description_fetcher = (
            description_fetcher or _bounded_http_get
        )
        self._mx_seconds = mx_seconds
        self._fetch_timeout_seconds = fetch_timeout_seconds
        self._max_service_types = max_service_types
        self._run_notes: list[str] = []

    def available(self) -> bool:
        return self._transport.check_availability() is None

    def unavailable_reason(self) -> str:
        reason = self._transport.check_availability()
        return reason or 'ssdp/upnp transport is unavailable'

    def last_run_notes(self) -> tuple[str, ...]:
        return tuple(self._run_notes)

    def _check_cancel(
        self, cancel_event: threading.Event | None,
    ) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise DiscoveryCancelledError(
                'operator cancelled the discovery run'
            )

    def _collect(
        self,
        scope: DiscoveryScanScope,
        cancel_event: threading.Event | None,
        networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    ) -> tuple[DiscoveryObservation, ...]:
        approved_types = frozenset(
            service_type.lower()
            for service_type in scope.approved_service_types
        )
        if len(approved_types) > self._max_service_types:
            raise DiscoveryScopeError(
                f'approved service type count {len(approved_types)} '
                f'exceeds bound {self._max_service_types}'
            )
        sts = (
            tuple(sorted(approved_types))
            if approved_types else ('ssdp:all',)
        )
        self._check_cancel(cancel_event)
        packets = tuple(
            _build_ssdp_msearch(st, self._mx_seconds) for st in sts
        )
        responses = self._transport.exchange(
            packets,
            listen_seconds=float(self._mx_seconds) + 1.0,
            cancel_event=cancel_event,
        )
        self._check_cancel(cancel_event)
        adverts: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        malformed = 0
        dropped_type = 0
        dropped_scope = 0
        duplicates = 0
        for payload, source in responses:
            try:
                headers, st = _parse_ssdp_datagram(payload)
            except ValueError:
                malformed += 1
                continue
            location = headers.get('location')
            if not location:
                malformed += 1
                continue
            if approved_types and (st or '').lower() not in approved_types:
                dropped_type += 1
                continue
            if not _ambient_endpoint_admitted(location, scope, networks):
                dropped_scope += 1
                continue
            key = (headers.get('usn') or '', location)
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            adverts.append({
                'headers': headers,
                'st': st,
                'location': location,
                'source_host': source[0],
            })
        observations: list[DiscoveryObservation] = []
        unfetched = 0
        off_host = 0
        for advert in adverts:
            location = advert['location']
            description: dict[str, Any] = {}
            if _endpoint_host(location) != advert['source_host'].lower():
                off_host += 1
            else:
                document = self._description_fetcher(
                    location, self._fetch_timeout_seconds,
                )
                if document is None:
                    unfetched += 1
                else:
                    description = _parse_upnp_description(document)
            headers = advert['headers']
            usn_uuid = _ssdp_usn_uuid(headers.get('usn') or '')
            udn = description.get('udn')
            stable_identity: str | None = None
            if usn_uuid and udn:
                udn_uuid = _ssdp_usn_uuid(udn)
                if udn_uuid == usn_uuid:
                    stable_identity = usn_uuid
                else:
                    # Conflicting declared identifiers — record neither.
                    self._run_notes.append(
                        f'usn/udn mismatch at {location} — stable '
                        'identity left unrecorded'
                    )
            elif usn_uuid:
                stable_identity = usn_uuid
            observations.append(DiscoveryObservation(
                endpoint=location,
                manufacturer=description.get('manufacturer'),
                model=description.get('model'),
                device_name=description.get('device_name'),
                stable_identity=stable_identity,
                software_version=headers.get('server'),
                advertised_capabilities=tuple(
                    dict.fromkeys(description.get('service_types') or ())
                ),
                service_type=advert['st'] or None,
            ))
        if malformed:
            self._run_notes.append(
                f'{malformed} malformed ssdp datagram(s) ignored'
            )
        if dropped_type:
            self._run_notes.append(
                f'{dropped_type} advert(s) for non-approved service '
                'types dropped'
            )
        if dropped_scope:
            self._run_notes.append(
                f'{dropped_scope} advert(s) outside the approved scope '
                'dropped'
            )
        if duplicates:
            self._run_notes.append(
                f'{duplicates} duplicate advert(s) collapsed'
            )
        if off_host:
            self._run_notes.append(
                f'{off_host} description url(s) pointing off the '
                'responder host were not fetched'
            )
        if unfetched:
            self._run_notes.append(
                f'{unfetched} device description(s) could not be '
                'fetched — observations degrade to partial/unidentified'
            )
        return tuple(observations)

    def discover(
        self,
        scope: DiscoveryScanScope,
        *,
        cancel_event: threading.Event | None = None,
    ) -> tuple[DiscoveryObservation, ...]:
        self._run_notes = []
        networks = _approved_networks(scope)
        self._check_cancel(cancel_event)
        down = self._transport.check_availability()
        if down is not None:
            raise DiscoveryTransportError(down)
        return self._collect(scope, cancel_event, networks)

    def probe_identity(
        self,
        endpoint: str,
        scope: DiscoveryScanScope,
        *,
        cancel_event: threading.Event | None = None,
    ) -> DiscoveryObservation | None:
        networks = _approved_networks(scope)
        if (
            scope.approved_endpoints or scope.approved_networks
        ) and not _ambient_endpoint_admitted(endpoint, scope, networks):
            raise DiscoveryScopeError(
                f'endpoint {endpoint} is outside the approved scope'
            )
        self._run_notes = []
        for observation in self._collect(scope, cancel_event, networks):
            if observation.endpoint == endpoint:
                return observation
        return None


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
            except DiscoveryCancelledError as exc:
                outcome = 'cancelled'
                failure_reason = str(exc)
            except DiscoveryBackendUnavailableError as exc:
                outcome = 'unavailable'
                failure_reason = str(exc)
            except Exception as exc:  # error-boundary: backend read
                outcome = 'failed'
                failure_reason = str(exc)
        notes: tuple[str, ...] = ()
        notes_provider = getattr(backend, 'last_run_notes', None)
        if callable(notes_provider):
            try:
                notes = tuple(notes_provider())
            except Exception:  # error-boundary: notes are best-effort
                notes = ()
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
            notes=notes,
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
        notes: tuple[str, ...] = (),
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
            'notes': tuple(notes),
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
        except (
            DiscoveryBackendUnavailableError,
            DiscoveryCancelledError,
        ):
            # A backend that could not run yields no verdict at all —
            # never record 'endpoint_unreachable' drift the transport
            # could not actually observe.
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
    'cancelled': 'キャンセル',
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
    'DiscoveryCancelledError',
    'DiscoveryError',
    'DiscoveryMechanism',
    'DiscoveryObservation',
    'DiscoveryRunOutcome',
    'DiscoveryRunRecord',
    'DiscoveryScanScope',
    'DiscoveryTransportError',
    'DiscoveredDeviceRecord',
    'DriftKind',
    'DriftRecommendation',
    'DriftVerdict',
    'DNS_SD_ENUMERATION_TYPE',
    'EndpointProber',
    'FakeCapabilityProber',
    'FakeDiscoveryBackend',
    'FakeDiscoveryDevice',
    'FakeDiscoveryScenario',
    'IdentityBasis',
    'MDNS_GROUP',
    'MDNS_PORT',
    'MdnsDiscoveryBackend',
    'MulticastQueryTransport',
    'RebindingAction',
    'RebindingDecision',
    'ResolvedTrustedTarget',
    'SSDP_GROUP',
    'SSDP_PORT',
    'SsdpDiscoveryBackend',
    'StaleBindingError',
    'StaticEndpointProber',
    'TrustedBindingState',
    'TrustedDeviceBinding',
    'UdpMulticastTransport',
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
