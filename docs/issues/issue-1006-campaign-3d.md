# Issue #1006 — 測定キャンペーン計画点/ホールドアウト/進捗を部屋3Dビューへオーバーレイ

## Scope

`RoomViewport3D` gains a read-only campaign overlay: every `CampaignPoint`
of the selected `SpatialCampaignDesign` draws a **microphone marker at its
exact declared XYZ** on the CURRENT `SceneRevision` — never re-projected,
never approximated. Marker SHAPE carries the point's primary role so a
spatial holdout can never visually merge with optimization points, and a
progress ring carries only what persisted authority says (runner cell
states + point bindings) — unknown stays unknown.

This is a READ/SELECT surface per the issue: it does not rebuild #210's
measurement-target pattern editing or #956's runner wiring. It reads
`SpatialCampaignDesign` + campaign-executor cells spatially and links the
checklist UI to the room for spatial intent and holdout distribution.

- `room_campaign_overlay.py` (new, Qt-free): `resolve_campaign_overlay`
  + `RoomCampaignOverlayController`. Resolves against the CURRENT head on
  every render — `design.scene_revision_id`/`scene_content_hash` vs
  `current_head` (same staleness lesson as #999/#1009): a superseded
  design lapses, all progress/validity claims are withheld, the marker set
  dims to the stale color.
- Marker geometry: `position` is used verbatim (the point is already an
  acoustic reference), converted through `domain_to_render` (x, −y, z) —
  identical convention to the field/treatment overlays.
- `RoomViewport3D.render_campaign_overlay` / `clear_campaign_overlay`:
  actors under `campaign-overlay-` in `_OVERLAY_ACTOR_PREFIXES`,
  `pickable=False` on every actor, ASCII-only viewport lines (VTK drops
  CJK), legend in the upper-right, status lines upper-left.
- `RoomWorkspace` wires a `RoomCampaignOverlayController` (scene repo +
  `CadSpatialCampaignRepository` + `CadMeasurementRunnerRepository`) and
  renders inside `_render`'s deferred block — only while the acoustics
  context is shown and the acoustics overlay toggle is on and the overlay
  is armed (it is a focused inspection surface, not an always-on layer).
- Deep link: the campaign checklist gains 「3Dで測定位置を確認」
  (`campaign3dButton`, UIA name 「測定位置を3Dで確認」). It emits a
  `WorkspaceDeepLink` with `kind=MEASUREMENT_CAMPAIGN` carrying the
  selected row's `target_entity_id` (marker↔cell sync, bounded by the
  position join) or, with no selection, the newest design's `design_id`.
  `NavigationTargetKind.MEASUREMENT_CAMPAIGN` is added to the room
  workspace mount's `focus_kinds` (and the app composition's `_make_room`
  focus port), so the link lands on `focus_campaign_overlay`: arm →
  `set_context('acoustics')` → re-render → `TargetFocusResult`. The room
  REGISTRATION declares the same kinds via
  `build_canonical_workspace_registrations(focus_kinds=...)` — an
  unmounted destination resolves capabilities from the registration, so
  the FIRST click in a fresh session is focusable, not degraded to
  `request_entity`.
- Marker→cell sync is bounded by honesty: a selected cell's
  `target_entity_id` joins a design point only through
  `acoustic_reference_position` within
  `expectations.duplicate_tolerance_m`; no match surfaces a 「位置一致
  なし」 notice instead of guessing. The executor's
  `next_incomplete_cell` also drives the same focus ring — the guided
  next-acquisition marker is emphasized in white.

## Vocabulary

