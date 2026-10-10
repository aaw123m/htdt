# Issue #1011 — ケーブル配線の検査一覧 + 経路ジオメトリ権威 (REV73)

M1 + M2 + M3 を実装。M3 の構造的注記と実装形態は末尾に記載。

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

## M3: 3D ウェイポイント記録 (`cable_run_waypoint.py`)

defer 注記で挙げた入力面の問題を、計測ツール (`RoomMeasureController`)
と同じ「専用編集モードがエンティティ選択と共存する」形で解決した。

- `CableRunWaypointController` — 記録セッション (対象 run、点列 +
  undo/redo、確定済み区間の集合、concealment/通過面/source/record_kind/
  note のフィールド) を所有。`begin(run_id)` は検査一覧を解決して宣言
  区間を束縛し、`RoomViewport3D.set_waypoint_pick_armed(True)` でモード
  に入る。コミットは `CadCableRunGeometryRepository.save_geometry` のみ
  — run は begin 時のスナップショットではなくコミット時点の最新版に
  再ピン留めし (stale pin は失敗しても新しい版を上書きしない)、失敗時は
  セッションと記録点を保持する。同一 geometry_id への再記録は
  `latest_geometry_for_run` で version を進めて append-only を守る。
- ピックの honest な解決: armed 中は `room-floor` / `room-shell` /
  `authoring-surface-*` も pickable になり、全 pick が `waypointPicked`
  (ドメイン座標) に流れる。エンティティ上のクリックはその物体表面の
  点を記録し (選択にはならない)、何も拾えないクリックは
  `pick_waypoint_domain` が部屋境界 (床・外周壁・天井平面) とのレイ交差
  にフォールバックする — 空間の自由点は作らない。disarm で pickable
  状態とポップオーバー許可を完全に戻す。
- 下書きオーバーレイ `render_cable_route_draft` — 記録中の点列と確定済み
  区間を `cable-route-draft-` プレフィクスの stale 色で描く。登録済み経路
  の accent/warning 系と色を分け、常に non-pickable・非永続。
- `CableRunWaypointPanel` — 配線一覧の詳細の下にマウント (記録対象が
  選ばれた場所 = 記録 UI の場所)。区間コンボは宣言済み区間のみを列挙
  (宣言外への記録は選択肢自体が存在しない)、concealment は区間の宣言
  path_kind を既定値として引き継ぐ。全コントロールに JA 文字列と
  accessibleName。点の undo/redo は記録中の点列のみに作用し、シーンの
  編集には触れない。
- ワークスペース配線: Esc → `cancel_active_operation` (記録中止は選択
  解除より先)、`before_deactivate` は計測同様にセッションを畳む、
  `emptyClicked` は境界フォールバック記録、marquee は記録中は無効。
  `cable_run_panel.refresh()` 後に `refresh_run` で対象 run を再解決
  (消失 → セッション終了、区間離脱 → 警告)。

## テスト

`backend/tests/test_issue_1011_cable_routes.py` (16件) +
`backend/tests/test_issue_1011_cable_waypoints.py` (M3, 24件):

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
- M3: 記録トランザクション (begin→点→確定→保存で sealed レコードが
  正しい pin/フィールドで着地)、未確定点の保存拒否、宣言外区間の
  fail closed、undo/redo が点列のみに作用、コミットが run 最新版に
  再ピン (stale pin は新しい版を上書きしない)、append-only な version
  追加、対象消失でセッション終了、部屋境界レイ交差、draft オーバーレイ
  の prefix/non-pickable/ドメイン→レンダー変換、ワークスペース配線
  (pick・Esc・パネルマウント・a11y)。
