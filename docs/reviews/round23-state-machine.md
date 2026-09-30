# Round 23 — generative state-machine testing

Scope: seeded random-walk property harnesses over four HTDT state-machine
surfaces on main (`c398c8b6`, post REV22-ROT), Qt-offscreen
(`QT_QPA_PLATFORM=offscreen`), Python 3.12.10 (NuGet `python` package at
`C:/devin/python`), `pip install -e backend[dev]`. Branch
`devin/rev23-machine`. Plain `random.Random(seed)` loops — `hypothesis` is
not installed on this box.

Suite gate (local, no GitHub Actions):

    TMPDIR=C:\t PYTHONIOENCODING=utf-8 QT_QPA_PLATFORM=offscreen \
      C:/devin/python/python.exe -m pytest -p pydantic backend/tests \
      -q -n 4 -p no:warnings

## Harness

`backend/tests/test_round23_state_machine.py` — 530 seeded walks,
530 test cases:

| Surface | Walk | Seeds | Steps | Model |
| --- | --- | --- | --- | --- |
| History | `test_history_state_machine_walk` | 160 | 60 | `TheaterWorkingDocument` journal (position-indexed content hashes), bounded history (`history_limit` ∈ {4, 8, 500}), schema-4/5 docs, side-effect flags tracked by owner position |
| Document | `test_document_state_machine_walk` | 80 | 34 | `RoomWorkspaceController` + `ProjectLibraryRepository` + `SceneRepository` over real SQLite in `tmp_path` — open/save/discard/undo/redo/restore/dirty-resolution/recovery/archive |
| Activity | `test_activity_state_machine_walk` | 150 | 60 | `ActivityCenter` (history_limit = record_limit = 9) — records-order + history-row mirrors, eviction, retry, authority reclassification, persist round-trip |
| Worker | `test_worker_state_machine_walk` | 40 | 56 | Real `NativeWorkerPool` + offscreen `QApplication`, generation-tagged payloads, start/cancel/cancel_all/stop_all/shutdown interleavings, completion-attribution and drain invariants |
| Library | `test_library_state_machine_walk` | 100 | 40 | `ProjectLibraryRepository` list/mutate on real SQLite — ordering (`last_opened_at_utc` nulls-last, `created_at_utc`, rowid), archive/duplicate/rename, no phantom rows |

Every walk asserts after each op: content/hash round-trip, index/length/
epoch/dropped counters, `is_dirty` truth, `can_undo`/`can_redo` labels,
resolvability contract, and per-surface invariants (no orphan entities, no
stale completion, history rows terminal-only, totals consistent). Any
failure raises `WalkFailure` carrying the seed and full op trace —
reproducible via `pytest tests/test_round23_state_machine.py -k seed`.

## Findings — fixed in product

Nine real invariant violations, all reproduced by a seeded walk, minimized,
fixed in product code, and covered by the (now-passing) seeds below.

### 1. `QUEUED/PREFLIGHTING → CANCELLATION_REQUESTED` skipped `started_at`

`_transition` only stamped `started_at` for `RUNNING`, so cancelling a
queued op produced a snapshot that fails the model's own
`valid_operation` validator (`RUNNING`/`CANCELLATION_REQUESTED` require
`started_at`). A persisted snapshot then crashes
`ActivityCenter.load_active_operations` on load. Fixed by stamping
`started_at` for `CANCELLATION_REQUESTED` too.
Seeds: activity 3, 4.

### 2. `_record` raised `KeyError` for evicted-but-resolvable operations

`get()`/`retry()` resolve a terminal op through `_records` then the
bounded `_history`, but `_record()` (used by every mutating transition)
consulted only `_records` — so `request_cancel`, `update_progress`,
`mark_commit_point` on an aged-out but still-history-visible operation
misreported "unknown" (`KeyError`) instead of the honest
`OperationTransitionError(already <state>)`. Fixed with a
`_history_snapshot` fallback that raises the transition error for known
terminal ops and keeps `KeyError` for genuinely unknown ids.
Seed: activity 7.

### 3. `note_authorities_changed` left stale history rows

Pass-1 mutated `record.snapshot` in place for non-intersecting and
non-completed ops, but never updated the parallel `_history` row — once
the record was evicted, `get()`/history reverted to the stale snapshot
(`current_for_input`, `updated_at` regressed). Fixed with a
`_sync_history_row` helper applied after every in-place rewrite.
Seed: activity 5.

### 4. `retry()` crashed on declared `NOT_CANCELLABLE` with a factory callback

`retry` forwarded the retry-factory's `cancel_callback` into `submit`
while keeping the parent's declared `NOT_CANCELLABLE` — `submit` rejects
that contradictory pair, so retrying a non-cancellable op whose factory
supplied a callback raised `OperationTransitionError`. Fixed: declared
cancellability wins; a declared `NOT_CANCELLABLE` drops the stray
callback (and vice versa).
Seed: activity 2.

### 5. `request_cancel` crashed on an already-requested operation

`can_cancel_now` stayed `True` for `CANCELLATION_REQUESTED`, so a second
cancel request reached `_transition` with the disallowed self-transition
`CANCELLATION_REQUESTED → CANCELLATION_REQUESTED` and raised —
`prepare_shutdown` could hit the same path when re-walking active ops.
Honest fix: `can_cancel_now` is `False` once cancellation is already
requested (the UI should show "cancelling", not offer Cancel again), so
`request_cancel` returns `False` instead of crashing.
Seeds: activity 1, 3, 9, 11.

### 6. `note_authorities_changed` iterated `_records` while mutating it

