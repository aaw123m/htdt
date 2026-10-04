# REV46 — second-pass review of the issue-verification system

Fresh-eyes audit of the post-REV45 state of `scripts/verify_open_issues.py`,
`scripts/issue_verification_manifest.yaml` and
`.github/workflows/verify-open-issues.yml`, focused on the machinery REV45
added (process-tree kill, comment upsert, verdict cache, rerun-failed).
Findings are severity-graded; each is fixed in this PR or documented
no-fix with the reason.

## Findings

### Process containment / tree kill

- **High — REV45's `taskkill /T /F` did not reap detached grandchildren and
  reaped nothing when the runner itself died mid-check.** `taskkill /T` only
  walks the parent/child link; a check that double-forks (or a direct child
  that exits leaving its own children behind) survives, keeps basetemp
  handles, appends into the shared `<check-id>.log`, and burns CPU for the
  rest of the run. If the runner process was killed (Ctrl+C, OOM, job
  cancellation), every in-flight check tree was orphaned unconditionally.
  **Fix:** every check runs inside a Windows Job Object with
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. Nested-job membership (Win8+)
  collects grandchildren, a sweep (`TerminateJobObject`) runs after *every*
  attempt — pass, fail, or timeout — and the kernel kills all members when
  the last job handle closes, covering runner death. `taskkill /T /F`
  remains as fallback when Job creation fails. ctypes signatures declared
  (64-bit handles would truncate to C `int`). POSIX uses
  `start_new_session` + `os.killpg(pid)` — the pgid equals the child pid
  and remains valid to signal even after the group leader exits.
- **Medium — stragglers survived a *successful* attempt.** REV45 only
  killed on timeout; a passing check could still leak children.
  **Fix:** the same `_sweep_process_tree` runs on every exit path.
- **Medium — killed attempts could leak their file handles into the
  retry.** `TerminateJobObject`/`taskkill` are asynchronous; an immediate
  rerun raced on basetemp/log handles.
  **Fix:** 2s pause before attempts >1.
- **Medium — Popen spawn failure crashed the attempt loop.** An
  `OSError` from `Popen` (missing interpreter, path too long) propagated
  out of `run_check` instead of becoming an `error` attempt eligible for
  retry. **Fix:** spawn failure records an `error` attempt.
- **Low — `AssignProcessToJobObject` failure.** Child already in a job or
  exited early → fall back to `taskkill` sweep rather than failing.

### Upsert comments

- **High — a human comment merely *containing* the marker got
  clobbered.** REV45 matched `COMMENT_MARKER in body` anywhere; a maintainer
  quoting `<!-- verify-open-issues -->` in their own text would have it
  overwritten by the next run. **Fix:** ours = body *starts with* the
  marker **and** carries the `自動検証` signature written by
  `_comment_body` (GITHUB_TOKEN actors cannot call `GET /user`, so
  authorship is proven by body signature, not user id).
- **Medium — posting race left duplicate marked comments permanently.**
  Two concurrent runs both found no comment → both POSTed → the later
  lookup PATCHed an arbitrary one and the stale copy kept shadowing the
  verdict. **Fix:** lookup returns all matches; the oldest is updated and
  the rest deleted best-effort.
- **Low — >100-comment issues.** Lookup already paginates via `Link`
  headers; now on a per-issue `/comments` endpoint with
  `per_page=100` (unchanged semantics, made robust by non-list-page
  guards).
- **Info — mid-batch post failure** leaves some issues updated, some not.
  That is acceptable: every comment carries its run id/timestamp,
  `--post-summary-from` is the documented retry path, and exit code 2
  signals the partial failure. No transactional fix attempted (the API has
  no batch update).

### Cache

- **Medium — the cache key missed the runner-managed env.**
  `QT_QPA_PLATFORM`/`PYTHONIOENCODING` are set by the runner and
  materially change verdicts (offscreen vs real-GUI pytest), but were not
  in the key — a cached verdict could be replayed under a different UI
  environment. **Fix:** the key now includes `managed_env` for the vars
  the runner injects, and the env fingerprint also records
  `platform.machine()`.
