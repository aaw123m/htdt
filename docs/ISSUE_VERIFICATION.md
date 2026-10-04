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

# Reuse cached verdicts for checks whose inputs did not change:
python scripts/verify_open_issues.py --offline --use-cache

# Make a verdict set fatal to the exit code (opt-in gating):
python scripts/verify_open_issues.py --fail-on-verdict failing
```

Token lookup order: `GITHUB_TOKEN`, `GITHUB_KA0923S_PAT`, `GH_TOKEN`.

| Flag | Default | Notes |
|---|---|---|
| `--rerun-failed N` | `1` | Retries a `failed`/`error` check up to N extra times. All attempts land in the report with a `flaky` flag, so a fail→pass suite is *visible as flaky* rather than silently trusted. `timeout` is never retried — it already spent the full check budget. A short pause (2s) separates attempts so the killed attempt's file handles fully release. |
| `--use-cache` | off | Skips re-running a check when `(check config, HEAD sha, python env, managed env vars)` already has a stored verdict in `--cache-file` (default `artifacts/issue-verification/cache.json`). Refuses a dirty worktree — local edits are not covered by the key — unless `--allow-dirty-cache` is given. Cached results are marked `"cached": true`. The key covers the runner-managed env (`QT_QPA_PLATFORM`, `PYTHONIOENCODING`) because those change verdicts; it intentionally does not try to fingerprint every ambient env var. |
| `--fail-on-verdict` | none | Comma-separated verdicts (e.g. `failing,unmapped`) that flip the exit code to `3`. Default behavior is unchanged: verdicts are data, not a gate. |

## Reading the report

`--report-dir` receives:

- `issue_verification_report.json` — machine-readable verdicts (per check:
  `status`, `attempts`, `flaky`, `cached`, `log_path`, `detail`)
- `issue-verification-<date>.md` — the same table for humans, opened by an
  **At a glance** section that groups issues by verdict (close candidates /
  red checks / manual-only / stale manifest entries)
- `logs/<check-id>.log` — stdout/stderr of every executed check attempt
  (retry attempts are separated by `===== attempt N/N =====` markers)
- `iv-<check-id>-*/` — per-check work dirs (pytest `--basetemp`, script
  scratch space) kept for post-mortem on failures

Per-issue verdict:

| Verdict | Meaning |
|---|---|
| `verified` | Every automated check passed **and** the manifest declares no remaining manual evidence. The issue is a close candidate. |
| `partially_verified` | All automated checks passed but manual/physical evidence entries remain (e.g. owned-room campaigns, UX160). Software side is green; the issue stays open by design. |
| `manual_required` | No automated checks exist for the issue — triage/scheduling decision needed. |
| `failing` | At least one automated check failed, errored, or hit its timeout (after retries). Read `logs/<check-id>.log`. |
| `unmapped` | The issue is open but has no manifest entry — add one. |
| `not_open` | The manifest entry references a closed issue, or `--issues` named an issue that is not open — checks are not run; remove the stale entry to keep the manifest honest. |

The report deliberately **cannot** mark an issue with a `manual` entry as
`verified`; an issue only reaches `verified` when every check is automated and
green.

## Posting verdicts to GitHub (opt-in)

`--post-summary` **upserts** one marked comment per mapped issue
(`<!-- verify-open-issues -->`): the first run creates it, later runs edit it
in place. The latest verdict is therefore always visible on the issue itself
without digging through workflow artifacts, and watchers never see comment
spam. `unmapped` and `not_open` issues are skipped; any failed write exits
non-zero.

A comment counts as "ours" only when it **starts with** the marker and
carries the runner's 自動検証 signature — a human quoting the marker mid-body
is never edited. If duplicates exist (two runners posted concurrently), the
oldest is updated and the rest deleted best-effort; the run still posts
the verdict to the surviving comment.

`--post-summary-from <report.json>` posts from a saved report without
re-running checks or touching the issues API read path — it is the fast retry
path when a run succeeded but posting failed.

Enabling it in the workflow requires uncommenting the post step and changing
`permissions.issues` to `write`.

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
   - `id` must match `[A-Za-z0-9._-]+` (it becomes a log filename), must not
     start with `.`, must not be a Windows device name (`con`, `aux`, `nul`,
     `com1`-`com9`, `lpt1`-`lpt9` — `logs/<id>.log` cannot be created on
     windows-latest), and must be unique across all issues (one shared logs
     dir).
   - `timeout_seconds` (default 900, max 3600) and `pytest_workers`
     (default 4, `0` = `-n 0`) are tunable per check.
2. When an issue becomes fully verifiable, **remove its `manual` entries** so
   it can reach `verified`. When an issue closes, remove its manifest entry.
3. Never weaken or delete a failing check to green a verdict — fix the code
   or mark the manual remainder honestly.

## Workflow

Actions tab → `verify-open-issues` → Run workflow. The report uploads as the
`issue-verification-<run>` artifact.

Checks run sequentially inside the 60-minute job: per-check `timeout_seconds`
(≤3600) is the only bound, so keep manifest timeouts honest — the golden-path
check alone budgets 20 minutes. This is also why `timeout` results are not
retried while `failed`/`error` ones are.

## Design decisions — and the rejected alternatives

- **Upserted per-issue comment over new-comment-per-run**: the REV44
  `--post-summary` posted a fresh comment per issue per run — N runs × M
  issues of notification spam, which is why it was left off by default.
  Edit-in-place gives the same always-current status with exactly one comment
  per issue, total.
- **Per-issue comments over a single tracking-issue digest**: a digest issue
  keeps verdicts away from the issue a maintainer is actually triaging. The
  Markdown report's *At a glance* section covers the overview need. A digest
  issue remains an option if per-issue comments ever become unwelcome.
- **Comments over GitHub labels**: a `verified`/`failing` label would conflate
  a computed verdict with the issue's human taxonomy, needs the same write
  permission, and has no visible history. Comments carry the evidence pointer
  (run, ref, failing check ids) and are self-describing.
- **Opt-in SHA+env-keyed cache over unconditional caching**: a cache that lies
  is worse than no cache. Keys cover the check's execution-relevant fields,
  the exact HEAD sha, and the interpreter's installed-distribution
  fingerprint; dirty worktrees opt out entirely. It exists for local iteration
  and re-dispatched runs on the same commit — not as a time-budget mechanism.
- **Retry with visible attempts over single-shot**: the suite has known
  xdist/Qt teardown flakes; a one-shot `failing` verdict would be trusted too
  much, and a hidden auto-retry would hide real flakes. Every attempt is
  recorded and a verdict-changing retry surfaces `flaky: true`.
- **Process-group containment on Windows (Job Object), taskkill fallback**:
  `subprocess.run(timeout=)` kills only the direct child — on Windows, xdist
  workers survive, hold basetemp handles and keep burning CPU; REV45 timed
  out through `taskkill /T /F`, but that still misses *detached*
  grandchildren and reaps nothing when the runner itself dies mid-check.
  Every check process is now placed in a kill-on-close Job Object
  (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`): nested membership pulls
  grandchildren in, the kernel kills all members when the last job handle
  closes (runner crash included), and a sweep runs after *every* attempt —
  timeout, pass, or fail — so even a passing check cannot leak a straggler.
  If Job creation fails the runner falls back to `taskkill /T /F`; on POSIX
  it uses `start_new_session` + `killpg`. `TerminateJobObject` is
  asynchronous, so retries wait briefly for handles to release.
