# Round 20 — REV20-FIRST (first-run / fresh-install journey)

Scope: walk the getting-started path of `htdt.native_cad` (the shipped GUI
entry, `python -m htdt.native_cad` / HTDT.exe) as a brand-new user on a
clean machine: zero-state launch, first project creation, empty-app UX,
sample/tutorial path, missing optional deps, packaged-vs-dev differences,
and the second-run/restart path. Verified dynamically by clearing state
(temp data dirs, corrupted payloads, edge-path probes driving a real
`main()` offscreen) — Windows Server 2022, `QT_QPA_PLATFORM=offscreen`,
Python 3.12.10 NuGet, `pytest backend/tests -q -n 4`. Branch
`devin/rev20-first`.

**Verdict: CONVERGED for this dimension after this diff.** The 19 prior
rounds left the first-run surface almost entirely hardened — every staged
probe behaved honestly except two real gaps, both verified by reproduction
and both fixed here: (1) a *file* occupying the managed data directory
crashed the launch with a raw `FileExistsError` traceback instead of the
localized failure dialog; (2) quitting during the first-launch automatic
backup detached the worker past its shutdown budget and killed the QThread
mid-write, leaving a torn `htdt-backup-*` staging dir plus
`QThread: Destroyed` / `WinError 32` stderr noise.

## Journey ledger

| Path | Result |
|------|--------|
| Zero-state GUI launch (`--data-dir` on empty dir) | OK — creates `.instance.lock`, `cad-scenes.sqlite3`, `capture-receiver/` certs, `diagnostics/`, `launch-intents/incoming/`, `measurement-assets/`, `window-state/`; opens the default project "My Home Theater" (README-documented name); clean `RC=0`, no dialogs |
| Missing optional files on first launch | OK — `application_preferences.json` absent → documented defaults; no settings/recent-files migration crashes; `htdt-launch-build.json` written post-launch |
| First project creation (empty library) | OK — `resolve_startup_document` creates `DEFAULT_PROJECT_NAME` with a registered document; library list shows JA empty-state label ("まだプロジェクトはありません。…「新規プロジェクト…」") with live tooltips on disabled buttons |
| Empty-app UX elsewhere | OK — Projects page empty state + disabled-button tooltips verified; overview/workspace render the seeded empty project without dead screens |
| Sample/tutorial path (`--seed-synthetic-demo`) | OK — seeds the synthetic optimization demo; next GUI launch opens it via last-opened resolution; CLI prints the dev-only honesty line "合成デモは開発専用で…" |
| Missing optional deps | OK — `h5py` (SOFA), `pymupdf` (PDF underlay), `yaml` (CamillaDSP) are all lazy imports inside the operation with typed JA errors (`UnderlayImportError`, camilladsp `ImportError` wrapper); zero-dep launch unaffected |
| Hardware/network absence | OK — REW unreachable degrades via `RewApiUnavailable` → mapped JA message; capture receiver cert generation is local and runs on first launch |
| Packaged-vs-dev | OK — `python -m htdt` (retired browser API) refuses honestly without `HTDT_LEGACY_API` (rc=2, JA message); `build_info` version resolution has a packaged `_MEIPASS` branch; no dev-machine-only paths in the shipped launch chain |
| Second run: window/project persistence | OK — `window-state/<project_ref>.json` persists per project; corrupt/absent → defaults; legacy global fallback read; relaunch reopens the last-opened project |
| Second run: unclean shutdown | OK — `recovery-launch-metadata.json` recorded at launch; a session without `complete_launch` triggers the localized recovery dialog (セーフモード/通常起動/初期化), verified by probe |
| OS-intent queue | OK — malformed `.json` drop goes to `dead/`, stale (>900s) intents expire to `dead/`, launch unaffected |
| Corrupt database file | OK — localized launch-failure dialog, `RC=1`, no traceback |
| `--data-dir` pointing at a *missing* file path | OK — warning dialog (「ファイルが見つかりません」), app continues with startup document |
| Maintenance modes on fresh/empty dir | OK — `--backup` on a dir with no DB fails honestly (`FileNotFoundError` → stderr, rc=1); `--restore` into a brand-new dir creates and populates it (schema=1, files=6 verified); `--automatic-backup`, `--revalidate`, `--migrate-legacy-data` all follow the same nonzero+stderr contract |
| **`--data-dir` occupied by a file** | **BROKEN → FIXED** — `assert_managed_root_available` only rejected a non-directory root for `source == 'bootstrap'`; explicit/default roots passed through and `SingleInstanceGuard.acquire()`'s `mkdir(exist_ok=True)` raised a raw `FileExistsError` traceback with `RC=EXC`, no dialog |
| **Quit during first-launch backup** | **BROKEN → FIXED** — `AutomaticBackupRunner.job` never forwarded the pool cancel event into `run_due`/`create_backup`; worse, the dominant stage (~15s authority-graph audit, run twice per backup) had no cancellation seam at all, so even a threaded flag could not land inside the 1.8s shutdown budget |

