# REV42 — automation sweep (manual → automated)

Scope: repo-wide audit of user-facing workflows for manual-but-automatable
steps — repeated clicks, manual refreshes, manual re-entry, manual cleanup,
multi-step sequences, things users must remember. VERDICT per checklist
item, implemented items in this PR, honest reasons for everything left
manual. Method: (1) traced every workflow surface (measurement/batch,
inbox, storage, backups, journeys, settings) to its driver; (2) verified
each candidate against the real code path — many "manual" steps already
have an auto-lane; (3) implemented the three real gaps as fail-closed
background lanes with tests and a real-GUI pass.

## Automated in this PR

| Item | Manual step before | Automation now | Gate honesty |
|------|--------------------|----------------|--------------|
| **A — In-session periodic backup re-drive** | `AutomaticBackupRunner` evaluated `periodic` **once per launch** — a multi-day session never produced another generation even when `interval_hours` elapsed (contradicting `record_clean_close`'s "the next eligible scheduler tick" and the automatic_backup.txt design). | `QTimer` re-drive (15 min cadence) on the same `NativeWorkerPool`; ticks absorbed while a job is in flight; a `pool.start` raise cannot wedge the guard; `shutdown()` stops the timer. | Only a *real* run surfaces an Activity entry + statusbar — quiet evaluations stay silent, so no per-tick nagging. Backup policy unchanged (interval/keep generations). |
| **B — Scheduled storage integrity scan** | Inventory existed only if the user opened データ管理 and clicked scan — `missing_referenced` and `reclaimable_bytes` never surfaced by themselves. | `StorageWatchRunner` drives `plan_storage_gc` on a worker every 4 h; **only reportable results** (missing refs, or ≥ 64 MiB reclaimable) produce one Activity entry + statusbar line; failures surface once via `scan_failed`. Opt-out pref `maintenance.storage_watch_enabled` (default on) under a new メンテナンス category, re-read every tick. | **Read-only** — deliberately bypasses `DataManagementController.scan_storage` (its completion signal pops a `QMessageBox` = nagging dialog); `run_storage_gc` stays behind its manual confirm. Nothing is ever deleted by this lane. |
| **C — `.htdtcapture` watch-folder** | A bundle exported to disk reached the inbox only via relaunch arg or manual open. | `integrations.capture_watch_dir` pref (opt-in, empty = off) + `CaptureWatchRunner` (10 s poll): baseline marks pre-existing files seen-never-staged; two-scan `(mtime,size)` signature settle; routes through `route_capture_intent` (ingest → **inbox stage**, never promote) on a worker job; `arrival_source='watch_folder'` provenance; Activity entry deep-linked to inbox + statusbar + live inbox refresh. | Same contract as `integrations.rew_watch_dir` (REV41): opt-in path, settle-before-read, stage-not-promote. Junk drops report `failed` once, never become inbox rows, never kill the lane. |

## Already automated (audit verdicts — no change)

| Candidate | Verdict |
|-----------|---------|
| REW poll/ingest/assign/watch-dir | Shipped in PR #513/#516 — `_rew_auto_tick` loop, `scan_rew_watch_dir`, `auto_assign_batch_items`, `rew_auto_*`/`rew_watch_dir` prefs. |
| Backup due-check at launch | `start_automatic_backup` already ran a one-shot check (item A adds the missing re-drive). |
| Optimization/measurement list refreshes | Refresh on state change signals; buttons are fallback affordances. |
| Batch commit idempotent/resumable | `commit_batch` retries in place with revision pinning; partial progress persists. |
| Window layout | `PersistedWindowState` save/restore hooks on close/launch. |
| REW install auto-detect | `find_rew_install` probes standard paths; `rew_install_path` is the override. |
| Capture receiver staging | `CaptureReceiverService` + pairing already auto-stage deliveries into the inbox. |

## Stays manual — and why

| Step | Why manual is honest |
|------|----------------------|
| Batch commit (`commit_batch`) | Explicit evidence gate — the user reviews auto-assignment before persist. Auto-committing would write unreviewed measurement bindings to evidence. |
| Storage GC delete (`run_storage_gc`) | Destructive unlink under confirm dialog — scheduling deletes is exactly the class of automation the task forbids bypassing. |
| Journey/wizard auto-advance | Each step is already one click of honest navigation; auto-opening the next page would hijack user context and skip reading. |
| Manual refresh buttons | Fallbacks for when the auto-lane is off or stale — removing them removes the honest override. |
| REW host/port | Loopback-only by contract — there is no safe auto-detect beyond the persisted defaults. |
| Pre-existing files in watch dirs | Baseline-skip is the REW contract: the folder watches *new drops*, not a bulk import — surprise-ingesting stale files would be worse than the manual re-drop. |

## Judgment call — reported, not fixed

**Measurement quality reports have no production producer.**
`cad_measurement_quality.build_measurement_quality_report` /
`save_report` are only exercised by tests; nothing in the app ever
produces one, so every measurement's quality state stays
`missing`/`quality_pending` and the campaign cell can never complete.
Honestly auto-producing a report needs an evidence-derivation design —
what the app may assert from REW metadata/raw assets vs. what still
needs operator judgment — which is a dedicated feature, not a sweep
fix. Flagged for the campaign rather than half-implemented.

## Evidence

- Scoped pytest (per inv):
  `TMPDIR=/c/t PYTHONIOENCODING=utf-8 QT_QPA_PLATFORM=offscreen C:/devin/python/python.exe -m pytest backend/tests/test_automatic_backup_runner.py backend/tests/test_storage_watch_runner.py backend/tests/test_capture_watch_runner.py backend/tests/test_application_preferences.py backend/tests/test_launch_router.py backend/tests/test_rev24_ux_surfaces.py backend/tests/test_review_round13_settings.py backend/tests/test_capture_inbox.py backend/tests/test_workflow_application.py -q -n 4 -p no:warnings` → **136 passed**
- New regression coverage: periodic re-drive + no-overlap + shutdown-stop + disabled-pref (A/B), scanner baseline/settle/redeliver + end-to-end stage with `watch_folder` provenance + junk-reject + disabled/missing-dir (C).
- Real-GUI pass (`python -m htdt.native_cad`, Mesa GL): automatic `automatic_periodic` backup appeared in the バックアップ list; new prefs render in 環境設定 (キャプチャバンドルの監視フォルダー under 連携, メンテナンス section); a `.htdtcapture` dropped into the configured watch dir staged an inbox row (`保留中`, 未割り当て scope, `arrival_source=watch_folder`) — verified in the アクティビティ timeline and the 取り込み page. Screenshots in the PR body.
