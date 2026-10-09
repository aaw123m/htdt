# Issue #982 — 選択対象別インスペクタ + 危険編集の事前プレビュー

Reorganize the room geometry inspector around the current selection
target (頂点 / 辺 / 壁 / 開口) and put every dangerous wall/opening edit
behind an honest, uncommitted impact preview. CAD beginners can now see
WHAT they are about to change and WHAT it affects before anything is
committed — and Esc undoes the preview, not the document.

## Layer shape

- `htdt.room_geometry_preview` — Qt-free preview builders (new module).
  `GeometryChangePreview` carries `kind`, `title`, `lines` (JA impact
  lines), `room`, `topology`, `room_only` flag, `feasible`, `blockers`,
  `base_revision_id`, `base_content_hash` and the selection fixups to
  apply after commit. `preview_wall_delete`, `preview_wall_merge`,
  `preview_vertex_delete`, `preview_wall_thickness`,
  `preview_opening_update`, `preview_opening_delete` each re-run the
  candidate through `cad_walls` authority (no new CAD truth values) and
  diff the result: lost / reassigned / moved openings named by ID with
  before→after dimensions, per-wall delta lines, affected constraint
  bindings by ID, and `validate_room_authoring_model` issues mapped onto
  the candidate. Anything the preview cannot know — optimization /
  solver readiness — renders as 「不明（実行時に再評価されます）」, never
  silently omitted.
- `htdt.room_geometry_panel` — the inspector itself:
  - `selection_title` + `selection_context` header rows state the
    current target （頂点/辺/壁/開口/形状/なし), its ID, distance/size,
    開口/クリアランス dependency counts and the editability state.
  - `InspectorSection` collapsible groups (vertex / edge / wall /
    opening) hide the non-selected targets' forms and dangerous buttons;
    picking a target expands only its sections and focuses the first
    editable field (existing convention from `room_workspace.py`).
  - Armed preview: `preview_host` (objectName `geometryChangePreview`,
    OVERLAY surface) shows the JA title + bullet lines + blocker list;
    「この内容で適用」 is enabled only when `feasible`; 「変更を取り消す」
    discards. While armed, every commit-capable control is disabled so
    the visible preview is the only pending change.
  - Apply = `geometry.apply_geometry_candidate` inside `_run` → one
    `ReplaceRoomCommand` → exactly one Undo step; no intermediate ops
    on the undo stack. Cancel = `cancel_pending_preview()` → notice
    「変更を取り消しました（部屋は変わりません）」; the document is never
    touched.
  - Stale-revision fencing: the preview records
    `(working.source_revision_id, scene_content_hash(committed))` when
    armed; `refresh()` drops it (「変更前の基準が変わりました」) and
    `_apply_pending_preview` rejects it via `is_stale`, so delayed
    results can never land on a different SceneRevision.
  - Field captions: every numeric field shows `単位 m · 有効範囲
    <min–max>`; dependent bounds (opening offset `有効: 0–len−width`,
    split offset `有効: 0–edge-length`, sill `有効: 0–height−h`) are
    recomputed per selection — invalid values refuse to arm/commit and
    log a `commit_rejected` event.
  - `operation_events` list records `preview_armed`,
    `preview_applied`, `preview_cancelled`, `preview_stale_dropped`,
    `preview_stale_rejected`, `commit`, `commit_rejected` tuples —
    the raw material for #936's op-count / error-rate measurement of
    壁選択→開口編集→Undo.
  - 「編集終了」 tooltip now states that mode exit keeps committed
    changes (use 「元に戻す」/Undo to revert), visually distinct from
    the preview's 適用/取り消し pair.
- `htdt.room_geometry_input` — selection authority extended:
  - `selected_opening_id` + `select_opening(opening_id)` (idempotent;
    `KeyError` for unknown ids). Selecting an opening also selects its
    parent wall edge so the wall context stays coherent.
  - `clear_selection()` public reset used by previews that delete the
    selected target.
  - `apply_geometry_candidate(room, topology, message=...)` — the
    commit seam used by previews: `replace_room` for vertex-only
    changes, `replace_room_topology` otherwise.
  - `_hit_handle` adds an opening-outline hit-test (sill/sill+height
    corners projected, ≤10px, `best*0.5` bias) so clicking an opening in
    3D selects the opening — driving inspector focus — rather than the
    parent wall's edge.
  - `_render_edit_handles` colours the selected opening `#E8A33D` at
    width 5.0 / opacity 0.95 (vs accent for openings on the selected
    wall, muted otherwise) — inspector selection → 3D highlight.
- `htdt.room_workspace` — `cancel_active_operation` inserts
  `geometry_panel.cancel_pending_preview()` between transform cancel
  and geometry-edit cancel: Esc order is now underlay calibration →
  measure → transform → **armed preview** → geometry edit → working-doc
  preview → clear selection.

## Preview semantics

- Armed on: 頂点削除, 壁結合 (`merge_wall_button`), 壁削除
  (`delete_wall_button`), 壁厚 change (`_wall_thickness_edited`),
  開口適用 (`_apply_opening`), 開口削除 (`_delete_opening`).
- Still immediate (not previewed): 頂点 X/Y edit, 辺長/分割,
  クリアランス add/apply/delete, 開放状態 toggle changes that stay
  inside `apply_opening` (apply is previewed; field edits are not).
- Infeasible previews (e.g. wall delete orphaning openings/bindings —
  `WallTopologyError` captured as `blockers`) show the blocker list
  「実行できない理由:」 and keep 適用 disabled; the base document is
  unchanged either way.
- `working.has_preview` (entity-transform preview) is untouched — the
  panel preview is a *panel-level* candidate bound to the revision it
  was computed from.

## Honesty + undo guarantees

- Unknown side-effects render 「不明」 — nothing is synthesized or
  silenced.
- Preview never mutates the SceneRevision: the candidate is computed in
  memory and committed only by 適用.
- One preview = one `ReplaceRoomCommand` = one Undo step; cancel adds
  zero history entries.
- Stale previews (document moved while armed) are dropped at refresh
  and rejected at apply; `mark_pending_editor_rejected` records the
  rejection for the notice surface.

## Validation

- `backend/tests/test_issue_982_geom_inspector.py` — 11 tests:
  sections-follow-target + collapse, opening↔3D selection sync,
  wall-delete preview names affected openings by ID + infeasible-gated
  apply + no-commit guarantee, one-Undo-step apply, stale-revision
  rejection (refresh drop), out-of-bounds opening refuse-to-arm,
  opening-apply diff + apply, Esc-chain order (preview → edit mode),
  topology-less vertex delete, #936 operation_events sequence,
  narrow-width + 200% font readability (wordWrap, cancel labelled).
- `test_rev36_geomport2.py` merge test updated for the two-step
  preview→apply flow.
- Real-GUI (Mesa GL) evidence: host-type header with collapsed
  non-selected sections, wall-delete preview listing `door-…` IDs,
  visually distinct 適用/取り消し/編集終了, 3D click → inspector focus,
  200% DPI readability.
