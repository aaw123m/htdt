"""#949: real mDNS/SSDP discovery backends — the #879 seam fed by wire.

Fake UDP event injection covers: out-of-scope rejection (endpoints,
networks, service types), duplicate adverts, tampered adverts (malformed
datagrams, USN/UDN mismatch, off-host description URLs), timeouts,
identity drift on rediscovery, operator cancel, and rescan. The
transport seam is injectable precisely so the fail-closed surfaces
(firewall, missing route, disabled multicast) are verifiable without a
LAN — a real-environment pass remains a #879 physical gate.
"""

from __future__ import annotations

import socket
import struct
import threading

import pytest

from htdt.cad_device_discovery import (
    DNS_SD_ENUMERATION_TYPE,
    DeviceDiscoveryService,
    DiscoveryBackendUnavailableError,
    DiscoveryCancelledError,
    DiscoveryScopeError,
    DiscoveryTransportError,
    FakeCapabilityProber,
    MdnsDiscoveryBackend,
    SsdpDiscoveryBackend,
    UdpMulticastTransport,
    build_scan_scope,
)
from htdt.cad_device_adapter import AdapterCapabilityReport
from htdt.cad_device_discovery import CapabilityProbeResult

NOW = '2026-10-08T00:00:00+00:00'
LATER = '2026-10-08T01:00:00+00:00'
DOC = 'doc-949'

HOST_A = '192.168.9.5'
HOST_B = '192.168.9.6'
HOST_OUTSIDE = '10.7.7.7'
TYPE_HTDT = '_htdt._tcp.local'
TYPE_RAOP = '_raop._tcp.local'
ST_MEDIA = 'urn:schemas-upnp-org:device:MediaRenderer:1'


def _scope(**kw: object):
    return build_scan_scope(approved_by='operator-1', approved_at_utc=NOW, **kw)


# -- wire builders -----------------------------------------------------------


def _dns_name(name: str) -> bytes:
    out = bytearray()
    for label in name.rstrip('.').split('.'):
        out.append(len(label))
        out += label.encode('utf-8')
    out.append(0)
    return bytes(out)


def _rr(owner: bytes, rtype: int, rdata: bytes, ttl: int = 120) -> bytes:
    return owner + struct.pack('>HHIH', rtype, 0x8001, ttl, len(rdata)) + rdata


def _mdns_packet(
    ptrs: tuple[tuple[str, str], ...] = (),
    srvs: tuple[tuple[str, int, str], ...] = (),
    txts: tuple[tuple[str, tuple[str, ...]], ...] = (),
    addrs: tuple[tuple[str, str], ...] = (),
) -> bytes:
    """Answers-only response packet (questionless — responders often
    send bare announcements)."""
    count = len(ptrs) + len(srvs) + len(txts) + len(addrs)
    body = b''
    for owner, target in ptrs:
        body += _rr(_dns_name(owner), 12, _dns_name(target))
    for owner, port, target in srvs:
        body += _rr(
            _dns_name(owner), 33,
            struct.pack('>HHH', 0, 0, port) + _dns_name(target),
        )
    for owner, entries in txts:
        rdata = b''.join(bytes([len(e)]) + e.encode() for e in entries)
        body += _rr(_dns_name(owner), 16, rdata)
    for owner, ip in addrs:
        body += _rr(_dns_name(owner), 1, socket.inet_aton(ip), ttl=120)
    return struct.pack('>HHHHHH', 0, 0x8400, 0, count, 0, 0) + body


def _mdns_full_response(
    *,
    instance: str,
    service_type: str,
    ip: str,
    port: int,
    txt: tuple[str, ...] = (),
) -> bytes:
    host = f'{instance.split(".", 1)[0]}.host.local'
    return _mdns_packet(
        ptrs=((service_type, instance),),
        srvs=((instance, port, host),),
        txts=((instance, txt),),
        addrs=((host, ip),),
    )


def _ssdp_response(
    ip: str,
    usn: str,
    *,
    st: str = ST_MEDIA,
    location: str | None = None,
    server: str = 'TestOS/1.0 UPnP/1.0 Box/2.0',
) -> tuple[bytes, tuple[str, int]]:
    loc = location or f'http://{ip}:49152/device.xml'
    payload = (
        'HTTP/1.1 200 OK\r\n'
        'CACHE-CONTROL:max-age=120\r\n'
        f'LOCATION:{loc}\r\n'
        f'ST:{st}\r\n'
        f'USN:{usn}\r\n'
        f'SERVER:{server}\r\n'
        '\r\n'
    ).encode('ascii')
    return payload, (ip, 49150)


