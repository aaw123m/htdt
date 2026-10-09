# Issue #1002 — 実測 ETC ピーク ↔ 予測反射パス 対応レビュー面

「最近接ピーク」の印象で誤対応を確定しないための比較面。実測の ETC 包絡・
宣言済み観測イベント・予測反射パス・シール済み対応権威を一画面で照合する。
すべての表示は既存の権威（`cad_reflection_correspondence`、
`cad_prediction_measurement_registration`、永続化済み
`cad_deterministic_path_artifacts`）からのリプレイであり、UI は一切再判定
しない。最近接ピークの自動マッチャーは実装しない。

## レイヤー構成

- `htdt.reflection_correspondence_review` — Qt 非依存ビューモデル。
  `build_correspondence_review(scene_repository, document_id,
  measurement_id)` が以下を組み立てる:
  - `derive_etc_display_curve(dataset)` — 実測 IR からの表示専用 ETC 包絡
    （平滑化 |h|² 包絡をピーク 0 dB 正規化）。観測値ではないことを UI が
    明示する（パネル見出し「実測 ETC（表示用包絡 — 観測値ではありません）」）。
  - `predicted_path_from_guidance_entry(entry, scene_revision_ref)` —
    `reflection_guidance_presentation.load_reflection_guidance_view` の
    永続化エントリを `PredictedReflectionPath` 権威形式へ写す。
    `interaction_refs` の先頭はシール済み deterministic path 自体
    （`path_id` + `semantic_sha256`）で、対応宣言はこのピンで照合される
    （宣言時と再生時で scene_revision_ref の有無が変わっても一致する）。
  - `CorrespondenceRegistrationRow` — #564 registration の stale 判定
    （`evaluate_registration_freshness`）、timing method / applied offset /
    uncertainty、comparability 状態をそのまま行化。
  - `CorrespondencePairingRow` — 宣言済みペアリングを `predicted_row_id` /
    `observed_row_id` で正確な行へ結合。`manual_expert_label` は
    `is_hypothesis=True`（仮説）として区別される。
  - 可用性: `no_measurement` / `no_ir_dataset` / `no_registration` /
    `no_predicted_paths` / `ready` — 欠落時は理由と現在のナビ対象
    （IR を持つ測定一覧）を提示し、観測を捏造しない。
- `htdt.measurement.ui.reflection_correspondence_panel.ReflectionCorrespondencePanel`
  — 測定ワークスペースの `correspondence` コンテキスト。
  - 上段: 測定選択（IR を持つ測定のみ列挙）＋状態バナー（可用性と理由）。
  - 左ペイン: ETC 包絡プロット（pyqtgraph）。時刻カーソル
    （InfiniteLine）、宣言済み観測イベントのゲート区間
    （LinearRegionItem, ms）、予測到達時刻の紫点線、登録状態表示。
  - 右ペイン: 遅延生成 `RoomViewport3D`（生成失敗時は正直なフォール
    バック文言）＋予測パス表（到達 ms / 面列 / 版 / stale）＋観測
    イベント表（時刻・幅・抽出アルゴリズム・DOA/不確かさ）＋対応表
    （対応状態・観測 ms・予測 ms・Δt・DOA・matching_algorithm・
    evidence dimensions・validation_role・参照）。
  - クリック → `observed_row_id`/`predicted_row_id` の一致で相互強調。
    `unresolved_cluster` / `many_to_one` / `one_to_many` は同じ
    `semantic.warning` 色で全候補同時表示 — 曖昧集合に「正解色」は
    割り当てない。`matched` は scientific.predicted、仮説は
    accent.primary、unmatched は text.muted。
  - 「候補を仮説として保存」— 選択中の予測行 × 観測行（またはカーソル
    ゲート → `observed_event_from_manual_gate`）から
    `build_manual_hypothesis_pairing` で ambiguous・
    `manual_expert_label`・time_alignment のみのシール済み仮説を保存。
    カノニカル評価器は time-only ペアを構造的に qualified できないため
    仮説は絶対に確定判定にならない。実測データセットは一切書き換えない。
- `RoomViewport3D.render_reflection_correspondence_overlay` —
  `correspond-*` 接頭辞（`_OVERLAY_ACTOR_PREFIXES` 登録済み、全
  `pickable=False`）。非選択パスは 1 つのバッチ点群、選択パスのみ
  `pv.Line`/`pv.Sphere` 俳優を生成（実行時負荷要件）。3D 内テキストは
  ASCII のみ（Mesa/VTK の CJK .ttc 制限）、日本語は Qt 側へ。

## 導線

- 測定ワークスペース: 品質詳細カードの「反射対応を確認」ボタン →
  `set_context('correspondence')` → パネルの測定選択にバインド。
- Room ワークスペース: acoustics コンテキストツール帯「反射対応」→
  `WorkspaceDeepLink(WorkspaceId.MEASUREMENT, 'correspondence')`。
- `workflow_navigation.CANONICAL_WORKSPACE_CONTEXTS[MEASUREMENT]` に
  `correspondence` コンテキストを登録（`comparison` と `calibration` の間）。

## 正直さの約束（#677 受け入れ語彙）

- 1:1 / 二面接近 / 曖昧クラスタ / predicted-only / observed-only /
  クロックずれ / DOA なし / 測定帯域不足 — 各々は別の UI 状態として
  描かれ、相互に混ざらない。
- ETC ピーク時刻 ↔ 3D インタラクション位置は正確なソース ID
  （deterministic path pin / measurement_ref.sha）で結ばれる。距離・速度
  からの捏造同定は行わない。
- 再読込時に旧 SHA → 行ごとに `stale` が立つ（登録は
  `evaluate_registration_freshness`、パスは scene_revision_id 比較）。
- 仮説（human selection）は `manual_expert_label` + 時間のみ根拠の
  ambiguous として保存され、正典評価器以外の判定は存在しない。
- ReflectionGeometryPreview / ReflectionGuidanceSession / scrub_source
  とは別レイヤー。外部検証用データをモデル同定に二重利用しない。

## テスト

`backend/tests/test_issue_1002_etc_correspondence.py` — 17 件: 可用性
各状態（no_measurement/no_ir/no_registration/no_paths）、正確な行結合、
曖昧クラスタの誠実表示、unmatched 各状態、stale フラグ、クロックずれ
（estimated 法 + offset + 不確かさ）、DOA なし、仮説保存 → 正典評価で
qualified 不能、ETC 包絡の正規化・表示専用、パネル相互強調・狭幅
(1160px 閾値)・DPI 200 相当・ビューポート生成失敗フォールバック。
