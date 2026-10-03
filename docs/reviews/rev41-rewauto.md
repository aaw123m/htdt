# REV41 — REW automation review (PR #513)

Scope: PR #513 — `rew_auto.py` helpers (`find_rew_install`, `launch_rew`,
`propose_assignment_target`, `scan_rew_watch_dir`), the workspace poll loop
(`_rew_auto_tick`/`_start_rew_auto_job`/`_apply_rew_auto_result`,
navigation-gate exclusion via `_rew_auto_job_keys`/`_user_busy_count`),
`auto_assign_batch_items`, and the new preferences
(`integrations.rew_auto_*`, `rew_watch_dir`, `rew_install_path`).

Method: (1) line-level audit of every file the PR touched plus the
collision surfaces (`NativeWorkerPool` lifecycle, `latest_revision`
preconditions, batch staging/commit); (2) runtime probes
(`probe_rev41_rewauto.py`, throwaway — not committed): mid-write watch-dir
drop, drop during the no-scene window, stage-failure retry, `'seat1'`-vs-
`'seat10'` matching, `auto_assign_batch_items` raise escaping the apply;
(3) scoped pytest of the REW automation + adjacent navigation/job-safety
suites.

All five probe scenarios confirmed real defects; nine defects total were
fixed in this PR with regression coverage.

## Fixed defects (severity-ranked)

| # | Defect | Severity | Fix |
|---|--------|----------|-----|
| 1 | **Mid-write watch-dir file staged truncated** — `scan_rew_watch_dir` delivered a file on first sighting; a large copy in flight was read part-way AND re-staged on the next scan as a "changed" file → truncated row + duplicate row in the import queue. Probe-verified: write→scan staged `part1`, grow→scan re-staged `part1+part2` as a second row. | **High** | `scan_rew_watch_dir` gained a `pending` map: a new/changed signature must survive two consecutive scans before delivery; a still-changing signature stays pending; deleted candidates are pruned. Files are never read mid-write. |
| 2 | **Fetched measurement silently dropped on stage failure** — `_apply_rew_auto_result` merged `fetched_uuids` into `_rew_seen_uuids` *before* `stage_rew_snapshots`; a stage exception left the uuid marked seen → never refetched → the measurement the UI promised to auto-load was permanently skipped. Probe-verified. | **High** | uuids merge into the seen-set only after `stage_rew_snapshots` succeeds; a failed stage retries on the next poll. |
| 3 | **Watch-dir drop during the no-scene window permanently lost** — `stage_rew_text_files` raises `MeasurementWorkflowError` when no scene revision exists; the file was already marked seen at scan time → after saving a scene the drop was never staged. Probe-verified. | **High** | Apply-side `latest_revision()` check routes watch files to the `needs_scene` path and `_unmark_watch_files` re-queues them; they stage on the first polls after a scene is saved. |
| 4 | **`'seat1'` bound inside `'seat10'`** — substring matching made a measurement titled `seat10` auto-assign to a target named `seat1` (or collapse ambiguity to the wrong survivor). A confident wrong-seat bind is worse than no bind. Probe-verified. | **Medium** | `_name_within_label` rejects matches where the name touches an ASCII letter/digit/underscore run-on (word-boundary semantics); punctuation and non-ASCII (JA compounds, `seat-a`) still match. |
| 5 | **`auto_assign_batch_items` raise escaped to `sys.excepthook` every tick** — called unguarded inside the apply; a scene-read error aborted `_apply_rew_auto_result` mid-run (batch staged but no notice, watch path never reached) and surfaced only via the global excepthook. Probe-verified. | **Medium** | `_auto_assign_safely` wraps the call → JA error notice + rows reported as unresolved so the success detail still points at 「割り当て」. |
| 6 | **Skipped watch files swallowed silently** — `watch_skipped` (oversized/unreadable drops) was collected by the worker and then ignored by the apply — invisible to the operator. | **Medium** | Deduped JA warning notice (`watch_skipped:` key) + skipped count appended to the success detail when a partial batch stages. |
| 7 | **Persistent auto warnings re-shouted every 15 s tick** — `needs_scene`/`watch_dir_missing` rewrote the same warning every poll, clobbering whatever notice the operator was reading. | **Low** | `_auto_notice_once` keys each persistent condition; the key clears when the condition resolves so it can fire again. |
| 8 | **`pool.start` raise leaked the job key → permanent navigation wedge** — the key was registered in `_rew_auto_job_keys` before `start()`; a raise (e.g. pool shut down) left a phantom auto job → `_user_busy_count` returned −1 (truthy) → navigation permanently blocked by "background processing". | **Low** | `start` is try-wrapped; on raise the key/handler/purpose/latest entries are removed before re-raising. |
| 9 | **`_user_busy_count` could go transiently negative** — `active_count` drops when the worker thread finishes, before `_job_completed` discards the key on the event loop; a negative result is truthy → spurious navigation block in that window. | **Low** | Clamped `max(0, …)`. |
| 10 | **Blank error prefix on auto jobs** — the job handler was registered with `''` as its error title, so a poll job failure completed with an untitled error notice. | **Low** | Real JA prefix `REW自動処理に失敗しました`. |

