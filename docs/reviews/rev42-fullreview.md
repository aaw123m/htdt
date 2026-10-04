# REV42 — Second-pass adversarial full-implementation review

Scope: fresh adversarial pass over the entire implementation after
REV24–41's first-pass sweeps — prioritizing what changed since those
sweeps and the seams between recent features: REW auto-connect/ingest/
watch/assign (#513+#516, REV40+REV41), guidance overlay (#514+#515),
journey strips, terms/help registry, measurement batch queue, R150
chains/stochastic/decay provenance, capture supplemental handoffs
(#517) and promoted-authority kinds (#518).

Method: (1) line-level audit of the recently-merged surfaces and their
integration seams (auto-ingest vs quality gates/authority, overlay vs
document switching/contexts, journey strips vs auto-ops); (2) repo-wide
high-risk classes — authority/provenance integrity, fail-closed
regressions, concurrency between background jobs (REW poller, watch
scan) and UI/engine paths, resource bounds, persisted-schema
compatibility; (3) runtime probes (`probe_rev42_fullreview.py`,
throwaway — not committed) confirming each candidate defect before
fixing; (4) scoped pytest of the affected suites (≈275 tests green).

All four probed defects confirmed real; a fifth (unremovable queue
rows) was found during fix design. Five defects fixed with regression
coverage in `backend/tests/test_rev42_fullreview.py` (11 tests).

## Fixed defects (severity-ranked)

| # | Defect | Severity | Fix |
|---|--------|----------|-----|
| 1 | **Uncommitted batch rows invisible to the dirty-state contract** — `before_deactivate`/`dirty_state`/`resolve_dirty_state` only tracked `pending_import`; the batch queue (staged by hand *or* by the REW automation) never gated anything. A document switch, project switch, or exit silently dropped every queued row — including rows the auto-ingest had just promised the user were in the queue. Probe-verified. | **Medium-high** | Uncommitted rows now classify as `pending_import`. `keep_draft` releases the exact rows present at acknowledgement (a newly staged row re-arms the gate, committed rows stop counting); `discard_pending` drops them via the new `discard_batch_items`. The dialog's honest wording already fits: the queue cannot survive a destructive context, so keep is only offered for `navigate`. |
| 2 | **`seen` map in `scan_rew_watch_dir` never evicted** — `pending` keys were pruned but `seen` keys accumulated forever (unbounded growth), and a file deleted then re-dropped with an identical (mtime,size) signature matched its stale marker and was **silently never delivered again** — indistinguishable from "no new drops". Probe-verified. | **Medium** | `seen` markers for files no longer in the directory are evicted each scan (the scanned sentinel stays): the map is bounded by the watched file set and an identical re-drop delivers like a new drop. |
| 3 | **Mid-stage failure leaked queue entries → retry double-staged** — `stage_rew_text_files`/`stage_rew_snapshots` appended `_BatchEntry` rows inside the loop and ran the `_duplicate_names` repository read *afterwards*; any raise left the appended entries in `_batch`, so the auto-ingest retry staged the same files/snapshots a second time → duplicate queue rows (each retry adding more). Probe-verified: failed stage then retry produced 2 rows for one file. | **Medium** | Both stage calls are atomic: appended entries roll back via `_unstage_batch_entries` on any exception (identity-checked, never touches committed entries). |
| 4 | **Unbounded background retries + notice spam** — three instances of the same class: (a) a measurement whose snapshot fetch kept failing was re-requested **every poll tick forever** (~15 s, indefinite); (b) a watch file whose staging kept failing cycled scan→stage→fail every other tick indefinitely; (c) both stage-failure paths called `_operation_error_notice` unconditionally → the error notice re-shouted every tick, clobbering whatever the operator was reading. Probe-verified (a). | **Medium-low** | Per-uuid/per-path failure counts cap at `_REW_AUTO_MAX_ATTEMPTS` (3): a wedged fetch is marked seen and reported once by name; a wedged watch file keeps its seen marker (a later rewrite still re-delivers). Stage-failure notices dedupe through `_rew_auto_notice_keys`, and a failing tick no longer counts as 'quiet' — the keys survive to the next tick so dedupe actually holds. Failure counters prune against the current library/drops. |
| 5 | **No way to remove an uncommitted queue row** — the only discard API was `discard_batch_committed`; a parse-failed or unwanted row could never be committed *or* removed, so auto-ingested noise accumulated permanently (and, with fix 1, blocked navigation with no on-page remedy). | **Medium-low** | `discard_batch_items(item_ids)` controller API + 「選択項目を削除」 per-selection button on the batch card (committed rows are never touched — they name persisted evidence). |

## Seams re-audited — verified clean

- **Auto-ingest vs authority gates**: auto-staged rows go through the
  exact same `commit_batch` normalization + validation as manual
  imports; `auto_assign_batch_items` only pre-fills (unique name-match,
  never overwrites a user choice) and commits stay explicitly
  user-gated. No path mints authority unattended.
- **Overlay vs document switching**: `bind_reflection_guidance` gates on
  `guides_visible` + mode + `current_context=='acoustics'`; the
  overlay's own sqlite connection reports honest failure (REV41-overlay
  confirmed clean; re-verified against the current tree).
- **Journey strips vs auto-ops**: `_refresh_journey` computes
  `staged_pending` from `pending_import or any(status in
  staged/failed)` — auto-staged rows correctly keep the evidence step
  open. Verified.
- **Capture supplemental handoffs (#517)**: `_validate_source_payload_contract`
  rederives supported handoffs and compares canonically; unsupported
  kinds stay preservation-only (hash-bound); non-JSON/non-object/
  schema-less documents fail closed. `sections['supplemental_documents']`
  is always present (only the serialized plan omits the empty section)
  — no KeyError path.
- **R150D decay provenance**: `measured_late_decay_law` fails closed
  without an exact `evidence_ref`; `UNSUPPORTED` artifacts are rejected
  from solver-result envelopes; `execution_id`/provenance derive from
  semantic hashes — no fabricated run records.
- **Mount lifecycle**: `deactivate_rew_auto` stops the poll timer;
  `closeEvent` drains the pool and clears job keys; `_job_completed`
  drops superseded-purpose results before they can stage stale data.
- **Commit path**: `commit_batch` is idempotent (committed entries skip;
  'already exists' + identical payload resumes; mismatched payload
  fails closed); `reuse_existing` never silently becomes an insert —
  an unsatisfied reuse target reports honestly and stays committable.
- **Pending token**: `keep_draft` releases a semantic hash of the staged
  content, not an object id — a re-created equivalent pending cannot
  defeat the acknowledgement (#796).

## Judgment calls — reported, not fixed

| # | Finding | Severity | Notes |
|---|---------|----------|-------|
| 1 | **Stage-failure retry remains unbounded (but quiet)** — a snapshot that fetched fine but keeps failing `stage_rew_snapshots` (e.g. persistently broken repo read) re-fetches + re-stages every tick forever; the fix bounds the *notice*, not the loop. Capping would strand measurements behind a transient repo hiccup — the failing rows lose nothing while queued, and the cap-vs-recovery trade-off is a design call. | LOW residual | Suggested: escalate to the operator after N consecutive stage failures (persistent failure is likely a repo problem, not a measurement problem). |
| 2 | **Auto-assigned rows carry thin-but-honest assignment defaults** — `MeasurementAssignment(measurement_entity_id=…)` leaves `evidence_type='measured'`, `channel_role='unknown'`, no acquisition — identical to what a lazy manual commit produces; fields honestly say `unknown` rather than fabricating provenance. The batch UI lets the user refine before committing. | LOW consistency | Auto-assign could plausibly also infer `measurement_direction` from the entity, but only when that is *provable* — flagged for product intent. |
| 3 | **Restart re-baseline** (carried from REV41, unchanged) — `_rew_seen_uuids`/`_rew_watch_seen`/`_batch`/`_batch_released_ids` are in-memory; measurements taken while the app was closed are treated as pre-existing, and queued rows don't survive restart (the dirty dialog now warns on exit instead of dropping silently). Persistence is a feature, not a defect fix. | Design | — |
| 4 | **`_batch` growth is user-gated but unbounded** — every auto-staged row waits for explicit commit/discard; a high-volume REW session grows the in-memory queue. Bounded in practice by library growth rate and now removable per-selection or wholesale via the dirty dialog. | LOW | — |
| 5 | **Commit/stage same-scan race** (carried from REV41, unchanged) — a commit landing between scan and stage re-classifies the staged row. Pre-existing batch pattern. | LOW race | — |
| 6 | **UI-thread `_batch` mutation vs worker `commit_batch`** — `stage_*` appends on the UI thread while `commit_batch` iterates a snapshot on the pool thread. Verified benign: the snapshot means newly staged rows can't over-commit; `dict.get`/attribute writes are atomic under the GIL; rollback only removes entries by identity and never a committed one. | — verified | No fix needed; documented for future readers. |

## Probe evidence (runtime, not code-reading)

`backend/tests/probe_rev42_fullreview.py` (throwaway): four scenarios
against the real scan/pool/apply/dirty paths — seen-map growth +
identical-signature re-drop loss (P1), mid-stage repo failure → retry
double-staging (P2), permanently-failing fetch retried every tick (P3),
uncommitted batch rows reading `clean`/allowed through the dirty
contract (P4). All four reproduced before the fix; the regression suite
`test_rev42_fullreview.py` (11 tests) pins the fixed behavior, and the
affected suites (`test_measurement_rew_auto`, `test_rew_auto`,
`test_workspace_dirty_state`, `test_measurement_workflow_extensions`,
`test_measurement_workflow_ux130`, `test_round8_measurement_journey`,
`test_measurement_workspace_composition`, `test_round12_dirty`,
`test_import_truth_matrix`) pass under `-n 4` offscreen.
