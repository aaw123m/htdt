# Issue #1009 — 壁面への吸音処理被覆オーバーレイ（実効クリップ済みパッチのみ）

## Scope

`RoomViewport3D` gains a read-only treatment-coverage overlay: each
exact-bound `AcousticTreatmentPlacement` draws the **clipped effective
patch** the solver binds (`TreatmentFootprint.patch_uv_polygons`), never
the authored rectangle. The overlay resolves against the EXACT current
`SceneRevision` on every render — same staleness discipline as #999's
field overlay — so a scene edit can never leave a superseded footprint
drawn on the wall.

- `room_treatment_overlay.py` (new, Qt-free): `resolve_treatment_overlay`
  + `RoomTreatmentOverlayController`. Per resolve: the document's latest
  placement versions are listed (`latest_placements_for_document`), each
  binding is re-evaluated against the live head
  (`evaluate_placement_surface_binding(placement, scene_revision_id=head)`),
  and the footprint is re-derived from the head's semantic geometry via
  `derive_treatment_footprint` — a public entry into the same
  `_derive_footprint` the solver uses.
- Transform path: `patch_uv_polygons` (host-plane UV) → world domain via
  `plane_origin_m + u·plane_u_axis + v·plane_v_axis` → VTK render space via
  `domain_to_render` (x, −y, z), the identical flip convention the field
  overlay uses.
- `RoomViewport3D.render_treatment_overlay` /
  `clear_treatment_overlay`: actors under a new `treatment-overlay-`
  prefix in `_OVERLAY_ACTOR_PREFIXES`, `pickable=False`, translucent fill
  + closed outline per patch ring, ASCII-only status text (VTK drops CJK).
  Signature-skipped renders sweep the prefix via `_remove_overlay_actors`.
- `RoomWorkspace` wires a `RoomTreatmentOverlayController` (scene repo +
  `controller.treatment_repository` + document id) and renders it inside
  `_render`'s `deferred_render` block, gated on the acoustics context +
  acoustics overlay toggle; leaving acoustics clears the actors.
- `RoomTreatmentPanel` gains a 「壁面被覆」 section fed by
  `coverage_provider` (the workspace's `resolve` callable): lifecycle,
  実効/矩形 ratio, clip/overlap notes, and lapsed notices — the same
  vocabulary the viewport draws.

## Vocabulary

| Term | Meaning |
|---|---|
| `TreatmentOverlayPatch` | One drawable patch: placement + footprint + render rings + `patch_area_m2`/`rectangle_area_m2` + `effective_ratio` + `clipped`/`overlap_with` flags. |
| `TreatmentOverlayNotice` | A placement that contributes no drawn patch — `unbound` (ホスト面未設定), `lapsed` (ホストバインド失効 with the binding state), `out_of_bounds` (範囲外 — empty clipped patch), `underivable`, `unreadable`. Surfaced, never silent. |
| `effective_ratio` | `patch_area_m2 / (width_m × height_m)` — how much of the authored rectangle actually covers the host. <1 ⇒ the rectangle overhangs and `clipped=True`. |
| Lifecycle labels | `提案された配置` (proposed, accent fill) vs `設置記録` (installed, success fill) — distinct colors and distinct vocabulary. |
| Binding labels | `BINDING_STATE_LABELS` mirrors the panel's `_BINDING_STATE_LABELS` (`stale_scene_revision`→古い (リビジョン更新), `surface_removed`→面が削除済み, …). |

## Honesty contract

- Only the clipped patch is colored; an overhanging rectangle reports
  `clipped` + its ratio instead of painting untreated wall as treated.
- A placement whose binding does not evaluate `exact` against the CURRENT
  head draws nothing and appears as a notice naming the binding state
  (`stale_scene_revision`, `surface_removed`,
  `surface_authority_mismatch`, `legacy_unverified`, …) — no stale
  footprint survives a scene edit.
- Same-host derived patches that overlap beyond
  `FOOTPRINT_OVERLAP_TOLERANCE_M2` still draw (the conflict stays visible)
  but carry the warning edge color + 「重複」/`OVERLAP` vocabulary — the
  same conservatism the solver applies (`BLOCKED_OVERLAP`).
- Placement reads go through the repository's authority replay; a
  revalidation failure degrades to an `unreadable` notice, never an
  exception mid-render.

## Staleness hookup

The controller caches on `(head.revision_id, head.content_hash, latest
placement SHA set)` — an unchanged scene resolves free, while any new head
or new placement version re-resolves in full. Signature-skipped viewport
renders drop `treatment-overlay-` actors; the compositor's next pass
re-resolves from live authority.

## Validation

`backend/tests/test_issue_1009_treatment_overlay.py` (12 tests):
UV→world→render round-trip on the plane frame, clipped-vs-rectangle
honesty (patch area vs 1.4×1.0 rect, every render vertex inside the face),
proposed/installed + lapsed/unbound/out-of-bounds vocabulary, overlap
warnings, exact-head cache invalidation, viewport actor lifecycle
(prefix + non-pickable + clean sweep), panel 被覆 section, narrow 260px
layout + DPI-200 font + UIA reachable names.
