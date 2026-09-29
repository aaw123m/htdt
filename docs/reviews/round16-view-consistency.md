# Round 16 — multi-view agreement

Scope: every panel that re-presents the same entity, value, or state —
objects tree, inspector (position/size/orientation/aim fields, product-link
badge), room canvas labels & mesh, placement sub-panels
(constraints / video / system-expansion / standards), history panel list +
open revision detail, undo/redo header & dirty gate, delete paths and the
sidecar authorities (materials, environment, listener poses, screen
transfers, video workspace, proposed placements), unit display prefs, and
the SPA frontend. Method: trace each mutation → notify → repaint edge in
code, then exercise the suspicious ones offscreen with pytest
(`QT_QPA_PLATFORM=offscreen`, `-n 4`, Python 3.12, Windows).
Branch `devin/rev16-consist`.

## Refresh topology (what actually mirrors what)

`RoomWorkspaceController` mutations funnel through
`RoomWorkspace._refresh()`, which fans out to: `_refresh_inspector`
(full inspector rebuild), `_sync_objects_panel` (tree `clear()` +
rebuild — names, visibility, lock flags, selection always live),
`mark_broken_constraints`, recovery banner, the current context's panel
sync, geometry/acoustics panel refresh, `_render` (canvas meshes +
labels re-read `entity.name`/`entity.position` per frame — always live),
and the dirty status line. Anything *not* inside `_refresh`'s fan-out is
a stale-view candidate.

