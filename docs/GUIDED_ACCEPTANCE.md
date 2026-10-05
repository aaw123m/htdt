# Guided Acceptance Wizard (REV48-HWGUIDE)

Physical acceptance gates — the ones every remaining open issue (#1, #3, #4,
#5, #8, #132) is blocked on — are turned into guided, step-by-step runs
inside the app. The goal is minimal human work: the app checks everything
software can honestly check, auto-captures every piece of evidence it can
observe, and asks the human only to perform the physical action and press
one confirm button (or type an attestation when the step is genuinely
unverifiable).

## Where it lives

App rail → **受入検証** (`ApplicationDestinationId.ACCEPTANCE`). The page is
project-independent: acceptance runs are application-scope authority, stored
in the canonical `cad-scenes.sqlite3` alongside every other append-only
record.

## Step kinds — the honesty contract

Every step in a gate is exactly one of three kinds:

| Kind | What happens | Status source |
|---|---|---|
| `auto` (自動) | The app runs a real check (REW API probe, calibration SHA, backup/restore roundtrip, repo-script subprocess). The button **チェック実行** runs it live. | `auto_check` — only when the check really executed and returned `pass` |
| `guided_manual` (手順) | A physical action is required (aim the UMIK-1, wire the AVR). The page shows the exact instruction; **証跡を自動取得** runs the checkable part (device list, REW mapping, measurement count) as evidence, then the human presses **確認しました（合格）** — one click, no form-filling. | `human_confirm` |
| `attest` (証明) | Genuinely unverifiable by software (perceived quality, DPI visual check, routing judgement). The human types what they confirmed plus optional file-picked evidence (photo/log, digest-bound). | `attestation` |

A step is never marked auto-verified unless the check ran on this machine.
Verdicts from checks are fail-closed: `pass`, `fail`, `unavailable`
(dependency missing — REW down, script absent), or `deferred` (two-phase
checks like restart persistence). `unavailable` keeps the step **pending**
with the reason recorded — it is not a verdict on the system under test,
never silently passed or bricked into a terminal state (`blocked` remains
in the model as a terminal status but no current path sets it). Steps can
also be marked **スキップ** or **不合格** — the run status then honestly
reports `partial`/`failed`.

## The gate manifest

`htdt/acceptance_gates.py` holds a machine-readable `GateDefinition` per
gate, each step carrying a `source_ref` naming the doc/script item it was
derived from:

- **windows-m10** — `docs/WINDOWS_ACCEPTANCE.md` §2 items 1–10 (12 steps:
  4 auto, 5 guided-manual, 3 attest)
- **o60r** — `docs/O60E_CAMPAIGN.md` + `scripts/run-o60-owned-room-gate.ps1`
  + `scripts/audit_o60_owned_room.py` (issue #1)
- **ux160** — `docs/ISSUE_118_UX160_OWNED_WINDOWS_ACCEPTANCE_2026-09-20.md`
  UX-A01..A16 + DPI matrix (issue #3)
- **golden-path** — `scripts/golden_path_preflight.py` auto + UI-run attest
  (issue #8 / #723 physical gate)
- **native-matrix** — issue #132 rows (dependency-lock auto, DPI/recovery/
  offline/routing/installed-product human gates)
- **o90e** — issue #4 robust-domain evidence
- **o100-physical** — issue #5; auto cross-references `gate_run_completed`
  for ux160/o60r/o90e plus a production-eligibility attestation

## Auto checks

`htdt/acceptance_checks.py` — each returns a verdict + JA detail +
machine-readable evidence captured into the step record:

- `env_snapshot` — OS/Python/Qt/PySide6/display/GPU-visible config + git
  HEAD when running from a checkout
- `rew_engine_probe`, `rew_audio_ready`, `rew_input_ready` (UMIK-1 + 48kHz +
  cal file), `rew_calibration_selected`, `rew_output_mapping`,
  `rew_measurement_count[:N]` — live `RewApiClient` probes
- `persistence_probe` — two-phase: writes a digest-bound marker, defers;
  after restart the marker must read back byte-identical
- `backup_restore_roundtrip` — real `create_backup` → `validate_backup` →
  `restore_backup` into scratch → `PRAGMA integrity_check` + the run's own
  rows present in the restored authority
- `campaign_registered`, `comparison_present` — authority-DB queries
- `gate_run_completed:<gate>` — a passed run of another gate exists
  (cross-gate prerequisite)
- `dependency_lock_check`, `golden_path_preflight`, `o60r_audit` —
  subprocess the repo's own scripts; unknown/absent scripts → `unavailable`

## Persistence and replay

`htdt/cad_acceptance_repository.py` on tables `htdt_acceptance_runs` +
`htdt_acceptance_evidence` (schema v18). Runs are append-only revisions:
every transition inserts `(run_id, revision)` with `run_sha256` chained to
the previous revision — `verify_run_chain` re-derives every hash and rejects
gaps, out-of-order rows, tampered payloads, and index-column divergence.
Evidence files go into the managed-assets store (`measurement-assets/<sha>`)
with the manifest row in `htdt_acceptance_evidence`, which also joins
`native_backup._ASSET_MANIFEST_TABLES` so backups carry and validate them.

An in-progress run survives app restart — reopen **受入検証** and pick it
under **受入の実行**; the `persistence_probe` step's second phase then
verifies across the restart. Finished runs stay listed there too (labeled
with their verdict), so a verifier can reopen one and re-export its
bundle.

## Output

On run completion, **証拠バンドルをエクスポート** writes a JSON bundle
(`htdt.acceptance-evidence-bundle` v1): gate id + manifest SHA, machine/code
provenance, per-step status with `verdict_source` (`auto_check` /
`human_confirm` / `attestation`), every evidence digest, and a summary
count (`auto_verified` vs `human_confirmed` vs `attested` vs
`failed`/`blocked`/`skipped`). A copy is also installed as a digest-bound
run-level evidence asset, so the authority DB alone reproduces the bundle.

## What still needs a human

- The physical actions themselves: aiming the UMIK-1, selecting the 90°
  calibration file, doing the REW measurements, wiring the RX-A4A, rebooting
  Windows, visually checking DPI/GPU routing — by design these stay
  `guided_manual`/`attest`.
- `attest` verdicts are typed human statements, honestly labelled — a
  verifier sees exactly which steps are machine-proven vs declared.
- Cross-machine facts (a different Windows box, different GPU path) require
  running the same gate there; the environment snapshot in each run records
  where it ran.

## Tests

`backend/tests/test_cad_acceptance.py` — manifest↔executor registry
consistency, model status rollup, append-only chain + tamper detection,
evidence digest integrity, resume-across-restart, and every auto check
against a mocked REW client.
