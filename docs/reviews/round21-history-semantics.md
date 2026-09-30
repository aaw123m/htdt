# Round 21 — History / undo / revision semantics

Scope: the user-visible semantics of undo/redo/revision that sit above the
Round-14 command-model audit — driven by scripted sequences through the real
command/document/controller APIs (`WorkingDocument`, `CommandHistory`,
`RoomWorkspaceController`, `SceneRepository`, `diff_scene_documents`), not by
reading code in isolation. Seven dimensions: redo invalidation, undo across
boundaries (save/load/project-switch/dialog commit/preview/recovery),
composite-op atomicity, revision-restore byte equality + honest head + sidecar
behavior, diff-view completeness (rename-only, reorder-only, attachment,
field-level), history-UI truthfulness (order, labels, applied flags), and the
500-entry cap (boundary landing + eviction visibility).

Method: a 37-check harness (`C:\t\verify_rev21.py`) exercising real push/undo/
redo/save/restore/diff sequences, plus code review of the surfaces the harness
reaches indirectly (edit-menu history slice, history panel diff detail).
Remaining harness "FAIL" lines are check-side artifacts (label-vs-entry
comparison, `recovery_candidate` is bind-time only, an off-by-one boundary
expectation — the correct landing is the state produced by the last evicted
command); the pytest file asserts the true semantics.

Branch `devin/rev21-history`. Tests:
`backend/tests/test_round21_history_semantics.py` (24 cases).

## Verdict table

| # | Finding | Severity | Verdict |
|---|---------|----------|---------|
| 1 | `merge_history_since` / `merge_with_previous` measured a batch by `history_index` delta — but cap eviction moves the index *backwards*, so a multi-push op committed at a full history (group duplicate, drag + fused constraint propagation) stayed unfused: Undo then restored it one entity at a time, breaking the promised single-step contract exactly when history was fullest | MED — composite-undo contract broken near the cap | FIXED — monotonic `history_epoch` marker counts pushes, not cursor position; both call sites migrated |
| 2 | `diff_scene_documents` keyed entities by id-set only: a **reorder-only** revision diffed empty ("同一内容") although entity order feeds the content hash and persisted a genuinely different document — history panel lied | MED — false "no changes" on a real revision | FIXED — `SceneDiff.entity_order_changed` + `オブジェクトの順序を変更` summary line |
| 3 | Changes to `kind`, `semantic_bindings`, `operational_zones` produced an `EntityFieldChange` with `fields=()` → summary rendered "name: を変更" (change with no detail) | MED — misleading diff line | FIXED — all three fields diffed, with JP labels 種別 / 機能割当 / 運用クリアランス |
| 4 | Restoring a revision whose *content* equals the current head deduped to the existing head (`created=False`) while the status line claimed "履歴版を新しい先頭版として復元しました" | LOW — false success claim | FIXED — compares result vs pre-restore head, reports その履歴版は現在の先頭版と同一内容です |
| 5 | Removed-entity rows in the history-panel diff fell back to raw `entity_id` — names were only resolved against the newer side | LOW — unreadable labels | FIXED — `diff_summary_lines` accepts `fallback_document`; panel passes the older revision |
| 6 | Bounded-history eviction was invisible: nothing exposed how many oldest commands were dropped, so the edit menu's "最近の編集" slice read as complete history | MED — silent data loss at the cap | FIXED — `CommandHistory.dropped` / `WorkingDocument.history_dropped`; edit menu shows …さらに N 件 and 履歴上限で最古 N 件は破棄済み |

## Verified honest (no change needed)

- **Redo invalidation (D1)** — a divergent edit after undo truncates the redo
  tail; a *failed* push (duplicate id raising `EditStateError`) does not fork
  the timeline so the pending redo stays reachable; a no-op push isn't an
  edit at all and leaves the tail intact. `undo_label`/`redo_label` come from
  the command's `CommandPresentation` and describe the exact step.
- **Undo across boundaries (D2)** — save→undo flips dirty correctly and redo
  returns to the clean saved state; save-after-undo writes an honest new head
  descending from the undone head; undo during a preview raises
  `EditStateError` rather than silently acting; while a recovery candidate is
  pending `undo()`/`redo()` return `False` (never corrupt); rebinding a
  controller to a document starts with empty history; restore clears history
  (a restore is a commit boundary).
- **Composite ops (D3)** — batch delete restores all parts in one undo step;
  `merge_last` fuses only commands below the redo cursor; `SequentialEditCommand`
  replays apply/revert in order; the cap-eviction fuse regression is covered
  by the fix above.
- **Revision restore (D4)** — restore creates an honest NEW head descending
  from the old head (`detached=False`), byte-for-byte content and content hash
  equal to the original; detached revisions restore to mainline head;
  same-content restore dedupes instead of minting a phantom revision;
  sidecars (poses, materials, video workspace) are document-scoped live
  state — deliberately not revision content — and stay inert/dormant across
  restore while remaining consistent with the dirty digest.
- **Diff view (D5)** — rename-only diffs report `name`; attachment and
  construction-assembly changes carry dedicated flags; `schema_version` bumps
  stay diff-empty by design (migration plumbing, not authored content).
- **History UI (D6)** — `history_entries` order is push order with `index`
  positions inside the retained window; `applied` flags track the cursor
  honestly (undone tail shows ○); revision listing is chronological.
- **Limits (D7)** — the cap keeps exactly `limit` commands; undoing to the
  boundary lands on the state produced by the last evicted command (position
  before the oldest *retained* command) — correct, not corrupt; `can_undo`
  goes False exactly at the boundary.

## Deferred

- The diff detail still cannot explain *what* changed inside
  `semantic_bindings`/`operational_zones` payloads (field-name level only) —
  acceptable: the summary names the field, payloads are inspectable objects.
- `schema_version`, entity `aim_xyz` vs binding interplay, and revision-label
  text aren't diffed — labels are cosmetic metadata by design.
