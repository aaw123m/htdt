# Issue #949 slice — real mDNS/SSDP discovery backends

Scope: `MdnsDiscoveryBackend` and `SsdpDiscoveryBackend` are real,
read-only wire implementations behind the #879 `DiscoveryBackend` seam —
stdlib only (`socket`/`select`/`struct`/`ipaddress`/`xml.etree`), no new
dependencies, bounded timeouts, and fail-closed on every environment
that cannot multicast. `VendorDiscoveryBackend` remains a fail-closed
stub: vendor-specific paths ship only where a documented spec exists.

## What changed

- `MulticastQueryTransport` — the injectable seam: one `exchange` sends
  each datagram once and collects `(payload, source)` pairs inside a
  bounded window. `UdpMulticastTransport` is the real UDP implementation
  (mDNS: join `224.0.0.251:5353`; SSDP: M-SEARCH to
  `239.255.255.250:1900` from an ephemeral port). All waits are bounded;
  nothing retransmits.
- `MdnsDiscoveryBackend` — PTR queries for the operator-approved service
  types; when none are approved it enumerates service types via the
  DNS-SD enumeration PTR first, then queries each discovered type —
  bounded by `max_service_types`. SRV/TXT/A/AAAA records build the
  observation; identity fields are advertised claims only
  (`manufacturer`/`model`/`serial`/`firmware` TXT keys, mapped per the
  in-code table — never inferred).
- `SsdpDiscoveryBackend` — one M-SEARCH per approved ST (`ssdp:all`
  when none), NOTIFY adverts accepted too. The LOCATION document is
  fetched only when it lives on the responder's own host (off-host URLs
  are never followed). USN and UDN must agree to count as
  `stable_identity`; a mismatch leaves the field empty and records a
  run note. A missing/unparseable description degrades the observation
  to `partial`/`unidentified` instead of dropping it.
- Scope admission for ambient discovery: `approved_endpoints` (host
  match) and `approved_networks` (CIDR/address membership) gate every
  advertised endpoint before it becomes an observation; invalid network
  entries raise `DiscoveryScopeError` before a single packet leaves.
  Host names that cannot prove network membership are not admitted when
  networks are configured.
- New run outcome `cancelled` (JA: キャンセル) — the operator's
  `threading.Event` stops a run between query rounds and after each
  receive; a cancelled or unavailable run never emits device records.
- `DiscoveryRunRecord.notes` now carries the backend's run notes:
  malformed datagrams, out-of-scope drops, duplicate collapses,
  unfetched descriptions, enumeration caps — the "what was filtered and
  why" trail is sealed with the run.
- `recheck_binding` propagates `DiscoveryBackendUnavailableError` /
  `DiscoveryCancelledError` — a backend that could not run produces no
  drift verdict at all rather than fabricating `endpoint_unreachable`.

## Honesty rules kept

- `available()`/`unavailable_reason()` are real probes — a host that
  cannot open the multicast socket (firewall, missing route, disabled
  interface) reports `unavailable`; a run that dies mid-exchange maps to
  `unavailable` via `DiscoveryTransportError` ⊂
  `DiscoveryBackendUnavailableError`.
- `backend_is_simulated` stays `False` on the real backends and `True`
  on `FakeDiscoveryBackend`; simulated and real observations can never
  read as the same evidence.
- Discovery is read-only: the backends only ever *send* PTR queries and
  M-SEARCH probes; nothing writes device state, nothing auto-deploys —
  promotion to `trusted` still requires the operator's explicit `bind()`.
- `service_type` is recorded on the unsealed `DiscoveryObservation`
  (which advertised the endpoint); sealed record schemas are unchanged
  — no stored-hash churn on existing rows.

## Testing (fake UDP injection — no LAN required)

`backend/tests/test_issue_949_lan_discovery.py` drives the wire parsers
with crafted datagrams through `FakeTransport` (queued response rounds)
and `RecordingFetcher` (description documents): out-of-scope endpoint/
network/ST rejection, duplicates, malformed packets, USN/UDN mismatch,
off-host LOCATION, unfetchable descriptions, quiet-LAN empty runs,
mid-run transport failure → `unavailable`, operator cancel →
`cancelled`, rescan, and the full ladder — discover → probe → bind →
rediscovery with a changed serial → `replacement_suspect` drift.

## Real-network verification procedure (physical evidence → #879 gate)

1. Approve a scope: `build_scan_scope(approved_service_types=('_htdt._tcp.local',), approved_networks=('192.168.x.0/24',), approved_by=…, approved_at_utc=…)`.
2. `service.run_discovery(backend=MdnsDiscoveryBackend(), scope=scope, …)`
   — on a LAN with mDNS devices the run completes with real
   observations; on a host without multicast it reports `unavailable`
   with the OS-level reason.
3. Repeat for `SsdpDiscoveryBackend()` on a LAN with UPnP devices —
   confirm `LOCATION` documents fetched only from same-host URLs.
4. Re-run with Windows firewall blocking the multicast groups — expect
   `unavailable`, never `failed`, never a fabricated `completed`.
5. Verify the sealed `DiscoveryRunRecord.notes` list any filtered
   endpoints/malformed datagrams seen on the real LAN.

Recording of that evidence stays under the #879 `physical` gate —
this slice only adds the procedure and the backend it exercises.
