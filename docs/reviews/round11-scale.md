# Round 11 — scale & realistic-volume behavior

Scope: how the app behaves at real user volumes — ~100 projects, ~500 pending
REW files, thousands of measurement rows, repeated page visits, long sessions.
Prior rounds read first (`round6-perf`, `round9-optimizer`, `round9-viewport`);
their findings are not re-reported. Branch `devin/rev11-scale`.

Method: throwaway instrumentation scripts (not committed) counting
`sqlite3.connect`, `check_native_schema_compatibility`, importer replays,
scene-revision materializations, managed-asset reads+bytes, wall time and
tracemalloc peak — on a Windows box (`QT_QPA_PLATFORM=offscreen`,
`C:/devin/python/python.exe`). Fixtures used `make_f1_scene` + real
`stage_rew_text`/`commit_pending` writes so every row carries the full
import-transformation binding (seals + managed asset + pinned importer
replay), i.e. the expensive authoritative-read path is exercised, not a
short-circuit.

Constraint honored throughout: the repository's fail-closed contract —
*every* authoritative read re-verifies persisted seals, resolves the
content-addressed raw asset and re-runs the pinned importer
(`cad_measurement_repository._dataset_and_asset` docstring: "fail closed on
every authoritative read rather than cache integrity state"). No fix below
caches integrity state across calls; verification is memoized only within a
single outer operation.

## Measured before/after (N=60 committed measurements, Windows dev box)

| Operation | Before | After |
|---|---|---|
| `measurement_views()` ×1 | 7 665 ms · 604 conns · 181 compat checks · 60 replays · 120 asset reads · 270 MB | 3 644 ms · 9 conns · 4 compat · 60 replays · 60 asset reads · 270 MB |
| `refresh()` (workspace) | up to 9× views ≈ 69 s | 1× views ≈ 3.6 s |
| `stage_rew_text_files` 30 files | 126.8 s (at N=30) | 0.075 s · 6 conns (at N=60) |
| `stage_rew_text_files` 60 files | 501.6 s | ~0.15 s |
| `batch_items()` 30 items | 101.8 s | 0.013 s · 3 conns |
| `commit_batch` 30 items | blocking UI, no progress/cancel | 0.13 s · 34 conns, worker thread + progress + cancel |
| `list_projects()` ×100 | 10.3 ms · 2 conns — already fine | unchanged |

Spot-check at N=300 committed measurements (same box, after-state):
`measurement_views()` = 20 919 ms · **9 conns** · 300 replays · 300 asset
reads · 274 MB — connection count is flat in N (the ~70 ms/row wall time is
the importer-replay floor, finding #8). `stage_rew_text_files(300)` =
704 ms · 6 conns; `batch_items()` = 26–38 ms · 3 conns; `commit_batch(300
reuse)` = 1 123 ms · 304 conns (write path stays ~1 conn/item — bounded and
linear, with progress callbacks every item); `commit_batch(120 new
imports)` = 510 ms · 124 conns.

## Verdict table

| # | Finding | Severity | Verdict |
|---|---------|----------|---------|
| 1 | `measurement_views()` fanned out per row: each of N rows paid its own `dataset_for_measurement` (connect + full verify), `latest_correction`, `latest_disposition`, `latest_report`, `list_attachments` and a `cad_measurement_assets` row lookup inside `validate_raw_asset` — ≈10 connections per measurement (604 at N=60), plus one `check_native_schema_compatibility` ro-connection per repository method (181). | HIGH throughput | FIXED — new document-scoped batch reads: `datasets_for_document`, `latest_corrections`, `latest_dispositions`, `latest_reports`, `attachments_for_document`, plus per-operation `datasets`/`measurements`/`revisions`/`bound`/`asset_rows` verify memos threaded into every validator (`validate_raw_asset`, `dataset_for_measurement` hit them instead of opening a connection). Fail-closed preserved: each dataset still verified once *per listing*. Per-row per-measurement lookups remain as stand-in fallbacks. 604→9 conns — flat at N=300. |
| 2 | `MeasurementPageWorkspace.refresh()` ran `measurement_views()` up to 9 times — `_refresh_batch` (labels), `_refresh_campaign` (combo + labels), `_refresh_quality`, `_refresh_comparison_choices` (up to 4 `comparison_candidates()` calls), `_preview_comparison_pair` (candidates + `comparison_mismatches`). At N=60 one refresh ≈ 69 s. | HIGH UX | FIXED — `refresh()` computes `views`/`batch_items` once and threads them through all sections; `comparison_candidates`/`comparison_semantics`/`comparison_mismatches`/`compare_datasets`/`_resolve_comparison_sides`/`_view_by_dataset` accept an optional `views=` listing. Event handlers that need freshness still get it via default `views=None`. |
| 3 | Batch staging/listing/commit was O(staged × document): `_classify_duplicate` ran two indexed queries per file and `_batch_item_view`'s `duplicate_of_name` resolved through `measurement_views()` — a *full verified listing per staged item* (501 s to stage 60 files at N=60; projected hours at 500). | HIGH throughput | FIXED — `_duplicate_source_maps()` builds `source_sha256`/`external_source_id` → measurement_id maps once per batch op (new repo maps `measurement_ids_by_source_sha256`, `measurement_ids_by_external_source`); `_duplicate_names()` resolves labels once via `list_measurements` + `latest_corrections` + memoized `scene_repository.get` — no dataset verification at all. Shared by `stage_rew_text_files`, `stage_rew_snapshots`, `batch_items`, `commit_batch`. |
| 4 | `commit_batch` ran synchronously on the UI thread — minutes frozen at hundreds of files, no progress, no cancel. | HIGH UX | FIXED — commit moves onto the existing `NativeWorkerPool` (`_start_job` infra): controller `commit_batch(cancel_event=, progress=)` checks the event between items (each item commits atomically — partial results kept, honest cancel), emits `(done,total)` progress, workspace shows live `保存しています… n/total`, a `保存をキャンセル` button, and disables batch buttons + double-launch while the job runs. `before_deactivate`/`dirty_state`/`closeEvent` already gate on `active_count`. |
| 5 | `check_native_schema_compatibility` opened its own ro-connection on every repository method call — 5+ per `measurement_views`, hundreds across a session. | MED throughput | FIXED — result memoized in `_COMPATIBLE_SCHEMA_SIGNATURES` keyed by `(path, file signature)` — the same invalidation contract `_ENSURED_SCHEMA_SIGNATURES` already uses: any committed write mutates mtime/size and forces a re-check. 181→4 checks at N=60. |
| 6 | Every dataset verification paid **two** full file reads: `verify_managed_asset` streamed a SHA-256, then `_read_verified_asset` re-read and re-hashed for TOCTOU. 120 asset reads per listing at N=60. | MED I/O | FIXED — new `read_managed_asset_verified` performs the identical checks in one pass and returns the hashed bytes (strictly stronger: bytes hashed = bytes used, no window). `_dataset_and_asset`, IR dataset verify and `verify_measurement_asset_authority` use it via `_asset_and_bytes_for_*`. 120→60 reads. |
| 7 | Hot duplicate-detection columns lack indexes: `cad_frequency_responses.source_sha256` (dup scan) and `cad_measurement_corrections.document_id` (document-scoped listing) have no index; `cad_measurement_quality_reports`/`cad_measurement_observations` have no `document_id` column at all (batched reads join through `cad_measurements`). | LOW | DEFERRED — needs a `NATIVE_SCHEMA_VERSION` migration; post-fix queries are one indexed join per listing, so remaining gain is at very large N. Sketch below. |
| 8 | The listing's remaining ~60 ms/row is the *deliberate* fail-closed importer replay (60 replays = ~3.6 s at N=60 ≈ 30 s at N=500). | MED | DEFERRED — cannot be skipped without weakening the evidence contract; needs a background refresh worker and/or a revision-signature-keyed read model. Sketch below. |
| 9 | `refresh()` rebuilds the quality table (`setRowCount(N)` + per-cell `QTableWidgetItem`) and every combo each visit. At hundreds of rows this is visible CPU churn, though no longer catastrophic now the listing is shared. | LOW | DEFERRED — targeted row-diff updates; bounded by the same refresh-worker work as #8. |
| 10 | `commit_batch` progress/cancel now exists for batch commits; other heavy UI-thread paths (single `commit_pending`, staging >500 files' read loop, compare replay) remain synchronous. | LOW | DEFERRED — same worker pattern applies when volumes justify it; staging reads are bounded file I/O, not verification. |

## What already works (verified, not re-fixed)

- **Project library at ~100 projects** — `list_projects()` is one indexed
  query: 10 ms / 2 conns; no per-row fan-out.
- **Long-session hygiene** — the 800 ms launch-intent drain is a single
  bounded `QTimer`; sqlite connections are `closing()`-scoped per call;
  `NativeWorkerPool` threads release on `finished`; no accumulating
  observers/listeners found on repeated project open/close.
- **DB growth gates** — `integrity_problems()`/schema scans run at
  startup/migration only, not per page visit; the ensured-schema signature
  memo already skips re-validation once a file is proven.
- **Batch listing** — `batch_items()` uses `_batch_item_view` projections
  (no dataset verification by design) — now actually cheap end-to-end.
- **Cancellation semantics** — `WORKER_CANCELLED` is a first-class
  completion; cancel between items leaves already-committed items persisted
  (each item's save is atomic) and uncommitted items stay staged — matches
  the batch page's "失敗・未保存の項目は一覧に残っています" contract.

## Deferred (sketches)

- **Indexes (#7).** Next `NATIVE_SCHEMA_VERSION` bump: add
  `idx_frequency_responses_source_sha256` and
  `idx_measurement_corrections_document_seq` style coverage; consider a
  `document_id` column (denormalized, backfilled) on
  `cad_measurement_quality_reports`/`cad_measurement_observations` so
  document-scoped reads skip the join.
- **Listing workerization (#8).** `measurement_views()` onto the
  `NativeWorkerPool` with per-`refresh()` generation stamps, or a persistent
  view model invalidated on write (repository events already exist around
  saves). The 60 ms/row replay floor is the reason verification must stay —
  the win is taking it off the UI thread and not redoing it per section,
  which this round already halves again via shared listings.
- **Table diffing (#9).** Keep per-row `QTableWidgetItem`s and patch cells
  whose view changed (measurement_id → item map), instead of
  `setRowCount` + full repopulation; same for campaign/comparison combos.

## Tests

`backend/tests/test_review_round11_scale.py` (7 tests): bounded-connection
contracts for `measurement_views`/`stage_rew_text_files`/`batch_items`/
`commit_batch` (sqlite connect counter — asserts no per-row fan-out),
overlay-field equivalence (correction/disposition/attachment preserved
through the batched path), zero `measurement_views` calls during staging,
commit progress callbacks + cooperative cancel between items, and a single
authoritative listing per workspace `refresh()`.
