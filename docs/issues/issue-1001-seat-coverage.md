# Issue #1001 — 座席カバレッジ・マーカー layer

Read-only per-seat coverage evidence on the 3D CAD view: exact
ear-position evidence points — discrete markers at each sealed
`receiver_reference_position_m`, NOT a whole-room heatmap (#755
interpolation stays out of scope for MVP). Every value, colour, verdict
and identity comes verbatim from `CadCoverageRepository` — the UI never
re-computes coverage.

## Layer shape

- `htdt.room_coverage_overlay` — Qt-free resolver + controller.
  `resolve_coverage_overlay(...)` re-reads `current_head` plus the
  sealed `CoverageEvaluation` store every call and returns a
  `CoverageOverlayScene`: `markers` (one per seated result with a
  position), `rays` (speaker→seat geometric direction, only while
  `source_acoustic_axis` is evidence-determined), `seat_rows` (table),
  `legend`, `notices`, `delta_*` (A/B), `provenance`, `options`
  (evaluation selector entries marked `[履歴]`). The
  `RoomSeatCoverageOverlayController` wraps it for the workspace —
  deliberately uncached so a scene edit, a new comparison run or a
  selector change can never leave a superseded marker on screen.
- `RoomViewport3D.render_coverage_overlay(scene)` — discrete spheres
  (cubes for diagnostic-role seats) under the `coverage-overlay-`
  prefix, `pickable=False`, swept with the other overlay prefixes on
  every re-render; a Disc ring for required-role seats, a larger ring
  for the focused seat, ASCII `add_point_labels` (value + optional Δ
  line), thin `pv.Line` rays, an ASCII status line + legend, and
  `clear_coverage_overlay()` for teardown.
- `RoomSeatCoveragePanel` — the placement-レイアウト panel: the
  「カバレッジ表示」 toggle, evaluation/variant selector, frequency
  combo (aggregate + exact requested grid only), quantity combo
  (relative directivity level / off-axis loss / eligibility gate),
  baseline combo (A/B, excluding the current evaluation), status line,
  the seat table, a detail card (ear position, angles, support states,
  provenance, remediation hint), legend chips and the fixed
  「幾何方向のみ」 disclaimer.
- `RoomWorkspace` — wires the repositories (`CadCoverageRepository`
  reusing the same DB path as scene/variant/equipment/directivity), the
  controller and the panel; renders only in the placement/acoustics
  contexts while the acoustics overlay is on; `select_entity` keeps
  selection two-way synced by the stable `seat_entity_id`.

## Honesty contract

- Values are verbatim `relative_level_db` / `off_axis_loss_db` /
  aggregate scalars / seat `coverage_pass` — coloured on a FIXED
  5-band scale (`coverage_band_for_value`), never normalised.
- `coverage_pass` undecided is its own state — never drawn as FAIL or
  0 dB; `n/a` markers render grey/wireframe with the sealed `reason`.
- A seat-level `unsupported` still shows any SUPPORTED per-frequency
  result at that exact grid slot (real evaluated evidence).
- Marker colour, glyph, label and the required/diagnostic legend roles
  are data-driven (`CoverageSeatMarker`), so priority seats can never
  stand in for all seats.
- Staleness: evaluation pinned to a superseded scene revision →
  `historical` (stale-violet wireframe markers at sealed positions,
  labels/rays/Δ withheld); failed binding replay, unknown evaluation or
  off-grid frequency → `blocked`, nothing drawn. A seat id that no
  longer names an entity on the current head is never re-mapped.
- Rays exist only while `source_acoustic_axis is not None` and are
  labelled geometric direction — never a claimed energy path
  (occlusion is not evaluated); a design-vs-observed aim mismatch
  surfaces as a notice instead.
- A/B is gated to the same scene revision + content hash, the same
  aggregation mode (aggregate) or the same requested frequency in the
  baseline's own grid, and byte-identical `receiver_reference_position_m`
  per seat — otherwise Δ cells stay empty and `delta_semantics`
  explains why. Δ is per-seat arithmetic on sealed aggregates; no
  all-seat-mean claim is made.
- `priority_aggregates` on the evaluation is preferred for seat roles;
  the `CadSeatPriorityProfileRepository` fallback binds profiles to the
  evaluation's exact `scene_revision_id + scene_content_hash` and is
  presented as a role hint, never as an aggregate claim.

## Files

- `backend/src/htdt/room_coverage_overlay.py` — resolver, controller,
  fixed band scale, role/glyph + disclaimer tables.
- `backend/src/htdt/room_seat_coverage_panel.py` — Qt panel (JA UI;
  3D viewport stays ASCII-only under Mesa/.ttc).
- `backend/src/htdt/room_viewport.py` — `coverage-overlay-` prefix +
  `render_coverage_overlay` / `clear_coverage_overlay`.
- `backend/src/htdt/room_workspace.py` — wiring, render-pass
  composition, selection sync.
- `backend/tests/test_issue_1001_seat_coverage.py` — verbatim-value,
  unknown-vs-fail, stale/historical/block, A/B gating, no-interpolation
  (10/100 seats), teardown, panel and workspace-level tests.

## Out of scope (MVP)

- Continuous heatmap / smooth interpolation between seats (#755) —
  would need method, extent, uncertainty and interpolation authority.
- Occlusion evaluation — the UI states it is not evaluated rather than
  implying a measured energy path.
- Persisting evaluations that used a `seat_priority` profile — the
  repository's binding replay has no `priority_profile` parameter, so
  such evaluations can never be stored; roles come from
  `priority_aggregates` or a revision-bound profile instead.
