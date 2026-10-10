# Issue #985 — 3Dレビュー・提案パッケージ生成をUIスレッドから分離

## Scope

`PresentationWorkspace._build_review` ran `OffscreenSceneRenderer`
construction, `build_review_package`, and `verify_review_package`
synchronously inside a `clicked` handler; `_build_proposal` did the same
for `build_proposal_package` + `verify_proposal_package`. For a session
with many viewpoints and yaw-stepped frames this froze the Qt event loop
for the entire build — no progress, no cancel, no safe retry.

This change moves both jobs onto a bounded `NativeWorkerPool` lane (the
existing project-bundle-export / `SupportHealthRunner` pattern — reused,
not re-implemented), registers each run in the shared `ActivityCenter`,
and restructures output publication so a cancelled/failed/killed build
can never leave a directory that looks like a finished package.

## What changed

| File | Change |
|---|---|
| `backend/src/htdt/package_progress.py` | **New.** `PackageBuildProgress` — a measured observation (`stage_label`, 1-based `stage_index`/`stage_count`, `done_units`/`total_units`/`unit_label`, cumulative `bytes_written`). `ExportCancelledError` — cooperative-cancel signal raised inside a builder. `ProgressCallback` — the callable signature builders accept. No estimated percentages, no ETAs. |
| `backend/src/htdt/cad_review_package.py` | `build_review_package` gained optional `progress` and `cancel_event` kwargs (no-ops for existing synchronous callers). Emits stage rows 権威の解決→フレーム描画→図面生成→セマンティック/ビューア→マニフェスト with ITEMS progress per attempted frame (done/total `フレーム`) and per sheet (`シート`); cancel is polled per frame and per sheet. Stage index space reserves 6/6 for the caller-side verify step. |
| `backend/src/htdt/cad_proposal_package.py` | Same two kwargs; stage rows 権威の解決→図面生成→比較・決定→文書・マニフェスト (5 stages, stage 5 = caller-side verify). |
| `backend/src/htdt/presentation_export_runner.py` | **New.** `PresentationExportRunner(QObject)` — mirrors `SupportHealthRunner`'s contract (`start`→bool busy gate, `request_cancel`, `shutdown`, Qt signals for started/progress/completed/failed/cancelled/finished). Owns a `NativeWorkerPool`. `PresentationExportJob` — frozen pin of session, package_dir, output_root, yaw steps, expected items. `_publish_staged` — atomic rename. |
| `backend/src/htdt/presentation_workspace.py` | `_build_review`/`_build_proposal` now pin a `PresentationExportJob` on the UI thread and hand it to the runner. New 「出力を中止」 button (visible only while a job runs), busy-gate on double-clicks, per-progress status text (stage i/n + units + bytes written), non-blocking completion/failure/cancel notification, historical-result note, `closeEvent` drains the runner. |
| `backend/src/htdt/workflow_application.py` | `_make_presentation` passes the shared `activity_center` through. |
| `backend/tests/test_issue_985_async_export.py` | **New.** 12 tests (below). |

## Design

### Threading model / VTK affinity

`OffscreenSceneRenderer` is constructed **inside** the worker lane by a
`renderer_factory` invoked from the `NativeWorkerPool` callable — the
renderer and its `pv.Plotter` frames never exist on the UI thread and a
live `QtInteractor` is never touched cross-thread. Tests inject a stub
renderer through the same factory, so the tested path is the real path.

### ActivityCenter registration

Each `start` submits one operation before the lane spins up:

- `operation_kind`: `presentation.export.review` / `.proposal`
- `operation_class`: `EXTERNAL_IO`
- `cancellability`: `CANCEL_UNTIL_COMMIT` with a live `cancel_callback`
  into `pool.cancel(task_key)`
- `retry_policy`: `SAFE_NEW_ATTEMPT` — a re-run validates a fresh
  `package_dir` and rebuilds from scratch, so retry is safe by
  construction
- `navigation_policy`: `BACKGROUNDABLE` — the operator may keep working
- `deep_link`: presentation workspace → `export` section, pinned
  `revision_id`
- `input_authority_refs`: `presentation-session:<sha256>` +
  `scene-revision:<rev>:<hash>` — the sealed inputs the job consumed

Progress is relayed through a `Signal(object)` (worker thread → queued
delivery → UI thread) and mapped to `OperationProgress`: `ITEMS` with
real `done/total` + `unit_label` while frames/sheets flow, `STAGE` for
fixed stages. The builder only ever reports units it actually processed;
a failed frame still counts as *processed* and lands in `warnings`.