- **Info — `--use-cache` stays local-only; it is deliberately *not* wired
  into the workflow.** A `workflow_dispatch` is a request for fresh truth;
  serving verdicts from a cache on the same sha is exactly what the
  dispatch is meant to bypass, and the artifact upload is the audit trail.
  The default cache path sits under gitignored `artifacts/`. If
  re-dispatch latency ever matters, `actions/cache` is a one-step add —
  documented in `docs/ISSUE_VERIFICATION.md`.
- **Info — dirty-worktree guard** verified effective: `git status
  --porcelain` refuses unless `--allow-dirty-cache`; corrupt cache files
  are ignored and rewritten (pre-existing tests cover both).

### rerun-failed

- **Verified correct** — `timeout` never retries (budget spent), retries
  happen in-line per check (fail-fast on the same machine while the flake
  window is warm, instead of a whole-second-pass), `attempts`/`flaky` are
  in both JSON and MD, and attempt separators land in the log.

### Schema / CLI / API hardening

- **Medium — check ids that are Windows device names (`con`, `aux`,
  `com1`…) or start with `.` broke `logs/<id>.log` creation on
  windows-latest.** **Fix:** manifest load rejects them.
- **Medium — a duplicated check id across two issues silently overwrote
  the earlier check's log.** **Fix:** ids must be unique manifest-wide.
- **Low — `--issues -1`/`0` passed validation** and produced an empty,
  misleading report. **Fix:** non-positive numbers → exit 2.
- **Low — a non-list API page** (e.g. abuse-detection dict response)
  crashed with a TypeError. **Fix:** shape-checked → clean `RuntimeError`.
- **Low — the JSON report could be read mid-write.** **Fix:** temp-file +
  `os.replace` atomic publish.
- **Low — unhandled exception anywhere in the check loop** aborted the
  whole run without a report. **Fix:** `main()` catches, prints the
  traceback, exits 2 (the tool failed; no half report).

### Manifest coverage drift

- **Medium — issue #538 (auralization evidence & shareability) was
  unmapped.** **Fix:** new entry — `auralization-foundation-authority`
  pytest over the four existing auralization/listening-session test files
  + JA manual entry for the capability-bound multi-source review package
  and measured-vs-predicted listening validation (not implemented).
  All other live open issues (1,2,3,4,5,8,34,131,132,471,472,475,533,534)
  remain mapped; their check paths were re-verified to exist.

### Workflow

- **Medium — a failing verify step swallowed the report.** The upload
  step ran only on success, so the most interesting runs produced no
  artifact. **Fix:** `if: always()`; `run_attempt` added to the artifact
  name so re-runs don't collide; `if-no-files-found` downgraded to `warn`
  (a crashed runner still exits the job correctly).
- **Info — permissions stay `contents: read` + `issues: read`**; the
  commented post step documents the `issues: write` bump, matching the
  new upsert semantics.

## Explicitly considered and rejected

- **actions/cache in the workflow** — see Cache above: a dispatch is a
  fresh-truth request; caching would serve identical verdicts or, worse,
  stale ones after an env change.
- **Retrying `timeout` checks** — the timeout *is* the spent budget;
  retrying doubles the worst-case wall time for a signal that is already
  deterministic.
- **Killing stragglers via per-check basetemp cleanup instead of a
  sweep** — leaves CPU-burning orphans alive; containment (job object)
  is strictly stronger and simpler.
- **DELETE-ing duplicate comments via a batch endpoint** — none exists;
  per-comment DELETEs are already best-effort and harmless if one fails.
- **Comment authorship via `GET /user`** — `GITHUB_TOKEN` actors get 403;
  body-signature matching is both available and more precise (a bot-run
  comment is identified by its content, not by whichever credential
  happened to post it).

## Verdict

With the above fixes the system is **trustworthy as CI triage signal**:
fail-closed schema, verdicts are data (exit 0) unless `--fail-on-verdict`
opts into gating, process containment is kernel-enforced, and the comment
surface can no longer clobber human text. It remains a manual-dispatch,
read-only-by-default lane — appropriate while the repo has no PR CI to
block on.
