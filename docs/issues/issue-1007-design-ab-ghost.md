# Issue #1007 — A/B design-alternative diff overlay

Read-only 3D comparison surface for persisted `DesignComparisonSet`
manifests: color-coded diff ghosts of two pinned alternatives plus a
change-reason card （差分理由カード）, driven solely by the comparison
authority. Distinct from `render_history_ghost` (single gray wireframe of
an old revision) and from the #981 IFC old-vs-new import diff — this
surface compares *saved design alternatives*, not history or imports.

## Layer shape

- `htdt.design_ab_overlay` — presentation model.
  `build_ab_overlay_preview(comparison_set, before, after, *,
  resolve_document, evidence_resolver=None, head_revision_id=None)`
  returns a `DesignAbOverlayPreview` render plan: categorized entity
  items, unchanged context entities, frame state, evidence-availability
  rows, summary/disclaimer/legend — or an 'impossible' state with
  explicit reasons.
- `RoomViewport3D.render_design_ab_overlay(preview, *, show_context,
  highlight_entity_id)` — draws `abdiff-*` actors (all
  `pickable=False`): one bounded wireframe ghost per categorized entity,
  plus summary text (upper right), disclaimer (left edge), legend (lower
  left), and the orientation axes. `clear_design_ab_overlay()` removes
  the whole layer; `'abdiff-'` is registered in
  `_OVERLAY_ACTOR_PREFIXES` so every bulk sweep (scene rebuild,
  toggle-off, project switch) drops it too.
- Presentation workspace `compare` page — a `表示:` combo adds
  重畳（単一画面） and 差分のみ alongside the existing 横並び; the new
  `compare_stack` page holds one read-only overlay viewport and the
  差分理由カード list. Mode switches re-dispatch the loaded pair
  silently; every load re-verifies both pins through
  `CadPresentationRepository.alternative_document` (exact
  SceneRevision + content hash), so a stale pin degrades to the honest
  比較不可 state rather than painting guessed ghosts.
- Diff-card rows carry the pinned entity id; clicking a row re-renders
  the overlay with that entity highlighted (thicker wireframe). No
  selection or edit is implied — the surface stays read-only.

## Verdict source (authority-only)

`diff_alternatives(before, after, before_document=…, after_document=…)`
— itself delegating the scene component to
`diff_scene_documents` — is the SOLE comparison authority. The overlay
never derives a changed/unchanged verdict from mesh or coordinate
differences; category mapping is a straight read of the authority's
`SceneDiff`:

| Authority diff | Category | Ghost |
|---|---|---|
| `removed_entity_ids` | 削除（案Aのみ） | red wireframe at the OLD persisted position |
| `added_entity_ids` | 追加（案Bのみ） | green wireframe at the NEW persisted position |
| `entity_changes` with position/orientation fields | 移動・回転（元→先） | amber origin outline + destination outline + authoritative arrow |
| … same, on a degraded frame | 位置関係（参考表示） | paired amber outlines only — NEVER an arrow |
| … `kind` field changed | 属性変更のみ | blue outline — surface identity is in doubt, no positional claim |
| … any other fields (material/size/role/…) | 属性変更のみ | blue outline + attribute-diff card row naming the fields |

## Frame honesty

- Alternatives resolving to different `document_id`s or different
  `coordinate_system`s are 比較不可 with explicit reasons — a foreign
  project can never ghost-display as the current scene. Resolution
  failures (hash mismatch, missing revision) are likewise listed, never
  guessed past.
- Room-prism / wall-topology / R120 semantic-geometry differences flip
  `frame_state` to 'degraded': positional correspondence becomes
  reference-only (paired outlines, no arrows, both room shells drawn in
  their A/B colors), and `AB_OVERLAY_DEGRADED_FRAME_NOTE` is appended to
  the disclaimer.
- `staleness_note` always names which side (if any) is the current head
  — a pinned pair stays a historical comparison even after the head
  advances, and it is never presented as a diff against HEAD.

## Change reasons (差分理由カード)

Each card row names the entity, its category label, the changed field
labels (`ENTITY_FIELD_LABELS`), and the reason. Reasons come only from
recorded evidence — the B side's `semantic_change_summary`, then A's,
then unique evidence-ref labels; a missing record prints 理由未記録，
never a fabricated rationale. A trailing section lists every evidence
ref with its exact `EvidenceAvailability` state (利用可/参照未解決/
別プロジェクト/ハッシュ不一致/基準不一致/文脈不一致/解決不能/未対応)
— unresolvable or unsupported evidence is never colored as an
improvement.

## Category vocabulary

```python
AB_OVERLAY_CATEGORY_VOCAB = {
    'removed':              ('削除（案Aのみ）',        '#e05555'),
    'added':                ('追加（案Bのみ）',        '#59d98c'),
    'moved':                ('移動・回転（元→先）',    '#ffb340'),
    'position_unverified':  ('位置関係（参考表示）',   '#b8892a'),
    'attribute_only':       ('属性変更のみ',           '#4da3ff'),
}
AB_OVERLAY_CONTEXT_COLOR = '#77808c'   # 同一（変更なし） context ghosts
```

Colors are explanation symbols only — they never encode
improvement/winner (no scoring, per the comparison-set contract).

## Tests

`backend/tests/test_issue_1007_ab_overlay.py` (22 tests): authority-only
category mapping, cross-document / coordinate-frame / unresolvable-pin /
same-alternative impossible states, degraded-frame arrow suppression,
recorded-reason precedence + 理由未記録， evidence-availability rows,
staleness wording, viewport actor shape + pickable=False + cleanup +
prefix sweep, 重畳/差分のみ mode switching, impossible-state card,
row-click highlight, refresh cleanup, narrow/DPI200/UIA, and a
120-entity scale check.

## Verified on real GUI

Windows PySide6/VTK GUI on Mesa GL: seeded comparison set rendered all
categories, the card rows, legend, and disclaimer; 差分のみ dropped only
context actors; 横並び restored the two pinned views. Pre-existing note:
left-drag on any `RoomViewport3D` is consumed by the selection marquee —
wheel zoom works; whether orbit should be reachable on read-only
surfaces is a separate product question, not part of #1007.