def _ssdp_notify(
    ip: str, usn: str, *, nt: str, location: str,
) -> tuple[bytes, tuple[str, int]]:
    payload = (
        'NOTIFY * HTTP/1.1\r\n'
        f'HOST:239.255.255.250:1900\r\n'
        f'NT:{nt}\r\n'
        'NTS:ssdp:alive\r\n'
        f'LOCATION:{location}\r\n'
        f'USN:{usn}\r\n'
        '\r\n'
    ).encode('ascii')
    return payload, (ip, 49150)


def _upnp_doc(
    *,
    friendly: str = 'Living Room AVR',
    manufacturer: str = 'Denon',
    model: str = 'AVR-X3800H',
    serial: str = 'SN-778',
    udn: str = 'uuid:device-1',
    services: tuple[str, ...] = ('urn:schemas-upnp-org:service:AVTransport:1',),
) -> bytes:
    service_xml = ''.join(
        f'<service><serviceType>{s}</serviceType></service>' for s in services
    )
    return (
        '<?xml version="1.0"?>'
        '<root xmlns="urn:schemas-upnp-org:device-1-0"><device>'
        f'<friendlyName>{friendly}</friendlyName>'
        f'<manufacturer>{manufacturer}</manufacturer>'
        f'<modelName>{model}</modelName>'
        f'<serialNumber>{serial}</serialNumber>'
        f'<UDN>{udn}</UDN>'
        f'<serviceList>{service_xml}</serviceList>'
        '</device></root>'
    ).encode('utf-8')


# -- fakes -------------------------------------------------------------------


class FakeTransport:
    """Injectable MulticastQueryTransport serving queued rounds.

    ``rounds`` are consumed in order — each ``exchange`` call pops one;
    once exhausted the transport answers nothing (the quiet-LAN case).
    """

    def __init__(
        self,
        rounds: tuple[tuple[tuple[bytes, tuple[str, int]], ...], ...] = (),
        *,
        available_reason: str | None = None,
        raise_on_exchange: Exception | None = None,
    ) -> None:
        self._rounds = [tuple(r) for r in rounds]
        self._reason = available_reason
        self._raise = raise_on_exchange
        self.calls = 0
        self.sent: list[tuple[bytes, ...]] = []

    def check_availability(self) -> str | None:
        return self._reason

    def exchange(
        self,
        packets: tuple[bytes, ...],
        *,
        listen_seconds: float,
        cancel_event: threading.Event | None = None,
    ) -> tuple[tuple[bytes, tuple[str, int]], ...]:
        self.calls += 1
        self.sent.append(tuple(packets))
        if self._raise is not None:
            raise self._raise
        if not self._rounds:
            return ()
        return self._rounds.pop(0)


class RecordingFetcher:
    """UPnP description fetcher that records its URLs."""

    def __init__(self, documents: dict[str, bytes | None] | None = None) -> None:
        self.documents = documents or {}
        self.requested: list[str] = []

    def __call__(self, url: str, timeout_seconds: float) -> bytes | None:
        self.requested.append(url)
        return self.documents.get(url)


def _capability_report() -> AdapterCapabilityReport:
    return AdapterCapabilityReport(
        adapter_id='htdt-avr-lan',
        adapter_version='1',
        adapter_kind='network_api',
        device_family='avr',
        supports_apply=True,
        supports_read_back=True,
        supports_materialization=True,
        deploy_mechanism='machine_write',
        readback_mechanism='machine_exact',
        rollback_mechanism='previous_config',
        runtime_observation='telemetry',
        supported_features=('gain',),
        limit_notes=(),
        auth_requirements=('approved_remote_endpoint',),
        applicability='avr lan targets',
        protocol_authority='documented',
        notes=(),
    )


def _probe_result() -> CapabilityProbeResult:
    return CapabilityProbeResult(
        report=_capability_report(), firmware_version='1.58.0',
    )


# -- mDNS --------------------------------------------------------------------


