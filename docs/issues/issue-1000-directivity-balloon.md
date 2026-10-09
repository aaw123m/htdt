# Issue #1000 — スピーカー指向性バルーン overlay

Per-speaker measured-directivity balloons on the 3D room viewport. Pick a
speaker and see — per exact measured frequency — the direction-dependent
relative level in 3D, keyed to the speaker's *installed* aim so install
aim-vs-measurement-frame mismatches and coverage gaps toward seats are
visually obvious. Distinct from #999's in-room field volume: this is the
source-local *measured* directivity, never a propagated field.

## Layer shape

- `htdt.room_directivity_overlay` — Qt-free resolver + controller.
  `resolve_directivity_overlay(...)` re-reads `current_head` and every
  bound authority through the sealed repositories on each call and returns
  a `DirectivityOverlayScene`: `balloons` (one `DirectivityBalloonViewModel`
  per speaker), `options` (speaker selector rows), `legend` (relative-dB
  colour scale + aim-arrow keys + UNMEASURED), ASCII `viewport_lines`,
  JA `notices`, `summary_ja`, `provenance`.
  `RoomDirectivityOverlayController` wraps it for the workspace with a
  staleness key pinning the head revision + content hash and the full
  authority snapshot (context sha, dataset ids + payload hash + asset
  presence, aim sha) — a scene edit, a new dataset, or a removed asset
  re-derives every vertex on the next render.
- `RoomViewport3D.render_directivity_overlay(scene)` /
  `clear_directivity_overlay()` — `directivity-` actor prefix registered
  in `_OVERLAY_ACTOR_PREFIXES` so the compositor sweeps the overlay with
  the rest; non-pickable; a `pv.PolyData` surface built ONLY from
  complete measured quads for `full_sphere_grid` datasets, measured-point
  clouds for `hv_cuts_suspect`/partial grids, gray wireframe spheres for
  honest UNKNOWN/blocked speakers, `pv.Line` aim arrows, an ASCII status
  block + legend. All viewport text is ASCII (Mesa drops CJK); JA strings
  live in the Qt panel only.
- `RoomDirectivityPanel` — the placement-column Qt panel: 「3Dに指向性
  バルーンを表示」 toggle, speaker combo (all / one), exact-grid
  frequency combo (rebuilt from the resolved scene), display-floor combo,
  status/detail JA labels and the fixed disclaimer.
- `RoomWorkspace` — wires `CadDirectivityRepository`,
  `CadInstallationContextRepository` and `CadCoverageAimRepository`
  (same DB as scene/equipment), the controller and the panel; renders in
  the placement/acoustics contexts while the 音響 overlay is on and the
  overlay is armed.

## Authority chain (no synthesized shapes)

- Equipment binding comes from `SpeakerInstallationContext` via
  `CadInstallationContextRepository.latest_contexts_for_document` —
  never a guessed speaker model.
- Datasets come from `CadDirectivityRepository.list_datasets_for_definition`
  — every row re-verifies the bound equipment definition, the managed
  source asset and the recorded import replay; a missing asset or a
  tampered row fails closed → `blocked`, never silently degraded.
- Orientation keys to `entity.aim_xyz` through the same
  `explicit-aim-body-up-source-frame-1` convention as the coverage
  evaluator (`cad_coverage._source_frame`): aim is the acoustic reference
  axis in world; body +Z supplies only the roll reference; body pose
  never substitutes for an unknown aim → `aim_xyz is None` renders
  UNKNOWN, not a guessed direction.
- When a `CadAcousticAimState` exists, declared axes (`design_aim_target`,
  `as_built_observed_aim`, `cabinet_pose`) draw as separate arrows next to
  the installed `aim` arrow, and ≥0.5° divergences surface as notices —
  disagreement is shown, never merged.

## Display honesty contract

- Frequency selection is the dataset's exact measured grid only; an
  off-grid request is `blocked` — the overlay never interpolates between
  frequencies or across measured angular samples.
- Vertices sit at *declared* grid angles only. Each cell's direction is
  round-tripped through the evaluator's own angle semantics
  (`direction_to_directivity_angles`); a declared cell the semantics
  cannot reach (e.g. |v|=90° h≠0 corners under `horizontal_vertical`)
  is reported as an `unreachable_under_declared_semantics` gap, never
  drawn at a wrong position.
- Faces span only fully-measured quads — wrap seams are bridged only for
  a grid-like `signed_180` seam (≤2× interior step), poles and unmeasured
  cells never get invented surface. `hv_cuts_suspect`/partial grids always
  render point clouds.
- Radius is a display mapping of relative dB:
  `r = r_min + (r_max − r_min) · clamp((gain − floor_db)/(0 − floor_db))`,
  r_max derived from the entity envelope. It is labelled relative-dB
  everywhere and is never a physical distance, an SPL, or an in-room
  propagated level.
- Vertex budget is bounded (~6000): denser grids are index-decimated by
  stride and the decimation is recorded as a gap notice — LOD never
  fabricates interpolated samples.

## States

`mesh` (closed measured surface + grid points) · `points` (measured
direction cloud) · `unknown` (gray wireframe — no sealed data / no aim /
no equipment binding) · `blocked` (authority re-verification failed,
off-grid frequency, missing asset — fail closed).

## Evidence

- `backend/tests/test_issue_1000_directivity_balloon.py` — 13 tests:
  UNKNOWN honesty (no context / no dataset / no aim), aim binding and
  sign-correct rotation, exact-grid frequency blocking, mesh vs points
  classification, radius display mapping, fail-closed asset loss,
  declared-axis arrows + divergence notice, staleness lapse on scene
  edit, controller arm/clear, and workspace toggle draw/clear.
- Real-GUI evidence (testing agent): seeded room project, Mesa GL, panel
  toggle on/off, balloon visible in the 3D viewport.

Refs #1000
