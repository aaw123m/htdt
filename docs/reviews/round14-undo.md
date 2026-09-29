# Round 14 — Undo / Command-Model Depth

Scope: the `WorkingDocument` command history is the editor's safety net. A
command whose undo restores a slightly different state, a macro that half-undoes,
or a refresh tick that pushes phantom commands silently corrupts user work.
This round enumerated the whole command model and verified — for every command
type — execute → serialize → undo → byte-equal → redo → byte-equal
(`canonical_scene_json`: all fields, ids, entity order, derived state).
Branch `devin/rev14-undo`; verified with `QT_QPA_PLATFORM=offscreen` pytest.

## Command model enumeration (as verified)

- **Custom, not QUndoStack.** `EditCommand` protocol (`is_noop` / `apply` /
  `revert`, `cad_document.py`); `CommandHistory` holds `list[EditCommand]` +
  `_index` cursor; `WorkingDocument` owns one history plus a preview document.
- **Commands are frozen dataclasses keyed by entity_id or whole-entity
  snapshots** — identity, not index — except `DeleteEntitiesCommand`,
  `AddEntitiesCommand`, and `EntitySetEditCommand.removed_indices`, which store
  indices. Safe because history is strictly linear: an index captured at push
  time is revisited only after every later command was reverted.
- **Merge = preview/commit.** Drags mutate `_preview_document` continuously and
  `commit_preview()` pushes ONE `TransformEntitiesCommand`. Two drags separated
  by a click are two commands — verified (`test_two_preview_commits_do_not_merge`).
- **Macros = `CompositeEditCommand`** (inner edit + `apply_side`/`revert_side`
  binding sidecar state — authoring/placement constraints — into one step) and
  now **`SequentialEditCommand`** + `merge_last`/`merge_history_since` for
  fusing an already-applied run of commands into one step.
- **Dirty tracking** is content-hash based (`scene_content_hash` vs
  `_saved_hash`), NOT index-based — so undo past a save point correctly reports
  dirty, and redo back to the saved content correctly reports clean
  (`test_save_then_undo_tracks_real_clean_state`).
- **Spec:** `docs/CAD_EDITOR_SPEC.md` — undo after save touches only the draft;
  nothing/no-op/cancel never lands in history; "履歴はメモリ内で上限を持ち"
  (history must have an in-memory bound).

## Findings