class TestMdnsWire:
    def test_full_observation_from_wire(self) -> None:
        transport = FakeTransport(rounds=((
            (_mdns_full_response(
                instance='Living Room AVR._htdt._tcp.local',
                service_type=TYPE_HTDT,
                ip=HOST_A, port=8009,
                txt=(
                    'manufacturer=Denon', 'model=AVR-X3800H',
                    'serialnumber=SN-778', 'firmware=1.58.0',
                    'swversion=9.9', 'features=gain,bass',
                ),
            ), (HOST_A, 5353)),
        ),))
        backend = MdnsDiscoveryBackend(transport=transport)
        obs = backend.discover(_scope(approved_service_types=(TYPE_HTDT,)))
        assert len(obs) == 1
        o = obs[0]
        assert o.endpoint == f'mdns://{HOST_A}:8009'
        assert o.manufacturer == 'Denon'
        assert o.model == 'AVR-X3800H'
        assert o.device_name == 'Living Room AVR'
        assert o.stable_identity == 'SN-778'
        assert o.firmware_version == '1.58.0'
        assert o.software_version == '9.9'
        assert o.advertised_capabilities == ('gain', 'bass')
        assert o.service_type == TYPE_HTDT
        assert backend.backend_is_simulated is False
        # the query went out once, carrying the approved PTR question
        assert len(transport.sent) == 1
        assert _dns_name(TYPE_HTDT) in transport.sent[0][0]

    def test_enumeration_then_typed_query(self) -> None:
        # round 1: enumeration answer advertising one service type
        # round 2: the instance answer
        transport = FakeTransport(rounds=(
            ((_mdns_packet(ptrs=(
                (DNS_SD_ENUMERATION_TYPE, '_raop._tcp.local'),
                (DNS_SD_ENUMERATION_TYPE, TYPE_HTDT),
            )), (HOST_A, 5353)),),
            ((_mdns_full_response(
                instance='Amp._htdt._tcp.local', service_type=TYPE_HTDT,
                ip=HOST_A, port=8009,
            ), (HOST_A, 5353)),),
        ))
        backend = MdnsDiscoveryBackend(transport=transport)
        obs = backend.discover(_scope(approved_networks=('192.168.9.0/24',)))
        assert len(obs) == 1
        assert obs[0].endpoint == f'mdns://{HOST_A}:8009'
        # two exchanges: enumeration + typed follow-up
        assert transport.calls == 2
        assert _dns_name(DNS_SD_ENUMERATION_TYPE) in transport.sent[0][0]
        assert _dns_name(TYPE_HTDT) in transport.sent[1][0]
        assert _dns_name('_raop._tcp.local') in transport.sent[1][0]

    def test_enumeration_capped_by_max_service_types(self) -> None:
        many = tuple(
            (DNS_SD_ENUMERATION_TYPE, f'_svc{i}._tcp.local')
            for i in range(5)
        )
        transport = FakeTransport(rounds=(
            ((_mdns_packet(ptrs=many), (HOST_A, 5353)),),
            (),
        ))
        backend = MdnsDiscoveryBackend(transport=transport, max_service_types=2)
        backend.discover(_scope(approved_networks=('192.168.9.0/24',)))
        assert transport.calls == 2
        # exactly 2 questions in the follow-up query
        sent = transport.sent[1][0]
        qdcount = struct.unpack('>H', sent[4:6])[0]
        assert qdcount == 2
        assert any('bound' in n for n in backend.last_run_notes())

    def test_out_of_scope_endpoint_dropped(self) -> None:
        transport = FakeTransport(rounds=((
            (_mdns_full_response(
                instance='Rogue._htdt._tcp.local', service_type=TYPE_HTDT,
                ip=HOST_OUTSIDE, port=8009,
            ), (HOST_OUTSIDE, 5353)),
        ),))
        backend = MdnsDiscoveryBackend(transport=transport)
        obs = backend.discover(_scope(
            approved_service_types=(TYPE_HTDT,),
            approved_endpoints=(f'mdns://{HOST_A}:8009',),
        ))
        assert obs == ()
        assert any('outside the approved scope' in n
                   for n in backend.last_run_notes())

    def test_network_cidr_admission(self) -> None:
        transport = FakeTransport(rounds=((
            (_mdns_full_response(
                instance='In._htdt._tcp.local', service_type=TYPE_HTDT,
                ip=HOST_A, port=8009,
            ), (HOST_A, 5353)),
            (_mdns_full_response(
                instance='Out._htdt._tcp.local', service_type=TYPE_HTDT,
                ip=HOST_OUTSIDE, port=8009,
            ), (HOST_OUTSIDE, 5353)),
        ),))
        backend = MdnsDiscoveryBackend(transport=transport)
        obs = backend.discover(_scope(
            approved_service_types=(TYPE_HTDT,),
            approved_networks=('192.168.9.0/24',),
        ))
        assert [o.endpoint for o in obs] == [f'mdns://{HOST_A}:8009']

    def test_bad_network_entry_fails_closed(self) -> None:
        backend = MdnsDiscoveryBackend(transport=FakeTransport())
        with pytest.raises(DiscoveryScopeError):
            backend.discover(_scope(approved_networks=('not-a-cidr',)))

    def test_multiple_services_on_one_ip(self) -> None:
        transport = FakeTransport(rounds=((
            (_mdns_full_response(
                instance='Amp._htdt._tcp.local', service_type=TYPE_HTDT,
                ip=HOST_A, port=8009,
            ), (HOST_A, 5353)),
            (_mdns_full_response(
                instance='Amp Streamer._raop._tcp.local',
                service_type=TYPE_RAOP, ip=HOST_A, port=7000,
            ), (HOST_A, 5353)),
        ),))
        backend = MdnsDiscoveryBackend(transport=transport)
        obs = backend.discover(_scope(
            approved_service_types=(TYPE_HTDT, TYPE_RAOP),
        ))
        assert sorted(o.endpoint for o in obs) == [
            f'mdns://{HOST_A}:7000', f'mdns://{HOST_A}:8009',
        ]
        assert {o.service_type for o in obs} == {TYPE_HTDT, TYPE_RAOP}

    def test_malformed_packet_ignored_with_note(self) -> None:
        transport = FakeTransport(rounds=((
            (b'\x00\x01garbage-not-dns', (HOST_A, 5353)),
            (_mdns_full_response(
                instance='Amp._htdt._tcp.local', service_type=TYPE_HTDT,
                ip=HOST_A, port=8009,
            ), (HOST_A, 5353)),
        ),))
        backend = MdnsDiscoveryBackend(transport=transport)
        obs = backend.discover(_scope(approved_service_types=(TYPE_HTDT,)))
        assert len(obs) == 1
        assert any('malformed' in n for n in backend.last_run_notes())

    def test_instance_without_srv_is_not_recorded(self) -> None:
        transport = FakeTransport(rounds=((
            (_mdns_packet(ptrs=((TYPE_HTDT, 'Ghost._htdt._tcp.local'),)),
             (HOST_A, 5353)),
        ),))
        backend = MdnsDiscoveryBackend(transport=transport)
        obs = backend.discover(_scope(approved_service_types=(TYPE_HTDT,)))
        assert obs == ()
        assert any('without an srv locator' in n
                   for n in backend.last_run_notes())

    def test_quiet_lan_completes_empty(self) -> None:
        backend = MdnsDiscoveryBackend(transport=FakeTransport())
        service = DeviceDiscoveryService()
        run, devices = service.run_discovery(
            document_id=DOC, backend=backend,
            scope=_scope(approved_service_types=(TYPE_HTDT,)),
            started_at_utc=NOW, finished_at_utc=NOW,
        )
        assert run.outcome == 'completed'
        assert run.device_count == 0
        assert run.failure_reason is None
        assert devices == ()

    def test_probe_identity_scoped(self) -> None:
        backend = MdnsDiscoveryBackend(transport=FakeTransport())
        with pytest.raises(DiscoveryScopeError):
            backend.probe_identity(
                f'mdns://{HOST_OUTSIDE}:8009',
                _scope(approved_endpoints=(f'mdns://{HOST_A}:8009',)),
            )

    def test_probe_identity_returns_match(self) -> None:
        instance = 'Amp._htdt._tcp.local'
        transport = FakeTransport(rounds=((
            (_mdns_full_response(
                instance=instance, service_type=TYPE_HTDT,
                ip=HOST_A, port=8009, txt=('serialnumber=SN-778',),
            ), (HOST_A, 5353)),
        ),))
        backend = MdnsDiscoveryBackend(transport=transport)
        scope = _scope(approved_service_types=(TYPE_HTDT,))
        obs = backend.probe_identity(f'mdns://{HOST_A}:8009', scope)
        assert obs is not None
        assert obs.stable_identity == 'SN-778'

    def test_probe_identity_absent_returns_none(self) -> None:
        backend = MdnsDiscoveryBackend(transport=FakeTransport())
        obs = backend.probe_identity(
            f'mdns://{HOST_A}:8009',
            _scope(approved_service_types=(TYPE_HTDT,)),
        )
        assert obs is None


