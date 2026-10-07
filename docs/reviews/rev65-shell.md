# REV65-SHELL — UI/UX サーフェス・ディープレビュー

Scope: `application_pages.py` (Capture Inbox / Support / ミッション台帳 /
フィールドリターン照合 UI)、`applicability_envelope_panel.py`、
`golden_path_journey.py`、`overview_workspace.py`、`overview_readiness.py`、
`measurement_page_workspace.py`、`room_workspace.py`、
`workflow_application.py` の UI 面パス、`workflow_shell.py`、および
408ec651..HEAD で変更のあった `*_panel.py` / `*_page.py`。

手法: 408ec651..origin/main の UI 差分を全件精読し、参照する語彙
(MissionPurpose / ReceiverPairing.state / capture_mission_packages.status /
MissionPackage フィールド / OverviewVariantStage / capability_state /
claim_verdicts トークン形式 / undecided・applyable プロパティ /
build_mission・record_return_rebase_decision・apply_returned_tasks の
シグネチャ) を各定義側と照合。配線・フォールバック・モーダル連鎖・
ワーカープール相互作用・refresh 越しの Qt オブジェクト寿命を確認した。

## 実装した修正

| # | 対象 | 症状 | 修正 |
| --- | --- | --- | --- |
| S1 | `application_pages.py` `_sync_contribution_detail` の `_rebase_context` 呼出し | 新規の裸 `except Exception` が全失敗を黙って `None` へ落とし、再基準/適用ボタンを disable にしていた。# error-boundary: コメント無し・記録無しで、sealed store の IntegrityError (改竄) すら「決定対象なし」に偽装する fail-open | `EXPECTED_OPERATION_ERRORS` + `is_authority_failure` → 再 raise + `report_boundary_failure('再基準コンテキストの解決')` で degrade を記録 |
| S2 | 同 `_resolve_return_evidence` の失敗表示 | `f"証跡: 解読失敗（{exc}）"` が `str(exc)` をオペレータ表示に直接混入 (英語の Errno/pydantic ノイズ)。`operation_error_message` を通す既定違反 — 同ファイルの `_run_export` は既に同関数を使用 | `EXPECTED_OPERATION_ERRORS` + authority → raise + `operation_error_message(exc)` で JA マップ済み文言を表示 |
| S3 | 同 `_record_rebase_decision` のタスク選択 | `task_id[:8]`+reason のラベルで `labels.index(choice)` — 8 文字 prefix と reason が衝突した 2 件の要調整タスクで、選んだ 2 件目のラベルが 1 件目へ `index` 解決され、決定が **誤った task_id** に記録される。同ファイルのミッション発行ダイアログが「Labels must be unique」として既に修正済みの同一パターン | 重複ラベルに `[task_id]` 全形を付与して一意化 (device_labels と同じイディオム) |

## Findings (report only)

- `golden_path_journey`: `export` ステップは `verified` で done 化する
  (detail は「いつでも出力できます」)。最終停止点としての意図的な見せ方と
  判断。また各ステップは実状態を独立評価するため、前提が未達のまま後段が
  done になる組合せ (例: room 未完成だが測定 1 件) は表示され得る —
  guidance-only strip の設計どおりと判断し不問。
- `applicability_envelope_panel`: `weakest_dimensions` (`solver_domain` 等)
  と `basis_states` (`capability=supported`) を生トークンで表示。レポート
  側 (`cad_applicability_envelope` L1826) も同一の生トークン描画であり、
  設計上の語彙見せとして一致 — JA 化はドメイン判断のため報告のみ。
- `_refresh_missions` / `_refresh_contributions` の `list_*` 呼出しは
  無ガードで、失敗は Qt スロット経由で uncaught boundary へ落ちる。
  両者で同一スタイル (pre-existing パターン踏襲) のため不問。
- `_sync_contribution_detail` の早期 return が `self._selected_contribution`
  をクリアしない (stale 参照が残る) が、到達には `_active_rebase_context`
  が必要で同時に None 化されるため到達不能。整理用候補。
- 新規ボタン (`issue_button`, `export_mission_button`, `envelope_button`,
  journey ステップ) に `setAccessibleName` なし — 同ファイル内の既存
  ボタン 14/20 も未設定で確立された規約ではないため報告のみ。
- `_export_mission_dialog` の `getSaveFileName` は `.json` 拡張を
  強制しない (ユーザ入力名がそのまま使われる)。書き出すバイトは
  同一パッケージで拡張子は任意 — 運用上の軽微な件。
- `_apply_record` / `_rebase_record` 片側のみ配線された仮定の構成では
  もう片方のボタンが表示済みのまま永遠に disabled — 実配線は常に
  一式なので到達不能。報告のみ。

## 回帰テスト

`backend/tests/test_rev65_shell.py` (5 tests):

- `test_resolve_failure_surfaces_mapped_message_not_raw_text` —
  OSError を投げる resolver で「解読失敗（<JA マップ済み文言>）」を
  確認し生の英文が混入しないことを検証 (pre-fix: 生 `str(exc)` で失敗)
- `test_resolve_authority_failure_propagates_instead_of_degrading` —
  IntegrityError 系が propagate することを検証 (pre-fix: 黙殺で失敗)
- `test_rebase_context_authority_failure_propagates` — 同上 (rebase
  コンテキスト経路)
- `test_rebase_context_expected_failure_degrades_closed` —
  OSError は context=None + ボタン disable で fail-closed (通過は前後で
  不変の characterization)
- `test_rebase_picker_binds_the_picked_task_on_label_collision` —
  8 文字 prefix 衝突した要調整 2 件で、2 件目選択が 2 件目の task_id に
  記録されることを検証 (pre-fix: 1 件目へ記録され失敗)

検証: `git stash` で修正前に戻すと 4/5 が失敗、修正後は全件 pass を確認。
`test_application_pages.py` + `test_golden_path_journey.py` +
`test_overview_readiness.py` + `test_overview_lifecycle.py` 併せて
52 tests が全件 green。
