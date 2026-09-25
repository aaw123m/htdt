# Issue #1069 — Lighting adapter qualification: Hue local API first, Matter Scenes evaluated

Status: **software slice landed** — fixture-verified, hardware-gated for
real bridges. Code: `backend/src/htdt/cad_hue_lighting.py`,
tests: `backend/tests/test_cad_hue_lighting.py`.

## Target 1 — Philips Hue local API (CLIP v2)

`HueLocalAdapter` implements the #726 `EquipmentDeviceAdapter` contract
over the bridge-local REST API:

```text
discover_targets  -> /clip/v2/resource/{light,grouped_light,scene}
probe             -> per-resource bounded surface (on/dimming/CCT/xy/recall)
observe           -> normalized on/brightness_pct/cct_k/color_xy + identity
plan/apply        -> PUT bodies; explicit operator_confirmed gate
read_back         -> fresh GET; verify_action_outcome is the arbiter
events            -> /eventstream/clip/v2 objects as HueEventRecord evidence
```

Boundary decisions:

- Endpoints are explicit `hue://<host>/<rtype>/<rid>` bindings; discovery
  lists candidates but never binds.
- `hue-application-key` stays in the secret store: the binding holds
  `credential_ref` (a name); a `credential_resolver` resolves it at
  request time and the value never enters records.
- Bounded mutation surface only: `on`, `brightness_pct`, `cct_k`
  (mirek, clamped 153–500), `color_xy`, `scene_recall`. Gradients,
  entertainment/streaming and dynamic scenes report `unsupported`.
- Hue `200`/API errors keep exact semantics — an API ACK is not observed
  state; verification requires the read-back comparison.
- `DeviceKind` gained `lighting_fixture`/`lighting_controller` so light
  and scene/group bindings stay typed.

## Target 2 — Matter Scenes (evaluation outcome)

Matter Scenes (1.4.2 certifiable; 1.5 spec published) are a parallel
controller track, **not** claimed here: a conformant adapter needs a
Matter controller stack (chip-tool/python-matter-server), device
commissioning and ACLs. The adapter seam (`HueTransport` /
`EquipmentDeviceAdapter`) is the intended insertion point — a Matter
adapter would reuse `DeviceTargetBinding`, the ACK≠truth rule and the
commissioning-record handoff unchanged. Cloud control is not required
for any part of this slice.

## Verified locally / not yet verified

Verified: full lifecycle against `FixtureHueTransport` (probe → plan →
apply → read-back → `verified`; rejection path; event normalization;
endpoint typing). Not verified: a real Hue bridge, HTTPS/TLS details,
event-stream streaming, or Matter hardware.