class TestSsdpWire:
    def test_full_observation_via_description(self) -> None:
        loc = f'http://{HOST_A}:49152/device.xml'
        transport = FakeTransport(rounds=((
            _ssdp_response(HOST_A, 'uuid:device-1::' + ST_MEDIA),
        ),))
        fetcher = RecordingFetcher({loc: _upnp_doc()})
        backend = SsdpDiscoveryBackend(
            transport=transport, description_fetcher=fetcher,
        )
        obs = backend.discover(_scope(approved_service_types=(ST_MEDIA,)))
        assert len(obs) == 1
        o = obs[0]
        assert o.endpoint == loc
        assert o.manufacturer == 'Denon'
        assert o.model == 'AVR-X3800H'
        assert o.device_name == 'Living Room AVR'
        assert o.stable_identity == 'device-1'  # usn/udn agree
        assert o.software_version == 'TestOS/1.0 UPnP/1.0 Box/2.0'
        assert o.advertised_capabilities == (
            'urn:schemas-upnp-org:service:AVTransport:1',
        )
        assert o.service_type == ST_MEDIA
        assert fetcher.requested == [loc]
        assert backend.backend_is_simulated is False

    def test_notify_advert_is_parsed(self) -> None:
        loc = f'http://{HOST_A}:49152/device.xml'
        transport = FakeTransport(rounds=((
            _ssdp_notify(HOST_A, 'uuid:device-1', nt=ST_MEDIA, location=loc),
        ),))
        fetcher = RecordingFetcher({loc: _upnp_doc()})
        backend = SsdpDiscoveryBackend(
            transport=transport, description_fetcher=fetcher,
        )
        obs = backend.discover(_scope(approved_networks=('192.168.9.0/24',)))
        assert len(obs) == 1
        assert obs[0].service_type == ST_MEDIA

    def test_usn_udn_mismatch_drops_stable_identity(self) -> None:
        loc = f'http://{HOST_A}:49152/device.xml'
        transport = FakeTransport(rounds=((
            _ssdp_response(HOST_A, 'uuid:claimed-1::' + ST_MEDIA),
        ),))
        fetcher = RecordingFetcher({loc: _upnp_doc(udn='uuid:other-9')})
        backend = SsdpDiscoveryBackend(
            transport=transport, description_fetcher=fetcher,
        )
        obs = backend.discover(_scope(approved_networks=('192.168.9.0/24',)))
        assert len(obs) == 1
        assert obs[0].stable_identity is None
        assert any('usn/udn mismatch' in n for n in backend.last_run_notes())

    def test_off_host_location_never_fetched(self) -> None:
        loc = f'http://{HOST_OUTSIDE}:49152/device.xml'  # off the responder
        transport = FakeTransport(rounds=((
            _ssdp_response(HOST_A, 'uuid:device-1::' + ST_MEDIA,
                           location=loc),
        ),))
        fetcher = RecordingFetcher({loc: _upnp_doc()})
        backend = SsdpDiscoveryBackend(
            transport=transport, description_fetcher=fetcher,
        )
        obs = backend.discover(_scope(
            approved_networks=('0.0.0.0/0',),
        ))
        assert len(obs) == 1  # advert itself was in scope
        assert fetcher.requested == []  # but the off-host fetch never ran
        assert obs[0].manufacturer is None
        assert any('off the responder host' in n
                   for n in backend.last_run_notes())

    def test_unfetchable_description_degrades_not_drops(self) -> None:
        loc = f'http://{HOST_A}:49152/device.xml'
        transport = FakeTransport(rounds=((
            _ssdp_response(HOST_A, 'uuid:device-1::' + ST_MEDIA),
        ),))
        backend = SsdpDiscoveryBackend(
            transport=transport, description_fetcher=RecordingFetcher(),
        )
        obs = backend.discover(_scope(approved_networks=('192.168.9.0/24',)))
        assert len(obs) == 1
        assert obs[0].manufacturer is None
        assert obs[0].stable_identity == 'device-1'  # USN alone stands
        assert any('could not be' in n for n in backend.last_run_notes())

    def test_duplicate_adverts_collapse(self) -> None:
        loc = f'http://{HOST_A}:49152/device.xml'
        transport = FakeTransport(rounds=((
            _ssdp_response(HOST_A, 'uuid:device-1::' + ST_MEDIA),
            _ssdp_response(HOST_A, 'uuid:device-1::' + ST_MEDIA),
        ),))
        backend = SsdpDiscoveryBackend(
            transport=transport, description_fetcher=RecordingFetcher({loc: None}),
        )
        obs = backend.discover(_scope(approved_networks=('192.168.9.0/24',)))
        assert len(obs) == 1
        assert any('duplicate' in n for n in backend.last_run_notes())

    def test_wrong_st_dropped(self) -> None:
        transport = FakeTransport(rounds=((
            _ssdp_response(HOST_A, 'uuid:device-1::urn:other:device:1',
                           st='urn:other:device:1'),
        ),))
        backend = SsdpDiscoveryBackend(transport=transport)
        obs = backend.discover(_scope(approved_service_types=(ST_MEDIA,)))
        assert obs == ()
        assert any('non-approved service' in n
                   for n in backend.last_run_notes())

    def test_out_of_scope_location_dropped(self) -> None:
        transport = FakeTransport(rounds=((
            _ssdp_response(HOST_OUTSIDE, 'uuid:x::' + ST_MEDIA),
        ),))
        backend = SsdpDiscoveryBackend(transport=transport)
        obs = backend.discover(_scope(
            approved_endpoints=(f'http://{HOST_A}:49152/device.xml',),
        ))
        assert obs == ()
        assert any('outside the approved scope' in n
                   for n in backend.last_run_notes())

    def test_malformed_datagram_ignored(self) -> None:
        transport = FakeTransport(rounds=((
            (b'NOT-A-SSDP\r\n\r\n', (HOST_A, 1900)),
            _ssdp_response(HOST_A, 'uuid:device-1::' + ST_MEDIA,
                           location=f'http://{HOST_A}:49152/d.xml'),
        ),))
        backend = SsdpDiscoveryBackend(
            transport=transport,
            description_fetcher=RecordingFetcher(),
        )
        obs = backend.discover(_scope(approved_networks=('192.168.9.0/24',)))
        assert len(obs) == 1
        assert any('malformed' in n for n in backend.last_run_notes())

    def test_probe_identity_scoped(self) -> None:
        backend = SsdpDiscoveryBackend(transport=FakeTransport())
        with pytest.raises(DiscoveryScopeError):
            backend.probe_identity(
                f'http://{HOST_OUTSIDE}:49152/device.xml',
                _scope(approved_endpoints=(f'http://{HOST_A}:49152/d.xml',)),
            )


