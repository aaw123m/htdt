# Release verification gate

`scripts/run_release_verification.py` is the single entry point that answers:
**was this exact source revision software-verified?** It runs a versioned
manifest of check classes, then writes machine-readable (JSON) and
human-readable (Markdown) evidence bound to the exact commit, dirty state,
and toolchain that produced it. A PASS can be attached to a release
artifact as provenance; anything less can never be presented as verified.

This is deliberately distinct from `docs/ISSUE_VERIFICATION.md`
(`verify_open_issues.py`), which answers a triage question per open issue.
The release gate asks a different question — a bounded, deterministic set
of checks about the code as a whole, run once against one revision — and
its output is evidence that downstream packaging binds to.

## Components

| Piece | Path | Role |
|---|---|---|
| Manifest | `scripts/release_verification_manifest.yaml` | Versioned declaration of every check class (`required`, `include_in_dev`, `requires_capability`), the checks in each, and the external gates the software verdict does not cover. |
| Runner | `scripts/run_release_verification.py` | Loads the manifest fail-closed, runs selected classes in manifest order in bounded subprocesses, writes evidence. Reuses the bounded-subprocess machinery from `verify_open_issues.py` (Job-Object kill-on-close on Windows, attempt retries, shared `_check_env` / `_env_fingerprint`). |
| Package smoke | `scripts/package_smoke.py` | Optional check class: after `scripts/build-native.ps1` produces `dist-native/HTDT`, verifies the package's `build_info.json` matches the checked-out commit and lock hash, and that `HTDT.exe --version` reports the same revision. |
| Evidence binder | `scripts/verification_evidence.ps1` | `Get-VerificationBlock`: fail-closed validator that turns an evidence JSON file into the `verification` section of the release manifest — or `unverified` / `rejected` when absent, malformed, wrong schema, not `passed`, not `release`/`full`, or bound to a different commit. |
| Installer binding | `scripts/build-installer.ps1` | `-VerificationEvidence` embeds the validated block into `htdt-release-manifest/1` beside the installer; `-RequireVerification` makes the build abort instead of shipping an unverifiable manifest. |
| Workflow | `.github/workflows/build-windows-artifacts.yml` | `run_software_verification` input (default on) runs the release profile on the built commit before packaging and uploads the evidence as the `release-verification-<run>` artifact; the installer and portable manifests bind to it. |
| Tests | `backend/tests/test_release_verification.py` | Offline suite: manifest schema, fail-closed verdict semantics, end-to-end runner behavior, binding pins. |

## Running it

```powershell
# Release-candidate gate (everything in the manifest; the documented
# release command):
python scripts/run_release_verification.py --profile release --report-dir out\release-verification

# Developer fast path (a manifest-declared subset — include_in_dev classes):
python scripts/run_release_verification.py --profile dev

# Deterministic subset by class id (marks the run 'scoped'):
python scripts/run_release_verification.py --classes schema-migration,authority-invariants

# Print the resolved plan — class ids, check targets, timeouts, capability
# verdicts — without running anything:
python scripts/run_release_verification.py --dry-run

# One retry for flaky-prone checks (per-check logs record each attempt):
python scripts/run_release_verification.py --rerun-failed 1
```

Environment knobs match the test recipe: `QT_QPA_PLATFORM=offscreen` and
`PYTHONIOENCODING=utf-8` are forced onto every check child via the shared
`_check_env`, and the recorded `toolchain.managed_env` shows exactly which
variables the gate managed. `TMPDIR` should point at a writable scratch
dir (`C:\t` on the dev boxes) because checks create per-check work dirs.

## Semantics

- **Deterministic selection**: classes run in manifest order; checks run
  in declaration order. `--dry-run` prints the exact resolved plan.
- **Verdicts**: `passed` (every executed check passed; optional classes
  may be `skipped` with an explicit probe reason), `failed` (any check
  `failed`/`timeout`/`error`, or a required check not executed),
  `incomplete` (interrupted or an internal tool error — *never* a PASS,
  even if every executed check was green).
- **Skips are explicit**: optional classes carrying
  `requires_capability` (`native_package`, `inno_setup`) are probed
  before execution; when unsupported, every check in the class is marked
  `skipped` with a `skip_reason` — never silently absent. A *required*
  class cannot depend on a capability (schema-rejected).
- **Coverage is honest**: `coverage: full` only when the release profile
  ran every class; `dev` or `--classes` runs record `coverage: scoped`.
- **Exit codes**: `0` passed, `1` failed, `2` tool error (manifest
  invalid, empty selection, internal error), `3` incomplete.

## Evidence

Per run, `--report-dir` receives:

- `release_verification_evidence.json` — schema
  `htdt-release-verification/1`: `revision.commit_sha` / `dirty` /
  `branch`, `manifest.path` + `version` + `sha256`, `runner.sha256`
  (self-hash), `toolchain` (python version/executable/pip/platform +
  `environment_fingerprint` + dependency-lock file sha256 + managed env),
  `selection` (profile + requested/executed class ids), per-class and
  per-check `status`/`duration_s`/`exit_code`/`log_path`/`skip_reason`/
  `attempts`, `external_gates` echoed from the manifest, and `verdict` +
  `verdict_reasons`.
- `release-verification-<date>.md` — the same verdict, class table, and
  check detail in a reviewable form.
- `logs/<check-id>.log` — each check's captured output.
- `rv-<check-id>/` — each check's work dir (pytest `--basetemp` also lives
  here, so checks never share temp state).

## Provenance binding

The release manifest written beside `HTDT-Setup-<version>.exe` (and the
portable `htdt-release-manifest/1`) gains a `verification` block:

```
verification:
  status: verified | unverified | rejected
  evidence_sha256: <sha256 of release_verification_evidence.json>
  verdict: passed
  profile: release
  coverage: full
  evidence_commit_sha: <commit the evidence was produced on>
  ...
```

`Get-VerificationBlock` accepts only `status: verified` — evidence must
be `passed` + `run_completed` + `profile: release` + `coverage: full` +
matching commit. Anything else is `rejected` (or `unverified` when no
evidence was supplied), and `-RequireVerification` turns that into a
build-stopping error. An artifact built from an unverified or mismatched
revision is therefore visible as such in its own manifest.

## What this gate is not

The manifest declares `external_gates` — owned-room physical campaigns,
real-GPU solver validation, and UX160 owned-Windows visual acceptance —
and the evidence echoes them verbatim. A software PASS never claims to
cover them; they remain separately typed gates. Likewise this changes no
repository CI policy: it is a local/dispatch-time gate, not a per-PR
merge check.
