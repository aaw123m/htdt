# Issue #1057 — Yamaha RX-A4A evidence-first adapter qualification

Status: **qualification packet landed (software-only)** — no live adapter,
no hardware claims. Code: `backend/src/htdt/cad_yamaha_rxa4a.py`,
tests: `backend/tests/test_cad_yamaha_rxa4a.py`.

## What this issue owns

Everything establishable from public/official Yamaha material *before*
touching the real AVR. The output is a firmware-scoped qualification
packet + sanitized fixtures + backup semantics — not a generic "Yamaha
supported" claim.

## Packet contents (machine-readable)

- `build_rxa4a_capability_matrix()` — #792 rows for adapter
  `htdt-yamaha-rxa4a` v0, firmware **2.26** (released 2026-09-15):
  read directions on documented capabilities are `DOCUMENTED_ONLY`
  (`user_attested_manual` provenance); anything interface-dependent is
  `UNKNOWN`/`experimental_undocumented`. No hardware tiers.
- `RXA4A_FIRMWARE_HISTORY` — 1.65/1.73/2.02/2.12/2.24/2.26 mapped to the
  capability families they touched; older rows carry `stale_after=2.26`,
  and `resolve_firmware_capability` returns `NEEDS_REQUALIFICATION` for a
  unit reporting older firmware — qualification never silently inherits.
- `RXA4A_SOURCE_INVENTORY` — official-source ledger (product page,
  specs, firmware page, RX-A4A manual backup page current; sibling-model
  manual page explicitly `research_only`).
- `RXA4A_BACKUP_SEMANTICS` — menu path, USB FAT16/FAT32 medium,
  `MC_backup_<model>.dat` filename pattern (research-only until
  confirmed on RX-A4A), account/password exclusion, restore-requires-
  reboot, and the `unreviewed_opaque` format status: raw backups are
  stored content-addressed, never parsed.
- `RXA4A_FIXTURES` — sanitized `getSystemInfo`-shape payloads plus
  negative cases (model mismatch, firmware mismatch, unsupported-field
  error envelope, malformed response, ACK-without-readback). No real
  MAC/IP/serial/account data.
- `RXA4A_HARDWARE_GATE_CHECKLIST` — the single consolidated owned-unit
  session (discover/bind, firmware readback, capability probe, backup
  generation, one approved non-destructive write + read-back,
  disconnect/reconnect).

## Documented capability ≠ API capability

The matrix deliberately keeps e.g. `power`/read at `DOCUMENTED_ONLY` —
the manual proves the *product* supports it; no official interface
document proves an adapter *field*. Mirrored YXC specs are
`experimental_undocumented`/`RESEARCH_ONLY` until source/license review
and an owned-hardware probe. UI scraping is not a supported contract.

## Still gated on owned hardware

Everything in `RXA4A_HARDWARE_GATE_CHECKLIST`. Software work claimed
here was verified only by unit tests (fixtures), not by a real RX-A4A.