# -- service-level run honesty ------------------------------------------------


class TestRunHonesty:
    def test_transport_down_is_unavailable_not_failed(self) -> None:
        backend = MdnsDiscoveryBackend(
            transport=FakeTransport(
                available_reason='firewall blocks multicast join',
            ),
        )
        service = DeviceDiscoveryService()
        run, devices = service.run_discovery(
            document_id=DOC, backend=backend,
            scope=_scope(approved_service_types=(TYPE_HTDT,)),
            started_at_utc=NOW, finished_at_utc=NOW,
        )
        assert run.outcome == 'unavailable'
        assert 'firewall' in (run.failure_reason or '')
        assert devices == ()

    def test_transport_failure_mid_run_is_unavailable(self) -> None:
        backend = SsdpDiscoveryBackend(
            transport=FakeTransport(
                raise_on_exchange=DiscoveryTransportError('send failed'),
            ),
        )
        service = DeviceDiscoveryService()
        run, _ = service.run_discovery(
            document_id=DOC, backend=backend,
            scope=_scope(approved_service_types=(ST_MEDIA,)),
            started_at_utc=NOW, finished_at_utc=NOW,
        )
        assert run.outcome == 'unavailable'
        assert 'send failed' in (run.failure_reason or '')

    def test_operator_cancel_records_cancelled(self) -> None:
        backend = MdnsDiscoveryBackend(
            transport=FakeTransport(
                raise_on_exchange=DiscoveryCancelledError('operator stop'),
            ),
        )
        service = DeviceDiscoveryService()
        run, devices = service.run_discovery(
            document_id=DOC, backend=backend,
            scope=_scope(approved_service_types=(TYPE_HTDT,)),
            started_at_utc=NOW, finished_at_utc=NOW,
        )
        assert run.outcome == 'cancelled'
        assert 'operator stop' in (run.failure_reason or '')
        assert devices == ()

    def test_cancel_event_stops_discovery(self) -> None:
        event = threading.Event()
        event.set()
        backend = MdnsDiscoveryBackend(transport=FakeTransport())
        with pytest.raises(DiscoveryCancelledError):
            backend.discover(
                _scope(approved_service_types=(TYPE_HTDT,)),
                cancel_event=event,
            )

    def test_notes_reach_the_sealed_run(self) -> None:
        transport = FakeTransport(rounds=((
            (b'\x00garbage', (HOST_A, 5353)),
        ),))
        backend = MdnsDiscoveryBackend(transport=transport)
        service = DeviceDiscoveryService()
        run, _ = service.run_discovery(
            document_id=DOC, backend=backend,
            scope=_scope(approved_service_types=(TYPE_HTDT,)),
            started_at_utc=NOW, finished_at_utc=NOW,
        )
        assert run.outcome == 'completed'
        assert any('malformed' in n for n in run.notes)

    def test_rescan_runs_again(self) -> None:
        first = ((_mdns_full_response(
            instance='Amp._htdt._tcp.local', service_type=TYPE_HTDT,
            ip=HOST_A, port=8009, txt=('serialnumber=SN-778',),
        ), (HOST_A, 5353)),)
        second = ((_mdns_full_response(
            instance='Amp._htdt._tcp.local', service_type=TYPE_HTDT,
            ip=HOST_A, port=8009, txt=('serialnumber=SN-999',),
        ), (HOST_A, 5353)),)
        transport = FakeTransport(rounds=(first,))
        backend = MdnsDiscoveryBackend(transport=transport)
        scope = _scope(approved_service_types=(TYPE_HTDT,))
        obs1 = backend.discover(scope)
        # swap the round queue for the rescan — a new exchange serves
        # the new round (identifier changed on rediscovery)
        backend._transport = FakeTransport(rounds=(second,))
        obs2 = backend.discover(scope)
        assert obs1[0].stable_identity == 'SN-778'
        assert obs2[0].stable_identity == 'SN-999'
        # run notes do not leak across runs
        assert backend.last_run_notes() == ()

    def test_name_collision_marks_ambiguous(self) -> None:
        # two endpoints, identical advertised name+model, no stable ids
        shared = ('manufacturer=Denon', 'model=AVR-X3800H')
        transport = FakeTransport(rounds=((
            (_mdns_full_response(
                instance='AVR._htdt._tcp.local', service_type=TYPE_HTDT,
                ip=HOST_A, port=8009, txt=shared,
            ), (HOST_A, 5353)),
            (_mdns_full_response(
                instance='AVR._htdt._tcp.local', service_type=TYPE_HTDT,
                ip=HOST_B, port=8009, txt=shared,
            ), (HOST_B, 5353)),
        ),))
        backend = MdnsDiscoveryBackend(transport=transport)
        service = DeviceDiscoveryService()
        run, devices = service.run_discovery(
            document_id=DOC, backend=backend,
            scope=_scope(approved_service_types=(TYPE_HTDT,)),
            started_at_utc=NOW, finished_at_utc=NOW,
        )
        assert run.outcome == 'completed'
        assert run.device_count == 2
        assert run.ambiguous_count == 2
        assert all(d.identity_state == 'ambiguous' for d in devices)


