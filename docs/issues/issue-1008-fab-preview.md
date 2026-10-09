# #1008 製作パッケージの3D読取プレビュー (FabricationPreview)

## 問題

`cad_treatment_fabrication.py` は封緘済み
`TreatmentFabricationPackage` (部材/カットリスト/井戸表/公差/kerf/
気隙・取付向き) を生成するが、Room ビューから発行済みパッケージを
立体確認する経路が存在しなかった。`treatment_boundary_overlay.py`
はソルバ境界オーバーレイであり本 GUI ではない。

## 実装

**`backend/src/htdt/fabrication_preview.py`** (Qt 非依存リゾルバ、
#1009/#999 と同じ current-head 規律):

- `FabricationPreviewController.arm(package, placement_instance_id)`
  — 発行済み (封緘) パッケージのみ受理。`resolve()` は
  `(head.revision_id, head.content_hash, package_sha256, …)` を
  キーに毎レンダ再解決し、リビジョン替わり/別パッケージ再発行で
  旧アクターが残存しない。
- `SUPPORTED_FAMILIES = {rectangular_panel, qrd_1d}` — それ以外
  (2D QRD・自由形状) は `supported=False` のみ返し、無理な3D化は
  しない (表/カード表示へフォールバック)。
- **矩形パネル**: `overall_depth − Σ part depth` の気隙を derived/
  unspecified のワイヤ枠として壁側に立て、部材は `package.parts`
  順に P1.. の実インデックスで積層 — 描画番号とレポート/カット
  リストの番号は同一規則。
- **1D QRD**: `well_table` 1:1 — well i は `row.well_index == i` を
  検証した上で `row.depth_m` そのまま描く (再計算しない)。フィンは
  生成規則 (j 位置 = 隣接井戸深さの max、深さグループ化 part) から
  `finished_depth_m` 一致で part へ逆写像し、`fin-…@j` インスタンス
  として P 番号を共有。well ラベルは24個上限・表は全行表示。
- **参考配置**: canonical (current head で binding 'exact' +
  footprint 導出可能) な `AcousticTreatmentPlacement` の場合のみ、
  取付矩形の面投影リング + authored rect を `overall_depth` 押し
  出したクリアランス包絡を REFERENCE ジオメトリとして描画。
  canonical でなければ中立アンカー (部屋 −Y 縁) + 明示通知。
  いかなる場合も工場寸法を音響境界モデルへ戻さない。
- `build_fabrication_report(package)` — 決定的な Markdown レポート:
  pinned 識別 (package_sha256 + definition_sha256 + spec_version +
  supersedes)、部材表 (P番号・寸法・数量・材料)、カットリスト
  (公差は grounded のみ・未指定は `unspecified`)、井戸表、BOM、
  図面一覧、警告、設計値由来の免責。

**`backend/src/htdt/room_fabrication_panel.py`**: 非モーダル
`FabricationPreviewDialog` — 定義選択 (FAB10/FAB20 以外は発行不可 +
理由表示)、明示 FabricationSpec 入力 (公差は未チェック = unspecified)、
QRD パラメータ群 (素数・設計周波数・周期・井戸幅/フィン厚/背板厚)、
発行ボタン → `previewRequested` でホストへ受渡。パーツ/カット/
井戸の3表は package 1:1、行選択が `previewSelectionChanged` で
3D ハイライト + カットリスト行の材料/公差/数量を表示。断面・分解
スライダは `previewViewChanged`。「閉じる」= `previewCleared` で
アクター掃除。レポート出力は上記同一レンダラ。

**`backend/src/htdt/room_viewport.py`**: `_OVERLAY_ACTOR_PREFIXES`
に `'fabrication-'` 追加 + `render_fabrication_preview(scene)` /
`clear_fabrication_preview()`。部材はアンカー軸で向き付けた
pv.Cube (domain→render で y 反転)、選択は warning 色 + エッジ強調、
断面は clip + 断面輪郭線、全アクター non-pickable・`fabrication-`
prefix で一括掃除。VTK テキストは ASCII のみ (CJK は落ちる)、
JA 文案は `summary_ja` で Qt 側へ。

**`backend/src/htdt/room_workspace.py`**: コントローラ生成
(~4258) + コンポジタの `deferred_render` ブロックで
`treatment_overlay` 直後に描画 + `show_/clear_/update_/select_
fabrication_part` ファサード。

**`backend/src/htdt/room_acoustics_panel.py`**: RoomTreatmentPanel
末尾に「製作プレビュー…」ボタン (幅予算への影響なし)。
`workflow_application.py` は `treatment_panel.fabrication_host =
workspace` の1行接続。

## テスト (`test_issue_1008_fab_preview.py`、13件)

- パネル: derived 気隙 (index 0・unspecified・wireframe) + 部材
  P番号/寸法/stack 位置一致、viewport_lines 全 ASCII
- QRD: well 表 1:1 (7 well・label・深さ)、フィン深さ = 隣接 max、
  深さ0の未発行フィンは描画しない、P番号共有
- 未対応 family (`qrd_2d` via model_copy) → supported=False・部材0
- canonical 配置 → REF リング (z=1 平面) + クリアランス包絡 +
  「REF mount+clearance」表示
- 失効配置 (2nd revision) → 中立アンカー + not canonical 通知
- 選択 (cut_group キー) → 該当部材ハイライト + 材料/数量/公差詳細、
  不一致キーは明示通知
- レポート: P番号・cut_group・井戸表・sha ピン・fin 公差 grounded・
  unspecified 表示・設計値由来免責
- ビュー制御: section 位置・exploded の stack 方向移動
- disarm→None・再発行 (新 sha + supersedes) で旧描画なし
- viewport: `fabrication-` アクター命名・non-pickable・
  `_remove_overlay_actors` 一括掃除・断面アクター
- dialog: 発行→host 受渡・3表 1:1・行選択→select・スライダ→
  view・閉じる→clear・パネル定義は qrd 群非表示

※ `test_accessible_labels::test_mounted_destinations_have_no_
unnamed_controls` は base commit (0ecb4994) で既に10件の既存
unnamed control を検出して fail する既知不具合 (seatCoverage /
correspondence / ActivityPage 由来) — 本変更は unnamed を増やさない。

## スコープ外

ネスティング最適化・構造/耐荷重認定・CNC 出力・吸音率/散乱性能の
評価 (別権威)・境界モデルへのフィードバック。
