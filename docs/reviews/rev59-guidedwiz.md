# REV59-GUIDEDWIZ — zero-knowledge guided verification wizard

Child track of REV59 on `devin/<ts>-rev59-guidedwiz`; consumes the
CLOSEAUX bridge (schema v75) that landed on main.

## Scope

- `verification_wizard.py` — Qt-free core:
  - `load_wizard_issues(path)` — manifest → picker rows. Repeated
    `issue:` keys merge under one `issue_ref`, matching how
    `evaluate_issue_verdict` groups sealed gates (defensive — main
    deduplicated the manifest concurrently at 72d6576d).
  - `resolve_check_argv` — the runner's argv contract lifted headless:
    pytest gets `-q -p no:warnings --tb=short -n <workers>` plus an
    isolated `--basetemp`; script checks substitute
    `{python}`/`{work_dir}`/`{report_dir}`; missing test paths and
    manual kinds fail closed before any work dir exists.
  - `run_check` — bounded subprocess (per-check timeout, process-tree
    sweep on timeout, log per check under `verification-runs/logs/`).
    Never raises: spawn/missing-target problems come back as `error`.
  - `ManifestGateStore` — app-scope persistence on
    `cad-scenes.sqlite3` under `document_id='application'`: seals gates
    idempotently, commits `GateRunResult`s, and binds evidence by
    digest — the check log sha256 lands in `detail`, and manual
    evidence commits a bundle JSON (note + per-file digests) whose own
    digest becomes the `AuthorityRef`. Replayable down to bytes via
    `ManagedAssetStore.read_verified`.
- `verification_wizard_page.py` — `VerificationWizardPage`: issue
  picker with per-issue verdict suffix, check list (`自動`/`実機`),
  plain-language `description` panel, generated checklist from
  `derive_verification_requirement` when a manual gate declares cells,
  evidence attach + typed attestation → `evidence_committed`, and the
  verdict banner driven by `evaluate_issue_verdict` +
  `MANIFEST_VERDICT_LABELS`. Worker detach on close matches
  `acceptance_page.py`.
- Mount: `ApplicationDestinationId.VERIFICATION` (検証ウィザード),
  `WorkspaceRegistration` + lazy import + `navigation.verification`
  palette command.

## Honesty contract

- A manual gate satisfies ONLY via `evidence_committed` + bound ref —
  a passing script never substitutes (asserted by the store test).
- `unevaluated`/`partially_verified`/`failing`/`manual_required`/`verified`
  come straight from the bridge; the page never invents a verdict.
- `evidence_committed` without files is an honest attestation record:
  the bundle JSON is the evidence.

## Concurrency note

The `command:`/`argv:` loader bug (manifest script checks use
`command:`; the loader read only `argv:`) was found here first —
then fixed independently upstream at 3e66dc48 with better per-check
error context, so this branch takes the upstream file wholesale and
the fix does not appear in this PR's diff.

## Tests

`test_rev59_guidedwiz.py` — 14 tests: synthetic + REAL manifest load
(188 unique issues / 204 pytest / 1 script / 115 manual), argv
construction + placeholder substitution + fail-closed cases, bounded
subprocess outcomes (passed/failed/timeout/error), store round-trip
with log-digest binding, manual-gate honesty (passed ≠ satisfied),
evidence bundle replay, latest-result selection, verdict ladder, and
two offscreen page tests (issue list + commit + verdict render, missing
manifest error surface).

## Remainder

- The single `script` check (`golden-path-preflight`) runs with
  `{work_dir}`/`{report_dir}` bound to the wizard's per-run dirs.
