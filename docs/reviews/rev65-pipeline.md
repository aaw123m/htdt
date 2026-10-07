# REV65 — pipeline slice (scripts/ + workflows)

Scope: `scripts/*.py`, `scripts/*.ps1`, `.github/workflows/*.yml` — the
automation layer that runs unattended in CI and operator shells. A
fail-open here is a real defect: a gate that reports PASS on partial
work, a hung fetch that never returns, or an artifact name that can
never be uploaded again all break the release promise silently.

Branch `devin/1791369629-rev65-pipeline`. Verified under Python 3.12 /
pytest 9.1.1 (`backend/tests/test_rev65_pipeline.py`, one failing→passing
test per fix; neighboring signature/lifecycle/release-verification/corpus
suites re-run green).

## Method

Read end-to-end: `run_release_verification.py`, `verify_open_issues.py`,
`release_signature.py`, `sign-release.ps1`, `verification_evidence.ps1`,
`build-installer.ps1`, `build-native.ps1`, `issue_lifecycle.py`,
`fetch_external_corpus.py`, `commit_manifest_verification.py`,
`check_dependency_lock.py`, `package_smoke.py`, `ux160_driver.py`,
`ux160_acceptance.py`, all N60–N90/O60 gate scripts,
`golden_path_preflight.py`, `validate_*_windows.py`, `select_affected_tests.py`,
`sync_capture_contract.py`, and both workflows. R-series experiment
drivers (`run_r100*/r130*`) were swept for verdict semantics — they are
evidence emitters (blocked payloads record `*_NOT_VALIDATED` states, no
PASS verdicts), not gates, and were clean.

The release-verification chain (runner → evidence JSON →
`verification_evidence.ps1` → installer/portable manifests → workflow)
was traced end-to-end: commit-binding, `profile=release` +
`coverage=full` + `verdict=passed` + `run_completed` are all enforced
fail-closed; `-RequireVerification`/`-RequireEvidence` make the manifest
step fatal on missing or non-green evidence.

## Defects fixed

| # | File | Defect | Fix |
|---|------|--------|-----|
| 1 | `scripts/release_signature.py` | `signed_sha256` was unconditionally required non-empty, but `sign-release.ps1` legitimately emits `''` for `unsigned`/`signing_failed`/`unverifiable` reports — so `load_signature_report` rejected every non-signed report and the honest non-signed merge path (`publisher_signature.status` recorded next to the verification block, `--require-signed` producing its clean "signature required" error) was dead code. | `signed_sha256` now validated conditionally: non-empty digest required iff `status == 'signed_verified'`, required empty otherwise (a contradictory digest on a non-signed report is rejected). |
| 2 | `scripts/ux160_acceptance.py` | `subprocess.TimeoutExpired` from the 600 s cell timeout propagated uncaught — one hung cell killed the entire matrix run with no `matrix.json` and no report, contradicting the docstring's "a crashed cell is recorded BLOCKED". | `TimeoutExpired` caught in `run_cell`; cell recorded `status='blocked'`, `returncode=None`, `detail='timeout'` (report renders "BLOCKED (timeout)"). |
| 3 | `.github/workflows/build-windows-artifacts.yml` | Both upload-artifact names (`release-verification-${{ github.run_number }}`, `htdt-windows-${{ env.DISPLAY_VERSION }}`) lacked `${{ github.run_attempt }}` — v4+ artifact names are immutable, so any re-run of the same run (or same commit for the build artifact) collided and failed the upload step; a failed run could never go green. `verify-open-issues.yml` already documents this convention. | `${{ github.run_attempt }}` appended to both artifact names with the explanatory comment. |
| 4 | `scripts/fetch_external_corpus.py` | (a) an unknown or non-fetchable `--dataset` admission id produced an empty plan → 0-line receipt → exit 0 "0/0 files verified" (fail-open: CI believes fetch+verify ran). (b) `urllib.request.urlopen` had no timeout — an unattended fetch could block forever on a stalled connection. | Every `--dataset` id is validated against the manifest (must exist and be `download_on_demand_candidate`) → `parser.error` exit 2. `--timeout-seconds` (default 300) feeds `urlopen(timeout=)` — a per-read bound, so multi-GB payloads still complete while a stalled socket terminates. |
| 5 | `scripts/build-native.ps1` | `git status --porcelain` failure collapsed to `$Dirty = $false` (`($LASTEXITCODE -eq 0) -and [bool]$Status`) — a possibly-dirty tree got stamped "clean" into `build_info.json`, the display version, and every downstream identity claim. | Non-zero `git status` exit now throws (same fail-closed style as every other tool invocation in the script). |
| 6 | `scripts/commit_manifest_verification.py` | Docstring promises exit 3 for integrity/commit failure, but no path returned it — sealed-store write failures propagated as traceback exit 1, indistinguishable from a crash and from the documented 2 (bad args/inputs). | Repository construction + gate/result writes wrapped; failure → `return 3`. |
| 7 | `scripts/issue_lifecycle.py` | `classify_issue(check_statuses=...)` was dead wiring: `main()` never supplied per-issue check results, so `implementation_present_checks_red` was unreachable for landed states and an issue with red automated checks classified as `gate_remaining`/`closeable` — the wrong answer to "what landed and what remains". | New `--verification-report` option reads an `issue_verification_report.json` and feeds per-issue check statuses into classification; the report header records whether statuses were supplied. |

