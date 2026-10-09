# Issue #1004 — 竣工測量の不確かさ・未検証要素オーバーレイ

`RoomViewport3D` の幾何コンテキストに、測量権威
(`backend/src/htdt/cad_geometry_survey.py`, #613) のみを駆動源とする
読み取り専用オーバーレイを追加する。CAD 面に「この壁は何の証跡で、
どの程度の精度で、検証済みか」を色と凡例で示し、判定不能な要素は
絶対に推測で貼り付けない。

## Scope

- **解決対象**: `element_key` → 現行 `SceneRevision` head の安定 ID
  (コンパイル済み執筆面 `floor` / `ceiling` / `wall:<i>` /
  `wall:<i>:opening` 等・scene `entity_id`・R120
  `semantic-surface:<sha>`)。解決不能なキーは理由付きで unmapped。
- **表示モード** (排他): 証拠階層 `tier` / 達成不確かさ
  `uncertainty_mm` / 検証状態 `verification` / 設計–現況差分
  `delta_mm`。`OverlayControls` の「測量」チェック + 「表示…」メニュー
  の測量モード群、および幾何ドック `RoomSurveyPanel` のコンボが同じ
  `survey_mode` を共有する。
- **コントロール測定**: 独立コントロールを点/測線で描画。
  `used_for_registration` は検証合格として表示しない。
  許容差なしは残差のみ表示。
- **クリック先**: オーバーレイ actor は `pickable=False`
  (既存オーバーレイ規約) のため、ドックパネルの行選択が権威詳細
  (キャンペーン・機器・アプリ+バージョン・校正証跡・元ハッシュ・
  ブロック中タスク理由・再確認候補・下流失効成果物) を1操作で開く。
- **ズームアウト**: カメラ距離がシーン対角の 2.5 倍超 (または 12m超)
  では要素個別ラベル/コントロールラベルを描かず、面の着色 + 集計数 +
  ASCII 凡例のみ。細い誤差帯を誇張しない。

## Vocabulary

| bucket | 意味 |
| --- | --- |
| `t0`–`t4` | 証拠階層 (design/user → derived → field → survey → qualified) |
| `le5`/`le25`/`le50`/`gt50`/`unknown` | 達成不確かさ mm。データなしは `unknown` グレー — 0mm/安全緑ではない |
| `verified`/`unverified`/`stale`/`failed`/`unknown` | 検証状態。`failed` は `control_check_failed` 専用 — 静かな未検証に折り畳まない |
| `delta_le2`/`le10`/`le50`/`gt50`/`delta_na`/`none` | 設計–現況差分。差分なしレコードは `delta_na`、レコード自体なしは `none` — どちらも緑にしない |
| unmapped: `missing`/`renumbered`/`non_unique`/`unsupported_frame`/`unreadable` | 面への貼付不能理由 |

## Honesty contract

- `element_key` が一意に解決しない・再採番で消えた・参照キャンペーンの
  座標系が検証不能 (左手系/欠落) のいずれでも面に貼らない。
  「近くの壁」を推測で着色しない。
- `delta_mm` は方向を持たない — 面中央に `Δ ±Nmm` のラベルのみ。
  変位矢印は `direction proven` な情報源が存在しない限り描かない。
- `element_sha256` が封緘 qualification の pinned sha と一致する場合にのみ
  評価状態を表示する。不一致 = unevaluated (unknown)。
- オーバーレイは読み取り専用: qualification / evidence / verdict を
  一切変更しない。精度 (precision) と音響有効性 (acoustic validity) を
  混同しない — タスク verdict は別レイヤとして理由付きで表示する。

## Staleness hookup

`RoomSurveyOverlayController` は `(document_id, head.revision_id,
head.content_hash, qualification_sha256, 全権威レコード sha 群, mode)`
をキャッシュキーに持つ。シーン編集・プロジェクト/head/測量同一性の
不一致は必ず再解決を引き起こし、古い色は次の描画で必ず消える。
コンテキスト離脱・トグルオフ時は `'survey-overlay-'` プレフィックスで
全 actor を掃引 (`_OVERLAY_ACTOR_PREFIXES` 登録済み)。

## Validation

`backend/tests/test_issue_1004_survey_overlay.py` (17 tests):

- 混合証跡 (TLS T3 / 消費者 AR T2 / design-only T0 / unknown-instrument /
  stale) の1部屋共存
- renumbered / missing / non_unique / unsupported_frame → 全て unmapped、
  描画 actor ゼロ
- reg-used コントロール非合格・許容差なし残差のみ・不合格コントロール
- sha/リビジョン変化での stale 色消去・read-only 判定不変
- ASCII 凡例 + 集計 counts・近/遠ズームのラベル出し分け
- viewport prefix/pickable=False/clear・パネル narrow 260px / DPI200
