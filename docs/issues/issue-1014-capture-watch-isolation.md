# Issue #1014 / #1021 — CaptureWatchRunner epoch isolation

## Scope

`CaptureWatchRunner._tick` abandons a scan job that outlives
`_STALL_BUDGET_S` (300 s): it requests cooperative cancel, bumps
`_job_generation`, releases `_in_flight`, and immediately starts a
replacement worker. `NativeWorkerPool.start` only *detaches* the old
`QThread` into module ownership — the abandoned callable may still be
mid-`iterdir` or mid-route when its successor begins.

Before this change the abandoned worker and its replacement shared the
runner's mutable `_seen`, `_pending` and `_route_failures` dicts, and
`scan_capture_watch_dir` wrote them *before* any generation check. A
worker resuming from slow IO could therefore corrupt the new epoch's
baseline and pending sets (missed candidates, double routing, or
markers bleeding across watch roots) — the comment said "a stale
generation must not write to them" but nothing enforced it inside the
scanner's side-effects.

## Fix shape

**Job-local state + owner-thread publish.** `_tick` snapshots
`seen`/`pending`/`route_failures` into job-local dicts; the worker
computes the whole next-state against them and returns a frozen
`_WatchScanResult(root, generation, seen, pending, route_failures,
results)`. The worker never reads or writes runner state — not even a
read of `_job_generation` (cancellation is signalled exclusively through
its `cancel` event, which every abandonment path already sets).

`_on_completed` runs on the Qt owner thread and publishes the
next-state wholesale (`clear` + `update`, atomic with respect to that
thread) only while `result.generation == _job_generation` **and**
`result.root == _watched_root` name the live epoch. This guard stands
on its own: the pool's worker-identity dispatch already drops detached
workers' completions, and a cancelled worker's result always reports
`WORKER_CANCELLED`, but the epoch check does not rely on either — a
result computed under another root or generation is discarded even if a
completion were delivered for it.

**Generation coverage.** `_job_generation` now bumps on every event that
retires a job's snapshot: stall-cancel (existing) and every watch-root
change / epoch re-baseline (new), so a job can never publish into an
epoch that began after its snapshot was taken. A disabled watch sets
`_watched_root = None`, which the root equality check rejects — a job
that finishes after the lane was turned off drops its state, and the
next enable re-baselines as before.

**`_job_finished` generation guard.** The `on_finished` callback is
bound with the job's generation; a callback from an abandoned
generation returns without touching `_in_flight`/`_in_flight_since`, so
a late `finished` can never unlatch the replacement job's gate. (Pool
task-record identity already prevents stale `finished` delivery; this
is defense-in-depth for queued-callback ordering.)

**Idempotency at the canonical repo.** Even when a stale worker still
attempts a route (e.g. it was mid-call when cancelled), duplicate
route/ingest of the same bundle is suppressed by content identity, not
by `(mtime_ns, size)` settled-signature hints:
`import_capture_artifact` → `CaptureIngestionRepository.ingest`
deduplicates on `lineage_digest` (verified re-import), and
`CaptureInboxRepository.stage` reports `already_staged` for an existing
lineage — one inbox row, an honest `arrival_count` bump, never a second
promotion. Rejected envelopes deduplicate on the envelope SHA-256 the
same way.

## Preserved contracts

- First-baseline preexisting files are never auto-imported (the
  `\x00scanned:<root>` sentinel and unreadable-listing rule are
  unchanged).
- `(mtime_ns, size)` + two scans is still the settle criterion — a
  signature hint for *delivery timing*, never an import identity.
- Watch-dir change / disable→enable / cancel / shutdown / stall release
  keep marker epochs and roots unmixed; a missing directory still keeps
  the epoch (transient gap, not re-arm).
- `NativeWorkerPool` detach/cancel semantics untouched — no detached
  thread is force-terminated; `WorkerShutdownReport` unchanged.
- `scan_completed` payload shape unchanged: a list of
  `(path, result, error)` per routed drop, emitted only when non-empty.

## Validation

`backend/tests/test_issue_1014_capture_watch_isolation.py` (7 tests) —
barrier-controlled reproduction with real Qt worker threads:

- `test_stalled_worker_resuming_late_cannot_write_new_epoch`: the doomed
  job is blocked inside its scan, the stall tick starts the new epoch,
  then the old worker resumes and writes a poison marker — zero writes
  land in the live epoch's dicts (the worker provably received copies,
  not the runner's dicts), the drop routes exactly once, and the
  detached thread exits cleanly.
- `test_abandoned_route_redelivery_is_idempotent`: the doomed job is
  blocked *inside* `route_capture_intent` before its DB commit; the new
  epoch routes the same bundle first, the stale route then commits —
  one inbox row, `arrival_count == 2`.
- `test_watch_root_change_with_abandoned_job_keeps_epochs_unmixed`:
  stalled root-A job + watch moved to B — no A-path markers in B's
  epoch, B's pre-existing files baseline rather than stage.
- `test_disabled_reenabled_watch_rebaselines_after_abandoned_job`:
  off→on is a new epoch — a drop made while disabled baselines.
- `test_shutdown_with_abandoned_job_leaves_state_consistent`: late
  completions after shutdown mutate and emit nothing.
- `test_on_completed_drops_stale_state_even_if_delivered` /
  `test_job_finished_ignores_stale_generation`: the publish and
  latch guards verified directly against stale `(root, generation)`.

Race reproduction confirmed against the pre-fix runner: the same gated
tests fail there — the abandoned worker's poison marker and root-A
`seen` entries leak into the replacement epoch's `runner._seen`, and
its extra route bumps `arrival_count` without a second inbox row.

Combined run: `test_issue_1014_capture_watch_isolation.py` (7) +
`test_capture_watch_runner.py` (14) — 21 passed.

## Residual risk

- A stale route that completes *before* cancellation is observed is
  externally visible (an inbox row may arrive while watching a
  different root) — intended: the drop was a real delivery, and the
  route is idempotent by content lineage.
- The settle hint remains `(mtime_ns, size)` — a same-signature
  rewrite inside one timestamp tick is still indistinguishable; the
  canonical dedup keys on content so the worst case is a skipped
  re-delivery, not a wrong import.
