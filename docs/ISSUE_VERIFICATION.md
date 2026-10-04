# Issue verification

`scripts/verify_open_issues.py` answers one question for triage: **which open
GitHub issues already have green automated evidence, and which still need
manual/physical work?** It is read-only — it never edits issues — and it is
fail-closed: a malformed manifest stops the run instead of producing a
misleading report.

## Components

| Piece | Path | Role |
|---|---|---|
| Manifest | `scripts/issue_verification_manifest.yaml` | Maps each tracked open issue to honest checks (`pytest` / `script` / `manual`) plus the manual evidence that remains. |
| Runner | `scripts/verify_open_issues.py` | Fetches open issues, runs each mapped check in a bounded subprocess, writes the report. |
| Workflow | `.github/workflows/verify-open-issues.yml` | `workflow_dispatch` on `windows-latest`: installs `backend[dev]`, runs the runner, uploads the report as an artifact. |

## Running it

```powershell
# Live mode (reads open issues from the GitHub API; token from env):
$env:GITHUB_TOKEN = '<token with issues:read>'
python scripts/verify_open_issues.py --report-dir out\issue-verification

# Offline / local smoke (no API calls):
C:/devin/python/python.exe scripts/verify_open_issues.py --offline --report-dir C:/t/iv-report

# Restrict to specific issues:
python scripts/verify_open_issues.py --issues 8,471,472,475

# Print the plan without running anything:
python scripts/verify_open_issues.py --dry-run
```

Token lookup order: `GITHUB_TOKEN`, `GITHUB_KA0923S_PAT`, `GH_TOKEN`.

## Reading the report

`--report-dir` receives:

- `issue_verification_report.json` — machine-readable verdicts
- `issue-verification-<date>.md` — the same table for humans
- `logs/<check-id>.log` — stdout/stderr of every executed check
- `iv-<check-id>-*/` — per-check work dirs (pytest `--basetemp`, script
  scratch space) kept for post-mortem on failures

Per-issue verdict:

| Verdict | Meaning |
|---|---|
| `verified` | Every automated check passed **and** the manifest declares no remaining manual evidence. The issue is a close candidate. |
| `partially_verified` | All automated checks passed but manual/physical evidence entries remain (e.g. owned-room campaigns, UX160). Software side is green; the issue stays open by design. |
| `manual_required` | No automated checks exist for the issue — triage/scheduling decision needed. |
| `failing` | At least one automated check failed, errored, or hit its timeout. Read `logs/<check-id>.log`. |
| `unmapped` | The issue is open but has no manifest entry — add one. |
| `not_open` | Manifest entry references an issue that is closed — remove the entry to keep the manifest honest. |

The report deliberately **cannot** mark an issue with a `manual` entry as
`verified`; an issue only reaches `verified` when every check is automated and
green.

## Adding or updating checks

When a fix/feature lands and an issue gains (or loses) automated evidence:

1. Edit `scripts/issue_verification_manifest.yaml`:
   - `kind: pytest` — `tests:` is a list of repo-relative test paths or
     `file.py::Node` ids. Each path must exist; the manifest test suite
     (`backend/tests/test_issue_verification.py`) fails on dangling paths.
   - `kind: script` — `command:` is an argv list; `{python}`, `{work_dir}`
     and `{report_dir}` are resolved by the runner. No shell is involved.
   - `kind: manual` — never executed; describe the exact physical/manual
     evidence required (JA for user-facing gates).
   - `timeout_seconds` (default 900, max 3600) and `pytest_workers`
     (default 4, `0` = `-n 0`) are tunable per check.
2. When an issue becomes fully verifiable, **remove its `manual` entries** so
   it can reach `verified`. When an issue closes, remove its manifest entry.
3. Never weaken or delete a failing check to green a verdict — fix the code
   or mark the manual remainder honestly.

## Workflow

Actions tab → `verify-open-issues` → Run workflow. The report uploads as the
`issue-verification-<run>` artifact.

An optional `--post-summary` step exists (commented out in the workflow):
it posts a one-line verdict comment onto each mapped issue. It is real triage
signal but noisy while iterating; enabling it requires uncommenting the step
and upgrading `permissions.issues` to `write`. Re-commenting without
re-running uses `--post-summary-from <report.json>`.

## Design decisions

- **Artifact reports, not committed `docs/reviews/` files**: per-run output is
  generated data; committing dated auto-reports would spam git history and
  dirty the working tree on every dispatch. Maintainers can still run with
  `--report-dir docs/reviews` when a committed snapshot is wanted.
- **YAML manifest**: `PyYAML` is already a pinned dependency (CamillaDSP
  import), so the manifest gets comments and readability for free. The runner
  also accepts a `.json` manifest for environments without PyYAML.
- **Stdlib-only runner** (plus PyYAML for YAML manifests): no `requests`
  dependency; the only network call is the read-only issues list.
- **Read-only by default**: issues are never modified; `--post-summary` /
  `--post-summary-from` are the only write paths and are opt-in.
