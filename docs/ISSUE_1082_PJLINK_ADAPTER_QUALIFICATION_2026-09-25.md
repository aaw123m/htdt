# Issue #1082 — PJLink projector adapter qualification (JBMIA Class 2 v2.10)

Status: **software slice landed** — fixture-verified; real projectors are
the hardware gate. Code: `backend/src/htdt/cad_pjlink.py`,
tests: `backend/tests/test_cad_pjlink.py`.

## Surface

`PJLinkDeviceAdapter` implements the #726 `EquipmentDeviceAdapter`
contract (device_kind `projector`) over the injectable `PJLinkTransport`
wire seam (a real transport owns TCP :4352):

```text
discover  -> explicit endpoints only (discovered != bound)
probe     -> per-command support map (CLSS/INF1/INF2/INFO/SNUM/SVER/
             POWR/INPT/AVMT/ERST/LAMP/FILT/INST/IRES/RRES/FRZ)
observe   -> normalized power/input/av_mute/freeze/error-status/lamp/
             filter/inputs/resolutions + identity; raw dump hashed
plan      -> power, input, av_mute, freeze -> exact PJLink set commands
apply     -> operator_confirmed gate; OK is ACK only
read-back -> re-observe; verify_action_outcome decides verified/ack_only/
             not_accepted/field_mismatch
```

## Contract details

- **Class probing is per-command**: reporting `CLSS 2` never implies all
  Class-2 commands work; each probe keeps `supported`/`unsupported` with
  its detail.
- **Exact error categories**: `ERR1` unsupported command, `ERR2` invalid
  parameter, `ERR3` temporarily unavailable, `ERR4` device failure,
  `ERRA` auth — surfaced as `PJLinkError.kind`, never collapsed into
  "offline" (`ERR3` during standby is a warm-up truth, not a failure).
- **Transitional power states preserved**: `0002` cooling / `0003`
  warming stay distinct so callers never hammer a transitioning unit.
- **Class-2 notifications are optional event evidence**
  (`PJLinkNotificationRecord`: raw text + receive time + normalized
  field) — never guaranteed/complete truth.
- **Authentication**: only the documented seed+password MD5 path
  (`pjlink_auth_digest`); the secret stays in the local store via
  `credential_ref` — a name, never a value.
- **JBMIA compatibility list = discovery hint**, never a hardware tier;
  tiers remain DOCUMENTED_ONLY/FIXTURE_VERIFIED/HARDWARE_VERIFIED (#792).
- Lens memory, picture calibration, HDR mode etc. are out of PJLink's
  contract — they stay manufacturer-specific and are not fabricated.

## Verified locally / not yet verified

Verified: frame codec, all normalizers, capability probe, state observe,
plan→apply→read-back `verified`, ERR4 ack rejection, notification
records, auth digest. Not verified: a real projector on :4352, Class-2
notification socket timing, or auth against hardware.
