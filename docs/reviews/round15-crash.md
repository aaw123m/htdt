# Round 15 — crash / power-loss safety depth

Scope: every multi-step write with a bisectable commit point — `SceneRepository.save`
(revision commit), `capture_ingestion_transaction.ingest` (bundle import),
`capture_receiver.handle_delivery` (ingest → stage → delivery-ledger chain),
`capture_inbox.stage`, `launch_router.route_capture_intent` (file-open lane),
`native_backup` `create_backup` / `restore_backup`, `cad_schema.ensure_native_schema`
(migration chain), `legacy_data` archive swap, `data_relocation` move journal,
`installation_handoff` package write, application-preferences / journal / auto-backup
state files, launch-intent queue files, and `runtime_instance` locks. Method: real
fault injection, not code-reading — a self-re-spawning harness kills child processes
with `TerminateProcess` inside named windows (mid-transaction, inside `commit()`,
inside a migration step, inside the journaled restore swap), truncates artifacts at
their halfway byte, and leaves staging dirs / `.*.tmp` files behind; restart behavior
is then observed, not assumed. Python 3.12, Windows, pytest `-n 4`,
`QT_QPA_PLATFORM=offscreen`. Branch `devin/rev15-crash`.

## Crash contract as verified (injection results)

| Mechanism | Injected fault | Observed behavior | Verdict |
|---|---|---|---|
| `SceneRepository.save` — revision insert + head advance in one `BEGIN IMMEDIATE` | `TerminateProcess` mid-transaction (hot rollback journal left) | Reopen: `integrity_check=ok`, zero partial rows — whole logical write rolled back | Honest |
| `ingest()` — single txn covering run + evidence + authority rows | Kill inside `commit()` (proxy connection) | `runs=0, evidence=0`, integrity ok — nothing half-committed | Honest |
| `ingest()` | Kill one instruction *after* `commit()` | `runs=1, evidence=6` — fully durable | Honest |
| `handle_delivery` — ingest commit → stage commit → ledger commit | Kill between ingest and stage commits | Run + evidence durable, zero inbox rows, zero ledger rows, `list_items()` empty — delivered bytes are invisible forever; retention purge can later delete an unseen capture | **Fixed**: `reconcile_orphaned_ingestions()` restages orphans (see below) |
| `handle_delivery` | Kill between stage commit and ledger write | Redelivery dedups at the bundle level and rewrites the ledger row (`accepted`); item already visible | Honest (at-least-once) |
| `route_capture_intent` (file-open lane) | Same ingest→stage window | Same orphan mechanism; reconcile now runs before each routed import | **Fixed** (shared fix) |
| `ensure_native_schema` migration chain | Kill inside `_MIGRATIONS[7]` | Stored `schema_version=5` — the marker is written last per step, but convergence helpers' `executescript` commits earlier steps mid-chain; resume from boundary reaches v10, `integrity_check=ok`, ledger rows unique | Honest outcome, **docstring oversold** — "Atomically migrate" corrected to describe boundary resume |
| `restore_backup` — staged extract → validate → journaled swap | Kill inside the `live_evacuated` journal phase | `.data-restore-rollback-*` dir + journal survive; `recover_interrupted_restore` on next open completes the swap to the validated restored state; live db `integrity_check=ok` | Honest |
| Restore / backup staging residue | Orphan `htdt-restore-stage-*`, `htdt-backup-*` dirs and `.*.tmp` files | **Before fix**: survived forever — junk accumulates silently (never reads as valid, but never cleaned either) | **Fixed**: age-guarded sweeps on open and next backup |
| `create_backup` vs in-flight write | Snapshot taken while a second connection holds an uncommitted txn | `sqlite3.Connection.backup()` yields the pre-txn snapshot; post-commit backup has all rows — never a torn mix | Honest |
| `SingleInstanceGuard` `.instance.lock` | Holder killed (TerminateProcess) | OS byte-range lock released by kernel; new holder acquires immediately; stale advisory metadata ignored | Honest |
| `runtime.json` (owner metadata) | Truncated mid-file | `read_runtime_info` → `None`; surfaced by support diagnostics as unclean-exit evidence | Honest |
| `.htdtcapture` archive delivery | Truncated at half its bytes | HTTP 400, zero ingested runs, one `rejected` ledger row — torn bundle never stages | Honest |
| Launch-intent queue file | Truncated JSON in `incoming/` | Dead-lettered on next drain; queue not wedged | Honest |
| Preferences file | Truncated | `PreferenceLoadState.CORRUPT` + JP error 「設定ファイルを読み込めません」; `reset_persisted_file` preserves old under `.recovery` | Honest |
| `.htdt-backup` archive | Truncated | `validate_backup` raises; never installs | Honest |
| `legacy_data` archive swap | Journal-first protocol | Rename journal written atomically before either `os.replace`; 'interrupted' state reported on next open | Honest (verified in code, journal path already covered by prior rounds) |
| `data_relocation` move | Phase journal + `_recover_journal_locked` | Per-phase settle — copies settle forward, cuts roll back | Honest (code-verified) |
| `installation_handoff` package | Digest-verified promote | Members promote before manifest; verify or rollback on failure | Honest (code-verified) |