## Findings (report only)

* `scripts/verification_evidence.ps1` — when `-ExpectedCommitSha` is empty
  (package built outside `build-native.ps1`, no `commit_sha` in
  `build_info.json`), commit binding is skipped yet the block still
  reports `status='verified'` — an overclaim of binding. Downgrading or
  rejecting is a domain call; the release workflow always supplies a real
  sha today, so left as-is.
* `scripts/commit_manifest_verification.py` — report checks with no
  sealed gate are listed on stderr but exit stays 0; `skipped_manual`
  counts any status outside `_OUTCOME_MAP` as "manual left for the
  wizard" although `verify_open_issues.py` never puts manual checks in
  `checks[]` — effectively a dead counter. Cosmetic.
* `scripts/run_release_verification.py` — report-dir naming is
  second-granularity; two runs in the same second share a directory
  (benign — the verdict files are written atomically, nothing merges).
* `scripts/ux160_driver.py` — an empty placeholder PNG is created before
  `grab().save`; a grab failure leaves a 0-byte screenshot on disk.
  Screenshots are evidence artifacts, not verdict inputs — cosmetic.
* `scripts/issue_lifecycle.py` — `load_verification_issues` on a
  non-mapping YAML document raises `AttributeError` instead of the
  script's clean `_fail` path. Cosmetic robustness.
* `scripts/release_signature.py` — `FORBIDDEN_REPORT_FIELDS` scans only
  top-level keys; nested secret material would not be rejected. Harmless
  in practice — the merge copies a fixed whitelist of fields into the
  manifest, so no nested field can propagate. Defense-in-depth only.

## Verified clean (not exhaustively listed)

* `run_release_verification.py` — interrupted runs mark every pending row
  `not_executed`; verdicts: interrupted→`incomplete`, any non-pass
  executed→`failed`, required-but-skipped→`failed`; timeouts map to
  `timeout` and are never retried; atomic JSON writes; dry-run and empty
  selection exit 2.
* `verify_open_issues.py` — bounded subprocess machinery (Job Objects,
  `_sweep_process_tree` on every exit path), cache refused on a dirty
  tree, status-comment marker + signature checks, 0/2/3 exit contract.
* `sign-release.ps1` — PFX password only via env var, `signtool verify
  /pa` per artifact, `-RequireSigning` throws on any non-`signed_verified`.
* N60–N90/O60 gate scripts — consistent `ErrorActionPreference=Stop`,
  exit-checked `Invoke-Git`, dirty-tree refusal, detach/restore in
  `finally` with restore verification, honest PASS/FAIL exit.
* `check_dependency_lock.py` — lock parse is fail-closed on any
  unparsable line; `--refresh-hashes` rewrites only after every digest is
  fetched (no partial lock); pyproject pins must equal lock pins.
* `package_smoke.py` — exit 2 for tool errors, 1 for real mismatches;
  commit/lock/dirty binding between `build_info.json`, `HEAD`, and the
  tree is honest.
* `validate_*_windows.py`, `golden_path_preflight.py`,
  `select_affected_tests.py` — assertion-style validators propagate
  non-zero; test selection degrades to full-suite on any uncertainty.