## Judgment calls — reported, not fixed

| # | Finding | Severity | Notes |
|---|---------|----------|-------|
| 1 | **App-restart re-baseline** — the uuid seen-set and watch seen-map are in-memory; on restart the first poll baselines the current REW library, so measurements taken *while the app was closed* are treated as pre-existing (never staged). Consistent with the fail-safe "never flood the queue" contract, but drops during downtime need a manual import. Persisting the seen-set is a feature, not a defect fix. | Design | Deferred to product intent. |
| 2 | **Nonexistent `integrations.rew_install_path` silently falls through** to env/candidates instead of warning — a typo'd path looks identical to "not configured" until the launch button appears from a fallback candidate. | LOW consistency | One-line warning possible; matches the existing pref convention. |
| 3 | **`.mdat` in `REW_TEXT_SUFFIXES`** while the manual import dialog filters `*.txt *.frd` — the watch dir accepts a suffix the dialog does not advertise. Possibly deliberate (REW text exports can use `.mdat`), flagged for confirmation. | LOW consistency | — |
| 4 | **`assignment_targets()` is pinned to `self._pending`'s revision** when a batch is pending — auto-assign resolves names against the pending-scene targets; if the committed scene diverges, matches can target a stale view. Same pinning convention the rest of the workflow uses. | LOW correctness | Correct per the pinning contract; noted because the poll runs unattended. |
| 5 | **commit/stage duplicate-classification race** — a commit that lands between scan and stage re-classifies the staged row. Pre-existing batch pattern, not introduced by #513. | LOW race | Out of scope for this review. |
| 6 | **Launch path edge cases are honest but weak**: port already taken → REW starts without its API → state sits at `launching` until the deadline → `unavailable` (correct fail-closed, just slow); an `install_path` pointing at a `.bat`/launcher that never opens the API dead-ends the same way. Acceptable — bounded, reported, no fake state. | LOW UX | — |
| 7 | **(mtime_ns, size) signature** — a rewrite that preserves both (e.g. fixed-record overwrite with restored mtime) is invisible. Rare in practice; noted as an accepted limitation. | — observed | — |

## Probe evidence (runtime, not code-reading)

`backend/tests/probe_rev41_rewauto.py` (throwaway): five scenarios run
against the real scan/pool/apply paths — mid-write drop (truncated +
duplicate staging), no-scene-window drop (permanently lost after save),
stage-failure uuid swallow (never refetched), `'seat1'`⊂`'seat10'` bind
(wrong seat assigned), `auto_assign_batch_items` raise (escaped apply →
excepthook). All five reproduced before the fix; the regression tests in
`test_rew_auto.py`/`test_measurement_rew_auto.py` now pin the fixed
behavior (mid-write deferral, unmark-retry, no-scene requeue, token
boundary, assign-guard, start-leak cleanup, busy-count clamp, notice
dedupe).

## Test suite

`pytest backend/tests/test_rew_auto.py test_measurement_rew_auto.py
test_measurement_workspace_composition.py test_workspace_dirty_state.py
test_measurement_job_staleness.py test_rew_snapshot_import.py
test_measurement_workflow_extensions.py test_measurement_journey.py
-q -n 4` (offscreen, unique basetemp) → **92 passed**.
