# Issue #1010 — 「運用クリアランス」 layer

Read-only 3D display layer for persisted operational zones (door swing /
recline / rack slide-out / service access / rotation) and their clearance
conflicts on the Room viewport. Display only — no new geometry math, and
unrelated to #1005 mounting-substrate eligibility (movable-time
interference only).

## Layer shape

- `htdt.room_operational_clearance` — presentation model.
  `build_operational_clearance_preview(document, enabled_kinds=…)` resolves
  every declared `OperationalZone` into a render item carrying the exact
  world-XY envelope polygon from `operational_zone_footprint`, plus
  conflict render items derived solely from `operational_clearance_conflicts`.
- `RoomViewport3D.render_operational_clearance_overlay(preview)` — draws
  `opclear-*` actors (all `pickable=False`): a shaded floor fill +
  outline per zone colored by kind （開閉/リクライニング/引出し/サービス/
  回転）, a faint prism only for zones with declared `height_max_m`,
  red clash-region fills plus the *other side's* outline for every
  conflict (conflicting entity footprint / other zone / room-boundary
  segment), dim wireframe UNKNOWN beads for unevaluatable zones, and dim
  未宣言 discs over physical entities that declare no zones.
- `RoomOperationalClearancePanel` — placement-page panel: preview toggle,
  five zone-kind filter checkboxes, honest counts, disclaimer.
- `RoomOverlayState.operational_clearance` / `.operational_zone_kinds`
  carry the layer state; `RoomWorkspace._render` recomputes the preview
  on every refresh from `controller.document`.

## Conflict surfacing

`operational_clearance_conflicts` is the only conflict source. Each
record highlights both parties: the owning zone outline turns conflict
red, the overlap/outside-room region fills red, and the conflicting
side (other entity footprint, other zone footprint, or the room
boundary line + contact points) gets its own highlight actor.

## Honesty wording

- Legend/disclaimer state XY-projection only — NOT an exact 3D
  swept-solid collision test.
- Zones without height data render flat and earn no "clear in height"
  claim.
- UNKNOWN zones (footprint not evaluatable) and 未宣言 entities are
  presented as 判定不能 — visually and in counts distinct from
  "no interference".

## Live refresh

The preview is rebuilt from `controller.document` on every `_render()`.
During a drag preview the working document already reflects the pending
transform, so footprints and conflicts follow the pointer live — no
stale-revision results are ever painted.

## Tests

`backend/tests/test_issue_1010_operational_clearance.py` — 16 tests:
footprint→actor mapping, conflict dual-highlight for all three conflict
kinds, UNKNOWN/未宣言 vs clear distinction, kind-filter honest counts,
disclaimer wording, live refresh during move preview, workspace
toggle/filter wiring, narrow-UI / DPI-200 / UIA reachability.