### Staged build → verify → atomic publish

1. The worker creates `.<package>.staging-<rand>` as a sibling of the
   final `package_dir` (same filesystem → the publish step is a single
   rename).
2. The builder writes the whole package into the staging dir.
3. The worker re-verifies the package (`verify_*_package` re-hashes
   every manifest entry) — verification failure discards the staging
   tree, nothing is published.
4. On the UI thread, the completion handler honors a cancel request that
   landed before publication, then `mark_commit_point` → `os.replace`
   (staging → final). From here cancel is no longer offered.
5. Any exception — build error, verify mismatch, `ExportCancelledError`,
   OSError (disk full), renderer factory crash — removes the staging
   tree; the final name only ever names a complete, verified package.
   A crash that abandons a staging dir leaves a `.<name>.staging-*`
   folder that can never be mistaken for output.

### Pin + attribution

`PresentationExportJob` is frozen at click time: the sealed
`PresentationSession` (session id + `session_sha256`), its pinned
`scene_revision_id`/`scene_content_hash`, the render settings (via the
session), the validated `package_dir`, and the expected frame/sheet
counts. Re-selecting a different session in the combo mid-run does not
re-attribute the result — the operation row keeps its `document_ref`,
`revision_ref` and deep link to the pinned revision.

On completion the runner calls `session_stale(session)`: if the pinned
revision is no longer the document head, `note_authorities_changed`
reclassifies the record as `COMPLETED_FOR_HISTORICAL_INPUT` and the
status line adds 「旧リビジョンの結果として保持」 — an old-revision
package is never silently presented as current.

### Diagnostics (#962)

Failure emits `failure_correlation_id()` (`[diag: XXXX]`), stored as the
operation's `diagnostic_id` and included in `error_summary` — the same
correlation id convention the support-bundle collector consumes.

### Busy gate / re-run

`runner.busy` refuses a second `start` while a job is in flight, and the
page disables both build buttons for the same window — double-clicks
cannot spawn parallel builds. After any terminal state a fresh run is
allowed (SAFE_NEW_ATTEMPT semantics).

### Close / project switch

`PresentationWorkspace.closeEvent` → `runner.shutdown()`: cooperative
cancel + bounded pool drain; a worker that ignores the flag past the
timeout detaches to module ownership but still runs its own staging
cleanup before exiting. The operation record ends `CANCELLED`, never
`RUNNING`-forever.

## Honesty notes

- No `%` or ETA is ever shown — only measured `i/n` stage counts,
  `done/total` unit counts, and cumulative bytes written.
- UNKNOWN stays neutral: an unavailable renderer still produces an
  honest `unavailable` capability row inside a successful package, not a
  red failure.
- `expected_frames`/`expected_sheets` shown at start are the *pin* — the
  manifest's actual `render`/`drawing` entry counts are authoritative.
- Output contents/manifest/evidence format are unchanged — same
  builders, same sealed authority, same verifier.

## Tests

`backend/tests/test_issue_985_async_export.py` (offscreen, stub
renderer injected via the runner's factory):

- off-UI-thread execution (render thread ≠ main thread) + atomic publish + clean verify
- measured stage/ITEMS progress rows land on the ActivityCenter record
- 40 viewpoints × 9 yaw frames = **360-frame** build with correct totals
- cancel mid-render → `CANCELLED`, no package dir, no staging leftovers
- cancel racing publication → result discarded, still `CANCELLED`
- renderer-factory crash → `FAILED` + `[diag: …]` id, staging cleaned
- disk-full mid-write → `FAILED`, staging cleaned
- verify failure → `FAILED`, nothing published
- duplicate start refused; re-run allowed after settle
- stale pinned revision → `COMPLETED_FOR_HISTORICAL_INPUT`
- proposal package end-to-end
- `shutdown()` mid-run drains and cancels (project-switch path)
- `_publish_staged` atomicity: fresh target, empty-target replace,
  non-empty-target refusal preserving staging for cleanup

## Follow-ups / known limits

- Real Win11 UI-responsiveness measurement vs #867 thresholds belongs to
  the GUI pass on this PR (Mesa GL box); unit tests prove the lane is
  off-thread but cannot measure human-perceived latency.
- A process kill mid-build leaves a `.<name>.staging-*` directory — by
  design it is visibly a temp artifact, not a package; a future sweep
  could remove stale staging dirs on app start.