| Mechanism | Check | Observed | Verdict |
|---|---|---|---|
| Objects tree on rename/edit/delete/undo | `sync_document` rebuilds every row from `document.entities` each refresh | Names, ▶-marker, counts, selection all re-derived — no cached row survives | Honest |
| Canvas labels & meshes | `_render` re-reads entity fields every refresh; hidden set respected | Always live; no cached actors keyed to stale text | Honest |
| Inspector on external change | `set_entity` rebuilds widgets; `_dirty_widgets` guards in-flight typing | In-flight user text survives a same-entity refresh — deliberate (#583) | Honest |
| Inspector `_dirty_widgets` after commit/undo | Post-refresh loop clears fields whose value matches the fresh baseline | **Position/size/name cleared, but orientation roll/pitch/yaw and aim yaw/pitch never compared** — an edited-then-saved orientation stayed flagged dirty: the inspector showed the old widget value (baseline reset), and the *next unrelated* edit silently recommitted the stale orientation | **Fixed**: `_widget_matches_baseline` now compares orientation/aim field values against the captured baseline |
| `restore_revision` dirty gate | Refuses restore while unsaved edits exist | Gate read `working.is_dirty` (scene only) while `before_deactivate` and the system-expansion apply block already use the full `is_dirty` — unsaved *sidecar* edits (video workspace, pose selections, materials) allowed a restore that rebinds the document under them | **Fixed**: gate now reads `self.is_dirty` |
| Placement context `_refresh` | Four sub-panels share one page: constraints, video, system-expansion, standards | `_refresh` synced only `constraints_panel`; the video panel's bound-entity tables went stale during in-placement edits (seat/screen deletes, transfers) — `_screen_entity_id` was even kept when its screen was deleted, so the heading still claimed the removed screen | **Fixed**: `_refresh` now calls `_sync_video_panel()` in the placement context; `sync_document` resets the screen binding + JP heading when no screen remains |
| Video panel seat cards on rename | Seat riser/seat cards and pose-combo entries keyed by entity id | Row rebuild was keyed on `list(self._seat_widgets) != seat_ids` — a *rename* keeps ids, so the card and combo kept showing the old name while tree/inspector/canvas showed the new one | **Fixed**: rebuild keyed on a `(entity_id, name)` signature |
| Video panel stale bindings | `screen_bindings`/`seat_bindings` persist by entity id | Seat bindings were filtered to live seats in `_video_workspace_from_panel`, but dangling *screen* bindings were written back and later reported as misleading「スクリーンの画素設定が未バインドです」/ consumed by `build_request_from_workspace` | **Fixed**: stale screen-binding ids are dropped the same way |
| Stale-widget writes after unsaved delete | `_seat_pose_changed` / `_screen_transfer_changed` reachable from stale combo state | `listener_pose_repository.select_pose` validates against **HEAD**, not the draft — a card for a draft-deleted seat still in head could write a real persisted selection for a seat the working document no longer has | **Fixed**: both handlers no-op when the entity id isn't a live draft seat/screen |
| History panel open detail vs refresh | `sync_revisions` repopulates list; `_sync_history_panel` then pushed the generic summary | Selecting a revision shows a computed diff (`_history_diff`); the next refresh clobbered it back to the summary even though the revision row stayed selected | **Fixed**: refresh keeps and recomputes the open diff when a revision is selected |
| Undo/redo header & commands | Header refresh in `_refresh`; command availability bound in `workflow_application` | `edit.undo`/`edit.redo` gated on `working.can_undo`/`can_redo`; `project.save` (room lane) on full `is_dirty` + focused-editor — consistent contract | Honest |
| `system_expansion_panel` & `standards_panel` | Read `current_head` + saved variants / stored evaluations | Committed-authority views by design — they mirror HEAD, refresh-free is coherent | Honest |
| History list rows | `sync_revisions` reapplies `selected_id` and re-emits row changes | Live | Honest |
| Ghost/preview (`has_preview`) | `committed_document` = draft minus preview for missing-inputs; evaluation baseline = saved head | `evaluate_video` mixes draft workspace with head baseline — bounded and labelled (「ドラフト基準」copy in place), not a silent disagree | Honest, bounded |
| Unit display pref | `display_input.length_unit`/`numeric_precision` bound to inspector + measure panel only | All other panels suffix `m`/`mm` literally at format sites — honest-by-suffix (a readout tagged「m」is always metres); pref is a documented inspector/measure feature, not a global toggle | Honest (scoped) |
| SPA frontend vs Qt | `main.py` FastAPI serves projects/contexts/search-specs | Separate data authority (its own stores), legacy surface — not a same-entity second view of the scene document | Out of scope (different authority) |

## Deferred / documented

- **Orphan sidecar selection rows**: deleting a seat/screen leaves its
  pose/transfer *selection* rows in the repositories. They are invisible —
  every read path filters to live entities — and `_restore_sidecars`
  already drops dead rows. Persisted cruft, not a view disagreement.
- **`restore_revision` on clean scene + dirty sidecars** is now blocked by
  the fixed gate; the wider "restore while panel has unapplied text" flow
  still relies on `_pending_inspector_edits` being committed first, same
  as save.

## Fixes

- `room_workspace.py`: `_widget_matches_baseline` compares orientation
  (roll/pitch/yaw) and aim (yaw/pitch) field values to the baseline;
  `restore_revision` gate `working.is_dirty` → `is_dirty`; `_refresh`
  syncs the video panel in the placement context;
  `_video_workspace_from_panel` drops screen bindings for deleted
  screens; `_seat_pose_changed`/`_screen_transfer_changed` ignore stale
  widget signals for non-live entities; `_sync_history_panel` recomputes
  the open diff instead of clobbering to summary.
- `room_video_panel.py`: `sync_document` clears `_screen_entity_id` and
  reverts the JP heading when no screen exists; seat-card rebuild keyed
  on an `(entity_id, name)` signature so renames repaint.

## Tests

`backend/tests/test_room_view_consistency.py` (6 tests, all new):

- `test_inspector_orientation_field_resyncs_after_commit_and_undo` —
  orientation edit → save → field leaves `_dirty_widgets`; undo → field
  re-syncs to restored value.
- `test_restore_revision_requires_clean_sidecars` — saved head +
  dirty video workspace ⇒ restore raises `EditStateError`; clean
  sidecars ⇒ restore succeeds.
- `test_video_panel_tracks_deletes_in_placement_context` — screen delete
  inside placement context clears panel binding, heading, and bindings.
- `test_video_panel_seat_card_follows_entity_rename` — rename while
  placement open ⇒ card title and combo entries show the new name.
- `test_stale_seat_pose_write_is_guarded` — delete unsaved seat, then
  fire `_seat_pose_changed` ⇒ no persisted pose selection.
- `test_history_detail_recomputes_open_diff_on_refresh` — open diff
  survives refresh and tracks the latest head.
