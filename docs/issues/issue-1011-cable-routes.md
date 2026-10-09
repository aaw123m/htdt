# Issue #1011 — ケーブル配線の検査一覧 + 経路ジオメトリ権威 (REV73)

M1 + M2 を実装。M3 (3D ウェイポイント記録 UI) は構造的な注記とともに
defer — 理由は末尾に記載。

## M1: 配線一覧 (検査サービス + パネル + 3D オーバーレイ)

`cad_cable_run_inspection.py::inspect_cable_runs` が読み取り専用の投影を
組み立てる。既存 `CableRun` 権威を `SceneRepository.current_head` の
content hash、`CadSignalPathRepository.list_paths` のエッジ集合、
シーンエンティティ表と照合し、端点バインド (bound / unbound / missing)、
信号経路エッジ解決状態、宣言区間 (path_kind / 記録長 / description)、
サービスループ、バージョン系譜、current/stale/missing 鮮度を返す。

- `CableRunPanel` (`cable_run_panel.py`): 配置ページの InstallationPanel
  直下にマウント。一覧 + 詳細 (宣言長、区間別 path_kind、サービス
  ループ、系譜、経路の登録状態)。`bind_length_policy_widget` で表示単位
  ポリシーに追従。
- `route_state` — `unregistered` / `registered` / `stale`。3D の正直な
  契約: `bound` 端点のみが位置を持つ。`unbound`/`missing` 端点は位置を
  持たず、ビューポートはそれらを置けない (=物理的に線を引けない)。
- `RoomViewport3D.render_cable_route_overlay` (`room_viewport.py`):
  bound 端点を球体 + ラベルで描画し、経路未登録の区間には両端点の中点に
  `経路形状未登録` ラベルを置く。**両端点間に線は引かない — 常に**。
  全アクター non-pickable + `cable-route-` プレフィクスで
  `_OVERLAY_ACTOR_PREFIXES` による一括 cleanup に乗る。`render_document`
  のシグネチャ skip 経路でもオーバーレイは除去される (stale/head drift
  で残り続けない)。

## M2: `CableRunGeometry` (スキーマ v116)

新規 sealed テーブル `cad_cable_run_geometries` — `cad_cable_runs` を
拡張せず別権威にしたため、legacy の「記録長のみ」の run は一切無効化
されない (ジオメトリ不在のまま現行のまま)。v116 配線一式:
`NATIVE_BASELINE_DDL` 追記 (CREATE + 2 INDEX) / `NATIVE_SCHEMA_TABLES` /
`NATIVE_SCHEMA_VERSION=116` + `_migrate_115_to_116` (baseline DDL 再実行)
/ `_ROW_BINDINGS` / audit `_ReplayProbe` + `_RepositoryChain._build`
factory 分岐 / `_LIFECYCLE_TABLE_LABELS` `ケーブル経路ジオメトリ`。

権威の形 (`cad_cable_run_geometry.py`):
- `CableRunSegmentGeometry` — `segment_sequence` に対し `waypoints` (XYZ,
  m, 有限, ≥2 点, 順序保持)、`concealment_kind` (run の宣言 `path_kind`
  とは別の「記録された」種別)、`traversed_surface_ids` (wall / opening /
  soffit / riser / partial wall / adjacent region の id; freshness で
  `scene_surface_ids` と照合)、`source` (authored / imported /
  as_built_survey)、`record_kind` (design / as_built — 設計対施工は別の
  数値・別レイヤとして併記し、施工記録のみが実測を主張する)、`note`。
- `CableRunGeometry` — `run_id + run_version + run_semantic_sha256 +
  scene_revision_id + scene_content_hash` で run の正確なレコードに
  ピン留め。`geometric_length_m` は各区間の polyline 長の決定論的和で、
  run の `total_length_m` (宣言+サービスループ) とは別のまま。
- `evaluate_cable_run_geometry_freshness` — current/stale/missing を共有:
  ピン先 run の semantic hash や scene pin が動くと stale、run 不存・
  traversing 先 surface 不存で missing。
- `evaluate_cable_run_geometry_divergence` — 区間ごとの宣言長と幾何長を
  別の数値として併記 (差分は警告表面であり、記録長を上書きしない)。
  `unregistered_sequences` (ジオメトリ未登録の宣言区間) と
  `concealment_mismatch` も表面化。サービスループは常に独立した数値。

## M3 defer — 構造的注記

3D 上でのウェイポイント記録 (クリックして点を打つ) は、VTK picking を
追加するだけでなく、既存の「エンティティ選択 = pickable メッシュ」
アーキテクチャと直行しない編集モード (空間上の自由点の pick、点列の
undo/redo、区間への所属付け、concealment/surface 編集 UI、永続化
トランザクション) を必要とする。M1/M2 の枠内に収まる変更量を超えるため、
正直な設計としてここで区切る。権威側はすでに arbitrary waypoint を保持
できるため、M3 は純粋に入力面の問題に限定されている。

## テスト (`backend/tests/test_issue_1011_cable_routes.py`, 16件)

- M1: 一覧 (端点 bound/unbound/missing、区間宣言長、サービスループ、
  系譜、鮮度 current/stale/missing、信号エッジ解決)、オーバーレイ投影が
  bound 端点にのみ位置を持つこと。
- M2: シールハッシュ + ウェイポイント保存、未宣言区間拒否、divergence
  (数値の分離 + concealment mismatch + unregistered sequences)、鮮度
  語彙、リポジトリ round-trip + append-only + pin 検証、legacy
  length-only run の有効性維持、v115→v116 マイグレーションでテーブル再
  作成 + 既存 run 保持。
- UI: パネル列挙 + `経路形状未登録` 表示、宣言 vs 幾何の併記 + 差分
  警告、幅220px での動作、UIA accessibleName。
