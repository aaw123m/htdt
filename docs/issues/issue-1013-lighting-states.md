# Issue #1013 — 3D Room lighting-scene state preview

## Summary

Room/Video now hosts a read-only 「照明シーン」 preview toggle that draws
the *configured* state of the currently selected `LightingScene` onto the
3D room — keeping `desired` / `commanded` / `read_back` / `measured` as
four visually separate channels that are never merged into one "actual"
state.

## Preview shape

- `backend/src/htdt/room_lighting_preview.py` — pure presentation model
  (`build_lighting_scene_preview`). No I/O, no device access; derives
  pins, the `3D未配置` list, zone rows, missing refs, and legends from a
  sealed `LightingScene` + fixture/zone inventory + commissioning
  records.
- `RoomViewport3D.render_lighting_scene_preview` — draws per pin:
  - an anchor disc on the exact entity position (zone-role color, or
    neutral when no zone assigns the fixture);
  - a row of four stage beads above the entity top — one bead per stage,
    stage color when known, dim `不明` bead when the stage was never
    recorded (never 0%, never the desired echo);
  - a wireframe marker when several scene refs assign one fixture
    (`※複合割当`), a line to the resolved bias-target entity for bias
    zones, and a per-fixture `stage_bead_label` (`目30% 送– 応0% 測–`);
  - viewport text blocks: scene identity/counts, `3D未配置` + unresolved
    refs, the disclaimer, and a stage+zone legend.
- `RoomLightingPreviewPanel` — toggle `照明シーンを3Dに表示`, scene
  identity line, counts (ピン / 3D未配置 / 未解決参照 / 無視した他シーン
  記録), the `3D未配置・ゾーン` list with per-item tooltips, and the
  honesty disclaimer. Lives on the placement (Room/Video) page.
- `RoomWorkspace` — `lighting_scene` flag on `RoomOverlayState`, a
  `lighting-*` overlay-actor prefix swept by `_remove_overlay_actors`,
  `CadLightingRepository.current_scene(document_id)` for the authority,
  and a `lighting_inventory_provider` seam returning
  `(fixtures, zones, records)` — inventory persistence does not exist
  yet, so the default empty inventory honestly reports scene refs as
  unresolved rather than inventing placements.

## Binding rules

- A fixture is pinned **only** when named by the scene (directly or via
  a zone member list) **and** its `entity_id` resolves exactly to an
  entity in the current document.
- `entity_id=None` (control-only) fixtures and dangling bindings stay in
  the `3D未配置` list with a reason (`制御専用` / `バインド先の部屋物体
  が未解決`) — never drawn, never guessed.
- Direct `fixture:` states win the desired slot; otherwise the first
  zone assignment in scene order applies. Several assigning refs flag
  `multi_assigned` — states are never merged.
- Commissioning records participate only when bound to the exact scene
  identity (`scene_id` + `version` + `scene_sha256`); foreign records
  are ignored and counted (`他シーン記録N件は無視`).
- Zone-level records stay at zone level — a `zone:` read-back is zone
  evidence and never fills member pins' device stages.

## State vocabulary (never conflated)

| Slot        | Label          | Meaning                              |
|-------------|----------------|--------------------------------------|
| `desired`   | 目標 desired   | design target (scene or record snap) |
| `commanded` | 送信 commanded | a request was sent                   |
| `read_back` | 応答 read-back | device's own report — not lux        |
| `measured`  | 実測 measured  | real observation                     |
| —           | 不明 unknown   | stage absent — dim bead, `–`         |

`read_back` is never promoted into `measured`; `desired 30%` +
`read_back 0%` renders as `目30% 応0%`, never "on". Recorded non-desired
stages without `observed_at_utc` are flagged stale. Capability-mismatched
state (`level` on non-dimmable, `cct`, `color`) marks `非対応:...`.

## Honesty contract

Markers/colors are **explanation symbols of configured state** — never a
claim of lux, beam spread, or room illumination. The legend + disclaimer
state this; the preview sends nothing (「適用」「実機読出し」 remain on
the approved `cad_hue_lighting` device-action path). All overlay actors
are non-pickable.

## Tests

`backend/tests/test_issue_1013_lighting_states.py` — 23 tests: exact-
binding pins, control-only/dangling listing, four-stage distinction,
unknown honesty, foreign-record ignore, stale flag, zone expansion +
multi-assign + bias link + zone-level evidence fencing, missing refs,
capability-unsupported marks, legend/disclaimer claims, viewport actor
lifecycle + dim unknown beads + prefix sweep, panel signal/lists/narrow-
UI (200%-font proxy), and workspace toggle render/clear + no-scene path.