| Term | Meaning |
|---|---|
| `CampaignOverlayMarker` | One drawable marker: point_id, declared position, roles + `primary_role` + `glyph`, `progress` + detail, `validity`, zone_id, provenance, focused, channel_coverage. |
| `CampaignOverlayScene` | Resolved surface: revision id + hash, design id, `lapsed`, `scene_unbound`, `progress_source`, markers, legend, `progress_counts`, `shown_count`/`total_count`, notices, ASCII `viewport_lines`, JA `summary_ja` with 表示x/全体y. |
| Role → glyph | `reference_alignment`→cone, `optimization`→sphere, `spatial_holdout`→cube, `repeatability`→disc, `diagnostic`→wireframe sphere, `boundary_stress`→cylinder, `standards_required`→diamond — seven distinct shapes AND seven distinct colors, with legend ASCII (REF/OPT/HOLDOUT/REP/DIAG/BND/STD). `campaign_primary_role` orders holdout/boundary/standards ahead of optimization: holdout NEVER borrows the optimization sphere or color. |
| `CampaignProgress` | `measured`(測定済み), `blocked`(要対応), `staged`(取り込み済), `pending`(未測定), `skipped`(スキップ済み), `unbound`(セル未結合), `unknown`(不明), `lapsed`(失効) — drawn as a progress ring around the marker; lapsed markers render wireframe in the stale color. |
| `CellValidity` | zone membership first (`excluded`→除外ゾーン内, `challenge_region`→チャレンジ領域内, none→リスニングエリア外), then room bounds (`outside_room`→室外形), then `head_height_range_m` (`height_out_of_range`→耳高範囲外). Anything not derivable from declared geometry is `不明` — never inferred. |

## Honesty contract

- Progress joins ONLY persisted authority: runner cells whose
  `cell_states` exist in the latest run for the document's newest plan
  (matched to design points by `acoustic_reference_position` within
  `duplicate_tolerance_m`), plus persisted `CampaignPointBinding` rows
  (a binding or a `completed` cell ⇒ `measured`). No executor run, no
  plan, or a repository failure ⇒ explicit `セル未結合`/`不明` plus an
  ASCII "progress: no executor run (unknown)" viewport line — never a
  silent claim.
- Lapsed designs (pinned to a superseded revision, or scene content hash
  drift) render every marker dimmed + `unknown` progress + `unknown`
  validity, with `LAPSED` in the status line and 失効 in the summary —
  the counts still show the drawn markers honestly (表示x/全体y).
- Unbound designs (no scene revision bound at declaration) keep markers
  but declare the missing binding as a notice.
- Validity vocabulary comes only from the design's declared
  `ListeningAreaSpec` zones, the `RoomPrism` envelope, and declared
  `head_height_range_m` — assessment is never invented; missing room or
  missing height range yields `不明`.
- Marker↔cell selection sync is bounded: no position match ⇒ notice, no
  focus ring. Count honesty: `progress_counts` + `shown/total` always
  enumerate every declared point.

## Staleness hookup

The controller caches on `(head.revision_id, head.content_hash, armed
request)` — an unchanged scene resolves free, a new head re-resolves in
full and flips `lapsed` when the design is pinned to the old revision.
`clear_campaign_overlay` sweeps the `campaign-overlay-` prefix and is
always safe to call.

## Validation

`backend/tests/test_issue_1006_campaign_3d.py` (15 tests):
exact-XYZ marker placement (`mesh.GetCenter()` == `domain_to_render` of
the declared point, both resolved and rendered), role glyph + ASCII legend
covering all seven roles, holdout-vs-optimization separation (shape AND
color, holdout wins when roles share a point), executor progress honesty
(staged/blocked/measured/unbound counts, binding-only measurement, no-run
→ unknown line), lapsed revision → all claims withheld + LAPSED line,
controller cache invalidation on new head, bounded focus_entity_id sync
(no join ⇒ honest notice, no focus), explicit design_id selection vs
newest-default, non-pickable actor discipline + prefix sweep +
`_OVERLAY_ACTOR_PREFIXES` registration, `build_room_workspace_mount`
focus_kinds + `focus_campaign_overlay` arming + acoustics context switch,
campaign-page button deep link (selected holdout row → entity id; no
selection → design id), UIA accessible name, narrow 260px + DPI-200
font reachability.
