# Issue #883 — crash-safe autosave + session recovery (design)

## Survey findings (what already exists)

Startup (`native_cad._run_gui`): `decide_launch` over bounded launch metadata
(#739 `startup_recovery.py`) → `record_launch` → splash → schema upgrade →
`SceneRepository` → `resolve_startup_document` → `annotate_launch` → window →
`app.exec()` → `complete_launch(clean=True)` + `record_clean_close`.
Exception paths call `complete_launch(clean=False, failure_class=...)`.

Existing crash/recovery assets:
- `scene_recovery_snapshots` — per-document draft (content-hash verified),
  surfaced by the workspace recovery banner; operator must accept.
- `RecoveryLaunchRecord`s — bounded launch history, `clean_exit` tri-state.
- `previous_session_unexpected_end` — runtime.json + dead-pid detection.
- Sealed, append-only authorities: commissioning transitions
  (deterministically resumable via `derive_run_state`), apply
  transactions (`unknown_state`/`insufficient_evidence` fail-closed),
  sweep acquisition runs (terminal-only rows), owned-room campaign rows.
- `write_*_atomic`, `write_runtime_info`, `_default_pid_alive`,
  `AutomaticBackupScheduler.record_clean_close`.

Gaps this issue closes:
- No bounded per-session journal binding volatile state (workspace, dirty
  docs, declared in-flight ops, write intents) to one session identity.
- No crash-classification distinguishing clean / crash / kill / OS restart
  / foreign-version recovery data.
- No recovery-inspection surface enumerating restorable items and the
  reconciliation queue; no sealed decision lineage.
- In-flight external side effects (device apply, sweep acquisition, file
  writes) are not surfaced as uncertain at recovery time.

## Design

### File journal — `<data_dir>/session-journal/<session_id>/`
- `envelope.json` — atomic; session identity (session_id = launch_id),
  build/app/journal/native-schema versions, pid, boot token,
  started/last_seen/project_document_id, `closed_clean`.
- `journal.jsonl` — append-only entries, one JSON object per line;
  seq + prev_sha256 chain + entry_sha256; fsync per append.
- Bounded: `MAX_JOURNAL_ENTRIES` → compaction entry carrying full state +
  `compacted_from_sha`; `MAX_SESSION_JOURNALS` retention (undecided
  sessions are never silently dropped; oldest decided/clean go first,
  retention-forced drops are sealed as decisions).
- Payload keys matching secret patterns (password/token/secret/key/
  credential/bearer) are rejected at write — no plaintext secrets.

### Session endings
`clean | application_crash | forced_termination |
 os_restart_or_power_loss | still_running | incompatible |
 unresolvable`
Derived from: envelope.closed_clean, pid liveness, boot-token change,
launch-record `failure_class`/crash correlation, journal/native schema
versions. Nothing is inferred without evidence.

### Sealed tables (schema v100)
- `session_recovery_journals` — one sealed row per detected crashed
  session: envelope facts + ending class + entry_count + head/envelope
  sha (tamper-evidence once files rotate away).
- `session_recovery_decisions` — append-only operator decisions:
  `restore_accepted | restore_completed | discarded |
  reconcile_resolved | reconcile_deferred | evidence_rejected |
  retention_discard`, scope kind/ref, `restoring_session_id` lineage.
- `session_recovery_reconciliations` — append-only outcomes:
  `resolved_confirmed | resolved_rolled_back | remains_unknown |
  rejected | readback_unavailable`, operation kind/ref, evidence.
`document_id` binds the session's project (`''` when unscoped).

### Recovery report (inspection)
`SessionRecoveryReport`: envelope facts, ending, integrity verdict
(`intact | tail_torn | corrupt | incompatible`), restorable items,
reconciliation items, decided flag.
- restorable: `scene_draft` (hash-verified vs `scene_recovery_snapshots`),
  `workspace_state`, `pending_annotation` (journaled), `project_binding`
  (dirty document identity), resumable authorities: open commissioning
  run (last sealed transition), in-progress campaign/acquisition refs.
- reconciliation (fail-closed, never infers success):
  `device_apply_transaction` → `DEVICE_STATE_UNKNOWN` /
  `RECONCILIATION_REQUIRED` until sealed terminal evidence or adapter
  read-back resolves it; `sweep_acquisition` → `ACQUISITION_INCOMPLETE`;
  `file_write` → `WRITE_COMPLETION_UNKNOWN` until sha verify.
- Canonical state is never written by inspection/restore; scene drafts
  stay operator-gated by the existing recovery banner; restore merely
  records the accepted decision + lineage.

### Wiring
- `_run_gui`: `SessionJournal.open` bound to `record_launch`'s launch_id;
  heartbeat timer → `last_seen` + `workspace_snapshot`; project bound on
  `annotate_launch`; `note_failure` in exception paths; `close_clean` on
  clean exit; inspection after window open (suppressed in safe mode);
  `SessionRecoveryDialog` offers restore/discard/defer per session.
- `SessionJournalRegistry.current()` module seam so workspace/engine can
  declare: scene-draft writes (`_sync_recovery`), workspace snapshots,
  sweep-acquisition stage transitions, apply-transaction boundaries,
  write intents.

### Crash-injection tests
Journal round-trip; torn tail tolerated (last_good_seq), mid-file
corruption rejected; kill child process → unclean evidence + restorable
items; boot-token change → os_restart_or_power_loss; incompatible
versions fail closed; reconciliation verdicts per sealed evidence;
retention bounds; secret-key rejection; sealed decision lineage.
