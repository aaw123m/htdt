# Issue #980 — OBJ/PLY/STL/GLB 取込ダイアログの読み取り専用3Dプレビュー

## Scope

`GeometryImportDialog` showed the import mesh only as text: counts,
diagnostics, a string repair summary. Issue #980 adds a read-only 3D
preview pane (PyVista `QtInteractor`, same stack as the room viewport /
fabrication preview) that renders the pre-import mesh with origin
marker, axes, grid and a dimensioned bounding box — and reflects the
unit / up / forward / handedness / anchor declaration immediately.

Nothing about the import authority changes: adoption stays
explicit-confirmation (`import_request()` / `_accept` untouched),
source/destination authority unchanged, and no QA / repair / solver
readiness is recomputed — the pane is a render-only adapter over the
already-computed `RawVisualMesh` + `RawMeshDiagnosticResult` +
`RepairedRawMesh`.

Lives in `backend/src/htdt/geometry_import_preview.py` (pure resolver,
no Qt) and `backend/src/htdt/geometry_import_dialog.py` (pane + wiring);
tests in `backend/tests/test_issue_980_import_preview.py`.

## Layout shape

```
QDialog (1120x720)
├── header / file info / diagnostics group   (unchanged)
├── QSplitter (horizontal)
│   ├── QScrollArea  — declaration / issues / repair / destination
│   │                  groups exactly as before
│   └── QGroupBox 「3Dプレビュー（読み取り専用）」
│       ├── controls row: [表示 QComboBox] [フォーカス QComboBox]
│       ├── info QLabel   — counts, dims, notices, fate legend
│       └── QtInteractor  — mesh + origin + axes + grid + bbox + focus
└── QDialogButtonBox インポート/キャンセル       (unchanged)
```

The declaration column keeps 3/5 of the width, the pane 2/5; the whole
dialog remains usable on narrow screens because the scroll area keeps
every existing control reachable.

## Declaration → preview transform

`resolve_preview_declaration` calls `make_mesh_import_authority` +
`mesh_import_scene_transform` — the *same* transform authority the
entity-body / room commit paths run — so the previewed vertices match
the imported geometry exactly (unit scale → axis rotation → anchor
shift). `test_preview_matches_commit_transform_exactly` asserts
vertex-for-vertex equality against `mesh_import_scene_transform`.

No auto-estimation or forcing:

- unit undeclared (placeholder still selected, or an incomplete
  declaration rejected by the authority model) → scene renders the raw
  source coordinates, `coordinate_space='source_units'`, info label
  「単位未宣言 — ソース座標をそのまま表示（m換算なし）」.
- up/forward unknown or handedness not right/left →
  `mesh_import_axis_matrix` returns `None`, the scene renders
  unrotated with the 「軸・座標系の宣言が未解決」 notice — the same
  honest state the commit authority records.

Changing any declaration widget (`activated` / `valueChanged`)
rebuilds the scene in the same pass, so the meter-converted
dimensions update immediately.

## Original / repaired / diff views

- 元のメッシュ — always available.
- 修復済みメッシュ / 修復差分 — disabled until `_preview_repair()`
  produced a `RepairedRawMesh`; never shows stale repair data (a failed
  re-preview re-disables them).

The diff view colors every *source* triangle by fate, reconstructed
from the actual repair lineage of the same mesh id
(`repaired.source_raw_mesh_id` + `source_raw_mesh_semantic_hash` are
verified — a foreign repaired mesh reports all-`unknown`):

- `kept` — same position multiset, same cyclic order
- `flipped` — same positions, reversed winding (winding repair)
- `moved` — vertex-moving ops ran and the leftover counts match
- `removed` — no surviving repaired entry under the same
  `source_primitive` (a moved triangle still leaves one)
- `unknown` — genuinely ambiguous (removed vs moved unresolvable from
  lineage counts), never guessed

Removed faces get no repaired entry at all — they are colored into the
source-side ghost surfaces is *not* attempted; instead the diff draws
each fate group of the *drawn* faces and the info line prints the
per-fate legend counts, so 削除 counts are visible even where there is
no repaired surface to paint.

## Defect focus

The focus combo lists `_FOCUS_ALL` plus every issue code in
`MeshHealthSummary.issues` (dedup'd, JA labels from
`_ISSUE_CODE_LABELS`). Focus positions come from
`defect_focus_points`, a render-only single-pass derivation:

- open_boundary / watertightness → canonicalized edges with
  incidence 1 → edge midpoints
- non_manifold_edge → incidence > 2 → edge midpoints
- duplicate_face → centroid of the 2nd+ face in each canonicalized group
- sliver_face / tiny_feature → centroids under the diagnostic profile's
  own thresholds
- inverted_normal / overlapping_face / anything else → `state='unknown'`
  with an honest 「この欠陥の位置は判定不能です」 note — these need the
  full orientation propagation / coplanar sweep and are not re-derived
  for display.

The QA verdict itself stays with `diagnose_raw_visual_mesh` /
`MeshHealthSummary`; the adapter never recomputes counts or states.

## Bounded load / decimation

`decimate_for_preview` stride-samples to `PREVIEW_MAX_FACES = 200_000`
deterministically (same mesh → same preview every refresh) and
re-indexes the referenced vertices. It returns `kept_face_indices` so
diff fate colors stay lineage-exact under decimation, and a
「表示用に間引き済み（取込データは変更されません）」 notice is shown.
Decimation is display-only — it never feeds the import request, the
repaired mesh, or any persisted asset. Edge highlighting is capped
separately at `_PREVIEW_EDGE_FACE_LIMIT = 20_000` drawn faces.

## Unsupported vs render failure

- `scene.supported=False` (`unsupported_geometry`) means the geometry
  cannot be drawn — empty or non-finite — reported as such.
- A VTK/Qt failure (no pyvista, no GL context, draw exception) is a
  render-layer failure: the pane falls back to a JA notice and the info
  line gains 「3D描画に失敗しました（メッシュ自体は取込可能です）」 —
  never conflated with geometry support, and the dialog keeps working.
- `done()` closes the plotter to release GL resources on accept/cancel.

## Files

- `backend/src/htdt/geometry_import_preview.py` — new render-only
  resolver (`PreviewDeclaration`, `ImportPreviewScene`,
  `resolve_preview_declaration`, `source_triangle_fates`,
  `defect_focus_points`, `decimate_for_preview`,
  `build_import_preview_scene`).
- `backend/src/htdt/geometry_import_dialog.py` — preview pane,
  declaration/focus/view wiring, repair-view gating, GL cleanup.
- `backend/tests/test_issue_980_import_preview.py` — 19 tests:
  transform parity with the commit authority, honest undeclared/
  unresolved states, fate reconstruction incl. foreign-lineage
  rejection, focus positions, deterministic decimation, dialog pane.