## What "dishonest" looked like, concretely

- **Ghost ingestion**: `handle_delivery` is a three-commit chain (ingest → stage →
  delivery ledger). A crash between the first and second left a fully durable
  `capture_ingestion_runs` + evidence row set with no inbox item and no ledger entry.
  Nothing enumerated those runs: the inbox is built from `capture_inbox_items`, the
  ledger only feeds dedup. The bytes survived intact but were invisible — and the
  retention purge treats run rows and inbox items as one unit, so the orphan could
  later be deleted without ever being seen. A redelivery of the *same* bundle would
  have healed it (dedup → `already_staged`), but a crash is precisely when the sender
  may not retry.
- **Residue lie**: `TemporaryDirectory` guards the happy path only — process death
  skips `__exit__`, so `htdt-restore-stage-*` trees (a full extracted db + assets) and
  `htdt-backup-*` staging trees accumulated silently, and atomic-writer `.*.tmp` files
  in the data dir and intent inbox stayed forever.
- **Atomicity oversell**: `ensure_native_schema` documented "Atomically migrate" — the
  injection shows convergence helpers commit at internal `executescript` boundaries,
  so a kill lands at a *step boundary*, not inside a step. The outcome is honest
  (version marker last per step + idempotent resume), but the claim wasn't.

## Fixes

- `CaptureInboxRepository.reconcile_orphaned_ingestions()` — restages persisted
  ingestions that never reached the inbox (`arrival_source='restart_recovery'`,
  JP detail「未ステージの取り込みを復旧」). Idempotent (already-staged lineages
  untouched), bounded (`max_items`), per-orphan failures logged and skipped. Wired
  into the three places an operator-visible heal belongs: every receiver delivery
  (`handle_delivery`), every file-open route (`route_capture_intent`, before its own
  ingest so the new run isn't a false orphan), and every inbox listing
  (`workflow_application._make_inbox`). `htdt-capture-import` CLI ingestions —
  deliberately unstaged — surface the same way, which is the correct honest end
  state (staged ≠ promoted; the operator still decides).
- `native_backup`: `_sweep_open_residue()` runs at the end of
  `recover_interrupted_restore` (which `ensure_native_schema` calls on every open):
  removes `htdt-restore-stage-*` dirs and `.*.tmp` files in the data dir and
  `launch-intents/incoming/`, all gated on a 60-second age floor so an in-flight
  write is never swept. `_sweep_backup_staging()` in `_create_backup` clears
  `htdt-backup-*` staging dirs beside the archives. Failures warn, never break
  startup — residue is junk, not a blocker.
- `cad_schema.ensure_native_schema` docstring now describes the real durability:
  boundary commits + idempotent resume.

## Deferred (documented, not fixed)

- `mat:<id>.json` NTFS ADS materialization write is in-place (rename-atomic publish
  cannot target a stream); the write is read-back verified, the filename is
  content-addressed, and nothing in HTDT re-reads it — a torn file is a regenerable
  vendor artifact, not authority. A digest sidecar could harden it; out of small-diff
  scope.
- `mat:` ADS torn-write injection not run (Windows ADS semantics on a read-back-
  verified derived file — deferred above).
- Reconcile heals orphans only at delivery/route/listing boundaries — there is no
  background sweeper. A crash orphan stays invisible until one of those fires; that
  is the intended honest minimum.
- `_sweep_open_residue` covers the managed data dir and intent inbox; other
  ancillary dirs (e.g. `.htdtproject-*` export temps elsewhere) were not swept —
  left for a targeted follow-up if evidence shows they accumulate.

## Files changed

- `backend/src/htdt/capture_inbox.py` — `reconcile_orphaned_ingestions()` +
  `CAPTURE_INBOX_RECOVERY_SOURCE`; JP detail string.
- `backend/src/htdt/capture_receiver.py` — reconcile call before ingest in
  `handle_delivery`.
- `backend/src/htdt/launch_router.py` — reconcile call before `import_capture_artifact`
  in `route_capture_intent`.
- `backend/src/htdt/workflow_application.py` — inbox `list_items` wrapper reconciles
  before listing in `_make_inbox`.
- `backend/src/htdt/native_backup.py` — `_sweep_open_residue()` (restore-stage dirs,
  `.*.tmp` files, 60 s age floor) called from `recover_interrupted_restore`;
  `_sweep_backup_staging()` called from `_create_backup`.
- `backend/src/htdt/cad_schema.py` — `ensure_native_schema` docstring describes real
  boundary-commit + resume semantics.
- `backend/tests/test_review_round15_crash.py` — injection regressions: real
  `TerminateProcess` kills on mid-transaction / instance-lock children, orphan
  restage (idempotent + receiver-heal paths), residue sweep incl. in-flight-temp
  preservation, migration boundary resume from a stamped v1 db, torn-archive
  rejection.
