# Round 6 — specification inconsistency audit

Branch: `devin/rev6-spec`. Scope: cross-module spec drift — unit conventions
at module seams, coordinate/handedness conventions, schema/identity semantics,
API↔UI↔doc drift, and authority/evidence contract asymmetries. Prior rounds
(1–5) already swept edge-case bugs; this pass looks for places where two ends
of the same contract disagree about *meaning*.

Verification: `cd backend && TMPDIR=/c/t C:/devin/python/python.exe -m pytest -q -n 4`
(Python 3.12.10 installed fresh via Chocolatey — the box had none).

## Fixed in this branch

| # | Severity | Location | Finding | Fix |
|---|----------|----------|---------|-----|
| F1 | medium | `room_workspace.py` `MetricSpinBox`, `workflow_application.py` `_make_room`, `application_preferences.py` keys | **Dead display-unit feature.** `IMPLEMENTATION_STATUS` (#496) declares `cad_display_units` "the one shared conversion policy" that "all" length UI runs through, with `length_display_policy_from_preferences` binding `display_input.length_unit`/`numeric_precision`. In reality the module had **zero production callers**: `MetricSpinBox` re-implemented conversion with its own `UNIT_SCALES`/`UNIT_DECIMALS` tables (inch→SI divided by `1/0.0254` instead of the authority's exact `× 0.0254` — a few-ulp divergence from `display_to_si`; inch default decimals 3 vs policy 2), and `SelectionInspector.set_display_units` was only invoked by `test_room_inspector.py`. Users setting "mm" in Preferences (the store default!) still saw metres everywhere. | `MetricSpinBox` now derives `UNIT_SCALES`/`UNIT_DECIMALS` from `cad_display_units` (`si_to_display`, `DEFAULT_DISPLAY_DECIMALS`) and routes every SI↔display conversion through `si_to_display`/`display_to_si` — Qt cosmetics (suffix, step) stay local. New `bind_inspector_display_length_policy(inspector, preferences)` applies the policy at room-workspace construction and re-applies live via `ApplicationPreferenceStore.subscribe` on the two keys; the weakref'd inspector keeps a closed workspace from breaking unrelated preference commits. |
| F2 | low | `authority_graph.py` `semantic_fingerprint`, `cad_readiness_planner.py` `_action_id`, `cad_yamaha_rxa4a.py` `_packet_hash`, `help_registry.py` `fingerprint`, `project_lifecycle.py` `ProjectDeletionPlan.fingerprint` | **Fingerprint serialization diverged from the canonical profile.** Five identity/consistency digests hand-rolled `sha256(json.dumps(...))` with non-canonical profiles: authority_graph/help_registry/project_lifecycle omitted `ensure_ascii=False` and `separators=(',', ':')`; readiness_planner omitted `ensure_ascii=False`; yamaha_rxa4a omitted `allow_nan=False`. The canonical profile is the declared single implementation — "anything hashing 'the same' document through a different profile produces a different digest" — so these were spec-drift sites even though each is recompute-consistent. | All five delegate to `canonical_sha256`. Digest values change, but every site is a recompute-and-compare fingerprint (rebuild determinism, stale-preview pin, deterministic action id) with no persisted literal digests — verified no test pins a hex value. |

## Deferred (needs product decisions)

- **D1 (medium) — room-polygon winding is never normalized, but wall identity
  is direction-dependent.** `RoomPrism.footprint_vertices` is validated as a
  shapely `Polygon` (`cad_scene.py`) yet stored in authored order;
  `make_polygon_room` does not canonicalize winding. `cad_walls._edge_pairs`
  then derives directed `wall:{from_vertex}->{to_vertex}` ids from that order
  and `WallOpening.offset_m` measures along `from→to`. Two documents of the
  same physical room authored with reversed winding get disjoint wall ids and
  mirrored opening offsets — an identity asymmetry across import/authoring
  paths. **Sketch:** normalize winding + choose a deterministic start vertex
  at scene ingest (canonical CCW under +X/+Y), migrating stored wall ids; or
  keep authored order but persist an explicit winding flag so openings stay
  interpretable. Either way it touches stored identities → product decision.
- **D2 (low) — `cad_units` collapses all dB quantities into one `'level'`
  family.** dB SPL (ref 20 µPa), dBFS, gain dB and AVR-relative `volume_db`
  share a single `UnitKind='db'`/`_QUANTITY_FAMILY['level']`, so
  `convert_unit` cannot distinguish reference levels. Today the codebase is
  safe because every surface uses named fields (`*_db_spl`, `volume_db`) and
  `cad_ambient_noise.level_semantics` gates quantitative deltas — verified
  consistent — but the generic authority is a latent trap for future callers.
  **Sketch:** split kinds (`db_spl`, `dbfs`, `db_gain`) or annotate the level
  reference in `UnitKind`; schema decision, deferred.
- **D3 (low) — measure-tool readout ignores the display policy.** With F1
  fixed the inspector follows `display_input.*`, but
  `cad_measure.format_measure_result` still hardcodes `m`/`°` (and report
  tables render `m`/`Hz`/`dB` directly). **Sketch:** decide which surfaces the
  preference governs (inspector only vs. also measure overlay, viewport
  readouts, status bar, report exports — report exports arguably should stay
  SI regardless), then thread `LengthDisplayPolicy` through those renderers.

## Audited — verified consistent, no action

- **Angles/rotation:** one quaternion authority in `cad_scene.py` — intrinsic
  Z-Y-X, degrees at every API edge (`quaternion_from_euler_deg`,
  `rotate_orientation_world`, `quaternion_to_axis_angle`). Azimuth is
  `degrees(atan2(x, y))` measured from +Y (rear) toward +X; pitch is
  `degrees(asin(z))` — identical in `cad_measure`, `cad_objects`
  (`aim_yaw_pitch_deg`/`direction_from_yaw_pitch_deg`) and
  `cad_extended_search` (`aim_horizontal_yaw_deg`/`direction_with_horizontal_yaw`).
- **Phase units:** `phase_deg` (measurement/directivity/FIR/benchmark domain)
  vs `phase_rad` (solver/wave domain) is a deliberate boundary with explicit
  conversions (e.g. `cad_wave_excitation.py` `volume_velocity_phase_deg→rad`
  at excitation build); no module reads one as the other.
- **Import units & axes:** `geometry_import_dialog` forces an explicit unit
  declaration (GLB/HTDTMSH1 pinned to metres; OBJ/PLY/STL require an operator
  pick, 'custom' carries `custom_scale_to_meters`);
  `mesh_import_authority._UNIT_SCALE_TO_METERS` is exact SI and
  `mesh_import_axis_matrix` maps source bases onto HTDT X-right/Y-rear/Z-up
  with `right = forward × up` (post-R1 handedness fix). Capture/datum paths
  declare handedness explicitly ('unknown' rejected in
  `cad_spatial_ir_measurement`; `cad_installation_datum` is `Literal['right']`).
- **dB discipline:** `db_spl` field naming, `level_semantics` separating
  calibrated absolute SPL from relative profiles, quantitative deltas gated on
  absolute-SPL authority — consistent. `volume_db` is AVR-relative by
  convention (named, documented) — see D2 for the generic-converter trap.
- **Hz conventions:** `_hz` field suffixes everywhere; the one table-unit
  seam (`cad_wave_excitation._TABLE_FREQUENCY_UNIT_SCALE` Hz/kHz) converts
  explicitly; REW parser strict `Hz`/`dB`/`deg`; report axes Hz/dB.
- **Scene-revision pinning:** `scene_revision_id`/`scene_content_hash` is a
  both-or-neither pair (`cad_analysis_study` validator); consumers needing a
  pin reject `None` explicitly (`cad_action_item_repository._validate_anchor`,
  measurement `_validate_current_*`). No sibling accepts-unpinned-where-
  sibling-requires asymmetry found.
- **Receiver/ear-height convention:** entity `position` is the physical box
  center; seat ear reference = `position.z + ear_height_m(0.65)` = 1.10 m
  absolute for the default 0.9 m seat, matching the template MLP at `z=1.1`
  and speaker nominal `z = 1.1 + …` in `cad_project_template`. Consistent.
- **`canonical_scene_json` null-omission:** drops absent optional fields so
  pre-feature scene hashes stay stable — deliberate wire-compat behavior.
- **`cad_camilladsp` `_section_text`/`device_context`:** `sort_keys=True`
  without canonical separators — these build stored *config text* and a
  context *string*, not hash inputs; left as-is intentionally.
- **`schema` wire key:** still pinned by `test_schema_wire_key.py`; untouched.

## Summary stats

- 7 files modified: `authority_graph.py`, `cad_readiness_planner.py`,
  `cad_yamaha_rxa4a.py`, `help_registry.py`, `project_lifecycle.py`,
  `room_workspace.py`, `workflow_application.py` + `tests/test_room_inspector.py`
  (+2 regression tests) + this doc.
- Fixed: 1 medium (F1), 1 low (F2, five sites). Deferred-with-sketch: D1, D2, D3.
- Scoped tests: 43 passed (`test_room_inspector`, `test_authority_graph`,
  `test_cad_readiness_planner`, `test_project_lifecycle`, `test_cad_yamaha_rxa4a`,
  `test_help_registry`, `test_cad_display_units`, `test_workflow_help`).
- Full suite: `pytest -q -n 4` — see run result below.
