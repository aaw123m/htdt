# Issue #973 — 測定・最適化・部屋ワークスペースの選択/スクロール/フィルタ/パネル開閉/スプリッタ復元

## Scope

`PersistedWindowState` にはすでに window geometry、最後の workspace、
プロジェクト別の context ID が保存されている。`workflow_shell.py` には
正準 navigation history、Alt+Left/Right、deep-link focus がある。
ナビゲーションそのものは再実装しない。

不足していたのはワークスペース「内部」の表示状態: 長いスクロール位置、
選択中の measurement/comparison、有効なフィルタ、詳細パネルの開閉、
スプリッタ比。再訪ごとに「どこまで見たか」を探し直す認知負荷が残って
いた。本issueではこれを UX convenience 専用の `WorkspaceViewState` と
して追加する — 履歴・authority・証拠は一切置き換えない。

## スキーマ (`window_state.py`)

```python
class WorkspaceViewState(BaseModel, frozen=True):
    scroll_offset: int | None        # 0..10_000_000
    selected_entity: str | None      # 安定 ID (measurement_id, comparison_id,
                                     #  cell_index, objective_id, candidate_id,
                                     #  validation_id, entity id ...)
    filters: dict[str, str]          # 最大32件, key<=160, value<=256文字
    expanded_panels: tuple[str, ...] # 最大32件
    splitter_ratio: float | None     # 0..1
```

- キーは `view_state_key(workspace, context)` = `"{workspace}:{context}"`。
  context のない workspace では workspace id のみ。
- `PersistedWindowState.view_states` に格納。エントリ単位で検証 —
  壊れたエントリは 1 件だけ落とし、geometry・contexts には影響しない。
  不明な workspace のエントリは `_pruned_state` で除去。
- 全体が破損したファイルは `load_window_state` が `None` を返し、
  初期表示に安全にフォールバックする (証拠データに無影響)。
- 値は非秘密・有界・プロジェクトローカルの UI 値のみ (per-project
  `window-state.json` の既存レイアウトに従う)。

## Shell 配線 (`workflow_shell.py` / `workflow_application.py`)

`WorkspaceMount` に 2 つの任意 port を追加:

- `capture_view_state() -> WorkspaceViewState | None`
- `restore_view_state(state) -> None`

Shell は navigate の deactivate 側で「旧 (workspace, context) キー」に
capture を保存し、activate 側で新キーの値を restore する。コンテキスト
切替 (`select_context`) でも同様に旧 context キーへ capture → 新 context
キーから restore。同一 destination/context への再選択では再復元しない
(生きている UI をスナップさせないため)。

- `seed_view_states` (起動時に persisted state を流し込む),
  `collect_view_states` (close hook で全 mount から回収),
  `view_states` / `reset_view_states` を追加。
- `workflow_application._switch_project` は dispose 直後に
  `reset_view_states()` してから rebuild → プロジェクト A/B で
  view-state map が混ざらない (B 側には B の保存値だけが seed される)。
- deep-link は `navigate_to_target` 内で `navigate` → `select_context`
  → `focus_target`/`request_entity` の順なので、明示 focus が常に
  最後に実行され、view-state 復元より優先される。
- `before_deactivate` veto / dirty-state save/discard との矛盾なし:
  キャンセルされた navigate では destination restore が走らず、
  capture は旧状態のコピーを取るだけで表示を変更しない。

## 復元対象

### MeasurementPageWorkspace (`measurement_page_workspace.py`)

- **quality**: フィルタ (verdict / state / channel / position / search
  text)、選択 measurement_id (quality_table col0 の `UserRole`)、
  splitter ratio、スクロール。vanished → `clearSelection` +
  正直な note (`対象の測定…はありません`) + quality_table focus。
- **comparison**: preset combo (再選択が `_refresh_comparison_choices`
  を駆動)、dataset A/B seat、band/ref/excluded spins、ref_band check、
  smooth combo (float data 値で一致)、history row
  (`comparison_id` を UserRole に保存して id 一致で復帰 —
  selectRow 経由で `_history_selection_changed` が sealed record を
  再表示するので「保存済み比較」のラベルは維持される。古い結果を
  current/accepted 扱いしない)。
- **campaign**: plan_id combo seat、`runner_repository.get_run` で
  run 存在確認してから `_campaign_run_id` を再設定して `_refresh_campaign`、
  cell_index (campaign_table col0 `UserRole`) で行復帰。vanished →
  clearSelection + table focus。
- その他の context は scroll のみ。
- スクロール復元は `setValue` を clamp し、`QTimer.singleShot(0, …)` で
  delayed-refresh 後に再適用 (レイアウト再計算で maximum が変わるため)。

### OptimizationWorkflowWorkspace (`optimization_workflow_workspace.py`)

- **candidates**: フィルタテキスト、`search_selected_candidate_id` /
  `extended_candidate` の選択、expanded advanced panels
  (7 サイト: axis_numeric / linked_variables / orientation_search /
  joint_search / extended_candidates / authority_fidelity /
  next_measurements — `_collapsible` ラッパで `_advanced_panels` に登録)、
  candidates splitter ratio。
- **comparison**: `objectives` 選択 ID 群、`pareto_tree` の ROLE 選択。
- **validation**: `validation_tree` の ROLE 選択 (validation_id)。
- 全ページ: スクロール + 現ページ内の expanded panels。
- tree の id 復帰は hidden item をスキップ。vanished → clearSelection +
  親 widget focus。

### RoomWorkspace (`room_workspace.py`)

- `right_stack` の現在 dock ページ (objects / placement / history) の
  スクロール、`controller.view_state.selected_id`、
  `SelectionInspector.expanded_sections` (collapsible QToolButton のみ)、
  `_content_splitter` の最終ペイン比。
- `select_entity` は `controller.document.entities` に存在する id のみ。
  vanished → `select_entity(None)` + inspector focus。

## Tests (`backend/tests/test_view_state.py`)

- モデル境界・往復・破損エントリ個別 drop・未知 workspace prune・
  ファイル破損 → None。
- Shell: workspace 往復 round-trip、context 切替で旧キー保存、
  deep-link focus が restore より後 (優先)、seed の未知 workspace
  排除 + reset (A/B 隔離)、deactivate veto で restore 非適用、
  `collect_view_states` が live mount を回収、composition close →
  per-project save。
- Measurement quality (real): row+filter round-trip、vanished →
  空選択+honest note、index 並べ替え後も id で復帰、
  delayed `_refresh_quality` 後も選択が残る。

## Follow-ups / 留意

- restored view state はあくまで UI 表示位置 — stale 結果を
  current/accepted 扱いしない (sealed comparison の再表示も
  「保存済み比較」ラベルのまま)。
- #936 の journey との再選択操作数比較は real-GUI 検証 (testing agent)
  で計測。