## Fixed

| File | Change |
|------|--------|
| `data_relocation.py` | `assert_managed_root_available` now rejects `root.exists() and not root.is_dir()` for **every** source (explicit/default/bootstrap) with `ManagedDataUnavailableError` — the JA launch-failure dialog ("管理データにアクセスできませんでした") or maintenance stderr+rc=1 handles it |
| `native_cad.py` | `guard.acquire()` wrapped in `try/except OSError` → same unavailable-root failure lane (dialog for GUI, stderr+rc=1 for maintenance) — covers permission/read-only-fs cases the existence check cannot |
| `automatic_backup.py` | `run_due()` gains keyword-only `is_cancelled: Callable[[], bool]`, forwarded to `create_backup` |
| `automatic_backup_runner.py` | `job` passes `is_cancelled=_cancel.is_set` — pool `cancel_all()` now reaches the copy |
| `native_backup.py` | Per-chunk cancel seams added inside the dominant I/O: `_copy_file_cancellable` replaces the three `shutil.copyfile` sites; `_write_zip_member` replaces `archive.write` (same `ZipInfo.from_file` + compresslevel metadata, streamed in 4MB chunks); `_validate_asset_contract` and `_build_manifest` poll per row; `is_cancelled` forwarded into both `audit_native_authority_graph` calls |
| `native_authority_audit.py` | `audit_native_authority_graph` gains keyword-only `is_cancelled`, polled per row in all five replay loops (replay probes, ingestion runs, asset tables, content blobs, structural payloads) via a lazy `native_backup` import — the audit was the true long pole: ~15s/pass on a real project, twice per `create_backup` |

## Verified after fix

- `datadir-is-file` probe: `RC=1` + dialog "HTDTのデータディレクトリを開けません / 管理データにアクセスできませんでした / 保存先のドライブやフォルダを確認して…" (was `RC=EXC FileExistsError [WinError 183]`).
- Quit-at-2.5s during first-launch backup over an 800MB asset dir + demo DB (32s `create_backup` cold): worker cancelled inside budget — `RC=0`, empty `-backups/` (staging unwound by `TemporaryDirectory` `__exit__`), no `QThread: Destroyed`, no `WinError 32` in the run's log section (pre-fix run showed both plus a torn `htdt-backup-*` dir).
- `backend/tests/test_first_run.py` (new, 7 tests): file-occupied root rejected for all 3 sources; `main()` GUI + `--revalidate` maintenance over file-root → dialog/stderr + rc=1; `run_due(is_cancelled=…)` raises `BackupCancelledError` with zero archive/staging residue (immediate and mid-pipeline flip); runner threads the pool event into `run_due` and swallows the cancelled completion.
- `test_automatic_backup_runner.py` stubs updated for the new keyword (the runner now passes `is_cancelled=`); all existing backup/launch/relocation/audit suites green (`-n 4`).

## Deferred (correctly — none are first-run blockers)

- `_snapshot_database` (SQLite C-level backup call) and `ManagedAssetStore.read_file` inside the audit stay un-seamed: whole-call units bounded by a single DB/asset copy — a >1.8s single read is the designed detach fallback, not new exposure.
- `--backup` on an empty data dir reports `FileNotFoundError: native database does not exist: <path>` — honest rc=1, but the English class name reaches stderr verbatim via `concise_reason`; a JA-mapped message would polish the maintenance UX. Cosmetic.
- Detached-worker teardown is inherent: a task that ignores its cancel flag past the budget is still killed at interpreter exit (Qt limitation) — now unreachable through the backup path, still the documented design for other pools.
- `DEFAULT_PROJECT_NAME = 'My Home Theater'` stays English in a JA UI — verbatim-documented product name in README, not a gap.
- `PENDING_PREFERENCE_KEYS` (`startup_destination`, `reopen_last_project`) render disabled as 「準備中」 — honest placeholder, not a lie.
