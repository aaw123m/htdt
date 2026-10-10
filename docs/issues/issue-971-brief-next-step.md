# #971 決定ブリーフ「次の一手」アクション可能ディープリンク

## 概要

Decision Brief パネルは #937 で評決 (推奨可能/条件付き/未整備) を
提示できたが、演算子が次に何をすればよいかは**画面遷移が手作業**で、
欠落証拠を生成する画面まで自分で探す必要があった。

このスライスは各候補アクションの推奨種別・欠落ゲートから、
当該画面へのワンクリック遷移ボタンを生成する。

## 実装

`backend/src/htdt/decision_brief_panel.py`:

- `WorkspaceDeepLink` を取り込み、静的ルートマップを定義。
  - `_REC_ROUTES`: 推奨種別 → ディープリンク
    - `apply_candidate` → 最適化/candidates
    - `remeasure` → 測定/acquisition
    - `verify_channel`, `deploy` → 測定/calibration
    - `collect_evidence` → 各 gap のゲート種別で動的解決
  - `_GAP_GATE_ROUTES`: ゲート種別 → 生成画面ディープリンク
    - `solver_gate` → 部屋/acoustics
    - `channel_verify`, `deployment` → 測定/calibration
    - `campaign` → 測定/campaign
    - `production_gate` → 最適化/validation
    - `comparison` → 最適化/comparison
- `_action_widget`: 候補ごとにカードを構築 — ラベル + 推奨ボタン
  「次の一手へ: <kind>」+ 各欠落証拠「<gate> の証拠を作る画面へ」。
  全ボタンに a11y 名 (rank 付き) を付与。
- `_wire_nav_button`: リンク未解決または navigator 未配線なら
  disabled + tooltip で理由明示 (greyed-out, 決して dead)。
- `_navigate`: `on_navigate(link)` が False/例外なら
  「その画面へ移動できませんでした。」とステータス表示 (fail-closed)。
- `on_navigate` は `Callable[[WorkspaceDeepLink], bool] | None`。
- `optimization_workflow_workspace.py` が `self._on_navigate` を注入
  (既存の `_revalidation_navigate` と同じルート解決経路)。

## 正直性

- 未マップの kind/gate はボタンを disabled + 理由 tooltip。
- ナビゲーション失敗は status bar に明示 (無言無動作にしない)。
- ルートマップ整合性は `_CONTEXT_IDS`/`CANONICAL_WORKSPACE_CONTEXTS`
  との突合テストで継続保証 — 存在しない画面へリンクしない。

## テスト

`backend/tests/test_issue_971_brief_next_step.py` (5テスト):

- ready action → apply_candidate ボタンが enabled + 正リンク解決
- gap ボタンが各ゲートの生成画面に解決
- navigator 未配線時は全ボタン disabled + 理由 tooltip
- 遷移失敗時にステータス表示
- 全ルートマップのセクションが workspace のコンテキストに存在
