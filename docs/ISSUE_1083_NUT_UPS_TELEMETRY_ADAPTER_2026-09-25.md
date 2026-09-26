# Issue #1083 — NUT UPS/PDU telemetry adapter (RFC 9271)

Status: **software slice landed** — fixture-verified; a real `upsd` is
the hardware gate. Code: `backend/src/htdt/cad_nut_adapter.py` +
`battery_charge_pct`/`battery_runtime_s` on
`OperatingPowerObservation` (#1049); tests: `test_cad_nut_adapter.py`.

## Surface

`NUTTelemetryAdapter` is strictly read-only over the RFC 9271 line
protocol (`NUTTransport` seam; `LIST UPS` / `LIST VAR` / `VER` /
`NETVER` / `GET VAR` only — never `SET`/`INSTCMD`/login):

```text
list_devices  -> (name, description) from LIST UPS
dump_device   -> NUTDeviceRecord: full var map, raw_sha256 pinned
observe       -> per-UPS OperatingPowerObservation + PowerEventObservation
                 composed into a PowerThermalTelemetrySession (#1049)
```

## Mapping

- `ups.realpower`/`output.realpower` → `real_power_w`;
  `ups.power`/`output.power` → `apparent_power_va`;
  `output.voltage` (else `input.voltage`) → `voltage_v`;
  `output.current`/`input.current` → `current_a`;
  `battery.charge`/`battery.runtime` → the new battery fields.
  Nothing is derived — absent variables stay `None`.
- `ups.status` tokens: `OL` → `observed_state='active'`; `OFF` →
  `disconnected`; all other tokens → `vendor_defined` with the raw token
  list in `state_label`, plus per-token `PowerEventObservation` records
  (`OB` → `ups_transfer`, `LB`/`RB`/`OVER`/`BYPASS`/… →
  `vendor_defined` + label, unmapped tokens → `unknown`). Event records
  are evidence of what the UPS reported, not a mains diagnosis.
- `subject_kind` drives the instrument source: a UPS reports as
  `ups_reported`, a NUT-managed PDU branch as `pdu_reported`.
- `NUTDeviceRecord.raw_sha256` + per-record provenance
  (`EquipmentDataProvenance` → `nut-upsd` + server/protocol version)
  keep every observation traceable to the exact dump.

## Verified locally / not yet verified

Verified: LIST/GET framing, error categories (`UNKNOWN-UPS`,
`VAR-NOT-SUPPORTED`, transport down), power/battery/event mapping,
pdu_branch labeling, composition into a hashed telemetry session.
Not verified: a live `upsd` daemon, real UPS firmware quirks, auth
(`upsd.users`) — the read-only surface does not need it.
