# Issue #1005 — 設置実現性検査 layer

Read-only 3D inspection surface for persisted `SpeakerInstallationContext`
records: does the declared mount sit on evidence, or is it merely drawn in
space? 「3Dに収まる」は「証跡つきで設置できる」とは違う — this layer renders
the difference. Display only; every verdict comes verbatim from
`cad_installation_feasibility.installation_feasibility()` — the UI never
re-judges.

## Layer shape

- `htdt.installation_feasibility_viewmodel` — presentation model.
  `build_installation_feasibility_preview(document, contexts,
  service_clearances)` maps each persisted context to an
  `InstallationFeasibilityItem` carrying the exact entity, the exact
  wall/ceiling/floor element it binds to (`element` + `element_ref`), the
  derived `InstallationRequest`, the authority's
  `InstallationFeasibilityReport` verbatim (six `FeasibilityCheck` rows:
  substrate/payload/framing/cutout/service_clearance, or mount_surface for
  non-construction hosts), the gated headline verdict, on-site
  `confirmations` in JA, and evidence-gated geometry anchors
  (`wall_face` / `slab_face` / `cutout_volume` / `service_volume` /
  `cavity_volume`). The preview is keyed to `scene_content_hash(document)`
  — `head_sha256`.
- `RoomViewport3D.render_installation_feasibility_overlay(preview)` —
  draws `installation-feasibility-*` actors (all `pickable=False`): a
  verdict-colored glyph sphere per entity and per substrate anchor, a
  translucent mounting-face fill + outline on the bound wall/slab, and
  translucent extrusions for cutout depth (amber), service clearance
  (cyan) and declared cavity depth (green), plus summary / disclaimer /
  legend text actors.
- `RoomInstallationFeasibilityPanel` — placement-page panel
  (`設置実現性検査（読み取り専用レイヤー）`, checkbox
  `installFeasibilityPreviewToggle`): honest counts
  （記録N件 · 証拠あり… · 要確認項目…), a verdict-colored item list, and a
  per-selection detail block showing every `FeasibilityCheck` reason in JA
  (`cutout depth 0.280m exceeds cavity 0.100m` passes through verbatim)
  plus 現場確認項目.
- `RoomWorkspace._render` rebuilds the preview every pass from
  `controller.document` + `installation_panel.context_repository` +
  `CadRoomQualificationRepository` service envelopes — edits lapse the
  overlay honestly.

## Request provenance (no invented inputs)

Every `InstallationRequest` field traces to a declared authority:

- `mount_surface` / `element` / `element_ref` from the recorded
  `selected_mounting_mode` + `host_entity_id` (wall ids only bind when the
  host IS a wall in `wall_topology`; entity hosts honestly degrade to
  `furniture_top` → `not_applicable`).
- `payload_kg` only from the entity's declared
  `SemanticCapabilityBinding(capability='mountable')` — when absent the
  check list still renders but the headline stays 未評価 (gray) rather than
  silently passing at 0 kg.
- `cutout_depth_m` for in-wall mounts derived from the entity's declared
  XY footprint span along the wall normal; `in_ceiling` uses `size_m.z`.
- `service_clearance_m` only from a persisted `ServiceEnvelopeProfile`
  keyed by entity ref_id.

Undeclared derivations surface as states (`undeclared`, `unbound_host`,
`missing_entity`) and/or JA confirmation items — never as invented
numbers.

## Verdict vocabulary

`supported` 証拠あり `#59d98c` · `conflict` 不適合 `#e05555` ·
`unknown` 不明 `#8a93a3` (gray — NEVER the conflict red; unknown items are
translated into on-site confirmation items) · `not_applicable` 対象外
`#6b7280` · unevaluated items render a wireframe glyph instead of a filled
one.

## Honesty wording

The disclaimer states this surface is NOT 施工許可・建築基準・耐荷重法規
への適合認定, and that 「不明」は不適合ではなく現場確認項目です. Volumes
render only when the backing dimension is evidence-backed: an assembly
without a cavity layer draws no invented depth (cutout shows unknown, no
cavity volume at all).

## Known limitation

VTK text actors on this test box drop CJK glyphs (font fallback) — the
3D legend/summary/disclaimer render partial text here; the Qt panel shows
everything. Same environmental limitation as `opclear-*` overlay text.

## Tests

`backend/tests/test_issue_1005_installation_inspection.py` — 20 tests:
viewmodel binding (entity + exact wall/slab element), glyph vocabulary
(unknown is gray, never conflict red), verbatim check surfacing,
undeclared-payload headline gating, entity-host `not_applicable`,
evidence-gated volumes (masonry: no cavity volume), in-wall
cutout>declared-cavity conflict, head-sha staleness on scene edits,
entity-removal honesty (対象機器が不在), wall-face anchor pins at the
entity's true wall projection, panel JA detail + confirmations,
viewport actor cleanup, and narrow-320px / DPI-2x / UIA accessibility.
