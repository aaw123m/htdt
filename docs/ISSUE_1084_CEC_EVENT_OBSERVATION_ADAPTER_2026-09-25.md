# Issue #1084 — CEC event-observation adapter (libCEC, bounded)

Status: **software slice landed** — fixture-verified; a real CEC bus is
the hardware gate. Code: `backend/src/htdt/cad_cec_adapter.py`,
tests: `backend/tests/test_cad_cec_adapter.py`.

## What it is (and is not)

`CECObservationAdapter` collects raw CEC bus frames from a `CECTransport`
seam and produces self-hashed `CECEventRecord` evidence — raw text +
receive time + bus endpoint + normalized interpretation. It is
deliberately **not** an `EquipmentDeviceAdapter`: CEC observation is a
bus-level evidence stream, not a per-device control contract, and CEC
frames are *claims by devices on the bus*, never handshake truth —
device truth still comes from that device's own adapter read-back
(PJLink #1082, vendor APIs).

## Normalization subset

ACTIVE_SOURCE (0x82), ROUTING_CHANGE (0x80), INACTIVE_SOURCE (0x9D),
STANDBY (0x36), REPORT/GIVE_DEVICE_POWER_STATUS (0x90/0x8F with
on/standby/transition operands), REPORT_PHYSICAL_ADDRESS (0x84),
REQUEST_ACTIVE_SOURCE (0x85), CEC version pair (0x9E/0x9F),
DEVICE_VENDOR_ID (0x87), OSD name pair (0x46/0x47), USER_CONTROL
(0x44/0x45), system-audio trio (0x70/0x72/0x7A), VENDOR_COMMAND (0x89).
Everything else → `event_kind='unknown'` with raw payload intact;
malformed lines → `unknown` + `malformed_frame` label, never dropped.

## Boundary

- Observe-only by default: `CECTransport.tx` is a separate explicit
  grant; `FixtureCECTransport` rejects it (`transmit_unavailable`) to
  mirror the default posture.
- libCEC is GPL — the adapter consumes a frame stream (e.g.
  `cec-client` traffic output) out-of-process; no libCEC symbols are
  linked into HTDT.
- Endpoints are explicit (`cec:/dev/cec0`-style bus bindings); the
  adapter does not guess bus topology, and logical-address claims are
  recorded, not trusted.

## Verified locally / not yet verified

Verified: frame parsing (with `>>`/`<<` prefixes), the opcode subset,
power-status/physical-address normalization, unknown/malformed
retention, frozen record hashing, unbound-endpoint and observe-only
rejection. Not verified: a real libCEC adapter, bus contention, or
transmission.