- **`--use-cache` stays local-only** (not wired into the workflow): the
  artifact upload is the audit trail and a dispatch is a request for fresh
  truth — a re-run on the same sha would serve stale verdicts from a
  cache that is exactly what the dispatch is meant to bypass. The cache
  file lives under gitignored `artifacts/`; `actions/cache` can be added
  later if re-dispatch latency actually becomes a problem.
- **Write-ahead report**: the JSON report is written to a temp file and
  `os.replace`d into place, so a reader mid-write (or a killed run) never
  sees a torn file.
- **Artifact reports, not committed `docs/reviews/` files**: per-run output is
  generated data; committing dated auto-reports would spam git history and
  dirty the working tree on every dispatch. Maintainers can still run with
  `--report-dir docs/reviews` when a committed snapshot is wanted.
- **YAML manifest**: `PyYAML` is already a pinned dependency (CamillaDSP
  import), so the manifest gets comments and readability for free. The runner
  also accepts a `.json` manifest for environments without PyYAML.
- **Stdlib-only runner** (plus PyYAML for YAML manifests): no `requests`
  dependency; the only network call is the read-only issues list (plus the
  opt-in comment list/upsert).
- **Read-only by default**: issues are never modified; `--post-summary` /
  `--post-summary-from` are the only write paths and are opt-in.
- **Exit status**: `0` on a completed run (verdicts are data), `2` when the
  tool itself cannot finish (bad manifest, API failure, bad arguments, failed
  comment posts), `3` when `--fail-on-verdict` matches — opt-in gating for
  anyone who later wants this as a check gate.