| # | Finding | Severity | Verdict |
|---|---------|----------|---------|
| U1 | **`mark_broken_constraints` pushed a phantom command on every refresh** — `_refresh()` → mark → `update_authoring_constraints` → `push`. Undo reverted the marker, the next refresh re-marked and re-pushed: **Undo could never reach the user's real edit** (a livelock), and a delete consumed two Undo steps (entities, then marking). | High | **Fixed** — delete-time marking rides inside the delete command via `CompositeEditCommand` sides (atomic restore of entities + unbroken constraints); refresh-time marking applies the persisted constraint set directly, never touching history. Labels still surface via `_pending_broken_labels`. |
| U2 | **`CommandHistory` was unbounded** — spec requires an in-memory limit; nothing dropped oldest commands. | Medium | **Fixed** — `CommandHistory(limit=DEFAULT_HISTORY_LIMIT=500)`; push evicts the oldest entries, dirty marker stays truthful when the saved state falls out of reach (test: 600 edits, undo to exhaustion, still dirty). |
| U3 | **Driver edit + constraint propagation = two Undo steps** — every `propagate_constraints` call site (`finish_at`, `nudge_selection`, numeric edits, cabinet align, batch field edit, layout align/distribute) pushed a second command right after the driver. One user gesture needed two Undos; the intermediate state (driver moved, subjects not) was an inconsistent constraint state. | High | **Fixed** — `propagate_constraints(…, merge_with_previous=True)` at all six call sites fuses propagation into the driver step via `merge_last(2)`; the fused step keeps the driver's label. |
| U4 | **`RoomWorkspaceController.duplicate_selected` promised "one Undo step" but pushed N** — one `duplicate_entity` command per selected entity; Undo restored copies one at a time. (The UI path is already atomic via `layout_duplicate` → `EntitySetEditCommand`; the controller method itself was still wrong.) | Medium | **Fixed** — `history_index` marker + `merge_history_since(marker)` fuses the loop into one step. |
| U5 | **`_document.entity()` leaked `KeyError` and `next()` leaked `StopIteration`** on stale ids — `move_entity`, `rotate_entity`, `update_entity(s)`, `duplicate_entity`, `transform_entities`, `TheaterWorkingDocument.replace_entities`, `_begin_preview`. A UI path catching `EditStateError` would miss these raw exceptions. | Medium | **Fixed** — `_require_entity` converts to `EditStateError`; `update_entity`/`update_entities` also reject unknown update fields (`unsupported entity fields: …`) instead of silently absorbing typos into `model_dump`. |
| U6 | **`transform_entities` / `apply_entity_set_edit` trusted caller-built `model_copy` results** — `model_copy` bypasses validators, so a command could smuggle an invariant-breaking entity (e.g. `speaker_role=None` on a speaker) into history, where every later apply/revert replays the corruption. | Medium | **Fixed** — inbound `after`/`added`/`replaced_after` entities are revalidated via `SceneEntity.model_validate` before the command is recorded. |
| U7 | Failed push contract verified — `CommandHistory.push` applies **before** truncating the redo tail, so a `before`-state mismatch raises leaving the redo branch intact (regression test `test_failed_push_preserves_redo_tail`). | — | Honest (round-9 contract confirmed; added coverage). |
| U8 | Revert-failure contract verified — a failing `revert_side` leaves the command applied and retryable, never half-undone (idempotent side callables; no compensation — matches the round-9 `test_round12_dirty` contract). | — | Honest. |
| U9 | Preview boundaries verified — undo/redo during an active preview raises `EditStateError`; cancel records nothing; no-op commit (position unchanged) pushes nothing. | — | Honest. |
| U10 | Cross-revision honesty — undo across a save point edits only the draft and flips `is_dirty`; `restore_revision` loads an immutable lineage revision into a fresh history rather than mutating it. | — | Honest. |
| U11 | Label truth — fused steps present the driver's label (移動/複製/削除…), not the propagation's internal one; `undo_label`/`redo_label` read from `CommandPresentation` of the command at the cursor. | — | Honest (locked by `test_merge_last_fuses_into_single_step`). |
| U12 | UI enabled-state — `undo` is enabled while `can_undo or has_preview` (undo cancels the preview first) — honest affordance. | — | Honest. |

## Deferred

- **D1 — broken constraints never auto-heal.** Undoing a non-delete removal
  (raw `apply_entity_set_edit`) restores the member entity but leaves the
  constraint `broken=True` until the user acts — deliberate per the
  "surfaced, never re-bound by name" policy, now also the semantics after
  U1's refresh-maintenance change. A future round could add an explicit
  "unbreak" verb; left as-is on purpose.
- **D2 — history bound is per-document, not global.** 500 steps × large
  entity snapshots is bounded per WorkingDocument; documents each carry
  their own stack. Adequate for the spec; a global byte budget would be a
  separate policy decision.
- **D3 — `native_editor` delete bypasses constraint marking entirely** (its
  `working.delete_entity` has no controller to pass composite sides). The
  refresh-time marker still catches the member loss, so the constraint
  ends correctly broken — just not inside the delete's own Undo step.
  Acceptable: the native editor path has no constraint UI.

## Contract changes

- `mark_broken_constraints()` no longer pushes an undoable command — it is
  refresh maintenance. `test_deleted_member_relationship_stays_inspectable`
  was updated accordingly: undo now restores the removed member in one step
  and the persisted broken flag stays surfaced (the controller delete path
  restores entities + constraints atomically inside one command).
- New public surface: `SequentialEditCommand`, `CommandHistory(limit=…)`,
  `CommandHistory.merge_last`, `WorkingDocument.history_index` /
  `merge_last` / `merge_history_since`, `delete_entities(apply_side=…,
  revert_side=…)`, `propagate_constraints(merge_with_previous=…)`.

## Tests

`backend/tests/test_round14_undo.py` (37 tests): per-verb serialized
round-trips for every mutation verb (move/rotate/transform/add/duplicate/
update/update_entities/delete/replace_document/preview_commit/entity_set/
composite), interleaved delete-order restore, index-drift, failed-push redo
preservation, failed-revert retryability, bound eviction + dirty honesty past
the bound, merge/fuse semantics, duplicate/delete/propagate one-step counts,
undo-livelock regression, preview boundaries, input hygiene, save→undo→clean
marker.