Pass-1's `for record in self._records.values()` crashed with
`RuntimeError: dictionary changed size during iteration` whenever a
`COMPLETED` reclassification (`_transition` → `_archive`) evicted or
removed entries mid-loop — i.e. whenever records sat near the bound.
Fixed by iterating a list snapshot.
Seeds: activity 47, 52, 88, 139.

### 7. `resolve_dirty_state` reported false success

`commit_preview`/`cancel_preview` without a preview and
`recover_draft`/`discard_recovery` without a candidate returned
`(True, <message>)` — the shell therefore believed an effect ran when
none could. Added precondition guards returning `(False, 日本語 message)`
when the action is inapplicable (the existing inner `EditStateError`s
were the only checks; `resolve_dirty_state` never reached them without
raising through the wrapper semantics).
Seeds: document 3, 6.

### 8. `RoomWorkspaceController.set_revision_label` always raised `TypeError`

The controller passed `(revision_id, label, note)` positionally but
`SceneRepository.set_revision_label` declares `label`/`note`
keyword-only — every label write through the workspace crashed.
Fixed the call to use keywords.
Seeds: document 8, 9, 10, 11.

### 9. `_LINGERING_THREADS` retained C++-deleted `QThread`s forever

`_detach` wires `finished → _release_lingering` — but a thread that
finishes in the window between `thread.wait()` timing out and the
`connect` never emits to the late connection. Its earlier
`finished → deleteLater` wiring (connected at `start`) still destroys
the C++ object, leaving a dead wrapper in `_LINGERING_THREADS`:
`lingering_thread_count()` never drains back to baseline (and touching
the wrapper raises `RuntimeError: Internal C++ object already deleted`).
Fixed by also releasing on `destroyed`, which fires whenever the object
is deleted regardless of which `finished` emissions were missed.
Seed: worker 24 (also observed on worker 1 — timing-dependent;
the `isFinished() → connect` window is inherently probabilistic).

## Harness/model bugs found while minimizing (not product bugs)

Kept for the record — each was a false-positive in the walk itself, fixed
in the harness before any product conclusion was drawn:

- `pytest.raises` failures raise `Failed` (a `BaseException`), so the
  preview-guard `except Exception` missed them — rewritten to
  `try/except EditStateError` + explicit `check`.
- Pydantic equality: `entries[:n] == opened` compared `tuple` to `list`
  — always unequal; switched to `tuple(opened)`.
- Side-owner bookkeeping ran before `record_push`, recording a
  pre-increment position; moved to post-push.
- `record_push(changed)` conflated "history slot pushed" with "content
  changed" — a resolved-identical command (e.g. an attached-child move
  snapped back by `apply_attachments`) pushes a slot while returning
  `False`; the model now watches the `history_epoch` counter.
- Redo-tail owner eviction ran after `pos += 1`, letting a deleted tail
  owner at `pos+1` collide with the new entry's position; moved before
  the increment.
- `_close_model` recomputed `recovery_hash` unconditionally while
  `_sync_recovery` freezes the persisted row during a pending candidate.
- `_eviction_model` ran on every `authorities_changed`; `_archive`'s
  bound trim only happens when a records-resident `COMPLETED` op is
  reclassified through `_transition`.
- `_op_detach` built `attachments=()` — schema-5 `SceneDocument`
  requires the field omitted when empty (`kept or None`).
- `_op_attach`'s `_op_add` fallback bypassed the preview guard.

## Result

- Harness suite (this file): **530/530 walks green** on the branch.
- Scoped regression: `test_activity_center`, `test_native_worker`,
  `test_workspace_dirty_state` all green.
- Full `backend/tests` suite green on the merge-test tree (~75 min).

## Post-merge E2E follow-up (devin/rev23-machine-2)

Runtime offscreen verification of the nine fixes surfaced a residual
defect in #6's repair:

- **Residual**: `note_authorities_changed` still aborted mid-batch when
  `_records` exceeded `record_limit`. Each reclassifying
  `_transition` → `_archive` evicts the oldest terminal records —
  including not-yet-visited ones — so the next visited evicted op fell
  through to the history fallback and raised
  `OperationTransitionError('already completed')`. The calling
  `_note_superseded_inputs` swallows it, so the batch half-applied
  silently. The 530-walk harness missed it because walks never exceed
  `record_limit` while carrying matching `input_authority_refs`. Fixed:
  that transition now tolerates the mid-pass eviction (the op's
  surviving history row is reclassified in place by pass 2). Kept
  regression test:
  `test_authorities_changed_reclassifies_past_record_evictions`
  (fails pre-fix with `OperationTransitionError: operation op-1 is
  already completed`; green post-fix). Found by runtime E2E testing,
  not by a seeded walk.

## Deferred

- `worker` drain check is inherently timing-sensitive (real threads,
  ~0.7 s worst-case op vs 15 s budget). One flake observed pre-fix on
  this box; the diagnostic now dumps the lingering-thread states on
  failure for faster triage.
- Pre-existing (not caused by this round, found by E2E): the
  bundle-completion slot `worker.completed → _bundle_job_completed`
  runs on the worker thread because `WorkflowApplicationComposition`
  is not a `QObject`, so the export/import done `QMessageBox` is built
  and `exec()`'d off-GUI-thread (undefined behavior / crash risk).
- Pre-existing: closing during a running backup
  (`audit_native_authority_graph`) leaves a detached worker that the
  interpreter teardown destroys — exit code 127 in the recorded case
  (no hang; `lingering_thread_count` was honest).