class TestUnavailableBackendsHonest:
    """`simulated` is never mistaken for a real observation and a
    permission-denied environment fails closed."""

    def test_real_backends_are_not_simulated(self) -> None:
        assert MdnsDiscoveryBackend(
            transport=FakeTransport(),
        ).backend_is_simulated is False
        assert SsdpDiscoveryBackend(
            transport=FakeTransport(),
        ).backend_is_simulated is False

    def test_real_transport_constructs(self) -> None:
        # The default constructor wires the real UDP transport; it must
        # exist even where multicast is unusable (available() reports).
        backend = MdnsDiscoveryBackend()
        assert isinstance(backend._transport, UdpMulticastTransport)
        assert backend.available() in (True, False)
        if not backend.available():
            assert backend.unavailable_reason()

    def test_discover_on_down_transport_raises_unavailable(self) -> None:
        backend = MdnsDiscoveryBackend(
            transport=FakeTransport(available_reason='no route'),
        )
        with pytest.raises(DiscoveryBackendUnavailableError):
            backend.discover(_scope(approved_service_types=(TYPE_HTDT,)))


class TestDriftOnRediscovery:
    """A bound device rediscovered over the real wire path with a
    changed identifier produces the drift verdict — never silently
    re-trusted."""

    def _bound_binding(
        self, endpoint: str, serial: str,
    ) -> tuple:
        round0 = ((_mdns_full_response(
            instance='Amp._htdt._tcp.local', service_type=TYPE_HTDT,
            ip=HOST_A, port=8009,
            txt=(
                'manufacturer=Denon', 'model=AVR-X3800H',
                f'serialnumber={serial}',
            ),
        ), (HOST_A, 5353)),)
        backend = MdnsDiscoveryBackend(
            transport=FakeTransport(rounds=(round0,)),
        )
        service = DeviceDiscoveryService()
        scope = _scope(approved_service_types=(TYPE_HTDT,))
        run, devices = service.run_discovery(
            document_id=DOC, backend=backend, scope=scope,
            started_at_utc=NOW, finished_at_utc=NOW,
        )
        assert len(devices) == 1
        probe = service.probe_capability(
            document_id=DOC, device=devices[0],
            prober=FakeCapabilityProber({endpoint: _probe_result()}),
            adapter_id='htdt-avr-lan', at_utc=LATER,
        )
        binding = service.bind(
            document_id=DOC, device=devices[0], probe=probe,
            operator_id='operator-1', at_utc=LATER,
        )
        return service, backend, binding, scope

    def test_stable_identity_change_flags_replacement(self) -> None:
        endpoint = f'mdns://{HOST_A}:8009'
        service, backend, binding, scope = self._bound_binding(
            endpoint, 'SN-778',
        )
        # rediscovery reports a different serial — the unit was swapped
        backend._transport = FakeTransport(rounds=(((
            _mdns_full_response(
                instance='Amp._htdt._tcp.local', service_type=TYPE_HTDT,
                ip=HOST_A, port=8009,
                txt=(
                    'manufacturer=Denon', 'model=AVR-X3800H',
                    'serialnumber=SN-999',
                ),
            ), (HOST_A, 5353)),),))
        report, successor = service.recheck_binding(
            document_id=DOC, binding_chain=(binding,), backend=backend,
            scope=scope, at_utc=LATER,
        )
        assert report.drift_kind == 'identity_replaced'
        assert report.verdict == 'replacement_suspect'
        assert successor is not None
        assert successor.trust_state == 'invalidated_drift'

    def test_same_identity_stays_trusted(self) -> None:
        endpoint = f'mdns://{HOST_A}:8009'
        service, backend, binding, scope = self._bound_binding(
            endpoint, 'SN-778',
        )
        backend._transport = FakeTransport(rounds=(((
            _mdns_full_response(
                instance='Amp._htdt._tcp.local', service_type=TYPE_HTDT,
                ip=HOST_A, port=8009,
                txt=(
                    'manufacturer=Denon', 'model=AVR-X3800H',
                    'serialnumber=SN-778',
                ),
            ), (HOST_A, 5353)),),))
        report, successor = service.recheck_binding(
            document_id=DOC, binding_chain=(binding,), backend=backend,
            scope=scope, at_utc=LATER,
        )
        assert report.verdict in ('unchanged', 'unverifiable')
        if report.verdict == 'unchanged':
            assert successor is None

    def test_unavailable_backend_never_fakes_unreachable(self) -> None:
        endpoint = f'mdns://{HOST_A}:8009'
        service, backend, binding, scope = self._bound_binding(
            endpoint, 'SN-778',
        )
        # transport dies between binding and recheck — the recheck must
        # surface 'backend unavailable', not fabricate drift evidence
        backend._transport = FakeTransport(
            raise_on_exchange=DiscoveryTransportError('NIC down'),
        )
        with pytest.raises(DiscoveryBackendUnavailableError):
            service.recheck_binding(
                document_id=DOC, binding_chain=(binding,), backend=backend,
                scope=scope, at_utc=LATER,
            )
