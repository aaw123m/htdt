# REV43 — integration seams + boundary review

Scope: adversarial review of the boundaries BETWEEN features on post-REV42
main — REW auto-ingest/watch/assign vs the quality producer (#521), watch
folders vs inbox stage/promote gates, guidance surfaces vs quality/journey,
the new background lanes vs each other and vs document lifecycle, plus the
repo-wide boundary classes: timer/worker lifecycle across unmount/remount,
persisted-vs-live state divergence, staged-vs-committed authority
confusion, and sqlite transaction boundaries of the new producers.
Method: traced each seam end-to-end through the real code paths; fixed
only clear defects (with regression tests); everything else is listed as
verified-clean or a judgment call.

## Defects fixed in this PR

| # | Defect | Root cause | Fix |
|---|--------|-----------|-----|
| **A** | **`.htdtcapture` watch folder silently loses a drop after one routing failure** | `scan_capture_watch_dir` marks a file `seen` at delivery; on a `failed` outcome or a routing exception the marker stayed, so the file was invisible to every later scan. A restart could not recover it either — the baseline pass marks every pre-existing file seen. The scanner's docstring even documents the requeue path ("can be re-queued by removing its `seen` marker") — it was never wired. Transient failures are realistic: `inbox.stage` runs `BEGIN IMMEDIATE` against writers like batch commits, and a descriptor can land a beat before its companion bundle (`user_action_required`). | `CaptureWatchRunner` now keeps per-path attempt counts (`_route_failures`, worker-side only) mirroring the REW lane's `_fail_watch_files`: a non-success route drops the `seen` marker so the file re-settles and re-routes, up to `_ROUTE_MAX_ATTEMPTS` = 3; at the cap the marker stays (wedged) and a signature-changing rewrite still re-delivers it. Success clears the count; counts for vanished files are pruned. |
| **B** | **Rejected/needing-action watch drops produced zero user signal** | `_on_capture_watch_completed` counted failures as `result is None` or `outcome == 'failed'` — `invalid_or_unsupported` and `user_action_required` results fell through both filters, hit `if not staged and not failed: return`, and posted no Activity entry and no statusbar line. The drop vanished silently. | `failed` now covers every non-success outcome (`not in ('staged_for_review', 'already_staged')`); `already_staged` re-arrivals stay quiet by design. |

## Verified clean — seams traced, no defect

- **Auto-ingest quality parity (spec question 1).** Every measurement
  commit lane produces a report through `_produce_quality_report`:
  single pending commit (`measurement_workflow.py:864`), batch commit
  (:2913), IR re-derivation (:1013), runner cell commit (:2215), the
  optimization page REW save (`optimization_workflow_controller.py:714`)
  — plus the first-read backfill (:1235) for pre-producer rows and
  interrupted commits. Auto-ingested rows take the identical
  stage→`auto_assign_batch_items`→`commit_batch`→produce path as manual
  commits; the producer derives only from persisted authorities, so the
  reports are identical given identical records. The synthetic demo
  seed (`cad_synthetic_demo.py:312`) is the only save without a produce
  call — covered by backfill.
- **`.htdtcapture` watch vs stage/promote gates.** Delivery routes
  through `route_capture_intent` → ingest → `inbox.stage` — stage never
  promotes; `stage` is idempotent (`already_staged` bumps
  `arrival_count`) and `BEGIN IMMEDIATE`-serialized. A crash between
  ingest-commit and stage leaves an orphan that
  `reconcile_orphaned_ingestions` re-stages — invoked on every route AND
  on every inbox `list_items` (`workflow_application.py:2209`), so the
  GUI open heals stage-side orphans even when no new drop arrives.
- **Backup re-tick vs concurrent jobs.** `create_backup` snapshots the
  database through sqlite's online backup API — consistent under live
  writers; asset copies are fail-closed (`BackupError` → next tick).
  Cancel propagates into the copy loop (`is_cancelled` →
  `BackupCancelledError`), so shutdown detaches at worst a cheap
  `evaluate` read. Periodic/manual/clean-close writers interleave on
  `automatic-backup-state.json` through atomic tmp+`os.replace` writes
  (lost-update of benign keys only — see judgment calls).
- **GC scan mid-ingest.** `plan_storage_gc` is read-only; `run_storage_gc`
  re-proves reachability under `BEGIN IMMEDIATE` at delete time, plus a
  second transaction that re-verifies before unlink — a file referenced
  between plan and delete is skipped, never deleted-then-dangled.
- **Timer/worker lifecycle across unmount/remount.** Mounts persist
  across navigation: `deactivate_rew_auto` stops the REW timer,
  `_start_rew_auto` guards `isActive()` (no double-arm), in-flight auto
  jobs finish and apply on the hidden page (results still land in the
  queue — by design). `before_deactivate`/`dirty_state` gates block on
  uncommitted batch rows and staged imports; `keep_draft` is offered for
  navigation only — on dispose/exit/project-switch only discard is
  offered, which is honest because the in-memory `_batch` dies with its
  mount (`workspace_dirty_state.py:138-156` documents exactly this).
  `closeEvent` drains the pool; runners drop late completions via
  `_closed`; project-switch respawns all three runners on the new
  composition (`_open_document:1432-1440`) after close hooks shut the
  old ones down.
- **REW lane cross-thread dicts.** `_rew_watch_seen`/`_pending`/
  `_fetch_failures`/`_seen_uuids` are mutated by worker `work()` and by
  GUI apply — but never concurrently: `_apply_rew_auto_result` runs
  inside the same queued `_job_completed` slot that clears
  `_rew_auto_job_keys`, and the next `work()` cannot launch until that
  slot returns (single GUI thread). Detached workers under `stop_busy`
  may still iterate them — their results are discarded, so a
  `RuntimeError` there is inert.
- **Persisted-vs-live preference divergence.** `capture_watch_dir`,
  `storage_watch_enabled` and `rew_auto_*` are re-read every tick; the
  REW endpoint rebinds `base_url` in place on mounted clients. Changes
  apply next tick — never retroactively to a resolved batch.
- **sqlite txn boundaries of the new producers.** `save_observation`
  needs `BEGIN IMMEDIATE` (observation ids are content-addressed — two
  concurrent producers of the same measurement can generate the same id
  and must serialize the check-then-insert); `save_report`'s missing
  `BEGIN IMMEDIATE` is harmless (uuid ids — the PK is the real guard).
  `commit_batch` is per-save-txn + backfill-on-read: a measurement
  without report/context is honest pending, not corruption; an orphan
  observation is reused by the next produce (content-identical).
- **Doc switched mid-poll.** The repository and `data_dir` are
  app-scoped; a detached capture-watch job that outlives its composition
  still stages into the shared, document-agnostic inbox — the new
  composition's page lists it correctly. `capture_receiver`'s
  `delivery_staged` is disconnected before respawn and reconnected in
  the new composition's `__init__`; a delivery in the gap loses only the
  toast, never the staged item.
- **Guidance/journey vs quality.** The measurement journey names
  "review quality" as a step label only; the runner derives cell
  outcomes from the latest replay-validated report via `commit_cell`
  and never asserts a verdict itself — `runner_commit_cell` derives the
  report on demand if no commit path produced one.

## Judgment calls — reported, not fixed

- **Duplicate same-epoch reports on concurrent produce.** GUI backfill
  and a worker commit can both `produce_report` for one measurement in
  the same epoch — both insert (uuid report ids), `latest_report`
  deterministically picks one. Benign duplicates; a UNIQUE constraint
  would need a migration, not worth it.
- **Unbounded first-read backfill.** A project with many pre-#521
  measurements produces one report per report-less row inside
  `measurement_views()` on the GUI thread — one-time cost per row,
  self-limiting (each produce lands in the overlay), but a large legacy
  project pays a multi-second first-render freeze. Candidate for a
  background backfill lane if it ever hurts.
- **Backup state lost-update.** `run_due`/`record_external_generation`/
  `record_clean_close` each do load-modify-save on
  `automatic-backup-state.json`; two writers on different workers can
  lose `clean_close_pending` or `last_automatic_at_utc` — worst case is
  one extra archive or a missed recovery hint. Serialization (a lockfile
  or single-writer queue) is a policy decision.
- **Auto-staging while the measurement page is hidden.** A poll job
  whose apply lands after navigation stages unacknowledged batch rows;
  the dirty gate re-arms on them, so nothing is lost — the operator just
  wasn't watching the notice. Deferring apply to next activation is a UX
  call.
- **`reconcile_orphaned_ingestions` inside `route_capture_intent` is
  unwrapped** (`launch_router.py:251`) — its own failure poisons that
  route attempt. Now covered by fix A's bounded retry; a persistently
  failing reconcile wedges drops visibly via Activity entries.
- **Detached storage-scan vs manual GC/restore.** A `plan_storage_gc`
  detached mid-restore reads a mixed tree; its report may be stale —
  read-only, self-corrects next tick.
- **Up to 3 failure Activity entries per wedged drop** with the retry
  fix — bounded, and each attempt honestly reported. A dedupe would hide
  retry progress; left explicit.
- **Storage-watch in-flight scan ignores a just-disabled pref** —
  disabling stops future ticks, not the running one; its report still
  lands. Honest, one scan late.

## Evidence

- New regression coverage (5 tests):
  `test_runner_retries_failed_route_and_stages_later`,
  `test_runner_gives_up_after_retry_cap`,
  `test_runner_rewrite_after_wedge_redelivers`,
  `test_capture_watch_completion_reports_rejected_outcomes`,
  `test_capture_watch_completion_quiet_on_duplicates_only`.
- Scoped pytest (per-invocation basetemp):
  `TMPDIR=/c/t PYTHONIOENCODING=utf-8 QT_QPA_PLATFORM=offscreen
  C:/devin/python/python.exe -m pytest
  backend/tests/test_capture_watch_runner.py
  backend/tests/test_storage_watch_runner.py
  backend/tests/test_automatic_backup_runner.py
  backend/tests/test_launch_router.py
  backend/tests/test_capture_inbox.py
  backend/tests/test_workflow_application.py -q -n 4 -p no:warnings`
  → **99 passed**; plus `test_rew_auto.py`, `test_measurement_rew_auto.py`,
  `test_measurement_quality_producer.py` → **48 passed** (seam-adjacent
  lanes re-verified unchanged).
- Final merged-tree scope run recorded in the PR body.
